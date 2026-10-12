#!/usr/bin/env python3
"""Fixed node reboot action, survivor leases and read-only recovery tracking.

No shell, arbitrary command, SSH, provider call, database or background reboot.
Only explicit confirmation schedules systemd's ordinary reboot sequence.
"""
import base64
from datetime import datetime
from concurrent.futures import ThreadPoolExecutor
import hashlib
import hmac
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import ipaddress
import json
import os
from pathlib import Path
import secrets
import socket
import struct
import subprocess
import threading
import time
from urllib.error import HTTPError
from urllib.request import Request, build_opener, ProxyHandler

PROTOCOL = 'desk-node-operation-v1'
MAX_BODY = 8192
LEASE_SECONDS = 600
CHALLENGE_SECONDS = 60
BOOT_PATH = '/proc/sys/kernel/random/boot_id'


class Failure(Exception):
    def __init__(self, status, message):
        self.status, self.message = status, message


def encode(value):
    return json.dumps(value, separators=(',', ':'), sort_keys=True).encode()


def boot_id():
    return Path(BOOT_PATH).read_text().strip()


def dns_ready(host, port):
    """Ignored existing backend health name; validate ID and an IPv4 answer."""
    identity = secrets.randbelow(65536)
    name = b''.join(bytes([len(p)]) + p.encode() for p in 'health.desk-dns.test'.split('.')) + b'\0'
    query = struct.pack('!6H', identity, 0x100, 1, 0, 0, 0) + name + struct.pack('!2H', 1, 1)
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
            sock.settimeout(1)
            sock.connect((host, port))
            sock.send(query)
            reply = sock.recv(4096)
        if len(reply) < 12:
            return False
        ident, flags, questions, answers, _, _ = struct.unpack('!6H', reply[:12])
        if ident != identity or not flags & 0x8000 or flags & 0x020f or questions != 1 or not 0 < answers <= 64:
            return False
        def skip_name(offset):
            while True:
                if offset >= len(reply): raise ValueError()
                length = reply[offset]; offset += 1
                if length == 0: return offset
                if length & 0xc0 == 0xc0:
                    if offset >= len(reply): raise ValueError()
                    return offset + 1
                if length > 63 or offset + length > len(reply): raise ValueError()
                offset += length
        offset = skip_name(12) + 4
        for _ in range(answers):
            offset = skip_name(offset)
            if offset + 10 > len(reply): return False
            kind, klass, _, length = struct.unpack('!HHIH', reply[offset:offset+10])
            offset += 10
            if offset + length > len(reply): return False
            if kind == klass == 1 and length == 4 and reply[offset:offset+4] == b'\x7f\x00\x00\x01': return True
            offset += length
        return False
    except (OSError, ValueError):
        return False


