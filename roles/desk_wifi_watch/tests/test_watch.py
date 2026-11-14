"""Offline clocks, file trees and injected commands: never contact managed nodes."""
import configparser
import importlib.util
import json
import os
import re
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[3]
SPEC = importlib.util.spec_from_file_location('watch', ROOT/'roles/desk_wifi_watch/files/desk-wifi-watch.py')
watch = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(watch)
CONFIG = {'management_peers':['192.168.1.102','192.168.1.103'], 'mesh_peers':['10.10.10.12','10.10.10.13']}


class FakeCommands:
    def __init__(self, router=True, retry=False, wlan=False, mesh=True):
        self.router, self.retry, self.wlan, self.mesh = router, retry, wlan, mesh
        self.calls = []

    def run(self, argv, timeout=0.6):
        self.calls.append(argv)
        if argv[0] == 'ping':
            return '' if self.router else None
        if argv[0] == 'nmcli' and 'GENERAL.STATE,GENERAL.CONNECTION' in argv:
            return 'GENERAL.STATE:100 (connected)\nGENERAL.CONNECTION:profile'
        if argv[0] == 'nmcli':
            return '*:AA\\:BB\\:CC\\:DD\\:EE\\:FF:70'
        if argv[0] == 'journalctl':
            return '\n'.join(['other message']+['aic wlan0 message '+str(i) for i in range(30)]+['wlan0 psk=DO_NOT_RECORD'])
        return ''

    def pings(self, targets):
        self.calls.extend([['ping', '-I', interface, '-c1', '-W1', peer] for interface, peer in targets])
        return [self.retry, self.wlan, False, self.mesh, False]


