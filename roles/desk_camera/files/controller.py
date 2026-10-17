"""Camera placement over the existing majority lease; no media in NATS."""
import asyncio,base64,datetime,http.server,ipaddress,json,logging,math,os,pathlib,secrets,signal,subprocess,sys,threading,time,urllib.request
sys.path.insert(0,'/usr/local/libexec/desk-homeassistant')
from transport import SharedRPC,FleetStatus
from health import CameraHealth
from cache_gc import cleanup as clean_cache
from playback import Playback,PlaybackError,clip_path
from lease import Lease,clock
from config_journal import atomic,encoded
from protocol import pack,unpack,MAX

ROOT=pathlib.Path('/var/lib/desk-camera');RUN=pathlib.Path('/run/desk-camera')
PREFIX='desk.ha.media'

class StatusFiles:
 """Share parsed metadata, but never cache readiness or lease deadlines."""
 def __init__(self,max_age=1.,monotonic=time.monotonic):
  self.max_age,self.monotonic=max_age,monotonic;self.entries={};self.paths=None;self.paths_key=None;self.paths_at=0.;self.lock=threading.RLock()
 def read(self,path):
  with self.lock:
   try:
    st=path.stat();key=(st.st_ino,st.st_mtime_ns,st.st_size);now=self.monotonic();old=self.entries.get(path)
    if old and old[0]==key and now-old[1]<self.max_age:return old[2]
    self.entries.pop(path,None)
    if st.st_size>65536:raise ValueError('Camera status exceeds limit')
    value=json.loads(path.read_bytes());self.entries[path]=(key,now,value);return value
   except Exception:
    self.entries.pop(path,None);raise
 def permits(self):
  with self.lock:
   st=RUN.stat();key=(RUN,st.st_ino,st.st_mtime_ns);now=self.monotonic()
   if self.paths is None or key!=self.paths_key or now-self.paths_at>=self.max_age:
    self.paths=[RUN/'permit.json',*RUN.glob('*/permit.json')];self.paths_key=key;self.paths_at=now
   return self.paths

def paths(c):
 workload=c['workload']
 if not __import__('re').fullmatch(r'[a-z0-9_]{1,64}',workload):raise ValueError('Invalid camera workload')
 return (ROOT,RUN) if workload=='tapo_c222_160' else (ROOT/workload,RUN/workload)

class Telemetry:
 def __init__(self):self.value=None;self.at=0;self.lock=threading.Lock()
 def refresh(self,a):
  with self.lock:
   if clock()-self.at<1.5:
    a.local=self.value;a.local_at=self.at;return
   a.read_telemetry();self.value=a.local;self.at=a.local_at

