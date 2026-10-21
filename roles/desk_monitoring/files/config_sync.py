#!/usr/bin/env python3
"""Event-driven Beszel configuration replication; metrics never enter this journal."""
import argparse
import base64
from concurrent.futures import ThreadPoolExecutor
import copy
import hashlib
import hmac
import http.server
import json
import logging
import os
from pathlib import Path
import re
import secrets
import socketserver
import sqlite3
from contextlib import closing
import ssl
import sys
import subprocess
import threading
import time
import urllib.error
import urllib.parse
import urllib.request

from cryptography.fernet import Fernet

LIMIT = 2 * 1024 * 1024
BATCH_TIMEOUT = 30  # Password hashing must fit the CPU-capped native hub transaction.
COLLECTIONS = ('users', '_superusers', 'systems', 'user_settings', 'alerts',
               'quiet_hours', 'network_monitors', 'fingerprints', 'universal_tokens')
FIELDS = {
    'users': {'email', 'emailVisibility', 'verified', 'name', 'username', 'role'},
    '_superusers': {'email', 'emailVisibility', 'verified'},
    'systems': {'name', 'host', 'port', 'users', 'paused'},
    'user_settings': {'user', 'settings'},
    'alerts': {'user', 'system', 'name', 'min', 'value'},
    'quiet_hours': {'user', 'system', 'start', 'end', 'type'},
    'network_monitors': {'system', 'server', 'target', 'protocol', 'port', 'interval', 'enabled'},
    'fingerprints': {'system', 'token'},
    'universal_tokens': {'user', 'token'},
}
RELATIONS = {'systems': {'users': 'users'}, 'user_settings': {'user': 'users'},
             'alerts': {'user': 'users', 'system': 'systems'},
             'quiet_hours': {'user': 'users', 'system': 'systems'},
             'network_monitors': {'system': 'systems'},
             'fingerprints': {'system': 'systems'}, 'universal_tokens': {'user':'users'}}
RECORD_ROUTE = re.compile(r'^/api/collections/([a-zA-Z0-9_]+)/records(?:/([a-z0-9]{6,15}))?$')
ID_LENGTHS={'quiet_hours':10,'network_monitors':10,'universal_tokens':10,'fingerprints':9}

def record_identity(collection):
    return secrets.token_hex(8)[:ID_LENGTHS.get(collection,15)]

LOG = logging.getLogger('beszel-config')


def encoded(value):
    return json.dumps(value, sort_keys=True, separators=(',', ':'), allow_nan=False).encode()


def digest(value):
    return hashlib.sha256(encoded(value)).hexdigest()


def atomic(path, data):
    temporary = path.with_name(path.name + '.pending')
    fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, 'wb') as stream:
        stream.write(data)
        stream.flush()
        os.fsync(stream.fileno())
    os.replace(temporary, path)
    fd = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


# The same tiny configuration journal is reused by the DNS policy boundary.
import importlib.util
_journal_path = Path(__file__).with_name('config-journal.py')
if not _journal_path.exists(): _journal_path = Path(__file__).with_name('config_journal.py')
_spec = importlib.util.spec_from_file_location('desk_config_journal', _journal_path)
_journal_module = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_journal_module)
Failure, Journal = _journal_module.Failure, _journal_module.Journal

def jwt_from_local_record(database, collection, identity=None, admin_email='admin@desk.cluster'):
    """Match pinned PB 0.40.4's short-lived native token format; never print keys.

    Runs as beszel-hub, the existing database owner. SQLite is read-only. All
    configuration writes still use PocketBase APIs, validation and record hooks.
    """
    with closing(sqlite3.connect('file:' + str(database) + '?mode=ro', uri=True)) as db:
        row = db.execute('SELECT id, options FROM _collections WHERE name=?', (collection,)).fetchone()
        if not row:
            raise Failure(503, 'Beszel auth collection unavailable')
        collection_id, options = row[0], json.loads(row[1])
        if identity is None:
            users = db.execute('SELECT id,tokenKey FROM _superusers WHERE email=?',(admin_email,)).fetchall()
            if len(users) != 1:
                raise Failure(409, 'Exactly one local monitoring administrator required')
            identity, token_key = users[0]
        else:
            users = db.execute('SELECT tokenKey FROM ' + collection + ' WHERE id=?', (identity,)).fetchall()
            if len(users) != 1:
                raise Failure(403, 'Monitoring identity unavailable')
            token_key = users[0][0]
    b64 = lambda data: base64.urlsafe_b64encode(data).rstrip(b'=')
    claims = {'type': 'auth', 'id': identity, 'collectionId': collection_id,
              'refreshable': False, 'exp': int(time.time()) + 60}
    signing = b64(encoded({'alg': 'HS256', 'typ': 'JWT'})) + b'.' + b64(encoded(claims))
    signature = hmac.new((token_key + options['authToken']['secret']).encode(), signing, hashlib.sha256).digest()
    return (signing + b'.' + b64(signature)).decode()


