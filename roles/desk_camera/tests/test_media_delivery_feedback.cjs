const fs=require('node:fs'),vm=require('node:vm'),assert=require('node:assert/strict');
const source=fs.readFileSync(process.argv[2],'utf8').split('/* Native Media Browser feedback.')[1];assert(source);
(async()=>{
 const listeners={},intervals=new Map(),posts=[],requests=[],shown=[];let id=0,rnd=0,offline=false,resolveOpen,closing=0;
 const dialog={localName:'hui-dialog-web-browser-play-media',isConnected:true,showDialog:p=>shown.push(p),closeDialog:()=>{}};
 const hass={callWS:msg=>{assert(!offline,'Detached heartbeat/close must not need Core');posts.push(msg.value);return msg.value.action==='open'?new Promise(resolve=>resolveOpen=resolve):Promise.resolve({closed:true});}};
 const browser={localName:'ha-media-player-browse',hass,dispatchEvent:()=>{}};
 const panel={localName:'ha-panel-media-browser',hass,isConnected:true,shadowRoot:{querySelector:n=>n==='ha-media-player-browse'?browser:{entityId:'browser'}}};
 const location={origin:'http://home',pathname:'/media-browser/browser/folder'};
 const context={window:{addEventListener:(n,f)=>listeners[n]=f,dispatchEvent:()=>{}},document:{querySelectorAll:()=>[dialog]},location,URL,AbortController,AbortSignal,performance,
  crypto:{getRandomValues:b=>(b.fill(++rnd),b)},CustomEvent:class{constructor(n,o){this.type=n;Object.assign(this,o);}},
  setInterval:f=>(intervals.set(++id,f),id),clearInterval:i=>intervals.delete(i),setTimeout:(f)=>setImmediate(f),
  fetch:async(url,options)=>{requests.push({url,options});return {ok:true,json:async()=>url.endsWith('/close')?{closed:++closing>1}:{active:true}};}};
 vm.runInNewContext('/* Native Media Browser feedback.'+source,context);
 const tick=()=>new Promise(resolve=>setImmediate(resolve));
 const event=()=>({detail:{item:{media_content_id:'media-source://tapo_control/tapo_control/?entry=x&date=2026-10-17&startDate=1792200000&endDate=1792200060',title:'clip'}},composedPath:()=>[browser,panel],preventDefault(){},stopImmediatePropagation(){}});
 const grant=scope=>{const base='/desk-camera-media/v1/deskb1x/'+'f'.repeat(64)+'/';return {url:base+'index.m3u8',mime_type:'application/x-mpegURL',delivery:{viewer:scope,touch_url:base+'touch',close_url:base+'close',heartbeat_seconds:5,expires_in_seconds:7200}};};
 listeners['media-picked'](event());await tick();resolveOpen(grant(posts.at(-1).scope));await tick();
 assert.match(shown.at(-1).sourceUrl,/^\/desk-camera-media\/v1\//);
 offline=true;panel.isConnected=false;for(const fn of intervals.values())fn();await tick();
 assert.equal(requests.at(-1).url.endsWith('/touch'),true);assert.equal(posts.length,1);assert.equal(intervals.size,1);
 listeners['dialog-closed']({detail:{dialog:'hui-dialog-web-browser-play-media'}});await tick();await tick();await tick();
 assert.equal(requests.filter(r=>r.url.endsWith('/close')).length,2);assert.equal(intervals.size,0);assert.equal(posts.length,1);
 for(const r of requests){assert.equal(r.options.method,'POST');assert.equal(r.options.credentials,'omit');assert.equal(r.options.redirect,'error');}
 // A delayed grant arriving after close is immediately released on its serving
 // node, even if the initial authenticated Core request was cancelled first.
 offline=false;panel.isConnected=true;listeners['media-picked'](event());await tick();const scope=posts.at(-1).scope;
 listeners['dialog-closed']({detail:{dialog:'hui-dialog-web-browser-play-media'}});await tick();offline=true;const before=requests.length;
 resolveOpen(grant(scope));await tick();assert.equal(requests.length,before+1);assert(requests.at(-1).url.endsWith('/close'));
 // Capability routes from an untrusted/malformed response cannot send browser
 // requests to another origin, nor become a player source.
 offline=false;listeners['media-picked'](event());await tick();const bad=grant(posts.at(-1).scope);bad.url='https://other/clip.mp4';
 resolveOpen(bad);await tick();assert.match(shown.at(-1).sourceUrl,/^data:image/);assert(!requests.some(r=>r.url.includes('other')));
 console.log('PASS: independent touch/close while Core offline, panel rebuild survives, cleanup acknowledgement, late grant release, malformed route rejected');
})().catch(e=>{console.error(e);process.exitCode=1;});
