"""Parent-owned scratch and fail-closed recovery for the Rust worker."""
import pathlib
import subprocess
import sys
import tempfile
import types
import unittest
from unittest.mock import patch

FILES = pathlib.Path(__file__).resolve().parents[1] / 'files'
sys.path.insert(0, str(FILES))
import ha  # noqa: E402


class CheckpointParent(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = pathlib.Path(self.temp.name)
        self.run = self.root / 'run'
        self.run.mkdir()

    def write_scratch(self, command):
        private = pathlib.Path(command[command.index('--run') + 1])
        self.assertEqual(private.parent, self.run)
        self.assertEqual(private.stat().st_mode & 0o777, 0o700)
        (private / 'ckpt-rust-orphan').mkdir()
        (private / 'ckpt-rust-orphan/database').write_bytes(b'fixture')
        return private

    def test_every_operation_has_private_scratch_and_uses_rust_with_python_policy(self):
        for action in ('create', 'validate', 'restore'):
            def execute(command, blob=None):
                self.assertEqual(command[0], '/usr/local/libexec/desk-network/rust/desk-checkpoint')
                self.assertEqual(command[command.index('--python') + 1], sys.executable)
                self.assertEqual(command[command.index('--ha') + 1], str(FILES / 'ha.py'))
                self.assertEqual(command[command.index('--snapshot-worker') + 1], action)
                self.assertEqual('--data' in command, action != 'validate')
                self.write_scratch(command)
                return subprocess.CompletedProcess(command, 0, b'fixture' if action == 'create' else b'')
            with self.subTest(action=action), patch.object(ha.checkpoint_budget, 'execute', side_effect=execute):
                result = ha.snapshot_worker(action, data=self.root / 'data', run=self.run, blob=b'private-fixture')
                self.assertEqual(result, b'fixture' if action == 'create' else b'')
            self.assertEqual(list(self.run.iterdir()), [])

    def test_retired_python_worker_cli_is_rejected(self):
        result = subprocess.run([sys.executable, str(FILES / 'ha.py'),
                                 '--snapshot-worker', 'validate'], input=b'fixture',
                                capture_output=True, timeout=5)
        self.assertEqual(result.returncode, 2)
        self.assertIn(b'unrecognized arguments', result.stderr)
        self.assertEqual(result.stdout, b'')

    def test_timeout_and_sigkill_errors_clean_each_operation_after_execute_returns(self):
        for action in ('create', 'validate', 'restore'):
            for error in (subprocess.TimeoutExpired('fixture', 1), subprocess.CalledProcessError(-9, ['fixture'])):
                def execute(command, blob=None):
                    self.write_scratch(command)
                    raise error
                with self.subTest(action=action, error=type(error).__name__), patch.object(ha.checkpoint_budget, 'execute', side_effect=execute):
                    with self.assertRaises(type(error)):
                        ha.snapshot_worker(action, data=self.root / 'data', run=self.run)
                self.assertEqual(list(self.run.iterdir()), [])

    def test_uncertain_stop_retains_marker_until_successful_startup_recovery(self):
        unit = 'desk-network-checkpoint-' + 'c' * 24 + '.service'
        def execute(command, blob=None):
            private = self.write_scratch(command)
            (private / 'worker.unit').write_text(unit)
            raise ha.checkpoint_budget.WorkerNotStopped('fixture stop uncertain')
        with patch.object(ha.checkpoint_budget, 'execute', side_effect=execute):
            with self.assertRaises(ha.checkpoint_budget.WorkerNotStopped):
                ha.snapshot_worker('validate', run=self.run, blob=b'fixture')
        private, = self.run.iterdir()
        with patch.object(ha.checkpoint_budget, 'stop_worker', side_effect=ha.checkpoint_budget.WorkerNotStopped('fixture still alive')):
            with self.assertRaises(ha.checkpoint_budget.WorkerNotStopped):
                ha.checkpoint_budget.sweep_stale_sqlite(self.run)
        self.assertTrue((private / 'ckpt-rust-orphan/database').is_file())
        def stopped(name):
            self.assertEqual(name, unit)
            self.assertTrue(private.exists(), 'Files removed before termination was confirmed')
        (self.run / 'public.json').write_bytes(b'keep')
        with patch.object(ha.checkpoint_budget, 'stop_worker', side_effect=stopped):
            ha.checkpoint_budget.sweep_stale_sqlite(self.run)
        ha.checkpoint_budget.sweep_stale_sqlite(self.run)
        self.assertEqual([p.name for p in self.run.iterdir()], ['public.json'])

    def test_unit_marker_is_published_before_linux_launch(self):
        def launch(argv, **kwargs):
            private, = self.run.iterdir()
            marker = (private / 'worker.unit').read_text()
            self.assertIn('--unit=' + marker, argv)
            self.assertFalse((private / 'worker.unit.tmp').exists())
            return subprocess.CompletedProcess(argv, 0, b'')
        with patch.object(ha.checkpoint_budget.sys, 'platform', 'linux'), \
             patch.object(ha.checkpoint_budget.subprocess, 'run', side_effect=launch), \
             patch.object(ha.checkpoint_budget, 'stop_worker') as stop:
            ha.snapshot_worker('validate', run=self.run, blob=b'fixture')
        stop.assert_called_once()
        self.assertEqual(list(self.run.iterdir()), [])

    def test_store_stage_propagates_its_runtime_directory(self):
        blob = b'fixture-checkpoint'
        store = ha.Store(self.root / 'store', b'fixture-key', ['a', 'b', 'c'], run=self.run)
        def execute(command, blob=None):
            self.write_scratch(command)
            return subprocess.CompletedProcess(command, 0, b'')
        with patch.object(ha.checkpoint_budget, 'execute', side_effect=execute):
            receipt = store.stage({'revision': [1, 'a', 1], 'digest': ha.sha(blob)}, blob, 'a')
        self.assertEqual(receipt['digest'], ha.sha(blob))
        self.assertEqual(list(self.run.iterdir()), [])

    def test_startup_recovers_before_http_and_aborts_if_recovery_is_unsafe(self):
        config = self.root / 'config.json'
        config.write_text('{}')
        cluster = types.SimpleNamespace(run=self.run, members={'a': '127.0.0.1'}, node='a')
        class StopBeforeListening(Exception):
            pass
        orphan = self.run / 'ckpt-old'
        orphan.mkdir()
        (orphan / 'db').write_bytes(b'fixture')
        def server(*args):
            self.assertEqual(list(self.run.iterdir()), [])
            raise StopBeforeListening()
        with patch.object(sys, 'argv', ['ha.py', '--config', str(config)]), \
             patch.object(ha, 'Cluster', return_value=cluster), patch.object(ha, 'Server', side_effect=server):
            with self.assertRaises(StopBeforeListening):
                ha.main()
        orphan.mkdir()
        (orphan / 'worker.unit').write_text('desk-network.service')
        with patch.object(sys, 'argv', ['ha.py', '--config', str(config)]), \
             patch.object(ha, 'Cluster', return_value=cluster), patch.object(ha, 'Server') as listen, \
             patch.object(ha.checkpoint_budget.subprocess, 'run') as systemctl:
            with self.assertRaises(ha.checkpoint_budget.WorkerNotStopped):
                ha.main()
        listen.assert_not_called()
        systemctl.assert_not_called()
        self.assertTrue(orphan.exists())
