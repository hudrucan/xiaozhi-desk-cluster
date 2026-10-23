"""Exercise the real readiness handler and policy readers without starting HA."""
import ast
import asyncio
import copy
import json
import pathlib
import sys
import tempfile
import threading
import types
import unittest
from unittest.mock import patch

FILES=pathlib.Path(__file__).resolve().parents[1]/'files/desk_cluster'
CAM=FILES.parents[2]/'desk_camera/files'
SOURCE=FILES/'__init__.py'
W='tapo_c222_163';HOST='192.168.1.163'


class ReadinessTests(unittest.IsolatedAsyncioTestCase):
 def setUp(self):
  self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
  self.root=pathlib.Path(self.tmp.name);component=self.root/'component';component.mkdir()
  for name in ['recording_remote.py','observation_remote.py','command_remote.py']:
   (component/name).write_bytes((FILES/name).read_bytes())
  for name in ['observation_contract.py','command_contract.py']:
   (component/name).write_bytes((CAM/name).read_bytes())
  package=types.ModuleType('readiness_test');package.__path__=[str(component)]
  self.sdk=type('Tapo',(),{})
  self.sdk._desk_recording_guard=dict(hosts={HOST},before_native_setup=True,blocked=3)
  self.sdk._desk_observation_guard=dict(hosts={HOST:W},before_native_setup=True,calls=7,failed=1)
  self.sdk._desk_command_guard=dict(hosts={HOST:W},before_native_setup=True)
  modules=patch.dict(sys.modules,{'readiness_test':package,'pytapo':types.SimpleNamespace(Tapo=self.sdk)})
  modules.start();self.addCleanup(modules.stop)
  self.storage=self.root/'.storage';self.storage.mkdir()
  self.policies={name:self.storage/('desk_camera_'+name+'_authority') for name in ['recording','observation','command']}
  for p in self.policies.values():p.write_text(json.dumps(dict(version=1,workloads=[W])))
  async def executor(fn,*args):return await asyncio.to_thread(fn,*args)
  self.hass=types.SimpleNamespace(config=types.SimpleNamespace(path=lambda p:str(self.root/p)),is_running=True,async_add_executor_job=executor)
  view=next(n for n in ast.parse(SOURCE.read_text()).body if isinstance(n,ast.ClassDef) and n.name=='Readiness')
  web=types.SimpleNamespace(json_response=lambda value,status:types.SimpleNamespace(data=value,status=status))
  scope={'__package__':'readiness_test','HomeAssistantView':object,'pathlib':pathlib,'web':web}
  exec(compile(ast.Module(body=[view],type_ignores=[]),str(SOURCE),'exec'),scope)
  self.view=scope['Readiness'](self.hass)

 async def test_all_policy_reads_and_optional_file_probes_run_off_loop(self):
  loop_thread=threading.get_ident();calls=[]
  read=pathlib.Path.read_bytes;is_file=pathlib.Path.is_file
  def off_loop(fn):
   def checked(path):
    self.assertNotEqual(threading.get_ident(),loop_thread,'Readiness performed blocking filesystem I/O on HA loop')
    calls.append(path.name);return fn(path)
   return checked
  with patch.object(pathlib.Path,'read_bytes',off_loop(read)),patch.object(pathlib.Path,'is_file',off_loop(is_file)):
   result=await self.view.get(None)
  self.assertEqual(result.status,200);self.assertTrue(result.data['ready'])
  self.assertTrue({p.name for p in self.policies.values()}<=set(calls))
  self.assertEqual(result.data['recording_authority']['blocked_core_calls'],3)
  self.assertEqual(result.data['observation_authority']['owner_reads'],7)

 async def test_each_guard_and_before_setup_failure_still_returns_503(self):
  for attr in ['_desk_recording_guard','_desk_observation_guard','_desk_command_guard']:
   original=copy.deepcopy(getattr(self.sdk,attr))
   for change in [dict(hosts=set()),dict(before_native_setup=False)]:
    with self.subTest(guard=attr,change=change):
     setattr(self.sdk,attr,{**original,**change})
     result=await self.view.get(None)
     self.assertEqual(result.status,503);self.assertFalse(result.data['ready'])
   setattr(self.sdk,attr,original)

 async def test_running_state_is_checked_after_executor_finishes(self):
  async def executor(fn,*args):
   value=await asyncio.to_thread(fn,*args);self.hass.is_running=False;return value
  self.hass.async_add_executor_job=executor
  result=await self.view.get(None)
  self.assertEqual(result.status,503);self.assertFalse(result.data['ready'])

 async def test_missing_optional_policies_keep_existing_empty_defaults(self):
  for name in ['observation','command']:self.policies[name].unlink()
  result=await self.view.get(None)
  self.assertEqual(result.status,200)
  for name in ['observation_authority','command_authority']:
   self.assertEqual(result.data[name]['workloads'],[])
   self.assertTrue(result.data[name]['guarded'])

 async def test_changed_policy_is_read_again_without_stale_ready_cache(self):
  self.assertEqual((await self.view.get(None)).status,200)
  self.policies['observation'].write_text(json.dumps(dict(version=1,workloads=['tapo_c222_160'])))
  self.assertEqual((await self.view.get(None)).status,503)

 async def test_malformed_or_unreadable_policy_cannot_return_ready(self):
  for p in self.policies.values():
   with self.subTest(policy=p.name):
    original=p.read_bytes();p.write_text('{broken')
    try:
     with self.assertRaises(ValueError):await self.view.get(None)
    finally:p.write_bytes(original)
  with patch.object(pathlib.Path,'read_bytes',side_effect=OSError('unavailable')):
   with self.assertRaises(OSError):await self.view.get(None)


if __name__=='__main__':unittest.main()
