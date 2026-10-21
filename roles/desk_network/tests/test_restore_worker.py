"""Test-only Python restore CLI, private stdin and parent allocation bounds."""
from contextlib import closing
import pathlib,sqlite3,subprocess,sys,tempfile,tracemalloc,unittest
from unittest.mock import patch
sys.path.insert(0,str(pathlib.Path(__file__).resolve().parents[1]/'files'))
import ha
import python_worker_reference as reference

class RestoreTests(unittest.TestCase):
 def setUp(self):
  self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup);self.root=pathlib.Path(self.temp.name)
  self.data=self.root/'data';(self.data/'db').mkdir(parents=True);(self.data/'config').mkdir();self.run=self.root/'run';self.run.mkdir()
  self.commands=[]
  def child(command,blob=None):
   self.commands.append(command)
   # Test host has no production uid20211. Only chown is stubbed in the child;
   # stdin parsing, CLI, decompression, validation and atomic writes run intact.
   wrapper='import os,sys,runpy;sys.path.insert(0,os.path.dirname(sys.argv[1]));os.chown=lambda *a:None;sys.argv=sys.argv[1:];runpy.run_path(sys.argv[0],run_name="__main__")'
   cmd=[sys.executable,'-c',wrapper,reference.__file__,'--snapshot-worker','restore']
   for flag in ('--run','--data'):cmd += [flag,command[command.index(flag)+1]]
   return subprocess.run(cmd,input=blob,stdout=subprocess.PIPE,stderr=subprocess.PIPE,check=True,timeout=20)
  p=patch.object(ha.checkpoint_budget,'execute',side_effect=child);p.start();self.addCleanup(p.stop)
 def blob(self,size=1):
  path=self.root/'source.db'
  with closing(sqlite3.connect(path)) as db:
   db.execute('CREATE TABLE bulk(data BLOB)');db.execute('INSERT INTO bulk VALUES (zeroblob(?))',(size,));db.commit()
  return ha.checkpoint_blob(path,b"ARPSCAN_RUN_SCHD='*/1 * * * *'\n",b'[]')
 def test_real_worker_restores_and_keeps_token_out_of_argv(self):
  token='private-fixture-credential';blob=self.blob()
  ha.restore_worker(blob,self.data,self.run,token)
  with closing(sqlite3.connect(self.data/'db/app.db')) as db:self.assertEqual(db.execute('SELECT length(data) FROM bulk').fetchone()[0],1)
  self.assertIn(token,(self.data/'config/app.conf').read_text());self.assertEqual((self.data/'config/workflows.json').read_bytes(),b'[]')
  self.assertNotIn(token,str(self.commands));self.assertIn('restore',self.commands[0]);self.assertEqual(list(self.run.iterdir()),[])
 def test_large_database_scratch_stays_out_of_parent(self):
  blob=self.blob(13_400_000);tracemalloc.start()
  try:
   ha.restore_worker(blob,self.data,self.run,'fixture');_,peak=tracemalloc.get_traced_memory()
  finally:tracemalloc.stop()
  self.assertLess(peak,4*1024*1024);self.assertEqual(list(self.run.iterdir()),[])
 def test_invalid_blob_fails_before_target_changes_and_cleans_scratch(self):
  (self.data/'db/app.db').write_bytes(b'keep-old')
  with self.assertRaises(subprocess.CalledProcessError):ha.restore_worker(b'bad-gzip',self.data,self.run,'fixture')
  self.assertEqual((self.data/'db/app.db').read_bytes(),b'keep-old');self.assertEqual(list(self.run.iterdir()),[])
 def test_invalid_credentials_fail_before_target_changes(self):
  blob=self.blob()
  with self.assertRaises(subprocess.CalledProcessError):ha.restore_worker(blob,self.data,self.run,'')
  self.assertFalse((self.data/'db/app.db').exists());self.assertEqual(list(self.run.iterdir()),[])
if __name__=='__main__':unittest.main()
