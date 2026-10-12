#!/usr/bin/env python3
"""Small AdGuard API boundary around upstream adguardhome-sync, not a DNS proxy."""
import argparse
import base64
import hashlib
import hmac
import http.cookies
import http.server
import json
import importlib.util
from contextlib import contextmanager
import os
from pathlib import Path
import ssl
import subprocess
import threading
import time
import urllib.error
import urllib.request

LIMIT = 2 * 1024 * 1024
RESERVED = {'health.desk-dns.test', 'who.desk-dns.test'}
RULE = '||blocked.desk-dns.test^'
READS = {'status', 'profile', 'parental/status', 'safesearch/status',
         'safebrowsing/status', 'rewrite/settings', 'rewrite/list',
         'blocked_services/get', 'filtering/status', 'clients',
         'querylog/config', 'stats/config', 'querylog_info', 'stats_info', 'access/list', 'dns_info'}
WRITES = {'querylog/config/update': 'PUT', 'stats/config/update': 'PUT',
          'querylog_config': 'POST', 'stats_config': 'POST',
          'profile/update': 'PUT', 'dns_config': 'POST', 'protection': 'POST',
          'parental/enable': 'POST', 'parental/disable': 'POST',
          'safebrowsing/enable': 'POST', 'safebrowsing/disable': 'POST',
          'safesearch/settings': 'PUT', 'rewrite/settings/update': 'PUT',
          'rewrite/add': 'POST', 'rewrite/delete': 'POST', 'rewrite/update': 'PUT',
          'filtering/config': 'POST', 'filtering/add_url': 'POST',
          'filtering/remove_url': 'POST', 'filtering/set_url': 'POST',
          'filtering/set_rules': 'POST', 'filtering/refresh': 'POST',
          'blocked_services/update': 'PUT', 'clients/add': 'POST',
          'clients/update': 'POST', 'clients/delete': 'POST'}
DNS_FIELDS = {'bootstrap_dns', 'upstream_dns', 'fallback_dns', 'protection_enabled',
              'ratelimit', 'ratelimit_subnet_len_ipv4', 'ratelimit_subnet_len_ipv6',
              'ratelimit_whitelist', 'blocking_mode', 'blocking_ipv4', 'blocking_ipv6',
              'blocked_response_ttl', 'edns_cs_enabled', 'edns_cs_use_custom',
              'edns_cs_custom_ip', 'disable_ipv6', 'dnssec_enabled', 'cache_size',
              'cache_ttl_min', 'cache_ttl_max', 'cache_enabled', 'cache_optimistic',
              'upstream_mode', 'use_private_ptr_resolvers', 'resolve_clients',
              'local_ptr_upstreams', 'upstream_timeout'}
CLIENT_FIELDS = {'name', 'ids', 'use_global_settings', 'filtering_enabled',
                 'parental_enabled', 'safebrowsing_enabled', 'safesearch_enabled',
                 'safe_search', 'use_global_blocked_services',
                 'blocked_services_schedule', 'blocked_services', 'upstreams',
                 'tags', 'ignore_querylog', 'ignore_statistics',
                 'upstreams_cache_enabled', 'upstreams_cache_size'}
_SESSION_LOCK = threading.Lock()
_SESSIONS = {}
_FILTER_JOB = None
_FILTER_JOB_LOCK = threading.Lock()


def encoded(value):
    return json.dumps(value, sort_keys=True, separators=(',', ':')).encode()


def owns(vip):
    addresses = json.loads(subprocess.check_output(
        ['/usr/sbin/ip', '-j', '-4', 'address', 'show'], timeout=2))
    return any(a.get('local') == vip for interface in addresses
               for a in interface.get('addr_info', []))


def basic(config):
    return 'Basic ' + base64.b64encode(
        (config['username'] + ':' + config['password']).encode()).decode()


