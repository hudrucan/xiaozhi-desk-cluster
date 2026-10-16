/* Meaningful watchdog cases with a fake DOM; no camera or browser connection. */
const fs=require('node:fs'),vm=require('node:vm'),assert=require('node:assert/strict');
let cls,now=0;
const context={HTMLElement:class{},customElements:{get:()=>false,define:(n,c)=>{cls=c;}},window:{},performance:{now:()=>now},document:{hidden:false},structuredClone,console};
vm.runInNewContext(fs.readFileSync(process.argv[2],'utf8'),context);
function card(){const c=new cls();c.visible=true;c._hass={connection:{connected:true}};c.card={video:{currentTime:1,readyState:4,paused:false}};c.currentTime=1;c.lastProgress=0;c.retry=0;c.mount=()=>c.mounts=(c.mounts||0)+1;return c;}
let c=card();now=21000;c.tick();assert.equal(c.mounts,1);
c=card();c.card.video.currentTime=2;c.tick();assert.equal(c.mounts,undefined);assert.equal(c.lastProgress,now);
c=card();c.card.video.paused=true;c.tick();assert.equal(c.mounts,undefined);
c=card();context.document.hidden=true;c.tick();assert.equal(c.mounts,undefined);context.document.hidden=false;
c=card();c._hass.connection.connected=false;c.tick();assert.equal(c.mounts,undefined);
assert.throws(()=>new cls().setConfig({streams:[{url:'rtsp://private'}]}));
console.log('PASS: stalled live reconnects; progress, pause, hidden and disconnected Core are respected; arbitrary streams rejected');

// Card insertion/removal changes only the footer, never the live video instance.
context.document.createElement=()=>({style:{},set textContent(v){this.text=v;},get textContent(){return this.text;}});
c=card();c.footer={replaceChildren(){this.children=[];},append(v){this.children.push(v);}};const stream=c.card;
for(const [state,label] of [['absent','Live only · No microSD card'],['ready','Recordings'],['absent','Live only · No microSD card'],['unknown','Live · microSD status unavailable']]){
 c.renderStorage(state);assert.equal(c.footer.children[0].textContent,label);assert.equal(c.card,stream);
}
console.log('PASS: SD insertion, removal and unavailable status update the footer without restarting live view');
