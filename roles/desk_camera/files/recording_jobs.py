"""Bounded metadata tickets outside the lease loop, gated by owner authority.

The SDK backend must drain executor calls on cancellation. Production activation
requires the coordinated Core guard and worker policy cutover.
"""
import asyncio
import time

from recording_contract import (ContractError, FRESH_FOR, STALE_FOR, READS, REASONS, SENDERS,
                                fingerprint, request, response)


class RecordingJobs:
    def __init__(self, execute, permitted, *, owner_authorized=None,
                 wall_clock=time.time, monotonic=time.monotonic,
                 capacity=32, poll_interval=.25, admission=None):
        self.execute, self.permitted = execute, permitted
        self.owner_authorized = owner_authorized or (lambda workload, generation: False)
        self.wall_clock, self.monotonic = wall_clock, monotonic
        self.capacity, self.poll_interval = capacity, poll_interval
        self.jobs = {}
        self.admission = admission if admission is not None else asyncio.Lock()
        self.closed = False
        self.paused = False

    def fence(self, job):
        value = job['request']
        if job.get('reason'):
            return job['reason']
        if self.closed:
            return 'worker_unavailable'
        try:
            if self.owner_authorized(value['workload'], value['generation']) is not True:
                return 'authority_disabled'
            if self.permitted(value['workload'], value['generation']) is not True:
                return 'owner_changed'
        except Exception:
            return 'worker_unavailable'
        if self.wall_clock() >= value['deadline'] or self.monotonic() >= job['until']:
            return 'deadline_exceeded'
        return None

    def stop(self, job, reason):
        if job['state'] in ('done', 'failed', 'cancelled'):
            return
        queued = job['state'] == 'queued'
        job.setdefault('reason', reason)  # A late cancel must not hide owner loss.
        job['state'] = 'cancelling'
        if queued:
            # No backend has started, even if a different subsystem holds admission.
            job['task'].cancel()
            job['state'] = 'cancelled' if job['reason'] == 'cancelled' else 'failed'
            return
        work = job.get('work')
        if work and not work.done() and not work.cancelling():
            work.cancel()

    def view(self, job):
        reason = self.fence(job)
        if reason and job['state'] not in ('failed', 'cancelled'):
            if job['state'] == 'done':
                return response(job['request'], 'failed', reason=reason)
            self.stop(job, reason)
        state = job['state']
        data = job.get('data') if state == 'done' else None
        if data is not None:
            age = self.wall_clock()-data['observed_at']
            if age > STALE_FOR:
                return response(job['request'], 'failed', reason='index_expired')
            if age > FRESH_FOR:
                data = dict(data, freshness='stale')
        try:
            value = response(job['request'], state, data=data, now=self.wall_clock(),
                             reason=job.get('reason') if state in ('failed', 'cancelled', 'cancelling') else None)
        except ContractError:
            return response(job['request'], 'failed', reason='invalid_result')
        if data is not None:
            reason = self.fence(job)
            if reason:
                return response(job['request'], 'failed', reason=reason)
        return value

    def handle(self, sender, value):
        """Constant-time admission/poll; authenticated envelope must be checked upstream."""
        if not isinstance(sender, str) or sender not in SENDERS:
            raise ContractError('invalid_request')
        value = request(value, self.wall_clock())
        key = (sender, value['workload'], value['generation'], value['request_id'])
        job = self.jobs.get(key)
        if value['operation'] not in READS:
            if not job:
                return response(value, 'failed', reason='unknown_request')
            if value['operation'] == 'cancel':
                self.stop(job, 'cancelled')
            return self.view(job)
        if job:
            if job['fingerprint'] != fingerprint(value):
                return response(value, 'failed', reason='request_conflict')
            return self.view(job)
        now = self.wall_clock()
        candidate = {'request': value, 'until': self.monotonic()+max(0, value['deadline']-now)}
        reason = self.fence(candidate) or ('camera_busy' if self.paused else None)
        if reason:
            return response(value, 'failed', reason=reason)
        # Retain completed tickets through their entire retry deadline. Expired
        # submissions fail before this prune and cannot re-run a forgotten ticket.
        for old_key, old in list(self.jobs.items()):
            if old['task'].done() and now >= old['request']['deadline']:
                self.jobs.pop(old_key)
        if len(self.jobs) >= self.capacity:
            return response(value, 'failed', reason='capacity')
        job = dict(candidate, fingerprint=fingerprint(value), state='queued')
        self.jobs[key] = job
        job['task'] = asyncio.create_task(self.run(job))
        return self.view(job)

    async def watch(self, job):
        while True:
            reason = self.fence(job) or ('camera_busy' if self.paused else None)
            if reason:
                self.stop(job, reason)
                return
            await asyncio.sleep(self.poll_interval)

    async def run(self, job):
        try:
            async with self.admission:
                reason = self.fence(job) or ('camera_busy' if self.paused else None)
                if reason:
                    job['reason'] = reason
                    raise ContractError(reason)
                job['state'] = 'running'
                # The permit may change during an await, including while queued.
                job['work'] = asyncio.create_task(self.execute(request(job['request'], self.wall_clock())))
                watcher = asyncio.create_task(self.watch(job))
                try:
                    data = await asyncio.shield(job['work'])
                except asyncio.CancelledError:
                    self.stop(job, job.get('reason', 'worker_unavailable'))
                    await asyncio.gather(job['work'], return_exceptions=True)
                    raise
                finally:
                    watcher.cancel()
                    await asyncio.gather(watcher, return_exceptions=True)
                reason = self.fence(job)
                if reason:
                    raise ContractError(reason)
                # Include envelope overhead in the budget before marking success.
                data = response(job['request'], 'done', data=data, now=self.wall_clock())['data']
                reason = self.fence(job)
                if reason:
                    raise ContractError(reason)
                job['data'], job['state'] = data, 'done'
        except (Exception, asyncio.CancelledError) as error:
            # The host backend must finish non-interruptible SDK calls and close
            # media resources before cancellation is acknowledged here.
            reason = job.get('reason')
            if not reason:
                reason = error.code if isinstance(error, ContractError) and error.code in REASONS else 'worker_unavailable'
            job['reason'] = reason
            job['state'] = 'cancelled' if reason == 'cancelled' else 'failed'

    async def pause(self):
        """Trusted host preempts metadata before taking shared playback admission."""
        self.paused = True
        tasks = []
        for job in self.jobs.values():
            if not job['task'].done():
                self.stop(job, 'camera_busy')
                tasks.append(job['task'])
        await asyncio.gather(*tasks, return_exceptions=True)

    def resume(self):
        if not self.closed:
            self.paused = False

    async def close(self):
        self.closed = True
        for job in self.jobs.values():
            self.stop(job, 'worker_unavailable')
        await asyncio.gather(*(j['task'] for j in self.jobs.values()), return_exceptions=True)
