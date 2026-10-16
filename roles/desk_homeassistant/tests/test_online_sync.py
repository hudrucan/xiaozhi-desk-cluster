from contextlib import closing
import asyncio,io,json,pathlib,sqlite3,sys,tarfile,tempfile,time,unittest
from unittest.mock import AsyncMock,Mock,patch
from cryptography.fernet import Fernet
FILES=pathlib.Path(__file__).resolve().parents[1]/'files';sys.path.insert(0,str(FILES))
import snapshot,agent,ui_bridge

class NativeImport(unittest.TestCase):
 def setUp(self):
  self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup);self.root=pathlib.Path(self.tmp.name);self.key=Fernet.generate_key();self.ticket='a'*32;self.backup_id='b'*8
 def archive(self,*,extra=None,protected=False,member=None,contents=None):
  db=self.root/'sample.db'
  with closing(sqlite3.connect(db)) as con:con.execute('create table if not exists evidence(v)');con.execute('delete from evidence');con.execute("insert into evidence values ('live-backup')");con.commit()
  files={'data/configuration.yaml':b'default_config:\n','data/.storage/auth':b'{"data":{"users":["original"]}}','data/home-assistant_v2.db':db.read_bytes(),'data/custom_components/pilot/__init__.py':b'# retained\n'}
  if contents:files.update(contents)
  inner=io.BytesIO()
  with tarfile.open(fileobj=inner,mode='w:gz') as tar:
   for name,raw in files.items():
    e=tarfile.TarInfo(name);e.size=len(raw);tar.addfile(e,io.BytesIO(raw))
   if member:tar.addfile(member)
  meta={'protected':protected,'slug':self.backup_id,'extra':extra if extra is not None else {'desk_cluster_sync':self.ticket},'homeassistant':{'version':'2026.10.1','exclude_database':False}}
  path=self.root/'native.tar'
  with tarfile.open(path,'w') as tar:
   for name,raw in {'./backup.json':json.dumps(meta).encode(),'./homeassistant.tar.gz':inner.getvalue()}.items():
    e=tarfile.TarInfo(name);e.size=len(raw);tar.addfile(e,io.BytesIO(raw))
  return path
 def test_native_archive_roundtrip_auth_custom_components_and_recorder(self):
  blob,manifest=snapshot.import_native(self.archive(),self.key,'pinned',self.ticket,self.backup_id);dst=self.root/'restored';snapshot.unpack(blob,self.key,'pinned',dst)
  self.assertEqual(json.loads((dst/'.storage/auth').read_text())['data']['users'],['original']);self.assertTrue((dst/'custom_components/pilot/__init__.py').exists())
  with closing(sqlite3.connect(dst/'home-assistant_v2.db')) as db:self.assertEqual(db.execute('select v from evidence').fetchone()[0],'live-backup')
  self.assertEqual(snapshot.config_digest(dst),manifest['config_digest'])
 def test_foreign_encrypted_and_wrong_version_archives_rejected(self):
  for kwargs in ({'extra':{}},{'protected':True}):
   with self.assertRaises(ValueError):snapshot.import_native(self.archive(**kwargs),self.key,'pinned',self.ticket,self.backup_id)
 def test_links_traversal_duplicates_and_size_bombs_rejected(self):
  for name,kind in [('data/../../escape',tarfile.REGTYPE),('data/link',tarfile.SYMTYPE),('data/.storage/auth',tarfile.REGTYPE)]:
   e=tarfile.TarInfo(name);e.type=kind;e.linkname='/etc/passwd'
   with self.assertRaises(ValueError):snapshot.import_native(self.archive(member=e),self.key,'pinned',self.ticket,self.backup_id)
  with patch.object(snapshot,'MAX_PLAIN',20):
   with self.assertRaises(ValueError):snapshot.import_native(self.archive(),self.key,'pinned',self.ticket,self.backup_id)
 def test_native_input_symlink_is_rejected(self):
  path=self.archive();link=self.root/'link.tar';link.symlink_to(path)
  with self.assertRaises(OSError):snapshot.import_native(link,self.key,'pinned',self.ticket,self.backup_id)
 def test_only_known_runtime_files_excluded_from_dirty_indicator_not_snapshot(self):
  root=self.root/'config';root.mkdir();(root/'.storage').mkdir();(root/'.storage/auth').write_text('{}');(root/'configuration.yaml').write_text('default_config:\n');before=snapshot.config_digest(root)
  for name in snapshot.RUNTIME_STATE:(root/name).write_text('{}')
  self.assertEqual(before,snapshot.config_digest(root));blob,_=snapshot.create(root,self.key,'pinned');dst=self.root/'restored';snapshot.unpack(blob,self.key,'pinned',dst);self.assertTrue((dst/'.storage/core.restore_state').exists())
  (root/'.storage/core.config_entries').write_text('{}');self.assertNotEqual(before,snapshot.config_digest(root))

