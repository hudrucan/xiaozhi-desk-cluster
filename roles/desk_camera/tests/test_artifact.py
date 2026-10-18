import base64,hashlib,http.server,json,pathlib,secrets,sys,tempfile,threading,time,types,unittest,urllib.request,urllib.error
from unittest.mock import patch
BASE=pathlib.Path(__file__).resolve().parents[2];sys.path.insert(0,str(BASE/'desk_camera/files'));sys.path.insert(0,str(BASE/'desk_homeassistant/files'));sys.modules.setdefault('nats',types.ModuleType('nats'))
import controller,playback
from protocol import pack
class TestHandler(controller.Handler):
 def setup(self):
  super().setup()
  if getattr(self.server,'test_peer',None):self.client_address=(self.server.test_peer,self.client_address[1])
class ArtifactTests(unittest.TestCase):
 def setUp(self):
  self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup);self.patch=patch.object(playback,'ROOT',pathlib.Path(self.tmp.name).resolve());self.patch.start();self.addCleanup(self.patch.stop)
  self.workload='tapo_c222_160';self.ident='a'*32;p=playback.clip_path(self.workload,self.ident);p.parent.mkdir(parents=True);p.write_bytes(b'0'*2048)
  self.server=http.server.ThreadingHTTPServer(('127.0.0.1',0),TestHandler);self.server.agents=[types.SimpleNamespace(node='deskb1x',members={'deskb1x':'127.0.0.1'},c={'key':'test-key'})]
  self.server.core_artifact_peer=controller.CoreArtifactPeer()
  self.thread=threading.Thread(target=self.server.serve_forever,daemon=True);self.thread.start();self.addCleanup(self.server.server_close);self.addCleanup(self.server.shutdown)
  self.url='http://127.0.0.1:'+str(self.server.server_port)+'/artifact/'+self.workload+'/'+self.ident
 def token(self,**kwargs):
  v={'sender':'deskb1x','recipient':'deskb1x','id':secrets.token_hex(16),'at':time.time(),'action':'artifact','value':{'workload':self.workload,'id':self.ident}};v.update(kwargs);return base64.b64encode(pack(v,'test-key')).decode()
 def test_unsigned_wrong_scope_and_expired_requests_cannot_read_clip(self):
  for token in ('',self.token(value={'workload':self.workload,'id':'b'*32}),self.token(at=time.time()-20)):
   with self.assertRaises(urllib.error.HTTPError) as e:urllib.request.urlopen(urllib.request.Request(self.url,headers={'X-Desk-Artifact':token}))
   self.assertEqual(e.exception.code,404);e.exception.close()
 def test_signed_range_supports_browser_seeking_and_byte_bound(self):
  request=urllib.request.Request(self.url,headers={'X-Desk-Artifact':self.token(),'Range':'bytes=100-199'})
  with urllib.request.urlopen(request) as response:
   self.assertEqual(response.status,206);self.assertEqual(response.headers['Content-Range'],'bytes 100-199/2048');self.assertEqual(len(response.read()),100)
 def test_thumbnail_session_expiry_is_typed_only_after_authentication(self):
  from unittest.mock import Mock
  self.server.thumbnail_fetcher=types.SimpleNamespace(request=Mock(side_effect=playback.PlaybackError('session_expired')))
  data=b'{}';scope={'workload':self.workload,'id':self.ident,'source':'fetch','generation':'b'*32,'digest':hashlib.sha256(data).hexdigest()}
  url=self.url.replace('/artifact/','/thumbnail/')
  for token,status in (('',404),(self.token(action='thumbnail',value=scope),409)):
   with self.assertRaises(urllib.error.HTTPError) as err:urllib.request.urlopen(urllib.request.Request(url,data=data,headers={'X-Desk-Artifact':token}))
   self.assertEqual(err.exception.code,status);self.assertEqual(err.exception.headers.get('X-Desk-Media-Error'),'session_expired' if status==409 else None);err.exception.close()
 def test_only_exact_running_core_peer_can_read_signed_artifact(self):
  self.server.test_peer='10.88.0.18'
  result=types.SimpleNamespace(stdout=b'{"running":true,"networks":{"podman":{"IPAddress":"10.88.0.18"}}}')
  with patch.object(controller.subprocess,'run',return_value=result) as inspect:
   for _ in range(2):
    with urllib.request.urlopen(urllib.request.Request(self.url,headers={'X-Desk-Artifact':self.token(),'Range':'bytes=0-31'})) as response:self.assertEqual(response.status,206);self.assertEqual(len(response.read()),32)
   self.assertEqual(inspect.call_count,1)
   self.server.test_peer='10.88.0.19'
   with self.assertRaises(urllib.error.HTTPError) as error:urllib.request.urlopen(urllib.request.Request(self.url,headers={'X-Desk-Artifact':self.token()}))
   self.assertEqual(error.exception.code,404);error.exception.close()
 def test_unsigned_bridge_peer_never_inspects_core(self):
  self.server.test_peer='10.88.0.18'
  with patch.object(controller.subprocess,'run') as inspect:
   with self.assertRaises(urllib.error.HTTPError) as error:urllib.request.urlopen(self.url)
   self.assertEqual(error.exception.code,404);error.exception.close();inspect.assert_not_called()
 def test_stopped_or_uninspectable_core_fails_closed(self):
  for output in (b'{"running":false,"networks":{"podman":{"IPAddress":"10.88.0.18"}}}',b'{}',b'invalid'):
   peer=controller.CoreArtifactPeer()
   with patch.object(controller.subprocess,'run',return_value=types.SimpleNamespace(stdout=output)):self.assertFalse(peer.allows('10.88.0.18'))
 def test_hls_requests_require_the_exact_file_and_generation_scope(self):
  from hls_files import artifact_path
  agent=self.server.agents[0];agent.c['workload']=self.workload;agent.playback=playback.Playback(agent);agent.playback.current=self.ident
  from unittest.mock import Mock
  agent.playback.task=Mock();agent.playback.task.done.return_value=False
  base=playback.clip_path(self.workload,self.ident).parent;base.joinpath('ready.mp4').unlink();generation='b'*32
  text='#EXTM3U\n#EXT-X-MAP:URI="init.mp4"\n#EXTINF:2,\ns000000.m4s\n#EXTINF:2,\ns000001.m4s\n'
  for name in ('index.m3u8','init.mp4','s000000.m4s','s000001.m4s'):
   p=artifact_path(base,generation,name);p.parent.mkdir(parents=True,exist_ok=True);p.write_bytes(text.encode() if name=='index.m3u8' else b'x'*2048)
  (base/'progress.json').write_text(json.dumps({'state':'playable','format':'hls','generation':generation}))
  url=self.url.replace('/artifact/','/hls/')+'/'+generation+'/index.m3u8'
  scope={'workload':self.workload,'id':self.ident,'generation':generation,'file':'index.m3u8'}
  with urllib.request.urlopen(urllib.request.Request(url,headers={'X-Desk-Artifact':self.token(value=scope)})) as response:self.assertEqual(response.status,200);self.assertEqual(response.read().decode(),text)
  for token in (self.token(),self.token(value=dict(scope,file='init.mp4')),self.token(value=dict(scope,generation='c'*32))):
   with self.assertRaises(urllib.error.HTTPError) as error:urllib.request.urlopen(urllib.request.Request(url,headers={'X-Desk-Artifact':token}))
   self.assertEqual(error.exception.code,404);error.exception.close()
if __name__=='__main__':unittest.main()
