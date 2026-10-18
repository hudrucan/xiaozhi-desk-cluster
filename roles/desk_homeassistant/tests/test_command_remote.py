"""Thin native command routing must never fall back, retry a write or rebind."""
import importlib.util
import json
import pathlib
import sys
import tempfile
import types
import unittest
from unittest.mock import Mock,patch
FILES=pathlib.Path(__file__).resolve().parents[1]/'files'
CAM=FILES.parents[1]/'desk_camera/files'
W='tapo_c222_163';G='a'*32
class SDK:
    host='192.168.1.163'
    def moveMotor(self,*a,**kw):return ('native-move',a,kw)
    def setPreset(self,*a,**kw):return ('native-preset',a,kw)
    def setPrivacyMode(self,*a,**kw):return ('native-privacy',a,kw)
    def reboot(self):return 'native-reboot'
    def executeFunction(self,*a,**kw):return 'native-wire'
    def performRequest(self,*a,**kw):return 'native-wire'
class RemoteTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup);root=pathlib.Path(self.tmp.name)
        for src in (FILES/'desk_cluster/command_remote.py',CAM/'command_contract.py'):(root/src.name).write_bytes(src.read_bytes())
        spec=importlib.util.spec_from_file_location('command_remote_test',root/'command_remote.py');self.m=importlib.util.module_from_spec(spec);spec.loader.exec_module(self.m)
        (root/'.storage').mkdir();(root/'.storage/desk_camera_command_authority').write_text(json.dumps(dict(version=1,workloads=[W])))
        self.hass=types.SimpleNamespace(config=types.SimpleNamespace(path=lambda p:str(root/p)))
        class Tapo(SDK):pass
        self.Tapo=Tapo;self.patch=patch.dict(sys.modules,{'pytapo':types.SimpleNamespace(Tapo=Tapo)});self.patch.start();self.addCleanup(self.patch.stop)
        self.state=self.m.install(self.hass);self.client=Tapo()
    def receipt(self,state='acknowledged',reason='accepted'):
        return dict(version=1,workload=W,generation=G,request_id='b'*32,state=state,reason=reason,observed=None)
    def test_selected_native_methods_route_exact_operations_and_refresh_state(self):
        for call,command,args in [(lambda:self.client.moveMotor(15,0),'ptz_move',dict(x=15,y=0)),(lambda:self.client.setPrivacyMode(False),'privacy_set',dict(enabled=False)),(lambda:self.client.setPreset(1),'preset_goto',dict(preset='1'))]:
            with patch.object(self.m,'execute',return_value=self.receipt()) as execute:
                self.assertEqual(call(),dict(error_code=0));execute.assert_called_once_with(W,command,args)
            self.assertTrue(self.client._desk_command_refresh)
        self.assertEqual(self.state['acknowledged'],3)
    def test_unknown_command_raises_without_core_fallback(self):
        with patch.object(self.m,'execute',return_value=self.receipt('unknown','outcome_unknown')) as execute:
            with self.assertRaisesRegex(ValueError,'no automatic resend'):self.client.moveMotor(15,0)
            execute.assert_called_once();self.assertTrue(self.client._desk_command_refresh)
    def test_raw_selected_wire_commands_cannot_bypass_owner(self):
        for method in ('motorMove','motorMoveToPreset','setLensMaskConfig'):
            with self.assertRaises(ValueError):self.client.executeFunction(method,{})
            with self.assertRaises(ValueError):self.client.executeFunction(method=method,params={})
            with self.assertRaises(ValueError):self.client.performRequest(dict(method='multipleRequest',params=dict(requests=[dict(method=method)])))
        self.assertEqual(self.client.executeFunction('getDeviceInfo',{}),'native-wire')
        self.assertEqual(self.client.reboot(),'native-reboot')
    def test_nonselected_sdk_path_and_fixed_guard_scope(self):
        self.client.host='192.168.1.254';self.assertEqual(self.client.moveMotor(5,0)[0],'native-move')
        self.assertEqual(self.client.executeFunction('motorMove',{}),'native-wire')
        self.assertIs(self.m.install(self.hass),self.state)
        with patch.object(self.m,'policy',return_value=set()):
            with self.assertRaises(ValueError):self.m.install(self.hass)
    def test_busy_and_sdk_retry_are_rejected_before_rpc(self):
        self.client._desk_command_lock=__import__('threading').Lock();self.client._desk_command_lock.acquire()
        with patch.object(self.m,'execute',side_effect=AssertionError('No RPC')):
            with self.assertRaises(ValueError):self.client.moveMotor(5,0)
            with self.assertRaises(ValueError):self.client.setPreset('1',retry=True)

class TicketTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup);root=pathlib.Path(self.tmp.name)
        for src in (FILES/'desk_cluster/command_remote.py',CAM/'command_contract.py'):(root/src.name).write_bytes(src.read_bytes())
        spec=importlib.util.spec_from_file_location('command_tickets_test',root/'command_remote.py');self.m=importlib.util.module_from_spec(spec);spec.loader.exec_module(self.m)
        self.fleet=dict(cameras=[dict(workload=W,owner='deskb1x',ready=True,generation=G)])
    def test_lost_submit_ack_polls_original_ticket_and_owner_without_resubmit(self):
        calls=[];submitted=[]
        def rpc(action,value=None):
            calls.append((action,value))
            if action=='cameras':return self.fleet
            req=value['request'];self.assertEqual(value['node'],'deskb1x');self.assertEqual(req['generation'],G)
            if req['operation']=='submit':submitted.append(req);raise TimeoutError('Lost ACK')
            return self.m.contract.response(req,'acknowledged','accepted')
        with patch.object(self.m,'rpc',side_effect=rpc),patch.object(self.m.time,'sleep'):
            value=self.m.execute(W,'ptz_move',dict(x=15,y=0))
        self.assertEqual(value['state'],'acknowledged');self.assertEqual(len(submitted),1)
        self.assertEqual([c[0] for c in calls],['cameras','camera_command','camera_command'])
        self.assertEqual(calls[-1][1]['request']['request_id'],submitted[0]['request_id'])
    def test_unknown_ticket_or_bridge_outage_never_creates_a_new_submit(self):
        clock=[0];submits=[];polls=[]
        def rpc(action,value=None):
            if action=='cameras':return self.fleet
            req=value['request']
            if req['operation']=='submit':submits.append(req);raise OSError('offline')
            polls.append(req);return self.m.contract.response(req,'rejected','unknown_request')
        with patch.object(self.m,'rpc',side_effect=rpc),patch.object(self.m.time,'monotonic',side_effect=lambda:clock[0]),patch.object(self.m.time,'sleep',side_effect=lambda _:clock.__setitem__(0,clock[0]+5)):
            value=self.m.execute(W,'ptz_move',dict(x=15,y=0))
        self.assertEqual(value['state'],'unknown');self.assertEqual(len(submits),1);self.assertTrue(polls)
        self.assertEqual({q['request_id'] for q in polls},{submits[0]['request_id']})
    def test_mismatched_receipt_and_invalid_arguments_never_rebind(self):
        calls=[]
        def rpc(action,value=None):
            calls.append(action)
            if action=='cameras':return self.fleet
            receipt=self.m.contract.response(value['request'],'acknowledged','accepted');receipt['generation']='c'*32;return receipt
        with patch.object(self.m,'rpc',side_effect=rpc):
            with self.assertRaises(ValueError):self.m.execute(W,'ptz_move',dict(x=5,y=0))
            self.assertEqual(calls,['cameras','camera_command'])
            calls.clear()
            with self.assertRaises(ValueError):self.m.execute(W,'format',{})
            self.assertFalse(calls)
if __name__=='__main__':unittest.main()