class Hub:
    def __init__(self, url, database, admin_email='admin@desk.cluster'):
        self.url, self.database = url, database
        self.admin_email=admin_email
        class NoRedirect(urllib.request.HTTPRedirectHandler):
            def redirect_request(self, *args, **kwargs):
                return None
        self.opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), NoRedirect())

    def request(self, path, method='GET', body=None, token=None):
        headers = {'Content-Type': 'application/json', 'Authorization': token or jwt_from_local_record(self.database, '_superusers', admin_email=self.admin_email)}
        request = urllib.request.Request(self.url + path, data=None if body is None else encoded(body),
                                         headers=headers, method=method)
        try:
            with self.opener.open(request, timeout=45 if method!='GET' else 8) as response:
                raw = response.read(LIMIT + 1)
                if len(raw) > LIMIT:
                    raise Failure(502, 'Monitoring API response exceeds boundary')
                return json.loads(raw) if raw.strip() else None
        except urllib.error.HTTPError as error:
            error.close()
            raise Failure(error.code, 'Beszel rejected configuration; no secrets logged') from None

    def health(self):
        try:
            with self.opener.open(self.url + '/api/health', timeout=2) as response:
                return response.status == 200
        except Exception: return False

    def collection_name(self, identity):
        if identity in COLLECTIONS: return identity
        with closing(sqlite3.connect('file:' + str(self.database) + '?mode=ro', uri=True)) as db:
            row=db.execute('SELECT name FROM _collections WHERE id=?',(identity,)).fetchone()
        if not row or row[0] not in COLLECTIONS:
            raise Failure(405,'This collection is not shared monitoring configuration')
        return row[0]

    def records(self, collection):
        path = '/api/collections/' + collection + '/records?perPage=500&sort=id'
        value = self.request(path)
        if value['totalItems'] > 500:
            raise Failure(409, 'Monitoring configuration exceeds supported record count')
        return value['items']

    def full_settings(self):
        # PocketBase's settings API masks SMTP/S3 secrets. Read their existing
        # native values as the database owner, never write SQLite here.
        with closing(sqlite3.connect('file:' + str(self.database) + '?mode=ro', uri=True)) as db:
            value = json.loads(db.execute("SELECT value FROM _params WHERE id='settings'").fetchone()[0])
        if not isinstance(value, dict):
            raise Failure(503, 'Encrypted native settings need migration before sync')
        return value

    def snapshot(self):
        records = {name: self.records(name) for name in COLLECTIONS}
        # Avatars require binary replication; reject rather than silently lose one.
        if any(row.get('avatar') for row in records['users']):
            raise Failure(409, 'Existing avatar files require migration before config sync')
        collections = {name: {} for name in COLLECTIONS}
        for name, rows in records.items():
            for row in rows:
                value = {k: copy.deepcopy(v) for k, v in row.items() if k in FIELDS[name]}
                if name == 'systems':
                    value['paused'] = row.get('status') == 'paused'
                collections[name][row['id']] = value
        settings = self.full_settings()
        # API batch is needed for atomic per-hub changes, not for metrics sampling.
        settings['batch']['enabled'] = True
        settings['batch']['timeout'] = max(BATCH_TIMEOUT, settings['batch'].get('timeout', 0))
        return {'collections': collections, 'settings': settings}


def natural_key(collection, row, reverse):
    if collection == 'users':
        return row['email'].lower()
    if collection == '_superusers':
        return row['email'].lower()
    if collection == 'systems':
        return row['host'], str(row['port'])
    if collection == 'network_monitors':
        return (reverse['systems'].get(row['system'], row['system']), row['target'], row['protocol'],
                row.get('port', 0) if row['protocol'] == 'tcp' else 0,
                row.get('server', '') if row['protocol'] == 'dns' else '')
    if collection in ('user_settings', 'fingerprints', 'universal_tokens'):
        field = 'system' if collection == 'fingerprints' else 'user'
        return reverse[RELATIONS[collection][field]].get(row[field], row[field])
    return None


def translate_refs(collection, value, mapping):
    value = copy.deepcopy(value)
    for field, target in RELATIONS.get(collection, {}).items():
        if field in value:
            def one(identity):
                if identity == '':
                    return ''
                if identity not in mapping[target]:
                    raise Failure(409, 'Monitoring relation is not in the shared configuration')
                return mapping[target][identity]
            value[field] = [one(x) for x in value[field]] if isinstance(value[field], list) else one(value[field])
    return value


def match_mapping(value, local, existing):
    mapping = copy.deepcopy(existing)
    for collection in COLLECTIONS:
        mapping.setdefault(collection, {})
        reverse = {name: {v: k for k, v in rows.items()} for name, rows in mapping.items()}
        for identity, desired in value['collections'][collection].items():
            if identity in mapping[collection]:
                if collection != 'network_monitors' or any(row['id'] == mapping[collection][identity] for row in local[collection]):
                    continue
                # Beszel create/update hooks choose a config-derived native ID.
                # Rebind a missing old ID before planning another create.

            wanted = natural_key(collection, desired, {n: {k: k for k in rows} for n, rows in value['collections'].items()})
            matches = [row['id'] for row in local[collection]
                       if wanted is not None and natural_key(collection, row, reverse) == wanted]
            if len(matches) > 1:
                raise Failure(409, 'Ambiguous existing monitoring record')
            mapping[collection][identity] = matches[0] if matches else identity
    return mapping


