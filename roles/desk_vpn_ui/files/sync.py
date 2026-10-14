#!/usr/bin/env python3
"""Native wg-easy UI backed by an encrypted, quorum configuration journal."""
import argparse,base64,copy,datetime,mimetypes,hashlib,hmac,http.server,json,logging,os,re,secrets,ssl,subprocess,threading,time,urllib.request,urllib.error,urllib.parse
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor
from cryptography.fernet import Fernet
from config_journal import Journal,Failure,atomic,encoded,digest
import database
LOG=logging.getLogger('vpn-config')
LIMIT=database.LIMIT
READ=re.compile(r'^/api/(session|information|client(?:/[1-9][0-9]*(?:/configuration|/qrcode\.svg)?)?|admin/(userconfig|interface|general|hooks|ip-info))$')
WRITE=re.compile(r'^/api/(client(?:/[1-9][0-9]*(?:/enable|/disable)?)?|admin/userconfig)$')

def asset(path,root=Path('/opt/desk-vpn-ui/app/public/_nuxt')):
 root=root.resolve();file=(root/urllib.parse.unquote(path[len('/vpn/_nuxt/'):])).resolve()
 if not file.is_relative_to(root) or not file.is_file() or file.stat().st_size>LIMIT:raise Failure(404,'VPN asset not found')
 return file.read_bytes(),mimetypes.guess_type(file.name)[0] or 'application/octet-stream'

class Native:
 def __init__(self,c):
  self.c=c;self.cookie='';self.login_at=0;self.lock=threading.Lock()
  self.opener=urllib.request.build_opener(urllib.request.ProxyHandler({}))
 def raw(self,path,method='GET',body=None,headers=None):
  req=urllib.request.Request('http://127.0.0.1:51821/vpn'+path,data=body,method=method,headers=headers or {})
  try:r=self.opener.open(req,timeout=20)
  except urllib.error.HTTPError as e:r=e
  with r:
   b=r.read(LIMIT+1)
   if len(b)>LIMIT:raise Failure(502,'Native response exceeds VPN boundary')
   return r.status,dict(r.headers),b
 def session(self):
  with self.lock:
   if self.cookie and time.monotonic()-self.login_at<1200:return
   code,headers,body=self.raw('/api/session','POST',encoded({'username':self.c['username'],'password':self.c['password'],'remember':False}),{'Content-Type':'application/json'})
   cookie=next((v for k,v in headers.items() if k.lower()=='set-cookie'),None)
   if code!=200 or not cookie:raise Failure(503,'VPN native session unavailable')
   self.cookie=cookie.split(';',1)[0];self.login_at=time.monotonic()
 def request(self,path,method='GET',body=None):
  self.session()
  result=self.raw(path,method,body,{'Cookie':self.cookie,'Content-Type':'application/json'})
  if result[0]==401:
   self.cookie='';self.session();result=self.raw(path,method,body,{'Cookie':self.cookie,'Content-Type':'application/json'})
  return result

