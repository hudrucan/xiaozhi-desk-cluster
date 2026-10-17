"""Single-active Core adapter over existing leases, journal and scoped Core NATS."""
import argparse,asyncio,base64,datetime,math,concurrent.futures,copy,hashlib,hmac,http.server,json,logging,os,pathlib,re,shutil,secrets,signal,socket,ssl,subprocess,threading,time,urllib.request
import nats,sys
from cryptography.fernet import Fernet
from config_journal import Journal,atomic,encoded,digest
from lease import Lease,clock
from protocol import pack,unpack,MAX
from snapshot import MAX_BLOB,config_digest
from ui_bridge import OnlineSync
from http_config import configure as configure_http
ROOT=pathlib.Path('/var/lib/desk-homeassistant');RUN=pathlib.Path('/run/desk-homeassistant')
LIB=pathlib.Path('/usr/local/libexec/desk-homeassistant')
LOG=logging.getLogger('desk-homeassistant')

def shell(*args,timeout=15):return subprocess.run(args,check=True,stdout=subprocess.PIPE,stderr=subprocess.DEVNULL,timeout=timeout)
def sha(raw):return hashlib.sha256(raw).hexdigest()

def configure_component(root):
 # Fresh process prevents old imported source hooks surviving an additive rollout.
 shell(sys.executable,str(LIB/'component_config.py'),str(root),timeout=30)

def worker(action,*,source=None,destination=None,ticket=None,backup_id=None):
 unit='desk-homeassistant-checkpoint-'+secrets.token_hex(8)+'.service'
 cmd=['systemd-run','--quiet','--pipe','--wait','--collect','--unit='+unit,'--slice=desk-homeassistant-checkpoint.slice',
  '--property=BindsTo=desk-homeassistant.service','--property=After=desk-homeassistant.service',
  '--property=CPUQuota=50%','--property=MemoryMax=512M','--property=RuntimeMaxSec=180','--property=TasksMax=8',
  '--property=Nice=19','--property=NoNewPrivileges=yes','--', '/opt/desk-homeassistant/bin/python',str(LIB/'snapshot.py'),action]
 if source:cmd+=['--root' if action=='create' else '--input',str(source)]
 if destination:cmd+=['--output',str(destination)]
 if ticket:cmd+=['--ticket',ticket,'--backup-id',backup_id]
 try:return shell(*cmd,timeout=190).stdout
 except BaseException:
  subprocess.run(['systemctl','stop',unit],stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL,timeout=5,check=False)
  raise

