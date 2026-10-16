import ast,asyncio,copy,importlib.util,io,json,pathlib,sqlite3,sys,tarfile,tempfile,time,unittest
from unittest.mock import patch,Mock,AsyncMock
from cryptography.fernet import Fernet
FILES=pathlib.Path(__file__).resolve().parents[1]/'files';sys.path.insert(0,str(FILES))
import snapshot,protocol,lease,config_journal,runtime,agent
class Snapshot(unittest.TestCase):
 def setUp(self):
  self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup);self.root=pathlib.Path(self.tmp.name)/'config';self.root.mkdir();(self.root/'.storage').mkdir();(self.root/'.storage/auth').write_text('{"data":{"users":["keep"]}}');(self.root/'configuration.yaml').write_text('frontend:\n');self.key=Fernet.generate_key();self.image='pinned-image'
 def bundle(self):return snapshot.create(self.root,self.key,self.image)[0]
 def test_roundtrip_preserves_auth_custom_code_and_local_sqlite(self):
  (self.root/'custom_components').mkdir();(self.root/'custom_components/test.py').write_text('x=1\n')
  with sqlite3.connect(self.root/'home-assistant_v2.db') as db:db.execute('create table events(v text)');db.execute("insert into events values ('preserve')")
  blob=self.bundle();target=pathlib.Path(self.tmp.name)/'restored';snapshot.unpack(blob,self.key,self.image,target)
  self.assertEqual(json.loads((target/'.storage/auth').read_text())['data']['users'],['keep'])
  self.assertEqual((target/'custom_components/test.py').read_text(),'x=1\n')
  with sqlite3.connect(target/'home-assistant_v2.db') as db:self.assertEqual(db.execute('select v from events').fetchone()[0],'preserve')
 def test_tampering_wrong_key_and_wrong_artifact_fail_closed(self):
  blob=self.bundle()
  for b,k,i in [(blob[:-1]+b'x',self.key,self.image),(blob,Fernet.generate_key(),self.image),(blob,self.key,'other-image')]:
   with self.assertRaises(Exception):snapshot.unpack(b,k,i,pathlib.Path(self.tmp.name)/'bad')
 def test_symlinks_and_missing_auth_cannot_be_certified(self):
  (self.root/'escape').symlink_to('/etc/passwd')
  with self.assertRaises(ValueError):self.bundle()
  (self.root/'escape').unlink();(self.root/'.storage/auth').unlink()
  with self.assertRaises(ValueError):self.bundle()
 def test_archive_rejects_traversal_duplicates_and_bombs_before_writing(self):
  for name in ('../escape','/absolute','a/../../bad'):
   output=io.BytesIO()
   with tarfile.open(fileobj=output,mode='w:gz') as tar:
    e=tarfile.TarInfo(name);e.size=1;tar.addfile(e,io.BytesIO(b'x'))
   with self.assertRaises(ValueError):snapshot.unpack(Fernet(self.key).encrypt(output.getvalue()),self.key,self.image,pathlib.Path(self.tmp.name)/'bad')
 def test_logs_and_live_lock_are_excluded_but_config_digest_detects_edits(self):
  first=snapshot.config_digest(self.root);(self.root/'home-assistant.log').write_text('private log');(self.root/'.ha_run.lock').write_text('stale')
  self.assertEqual(first,snapshot.config_digest(self.root));(self.root/'automations.yaml').write_text('[]');self.assertNotEqual(first,snapshot.config_digest(self.root))
 def test_only_camera_hot_cache_is_excluded(self):
  self.assertFalse(snapshot.included('www/tapo_control/entry/videos/clip.mp4'))
  self.assertTrue(snapshot.included('www/family/photo.jpg'))
  self.assertTrue(snapshot.included('.storage/tapo_control-entry'))
class Framework(unittest.TestCase):
 def test_existing_lease_and_journal_are_reused_without_consensus_changes(self):
  original=FILES.parents[1]/'desk_network/files/ha.py'
  old=next(n for n in ast.parse(original.read_text()).body if isinstance(n,ast.ClassDef) and n.name=='Lease')
  new=next(n for n in ast.parse((FILES/'lease.py').read_text()).body if isinstance(n,ast.ClassDef) and n.name=='Lease')
  self.assertEqual(ast.dump(old,include_attributes=False),ast.dump(new,include_attributes=False))
  self.assertEqual((FILES/'config_journal.py').read_bytes(),(FILES.parents[1]/'desk_monitoring/files/config_journal.py').read_bytes())
 def test_restart_quarantine_and_competing_owner(self):
  with tempfile.TemporaryDirectory() as tmp:
   clock=[100.];p=pathlib.Path(tmp)/'vote';v=lease.Lease(p,lambda:clock[0])
   with self.assertRaises(lease.Unavailable):v.grant([1,'a'],'a')
   clock[0]=118;v.grant([1,'a'],'a')
   with self.assertRaises(lease.Unavailable):v.grant([2,'b'],'b')
   restarted=lease.Lease(p,lambda:clock[0]);clock[0]=134
   with self.assertRaises(lease.Unavailable):restarted.grant([2,'b'],'b')
   clock[0]=136;restarted.grant([2,'b'],'b')
 def test_journal_recovers_highest_accepted_and_rejects_single_vote(self):
  with tempfile.TemporaryDirectory() as tmp:
   key=b'quorum-key';cipher=Fernet(Fernet.generate_key());members=['a','b','c'];journals=[config_journal.Journal(pathlib.Path(tmp)/n,cipher,n,members,key) for n in members]
   for j in journals:j.initialize({'digest':'old'})
   for j in journals[:2]:j.promise(1,[1,'a'])
   receipts=[j.accept(1,[1,'a'],{'digest':'new'}) for j in journals[:2]]
   accepted=[j.promise(1,[2,'b'])['accepted'] for j in journals[1:]]
   self.assertEqual(max((x for x in accepted if x),key=lambda x:x['ballot'])['value'],{'digest':'new'})
   with self.assertRaises(config_journal.Failure):journals[2].commit(1,{'digest':'new'},receipts[:1])
   journals[2].commit(1,{'digest':'new'},receipts);self.assertEqual(journals[2].state['revision'],1)
