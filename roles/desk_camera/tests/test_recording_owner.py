"""Owner allocation, per-HTTP fencing and immutable metadata pagination."""
import asyncio,copy,pathlib,sys,types,unittest
from unittest.mock import patch
sys.path.insert(0,str(pathlib.Path(__file__).resolve().parents[1]/'files'))
import recording_owner as owner
from recording_contract import ContractError,page
from playback_worker import MediaSessionExpired
from test_recording_contract import NOW,query

class Transport:
 def __init__(self):self.method='pytapo';self.calls=[];self.before=lambda:None;self.retry=False
 def _request(self,*args,**kwargs):
  self.calls.append(kwargs);self.before()
  if self.retry:self.retry=False;return self._request(*args,**kwargs)
  return {}
class SDK:
 def __init__(self):
  self.transport=Transport();self.userID=False;self.playerID='owner-player';self.allocations=0
  self.performRequest({'method':'getDeviceInfo'})
 def performRequest(self,*args,**kwargs):return self.transport._request('POST','camera',timeout=10)
 def executeFunction(self,method,*args,**kwargs):
  self.performRequest({'method':method})
  if method=='getUserID':self.allocations+=1;return {'user_id':self.allocations}
  return {}
 def getUserID(self,forceReload=False,retry=False):
  if forceReload or not self.userID:self.userID=self.executeFunction('getUserID')['user_id']
  return self.userID

class OwnerTests(unittest.TestCase):
 def setUp(self):
  self.remaining=10
  with patch.dict(sys.modules,{'pytapo':types.SimpleNamespace(Tapo=SDK)}):self.client=owner.create_owner_client(lambda:self.remaining)
 def test_constructor_does_not_allocate_and_only_explicit_begin_can_refresh(self):
  c=self.client;self.assertEqual(c.allocations,0)
  with self.assertRaises(MediaSessionExpired):c.getUserID()
  c.begin_media();self.assertEqual(c.getUserID(),1)
  with self.assertRaises(MediaSessionExpired):c.getUserID(True)
  self.assertEqual(c.allocations,1);c.begin_media();self.assertEqual(c.getUserID(),2)
 def test_every_http_request_is_bounded_including_sdk_recursive_retry(self):
  c=self.client;c.transport.retry=True;c.begin_media()
  self.assertTrue(all(v['timeout']==(1,1) for v in c.transport.calls))
  before=len(c.transport.calls);c.transport.retry=True;c.transport.before=lambda:setattr(self,'remaining',0)
  with self.assertRaises(ContractError):c.performRequest({})
  self.assertEqual(len(c.transport.calls),before+1)
 def test_expired_permit_cannot_start_or_return_success(self):
  c=self.client;self.remaining=2.9
  with self.assertRaises(ContractError):c.begin_media()
  self.assertEqual(c.allocations,0)
  self.remaining=10;c.transport.before=lambda:setattr(self,'remaining',0)
  with self.assertRaises(ContractError):c.performRequest({})
 def test_unaudited_transport_fails_closed(self):
  c=self.client;c._bounded_transport=False;c.transport.method='tpap'
  with self.assertRaises(ContractError) as err:c.performRequest({})
  self.assertEqual(err.exception.code,'worker_unavailable')

class Camera:
 def __init__(self):self.calls=[];self.rows=[{'video':{'startTime':int(NOW)+i*60,'endTime':int(NOW)+i*60+30}} for i in range(130)]
 def getSDCard(self):return [{'hd_info':{'status':'normal'}}]
 def getTime(self):return {'system':{'clock_status':{'seconds_from_1970':NOW+25200}}}
 def begin_media(self):self.calls.append('begin')
 def check_media_session(self):self.calls.append('check')
 def getRecordings(self,*args):self.calls.append(args);return copy.deepcopy(self.rows)
 def getRecordingsList(self):return [{'date':{'date':'20261017'}},{'date':{'date':'20261016'}}]
class ReaderTests(unittest.IsolatedAsyncioTestCase):
 async def asyncSetUp(self):
  self.camera=Camera()
  async def client(v):return self.camera
  async def control(fn,*args):return fn(*args)
  self.reader=owner.Reader(types.SimpleNamespace(client=client,control=control))
  self.clock=patch.object(owner.time,'time',return_value=NOW);self.clock.start();self.addCleanup(self.clock.stop)
 async def test_pages_are_one_pinned_snapshot_without_repeat_sdk_or_core_handles(self):
  request=query('index');first=await self.reader.execute(request);page(request,first,NOW)
  self.assertEqual(len(first['items']),128);self.assertFalse(first['complete']);self.assertEqual(first['camera_offset_seconds'],25200)
  request['query']['cursor']=first['next_cursor'];second=await self.reader.execute(request);page(request,second,NOW)
  self.assertEqual(len(second['items']),2);self.assertTrue(second['complete']);self.assertEqual(first['snapshot'],second['snapshot'])
  self.assertEqual(self.camera.calls,['begin',('20261017',0,2048),'check'])
  self.assertNotIn('session',first)
 async def test_cursor_is_bound_to_owner_generation_and_date(self):
  request=query('index');first=await self.reader.execute(request)
  for other in (query('index',generation='c'),query()):
   other['query']['cursor']=first['next_cursor']
   with self.assertRaises(ContractError):await self.reader.execute(other)
 async def test_no_sd_and_faulty_sd_are_explicit_and_do_not_allocate(self):
  for status,expected in [('offline','absent'),('unformatted','needs_attention')]:
   self.camera.getSDCard=lambda:[{'hd_info':{'status':status}}];self.reader.snapshots.clear()
   value=await self.reader.execute(query('index'));self.assertEqual(value['storage'],expected);self.assertEqual(value['items'],[])
  self.assertFalse(self.camera.calls)
 async def test_unknown_storage_and_full_sdk_limit_are_not_reported_as_empty(self):
  self.camera.getSDCard=lambda:{}
  with self.assertRaises(ContractError):await self.reader.execute(query())
  self.camera.getSDCard=lambda:[{'hd_info':{'status':'normal'}}];self.camera.rows*=16
  with self.assertRaises(ContractError):await self.reader.execute(query('index'))
 async def test_dates_are_sorted_and_do_not_allocate_session(self):
  value=await self.reader.execute(query());self.assertEqual(value['items'],['20261016','20261017']);self.assertFalse(self.camera.calls)

if __name__=='__main__':unittest.main()
