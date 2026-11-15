"""DB storage guards without mounts, containers, peers or credentials."""
import importlib.util
from pathlib import Path
import sys
import tempfile
import threading
import unittest
from unittest.mock import MagicMock,patch
FILES=Path(__file__).resolve().parents[1]/'files'
sys.path.insert(0,str(FILES))
spec=importlib.util.spec_from_file_location('ha_db_tmpfs',FILES/'ha.py');ha=importlib.util.module_from_spec(spec);spec.loader.exec_module(ha)

class DBTmpfs(unittest.TestCase):
    def cluster(self,root):
        c=object.__new__(ha.Cluster);c.c={'db_tmpfs':True};c.root=root;c.pid=(42,'99');return c

    def test_exact_mount_boundary_and_rw_tmpfs_required(self):
        valid='1 2 0:3 / /data/db rw,nosuid,nodev,noexec - tmpfs tmpfs rw\n'
        self.assertEqual(ha.tmpfs_mount('/data/db',valid),'0:3')
        for bad in (valid.replace('tmpfs','ext4'),valid.replace('/data/db ','/data/db-other '),valid.replace('rw,nosuid','ro,nosuid')):
            self.assertRaises(ha.Unavailable,ha.tmpfs_mount,'/data/db',bad)

    def test_host_container_device_inode_and_pid_identity(self):
        with tempfile.TemporaryDirectory() as d:
            root=Path(d);db=root/'data/db';db.mkdir(parents=True);c=self.cluster(root)
            host=f'1 2 0:3 / {db} rw - tmpfs tmpfs rw\n'
            inside='1 2 0:3 / /data/db rw - tmpfs tmpfs rw\n'
            paths={'/proc/self/mountinfo':host,'/proc/42/mountinfo':inside,'/proc/42/stat':'42 (fixture) '+' '.join(['0']*19+['99'])}
            read=Path.read_text;stat=Path.stat
            def get(p,*a,**kw):return paths[str(p)] if str(p) in paths else read(p,*a,**kw)
            def info(p,*a,**kw):return stat(db) if str(p)=='/proc/42/root/data/db' else stat(p,*a,**kw)
            with patch.object(Path,'read_text',get),patch.object(Path,'stat',info):
                c.require_db_tmpfs(True)
                paths['/proc/42/mountinfo']=inside.replace('0:3','0:4')
                self.assertRaises(ha.Unavailable,c.require_db_tmpfs,True)
                paths['/proc/42/mountinfo']=inside.replace('tmpfs','ext4')
                self.assertRaises(ha.Unavailable,c.require_db_tmpfs,True)
                paths['/proc/42/mountinfo']=inside;paths['/proc/42/stat']=paths['/proc/42/stat'].replace('99','100')
                self.assertRaises(ha.Unavailable,c.require_db_tmpfs,True)

    def test_missing_mount_blocks_restore_and_promotion_before_writes(self):
        c=self.cluster(Path('/not-mounted'));c.oplock=threading.RLock();c.running=False;c.warm=False
        c.owned=lambda:True;c.warm_state=lambda:{'state':'inactive'};c.retire_checkpoint_source=MagicMock()
        with patch.object(ha,'restore_worker') as restore,patch.object(ha.Cluster,'require_db_entrypoint'):
            self.assertRaises(ha.Unavailable,c.start_container,True)
            self.assertRaises(ha.Unavailable,c.activate_warm)
            restore.assert_not_called();c.retire_checkpoint_source.assert_not_called()
        c.c['db_tmpfs']=False;c.require_db_tmpfs(True);self.assertEqual(c.db_storage(),{'mode':'disk'})

    def test_mount_unit_and_canary_keep_state_and_recreate_only_one_node(self):
        import yaml
        role=FILES.parent;repo=role.parents[1]
        unit=(role/'templates/var-lib-desk-network-data-db.mount.j2').read_text()
        self.assertIn('size=256M,mode=0700,nosuid,nodev,noexec',unit)
        self.assertIn('Where=/var/lib/desk-network/data/db',unit)
        mount_tasks=yaml.safe_load((role/'tasks/db-tmpfs.yml').read_text())
        inspect=next(t for t in mount_tasks if t.get('register')=='network_db_container')
        self.assertEqual(inspect['ansible.builtin.command']['argv'],['podman','container','exists',ha.NAME])
        self.assertEqual(inspect['failed_when'],'network_db_container.rc not in [0, 1]')
        play=yaml.safe_load((repo/'playbooks/network/network-db-tmpfs.yml').read_text())[0]
        self.assertEqual(play['hosts'],'{{ desk_network_tmpfs_target }}')
        self.assertEqual(play['serial'],1);self.assertTrue(play['any_errors_fatal'])
        change=play['tasks'][-1];self.assertIn('rescue',change)
        self.assertEqual(change['block'][0]['ansible.builtin.systemd_service']['state'],'stopped')
        self.assertFalse(any('/state' in str(t.get('ansible.builtin.file',{})) for t in change['block']))


    def capture_run(self,root,tmpfs,baseline=False):
        import json
        c=self.cluster(root);c.c.update(db_tmpfs=tmpfs,api_token='fixture')
        c.oplock=threading.RLock();c.running=False;c.warm=True;c.warm_control=MagicMock()
        c.warm_path=root/'warm';c.run=root/'run';c.store=MagicMock();c.store.head={'digest':'fixture'}
        c.retire_checkpoint_source=MagicMock();c.require_db_tmpfs=MagicMock()
        c.members={'node':'192.0.2.1'};c.node='node';c.budget=MagicMock();c.budget.start.return_value=False
        c.local={};c.last_local=0
        calls=[]
        def command(*argv,**kwargs):
            calls.append(list(argv))
            return MagicMock(stdout=json.dumps([{'State':{'Pid':42}}]).encode())
        c.command=command
        with patch.object(ha,'restore_worker'),patch.object(Path,'read_text',return_value='42 (fixture) '+' '.join(['0']*19+['99'])):
            if baseline:ha._baseline_start_container(c,True)
            else:c.start_container(True)
        return calls[0]

    def test_tmpfs_adds_readonly_helper_and_disk_argv_matches_baseline(self):
        import ast,subprocess,os
        source=subprocess.check_output(['git','show','a125115b62dd394a512734af652b99cd9351ab16:roles/desk_network/files/ha.py'],text=True)
        method=next(n for c in ast.parse(source).body if isinstance(c,ast.ClassDef) and c.name=='Cluster' for n in c.body if isinstance(n,ast.FunctionDef) and n.name=='start_container')
        method.name='_baseline_start_container';exec(compile(ast.Module(body=[method],type_ignores=[]),'baseline','exec'),ha.__dict__)
        with tempfile.TemporaryDirectory() as d:
            root=Path(d);helper=root/'noop.py';helper.write_text('#!/usr/bin/env python3\nimport sys\nsys.exit(0)\n');helper.chmod(0o555)
            original=Path.lstat
            def info(p,*a,**kw):
                st=original(p,*a,**kw)
                if p==helper:
                    values=list(st);values[4]=0;return os.stat_result(values)
                return st
            with patch.object(ha,'DB_MOUNTS_CHECK',helper),patch.object(Path,'lstat',info):
                disk=self.capture_run(root,False)
                self.assertEqual(disk,self.capture_run(root,False,baseline=True))
                tmpfs=self.capture_run(root,True)
            bind=str(helper)+':/entrypoint.d/15-mounts.py:ro'
            self.assertNotIn(bind,disk);self.assertIn(bind,tmpfs)
            self.assertIn(str(root/'data/db')+':/data/db:rw',tmpfs)

    def test_missing_or_unsafe_helper_blocks_start_and_reports_error(self):
        import os
        with tempfile.TemporaryDirectory() as d:
            root=Path(d);helper=root/'noop.py';c=self.cluster(root)
            c.oplock=threading.RLock();c.command=MagicMock()
            with patch.object(ha,'DB_MOUNTS_CHECK',helper):
                self.assertRaisesRegex(ha.Unavailable,'entrypoint helper missing',c.start_container,True)
                self.assertIn('entrypoint helper missing',c.db_storage()['error'])
                helper.touch();helper.chmod(0o755)
                self.assertRaisesRegex(ha.Unavailable,'root-owned.*0555',c.start_container,True)
                helper.chmod(0o555)
                actual=helper.lstat();values=list(actual);values[4]=1234
                with patch.object(Path,'lstat',return_value=os.stat_result(values)):
                    self.assertRaisesRegex(ha.Unavailable,'root-owned.*0555',c.start_container,True)
                helper.unlink();helper.symlink_to(root/'absent')
                self.assertRaisesRegex(ha.Unavailable,'regular file',c.start_container,True)
                c.command.assert_not_called()

    def test_exited_container_ends_ansible_retries_and_enters_rescue(self):
        import copy,json,os,subprocess,yaml
        repo=FILES.parent.parents[1]
        play=yaml.safe_load((repo/'playbooks/network/network-db-tmpfs.yml').read_text())[0]
        gate=copy.deepcopy(next(t for t in play['tasks'][-1]['block'] if t.get('register')=='tmpfs_after'))
        with tempfile.TemporaryDirectory() as d:
            root=Path(d);podman=root/'podman'
            podman.write_text('#!'+sys.executable+'\nimport sys,json\nif sys.argv[1]=="inspect": print(json.dumps([{"State":{"Status":"exited"}}]))\nelif sys.argv[1]=="logs": print("\\n".join("line-"+str(i) for i in range(30)))\nelse: sys.exit(1)\n')
            podman.chmod(0o755)
            env=dict(os.environ,PATH=str(root)+os.pathsep+os.environ['PATH'],ANSIBLE_LOCAL_TEMP=str(root/'ansible'))
            code=gate['ansible.builtin.command']['argv'][-1]
            p=subprocess.run([sys.executable,'-c',code],env=env,capture_output=True,text=True,timeout=10)
            self.assertEqual(p.returncode,42,p.stderr)
            self.assertIn('line-10',p.stderr);self.assertNotIn('line-9\n',p.stderr)
            test=[{'hosts':'localhost','gather_facts':False,'vars':{'cluster_ip':'127.0.0.1','desk_network_db_tmpfs':True},'tasks':[{'block':[gate],'rescue':[{'ansible.builtin.command':{'argv':[sys.executable,'-c','print("rescued")']}}]}]}]
            target=root/'play.yml';target.write_text(yaml.safe_dump(test,sort_keys=False))
            p=subprocess.run(['ansible-playbook','-i','localhost,','-c','local',str(target)],env=env,capture_output=True,text=True,timeout=30)
            self.assertEqual(p.returncode,0,p.stdout+p.stderr)
            self.assertRegex(p.stdout,r'rescued=1')
            self.assertNotIn('FAILED - RETRYING',p.stdout)


    def test_exited_still_fails_fast_if_logs_time_out(self):
        import io,json,subprocess,yaml
        repo=FILES.parent.parents[1]
        play=yaml.safe_load((repo/'playbooks/network/network-db-tmpfs.yml').read_text())[0]
        gate=next(t for t in play['tasks'][-1]['block'] if t.get('register')=='tmpfs_after')
        code=gate['ansible.builtin.command']['argv'][-1]
        inspect=subprocess.CompletedProcess([],0,stdout=json.dumps([{'State':{'Status':'exited'}}]))
        with patch.object(subprocess,'run',side_effect=[inspect,subprocess.TimeoutExpired(['podman','logs'],3)]),patch.object(sys,'stderr',io.StringIO()) as output:
            with self.assertRaises(SystemExit) as error:exec(compile(code,'readiness','exec'),{})
            self.assertEqual(error.exception.code,42)
            self.assertIn('Cannot read container logs: TimeoutExpired',output.getvalue())
