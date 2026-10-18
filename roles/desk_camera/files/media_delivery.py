"""Ephemeral viewer capabilities. No SDK calls, credentials or new owner lease.

Issuance is root IPC/signed RPC only. Browser read/touch/close stays on the exact
serving node while Core restarts. Controller restart deliberately revokes grants.
"""
import hashlib
import json
import re
import secrets
import time
import os
from urllib.parse import urlsplit

from playback import clip_path


def token(value, length=32):
    return isinstance(value,str) and re.fullmatch('[a-f0-9]{'+str(length)+'}',value) is not None


class Delivery:
    def __init__(self, agents, *, mono=time.monotonic, wall=time.time, capacity=128):
        self.agents={a.c['workload']:a for a in agents}
        self.node=agents[0].node;self.mono=mono;self.wall=wall;self.capacity=capacity
        self.grants={};self.requests={}

    def base(self, cap):return '/desk-camera-media/v1/'+self.node+'/'+cap+'/'

    def prune(self):
        now=self.mono()
        for cap,g in list(self.grants.items()):
            if now>=g['expires'] or (g.get('closed_at') is not None and now-g['closed_at']>30):
                self.grants.pop(cap);self.requests.pop((g['sender'],g['viewer']),None)

    def issue(self, sender, value):
        if sender not in ('deskb1x','deskb2x','deskb3x') or set(value)!={'workload','id','attempt','viewer','deadline'}:raise ValueError('Viewer issuance')
        if not token(value['viewer']) or not token(value['id']) or (value['attempt'] is not None and not token(value['attempt'])):raise ValueError('Viewer binding')
        deadline=value['deadline']
        if type(deadline) not in (float,int) or not self.wall()<deadline<=self.wall()+30:raise ValueError('Viewer deadline')
        self.prune();key=(sender,value['viewer']);fingerprint=hashlib.sha256(json.dumps(value,sort_keys=True,separators=(',',':'),allow_nan=False).encode()).hexdigest()
        old=self.requests.get(key)
        if old:
            g=self.grants[old]
            if g['fingerprint']!=fingerprint or not self.live(g):raise ValueError('Viewer already used')
            return self.reply(old,g)
        if len(self.grants)>=self.capacity:raise ValueError('Viewer capacity')
        a=self.agents[value['workload']];p=a.playback;status=p.status(value['id'])
        from recording_owner import owner_mode
        if not owner_mode(value['workload']) or status.get('state') not in ('ready','playable'):raise ValueError('Recording unavailable')
        running=p.active() and p.current==value['id']
        if running and (value['attempt']!=p.attempt or p.stop_requested or p.viewer_until is None or self.mono()>=p.viewer_until):raise ValueError('Playback attempt')
        if running and (not a.owned() or not a.ready()):raise ValueError('Owner changed')
        generation=status.get('generation') if status.get('format')=='hls' else None
        if generation is not None and not token(generation):raise ValueError('Media generation')
        cap=secrets.token_hex(32);now=self.mono()
        g=dict(value,sender=sender,fingerprint=fingerprint,expires=now+7200,until=now+20,
               owner_generation=a.generation if running else None,media_generation=generation,
               format='hls' if generation else 'mp4',closed_at=None)
        if not generation:
            st=clip_path(value['workload'],value['id']).stat();g['file_stamp']=(st.st_ino,st.st_size,st.st_mtime_ns)
        self.grants[cap]=g;self.requests[key]=cap
        self.maintain(a)
        return self.reply(cap,g)

    def reply(self,cap,g):
        base=self.base(cap)
        return {'url':base+('index.m3u8' if g['format']=='hls' else 'clip.mp4'),
                'mime_type':'application/x-mpegURL' if g['format']=='hls' else 'video/mp4',
                'touch_url':base+'touch','close_url':base+'close','viewer':g['viewer'],
                'heartbeat_seconds':5,'expires_in_seconds':max(0,int(g['expires']-self.mono()))}

    def live(self,g):return g['closed_at'] is None and self.mono()<min(g['until'],g['expires'])

    def binding(self,g):
        a=self.agents[g['workload']];p=a.playback;status=p.status(g['id'])
        if status.get('state') not in ('ready','playable'):raise ValueError('Recording unavailable')
        if g['format']=='hls':
            if status.get('format')!='hls' or status.get('generation')!=g['media_generation']:raise ValueError('Media generation changed')
        else:
            st=clip_path(g['workload'],g['id']).stat()
            if (st.st_ino,st.st_size,st.st_mtime_ns)!=g['file_stamp']:raise ValueError('Cached file changed')
        if p.active() and p.current==g['id']:
            if p.attempt!=g['attempt'] or a.generation!=g['owner_generation'] or not a.owned() or p.stop_requested:raise ValueError('Owner or attempt changed')
        return a,status

    def lookup(self,cap):
        if not token(cap,64) or cap not in self.grants:raise ValueError('Unknown viewer')
        return self.grants[cap]

    def access(self,cap,name):
        g=self.lookup(cap)
        if not self.live(g):raise ValueError('Viewer expired')
        self.binding(g)
        if g['format']=='hls':
            from hls_files import artifact_path
            path=artifact_path(clip_path(g['workload'],g['id']).parent,g['media_generation'],name)
        elif name=='clip.mp4':path=clip_path(g['workload'],g['id'])
        else:raise ValueError('Invalid media file')
        return path,g['format'],g['workload'],g['id']

    def action(self,cap,action):
        g=self.lookup(cap);a=self.agents[g['workload']];p=a.playback
        if action=='touch':
            if not self.live(g):raise ValueError('Viewer expired')
            self.binding(g);g['until']=min(self.mono()+20,g['expires']);self.maintain(a)
            return {'active':True}
        if action!='close':raise ValueError('Invalid viewer action')
        if g['closed_at'] is None:g['closed_at']=self.mono()
        self.maintain(a)
        others=any(self.live(v) and v['workload']==g['workload'] and v['id']==g['id'] and v['attempt']==g['attempt'] for v in self.grants.values())
        draining=not others and p.active() and (p.current,p.attempt)==(g['id'],g['attempt'])
        return {'closed':not draining,'state':'cancelling' if draining else 'closed'}

    def maintain(self,a):
        p=a.playback;matching=[g for g in self.grants.values() if (g['workload'],g['id'],g['attempt'])==(a.c['workload'],p.current,p.attempt)]
        if matching and p.active() and p.viewer_until is not None:
            live=[g for g in matching if self.live(g) and g['owner_generation']==a.generation]
            if live:
                if not p.stop_requested:p.viewer_until=max(g['until'] for g in live)
            else:p.stop_requested=True
        self.prune()

    def core_action(self,a,ident,attempt,close=False):
        # A restarted Core can rediscover the same preparation. Its temporary
        # waiter must not cancel detached viewers or renew their deadlines.
        matching=[g for g in self.grants.values() if (g['workload'],g['id'],g['attempt'])==(a.c['workload'],ident,attempt)]
        if not matching:return None
        p=a.playback
        if (p.current,p.attempt)!=(ident,attempt) or not p.active():return {'state':'cancelled' if close else 'missing'}
        self.maintain(a)
        if close:
            return {'state':'cancelling' if p.stop_requested else 'cancelled'}
        return {'state':'missing' if p.stop_requested else 'active'}

    def active_paths(self):
        return {clip_path(g['workload'],g['id']).parent for g in self.grants.values() if self.live(g)}


