#!/usr/bin/python3
"""One bounded observation; no network configuration or recovery actions."""
import fcntl
import json
import os
import re
import subprocess
import sys
import time

BUDGET = 8.0
GAP = 120
HEARTBEAT = 600
KERNEL = re.compile(r'aic|sdio|deauth|disassoc|firmware|wlan0', re.I)
SECRET = re.compile(r'(?:psk|password|passphrase|wep-key\d*|key_material)\s*[:=]', re.I)


def read(path):
    try:
        with open(path) as stream:
            return stream.read(32768).strip()
    except OSError:
        return ''


def gateway(routes):
    """Read the current IPv4 default with the lowest metric, without spawning ip."""
    found = []
    for line in routes.splitlines()[1:]:
        fields = line.split()
        try:
            if fields[0] == 'wlan0' and fields[1] == '00000000' and fields[7] == '00000000' and int(fields[3], 16) & 3 == 3:
                raw = bytes.fromhex(fields[2])[::-1]
                if len(raw) == 4 and any(raw):
                    found.append((int(fields[6]), '.'.join(str(n) for n in raw)))
        except (ValueError, IndexError):
            continue
    return min(found)[1] if found else None


class Commands:
    def __init__(self):
        self.deadline = time.monotonic() + BUDGET

    def run(self, argv, timeout=0.6):
        remaining = min(timeout, self.deadline - time.monotonic())
        if remaining <= 0:
            return None
        try:
            result = subprocess.run(argv, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                                    text=True, timeout=remaining, check=False,
                                    env={**os.environ, 'LC_ALL': 'C'})
            return result.stdout[:32768] if result.returncode == 0 else None
        except (OSError, subprocess.TimeoutExpired):
            return None

    def pings(self, targets):
        """Launch failure-path pings together; use one shared wait deadline."""
        deadline = min(self.deadline, time.monotonic() + 1.5)
        children = []
        results = []
        try:
            for interface, address in targets:
                if time.monotonic() >= deadline:
                    children.append(None)
                    continue
                try:
                    children.append(subprocess.Popen(
                        ['ping', '-I', interface, '-c1', '-W1', address],
                        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL))
                except OSError:
                    children.append(None)
            for child in children:
                try:
                    results.append(child is not None and child.wait(timeout=max(0.001, deadline-time.monotonic())) == 0)
                except subprocess.TimeoutExpired:
                    results.append(False)
        finally:
            for child in children:
                if child is not None:
                    if child.poll() is None:
                        child.kill()
                    try:
                        child.wait(timeout=0.2)
                    except subprocess.TimeoutExpired:
                        pass  # systemd also owns the complete service cgroup
        return results


def router_ping(commands, address):
    if not address:
        return False
    # stdout is discarded: a successful ping's body is not needed.
    return commands.run(['ping', '-I', 'wlan0', '-c1', '-W1', address], 1.3) is not None


def counters(root):
    values = {}
    for key in ('rx_bytes', 'tx_bytes'):
        value = read(root+'/sys/class/net/wlan0/statistics/'+key)
        values[key] = int(value) if value.isdecimal() else None
    values['wireless'] = read(root+'/proc/net/wireless')[-1024:]
    return values


def classify(router, wlan, mesh):
    return 'ok' if router else 'router_or_icmp' if wlan else 'wlan_path_down' if mesh else 'isolated'


def diagnostics(commands, config, address, since):
    targets = ([('wlan0', address)] if address else [])
    targets += [('wlan0', peer) for peer in config['management_peers']]
    targets += [('end0', peer) for peer in config['mesh_peers']]
    replies = commands.pings(targets)
    retry = bool(address and replies[0])
    offset = int(bool(address))
    wlan = any(replies[offset:offset+2])
    mesh = any(replies[offset+2:offset+4])
    route = commands.run(['ip', 'route', 'show', 'default', 'dev', 'wlan0'])
    neighbor = commands.run(['ip', 'neigh', 'show', address]) if address else ''
    nm = commands.run(['nmcli', '-t', '-f', 'GENERAL.STATE,GENERAL.CONNECTION', 'device', 'show', 'wlan0'], 0.8)
    wifi = commands.run(['nmcli', '-t', '-f', 'IN-USE,BSSID,SIGNAL', 'dev', 'wifi', 'list', 'ifname', 'wlan0', '--rescan', 'no'], 0.8)
    # No journal cache/whole-ring scan: request only the bounded recent window.
    kernel = commands.run(['journalctl', '-k', '-b', '--since', f'@{since:.6f}',
                           '--no-pager', '-n', '80', '-o', 'cat'], 1.0)
    active = next((line for line in (wifi or '').splitlines() if line.startswith('*:')), '')
    # nmcli terse output escapes colons in the BSSID; remove only those escapes.
    match = re.fullmatch(r'\*:((?:[0-9A-Fa-f]{2}\\:){5}[0-9A-Fa-f]{2}):([0-9]{1,3})', active)
    return retry, classify(retry, wlan, mesh), {
        'connected': bool(re.search(r'^GENERAL.STATE:100\b', nm or '', re.M)),
        'bssid': match[1].replace('\\:', ':') if match else None,
        'signal': int(match[2]) if match else None,
        'route': (route or '')[:512], 'neighbor': (neighbor or '')[:512],
        'kernel': [line[:256] for line in (kernel or '').splitlines() if KERNEL.search(line) and not SECRET.search(line)][-20:],
    }


