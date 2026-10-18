"""Lost replies, owner loss, admission and real cancellation-drain ordering."""
import asyncio
import copy
import pathlib
import sys
import unittest
from unittest.mock import patch

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]/'files'))
from recording_contract import ContractError
from recording_jobs import RecordingJobs
import recording_jobs
from test_recording_contract import NOW, query, result


class JobTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.wall, self.mono = NOW, 100.
        self.generation, self.authority = 'b'*32, True
        self.entered, self.finish, self.cleaning, self.drained = [asyncio.Event() for _ in range(4)]
        self.calls = []
        self.return_after_cancel = False
        self.payload = result()

        async def execute(value):
            self.calls.append(value);self.entered.set()
            try:
                await self.finish.wait()
            except asyncio.CancelledError:
                self.cleaning.set();await self.drained.wait()
                if not self.return_after_cancel:raise
            return copy.deepcopy(self.payload)

        self.service = RecordingJobs(execute,
            lambda workload, generation: generation == self.generation,
            owner_authorized=lambda workload, generation: self.authority,
            wall_clock=lambda: self.wall, monotonic=lambda: self.mono,
            poll_interval=.001)

    async def asyncTearDown(self):
        self.drained.set();self.finish.set();await self.service.close()

    def submit(self, value=None, sender='deskb3x'):
        return self.service.handle(sender, value or query())

    def command(self, operation='result', value=None, sender='deskb3x'):
        value = {k: v for k, v in (value or query()).items() if k not in ('query', 'deadline')}
        return self.service.handle(sender, dict(value, operation=operation))

    async def started(self):
        await asyncio.wait_for(self.entered.wait(), 2)

    async def done(self):
        await asyncio.wait_for(asyncio.gather(*(j['task'] for j in self.service.jobs.values()), return_exceptions=True), 2)

    async def test_default_gate_is_disabled_and_never_calls_backend(self):
        service = RecordingJobs(self.service.execute, lambda *a: True, wall_clock=lambda: NOW)
        self.assertEqual(service.handle('deskb3x', query())['reason'], 'authority_disabled')
        self.assertFalse(service.jobs);self.assertFalse(self.calls);await service.close()

    async def test_submit_returns_before_sdk_and_lost_reply_retry_runs_once(self):
        value = query();self.assertEqual(self.submit(value)['state'], 'queued');self.assertFalse(self.calls)
        value['query']['cursor'] = 'e'*32  # Caller mutation cannot alter an admitted request.
        await self.started()
        self.assertEqual(self.submit()['state'], 'running');self.assertEqual(len(self.calls), 1)
        self.finish.set();await self.done()
        first = self.command();first['data']['items'].clear()
        self.assertEqual(self.submit()['data']['items'], result()['items']);self.assertEqual(len(self.calls), 1)

    async def test_same_id_changed_query_conflicts_without_replacing_original_job(self):
        self.submit();await self.started();value = query();value['query']['cursor'] = 'd'*32
        self.assertEqual(self.submit(value)['reason'], 'request_conflict');self.assertEqual(len(self.calls), 1)

    async def test_request_lookup_is_scoped_to_sender_workload_and_generation(self):
        self.submit();await self.started()
        self.assertEqual(self.command('cancel', sender='deskb2x')['reason'], 'unknown_request')
        self.assertEqual(self.command('cancel', dict(query(), workload='tapo_c222_160'))['reason'], 'unknown_request')
        self.assertEqual(self.command('cancel', query(generation='c'))['reason'], 'unknown_request')
        self.assertFalse(self.cleaning.is_set())
        for sender in ('browser', [], None):
            with self.assertRaises(ContractError):self.submit(sender=sender)

    async def test_cancel_is_not_acknowledged_until_backend_has_actually_drained(self):
        self.submit();await self.started()
        self.assertEqual(self.command('cancel')['state'], 'cancelling')
        await asyncio.wait_for(self.cleaning.wait(), 2)
        self.assertTrue(self.service.admission.locked())
        self.assertEqual(self.command()['state'], 'cancelling')
        self.assertEqual(self.command('cancel')['state'], 'cancelling')
        second = query(ident='c');self.submit(second);await asyncio.sleep(0)
        self.assertEqual(len(self.calls), 1)
        self.drained.set();self.finish.set();await self.done()
        self.assertEqual(self.command()['state'], 'cancelled')
        self.assertEqual(self.command(value=second)['state'], 'done');self.assertEqual(len(self.calls), 2)

    async def test_cancelled_queued_ticket_never_starts_sdk(self):
        self.submit();await self.started();second = query(ident='c');self.submit(second)
        self.assertEqual(self.command('cancel', second)['state'], 'cancelled')
        self.finish.set();await self.done()
        self.assertEqual(self.command(value=second)['state'], 'cancelled');self.assertEqual(len(self.calls), 1)

    async def test_old_generation_queued_before_handoff_cannot_start_after_drain(self):
        self.submit();await self.started();second = query(ident='c');self.submit(second)
        self.generation = 'd'*32
        await asyncio.wait_for(self.cleaning.wait(), 2)
        self.drained.set();await self.done()
        for value in (query(), second):self.assertEqual(self.command(value=value)['reason'], 'owner_changed')
        self.assertEqual(len(self.calls), 1)

    async def test_owner_loss_suppresses_even_a_late_success_and_old_cancel_cannot_touch_successor(self):
        self.return_after_cancel = True
        self.submit();await self.started();self.generation = 'c'*32
        await asyncio.wait_for(self.cleaning.wait(), 2)
        successor = query(generation='c');self.submit(successor)
        self.assertEqual(len(self.calls), 1);self.drained.set();self.finish.set();await self.done()
        self.assertEqual(self.command()['reason'], 'owner_changed');self.assertNotIn('data', self.command())
        self.command('cancel')
        self.assertEqual(self.command(value=successor)['state'], 'done');self.assertEqual(len(self.calls), 2)

    async def test_authority_revocation_cancels_without_waiting_for_a_poll(self):
        self.submit();await self.started();self.authority = False
        await asyncio.wait_for(self.cleaning.wait(), 2)
        self.drained.set();await self.done()
        self.assertEqual(self.command()['reason'], 'authority_disabled')

    async def test_deadline_remains_bounded_when_wall_clock_moves_backwards(self):
        self.submit();await self.started();self.wall -= 100;self.mono += 31
        await asyncio.wait_for(self.cleaning.wait(), 2)
        self.drained.set();await self.done()
        self.assertEqual(self.command()['reason'], 'deadline_exceeded')

    async def test_completed_payload_is_not_served_after_owner_change(self):
        self.finish.set();self.submit();await self.done();self.generation = 'c'*32
        value = self.command();self.assertEqual(value['reason'], 'owner_changed');self.assertNotIn('data', value)

    async def test_owner_is_rechecked_after_response_validation(self):
        self.finish.set();self.submit();await self.done()
        original = recording_jobs.response
        def serialized(*args, **kwargs):
            value = original(*args, **kwargs)
            if kwargs.get('data') is not None:self.generation = 'c'*32
            return value
        with patch.object(recording_jobs, 'response', side_effect=serialized):value = self.command()
        self.assertEqual(value['reason'], 'owner_changed');self.assertNotIn('data', value)

    async def test_cached_page_becomes_stale_and_cannot_outlive_snapshot_budget(self):
        self.payload['observed_at'] = NOW-29
        self.finish.set();self.submit();await self.done();self.wall += 2;self.mono += 2
        self.assertEqual(self.command()['data']['freshness'], 'stale')
        # A backend may explicitly return an older, stale page while media is busy.
        second = query(ident='c');second['deadline'] = self.wall+30
        self.payload.update(observed_at=self.wall-299, freshness='stale')
        self.submit(second);await self.done();self.wall += 2;self.mono += 2
        self.assertEqual(self.command(value=second)['reason'], 'index_expired')

    async def test_shared_playback_admission_blocks_metadata_and_queued_shutdown_does_not_wait_for_it(self):
        shared = asyncio.Lock();self.service.admission = shared
        async with shared:
            self.submit();await asyncio.sleep(0)
            self.assertFalse(self.calls)
            await asyncio.wait_for(self.service.close(), 2)
            self.assertFalse(self.calls)
        self.assertEqual(self.command()['reason'], 'worker_unavailable')

    async def test_playback_preempts_queued_reads_but_waits_for_running_sdk_cleanup(self):
        self.submit();await self.started();second = query(ident='c');self.submit(second)
        pause = asyncio.create_task(self.service.pause())
        await asyncio.wait_for(self.cleaning.wait(), 2)
        self.assertFalse(pause.done());self.assertTrue(self.service.admission.locked())
        self.assertEqual(self.submit(query(ident='d'))['reason'], 'camera_busy')
        self.drained.set();await asyncio.wait_for(pause, 2)
        self.assertFalse(self.service.admission.locked());self.assertEqual(len(self.calls), 1)
        self.assertEqual(self.command(value=second)['reason'], 'camera_busy')
        self.service.resume();self.finish.set();self.submit(query(ident='d'));await self.done()
        self.assertEqual(len(self.calls), 2)

    async def test_completed_metadata_can_still_be_read_during_playback_without_sdk(self):
        self.finish.set();self.submit();await self.done();await self.service.pause()
        self.assertEqual(self.command()['state'], 'done');self.assertEqual(len(self.calls), 1)
        self.assertEqual(self.submit(query(ident='c'))['reason'], 'camera_busy')

    async def test_unreadable_owner_permit_fails_closed_without_leaking_backend_error(self):
        def broken(*args):raise OSError('private permit path')
        self.service.permitted = broken
        value = self.submit();self.assertEqual(value['reason'], 'worker_unavailable')
        self.assertNotIn('private', str(value));self.assertFalse(self.calls)

    async def test_capacity_retains_live_retry_tombstones_and_expired_submit_cannot_restart(self):
        self.service.capacity = 1;self.finish.set();self.submit();await self.done()
        self.assertEqual(self.submit(query(ident='c'))['reason'], 'capacity')
        self.wall += 31;self.mono += 31
        fresh = query(ident='c');fresh['deadline'] = self.wall+30
        self.assertEqual(self.submit(fresh)['state'], 'queued');await self.done()
        self.assertEqual(self.submit()['reason'], 'deadline_exceeded');self.assertEqual(len(self.calls), 2)

    async def test_invalid_backend_output_and_exceptions_do_not_escape_into_metadata(self):
        self.payload['session'] = 'private-handle';self.finish.set();self.submit();await self.done()
        value = self.command();self.assertEqual(value['reason'], 'invalid_result');self.assertNotIn('private', str(value))
        async def failure(value):raise RuntimeError('private camera password and handle')
        self.service.execute = failure;second = query(ident='c');self.submit(second);await self.done()
        value = self.command(value=second);self.assertEqual(value['reason'], 'worker_unavailable');self.assertNotIn('private', str(value))

    async def test_typed_session_expiry_is_not_retried_or_rendered_as_an_empty_card(self):
        async def expired(value):self.calls.append(value);raise ContractError('session_expired')
        self.service.execute = expired;self.submit();await self.done()
        value = self.command();self.assertEqual(value['reason'], 'session_expired');self.assertNotIn('data', value)
        self.submit();self.assertEqual(len(self.calls), 1)

    async def test_shutdown_waits_for_sdk_cleanup_and_rejects_new_work(self):
        self.submit();await self.started();close = asyncio.create_task(self.service.close())
        await asyncio.wait_for(self.cleaning.wait(), 2);self.assertFalse(close.done())
        self.assertEqual(self.submit(query(ident='c'))['reason'], 'worker_unavailable')
        self.drained.set();await asyncio.wait_for(close, 2)
        self.assertFalse(self.service.admission.locked());self.assertEqual(self.command()['reason'], 'worker_unavailable')


if __name__ == '__main__':
    unittest.main()
