#!/usr/bin/python3
"""Read-only 480x320 cluster panel; cached GET, native RGB565, evdev touch."""
import argparse
import ctypes
from datetime import datetime, timezone
import fcntl
import ipaddress
import json
import math
import mmap
import os
from pathlib import Path
import select
import signal
import struct
import threading
import time
from urllib.parse import urlparse
from urllib.request import build_opener, ProxyHandler, Request, HTTPRedirectHandler
from PIL import Image, ImageDraw, ImageFont, ImageChops
from calibration_ui import Calibration, TARGETS, restore as restore_calibration
from display_idle import IdleDisplay, TIMEOUT_OPTIONS, valid_timeout, restore as restore_display, save as save_display
from fetch_settings import MIN_INTERVAL, MAX_INTERVAL, DEFAULT_INTERVAL, valid_interval, parse_interval, restore as restore_fetch, save as save_fetch

WIDTH, HEIGHT = 480, 320
MEMBERS = ('deskb1x', 'deskb2x', 'deskb3x')
NAVIGATION = (('Overview','overview'),('Nodes','node'),('Network','network'),('Events','events'),('Settings','settings'))
BG, SURFACE, BORDER = '#100d08', '#211b12', '#443721'
TEXT, MUTED, GOLD, GREEN, RED = '#f3ead8', '#baad95', '#d5a85e', '#96c9a0', '#de927f'
SETTINGS_ITEMS = (('calibration','Touch calibration','Adjust touch alignment'),
                  ('timeout','Screen timeout','Choose when the picture goes black'),
                  ('fetch','Fetch interval','Choose how often the panel fetches data'))
