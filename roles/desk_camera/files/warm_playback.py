"""One lease-fenced playback job in the existing warm thumbnail process.

Only root-local metadata IPC. No media prefetch, no HA runtime, no second camera
owner. The process preloads SDK code; camera login remains owner/job-bound.
"""
import asyncio,contextlib,json,os,pathlib,re,time
from playback import validate,identity,session_handle
from playback_worker import execute
ROOT=pathlib.Path('/output')
class Media:
 def __init__(self,worker,permitted):
  self.worker=worker;self.permitted=permitted;self.current=None;self.tombstones={};self.completed=0;self.cancelled=0;self.failed=0;self.last=None
 def busy(self):return bool(self.current and not self.current['task'].done())
 def status(self):return {'busy':self.busy(),'completed':self.completed,'cancelled':self.cancelled,'failed':self.failed,'last':self.last}
 def binding(self,v):
  if set(v)!={'action','id','attempt'} or not all(isinstance(v[k],str) and re.fullmatch('[a-f0-9]{32}',v[k]) for k in ('id','attempt')):raise ValueError('Media attempt')
  c=self.current
  return c if c and (c['id'],c['attempt'])==(v['id'],v['attempt']) else None
 async def action(self,v):
  if v['action']=='playback_start':return self.start(v)
  c=self.binding(v)
  if v['action']=='playback_cancel':
   self.tombstones={k:t for k,t in self.tombstones.items() if time.monotonic()-t<60}
   if len(self.tombstones)>=128:self.tombstones.pop(next(iter(self.tombstones)))
   self.tombstones[v['attempt']]=time.monotonic()
   if c and not c['task'].done():
    if not c['task'].cancelling():c['stop_reason']='controller_cancel';c['task'].cancel()
    await asyncio.gather(c['task'],return_exceptions=True)
   return {'state':'cancelled'}
  if v['action']!='playback_tick':raise ValueError('Media action')
  if not c:return {'state':'missing'}
  if not c['task'].done():
   if c['task'].cancelling() or time.monotonic()>c['until']:return {'state':'missing'}
   c['until']=time.monotonic()+8;return {'state':'active'}
  return {'state':'finished'}
 def start(self,v):
  if set(v)!={'action','id','attempt','workload','generation','clip','session'}:raise ValueError('Media request')
  if not all(isinstance(v[k],str) and re.fullmatch('[a-f0-9]{32}',v[k]) for k in ('id','attempt','generation')):raise ValueError('Media identity')
  job=validate(v['workload'],v['clip']);session=session_handle(v['session'])
  if identity(job)!=v['id'] or v['attempt'] in self.tombstones:raise ValueError('Stale media request')
  if self.busy():
   c=self.current
   if (c['id'],c['attempt'])==(v['id'],v['attempt']):return {'state':'active'}
   raise ValueError('Media busy')
  if not self.permitted(v['workload'],v['generation']):raise ValueError('Owner changed')
  path=ROOT/v['workload']/v['id']
  if any(p.is_symlink() for p in (ROOT,path.parent,path)):raise ValueError('Media path')
  path.mkdir(parents=True,exist_ok=True,mode=0o700)
  options_path=path/'options.json'
  if options_path.is_symlink() or options_path.stat().st_size>1024:raise ValueError('Media options')
  options=json.loads(options_path.read_bytes())
  if options not in ({'format':'mp4'},) and not (set(options)=={'format','generation'} and options['format']=='hls' and isinstance(options['generation'],str) and re.fullmatch('[a-f0-9]{32}',options['generation'])):raise ValueError('Media format')
  c={k:v[k] for k in ('id','attempt','workload','generation')};c.update(until=time.monotonic()+8,started=time.monotonic())
  c['task']=asyncio.create_task(self.run(c,job,session,path,options));self.current=c
  return {'state':'active'}
 async def watch(self,c):
  while not c['task'].done():
   if time.monotonic()>c['until'] or not self.permitted(c['workload'],c['generation']):
    if not c['task'].cancelling():c['stop_reason']='controller_heartbeat_expired' if time.monotonic()>c['until'] else 'owner_lost';c['task'].cancel()
    return
   await asyncio.sleep(.25)
 async def run(self,c,job,session,path,options):
  watch=asyncio.create_task(self.watch(c));stage='thumbnail_cleanup'
  def save(value):
   nonlocal stage
   stage=value.get('stage',stage);value=dict(value,runtime='warm',warm_elapsed_seconds=round(time.monotonic()-c['started'],3))
   tmp=path/'progress.tmp';tmp.write_text(json.dumps(value));os.replace(tmp,path/'progress.json')
  try:
   # Playback preempts the node's thumbnail request, including its actual SDK
   # cleanup. No shared camera client may be mutated before that acknowledgement.
   if self.worker.current:await self.worker.cancel(self.worker.current[0])
   save({'state':'preparing','stage':'camera_connection'})
   tapo=await self.worker.client(c)
   if not self.permitted(c['workload'],c['generation']):raise ValueError('Owner changed')
   await asyncio.wait_for(execute(tapo,job,session,path,options,save,self.worker.control,lambda:self.permitted(c['workload'],c['generation'])),max(190,job['end']-job['start']+120))
   self.completed+=1;self.last={'state':'ready','seconds':round(time.monotonic()-c['started'],3),'workload':c['workload']}
  except asyncio.CancelledError:
   self.cancelled+=1;save({'state':'failed','stage':stage,'stop_reason':c.get('stop_reason','shutdown'),'error':'Playback closed or ownership changed.'});self.last={'state':'cancelled','reason':c.get('stop_reason','shutdown'),'workload':c['workload']}
   raise
  except Exception as e:
   self.failed+=1;save({'state':'failed','stage':stage,'error_type':type(e).__name__,'error':'Recording preparation failed; retry when the worker is ready.'});self.last={'state':'failed','stage':stage,'error_type':type(e).__name__,'workload':c['workload']}
   saved=self.worker.clients.pop(c['workload'],None)
   if saved:self.worker.close(saved[1])
  finally:
   watch.cancel();await asyncio.gather(watch,return_exceptions=True)
   (path/'clip.mp4').unlink(missing_ok=True)
 async def stop(self):
  if self.busy():
   task=self.current['task']
   if not task.cancelling():task.cancel()
   await asyncio.gather(task,return_exceptions=True)
