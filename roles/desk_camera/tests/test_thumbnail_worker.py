import asyncio,contextlib,hashlib,importlib.util,io,json,pathlib,sys,tempfile,types,unittest
from unittest.mock import AsyncMock,Mock,patch
from PIL import Image
BASE=pathlib.Path(__file__).resolve().parents[1]/'files';sys.path.insert(0,str(BASE));import thumbnail_worker as worker,thumbnail_fetch as fetch
b=io.BytesIO();Image.new('RGB',(32,18)).save(b,format='JPEG');JPEG=b.getvalue()
sdk=types.ModuleType('pytapo.media_stream.snapshot');snapshot=Mock();sdk.getRecordingSnapshots=snapshot
sys.modules.setdefault('pytapo',types.ModuleType('pytapo'));sys.modules.setdefault('pytapo.media_stream',types.ModuleType('pytapo.media_stream'));sys.modules['pytapo.media_stream.snapshot']=sdk
class RuntimeTests(unittest.IsolatedAsyncioTestCase):
 async def asyncSetUp(self):
  self.w=worker.Worker();self.client=types.SimpleNamespace(executeFunction=Mock(return_value={'playback':{'search_detection_list':[{'start_time':1792112401}]}}));self.w.client=AsyncMock(return_value=self.client);self.closed=False
  self.job={'id':'a'*32,'workload':'tapo_c222_160','generation':'b'*32,'clip':{'date':'20261016','start':1792112400,'end':1792112460},'session':{'user_id':12,'player_id':'native'}}
  self.permission=patch.object(worker,'permitted',return_value=True);self.permission.start();self.addCleanup(self.permission.stop)
  async def pictures(*args,**kwargs):
   try:yield JPEG
   finally:self.closed=True
  snapshot.reset_mock();snapshot.side_effect=pictures
 async def test_one_event_jpeg_reuses_supplied_handle_and_closes_media(self):
  data,kind=await self.w.produce(self.job);self.assertEqual((data,kind),(JPEG,'event'));self.assertEqual(self.client.userID,12);self.assertEqual(self.client.playerID,'native');self.assertTrue(self.closed);self.assertEqual(self.w.completed,1)
 async def test_no_snapshot_falls_back_to_short_preview_and_frame_on_worker(self):
  async def pictures(*args,**kwargs):
   if False:yield JPEG
  snapshot.side_effect=pictures;self.w.preview=AsyncMock(return_value=b'transport');self.w.frame=AsyncMock(return_value=JPEG)
  data,kind=await self.w.produce(self.job);self.assertEqual(kind,'keyframe');self.w.preview.assert_awaited_once_with(self.client,1792112400,1792112460);self.w.frame.assert_awaited_once_with(b'transport')
 async def test_cancel_closes_snapshot_before_ack_and_tombstone_blocks_late_job(self):
  opened=asyncio.Event()
  async def pictures(*args,**kwargs):
   try:opened.set();await asyncio.Event().wait();yield JPEG
   finally:self.closed=True
  snapshot.side_effect=pictures;t=asyncio.create_task(self.w.produce(self.job));self.w.current=(self.job['id'],t);await opened.wait();await self.w.cancel(self.job['id']);self.assertTrue(self.closed);self.assertTrue(t.cancelled());self.assertIsNone(self.w.current);self.assertIn(self.job['id'],self.w.tombstones)
 async def test_owner_loss_cancels_active_media(self):
  task=asyncio.create_task(asyncio.Event().wait())
  with patch.object(worker,'permitted',return_value=False):await self.w.watch(self.job,task)
  await asyncio.gather(task,return_exceptions=True);self.assertTrue(task.cancelled())
 async def test_control_http_finishes_before_cancellation_ack(self):
  import threading
  started=threading.Event();finish=threading.Event();done=threading.Event()
  def control():started.set();finish.wait(2);done.set();return {}
  task=asyncio.create_task(self.w.control(control));await asyncio.to_thread(started.wait,1);task.cancel();await asyncio.sleep(.01);self.assertFalse(task.done());finish.set();await asyncio.gather(task,return_exceptions=True);self.assertTrue(done.is_set())
 async def test_stale_permit_prevents_any_camera_operation(self):
  with patch.object(worker,'permitted',return_value=False):
   with self.assertRaises(ValueError):await self.w.produce(self.job)
  self.w.client.assert_not_called()
class FetchTests(unittest.TestCase):
 def setUp(self):
  self.a=types.SimpleNamespace(node='deskb2x',generation='b'*32,c={'workload':'tapo_c222_160'},owned=lambda:True,ready=lambda:True,playback=types.SimpleNamespace(active=lambda:False));self.f=fetch.Fetcher();self.body=json.dumps({'clip':{'date':'20261016','start':1792112400,'end':1792112460},'session':{'user_id':12,'player_id':'native'}}).encode();self.scope={'workload':'tapo_c222_160','id':'a'*32,'source':'fetch','generation':'b'*32,'digest':hashlib.sha256(self.body).hexdigest()}
 def test_bound_request_forwards_only_to_root_worker(self):
  with patch('thumbnail_decode.memory_ok',return_value=True),patch.object(fetch,'rpc',return_value=({'kind':'event'},JPEG)) as rpc:
   self.assertEqual(self.f.request([self.a],self.scope,self.body),(JPEG,'event'));self.assertEqual(rpc.call_args.args[0]['session']['user_id'],12);self.assertEqual(self.f.last['node'],'deskb2x')
 def test_cancel_ack_waits_for_old_http_handler_admission_to_release(self):
  import threading,time
  self.f.lock.acquire();scope=dict(self.scope,source='cancel',digest=hashlib.sha256(b'').hexdigest());released=threading.Event()
  def release():time.sleep(.03);self.f.lock.release();released.set()
  threading.Thread(target=release).start()
  with patch.object(fetch,'rpc',return_value=({},b'')):self.f.request([self.a],scope,b'')
  self.assertTrue(released.is_set());self.assertFalse(self.f.lock.locked())
 def test_wrong_digest_owner_and_busy_do_not_forward(self):
  with patch.object(fetch,'rpc') as rpc:
   with self.assertRaises(ValueError):self.f.request([self.a],self.scope,b'wrong')
   self.a.generation='c'*32
   with self.assertRaises(ValueError):self.f.request([self.a],self.scope,self.body)
   rpc.assert_not_called()
 def test_cancel_reaches_worker_even_after_lease_loss_and_requires_empty_body(self):
  self.a.owned=lambda:False;s=dict(self.scope,source='cancel',digest=hashlib.sha256(b'').hexdigest())
  with patch.object(fetch,'rpc',return_value=({},b'')) as rpc:
   self.assertEqual(self.f.request([self.a],s,b''),(b'','cancelled'));self.assertEqual(rpc.call_args.args[0]['action'],'cancel')
if __name__=='__main__':unittest.main()
