#!/usr/bin/env python3
"""Confirmed node maintenance/reboot, survivor leases and recovery tracking.

No shell, arbitrary command, SSH, provider call or database.
Only explicit confirmation starts drain and, if selected, an ordinary reboot.
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
DNS_QUIET_SECONDS = 10  # Current private DNS proxy timeout server: 10s.
BOOT_PATH = '/proc/sys/kernel/random/boot_id'
HISTORY_LIMIT = 4
HISTORY_SECONDS = 6 * 3600
HISTORY_STATES = frozenset(('accepted', 'draining', 'releasing_vips', 'maintenance',
    'rebooting', 'offline', 'starting', 'services_ready', 'configuration_synced',
    'recovering', 'rejoined', 'resumed', 'needs_attention'))
HISTORY_TERMINAL = frozenset(('rejoined', 'resumed'))
HISTORY_ORDER = ('accepted', 'draining', 'releasing_vips', 'maintenance', 'rebooting',
    'offline', 'starting', 'services_ready', 'configuration_synced', 'recovering',
    'needs_attention', 'resumed', 'rejoined')


class Failure(Exception):
    def __init__(self, status, message):
        self.status, self.message = status, message


def encode(value):
    return json.dumps(value, separators=(',', ':'), sort_keys=True).encode()


def boot_id():
    return Path(BOOT_PATH).read_text().strip()


def dns_ready(host, port):
    """Frontend liveness independent of an intentionally withdrawn backend."""
    identity = secrets.randbelow(65536)
    name = b''.join(bytes([len(p)]) + p.encode() for p in 'frontend.desk-dns.test'.split('.')) + b'\0'
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
        self.resume_lock = threading.Lock()
        self.nonces = {}
        self.cached_health = (0, None)
        self.opener = build_opener(ProxyHandler({}))
        self.state = {'lease': None, 'executed': None, 'completed': None, 'maintenance': None, 'history': []}
        if self.path.exists():
            try:
                value = json.loads(self.path.read_text())
                if not isinstance(value,dict) or not {'lease','executed'} <= set(value) <= set(self.state):
                    raise ValueError()
                self.state.update(value)
            except (OSError, ValueError, TypeError):
                raise RuntimeError('Node control state needs review') from None
        self.state['history'] = self.merge_history([self.state['history']])
        self.reboot = reboot or self.system_reboot

    def merge_history(self, sources):
        """Sanitized display metadata only; never used to authorize an action."""
        records = {}
        now = int(time.time())
        for source in sources:
            if not isinstance(source, list): continue
            for value in source[:HISTORY_LIMIT]:
                if (not isinstance(value, dict) or not isinstance(value.get('node'), str) or value['node'] not in self.members
                        or not isinstance(value.get('id'), str) or len(value['id']) != 32
                        or any(c not in '0123456789abcdef' for c in value['id'])
                        or value.get('action') not in ('maintenance', 'reboot')
                        or type(value.get('started')) is not int
                        or not now-HISTORY_SECONDS <= value['started'] <= now+5
                        or not isinstance(value.get('events'), list)):
                    continue
                key = (value['id'], value['node'], value['started'])
                record = records.setdefault(key, {k:value[k] for k in ('id','node','action','started')})
                if value['action'] == 'reboot': record['action'] = 'reboot'
                events = record.setdefault('events', {})
                for event in value['events'][:len(HISTORY_STATES)]:
                    if (not isinstance(event, dict) or not isinstance(event.get('state'), str) or event['state'] not in HISTORY_STATES
                            or type(event.get('at')) is not int
                            or not value['started'] <= event['at'] <= now+5):
                        continue
                    phase = event['state']
                    # Several observers may see one milestone. Retain its first observation.
                    events[phase] = min(events.get(phase, event['at']), event['at'])
        output = []
        for record in records.values():
            steps = [{'state':state,'at':stamp} for state,stamp in record.pop('events').items()]
            steps.sort(key=lambda e: (e['at'], list(HISTORY_ORDER).index(e['state'])))
            if not steps: continue
            terminals = [e for e in steps if e['state'] in HISTORY_TERMINAL]
            last = terminals[-1] if terminals else steps[-1]
            record.update(events=steps, state=last['state'])
            if terminals: record['finished'] = last['at']
            output.append(record)
        return sorted(output, key=lambda r:r.get('finished', r['events'][-1]['at']), reverse=True)[:HISTORY_LIMIT]

    def record_history(self, operation, state, at=None):
        if not operation or state not in HISTORY_STATES: return False
        with self.lock:
            old = self.state['history']
            existing = next((r for r in old if r['id'] == operation['id']), None)
            if existing and ((existing['state'] in HISTORY_TERMINAL and state not in ('services_ready','configuration_synced')) or any(e['state']==state for e in existing['events'])):
                return False
            record = {k:operation[k] for k in ('id','node','started')}
            record['action'] = operation.get('action', 'reboot')
            record['events'] = [{'state':state, 'at':max(operation['started'], int(time.time() if at is None else at))}]
            updated = self.merge_history([old, [record]])
            if updated == old: return False
            self.state['history'] = updated
            return True

    def save(self):
        # Piggyback durable lifecycle milestones on the existing state writes.
        operation = self.state['maintenance'] or self.state['executed']
        if operation:
            self.record_history(operation, operation.get('state', 'accepted'))
        completed = self.state['completed']
        if completed:
            self.record_history(completed, 'rejoined', completed['finished'])
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
            result['metadata'] = {k: value.get(k) for k in ('ip', 'vip', 'core', 'mqtt', 'activity', 'connections', 'admission', 'maintenance_supported', 'dns_health', 'vpn_ready', 'homeassistant')}
            result['revisions'] = {k: configs.get(k, {}).get('revision') for k in ('xiaozhi', 'dns', 'monitoring')}
            valid = value.get('id') == self.node and value.get('boot_id') == result['boot_id'] and 0 <= age <= 15
            if valid:
                result['readiness'] = {
                    'services':all(components.get(k) == 'ready' for k in ('worker','core','mqtt','dns')),
                    'configuration':all(configs.get(k, {}).get('state') == 'synced' for k in ('xiaozhi','dns','monitoring')),
                    'waiting':[k for k in ('worker','core','mqtt','dns') if components.get(k) != 'ready'] +
                        [k+' config' for k in ('xiaozhi','dns','monitoring') if configs.get(k,{}).get('state') != 'synced']}
            valid &= all(components.get(k) == 'ready' for k in ('worker', 'core', 'mqtt', 'dns'))
            valid &= all(configs.get(k, {}).get('state') == 'synced' for k in ('xiaozhi', 'dns', 'monitoring'))
            if valid:
                routes = self.get(f"http://{member['private']}:{self.config['nats_port']}/routez")
                valid = routes.get('num_routes', 0) >= 2 and dns_ready(member['management'], self.config['dns_port'])
            result['ready'] = bool(valid)
            if 'readiness' in result and not valid and not result['readiness']['waiting']:
                result['readiness']['waiting'].append('cluster routing')
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

    @staticmethod
    def dns_survivors(views, target, withdrawn=False):
        expected = set(views) - {target}
        if len(expected) != 2:
            return False
        for node in expected:
            health = views[node].get('metadata', {}).get('dns_health', {})
            eligible = health.get('eligible_backends', [])
            if (health.get('mode') != 'adguard' or not expected <= set(eligible)
                    or (withdrawn and target in eligible)):
                return False
        return True

    def challenge(self, value):
        if not {'node'} <= set(value) <= {'node','action'} or value['node'] not in self.members or value.get('action','reboot') not in ('maintenance','reboot'):
            raise Failure(400, 'Unknown node')
        views = self.views()
        maintained = views.get(value['node'], {}).get('maintenance')
        maintenance_id = maintained['id'] if (maintained and maintained.get('state') == 'maintenance'
            and maintained['expires'] > time.time() and value.get('action','reboot') == 'reboot') else None
        if any(v.get('maintenance') and (n!=value['node'] or maintenance_id is None) for n,v in views.items()):
            raise Failure(409, 'A node is still in maintenance or recovering')
        if any(v.get('lease') and v['lease']['expires'] > time.time()
               and v['lease']['id'] != maintenance_id for v in views.values()):
            # Status resolves successful operations before another preparation.
            raise Failure(409, 'Another reboot is still being tracked')
        if not self.matching_ready(views):
            raise Failure(412, 'All three nodes must be healthy and synced before reboot')
        target = value['node']
        if not self.dns_survivors(views, target):
            raise Failure(412, 'Both surviving AdGuard backends must be eligible before maintenance')
        if views[target].get('metadata', {}).get('maintenance_supported') is not True:
            raise Failure(412, 'Deploy maintenance support on worker, core and gateway first')
        payload = {'id': secrets.token_hex(16), 'node': target, 'boot_id': views[target]['boot_id'],
                   'expires': int(time.time()) + CHALLENGE_SECONDS, 'action':value.get('action','reboot'), 'maintenance_id':maintenance_id}
        raw = encode(payload)
        token = base64.urlsafe_b64encode(raw).decode().rstrip('=') + '.' + hmac.new(self.key, raw, hashlib.sha256).hexdigest()
        return {'token': token, 'node': target, 'ip': self.members[target]['management'],
                'metadata': views[target].get('metadata', {}), 'expires': payload['expires'], 'action':payload['action']}

    def validate_token(self, token):
        try:
            text, signature = token.split('.')
            raw = base64.urlsafe_b64decode(text + '=' * (-len(text) % 4))
            if not hmac.compare_digest(signature, hmac.new(self.key, raw, hashlib.sha256).hexdigest()): raise ValueError()
            payload = json.loads(raw)
            if set(payload) != {'id', 'node', 'boot_id', 'expires', 'action', 'maintenance_id'} or payload['action'] not in ('maintenance','reboot') or payload['node'] not in self.members: raise ValueError()
            if type(payload['expires']) is not int or not time.time() < payload['expires'] <= time.time() + CHALLENGE_SECONDS: raise ValueError()
            if payload['maintenance_id'] is not None and (not isinstance(payload['maintenance_id'],str) or len(payload['maintenance_id'])!=32 or payload['action']!='reboot'): raise ValueError()
            if len(payload['id']) != 32 or not isinstance(payload['boot_id'], str): raise ValueError()
            return payload
        except (ValueError, TypeError, KeyError, AttributeError):
            raise Failure(409, 'Confirmation expired or invalid; prepare again') from None

    def confirm(self, token):
        payload = self.validate_token(token)
        target = payload['node']
        if payload['maintenance_id'] is not None:
            return self.peer(target, '/peer/reboot-ready', {'token':token})
        if target == self.node:
            # Keep a surviving coordinator when the entrypoint lands on target.
            observer = next(n for n in self.members if n != target)
            return self.peer(observer, '/peer/confirm', {'token': token})
        views = self.views()
        executed = views[target].get('executed')
        if executed and executed.get('id')==payload['id']:
            return {'accepted':True,'id':payload['id'],'node':target,'uncertain':True}
        if any(v.get('maintenance') for v in views.values()) or not self.matching_ready(views) or views[target]['boot_id'] != payload['boot_id']:
            raise Failure(412, 'Node readiness or boot changed; prepare again')
        lease = {k:v for k,v in payload.items() if k!='maintenance_id'}
        lease = {**lease, 'expires': int(time.time()) + LEASE_SECONDS, 'started': int(time.time())}
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
            return {**self.health(), 'lease': lease, 'completed': self.state['completed'], 'maintenance':self.state['maintenance'], 'executed':self.state['executed'], 'history':self.state['history']}
        if path == '/peer/reboot-ready':
            if set(value) != {'token'}: raise Failure(400, 'Invalid maintained reboot')
            token = self.validate_token(value['token'])
            with self.lock:
                current = self.state['maintenance']
                executed = self.state['executed']
                if (not current or current['state'] != 'maintenance' or current['expires'] <= time.time()+90
                        or token['node'] != self.node or token['boot_id'] != boot_id()
                        or token['maintenance_id'] != current['id'] or not executed):
                    raise Failure(409, 'Maintenance changed; prepare again')
            views = self.views()
            if not self.matching_ready(views) or not self.dns_survivors(views,self.node,withdrawn=True) or any(views[n].get('lease') != executed for n in self.members if n!=self.node):
                raise Failure(412, 'Survivor readiness or lease changed')
            meta = views[self.node].get('metadata', {})
            counts = [meta.get('core'),meta.get('mqtt'),*meta.get('activity',{}).values(),*meta.get('connections',{}).values()]
            if (len(counts)<7 or not all(type(n) is int and n==0 for n in counts)
                    or not all(meta.get('admission',{}).get(k) is True for k in ('worker','core','mqtt'))
                    or not all(meta.get('vip',{}).get(k) is False for k in self.managed_vips(views))):
                raise Failure(412, 'Node drain is not complete')
            with self.lock:
                if self.state['maintenance'] is not current or current['state']!='maintenance':
                    raise Failure(409, 'Maintenance changed')
                current['state'],current['action']='rebooting','reboot'
                current['reboot_started']=int(time.time())
                self.save()
            def reboot_ready():
                try: self.reboot()
                except (OSError, subprocess.SubprocessError): self.resume(current['id'],recovering=True)
            timer=threading.Timer(3,reboot_ready);timer.daemon=True;timer.start()
            return {'accepted':True,'id':current['id'],'node':self.node,'uncertain':False}
        if path == '/peer/resume':
            if set(value) != {'id'} or not isinstance(value['id'],str): raise Failure(400, 'Invalid resume')
            return self.request_resume(value['id'])
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
        if (set(value) != {'id', 'node', 'boot_id', 'expires', 'started', 'action'} or value.get('action') not in ('maintenance','reboot') or value['node'] not in self.members
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
            self.begin_maintenance(value)
        timer = threading.Timer(3, run)
        timer.daemon = True
        timer.start()
        return {'scheduled': True, 'action':value['action']}

    def write_admission(self, value):
        path = Path('/run/desk-node-maintenance/state.json')
        path.parent.mkdir(parents=True, exist_ok=True, mode=0o755)
        raw = {'protocol':'desk-node-maintenance-v1', 'node':self.node,
               'boot_id':value['boot_id'], 'expires':value['expires']}
        temporary = path.with_suffix('.tmp')
        with temporary.open('w') as stream:
            os.chmod(temporary, 0o644)
            json.dump(raw, stream)
        os.replace(temporary, path)

    @staticmethod
    def managed_vips(views):
        keys = ('dns', 'xiaozhi')
        if Path('/etc/desk-vpn/public.json').is_file() or any(v.get('metadata', {}).get('vip', {}).get('vpn') is not None for v in views.values()):
            keys += ('vpn',)
        return keys

    @staticmethod
    def vip_service(name, action):
        if name not in ('keepalived', 'desk-dns-vip', 'desk-vpn-vip') or action not in ('start', 'stop'):
            raise ValueError('Invalid fixed service action')
        subprocess.run(['/usr/bin/systemctl', action, name], check=True, timeout=15,
                       stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

    def request_resume(self, operation_id):
        # A peer request must acknowledge before its four-second HTTP deadline.
        # The existing recovery monitor owns service starts and lease release.
        with self.lock:
            value = self.state['maintenance']
            if value is None or value['id'] != operation_id:
                return {'resumed':True}
            if value['state'] == 'rebooting' and value['boot_id'] == boot_id():
                raise Failure(409, 'Reboot was already requested; wait for recovery')
            value['state'] = 'recovering'
            self.save()
        return {'resuming':True}

    def resume(self, operation_id, recovering=False):
        # Cancel, expiry and explicit Resume can coincide. Restore once.
        with self.resume_lock:
            return self._resume(operation_id, recovering)

    def _resume(self, operation_id, recovering=False):
        with self.lock:
            value = self.state['maintenance']
            if value is None or value['id'] != operation_id:
                return {'resumed':True}
            # Mark recovery before service actions so restart can safely retry.
            if value['state'] == 'rebooting' and value['boot_id'] == boot_id() and not recovering:
                raise Failure(409, 'Reboot was already requested; wait for recovery')
            changed_boot = value['boot_id'] != boot_id() and value.get('action') == 'reboot'
            value['state'] = 'recovering'
            self.save()
        Path('/run/desk-node-maintenance/state.json').unlink(missing_ok=True)
        for service in value.get('services', []):
            self.vip_service(service, 'start')
        with self.lock:
            if not changed_boot:
                self.record_history(value, 'resumed')
            self.state['maintenance'] = None
            if not changed_boot: self.state['executed'] = None
            self.save()
            self.cached_health = (0, None)
        for node in self.members:
            if node != self.node and not changed_boot:
                try: self.peer(node, '/peer/release', {'id':operation_id})
                except Failure: pass
        return {'resumed':True}

    def homeassistant_drained(self, views):
        """Do not reboot before a clean Core handoff and matching survivor checkpoints."""
        statuses = {node: view.get('metadata', {}).get('homeassistant') for node, view in views.items()}
        if not any(statuses.values()):
            return not Path('/etc/desk-homeassistant/config.json').is_file()
        if len(statuses) != 3 or any(not isinstance(value, dict) for value in statuses.values()):
            return False
        local = statuses.get(self.node, {})
        peers = [value for node, value in statuses.items() if node != self.node]
        return (local.get('state') == 'standby' and all(value.get('sync') == 'synced' for value in statuses.values())
                and len({value.get('checkpoint_revision') for value in statuses.values()}) == 1
                and sum(value.get('ready') is True for value in peers) == 1)

    def begin_maintenance(self, lease):
        value = {**lease, 'state':'draining', 'services':[]}
        with self.lock:
            self.state['maintenance'] = value
            self.save()
        self.write_admission(value)
        deadline = min(lease['expires'], time.time() + 240)
        dns_withdrawn_at = None
        try:
            while time.time() < deadline:
                with self.lock:
                    if self.state['maintenance'] is not value or value['state'] != 'draining':
                        return
                views = self.views()
                survivors = [views[n] for n in self.members if n != self.node]
                if not (all(v.get('ready') and v.get('lease') == lease for v in survivors)
                        and len({encode(v.get('revisions')) for v in survivors}) == 1
                        and self.dns_survivors(views,self.node)):
                    raise Failure(412, 'Survivor readiness changed during drain')
                local = views.get(self.node, {})
                meta = local.get('metadata', {})
                counts = [meta.get('core'), meta.get('mqtt'), *meta.get('activity', {}).values(),
                          *meta.get('connections', {}).values()]
                paused = meta.get('admission', {})
                dns_drained = self.dns_survivors(views,self.node,withdrawn=True)
                if not dns_drained:
                    dns_withdrawn_at = None
                elif dns_withdrawn_at is None:
                    dns_withdrawn_at = time.monotonic()
                dns_quiet = dns_withdrawn_at is not None and time.monotonic()-dns_withdrawn_at >= DNS_QUIET_SECONDS
                if (dns_quiet and self.homeassistant_drained(views) and local.get('ready') and meta.get('maintenance_supported') is True
                        and all(paused.get(k) is True for k in ('worker', 'core', 'mqtt'))
                        and len(counts) >= 7 and all(type(n) is int and n == 0 for n in counts)):
                    break
                time.sleep(1)
            else:
                raise Failure(409, 'Drain timed out; existing sessions were retained')
            with self.lock:
                if self.state['maintenance'] is not value or value['state'] != 'draining': return
                # Preserve exactly which election services were running.
                for service in (('keepalived', 'desk-dns-vip', 'desk-vpn-vip') if 'vpn' in self.managed_vips(views) else ('keepalived', 'desk-dns-vip')):
                    active = subprocess.run(['/usr/bin/systemctl','is-active',service],
                                            capture_output=True, text=True, timeout=5)
                    if active.stdout.strip() == 'active': value['services'].append(service)
                value['state'] = 'releasing_vips'
                self.save()
                for service in value['services']: self.vip_service(service, 'stop')
            while time.time() < deadline:
                with self.lock:
                    if self.state['maintenance'] is not value or value['state'] != 'releasing_vips': return
                views = self.views()
                local = views.get(self.node, {}).get('metadata', {}).get('vip', {})
                # Every configured VIP must be present on exactly one healthy survivor.
                peers = [views[n] for n in self.members if n != self.node]
                if (all(local.get(k) is False for k in self.managed_vips(views))
                        and all(v.get('ready') and v.get('lease') == lease for v in peers)
                        and len({encode(v.get('revisions')) for v in peers}) == 1
                        and all(sum(v.get('metadata', {}).get('vip', {}).get(k) is True for v in peers) == 1
                                for k in self.managed_vips(views))
                        and all(v.get('metadata', {}).get('vpn_ready') is True for v in peers
                                if v.get('metadata', {}).get('vip', {}).get('vpn') is True)):
                    with self.lock:
                        if self.state['maintenance'] is not value: return
                        value['state'] = 'maintenance'
                        self.save()
                        if lease.get('action', 'reboot') == 'reboot':
                            value['state'] = 'rebooting'
                            self.save()
                            self.reboot()
                    return
                time.sleep(1)
            raise Failure(409, 'VIP handoff did not complete')
        except (Failure, OSError, subprocess.SubprocessError):
            try: self.resume(lease['id'], recovering=True)
            except (Failure, OSError, subprocess.SubprocessError):
                print('Maintenance recovery needs attention', flush=True)
            print('Maintenance cancelled; no forced session termination or reboot', flush=True)

    def monitor_maintenance(self):
        while True:
            with self.lock: value = self.state['maintenance']
            if value and (value['boot_id'] != boot_id() or value['expires'] <= time.time()
                          or value['state'] == 'recovering'
                          or (value['state'] in ('draining','releasing_vips') and time.time()-value['started']>=240)):
                try: self.resume(value['id'], recovering=True)
                except (Failure, OSError, subprocess.SubprocessError): pass
            elif value and not Path('/run/desk-node-maintenance/state.json').exists():
                try: self.write_admission(value)
                except OSError: pass
            time.sleep(1)

    def status(self):
        views = self.views()
        result = self.operation_status(views)
        operation = result.get('operation')
        with self.lock:
            changed = False
            if operation:
                target = views.get(operation['node'], {})
                readiness = target.get('readiness')
                if isinstance(readiness, dict):
                    operation['readiness'] = readiness
                    if operation['state'] in ('starting','rejoined') and 'finished' not in operation:
                        for phase, key in (('services_ready','services'), ('configuration_synced','configuration')):
                            if readiness.get(key) is True:
                                changed |= self.record_history(operation, phase)
                changed |= self.record_history(operation, operation['state'], operation.get('finished'))
            if changed:
                try: self.save()  # Newly observed milestones only; no writes on idle polls.
                except OSError: pass  # Display history must not fail a status read.
            result['history'] = self.merge_history([self.state['history']] + [v.get('history', []) for v in views.values()])
        return result

    def operation_status(self, views):
        leases = [v['lease'] for v in views.values() if v.get('lease')]
        if not leases:
            completed = [v['completed'] for v in views.values() if v.get('completed')
                         and v['completed']['expires'] > time.time()]
            if not completed: return {'protocol': PROTOCOL, 'operation': None, 'idle_confirmed':all(v.get('ready') and not v.get('maintenance') for v in views.values())}
            result = max(completed,key=lambda value:value['finished'])
            return {'protocol':PROTOCOL, 'operation':{k:result[k] for k in
                ('id','node','state','started','expires','finished','action') if k in result}}
        active_leases = [v for v in leases if v['expires'] > time.time()]
        lease = min(active_leases or leases, key=lambda v: v['started'])
        target = views.get(lease['node'], {})
        maintenance = target.get('maintenance')
        state = maintenance.get('state', 'accepted') if maintenance else 'accepted'
        if not target.get('boot_id'): state = 'offline'
        elif target['boot_id'] != lease['boot_id']:
            state = 'starting'
            if self.matching_ready(views): state = 'rejoined'
        elif not maintenance and time.time() - lease['started'] > 60: state = 'needs_attention'
        if time.time() - (maintenance.get('reboot_started',lease['started']) if maintenance else lease['started']) > 300 and state not in ('rejoined','maintenance'): state = 'needs_attention'
        if state == 'rejoined':
            for node in self.members:
                try: self.peer(node, '/peer/complete', {'id': lease['id']})
                except Failure: pass
        return {'protocol': PROTOCOL, 'operation': {'id': lease['id'], 'node': lease['node'],
            'state': state, 'action':maintenance.get('action',lease.get('action','reboot')) if maintenance else lease.get('action','reboot'), 'started': lease['started'], 'expires': lease['expires']}}


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
        try:
            self.wfile.write(raw)
        except ConnectionError:
            pass  # The caller timed out; the accepted operation retains ownership.

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
            elif self.path == '/api/cluster/operations/resume' and set(value) == {'node','id'} and value['node'] in control.members:
                self.respond(200, control.peer(value['node'], '/peer/resume', {'id':value['id']}))
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
    monitor = threading.Thread(target=control.monitor_maintenance, daemon=True)
    monitor.start()
    server.serve_forever(poll_interval=1)


if __name__ == '__main__': main()
