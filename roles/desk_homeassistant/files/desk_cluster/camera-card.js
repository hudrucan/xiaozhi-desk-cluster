/* Owned watchdog around the unmodified WebRTC Camera card. No camera credentials. */
const WORKLOADS={bed_hd:'tapo_c222_160',bed_sd:'tapo_c222_160',cam161_hd:'tapo_c222_161',cam161_sd:'tapo_c222_161',cam163_hd:'tapo_c222_163',cam163_sd:'tapo_c222_163'};
const PTZ_PREFIX={tapo_c222_160:'bed',tapo_c222_161:'makeup',tapo_c222_163:'guess'};
const CONTROL_STYLE=`
.controls{gap:8px;left:8px;right:8px;bottom:8px}
.controls ha-icon{flex-shrink:0;display:flex;align-items:center;justify-content:center;box-sizing:border-box;width:36px;height:36px;padding:6px;line-height:0;border-radius:6px;background:rgba(0,0,0,.25);--mdc-icon-size:24px}
.controls .stream{margin:0;box-sizing:border-box;height:36px;min-width:36px;text-align:center;padding:6px 8px;font-size:16px;line-height:24px;border-radius:6px;background:rgba(0,0,0,.25)}
.controls .space{flex:1;min-width:0;width:auto}
.ptz{top:40%;right:10px}.ptz-move{width:96px;height:96px}.ptz-move ha-icon{width:32px;height:32px;display:flex;align-items:center;justify-content:center;--mdc-icon-size:24px}
`;
let storageConnection,storageAt=0,storagePromise;
function storageFor(hass){
  const now=performance.now();
  if(!storagePromise||storageConnection!==hass.connection||now-storageAt>10000){
    storageConnection=hass.connection;storageAt=now;
    storagePromise=hass.callWS({type:'desk_cluster/camera_storage'}).catch(()=>({}));
  }
  return storagePromise;
}
class DeskCameraCard extends HTMLElement {
  setConfig(config){
    if(!config.streams?.length||config.streams.some(s=>!WORKLOADS[s.url]))throw Error('Use a configured cluster camera stream');
    this.config=structuredClone(config);this.workload=WORKLOADS[config.streams[0].url];
    if(config.streams.some(s=>WORKLOADS[s.url]!==this.workload))throw Error('Use one camera per card');
    if(this.isConnected)this.mount();
  }
  set hass(value){this._hass=value;if(this.card)this.card.hass=value;this.refreshStorage();}
  getCardSize(){return this.card?.getCardSize()||6;}
  connectedCallback(){
    this.visible=true;this.lastProgress=performance.now();this.currentTime=-1;this.retry=0;
    this.observer=new IntersectionObserver(entries=>{this.visible=entries.some(e=>e.isIntersecting);this.lastProgress=performance.now();},{threshold:.1});this.observer.observe(this);
    this.mount();this.timer=setInterval(()=>this.tick(),2000);
  }
  disconnectedCallback(){clearInterval(this.timer);this.observer?.disconnect();this.card?.remove();this.card=null;this.mountToken=(this.mountToken||0)+1;}
  async mount(){
    if(!this.isConnected||!this.config)return;
    const token=this.mountToken=(this.mountToken||0)+1;
    await customElements.whenDefined('webrtc-camera');if(!this.isConnected||token!==this.mountToken)return;
    // Keep the user's selected HD/SD stream across recovery.
    const selected=this.card?.config?.url,config=structuredClone(this.config);config.type='custom:webrtc-camera';
    config.style=CONTROL_STYLE+(config.style||'');
    if(config.ptz===undefined){
      const prefix=PTZ_PREFIX[this.workload];config.ptz={service:'button.press',opacity:.7};
      for(const direction of ['left','right','up','down'])config.ptz['data_'+direction]={entity_id:`button.${prefix}_move_${direction}`};
    }
    const index=config.streams.findIndex(s=>s.url===selected);
    if(index>0)config.streams=[...config.streams.slice(index),...config.streams.slice(0,index)];
    const card=document.createElement('webrtc-camera');card.setConfig(config);if(this._hass)card.hass=this._hass;
    const shell=document.createElement('ha-card');shell.style.cssText='display:block;overflow:hidden';
    const footer=document.createElement('div');footer.style.cssText='display:flex;align-items:center;justify-content:flex-end;padding:0 8px;min-height:32px';
    this.footer=footer;this.storageState=null;this.renderStorage('unknown');this.refreshStorage();
    shell.append(card,footer);this.replaceChildren(shell);this.card=card;this.lastProgress=performance.now();this.currentTime=-1;
  }
  async refreshStorage(){
    if(!this.isConnected||!this._hass?.connection?.connected||!this._hass.callWS)return;
    const hass=this._hass;
    const data=await storageFor(hass);if(this._hass!==hass&&this._hass?.connection!==hass.connection)return;
    if(this.isConnected)this.renderStorage(data[this.workload]||'unknown');
  }
  renderStorage(state){
    if(!this.footer||state===this.storageState)return;this.storageState=state;
    this.footer.replaceChildren();
    if(state==='ready'){
      const link=document.createElement('a');link.textContent='Recordings';link.href='/media-browser/browser/app%2Cmedia-source%3A%2F%2Ftapo_control';link.style.cssText='color:var(--primary-color);font:inherit;font-size:14px;font-weight:500;text-decoration:none;padding:6px 8px;line-height:20px';link.onclick=event=>{event.preventDefault();history.pushState(null,'',link.getAttribute('href'));window.dispatchEvent(new CustomEvent('location-changed'));};this.footer.append(link);
    }else{
      const label=document.createElement('span');label.style.cssText='font-size:12px;color:var(--secondary-text-color)';
      label.textContent=state==='absent'?'Live only · No microSD card':state==='needs_attention'?'Live only · Check microSD in Tapo':'Live · microSD status unavailable';this.footer.append(label);
    }
  }
  tick(){
    if(!document.hidden&&this.visible)this.refreshStorage();
    if(document.hidden||!this.visible||!this._hass?.connection?.connected){this.lastProgress=performance.now();return;}
    const video=this.card?.video;if(!video)return;
    if(video.currentTime!==this.currentTime&&video.readyState>=2){this.currentTime=video.currentTime;this.lastProgress=performance.now();this.retry=0;return;}
    // Respect an intentional pause. A never-started or stalled live connection is retried.
    if(video.paused&&video.readyState>=2&&!video.error)return;
    const delay=Math.min(30000,5000*2**this.retry);
    if(performance.now()-this.lastProgress<20000||performance.now()-(this.lastRetry||0)<delay)return;
    this.lastRetry=performance.now();this.retry=Math.min(this.retry+1,3);this.mount();
  }
}
if(!customElements.get('desk-camera-card'))customElements.define('desk-camera-card',DeskCameraCard);
window.customCards=window.customCards||[];
if(!window.customCards.some(c=>c.type==='desk-camera-card'))window.customCards.push({type:'desk-camera-card',name:'Cluster camera',description:'WebRTC camera with automatic stream recovery'});

