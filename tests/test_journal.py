"""Offline journal rollout: real file copies, stubbed systemd, no managed hosts."""
import copy
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

import yaml

ROOT = Path(__file__).resolve().parents[1]
SERVICES = ['systemd-journald', 'haproxy', 'keepalived', 'desk-caddy']
NODES = ['deskb1x', 'deskb2x', 'deskb3x']


class JournalTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='desk-journal-test-')
        self.addCleanup(self.temp.cleanup)
        self.directory = Path(self.temp.name)
        self.play = yaml.safe_load((ROOT/'playbooks/node/journal.yml').read_text())[0]
        self.events = self.directory/'events.jsonl'

    def fixture(self, failed_service=None):
        play = copy.deepcopy(self.play)
        self.assertEqual((play['hosts'], play['serial'], play['order'], play['any_errors_fatal']),
                         ('deskboxes', 1, 'inventory', True))
        self.assertFalse(play['gather_facts'])
        self.assertEqual([next(k for k in task if k.startswith('ansible.builtin.')) for task in play['tasks']],
                         ['ansible.builtin.file', 'ansible.builtin.copy', 'ansible.builtin.meta', 'ansible.builtin.command'])
        directory, install, flush, check = play['tasks']
        self.assertEqual(directory['ansible.builtin.file']['path'], '/etc/systemd/journald.conf.d')
        self.assertEqual(install['ansible.builtin.copy']['dest'], '/etc/systemd/journald.conf.d/90-desk-journal.conf')
        self.assertEqual(install['ansible.builtin.copy']['content'], '[Journal]\nSystemMaxUse=200M\n')
        self.assertEqual(flush['ansible.builtin.meta'], 'flush_handlers')
        self.assertEqual(check['ansible.builtin.command']['argv'], ['/usr/bin/systemctl', 'is-active', '--quiet', '{{ item }}'])
        self.assertEqual(check['loop'], SERVICES)
        self.assertEqual(len(play['handlers']), 1)
        handler = play['handlers'][0]
        self.assertEqual(handler['ansible.builtin.systemd_service'], {'name':'systemd-journald', 'state':'restarted'})
        self.assertEqual(install['notify'], handler['name'])
        play['become'] = False
        for task, module, field, suffix in [(directory, 'ansible.builtin.file', 'path', ''),
                                           (install, 'ansible.builtin.copy', 'dest', '/90-desk-journal.conf')]:
            task[module][field] = str(self.directory)+'/{{ inventory_hostname }}/journald.conf.d'+suffix
            task[module].pop('owner'); task[module].pop('group')
        stub = self.directory/'systemd_stub.py'
        stub.write_text(f'''import json,sys
from pathlib import Path
root=Path({str(self.directory)!r})
action,node,*units=sys.argv[1:]
with (root/'events.jsonl').open('a') as log: log.write(json.dumps([action,node,*units])+'\\n')
marker=root/(node+'-restarted')
if action=='restart': marker.touch()
else:
    assert marker.exists(), 'Service checks ran before journal restart'
    assert (root/node/'journald.conf.d/90-desk-journal.conf').read_text()=='[Journal]\\nSystemMaxUse=200M\\n'
    if (node,units[0])=={failed_service!r}: raise SystemExit(3)
''')
        handler.pop('ansible.builtin.systemd_service')
        handler['ansible.builtin.command'] = {'argv':[sys.executable, str(stub), 'restart', '{{ inventory_hostname }}']}
        check['ansible.builtin.command']['argv'] = [sys.executable, str(stub), 'check', '{{ inventory_hostname }}', '{{ item }}']
        check['delay'] = 0; check['retries'] = 1
        (self.directory/'journal.yml').write_text(yaml.safe_dump([play], sort_keys=False))
        (self.directory/'inventory.ini').write_text('[deskboxes]\n'+''.join(f'{n} ansible_connection=local ansible_python_interpreter={sys.executable}\n' for n in NODES))

    def run_play(self):
        env = dict(os.environ, ANSIBLE_LOCAL_TEMP=str(self.directory/'ansible-local'),
                   ANSIBLE_REMOTE_TEMP=str(self.directory/'ansible-remote'), ANSIBLE_NOCOLOR='1')
        result = subprocess.run(['ansible-playbook', '-i', str(self.directory/'inventory.ini'),
                                 str(self.directory/'journal.yml')], cwd=ROOT, env=env,
                                capture_output=True, text=True, timeout=90)
        return result

    def read_events(self):
        return [json.loads(line) for line in self.events.read_text().splitlines()]

    def test_serial_apply_restarts_once_and_unchanged_rerun_does_not(self):
        self.fixture()
        first = self.run_play()
        self.assertEqual(first.returncode, 0, first.stdout+first.stderr)
        expected = [event for node in NODES for event in [['restart',node], *[['check',node,unit] for unit in SERVICES]]]
        self.assertEqual(self.read_events(), expected)
        second = self.run_play()
        self.assertEqual(second.returncode, 0, second.stdout+second.stderr)
        self.assertEqual(self.read_events(), expected+[['check',node,unit] for node in NODES for unit in SERVICES])
        for node in NODES:
            self.assertRegex(second.stdout, node+r'\s+:.*changed=0')

    def test_one_inactive_service_stops_before_next_node(self):
        self.fixture(('deskb2x','keepalived'))
        result = self.run_play()
        self.assertNotEqual(result.returncode, 0)
        events = self.read_events()
        self.assertIn(['check','deskb2x','keepalived'], events)
        self.assertFalse(any(event[1]=='deskb3x' for event in events))
        self.assertFalse((self.directory/'deskb3x').exists())


if __name__ == '__main__':
    unittest.main()
