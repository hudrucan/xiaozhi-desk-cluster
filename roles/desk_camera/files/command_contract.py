"""Phase 3B: three explicit operations, immutable tickets and bounded receipts."""
import hashlib
import json
import math
import pathlib
import re

HOSTS = {f'tapo_c222_{n}': f'192.168.1.{n}' for n in (160, 161, 163)}


def encoded(value):return json.dumps(value, separators=(',', ':'), allow_nan=False).encode()


def token(value):return isinstance(value, str) and re.fullmatch('[a-f0-9]{32}', value) is not None


def policy(path):
    try:raw = pathlib.Path(path).read_bytes()
    except FileNotFoundError:return frozenset()
    if len(raw) > 4096:raise ValueError('Command policy size')
    value = json.loads(raw)
    if set(value) != {'version', 'workloads'} or type(value['version']) is not int or value['version'] != 1 or not isinstance(value['workloads'], list) or any(not isinstance(w, str) or w not in HOSTS for w in value['workloads']) or len(set(value['workloads'])) != len(value['workloads']):raise ValueError('Command policy')
    return frozenset(value['workloads'])

NODES = frozenset(('deskb1x', 'deskb2x', 'deskb3x'))
MAX_DEADLINE = 20
TERMINAL = frozenset(('acknowledged', 'reconciled', 'unknown', 'rejected'))
REASONS = frozenset(('accepted', 'readback_matches', 'outcome_unknown', 'owner_changed',
                     'deadline_exceeded', 'authority_disabled', 'worker_restarted',
                     'not_dispatched', 'request_conflict', 'unknown_request', 'capacity',
                     'busy', 'storage_unavailable', 'invalid_preset', 'worker_stopping', 'clock_untrusted'))


def arguments(command, value):
    if not isinstance(value, dict):raise ValueError('Command arguments')
    if command == 'ptz_move':
        if set(value) != {'x', 'y'} or any(type(value[k]) is not int or not -360 <= value[k] <= 360 for k in ('x', 'y')) or (value['x'] == 0) == (value['y'] == 0):raise ValueError('Cardinal PTZ step')
    elif command == 'privacy_set':
        if set(value) != {'enabled'} or type(value['enabled']) is not bool:raise ValueError('Privacy target')
    elif command == 'preset_goto':
        if set(value) != {'preset'} or not isinstance(value['preset'], str) or not re.fullmatch('[0-9]{1,3}', value['preset']):raise ValueError('Preset identity')
    else:raise ValueError('Unsupported camera command')
    return value


def request(value, now):
    if not isinstance(value, dict) or len(encoded(value)) > 2048:raise ValueError('Command request')
    value = json.loads(encoded(value));base = {'version', 'workload', 'generation', 'request_id', 'operation'}
    if type(value.get('version')) is not int or value['version'] != 1 or value.get('workload') not in HOSTS or not all(token(value.get(k)) for k in ('generation', 'request_id')):raise ValueError('Command binding')
    if value['operation'] == 'submit':
        if set(value) != base | {'command', 'arguments', 'deadline'}:raise ValueError('Command fields')
        deadline = value['deadline']
        if type(deadline) not in (int, float) or not math.isfinite(deadline) or deadline > now+MAX_DEADLINE:raise ValueError('Command deadline')
        arguments(value['command'], value['arguments'])
    elif value['operation'] != 'result' or set(value) != base:raise ValueError('Command operation')
    return value


def fingerprint(value):return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(',', ':'), allow_nan=False).encode()).hexdigest()


def payload(command, args):
    """These exact wire shapes are pinned to pytapo 3.4.26; no raw SDK RPC."""
    arguments(command, args)
    if command == 'ptz_move':method, params = 'motorMove', {'motor': {'move': {'x_coord': str(args['x']), 'y_coord': str(args['y'])}}}
    elif command == 'privacy_set':method, params = 'setLensMaskConfig', {'lens_mask': {'lens_mask_info': {'enabled': 'on' if args['enabled'] else 'off'}}}
    else:method, params = 'motorMoveToPreset', {'preset': {'goto_preset': {'id': args['preset']}}}
    return dict(method='multipleRequest', params=dict(requests=[dict(method=method, params=params)]))


def response(req, state, reason, observed=None):
    value = {k:req[k] for k in ('version', 'workload', 'generation', 'request_id')}
    value.update(state=state, reason=reason, observed=observed)
    return validate_response(value, req)


def validate_response(value, req):
    if not isinstance(value, dict) or set(value) != {'version', 'workload', 'generation', 'request_id', 'state', 'reason', 'observed'} or len(encoded(value)) > 2048:raise ValueError('Command receipt')
    if type(value['version']) is not int or any(value[k] != req[k] for k in ('version', 'workload', 'generation', 'request_id')):raise ValueError('Command receipt binding')
    if value['state'] not in TERMINAL | {'queued', 'running'} or value['reason'] not in REASONS:raise ValueError('Command result')
    observed = value['observed']
    if observed is not None:
        if not isinstance(observed, dict) or set(observed) != {'privacy', 'observed_at', 'matches'} or any(type(observed[k]) is not bool for k in ('privacy', 'matches')) or type(observed['observed_at']) not in (int, float) or not math.isfinite(observed['observed_at']):raise ValueError('Command readback')
    if value['state'] == 'reconciled' and (not observed or not observed['matches']):raise ValueError('Command reconciliation')
    if req.get('operation') == 'submit' and observed is not None:
        if req['command'] != 'privacy_set' or observed['matches'] != (observed['privacy'] == req['arguments']['enabled']):raise ValueError('Command target readback')
    if value['state'] == 'acknowledged' and value['reason'] != 'accepted' or value['state'] == 'reconciled' and value['reason'] != 'readback_matches':raise ValueError('Command receipt outcome')
    return value