class Cluster:
 def __init__(self,c,journal,native):
  self.c,self.journal,self.native=c,journal,native;self.seed=json.loads(Path(c['seed']).read_text())
  self.key=base64.urlsafe_b64decode(c['key']);self.lock=threading.RLock();self.pool=ThreadPoolExecutor(max_workers=6)
  self.opener=urllib.request.build_opener(urllib.request.ProxyHandler({}),urllib.request.HTTPSHandler(context=ssl.create_default_context(cafile=c['certificate'])))
  self.heads={};self.error=None;self.event=threading.Event();self.stats_cache=(0,[]);self.health_cache=(0,False);self.initialized=False
 def status(self,full=False):
  head=self.journal.status(full);head['apply_error']=self.error;return head
 def peer(self,action,v):
  if action=='status':return self.status(True)
  if action=='stats':return {'owner':self.owned(),'peers':self.stats() if self.owned() else []}
  if action=='promise':return self.journal.promise(v['revision'],v['ballot'])
  if action in ('accept','commit'):
   database.validate(v['value'],self.seed)
   if action=='accept':return self.journal.accept(v['revision'],v['ballot'],v['value'])
   self.journal.commit(v['revision'],v['value'],v['certificate']);self.event.set();return self.status()
  raise Failure(404,'Unknown VPN peer action')
 def call(self,node,action,v=None):
  if node==self.c['node']:return self.peer(action,v or {})
  path='/peer/'+action;raw=encoded(v or {});stamp=str(int(time.time()));nonce=secrets.token_hex(16)
  signed=('POST\n'+path+'\n'+stamp+'\n'+nonce+'\n'+hashlib.sha256(raw).hexdigest()).encode()
  headers={'Content-Type':'application/json','X-Desk-Time':stamp,'X-Desk-Nonce':nonce,'X-Desk-Signature':hmac.new(self.key,signed,hashlib.sha256).hexdigest()}
  request=urllib.request.Request('https://'+self.c['members'][node]+':51855'+path,data=raw,headers=headers)
  with self.opener.open(request,timeout=3) as response:
   raw=response.read(LIMIT+1)
   if len(raw)>LIMIT:raise Failure(502,'Oversize peer response')
   return json.loads(raw)
 def fanout(self,action,v=None):
  jobs={n:self.pool.submit(self.call,n,action,v) for n in self.c['members']};result={}
  for node,f in jobs.items():
   try:result[node]=f.result()
   except Exception:pass
  return result
 def refresh(self,apply=True):
  heads=self.fanout('status');valid=[v for v in heads.values() if v['revision']>=0]
  if valid:
   head=max(valid,key=lambda v:v['revision'])
   if head['revision']>self.journal.state['revision']:self.peer('commit',head)
  self.heads=heads
  if apply:self.apply()
  return heads
 def choose(self,wanted,expected):
  for attempt in range(3):
   heads=self.refresh(False)
   if len(heads)<2:raise Failure(503,'Two nodes are required to save VPN configuration')
   state=self.journal.state
   if state['revision']!=expected:return False
   rev=expected+1;ballot=[max(h['promised'][0] for h in heads.values())+1,self.c['node']]
   promises=self.fanout('promise',{'revision':rev,'ballot':ballot})
   if len(promises)<2 or any('head' in p for p in promises.values()):continue
   accepted=[p['accepted'] for p in promises.values() if p.get('accepted')]
   chosen=max(accepted,key=lambda p:p['ballot'])['value'] if accepted else wanted
   receipts=self.fanout('accept',{'revision':rev,'ballot':ballot,'value':chosen})
   if len(receipts)<2:continue
   payload={'revision':rev,'value':chosen,'certificate':list(receipts.values())}
   # A valid accept quorum has chosen the revision even if commit acknowledgments are lost.
   self.peer('commit',payload);self.fanout('commit',payload)
   return digest(chosen)==digest(wanted)
  raise Failure(409,'Concurrent VPN configuration update; refresh and retry')
 def apply(self,force=False):
  with self.lock:
   with self.journal.lock:
    rev=self.journal.state['revision'];v=copy.deepcopy(self.journal.state['value'])
    if not force and rev==self.journal.state['applied_revision']:
     self.error=None;return
   database.validate(v,self.seed);self.apply_database(v)
   if not self.c.get('simulate'):
    text,peers=database.tunnel(v)
    atomic(Path('/etc/desk-vpn/wgdesk0.conf'),text.encode())
    # syncconf changes only peer configuration, retaining sessions for unchanged peers.
    stripped=subprocess.check_output(['/usr/bin/wg-quick','strip','/etc/desk-vpn/wgdesk0.conf'],timeout=5)
    subprocess.run(['/usr/bin/wg','syncconf','wgdesk0','/dev/stdin'],input=stripped,check=True,capture_output=True,timeout=5)
    public_path=Path('/etc/desk-vpn/public.json');public=json.loads(public_path.read_text());public['peers']=peers;public['ui_managed']=True
    atomic(public_path,encoded(public))
   with self.journal.lock:
    self.journal.state.update(applied_revision=rev,applied_value=v);self.journal.save()
   self.error=None;self.publish();LOG.info('VPN configuration applied; revision=%d peers=%d',rev,len(v['tables']['clients_table']))
 def apply_database(self,v):
  if self.c.get('simulate'):database.apply(self.c['database'],v);return
  subprocess.run(['/usr/bin/setpriv','--reuid=desk-vpn-ui','--regid=desk-vpn-ui','--init-groups','/usr/bin/python3',str(Path(__file__).with_name('database.py')),'apply',self.c['database']],input=encoded(v),check=True,capture_output=True,timeout=10)
 def mutate(self,path,method,raw):
  with self.lock:
   self.refresh();rev=self.journal.state['revision']
   # Every native mutation starts from the latest committed snapshot.
   code,headers,body=self.native.request(path,method,raw)
   if code>=400:
    self.apply_database(self.journal.state['value']);return code,headers,body
   try:
    wanted=database.read(self.c['database']);database.validate(wanted,self.seed)
    if digest(wanted)!=digest(self.journal.state['value']):
     if not self.choose(wanted,rev):raise Failure(409,'Another update was recovered; refresh before retrying')
    try:self.apply(True)
    except Exception:
     # The accept quorum already chose the value; return native success plus visible Pending status.
     self.error='apply_pending';self.publish();self.event.set()
   except Exception:
    # Restore only the latest chosen state; never undo an acknowledged revision.
    self.apply_database(self.journal.state['value']);self.event.set();raise
   self.heads=self.fanout('status')
   return code,headers,body
 def owned(self):
  try:
   links=json.loads(subprocess.check_output(['/usr/sbin/ip','-j','-4','addr','show','dev','wlan0'],timeout=2))
   return any(a['local']=='192.168.1.254' for link in links for a in link.get('addr_info',[]))
  except Exception:return False
 def stats(self):
  fields={}
  for field in ('endpoints','latest-handshakes','transfer'):
   text=subprocess.check_output(['/usr/bin/wg','show','wgdesk0',field],text=True,timeout=2)
   fields[field]={p[0]:p[1:] for p in (line.split() for line in text.splitlines()) if len(p)>=2}
  peers={r['public_key'] for r in self.journal.state['value']['tables']['clients_table'] if r['enabled']};out=[]
  for key in peers:
   ep=fields['endpoints'].get(key,['(none)'])[0];stamp=int(fields['latest-handshakes'].get(key,['0'])[0]);transfer=fields['transfer'].get(key,['0','0'])
   out.append({'publicKey':key,'endpoint':None if ep=='(none)' else ep,'latestHandshakeAt':datetime.datetime.fromtimestamp(stamp,datetime.timezone.utc).isoformat() if stamp else None,'transferRx':int(transfer[0]),'transferTx':int(transfer[1])})
  return out
 def owner_stats(self):
  if time.monotonic()-self.stats_cache[0]<5:return self.stats_cache[1]
  replies=self.fanout('stats');owners=[r['peers'] for r in replies.values() if r['owner']]
  result=owners[0] if len(owners)==1 else [];self.stats_cache=(time.monotonic(),result);return result
 def publish(self):
  state=self.status();atomic(Path(self.c['ready']),encoded({'protocol':'desk-vpn-sync-v1','node':self.c['node'],'observed_at':time.time(),'revision':state['revision'],'applied_revision':state['applied_revision'],'ready':self.initialized and state['revision']==state['applied_revision'] and not self.error}))
 def health(self):
  if not self.initialized:return False
  if time.monotonic()-self.health_cache[0]<10:return self.health_cache[1]
  try:ok=self.native.request('/api/session')[0]==200
  except Exception:ok=False
  self.health_cache=(time.monotonic(),ok);return ok
 def ui_status(self):
  nodes=[];rev=self.journal.state['revision']
  for node in self.c['members']:
   h=self.status() if node==self.c['node'] else self.heads.get(node)
   state='Unavailable' if not h else 'Applied' if h['applied_revision']==rev and h['revision']==rev and not h.get('apply_error') else 'Pending'
   nodes.append({'node':node,'state':state})
  count=sum(n['state']=='Applied' for n in nodes)
  return {'revision':rev,'nodes':nodes,'message':f'Revision {rev} · {count}/3 applied'+(' · Pending nodes catch up automatically' if count<3 else '')}
 def loop(self):
  while True:
   try:
    with self.lock:
     heads=self.refresh()
     if len(heads)>=2 and any(h.get('accepted') for h in heads.values()):
      self.choose(self.journal.state['value'],self.journal.state['revision']);self.apply()
     self.publish()
   except Exception:self.error='apply_failed';self.publish();LOG.warning('VPN reconciliation pending; no private data logged')
   self.event.wait(10);self.event.clear()

