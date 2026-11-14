"""Run the actual role on local aliases with filesystem remapping and systemd stubs."""
import copy
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

import yaml

ROOT=Path(__file__).resolve().parents[3]
ROLE=ROOT/'roles/desk_wifi_watch'
NODES=['deskb1x','deskb2x','deskb3x']


class Rollout(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory(prefix='desk-wifi-watch-test-')
        self.addCleanup(self.temp.cleanup)
        self.directory=Path(self.temp.name)
        self.play=yaml.safe_load((ROOT/'playbooks/node/wifi-watch.yml').read_text())[0]
        self.events=self.directory/'events.jsonl'

    def fixture(self, failure=None):
        self.assertEqual([self.play[k] for k in ('hosts','serial','order','any_errors_fatal')],
                         ['deskboxes',1,'inventory',True])
        self.assertEqual(self.play['roles'],['desk_wifi_watch'])
        self.assertNotIn('wifi-watch', (ROOT/'site.yml').read_text())
        target=self.directory/'role'
        target.mkdir()
        for sub in ('tasks','defaults','handlers','templates','files'):
            (target/sub).mkdir()
        stub=self.directory/'systemd.py'
        stub.write_text('''import json,sys
from pathlib import Path
root=Path(sys.argv[1]); node=sys.argv[2]; args=sys.argv[3:]
with (root/'events.jsonl').open('a') as f: f.write(json.dumps([node,*args])+'\\n')
if 'start' in args and 'desk-wifi-watch.service' in args:
    assert (root/node/'etc/desk-wifi-watch.json').is_file()
    assert (root/node/'etc/systemd/system/desk-wifi-watch.service').is_file()
    if node=='''+repr(failure)+''': sys.exit(1)
''')
        def transform(task):
            task=copy.deepcopy(task)
            for key in ('ansible.builtin.file','ansible.builtin.copy','ansible.builtin.template','ansible.builtin.stat'):
                if key in task:
                    for attr in ('path','dest'):
                        if attr in task[key]:
                            path=task[key][attr]
                            task[key][attr]=str(self.directory)+'/{{ inventory_hostname }}'+path
                    for attr in ('owner','group'): task[key].pop(attr,None)
            if 'ansible.builtin.systemd_service' in task:
                values=task.pop('ansible.builtin.systemd_service')
                task['ansible.builtin.command']={'argv':[sys.executable,str(stub),str(self.directory),'{{ inventory_hostname }}','systemd',json.dumps(values)]}
            if 'ansible.builtin.command' in task and task['ansible.builtin.command']['argv'][0]=='/usr/bin/systemctl':
                task['ansible.builtin.command']['argv']=[sys.executable,str(stub),str(self.directory),'{{ inventory_hostname }}',*task['ansible.builtin.command']['argv'][1:]]
            return task
        for sub in ('tasks','handlers'):
            for path in (ROLE/sub).glob('*.yml'):
                tasks=[transform(t) for t in yaml.safe_load(path.read_text())]
                (target/sub/path.name).write_text(yaml.safe_dump(tasks,sort_keys=False))
        for sub in ('defaults','templates','files'):
            for path in (ROLE/sub).iterdir():
                if not path.is_file(): continue
                (target/sub/path.name).write_bytes(path.read_bytes())
        play=copy.deepcopy(self.play)
        play['become']=False; play['roles']=[str(target)]
        play['post_tasks']=[transform(t) for t in play['post_tasks']]
        # Real deployment assumes /etc/systemd/system exists on Debian.
        for node in NODES: (self.directory/node/'etc/systemd/system').mkdir(parents=True)
        (self.directory/'play.yml').write_text(yaml.safe_dump([play],sort_keys=False))
        (self.directory/'inventory.ini').write_text('[deskboxes]\n'+''.join(
            f'{node} ansible_connection=local ansible_python_interpreter={sys.executable} ansible_host=192.168.1.{101+i} cluster_ip=10.10.10.{11+i}\n'
            for i,node in enumerate(NODES)))

    def run_play(self,state='present'):
        env=dict(os.environ,ANSIBLE_LOCAL_TEMP=str(self.directory/'ansible-local'),
                 ANSIBLE_REMOTE_TEMP=str(self.directory/'ansible-remote'),ANSIBLE_NOCOLOR='1')
        return subprocess.run(['ansible-playbook','-i',str(self.directory/'inventory.ini'),
                               str(self.directory/'play.yml'),'-e','desk_wifi_watch_state='+state],
                              cwd=ROOT,env=env,capture_output=True,text=True,timeout=120)

    def read_events(self):
        return [json.loads(line) for line in self.events.read_text().splitlines()]

    def test_serial_present_idempotent_and_absent(self):
        self.fixture()
        result=self.run_play()
        self.assertEqual(result.returncode,0,result.stdout+result.stderr)
        events=self.read_events()
        self.assertEqual([e[0] for e in events],[n for n in NODES for _ in range(4)])
        for node in NODES:
            config=json.loads((self.directory/node/'etc/desk-wifi-watch.json').read_text())
            self.assertEqual(len(config['management_peers']),2)
            self.assertNotIn('192.168.1.'+str(101+NODES.index(node)),config['management_peers'])
        result=self.run_play()
        self.assertEqual(result.returncode,0,result.stdout+result.stderr)
        for node in NODES: self.assertRegex(result.stdout,node+r'\s+:.*changed=1')  # enable/start is represented by a command stub
        for node in NODES:
            for directory in ('var/lib/desk-wifi-watch','run/desk-wifi-watch'):
                path=self.directory/node/directory; path.mkdir(parents=True)
                (path/'state.json').write_text('{}')
        before=len(self.read_events())
        result=self.run_play('absent')
        self.assertEqual(result.returncode,0,result.stdout+result.stderr)
        events=self.read_events()[before:]
        self.assertEqual([e[0] for e in events],[n for n in NODES for _ in range(3)])
        for node in NODES:
            self.assertIn('stopped',events[NODES.index(node)*3][2])
            for path in ('etc/systemd/system/desk-wifi-watch.timer','etc/systemd/system/desk-wifi-watch.service',
                         'etc/desk-wifi-watch.json','usr/local/libexec/desk-wifi-watch.py',
                         'var/lib/desk-wifi-watch','run/desk-wifi-watch'):
                self.assertFalse((self.directory/node/path).exists())
        result=self.run_play('absent')
        self.assertEqual(result.returncode,0,result.stdout+result.stderr)
        self.assertEqual(self.read_events()[before:],events)

    def test_failed_trial_stops_before_third_node(self):
        self.fixture('deskb2x')
        result=self.run_play()
        self.assertNotEqual(result.returncode,0)
        events=self.read_events()
        self.assertIn(['deskb2x','start','desk-wifi-watch.service'],events)
        self.assertFalse(any(e[0]=='deskb3x' for e in events))
        self.assertFalse((self.directory/'deskb3x/etc/desk-wifi-watch.json').exists())

    def test_unit_limits_and_absent_stop_before_delete(self):
        unit=(ROLE/'templates/desk-wifi-watch.service.j2').read_text()
        for setting in ('Type=oneshot','TimeoutStartSec=20s','Nice=19','IOSchedulingClass=idle',
                        'CPUQuota=30%','MemoryMax=32M','TasksMax=32','PrivateTmp=yes','ProtectSystem=strict',
                        'ReadWritePaths=/var/lib/desk-wifi-watch /run/desk-wifi-watch','NoNewPrivileges=yes',
                        'CapabilityBoundingSet=CAP_NET_RAW','User=root','SyslogIdentifier=desk-wifi-watch',
                        'RuntimeDirectoryPreserve=yes','StateDirectoryMode=0700','RuntimeDirectoryMode=0700'):
            self.assertIn(setting+'\n',unit)
        timer=(ROLE/'templates/desk-wifi-watch.timer.j2').read_text()
        for setting in ('OnBootSec=2min','OnUnitActiveSec=30s','AccuracySec=5s'): self.assertIn(setting+'\n',timer)
        absent=yaml.safe_load((ROLE/'tasks/absent.yml').read_text())
        self.assertEqual(absent[1]['ansible.builtin.systemd_service']['state'],'stopped')
        self.assertFalse(absent[1]['ansible.builtin.systemd_service']['enabled'])
        self.assertEqual(absent[2]['ansible.builtin.file']['state'],'absent')
        source=(ROLE/'files/desk-wifi-watch.py').read_text()
        self.assertIn('fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)',source)
        self.assertNotIn('desk_wifi_watch_recovery',source)
        self.assertFalse(yaml.safe_load((ROLE/'defaults/main.yml').read_text())['desk_wifi_watch_recovery'])


if __name__=='__main__': unittest.main()
