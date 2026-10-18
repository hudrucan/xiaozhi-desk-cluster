"""Durable at-most-one-dispatch tickets in the existing warm process.

No automatic replay after process/owner loss. A device ACK is not proof of motion.
"""
import asyncio
import contextlib
import functools
import hashlib
import json
import os
import pathlib
import threading
import time

from command_contract import NODES, TERMINAL, fingerprint, payload, policy, request, response, validate_response
from observation_contract import HOSTS
from observation_worker import ReadOnly, create_client as readonly_client, lease

POLICY = pathlib.Path('/policy/commands.json')
LEDGER = pathlib.Path('/commands/ledger.json')
CREDENTIALS = pathlib.Path('/input/credentials.json')
RETENTION = 900
CAPACITY = 128
SDK_HASHES = {
    '__init__.py': '185bbeccb4b87623fc053d44a9f6f78418e604a1f50721f142dffda469d2614f',
    'transport/pytapo/pytapo.py': '9dbb976454fad28c3329f22cd8ddf5a919212db4913eb415e12c4eb15a4e5e3d',
    'transport/transport.py': '170710b3006b76dc22166f0925454f6587b7c337240ae388aec69a08e95b921d',
}


@functools.lru_cache(maxsize=1)
def verify_sdk():
    import pytapo
    root = pathlib.Path(pytapo.__file__).parent
    for name, digest in SDK_HASHES.items():
        if hashlib.sha256((root/name).read_bytes()).hexdigest() != digest:raise ValueError('Unaudited command SDK')


def dispose(client):
    with contextlib.suppress(Exception):client.close()
    loop = getattr(getattr(client, 'asyncHandler', None), '_loop', None)
    if loop is not None and not loop.is_running():loop.close()


class Journal:
    def __init__(self, path):
        self.path = pathlib.Path(path);self.lock = threading.RLock();self.records = {};self.healthy = True;self.wall_floor = 0
        # A corrupt/unreadable ledger disables commands; it must never be reset.
        try:
            if self.path.is_symlink():raise ValueError('Command ledger symlink')
            raw = self.path.read_bytes()
            if len(raw) > 512*1024:raise ValueError('Command ledger size')
            doc = json.loads(raw)
            if set(doc) != {'version', 'wall_floor', 'records'} or type(doc['version']) is not int or doc['version'] != 1 or not isinstance(doc['records'], dict) or len(doc['records']) > CAPACITY or type(doc['wall_floor']) not in (int, float) or not 0 <= doc['wall_floor'] <= 1e12:raise ValueError('Command ledger')
            self.wall_floor = doc['wall_floor']
            for key, record in doc['records'].items():
                req = record['request'];request(req, req['deadline'])
                if key != req['workload']+':'+req['request_id'] or set(record) != {'request', 'receipt'}:raise ValueError('Command ledger identity')
                value = validate_response(record['receipt'], req)
                if value['state'] in ('queued', 'running'):
                    record['receipt'] = response(req, 'unknown' if value['state'] == 'running' else 'rejected', 'worker_restarted')
            self.records = doc['records']
            self.commit(self.records)
        except FileNotFoundError:
            # Missing parent means the durable mount is unavailable, not a fresh ledger.
            if not self.path.parent.is_dir():self.healthy = False
            else:
                try:self.commit({})  # Verify durable write access before readiness.
                except Exception:self.healthy = False
        except Exception:self.healthy = False

    @staticmethod
    def key(req):return req['workload']+':'+req['request_id']

    def commit(self, records):
        tmp = self.path.with_suffix('.new');floor = max(self.wall_floor, time.time())
        try:
            fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW, 0o600)
            with os.fdopen(fd, 'wb') as stream:
                stream.write(json.dumps(dict(version=1, wall_floor=floor, records=records), separators=(',', ':'), allow_nan=False).encode());stream.flush();os.fsync(stream.fileno())
            os.replace(tmp, self.path)
            fd = os.open(self.path.parent, os.O_RDONLY | os.O_DIRECTORY)
            try:os.fsync(fd)
            finally:os.close(fd)
        except Exception:
            self.healthy = False;raise
        self.records = records;self.wall_floor = floor

    def get(self, req):
        # Published dictionaries are immutable; commit swaps the reference only
        # after fsync. Receipt polling must not wait on a disk writer's lock.
        record = self.records.get(self.key(req))
        return json.loads(json.dumps(record)) if record else None

    def put(self, req, receipt, new=False):
        with self.lock:
            if not self.healthy:raise OSError('Command journal unavailable')
            records = dict(self.records)
            if new:
                records = {k:v for k,v in records.items() if v['receipt']['state'] not in TERMINAL or time.time() < v['request']['deadline']+RETENTION}
                if len(records) >= CAPACITY:raise ValueError('Command journal full')
            records[self.key(req)] = dict(request=req, receipt=receipt)
            self.commit(records)


