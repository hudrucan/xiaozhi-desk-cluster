#!/usr/bin/env python3
"""Owner-routed LAN mDNS and ARP jobs. No database, arbitrary commands, or persisted queue."""
import asyncio,datetime,json,math,os,pathlib,secrets,socket,time,urllib.request
import nats
from arp import scan as arp_scan
from mdns import resolve,interface_address,save_json
from scan_protocol import NODES,MAX_BYTES,Unavailable,encoded,pack,unpack,validate_job,validate_result

class Service:
 def __init__(self,c):
  self.c=c;self.node=c['node'];self.key=c['key'];self.bus=None;self.worker_bus=None
  self.scan_states={};self.busy=False;self.dispatch_busy=False;self.seen={};self.tasks=set();self.errors=0
  self.opener=urllib.request.build_opener(urllib.request.ProxyHandler({}))
 def fetch(self,url):
  with self.opener.open(url,timeout=.7) as r:
   raw=r.read(MAX_BYTES+1)
   if len(raw)>MAX_BYTES:raise ValueError('Metadata too large')
   return json.loads(raw)
 async def owner(self,node,term=None):
  if node not in self.c['members']:raise Unavailable('Unknown owner')
  v=await asyncio.to_thread(self.fetch,'http://'+self.c['members'][node]+':20213/health')
  if v.get('state')!='active' or v.get('node')!=node or term is not None and v.get('lease_term')!=term:raise Unavailable('Owner lease no longer current')
  return v['lease_term']
 async def telemetry(self,node):
  v=await asyncio.to_thread(self.fetch,'http://'+self.c['members'][node]+':8010/local')
  at=datetime.datetime.fromisoformat(v['observed_at'].replace('Z','+00:00')).timestamp()
  cpu=v.get('cpu');admission=v.get('admission');activity=v.get('activity')
  if v.get('id')!=node or v.get('online') is not True or not -3<=time.time()-at<15 or type(cpu) not in (int,float) or not math.isfinite(cpu) or not 0<=cpu<=100:raise Unavailable('Stale telemetry')
  if not isinstance(admission,dict) or not all(admission.get(k) is False for k in ('worker','core','mqtt')):raise Unavailable('Maintenance or missing admission')
  if not isinstance(activity,dict) or not all(type(activity.get(k)) is int and activity[k]==0 for k in ('asr','llm','tts')):raise Unavailable('Voice worker active')
  return v
 async def request(self,node,value,kind,timeout):
  inbox='desk.network.reply.'+self.node+'.'+secrets.token_hex(16)
  future=asyncio.get_running_loop().create_future()
  async def received(msg):
   if not future.done():
    try:future.set_result(unpack(msg.data,self.key))
    except Exception:future.set_exception(Unavailable('Invalid signed reply'))
  sub=await self.bus.subscribe(inbox,cb=received,pending_msgs_limit=1,pending_bytes_limit=MAX_BYTES+256)
  try:
   await self.bus.flush(timeout=.5)
   await self.bus.publish('desk.network.'+kind+'.'+node,pack(value,self.key),reply=inbox)
   return await asyncio.wait_for(future,timeout)
  finally:await sub.unsubscribe()
 async def status(self,msg):
  try:
   value=unpack(msg.data,self.key);node=value['owner']
   if msg.reply.split('.')[:4]!=['desk','network','reply',node] or node not in NODES:return
   await msg.respond(pack({'id':value['id'],'node':self.node,'busy':self.busy,'plugins':['mdns','arp'],'at':time.time()},self.key))
  except Exception:pass
 async def execute(self,msg):
  job=None
  try:
   job=validate_job(unpack(msg.data,self.key),self.node)
   if msg.reply.split('.')[:4]!=['desk','network','reply',job['owner']]:return
   now=time.time();self.seen={k:v for k,v in self.seen.items() if v>now}
   if self.busy or len(self.seen)>=128 or job['id'] in self.seen:raise Unavailable('Worker busy or duplicate')
   # Mark busy before awaits, so different callbacks cannot overlap scans.
   self.busy=True;self.seen[job['id']]=job['deadline']+30
   try:
    await self.owner(job['owner'],job['term']);await self.telemetry(self.node)
    if job['deadline']-time.time()<(13 if job['plugin']=='arp' else 5.3):raise Unavailable('Too little job budget')
    started=time.monotonic();cpu=time.process_time()
    found,stats=await asyncio.to_thread(arp_scan) if job['plugin']=='arp' else await asyncio.to_thread(resolve,job['targets'],interface_address())
    await self.owner(job['owner'],job['term']);await self.telemetry(self.node)
    if time.time()>=job['deadline']:raise Unavailable('Deadline elapsed')
    stats.update(plugin=job['plugin'],wall_seconds=round(time.monotonic()-started,3),cpu_seconds=round(time.process_time()-cpu,4))
    result={'id':job['id'],'term':job['term'],'worker':self.node,'state':'ready','found':found,'stats':stats}
    save_json(pathlib.Path('/run/desk-network-scan/last-worker.json'),dict(stats,job_id=job['id'],owner=job['owner'],worker=self.node))
    await msg.respond(pack(result,self.key))
   finally:self.busy=False
  except Exception:
   if job:
    try:await msg.respond(pack({'id':job['id'],'term':job['term'],'worker':self.node,'state':'unavailable'},self.key))
    except Exception:pass
 def enqueue(self,msg):
  if len(self.tasks)>=4:return
  task=asyncio.create_task(self.execute(msg));self.tasks.add(task);task.add_done_callback(self.tasks.discard)
 async def worker_status(self,node,plugin="mdns"):
  ident=secrets.token_hex(16)
  t,v=await asyncio.gather(self.telemetry(node),self.request(node,{'id':ident,'owner':self.node},'status',.8))
  if v.get('id')!=ident or v.get('node')!=node or v.get('busy') is not False or plugin not in v.get('plugins',[]) or type(v.get('at')) not in (int,float) or not -2<time.time()-v['at']<3:raise Unavailable('Worker unavailable')
  # Prefer offloading; voice activity and maintenance have already been excluded.
  score=t['cpu']+sum(cost for k,cost in {'xiaozhi':20,'dns':10,'vpn':5}.items() if t.get('vip',{}).get(k))+(15 if node==self.node else 0)
  return score,node
 def record(self,plugin,term,phase,**values):
  previous=self.scan_states.get(plugin,{})
  state={'last_success_at':previous['last_success_at']} if 'last_success_at' in previous else {}
  if phase!='selecting':state.update(previous)
  if phase in ('running','completed'):state.pop('reason',None)
  now=time.time();state.update(phase=phase,at=now,**values)
  if phase=='selecting':state['started_at']=now
  if phase=='completed':state.update(finished_at=now,last_success_at=now)
  self.scan_states[plugin]=state
  try:save_json(pathlib.Path('/run/desk-network-scan/owner-state.json'),{'owner':self.node,'term':term,'scans':self.scan_states})
  except OSError:pass # Telemetry storage must never fail an otherwise valid scan.
 async def dispatch(self,targets,plugin="mdns"):
  term=await self.owner(self.node)
  self.record(plugin,term,'selecting')
  replies=await asyncio.gather(*(self.worker_status(n,plugin) for n in NODES),return_exceptions=True)
  workers=sorted(v for v in replies if isinstance(v,tuple))
  if not workers:
   self.record(plugin,term,'failed',reason='no_eligible_workers');raise Unavailable('no_eligible_workers')
  for _,node in workers[:2]:
   await self.owner(self.node,term)
   job={'id':secrets.token_hex(16),'owner':self.node,'term':term,'worker':node,'plugin':plugin,'targets':targets,'deadline':time.time()+(18 if plugin=='arp' else 8)}
   validate_job(job,node)
   self.record(plugin,term,'running',worker=node,job_id=job['id'])
   try:
    result=await self.request(node,job,'scan',18.1 if plugin=='arp' else 8.1)
    await self.owner(self.node,term)
    found,stats=validate_result(result,job)
    save_json(pathlib.Path('/run/desk-network-scan/last-dispatch.json'),dict(stats,owner=self.node,term=term,targets=len(targets)))
    self.record(plugin,term,'completed',**{k:v for k,v in stats.items() if k in ('worker','job_id','wall_seconds','scan_seconds','received','resolved')})
    return {'state':'ready','found':found,'stats':stats}
   except Exception:
    # A busy/rejected worker can reply immediately; a timed-out attempt gets
    # no concurrent retry before its deadline. No local hidden fallback.
    self.record(plugin,term,'retrying',worker=node,reason='workers_unavailable')
    remaining=job['deadline']-time.time()
    if remaining>0:await asyncio.sleep(remaining)
  self.record(plugin,term,'failed',reason='workers_unavailable')
  raise Unavailable('workers_unavailable')
 async def local(self,reader,writer):
  accepted=False
  try:
   raw=await asyncio.wait_for(reader.readline(),1)
   if len(raw)>MAX_BYTES:raise ValueError('Local request too large')
   value=json.loads(raw)
   if value=={'health':True}:
    response={'state':'ready' if self.bus.is_connected and self.worker_bus.is_connected else 'unavailable'}
   else:
    if self.dispatch_busy:raise Unavailable('dispatcher_busy')
    self.dispatch_busy=True;accepted=True
    if set(value) not in ({'targets'},{'targets','plugin'}):raise ValueError('Invalid local request')
    plugin=value.get('plugin','mdns')
    # Reuse strict validation before telemetry or NATS publication.
    validate_job({'id':'0'*32,'owner':self.node,'term':[1,self.node],'worker':self.node,'plugin':plugin,'deadline':time.time()+(18 if plugin=='arp' else 8),'targets':value['targets']},self.node)
    response=await asyncio.wait_for(self.dispatch(value['targets'],plugin),43 if plugin=='arp' else 21)
  except Exception as error:
   reason=str(error) if isinstance(error,Unavailable) else 'transport_unavailable'
   if reason not in ('dispatcher_busy','no_eligible_workers','workers_unavailable'):reason='owner_unavailable' if isinstance(error,Unavailable) else 'transport_unavailable'
   response={'state':'unavailable','reason':reason}
  finally:
   if accepted:self.dispatch_busy=False
  try:writer.write(encoded(response)+b'\n');await writer.drain()
  finally:writer.close();await writer.wait_closed()
 async def start(self):
  async def error(_):self.errors+=1  # Do not log server exception text/credentials.
  kwargs=dict(servers=self.c['servers'],connect_timeout=1,reconnect_time_wait=1,max_reconnect_attempts=-1,error_cb=error,ping_interval=10,max_outstanding_pings=2)
  self.bus=await nats.connect(user=self.c['coordinator']['user'],password=self.c['coordinator']['password'],**kwargs)
  self.worker_bus=await nats.connect(user=self.c['worker']['user'],password=self.c['worker']['password'],**kwargs)
  async def scan(msg):self.enqueue(msg)
  await self.worker_bus.subscribe('desk.network.scan.'+self.node,cb=scan,pending_msgs_limit=4,pending_bytes_limit=131072)
  await self.worker_bus.subscribe('desk.network.status.'+self.node,cb=self.status,pending_msgs_limit=8,pending_bytes_limit=65536)
  await self.worker_bus.flush(timeout=1)
  path='/run/desk-network-scan/dispatch.sock';pathlib.Path(path).unlink(missing_ok=True)
  server=await asyncio.start_unix_server(self.local,path=path,limit=MAX_BYTES+1);os.chmod(path,0o660)
  async with server:await server.serve_forever()

async def main():
 c=json.loads((pathlib.Path(os.environ['CREDENTIALS_DIRECTORY'])/'config.json').read_text())
 service=Service(c)
 try:await service.start()
 except Exception:raise SystemExit('Network scan transport unavailable')
if __name__=='__main__':asyncio.run(main())
