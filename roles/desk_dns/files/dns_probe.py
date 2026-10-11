#!/usr/bin/env python3
"""Bounded DNS probes and read-only isolation guards; Python standard library only."""
import argparse
import hashlib
import ipaddress
import json
from pathlib import Path
import re
import secrets
import socket
import struct
import subprocess
import sys


SERVICES = ('nats-server', 'haproxy', 'keepalived', 'xiaozhi-control-plane',
            'xiaozhi-worker', 'xiaozhi-core', 'xiaozhi-mqtt-gateway',
            'xiaozhi-runtime-apply', 'deskbox-front-panel')
HEALTH = 'health.desk-dns.test'
FRONTEND = 'frontend.desk-dns.test'
IDENTITY = 'who.desk-dns.test'
BLOCKED = 'blocked.desk-dns.test'


def run(*argv):
    return subprocess.check_output(argv, text=True, timeout=10).strip()


def validate(value):
    nodes = value['nodes']
    if set(nodes) != {'deskb1x', 'deskb2x', 'deskb3x'}:
        raise ValueError('Exactly three distinct nodes are required')
    private = [ipaddress.IPv4Address(n['private']) for n in nodes.values()]
    management = [ipaddress.IPv4Address(n['management']) for n in nodes.values()]
    vip = ipaddress.IPv4Address(value['vip'])
    network = ipaddress.IPv4Network('192.168.1.0/24')
    if (len(set(private)) != 3 or len(set(management)) != 3 or vip not in network
            or vip in management or vip in (network.network_address, network.broadcast_address)
            or str(vip) == '192.168.1.1'):
        raise ValueError('DNS VIP must be separate from router, nodes and Xiaozhi VIP')
    if any(ip not in network for ip in management):
        raise ValueError('Management nodes must share the DNS VIP subnet')
    if any(ip not in ipaddress.IPv4Network('10.10.10.0/24') for ip in private):
        raise ValueError('Backends require the existing private Ethernet subnet')
    if type(value['vrid']) is not int or value['vrid'] == 51 or not 1 <= value['vrid'] <= 255:
        raise ValueError('Separate valid VRID required')
    if value['backend_port'] != 5354 or value['test_port'] != 5353 or value['web_port'] != 3001:
        raise ValueError('This foundation uses reserved high backend/test/UI ports')
    # Direct IP upstreams avoid depending on router/system/cluster DNS to bootstrap.
    if not value['upstreams']:
        raise ValueError('Explicit upstream resolvers required')
    for upstream in value['upstreams']:
        ip = ipaddress.IPv4Address(upstream)
        if not ip.is_global:
            raise ValueError('Upstreams must be public IPv4 resolver addresses')
    return value


def request(name, ident):
    labels = name.rstrip('.').split('.')
    if not labels or any(not re.fullmatch(r'[A-Za-z0-9-]{1,63}', label) for label in labels):
        raise ValueError('Invalid DNS probe name')
    question = b''.join(bytes([len(label)]) + label.encode('ascii') for label in labels) + b'\0'
    if len(question) > 255:
        raise ValueError('Oversize DNS probe name')
    return struct.pack('!6H', ident, 0x0100, 1, 0, 0, 0) + question + struct.pack('!HH', 1, 1)


def skip_name(data, pos):
    # Only need the wire length: compressed names end after their two-byte pointer.
    for _ in range(128):
        if pos >= len(data):
            raise ValueError('Truncated DNS name')
        length = data[pos]
        if length & 0xC0 == 0xC0:
            if pos + 1 >= len(data) or (struct.unpack_from('!H', data, pos)[0] & 0x3FFF) >= len(data):
                raise ValueError('Invalid DNS pointer')
            return pos + 2
        if length & 0xC0:
            raise ValueError('Invalid DNS label')
        pos += 1
        if length == 0:
            return pos
        pos += length
    raise ValueError('Oversize DNS name')


