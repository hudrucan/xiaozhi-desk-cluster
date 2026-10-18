"""Owner-only SDK adapter. Handles never leave this warm process."""
import datetime
import json
import pathlib
import secrets
import time

from playback_worker import CoreMediaSession, MediaSessionExpired
from recording_contract import ContractError, PAGE_SIZE, WORKLOADS, date

POLICY = pathlib.Path('/policy/owner.json') if pathlib.Path('/policy').is_dir() else pathlib.Path('/etc/desk-camera/recording-policy/owner.json')
_policies = {}


def selected(path=POLICY):
    try:
        st = path.stat();key = (st.st_ino, st.st_mtime_ns, st.st_size)
        cached = _policies.get(path)
        if cached and cached[0] == key and time.monotonic()-cached[1] < 1:return cached[2]
        raw = path.read_bytes()
    except FileNotFoundError:
        _policies.pop(path, None)
        return frozenset()
    if len(raw) > 4096:
        raise ValueError('Authority policy size')
    value = json.loads(raw)
    if set(value) != {'version', 'workloads'} or type(value['version']) is not int or value['version'] != 1 or not isinstance(value['workloads'], list):
        raise ValueError('Authority policy')
    if not all(isinstance(w, str) and w in WORKLOADS for w in value['workloads']) or len(set(value['workloads'])) != len(value['workloads']):
        raise ValueError('Authority policy cameras')
    result = frozenset(value['workloads'])
    if len(_policies) >= 8:_policies.clear()
    _policies[path] = key, time.monotonic(), result
    return result


def owner_mode(workload):
    return workload in selected()


class OwnerMediaSession:
    expire_media_session = CoreMediaSession.expire_media_session

    def guard(self):
        if self._owner_remaining() < 3:
            raise ContractError('owner_changed')

    def check_media_session(self):
        self.guard()
        return CoreMediaSession.check_media_session(self)

    def getUserID(self, forceReload=False, retry=False):
        self.guard()
        if self._allocating:
            return super().getUserID(forceReload, retry)
        if forceReload or retry:
            self.expire_media_session()
        return self.check_media_session()

    def executeFunction(self, method, *args, **kwargs):
        self.guard()
        if method == 'getUserID' and not self._allocating:
            self.expire_media_session()
        value = super().executeFunction(method, *args, **kwargs)
        self.guard()
        return value

    def performRequest(self, *args, **kwargs):
        self.guard()
        if not getattr(self, '_bounded_transport', False):
            # Only the installed, audited requests-based legacy transport is armed.
            if self.transport.method != 'pytapo':
                raise ContractError('worker_unavailable')
            original = self.transport._request
            def bounded(*args, **kwargs):
                self.guard()
                kwargs['timeout'] = (1, 1)
                value = original(*args, **kwargs)
                self.guard()
                return value
            self.transport._request = bounded
            self._bounded_transport = True
        value = super().performRequest(*args, **kwargs)
        self.guard()
        return value

    def begin_media(self):
        self.guard()
        self._allocating = True
        try:
            uid = self.getUserID(True)
            self._owner_allocations = getattr(self, '_owner_allocations', 0)+1
            CoreMediaSession.bind_media_session(self, {'user_id': uid, 'player_id': self.playerID})
        finally:
            self._allocating = False
        self.check_media_session()


def create_owner_client(remaining, *args, **kwargs):
    from pytapo import Tapo
    class OwnerTapo(OwnerMediaSession, Tapo):
        def __init__(self, *args, **kwargs):
            self._owner_remaining, self._allocating = remaining, False
            super().__init__(*args, **kwargs)
    return OwnerTapo(*args, **kwargs)


async def bind_session(tapo, session, control):
    if isinstance(tapo, OwnerMediaSession):
        if session is not None:
            raise ValueError('Core handle rejected in owner mode')
        await control(tapo.begin_media)
    else:
        tapo.bind_media_session(session)