class Control:
    def __init__(self, config, state_path, reboot=None):
        self.config = config
        self.node = config['node']
        self.members = {m['id']: m for m in config['members']}
        self.key = bytes.fromhex(config['key'])
        self.path = Path(state_path)
        self.lock = threading.RLock()
        self.nonces = {}
        self.cached_health = (0, None)
        self.opener = build_opener(ProxyHandler({}))
        self.state = {'lease': None, 'executed': None, 'completed': None}
        if self.path.exists():
            try:
                value = json.loads(self.path.read_text())
                if not isinstance(value,dict) or not {'lease','executed'} <= set(value) <= set(self.state):
                    raise ValueError()
                self.state.update(value)
            except (OSError, ValueError, TypeError):
                raise RuntimeError('Node control state needs review') from None
        self.reboot = reboot or self.system_reboot

    def save(self):
        self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        temporary = self.path.with_suffix('.tmp')
        with temporary.open('w') as stream:
            os.chmod(temporary, 0o600)
            json.dump(self.state, stream)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, self.path)
        fd = os.open(self.path.parent, os.O_DIRECTORY)
        try: os.fsync(fd)
        finally: os.close(fd)

    @staticmethod
    def system_reboot():
        subprocess.run(['/usr/bin/systemctl', 'reboot', '--no-block'],
                       check=True, timeout=10, stdin=subprocess.DEVNULL,
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

    def get(self, url):
        with self.opener.open(Request(url), timeout=2) as response:
            raw = response.read(65537)
            if len(raw) > 65536: raise ValueError()
            return json.loads(raw)

    def health(self):
        with self.lock:
            stamp, cached = self.cached_health
            if cached is not None and time.monotonic() - stamp < 3:
                return cached
        member = self.members[self.node]
        result = {'node': self.node, 'boot_id': boot_id(), 'ready': False, 'metadata': {}}
        try:
            value = self.get(f"http://{member['private']}:{self.config['dashboard_port']}/local")
            age = time.time() - datetime.fromisoformat(value['observed_at'].replace('Z', '+00:00')).timestamp()
            components = value.get('components', {})
            configs = value.get('configuration', {})
            result['metadata'] = {k: value.get(k) for k in ('ip', 'vip', 'core', 'mqtt', 'activity')}
            result['revisions'] = {k: configs.get(k, {}).get('revision') for k in ('xiaozhi', 'dns', 'monitoring')}
            valid = value.get('id') == self.node and value.get('boot_id') == result['boot_id'] and 0 <= age <= 15
            valid &= all(components.get(k) == 'ready' for k in ('worker', 'core', 'mqtt', 'dns'))
            valid &= all(configs.get(k, {}).get('state') == 'synced' for k in ('xiaozhi', 'dns', 'monitoring'))
            if valid:
                routes = self.get(f"http://{member['private']}:{self.config['nats_port']}/routez")
                valid = routes.get('num_routes', 0) >= 2 and dns_ready(member['management'], self.config['dns_port'])
            result['ready'] = bool(valid)
        except (OSError, ValueError, KeyError, TypeError):
            pass
        with self.lock:
            self.cached_health = (time.monotonic(), result)
        return result

    def signature(self, path, raw, stamp, nonce):
        return hmac.new(self.key, path.encode() + b'\n' + stamp.encode() + b'\n' + nonce.encode() + b'\n' + raw, hashlib.sha256).hexdigest()

    def authenticate(self, path, raw, headers):
        stamp, nonce, signature = (headers.get(k, '') for k in ('X-Desk-Time', 'X-Desk-Nonce', 'X-Desk-Signature'))
        try:
            valid = abs(time.time() - int(stamp)) <= 30 and len(nonce) == 32
        except ValueError: valid = False
        if not valid or not hmac.compare_digest(signature, self.signature(path, raw, stamp, nonce)):
            raise Failure(403, 'Peer authentication failed')
        with self.lock:
            self.nonces = {n: expiry for n, expiry in self.nonces.items() if expiry > time.monotonic()}
            if nonce in self.nonces or len(self.nonces) >= 512:
                raise Failure(409, 'Peer request was already used')
            self.nonces[nonce] = time.monotonic() + 60

    def peer(self, node, path, value):
        if node == self.node:
            return self.peer_action(path, value)
        raw, stamp, nonce = encode(value), str(int(time.time())), secrets.token_hex(16)
        headers = {'Content-Type': 'application/json', 'X-Desk-Time': stamp, 'X-Desk-Nonce': nonce,
                   'X-Desk-Signature': self.signature(path, raw, stamp, nonce)}
        url = f"http://{self.members[node]['private']}:{self.config['port']}{path}"
        try:
            with self.opener.open(Request(url, data=raw, headers=headers, method='POST'), timeout=4) as response:
                data = response.read(MAX_BODY + 1)
                if len(data) > MAX_BODY: raise ValueError()
                return json.loads(data)
        except HTTPError as error:
            raise Failure(error.code if error.code in (409, 412, 503) else 503, 'Peer did not accept the operation') from None
        except (OSError, ValueError):
            raise Failure(503, 'Peer is unavailable') from None

    def views(self):
        def one(node):
            try: return node, self.peer(node, '/peer/state', {})
            except Failure: return node, {'node': node, 'ready': False, 'lease': None}
        with ThreadPoolExecutor(max_workers=3) as pool:
            return dict(pool.map(one, self.members))

    def active(self):
        lease = self.state['lease']
        return lease if lease and lease['expires'] > time.time() else None

    @staticmethod
    def matching_ready(views):
        return len(views) == 3 and all(v.get('ready') for v in views.values()) and len({encode(v.get('revisions')) for v in views.values()}) == 1

    def challenge(self, value):
        if set(value) != {'node'} or value['node'] not in self.members:
            raise Failure(400, 'Unknown node')
        views = self.views()
        if any(v.get('lease') and v['lease']['expires'] > time.time() for v in views.values()):
            # Status resolves successful operations before another preparation.
            raise Failure(409, 'Another reboot is still being tracked')
        if not self.matching_ready(views):
            raise Failure(412, 'All three nodes must be healthy and synced before reboot')
        target = value['node']
        payload = {'id': secrets.token_hex(16), 'node': target, 'boot_id': views[target]['boot_id'],
                   'expires': int(time.time()) + CHALLENGE_SECONDS}
        raw = encode(payload)
        token = base64.urlsafe_b64encode(raw).decode().rstrip('=') + '.' + hmac.new(self.key, raw, hashlib.sha256).hexdigest()
        return {'token': token, 'node': target, 'ip': self.members[target]['management'],
                'metadata': views[target].get('metadata', {}), 'expires': payload['expires']}

    def validate_token(self, token):
        try:
            text, signature = token.split('.')
            raw = base64.urlsafe_b64decode(text + '=' * (-len(text) % 4))
            if not hmac.compare_digest(signature, hmac.new(self.key, raw, hashlib.sha256).hexdigest()): raise ValueError()
            payload = json.loads(raw)
            if set(payload) != {'id', 'node', 'boot_id', 'expires'} or payload['node'] not in self.members: raise ValueError()
            if type(payload['expires']) is not int or not time.time() < payload['expires'] <= time.time() + CHALLENGE_SECONDS: raise ValueError()
            if len(payload['id']) != 32 or not isinstance(payload['boot_id'], str): raise ValueError()
            return payload
        except (ValueError, TypeError, KeyError, AttributeError):
            raise Failure(409, 'Confirmation expired or invalid; prepare again') from None

    def confirm(self, token):
        payload = self.validate_token(token)
        target = payload['node']
        if target == self.node:
            # Keep a surviving coordinator when the entrypoint lands on target.
            observer = next(n for n in self.members if n != target)
            return self.peer(observer, '/peer/confirm', {'token': token})
        views = self.views()
        if not self.matching_ready(views) or views[target]['boot_id'] != payload['boot_id']:
            raise Failure(412, 'Node readiness or boot changed; prepare again')
        lease = {**payload, 'expires': int(time.time()) + LEASE_SECONDS, 'started': int(time.time())}
        survivors = [n for n in self.members if n != target]
        acquired = []
        try:
            for node in survivors:
                self.peer(node, '/peer/acquire', lease)
                acquired.append(node)
        except Failure:
            for node in acquired:
                try: self.peer(node, '/peer/release', {'id': lease['id']})
                except Failure: pass
            raise
        # Once execution is requested, keep leases even if reply is lost.
        # Retrying a reboot could reboot the recovered machine a second time.
        try:
            self.peer(target, '/peer/execute', lease)
            uncertain = False
        except Failure:
            uncertain = True
        return {'accepted': True, 'id': lease['id'], 'node': target, 'uncertain': uncertain}

    def peer_action(self, path, value):
        if path == '/peer/state':
            if value != {}: raise Failure(400, 'Invalid state request')
            with self.lock: lease = self.state['lease']
            return {**self.health(), 'lease': lease, 'completed': self.state['completed']}
        if path == '/peer/confirm':
            if set(value) != {'token'}: raise Failure(400, 'Invalid confirmation')
            return self.confirm(value['token'])
        if path == '/peer/complete':
            if set(value) != {'id'}: raise Failure(400, 'Invalid completion')
            with self.lock:
                if self.state['completed'] and self.state['completed']['id'] == value['id']:
                    return {'completed': True}
                lease = self.state['lease'] or self.state['executed']
                if lease is None or lease['id'] != value['id']:
                    raise Failure(409, 'Unknown completed operation')
                # Keep the result after releasing the lock so every observer can
                # see Rejoined, including a browser that was briefly hidden.
                self.state['completed'] = {**lease, 'state':'rejoined',
                    'finished':int(time.time()), 'expires':int(time.time())+3600}
                if self.state['lease'] and self.state['lease']['id'] == value['id']:
                    self.state['lease'] = None
                self.save()
            return {'completed': True}
        if path == '/peer/release':
            if set(value) != {'id'}: raise Failure(400, 'Invalid release')
            with self.lock:
                if self.state['lease'] and self.state['lease']['id'] == value['id']:
                    self.state['lease'] = None
                    self.save()
            return {'released': True}
        if path not in ('/peer/acquire', '/peer/execute'):
            raise Failure(404, 'Unknown operation')
        if (set(value) != {'id', 'node', 'boot_id', 'expires', 'started'} or value['node'] not in self.members
                or not isinstance(value['id'], str) or len(value['id']) != 32
                or not isinstance(value['boot_id'], str) or type(value['started']) is not int
                or type(value['expires']) is not int or not time.time() < value['expires'] <= time.time() + LEASE_SECONDS):
            raise Failure(400, 'Invalid operation lease')
        if path == '/peer/acquire':
            if value['node'] == self.node: raise Failure(400, 'Target cannot hold a survivor lease')
            with self.lock:
                current = self.active()
                if current is not None and current != value:
                    raise Failure(409, 'Another operation holds this node')
                self.state['lease'] = value
                self.save()
            return {'held': True}
        if value['node'] != self.node or value['boot_id'] != boot_id():
            raise Failure(409, 'Target boot changed')
        others = [n for n in self.members if n != self.node]
        for node in others:
            view = self.peer(node, '/peer/state', {})
            if view.get('lease') != value or not view.get('ready'):
                raise Failure(412, 'Both survivors must hold the same operation and remain healthy')
        with self.lock:
            if self.state['executed'] and self.state['executed']['id'] == value['id']:
                raise Failure(409, 'Reboot was already scheduled')
            self.state['executed'] = value
            self.save()
        def run():
            try: self.reboot()
            except (OSError, subprocess.SubprocessError):
                # No retries. Survivor UI times out to needs-attention.
                print('Node reboot command failed; manual review required', flush=True)
        timer = threading.Timer(3, run)
        timer.daemon = True
        timer.start()
        return {'scheduled': True}

    def status(self):
        views = self.views()
        leases = [v['lease'] for v in views.values() if v.get('lease')]
        if not leases:
            completed = [v['completed'] for v in views.values() if v.get('completed')
                         and v['completed']['expires'] > time.time()]
            if not completed: return {'protocol': PROTOCOL, 'operation': None}
            result = max(completed,key=lambda value:value['finished'])
            return {'protocol':PROTOCOL, 'operation':{k:result[k] for k in
                ('id','node','state','started','expires','finished')}}
        active_leases = [v for v in leases if v['expires'] > time.time()]
        lease = min(active_leases or leases, key=lambda v: v['started'])
        target = views.get(lease['node'], {})
        state = 'accepted'
        if not target.get('boot_id'): state = 'offline'
        elif target['boot_id'] != lease['boot_id']:
            state = 'starting'
            if self.matching_ready(views): state = 'rejoined'
        elif time.time() - lease['started'] > 60: state = 'needs_attention'
        if time.time() - lease['started'] > 300 and state != 'rejoined': state = 'needs_attention'
        if state == 'rejoined':
            for node in self.members:
                try: self.peer(node, '/peer/complete', {'id': lease['id']})
                except Failure: pass
        return {'protocol': PROTOCOL, 'operation': {'id': lease['id'], 'node': lease['node'],
            'state': state, 'started': lease['started'], 'expires': lease['expires']}}


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *args): pass

    def respond(self, status, value):
        raw = encode(value)
        self.send_response(status)
        self.send_header('Content-Type', 'application/json')
        self.send_header('Cache-Control', 'no-store')
        self.send_header('Content-Length', str(len(raw)))
        self.send_header('X-Content-Type-Options', 'nosniff')
        self.end_headers()
        self.wfile.write(raw)

    def handle_request(self):
        control = self.server.control
        try:
            allowed = {'127.0.0.1', *(m['private'] for m in control.members.values())}
            if self.client_address[0] not in allowed: raise Failure(403, 'Private cluster peers only')
            if self.path == '/health' and self.command == 'GET':
                self.respond(200, {'ready': True}); return
            if self.path == '/api/cluster/operations' and self.command == 'GET':
                self.respond(200, control.status()); return
            if self.command != 'POST': raise Failure(405, 'Method not allowed')
            if self.headers.get('Content-Type') != 'application/json': raise Failure(415, 'JSON required')
            if self.headers.get('Transfer-Encoding'): raise Failure(400, 'Content length required')
            try: size = int(self.headers.get('Content-Length', '-1'))
            except ValueError: raise Failure(400, 'Invalid content length')
            if not 0 <= size <= MAX_BODY: raise Failure(413, 'Invalid request size')
            self.connection.settimeout(5)
            raw = self.rfile.read(size)
            if len(raw) != size: raise Failure(400, 'Incomplete request')
            if self.path.startswith('/peer/'):
                control.authenticate(self.path, raw, self.headers)
                value = json.loads(raw)
                if not isinstance(value, dict): raise Failure(400, 'Invalid body')
                self.respond(200, control.peer_action(self.path, value)); return
            host = control.config['public_host']
            if (self.headers.get('Host') != host or self.headers.get('Origin') != 'http://' + host
                    or self.headers.get('X-Desk-Action') != 'confirm-node-operation'):
                raise Failure(403, 'Same-origin confirmation required')
            value = json.loads(raw)
            if not isinstance(value, dict): raise Failure(400, 'Invalid body')
            if self.path == '/api/cluster/operations/prepare':
                self.respond(200, control.challenge(value))
            elif self.path == '/api/cluster/operations/reboot' and set(value) == {'token'}:
                self.respond(202, control.confirm(value['token']))
            else: raise Failure(404, 'Unknown operation')
        except Failure as error: self.respond(error.status, {'error': error.message})
        except (OSError, ValueError, TypeError, KeyError): self.respond(503, {'error': 'Node operation is unavailable; no automatic retry'})

    do_GET = do_POST = do_PUT = do_DELETE = do_OPTIONS = handle_request


