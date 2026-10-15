import unittest,sys,pathlib,tempfile
from unittest.mock import patch
sys.path.insert(0,str(pathlib.Path(__file__).resolve().parents[1]/'files'))
import bootstrap
class Budget(unittest.TestCase):
 def setup_budget(self,root):
  proc=root/'proc';group='/machine.slice/libpod-'+('a'*64)+'.scope';p=proc/'123';p.mkdir(parents=True);(p/'stat').write_text('123 (init) '+' '.join(['S']+['0']*18+['999']));(p/'cgroup').write_text('0::'+group+'/container\n')
  cgroup=root/'cgroup';scope=cgroup/group.lstrip('/');scope.mkdir(parents=True);(scope/'cpu.max').write_text('50000 100000\n');(scope/'container').mkdir();(scope/'container/cpu.max').write_text('max 100000\n');now=[10.];budget=bootstrap.Budget(lambda:now[0],cgroup,proc)
  return budget,now,p
 def test_boost_pins_scope_and_watchdog_restores_at_deadline(self):
  with tempfile.TemporaryDirectory() as td:
   b,now,p=self.setup_budget(pathlib.Path(td));calls=[]
   with patch('bootstrap.os.write',side_effect=lambda fd,data:calls.append(data) or len(data)):
    self.assertTrue(b.start((123,'999'),{'cpu':15,'activity':{'asr':0,'llm':0,'tts':0}},True));b.settle();self.assertEqual(len(calls),1);now[0]=71;b.settle();self.assertEqual(calls,[b'100000 100000',b'50000 100000']);self.assertIsNone(b.fd)
 def test_readiness_forces_early_reversion(self):
  with tempfile.TemporaryDirectory() as td:
   b,now,p=self.setup_budget(pathlib.Path(td));calls=[]
   with patch('bootstrap.os.write',side_effect=lambda fd,data:calls.append(data) or len(data)):
    self.assertTrue(b.start((123,'999'),{'cpu':15,'activity':dict.fromkeys(('asr','llm','tts'),0)},True));b.settle(True);self.assertEqual(len(calls),2);self.assertIsNone(b.fd)
 def test_busy_stale_or_reused_pid_never_boosts(self):
  with tempfile.TemporaryDirectory() as td:
   b,now,p=self.setup_budget(pathlib.Path(td));idle={'cpu':15,'activity':dict.fromkeys(('asr','llm','tts'),0)}
   for local,fresh,start in ((dict(idle,cpu=90),True,'999'),(dict(idle,activity={'asr':1}),True,'999'),(idle,False,'999'),(idle,True,'998'),(dict(idle,activity=None),True,'999')):
    self.assertFalse(b.start((123,start),local,fresh));self.assertIsNone(b.fd)
 def test_unrelated_cgroup_or_existing_different_quota_is_untouched(self):
  with tempfile.TemporaryDirectory() as td:
   root=pathlib.Path(td);b,now,p=self.setup_budget(root);idle={'cpu':15,'activity':dict.fromkeys(('asr','llm','tts'),0)}
   (p/'cgroup').write_text('0::/system.slice/other.service');self.assertFalse(b.start((123,'999'),idle,True))

 def test_own_startup_cpu_does_not_cancel_budget_but_voice_does(self):
  self.assertFalse(bootstrap.idle({'cpu':90,'activity':dict.fromkeys(('asr','llm','tts'),0)}))
  self.assertTrue(bootstrap.quiet({'cpu':90,'activity':dict.fromkeys(('asr','llm','tts'),0)}))
  self.assertFalse(bootstrap.quiet({'cpu':15,'activity':{'asr':1,'llm':0,'tts':0}}))
 def test_real_platform_child_quota_is_pinned_and_restored(self):
  with tempfile.TemporaryDirectory() as td:
   root=pathlib.Path(td);b,now,p=self.setup_budget(root);scope=next((root/'cgroup/machine.slice').iterdir());(scope/'cpu.max').write_text('max 100000\n');(scope/'container/cpu.max').write_text('50000 100000\n');calls=[]
   with patch('bootstrap.os.write',side_effect=lambda fd,data:calls.append(data) or len(data)):
    self.assertTrue(b.start((123,'999'),{'cpu':15,'activity':dict.fromkeys(('asr','llm','tts'),0)},True));b.settle(True);self.assertEqual(calls,[b'100000 100000',b'50000 100000']);self.assertIsNone(b.fd)
