#!/usr/bin/env python3
"""Read-only bounded isolation checks for the Beszel trial."""
import argparse
import hashlib
import ipaddress
import json
from pathlib import Path
import re
import subprocess
import sys
import urllib.request

UNITS = ('nats-server', 'haproxy', 'keepalived', 'xiaozhi-control-plane',
         'xiaozhi-worker', 'xiaozhi-core', 'xiaozhi-mqtt-gateway',
         'xiaozhi-runtime-apply', 'deskbox-front-panel', 'desk-adguard',
         'desk-dnsdist', 'desk-dns-ui', 'desk-dns-vip', 'desk-dns-sync-agent')
MARKERS = ('MONITORING ROUTE', 'MONITORING BACKEND', 'DNS UI ROUTE', 'DNS UI BACKEND')


def normalize_config(text):
    for marker in MARKERS:
        text = re.sub(r'^# BEGIN DESK ' + marker + r'\n.*?^# END DESK ' + marker + r'\n', '', text,
                      flags=re.MULTILINE | re.DOTALL)
    return text


def state():
    units = {}
    for unit in UNITS:
        result = subprocess.check_output(['systemctl', 'show', unit,
                                          '--property=MainPID,ActiveState'], text=True, timeout=5)
        units[unit] = dict(line.split('=', 1) for line in result.splitlines())
        if unit != 'desk-dns-vip' and (units[unit]['ActiveState'] != 'active' or units[unit]['MainPID'] == '0'):
            raise ValueError('Protected service unavailable')
    paths = {Path('/etc/resolv.conf'), Path('/etc/xiaozhi-ingress.json')}
    for directory in ('/etc/haproxy', '/etc/keepalived', '/etc/desk-dns', '/etc/systemd/system'):
        for path in Path(directory).rglob('*'):
            if path.is_file() and (directory != '/etc/systemd/system' or any(unit in str(path) for unit in UNITS)):
                # Policy/status/private session state can change independently of monitoring.
                if directory == '/etc/desk-dns' and path.name not in ('public.json', 'vip.json', 'election.json', 'ui-haproxy.cfg', 'keepalived.conf'):
                    continue
                paths.add(path)
    paths.update(p for p in Path('/etc').glob('xiaozhi*.env') if p.is_file())
    digests = {str(p): hashlib.sha256(p.read_bytes()).hexdigest() for p in sorted(paths)}
    return {'units': units, 'digests': digests,
            'haproxy_base': hashlib.sha256(normalize_config(Path('/etc/haproxy/haproxy.cfg').read_text()).encode()).hexdigest()}


def guard(previous, current, after_proxy=False):
    if previous['units'] != current['units']:
        raise ValueError('Protected service state or PIDs changed')
    before = dict(previous['digests'])
    after = dict(current['digests'])
    if after_proxy:
        before.pop('/etc/haproxy/haproxy.cfg', None)
        after.pop('/etc/haproxy/haproxy.cfg', None)
    if before != after or previous['haproxy_base'] != current['haproxy_base']:
        raise ValueError('Protected configuration changed')


def health(private, vip):
    if ipaddress.IPv4Address(private) not in ipaddress.IPv4Network('10.10.10.0/24'):
        raise ValueError('Invalid private address')
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    with opener.open('http://' + private + ':8004/api/cluster', timeout=5) as response:
        data = json.loads(response.read(1048576))
    if data['ingress']['configured_vip'] != vip:
        raise ValueError('Cloud/deployment VIP drift')
    with opener.open('http://' + private + ':8004/healthz', timeout=5) as response:
        if response.status != 200:
            raise ValueError('Control plane unhealthy')


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('mode', choices=['freeze', 'guard'])
    parser.add_argument('--private', required=True)
    parser.add_argument('--vip', required=True)
    parser.add_argument('--after-proxy', action='store_true')
    args = parser.parse_args()
    health(args.private, args.vip)
    current = state()
    if args.mode == 'freeze':
        sockets = subprocess.check_output(['ss', '-ltnp'], text=True, timeout=5)
        for port, service in ((8090, 'beszel-hub'), (45876, 'beszel-agent')):
            matching = [line for line in sockets.splitlines() if re.search(r':' + str(port) + r'\s', line)]
            pid = subprocess.check_output(['systemctl', 'show', service, '--property=MainPID', '--value'], text=True, timeout=5).strip()
            if matching and (pid == '0' or any('pid=' + pid + ',' not in line for line in matching)):
                raise ValueError('Reserved monitoring port already in use')
        print(json.dumps(current))
    else:
        guard(json.load(sys.stdin), current, args.after_proxy)
        print('Protected services/configuration unchanged')


if __name__ == '__main__':
    try:
        main()
    except Exception:
        raise SystemExit('Monitoring isolation check failed; inspect local service state')
