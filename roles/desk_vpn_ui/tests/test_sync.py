import ast,base64,os,copy,hashlib,json,sqlite3,tempfile,unittest
from pathlib import Path
from cryptography.fernet import Fernet
import sys
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'files'))
import database,seed,sync
from config_journal import Journal,Failure,digest
FIXTURE=Path(__file__).resolve().parent/'fixtures/wg-easy'
MIGRATIONS=Path(os.environ['WG_EASY_SOURCE'])/'src/server/database/migrations' if 'WG_EASY_SOURCE' in os.environ else FIXTURE/'migrations'

class Fixture(unittest.TestCase):
 def test_bundled_migrations_match_the_build_pin_and_recorded_hashes(self):
  manifest=json.loads((FIXTURE/'manifest.json').read_text())
  build=Path(__file__).resolve().parents[3]/'scripts/build-vpn-ui.py'
  pin=next(n.value.value for n in ast.parse(build.read_text()).body if isinstance(n,ast.Assign) and any(isinstance(t,ast.Name) and t.id=='PIN' for t in n.targets))
  self.assertEqual(manifest['upstream_revision'],pin)
  for name,digest in manifest['files'].items():
   with self.subTest(file=name):self.assertEqual(hashlib.sha256((FIXTURE/name).read_bytes()).hexdigest(),digest)
  journal=json.loads((FIXTURE/'migrations/meta/_journal.json').read_text())
  expected={entry['tag']+'.sql' for entry in journal['entries']}
  self.assertEqual({p.name for p in (FIXTURE/'migrations').glob('*.sql')},expected)
  self.assertEqual({name for name in manifest['files'] if name.startswith('migrations/')},{'migrations/meta/_journal.json'}|{'migrations/'+name for name in expected})
class Native:
 def __init__(self,path):self.path=path
 def request(self,path,method,raw):
  with sync.database.closing(sqlite3.connect(self.path)) as db, db:db.execute('UPDATE clients_table SET name=?',(json.loads(raw)['name'],))
  return 200,{},b'{}'
class Tests(unittest.TestCase):
 def setUp(self):
  self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup);root=Path(self.temp.name);self.clusters={};key=Fernet.generate_key();creds={'server_private':base64.b64encode(os.urandom(32)).decode(),'server_public':'','client_public':''};creds['server_public']=database.public(creds['server_private']);client=base64.b64encode(os.urandom(32)).decode();creds['client_public']=database.public(client)
  value=seed.make(MIGRATIONS,root/'seed.db',creds,client,'hash');(root/'seed.json').write_text(json.dumps(value))
  for node in ('n1','n2','n3'):
   db=root/(node+'.db');seed.make(MIGRATIONS,db,creds,client,'hash');database.apply(db,value)
   c={'node':node,'key':key.decode(),'members':dict(n1='a',n2='b',n3='c'),'certificate':None,'database':str(db),'seed':str(root/'seed.json'),'simulate':True,'ready':str(root/(node+'.ready'))}
   j=Journal(root/(node+'.journal'),Fernet(key),node,c['members'],key);j.initialize(value)
   x=sync.Cluster.__new__(sync.Cluster);x.c=c;x.journal=j;x.native=Native(db);x.seed=value;x.lock=sync.threading.RLock();x.pool=sync.ThreadPoolExecutor(6);self.addCleanup(x.pool.shutdown);x.key=key;x.heads={};x.error=None;x.initialized=True;x.event=sync.threading.Event();x.stats_cache=(0,[]);x.apply();self.clusters[node]=x
  self.off=set()
  for x in self.clusters.values():
   def call(n,a,v=None):
    if n in self.off:raise OSError('offline')
    return self.clusters[n].peer(a,v or {})
   x.call=call
 def test_quorum_offline_catchup(self):
  self.off.add('n3');x=self.clusters['n1'];x.mutate('/api/client/1','POST',b'{"name":"new"}')
  self.assertEqual(x.journal.state['revision'],1);self.assertEqual(self.clusters['n2'].journal.state['revision'],1);self.assertEqual(self.clusters['n3'].journal.state['revision'],0)
  self.off.clear();self.clusters['n3'].refresh();self.assertEqual(database.read(self.clusters['n3'].c['database'])['tables']['clients_table'][0]['name'],'new')
 def test_no_quorum_rolls_back(self):
  self.off.update(('n2','n3'));x=self.clusters['n1']
  with self.assertRaises(Failure):x.mutate('/api/client/1','POST',b'{"name":"failed"}')
  self.assertEqual(database.read(x.c['database'])['tables']['clients_table'][0]['name'],'iPhone')
 def test_carries_forward_accepted_proposal(self):
  x=self.clusters['n1'];v=copy.deepcopy(x.seed);v['tables']['clients_table'][0]['name']='previous'
  for n in ('n1','n2'):
   self.clusters[n].journal.promise(1,[1,'n2']);self.clusters[n].journal.accept(1,[1,'n2'],v)
  with self.assertRaises(Failure):x.mutate('/api/client/1','POST',b'{"name":"later"}')
  self.assertEqual(x.journal.state['value']['tables']['clients_table'][0]['name'],'previous')
 def test_no_false_failure_after_commit(self):
  x=self.clusters['n1'];original=x.apply
  def apply(force=False):
   if force:raise OSError('kernel unavailable')
   return original(force)
  x.apply=apply
  self.assertEqual(x.mutate('/api/client/1','POST',b'{"name":"committed"}')[0],200)
  self.assertEqual(x.journal.state['revision'],1);self.assertEqual(x.error,'apply_pending')
 def test_validation_and_disabled_peer(self):
  x=self.clusters['n1'];v=copy.deepcopy(x.seed);v['tables']['clients_table'][0]['enabled']=0;database.validate(v,x.seed);self.assertEqual(database.tunnel(v)[1],{})
  for field,value in [('post_up','touch /tmp/evil'),('server_allowed_ips','["0.0.0.0/0"]'),('ipv4_address','192.168.1.1')]:
   v=copy.deepcopy(x.seed);v['tables']['clients_table'][0][field]=value
   with self.assertRaises(ValueError):database.validate(v,x.seed)
 def test_staged_assets_are_manifest_independent_and_bounded(self):
  root=Path(self.temp.name)/'assets';root.mkdir();(root/'new-hash.js').write_text('export default 1;')
  data,mime=sync.asset('/vpn/_nuxt/new-hash.js',root);self.assertEqual(data,b'export default 1;');self.assertIn(mime,('text/javascript','application/javascript'))
  for path in ('/vpn/_nuxt/../seed.json','/vpn/_nuxt/%2e%2e/seed.json'):
   with self.assertRaises(Failure):sync.asset(path,root)
 def test_reject_unsigned_commit(self):
  x=self.clusters['n1'];v=copy.deepcopy(x.seed)
  with self.assertRaises(Failure):x.peer('commit',{'revision':1,'value':v,'certificate':[]})
if __name__=='__main__':unittest.main()
