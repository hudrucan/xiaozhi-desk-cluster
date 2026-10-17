"""Synthetic offline integration of the actual worker pipeline; no camera login."""
import asyncio,contextlib,json,pathlib,shutil,subprocess,sys,tempfile,types,unittest
from unittest.mock import patch
BASE=pathlib.Path(__file__).resolve().parents[1]/'files';sys.path.insert(0,str(BASE))
import hls_worker,hls_files
class Session:
 def __init__(self,owner):self.owner=owner
 def set_window_size(self,n):pass
 async def __aenter__(self):return self
 async def __aexit__(self,*args):self.owner.closed=True
 async def transceive(self,*args,**kwargs):
  video=self.owner.video;audio=self.owner.audio
  for i in range(0,len(video),12500):
   j=(i//12500)*800
   await asyncio.sleep(.05)
   yield types.SimpleNamespace(mimetype='video/mp2t',plaintext=video[i:i+12500],audioPayload=audio[j:j+800],audioPayloadType=8)
  yield types.SimpleNamespace(mimetype='application/json',plaintext=json.dumps({'type':'notification','params':{'event_type':'stream_status','status':'finished'}}).encode())
class Downloader:
 def __init__(self,*a,**kw):pass
 def _buildRequest(self):return {'method':self.method}
@unittest.skipUnless(shutil.which('ffmpeg'),'ffmpeg required for local-only synthetic pipeline')
class Pipeline(unittest.IsolatedAsyncioTestCase):
 @classmethod
 def setUpClass(cls):
  cls.tmp=tempfile.TemporaryDirectory();root=pathlib.Path(cls.tmp.name);ff=shutil.which('ffmpeg')
  subprocess.run([ff,'-v','error','-y','-f','lavfi','-i','testsrc2=size=640x360:rate=10','-t','12','-an','-c:v','libx264','-preset','ultrafast','-g','20','-sc_threshold','0','-threads','1','-f','mpegts','-muxrate','1000000',str(root/'v.ts')],check=True)
  subprocess.run([ff,'-v','error','-y','-f','lavfi','-i','sine=frequency=440:sample_rate=8000','-t','12','-c:a','pcm_alaw','-f','alaw',str(root/'a.alaw')],check=True)
  cls.video=(root/'v.ts').read_bytes();cls.audio=(root/'a.alaw').read_bytes()
 @classmethod
 def tearDownClass(cls):cls.tmp.cleanup()
 async def run_pipeline(self,cancel=False):
  with tempfile.TemporaryDirectory() as tmp:
   root=pathlib.Path(tmp).resolve();gen='a'*32;states=[];early=asyncio.Event()
   owner=types.SimpleNamespace(video=self.video,audio=self.audio,closed=False,getAudioConfig=lambda:{'audio_config':{'microphone':{'sampling_rate':8}}});owner.getMediaSession=lambda _:Session(owner)
   modules={'pytapo':types.ModuleType('pytapo'),'pytapo.media_stream':types.ModuleType('pytapo.media_stream'),'pytapo.media_stream.downloader':types.SimpleNamespace(Downloader=Downloader),'pytapo.media_stream._utils':types.SimpleNamespace(StreamType=types.SimpleNamespace(Download='download')),'rtp':types.SimpleNamespace(PayloadType=types.SimpleNamespace(PCMU=0,PCMA=8))}
   def save(v):
    states.append(v)
    if v['state']=='playable':early.set()
   with patch.dict(sys.modules,modules):
    task=asyncio.create_task(hls_worker.package(owner,{'start':0,'end':12},0,root,gen,save))
    await asyncio.wait_for(early.wait(),15);self.assertFalse(task.done());self.assertTrue(states[-1]['has_audio'])
    if cancel:
     task.cancel()
     with self.assertRaises(asyncio.CancelledError):await task
    else:
     text=(root/'hls'/gen/'index.m3u8').read_text();snap=root/'hls'/gen/'preview.m3u8';snap.write_text(text+'\n#EXT-X-ENDLIST\n')
     def decode():return subprocess.run([shutil.which('ffmpeg'),'-v','error','-i',str(snap),'-t','1','-f','null','-'],capture_output=True,timeout=5)
     result=await asyncio.to_thread(decode);self.assertEqual(result.returncode,0,result.stderr.decode());self.assertFalse(task.done())
     await asyncio.wait_for(task,15);self.assertEqual(states[-1]['state'],'ready');self.assertTrue(hls_files.snapshot(root,gen)['complete'])
    self.assertTrue(owner.closed)
 async def test_real_hls_packager_decodes_audio_and_video_before_input_finishes(self):await self.run_pipeline()
 async def test_cancellation_closes_the_camera_session_and_worker_pipes(self):await self.run_pipeline(cancel=True)
if __name__=='__main__':unittest.main()
