import asyncio,importlib.util,json,pathlib,sys,tempfile,time,unittest
from unittest.mock import patch,AsyncMock
FILES=pathlib.Path(__file__).resolve().parents[1]/'files'
sys.path.insert(0,str(FILES))
import scan_protocol as p
from scan_service import Service
import mdns
import install_nats_scan
NODE='deskb2x';OWNER='deskb1x';IP='192.168.1.115';MAC='02:11:22:33:44:55'
def job():return {'id':'a'*32,'owner':OWNER,'term':[7,OWNER],'worker':NODE,'plugin':'mdns','deadline':time.time()+8,'targets':{IP:MAC}}
def config():return {'node':OWNER,'key':'test-key','members':{n:'10.10.10.'+str(i+11) for i,n in enumerate(p.NODES)}}
class Protocol(unittest.TestCase):
 def test_signature_limits_deadlines_and_allowlist(self):
  v=job();self.assertEqual(p.unpack(p.pack(v,'key'),'key'),v);p.validate_job(v,NODE)
  for raw in (p.pack(v,'wrong'),b'x'*40000,b'{}'):
   with self.assertRaises((ValueError,TypeError)):p.unpack(raw,'key')
  for k,value in [('worker',OWNER),('plugin','shell'),('deadline',time.time()-1),('deadline',time.time()+20),('term',[6,'other']),('targets',{'10.77.0.2':MAC}),('targets',{IP:'ff:ff:ff:ff:ff:ff'})]:
   bad=dict(v);bad[k]=value
   with self.assertRaises((ValueError,TypeError)):p.validate_job(bad,NODE)
 def test_results_reject_old_owner_duplicate_attempt_identity_invalid_names(self):
  j=job();v={'id':j['id'],'term':j['term'],'worker':NODE,'state':'ready','found':{IP:['zerow.local',120]},'stats':{'sent':4,'received':2}}
  self.assertEqual(p.validate_result(v,j)[0][IP],('zerow.local',120))
  for k,value in [('id','b'*32),('term',[6,OWNER]),('worker',OWNER),('state','unavailable'),('found',{'192.168.1.28':['wrong.local',120]}),('found',{IP:['bad|host.local',120]}),('stats',{'sent':9999,'received':0})]:
   bad=dict(v);bad[k]=value
   with self.assertRaises((ValueError,p.Unavailable)):p.validate_result(bad,j)
 def test_nats_user_insertion_preserves_original_auth_and_is_idempotent(self):
  secret={'coordinators':{OWNER:{'user':'owner','password':'fake'}},'workers':{NODE:{'user':'worker','password':'fake'}}}
  text='authorization {\n  users: [\n    {user: original,password: unchanged}\n  ]\n}\ncluster {name: original}\n'
  new=install_nats_scan.candidate(text,secret);self.assertEqual(install_nats_scan.candidate(new,secret),new)
  self.assertIn('user: original,password: unchanged',new);self.assertIn('cluster {name: original}',new)
  users=install_nats_scan.users(secret);self.assertEqual(users[1]['permissions']['subscribe']['allow'],['desk.network.scan.'+NODE,'desk.network.status.'+NODE])
  self.assertNotIn('publish',users[1]['permissions']);self.assertEqual(users[1]['permissions']['allow_responses']['max'],1)
 def test_distributed_failure_does_not_negative_cache_or_fallback_local(self):
  import test_mdns
  with tempfile.TemporaryDirectory() as td,patch.object(mdns,'interface_address',return_value='192.168.1.101'):
   root=pathlib.Path(td);db=test_mdns.MDNS().fixture(root);cache=root/'cache'
   def unavailable(*args):raise OSError('No workers')
   with self.assertRaises(OSError):mdns.run(db,cache,unavailable)
   self.assertFalse(cache.exists())
