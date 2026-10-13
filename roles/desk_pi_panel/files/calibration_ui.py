"""Local touch calibration only: raw samples, independent validation, explicit save."""
import json
import math
import os
from pathlib import Path
import statistics
import time

TARGETS=((44,60),(436,60),(436,242),(44,242),(240,150))
MAX_ERROR=16
STATE_PATH=Path('/var/lib/desk-cluster-panel/calibration.json')

def valid_transform(value):
    return isinstance(value,list) and len(value)==6 and all(type(v) in (float,int) and math.isfinite(v) and abs(v)<1e6 for v in value) and abs(value[0]*value[4]-value[1]*value[3])>1e-6

def physical(point,rotation):
    x,y=point
    return (479-x,319-y) if rotation==180 else (x,y)

def project(raw,transform,rotation):
    x,y=raw;a,b,c,d,e,f=transform
    return physical((a*x+b*y+c,d*x+e*y+f),rotation)

def fit(samples,rotation):
    if len(samples)!=4:raise ValueError('Four corner samples required')
    design=[[float(x),float(y),1.0] for x,y in samples]
    output=[physical(t,rotation) for t in TARGETS[:4]]
    def solve(axis):
        rows=[[sum(r[i]*r[j] for r in design) for j in range(3)]+[sum(r[i]*v[axis] for r,v in zip(design,output))] for i in range(3)]
        for c in range(3):
            pivot=max(range(c,3),key=lambda r:abs(rows[r][c]));rows[c],rows[pivot]=rows[pivot],rows[c]
            if abs(rows[c][c])<1e-7:raise ValueError('Points overlap; retry calibration')
            div=rows[c][c];rows[c]=[v/div for v in rows[c]]
            for r in range(3):
                if r!=c:
                    factor=rows[r][c];rows[r]=[a-factor*b for a,b in zip(rows[r],rows[c])]
        return [r[3] for r in rows]
    transform=solve(0)+solve(1)
    if not valid_transform(transform):raise ValueError('Invalid mapping; retry calibration')
    residual=max(math.dist(project(raw,transform,rotation),target) for raw,target in zip(samples,TARGETS[:4]))
    if residual>MAX_ERROR:raise ValueError('Corner taps disagree; retry carefully')
    return transform,residual

def stable_sample(points,duration):
    # Use per-axis median; discard edge transients from touch-down/lift.
    if len(points)<5 or duration<0.12:raise ValueError('Hold the target briefly, then lift')
    trim=len(points)//5
    core=points[trim:len(points)-trim] if trim else points
    x=statistics.median(p[0] for p in core);y=statistics.median(p[1] for p in core)
    deviations=sorted(max(abs(p[0]-x),abs(p[1]-y)) for p in core)
    if deviations[min(len(deviations)-1,int(len(deviations)*0.9))]>160:
        raise ValueError('Touch moved; tap the same target again')
    return x,y

class Calibration:
    def __init__(self,rotation,now=None):
        self.rotation=rotation
        self.phase='confirm';self.samples=[];self.transform=None
        self.error=None;self.message='Current mapping stays until you choose Apply.'
        self.marker=None;self.version=0
        self.deadline=(time.monotonic() if now is None else now)+90
    def start(self,now):
        self.phase='collect';self.samples=[];self.transform=None;self.error=None
        self.marker=None;self.message='Hold the target briefly, then lift.'
        self.deadline=now+90;self.version+=1
    def sample(self,points,duration,now):
        try:
            point=stable_sample(points,duration)
            if len(self.samples)<4:
                self.samples.append(point)
                if len(self.samples)==4:self.transform,self.error=fit(self.samples,self.rotation)
                self.message='Hold the target briefly, then lift.'
            else:
                centre_error=math.dist(project(point,self.transform,self.rotation),TARGETS[4])
                self.error=max(self.error,centre_error)
                if self.error>MAX_ERROR:raise ValueError('Centre check missed; retry calibration')
                self.phase='review';self.message='Tap the test area; crosshair should follow.'
            self.deadline=now+90
        except ValueError as error:
            self.message=str(error)
            if len(self.samples)>=4:self.phase='failed';self.transform=None
        self.version+=1
    def cancel(self):self.phase='cancelled';self.version+=1
    def tick(self,now):
        if now>=self.deadline and self.phase not in ('cancelled','applied'):
            self.cancel();return True
        return False
    def preview(self,point):
        if self.phase=='review' and self.transform:
            x,y=project(point,self.transform,self.rotation)
            self.marker=(max(0,min(479,round(x))),max(0,min(219,round(y))))
            self.version+=1
    def apply(self,config,path=STATE_PATH):
        if self.phase!='review' or not valid_transform(self.transform):raise ValueError('Calibration not validated')
        save(self.transform,self.error,path)
        config['touch_transform']=list(self.transform)
        self.phase='applied';self.version+=1

def save(transform,error,path=STATE_PATH):
    if not valid_transform(transform) or type(error) not in (float,int) or not math.isfinite(error) or not 0<=error<=MAX_ERROR:
        raise ValueError('Invalid validated calibration')
    path=Path(path);path.parent.mkdir(parents=True,exist_ok=True,mode=0o700)
    temp=path.with_suffix('.tmp')
    try:
        fd=os.open(temp,os.O_WRONLY|os.O_CREAT|os.O_TRUNC,0o600)
        with os.fdopen(fd,'w') as stream:
            json.dump({'protocol':'desk-panel-touch-v1','transform':transform,'error_pixels':error},stream)
            stream.flush();os.fsync(stream.fileno())
        os.replace(temp,path)
        try:
            directory=os.open(path.parent,os.O_DIRECTORY)
            try:os.fsync(directory)
            finally:os.close(directory)
        except OSError:pass  # Replacement committed; the active mapping must match the saved file.
    finally:temp.unlink(missing_ok=True)

def restore(config,path=STATE_PATH):
    try:
        raw=Path(path).read_bytes()
        if len(raw)>2048:return False
        value=json.loads(raw)
        if value.get('protocol')!='desk-panel-touch-v1' or not valid_transform(value.get('transform')):return False
        error=value.get('error_pixels')
        if type(error) not in (int,float) or not math.isfinite(error) or not 0<=error<=MAX_ERROR:return False
        config['touch_transform']=value['transform'];config['calibration_error']=error
        return True
    except (OSError,ValueError,TypeError,AttributeError):return False
