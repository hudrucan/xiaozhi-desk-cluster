"""Detached viewers: exact media authority, expiry, retries, refs and HTTP bounds."""
import asyncio,copy,http.server,json,pathlib,sys,tempfile,threading,types,unittest,urllib.error,urllib.request
from unittest.mock import patch
sys.path.insert(0,str(pathlib.Path(__file__).resolve().parents[1]/'files'))
import playback
from media_delivery import Delivery,manifest,serve

class DeliveryTests(unittest.TestCase):
 def setUp(self):
  self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup);self.root=pathlib.Path(self.tmp.name).resolve()
  self.patch=patch.object(playback,'ROOT',self.root);self.patch.start();self.addCleanup(self.patch.stop)
  self.authority=patch('recording_owner.owner_mode',return_value=True);self.authority.start();self.addCleanup(self.authority.stop)
  self.now=100.;self.wall=1792200000.;self.running=True;self.owned=True
  self.p=types.SimpleNamespace(current='a'*32,attempt='b'*32,viewer_until=130.,stop_requested=False,active=lambda:self.running)
  self.status={'state':'playable','format':'hls','generation':'d'*32};self.p.status=lambda ident:copy.deepcopy(self.status)
  self.a=types.SimpleNamespace(node='deskb1x',c={'workload':'tapo_c222_163'},generation='c'*32,owned=lambda:self.owned,ready=lambda:True,playback=self.p,members={'deskb1x':'127.0.0.1'})
  self.d=Delivery([self.a],mono=lambda:self.now,wall=lambda:self.wall)
  self.value={'workload':'tapo_c222_163','id':'a'*32,'attempt':'b'*32,'viewer':'e'*32,'deadline':self.wall+25}
 def issue(self,value=None):
  reply=self.d.issue('deskb3x',value or self.value);cap=reply['url'].split('/')[4];return cap,reply
 def test_issue_retry_is_exact_and_never_constructs_sdk_or_alters_owner_lease(self):
  cap,reply=self.issue();self.assertEqual(self.d.issue('deskb3x',self.value),reply);self.assertEqual(len(self.d.grants),1)
  self.assertEqual(self.p.viewer_until,120);self.assertEqual(self.a.generation,'c'*32)
  for change in ({'attempt':'f'*32},{'deadline':self.wall+24}):
   with self.assertRaises(ValueError):self.issue(dict(self.value,**change))
  self.assertEqual(self.d.access(cap,'init.mp4')[0],self.root/'tapo_c222_163'/('a'*32)/'hls'/('d'*32)/'init.mp4')
 def test_capability_is_opaque_and_only_exact_media_names_are_accepted(self):
  cap,reply=self.issue();self.assertEqual(len(cap),64);self.assertNotIn('user_id',str(reply));self.assertNotIn('password',str(reply))
  for name in ('../ready.mp4','index.m3u8?x=1','s000000.m4s/../../secret','clip.mp4','/etc/passwd'):
   with self.assertRaises(ValueError):self.d.access(cap,name)
  for bad in ('f'*64,cap[:-1],'../'+cap):
   with self.assertRaises(ValueError):self.d.access(bad,'index.m3u8')
 def test_touch_does_not_resurrect_expiry_and_reads_never_renew(self):
  cap,_=self.issue();self.now=119;self.d.access(cap,'index.m3u8');self.assertEqual(self.d.grants[cap]['until'],120)
  self.now=121
  with self.assertRaises(ValueError):self.d.action(cap,'touch')
  with self.assertRaises(ValueError):self.d.access(cap,'index.m3u8')
  self.d.maintain(self.a);self.assertTrue(self.p.stop_requested)
 def test_last_close_waits_for_actual_worker_cleanup_and_late_touch_cannot_reopen(self):
  cap,_=self.issue();self.assertEqual(self.d.action(cap,'close')['state'],'cancelling');self.assertTrue(self.p.stop_requested)
  self.assertEqual(self.d.action(cap,'close')['state'],'cancelling')
  with self.assertRaises(ValueError):self.d.action(cap,'touch')
  self.running=False;self.assertTrue(self.d.action(cap,'close')['closed'])
  with self.assertRaises(ValueError):self.issue()
 def test_two_viewers_share_attempt_and_only_last_close_stops_it(self):
  first,_=self.issue();second,_=self.issue(dict(self.value,viewer='f'*32))
  self.assertTrue(self.d.action(first,'close')['closed']);self.assertFalse(self.p.stop_requested)
  self.now=110;self.d.action(second,'touch');self.assertEqual(self.p.viewer_until,130)
  self.assertFalse(self.d.action(second,'close')['closed']);self.assertTrue(self.p.stop_requested)
 def test_old_attempt_cancel_does_not_affect_a_new_preparation(self):
  cap,_=self.issue();self.p.attempt='f'*32;self.p.viewer_until=140
  self.assertTrue(self.d.action(cap,'close')['closed']);self.assertFalse(self.p.stop_requested);self.assertEqual(self.p.viewer_until,140)
  with self.assertRaises(ValueError):self.d.access(cap,'index.m3u8')
 def test_owner_generation_or_media_generation_change_revokes_running_reads(self):
  cap,_=self.issue();self.a.generation='f'*32
  with self.assertRaises(ValueError):self.d.access(cap,'init.mp4')
  self.a.generation='c'*32;self.status['generation']='f'*32
  with self.assertRaises(ValueError):self.d.action(cap,'touch')
 def test_completed_cache_survives_core_and_camera_owner_transfer_without_new_sdk(self):
  cap,_=self.issue();self.running=False;self.status['state']='ready';self.a.generation='f'*32;self.owned=False
  self.now=110;self.assertTrue(self.d.action(cap,'touch')['active']);self.d.access(cap,'index.m3u8')
  self.assertFalse(self.p.stop_requested)
 def test_controller_restart_revokes_all_previous_capabilities(self):
  cap,_=self.issue();replacement=Delivery([self.a],mono=lambda:self.now,wall=lambda:self.wall)
  with self.assertRaises(ValueError):replacement.access(cap,'index.m3u8')
 def test_new_core_waiter_cannot_cancel_or_renew_detached_viewers(self):
  cap,_=self.issue();self.now=110
  self.assertEqual(self.d.core_action(self.a,'a'*32,'b'*32,True),{'state':'cancelled'})
  self.assertFalse(self.p.stop_requested);self.assertEqual(self.p.viewer_until,120)
  self.assertEqual(self.d.core_action(self.a,'a'*32,'b'*32),{'state':'active'})
  self.assertEqual(self.p.viewer_until,120)
  self.now=121
  self.assertEqual(self.d.core_action(self.a,'a'*32,'b'*32,True),{'state':'cancelling'});self.assertTrue(self.p.stop_requested)
  self.running=False
  self.assertEqual(self.d.core_action(self.a,'a'*32,'b'*32,True),{'state':'cancelled'})
 def test_unrelated_or_unmanaged_core_waiter_uses_original_lifecycle(self):
  self.assertIsNone(self.d.core_action(self.a,'a'*32,'b'*32,True))
  self.issue();self.assertIsNone(self.d.core_action(self.a,'a'*32,'f'*32,True))
 def test_live_completed_viewer_pins_cache_only_until_its_heartbeat_expires(self):
  cap,_=self.issue();self.running=False
  self.assertEqual(self.d.active_paths(),{playback.clip_path(self.value['workload'],self.value['id']).parent})
  self.now=121;self.assertFalse(self.d.active_paths())
 def test_absolute_expiry_capacity_and_closed_tombstone_are_bounded(self):
  self.d.capacity=1;cap,_=self.issue()
  with self.assertRaises(ValueError):self.issue(dict(self.value,viewer='f'*32))
  self.d.action(cap,'close');self.running=False;self.now+=31;self.d.prune();self.assertFalse(self.d.grants);self.assertFalse(self.d.requests)
  self.p.stop_requested=False;self.issue(dict(self.value,viewer='f'*32));self.now+=7201
  with self.assertRaises(ValueError):self.d.action(next(iter(self.d.grants)),'touch')
 def test_unsigned_sender_unselected_camera_and_wrong_attempt_cannot_issue(self):
  with self.assertRaises(ValueError):self.d.issue('browser',self.value)
  with self.assertRaises(ValueError):self.issue(dict(self.value,attempt='f'*32))
  with patch('recording_owner.owner_mode',return_value=False):
   with self.assertRaises(ValueError):self.issue()
  self.assertFalse(self.d.grants)
 def test_cached_mp4_file_replacement_invalidates_grant(self):
  self.running=False;self.status={'state':'ready'};p=playback.clip_path(self.value['workload'],self.value['id']);p.parent.mkdir(parents=True);p.write_bytes(b'x'*2048)
  cap,_=self.issue(dict(self.value,attempt=None));self.d.access(cap,'clip.mp4');p.unlink();p.write_bytes(b'y'*2048)
  with self.assertRaises(ValueError):self.d.access(cap,'clip.mp4')
 def test_manifest_keeps_dvr_start_and_rejects_external_uri(self):
  raw='#EXTM3U\n#EXT-X-MAP:URI="init.mp4"\n#EXTINF:2,\ns000000.m4s\n'
  text=manifest(raw,'/cap/');self.assertIn('#EXT-X-START:TIME-OFFSET=0',text);self.assertIn('HOLD-BACK=1810',text);self.assertIn('URI="/cap/init.mp4"',text)
  for bad in (raw.replace('init.mp4','https://other/secret'),raw.replace('s000000.m4s','../secret'),raw+'#EXT-X-KEY:METHOD=AES-128,URI="secret"\n'):
   with self.assertRaises(ValueError):manifest(bad,'/cap/')

