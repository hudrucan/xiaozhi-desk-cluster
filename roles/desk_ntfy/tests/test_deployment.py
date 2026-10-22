"""Execute the real play conditions with local peers; never contact the cluster."""
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest

import yaml

ROOT = Path(__file__).resolve().parents[3]
ANSIBLE = shutil.which('ansible-playbook') or str(Path(sys.executable).with_name('ansible-playbook'))
PEER = '''import fcntl,json,sys
from pathlib import Path
action,node,file=sys.argv[1:]
p=Path(file)
with p.with_suffix('.lock').open('w') as lock:
 fcntl.flock(lock,fcntl.LOCK_EX)
 state=json.loads(p.read_text())
 running=state['running']
 if action=='recover':
  if node in running:sys.exit(0)  # systemd state=started leaves active peers untouched.
  running.append(node)
 if action=='ready':
  assert len(running)>=2 and node in running,'quorum must start before readiness'
 if action=='start':
  if state['rolling']:assert len([n for n in running if n!=node])>=2
  if node not in running:running.append(node)
 state['events'].append([action,node])
 p.write_text(json.dumps(state))
'''


class Deployment(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.state = self.root / 'state.json'
        self.peer = self.root / 'peer.py';self.peer.write_text(PEER)
        self.members = ['deskb1x','deskb2x','deskb3x']
        (self.root/'inventory').write_text('[deskboxes]\n' + '\n'.join(self.members) + '\n[deskboxes:vars]\nansible_connection=local\nansible_python_interpreter=' + sys.executable + '\n')
        self.environment = dict(os.environ, ANSIBLE_LOCAL_TEMP=str(self.root/'local'), ANSIBLE_NOCOLOR='1', ANSIBLE_CONFIG=str(ROOT/'ansible.cfg'), ANSIBLE_ROLES_PATH=str(self.root/'roles'))

    def command(self, action):
        return {'ansible.builtin.command': {'argv': [sys.executable,str(self.peer),action,'{{ inventory_hostname }}',str(self.state)]}}

    def run_play(self, plays):
        file=self.root/'play.yml';file.write_text(yaml.safe_dump(plays,sort_keys=False))
        result=subprocess.run([ANSIBLE,'-i',str(self.root/'inventory'),str(file)],cwd=ROOT,env=self.environment,text=True,stdout=subprocess.PIPE,stderr=subprocess.STDOUT,timeout=120)
        self.assertEqual(result.returncode,0,result.stdout)
        return json.loads(self.state.read_text())['events']

    def recover(self, journal_exists, running):
        journal=self.root/'journal.enc';identity=b'unchanged encrypted journal, seed and token identities'
        if journal_exists:journal.write_bytes(identity)
        self.state.write_text(json.dumps({'running':running,'rolling':journal_exists,'events':[]}))
        tasks=self.root/'roles/desk_ntfy/tasks';tasks.mkdir(parents=True)
        (tasks/'main.yml').write_text(yaml.safe_dump([self.command('start')]))
        (tasks/'ready.yml').write_text(yaml.safe_dump([self.command('ready')]))
        (tasks/'ingress.yml').write_text(yaml.safe_dump([self.command('ready')]))
        plays=yaml.safe_load((ROOT/'playbooks/notifications/ntfy.yml').read_text())[:4]
        for play in plays:play['become']=False;play['gather_facts']=False;play.pop('gather_subset',None)
        for task in plays[0]['tasks']:
            if 'ansible.builtin.stat' in task:task['ansible.builtin.stat']['path']=str(journal)
            if 'ansible.builtin.systemd_service' in task:
                self.assertEqual(task.pop('ansible.builtin.systemd_service'),{'name':'desk-ntfy','state':'started'})
                task.update(self.command('recover'))
        events=self.run_play(plays)
        if journal_exists:self.assertEqual(journal.read_bytes(),identity)
        return events

    def test_full_stop_with_existing_journal_starts_all_before_waiting(self):
        events=self.recover(True,[])
        self.assertEqual({n for action,n in events[:3] if action=='recover'},set(self.members))
        self.assertEqual({n for action,n in events[3:6] if action=='ready'},set(self.members))
        self.assertEqual(events[6:12],[[action,node] for node in self.members for action in ['start','ready']])

    def test_one_active_recovers_stopped_peers_before_rolling_restart(self):
        events=self.recover(True,['deskb2x'])
        self.assertEqual({n for action,n in events[:2] if action=='recover'},{'deskb1x','deskb3x'})
        self.assertEqual({n for action,n in events[2:5] if action=='ready'},set(self.members))
        self.assertEqual(events[5:11],[[action,node] for node in self.members for action in ['start','ready']])

    def test_two_active_recovers_last_peer_before_rolling_restart(self):
        events=self.recover(True,self.members[:2])
        self.assertEqual(events[0],['recover','deskb3x'])
        self.assertEqual({n for action,n in events[1:4] if action=='ready'},set(self.members))
        self.assertEqual(events[4:10],[[action,node] for node in self.members for action in ['start','ready']])

    def test_initial_bootstrap_still_starts_all_before_waiting(self):
        events=self.recover(False,[])
        self.assertEqual({n for action,n in events[:3] if action=='start'},set(self.members))

    def test_live_cluster_upgrade_waits_for_each_replica_before_next(self):
        events=self.recover(True,self.members.copy())
        self.assertEqual({n for action,n in events[:3] if action=='ready'},set(self.members))
        self.assertEqual(events[3:9],[[action,node] for node in self.members for action in ['start','ready']])

    def test_ha_rerun_and_beszel_only_change_do_not_checkpoint(self):
        self.state.write_text(json.dumps({'running':self.members,'rolling':False,'events':[]}))
        plays=yaml.safe_load((ROOT/'playbooks/notifications/ntfy-integrations.yml').read_text())
        stage=plays[2]
        stage['become']=False;stage.pop('vars_files')
        stage['vars']={'desk_ntfy_base_url':'http://192.168.1.186:2586','ntfy_publishers':{'homeassistant':'fixture-token'},'ntfy_beszel_settings':{'changed':True}}
        stage['tasks']=stage['tasks'][1:]
        package=stage['tasks'][0]['ansible.builtin.template']
        package.update(src=str(ROOT/'roles/desk_ntfy/templates/homeassistant-ntfy.yaml.j2'),dest=str(self.root/'{{ inventory_hostname }}/package.yaml'))
        hook=stage['tasks'][1]['ansible.builtin.copy']
        hook.update(src=str(self.root/'{{ inventory_hostname }}-hook'),dest=str(self.root/'{{ inventory_hostname }}/hook.py'))
        for node in self.members:
            (self.root/node).mkdir();(self.root/f'{node}-hook').write_text('initial hook')
        checkpoint=self.root/'checkpoint.yml'
        checkpoint.write_text(yaml.safe_dump([{'hosts':'deskboxes','gather_facts':False,'tasks':[dict(self.command('checkpoint'),run_once=True)]}]))
        gate=dict(plays[3]);gate['ansible.builtin.import_playbook']=str(checkpoint)
        first=self.run_play([stage,gate]);self.assertEqual(len(first),1)
        self.assertEqual(self.run_play([stage,gate]),first)  # Includes Beszel changed=True.
        (self.root/'deskb3x-hook').write_text('updated hook')
        self.assertEqual(len(self.run_play([stage,gate])),2)
        self.assertEqual(len(self.run_play([stage,gate])),2)


if __name__ == '__main__':unittest.main()
