import importlib.util,json,pathlib,tempfile,threading,time,unittest
from unittest.mock import patch,MagicMock
FILES=pathlib.Path(__file__).resolve().parents[1]/'files'
if not (FILES/'warm_runtime.py').exists():FILES=pathlib.Path(__file__).parent
spec=importlib.util.spec_from_file_location('warm_runtime',FILES/'warm_runtime.py');w=importlib.util.module_from_spec(spec);spec.loader.exec_module(w)
spec=importlib.util.spec_from_file_location('warm_ha',FILES/'ha.py');ha=importlib.util.module_from_spec(spec);spec.loader.exec_module(ha)
class Warm(unittest.TestCase):
 def test_control_permissions_are_ready_before_atomic_publication(self):
  with tempfile.TemporaryDirectory() as td:
   c=object.__new__(ha.Cluster);c.warm_path=pathlib.Path(td)/'warm';c.generation='a'*32;c.until=100;replace=ha.os.replace;seen=[]
   def published(source,destination):
    seen.append(pathlib.Path(source).stat().st_mode & 0o777);replace(source,destination)
   with patch.object(ha.os,'replace',side_effect=published):c.warm_control(True)
   self.assertEqual(seen,[0o644]);self.assertEqual(json.loads((c.warm_path/'control.json').read_text())['until'],100)
 def test_lease_deadline_rejects_expired_invalid_and_future(self):
  with tempfile.TemporaryDirectory() as td:
   p=pathlib.Path(td)/'control';gen='a'*32
   for until,expected in [(109,gen),(100,None),(99,None),(112,None),(float('nan'),None),(float('inf'),None),('109',None),(True,None)]:
    p.write_text(json.dumps({'generation':gen,'until':until}));self.assertEqual(w.request(p,100),expected)
   for value in ({},{'generation':'shell','until':109},{'generation':gen,'until':109,'junk':'a'*2000}):
    p.write_text(json.dumps(value));self.assertIsNone(w.request(p,100))
   p.unlink();self.assertIsNone(w.request(p,100))
 def test_pristine_rejects_live_database_or_multiple_threads(self):
  with tempfile.TemporaryDirectory() as td:
   proc=pathlib.Path(td);(proc/'self/task/1').mkdir(parents=True);(proc/'self/fd').mkdir()
   w.assert_pristine(proc)
   (proc/'self/fd/9').symlink_to('/data/db/app.db');self.assertRaises(RuntimeError,w.assert_pristine,proc)
   (proc/'self/fd/9').unlink();(proc/'self/task/2').mkdir();self.assertRaises(RuntimeError,w.assert_pristine,proc)
 def test_active_health_cannot_reuse_old_generation_or_inactive_cache(self):
  c=object.__new__(ha.Cluster);c.warm=True;c.generation='a'*32;c.warm_state=lambda:{'state':'inactive','generation':'a'*32};c.opener=MagicMock()
  self.assertFalse(c.native_health());c.opener.open.assert_not_called()
  c.warm_state=lambda:{'state':'active','generation':'b'*32};self.assertFalse(c.native_health());c.opener.open.assert_not_called()
 def test_deactivate_waits_for_all_writer_groups_and_keeps_warm_container(self):
  c=object.__new__(ha.Cluster);c.until=200;c.ready=True;c.scan_queued_at=100;c.scan_cache_at=100;c.generation='a'*32;c.scan_lock=threading.Lock();c.warm_control=MagicMock();c.running=True;c.pid=(1,'start')
  c.warm_state=MagicMock(side_effect=[{'state':'inactive','groups':[42]},{'state':'inactive','groups':[]}])
  self.assertTrue(c.deactivate_warm());self.assertEqual(c.until,0);self.assertFalse(c.ready);self.assertTrue(c.running);self.assertEqual(c.pid,(1,'start'));self.assertIsNone(c.generation);self.assertEqual(c.scan_queued_at,0)
 def test_activate_restores_before_authorizing_and_refuses_running_child(self):
  c=object.__new__(ha.Cluster);c.owned=lambda:True;c.warm_state=lambda:{'state':'inactive'};c.store=MagicMock();c.store.head={'digest':'test'};c.root=pathlib.Path('/data');c.run=pathlib.Path('/run');c.c={'api_token':'fake'};c.warm_control=MagicMock()
  order=[]
  with patch.object(ha,'restore',side_effect=lambda *args:order.append('restore')):
   c.warm_control.side_effect=lambda *args:order.append('authorize');c.activate_warm()
  self.assertEqual(order,['restore','authorize']);self.assertRegex(c.generation,'^[a-f0-9]{32}$')
  c.warm_state=lambda:{'state':'active'}
  with patch.object(ha,'restore') as restore:
   self.assertRaises(ha.Unavailable,c.activate_warm);restore.assert_not_called()
 def test_lease_lost_during_restore_never_authorizes(self):
  c=object.__new__(ha.Cluster);c.owned=MagicMock(side_effect=[True,False]);c.warm_state=lambda:{'state':'inactive'};c.store=MagicMock();c.store.head={'digest':'test'};c.root=pathlib.Path('/data');c.run=pathlib.Path('/run');c.c={'api_token':'fake'};c.warm_control=MagicMock()
  with patch.object(ha,'restore'):self.assertRaises(ha.Unavailable,c.activate_warm)
  c.warm_control.assert_not_called()
 def test_kill_preserves_only_acknowledged_inactive_runtime(self):
  c=object.__new__(ha.Cluster);c.warm=True;c.running=True;c.stop=threading.Event();c.oplock=threading.RLock();c.deactivate_warm=MagicMock(return_value=True);c.budget=MagicMock();c.ready=True;c.until=100
  c.kill_container();c.budget.settle.assert_not_called();self.assertTrue(c.running);self.assertEqual(c.until,0);self.assertFalse(c.ready)
 def test_expired_or_changed_term_cannot_be_reauthorized(self):
  c=object.__new__(ha.Cluster);c.oplock=threading.RLock();c.warm=True;c.warm_control=MagicMock();c.term=[8,'node'];c.until=0;c.owned=lambda:False
  self.assertFalse(c.renew(100,[8,'node']));c.warm_control.assert_not_called()
  c.owned=lambda:True
  self.assertFalse(c.renew(100,[7,'node']));c.warm_control.assert_not_called()
 def test_failed_inactive_ack_destroys_entire_namespace(self):
  with tempfile.TemporaryDirectory() as td:
   c=object.__new__(ha.Cluster);c.warm=True;c.running=True;c.stop=threading.Event();c.oplock=threading.RLock();c.deactivate_warm=MagicMock(return_value=False);c.budget=MagicMock();c.ready=True;c.until=100;c.scan_lock=threading.Lock();c.pid=None;c.run=pathlib.Path(td)
   c.command=MagicMock(return_value=type('Result',(),{'stdout':'[{"State":{"Pid":0}}]'})())
   c.kill_container();self.assertFalse(c.running);self.assertIsNone(c.pid);c.budget.settle.assert_called_once_with(True);c.command.assert_any_call('podman','rm','-f',ha.NAME,timeout=2)
 def test_launch_refuses_fork_if_preload_is_not_pristine(self):
  with patch.object(w,'assert_pristine',side_effect=RuntimeError('unsafe')),patch.object(w.os,'fork') as fork:
   self.assertRaises(RuntimeError,w.launch,lambda:None);fork.assert_not_called()
if __name__=='__main__':unittest.main()
