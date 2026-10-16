import pathlib,tempfile,sys,os,unittest
from unittest.mock import patch
sys.path.insert(0,str(pathlib.Path(__file__).resolve().parents[1]/'files'))
import cache_gc
class CacheTests(unittest.TestCase):
 def setUp(self):
  self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup);self.root=pathlib.Path(self.tmp.name);self.patcher=patch.object(cache_gc,'ROOT',self.root);self.patcher.start();self.addCleanup(self.patcher.stop)
 def file(self,name,at,size=10):
  p=self.root/name;p.parent.mkdir(parents=True,exist_ok=True);p.write_bytes(b'x'*size);os.utime(p,(at,at));return p
 def test_old_files_expire_but_active_jobs_and_other_files_are_retained(self):
  old=self.file('cache/cold/bed/videos/1-2.mp4',1);recent=self.file('cache/hot/entry/videos/ready.mp4',4000);other=self.file('cache/cold/bed/videos/user.txt',1)
  active=self.root/'playback/tapo_c222_160'/('a'*32);clip=self.file(str(active.relative_to(self.root))+'/ready.mp4',1)
  cache_gc.cleanup([active],now=4001);self.assertFalse(old.exists());self.assertTrue(recent.exists());self.assertTrue(other.exists());self.assertTrue(clip.exists())
 def test_quota_evicts_oldest_completed_file_and_does_not_follow_links(self):
  first=self.file('cache/cold/bed/videos/1-2.mp4',100);last=self.file('cache/cold/bed/videos/3-4.mp4',200)
  link=self.root/'cache/hot';link.parent.mkdir(exist_ok=True);link.symlink_to(self.root/'cache/cold',target_is_directory=True)
  result=cache_gc.cleanup(now=300,quota=10);self.assertFalse(first.exists());self.assertTrue(last.exists());self.assertEqual(result['bytes'],10)
if __name__=='__main__':unittest.main()
