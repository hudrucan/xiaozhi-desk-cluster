import importlib.util,json,sqlite3,tempfile,unittest
from contextlib import closing
from pathlib import Path
from cryptography.fernet import Fernet
spec=importlib.util.spec_from_file_location('seed',Path(__file__).resolve().parents[1]/'files/config_seed.py')
m=importlib.util.module_from_spec(spec);spec.loader.exec_module(m)

class SeedTests(unittest.TestCase):
 def setUp(self):
  self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup);root=Path(self.tmp.name);self.db=root/'data.db'
  self.key=Fernet.generate_key();self.config={'key':self.key.decode(),'seed':str(root/'seed.fernet'),'journal':str(root/'checkpoint.fernet')}
  seed={'auth':{name:{'canonicaladmin':{'email':'admin@desk.cluster','password':'chosen source password hash'}} for name in ('users','_superusers')}}
  Path(self.config['seed']).write_bytes(Fernet(self.key).encrypt(json.dumps(seed).encode()))
  with closing(sqlite3.connect(self.db)) as db:
   for name in ('users','_superusers'):
    db.execute('CREATE TABLE '+name+' (id TEXT PRIMARY KEY,email TEXT,password TEXT,tokenKey TEXT)')
    db.execute('INSERT INTO '+name+' VALUES (?,?,?,?)',('localadmin','admin@desk.cluster','original local password hash','original token key'))
   db.execute('CREATE TABLE system_stats (id TEXT PRIMARY KEY,data TEXT)');db.execute('INSERT INTO system_stats VALUES (?,?)',('unchanged history','local metrics'));db.commit()
 def auth(self):
  with closing(sqlite3.connect(self.db)) as db:return {name:db.execute('SELECT * FROM '+name).fetchall() for name in ('users','_superusers')}
 def test_initial_alignment_keeps_ids_and_metrics_and_encrypts_backup(self):
  self.assertTrue(m.align(self.config,self.db))
  for rows in self.auth().values():
   self.assertEqual(rows[0][0],'localadmin');self.assertEqual(rows[0][2],'chosen source password hash');self.assertNotEqual(rows[0][3],'original token key')
  with closing(sqlite3.connect(self.db)) as db:self.assertEqual(db.execute('SELECT * FROM system_stats').fetchall(),[('unchanged history','local metrics')])
  backup=Path(self.tmp.name)/'initial-auth-rollback.fernet'
  self.assertNotIn(b'original local password hash',backup.read_bytes());self.assertEqual(backup.stat().st_mode&0o777,0o600)
  self.assertFalse(m.align(self.config,self.db))
 def test_failed_initial_migration_restores_only_authentication(self):
  before=self.auth();m.align(self.config,self.db);self.assertTrue(m.rollback(self.config,self.db));self.assertEqual(before,self.auth())
  # A retry reuses the matching original encrypted backup instead of failing.
  self.assertTrue(m.align(self.config,self.db))
 def test_bootstrap_seed_never_overwrites_a_later_shared_password(self):
  Path(self.config['journal']).write_bytes(b'committed replica checkpoint')
  self.assertFalse(m.align(self.config,self.db));self.assertEqual(self.auth()['users'][0][2],'original local password hash')
 def test_old_pocketbase_admin_email_is_aligned_without_replacing_its_id(self):
  with closing(sqlite3.connect(self.db)) as db:db.execute("UPDATE _superusers SET email='old@desk.cluster'");db.commit()
  before=self.auth();self.assertTrue(m.align(self.config,self.db))
  self.assertEqual(self.auth()['_superusers'][0][:2],('localadmin','admin@desk.cluster'))
  m.rollback(self.config,self.db);self.assertEqual(before,self.auth())
 def test_missing_identity_is_rejected_before_any_authentication_change(self):
  with closing(sqlite3.connect(self.db)) as db:db.execute("UPDATE users SET email='different@desk.cluster'");db.commit()
  before=self.auth()
  with self.assertRaises(ValueError):m.align(self.config,self.db)
  self.assertEqual(before,self.auth())
if __name__=='__main__':unittest.main()
