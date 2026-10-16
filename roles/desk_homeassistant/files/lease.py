"""Existing Network lease primitive, unchanged; no new election algorithm."""
import json,pathlib,threading,time
from config_journal import atomic,encoded
TTL=15.
def clock():return time.clock_gettime(time.CLOCK_BOOTTIME) if hasattr(time,'CLOCK_BOOTTIME') else time.monotonic()
class Unavailable(Exception):pass
class Lease:
 """Boot quarantine preserves grants lost from RAM across a voter restart."""
 def __init__(self,path,clock=clock):
  self.path=pathlib.Path(path);self.clock=clock;self.lock=threading.RLock()
  self.term=json.loads(self.path.read_text()) if self.path.exists() else [0,'']
  self.quarantine=clock()+TTL+2;self.until=self.quarantine;self.holder=None
 def grant(self,term,node):
  with self.lock:
   now=self.clock()
   if len(term)!=2 or not isinstance(term[0],int) or term[0]<1 or term[1]!=node:raise Unavailable('Invalid lease term')
   if now<self.until and (self.holder!=node or term!=self.term):raise Unavailable('Lease held or voter warming up')
   if term<self.term:raise Unavailable('Term superseded')
   if term!=self.term:atomic(self.path,encoded(term));self.term=term
   self.holder=node;self.until=now+TTL
   return {'term':term,'holder':node,'ttl':TTL}
 def status(self):
  with self.lock:return {'promised':self.term,'holder':self.holder if self.until>self.clock() else None,'remaining':max(0,self.until-self.clock())}
