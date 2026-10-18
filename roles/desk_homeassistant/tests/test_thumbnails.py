import ast,asyncio,datetime,hashlib,importlib.util,io,json,pathlib,re,secrets,time,types,unittest
from unittest.mock import Mock,AsyncMock,patch
from PIL import Image
aiohttp=types.SimpleNamespace(ClientTimeout=lambda **kwargs:kwargs)
BASE=pathlib.Path(__file__).resolve().parents[1]/'files/desk_cluster'
spec=importlib.util.spec_from_file_location('thumbnail_cache',BASE/'thumbnail_cache.py');cache=importlib.util.module_from_spec(spec);spec.loader.exec_module(cache)
spec=importlib.util.spec_from_file_location('thumbnail_recording_model',BASE/'recording_model.py');model=importlib.util.module_from_spec(spec);spec.loader.exec_module(model)
ns={'asyncio':asyncio,'datetime':datetime,'hashlib':hashlib,'io':io,'json':json,'re':re,'secrets':secrets,'time':time,'Cache':cache.Cache,'aiohttp':aiohttp,'EVENT_HOMEASSISTANT_STOP':'stop','PLACEHOLDER':b'placeholder','PAUSED':b'paused','MediaSessionExpired':model.MediaSessionExpired}
tree=ast.parse((BASE/'recording_thumbnails.py').read_text());exec(compile(ast.Module(body=[n for n in tree.body if isinstance(n,(ast.FunctionDef,ast.ClassDef)) and n.name not in ('ThumbnailView','ThumbnailScopeView')],type_ignores=[]),'recording_thumbnails','exec'),ns);Thumbnails=ns['Thumbnails']
buffer=io.BytesIO();Image.new('RGB',(32,18)).save(buffer,format='JPEG');JPEG=buffer.getvalue()
class CacheTests(unittest.TestCase):
 def test_ttl_lru_byte_and_entry_limits(self):
  c=cache.Cache(limit=6,entries=2,ttl=10)
  with patch.object(cache.time,'monotonic',return_value=100) as clock:
   c.put('a',b'aa','event');c.put('b',b'bb','event');self.assertEqual(c.get('a')[0],b'aa');c.put('c',b'ccc','keyframe');self.assertIsNone(c.get('b'));self.assertEqual(c.bytes,5);clock.return_value=111;c.prune();self.assertEqual(c.bytes,0)
 def test_negative_ttl_and_replacement_accounting(self):
  c=cache.Cache(limit=100,entries=2,ttl=1000)
  with patch.object(cache.time,'monotonic',return_value=100) as clock:
   c.put('x',b'abc','placeholder');c.put('x',b'abcdef','event');self.assertEqual(c.bytes,6);c.put('y',b'none','placeholder');clock.return_value=121;c.prune();self.assertIsNone(c.get('y'));self.assertEqual(c.bytes,6)
