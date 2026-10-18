"""Owned adapter behavior with native/HA boundaries stubbed; no camera access."""
import ast,asyncio,copy,datetime,re,importlib.util,pathlib,sys,time,types,unittest
from unittest.mock import AsyncMock,Mock,patch
from urllib.parse import urlparse,parse_qsl,urlencode
BASE=pathlib.Path(__file__).resolve().parents[1]/'files';UI=BASE/'desk_cluster'
def module(name,path):
 spec=importlib.util.spec_from_file_location(name,path);value=importlib.util.module_from_spec(spec);spec.loader.exec_module(value);return value
remote=module('recording_remote',UI/'recording_remote.py');model=module('recording_model',UI/'recording_model.py');storage=module('camera_storage',UI/'camera_storage.py')
sys.path.insert(0,str(BASE.parents[1]/'desk_camera/files'));import playback
class Unresolvable(Exception):pass
class Native:
 name='Tapo: Recordings'
 def __init__(self,hass,entry):self.hass=hass;self.entry=entry;self.calls=0
 async def async_browse_media(self,item):self.calls+=1;return copy.deepcopy(self.raw)
 def _any_download_active(self,device):return False
 def generateView(self,identifier,title,can_play,can_expand,thumbnail=None,children=None):return types.SimpleNamespace(identifier=identifier,title=title,can_play=can_play,can_expand=can_expand,children=children)
 def _format_clip_label(self,a,b,offset):return str(a)+'-'+str(b)
 def _create_background_task(self,coro,entry):return asyncio.create_task(coro)