class Agent(OnlineSync):
 def __init__(self,c):
  self.ui_init()
  self.c=c;self.node=c['node'];self.members=c['members'];self.key=c['key'];self.lease=Lease(ROOT/'vote.json')
  self.journal=Journal(ROOT/'journal.bin',Fernet(c['encryption_key']),self.node,self.members,self.key.encode())
  self.journal.initialize(c['seed']);self.bus=None;self.loop=None;self.stop=asyncio.Event();self.nonces={}
  self.until=0.;self.term=None;self.generation=None;self.ready=False;self.phase='standby';self.cooldown=0.;self.started_at=0.;self.op=None;self.exclusive=threading.Lock()
  self.heads={};self.local={};self.local_at=0.;self.disk_digest=None;self.last_digest=0.;self.last_gc=0.;self.applied=-1;self.last_error=None;self.unhealthy_since=None
  context=ssl.create_default_context(cafile=c['certificate'])
  self.opener=urllib.request.build_opener(urllib.request.ProxyHandler({}),urllib.request.HTTPSHandler(context=context))
 def owned(self):return self.term is not None and clock()<self.until
 def has_blob(self,value):
  if not isinstance(value,dict) or set(value)!={'digest','bytes','created_at','config_digest','image','format'} or type(value.get('created_at')) not in (int,float) or not math.isfinite(value['created_at']) or not re.fullmatch('[a-f0-9]{64}',value.get('config_digest','')) or value.get('image')!=self.c['image'] or value.get('format')!='desk-homeassistant-bundle-v1' or not re.fullmatch('[a-f0-9]{64}',value.get('digest','')) or type(value.get('bytes')) is not int or not 0<value['bytes']<=MAX_BLOB:return False
  p=ROOT/'blobs'/value['digest']
  return p.is_file() and p.stat().st_size==value['bytes']
 def public(self):
  head=self.journal.status(True);v=head['value'] or {};now=time.time()
  return {'protocol':'desk-homeassistant-v1','node':self.node,'observed_at':now,'state':self.phase if self.owned() else 'standby',
   'owner':self.node if self.owned() else None,'ready':self.owned() and self.ready,'term':self.term if self.owned() else None,
   'checkpoint_revision':head['revision'],'checkpoint_at':v.get('created_at'),'checkpoint_age_seconds':max(0,round(now-v['created_at'])) if v.get('created_at') else None,
   'pending_disk_changes':self.disk_digest!=v.get('config_digest') if self.owned() and self.disk_digest else None,
   'sync':'synced' if self.has_blob(v) else 'pending','applied_revision':self.applied,'voter_ready':clock()>=self.lease.quarantine,
   'error':self.last_error,'url':'http://192.168.1.186:8123/','config_rpo':'last_clean_checkpoint'}
 def status(self):
  return {'node':self.node,'lease':self.lease.status(),'owned':self.owned(),'ready':self.ready and self.owned(),
   'journal':self.journal.status(True),'eligible':self.eligible(),'score':self.score(),'blob_ready':self.has_blob(self.journal.state['value'])}
 def eligible(self):
  return clock()>=self.cooldown and clock()-self.local_at<15 and self.local.get('online') is True and not any(v is True for v in self.local.get('admission',{}).values()) and self.local.get('memory_available_mib',0)>=(512 if self.owned() else 1800)
 def draining(self):
  return clock()-self.local_at<15 and any(v is True for v in self.local.get('admission',{}).values())
 def score(self):
  net_active=((self.local.get('network') or {}).get('state') in ('starting','active') or self.local.get('network_cost',0)>0)
  net_cost=20 if net_active else 0
  vip_cost=sum(n for k,n in {'xiaozhi':20,'dns':10,'vpn':5}.items() if self.local.get('vip',{}).get(k))
  other_owners=sum(1 for k in ('xiaozhi','dns','vpn') if self.local.get('vip',{}).get(k))+(1 if net_active else 0)
  overload_penalty=100 if other_owners>=2 else 0  # keep equal to ha.py OVERLOAD_PENALTY
  return self.local.get('cpu',100)+vip_cost+net_cost+overload_penalty
 def refresh(self):
  try:
   with self.opener.open('http://'+self.members[self.node]+':8010/local',timeout=.8) as r:v=json.loads(r.read(65536))
   if v.get('id')!=self.node or v.get('online') is not True or type(v.get('cpu')) not in (int,float) or not math.isfinite(v['cpu']):raise ValueError()
   age=time.time()-datetime.datetime.fromisoformat(v['observed_at'].replace('Z','+00:00')).timestamp()
   if not -3<age<15:raise ValueError()
   mem={k:int(x.split()[0]) for k,x in (l.split(':',1) for l in pathlib.Path('/proc/meminfo').read_text().splitlines())}
   v['memory_available_mib']=mem['MemAvailable']//1024
   try:
    network=json.loads(pathlib.Path('/run/desk-network/public.json').read_bytes());v['network_cost']=20 if network['state']=='active' else 0
   except Exception:pass
   self.local=v;self.local_at=clock()
  except Exception:pass
 def permit(self,active=True):
  atomic(RUN/'permit.json',encoded({'until':self.until if active else 0,'generation':self.generation or '0'*32}));os.chmod(RUN/'permit.json',0o600)
 def rpc(self,action,value,sender):
  if action=='status':return self.status()
  if action=='lease':
   term=value.get('term')
   if not isinstance(term,list) or len(term)!=2 or type(term[0]) is not int or not 0<term[0]<=1000000000 or term[1]!=sender:raise ValueError('Invalid lease term')
   return self.lease.grant(term,sender)
  # Journal changes require a current majority-lease proposer; no native commands.
  grant=self.lease.status()
  if grant['holder']!=sender or grant['remaining']<=0:raise ValueError('No proposer lease')
  if action=='promise':return self.journal.promise(value['revision'],value['ballot'])
  if action=='accept':
   if not self.has_blob(value['value']):raise ValueError('Checkpoint not staged')
   return self.journal.accept(value['revision'],value['ballot'],value['value'])
  if action=='commit':
   if not self.has_blob(value['value']):raise ValueError('Checkpoint not staged')
   self.journal.commit(value['revision'],value['value'],value['certificate']);return {'revision':self.journal.state['revision']}
  raise ValueError('Unknown Core operation')
 async def received(self,msg):
  try:
   v=unpack(msg.data,self.key)
   if v['recipient']!=self.node or msg.reply.split('.')[:4]!=['desk','homeassistant','reply',v['sender']]:return
   self.nonces={k:t for k,t in self.nonces.items() if clock()-t<20}
   if v['id'] in self.nonces or len(self.nonces)>1024:return
   self.nonces[v['id']]=clock()
   result=await asyncio.to_thread(self.rpc,v['action'],v.get('value',{}),v['sender']);ok=True
  except Exception:result={'error':'Core operation rejected'};ok=False
  try:await msg.respond(pack({'sender':self.node,'recipient':v['sender'],'id':v['id'],'at':time.time(),'ok':ok,'value':result},self.key))
  except Exception:pass
 async def call(self,node,action,value=None,timeout=1.5):
  if node==self.node:return await asyncio.to_thread(self.rpc,action,value or {},self.node)
  ident=secrets.token_hex(16);inbox='desk.homeassistant.reply.'+self.node+'.'+ident;future=asyncio.get_running_loop().create_future()
  async def receive(msg):
   if future.done():return
   try:
    v=unpack(msg.data,self.key)
    if v['sender']!=node or v['recipient']!=self.node or v['id']!=ident or v.get('ok') is not True:raise ValueError()
    future.set_result(v['value'])
   except Exception:future.set_exception(ValueError('Core peer rejected'))
  sub=await self.bus.subscribe(inbox,cb=receive,pending_msgs_limit=1,pending_bytes_limit=MAX+256)
  try:
   await self.bus.flush(timeout=.5)
   await self.bus.publish('desk.homeassistant.control.'+node,pack({'sender':self.node,'recipient':node,'id':ident,'at':time.time(),'action':action,'value':value or {}},self.key),reply=inbox)
   return await asyncio.wait_for(future,timeout)
  finally:await sub.unsubscribe()
 async def fanout(self,action,value=None):
  replies=await asyncio.gather(*(self.call(n,action,value) for n in self.members),return_exceptions=True)
  return {n:r for n,r in zip(self.members,replies) if not isinstance(r,BaseException)}
 def transfer(self,node,method,value,blob=None):
  path='/blobs/'+value['digest'];stamp=str(int(time.time()));nonce=secrets.token_hex(16);body=blob or b''
  signed=(method+'\n'+path+'\n'+stamp+'\n'+nonce+'\n'+sha(body)).encode()
  req=urllib.request.Request('https://'+self.members[node]+':20225'+path,data=blob,method=method,
   headers={'X-Desk-Node':self.node,'X-Desk-Time':stamp,'X-Desk-Nonce':nonce,'X-Desk-Signature':hmac.new(self.key.encode(),signed,hashlib.sha256).hexdigest(),'Content-Type':'application/octet-stream'})
  with self.opener.open(req,timeout=180) as r:
   parts=[];size=0
   while True:
    part=r.read(min(65536,MAX_BLOB+1-size))
    if not part:break
    size+=len(part)
    if size>MAX_BLOB:raise ValueError('Blob limit')
    parts.append(part)
   raw=b''.join(parts)
   if method=='GET':
    if sha(raw)!=value['digest'] or len(raw)!=value['bytes']:raise ValueError('Blob mismatch')
    self.store_blob(value['digest'],raw)
   return raw
 def store_blob(self,name,raw):
  if not re.fullmatch('[a-f0-9]{64}',name) or sha(raw)!=name or len(raw)>MAX_BLOB:raise ValueError('Blob mismatch')
  final=ROOT/'blobs'/name
  if final.is_file():return
  temp=ROOT/'blobs'/('incoming-'+secrets.token_hex(8));atomic(temp,raw)
  try:worker('validate',source=temp);os.replace(temp,final)
  finally:temp.unlink(missing_ok=True)
 def collect_old_artifacts(self):
  # Keep the certified/accepted/seed values and recent in-flight transfers.
  state=self.journal.status(True)
  keep={v.get('digest') for v in (state.get('value') or {},(state.get('accepted') or {}).get('value') or {},self.c['seed'])}
  now=time.time()
  for dirname,pattern,protected in [('blobs','[a-f0-9]{64}',keep),('generations','[a-f0-9]{32}',{(ROOT/'current').resolve().name})]:
   entries=sorted((p for p in (ROOT/dirname).iterdir() if re.fullmatch(pattern,p.name) and not p.is_symlink()),key=lambda p:p.stat().st_mtime,reverse=True)
   protected=protected|{p.name for p in entries[:3]}
   for p in entries:
    if p.name not in protected and now-p.stat().st_mtime>3600:
     if dirname=='blobs':p.unlink()
     else:shutil.rmtree(p)
 async def ensure_blob(self,value,heads):
  if self.has_blob(value):return
  for node in heads:
   if node==self.node:continue
   try:await asyncio.to_thread(self.transfer,node,'GET',value);return
   except Exception:continue
  raise ValueError('Certified checkpoint unavailable')
 async def catchup(self,heads):
  if len(heads)<2:raise ValueError('No configuration quorum')
  best=max(heads.values(),key=lambda h:h['journal']['revision'])['journal']
  await self.ensure_blob(best['value'],heads)
  if best['revision']>self.journal.state['revision']:
   self.journal.commit(best['revision'],best['value'],best['certificate'])
 async def propose(self,desired):
  for attempt in range(4):
   if not self.owned():raise ValueError('Lease lost')
   heads=await self.fanout('status');await self.catchup(heads)
   revision=self.journal.state['revision']+1
   ballot=[max(int(time.time()*1000),max(h['journal']['promised'][0] for h in heads.values())+1),self.node]
   promises=await self.fanout('promise',{'revision':revision,'ballot':ballot})
   if len(promises)<2:raise ValueError('No promise quorum')
   if any('head' in p for p in promises.values()):continue
   accepted=[p['accepted'] for p in promises.values() if p.get('accepted')]
   chosen=max(accepted,key=lambda a:a['ballot'])['value'] if accepted else desired
   await self.ensure_blob(chosen,heads)
   raw=(ROOT/'blobs'/chosen['digest']).read_bytes()
   staged=await asyncio.gather(*(asyncio.to_thread(self.transfer,n,'POST',chosen,raw) for n in self.members if n!=self.node),return_exceptions=True)
   if sum(not isinstance(v,BaseException) for v in staged)<1:raise ValueError('No durable blob quorum')
   votes=await self.fanout('accept',{'revision':revision,'ballot':ballot,'value':chosen})
   if len(votes)<2 or not self.owned():raise ValueError('No accept quorum')
   commits=await self.fanout('commit',{'revision':revision,'value':chosen,'certificate':list(votes.values())})
   if len(commits)<2:raise ValueError('Commit acknowledgment pending')
   if digest(chosen)==digest(desired):return revision
  raise ValueError('Recovery needs another checkpoint attempt')
 async def recover_accepted(self,heads):
  values=[h['journal'].get('accepted') for h in heads.values() if h['journal']['revision']==self.journal.state['revision']]
  if any(values):
   desired=max((v for v in values if v),key=lambda v:v['ballot'])['value'];await self.propose(desired)
 async def activate(self):
  with self.exclusive:
   self.phase='starting';self.last_error=None
   await self.catchup(self.heads);await self.recover_accepted(self.heads)
   value=self.journal.state['value'];target=ROOT/'generations'/secrets.token_hex(16);target.mkdir(mode=0o700)
   await asyncio.to_thread(worker,'restore',source=ROOT/'blobs'/value['digest'],destination=target)
   await asyncio.to_thread(configure_http,target)
   await asyncio.to_thread(configure_component,target)
   if not self.owned():raise ValueError('Lease lost during restore')
   link=ROOT/'current.new';link.unlink(missing_ok=True);link.symlink_to(target);os.replace(link,ROOT/'current')
   fd=os.open(ROOT,os.O_RDONLY|os.O_DIRECTORY)
   try:os.fsync(fd)
   finally:os.close(fd)
   self.applied=self.journal.state['revision'];self.generation=secrets.token_hex(16);self.permit()
   self.started_at=clock();self.unhealthy_since=None;await asyncio.to_thread(shell,'systemctl','start','desk-homeassistant-core',timeout=20)
 async def checkpoint(self,handoff=False):
  if not self.owned() or self.phase!='active' or not self.ready:raise ValueError('Core is not ready')
  if self.ui_busy():raise ValueError('Online sync is running')
  with self.exclusive:
   self.phase='checkpointing';self.ready=False;self.last_error=None;certified=False
   try:
    await asyncio.to_thread(shell,'systemctl','stop','desk-homeassistant-core',timeout=80)
    exit=json.loads((RUN/'stopped.json').read_bytes())
    if exit['exit_code']!=0 or exit['oom'] or time.time()-exit['at']>10:raise ValueError('Core did not stop cleanly')
    await asyncio.to_thread(configure_http,(ROOT/'current').resolve())
    await asyncio.to_thread(configure_component,(ROOT/'current').resolve())
    path=ROOT/'blobs'/('create-'+secrets.token_hex(8))
    try:
     value=json.loads(await asyncio.to_thread(worker,'create',source=(ROOT/'current').resolve(),destination=path))
     os.replace(path,ROOT/'blobs'/value['digest'])
    finally:path.unlink(missing_ok=True)
    self.applied=await self.propose(value);self.disk_digest=value['config_digest'];certified=True
   except Exception:
    self.last_error='checkpoint_failed';raise
   finally:
    if self.owned() and (not handoff or not certified):
     self.phase='starting';self.started_at=clock();self.unhealthy_since=None;self.permit()
     await asyncio.to_thread(shell,'systemctl','start','desk-homeassistant-core',timeout=20)
    elif handoff and certified:
     self.until=0;self.permit(False);self.term=None;self.phase='standby';self.cooldown=clock()+300
 def health_expired(self,ready):
  self.ready=ready
  if ready:self.phase='active';self.unhealthy_since=None;return False
  # Native HA Restart keeps the supervised namespace alive. Give it the same
  # startup grace without restoring an older checkpoint or losing local saves.
  if self.unhealthy_since is None:self.unhealthy_since=self.started_at if self.phase=='starting' else clock()
  if self.phase=='active':
   try:
    exited=json.loads((RUN/'exited.json').read_bytes())
    if exited['generation']==self.generation:return True
   except Exception:pass
  return clock()-self.unhealthy_since>180
 async def native_health(self):
  def fetch():
   with self.opener.open('http://'+self.members[self.node]+':18123/api/desk_cluster/ready',timeout=1) as r:return json.loads(r.read(1024)).get('ready') is True
  try:return await asyncio.to_thread(fetch)
  except Exception:return False
 async def cli(self,reader,writer):
  try:
   data=await asyncio.wait_for(reader.readline(),1)
   if len(data)>1024:raise ValueError()
   v=json.loads(data)
   if v=={'action':'status'}:result=self.public()
   elif v in ({'action':'checkpoint'},{'action':'handoff'}):
    if self.op and not self.op.done():raise ValueError('Operation in progress')
    self.op=asyncio.create_task(self.checkpoint(v['action']=='handoff'))
    await self.op;result=self.public()
   else:raise ValueError()
   writer.write(encoded({'ok':True,'value':result})+b'\n')
  except Exception:writer.write(encoded({'ok':False,'error':'Core operation failed; inspect public status'})+b'\n')
  finally:
   try:await writer.drain()
   finally:writer.close()
 async def run(self):
  self.loop=asyncio.get_running_loop()
  self.bus=await nats.connect(servers=self.c['servers'],user=self.c['nats']['user'],password=self.c['nats']['password'],name='desk-homeassistant-'+self.node,allow_reconnect=True,max_reconnect_attempts=-1,reconnect_time_wait=1,pending_size=262144)
  await self.bus.subscribe('desk.homeassistant.control.'+self.node,cb=self.received,pending_msgs_limit=64,pending_bytes_limit=131072);await self.bus.flush()
  socketpath=RUN/'admin.sock';socketpath.unlink(missing_ok=True)
  server=await asyncio.start_unix_server(self.cli,path=str(socketpath));os.chmod(socketpath,0o600)
  ui_server=await self.ui_server(ROOT)
  while not self.stop.is_set():
   try:
    self.expire_ui_job()
    await asyncio.to_thread(self.refresh);heads=await self.fanout('status');self.heads=heads
    if self.owned():
     stamp=clock();votes=await self.fanout('lease',{'term':self.term})
     if len(votes)>=2 and self.owned():self.until=stamp+10;self.permit()
     if self.op and self.op.done():
      try:self.op.result()
      except Exception:self.last_error='core_operation_failed'
      self.op=None
     if self.draining() and not self.op and not self.ui_busy() and self.phase=='active':self.op=asyncio.create_task(self.checkpoint(True))
     if not self.op:
      if self.health_expired(await self.native_health()):
       self.until=0;self.permit(False);self.term=None;self.phase='standby';self.cooldown=clock()+90;self.last_error='core_start_failed'
     if self.ready and clock()-self.last_digest>30:
      self.disk_digest=await asyncio.to_thread(config_digest,(ROOT/'current').resolve());self.last_digest=clock()
    else:
     self.ready=False;self.permit(False)
     if self.op and not self.op.done():
      await asyncio.sleep(1);continue
     self.term=None;self.phase='standby'
     await self.catchup(heads)
     active=any(h['owned'] or h['lease']['holder'] for h in heads.values())
     choices=sorted((h['score'],n) for n,h in heads.items() if h['eligible'] and h['blob_ready'])
     if not active and len(heads)>=2 and choices and choices[0][1]==self.node and self.eligible():
      term=[max(h['lease']['promised'][0] for h in heads.values())+1,self.node];stamp=clock();votes=await self.fanout('lease',{'term':term})
      if len(votes)>=2:
       self.term=term;self.until=stamp+10;self.generation=secrets.token_hex(16);self.permit();self.op=asyncio.create_task(self.activate())
    if (not self.op or self.op.done()) and clock()-self.last_gc>600:
     await asyncio.to_thread(self.collect_old_artifacts);self.last_gc=clock()
    atomic(RUN/'public.json',encoded(self.public()));os.chmod(RUN/'public.json',0o644)
   except Exception as error:
    self.last_error='quorum_or_checkpoint_unavailable';LOG.warning('Core reconciliation deferred: %s',type(error).__name__)
   try:await asyncio.wait_for(self.stop.wait(),2)
   except asyncio.TimeoutError:pass
  self.ready=False;self.until=0;self.permit(False)
  ui_server.close();await ui_server.wait_closed()
  server.close();await server.wait_closed();await self.bus.close()

