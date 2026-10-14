#!/usr/bin/python3
"""Own only dedicated chains and tunnel-scoped jumps; never flush a table."""
import subprocess,sys,json
from pathlib import Path
IPTABLES='/usr/sbin/iptables';RESTORE='/usr/sbin/iptables-restore'
IFACE='wgdesk0';CLIENT='10.77.0.0/24';LAN='192.168.1.0/24'
def config():
    v=json.loads(Path('/etc/desk-vpn/public.json').read_text())
    if v.get('protocol')!='desk-vpn-ha-v1' or v.get('management') not in ('192.168.1.101','192.168.1.102','192.168.1.103') or v.get('vip')!='192.168.1.254':raise ValueError('Invalid fixed VPN metadata')
    return v
CHAINS={'filter':('DESK_WG_INPUT','DESK_WG_FORWARD','DESK_WG_PUBLIC','DESK_WG_OUTPUT'),'nat':('DESK_WG_NAT','DESK_WG_LOCAL')}
def ip(table,*args,check=True):
    return subprocess.run([IPTABLES,'-w','5','-t',table,*args],check=check,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL).returncode

def rules(host,legacy=False,owner=False):
    return {'DESK_WG_OUTPUT':([('-j','ACCEPT')] if owner else ([('-m','conntrack','--ctstate','ESTABLISHED','--ctdir','REPLY','--ctorigdst','192.168.1.103','-j','ACCEPT')] if legacy and host=='192.168.1.103' else [])+[('-j','DROP')]), 'DESK_WG_PUBLIC':[('-d','192.168.1.254/32','-j','ACCEPT')]+([('-d','192.168.1.103/32','-j','ACCEPT')] if legacy and host=='192.168.1.103' else [])+[('-j','DROP')], 'DESK_WG_INPUT':[('-s',CLIENT,'-d',LAN,'-j','ACCEPT'),
        ('-s',CLIENT,'-d','10.77.0.1/32','-p','icmp','--icmp-type','echo-request','-j','ACCEPT'),('-j','DROP')],
      'DESK_WG_FORWARD':[('-i',IFACE,'-s',CLIENT,'-o','wlan0','-d',LAN,'-j','ACCEPT'),
        ('-o',IFACE,'-d',CLIENT,'-m','conntrack','--ctstate','ESTABLISHED,RELATED','-j','ACCEPT'),('-j','DROP')],
      'DESK_WG_NAT':[('-s',CLIENT,'-o','wlan0','-d',LAN,'-j','SNAT','--to-source',host)],
      'DESK_WG_LOCAL':[('-s',CLIENT,'-d',LAN,'-j','SNAT','--to-source',host)]}

def jumps():
    return [('filter','OUTPUT',('-p','udp','--sport','51820','-m','mark','--mark','51853','-j','DESK_WG_OUTPUT')),('filter','INPUT',('-p','udp','--dport','51820','-j','DESK_WG_PUBLIC')),('filter','INPUT',('-i',IFACE,'-j','DESK_WG_INPUT')),
      ('filter','FORWARD',('-i',IFACE,'-j','DESK_WG_FORWARD')),
      ('filter','FORWARD',('-o',IFACE,'-j','DESK_WG_FORWARD')),
      ('nat','POSTROUTING',('-s',CLIENT,'-o','wlan0','-j','DESK_WG_NAT')),
      ('nat','INPUT',('-i',IFACE,'-s',CLIENT,'-d',LAN,'-j','DESK_WG_LOCAL'))]

def document(host,legacy=False,owner=False):
    plan=rules(host,legacy,owner);lines=[]
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

def up(host,legacy=False,owner=False):
    if host not in ('192.168.1.101','192.168.1.102','192.168.1.103'):raise ValueError('Only cluster members are supported')
    if Path('/proc/sys/net/ipv4/ip_forward').read_text().strip()!='1':raise ValueError('IPv4 forwarding must already be enabled')
    text=document(host,legacy,owner)
    for test in (True,False):
        subprocess.run([RESTORE,'--noflush','--wait','5']+(['--test'] if test else []),input=text,text=True,check=True,stdout=subprocess.DEVNULL,stderr=subprocess.PIPE)

if __name__=='__main__':
    if sys.argv[1:] == ['up']:
        v=config();links=json.loads(subprocess.check_output(['ip','-j','-4','addr','show','dev','wlan0']));owner=any(a['local']==v['vip'] for d in links for a in d.get('addr_info',[]));up(v['management'],v.get('legacy') is True,owner)
    elif sys.argv[1:] == ['down']:down()
    else:raise SystemExit('Use up or down')
