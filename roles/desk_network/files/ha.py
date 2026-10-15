#!/usr/bin/env python3
"""One LAN collector, majority leases, certified consistent SQLite checkpoints."""
from contextlib import closing
import ast, signal, argparse, base64, concurrent.futures, gzip, hashlib, hmac, http.client, http.server, io, json, logging, os, pathlib, re, secrets, socket, sqlite3, ssl, subprocess, sys, threading, time, urllib.parse, urllib.request
LOG = logging.getLogger('desk-network')
TTL, MARGIN, LIMIT, DBLIMIT = 15., 5., 16*1024*1024, 64*1024*1024
def clock():return time.clock_gettime(time.CLOCK_BOOTTIME) if hasattr(time,'CLOCK_BOOTTIME') else time.monotonic()
NAME = 'desk-netalertx'
IMAGE = 'ghcr.io/netalertx/netalertx@sha256:f95bca7a794141296dd1d16be5bb7f39f6fdbcd20353cdb51eee696e12d317f6'
def encoded(v): return json.dumps(v,sort_keys=True,separators=(',',':')).encode()
def sha(b): return hashlib.sha256(b).hexdigest()
def read_bounded(stream,limit):
 # read(limit+1) makes BufferedReader reserve the full ceiling, even for tiny replies.
 # Grow only with bytes actually received; never allocate the ceiling up front.
 out=io.BytesIO();size=0
 while True:
  part=stream.read(min(65536,limit+1-size))
  if not part:break
  size+=len(part)
  if size>limit:raise Unavailable('Response exceeds byte limit')
  out.write(part)
 return out.getvalue()
def atomic(p,b):
 p=pathlib.Path(p);p.parent.mkdir(parents=True,exist_ok=True);tmp=p.with_name(p.name+'.tmp-'+secrets.token_hex(4))
 fd=os.open(tmp,os.O_WRONLY|os.O_CREAT|os.O_EXCL,0o600)
 try:
  with os.fdopen(fd,'wb') as f: f.write(b);f.flush();os.fsync(f.fileno())
  os.replace(tmp,p)
  d=os.open(p.parent,os.O_RDONLY|os.O_DIRECTORY)
  try:os.fsync(d)
  finally:os.close(d)
 finally:tmp.unlink(missing_ok=True)
class Unavailable(Exception): pass
class Lease:
 """Boot quarantine preserves grants lost from RAM across a voter restart."""
 def __init__(self,path,clock=clock):
  self.path=pathlib.Path(path);self.clock=clock;self.lock=threading.RLock()
  self.term=json.loads(self.path.read_text()) if self.path.exists() else [0,'']
  self.quarantine=clock()+TTL+2;self.until=self.quarantine;self.holder=None
 def grant(self,term,node):
  with self.lock:
   now=self.clock()
   if len(term)!=2 or not isinstance(term[0],int) or term[0]<1 or term[1]!=node:raise Unavailable('Invalid lease term')
   if now<self.until and (self.holder!=node or term!=self.term):raise Unavailable('Lease held or voter warming up')
   if term<self.term:raise Unavailable('Term superseded')
   if term!=self.term:atomic(self.path,encoded(term));self.term=term
   self.holder=node;self.until=now+TTL
   return {'term':term,'holder':node,'ttl':TTL}
 def status(self):
  with self.lock:return {'promised':self.term,'holder':self.holder if self.until>self.clock() else None,'remaining':max(0,self.until-self.clock())}
class Store:
 def __init__(self,root,key,members):
  self.root=pathlib.Path(root);self.root.mkdir(parents=True,exist_ok=True);self.key=key;self.members=members;self.lock=threading.RLock()
  self.head=json.loads((self.root/'head.json').read_text()) if (self.root/'head.json').exists() else None
 def receipt(self,node,meta):
  value={'node':node,'revision':meta['revision'],'digest':meta['digest']}
  return dict(value,signature=hmac.new(self.key,encoded(value),hashlib.sha256).hexdigest())
 def verify(self,meta):
  if len(meta['revision'])!=3 or meta['revision'][1] not in self.members or not re.fullmatch('[a-f0-9]{64}',meta['digest']):raise Unavailable('Invalid checkpoint metadata')
  seen=set()
  for r in meta['certificate']:
   if r['node'] not in self.members or r['revision']!=meta['revision'] or r['digest']!=meta['digest']:raise Unavailable('Invalid checkpoint receipt')
   v={k:r[k] for k in ('node','revision','digest')}
   if not hmac.compare_digest(r['signature'],hmac.new(self.key,encoded(v),hashlib.sha256).hexdigest()):raise Unavailable('Invalid checkpoint signature')
   seen.add(r['node'])
  if len(seen)<2:raise Unavailable('Checkpoint lacks a majority')
 def blob(self,digest):
  if not re.fullmatch('[a-f0-9]{64}',digest):raise Unavailable('Invalid checkpoint identifier')
  p=self.root/(digest+'.gz');b=p.read_bytes()
  if len(b)>LIMIT or sha(b)!=digest:raise Unavailable('Checkpoint corruption')
  return b
 def stage(self,meta,blob,node):
  if len(blob)>LIMIT or sha(blob)!=meta['digest']:raise Unavailable('Checkpoint digest mismatch')
  with self.lock:
   if self.head and meta['revision']<self.head['revision']:raise Unavailable('Checkpoint superseded')
   # A cheap bounded envelope check; SQLite integrity is checked before promotion.
   snapshot_worker('validate',blob=blob)
   path=self.root/(meta['digest']+'.gz')
   if not path.exists():atomic(path,blob)
   return self.receipt(node,meta)
 def commit(self,meta):
  self.verify(meta)
  with self.lock:
   self.blob(meta['digest'])
   if self.head and meta['revision']<self.head['revision']:return
   if self.head and meta['revision']==self.head['revision'] and meta['digest']!=self.head['digest']:raise Unavailable('Conflicting checkpoint')
   atomic(self.root/'head.json',encoded(meta));self.head=meta
   keep={meta['digest']+'.gz'}
   for p in self.root.glob('*.gz'):
    if p.name not in keep and time.time()-p.stat().st_mtime>TTL*3:p.unlink()
