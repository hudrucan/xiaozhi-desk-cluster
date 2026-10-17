"""Periodic skip is read-only and never survives a changed source or term."""
import importlib.util,pathlib,sqlite3,tempfile,threading,unittest
from unittest.mock import MagicMock,patch
FILES=pathlib.Path(__file__).resolve().parents[1]/'files'
spec=importlib.util.spec_from_file_location('source',FILES/'checkpoint_source.py');m=importlib.util.module_from_spec(spec);spec.loader.exec_module(m)
spec=importlib.util.spec_from_file_location('source_ha',FILES/'ha.py');ha=importlib.util.module_from_spec(spec);spec.loader.exec_module(ha)
class SourceTests(unittest.TestCase):
 def setUp(self):
  self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup);self.root=pathlib.Path(self.tmp.name)
  (self.root/'db').mkdir();(self.root/'config').mkdir();(self.root/'config/app.conf').write_text('scan=1')
  self.db=sqlite3.connect(self.root/'db/app.db',isolation_level=None);self.addCleanup(self.db.close)
  self.db.execute('PRAGMA journal_mode=WAL');self.db.execute('CREATE TABLE items(value)')
  self.source=m.CheckpointSource(self.root);self.addCleanup(self.source.close)
 def accept(self):
  stamp=self.source.stamp();self.source.accept([1,'a'],stamp,'digest');return stamp
 def test_unchanged_source_reuses_only_same_term_and_committed_digest(self):
  stamp=self.accept();self.assertTrue(self.source.unchanged([1,'a'],self.source.stamp(),'digest'))
  self.assertFalse(self.source.unchanged([2,'a'],stamp,'digest'));self.assertFalse(self.source.unchanged([1,'a'],stamp,'other'))
  with self.assertRaises(sqlite3.OperationalError):self.source.connection.execute('INSERT INTO items VALUES (1)')
 def test_wal_commit_config_and_workflow_edits_each_invalidate(self):
  for change in [lambda:self.db.execute('INSERT INTO items VALUES (1)'),lambda:(self.root/'config/app.conf').write_text('scan=2'),lambda:(self.root/'config/workflows.json').write_text('[]'),lambda:(self.root/'config/workflows.json').unlink()]:
   self.accept();change();self.assertFalse(self.source.unchanged([1,'a'],self.source.stamp(),'digest'))
 def test_write_during_checkpoint_cannot_be_marked_unchanged(self):
  before=self.source.stamp();self.db.execute('INSERT INTO items VALUES (1)');self.source.accept([1,'a'],before,'digest');self.assertIsNone(self.source.accepted)
 def test_database_replacement_resets_connection_and_cache(self):
  self.accept();replacement=self.root/'new.db'
  with sqlite3.connect(replacement) as db:db.execute('CREATE TABLE other(value)')
  replacement.replace(self.root/'db/app.db');self.source.stamp();self.assertIsNone(self.source.accepted)
 def test_retired_source_cannot_be_reopened_by_a_late_job(self):
  self.accept();connection=self.source.connection;self.source.close()
  with self.assertRaises(sqlite3.ProgrammingError):connection.execute('SELECT 1')
  with self.assertRaises(RuntimeError):self.source.stamp()
  with self.assertRaises(RuntimeError):self.source.accept([1,'a'],None,'digest')
  self.assertIsNone(self.source.connection)
 def test_fencing_closes_detector_before_native_shutdown(self):
  c=ha.Cluster.__new__(ha.Cluster);c.oplock=threading.RLock();c._checkpoint_source=self.source
  self.accept();connection=self.source.connection
  c.warm=True;c.running=True;c.stop=threading.Event();c.budget=MagicMock();c.until=110;c.ready=True
  def stopped():
   with self.assertRaises(sqlite3.ProgrammingError):connection.execute('SELECT 1')
   self.assertIsNone(c._checkpoint_source);return True
  c.deactivate_warm=stopped;c.kill_container();self.assertEqual(c.until,0)
 def test_only_periodic_snapshot_can_skip_and_force_still_runs_worker(self):
  c=ha.Cluster.__new__(ha.Cluster);c.snaplock=threading.Lock();c.oplock=threading.RLock();c.term=[1,'a'];c.owned=lambda:True
  c.root=self.root.parent;c.run=self.root/'run';c._checkpoint_source=self.source;c.store=MagicMock();c.store.head={'digest':ha.sha(b'blob')}
  c._checkpoint_source.accept(c.term,self.source.stamp(),c.store.head['digest'])
  with patch.object(ha,'snapshot_worker',return_value=b'blob') as worker:
   c.snapshot(force=False);worker.assert_not_called()
   c.snapshot();worker.assert_called_once()
 def test_probe_racing_fencing_closes_its_captured_handle(self):
  c=ha.Cluster.__new__(ha.Cluster);c.snaplock=threading.Lock();c.oplock=threading.RLock();c.term=[1,'a'];c.until=110
  c.owned=lambda:c.until>100;c.root=self.root.parent;c.run=self.root/'run';c._checkpoint_source=self.source
  stamp=self.source.stamp
  def fenced():
   result=stamp();c.until=0;c._checkpoint_source=None;return result
  with patch.object(self.source,'stamp',side_effect=fenced),patch.object(ha,'snapshot_worker') as worker:
   with self.assertRaises(ha.Unavailable):c.snapshot(force=False)
  self.assertTrue(self.source.closed);self.assertIsNone(self.source.connection);worker.assert_not_called()
if __name__=='__main__':unittest.main()
