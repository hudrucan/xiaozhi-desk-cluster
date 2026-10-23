#!/usr/bin/env python3
"""Validate the complete HAProxy candidate before a graceful monitoring-only reload."""
import argparse
import os
from pathlib import Path
import re
import subprocess
import tempfile

HTTPS_BEGIN = '# BEGIN DESK HTTPS\n'
HTTPS_END = '# END DESK HTTPS\n'
HTTPS_PREPARED = HTTPS_BEGIN + '# Local listeners prepared; public TLS is not enabled.\n' + HTTPS_END


def https_fragment(config):
    config = config.rstrip() + '\n'
    blocks = re.findall(r'^# BEGIN DESK HTTPS\n.*?^# END DESK HTTPS\n', config,
                        flags=re.MULTILINE | re.DOTALL)
    if len(blocks) > 1 or config.count(HTTPS_BEGIN) != len(blocks) or config.count(HTTPS_END) != len(blocks):
        raise ValueError('Malformed HTTPS boundary')
    return blocks[0] if blocks else ''


def https_candidate(current, fragment=None):
    """Local, permissioned PROXY sockets reuse each frontend's unchanged src ACL.

    None retains activation; an empty fragment explicitly rolls it back. HTTP
    clients cannot send PROXY headers to the existing TCP binds. Caddy owns the
    sockets, and the second ntfy hop accepts identity only from cluster peers.
    """
    current = current.rstrip() + '\n'
    active = https_fragment(current) if fragment is None else fragment.rstrip() + '\n' if fragment else ''
    if active and (https_fragment(active) != active or ('frontend desk_https\n' not in active and active != HTTPS_PREPARED)):
        raise ValueError('Invalid HTTPS fragment')
    current = re.sub(r'^# BEGIN DESK HTTPS\n.*?^# END DESK HTTPS\n', '', current,
                     flags=re.MULTILINE | re.DOTALL)
    current = re.sub(r'^# BEGIN DESK HTTPS [A-Z]+\n.*?^# END DESK HTTPS [A-Z]+\n', '', current,
                     flags=re.MULTILINE | re.DOTALL)
    # Undo only our ntfy identity guard before rebuilding it.
    current = current.replace('    http-request set-header X-Forwarded-For %[src] unless { var(txn.desk_https) -m bool }\n',
                              '    http-request set-header X-Forwarded-For %[src]\n')
    if not active:
        return current.rstrip() + '\n'
    for frontend, name, readiness in [('xiaozhi_settings', 'cluster', None), ('desk_ntfy_vip', 'ntfy', '/ready'), ('desk_homeassistant_ui', 'ha', '/api/desk_cluster/ready')]:
        pattern = r'(^frontend ' + frontend + r'\n(?:(?!^(?:frontend|backend|listen) ).*\n)*?    bind [^\n]+\n)'
        extra = (f'# BEGIN DESK HTTPS {name.upper()}\n'
                 f'    bind /run/haproxy/desk-https-{name}.sock user desk-caddy group desk-caddy mode 600 accept-proxy\n'
                 '    http-request set-var(txn.desk_https_health) bool(true) if { fc_rcvd_proxy } { src 10.10.10.11 10.10.10.12 10.10.10.13 } { method GET } { path /__desk_https_health }\n')
        if readiness:
            # Application readiness retains its existing source ACL boundary.
            extra += (f'    http-request set-path {readiness} if {{ var(txn.desk_https_health) -m bool }}\n'
                      '    http-request set-src ipv4(127.0.0.1) if { var(txn.desk_https_health) -m bool }\n')
        else:
            # Cluster ingress health ends at this frontend, independently of
            # Dashboard state. Public binds never accept PROXY headers.
            extra += ('    http-request deny deny_status 404 if { path /__desk_https_health } !{ var(txn.desk_https_health) -m bool }\n'
                      '    http-request return status 200 content-type text/plain string ok if { var(txn.desk_https_health) -m bool }\n')
        extra += (
                 '    http-request del-header Forwarded\n'
                 '    http-request del-header X-Real-IP\n'
                 '    http-request del-header CF-Connecting-IP\n'
                 '    http-request del-header True-Client-IP\n'
                 '    http-request set-header X-Forwarded-For %[src] if { fc_rcvd_proxy }\n'
                 '    http-request set-header X-Forwarded-Proto https if { fc_rcvd_proxy }\n'
                 '    http-request set-header X-Forwarded-Host %[req.hdr(Host)] if { fc_rcvd_proxy }\n'
                 '    http-request del-header X-Forwarded-Host unless { fc_rcvd_proxy }\n'
                 '    http-request del-header X-Forwarded-Proto unless { fc_rcvd_proxy }\n'
                 '    http-request del-header X-Forwarded-For unless { fc_rcvd_proxy }\n')
        extra += f'# END DESK HTTPS {name.upper()}\n'
        current, count = re.subn(pattern, lambda m: m[0] + extra, current, count=1, flags=re.MULTILINE)
        if count != 1: raise ValueError('Expected HTTPS upstream frontend: ' + frontend)
    node = 'frontend desk_ntfy_node\n'
    if current.count(node) != 1: raise ValueError('Expected private ntfy boundary')
    # src here is the immediate HAProxy peer, not the original public client.
    guard = ('# BEGIN DESK HTTPS IDENTITY\n'
             '    http-request set-var(txn.desk_https) bool(true) if { src 10.10.10.11 10.10.10.12 10.10.10.13 } { req.hdr(X-Forwarded-Proto) -m str https } { req.hdr_cnt(X-Forwarded-For) eq 1 } { req.hdr_ip(X-Forwarded-For) -m found }\n'
             '# END DESK HTTPS IDENTITY\n')
    start = current.index(node)
    identity = '    http-request set-header X-Forwarded-For %[src]\n'
    index = current.find(identity, start)
    if index < 0: raise ValueError('Expected native ntfy identity boundary')
    current = current[:index] + guard + current[index:].replace(identity,
        '    http-request set-header X-Forwarded-For %[src] unless { var(txn.desk_https) -m bool }\n', 1)
    return current.rstrip() + '\n\n' + active


