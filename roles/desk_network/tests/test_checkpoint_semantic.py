"""Logical changes, forced freshness, coherent reads and durable replication."""
import ast
from contextlib import closing
from pathlib import Path
import sqlite3
import subprocess
import sys
import threading
import unittest
from unittest.mock import patch
import test_checkpoint_dedup as reference
ha, FILES = reference.ha, reference.FILES
from checkpoint_source import semantic_fingerprint


class SemanticCheckpoint(unittest.TestCase):
    setUp = reference.CheckpointDedup.setUp
    controller = reference.CheckpointDedup.controller
    fanout = reference.CheckpointDedup.fanout
    worker = reference.CheckpointDedup.worker

    def prepare(self):
        for statement in [
            'CREATE TABLE Devices(id INTEGER PRIMARY KEY, devName TEXT COLLATE NOCASE, devLastConnection, devPresentLastScan, devLastIP)',
            "INSERT INTO Devices VALUES(1,'phone','old',1,'192.168.1.1')",
            'CREATE TABLE AppEvents(id INTEGER PRIMARY KEY AUTOINCREMENT, value)',
            'CREATE TABLE Online_History(value)', 'CREATE TABLE CurrentScan(value)',
            'CREATE TABLE Events(value)', 'CREATE TABLE Sessions(value)',
            'CREATE TABLE Settings(value)', 'CREATE TABLE Plugins_History(value)',
            'CREATE TABLE Plugins_Objects(value)', 'CREATE TABLE Plugins_Language_Strings(value)',
            'CREATE TABLE Notifications(value)']:
            self.db.execute(statement)
        self.transfers = []
        self.cluster.snapshot(force=False)
        self.transfers.clear()

    def noise(self, number=1):
        self.db.execute('UPDATE Devices SET devLastConnection=?', (str(number),))
        for table in ('AppEvents', 'Online_History', 'CurrentScan'):
            self.db.execute('INSERT INTO '+table+'(value) VALUES (?)', (number,))

    def test_noise_cycles_no_blob_no_fanout_no_head_write_and_restore_synced(self):
        with patch.object(ha, 'snapshot_worker', side_effect=self.worker):
            self.prepare()
            head = dict(self.cluster.store.head)
            with patch.object(ha, 'atomic', wraps=ha.atomic) as writes, patch.object(ha, 'checkpoint', wraps=ha.checkpoint) as backup:
                for n in range(12):
                    self.noise(n)
                    self.cluster.snapshot(force=False)
                    self.assertEqual(self.cluster.sync_state, 'synced')
                writes.assert_not_called()
                backup.assert_not_called()
            self.assertEqual(self.transfers, [])
            self.assertEqual(self.cluster.store.head, head)
            follower = ha.Store(self.root/'c', b'test-key', ['a','b','c'])
            follower.verify(follower.head)
            db, _, _ = ha.unpack(follower.blob(follower.head['digest']))
            restored = self.root/'restored.db'; restored.write_bytes(db)
            with closing(sqlite3.connect(restored)) as connection:
                self.assertEqual(connection.execute('SELECT devName,devLastConnection FROM Devices').fetchall(), [('phone','old')])
            self.assertEqual(follower.head, head)

    def test_all_other_data_and_configuration_are_meaningful(self):
        with patch.object(ha, 'snapshot_worker', side_effect=self.worker):
            self.prepare()
            changes = [lambda: self.db.execute("UPDATE Devices SET devName='PHONE'"),
                       lambda: self.db.execute('UPDATE Devices SET devPresentLastScan=0'),
                       lambda: self.db.execute("UPDATE Devices SET devLastIP='192.168.1.2'"),
                       lambda: self.db.execute("INSERT INTO Devices VALUES(2,'new','now',1,'192.168.1.3')")]
            changes += [lambda t=t: self.db.execute('INSERT INTO '+t+' VALUES (?)', ('changed',))
                        for t in ('Events','Sessions','Settings','Plugins_History','Plugins_Objects','Plugins_Language_Strings','Notifications')]
            changes += [lambda: (self.root/'data/config/app.conf').write_text('scan=2'),
                        lambda: (self.root/'data/config/workflows.json').write_text('[]')]
            for change in changes:
                with self.subTest(change=change):
                    self.transfers.clear(); old = self.cluster.store.head['digest']
                    change(); self.cluster.snapshot(force=False)
                    self.assertEqual(self.transfers, ['stage','commit'])
                    self.assertNotEqual(self.cluster.store.head['digest'], old)

    def test_maximum_age_bypasses_both_caches_and_refreshes_unchanged_head(self):
        with patch.object(ha, 'snapshot_worker', side_effect=self.worker), patch.object(ha.time, 'time', return_value=100000) as now:
            self.prepare()
            now.return_value += 21601
            # Even a byte-identical DB needs a new certified head timestamp.
            self.cluster.snapshot(force=False)
            self.assertEqual(self.transfers, ['stage','commit'])
            self.assertEqual(self.cluster.store.head['at'], now.return_value)
            self.transfers.clear(); self.noise()
            self.cluster.c = {'checkpoint_max_age_seconds':60}
            now.return_value += 61; self.cluster.snapshot(force=False)
            self.assertEqual(self.transfers, ['stage','commit'])

    def test_forced_handoff_takes_full_snapshot_despite_matching_semantics(self):
        with patch.object(ha, 'snapshot_worker', side_effect=self.worker):
            self.prepare(); self.noise()
            job = {'done':threading.Event(), 'term':list(self.cluster.term), 'ok':False}
            self.cluster.rebalance_checkpoint(job)
            self.assertTrue(job['ok']); self.assertTrue(job['done'].is_set())
            self.assertEqual(self.transfers, ['stage','commit'])
            self.transfers.clear(); self.cluster.snapshot()  # Byte-identical forced checkpoints also certify freshness.
            self.assertEqual(self.transfers, ['stage','commit'])
            self.transfers.clear(); self.noise(2); self.cluster.snapshot()  # warm-drain's actual call
            self.assertEqual(self.transfers, ['stage','commit'])
            tree = ast.parse((FILES/'ha.py').read_text())
            loop = next(n for n in ast.walk(tree) if isinstance(n, ast.FunctionDef) and n.name=='run_loop')
            forced = next(n for n in ast.walk(loop) if isinstance(n, ast.If) and isinstance(n.test,ast.Name) and n.test.id=='forced')
            calls = [n for n in ast.walk(forced) if isinstance(n,ast.Call) and isinstance(n.func,ast.Attribute) and n.func.attr=='snapshot']
            self.assertEqual(len(calls),1); self.assertFalse(calls[0].keywords)

    def test_probe_backup_race_never_accepts_wrong_fingerprint(self):
        def racing(action, **kwargs):
            result = self.worker(action, **kwargs)
            if action == 'semantic':
                self.db.execute("INSERT INTO items VALUES ('racing')")
            return result
        with patch.object(ha,'snapshot_worker',side_effect=self.worker): self.prepare()
        self.noise()
        with patch.object(ha,'snapshot_worker',side_effect=racing): self.cluster.snapshot(force=False)
        self.assertIsNone(self.cluster._checkpoint_source.accepted)
        # The semantic skip was of a coherent pre-race view; the next cycle must
        # detect the real WAL change, rather than cache that new data_version.
        self.transfers.clear()
        with patch.object(ha,'snapshot_worker',side_effect=self.worker): self.cluster.snapshot(force=False)
        self.assertEqual(self.transfers,['stage','commit'])

    def test_changed_probe_then_newer_backup_cannot_seed_semantic_cache(self):
        with patch.object(ha,'snapshot_worker',side_effect=self.worker): self.prepare()
        self.db.execute('INSERT INTO Events VALUES(1)')
        def racing(action,**kwargs):
            result=self.worker(action,**kwargs)
            if action=='semantic':self.db.execute('INSERT INTO Events VALUES(2)')
            return result
        with patch.object(ha,'snapshot_worker',side_effect=racing): self.cluster.snapshot(force=False)
        self.assertIsNone(self.cluster._semantic_checkpoint)
        self.assertIsNone(self.cluster._checkpoint_source.accepted)
        db,_,_=ha.unpack(self.cluster.store.blob(self.cluster.store.head['digest']))
        restored=self.root/'raced.db';restored.write_bytes(db)
        with closing(sqlite3.connect(restored)) as reader:
            self.assertEqual(reader.execute('SELECT * FROM Events').fetchall(),[(1,),(2,)])

    def test_configuration_aba_during_backup_invalidates_cache(self):
        with patch.object(ha,'snapshot_worker',side_effect=self.worker):self.prepare()
        self.db.execute('INSERT INTO Events VALUES(3)')
        def racing(action,**kwargs):
            if action!='create':return self.worker(action,**kwargs)
            path=self.root/'data/config/app.conf';original=path.read_bytes()
            path.write_text('changed while backing up')
            result=self.worker(action,**kwargs)
            path.write_bytes(original)
            return result
        with patch.object(ha,'snapshot_worker',side_effect=racing):self.cluster.snapshot(force=False)
        self.assertIsNone(self.cluster._semantic_checkpoint)
        self.assertIsNone(self.cluster._checkpoint_source.accepted)

    def test_commit_ack_failure_cannot_be_skipped_as_synced(self):
        with patch.object(ha,'snapshot_worker',side_effect=self.worker):
            self.prepare(); self.db.execute('INSERT INTO Events VALUES(42)')
            original = self.cluster.fanout
            def fail(action, value, timeout):
                result = original(action,value,timeout)
                return {'a':result['a']} if action=='commit' else result
            with patch.object(self.cluster,'fanout',side_effect=fail):
                with self.assertRaises(ha.Unavailable): self.cluster.snapshot(force=False)
            self.assertEqual(self.cluster.sync_state,'pending')
            self.transfers.clear(); self.cluster.snapshot(force=False)
            self.assertEqual(self.transfers,['stage','commit']); self.assertEqual(self.cluster.sync_state,'synced')

    def test_remote_quorum_without_local_head_is_pending_not_semantic_skip(self):
        with patch.object(ha,'snapshot_worker',side_effect=self.worker):
            self.prepare();self.db.execute('INSERT INTO Events VALUES(8)')
            original=self.cluster.fanout
            def remote_only(action,value,timeout):
                if action!='commit':return original(action,value,timeout)
                for name in ('b','c'):self.stores[name].commit(value['meta'])
                return {'b':{'ok':True},'c':{'ok':True}}
            with patch.object(self.cluster,'fanout',side_effect=remote_only):
                with self.assertRaises(ha.Unavailable):self.cluster.snapshot(force=False)
            self.assertEqual(self.cluster.sync_state,'pending')
            self.transfers.clear();self.cluster.snapshot(force=False)
            self.assertEqual(self.transfers,['stage','commit'])

    def test_semantic_read_transaction_has_one_consistent_database_view(self):
        self.transfers = []
        with patch.object(ha,'snapshot_worker',side_effect=self.worker): self.prepare()
        old = semantic_fingerprint(self.root/'data')
        real = sqlite3.connect; raced = []
        class Reader:
            def __init__(self): self.db = real((self.root/'data/db/app.db').as_uri()+'?mode=ro',uri=True)
            def __getattr__(self,n): return getattr(self.db,n)
            def execute(self,sql,*args):
                result = self.db.execute(sql,*args)
                if sql.startswith('SELECT type,name') and not raced:
                    self.writer.execute('INSERT INTO Events VALUES(99)'); raced.append(True)
                return result
        Reader.root=self.root; Reader.writer=self.db
        with patch('checkpoint_source.sqlite3.connect',side_effect=lambda *a,**k: Reader()):
            self.assertEqual(semantic_fingerprint(self.root/'data'),old)
        self.assertNotEqual(semantic_fingerprint(self.root/'data'),old)

    def test_uncertain_probe_falls_back_to_full_but_unreaped_worker_stops(self):
        with patch.object(ha,'snapshot_worker',side_effect=self.worker):self.prepare()
        self.noise()
        def broken(action,**kwargs):
            if action=='semantic':raise ValueError('incomplete probe')
            return self.worker(action,**kwargs)
        with patch.object(ha,'snapshot_worker',side_effect=broken):self.cluster.snapshot(force=False)
        self.assertEqual(self.transfers,['stage','commit'])
        self.noise(2);self.transfers.clear()
        with patch.object(ha,'snapshot_worker',side_effect=ha.checkpoint_budget.WorkerNotStopped('unreaped')) as worker:
            with self.assertRaises(ha.checkpoint_budget.WorkerNotStopped):self.cluster.snapshot(force=False)
            worker.assert_called_once()
        self.assertEqual(self.transfers,[])

    def test_typed_rows_ignore_insertion_order_but_keep_duplicates_and_schema(self):
        self.db.execute('CREATE TABLE UnknownTable(value COLLATE NOCASE)')
        values=[None,1,1.0,'A','a',b'bytes','duplicate','duplicate']
        self.db.executemany('INSERT INTO UnknownTable VALUES (?)',[(x,) for x in values])
        first=semantic_fingerprint(self.root/'data')
        self.db.execute('DELETE FROM UnknownTable')
        self.db.executemany('INSERT INTO UnknownTable VALUES (?)',[(x,) for x in reversed(values)])
        self.assertEqual(semantic_fingerprint(self.root/'data'),first)
        self.db.execute("DELETE FROM UnknownTable WHERE rowid=(SELECT rowid FROM UnknownTable WHERE value='duplicate' LIMIT 1)")
        self.assertNotEqual(semantic_fingerprint(self.root/'data'),first)
        second=semantic_fingerprint(self.root/'data')
        self.db.execute('CREATE INDEX meaningful_index ON UnknownTable(value)')
        self.assertNotEqual(semantic_fingerprint(self.root/'data'),second)

    def test_worker_cli_and_budget_route_and_fencing(self):
        with patch.object(ha,'snapshot_worker',side_effect=self.worker): self.prepare()
        result = subprocess.run([sys.executable,str(FILES/'checkpoint_source.py'),'--data',str(self.root/'data'),'--run',str(self.root/'run')],capture_output=True,check=True)
        self.assertEqual(result.stdout.decode().strip(),semantic_fingerprint(self.root/'data'))
        from types import SimpleNamespace
        with patch.object(ha.checkpoint_budget,'execute',return_value=SimpleNamespace(stdout=result.stdout)) as execute:
            self.assertEqual(ha.snapshot_worker('semantic',data=self.root/'data',run=self.root/'run'),result.stdout)
            self.assertIn('--run',execute.call_args.args[0])
            self.assertEqual(list((self.root/'run').glob('ckpt-*')),[])
        self.noise()
        def fence(action,**kwargs):
            result=self.worker(action,**kwargs)
            if action=='semantic':self.cluster.term=[2,'a']
            return result
        self.transfers.clear()
        with patch.object(ha,'snapshot_worker',side_effect=fence):
            with self.assertRaises(ha.Unavailable):self.cluster.snapshot(force=False)
        self.assertEqual(self.transfers,[])
