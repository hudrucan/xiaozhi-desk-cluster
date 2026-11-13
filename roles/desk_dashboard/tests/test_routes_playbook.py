"""Offline route-only deployment contract and local HAProxy probe protocol."""
import contextlib
import io
import json
from pathlib import Path
import subprocess
import unittest
from unittest.mock import patch

from jinja2.nativetypes import NativeEnvironment
import yaml

ROOT = Path(__file__).resolve().parents[3]
SHARED = ROOT / 'playbooks/dashboard/tasks/dashboard-routes.yml'
PLAY = ROOT / 'playbooks/dashboard/dashboard-routes.yml'


class RoutesPlaybook(unittest.TestCase):
    def test_original_reload_argv_is_preserved_for_both_page_states(self):
        # Immutable pre-refactor HEAD keeps this regression valid after committing.
        before = subprocess.check_output(['git', 'show', 'ec8cb9f243e28d9b888d32593b1019437620f1d8:playbooks/dashboard/desk-dashboard.yml'], cwd=ROOT, text=True)
        old_play = yaml.safe_load(before)[0]
        old_block = next(t['block'] for t in old_play['tasks'] if 'block' in t)
        original = next(t for t in old_block if t['name'] == 'Validate HTTP and MQTT and gracefully reload HAProxy')
        shared = yaml.safe_load(SHARED.read_text())
        transaction = shared[-1]
        validate, reload = transaction['block'][-2:]
        self.assertEqual(reload['ansible.builtin.command']['argv'], original['ansible.builtin.command']['argv'])
        self.assertEqual(reload['changed_when'], original['changed_when'])
        env = NativeEnvironment()
        for changed in (False, True):
            expected = env.from_string(original['ansible.builtin.command']['argv']).render(dashboard_page={'changed': changed})
            actual = env.from_string(reload['ansible.builtin.command']['argv']).render(dashboard_page={'changed': changed})
            self.assertEqual(actual, expected)
            self.assertEqual('--reload-page' in actual, changed)
            self.assertEqual(validate['ansible.builtin.command']['argv'], expected[:8] + ['--validate-only'])
        # No other part of the legacy playbook is refactored.
        start = before.index('        - name: Install the complete route validator\n')
        end = before.index('        - name: Verify unchanged protected services', start)
        expected_file = before[:start] + '        - name: Install and validate the dashboard routes\n          ansible.builtin.import_tasks: tasks/dashboard-routes.yml\n' + before[end:]
        self.assertEqual((ROOT / 'playbooks/dashboard/desk-dashboard.yml').read_text(), expected_file)
        rescue = transaction['rescue']
        self.assertEqual(rescue[-2]['ansible.builtin.systemd_service']['state'], 'reloaded')
        self.assertIn('-c', rescue[-3]['ansible.builtin.command']['argv'])
        self.assertEqual(rescue[-3]['ansible.builtin.command']['argv'][-1], '/etc/haproxy/xiaozhi-mqtt.cfg')
        self.assertIn('ansible.builtin.fail', rescue[-1])

    def test_route_only_play_is_ordered_and_imports_the_shared_transaction(self):
        play = yaml.safe_load(PLAY.read_text())[0]
        self.assertEqual(play['serial'], 1)
        self.assertEqual(play['order'], 'inventory')
        self.assertTrue(play['any_errors_fatal'])
        self.assertFalse(play['gather_facts'])
        tasks = play['tasks']
        self.assertIn("ansible_play_hosts_all == ['deskb1x', 'deskb2x', 'deskb3x']", tasks[0]['ansible.builtin.assert']['that'])
        imports = [t for t in tasks if 'ansible.builtin.import_tasks' in t]
        self.assertEqual([t['ansible.builtin.import_tasks'] for t in imports], ['tasks/dashboard-routes.yml'])
        self.assertEqual(len(play['vars']['dashboard_route_paths']), 10)
        self.assertEqual(play['vars']['dashboard_route_paths'][:3], ['/', '/api/cluster/status', '/api/cluster/panel'])
        self.assertEqual(len(set(play['vars']['dashboard_route_paths'])), 10)
        self.assertTrue(play['vars']['dashboard_page']['changed'])
        for task in tasks:
            self.assertNotIn('ansible.builtin.include_role', task)
            self.assertNotIn('ansible.builtin.systemd_service', task)

    def test_local_probe_sends_v2_and_stops_on_bad_http_status(self):
        play = yaml.safe_load(PLAY.read_text())[0]
        script = play['tasks'][-1]['ansible.builtin.command']['argv'][2]
        paths = play['vars']['dashboard_route_paths']
        peers = []
        class Peer:
            def __init__(self, *args): self.sent = []; peers.append(self)
            def __enter__(self): return self
            def __exit__(self, *args): pass
            def settimeout(self, value): pass
            def connect(self, path): self.path = path
            def sendall(self, data): self.sent.append(data)
        class Response:
            status = 200
            def __init__(self, peer): pass
            def begin(self): pass
            def read(self): return b'ok'
        with patch('socket.socket', Peer), patch('http.client.HTTPResponse', Response), patch('sys.argv', ['probe', json.dumps(paths)]), contextlib.redirect_stdout(io.StringIO()):
            exec(compile(script, str(PLAY), 'exec'), {})
            self.assertEqual(len(peers), 10)
            for peer, path in zip(peers, paths):
                self.assertEqual(peer.path, '/run/haproxy/desk-https-cluster.sock')
                self.assertEqual(peer.sent[0], b'\r\n\r\n\x00\r\nQUIT\n\x21\x11\x00\x0c' + b'\x7f\x00\x00\x01' * 2 + b'\xc0\x00\x00\x50')
                self.assertIn(('GET ' + path + ' HTTP/1.1\r\n').encode(), peer.sent[1])
            Response.status = 404
            with self.assertRaisesRegex(SystemExit, 'expected 200, got 404'):
                exec(compile(script, str(PLAY), 'exec'), {})
