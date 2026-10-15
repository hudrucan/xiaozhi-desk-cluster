/* Native NetAlertX sidebar only: bounded polling, explicit queue/import states. */
(()=>{
 const button=document.getElementById('desk-network-scan'),panel=document.getElementById('desk-network-discovery');
 if(!button||!panel)return;
 panel.style.whiteSpace='normal';
 const phaseLabels={idle:'Waiting for a scan',queued:'Queued',selecting:'Selecting worker',running:'Scanning',retrying:'Trying another worker',completed:'Importing results',importing:'Importing results',complete:'Complete',cached:'Cached',failed:'Unavailable'};
 const busy=new Set(['queued','selecting','running','retrying','completed','importing']);
 let timer,inflight=false,sending=false;
 const text=(id,value)=>{const el=document.getElementById(id);if(el)el.textContent=value;};
 const age=seconds=>seconds<60?`${Math.floor(seconds)}s ago`:seconds<3600?`${Math.floor(seconds/60)}m ago`:`${Math.floor(seconds/3600)}h ago`;
 function render(value){
  const arp=value.scans?.arp||{},mdns=value.scans?.mdns||{},inventory=value.inventory||{};
  const active=busy.has(arp.phase);button.disabled=sending||active;button.textContent=sending?'Queuing…':active?phaseLabels[arp.phase]:'Scan now';
  text('desk-discovery-freshness',inventory.state==='fresh'?'Fresh':inventory.state==='stale'?'Stale':'Unknown');
  text('desk-discovery-owner',`Owner · ${value.owner||'unconfirmed'}`);
  text('desk-discovery-arp',`ARP · ${phaseLabels[arp.phase]||'Unavailable'}${arp.worker?` · ${arp.worker}`:''}`);
  const total=arp.devices===undefined?'':` · ${arp.devices} devices`;
  const changes=arp.phase==='complete'&&Number.isInteger(arp.new_devices)?` · ${arp.new_devices} new, ${arp.ip_changes||0} IP changes`:'';
  text('desk-discovery-result',arp.message||((arp.wall_seconds===undefined?'':`${arp.wall_seconds.toFixed(1)}s`)+total+changes)||'No completed observation');
  text('desk-discovery-mdns',`mDNS · ${phaseLabels[mdns.phase]||'Unavailable'}${mdns.worker&&mdns.phase!=='cached'?` · ${mdns.worker}`:''}`);
  text('desk-discovery-age',Number.isFinite(inventory.age_seconds)?`Data imported ${age(inventory.age_seconds)}${inventory.state==='stale'?' · showing last known presence':''}`:'Last import unavailable · freshness unknown');
  return active;
 }
 async function poll(){
  clearTimeout(timer);if(document.hidden||inflight)return;inflight=true;let active=false;
  try{
   const response=await fetch('/network/desk/scan-status',{cache:'no-store',signal:AbortSignal.timeout(4000)});
   if(!response.ok)throw Error();const value=await response.json();if(value.protocol!=='desk-network-discovery-v1')throw Error();active=render(value);
  }catch{button.disabled=sending;button.textContent=sending?'Queuing…':'Scan now';text('desk-discovery-freshness','Unknown');text('desk-discovery-age','Network status unavailable · no fresh observation confirmed');}
  finally{inflight=false;if(!document.hidden)timer=setTimeout(poll,active||panel.open?2500:15000);}
 }
 button.addEventListener('click',async()=>{
  if(sending)return;sending=true;button.disabled=true;button.textContent='Queuing…';panel.open=true;
  try{
   const response=await fetch('/network/desk/scan',{method:'POST',headers:{'X-Desk-Action':'scan'},signal:AbortSignal.timeout(8000)});
   if(!response.ok)throw Error();text('desk-discovery-arp','ARP · Queued');
  }catch{text('desk-discovery-result','Scan request was not confirmed. Check status before retrying.');}
  finally{sending=false;poll();}
 });
 panel.addEventListener('toggle',()=>{if(panel.open)poll();});
 document.addEventListener('visibilitychange',()=>{clearTimeout(timer);if(!document.hidden)poll();});
 poll();
})();
