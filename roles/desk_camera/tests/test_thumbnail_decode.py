import base64,hashlib,http.server,importlib.util,io,json,pathlib,secrets,subprocess,sys,tempfile,threading,time,types,unittest,urllib.error,urllib.request
from unittest.mock import Mock,patch
from PIL import Image
BASE=pathlib.Path(__file__).resolve().parents[2];sys.path.insert(0,str(BASE/'desk_camera/files'));sys.path.insert(0,str(BASE/'desk_homeassistant/files'));sys.modules.setdefault('nats',types.ModuleType('nats'))
import thumbnail_decode,controller,playback
from protocol import pack
b=io.BytesIO();Image.new('RGB',(32,18)).save(b,format='JPEG');JPEG=b.getvalue()
class DecodeTests(unittest.TestCase):
 def setUp(self):
  self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup);self.patch=patch.object(playback,'ROOT',pathlib.Path(self.temp.name).resolve());self.patch.start();self.addCleanup(self.patch.stop)
  self.agent=types.SimpleNamespace(node='deskb1x',members={'deskb1x':'127.0.0.1'},c={'workload':'tapo_c222_160','key':'test-key'},generation='a'*32,owned=lambda:True,ready=lambda:True,playback=types.SimpleNamespace(active=lambda:False))
  mem=patch.object(thumbnail_decode,'memory_ok',return_value=True);mem.start();self.addCleanup(mem.stop)
  self.scope={'workload':'tapo_c222_160','id':'b'*32,'source':'ts','generation':'a'*32,'digest':hashlib.sha256(b'TS').hexdigest()};self.decoder=thumbnail_decode.Decoder()
 def test_wrong_body_generation_and_busy_cannot_run_decoder(self):
  with patch.object(thumbnail_decode.subprocess,'run') as run:
   with self.assertRaises(ValueError):self.decoder.decode([self.agent],self.scope,b'wrong')
   self.agent.generation='c'*32
   with self.assertRaises(ValueError):self.decoder.decode([self.agent],self.scope,b'TS')
   self.assertTrue(all(c.args[0][1:3]==['rm','-f'] for c in run.call_args_list));self.assertFalse(self.decoder.lock.locked())
 def test_decoder_is_bounded_and_closes_container(self):
  def run(cmd,**kwargs):return types.SimpleNamespace(returncode=0,stdout=JPEG,stderr=b'{"elapsed_seconds":0.2}')
  with patch.object(thumbnail_decode.subprocess,'run',side_effect=run) as run:
   self.assertEqual(self.decoder.decode([self.agent],self.scope,b'TS'),JPEG)
   cmd=run.call_args_list[0].args[0];self.assertIn('--network=none',cmd);self.assertIn('--memory=256m',cmd);self.assertIn('--cpus=.25',cmd);self.assertEqual(cmd[0],'systemd-run');self.assertIn('--property=CPUQuota=25%',cmd);self.assertIn('--property=MemoryMax=320M',cmd);self.assertEqual(self.decoder.completed,1);self.assertEqual(run.call_args_list[-1].args[0][:3],['podman','rm','-f']);self.assertFalse(self.decoder.lock.locked())
 def test_timeout_always_releases_admission_lock(self):
  def run(cmd,**kwargs):
   if cmd[0]=='systemd-run':raise subprocess.TimeoutExpired(cmd,20)
   return types.SimpleNamespace(returncode=0)
  with patch.object(thumbnail_decode.subprocess,'run',side_effect=run):
   with self.assertRaises(ValueError):self.decoder.decode([self.agent],self.scope,b'TS')
   self.assertFalse(self.decoder.lock.locked())
 def test_post_requires_signed_exact_scope_before_read_or_decode(self):
  server=http.server.ThreadingHTTPServer(('127.0.0.1',0),controller.Handler);server.agents=[self.agent];server.core_artifact_peer=Mock();server.thumbnail_decoder=Mock();server.thumbnail_decoder.decode.return_value=JPEG
  threading.Thread(target=server.serve_forever,daemon=True).start();self.addCleanup(server.server_close);self.addCleanup(server.shutdown)
  url=f'http://127.0.0.1:{server.server_port}/thumbnail/tapo_c222_160/'+self.scope['id']
  token=lambda scope:base64.b64encode(pack({'sender':'deskb1x','recipient':'deskb1x','id':secrets.token_hex(16),'at':time.time(),'action':'thumbnail','value':scope},'test-key')).decode()
  for header in ('',token(dict(self.scope,id='d'*32))):
   with self.assertRaises(urllib.error.HTTPError) as error:urllib.request.urlopen(urllib.request.Request(url,data=b'TS',headers={'X-Desk-Artifact':header}))
   self.assertEqual(error.exception.code,404);error.exception.close()
  server.thumbnail_decoder.decode.assert_not_called()
  with urllib.request.urlopen(urllib.request.Request(url,data=b'TS',headers={'X-Desk-Artifact':token(self.scope)})) as r:self.assertEqual(r.status,200);self.assertEqual(r.read(),JPEG)
 def test_real_wrapper_decodes_only_one_synthetic_frame(self):
  data=subprocess.check_output(['ffmpeg','-hide_banner','-loglevel','error','-f','lavfi','-i','testsrc2=s=160x90:r=10','-t','1','-c:v','libx264','-threads','1','-f','mpegts','pipe:1'])
  p=subprocess.run([sys.executable,'-c',thumbnail_decode.WRAPPER],input=data,capture_output=True,timeout=15)
  self.assertEqual(p.returncode,0,p.stderr.decode()[:200]);self.assertEqual(Image.open(io.BytesIO(p.stdout)).format,'JPEG');self.assertIn('elapsed_seconds',json.loads(p.stderr))
if __name__=='__main__':unittest.main()
