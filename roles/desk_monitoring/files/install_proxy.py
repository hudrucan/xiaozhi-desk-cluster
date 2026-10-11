#!/usr/bin/env python3
"""Validate the complete HAProxy candidate before a graceful monitoring-only reload."""
import argparse
import os
from pathlib import Path
import re
import subprocess
import tempfile

ROUTE = '''# BEGIN DESK MONITORING ROUTE
    http-request redirect location /monitor/ code 302 if { path /monitor }
    use_backend desk_monitoring_hubs if { path_beg /monitor/ }
# END DESK MONITORING ROUTE
'''


DNS_ROUTE = '# BEGIN DESK DNS UI ROUTE\n    http-request deny unless { src 192.168.1.0/24 127.0.0.1 } or !{ path_beg /dns /monitor } !{ path / }\n    http-request return status 200 content-type text/html file /etc/desk-ui/index.html if { path / }\n    http-request redirect location /dns/ code 302 if { path /dns }\n    http-request redirect location /monitor/ code 302 if { path /monitor }\n    use_backend desk_adguard_ui if { path_beg /dns/ }\n# END DESK DNS UI ROUTE\n'


def candidate(current, backend, dns_backend=None):
    if dns_backend is None:
        retained = re.search(r'^# BEGIN DESK DNS UI BACKEND\n(.*?)^# END DESK DNS UI BACKEND\n', current, flags=re.MULTILINE | re.DOTALL)
        if retained:
            dns_backend = retained.group(1)
    for name in ('ROUTE', 'BACKEND'):
        current = re.sub(r'^# BEGIN DESK MONITORING ' + name + r'\n.*?^# END DESK MONITORING ' + name + r'\n', '', current,
                         flags=re.MULTILINE | re.DOTALL)
    if dns_backend is not None:
        for name in ('ROUTE', 'BACKEND'):
            current = re.sub(r'^# BEGIN DESK DNS UI ' + name + r'\n.*?^# END DESK DNS UI ' + name + r'\n', '', current, flags=re.MULTILINE | re.DOTALL)
    if current.count('    default_backend xiaozhi_control_planes\n') != 1:
        raise ValueError('Unexpected Settings frontend')
    anchor = '    default_backend xiaozhi_control_planes\n'
    # Insert monitoring after an existing DNS block; preserve it on monitoring-only reruns.
    route = ROUTE.replace('    http-request redirect location /monitor/ code 302 if { path /monitor }\n', '') if dns_backend is not None else ROUTE
    updated = current.replace(anchor, (DNS_ROUTE if dns_backend is not None else '') + route + anchor) + (
        '# BEGIN DESK MONITORING BACKEND\n' + backend.rstrip() + '\n# END DESK MONITORING BACKEND\n')
    if dns_backend is not None:
        updated += '# BEGIN DESK DNS UI BACKEND\n' + dns_backend.rstrip() + '\n# END DESK DNS UI BACKEND\n'
    return updated


def install(backend, dns_backend=None, reload_page=False):
    path = Path('/etc/haproxy/haproxy.cfg')
    current = path.read_text()
    updated = candidate(current, backend, dns_backend)
    if current == updated and not reload_page:
        return False
    # This cluster already runs MQTT through the same master. Never validate only HTTP.
    mqtt = Path('/etc/haproxy/xiaozhi-mqtt.cfg')
    if not mqtt.is_file():
        raise ValueError('Expected MQTT fragment unavailable')
    unit = subprocess.check_output(['systemctl', 'show', 'haproxy', '--property=ExecStart'], text=True, timeout=5)
    if str(path) not in unit or str(mqtt) not in unit:
        raise ValueError('Unexpected HAProxy composition')
    fd, temporary = tempfile.mkstemp(prefix='.monitoring-', dir=path.parent)
    try:
        with os.fdopen(fd, 'w') as output:
            output.write(updated)
        subprocess.run(['/usr/sbin/haproxy', '-c', '-f', temporary, '-f', str(mqtt)],
                       check=True, capture_output=True, timeout=10)
        os.chmod(temporary, 0o644)
        os.replace(temporary, path)
        try:
            subprocess.run(['systemctl', 'reload', 'haproxy'], check=True, capture_output=True, timeout=15)
        except subprocess.SubprocessError:
            path.write_text(current)
            subprocess.run(['systemctl', 'reload', 'haproxy'], check=True, capture_output=True, timeout=15)
            raise
        return True
    finally:
        if Path(temporary).exists():
            Path(temporary).unlink()


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--backend', required=True)
    parser.add_argument('--dns-backend')
    parser.add_argument('--reload-page', action='store_true')
    args = parser.parse_args()
    try:
        changed = install(Path(args.backend).read_text(), Path(args.dns_backend).read_text() if args.dns_backend else None, args.reload_page)
        print('changed' if changed else 'unchanged')
    except Exception:
        raise SystemExit('Monitoring HAProxy candidate failed; existing routing retained')
