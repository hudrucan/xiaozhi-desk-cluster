#!/usr/bin/python3
"""Read-only 480x320 cluster panel; cached GET, native RGB565, evdev touch."""
import argparse
import ctypes
from datetime import datetime, timezone
import fcntl
import ipaddress
import json
import math
import mmap
import os
from pathlib import Path
import select
import signal
import struct
import threading
import time
from urllib.parse import urlparse
from urllib.request import build_opener, ProxyHandler, Request, HTTPRedirectHandler
from PIL import Image, ImageDraw, ImageFont, ImageChops
from calibration_ui import Calibration, TARGETS, restore as restore_calibration

WIDTH, HEIGHT = 480, 320
MEMBERS = ('deskb1x', 'deskb2x', 'deskb3x')
BG, SURFACE, BORDER = '#100d08', '#211b12', '#443721'
TEXT, MUTED, GOLD, GREEN, RED = '#f3ead8', '#baad95', '#d5a85e', '#96c9a0', '#de927f'
CALIBRATE_BUTTON = (130, 195, 350, 243)
START_BUTTON, CONFIRM_CANCEL_BUTTON = (100, 243, 228, 291), (252, 243, 380, 291)
APPLY_BUTTON, RETRY_BUTTON, CANCEL_BUTTON = (32, 243, 160, 291), (176, 243, 304, 291), (320, 243, 448, 291)
COLLECT_CANCEL_BUTTON = (358, 275, 466, 319)

def inside(point, box):
    return box[0] <= point[0] <= box[2] and box[1] <= point[1] <= box[3]

def finite(value):
    return type(value) in (int, float) and math.isfinite(value)

def timestamp(value):
    try:
        dt = datetime.fromisoformat(value.replace('Z', '+00:00'))
        return dt.timestamp() if dt.tzinfo else None
    except (ValueError, AttributeError, TypeError):
        return None

def validate(value):
    if not isinstance(value, dict) or value.get('protocol') != 'desk-cluster-panel-v1':
        raise ValueError('Panel protocol mismatch')
    if value.get('state') not in ('ready', 'degraded', 'unavailable'):
        raise ValueError('Invalid panel state')
    nodes = value.get('nodes')
    if not isinstance(nodes, list) or len(nodes) != 3 or any(not isinstance(n,dict) for n in nodes):
        raise ValueError('Invalid panel membership')
    if tuple(n.get('id') for n in nodes) != MEMBERS:
        raise ValueError('Unexpected panel membership')
    for n in nodes:
        if type(n.get('online')) is not bool:
            raise ValueError('Invalid node availability')
        for field in ('cpu_percent','memory_percent','temperature_c','age_ms'):
            v=n.get(field)
            if v is not None and (not finite(v) or v<0 or v>1e9):
                raise ValueError('Invalid node value')
        for field in ('jobs','components','configuration','admission_paused','vip'):
            if not isinstance(n.get(field),dict):
                raise ValueError('Invalid node metadata')
        for v in n['jobs'].values():
            if v is not None and (type(v) is not int or not 0<=v<=2):
                raise ValueError('Invalid activity')
    if not isinstance(value.get('owners'),dict) or not isinstance(value.get('dns'),dict):
        raise ValueError('Invalid routing metadata')
    for key in ('dns','xiaozhi'):
        o=value['owners'].get(key)
        if not isinstance(o,dict) or o.get('state') not in ('known','conflict','unknown') or (o.get('node') is not None and o['node'] not in MEMBERS):
            raise ValueError('Invalid owner')
    if value['dns'].get('mode') not in ('adguard','fallback','unavailable','unknown'):
        raise ValueError('Invalid DNS mode')
    events=value.get('events')
    if not isinstance(events,list) or len(events)>3 or any(not isinstance(e,dict) or not isinstance(e.get('message'),str) or len(e['message'])>256 or e.get('node') not in MEMBERS for e in events):
        raise ValueError('Invalid events')
    for n in nodes:
        for config in n['configuration'].values():
            if not isinstance(config,dict) or config.get('state') not in ('synced','pending','failed','unknown'):
                raise ValueError('Invalid config state')
        if not isinstance(n.get('ip'),str) or len(n['ip'])>64:
            raise ValueError('Invalid node IP')
    if timestamp(value.get('observed_at')) is None:
        raise ValueError('Invalid observation timestamp')
    if not finite(value.get('age_ms')) or not 0<=value['age_ms']<=1e12:
        raise ValueError('Invalid observation age')
    return value

