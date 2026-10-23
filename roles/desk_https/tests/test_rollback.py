"""Execute rollback play logic with one unreachable node and fixture command actions."""
import json
import os
from pathlib import Path
import shutil
import socket
import subprocess
import tempfile
import unittest
from test_https import ROOT


@unittest.skipUnless(shutil.which('ansible-playbook'), 'Requires the existing Ansible runtime')
class DegradedRollback(unittest.TestCase):
    def test_survivors_roll_back_with_or_without_the_original_dns_coordinator(self):
        for dead in ['deskb2x', 'deskb1x']:
            with self.subTest(dead=dead), tempfile.TemporaryDirectory(prefix='https-rollback-') as tmp:
                tmp=Path(tmp);plugins=tmp/'plugins';plugins.mkdir();log=tmp/'commands.jsonl'
                # Instrument only command action names in a temporary role copy;
                # FQCN ansible.builtin.command cannot be shadowed safely. All
                # reachability, guards, host selection and task conditions are real.
                role=tmp/'roles/desk_https'
                for name in ['defaults/main.yml','tasks/main.yml','tasks/rollback_preflight.yml','tasks/rollback.yml','files/dns_records.py']:
                    target=role/name;target.parent.mkdir(parents=True,exist_ok=True)
                    target.write_text((ROOT/'roles/desk_https'/name).read_text().replace('ansible.builtin.command:','https_fixture_command:'))
                playbook=tmp/'caddy.yml';playbook.write_text((ROOT/'playbooks/https/caddy.yml').read_text())
                (plugins/'https_fixture_command.py').write_text('''import json, os
from ansible.plugins.action import ActionBase
class ActionModule(ActionBase):
    def run(self, tmp=None, task_vars=None):
        argv=self._task.args['argv']
        with open(os.environ['HTTPS_TEST_ACTION_LOG'],'a') as f:
            f.write(json.dumps({'host':task_vars['inventory_hostname'],'argv':argv})+'\\n')
        dns='--rollback' in argv
        return {'changed':False,'rc':0,'stdout':json.dumps({'status':'pending','changed':False,'reason':'Ownership or certified policy unavailable'}) if dns else 'changed'}
''')
                # Hold a non-listening TCP socket so the failed SSH port is known
                # and cannot accidentally connect to a real local SSH service.
                with socket.socket() as closed:
                    closed.bind(('127.0.0.1',0));port=closed.getsockname()[1]
                    inventory=tmp/'inventory.ini'
                    inventory.write_text('[xiaozhi_ingress]\n'+'\n'.join(
                        f'{node} ansible_connection=ssh ansible_host=127.0.0.1 ansible_port={port} ansible_ssh_common_args="-o BatchMode=yes -o ConnectTimeout=1"'
                        if node==dead else f'{node} ansible_connection=local ansible_python_interpreter={shutil.which("python3")}'
                        for node in ['deskb1x','deskb2x','deskb3x'])+'\n')
                    env={**os.environ,'ANSIBLE_ROLES_PATH':str(tmp/'roles'),'ANSIBLE_ACTION_PLUGINS':str(plugins),'HTTPS_TEST_ACTION_LOG':str(log),'ANSIBLE_NOCOLOR':'1','ANSIBLE_LOCAL_TEMP':str(tmp/'ansible-local')}
                    argv=['ansible-playbook','-i',str(inventory),str(playbook),'-e','desk_https_phase=rollback','-e','ansible_become=false']
                    denied=subprocess.run(argv,cwd=ROOT,env=env,text=True,capture_output=True,timeout=30)
                    self.assertNotEqual(denied.returncode,0);self.assertFalse(log.exists(),denied.stdout+denied.stderr)
                    result=subprocess.run(argv+['-e','desk_https_apply_approved=true'],cwd=ROOT,env=env,text=True,capture_output=True,timeout=60)
                self.assertEqual(result.returncode,0,result.stdout+result.stderr)
                commands=[json.loads(line) for line in log.read_text().splitlines()]
                survivors=set(['deskb1x','deskb2x','deskb3x'])-{dead}
                removed=[x for x in commands if '--remove-https' in x['argv']]
                self.assertEqual({x['host'] for x in removed},survivors)
                dns=[x for x in commands if '--rollback' in x['argv']]
                self.assertEqual(len(dns),1);self.assertEqual(dns[0]['host'],sorted(survivors)[0])
                self.assertIn('pending',result.stdout)
                self.assertIn(f'HTTPS rollback pending on {dead}',result.stdout)


if __name__=='__main__':unittest.main()