class OnlineController(unittest.IsolatedAsyncioTestCase):
 def make(self):
  a=object.__new__(agent.Agent);a.ui_init();a.owned=lambda:True;a.ready=True;a.phase='active';a.op=None;a.generation='g';a.term=[1,'n'];a.node='n';a.members={'n':'ip'};return a
 async def test_reservation_deduplicates_and_rejects_stale_ownership(self):
  a=self.make();ticket=a.ui_begin()['ticket']
  with self.assertRaises(ValueError):a.ui_begin()
  a.generation='other'
  with self.assertRaises(ValueError):a.ui_check(ticket)
 async def test_expired_reservation_releases_busy_gate(self):
  a=self.make();a.ui_begin();a.ui_job['deadline']=0;a.expire_ui_job();self.assertFalse(a.ui_busy());self.assertEqual(a.ui_job['state'],'failed');self.assertIn('ticket',a.ui_begin())
 async def test_checkpoint_cannot_stop_core_while_native_backup_preparing(self):
  a=self.make();a.ui_begin()
  with patch.object(agent,'shell') as shell:
   with self.assertRaises(ValueError):await a.checkpoint()
   shell.assert_not_called()
 async def test_online_sync_keeps_ready_core_and_never_calls_lifecycle_commands(self):
  a=self.make();ticket=a.ui_begin()['ticket'];a.propose=AsyncMock(return_value=8);a.disk_digest=None
  with tempfile.TemporaryDirectory() as tmp,patch.object(agent,'ROOT',pathlib.Path(tmp)),patch.object(agent,'shell') as shell:
   root=pathlib.Path(tmp);(root/'blobs').mkdir();(root/'config/backups').mkdir(parents=True);(root/'current').symlink_to(root/'config');(root/'config/configuration.yaml').write_text('a: b')
   value={'digest':'f'*64,'config_digest':'e'*64}
   def worker(*args,**kw):kw['destination'].write_bytes(b'encrypted');return json.dumps(value)
   with patch.object(agent,'worker',side_effect=worker):await a.online_sync(ticket,'native.tar','b'*8)
   shell.assert_not_called();self.assertTrue(a.ready);self.assertEqual(a.phase,'active');self.assertEqual(a.ui_job['state'],'complete');self.assertEqual(a.applied,8)
 async def test_failed_replication_keeps_local_core_and_previous_revision(self):
  a=self.make();a.applied=7;ticket=a.ui_begin()['ticket'];a.propose=AsyncMock(side_effect=ValueError('quorum unavailable'))
  with tempfile.TemporaryDirectory() as tmp,patch.object(agent,'ROOT',pathlib.Path(tmp)),patch.object(agent,'shell') as shell:
   root=pathlib.Path(tmp);(root/'blobs').mkdir();(root/'config/backups').mkdir(parents=True);(root/'current').symlink_to(root/'config')
   def worker(*args,**kw):kw['destination'].write_bytes(b'blob');return json.dumps({'digest':'f'*64})
   with patch.object(agent,'worker',side_effect=worker):
    with self.assertRaises(ValueError):await a.online_sync(ticket,'native.tar','b'*8)
   self.assertTrue(a.ready);self.assertEqual(a.applied,7);self.assertEqual(a.ui_job['state'],'failed');shell.assert_not_called()
 async def test_socket_rejects_lifecycle_commands(self):
  a=self.make();reader=asyncio.StreamReader();reader.feed_data(b'{"action":"handoff"}\n');reader.feed_eof();writer=Mock();writer.drain=AsyncMock();writer.wait_closed=AsyncMock()
  await a.ui_cli(reader,writer);self.assertFalse(json.loads(writer.write.call_args.args[0])['ok'])

if __name__=='__main__':unittest.main()
