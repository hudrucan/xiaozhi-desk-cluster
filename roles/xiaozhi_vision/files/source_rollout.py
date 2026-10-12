import hashlib,json,os,re,subprocess,sys,time
from pathlib import Path
from urllib.request import build_opener,ProxyHandler

phase,sha,unit=sys.argv[1:]
if not re.fullmatch('[0-9a-f]{40}',sha) or unit not in ('xiaozhi-worker','xiaozhi-core','xiaozhi-control-plane'): raise SystemExit('Invalid rollout arguments')
entry={'xiaozhi-worker':'worker_asr.py','xiaozhi-core':'core_server.py','xiaozhi-control-plane':'control_plane.py'}[unit]
requirement={'xiaozhi-worker':'requirements-worker-voice.txt','xiaozhi-core':'requirements-core-voice.txt','xiaozhi-control-plane':'requirements-control-plane.txt'}[unit]
root=Path('/var/lib/xiaozhi-vision-rollout')/sha
record=root/(unit+'.json')
fragment=Path('/etc/systemd/system')/(unit+'.service')
opener=build_opener(ProxyHandler({}))
def command(args): return subprocess.check_output(args,text=True,stderr=subprocess.DEVNULL,timeout=90).strip()
def get(url):
 with opener.open(url,timeout=4) as r: return json.loads(r.read(1048576))
def private():
 # Public deployment metadata contains private peer addresses, never credentials.
 value=json.load(open('/etc/desk-dashboard/config.json'))
 return next(m['private'] for m in value['members'] if m['id']==value['node'])
def frozen():
 paths=['/etc/xiaozhi-worker-asr/config.json','/etc/xiaozhi-worker-llm/config.json','/etc/xiaozhi-worker-tts/config.json','/etc/xiaozhi-core-tts/config.json','/etc/xiaozhi-worker.env','/etc/xiaozhi-core.env','/etc/xiaozhi-control-plane.env','/etc/xiaozhi-core-runtime/revision.env']
 return {p:hashlib.sha256(Path(p).read_bytes()).hexdigest() for p in paths if Path(p).is_file()}
def metadata():
 text=fragment.read_text()
 work=command(['systemctl','show',unit,'--property=WorkingDirectory','--value'])
 match=re.search(r'^ExecStart=(/opt/[^\s]+/bin/python) ((?:/opt/[^\s]+/)?'+re.escape(entry)+r')$',text,re.M)
 if not match or not work.startswith('/opt/') or not work.endswith('/repo/main/xiaozhi-server'): raise ValueError('Unexpected direct runtime layout')
 if len(re.findall(r'^WorkingDirectory=',text,re.M))!=1: raise ValueError('Unexpected working directory composition')
 source_work=str(Path(match[2]).parent) if Path(match[2]).is_absolute() else work
 if not source_work.endswith('/repo/main/xiaozhi-server'): raise ValueError('Unexpected source entrypoint')
 return {'work':work,'source_work':source_work,'python':match[1],'unit':text,'frozen':frozen(),'head':command(['git','-C',str(Path(source_work).parents[1]),'rev-parse','HEAD'])}
def readiness(data):
 if frozen()!=data['frozen']: raise ValueError('Runtime bundle/environment drift')
 host=private()
 cloud=get('http://'+host+':8004/api/cluster')['configuration']
 if cloud['desired_revision']!=data['revision'] or cloud['sync_status']!='synced': return False
 if unit=='xiaozhi-control-plane':
  return command(['systemctl','is-active',unit])=='active' and get('http://'+host+':8004/healthz')['healthy'] is True
 if unit=='xiaozhi-core':
  core=get('http://'+host+':8000/status')
  return core['status']=='ready' and core['worker_rpc']['state']=='connected' and core['tts']['revision']==data['revision']
 runtime=get('http://'+host+':8004/api/settings/runtime')
 node=os.uname().nodename
 rows=runtime.get('nodes',[])
 return any(row.get('node_id',row.get('node'))==node and row.get('worker_revision')==data['revision'] and row.get('worker_ready',row.get('ready')) is True for row in rows)
def atomic_write(path, text, mode):
 temporary=path.with_name('.'+path.name+'.vision-staged')
 with temporary.open('w') as stream:
  os.chmod(temporary,mode);stream.write(text);stream.flush();os.fsync(stream.fileno())
 os.replace(temporary,path)
 descriptor=os.open(path.parent,os.O_DIRECTORY)
 try:os.fsync(descriptor)
 finally:os.close(descriptor)
