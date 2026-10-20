"""Bounded before/after camera infrastructure sample, without opening streams."""
import pathlib,subprocess,json,time,urllib.request

def cmd(*args):return subprocess.run(args,capture_output=True,text=True,timeout=6).stdout.strip()
def groups():
 result={}
 for name in ('desk-camera','desk-camera-relay','desk-camera-proxy'):
  cg=cmd('systemctl','show',name,'-p','ControlGroup','--value')
  if cg and pathlib.Path('/sys/fs/cgroup'+cg).is_dir():result[name]=pathlib.Path('/sys/fs/cgroup'+cg)
 return result
paths=groups();node=json.loads(pathlib.Path('/etc/desk-camera/config.json').read_bytes())['node']
opener=urllib.request.build_opener(urllib.request.ProxyHandler({}));ip=json.loads(pathlib.Path('/etc/desk-camera/config.json').read_bytes())['members'][node]
def status():
 with opener.open('http://'+ip+':20226/status',timeout=2) as response:return json.loads(response.read(16384))
def sample():
 return {name:{'cpu':int(dict(line.split() for line in (p/'cpu.stat').read_text().splitlines())['usage_usec']),'memory':int((p/'memory.current').read_text()),'io':(p/'io.stat').read_text(),'oom':int(dict(line.split() for line in (p/'memory.events').read_text().splitlines())['oom_kill'])} for name,p in paths.items()}
a=sample();first=status();start=time.monotonic();time.sleep(30);b=sample();elapsed=time.monotonic()-start;last=status()
result={'node':node,'seconds':round(elapsed,2),'scope':'camera infrastructure; no streams opened by measurement','services':{n:{'cpu_percent_one_core':round((v['cpu']-a[n]['cpu'])/elapsed/10000,2),'memory_mib':round(v['memory']/1048576,1),'block_io_changed':v['io']!=a[n]['io'],'oom_kill':v['oom']} for n,v in b.items()},'transport':last.get('transport'),'cameras':last['cameras']}
if 'transport' in first and 'transport' in last:
 result['transport_delta']={k:last['transport'][k]-first['transport'][k] for k in ('requests_sent','status_batches','legacy_fallbacks','startup_flushes')}
 result['request_rate_per_second']=round(result['transport_delta']['requests_sent']/elapsed,2)
p=pathlib.Path('/var/lib/desk-camera/optimization-validation.json');p.write_text(json.dumps(result));p.chmod(0o600);print(json.dumps(result))