class HTTPTests(unittest.IsolatedAsyncioTestCase):
 async def asyncSetUp(self):
  self.base=DeliveryTests();self.base.setUp();self.addCleanup(self.base.doCleanups)
  self.d=self.base.d;self.cap,self.grant=self.base.issue();p=playback.clip_path(self.base.value['workload'],self.base.value['id']).parent/'hls'/('d'*32);p.mkdir(parents=True)
  self.raw='#EXTM3U\n#EXT-X-MAP:URI="init.mp4"\n#EXTINF:2,\ns000000.m4s\n'
  (p/'index.m3u8').write_text(self.raw);(p/'init.mp4').write_bytes(b'x'*2048);(p/'s000000.m4s').write_bytes(b'y'*4096)
  class Handler(http.server.BaseHTTPRequestHandler):
   def log_message(self,*args):pass
   def do_GET(self):serve(self)
   def do_POST(self):serve(self,post=True)
  self.server=http.server.ThreadingHTTPServer(('127.0.0.1',0),Handler);self.server.loop=asyncio.get_running_loop();self.server.agents=[self.base.a];self.server.delivery=self.d;self.server.delivery_slots=threading.BoundedSemaphore(8)
  self.thread=threading.Thread(target=self.server.serve_forever,daemon=True);self.thread.start()
  self.origin='http://127.0.0.1:'+str(self.server.server_port);self.url=self.origin+self.grant['url']
 async def asyncTearDown(self):
  await asyncio.to_thread(self.server.shutdown);self.server.server_close()
 async def fetch(self,path=None,method='GET',headers=None,data=None):
  def read():
   request=urllib.request.Request(path or self.url,method=method,headers=headers or {},data=data)
   try:
    with urllib.request.urlopen(request,timeout=4) as response:return response.status,dict(response.headers),response.read()
   except urllib.error.HTTPError as e:
    with e:return e.code,dict(e.headers),e.read()
  return await asyncio.to_thread(read)
 async def test_real_http_playlist_and_ranges_work_without_core(self):
  code,headers,body=await self.fetch();self.assertEqual(code,200);self.assertIn((self.d.base(self.cap)+'init.mp4').encode(),body);self.assertEqual(headers['Cache-Control'],'private, no-store')
  code,headers,body=await self.fetch(self.url.replace('index.m3u8','s000000.m4s'),headers={'Range':'bytes=100-199'})
  self.assertEqual(code,206);self.assertEqual(len(body),100);self.assertEqual(headers['Content-Range'],'bytes 100-199/4096')
 async def test_unknown_token_traversal_wrong_node_and_query_are_rejected(self):
  for url in (self.url.replace(self.cap,'f'*64),self.url.replace('deskb1x','deskb2x'),self.url+'?x=1',self.url.replace('index.m3u8','../ready.mp4')):
   self.assertEqual((await self.fetch(url))[0],404)
 async def test_only_empty_same_origin_post_can_renew_and_close(self):
  touch=self.origin+self.grant['touch_url'];close=self.origin+self.grant['close_url']
  self.assertEqual((await self.fetch(touch,method='GET'))[0],404)
  self.assertEqual((await self.fetch(touch,method='POST',data=b'x'))[0],404)
  self.assertEqual((await self.fetch(touch,method='POST',data=b'',headers={'Origin':'http://other'}))[0],404)
  self.assertEqual((await self.fetch(touch,method='POST',data=b'',headers={'Origin':self.origin}))[0],200)
  code,_,body=await self.fetch(close,method='POST',data=b'');self.assertEqual(code,200);self.assertEqual(json.loads(body)['state'],'cancelling')
  self.assertEqual((await self.fetch())[0],404);self.assertTrue(self.base.p.stop_requested)
 async def test_no_symlink_or_multi_range_escape(self):
  path=playback.clip_path(self.base.value['workload'],self.base.value['id']).parent/'hls'/('d'*32)/'init.mp4';path.unlink();path.symlink_to('/etc/hosts')
  self.assertEqual((await self.fetch(self.url.replace('index.m3u8','init.mp4')))[0],404)
  self.assertEqual((await self.fetch(self.url.replace('index.m3u8','s000000.m4s'),headers={'Range':'bytes=1-2,4-6'}))[0],404)

if __name__=='__main__':unittest.main()
