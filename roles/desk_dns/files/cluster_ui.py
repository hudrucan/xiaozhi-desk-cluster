#!/usr/bin/env python3
"""Aggregate AdGuard observability; DNS requests never traverse this service."""
import argparse
import importlib.util
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
import hmac
import urllib.error
import http.server
import json
from pathlib import Path
import ssl
import threading
import urllib.parse
import urllib.request

path=Path(__file__).with_name('desk-dns-sync.py')
if not path.exists(): path=Path(__file__).with_name('desk_dns_sync.py')
spec=importlib.util.spec_from_file_location('dns_public_write_contract',path)
contract=importlib.util.module_from_spec(spec);spec.loader.exec_module(contract)

FILTER_WRITES = {'filtering/config', 'filtering/add_url', 'filtering/remove_url',
                 'filtering/set_url', 'filtering/set_rules', 'filtering/refresh'}
LIMIT = 4 * 1024 * 1024
READS = {'stats', 'querylog', 'clients', 'clients/find', 'status'}
CONFIG_WRITES = {('PUT', 'querylog/config/update'), ('PUT', 'stats/config/update'), ('POST', 'querylog_config'), ('POST', 'stats_config')}
OPERATIONS = {('POST', 'clients/search'), ('POST', 'stats_reset'), ('POST', 'querylog_clear'), ('POST', 'cache_clear')} | CONFIG_WRITES


def known_client_names(route, value, nodes):
    names = {address: name for name, addresses in nodes.items() for address in addresses.values()}
    if route.split('?')[0] == 'clients':
        for client in value.get('auto_clients') or []:
            if not client.get('name') and client.get('ip') in names: client['name'] = names[client['ip']]
    elif route.split('?')[0] in ('clients/find', 'clients/search'):
        for item in value or []:
            for address, client in item.items():
                if address in names and not (client or {}).get('name'): item[address] = {**(client or {}), 'name': names[address]}
    elif route.split('?')[0] == 'querylog':
        for row in value.get('data') or []:
            if row.get('client') in names and not (row.get('client_info') or {}).get('name'):
                row['client_info'] = {**(row.get('client_info') or {}), 'name': names[row['client']]}
    return value


def merge_stats(values):
    if len({v['time_units'] for v in values}) != 1:
        raise ValueError('Statistics intervals disagree')
    result = {'time_units': values[0]['time_units']}
    for key in ('num_dns_queries', 'num_blocked_filtering', 'num_replaced_safebrowsing',
                'num_replaced_safesearch', 'num_replaced_parental'):
        result[key] = sum(v.get(key, 0) for v in values)
    count = result['num_dns_queries']
    result['avg_processing_time'] = sum(v.get('avg_processing_time', 0) * v['num_dns_queries'] for v in values) / count if count else 0
    for key in ('dns_queries', 'blocked_filtering', 'replaced_safebrowsing', 'replaced_parental'):
        arrays = [v.get(key, []) for v in values]
        if len({len(a) for a in arrays}) != 1:
            raise ValueError('Statistics windows disagree')
        result[key] = [sum(x) for x in zip(*arrays)]
    for key in ('top_queried_domains', 'top_clients', 'top_blocked_domains', 'top_upstreams_responses'):
        counts = {}
        for value in values:
            for entry in (value.get(key) or []):
                for name, n in entry.items():
                    counts[name] = counts.get(name, 0) + n
        result[key] = [{name: n} for name, n in sorted(counts.items(), key=lambda x: (-x[1], x[0]))[:100]]
    totals, weights = {}, {}
    for value in values:
        counts = {k: n for row in (value.get('top_upstreams_responses') or []) for k, n in row.items()}
        for row in (value.get('top_upstreams_avg_time') or []):
            for name, average in row.items():
                n = counts.get(name, 0)
                totals[name] = totals.get(name, 0) + average * n
                weights[name] = weights.get(name, 0) + n
    result['top_upstreams_avg_time'] = [{k: totals[k] / n} for k, n in sorted(weights.items()) if n]
    return result


def merge_clients(values):
    persistent, automatic, tags = {}, {}, set()
    for value in values:
        for client in (value.get('clients') or []):
            key = (client['name'], tuple(sorted(client.get('ids', []))))
            persistent.setdefault(key, client)
        for client in (value.get('auto_clients') or []):
            previous = automatic.get(client['ip'])
            if previous is None or (not previous.get('name') and client.get('name')):
                automatic[client['ip']] = client
        tags.update((value.get('supported_tags') or []))
    return {'clients': list(persistent.values()), 'auto_clients': [automatic[k] for k in sorted(automatic)], 'supported_tags': sorted(tags)}


