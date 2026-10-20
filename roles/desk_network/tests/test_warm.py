import importlib.util,json,pathlib,tempfile,threading,time,unittest,os,subprocess,sys
from types import SimpleNamespace
from unittest.mock import patch,MagicMock
FILES=pathlib.Path(__file__).resolve().parents[1]/'files'
if not (FILES/'warm_runtime.py').exists():FILES=pathlib.Path(__file__).parent
spec=importlib.util.spec_from_file_location('warm_runtime',FILES/'warm_runtime.py');w=importlib.util.module_from_spec(spec);spec.loader.exec_module(w)
spec=importlib.util.spec_from_file_location('warm_ha',FILES/'ha.py');ha=importlib.util.module_from_spec(spec);spec.loader.exec_module(ha)
class Warm(unittest.TestCase):
 def test_direct_child_observation_uses_nonreaping_waitid_without_proc_scan(self):
  system=SimpleNamespace(P_PID=1,WEXITED=4,WNOHANG=1,WNOWAIT=0x1000000,
   waitid=MagicMock(side_effect=[None,SimpleNamespace(si_pid=123),ChildProcessError()]))
  with patch.object(w,'os',system),patch.object(w,'live_group',side_effect=AssertionError('active loop must not scan proc')):
   self.assertTrue(w.child_running(123))
   self.assertFalse(w.child_running(123))
   self.assertFalse(w.child_running(123))
  self.assertEqual(system.waitid.call_count,3)
  for call in system.waitid.call_args_list:
   self.assertEqual(call.args,(system.P_PID,123,system.WEXITED|system.WNOHANG|system.WNOWAIT))
 @unittest.skipUnless(all(hasattr(os,name) for name in ('waitid','P_PID','WEXITED','WNOHANG','WNOWAIT')),
  'Real child observation requires waitid with WNOWAIT; run on Linux')
 def test_direct_child_observation_keeps_exit_status_and_never_scans_proc(self):
  child=subprocess.Popen([sys.executable,'-c','import time;time.sleep(10)'])
  try:
   with patch.object(w,'live_group',side_effect=AssertionError('active loop must not scan proc')):
    self.assertTrue(w.child_running(child.pid))
    child.terminate()
    deadline=time.monotonic()+5
    while w.child_running(child.pid) and time.monotonic()<deadline:time.sleep(.01)
    self.assertFalse(w.child_running(child.pid))
   self.assertEqual(child.wait(),-15)
   self.assertFalse(w.child_running(child.pid))
  finally:
   if child.poll() is None:child.kill();child.wait()
 def test_stop_checks_all_descendant_groups_after_killing_leaders(self):
  with patch.object(w.os,'killpg') as kill,patch.object(w,'reap'),patch.object(w,'live_group',side_effect=[True,False,False]),patch.object(w.time,'sleep'):
   w.stop([10,11])
   self.assertEqual(kill.call_count,2)
   self.assertEqual(w.live_group.call_count,3)
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
 def test_control_reader_matches_request_for_every_deadline_case(self):
  with tempfile.TemporaryDirectory() as td:
   p=pathlib.Path(td)/'control';gen='a'*32;tick=[0.]
   def fresh():return w.ControlReader(p,monotonic=lambda:tick[0])
   for until,expected in [(109,gen),(100,None),(99,None),(112,None),(float('nan'),None),(float('inf'),None),('109',None),(True,None)]:
    p.write_text(json.dumps({'generation':gen,'until':until}));self.assertEqual(fresh()(100),expected);self.assertEqual(fresh()(100),w.request(p,100))
   for value in ({},{'generation':'shell','until':109},{'generation':gen,'until':109,'junk':'a'*2000}):
    p.write_text(json.dumps(value));self.assertIsNone(fresh()(100));self.assertEqual(fresh()(100),w.request(p,100))
   p.write_text('not json');self.assertIsNone(fresh()(100))
   p.unlink();self.assertIsNone(fresh()(100))
 def test_control_reader_skips_unchanged_file_but_never_caches_the_deadline(self):
  with tempfile.TemporaryDirectory() as td:
   p=pathlib.Path(td)/'control';gen='b'*32;tick=[0.]
   p.write_text(json.dumps({'generation':gen,'until':109}))
   read=w.ControlReader(p,monotonic=lambda:tick[0])
   with patch.object(pathlib.Path,'read_text',autospec=True,side_effect=pathlib.Path.read_text) as spy:
    self.assertEqual(read(100),gen)
    for now in (100.1,105,108.9):tick[0]+=.1;self.assertEqual(read(now),gen)
    self.assertEqual(spy.call_count,1)
    # The cached deadline is judged against the supplied clock on every call.
    self.assertIsNone(read(109));self.assertIsNone(read(120));self.assertEqual(spy.call_count,1)
 def test_control_reader_reparses_on_change_age_and_replacement(self):
  with tempfile.TemporaryDirectory() as td:
   p=pathlib.Path(td)/'control';a,b='a'*32,'b'*32;tick=[0.]
   p.write_text(json.dumps({'generation':a,'until':109}))
   read=w.ControlReader(p,monotonic=lambda:tick[0])
   self.assertEqual(read(100),a)
   # Atomic replacement (the host's write path) is a new inode: same size and content length, new generation.
   tmp=p.with_name('control.tmp');tmp.write_text(json.dumps({'generation':b,'until':109}));os.replace(tmp,p)
   self.assertEqual(read(100),b)
   # Even with an identical stat key the file is re-read once max_age has elapsed.
   st=p.stat();p.write_text(json.dumps({'generation':a,'until':109}));os.utime(p,ns=(st.st_atime_ns,st.st_mtime_ns))
   with patch.object(pathlib.Path,'stat',autospec=True,side_effect=lambda self,*a,**k:st):
    self.assertEqual(read(100),b);tick[0]+=1.01;self.assertEqual(read(100),a)
   p.unlink();self.assertIsNone(read(100))
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
  with patch.object(ha,'restore_worker',side_effect=lambda *args:order.append('restore')):
   c.warm_control.side_effect=lambda *args:order.append('authorize');c.activate_warm()
  self.assertEqual(order,['restore','authorize']);self.assertRegex(c.generation,'^[a-f0-9]{32}$')
  c.warm_state=lambda:{'state':'active'}
  with patch.object(ha,'restore_worker') as restore:
   self.assertRaises(ha.Unavailable,c.activate_warm);restore.assert_not_called()
 def test_lease_lost_during_restore_never_authorizes(self):
  c=object.__new__(ha.Cluster);c.owned=MagicMock(side_effect=[True,False]);c.warm_state=lambda:{'state':'inactive'};c.store=MagicMock();c.store.head={'digest':'test'};c.root=pathlib.Path('/data');c.run=pathlib.Path('/run');c.c={'api_token':'fake'};c.warm_control=MagicMock()
  with patch.object(ha,'restore_worker'):self.assertRaises(ha.Unavailable,c.activate_warm)
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