def manifest(text,base):
    """Keep the DVR start semantics of the owned Core proxy without HA signing."""
    from hls_files import playlist
    playlist(text)
    lines=['#EXTM3U','#EXT-X-START:TIME-OFFSET=0,PRECISE=YES']
    if '#EXT-X-ENDLIST' not in text.splitlines():lines.append('#EXT-X-SERVER-CONTROL:HOLD-BACK=1810.000')
    for line in text.splitlines()[1:]:
        if line.startswith(('#EXT-X-START:','#EXT-X-SERVER-CONTROL:')):continue
        if line=='#EXT-X-MAP:URI="init.mp4"':line='#EXT-X-MAP:URI="'+base+'init.mp4"'
        elif line and not line.startswith('#'):line=base+line
        lines.append(line)
    return '\n'.join(lines)+'\n'


def serve(handler,post=False):
    """HTTP threads only serve bounded artifacts; grant mutations use the loop."""
    stream=None;acquired=False;headers=False
    try:
        server=handler.server
        if handler.client_address[0] not in set(server.agents[0].members.values())|{'127.0.0.1'}:raise ValueError('Private ingress only')
        acquired=server.delivery_slots.acquire(blocking=False)
        if not acquired:raise ValueError('Delivery capacity')
        path=urlsplit(handler.path)
        if path.query or path.fragment or len(handler.path)>256:raise ValueError('Media path')
        parts=path.path.split('/')
        if len(parts)!=6 or parts[1:3]!=['desk-camera-media','v1'] or parts[3]!=server.agents[0].node:raise ValueError('Media route')
        cap,name=parts[4:]
        def invoke(fn,*args):
            import concurrent.futures
            result=concurrent.futures.Future()
            def apply():
                if not result.set_running_or_notify_cancel():return
                try:result.set_result(fn(*args))
                except BaseException as error:result.set_exception(error)
            server.loop.call_soon_threadsafe(apply)
            try:return result.result(timeout=2)
            except TimeoutError:result.cancel();raise
        handler.connection.settimeout(10)
        if post:
            if handler.headers.get('Transfer-Encoding') or handler.headers.get('Content-Length','0')!='0':raise ValueError('Empty viewer action only')
            if name not in ('touch','close'):raise ValueError('Viewer action')
            origin=handler.headers.get('Origin')
            if origin is not None and urlsplit(origin).netloc!=handler.headers.get('Host'):raise ValueError('Cross-origin viewer action')
            value=invoke(server.delivery.action,cap,name);body=json.dumps(value,separators=(',',':')).encode()
            handler.send_response(200);handler.send_header('Content-Type','application/json');handler.send_header('Content-Length',str(len(body)))
            handler.send_header('Cache-Control','private, no-store');handler.send_header('Referrer-Policy','no-referrer');handler.send_header('Connection','close');handler.end_headers();headers=True;handler.wfile.write(body);return
        p,format,workload,ident=invoke(server.delivery.access,cap,name)
        limit=131072 if name=='index.m3u8' else 16*1024*1024 if format=='hls' else 256*1024*1024
        if not p.is_file() or p.is_symlink():raise ValueError('Media file')
        fd=os.open(p,os.O_RDONLY|os.O_NOFOLLOW);stream=os.fdopen(fd,'rb');size=os.fstat(fd).st_size
        if not 0<size<=limit:raise ValueError('Media size')
        (clip_path(workload,ident).parent/'.access').touch()
        if name=='index.m3u8':
            body=manifest(stream.read(limit+1).decode(),server.delivery.base(cap)).encode()
            handler.send_response(200);handler.send_header('Content-Type','application/x-mpegURL');handler.send_header('Content-Length',str(len(body)))
            handler.send_header('Cache-Control','private, no-store');handler.send_header('Referrer-Policy','no-referrer');handler.send_header('X-Content-Type-Options','nosniff');handler.send_header('Connection','close');handler.end_headers();headers=True;handler.wfile.write(body);return
        start,end,code=0,size-1,200
        requested=handler.headers.get('Range')
        if requested:
            match=re.fullmatch(r'bytes=(\d*)-(\d*)',requested)
            if not match or not any(match.groups()):raise ValueError('Media range')
            left,right=match.groups()
            if left:start=int(left);end=min(int(right),end) if right else end
            else:start=max(0,size-int(right))
            if not 0<=start<=end<size:raise ValueError('Media range')
            code=206
        handler.send_response(code);handler.send_header('Content-Type','video/mp4');handler.send_header('Content-Length',str(end-start+1));handler.send_header('Accept-Ranges','bytes')
        if code==206:handler.send_header('Content-Range',f'bytes {start}-{end}/{size}')
        handler.send_header('Cache-Control','private, no-store');handler.send_header('Referrer-Policy','no-referrer');handler.send_header('X-Content-Type-Options','nosniff');handler.send_header('Connection','close');handler.end_headers();headers=True
        stream.seek(start);remaining=end-start+1
        while remaining:
            chunk=stream.read(min(65536,remaining))
            if not chunk:break
            handler.wfile.write(chunk);remaining-=len(chunk)
    except (BrokenPipeError,ConnectionResetError,TimeoutError):pass
    except Exception:
        if not headers:handler.send_error(404,'Recording unavailable')
    finally:
        if stream is not None:stream.close()
        if acquired:handler.server.delivery_slots.release()
