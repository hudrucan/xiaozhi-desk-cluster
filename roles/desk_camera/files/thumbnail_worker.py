"""Warm, single-flight camera thumbnail runtime. Only root-local IPC; no HA Core."""
import asyncio,contextlib,io,json,os,pathlib,re,time
from contextlib import aclosing
from playback_worker import create_client,MediaSessionExpired
from recording_owner import owner_mode,create_owner_client,Reader,bind_session
from recording_jobs import RecordingJobs
from recording_contract import ContractError,response,READS
RUN=pathlib.Path('/ipc');LEASE=pathlib.Path('/lease');MAX_IMAGE=2*1024*1024

def permitted(workload,generation):
 from time import clock_gettime,CLOCK_BOOTTIME
 path=LEASE if workload=='tapo_c222_160' else LEASE/workload
 try:
  v=json.loads((path/'permit.json').read_bytes());now=clock_gettime(CLOCK_BOOTTIME)
  return v['generation']==generation and now<v['until']<=now+11
 except (OSError,ValueError,KeyError,TypeError):return False

def image(data):
 from PIL import Image
 if not isinstance(data,bytes) or not 4<len(data)<=MAX_IMAGE:raise ValueError('JPEG size')
 with Image.open(io.BytesIO(data)) as p:
  if p.format!='JPEG' or not all(0<n<=4096 for n in p.size):raise ValueError('JPEG format')
  p.verify()
 return data