def unpack(blob):
 with gzip.GzipFile(fileobj=io.BytesIO(blob)) as f:raw=read_bounded(f,DBLIMIT*2)
 v=json.loads(raw)
 if not {'database','configuration'}<=set(v) or set(v)-{'database','configuration','workflows'}:raise Unavailable('Unknown checkpoint members')
 db=base64.b64decode(v['database'],validate=True);conf=base64.b64decode(v['configuration'],validate=True)
 if len(db)>DBLIMIT or not db.startswith(b'SQLite format 3\x00') or len(conf)>1024*1024:raise Unavailable('Invalid checkpoint payload')
 workflows=base64.b64decode(v['workflows'],validate=True) if 'workflows' in v else None
 if workflows is not None:
  if len(workflows)>1024*1024 or not isinstance(json.loads(workflows),list):raise Unavailable('Invalid workflow payload')
 return db,conf,workflows
def snapshot_worker(action,*,data=None,run=None,blob=None):
 # JSON/base64/SQLite scratch allocations must not accumulate in the lease/UI
 # threads' allocator arenas. A short-lived interpreter releases all of them.
 cmd=[sys.executable,str(pathlib.Path(__file__).resolve()),'--snapshot-worker',action]
 if action=='create':cmd += ['--data',str(data),'--run',str(run)]
 result=subprocess.run(cmd,input=blob,stdout=subprocess.PIPE,stderr=subprocess.PIPE,check=True,timeout=15)
 if len(result.stdout)>LIMIT:raise Unavailable('Checkpoint exceeds byte limit')
 return result.stdout
POLICY={
 'SCAN_SUBNETS':['192.168.1.0/24 --interface=wlan0'], 'TIMEZONE':'Asia/Ho_Chi_Minh',
 'BACKEND_API_URL':'/network/server', 'LOADED_PLUGINS':['ARPSCAN','UI','CUSTPROP','DBCLNP','INTRNT','MAINT','NEWDEV','SETPWD','SYNC','VNDRPDT','WORKFLOWS'],
 'ARPSCAN_RUN':'schedule','ARPSCAN_ARGS':'sudo arp-scan --ignoredups --retry=3',
 'NSLOOKUP_RUN':'disabled','AVAHISCAN_RUN':'disabled','NBTSCAN_RUN':'disabled','DIGSCAN_RUN':'disabled',
 'INTRNT_RUN':'disabled','SYNC_RUN':'disabled','VNDRPDT_RUN':'disabled','WORKFLOWS_RUN':'disabled',
 'MAINT_RUN':'schedule','MAINT_RUN_SCHD':'17 3 * * *','MAINT_PERF_DAYS':0,'DBCLNP_RUN':'schedule','DBCLNP_RUN_SCHD':'37 3 * * *',
 'DAYS_TO_KEEP_EVENTS':14,'DEV_HIST_DAYS':7,'LOG_LEVEL':'minimal','SETPWD_enable_password':False,'DEEP_SLEEP':False,
 'REPORT_DASHBOARD_URL':'http://192.168.1.186/network'}
def scan_schedule(conf):
 # Parse one literal assignment only; never execute the native Python config.
 matches=re.findall(r'^ARPSCAN_RUN_SCHD[ \t]*=(.*)$',conf.decode(),flags=re.M)
 if not matches:return '',0
 try:value=ast.literal_eval(matches[-1].strip())
 except (ValueError,SyntaxError):return '',0
 if not isinstance(value,str) or len(value)>128 or not value.isascii() or any(ord(c)<32 for c in value):return '',0
 fields=value.split();minutes=0
 if len(fields)==5 and fields[1:]==['*']*4:
  if fields[0]=='*':minutes=1
  elif re.fullmatch(r'\*/[1-9][0-9]?',fields[0]):
   step=int(fields[0][2:])
   if step<=60 and 60%step==0:minutes=step
 return value,minutes
def managed_settings_error(path,body,token):
 parsed=urllib.parse.urlsplit(path)
 if parsed.path!='/network/php/server/util.php':return None
 try:
  form=urllib.parse.parse_qs(body.decode(),keep_blank_values=True);query=urllib.parse.parse_qs(parsed.query)
  if (form.get('function') or query.get('function'))!=['savesettings']:return None
  settings=json.loads((form.get('settings') or query.get('settings') or [''])[0]);expected=dict(POLICY,API_TOKEN=token)
  if not isinstance(settings,list):raise ValueError()
  for row in settings:
   if not isinstance(row,list) or len(row)<4:raise ValueError()
   key,value=row[1],row[3]
   if key not in expected:continue
   target=expected[key]
   if isinstance(target,bool):
    if isinstance(value,bool):pass
    elif str(value).lower() in ('true','false','1','0'):value=str(value).lower() in ('true','1')
    else:raise ValueError()
   elif isinstance(target,int):
    if not re.fullmatch(r'-?[0-9]+',str(value)):raise ValueError()
    value=int(value)
   elif isinstance(target,list) and isinstance(value,str):
    try:value=json.loads(value)
    except (ValueError,TypeError):value=ast.literal_eval(value)
   # Native multi-selects may serialize plugin sets in display order.
   if key=='LOADED_PLUGINS' and isinstance(value,list):value,target=sorted(value),sorted(target)
   if value!=target:return 'Cluster-managed setting cannot be changed: '+key
 except (ValueError,TypeError,SyntaxError,IndexError,KeyError,UnicodeError):return 'Invalid settings payload'
 return None
