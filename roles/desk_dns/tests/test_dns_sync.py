import copy
import http.client
import importlib.util
import json
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import patch
import urllib.error
import urllib.request


def load(name):
    path = Path(__file__).resolve().parents[1] / 'files' / (name + '.py')
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


sync = load('desk_dns_sync')
account = load('dns_sync_account')


class PolicyTests(unittest.TestCase):
    def test_account_preserves_policy_and_passwords(self):
        document = {'schema_version': 34, 'users': [{'name': 'admin', 'password': 'retained-hash'}],
                    'dns': {'port': 5354}, 'user_rules': ['private-rule']}
        original = copy.deepcopy(document)
        result, changed = account.account_candidate(document, {'username': 'desk-sync', 'password_hash': 'service-hash'})
        self.assertTrue(changed)
        self.assertEqual({k: v for k, v in result.items() if k != 'users'},
                         {k: v for k, v in original.items() if k != 'users'})
        self.assertEqual(result['users'][0], original['users'][0])
        self.assertFalse(account.account_candidate(result, {'username': 'desk-sync', 'password_hash': 'service-hash'})[1])

    def test_anonymous_local_mode_does_not_reintroduce_automation_user(self):
        document = {'schema_version': 34, 'users': [{'name': 'admin', 'password': 'fixture'}], 'dns': {'port': 5354}}
        result, changed = account.account_candidate(document, {'local_auth': False})
        self.assertTrue(changed)
        self.assertEqual(result['users'], [])
        self.assertEqual(result['dns'], {'port': 5354})
        self.assertFalse(account.account_candidate(result, {'local_auth': False})[1])

    def test_unknown_existing_account_is_not_rotated(self):
        with self.assertRaises(ValueError):
            account.account_candidate({'schema_version': 34, 'users': [{'name': 'desk-sync', 'password': 'old'}]},
                                      {'username': 'desk-sync', 'password_hash': 'new'})

    def test_rewrites_exclude_only_reserved_identity_records(self):
        entries = [{'domain': d, 'answer': '10.10.10.11'}
                   for d in ('who.desk-dns.test', 'Health.Desk-Dns.Test.', 'my.home')]
        self.assertEqual(sync.project('rewrite/list', entries), entries[2:])

    def test_dns_projection_retains_policy_not_bind_or_paths(self):
        self.assertEqual(sync.project('dns_info', {'upstream_dns': ['1.1.1.1'],
                                                 'bind_hosts': ['10.10.10.11'], 'port': 5354,
                                                 'default_local_ptr_upstreams': ['private'],
                                                 'upstream_dns_file': ''}), {'upstream_dns': ['1.1.1.1']})
        with self.assertRaises(ValueError):
            sync.project('dns_info', {'upstream_dns_file': '/private/file'})

    def test_runtime_clients_and_filter_metadata_are_not_policy(self):
        self.assertEqual(sync.project('clients', {'auto_clients': [{'ip': 'private'}],
                                                 'clients': [{'name': 'one', 'ids': ['one'], 'disallowed': False}]}),
                         {'clients': [{'name': 'one', 'ids': ['one']}]})
        source = {'enabled': True, 'interval': 24, 'filters': [{'url': 'https://filter', 'name': 'one',
                                                              'enabled': True, 'id': 7, 'rules_count': 999}],
                  'user_rules': ['new-rule']}
        projected = sync.project('filtering/status', source)
        self.assertNotIn('id', projected['filters'][0])
        self.assertEqual(projected['user_rules'], [sync.RULE, 'new-rule'])

    def test_empty_adguard_collections_may_be_null(self):
        self.assertEqual(sync.project('clients', {'clients': None}), {'clients': []})
        self.assertEqual(sync.project('rewrite/list', None), [])
        value = sync.project('filtering/status', {'filters': None, 'whitelist_filters': None, 'user_rules': None})
        self.assertEqual(value['filters'], [])
        self.assertEqual(value['whitelist_filters'], [])
        self.assertEqual(value['user_rules'], [sync.RULE])

    def test_reserved_rewrite_mutations_rejected(self):
        for route in ('rewrite/add', 'rewrite/delete'):
            with self.assertRaises(ValueError):
                sync.validate_write(route, {'domain': 'WHO.DESK-DNS.TEST.'})
        with self.assertRaises(ValueError):
            sync.validate_write('rewrite/update', {'target': {'domain': 'mine'},
                                                  'update': {'domain': 'health.desk-dns.test'}})
        with self.assertRaises(ValueError):
            sync.validate_write('rewrite/settings/update', {'enabled': False})

    def test_rule_updates_keep_private_probe(self):
        self.assertEqual(sync.validate_write('filtering/set_rules', {'rules': ['my-rule']})['rules'],
                         [sync.RULE, 'my-rule'])

    def test_host_local_or_account_mutation_rejected(self):
        with self.assertRaises(ValueError):
            sync.validate_write('dns_config', {'port': 53})
        with self.assertRaises(ValueError):
            sync.validate_write('profile/update', {'name': 'admin', 'password': 'oops'})
        self.assertEqual(sync.validate_write('profile/update', {'name': 'desk-sync', 'language': 'en'}),
                         {'name': 'desk-sync', 'language': 'en'})


