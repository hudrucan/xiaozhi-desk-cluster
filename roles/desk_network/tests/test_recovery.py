import json,pathlib,re,shutil,subprocess,unittest
ROOT=pathlib.Path(__file__).resolve().parents[1]
RAW=(ROOT/'files/recovering.http').read_bytes()
class Checks(unittest.TestCase):
 def test_recovery_response_preserves_unavailable_status_and_framing(self):
  head,body=RAW.split(b'\r\n\r\n',1);headers=head.decode().split('\r\n')
  self.assertEqual(headers[0],'HTTP/1.1 503 Service Unavailable')
  self.assertIn('Cache-Control: no-store',headers);self.assertIn('Retry-After: 3',headers)
  self.assertIn('Content-Length: '+str(len(body)),headers)
  self.assertLess(len(RAW),8192)
  self.assertNotRegex(body.decode(),r'<(?:script|link)[^>]*(?:src|href)=')
 def test_native_response_override_is_limited_to_navigation(self):
  config=(ROOT/'templates/backend.cfg.j2').read_text()
  self.assertIn('errorfile 503 /etc/desk-network/recovering.http',config)
  gate=next(line for line in config.splitlines() if 'set-var(txn.network_document)' in line)
  self.assertIn('{ method GET HEAD }',gate);self.assertIn('text/html',gate)
  pattern=re.search(r'path_reg (\S+) ',gate).group(1)
  self.assertRegex('/network/devices.php',pattern);self.assertRegex('/network/',pattern)
  for path in ('/network/php/server/query_json.php','/network/desk/status','/network/server/devices/search'):
   self.assertNotRegex(path,pattern)
  self.assertIn('if { status 503 } { var(txn.network_document) -m bool }',config)
 def test_poll_is_serial_bounded_and_reloads_only_when_active(self):
  if not shutil.which('node'):self.skipTest('Node.js unavailable')
  script=re.search(r'<script>(.*?)</script>',RAW.decode(),re.S).group(1)
  harness=r"""const vm=require('vm'),assert=require('assert');
async function scenario(reply){
 const elements={};for(const id of ['status','detail','elapsed','retry'])elements[id]={textContent:'',disabled:false,addEventListener:(name,fn)=>elements[id].click=fn};
 let calls=0,reloads=0,timers=[],intervals=[],release;
 const pending=new Promise(resolve=>release=resolve);
 const context={Date,AbortController,Error,document:{hidden:false,getElementById:id=>elements[id]},location:{reload:()=>reloads++},setTimeout:(fn,ms)=>{timers.push({fn,ms});return timers.length},clearTimeout:()=>{},setInterval:fn=>intervals.push(fn),fetch:async(path,opts)=>{calls++;assert.equal(path,'/network/desk/status');assert.equal(opts.cache,'no-store');assert(opts.signal);return pending;}};
 vm.runInNewContext(SCRIPT,context);await elements.retry.click();assert.equal(calls,1,'Duplicate checks overlap');assert(timers.some(t=>t.ms===4000));
 release(reply);await new Promise(resolve=>setImmediate(resolve));
 return {calls,reloads,timers,elements,intervals};
}
(async()=>{
 let s=await scenario({ok:false});assert.equal(s.reloads,0);assert(s.timers.some(t=>t.ms===3000));assert.equal(s.elements.retry.disabled,false);
 s=await scenario({ok:true,json:async()=>({state:'starting'})});assert.equal(s.reloads,0);
 s=await scenario({ok:true,json:async()=>({state:'active'})});assert.equal(s.reloads,1);assert.equal(s.elements.retry.disabled,true);
})().catch(e=>{console.error(e);process.exit(1)});
""".replace('SCRIPT',json.dumps(script))
  subprocess.run(['node','-e',harness],check=True,capture_output=True)
if __name__=='__main__':unittest.main()
