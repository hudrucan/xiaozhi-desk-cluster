import base64,hashlib,importlib.util,json,pathlib,sys,types,unittest
from unittest.mock import patch
BASE=pathlib.Path(__file__).resolve().parents[1]/'files';sys.path.insert(0,str(BASE));sys.path.insert(0,str(BASE.parents[1]/'desk_camera/files'))
spec=importlib.util.spec_from_file_location('camera_api_ticket',BASE/'camera_api.py');api=importlib.util.module_from_spec(spec);spec.loader.exec_module(api)
from protocol import unpack
class TicketTests(unittest.TestCase):
 def setUp(self):
  self.scope={'workload':'tapo_c222_160','node':'deskb2x','id':'a'*32,'generation':'b'*32,'source':'ts','digest':hashlib.sha256(b'TS').hexdigest()}
  config={'node':'deskb1x','members':{'deskb1x':'10.10.10.11','deskb2x':'10.10.10.12'},'key':'test-key'}
  self.patch=patch.object(api,'pathlib',types.SimpleNamespace(Path=lambda _:types.SimpleNamespace(read_bytes=lambda:json.dumps(config).encode())));self.patch.start();self.addCleanup(self.patch.stop)
 def test_exact_member_route_and_body_digest_are_bound(self):
  ticket=api.thumbnail_ticket(self.scope);value=unpack(base64.b64decode(ticket['authorization']),'test-key')
  self.assertEqual(value['action'],'thumbnail');self.assertEqual(value['recipient'],'deskb2x');self.assertEqual(value['value'],{k:v for k,v in self.scope.items() if k!='node'});self.assertEqual(ticket['url'],'http://10.10.10.12:20226/thumbnail/tapo_c222_160/'+'a'*32)
 def test_unknown_members_sources_and_bad_cached_digest_are_rejected(self):
  for changed in ({'node':'evil.example'},{'source':'url'},{'digest':'bad'},{'generation':'../private'},{'source':'cache','generation':''}):
   with self.assertRaises(ValueError):api.thumbnail_ticket(dict(self.scope,**changed))
 def test_cached_path_requires_empty_body_and_generation(self):
  ticket=api.thumbnail_ticket(dict(self.scope,source='cache',generation='',digest=hashlib.sha256(b'').hexdigest()));self.assertIn('/thumbnail/',ticket['url'])
 def test_fetch_and_cancel_are_signed_with_owner_generation_and_digest(self):
  ticket=api.thumbnail_ticket(dict(self.scope,source='fetch'));value=unpack(base64.b64decode(ticket['authorization']),'test-key');self.assertEqual(value['value']['source'],'fetch')
  scope=dict(self.scope,source='cancel',digest=hashlib.sha256(b'').hexdigest());self.assertTrue(api.thumbnail_ticket(scope))
  with self.assertRaises(ValueError):api.thumbnail_ticket(dict(scope,digest=hashlib.sha256(b'x').hexdigest()))
if __name__=='__main__':unittest.main()
