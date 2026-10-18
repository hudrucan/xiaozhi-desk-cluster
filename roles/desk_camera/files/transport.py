"""Shared, bounded camera RPC transport; lease grants remain per workload."""
import asyncio,secrets,time,sys
sys.path.insert(0,'/usr/local/libexec/desk-homeassistant')
import nats
from lease import clock
from protocol import pack,unpack,MAX

class Rejected(ValueError):pass

class SharedRPC:
 def __init__(self,agents):
  self.agents=agents;self.node=agents[0].node;self.c=agents[0].c;self.bus=None;self.pending={};self.subscriptions=[]
  self.sent=0;self.reply_subscriptions=0;self.flushes=0;self.metadata_pending=set()
 async def start(self):
  self.bus=await nats.connect(servers=self.c['servers'],user=self.c['nats']['user'],password=self.c['nats']['password'],name='desk-camera-'+self.node,max_reconnect_attempts=-1,reconnect_time_wait=1,pending_size=131072)
  for agent in self.agents:
   self.subscriptions.append(await self.bus.subscribe(agent.prefix+'.control.'+self.node,cb=agent.received,pending_msgs_limit=32,pending_bytes_limit=131072))
   async def reply(msg,prefix=agent.prefix):await self.received(msg,prefix)
   self.subscriptions.append(await self.bus.subscribe(agent.prefix+'.reply.'+self.node+'.*',cb=reply,pending_msgs_limit=64,pending_bytes_limit=262144));self.reply_subscriptions+=1
  await self.bus.flush(timeout=1);self.flushes+=1
 async def received(self,msg,prefix):
  try:
   v=unpack(msg.data,self.c['key']);entry=self.pending.get(v['id'])
   if not entry:return
   node,expected,future=entry
   if future.done() or expected!=prefix or v['sender']!=node or v['recipient']!=self.node or msg.subject!=prefix+'.reply.'+self.node+'.'+v['id']:return
   if v.get('ok') is False:future.set_exception(Rejected('Camera RPC rejected'))
   elif v.get('ok') is True and isinstance(v.get('value'),dict):future.set_result(v['value'])
  except Exception:return
 async def call(self,agent,node,action,value):
  if action in ('recording_v1','observation_v1','command_v1'):
   if len(self.metadata_pending)>=4:raise ValueError('Recording RPC capacity reached')
   slot=object();self.metadata_pending.add(slot)
   try:
    if node==self.node:
     from media_rpc import request
     return await request({'action':action,'id':value.get('request_id',secrets.token_hex(16)),'sender':self.node,'request':value},timeout=1)
    return await self.remote(agent,node,action,value)
   finally:self.metadata_pending.discard(slot)
  if node==self.node:return agent.rpc(action,value,node)
  return await self.remote(agent,node,action,value)
 async def remote(self,agent,node,action,value):
  if len(self.pending)>=32:raise ValueError('Camera RPC capacity reached')
  ident=secrets.token_hex(16);future=asyncio.get_running_loop().create_future();inbox=agent.prefix+'.reply.'+self.node+'.'+ident
  self.pending[ident]=(node,agent.prefix,future)
  try:
   await self.bus.publish(agent.prefix+'.control.'+node,pack({'sender':self.node,'recipient':node,'id':ident,'at':time.time(),'action':action,'value':value},self.c['key']),reply=inbox);self.sent+=1
   return await asyncio.wait_for(future,1.5)
  finally:self.pending.pop(ident,None)
 async def close(self):
  for _,_,future in list(self.pending.values()):
   if not future.done():future.cancel()
  if self.bus:await self.bus.close()
 def public(self):
  return {'connected':bool(self.bus and self.bus.is_connected),'reply_subscriptions':self.reply_subscriptions,'startup_flushes':self.flushes,'requests_sent':self.sent,'pending_requests':len(self.pending)}

class FleetStatus:
 """Share status collection for at most one second; never cache lease grants."""
 def __init__(self,agents,transport):
  self.agents=agents;self.transport=transport;self.root=agents[0];self.workloads={a.c['workload'] for a in agents};self.lock=asyncio.Lock();self.cached=None;self.at=0;self.batches=0;self.fallbacks=0
 def local(self):return {a.c['workload']:a.status() for a in self.agents}
 async def node(self,node):
  try:
   result=await self.transport.call(self.root,node,'fleet_status',{})
   if set(result)!=self.workloads or not all(isinstance(v,dict) for v in result.values()):raise ValueError('Invalid fleet status')
   return result
  except Rejected:
   # Rolling updates can still talk to the previous single-workload RPC handlers.
   self.fallbacks+=1
   result=await asyncio.gather(*(self.transport.call(a,node,'status',{}) for a in self.agents),return_exceptions=True)
   return {a.c['workload']:v for a,v in zip(self.agents,result) if isinstance(v,dict)}
 async def heads(self,agent):
  async with self.lock:
   if self.cached is None or clock()-self.at>=1:
    nodes=list(agent.members);values=await asyncio.gather(*(self.node(n) for n in nodes),return_exceptions=True)
    self.cached={n:v for n,v in zip(nodes,values) if isinstance(v,dict)};self.at=clock();self.batches+=1
   heads={n:v[agent.c['workload']] for n,v in self.cached.items() if agent.c['workload'] in v}
  # Local eligibility and occupied slots must reflect changes since the last batch.
  heads[agent.node]=agent.status()
  return heads
 def public(self):return dict(self.transport.public(),status_batches=self.batches,legacy_fallbacks=self.fallbacks)