class Backend:
    def __init__(self, permitted):self.permitted = permitted

    def client(self, req, deadline, before):
        verify_sdk()
        from pytapo import Tapo
        secret = json.loads(CREDENTIALS.read_bytes())[req['workload']]
        if secret['ip_address'] != HOSTS[req['workload']]:raise ValueError('Command host binding')
        permitted = self.permitted
        class CommandTapo(ReadOnly, Tapo):
            def __init__(self):
                self._workload, self._generation, self._deadline = req['workload'], req['generation'], deadline
                self._expected = None;self._sent = False;self._calls = 0;self._http_calls = 0
                super().__init__(secret['ip_address'], 'admin', secret['cloud_password'], secret['cloud_password'],
                                 reuseSession=False, retryStok=False, controlPort=secret.get('control_port', 443),
                                 isKLAP=False, transportMethod='pytapo', printWarnInformation=False)
                original = self.transport._request
                def once(method, url, **kwargs):
                    self.guard()
                    if self._expected is not None:
                        # Suppress the SDK's HTTP reset retry too. Login exchanges
                        # are not device writes; only the /ds send marks dispatch.
                        kwargs['transientRetryCount'] = 1
                        if '/ds' in url:
                            if self._http_calls:raise ValueError('Command HTTP replay blocked')
                            before();self._sent = True;self._http_calls += 1
                    return original(method, url, **kwargs)
                self.transport._request = once
                for name in ('_retry_or_return', '_retry_on_exception'):
                    original_retry = getattr(self.transport, name)
                    async def no_retry(*args, _original=original_retry, **kwargs):
                        if self._expected is not None:raise ValueError('Command transport retry blocked')
                        return await _original(*args, **kwargs)
                    setattr(self.transport, name, no_retry)
            def guard(self):
                if not permitted(req) or time.monotonic() >= self._deadline:raise ValueError('Command lease or deadline')
            def performRequest(self, value, *args, **kwargs):
                if self._expected is None:return super().performRequest(value, *args, **kwargs)
                if value != self._expected or self._calls:raise ValueError('Command SDK replay or implicit write blocked')
                self.guard();self._calls += 1
                return Tapo.performRequest(self, value, *args, **kwargs)
        client = CommandTapo.__new__(CommandTapo)
        try:client.__init__();return client
        except BaseException:
            dispose(client)
            raise

    def execute(self, req, before, until):
        client = None;sent = False;ack = False;invalid_preset = False;observed = None
        try:
            client = self.client(req, min(until, time.monotonic()+6), before)
            args = req['arguments'];command = req['command']
            if command == 'preset_goto' and args['preset'] not in client.presets:
                invalid_preset = True;raise ValueError('Unknown preset')
            client._expected = payload(command, args)
            try:
                if command == 'ptz_move':value = client.moveMotor(args['x'], args['y'])
                elif command == 'privacy_set':value = client.setPrivacyMode(args['enabled'])
                else:value = client.setPreset(args['preset'])
                # The pinned SDK returns a result dictionary after checking errors.
                ack = isinstance(value, dict) and value.get('error_code', 0) == 0
            finally:sent = client._sent;client._expected = None
        except Exception:pass
        finally:
            if client:
                dispose(client)
        # A timeout never retries a write. Only an independent read can reconcile
        # an absolute privacy target. Relative motion has no trustworthy readback.
        if sent and req['command'] == 'privacy_set' and self.permitted(req) and time.monotonic() < until:
            reader = None
            try:
                secret = json.loads(CREDENTIALS.read_bytes())[req['workload']]
                reader = readonly_client(req['workload'], req['generation'], secret, min(until, time.monotonic()+4))
                data = reader.executeFunction('getLensMaskConfig', {'lens_mask': {'name': ['lens_mask_info']}})
                enabled = data['lens_mask']['lens_mask_info']['enabled']
                if enabled not in ('on', 'off'):raise ValueError('Privacy readback')
                observed = dict(privacy=enabled == 'on', observed_at=time.time(), matches=(enabled == 'on') == req['arguments']['enabled'])
            except Exception:pass
            finally:
                if reader:
                    dispose(reader)
        if ack:return response(req, 'acknowledged', 'accepted', observed)
        if sent and observed and observed['matches']:return response(req, 'reconciled', 'readback_matches', observed)
        return response(req, 'unknown' if sent else 'rejected', 'outcome_unknown' if sent else 'invalid_preset' if invalid_preset else 'not_dispatched', observed)


