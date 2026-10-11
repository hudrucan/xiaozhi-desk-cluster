#!/usr/bin/env python3
"""Explicit on-node isolated fallback check; never changes production services.
Run with a validated rendered dnsdist candidate passed as --config.
"""
import argparse,importlib.util,socket,subprocess,tempfile,time,struct
from pathlib import Path
parser=argparse.ArgumentParser();parser.add_argument('--config',required=True);args=parser.parse_args()
spec=importlib.util.spec_from_file_location('dns_probe','/usr/local/libexec/desk-dns/dns-probe.py');p=importlib.util.module_from_spec(spec);spec.loader.exec_module(p)
original=Path(args.config).read_text()
for mode in ['normal','fallback','recovered','both_down']:
 with socket.socket(socket.AF_INET,socket.SOCK_DGRAM) as s:s.bind(('127.0.0.1',0));port=s.getsockname()[1]
 config='\n'.join(line for line in original.splitlines() if not line.startswith(('setLocal(', 'addLocal(')))+'\n'
 config=f'setLocal("127.0.0.1:{port}")\n'+config
 if mode in ('fallback','both_down'):
  config=config.replace('maxCheckFailures=2, rise=3})','maxCheckFailures=2, rise=3}).setDown()')
 elif mode=='recovered':
  config=config.replace('maxCheckFailures=2, rise=3})','maxCheckFailures=2, rise=3}).setUp()',1)
  start=config.index('maxCheckFailures=2, rise=3}).setUp()')+len('maxCheckFailures=2, rise=3}).setUp()')
  config=config[:start]+config[start:].replace('maxCheckFailures=2, rise=3})','maxCheckFailures=2, rise=3}).setDown()')
 if mode in ('fallback','recovered'):
  config=config.replace('maxCheckFailures=2, rise=2})','maxCheckFailures=2, rise=2}).setUp()')
 elif mode=='both_down':
  config=config.replace('maxCheckFailures=2, rise=2})','maxCheckFailures=2, rise=2}).setDown()')
 config=config.replace(').setDown()', '):setDown()').replace(').setUp()', '):setUp()')
 with tempfile.TemporaryDirectory(prefix='desk-dns-isolated-') as directory:
  path=Path(directory)/'dnsdist.conf';path.write_text(config)
  with (Path(directory)/'output.log').open('wb') as log:
   process=subprocess.Popen(['/usr/bin/dnsdist','--supervised','--disable-syslog','-C',str(path)],stdout=log,stderr=log)
   try:
    for _ in range(25):
     if process.poll() is not None:
      print((Path(directory)/'output.log').read_text()[-2000:],flush=True);raise RuntimeError('isolated dnsdist exited')
     try:
      if p.query('127.0.0.1',port,'frontend.desk-dns.test')==['127.0.0.1']:break
     except Exception:time.sleep(.2)
    else:raise RuntimeError('isolated frontend unavailable')
    for tcp in [False,True]:
     assert p.query('127.0.0.1',port,'frontend.desk-dns.test',tcp)==['127.0.0.1']
     if mode in ['normal','recovered']:
      for _ in range(10):
       try:
        assert p.query('127.0.0.1',port,p.BLOCKED,tcp)==['0.0.0.0'];break
       except Exception:time.sleep(.3)
      else:raise RuntimeError('native filtering unavailable')
      assert p.query('127.0.0.1',port,p.IDENTITY,tcp)[0].startswith('10.10.10.')
     elif mode=='fallback':
      assert set(p.query('127.0.0.1',port,'one.one.one.one',tcp))=={'1.0.0.1','1.1.1.1'}
     else:
      try:p.query('127.0.0.1',port,'one.one.one.one',tcp)
      except ValueError:pass
      else:raise AssertionError('expected SERVFAIL when both pools unavailable')
    print(mode+': UDP/TCP PASS',flush=True)
   finally:
    process.terminate()
    try:process.wait(timeout=3)
    except subprocess.TimeoutExpired:process.kill();process.wait()
