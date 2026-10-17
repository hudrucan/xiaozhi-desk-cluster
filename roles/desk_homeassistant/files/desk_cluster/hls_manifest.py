"""Rewrite only our fixed HLS URIs; each browser request has its own HA signature."""
import re

def valid_name(name):
    return name in {'index.m3u8','init.mp4'} or bool(re.fullmatch(r's[0-9]{6}\.m4s',name))

def rewrite(text, sign):
    if len(text.encode())>131072 or not text.startswith('#EXTM3U\n'):
        raise ValueError('Invalid HLS manifest')
    result=['#EXTM3U','#EXT-X-START:TIME-OFFSET=0,PRECISE=YES']
    # A growing recording is DVR, not a live-edge stream. The native HA player
    # enables live catch-up; a hold-back beyond this bounded job keeps normal 1x
    # playback while EXT-X-START explicitly selects the beginning.
    if '#EXT-X-ENDLIST' not in text.splitlines():
        result.append('#EXT-X-SERVER-CONTROL:HOLD-BACK=1810.000')
    count=0
    for line in text.splitlines()[1:]:
        if line.startswith(('#EXT-X-START:','#EXT-X-SERVER-CONTROL:')):continue
        if line.startswith('#EXT-X-MAP:'):
            if line!='#EXT-X-MAP:URI="init.mp4"':raise ValueError('Invalid HLS initialization')
            result.append('#EXT-X-MAP:URI="'+sign('init.mp4')+'"')
        elif 'URI=' in line or line.startswith('#EXT-X-KEY'):
            raise ValueError('Unexpected HLS URI')
        elif line and not line.startswith('#'):
            if line!=f's{count:06d}.m4s':raise ValueError('Unexpected segment')
            result.append(sign(line));count+=1
        else:result.append(line)
    if not 0<count<=1024:raise ValueError('Incomplete HLS manifest')
    return '\n'.join(result)+'\n'
