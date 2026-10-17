#!/usr/bin/env python3
"""One LAN collector, majority leases, certified consistent SQLite checkpoints."""
from contextlib import closing
import discovery,bootstrap,checkpoint_budget
from checkpoint_source import CheckpointSource
from peer_http import PeerHTTP
import ast, signal, argparse, base64, concurrent.futures, gzip, hashlib, hmac, http.client, http.server, io, json, logging, os, pathlib, re, secrets, shutil, socket, sqlite3, ssl, struct, subprocess, sys, threading, time, urllib.parse, urllib.request, zlib
LOG = logging.getLogger('desk-network')
TTL, MARGIN, LIMIT, DBLIMIT = 15., 5., 16*1024*1024, 64*1024*1024
OVERLOAD_PENALTY=100
def ha_held(n):return (n.get('homeassistant') or {}).get('state') in ('starting','active','checkpointing')
def other_owners(n):return sum(1 for k in ('xiaozhi','dns','vpn') if (n.get('vip') or {}).get(k))+(1 if ha_held(n) else 0)
def clock():return time.clock_gettime(time.CLOCK_BOOTTIME) if hasattr(time,'CLOCK_BOOTTIME') else time.monotonic()
NAME = 'desk-netalertx'
IMAGE = 'ghcr.io/netalertx/netalertx@sha256:f95bca7a794141296dd1d16be5bb7f39f6fdbcd20353cdb51eee696e12d317f6'
def encoded(v): return json.dumps(v,sort_keys=True,separators=(',',':')).encode()
def sha(b): return hashlib.sha256(b).hexdigest()
def resuming_https_handler(context):
 # urllib's HTTPSHandler with the same pinned-CA handshake and hostname check, plus TLS session resumption:
 # the abbreviated handshake skips the server's RSA signature on every peer RPC. Each (host,port) keeps the
 # newest session; a rejected or expired session silently falls back to a full, fully verified handshake.
 # Requests keep their own TCP connection (Connection: close) and every body is still HMAC-signed.
 sessions={}
 class Connection(http.client.HTTPSConnection):
  def connect(self):
   http.client.HTTPConnection.connect(self)
   self.sock=self._context.wrap_socket(self.sock,server_hostname=self._tunnel_host or self.host,session=sessions.get((self.host,self.port)))
  def close(self):
   # http.client closes the connection inside getresponse() for Connection: close, after the
   # server's post-handshake tickets have been read and before self.sock is released.
   try:
    session=self.sock.session if self.sock is not None else None
    if session is not None and session.has_ticket:sessions[(self.host,self.port)]=session
   except Exception:pass
   super().close()
 class Handler(urllib.request.HTTPSHandler):
  def https_open(self,req):return self.do_open(Connection,req,context=self._context)
 return Handler(context=context)
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
def atomic(p,b,mode=0o600):
 p=pathlib.Path(p);p.parent.mkdir(parents=True,exist_ok=True);tmp=p.with_name(p.name+'.tmp-'+secrets.token_hex(4))
 fd=os.open(tmp,os.O_WRONLY|os.O_CREAT|os.O_EXCL,0o600)
 try:
  os.fchmod(fd,mode)
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
 v=json.loads(raw);del raw # raw JSON and the base64 text are dropped as soon as decoded; only validation order is unchanged
 if not {'database','configuration'}<=set(v) or set(v)-{'database','configuration','workflows'}:raise Unavailable('Unknown checkpoint members')
 db=base64.b64decode(v.pop('database'),validate=True);conf=base64.b64decode(v['configuration'],validate=True)
 if len(db)>DBLIMIT or not db.startswith(b'SQLite format 3\x00') or len(conf)>1024*1024:raise Unavailable('Invalid checkpoint payload')
 workflows=base64.b64decode(v['workflows'],validate=True) if 'workflows' in v else None
 if workflows is not None:
  if len(workflows)>1024*1024 or not isinstance(json.loads(workflows),list):raise Unavailable('Invalid workflow payload')
 return db,conf,workflows
