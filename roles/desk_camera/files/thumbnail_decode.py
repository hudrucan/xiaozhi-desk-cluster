"""One bounded frame decode on an existing camera worker; no camera credentials."""
import hashlib,json,pathlib,re,subprocess,threading,time
from playback import IMAGE,clip_path
MAX_INPUT=1024*1024
MAX_IMAGE=2*1024*1024
WRAPPER='''import subprocess,sys,json,pathlib,time
start=time.monotonic()
args=['ffmpeg','-hide_banner','-loglevel','error','-threads','1','-probesize','1048576','-analyzeduration','1000000']
if len(sys.argv)>1:args+=['-i',sys.argv[1]];data=None
else:args+=['-f','mpegts','-i','pipe:0'];data=sys.stdin.buffer.read(1048577)
args+=['-map','0:v:0','-an','-frames:v','1','-vf','scale=320:180:force_original_aspect_ratio=decrease','-q:v','4','-threads','1','-f','image2pipe','pipe:1']
p=subprocess.run(args,input=data,capture_output=True,timeout=12)
assert p.returncode==0 and 4<len(p.stdout)<2097152 and p.stdout[:2]==b'\\xff\\xd8'
metrics={'elapsed_seconds':round(time.monotonic()-start,3)}
for name in ('memory.peak','cpu.stat','memory.events'):
 try:metrics[name]=pathlib.Path('/sys/fs/cgroup',name).read_text().strip()
 except OSError:pass
sys.stderr.write(json.dumps(metrics));sys.stdout.buffer.write(p.stdout)
'''
def memory_ok():
 try:
  values=dict(line.split(':',1) for line in pathlib.Path('/proc/meminfo').read_text().splitlines())
  return int(values['MemAvailable'].split()[0])>=384*1024
 except (OSError,ValueError,KeyError):return False

class Decoder:
 def __init__(self):self.lock=threading.Lock();self.completed=0;self.failed=0;self.last=None
 def decode(self,agents,scope,data):
  if set(scope)!={'workload','id','source','generation','digest'} or scope['source'] not in ('ts','cache'):raise ValueError('Invalid thumbnail scope')
  if not re.fullmatch('[a-f0-9]{64}',scope['digest']) or hashlib.sha256(data).hexdigest()!=scope['digest']:raise ValueError('Invalid thumbnail body')
  agent=next(a for a in agents if a.c['workload']==scope['workload']);path=clip_path(scope['workload'],scope['id']).parent
  if not memory_ok():raise ValueError('Frame decode memory admission')
  if any(a.playback.active() for a in agents):raise ValueError('Recording has priority')
  if not self.lock.acquire(blocking=False):raise ValueError('Frame decoder busy')
  name='desk-camera-thumbnail';started=time.monotonic()
  try:
   cmd=['podman','run','--rm','--name',name,'--pull=never','--network=none','--read-only','--cap-drop=ALL','--security-opt=no-new-privileges','--cpus=.25','--memory=256m','--pids-limit=48','--log-driver=none','--tmpfs=/tmp:size=8m','--entrypoint=python3']
   tail=[]
   if scope['source']=='ts':
    if not data or len(data)>MAX_INPUT or not re.fullmatch('[a-f0-9]{32}',scope['generation']) or not agent.owned() or not agent.ready() or scope['generation']!=agent.generation:raise ValueError('Camera owner changed')
    cmd.insert(2,'-i')
   else:
    if data or scope['generation']!='':raise ValueError('Invalid cached input')
    state=agent.playback.status(scope['id'])
    if state.get('state')!='ready':raise ValueError('Cached clip unavailable')
    if state.get('format')=='hls':
     from hls_files import artifact_path,snapshot
     snapshot(path,state['generation']);file=artifact_path(path,state['generation'],'index.m3u8').relative_to(path)
    else:file=pathlib.Path('ready.mp4')
    if any(p.is_symlink() for p in (path,*path.parents)):raise ValueError('Invalid cached path')
    (path/'.access').touch();cmd+=['-v',str(path)+':/input:ro'];tail=['/input/'+str(file)]
   cmd += [IMAGE,'-c',WRAPPER,*tail]
   # Escape the controller's 15% CPU / 96 MiB cgroup, as playback jobs do.
   cmd=['systemd-run','--quiet','--wait','--collect','--pipe','--unit=desk-camera-thumbnail','--property=CPUQuota=25%','--property=MemoryMax=320M','--property=TasksMax=64','--property=RuntimeMaxSec=24','--',*cmd]
   try:p=subprocess.run(cmd,input=data if scope['source']=='ts' else None,capture_output=True,timeout=20)
   except subprocess.TimeoutExpired:raise ValueError('Frame decode timed out') from None
   if p.returncode or not 4<len(p.stdout)<=MAX_IMAGE or p.stdout[:2]!=b'\xff\xd8':raise ValueError('Frame decode failed')
   try:metrics=json.loads(p.stderr)
   except (ValueError,TypeError):metrics={}
   self.last=dict(metrics,source=scope['source'],request_seconds=round(time.monotonic()-started,3),input_bytes=len(data),jpeg_bytes=len(p.stdout));self.completed+=1
   return p.stdout
  except Exception:self.failed+=1;raise
  finally:
   try:subprocess.run(['podman','rm','-f',name],stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL,timeout=5)
   finally:self.lock.release()
 def public(self):return {'completed':self.completed,'failed':self.failed,'last':self.last,'busy':self.lock.locked()}
