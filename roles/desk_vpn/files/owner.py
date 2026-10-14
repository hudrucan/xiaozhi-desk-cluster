#!/usr/bin/python3
"""Owner-only WG source route. Existing default/private routes stay untouched."""
import json,subprocess,sys
import firewall
TABLE='51853';PRIORITY='11853'
def command(*args,check=True):
 return subprocess.run(args,check=check,timeout=5,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)
v=firewall.config()
mode=sys.argv[1] if len(sys.argv)==2 else ''
if mode not in ('master','backup'):raise SystemExit(2)
links=json.loads(subprocess.check_output(['ip','-j','-4','addr','show','dev','wlan0']))
owned=any(a['local']==v['vip'] for d in links for a in d.get('addr_info',[]))
active=mode=='master' and owned
# Keep only the role-owned marked lookup, never replace main routes.
while command('ip','-4','rule','del','priority',PRIORITY,'fwmark',TABLE,'lookup',TABLE,check=False).returncode==0:pass
command('ip','-4','route','flush','table',TABLE,check=False)
if active:
 command('ip','-4','route','add','table',TABLE,'192.168.1.0/24','dev','wlan0','src',v['vip'])
 command('ip','-4','route','add','table',TABLE,'default','via','192.168.1.1','dev','wlan0','src',v['vip'])
 command('ip','-4','rule','add','priority',PRIORITY,'fwmark',TABLE,'lookup',TABLE)
firewall.up(v['management'],v.get('legacy') is True,active)