def plan_apply(value, previous, mapping, local):
    requests = []
    for collection in COLLECTIONS:
        actual = {row['id']: row for row in local[collection]}
        for identity, desired in value['collections'][collection].items():
            local_id = mapping[collection][identity]
            body = translate_refs(collection, {k: v for k, v in desired.items() if not k.startswith('_')}, mapping)
            if collection == 'systems':
                paused = body.pop('paused', False)
                if local_id not in actual or paused != (actual.get(local_id, {}).get('status') == 'paused'):
                    body['status'] = 'paused' if paused else 'pending'
            old_reset = (previous or {}).get('collections',{}).get(collection,{}).get(identity,{}).get('_fingerprint_reset')
            if desired.get('_fingerprint_reset') and desired['_fingerprint_reset'] != old_reset:
                body['fingerprint']=''
            old_password = (previous or {}).get('collections', {}).get(collection, {}).get(identity, {}).get('_password')
            if desired.get('_password') and (old_password != desired['_password'] or local_id not in actual):
                body['password'] = body['passwordConfirm'] = desired['_password']
            unchanged = local_id in actual and all(actual[local_id].get(k) == v for k, v in body.items())
            if unchanged:
                continue
            path = '/api/collections/' + collection + '/records'
            if local_id in actual:
                requests.append({'method': 'PATCH', 'url': path + '/' + local_id, 'body': body})
            else:
                if collection in ('users', '_superusers') and not body.get('password'):
                    raise Failure(409, 'A new monitoring account needs a password')
                requests.append({'method': 'POST', 'url': path, 'body': {'id': local_id, **body}})
    # Only records managed by our mapping are eligible for deletion.
    for collection in reversed(COLLECTIONS):
        actual_ids = {row['id'] for row in local[collection]}
        for identity, local_id in mapping[collection].items():
            if identity not in value['collections'][collection] and local_id in actual_ids:
                requests.append({'method': 'DELETE', 'url': '/api/collections/' + collection + '/records/' + local_id})
    return requests


def validate_uniques(value):
    rows=value['collections']
    for collection, key in (
        ('users',lambda row:row.get('email','').lower()),
        ('_superusers',lambda row:row.get('email','').lower()),
        ('systems',lambda row:(row.get('host'),str(row.get('port','')))),
        ('user_settings',lambda row:row.get('user')),
        ('fingerprints',lambda row:row.get('system')),
        ('universal_tokens',lambda row:row.get('user')),
        ('network_monitors',lambda row:natural_key('network_monitors',row,{'systems':{}}))):
        identities=[key(row) for row in rows[collection].values()]
        if len(identities)!=len(set(identities)):
            raise Failure(409,'Shared configuration contains a duplicate '+collection+' identity')


def mutation(value, collection, identity, method, body):
    updated = copy.deepcopy(value)
    rows = updated['collections'][collection]
    if method == 'DELETE':
        if identity not in rows:
            raise Failure(404, 'Shared monitoring record not found')
        if collection in ('users', '_superusers') and len(rows) == 1:
            raise Failure(409, 'Retain the cluster administrator')
        def remove(name,key):
            updated['collections'][name].pop(key,None)
            for child,relations in RELATIONS.items():
                for child_id,item in list(updated['collections'][child].items()):
                    for field,target in relations.items():
                        if target!=name:continue
                        if item.get(field)==key:
                            remove(child,child_id)
                            break
                        if isinstance(item.get(field),list) and key in item[field]:
                            item[field]=[ref for ref in item[field] if ref!=key]
                            if child=='systems' and field=='users' and not item[field]:
                                remove(child,child_id)
                                break
        remove(collection,identity)
        return updated
    if not isinstance(body, dict):
        raise Failure(400, 'Monitoring configuration must be an object')
    if method not in ('POST','PATCH','DELETE'):
        raise Failure(405, 'Unsupported configuration mutation method')
    body = {k:v for k,v in body.items() if k not in ('id','created','updated')}
    if collection == 'systems':
        # Native Add/Edit System submits these read-only installation fields.
        # Beszel ignores them here; its next request saves the fingerprint token.
        body = {k:v for k,v in body.items() if k not in ('pkey', 'tkn')}
        # The form sends one user ID; PocketBase reads a multi-relation as a list.
        if isinstance(body.get('users'), str):
            body['users'] = [body['users']] if body['users'] else []
    if collection=='fingerprints' and 'fingerprint' in body:
        if body.pop('fingerprint') != '': raise Failure(400,'Fingerprint values are native runtime data')
        body['_fingerprint_reset']=secrets.token_hex(16)
    allowed = FIELDS[collection] | ({'_fingerprint_reset'} if collection=='fingerprints' else set()) | ({'password', 'passwordConfirm', 'oldPassword'} if collection in ('users', '_superusers') else set())
    if collection == 'systems':
        allowed |= {'status'}
    if set(body) - allowed:
        raise Failure(400, 'Unsupported shared configuration fields')
    body = copy.deepcopy(body)
    if 'password' in body and not body['password']:
        body.pop('password'); body.pop('passwordConfirm', None)
    if 'password' in body:
        if body['password'] != body.get('passwordConfirm') or not isinstance(body['password'], str) or not 8 <= len(body['password'].encode()) <= 72:
            raise Failure(400, 'Password confirmation is invalid')
        body['_password'] = body.pop('password')
        body.pop('passwordConfirm', None)
    body.pop('oldPassword', None)
    if 'email' in body and (not isinstance(body['email'], str) or not re.fullmatch(r'[^\s@]+@[^\s@]+', body['email'])):
        raise Failure(400, 'Invalid monitoring email')
    if 'status' in body:
        if body['status'] not in ('up', 'down', 'pending', 'paused'):
            raise Failure(400, 'Invalid monitoring status')
        body['paused'] = body.pop('status') == 'paused'
    if method == 'PATCH' and identity not in rows:
        raise Failure(404, 'Shared monitoring record not found')
    if method == 'POST' and identity in rows:
        raise Failure(409, 'Shared monitoring record already exists')
    rows[identity] = {**rows.get(identity, {}), **body}
    return updated