def full_candidate(current, rendered):
    """A full ingress or HA rerun must retain the installed HTTPS activation."""
    return https_candidate(rendered, https_fragment(current)) if https_fragment(current) else rendered

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


def candidate(current, backend, dns_backend=None, dashboard_backend=None, vpn_backend=None, network_backend=None, ntfy_config=None, https_config=None):
    if https_config == HTTPS_PREPARED and https_fragment(current):
        https_config = https_fragment(current)  # Preparation reruns never withdraw :443.
    retained_https = https_fragment(current)
    if retained_https:
        if https_config is None: https_config = retained_https
        current = current.replace(retained_https, '').rstrip() + '\n'
    if ntfy_config is not None:
        current = re.sub(r'^# BEGIN DESK NTFY\n.*?^# END DESK NTFY\n', '', current, flags=re.MULTILINE | re.DOTALL)
        current = current.rstrip() + '\n' + ntfy_config.rstrip() + '\n'
    if network_backend is None:
        retained = re.search(r'^# BEGIN DESK NETWORK BACKEND\n(.*?)^# END DESK NETWORK BACKEND\n', current, flags=re.MULTILINE | re.DOTALL)
        if retained: network_backend = retained.group(1)
    for name in ('ROUTE', 'BACKEND'):
        current = re.sub(r'^# BEGIN DESK NETWORK ' + name + r'\n.*?^# END DESK NETWORK ' + name + r'\n', '', current, flags=re.MULTILINE | re.DOTALL)
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
    if network_backend is not None:
        dns_route = dns_route.replace('path_beg /dns /monitor', 'path_beg /dns /monitor /network')
    network_route = ('# BEGIN DESK NETWORK ROUTE\n    http-request redirect location /network/ code 302 if { path /network }\n    use_backend desk_network if { path_beg /network/ }\n# END DESK NETWORK ROUTE\n') if network_backend is not None else ''
    vpn_route = ('# BEGIN DESK VPN UI ROUTE\n    http-request redirect location /vpn/ code 302 if { path /vpn }\n    use_backend desk_vpn_ui if { path_beg /vpn/ }\n# END DESK VPN UI ROUTE\n') if vpn_backend is not None else ''
    updated = current.replace(anchor, network_route + vpn_route + (dns_route if dns_backend is not None else '') + route + anchor) + (
        '# BEGIN DESK MONITORING BACKEND\n' + backend.rstrip() + '\n# END DESK MONITORING BACKEND\n')
    if dns_backend is not None:
        updated += '# BEGIN DESK DNS UI BACKEND\n' + dns_backend.rstrip() + '\n# END DESK DNS UI BACKEND\n'
    if dashboard_backend is not None:
        updated += '# BEGIN DESK DASHBOARD BACKEND\n' + dashboard_backend.rstrip() + '\n# END DESK DASHBOARD BACKEND\n'
    if vpn_backend is not None:
        updated += '# BEGIN DESK VPN UI BACKEND\n' + vpn_backend.rstrip() + '\n# END DESK VPN UI BACKEND\n'
    if network_backend is not None:
        updated += '# BEGIN DESK NETWORK BACKEND\n' + network_backend.rstrip() + '\n# END DESK NETWORK BACKEND\n'
    return https_candidate(updated, https_config) if https_config is not None or https_fragment(updated) else updated