SETTINGS_LIST_BOX, SETTINGS_ROW_HEIGHT = (14,50,466,274), 72
SETTINGS_BACK_BUTTON = (6,3,50,47)
CALIBRATE_BUTTON = (130,195,350,243)
TIMEOUT_SELECT_BUTTON, TIMEOUT_LIST_BOX = (14,64,466,110), (14,112,466,271)
FETCH_INPUT_BOX, FETCH_APPLY_BUTTON = (14,75,210,119), (14,134,210,178)
FETCH_KEYS = tuple((label,(236+(i%3)*80,64+(i//3)*53,306+(i%3)*80,107+(i//3)*53))
                   for i,label in enumerate(('1','2','3','4','5','6','7','8','9','<','0','Clear')))
START_BUTTON, CONFIRM_CANCEL_BUTTON = (100, 243, 228, 291), (252, 243, 380, 291)
APPLY_BUTTON, RETRY_BUTTON, CANCEL_BUTTON = (32, 243, 160, 291), (176, 243, 304, 291), (320, 243, 448, 291)
COLLECT_CANCEL_BUTTON = (358, 275, 466, 319)
NODE_TAB_TOP, NODE_TAB_BOTTOM = 45, 75
OVERVIEW_CARD_TOP, OVERVIEW_CARD_BOTTOM = 45, 224
NODE_STATUS_BUTTON, NODE_SERVICES_BUTTON = (238,85,344,115), (356,85,466,115)

def inside(point, box):
    return box[0] <= point[0] <= box[2] and box[1] <= point[1] <= box[3]

class SettingsMenu:
    """Bounded list scrolling; a drag never selects an item on release."""
    def __init__(self,items=SETTINGS_ITEMS,box=SETTINGS_LIST_BOX,row_height=SETTINGS_ROW_HEIGHT):
        self.items=items;self.box=box;self.row_height=row_height;self.offset=0;self.cancel()
    @property
    def max_offset(self):
        return max(0,len(self.items)*self.row_height-(self.box[3]-self.box[1]+1))
    def cancel(self):
        self.start=None;self.dragged=False
    def begin(self,point):
        self.cancel()
        if inside(point,self.box):self.start=point;self.anchor=self.offset
    def move(self,point):
        if self.start is None:return False
        dx,dy=point[0]-self.start[0],point[1]-self.start[1]
        if max(abs(dx),abs(dy))>=12:self.dragged=True
        previous=self.offset
        if self.dragged:self.offset=max(0,min(self.max_offset,self.anchor-dy))
        return self.offset!=previous
    def end(self,point):
        self.move(point)
        start,dragged=self.start,self.dragged;self.cancel()
        if start is None or dragged or not inside(point,self.box):return None
        row=(point[1]-self.box[1]+self.offset)//self.row_height
        first=(start[1]-self.box[1]+self.offset)//self.row_height
        return self.items[row][0] if row==first and 0<=row<len(self.items) else None

class TimeoutSelect(SettingsMenu):
    def __init__(self):
        super().__init__(TIMEOUT_OPTIONS,TIMEOUT_LIST_BOX,40)
        self.opened=False
    def open(self,current):
        index=next(i for i,(seconds,_) in enumerate(self.items) if seconds==current)
        self.offset=max(0,min(self.max_offset,(index-1)*self.row_height))
        self.cancel();self.opened=True
    def close(self):
        self.opened=False;self.cancel()

def finite(value):
    return type(value) in (int, float) and math.isfinite(value)

def timestamp(value):
    try:
        dt = datetime.fromisoformat(value.replace('Z', '+00:00'))
        return dt.timestamp() if dt.tzinfo else None
    except (ValueError, AttributeError, TypeError):
        return None

def validate(value):
    if not isinstance(value, dict) or value.get('protocol') != 'desk-cluster-panel-v1':
        raise ValueError('Panel protocol mismatch')
    if value.get('state') not in ('ready', 'degraded', 'unavailable'):
        raise ValueError('Invalid panel state')
    nodes = value.get('nodes')
    if not isinstance(nodes, list) or len(nodes) != 3 or any(not isinstance(n,dict) for n in nodes):
        raise ValueError('Invalid panel membership')
    if tuple(n.get('id') for n in nodes) != MEMBERS:
        raise ValueError('Unexpected panel membership')
    for n in nodes:
        if type(n.get('online')) is not bool:
            raise ValueError('Invalid node availability')
        for field in ('cpu_percent','memory_percent','temperature_c','age_ms'):
            v=n.get(field)
            if v is not None and (not finite(v) or v<0 or v>1e9):
                raise ValueError('Invalid node value')
        for field in ('jobs','components','configuration','admission_paused','vip'):
            if not isinstance(n.get(field),dict):
                raise ValueError('Invalid node metadata')
        if n.get('vpn_ready') is not None and type(n['vpn_ready']) is not bool:
            raise ValueError('Invalid VPN readiness')
        if n.get('monitoring_ready') is not None and type(n['monitoring_ready']) is not bool:
            raise ValueError('Invalid monitoring readiness')
        if any(n['vip'].get(k) is not None and type(n['vip'][k]) is not bool for k in ('xiaozhi','dns','vpn')):
            raise ValueError('Invalid VIP ownership')
        for v in n['jobs'].values():
            if v is not None and (type(v) is not int or not 0<=v<=2):
                raise ValueError('Invalid activity')
        info=n.get('network')
        if info is not None:
            if not isinstance(info,dict) or info.get('state') not in ('active','standby','starting') or info.get('sync') not in ('synced','pending'):
                raise ValueError('Invalid Network state')
            if type(info.get('voter_ready')) is not bool or info.get('subnet')!='192.168.1.0/24' or type(info.get('scan_minutes')) is not int or not 0<=info['scan_minutes']<=60:
                raise ValueError('Invalid Network metadata')
            schedule=info.get('scan_schedule','')
            if not isinstance(schedule,str) or len(schedule)>128 or any(c in schedule for c in '\r\n\0'):
                raise ValueError('Invalid Network schedule')
            if not finite(info.get('age_ms')) or not 0<=info['age_ms']<=1e12:
                raise ValueError('Invalid Network age')
            revision=info.get('revision')
            if revision is not None and (not isinstance(revision,dict) or revision.get('node') not in MEMBERS or any(type(revision.get(k)) is not int or not 0<=revision[k]<=1e9 for k in ('term','sequence'))):
                raise ValueError('Invalid Network checkpoint')
    if not isinstance(value.get('owners'),dict) or not isinstance(value.get('dns'),dict):
        raise ValueError('Invalid routing metadata')
    if value.get('monitoring_preferred') is not None and value['monitoring_preferred'] not in MEMBERS:
        raise ValueError('Invalid monitoring preference')
    for key in ('dns','xiaozhi')+tuple(k for k in ('vpn','network') if k in value['owners']):
        o=value['owners'].get(key)
        if not isinstance(o,dict) or o.get('state') not in ('known','conflict','unknown') or (o.get('node') is not None and o['node'] not in MEMBERS):
            raise ValueError('Invalid owner')
    if value['dns'].get('mode') not in ('adguard','fallback','unavailable','unknown'):
        raise ValueError('Invalid DNS mode')
    events=value.get('events')
    if not isinstance(events,list) or len(events)>3 or any(not isinstance(e,dict) or not isinstance(e.get('message'),str) or len(e['message'])>256 or e.get('node') not in MEMBERS for e in events):
        raise ValueError('Invalid events')
    for n in nodes:
        for config in n['configuration'].values():
            if not isinstance(config,dict) or config.get('state') not in ('synced','pending','failed','unknown'):
                raise ValueError('Invalid config state')
        if not isinstance(n.get('ip'),str) or len(n['ip'])>64:
            raise ValueError('Invalid node IP')
    if timestamp(value.get('observed_at')) is None:
        raise ValueError('Invalid observation timestamp')
    if not finite(value.get('age_ms')) or not 0<=value['age_ms']<=1e12:
        raise ValueError('Invalid observation age')
    return value

class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self,*args):return None

class Model:
    def __init__(self,interval=DEFAULT_INTERVAL):
        if not valid_interval(interval):raise ValueError('Invalid fetch interval')
        self.lock=threading.Lock()
        self.schedule=threading.Condition(self.lock)
        self.interval=interval;self.revision=0
        self.value=None
        self.received=0
        self.ok=False
        self.stopped=threading.Event()
        self.opener=build_opener(ProxyHandler({}),NoRedirect())
    def set_interval(self,interval):
        if not valid_interval(interval):raise ValueError('Invalid fetch interval')
        with self.schedule:
            self.interval=interval;self.revision+=1;self.schedule.notify_all()
    def stop(self):
        with self.schedule:
            self.stopped.set();self.schedule.notify_all()
    def poll(self,url):
        while not self.stopped.is_set():
            with self.lock:revision=self.revision
            try:
                with self.opener.open(Request(url,headers={'Accept':'application/json'}),timeout=3) as r:
                    raw=r.read(8193)
                    if len(raw)>8192:raise ValueError('Oversize snapshot')
                    value=validate(json.loads(raw))
                with self.lock:
                    self.value,self.received,self.ok=value,time.monotonic(),True
            except (OSError,ValueError,TypeError):
                with self.lock:self.ok=False
            with self.schedule:
                self.schedule.wait_for(lambda:self.stopped.is_set() or self.revision!=revision,timeout=self.interval)
    def snapshot(self):
        with self.lock:
            return self.value,self.received,self.ok

def freshness(value,received,now):
    if not value or not received:return False,0
    elapsed=max(0,(now-received)*1000)
    age=value['age_ms']+elapsed
    return age<=20000 and value['state']!='unavailable',elapsed

def node_live(n,live,elapsed):
    age=n.get('age_ms')
    return live and n['online'] and finite(age) and age+elapsed<=20000

def network_live(n,live,elapsed):
    info=n.get('network')
    return node_live(n,live,elapsed) and bool(info) and info['age_ms']+elapsed<15000

def network_detail(n,value,live,elapsed):
    if not network_live(n,live,elapsed):
        return {'role':'Unavailable','label':'Unknown','revision':'No fresh metadata','ok':False}
    info=n['network'];revision=info.get('revision')
    def key(r):return r['term'],r['node'],r['sequence']
    revisions=[peer['network']['revision'] for peer in value['nodes'] if network_live(peer,live,elapsed) and peer['network'].get('revision')]
    ok=info['sync']=='synced' and info['voter_ready'] and revision is not None and key(revision)==max(map(key,revisions))
    return {'role':{'active':'Collector','standby':'Cold standby','starting':'Starting'}[info['state']],
            'label':'Warming up' if not info['voter_ready'] else 'Synced' if ok else 'Pending',
            'revision':f'Checkpoint {revision["term"]}.{revision["sequence"]}' if revision else 'No checkpoint','ok':ok}

def navigation_page(x):
    return NAVIGATION[min(len(NAVIGATION)-1,max(0,int(x)*len(NAVIGATION)//WIDTH))][1]

def vip_role(n,key):
    value=n['vip'].get(key)
    return 'Owner' if value is True else 'Standby' if value is False else 'Unconfirmed'

class Renderer:
    def __init__(self,font_root='/usr/share/fonts/truetype/dejavu'):
        root=Path(font_root)
        self.fonts={size:ImageFont.truetype(str(root/'DejaVuSans.ttf'),size,layout_engine=ImageFont.Layout.BASIC) for size in (11,12,13,14,16,18,22)}
        self.bold={size:ImageFont.truetype(str(root/'DejaVuSans-Bold.ttf'),size,layout_engine=ImageFont.Layout.BASIC) for size in (12,13,14,16,18,22)}
    def text(self,d,xy,value,size=12,color=TEXT,bold=False,width=None):
        value=str(value)
        font=(self.bold if bold else self.fonts)[size]
        if width:
            while value and d.textlength(value,font=font)>width:
                value=value[:-2]+'…' if not value.endswith('…') else value[:-2]+'…'
        d.text(xy,value,font=font,fill=color)
    def draw(self,value,received,ok,page='overview',selected=0,now=None,node_view='status'):
        now=time.monotonic() if now is None else now
        live,elapsed=freshness(value,received,now)
        image=Image.new('RGB',(WIDTH,HEIGHT),BG);d=ImageDraw.Draw(image)
        font=self.bold[14];bounds=d.textbbox((0,0),'DESK CLUSTER',font=font)
        d.text((14-bounds[0],(35-(bounds[3]-bounds[1]))/2-bounds[1]),'DESK CLUSTER',font=font,fill=GOLD)
        status='Connecting' if value is None else 'Stale' if not live else 'Reconnecting' if not ok else value['state'].title()
        self.right_text(d,(330,0,466,35),status,12,GREEN if live and ok and value['state']=='ready' else RED)
        d.line((14,35,466,35),fill=BORDER)
        if page=='overview':self.overview(d,value,live,elapsed)
        elif page=='node':self.node(d,value,live,elapsed,selected,node_view)
        elif page=='network':self.network(d,value,live,elapsed)
        else:self.events(d,value,live)
        self.navigation(d,page)
        return image
    def button(self,d,box,label,primary=False):
        d.rounded_rectangle(box,radius=8,fill=GOLD if primary else SURFACE,outline=GOLD if primary else BORDER)
        self.center_text(d,box,label,14,BG if primary else TEXT,True)
    def center_text(self,d,box,label,size,color,bold=False):
        font=(self.bold if bold else self.fonts)[size]
        left,top,right,bottom=d.textbbox((0,0),label,font=font)
        x=(box[0]+box[2]-(right-left))/2-left
        y=(box[1]+box[3]-(bottom-top))/2-top
        d.text((x,y),label,font=font,fill=color)
    def right_text(self,d,box,label,size,color):
        font=self.fonts[size]
        left,top,right,bottom=d.textbbox((0,0),label,font=font)
        d.text((box[2]-right,(box[1]+box[3]-(bottom-top))/2-top),label,font=font,fill=color)
    def columns(self,d,box,count):
        d.rounded_rectangle(box,radius=8,fill=SURFACE,outline=BORDER)
        width=(box[2]-box[0])/count
        for i in range(1,count):
            x=round(box[0]+i*width)
            d.line((x,box[1]+9,x,box[3]-9),fill=BORDER)
        return [(round(box[0]+i*width),box[1],round(box[0]+(i+1)*width),box[3]) for i in range(count)]
    def settings(self,config,error=None,view='menu',menu=None,timeout_select=None,fetch_input=None,fetch_error=None):
        image=Image.new('RGB',(WIDTH,HEIGHT),BG);d=ImageDraw.Draw(image)
        title={'menu':'Panel settings','calibration':'Touch calibration','timeout':'Screen timeout','fetch':'Fetch interval'}[view]
        self.text(d,(60 if view!='menu' else 14,12),title,18,TEXT,True)
        if view!='menu':self.center_text(d,SETTINGS_BACK_BUTTON,'<',22,GOLD)
        d.line((14,49,466,49),fill=BORDER)
        if view=='menu':
            menu=menu or SettingsMenu()
            left,top,right,bottom=SETTINGS_LIST_BOX
            viewport=Image.new('RGB',(right-left+1,bottom-top+1),BG);rows=ImageDraw.Draw(viewport)
            for i,(_,label,detail) in enumerate(menu.items):
                y=i*SETTINGS_ROW_HEIGHT-menu.offset
                if y+SETTINGS_ROW_HEIGHT<=0 or y>=viewport.height:continue
                self.text(rows,(0,y+12),label,16,TEXT,True,width=viewport.width-40)
                self.text(rows,(0,y+40),detail,12,MUTED,width=viewport.width-40)
                self.center_text(rows,(viewport.width-30,y,viewport.width,y+SETTINGS_ROW_HEIGHT),'›',18,GOLD)
                rows.line((0,y+SETTINGS_ROW_HEIGHT-1,viewport.width-1,y+SETTINGS_ROW_HEIGHT-1),fill=BORDER)
            image.paste(viewport,(left,top))
            if menu.max_offset:
                track=bottom-top+1;thumb=max(20,round(track*track/(len(menu.items)*SETTINGS_ROW_HEIGHT)))
                y=top+round((track-thumb)*menu.offset/menu.max_offset)
                d.line((474,top,474,bottom),fill=BORDER,width=2)
                d.line((474,y,474,y+thumb-1),fill=GOLD,width=2)
        elif view=='calibration':
            self.text(d,(14,69),'Recalibrate for the mounted screen.',14,TEXT)
            self.text(d,(14,99),'Use a stylus or a steady fingertip.',13,MUTED)
            self.text(d,(14,131),f'Mount rotation: {config.get("rotation",0)} degrees',12,MUTED)
            error_px=config.get('calibration_error')
            label=f'Last validated error: {error_px:.1f} px' if finite(error_px) else 'No saved validation result'
            self.text(d,(14,153),label,12,GREEN if finite(error_px) else MUTED)
            self.button(d,CALIBRATE_BUTTON,'Calibrate touch',True)
        elif view=='timeout':
            current=config.get('screen_timeout',0)
            opened=timeout_select is not None and timeout_select.opened
            d.rounded_rectangle(TIMEOUT_SELECT_BUTTON,radius=6,fill=SURFACE,outline=GOLD if opened else BORDER)
            self.text(d,(30,77),dict(TIMEOUT_OPTIONS)[current],16,TEXT,True)
            if opened:d.line((432,89,438,83,444,89),fill=GOLD,width=2)
            else:d.line((432,83,438,89,444,83),fill=GOLD,width=2)
            self.text(d,(14,144),'Blanks picture; backlight stays on.',13,MUTED)
            self.text(d,(14,174),'Touch to wake; first touch only wakes.',12,MUTED)
            if error:self.text(d,(14,220),error,12,RED,width=452)
            if opened:self.timeout_options(image,timeout_select,current)
        elif view=='fetch':
            current=config.get('fetch_interval',DEFAULT_INTERVAL)
            draft=str(current) if fetch_input is None else fetch_input
            value=parse_interval(draft)
            self.text(d,(14,55),'Interval in seconds',12,MUTED)
            d.rounded_rectangle(FETCH_INPUT_BOX,radius=6,fill=SURFACE,outline=GOLD)
            self.center_text(d,FETCH_INPUT_BOX,(draft or '—')+' s',22,TEXT,True)
            self.button(d,FETCH_APPLY_BUTTON,'Apply',value is not None and value!=current)
            self.text(d,(14,191),f'Min {MIN_INTERVAL}s · Max {MAX_INTERVAL}s',13,MUTED)
            self.text(d,(14,216),f'Current: {current}s',13,GREEN)
            if fetch_error:self.text(d,(14,244),fetch_error,11,RED,width=206)
            elif value is None:self.text(d,(14,244),'Enter 1–60 seconds.',12,RED)
            for label,box in FETCH_KEYS:self.button(d,box,label)
        self.navigation(d,'settings')
        return image
    def timeout_options(self,image,menu,current):
        left,top,right,bottom=menu.box
        viewport=Image.new('RGB',(right-left+1,bottom-top+1),SURFACE);d=ImageDraw.Draw(viewport)
        for i,(seconds,label) in enumerate(menu.items):
            y=i*menu.row_height-menu.offset
            if y+menu.row_height<=0 or y>=viewport.height:continue
            selected=seconds==current
            if selected:d.rectangle((0,y,viewport.width-1,y+menu.row_height-1),fill=GOLD)
            color=BG if selected else TEXT
            self.center_text(d,(16,y,110,y+menu.row_height),label,14,color,selected)
            if selected:self.center_text(d,(viewport.width-64,y,viewport.width-20,y+menu.row_height),'✓',16,color)
            d.line((16,y+menu.row_height-1,viewport.width-16,y+menu.row_height-1),fill=BORDER)
        # Position indicator stays inside the popup and cannot overlap navigation.
        thumb=max(20,round(viewport.height*viewport.height/(len(menu.items)*menu.row_height)))
        y=round((viewport.height-thumb)*menu.offset/menu.max_offset) if menu.max_offset else 0
        d.line((viewport.width-4,0,viewport.width-4,viewport.height-1),fill=BORDER,width=2)
        d.line((viewport.width-4,y,viewport.width-4,y+thumb-1),fill=GOLD,width=2)
        d.rectangle((0,0,viewport.width-1,viewport.height-1),outline=BORDER)
        image.paste(viewport,(left,top))
    def navigation(self,d,page):
        d.line((0,275,480,275),fill=BORDER)
        width=WIDTH//len(NAVIGATION)
        for i,(label,target) in enumerate(NAVIGATION):
            box=(i*width,276,(i+1)*width-1,319)
            if page==target:d.rectangle(box,fill=SURFACE)
            self.center_text(d,box,label,13,GOLD if page==target else MUTED,bold=page==target)
    def calibration(self,cal):
        image=Image.new('RGB',(WIDTH,HEIGHT),BG);d=ImageDraw.Draw(image)
        self.text(d,(14,10),'Touch calibration',16,TEXT,True)
        if cal.phase=='confirm':
            self.text(d,(26,66),'Tap four corners and the centre.',16,TEXT,True)
            self.text(d,(26,106),'Old mapping stays until Apply.',14,MUTED)
            self.text(d,(26,139),'Cancel or timeout keeps current settings.',12,MUTED)
            self.button(d,START_BUTTON,'Start',True)
            self.button(d,CONFIRM_CANCEL_BUTTON,'Cancel')
        elif cal.phase=='collect':
            index=len(cal.samples);tx,ty=TARGETS[index]
            d.ellipse((tx-24,ty-24,tx+24,ty+24),outline=GOLD,width=3)
            d.line((tx-12,ty,tx+12,ty),fill=GOLD,width=2);d.line((tx,ty-12,tx,ty+12),fill=GOLD,width=2)
            if index!=4:
                self.text(d,(93,124),f'Target {index+1} / 5',18,GOLD,True)
                self.text(d,(93,158),cal.message,12,MUTED,width=330)
            else:
                self.text(d,(120,75),'Centre check · 5 / 5',16,GOLD,True)
                self.text(d,(56,191),cal.message,12,MUTED,width=368)
            self.button(d,COLLECT_CANCEL_BUTTON,'Cancel')
            self.text(d,(14,291),'Hold anywhere 2s to cancel',11,MUTED,width=307)
        else:
            good=cal.phase=='review'
            self.text(d,(14,43),'Check the new mapping' if good else 'Calibration needs retry',16,GREEN if good else RED,True)
            if good:
                self.text(d,(14,72),f'Validated error: {cal.error:.1f} px',13,GREEN)
                d.rounded_rectangle((14,101,466,219),radius=8,fill=SURFACE,outline=BORDER)
                self.text(d,(30,110),cal.message,12,MUTED,width=422)
                if cal.marker:
                    x,y=cal.marker
                    if 14<=x<=466 and 101<=y<=219:
                        d.ellipse((x-10,y-10,x+10,y+10),outline=GOLD,width=2)
                        d.line((x-14,y,x+14,y),fill=GOLD);d.line((x,y-14,x,y+14),fill=GOLD)
            else:
                self.text(d,(14,90),cal.message,14,MUTED,width=450)
                self.text(d,(14,124),'Your previous mapping is still active.',13,MUTED)
            if good:self.button(d,APPLY_BUTTON,'Apply',True)
            self.button(d,RETRY_BUTTON,'Retry',not good)
            self.button(d,CANCEL_BUTTON,'Cancel')
        return image
    def owner(self,value,key,live,elapsed):
        o=(value or {}).get('owners',{}).get(key,{})
        node=next((n for n in (value or {}).get('nodes',[]) if n['id']==o.get('node')),None)
        if key=='network':
            if o.get('state')=='conflict':
                active=[n for n in (value or {}).get('nodes',[]) if network_live(n,live,elapsed) and n['network']['state']=='active' and n['network']['voter_ready']]
                return 'Conflict' if len(active)>1 else 'Unconfirmed'
            if not node or not network_live(node,live,elapsed):return 'Unconfirmed'
        elif o.get('state')=='conflict':
            owners=[n for n in (value or {}).get('nodes',[]) if node_live(n,live,elapsed) and n['vip'].get(key) is True]
            return 'Conflict' if len(owners)>1 else 'Unconfirmed'
        return o['node'] if node and node_live(node,live,elapsed) and o.get('state')=='known' else 'Unconfirmed'
    def overview_roles(self,n,value,live,elapsed):
        if not node_live(n,live,elapsed):return ['Unconfirmed']
        roles=[]
        for key,label in (('xiaozhi','Xiaozhi'),('dns','DNS'),('vpn','VPN')):
            if n['vip'].get(key) is True:
                roles.append(label+(' !' if self.owner(value,key,live,elapsed)=='Conflict' else ''))
        info=n.get('network') if network_live(n,live,elapsed) else None
        if info and info['state']=='active' and info['voter_ready']:
            roles.append('Network'+(' !' if self.owner(value,'network',live,elapsed)=='Conflict' else ''))
        if value.get('monitoring_preferred')==n['id'] and n.get('monitoring_ready') is True and self.owner(value,'xiaozhi',live,elapsed) in MEMBERS:
            roles.append('Monitor')
        if roles:return roles
        standby=all(n['vip'].get(key) is False for key in ('xiaozhi','dns','vpn')) and info is not None and info['state']=='standby'
        return ['Standby' if standby else 'Unconfirmed']
    def overview(self,d,value,live,elapsed):
        for i in range(3):
            x=14+i*155;n=value['nodes'][i] if value else None
            online=n is not None and node_live(n,live,elapsed)
            d.rounded_rectangle((x,OVERVIEW_CARD_TOP,x+142,OVERVIEW_CARD_BOTTOM),radius=10,fill=SURFACE,outline=BORDER)
            self.text(d,(x+12,55),MEMBERS[i],14,TEXT,True)
            d.ellipse((x+123,62,x+129,68),fill=GREEN if online else RED)
            d.line((x+12,81,x+130,81),fill=BORDER)
            if not online:
                self.text(d,(x+12,92),'Telemetry',12,MUTED)
                self.text(d,(x+12,117),'unavailable',12,RED)
                self.text(d,(x+12,191),'Unconfirmed',11,MUTED)
            else:
                jobs=n['jobs'];busy=[label for key,label in (('asr','ASR'),('llm_vlm','LLM/VLM'),('tts','TTS')) if finite(jobs.get(key)) and jobs[key]>0]
                activity=' + '.join(busy) if busy else 'Paused' if any(v is True for v in n['admission_paused'].values()) else 'Voice idle' if all(jobs.get(k)==0 for k in ('asr','llm_vlm','tts')) else 'Voice unknown'
                self.text(d,(x+12,92),activity,12,GOLD if busy else MUTED,width=118)
                cpu=n.get('cpu_percent');temp=n.get('temperature_c');mem=n.get('memory_percent')
                self.text(d,(x+12,117),f'{cpu:.0f}% CPU' if finite(cpu) else 'CPU —',16,TEXT,True)
                memory=f'{mem:.0f}% RAM' if finite(mem) else 'RAM —'
                temperature=f'{temp:.0f}°C' if finite(temp) else '—°C'
                self.text(d,(x+12,147),memory+' - '+temperature,13,TEXT,width=122)
                d.line((x+12,181,x+130,181),fill=BORDER)
                roles=self.overview_roles(n,value,live,elapsed)
                for compact in (False,True):
                    lines=['']
                    for role in roles:
                        label=role.replace('Network','Net').replace('Monitor','Mon') if compact else role
                        candidate=lines[-1]+' · '+label if lines[-1] else label
                        if d.textlength(candidate,font=self.fonts[11])>118:lines.append(label)
                        else:lines[-1]=candidate
                    if len(lines)<=2:break
                for row,line in enumerate(lines):
                    color=RED if '!' in line else MUTED if roles in (['Standby'],['Unconfirmed']) else GOLD
                    self.text(d,(x+12,(188 if len(lines)>1 else 197)+row*17),line,11,color,width=118)
        mode=(value or {}).get('dns',{}).get('mode')
        label={'adguard':'AdGuard filtering','fallback':'Fallback · 1.1.1.1','unavailable':'DNS unavailable'}.get(mode,'DNS unconfirmed') if live else 'DNS unconfirmed'
        online=sum(node_live(n,live,elapsed) for n in value['nodes']) if value else 0
        self.text(d,(14,236),label,12,GREEN if live and mode=='adguard' else RED)
        self.right_text(d,(340,232,466,254),f'{online}/3 online',12,MUTED)
        self.text(d,(14,256),'Touch a node for details',11,MUTED)
    def network(self,d,value,live,elapsed):
        self.text(d,(14,46),'Network collector',14,TEXT,True)
        owner=self.owner(value,'network',live,elapsed)
        self.right_text(d,(270,41,466,67),owner,13,GOLD)
        active=next((n['network'] for n in (value or {}).get('nodes',[]) if n['id']==owner and network_live(n,live,elapsed)),None)
        scope='Waiting for a fresh collector'
        if active:
            scan=f'scan every {active["scan_minutes"]} min' if active['scan_minutes'] else 'schedule '+(active.get('scan_schedule') or 'unavailable')
            scope='LAN '+active['subnet']+' · '+scan
        self.text(d,(14,75),scope,12,MUTED,width=452)
        for i,node_id in enumerate(MEMBERS):
            n=next((n for n in (value or {}).get('nodes',[]) if n['id']==node_id),None)
            detail=network_detail(n,value,live,elapsed) if n else {'role':'Unavailable','label':'Unknown','revision':'No fresh metadata','ok':False}
            y=101+i*54
            d.rounded_rectangle((14,y,466,y+47),radius=8,fill=SURFACE,outline=BORDER)
            self.text(d,(26,y+6),node_id,13,TEXT,True)
            self.text(d,(154,y+6),detail['role'],12,GOLD if detail['role']=='Collector' else MUTED)
            self.right_text(d,(322,y+2,452,y+25),detail['label'],12,GREEN if detail['ok'] else MUTED)
            self.text(d,(26,y+27),detail['revision'],11,MUTED,width=410)
    def node(self,d,value,live,elapsed,index,node_view='status'):
        for i in range(3):
            x=14+i*155
            box=(x,NODE_TAB_TOP,x+142,NODE_TAB_BOTTOM)
            d.rounded_rectangle(box,radius=7,fill=SURFACE if i==index else BG,outline=GOLD if i==index else BORDER)
            self.center_text(d,box,MEMBERS[i],12,GOLD if i==index else MUTED,True)
        n=value['nodes'][index] if value else None
        online=n is not None and node_live(n,live,elapsed)
        self.text(d,(14,92),n.get('ip','') if n else 'Waiting for cluster',13,TEXT,True)
        self.text(d,(161,94),'Online' if online else 'Offline',11,GREEN if online else RED)
        for box,label,target in ((NODE_STATUS_BUTTON,'Status','status'),(NODE_SERVICES_BUTTON,'Services','services')):
            d.rounded_rectangle(box,radius=6,fill=SURFACE if node_view==target else BG,outline=GOLD if node_view==target else BORDER)
            self.center_text(d,box,label,12,GOLD if node_view==target else MUTED,node_view==target)
        if not online:
            d.rounded_rectangle((14,123,466,258),radius=8,fill=SURFACE,outline=BORDER)
            self.text(d,(28,145),'Fresh telemetry required',16,RED)
            self.text(d,(28,179),'Owner, activity and health are unconfirmed.',12,MUTED,width=424)
            return
        if node_view=='services':
            boxes=self.columns(d,(14,125,466,174),3)
            for box,(label,key) in zip(boxes,(('Xiaozhi VIP','xiaozhi'),('DNS VIP','dns'),('VPN VIP','vpn'))):
                role=vip_role(n,key)
                self.text(d,(box[0]+12,131),label,12,MUTED)
                self.text(d,(box[0]+12,150),role,14,GOLD if role=='Owner' else TEXT,True,width=126)
            ready=n.get('vpn_ready')
            self.text(d,(26,184),'VPN eligibility',12,MUTED)
            self.right_text(d,(250,179,452,202),'Ready' if ready is True else 'Not eligible' if ready is False else 'Unconfirmed',12,GREEN if ready is True else MUTED)
            detail=network_detail(n,value,live,elapsed)
            d.rounded_rectangle((14,209,466,268),radius=8,fill=SURFACE,outline=BORDER)
            self.text(d,(26,215),'Network · '+detail['role'],12,TEXT,True)
            self.right_text(d,(310,210,452,234),detail['label'],12,GREEN if detail['ok'] else MUTED)
            info=n.get('network') if network_live(n,live,elapsed) else None
            scope='Waiting for fresh service metadata'
            if info:
                scope='LAN '+info['subnet']
                if info['state']!='standby':scope+=' · '+(f'scan every {info["scan_minutes"]} min' if info['scan_minutes'] else (info.get('scan_schedule') or 'schedule unavailable'))
            self.text(d,(26,235),scope,11,MUTED,width=426)
            self.text(d,(26,251),detail['revision'],11,MUTED,width=426)
            return
        boxes=self.columns(d,(14,125,466,169),3)
        for box,(label,field,suffix) in zip(boxes,(('CPU','cpu_percent','%'),('Memory','memory_percent','%'),('Temp','temperature_c','°C'))):
            v=n.get(field)
            self.text(d,(box[0]+12,131),label,11,MUTED)
            self.text(d,(box[0]+12,149),f'{v:.1f}{suffix}' if finite(v) else '—',14,TEXT,True)
        boxes=self.columns(d,(14,178,466,218),5)
        for box,(label,key) in zip(boxes,(('Voice','worker'),('Core','core'),('MQTT','mqtt'),('DNS','dns'),('VPN','vpn'))):
            state=n['components'].get(key) if key!='vpn' else 'ready' if n.get('vpn_ready') is True else 'unavailable' if n.get('vpn_ready') is False else None
            ready=state=='ready'
            self.text(d,(box[0]+12,182),label,11,MUTED)
            self.text(d,(box[0]+12,199),'Ready' if ready else 'Unknown' if state is None else 'Not ready',11,GREEN if ready else RED,width=box[2]-box[0]-24)
        boxes=self.columns(d,(14,227,466,268),3)
        for box,(label,key) in zip(boxes,(('Xiaozhi','xiaozhi'),('DNS','dns'),('Monitor','monitoring'))):
            cfg=n['configuration'].get(key,{})
            txt=f'{label} · r{cfg["revision"]}' if type(cfg.get('revision')) is int else label+' · —'
            self.text(d,(box[0]+12,231),txt,11,MUTED,width=126)
            self.text(d,(box[0]+12,249),cfg.get('state','unknown').title(),11,GREEN if cfg.get('state')=='synced' else RED,width=126)
    def events(self,d,value,live):
        self.text(d,(14,46),'Recent events',14,TEXT,True)
        if not live:self.right_text(d,(310,41,466,67),'Cached',12,RED)
        events=(value or {}).get('events',[])
        events=events[:3] if isinstance(events,list) else []
        if not events:
            d.rounded_rectangle((14,81,466,140),radius=8,fill=SURFACE,outline=BORDER)
            self.text(d,(28,102),'No retained observations',13,MUTED)
        for i,e in enumerate(events):
            if not isinstance(e,dict):continue
            y=81+i*63
            d.rounded_rectangle((14,y,466,y+54),radius=8,fill=SURFACE,outline=BORDER)
            self.text(d,(28,y+7),e.get('node',''),12,GOLD,width=120)
            dt=timestamp(e.get('at'));t=datetime.fromtimestamp(dt).astimezone().strftime('%H:%M:%S') if dt else '—'
            self.right_text(d,(336,y+4,452,y+27),t,11,MUTED)
            self.text(d,(28,y+29),e.get('message',''),12,TEXT,width=424)
        if not live:self.text(d,(14,256),'Current cluster state is unconfirmed',11,RED,width=452)

def validate_framebuffer(v,stride):
    if (v[0],v[1],v[6],v[8:10],v[11:13],v[14:16]) != (480,320,16,(11,5),(5,6),(0,5)) or v[4] or v[5] or stride!=960:
        raise ValueError('Expected 480x320 RGB565 without viewport offsets')

class Framebuffer:
    def __init__(self,path,packer,rotation=0):
        if rotation not in (0,180):raise ValueError("Supported panel rotation: 0 or 180")
        self.rotation=rotation
        self.fd=os.open(path,os.O_RDWR)
        v=struct.unpack('40I',fcntl.ioctl(self.fd,0x4600,bytes(160)))
        self.width,self.height=v[:2]
        name=Path(path).name
        stride=int(Path('/sys/class/graphics',name,'stride').read_text())
        validate_framebuffer(v,stride)
        self.buffer=mmap.mmap(self.fd,stride*self.height,mmap.MAP_SHARED,mmap.PROT_WRITE|mmap.PROT_READ)
        self.lib=ctypes.CDLL(packer);self.pack=self.lib.desk_rgb565
        self.pack.argtypes=(ctypes.c_void_p,ctypes.c_void_p,ctypes.c_size_t);self.pack.restype=None
        self.output=ctypes.create_string_buffer(WIDTH*HEIGHT*2);self.previous=None
    def show(self,image):
        if self.rotation:image=image.transpose(Image.Transpose.ROTATE_180)
        diff=ImageChops.difference(image,self.previous).getbbox() if self.previous else (0,0,WIDTH,HEIGHT)
        if diff is None:return
        left,top,right,bottom=diff
        crop=image.crop(diff);raw=crop.tobytes();count=(right-left)*(bottom-top)
        self.pack(raw,self.output,count);packed=self.output.raw[:count*2];row=(right-left)*2
        for y in range(bottom-top):
            start=(top+y)*960+left*2
            self.buffer[start:start+row]=packed[y*row:(y+1)*row]
        self.previous=image
    def close(self):self.buffer.close();os.close(self.fd)

def touch_point(x,y,rx,ry,config):
    def normalize(v,r):return max(0,min(1,(v-r.min)/(r.max-r.min)))
    u,v=normalize(x,rx),normalize(y,ry)
    if config.get('swap_axes'):u,v=v,u
    if config.get('invert_x'):u=1-u
    if config.get('invert_y'):v=1-v
    transform=config.get('touch_transform')
    if transform:
        a,b,c,d,e,f=transform
        tx,ty=round(a*x+b*y+c),round(d*x+e*y+f)
        tx,ty=max(0,min(WIDTH-1,tx)),max(0,min(HEIGHT-1,ty))
    else:
        tx,ty=round(u*(WIDTH-1)),round(v*(HEIGHT-1))
    if config.get('rotation',0)==180:tx,ty=WIDTH-1-tx,HEIGHT-1-ty
    return tx,ty

def run(config):
    from evdev import InputDevice, ecodes
    restore_calibration(config)
    restore_display(config)
    restore_fetch(config)
    idle=IdleDisplay(config['screen_timeout']);settings_error=None
    fetch_input=str(config['fetch_interval']);fetch_error=None
    model=Model(config['fetch_interval']);renderer=Renderer();fb=Framebuffer(config['framebuffer'],config['packer'],config.get('rotation',0))
    touch=InputDevice(config['touch']);rx=touch.absinfo(ecodes.ABS_X);ry=touch.absinfo(ecodes.ABS_Y)
    if rx is None or ry is None or rx.max<=rx.min or ry.max<=ry.min:raise ValueError('Invalid touchscreen range')
    thread=threading.Thread(target=model.poll,args=(config['url'],),daemon=True);thread.start()
    for sig in (signal.SIGTERM,signal.SIGINT):signal.signal(sig,lambda *_:model.stopped.set())
    page,selected='overview',0;node_view='status';settings_view='menu';settings_menu=SettingsMenu();timeout_select=TimeoutSelect();cal=None
    x,y=rx.min,ry.min;pressed=False;points=[];press_at=0;last_render=0;last_key=None
    try:
        while not model.stopped.is_set():
            ready,_,_=select.select([touch.fd],[],[],0.08 if cal or (pressed and page=='settings' and (settings_view=='menu' or timeout_select.opened)) else 0.2)
            if ready:
                for event in touch.read():
                    now=time.monotonic()
                    if event.type==ecodes.EV_ABS:
                        if event.code==ecodes.ABS_X:x=event.value
                        if event.code==ecodes.ABS_Y:y=event.value
                    elif event.type==ecodes.EV_KEY and event.code==ecodes.BTN_TOUCH:
                        if event.value==1:
                            pressed=True;points=[];press_at=now
                            settings_menu.cancel()
                            timeout_select.cancel()
                            if idle.touch_down(now):last_key=None;last_render=0
                        elif pressed or idle.pressed:
                            was_pressed=pressed
                            pressed=False
                            if idle.touch_up(now) or not was_pressed:points=[];continue
                            if not points:continue
                            raw=points[-1];tx,ty=touch_point(*raw,rx,ry,config)
                            duration=now-press_at
                            if cal:
                                if duration>=2 or (cal.phase=='collect' and inside((tx,ty),COLLECT_CANCEL_BUTTON)):cal.cancel()
                                elif cal.phase=='confirm':
                                    if inside((tx,ty),START_BUTTON):cal.start(now)
                                    elif inside((tx,ty),CONFIRM_CANCEL_BUTTON):cal.cancel()
                                elif cal.phase=='collect':cal.sample(points,duration,now)
                                elif cal.phase in ('review','failed'):
                                    if any(inside((tx,ty),box) for box in (APPLY_BUTTON,RETRY_BUTTON,CANCEL_BUTTON)):
                                        if inside((tx,ty),APPLY_BUTTON) and cal.phase=='review':
                                            try:
                                                cal.apply(config);config['calibration_error']=cal.error
                                            except (OSError,ValueError):
                                                cal.phase='failed';cal.message='Could not save; previous mapping retained.';cal.version+=1
                                        elif inside((tx,ty),RETRY_BUTTON):cal.start(now)
                                        elif inside((tx,ty),CANCEL_BUTTON):cal.cancel()
                                    elif cal.phase=='review':cal.preview(raw)
                                if cal.phase in ('cancelled','applied'):cal=None;page='settings';settings_view='calibration'
                            elif page=='settings' and settings_view=='menu' and settings_menu.start is not None:
                                target=settings_menu.end(touch_point(x,y,rx,ry,config))
                                if target is not None:
                                    settings_view=target
                                    if target=='fetch':fetch_input=str(config['fetch_interval']);fetch_error=None
                            elif page=='settings' and settings_view=='timeout' and timeout_select.opened:
                                if timeout_select.start is not None:
                                    seconds=timeout_select.end(touch_point(x,y,rx,ry,config))
                                    if seconds is not None:
                                        timeout_select.close()
                                        if seconds!=config['screen_timeout']:
                                            try:save_display(seconds)
                                            except (OSError,ValueError):settings_error='Could not save; previous timeout retained.'
                                            else:
                                                config['screen_timeout']=seconds;idle.configure(seconds,now);settings_error=None
                                else:
                                    timeout_select.close()
                                    if inside((tx,ty),SETTINGS_BACK_BUTTON):settings_view='menu'
                                    elif ty>=276:page=navigation_page(tx);settings_view='menu'
                            elif ty>=276:page=navigation_page(tx);settings_view='menu'
                            elif page=='settings':
                                if settings_view!='menu' and inside((tx,ty),SETTINGS_BACK_BUTTON):settings_view='menu'
                                elif settings_view=='calibration':
                                    if inside((tx,ty),CALIBRATE_BUTTON):cal=Calibration(config.get('rotation',0),now)
                                elif settings_view=='timeout':
                                    if inside((tx,ty),TIMEOUT_SELECT_BUTTON):timeout_select.open(config['screen_timeout'])
                                elif settings_view=='fetch':
                                    if inside((tx,ty),FETCH_APPLY_BUTTON):
                                        interval=parse_interval(fetch_input)
                                        if interval is None:fetch_error='Enter 1–60 seconds.'
                                        elif interval!=config['fetch_interval']:
                                            try:save_fetch(interval)
                                            except (OSError,ValueError):fetch_error='Save failed; unchanged.'
                                            else:
                                                config['fetch_interval']=interval;model.set_interval(interval)
                                                fetch_input=str(interval);fetch_error=None
                                    else:
                                        for label,box in FETCH_KEYS:
                                            if inside((tx,ty),box):
                                                if label=='Clear':fetch_input=''
                                                elif label=='<':fetch_input=fetch_input[:-1]
                                                elif len(fetch_input)<2:fetch_input+=label
                                                fetch_error=None;break
                            elif page=='overview' and OVERVIEW_CARD_TOP<=ty<=OVERVIEW_CARD_BOTTOM:selected=min(2,max(0,(tx-14)//155));page='node'
                            elif page=='node' and NODE_TAB_TOP<=ty<=NODE_TAB_BOTTOM:selected=min(2,max(0,(tx-14)//155))
                            elif page=='node' and inside((tx,ty),NODE_STATUS_BUTTON):node_view='status'
                            elif page=='node' and inside((tx,ty),NODE_SERVICES_BUTTON):node_view='services'
                            last_render=0
                    elif event.type==ecodes.EV_SYN and event.code==ecodes.SYN_REPORT and pressed:
                        idle.activity(now)
                        if not cal and page=='settings' and settings_view=='menu' and not idle.consume_touch:
                            point=touch_point(x,y,rx,ry,config)
                            if not points:settings_menu.begin(point)
                            elif settings_menu.move(point):last_render=0
                        elif not cal and page=='settings' and settings_view=='timeout' and timeout_select.opened and not idle.consume_touch:
                            point=touch_point(x,y,rx,ry,config)
                            if not points:timeout_select.begin(point)
                            elif timeout_select.move(point):last_render=0
                        if len(points)<256:points.append((x,y))
            now=time.monotonic()
            if cal and cal.tick(now):cal=None;page='settings';settings_view='calibration';last_render=0
            # Emergency escape does not depend on the old or new coordinate mapping.
            if cal and pressed and now-press_at>=2:
                cal=None;page='settings';settings_view='calibration';pressed=False;last_render=0
            if idle.tick(now,inhibit=cal is not None):
                fb.show(Image.new('RGB',(WIDTH,HEIGHT),'black'))
            if idle.sleeping:continue
            if now-last_render>=(0.08 if cal else 1):
                value,received,ok=model.snapshot()
                live,elapsed=freshness(value,received,now)
                online=tuple(node_live(n,live,elapsed) for n in value['nodes']) if value else ()
                network_fresh=tuple(network_live(n,live,elapsed) for n in value['nodes']) if value else ()
                key=('cal',id(cal),cal.version) if cal else ('settings',settings_view,settings_menu.offset,timeout_select.opened,timeout_select.offset,config.get('calibration_error'),config['screen_timeout'],settings_error,config['fetch_interval'],fetch_input,fetch_error) if page=='settings' else (id(value),live,ok,online,network_fresh,page,selected,node_view)
                if key!=last_key:
                    image=renderer.calibration(cal) if cal else renderer.settings(config,settings_error,settings_view,settings_menu,timeout_select,fetch_input,fetch_error) if page=='settings' else renderer.draw(value,received,ok,page,selected,now,node_view)
                    fb.show(image);last_key=key
                last_render=now
    finally:
        model.stop();touch.close();fb.close()

def load(path):
    config=json.loads(Path(path).read_text());url=urlparse(config['url'])
    if url.scheme!='http' or url.path!='/api/cluster/panel' or url.query or url.fragment or url.username or url.password or not ipaddress.ip_address(url.hostname).is_private:
        raise ValueError('Use the private cluster panel GET endpoint')
    if config.get('rotation',0) not in (0,180):raise ValueError('Supported panel rotation: 0 or 180')
    if not valid_timeout(config.get('screen_timeout',0)):raise ValueError('Invalid screen timeout')
    if not valid_interval(config.get('fetch_interval',DEFAULT_INTERVAL)):raise ValueError('Invalid fetch interval')
    transform=config.get('touch_transform')
    if transform is not None and (not isinstance(transform,list) or len(transform)!=6 or any(not finite(v) or abs(v)>1e6 for v in transform)):
        raise ValueError('Invalid touch affine transform')
    return config

if __name__=='__main__':
    parser=argparse.ArgumentParser();parser.add_argument('--config',default='/etc/desk-cluster-panel/config.json')
    args=parser.parse_args();run(load(args.config))
