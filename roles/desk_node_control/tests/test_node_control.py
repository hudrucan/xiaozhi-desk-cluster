"""Offline reboot coordination; never executes a real reboot."""
import copy
import http.client
import threading
import importlib.util
import json
from pathlib import Path
import tempfile
import time
import unittest
from unittest.mock import Mock, patch

PATH = Path(__file__).resolve().parents[1] / 'files/node_control.py'
spec = importlib.util.spec_from_file_location('node_control', PATH)
nc = importlib.util.module_from_spec(spec)
spec.loader.exec_module(nc)
MEMBERS = [{'id':f'deskb{i}x', 'private':f'10.10.10.{10+i}', 'management':f'192.168.1.{100+i}'} for i in (1,2,3)]


class ControlTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.controls = {}
        self.health = {}
        self.callbacks = []
        self.quiet_patch = patch.object(nc, 'DNS_QUIET_SECONDS', 0)
        self.quiet_patch.start(); self.addCleanup(self.quiet_patch.stop)
        for member in MEMBERS:
            node = member['id']
            config = {'node':node,'port':8011,'members':MEMBERS,'key':'ab'*32,'public_host':'192.168.1.186',
                      'dashboard_port':8010,'dns_port':5353,'nats_port':8222}
            control = nc.Control(config, Path(self.directory.name)/node/'state.json', reboot=Mock())
            self.controls[node] = control
            self.health[node] = {'node':node,'boot_id':'old-boot','ready':True,
                                 'revisions':{'xiaozhi':11,'dns':5,'monitoring':4},'metadata':{
                                     'dns_health':{'mode':'adguard','eligible_backends':['deskb1x','deskb2x','deskb3x']},
                                     'maintenance_supported':True,'activity':{'asr':0,'llm':0,'tts':0},
                                     'core':0,'mqtt':0,'connections':{'core':0,'mqtt':0},
                                     'admission':{'worker':True,'core':True,'mqtt':True},
                                     'vip':{'xiaozhi':node=='deskb1x','dns':node=='deskb2x'}}}
            def withdraw(value, node=node):
                for state in self.health.values():
                    state['metadata']['dns_health']['eligible_backends'] = [n for n in state['metadata']['dns_health']['eligible_backends'] if n!=node]
            control.write_admission = Mock(side_effect=withdraw)
            control.vip_service = Mock()
            control.health = lambda node=node: copy.deepcopy(self.health[node])
            control.peer = lambda target,path,body: self.controls[target].peer_action(path,body)
        self.command_patch = patch.object(nc.subprocess,'run',return_value=Mock(stdout='active\n'))
        self.command_patch.start(); self.addCleanup(self.command_patch.stop)
        self.boot_patch = patch.object(nc, 'boot_id', return_value='old-boot')
        self.boot_patch.start(); self.addCleanup(self.boot_patch.stop)
        callbacks = self.callbacks
        class Timer:
            def __init__(self, seconds, callback): self.callback = callback
            def start(self): callbacks.append(self.callback)
        self.timer_patch = patch.object(nc.threading,'Timer',Timer)
        self.timer_patch.start(); self.addCleanup(self.timer_patch.stop)

    def challenge(self, target='deskb3x'):
        return self.controls['deskb1x'].challenge({'node':target})['token']

    def test_confirmation_requires_healthy_matching_revisions(self):
        self.health['deskb2x']['ready'] = False
        with self.assertRaises(nc.Failure): self.challenge()
        self.health['deskb2x']['ready'] = True
        self.health['deskb2x']['revisions']['dns'] = 6
        with self.assertRaises(nc.Failure): self.challenge()
        self.assertFalse(self.callbacks)

    def test_optional_vpn_is_included_in_required_handoff(self):
        target=self.controls['deskb3x']
        self.assertEqual(target.managed_vips(self.health), ('dns','xiaozhi'))
        with patch.object(nc.Path, 'is_file', return_value=True):
            self.assertEqual(target.managed_vips(self.health), ('dns','xiaozhi','vpn'))
        for node,value in self.health.items():
            value['metadata']['vip']['vpn']=node=='deskb3x'
            value['metadata']['vpn_ready']=True
        self.assertEqual(target.managed_vips(self.health), ('dns','xiaozhi','vpn'))

    def test_missing_native_dns_survivor_rejects_before_any_pause(self):
        self.health['deskb1x']['metadata']['dns_health']['eligible_backends']=['deskb1x','deskb3x']
        with self.assertRaises(nc.Failure):self.challenge()
        self.assertFalse(self.callbacks)
        self.controls['deskb3x'].write_admission.assert_not_called()

    def test_preparation_never_reboots_and_boot_change_invalidates_confirmation(self):
        token = self.challenge()
        self.assertFalse(self.callbacks)
        self.health['deskb3x']['boot_id'] = 'new-boot'
        with self.assertRaises(nc.Failure): self.controls['deskb1x'].confirm(token)
        self.assertFalse(self.callbacks)

    def test_two_survivors_hold_exclusive_durable_lease_and_execution_is_not_retried(self):
        token = self.challenge()
        accepted = self.controls['deskb1x'].confirm(token)
        self.assertTrue(accepted['accepted'])
        self.assertFalse(accepted['uncertain'])
        self.assertEqual(len(self.callbacks),1)
        one, two = self.controls['deskb1x'], self.controls['deskb2x']
        self.assertEqual(one.state['lease'],two.state['lease'])
        self.assertIsNone(self.controls['deskb3x'].state['lease'])
        restored = nc.Control(one.config,one.path,reboot=Mock())
        self.assertEqual(restored.active(),one.active())
        again = one.confirm(token)
        self.assertTrue(again['uncertain'])
        self.assertEqual(len(self.callbacks),1)
        self.callbacks[0]()
        self.controls['deskb3x'].reboot.assert_called_once_with()
        with self.assertRaises(nc.Failure): self.challenge('deskb2x')

    def test_target_entrypoint_hands_off_to_survivor_and_recovers_after_new_ready_boot(self):
        token = self.challenge()
        self.controls['deskb3x'].confirm(token)
        self.health['deskb3x'] = {'node':'deskb3x','ready':False}
        self.assertEqual(self.controls['deskb2x'].status()['operation']['state'],'offline')
        self.health['deskb3x'] = {'node':'deskb3x','boot_id':'new-boot','ready':False}
        self.assertEqual(self.controls['deskb2x'].status()['operation']['state'],'starting')
        self.health['deskb3x'].update(ready=True,revisions={'xiaozhi':11,'dns':5,'monitoring':4})
        self.assertEqual(self.controls['deskb2x'].status()['operation']['state'],'rejoined')
        self.assertIsNone(self.controls['deskb1x'].active())
        self.assertIsNone(self.controls['deskb2x'].active())
        # Another browser/observer must still see completion after lock release.
        self.assertEqual(self.controls['deskb1x'].status()['operation']['state'],'rejoined')
        restored = nc.Control(self.controls['deskb2x'].config,self.controls['deskb2x'].path,reboot=Mock())
        self.assertEqual(restored.state['completed']['state'],'rejoined')

    def test_failed_second_lease_never_executes_and_releases_own_partial_lock(self):
        token = self.challenge()
        original = self.controls['deskb1x'].peer
        def failing(node,path,value):
            if node=='deskb2x' and path=='/peer/acquire': raise nc.Failure(503,'fixture')
            return original(node,path,value)
        self.controls['deskb1x'].peer = failing
        with self.assertRaises(nc.Failure): self.controls['deskb1x'].confirm(token)
        self.assertIsNone(self.controls['deskb1x'].active())
        self.assertFalse(self.callbacks)

    def test_direct_execution_without_two_leases_is_rejected(self):
        value = {'id':'a'*32,'node':'deskb3x','boot_id':'old-boot','started':int(time.time()),'expires':int(time.time())+500,'action':'reboot'}
        with self.assertRaises(nc.Failure): self.controls['deskb3x'].peer_action('/peer/execute',value)
        self.assertFalse(self.callbacks)

    def test_peer_authentication_rejects_replay_and_body_tampering(self):
        control = self.controls['deskb1x']
        raw, stamp, nonce = b'{}', str(int(time.time())), 'c'*32
        headers = {'X-Desk-Time':stamp,'X-Desk-Nonce':nonce,
                   'X-Desk-Signature':control.signature('/peer/state',raw,stamp,nonce)}
        control.authenticate('/peer/state',raw,headers)
        with self.assertRaises(nc.Failure): control.authenticate('/peer/state',raw,headers)
        headers['X-Desk-Nonce'] = 'd'*32
        with self.assertRaises(nc.Failure): control.authenticate('/peer/state',b'{"node":"other"}',headers)

    def test_http_requires_same_origin_confirmation_and_does_not_expose_peer_api(self):
        server = nc.ThreadingHTTPServer(('127.0.0.1',0),nc.Handler)
        server.control = self.controls['deskb1x']; server.daemon_threads=True
        thread = threading.Thread(target=server.serve_forever,daemon=True); thread.start()
        try:
            conn=http.client.HTTPConnection('127.0.0.1',server.server_port,timeout=2)
            body=json.dumps({'node':'deskb3x'})
            headers={'Host':'192.168.1.186','Content-Type':'application/json','X-Desk-Action':'confirm-node-operation'}
            conn.request('POST','/api/cluster/operations/prepare',body,headers)
            res=conn.getresponse(); self.assertEqual(res.status,403); res.read()
            conn.request('POST','/api/cluster/operations/prepare',body,{**headers,'Origin':'http://other.example'})
            res=conn.getresponse(); self.assertEqual(res.status,403); res.read()
            conn.request('POST','/api/cluster/operations/prepare',body,{**headers,'Origin':'http://192.168.1.186'})
            res=conn.getresponse(); self.assertEqual(res.status,200); self.assertIn('token',json.loads(res.read()))
            conn.request('POST','/peer/execute',body,headers)
            res=conn.getresponse(); self.assertEqual(res.status,403); res.read()
            conn.request('GET','/api/cluster/operations/reboot')
            res=conn.getresponse(); self.assertEqual(res.status,405); res.read(); conn.close()
            self.assertFalse(self.callbacks)
        finally:
            server.shutdown();server.server_close();thread.join(timeout=2)

    def test_modified_challenge_cannot_schedule_a_reboot(self):
        token = self.challenge()
        with self.assertRaises(nc.Failure): self.controls['deskb1x'].confirm(token[:-1] + ('0' if token[-1]!='0' else '1'))
        self.assertFalse(self.callbacks)

    def test_simultaneous_other_target_cannot_override_a_survivor_lock(self):
        first = self.challenge('deskb3x')
        second = self.challenge('deskb2x')
        self.controls['deskb1x'].confirm(first)
        with self.assertRaises(nc.Failure): self.controls['deskb1x'].confirm(second)
        self.assertEqual(len(self.callbacks),1)
        self.assertEqual(self.controls['deskb1x'].active()['node'],'deskb3x')

    def test_stale_or_invalid_peer_authentication_cannot_take_a_lease(self):
        c = self.controls['deskb1x']
        raw, stamp, nonce = b'{}',str(int(time.time())-100),'a'*32
        headers={'X-Desk-Time':stamp,'X-Desk-Nonce':nonce,'X-Desk-Signature':c.signature('/peer/acquire',raw,stamp,nonce)}
        with self.assertRaises(nc.Failure): c.authenticate('/peer/acquire',raw,headers)
        self.assertIsNone(c.active())

    def test_unsupported_target_is_rejected_before_any_lease_or_drain(self):
        self.health['deskb3x']['metadata']['maintenance_supported'] = False
        with self.assertRaises(nc.Failure): self.challenge()
        self.assertFalse(self.callbacks)
        self.assertIsNone(self.controls['deskb1x'].active())

    def test_vpn_election_is_stopped_and_restored_with_maintenance(self):
        one,target=self.controls['deskb1x'],self.controls['deskb3x']
        for node,value in self.health.items():
            value['metadata']['vip']['vpn']=node=='deskb3x'
            value['metadata']['vpn_ready']=True
        def handoff(service,action):
            if service=='desk-vpn-vip' and action=='stop':
                self.health['deskb3x']['metadata']['vip']['vpn']=False
                self.health['deskb1x']['metadata']['vip']['vpn']=True
        target.vip_service.side_effect=handoff
        token=one.challenge({'node':'deskb3x','action':'maintenance'})['token']
        one.confirm(token);self.callbacks[0]()
        self.assertEqual(target.state['maintenance']['state'],'maintenance')
        target.vip_service.assert_any_call('desk-vpn-vip','stop')
        target.reboot.assert_not_called()
        target.resume(target.state['maintenance']['id'])
        target.vip_service.assert_any_call('desk-vpn-vip','start')
        self.assertIsNone(target.state['maintenance'])

    def test_maintenance_only_releases_vips_without_reboot_and_can_resume(self):
        one,target=self.controls['deskb1x'],self.controls['deskb3x']
        token=one.challenge({'node':'deskb3x','action':'maintenance'})['token']
        one.confirm(token);self.callbacks[0]()
        self.assertEqual(target.state['maintenance']['state'],'maintenance')
        target.reboot.assert_not_called()
        target.vip_service.assert_any_call('keepalived','stop')
        target.resume(target.state['maintenance']['id'])
        target.vip_service.assert_any_call('keepalived','start')
        self.assertIsNone(target.state['maintenance'])
        self.assertIsNone(one.active())

    def test_resume_acknowledges_before_service_recovery_and_keeps_lease(self):
        one,target=self.controls['deskb1x'],self.controls['deskb3x']
        one.confirm(one.challenge({'node':'deskb3x','action':'maintenance'})['token'])
        self.callbacks[0]()
        operation=target.state['maintenance']['id']
        target.vip_service.reset_mock()
        self.assertEqual(target.peer_action('/peer/resume',{'id':operation}),{'resuming':True})
        target.vip_service.assert_not_called()
        self.assertEqual(target.state['maintenance']['state'],'recovering')
        self.assertIsNotNone(one.active())
        target.resume(operation,recovering=True)
        target.vip_service.assert_any_call('keepalived','start')
        self.assertIsNone(one.active())
        calls=target.vip_service.call_count
        target.resume(operation,recovering=True)
        self.assertEqual(target.vip_service.call_count,calls)

    def test_dns_withdrawal_must_be_observed_before_vip_stop_or_reboot(self):
        one,target=self.controls['deskb1x'],self.controls['deskb3x']
        target.write_admission = Mock()  # No native backend withdrawal observed.
        one.confirm(self.challenge())
        with patch.object(nc.time,'sleep',side_effect=nc.Failure(409,'Drain timed out')):
            self.callbacks[0]()
        target.vip_service.assert_not_called()
        target.reboot.assert_not_called()
        self.assertIsNone(target.state['maintenance'])

    def test_existing_dns_response_window_precedes_vip_stop(self):
        one,target=self.controls['deskb1x'],self.controls['deskb3x']
        one.confirm(self.challenge())
        with patch.object(nc,'DNS_QUIET_SECONDS',10), patch.object(nc.time,'sleep',side_effect=nc.Failure(409,'Cancelled')):
            self.callbacks[0]()
        target.vip_service.assert_not_called()
        target.reboot.assert_not_called()

    def test_survivor_failure_cancels_drain_without_stopping_vips_or_reboot(self):
        one,target=self.controls['deskb1x'],self.controls['deskb3x']
        one.confirm(self.challenge())
        self.health['deskb2x']['ready']=False
        self.callbacks[0]()
        target.reboot.assert_not_called()
        target.vip_service.assert_not_called()
        self.assertIsNone(target.state['maintenance'])

    def test_ready_maintenance_reboot_requires_new_token_and_cannot_repeat(self):
        one,target=self.controls['deskb1x'],self.controls['deskb3x']
        first=one.challenge({'node':'deskb3x','action':'maintenance'})['token']
        one.confirm(first);self.callbacks[0]()
        token=one.challenge({'node':'deskb3x','action':'reboot'})['token']
        one.confirm(token)
        self.assertEqual(target.state['maintenance']['state'],'rebooting')
        with self.assertRaises(nc.Failure): one.confirm(token)
        self.callbacks[-1]()
        target.reboot.assert_called_once_with()

    def test_unknown_actions_cannot_execute_commands(self):
        with self.assertRaises(nc.Failure): self.controls['deskb1x'].peer_action('/peer/shell',{'command':'reboot'})
        self.assertFalse(self.callbacks)

    def test_history_is_display_only_bounded_and_excludes_credentials(self):
        c=self.controls['deskb1x']; now=int(time.time())
        records=[]
        for i in range(8):
            records.append({'id':f'{i:032x}','node':'deskb3x','action':'reboot',
                'started':now-100+i,'events':[{'state':'rejoined','at':now-50+i}],
                'boot_id':'private-boot','key':'secret','token':'confirmation'})
        history=c.merge_history([records[:4],records[4:]])
        self.assertEqual(len(history),nc.HISTORY_LIMIT)
        self.assertNotIn('private-boot',json.dumps(history)); self.assertNotIn('secret',json.dumps(history))
        dirty=[{'id':'a'*32,'node':[],'action':'reboot','started':now,'events':[]},
            {'id':'b'*32,'node':'deskb3x','action':'reboot','started':now,
             'events':[{'state':[],'at':now},{'state':'rejoined','at':now+60}]}]
        self.assertEqual(c.merge_history([dirty]),[])
        # A display record never acquires a lease or executes an action.
        self.assertIsNone(c.active()); self.assertFalse(self.callbacks)

    def test_history_survives_target_restart_and_merges_peer_milestones(self):
        one,target=self.controls['deskb1x'],self.controls['deskb3x']
        one.confirm(self.challenge()); self.callbacks[0]()
        record=target.state['history'][0]
        self.assertEqual(record['state'],'rebooting')
        self.assertEqual([e['state'] for e in record['events']],
                         ['accepted','draining','releasing_vips','maintenance','rebooting'])
        restored=nc.Control(target.config,target.path,reboot=Mock())
        self.assertEqual(restored.state['history'],target.state['history'])
        self.health['deskb3x']={'node':'deskb3x','ready':False}
        self.assertEqual(one.status()['operation']['state'],'offline')
        self.health['deskb3x']={'node':'deskb3x','boot_id':'new-boot','ready':True,
            'revisions':{'xiaozhi':11,'dns':5,'monitoring':4},
            'readiness':{'services':True,'configuration':True,'waiting':[]}}
        result=one.status()
        self.assertEqual(result['operation']['state'],'rejoined')
        events=result['history'][0]['events']
        states={e['state'] for e in events}
        self.assertTrue({'draining','rebooting','offline','services_ready','configuration_synced','rejoined'}<=states)
        self.assertNotIn('starting',states) # Missed observations are not fabricated.
        self.assertEqual(self.controls['deskb2x'].status()['history'],result['history'])
        self.assertEqual(len(self.callbacks),1)

    def test_idle_and_repeated_history_reads_do_not_write_state(self):
        c=self.controls['deskb1x']
        with patch.object(c,'save',wraps=c.save) as saved:
            for _ in range(3):self.assertEqual(c.status()['history'],[])
            saved.assert_not_called()
        c.confirm(self.challenge()); self.callbacks[0]()
        c.status()
        with patch.object(c,'save',wraps=c.save) as saved:
            for _ in range(3):c.status()
            saved.assert_not_called()

    def test_resume_history_does_not_claim_reboot_or_rejoin(self):
        one,target=self.controls['deskb1x'],self.controls['deskb3x']
        one.confirm(one.challenge({'node':'deskb3x','action':'maintenance'})['token'])
        self.callbacks[0]()
        target.resume(target.state['maintenance']['id'])
        record=one.status()['history'][0]
        self.assertEqual(record['state'],'resumed')
        self.assertEqual(record['action'],'maintenance')
        self.assertNotIn('rebooting',{e['state'] for e in record['events']})
        target.reboot.assert_not_called()

    def test_history_peer_response_fits_existing_body_limit(self):
        c=self.controls['deskb1x']; now=int(time.time())
        c.state['history']=[{'id':f'{i:032x}','node':'deskb3x','action':'reboot','started':now-100,
            'events':[{'state':state,'at':now-50} for state in nc.HISTORY_ORDER]}
            for i in range(nc.HISTORY_LIMIT)]
        raw=nc.encode(c.peer_action('/peer/state',{}))
        self.assertLess(len(raw),nc.MAX_BODY)

    def test_legacy_state_migrates_without_changing_action_records(self):
        c=self.controls['deskb1x']; old={k:v for k,v in c.state.items() if k!='history'}
        c.path.parent.mkdir(parents=True);c.path.write_text(json.dumps(old))
        restored=nc.Control(c.config,c.path,reboot=Mock())
        self.assertEqual(restored.state['history'],[])
        self.assertEqual({k:v for k,v in restored.state.items() if k!='history'},old)

    def test_history_write_failure_does_not_break_status_or_retry_execution(self):
        c=self.controls['deskb1x']; c.confirm(self.challenge())
        with patch.object(c,'save',side_effect=OSError('fixture disk full')):
            result=c.status()
        self.assertEqual(result['operation']['state'],'accepted')
        self.assertEqual(len(self.callbacks),1)

    def test_readiness_uses_fresh_local_metadata_without_new_probes(self):
        c=self.controls['deskb1x']
        value={'id':c.node,'boot_id':'old-boot','observed_at':time.strftime('%Y-%m-%dT%H:%M:%SZ',time.gmtime()),
            'components':{'worker':'ready','core':'ready','mqtt':'ready','dns':'ready'},
            'configuration':{k:{'state':'synced','revision':1} for k in ('xiaozhi','dns','monitoring')}}
        value['configuration']['xiaozhi']['state']='pending'
        c.get=Mock(return_value=value)
        result=nc.Control.health(c)
        self.assertEqual(result['readiness'],{'services':True,'configuration':False,'waiting':['xiaozhi config']})
        self.assertFalse(result['ready']);c.get.assert_called_once()
        c.cached_health=(0,None)
        value['observed_at']=time.strftime('%Y-%m-%dT%H:%M:%SZ',time.gmtime(time.time()-30))
        self.assertNotIn('readiness',nc.Control.health(c))

    def test_old_completed_record_does_not_fabricate_past_readiness_steps(self):
        one=self.controls['deskb1x'];one.confirm(self.challenge())
        self.health['deskb3x']={'node':'deskb3x','boot_id':'new-boot','ready':True,
            'revisions':{'xiaozhi':11,'dns':5,'monitoring':4}}
        one.status()
        self.health['deskb3x']['readiness']={'services':True,'configuration':True,'waiting':[]}
        result=one.status()
        states={e['state'] for e in result['history'][0]['events']}
        self.assertNotIn('services_ready',states);self.assertNotIn('configuration_synced',states)


if __name__=='__main__': unittest.main()
