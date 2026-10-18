"""Early getMost adapter. Native mapping, entity registry and command SDK remain."""
import asyncio
import functools
import importlib.util
import json
import pathlib
import socket
import threading
import time

# Also loaded as a standalone sitecustomize module, before HA imports.
_spec = importlib.util.spec_from_file_location('_desk_observation_contract', pathlib.Path(__file__).with_name('observation_contract.py'))
contract = importlib.util.module_from_spec(_spec);_spec.loader.exec_module(contract)
SOCKET = '/run/desk-cluster/bridge.sock'


def policy(hass):return contract.policy(hass.config.path('.storage/desk_camera_observation_authority'))


def rpc(value):
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as s:
        s.settimeout(3);s.connect(SOCKET)
        s.sendall(contract.encoded(dict(action='camera_observation', value=value))+b'\n')
        with s.makefile('rb') as stream:raw = stream.readline(contract.MAX_RESPONSE+1024)
    if len(raw) > contract.MAX_RESPONSE+512:raise ValueError('Observation bridge size')
    v = json.loads(raw)
    if v.get('ok') is not True:raise ValueError('Observation bridge unavailable')
    return v['value']


def forced(client):
    hass = getattr(client, 'hass', None)
    if hass is None:return False
    # Called on the HA loop via run_coroutine_threadsafe, never read HA runtime
    # dictionaries from the SDK executor thread.
    async def inspect():
        return any(isinstance(v, dict) and v.get('lastUpdate') == 0 and v.get('controller') is client
                   for v in hass.data.get('tapo_control', {}).values())
    return asyncio.run_coroutine_threadsafe(inspect(), hass.loop).result(2)


def install(hass):
    from pytapo import Tapo
    hosts = {contract.HOSTS[w]:w for w in policy(hass)}
    existing = getattr(Tapo, '_desk_observation_guard', None)
    if existing is not None:
        if existing['hosts'] != hosts:raise ValueError('Observation policy requires Core restart')
        return existing
    state = dict(hosts=hosts, calls=0, failed=0, before_native_setup=False)
    original = Tapo.getMost
    @functools.wraps(original)
    def get_most(client, omit_methods=None, chn_id=None):
        workload = hosts.get(getattr(client, 'host', None))
        if workload is None:return original(client, [] if omit_methods is None else omit_methods, chn_id)
        if omit_methods or getattr(client, 'childID', None) or getattr(client, 'isKLAP', False):raise ValueError('Unsupported owner status scope')
        lock = getattr(client, '_desk_status_lock', None)
        if lock is None:lock = client._desk_status_lock = threading.Lock()
        with lock:
            if time.monotonic() < getattr(client, '_desk_status_retry', 0):raise ValueError('Owner status retry deferred')
            after = getattr(client, '_desk_status_snapshot', None) if getattr(client, '_desk_command_refresh', False) or forced(client) else None
            deadline = time.monotonic()+9
            try:
                while time.monotonic() < deadline:
                    v = rpc(dict(workload=workload, after=after))
                    expected = dict(version=1, workload=workload, generation=v['generation'], after=after)
                    if v.get('state') == 'ready':
                        contract.snapshot(v, expected, time.time())
                        if v['channels'] != (chn_id or []):raise ValueError('Owner status channels differ')
                        data = v['data']
                        # getMost also refreshes these SDK-local fields used by commands.
                        if data['getPresetConfig'][0]:client.presets = client.processPresetsResponse(data['getPresetConfig'][0])
                        client.basicInfo = data['getDeviceInfo'];client._desk_status_snapshot = v['snapshot']
                        client._desk_status_observed_at = v['observed_at'];state['calls'] += 1
                        client._desk_command_refresh = False
                        return data
                    if v.get('state') != 'pending':raise ValueError('Owner status unavailable')
                    time.sleep(.25)
                raise TimeoutError('Owner status deadline')
            except Exception:
                state['failed'] += 1;client._desk_status_retry = time.monotonic()+3
                raise ValueError('Owner status unavailable') from None
    Tapo.getMost = get_most;Tapo._desk_observation_guard = state
    return state


def status(hass):
    from pytapo import Tapo
    selected = policy(hass);state = getattr(Tapo, '_desk_observation_guard', {})
    return dict(version=1, workloads=sorted(selected),
                guarded=state.get('hosts') == {contract.HOSTS[w]:w for w in selected},
                before_native_setup=state.get('before_native_setup') is True,
                owner_reads=state.get('calls', 0), failed_reads=state.get('failed', 0))