def apply_policy(conf,token):
 text=conf.decode();items=dict(POLICY,API_TOKEN=token)
 if not re.search(r"^ARPSCAN_RUN_SCHD[ \t]*=",text,flags=re.M):text+="\nARPSCAN_RUN_SCHD='*/5 * * * *'\n"
 for key,value in items.items():
  text=re.sub(r'^'+re.escape(key)+r'\s*=.*\n?', '',text,flags=re.M)
  text+='\n'+key+'='+repr(value)+'\n'
 return text.encode()
def checkpoint(data,run):
 data=pathlib.Path(data);run=pathlib.Path(run);run.mkdir(parents=True,exist_ok=True)
 dbpath=run/('sqlite-'+secrets.token_hex(8)+'.db')
 try:
  conf=(data/'config/app.conf').read_bytes();workflow_path=data/'config/workflows.json'
  workflows=workflow_path.read_bytes() if workflow_path.exists() else None
  with closing(sqlite3.connect('file:'+str(data/'db/app.db')+'?mode=ro',uri=True,timeout=3)) as src,closing(sqlite3.connect(dbpath)) as dst:
   src.backup(dst,pages=256,sleep=.01)
   if dst.execute('PRAGMA quick_check').fetchone()[0]!='ok':raise Unavailable('Database integrity check failed')
  if conf!=(data/'config/app.conf').read_bytes() or workflows!=(workflow_path.read_bytes() if workflow_path.exists() else None):raise Unavailable('Configuration changed during snapshot')
  db=dbpath.read_bytes()
  if len(db)>DBLIMIT:raise Unavailable('Database exceeds checkpoint limit')
  payload={'database':base64.b64encode(db).decode(),'configuration':base64.b64encode(conf).decode()}
  if workflows is not None:payload['workflows']=base64.b64encode(workflows).decode()
  blob=gzip.compress(encoded(payload),compresslevel=1,mtime=0);unpack(blob);return blob
 finally:dbpath.unlink(missing_ok=True)
def restore(blob,data,run,token):
 db,conf,workflows=unpack(blob);data=pathlib.Path(data);check=pathlib.Path(run)/'restore-check.db';atomic(check,db)
 try:
  with closing(sqlite3.connect(check)) as c:
   if c.execute('PRAGMA quick_check').fetchone()[0]!='ok':raise Unavailable('Database integrity check failed')
 finally:check.unlink(missing_ok=True)
 for suffix in ('-wal','-shm'):(data/('db/app.db'+suffix)).unlink(missing_ok=True)
 atomic(data/'db/app.db',db);atomic(data/'config/app.conf',apply_policy(conf,token))
 workflow_path=data/'config/workflows.json'
 if workflows is None:workflow_path.unlink(missing_ok=True)
 else:atomic(workflow_path,workflows);os.chown(workflow_path,20211,20211)
 for p in (data,data/'db',data/'config',data/'db/app.db',data/'config/app.conf'):
  os.chown(p,20211,20211);os.chmod(p,0o700 if p.is_dir() else 0o600)