def load_config(path):
    value = json.loads(Path(path).read_text())
    if (set(value) != {'node', 'port', 'members', 'key', 'public_host', 'dashboard_port', 'dns_port', 'nats_port'}
            or len(value['members']) != 3 or len(bytes.fromhex(value['key'])) != 32
            or not ipaddress.ip_address(value['public_host']).is_private): raise ValueError('Invalid node operation config')
    members = value['members']
    if sorted(m['id'] for m in members) != ['deskb1x', 'deskb2x', 'deskb3x'] or value['node'] not in {m['id'] for m in members}: raise ValueError('Invalid membership')
    for member in members:
        if set(member) != {'id', 'private', 'management'} or not all(ipaddress.ip_address(member[k]).is_private for k in ('private', 'management')): raise ValueError('Invalid peer address')
    for key in ('port', 'dashboard_port', 'dns_port', 'nats_port'):
        if type(value[key]) is not int or not 1024 <= value[key] <= 65535: raise ValueError('Invalid port')
    return value


def main():
    import sys
    config = load_config(sys.argv[1])
    control = Control(config, '/var/lib/desk-node-control/state.json')
    host = control.members[control.node]['private']
    server = ThreadingHTTPServer((host, config['port']), Handler)
    server.control = control
    server.daemon_threads = True
    server.serve_forever(poll_interval=1)


if __name__ == '__main__': main()