class Worker:
 def __init__(self):
  from warm_playback import Media
  from observation_worker import Observations
  from command_worker import Commands
  self.observations=Observations()
  self.commands=Commands(self.observations)
  self.media=Media(self,permitted);self.current=None;self.clients={};self.tombstones={};self.completed=0;self.cancelled=0;self.failed=0;self.last=None
  self.admission=asyncio.Lock();self.reader=Reader(self)
  self.recordings=RecordingJobs(self.reader.execute,permitted,owner_authorized=lambda w,g:owner_mode(w),admission=self.admission)
 def remaining(self,workload,generation):
  if not owner_mode(workload) or not permitted(workload,generation):return 0
  for job in self.recordings.jobs.copy().values():
   request=job['request']
   if request['workload']==workload and request['generation']==generation and job['state'] in ('running','cancelling'):
    if job.get('reason'):raise ContractError(job['reason'])
    if time.time()>=request['deadline'] or time.monotonic()>=job['until']:raise ContractError('deadline_exceeded')
  path=LEASE if workload=='tapo_c222_160' else LEASE/workload
  return json.loads((path/'permit.json').read_bytes())['until']-time.clock_gettime(time.CLOCK_BOOTTIME)
 async def control(self,fn,*args,**kwargs):
  # A control HTTP call in a thread cannot be interrupted safely. Finish that
  # one call before acknowledging cancellation; never let it overlap playback.
  task=asyncio.create_task(asyncio.to_thread(fn,*args,**kwargs))
  try:return await asyncio.shield(task)
  except asyncio.CancelledError:
   with contextlib.suppress(Exception):
    result=await task
    if hasattr(result,'close'):result.close()
   raise
 async def client(self,v):
  workload=v['workload'];saved=self.clients.get(workload);owned=owner_mode(workload)
  if saved and time.monotonic()-saved[0]<600 and saved[2]==v['generation'] and (saved[3] if len(saved)>3 else False)==owned:return saved[1]
  if saved:self.close(saved[1])
  secret=json.loads(pathlib.Path('/input/credentials.json').read_bytes())[workload];password=secret['cloud_password']
  factory=(lambda *args,**kwargs:create_owner_client(lambda:self.remaining(workload,v['generation']),*args,**kwargs)) if owned else create_client
  tapo=await self.control(factory,secret['ip_address'],'admin',password,password,reuseSession=False,retryStok=False,controlPort=secret.get('control_port',443),isKLAP=False)
  self.clients[workload]=(time.monotonic(),tapo,v['generation'],owned);return tapo
 def close(self,client):
  with contextlib.suppress(Exception):client.close()
 async def preview(self,tapo,start,end):
  from pytapo.media_stream.downloader import Downloader
  from pytapo.media_stream._utils import StreamType
  request=Downloader(tapo,start,min(end,start+4),0,padding=0)._buildRequest();chunks=[];size=0;at=time.monotonic()
  session=tapo.getMediaSession(StreamType.Download);session.set_window_size(50)
  async with session:
   async with aclosing(session.transceive(json.dumps(request),no_data_timeout=4)) as stream:
    async for response in stream:
     if response.mimetype=='video/mp2t':
      part=response.plaintext
      if not isinstance(part,bytes) or size+len(part)>1024*1024:raise ValueError('Preview byte limit')
      chunks.append(part);size+=len(part)
      if size>=256*1024 or (size>=65536 and time.monotonic()-at>=2):break
     elif response.mimetype=='application/json':
      params=json.loads(response.plaintext).get('params',{})
      if params.get('error_code') not in (None,0):raise ValueError('Preview rejected')
      if params.get('event_type')=='stream_status' and params.get('status')=='finished':break
  if not size:raise ValueError('No preview')
  return b''.join(chunks)
 async def frame(self,data):
  cmd=['ffmpeg','-hide_banner','-loglevel','error','-threads','1','-probesize','1048576','-analyzeduration','1000000','-f','mpegts','-i','pipe:0','-map','0:v:0','-an','-frames:v','1','-vf','scale=320:180:force_original_aspect_ratio=decrease','-q:v','4','-threads','1','-f','image2pipe','pipe:1']
  proc=await asyncio.create_subprocess_exec(*cmd,stdin=asyncio.subprocess.PIPE,stdout=asyncio.subprocess.PIPE,stderr=asyncio.subprocess.DEVNULL)
  try:
   out,_=await asyncio.wait_for(proc.communicate(data),12)
   if proc.returncode:raise ValueError('Frame unavailable')
   return image(out)
  finally:
   if proc.returncode is None:
    with contextlib.suppress(ProcessLookupError):proc.kill()
   await proc.wait()
 async def produce(self,v):
  async with self.admission:return await self.produce_locked(v)
 async def produce_locked(self,v):
  started=time.monotonic();stage='admission';workload=v['workload'];generation=v['generation'];tapo=None
  try:
   if not permitted(workload,generation):raise ValueError('Owner changed')
   stage='connection';tapo=await self.client(v)
   # Selected cameras allocate only here; legacy cameras retain the Core handle.
   await bind_session(tapo,v['session'],self.control)
   if not permitted(workload,generation):raise ValueError('Owner changed')
   stage='events';start,end=v['clip']['start'],v['clip']['end']
   events=await self.control(tapo.executeFunction,'searchDetectionList',{'playback':{'search_detection_list':{'start_index':0,'end_index':15,'channel':0,'start_time':start,'end_time':end}}})
   stamps=sorted({int(e['start_time']) for e in events['playback']['search_detection_list'] if start<=int(e['start_time'])<end})
   data=None;kind='event'
   if stamps:
    from pytapo.media_stream.snapshot import getRecordingSnapshots
    stage='snapshot'
    try:
     async with aclosing(getRecordingSnapshots(tapo,[stamps[0]],timeout=3)) as stream:data=await anext(stream,None)
    except MediaSessionExpired:raise
    except (TimeoutError,ValueError):pass
   if data:data=image(data)
   else:
    if not permitted(workload,generation):raise ValueError('Owner changed')
    stage='preview';raw=await self.preview(tapo,start,end);stage='decode';data=await self.frame(raw);kind='keyframe'
   tapo.check_media_session()
   if not permitted(workload,generation):raise ValueError('Owner changed')
   self.completed+=1;self.last={'kind':kind,'seconds':round(time.monotonic()-started,3),'bytes':len(data),'workload':workload}
   return data,kind
  except asyncio.CancelledError:self.cancelled+=1;self.last={'state':'cancelled','stage':stage,'workload':workload};raise
  except Exception as e:
   self.failed+=1;self.last={'state':'failed','stage':stage,'error_type':type(e).__name__,'workload':workload}
   saved=self.clients.pop(workload,None)
   if saved:self.close(saved[1])
   if getattr(tapo,'_core_media_expired',False):raise MediaSessionExpired() from None
   raise
 async def watch(self,v,task):
  while not task.done():
   if not permitted(v['workload'],v['generation']):
    if not task.cancelling():task.cancel()
    return
   await asyncio.sleep(.25)
 async def cancel(self,ident):
  self.tombstones={k:t for k,t in self.tombstones.items() if time.monotonic()-t<60}
  self.tombstones[ident]=time.monotonic()
  if self.current and self.current[0]==ident:
   task=self.current[1]
   if not task.cancelling():task.cancel()
   await asyncio.gather(task,return_exceptions=True)
   if self.current and self.current[1] is task:self.current=None
 async def handle(self,reader,writer):
  data=b'';out={'ok':False}
  try:
   raw=await asyncio.wait_for(reader.readline(),2)
   if len(raw)>4096:raise ValueError('Request size')
   v=json.loads(raw);ident=v.get('id','')
   if not re.fullmatch('[a-f0-9]{32}',ident):raise ValueError('Job identity')
   if v.get('action')=='observation_v1':out={'ok':True,'value':self.observations.handle(v['request'])}
   elif v.get('action')=='command_v1':
    if ident!=v['request']['request_id']:raise ValueError('Command ticket binding')
    out={'ok':True,'value':await self.commands.handle(v['sender'],v['request'])}
   elif v.get('action')=='recording_v1':
    request=v['request']
    if ident!=request['request_id']:raise ValueError('Ticket binding')
    known=(v['sender'],request['workload'],request['generation'],ident) in self.recordings.jobs
    if request['operation'] in READS and not known and (self.current is not None or self.media.busy()):result=response(request,'failed',reason='camera_busy')
    else:result=self.recordings.handle(v['sender'],request)
    out={'ok':True,'value':result}
   elif v.get('action','').startswith('playback_'):out={'ok':True,'value':await self.media.action(v)}
   elif v.get('action')=='cancel':await self.cancel(ident);out={'ok':True}
   elif v.get('action')=='status':out={'ok':True,'value':{'completed':self.completed,'cancelled':self.cancelled,'failed':self.failed,'last':self.last,'busy':self.current is not None,'observations':self.observations.status(),'commands':self.commands.status(),'media':self.media.status(),'recordings':{'states':{state:sum(j['state']==state for j in self.recordings.jobs.values()) for state in ('queued','running','cancelling','done','failed','cancelled')},'owner_allocations':{w:getattr(saved[1],'_owner_allocations',0) for w,saved in self.clients.items()}}}}
   elif v.get('action')=='fetch':
    if self.current is not None or self.media.busy() or ident in self.tombstones:raise ValueError('Busy or cancelled')
    task=asyncio.create_task(self.produce(v));self.current=(ident,task);watch=asyncio.create_task(self.watch(v,task))
    try:
     data,kind=await asyncio.wait_for(asyncio.shield(task),32);out={'ok':True,'kind':kind,'bytes':len(data)}
    finally:
     if not task.done() and not task.cancelling():task.cancel()
     await asyncio.gather(task,return_exceptions=True);watch.cancel();await asyncio.gather(watch,return_exceptions=True)
     if self.current and self.current[1] is task:self.current=None
   else:raise ValueError('Action')
  except MediaSessionExpired:out={'ok':False,'reason':'session_expired'}
  except (Exception,asyncio.CancelledError):pass
  try:writer.write(json.dumps(out).encode()+b'\n'+data);await writer.drain()
  except (ConnectionError,BrokenPipeError):pass
  finally:writer.close();await writer.wait_closed()
 async def run(self):
  # Load SDK/decoder modules before readiness; imports must never block a
  # running job's cancellation handler. This performs no camera login.
  def preload():
   from pytapo import Tapo
   from pytapo.media_stream.snapshot import getRecordingSnapshots
   from PIL import Image
   from pytapo.media_stream.downloader import Downloader
   import hls_worker
  await asyncio.to_thread(preload)
  self.observations.start()
  path=RUN/'worker.sock';path.unlink(missing_ok=True)
  server=await asyncio.start_unix_server(self.handle,path=str(path),limit=8192);path.chmod(0o600)
  try:
   async with server:await server.serve_forever()
  finally:
   await self.commands.stop();await self.observations.stop();await self.media.stop();await self.recordings.close()
   for saved in self.clients.values():self.close(saved[1])
if __name__=='__main__':asyncio.run(Worker().run())
