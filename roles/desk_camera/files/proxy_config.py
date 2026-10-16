"""Share identical ready checks between camera HTTP and RTSP backends."""
import re

def tracked_health_checks(text):
 lines=text.splitlines();sections={};current=None
 for i,line in enumerate(lines):
  if line.startswith(('backend ','frontend ','global','defaults')):
   current=line.split()[1] if line.startswith('backend ') else None
   if current in sections:raise ValueError('Duplicate proxy backend')
   if current:sections[current]=[]
  if current:sections[current].append((i,line))
 mappings={'camera_rtsp_owner':'camera_owner'}
 for name in sections:
  match=re.fullmatch(r'camera_rtsp_owner_([0-9]+)',name)
  if match:mappings[name]='camera_'+match[1]
 changed={}
 for rtsp,http in mappings.items():
  if rtsp not in sections:continue
  if http not in sections:raise ValueError('Missing signaling backend')
  source={line.split()[1]:line.split() for _,line in sections[http] if line.strip().startswith('server ')}
  expected=[line.strip() for _,line in sections[http] if line.strip().startswith('http-check ')]
  observed=[line.strip() for _,line in sections[rtsp] if line.strip().startswith('http-check ')]
  if observed and observed!=expected:raise ValueError('Readiness policies differ')
  for i,line in sections[rtsp]:
   value=line.strip()
   if value=='option httpchk' or value.startswith('http-check '):changed[i]=None
   if not value.startswith('server '):continue
   parts=value.split();name=parts[1];target=source.get(name)
   if not target or 'check' not in target or target[2].rsplit(':',1)[0]!=parts[2].rsplit(':',1)[0]:raise ValueError('Unmatched relay readiness target')
   track=http+'/'+name
   if parts[3:]==['track',track]:continue
   if 'check' not in parts or parts[3:]!=target[3:]:raise ValueError('Relay readiness options differ')
   changed[i]='  server '+name+' '+parts[2]+' track '+track
 return '\n'.join(changed.get(i,line) for i,line in enumerate(lines) if changed.get(i,line) is not None)+'\n'
