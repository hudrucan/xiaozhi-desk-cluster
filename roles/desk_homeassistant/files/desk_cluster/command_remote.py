"""Native PTZ/preset/privacy methods use immutable owner command tickets.

Other native commands remain on Core. Loaded before native setup; no SDK I/O.
"""
import functools
import importlib.util
import json
import pathlib
import secrets
import socket
import threading
import time

_spec = importlib.util.spec_from_file_location('_desk_command_contract', pathlib.Path(__file__).with_name('command_contract.py'))
contract = importlib.util.module_from_spec(_spec);_spec.loader.exec_module(contract)
SOCKET = '/run/desk-cluster/bridge.sock'


def policy(hass):return contract.policy(hass.config.path('.storage/desk_camera_command_authority'))


def rpc(action, value=None):
    envelope = dict(action=action)
    if value is not None:envelope['value'] = value
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as s:
        s.settimeout(3);s.connect(SOCKET);s.sendall(contract.encoded(envelope)+b'\n')
        with s.makefile('rb') as stream:raw = stream.readline(34000)
    if len(raw) > 32768:raise ValueError('Command bridge size')
    result = json.loads(raw)
    if result.get('ok') is not True:raise ValueError('Command bridge unavailable')
    return result['value']


def execute(workload, command, arguments):
    contract.arguments(command, arguments)
    fleet = rpc('cameras')
    camera = next((v for v in fleet['cameras'] if v.get('workload') == workload and v.get('ready') and v.get('owner') in contract.NODES and contract.token(v.get('generation'))), None)
    if camera is None:raise ValueError('Camera command owner unavailable; no command submitted')
    req = contract.request(dict(version=1, workload=workload, generation=camera['generation'], request_id=secrets.token_hex(16),
                                operation='submit', command=command, arguments=arguments, deadline=time.time()+18), time.time())
    route = dict(workload=workload, node=camera['owner']);end = time.monotonic()+18
    poll = {k:req[k] for k in ('version', 'workload', 'generation', 'request_id')};poll['operation'] = 'result'
    # Exactly one submit, even if its response is lost. Poll the same original
    # node/generation; unknown_request is not evidence that a delayed send failed.
    try:reply = rpc('camera_command', dict(route, request=req))
    except Exception:reply = None
    while True:
        if reply is not None:
            contract.validate_response(reply, req)
            if reply['state'] in contract.TERMINAL and reply['reason'] != 'unknown_request':return reply
        if time.monotonic() >= end:return contract.response(req, 'unknown', 'outcome_unknown')
        time.sleep(.25)
        try:reply = rpc('camera_command', dict(route, request=poll))
        except Exception:reply = None


def install(hass):
    from pytapo import Tapo
    hosts = {contract.HOSTS[w]:w for w in policy(hass)}
    existing = getattr(Tapo, '_desk_command_guard', None)
    if existing is not None:
        if existing['hosts'] != hosts:raise ValueError('Command policy requires clean Core restart')
        return existing
    state = dict(hosts=hosts, before_native_setup=False, acknowledged=0, reconciled=0, unknown=0, rejected=0, blocked_core_calls=0)

    def invoke(client, command, arguments):
        lock = getattr(client, '_desk_command_lock', None)
        if lock is None:lock = client._desk_command_lock = threading.Lock()
        if not lock.acquire(blocking=False):raise ValueError('Camera command already pending')
        try:
            client._desk_command_receipt = None
            reply = execute(hosts[client.host], command, arguments);state[reply['state']] += 1
            client._desk_command_receipt = reply
            if reply['state'] not in ('acknowledged', 'reconciled'):
                raise ValueError('Camera command '+reply['state']+' ('+reply['reason']+'); no automatic resend')
            return dict(error_code=0)
        finally:
            client._desk_command_refresh = True
            lock.release()

    originals = {name:getattr(Tapo, name) for name in ('moveMotor', 'setPreset', 'setPrivacyMode')}
    @functools.wraps(originals['moveMotor'])
    def move(client, x, y):
        if getattr(client, 'host', None) not in hosts:return originals['moveMotor'](client, x, y)
        return invoke(client, 'ptz_move', dict(x=x, y=y))
    @functools.wraps(originals['setPreset'])
    def preset(client, presetID, retry=False):
        if getattr(client, 'host', None) not in hosts:return originals['setPreset'](client, presetID, retry)
        if retry is not False:raise ValueError('Owner command cannot be retried by SDK')
        return invoke(client, 'preset_goto', dict(preset=str(presetID)))
    @functools.wraps(originals['setPrivacyMode'])
    def privacy(client, enabled):
        if getattr(client, 'host', None) not in hosts:return originals['setPrivacyMode'](client, enabled)
        return invoke(client, 'privacy_set', dict(enabled=enabled))
    Tapo.moveMotor, Tapo.setPreset, Tapo.setPrivacyMode = move, preset, privacy

    # Fail closed if a native/raw path tries the selected wire writes directly.
    forbidden = frozenset(('motorMove', 'motorMoveToPreset', 'setLensMaskConfig'))
    def contains(value):
        if isinstance(value, dict):return (isinstance(value.get('method'), str) and value['method'] in forbidden) or any(contains(v) for v in value.values())
        if isinstance(value, (tuple, list)):return any(contains(v) for v in value)
        return False
    for name in ('executeFunction', 'performRequest'):
        original = getattr(Tapo, name)
        def wrap(original=original, name=name):
            @functools.wraps(original)
            def guarded(client, *args, **kwargs):
                if getattr(client, 'host', None) in hosts and (name == 'executeFunction' and ((args and args[0] in forbidden) or kwargs.get('method') in forbidden) or contains(args) or contains(kwargs)):
                    state['blocked_core_calls'] += 1;raise ValueError('Command is managed by its camera owner')
                return original(client, *args, **kwargs)
            return guarded
        setattr(Tapo, name, wrap())
    Tapo._desk_command_guard = state
    return state


def status(hass):
    from pytapo import Tapo
    selected = policy(hass);state = getattr(Tapo, '_desk_command_guard', {})
    return dict(version=1, workloads=sorted(selected),
                guarded=state.get('hosts') == {contract.HOSTS[w]:w for w in selected},
                before_native_setup=state.get('before_native_setup') is True,
                **{k:state.get(k, 0) for k in ('acknowledged', 'reconciled', 'unknown', 'rejected', 'blocked_core_calls')})
