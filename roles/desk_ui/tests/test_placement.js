const fs=require('fs'),vm=require('vm'),assert=require('assert');
const source=fs.readFileSync(require('path').join(__dirname,'../templates/index.html.j2'),'utf8');
const state={snapshot:{nodes:[]},unavailable:false};const ctx=vm.createContext({state,Date,Number,String,icon:()=>'',esc:String});vm.runInContext(source.slice(source.indexOf('function blank'),source.indexOf('function configSummary')),ctx);
const now=new Date().toISOString();let node={id:'deskb1x',ip:'192.168.1.101',observed_at:now,online:true,vip:{vpn:true},service_roles:{network:'trial'},activity:{}};state.snapshot.nodes=[{id:'deskb2x',online:true,observed_at:now,vip:{xiaozhi:true},monitoring_preferred:'deskb1x'},node];assert.match(ctx.roles(node),/Monitoring preferred/);assert.match(ctx.roles(node),/Network trial/);node={...node,observed_at:new Date(Date.now()-30000).toISOString()};assert.doesNotMatch(ctx.roles(node),/Network trial|Monitoring preferred/);state.snapshot.nodes[0].observed_at=node.observed_at;assert.doesNotMatch(ctx.roles({...node,observed_at:now}),/Monitoring preferred/);console.log('PASS fresh preference, truthful canary and stale-role exclusion');

state.snapshot.nodes.push({id:'deskb3x',online:true,observed_at:now,vip:{xiaozhi:true},monitoring_preferred:'deskb3x'}); state.snapshot.nodes[0].observed_at=now; assert.doesNotMatch(ctx.roles({...node,observed_at:now}),/Monitoring preferred/); console.log('PASS conflicting UI owners suppress preference');

vm.runInContext(source.slice(source.indexOf('function currentNetwork'),source.indexOf('function detailMarkup')),ctx);
const ready={...node,online:true,observed_at:now,network:{state:'active',sync:'synced',observed_at:now,subnet:'192.168.1.0/24',scan_minutes:5,voter_ready:true,revision:{term:17,node:'deskb1x',sequence:5}}};
const standby={...ready,id:'deskb2x',network:{...ready.network,state:'standby'}};state.snapshot.nodes=[ready,standby];
assert.equal(ctx.networkDetail(ready).label,'Synced');assert.equal(ctx.networkDetail(standby).role,'Network standby');assert.match(ctx.networkDetail(standby).scope,/Cold standby/);
standby.network={...standby.network,revision:{term:17,node:'deskb1x',sequence:4}};
assert.equal(ctx.networkDetail(standby).label,'Pending');
standby.network={...standby.network,voter_ready:false};assert.equal(ctx.networkDetail(standby).label,'Warming up');
standby.network={...ready.network,observed_at:new Date(Date.now()-16000).toISOString()};assert.equal(ctx.networkDetail(standby).label,'Unknown');
console.log('PASS Network collector, standby, checkpoint lag, restart warming and stale exclusion');

Object.assign(ctx,{networkEnabled:true,nodeControlEnabled:false,finite:Number.isFinite});
vm.runInContext(source.slice(source.indexOf('function detailMarkup'),source.indexOf('function render(){')),ctx);
const detailNode={...ready,vip:{xiaozhi:false,dns:false,vpn:true},activity:{asr:0,llm:0,tts:0},configuration:{},sessions:{},components:{dns:'ready'}};
state.snapshot.nodes=[detailNode];const markup=ctx.detailMarkup(detailNode);
assert.match(markup,/Network owner/);assert.match(markup,/scan every 5 min/);assert.match(markup,/checkpoint 17.5/);assert.match(markup,/href="\/network\/">Open network/);
assert.match(markup,/Network<br>/);assert.match(markup,/Synced/);
const cold={...detailNode,id:'deskb2x',network:{...detailNode.network,state:'standby'}};assert.match(ctx.detailMarkup(cold),/Network standby/);assert.match(ctx.detailMarkup(cold),/Cold standby/);
ctx.networkEnabled=false;assert.doesNotMatch(ctx.detailMarkup(detailNode),/Network<br>|Open network|Network owner/);
console.log('PASS Network details render in ownership, configuration and service link; hidden when disabled');

ctx.networkEnabled=true;
assert.match(ctx.detailMarkup({...detailNode,network:{...detailNode.network,scan_minutes:1,scan_schedule:'*/1 * * * *'}}),/scan every 1 min/);
assert.match(ctx.detailMarkup({...detailNode,network:{...detailNode.network,scan_minutes:0,scan_schedule:'0 3 * * *'}}),/schedule 0 3 \* \* \*/);
console.log('PASS actual user cron and nonuniform schedule in Network details');
