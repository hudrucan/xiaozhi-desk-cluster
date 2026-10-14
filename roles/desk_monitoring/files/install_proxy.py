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


DNS_ROUTE = r'''# BEGIN DESK DNS UI ROUTE
    http-request deny unless { src 192.168.1.0/24 127.0.0.1 } or !{ path_beg /dns /monitor } !{ path / }
    http-request return status 200 content-type text/html file /etc/desk-ui/index.html if { path / }
    http-request redirect location /xiaozhi/ code 302 if { path /xiaozhi /settings /settings/ }
    # Bootstrap /xiaozhi/ota/ keeps its existing public transport path.
    http-request replace-path ^/xiaozhi/(.*)$ /settings/\1 if { path_beg /xiaozhi/ } !{ path_beg /xiaozhi/ota }
    http-request redirect location /dns/ code 302 if { path /dns }
    http-request redirect location /monitor/ code 302 if { path /monitor }
    use_backend desk_adguard_ui if { path_beg /dns/ }
# END DESK DNS UI ROUTE
'''


def candidate(current, backend, dns_backend=None, dashboard_backend=None, vpn_backend=None):
    if vpn_backend is None:
        retained = re.search(r'^# BEGIN DESK VPN UI BACKEND\n(.*?)^# END DESK VPN UI BACKEND\n', current, flags=re.MULTILINE | re.DOTALL)
        if retained: vpn_backend = retained.group(1)
    for name in ('ROUTE','BACKEND'):
        current = re.sub(r'^# BEGIN DESK VPN UI ' + name + r'\n.*?^# END DESK VPN UI ' + name + r'\n','',current,flags=re.MULTILINE | re.DOTALL)
    if dashboard_backend is None:
        retained = re.search(r'^# BEGIN DESK DASHBOARD BACKEND\n(.*?)^# END DESK DASHBOARD BACKEND\n', current, flags=re.MULTILINE | re.DOTALL)
        if retained:
            dashboard_backend = retained.group(1)
    current = re.sub(r'^# BEGIN DESK DASHBOARD BACKEND\n.*?^# END DESK DASHBOARD BACKEND\n', '', current, flags=re.MULTILINE | re.DOTALL)
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
    dns_route = DNS_ROUTE
    if dashboard_backend is not None:
        dns_route = dns_route.replace('!{ path_beg /dns /monitor }', '!{ path_beg /dns /monitor /api/cluster/status /api/cluster/panel /api/cluster/operations }')
        dns_route = dns_route.replace('    http-request return status 200 content-type text/html file /etc/desk-ui/index.html if { path / }\n', '')
        dns_route = dns_route.replace('    use_backend desk_adguard_ui', '    use_backend desk_dashboard if { path / /api/cluster/status /api/cluster/panel }\n    use_backend desk_adguard_ui')
    if dashboard_backend is not None and 'backend desk_node_control\n' in dashboard_backend:
        dns_route = dns_route.replace('    use_backend desk_dashboard',
            '    use_backend desk_node_control if { path /api/cluster/operations /api/cluster/operations/prepare /api/cluster/operations/reboot /api/cluster/operations/resume }\n    use_backend desk_dashboard')
    if vpn_backend is not None:
        dns_route = dns_route.replace('path_beg /dns /monitor', 'path_beg /dns /monitor /vpn')
    vpn_route = ('# BEGIN DESK VPN UI ROUTE\n    http-request redirect location /vpn/ code 302 if { path /vpn }\n    use_backend desk_vpn_ui if { path_beg /vpn/ }\n# END DESK VPN UI ROUTE\n') if vpn_backend is not None else ''
    updated = current.replace(anchor, vpn_route + (dns_route if dns_backend is not None else '') + route + anchor) + (
        '# BEGIN DESK MONITORING BACKEND\n' + backend.rstrip() + '\n# END DESK MONITORING BACKEND\n')
    if dns_backend is not None:
        updated += '# BEGIN DESK DNS UI BACKEND\n' + dns_backend.rstrip() + '\n# END DESK DNS UI BACKEND\n'
    if dashboard_backend is not None:
        updated += '# BEGIN DESK DASHBOARD BACKEND\n' + dashboard_backend.rstrip() + '\n# END DESK DASHBOARD BACKEND\n'
    if vpn_backend is not None:
        updated += '# BEGIN DESK VPN UI BACKEND\n' + vpn_backend.rstrip() + '\n# END DESK VPN UI BACKEND\n'
    return updated


def install(backend, dns_backend=None, reload_page=False, dashboard_backend=None, vpn_backend=None):
    path = Path('/etc/haproxy/haproxy.cfg')
    current = path.read_text()
    updated = candidate(current, backend, dns_backend, dashboard_backend, vpn_backend)
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
    parser.add_argument('--dashboard-backend')
    parser.add_argument('--vpn-backend')
    args = parser.parse_args()
    try:
        changed = install(Path(args.backend).read_text(), Path(args.dns_backend).read_text() if args.dns_backend else None, args.reload_page, Path(args.dashboard_backend).read_text() if args.dashboard_backend else None, Path(args.vpn_backend).read_text() if args.vpn_backend else None)
        print('changed' if changed else 'unchanged')
    except Exception:
        raise SystemExit('Monitoring HAProxy candidate failed; existing routing retained')
