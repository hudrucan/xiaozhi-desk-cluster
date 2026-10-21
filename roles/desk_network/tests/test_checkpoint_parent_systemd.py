"""Opt-in root/Linux fixture tests. Never binds to or starts production services."""
import json
import os
import pathlib
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
import uuid

FILES = pathlib.Path(__file__).resolve().parents[1] / 'files'
sys.path.insert(0, str(FILES))
import ha  # noqa: E402

BINARY = pathlib.Path(os.environ.get('DESK_CHECKPOINT_BINARY', '/nonexistent'))
BRIDGE = FILES.parents[2] / 'tools/desk-checkpoint/bridge.py'
GOLDEN = pathlib.Path(__file__).parent / 'fixtures/checkpoint/workflows.gz'
ENABLED = sys.platform == 'linux' and os.geteuid() == 0 and os.environ.get('DESK_CHECKPOINT_SYSTEMD_TESTS') == '1' and BINARY.is_file()
# Only the fixture launch is changed. execute()/stop_worker(), marker publication,
# parent finally, Store.stage and startup sweep are the real application code.
PARENT = '''import json,pathlib,subprocess,sys
sys.path.insert(0,sys.argv[2])
import ha
job=json.loads(pathlib.Path(sys.argv[1]).read_text())
original=ha.checkpoint_budget.subprocess.run
def isolated(argv,**kwargs):
 if argv[0]=='/usr/bin/systemd-run':
  argv=[a for a in argv if not a.startswith(('--property=BindsTo=','--property=After=','--slice='))]
  argv=['--property=RuntimeMaxSec=20' if a=='--property=RuntimeMaxSec=50' else a for a in argv]
  if job['oom']:
   argv=['--property=MemoryMax=64M' if a=='--property=MemoryMax=384M' else a for a in argv]
   argv.insert(1,'--property=MemorySwapMax=0')
  split=argv.index('--')+1
  worker=argv[split:]
  worker[0]=job['binary']
  for option,value in (('--bridge',job['bridge']),('--ha',str(pathlib.Path(sys.argv[2])/'ha.py')),('--python',sys.executable)):
   worker[worker.index(option)+1]=value
  argv=argv[:split]+worker
 return original(argv,**kwargs)
ha.checkpoint_budget.subprocess.run=isolated
ha.checkpoint_budget.TIMEOUT=job['timeout']
blob=pathlib.Path(job['input']).read_bytes() if job['input'] else None
if job['stage']:
 store=ha.Store(job['store'],b'fixture-key',['a','b','c'],run=job['run'])
 store.stage({'revision':[1,'a',1],'digest':ha.sha(blob)},blob,'a')
else:
 result=ha.snapshot_worker(job['action'],data=job['data'],run=job['run'],blob=blob)
 pathlib.Path(job['output']).write_bytes(result)
'''