class Protocol(unittest.TestCase):
 def test_signed_identity_deadline_and_payload_limits(self):
  v={'sender':'deskb1x','recipient':'deskb2x','id':'a'*32,'at':time.time(),'action':'status'}
  self.assertEqual(protocol.unpack(protocol.pack(v,'k'),'k'),v)
  for changed in [dict(v,at=time.time()-11),dict(v,id='bad'),dict(v,sender='outside')]:
   with self.assertRaises(ValueError):protocol.unpack(protocol.pack(changed,'k'),'k')
  with self.assertRaises(ValueError):protocol.unpack(protocol.pack(v,'k'),'wrong')
  with self.assertRaises(ValueError):protocol.pack(dict(v,value='x'*protocol.MAX),'k')
class Control(unittest.IsolatedAsyncioTestCase):
 async def test_checkpoint_requires_ready_owner_and_failed_clean_stop_never_snapshots(self):
  a=object.__new__(agent.Agent);a.owned=lambda:True;a.phase='active';a.ready=True;a.exclusive=__import__('threading').Lock();a.started_at=0;a.permit=Mock()
  with patch.object(agent,'shell',side_effect=RuntimeError('stop failed')),patch.object(agent,'worker') as worker:
   with self.assertRaises(RuntimeError):await a.checkpoint()
   worker.assert_not_called()
  a.ready=False
  with self.assertRaises(ValueError):await a.checkpoint()
 async def test_old_accepted_revision_cannot_rollback_a_newer_certified_head(self):
  a=object.__new__(agent.Agent);a.journal=Mock();a.journal.state={'revision':3};a.propose=AsyncMock()
  heads={'a':{'journal':{'revision':1,'accepted':{'ballot':[100,'a'],'value':{'digest':'old'}}}},'b':{'journal':{'revision':3,'accepted':None}}}
  await a.recover_accepted(heads);a.propose.assert_not_called()
  heads['b']['journal']['accepted']={'ballot':[101,'b'],'value':{'digest':'next'}}
  await a.recover_accepted(heads);a.propose.assert_awaited_once_with({'digest':'next'})
 async def test_native_restart_and_transient_http_loss_keep_local_configuration(self):
  a=object.__new__(agent.Agent);a.phase='active';a.started_at=1;a.unhealthy_since=None;a.generation='new'
  with tempfile.TemporaryDirectory() as tmp,patch.object(agent,'RUN',pathlib.Path(tmp)),patch.object(agent,'clock',return_value=1000):
   self.assertFalse(a.health_expired(False));self.assertEqual(a.unhealthy_since,1000)
   with patch.object(agent,'clock',return_value=1080):self.assertFalse(a.health_expired(False))
   self.assertFalse(a.health_expired(True));self.assertIsNone(a.unhealthy_since)
   (pathlib.Path(tmp)/'exited.json').write_text(json.dumps({'generation':'new'}));self.assertTrue(a.health_expired(False))
 async def test_telemetry_restart_never_requests_owner_handoff(self):
  a=object.__new__(agent.Agent);a.local_at=agent.clock()-20;a.local={'admission':{'core':False}}
  self.assertFalse(a.draining());a.local_at=agent.clock();self.assertFalse(a.draining());a.local['admission']['core']=True;self.assertTrue(a.draining())
 async def test_stale_admission_blocks_candidate(self):
  a=object.__new__(agent.Agent);a.cooldown=0;a.local_at=agent.clock()-20;a.local={'online':True,'memory_available_mib':2500,'admission':{}};a.owned=lambda:False
  self.assertFalse(a.eligible());a.local_at=agent.clock();self.assertTrue(a.eligible());a.local['admission']={'worker':True};self.assertFalse(a.eligible())
