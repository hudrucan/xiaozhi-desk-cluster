"""Per-camera, lease-bound playback jobs. NATS carries fixed metadata only."""
import asyncio,hashlib,json,os,pathlib,re,shutil,time
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
 def __init__(self,agent):self.agent=agent;self.task=None;self.current=None
 def active(self):return bool(self.task and not self.task.done())
 def status(self,ident):
  p=clip_path(self.agent.c['workload'],ident)
  if p.is_file() and not p.is_symlink() and 1024<p.stat().st_size<=LIMIT:return {'id':ident,'node':self.agent.node,'state':'ready','bytes':p.stat().st_size}
  value={'id':ident,'node':self.agent.node,'state':'preparing' if self.current==ident and self.active() else 'missing'}
  if self.current==ident:
   try:
    v=json.loads((p.parent/'progress.json').read_bytes())
    value.update({k:v[k] for k in ('state','progress','total','error') if k in v})
   except Exception:pass
   if not self.active() and value['state'] in ('missing','preparing'):value.update(state='failed',error='Worker changed or preparation stopped; retry this clip.')
  return value
 def cached(self,value):
  job=validate(self.agent.c['workload'],value);ident=identity(job);state=self.status(ident)
  if state['state']=='ready':return state
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
     if state['state'] in ('ready','preparing'):items.append(dict(job,id=ident,node=self.agent.node,state=state['state']))
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
 def prepare(self,value,session=None):
  job=validate(self.agent.c['workload'],value);ident=identity(job);status=self.status(ident)
  if status['state']=='ready':return status
  if self.active():
   if self.current!=ident:raise PlaybackError('camera_busy')
   return status
  if not self.agent.owned() or not self.agent.ready() or not self.agent.eligible():raise PlaybackError('worker_not_ready')
  if self.agent.local.get('memory_available_mib',0)<1200:raise PlaybackError('memory_low')
  session=session_handle(session)
  if shutil.disk_usage(ROOT if ROOT.exists() else ROOT.parent).free<512*1024*1024:raise PlaybackError('disk_low')
  self.current=ident;self.task=asyncio.create_task(self.run(job,ident,self.agent.generation,session));return {'id':ident,'node':self.agent.node,'state':'preparing'}
 async def run(self,job,ident,generation,session):
  path=clip_path(job['workload'],ident).parent;path.mkdir(parents=True,exist_ok=True,mode=0o700)
  # Only our completed old jobs; never evict an in-flight clip or arbitrary path.
  for old in path.parent.iterdir():
   if old!=path and re.fullmatch('[a-f0-9]{32}',old.name) and not old.is_symlink() and time.time()-old.stat().st_mtime>3600:shutil.rmtree(old)
  completed=sorted((p for p in ROOT.glob('*/*/ready.mp4') if not p.is_symlink()),key=lambda p:p.stat().st_mtime)
  total=sum(p.stat().st_size for p in completed)
  for p in completed:
   if total<=768*1024*1024:break
   total-=p.stat().st_size;p.unlink()
  if shutil.disk_usage(path).free<512*1024*1024:return
  (path/'request.json').write_text(json.dumps(job));(path/'request.json').chmod(0o600)
  (path/'progress.json').write_text('{"state":"preparing"}');(path/'clip.mp4').unlink(missing_ok=True)
  # Ephemeral native media handle never enters persistent request/progress files.
  session_path=self.agent.run_dir/'playback-session.json'
  from config_journal import atomic,encoded
  atomic(session_path,encoded(session))
  name='desk-camera-playback-'+job['workload'];proc=None
  async def command(*args,timeout=8):
   p=await asyncio.create_subprocess_exec(*args,stdout=asyncio.subprocess.PIPE,stderr=asyncio.subprocess.DEVNULL)
   try:return await asyncio.wait_for(p.communicate(),timeout)
   except BaseException:p.kill();await p.wait();raise
  try:
   proc=await asyncio.create_subprocess_exec('systemd-run','--quiet','--wait','--collect','--pipe','--unit=desk-camera-job-'+job['workload'],'--property=CPUQuota=60%','--property=MemoryMax=896M','--property=RuntimeMaxSec='+str(max(190,job['end']-job['start']+120)),'--property=TasksMax=96','--property=BindsTo=desk-camera-relay.service','--','podman','run','--replace','--name',name,'--pull=never','--network=host','--cap-drop=ALL','--security-opt=no-new-privileges','--read-only','--pids-limit=64','--cpus=.5','--memory=768m','--log-driver=none','--tmpfs=/tmp:size=320m','-e','PYTHONDONTWRITEBYTECODE=1','-e','PYTHONPATH=/deps','-v','/var/lib/desk-camera/deps:/deps:ro','-v','/etc/desk-camera/playback.json:/input/credentials.json:ro','-v',str(session_path)+':/input/session.json:ro','-v','/usr/local/libexec/desk-camera/playback_worker.py:/worker.py:ro','-v',str(path)+':/output:rw','--entrypoint=python3',IMAGE,'/worker.py',stdout=asyncio.subprocess.DEVNULL,stderr=asyncio.subprocess.DEVNULL)
   deadline=time.monotonic()+max(190,job['end']-job['start']+120);identified=False;waiter=asyncio.create_task(proc.wait())
   while proc.returncode is None:
    if not self.agent.owned() or self.agent.generation!=generation or time.monotonic()>deadline:raise ValueError('Playback lease expired')
    try:
     if identified:raise StopIteration()
     raw,_=await command('podman','inspect',name,timeout=8);info=json.loads(raw)[0]
     pid=info['State']['Pid']
     if pid:
      start=pathlib.Path('/proc',str(pid),'stat').read_text().split(') ',1)[1].split()[19]
      from config_journal import atomic,encoded
      atomic(self.agent.run_dir/'playback-process.json',encoded({'pid':pid,'start':start}));identified=True
    except (TimeoutError,StopIteration,FileNotFoundError,KeyError,ValueError,IndexError):pass
    try:await asyncio.wait_for(asyncio.shield(waiter),1)
    except TimeoutError:pass
   if proc.returncode or not (path/'ready.mp4').is_file():raise ValueError('Playback failed')
  except BaseException as err:
   (path/'ready.mp4').unlink(missing_ok=True)
   try:failed=json.loads((path/'progress.json').read_bytes()).get('state')=='failed'
   except Exception:failed=False
   if not failed:(path/'progress.json').write_text(json.dumps({'state':'failed','error_type':type(err).__name__,'error':'Clip preparation interrupted. Retry when the worker is ready.'}))
  finally:
   try:await command('podman','kill',name,timeout=3)
   except Exception:pass
   if proc is not None:
    try:await asyncio.wait_for(proc.wait(),3)
    except TimeoutError:proc.kill();await proc.wait()
   try:await command('podman','rm','-f',name)
   except Exception:pass
   (self.agent.run_dir/'playback-process.json').unlink(missing_ok=True)
   session_path.unlink(missing_ok=True)
   (path/'clip.mp4').unlink(missing_ok=True)
   # A failed SDK stream can leave temporary transport files in this job directory.
   for p in path.iterdir():
    if p.is_file() and p.name not in {'ready.mp4','request.json','progress.json'}:p.unlink()