class Cluster:
    def __init__(self, config, journal, hub):
        self.config, self.journal, self.hub = config, journal, hub
        self.key = base64.urlsafe_b64decode(config['key'])
        self.lock, self.apply_lock = threading.Lock(), threading.Lock()
        self.pool = ThreadPoolExecutor(max_workers=6)
        self.heads, self.heads_at, self.last_error = {}, 0, None
        self.last_error_code = None
        self.hub_ready = False
        self.context = ssl.create_default_context(cafile=config['certificate'])
        class NoRedirect(urllib.request.HTTPRedirectHandler):
            def redirect_request(self, *args, **kwargs): return None
        self.opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), NoRedirect(), urllib.request.HTTPSHandler(context=self.context))

    def call(self, node, action, payload=None):
        if node == self.config['node']:
            return self.peer(action, payload or {})
        path = '/peer/' + action
        raw = encoded(payload or {})
        stamp, nonce = str(int(time.time())), secrets.token_hex(16)
        signed = ('POST\n' + path + '\n' + stamp + '\n' + nonce + '\n' + hashlib.sha256(raw).hexdigest()).encode()
        headers = {'Content-Type': 'application/json', 'X-Desk-Time': stamp, 'X-Desk-Nonce': nonce,
                   'X-Desk-Signature': hmac.new(self.key, signed, hashlib.sha256).hexdigest()}
        request = urllib.request.Request('https://' + self.config['members'][node] + ':' + str(self.config['peer_port']) + path,
                                         data=raw, headers=headers)
        with self.opener.open(request, timeout=60 if action=='commit' else 5) as response:
            data = response.read(LIMIT + 1)
            if len(data) > LIMIT:
                raise Failure(502, 'Peer response exceeds configuration boundary')
            return json.loads(data)

    def fanout(self, action, payload=None):
        jobs = {node: self.pool.submit(self.call, node, action, payload) for node in self.config['members']}
        result = {}
        for node, future in jobs.items():
            try:
                result[node] = future.result()
            except Exception:
                pass
        return result

    def status(self, full=False):
        result = self.journal.status(full)
        result['apply_error'] = self.last_error_code
        result['partial_apply'] = (self.journal.state.get('record_applied_revision', -1) > self.journal.state['applied_revision'] and self.journal.state.get('native_record_changes', False))
        return result

    def peer(self, action, payload):
        if action == 'status':
            return self.status(True)
        if action == 'promise':
            return self.journal.promise(payload['revision'], payload['ballot'])
        if action == 'accept':
            return self.journal.accept(payload['revision'], payload['ballot'], payload['value'])
        if action == 'commit':
            self.journal.commit(payload['revision'], payload['value'], payload['certificate'])
            try:
                self.apply()
            except Exception as error:
                self.last_error_code = error.code if isinstance(error, Failure) else 503
                self.last_error = 'Native Beszel configuration apply failed'
                LOG.warning('Configuration apply failed; revision=%s', payload['revision'])
            return self.status()
        raise Failure(404, 'Unknown monitoring peer action')

    def apply(self):
        # Peer status and UI health must not wait for native API or password hashing.
        with self.apply_lock:
            with self.journal.lock:
                state = self.journal.state
                revision = state['revision']
                if revision < 0 or state['applied_revision'] == revision: return
                value = copy.deepcopy(state['value'])
                mapping = copy.deepcopy(state['mapping'])
                previous = copy.deepcopy(state.get('record_applied_value', state['applied_value']))
                previous_resets = copy.deepcopy(state.get('token_resets_applied', {}))
            changed_resets = False
            resets = value.get('token_resets', {})
            for user, generation in resets.items():
                if generation != previous_resets.get(user):
                    local_user = mapping['users'].get(user)
                    if local_user:
                        token = jwt_from_local_record(self.hub.database, 'users', local_user)
                        self.hub.request('/api/beszel/universal-token?enable=0', token=token)
                        changed_resets = True
            local = {name: self.hub.records(name) for name in COLLECTIONS}
            changed = 0
            # Native hooks can initialize defaults or rename records. Re-read and
            # apply the final projected configuration, rather than just trusting 200.
            for attempt in range(4):
                mapping = match_mapping(value, local, mapping)
                requests = plan_apply(value, previous, mapping, local)
                if not requests: break
                if attempt == 3: raise Failure(503, 'Native records have not converged')
                self.hub.request('/api/batch', 'POST', {'requests': requests})
                changed += len(requests)
                previous = value
                local = {name: self.hub.records(name) for name in COLLECTIONS}
                mapping = match_mapping(value, local, mapping)
                with self.journal.lock:
                    self.journal.state.update(mapping=copy.deepcopy(mapping),record_applied_revision=revision,
                        record_applied_value=copy.deepcopy(value),token_resets_applied=copy.deepcopy(resets),
                        native_record_changes=True)
                    self.journal.save()
            else:
                raise Failure(503, 'Native records have not converged to committed settings')
            mapping={name:{identity:local_id for identity,local_id in rows.items() if identity in value['collections'][name]} for name,rows in mapping.items()}
            # Persist native-record progress before the separate settings API call.
            # A failed settings call is explicit partial apply, never a false rollback.
            with self.journal.lock:
                self.journal.state.update(mapping=mapping, record_applied_revision=revision,
                    record_applied_value=copy.deepcopy(value), token_resets_applied=copy.deepcopy(resets),
                    native_record_changes=bool(changed or changed_resets or (state.get('native_record_changes') and state.get('record_applied_revision') == revision)))
                self.journal.save()
            settings = self.hub.full_settings()
            if settings != value['settings']:
                self.hub.request('/api/settings', 'PATCH', value['settings'])
            if self.hub.full_settings() != value['settings']:
                raise Failure(503, 'Native application settings have not converged')
            with self.journal.lock:
                state = self.journal.state
                state.update(applied_revision=revision, applied_value=copy.deepcopy(value), native_record_changes=False)
                self.journal.save()
            self.last_error = self.last_error_code = None
            LOG.info('Configuration applied; revision=%s changed_records=%s', revision, changed)

    def refresh(self):
        self.hub_ready = self.hub.health()
        heads = self.fanout('status')
        valid = [head for head in heads.values() if head['revision'] >= 0]
        if valid:
            head = max(valid, key=lambda h: h['revision'])
            if head['revision'] > self.journal.state['revision']:
                self.journal.commit(head['revision'], head['value'], head['certificate'])
            try:
                self.apply()
            except Exception as error:
                self.last_error_code = error.code if isinstance(error, Failure) else 503
                self.last_error = 'Native Beszel configuration apply failed'
        self.heads, self.heads_at = heads, time.monotonic()
        return heads

    def choose(self, wanted, expected_revision=None):
        """Recover an already accepted proposal before submitting a different one."""
        for attempt in range(6):
            heads = self.refresh()
            if len(heads) < 2:
                raise Failure(503, 'Two monitoring nodes are required to save configuration')
            state = self.journal.state
            if expected_revision is not None and state['revision'] != expected_revision:
                return False, state['revision'], self.ready_nodes()
            revision = state['revision'] + 1
            counter = max(head['promised'][0] for head in heads.values()) + 1
            ballot = [counter, self.config['node']]
            promises = self.fanout('promise', {'revision': revision, 'ballot': ballot})
            if len(promises) < 2 or any('head' in p for p in promises.values()):
                continue
            accepted = [p['accepted'] for p in promises.values() if p.get('accepted')]
            chosen = max(accepted, key=lambda p: p['ballot'])['value'] if accepted else wanted
            receipts = self.fanout('accept', {'revision': revision, 'ballot': ballot, 'value': chosen})
            if len(receipts) < 2:
                continue
            result = self.fanout('commit', {'revision': revision, 'value': chosen, 'certificate': list(receipts.values())})
            self.heads, self.heads_at = result, time.monotonic()
            applied = [node for node, head in result.items() if head['applied_revision'] == revision]
            if digest(chosen) != digest(wanted):
                # The caller must recompute its mutation against the recovered value.
                return False, revision, applied
            return True, revision, applied
        raise Failure(503, 'Monitoring configuration changed concurrently; retry save')

    def submit(self, intent):
        with self.lock:
            for attempt in range(6):
                self.refresh()
                base_revision = self.journal.state['revision']
                old = copy.deepcopy(self.journal.state['value'])
                wanted = self.desired(old, intent)
                if digest(old) == digest(wanted):
                    ready=self.ready_nodes()
                    if len(ready)<2 or self.config['node'] not in ready:
                        raise Failure(503,'Committed configuration is still awaiting two native apply acknowledgments')
                    return self.journal.state['revision'], ready
                complete, revision, applied = self.choose(wanted, base_revision)
                if not complete:
                    continue
                if len(applied) < 2 or self.config['node'] not in applied:
                    errors = [head.get('apply_error') for head in self.heads.values()]
                    if not applied and errors and all(code == 400 for code in errors) and not any(head.get('partial_apply') for head in self.heads.values()):
                        # Every reachable native API rejected before applying its
                        # transaction. Revert only if nobody committed a newer edit.
                        reverted, _, _ = self.choose(old, revision)
                        if reverted:
                            raise Failure(400, 'Beszel rejected this configuration; previous settings retained')
                    # An acknowledged desired state survives a temporary replica
                    # failure. Never roll it back over a concurrent valid update.
                    raise Failure(503, 'Configuration committed but awaiting native apply; retry after recovery')
                LOG.info('Configuration committed; revision=%s coverage=%s/3', revision, len(applied))
                return revision, applied
            raise Failure(503, 'Monitoring configuration busy; retry save')

    def desired(self, value, intent):
        value = copy.deepcopy(value)
        value['settings']['batch']['enabled'] = True
        value['settings']['batch']['timeout'] = max(BATCH_TIMEOUT, value['settings']['batch'].get('timeout', 0))
        collection, method, identity, body = (intent.get(k) for k in ('collection', 'method', 'identity', 'body'))
        if collection == 'batch':
            updated=copy.deepcopy(value)
            for child in body: updated=self.desired(updated,child)
            return updated
        if collection == 'universal-tokens':
            updated=copy.deepcopy(value); rows=updated['collections']['universal_tokens']
            existing=[key for key,row in rows.items() if row['user']==identity]
            updated.setdefault('token_resets', {})[identity]=body['reset']
            if body['enable']=='0' or body.get('permanent')!='1':
                for key in existing: del rows[key]
            else:
                key=existing[0] if existing else body['id']
                rows[key]={'user':identity,'token':body['token']}
            return updated
        if collection == 'settings':
            if method != 'PATCH' or not isinstance(body, dict):
                raise Failure(405, 'Shared application settings require PATCH JSON')
            updated = copy.deepcopy(value)
            def merge(left, right):
                for k, v in right.items():
                    if isinstance(v, dict) and isinstance(left.get(k), dict):
                        merge(left[k], v)
                    else:
                        left[k] = copy.deepcopy(v)
            merge(updated['settings'], body)
            updated['settings']['batch']['enabled'] = True
            updated['settings']['batch']['timeout'] = max(BATCH_TIMEOUT, updated['settings']['batch'].get('timeout', 0))
            return updated
        if collection == 'user-alerts':
            updated = copy.deepcopy(value)
            rows = updated['collections']['alerts']
            for system in body['systems']:
                matches = [key for key, row in rows.items() if row['user'] == identity and row['system'] == system and row['name'] == body['name']]
                if method == 'DELETE':
                    for key in matches:
                        del rows[key]
                elif not matches or body.get('overwrite'):
                    key = matches[0] if matches else secrets.token_hex(8)[:15]
                    rows[key] = {'user': identity, 'system': system, 'name': body['name'],
                                 'min': body.get('min', 0), 'value': body['value']}
            return updated
        updated = mutation(value, collection, identity, method, body)
        if collection == 'network_monitors' and method != 'DELETE':
            row = updated['collections'][collection][identity]
            if row.get('protocol') != 'tcp': row['port'] = 0
            if row.get('protocol') != 'dns': row['server'] = ''
        if collection == '_superusers' and not any(row['email']==self.config.get('admin_email','admin@desk.cluster') for row in updated['collections']['_superusers'].values()):
            raise Failure(409,'Retain the Ansible-managed PocketBase administrator')
        if collection == 'users':
            admins = [row for row in updated['collections']['users'].values() if row.get('role') == 'admin']
            if not admins or not any(row['email'] == self.config.get('admin_email', 'admin@desk.cluster') for row in admins):
                raise Failure(409, 'Retain the Ansible-managed auto-login administrator email')
        updated['token_resets']={user:gen for user,gen in updated.get('token_resets',{}).items() if user in updated['collections']['users']}
        validate_uniques(updated)
        return updated

    def ready_nodes(self):
        state = self.journal.state
        return [node for node, head in self.heads.items() if head['revision'] == state['revision']
                and head['applied_revision'] == state['revision'] and head['digest'] == digest(state['value'])]

    def healthy(self):
        # UI liveness is independent of write quorum or a pending configuration.
        return self.hub.health()

    def sync_status(self):
        state = self.journal.status()
        ready = self.ready_nodes()
        return {'revision':state['revision'], 'applied_revision':state['applied_revision'],
            'ready_nodes':ready, 'writable':len(self.heads)>=2,
            'state':'synced' if len(ready)==len(self.config['members']) else 'pending',
            'apply_error':self.last_error_code}

    def intent(self, path, method, body, auth, actor=None):
        # Native token refresh validates the caller's original identity and role.
        if actor is None:
            for collection in ('users', '_superusers'):
                try:
                    result = self.hub.request('/api/collections/' + collection + '/auth-refresh', 'POST', {}, token=auth or ' ')
                    actor = result['record']
                    break
                except Failure:
                    pass
        if actor is None:
            raise Failure(403, 'Monitoring authentication required')
        superuser = actor['collectionName'] == '_superusers'
        admin = superuser or actor.get('role') == 'admin'
        mapping = self.journal.state['mapping']
        reverse = {name: {v: k for k, v in rows.items()} for name, rows in mapping.items()}
        if path == '/api/batch':
            if method!='POST' or not isinstance(body,dict) or set(body)!={'requests'} or not isinstance(body['requests'],list) or not 1<=len(body['requests'])<=100:
                raise Failure(400,'Invalid shared batch')
            children=[]
            for request in body['requests']:
                route=urllib.parse.urlsplit(request['url']).path
                if not RECORD_ROUTE.fullmatch(route):raise Failure(405,'Only shared records may use the batch boundary')
                children.append(self.intent(route,request['method'],request.get('body',{}),auth,actor=actor))
            return {'collection':'batch','method':'POST','body':children}
        if path == '/api/beszel/universal-token':
            if superuser or body.get('enable') not in ('0','1'):
                raise Failure(400,'Invalid token operation')
            import uuid
            body=copy.deepcopy(body); body['token']=body.get('token') or str(uuid.uuid4()); body['id']=record_identity('universal_tokens'); body['reset']=secrets.token_hex(16)
            return {'collection':'universal-tokens','method':'SET','identity':reverse['users'][actor['id']],'body':body}
        if path == '/api/settings':
            if not superuser:
                raise Failure(403, 'PocketBase administrator required')
            return {'collection': 'settings', 'method': method, 'body': body}
        if path == '/api/beszel/user-alerts':
            if superuser or set(body) - {'name', 'systems', 'min', 'value', 'overwrite'}:
                raise Failure(400, 'Invalid monitoring alert update')
            body = copy.deepcopy(body)
            body['systems'] = [reverse['systems'][x] for x in body['systems']]
            return {'collection': 'user-alerts', 'method': method, 'identity': reverse['users'][actor['id']], 'body': body}
        match = RECORD_ROUTE.fullmatch(path)
        if not match:
            raise Failure(405, 'This mutation is not a shared monitoring setting')
        collection, local_id = match.groups()
        if collection not in COLLECTIONS:collection=self.hub.collection_name(collection)
        if not admin and collection not in ('user_settings', 'quiet_hours', 'alerts'):
            raise Failure(403, 'Monitoring administrator required')
        if collection == '_superusers' and not superuser:
            raise Failure(403, 'PocketBase administrator required for account management')
        if collection == 'users' and not superuser:
            if local_id != actor['id'] or set(body or {}) - {'name','username','password','passwordConfirm','oldPassword','emailVisibility'}:
                raise Failure(403, 'PocketBase administrator required for account management')
            if body and body.get('password'):
                # Self-service password changes must prove the current password.
                if not body.get('oldPassword'):
                    raise Failure(400, 'Current password required for self-service changes')
                self.hub.request('/api/collections/users/auth-with-password', 'POST',
                    {'identity': actor['email'], 'password': body['oldPassword']}, token=auth or ' ')
        specified=(body or {}).get('id')
        if specified and (method!='POST' or not re.fullmatch('[a-z0-9]{6,15}',specified)):
            if specified!=local_id:raise Failure(400,'Invalid monitoring record ID')
        identity = reverse[collection].get(local_id) if local_id else specified or record_identity(collection)
        if local_id and identity is None:
            raise Failure(409, 'Refresh the page after hub failover before editing')
        body = translate_refs(collection, body, reverse) if body else {}
        if not superuser and collection in ('user_settings', 'quiet_hours', 'alerts'):
            user = reverse['users'][actor['id']]
            row = self.journal.state['value']['collections'][collection].get(identity, {})
            if body.get('user', row.get('user')) != user:
                raise Failure(403, 'Only the current user may change these settings')
        return {'collection': collection, 'method': method, 'identity': identity, 'body': body}


