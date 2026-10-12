import json, hashlib, re, subprocess, sys, shutil, os
from pathlib import Path
from urllib.request import urlopen
phase, sha = sys.argv[1:]
assert re.fullmatch('[0-9a-f]{40}', sha)
unit=Path('/etc/systemd/system/xiaozhi-mqtt-gateway.service')
record=Path('/var/lib/desk-maintenance-rollout')/sha/'gateway.json'
def cmd(args): return subprocess.check_output(args,text=True,stderr=subprocess.DEVNULL,timeout=90).strip()
def status():
    config=json.load(open('/etc/desk-dashboard/config.json'))
    ip=next(m['private'] for m in config['members'] if m['id']==config['node'])
    return json.load(urlopen('http://'+ip+':8007/status',timeout=4))
def digest(): return hashlib.sha256(Path('/etc/xiaozhi-mqtt-gateway.env').read_bytes()).hexdigest()
def write(text):
    tmp=unit.with_suffix('.maintenance.tmp'); tmp.write_text(text); tmp.chmod(0o644); os.replace(tmp,unit)
if phase=='stage':
    text=unit.read_text(); old=cmd(['systemctl','show','xiaozhi-mqtt-gateway','-p','WorkingDirectory','--value'])
    assert old.startswith('/opt/xiaozhi-mqtt-gateway/releases/')
    match=re.search(r'^ExecStart=(/opt/xiaozhi-mqtt-gateway/node-[^ ]+/bin/node) '+re.escape(old)+r'/app.js$',text,re.M); assert match
    current=status(); assert current['status']=='ready' and current['active_mqtt_connections']==0
    if cmd(['git','-C',old,'rev-parse','HEAD'])==sha and current.get('maintenance_supported') is True:
        print('{"staged":false,"already_current":true}');raise SystemExit(0)
    cmd(['git','-C',old,'diff','--quiet']); cmd(['git','-C',old,'diff','--cached','--quiet'])
    cmd(['git','-C',old,'fetch','--no-tags','origin',sha])
    for name in ('package.json','package-lock.json'):
        assert Path(old,name).read_text().strip()==cmd(['git','-C',old,'show',sha+':'+name]).strip(), 'Dependency drift'
    new='/opt/xiaozhi-mqtt-gateway/releases/'+sha
    if not Path(new).exists(): cmd(['git','-C',old,'worktree','add','--detach',new,sha])
    assert cmd(['git','-C',new,'rev-parse','HEAD'])==sha
    if not Path(new,'node_modules').exists(): shutil.copytree(Path(old,'node_modules'),Path(new,'node_modules'))
    for name in ('mqtt.json',):
        source=Path(old,'config',name); target=Path(new,'config',name)
        assert source.is_file(), 'Applied gateway configuration missing'
        shutil.copy2(source,target); info=source.stat();os.chown(target,info.st_uid,info.st_gid)
        assert hashlib.sha256(source.read_bytes()).digest()==hashlib.sha256(target.read_bytes()).digest()
    cmd([match[1],'--check',new+'/app.js'])
    for path in Path(new,'src').glob('*.js'): cmd([match[1],'--check',str(path)])
    record.parent.mkdir(parents=True,exist_ok=True,mode=0o700)
    record.write_text(json.dumps({'old':old,'new':new,'unit':text,'env':digest(),'mqtt':hashlib.sha256(Path(old,'config/mqtt.json').read_bytes()).hexdigest()})); record.chmod(0o600)
    print(json.dumps({'staged':True}))
elif phase=='activate':
    data=json.loads(record.read_text()); assert digest()==data['env']
    if unit.read_text()==data['unit'].replace(data['old'],data['new']):
        assert status().get('maintenance_supported') is True
        print('{"activated":false,"already_current":true}');raise SystemExit(0)
    assert unit.read_text()==data['unit']
    if 'mqtt' in data:
        for path in (data['old'],data['new']):
            assert hashlib.sha256(Path(path,'config/mqtt.json').read_bytes()).hexdigest()==data['mqtt'], 'Gateway configuration drift'
    assert status()['active_mqtt_connections']==0
    write(data['unit'].replace(data['old'],data['new']))
    cmd(['systemctl','daemon-reload']); cmd(['systemctl','restart','xiaozhi-mqtt-gateway']); print('{"activated":true}')
elif phase=='ready':
    data=json.loads(record.read_text()); value=status()
    assert digest()==data['env'] and value['status']=='ready' and value['maintenance_supported'] is True
    print('{"ready":true}')
elif phase=='rollback':
    data=json.loads(record.read_text()); write(data['unit']); cmd(['systemctl','daemon-reload']);cmd(['systemctl','restart','xiaozhi-mqtt-gateway']); print('{"rolled_back":true}')
else: raise ValueError('Invalid phase')