class Cluster:
 def __init__(self,c):
  self.c=c;self.key=base64.b64decode(c['key']);self.node=c['node'];self.members=c['members'];self.root=pathlib.Path('/var/lib/desk-network');self.run=pathlib.Path('/run/desk-network');self.run.mkdir(exist_ok=True)
  self.store=Store(self.root/'state',self.key,self.members);self.lease=Lease(self.root/'state/vote.json')
  self.term=None;self.until=0.;self.running=False;self.ready=False;self.starting=False;self.sync_state='synced';self.last_snapshot=0.;self.last_health=0.;self.last_local=0.;self.local={};self.sequence=0;self.pid=None;self.cooldown=0.;self.container_started=0.;self.unhealthy_since=0.
  self.oplock=threading.RLock();self.snaplock=threading.Lock();self.pool=concurrent.futures.ThreadPoolExecutor(max_workers=8);self.stop=threading.Event();self.nonces={};self.authlock=threading.Lock()
  context=ssl.create_default_context(cafile=c['certificate']);self.opener=urllib.request.build_opener(urllib.request.ProxyHandler({}),urllib.request.HTTPSHandler(context=context))
 def owned(self):return self.running and clock()<self.until
 def status(self):
  return {'node':self.node,'lease':self.lease.status(),'active':self.owned(),'ready':self.owned() and self.ready,'head':self.store.head,'eligible':self.eligible(),'score':self.score(),'sync':self.sync_state}
 def eligible(self):
  if clock()<self.cooldown or not self.local or clock()-self.last_local>20 or not self.local.get('online'):return False
  return not any(v is True for v in self.local.get('admission',{}).values())
 def score(self):
  n=self.local;score=n.get('cpu')
  if not isinstance(score,(int,float)):score=100
  return score+sum(cost for k,cost in {'xiaozhi':20,'dns':10,'vpn':5}.items() if n.get('vip',{}).get(k))+sum(30 for k in ('asr','llm','tts') if (n.get('activity',{}).get(k) or 0)>0)
 def refresh_local(self):
  try:
   with self.opener.open('http://'+self.members[self.node]+':8010/local',timeout=.6) as r:n=json.loads(r.read(32768))
   if n.get('id')==self.node:self.local=n;self.last_local=clock()
  except Exception:pass
 def call(self,node,action,value=None,timeout=1.2):
  if node==self.node:return self.peer(self.node,action,value or {})
  path='/peer/'+action;raw=encoded(value or {});stamp=str(int(time.time()));nonce=secrets.token_hex(16)
  signed=('POST\n'+path+'\n'+stamp+'\n'+nonce+'\n'+sha(raw)).encode()
  headers={'Content-Type':'application/json','X-Desk-Node':self.node,'X-Desk-Time':stamp,'X-Desk-Nonce':nonce,'X-Desk-Signature':hmac.new(self.key,signed,hashlib.sha256).hexdigest()}
  req=urllib.request.Request('https://'+self.members[node]+':20215'+path,data=raw,headers=headers)
  with self.opener.open(req,timeout=timeout) as r:
   b=read_bounded(r,LIMIT*2)
   return json.loads(b)
 def fanout(self,action,value=None,timeout=1.2):
  jobs={n:self.pool.submit(self.call,n,action,value,timeout) for n in self.members};out={}
  for n,f in jobs.items():
   try:out[n]=f.result(timeout=timeout+1)
   except Exception:pass
  return out
 def peer(self,node,action,v):
  if action=='status':return self.status()
  if action=='lease':
   if not self.eligible() and node==self.node:raise Unavailable('Local node unavailable')
   return self.lease.grant(v['term'],node)
  if action=='blob':return {'blob':base64.b64encode(self.store.blob(v['digest'])).decode()}
  if action=='stage':
   lease=self.lease.status()
   if lease['holder']!=node or lease['promised']!=v['meta']['revision'][:2]:raise Unavailable('Sender does not hold lease')
   return self.store.stage(v['meta'],base64.b64decode(v['blob'],validate=True),self.node)
  if action=='commit':self.store.commit(v['meta']);return {'ok':True}
  raise Unavailable('Unknown peer operation')
 def catchup(self,heads):
  candidates=[(n,h['head']) for n,h in heads.items() if h.get('head')]
  if not candidates:raise Unavailable('No committed checkpoint')
  n,meta=max(candidates,key=lambda x:x[1]['revision']);self.store.verify(meta)
  if not self.store.head or meta['revision']>self.store.head['revision']:
   blob=base64.b64decode(self.call(n,'blob',{'digest':meta['digest']},timeout=5)['blob'],validate=True)
   self.store.stage(meta,blob,self.node);self.store.commit(meta)
 def snapshot(self):
  with self.snaplock:
   if not self.owned():raise Unavailable('Collector does not hold lease')
   blob=snapshot_worker('create',data=self.root/'data',run=self.run)
   if self.store.head and sha(blob)==self.store.head['digest']:
    self.last_snapshot=clock();self.sync_state='synced';return
   self.sequence+=1;meta={'revision':self.term+[self.sequence],'digest':sha(blob),'at':time.time()}
   replies=self.fanout('stage',{'meta':meta,'blob':base64.b64encode(blob).decode()},timeout=5)
   if len(replies)<2: self.sync_state='pending';raise Unavailable('Checkpoint needs two replicas')
   meta['certificate']=list(replies.values());self.store.verify(meta)
   if not self.owned():raise Unavailable('Lease expired during checkpoint')
   replies=self.fanout('commit',{'meta':meta},timeout=3)
   if len(replies)<2:self.sync_state='pending';raise Unavailable('Checkpoint acknowledgment pending')
   self.last_snapshot=clock();self.sync_state='synced'
 def command(self,*args,timeout=12):
  return subprocess.run(args,check=True,capture_output=True,timeout=timeout)
 def start_container(self):
  with self.oplock:
   if self.running or clock()>=self.until:return
   restore(self.store.blob(self.store.head['digest']),self.root/'data',self.run,self.c['api_token'])
   cmd=['podman','run','-d','--replace','--name',NAME,'--network=host','--read-only','--cpus=0.5','--cpu-shares=128','--memory=512m','--pids-limit=256','--log-driver=k8s-file','--log-opt=max-size=5mb','--cap-drop=ALL','--tmpfs','/tmp:rw,noexec,nosuid,nodev,mode=1700,size=256m','-v',str(self.root/'data')+':/data:rw','-v','/usr/local/libexec/desk-network/api-loopback.py:/app/server/api_server/api_server_start.py:ro','-v','/etc/desk-network/policy.json:/data/config/app_conf_override.json:ro','-v','/usr/local/libexec/desk-network/override-policy.sh:/entrypoint.d/35-apply-conf-override.sh:ro','-v','/usr/local/libexec/desk-network/lang-cached.php:/app/front/php/templates/language/lang.php:ro','-v','/usr/local/libexec/desk-network/opcache.so:/usr/lib/php83/modules/opcache.so:ro','-v','/usr/local/libexec/desk-network/opcache.ini:/etc/php83/conf.d/10_desk_opcache.ini:ro','-e','LISTEN_ADDR='+self.members[self.node],'-e','PORT=20211','-e','GRAPHQL_PORT=20212']
   for cap in ('NET_ADMIN','NET_RAW','NET_BIND_SERVICE','CHOWN','SETUID','SETGID'):cmd+=['--cap-add='+cap]
   cmd.append(IMAGE);self.running=True;self.ready=False;self.container_started=clock()
   try:
    self.command(*cmd,timeout=5)
    info=json.loads(self.command('podman','inspect',NAME,timeout=1).stdout)[0];pid=info['State']['Pid']
    self.pid=(pid,pathlib.Path(f'/proc/{pid}/stat').read_text().split(') ',1)[1].split()[19])
   except Exception:
    self.kill_container();raise
   if clock()>=self.until:self.kill_container();raise Unavailable('Lease expired while starting')
   self.last_snapshot=clock()
   LOG.info('LAN collector started on %s',self.node)
 def kill_container(self):
  self.until=0.;self.ready=False
  # Kill the namespace init immediately, before potentially slow Podman cleanup.
  # Linux kills every process in a PID namespace when its init exits.
  try:
   record=self.pid
   if not record and self.running:
    info=json.loads(self.command('podman','inspect',NAME,timeout=1).stdout)[0];pid=info['State']['Pid']
    if pid:record=(pid,pathlib.Path(f'/proc/{pid}/stat').read_text().split(') ',1)[1].split()[19])
   if record:
    pid,start=record
    if pathlib.Path(f'/proc/{pid}/stat').read_text().split(') ',1)[1].split()[19]==start:os.kill(pid,signal.SIGKILL)
  except Exception:pass
  try:self.command('podman','rm','-f',NAME,timeout=2)
  except Exception:pass
  self.running=False;self.pid=None
  (self.run/'public.json').unlink(missing_ok=True)
 def fence(self):
  while not self.stop.wait(.5):
   if self.running and clock()>=self.until:self.kill_container();LOG.warning('Collector fenced after lease expiration')
 def native_health(self):
  try:
   req=urllib.request.Request('http://127.0.0.1:20212/auth',headers={'Authorization':'Bearer '+self.c['api_token']})
   with self.opener.open(req,timeout=2) as r: ok=r.status==200 and json.loads(r.read(32768)).get('success') is True
   if not ok or not self.pid:return False
   api=pathlib.Path(f'/proc/{self.pid[0]}/root/tmp/api')
   return all((api/name).stat().st_size>100 for name in ('table_settings.json','table_plugins_language_strings.json'))
  except Exception:return False
 def live_scan(self):
  if not self.owned():return '',0
  try:
   path=self.root/'data/config/app.conf';stat=path.stat();version=(stat.st_mtime_ns,stat.st_size)
   if getattr(self,'scan_version',None)!=version:
    self.scan_value=scan_schedule(path.read_bytes()) if stat.st_size<=1024*1024 else ('',0);self.scan_version=version
   return self.scan_value
  except OSError:return '',0
 def public(self):
  schedule,minutes=self.live_scan()
  return {'node':self.node,'state':'active' if self.owned() and self.ready else 'starting' if self.owned() else 'standby','protocol':'desk-network-v1','observed_at':time.time(),'voter_ready':clock()>=self.lease.quarantine,'revision':self.store.head['revision'] if self.store.head else None,'sync':self.sync_state,'subnet':'192.168.1.0/24','scan_minutes':minutes,'scan_schedule':schedule}
 def run_loop(self):
  self.kill_container();threading.Thread(target=self.fence,daemon=True).start()
  while not self.stop.is_set():
   try:
    self.refresh_local();heads=self.fanout('status')
    if self.owned():
     if any(v is True for v in self.local.get('admission',{}).values()):
      try:self.snapshot()
      except Exception:pass
      self.kill_container();self.term=None
     else:
      stamp=clock();votes=self.fanout('lease',{'term':self.term})
      if len(votes)>=2:self.until=stamp+TTL-MARGIN
      if clock()-self.last_health>3:
       healthy=self.native_health();self.last_health=clock()
       if healthy:self.unhealthy_since=0.
       elif not self.unhealthy_since:self.unhealthy_since=clock()
       self.ready=healthy
       if not healthy and clock()-self.container_started>120 and clock()-self.unhealthy_since>20:
        self.cooldown=clock()+90;self.kill_container();self.term=None;LOG.warning('Unhealthy collector withdrawn')
      if self.ready and clock()-self.last_snapshot>300:
       # Snapshot work never blocks lease renewal or its fencing watchdog.
       if not self.snaplock.locked():threading.Thread(target=self.try_snapshot,daemon=True).start()
    else:
     self.catchup(heads)
     active=any(h.get('active') or h['lease']['holder'] for h in heads.values())
     candidates=[(h.get('score',200),n) for n,h in heads.items() if h.get('eligible') and h['lease']['remaining']==0]
     if not active and len(heads)>=2 and candidates and min(candidates)[1]==self.node:
      counter=max(h['lease']['promised'][0] for h in heads.values())+1;term=[counter,self.node];stamp=clock()
      votes=self.fanout('lease',{'term':term})
      if len(votes)>=2:
       self.term=term;self.sequence=0;self.until=stamp+TTL-MARGIN;self.start_container()
    atomic_public(self.run/'public.json',self.public())
   except Exception as e:LOG.warning('Network reconciliation pending (%s)',type(e).__name__)
   self.stop.wait(2)
  self.kill_container()
 def try_snapshot(self):
  try:self.snapshot()
  except Exception: self.sync_state='pending';self.last_snapshot=clock()-270;LOG.warning('Checkpoint replication pending')