class Server(socketserver.ThreadingMixIn, http.server.HTTPServer):
    daemon_threads = True
    allow_reuse_address = True
    def __init__(self, address, handler, cluster, peer=False):
        self.cluster, self.peer = cluster, peer
        self.tls = None
        self.slots = threading.BoundedSemaphore(8)
        self.nonces, self.nonce_lock = {}, threading.Lock()
        super().__init__(address, handler)

    def handle_error(self, request, address):
        error=sys.exc_info()[1]
        if isinstance(error,(ConnectionResetError,BrokenPipeError,TimeoutError,ssl.SSLEOFError)):
            return
        LOG.warning('Monitoring transport error; code=%s',type(error).__name__)

    def get_request(self):
        request,address=super().get_request()
        request.settimeout(3)
        if self.peer:
            if address[0] not in self.cluster.config['members'].values():
                request.close()
                raise OSError('Peer not allowed')
            try:
                request=self.tls.wrap_socket(request,server_side=True)
            except Exception:
                request.close()
                raise
        return request,address

    def process_request(self, request, address):
        if not self.slots.acquire(False):
            self.shutdown_request(request)
            return
        try:
            super().process_request(request, address)
        except Exception:
            self.slots.release()
            raise

    def process_request_thread(self, request, address):
        try:
            super().process_request_thread(request, address)
        finally:
            self.slots.release()