class Controller:
 def __init__(self,c,telemetry=None,files=None):
  self.c=c;self.node=c['node'];self.members=c['members'];self.root,self.run_dir=paths(c);self.root.mkdir(parents=True,exist_ok=True,mode=0o700);self.run_dir.mkdir(parents=True,exist_ok=True,mode=0o700);self.lease=Lease(self.root/'vote.json')
  self.prefix=PREFIX if c['workload']=='tapo_c222_160' else PREFIX+'.'+c['workload'];self.telemetry=telemetry or Telemetry();self.health=CameraHealth();self.owner_seen=None;self.owner_changed_at=None;self.previous_owner=None
  self.until=0;self.term=None;self.generation=None;self.transport=None;self.fleet=None;self.stop=asyncio.Event();self.nonces={};self.heads={};self.local=None;self.local_at=0;self.started_at=0;self.cooldown=0
  self.files=files or StatusFiles();self.playback=Playback(self)
  self.opener=urllib.request.build_opener(urllib.request.ProxyHandler({}))
 def owned(self):return self.term is not None and clock()<self.until
 def eligible(self):
  return clock()>=self.cooldown and self.node in self.c['enabled_nodes'] and not (self.root/'drain').exists() and not (ROOT/'drain').exists() and self.health.reachable is True and self.local is not None and clock()-self.local_at<15 and self.local.get('online') is True and self.local.get('memory_available_mib',0)>=256 and not any(self.local.get('admission',{}).values())
 def ready(self):
  if not self.owned():return False
  try:
   v=self.files.read(self.run_dir/'runtime.json')
   return self.owned() and v.get('generation')==self.generation and v.get('ready') is True and time.time()-v['at']<4
  except Exception:return False
 def placement_score(self):
  # Prefer a free relay slot; retain a healthy owner instead of constantly rebalancing.
  slots=0
  files=getattr(self,'files',None)
  for p in files.permits() if files else [RUN/'permit.json',*RUN.glob('*/permit.json')]:
   if p.parent==self.run_dir:continue
   try:
    v=files.read(p) if files else json.loads(p.read_bytes())
    if v.get('generation') and clock()<v['until']<=clock()+11:slots+=1
   except Exception:pass
  return (self.health.penalty(self.local) if hasattr(self,'health') else (self.local.get('cpu',100) if self.local else 100))+100*slots
 def status(self):
  return {'node':self.node,'lease':self.lease.status(),'owned':self.owned(),'eligible':self.eligible(),'score':self.placement_score(),'definition':self.c['definition'],'ready':self.ready(),'generation':self.generation if self.owned() else None,'reachable':self.health.reachable,'memory_available_mib':self.local.get('memory_available_mib') if self.local else None}
 def public(self):
  owners=[n for n,h in self.heads.items() if h['owned'] and h['definition']==self.c['definition']]
  owner=owners[0] if len(owners)==1 else None
  if owner and owner!=self.owner_seen:
   self.previous_owner=self.owner_seen;self.owner_seen=owner;self.owner_changed_at=time.time()
  return {'node':self.node,'workload':self.c['workload'],'owner':owner,'ready':bool(owner and self.heads[owner]['ready']),'local_ready':self.ready(),'voter_ready':clock()>=self.lease.quarantine,'observed_at':time.time(),'generation':self.heads[owner].get('generation') if owner else None,'reachable':self.health.reachable,'score':self.placement_score(),'previous_owner':self.previous_owner,'owner_changed_at':self.owner_changed_at,'playback_active':self.playback.active(),'draining':bool((ROOT/'drain').exists() or (self.root/'drain').exists() or (self.local and any(self.local.get('admission',{}).values())))}
 def refresh(self):
  self.telemetry.refresh(self);self.health.refresh(self.c,self.local)
 def read_telemetry(self):
  try:
   with self.opener.open('http://'+self.members[self.node]+':8010/local',timeout=1) as r:v=json.loads(r.read(65536))
   age=time.time()-datetime.datetime.fromisoformat(v['observed_at'].replace('Z','+00:00')).timestamp()
   if v.get('id')!=self.node or not -3<age<15 or type(v.get('cpu')) not in (int,float) or not math.isfinite(v['cpu']):raise ValueError()
   mem={k:int(v.split()[0]) for k,v in (line.split(':',1) for line in pathlib.Path('/proc/meminfo').read_text().splitlines())}
   v['memory_available_mib']=mem['MemAvailable']//1024;self.local=v;self.local_at=clock()
  except Exception:pass
 def permit(self):atomic(self.run_dir/'permit.json',encoded({'until':self.until,'generation':self.generation if self.owned() else None}))
 def rpc(self,action,value,sender):
  if action=='playback_inventory':return self.playback.inventory()
  if action=='playback_cached':return self.playback.cached(value)
  if action=='playback_prepare':
   try:
    if self.fleet and any(a is not self and a.playback.active() for a in self.fleet.agents):raise PlaybackError('node_busy')
    return self.playback.prepare(value['clip'],value.get('session'),value.get('managed',False))
   except PlaybackError as err:return {'state':'rejected','reason':err.code}
  if action in ('playback_cancel','playback_touch'):return self.playback.viewer_action(value['id'],value['attempt'],action=='playback_cancel')
  if action=='playback_status':return self.playback.status(value['id'])
  if action=='status':return self.status()
  if action=='fleet_status' and self.prefix==PREFIX and self.fleet:return self.fleet.local()
  if action!='lease' or value.get('definition')!=self.c['definition']:raise ValueError()
  term=value.get('term')
  if not isinstance(term,list) or len(term)!=2 or type(term[0]) is not int or not 0<term[0]<1000000000 or term[1]!=sender:raise ValueError()
  return self.lease.grant(term,sender)
 async def received(self,msg):
  v=None
  try:
   v=unpack(msg.data,self.c['key'])
   if v['recipient']!=self.node or not msg.reply.startswith(self.prefix+'.reply.'+v['sender']+'.'):return
   self.nonces={k:t for k,t in self.nonces.items() if clock()-t<20}
   if v['id'] in self.nonces or len(self.nonces)>1024:return
   self.nonces[v['id']]=clock();result=self.rpc(v['action'],v.get('value',{}),v['sender']);ok=True
  except Exception:result={};ok=False
  if v:
   try:await msg.respond(pack({'sender':self.node,'recipient':v['sender'],'id':v['id'],'at':time.time(),'ok':ok,'value':result},self.c['key']))
   except Exception:pass
 async def call(self,node,action,value):return await self.transport.call(self,node,action,value)
 async def fanout(self,action,value=None):
  if action=='status' and self.fleet:return await self.fleet.heads(self)
  replies=await asyncio.gather(*(self.call(n,action,value or {}) for n in self.members),return_exceptions=True)
  return {n:r for n,r in zip(self.members,replies) if not isinstance(r,BaseException)}
 async def reconcile(self):
  await asyncio.to_thread(self.refresh);heads=await self.fanout('status');self.heads=heads
  if self.owned():
   if clock()-self.started_at>30 and not self.ready():self.cooldown=clock()+90
   if not self.eligible():self.until=0
   else:
    stamp=clock();votes=await self.fanout('lease',{'term':self.term,'definition':self.c['definition']})
    if len(votes)>=2 and self.owned():self.until=stamp+10
   self.permit()
  if not self.owned():
   self.term=None;self.permit()
   active=any(h['owned'] or h['lease']['holder'] for h in heads.values())
   choices=sorted((h['score'],n) for n,h in heads.items() if h['eligible'] and h['definition']==self.c['definition'])
   if not active and len(heads)>=2 and choices and choices[0][1]==self.node and self.eligible():
    term=[max(h['lease']['promised'][0] for h in heads.values())+1,self.node];stamp=clock()
    votes=await self.fanout('lease',{'term':term,'definition':self.c['definition']})
    if len(votes)>=2:self.term=term;self.until=stamp+10;self.started_at=clock();self.generation=secrets.token_hex(16);self.permit()
  atomic(self.run_dir/'public.json',encoded(self.public()));os.chmod(self.run_dir/'public.json',0o644)
 async def run(self,delay=0):
  await asyncio.sleep(delay)
  try:
   while not self.stop.is_set():
    try:await self.reconcile()
    except Exception:logging.warning('Camera placement deferred; details withheld')
    try:await asyncio.wait_for(self.stop.wait(),2)
    except TimeoutError:pass
  finally:self.until=0;self.permit()