def atomic_public(p,v):
 b=encoded(v);tmp=pathlib.Path(str(p)+'.tmp');tmp.write_bytes(b);os.chmod(tmp,0o644);os.replace(tmp,p)
def read_request(handler,limit=LIMIT*2):
 if handler.headers.get('Transfer-Encoding'):raise Unavailable('Chunked request not supported')
 n=int(handler.headers.get('Content-Length','0'))
 if n<0 or n>limit:raise Unavailable('Request too large')
 return handler.rfile.read(n)
def write_response(h,status,b,ctype='application/json',headers=None):
 h.send_response(status);h.send_header('Content-Type',ctype);h.send_header('Content-Length',str(len(b)));h.send_header('Cache-Control','no-store')
 for k,v in (headers or {}).items():h.send_header(k,v)
 h.end_headers()
 if h.command!='HEAD':h.wfile.write(b)
class Peer(http.server.BaseHTTPRequestHandler):
 def log_message(self,*args):pass
 def do_POST(self):
  c=self.server.cluster
  try:
   raw=read_request(self);node=self.headers.get('X-Desk-Node');stamp=self.headers.get('X-Desk-Time','');nonce=self.headers.get('X-Desk-Nonce','')
   if node not in c.members or self.client_address[0]!=c.members[node] or abs(time.time()-int(stamp))>20 or not re.fullmatch('[a-f0-9]{32}',nonce):raise Unavailable('Invalid peer identity')
   message=('POST\n'+self.path+'\n'+stamp+'\n'+nonce+'\n'+sha(raw)).encode()
   if not hmac.compare_digest(self.headers.get('X-Desk-Signature',''),hmac.new(c.key,message,hashlib.sha256).hexdigest()):raise Unavailable('Invalid peer signature')
   with c.authlock:
    c.nonces={n:t for n,t in c.nonces.items() if clock()-t<30}
    if nonce in c.nonces or len(c.nonces)>512:raise Unavailable('Peer replay rejected')
    c.nonces[nonce]=clock()
   value=c.peer(node,self.path.removeprefix('/peer/'),json.loads(raw));write_response(self,200,encoded(value))
  except Exception:write_response(self,409,encoded({'error':'Peer request rejected'}))
