"""Cheap cached camera reachability and pressure scoring; no authentication or stream."""
import math,socket,time,urllib.parse

class CameraHealth:
 def __init__(self):self.checked=0;self.reachable=None;self.failures=0;self.cpu=None
 def refresh(self,config,local,now=None):
  now=time.monotonic() if now is None else now
  sample=local.get('cpu',100) if local else 100
  if type(sample) in (int,float) and math.isfinite(sample):self.cpu=sample if self.cpu is None else .75*self.cpu+.25*sample
  if now-self.checked<15 and self.reachable is not None:return
  self.checked=now
  host=urllib.parse.urlsplit(next(iter(config['streams'].values()))).hostname
  try:
   with socket.create_connection((host,554),timeout=.4):pass
   self.failures=0;self.reachable=True
  except OSError:
   self.failures+=1
   if self.reachable is None or self.failures>=2:self.reachable=False
 def penalty(self,local):
  memory=local.get('memory_available_mib',0) if local else 0
  return round((self.cpu if self.cpu is not None else 100)+max(0,1024-memory)/32,2)
