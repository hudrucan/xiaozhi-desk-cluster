"""Offline journal rollout: real modules, stubbed systemd/journalctl, no hosts."""
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

    def fixture(self, failed_service=None, missing_flush=False, preinstalled=False, oversized=False, lost=False, uncovered=False):
        play = copy.deepcopy(self.play)
        self.assertEqual((play['hosts'], play['serial'], play['order'], play['any_errors_fatal']),
                         ('deskboxes', 1, 'inventory', True))
        self.assertFalse(play['gather_facts'])
        tasks = play['tasks']
        line = next(t['ansible.builtin.lineinfile'] for t in tasks if 'ansible.builtin.lineinfile' in t)
        self.assertEqual(line, dict(path='/etc/default/armbian-ramlog', regexp='^ENABLED=',
                                    line='ENABLED=false', create=False))
        install = next(t for t in tasks if 'content' in t.get('ansible.builtin.copy', {}))
        self.assertEqual(install['ansible.builtin.copy']['content'], '[Journal]\nSystemMaxUse=200M\n')
        handler = play['handlers'][0]
        self.assertEqual(handler['ansible.builtin.systemd_service'], {'name':'systemd-journald', 'state':'restarted'})
        self.assertEqual(install['notify'], handler['name'])
        flush = next(t for t in tasks if t.get('ansible.builtin.command', {}).get('argv') == ['/usr/bin/journalctl', '--flush'])
        self.assertEqual(flush['when'], 'not desk_journal_flushed.stat.exists')
        self.assertNotIn('changed_when', flush)
        check = next(t for t in tasks if t.get('ansible.builtin.command', {}).get('argv', [])[:1] == ['/usr/bin/systemctl'])
        self.assertEqual(check['loop'], SERVICES)
        self.assertEqual(check['ansible.builtin.command']['argv'], ['/usr/bin/systemctl', 'is-active', '--quiet', '{{ item }}'])
        measure = next(t for t in tasks if t.get('register') == 'desk_journal_measurement')
        count = next(t for t in tasks if t.get('register') == 'desk_journal_count_after')
        self.assertFalse(measure['changed_when'])
        self.assertFalse(count['changed_when'])
        # Remap only paths and external actions; execute the real Ansible flow.
        play['become'] = False
        for task in tasks:
            for module in ('stat', 'file', 'copy', 'lineinfile'):
                options = task.get('ansible.builtin.'+module)
                if options is None:
                    continue
                key = 'dest' if module == 'copy' else 'path'
                options[key] = str(self.directory)+'/{{ inventory_hostname }}'+options[key]
                if module == 'copy' and 'src' in options:
                    options['src'] = str(ROOT/'playbooks/node'/options['src'])
                    options.pop('validate', None)
                options.pop('owner', None)
                options.pop('group', None)
        for node, enabled in zip(NODES, ('true', 'false', None)):
            root = self.directory/node
            (root/'run/systemd/journal').mkdir(parents=True)
            (root/'usr/local/libexec').mkdir(parents=True)
            (root/'etc/logrotate.d').mkdir(parents=True)
            if enabled is not None:
                config = root/'etc/default/armbian-ramlog'
                config.parent.mkdir(parents=True)
                config.write_text('# preserved configuration\nENABLED='+enabled+'\nSIZE=50M\n')
            if preinstalled:
                dropin = root/'etc/systemd/journald.conf.d/90-desk-journal.conf'
                dropin.parent.mkdir(parents=True)
                dropin.write_text('[Journal]\nSystemMaxUse=200M\n')
                dropin.chmod(0o644)
        stub = self.directory/'systemd_stub.py'
        stub.write_text(f'''import json,sys
from pathlib import Path
root=Path({str(self.directory)!r})
action,node,*units=sys.argv[1:]
with (root/'events.jsonl').open('a') as log: log.write(json.dumps([action,node,*units])+'\\n')
node_root=root/node
if action=='inspect':
    print(json.dumps(dict(entries=100, disk_bytes={170*1024*1024 if oversized else 1024}, ram_bytes=1024, safe_bytes=160*1024*1024, rotation_rc=0, rotation_uncovered={['/var/log/haproxy.log'] if uncovered else []!r})))
elif action=='count':
    print({99 if lost else 100})
elif action=='flush':
    # The user's safety amendment requires saving RAM before disabling ramlog.
    config=node_root/'etc/default/armbian-ramlog'
    if node=='deskb1x': assert 'ENABLED=true' in config.read_text()
    if not {missing_flush!r}: (node_root/'run/systemd/journal/flushed').touch()
elif action=='cleanup':
    config=node_root/'etc/default/armbian-ramlog'
    assert not config.exists() or 'ENABLED=true' not in config.read_text()
    assert (node_root/'run/systemd/journal/flushed').exists()
    print('[]')
elif action=='restart':
    assert (node_root/'run/systemd/journal/flushed').exists()
    config=node_root/'etc/default/armbian-ramlog'
    assert not config.exists() or 'ENABLED=true' not in config.read_text()
elif action=='check':
    assert (node_root/'run/systemd/journal/flushed').exists(), 'Checks ran before persistent flush'
    assert (node_root/'etc/systemd/journald.conf.d/90-desk-journal.conf').read_text()=='[Journal]\\nSystemMaxUse=200M\\n'
    if (node,units[0])=={failed_service!r}: raise SystemExit(3)
''')
        cleanup = next(t for t in tasks if t.get('register') == 'desk_journal_cleanup')
        for task, action in ((measure, 'inspect'), (count, 'count'), (cleanup, 'cleanup')):
            task['ansible.builtin.command']['argv'] = [sys.executable, str(stub), action, '{{ inventory_hostname }}']
        handler.pop('ansible.builtin.systemd_service')
        handler['ansible.builtin.command'] = {'argv':[sys.executable, str(stub), 'restart', '{{ inventory_hostname }}']}
        flush['ansible.builtin.command']['argv'] = [sys.executable, str(stub), 'flush', '{{ inventory_hostname }}']
        check['ansible.builtin.command']['argv'] = [sys.executable, str(stub), 'check', '{{ inventory_hostname }}', '{{ item }}']
        check['delay'] = 0
        check['retries'] = 1
        (self.directory/'journal.yml').write_text(yaml.safe_dump([play], sort_keys=False))
        (self.directory/'inventory.ini').write_text('[deskboxes]\n'+''.join(f'{n} ansible_connection=local ansible_python_interpreter={sys.executable}\n' for n in NODES))

    def run_play(self, check=False):
        env = dict(os.environ, ANSIBLE_LOCAL_TEMP=str(self.directory/'ansible-local'),
                   ANSIBLE_REMOTE_TEMP=str(self.directory/'ansible-remote'), ANSIBLE_NOCOLOR='1')
        return subprocess.run(['ansible-playbook', '-i', str(self.directory/'inventory.ini'),
                               str(self.directory/'journal.yml'), *(['--check', '--diff'] if check else [])],
                              cwd=ROOT, env=env, capture_output=True, text=True, timeout=120)

    def read_events(self):
        return [json.loads(line) for line in self.events.read_text().splitlines()] if self.events.exists() else []

    def test_serial_apply_flushes_once_and_unchanged_rerun_does_not(self):
        self.fixture()
        first = self.run_play()
        self.assertEqual(first.returncode, 0, first.stdout+first.stderr)
        expected = [event for node in NODES for event in [['inspect',node], ['flush',node], ['count',node], ['cleanup',node], ['restart',node], *[['check',node,unit] for unit in SERVICES]]]
        self.assertEqual(self.read_events(), expected)
        for node in NODES[:2]:
            self.assertEqual((self.directory/node/'etc/default/armbian-ramlog').read_text(),
                             '# preserved configuration\nENABLED=false\nSIZE=50M\n')
        self.assertFalse((self.directory/NODES[2]/'etc/default/armbian-ramlog').exists())
        second = self.run_play()
        self.assertEqual(second.returncode, 0, second.stdout+second.stderr)
        self.assertEqual(self.read_events(), expected+[event for node in NODES for event in [['inspect',node], ['count',node], ['cleanup',node], *[['check',node,unit] for unit in SERVICES]]])
        for node in NODES:
            self.assertRegex(second.stdout, node+r'\s+:.*changed=0')
        self.assertFalse(list(self.directory.glob('*/etc/default/armbian-ramlog.*')))

    def test_one_inactive_service_stops_before_next_node(self):
        self.fixture(('deskb2x','keepalived'))
        result = self.run_play()
        self.assertNotEqual(result.returncode, 0)
        events = self.read_events()
        self.assertIn(['check','deskb2x','keepalived'], events)
        self.assertFalse(any(event[1]=='deskb3x' for event in events))

    def test_missing_flag_after_successful_command_stops_rollout(self):
        self.fixture(missing_flush=True, preinstalled=True)
        result = self.run_play()
        self.assertNotEqual(result.returncode, 0, result.stdout)
        self.assertEqual(self.read_events(), [['inspect','deskb1x'], ['flush','deskb1x']])

    def test_check_diff_is_local_and_does_not_flush_or_write(self):
        self.fixture(preinstalled=True)
        before = {str(p):p.read_bytes() for p in self.directory.glob('deskb*/etc/**/*') if p.is_file()}
        result = self.run_play(check=True)
        self.assertEqual(result.returncode, 0, result.stdout+result.stderr)
        self.assertIn('+ENABLED=false', result.stdout)
        self.assertEqual(self.read_events(), [event for node in NODES for event in [['inspect',node], ['count',node]]])
        self.assertEqual(before, {str(p):p.read_bytes() for p in self.directory.glob('deskb*/etc/**/*') if p.is_file()})
        self.assertFalse(list(self.directory.glob('deskb*/run/systemd/journal/flushed')))


    def test_oversized_journal_stops_before_any_changes(self):
        self.fixture(oversized=True)
        result = self.run_play()
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(self.read_events(), [['inspect','deskb1x']])
        self.assertIn('ENABLED=true', (self.directory/'deskb1x/etc/default/armbian-ramlog').read_text())
        self.assertFalse((self.directory/'deskb1x/etc/systemd').exists())

    def test_entry_loss_stops_before_disabling_ramlog(self):
        self.fixture(lost=True)
        result = self.run_play()
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(self.read_events(), [['inspect','deskb1x'], ['flush','deskb1x'], ['count','deskb1x']])
        self.assertIn('ENABLED=true', (self.directory/'deskb1x/etc/default/armbian-ramlog').read_text())

    def test_uncovered_log_stops_before_flush_or_changes(self):
        self.fixture(uncovered=True)
        result = self.run_play()
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(self.read_events(), [['inspect','deskb1x']])


if __name__ == '__main__':
    unittest.main()
