#!/usr/bin/env python3
"""Reproducible native wg-easy ARM64 artifact. No Docker or kernel lifecycle patch."""
import argparse,base64,hashlib,io,json,os,shutil,subprocess,tarfile,urllib.request
from pathlib import Path
PIN='2dc8ba779216929c10c1998341d36963fe0eca7a';NODE='v24.19.0'
def download(url):
 with urllib.request.urlopen(url,timeout=60) as r:return r.read()
def npm(name,version):
 meta=json.loads(download('https://registry.npmjs.org/'+name+'/'+version));data=download(meta['dist']['tarball'])
 assert meta['dist']['integrity']=='sha512-'+base64.b64encode(hashlib.sha512(data).digest()).decode()
 return data

def package(source,out):
 app=out/'app';shutil.rmtree(app,ignore_errors=True);shutil.copytree(source/'src/.output',app,symlinks=True)
 for name,version,dest in [('@libsql/linux-arm64-gnu','0.5.29',app/'server/node_modules/@libsql/linux-arm64-gnu'),('argon2','0.44.0',app/'server/node_modules/argon2')]:
  with tarfile.open(fileobj=io.BytesIO(npm(name,version)),mode='r:gz') as t:
   for member in t.getmembers():
    if not member.name.startswith('package/'):continue
    member.name=member.name[8:]
    if member.name:t.extract(member,dest,filter='data')
 shutil.copytree(source/'src/server/database/migrations',app/'server/database/migrations',dirs_exist_ok=True)
 shutil.copy2(source/'LICENSE',app/'LICENSE')
 # Ship the precise modified upstream source alongside the AGPL binary artifact.
 source_tar=app/'wg-easy-source.tar.gz'
 with tarfile.open(source_tar,'w:gz') as t:
  for file in source.rglob('*'):
   if any(p in ('.git','node_modules','.nuxt','.output') for p in file.relative_to(source).parts):continue
   if file.is_file():t.add(file,arcname='wg-easy/'+str(file.relative_to(source)))
 with tarfile.open(out/'app.tar.gz','w:gz') as t:t.add(app,arcname='app')
 with tarfile.open(out/'assets.tar.gz','w:gz') as t:t.add(app/'public/_nuxt',arcname='_nuxt')
 filename='node-'+NODE+'-linux-arm64.tar.xz';url='https://nodejs.org/dist/'+NODE+'/'
 expected=next(line.split()[0] for line in download(url+'SHASUMS256.txt').decode().splitlines() if line.split()[-1]==filename)
 data=download(url+filename);assert hashlib.sha256(data).hexdigest()==expected;(out/filename).write_bytes(data)
 (out/'checksums.json').write_text(json.dumps({filename:expected,'app.tar.gz':hashlib.sha256((out/'app.tar.gz').read_bytes()).hexdigest(),'assets.tar.gz':hashlib.sha256((out/'assets.tar.gz').read_bytes()).hexdigest()}))

def main():
 p=argparse.ArgumentParser();p.add_argument('--source',type=Path,default=Path('/private/tmp/desk-wg-easy-build'));p.add_argument('--out',type=Path,default=Path('/private/tmp/desk-vpn-ui-package'));p.add_argument('--node',default='node');p.add_argument('--pnpm',default='pnpm');p.add_argument('--package-only',action='store_true');a=p.parse_args();repo=Path(__file__).resolve().parents[1]
 if not a.package_only:
  if a.source.exists():raise SystemExit('Use an empty source directory to avoid patching unrelated work')
  subprocess.run(['git','clone','--depth','1','--branch','v15.3.0','https://github.com/wg-easy/wg-easy.git',str(a.source)],check=True)
  assert subprocess.check_output(['git','rev-parse','HEAD'],cwd=a.source,text=True).strip()==PIN
  subprocess.run(['git','apply',str(repo/'roles/desk_vpn_ui/files/upstream.patch')],cwd=a.source,check=True)
  env=dict(os.environ);nodepath=shutil.which(a.node) or a.node;env['PATH']=str(Path(nodepath).resolve().parent)+os.pathsep+env['PATH']
  subprocess.run([a.pnpm,'install','--frozen-lockfile'],cwd=a.source/'src',env=env,check=True)
  subprocess.run([a.pnpm,'build'],cwd=a.source/'src',env=env,check=True)
 assert subprocess.check_output(['git','rev-parse','HEAD'],cwd=a.source,text=True).strip()==PIN
 a.out.mkdir(parents=True,exist_ok=True);package(a.source,a.out);print('Verified native ARM64 UI package prepared')
if __name__=='__main__':main()
