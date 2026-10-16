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
 total=sum(p.stat().st_size for p in files)
 for p in sorted(files,key=lambda p:p.stat().st_mtime):
  stat=p.stat()
  if now-stat.st_mtime>TTL or total>quota:
   p.unlink();total-=stat.st_size;removed+=1
 for p in ROOT.glob('playback/*/*'):
  if not re.fullmatch('tapo_c222_(160|161|163)',p.parent.name) or not re.fullmatch('[a-f0-9]{32}',p.name) or p.is_symlink() or p in active:continue
  if p.is_dir() and not (p/'ready.mp4').exists() and now-p.stat().st_mtime>TTL:shutil.rmtree(p)
 return {'bytes':total,'removed_files':removed}