def answers(data, query):
    if not 12 <= len(data) <= 4096:
        raise ValueError('Invalid DNS response length')
    ident, flags, qd, an, _, _ = struct.unpack_from('!6H', data)
    if ident != struct.unpack_from('!H', query)[0] or not flags & 0x8000 or flags & 0x7A0F or qd != 1 or an > 32:
        raise ValueError('Invalid or unsuccessful DNS response')
    end = skip_name(data, 12) + 4
    if data[12:end] != query[12:]:
        raise ValueError('DNS question mismatch')
    result = []
    pos = end
    for _ in range(an):
        pos = skip_name(data, pos)
        if pos + 10 > len(data):
            raise ValueError('Truncated DNS answer')
        typ, cls, _, size = struct.unpack_from('!HHIH', data, pos)
        pos += 10
        if pos + size > len(data):
            raise ValueError('Truncated DNS answer data')
        if typ == 1 and cls == 1 and size == 4:
            result.append(str(ipaddress.IPv4Address(data[pos:pos + size])))
        pos += size
    return result


def query(host, port, name, tcp=False):
    address, separator, interface = host.partition('%')
    ip = ipaddress.ip_address(address)
    if separator and (ip.version != 6 or not interface):
        raise ValueError('Interface scope requires an IPv6 address')
    if ip.version == 6 and ip.is_link_local and not separator:
        raise ValueError('Link-local DNS requires an interface scope')
    family = socket.AF_INET6 if ip.version == 6 else socket.AF_INET
    scope = socket.if_nametoindex(interface) if separator else 0
    endpoint = (str(ip), port, 0, scope) if ip.version == 6 else (str(ip), port)
    payload = request(name, secrets.randbelow(65536))
    with socket.socket(family, socket.SOCK_STREAM if tcp else socket.SOCK_DGRAM) as sock:
        sock.settimeout(2)
        sock.connect(endpoint)
        if tcp:
            sock.sendall(struct.pack('!H', len(payload)) + payload)
            def receive(size):
                chunks = bytearray()
                while len(chunks) < size:
                    chunk = sock.recv(size - len(chunks))
                    if not chunk:
                        raise ValueError('Incomplete TCP DNS response')
                    chunks.extend(chunk)
                return bytes(chunks)
            size = struct.unpack('!H', receive(2))[0]
            if not 12 <= size <= 4096:
                raise ValueError('Oversize TCP DNS response')
            data = receive(size)
        else:
            sock.send(payload)
            data = sock.recv(4097)
    return answers(data, payload)


def protected_state():
    units = {}
    for unit in SERVICES:
        values = dict(line.split('=', 1) for line in run('systemctl', 'show', unit,
                     '--property=ActiveState,MainPID,FragmentPath').splitlines())
        if values.get('ActiveState') != 'active' or int(values.get('MainPID', '0')) <= 0:
            raise ValueError('Protected Xiaozhi service is not active: ' + unit)
        units[unit] = values
    paths = {Path('/etc/resolv.conf')}
    for directory in ('/etc/haproxy', '/etc/keepalived', '/etc/systemd/system'):
        for path in Path(directory).rglob('*'):
            if path.is_file() and (directory != '/etc/systemd/system'
                    or any(unit in str(path) for unit in SERVICES)):
                paths.add(path)
    for pattern in ('xiaozhi*.env', 'xiaozhi*.json'):
        paths.update(p for p in Path('/etc').glob(pattern) if p.is_file())
    digests = {str(p): hashlib.sha256(p.read_bytes()).hexdigest() for p in sorted(paths)}
    return {'units': units, 'digests': digests,
            'nonlocal_bind': Path('/proc/sys/net/ipv4/ip_nonlocal_bind').read_text().strip()}