class Handler(http.server.BaseHTTPRequestHandler):
 def log_message(self,*args):pass
 def reply(self,status,body):
  self.send_response(status);self.send_header('Content-Type','application/json');self.send_header('Content-Length',str(len(body)));self.send_header('Cache-Control','no-store');self.send_header('Connection','close');self.end_headers();self.wfile.write(body)
 def handle_request(self):
  a=self.server.agent
  try:
   if self.client_address[0] not in set(a.members.values())|{'127.0.0.1'}:return self.reply(403,b'{}')
   if self.server.kind=='status':
    if self.command!='GET':return self.reply(405,b'{}')
    if self.path=='/ready':return self.reply(200 if a.owned() and a.ready else 503,encoded(a.public()))
    if self.path=='/status':return self.reply(200,encoded(a.public()))
    return self.reply(404,b'{}')
   match=re.fullmatch('/blobs/([a-f0-9]{64})',self.path)
   if not match or self.command not in ('GET','POST'):return self.reply(404,b'{}')
   size=int(self.headers.get('Content-Length','0'))
   if not 0<=size<=MAX_BLOB:raise ValueError()
   body=self.rfile.read(size)
   sender=self.headers.get('X-Desk-Node');stamp=self.headers.get('X-Desk-Time','');nonce=self.headers.get('X-Desk-Nonce','')
   if sender not in a.members or self.client_address[0]!=a.members[sender] or abs(time.time()-int(stamp))>30 or not re.fullmatch('[a-f0-9]{32}',nonce):raise ValueError()
   raw=(self.command+'\n'+self.path+'\n'+stamp+'\n'+nonce+'\n'+sha(body)).encode()
   if not hmac.compare_digest(self.headers.get('X-Desk-Signature',''),hmac.new(a.key.encode(),raw,hashlib.sha256).hexdigest()):raise ValueError()
   if self.command=='POST':a.store_blob(match[1],body);return self.reply(200,b'{}')
   path=ROOT/'blobs'/match[1]
   if not path.is_file() or path.stat().st_size>MAX_BLOB:raise ValueError()
   blob=path.read_bytes();self.reply(200,blob)
  except (BrokenPipeError,ConnectionResetError):pass
  except Exception:self.reply(409,b'{}')
 do_GET=do_POST=handle_request

