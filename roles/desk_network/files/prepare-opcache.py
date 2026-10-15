#!/usr/bin/env python3
"""Extract one checksum-pinned official Alpine module, never arbitrary archive paths."""
import argparse,hashlib,io,json,os,pathlib,tarfile
PACKAGE_SHA256='e6f46dec63fdab796e48cd0b3f10b868306dc415c0fc47706771e4ed69d47e38'
MODULE_SHA256='ddb9a4d4bf278b975d4327f28b37f6777cb48ac4595f166064b2441a18a3bc22'
MODULE_PATH='usr/lib/php83/modules/opcache.so'
def module_bytes(raw):
 if len(raw)>2*1024*1024 or hashlib.sha256(raw).hexdigest()!=PACKAGE_SHA256:
  raise ValueError('Pinned OPcache package checksum mismatch')
 with tarfile.open(fileobj=io.BytesIO(raw),mode='r:gz',ignore_zeros=True) as archive:
  info=archive.getmember('.PKGINFO');module=archive.getmember(MODULE_PATH)
  if not info.isfile() or info.size>4096 or not module.isfile() or module.size>2*1024*1024:
   raise ValueError('Invalid OPcache archive members')
  metadata=archive.extractfile(info).read().decode()
  if not all(line in metadata.splitlines() for line in ('pkgname = php83-opcache','pkgver = 8.3.29-r0','arch = aarch64')):
   raise ValueError('OPcache version or architecture mismatch')
  value=archive.extractfile(module).read()
 if hashlib.sha256(value).hexdigest()!=MODULE_SHA256:
  raise ValueError('Pinned OPcache module checksum mismatch')
 return value
def install(package,target):
 if package.stat().st_size>2*1024*1024:raise ValueError('OPcache package too large')
 value=module_bytes(package.read_bytes())
 if target.is_file() and target.read_bytes()==value:return False
 tmp=target.with_suffix('.tmp')
 try:
  tmp.write_bytes(value);tmp.chmod(0o644);os.replace(tmp,target)
 finally:tmp.unlink(missing_ok=True)
 return True
if __name__=='__main__':
 p=argparse.ArgumentParser();p.add_argument('package',type=pathlib.Path);p.add_argument('target',type=pathlib.Path);a=p.parse_args()
 print(json.dumps({'changed':install(a.package,a.target)}))
