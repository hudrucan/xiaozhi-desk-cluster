import json,pathlib,subprocess,time
def cmd(*a):return subprocess.run(a,capture_output=True,text=True,timeout=6)
groups={}
names=cmd('podman','ps','--format={{.Names}}').stdout.splitlines()
for name in [n for n in names if n=='desk-homeassistant-core' or n.startswith('desk-camera-relay')]:
 try:
  info=json.loads(cmd('podman','inspect',name).stdout)[0]
  if not info['State']['Running']:continue
  pid=info['State']['Pid'];path=pathlib.Path('/sys/fs/cgroup'+pathlib.Path('/proc',str(pid),'cgroup').read_text().strip().split(':',2)[2]);groups[name]=path
 except Exception:pass
for name in ('desk-camera','desk-camera-proxy'):
 try:
  cg=cmd('systemctl','show',name,'--property=ControlGroup','--value').stdout.strip()
  if cg:groups[name]=pathlib.Path('/sys/fs/cgroup'+cg)
 except Exception:pass
def sample():
 result={}
 for name,p in groups.items():
  try:result[name]={'cpu':int(dict(line.split() for line in (p/'cpu.stat').read_text().splitlines())['usage_usec']),'memory':int((p/'memory.current').read_text()),'oom_kill':int(dict(line.split() for line in (p/'memory.events').read_text().splitlines())['oom_kill'])}
  except Exception:pass
 return result
a=sample();start=time.monotonic();time.sleep(6);b=sample();elapsed=time.monotonic()-start
out={'seconds':round(elapsed,2),'sample':'three camera live cards open on browser','services':{n:{'cpu_percent_one_core':round((v['cpu']-a[n]['cpu'])/elapsed/10000,2),'memory_mib':round(v['memory']/1048576,1),'oom_kill':v['oom_kill']} for n,v in b.items() if n in a}}
path=pathlib.Path('/var/lib/desk-camera/resource-validation.json');path.write_text(json.dumps(out));path.chmod(0o600);print(json.dumps(out))