class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self,*args):return None

class Model:
    def __init__(self):
        self.lock=threading.Lock()
        self.value=None
        self.received=0
        self.ok=False
        self.stopped=threading.Event()
        self.opener=build_opener(ProxyHandler({}),NoRedirect())
    def poll(self,url):
        while not self.stopped.is_set():
            try:
                with self.opener.open(Request(url,headers={'Accept':'application/json'}),timeout=3) as r:
                    raw=r.read(8193)
                    if len(raw)>8192:raise ValueError('Oversize snapshot')
                    value=validate(json.loads(raw))
                with self.lock:
                    self.value,self.received,self.ok=value,time.monotonic(),True
            except (OSError,ValueError,TypeError):
                with self.lock:self.ok=False
            self.stopped.wait(5)
    def snapshot(self):
        with self.lock:
            return self.value,self.received,self.ok

def freshness(value,received,now):
    if not value or not received:return False,0
    elapsed=max(0,(now-received)*1000)
    age=value['age_ms']+elapsed
    return age<=20000 and value['state']!='unavailable',elapsed

def node_live(n,live,elapsed):
    age=n.get('age_ms')
    return live and n['online'] and finite(age) and age+elapsed<=20000

class Renderer:
    def __init__(self,font_root='/usr/share/fonts/truetype/dejavu'):
        root=Path(font_root)
        self.fonts={size:ImageFont.truetype(str(root/'DejaVuSans.ttf'),size,layout_engine=ImageFont.Layout.BASIC) for size in (11,12,13,14,16,18,22)}
        self.bold={size:ImageFont.truetype(str(root/'DejaVuSans-Bold.ttf'),size,layout_engine=ImageFont.Layout.BASIC) for size in (12,13,14,16,18,22)}
    def text(self,d,xy,value,size=12,color=TEXT,bold=False,width=None):
        value=str(value)
        font=(self.bold if bold else self.fonts)[size]
        if width:
            while value and d.textlength(value,font=font)>width:
                value=value[:-2]+'…' if not value.endswith('…') else value[:-2]+'…'
        d.text(xy,value,font=font,fill=color)
    def draw(self,value,received,ok,page='overview',selected=0,now=None):
        now=time.monotonic() if now is None else now
        live,elapsed=freshness(value,received,now)
        image=Image.new('RGB',(WIDTH,HEIGHT),BG);d=ImageDraw.Draw(image)
        font=self.bold[14];bounds=d.textbbox((0,0),'DESK CLUSTER',font=font)
        d.text((14-bounds[0],(35-(bounds[3]-bounds[1]))/2-bounds[1]),'DESK CLUSTER',font=font,fill=GOLD)
        status='Connecting' if value is None else 'Stale' if not live else 'Reconnecting' if not ok else value['state'].title()
        self.right_text(d,(330,0,466,35),status,12,GREEN if live and ok and value['state']=='ready' else RED)
        d.line((14,35,466,35),fill=BORDER)
        if page=='overview':self.overview(d,value,live,elapsed)
        elif page=='node':self.node(d,value,live,elapsed,selected)
        else:self.events(d,value,live)
        self.navigation(d,page)
        return image
    def button(self,d,box,label,primary=False):
        d.rounded_rectangle(box,radius=8,fill=GOLD if primary else SURFACE,outline=GOLD if primary else BORDER)
        self.center_text(d,box,label,14,BG if primary else TEXT,True)
    def center_text(self,d,box,label,size,color,bold=False):
        font=(self.bold if bold else self.fonts)[size]
        left,top,right,bottom=d.textbbox((0,0),label,font=font)
        x=(box[0]+box[2]-(right-left))/2-left
        y=(box[1]+box[3]-(bottom-top))/2-top
        d.text((x,y),label,font=font,fill=color)
    def right_text(self,d,box,label,size,color):
        font=self.fonts[size]
        left,top,right,bottom=d.textbbox((0,0),label,font=font)
        d.text((box[2]-right,(box[1]+box[3]-(bottom-top))/2-top),label,font=font,fill=color)
    def columns(self,d,box,count):
        d.rounded_rectangle(box,radius=8,fill=SURFACE,outline=BORDER)
        width=(box[2]-box[0])/count
        for i in range(1,count):
            x=round(box[0]+i*width)
            d.line((x,box[1]+9,x,box[3]-9),fill=BORDER)
        return [(round(box[0]+i*width),box[1],round(box[0]+(i+1)*width),box[3]) for i in range(count)]
    def settings(self,config):
        image=Image.new('RGB',(WIDTH,HEIGHT),BG);d=ImageDraw.Draw(image)
        self.text(d,(14,12),'Panel settings',18,TEXT,True)
        self.text(d,(14,49),'Touch calibration',16,GOLD,True)
        self.text(d,(14,77),'Recalibrate for the mounted screen.',13,MUTED)
        self.text(d,(14,100),'Use a stylus or a steady fingertip.',13,MUTED)
        self.text(d,(14,131),f'Mount rotation: {config.get("rotation",0)} degrees',12,MUTED)
        if finite(config.get('calibration_error')):
            self.text(d,(14,153),f'Last validated error: {config["calibration_error"]:.1f} px',12,GREEN)
        self.button(d,CALIBRATE_BUTTON,'Calibrate touch',True)
        self.navigation(d,'settings')
        return image
    def navigation(self,d,page):
        d.line((0,275,480,275),fill=BORDER)
        for i,(label,target) in enumerate((('Overview','overview'),('Nodes','node'),('Events','events'),('Settings','settings'))):
            if page==target:d.rectangle((i*120,276,(i+1)*120-1,319),fill=SURFACE)
            self.center_text(d,(i*120,276,(i+1)*120-1,319),label,13,GOLD if page==target else MUTED,bold=page==target)
    def calibration(self,cal):
        image=Image.new('RGB',(WIDTH,HEIGHT),BG);d=ImageDraw.Draw(image)
        self.text(d,(14,10),'Touch calibration',16,TEXT,True)
        if cal.phase=='confirm':
            self.text(d,(26,66),'Tap four corners and the centre.',16,TEXT,True)
            self.text(d,(26,106),'Old mapping stays until Apply.',14,MUTED)
            self.text(d,(26,139),'Cancel or timeout keeps current settings.',12,MUTED)
            self.button(d,START_BUTTON,'Start',True)
            self.button(d,CONFIRM_CANCEL_BUTTON,'Cancel')
        elif cal.phase=='collect':
            index=len(cal.samples);tx,ty=TARGETS[index]
            d.ellipse((tx-24,ty-24,tx+24,ty+24),outline=GOLD,width=3)
            d.line((tx-12,ty,tx+12,ty),fill=GOLD,width=2);d.line((tx,ty-12,tx,ty+12),fill=GOLD,width=2)
            if index!=4:
                self.text(d,(93,124),f'Target {index+1} / 5',18,GOLD,True)
                self.text(d,(93,158),cal.message,12,MUTED,width=330)
            else:
                self.text(d,(120,75),'Centre check · 5 / 5',16,GOLD,True)
                self.text(d,(56,191),cal.message,12,MUTED,width=368)
            self.button(d,COLLECT_CANCEL_BUTTON,'Cancel')
            self.text(d,(14,291),'Hold anywhere 2s to cancel',11,MUTED,width=307)
        else:
            good=cal.phase=='review'
            self.text(d,(14,43),'Check the new mapping' if good else 'Calibration needs retry',16,GREEN if good else RED,True)
            if good:
                self.text(d,(14,72),f'Validated error: {cal.error:.1f} px',13,GREEN)
                d.rounded_rectangle((14,101,466,219),radius=8,fill=SURFACE,outline=BORDER)
                self.text(d,(30,110),cal.message,12,MUTED,width=422)
                if cal.marker:
                    x,y=cal.marker
                    if 14<=x<=466 and 101<=y<=219:
                        d.ellipse((x-10,y-10,x+10,y+10),outline=GOLD,width=2)
                        d.line((x-14,y,x+14,y),fill=GOLD);d.line((x,y-14,x,y+14),fill=GOLD)
            else:
                self.text(d,(14,90),cal.message,14,MUTED,width=450)
                self.text(d,(14,124),'Your previous mapping is still active.',13,MUTED)
            if good:self.button(d,APPLY_BUTTON,'Apply',True)
            self.button(d,RETRY_BUTTON,'Retry',not good)
            self.button(d,CANCEL_BUTTON,'Cancel')
        return image
    def owner(self,value,key,live,elapsed):
        o=(value or {}).get('owners',{}).get(key,{})
        node=next((n for n in (value or {}).get('nodes',[]) if n['id']==o.get('node')),None)
        return o['node'] if node and node_live(node,live,elapsed) and o.get('state')=='known' else 'Unconfirmed'
    def overview(self,d,value,live,elapsed):
        self.text(d,(14,45),'Xiaozhi',12,MUTED)
        self.text(d,(80,45),self.owner(value,'xiaozhi',live,elapsed),12,GOLD)
        self.text(d,(259,45),'DNS',12,MUTED)
        self.text(d,(300,45),self.owner(value,'dns',live,elapsed),12,GREEN)
        for i in range(3):
            x=14+i*155;n=value['nodes'][i] if value else None
            online=n is not None and node_live(n,live,elapsed)
            d.rounded_rectangle((x,75,x+142,224),radius=10,fill=SURFACE,outline=BORDER)
            self.text(d,(x+12,85),MEMBERS[i],14,TEXT,True)
            d.ellipse((x+123,92,x+129,98),fill=GREEN if online else RED)
            d.line((x+12,111,x+130,111),fill=BORDER)
            if not online:
                self.text(d,(x+12,126),'Telemetry',12,MUTED)
                self.text(d,(x+12,149),'unavailable',12,RED)
                self.text(d,(x+12,199),'Unconfirmed',11,MUTED)
            else:
                jobs=n['jobs'];busy=[label for key,label in (('asr','ASR'),('llm_vlm','LLM/VLM'),('tts','TTS')) if finite(jobs.get(key)) and jobs[key]>0]
                activity=' + '.join(busy) if busy else 'Paused' if any(v is True for v in n['admission_paused'].values()) else 'Voice idle' if all(jobs.get(k)==0 for k in ('asr','llm_vlm','tts')) else 'Voice unknown'
                self.text(d,(x+12,119),activity,12,GOLD if busy else MUTED,width=118)
                cpu=n.get('cpu_percent');temp=n.get('temperature_c');mem=n.get('memory_percent')
                self.text(d,(x+12,142),f'{cpu:.0f}% CPU' if finite(cpu) else 'CPU —',16,TEXT,True)
                detail=f'{mem:.0f}% RAM' if finite(mem) else 'RAM —'
                if finite(temp):detail+=f' · {temp:.0f}°C'
                self.text(d,(x+12,169),detail,11,MUTED,width=118)
                d.line((x+12,193,x+130,193),fill=BORDER)
                role='Xiaozhi VIP' if n['vip'].get('xiaozhi') is True else 'DNS VIP' if n['vip'].get('dns') is True else 'Standby'
                self.text(d,(x+12,199),role,11,GOLD,width=118)
        mode=(value or {}).get('dns',{}).get('mode')
        label={'adguard':'AdGuard filtering','fallback':'Fallback · 1.1.1.1','unavailable':'DNS unavailable'}.get(mode,'DNS unconfirmed') if live else 'DNS unconfirmed'
        online=sum(node_live(n,live,elapsed) for n in value['nodes']) if value else 0
        self.text(d,(14,236),label,12,GREEN if live and mode=='adguard' else RED)
        self.right_text(d,(340,232,466,254),f'{online}/3 online',12,MUTED)
        self.text(d,(14,256),'Touch a node for details',11,MUTED)
    def node(self,d,value,live,elapsed,index):
        for i in range(3):
            x=14+i*155
            d.rounded_rectangle((x,45,x+142,79),radius=7,fill=SURFACE if i==index else BG,outline=GOLD if i==index else BORDER)
            self.center_text(d,(x,45,x+142,79),MEMBERS[i],12,GOLD if i==index else MUTED,True)
        n=value['nodes'][index] if value else None
        online=n is not None and node_live(n,live,elapsed)
        self.text(d,(14,92),n.get('ip','') if n else 'Waiting for cluster',13,TEXT,True)
        self.right_text(d,(320,87,466,111),'Online' if online else 'Unavailable',12,GREEN if online else RED)
        if not online:
            d.rounded_rectangle((14,123,466,258),radius=8,fill=SURFACE,outline=BORDER)
            self.text(d,(28,145),'Fresh telemetry required',16,RED)
            self.text(d,(28,179),'Owner, activity and health are unconfirmed.',12,MUTED,width=424)
            return
        boxes=self.columns(d,(14,119,466,169),3)
        for box,(label,field,suffix) in zip(boxes,(('CPU','cpu_percent','%'),('Memory','memory_percent','%'),('Temp','temperature_c','°C'))):
            v=n.get(field)
            self.text(d,(box[0]+12,125),label,11,MUTED)
            self.text(d,(box[0]+12,143),f'{v:.1f}{suffix}' if finite(v) else '—',16,TEXT,True)
        boxes=self.columns(d,(14,178,466,218),4)
        for box,(label,key) in zip(boxes,(('Voice','worker'),('Core','core'),('MQTT','mqtt'),('DNS','dns'))):
            ready=n['components'].get(key)=='ready'
            self.text(d,(box[0]+12,182),label,11,MUTED)
            self.text(d,(box[0]+12,199),'Ready' if ready else 'Unknown' if n['components'].get(key) is None else 'Not ready',11,GREEN if ready else RED,width=89)
        boxes=self.columns(d,(14,227,466,268),3)
        for box,(label,key) in zip(boxes,(('Xiaozhi','xiaozhi'),('DNS','dns'),('Monitor','monitoring'))):
            cfg=n['configuration'].get(key,{})
            txt=f'{label} · r{cfg["revision"]}' if type(cfg.get('revision')) is int else label+' · —'
            self.text(d,(box[0]+12,231),txt,11,MUTED,width=126)
            self.text(d,(box[0]+12,249),cfg.get('state','unknown').title(),11,GREEN if cfg.get('state')=='synced' else RED,width=126)
    def events(self,d,value,live):
        self.text(d,(14,46),'Recent events',14,TEXT,True)
        if not live:self.right_text(d,(310,41,466,67),'Cached',12,RED)
        events=(value or {}).get('events',[])
        events=events[:3] if isinstance(events,list) else []
        if not events:
            d.rounded_rectangle((14,81,466,140),radius=8,fill=SURFACE,outline=BORDER)
            self.text(d,(28,102),'No retained observations',13,MUTED)
        for i,e in enumerate(events):
            if not isinstance(e,dict):continue
            y=81+i*63
            d.rounded_rectangle((14,y,466,y+54),radius=8,fill=SURFACE,outline=BORDER)
            self.text(d,(28,y+7),e.get('node',''),12,GOLD,width=120)
            dt=timestamp(e.get('at'));t=datetime.fromtimestamp(dt).astimezone().strftime('%H:%M:%S') if dt else '—'
            self.right_text(d,(336,y+4,452,y+27),t,11,MUTED)
            self.text(d,(28,y+29),e.get('message',''),12,TEXT,width=424)
        if not live:self.text(d,(14,256),'Current cluster state is unconfirmed',11,RED,width=452)