def topology(config, node):
    address = config['nodes'][node]['management']
    # A locally owned VIP routes through lo; identify the management link from
    # the node's permanent management address instead of routing to the VIP.
    all_addresses = json.loads(run('ip', '-j', '-4', 'addr', 'show'))
    addresses = [dev for dev in all_addresses if any(
        entry.get('family') == 'inet' and entry.get('local') == address
        for entry in dev.get('addr_info', []))]
    if len(addresses) != 1:
        raise ValueError('Ambiguous management address assignment')
    interface = addresses[0]['ifname']
    if not re.fullmatch(r'[A-Za-z0-9_.-]{1,15}', interface):
        raise ValueError('Unsupported management interface name')
    links = json.loads(run('ip', '-j', 'link', 'show', 'dev', interface))
    mac = links[0]['address']
    if not re.fullmatch(r'[0-9a-fA-F]{2}(?::[0-9a-fA-F]{2}){5}', mac):
        raise ValueError('Invalid management link address')
    entries = [a for dev in addresses for a in dev['addr_info'] if a['family'] == 'inet']
    local = next(a for a in entries if a['local'] == address)
    network = ipaddress.IPv4Network(address + '/' + str(local['prefixlen']), strict=False)
    if ipaddress.IPv4Address(config['vip']) not in network or interface == 'end0':
        raise ValueError('VIP requires the existing management interface')
    existing = []
    for path in Path('/etc/keepalived').rglob('*.conf'):
        existing += re.findall(r'virtual_router_id\s+(\d+)', path.read_text())
        if re.search(r'(?<![\d.])' + re.escape(config['vip']) + r'(?![\d.])', path.read_text()):
            raise ValueError('DNS VIP appears in existing Xiaozhi Keepalived config')
    if str(config['vrid']) in existing:
        raise ValueError('DNS VRID collides with existing Keepalived')
    run('/usr/sbin/keepalived', '--version')  # Never install/upgrade shared Keepalived here.
    if Path('/proc/sys/net/ipv4/ip_nonlocal_bind').read_text().strip() != '1':
        raise ValueError('Existing nonlocal bind prerequisite absent; no sysctl will be changed')
    if any(a['local'] == config['vip'] for a in entries):
        if run('systemctl', 'is-active', 'desk-dns-vip') != 'active':
            raise ValueError('DNS VIP is already assigned by another owner')
    vendor = subprocess.run(['systemctl', 'is-active', 'dnsdist'], capture_output=True, text=True, timeout=5)
    if vendor.returncode == 0:
        raise ValueError('Unmanaged vendor dnsdist service is active')
    # Check only managed endpoints; leave local resolver/other service ports alone.
    managed = set()
    for service in ('desk-adguard', 'desk-dnsdist', 'desk-dns-ui'):
        for line in run('systemctl', 'show', service, '--property=MainPID').splitlines():
            managed.add(line.split('=', 1)[1])
    for line in run('ss', '-H', '-lnutp').splitlines():
        fields = line.split()
        if len(fields) < 5:
            continue
        endpoint = fields[4]
        for host, port in ((address, config['test_port']), (config['vip'], 53),
                           (config['nodes'][node]['private'], config['backend_port']),
                           ('127.0.0.1', config['web_port']), (config['vip'], config['web_port'])):
            if endpoint in (host + ':' + str(port), '0.0.0.0:' + str(port), '*:' + str(port), '[::]:' + str(port)):
                if not set(re.findall(r'pid=(\d+)', line)) & managed:
                    raise ValueError('DNS endpoint is already used by an unmanaged service')
    election = Path('/etc/desk-dns/election.json')
    xiaozhi_vip = str(ipaddress.IPv4Address(json.loads(Path('/etc/xiaozhi-ingress.json').read_text())['vip']))
    if config['vip'] == xiaozhi_vip:
        raise ValueError('DNS and Xiaozhi VIP must differ')
    policy = run('apt-cache', 'policy', 'dnsdist')
    installed = re.search(r'Installed:\s+(\S+)', policy)
    candidate = re.search(r'Candidate:\s+(\S+)', policy)
    selected = installed.group(1) if installed and installed.group(1) != '(none)' else candidate.group(1) if candidate else ''
    if not selected or selected == '(none)':
        raise ValueError('dnsdist package must be available in configured apt metadata')
    simulation = run('apt-get', '--simulate', '--no-install-recommends', 'install', 'dnsdist=' + selected)
    if re.search(r'^Remv\s|^Inst\s+\S+\s+\[', simulation, re.MULTILINE):
        raise ValueError('DNS package installation would remove or upgrade existing packages')
    return {'interface': interface, 'prefix': local['prefixlen'],
            'mac': mac, 'xiaozhi_vip': xiaozhi_vip, 'dnsdist_version': selected,
            'election': json.loads(election.read_text()) if election.exists() else None}


