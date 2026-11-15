"""Measure durable writes and peer transfers, including repeated unchanged cycles."""
import importlib.util
from pathlib import Path
import sqlite3
import sys
import tempfile
import threading
import unittest
from unittest.mock import patch

FILES = Path(__file__).resolve().parents[1] / 'files'
sys.path.insert(0, str(FILES))
spec = importlib.util.spec_from_file_location('dedup_ha', FILES / 'ha.py')
ha = importlib.util.module_from_spec(spec)
spec.loader.exec_module(ha)


class CheckpointDedup(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        data = self.root / 'data'
        (data / 'db').mkdir(parents=True)
        (data / 'config').mkdir()
        (data / 'config/app.conf').write_text('scan=1')
        self.db = sqlite3.connect(data / 'db/app.db', isolation_level=None)
        self.addCleanup(self.db.close)
        self.db.execute('PRAGMA journal_mode=WAL')
        self.db.execute('CREATE TABLE items(value)')
        self.stores = {n: ha.Store(self.root / n, b'test-key', ['a', 'b', 'c']) for n in 'abc'}
        self.cluster = self.controller()

    def controller(self):
        c = ha.Cluster.__new__(ha.Cluster)
        c.snaplock = threading.Lock()
        c.oplock = threading.RLock()
        c.root = self.root
        c.run = self.root / 'run'
        c.term = [1, 'a']
        c.sequence = 0
        c.owned = lambda: True
        c.store = self.stores['a']
        c.fanout = self.fanout
        self.addCleanup(c.retire_checkpoint_source)
        return c

    def fanout(self, action, value, timeout):
        self.transfers.append(action)
        if action == 'stage':
            blob = ha.base64.b64decode(value['blob'])
            return {n: s.stage(value['meta'], blob, n) for n, s in self.stores.items()}
        self.assertEqual(action, 'commit')
        for s in self.stores.values():
            s.commit(value['meta'])
        return {n: {'ok': True} for n in self.stores}

    def worker(self, action, **kwargs):
        if action == 'semantic':
            from checkpoint_source import semantic_fingerprint
            return semantic_fingerprint(self.root / 'data').encode()
        if action == 'create':
            return ha.checkpoint(self.root / 'data', self.root / 'scratch')
        self.assertEqual(action, 'validate')
        ha.unpack(kwargs['blob'])
        return b''

    def test_unchanged_restart_and_new_term_need_no_disk_write_or_replication(self):
        self.transfers = []
        with patch.object(ha, 'snapshot_worker', side_effect=self.worker), patch.object(ha, 'atomic', wraps=ha.atomic) as writes:
            self.cluster.snapshot(force=False)
            self.assertEqual(self.transfers, ['stage', 'commit'])
            self.assertEqual(sum(str(c.args[0]).endswith('.gz') for c in writes.call_args_list), 3)
            self.assertEqual(sum(str(c.args[0]).endswith('head.json') for c in writes.call_args_list), 3)
            heads = {n: (s.root / 'head.json').read_bytes() for n, s in self.stores.items()}
            stats = {str(p): (p.stat().st_ino, p.stat().st_mtime_ns) for s in self.stores.values() for p in s.root.iterdir()}
            writes.reset_mock()
            self.transfers.clear()
            for _ in range(8):
                self.cluster.snapshot(force=False)
            # Controller restart loses only the detector caches. Periodic
            # backup/hash still agrees; no read of the previous compressed file.
            self.stores['a'] = ha.Store(self.root / 'a', b'test-key', ['a', 'b', 'c'])
            self.cluster = self.controller()
            self.cluster.term = [2, 'a']
            with patch.object(self.cluster.store, 'blob', side_effect=AssertionError('old blob reread')):
                self.cluster.snapshot(force=False)
                for _ in range(8):
                    self.cluster.snapshot(force=False)
            writes.assert_not_called()
            self.assertEqual(self.transfers, [])
            self.assertEqual(stats, {str(p): (p.stat().st_ino, p.stat().st_mtime_ns) for s in self.stores.values() for p in s.root.iterdir()})
            # A follower restarted after those cycles loads and verifies the
            # original certificate and usable SQLite snapshot without head.at aging.
            follower = ha.Store(self.root / 'c', b'test-key', ['a', 'b', 'c'])
            follower.verify(follower.head)
            db, conf, _ = ha.unpack(follower.blob(follower.head['digest']))
            restored = self.root / 'restored.db'
            restored.write_bytes(db)
            with sqlite3.connect(restored) as connection:
                self.assertEqual(connection.execute('SELECT count(*) FROM items').fetchone()[0], 0)
            self.assertEqual(conf, b'scan=1')
            self.assertEqual(heads['c'], (follower.root / 'head.json').read_bytes())
            # A real WAL commit must invalidate the detector and reach quorum.
            self.db.execute('INSERT INTO items VALUES (42)')
            self.cluster.snapshot(force=False)
            self.assertEqual(self.transfers, ['stage', 'commit'])
            self.assertNotEqual(follower.head['digest'], self.cluster.store.head['digest'])
            self.assertEqual(sum(str(c.args[0]).endswith('.gz') for c in writes.call_args_list), 3)

    def test_replayed_commit_is_read_only_but_missing_blob_still_fails(self):
        self.transfers = []
        with patch.object(ha, 'snapshot_worker', side_effect=self.worker):
            self.cluster.snapshot()
        for store in self.stores.values():
            with patch.object(ha, 'atomic') as writes:
                store.commit(dict(store.head))
                writes.assert_not_called()
            store.blob(store.head['digest'])
        store = self.stores['b']
        (store.root / 'head.json').unlink()
        with patch.object(ha, 'atomic', wraps=ha.atomic) as writes:
            store.commit(dict(store.head))
            self.assertEqual(writes.call_count, 1)
        self.assertEqual(ha.json.loads((store.root / 'head.json').read_bytes()), store.head)
        store = self.stores['c']
        (store.root / (store.head['digest'] + '.gz')).unlink()
        with self.assertRaises(FileNotFoundError):
            store.commit(dict(store.head))
