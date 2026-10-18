"""Guard precedes native imports and refuses uncovered worker authority."""
import importlib.util,json,pathlib,runpy,sys,tempfile,types,unittest
from unittest.mock import Mock,patch
FILES=pathlib.Path(__file__).resolve().parents[1]/'files'
spec=importlib.util.spec_from_file_location('camera_bootstrap',FILES/'recording_bootstrap.py');m=importlib.util.module_from_spec(spec);spec.loader.exec_module(m)
class SDK:
 def __init__(self):raise AssertionError('No SDK client may be constructed')
 def getUserID(self,*a,**kw):raise AssertionError('No camera request may run')
 getMost=getRecordings=getRecordingsUTC=getRecordingsList=getMediaSession=executeFunction=performRequest=getUserID
 moveMotor=setPreset=setPrivacyMode=getUserID
class BootstrapTests(unittest.TestCase):
 def setUp(self):
  self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup);self.root=pathlib.Path(self.tmp.name);(self.root/'.storage').mkdir();folder=self.root/'custom_components/desk_cluster';folder.mkdir(parents=True)
  (folder/'recording_remote.py').write_bytes((FILES/'desk_cluster/recording_remote.py').read_bytes())
  self.policy=self.root/'.storage/desk_camera_recording_authority';self.policy.write_text(json.dumps({'version':1,'workloads':['tapo_c222_163']}));self.active=self.root/'owner.json';self.active.write_text(json.dumps({'version':1,'workloads':['tapo_c222_163']}))
 def test_sdk_is_guarded_without_importing_or_initializing_native_integration(self):
  class Tapo(SDK):pass
  with patch.dict(sys.modules,{'pytapo':types.SimpleNamespace(Tapo=Tapo),'custom_components.tapo_control':None}):
   m.arm(self.root,self.active);self.assertTrue(Tapo._desk_recording_guard['before_native_setup'])
   client=object.__new__(Tapo);client.host='192.168.1.163'
   with self.assertRaisesRegex(ValueError,'managed by its camera owner'):client.getUserID()
 def test_observation_guard_is_armed_before_native_setup_without_sdk_io(self):
  folder=self.root/'custom_components/desk_cluster'
  for src in (FILES/'desk_cluster/observation_remote.py',FILES.parents[1]/'desk_camera/files/observation_contract.py'):
   (folder/src.name).write_bytes(src.read_bytes())
  (self.root/'.storage/desk_camera_observation_authority').write_text(json.dumps({'version':1,'workloads':['tapo_c222_163']}))
  self.active.with_name('observations.json').write_text(json.dumps({'version':1,'workloads':['tapo_c222_163']}))
  class Tapo(SDK):pass
  with patch.dict(sys.modules,{'pytapo':types.SimpleNamespace(Tapo=Tapo)}):
   m.arm(self.root,self.active)
   self.assertTrue(Tapo._desk_observation_guard['before_native_setup'])
   self.assertEqual(Tapo._desk_observation_guard['hosts'],{'192.168.1.163':'tapo_c222_163'})
 def test_active_worker_must_be_covered_even_when_core_policy_is_missing(self):
  self.policy.unlink()
  with self.assertRaisesRegex(ValueError,'does not cover'):m.arm(self.root,self.active)
 def command_setup(self, core=True, active=True):
  folder=self.root/'custom_components/desk_cluster';cam=FILES.parents[1]/'desk_camera/files'
  for src in (FILES/'desk_cluster/command_remote.py',cam/'command_contract.py',FILES/'desk_cluster/observation_remote.py',cam/'observation_contract.py'):
   (folder/src.name).write_bytes(src.read_bytes())
  value=json.dumps({'version':1,'workloads':['tapo_c222_163']})
  (self.root/'.storage/desk_camera_observation_authority').write_text(value);self.active.with_name('observations.json').write_text(value)
  if core:(self.root/'.storage/desk_camera_command_authority').write_text(value)
  if active:self.active.with_name('commands.json').write_text(value)
 def test_commands_guarded_before_native_setup_without_any_sdk_client(self):
  self.command_setup(active=False)
  class Tapo(SDK):pass
  with patch.dict(sys.modules,{'pytapo':types.SimpleNamespace(Tapo=Tapo)}):
   m.arm(self.root,self.active);self.assertTrue(Tapo._desk_command_guard['before_native_setup'])
   self.assertEqual(Tapo._desk_command_guard['hosts'],{'192.168.1.163':'tapo_c222_163'})
   client=object.__new__(Tapo);client.host='192.168.1.163'
   with self.assertRaisesRegex(ValueError,'Recording session'):client.getUserID()
   with self.assertRaisesRegex(ValueError,'Command is managed'):client.executeFunction('motorMove',{})
   remote=Tapo.moveMotor.__globals__
   with patch.dict(remote,{'execute':Mock(return_value=dict(state='acknowledged'))}):
    self.assertEqual(client.moveMotor(15,0),dict(error_code=0))
 def test_active_commands_refuse_missing_core_selection_or_adapter(self):
  self.command_setup(core=False)
  class Tapo(SDK):pass
  with patch.dict(sys.modules,{'pytapo':types.SimpleNamespace(Tapo=Tapo)}):
   with self.assertRaisesRegex(ValueError,'does not cover'):m.arm(self.root,self.active)
 def test_commands_require_owner_observations(self):
  self.command_setup(active=False);(self.root/'.storage/desk_camera_observation_authority').unlink()
  class Tapo(SDK):pass
  with patch.dict(sys.modules,{'pytapo':types.SimpleNamespace(Tapo=Tapo)}):
   with self.assertRaisesRegex(ValueError,'require owner-side'):m.arm(self.root,self.active)
 def test_invalid_policy_fails_before_sdk_import_or_camera_construction(self):
  self.policy.write_text('{"version":2,"workloads":[]}')
  with self.assertRaises(ValueError):m.arm(self.root,self.active)
 def test_sitecustomize_failure_terminates_instead_of_python_ignoring_import_error(self):
  with patch.dict(m.os.environ,{'DESK_CAMERA_RECORDING_GUARD':'1'}),patch('importlib.util.spec_from_file_location',side_effect=RuntimeError('test')),patch('os._exit',side_effect=SystemExit(78)) as stop:
   with self.assertRaises(SystemExit) as error:runpy.run_path(str(FILES/'recording_bootstrap.py'))
   self.assertEqual(error.exception.code,78);stop.assert_called_once_with(78)
if __name__=='__main__':unittest.main()
