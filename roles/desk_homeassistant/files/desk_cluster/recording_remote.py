"""Thin Core rendering/client and fail-closed SDK guard for owner-mode cameras."""
import asyncio
import datetime
import functools
import importlib
import json
import pathlib
import secrets
import time
from urllib.parse import urlencode

HOSTS = {f'tapo_c222_{n}':f'192.168.1.{n}' for n in (160,161,163)}


def policy(hass):
    if not hasattr(hass, 'config'):return frozenset()
    path = pathlib.Path(hass.config.path('.storage/desk_camera_recording_authority'))
    try:raw = path.read_bytes()
    except FileNotFoundError:return frozenset()
    if len(raw)>4096:raise ValueError('Core recording policy size')
    value = json.loads(raw)
    if set(value)!={'version','workloads'} or type(value['version']) is not int or value['version']!=1 or not isinstance(value['workloads'],list) or any(not isinstance(w,str) or w not in HOSTS for w in value['workloads']) or len(set(value['workloads']))!=len(value['workloads']):raise ValueError('Core recording policy')
    return frozenset(value['workloads'])


def install_sdk_guard(hass):
    from pytapo import Tapo
    hosts = {HOSTS[w] for w in policy(hass)}
    existing = getattr(Tapo, '_desk_recording_guard', None)
    if existing is not None:
        if existing['hosts'] != hosts:raise ValueError('Authority changes require clean Core restart')
        return existing
    state = {'hosts':hosts,'blocked':0}
    forbidden = {'getUserID','searchVideoOfDay','searchVideoWithUTC','searchDateWithVideo','searchDetectionList'}
    def contains(value):
        if isinstance(value,dict):return (isinstance(value.get('method'),str) and value['method'] in forbidden) or any(contains(v) for v in value.values())
        if isinstance(value,(tuple,list)):return any(contains(v) for v in value)
        return False
    for name in ('getUserID','getRecordings','getRecordingsUTC','getRecordingsList','getMediaSession','executeFunction','performRequest'):
        original = getattr(Tapo,name)
        def wrap(original=original,name=name):
            @functools.wraps(original)
            def guarded(client,*args,**kwargs):
                blocked = name not in ('executeFunction','performRequest') or (name=='executeFunction' and args and args[0] in forbidden) or contains(args) or contains(kwargs)
                if getattr(client,'host',None) in hosts and blocked:
                    state['blocked']+=1
                    raise ValueError('Recording session is managed by its camera owner')
                return original(client,*args,**kwargs)
            return guarded
        setattr(Tapo,name,wrap())
    Tapo._desk_recording_guard = state
    return state


def install_guard(hass):
    state=install_sdk_guard(hass)
    if state.get('native_scheduler_installed'):return
    hosts=state['hosts']
    # Native scheduleAll still probes recordings with media sync switched off.
    # Skip that scheduler for selected entries; ordinary coordinator polling stays.
    native=importlib.import_module('custom_components.tapo_control')
    schedule=native.scheduleAll
    async def scheduled(hass,device,entry,media_sync):
        if entry.data.get('ip_address') in hosts:return
        return await schedule(hass,device,entry,media_sync)
    native.scheduleAll=scheduled
    state['native_scheduler_installed']=True


def guard_status(hass):
    from pytapo import Tapo
    selected = policy(hass);state = getattr(Tapo,'_desk_recording_guard',{})
    return {'version':1,'workloads':sorted(selected),'guarded':state.get('hosts')=={HOSTS[w] for w in selected},'before_native_setup':state.get('before_native_setup') is True,'blocked_core_calls':state.get('blocked',0)}


class OwnerAPI:
    def __init__(self,hass,bridge):
        self.hass,self.bridge,self.workloads=hass,bridge,policy(hass)

    def enabled(self,workload):return workload in self.workloads

    async def read(self,workload,operation,date=None):
        from .recording_contract import page,request as validate,token
        fleet=await self.bridge('cameras')
        camera=next((v for v in fleet['cameras'] if v['workload']==workload and v.get('ready') and v.get('owner') and token(v.get('generation'))),None)
        if not camera:raise ValueError('Camera owner unavailable')
        cursor=None;pages=[];snapshot=None;items=[]
        for _ in range(32):
            query={'cursor':cursor}
            if operation=='index':query['date']=date
            request={'version':1,'workload':workload,'generation':camera['generation'],'request_id':secrets.token_hex(16),
                     'operation':operation,'deadline':time.time()+25,'query':query}
            validate(request,time.time())
            scope={'workload':workload,'node':camera['owner']}
            try:
                reply=await self.bridge('camera_recording',value=dict(scope,request=request))
                while reply['state'] in ('queued','running','cancelling'):
                    if time.time()>=request['deadline']:raise TimeoutError()
                    await asyncio.sleep(.2)
                    poll={k:v for k,v in request.items() if k not in ('query','deadline')};poll['operation']='result'
                    reply=await self.bridge('camera_recording',value=dict(scope,request=poll))
                if reply['state']!='done':raise ValueError('Owner recording '+reply.get('reason','unavailable'))
                if any(reply.get(k)!=request[k] for k in ('version','workload','generation','request_id')):raise ValueError('Owner response binding')
                data=page(request,reply['data'],time.time())
            except BaseException:
                cancel={k:v for k,v in request.items() if k not in ('query','deadline')};cancel['operation']='cancel'
                try:await self.bridge('camera_recording',value=dict(scope,request=cancel))
                except Exception:pass
                raise
            if snapshot is not None and data['snapshot']!=snapshot:raise ValueError('Index snapshot changed')
            snapshot=data['snapshot'];pages.append(data);items.extend(data['items'])
            if len(items)>2048:raise ValueError('Recording index capacity')
            cursor=data['next_cursor']
            if cursor is None:
                return dict(data,items=items,complete=all(p['complete'] or p['next_cursor'] is not None for p in pages))
        raise ValueError('Recording pagination capacity')

    async def browse(self,source,query,entry,workload,device):
        operation='index' if query.get('date') else 'dates'
        data=await self.read(workload,operation,query.get('date','').replace('-','') or None)
        if data['storage']!='ready':raise ValueError('Camera microSD '+data['storage'])
        device['timezoneOffset']=data['camera_offset_seconds']
        def identifier(q):return 'tapo_control/?'+urlencode(q)
        children=[]
        order='media_view_days_order' if operation=='dates' else 'media_view_recordings_order'
        ordered=list(reversed(data['items'])) if entry.data.get(order)=='Descending' else data['items']
        if operation=='dates':
            for d in ordered:
                label=datetime.datetime.strptime(d,'%Y%m%d').strftime('%Y-%m-%d')
                children.append(source.generateView(identifier(dict(query,date=label,title=label)),label,False,True))
        else:
            from homeassistant.util import dt
            for clip in ordered:
                def clock(ts):return dt.as_local(dt.utc_from_timestamp(ts-data['camera_offset_seconds'])).strftime('%H:%M:%S')
                label=clock(clip['start'])+'-'+clock(clip['end'])
                children.append(source.generateView(identifier(dict(query,startDate=clip['start'],endDate=clip['end'],title=label)),label,True,False))
        title=query.get('title',device.get('name','Recordings'))
        if not data['complete'] or data['freshness']=='stale':title+=' · Last known list'
        return source.generateView(identifier(query),title,False,True,children=children)
