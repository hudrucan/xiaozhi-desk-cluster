#!/usr/bin/python3
import importlib.util,json,select,time
from pathlib import Path
from PIL import Image,ImageDraw
from evdev import InputDevice,ecodes
spec=importlib.util.spec_from_file_location('panel','/usr/local/libexec/desk-cluster-panel/panel.py');p=importlib.util.module_from_spec(spec);spec.loader.exec_module(p)
config=p.load('/etc/desk-cluster-panel/config.json');fb=p.Framebuffer(config['framebuffer'],config['packer'],config.get('rotation',0));renderer=p.Renderer()
touch=InputDevice(config['touch']);rx=touch.absinfo(ecodes.ABS_X);ry=touch.absinfo(ecodes.ABS_Y)
targets=[(40,55),(440,55),(40,265),(440,265)]
samples=[];x=y=0;pressed=False;observed=False;pending=None
try:
 for i,target in enumerate(targets):
  im=Image.new('RGB',(480,320),p.BG);draw=ImageDraw.Draw(im)
  renderer.text(draw,(90,125),'Touch calibration',18,p.TEXT,True)
  renderer.text(draw,(90,160),f'Tap the gold target · {i+1}/4',14,p.GOLD)
  renderer.text(draw,(90,189),'Press and release once',12,p.MUTED)
  tx,ty=target;draw.ellipse((tx-14,ty-14,tx+14,ty+14),outline=p.GOLD,width=2);draw.line((tx-20,ty,tx+20,ty),fill=p.GOLD,width=2);draw.line((tx,ty-20,tx,ty+20),fill=p.GOLD,width=2)
  fb.show(im);deadline=time.monotonic()+90;points=[];done=False
  while time.monotonic()<deadline and not done:
   ready,_,_=select.select([touch.fd],[],[],0.5)
   if not ready:continue
   for e in touch.read():
    if e.type==ecodes.EV_ABS:
     if e.code==ecodes.ABS_X:x=e.value
     if e.code==ecodes.ABS_Y:y=e.value
    if e.type==ecodes.EV_KEY and e.code==ecodes.BTN_TOUCH:
     if e.value==1:pressed=True;points=[]
     elif pressed:
      pressed=False
      if points:
       points.sort();mid=points[len(points)//2];samples.append(mid);done=True;print(f"Captured target {i+1}/4",flush=True)
    if e.type==ecodes.EV_SYN and e.code==ecodes.SYN_REPORT and pressed:points.append((x,y))
   if done:break
  if not done:raise RuntimeError('Calibration timed out; previous configuration retained')
  time.sleep(0.25)
 # Use three non-collinear points for an affine transform; fourth is an independent check.
 def solve(values):
  rows=[[float(x),float(y),1.0,float(v)] for (x,y),v in zip(samples[:3],values)]
  for c in range(3):
   pivot=max(range(c,3),key=lambda r:abs(rows[r][c]));rows[c],rows[pivot]=rows[pivot],rows[c]
   if abs(rows[c][c])<1e-7:raise ValueError('Touch samples overlap; calibration rejected')
   div=rows[c][c];rows[c]=[v/div for v in rows[c]]
   for r in range(3):
    if r!=c:
     factor=rows[r][c];rows[r]=[a-factor*b for a,b in zip(rows[r],rows[c])]
  return [r[3] for r in rows]
 physical=[(479-x,319-y) if config.get('rotation',0)==180 else (x,y) for x,y in targets]
 a=solve([t[0] for t in physical[:3]]);b=solve([t[1] for t in physical[:3]])
 x,y=samples[3];predicted=(a[0]*x+a[1]*y+a[2],b[0]*x+b[1]*y+b[2])
 error=((predicted[0]-physical[3][0])**2+(predicted[1]-physical[3][1])**2)**0.5
 if error>30:raise ValueError('Touch calibration inconsistent; previous configuration retained')
 result={'transform':a+b,'check_error_pixels':round(error,2)}
 Path('/tmp/desk-panel-touch-calibration.json').write_text(json.dumps(result))
 print(json.dumps(result),flush=True)
 im=Image.new('RGB',(480,320),p.BG);draw=ImageDraw.Draw(im);renderer.text(draw,(95,130),'Calibration captured',18,p.GREEN,True);renderer.text(draw,(95,164),'Applying touch mapping…',14,p.MUTED);fb.show(im)
finally:touch.close();fb.close()
