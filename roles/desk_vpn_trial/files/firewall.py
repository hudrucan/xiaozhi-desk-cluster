#!/usr/bin/python3
"""Own only dedicated chains and tunnel-scoped jumps; never flush a table."""
import subprocess,sys
from pathlib import Path
IPTABLES='/usr/sbin/iptables';RESTORE='/usr/sbin/iptables-restore'
IFACE='wgdesk0';CLIENT='10.77.0.2/32';LAN='192.168.1.0/24'
CHAINS={'filter':('DESK_WG_INPUT','DESK_WG_FORWARD'),'nat':('DESK_WG_NAT','DESK_WG_LOCAL')}
def ip(table,*args,check=True):
    return subprocess.run([IPTABLES,'-w','5','-t',table,*args],check=check,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL).returncode

def rules(host):
    return {'DESK_WG_INPUT':[('-s',CLIENT,'-d',LAN,'-j','ACCEPT'),
        ('-s',CLIENT,'-d','10.77.0.1/32','-p','icmp','--icmp-type','echo-request','-j','ACCEPT'),('-j','DROP')],
      'DESK_WG_FORWARD':[('-i',IFACE,'-s',CLIENT,'-o','wlan0','-d',LAN,'-j','ACCEPT'),
        ('-o',IFACE,'-d',CLIENT,'-m','conntrack','--ctstate','ESTABLISHED,RELATED','-j','ACCEPT'),('-j','DROP')],
      'DESK_WG_NAT':[('-s',CLIENT,'-o','wlan0','-d',LAN,'-j','SNAT','--to-source',host)],
      'DESK_WG_LOCAL':[('-s',CLIENT,'-d',LAN,'-j','SNAT','--to-source',host)]}

def jumps():
    return [('filter','INPUT',('-i',IFACE,'-j','DESK_WG_INPUT')),
      ('filter','FORWARD',('-i',IFACE,'-j','DESK_WG_FORWARD')),
      ('filter','FORWARD',('-o',IFACE,'-j','DESK_WG_FORWARD')),
      ('nat','POSTROUTING',('-s',CLIENT,'-o','wlan0','-j','DESK_WG_NAT')),
      ('nat','INPUT',('-i',IFACE,'-s',CLIENT,'-d',LAN,'-j','DESK_WG_LOCAL'))]

def document(host):
    plan=rules(host);lines=[]
    for table,names in CHAINS.items():
        lines.append('*'+table)
        for chain in names:
            lines.append(':'+chain+' - [0:0]')
            lines.append('-F '+chain)  # Flush only our named chains in the transaction.
            lines.extend('-A '+chain+' '+' '.join(rule) for rule in plan[chain])
        for target,chain,rule in jumps():
            if target==table and ip(table,'-C',chain,*rule,check=False)!=0:
                lines.append('-I '+chain+' 1 '+' '.join(rule))
        lines.append('COMMIT')
    return '\n'.join(lines)+'\n'

def down():
    if Path('/sys/class/net/'+IFACE).exists():
        raise RuntimeError('Remove tunnel before removing its firewall protection')
    for table,chain,rule in jumps():
        while ip(table,'-C',chain,*rule,check=False)==0:ip(table,'-D',chain,*rule)
    for table,names in CHAINS.items():
        for chain in names:
            if ip(table,'-S',chain,check=False)==0:
                ip(table,'-F',chain);ip(table,'-X',chain)

def up(host):
    if host!='192.168.1.103':raise ValueError('Only the selected trial node is supported')
    if Path('/proc/sys/net/ipv4/ip_forward').read_text().strip()!='1':raise ValueError('IPv4 forwarding must already be enabled')
    text=document(host)
    for test in (True,False):
        subprocess.run([RESTORE,'--noflush','--wait','5']+(['--test'] if test else []),input=text,text=True,check=True,stdout=subprocess.DEVNULL,stderr=subprocess.PIPE)

if __name__=='__main__':
    if sys.argv[1:] == ['up']:up('192.168.1.103')
    elif sys.argv[1:] == ['down']:down()
    else:raise SystemExit('Use up or down')
