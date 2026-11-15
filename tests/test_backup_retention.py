"""Owned backup expiry, source-mtime traps, symlinks and actual Ansible ordering."""
import datetime
import importlib.util
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import tempfile
import time
import unittest
import yaml

ROOT = Path(__file__).resolve().parents[1]
ROLE = ROOT/'roles/desk_backup_retention'
spec = importlib.util.spec_from_file_location('backup_retention', ROLE/'files/retention.py')
retention = importlib.util.module_from_spec(spec);spec.loader.exec_module(retention)
CATALOG = json.loads((ROLE/'files/catalog.json').read_text())


class Expiry(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory();self.addCleanup(temp.cleanup)
        self.root = Path(temp.name);self.now = time.time()

    def request(self, scope, check=False):
        return {'catalog':CATALOG,'scopes':[scope],'days':3,'check':check}

    def directory(self, name, age, parent='/var/tmp'):
        path = self.root/parent.lstrip('/')/name;path.mkdir(parents=True)
        (path/'saved.cfg').write_text('rollback')
        stamp = self.now-age*86400;os.utime(path,(stamp,stamp))
        return path

    def backup(self, base, age, pid=123):
        timestamp = datetime.datetime.fromtimestamp(self.now-age*86400).strftime('%Y-%m-%d@%H:%M:%S')
        path=self.root/(base+'.'+str(pid)+'.'+timestamp+'~').lstrip('/')
        path.parent.mkdir(parents=True,exist_ok=True);path.write_text('old source')
        # preserved_copy deliberately retains a very old source mtime.
        os.utime(path,(1,1));return path

    def test_expiry_newest_unmatched_idempotence_and_check_mode(self):
        old=self.directory('desk-dashboard-routes-old',10)
        newest=self.directory('desk-dashboard-routes-newest',5)
        foreign=self.directory('desk-unrelated-old',20)
        live=self.root/'var/tmp/desk-dashboard-routes';live.mkdir()
        request=self.request('dashboard-routes',True)
        result=retention.prune(request,self.root,self.now)
        self.assertEqual(result['deleted'],[]);self.assertEqual(result['would_delete'],[str(old)])
        self.assertTrue(old.exists())
        request['check']=False;result=retention.prune(request,self.root,self.now)
        self.assertEqual(result['deleted'],[str(old)])
        self.assertTrue(newest.exists());self.assertTrue(foreign.exists());self.assertTrue(live.exists())
        self.assertEqual(retention.prune(request,self.root,self.now)['deleted'],[])

    def test_ansible_filename_time_recent_newest_ties_and_legacy_unmatched(self):
        base='/etc/desk-https/Caddyfile'
        old=self.backup(base,10);recent=self.backup(base,1);newest=self.backup(base,.5)
        tie=self.backup(base,.5,456)
        bad=old.with_name('Caddyfile.bak');bad.write_text('unowned')
        malformed=old.with_name('Caddyfile.123.2026-99-99@00:00:00~');malformed.write_text('unowned')
        active=self.root/base.lstrip('/');active.write_text('active')
        result=retention.prune(self.request(base),self.root,self.now)
        self.assertEqual(result['deleted'],[str(old)])
        for path in (recent,newest,tie,bad,malformed,active):self.assertTrue(path.exists())
        # All old: keep both latest timestamp ties even with preserved mtime.
        self.now+=5*86400
        result=retention.prune(self.request(base),self.root,self.now)
        self.assertEqual(result['deleted'],[str(recent)])
        self.assertTrue(newest.exists());self.assertTrue(tie.exists())

    def test_versions_form_one_kind_and_never_remove_live_binary(self):
        old=self.backup('/opt/desk-caddy/2.10.0/caddy',20)
        new=self.backup('/opt/desk-caddy/2.11.7/caddy',10)
        active=new.parent/'caddy';active.write_text('live')
        result=retention.prune(self.request('caddy-binary'),self.root,self.now)
        self.assertEqual(result['deleted'],[str(old)])
        self.assertTrue(new.exists());self.assertTrue(active.exists());self.assertTrue(old.parent.exists())

    def test_hardlinks_symlinks_mounts_owners_and_protected_paths(self):
        target=self.root/'var/lib/data';target.mkdir(parents=True);sentinel=target/'db';sentinel.write_text('durable')
        parent=self.root/'var/tmp';parent.mkdir()
        (parent/'desk-dashboard-routes-link').symlink_to(target,target_is_directory=True)
        old=self.directory('desk-dashboard-routes-hardlinked',10)
        os.link(sentinel,old/'hardlink');os.utime(old,(self.now-10*86400,self.now-10*86400))
        self.directory('desk-dashboard-routes-latest',5)
        result=retention.prune(self.request('dashboard-routes'),self.root,self.now)
        self.assertEqual(result['deleted'],[]);self.assertTrue(old.exists())
        self.assertEqual(sentinel.read_text(),'durable')
        from unittest.mock import patch
        with patch.object(retention.os.path,'ismount',return_value=True):
            self.assertEqual(retention.prune(self.request('dashboard-routes'),self.root,self.now)['deleted'],[])
        with patch.object(retention.os,'geteuid',return_value=os.geteuid()+1):
            self.assertEqual(retention.prune(self.request('dashboard-routes'),self.root,self.now)['deleted'],[])
        evil={'evil':{'type':'ansible','file':'/var/lib/data/db'}}
        with self.assertRaises(ValueError):retention.prune({'catalog':evil,'scopes':['evil'],'days':3},self.root,self.now)
        with self.assertRaises(ValueError):retention.prune(self.request('unknown'),self.root,self.now)
        for days in (0,-1,float('nan'),float('inf')):
            request=self.request('dashboard-routes');request['days']=days
            with self.assertRaises(ValueError):retention.prune(request,self.root,self.now)
        # A symlink in an ancestor is refused, even when it stays inside root.
        (self.root/'etc').symlink_to(target,target_is_directory=True)
        self.assertEqual(retention.prune(self.request('/etc/desk-https/Caddyfile'),self.root,self.now)['deleted'],[])


@unittest.skipUnless(shutil.which('ansible-playbook'),'Ansible required')
class ActualPlay(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temp=tempfile.TemporaryDirectory();cls.base=Path(cls.temp.name)
        cls.roles=cls.base/'roles';shutil.copytree(ROLE,cls.roles/'desk_backup_retention')
        helper=cls.roles/'desk_backup_retention/files/retention.py'
        text=helper.read_text().replace("print(json.dumps(prune(json.load(sys.stdin)), separators=(',', ':')))",
            "request=json.load(sys.stdin)\n    root=Path(os.environ['TEST_BACKUP_ROOT'])\n    if not request['check']:\n        assert (root.parent/'gate').exists() and (root/'handler').exists()\n    print(json.dumps(prune(request, root=root), separators=(',', ':')))")
        helper.write_text(text)
        tasks=cls.roles/'desk_backup_retention/tasks/main.yml';data=yaml.safe_load(tasks.read_text())
        data[0]['environment']={'TEST_BACKUP_ROOT':'{{ fixture_root }}'};tasks.write_text(yaml.safe_dump(data,sort_keys=False))
        cls.inventory=cls.base/'inventory.yml'

    @classmethod
    def tearDownClass(cls):cls.temp.cleanup()

    def fixture(self,fail=False,rescue=False,check=False):
        current=Path(tempfile.mkdtemp(dir=self.base));hosts={}
        for index in range(3):
            root=current/str(index);root.mkdir()
            for name,age in [('old',10),('latest',5)]:
                p=root/'var/tmp'/('desk-dashboard-routes-'+name);p.mkdir(parents=True)
                (p/'saved').write_text('rollback');s=time.time()-age*86400;os.utime(p,(s,s))
            hosts['n'+str(index)]={'ansible_connection':'local','ansible_python_interpreter':shutil.which('python3'),'fixture_root':str(root)}
        self.inventory.write_text(yaml.safe_dump({'all':{'hosts':hosts}}))
        source=yaml.safe_load((ROOT/'playbooks/dashboard/dashboard-routes.yml').read_text())[0]['post_tasks']
        # Keep production per-host/run_once behavior; become is fixture-local only.
        for task in source:
            if 'ansible.builtin.include_role' in task:task['ansible.builtin.include_role']['apply']['become']=False
        create={'name':'Create new rollback before gates','ansible.builtin.command':{'argv':['python3','-c',"from pathlib import Path;p=Path(__import__('sys').argv[1]);p.mkdir(parents=True,exist_ok=True)",'{{ fixture_root }}/var/tmp/desk-dashboard-routes-fresh']},'changed_when':False}
        notify={'name':'Queue deployment handler','ansible.builtin.command':{'argv':['true']},'notify':'fixture handler','changed_when':True}
        gate={'name':'Final readiness gate','ansible.builtin.command':{'argv':['python3','-c',"from pathlib import Path;import sys;Path(sys.argv[1]).write_text('passed');raise SystemExit(int(sys.argv[2]))",'{{ fixture_root }}/../gate','1' if fail else '0']},'changed_when':False}
        if rescue:
            gate['ansible.builtin.command']['argv'][-1]='{{ (inventory_hostname == "n1") | int }}'
            gate={'block':[gate],'rescue':[{'ansible.builtin.debug':{'msg':'rollback completed'}}]}
        play={'hosts':'all','gather_facts':False,'tasks':[create,notify],
              'handlers':[{'name':'fixture handler','ansible.builtin.command':{'argv':['python3','-c',"from pathlib import Path;import sys;Path(sys.argv[1]).write_text('handler')",'{{ fixture_root }}/handler']}}],
              }
        final={'hosts':'all','gather_facts':False,'run_once':not rescue,'tasks':[gate],'post_tasks':source}
        path=current/'play.yml';path.write_text(yaml.safe_dump([play,final],sort_keys=False))
        env=dict(os.environ,ANSIBLE_ROLES_PATH=str(self.roles),ANSIBLE_LOCAL_TEMP=str(current/'local-tmp'),ANSIBLE_NOCOLOR='1')
        cmd=['ansible-playbook','-i',str(self.inventory),str(path)]
        if check:cmd+=['--check','--diff']
        result=subprocess.run(cmd,env=env,stdout=subprocess.PIPE,stderr=subprocess.STDOUT,text=True,timeout=45)
        (current/'run.log').write_text(result.stdout)
        self.assertIn('PLAY RECAP',result.stdout,result.stdout)
        return current,cmd,env,result

    def test_real_apply_all_hosts_then_idempotent_and_check(self):
        current,cmd,env,result=self.fixture();self.assertEqual(result.returncode,0,result.stdout)
        for index in range(3):
            parent=current/str(index)/'var/tmp'
            self.assertEqual(sorted(p.name for p in parent.iterdir()),['desk-dashboard-routes-fresh'])
        rerun=subprocess.run(cmd,env=env,stdout=subprocess.PIPE,stderr=subprocess.STDOUT,text=True,timeout=45)
        self.assertEqual(rerun.returncode,0,rerun.stdout)
        section=rerun.stdout.split('TASK [desk_backup_retention : Prune')[1].split('TASK [')[0]
        self.assertNotIn('changed: [',section)
        current,_,_,result=self.fixture(check=True);self.assertEqual(result.returncode,0,result.stdout)
        for index in range(3):self.assertTrue((current/str(index)/'var/tmp/desk-dashboard-routes-old').exists())
        self.assertNotIn('changed: [',result.stdout)

    def test_failed_gate_or_successfully_handled_rescue_never_prunes(self):
        for rescue in (False,True):
            with self.subTest(rescue=rescue):
                current,_,_,result=self.fixture(fail=True,rescue=rescue)
                self.assertEqual(result.returncode==0,rescue,result.stdout)
                for index in range(3):self.assertTrue((current/str(index)/'var/tmp/desk-dashboard-routes-old').exists())


class Integration(unittest.TestCase):
    def test_only_post_success_catalog_scopes_and_baseline_preserved(self):
        # Inventory every reachable producer plus explicit retained rollout dirs.
        files=json.loads((ROOT/'roles/desk_backup_retention/files/integrations.json').read_text())
        for name in files:
            plays=yaml.safe_load((ROOT/name).read_text());post=plays[-1]['post_tasks']
            self.assertEqual(post[0]['ansible.builtin.meta'],'flush_handlers')
            self.assertEqual(post[1]['ansible.builtin.include_role']['name'],'desk_backup_retention')
            self.assertFalse(post[1]['run_once']);self.assertFalse(post[1]['ansible.builtin.include_role']['apply']['run_once'])
            self.assertIn('ansible_failed_task is not defined',post[1]['when'])
            self.assertIn('ansible_skip_tags | length == 0',post[1]['when'])
            self.assertTrue(set(post[1]['vars']['desk_backup_retention_scopes'])<=set(CATALOG))
        for spec in CATALOG.values():self.assertNotIn('/var/lib/',json.dumps(spec))
        https=yaml.safe_load((ROOT/'playbooks/https/caddy.yml').read_text())[-1]
        self.assertIn("desk_https_phase | default('preflight') == 'caddy'",https['post_tasks'][1]['when'])
        self.assertNotIn('backup-retention.yml',(ROOT/'site.yml').read_text())
