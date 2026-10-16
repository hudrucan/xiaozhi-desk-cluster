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
  with patch.object(budget.sys,'platform','linux'),patch.object(budget.subprocess,'run',return_value=MagicMock(stdout=b'checkpoint')) as run:
   result=budget.execute(['/usr/bin/python3','/fixed/ha.py','--snapshot-worker','validate'],b'private-checkpoint')
  args=run.call_args.args[0]
  self.assertIn('--slice=desk-network-checkpoint.slice',args)
  self.assertIn('--property=BindsTo=desk-network.service',args)
  self.assertIn('--property=RuntimeMaxSec=50',args)
  self.assertIn('--property=CPUQuota=50%',args)
  self.assertEqual(run.call_args.kwargs['input'],b'private-checkpoint')
  self.assertEqual(result.stdout,b'checkpoint')
 def test_timeout_stops_transient_worker_and_never_falls_back_unbounded(self):
  with patch.object(budget.sys,'platform','linux'),patch.object(budget.subprocess,'run',side_effect=[subprocess.TimeoutExpired('worker',60),MagicMock()]) as run:
   with self.assertRaises(subprocess.TimeoutExpired):budget.execute(['/fixed/worker'])
  self.assertEqual(run.call_count,2)
  self.assertEqual(run.call_args_list[1].args[0][:2],['/usr/bin/systemctl','stop'])
 def test_missing_systemd_fails_closed(self):
  with patch.object(budget.sys,'platform','linux'),patch.object(budget.subprocess,'run',side_effect=[FileNotFoundError(),MagicMock()]) as run:
   with self.assertRaises(FileNotFoundError):budget.execute(['/fixed/worker'])
  self.assertEqual(run.call_count,2)

if __name__=='__main__':unittest.main()
