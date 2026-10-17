"""Warm playback admission, cancellation and independent fencing; no camera I/O."""
import asyncio,json,pathlib,sys,tempfile,time,types,unittest
from unittest.mock import AsyncMock,Mock,patch
BASE=pathlib.Path(__file__).resolve().parents[1]/'files';sys.path.insert(0,str(BASE))
import warm_playback as warm,playback,media_rpc
class WarmTests(unittest.IsolatedAsyncioTestCase):
 async def asyncSetUp(self):
  self.tmp=tempfile.TemporaryDirectory();self.root=pathlib.Path(self.tmp.name);self.patch=patch.object(warm,'ROOT',self.root);self.patch.start()
  self.worker=types.SimpleNamespace(current=None,client=AsyncMock(return_value=object()),control=AsyncMock(),cancel=AsyncMock(),clients={},close=Mock())
  self.allowed=Mock(return_value=True);self.m=warm.Media(self.worker,self.allowed)
  self.clip={'date':'20261017','start':1792190000,'end':1792190122};self.job=playback.validate('tapo_c222_163',self.clip);self.ident=playback.identity(self.job);self.path=self.root/self.job['workload']/self.ident;self.path.mkdir(parents=True);(self.path/'options.json').write_text(json.dumps({'format':'hls','generation':'f'*32}))
  self.v={'action':'playback_start','id':self.ident,'attempt':'a'*32,'workload':self.job['workload'],'generation':'b'*32,'clip':self.clip,'session':{'user_id':12,'player_id':'native'}}
  self.execute=AsyncMock();self.exec_patch=patch.object(warm,'execute',self.execute);self.exec_patch.start()
 async def asyncTearDown(self):
  await self.m.stop();self.exec_patch.stop();self.patch.stop();self.tmp.cleanup()
 def message(self,action,attempt='a'):return {'action':action,'id':self.ident,'attempt':attempt*32}
 async def test_hls_uses_warm_client_and_supplied_native_handle_without_new_container(self):
  def result(tapo,job,session,path,options,save,control,permitted):
   self.assertEqual(session,self.v['session']);self.assertEqual(options['format'],'hls');save({'state':'ready','format':'hls'})
  self.execute.side_effect=result
  self.assertEqual(await self.m.action(self.v),{'state':'active'});await self.m.current['task']
  self.execute.assert_awaited_once();self.assertEqual(self.m.completed,1);self.assertEqual(json.loads((self.path/'progress.json').read_bytes())['runtime'],'warm')
 async def test_playback_waits_for_thumbnail_cleanup_before_using_shared_client(self):
  entered=asyncio.Event();finish=asyncio.Event();self.worker.current=('c'*32,object())
  async def cancel(ident):entered.set();await finish.wait();self.worker.current=None
  self.worker.cancel.side_effect=cancel
  await self.m.action(self.v);await entered.wait();self.worker.client.assert_not_awaited();finish.set();await self.m.current['task'];self.worker.client.assert_awaited_once()
 async def test_cancel_waits_for_cleanup_and_old_attempt_cannot_kill_new_job(self):
  entered=asyncio.Event();cleaning=asyncio.Event();finish=asyncio.Event()
  async def execute(*args):
   try:entered.set();await asyncio.Event().wait()
   finally:cleaning.set();await finish.wait()
  self.execute.side_effect=execute;await self.m.action(self.v);await entered.wait()
  self.assertEqual(await self.m.action(self.message('playback_cancel','d')),{'state':'cancelled'});self.assertTrue(self.m.busy())
  closing=asyncio.create_task(self.m.action(self.message('playback_cancel')));await cleaning.wait();self.assertFalse(closing.done())
  finish.set();await closing;self.assertFalse(self.m.busy());self.assertEqual(self.m.cancelled,1)
 async def test_owner_loss_independently_closes_active_playback(self):
  entered=asyncio.Event();closed=asyncio.Event()
  async def execute(*args):
   try:entered.set();await asyncio.Event().wait()
   finally:closed.set()
  self.execute.side_effect=execute;await self.m.action(self.v);await entered.wait();self.allowed.return_value=False
  await asyncio.wait_for(closed.wait(),1);await asyncio.gather(self.m.current['task'],return_exceptions=True);self.assertEqual(self.m.cancelled,1)
 async def test_lost_controller_lease_is_not_resurrected_by_late_tick(self):
  entered=asyncio.Event()
  async def execute(*args):entered.set();await asyncio.Event().wait()
  self.execute.side_effect=execute;await self.m.action(self.v);await entered.wait();self.m.current['until']=time.monotonic()-1
  self.assertEqual(await self.m.action(self.message('playback_tick')),{'state':'missing'})
  await asyncio.sleep(.3);self.assertFalse(self.m.busy())
 async def test_cancel_before_delayed_start_is_tombstoned(self):
  await self.m.action(self.message('playback_cancel'))
  with self.assertRaises(ValueError):await self.m.action(self.v)
  self.worker.client.assert_not_awaited()
 async def test_forged_identity_and_stale_owner_are_rejected_before_camera_access(self):
  with self.assertRaises(ValueError):await self.m.action(dict(self.v,id='e'*32))
  self.allowed.return_value=False
  with self.assertRaises(ValueError):await self.m.action(self.v)
  self.worker.client.assert_not_awaited()
 async def test_one_node_cannot_run_two_competing_playback_jobs(self):
  entered=asyncio.Event()
  async def execute(*args):entered.set();await asyncio.Event().wait()
  self.execute.side_effect=execute;await self.m.action(self.v);await entered.wait()
  with self.assertRaises(ValueError):await self.m.action(dict(self.v,attempt='c'*32))