def parse(value):return dict(parse_qsl(urlparse(value).query))
def ident(value):return 'tapo_control/?'+urlencode(value)
ns={'LOADING_IMAGE':'data:image/svg+xml;base64,PHN2Zy8+', 'asyncio':asyncio,'re':re,'copy':copy,'datetime':datetime,'time':time,'urlparse':urlparse,'parse_qsl':parse_qsl,'persistent_notification':Mock(),'async_sign_path':lambda h,p,d:p,'Unresolvable':Unresolvable,'PlayMedia':lambda url,mime:(url,mime),'TapoMediaSource':Native,'parse_identifier':parse,'build_identifier':ident,'bridge':AsyncMock(),'HOSTS':storage.HOSTS,'storage_state':storage.storage_state,'PART_SECONDS':model.PART_SECONDS,'MESSAGES':model.MESSAGES,'cached_state':model.cached_state,'clip_id':model.clip_id,'label':model.label,'parts':model.parts,'DOMAIN':'tapo_control','OwnerAPI':remote.OwnerAPI}
tree=ast.parse((UI/'camera_recordings.py').read_text());exec(compile(ast.Module(body=[n for n in tree.body if isinstance(n,ast.ClassDef)],type_ignores=[]),str(UI/'camera_recordings.py'),'exec'),ns);Adapter=ns['WorkerRecordings']
class Recordings(unittest.IsolatedAsyncioTestCase):
 async def test_owner_mode_browse_and_playback_never_allocate_on_core_or_fall_back(self):
  a=self.make();a.owner.workloads=frozenset(['tapo_c222_160']);a.owner.browse=AsyncMock(return_value=copy.deepcopy(a.raw))
  await a.async_browse_media(self.item);self.assertEqual(a.calls,0);self.controller.getUserID.assert_not_called()
  ns['bridge']=AsyncMock(return_value={'state':'ready','node':'deskb1x','id':'a'*32})
  await a.async_resolve_media(types.SimpleNamespace(identifier=self.child.identifier))
  self.controller.getUserID.assert_not_called();self.assertIsNone(ns['bridge'].call_args.kwargs['value']['session'])
  a.index_cache.clear();a.owner.browse.side_effect=ValueError('owner unavailable')
  with self.assertRaises(Unresolvable):await a.async_browse_media(self.item)
  self.assertEqual(a.calls,0)
 def make(self):
  self.entry=types.SimpleNamespace(entry_id='camera',domain='tapo_control',data={'ip_address':'192.168.1.160'})
  self.controller=types.SimpleNamespace(getUserID=Mock(return_value=123),userID=123,playerID='native-player')
  self.device={'camData':{'sdCardData':[{'status':'normal'}]},'controller':self.controller}
  self.hass=types.SimpleNamespace(config_entries=types.SimpleNamespace(async_get_entry=lambda _:self.entry),data={'tapo_control':{'camera':self.device}},async_add_executor_job=AsyncMock(side_effect=lambda fn,*args:fn(*args)))
  self.a=Adapter(self.hass);self.a.inventory=AsyncMock(return_value={'items':[],'complete':True})
  self.query={'entry':'camera','date':'2026-10-16','title':'Day'};self.item=types.SimpleNamespace(identifier=ident(self.query))
  q=dict(self.query,startDate=1792112400,endDate=1792112460,title='00:00-00:01');self.child=self.a.generateView(ident(q),'00:00-00:01',True,False);self.a.raw=self.a.generateView(self.item.identifier,'Day',False,True,children=[self.child]);return self.a
 async def test_native_loading_placeholder_never_allocates_a_camera_handle(self):
  a=self.make();ns['bridge']=AsyncMock()
  result=await a.async_resolve_media(types.SimpleNamespace(identifier='tapo_control/?desk_loading='+'a'*32))
  self.assertTrue(result[0].startswith('data:image/svg+xml;base64,'));self.assertEqual(result[1],'image/svg+xml')
  self.controller.getUserID.assert_not_called();ns['bridge'].assert_not_awaited()
 async def test_browsing_during_preparation_uses_last_index_without_camera_request(self):
  a=self.make();await a.async_browse_media(self.item);self.assertEqual(a.calls,1)
  task=asyncio.get_running_loop().create_future();a.jobs={'x':{'workload':'tapo_c222_160','task':task}}
  a.index_cache[self.item.identifier]=(0,a.index_cache[self.item.identifier][1]);value=await a.async_browse_media(self.item)
  self.assertEqual(a.calls,1);self.assertIn('Not downloaded',value.children[0].title);task.cancel()
 async def test_completed_worker_cache_is_marked_and_eviction_removes_the_mark(self):
  a=self.make();item={'workload':'tapo_c222_160','id':model.clip_id('tapo_c222_160','20261016',1792112400,1792112460),'state':'ready'}
  a.inventory.return_value={'items':[item],'complete':True};v=await a.async_browse_media(self.item);self.assertIn('Downloaded',v.children[0].title)
  a.inventory.return_value={'items':[],'complete':True};v=await a.async_browse_media(self.item);self.assertIn('Not downloaded',v.children[0].title)
 async def test_long_original_recording_expands_to_bounded_contiguous_parts(self):
  a=self.make();q=dict(self.query,startDate=1792112400,endDate=1792114434,title='Long clip');a.raw.children=[a.generateView(ident(q),'Long clip',True,False)]
  v=await a.async_browse_media(self.item);self.assertTrue(v.children[0].can_expand);self.assertFalse(v.children[0].can_play);self.assertIsNone(v.children[0].thumbnail)
  v=await a.async_browse_media(types.SimpleNamespace(identifier=ident(q)));ranges=[(int(parse(c.identifier)['startDate']),int(parse(c.identifier)['endDate'])) for c in v.children]
  self.assertTrue(all(c.thumbnail.startswith('/api/desk_cluster/thumbnail/') for c in v.children));self.assertEqual(len(ranges),4);self.assertEqual(ranges[0][0],q['startDate']);self.assertEqual(ranges[-1][1],q['endDate']);self.assertTrue(all(b-a<=600 for a,b in ranges));self.assertTrue(all(ranges[i][1]==ranges[i+1][0] for i in range(len(ranges)-1)))
 async def test_cached_playback_does_not_allocate_another_camera_handle(self):
  a=self.make();q=parse(self.child.identifier);item={'workload':'tapo_c222_160','id':model.clip_id('tapo_c222_160',q['date'],q['startDate'],q['endDate']),'state':'ready'}
  a.inventory.return_value={'items':[item],'complete':True};ns['bridge']=AsyncMock(return_value={'state':'ready','node':'deskb2x','id':item['id']})
  await a.async_resolve_media(types.SimpleNamespace(identifier=self.child.identifier));self.controller.getUserID.assert_not_called();self.assertIsNone(ns['bridge'].call_args.kwargs['value']['session'])
 async def test_rejoining_running_clip_uses_existing_worker_without_new_session_or_job(self):
  a=self.make();q=parse(self.child.identifier);ident=model.clip_id('tapo_c222_160',q['date'],q['startDate'],q['endDate'])
  a.inventory.return_value={'items':[{'workload':'tapo_c222_160','id':ident,'state':'preparing','node':'deskb3x'}],'complete':True};ns['bridge']=AsyncMock()
  ready=asyncio.Event()
  async def wait(*args):
   await ready.wait();args[0]['available'].set()
  a.wait=wait;ns['bridge']=AsyncMock(return_value={'state':'ready'})
  resolve=asyncio.create_task(a.async_resolve_media(types.SimpleNamespace(identifier=self.child.identifier)))
  await asyncio.sleep(0);await asyncio.sleep(0);self.assertFalse(resolve.done());self.controller.getUserID.assert_not_called();ns['bridge'].assert_not_called()
  ready.set();value=await resolve;self.assertIn(ident,value[0]);self.assertEqual(ns['bridge'].call_args.args[0],'camera_status')
 async def test_closing_resolver_keeps_preparation_running_and_rejoin_waits(self):
  a=self.make();ready=asyncio.Event()
  async def finish():await ready.wait()
  job={'workload':'tapo_c222_160','node':'deskb1x','id':'a'*32,'task':asyncio.create_task(finish())}
  resolve=asyncio.create_task(a.prepared(job['workload'],job));await asyncio.sleep(0);resolve.cancel()
  with self.assertRaises(asyncio.CancelledError):await resolve
  self.assertFalse(job['task'].cancelled());self.assertFalse(job['task'].done())
  ns['bridge']=AsyncMock(return_value={'state':'ready'});ready.set();value=await a.prepared(job['workload'],job);self.assertIn(job['id'],value[0])
 async def test_new_preparation_resolves_only_after_worker_is_ready(self):
  a=self.make();ready=asyncio.Event()
  async def wait(*args):
   await ready.wait();args[0]['available'].set()
  a.wait=wait
  async def bridge(action,**kwargs):
   if action=='camera_prepare':return {'id':'b'*32,'node':'deskb1x','state':'preparing'}
   if action=='camera_status':return {'state':'ready'}
   raise AssertionError(action)
  ns['bridge']=AsyncMock(side_effect=bridge)
  resolve=asyncio.create_task(a.async_resolve_media(types.SimpleNamespace(identifier=self.child.identifier)))
  await asyncio.sleep(0);await asyncio.sleep(0);self.assertFalse(resolve.done());self.controller.getUserID.assert_called_once()
  ready.set();value=await resolve;self.assertIn('b'*32,value[0]);self.assertEqual(ns['bridge'].call_args.args[0],'camera_status')
 async def test_failed_preparation_does_not_return_a_dead_media_url(self):
  a=self.make();job={'workload':'tapo_c222_160','node':'deskb1x','id':'c'*32,'task':asyncio.create_task(asyncio.sleep(0))};ns['bridge']=AsyncMock(return_value={'state':'failed'})
  with self.assertRaisesRegex(Unresolvable,'could not prepare'):await a.prepared(job['workload'],job)
 async def test_hls_resolves_while_background_download_is_still_running(self):
  a=self.make();done=asyncio.Event();job={'workload':'tapo_c222_160','node':'deskb1x','id':'d'*32,'available':asyncio.Event()}
  job['task']=asyncio.create_task(done.wait());job['available'].set()
  ns['bridge']=AsyncMock(return_value={'state':'playable','format':'hls','generation':'e'*32})
  value=await a.prepared(job['workload'],job)
  self.assertEqual(value[1],'application/x-mpegURL');self.assertIn('/'+'e'*32+'/index.m3u8',value[0]);self.assertFalse(job['task'].done())
  done.set();await job['task']
 async def test_fresh_core_handle_is_bound_and_resource_rejection_is_specific(self):
  a=self.make();ns['bridge']=AsyncMock(return_value={'state':'rejected','reason':'memory_low'})
  with self.assertRaisesRegex(Unresolvable,'free memory'):await a.async_resolve_media(types.SimpleNamespace(identifier=self.child.identifier))
  self.assertEqual(ns['bridge'].call_args.kwargs['value']['session'],{'user_id':123,'player_id':'native-player'})
  self.controller.getUserID.assert_called_once_with(True)
 async def test_stale_browse_handle_refreshes_after_thumbnail_drain_under_entry_lock(self):
  a=self.make();drained=False
  async def pause(entry):
   nonlocal drained
   self.assertEqual(entry,'camera');await asyncio.sleep(0);drained=True
  a._thumbnails=types.SimpleNamespace(pause=AsyncMock(side_effect=pause))
  def allocate(force=False):
   self.assertTrue(drained);self.assertTrue(a.locks['camera'].locked());self.assertTrue(force)
   self.controller.userID=456;return 456
  self.controller.getUserID.side_effect=allocate
  ns['bridge']=AsyncMock(return_value={'state':'rejected','reason':'memory_low'})
  with self.assertRaises(Unresolvable):await a.async_resolve_media(types.SimpleNamespace(identifier=self.child.identifier))
  self.controller.getUserID.assert_called_once_with(True)
  ns['bridge'].assert_awaited_once()
  self.assertEqual(ns['bridge'].call_args.kwargs['value']['session'],{'user_id':456,'player_id':'native-player'})
 async def test_failed_core_allocation_never_submits_or_retries_worker_preparation(self):
  a=self.make();self.controller.getUserID.side_effect=ValueError('Camera busy');ns['bridge']=AsyncMock()
  with self.assertRaisesRegex(Unresolvable,'session is busy'):await a.async_resolve_media(types.SimpleNamespace(identifier=self.child.identifier))
  self.controller.getUserID.assert_called_once_with(True);ns['bridge'].assert_not_awaited();self.assertFalse(a.jobs)
 async def test_expiry_invalidates_only_matching_core_handle_and_index_without_device_io(self):
  a=self.make();await a.async_browse_media(self.item)
  a.index_cache['tapo_control/?entry=other']=(0,'other-camera')
  await a.invalidate_media_session('camera',{'user_id':123,'player_id':'native-player'})
  self.assertIs(self.controller.userID,False);self.controller.getUserID.assert_not_called()
  self.assertNotIn(self.item.identifier,a.index_cache);self.assertIn('tapo_control/?entry=other',a.index_cache)
 async def test_delayed_expiry_cannot_discard_newer_core_handle(self):
  a=self.make();lock=a.locks.setdefault('camera',asyncio.Lock())
  async with lock:
   task=asyncio.create_task(a.invalidate_media_session('camera',{'user_id':123,'player_id':'native-player'}))
   await asyncio.sleep(0);self.assertFalse(task.done());self.controller.userID=456
  await task;self.assertEqual(self.controller.userID,456);self.controller.getUserID.assert_not_called()
 async def test_worker_expiry_notification_clears_matching_handle_without_retry(self):
  a=self.make();ns['bridge']=AsyncMock(return_value={'state':'failed','reason':'session_expired'});ns['persistent_notification']=Mock()
  job={'workload':'tapo_c222_160','node':'deskb1x','id':'a'*32,'duration':1,'_entry':'camera','_session':{'user_id':123,'player_id':'native-player'}}
  with patch.object(asyncio,'sleep',new=AsyncMock()):await a.wait(job,'notice')
  self.assertIs(self.controller.userID,False);self.controller.getUserID.assert_not_called();ns['bridge'].assert_awaited_once()
  self.assertEqual(ns['persistent_notification'].async_create.call_args.args[1],model.MESSAGES['session_expired'])
 async def test_expired_preparation_returns_specific_error_instead_of_dead_url(self):
  a=self.make();job={'workload':'tapo_c222_160','node':'deskb1x','id':'a'*32,'task':asyncio.create_task(asyncio.sleep(0))};ns['bridge']=AsyncMock(return_value={'state':'failed','reason':'session_expired'})
  with self.assertRaisesRegex(Unresolvable,'session expired'):await a.prepared(job['workload'],job)
 async def test_thumbnail_check_is_explicit_serialized_and_cached(self):
  a=self.make();ns['__package__']='desk_probe_test';probe_module=types.ModuleType('desk_probe_test.thumbnail_probe');probe_module.check=AsyncMock(return_value={'status':'ready','bytes':1234,'width':640,'height':360});ns['bridge']=AsyncMock(return_value={'items':[],'complete':True})
  with patch.dict(sys.modules,{'desk_probe_test.thumbnail_probe':probe_module}):
   query=dict(self.query,thumbnail_check='1')
   for _ in range(2):value=await a.thumbnail_check(query,self.entry,'tapo_c222_160',self.device)
   probe_module.check.assert_awaited_once();self.assertIn('ready',value.title);self.assertIn('width=640',value.children[0].title)
 async def test_thumbnail_check_refuses_to_compete_with_active_playback(self):
  a=self.make();ns['bridge']=AsyncMock(return_value={'items':[{'state':'preparing'}],'complete':True})
  with self.assertRaisesRegex(Unresolvable,'No thumbnail request'):await a.thumbnail_check(self.query,self.entry,'tapo_c222_160',self.device)
 def test_worker_and_core_use_identical_cache_identity(self):
  job=playback.validate('tapo_c222_160',{'date':'20261016','start':1792112400,'end':1792112460})
  self.assertEqual(playback.identity(job),model.clip_id('tapo_c222_160','2026-10-16',job['start'],job['end']))
if __name__=='__main__':unittest.main()
