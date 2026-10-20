"""Read-only check: stable per-camera ownership, bounded RPC state and service routes."""
import json,pathlib,urllib.request,time,base64
c=json.loads(pathlib.Path('/etc/desk-camera/config.json').read_bytes());opener=urllib.request.build_opener(urllib.request.ProxyHandler({}));before={};after={}
for results in (before,after):
 for n,ip in c['members'].items():
  with opener.open('http://'+ip+':20226/status',timeout=2) as r:results[n]=json.loads(r.read(16384))
 if results is before:time.sleep(5)
for node,v in after.items():
 t=v['transport'];first=before[node]['transport']
 assert v['voter_ready'] and t['connected'] and t['reply_subscriptions']==3 and t['startup_flushes']==1 and t['pending_requests']<32
 assert t['legacy_fallbacks']==first['legacy_fallbacks'],'Legacy fallback still active'
 for x in v['cameras']:assert x['ready']
for camera in c['cameras']:
 w=camera['workload'];owners=[n for n,v in after.items() if next(x for x in v['cameras'] if x['workload']==w)['local_ready']]
 assert len(owners)==1
 stream=next(iter(camera['streams']));req=urllib.request.Request('http://192.168.1.186:1986/api/streams?src='+stream);req.add_header('Authorization','Basic '+base64.b64encode((c['relay']['user']+':'+c['relay']['password']).encode()).decode())
 with opener.open(req,timeout=3) as r:data=json.loads(r.read(65536))
 host='192.168.1.'+w.rsplit('_',1)[1];assert any(host in p.get('url','') for p in data['producers'])
 print(json.dumps({'workload':w,'owner':owners[0],'http_source_route':'PASS'}))
for path in ('/','/xiaozhi/','/dns/','/monitor/','/vpn/','/network/'):
 with opener.open('http://192.168.1.186'+path,timeout=8) as response:assert response.status==200
print(json.dumps({'transport_no_fallback_growth':'PASS','fixed_reply_subscriptions':'PASS','cluster_ui_routes':'PASS'}))
