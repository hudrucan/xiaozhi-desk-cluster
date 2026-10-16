"""Offline RPC correlation, bounded cleanup, batching and rolling compatibility."""
import asyncio,importlib.util,pathlib,sys,time,types,unittest
ROOT=pathlib.Path(__file__).parent
if not (ROOT/'transport.py').exists():ROOT=ROOT.parent/'files'
CORE=ROOT.parent.parent/'desk_homeassistant/files'
if not CORE.is_dir():CORE=pathlib.Path('/usr/local/libexec/desk-homeassistant')
sys.path.insert(0,str(CORE));sys.modules.setdefault('nats',types.SimpleNamespace())
spec=importlib.util.spec_from_file_location('camera_transport',ROOT/'transport.py');m=importlib.util.module_from_spec(spec);spec.loader.exec_module(m)
from protocol import pack,unpack
KEY='offline-test-signing-key';NODES=('deskb1x','deskb2x','deskb3x')
class Agent:
 def __init__(self,i=0):
  self.node=NODES[0];self.members={n:n for n in NODES};self.prefix='desk.ha.media'+('' if i==0 else '.cam'+str(i));self.c={'key':KEY,'workload':'cam'+str(i),'servers':[],'nats':{'user':'offline','password':'offline'}};self.score=0;self.grants=0;self.fleet=None
 async def received(self,msg):pass
 def status(self):return {'score':self.score,'definition':'definition'+self.c['workload']}
 def rpc(self,action,value,sender):
  if action=='status':return self.status()
  if action=='fleet_status':return self.fleet.local()
  if action=='lease':self.grants+=1;return {'grant':self.grants}
  raise ValueError()
class Bus:
 def __init__(self):self.handlers={};self.messages=[];self.flushes=0;self.is_connected=True;self.auto=False
 async def subscribe(self,subject,cb,**kw):self.handlers[subject]=cb;return object()
 async def flush(self,**kw):self.flushes+=1
 async def close(self):self.is_connected=False
 async def publish(self,subject,data,reply):
  self.messages.append((subject,data,reply))
  if self.auto:await self.answer(len(self.messages)-1)
 async def answer(self,i,**override):
  subject,raw,reply=self.messages[i];request=unpack(raw,KEY);body={'sender':request['recipient'],'recipient':request['sender'],'id':request['id'],'at':time.time(),'ok':True,'value':{'action':request['action']}};body.update(override)
  await self.deliver(reply,pack(body,KEY))
 async def deliver(self,subject,data):
  prefix=subject.rsplit('.',1)[0]+'.*';cb=self.handlers.get(prefix)
  if cb:await cb(types.SimpleNamespace(subject=subject,data=data))
