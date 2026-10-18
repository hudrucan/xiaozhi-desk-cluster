"""Command identity, durable crash boundaries, fencing and overload behavior."""
import asyncio
import json
import pathlib
import sys
import tempfile
import time
import unittest
from unittest.mock import Mock, patch
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]/'files'))
import command_contract as c
import command_worker as m
from observation_worker import Observations
W='tapo_c222_163';G='a'*32

def submit(ident='b', **changes):
    value=dict(version=1,workload=W,generation=G,request_id=ident*32,operation='submit',command='ptz_move',arguments=dict(x=15,y=0),deadline=time.time()+15)
    value.update(changes);return value

def poll(req):return dict({k:req[k] for k in ('version','workload','generation','request_id')},operation='result')

class ContractTests(unittest.TestCase):
    def test_only_explicit_bounded_operations(self):
        c.request(submit(),time.time())
        for kwargs in [dict(command='reboot'),dict(arguments=dict(x=True,y=0)),dict(arguments=dict(x=1,y=1)),dict(arguments=dict(x=0,y=0)),dict(arguments=dict(x=361,y=0)),dict(command='privacy_set',arguments=dict(enabled=1)),dict(command='preset_goto',arguments=dict(preset='../1')),dict(version=True),dict(deadline=time.time()+21)]:
            with self.assertRaises(ValueError):c.request(submit(**kwargs),time.time())
        for k,v in [('generation','bad'),('workload','other'),('operation','cancel')]:
            with self.assertRaises(ValueError):c.request(submit(**{k:v}),time.time())
    def test_receipt_binding_and_reconciliation(self):
        req=submit();receipt=c.response(req,'unknown','outcome_unknown');c.validate_response(receipt,req)
        with self.assertRaises(ValueError):c.validate_response(dict(receipt,generation='c'*32),req)
        with self.assertRaises(ValueError):c.response(req,'reconciled','readback_matches')
        self.assertEqual(c.fingerprint(req),c.fingerprint(dict(reversed(list(req.items())))))

class JournalTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup);self.path=pathlib.Path(self.tmp.name)/'ledger.json';self.req=submit()
    def test_restart_never_replays_queued_or_running_and_preserves_ack(self):
        for state,after in [('queued','rejected'),('running','unknown'),('acknowledged','acknowledged')]:
            j=m.Journal(self.path);j.put(self.req,c.response(self.req,state,'accepted'))
            recovered=m.Journal(self.path);self.assertTrue(recovered.healthy);self.assertEqual(recovered.get(self.req)['receipt']['state'],after)
    def test_corrupt_or_missing_mount_disables_commands_without_reset(self):
        self.path.write_text('broken');j=m.Journal(self.path);self.assertFalse(j.healthy);self.assertEqual(self.path.read_text(),'broken')
        self.assertFalse(m.Journal(self.path/'missing').healthy)
        self.path.unlink();self.path.symlink_to(self.path.parent/'absent')
        self.assertFalse(m.Journal(self.path).healthy);self.assertTrue(self.path.is_symlink())
    def test_fsync_failure_cannot_commit_a_replayable_new_state(self):
        j=m.Journal(self.path);j.put(self.req,c.response(self.req,'queued','accepted'))
        with patch.object(m.os,'fsync',side_effect=OSError('disk')):
            with self.assertRaises(OSError):j.put(self.req,c.response(self.req,'running','accepted'))
        self.assertFalse(j.healthy);self.assertEqual(j.get(self.req)['receipt']['state'],'queued')

class JobsTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup);self.path=pathlib.Path(self.tmp.name)/'ledger.json'
        self.observations=Mock();self.jobs=m.Commands(self.observations,self.path);self.held=(G,10)
        self.policy=patch.object(m,'policy',return_value={W});self.policy.start();self.addCleanup(self.policy.stop)
        self.lease=patch.object(m,'lease',side_effect=lambda _:self.held);self.lease.start();self.addCleanup(self.lease.stop)
    async def finish(self):await self.jobs.task
    async def test_lost_ack_duplicate_submit_and_cross_root_poll_never_redispatch(self):
        calls=[]
        def run(req,before,until):
            before();calls.append(req['request_id']);return c.response(req,'unknown','outcome_unknown')
        self.jobs.backend.execute=run;req=submit()
        self.assertEqual((await self.jobs.handle('deskb1x',req))['state'],'queued');await self.finish()
        for sender in c.NODES:
            self.assertEqual((await self.jobs.handle(sender,req))['state'],'unknown')
            self.assertEqual((await self.jobs.handle(sender,poll(req)))['state'],'unknown')
        self.assertEqual(calls,[req['request_id']]);self.observations.invalidate.assert_called_once_with(W)
    async def test_conflicting_retry_or_generation_is_rejected(self):
        self.jobs.backend.execute=lambda req,before,until:c.response(req,'acknowledged','accepted')
        req=submit();await self.jobs.handle('deskb1x',req);await self.finish()
        for changed in (dict(arguments=dict(x=-15,y=0)),dict(generation='c'*32),dict(deadline=req['deadline']+1)):
            self.assertEqual((await self.jobs.handle('deskb1x',dict(req,**changed)))['reason'],'request_conflict')
    async def test_owner_loss_before_bytes_and_expired_submit_do_not_write(self):
        calls=[]
        def run(req,before,until):
            self.held=('c'*32,10)
            try:before();calls.append('write')
            except ValueError:pass
            return c.response(req,'rejected','not_dispatched')
        self.jobs.backend.execute=run;req=submit();await self.jobs.handle('deskb1x',req);await self.finish();self.assertFalse(calls)
        for changed,reason in [(dict(generation='d'*32),'owner_changed'),(dict(deadline=time.time()-1),'deadline_exceeded')]:
            self.assertEqual((await self.jobs.handle('deskb1x',submit('c',**changed)))['reason'],reason)
    async def test_same_ticket_after_worker_restart_stays_unknown(self):
        req=submit();self.jobs.journal.put(req,c.response(req,'running','accepted'))
        recovered=m.Commands(self.observations,self.path);recovered.backend.execute=Mock(side_effect=AssertionError('No replay'))
        self.assertEqual((await recovered.handle('deskb2x',req))['state'],'unknown');self.assertIsNone(recovered.task)
    async def test_busy_rejects_new_ticket_but_existing_ticket_can_be_polled(self):
        entered=__import__('threading').Event();finish=__import__('threading').Event()
        def run(req,before,until):
            before();entered.set();finish.wait(2);return c.response(req,'acknowledged','accepted')
        self.jobs.backend.execute=run;req=submit();await self.jobs.handle('deskb1x',req)
        await asyncio.to_thread(entered.wait,1)
        try:
            self.assertEqual((await self.jobs.handle('deskb1x',submit('c')))['reason'],'busy')
            self.assertEqual((await self.jobs.handle('deskb2x',poll(req)))['state'],'running')
        finally:finish.set();await self.finish()
    async def test_full_retained_journal_is_backpressure_not_eviction(self):
        with patch.object(m,'CAPACITY',1):
            req=submit();self.jobs.journal.put(req,c.response(req,'unknown','outcome_unknown'))
            self.assertEqual((await self.jobs.handle('deskb1x',submit('c')))['reason'],'capacity')
            self.assertEqual((await self.jobs.handle('deskb1x',req))['state'],'unknown')
    async def test_clock_rollback_cannot_make_a_pruned_old_command_executable_again(self):
        with patch.object(m.time,'time',return_value=1000):
            self.path.unlink();self.jobs=m.Commands(self.observations,self.path)
            old=submit();self.jobs.journal.put(old,c.response(old,'acknowledged','accepted'),new=True)
        with patch.object(m.time,'time',return_value=2000):
            current=submit('c');self.jobs.journal.put(current,c.response(current,'acknowledged','accepted'),new=True)
        self.assertIsNone(self.jobs.journal.get(old))
        with patch.object(m.time,'time',return_value=1005):
            self.assertEqual((await self.jobs.handle('deskb1x',old))['reason'],'clock_untrusted')
            recovered=m.Commands(self.observations,self.path)
            self.assertEqual((await recovered.handle('deskb1x',old))['reason'],'clock_untrusted')
        self.assertIsNone(self.jobs.task)
    async def test_storage_failure_after_dispatch_is_uncertain_and_blocks_new_work(self):
        req=submit()
        def run(req,before,until):before();return c.response(req,'acknowledged','accepted')
        self.jobs.backend.execute=run;await self.jobs.handle('deskb1x',req)
        original=self.jobs.journal.put
        def put(r,v,**kwargs):
            if v['state']=='acknowledged':self.jobs.journal.healthy=False;raise OSError('disk')
            return original(r,v,**kwargs)
        with patch.object(self.jobs.journal,'put',side_effect=put):await self.finish()
        self.assertEqual((await self.jobs.handle('deskb1x',poll(req)))['state'],'unknown')
        self.assertEqual((await self.jobs.handle('deskb1x',submit('c')))['reason'],'storage_unavailable')
    async def test_slow_final_fsync_does_not_block_receipt_polling_or_warm_loop(self):
        entered=__import__('threading').Event();release=__import__('threading').Event();original=self.jobs.journal.commit
        def commit(records):
            if any(r['receipt']['state']=='acknowledged' for r in records.values()):entered.set();release.wait(2)
            return original(records)
        def run(req,before,until):before();return c.response(req,'acknowledged','accepted')
        self.jobs.backend.execute=run;req=submit()
        with patch.object(self.jobs.journal,'commit',side_effect=commit):
            await self.jobs.handle('deskb1x',req);await asyncio.to_thread(entered.wait,1)
            try:
                value=await asyncio.wait_for(self.jobs.handle('deskb2x',poll(req)),.1)
                self.assertEqual(value['state'],'running');self.assertTrue(self.jobs.status()['busy'])
            finally:release.set();await self.finish()

class InvalidationTests(unittest.IsolatedAsyncioTestCase):
    async def test_read_started_before_command_is_not_published_after_it(self):
        worker=Observations();worker.scope=lambda:{W:G};entered=__import__('threading').Event();finish=__import__('threading').Event();calls=[]
        def read(w,g):
            calls.append(w);entered.set();finish.wait(2)
            if len(calls)>1:worker.stopping=True
            return dict(generation=G,snapshot='e'*32,observed_at=time.time())
        with patch.object(worker,'read',side_effect=read):
            worker.start();await asyncio.to_thread(entered.wait,1);worker.invalidate(W);finish.set();await worker.task
        self.assertEqual(len(calls),2);self.assertEqual(worker.reads[W],1)
    async def test_command_does_not_bypass_camera_error_backoff(self):
        worker=Observations();worker.errors[W]=3;worker.due[W]=time.monotonic()+40;old=worker.due[W]
        worker.invalidate(W);self.assertEqual(worker.due[W],old)
if __name__=='__main__':unittest.main()