class ClientReuseTests(unittest.IsolatedAsyncioTestCase):
 async def test_preloading_does_not_log_in_and_same_generation_reuses_authenticated_client(self):
  import thumbnail_worker
  sdk=types.ModuleType('pytapo');created=[]
  def make(*args,**kwargs):
   value=types.SimpleNamespace(close=Mock());created.append(value);return value
  sdk.Tapo=make
  with patch.dict(sys.modules,{'pytapo':sdk}),patch.object(pathlib.Path,'read_bytes',return_value=json.dumps({'tapo_c222_163':{'cloud_password':'fake','ip_address':'192.0.2.1'}}).encode()):
   worker=thumbnail_worker.Worker();self.assertEqual(created,[])
   v={'workload':'tapo_c222_163','generation':'a'*32}
   first=await worker.client(v);self.assertIs(first,await worker.client(v));self.assertEqual(len(created),1)
   second=await worker.client(dict(v,generation='b'*32));self.assertIsNot(first,second);first.close.assert_called_once()
class ControllerTests(unittest.IsolatedAsyncioTestCase):
 async def test_warm_path_submits_metadata_without_container_or_persisted_handle(self):
  with tempfile.TemporaryDirectory() as tmp:
   root=pathlib.Path(tmp);agent=types.SimpleNamespace(c={'workload':'tapo_c222_163','progressive':True},generation='b'*32,owned=lambda:True,node='deskb1x')
   p=playback.Playback(agent);p.current='a'*32;p.attempt='c'*32;job=playback.validate(agent.c['workload'],{'date':'20261017','start':1792190000,'end':1792190122})
   async def rpc(v,**kwargs):
    if v['action']=='playback_start':self.assertEqual(v['session']['user_id'],12);return {'state':'active'}
    if v['action']=='playback_tick':
     path=playback.clip_path(job['workload'],'a'*32);path.write_bytes(b'x'*2048);return {'state':'finished'}
    raise AssertionError(v['action'])
   with patch.object(playback,'ROOT',root),patch.object(media_rpc,'request',side_effect=rpc) as request,patch.object(asyncio,'create_subprocess_exec') as subprocess:
    await p.run(job,'a'*32,agent.generation,{'user_id':12,'player_id':'native'})
    subprocess.assert_not_called();self.assertEqual(request.call_args_list[0].args[0]['action'],'playback_start');self.assertFalse(list(root.glob('**/playback-session.json')))
if __name__=='__main__':unittest.main()
