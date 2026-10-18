"""Phase-1 metadata contract. No SDK objects, media handles or bytes on the wire."""
import datetime
import hashlib
import json
import math
import re

VERSION = 1
MAX_REQUEST = 2048
MAX_RESPONSE = 32768  # Leave room inside the existing signed 64-KiB NATS envelope.
PAGE_SIZE = 128
MAX_DEADLINE = 30
FRESH_FOR = 30
STALE_FOR = 300
WORKLOADS = frozenset(('tapo_c222_160', 'tapo_c222_161', 'tapo_c222_163'))
SENDERS = frozenset(('deskb1x', 'deskb2x', 'deskb3x'))
READS = frozenset(('dates', 'index'))
REASONS = frozenset(('authority_disabled', 'owner_changed', 'deadline_exceeded',
                     'cancelled', 'request_conflict', 'capacity', 'unknown_request',
                     'camera_busy', 'session_expired', 'storage_unavailable',
                     'index_expired', 'invalid_result', 'worker_unavailable'))


class ContractError(ValueError):
    def __init__(self, code):
        self.code = code
        super().__init__(code)


def encoded(value):
    return json.dumps(value, sort_keys=True, separators=(',', ':'), allow_nan=False).encode()


def token(value):
    return isinstance(value, str) and re.fullmatch('[a-f0-9]{32}', value) is not None


def date(value):
    if not isinstance(value, str) or not re.fullmatch('[0-9]{8}', value):
        raise ValueError('date')
    datetime.datetime.strptime(value, '%Y%m%d')
    return value


def number(value):
    return type(value) in (int, float) and math.isfinite(value)


def request(value, now):
    """Copy and validate before admission. A poll/cancel binds the original ticket."""
    try:
        raw = encoded(value)
        if len(raw) > MAX_REQUEST or not isinstance(value, dict):
            raise ValueError('request')
        value = json.loads(raw)
        base = {'version', 'workload', 'generation', 'request_id', 'operation'}
        if type(value.get('version')) is not int or value['version'] != VERSION:
            raise ValueError('version')
        if value.get('workload') not in WORKLOADS or not all(token(value.get(k)) for k in ('generation', 'request_id')):
            raise ValueError('binding')
        operation = value.get('operation')
        if operation in READS:
            if set(value) != base | {'query', 'deadline'}:
                raise ValueError('fields')
            if not number(value['deadline']) or value['deadline'] > now + MAX_DEADLINE:
                raise ValueError('deadline')
            query = value['query']
            if not isinstance(query, dict) or set(query) != ({'cursor'} if operation == 'dates' else {'date', 'cursor'}):
                raise ValueError('query')
            if query['cursor'] is not None and not token(query['cursor']):
                raise ValueError('cursor')
            if operation == 'index':
                date(query['date'])
        elif operation not in ('result', 'cancel') or set(value) != base:
            raise ValueError('operation')
        return value
    except (ValueError, TypeError, KeyError, OverflowError):
        raise ContractError('invalid_request') from None


def fingerprint(value):
    return hashlib.sha256(encoded(value)).hexdigest()


def page(request, value, now):
    """Validate a backend page without converting camera timestamps to host time."""
    try:
        raw = encoded(value)
        if len(raw) > MAX_RESPONSE or not isinstance(value, dict):
            raise ValueError('page')
        value = json.loads(raw)
        fields = {'items', 'snapshot', 'next_cursor', 'observed_at', 'freshness',
                  'complete', 'storage', 'time_basis', 'camera_offset_seconds',
                  'clock_correction_seconds'}
        if request['operation'] == 'index':
            fields.add('date')
            if value.get('date') != request['query']['date']:
                raise ValueError('date binding')
        if set(value) != fields or not token(value['snapshot']):
            raise ValueError('fields')
        if value['next_cursor'] is not None and not token(value['next_cursor']):
            raise ValueError('cursor')
        if not number(value['observed_at']) or not 0 <= value['observed_at'] <= now + 3:
            raise ValueError('observation')
        if value['freshness'] not in ('fresh', 'stale') or type(value['complete']) is not bool:
            raise ValueError('freshness')
        if now-value['observed_at'] > (FRESH_FOR if value['freshness'] == 'fresh' else STALE_FOR):
            raise ValueError('expired observation')
        if value['complete'] and value['next_cursor'] is not None:
            raise ValueError('incomplete pagination')
        if value['time_basis'] != 'camera_epoch_seconds':
            raise ValueError('units')
        for key in ('camera_offset_seconds', 'clock_correction_seconds'):
            if type(value[key]) is not int or not -(2**31) <= value[key] < 2**31:
                raise ValueError('offset units')
        items = value['items']
        if not isinstance(items, list) or len(items) > PAGE_SIZE:
            raise ValueError('page size')
        if value['storage'] not in ('ready', 'absent', 'needs_attention'):
            raise ValueError('storage')
        if value['storage'] != 'ready' and (items or value['next_cursor'] is not None or not value['complete']):
            raise ValueError('storage data')
        if request['operation'] == 'dates':
            if items != sorted(set(date(d) for d in items)):
                raise ValueError('date ordering')
        else:
            bounds = []
            for clip in items:
                if not isinstance(clip, dict) or set(clip) != {'start', 'end'}:
                    raise ValueError('clip')
                start, end = clip['start'], clip['end']
                if type(start) is not int or type(end) is not int or not 1500000000 <= start < end <= 2200000000 or end-start > 86400:
                    raise ValueError('clip seconds')
                # Date is camera-local; allow adjacent UTC days just as playback.validate does.
                day = datetime.datetime.strptime(value['date'], '%Y%m%d').replace(tzinfo=datetime.timezone.utc).timestamp()
                if not day-86400 <= start <= day+172800:
                    raise ValueError('clip date')
                bounds.append((start, end))
            if bounds != sorted(set(bounds)):
                raise ValueError('clip ordering')
        return value
    except (ValueError, TypeError, KeyError, OverflowError):
        raise ContractError('invalid_result') from None


def response(request, state, *, data=None, reason=None, now=None):
    value = {k: request[k] for k in ('version', 'workload', 'generation', 'request_id')}
    if state not in ('queued', 'running', 'cancelling', 'done', 'failed', 'cancelled'):
        raise ContractError('invalid_result')
    if (state == 'done') != (data is not None) or (state in ('failed', 'cancelled', 'cancelling')) != (reason is not None):
        raise ContractError('invalid_result')
    if reason is not None and reason not in REASONS:
        raise ContractError('invalid_result')
    value['state'] = state
    if data is not None:
        if now is None:
            raise ContractError('invalid_result')
        value['data'] = page(request, data, now)
    if reason is not None:
        value['reason'] = reason
    if len(encoded(value)) > MAX_RESPONSE:
        raise ContractError('invalid_result')
    return json.loads(encoded(value))
