"""Verified keep-alive, bounded channels and uncertain POST cleanup."""
import concurrent.futures,http.client,http.server,pathlib,ssl,sys,threading,unittest
sys.path.insert(0,str(pathlib.Path(__file__).resolve().parents[1]/'files'))
from peer_http import PeerHTTP
import test_peer_tls as tls

@unittest.skipUnless(tls.HAVE_TLS_FIXTURE,'cryptography is required for local TLS fixtures')
class PoolTests(unittest.TestCase):
 def setUp(self):
  self.fixture=tls.PeerTlsResumption();self.fixture.setUp();self.addCleanup(self.fixture.doCleanups)
  self.connections=[];self.posts=[];self.entered=threading.Event();self.release=threading.Event();self.addCleanup(self.release.set)
  outer=self
  class Handler(http.server.BaseHTTPRequestHandler):
   protocol_version='HTTP/1.1'
   def setup(self):super().setup();outer.connections.append(self.connection)
   def log_message(self,*args):pass
   def do_POST(self):
    self.rfile.read(int(self.headers.get('Content-Length',0)));outer.posts.append(self.path)
    if self.path=='/drop':self.close_connection=True;self.connection.close();return
    if self.path=='/slow':outer.entered.set();outer.release.wait(3)
    body=b'x'*1025 if self.path=='/large' else b'{"ok":true}'
    self.send_response(200);self.send_header('Content-Length',str(len(body)));self.end_headers();self.wfile.write(body)
  self.fixture.Handler=Handler;self.server=self.fixture.serve(self.fixture.good);self.port=self.server.server_address[1]
  self.client=PeerHTTP(ssl.create_default_context(cafile=self.fixture.good[0]),1024,capacity=1);self.addCleanup(self.client.close)
 def post(self,path='/ok',channel='control',timeout=2):return self.client.post('127.0.0.1',self.port,path,b'{}',{},timeout,channel)
 def test_reuses_one_verified_connection_and_keeps_response_bound(self):
  for _ in range(4):self.assertEqual(self.post(),(200,b'{"ok":true}'))
  self.assertEqual(len(self.connections),1);self.assertEqual(self.client.public()['connections_reused'],3)
  with self.assertRaises(ValueError):self.post('/large')
  self.assertEqual(self.post()[0],200);self.assertEqual(len(self.connections),2)
 def test_checkpoint_cannot_consume_control_channel_capacity(self):
  with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
   slow=pool.submit(self.post,'/slow','checkpoint');self.assertTrue(self.entered.wait(2))
   self.assertEqual(self.post()[0],200)
   with self.assertRaises(TimeoutError):self.post('/ok','checkpoint',.05)
   self.release.set();self.assertEqual(slow.result(2)[0],200)
  self.assertEqual(self.posts.count('/ok'),1)
 def test_failed_post_is_never_retried_and_next_call_reconnects(self):
  with self.assertRaises((OSError,http.client.HTTPException)):self.post('/drop')
  self.assertEqual(self.posts,['/drop']);self.assertEqual(self.post()[0],200)
  self.assertEqual(len(self.connections),2)
 def test_peer_restart_discards_stale_socket_then_reconnects_with_same_pin(self):
  import socket
  self.post();self.server.shutdown();self.server.server_close()
  for conn in self.connections:
   try:conn.shutdown(socket.SHUT_RDWR);conn.close()
   except OSError:pass
  self.server=self.fixture.serve(self.fixture.good,self.port)
  with self.assertRaises((OSError,http.client.HTTPException)):self.post()
  self.assertEqual(self.post()[0],200)
 def test_expired_idle_connection_reconnects_and_close_disallows_new_requests(self):
  self.post();self.client.idle_seconds=0;self.post();self.assertEqual(len(self.connections),2)
  self.client.close()
  with self.assertRaises(OSError):self.post()
 def test_pin_and_hostname_are_verified_on_new_connections(self):
  import ipaddress
  from cryptography import x509
  other=self.fixture.mint(self.fixture.dir,'other',x509.IPAddress(ipaddress.ip_address('127.0.0.1')))
  wrong=self.fixture.mint(self.fixture.dir,'named',x509.DNSName('peer.example'))
  for certificate,port in [(other[0],self.port),(wrong[0],self.fixture.serve(wrong).server_address[1])]:
   client=PeerHTTP(ssl.create_default_context(cafile=certificate),1024);self.addCleanup(client.close)
   with self.assertRaises(ssl.SSLCertVerificationError):client.post('127.0.0.1',port,'/ok',b'{}',{},1)
 def test_old_http10_server_remains_compatible_without_persistent_reuse(self):
  self.fixture.Handler.protocol_version='HTTP/1.0'
  self.post();self.post();self.assertEqual(len(self.connections),2)
 def test_every_persistent_request_still_checks_hmac_and_replay(self):
  import hashlib,hmac,json,time
  from types import SimpleNamespace
  key=b'offline-test-key';body=b'{}';path='/peer/status';stamp=str(int(time.time()));nonce='a'*32
  self.server.RequestHandlerClass=tls.ha.Peer
  self.server.cluster=SimpleNamespace(members={'a':'127.0.0.1'},key=key,authlock=threading.Lock(),nonces={},peer=lambda *args:{'ok':True})
  signed=('POST\n'+path+'\n'+stamp+'\n'+nonce+'\n'+hashlib.sha256(body).hexdigest()).encode()
  headers={'X-Desk-Node':'a','X-Desk-Time':stamp,'X-Desk-Nonce':nonce,'X-Desk-Signature':hmac.new(key,signed,hashlib.sha256).hexdigest()}
  def send(h):return self.client.post('127.0.0.1',self.port,path,body,h,2)[0]
  self.assertEqual(send(headers),200)
  self.assertEqual(send(headers),409)  # Replay on the same authenticated connection.
  self.assertEqual(send(dict(headers,**{'X-Desk-Nonce':'b'*32,'X-Desk-Signature':'invalid'})),409)
if __name__=='__main__':unittest.main()