class Dispatch(unittest.IsolatedAsyncioTestCase):
 async def test_readiness_requires_live_transport_without_dispatching(self):
  import types
  for connected in (True,False):
   s=Service(config());s.bus=types.SimpleNamespace(is_connected=True);s.worker_bus=types.SimpleNamespace(is_connected=connected);s.dispatch=AsyncMock()
   reader=asyncio.StreamReader();reader.feed_data(b'{"health":true}\n');reader.feed_eof()
   class Writer:
    raw=b''
    def write(self,data):self.raw+=data
    async def drain(self):pass
    def close(self):pass
    async def wait_closed(self):pass
   w=Writer();await s.local(reader,w)
   self.assertEqual(json.loads(w.raw)['state'],'ready' if connected else 'unavailable');s.dispatch.assert_not_awaited()
 async def test_owner_selects_node_with_score_not_queue_group(self):
  s=Service(config());s.owner=AsyncMock(return_value=[7,OWNER]);s.worker_status=AsyncMock(side_effect=[(50,OWNER),(10,NODE),(30,'deskb3x')])
  seen=[]
  async def request(node,j,kind,timeout):
   seen.append((node,kind));return {'id':j['id'],'term':j['term'],'worker':node,'state':'ready','found':{IP:['zerow.local',120]},'stats':{'sent':4,'received':1}}
  s.request=request
  with patch('scan_service.save_json'):r=await s.dispatch({IP:MAC})
  self.assertEqual(seen,[(NODE,'scan')]);self.assertEqual(r['stats']['worker'],NODE)
 async def test_worker_failure_retries_different_node_only_after_deadline(self):
  s=Service(config());s.owner=AsyncMock(return_value=[7,OWNER]);s.worker_status=AsyncMock(side_effect=[(50,OWNER),(10,NODE),(30,'deskb3x')]);seen=[]
  async def request(node,j,kind,timeout):
   seen.append(node)
   return {'id':j['id'],'term':j['term'],'worker':node,'state':'ready' if node=='deskb3x' else 'unavailable','found':{},'stats':{'sent':0,'received':0}}
  s.request=request
  with patch('scan_service.save_json'),patch('scan_service.asyncio.sleep',new_callable=AsyncMock) as sleep:r=await s.dispatch({IP:MAC})
  self.assertEqual(seen,[NODE,'deskb3x']);self.assertTrue(sleep.await_args.args[0]>0)
 async def test_owner_loss_prevents_dispatch_or_result_acceptance(self):
  s=Service(config());s.owner=AsyncMock(side_effect=p.Unavailable('no owner'));s.worker_status=AsyncMock()
  with self.assertRaises(p.Unavailable):await s.dispatch({IP:MAC})
  s.worker_status.assert_not_awaited()
  s.owner=AsyncMock(side_effect=[[7,OWNER],[7,OWNER],p.Unavailable('old term'),p.Unavailable('old term')]);s.worker_status=AsyncMock(side_effect=[(50,OWNER),(10,NODE),(30,'deskb3x')]);s.request=AsyncMock(return_value={})
  with patch('scan_service.asyncio.sleep',new_callable=AsyncMock),self.assertRaises(p.Unavailable):await s.dispatch({IP:MAC})
 async def test_worker_dedup_busy_and_maintenance_never_start_lookup(self):
  c=config();c['node']=NODE;s=Service(c);s.owner=AsyncMock(return_value=[7,OWNER]);s.telemetry=AsyncMock(side_effect=p.Unavailable('maintenance'))
  class Msg:
   reply='desk.network.reply.'+OWNER+'.test'
   data=p.pack(job(),'test-key')
   respond=AsyncMock()
  with patch('scan_service.resolve') as resolve:
   await s.execute(Msg());await s.execute(Msg());resolve.assert_not_called()
  self.assertFalse(s.busy);self.assertIn('a'*32,s.seen)
 async def test_telemetry_excludes_stale_missing_maintenance_and_voice(self):
  import datetime
  s=Service(config());base={'id':NODE,'online':True,'observed_at':datetime.datetime.now(datetime.timezone.utc).isoformat(),'cpu':20,'admission':{'worker':False,'mqtt':False,'core':False},'activity':{'asr':0,'llm':0,'tts':0}}
  for change in ({'observed_at':'2000-01-01T00:00:00Z'},{'admission':{'worker':True}},{'activity':{'tts':1}},{'cpu':float('nan')},{'online':False}):
   s.fetch=lambda url:dict(base,**change)
   with self.assertRaises(p.Unavailable):await s.telemetry(NODE)
  s.fetch=lambda url:base;self.assertEqual((await s.telemetry(NODE))['cpu'],20)
if __name__=='__main__':unittest.main()