def snapshot_worker(action,*,data=None,run=None,blob=None):
 # JSON/base64/SQLite scratch allocations must not accumulate in the lease/UI
 # threads' allocator arenas. A short-lived interpreter releases all of them.
 cmd=[sys.executable,str(pathlib.Path(__file__).resolve()),'--snapshot-worker',action]
 worker_dir=None
 if action=='create':
  # Give each worker its own isolated scratch directory.
  # subprocess.run() (inside execute()) is synchronous: it only returns after
  # the worker process has fully exited — whether by normal exit, RuntimeMaxSec
  # timeout, or OOM-SIGKILL.  The rmtree in finally therefore always runs after
  # the worker is confirmed dead, with no surviving handles to any file.
  # No age check or mtime heuristic is needed.
  worker_dir=pathlib.Path(run)/('ckpt-'+secrets.token_hex(8))
  worker_dir.mkdir(mode=0o700,parents=True,exist_ok=False)
  cmd += ['--data',str(data),'--run',str(worker_dir)]
 try:
  result=checkpoint_budget.execute(cmd,blob)
  if len(result.stdout)>LIMIT:raise Unavailable('Checkpoint exceeds byte limit')
  return result.stdout
 finally:
  if worker_dir is not None:
   shutil.rmtree(worker_dir,ignore_errors=True)