def project(route, value):
    """Exclude host-local state and runtime counters from replicated policy."""
    if route == 'status': return {'protection_enabled':value['protection_enabled']}
    if route == 'profile': return {k:value[k] for k in ('language','theme') if k in value}
    if route in ('querylog/config', 'stats/config'):
        return {**value, 'ignored': sorted(value.get('ignored') or [])}
    if route == 'rewrite/list':
        return [x for x in (value or []) if x['domain'].lower().rstrip('.') not in RESERVED]
    if route == 'rewrite/settings':
        # Internal identity/health rewrites must remain enabled on every backend.
        return {'enabled': True}
    if route == 'dns_info':
        if value.get('upstream_dns_file'):
            raise ValueError('External upstream files are not a portable policy')
        return {k: v for k, v in value.items() if k in DNS_FIELDS}
    if route == 'clients':
        return {'clients': [{k: v for k, v in x.items() if k in CLIENT_FIELDS}
                            for x in (value.get('clients') or [])]}
    if route == 'filtering/status':
        return {k: value[k] for k in ('enabled', 'interval') if k in value} | {
            'filters': [{k: x[k] for k in ('url', 'name', 'enabled')} for x in (value.get('filters') or [])],
            'whitelist_filters': [{k: x[k] for k in ('url', 'name', 'enabled')}
                                  for x in (value.get('whitelist_filters') or [])],
            'user_rules': [RULE] + [r for r in (value.get('user_rules') or []) if r != RULE]}
    return value


def validate_write(route, value, serialized=False):
    if route in ('querylog/config/update', 'querylog_config', 'stats/config/update', 'stats_config'):
        allowed = {'enabled', 'interval', 'ignored', 'ignored_enabled', 'anonymize_client_ip'}
        if not isinstance(value, dict) or set(value) - allowed: raise ValueError('Only shared observation settings allowed')
    if route == 'dns_config' and set(value) - DNS_FIELDS:
        raise ValueError('Host-local DNS configuration cannot be synchronized')
    if route.startswith('rewrite/'):
        entries = [value] if route != 'rewrite/update' else [value['target'], value['update']]
        if any(x.get('domain', '').lower().rstrip('.') in RESERVED for x in entries):
            raise ValueError('Reserved rewrite')
        if route == 'rewrite/settings/update' and value != {'enabled': True}:
            raise ValueError('Internal rewrites must remain enabled')
    if route == 'filtering/config' and serialized:
        value['interval'] = 0  # All refreshes use the serialized cluster job.
    if route == 'filtering/set_rules':
        value['rules'] = [RULE] + [r for r in value['rules'] if r != RULE]
    if route == 'profile/update':
        if set(value) - {'name', 'language', 'theme'} or value.get('name','') not in ('desk-sync', ''):
            raise ValueError('Account changes are not policy')
    return value


def fetch(url, config, method='GET', value=None, context=None, return_mutation=False, timeout=25):
    # No redirects, credential forwarding to arbitrary URLs, or environment proxies.
    class NoRedirect(urllib.request.HTTPRedirectHandler):
        def redirect_request(self, *args, **kwargs):
            return None
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), NoRedirect(),
                                        urllib.request.HTTPSHandler(context=context))
    local = url.startswith(config.get('adguard', '') + '/control/')
    local_auth = config.get('local_auth', True)
    if local and not local_auth and url.endswith('/control/profile/update') and isinstance(value, dict):
        value = {k: v for k, v in value.items() if k != 'name'}
    key = (config.get('adguard'), config['username'], config['password'])
    for attempt in range(2):
        headers = {'Content-Type': 'application/json'}
        if local and local_auth:
            # AdGuard checks bcrypt on every Basic request. Authenticate once, then
            # reuse its own session cookie in memory; this is outside DNS forwarding.
            with _SESSION_LOCK:
                if key not in _SESSIONS:
                    login = urllib.request.Request(config['adguard'] + '/control/login',
                                                   data=encoded({'name': config['username'],
                                                                 'password': config['password']}),
                                                   headers=headers, method='POST')
                    with opener.open(login, timeout=20) as response:
                        cookies = http.cookies.SimpleCookie()
                        cookies.load(response.headers.get('Set-Cookie', ''))
                        token = cookies.get('agh_session')
                        if not token:
                            raise ValueError('Login did not create a session')
                        _SESSIONS[key] = token.value
                headers['Cookie'] = 'agh_session=' + _SESSIONS[key]
        elif not local:
            # The protected boundary verifies this shared token cheaply, without bcrypt.
            headers['Authorization'] = basic(config)
        request = urllib.request.Request(url, data=None if value is None else encoded(value),
                                         method=method, headers=headers)
        try:
            with opener.open(request, timeout=timeout) as response:
                data = response.read(LIMIT + 1)
                if len(data) > LIMIT:
                    raise ValueError('Response exceeds policy boundary')
                if not data.strip() or (method != 'GET' and not return_mutation):
                    return None
                return json.loads(data)
        except urllib.error.HTTPError as error:
            # A revoked/expired session is refreshed once; never retry a mutation
            # that received any response other than authentication rejection.
            if local and local_auth and error.code in (401, 403) and attempt == 0:
                error.close()
                with _SESSION_LOCK:
                    _SESSIONS.pop(key, None)
                continue
            raise


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None


