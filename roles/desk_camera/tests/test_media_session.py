"""Worker handle authority, including SDK refresh/bypass and latched failures."""
import asyncio,pathlib,sys,tempfile,types,unittest
from unittest.mock import AsyncMock,Mock,patch
FILES=pathlib.Path(__file__).resolve().parents[1]/'files';sys.path.insert(0,str(FILES))
import playback_worker as media

class SDK:
 def __init__(self,allocate=False):
  self.userID=False;self.playerID='sdk-player';self.allocations=0;self.calls=[]
  if allocate:self.getUserID()
 def getUserID(self,forceReload=False,retry=False):
  if not self.userID or forceReload:self.userID=self.executeFunction('getUserID')['user_id']
  return self.userID
 def executeFunction(self,method,*args,**kwargs):
  self.calls.append(method)
  if method=='getUserID':self.allocations+=1;return {'user_id':'new-sdk-handle'}
  return {}
 def getTimeCorrection(self):return 0
 def getRecordings(self,date):
  if getattr(self,'expired',False):self.getUserID(True)
  return [{'clip':{'startTime':1792190000,'endTime':1792190122}}]

class SessionTests(unittest.TestCase):
 def setUp(self):
  sdk=types.ModuleType('pytapo');sdk.Tapo=SDK
  self.patch=patch.dict(sys.modules,{'pytapo':sdk});self.patch.start();self.addCleanup(self.patch.stop)
  self.client=media.create_client();self.handle={'user_id':12,'player_id':'core-player'}
  self.client.bind_media_session(self.handle)
 def test_normal_sdk_reads_use_core_handle_without_allocation(self):
  self.assertEqual(self.client.getUserID(),12);self.client.getRecordings('20261017')
  self.assertEqual((self.client.allocations,self.client.playerID),(0,'core-player'))
 def test_forced_refresh_and_direct_allocation_fail_closed(self):
  for call in (lambda:self.client.getUserID(True),lambda:self.client.getUserID(retry=True),lambda:self.client.executeFunction('getUserID'),lambda:SDK.getUserID(self.client,True)):
   self.client.bind_media_session(self.handle)
   with self.assertRaises(media.MediaSessionExpired):call()
   self.assertEqual(self.client.allocations,0)
   with self.assertRaises(media.MediaSessionExpired):self.client.getUserID()
 def test_guard_covers_construction_without_patching_native_sdk(self):
  with self.assertRaises(media.MediaSessionExpired):media.create_client(allocate=True)
  native=SDK(allocate=True);self.assertEqual(native.allocations,1)
 def test_rebinding_is_required_after_expiry_and_detects_handle_mutation(self):
  for field,value in (('userID',13),('playerID','changed')):
   self.client.bind_media_session(self.handle);setattr(self.client,field,value)
   with self.assertRaises(media.MediaSessionExpired):self.client.check_media_session()
  self.client.bind_media_session({'user_id':14,'player_id':'new-core-player'})
  self.assertEqual(self.client.getUserID(),14);self.assertEqual(self.client.allocations,0)
 def test_binding_validates_copies_and_preserves_zero_id_contract(self):
  self.handle['user_id']=99;self.assertEqual(self.client.getUserID(),12)
  with self.assertRaises(ValueError):self.client.bind_media_session({'user_id':True,'player_id':'bad'})
  self.client.bind_media_session({'user_id':0,'player_id':'zero'});self.assertEqual(self.client.getUserID(),0)
  self.client.userID=False
  with self.assertRaises(media.MediaSessionExpired):self.client.getUserID()
 def test_masked_sdk_error_remains_typed_without_handle_details(self):
  try:self.client.getUserID(True)
  except media.MediaSessionExpired:pass
  failure=media.failure(RuntimeError('private SDK details'),self.client)
  self.assertEqual(failure['reason'],'session_expired');self.assertNotIn('private',str(failure));self.assertNotIn('core-player',str(failure))

class PipelineTests(unittest.IsolatedAsyncioTestCase):
 async def test_expired_index_never_starts_downloader_or_hls_and_healthy_path_still_runs(self):
  sdk=types.ModuleType('pytapo');sdk.Tapo=SDK;downloader=types.ModuleType('pytapo.media_stream.downloader');downloader.Downloader=Mock()
  package=AsyncMock();hls=types.ModuleType('hls_worker');hls.package=package
  with patch.dict(sys.modules,{'pytapo':sdk,'pytapo.media_stream.downloader':downloader,'hls_worker':hls}),tempfile.TemporaryDirectory() as tmp:
   client=media.create_client();client.expired=True
   job={'date':'20261017','start':1792190000,'end':1792190122};handle={'user_id':12,'player_id':'core-player'}
   with self.assertRaises(media.MediaSessionExpired):await media.execute(client,job,handle,pathlib.Path(tmp),{'format':'hls','generation':'a'*32},Mock(),asyncio.to_thread)
   package.assert_not_awaited();downloader.Downloader.assert_not_called();self.assertEqual(client.allocations,0)
   client.expired=False
   await media.execute(client,job,handle,pathlib.Path(tmp),{'format':'hls','generation':'a'*32},Mock(),asyncio.to_thread)
   package.assert_awaited_once();self.assertEqual(client.allocations,0)
if __name__=='__main__':unittest.main()
