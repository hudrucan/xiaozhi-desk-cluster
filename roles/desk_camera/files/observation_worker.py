"""Single bounded read lane in the existing warm runtime; standby never logs in."""
import asyncio
import contextlib
import json
import pathlib
import secrets
import time

from observation_contract import HOSTS, PERIOD, FRESH_FOR, batch, channels, policy, read_request, request

POLICY = pathlib.Path('/policy/observations.json')
LEASE = pathlib.Path('/lease')


def lease(workload):
    path = LEASE if workload == 'tapo_c222_160' else LEASE/workload
    try:
        v = json.loads((path/'permit.json').read_bytes())
        remaining = v['until']-time.clock_gettime(time.CLOCK_BOOTTIME)
        if not isinstance(v['generation'], str) or len(v['generation']) != 32 or not 0 < remaining <= 11:return None
        return v['generation'], remaining
    except (OSError, ValueError, KeyError, TypeError):return None


class ReadOnly:
    def guard(self):
        current = lease(self._workload)
        if not current or current[0] != self._generation or current[1] < 3 or time.monotonic() >= self._deadline or self._workload not in policy(POLICY):raise ValueError('Observation lease or deadline')

    def performRequest(self, value, *args, **kwargs):
        read_request(value);self.guard()
        if not getattr(self, '_bounded_observation', False):
            if self.transport.method != 'pytapo':raise ValueError('Unaudited status transport')
            original = self.transport._request
            def bounded(*args, **kwargs):
                self.guard();kwargs['timeout'] = (1, 1)
                result = original(*args, **kwargs)
                self.guard();return result
            self.transport._request = bounded;self._bounded_observation = True
        result = super().performRequest(value, *args, **kwargs)
        self.guard();return result

    def getUserID(self, *args, **kwargs):raise ValueError('Read-only camera adapter')
    def getMediaSession(self, *args, **kwargs):raise ValueError('Read-only camera adapter')


def create_client(workload, generation, secret, deadline):
    from pytapo import Tapo
    if secret['ip_address'] != HOSTS[workload]:raise ValueError('Observation host binding')
    class StatusTapo(ReadOnly, Tapo):
        def __init__(self):
            self._workload, self._generation, self._deadline = workload, generation, deadline
            super().__init__(secret['ip_address'], 'admin', secret['cloud_password'], secret['cloud_password'],
                             reuseSession=False, retryStok=False, controlPort=secret.get('control_port', 443),
                             isKLAP=False, transportMethod='pytapo', printWarnInformation=False)
    client = StatusTapo.__new__(StatusTapo)
    try:
        client.__init__();return client
    except BaseException:
        with contextlib.suppress(Exception):client.close()
        raise


class Observations:
    def __init__(self):
        self.clients = {};self.values = {};self.due = {};self.attempts = {};self.errors = {};self.reads = {};self.epochs = {}
        self.wake = asyncio.Event();self.stopping = False;self.task = None

    def start(self):self.task = asyncio.create_task(self.run())

    def invalidate(self, workload):
        # Called on the warm event loop after a command drains. A read started
        # before that command must not be published as its fresh readback.
        self.epochs[workload] = self.epochs.get(workload, 0)+1
        self.values.pop(workload, None)
        if workload not in self.errors:self.due[workload] = 0
        self.wake.set()

    def scope(self):
        try:selected = policy(POLICY)
        except (OSError, ValueError):selected = frozenset()
        return {w:held[0] for w in selected if (held := lease(w))}

    @staticmethod
    def close(client):
        with contextlib.suppress(Exception):client.close()

    def read(self, workload, generation):
        deadline = time.monotonic()+6
        client = self.clients.get(workload)
        if client is None:
            secrets_by_camera = json.loads(pathlib.Path('/input/credentials.json').read_bytes())
            client = create_client(workload, generation, secrets_by_camera[workload], deadline)
            self.clients[workload] = client
        client._deadline = deadline;client.guard()
        # Channel discovery is a fixed read, never caller-supplied SDK arguments.
        try:raw_channels = client.getAllChnInfo()['system']['chn_info']
        except Exception:
            client.guard();raw_channels = []
        ch = channels([v['chn_id'] for v in raw_channels])
        data = batch(client.getMost([], ch))
        client.guard()
        return dict(version=1, workload=workload, generation=generation, state='ready',
                    snapshot=secrets.token_hex(16), observed_at=time.time(), channels=ch, data=data)

    async def run(self):
        try:
            while not self.stopping:
                active = self.scope()
                # Only this loop mutates/closes clients, after the read thread drains.
                for w, client in list(self.clients.items()):
                    if active.get(w) != client._generation:
                        self.close(self.clients.pop(w));self.values.pop(w, None);self.due.pop(w, None)
                for w in list(self.values):
                    if active.get(w) != self.values[w]['generation']:self.values.pop(w, None)
                candidates = [w for w in active if time.monotonic() >= self.due.get(w, 0)]
                if candidates:
                    w = min(candidates, key=lambda k:self.due.get(k, 0));g = active[w]
                    self.attempts[w] = time.monotonic();epoch = self.epochs.get(w, 0)
                    read = asyncio.create_task(asyncio.to_thread(self.read, w, g))
                    try:
                        value = await asyncio.shield(read)
                        if self.scope().get(w) == g and epoch == self.epochs.get(w, 0):
                            self.values[w] = value;self.reads[w] = self.reads.get(w, 0)+1;self.errors.pop(w, None)
                        self.due[w] = time.monotonic()+PERIOD if epoch == self.epochs.get(w, 0) else 0
                    except asyncio.CancelledError:
                        with contextlib.suppress(Exception):await read
                        raise
                    except Exception:
                        client = self.clients.pop(w, None)
                        if client:self.close(client)
                        self.errors[w] = min(6, self.errors.get(w, 0)+1)
                        self.due[w] = time.monotonic()+min(60, 2**self.errors[w])
                    continue
                self.wake.clear()
                try:await asyncio.wait_for(self.wake.wait(), 1)
                except TimeoutError:pass
        finally:
            for client in self.clients.values():self.close(client)
            self.clients.clear()

    def handle(self, v):
        v = request(v);w = v['workload'];g = v['generation']
        if self.scope().get(w) != g:return dict(version=1, workload=w, generation=g, state='unavailable')
        saved = self.values.get(w)
        if saved and saved['generation'] == g and saved['snapshot'] != v['after'] and 0 <= time.time()-saved['observed_at'] <= FRESH_FOR:return saved
        # Coalesce refreshes; at most one status read thread on the entire node.
        # Camera errors retain their backoff even if native HA retries every second.
        if w not in self.errors and time.monotonic()-self.attempts.get(w, -10) >= FRESH_FOR:
            self.due[w] = 0;self.wake.set()
        return dict(version=1, workload=w, generation=g, state='pending')

    def status(self):
        active = self.scope()
        return {w:dict(owned=active.get(w) is not None, reads=self.reads.get(w, 0),
                       failures=self.errors.get(w, 0),
                       age_seconds=round(time.time()-self.values[w]['observed_at'], 3) if w in self.values and self.values[w]['generation'] == active.get(w) else None)
                for w in HOSTS}

    async def stop(self):
        self.stopping = True;self.wake.set()
        if self.task:await self.task
