"""Camera placement over the existing majority lease; no media in NATS."""
import asyncio,datetime,http.server,json,logging,math,os,pathlib,secrets,signal,sys,threading,time,urllib.request
sys.path.insert(0,'/usr/local/libexec/desk-homeassistant')
from transport import SharedRPC,FleetStatus
from lease import Lease,clock
from config_journal import atomic,encoded
from protocol import pack,unpack,MAX

ROOT=pathlib.Path('/var/lib/desk-camera');RUN=pathlib.Path('/run/desk-camera')
PREFIX='desk.ha.media'

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
 def __init__(self,c,telemetry=None):
  self.c=c;self.node=c['node'];self.members=c['members'];self.root,self.run_dir=paths(c);self.root.mkdir(parents=True,exist_ok=True,mode=0o700);self.run_dir.mkdir(parents=True,exist_ok=True,mode=0o700);self.lease=Lease(self.root/'vote.json')
  self.prefix=PREFIX if c['workload']=='tapo_c222_160' else PREFIX+'.'+c['workload'];self.telemetry=telemetry or Telemetry()
  self.until=0;self.term=None;self.generation=None;self.transport=None;self.fleet=None;self.stop=asyncio.Event();self.nonces={};self.heads={};self.local=None;self.local_at=0;self.started_at=0;self.cooldown=0
  self.opener=urllib.request.build_opener(urllib.request.ProxyHandler({}))
 def owned(self):return self.term is not None and clock()<self.until
 def eligible(self):
  return clock()>=self.cooldown and self.node in self.c['enabled_nodes'] and not (self.root/'drain').exists() and self.local is not None and clock()-self.local_at<15 and self.local.get('online') is True and self.local.get('memory_available_mib',0)>=256 and not any(self.local.get('admission',{}).values())
 def ready(self):
  if not self.owned():return False
  try:
   v=json.loads((self.run_dir/'runtime.json').read_bytes())
   return self.owned() and v.get('generation')==self.generation and v.get('ready') is True and time.time()-v['at']<4
  except Exception:return False
 def placement_score(self):
  # Prefer a free relay slot; retain a healthy owner instead of constantly rebalancing.
  slots=0
  for p in [RUN/'permit.json',*RUN.glob('*/permit.json')]:
   if p.parent==self.run_dir:continue
   try:
    v=json.loads(p.read_bytes())
    if v.get('generation') and clock()<v['until']<=clock()+11:slots+=1
   except Exception:pass
  return (self.local.get('cpu',100) if self.local else 100)+100*slots
 def status(self):
  return {'node':self.node,'lease':self.lease.status(),'owned':self.owned(),'eligible':self.eligible(),'score':self.placement_score(),'definition':self.c['definition'],'ready':self.ready()}
 def public(self):
  owners=[n for n,h in self.heads.items() if h['owned'] and h['definition']==self.c['definition']]
  owner=owners[0] if len(owners)==1 else None
  return {'node':self.node,'workload':self.c['workload'],'owner':owner,'ready':bool(owner and self.heads[owner]['ready']),'local_ready':self.ready(),'voter_ready':clock()>=self.lease.quarantine,'observed_at':time.time()}
 def refresh(self):self.telemetry.refresh(self)
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

class Handler(http.server.BaseHTTPRequestHandler):
 def log_message(self,*a):pass
 def do_GET(self):
  agents=self.server.agents;a=agents[0];status=200;body=b'{}'
  selected=next((x for x in agents if self.path=='/ready/'+x.c['workload']),None)
  if self.client_address[0] not in set(a.members.values())|{'127.0.0.1'}:status=403
  elif selected is not None:status=200 if selected.ready() else 503
  elif self.path=='/ready':status=200 if a.ready() else 503
  elif self.path=='/status':body=encoded(dict(a.public(),cameras=[x.public() for x in agents],transport=self.server.fleet.public(),voter_ready=all(clock()>=x.lease.quarantine for x in agents)))
  else:status=404
  self.send_response(status);self.send_header('Content-Type','application/json');self.send_header('Content-Length',str(len(body)));self.send_header('Connection','close');self.end_headers()
  try:self.wfile.write(body)
  except (BrokenPipeError,ConnectionResetError):pass

if __name__=='__main__':
 async def main():
  config=json.loads(pathlib.Path('/etc/desk-camera/config.json').read_bytes());telemetry=Telemetry()
  agents=[Controller(dict(config,**camera),telemetry) for camera in config.get('cameras',[{}])]
  transport=SharedRPC(agents);fleet=FleetStatus(agents,transport)
  for a in agents:a.transport=transport;a.fleet=fleet
  server=http.server.HTTPServer((agents[0].members[agents[0].node],20226),Handler);server.agents=agents;server.fleet=fleet;server.timeout=2
  threading.Thread(target=server.serve_forever,daemon=True).start()
  def stop():
   for a in agents:a.stop.set()
  for sig in (signal.SIGINT,signal.SIGTERM):asyncio.get_running_loop().add_signal_handler(sig,stop)
  try:
   await transport.start()
   await asyncio.gather(*(a.run(i*4) for i,a in enumerate(agents)))
  finally:await transport.close();server.shutdown()
 try:asyncio.run(main())
 except Exception:raise SystemExit('Camera controller stopped; private details withheld')
