#!/usr/bin/env python3
"""Seed from the stopped default trial; snapshots include WAL via SQLite backup."""
import argparse,json,pathlib,base64
from ha import Store,checkpoint,apply_policy,atomic,sha
p=argparse.ArgumentParser();p.add_argument('--config',default='/etc/desk-network/config.json');p.add_argument('--data',default='/var/lib/desk-netalertx-trial/data');p.add_argument('--output',default='/var/lib/desk-network-seed');a=p.parse_args()
c=json.loads(pathlib.Path(a.config).read_text());root=pathlib.Path(a.output);root.mkdir(exist_ok=True);blob=checkpoint(a.data,'/run/desk-network-seed');from ha import unpack,encoded
import gzip
# The production policy is part of the initial checkpoint, not old trial settings.
db,conf,workflows=unpack(blob);payload={'database':base64.b64encode(db).decode(),'configuration':base64.b64encode(apply_policy(conf,c['api_token'])).decode()}
if workflows is not None:payload['workflows']=base64.b64encode(workflows).decode()
blob=gzip.compress(encoded(payload),compresslevel=1,mtime=0)
s=Store(root,base64.b64decode(c['key']),c['members']);meta={'revision':[0,c['node'],0],'digest':sha(blob),'at':0}
s.stage(meta,blob,c['node']);meta['certificate']=[s.receipt(n,meta) for n in c['members']];s.commit(meta)
print('Consistent initial network checkpoint created')
