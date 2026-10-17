import importlib.util,json,pathlib,sys,tempfile,types,unittest
from unittest.mock import Mock,patch
BASE=pathlib.Path(__file__).resolve().parents[1]/'files';sys.path.insert(0,str(BASE))
import hls_files,playback
spec=importlib.util.spec_from_file_location('hls_manifest',BASE.parents[1]/'desk_homeassistant/files/desk_cluster/hls_manifest.py');manifest=importlib.util.module_from_spec(spec);spec.loader.exec_module(manifest)
TEXT='#EXTM3U\n#EXT-X-MAP:URI="init.mp4"\n#EXTINF:2.0,\ns000000.m4s\n#EXTINF:2.0,\ns000001.m4s\n'
class HlsTests(unittest.TestCase):
 def setUp(self):
  self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup);self.root=pathlib.Path(self.tmp.name).resolve();self.gen='b'*32
 def create(self,base=None,complete=False):
  base=base or self.root
  for name in ('init.mp4','s000000.m4s','s000001.m4s','index.m3u8'):
   p=hls_files.artifact_path(base,self.gen,name);p.parent.mkdir(parents=True,exist_ok=True);p.write_bytes((TEXT+('#EXT-X-ENDLIST\n' if complete else '')).encode() if name=='index.m3u8' else b'x'*2048)
  return base
 def test_fixed_paths_and_manifest_reject_traversal_remote_uri_and_temporary_files(self):
  for name in ('../index.m3u8','s000000.m4s.tmp','https://outside/x','other.m3u8'):
   with self.assertRaises(ValueError):hls_files.artifact_path(self.root,self.gen,name)
  for text in (TEXT.replace('s000000.m4s','https://outside/x'),TEXT.replace('init.mp4','../private'),TEXT.replace('s000001.m4s','s000004.m4s')):
   with self.assertRaises(ValueError):hls_files.playlist(text)
 def test_each_manifest_uri_is_signed_and_playback_starts_at_the_beginning(self):
  sign=Mock(side_effect=lambda name:'/protected/'+name+'?signed='+name)
  text=manifest.rewrite(TEXT,sign)
  self.assertIn('TIME-OFFSET=0',text);self.assertIn('HOLD-BACK=1810.000',text);self.assertEqual(sign.call_count,3)
  self.assertIn('URI="/protected/init.mp4?signed=init.mp4"',text);self.assertIn('/protected/s000001.m4s?signed=s000001.m4s',text)
  self.assertNotIn('HOLD-BACK',manifest.rewrite(TEXT+'#EXT-X-ENDLIST\n',sign))
  with self.assertRaises(ValueError):manifest.rewrite(TEXT.replace('s000000.m4s','../secret'),sign)
 def test_complete_snapshot_requires_every_referenced_file_and_rejects_symlinks(self):
  self.create(complete=True);self.assertTrue(hls_files.snapshot(self.root,self.gen)['complete'])
  p=hls_files.artifact_path(self.root,self.gen,'s000001.m4s');p.unlink()
  with self.assertRaises(ValueError):hls_files.snapshot(self.root,self.gen)
  p.symlink_to('/etc/passwd')
  with self.assertRaises(ValueError):hls_files.snapshot(self.root,self.gen)
 def test_status_validation_reuses_only_the_same_playlist_for_one_second(self):
  self.create(complete=True);agent=types.SimpleNamespace(c={'workload':'tapo_c222_160'},node='deskb1x');p=playback.Playback(agent)
  with patch.object(hls_files,'snapshot',wraps=hls_files.snapshot) as scan,patch.object(playback.time,'monotonic',return_value=100) as clock:
   p.hls_snapshot('a'*32,self.root,self.gen);p.hls_snapshot('a'*32,self.root,self.gen);self.assertEqual(scan.call_count,1)
   index=hls_files.artifact_path(self.root,self.gen,'index.m3u8');index.write_text(index.read_text()+'#updated\n')
   p.hls_snapshot('a'*32,self.root,self.gen);self.assertEqual(scan.call_count,2)
   clock.return_value=102;p.hls_snapshot('a'*32,self.root,self.gen);self.assertEqual(scan.call_count,3)
   hls_files.artifact_path(self.root,self.gen,'s000001.m4s').unlink();clock.return_value=104
   with self.assertRaises(ValueError):p.hls_snapshot('a'*32,self.root,self.gen)
 def test_partial_hls_is_playable_only_while_its_original_job_is_running(self):
  agent=types.SimpleNamespace(c={'workload':'tapo_c222_160'},node='deskb1x');p=playback.Playback(agent);ident='a'*32
  with patch.object(playback,'ROOT',self.root):
   base=playback.clip_path(agent.c['workload'],ident).parent;self.create(base)
   (base/'progress.json').write_text(json.dumps({'state':'playable','format':'hls','generation':self.gen}));p.current=ident;p.task=Mock();p.task.done.return_value=False
   self.assertEqual(p.status(ident)['state'],'playable');p.task.done.return_value=True;self.assertEqual(p.status(ident)['state'],'failed')
   (base/'progress.json').write_text(json.dumps({'state':'ready','format':'hls','generation':self.gen}));self.create(base,complete=True);self.assertEqual(p.status(ident)['state'],'ready')
if __name__=='__main__':unittest.main()
