#!/usr/bin/env python3
"""Rename the existing local admin without replacing its ID/password/history."""
import argparse
import json
import os
from pathlib import Path
import re
import sqlite3
import subprocess
import tempfile
import time
import urllib.request

DATABASE=Path('/var/lib/beszel-hub/data.db')
UNIT=Path('/etc/systemd/system/beszel-hub.service')

def admin(db, target):
    rows=db.execute("SELECT id,email FROM users WHERE role='admin'").fetchall()
    if len(rows)!=1:raise ValueError('Exactly one existing admin required')
    identity,email=rows[0]
    if db.execute('SELECT count(*) FROM users WHERE lower(email)=lower(?) AND id<>?',(target,identity)).fetchone()[0]:
        raise ValueError('Target account already belongs to another user')
    return identity,email

def rename(db,target):
    identity,email=admin(db,target)
    if email==target:return False
    with db:
        cursor=db.execute("UPDATE users SET email=? WHERE id=? AND email=? AND role='admin'",(target,identity,email))
        if cursor.rowcount!=1:raise ValueError('Account changed during migration')
    return True

def atomic_unit(data):
    fd,name=tempfile.mkstemp(prefix='.beszel-email-',dir=UNIT.parent)
    try:
        os.fchmod(fd,0o644)
        with os.fdopen(fd,'wb') as stream:stream.write(data);stream.flush();os.fsync(stream.fileno())
        os.replace(name,UNIT)
    finally:
        if Path(name).exists():Path(name).unlink()

def service(*args):
    subprocess.run(['systemctl',*args],check=True,capture_output=True,timeout=20)

def configure(target):
    if not re.fullmatch(r'[A-Za-z0-9._+-]+@[A-Za-z0-9.-]+',target):raise ValueError('Invalid email')
    if not DATABASE.is_file() or not UNIT.is_file():return False
    with sqlite3.connect('file:'+str(DATABASE)+'?mode=ro',uri=True) as db:identity,old_email=admin(db,target)
    original=UNIT.read_bytes()
    text=original.decode()
    lines=re.findall(r'^Environment="AUTO_LOGIN=[^"]*"$',text,re.M)
    if len(lines)>1:raise ValueError('Ambiguous auto-login unit')
    wanted=text.replace(lines[0],'Environment="AUTO_LOGIN='+target+'"') if lines else text.replace('[Service]\n','[Service]\nEnvironment="AUTO_LOGIN='+target+'"\n',1)
    host=re.search(r'--http=(10\.10\.10\.\d+):8090',text)
    if not host:raise ValueError('Unexpected private hub endpoint')
    if old_email==target and original==wanted.encode():return False
    service('stop','beszel-hub')
    try:
        directory=DATABASE.parent/'account-migration-backups'
        directory.mkdir(mode=0o700,exist_ok=True);directory.chmod(0o700)
        fd,backup=tempfile.mkstemp(prefix='account-',suffix='.sqlite',dir=directory)
        os.fchmod(fd,0o600);os.close(fd)
        with sqlite3.connect(DATABASE) as db:
            # Take a consistent backup after the hub has flushed/stopped.
            with sqlite3.connect(backup) as copy:db.backup(copy)
            if admin(db,target)!=(identity,old_email):raise ValueError('Account drift')
            rename(db,target)
        atomic_unit(wanted.encode());service('daemon-reload');service('start','beszel-hub')
        opener=urllib.request.build_opener(urllib.request.ProxyHandler({}))
        for attempt in range(10):
            try:
                request=urllib.request.Request('http://'+host[1]+':8090/api/collections/users/auth-refresh',data=b'{}',headers={'Content-Type':'application/json'})
                with opener.open(request,timeout=3) as response:record=json.load(response)['record']
                if (record['id'],record['email'],record['role'])!=(identity,target,'admin'):raise ValueError('Unexpected account after migration')
                return True
            except Exception:
                if attempt==9:raise
                time.sleep(.5)
    except Exception:
        service('stop','beszel-hub')
        with sqlite3.connect(DATABASE) as db:
            db.execute('UPDATE users SET email=? WHERE id=?',(old_email,identity));db.commit()
        atomic_unit(original);service('daemon-reload');service('start','beszel-hub')
        raise

if __name__=='__main__':
    parser=argparse.ArgumentParser();parser.add_argument('--email',required=True);parser.add_argument('--check-existing',action='store_true');args=parser.parse_args()
    try:
        if args.check_existing:
            if not DATABASE.is_file() or not UNIT.is_file():raise ValueError('Existing hub required')
            with sqlite3.connect('file:'+str(DATABASE)+'?mode=ro',uri=True) as db:admin(db,args.email)
            print('ready')
        else:print('changed' if configure(args.email) else 'unchanged')
    except Exception:raise SystemExit('Beszel email migration failed; existing account retained; no credentials printed')
