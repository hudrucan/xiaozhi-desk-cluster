"""One reversible media-owner drain; never stops Core or other services."""
import base64,json,pathlib,subprocess,time,urllib.request
config=json.loads(pathlib.Path('/etc/desk-camera/config.json').read_bytes())
ROOT=pathlib.Path('/var/lib/desk-camera');drain=ROOT/'drain';out=ROOT/'transfer-validation.json'
if drain.exists():raise SystemExit('Existing operator drain; refusing test')
def status(ip):
 try:
  with urllib.request.build_opener(urllib.request.ProxyHandler({})).open('http://'+ip+':20226/status',timeout=.7) as r:return json.loads(r.read(4096))
 except Exception:return None
def streams():
 req=urllib.request.Request('http://192.168.1.186:1986/api/streams?src=bed_hd&video')
 r=config['relay'];req.add_header('Authorization','Basic '+base64.b64encode((r['user']+':'+r['password']).encode()).decode())
 with urllib.request.build_opener(urllib.request.ProxyHandler({})).open(req,timeout=6) as response:return response.status
before={n:status(ip) for n,ip in config['members'].items()}
owners=[n for n,v in before.items() if v and v['local_ready']]
if owners!=[config['node']]:raise SystemExit('Test must run on the current ready owner')
report={'old_owner':config['node'],'started':time.time(),'observations':[]};start=time.monotonic()
try:
 drain.write_text('camera media transfer verification\n');drain.chmod(0o600)
 for _ in range(80):
  views={n:status(ip) for n,ip in config['members'].items()};ready=[n for n,v in views.items() if v and v['local_ready']]
  report['observations'].append({'seconds':round(time.monotonic()-start,2),'ready':ready})
  if len(ready)>1:raise RuntimeError('Overlapping ready owners')
  if len(ready)==1 and ready[0]!=config['node']:
   report['new_owner']=ready[0];report['ready_seconds']=round(time.monotonic()-start,2)
   report['stream_probe_http']=streams();report['probe_seconds']=round(time.monotonic()-start,2);break
  time.sleep(.5)
 else:raise TimeoutError('No ready media successor')
finally:
 drain.unlink(missing_ok=True);report['drain_removed']=True;report['finished']=time.time();out.write_text(json.dumps(report));out.chmod(0o600)
print(json.dumps({k:v for k,v in report.items() if k!='observations'}))