@unittest.skipUnless(ENABLED, 'opt-in isolated root/Linux systemd fixture tests')
class CheckpointParentSystemd(unittest.TestCase):
    def setUp(self):
        self.root = pathlib.Path(tempfile.mkdtemp(prefix='desk-parent-fixture-'))
        self.parents = []
        self.addCleanup(self.cleanup)
        self.runner = self.root / 'parent.py'
        self.runner.write_text(PARENT)
        self.blob = GOLDEN.read_bytes()
        db, conf, workflows = ha.unpack(self.blob)
        self.data = self.root / 'data'
        (self.data / 'db').mkdir(parents=True)
        (self.data / 'config').mkdir()
        (self.data / 'db/app.db').write_bytes(db)
        (self.data / 'config/app.conf').write_bytes(conf)
        (self.data / 'config/workflows.json').write_bytes(workflows)

    def cleanup(self):
        for parent in self.parents:
            if parent.poll() is None:
                parent.kill()
                parent.communicate(timeout=5)
        # Do not remove a fixture tree if any unit's death cannot be confirmed.
        for marker in self.root.rglob('worker.unit'):
            ha.checkpoint_budget.stop_worker(marker.read_text())
        shutil.rmtree(self.root)

    def launch(self, action, *, slow=False, timeout=15, stage=False, oom=False):
        case = self.root / (action + '-' + str(len(self.parents)))
        case.mkdir()
        run = case / 'run'
        ready = case / 'ready.json'
        helper = BRIDGE
        if slow or oom:
            helper = case / 'slow.py'
            helper.write_text('import json,os,pathlib,time\npathlib.Path(' + repr(str(ready)) + ').write_text(json.dumps({"helper":os.getpid(),"worker":os.getppid()}))\n' + ('time.sleep(.5)\nbytearray(256*1024*1024)\n' if oom else 'time.sleep(60)\n'))
        incoming = case / 'input'
        incoming.write_bytes((ha.encoded({'token': 'fixture-token'}) + b'\n' if action == 'restore' else b'') + self.blob)
        output = case / 'output'
        job = {'binary': str(BINARY), 'bridge': str(helper), 'timeout': timeout,
               'action': action, 'data': str(case / 'target' if action == 'restore' else self.data),
               'run': str(run), 'input': None if action == 'create' else str(incoming),
               'output': str(output), 'stage': stage, 'store': str(case / 'store'), 'oom': oom}
        path = case / 'job.json'
        path.write_text(json.dumps(job))
        parent = subprocess.Popen([sys.executable, str(self.runner), str(path), str(FILES)], stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        self.parents.append(parent)
        return parent, run, ready, job

    def ready(self, parent, run, ready):
        until = time.monotonic() + 10
        while time.monotonic() < until:
            if ready.exists():
                private, = run.iterdir()
                self.assertEqual(private.stat().st_mode & 0o777, 0o700)
                marker = private / 'worker.unit'
                self.assertTrue(marker.is_file())
                return marker.read_text(), json.loads(ready.read_text())
            if parent.poll() is not None:
                self.fail('Fixture parent exited before helper readiness: ' + parent.communicate()[1].decode())
            time.sleep(.01)
        self.fail('Fixture helper did not become ready')

    def assert_dead(self, pids):
        for pid in pids.values():
            stat = pathlib.Path('/proc') / str(pid) / 'stat'
            try:
                state = stat.read_text().rsplit(')', 1)[1].split()[0]
            except FileNotFoundError:
                continue
            self.assertEqual(state, 'Z', 'Worker/helper still executing after parent cleanup')

    def test_normal_operations_and_real_store_stage(self):
        for action in ('create', 'validate', 'restore'):
            with self.subTest(action=action):
                parent, run, ready, job = self.launch(action, stage=action == 'validate')
                out, err = parent.communicate(timeout=20)
                self.assertEqual(parent.returncode, 0, err.decode())
                self.assertEqual(list(run.iterdir()), [])
                if action == 'create':
                    expected = ha.checkpoint(self.data, self.root / 'reference')
                    self.assertEqual(pathlib.Path(job['output']).read_bytes(), expected)
                if action == 'validate':
                    self.assertTrue((pathlib.Path(job['store']) / (ha.sha(self.blob) + '.gz')).is_file())
                if action == 'restore':
                    self.assertEqual((pathlib.Path(job['data']) / 'db/app.db').read_bytes(), ha.unpack(self.blob)[0])

    def test_runner_timeout_stops_worker_and_helper_before_parent_cleanup(self):
        for action in ('create', 'validate', 'restore'):
            with self.subTest(action=action):
                parent, run, ready, job = self.launch(action, slow=True, timeout=2)
                unit, pids = self.ready(parent, run, ready)
                out, err = parent.communicate(timeout=20)
                self.assertNotEqual(parent.returncode, 0)
                self.assertIn(b'TimeoutExpired', err)
                self.assertEqual(list(run.iterdir()), [])
                self.assert_dead(pids)

    def test_sigkill_unit_stops_helpers_before_parent_cleanup(self):
        for action in ('create', 'validate', 'restore'):
            with self.subTest(action=action):
                parent, run, ready, job = self.launch(action, slow=True)
                unit, pids = self.ready(parent, run, ready)
                subprocess.run(['systemctl', 'kill', '--kill-whom=all', '--signal=SIGKILL', unit], check=True)
                out, err = parent.communicate(timeout=20)
                self.assertNotEqual(parent.returncode, 0)
                self.assertEqual(list(run.iterdir()), [])
                self.assert_dead(pids)

    def test_parent_sigkill_recovered_by_real_startup_sweep(self):
        for action in ('create', 'validate', 'restore'):
            with self.subTest(action=action):
                parent, run, ready, job = self.launch(action, slow=True)
                unit, pids = self.ready(parent, run, ready)
                parent.kill()
                parent.communicate(timeout=5)
                self.assertTrue(list(run.iterdir()), 'Parent crash must leave an orphan for this test')
                self.assertTrue((pathlib.Path('/proc') / str(pids['worker'])).exists())
                ha.checkpoint_budget.sweep_stale_sqlite(run)
                self.assertEqual(list(run.iterdir()), [])
                self.assert_dead(pids)
                ha.checkpoint_budget.sweep_stale_sqlite(run)

    def test_cgroup_oom_cleans_each_operation_and_next_worker_succeeds(self):
        for action in ('create', 'validate', 'restore'):
            with self.subTest(action=action):
                parent, run, ready, job = self.launch(action, oom=True)
                unit, pids = self.ready(parent, run, ready)
                out, err = parent.communicate(timeout=20)
                self.assertNotEqual(parent.returncode, 0)
                self.assertEqual(list(run.iterdir()), [])
                self.assert_dead(pids)
                journal = subprocess.check_output(['journalctl', '-u', unit, '--no-pager', '-o', 'cat'], text=True)
                self.assertIn('oom-kill', journal)
                recovery, recovered_run, _, _ = self.launch(action)
                _, err = recovery.communicate(timeout=20)
                self.assertEqual(recovery.returncode, 0, err.decode())
                self.assertEqual(list(recovered_run.iterdir()), [])

    def test_controller_cgroup_stops_pending_launcher_before_recovery(self):
        run = self.root / 'pending-run'
        private = run / 'ckpt-pending'
        private.mkdir(parents=True, mode=0o700)
        worker_unit = 'desk-network-checkpoint-' + uuid.uuid4().hex[:24] + '.service'
        (private / 'worker.unit').write_text(worker_unit)
        child_pid = self.root / 'pending.pid'
        child = self.root / 'pending.py'
        child.write_text('import os,pathlib,time\npathlib.Path(' + repr(str(child_pid)) + ').write_text(str(os.getpid()))\ntime.sleep(60)\n')
        controller = self.root / 'controller.py'
        controller.write_text('import subprocess,sys,time\nsubprocess.Popen([sys.executable,' + repr(str(child)) + '])\ntime.sleep(60)\n')
        unit = 'desk-parent-fixture-controller-' + uuid.uuid4().hex + '.service'
        def stop_controller():
            subprocess.run(['systemctl', 'stop', unit], check=True, timeout=5)
            subprocess.run(['systemctl', 'reset-failed', unit], capture_output=True, timeout=5)
        self.addCleanup(stop_controller)
        subprocess.run(['systemd-run', '--quiet', '--unit=' + unit,
                        '--property=KillMode=control-group', '--property=TimeoutStopSec=1',
                        '--property=CPUQuota=50%', '--property=MemoryMax=384M',
                        '--property=RuntimeMaxSec=15', '--', sys.executable, str(controller)], check=True)
        until = time.monotonic() + 5
        while not child_pid.exists() and time.monotonic() < until:
            time.sleep(.01)
        self.assertTrue(child_pid.is_file())
        subprocess.run(['systemctl', 'kill', '--kill-whom=main', '--signal=SIGKILL', unit], check=True)
        # This blocking stop is the restart barrier provided by the real service
        # manager before the next ExecStart; startup must not race old clients.
        subprocess.run(['systemctl', 'stop', unit], check=True, timeout=5)
        self.assert_dead({'pending_launcher': int(child_pid.read_text())})
        ha.checkpoint_budget.sweep_stale_sqlite(run)
        self.assertEqual(list(run.iterdir()), [])
        template = FILES.parent / 'templates/desk-network.service.j2'
        self.assertIn('KillMode=control-group', template.read_text())
