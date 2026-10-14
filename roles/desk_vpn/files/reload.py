#!/usr/bin/python3
"""Apply shared peer settings without replacing live keys/sessions."""
import subprocess
subprocess.run(['/usr/bin/python3','/usr/local/libexec/desk-vpn/firewall.py','up'],check=True,timeout=15)
value=subprocess.check_output(['/usr/bin/wg-quick','strip','/etc/desk-vpn/wgdesk0.conf'],timeout=5)
subprocess.run(['/usr/bin/wg','syncconf','wgdesk0','/dev/stdin'],input=value,check=True,timeout=5)

import json
links=json.loads(subprocess.check_output(['ip','-j','-4','addr','show','dev','wlan0']))
owned=any(a['local']=='192.168.1.254' for d in links for a in d.get('addr_info',[]))
subprocess.run(['/usr/bin/python3','/usr/local/libexec/desk-vpn/owner.py','master' if owned else 'backup'],check=True,timeout=15)
