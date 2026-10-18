import asyncio
import copy
import importlib.util
import json
import pathlib
import sys
import tempfile
import time
import types
import unittest
from unittest.mock import patch
FILES=pathlib.Path(__file__).resolve().parents[1]/'files'
CAM=FILES.parents[1]/'desk_camera/files'
sys.path.insert(0,str(CAM))
from observation_contract import COUNTS
W='tapo_c222_163';G='a'*32

def response():
    data={k:[False]*n for k,n in COUNTS.items()}
    data['getDeviceInfo']=[{'device_info':{'basic_info':{'model':'C222'}}}]
    data['getPresetConfig']=[{'preset':{'id':'test'}}]
    return dict(version=1,workload=W,generation=G,state='ready',snapshot='b'*32,observed_at=time.time(),channels=[],data=data)
class SDK:
    host='192.168.1.163';childID=None;isKLAP=False;hass=None
    def getMost(self,*a):raise AssertionError('No Core SDK read fallback')
    def processPresetsResponse(self,v):return v['preset']
    def setPrivacyMode(self,value):return ('existing-command',value)
class RemoteTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup);root=pathlib.Path(self.tmp.name)
        for src in (FILES/'desk_cluster/observation_remote.py',CAM/'observation_contract.py'):(root/src.name).write_bytes(src.read_bytes())
        spec=importlib.util.spec_from_file_location('observation_remote_test',root/'observation_remote.py');self.m=importlib.util.module_from_spec(spec);spec.loader.exec_module(self.m)
        (root/'.storage').mkdir();(root/'.storage/desk_camera_observation_authority').write_text(json.dumps(dict(version=1,workloads=[W])))
        self.hass=types.SimpleNamespace(config=types.SimpleNamespace(path=lambda p:str(root/p)))
        class Tapo(SDK):pass
        self.Tapo=Tapo;self.sdk=patch.dict(sys.modules,{'pytapo':types.SimpleNamespace(Tapo=Tapo)});self.sdk.start();self.addCleanup(self.sdk.stop)
        self.state=self.m.install(self.hass);self.client=Tapo()
    def test_native_sdk_fields_and_mapping_input_are_preserved(self):
        v=response()
        with patch.object(self.m,'rpc',return_value=v):self.assertIs(self.client.getMost([],[]),v['data'])
        self.assertEqual(self.client.presets,{'id':'test'});self.assertEqual(self.client.basicInfo,v['data']['getDeviceInfo']);self.assertEqual(self.state['calls'],1)
    def test_pending_waits_for_snapshot_but_failure_never_uses_core_sdk(self):
        v=response()
        with patch.object(self.m,'rpc',side_effect=[dict(v,state='pending'),v]),patch.object(self.m.time,'sleep'):
            self.client.getMost([],[])
        with patch.object(self.m,'rpc',side_effect=OSError('offline')) as rpc:
            with self.assertRaises(ValueError):self.client.getMost([],[])
            with self.assertRaises(ValueError):self.client.getMost([],[])
            self.assertEqual(rpc.call_count,1)
    def test_expired_cross_workload_or_mismatched_channel_data_fails(self):
        for key,value in (('observed_at',time.time()-5),('workload','tapo_c222_160'),('channels',['0'])):
            v=response();v[key]=value;self.client._desk_status_retry=0
            with patch.object(self.m,'rpc',return_value=v):
                with self.assertRaises(ValueError):self.client.getMost([],[])
    def test_explicit_refresh_requests_new_snapshot(self):
        self.client._desk_status_snapshot='c'*32
        with patch.object(self.m,'forced',return_value=True),patch.object(self.m,'rpc',return_value=response()) as rpc:
            self.client.getMost([],[])
        self.assertEqual(rpc.call_args.args[0],dict(workload=W,after='c'*32))
    def test_command_requires_new_snapshot_even_without_native_force(self):
        self.client._desk_status_snapshot='c'*32;self.client._desk_command_refresh=True
        with patch.object(self.m,'forced',return_value=False),patch.object(self.m,'rpc',return_value=response()) as rpc:
            self.client.getMost([],[])
        self.assertEqual(rpc.call_args.args[0]['after'],'c'*32);self.assertFalse(self.client._desk_command_refresh)
    def test_guard_is_installed_once_and_scope_is_fixed(self):
        self.assertIs(self.m.install(self.hass),self.state)
        with patch.object(self.m,'policy',return_value=set()):
            with self.assertRaises(ValueError):self.m.install(self.hass)
        for args in ((['getAudioConfig'],[]),([],['3'])):
            with patch.object(self.m,'rpc',return_value=response()):
                with self.assertRaises(ValueError):self.client.getMost(*args)
    def test_command_method_is_not_replaced_or_routed(self):
        self.assertIs(self.Tapo.setPrivacyMode,SDK.setPrivacyMode)
        with patch.object(self.m,'rpc',side_effect=AssertionError('No command RPC')):
            self.assertEqual(self.client.setPrivacyMode(True),('existing-command',True))
    def test_nonselected_camera_keeps_original_sdk(self):
        self.client.host='192.168.1.254'
        with self.assertRaisesRegex(AssertionError,'Core SDK'):self.client.getMost([],[])

class ForceTests(unittest.IsolatedAsyncioTestCase):
    async def test_runtime_read_is_scheduled_on_ha_loop(self):
        with tempfile.TemporaryDirectory() as d:
            root=pathlib.Path(d)
            for src in (FILES/'desk_cluster/observation_remote.py',CAM/'observation_contract.py'):(root/src.name).write_bytes(src.read_bytes())
            spec=importlib.util.spec_from_file_location('observation_force_test',root/'observation_remote.py');m=importlib.util.module_from_spec(spec);spec.loader.exec_module(m)
            client=types.SimpleNamespace();hass=types.SimpleNamespace(loop=asyncio.get_running_loop(),data={'tapo_control':{'entry':dict(controller=client,lastUpdate=0)}});client.hass=hass
            self.assertTrue(await asyncio.to_thread(m.forced,client))
            hass.data['tapo_control']['entry']['lastUpdate']=10
            self.assertFalse(await asyncio.to_thread(m.forced,client))
if __name__=='__main__':unittest.main()