def install(backend=None, dns_backend=None, reload_page=False, dashboard_backend=None, vpn_backend=None, network_backend=None, ntfy_config=None, https_config=None, full_config=None, validate_only=False, defer_reload=False):
    path = Path('/etc/haproxy/haproxy.cfg')
    current = path.read_text()
    updated = full_candidate(current, full_config) if full_config is not None else candidate(current, backend, dns_backend, dashboard_backend, vpn_backend, network_backend, ntfy_config, https_config)
    if current == updated and not reload_page and not validate_only:
        return False
    # This cluster already runs MQTT through the same master. Never validate only HTTP.
    mqtt = Path('/etc/haproxy/xiaozhi-mqtt.cfg')
    if not mqtt.is_file() and full_config is None:
        raise ValueError('Expected MQTT fragment unavailable')
    unit = subprocess.check_output(['systemctl', 'show', 'haproxy', '--property=ExecStart', '--property=ExecReload'], text=True, timeout=5)
    if mqtt.is_file() and not all(any(line.startswith(prop + '=') and str(path) in line and str(mqtt) in line
                                     for line in unit.splitlines()) for prop in ('ExecStart', 'ExecReload')):
        raise ValueError('Unexpected HAProxy composition')
    fd, temporary = tempfile.mkstemp(prefix='.monitoring-', dir=path.parent)
    try:
        with os.fdopen(fd, 'w') as output:
            output.write(updated)
        subprocess.run(['/usr/sbin/haproxy', '-c', '-f', temporary] + (['-f', str(mqtt)] if mqtt.is_file() else []),
                       check=True, capture_output=True, timeout=10)
        if validate_only: return False
        os.chmod(temporary, 0o644)
        os.replace(temporary, path)
        # Foundation bootstrap may not have a running HAProxy yet. Active ingress
        # always reloads transactionally here, including full Ansible reruns.
        if defer_reload and subprocess.run(['systemctl', 'is-active', '--quiet', 'haproxy'],
                                            capture_output=True, timeout=5).returncode != 0:
            return True
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
    parser.add_argument('--backend')
    parser.add_argument('--dns-backend')
    parser.add_argument('--reload-page', action='store_true')
    parser.add_argument('--dashboard-backend')
    parser.add_argument('--vpn-backend')
    parser.add_argument('--network-backend')
    parser.add_argument('--ntfy-config')
    parser.add_argument('--https-config')
    parser.add_argument('--remove-https', action='store_true')
    parser.add_argument('--prepare-https', action='store_true')
    parser.add_argument('--full-config')
    parser.add_argument('--validate-only', action='store_true')
    parser.add_argument('--defer-reload', action='store_true')
    args = parser.parse_args()
    try:
        if not args.backend and not args.full_config: raise ValueError('Backend or full candidate required')
        if sum([args.remove_https, args.prepare_https, bool(args.https_config)]) > 1: raise ValueError('Conflicting HTTPS operations')
        if args.full_config and (args.remove_https or args.prepare_https or args.https_config): raise ValueError('Full reruns retain activation; use the explicit HTTPS operation separately')
        https = '' if args.remove_https else HTTPS_PREPARED if args.prepare_https else Path(args.https_config).read_text() if args.https_config else None
        changed = install(Path(args.backend).read_text() if args.backend else None, Path(args.dns_backend).read_text() if args.dns_backend else None, args.reload_page, Path(args.dashboard_backend).read_text() if args.dashboard_backend else None, Path(args.vpn_backend).read_text() if args.vpn_backend else None, Path(args.network_backend).read_text() if args.network_backend else None, Path(args.ntfy_config).read_text() if args.ntfy_config else None, https, Path(args.full_config).read_text() if args.full_config else None, args.validate_only, args.defer_reload)
        print('changed' if changed else 'unchanged')
    except Exception:
        raise SystemExit('Monitoring HAProxy candidate failed; existing routing retained')
