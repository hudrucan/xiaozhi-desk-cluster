#!/usr/bin/env python3
"""Read existing Beszel configuration and align initial auth while its hub is stopped."""
import argparse, json, os, secrets, sqlite3, string
from contextlib import closing
from pathlib import Path

COLLECTIONS=('users','_superusers','systems','user_settings','alerts','quiet_hours','network_monitors','fingerprints','universal_tokens')
FIELDS={
 'users':('email','emailVisibility','verified','name','username','role'),
 '_superusers':('email','emailVisibility','verified'),
 'systems':('name','host','port','users','status'),
 'user_settings':('user','settings'),
 'alerts':('user','system','name','min','value'),
 'quiet_hours':('user','system','start','end','type'),
 'network_monitors':('system','server','target','protocol','port','interval','enabled'),
 'fingerprints':('system','token'),
 'universal_tokens':('user','token'),
}
DATABASE=Path('/var/lib/beszel-hub/data.db')

def snapshot(database=DATABASE):
 with closing(sqlite3.connect('file:'+str(database)+'?mode=ro',uri=True)) as db:
  db.row_factory=sqlite3.Row;db.execute('BEGIN')
  types={name:{f['name']:f['type'] for f in json.loads(fields)} for name,fields in db.execute('SELECT name,fields FROM _collections')}
  collections={name:{} for name in COLLECTIONS};auth={}
  for name in COLLECTIONS:
   for original in db.execute('SELECT * FROM '+name):
    if name=='users' and original['avatar']:raise ValueError('Avatar files need explicit migration')
    value={}
    for field in FIELDS[name]:
     raw=original[field];kind=types[name][field]
     if kind=='bool':raw=bool(raw)
     if kind=='json' or (kind=='relation' and raw and raw.startswith('[')):
      raw=json.loads(raw) if isinstance(raw,str) and raw else None
     value[field]=raw
    if name=='systems':value['paused']=value.pop('status')=='paused'
    collections[name][original['id']]=value
    if name in ('users','_superusers'):
     auth.setdefault(name,{})[original['id']]={'email':original['email'],'password':original['password']}
  settings=json.loads(db.execute("SELECT value FROM _params WHERE id='settings'").fetchone()[0])
  if not isinstance(settings,dict):raise ValueError('Encrypted native settings require migration')
  settings['batch']['enabled']=True
  settings['batch']['timeout']=max(30,settings['batch'].get('timeout',0))
  return {'value':{'collections':collections,'settings':settings},'auth':auth}

def cipher(config):
 from cryptography.fernet import Fernet
 return Fernet(config['key'].encode())

def read_seed(config):return json.loads(cipher(config).decrypt(Path(config['seed']).read_bytes()))

def backup(config,database=DATABASE):
 path=Path(config['journal']).with_name('pre-sync-config.fernet')
 if path.exists() or Path(config['journal']).exists():return False
 fd=os.open(path,os.O_WRONLY|os.O_CREAT|os.O_EXCL,0o600)
 with os.fdopen(fd,'wb') as output:
  output.write(cipher(config).encrypt(json.dumps(snapshot(database)).encode()));output.flush();os.fsync(output.fileno())
 return True

def auth_plan(config,database=DATABASE):
 seed=read_seed(config);changes=[]
 with closing(sqlite3.connect('file:'+str(database)+'?mode=ro',uri=True)) as db:
  for collection,records in seed['auth'].items():
   for item in records.values():
    rows=db.execute('SELECT id,password,tokenKey,email FROM '+collection+' WHERE email=?',(item['email'],)).fetchall()
    if not rows and collection=='_superusers':
     rows=db.execute('SELECT id,password,tokenKey,email FROM _superusers').fetchall()
    if len(rows)!=1:raise ValueError('Existing authentication identity ambiguous')
    identity,password,token,email=rows[0]
    if password!=item['password'] or email!=item['email']:changes.append((collection,identity,item['password'],item['email']))
 return changes

def align(config,database=DATABASE):
 if Path(config['journal']).exists():return False
 changes=auth_plan(config,database)
 if not changes:return False
 backup=Path(config['journal']).with_name('initial-auth-rollback.fernet')
 with closing(sqlite3.connect(database)) as db:
  originals=[{'collection':collection,'id':identity,'password':db.execute('SELECT password FROM '+collection+' WHERE id=?',(identity,)).fetchone()[0],
              'tokenKey':db.execute('SELECT tokenKey FROM '+collection+' WHERE id=?',(identity,)).fetchone()[0],
              'email':db.execute('SELECT email FROM '+collection+' WHERE id=?',(identity,)).fetchone()[0]} for collection,identity,_,_ in changes]
  if backup.exists():
   if json.loads(cipher(config).decrypt(backup.read_bytes()))!=originals:raise ValueError('Initial auth rollback differs')
  else:
   fd=os.open(backup,os.O_WRONLY|os.O_CREAT|os.O_EXCL,0o600)
   with os.fdopen(fd,'wb') as output:output.write(cipher(config).encrypt(json.dumps(originals).encode()));output.flush();os.fsync(output.fileno())
  with db:
   for collection,identity,password,email in changes:
    token=''.join(secrets.choice(string.ascii_letters+string.digits) for _ in range(50))
    db.execute('UPDATE '+collection+' SET password=?,tokenKey=?,email=? WHERE id=?',(password,token,email,identity))
 return True

def rollback(config,database=DATABASE):
 path=Path(config['journal']).with_name('initial-auth-rollback.fernet')
 if not path.exists():return False
 originals=json.loads(cipher(config).decrypt(path.read_bytes()))
 with closing(sqlite3.connect(database)) as db:
  with db:
   for row in originals:
    if row['collection'] not in ('users','_superusers'):raise ValueError('Invalid rollback scope')
    db.execute('UPDATE '+row['collection']+' SET password=?,tokenKey=?,email=? WHERE id=?',(row['password'],row['tokenKey'],row['email'],row['id']))
 return True

if __name__=='__main__':
 parser=argparse.ArgumentParser();parser.add_argument('action',choices=('snapshot','inspect','align','rollback','backup'));parser.add_argument('--config',default='/etc/desk-monitoring/config-sync.json');args=parser.parse_args()
 try:
  if args.action=='snapshot':print(json.dumps(snapshot()))
  else:
   config=json.loads(Path(args.config).read_bytes())
   if args.action=='backup':print('changed' if backup(config) else 'unchanged')
   elif args.action=='inspect':print('changed' if not Path(config['journal']).exists() and auth_plan(config) else 'unchanged')
   else:print('changed' if (align if args.action=='align' else rollback)(config) else 'unchanged')
 except Exception:raise SystemExit('Beszel initial configuration preparation failed; no credentials printed')
