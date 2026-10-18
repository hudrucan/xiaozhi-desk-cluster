"""Exercise the installed pinned SDK with ALL socket/HTTP I/O mocked.

Requires pytapo 3.4.26 and its dependencies on PYTHONPATH; skips if absent.
No SDK/vendor source is modified. Real SDK performRequest/executeFunction/send
and HTTP retry code run; only __init__/authentication state and HTTP are faked.
"""
import json
import pathlib
import os
import socket
import sys
import tempfile
import time
import unittest
from unittest.mock import Mock, patch
sys.path.insert(0,str(pathlib.Path(__file__).resolve().parents[1]/'files'))
import command_contract as c
import command_worker as m
import observation_worker as obs
try:
    import requests
    from pytapo import Tapo
    from pytapo.asyncHandler import AsyncHandler
    from pytapo.logger import Logger
    from pytapo.transport.transport import Transport
except ImportError as error:
    if os.environ.get('DESK_REQUIRE_COMMAND_SDK_TESTS') == '1':raise
    Tapo=None
W='tapo_c222_163';G='a'*32

def request(command='ptz_move',args=None):return dict(version=1,workload=W,generation=G,request_id='b'*32,operation='submit',command=command,arguments=args or dict(x=15,y=0),deadline=time.time()+15)

@unittest.skipIf(Tapo is None,'Pinned SDK/dependencies not available')
class SDKTests(unittest.TestCase):
    def setUp(self):
        self.stack=__import__('contextlib').ExitStack();self.addCleanup(self.stack.close)
        self.tmp=self.stack.enter_context(tempfile.TemporaryDirectory());self.credentials=pathlib.Path(self.tmp)/'credentials.json'
        self.credentials.write_text(json.dumps({W:dict(ip_address='192.168.1.163',cloud_password='synthetic-test-only')}))
        self.stack.enter_context(patch.object(m,'CREDENTIALS',self.credentials))
        self.stack.enter_context(patch.object(obs,'lease',return_value=(G,10)))
        self.stack.enter_context(patch.object(obs,'policy',return_value={W}))
        self.stack.enter_context(patch.object(socket.socket,'connect',side_effect=AssertionError('No network allowed')))
        self.calls=[];self.mode='ack';self.privacy='on';self.before=Mock();self.permitted=True
        def http(session,method,url,**kwargs):
            value=json.loads(kwargs['data']);items=value['params']['requests'];method_name=items[0]['method'];writing=method_name in ('motorMove','motorMoveToPreset','setLensMaskConfig','setCruise')
            if writing:
                self.calls.append((method_name,kwargs.get('timeout')))
                if self.mode=='timeout':raise requests.exceptions.ReadTimeout('lost ACK')
                if self.mode=='reset':raise requests.exceptions.ConnectionError('connection reset by peer')
                if self.mode=='retryable':return Mock(status_code=200,text='{"error_code":-1}',json=lambda:{'error_code':-1})
                if self.mode=='owner_loss':self.permitted=False;raise requests.exceptions.ReadTimeout('owner lost')
            result={'lens_mask':{'lens_mask_info':{'enabled':self.privacy}}} if method_name=='getLensMaskConfig' else {}
            item=dict(method=method_name,error_code=-64303 if writing and self.mode=='implicit_write' else 0,result=result)
            out=dict(error_code=0,result=dict(responses=[item]))
            return Mock(status_code=200,text=json.dumps(out),json=lambda:out)
        self.stack.enter_context(patch.object(requests.sessions.Session,'request',new=http))
        def initialize(client,host,user,password,*args,**kwargs):
            client.host=host;client.childID=None;client.isKLAP=False;client.hass=None;client.presets={'1':'Desk'}
            client.asyncHandler=AsyncHandler(None)
            client.transport=Transport(host,443,user,password,Logger(False,False),method='pytapo',asyncHandler=client.asyncHandler,retryStok=False,reuseSession=False)
            client.transport.stok='synthetic';client.transport.isSecureConnectionCached=False
            client.performRequest(dict(method='multipleRequest',params=dict(requests=[dict(method='getDeviceInfo',params=dict(device_info=dict(name=['basic_info'])))])))
        self.stack.enter_context(patch.object(Tapo,'__init__',new=initialize))
        self.backend=m.Backend(lambda req:self.permitted)
    def run_command(self,req):return self.backend.execute(req,self.before,time.monotonic()+12)
    def test_ack_uses_real_sdk_payload_and_one_bounded_http_dispatch(self):
        value=self.run_command(request());self.assertEqual(value['state'],'acknowledged')
        self.assertEqual(self.calls,[('motorMove',(1,1))]);self.before.assert_called_once()
    def test_timeout_reset_retryable_error_and_implicit_cruise_never_send_twice(self):
        for mode in ('timeout','reset','retryable','implicit_write'):
            self.mode=mode;self.calls.clear();self.before.reset_mock()
            value=self.run_command(request());self.assertEqual(value['state'],'unknown',mode)
            self.assertEqual(self.calls,[('motorMove',(1,1))],mode);self.before.assert_called_once()
    def test_unknown_privacy_reconciles_by_read_without_second_write(self):
        self.mode='timeout';value=self.run_command(request('privacy_set',dict(enabled=True)))
        self.assertEqual(value['state'],'reconciled');self.assertEqual(value['observed']['privacy'],True)
        self.assertEqual(self.calls,[('setLensMaskConfig',(1,1))])
        self.privacy='off';self.calls.clear();value=self.run_command(request('privacy_set',dict(enabled=True)))
        self.assertEqual(value['state'],'unknown');self.assertFalse(value['observed']['matches']);self.assertEqual(len(self.calls),1)
    def test_preset_is_validated_and_unknown_preset_never_dispatches(self):
        value=self.run_command(request('preset_goto',dict(preset='9')))
        self.assertEqual(value['reason'],'invalid_preset');self.assertFalse(self.calls);self.before.assert_not_called()
        value=self.run_command(request('preset_goto',dict(preset='1')))
        self.assertEqual(value['state'],'acknowledged');self.assertEqual(self.calls,[('motorMoveToPreset',(1,1))])
    def test_owner_loss_during_send_is_uncertain_without_retry(self):
        self.mode='owner_loss';value=self.run_command(request());self.assertEqual(value['state'],'unknown');self.assertEqual(len(self.calls),1)
    def test_unavailable_lease_and_journal_failure_stop_before_device_bytes(self):
        self.permitted=False;value=self.run_command(request());self.assertEqual(value['state'],'rejected');self.assertFalse(self.calls)
        self.permitted=True;self.before.side_effect=OSError('journal full')
        value=self.run_command(request());self.assertEqual(value['state'],'rejected');self.assertFalse(self.calls)
    def test_changed_sdk_hash_refuses_before_any_device_write(self):
        m.verify_sdk.cache_clear()
        try:
            with patch.dict(m.SDK_HASHES,{'__init__.py':'0'*64}):
                value=self.run_command(request());self.assertEqual(value['state'],'rejected');self.assertFalse(self.calls)
        finally:m.verify_sdk.cache_clear()
if __name__=='__main__':unittest.main()
