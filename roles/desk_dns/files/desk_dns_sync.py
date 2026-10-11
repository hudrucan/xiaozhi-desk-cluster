#!/usr/bin/env python3
"""Small AdGuard API boundary around upstream adguardhome-sync, not a DNS proxy."""
import argparse
import base64
import hashlib
import hmac
import http.cookies
import http.server
import json
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
         'querylog/config', 'stats/config', 'access/list', 'dns_info'}
WRITES = {'profile/update': 'PUT', 'dns_config': 'POST',
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


def validate_write(route, value):
    if route == 'dns_config' and set(value) - DNS_FIELDS:
        raise ValueError('Host-local DNS configuration cannot be synchronized')
    if route.startswith('rewrite/'):
        entries = [value] if route != 'rewrite/update' else [value['target'], value['update']]
        if any(x.get('domain', '').lower().rstrip('.') in RESERVED for x in entries):
            raise ValueError('Reserved rewrite')
        if route == 'rewrite/settings/update' and value != {'enabled': True}:
            raise ValueError('Internal rewrites must remain enabled')
    if route == 'filtering/set_rules':
        value['rules'] = [RULE] + [r for r in value['rules'] if r != RULE]
    if route == 'profile/update':
        if set(value) - {'name', 'language', 'theme'} or value.get('name') not in ('desk-sync', ''):
            raise ValueError('Account changes are not policy')
    return value


def fetch(url, config, method='GET', value=None, context=None):
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
            with opener.open(request, timeout=25) as response:
                data = response.read(LIMIT + 1)
                if len(data) > LIMIT:
                    raise ValueError('Response exceeds policy boundary')
                if not data.strip() or method != 'GET':
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


class Server(http.server.ThreadingHTTPServer):
    daemon_threads = True
    request_queue_size = 8

    def __init__(self, address, config, source=False):
        self.config = config
        self.source = source
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
            owner = owns(config['vip'])
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
            validate_write(route, value)
            result = fetch(config['adguard'] + '/control/' + route, config,
                           self.command, value)
            return self.answer(200, result)
        except (ValueError, KeyError, TypeError):
            self.answer(400, {'error': 'invalid_policy'})
        except Exception:
            # Never echo AdGuard error bodies: upstream URLs can contain credentials.
            self.answer(503, {'error': 'local_adguard_unavailable'})

    do_GET = handle_api
    do_POST = handle_api
    do_PUT = handle_api


POLICY_ROUTES = ['dns_info', 'filtering/status', 'rewrite/list', 'rewrite/settings',
                 'clients', 'blocked_services/get', 'safesearch/status',
                 'safebrowsing/status', 'parental/status']


def snapshot(base, config, context=None):
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
    return hashlib.sha256(encoded(policy)).hexdigest()


def atomic_status(config, value):
    path = Path(config['state'])
    temporary = path.with_suffix('.tmp')
    temporary.write_bytes(encoded(value))
    temporary.chmod(0o600)
    temporary.replace(path)


def pull(config):
    state = {'protocol': 'desk-dns-sync-v1', 'node': config['node'], 'checked_at': time.time()}
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


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('action', choices=['serve', 'pull'])
    parser.add_argument('--config', default='/etc/desk-dns/sync-agent.json')
    args = parser.parse_args()
    config = json.loads(Path(args.config).read_bytes())
    if args.action == 'pull':
        raise SystemExit(pull(config))
    local = Server(('127.0.0.1', config['port']), config)
    source = Server((config['vip'], config['port']), config, source=True)
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.minimum_version = ssl.TLSVersion.TLSv1_2
    context.load_cert_chain(config['certificate'], config['key'])
    source.tls = context
    threading.Thread(target=source.serve_forever, daemon=True).start()
    local.serve_forever()


if __name__ == '__main__':
    main()
