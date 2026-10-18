"""Exercise production proxy admission, framing and checkpoint ordering."""
import io,pathlib,sys,threading,tracemalloc,types,unittest
from concurrent.futures import ThreadPoolExecutor
from unittest.mock import patch
sys.path.insert(0,str(pathlib.Path(__file__).resolve().parents[1]/'files'))
import ha

class Response:
 status=200
 def __init__(self,size,ctype='application/json',known=True):
  self.remaining=size;self.length=size if known else None;self.ctype=ctype;self.reads=[]
 def getheader(self,key,default=None):return self.ctype if key=='Content-Type' else default
 def getheaders(self):return [('Content-Type',self.ctype),('Set-Cookie','native=x; Path=/; HttpOnly'),('Location','/devices.php')]
 def read1(self,n):
  self.reads.append(n);n=min(n,self.remaining);self.remaining-=n;return b'x'*n
 def read(self,n):return self.read1(n)

class Sink:
 def __init__(self):self.bytes=0;self.largest=0
 def write(self,b):self.bytes+=len(b);self.largest=max(self.largest,len(b))
 def flush(self):pass

class Tests(unittest.TestCase):
 def setUp(self):
  self.responses=[];self.closed=[];self.snapshots=[];self.forwarded=[]
  c=types.SimpleNamespace(members={'a':'127.0.0.1'},node='a',owned=lambda:True,ready=True,sync_state='synced',c={'api_token':'fixture'},term=[1,'a'],fanout=lambda *a:{'a':{},'b':{}},renew=lambda *a:True,snapshot=lambda:self.snapshots.append('checkpoint'))
  self.server=types.SimpleNamespace(cluster=c,ui_slots=threading.BoundedSemaphore(8),rewrite_slots=threading.BoundedSemaphore(2))
  outer=self
  class Connection:
   def __init__(self,*a,**kw):pass
   def request(self,*a,**kw):outer.forwarded.append(a)
   def getresponse(self):return outer.responses.pop(0)
   def close(self):outer.closed.append(True)
  p=patch.object(ha.http.client,'HTTPConnection',Connection);p.start();self.addCleanup(p.stop)
 def handler(self,method='GET'):
  h=types.SimpleNamespace(server=self.server,client_address=('127.0.0.1',1),path='/network/api/table_events.json',headers={},command=method,rfile=io.BytesIO(),wfile=Sink(),statuses=[],sent_headers=[],close_connection=False)
  h.send_response=h.statuses.append;h.send_header=lambda k,v:h.sent_headers.append((k,v));h.end_headers=lambda:None
  return h
 def run_response(self,r,method='GET'):
  self.responses.append(r);h=self.handler(method);ha.UI.handle_ui(h);return h
 def test_large_json_stream_has_constant_scratch_and_preserves_headers(self):
  r=Response(8*1024*1024);tracemalloc.start()
  try:h=self.run_response(r);_,peak=tracemalloc.get_traced_memory()
  finally:tracemalloc.stop()
  self.assertLess(peak,512*1024);self.assertEqual(h.statuses,[200]);self.assertEqual(h.wfile.bytes,8*1024*1024)
  self.assertLessEqual(h.wfile.largest,65536);self.assertTrue(h.close_connection)
  self.assertIn(('Content-Length',str(8*1024*1024)),h.sent_headers)
  self.assertIn(('Set-Cookie','native=x; Path=/network/; HttpOnly'),h.sent_headers)
  self.assertIn(('Location','/network/devices.php'),h.sent_headers)
 def test_known_oversize_rejected_before_success_headers(self):
  r=Response(ha.LIMIT+1);h=self.run_response(r)
  self.assertEqual(h.statuses,[503]);self.assertEqual(r.reads,[]);self.assertTrue(h.close_connection)
 def test_eight_stalled_large_json_clients_hold_chunks_not_whole_bodies(self):
  gate=threading.Event();entered=threading.Barrier(9);handlers=[self.handler() for _ in range(8)]
  self.responses=[Response(8*1024*1024) for _ in handlers]
  for h in handlers:
   original=h.wfile.write;first=[True]
   def slow(b,original=original,first=first):
    if first[0]:
     first[0]=False;entered.wait(timeout=5);gate.wait(5)
    original(b)
   h.wfile.write=slow
  tracemalloc.start()
  try:
   with ThreadPoolExecutor(max_workers=8) as pool:
    jobs=[pool.submit(ha.UI.handle_ui,h) for h in handlers]
    try:
     entered.wait(timeout=5);_,peak=tracemalloc.get_traced_memory()
     self.assertLess(peak,2*1024*1024)
    finally:gate.set()
    for f in jobs:f.result(5)
  finally:tracemalloc.stop()
  self.assertTrue(all(h.statuses==[200] and h.wfile.bytes==8*1024*1024 for h in handlers))
 def test_unknown_oversize_closes_without_second_http_response(self):
  r=Response(ha.LIMIT+1,known=False);h=self.run_response(r)
  self.assertEqual(h.statuses,[200]);self.assertEqual(h.wfile.bytes,ha.LIMIT);self.assertTrue(h.close_connection)
 def test_truncated_upstream_never_appends_a_503_to_stream(self):
  r=Response(ha.UI_CHUNK);r.length*=2;h=self.run_response(r)
  self.assertEqual(h.statuses,[200]);self.assertEqual(h.wfile.bytes,ha.UI_CHUNK);self.assertTrue(h.close_connection)
 def test_head_keeps_upstream_length_without_reading_body(self):
  r=Response(500000);h=self.run_response(r,'HEAD')
  self.assertEqual(h.statuses,[200]);self.assertEqual(h.wfile.bytes,0);self.assertEqual(r.reads,[])
  self.assertIn(('Content-Length','500000'),h.sent_headers)
 def test_real_http_parser_head_zero_remaining_keeps_origin_length(self):
  class Socket:
   def makefile(self,*a):return io.BytesIO(b'HTTP/1.1 200 OK\r\nContent-Length: 500000\r\nContent-Type: application/json\r\n\r\n')
  r=ha.http.client.HTTPResponse(Socket(),method='HEAD');r.begin();self.assertEqual(r.length,0)
  h=self.run_response(r,'HEAD');self.assertEqual(h.statuses,[200])
  self.assertIn(('Content-Length','500000'),h.sent_headers);self.assertEqual(h.wfile.bytes,0)
 def test_real_chunked_upstream_is_relayed_with_close_framing(self):
  class Socket:
   def makefile(self,*a):return io.BytesIO(b'HTTP/1.1 200 OK\r\nTransfer-Encoding: chunked\r\nContent-Type: application/json\r\n\r\n5\r\nhello\r\n3\r\nabc\r\n0\r\n\r\n')
  r=ha.http.client.HTTPResponse(Socket());r.begin();self.responses.append(r)
  h=self.handler();parts=[];h.wfile.write=parts.append;ha.UI.handle_ui(h)
  self.assertEqual(h.statuses,[200]);self.assertEqual(b''.join(parts),b'helloabc')
  self.assertTrue(h.close_connection);self.assertFalse(any(k in ('Content-Length','Transfer-Encoding') for k,v in h.sent_headers))
 def test_sse_long_lines_are_chunked_and_owner_loss_stops_relay(self):
  r=Response(20*ha.UI_CHUNK,'text/event-stream',known=False)
  held=iter([True,True,True,False]);self.server.cluster.owned=lambda:next(held,False)
  h=self.run_response(r)
  self.assertEqual(h.statuses,[200]);self.assertGreater(h.wfile.bytes,0);self.assertLess(h.wfile.bytes,20*ha.UI_CHUNK)
  self.assertTrue(all(n==ha.UI_CHUNK for n in r.reads))
 def test_request_overload_rejects_before_reading_body_and_does_not_take_peer_slots(self):
  for _ in range(8):self.server.ui_slots.acquire()
  h=self.handler();h.headers={'Content-Length':'2000000'}
  h.rfile=types.SimpleNamespace(read=lambda n:self.fail('Overloaded body was read'))
  ha.UI.handle_ui(h);self.assertEqual(h.statuses,[503]);self.assertTrue(h.close_connection)
  # Status probes stay outside the UI budget.
  self.server.cluster.public=lambda:{'state':'active'};probe=self.handler();probe.path='/network/desk/status'
  ha.UI.handle_ui(probe);self.assertEqual(probe.statuses,[200])
 def test_rewrite_budget_is_held_through_slow_client_and_released_on_error(self):
  gate=threading.Event();entered=threading.Barrier(3)
  def slow(b):entered.wait(timeout=5);gate.wait(5);raise BrokenPipeError()
  first=[self.handler(),self.handler()]
  for h in first:h.wfile.write=slow
  self.responses=[Response(100,'text/html'),Response(100,'text/html'),Response(100,'text/html')]
  with ThreadPoolExecutor(max_workers=2) as pool:
   jobs=[pool.submit(ha.UI.handle_ui,h) for h in first]
   try:
    entered.wait(timeout=5);h=self.handler();ha.UI.handle_ui(h);self.assertEqual(h.statuses,[503])
   finally:gate.set()
   for f in jobs:f.result(5)
  h=self.run_response(Response(100,'text/html'));self.assertEqual(h.statuses,[200])
  for _ in range(8):self.assertTrue(self.server.ui_slots.acquire(blocking=False))
 def test_large_text_is_rejected_without_rewrite(self):
  with patch.object(ha,'rewrite_text') as rewrite:h=self.run_response(Response(ha.UI_TEXT_LIMIT+1,'text/html'))
  self.assertEqual(h.statuses,[503]);rewrite.assert_not_called()
 def test_mutation_body_then_checkpoint_then_success_ack(self):
  r=Response(100);order=[];read=r.read
  def read_body(n):
   b=read(n)
   if not b:order.append('body_complete')
   return b
  r.read=read_body;self.responses.append(r);h=self.handler('POST')
  self.server.cluster.snapshot=lambda:order.append('checkpoint')
  h.send_response=lambda status:(order.append('ack'),h.statuses.append(status))
  ha.UI.handle_ui(h)
  self.assertEqual(order,['body_complete','checkpoint','ack']);self.assertEqual(h.statuses,[200])
 def test_failed_mutation_does_not_checkpoint(self):
  r=Response(100);r.status=400;h=self.run_response(r,'POST')
  self.assertEqual(h.statuses,[400]);self.assertEqual(self.snapshots,[])
 def test_busy_mutation_is_rejected_before_native_write(self):
  for _ in range(2):self.server.rewrite_slots.acquire()
  h=self.run_response(Response(100),'POST')
  self.assertEqual(h.statuses,[503]);self.assertEqual(self.forwarded,[]);self.assertEqual(self.snapshots,[])
 def test_mutation_sse_releases_reserved_buffer_and_keeps_async_semantics(self):
  h=self.run_response(Response(100,'text/event-stream',known=False),'POST')
  self.assertEqual(h.statuses,[200]);self.assertEqual(self.snapshots,[])
  for _ in range(2):self.assertTrue(self.server.rewrite_slots.acquire(blocking=False))
 def test_checkpoint_failure_preserves_pending_replication_header(self):
  def fail():raise ha.Unavailable('Fixture quorum unavailable')
  self.server.cluster.snapshot=fail
  with self.assertLogs(ha.LOG,'WARNING'):h=self.run_response(Response(100),'POST')
  self.assertEqual(h.statuses,[200]);self.assertIn(('X-Desk-Network-Sync','pending'),h.sent_headers)
if __name__=='__main__':unittest.main()
