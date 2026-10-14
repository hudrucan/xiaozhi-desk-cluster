import importlib.util,subprocess
from unittest.mock import patch
from pathlib import Path
p=Path(__file__).resolve().parents[1]/'files'/'firewall.py'
spec=importlib.util.spec_from_file_location('fw',p);fw=importlib.util.module_from_spec(spec);spec.loader.exec_module(fw)
r=fw.rules('192.168.1.103')
assert r['DESK_WG_FORWARD'][-1]==('-j','DROP') and r['DESK_WG_INPUT'][-1]==('-j','DROP')
allows=[v for v in r['DESK_WG_FORWARD'] if '-i' in v]
assert len(allows)==1 and allows[0]==('-i','wgdesk0','-s','10.77.0.2/32','-o','wlan0','-d','192.168.1.0/24','-j','ACCEPT')
assert r['DESK_WG_INPUT'][0]==('-s','10.77.0.2/32','-d','192.168.1.0/24','-j','ACCEPT')
assert all('192.168.1.0/24' in rule for rule in r['DESK_WG_NAT']+r['DESK_WG_LOCAL'])
assert all('-s' in rule or '-i' in rule or '-o' in rule for _,_,rule in fw.jumps())
with patch.object(fw,'ip',return_value=1):text=fw.document('192.168.1.103')
assert '-F INPUT' not in text and '-F FORWARD' not in text and '-F POSTROUTING' not in text
assert '-F DESK_WG_FORWARD' in text and '-I INPUT 1 -i wgdesk0' in text
with patch.object(fw,'ip',return_value=0):existing=fw.document('192.168.1.103')
assert '-I ' not in existing
calls=[]
def fail(command,**kw):
 calls.append(command);raise subprocess.CalledProcessError(1,command)
with patch.object(fw.Path,'read_text',return_value='1'),patch.object(fw,'ip',return_value=0),patch.object(fw.subprocess,'run',side_effect=fail):
 try:fw.up('192.168.1.103')
 except subprocess.CalledProcessError:pass
 else:raise AssertionError('expected rejected candidate')
assert len(calls)==1 and '--test' in calls[0] and '--noflush' in calls[0]
with patch.object(fw.Path,'exists',return_value=True):
 try:fw.down()
 except RuntimeError:pass
 else:raise AssertionError('live tunnel protection removed')
try:fw.up('192.168.1.102')
except ValueError:pass
else:raise AssertionError('wrong node accepted')
print('PASS full LAN peer scope, private/WAN default deny, local-host access, idempotent jumps, pre-apply validation and no table flush/live unprotected tunnel')
