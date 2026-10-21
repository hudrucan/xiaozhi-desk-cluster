"""Measurement failure must never look like a zero-cost successful worker."""
import contextlib
import io
import importlib.util
import json
import pathlib
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

PATH = pathlib.Path(__file__).resolve().parents[3] / 'tools/desk-checkpoint/bench.py'
spec = importlib.util.spec_from_file_location('checkpoint_benchmark', PATH)
bench = importlib.util.module_from_spec(spec)
spec.loader.exec_module(bench)


class BenchmarkTests(unittest.TestCase):
    def test_missing_result_after_wrapper_oom_preserves_unknown_metrics(self):
        with tempfile.TemporaryDirectory() as temp:
            work = pathlib.Path(temp)
            def read(path):
                return None if pathlib.Path(path).name == 'memory.peak' else '50000 100000'
            with patch.object(bench, 'controller', return_value=None), \
                 patch.object(bench, 'environment', return_value={}), \
                 patch.object(bench, 'read', side_effect=read), \
                 patch.object(bench, 'counters', return_value={'oom_kill': 1}), \
                 patch.object(bench.subprocess, 'run', return_value=subprocess.CompletedProcess([], 0)), \
                 patch.object(bench.subprocess, 'check_output', side_effect=['/fixture', 'failed']):
                result = bench.measured({'steps': [{'argv': ['worker']}]}, work, True)
            self.assertEqual(result['errors'], 1)
            self.assertIsNone(result['wall_seconds'])
            self.assertIsNone(result['cpu_seconds'])
            self.assertIsNone(result['peak_rss_bytes'])
            self.assertIsNone(result['cgroup']['memory_peak_bytes'])
            self.assertEqual(result['cgroup']['memory_events']['oom_kill'], 1)
            result.update(mib=1, operation='create', engine='rust', warmup=False)
            group = bench.summary([result])['groups'][0]
            self.assertEqual(group['errors'], 1)
            self.assertIsNone(group['metrics']['wall_seconds'])

    def test_real_child_result_is_complete_and_leaves_no_publication_temp(self):
        with tempfile.TemporaryDirectory() as temp:
            work = pathlib.Path(temp)
            destination = work / 'measure.json'
            spec_path = work / 'spec.json'
            spec_path.write_text(json.dumps({'result': str(destination), 'steps': [
                {'argv': [sys.executable, '-c', 'pass'], 'stdin': None,
                 'stdout': str(work / 'output'), 'expected_exit': 0}]}))
            bench.inside(spec_path)
            result = json.loads(destination.read_text())
            self.assertEqual(len(result), 1)
            self.assertEqual(result[0]['returncode'], 0)
            self.assertGreater(result[0]['wall_seconds'], 0)
            self.assertFalse(destination.with_suffix('.tmp').exists())

    def test_resume_keeps_completed_samples_and_refuses_a_changed_binary(self):
        with tempfile.TemporaryDirectory() as temp:
            root = pathlib.Path(temp)
            worker = root / 'worker'
            worker.write_text('#!/bin/sh\nexit 1\n')
            worker.chmod(0o700)
            output = root / 'report.json'
            args = ['bench.py', '--binary', str(worker), '--output', str(output),
                    '--scratch', str(root), '--sizes', '1', '--operations', 'invalid',
                    '--runs', '1', '--warmups', '0']
            with patch.object(sys, 'argv', args), contextlib.redirect_stdout(io.StringIO()):
                bench.main()
            initial = json.loads(output.read_text())
            kept = initial['rows'][0]
            initial['rows'] = [kept]
            output.write_text(json.dumps(initial))
            with patch.object(sys, 'argv', args + ['--resume']), \
                 patch.object(bench, 'measured', wraps=bench.measured) as measured, \
                 contextlib.redirect_stdout(io.StringIO()):
                bench.main()
            resumed = json.loads(output.read_text())
            self.assertEqual(measured.call_count, 1)
            self.assertEqual(len(resumed['rows']), 2)
            self.assertEqual(resumed['rows'][0], kept)
            self.assertEqual(len(resumed['resume_events']), 1)
            worker.write_text('#!/bin/sh\nexit 2\n')
            with patch.object(sys, 'argv', args + ['--resume']), \
                 contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit) as error:
                bench.main()
            self.assertEqual(error.exception.code, 2)
