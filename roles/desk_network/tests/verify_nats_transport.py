"""Real isolated NATS broker, fake providers; no cluster credentials or scans."""
import asyncio,json,pathlib,subprocess,tempfile,socket,time,os,sys
from unittest.mock import patch,AsyncMock
files=pathlib.Path(__file__).resolve().parents[1]/'files'
if files.exists():sys.path.insert(0,str(files))
import nats
from scan_service import Service
from scan_protocol import pack,unpack
from install_nats_scan import candidate
NODES=('deskb1x','deskb2x','deskb3x')
async def test(url,secret):
 services=[];clients=[];errors=[]
 async def err(e):errors.append(type(e).__name__)
 async def connect(cred):
  c=await nats.connect(servers=[url],user=cred['user'],password=cred['password'],error_cb=err,connect_timeout=1,max_reconnect_attempts=0);clients.append(c);return c
 try:
  for node in NODES:
   s=Service({'node':node,'key':'fake-signing-key','members':dict.fromkeys(NODES,'127.0.0.1')})
   s.bus=await connect(secret['coordinators'][node]);s.worker_bus=await connect(secret['workers'][node])
   s.owner=AsyncMock(return_value=[3,'deskb1x']);s.telemetry=AsyncMock(return_value={'cpu':10,'vip':{}})
   async def scan(msg,service=s):service.enqueue(msg)
   await s.worker_bus.subscribe('desk.network.scan.'+node,cb=scan)
   await s.worker_bus.subscribe('desk.network.status.'+node,cb=s.status);await s.worker_bus.flush()
   services.append(s)
  with patch('scan_service.save_json'),patch('scan_service.interface_address',return_value='192.168.1.101'),patch('scan_service.resolve',return_value=({'192.168.1.115':('zerow.local',120)},{'sent':4,'received':2})):
   result=await services[0].dispatch({'192.168.1.115':'02:11:22:33:44:55'})
   assert result['stats']['worker'] in ('deskb2x','deskb3x'),result
   assert result['found']['192.168.1.115'][0]=='zerow.local'
   print('PASS real NATS node-directed job/result and off-owner selection')
  with patch('scan_service.save_json'),patch('scan_service.arp_scan',return_value=({'192.168.1.1':['02:11:22:33:44:01','Router']},{'sent':0,'received':1})):
   result=await services[0].dispatch({},'arp')
   assert result['stats']['worker'] in ('deskb2x','deskb3x'),result
   assert result['found']['192.168.1.1'][0]=='02:11:22:33:44:01'
   print('PASS real NATS directed ARPSCAN job/result')
  # Bad signature must not obtain a reply or run a provider.
  c=services[0].bus;reply='desk.network.reply.deskb1x.bad';got=[]
  async def noop(msg):pass
  sub=await c.subscribe(reply,cb=noop)
  before=len(services[1].seen)
  await c.publish('desk.network.scan.deskb2x',b'{"signature":"bad","body":{}}',reply=reply);await c.flush();await asyncio.sleep(.1)
  assert len(services[1].seen)==before
  await sub.unsubscribe();print('PASS invalid signed job rejected')
  count=len(errors)
  # Worker cannot publish jobs, subscribe to another worker or see voice topics.
  w=services[1].worker_bus
  await w.publish('desk.network.scan.deskb3x',b'{}');await w.subscribe('desk.network.scan.deskb3x',cb=services[1].status);await w.subscribe('xiaozhi.>',cb=services[1].status)
  await w.flush();await asyncio.sleep(.15)
  assert len(errors)>=count+3,errors
  # Coordinator cannot observe Xiaozhi traffic either.
  await c.subscribe('xiaozhi.>',cb=services[0].status);await c.flush();await asyncio.sleep(.1)
  assert len(errors)>=count+4
  print('PASS actual NATS worker/coordinator ACL denies foreign subjects')
 finally:
  for c in clients:
   if not c.is_closed:await c.close()

def main():
 with tempfile.TemporaryDirectory(prefix='desk-scan-nats-') as td:
  with socket.socket() as s:s.bind(('127.0.0.1',0));port=s.getsockname()[1]
  secret={'coordinators':{n:{'user':'c-'+n,'password':'fake-c-'+n} for n in NODES},'workers':{n:{'user':'w-'+n,'password':'fake-w-'+n} for n in NODES}}
  text='listen: "127.0.0.1:'+str(port)+'"\nmax_payload: 64KB\nauthorization {\n  users: [\n  ]\n}\n'
  cfg=pathlib.Path(td)/'nats.conf';cfg.write_text(candidate(text,secret));cfg.chmod(0o600)
  process=subprocess.Popen(['/usr/sbin/nats-server','-c',str(cfg)],stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)
  try:
   for _ in range(30):
    try:
     with socket.create_connection(('127.0.0.1',port),timeout=.1):break
    except OSError:time.sleep(.05)
   assert process.poll() is None,'Isolated NATS exited'
   asyncio.run(test('nats://127.0.0.1:'+str(port),secret))
  finally:
   process.terminate()
   try:process.wait(timeout=3)
   except subprocess.TimeoutExpired:process.kill();process.wait()
if __name__=='__main__':main()