def validate_framebuffer(v,stride):
    if (v[0],v[1],v[6],v[8:10],v[11:13],v[14:16]) != (480,320,16,(11,5),(5,6),(0,5)) or v[4] or v[5] or stride!=960:
        raise ValueError('Expected 480x320 RGB565 without viewport offsets')

class Framebuffer:
    def __init__(self,path,packer,rotation=0):
        if rotation not in (0,180):raise ValueError("Supported panel rotation: 0 or 180")
        self.rotation=rotation
        self.fd=os.open(path,os.O_RDWR)
        v=struct.unpack('40I',fcntl.ioctl(self.fd,0x4600,bytes(160)))
        self.width,self.height=v[:2]
        name=Path(path).name
        stride=int(Path('/sys/class/graphics',name,'stride').read_text())
        validate_framebuffer(v,stride)
        self.buffer=mmap.mmap(self.fd,stride*self.height,mmap.MAP_SHARED,mmap.PROT_WRITE|mmap.PROT_READ)
        self.lib=ctypes.CDLL(packer);self.pack=self.lib.desk_rgb565
        self.pack.argtypes=(ctypes.c_void_p,ctypes.c_void_p,ctypes.c_size_t);self.pack.restype=None
        self.output=ctypes.create_string_buffer(WIDTH*HEIGHT*2);self.previous=None
    def show(self,image):
        if self.rotation:image=image.transpose(Image.Transpose.ROTATE_180)
        diff=ImageChops.difference(image,self.previous).getbbox() if self.previous else (0,0,WIDTH,HEIGHT)
        if diff is None:return
        left,top,right,bottom=diff
        crop=image.crop(diff);raw=crop.tobytes();count=(right-left)*(bottom-top)
        self.pack(raw,self.output,count);packed=self.output.raw[:count*2];row=(right-left)*2
        for y in range(bottom-top):
            start=(top+y)*960+left*2
            self.buffer[start:start+row]=packed[y*row:(y+1)*row]
        self.previous=image
    def close(self):self.buffer.close();os.close(self.fd)

