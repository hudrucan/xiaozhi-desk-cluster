const test=require('node:test'),assert=require('node:assert/strict'),fs=require('node:fs'),vm=require('node:vm'),path=require('node:path');
const file=path.resolve(__dirname,'../files/desk_cluster/thumbnail-focus.js');
function setup(){const listeners={},posts=[];let id=0;const window={addEventListener:(n,fn)=>listeners[n]=fn};const hass={callWS:msg=>{posts.push(msg.value);return Promise.resolve({ok:true})}};const location={origin:'http://home',pathname:'/home/overview'};vm.runInNewContext(fs.readFileSync(file,'utf8'),{window,location,document:{addEventListener:()=>{},documentElement:{setAttribute:()=>{}}},crypto:{getRandomValues:bytes=>{bytes.fill(++id);return bytes;}},URL,JSON,Date,requestAnimationFrame:()=>{},setInterval:()=>{}});return {posts,location,event:(detail)=>listeners['media-browsed']({detail,composedPath:()=>[{hass}]}),leave:()=>listeners['location-changed'](),hide:()=>listeners.pagehide()};}
const entry='camera',date='2026-10-17';const folder='media-source://tapo_control/tapo_control/?'+new URLSearchParams({entry,date});
test('native current children receive one tab scope, same-folder heartbeat is stable',()=>{const s=setup(),child={thumbnail:'/api/desk_cluster/thumbnail/camera/20261017/1792112400/1792112460'};s.event({ids:[{media_content_id:folder}],current:{children:[child]}});const u=new URL(child.thumbnail,'http://home');assert.equal(u.searchParams.get('sequence'),'1');assert.match(u.searchParams.get('client'),/^[a-f0-9]{32}$/);assert.match(u.searchParams.get('scope'),/^[a-f0-9]{32}$/);s.event({ids:[{media_content_id:folder}],current:{children:[child]}});assert.equal(s.posts.at(-1).sequence,1);});
test('leaving date cancels old scope, delayed requests keep old sequence',()=>{const s=setup();s.event({ids:[{media_content_id:folder}]});s.leave();assert.equal(s.posts.at(-1).sequence,2);assert.equal(s.posts.at(-1).folder,null);s.hide();assert.equal(s.posts.at(-1).sequence,3);});
test('different parts of one date get different scope and exact bounds',()=>{const s=setup();s.event({ids:[{media_content_id:folder}]});s.event({ids:[{media_content_id:folder+'&startDate=1792112400&endDate=1792112460'}]});assert.equal(s.posts.at(-1).sequence,2);assert.equal(s.posts.at(-1).folder.lower,1792112400);});

test('native folder result is scoped even before HA updates browser history',()=>{const s=setup();s.event({ids:[{media_content_id:folder}]});const next=folder+'&startDate=1792112400&endDate=1792112460';s.event({ids:[{media_content_id:next}]});const child={thumbnail:'/api/desk_cluster/thumbnail/camera/20261017/1792112400/1792112460'};s.event({ids:[{media_content_id:next}],current:{children:[child]}});assert.match(child.thumbnail,/scope=/);assert.equal(s.posts.at(-1).folder.lower,1792112400);});

// Native image results must not leave an endless spinner or expose a recycled
// row's old JPEG as the newly requested clip.
const stateSource=fs.readFileSync(file,'utf8').match(/function imageState\([\s\S]*?\n  }/)[0];
const imageState=vm.runInNewContext('('+stateSource+')',{atob});
test('unresolved thumbnail loads, recycled row does not expose a different clip',()=>{
  assert.equal(imageState('',0,400,true,'',false),'loading');
  assert.equal(imageState('data:image/jpeg;base64,old',0,400,false,'data:image/jpeg;base64,old',false),'loading');
  assert.equal(imageState('data:image/jpeg;base64,new',0,400,false,'data:image/jpeg;base64,old',false),'ready');
});
test('cached JPEG on back remains visible without Resource Timing history',()=>{
  const jpeg='background-image:url(data:image/jpeg;base64,cached)';
  assert.equal(imageState(jpeg,0,400,true,jpeg,false),'ready');
  assert.equal(imageState(jpeg,0,46000,true,jpeg,false),'ready');
});
test('placeholder and timeout stop spinner, late JPEG recovers',()=>{
  assert.equal(imageState('data:image/svg+xml;base64,fallback',0,400,true,'',false),'unavailable');
  assert.equal(imageState('',0,45000,true,'',false),'unavailable');
  assert.equal(imageState('data:image/jpeg;base64,ok',0,46000,true,'',false),'ready');
});

test('playback pause is distinct from failure and JPEG remains authoritative',()=>{
  const paused='data:image/svg+xml;base64,'+Buffer.from('<svg data-desk-thumbnail-paused="true"/>').toString('base64');
  assert.equal(imageState(paused,0,46000,true,'',true),'paused');
  assert.equal(imageState('data:image/jpeg;base64,cached',0,46000,true,paused,true),'ready');
});