def is_mutation(method,path,body):
 if method in ('GET','HEAD'):
  query=urllib.parse.parse_qs(urllib.parse.urlsplit(path).query)
  return bool(set(query)&{'action','function','sql','query','delete','update'})
 if method=='OPTIONS':return False
 if urllib.parse.urlsplit(path).path in ('/network/server/devices/by-status','/network/server/devices/search'):return False
 if '/graphql' in path or 'query_graphql.php' in path:
  try:q=json.loads(body).get('query','');return bool(re.search(r'\bmutation\b',q))
  except Exception:return True
 return True
def rewrite_text(raw,ctype,node='',path=''):
 if not any(t in ctype for t in ('text/html','javascript','text/css')):return raw
 text=raw.decode('utf-8',errors='replace')
 # The pinned upstream Treeviz bundle appends a demo that tries to draw into
 # nonexistent #tree/#tree-horizontal elements. Keep the library/export intact.
 if urllib.parse.urlsplit(path).path=='/network/lib/treeviz/treeviz.iife.js' and sha(raw)=='577d2115d592e0eaa8bfaa1080e7960d5719dc0028fa371fc866562b5a616818':
  text=text.split('var ft=[{id:1,text_1:"Chaos"',1)[0]+'})();\n'
 if 'javascript' in ctype or 'text/html' in ctype:
  text=re.sub(r'\b(?:window\.)?localStorage\b','deskNetworkLocalStorage',text)
  text=re.sub(r'\b(?:window\.)?sessionStorage\b','deskNetworkSessionStorage',text)
 text=re.sub(r'([\"\x27])/(?!/|network(?:/|[\"\x27]))(?=(?:php|server|deviceDetails|llms|js|css|img|images|lib|vendor|api|index|devices|settings|presence|events|plugins)(?:[/.]|["\x27]))',r'\1/network/',text)
 if 'text/html' in ctype and '<body' in text:
  page=urllib.parse.urlsplit(path).path
  if page in ('/network/presence.php','/network/events.php'):
   # These native pages call main eagerly, before API credentials/settings finish
   # bootstrapping. Reuse the existing lifecycle gate used by Device details.
   start='callAfterAppInitialized(main);'
   if page=='/network/events.php':start='callAfterAppInitialized(()=>{tableRows=parseInt(getCookie(parTableRows)||getSetting("UI_DEFAULT_PAGE_SIZE"),10);main();});'
   text=re.sub(r'(?m)^[ \t]*main\(\);',lambda m:start,text,count=1)
  # Treeview parents expand folders; child links own page navigation.
  text=re.sub(r'<a href="(?:devices|settings)\.php"\s+onclick="openUrl\([^"\n]*\)"\s*>','<a href="#" role="button">',text)
  # Sidebar links also appear on pages without the destination's tab helper.
  # Guard only inline handlers; do not alter the actual function declaration.
  text=re.sub(r'onclick="([^"]*\binitializeTabs\(\)[^"]*)"',lambda m:'onclick="'+m[1].replace('initializeTabs()',"(typeof initializeTabs==='function'&&initializeTabs())")+'"',text)
  storage="""<script>(()=>{function isolated(storage){const prefix='desk_network:';const keys=()=>Object.keys(storage).filter(k=>k.startsWith(prefix));return {getItem:k=>storage.getItem(prefix+k),setItem:(k,v)=>storage.setItem(prefix+k,v),removeItem:k=>storage.removeItem(prefix+k),clear:()=>keys().forEach(k=>storage.removeItem(k)),key:n=>keys()[n]?.slice(prefix.length)??null,get length(){return keys().length}};}window.deskNetworkLocalStorage=isolated(window.localStorage);window.deskNetworkSessionStorage=isolated(window.sessionStorage);})();</script>"""
  layout="""<style id="desk-network-layout">#desk-network-tools .desk-network-meta{margin:0 10px 8px;display:flex;align-items:center;gap:6px;font-size:11px}#desk-network-status{flex:1;min-width:0;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;opacity:.8}#desk-network-scan{flex-shrink:0}.sidebar-collapse #desk-network-tools .desk-network-meta{display:none}</style>"""
  text=text.replace('<head>','<head>'+storage+layout,1)
  text=text.replace('src="lib/treeviz/treeviz.iife.js"','src="lib/treeviz/treeviz.iife.js?desk=library-only-v1"')
  text=text.replace('js/api.js?v=','js/api.js?desk=base-v1&v=')
  banner='<li id="desk-network-tools"><a href="/"><i class="fa fa-fw fa-home"></i><span>Desk Cluster</span></a><div class="desk-network-meta"><span id="desk-network-status">HA</span><button type="button" id="desk-network-scan" class="btn btn-default btn-xs">Scan now</button></div></li>'
  text,placed=re.subn(r'(<ul\b[^>]*class="[^"]*\bsidebar-menu\b[^"]*"[^>]*>)',lambda m:m[1]+banner,text,count=1)
  if not placed:text=re.sub(r'(<body[^>]*>)',lambda m:m[1]+'<ul>'+banner+'</ul>',text,count=1)
  if urllib.parse.urlsplit(path).path=='/network/settings.php':
   notice='<aside id="desk-settings-policy" style="padding:10px 15px;background:#3c8dbc18;border-bottom:1px solid #3c8dbc40;font-size:13px">Cluster-managed controls are marked and locked. Other settings, including the scan schedule, can be saved here.</aside><script defer src="/network/desk/settings-policy.js"></script>'
   text,inserted=re.subn(r'(<div\b[^>]*\bclass="[^"]*\bcontent-wrapper\b[^"]*"[^>]*>)',lambda m:m[1]+notice,text,count=1)
   if not inserted:text=text.replace(banner,banner+notice,1)
  script='''<script>(()=>{const initialOwner=OWNER_JSON;let wasUnavailable=false;const out=document.getElementById('desk-network-status'),b=document.getElementById('desk-network-scan');async function status(){try{const r=await fetch('/network/desk/status');if(!r.ok)throw Error('Recovering');const s=await r.json();if(s.node!==initialOwner||wasUnavailable){location.reload();return;}out.textContent=s.sync==='synced'?'Synced':'Pending';out.title=`Network · ${s.node} · LAN only · ${s.sync==='synced'?'Synced':'Sync pending'}`;}catch{wasUnavailable=true;out.textContent='Reconnecting';out.title='Network · reconnecting';}}b?.addEventListener('click',async()=>{b.disabled=true;try{const r=await fetch('/network/desk/scan',{method:'POST',headers:{'X-Desk-Action':'scan'}});b.textContent=r.ok?'Queued':'Try again';}catch{b.textContent='Try again';}setTimeout(()=>{b.disabled=false;b.textContent='Scan now';},5000);});status();setInterval(status,10000);})();</script>'''
  script=script.replace('OWNER_JSON',json.dumps(node))
  text=text.replace('</body>',script+'</body>')
 return text.encode()
