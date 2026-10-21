import importlib.util
from pathlib import Path
import subprocess
import unittest
from unittest.mock import patch, MagicMock

FILES=Path(__file__).resolve().parents[1]/'files'
spec=importlib.util.spec_from_file_location('checkpoint_budget',FILES/'checkpoint_budget.py')
budget=importlib.util.module_from_spec(spec);spec.loader.exec_module(budget)

class CheckpointBudget(unittest.TestCase):
 def test_linux_never_runs_outside_shared_budget_and_does_not_change_blob(self):
  with patch.object(budget.sys,'platform','linux'),patch.object(budget,'stop_worker'),patch.object(budget.subprocess,'run',return_value=MagicMock(stdout=b'checkpoint')) as run:
   result=budget.execute(['/usr/bin/python3','/fixed/ha.py','--snapshot-worker','validate'],b'private-checkpoint')
  args=run.call_args.args[0]
  self.assertIn('--slice=desk-network-checkpoint.slice',args)
  self.assertIn('--property=BindsTo=desk-network.service',args)
  self.assertIn('--property=RuntimeMaxSec=50',args)
  self.assertIn('--property=CPUQuota=50%',args)
  self.assertEqual(run.call_args.kwargs['input'],b'private-checkpoint')
  self.assertEqual(result.stdout,b'checkpoint')
 def test_timeout_stops_transient_worker_and_never_falls_back_unbounded(self):
  with patch.object(budget.sys,'platform','linux'),patch.object(budget,'stop_worker') as stop,patch.object(budget.subprocess,'run',side_effect=subprocess.TimeoutExpired('worker',60)) as run:
   with self.assertRaises(subprocess.TimeoutExpired):budget.execute(['/fixed/worker'])
  self.assertEqual(run.call_count,1)
  stop.assert_called_once()
 def test_missing_systemd_fails_closed(self):
  with patch.object(budget.sys,'platform','linux'),patch.object(budget,'stop_worker') as stop,patch.object(budget.subprocess,'run',side_effect=FileNotFoundError()) as run:
   with self.assertRaises(FileNotFoundError):budget.execute(['/fixed/worker'])
  self.assertEqual(run.call_count,1)
  stop.assert_called_once()

 def test_stop_confirms_empty_cgroup_and_rejects_live_helpers(self):
  unit='desk-network-checkpoint-'+'a'*24+'.service'
  before=subprocess.CompletedProcess([],0,'LoadState=loaded\nActiveState=active\nControlGroup=/fixture\n')
  after=subprocess.CompletedProcess([],0,'LoadState=loaded\nActiveState=failed\nControlGroup=/fixture\n')
  for populated in ('0','1'):
   with self.subTest(populated=populated),patch.object(budget.subprocess,'run',side_effect=[before,MagicMock(),after]),patch.object(Path,'read_text',return_value='populated '+populated+'\nfrozen 0\n'):
    if populated=='0':budget.stop_worker(unit)
    else:
     with self.assertRaises(budget.WorkerNotStopped):budget.stop_worker(unit)

 def test_missing_unit_is_dead_but_unavailable_manager_is_not_confirmation(self):
  unit='desk-network-checkpoint-'+'b'*24+'.service'
  with patch.object(budget.subprocess,'run',return_value=subprocess.CompletedProcess([],1,'LoadState=not-found\n')):
   budget.stop_worker(unit)
  for failure in (subprocess.TimeoutExpired('show',5),OSError('unavailable')):
   with patch.object(budget.subprocess,'run',side_effect=failure):
    with self.assertRaises(budget.WorkerNotStopped):budget.stop_worker(unit)

 def test_cleanup_interruption_retains_the_private_directory_contract(self):
  with patch.object(budget.sys,'platform','linux'),patch.object(budget.subprocess,'run',return_value=MagicMock(stdout=b'')),patch.object(budget,'stop_worker',side_effect=KeyboardInterrupt):
   with self.assertRaises(budget.WorkerNotStopped):budget.execute(['/fixed/worker'])
 def test_startup_sweep_removes_orphaned_scratch_but_keeps_other_files(self):
  import tempfile
  with tempfile.TemporaryDirectory() as td:
   run=Path(td)
   for name in ('sqlite-a.db','sqlite-a.db-journal','sqlite-b.db-wal','sqlite-b.db-shm','public.json','restore-check.db'):(run/name).write_bytes(b'x')
   (run/'ckpt-dead').mkdir();(run/'ckpt-dead/sqlite-c.db').write_bytes(b'x')
   budget.sweep_stale_sqlite(run)
   self.assertEqual(sorted(p.name for p in run.iterdir()),['public.json','restore-check.db'])
   budget.sweep_stale_sqlite(run)

if __name__=='__main__':unittest.main()
