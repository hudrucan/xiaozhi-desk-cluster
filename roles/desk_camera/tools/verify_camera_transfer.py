"""Reversible single-camera relay drain, with other cameras and Core left running."""
import base64,json,pathlib,time,urllib.request,subprocess
c=json.loads(pathlib.Path('/etc/desk-camera/config.json').read_bytes());workload='tapo_c222_161';camera=next(x for x in c['cameras'] if x['workload']==workload);opener=urllib.request.build_opener(urllib.request.ProxyHandler({}))
def views():
 out={}
 for node,ip in c['members'].items():
  with opener.open('http://'+ip+':20226/status',timeout=2) as r:out[node]=json.loads(r.read(16384))['cameras']
 return {w:[n for n,items in out.items() if any(v['workload']==w and v['local_ready'] for v in items)] for w in [x['workload'] for x in c['cameras']]}
def core_pid():
 p=subprocess.run(['podman','inspect','desk-homeassistant-core'],capture_output=True)
 return json.loads(p.stdout)[0]['State']['Pid'] if p.returncode==0 else None
before=views()
if before[workload]!=[c['node']]:raise SystemExit('Must execute on the selected camera owner')
drain=pathlib.Path('/var/lib/desk-camera')/workload/'drain'
if drain.exists():raise SystemExit('Operator drain already exists')
report={'workload':workload,'old_owner':c['node'],'other_owners_before':{w:v for w,v in before.items() if w!=workload},'observations':[]};pid=core_pid();start=time.monotonic()
try:
 drain.write_text('single camera software transfer test');drain.chmod(0o600)
 for _ in range(90):
  v=views();owners=v[workload];elapsed=round(time.monotonic()-start,2);report['observations'].append({'seconds':elapsed,'owners':owners})
  if len(owners)>1:raise RuntimeError('Overlapping ready relay owners')
  if any(v[w]!=old for w,old in before.items() if w!=workload):raise RuntimeError('Another camera owner changed')
  if owners and owners!=before[workload]:
   report['new_owner']=owners[0];report['ready_seconds']=elapsed;break
  time.sleep(.5)
 else:raise TimeoutError('No successor')
 r=c['relay'];stream=next(iter(camera['streams']));req=urllib.request.Request('http://192.168.1.186:1986/api/streams?src='+stream);req.add_header('Authorization','Basic '+base64.b64encode((r['user']+':'+r['password']).encode()).decode())
 for attempt in range(10):
  try:
   with opener.open(req,timeout=5) as response:data=json.loads(response.read(65536));report['stream_route_http']=response.status
   break
  except urllib.error.HTTPError as e:
   if e.code!=503 or attempt==9:raise
   time.sleep(.5)
 report['route_ready_seconds']=round(time.monotonic()-start,2)
 if not any('192.168.1.161' in p.get('url','') for p in data['producers']):raise RuntimeError('Wrong camera source')
 report['core_pid_unchanged']=core_pid()==pid;report['other_owners_unchanged']=True
finally:
 drain.unlink(missing_ok=True);report['drain_removed']=True;p=pathlib.Path('/var/lib/desk-camera/transfer-161-validation.json');p.write_text(json.dumps(report));p.chmod(0o600)
print(json.dumps({k:v for k,v in report.items() if k!='observations'}))
