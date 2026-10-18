import asyncio
import copy
import pathlib
import sys
import tempfile
import time
import types
import unittest
from unittest.mock import patch
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]/'files'))
import observation_contract as c
import observation_worker as w

W='tapo_c222_163';G='a'*32

def data():
    result={k:[False]*n for k,n in c.COUNTS.items()}
    result['getDeviceInfo']=[{'device_info':{'basic_info':{'model':'C222','device_type':'SMART.IPCAMERA'}}}]
    return result

def request(after=None):return dict(version=1,workload=W,generation=G,after=after)
def value():return dict(version=1,workload=W,generation=G,state='ready',snapshot='b'*32,observed_at=time.time(),channels=[],data=data())

class ContractTests(unittest.TestCase):
    def test_pinned_batch_and_scope(self):
        self.assertEqual(sum(c.COUNTS.values()),87);self.assertEqual(len(c.COUNTS),82)
        c.batch(data());c.read_request(dict(method='multipleRequest',params={'requests':c.READ_REQUESTS}))
        c.snapshot(value(),request(),time.time())
    def test_writes_handles_unknown_nested_rpc_and_changed_read_parameters_rejected(self):
        for operation in ('setCruise','setLensMaskConfig','getUserID','controlChild','get','multipleRequest'):
            with self.assertRaises(ValueError):c.read_request(dict(method='multipleRequest',params={'requests':[{'method':operation}]}))
        item=copy.deepcopy(c.READ_REQUESTS[0]);item['params']={'other':'all'}
        with self.assertRaises(ValueError):c.read_request(dict(method='multipleRequest',params={'requests':[item]}))
        with self.assertRaises(ValueError):c.read_request({'method':'getDeviceInfo'})
    def test_snapshot_cannot_cross_generation_workload_refresh_or_age(self):
        for key,replacement in (('generation','c'*32),('workload','tapo_c222_160'),('observed_at',time.time()-3),('snapshot','opaque'),('version',True)):
            v=value();v[key]=replacement
            with self.assertRaises(ValueError):c.snapshot(v,request(),time.time())
        with self.assertRaises(ValueError):c.snapshot(value(),request('b'*32),time.time())
    def test_no_private_data_or_unbounded_response(self):
        for k in ('password','user_id','playerID','stok','local_key','authorization'):
            v=data();v['getDeviceInfo'][0][k]='private'
            with self.assertRaises(ValueError):c.batch(v)
        v=data();v['extra']=False
        with self.assertRaises(ValueError):c.batch(v)
        v=data();v['getLedStatus']*=2
        with self.assertRaises(ValueError):c.batch(v)
        for v in (float('nan'),{'large':['x'*1025]},[[[[[[[[[[[[[0]]]]]]]]]]]]]):
            with self.assertRaises(ValueError):c.safe_tree(v)
    def test_policy_is_explicit_and_validated(self):
        with tempfile.TemporaryDirectory() as d:
            p=pathlib.Path(d)/'policy';self.assertEqual(c.policy(p),frozenset())
            p.write_text('{"version":1,"workloads":["'+W+'"]}');self.assertEqual(c.policy(p),{W})
            p.write_text('{"version":1,"workloads":["unknown"]}')
            with self.assertRaises(ValueError):c.policy(p)

class Transport:
    method='pytapo'
    def __init__(self):self.calls=[];self.during=lambda:None
    def _request(self,*args,**kwargs):self.calls.append(kwargs);self.during();return {}
class SDK:
    def __init__(self,*args,**kwargs):
        self.transport=Transport();self.userID=False
        self.performRequest(dict(method='multipleRequest',params={'requests':[next(r for r in c.READ_REQUESTS if r['method']=='getDeviceInfo')]}))
    def performRequest(self,v,*args,**kwargs):return self.transport._request('POST','camera',timeout=99)
    def executeFunction(self,method,*args):
        # Mirror SDK -64303 fallback: a read error tries an implicit write.
        return self.performRequest(dict(method='multipleRequest',params={'requests':[{'method':'setCruise','params':{'enabled':False}}]}))
    def close(self):pass