def merge_lookup(values):
    found = {}
    for value in values:
        for item in value or []:
            for key, client in item.items():
                client = client or {}
                if key not in found or (not found[key].get('name') and client.get('name')):
                    found[key] = client
    return [{k: dict(v, name=v.get('name', ''))} for k, v in found.items()]


def merge_logs(values, limit, offset=0):
    rows = [dict(row, desk_node=node) for node, value in values.items() for row in (value.get('data') or [])]
    rows.sort(key=lambda r: (datetime.fromisoformat(r['time'].replace('Z', '+00:00')).timestamp(), r['time'], r['desk_node']), reverse=True)
    rows = rows[offset:offset + limit]
    return {'data': rows, 'oldest': rows[-1]['time'] if rows else ''}


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None


class Server(http.server.ThreadingHTTPServer):
    daemon_threads = True
    request_queue_size = 16

    def __init__(self, config):
        self.config = config
        self.pool = ThreadPoolExecutor(max_workers=6)
        self.slots = threading.BoundedSemaphore(8)
        self.context = ssl.create_default_context(cafile=config['ca'])
        super().__init__(('127.0.0.1', 3003), Handler)

    def process_request(self, request, address):
        if not self.slots.acquire(blocking=False):
            self.shutdown_request(request); return
        super().process_request(request, address)

    def process_request_thread(self, request, address):
        try: super().process_request_thread(request, address)
        finally: self.slots.release()

    def call(self, node, route, method='GET', body=None):
        local = node == self.config.get('node')
        address = self.config['nodes'][node]['private']
        headers = {'Authorization': self.config['authorization']}
        body = body or None
        if body is not None: headers['Content-Type'] = 'application/json'
        endpoint = 'http://127.0.0.1:3001/control/' if local else 'https://' + address + ':5443/desk-observe/control/'
        request = urllib.request.Request(endpoint + route,
                                         data=body, method=method, headers=headers)
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), NoRedirect(), urllib.request.HTTPSHandler(context=self.context))
        with opener.open(request, timeout=3 if method=='GET' else 60) as response:
            data = response.read(LIMIT + 1)
            if len(data) > LIMIT: raise ValueError('Oversize peer response')
            if method != 'GET' and not route.startswith('clients/search'): return None
            value = json.loads(data) if data.strip() else None
            return known_client_names(route, value, self.config['nodes']) if value is not None else None

    def fanout(self, route, method='GET', body=None):
        jobs = {n: self.pool.submit(self.call, n, route, method, body) for n in self.config['nodes']}
        results, missing = {}, []
        for node, job in jobs.items():
            try: results[node] = job.result(timeout=4 if method=='GET' else 65)
            except Exception as error:
                missing.append(node)
                print('DNS cluster request failed; node=' + node + ' operation=' + route.split('?')[0] + ' code=' + str(getattr(error, 'code', type(error).__name__)), flush=True)
        if not results: raise OSError('No available nodes')
        return results, missing

    def native(self, path, method='GET', body=None):
        # UI assets and ordinary reads can use another native hub during a rebuild.
        # Persistent writes are handled by the shared policy boundary above this.
        if not path.startswith('/') or path.startswith('//') or '/desk-' in path:
            raise ValueError('Invalid native UI path')
        ordered=sorted(self.config['nodes'],key=lambda n:n!=self.config['node'])
        last=None
        for node in ordered:
            try:
                if node==self.config['node']:
                    return self.native_local(path,method,body)
                endpoint='https://'+self.config['nodes'][node]['private']+':5443/desk-observe/ui'+path
                request=urllib.request.Request(endpoint,data=body or None,method=method,
                    headers={'Authorization':self.config['authorization'],'Content-Type':'application/json'})
                opener=urllib.request.build_opener(urllib.request.ProxyHandler({}),NoRedirect(),urllib.request.HTTPSHandler(context=self.context))
                with opener.open(request,timeout=4) as response:
                    data=response.read(16*1024*1024+1)
                    if len(data)>16*1024*1024: raise ValueError('Native asset too large')
                    return data,response.headers.get('Content-Type','application/octet-stream'),node
            except urllib.error.HTTPError as error:
                if error.code<500: raise
                last=error
            except Exception as error: last=error
        raise OSError('Native UI sources unavailable') from last

    def native_local(self,path,method='GET',body=None):
        request=urllib.request.Request('http://127.0.0.1:3001'+path,data=body or None,method=method,
            headers={'Content-Type':'application/json'} if body else {})
        opener=urllib.request.build_opener(urllib.request.ProxyHandler({}),NoRedirect())
        with opener.open(request,timeout=3 if method=='GET' else 60) as response:
            data=response.read(16*1024*1024+1)
            if len(data)>16*1024*1024: raise ValueError('Native asset too large')
            return data,response.headers.get('Content-Type','application/octet-stream'),self.config['node']

    def policy(self,action,body):
        request=urllib.request.Request('http://127.0.0.1:3002/desk-sync/policy/'+action,
            data=json.dumps(body).encode(),method='POST',
            headers={'Authorization':self.config['authorization'],'Content-Type':'application/json'})
        opener=urllib.request.build_opener(urllib.request.ProxyHandler({}),NoRedirect())
        with opener.open(request,timeout=160 if action=='write' else 960 if action=='apply' else 10) as response:
            return json.loads(response.read(LIMIT+1))


