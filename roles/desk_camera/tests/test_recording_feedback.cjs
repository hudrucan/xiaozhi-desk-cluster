const fs=require('node:fs'),vm=require('node:vm'),assert=require('node:assert/strict');
const source=fs.readFileSync(process.argv[2],'utf8').split('/* Native Media Browser feedback.')[1];assert(source);
(async()=>{
 const listeners={},intervals=new Map(),posts=[],emitted=[],shown=[],viewerEvents=[];let id=0,rnd=0,closed=0;
 let resolveOpen;const dialog={localName:'hui-dialog-web-browser-play-media',showDialog:p=>shown.push(p),closeDialog:()=>closed++};
 const hass={callWS:msg=>{
  assert.equal(msg.type,'desk_cluster/recording_session');const v=msg.value;posts.push(v);
  if(v.action==='open')return new Promise(resolve=>resolveOpen=resolve);
  return Promise.resolve({closed:true});
 }};
 const browser={localName:'ha-media-player-browse',hass,dispatchEvent:e=>emitted.push(e)};
 const player={entityId:'browser'};
 const panel={localName:'ha-panel-media-browser',hass,isConnected:true,shadowRoot:{querySelector:n=>n==='ha-media-player-browse'?browser:player}};
 const location={origin:'http://home',pathname:'/media-browser/browser/folder'};
 const context={window:{addEventListener:(n,f)=>listeners[n]=f,dispatchEvent:e=>viewerEvents.push(e)},document:{querySelectorAll:()=>[dialog]},location,URL,AbortController,crypto:{getRandomValues:b=>(b.fill(++rnd),b)},CustomEvent:class{constructor(n,o){this.type=n;Object.assign(this,o);}},setInterval:f=>(intervals.set(++id,f),id),clearInterval:i=>intervals.delete(i),setTimeout};
 vm.runInNewContext('/* Native Media Browser feedback.'+source,context);
 const tick=()=>new Promise(resolve=>setImmediate(resolve));
 function event(url='media-source://tapo_control/tapo_control/?entry=x&date=2026-10-17&startDate=1792200000&endDate=1792200060'){
  return {detail:{item:{media_content_id:url,title:'clip'}},composedPath:()=>[browser,panel],preventDefault(){this.prevented=true;},stopImmediatePropagation(){this.stopped=true;}};
 }
 let e=event('media-source://other/file');listeners['media-picked'](e);assert.equal(e.stopped,undefined);
 e=event();listeners['media-picked'](e);assert.equal(e.stopped,true);assert.equal(emitted.length,1);assert.match(emitted[0].detail.item.media_content_id,/desk_loading=/);await tick();assert.equal(posts.filter(p=>p.action==='open').length,1);
 listeners['media-picked'](event());assert.equal(emitted.length,1);
 listeners['dialog-closed']({detail:{dialog:'hui-dialog-web-browser-play-media'}});assert.equal(posts.at(-1).action,'close');assert.equal(viewerEvents.at(-1).detail.active,false);await tick();assert.equal(viewerEvents.at(-1).detail.drained,true);
 resolveOpen({url:'/api/desk_cluster/recording/c/n/i',mime_type:'video/mp4'});await tick();assert.equal(shown.length,0);assert.equal(intervals.size,0);
 listeners['media-picked'](event());await tick();resolveOpen({url:'/api/desk_cluster/recording/c/n/i',mime_type:'video/mp4'});await tick();assert.equal(shown.at(-1).sourceType,'video/mp4');
 listeners['location-changed']();assert.notEqual(posts.at(-1).action,'close');location.pathname='/home/overview';listeners['location-changed']();assert.equal(posts.at(-1).action,'close');assert.equal(closed,1);
 const late={detail:{dialogTag:'hui-dialog-web-browser-play-media',dialogParams:{sourceUrl:'/desk-cluster-assets/recording-loading.svg?viewer='+new URL(emitted.at(-1).detail.item.media_content_id).searchParams.get('desk_loading')}},preventDefault(){this.prevented=true;},stopImmediatePropagation(){this.stopped=true;}};listeners['show-dialog'](late);assert.equal(late.stopped,true);
 player.entityId='media_player.living_room';e=event();listeners['media-picked'](e);assert.equal(e.stopped,undefined);
 console.log('PASS: immediate native placeholder, single resolve, last-view close, stale result suppression, navigation cancel, external players untouched');
})().catch(e=>{console.error(e);process.exitCode=1;});
