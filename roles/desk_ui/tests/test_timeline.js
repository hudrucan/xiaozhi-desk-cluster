'use strict';
const fs=require('node:fs'),vm=require('node:vm'),assert=require('node:assert/strict');
const path=require('node:path');
const source=fs.readFileSync(path.join(__dirname,'../templates/index.html.j2'),'utf8');
const functions=source.slice(source.indexOf('function timelineInfo'),source.indexOf('function renderEvents'));
const members=[1,2,3].map(i=>({id:`deskb${i}x`,management:`192.168.1.${100+i}`}));
const state={snapshot:{events:[]},openEvents:new Set()},operationState={history:[],latest:null,observedAt:Date.now(),unavailable:false};
const context=vm.createContext({membership:members,state,operationState,Date,Set,Map,Object,Number,Math,String,icon:()=>'<svg></svg>',esc:v=>String(v).replaceAll('<','&lt;').replaceAll('>','&gt;').replaceAll('"','&quot;')});
vm.runInContext(functions,context);
const base=Math.floor(Date.now()/1000)-200;
const ownership=(id,node,message,seconds)=>({id,node,message,kind:'ownership',subject:'dns',at:new Date((base+seconds)*1000).toISOString(),from:message.endsWith('released')?'Owner':'Standby',to:message.endsWith('released')?'Standby':'Owner'});
let checks=0;function check(name,fn){fn();checks++;console.log('PASS',name);}
check('VIP release/acquire renders source, destination and observed interval',()=>{
 state.snapshot.events=[ownership('a','deskb2x','DNS VIP released',0),ownership('b','deskb1x','DNS VIP acquired',3)];
 const group=context.clusterTimelineGroups()[0];assert.equal(group.title,'DNS owner changed');assert.equal(group.handover.interval,3000);
 const markup=context.timelineMarkup(group);assert.match(markup,/192\.168\.1\.102/);assert.match(markup,/192\.168\.1\.101/);assert.match(markup,/Observed handover interval: 3s/);
});
check('Acquisition alone does not fabricate old owner or timing',()=>{
 const group=context.timelineGroups([ownership('a','deskb1x','DNS VIP acquired',3)])[0];assert.equal(context.vipHandover(group),null);
});
check('Conflicting owner acquisitions do not collapse or claim normal handover',()=>{
 const group=context.timelineGroups([ownership('a','deskb2x','DNS VIP released',0),ownership('b','deskb1x','DNS VIP acquired',2),ownership('c','deskb3x','DNS VIP acquired',3)])[0];
 assert.equal(group.steps.length,3);assert.equal(context.vipHandover(group),null);
});
const record={id:'a'.repeat(32),node:'deskb3x',action:'reboot',started:base,events:[{state:'accepted',at:base},{state:'starting',at:base+110},{state:'services_ready',at:base+120},{state:'rejoined',at:base+148}]};
check('Completed operation retains elapsed time and missing stages stay unconfirmed',()=>{
 operationState.history=context.operationHistory([record]);state.snapshot.events=[];const group=context.clusterTimelineGroups()[0];
 assert.equal(group.title,'deskb3x rejoined the cluster');assert.match(group.subtitle,/2m 28s/);
 const markup=context.timelineMarkup(group);assert.match(markup,/Sync<span class="dc-sr"> · not separately observed/);assert.doesNotMatch(markup,/Sync<span class="dc-sr"> · observed complete/);
});
check('Active drain is prioritized ahead of newer collector events',()=>{
 const current={...record,id:'b'.repeat(32),events:[{state:'draining',at:base+1}]};operationState.history=context.operationHistory([current]);operationState.latest={...current,state:'draining',expires:Math.floor(Date.now()/1000)+300};
 state.snapshot.events=[{id:'new',node:'deskb1x',kind:'node',subject:'telemetry_collector',message:'Telemetry collector restarted',at:new Date().toISOString()}];
 const group=context.clusterTimelineGroups()[0];assert.equal(group.id,'operation:'+current.id);assert.equal(group.active,true);assert.match(context.timelineMarkup(group),/in progress/);
});
check('Unavailable operation API does not imply current progress',()=>{
 operationState.unavailable=true;const group=context.operationTimelineGroups()[0];assert.equal(group.active,false);assert.match(context.timelineMarkup(group),/Cached operation observations/);assert.doesNotMatch(context.timelineMarkup(group),/ · in progress/);operationState.unavailable=false;
});
check('Legacy completion has one explicit milestone and no invented stages',()=>{
 operationState.history=[];operationState.latest={id:'c'.repeat(32),node:'deskb1x',action:'reboot',state:'rejoined',started:base,finished:base+140,expires:base+3600};
 const group=context.operationTimelineGroups()[0];assert.equal(group.steps.length,1);assert.match(group.subtitle,/2m 20s/);
});
check('Invalid peer display data is dropped without injecting markup',()=>{
 assert.equal(context.operationHistory([{...record,node:'<script>'}]).length,0);assert.equal(context.operationHistory([{...record,id:[record.id]}]).length,0);
 const safe=context.operationHistory([{...record,events:[{state:'<script>',at:base}]}]);assert.equal(safe.length,0);
});
check('Explicitly observed reboot replaces duplicate generic reboot row',()=>{
 operationState.latest=null;operationState.history=context.operationHistory([record]);state.snapshot.events=[{id:'boot',node:'deskb3x',kind:'node',message:'Node reboot detected',at:new Date((base+111)*1000).toISOString()}];assert.equal(context.clusterTimelineGroups().length,1);
});
check('Missing reboot observation preserves the generic node event',()=>{
 operationState.history=context.operationHistory([{...record,events:[{state:'accepted',at:base}]}]);assert.equal(context.clusterTimelineGroups().length,2);
});
check('Recovery needing attention identifies the actual pending dependency',()=>{
 const pending={...record,id:'d'.repeat(32),events:[{state:'starting',at:base+110},{state:'services_ready',at:base+120},{state:'needs_attention',at:base+150}]};
 operationState.history=context.operationHistory([pending]);operationState.latest={...pending,state:'needs_attention',expires:Math.floor(Date.now()/1000)+60,readiness:{services:true,configuration:false,waiting:['xiaozhi config']}};
 const group=context.operationTimelineGroups()[0];assert.match(context.timelineMarkup(group),/Waiting for Xiaozhi config/);assert.match(context.timelineMarkup(group),/Sync<span class="dc-sr"> · in progress/);
});
console.log(`${checks} HA timeline regressions passed`);
