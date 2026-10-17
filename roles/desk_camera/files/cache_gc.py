"""Disposable recording cache: one-hour idle TTL, one GiB per node, no SD writes."""
import pathlib,time,re,shutil
ROOT=pathlib.Path('/var/lib/desk-camera');TTL=3600;QUOTA=1024*1024*1024

def cleanup(active=(),now=None,quota=QUOTA):
 now=time.time() if now is None else now;active={pathlib.Path(p) for p in active};files=[];removed=0
 def accept(path):
  return not path.is_symlink() and path.is_file() and not any(parent.is_symlink() for parent in path.parents if parent.is_relative_to(ROOT)) and not any(p==path or p in path.parents for p in active)
 for p in ROOT.glob('playback/*/*/ready.mp4'):
  if re.fullmatch('tapo_c222_(160|161|163)',p.parent.parent.name) and re.fullmatch('[a-f0-9]{32}',p.parent.name) and accept(p):files.append(p)
 for temperature in ('hot','cold'):
  for folder in ('videos','thumbs'):
   for p in (ROOT/'cache'/temperature).glob('*/'+folder+'/*'):
    if p.name.endswith(('.mp4','.jpg','.mp4.ts','.mp4.alaw','.mp4.mulaw')) and accept(p):files.append(p)
 groups=[]
 from hls_files import valid_name
 for folder in ROOT.glob('playback/*/*/hls/*'):
  job=folder.parent.parent
  if not re.fullmatch('tapo_c222_(160|161|163)',job.parent.name) or not re.fullmatch('[a-f0-9]{32}',job.name) or not re.fullmatch('[a-f0-9]{32}',folder.name):continue
  if folder.is_symlink() or not folder.is_dir() or any(x in folder.parents or x==folder for x in active) or any(x.is_symlink() for x in folder.parents if x.is_relative_to(ROOT)):continue
  children=list(folder.iterdir())
  if not children or any(not x.is_file() or x.is_symlink() or not (valid_name(x.name) or (x.name.endswith('.tmp') and valid_name(x.name[:-4]))) for x in children):continue
  stamp=(job/'.access').stat().st_mtime if (job/'.access').is_file() and not (job/'.access').is_symlink() else max(x.stat().st_mtime for x in children)
  groups.append((folder,sum(x.stat().st_size for x in children),stamp))
 total=sum(p.stat().st_size for p in files)+sum(size for _,size,_ in groups)
 for p,size,stamp in sorted([(p,p.stat().st_size,p.stat().st_mtime) for p in files]+groups,key=lambda item:item[2]):
  if now-stamp>TTL or total>quota:
   if p.is_dir():shutil.rmtree(p)
   else:p.unlink()
   total-=size;removed+=1
 for p in ROOT.glob('playback/*/*'):
  if not re.fullmatch('tapo_c222_(160|161|163)',p.parent.name) or not re.fullmatch('[a-f0-9]{32}',p.name) or p.is_symlink() or p in active:continue
  if p.is_dir() and not (p/'ready.mp4').exists() and not any((p/'hls').glob('*/index.m3u8')) and now-p.stat().st_mtime>TTL:shutil.rmtree(p)
 return {'bytes':total,'removed_files':removed}
