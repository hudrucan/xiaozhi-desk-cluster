/* Native HA panel: no extra navigation shell, theme or external dependencies. */
class DeskClusterPanel extends HTMLElement {
  set hass(value) { this._hass=value; if(!this.shadowRoot)this.mount(); this.updateMenu(); }
  set narrow(value) { this._narrow=value; this.updateMenu(); }
  updateMenu(){const menu=this.shadowRoot?.querySelector('ha-menu-button');if(menu){menu.hass=this._hass;menu.narrow=this._narrow;}}
  connectedCallback(){ if(this._hass&&!this.shadowRoot)this.mount(); this.start(); }
  disconnectedCallback(){ clearTimeout(this.timer); this.timer=null; }
  mount(){
    this.attachShadow({mode:'open'}).innerHTML=`
      <style>
        :host{display:block;height:100%;background:var(--primary-background-color);color:var(--primary-text-color);font-family:var(--paper-font-body1_-_font-family,Roboto,sans-serif)}
        header{height:var(--header-height,56px);display:flex;align-items:center;padding:0 16px;background:var(--app-header-background-color);color:var(--app-header-text-color);box-sizing:border-box;border-bottom:1px solid var(--divider-color)}
        header h1{font-size:20px;font-weight:400;margin:0}
        main{max-width:760px;margin:0 auto;padding:24px 16px;box-sizing:border-box}.intro{color:var(--secondary-text-color);font-size:14px;line-height:1.6;margin:0 0 20px}
        ha-card{display:block;padding:24px;margin-bottom:16px}.title{display:flex;align-items:center;gap:12px;margin-bottom:20px}.title h2{font-size:20px;font-weight:400;margin:0}.title ha-icon{color:var(--primary-color)}
        .row{display:flex;align-items:center;justify-content:space-between;gap:20px;min-height:44px;font-size:14px}.label{color:var(--secondary-text-color)}.value{text-align:right}.status{font-weight:500}.nodes{border-top:1px solid var(--divider-color);border-bottom:1px solid var(--divider-color);padding:12px 0;margin:16px 0}.node{display:flex;align-items:center;gap:8px}.dot{width:8px;height:8px;display:inline-block;border-radius:50%;background:var(--disabled-text-color)}.dot.ok{background:var(--success-color,#4caf50)}
        .actions{display:flex;justify-content:flex-end;padding-top:8px}ha-button{min-height:44px}.hint{font-size:13px;color:var(--secondary-text-color);line-height:1.6;margin:16px 0 0}.error{color:var(--error-color);margin:12px 0;font-size:14px;line-height:1.5}.error:empty{display:none}ha-linear-progress{display:block;margin-top:16px}ha-linear-progress[hidden]{display:none}.foot{font-size:13px;line-height:1.6;color:var(--secondary-text-color);padding:0 8px}
        @media(max-width:480px){main{padding:16px 12px}ha-card{padding:20px}.row{gap:12px}}
      </style>
      <header><ha-menu-button></ha-menu-button><h1>Cluster</h1></header>
      <main><p class="intro">Save changes in Home Assistant as usual. Sync config when you are ready to update the recovery copy on the other nodes.</p>
      <ha-card><div class="title"><ha-icon icon="mdi:cloud-sync-outline"></ha-icon><h2>Configuration sync</h2></div>
      <div class="row"><span class="label">Status</span><span class="value status" id="status" role="status" aria-live="polite">Connecting…</span></div>
      <div class="row"><span class="label">Core owner</span><span class="value" id="owner">—</span></div>
      <div class="row"><span class="label">Recovery revision</span><span class="value" id="revision">—</span></div>
      <div class="row"><span class="label">Last sync</span><span class="value" id="last">—</span></div>
      <p class="hint" id="pending" role="status" hidden></p><div class="nodes" id="nodes"></div><ha-linear-progress id="progress" indeterminate hidden></ha-linear-progress>
      <p class="error" id="error" role="alert"></p>
      <div class="actions"><ha-button id="sync" appearance="filled">Sync config</ha-button></div>
      <p class="hint">Home Assistant keeps running during sync. A sync includes configuration saved to disk; changes made during sync remain pending for the next one.</p></ha-card>
      <ha-card id="media-card" hidden><div class="title"><ha-icon icon="mdi:cctv"></ha-icon><h2>Camera workers</h2></div><div id="camera-rows"></div><p class="hint">Healthy workers stay in place. Live streams reconnect automatically after a transfer.</p><div class="actions"><ha-button id="maintenance">Node maintenance</ha-button></div></ha-card><p class="foot">Recovery uses the latest revision confirmed by at least two nodes. If a node is offline, it catches up when it returns. SQLite remains local to each node.</p></main>`;
    this.updateMenu();
    this.shadowRoot.getElementById('sync').onclick=()=>this.sync(); this.shadowRoot.getElementById('maintenance').onclick=()=>window.open(location.protocol==='https:'&&location.hostname==='ha.cluster.huynm.dev'?'https://cluster.huynm.dev/':'http://192.168.1.186/','_blank','noopener'); this.start();
  }
  start(){if(this.isConnected&&this.shadowRoot&&!this.timer){this.timer=setTimeout(()=>{this.timer=null;this.refresh();},0);}}
  async refresh(){
    if(!this.isConnected||!this._hass)return;
    try{this.data=await this._hass.callWS({type:'desk_cluster/status'});this.transportError=null;}
    catch(e){this.transportError=e.message||'Cluster connection unavailable. Retrying…';}
    try{this.cameras=await this._hass.callWS({type:'desk_cluster/cameras'});}catch{this.cameras=null;}
    this.draw();this.drawCameras();if(this.isConnected)this.timer=setTimeout(()=>{this.timer=null;this.refresh();},3000);
  }
  draw(){
    const root=this.shadowRoot,d=this.data,button=root.getElementById('sync');
    const text=(id,value)=>{const element=root.getElementById(id);if(element.textContent!==value)element.textContent=value;};
    text('error',this.transportError||d?.preparation?.error||d?.operation?.error||'');
    if(!d){button.disabled=true;return;}
    const op=d.operation||{},prep=d.preparation||{},busy=['preparing','replicating'].includes(op.state)||['preparing','creating_snapshot','replicating'].includes(prep.stage);
    const replicas=d.replicas||[],count=replicas.filter(n=>n.synced).length;
    let label=d.pending_disk_changes?'Changes pending':`Synced · ${count}/${replicas.length}`;
    if(op.state==='failed'||prep.stage==='failed')label='Sync incomplete';
    if(busy)label=op.state==='replicating'?'Syncing recovery copy…':prep.stage==='creating_snapshot'?'Creating snapshot…':'Preparing saved configuration…';
    text('status',label);
    const pending=root.getElementById('pending');pending.hidden=!d.pending_disk_changes;pending.style.color='var(--warning-color,#ff9800)';text('pending',d.pending_disk_changes?'Changes are saved locally. Sync config to include them in recovery after a node failure.':'');
    text('owner',d.owner||'Unavailable');text('revision',`r${d.checkpoint_revision}`);
    text('last',d.checkpoint_at?new Date(d.checkpoint_at*1000).toLocaleString(): '—');
    const nodes=root.getElementById('nodes'),signature=JSON.stringify(replicas);
    if(this.replicaSignature!==signature){this.replicaSignature=signature;nodes.replaceChildren();
    for(const n of replicas){const row=document.createElement('div');row.className='row';const name=document.createElement('span');name.textContent=n.node;const state=document.createElement('span');state.className='node';const dot=document.createElement('span');dot.className='dot'+(n.synced?' ok':'');state.append(dot,document.createTextNode(n.synced?'Synced':'Pending'));row.append(name,state);nodes.append(row);}}
    root.getElementById('progress').hidden=!busy;const disabled=Boolean(busy||!d.ready||this.transportError||this.submitting);if(button.disabled!==disabled)button.disabled=disabled;
    text('sync',busy?'Syncing…':'Sync config');
  }
  drawCameras(){
    const root=this.shadowRoot,card=root.getElementById('media-card'),d=this.cameras;
    card.hidden=!d?.enabled;if(card.hidden)return;
    const sig=JSON.stringify(d.cameras);if(sig===this.cameraSignature)return;this.cameraSignature=sig;
    const rows=root.getElementById('camera-rows');rows.replaceChildren();
    const names={tapo_c222_160:'Bed Cam',tapo_c222_161:'Makeup',tapo_c222_163:'Guess'};
    for(const camera of d.cameras){
      const row=document.createElement('div');row.className='row';const label=document.createElement('span');label.textContent=names[camera.workload]||camera.workload;
      const value=document.createElement('span');value.className='value';value.textContent=camera.owner?`${camera.owner} · Ready`:'Waiting for a ready worker';
      const reachable=camera.nodes.filter(n=>n.reachable===true).length;const detail=document.createElement('small');detail.style.cssText='display:block;color:var(--secondary-text-color);font-size:12px;line-height:1.6';detail.textContent=`${reachable}/${camera.nodes.length} nodes can reach camera`+(camera.previous_owner?` · ${camera.previous_owner} → ${camera.owner}`:'');label.append(detail);value.title=camera.owner_changed_at?'Owner observed '+new Date(camera.owner_changed_at*1000).toLocaleString():'';
      row.append(label,value);rows.append(row);
    }
  }
  async sync(){
    if(this.submitting)return;this.submitting=true;
    let timeout;
    try{this.draw();await Promise.race([this._hass.callWS({type:'desk_cluster/sync'}),new Promise((_,reject)=>{timeout=setTimeout(()=>reject(new Error('No response from Cluster. Check connection and retry.')),10000);})]);this.transportError=null;}
    catch(e){this.transportError=e.message||'Unable to start sync.';}
    finally{clearTimeout(timeout);this.submitting=false;clearTimeout(this.timer);this.timer=null;await this.refresh();}
  }
}
if(!customElements.get('desk-cluster-panel'))customElements.define('desk-cluster-panel',DeskClusterPanel);
