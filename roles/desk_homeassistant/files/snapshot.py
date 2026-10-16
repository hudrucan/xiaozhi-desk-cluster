"""Consistent stopped-Core bundles; authenticated encryption, bounded extraction."""
from contextlib import closing
import argparse,hashlib,io,json,os,pathlib,re,shutil,sqlite3,tarfile,tempfile,time
from cryptography.fernet import Fernet
from config_journal import encoded,atomic
MAX_BLOB=64*1024*1024
MAX_PLAIN=128*1024*1024
MAX_FILES=8192
EXCLUDED={'.ha_run.lock','__pycache__','.git','backups','tmp_backups','.cache','tts','deps','media'}
# Keep these in recovery bundles, but do not mistake runtime/backup bookkeeping
# for configuration edits. Auth and registries deliberately remain tracked.
RUNTIME_STATE={'.storage/core.restore_state','.storage/trace.saved_traces','.storage/backup'}
def sha(raw):return hashlib.sha256(raw).hexdigest()
def safe_name(name):
 p=pathlib.PurePosixPath(name)
 if not name or len(name)>512 or p.is_absolute() or '..' in p.parts or str(p)!=name or '\\' in name or '\x00' in name:raise ValueError('Unsafe snapshot path')
 return p

def included(name):
 p=pathlib.PurePosixPath(name)
 return not any(v in EXCLUDED for v in p.parts) and not p.name.startswith('home-assistant.log') and not p.name.endswith(('.db-wal','.db-shm','.pyc','~'))

def config_digest(root):
 result={}
 for p in sorted(pathlib.Path(root).rglob('*')):
  name=p.relative_to(root).as_posix()
  if included(name) and name not in RUNTIME_STATE and p.is_file() and not p.is_symlink() and not name.endswith('.db'):
   result[name]=sha(p.read_bytes())
 return sha(encoded(result))

def import_native(path,key,image,ticket,backup_id):
 """Convert a completed, unencrypted native Core backup to our existing format.

 Never extract onto a running config. Reject links, duplicates and size bombs
 before writing; the staged generation is then checked by the normal validator.
 """
 if not re.fullmatch('[a-f0-9]{32}',ticket) or not re.fullmatch('[a-f0-9]{8}',backup_id):raise ValueError('Invalid backup identity')
 path=pathlib.Path(path)
 fd=os.open(path,os.O_RDONLY|os.O_NOFOLLOW)
 with os.fdopen(fd,'rb') as source:
  if os.fstat(source.fileno()).st_size>MAX_BLOB:raise ValueError('Native backup too large')
  with tarfile.open(fileobj=source,mode='r:') as outer:
   members={}
   for e in outer:
    name=e.name.removeprefix('./')
    if name not in {'backup.json','homeassistant.tar.gz'} or not e.isfile() or name in members or e.size>MAX_BLOB:raise ValueError('Invalid native backup')
    members[name]=e
   if set(members)!={'backup.json','homeassistant.tar.gz'} or members['backup.json'].size>65536:raise ValueError('Missing native data')
   meta=json.load(outer.extractfile(members['backup.json']))
   if meta.get('protected') is not False or meta.get('slug')!=backup_id or meta.get('extra',{}).get('desk_cluster_sync')!=ticket or meta.get('homeassistant',{}).get('version')!='2026.10.1' or meta['homeassistant'].get('exclude_database') is not False:raise ValueError('Backup provenance mismatch')
   with tempfile.TemporaryDirectory() as tmp:
    root=pathlib.Path(tmp);seen=set();size=0
    with tarfile.open(fileobj=outer.extractfile(members['homeassistant.tar.gz']),mode='r|gz') as inner:
     for e in inner:
      name=e.name.removeprefix('./').rstrip('/')
      if name=='data' and e.isdir():continue
      if not name.startswith('data/'):raise ValueError('Invalid config root')
      name=name[5:];safe_name(name)
      if name in seen or not (e.isdir() or e.isfile()):raise ValueError('Duplicate or nonregular native member')
      seen.add(name);size+=e.size
      if len(seen)>MAX_FILES or size>MAX_PLAIN:raise ValueError('Native backup exceeds boundary')
      if e.isdir() or not included(name):continue
      p=root/name;p.parent.mkdir(parents=True,exist_ok=True,mode=0o700)
      with p.open('xb') as dest:
       shutil.copyfileobj(inner.extractfile(e),dest,length=65536)
      os.chmod(p,0o600)
    if not (root/'home-assistant_v2.db').is_file():raise ValueError('Recorder backup missing')
    return create(root,key,image)