class Server(http.server.ThreadingHTTPServer):
 daemon_threads=True;request_queue_size=32
class Handler(http.server.BaseHTTPRequestHandler):
 protocol_version='HTTP/1.0'
 def log_message(self,*args):pass
 def send(self,code,body,headers=None):
  if not isinstance(body,bytes):body=encoded(body)
  self.send_response(code);self.send_header('Content-Length',str(len(body)));self.send_header('Cache-Control','no-store')
  self.send_header('Content-Type',{k.lower():v for k,v in (headers or {}).items()}.get('content-type','application/json'))
  for k,v in (headers or {}).items():
   if k.lower() in ('content-disposition','location'):self.send_header(k,v)
  self.end_headers();self.wfile.write(body)
 def run(self):
  self.connection.settimeout(25);cluster=self.server.cluster;c=cluster.c;path=urllib.parse.urlsplit(self.path).path
  length=int(self.headers.get('Content-Length','0'))
  if length<0 or length>LIMIT:raise Failure(413,'VPN request too large')
  raw=self.rfile.read(length)
  if self.server.kind=='peer':
   if self.command!='POST' or not path.startswith('/peer/') or self.client_address[0] not in c['members'].values():raise Failure(403,'Private cluster endpoint')
   stamp=self.headers.get('X-Desk-Time','');nonce=self.headers.get('X-Desk-Nonce','');sig=self.headers.get('X-Desk-Signature','')
   signed=('POST\n'+path+'\n'+stamp+'\n'+nonce+'\n'+hashlib.sha256(raw).hexdigest()).encode()
   if not stamp.isdigit() or abs(time.time()-int(stamp))>15 or not re.fullmatch('[0-9a-f]{32}',nonce) or not hmac.compare_digest(sig,hmac.new(cluster.key,signed,hashlib.sha256).hexdigest()):raise Failure(403,'Invalid peer authentication')
   with self.server.nonce_lock:
    self.server.nonces={n:t for n,t in self.server.nonces.items() if time.time()-t<20}
    if nonce in self.server.nonces or len(self.server.nonces)>1024:raise Failure(403,'Replayed peer request')
    self.server.nonces[nonce]=time.time()
   self.send(200,cluster.peer(path[6:],json.loads(raw)));return
  if path=='/health':
   ready=cluster.health();self.send(200 if ready else 503,{'ready':ready});return
  if self.server.kind=='native':
   if path!='/native/status' or self.client_address[0]!='127.0.0.1' or not hmac.compare_digest(self.headers.get('X-Desk-Backend',''),c['backend_token']):raise Failure(403,'Native endpoint')
   self.send(200,cluster.owner_stats());return
  if self.client_address[0] not in set(c['members'].values())|{'127.0.0.1'}:raise Failure(403,'Cluster ingress required')
  if not cluster.initialized:raise Failure(503,'VPN configuration recovery in progress')
  if path=='/vpn/api/desk-cluster/status':self.send(200,cluster.ui_status());return
  if not path.startswith('/vpn/'):raise Failure(404,'Unknown VPN route')
  if path.startswith('/vpn/_nuxt/'):
   body,content_type=asset(path);self.send(200,body,{'Content-Type':content_type});return
  nativepath=self.path[4:];route=path[4:]
  if route.startswith('/api/'):
   if self.command=='GET' and not READ.fullmatch(route):raise Failure(405,'This setting is managed by Desk Cluster')
   if self.command not in ('GET','POST','DELETE'):raise Failure(405,'Unsupported VPN method')
   if self.command!='GET':
    origin=self.headers.get('Origin');host=self.headers.get('Host')
    if not origin or urllib.parse.urlsplit(origin).netloc!=host or self.headers.get('Sec-Fetch-Site','same-origin') not in ('same-origin','none'):raise Failure(403,'Same-origin VPN update required')
    if not WRITE.fullmatch(route) or self.command=='DELETE' and not re.fullmatch(r'/api/client/[1-9][0-9]*',route):raise Failure(405,'This setting is managed by Desk Cluster')
    code,headers,body=cluster.mutate(nativepath,self.command,raw)
   else:
    with cluster.lock:
     cluster.apply();code,headers,body=cluster.native.request(nativepath)
  else:
   if self.command!='GET':raise Failure(405,'Unsupported VPN method')
   code,headers,body=cluster.native.raw(nativepath)
  self.send(code,body,headers)
 def handle_request(self):
  try:self.run()
  except database.Invalid as e:self.send(400,{'statusCode':400,'statusMessage':str(e),'message':str(e)})
  except (ValueError,KeyError,TypeError):self.send(400,{'statusCode':400,'statusMessage':'Invalid VPN configuration','message':'Invalid VPN configuration'})
  except Failure as e:self.send(e.code,{'statusCode':e.code,'statusMessage':e.message,'message':e.message})
  except (BrokenPipeError,ConnectionResetError):pass
  except Exception:self.send(503,{'statusCode':503,'statusMessage':'VPN request pending; refresh cluster status','message':'VPN request pending; refresh cluster status'})
 do_GET=do_POST=do_DELETE=do_PUT=do_PATCH=handle_request