def sd_state(value):
    statuses = []
    def visit(v):
        if isinstance(v, dict):
            if 'status' in v:statuses.append(str(v['status']).lower())
            for child in v.values():visit(child)
        elif isinstance(v, list):
            for child in v:visit(child)
    visit(value)
    if 'normal' in statuses:return 'ready'
    if statuses and set(statuses) <= {'offline', 'none', 'absent'}:return 'absent'
    if set(statuses) & {'unformatted', 'formatting', 'abnormal', 'error', 'readonly'}:return 'needs_attention'
    raise ContractError('storage_unavailable')


class Reader:
    def __init__(self, worker):
        self.worker, self.snapshots, self.cursors = worker, {}, {}

    async def execute(self, request):
        now = time.time()
        for key, saved in list(self.snapshots.items()):
            if now-saved['observed_at'] > 30:self.snapshots.pop(key)
        self.cursors = {c:v for c,v in self.cursors.items() if v[0] in self.snapshots}
        binding = (request['workload'], request['generation'], request['operation'], request['query'].get('date'))
        cursor = request['query']['cursor']
        if cursor is not None:
            item = self.cursors.get(cursor)
            if not item or item[0] not in self.snapshots:raise ContractError('index_expired')
            snapshot, offset, expected = item
            if binding != expected:raise ContractError('index_expired')
            return self.page(self.snapshots[snapshot], offset, binding)
        cached = next((s for s in self.snapshots.values() if s['_binding'] == binding), None)
        if cached:return self.page(cached, 0, binding)
        tapo = await self.worker.client(request)
        try:
            storage = sd_state(await self.worker.control(tapo.getSDCard))
            clock = await self.worker.control(tapo.getTime)
            offset = round(clock['system']['clock_status']['seconds_from_1970']-time.time())
            items = []
            if storage == 'ready':
                if request['operation'] == 'dates':
                    raw = await self.worker.control(tapo.getRecordingsList)
                    if not isinstance(raw, list) or len(raw) > 4096:raise ContractError('invalid_result')
                    items = sorted({date(str(v['date'])) for row in raw for v in row.values()})
                else:
                    await self.worker.control(tapo.begin_media)
                    raw = await self.worker.control(tapo.getRecordings, request['query']['date'], 0, 2048)
                    if not isinstance(raw,list) or len(raw)>=2048:raise ContractError('invalid_result')
                    rows = [v for row in raw for v in row.values()]
                    if len(rows)>=2048:raise ContractError('invalid_result')
                    for v in rows:
                        if any(type(v[k]) is not int for k in ('startTime','endTime')):raise ContractError('invalid_result')
                        items.append({'start':v['startTime'],'end':v['endTime']})
                    tapo.check_media_session()
                    items = [dict(start=a, end=b) for a,b in sorted({(v['start'],v['end']) for v in items})]
            snapshot = secrets.token_hex(16)
            saved = dict(items=items, snapshot=snapshot, next_cursor=None, observed_at=time.time(), freshness='fresh', complete=True,
                         storage=storage, time_basis='camera_epoch_seconds', camera_offset_seconds=offset,
                         clock_correction_seconds=-offset, _binding=binding)
            if request['operation'] == 'index':saved['date'] = request['query']['date']
            if len(self.snapshots) >= 16:
                self.snapshots.pop(min(self.snapshots, key=lambda k:self.snapshots[k]['observed_at']))
            self.snapshots[snapshot] = saved
            return self.page(saved, 0, binding)
        except MediaSessionExpired:
            raise ContractError('session_expired') from None

    def page(self, saved, offset, binding):
        value = {k:v for k,v in saved.items() if k != '_binding'}
        value['items'] = saved['items'][offset:offset+PAGE_SIZE]
        more = offset+PAGE_SIZE < len(saved['items'])
        value['complete'] = not more
        if more:
            # Stable continuation for a repeated page, without growing per retry.
            cursor = next((c for c,v in self.cursors.items() if v == (saved['snapshot'],offset+PAGE_SIZE,binding)), None)
            if cursor is None:
                if len(self.cursors) >= 256:raise ContractError('capacity')
                cursor = secrets.token_hex(16);self.cursors[cursor] = (saved['snapshot'],offset+PAGE_SIZE,binding)
            value['next_cursor'] = cursor
        return value