class Observation(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        for suffix, content in {
            '/proc/net/route':'Iface Destination Gateway Flags RefCnt Use Metric Mask\nwlan0 00000000 0101A8C0 0003 0 0 600 00000000\n',
            '/proc/net/wireless':'wlan0: signal', '/proc/sys/kernel/random/boot_id':'boot-1',
            '/sys/class/net/wlan0/statistics/rx_bytes':'100', '/sys/class/net/wlan0/statistics/tx_bytes':'200',
        }.items():
            p = self.root/suffix[1:]; p.parent.mkdir(parents=True, exist_ok=True); p.write_text(content)
        for directory in ('run','persist'):
            (self.root/directory).mkdir(mode=0o700)
        self.events, self.writes = [], []

    def sample(self, now, commands=None, wall=None):
        def save(path, value):
            self.writes.append(path)
            watch.atomic(path, value)
        return watch.observe(CONFIG, commands or FakeCommands(), root=str(self.root),
                             runtime=str(self.root/'run'), persistent=str(self.root/'persist'),
                             now=now, wall=100000+now if wall is None else wall,
                             emit=self.events.append, save=save)

    def state(self):
        return json.loads((self.root/'run/sample.json').read_text())

    def test_events_and_errors_survive_unit_notice_filter(self):
        unit=configparser.ConfigParser(interpolation=None)
        unit.read(ROOT/'roles/desk_wifi_watch/templates/desk-wifi-watch.service.j2')
        service=unit['Service']
        self.assertEqual(service['LogLevelMax'],'notice')
        self.assertEqual(service['SyslogLevel'],'notice')
        self.assertEqual(service['StandardOutput'],'journal')
        self.assertEqual(service['StandardError'],'journal')
        levels=['emerg','alert','crit','err','warning','notice','info','debug']
        default_priority=levels.index(service['SyslogLevel'])
        maximum_priority=levels.index(service['LogLevelMax'])
        self.sample(1)
        self.sample(601)
        for now in (631,661,691): self.sample(now,FakeCommands(False))
        self.sample(721,FakeCommands(False,wlan=True))
        self.sample(751)
        for event in ('observation_reset','heartbeat','outage_start','outage_change','outage_end'):
            self.assertTrue(any('event="'+event+'"' in line for line in self.events),event)
        # Execute the real stderr error handler before any network/state access.
        error=subprocess.run([sys.executable,str(ROOT/'roles/desk_wifi_watch/files/desk-wifi-watch.py'),
                              str(self.root/'missing-config.json')],capture_output=True,text=True,timeout=5)
        self.assertEqual(error.returncode,1)
        self.assertIn('event=observer_error',error.stderr)
        for line in [*self.events,*error.stderr.splitlines()]:
            prefix=re.match(r'^<([0-7])>',line)
            priority=int(prefix[1]) if prefix else default_priority
            self.assertLessEqual(priority,maximum_priority,line)
        self.assertGreater(levels.index('info'),maximum_priority)

    def test_classification(self):
        for router, wlan, mesh, expected in [(True,False,False,'ok'),(False,True,False,'router_or_icmp'),
                                             (False,False,True,'wlan_path_down'),(False,False,False,'isolated')]:
            self.assertEqual(watch.classify(router,wlan,mesh), expected)

    def test_healthy_one_command_and_no_file_write_or_log_until_heartbeat(self):
        self.sample(1)
        before = {p: (p.stat().st_mtime_ns,p.read_bytes()) for p in self.root.rglob('*.json')}
        self.writes.clear(); self.events.clear()
        commands = FakeCommands()
        self.sample(31, commands)
        self.assertEqual(commands.calls, [['ping','-I','wlan0','-c1','-W1','192.168.1.1']])
        self.assertEqual((self.writes,self.events), ([],[]))
        self.assertEqual(before, {p:(p.stat().st_mtime_ns,p.read_bytes()) for p in before})
        self.sample(601)
        self.assertEqual(len(self.events),1)
        self.assertIn('event="heartbeat"',self.events[0])
        self.assertEqual(len(self.writes),2)
        self.assertEqual((self.root/'persist/state.json').stat().st_mode & 0o777,0o600)

    def test_threshold_outage_end_wall_jump_and_assoc(self):
        self.sample(1)
        self.events.clear(); self.writes.clear()
        for now in (31,61):
            self.sample(now,FakeCommands(False))
            self.assertFalse(self.events)
            self.assertFalse(any('/persist/' in p for p in self.writes))
        self.sample(91,FakeCommands(False))
        self.assertTrue(self.state()['assoc_no_data'])
        self.assertEqual(self.state()['class'],'wlan_path_down')
        self.assertEqual(len(self.events),1)
        self.assertIn('event="outage_start"',self.events[0])
        self.assertIn('"AA:BB:CC:DD:EE:FF"',self.events[0])
        kernel_json = self.events[0].split('kernel=',1)[1]
        self.assertEqual(len(json.loads(kernel_json)),20)
        self.assertNotIn('DO_NOT_RECORD',self.events[0])
        self.sample(121,wall=50000)
        self.assertIn('event="outage_end"',self.events[-1])
        self.assertIn('duration=90',self.events[-1])

    def test_counter_change_and_failed_ping_retry(self):
        for now in (1,31,61):
            (self.root/'sys/class/net/wlan0/statistics/rx_bytes').write_text(str(now))
            self.sample(now,FakeCommands(False))
        self.assertFalse(self.state()['assoc_no_data'])
        self.sample(91,FakeCommands(False,retry=True))
        self.assertIn('event="outage_end"',self.events[-1])
        self.assertFalse(self.state()['outage'])

    def test_gap_and_boot_reset_without_long_outage_end(self):
        for now in (1,31,61): self.sample(now,FakeCommands(False))
        self.sample(300,FakeCommands(False))
        self.assertEqual(self.state()['fails'],1)
        self.assertIn('event="observation_reset"',self.events[-1])
        (self.root/'proc/sys/kernel/random/boot_id').write_text('boot-2')
        self.sample(2,FakeCommands(False))
        self.assertEqual(self.state()['fails'],1)
        self.assertFalse(self.state()['outage'])
        self.assertFalse(any('outage_end' in event for event in self.events))

    def test_corrupt_missing_and_wrong_shape_state_reset_safely(self):
        for content in ('{broken', '[]', '{}', '{"version":1,"boot":"x","mono":"bad"}'):
            (self.root/'run/sample.json').write_text(content)
            self.assertEqual(self.sample(1),0)
            self.assertIn('observation_reset',self.events[-1])
        (self.root/'run/sample.json').unlink()
        self.assertEqual(self.sample(31),0)

    def test_dynamic_route_missing_and_metric(self):
        self.assertIsNone(watch.gateway(''))
        route=(self.root/'proc/net/route').read_text()
        self.assertEqual(watch.gateway(route+'wlan0 00000000 FE01A8C0 0003 0 0 100 00000000\n'),'192.168.1.254')
        self.assertEqual(watch.gateway(route.replace('0101A8C0','0201A8C0')),'192.168.1.2')
        (self.root/'proc/net/route').unlink()
        # Missing route must produce a failed observation, not crash or invent a router.
        class NoRoute(FakeCommands):
            def pings(self, targets): return [False,False,True,False]
        self.sample(1,NoRoute(False))
        self.assertEqual(self.state()['fails'],1)

    def test_outage_classification_changes_are_recorded(self):
        for now in (1,31,61): self.sample(now,FakeCommands(False,wlan=True,mesh=False))
        self.assertEqual(self.state()['class'],'router_or_icmp')
        self.sample(91,FakeCommands(False,wlan=False,mesh=False))
        self.assertEqual(self.state()['class'],'isolated')
        self.assertIn('outage_change',self.events[-1])
        self.sample(121,FakeCommands(False,wlan=False,mesh=True))
        self.assertEqual(self.state()['class'],'wlan_path_down')
        self.assertIn('outage_change',self.events[-1])

    def test_persistent_corruption_does_not_break_live_sampling(self):
        self.sample(1)
        (self.root/'persist/state.json').write_text('broken')
        self.events.clear(); self.writes.clear()
        self.assertEqual(self.sample(31),0)
        self.assertEqual((self.events,self.writes),([],[]))
        self.sample(601)
        self.assertEqual(json.loads((self.root/'persist/state.json').read_text())['version'],1)

    def test_actual_healthy_subprocess_count_with_path_stub(self):
        binary=self.root/'bin'; binary.mkdir()
        ping=binary/'ping'
        ping.write_text('#!/bin/sh\nexit 0\n'); ping.chmod(0o755)
        self.sample(1)
        self.events.clear(); self.writes.clear()
        with patch.dict(os.environ,PATH=str(binary)+os.pathsep+os.environ['PATH']):
            with patch.object(watch.subprocess,'Popen',wraps=subprocess.Popen) as spawn:
                self.sample(31,watch.Commands())
                self.assertEqual(spawn.call_count,1)
                self.assertEqual(spawn.call_args.args[0],['ping','-I','wlan0','-c1','-W1','192.168.1.1'])
        self.assertEqual((self.writes,self.events),([],[]))

    def test_actual_parallel_children_share_deadline_and_are_reaped(self):
        original=subprocess.Popen
        children=[]
        def spawn(argv,**kwargs):
            child=original([sys.executable,'-c','import time; time.sleep(5)'],**kwargs)
            children.append(child)
            return child
        started=time.monotonic()
        with patch.object(watch.subprocess,'Popen',side_effect=spawn):
            self.assertEqual(watch.Commands().pings([('wlan0','stub')]*5),[False]*5)
        self.assertLess(time.monotonic()-started,3)
        self.assertEqual(len(children),5)
        self.assertTrue(all(child.poll() is not None for child in children))
        commands=watch.Commands(); commands.deadline=time.monotonic()+0.05
        started=time.monotonic()
        self.assertIsNone(commands.run([sys.executable,'-c','import time; time.sleep(5)'],2))
        self.assertLess(time.monotonic()-started,0.8)

    def test_main_lock_skips_overlapping_sample(self):
        config=self.root/'config.json'; config.write_text(json.dumps(CONFIG))
        lockpath=self.root/'run/lock'
        original_open=open
        def mapped(path,*args,**kwargs):
            return original_open(lockpath if path=='/run/desk-wifi-watch/lock' else path,*args,**kwargs)
        old_mask=os.umask(0o077)
        try:
            with original_open(lockpath,'a') as held:
                watch.fcntl.flock(held,watch.fcntl.LOCK_EX | watch.fcntl.LOCK_NB)
                with patch.object(watch,'open',side_effect=mapped,create=True), patch.object(sys,'argv',['watch',str(config)]):
                    with patch.object(watch,'observe') as observe:
                        self.assertEqual(watch.main(),0)
                        observe.assert_not_called()
        finally: os.umask(old_mask)

    def test_real_child_timeouts_and_parallel_ping_deadline(self):
        with patch.object(watch.subprocess,'run',side_effect=subprocess.TimeoutExpired('x',0.1)):
            self.assertIsNone(watch.Commands().run(['anything'],0.1))
        commands=watch.Commands()
        started=time.monotonic()
        self.assertIsNone(commands.run([sys.executable,'-c','import time; time.sleep(2)'],0.05))
        self.assertLess(time.monotonic()-started,0.8)
        children=[]
        class Child:
            returncode=None
            def __init__(self): self.killed=False
            def wait(self,timeout=None):
                assert len(children)==5, 'Pings were not launched together'
                assert timeout is not None, 'Every wait must be bounded'
                if not self.killed: raise subprocess.TimeoutExpired('ping',timeout)
                return -9
            def poll(self): return None if not self.killed else -9
            def kill(self): self.killed=True
        def spawn(*args,**kwargs):
            child=Child(); children.append(child); return child
        with patch.object(watch.subprocess,'Popen',side_effect=spawn):
            self.assertEqual(commands.pings([('wlan0','x')]*5),[False]*5)
        self.assertTrue(all(child.killed for child in children))
        commands.deadline=0
        with patch.object(watch.subprocess,'run') as spawn:
            self.assertIsNone(commands.run(['anything']))
            spawn.assert_not_called()


if __name__ == '__main__': unittest.main()
