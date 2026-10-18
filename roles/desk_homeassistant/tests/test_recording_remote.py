"""Core has no allocator for selected cameras; signed metadata stays owner-bound."""
import asyncio,importlib.util,json,pathlib,sys,tempfile,time,types,unittest
from unittest.mock import AsyncMock,patch
FILES=pathlib.Path(__file__).resolve().parents[1]/'files/desk_cluster'
CAMERA=FILES.parents[2]/'desk_camera/files'
package=types.ModuleType('remote_test');package.__path__=[str(FILES)];sys.modules['remote_test']=package
def load(name,path):
 spec=importlib.util.spec_from_file_location('remote_test.'+name,path);m=importlib.util.module_from_spec(spec);sys.modules[spec.name]=m;spec.loader.exec_module(m);return m
m=load('recording_remote',FILES/'recording_remote.py');c=load('recording_contract',CAMERA/'recording_contract.py')
class SDK:
 def __init__(self,host):self.host=host;self.calls=0
 def executeFunction(self,*args,**kwargs):self.calls+=1;return 'control'
 def performRequest(self,*args,**kwargs):self.calls+=1;return 'control'
 def getUserID(self,*args,**kwargs):self.calls+=1;return 'private'
 getRecordings=getRecordingsUTC=getRecordingsList=getMediaSession=getUserID
class RemoteTests(unittest.IsolatedAsyncioTestCase):
 def setUp(self):
  self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
  self.path=pathlib.Path(self.tmp.name)/'policy.json';self.path.write_text(json.dumps({'version':1,'workloads':['tapo_c222_163']}))
  self.hass=types.SimpleNamespace(config=types.SimpleNamespace(path=lambda *args:str(self.path)))
 def test_guard_blocks_sdk_and_raw_bypasses_but_preserves_control_and_other_cameras(self):
  class Tapo(SDK):pass
  sdk=types.SimpleNamespace(Tapo=Tapo)
  with patch.dict(sys.modules,{'pytapo':sdk,'custom_components.tapo_control':types.SimpleNamespace(scheduleAll=AsyncMock())}):
   selected=Tapo('192.168.1.163');other=Tapo('192.168.1.160');m.install_guard(self.hass)
   for fn,args in [(selected.getUserID,()),(selected.getRecordings,('20261017',)),(selected.getMediaSession,()),(selected.executeFunction,('getUserID',)),(selected.performRequest,({'method':'multipleRequest','params':{'requests':[{'method':'searchVideoOfDay'}]}},))]:
    with self.assertRaises(ValueError):fn(*args)
   self.assertEqual(selected.calls,0);self.assertEqual(selected.executeFunction('getDeviceInfo'),'control');self.assertEqual(other.getUserID(),'private')
   self.assertTrue(m.guard_status(self.hass)['guarded']);self.assertEqual(m.guard_status(self.hass)['blocked_core_calls'],5)
   self.path.write_text(json.dumps({'version':1,'workloads':[]}))
   with self.assertRaises(ValueError):m.install_guard(self.hass)
 async def test_native_media_scheduler_is_skipped_only_for_selected_camera(self):
  class Tapo(SDK):pass
  schedule=AsyncMock(return_value='legacy');native=types.SimpleNamespace(scheduleAll=schedule)
  with patch.dict(sys.modules,{'pytapo':types.SimpleNamespace(Tapo=Tapo),'custom_components.tapo_control':native}):
   m.install_guard(self.hass)
   await native.scheduleAll(self.hass,{},types.SimpleNamespace(data={'ip_address':'192.168.1.163'}),None)
   schedule.assert_not_awaited()
   value=await native.scheduleAll(self.hass,{},types.SimpleNamespace(data={'ip_address':'192.168.1.160'}),None)
   self.assertEqual(value,'legacy');schedule.assert_awaited_once()
 async def test_remote_pages_poll_and_validate_binding_without_sdk(self):
  now=time.time();self.calls=[];saved={};snapshot='d'*32
  async def bridge(action,value=None):
   if action=='cameras':return {'cameras':[{'workload':'tapo_c222_163','ready':True,'owner':'deskb1x','generation':'b'*32}]}
   request=value['request'];self.calls.append(request)
   if request['operation']=='dates':saved[request['request_id']]=request;return dict(c.response(request,'queued'))
   original=saved[request['request_id']]
   data={'items':['20261017'],'snapshot':snapshot,'next_cursor':None,'observed_at':now,'freshness':'fresh','complete':True,'storage':'ready','time_basis':'camera_epoch_seconds','camera_offset_seconds':0,'clock_correction_seconds':0}
   return c.response(original,'done',data=data,now=time.time())
  value=await m.OwnerAPI(self.hass,bridge).read('tapo_c222_163','dates')
  self.assertEqual(value['items'],['20261017']);self.assertEqual([v['operation'] for v in self.calls],['dates','result']);self.assertFalse(any('session' in v for v in self.calls))
 async def test_wrong_generation_is_cancelled_and_never_used_as_empty_index(self):
  calls=[]
  async def bridge(action,value=None):
   if action=='cameras':return {'cameras':[{'workload':'tapo_c222_163','ready':True,'owner':'deskb1x','generation':'b'*32}]}
   calls.append(value['request']['operation'])
   return {'state':'done','version':1,'workload':'tapo_c222_163','generation':'c'*32,'request_id':value['request']['request_id'],'data':{}}
  with self.assertRaises(ValueError):await m.OwnerAPI(self.hass,bridge).read('tapo_c222_163','dates')
  self.assertEqual(calls,['dates','cancel'])
 async def test_relay_ready_without_published_generation_is_not_recording_ready(self):
  bridge=AsyncMock(return_value={'cameras':[{'workload':'tapo_c222_163','ready':True,'owner':'deskb1x','generation':None}]})
  with self.assertRaisesRegex(ValueError,'Camera owner unavailable'):await m.OwnerAPI(self.hass,bridge).read('tapo_c222_163','dates')
  bridge.assert_awaited_once_with('cameras')
if __name__=='__main__':unittest.main()