class UI(http.server.BaseHTTPRequestHandler):
 protocol_version='HTTP/1.1'
 def log_message(self,*args):pass
 def handle_ui(self):
  c=self.server.cluster;conn=None
  try:
   if self.client_address[0] not in set(c.members.values())|{'127.0.0.1'}:return write_response(self,403,b'Forbidden','text/plain')
   if self.path=='/health':return write_response(self,200 if c.owned() and c.ready else 503,encoded(c.public()))
   if self.path=='/network/desk/status':return write_response(self,200,encoded(c.public()))
   if self.path=='/network/desk/settings-policy.js' and self.command in ('GET','HEAD'):
    keys=encoded(list(POLICY)+['API_TOKEN']);script=pathlib.Path('/usr/local/libexec/desk-network/settings-policy.js').read_bytes()
    return write_response(self,200,b'const deskManagedKeys='+keys+b';\n'+script,'application/javascript')
   if not self.path.startswith('/network/'):return write_response(self,404,b'Not found','text/plain')
   raw=read_request(self,2*1024*1024);mutation=is_mutation(self.command,self.path,raw)
   if mutation:
    error=managed_settings_error(self.path,raw,c.c['api_token'])
    if error:return write_response(self,400,encoded({'error':error}))
    origin=self.headers.get('Origin')
    if origin and origin!='http://192.168.1.186':return write_response(self,403,b'Cross-origin mutation rejected','text/plain')
    if self.headers.get('Sec-Fetch-Site')=='cross-site':return write_response(self,403,b'Cross-origin mutation rejected','text/plain')
   if not c.owned() or not c.ready:return write_response(self,503,b'Network collector is recovering','text/plain',{'Retry-After':'3'})
   if mutation:
    stamp=clock();term=list(c.term);votes=c.fanout('lease',{'term':term})
    if len(votes)<2 or not c.owned() or c.term!=term:return write_response(self,503,b'Network edit requires two available replicas','text/plain',{'Retry-After':'3'})
    c.until=max(c.until,stamp+TTL-MARGIN)
   if self.path=='/network/desk/scan':
    if self.command!='POST' or self.headers.get('X-Desk-Action')!='scan':return write_response(self,405,b'POST scan required','text/plain')
    req=urllib.request.Request('http://127.0.0.1:20212/plugin/ARPSCAN/run',data=b'{}',headers={'Authorization':'Bearer '+c.c['api_token'],'Content-Type':'application/json'})
    with c.opener.open(req,timeout=5) as r:return write_response(self,r.status,r.read(32768))
   path=self.path[len('/network'):];headers={k:v for k,v in self.headers.items() if k.lower() not in ('host','connection','transfer-encoding','content-length','accept-encoding')};headers['Host']='192.168.1.186';headers['Accept-Encoding']='identity'
   conn=http.client.HTTPConnection(c.members[c.node],20211,timeout=20);conn.request(self.command,path,body=raw or None,headers=headers);r=conn.getresponse();ctype=r.getheader('Content-Type','application/octet-stream')
   if 'text/event-stream' in ctype:
    self.send_response(r.status);self.send_header('Content-Type',ctype);self.send_header('Cache-Control','no-store');self.send_header('Connection','close');self.end_headers();self.close_connection=True
    while c.owned():
     line=r.readline()
     if not line:break
     self.wfile.write(line);self.wfile.flush()
    return
   b=read_bounded(r,LIMIT)
   if mutation and r.status<400:
    try:c.snapshot()
    except Exception:c.sync_state='pending';LOG.warning('Native edit has pending replication')
   b=rewrite_text(b,ctype,c.node,self.path);extra={}
   for k,v in r.getheaders():
    if k.lower()=='set-cookie':extra[k]=re.sub(r'(?i)path=/($|;)',r'Path=/network/\1',v)
    if k.lower()=='location':extra[k]='/network'+v if v.startswith('/') and not v.startswith('/network') else v
   extra['X-Desk-Network-Node']=c.node;extra['X-Desk-Network-Sync']=c.sync_state
   write_response(self,r.status,b,ctype,extra)
  except (BrokenPipeError,ConnectionResetError):pass
  except Exception:write_response(self,503,b'Network request temporarily unavailable','text/plain',{'Retry-After':'3'})
  finally:
   if conn:conn.close()
 do_GET=do_HEAD=do_POST=do_PUT=do_PATCH=do_DELETE=do_OPTIONS=handle_ui