class BoundaryTests(unittest.TestCase):
    def setUp(self):
        self.config = {'username': 'desk-sync', 'password': 'not-a-real-secret',
                       'vip': '192.168.1.250', 'node': 'deskb1x',
                       'management_nodes': ['127.0.0.1'], 'adguard': 'http://127.0.0.1:3001'}
        self.server = sync.Server(('127.0.0.1', 0), self.config)
        self.server.get_request = lambda: super(sync.Server, self.server).get_request()
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.url = 'http://127.0.0.1:' + str(self.server.server_port)
        self.owner = patch.object(sync, 'owns', return_value=False)
        self.owner.start()

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join()
        self.owner.stop()

    def request(self, route, method='GET', body=None, auth=True):
        headers = {'Authorization': sync.basic(self.config)} if auth else {}
        request = urllib.request.Request(self.url + route, headers=headers, method=method,
                                         data=None if body is None else sync.encoded(body))
        try:
            with urllib.request.urlopen(request, timeout=3) as response:
                return response.status, json.loads(response.read())
        except urllib.error.HTTPError as error:
            try:
                return error.code, json.loads(error.read())
            finally:
                error.close()

    def test_auth_and_unsupported_operations_are_closed(self):
        with patch.object(sync, 'fetch') as upstream:
            self.assertEqual(self.request('/control/status', auth=False)[0], 401)
            self.assertEqual(self.request('/control/querylog')[0], 404)
            self.assertEqual(self.request('/control/update', 'POST', {})[0], 405)
            upstream.assert_not_called()

    def test_filter_native_rejection_keeps_status_without_echoing_error_body(self):
        for code in (400,429,502):
            error=urllib.error.HTTPError('http://local',code,'secret upstream error',{},None)
            with patch.object(sync,'filter_write',side_effect=error):
                status,value=self.request('/desk-sync/filter/add_url','POST',{'url':'https://example.invalid/list'})
                self.assertEqual(status,code)
                self.assertEqual(value,{'error':'adguard_request_rejected'})
        with patch.object(sync,'filter_write',side_effect=sync.MaintenanceBusy):
            self.assertEqual(self.request('/desk-sync/filter/refresh','POST',{})[0],409)

    def test_source_is_read_only_and_requires_actual_vip_ownership(self):
        self.server.source = True  # Socket was already accepted locally; test the route boundary.
        with patch.object(sync, 'owns', return_value=True), patch.object(sync, 'fetch') as upstream:
            self.assertEqual(self.request('/control/dns_config', 'POST', {})[0], 409)
            upstream.assert_not_called()
        with patch.object(sync, 'owns', return_value=False):
            self.assertEqual(self.request('/control/status')[0], 409)
        self.server.source = False

    def test_new_owner_refuses_in_flight_replica_write(self):
        with patch.object(sync, 'owns', return_value=True), patch.object(sync, 'fetch') as upstream:
            self.assertEqual(self.request('/control/dns_config', 'POST', {'upstream_dns': ['1.1.1.1']})[0], 409)
            upstream.assert_not_called()

    def test_oversize_and_secret_error_body_are_not_forwarded(self):
        with patch.object(sync, 'fetch', side_effect=RuntimeError('password=SECRET')):
            status, value = self.request('/control/status')
            self.assertEqual(status, 503)
            self.assertNotIn('SECRET', json.dumps(value))
        with patch.object(sync, 'fetch') as upstream:
            connection = http.client.HTTPConnection('127.0.0.1', self.server.server_port, timeout=3)
            connection.putrequest('POST', '/control/filtering/set_rules')
            connection.putheader('Authorization', sync.basic(self.config))
            connection.putheader('Content-Length', str(sync.LIMIT + 1))
            connection.endheaders()
            self.assertEqual(connection.getresponse().status, 413)
            connection.close()
            upstream.assert_not_called()