class GuardTests(unittest.TestCase):
    def setUp(self):
        self.current=(G,10)
        self.stack=__import__('contextlib').ExitStack();self.addCleanup(self.stack.close)
        self.stack.enter_context(patch.object(w,'lease',side_effect=lambda _:self.current))
        self.stack.enter_context(patch.object(w,'policy',return_value={W}))
        self.stack.enter_context(patch.dict(sys.modules,{'pytapo':types.SimpleNamespace(Tapo=SDK)}))
        self.client=w.create_client(W,G,dict(ip_address=c.HOSTS[W],cloud_password='test'),time.monotonic()+6)
    def test_login_has_bounded_transport_and_never_allocates_media(self):
        self.assertFalse(self.client.userID);self.assertEqual(self.client.transport.calls,[{'timeout':(1,1)}])
        for method in (self.client.getUserID,self.client.getMediaSession):
            with self.assertRaises(ValueError):method()
    def test_sdk_implicit_write_fallback_cannot_reach_transport(self):
        with self.assertRaises(ValueError):self.client.executeFunction('getDeviceInfo',{})
        self.assertEqual(len(self.client.transport.calls),1)
    def test_unaudited_transport_cannot_be_armed(self):
        self.client._bounded_observation=False;self.client.transport.method='tpap'
        with self.assertRaises(ValueError):self.client.performRequest(dict(method='multipleRequest',params={'requests':[c.READ_REQUESTS[0]]}))
        self.assertEqual(len(self.client.transport.calls),1)
    def test_permit_expiration_generation_and_deadline_are_fail_closed(self):
        v=dict(method='multipleRequest',params={'requests':[c.READ_REQUESTS[0]]})
        for current in (None,(G,2.9),('c'*32,10)):
            self.current=current
            with self.assertRaises(ValueError):self.client.performRequest(v)
        self.current=(G,10);self.client._deadline=time.monotonic()-1
        with self.assertRaises(ValueError):self.client.performRequest(v)
        self.client._deadline=time.monotonic()+6;self.client.transport.during=lambda:setattr(self,'current',None)
        with self.assertRaises(ValueError):self.client.performRequest(v)

class WorkerTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.worker=w.Observations();self.active={W:G}
        self.scope=patch.object(self.worker,'scope',side_effect=lambda:self.active.copy());self.scope.start();self.addCleanup(self.scope.stop)
    async def test_pending_coalesces_and_old_generation_never_escapes(self):
        self.assertEqual(self.worker.handle(request())['state'],'pending')
        self.worker.values[W]=value();self.assertEqual(self.worker.handle(request())['state'],'ready')
        self.assertEqual(self.worker.handle(request('b'*32))['state'],'pending')
        self.active={W:'c'*32};self.assertEqual(self.worker.handle(request())['state'],'unavailable')
    async def test_cached_read_does_not_wait_for_media_or_sdk(self):
        self.worker.values[W]=value()
        with patch.object(self.worker,'read',side_effect=AssertionError('No synchronous camera I/O')):
            self.assertEqual(self.worker.handle(request())['state'],'ready')
    async def test_standby_never_constructs_sdk(self):
        self.active={};self.worker.start()
        with patch.object(self.worker,'read',side_effect=AssertionError('Standby login')):
            await asyncio.sleep(.02);await self.worker.stop()
        self.assertFalse(self.worker.values)
    async def test_single_lane_continues_without_core_and_discards_revoked_results(self):
        seen=[]
        def read(workload,generation):seen.append(workload);self.active={};return value()
        with patch.object(self.worker,'read',side_effect=read):
            self.worker.start();await asyncio.sleep(.04);await self.worker.stop()
        self.assertEqual(seen,[W]);self.assertFalse(self.worker.values)
    async def test_errors_backoff_cannot_be_bypassed_by_core_polling(self):
        self.worker.errors[W]=3;self.worker.due[W]=time.monotonic()+60;due=self.worker.due[W]
        for _ in range(20):self.worker.handle(request())
        self.assertEqual(self.worker.due[W],due)
if __name__=='__main__':unittest.main()