def load(path):
    try:
        value = json.loads(read(path))
        assert isinstance(value, dict) and value['version'] == 1
        assert isinstance(value['boot'], str) and value['boot']
        for key in ('mono', 'wall', 'emitted', 'start', 'kernel_wall'):
            assert type(value[key]) in (int, float) and 0 <= value[key] < 1e14
        for key in ('fails', 'stall'):
            assert type(value[key]) is int and 0 <= value[key] <= 3
        assert type(value['outage']) is bool and type(value['assoc_no_data']) is bool
        assert value['class'] in ('ok', 'router_or_icmp', 'wlan_path_down', 'isolated')
        assert value['rx'] is None or type(value['rx']) is int and value['rx'] >= 0
        return value
    except (OSError, ValueError, KeyError, TypeError, AssertionError):
        return None


def atomic(path, value):
    temporary = path+'.tmp'
    fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    os.fchmod(fd, 0o600)
    try:
        with os.fdopen(fd, 'w') as stream:
            json.dump(value, stream, separators=(',', ':'), allow_nan=False)
            stream.write('\n')
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def observe(config, commands, root='', runtime='/run/desk-wifi-watch', persistent='/var/lib/desk-wifi-watch',
            now=None, wall=None, emit=print, save=atomic):
    uptime = time.clock_gettime(time.CLOCK_BOOTTIME) if now is None else now
    now = time.monotonic() if now is None else now
    wall = time.time() if wall is None else wall
    boot = read(root+'/proc/sys/kernel/random/boot_id')
    old = load(runtime+'/sample.json')
    reset = (not old or old['boot'] != boot or now < old['mono']
             or old['fails'] > 0 and now-old['mono'] > GAP)
    state = dict(old) if old and not reset else {
        'version': 1, 'boot': boot, 'mono': now, 'wall': wall, 'emitted': now,
        'start': now, 'kernel_wall': wall-90, 'fails': 0, 'stall': 0,
        'outage': False, 'class': 'ok', 'rx': None, 'assoc_no_data': False,
    }
    data = counters(root)
    address = gateway(read(root+'/proc/net/route'))
    healthy = router_ping(commands, address)
    details = {}
    classification = 'ok'
    if not healthy:
        # Wall time can jump; derive a fresh bounded window rather than scanning since boot.
        since = max(state['kernel_wall'], wall-120) if wall >= state['kernel_wall'] else wall-90
        healthy, classification, details = diagnostics(commands, config, address, since)
        state['kernel_wall'] = wall
    event = 'observation_reset' if reset else None
    duration = None
    if healthy:
        if state['outage']:
            event, duration = 'outage_end', max(0, now-state['start'])
        state.update(fails=0, stall=0, outage=False, assoc_no_data=False, **{'class':'ok'})
    else:
        if state['fails'] == 0:
            state['start'] = now
        state['fails'] = min(3, state['fails']+1)
        state['stall'] = min(3, state['stall']+1) if data['rx_bytes'] is not None and data['rx_bytes'] == state['rx'] else 1
        associated = details.get('connected', False) and state['stall'] >= 3 and data['rx_bytes'] is not None
        if state['fails'] >= 3:
            if not state['outage']:
                event = 'outage_start'
            elif state['class'] != classification or state['assoc_no_data'] != associated:
                event = 'outage_change'
            state.update(outage=True, assoc_no_data=associated, **{'class':classification})
    if event is None and now-state['emitted'] >= HEARTBEAT:
        event = 'heartbeat'
    if event:
        state['emitted'] = now
        message = dict(event=event, classification=classification, confirmed_outage=state['outage'],
                       failures=state['fails'], boot=boot, wall=wall, monotonic=now, uptime=uptime,
                       gateway=address, assoc_no_data=state['assoc_no_data'], **data)
        if duration is not None:
            message['duration'] = duration
        message.update(details)
        emit(' '.join(key+'='+json.dumps(value, separators=(',', ':'), ensure_ascii=True)
                      for key, value in message.items()))
    # /run is tmpfs: only failed samples, recovery, reset or heartbeat write it.
    if not healthy or reset or event or old and old['fails']:
        state.update(mono=now, wall=wall, rx=data['rx_bytes'])
        save(runtime+'/sample.json', state)
    if event:
        save(persistent+'/state.json', state)
    return 0


def main():
    os.umask(0o077)
    with open(sys.argv[1] if len(sys.argv) > 1 else '/etc/desk-wifi-watch.json') as stream:
        config = json.load(stream)
    # Accept only physical peer IPv4 addresses, never VIPs, DNS names or shell input.
    for key, allowed in [('management_peers', {'192.168.1.101','192.168.1.102','192.168.1.103'}),
                         ('mesh_peers', {'10.10.10.11','10.10.10.12','10.10.10.13'})]:
        if len(config[key]) != 2 or len(set(config[key])) != 2 or not set(config[key]) <= allowed:
            raise ValueError('Invalid physical peer configuration')
    # systemd creates these with mode 0700. Keep one lock FD for the entire sample.
    with open('/run/desk-wifi-watch/lock', 'a') as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return 0
        return observe(config, Commands())


if __name__ == '__main__':
    try:
        sys.exit(main())
    except (OSError, ValueError, KeyError, TypeError) as error:
        print('event=observer_error category='+type(error).__name__, file=sys.stderr)
        sys.exit(1)