class Handler(http.server.BaseHTTPRequestHandler):
    protocol_version = 'HTTP/1.1'
    def log_message(self, *args):
        pass  # Never log mutation URLs, payloads, passwords, or notification URLs.

    def setup(self):
        super().setup()
        self.connection.settimeout(12)

    def send(self, code, value, extra=None):
        raw = encoded(value)
        self.send_response(code)
        self.send_header('Content-Type', 'application/json')
        self.send_header('Content-Length', str(len(raw)))
        self.send_header('Cache-Control', 'no-store')
        for key, val in (extra or {}).items():
            self.send_header(key, str(val))
        self.end_headers()
        self.wfile.write(raw)

    def body(self):
        if self.headers.get('Transfer-Encoding'):
            raise Failure(400, 'Chunked configuration uploads are not supported')
        length = int(self.headers.get('Content-Length', '0'))
        if length < 0 or length > LIMIT:
            raise Failure(413, 'Monitoring configuration exceeds boundary')
        raw = self.rfile.read(length)
        if length and len(raw) != length:
            raise Failure(400, 'Incomplete monitoring configuration')
        if raw and 'application/json' not in self.headers.get('Content-Type', ''):
            raise Failure(415, 'Shared settings require JSON; file uploads are not replicated')
        return raw, json.loads(raw) if raw else {}

    def authenticate_peer(self, raw):
        stamp, nonce = self.headers.get('X-Desk-Time', ''), self.headers.get('X-Desk-Nonce', '')
        if not stamp.isdigit() or abs(time.time() - int(stamp)) > 30 or not re.fullmatch('[a-f0-9]{32}', nonce):
            raise Failure(403, 'Invalid monitoring peer envelope')
        signed = ('POST\n' + self.path + '\n' + stamp + '\n' + nonce + '\n' + hashlib.sha256(raw).hexdigest()).encode()
        expected = hmac.new(self.server.cluster.key, signed, hashlib.sha256).hexdigest()
        if not hmac.compare_digest(expected, self.headers.get('X-Desk-Signature', '')):
            raise Failure(403, 'Monitoring peer authentication failed')
        with self.server.nonce_lock:
            self.server.nonces = {k: v for k, v in self.server.nonces.items() if time.time() - v < 35}
            if nonce in self.server.nonces or len(self.server.nonces) >= 1024:
                raise Failure(403, 'Monitoring peer envelope replayed')
            self.server.nonces[nonce] = time.time()

    def handle_request(self):
        cluster = self.server.cluster
        try:
            if self.client_address[0] not in set(cluster.config['members'].values()) | {'127.0.0.1'}:
                raise Failure(403, 'Only existing cluster proxies and peers may access this boundary')
            path = urllib.parse.urlsplit(self.path).path
            if self.server.peer:
                if self.command != 'POST' or not path.startswith('/peer/'):
                    raise Failure(404, 'Unknown monitoring peer path')
                raw, body = self.body()
                self.authenticate_peer(raw)
                self.send(200, cluster.peer(path.removeprefix('/peer/'), body))
                return
            token_write = path=='/api/beszel/universal-token' and 'enable' in urllib.parse.parse_qs(urllib.parse.urlsplit(self.path).query)
            if self.command == 'GET' and not token_write:
                if path == '/health':
                    self.send(200 if cluster.healthy() else 503, {'ready': cluster.healthy()})
                elif path == '/api/desk-config/status':
                    self.send(200, cluster.sync_status())
                else:
                    raise Failure(404, 'Unknown monitoring configuration path')
                return
            _, body = self.body()
            if token_write: body={key:values[-1] for key,values in urllib.parse.parse_qs(urllib.parse.urlsplit(self.path).query).items()}
            # Auth, realtime registration and one-shot tests stay local and are not journaled.
            ephemeral = (path.startswith('/api/settings/test/') or path == '/api/realtime' or path in ('/api/beszel/test-notification', '/api/beszel/test-heartbeat',
                         '/api/beszel/smart/refresh', '/api/beszel/zfs/refresh') or
                         re.fullmatch(r'/api/collections/(users|_superusers)/(auth-refresh|auth-with-password|auth-methods)', path))
            if ephemeral:
                request = urllib.request.Request(cluster.hub.url + self.path, data=encoded(body), method=self.command,
                                                 headers={'Content-Type': 'application/json', 'Authorization': self.headers.get('Authorization', '')})
                try:
                    with cluster.hub.opener.open(request, timeout=10) as response:
                        raw = response.read(LIMIT + 1)
                        self.send(response.status, json.loads(raw) if raw else {})
                except urllib.error.HTTPError as error:
                    error.close()
                    raise Failure(error.code, 'Monitoring request rejected') from None
                return
            intent = cluster.intent(path, self.command, body, self.headers.get('Authorization'))
            revision, ready = cluster.submit(intent)
            identity = intent.get('identity')
            if intent['collection'] in COLLECTIONS and self.command != 'DELETE':
                local_id = cluster.journal.state['mapping'][intent['collection']][identity]
                result = cluster.hub.request('/api/collections/' + intent['collection'] + '/records/' + local_id)
            elif intent['collection'] == 'settings':
                result = cluster.hub.request('/api/settings')
            elif intent['collection']=='batch':
                result=[]
                for child in intent['body']:
                    if child['method']=='DELETE':result.append({'status':204,'body':None});continue
                    local_id=cluster.journal.state['mapping'][child['collection']][child['identity']]
                    row=cluster.hub.request('/api/collections/'+child['collection']+'/records/'+local_id)
                    result.append({'status':201 if child['method']=='POST' else 200,'body':row})
            elif intent['collection']=='universal-tokens':
                if intent['body']['enable']=='1' and intent['body'].get('permanent')!='1':
                    token = jwt_from_local_record(cluster.hub.database, 'users', cluster.journal.state['mapping']['users'][intent['identity']])
                    result = cluster.hub.request('/api/beszel/universal-token?enable=1&token='+intent['body']['token'], token=token)
                else:
                    result={'token':intent['body']['token'],'active':intent['body']['enable']=='1','permanent':intent['body']['enable']=='1'}
            else:
                result = {'success': True}
            self.send(200, result, {'X-Desk-Config-Revision': revision, 'X-Desk-Config-Coverage': str(len(ready)) + '/3'})
        except Failure as error:
            self.close_connection = True
            self.send(error.code, {'code': error.code, 'message': error.message, 'data': {}})
        except Exception:
            self.close_connection = True
            LOG.warning('Monitoring config request failed; method=%s', self.command)
            self.send(503, {'code': 503, 'message': 'Monitoring configuration unavailable; no success acknowledged', 'data': {}})

    do_GET = do_POST = do_PATCH = do_PUT = do_DELETE = handle_request


