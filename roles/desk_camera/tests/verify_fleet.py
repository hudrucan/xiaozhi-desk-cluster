"""Read-only fleet/source routing check; no camera frames are saved."""
import base64,json,pathlib,urllib.request
config=json.loads(pathlib.Path('/etc/desk-camera/config.json').read_bytes());opener=urllib.request.build_opener(urllib.request.ProxyHandler({}));out={}
for node,ip in config['members'].items():
 with opener.open('http://'+ip+':20226/status',timeout=2) as r:out[node]=json.loads(r.read(16384))['cameras']
for cam in config['cameras']:
 workload=cam['workload'];owners=[node for node,values in out.items() if any(v['workload']==workload and v['local_ready'] for v in values)]
 if len(owners)!=1:raise RuntimeError('Camera ownership overlap or unready')
 stream=next(iter(cam['streams']));req=urllib.request.Request('http://192.168.1.186:1986/api/streams?src='+stream)
 req.add_header('Authorization','Basic '+base64.b64encode((config['relay']['user']+':'+config['relay']['password']).encode()).decode())
 with opener.open(req,timeout=3) as r:result=json.loads(r.read(65536))
 if not isinstance(result,dict) or not {'producers','consumers'}<=set(result):raise RuntimeError('Unexpected source API response')
 host='192.168.1.'+workload.rsplit('_',1)[1]
 if not any(host in p.get('url','') for p in result['producers']):raise RuntimeError('Wrong camera source')
 print(json.dumps({'workload':workload,'owner':owners[0],'ready':True,'source_routing':True}))