def probe(config, host, port, balanced=False, direct=False):
    expected = {host} if direct else {n['private'] for n in config['nodes'].values()}
    for tcp in (False, True):
        if query(host, port, HEALTH, tcp) != ['127.0.0.1']:
            raise ValueError('DNS health rewrite unavailable')
        if query(host, port, BLOCKED, tcp) != ['0.0.0.0']:
            raise ValueError('DNS filtering probe failed')
        seen = set()
        for _ in range(12 if balanced else 1):
            ips = query(host, port, IDENTITY, tcp)
            if len(ips) != 1 or ips[0] not in expected:
                raise ValueError('Unexpected DNS backend identity')
            seen.update(ips)
        if balanced and seen != expected:
            raise ValueError('Not all three AdGuard backends receive queries')


def owns(addresses, vip):
    return any(a.get('local') == vip for d in addresses
               for a in d.get('addr_info', []) if a.get('family') == 'inet')


def election(existing, nodes):
    maps = [value for value in existing if value is not None]
    for value in maps:
        if set(value) != set(nodes) or sorted(value.values()) != [100, 110, 120]:
            raise ValueError('Invalid persisted DNS election')
    if maps and any(value != maps[0] for value in maps):
        raise ValueError('Persisted DNS elections disagree')
    return maps[0] if maps else dict(zip(sorted(nodes), secrets.SystemRandom().sample([100, 110, 120], 3)))


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('action', choices=('preflight', 'guard', 'backend', 'ready', 'balanced', 'owners', 'prefer'))
    parser.add_argument('--config', required=True)
    parser.add_argument('--node')
    parser.add_argument('--snapshot')
    parser.add_argument('--host')
    parser.add_argument('--port', type=int)
    args = parser.parse_args()
    raw = sys.stdin.read(65537) if args.config == '-' else Path(args.config).read_text()
    if len(raw.encode('utf-8')) > 65536:
        raise ValueError('Oversize public metadata')
    config = validate(json.loads(raw))
    if args.action == 'preflight':
        print(json.dumps({'protected': protected_state(), 'topology': topology(config, args.node)}))
    elif args.action == 'guard':
        previous = json.loads(Path(args.snapshot).read_text()) if args.snapshot else config['snapshot']
        if protected_state() != previous['protected']:
            raise ValueError('Xiaozhi process, configuration or resolver drift')
        print('Protected Xiaozhi services, configuration and resolver: unchanged')
    elif args.action == 'prefer':
        # Nonzero means a priority penalty, not a health failure. Read the current
        # deployed Xiaozhi snapshot on every check so its VIP stays Cloud-owned.
        vip = str(ipaddress.IPv4Address(json.loads(Path('/etc/xiaozhi-ingress.json').read_text())['vip']))
        addresses = json.loads(run('ip', '-j', '-4', 'addr', 'show'))
        raise SystemExit(1 if owns(addresses, vip) else 0)
    elif args.action == 'ready':
        # VIP belongs to the working DNS frontend, including emergency mode.
        # Native backend/filter readiness remains strict in deployment probes.
        for tcp in (False, True):
            if query(args.host, args.port, FRONTEND, tcp) != ['127.0.0.1']:
                raise ValueError('DNS frontend unavailable')
        print('DNS UDP/TCP frontend: PASS')
    elif args.action == 'owners':
        addresses = json.loads(run('ip', '-j', '-4', 'addr', 'show'))
        xiaozhi_vip = str(ipaddress.IPv4Address(json.loads(Path('/etc/xiaozhi-ingress.json').read_text())['vip']))
        print(json.dumps({'owner': owns(addresses, config['vip']),
                          'xiaozhi_owner': owns(addresses, xiaozhi_vip)}))
    else:
        probe(config, args.host, args.port, args.action == 'balanced', args.action == 'backend')
        print('DNS UDP/TCP filtering, health and backend identity: PASS')


if __name__ == '__main__':
    try:
        main()
    except (OSError, ValueError, KeyError, TypeError, IndexError, StopIteration, subprocess.SubprocessError):
        raise SystemExit('Desk DNS verification failed; inspect local service status')
