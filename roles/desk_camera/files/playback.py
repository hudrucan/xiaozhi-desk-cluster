"""Per-camera, lease-bound playback jobs. NATS carries fixed metadata only."""
import asyncio,hashlib,secrets,json,os,pathlib,re,shutil,time
ROOT=pathlib.Path('/var/lib/desk-camera/playback');LIMIT=256*1024*1024
class PlaybackError(ValueError):
 def __init__(self,code):self.code=code;super().__init__(code)
def session_handle(value):
 if not isinstance(value,dict) or set(value)!={'user_id','player_id'}:raise PlaybackError('session_missing')
 uid=value['user_id'];player=value['player_id']
 if not ((type(uid) is int and 0<=uid<2**63) or (isinstance(uid,str) and re.fullmatch(r'[A-Za-z0-9_-]{1,128}',uid))):raise PlaybackError('session_missing')
 if not isinstance(player,str) or not re.fullmatch(r'[A-Za-z0-9_-]{1,128}',player):raise PlaybackError('session_missing')
 return dict(value)
IMAGE='ghcr.io/home-assistant/home-assistant@sha256:17100609af8bb79bf5390f5d514b9aaeb00d184ce758f37769cd80c450851db7'
def validate(workload,value):
 if workload not in {'tapo_c222_160','tapo_c222_161','tapo_c222_163'}:raise ValueError('This camera has no recordings')
 if set(value)!={'date','start','end'} or not re.fullmatch(r'\d{8}',value.get('date','')):raise ValueError('Invalid clip')
 start,end=value['start'],value['end']
 if type(start) is not int or type(end) is not int or not 1500000000<=start<end<=2200000000 or not 1<=end-start<=1800:raise ValueError('Clip must be at most thirty minutes')
 import datetime
 day=datetime.datetime.strptime(value['date'],'%Y%m%d').replace(tzinfo=datetime.timezone.utc).timestamp()
 if not day-86400<=start<=day+172800:raise ValueError('Clip date mismatch')
 return dict(value,workload=workload)
def identity(job):return hashlib.sha256(json.dumps(job,sort_keys=True,separators=(',',':')).encode()).hexdigest()[:32]
def clip_path(workload,ident):
 if workload not in {'tapo_c222_160','tapo_c222_161','tapo_c222_163'} or not re.fullmatch('[a-f0-9]{32}',ident):raise ValueError('Invalid clip identity')
 return ROOT/workload/ident/'ready.mp4'