class ThumbnailTests(unittest.IsolatedAsyncioTestCase):
 async def asyncSetUp(self):
  self.start=1792112400;self.end=self.start+60;self.controller=types.SimpleNamespace(getUserID=Mock(return_value=12),playerID='native-player');self.device={'controller':self.controller}
  self.hass=types.SimpleNamespace(async_create_background_task=lambda coro,name:asyncio.create_task(coro),bus=types.SimpleNamespace(async_listen_once=Mock()),async_add_executor_job=AsyncMock(side_effect=lambda fn,*args:fn(*args)))
  self.source=types.SimpleNamespace(hass=self.hass,locks={},camera_entry=lambda q:(None,'tapo_c222_160',self.device),check_storage=Mock(),inventory=AsyncMock(return_value={'items':[],'complete':True}),active=Mock(return_value=False),_any_download_active=Mock(return_value=False))
  self.bridge=AsyncMock(return_value={'cameras':[{'workload':'tapo_c222_160','ready':True,'owner':'deskb2x','generation':'a'*32}]});self.m=Thumbnails(self.source,self.bridge);self.m.remote=AsyncMock(return_value=(JPEG,'event'));self.t=('user','b'*32,1,'c'*32)
  await self.focus()
 async def focus(self,t=None,date='20261016',lower=None,upper=None):
  t=t or self.t;return await self.m.focus(t[0],{'client':t[1],'sequence':t[2],'scope':t[3],'folder':{'entry':'entry','date':date,'lower':lower,'upper':upper}})
 async def get(self,t=None):return await self.m.get('entry','20261016',self.start,self.end,t or self.t)
 async def asyncTearDown(self):await self.m.stop(None)
 async def test_fetch_routes_to_owner_and_core_only_allocates_handle(self):
  await self.get();self.m.remote.assert_awaited_once();args=self.m.remote.call_args.args;self.assertEqual(args[1]['owner'],'deskb2x');self.assertEqual(args[3],{'user_id':12,'player_id':'native-player'});self.controller.getUserID.assert_called_once()
 async def test_second_folder_scope_reuses_ram_without_worker_or_handle(self):
  await self.get();next_scope=('user','b'*32,2,'d'*32);await self.focus(next_scope);self.assertEqual((await self.get(next_scope))[1],'event');self.m.remote.assert_awaited_once();self.controller.getUserID.assert_called_once();self.assertEqual(self.m.stats()['hits'],1)
 async def test_switching_folder_cancels_current_and_queued_then_new_scope_runs(self):
  opened=asyncio.Event();closed=asyncio.Event()
  async def remote(*args):
   try:opened.set();await asyncio.Event().wait()
   finally:closed.set()
  self.m.remote.side_effect=remote;old=asyncio.create_task(self.get());await opened.wait()
  queued=asyncio.create_task(self.m.get('entry','20261016',self.end,self.end+60,self.t));await asyncio.sleep(0)
  newer=('user','b'*32,2,'d'*32);await self.focus(newer,date='20261017');self.assertTrue(closed.is_set());await asyncio.gather(old,queued);self.assertFalse(self.m.tasks);self.assertFalse(self.source.locks['entry'].locked());self.assertEqual(self.m.counts['cancelled'],2)
  self.assertEqual((await self.get())[1],'placeholder');self.m.remote.side_effect=None
  await self.focus(('user','b'*32,3,'e'*32));await self.get(('user','b'*32,3,'e'*32));self.assertEqual(self.m.counts['event_jpegs'],1)
 async def test_two_tabs_share_job_and_one_navigation_does_not_cancel_other(self):
  second=('user','d'*32,1,'e'*32);await self.focus(second);opened=asyncio.Event();gate=asyncio.Event()
  async def remote(*args):opened.set();await gate.wait();return JPEG,'event'
  self.m.remote.side_effect=remote;a=asyncio.create_task(self.get());await opened.wait();b=asyncio.create_task(self.get(second));await asyncio.sleep(0)
  await self.focus(('user','b'*32,2,'f'*32),date='20261017');self.assertEqual(self.m.counts['cancelled'],0);gate.set();self.assertTrue(all(r[1]=='event' for r in await asyncio.gather(a,b)));self.m.remote.assert_awaited_once()
 async def test_late_focus_and_old_requests_cannot_reactivate_previous_folder(self):
  await self.focus(('user','b'*32,2,'d'*32),date='20261017');self.assertEqual(await self.focus(),{'accepted':False});self.assertEqual((await self.get())[1],'placeholder');self.m.remote.assert_not_called()
 async def test_scope_is_bound_to_user_entry_date_and_part_bounds(self):
  t=('user','b'*32,2,'d'*32);await self.focus(t,lower=self.start+1,upper=self.end);self.assertEqual((await self.get(t))[1],'placeholder');self.assertFalse(self.m.live(('other',*t[1:])))
 async def test_expired_tab_scope_cancels_orphan(self):
  opened=asyncio.Event()
  async def remote(*args):opened.set();await asyncio.Event().wait()
  self.m.remote.side_effect=remote;a=asyncio.create_task(self.get());await opened.wait();self.m.clients[self.t[:2]]['at']-=100;await self.m.orphans();self.assertEqual((await a)[1],'placeholder');self.assertFalse(self.m.tasks)
 async def test_playback_cancels_thumbnail_and_waits_for_cleanup(self):
  opened=asyncio.Event();cleaned=asyncio.Event()
  async def remote(*args):
   try:opened.set();await asyncio.Event().wait()
   finally:await asyncio.sleep(.01);cleaned.set()
  self.m.remote.side_effect=remote;a=asyncio.create_task(self.get());await opened.wait();await self.m.pause('entry');self.assertTrue(cleaned.is_set());self.assertEqual((await a)[1],'paused');self.assertFalse(self.source.locks['entry'].locked())
 async def test_busy_does_not_allocate_or_call_worker(self):
  self.source.active.return_value=True;self.assertEqual((await self.get())[1],'paused');self.controller.getUserID.assert_not_called();self.m.remote.assert_not_called()
 async def test_expired_handle_invalidates_after_lock_release_without_negative_cache(self):
  async def invalidate(entry,session):
   self.assertEqual((entry,session),('entry',{'user_id':12,'player_id':'native-player'}));self.assertFalse(self.source.locks['entry'].locked())
  self.source.invalidate_media_session=AsyncMock(side_effect=invalidate);self.m.remote.side_effect=model.MediaSessionExpired()
  self.assertEqual((await self.get())[1],'placeholder');self.source.invalidate_media_session.assert_awaited_once()
  self.assertFalse(self.m.cache.data);self.assertEqual(self.m.last['reason'],'session_expired');self.m.remote.assert_awaited_once()
 async def test_remote_expiry_is_typed_only_after_explicit_cancel_cleanup(self):
  calls=[];cleaned=[]
  class Reply:
   status=409;headers={'X-Desk-Media-Error':'session_expired'}
   async def __aenter__(self):return self
   async def __aexit__(self,*args):pass
   async def read(self):cleaned.append(True);return b''
  class Client:
   def post(self,*args,**kwargs):calls.append(kwargs);return Reply()
  self.bridge.return_value={'url':'http://worker/thumbnail','authorization':'test-token'}
  with patch.dict(ns,{'async_get_clientsession':lambda _:Client()}):
   with self.assertRaises(model.MediaSessionExpired):await Thumbnails.remote(self.m,'tapo_c222_160',{'owner':'deskb2x','generation':'a'*32},{'date':'20261016','start':self.start,'end':self.end},{'user_id':12,'player_id':'native-player'})
  self.assertEqual(len(calls),2);self.assertEqual(calls[1]['data'],b'');self.assertTrue(cleaned)
 async def test_paused_preview_does_not_poison_cache_and_cached_jpeg_still_wins(self):
  self.source.active.return_value=True
  self.assertEqual((await self.get())[1],'paused');self.assertFalse(self.m.cache.data)
  self.source.active.return_value=False
  self.assertEqual((await self.get())[1],'event')
  await self.m.pause('entry');self.source.active.return_value=True
  self.assertEqual((await self.get())[1],'event');self.m.remote.assert_awaited_once()
 async def test_resume_removes_admission_pause_immediately(self):
  await self.m.pause('entry');self.assertEqual((await self.get())[1],'paused')
  self.m.cache.put(('entry','20261016',self.start,self.end),b'fallback','placeholder')
  self.m.resume('entry');self.assertEqual((await self.get())[1],'event')
 async def test_no_ready_owner_is_deferred_without_poisoning_cache(self):
  self.bridge.return_value={'cameras':[]}
  self.assertEqual((await self.get())[1],'paused');self.assertFalse(self.m.cache.data)
  self.controller.getUserID.assert_not_called()
 async def test_focus_close_is_monotonic_and_cannot_be_reopened_by_heartbeat(self):
  await self.m.focus('user',{'client':self.t[1],'sequence':2,'scope':None,'folder':None});self.assertFalse(self.m.live(self.t));self.assertEqual(await self.focus(),{'accepted':False})
 async def test_cancelled_remote_post_sends_and_awaits_explicit_worker_cancel(self):
  entered=asyncio.Event();cleanup=asyncio.Event();calls=[]
  class Reply:
   def __init__(self,first):self.first=first
   async def __aenter__(self):
    if self.first:entered.set();await asyncio.Event().wait()
    return self
   async def __aexit__(self,*args):pass
   async def read(self):cleanup.set();return b''
  class Client:
   def post(self,*args,**kwargs):calls.append(kwargs);return Reply(len(calls)==1)
  ns['async_get_clientsession']=lambda _:Client()
  self.bridge.return_value={'url':'http://worker/thumbnail','authorization':'test-token'}
  camera={'owner':'deskb2x','generation':'a'*32};method=Thumbnails.remote
  task=asyncio.create_task(method(self.m,'tapo_c222_160',camera,{'date':'20261016','start':self.start,'end':self.end},{'user_id':12,'player_id':'native'}))
  await entered.wait();task.cancel();await asyncio.gather(task,return_exceptions=True);self.assertTrue(cleanup.is_set());self.assertEqual(self.bridge.call_args.kwargs['value']['source'],'cancel');self.assertEqual(calls[-1]['data'],b'')
 def test_malformed_ranges_and_jpeg_are_rejected(self):
  with self.assertRaises(ValueError):ns['validate']('../secret','20261016',self.start,self.end)
  with self.assertRaises(Exception):ns['jpeg'](b'bad')
if __name__=='__main__':unittest.main()