class SessionTests(unittest.TestCase):
    def test_plaintext_success_is_ignored_for_plain_mutation_contract(self):
        import io
        config={'adguard':'http://127.0.0.1:3001','local_auth':False,'username':'test','password':'test'}
        class FakeOpener:
            def open(self,request,timeout):return io.BytesIO(b'OK 100 rules\n')
        with patch.object(sync.urllib.request,'build_opener',return_value=FakeOpener()):
            self.assertIsNone(sync.fetch(config['adguard']+'/control/filtering/add_url',config,'POST',{},return_mutation=False))
        class JsonOpener:
            def open(self,request,timeout):return io.BytesIO(b'{"updated":3}')
        with patch.object(sync.urllib.request,'build_opener',return_value=JsonOpener()):
            self.assertEqual(sync.fetch(config['adguard']+'/control/filtering/refresh',config,'POST',{},return_mutation=True),{'updated':3})

    def test_anonymous_local_fetch_skips_login_and_keeps_peer_auth(self):
        import io
        config = {'adguard': 'http://127.0.0.1:3001', 'local_auth': False,
                  'username': 'desk-sync', 'password': 'fixture-peer-password'}
        requests = []
        class FakeOpener:
            def open(self, request, timeout):
                requests.append(request)
                return io.BytesIO(b'{"ok":true}')
        with patch.object(sync.urllib.request, 'build_opener', return_value=FakeOpener()):
            self.assertEqual(sync.fetch(config['adguard'] + '/control/status', config), {'ok': True})
            sync.fetch(config['adguard'] + '/control/profile/update', config, 'PUT', {'name': 'desk-sync', 'theme': 'dark'})
            sync.fetch('https://192.168.1.250:3002/control/status', config)
        self.assertEqual(len(requests), 3)
        self.assertNotIn('Cookie', requests[0].headers)
        self.assertNotIn('Authorization', requests[0].headers)
        self.assertEqual(json.loads(requests[1].data), {'theme': 'dark'})
        self.assertEqual(requests[2].headers['Authorization'], sync.basic(config))

    def test_local_adguard_authenticates_once_and_refreshes_revoked_session(self):
        state = {'logins': 0, 'revoked': False}
        class FakeAdGuard(sync.http.server.BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass
            def do_POST(self):
                self.rfile.read(int(self.headers.get('Content-Length', '0')))
                state['logins'] += 1
                self.send_response(200)
                self.send_header('Set-Cookie', 'agh_session=session-' + str(state['logins']) + '; Path=/; HttpOnly')
                self.send_header('Content-Length', '0')
                self.end_headers()
            def do_GET(self):
                valid = self.headers.get('Cookie') == 'agh_session=session-' + str(state['logins'])
                if state['revoked']:
                    state['revoked'] = False
                    valid = False
                self.send_response(200 if valid else 401)
                body = b'{"ok":true}'
                self.send_header('Content-Length', str(len(body)))
                self.end_headers()
                self.wfile.write(body)
        server = sync.http.server.ThreadingHTTPServer(('127.0.0.1', 0), FakeAdGuard)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        config = {'adguard': 'http://127.0.0.1:' + str(server.server_port),
                  'username': 'desk-sync', 'password': 'fixture-only'}
        try:
            for unused in range(3):
                self.assertEqual(sync.fetch(config['adguard'] + '/control/status', config), {'ok': True})
            self.assertEqual(state['logins'], 1)
            state['revoked'] = True
            self.assertEqual(sync.fetch(config['adguard'] + '/control/status', config), {'ok': True})
            self.assertEqual(state['logins'], 2)
        finally:
            server.shutdown()
            server.server_close()
            thread.join()


class PullTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.config = {'node': 'deskb1x', 'nodes': ['deskb1x', 'deskb2x', 'deskb3x'],
                       'vip': '192.168.1.250', 'port': 3002, 'ca': '/unused',
                       'binary': '/unused', 'sync_config': '/unused',
                       'state': self.directory.name + '/status.json'}
        self.owner = patch.object(sync, 'owns', return_value=False)
        self.owner.start()
        self.context = patch.object(sync.ssl, 'create_default_context')
        self.context.start()

    def tearDown(self):
        self.context.stop()
        self.owner.stop()
        self.directory.cleanup()

    def test_vip_owner_never_syncs_to_itself(self):
        with patch.object(sync, 'owns', return_value=True), patch.object(sync, 'fetch') as remote:
            self.assertEqual(sync.pull(self.config), 0)
            remote.assert_not_called()

    def test_converged_policy_does_not_spawn_upstream(self):
        identity = {'node': 'deskb2x', 'owner': True}
        with patch.object(sync, 'fetch', return_value=identity), patch.object(sync, 'snapshot', return_value='same'), \
                patch.object(sync.subprocess, 'run') as run:
            self.assertEqual(sync.pull(self.config), 0)
            run.assert_not_called()
        self.assertEqual(json.loads(Path(self.config['state']).read_text())['state'], 'synced')

    def test_failed_sync_zero_exit_is_detected_and_next_pull_repairs(self):
        identity = {'node': 'deskb2x', 'owner': True}
        with patch.object(sync, 'fetch', return_value=identity), \
                patch.object(sync, 'snapshot', side_effect=['new', 'old', 'new', 'old']), \
                patch.object(sync.subprocess, 'run') as run:
            run.return_value.returncode = 0
            self.assertEqual(sync.pull(self.config), 1)
        with patch.object(sync, 'fetch', return_value=identity), \
                patch.object(sync, 'snapshot', side_effect=['new', 'old', 'new', 'new']), \
                patch.object(sync.subprocess, 'run') as run:
            run.return_value.returncode = 0
            self.assertEqual(sync.pull(self.config), 0)

    def test_source_failover_or_policy_edit_during_pull_is_not_success(self):
        for identity, hashes in (([{'node': 'deskb2x', 'owner': True}, {'node': 'deskb3x', 'owner': True}],
                                  ['new', 'old', 'new']),
                                 ([{'node': 'deskb2x', 'owner': True}] * 2, ['new', 'old', 'edited'])):
            with patch.object(sync, 'fetch', side_effect=identity), \
                    patch.object(sync, 'snapshot', side_effect=hashes), patch.object(sync.subprocess, 'run') as run:
                run.return_value.returncode = 0
                self.assertEqual(sync.pull(self.config), 1)


if __name__ == '__main__':
    unittest.main()
