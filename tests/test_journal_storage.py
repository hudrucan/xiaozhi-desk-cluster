"""Local filesystem tests: rotation transaction, archive guards and journal exchange."""
import importlib.util
import os
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch
import yaml

ROOT=Path(__file__).resolve().parents[1]
def load(name,path):
    spec=importlib.util.spec_from_file_location(name,ROOT/path)
    module=importlib.util.module_from_spec(spec);spec.loader.exec_module(module);return module
safety=load('safety_storage','playbooks/node/files/journal-safety.py')
move=load('move_storage','playbooks/node/files/journal-relocate.py')

class JournalStorage(unittest.TestCase):
    def test_validation_replaces_only_owned_rule_and_propagates_failure(self):
        with tempfile.TemporaryDirectory() as d:
            root=Path(d);rules=root/'etc/logrotate.d';rules.mkdir(parents=True)
            (root/'etc/logrotate.conf').write_text('weekly\ninclude /etc/logrotate.d\n')
            (rules/'desk-node-logs').write_text('OLD')
            (rules/'haproxy').write_text('/var/log/haproxy.log {}')
            candidate=root/'new';candidate.write_text('/var/log/syslog {}')
            def validate(argv,**kwargs):
                config=Path(argv[-1]).read_text();includes=Path(config.split('include ')[1].strip())
                self.assertEqual((includes/'haproxy').read_text(),'/var/log/haproxy.log {}')
                self.assertEqual((includes/'desk-node-logs').read_text(),candidate.read_text())
                raise subprocess.CalledProcessError(1,argv)
            with patch.object(safety.subprocess,'run',side_effect=validate):
                self.assertRaises(subprocess.CalledProcessError,safety.validate_rotation,candidate,root)
            self.assertEqual((rules/'desk-node-logs').read_text(),'OLD')

    def test_cleanup_keeps_active_open_unknown_symlink_and_journal(self):
        with tempfile.TemporaryDirectory() as d:
            root=Path(d);old=root/'var/log.hdd';old.mkdir(parents=True)
            (root/'proc/123/fd').mkdir(parents=True)
            (root/'run/systemd/journal').mkdir(parents=True)
            (root/'run/systemd/journal/flushed').touch()
            (root/'etc/default').mkdir(parents=True)
            (root/'etc/default/armbian-ramlog').write_text('ENABLED=true\n')
            for name in ('syslog','syslog.1','kern.log.2.gz','auth.log.1','unknown.1'):(old/name).write_text('keep')
            (old/'journal').mkdir();(old/'journal/evidence.journal').write_text('keep')
            (old/'boot.log.1').symlink_to(old/'syslog')
            (root/'proc/123/fd/7').symlink_to(old/'auth.log.1')
            self.assertRaises(ValueError,safety.cleanup_archives,root)
            (root/'etc/default/armbian-ramlog').write_text('ENABLED=false\n')
            self.assertEqual(safety.cleanup_archives(root),sorted(str(old/n) for n in ('syslog.1','kern.log.2.gz')))
            self.assertTrue((old/'auth.log.1').exists());self.assertTrue((old/'journal/evidence.journal').exists())
            self.assertTrue((old/'syslog').exists());self.assertTrue((old/'unknown.1').exists())

    def test_relocation_preserves_inodes_rolls_back_and_is_idempotent(self):
        with tempfile.TemporaryDirectory() as d:
            root=Path(d);old=root/'var/log.hdd/journal';old.mkdir(parents=True)
            (root/'run/systemd/journal').mkdir(parents=True);(root/'run/systemd/journal/flushed').touch()
            live=root/'var/log/journal';live.parent.mkdir();live.symlink_to(old)
            machine='a'*32;(root/'etc').mkdir();(root/'etc/machine-id').write_text(machine)
            (old/machine).mkdir();data=old/machine/'evidence.journal';data.write_bytes(b'fixture journal')
            inode=data.stat().st_ino
            with patch.object(move.os,'chown'):
                self.assertTrue(move.relocate(root))
            self.assertFalse(live.is_symlink());self.assertEqual((live/machine/data.name).stat().st_ino,inode)
            self.assertTrue(move.relocate(root,'rollback'));self.assertTrue(live.is_symlink())
            self.assertEqual(data.read_bytes(),b'fixture journal')
            with patch.object(move.os,'chown'):move.relocate(root)
            move.relocate(root,'finish')
            self.assertFalse(move.relocate(root))
            self.assertEqual((live/machine/data.name).read_bytes(),b'fixture journal')

    def test_relocation_refuses_enabled_ramlog_before_renaming(self):
        with tempfile.TemporaryDirectory() as d:
            root=Path(d);old=root/'var/log.hdd/journal';old.mkdir(parents=True)
            live=root/'var/log/journal';live.parent.mkdir();live.symlink_to(old)
            (root/'run/systemd/journal').mkdir(parents=True);(root/'run/systemd/journal/flushed').touch()
            (root/'etc/default').mkdir(parents=True)
            (root/'etc/default/armbian-ramlog').write_text('ENABLED=true\n')
            self.assertRaisesRegex(ValueError,'Disable ramlog',move.relocate,root)
            self.assertTrue(live.is_symlink());self.assertTrue(old.is_dir())

    def test_exchange_failure_restores_original_and_play_rescues_validation(self):
        with tempfile.TemporaryDirectory() as d:
            root=Path(d);old=root/'var/log.hdd/journal';old.mkdir(parents=True)
            (root/'run/systemd/journal').mkdir(parents=True);(root/'run/systemd/journal/flushed').touch()
            live=root/'var/log/journal';live.parent.mkdir();live.symlink_to(old)
            (root/'etc').mkdir();(root/'etc/machine-id').write_text('a'*32);(old/('a'*32)).mkdir()
            with patch.object(move.os,'chown'),patch.object(move,'exchange',side_effect=OSError('failure')):
                self.assertRaises(OSError,move.relocate,root)
            self.assertTrue(live.is_symlink());self.assertTrue(old.is_dir())
        play=yaml.safe_load((ROOT/'playbooks/node/journal-relocate.yml').read_text())[0]
        self.assertEqual((play['serial'],play['any_errors_fatal']),(1,True))
        block=play['tasks'][1];self.assertIn('rescue',block)
        commands=[t.get('ansible.builtin.command',{}).get('argv',[]) for t in block['block']]
        self.assertIn(['journalctl','--rotate'],commands)
        self.assertNotIn(['journalctl','--verify'],commands)
        verify=next(t for t in block['block'] if t.get('register')=='journal_archives')
        self.assertEqual(verify['ansible.builtin.command']['argv'][-1],'verify-archives')
        count=next(t for t in block['block'] if t.get('name')=='Require preserved count and persistent writes')
        self.assertEqual(count['ansible.builtin.assert']['that'],['journal_flag.stat.exists','journal_count_after.stdout | int >= journal_count_before.stdout | int'])
        services=next(t for t in block['block'] if t.get('register')=='journal_services')
        self.assertEqual(services['loop'],['systemd-journald','haproxy','keepalived','desk-caddy'])
        self.assertIn('not ansible_check_mode',block['when'])


    def journal_fixture(self, root):
        old=root/'var/log.hdd/journal';old.mkdir(parents=True)
        live=root/'var/log/journal';live.parent.mkdir();live.symlink_to(old)
        (root/'run/systemd/journal').mkdir(parents=True);(root/'run/systemd/journal/flushed').touch()
        machine='a'*32;(root/'etc').mkdir();(root/'etc/machine-id').write_text(machine)
        folder=old/machine;folder.mkdir()
        for name in ('system.journal','user-1000.journal','system@000.journal','user-1000@000.journal'):
            (folder/name).write_text('fixture '+name)
        with patch.object(move.os,'chown'):move.relocate(root)
        # Ownership is the sole simulated root operation; mode/inodes/exchange are real.
        stat=Path.stat;gid=move.grp.getgrnam('systemd-journal').gr_gid
        def ownership(path,*args,**kwargs):
            info=stat(path,*args,**kwargs)
            if path in (live,live/machine):
                values=list(info);values[4:6]=[0,gid];return os.stat_result(values)
            return info
        return live,old,machine,ownership

    def test_active_false_positive_does_not_rollback_but_archives_must_pass(self):
        with tempfile.TemporaryDirectory() as d:
            root=Path(d);live,old,machine,ownership=self.journal_fixture(root)
            checked=[]
            def journalctl(argv,**kwargs):
                path=Path(argv[-1].removeprefix('--file='));checked.append(path.name)
                if '@' not in path.name:
                    raise subprocess.CalledProcessError(1,argv,'active false positive')
                return subprocess.CompletedProcess(argv,0)
            with patch.object(Path,'stat',ownership),patch.object(move.subprocess,'run',side_effect=journalctl):
                self.assertEqual(move.verify_archives(root),2)
            move.relocate(root,'finish')
            self.assertFalse(live.is_symlink())
            self.assertEqual(checked,['system@000.journal','user-1000@000.journal'])
            self.assertTrue((live/machine/'system.journal').is_file())

    def test_archived_failure_rolls_back_same_journal_files(self):
        with tempfile.TemporaryDirectory() as d:
            root=Path(d);live,old,machine,ownership=self.journal_fixture(root)
            archive=live/machine/'system@000.journal';inode=archive.stat().st_ino
            with patch.object(Path,'stat',ownership),patch.object(move.subprocess,'run',side_effect=subprocess.CalledProcessError(1,['journalctl'])):
                try:
                    move.verify_archives(root)
                except subprocess.CalledProcessError:
                    self.assertTrue(move.relocate(root,'rollback'))
                else:self.fail('Corrupt archive was accepted')
            self.assertTrue(live.is_symlink())
            self.assertEqual((old/machine/archive.name).stat().st_ino,inode)

    def test_archive_gate_retains_active_location_permissions_and_flush_checks(self):
        with tempfile.TemporaryDirectory() as d:
            root=Path(d);live,old,machine,ownership=self.journal_fixture(root)
            with patch.object(Path,'stat',ownership),patch.object(move.subprocess,'run') as verify:
                (live/machine).chmod(0o755)
                self.assertRaisesRegex(ValueError,'2755',move.verify_archives,root)
                (live/machine).chmod(0o2755)
                active=live/machine/'system.journal';active.unlink();active.symlink_to(live/machine/'system@000.journal')
                self.assertRaisesRegex(ValueError,'active system',move.verify_archives,root)
                active.unlink();active.touch();(root/'run/systemd/journal/flushed').unlink()
                self.assertRaisesRegex(ValueError,'flush flag',move.verify_archives,root)
                verify.assert_not_called()


    def test_check_mode_and_real_directory_rerun_skip_entire_transaction(self):
        with tempfile.TemporaryDirectory() as d:
            root=Path(d);live=root/'journal';original=root/'old';original.mkdir()
            play=yaml.safe_load((ROOT/'playbooks/node/journal-relocate.yml').read_text())
            play[0].update(hosts='localhost',become=False)
            play[0]['tasks'][0]['ansible.builtin.stat']['path']=str(live)
            target=root/'play.yml';target.write_text(yaml.safe_dump(play,sort_keys=False))
            env=dict(os.environ,ANSIBLE_LOCAL_TEMP=str(root/'ansible-local'))
            live.symlink_to(original)
            for check_mode in (True,False):
                if not check_mode:live.unlink();live.mkdir()
                argv=['ansible-playbook','-i','localhost,','-c','local',str(target)]
                if check_mode:argv+=['--check','--diff']
                result=subprocess.run(argv,env=env,capture_output=True,text=True,timeout=30)
                self.assertEqual(result.returncode,0,result.stdout+result.stderr)
                self.assertRegex(result.stdout,r'changed=0\s+unreachable=0\s+failed=0')
                self.assertEqual(live.is_symlink(),check_mode)
