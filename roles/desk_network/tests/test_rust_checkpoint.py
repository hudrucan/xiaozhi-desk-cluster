"""Offline contract tests. Set DESK_CHECKPOINT_BINARY to a release build to enable."""
from contextlib import closing
import base64
import gzip
import hashlib
import json
import os
import pathlib
import signal
import sqlite3
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

ROOT = pathlib.Path(__file__).resolve().parents[3]
FILES = ROOT / 'roles/desk_network/files'
sys.path.insert(0, str(FILES))
import ha  # noqa: E402 -- repo helper loaded from its source directory
import python_worker_reference as reference  # noqa: E402

TOOL = ROOT / 'tools/desk-checkpoint'
BINARY = pathlib.Path(os.environ.get('DESK_CHECKPOINT_BINARY', TOOL / 'target/release/desk-checkpoint'))
GOLDEN = pathlib.Path(__file__).parent / 'fixtures/checkpoint'


def fixture(root, mib=1, workflows=True, random=False):
    data = root / 'data'
    (data / 'db').mkdir(parents=True)
    (data / 'config').mkdir()
    (data / 'config/app.conf').write_bytes(b"# test\nARPSCAN_RUN_SCHD='*/1 * * * *'\nTIMEZONE='UTC'\n")
    if workflows:
        (data / 'config/workflows.json').write_bytes(b'[{"name":"offline","enabled":"No"}]')
    c = sqlite3.connect(data / 'db/app.db')
    c.execute('PRAGMA journal_mode=WAL')
    c.execute('PRAGMA wal_autocheckpoint=0')
    c.execute('CREATE TABLE bulk(data BLOB)')
    # Keep the writer open so committed content remains in WAL.
    chunk = bytes(range(256)) * 256
    for _ in range(mib * 16):
        c.execute('INSERT INTO bulk VALUES (?)', (os.urandom(65536) if random else chunk,))
    c.commit()
    return data, c