async def local_client(reader,writer,agents):
 try:
  raw=await asyncio.wait_for(reader.readline(),2)
  if len(raw)>2048:raise ValueError()
  value=json.loads(raw);agent=next(a for a in agents if a.c['workload']==value['workload'])
  action=value['action']
  if action=='prepare':
   caches=await agent.fanout('playback_cached',value['clip']);cached=next((v for v in caches.values() if v.get('state') in ('ready','playable')),None)
   heads=await agent.fanout('status');owners=[n for n,h in heads.items() if h.get('owned') and h.get('ready')]
   if cached:result=cached
   else:
    if len(owners)!=1:raise ValueError('No ready camera owner')
    result=await agent.call(owners[0],'playback_prepare',{'clip':value['clip'],'session':value.get('session'),'managed':value.get('managed',False)})
  elif action=='inventory':
   replies=await agent.fanout('playback_inventory');items=[item for v in replies.values() for item in v.get('items',[])]
   result={'items':items[:240],'complete':len(items)<=240 and len(replies)==len(agent.members) and all(not v.get('truncated') for v in replies.values())}
  elif action in ('cancel','touch') and value['node'] in agent.members:result=await agent.call(value['node'],'playback_'+action,{'id':value['id'],'attempt':value['attempt']})
  elif action=='status' and value['node'] in agent.members:result=await agent.call(value['node'],'playback_status',{'id':value['id']})
  else:raise ValueError()
  out={'ok':True,'value':result}
 except Exception:out={'ok':False,'error':'Camera worker unavailable or busy. Retry when ready.'}
 try:writer.write(encoded(out)+b'\n');await writer.drain()
 finally:writer.close();await writer.wait_closed()

