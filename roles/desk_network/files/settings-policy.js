(()=>{
const style=document.createElement('style');style.id='desk-managed-styles';style.textContent=`
#settingsPage .setting_input.desk-managed-control{position:relative;background:transparent;border:0;z-index:auto;pointer-events:none}
#settingsPage .setting_input.desk-managed-control::after{content:'Cluster managed';position:absolute;inset:0;display:flex;align-items:center;justify-content:flex-end;padding:10px 13px;background:linear-gradient(90deg,transparent 55%,rgba(96,120,138,.07));border:1px solid rgba(128,144,156,.24);border-radius:4px;color:inherit;opacity:.7;font-size:11px;font-weight:normal;pointer-events:auto;cursor:not-allowed;z-index:11}
#settingsPage .desk-managed-control :disabled{opacity:1!important;color:inherit!important;-webkit-text-fill-color:currentColor;box-shadow:none;cursor:not-allowed}
#settingsPage .desk-managed-control .form-control{padding-right:128px;border-color:transparent;background:rgba(128,144,156,.04)}
#settingsPage .desk-managed-control button,#settingsPage .desk-managed-control input[id$="_in"]{display:none}
#settingsPage .desk-managed-control .select2-selection{opacity:1;box-shadow:none;padding-right:118px;border-color:transparent}
#settingsPage .desk-managed-control select[multiple]{min-height:64px;max-height:120px;padding-right:128px}
#settingsPage .desk-managed-control .select2-selection__choice{background:rgba(60,141,188,.12);border:1px solid rgba(128,144,156,.3);color:inherit}
#settingsPage .desk-managed-setting{display:none!important}
`;document.head.append(style);
function markManaged(){
 for(const key of deskManagedKeys){
 for(const suffix of ['', '__metadata']){
  const row=document.getElementById('row_'+key+suffix);if(!row)continue;
  const control=row.querySelector('.setting_input');if(control)control.classList.add('desk-managed-control');
  for(const input of row.querySelectorAll('input,select,textarea,button')){
   // Native overrides can contain a value omitted from a plugin's dropdown.
   // Preserve the actual cached setting instead of silently submitting its first option.
   if(input.id===key&&input.tagName==='SELECT'&&!input.multiple&&typeof window.getSetting==='function'){
    const expected=window.getSetting(key);
    if(typeof expected==='string'&&expected!==''&&!Array.from(input.options).some(option=>option.value===expected)){
     const option=document.createElement('option');option.value=expected;option.textContent=expected+' (cluster managed)';input.append(option);input.value=expected;
     if(typeof window.jQuery==='function'){
      // jQuery namespaced triggers still invoke native inline onchange handlers.
      // Refresh only the widget; do not mark this programmatic correction unsaved.
      const onchange=input.onchange;input.onchange=null;
      try{window.jQuery(input).trigger('change.select2');}finally{input.onchange=onchange;}
     }
    }
   }
   input.disabled=true;input.setAttribute('aria-disabled','true');input.title='Managed by Desk Cluster';
  }
  for(const widget of row.querySelectorAll('.select2-container')){widget.style.pointerEvents='none';widget.setAttribute('aria-disabled','true');}
 }
 }
}
// Native navigation shows its overlay before the unsaved-settings guard runs.
// If navigation is cancelled, this document remains and must become usable again.
window.addEventListener('beforeunload',()=>{
 setTimeout(()=>{if(typeof window.hideSpinner==='function')window.hideSpinner();},0);
});
window.addEventListener('pageshow',event=>{
 if(event.persisted&&typeof window.hideSpinner==='function')window.hideSpinner();
});
let queued=false;
const observer=new MutationObserver(()=>{if(queued)return;queued=true;requestAnimationFrame(()=>{queued=false;markManaged();});});
observer.observe(document.body,{childList:true,subtree:true});markManaged();
// A settings-import SSE can arrive before the replica acknowledgment. Keep the
// current page until Save completes, so its XHR and success/error UI survive.
let savesInFlight=0,reloadAfterSave=false;
const nativeClearCache=window.clearCache;
if(typeof nativeClearCache==='function')window.clearCache=function(...args){
 if(savesInFlight){reloadAfterSave=true;return;}
 return nativeClearCache.apply(this,args);
};
function isSettingsSave(options){
 return new URL(options.url,document.baseURI).pathname==='/network/php/server/util.php'&&new URLSearchParams(typeof options.data==='string'?options.data:'').get('function')==='savesettings';
}
if(typeof window.jQuery==='function'){
 const requests=window.jQuery(document);
 requests.ajaxSend((event,xhr,options)=>{if(isSettingsSave(options))savesInFlight++;});
 requests.ajaxComplete((event,xhr,options)=>{
  if(!isSettingsSave(options))return;
  savesInFlight=Math.max(0,savesInFlight-1);
  if(xhr.status>=400)reloadAfterSave=false;
  if(!savesInFlight&&reloadAfterSave){reloadAfterSave=false;setTimeout(()=>window.clearCache(),1000);}
 });
}
// Native Settings has no AJAX error callback; show rejected saves explicitly.
if(typeof window.jQuery==='function')window.jQuery(document).ajaxError((event,xhr,options)=>{
 const path=new URL(options.url,document.baseURI).pathname;
 if(path!=='/network/php/server/util.php')return;
 const form=new URLSearchParams(typeof options.data==='string'?options.data:'');
 if(form.get('function')!=='savesettings')return;
 const notice=document.getElementById('desk-settings-policy');if(!notice)return;
 notice.textContent=xhr.responseJSON?.error||'Settings could not be saved. Please try again.';notice.setAttribute('role','alert');notice.style.background='#dd4b3922';notice.style.borderColor='#dd4b39';
});
})();