class MaintenanceBusy(Exception):
    def __init__(self, reason="filter_maintenance_busy", retry_after=5):
        self.reason, self.retry_after = reason, max(1, int(retry_after))
        super().__init__(reason)


class Maintenance:
    """One cluster maintenance lease, held by the stable frontend VIP owner.

    A new owner waits longer than any previous lease before accepting work.
    Leases expire after killed clients; a failed/uncertain mutation never releases
    early. No database or continuous disk writes are used.
    """
    ttl = 180

    def __init__(self, config, clock=time.monotonic, warmup=180):
        self.config, self.clock, self.warmup = config, clock, warmup
        self.lock = threading.Lock()
        self.owner = False
        self.not_before = 0
        self.active = None
        self.local_token = None

    def observe(self, owner):
        with self.lock:
            if owner and not self.owner:
                self.not_before = self.clock() + self.warmup
            self.owner = owner

    def acquire(self, node):
        import secrets
        with self.lock:
            now = self.clock()
            if (not self.owner or now < self.not_before
                    or (self.active and self.active['expires'] > now)):
                raise MaintenanceBusy('filter_update_or_owner_transition', max(self.not_before-now, (self.active or {}).get('expires',now)-now))
            if node not in self.config['nodes']:
                raise ValueError('Unknown maintenance node')
            if other_filter_job(self.config):
                raise MaintenanceBusy('native_filter_job_still_running')
            if not other_backend_ready(node):
                raise MaintenanceBusy('Keep at least one other healthy AdGuard during maintenance')
            token = secrets.token_hex(24)
            self.active = {'node': node, 'token': token, 'expires': now + self.ttl}
            return {'token': token, 'ttl': self.ttl}

    def renew(self, node, token):
        with self.lock:
            if not self.owner or not self.active or self.active['node'] != node or not hmac.compare_digest(self.active['token'],token):
                raise MaintenanceBusy('maintenance_lease_lost')
            self.active['expires'] = self.clock() + self.ttl
            return {'ttl':self.ttl}

    def release(self, node, token):
        with self.lock:
            if (self.active and self.active['node'] == node
                    and hmac.compare_digest(self.active['token'], token)):
                self.active = None
                return True
            return False


def backend_probe():
    import importlib.util
    path = Path(__file__).with_name('dns-probe.py')
    if not path.exists(): path = Path(__file__).with_name('dns_probe.py')
    spec = importlib.util.spec_from_file_location('maintenance_dns_probe', path)
    module = importlib.util.module_from_spec(spec); spec.loader.exec_module(module)
    return module


def native_ready(node):
    try:
        public = json.loads(Path('/etc/desk-dns/public.json').read_bytes())
        probe = backend_probe()
        import secrets
        payload = probe.request(probe.HEALTH, secrets.randbelow(65536))
        context = ssl.create_default_context(cafile='/etc/desk-dns/sync/backend.crt')
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), NoRedirect(), urllib.request.HTTPSHandler(context=context))
        request = urllib.request.Request('https://' + public['nodes'][node]['private'] + ':5443/dns-query',
            data=payload, method='POST', headers={'Content-Type':'application/dns-message',
            'X-Forwarded-For':public['nodes'][node]['management']})
        with opener.open(request, timeout=2) as response:
            data = response.read(4097)
        return probe.answers(data, payload) == ['127.0.0.1']
    except Exception:
        return False