def thumbnail_request(handler):
 try:
  parts=handler.path.split('/')
  if len(parts)!=4 or parts[1]!='thumbnail':raise ValueError()
  workload,ident=parts[2:];clip_path(workload,ident)
  agent=handler.server.agents[0];token=handler.headers.get('X-Desk-Artifact','')
  if len(token)>4096:raise ValueError()
  value=unpack(base64.b64decode(token,validate=True),agent.c['key']);scope=value['value']
  if value['recipient']!=agent.node or value['action']!='thumbnail' or scope.get('workload')!=workload or scope.get('id')!=ident:raise ValueError()
  if handler.client_address[0] not in set(agent.members.values())|{'127.0.0.1'} and not handler.server.core_artifact_peer.allows(handler.client_address[0]):raise ValueError()
  length=int(handler.headers.get('Content-Length','-1'))
  if not 0<=length<=1024*1024:raise ValueError()
  handler.connection.settimeout(5);data=handler.rfile.read(length)
  if len(data)!=length:raise ValueError()
  if scope.get('source') in ('fetch','cancel'):result,kind=handler.server.thumbnail_fetcher.request(handler.server.agents,scope,data)
  else:result=handler.server.thumbnail_decoder.decode(handler.server.agents,scope,data);kind='keyframe'
  handler.send_response(200);handler.send_header('Content-Type','image/jpeg' if kind!='cancelled' else 'application/octet-stream');handler.send_header('X-Thumbnail-Source',kind);handler.send_header('Content-Length',str(len(result)));handler.send_header('Connection','close');handler.end_headers();handler.wfile.write(result)
 except (BrokenPipeError,ConnectionResetError):pass
 except Exception:handler.send_error(404,'Thumbnail unavailable')

class CoreArtifactPeer:
 """Only the protected local Core container may fetch signed media over its bridge."""
 def __init__(self):self.peers=set();self.until=0;self.lock=threading.Lock()
 def allows(self,peer):
  try:peer=str(ipaddress.ip_address(peer))
  except ValueError:return False
  with self.lock:
   if time.monotonic()>=self.until:
    self.peers=set()
    try:
     result=subprocess.run(['podman','inspect','--format','{"running":{{.State.Running}},"networks":{{json .NetworkSettings.Networks}}}','desk-homeassistant-core'],capture_output=True,timeout=5,check=True)
     value=json.loads(result.stdout)
     if value.get('running') is True:
      self.peers={str(ipaddress.ip_address(network['IPAddress'])) for network in value['networks'].values() if network.get('IPAddress')}
    except (OSError,subprocess.SubprocessError,ValueError,KeyError,TypeError):self.peers=set()
    self.until=time.monotonic()+5
   return peer in self.peers

