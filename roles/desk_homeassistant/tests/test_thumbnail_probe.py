import asyncio,importlib.util,io,pathlib,sys,types,unittest
from unittest.mock import AsyncMock,Mock,patch
from PIL import Image
BASE=pathlib.Path(__file__).resolve().parents[1]/'files/desk_cluster/thumbnail_probe.py'
spec=importlib.util.spec_from_file_location('thumbnail_probe',BASE);probe=importlib.util.module_from_spec(spec);spec.loader.exec_module(probe)
state={}
async def images(*args,**kwargs):
 try:
  if state.get('error'):raise state['error']
  yield state.get('image')
 finally:state['closed']=True
snapshot=Mock(side_effect=images);module=types.ModuleType('pytapo.media_stream.snapshot');module.getRecordingSnapshots=snapshot
sys.modules['pytapo']=types.ModuleType('pytapo');sys.modules['pytapo.media_stream']=types.ModuleType('pytapo.media_stream');sys.modules['pytapo.media_stream.snapshot']=module
class ProbeTests(unittest.IsolatedAsyncioTestCase):
 def make(self,events):
  controller=types.SimpleNamespace(getRecordings=Mock(return_value=[{'v':{'startTime':100,'endTime':200}}]),executeFunction=Mock(return_value={'playback':{'search_detection_list':events}}))
  async def execute(fn,*args):return fn(*args)
  self.hass=types.SimpleNamespace(async_add_executor_job=execute);self.device={'controller':controller};snapshot.reset_mock();state.clear();return controller
 async def test_only_one_native_controller_snapshot_for_real_in_range_event(self):
  controller=self.make([{'start_time':50},{'start_time':123},{'start_time':150}]);data=io.BytesIO();Image.new('RGB',(16,12)).save(data,format='JPEG');state['image']=data.getvalue()
  result=await probe.check(self.hass,self.device,'20261017');self.assertEqual(result['status'],'ready');self.assertEqual((result['width'],result['height']),(16,12));snapshot.assert_called_once_with(controller,[123],timeout=8);self.assertTrue(state['closed'])
  query=controller.executeFunction.call_args.args[1]['playback']['search_detection_list'];self.assertEqual(query['end_index'],127)
 async def test_no_event_does_not_open_media_session(self):
  self.make([]);result=await probe.check(self.hass,self.device,'20261017');self.assertEqual(result['status'],'no_events');snapshot.assert_not_called()
 async def test_unsupported_snapshot_returns_no_jpeg_without_retry(self):
  self.make([{'start_time':123}]);state['image']=None;result=await probe.check(self.hass,self.device,'20261017');self.assertEqual(result['status'],'no_jpeg');snapshot.assert_called_once();self.assertTrue(state['closed'])
 async def test_errors_are_sanitized_and_not_retried(self):
  self.make([{'start_time':123}]);state['error']=ValueError('private session must not be printed');result=await probe.check(self.hass,self.device,'20261017');self.assertEqual(result['status'],'error');self.assertEqual(result['stage'],'snapshot');self.assertNotIn('private',str(result));snapshot.assert_called_once();self.assertTrue(state['closed'])
if __name__=='__main__':unittest.main()