class DNSAccess(unittest.TestCase):
 def test_acl_only_adds_private_bridge_and_is_idempotent(self):
  import dns_access
  old='setLocal("192.168.1.250:53")\nsetACL({"192.168.1.0/24", "fe80::/10"})\nnewServer({address="1.2.3.4"})\n'
  new=dns_access.candidate(old);self.assertEqual(new.replace(', "10.88.0.0/16"',''),old);self.assertEqual(dns_access.candidate(new),new)
  with self.assertRaises(ValueError):dns_access.candidate('unexpected')
class Ingress(unittest.TestCase):
 def test_fragment_is_additive_idempotent_and_preserves_existing_bytes(self):
  import install_proxy
  old='global\n  daemon\n\nfrontend existing\n  bind :80\n\n'
  new=install_proxy.candidate(old,'frontend native\n  bind :8123\n')
  self.assertTrue(new.startswith(old));self.assertEqual(new,install_proxy.candidate(new,'frontend native\n  bind :8123\n'))
  self.assertEqual(new.replace(install_proxy.BEGIN+'frontend native\n  bind :8123\n'+install_proxy.END,''),old)
class HTTPMigration(unittest.TestCase):
 def test_pinned_native_storage_and_secret_tags_preserved(self):
  from http_config import configure
  with tempfile.TemporaryDirectory() as tmp:
   root=pathlib.Path(tmp);(root/'.storage').mkdir();p=root/'.storage/http'
   p.write_text(json.dumps({'key':'http','version':2,'minor_version':2,'data':{'stable':{'ip_ban_enabled':True},'pending':{'server_host':['old']}}}))
   (root/'configuration.yaml').write_text('http:\n  server_port: 8123\nother: !secret token\nautomation: !include automations.yaml\nenv: !env_var TOKEN\n')
   configure(root);first=p.read_bytes();configure(root);self.assertEqual(first,p.read_bytes());v=json.loads(p.read_text())['data'];self.assertIsNone(v['pending']);self.assertTrue(v['stable']['use_x_forwarded_for']);self.assertTrue(v['stable']['ip_ban_enabled']);self.assertIn('10.10.10.12/32',v['stable']['trusted_proxies']);self.assertIn('!secret', (root/'configuration.yaml').read_text());self.assertNotIn('http:',(root/'configuration.yaml').read_text());self.assertIn('automation: !include automations.yaml', (root/'configuration.yaml').read_text());self.assertIn('env: !env_var TOKEN', (root/'configuration.yaml').read_text())
class Retention(unittest.TestCase):
 def test_retains_certified_accepted_seed_current_and_inflight_artifacts(self):
  with tempfile.TemporaryDirectory() as tmp,patch.object(agent,'ROOT',pathlib.Path(tmp)):
   root=pathlib.Path(tmp);(root/'blobs').mkdir();(root/'generations').mkdir()
   names=[format(i,'064x') for i in range(10)]
   for i,n in enumerate(names):
    p=root/'blobs'/n;p.write_bytes(b'blob');__import__('os').utime(p,(time.time()-10000+i,time.time()-10000+i))
   recent=root/'blobs'/('f'*64);recent.write_bytes(b'inflight')
   g=root/'generations'/('a'*32);g.mkdir();(root/'current').symlink_to(g)
   a=object.__new__(agent.Agent);a.c={'seed':{'digest':names[0]}};a.journal=Mock();a.journal.status.return_value={'value':{'digest':names[1]},'accepted':{'value':{'digest':names[2]}}}
   a.collect_old_artifacts()
   for n in names[:3]:self.assertTrue((root/'blobs'/n).exists())
   self.assertFalse((root/'blobs'/names[3]).exists());self.assertTrue(recent.exists());self.assertTrue(g.exists())
class Runtime(unittest.TestCase):
 def test_final_fence_checks_pid_start_identity_before_killing(self):
  with tempfile.TemporaryDirectory() as tmp:
   root=pathlib.Path(tmp);(root/'runtime.json').write_text(json.dumps({'pid':123,'start':'99'}))
   with patch.object(runtime,'RUN',root),patch.object(runtime.pathlib,'Path') as path,patch.object(runtime.os,'kill') as kill,patch.object(runtime,'command') as command:
    path.return_value.read_text.return_value='123 (init) '+' '.join(['0']*19+['100'])
    runtime.fence();kill.assert_not_called();command.assert_called_with('podman','kill',runtime.NAME,check=False)
    path.return_value.read_text.return_value='123 (init) '+' '.join(['0']*19+['99'])
    runtime.fence();kill.assert_called_once_with(123,runtime.signal.SIGKILL)
 def test_permit_expires_and_generation_must_match(self):
  with tempfile.TemporaryDirectory() as tmp,patch.object(runtime,'RUN',pathlib.Path(tmp)),patch.object(runtime,'clock',return_value=100):
   p=pathlib.Path(tmp)/'permit.json'
   for until,expected in [(109,True),(99,False),(112,False),(float('nan'),False)]:
    p.write_text(json.dumps({'until':until,'generation':'a'*32}));self.assertEqual(runtime.authorized('a'*32),expected)
   p.write_text(json.dumps({'until':109,'generation':'b'*32}));self.assertFalse(runtime.authorized('a'*32))
if __name__=='__main__':unittest.main()
