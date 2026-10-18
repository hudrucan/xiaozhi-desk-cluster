import ast,asyncio,datetime,pathlib,re,time,types,sys,unittest
from unittest.mock import AsyncMock,Mock,patch
PATH=pathlib.Path(__file__).resolve().parents[1]/'files/desk_cluster/recording_sessions.py'
ns={'asyncio':asyncio,'datetime':datetime,'re':re,'time':time,'types':types,'bridge':AsyncMock(),'EVENT_HOMEASSISTANT_STOP':'stop'}
tree=ast.parse(PATH.read_text());exec(compile(ast.Module(body=[n for n in tree.body if isinstance(n,ast.ClassDef) and n.name=='Sessions'],type_ignores=[]),str(PATH),'exec'),ns)
class ViewerTests(unittest.IsolatedAsyncioTestCase):
 async def asyncSetUp(self):
  self.tasks=[]
  def background(coro,name):
   t=asyncio.create_task(coro);self.tasks.append(t);return t
  self.source=types.SimpleNamespace(hass=types.SimpleNamespace(async_create_background_task=background,bus=Mock()),jobs={},inventories={},camera_entry=lambda q:(None,'tapo_c222_160',None),async_resolve_media=AsyncMock(return_value=types.SimpleNamespace(url='/api/desk_cluster/recording/cam/node/clip',mime_type='video/mp4')))
  self.m=ns['Sessions'](self.source);self.key=('tapo_c222_160','20261017',1792190000,1792190060)
  ns['bridge']=AsyncMock(return_value={'state':'cancelled'})
 async def asyncTearDown(self):
  for t in [*self.tasks,*self.m.drains.values()]:t.cancel()
  await asyncio.gather(*self.tasks,*list(self.m.drains.values()),return_exceptions=True)
 def view(self,client='a',scope='b',seq=1,action='open'):
  v={'action':action,'client':client*32,'scope':scope*32,'sequence':seq}
  if action=='open':v['clip']={'entry':'camera','date':'2026-10-17','start':self.key[2],'end':self.key[3]}
  return v
 async def test_close_before_delayed_open_is_a_tombstone(self):
  await self.m.action('u',self.view(action='close'))
  with self.assertRaises(ValueError):await self.m.action('u',self.view())
  self.source.async_resolve_media.assert_not_awaited()
 async def test_two_tabs_share_job_only_last_close_cancels(self):
  await self.m.action('u',self.view());await self.m.action('u',self.view('c','d'))
  job={'workload':self.key[0],'node':'deskb3x','id':'e'*32,'attempt':'f'*32}
  self.m.bind('b'*32,self.key,job)
  await self.m.action('u',self.view(action='close'));ns['bridge'].assert_not_awaited()
  # Avoid HA import in cancel notification cleanup; it is orthogonal here.
  original=self.m.cancel
  async def cancel(key,value):await ns['bridge']('camera_cancel',value=value)
  self.m.cancel=cancel
  await self.m.action('u',self.view('c','d',action='close'))
  ns['bridge'].assert_awaited_once_with('camera_cancel',value=job)
 async def test_last_view_close_resumes_thumbnail_after_worker_drain(self):
  self.source._thumbnails=types.SimpleNamespace(resume=Mock())
  await self.m.action('u',self.view());await self.m.action('u',self.view('c','d'))
  await self.m.action('u',self.view(action='close'));self.source._thumbnails.resume.assert_not_called()
  await self.m.action('u',self.view('c','d',action='close'));self.source._thumbnails.resume.assert_called_once_with('camera')
 async def test_user_and_tab_cannot_close_another_viewer(self):
  await self.m.action('u',self.view())
  for user,value in [('other',self.view(action='close')),('u',self.view('c','b',action='touch'))]:
   with self.assertRaises(ValueError):await self.m.action(user,value)
  self.assertTrue(self.m.live('b'*32));ns['bridge'].assert_not_awaited()
 async def test_expiration_releases_refs_and_does_not_renew_orphan(self):
  await self.m.action('u',self.view());job={'workload':self.key[0],'node':'deskb2x','id':'e'*32,'attempt':'f'*32}
  self.m.bind('b'*32,self.key,job);self.m.records['b'*32]['at']-=25
  self.m.cancel=AsyncMock();await self.m.sweep();await asyncio.sleep(0)
  self.assertFalse(self.m.live('b'*32));self.m.cancel.assert_awaited_once();ns['bridge'].assert_not_awaited()
 async def test_late_result_after_close_never_returns_playable_url(self):
  entered=asyncio.Event();finish=asyncio.Event()
  async def resolve(item):entered.set();await finish.wait();return types.SimpleNamespace(url='/signed',mime_type='video/mp4')
  self.source.async_resolve_media=resolve
  opening=asyncio.create_task(self.m.action('u',self.view()));await entered.wait()
  await self.m.action('u',self.view(action='close'));finish.set()
  with self.assertRaises(ValueError):await opening
 async def test_binding_after_close_drains_only_that_attempt(self):
  await self.m.action('u',self.view());await self.m.action('u',self.view(action='close'))
  self.m.cancel=AsyncMock();job={'workload':self.key[0],'node':'deskb3x','id':'e'*32,'attempt':'f'*32}
  self.m.bind('b'*32,self.key,job);await asyncio.sleep(0);self.m.cancel.assert_awaited_once_with(self.key,job)
 async def test_new_open_waits_for_previous_worker_cleanup(self):
  gate=asyncio.Event();self.m.drains[self.key]=asyncio.create_task(gate.wait())
  opening=asyncio.create_task(self.m.action('u',self.view()));await asyncio.sleep(0)
  self.source.async_resolve_media.assert_not_awaited();gate.set();await opening
  self.source.async_resolve_media.assert_awaited_once()
 async def test_real_cancel_ack_then_waiter_cleanup_preserves_new_attempt(self):
  job={'workload':self.key[0],'node':'deskb3x','id':'e'*32,'attempt':'f'*32}
  waiter=asyncio.create_task(asyncio.Event().wait())
  self.source.jobs[self.key]=dict(job,attempt='0'*32,task=waiter)
  self.source.inventories[self.key[0]]=(0,{})
  components=types.ModuleType('homeassistant.components');components.persistent_notification=Mock()
  with patch.dict(sys.modules,{'homeassistant.components':components}):
   await self.m.cancel(self.key,job)
   self.assertFalse(waiter.done())
   components.persistent_notification.async_dismiss.assert_called_once()
   # Matching managed attempt releases its Core waiter after remote cleanup.
   self.source.jobs[self.key]['attempt']=job['attempt']
   await self.m.cancel(self.key,job)
   self.assertTrue(waiter.cancelled());self.assertNotIn(self.key[0],self.source.inventories)
 async def test_core_stop_and_sweep_leave_detached_job_to_browser_deadline(self):
  await self.m.action('u',self.view())
  job={'workload':self.key[0],'node':'deskb3x','id':'e'*32,'attempt':'f'*32}
  self.m.bind('b'*32,self.key,job);self.m.jobs[self.key]['external']=True
  await self.m.sweep();ns['bridge'].assert_not_awaited()
  self.m.cancel=AsyncMock();await self.m.stop(None)
  self.m.cancel.assert_not_awaited();self.assertFalse(self.m.jobs)
 async def test_second_core_viewer_binding_keeps_existing_detached_authority(self):
  await self.m.action('u',self.view())
  job={'workload':self.key[0],'node':'deskb3x','id':'e'*32,'attempt':'f'*32}
  self.m.bind('b'*32,self.key,job);self.m.jobs[self.key]['external']=True
  await self.m.action('u',self.view('c','d'));self.m.bind('d'*32,self.key,job)
  self.assertTrue(self.m.jobs[self.key]['external'])
  self.m.bind('d'*32,self.key,dict(job,attempt='0'*32))
  self.assertNotIn('external',self.m.jobs[self.key])
 async def test_authenticated_open_mints_exact_detached_viewer_and_core_stop_does_not_cancel(self):
  model=types.ModuleType('delivery_test.recording_model')
  exec(compile((PATH.parent/'recording_model.py').read_text(),'recording_model.py','exec'),model.__dict__)
  ident=model.clip_id(*self.key);scope='b'*32;job=dict(workload=self.key[0],node='deskb3x',id=ident,attempt='f'*32)
  waiter=asyncio.create_task(asyncio.Event().wait());self.tasks.append(waiter)
  self.source.jobs[self.key]=dict(job,task=waiter)
  self.source.owner=types.SimpleNamespace(enabled=lambda workload:True)
  async def resolve(item):
   self.m.bind(scope,self.key,job)
   return types.SimpleNamespace(url='/api/desk_cluster/recording/'+self.key[0]+'/deskb3x/'+ident+'/index.m3u8?authSig=legacy',mime_type='application/x-mpegURL')
  self.source.async_resolve_media=resolve
  base='/desk-camera-media/v1/deskb3x/'+'a'*64+'/'
  ns['bridge']=AsyncMock(return_value=dict(url=base+'index.m3u8',mime_type='application/x-mpegURL',touch_url=base+'touch',close_url=base+'close',viewer=scope,heartbeat_seconds=5,expires_in_seconds=7200))
  components=types.ModuleType('homeassistant.components');components.persistent_notification=Mock()
  with patch.dict(ns,{'__package__':'delivery_test'}),patch.dict(sys.modules,{'delivery_test.recording_model':model,'homeassistant.components':components}):
   result=await self.m.action('u',self.view())
  self.assertTrue(waiter.cancelled());components.persistent_notification.async_dismiss.assert_called_once_with(self.source.hass,'desk_camera_playback_'+ident)
  self.assertEqual(result['url'],base+'index.m3u8');self.assertEqual(result['delivery']['viewer'],scope)
  request=ns['bridge'].await_args
  self.assertEqual(request.args,('camera_delivery',));self.assertEqual({k:v for k,v in request.kwargs['value'].items() if k!='deadline'},dict(job,viewer=scope))
  self.assertTrue(time.time()<request.kwargs['value']['deadline']<time.time()+26)
  self.m.cancel=AsyncMock();await self.m.stop(None);self.m.cancel.assert_not_awaited()
if __name__=='__main__':unittest.main()