class Server(http.server.ThreadingHTTPServer):
 daemon_threads=True
 def __init__(self,*args,**kwargs):self.slots=threading.BoundedSemaphore(8);super().__init__(*args,**kwargs)
 def process_request(self,request,address):
  if not self.slots.acquire(False):self.shutdown_request(request);return
  try:super().process_request(request,address)
  except Exception:self.slots.release();raise
 def process_request_thread(self,request,address):
  try:
   if getattr(self,'context',None):request.settimeout(2);request=self.context.wrap_socket(request,server_side=True)
   request.settimeout(180);super().process_request_thread(request,address)
  except OSError:self.shutdown_request(request)
  finally:self.slots.release()
 def handle_error(self,*args):pass

def cli(action):
 with socket.socket(socket.AF_UNIX) as sock:
  sock.settimeout(300);sock.connect(str(RUN/'admin.sock'));sock.sendall(encoded({'action':action})+b'\n');data=b''
  while not data.endswith(b'\n'):
   data+=sock.recv(65536)
   if len(data)>65536:raise ValueError()
  value=json.loads(data)
  print(json.dumps(value))
  if not value['ok']:raise SystemExit(1)

def main():
 p=argparse.ArgumentParser();p.add_argument('--action',choices=['status','checkpoint','handoff']);p.add_argument('--config',default='/etc/desk-homeassistant/config.json');args=p.parse_args()
 if args.action:return cli(args.action)
 a=Agent(json.loads(pathlib.Path(args.config).read_bytes()));logging.basicConfig(level=logging.INFO,format='%(message)s')
 for port,kind in ((20223,'status'),(20225,'blobs')):
  s=Server((a.members[a.node],port),Handler);s.agent=a;s.kind=kind
  if kind=='blobs':
   ctx=ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER);ctx.load_cert_chain(a.c['certificate'],a.c['tls_key']);s.context=ctx
  threading.Thread(target=s.serve_forever,daemon=True).start()
 async def run():
  loop=asyncio.get_running_loop()
  for sig in (signal.SIGTERM,signal.SIGINT):loop.add_signal_handler(sig,a.stop.set)
  await a.run()
 asyncio.run(run())
if __name__=='__main__':
 try:main()
 except Exception:raise SystemExit('Home Assistant controller failed; private diagnostics withheld')