def write_record(data):
 root.mkdir(parents=True,exist_ok=True,mode=0o700)
 atomic_write(record,json.dumps(data),0o600)
try:
 if phase=='preflight':
  data=metadata(); host=private();cloud=get('http://'+host+':8004/api/cluster')['configuration']
  if cloud['sync_status']!='synced': raise ValueError('Cloud is not synced')
  data['revision']=cloud['desired_revision'];data['private']=host
  if unit=='xiaozhi-worker':
   command([data['python'],'-c','from google.genai import types; types.HttpOptions(timeout=30000,retry_options=types.HttpRetryOptions(attempts=1))'])
  write_record(data)
  print(json.dumps({'node':os.uname().nodename,'unit':unit,'revision':data['revision'],'source':data['head']}))
 elif phase=='stage':
  data=json.loads(record.read_text());old_repo=Path(data.get('source_work',data['work'])).parents[1]
  if frozen()!=data['frozen']: raise ValueError('Runtime configuration changed')
  command(['git','-C',str(old_repo),'diff','--quiet']);command(['git','-C',str(old_repo),'diff','--cached','--quiet'])
  command(['git','-C',str(old_repo),'fetch','--no-tags','origin',sha])
  # Check every frozen requirements file, including nested requirements, before source activation.
  for req in Path(data.get('source_work',data['work'])).glob('requirements*.txt'):
   current=req.read_text();new=command(['git','-C',str(old_repo),'show',sha+':main/xiaozhi-server/'+req.name])
   if current.strip()!=new.strip(): raise ValueError('Dependencies changed; use a full dependency rollout')
  if unit=='xiaozhi-control-plane':
   data['new_work']=data['work']
  else:
   new_repo=Path('/opt/xiaozhi-vision/releases')/sha/unit/'repo'
   new_repo.parent.mkdir(parents=True,exist_ok=True)
   if not new_repo.exists(): command(['git','-C',str(old_repo),'worktree','add','--detach',str(new_repo),sha])
   if command(['git','-C',str(new_repo),'rev-parse','HEAD'])!=sha: raise ValueError('Source revision differs')
   data['new_work']=str(new_repo/'main/xiaozhi-server')
   (Path(data['new_work'])/'data').mkdir(exist_ok=True)
  write_record(data);print(json.dumps({'unit':unit,'staged':True}))
 elif phase=='activate':
  data=json.loads(record.read_text())
  if frozen()!=data['frozen'] or fragment.read_text()!=data['unit']: raise ValueError('Runtime/unit drift before activation')
  if data['head']==sha:
   print(json.dumps({'unit':unit,'activated':False,'already_current':True}));raise SystemExit(0)
  if unit=='xiaozhi-control-plane':
   command(['git','-C',str(Path(data['work']).parents[1]),'checkout','--detach',sha])
  else:
   new=re.sub(r'^ExecStart='+re.escape(data['python'])+r' (?:/opt/[^\s]+/)?'+re.escape(entry)+r'$', 'ExecStart='+data['python']+' '+str(Path(data['new_work'])/entry),data['unit'],flags=re.M)
   # Preserve the existing data root as a read-only namespace mount, without a symlink.
   if (Path(data['work'])/'data').exists():
    new=new.replace('[Service]','[Service]\nBindReadOnlyPaths='+data['work']+'/data:'+data['new_work']+'/data',1)
   atomic_write(fragment,new,0o644)
  command(['systemctl','daemon-reload']);command(['systemctl','restart',unit])
  print(json.dumps({'unit':unit,'activated':True}))
 elif phase=='ready':
  data=json.loads(record.read_text())
  print(json.dumps({'unit':unit,'ready':readiness(data),'revision':data['revision']}))
 elif phase=='rollback':
  data=json.loads(record.read_text());atomic_write(fragment,data['unit'],0o644)
  if unit=='xiaozhi-control-plane':command(['git','-C',str(Path(data['work']).parents[1]),'checkout','--detach',data['head']])
  command(['systemctl','daemon-reload']);command(['systemctl','restart',unit]);print(json.dumps({'unit':unit,'rolled_back':True}))
 else: raise ValueError('Unknown rollout phase')
except Exception:
 print(json.dumps({'unit':unit,'phase':phase,'error':'Source rollout check failed; inspect safe unit metadata'}));raise SystemExit(1)