class Playback:
 def __init__(self,agent):self.agent=agent;self.task=None;self.current=None;self.hls_cache={};self.attempt=None;self.viewer_until=None;self.stop_requested=False
 def active(self):return bool(self.task and not self.task.done())
 def viewer_action(self,ident,attempt,close=False):
  clip_path(self.agent.c['workload'],ident)
  if not isinstance(attempt,str) or not re.fullmatch('[a-f0-9]{32}',attempt):raise ValueError('Invalid playback attempt')
  if ident!=self.current or attempt!=self.attempt or not self.active():return {'state':'cancelled' if close else 'missing'}
  if self.viewer_until is None:raise ValueError('Unmanaged playback')
  if close:
   self.stop_requested=True;return {'state':'cancelling'}
  if self.stop_requested or time.monotonic()>self.viewer_until:return {'state':'missing'}
  self.viewer_until=time.monotonic()+30;return {'state':'active'}

 def hls_snapshot(self,ident,base,generation):
  # Segment requests arrive concurrently. Reuse validation for at most one second;
  # a new atomic playlist immediately invalidates it. HTTP still authenticates,
  # validates and opens each requested file separately before sending headers.
  from hls_files import artifact_path,snapshot
  index=artifact_path(base,generation,'index.m3u8');stamp=index.stat()
  fingerprint=(generation,stamp.st_ino,stamp.st_mtime_ns,stamp.st_size)
  now=time.monotonic();cached=self.hls_cache.get(ident)
  if cached and cached[0]==fingerprint and now-cached[1]<1:return cached[2]
  value=snapshot(base,generation)
  if len(self.hls_cache)>=128:self.hls_cache.clear()
  self.hls_cache[ident]=(fingerprint,now,value)
  return value
 def status(self,ident):
  value=self._status(ident)
  if self.current==ident and self.active() and self.viewer_until is not None:value=dict(value,attempt=self.attempt)
  return value
 def _status(self,ident):
  p=clip_path(self.agent.c['workload'],ident)
  if p.is_file() and not p.is_symlink() and 1024<p.stat().st_size<=LIMIT:return {'id':ident,'node':self.agent.node,'state':'ready','bytes':p.stat().st_size}
  try:
   v=json.loads((p.parent/'progress.json').read_bytes())
   if v.get('format')=='hls' and v.get('state') in ('playable','ready'):
    h=self.hls_snapshot(ident,p.parent,v['generation'])
    running=self.current==ident and self.active()
    if (v['state']=='ready' and h['complete']) or (v['state']=='playable' and running):
     return dict(v,id=ident,node=self.agent.node,bytes=h['bytes'])
  except (OSError,ValueError,KeyError,TypeError):pass
  value={'id':ident,'node':self.agent.node,'state':'preparing' if self.current==ident and self.active() else 'missing'}
  if self.current==ident:
   try:
    v=json.loads((p.parent/'progress.json').read_bytes())
    value.update({k:v[k] for k in ('state','progress','total','error','format','generation') if k in v})
    if v.get('reason')=='session_expired':
     # Core may invalidate/reallocate only after the exact attempt has drained.
     if self.active():value.update(state='preparing');value.pop('error',None)
     else:value['reason']='session_expired'
   except Exception:pass
   if value.get('format')=='hls' and value['state'] in ('playable','ready'):value['state']='preparing' if self.active() else 'failed'
   if not self.active() and value['state'] in ('missing','preparing','playable','ready'):value.update(state='failed',error='Worker changed or preparation stopped; retry this clip.')
  return value
 def cached(self,value):
  job=validate(self.agent.c['workload'],value);ident=identity(job);state=self.status(ident)
  if state['state'] in ('ready','playable'):return state
  name={'tapo_c222_160':'bed','tapo_c222_161':'cam161','tapo_c222_163':'cam163'}[job['workload']]
  legacy=ROOT.parent/'cache/cold'/name/'videos'/f"{job['start']}-{job['end']}.mp4"
  # Adopt only a completed, stable native cache file; never copy or download SD.
  if legacy.is_file() and not legacy.is_symlink() and 1024<legacy.stat().st_size<=LIMIT and time.time()-legacy.stat().st_mtime>60:
   target=clip_path(job['workload'],ident);target.parent.mkdir(parents=True,exist_ok=True,mode=0o700)
   try:os.link(legacy,target)
   except FileExistsError:pass
   request=target.parent/'request.json'
   if not request.exists():request.write_text(json.dumps(job));request.chmod(0o600)
   return self.status(ident)
  return {'state':'missing'}
 def inventory(self):
  items=[];root=ROOT/self.agent.c['workload']
  if root.is_dir():
   for p in sorted(root.glob('*/request.json'),key=lambda p:p.stat().st_mtime,reverse=True)[:128]:
    try:
     if p.is_symlink() or p.parent.is_symlink() or p.stat().st_size>1024:continue
     job=json.loads(p.read_bytes());job=validate(self.agent.c['workload'],{k:job[k] for k in ('date','start','end')})
     ident=identity(job)
     if p.parent.name!=ident:continue
     state=self.status(ident)
     if state['state'] in ('ready','preparing','playable'):items.append(dict(job,**{k:state[k] for k in ('format','generation','attempt') if k in state},id=ident,node=self.agent.node,state=state['state']))
    except (OSError,ValueError,TypeError,KeyError):continue
  name={'tapo_c222_160':'bed','tapo_c222_161':'cam161','tapo_c222_163':'cam163'}[self.agent.c['workload']]
  for p in (ROOT.parent/'cache/cold'/name/'videos').glob('*.mp4'):
   try:
    match=re.fullmatch(r'(\d+)-(\d+)\.mp4',p.name)
    if not match or p.is_symlink() or not 1024<p.stat().st_size<=LIMIT or time.time()-p.stat().st_mtime<=60:continue
    start,end=map(int,match.groups())
    if not 1<=end-start<=1800:continue
    items.append({'workload':self.agent.c['workload'],'node':self.agent.node,'start':start,'end':end,'legacy':True,'state':'ready'})
   except (OSError,ValueError):continue
  return {'items':items[:128],'truncated':len(items)>128}
 def prepare(self,value,session=None,managed=False):
  if type(managed) is not bool:raise ValueError('Invalid viewer mode')
  job=validate(self.agent.c['workload'],value);ident=identity(job);status=self.status(ident)
  if status['state'] in ('ready','playable'):return status
  if self.active():
   if self.current!=ident:raise PlaybackError('camera_busy')
   return status
  if not self.agent.owned() or not self.agent.ready() or not self.agent.eligible():raise PlaybackError('worker_not_ready')
  if self.agent.local.get('memory_available_mib',0)<1200:raise PlaybackError('memory_low')
  from recording_owner import owner_mode
  if owner_mode(self.agent.c['workload']):
   if session is not None:raise PlaybackError('session_missing')
  else:session=session_handle(session)
  if shutil.disk_usage(ROOT if ROOT.exists() else ROOT.parent).free<512*1024*1024:raise PlaybackError('disk_low')
  self.current=ident;self.attempt=secrets.token_hex(16);self.stop_requested=False;self.viewer_until=time.monotonic()+30 if managed else None
  self.task=asyncio.create_task(self.run(job,ident,self.agent.generation,session));return dict(id=ident,node=self.agent.node,state='preparing',**({'attempt':self.attempt} if managed else {}))
 async def run(self,job,ident,generation,session):
  path=clip_path(job['workload'],ident).parent;path.mkdir(parents=True,exist_ok=True,mode=0o700)
  # Only our completed old jobs; never evict an in-flight clip or arbitrary path.
  for old in path.parent.iterdir():
   if old!=path and re.fullmatch('[a-f0-9]{32}',old.name) and not old.is_symlink() and time.time()-max([old.stat().st_mtime]+[p.stat().st_mtime for p in (old/'.access',old/'ready.mp4') if p.is_file() and not p.is_symlink()])>3600:shutil.rmtree(old)
  completed=sorted((p for p in ROOT.glob('*/*/ready.mp4') if not p.is_symlink()),key=lambda p:p.stat().st_mtime)
  total=sum(p.stat().st_size for p in completed)
  for p in completed:
   if total<=768*1024*1024:break
   total-=p.stat().st_size;p.unlink()
  if shutil.disk_usage(path).free<512*1024*1024:return
  (path/'request.json').write_text(json.dumps(job));(path/'request.json').chmod(0o600)
  options={'format':'hls','generation':secrets.token_hex(32)[:32]} if self.agent.c.get('progressive') is True else {'format':'mp4'}
  (path/'options.json').write_text(json.dumps(options))
  (path/'progress.json').write_text('{"state":"preparing"}');(path/'clip.mp4').unlink(missing_ok=True)
  # Reuse the root-local preloaded SDK process. Only ephemeral metadata crosses
  # IPC; no user handle is persisted and no per-click container is started.
  from media_rpc import request
  binding={'id':ident,'attempt':self.attempt};started=False;complete=False
  try:
   result=await request(dict(binding,action='playback_start',workload=job['workload'],generation=generation,clip={k:job[k] for k in ('date','start','end')},session=session))
   if result.get('state')!='active':raise ValueError('Warm worker unavailable')
   started=True;deadline=time.monotonic()+max(190,job['end']-job['start']+120)
   while True:
    if self.stop_requested or not self.agent.owned() or self.agent.generation!=generation or time.monotonic()>deadline or (self.viewer_until is not None and time.monotonic()>self.viewer_until):raise ValueError('Playback lease expired')
    status=await request(dict(binding,action='playback_tick'))
    if status.get('state')=='finished':
     if self.status(ident)['state']!='ready':raise ValueError('Playback failed')
     complete=True;break
    if status.get('state')!='active':raise ValueError('Warm playback stopped')
    await asyncio.sleep(1)
  except BaseException as err:
   self.last_stop={'type':type(err).__name__,'viewer_requested':self.stop_requested,'owned':self.agent.owned(),'same_generation':self.agent.generation==generation,'viewer_expired':self.viewer_until is not None and time.monotonic()>self.viewer_until}
   if not started:(path/'progress.json').write_text(json.dumps({'state':'failed','error_type':type(err).__name__,'error':'Warm media worker is not ready; retry shortly.'}))
  finally:
   if not complete:
    # A lost start acknowledgement may still have started the exact attempt.
    # The worker tombstone also rejects an original start arriving after cancel.
    try:await request(dict(binding,action='playback_cancel'),timeout=20)
    except Exception:pass  # Independent worker permit and 8s controller lease.
    try:
     progress=json.loads((path/'progress.json').read_bytes());progress['controller_stop']=getattr(self,'last_stop',{});(path/'progress.json').write_text(json.dumps(progress))
    except (OSError,ValueError):pass
   (path/'clip.mp4').unlink(missing_ok=True)