POLICY={
 'SCAN_SUBNETS':['192.168.1.0/24 --interface=wlan0'], 'TIMEZONE':'Asia/Ho_Chi_Minh',
 'BACKEND_API_URL':'/network/server', 'LOADED_PLUGINS':['ARPSCAN','AVAHISCAN','UI','CUSTPROP','DBCLNP','INTRNT','MAINT','NEWDEV','SETPWD','SYNC','VNDRPDT','WORKFLOWS'],
 'ARPSCAN_CMD':'python3 /app/desk_network/arp.py userSubnets={subnets}','ARPSCAN_RUN_TIMEOUT':60,'ARPSCAN_RUN':'schedule','ARPSCAN_ARGS':'sudo arp-scan --ignoredups --retry=3',
 'NSLOOKUP_RUN':'disabled','AVAHISCAN_RUN':'schedule','AVAHISCAN_RUN_SCHD':'*/5 * * * *','AVAHISCAN_CMD':'python3 /app/desk_network/mdns.py','AVAHISCAN_RUN_TIMEOUT':30,'AVAHISCAN_SET_ALWAYS':[],'AVAHISCAN_SET_EMPTY':['devName','devFQDN'],'AVAHISCAN_WATCH':['watchedValue2'],'AVAHISCAN_REPORT_ON':[],'NBTSCAN_RUN':'disabled','DIGSCAN_RUN':'disabled',
 'INTRNT_RUN':'disabled','SYNC_RUN':'disabled','VNDRPDT_RUN':'disabled','WORKFLOWS_RUN':'disabled',
 'MAINT_RUN':'schedule','MAINT_RUN_SCHD':'17 3 * * *','MAINT_PERF_DAYS':0,'DBCLNP_RUN':'schedule','DBCLNP_RUN_SCHD':'37 3 * * *',
 'DAYS_TO_KEEP_EVENTS':14,'DEV_HIST_DAYS':7,'LOG_LEVEL':'minimal','SETPWD_enable_password':False,'DEEP_SLEEP':False,
 'NEWDEV_IP_MATCH_NAME':False, 'REPORT_DASHBOARD_URL':'http://192.168.1.186/network'}
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
def checkpoint_blob(dbpath,conf,workflows):
 # Byte-identical to gzip.compress(encoded({'configuration','database'[,'workflows']}),compresslevel=1,mtime=0)
 # on the running interpreter, but the database is base64-encoded and deflated in chunks instead of held as raw,
 # base64 and JSON copies. The gzip header is taken from gzip.compress itself (its OS byte differs between Python
 # versions), the deflate stream and trailer are format-defined. 3-byte chunks keep chunked base64 equal to one-shot
 # base64; sorted keys keep encoded()'s member order.
 out=io.BytesIO();out.write(gzip.compress(b'',compresslevel=1,mtime=0)[:10])
 deflate=zlib.compressobj(1,zlib.DEFLATED,-zlib.MAX_WBITS);crc=0;size=0
 def put(part):
  nonlocal crc,size
  crc=zlib.crc32(part,crc);size+=len(part);out.write(deflate.compress(part))
 put(b'{"configuration":"'+base64.b64encode(conf)+b'","database":"')
 with open(dbpath,'rb') as f:
  while True:
   part=f.read(3*65536)
   if not part:break
   put(base64.b64encode(part))
 put(b'"')
 if workflows is not None:put(b',"workflows":"'+base64.b64encode(workflows)+b'"')
 put(b'}')
 out.write(deflate.flush());out.write(struct.pack('<LL',crc,size&0xffffffff))
 return out.getvalue()
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
  if dbpath.stat().st_size>DBLIMIT:raise Unavailable('Database exceeds checkpoint limit')
  blob=checkpoint_blob(dbpath,conf,workflows);unpack(blob);return blob
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
  self.term=None;self.until=0.;self.running=False;self.ready=False;self.starting=False;self.sync_state='synced';self.last_snapshot=0.;self.last_health=0.;self.last_local=0.;self.local={};self.sequence=0;self._snap_backoff=0.;self._rebalance_retry=0.;self._rebalance=None;self.pid=None;self.cooldown=0.;self.container_started=0.;self.boot_ready_seconds=None;self.unhealthy_since=0.
  self.warm=self.c.get("warm_standby") is True;self.generation=None;self.promotion_started=0.;self.promotion_seconds=None;self.warm_path=self.run/"warm"
  self.budget=bootstrap.Budget(clock);self.oplock=threading.RLock();self.snaplock=threading.Lock();self.pool=concurrent.futures.ThreadPoolExecutor(max_workers=8);self.stop=threading.Event();self.nonces={};self.authlock=threading.Lock();self.scan_lock=threading.Lock();self.scan_queued_at=0.;self.scan_cache={};self.scan_cache_at=0.
  context=ssl.create_default_context(cafile=c['certificate']);self.opener=urllib.request.build_opener(urllib.request.ProxyHandler({}),resuming_https_handler(context))
  self.peer_http=PeerHTTP(context,LIMIT*2)
  self.checkpoint_pool=concurrent.futures.ThreadPoolExecutor(max_workers=3)
 def owned(self):return self.running and clock()<self.until
 def status(self):
  return {'node':self.node,'lease':self.lease.status(),'active':self.owned(),'ready':self.owned() and self.ready,'head':self.store.head,'eligible':self.eligible(),'score':self.score(),'sync':self.sync_state}
 def eligible(self):
  if self.warm and ((self.run/'warm-drain').exists() or (not self.owned() and self.warm_state().get('state')!='inactive')):return False
  if clock()<self.cooldown or not self.local or clock()-self.last_local>20 or not self.local.get('online'):return False
  return not any(v is True for v in self.local.get('admission',{}).values())
 def score(self):
  n=self.local;score=n.get('cpu')
  if not isinstance(score,(int,float)):score=100
  if self.warm and self.warm_state().get('state')=='inactive':score-=100
  vip_cost=sum(cost for k,cost in {'xiaozhi':20,'dns':10,'vpn':5}.items() if n.get('vip',{}).get(k))
  activity_cost=sum(30 for k in ('asr','llm','tts') if (n.get('activity',{}).get(k) or 0)>0)
  ha_cost=20 if ha_held(n) else 0
  overload_penalty=OVERLOAD_PENALTY if other_owners(n)>=2 else 0
  return score+vip_cost+activity_cost+ha_cost+overload_penalty
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
  channel='checkpoint' if action in ('stage','commit','blob') else 'control'
  status,b=self.peer_http.post(self.members[node],20215,path,raw,headers,timeout,channel)
  if status!=200:raise Unavailable('Peer request rejected')
  return json.loads(b)
 def fanout(self,action,value=None,timeout=1.2):
  pool=self.checkpoint_pool if action in ('stage','commit','blob') else self.pool
  jobs={n:pool.submit(self.call,n,action,value,timeout) for n in self.members};out={}
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
 def snapshot(self,expected_term=None,force=True):
  with self.snaplock:
   # Only the cheap change probe shares the ownership lock. Worker execution
   # and replication remain outside it so renewal is never queued behind them.
   with self.oplock:
    if not self.owned() or (expected_term is not None and self.term!=expected_term):raise Unavailable('Collector does not hold expected lease')
    term=list(self.term)
    source=getattr(self,'_checkpoint_source',None)
    if source is None:self._checkpoint_source=source=CheckpointSource(self.root/'data')
    try:before=source.stamp()
    except Exception:self.retire_checkpoint_source(source);source=None;before=None
    if not self.owned() or self.term!=term:
     self.retire_checkpoint_source(source);raise Unavailable('Lease changed during checkpoint probe')
    if not force and source and self.store.head and source.unchanged(term,before,self.store.head['digest']):
     self.last_snapshot=clock();self.sync_state='synced';return
   blob=snapshot_worker('create',data=self.root/'data',run=self.run)
   if not self.owned() or self.term!=term:raise Unavailable('Lease changed during checkpoint')
   if not self.store.head or sha(blob)!=self.store.head['digest']:
    self.sequence+=1;meta={'revision':term+[self.sequence],'digest':sha(blob),'at':time.time()}
    replies=self.fanout('stage',{'meta':meta,'blob':base64.b64encode(blob).decode()},timeout=checkpoint_budget.STAGE_TIMEOUT)
    if len(replies)<2: self.sync_state='pending';raise Unavailable('Checkpoint needs two replicas')
    meta['certificate']=list(replies.values());self.store.verify(meta)
    if not self.owned() or self.term!=term:raise Unavailable('Lease changed during checkpoint')
    replies=self.fanout('commit',{'meta':meta},timeout=3)
    if len(replies)<2:self.sync_state='pending';raise Unavailable('Checkpoint acknowledgment pending')
   with self.oplock:
    if not self.owned() or self.term!=term:raise Unavailable('Lease changed during checkpoint commit')
    if source:
     try:source.accept(term,before,self.store.head['digest'])
     except Exception:self.retire_checkpoint_source(source)
    self.last_snapshot=clock();self.sync_state='synced'
 def retire_checkpoint_source(self,source=None):
  if source is None:source=getattr(self,'_checkpoint_source',None);self._checkpoint_source=None
  elif getattr(self,'_checkpoint_source',None) is source:self._checkpoint_source=None
  if source is not None:source.close()
 def command(self,*args,timeout=12):
  return subprocess.run(args,check=True,capture_output=True,timeout=timeout)
 def warm_control(self,active=False):
  self.warm_path.mkdir(mode=0o755,parents=True,exist_ok=True);os.chmod(self.warm_path,0o755)
  atomic(self.warm_path/'control.json',encoded({'generation':self.generation or '0'*32,'until':self.until if active else 0}),mode=0o644)
 def renew(self,stamp,term):
  with self.oplock:
   if not self.owned() or self.term!=term:return False
   self.until=max(self.until,stamp+TTL-MARGIN)
   if self.warm:self.warm_control(True)
   return True
 def warm_state(self):
  if not self.warm or not self.pid:return {}
  try:
   pid,start=self.pid;process=pathlib.Path(f'/proc/{pid}')
   if process.joinpath('stat').read_text().split(') ',1)[1].split()[19]!=start:return {}
   path=process/'root/tmp/desk-warm-status.json'
   if path.stat().st_size>2048:return {}
   value=json.loads(path.read_text())
   return value if isinstance(value,dict) else {}
  except (OSError,ValueError,IndexError):return {}
 def deactivate_warm(self):
  self.until=0.;self.ready=False;self.warm_control()
  deadline=clock()+1.5
  while clock()<deadline:
   value=self.warm_state()
   if value.get('state')=='inactive' and value.get('groups')==[]:
    with self.scan_lock:self.scan_queued_at=0.;self.scan_cache_at=0.
    self.generation=None
    return True
   time.sleep(.05)
  return False
 def start_container(self,preload=False):
  with self.oplock:
   if not preload and clock()>=self.until:return
   if self.running:
    if self.warm and not preload:
     self.activate_warm()
    return
   if self.warm:self.warm_control()
   self.retire_checkpoint_source()
   restore(self.store.blob(self.store.head['digest']),self.root/'data',self.run,self.c['api_token'])
   cmd=['podman','run','-d','--replace','--name',NAME,'--network=host','--read-only','--cpus=0.5','--cpu-shares=128','--memory=512m','--pids-limit=256','--log-driver=k8s-file','--log-opt=max-size=5mb','--cap-drop=ALL','--tmpfs','/tmp:rw,noexec,nosuid,nodev,mode=1700,size=256m','-v',str(self.root/'data')+':/data:rw','-v','/usr/local/libexec/desk-network/api-loopback.py:/app/server/api_server/api_server_start.py:ro','-v','/etc/desk-network/policy.json:/data/config/app_conf_override.json:ro','-v','/usr/local/libexec/desk-network/override-policy.sh:/entrypoint.d/35-apply-conf-override.sh:ro','-v','/usr/local/libexec/desk-network/lang-cached.php:/app/front/php/templates/language/lang.php:ro','-v','/usr/local/libexec/desk-network/opcache.so:/usr/lib/php83/modules/opcache.so:ro','-v','/usr/local/libexec/desk-network/opcache.ini:/etc/php83/conf.d/10_desk_opcache.ini:ro','-e','LISTEN_ADDR='+self.members[self.node],'-e','PORT=20211','-e','GRAPHQL_PORT=20212']
   cmd+=['-v','/usr/local/libexec/desk-network/mdns.py:/app/desk_network/mdns.py:ro','-v','/usr/local/libexec/desk-network/arp.py:/app/desk_network/arp.py:ro','-v','/usr/local/libexec/desk-network/discovery.py:/app/desk_network/discovery.py:ro']
   if self.c.get('scan_distributed'):
    cmd+=['-v','/run/desk-network-scan:/run/desk-network-scan:ro','-e','DESK_NETWORK_DISTRIBUTED=1']
   for cap in ('NET_ADMIN','NET_RAW','NET_BIND_SERVICE','CHOWN','SETUID','SETGID'):cmd+=['--cap-add='+cap]
   if self.warm:
    cmd+=['-v','/usr/local/libexec/desk-network/api_read_cache.py:/app/desk_network/api_read_cache.py:ro','-v',str(self.warm_path)+':/run/desk-network-warm:ro','-v','/usr/local/libexec/desk-network/warm_runtime.py:/app/desk_network/warm_runtime.py:ro','-v','/usr/local/libexec/desk-network/warm-backend.sh:/services/start-backend.sh:ro','-v','/usr/local/libexec/desk-network/warm-idle.sh:/services/start-php-fpm.sh:ro','-v','/usr/local/libexec/desk-network/warm-idle.sh:/services/start-cron.sh:ro']
   cmd.append(IMAGE);self.running=True;self.ready=False;self.container_started=clock();self.boot_ready_seconds=None
   try:
    self.command(*cmd,timeout=5)
    info=json.loads(self.command('podman','inspect',NAME,timeout=1).stdout)[0];pid=info['State']['Pid']
    self.pid=(pid,pathlib.Path(f'/proc/{pid}/stat').read_text().split(') ',1)[1].split()[19])
    if self.budget.start(self.pid,self.local,clock()-self.last_local<15):LOG.info('Collector bootstrap budget: at most one core for 60 seconds')
   except Exception:
    self.kill_container();raise
   if not preload and clock()>=self.until:self.kill_container();raise Unavailable('Lease expired while starting')
   self.last_snapshot=clock()
   LOG.info('LAN collector started on %s',self.node)
 def activate_warm(self):
  if not self.owned() or self.warm_state().get('state')!='inactive':raise Unavailable('Warm runtime not inactive')
  # No PHP/backend/database handles survive this boundary. The preloaded parent
  # contains imports only; restore the latest majority-certified data first.
  self.retire_checkpoint_source()
  restore(self.store.blob(self.store.head['digest']),self.root/'data',self.run,self.c['api_token'])
  if not self.owned():raise Unavailable('Lease expired during warm restore')
  self.generation=secrets.token_hex(16);self.promotion_started=clock();self.promotion_seconds=None
  self.boot_ready_seconds=None;self.container_started=clock();self.last_snapshot=clock();self.last_health=0.;self.unhealthy_since=0.
  self.warm_control(True)
 def kill_container(self,force=False):
  # Retire the short probe without waiting for the startup/restore lock.
  # A probe racing this boundary rechecks ownership before releasing oplock.
  self.until=0.;self.ready=False;self.retire_checkpoint_source()
  if self.warm and self.running and not force and not self.stop.is_set():
   self.until=0.;self.ready=False
   with self.oplock:
    if self.deactivate_warm():return
   LOG.warning('Warm fencing failed; destroying the native namespace')
  self.budget.settle(True)
  self.until=0.;self.ready=False
  with self.scan_lock:self.scan_queued_at=0.;self.scan_cache_at=0.
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
  self.running=False;self.pid=None;self.budget.close()
  (self.run/'public.json').unlink(missing_ok=True)
 def fence(self):
  while not self.stop.wait(.5):
   self.budget.settle(self.ready or (not self.owned() and not self.warm) or (self.warm and self.warm_state().get('state')=='inactive') or not bootstrap.quiet(self.local) or clock()-self.last_local>15)
   if self.running and clock()>=self.until and (not self.warm or self.generation is not None):self.kill_container();LOG.warning('Collector fenced after lease expiration')
 def native_health(self):
  if self.warm:
   state=self.warm_state()
   if state.get('state')!='active' or state.get('generation')!=self.generation:return False
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
 def scan_status(self):
  now=time.time()
  with self.scan_lock:
   if now-self.scan_cache_at<2:return self.scan_cache
   root=pathlib.Path(f'/proc/{self.pid[0]}/root/tmp') if self.pid and self.owned() else None
   value=discovery.status(self.root/'data/db/app.db',root,'/run/desk-network-scan/owner-state.json',self.node,list(self.term) if self.term else None,self.live_scan()[1],self.scan_queued_at,now)
   value['service_state']='active' if self.owned() and self.ready else 'starting' if self.owned() else 'standby'
   self.scan_cache=value;self.scan_cache_at=now
   return value
 def public(self):
  schedule,minutes=self.live_scan()
  return {'node':self.node,'state':'active' if self.owned() and self.ready else 'starting' if self.owned() else 'standby','protocol':'desk-network-v1','lease_term':list(self.term) if self.owned() else None,'observed_at':time.time(),'voter_ready':clock()>=self.lease.quarantine,'revision':self.store.head['revision'] if self.store.head else None,'sync':self.sync_state,'subnet':'192.168.1.0/24','scan_minutes':minutes,'scan_schedule':schedule,'boot_ready_seconds':self.boot_ready_seconds,'bootstrap_budget':'boosted' if self.budget.fd is not None else 'steady','standby_mode':'warm' if self.warm else 'cold','warm_state':self.warm_state().get('state') if self.warm else None,'preload_seconds':self.warm_state().get('preload_seconds') if self.warm else None,'promotion_seconds':self.promotion_seconds}
 def run_loop(self):
  self.kill_container(force=True);threading.Thread(target=self.fence,daemon=True).start()
  # Clean up any sqlite-*.db files left by OOM-killed workers from the previous
  # codebase (before per-worker scratch dirs).  No worker is running at this
  # point, so min_age_seconds=0 is safe.
  checkpoint_budget.sweep_stale_sqlite(self.run,min_age_seconds=0)
  while not self.stop.is_set():
   try:
    self.refresh_local();heads=self.fanout('status')
    if self.owned():
     held=other_owners(self.local)
     other_eligible=any(n!=self.node and h.get('eligible') and h.get('score',200)<OVERLOAD_PENALTY for n,h in heads.items())
     forced=any(v is True for v in self.local.get('admission',{}).values()) or (self.warm and (self.run/'warm-drain').exists())
     wanted=not forced and held>=2 and other_eligible
     job=self._rebalance;handover=False
     if job and job['done'].is_set():
      self._rebalance=None
      if wanted and clock()>=self._rebalance_retry:
       # Hand over only a checkpoint that succeeded under the term still held now.
       if job['ok'] and self.term==job['term'] and self.owned():handover=True
       else:self._rebalance_retry=clock()+60;LOG.warning('Rebalance deferred: checkpoint failed or lease changed, keeping lease')
      job=None
     if forced:
      try:self.snapshot()
      except Exception:pass
      self.kill_container();self.term=None
     elif handover:
      self.cooldown=clock()+60;self.kill_container();self.term=None
     else:
      if wanted and job is None and clock()>=self._rebalance_retry:
       # The checkpoint runs beside the loop so the lease keeps renewing while it waits on the worker, snaplock and replicas.
       LOG.info('Rebalancing network: node holds %d other owners, eligible peer available',held)
       self._rebalance={'done':threading.Event(),'ok':False,'term':list(self.term)}
       threading.Thread(target=self.rebalance_checkpoint,args=(self._rebalance,),daemon=True).start()
      stamp=clock();votes=self.fanout('lease',{'term':self.term})
      if len(votes)>=2:self.renew(stamp,self.term)
      if clock()-self.last_health>3:
       healthy=self.native_health();self.last_health=clock()
       if healthy:
        self.unhealthy_since=0.
        if self.boot_ready_seconds is None:
         self.boot_ready_seconds=round(clock()-self.container_started,2)
         if self.warm:self.promotion_seconds=round(clock()-self.promotion_started,2)
       elif not self.unhealthy_since:self.unhealthy_since=clock()
       self.ready=healthy
       if not healthy and clock()-self.container_started>120 and clock()-self.unhealthy_since>20:
        self.cooldown=clock()+90;self.kill_container();self.term=None;LOG.warning('Unhealthy collector withdrawn')
      if self.ready and self._rebalance is None and clock()-self.last_snapshot>300:
       # Snapshot work never blocks lease renewal or its fencing watchdog.
       if not self.snaplock.locked():threading.Thread(target=self.try_snapshot,daemon=True).start()
    else:
     self.catchup(heads)
     if self.warm and self.running and clock()-self.container_started>150 and self.warm_state().get('state') not in ('inactive','active'):
      self.kill_container(force=True);self.cooldown=clock()+90;LOG.warning('Warm preload failed; canary withdrawn')
     if self.warm and not self.running and clock()>=self.cooldown and self.local.get('online') and not any(v is True for v in self.local.get('admission',{}).values()):self.start_container(preload=True)
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
  self.kill_container(force=True)
 def rebalance_checkpoint(self,job):
  try:self.snapshot(expected_term=job['term']);job['ok']=True
  except Exception:job['ok']=False
  finally:job['done'].set()
 def try_snapshot(self):
  try:self.snapshot(force=False);self._snap_backoff=0.
  except subprocess.SubprocessError as e:
   # Worker was OOM-killed or timed out.  Back off exponentially to avoid
   # hammering the tmpfs slice every ~34 s while the slice is overloaded.
   # Cap at 5 minutes; reset to 0 on the next success.
   backoff=min(self._snap_backoff*2 or 30.,300.)
   self._snap_backoff=backoff
   self.sync_state='pending';self.last_snapshot=clock()-300+backoff
   LOG.warning('Checkpoint worker failed (%s); retrying in %.0fs',type(e).__name__,backoff)
  except Exception:
   self.sync_state='pending';self.last_snapshot=clock()-270;LOG.warning('Checkpoint replication pending')
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
 protocol_version='HTTP/1.1'
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
  except Exception:
   self.close_connection=True
   write_response(self,409,encoded({'error':'Peer request rejected'}),headers={'Connection':'close'})
