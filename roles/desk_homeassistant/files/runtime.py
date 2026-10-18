"""Independent Core process fence. No live lease means no running Core namespace."""
import argparse,json,math,os,pathlib,signal,subprocess,time
from lease import clock
from config_journal import atomic,encoded
NAME='desk-homeassistant-core'
RUN=pathlib.Path('/run/desk-homeassistant')
def command(*args,timeout=8,check=True):
 return subprocess.run(args,stdout=subprocess.PIPE,stderr=subprocess.DEVNULL,timeout=timeout,check=check)
def inspect():
 try:return json.loads(command('podman','inspect',NAME).stdout)[0]
 except Exception:return None

def authorized(generation=None):
 try:
  v=json.loads((RUN/'permit.json').read_bytes());until=v['until']
  return (type(until) in (int,float) and math.isfinite(until) and clock()<until<=clock()+11
   and isinstance(v['generation'],str) and len(v['generation'])==32 and (generation is None or generation==v['generation']))
 except Exception:return False

def fence():
 try:
  value=json.loads((RUN/'runtime.json').read_bytes());pid=value['pid'];start=value['start']
  if pathlib.Path('/proc',str(pid),'stat').read_text().split(') ',1)[1].split()[19]==start:os.kill(pid,signal.SIGKILL)
 except (FileNotFoundError,ProcessLookupError,KeyError,ValueError):pass
 # Covers an early launch failure before the init PID was recorded.
 command('podman','kill',NAME,check=False)

def stop():
 # This path is for planned clean checkpoints, never the expiry fence.
 command('podman','stop','--time=60',NAME,timeout=68,check=False)
 info=inspect()
 if not info or info['State']['Running']:raise RuntimeError('Core stop incomplete')
 state=info['State'];atomic(RUN/'stopped.json',encoded({'exit_code':state['ExitCode'],'oom':state.get('OOMKilled',False),'at':time.time()}))
 if state['ExitCode']!=0 or state.get('OOMKilled'):raise RuntimeError('Core did not stop cleanly')

def run():
 c=json.loads(pathlib.Path('/etc/desk-homeassistant/config.json').read_bytes())
 if not authorized():raise RuntimeError('No Core lease')
 generation=json.loads((RUN/'permit.json').read_bytes())['generation']
 (RUN/'exited.json').unlink(missing_ok=True)
 command('podman','rm','-f',NAME,check=False)
 cmd=['podman','run','--name',NAME,'--pull=never','--network=bridge','-p',c['members'][c['node']]+':18123:8123/tcp',
  '--cap-drop=ALL','--security-opt=no-new-privileges','--read-only','--pids-limit=256','--cpus=1.0','--memory=1536m',
  '--log-driver=k8s-file','--log-opt=max-size=2mb','--tmpfs=/tmp:rw,nosuid,nodev,size=128m','-e','TZ=Asia/Ho_Chi_Minh',
  '-v','/var/lib/desk-homeassistant/current:/config:rw','-v','/var/lib/desk-homeassistant/ui:/run/desk-cluster:ro',c['image']]
 if pathlib.Path('/etc/desk-camera/core-migration.json').is_file():
  cmd[-1:-1]=['-e','PYTHONPATH=/opt/desk-camera-bootstrap:/opt/desk-camera-deps',
   '-e','DESK_CAMERA_RECORDING_GUARD=1',
   '-v','/usr/local/libexec/desk-homeassistant/recording_bootstrap.py:/opt/desk-camera-bootstrap/sitecustomize.py:ro',
   '-v','/var/lib/desk-camera/deps:/opt/desk-camera-deps:ro',
   '-v','/var/lib/desk-camera/cache/cold:/media/tapo_control:rw',
   '-v','/var/lib/desk-camera/cache/hot:/config/www/tapo_control:rw']
  if pathlib.Path('/etc/desk-camera/recording-policy').is_dir():
   cmd[-1:-1]=['-v','/etc/desk-camera/recording-policy:/opt/desk-camera-authority:ro']
 proc=subprocess.Popen(cmd,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)
 stopping=[False]
 signal.signal(signal.SIGTERM,lambda *_:stopping.__setitem__(0,True));signal.signal(signal.SIGINT,lambda *_:stopping.__setitem__(0,True))
 identity=None
 try:
  while proc.poll() is None:
   if not identity:
    info=inspect()
    if info and info['State']['Running']:
     pid=info['State']['Pid'];identity=(pid,pathlib.Path('/proc',str(pid),'stat').read_text().split(') ',1)[1].split()[19])
     atomic(RUN/'runtime.json',encoded({'pid':identity[0],'start':identity[1],'generation':generation}))
   if not authorized(generation):
    # Kill the container init directly; do not wait for a hung Podman client.
    if identity:
     pid,start=identity
     try:
      if pathlib.Path('/proc',str(pid),'stat').read_text().split(') ',1)[1].split()[19]==start:os.kill(pid,signal.SIGKILL)
     except FileNotFoundError:pass
    else:command('podman','kill',NAME,check=False)
    break
   # systemd ExecStop performs the graceful stop before signalling this wrapper.
   if stopping[0]:break
   time.sleep(.25)
 finally:
  try:proc.wait(timeout=3)
  except subprocess.TimeoutExpired:
   command('podman','kill',NAME,check=False);proc.wait(timeout=5)
  info=inspect()
  if info:
   atomic(RUN/'exited.json',encoded({'exit_code':info['State']['ExitCode'],'oom':info['State'].get('OOMKilled',False),'at':time.time(),'generation':generation}))
if __name__=='__main__':
 p=argparse.ArgumentParser();p.add_argument('action',choices=['run','stop','fence']);a=p.parse_args()
 try:{'stop':stop,'run':run,'fence':fence}[a.action]()
 except Exception:raise SystemExit('Core runtime stopped; private diagnostics withheld')
