"""Disposable RAM-only LRU. No config, eMMC writes or replication."""
import collections,time
class Cache:
 def __init__(self,limit=16*1024*1024,entries=512,ttl=3600):self.limit=limit;self.entries=entries;self.ttl=ttl;self.data=collections.OrderedDict();self.bytes=0;self.evictions=0
 def prune(self,now=None):
  now=time.monotonic() if now is None else now
  for key,(until,data,kind) in list(self.data.items()):
   if until<=now:self.bytes-=len(data);del self.data[key];self.evictions+=1
 def get(self,key):
  self.prune();value=self.data.get(key)
  if value:self.data.move_to_end(key);return value[1:]
 def put(self,key,data,kind):
  self.prune()
  if not isinstance(data,bytes) or len(data)>self.limit:return
  old=self.data.pop(key,None)
  if old:self.bytes-=len(old[1])
  self.data[key]=(time.monotonic()+(20 if kind=='placeholder' else self.ttl),data,kind);self.bytes+=len(data)
  while self.bytes>self.limit or len(self.data)>self.entries:
   _,(_,data,_)=self.data.popitem(last=False);self.bytes-=len(data);self.evictions+=1
 def clear_placeholders(self,entry):
  for key,(_,data,kind) in list(self.data.items()):
   if isinstance(key,tuple) and key[0]==entry and kind=='placeholder':self.bytes-=len(data);del self.data[key]
 def stats(self):
  self.prune();return {'entries':len(self.data),'bytes':self.bytes,'limit_bytes':self.limit,'max_entries':self.entries,'ttl_seconds':self.ttl,'evictions':self.evictions,'storage':'Core RAM; discarded on restart/failover'}