def is_mutation(method,path,body):
 if method in ('GET','HEAD'):
  query=urllib.parse.parse_qs(urllib.parse.urlsplit(path).query)
  return bool(set(query)&{'action','function','sql','query','delete','update'})
 if method=='OPTIONS':return False
 route=urllib.parse.urlsplit(path).path
 # These pinned native POST handlers render/read only. A POST is not by itself
 # evidence of a database edit: checkpointing these serialized every page load.
 if method=='POST' and route in ('/network/server/devices/by-status','/network/server/devices/search','/network/php/components/device_cards.php','/network/php/components/tile_cards.php'):return False
 if method=='POST' and route=='/network/server/dbquery/read':
  # The native handler is autocommit despite its name. Only a single SELECT
  # may bypass the write quorum/checkpoint; malformed/other SQL fails closed.
  try:
   query=urllib.parse.unquote(base64.b64decode(json.loads(body)['rawSql'],validate=True).decode('utf-8'))
   return not bool(re.match(r'\s*SELECT\b',query,re.I))
  except (ValueError,KeyError,TypeError,UnicodeError):return True
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
  layout="""<style id="desk-network-layout">#desk-network-tools .desk-network-meta{margin:0 10px 8px;display:flex;align-items:center;gap:6px;font-size:11px}#desk-network-status{flex:1;min-width:0;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;opacity:.8}#desk-network-scan{flex-shrink:0}.sidebar-collapse #desk-network-tools .desk-network-meta,.sidebar-collapse #desk-network-discovery{display:none}#desk-network-discovery{margin:0 10px 10px;font-size:11px;border-top:1px solid #8883;padding-top:7px}#desk-network-discovery summary{cursor:pointer;display:flex;justify-content:space-between;gap:6px}#desk-network-discovery .desk-discovery-content{padding:8px 0 0;line-height:1.5;overflow-wrap:anywhere}#desk-network-discovery p{margin:0 0 5px}#desk-network-discovery small{display:block;opacity:.8;margin-bottom:8px}#desk-network-scan:disabled{opacity:.75}</style>"""
  text=text.replace('<head>','<head>'+storage+layout,1)
  text=text.replace('src="lib/treeviz/treeviz.iife.js"','src="lib/treeviz/treeviz.iife.js?desk=library-only-v1"')
  text=text.replace('js/api.js?v=','js/api.js?desk=base-v1&v=')
  banner='<li id="desk-network-tools"><a href="/"><i class="fa fa-fw fa-home"></i><span>Desk Cluster</span></a><div class="desk-network-meta"><span id="desk-network-status">HA</span><button type="button" id="desk-network-scan" class="btn btn-default btn-xs">Scan now</button></div><details id="desk-network-discovery"><summary>Discovery <span id="desk-discovery-freshness">Unknown</span></summary><div class="desk-discovery-content"><p id="desk-discovery-owner">Owner unconfirmed</p><p id="desk-discovery-arp">ARP · Waiting</p><small id="desk-discovery-result"></small><p id="desk-discovery-mdns">mDNS · Waiting</p><small id="desk-discovery-age">Freshness unknown</small></div></details></li>'
  text,placed=re.subn(r'(<ul\b[^>]*class="[^"]*\bsidebar-menu\b[^"]*"[^>]*>)',lambda m:m[1]+banner,text,count=1)
  if not placed:text=re.sub(r'(<body[^>]*>)',lambda m:m[1]+'<ul>'+banner+'</ul>',text,count=1)
  if urllib.parse.urlsplit(path).path=='/network/settings.php':
   notice='<aside id="desk-settings-policy" style="padding:10px 15px;background:#3c8dbc18;border-bottom:1px solid #3c8dbc40;font-size:13px">Cluster-managed controls are marked and locked. Other settings, including the scan schedule, can be saved here.</aside><script defer src="/network/desk/settings-policy.js"></script>'
   text,inserted=re.subn(r'(<div\b[^>]*\bclass="[^"]*\bcontent-wrapper\b[^"]*"[^>]*>)',lambda m:m[1]+notice,text,count=1)
   if not inserted:text=text.replace(banner,banner+notice,1)
  script='''<script>(()=>{const initialOwner=OWNER_JSON;let wasUnavailable=false;const out=document.getElementById('desk-network-status'),b=document.getElementById('desk-network-scan');async function status(){try{const r=await fetch('/network/desk/status');if(!r.ok)throw Error('Recovering');const s=await r.json();if(s.node!==initialOwner||wasUnavailable){location.reload();return;}out.textContent=s.sync==='synced'?'Synced':'Pending';out.title=`Network · ${s.node} · LAN only · ${s.sync==='synced'?'Synced':'Sync pending'}`;}catch{wasUnavailable=true;out.textContent='Reconnecting';out.title='Network · reconnecting';}}status();setInterval(status,10000);})();</script>'''
  script=script.replace('OWNER_JSON',json.dumps(node))
  text=text.replace('</body>',script+'<script defer src="/network/desk/scan-ui.js"></script></body>')
 return text.encode()