class Server(http.server.ThreadingHTTPServer):
 daemon_threads=True
 request_queue_size=32
 def __init__(self,*args,**kwargs):
  self.slots=threading.BoundedSemaphore(32);super().__init__(*args,**kwargs)
 def process_request(self,request,client_address):
  if not self.slots.acquire(blocking=False):self.shutdown_request(request);return
  try:super().process_request(request,client_address)
  except Exception:self.slots.release();raise
 def process_request_thread(self,request,client_address):
  try:
   # Perform TLS in an admitted worker, never in the accept loop used by lease RPCs.
   context=getattr(self,'tls_context',None)
   if context:
    request.settimeout(2);request=context.wrap_socket(request,server_side=True);request.settimeout(25)
   super().process_request_thread(request,client_address)
  except OSError:self.shutdown_request(request)
  finally:self.slots.release()
 def get_request(self):
  s,a=super().get_request();s.settimeout(25);return s,a
 def verify_request(self,r,a):return a[0] in set(self.cluster.members.values())|{'127.0.0.1'}
def main():
 parser=argparse.ArgumentParser();parser.add_argument('--snapshot-worker',choices=('create','validate'));parser.add_argument('--data');parser.add_argument('--run');parser.add_argument('--config',default='/etc/desk-network/config.json');parser.add_argument('--stop',action='store_true');args=parser.parse_args()
 if args.snapshot_worker:
  if args.snapshot_worker=='create':
   if not args.data or not args.run:parser.error('Checkpoint paths required')
   blob=checkpoint(args.data,args.run)
   if len(blob)>LIMIT:raise Unavailable('Checkpoint exceeds byte limit')
   sys.stdout.buffer.write(blob)
  else:unpack(read_bounded(sys.stdin.buffer,LIMIT))
  return
 if args.stop:
  try:
   info=json.loads(subprocess.check_output(['podman','inspect',NAME],stderr=subprocess.DEVNULL,timeout=1))[0];pid=info['State']['Pid']
   if pid:os.kill(pid,signal.SIGKILL)
  except Exception:pass
  try:subprocess.run(['podman','rm','-f',NAME],stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL,timeout=2)
  except Exception:pass
  return
 c=Cluster(json.loads(pathlib.Path(args.config).read_text()));logging.basicConfig(level=logging.INFO,format='%(message)s')
 peer=Server((c.members[c.node],20215),Peer);peer.cluster=c;ctx=ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER);ctx.load_cert_chain(c.c['certificate'],c.c['tls_key']);peer.tls_context=ctx
 ui=Server((c.members[c.node],20213),UI);ui.cluster=c
 for server in (peer,ui):threading.Thread(target=server.serve_forever,daemon=True).start()
 signal.signal(signal.SIGTERM,lambda *_:c.stop.set())
 try:c.run_loop()
 finally:c.stop.set();c.kill_container();peer.shutdown();ui.shutdown()
if __name__=='__main__':main()