/* Native Media Browser feedback. Viewer-scoped preparation in HA's own player.
 * The native image placeholder opens immediately; showDialog() swaps that same
 * native popup to video/HLS when ready. No frontend prototype or player fork.
 */
(() => {
  if(typeof window.addEventListener!=='function'||window.deskRecordingFeedback)return;
  window.deskRecordingFeedback=true;
  const random=()=>Array.from(crypto.getRandomValues(new Uint8Array(16)),n=>n.toString(16).padStart(2,'0')).join('');
  const client=random(),tag='hui-dialog-web-browser-play-media';
  let sequence=0,pending,draining=Promise.resolve();
  function api(action,p,extra={}) {
    // Existing HA WebSocket bypasses the browser's six HTTP connections,
    // which native thumbnail downloads can otherwise monopolize.
    return p.hass.callWS({type:'desk_cluster/recording_session',value:{action,client,sequence:p.sequence,scope:p.scope,...extra}})
      .then(value=>({ok:true,json:async()=>value}));
  }
  function findDialog(root=document) {
    for(const el of root.querySelectorAll('*')) {
      if(el.localName===tag)return el;
      if(el.shadowRoot){const found=findDialog(el.shadowRoot);if(found)return found;}
    }
  }
  function release(p,closePopup=true) {
    if(pending!==p)return;
    pending=undefined;p.closed=true;clearInterval(p.timer);
    // Wait for acknowledgement before a subsequent clip can allocate a handle.
    window.dispatchEvent(new CustomEvent('desk-recording-viewer',{detail:{entry:p.entry,active:false}}));
    draining=api('close',p).catch(()=>undefined).then(()=>window.dispatchEvent(new CustomEvent('desk-recording-viewer',{detail:{entry:p.entry,active:false,drained:true}})));
    if(closePopup)(p.dialog||findDialog())?.closeDialog();
  }
  function errorImage() {
    return 'data:image/svg+xml;charset=utf-8,'+encodeURIComponent('<svg xmlns="http://www.w3.org/2000/svg" width="640" height="360"><text x="320" y="180" text-anchor="middle" fill="#888" font-family="Roboto,Arial,sans-serif" font-size="18">Unable to prepare recording. Close and retry.</text></svg>');
  }
  async function open(p,clip) {
    try {
      await draining;if(pending!==p)return;
      const response=await api('open',p,{clip});
      if(!response.ok)throw Error('Recording unavailable');
      const result=await response.json();if(pending!==p)return;
      // Native dialog import/render can still be in flight for an instant hit.
      for(let i=0;i<60&&pending===p;i++) {
        p.dialog=findDialog();if(p.dialog?.showDialog)break;
        await new Promise(resolve=>setTimeout(resolve,50));
      }
      if(pending!==p)return;
      if(!p.dialog?.showDialog)throw Error('Player unavailable');
      // Only cluster media routes are accepted; no worker URL or credential is
      // exposed to the player. The Core response already signs these routes.
      if(!result.url?.startsWith('/api/desk_cluster/recording/')||!['video/mp4','application/x-mpegURL'].includes(result.mime_type))throw Error('Invalid recording');
      p.dialog.showDialog({sourceUrl:result.url,sourceType:result.mime_type,title:p.title,can_play:true});
    } catch(error) {
      if(pending!==p)return;
      p.dialog=p.dialog||findDialog();release(p,false);
      p.dialog?.showDialog({sourceUrl:errorImage(),sourceType:'image/svg+xml',title:p.title});
    }
  }
  function picked(event) {
    let url;try{url=new URL(event.detail?.item?.media_content_id);}catch{return;}
    if(url.protocol!=='media-source:'||url.hostname!=='tapo_control'||!url.searchParams.has('entry')||!/^\d+$/.test(url.searchParams.get('startDate')||'')||!/^\d+$/.test(url.searchParams.get('endDate')||''))return;
    const path=event.composedPath(),panel=path.find(el=>el.localName==='ha-panel-media-browser');
    const browser=path.find(el=>el.localName==='ha-media-player-browse')||panel?.shadowRoot?.querySelector('ha-media-player-browse');
    const player=panel?.shadowRoot?.querySelector('ha-bar-media-player');
    const hass=panel?.hass||browser?.hass;
    if(!browser||player?.entityId!=='browser'||!hass?.callWS)return;
    event.preventDefault();event.stopImmediatePropagation();
    if(pending)return;
    const item=event.detail.item;
    const p={hass,panel,path:location.pathname,sequence:++sequence,scope:random(),entry:url.searchParams.get('entry'),title:String(item.title||'Camera recording').slice(0,200)};
    pending=p;
    window.dispatchEvent(new CustomEvent('desk-recording-viewer',{detail:{entry:p.entry,active:true}}));
    p.timer=setInterval(()=>{
      if(pending!==p)return;
      if(!p.panel.isConnected){release(p);return;}
      p.dialog=p.dialog||findDialog();
      api('touch',p).catch(()=>undefined);
    },5000);
    // Use the native media-picked flow to load its standard dialog module.
    // This is a static local SVG, never a camera request or a fake media stream.
    const placeholder=new CustomEvent('media-picked',{bubbles:true,composed:true,detail:{...event.detail,item:{...item,title:'Preparing recording',media_content_id:'media-source://tapo_control/tapo_control/?desk_loading='+p.scope,media_content_type:'image/svg+xml'}}});
    browser.dispatchEvent(placeholder);
    open(p,{entry:url.searchParams.get('entry'),date:url.searchParams.get('date'),start:Number(url.searchParams.get('startDate')),end:Number(url.searchParams.get('endDate'))});
  }
  window.addEventListener('media-picked',picked,true);
  window.addEventListener('show-dialog',event=>{
    if(event.detail?.dialogTag!==tag)return;
    const source=event.detail.dialogParams?.sourceUrl||'';
    let url;try{url=new URL(source,location.origin);}catch{return;}
    const loadingScope=url.protocol==='data:'&&url.hash.startsWith('#viewer=')?url.hash.slice(8):url.pathname==='/desk-cluster-assets/recording-loading.svg'?url.searchParams.get('viewer'):null;
    if(loadingScope!==null&&(!pending||loadingScope!==pending.scope)){event.preventDefault();event.stopImmediatePropagation();}
  },true);
  window.addEventListener('dialog-closed',event=>{if(pending&&event.detail?.dialog===tag)release(pending,false);},true);
  const navigate=()=>{if(pending&&location.pathname!==pending.path)release(pending);};
  window.addEventListener('location-changed',navigate);
  window.addEventListener('popstate',navigate);
  window.addEventListener('pagehide',()=>{if(pending)release(pending);});
})();
