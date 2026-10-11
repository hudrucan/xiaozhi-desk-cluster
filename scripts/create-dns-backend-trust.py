#!/usr/bin/env python3
"""Create ignored pinned internal DNS transport trust; never print private keys."""
import os
from pathlib import Path
import subprocess
import tempfile

root = Path('secret'); root.mkdir(mode=0o700, exist_ok=True)
crt, pem = root/'desk-dns-backend.crt', root/'desk-dns-backend.pem'
if crt.exists() and pem.exists():
    print('Existing DNS backend trust retained')
elif crt.exists() or pem.exists():
    raise SystemExit('Incomplete DNS trust; retain and repair before retrying')
else:
    with tempfile.TemporaryDirectory() as temporary:
        key, cert = Path(temporary)/'key', Path(temporary)/'cert'
        subprocess.run(['openssl','req','-x509','-newkey','rsa:2048','-nodes','-days','825',
                        '-subj','/CN=dns-backend.desk.cluster',
                        '-addext','subjectAltName=DNS:dns-backend.desk.cluster,IP:10.10.10.11,IP:10.10.10.12,IP:10.10.10.13',
                        '-addext','basicConstraints=critical,CA:TRUE',
                        '-addext','extendedKeyUsage=serverAuth','-keyout',str(key),'-out',str(cert)],
                       check=True,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)
        for path,value in [(crt,cert.read_bytes()),(pem,cert.read_bytes()+key.read_bytes())]:
            fd=os.open(path,os.O_WRONLY|os.O_CREAT|os.O_EXCL,0o600)
            with os.fdopen(fd,'wb') as target:target.write(value)
    print('Created ignored pinned DNS backend TLS trust')