def other_backend_ready(node):
    public = json.loads(Path('/etc/desk-dns/public.json').read_bytes())
    return any(native_ready(peer) for peer in public['nodes'] if peer != node)


def coordinator(config):
    return 'https://' + config['vip'] + ':' + str(config['port'])


def lease_request(config, value):
    local = owns(config['vip'])
    context = None if local else ssl.create_default_context(cafile=config['ca'])
    base = 'http://127.0.0.1:' + str(config['port']) if local else coordinator(config)
    try:
        return fetch(base + '/desk-sync/lease', config, 'POST', value,
                     context=context, return_mutation=True)
    except urllib.error.HTTPError as error:
        if error.code != 409: raise
        try:
            pending=json.loads(error.read(2048))
            reason=pending.get('reason','filter_maintenance_busy')
            retry=pending.get('retry_after',5)
            if not isinstance(reason,str) or len(reason)>128: reason='filter_maintenance_busy'
            raise MaintenanceBusy(reason,retry) from None
        finally: error.close()


def acquire_maintenance(config):
    return lease_request(config, {'action': 'acquire', 'node': config['node']})['token']


def release_maintenance(config, token):
    try:
        lease_request(config, {'action': 'release', 'node': config['node'], 'token': token})
    except Exception:
        # Lost owner/transport is safe: lease expires, next owner has a cooldown.
        pass


def native_pid():
    return subprocess.check_output(['systemctl','show','desk-adguard','--property=MainPID,InvocationID','--value'],text=True,timeout=2).strip()


def record_filter_job(config, running):
    global _FILTER_JOB
    if not config.get('state'): return
    path=Path(config['state']).with_name('native-job.json')
    with _FILTER_JOB_LOCK:
        if running:
            _FILTER_JOB={'pid':native_pid()}
            temporary=path.with_suffix('.pending')
            fd=os.open(temporary,os.O_WRONLY|os.O_CREAT|os.O_TRUNC,0o600)
            with os.fdopen(fd,'wb') as stream:
                stream.write(encoded(_FILTER_JOB));stream.flush();os.fsync(stream.fileno())
            temporary.replace(path)
        else:
            _FILTER_JOB=None
            path.unlink(missing_ok=True)


def pending_filter_job():
    return _FILTER_JOB is not None


def other_filter_job(config):
    if pending_filter_job(): return True
    context=ssl.create_default_context(cafile='/etc/desk-dns/sync/backend.crt')
    for node,addresses in public_nodes().items():
        if node==config['node']: continue
        try:
            result=fetch('https://'+addresses['private']+':5443/desk-observe/policy/jobs',
                         config,'POST',{},context=context,return_mutation=True,timeout=2)
            if result.get('busy'): return True
        except Exception:
            # An unreachable node cannot be selected as a healthy maintenance backend.
            # Its native job is excluded from forwarding until health recovers.
            if native_ready(node): return True
    return False


def recover_maintenance(config,token,uncertain=False):
    stable=0
    while stable<3:
        try: lease_request(config, {'action':'renew','node':config['node'],'token':token})
        except Exception: return
        if uncertain and pending_filter_job():
            try:
                if native_pid()!=_FILTER_JOB['pid']: record_filter_job(config,False)
                else:
                    time.sleep(5);continue
            except Exception:
                time.sleep(5);continue
        stable=stable+1 if native_ready(config['node']) else 0
        time.sleep(5)
    release_maintenance(config,token)