class Handler(http.server.BaseHTTPRequestHandler):
    def log_message(self, *args): pass

    def setup(self):
        super().setup(); self.connection.settimeout(5)

    def answer(self, status, value, missing=None, content_type='application/json', revision=None, operation_id=None):
        data = value if isinstance(value, bytes) else json.dumps(value, separators=(',', ':')).encode()
        self.send_response(status)
        self.send_header('Content-Type', content_type)
        self.send_header('Content-Length', str(len(data)))
        self.send_header('Cache-Control', 'no-store')
        if missing is not None: self.send_header('X-Desk-DNS-Coverage', str(3-len(missing))+'/3')
        if revision is not None: self.send_header('X-Desk-DNS-Revision',str(revision))
        if operation_id: self.send_header('X-Desk-DNS-Operation',operation_id)
        if status==202: self.send_header('X-Desk-DNS-State','pending')
        self.end_headers(); self.wfile.write(data)

    def local(self, route, method, body):
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), NoRedirect())
        body = body or None
        headers = {'Content-Type': 'application/json'} if body is not None else {}
        request = urllib.request.Request('http://127.0.0.1:3001/control/' + route, data=body, method=method, headers=headers)
        with opener.open(request, timeout=3 if method=='GET' else 60) as response:
            data = response.read(LIMIT + 1)
            if len(data) > LIMIT: raise ValueError('Oversize local response')
            return data

    def handle_api(self):
        try:
            parts = urllib.parse.urlsplit(self.path)
            peer = parts.path.startswith('/desk-observe/control/')
            route = parts.path.removeprefix('/desk-observe/control/' if peer else '/control/')
            operation = (self.command, route)
            if self.headers.get('Transfer-Encoding'): return self.answer(400, {'error': 'invalid_body'})
            length = int(self.headers.get('Content-Length', '0'))
            if not 0 <= length <= 65536: return self.answer(413, {'error': 'body_too_large'})
            body = self.rfile.read(length) if self.command != 'GET' else None
            if body is not None and len(body) != length: raise ValueError('Incomplete body')
            internal=parts.path.startswith('/desk-observe/')
            if internal and not hmac.compare_digest(self.headers.get('Authorization',''),self.server.config['authorization']):
                return self.answer(401,{'error':'authentication_required'})
            if parts.path.startswith('/desk-observe/policy/') and self.command=='POST':
                result=self.server.policy(parts.path.removeprefix('/desk-observe/policy/'),json.loads(body or b'{}'))
                return self.answer(200,result)
            if parts.path.startswith('/desk-observe/ui/') and self.command=='GET':
                target=parts.path.removeprefix('/desk-observe/ui')+('?' +parts.query if parts.query else '')
                data,mime,node=self.server.native_local(target)
                return self.answer(200,data,[n for n in self.server.config['nodes'] if n!=node],mime)
            if internal and not peer: return self.answer(404,{'error':'route_not_allowed'})
            if route=='policy-wait.js' and not peer and self.command=='GET':
                path=Path(__file__).with_name('policy-wait.js')
                if not path.exists(): path=Path(__file__).with_name('policy_wait.js')
                return self.answer(200,path.read_bytes(),content_type='application/javascript')
            if route == 'cluster.js' and not peer and self.command=='GET':
                return self.answer(200,b'',content_type='application/javascript')
            if not peer and parts.path=='/control/desk_policy' and self.command=='GET':
                result=self.server.policy('public-status',{})
                return self.answer(200,result,[n for n in self.server.config['nodes'] if n not in result['ready']],revision=result['revision'])
            if not peer and route=='status' and self.command=='GET':
                data,mime,node=self.server.native('/control/status')
                return self.answer(200,data,[n for n in self.server.config['nodes'] if n!=node],mime)
            if not peer and parts.path=='/control/desk_health' and self.command=='GET':
                self.server.native('/control/status')
                return self.answer(200,{'frontend':'ready'})
            if peer:
                if self.command=='POST' and route in FILTER_WRITES:
                    request=urllib.request.Request('http://127.0.0.1:3002/desk-sync/filter/'+route.removeprefix('filtering/'),
                        data=body,method='POST',headers={'Authorization':self.server.config['authorization'],'Content-Type':'application/json'})
                    opener=urllib.request.build_opener(urllib.request.ProxyHandler({}),NoRedirect())
                    with opener.open(request,timeout=960) as response: data=response.read(LIMIT+1)
                elif (self.command=='GET' and route.split('?')[0] in READS) or operation in OPERATIONS:
                    data=self.local(route+('?' +parts.query if parts.query else ''),self.command,body)
                else: return self.answer(404,{'error':'route_not_allowed'})
                return self.answer(200,data)
            if parts.path.startswith('/control/') and contract.WRITES.get(route)==self.command:
                value=json.loads(body or b'{}')
                contract.validate_write(route,value,serialized=True)
                result=self.server.policy('write',{'route':route,'body':value})
                missing=[n for n in self.server.config['nodes'] if n not in result['ready']]
                return self.answer(result['status'],result['result'],missing,revision=result['revision'],operation_id=result.get('operation_id'))
            aggregated=(self.command=='GET' and route in READS) or operation in OPERATIONS or (route=='desk_cluster' and self.command=='GET')
            if not aggregated:
                # Safe native read/test routes. Unsupported writes cannot bypass
                # shared validation or modify the protected transport configuration.
                if self.command!='GET' and route not in ('test_upstream_dns', 'filtering/check_host'):
                    return self.answer(405,{'error':'unsupported_shared_write'})
                data,mime,node=self.server.native(self.path,self.command,body)
                if parts.path=='/' and 'text/html' in mime:
                    data=data.replace(b'</head>',b'<script src="control/policy-wait.js"></script></head>',1)
                return self.answer(200,data,[n for n in self.server.config['nodes'] if n!=node],mime)
            query = urllib.parse.parse_qs(parts.query)
            offset = int(query.pop('offset', ['0'])[0]); limit = int(query.get('limit', ['100'])[0])
            if not 0 <= offset <= 1000 or not 1 <= limit <= 500: raise ValueError('Invalid pagination')
            if route == 'querylog': query['limit'] = [str(offset + limit)]
            suffix = urllib.parse.urlencode(query, doseq=True)
            target = 'stats' if route == 'desk_cluster' else route
            results, missing = self.server.fanout(target + ('?' + suffix if suffix else ''), self.command, body)
            values = list(results.values())
            if route == 'desk_cluster':
                value = {'available': len(results), 'total': 3, 'missing': missing,
                         'nodes': {n: {'queries': v['num_dns_queries'], 'blocked': v['num_blocked_filtering'], 'average_ms': v.get('avg_processing_time', 0) * 1000} for n, v in results.items()}}
            elif route == 'stats': value = merge_stats(values)
            elif route == 'querylog': value = merge_logs(results, limit, offset)
            elif route == 'clients': value = merge_clients(values)
            elif route in ('clients/find', 'clients/search'): value = merge_lookup(values)
            else: value = None
            return self.answer(202 if missing and operation in OPERATIONS and route!='clients/search' else 200,value,missing)
        except urllib.error.HTTPError as error:
            # Preserve safe structured pending/retry reasons; never native URL bodies.
            value={'error':'adguard_request_rejected'}
            try:
                native=json.loads(error.read(2048))
                if isinstance(native,dict):
                    value={k:v for k,v in native.items() if k in ('error','reason','retry_after')}
            except Exception: pass
            self.answer(error.code,value)
        except (ValueError, KeyError, TypeError): self.answer(400, {'error': 'incompatible_request_or_data'})
        except Exception as error:
            print('DNS UI request failed; method=' + self.command + ' operation=' + str(locals().get('route', 'invalid')) + ' code=' + type(error).__name__, flush=True)
            self.answer(503, {'error': 'cluster_observability_unavailable'}, ['deskb1x', 'deskb2x', 'deskb3x'])

    do_GET = handle_api
    do_POST = handle_api
    do_PUT = handle_api


if __name__ == '__main__':
    parser = argparse.ArgumentParser(); parser.add_argument('--config', default='/etc/desk-dns/cluster-ui.json')
    args = parser.parse_args()
    config = json.loads(Path(args.config).read_bytes())
    if set(config['nodes']) != {'deskb1x', 'deskb2x', 'deskb3x'}: raise SystemExit('Invalid membership')
    Server(config).serve_forever()
