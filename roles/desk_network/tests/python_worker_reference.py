"""Test-only reference for the retired Python worker; never deployed."""
from contextlib import closing
import argparse
import json
import os
import pathlib
import sqlite3
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / 'files'))
from ha import (LIMIT, Unavailable, apply_policy, atomic, checkpoint, read_bounded, unpack)


def restore(blob,data,run,token):
 db,conf,workflows=unpack(blob);data=pathlib.Path(data);check=pathlib.Path(run)/'restore-check.db';atomic(check,db)
 try:
  with closing(sqlite3.connect(check)) as c:
   if c.execute('PRAGMA quick_check').fetchone()[0]!='ok':raise Unavailable('Database integrity check failed')
 finally:check.unlink(missing_ok=True)
 for suffix in ('-wal','-shm'):(data/('db/app.db'+suffix)).unlink(missing_ok=True)
 atomic(data/'db/app.db',db);atomic(data/'config/app.conf',apply_policy(conf,token))
 workflow_path=data/'config/workflows.json'
 if workflows is None:workflow_path.unlink(missing_ok=True)
 else:atomic(workflow_path,workflows);os.chown(workflow_path,20211,20211)
 for p in (data,data/'db',data/'config',data/'db/app.db',data/'config/app.conf'):
  os.chown(p,20211,20211);os.chmod(p,0o700 if p.is_dir() else 0o600)

def main():
 parser=argparse.ArgumentParser();parser.add_argument('--snapshot-worker',required=True,choices=('create','validate','restore'));parser.add_argument('--data');parser.add_argument('--run');args=parser.parse_args()
 if args.snapshot_worker:
  if args.snapshot_worker=='create':
   if not args.data or not args.run:parser.error('Checkpoint paths required')
   blob=checkpoint(args.data,args.run)
   if len(blob)>LIMIT:raise Unavailable('Checkpoint exceeds byte limit')
   sys.stdout.buffer.write(blob)
  elif args.snapshot_worker=='restore':
   if not args.data or not args.run:parser.error('Restore paths required')
   header=sys.stdin.buffer.readline(4097)
   if len(header)>4096 or not header.endswith(b'\n'):raise Unavailable('Restore envelope')
   value=json.loads(header)
   if set(value)!={'token'} or not isinstance(value['token'],str) or not 0<len(value['token'])<=2048:raise Unavailable('Restore credentials')
   restore(read_bounded(sys.stdin.buffer,LIMIT),args.data,args.run,value['token'])
  else:unpack(read_bounded(sys.stdin.buffer,LIMIT))
  return

if __name__=='__main__':main()
