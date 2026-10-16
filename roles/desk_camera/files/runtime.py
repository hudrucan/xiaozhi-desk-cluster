"""Warm empty go2rtc on standby; independent BOOTTIME fence for active sources."""
import base64,json,math,os,pathlib,signal,subprocess,sys,time,urllib.request,threading
sys.path.insert(0,'/usr/local/libexec/desk-homeassistant')
from lease import clock
from config_journal import atomic,encoded
RUN=pathlib.Path('/run/desk-camera')

def generation(v,now):
 until=v.get('until');g=v.get('generation')
 return g if type(until) in (int,float) and math.isfinite(until) and now<until<=now+11 and isinstance(g,str) and len(g)==32 and all(x in '0123456789abcdef' for x in g) else None
def render(c,g):
 return {'api':{'listen':c['members'][c['node']]+':'+str(c.get('api_port',1984)),'username':c['relay']['user'],'password':c['relay']['password'],'local_auth':True},
 'rtsp':{'listen':c['members'][c['node']]+':'+str(c.get('rtsp_port',18554)),'username':c['relay']['user'],'password':c['relay']['password']},
 'webrtc':{'listen':':'+str(c.get('webrtc_port',18555)),'candidates':[c['lan_ip']+':'+str(c.get('webrtc_port',18555))]},'streams':c['streams'] if g else {},'log':{'level':'error'}}
def probe(c):
 req=urllib.request.Request('http://'+c['members'][c['node']]+':'+str(c.get('api_port',1984))+'/api/streams')
 req.add_header('Authorization','Basic '+base64.b64encode((c['relay']['user']+':'+c['relay']['password']).encode()).decode())
 try:
  with urllib.request.build_opener(urllib.request.ProxyHandler({})).open(req,timeout=.5) as r:v=json.loads(r.read(65536))
  return set(v)==set(c['streams'])
 except Exception:return False
def command(*args,timeout=8,check=False):return subprocess.run(args,capture_output=True,timeout=timeout,check=check)
class Worker:
 def __init__(self,c):
  self.c=c;self.name='desk-camera-relay'+('' if c['workload']=='tapo_c222_160' else '-'+c['workload']);self.run_dir=RUN if c['workload']=='tapo_c222_160' else RUN/c['workload']
  self.run_dir.mkdir(parents=True,exist_ok=True,mode=0o700)
 def desired(self):
  try:return generation(json.loads((self.run_dir/'permit.json').read_bytes()),clock())
  except Exception:return None
 def inspect(self):
  try:return json.loads(command('podman','inspect',self.name).stdout)[0]
  except Exception:return None
 def kill(self):
  try:
   v=json.loads((self.run_dir/'playback-process.json').read_bytes());pid=v['pid'];start=v['start']
   if pathlib.Path('/proc',str(pid),'stat').read_text().split(') ',1)[1].split()[19]==start:os.kill(pid,signal.SIGKILL)
  except (FileNotFoundError,ProcessLookupError,KeyError,ValueError):pass
  try:
   v=json.loads((self.run_dir/'process.json').read_bytes());pid=v['pid'];start=v['start']
   if pathlib.Path('/proc',str(pid),'stat').read_text().split(') ',1)[1].split()[19]==start:os.kill(pid,signal.SIGKILL)
  except (FileNotFoundError,ProcessLookupError,KeyError,ValueError):pass
  try:command('podman','kill','desk-camera-playback-'+self.c['workload'],timeout=2)
  except subprocess.TimeoutExpired:pass
  command('podman','kill',self.name)
 def run(self,stopping):
  c=self.c
  command('podman','rm','-f',self.name);proc=None;active=object();checked=0;ok=False
  try:
   while not stopping.is_set():
    g=self.desired()
    if active!=g or proc is None or proc.poll() is not None:
     if proc is not None:self.kill();proc.wait(timeout=5)
     command('podman','rm','-f',self.name)
     atomic(self.run_dir/'go2rtc.yaml',encoded(render(c,g)));active=g;ok=False;checked=0
     proc=subprocess.Popen(['podman','run','--name',self.name,'--pull=never','--network=host','--cap-drop=ALL','--security-opt=no-new-privileges','--read-only','--pids-limit=64','--cpus=.5','--memory=192m','--log-driver=none','--tmpfs=/tmp:size=16m','-v',str(self.run_dir/'go2rtc.yaml')+':/config/go2rtc.yaml:ro',c['image']],stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)
     # Capture init identity before publishing readiness; also handles launch failure.
     for _ in range(20):
      info=self.inspect()
      if info and info['State']['Running']:
       pid=info['State']['Pid'];start=pathlib.Path('/proc',str(pid),'stat').read_text().split(') ',1)[1].split()[19]
       atomic(self.run_dir/'process.json',encoded({'pid':pid,'start':start}));break
      if self.desired()!=active or stopping.is_set() or proc.poll() is not None:break
      time.sleep(.1)
    if clock()-checked>1:
     ok=bool(active) and probe(c);checked=clock()
     atomic(self.run_dir/'runtime.json',encoded({'generation':active,'ready':ok,'at':time.time()}))
    time.sleep(.25)
  finally:
   self.kill()
   if proc is not None:proc.wait(timeout=5)
   command('podman','rm','-f',self.name);atomic(self.run_dir/'runtime.json',encoded({'generation':None,'ready':False,'at':time.time()}))

def main():
 c=json.loads(pathlib.Path('/etc/desk-camera/config.json').read_bytes());workers=[Worker(dict(c,**camera)) for camera in c.get('cameras',[{}])]
 if len(sys.argv)>1 and sys.argv[1]=='fence':
  for w in workers:w.kill()
  return
 stopping=threading.Event();failed=[]
 for sig in (signal.SIGINT,signal.SIGTERM):signal.signal(sig,lambda *_:stopping.set())
 def run(w):
  try:w.run(stopping)
  except Exception:failed.append(True);stopping.set()
 threads=[threading.Thread(target=run,args=(w,)) for w in workers]
 for t in threads:t.start()
 for t in threads:t.join()
 if failed:raise RuntimeError()
if __name__=='__main__':
 try:main()
 except Exception:raise SystemExit('Camera relay stopped; private details withheld')