def main():
 p=argparse.ArgumentParser();p.add_argument('config',type=Path);a=p.parse_args();c=json.loads(a.config.read_text())
 seed=json.loads(Path(c['seed']).read_text());database.validate(seed,seed)
 j=Journal(Path(c['journal']),Fernet(c['key'].encode()),c['node'],c['members'],base64.urlsafe_b64decode(c['key']));j.initialize(seed)
 cluster=Cluster(c,j,Native(c));previously_applied=j.state['applied_revision']>=0
 for kind,host,port in [('native','127.0.0.1',51822),('ui',c['members'][c['node']],51823),('peer',c['members'][c['node']],51855)]:
  server=Server((host,port),Handler);server.cluster=cluster;server.kind=kind;server.nonces={};server.nonce_lock=threading.Lock()
  if kind=='peer':
   context=ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER);context.minimum_version=ssl.TLSVersion.TLSv1_2;context.load_cert_chain(c['certificate'],c['tls_key']);server.socket=context.wrap_socket(server.socket,server_side=True)
  threading.Thread(target=server.serve_forever,daemon=True).start()
 # A returning node catches up before publishing VPN eligibility. A fresh seed
 # needs another matching member; an existing committed checkpoint can run alone.
 while not cluster.initialized:
  try:
   heads=cluster.refresh(False)
   if not previously_applied and sum(h['digest']==digest(j.state['value']) for h in heads.values())<2:
    raise Failure(503,'Waiting for another member to confirm the initial VPN seed')
   cluster.apply(True);cluster.initialized=True;cluster.publish()
  except Exception:
   cluster.error='startup_pending';cluster.publish();cluster.event.wait(3);cluster.event.clear()
 cluster.loop()
if __name__=='__main__':
 logging.basicConfig(level=logging.INFO,format='%(message)s');main()