class Commands:
    def __init__(self, observations, path=LEDGER):
        self.observations = observations;self.journal = Journal(path);self.task = None;self.closed = False;self.admission = asyncio.Lock()
        self.backend = Backend(self.permitted)

    def permitted(self, req):
        held = lease(req['workload'])
        return not self.closed and self.journal.wall_floor-3 <= time.time() < req['deadline'] and held is not None and held[0] == req['generation'] and held[1] >= 3 and req['workload'] in policy(POLICY)

    async def handle(self, sender, value):
        if sender not in NODES:raise ValueError('Command sender')
        req = request(value, time.time())
        until = time.monotonic()+max(0, req['deadline']-time.time()) if req['operation'] == 'submit' else None
        async with self.admission:return await self.admit(req, until)

    async def admit(self, req, until):
        record = self.journal.get(req)
        if record:
            old = record['request']
            if req['generation'] != old['generation'] or req['operation'] == 'submit' and fingerprint(req) != fingerprint(old):return response(req, 'rejected', 'request_conflict')
            receipt = record['receipt']
            if not self.journal.healthy and receipt['state'] not in TERMINAL:return response(req, 'unknown', 'storage_unavailable')
            return receipt
        if req['operation'] == 'result':return response(req, 'rejected', 'unknown_request')
        reason = ('worker_stopping' if self.closed else 'storage_unavailable' if not self.journal.healthy else
                  'clock_untrusted' if time.time() < self.journal.wall_floor-3 else
                  'deadline_exceeded' if time.time() >= req['deadline'] or time.monotonic() >= until else
                  'authority_disabled' if req['workload'] not in policy(POLICY) else
                  'owner_changed' if not self.permitted(req) else
                  'busy' if self.task and not self.task.done() else None)
        if reason:return response(req, 'rejected', reason)
        value = response(req, 'queued', 'accepted')
        try:await asyncio.to_thread(self.journal.put, req, value, new=True)
        except ValueError:return response(req, 'rejected', 'capacity')
        except OSError:return response(req, 'rejected', 'storage_unavailable')
        self.task = asyncio.create_task(self.run(req, until))
        return value

    async def run(self, req, until):
        def before():
            if not self.permitted(req) or time.monotonic() >= until:raise ValueError('Command fenced')
            self.journal.put(req, response(req, 'running', 'accepted'))
            # A lease may expire while fsync is draining; recheck before bytes.
            if not self.permitted(req) or time.monotonic() >= until:raise ValueError('Command fenced')
        try:
            if not self.permitted(req):value = response(req, 'rejected', 'owner_changed')
            else:value = await asyncio.to_thread(self.backend.execute, req, before, until)
            self.observations.invalidate(req['workload'])
            await asyncio.to_thread(self.journal.put, req, value)
        except Exception:
            # Keep the durable running marker if the final write fails. Never
            # turn an uncertain device operation into a fresh executable ticket.
            self.journal.healthy = False

    def status(self):
        return dict(storage_healthy=self.journal.healthy, clock_healthy=time.time() >= self.journal.wall_floor-3, busy=bool(self.task and not self.task.done()),
                    receipts=len(self.journal.records))

    async def stop(self):
        self.closed = True
        if self.task:await asyncio.shield(self.task)