class Handler(http.server.BaseHTTPRequestHandler):
 def do_POST(self):return thumbnail_request(self)
 def log_message(self,*a):pass
 def do_GET(self):
  if self.path.startswith(('/artifact/','/hls/')):
   return self.artifact()
  agents=self.server.agents;a=agents[0];status=200;body=b'{}'
  selected=next((x for x in agents if self.path=='/ready/'+x.c['workload']),None)
  if self.client_address[0] not in set(a.members.values())|{'127.0.0.1'}:status=403
  elif selected is not None:status=200 if selected.ready() else 503
  elif self.path=='/ready':status=200 if a.ready() else 503
  elif self.path=='/status':body=encoded(dict(a.public(),cameras=[x.public() for x in agents],transport=self.server.fleet.public(),thumbnail_decoder=self.server.thumbnail_decoder.public(),thumbnail_fetcher=self.server.thumbnail_fetcher.public(),voter_ready=all(clock()>=x.lease.quarantine for x in agents)))
  else:status=404
  self.send_response(status);self.send_header('Content-Type','application/json');self.send_header('Content-Length',str(len(body)));self.send_header('Connection','close');self.end_headers()
  try:self.wfile.write(body)
  except (BrokenPipeError,ConnectionResetError):pass
 def artifact(self):
  stream=None
  try:
   a=self.server.agents[0]
   parts=self.path.split('/')
   hls=parts[1]=='hls'
   if len(parts)!=(6 if hls else 4):raise ValueError()
   workload,ident=parts[2:4];p=clip_path(workload,ident)
   scope={'workload':workload,'id':ident}
   if hls:
    from hls_files import artifact_path
    generation,name=parts[4:];scope.update(generation=generation,file=name)
    p=artifact_path(p.parent,generation,name)
   token=self.headers.get('X-Desk-Artifact','')
   if len(token)>4096:raise ValueError()
   v=unpack(base64.b64decode(token,validate=True),a.c['key'])
   if v.get('recipient')!=a.node or v.get('action')!='artifact' or v.get('value')!=scope:raise ValueError()
   if self.client_address[0] not in set(a.members.values())|{'127.0.0.1'} and not self.server.core_artifact_peer.allows(self.client_address[0]):raise ValueError()
   if hls:
    selected=next(x for x in self.server.agents if x.c['workload']==workload)
    status=selected.playback.status(ident)
    if status.get('format')!='hls' or status.get('generation')!=generation or status.get('state') not in ('ready','playable'):raise ValueError()
   limit=131072 if hls and name=='index.m3u8' else 16*1024*1024 if hls else 256*1024*1024
   minimum=0 if hls else 1024
   if not p.is_file() or p.is_symlink() or not minimum<p.stat().st_size<=limit:raise ValueError()
   if hls:(clip_path(workload,ident).parent/'.access').touch()
   else:p.touch()
   stream=p.open('rb');size=os.fstat(stream.fileno()).st_size
   if not minimum<size<=limit:raise ValueError()
   start=0;end=size-1;code=200
   requested=self.headers.get('Range')
   if requested:
    match=__import__('re').fullmatch(r'bytes=(\d*)-(\d*)',requested)
    if not match or not any(match.groups()):raise ValueError()
    left,right=match.groups()
    if left:start=int(left);end=min(int(right),end) if right else end
    else:start=max(0,size-int(right))
    if not 0<=start<=end<size:raise ValueError()
    code=206
   self.send_response(code);self.send_header('Content-Type',('application/x-mpegURL' if name=='index.m3u8' else 'video/mp4') if hls else 'video/mp4');self.send_header('Accept-Ranges','bytes');self.send_header('Content-Length',str(end-start+1))
   if code==206:self.send_header('Content-Range',f'bytes {start}-{end}/{size}')
   self.send_header('Connection','close');self.end_headers()
   stream.seek(start);remaining=end-start+1
   while remaining:
    chunk=stream.read(min(65536,remaining))
    if not chunk:break
    self.wfile.write(chunk);remaining-=len(chunk)
  except (BrokenPipeError,ConnectionResetError):pass
  except Exception:
   self.send_error(404,'Recording unavailable')
  finally:
   if stream is not None:stream.close()


if __name__=='__main__':
 async def main():
  config=json.loads(pathlib.Path('/etc/desk-camera/config.json').read_bytes());telemetry=Telemetry();files=StatusFiles()
  agents=[Controller(dict(config,**camera),telemetry,files) for camera in config.get('cameras',[{}])]
  transport=SharedRPC(agents);fleet=FleetStatus(agents,transport)
  for a in agents:a.transport=transport;a.fleet=fleet
  from thumbnail_decode import Decoder
  from thumbnail_fetch import Fetcher
  server=http.server.ThreadingHTTPServer((agents[0].members[agents[0].node],20226),Handler);server.agents=agents;server.fleet=fleet;server.core_artifact_peer=CoreArtifactPeer();server.thumbnail_decoder=Decoder();server.thumbnail_fetcher=Fetcher();server.timeout=2
  threading.Thread(target=server.serve_forever,daemon=True).start()
  local_path=RUN/'bridge.sock';local_path.unlink(missing_ok=True)
  local_server=await asyncio.start_unix_server(lambda reader,writer:local_client(reader,writer,agents),path=str(local_path));os.chmod(local_path,0o600)
  async def collect_cache():
   while True:
    await asyncio.sleep(300)
    active=[clip_path(a.c['workload'],a.playback.current).parent for a in agents if a.playback.active() and a.playback.current]
    try:await asyncio.to_thread(clean_cache,active)
    except Exception:logging.warning('Recording cache cleanup deferred')
  cache_task=asyncio.create_task(collect_cache())
  def stop():
   for a in agents:a.stop.set()
  for sig in (signal.SIGINT,signal.SIGTERM):asyncio.get_running_loop().add_signal_handler(sig,stop)
  try:
   await transport.start()
   await asyncio.gather(*(a.run(i*4) for i,a in enumerate(agents)))
  finally:
   for a in agents:
    if a.playback.active():a.playback.task.cancel()
   await asyncio.gather(*(a.playback.task for a in agents if a.playback.task),return_exceptions=True)
   cache_task.cancel();await asyncio.gather(cache_task,return_exceptions=True)
   local_server.close();await local_server.wait_closed();await transport.close();server.shutdown()
 try:asyncio.run(main())
 except Exception:raise SystemExit('Camera controller stopped; private details withheld')
