import asyncio,importlib.util,json,pathlib,sys,tempfile,types,unittest
from unittest.mock import patch,Mock
FILES=pathlib.Path(__file__).resolve().parents[1]/'files';sys.path.insert(0,str(FILES))
import health,playback
class HealthTests(unittest.TestCase):
 def test_reachability_is_cached_and_two_failures_are_required_after_success(self):
  h=health.CameraHealth();c={'streams':{'x':'rtsp://u:p@192.0.2.1/stream1'}}
  with patch.object(health.socket,'create_connection') as connect:
   h.refresh(c,{'cpu':10},100);h.refresh(c,{'cpu':20},101);self.assertEqual(connect.call_count,1);self.assertTrue(h.reachable)
  with patch.object(health.socket,'create_connection',side_effect=OSError):
   h.refresh(c,{'cpu':40},116);self.assertTrue(h.reachable);h.refresh(c,{'cpu':40},132);self.assertFalse(h.reachable)
 def test_memory_pressure_and_smoothed_cpu_influence_placement(self):
  h=health.CameraHealth();h.cpu=20;self.assertEqual(h.penalty({'memory_available_mib':2048}),20);self.assertGreater(h.penalty({'memory_available_mib':256}),20)
class PlaybackTests(unittest.TestCase):
 def test_typed_clip_validation_and_stable_identity(self):
  v={'date':'20261016','start':1792112400,'end':1792112460};job=playback.validate('tapo_c222_160',v)
  self.assertEqual(playback.identity(job),playback.identity(dict(reversed(list(job.items())))))
  for change in ({'start':True},{'end':1792114401},{'date':'20260231'},{'date':'../../../etc'},{'path':'/etc/passwd'}):
   with self.assertRaises((ValueError,TypeError)):playback.validate('tapo_c222_160',dict(v,**change))
  self.assertEqual(playback.validate('tapo_c222_161',v)['workload'],'tapo_c222_161')
  with self.assertRaises(ValueError):playback.validate('unknown_camera',v)
 def test_artifact_paths_reject_traversal_unknown_camera_and_invalid_job(self):
  for workload,ident in [('unknown_camera','a'*32),('tapo_c222_160','../secret'),('../other','a'*32)]:
   with self.assertRaises(ValueError):playback.clip_path(workload,ident)
 def test_only_complete_bounded_files_can_be_served(self):
  with tempfile.TemporaryDirectory() as tmp,patch.object(playback,'ROOT',pathlib.Path(tmp)):
   a=types.SimpleNamespace(c={'workload':'tapo_c222_160'},node='deskb1x');p=playback.Playback(a);ident='a'*32;path=playback.clip_path(a.c['workload'],ident);path.parent.mkdir(parents=True)
   self.assertEqual(p.status(ident)['state'],'missing');path.write_bytes(b'x'*2048);self.assertEqual(p.status(ident)['state'],'ready')
   path.unlink();path.symlink_to('/etc/passwd');self.assertEqual(p.status(ident)['state'],'missing')
 def test_accepted_job_is_preparing_before_first_progress_file_exists(self):
  a=types.SimpleNamespace(c={'workload':'tapo_c222_160'},node='deskb1x');p=playback.Playback(a);p.current='a'*32;p.task=Mock();p.task.done.return_value=False
  with tempfile.TemporaryDirectory() as tmp,patch.object(playback,'ROOT',pathlib.Path(tmp)):
   self.assertEqual(p.status(p.current)['state'],'preparing')
 def test_expired_session_reason_is_visible_only_after_attempt_cleanup(self):
  a=types.SimpleNamespace(c={'workload':'tapo_c222_160'},node='deskb1x');p=playback.Playback(a);p.current='a'*32;p.task=Mock();p.task.done.return_value=False
  with tempfile.TemporaryDirectory() as tmp,patch.object(playback,'ROOT',pathlib.Path(tmp)):
   path=playback.clip_path(a.c['workload'],p.current).parent;path.mkdir(parents=True)
   (path/'progress.json').write_text(json.dumps({'state':'failed','reason':'session_expired','error':'safe message'}))
   state=p.status(p.current);self.assertEqual(state['state'],'preparing');self.assertNotIn('reason',state)
   p.task.done.return_value=True;state=p.status(p.current);self.assertEqual((state['state'],state['reason']),('failed','session_expired'))
 def test_inventory_requires_complete_artifact_and_never_returns_session_handle(self):
  with tempfile.TemporaryDirectory() as tmp,patch.object(playback,'ROOT',pathlib.Path(tmp)):
   a=types.SimpleNamespace(c={'workload':'tapo_c222_161'},node='deskb2x');p=playback.Playback(a);job=playback.validate(a.c['workload'],{'date':'20261016','start':1792112400,'end':1792112460});ident=playback.identity(job);path=playback.clip_path(a.c['workload'],ident);path.parent.mkdir(parents=True);(path.parent/'request.json').write_text(json.dumps(job))
   self.assertEqual(p.inventory()['items'],[]);path.write_bytes(b'x'*2048);items=p.inventory()['items'];self.assertEqual(items[0]['state'],'ready');self.assertEqual(items[0]['id'],ident);self.assertNotIn('session',items[0])
   path.unlink();self.assertEqual(p.inventory()['items'],[])
 def test_recording_handle_is_bounded_and_rejects_arbitrary_nested_payloads(self):
  self.assertEqual(playback.session_handle({'user_id':123,'player_id':'a-b'})['user_id'],123)
  for v in (None,{'user_id':{},'player_id':'x'},{'user_id':True,'player_id':'x'},{'user_id':123,'player_id':'../secret'},{'user_id':123,'player_id':'x','password':'not accepted'}):
   with self.assertRaises(playback.PlaybackError):playback.session_handle(v)
 def test_prepare_is_fenced_and_admission_is_required(self):
  with tempfile.TemporaryDirectory() as tmp,patch.object(playback,'ROOT',pathlib.Path(tmp)):
   a=types.SimpleNamespace(c={'workload':'tapo_c222_160'},node='deskb1x',owned=lambda:False,ready=lambda:True,eligible=lambda:True)
   with self.assertRaises(ValueError):playback.Playback(a).prepare({'date':'20261016','start':1792112400,'end':1792112460})
if __name__=='__main__':unittest.main()