def native_route(node,path):
 # The same-origin /server namespace is the authenticated native API, not an
 # nginx directory. Its token checks and the UI mutation quorum still apply.
 if path.startswith('/server/'):return '127.0.0.1',20212,path[len('/server'):]
 return node,20211,path
class UI(http.server.BaseHTTPRequestHandler):
 protocol_version='HTTP/1.1'
 def log_message(self,*args):pass
 def handle_ui(self):
  c=self.server.cluster;conn=None
  try:
   if self.client_address[0] not in set(c.members.values())|{'127.0.0.1'}:return write_response(self,403,b'Forbidden','text/plain')
   if self.path=='/health':return write_response(self,200 if c.owned() and c.ready else 503,encoded(c.public()))
   if self.path=='/network/desk/status':return write_response(self,200,encoded(c.public()))
   if self.path=='/network/desk/scan-status' and self.command in ('GET','HEAD'):return write_response(self,200,encoded(c.scan_status()))
   if self.path=='/network/desk/scan-ui.js' and self.command in ('GET','HEAD'):return write_response(self,200,pathlib.Path('/usr/local/libexec/desk-network/scan-ui.js').read_bytes(),'application/javascript')
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
    if not c.renew(stamp,term):return write_response(self,503,b'Network lease expired','text/plain')
   if self.path=='/network/desk/scan':
    if self.command!='POST' or self.headers.get('X-Desk-Action')!='scan':return write_response(self,405,b'POST scan required','text/plain')
    current=c.scan_status();phase=current['scans']['arp']['phase']
    with c.scan_lock:
     if phase in ('queued','selecting','running','retrying','importing') or time.time()-c.scan_queued_at<5:return write_response(self,202,encoded({'state':'queued','requested_at':c.scan_queued_at or time.time()}))
     requested=time.time();c.scan_queued_at=requested;c.scan_cache_at=0
    req=urllib.request.Request('http://127.0.0.1:20212/plugin/ARPSCAN/run',data=b'{}',headers={'Authorization':'Bearer '+c.c['api_token'],'Content-Type':'application/json'})
    try:
     with c.opener.open(req,timeout=5) as r:
      if r.status!=200:raise Unavailable('Native queue unavailable')
     return write_response(self,202,encoded({'state':'queued','requested_at':requested}))
    except Exception:
     with c.scan_lock:c.scan_queued_at=0;c.scan_cache_at=0
     return write_response(self,503,encoded({'error':'Native scan queue unavailable'}))
   path=self.path[len('/network'):];headers={k:v for k,v in self.headers.items() if k.lower() not in ('host','connection','transfer-encoding','content-length','accept-encoding')};headers['Host']='192.168.1.186';headers['Accept-Encoding']='identity'
   host,port,path=native_route(c.members[c.node],path)
   conn=http.client.HTTPConnection(host,port,timeout=20);conn.request(self.command,path,body=raw or None,headers=headers);r=conn.getresponse();ctype=r.getheader('Content-Type','application/octet-stream')
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
    request.setsockopt(socket.IPPROTO_TCP,socket.TCP_NODELAY,1)
   super().process_request_thread(request,client_address)
  except OSError:self.shutdown_request(request)
  finally:self.slots.release()
 def handle_error(self,request,client_address):
  # Health probes/closed browser tabs reset idle keep-alive sockets normally.
  # ThreadingMixIn catches these before process_request_thread can intercept.
  if isinstance(sys.exc_info()[1],(ConnectionResetError,BrokenPipeError)):return
  super().handle_error(request,client_address)
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
 finally:
  c.stop.set();c.kill_container();c.peer_http.close();peer.shutdown();ui.shutdown()
  c.pool.shutdown(wait=False,cancel_futures=True);c.checkpoint_pool.shutdown(wait=False,cancel_futures=True)
if __name__=='__main__':main()