@unittest.skipUnless(BINARY.is_file(), 'Rust release binary not built')
class RustCheckpoint(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = pathlib.Path(self.temp.name)
        self.run = self.root / 'run'
        self.run.mkdir()

    def command(self, operation, data=None, bridge=None):
        cmd = [str(BINARY), '--snapshot-worker', operation, '--run', str(self.run),
               '--bridge', str(bridge or TOOL / 'bridge.py'), '--ha', str(FILES / 'ha.py'),
               '--python', sys.executable, '--owner', f'{os.getuid()}:{os.getgid()}']
        if data is not None:
            cmd += ['--data', str(data)]
        return cmd

    def rust(self, operation, blob=None, data=None, ok=True):
        result = subprocess.run(self.command(operation, data), input=blob, capture_output=True, timeout=60)
        if ok:
            self.assertEqual(result.returncode, 0, result.stderr)
            if operation != 'create':
                self.assertEqual(result.stdout, b'')
        else:
            self.assertNotEqual(result.returncode, 0)
            self.assertEqual(result.stdout, b'')
        self.assertEqual(list(self.run.iterdir()), [])
        return result.stdout

    def restore_input(self, blob, token='fixture-token'):
        return ha.encoded({'token': token}) + b'\n' + blob

    def test_checked_in_python_golden_hashes_and_restore(self):
        manifest = json.loads((GOLDEN / 'manifest.json').read_text())
        for name, meta in manifest['cases'].items():
            with self.subTest(name=name):
                blob = (GOLDEN / (name + '.gz')).read_bytes()
                self.assertEqual(hashlib.sha256(blob).hexdigest(), meta['sha256'])
                self.rust('validate', blob)
                target = self.root / name
                self.rust('restore', self.restore_input(blob), target)
                self.assertEqual(hashlib.sha256((target / 'db/app.db').read_bytes()).hexdigest(), meta['database_sha256'])
                conf = base64.b64decode(meta['configuration'])
                self.assertEqual((target / 'config/app.conf').read_bytes(), ha.apply_policy(conf, 'fixture-token'))
                self.assertEqual((target / 'db/app.db').stat().st_mode & 0o777, 0o600)
                self.assertEqual(target.stat().st_mode & 0o777, 0o700)

    def test_sizes_wal_cross_language_and_identical_bytes(self):
        for mib in (1, 10, 25, 40):
            with self.subTest(mib=mib):
                root = self.root / str(mib)
                data, writer = fixture(root, mib, workflows=mib != 10)
                try:
                    py = ha.checkpoint(data, root / 'py-run')
                    rust = self.rust('create', data=data)
                    self.assertEqual(rust, py, 'deterministic gzip/snapshot mismatch')
                    self.assertEqual(ha.sha(rust), ha.sha(py))
                    self.rust('validate', py)
                    db, conf, workflows = ha.unpack(rust)
                    self.assertGreaterEqual(len(db), mib * 1024 * 1024)
                    rust_target, py_target = root / 'rust', root / 'python'
                    self.rust('restore', self.restore_input(py), rust_target)
                    with patch.object(ha.os, 'chown'):
                        reference.restore(rust, py_target, root / 'py-run', 'fixture-token')
                    for name in ('db/app.db', 'config/app.conf', 'config/workflows.json'):
                        a, b = rust_target / name, py_target / name
                        self.assertEqual(a.exists(), b.exists())
                        if a.exists():
                            self.assertEqual(a.read_bytes(), b.read_bytes())
                    with closing(sqlite3.connect(rust_target / 'db/app.db')) as c:
                        self.assertEqual(c.execute('SELECT sum(length(data)) FROM bulk').fetchone()[0], mib * 1024 * 1024)
                finally:
                    writer.close()

    def test_wire_envelope_escaping_order_gzip_members_and_headers(self):
        blob = (GOLDEN / 'legacy.gz').read_bytes()
        raw = gzip.decompress(blob)
        values = json.loads(raw)
        escaped = json.dumps(dict(reversed(list(values.items())))).replace('A', '\\u0041').encode()
        variants = [gzip.compress(escaped, mtime=0), gzip.compress(raw[:20]) + gzip.compress(raw[20:]), blob + b'\0' * 5]
        for os_byte in (3, 255):
            b = bytearray(blob)
            b[9] = os_byte
            variants.append(bytes(b))
        for b in variants:
            ha.unpack(b)
            self.rust('validate', b)

    def test_invalid_truncated_corrupt_and_unknown_members(self):
        good = (GOLDEN / 'legacy.gz').read_bytes()
        values = json.loads(gzip.decompress(good))
        bad = [b'', b'bad', good[:-1], good + b'corrupt', good[:-5] + b'abcde']
        for key, value in [('extra', ''), ('database', '!'), ('workflows', base64.b64encode(b'{}').decode())]:
            changed = dict(values)
            changed[key] = value
            bad.append(gzip.compress(ha.encoded(changed)))
        for b in bad:
            self.rust('validate', b, ok=False)
            target = self.root / 'target'
            (target / 'db').mkdir(parents=True, exist_ok=True)
            (target / 'db/app.db').write_bytes(b'old')
            self.rust('restore', self.restore_input(b), target, ok=False)
            self.assertEqual((target / 'db/app.db').read_bytes(), b'old')

    def test_compressed_expansion_and_payload_limits(self):
        self.rust('validate', b'x' * (ha.LIMIT + 1), ok=False)
        # A gzip bomb is produced in chunks rather than allocating 128 MiB.
        path = self.root / 'bomb.gz'
        with gzip.open(path, 'wb', compresslevel=1) as out:
            out.write(b'{"database":"')
            for _ in range(130):
                out.write(b'A' * 1024 * 1024)
            out.write(b'","configuration":""}')
        self.rust('validate', path.read_bytes(), ok=False)
        values = json.loads(gzip.decompress((GOLDEN / 'legacy.gz').read_bytes()))
        for key in ('configuration', 'workflows'):
            values[key] = base64.b64encode(b'x' * (1024 * 1024 + 1)).decode()
            self.rust('validate', gzip.compress(ha.encoded(values)), ok=False)
            values = json.loads(gzip.decompress((GOLDEN / 'legacy.gz').read_bytes()))
        db = b'SQLite format 3\0' + bytes(ha.DBLIMIT + 1 - 16)
        values['database'] = base64.b64encode(db).decode()
        self.rust('validate', gzip.compress(ha.encoded(values), compresslevel=1), ok=False)

    def test_exact_configuration_compressed_and_expansion_boundaries(self):
        values = json.loads(gzip.decompress((GOLDEN / 'legacy.gz').read_bytes()))
        values['configuration'] = base64.b64encode(b'x' * 1024 * 1024).decode()
        exact_conf = gzip.compress(ha.encoded(values), compresslevel=1, mtime=0)
        ha.unpack(exact_conf)
        self.rust('validate', exact_conf)
        # Python permits zero padding after a gzip member, including at LIMIT.
        original = (GOLDEN / 'legacy.gz').read_bytes()
        at_limit = original + bytes(ha.LIMIT - len(original))
        ha.unpack(at_limit)
        self.rust('validate', at_limit)
        self.rust('validate', at_limit + b'\0', ok=False)
        # Valid JSON followed by whitespace reaches the expanded ceiling without
        # tripping the independent database/base64/member ceiling first.
        path = self.root / 'whitespace-bomb.gz'
        with gzip.open(path, 'wb', compresslevel=1) as out:
            out.write(gzip.decompress(original))
            for _ in range(128):
                out.write(b' ' * 1024 * 1024)
        self.rust('validate', path.read_bytes(), ok=False)

    def test_integrity_checked_on_restore_not_envelope_validation(self):
        b = gzip.compress(ha.encoded({'database': base64.b64encode(b'SQLite format 3\0' + b'x' * 1000).decode(), 'configuration': ''}))
        ha.unpack(b)
        self.rust('validate', b)
        self.rust('restore', self.restore_input(b), self.root / 'target', ok=False)
        self.assertFalse((self.root / 'target/db/app.db').exists())

    def test_restore_combined_helper_accepts_maximum_members_and_python_json(self):
        values = json.loads(gzip.decompress((GOLDEN / 'legacy.gz').read_bytes()))
        conf = b'#' + b'x' * (1024 * 1024 - 2) + b'\n'
        workflows = b'[NaN,"\\u0111"]'
        workflows += b' ' * (1024 * 1024 - len(workflows))
        values['configuration'] = base64.b64encode(conf).decode()
        values['workflows'] = base64.b64encode(workflows).decode()
        blob = gzip.compress(ha.encoded(values), compresslevel=1, mtime=0)
        ha.unpack(blob)
        target = self.root / 'combined'
        token = 'fixture-đ\n"'
        self.rust('restore', self.restore_input(blob, token), target)
        self.assertEqual((target / 'config/app.conf').read_bytes(), ha.apply_policy(conf, token))
        self.assertEqual((target / 'config/workflows.json').read_bytes(), workflows)

    def test_restore_credentials_never_in_argv_or_error(self):
        blob = (GOLDEN / 'legacy.gz').read_bytes()
        for header in (b'{}\n', b'{"token":""}\n', b'x' * 4097, b'{"token":4}\n', b'{"token":"x","extra":0}\n'):
            self.rust('restore', header + blob, self.root / 'bad', ok=False)
        secret = 'fixture-credential-🔑'
        self.assertNotIn(secret, str(self.command('restore', self.root / 'target')))
        result = subprocess.run(self.command('restore', self.root / 'target'), input=self.restore_input(b'bad', secret), capture_output=True)
        self.assertNotIn(secret.encode(), result.stderr)
        self.assertNotIn(secret.encode(), result.stdout)

    def test_atomic_write_failure_and_optional_workflow_removal(self):
        blob = (GOLDEN / 'legacy.gz').read_bytes()
        target = self.root / 'target'
        (target / 'db/app.db').mkdir(parents=True)
        self.rust('restore', self.restore_input(blob), target, ok=False)
        self.assertEqual(list((target / 'db').iterdir()), [target / 'db/app.db'])
        (target / 'db/app.db').rmdir()
        (target / 'config').mkdir()
        (target / 'config/workflows.json').write_bytes(b'[]')
        for suffix in ('-wal', '-shm'):
            (target / ('db/app.db' + suffix)).write_bytes(b'old')
        self.rust('restore', self.restore_input(blob), target)
        self.assertFalse((target / 'config/workflows.json').exists())
        self.assertEqual(sorted(p.name for p in (target / 'db').iterdir()), ['app.db'])

    def test_cancellation_reaps_helper_and_cleans_scratch(self):
        data, writer = fixture(self.root)
        self.addCleanup(writer.close)
        helper = self.root / 'slow.py'
        helper.write_text('import time\ntime.sleep(60)\n')
        child = subprocess.Popen(self.command('create', data, helper), stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        try:
            time.sleep(.3)
            child.send_signal(signal.SIGTERM)
            out, _ = child.communicate(timeout=5)
            self.assertNotEqual(child.returncode, 0)
            self.assertEqual(out, b'')
            self.assertEqual(list(self.run.iterdir()), [])
        finally:
            if child.poll() is None:
                child.kill()
                child.communicate()

    def test_sigkill_all_operations_parent_cleanup(self):
        data, writer = fixture(self.root)
        self.addCleanup(writer.close)
        blob = (GOLDEN / 'workflows.gz').read_bytes()
        helper = self.root / 'slow.py'
        helper.write_text('import time\ntime.sleep(60)\n')
        target = self.root / 'target'
        (target / 'db').mkdir(parents=True)
        (target / 'db/app.db').write_bytes(b'keep-old')
        for action in ('create', 'validate', 'restore'):
            def killed(command, blob=None):
                private_run = command[command.index('--run') + 1]
                cmd = self.command(action, data if action == 'create' else target if action == 'restore' else None, helper)
                cmd[cmd.index('--run') + 1] = private_run
                child = subprocess.Popen(cmd, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, start_new_session=True)
                try:
                    if blob:
                        child.stdin.write(blob)
                        child.stdin.flush()
                    time.sleep(.25)
                    os.killpg(child.pid, signal.SIGKILL)
                    child.stdin.close()
                    child.stdin = None
                    child.communicate(timeout=5)
                    raise subprocess.CalledProcessError(child.returncode, cmd)
                finally:
                    if child.poll() is None:
                        child.kill()
                        child.wait()
            with patch.object(ha.checkpoint_budget, 'execute', side_effect=killed):
                with self.assertRaises(subprocess.CalledProcessError):
                    ha.snapshot_worker(action, data=data, run=self.run,
                                       blob=self.restore_input(blob) if action == 'restore' else blob if action == 'validate' else None)
            self.assertEqual(list(self.run.iterdir()), [])
            self.assertEqual((target / 'db/app.db').read_bytes(), b'keep-old')

    def test_timeout_all_operations_parent_cleanup(self):
        data, writer = fixture(self.root)
        self.addCleanup(writer.close)
        helper = self.root / 'slow-timeout.py'
        helper.write_text('import time\ntime.sleep(60)\n')
        blob = (GOLDEN / 'workflows.gz').read_bytes()
        for action in ('create', 'validate', 'restore'):
            def timed_out(command, blob=None):
                cmd = self.command(action, data if action == 'create' else self.root / 'target' if action == 'restore' else None, helper)
                cmd[cmd.index('--run') + 1] = command[command.index('--run') + 1]
                child = subprocess.Popen(cmd, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                         stderr=subprocess.PIPE, start_new_session=True)
                try:
                    child.communicate(blob, timeout=.3)
                except subprocess.TimeoutExpired:
                    os.killpg(child.pid, signal.SIGKILL)
                    child.communicate(timeout=5)
                    raise
                finally:
                    if child.poll() is None:
                        os.killpg(child.pid, signal.SIGKILL)
                        child.communicate()
            with self.subTest(action=action), patch.object(ha.checkpoint_budget, 'execute', side_effect=timed_out):
                with self.assertRaises(subprocess.TimeoutExpired):
                    ha.snapshot_worker(action, data=data, run=self.run,
                                       blob=self.restore_input(blob) if action == 'restore' else blob if action == 'validate' else None)
            self.assertEqual(list(self.run.iterdir()), [])

    def test_existing_certificates_and_parent_budget_cleanup(self):
        data, writer = fixture(self.root)
        self.addCleanup(writer.close)
        calls = []
        def execute(command, blob=None):
            operation = command[command.index('--snapshot-worker') + 1]
            cmd = self.command(operation, data if operation != 'validate' else None)
            if '--run' in command:
                cmd[cmd.index('--run') + 1] = command[command.index('--run') + 1]
            calls.append(cmd)
            return subprocess.run(cmd, input=blob, capture_output=True, check=True, timeout=60)
        with patch.object(ha.checkpoint_budget, 'execute', side_effect=execute):
            blob = ha.snapshot_worker('create', data=data, run=self.run)
            store = ha.Store(self.root / 'store', b'fixture-key', ['a', 'b', 'c'])
            meta = {'revision': [1, 'a', 1], 'digest': ha.sha(blob)}
            meta['certificate'] = [store.stage(meta, blob, 'a'), store.receipt('b', meta)]
            store.commit(meta)
            store.verify(meta)
            self.assertEqual(store.blob(meta['digest']), blob)
            self.assertEqual(list(self.run.iterdir()), [])