def create(root,key,image):
 root=pathlib.Path(root);files={};size=0
 with tempfile.TemporaryDirectory() as temporary:
  for p in sorted(root.rglob('*')):
   name=p.relative_to(root).as_posix()
   if not included(name):continue
   if p.is_symlink():raise ValueError('Symlinks are not snapshot files')
   if not p.is_file():continue
   safe_name(name)
   if p.stat().st_size>MAX_PLAIN:raise ValueError('Snapshot file too large')
   if name.endswith('.db'):
    backup=pathlib.Path(temporary)/'database'
    with closing(sqlite3.connect('file:'+str(p)+'?mode=ro',uri=True)) as source,closing(sqlite3.connect(backup)) as target:
     source.backup(target)
     if target.execute('PRAGMA quick_check').fetchone()[0]!='ok':raise ValueError('Invalid SQLite checkpoint')
    raw=backup.read_bytes();backup.unlink()
   else:raw=p.read_bytes()
   size+=len(raw)
   if size>MAX_PLAIN or len(files)>=MAX_FILES:raise ValueError('Snapshot exceeds boundary')
   files[name]=raw
  if 'configuration.yaml' not in files or '.storage/auth' not in files:raise ValueError('Retain completed onboarding and auth')
  before=config_digest(root)
  manifest={'format':'desk-homeassistant-bundle-v1','image':image,'at':time.time(),'config_digest':before,'files':{n:{'size':len(b),'sha256':sha(b)} for n,b in files.items()}}
  output=io.BytesIO()
  with tarfile.open(fileobj=output,mode='w:gz',compresslevel=1) as tar:
   for name,raw in dict(files,**{'desk-checkpoint-manifest.json':encoded(manifest)}).items():
    entry=tarfile.TarInfo(name);entry.size=len(raw);entry.mode=0o600;tar.addfile(entry,io.BytesIO(raw))
  if config_digest(root)!=before:raise ValueError('Config changed while stopped')
  blob=Fernet(key).encrypt(output.getvalue())
  if len(blob)>MAX_BLOB:raise ValueError('Encrypted snapshot too large')
  return blob,manifest

def unpack(blob,key,image,destination):
 if len(blob)>MAX_BLOB:raise ValueError('Snapshot too large')
 raw=Fernet(key).decrypt(blob);destination=pathlib.Path(destination)
 with tarfile.open(fileobj=io.BytesIO(raw),mode='r:gz') as tar:
  entries=[];size=0
  for entry in tar:
   size+=entry.size;entries.append(entry)
   if len(entries)>MAX_FILES+1 or size>MAX_PLAIN+1024*1024:raise ValueError('Expanded snapshot too large')
  names=set()
  for e in entries:
   safe_name(e.name)
   if not e.isfile() or e.name in names:raise ValueError('Nonregular/duplicate snapshot member')
   names.add(e.name)
  if 'desk-checkpoint-manifest.json' not in names:raise ValueError('Manifest missing')
  manifest=json.load(tar.extractfile('desk-checkpoint-manifest.json'))
  if manifest.get('format')!='desk-homeassistant-bundle-v1' or manifest.get('image')!=image or set(manifest['files'])!=names-{'desk-checkpoint-manifest.json'}:raise ValueError('Snapshot artifact mismatch')
  for e in entries:
   if e.name=='desk-checkpoint-manifest.json':continue
   raw=tar.extractfile(e).read();expected=manifest['files'][e.name]
   if len(raw)!=expected['size'] or sha(raw)!=expected['sha256']:raise ValueError('File checksum mismatch')
   if e.name.endswith('.json') or e.name.startswith('.storage/'):
    json.loads(raw)
   p=destination/e.name;p.parent.mkdir(parents=True,exist_ok=True,mode=0o700);atomic(p,raw)
   if e.name.endswith('.db'):
    with closing(sqlite3.connect('file:'+str(p)+'?mode=ro',uri=True)) as db:
     if db.execute('pragma quick_check').fetchone()[0]!='ok':raise ValueError('SQLite integrity failure')
  if not (destination/'.storage/auth').is_file() or not (destination/'configuration.yaml').is_file():raise ValueError('Core files missing')
  return manifest

def main():
 p=argparse.ArgumentParser();p.add_argument('action',choices=['create','validate','restore','import-native']);p.add_argument('--config',default='/etc/desk-homeassistant/config.json');p.add_argument('--root');p.add_argument('--input');p.add_argument('--output');p.add_argument('--ticket');p.add_argument('--backup-id');a=p.parse_args()
 c=json.loads(pathlib.Path(a.config).read_bytes())
 if a.action in ('create','import-native'):
  blob,manifest=create(a.root,c['encryption_key'],c['image']) if a.action=='create' else import_native(a.input,c['encryption_key'],c['image'],a.ticket,a.backup_id)
  atomic(pathlib.Path(a.output),blob)
  print(json.dumps({'digest':sha(blob),'bytes':len(blob),'created_at':manifest['at'],'config_digest':manifest['config_digest'],'image':c['image'],'format':manifest['format']}))
 else:
  blob=pathlib.Path(a.input).read_bytes()
  if a.action=='restore':unpack(blob,c['encryption_key'],c['image'],a.output)
  else:
   with tempfile.TemporaryDirectory() as tmp:unpack(blob,c['encryption_key'],c['image'],tmp)
  print('Validated encrypted Core checkpoint')
if __name__=='__main__':
 try:main()
 except Exception:raise SystemExit('Core checkpoint failed; private contents withheld')
