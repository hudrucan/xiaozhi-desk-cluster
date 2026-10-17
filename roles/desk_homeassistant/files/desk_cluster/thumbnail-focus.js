/* Scope native thumbnails to the current folder of this browser tab.
 * Uses HA's media-browsed event before Lit renders the supplied media item.
 * No fetch monkey-patch, frontend prototype override or replacement media UI.
 */
(() => {
  if (window.deskThumbnailFocus) return;
  window.deskThumbnailFocus = true;
  // getRandomValues works on the existing HTTP LAN origin as well.
  const nonce=()=>Array.from(crypto.getRandomValues(new Uint8Array(16)),n=>n.toString(16).padStart(2,'0')).join('');
  const client=nonce();
  let sequence=0,active,hass,lastPost=0,browserRoot,scanPending=false;
  const progress=new WeakMap(),viewers=new Set();
  let resumingEntry,resumeUntil=0;
  function retryPreview(el,value,url) {
    value.retryAt=Date.now();const retry=new URL(url,location.origin);
    retry.searchParams.set('retry',String(value.retryAt));
    const nextUrl=retry.pathname+retry.search,item=active.items.get(retry.pathname);
    if(item)item.thumbnail=nextUrl;
    el.url=nextUrl;
    progress.set(el,{...value,url:nextUrl,started:Date.now(),sameClip:true});
  }
  const events=[];
  function trace(phase,more={}){events.push({phase,sequence,range:active?[active.lower,active.upper]:null,...more});if(events.length>10)events.shift();document.documentElement.setAttribute('data-desk-thumbnail-state',JSON.stringify(events));}
  function folder(ids) {
    const id=ids?.at(-1)?.media_content_id;
    try {
      const url=new URL(id);
      if(url.protocol!=='media-source:'||url.hostname!=='tapo_control')return;
      const entry=url.searchParams.get('entry'),date=url.searchParams.get('date')?.replaceAll('-','');
      if(!/^[A-Za-z0-9_-]{1,64}$/.test(entry||'')||!/^\d{8}$/.test(date||''))return;
      const lower=url.searchParams.get('startDate'),upper=url.searchParams.get('endDate');
      if((lower!==null||upper!==null)&&(!/^\d+$/.test(lower||'')||!/^\d+$/.test(upper||'')))return;
      return {key:JSON.stringify([entry,date,lower,upper]),entry,date,lower:lower===null?null:Number(lower),upper:upper===null?null:Number(upper)};
    } catch {return;}
  }
  function post() {
    if(!hass?.callWS)return;
    lastPost=Date.now();
    const value={client,sequence,scope:active?.scope||null,folder:active?{entry:active.entry,date:active.date,lower:active.lower,upper:active.upper}:null};
    hass.callWS({type:'desk_cluster/thumbnail_scope',value}).then(()=>trace('scope-result',{status:200})).catch(()=>trace('scope-network-error'));
  }
  function move(next) {
    browserRoot?.host?.toggleAttribute('data-desk-thumbnail-layout',!!next);
    if(active?.key===next?.key)return;
    sequence+=1;active=next?{...next,scope:nonce(),items:new Map()}:undefined;
    post();
  }
  // Observe the native rendered request/result. This never fetches an image
  // itself, and only visits the current virtualized list (not the whole page).
  function imageState(background,started,now,sameClip,previousBackground,finished) {
    // A rendered JPEG is authoritative, including when back navigation reuses
    // an already-resolved row. Resource Timing has a finite buffer and must
    // never be required for showing that cached image.
    if(background.includes('data:image/jpeg')&&(sameClip||background!==previousBackground||finished))return 'ready';
    if(background.includes('data:image/svg+xml')&&(sameClip||background!==previousBackground||finished)) {
      const encoded=background.match(/base64,([A-Za-z0-9+/=]+)/)?.[1];
      try {if(encoded&&atob(encoded).includes('data-desk-thumbnail-paused'))return 'paused';}catch{}
      return 'unavailable';
    }
    if(now-started>=45000)return 'unavailable';
    return 'loading'; // Includes the server's admission/worker queue.
  }
  function paintProgress() {
    scanPending=false;
    if(!browserRoot||!active||document.visibilityState==='hidden')return;
    if(!browserRoot.querySelector('style[data-desk-thumbnail-layout]')) {
      const style=document.createElement('style');style.setAttribute('data-desk-thumbnail-layout','');
      // Native inset:0 stretches the host to 56px while its inner button stays
      // 40px at the top-left. Give the host an explicit centered square.
      style.textContent=`:host([data-desk-thumbnail-layout]) ha-list-item .graphic[data-desk-camera-thumbnail] > ha-icon-button.play {
        inset:auto;top:50%;left:50%;margin:0;transform:translate(-50%,-50%);
        width:32px;height:32px;--ha-icon-button-size:32px;
        --ha-icon-button-padding-inline:0px;--mdc-icon-size:18px;
        transition:opacity .1s ease-out;
      }
      :host([data-desk-thumbnail-layout]) .child .thumbnail[data-desk-camera-thumbnail] > ha-icon-button.play {
        inset:auto;top:50%;left:50%;margin:0;transform:translate(-50%,-50%);
        width:36px;height:36px;--ha-icon-button-size:36px;
        --ha-icon-button-padding-inline:0px;--mdc-icon-size:20px;
        background-color:rgba(0,0,0,.5);color:white;
        transition:opacity .1s ease-out;
      }`;
      browserRoot.append(style);
    }
    for(const el of browserRoot.querySelectorAll('ha-media-browser-thumbnail')) {
      const url=el.url,root=el.shadowRoot;
      if(!root||typeof url!=='string'||!url.startsWith('/api/desk_cluster/thumbnail/'))continue;
      el.parentElement?.setAttribute('data-desk-camera-thumbnail','');
      el.closest('.thumbnail')?.setAttribute('data-desk-camera-thumbnail','');
      const query=new URL(url,location.origin).searchParams;
      // Stale/native recycled rows must not be relabelled as a new request.
      if(query.get('scope')!==active.scope) {root.querySelector('[data-desk-thumbnail-progress]')?.remove();continue;}
      let value=progress.get(el);
      if(!value||value.url!==url){
        const clip=new URL(url,location.origin).pathname;
        value={url,clip,started:Date.now(),sameClip:!value||value.clip===clip,
          previousBackground:value?.background||'',readyBackground:value?.clip===clip?value.readyBackground:undefined,resumeUntil:value?.clip===clip?value.resumeUntil:undefined,retryAt:value?.clip===clip?value.retryAt:undefined,finished:false};
        progress.set(el,value);
      }
      const background=root.querySelector('.image')?.getAttribute('style')||'';
      value.finished ||= performance.getEntriesByName(new URL(url,location.origin).href).length>0;
      let state=imageState(background,value.started,Date.now(),value.sameClip,value.previousBackground,value.finished);
      const resuming=resumingEntry===active.entry&&Date.now()<resumeUntil&&!viewers.has(active.entry);
      // A transient owner-health miss can briefly return the generic fallback
      // after playback releases the camera. Continue this bounded resume cycle
      // past that short negative-cache TTL, rather than latching unavailable.
      if(state==='paused')value.resumeUntil=Date.now()+120000;
      else if(state==='unavailable'&&Date.now()<(value.resumeUntil||0))state='paused';
      const waiting=state==='paused'||(resuming&&state!=='ready');
      if(waiting&&resuming)state='loading';
      value.background=background;
      if(state==='ready'){value.finished=true;value.readyBackground=background;}
      // Native URL changes request a fresh authenticated image. Pause results
      // never enter RAM cache; retry lightly until the camera is released.
      if(waiting&&Date.now()-(value.retryAt||value.started)>=5000)retryPreview(el,value,url);

      el.setAttribute('data-desk-thumbnail-state',state);
      el.setAttribute('aria-busy',String(state==='loading'));
      let overlay=root.querySelector('[data-desk-thumbnail-progress]');
      if(state==='ready'){overlay?.remove();continue;}
      if(overlay?.dataset.state===state)continue;
      overlay?.remove();overlay=document.createElement('div');
      overlay.setAttribute('data-desk-thumbnail-progress','');overlay.dataset.state=state;
      overlay.setAttribute('role','status');
      overlay.setAttribute('aria-label',state==='loading'?'Loading preview':state==='paused'?'Preview paused during playback':'Preview unavailable');
      overlay.style.cssText='position:absolute;inset:0;display:flex;align-items:center;justify-content:center;gap:6px;background:var(--card-background-color,#1c1c1c);color:var(--secondary-text-color,#aaa);font:11px var(--ha-font-family-body,Roboto,sans-serif);pointer-events:none;border-radius:4px;';
      el.style.position='relative';
      if(state==='loading') {
        const spinner=document.createElement('ha-spinner');
        spinner.setAttribute('aria-hidden','true');
        spinner.style.cssText='--ha-spinner-size:20px;--track-width:2px;';
        overlay.append(spinner);
      } else if(state==='paused') {
        overlay.title='Preview paused during playback';
        if(value.readyBackground){overlay.style.cssText+='background-size:contain;background-position:center;background-repeat:no-repeat;'+value.readyBackground;}
        else {const icon=document.createElement('ha-icon');icon.setAttribute('icon','mdi:pause');icon.style.cssText='--mdc-icon-size:18px;opacity:.6';overlay.append(icon);}
      } else {const icon=document.createElement('ha-icon');icon.setAttribute('icon','mdi:image-off-outline');icon.style.cssText='--mdc-icon-size:18px;opacity:.6';overlay.append(icon);overlay.title='Preview unavailable. Reopen this folder to retry.';}
      root.append(overlay);
    }
  }
  function scheduleProgress(){if(!scanPending){scanPending=true;requestAnimationFrame(paintProgress);}}
  function browse(event) {
    const host=event.composedPath().find(el=>el.hass?.callWS);
    if(host)hass=host.hass;
    const browser=event.composedPath().find(el=>el.localName==='ha-media-player-browse');
    if(browser)browserRoot=browser.shadowRoot;
    const next=folder(event.detail?.ids);
    trace('browse',{current:!!event.detail?.current,children:event.detail?.current?.children?.length||0,parsed:!!next,hasHass:!!hass});
    move(next);
    if(!active)return;
    const current=event.detail?.current;
    for(const child of current?.children||[]) {
      if(typeof child.thumbnail!=='string'||!child.thumbnail.startsWith('/api/desk_cluster/thumbnail/'))continue;
      const url=new URL(child.thumbnail,location.origin);
      url.searchParams.set('client',client);url.searchParams.set('sequence',sequence);url.searchParams.set('scope',active.scope);
      child.thumbnail=url.pathname+url.search;
      active.items.set(url.pathname,child);
    }
    if(current){trace('annotated',{count:current.children?.filter(c=>c.thumbnail?.includes('scope=')).length||0});post();scheduleProgress();}
  }
  function locationFolder() {
    // HA navigation serializes each media item in a path segment. Cancel at
    // navigation time, before a potentially slow browse response arrives.
    const segments=location.pathname.split('/');const last=segments.at(-1);
    let ids;
    try {const item=decodeURIComponent(last);const comma=item.indexOf(',');ids=[{media_content_id:item.slice(comma+1)}];}catch{}
    return folder(ids);
  }
  function navigation(){move(locationFolder());trace('navigation');}
  window.addEventListener('desk-recording-viewer',event=>{
    const {entry,active:playing,drained}=event.detail||{};
    if(typeof entry!=='string')return;
    if(playing){viewers.add(entry);resumingEntry=undefined;}
    else {
      viewers.delete(entry);resumingEntry=entry;resumeUntil=Date.now()+120000;
      // Refresh uncached native rows after worker/session cleanup, not on every
      // HA state update. The cached JPEG remains visible throughout.
      if(drained&&active?.entry===entry)for(const el of browserRoot?.querySelectorAll('ha-media-browser-thumbnail')||[]) {
        const value=progress.get(el);
        if(value&&!value.readyBackground)retryPreview(el,value,value.url);
      }
    }
    scheduleProgress();
  });
  document.addEventListener('visibilitychange',()=>{if(document.visibilityState==='visible')scheduleProgress();});
  window.addEventListener('media-browsed',browse,true);
  // Small DOM-only check while this folder is visible. No polling API or
  // additional camera work. Also catches rows added by native virtualization.
  setInterval(()=>{if(active&&browserRoot&&document.visibilityState!=='hidden')scheduleProgress();},400);
  window.addEventListener('location-changed',navigation);
  window.addEventListener('popstate',navigation);
  window.addEventListener('pagehide',()=>{sequence+=1;active=undefined;post();});
  setInterval(()=>{if(active&&Date.now()-lastPost>=14000)post();},15000);
})();