@contextmanager
def held_maintenance(config):
    token = acquire_maintenance(config)
    stopped, lost = threading.Event(), threading.Event()
    def renew():
        while not stopped.wait(20):
            try: lease_request(config, {'action':'renew', 'node':config['node'], 'token':token})
            except Exception: lost.set()
    thread = threading.Thread(target=renew, daemon=True); thread.start()
    try:
        yield lost
    except urllib.error.HTTPError as error:
        if 400 <= error.code < 500: release_maintenance(config,token)
        else:
            # Unknown server failures keep the lease until native health recovers.
            threading.Thread(target=lambda: recover_maintenance(config,token,uncertain=pending_filter_job()),daemon=True).start()
        raise
    except Exception:
        # An HTTP timeout cannot prove that the native operation stopped.
        # Keep renewing while DNS is unhealthy. No success is acknowledged here.
        threading.Thread(target=lambda: recover_maintenance(config,token,uncertain=pending_filter_job()),daemon=True).start()
        raise
    else:
        if lost.is_set(): raise MaintenanceBusy('maintenance_lease_lost')
        release_maintenance(config, token)
    finally:
        stopped.set()


def filter_write(config, route, value):
    if route not in WRITES or not route.startswith('filtering/'):
        raise ValueError('Only filter mutations allowed')
    value = validate_write(route, value, serialized=True)
    with held_maintenance(config) as lost:
        record_filter_job(config,True)
        try:
            result = fetch(config['adguard'] + '/control/' + route, config, WRITES[route],
                       value, return_mutation=route == 'filtering/refresh', timeout=900)
        except urllib.error.HTTPError:
            # A complete native HTTP error proves its synchronous handler ended.
            # Quarantine DNS health separately; only transport uncertainty keeps
            # the durable in-flight marker until the process incarnation changes.
            record_filter_job(config,False)
            raise
        else: record_filter_job(config,False)
        deadline = time.monotonic() + 30
        while not native_ready(config['node']):
            if lost.is_set() or time.monotonic() >= deadline:
                raise MaintenanceBusy('native_dns_health_pending')
            time.sleep(1)
        return result


def refresh_filters(config):
    if not owns(config['vip']): return 0
    path = Path(config['state']).with_name('refresh.json')
    previous = json.loads(path.read_bytes()) if path.exists() else {}
    if time.time() - previous.get('completed_at', 0) < 86400: return 0
    try:
        public = json.loads(Path('/etc/desk-dns/public.json').read_bytes())
        context = ssl.create_default_context(cafile='/etc/desk-dns/sync/backend.crt')
        for node, addresses in sorted(public['nodes'].items()):
            if not owns(config['vip']): raise MaintenanceBusy('DNS owner changed')
            for whitelist in (False, True):
                if node == config['node']:
                    filter_write(config, 'filtering/refresh', {'whitelist': whitelist, 'force': True})
                else:
                    fetch('https://' + addresses['private'] + ':5443/desk-observe/control/filtering/refresh',
                          config, 'POST', {'whitelist': whitelist, 'force': True}, context=context, timeout=150)
        temporary = path.with_suffix('.tmp')
        temporary.write_bytes(encoded({'completed_at': time.time()})); temporary.chmod(0o600)
        temporary.replace(path)
        print('DNS filters refreshed serially on 3 nodes', flush=True)
        return 0
    except Exception:
        print('DNS filter refresh deferred; kept current filters; next timer retries', flush=True)
        return 1


class Server(http.server.ThreadingHTTPServer):
    daemon_threads = True
    request_queue_size = 8

    def __init__(self, address, config, source=False, maintenance=None, policy=None):
        self.config = config
        self.policy = policy
        self.source = source
        self.maintenance = maintenance or Maintenance(config)
        self.tls = None
        self.slots = threading.BoundedSemaphore(8)
        super().__init__(address, Handler)

    def get_request(self):
        request, address = super().get_request()
        request.settimeout(3)
        if self.source:
            if address[0] not in self.config['management_nodes']:
                request.close()
                raise OSError('Peer not allowed')
            try:
                request = self.tls.wrap_socket(request, server_side=True)
            except Exception:
                request.close()
                raise
        return request, address

    def process_request(self, request, address):
        if not self.slots.acquire(blocking=False):
            self.shutdown_request(request)
            return
        super().process_request(request, address)

    def process_request_thread(self, request, address):
        try:
            super().process_request_thread(request, address)
        finally:
            self.slots.release()


