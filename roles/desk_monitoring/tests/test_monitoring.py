import importlib.util
from pathlib import Path
import unittest

ROOT = Path(__file__).resolve().parents[1]


def load(name):
    spec = importlib.util.spec_from_file_location(name, ROOT / 'files' / (name + '.py'))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


bootstrap = load('bootstrap')
probe = load('monitoring_probe')
proxy = load('install_proxy')
NODES = {'deskb1x': '10.10.10.11', 'deskb2x': '10.10.10.12', 'deskb3x': '10.10.10.13'}
CREDENTIALS = {'email': 'admin@desk.cluster', 'password': 'dummy-test-password-of-sufficient-length'}


class FakeHub:
    def __init__(self):
        self.token = None
        self.first = True
        self.records = []
        self.writes = []

    def call(self, path, body=None):
        if path.endswith('first-run'):
            return {'firstRun': self.first}
        if path.endswith('create-user'):
            self.first = False
            self.writes.append(path)
            return {}
        if path.endswith('auth-refresh'):
            raise bootstrap.urllib.error.HTTPError(path,401,'fixture auth required',{},None)
        if path.endswith('auth-with-password'):
            return {'token': 'fake-token', 'record': {'id': 'user'}}
        if path.endswith('/info'):
            return {'key': 'ssh-ed25519 AAAATESTKEY'}
        if path.endswith('systems/records'):
            self.writes.append(path)
            record = dict(body, id=str(len(self.records)))
            self.records.append(record)
            return record
        if 'systems/records?' in path:
            return {'items': self.records}
        raise AssertionError(path)


class MonitoringTests(unittest.TestCase):
    def test_bootstrap_is_idempotent_and_never_resets_passwords(self):
        hub = FakeHub()
        first = bootstrap.reconcile(hub, NODES, CREDENTIALS)
        writes = len(hub.writes)
        second = bootstrap.reconcile(hub, NODES, CREDENTIALS)
        self.assertTrue(first['changed'])
        self.assertFalse(second['changed'])
        self.assertEqual(writes, len(hub.writes))
        self.assertEqual({item['host'] for item in hub.records}, set(NODES.values()))

    def test_auto_login_rerun_retains_existing_account_without_password_login(self):
        class NativeHub(FakeHub):
            def call(self,path,body=None):
                if path.endswith('auth-refresh'):
                    return {'token':'fake-native','record':{'id':'existing-user'}}
                if path.endswith('auth-with-password'):
                    raise AssertionError('Existing auto-login must not use bootstrap credentials')
                return super().call(path,body)
        hub=NativeHub();hub.first=False
        bootstrap.reconcile(hub,NODES,CREDENTIALS)
        self.assertTrue(all(r['users']==['existing-user'] for r in hub.records))
        self.assertFalse(any(p.endswith('create-user') for p in hub.writes))

    def test_existing_membership_drift_rejected_without_mutation(self):
        hub = FakeHub()
        bootstrap.reconcile(hub, NODES, CREDENTIALS)
        hub.records[0]['host'] = '192.168.1.1'
        writes = len(hub.writes)
        with self.assertRaises(ValueError):
            bootstrap.reconcile(hub, NODES, CREDENTIALS)
        self.assertEqual(writes, len(hub.writes))

    def test_endpoint_input_is_restricted_to_complete_private_membership(self):
        bootstrap.validate({'nodes': NODES, 'credentials': CREDENTIALS}, '10.10.10.11')
        with self.assertRaises(ValueError):
            bootstrap.validate({'nodes': dict(NODES, deskb1x='127.0.0.1'), 'credentials': CREDENTIALS}, '127.0.0.1')

    def test_proxy_is_idempotent_and_preserves_non_monitoring_routes(self):
        current = 'frontend xiaozhi_settings\n    http-request deny deny_status 404 if { path_beg /internal/ }\n    default_backend xiaozhi_control_planes\nbackend xiaozhi_control_planes\n    server deskb1x 10.10.10.11:8004 check\n'
        backend = 'backend desk_monitoring_hubs\n    server deskb1x 10.10.10.11:8090 check\n'
        updated = proxy.candidate(current, backend)
        self.assertEqual(proxy.candidate(updated, backend), updated)
        self.assertEqual(probe.normalize_config(updated), current)
        self.assertLess(updated.index('deny deny_status'), updated.index('use_backend desk_monitoring'))

    def test_only_monitoring_haproxy_changes_are_allowed(self):
        snapshot = {'units': {'keepalived': {'MainPID': '1', 'ActiveState': 'active'}}, 'digests': {'/etc/haproxy/haproxy.cfg': 'old', '/etc/resolv.conf': 'same'}, 'haproxy_base': 'same'}
        new = {**snapshot, 'digests': {**snapshot['digests'], '/etc/haproxy/haproxy.cfg': 'new'}}
        probe.guard(snapshot, new, after_proxy=True)
        with self.assertRaises(ValueError):
            probe.guard(snapshot, new)
        for bad in ({**new, 'haproxy_base': 'changed'}, {**new, 'units': {}}):
            with self.assertRaises(ValueError):
                probe.guard(snapshot, bad, after_proxy=True)


if __name__ == '__main__':
    unittest.main()
