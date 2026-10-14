import importlib.util,subprocess
from pathlib import Path
from unittest.mock import patch
p=Path(__file__).resolve().parents[1]/'files'/'firewall.py'
s=importlib.util.spec_from_file_location('fw',p);fw=importlib.util.module_from_spec(s);s.loader.exec_module(fw)
for host in ('192.168.1.101','192.168.1.102','192.168.1.103'):
 r=fw.rules(host)
 assert r['DESK_WG_PUBLIC']==[('-d','192.168.1.254/32','-j','ACCEPT'),('-j','DROP')]
 assert r['DESK_WG_FORWARD'][-1]==('-j','DROP')
 assert r['DESK_WG_INPUT'][-1]==('-j','DROP')
 assert r['DESK_WG_NAT'][0][-1]==host and '192.168.1.0/24' in r['DESK_WG_NAT'][0]
 assert r['DESK_WG_LOCAL'][0][-1]==host
 legacy=fw.rules(host,True)['DESK_WG_PUBLIC']
 assert ('-d','192.168.1.103/32','-j','ACCEPT') in legacy if host.endswith('.103') else len(legacy)==2
with patch.object(fw,'ip',return_value=1):text=fw.document('192.168.1.101')
assert '-F INPUT' not in text and '-F FORWARD' not in text and '-F POSTROUTING' not in text
assert '-F DESK_WG_PUBLIC' in text and '-I INPUT 1 -p udp --dport 51820' in text
with patch.object(fw,'ip',return_value=0):assert '-I ' not in fw.document('192.168.1.101')
with patch.object(fw.Path,'exists',return_value=True):
 try:fw.down()
 except RuntimeError:pass
 else:raise AssertionError('unprotected live tunnel')
print('PASS three-node public VIP gate, transitional endpoint only on node 3, full LAN/private deny, SNAT identity, idempotent chain transactions')

assert fw.rules('192.168.1.101')['DESK_WG_OUTPUT']==[('-j','DROP')]
assert fw.rules('192.168.1.101',owner=True)['DESK_WG_OUTPUT']==[('-j','ACCEPT')]
assert '--ctorigdst' in fw.rules('192.168.1.103',legacy=True)['DESK_WG_OUTPUT'][0]
print('PASS standby reply suppression and transitional legacy reply scoping')