class Handler(http.server.BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def setup(self):
        super().setup()
        self.connection.settimeout(10)

    def answer(self, status, value):
        data = encoded(value)
        self.send_response(status)
        self.send_header('Content-Type', 'application/json')
        self.send_header('Content-Length', str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def handle_api(self):
        config = self.server.config
        try:
            if self.server.source and self.client_address[0] not in config['management_nodes']:
                return self.answer(403, {'error': 'peer_not_allowed'})
            if not hmac.compare_digest(self.headers.get('Authorization', ''), basic(config)):
                return self.answer(401, {'error': 'authentication_required'})
            if self.path.startswith('/desk-sync/policy/') and self.command == 'POST' and not self.server.source:
                length = int(self.headers.get('Content-Length', '0'))
                if self.headers.get('Transfer-Encoding') or not 0 <= length <= LIMIT:
                    return self.answer(400, {'error':'invalid_body'})
                value = json.loads(self.rfile.read(length) or b'{}')
                action = self.path.removeprefix('/desk-sync/policy/')
                policy = self.server.policy
                if action == 'write':
                    result = policy.write(value['route'],value['body'])
                elif action == 'public-status': result=policy.public_status()
                elif action == 'reconcile':
                    policy.reconcile()
                    if policy.error: return self.answer(503,{'error':policy.error})
                    result=policy.journal.status()
                else: result = policy.peer(action,value)
                return self.answer(200,result)
            owner = owns(config['vip'])
            if self.path == '/desk-sync/lease' and self.command == 'POST':
                if not owner: return self.answer(409, {'error': 'source_unavailable'})
                self.server.maintenance.observe(owner)
                length = int(self.headers.get('Content-Length', '0'))
                if self.headers.get('Transfer-Encoding') or not 0 < length <= 1024:
                    return self.answer(400, {'error': 'invalid_body'})
                value = json.loads(self.rfile.read(length))
                if value.get('action') == 'acquire':
                    return self.answer(200, self.server.maintenance.acquire(value['node']))
                if value.get('action') == 'renew':
                    return self.answer(200,self.server.maintenance.renew(value['node'],value['token']))
                if value.get('action') == 'release':
                    return self.answer(200, {'released': self.server.maintenance.release(value['node'], value['token'])})
                return self.answer(400, {'error': 'invalid_lease_action'})
            if self.path == '/desk-sync/register' and self.command == 'POST' and not self.server.source:
                length = int(self.headers.get('Content-Length', '0'))
                if self.headers.get('Transfer-Encoding') or not 0 < length <= 1024:
                    return self.answer(400, {'error': 'invalid_body'})
                value = json.loads(self.rfile.read(length))
                token = value.get('token')
                if token is not None and (not isinstance(token, str) or len(token) != 48):
                    raise ValueError('Invalid token')
                self.server.maintenance.local_token = (token, time.monotonic() + Maintenance.ttl) if token else None
                return self.answer(200, None)
            if self.path.startswith('/desk-sync/filter/') and self.command == 'POST' and not self.server.source:
                length = int(self.headers.get('Content-Length', '0'))
                if self.headers.get('Transfer-Encoding') or not 0 <= length <= LIMIT:
                    return self.answer(400, {'error': 'invalid_body'})
                route = 'filtering/' + self.path.removeprefix('/desk-sync/filter/')
                result = filter_write(config, route, json.loads(self.rfile.read(length) or b'{}'))
                return self.answer(200, result)
            if self.server.source and (not owner or self.command != 'GET'):
                return self.answer(409, {'error': 'source_unavailable'})
            if self.path == '/desk-sync/owner' and self.command == 'GET':
                return self.answer(200, {'node': config['node'], 'owner': owner})
            if not self.path.startswith('/control/'):
                return self.answer(404, {'error': 'route_not_allowed'})
            route = self.path[len('/control/'):]
            if self.command == 'GET':
                if route not in READS:
                    return self.answer(404, {'error': 'route_not_allowed'})
                value = fetch(config['adguard'] + '/control/' + route, config)
                # Filtering runtime IDs are needed by upstream, but not part of equality.
                if route == 'filtering/status':
                    projected = project(route, value)
                    for kind in ('filters', 'whitelist_filters'):
                        for original, item in zip(value.get(kind) or [], projected[kind]):
                            item['id'] = original['id']
                    value = projected
                else:
                    value = project(route, value)
                return self.answer(200, value)
            if WRITES.get(route) != self.command:
                return self.answer(405, {'error': 'route_not_allowed'})
            # A receiver becoming VIP owner stops accepting a stale pull immediately.
            if owner:
                return self.answer(409, {'error': 'receiver_is_now_origin'})
            if self.headers.get('Transfer-Encoding'):
                return self.answer(400, {'error': 'invalid_body'})
            length = int(self.headers.get('Content-Length', '0'))
            if not 0 <= length <= LIMIT:
                return self.answer(413, {'error': 'body_too_large'})
            body = self.rfile.read(length)
            if len(body) != length:
                raise ValueError('Incomplete request')
            value = json.loads(body) if body.strip() else {}
            validate_write(route, value, serialized=config.get('serialized_updates', False))
            if route.startswith('filtering/') and config.get('serialized_updates'):
                lease = self.server.maintenance.local_token
                if not lease or lease[1] <= time.monotonic():
                    raise MaintenanceBusy('Replica maintenance lease required')
            result = fetch(config['adguard'] + '/control/' + route, config,
                           self.command, value)
            return self.answer(200, result)
        except MaintenanceBusy as error:
            self.answer(409, {'error':'filter_maintenance_busy', 'reason':error.reason, 'retry_after':error.retry_after})
        except PolicyFailure as error:
            self.answer(error.code, {'error':error.message})
        except urllib.error.HTTPError as error:
            # Native rejection is distinct from transport failure; never echo URLs.
            error.close()
            self.answer(error.code, {'error': 'adguard_request_rejected'})
        except (ValueError, KeyError, TypeError):
            self.answer(400, {'error': 'invalid_policy'})
        except Exception:
            # Never echo AdGuard error bodies: upstream URLs can contain credentials.
            self.answer(503, {'error': 'local_adguard_unavailable'})

    do_GET = handle_api
    do_POST = handle_api
    do_PUT = handle_api


POLICY_ROUTES = ['status', 'profile', 'querylog/config', 'stats/config', 'dns_info', 'filtering/status', 'rewrite/list', 'rewrite/settings',
                 'clients', 'blocked_services/get', 'safesearch/status',
                 'safebrowsing/status', 'parental/status']


def policy_snapshot(base, config, context=None):
    def canonical(value):
        if isinstance(value, dict):
            return {k: canonical(v) for k, v in value.items()}
        if isinstance(value, list):
            # Rule/upstream ordering matters. Normalize only unordered collections below.
            return [canonical(x) for x in value]
        return value
    policy = {}
    for route in POLICY_ROUTES:
        value = project(route, fetch(base + '/control/' + route, config, context=context))
        if route == 'filtering/status':
            for kind in ('filters', 'whitelist_filters'):
                value[kind].sort(key=lambda x: x['url'])
        if route == 'rewrite/list':
            value.sort(key=lambda x: encoded(x))
        if route == 'clients':
            value['clients'].sort(key=lambda x: x['name'])
        policy[route] = canonical(value)
    return policy


def snapshot(base, config, context=None):
    return hashlib.sha256(encoded(policy_snapshot(base,config,context))).hexdigest()


def atomic_status(config, value):
    path = Path(config['state'])
    temporary = path.with_suffix('.tmp')
    temporary.write_bytes(encoded(value))
    temporary.chmod(0o600)
    temporary.replace(path)


def legacy_pull(config):
    state = {'protocol': 'desk-dns-sync-v1', 'node': config['node'], 'checked_at': time.time()}
    token = None
    complete = False
    try:
        if owns(config['vip']):
            state['state'] = 'origin'
            atomic_status(config, state)
            return 0
        context = ssl.create_default_context(cafile=config['ca'])
        origin = 'https://' + config['vip'] + ':' + str(config['port'])
        local = 'http://127.0.0.1:' + str(config['port'])
        identity = fetch(origin + '/desk-sync/owner', config, context=context)
        if not identity.get('owner') or identity.get('node') not in config['nodes']:
            raise ValueError('No validated source')
        before = snapshot(origin, config, context)
        # Avoid an upstream process/filter refresh when policy already agrees.
        if before != snapshot(local, config):
            if config.get('serialized_updates'):
                token = acquire_maintenance(config)
                fetch(local + '/desk-sync/register', config, 'POST', {'token': token})
                if before != snapshot(origin, config, context):
                    raise MaintenanceBusy('Source policy changed before maintenance')
            result = subprocess.run([config['binary'], 'run', '--config', config['sync_config']],
                                    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                                    timeout=90, env={**os.environ, 'LOG_LEVEL': 'error',
                                                     'SSL_CERT_FILE': config['ca']})
            if result.returncode:
                raise ValueError('Upstream sync process failed')
        after_identity = fetch(origin + '/desk-sync/owner', config, context=context)
        after = snapshot(origin, config, context)
        if owns(config['vip']) or identity != after_identity or before != after:
            raise ValueError('Source changed during synchronization')
        if after != snapshot(local, config):
            raise ValueError('Policy does not yet agree')
        complete = True
        state.update(state='synced', origin=identity['node'], policy_sha256=after,
                     last_success_at=time.time())
        atomic_status(config, state)
        return 0
    except Exception:
        # Process exit alone is insufficient: upstream returns zero on some API errors.
        state.update(state='retry_pending', error='policy_sync_unavailable')
        atomic_status(config, state)
        print('DNS policy sync unavailable; retained local policy; next timer retries', flush=True)
        return 1
    finally:
        if token:
            try: fetch(local + '/desk-sync/register', config, 'POST', {'token': None})
            except Exception: pass
            if complete and native_ready(config['node']): release_maintenance(config, token)


def public_nodes():
    return json.loads(Path('/etc/desk-dns/public.json').read_bytes())['nodes']


def pull(config):
    # In-process journal state avoids races between timer processes and HTTP writes.
    try:
        fetch('http://127.0.0.1:'+str(config['port'])+'/desk-sync/policy/reconcile',
              config, 'POST', {}, timeout=150)
        return 0
    except Exception:
        print('DNS committed policy reconciliation pending; next timer retries', flush=True)
        return 1


def load_policy():
    path = Path(__file__).with_name('dns-policy.py')
    if not path.exists(): path=Path(__file__).with_name('dns_policy.py')
    spec=importlib.util.spec_from_file_location('desk_dns_policy',path)
    module=importlib.util.module_from_spec(spec);spec.loader.exec_module(module)
    return module


class PolicyFailure(Exception):
    pass


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('action', choices=['serve', 'pull', 'refresh'])
    parser.add_argument('--config', default='/etc/desk-dns/sync-agent.json')
    args = parser.parse_args()
    config = json.loads(Path(args.config).read_bytes())
    if args.action == 'refresh':
        raise SystemExit(refresh_filters(config))
    if args.action == 'pull':
        raise SystemExit(pull(config))
    global _FILTER_JOB
    marker=Path(config['state']).with_name('native-job.json')
    if marker.exists():
        _FILTER_JOB=json.loads(marker.read_bytes())
        if native_pid()!=_FILTER_JOB['pid']: record_filter_job(config,False)
    import sys
    # importlib-loaded tests may not register this module in sys.modules.
    from types import SimpleNamespace
    api=SimpleNamespace(**globals())
    module=load_policy()
    global PolicyFailure
    PolicyFailure=module.Failure
    policy=module.Policy(config,api)
    maintenance = Maintenance(config)
    def watch_owner():
        while True:
            try: maintenance.observe(owns(config['vip']))
            except Exception: maintenance.observe(False)
            time.sleep(2)
    threading.Thread(target=watch_owner, daemon=True).start()
    local = Server(('127.0.0.1', config['port']), config, maintenance=maintenance, policy=policy)
    source = Server((config['vip'], config['port']), config, source=True, maintenance=maintenance, policy=policy)
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.minimum_version = ssl.TLSVersion.TLSv1_2
    context.load_cert_chain(config['certificate'], config['key'])
    source.tls = context
    threading.Thread(target=source.serve_forever, daemon=True).start()
    local.serve_forever()


if __name__ == '__main__':
    main()