def touch_point(x,y,rx,ry,config):
    def normalize(v,r):return max(0,min(1,(v-r.min)/(r.max-r.min)))
    u,v=normalize(x,rx),normalize(y,ry)
    if config.get('swap_axes'):u,v=v,u
    if config.get('invert_x'):u=1-u
    if config.get('invert_y'):v=1-v
    transform=config.get('touch_transform')
    if transform:
        a,b,c,d,e,f=transform
        tx,ty=round(a*x+b*y+c),round(d*x+e*y+f)
        tx,ty=max(0,min(WIDTH-1,tx)),max(0,min(HEIGHT-1,ty))
    else:
        tx,ty=round(u*(WIDTH-1)),round(v*(HEIGHT-1))
    if config.get('rotation',0)==180:tx,ty=WIDTH-1-tx,HEIGHT-1-ty
    return tx,ty

def run(config):
    from evdev import InputDevice, ecodes
    restore_calibration(config)
    model=Model();renderer=Renderer();fb=Framebuffer(config['framebuffer'],config['packer'],config.get('rotation',0))
    touch=InputDevice(config['touch']);rx=touch.absinfo(ecodes.ABS_X);ry=touch.absinfo(ecodes.ABS_Y)
    if rx is None or ry is None or rx.max<=rx.min or ry.max<=ry.min:raise ValueError('Invalid touchscreen range')
    thread=threading.Thread(target=model.poll,args=(config['url'],),daemon=True);thread.start()
    for sig in (signal.SIGTERM,signal.SIGINT):signal.signal(sig,lambda *_:model.stopped.set())
    page,selected='overview',0;cal=None
    x,y=rx.min,ry.min;pressed=False;points=[];press_at=0;last_render=0;last_key=None
    try:
        while not model.stopped.is_set():
            ready,_,_=select.select([touch.fd],[],[],0.08 if cal else 0.2)
            if ready:
                for event in touch.read():
                    now=time.monotonic()
                    if event.type==ecodes.EV_ABS:
                        if event.code==ecodes.ABS_X:x=event.value
                        if event.code==ecodes.ABS_Y:y=event.value
                    elif event.type==ecodes.EV_KEY and event.code==ecodes.BTN_TOUCH:
                        if event.value==1:
                            pressed=True;points=[];press_at=now
                        elif pressed:
                            pressed=False
                            if not points:continue
                            raw=points[-1];tx,ty=touch_point(*raw,rx,ry,config)
                            duration=now-press_at
                            if cal:
                                if duration>=2 or (cal.phase=='collect' and inside((tx,ty),COLLECT_CANCEL_BUTTON)):cal.cancel()
                                elif cal.phase=='confirm':
                                    if inside((tx,ty),START_BUTTON):cal.start(now)
                                    elif inside((tx,ty),CONFIRM_CANCEL_BUTTON):cal.cancel()
                                elif cal.phase=='collect':cal.sample(points,duration,now)
                                elif cal.phase in ('review','failed'):
                                    if any(inside((tx,ty),box) for box in (APPLY_BUTTON,RETRY_BUTTON,CANCEL_BUTTON)):
                                        if inside((tx,ty),APPLY_BUTTON) and cal.phase=='review':
                                            try:
                                                cal.apply(config);config['calibration_error']=cal.error
                                            except (OSError,ValueError):
                                                cal.phase='failed';cal.message='Could not save; previous mapping retained.';cal.version+=1
                                        elif inside((tx,ty),RETRY_BUTTON):cal.start(now)
                                        elif inside((tx,ty),CANCEL_BUTTON):cal.cancel()
                                    elif cal.phase=='review':cal.preview(raw)
                                if cal.phase in ('cancelled','applied'):cal=None;page='settings'
                            elif ty>=276:page=('overview','node','events','settings')[min(3,tx//120)]
                            elif page=='settings' and inside((tx,ty),CALIBRATE_BUTTON):cal=Calibration(config.get('rotation',0),now)
                            elif page=='overview' and 74<=ty<=216:selected=min(2,max(0,(tx-14)//155));page='node'
                            elif page=='node' and 45<=ty<=78:selected=min(2,max(0,(tx-14)//155))
                            last_render=0
                    elif event.type==ecodes.EV_SYN and event.code==ecodes.SYN_REPORT and pressed:
                        if len(points)<256:points.append((x,y))
            now=time.monotonic()
            if cal and cal.tick(now):cal=None;page='settings';last_render=0
            # Emergency escape does not depend on the old or new coordinate mapping.
            if cal and pressed and now-press_at>=2:
                cal=None;page='settings';pressed=False;last_render=0
            if now-last_render>=(0.08 if cal else 1):
                value,received,ok=model.snapshot()
                live,elapsed=freshness(value,received,now)
                online=tuple(node_live(n,live,elapsed) for n in value['nodes']) if value else ()
                key=('cal',id(cal),cal.version) if cal else ('settings',config.get('calibration_error')) if page=='settings' else (id(value),live,ok,online,page,selected)
                if key!=last_key:
                    image=renderer.calibration(cal) if cal else renderer.settings(config) if page=='settings' else renderer.draw(value,received,ok,page,selected,now)
                    fb.show(image);last_key=key
                last_render=now
    finally:
        model.stopped.set();touch.close();fb.close()

def load(path):
    config=json.loads(Path(path).read_text());url=urlparse(config['url'])
    if url.scheme!='http' or url.path!='/api/cluster/panel' or url.query or url.fragment or url.username or url.password or not ipaddress.ip_address(url.hostname).is_private:
        raise ValueError('Use the private cluster panel GET endpoint')
    if config.get('rotation',0) not in (0,180):raise ValueError('Supported panel rotation: 0 or 180')
    transform=config.get('touch_transform')
    if transform is not None and (not isinstance(transform,list) or len(transform)!=6 or any(not finite(v) or abs(v)>1e6 for v in transform)):
        raise ValueError('Invalid touch affine transform')
    return config

if __name__=='__main__':
    parser=argparse.ArgumentParser();parser.add_argument('--config',default='/etc/desk-cluster-panel/config.json')
    args=parser.parse_args();run(load(args.config))
