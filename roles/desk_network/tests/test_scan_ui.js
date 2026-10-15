const vm=require('node:vm'),fs=require('node:fs'),assert=require('node:assert/strict'),path=require('node:path');
const listeners={},values={},timers=[];let posts=0,phase='queued';
const nodes={};for(const id of ['desk-network-scan','desk-network-discovery','desk-discovery-freshness','desk-discovery-owner','desk-discovery-arp','desk-discovery-result','desk-discovery-mdns','desk-discovery-age'])nodes[id]={style:{},disabled:false,textContent:'',open:false,addEventListener:(name,fn)=>listeners[id+':'+name]=fn};
const data=()=>({protocol:'desk-network-discovery-v1',owner:'deskb1x',scans:{arp:{phase,worker:'deskb3x',devices:22,new_devices:1,ip_changes:2,wall_seconds:4.2},mdns:{phase:'cached'}},inventory:{state:'stale',age_seconds:600}});
const context={document:{hidden:false,getElementById:id=>nodes[id],addEventListener:(name,fn)=>listeners[name]=fn},fetch:async(url,options)=>{if(options?.method==='POST'){posts++;return {ok:true}}return {ok:true,json:async()=>data()}},AbortSignal:{timeout:()=>null},setTimeout:fn=>{timers.push(fn);return timers.length},clearTimeout(){},console};
vm.runInNewContext(fs.readFileSync(path.join(__dirname,'../files/scan-ui.js'),'utf8'),context);
const tick=()=>new Promise(resolve=>setImmediate(resolve));
(async()=>{
 await tick();assert.equal(nodes['desk-network-scan'].disabled,true);assert.equal(nodes['desk-network-scan'].textContent,'Queued');assert.match(nodes['desk-discovery-age'].textContent,/last known presence/);
 phase='running';await timers.pop()();assert.match(nodes['desk-network-scan'].textContent,/Scanning/);assert.match(nodes['desk-discovery-arp'].textContent,/deskb3x/);
 phase='importing';await timers.pop()();assert.equal(nodes['desk-network-scan'].disabled,true);assert.match(nodes['desk-network-scan'].textContent,/Importing/);
 phase='complete';await timers.pop()();assert.equal(nodes['desk-network-scan'].disabled,false);assert.match(nodes['desk-discovery-result'].textContent,/1 new, 2 IP changes/);
 await listeners['desk-network-scan:click']();await tick();assert.equal(posts,1);assert.equal(nodes['desk-network-discovery'].open,true);
 context.document.hidden=true;const before=timers.length;await listeners.visibilitychange();assert.equal(timers.length,before);
 console.log('PASS queued/running/importing/completed states, stale-data labeling, scan action and hidden-tab polling');
})().catch(e=>{console.error(e);process.exitCode=1});