class TransportTests(unittest.IsolatedAsyncioTestCase):
 async def asyncSetUp(self):
  self.agents=[Agent(i) for i in range(3)];self.bus=Bus()
  async def connect(**kw):return self.bus
  m.nats.connect=connect;self.rpc=m.SharedRPC(self.agents);await self.rpc.start()
 async def asyncTearDown(self):await self.rpc.close()
 async def test_many_calls_reuse_one_startup_flush_and_reply_subscriptions(self):
  self.bus.auto=True
  for i in range(30):self.assertEqual(await self.rpc.call(self.agents[i%3],NODES[1],'status',{}),{'action':'status'})
  self.assertEqual(len(self.bus.handlers),6);self.assertEqual(self.bus.flushes,1);self.assertEqual(self.rpc.reply_subscriptions,3);self.assertEqual(len(self.rpc.pending),0)
 async def test_reordered_replies_reject_wrong_sender_recipient_signature_and_prefix(self):
  tasks=[asyncio.create_task(self.rpc.call(self.agents[i],NODES[i+1],'status',{})) for i in range(2)];await asyncio.sleep(0)
  await self.bus.answer(0,sender=NODES[2]);await self.bus.answer(0,recipient=NODES[2])
  raw=self.bus.messages[0];await self.bus.deliver(raw[2],b'{"body":{},"signature":"forged"}')
  ident=unpack(raw[1],KEY)['id'];wrong=self.agents[1].prefix+'.reply.'+NODES[0]+'.'+ident
  await self.bus.deliver(wrong,pack({'sender':NODES[1],'recipient':NODES[0],'id':ident,'at':time.time(),'ok':True,'value':{}},KEY))
  self.assertFalse(tasks[0].done());await self.bus.answer(1);await self.bus.answer(0)
  self.assertEqual(await asyncio.gather(*tasks),[{'action':'status'},{'action':'status'}]);self.assertFalse(self.rpc.pending)
  await self.bus.answer(0);self.assertFalse(self.rpc.pending)
 async def test_timeout_and_cancel_remove_pending_requests(self):
  with self.assertRaises(TimeoutError):await self.rpc.call(self.agents[0],NODES[1],'lease',{})
  self.assertFalse(self.rpc.pending)
  task=asyncio.create_task(self.rpc.call(self.agents[1],NODES[2],'lease',{}));await asyncio.sleep(0);task.cancel()
  with self.assertRaises(asyncio.CancelledError):await task
  self.assertFalse(self.rpc.pending);await self.bus.answer(0);self.assertFalse(self.rpc.pending)
 async def test_capacity_is_bounded_and_lease_grants_are_not_cached(self):
  a=self.agents[0]
  self.assertEqual((await self.rpc.call(a,a.node,'lease',{}))['grant'],1)
  self.assertEqual((await self.rpc.call(a,a.node,'lease',{}))['grant'],2)
  self.rpc.pending={str(i):(NODES[1],a.prefix,asyncio.get_running_loop().create_future()) for i in range(32)}
  with self.assertRaises(ValueError):await self.rpc.call(a,NODES[1],'lease',{})
  self.assertEqual(len(self.bus.messages),0)
class StatusTransport:
 def __init__(self,agents):self.agents=agents;self.calls=[];self.old=set();self.bad=set()
 async def call(self,a,node,action,value):
  self.calls.append((a.c['workload'],node,action));await asyncio.sleep(0)
  if node in self.bad:raise TimeoutError()
  if action=='fleet_status':
   if node in self.old:raise m.Rejected()
   return {x.c['workload']:x.status() for x in self.agents}
  return a.status()
 def public(self):return {}
class StatusTests(unittest.IsolatedAsyncioTestCase):
 async def asyncSetUp(self):
  self.agents=[Agent(i) for i in range(3)];self.rpc=StatusTransport(self.agents);self.fleet=m.FleetStatus(self.agents,self.rpc);self.now=[100.];self.original_clock=m.clock;m.clock=lambda:self.now[0]
  for a in self.agents:a.fleet=self.fleet
 async def asyncTearDown(self):m.clock=self.original_clock
 async def test_one_batch_for_three_cameras_local_status_is_always_current(self):
  heads=await asyncio.gather(*(self.fleet.heads(a) for a in self.agents));self.assertEqual(len(self.rpc.calls),3);self.assertEqual(self.fleet.batches,1)
  self.agents[1].score=99;v=await self.fleet.heads(self.agents[1]);self.assertEqual(v[NODES[0]]['score'],99);self.assertEqual(len(self.rpc.calls),3)
  self.now[0]+=1;await self.fleet.heads(self.agents[1]);self.assertEqual(len(self.rpc.calls),6)
 async def test_old_peer_fallback_is_per_workload_and_failed_peer_is_omitted(self):
  self.rpc.old={NODES[1]};self.rpc.bad={NODES[2]}
  v=await self.fleet.heads(self.agents[2]);self.assertEqual(set(v),{NODES[0],NODES[1]});self.assertEqual(self.fleet.fallbacks,1)
  self.assertEqual(len([x for x in self.rpc.calls if x[1]==NODES[1] and x[2]=='status']),3)
  self.assertEqual(v[NODES[1]]['definition'],'definitioncam2')
if __name__=='__main__':unittest.main()