def run(config_path):
    config = json.loads(config_path.read_text())
    members = config['members']
    if set(members) != {'deskb1x', 'deskb2x', 'deskb3x'} or len(set(members.values())) != 3:
        raise ValueError('Invalid monitoring membership')
    key = base64.urlsafe_b64decode(config['key'])
    journal = Journal(Path(config['journal']), Fernet(config['key'].encode()), config['node'], members, key)
    hub = Hub('http://' + members[config['node']] + ':8090', Path('/var/lib/beszel-hub/data.db'),config['admin_email'])
    cluster = Cluster(config, journal, hub)
    seed = Path(config['seed'])
    if seed.exists():
        journal.initialize(json.loads(Fernet(config['key'].encode()).decrypt(seed.read_bytes()))['value'])
    peer = Server((members[config['node']], config['peer_port']), Handler, cluster, peer=True)
    tls = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    tls.load_cert_chain(config['certificate'], config['private_key'])
    peer.tls = tls
    threading.Thread(target=peer.serve_forever, daemon=True).start()
    def reconcile():
        while True:
            try:
                cluster.refresh()
            except Exception:
                LOG.warning('Monitoring reconciliation unavailable; retaining committed configuration')
            time.sleep(5)
    threading.Thread(target=reconcile, daemon=True).start()
    public = Server((members[config['node']], config['port']), Handler, cluster)
    public.serve_forever()


if __name__ == '__main__':
    logging.basicConfig(level=logging.INFO, format='%(message)s')
    parser = argparse.ArgumentParser()
    parser.add_argument('--config', type=Path, default=Path('/etc/desk-monitoring/config-sync.json'))
    args = parser.parse_args()
    run(args.config)
