#!/usr/bin/env python3
"""Read-only routing, TLS, PROXY and source-ACL gates; never changes VIP/sysctls."""
import argparse
import ipaddress
import json
from pathlib import Path
import socket
import ssl
import struct
import subprocess

NODES = {f'deskb{i}x': {'mesh': f'10.10.10.{10+i}', 'management': f'192.168.1.{100+i}'} for i in (1, 2, 3)}
SITES = {'cluster.huynm.dev': '/api/cluster/panel', 'ntfy.cluster.huynm.dev': '/ready', 'ha.cluster.huynm.dev': '/api/desk_cluster/ready'}
SIGNATURE = b'\r\n\r\n\0\r\nQUIT\n'


def run(argv):
    result = subprocess.run(argv, text=True, capture_output=True, timeout=10)
    if result.returncode: raise RuntimeError('Read-only probe failed: ' + argv[0])
    return result.stdout.strip()


def inspect(node):
    own = NODES[node]
    addresses = json.loads(run(['ip', '-j', '-4', 'addr', 'show']))
    rp = {p.parent.name: int(p.read_text()) for p in Path('/proc/sys/net/ipv4/conf').glob('*/rp_filter')}
    routes = {ip: json.loads(run(['ip', '-j', 'route', 'get', ip, 'from', own['mesh']]))
              for ip in ['192.168.1.186', *(n['mesh'] for n in NODES.values())]}
    codes = {}
    for port in (80, 2586):
        codes[str(port)] = run(['curl', '--noproxy', '*', '--interface', own['mesh'], '--connect-timeout', '3', '--max-time', '6', '-s', '-o', '/dev/null', '-w', '%{http_code}', f'http://192.168.1.186:{port}/'])
    codes['local_ha'] = run(['curl', '--noproxy', '*', '--interface', own['mesh'], '--connect-timeout', '3', '--max-time', '6', '-s', '-o', '/dev/null', '-w', '%{http_code}', f'http://{own["mesh"]}:8123/'])
    unit = run(['systemctl', 'show', 'haproxy', '--property=ExecStart', '--property=ExecReload'])
    if not all(any(line.startswith(prop + '=') and '/etc/haproxy/haproxy.cfg' in line and '/etc/haproxy/xiaozhi-mqtt.cfg' in line
                   for line in unit.splitlines()) for prop in ('ExecStart', 'ExecReload')):
        raise RuntimeError('Unexpected HTTP/MQTT composition')
    return {'node': node, 'vip_owner': any(a['local'] == '192.168.1.186' for i in addresses for a in i.get('addr_info', [])),
            'rp_filter': rp, 'routes': routes, 'http_codes': codes, 'upstream': 'permissioned local Unix sockets'}


def proxy_v2(source, destination, local=False):
    if local: return SIGNATURE + b'\x20\x00\x00\x00'
    return SIGNATURE + b'\x21\x11\x00\x0c' + ipaddress.ip_address(source).packed + ipaddress.ip_address(destination).packed + struct.pack('!HH', 49152, 8443)


def connect(source, target, name, prefix, path=None, spoof=False, context=None):
    raw = socket.create_connection((target, 8443), timeout=5, source_address=(source, 0))
    try:
        if prefix: raw.sendall(prefix)
        with (context or ssl.create_default_context()).wrap_socket(raw, server_hostname=name) as tls:
            certificate = tls.getpeercert()
            if path is None: return {'tls': True, 'expires': certificate['notAfter']}
            headers = 'X-Forwarded-For: 192.168.1.101\r\nForwarded: for=192.168.1.101;proto=https\r\nX-Forwarded-Proto: https\r\n' if spoof else ''
            tls.sendall(f'GET {path} HTTP/1.1\r\nHost: {name}\r\n{headers}Connection: close\r\n\r\n'.encode())
            line = tls.makefile('rb').readline(4096)
            return {'tls': True, 'status': int(line.split()[1]), 'expires': certificate['notAfter']}
    finally:
        raw.close()


def rejected(source, target, name, prefix):
    # TCP reachability must succeed: a routing failure is not proof of rejection.
    raw = socket.create_connection((target, 8443), timeout=5, source_address=(source, 0))
    try:
        if prefix: raw.sendall(prefix)
        try:
            with ssl.create_default_context().wrap_socket(raw, server_hostname=name): pass
        except (ssl.SSLError, ConnectionError): return True
        raise RuntimeError('Untrusted/missing/v1 PROXY header was accepted')
    finally: raw.close()


def verify(node, data=True):
    own = NODES[node]
    results = []
    for peer in NODES.values():
        target = peer['mesh']
        for name, path in SITES.items():
            prefix = proxy_v2(own['management'], target)
            value = connect(own['mesh'], target, name, prefix, path if data else None)
            if data and value['status'] != 200: raise RuntimeError(f'HTTPS backend not ready: {target} {name} {value["status"]}')
            # Health connections have no public client. Cover both v2 address
            # and LOCAL forms through the same guard and verified TLS path.
            for local in (False, True):
                health = connect(own['mesh'], target, name, proxy_v2(own['mesh'], target, local=local), '/__desk_https_health' if data else None)
                if data and health['status'] != 200: raise RuntimeError(f'End-to-end health failed: {target} {name} {health["status"]}')
            if data:
                denied = connect(own['mesh'], target, name, proxy_v2('203.0.113.10', target), '/', spoof=True)
                if denied['status'] != 403: raise RuntimeError('Forwarding header bypassed original source ACL')
                health_denied = connect(own['mesh'], target, name, proxy_v2('203.0.113.10', target), '/__desk_https_health', spoof=True)
                if health_denied['status'] != 404: raise RuntimeError('Reserved health route accepted a public client')
            results.append({'target': target, 'name': name, **value})
        name = 'cluster.huynm.dev'
        rejected(own['mesh'], target, name, b'')
        rejected(own['mesh'], target, name, f'PROXY TCP4 {own["management"]} {target} 49152 8443\r\n'.encode())
        # Test the untrusted source locally on each node. Sending a management
        # source across WireGuard can be dropped by AllowedIPs before TCP and
        # would not prove that Caddy enforced its own peer boundary.
        if target == own['mesh']:
            rejected(own['management'], target, name, proxy_v2(own['management'], target))
    return results


def public():
    results = []
    for name, path in SITES.items():
        with socket.create_connection(('192.168.1.186', 443), timeout=5) as raw:
            with ssl.create_default_context().wrap_socket(raw, server_hostname=name) as tls:
                tls.sendall(f'GET {path} HTTP/1.1\r\nHost: {name}\r\nConnection: close\r\n\r\n'.encode())
                status = int(tls.makefile('rb').readline(4096).split()[1])
                if status != 200: raise RuntimeError('Public HTTPS path not ready: ' + name)
                results.append({'name': name, 'status': status})
    return results


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('mode', choices=['inspect', 'tls', 'verify', 'public'])
    parser.add_argument('--node', required=True, choices=NODES)
    args = parser.parse_args()
    print(json.dumps(inspect(args.node) if args.mode == 'inspect' else public() if args.mode == 'public' else verify(args.node, args.mode == 'verify'), sort_keys=True))
