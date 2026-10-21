import ast
import json
import re
from pathlib import Path
import yaml
from jinja2 import Environment, StrictUndefined, FileSystemLoader

root=Path(__file__).resolve().parents[3]
role=root/'roles/desk_dns'
defaults=yaml.safe_load((role/'defaults/main.yml').read_text())
yamls=[root/'playbooks/dns/dns.yml', root/'playbooks/dns/verify-dns.yml', root/'playbooks/dns/dns-sync.yml', root/'playbooks/dns/dns-cluster.yml', root/'playbooks/dns/dns-ipv6.yml', root/'playbooks/dns/dns-resources.yml', root/'playbooks/dns/dns-resilience.yml', *role.rglob('*.yml')]
for path in yamls:
    yaml.safe_load(path.read_text())
pyfiles=list(role.rglob('*.py'))
for path in pyfiles:
    ast.parse(path.read_text(), filename=str(path))
env=Environment(loader=FileSystemLoader(role / "templates"), undefined=StrictUndefined, keep_trailing_newline=True)
env.filters['to_json']=json.dumps
nodes={f'deskb{i}x':{'private':f'10.10.10.{10+i}','management':f'192.168.1.{100+i}'} for i in range(1,4)}
for node, address in nodes.items():
    values={**defaults,'desk_dns_nodes':nodes,'inventory_hostname':node,
            'cluster_ip':address['private'],'ansible_host':address['management'],
            'desk_dns_snapshot':{'topology':{'interface':'wlan0','prefix':24}},
            'desk_dns_election':{'deskb1x':100,'deskb2x':120,'deskb3x':110},
            'desk_dns_admin_password_hash':'$2b$12$'+'A'*53,
            'desk_dns_sync_password': 'offline-fixture-password-only'}
    for path in role.joinpath('templates').glob('*.j2'):
        rendered=env.from_string(path.read_text()).render(**values)
        assert '{{' not in rendered and '{%' not in rendered, path
        if path.name=='adguard-home.yaml.j2':
            agh=yaml.safe_load(rendered)
            assert agh['dns']['bind_hosts']==[address['private']]
            assert agh['http']['address']=='127.0.0.1:3001'
            assert not agh['dhcp']['enabled']
            assert agh['querylog']['file_enabled'] is False
            assert len(agh['dns']['allowed_clients'])==4
            assert agh['schema_version']==34
            assert all(item['enabled'] is True for item in agh['filtering']['rewrites'])
        if path.name=='keepalived.conf.j2':
            assert 'virtual_router_id 52' in rendered
            assert 'weight -50' in rendered and 'nopreempt' not in rendered
            assert rendered.count('interval 5') == 2 and rendered.count('timeout 4') == 2
            assert '192.168.1.186' not in rendered
            assert rendered.count('192.168.1.250')==1
        if path.name=='dnsdist.conf.j2':
            assert rendered.count('weight=1')==3
            assert rendered.count('newServer(')==3
            assert 'roundrobin' in rendered
            assert '192.168.1.186' not in rendered
            assert 'webserver(' not in rendered
        if path.name=='ui-haproxy.cfg.j2':
            assert 'bind 192.168.1.250:3001' in rendered
            assert 'server local 127.0.0.1:3001' in rendered
            assert '192.168.1.186' not in rendered
        if path.name=='desk-dns-vip.service.j2':
            assert 'RestrictAddressFamilies=AF_UNIX AF_INET AF_NETLINK AF_PACKET' in rendered
        if path.name=='backend-haproxy.cfg.j2':
            assert 'mode 660 level user' in rendered
            assert 'backend adguard_clients' in rendered
        if path.name=='desk-dns-backend.service.j2':
            assert 'RuntimeDirectoryMode=0710' in rendered
        if path.name=='desk-dns-activity.service.j2':
            assert 'SupplementaryGroups=desk-dnsdist' in rendered
            assert 'CapabilityBoundingSet=CAP_NET_RAW' in rendered
            assert 'CPUQuota=5%' in rendered and 'MemoryMax=32M' in rendered
            assert 'RestrictAddressFamilies=AF_UNIX AF_PACKET' in rendered
            assert 'Requires=' not in rendered and 'PartOf=' not in rendered
            assert f'--node {node}' in rendered
        if path.name=='adguard-home-sync.yaml.j2':
            sync=yaml.safe_load(rendered)
            assert sync['origin']['url']=='https://192.168.1.250:3002'
            assert sync['replica']['url']=='http://127.0.0.1:3002'
            assert sync['api']['port']==0
            assert sync['features']['dhcp']=={'serverConfig': False, 'staticLeases': False}
            assert sync['features']['dns']['accessLists'] is False
            assert not sync['features']['tlsConfig'] and sync['features']['statsConfig'] and sync['features']['queryLogConfig']
            assert sync['origin']['insecureSkipVerify'] is False
        if path.name=='desk-dns.slice.j2':
            assert 'CPUQuota=100%' in rendered and 'CPUWeight=10' in rendered
            assert 'MemoryHigh=512M' in rendered and 'MemoryMax=768M' in rendered
        if path.name=='desk-dnssync.slice.j2':
            assert 'CPUQuota=20%' in rendered and 'MemoryMax=128M' in rendered
        if path.name=='desk-adguard.service.j2':
            assert f'ExecStart=/opt/desk-adguard/{defaults["desk_dns_adguard_version"]}/adguard-home ' in rendered
            assert 'AdGuardHome/AdGuardHome' not in rendered
            assert 'Environment=GOMEMLIMIT=320MiB' in rendered
            assert 'MemoryHigh=384M' in rendered and 'MemoryMax=512M' in rendered
        if path.name.endswith('.service.j2'):
            for command in rendered.splitlines():
                if command.startswith('ExecStart='):
                    assert all('_' not in name for name in re.findall(r'/usr/local/libexec/desk-dns/([^ ]+)', command))

            assert ('Slice=desk-dnssync.slice' if path.name.startswith(('desk-dns-sync', 'desk-dns-filter-refresh')) else 'Slice=desk-dnsfront.slice' if path.name in ('desk-dnsdist.service.j2', 'desk-dns-vip.service.j2') else 'Slice=desk-dns.slice') in rendered
            assert '[Service]' in rendered and 'ExecStart=' in rendered
# IPv6 uses the existing election and preserves a separate scoped DNS endpoint.
for node, address in nodes.items():
    values={**defaults, 'desk_dns_nodes':nodes, 'inventory_hostname':node,
            'cluster_ip':address['private'], 'ansible_host':address['management'],
            'desk_dns_snapshot':{'topology':{'interface':'wlan0','prefix':24}},
            'desk_dns_election':{'deskb1x':100,'deskb2x':120,'deskb3x':110},
            'desk_dns_ipv6_vip_enabled':True, 'desk_dns_client_identity_enabled':True}
    rendered={name:env.from_string((role/'templates'/name).read_text()).render(**values)
              for name in ('keepalived.conf.j2','dnsdist.conf.j2','desk-dns-vip.service.j2')}
    assert 'virtual_ipaddress_excluded' in rendered['keepalived.conf.j2']
    assert 'fe80::250/128 dev wlan0' in rendered['keepalived.conf.j2']
    assert rendered['keepalived.conf.j2'].count('virtual_router_id') == 1
    assert 'addLocal("[::]:53")' in rendered['dnsdist.conf.j2']
    assert '"fe80::/10"' in rendered['dnsdist.conf.j2']
    assert 'addXForwardedHeaders=true' in rendered['dnsdist.conf.j2']
    assert 'RestrictAddressFamilies=AF_UNIX AF_INET AF_INET6 AF_NETLINK AF_PACKET' in rendered['desk-dns-vip.service.j2']
    assert 'CAP_NET_ADMIN' in rendered['desk-dns-vip.service.j2']
# Production mutations must remain within explicit non-check-mode blocks.
for filename in ('backend.yml','frontend.yml','vip.yml','ipv6-access.yml','ipv6-counter.yml','ipv6-frontend.yml','ipv6-vip.yml'):
    tasks=yaml.safe_load((role/'tasks'/filename).read_text())
    blocks=[t for t in tasks if 'block' in t]
    assert len(blocks)==1 and blocks[0]['when']=='not ansible_check_mode'
frontend=yaml.safe_load((role/'tasks/frontend.yml').read_text())[-1]['block']
vip=yaml.safe_load((role/'tasks/vip.yml').read_text())[-1]['block']
assert frontend[0]['ansible.builtin.include_tasks']=='check_backends.yml'
assert vip[0]['ansible.builtin.include_tasks']=='check_backends.yml'
assert 'Recheck every frontend' in vip[1]['name']
assert all('notify' not in task for task in vip)
assert 'desk_dns' not in (root/'site.yml').read_text()

backend=yaml.safe_load((role/'tasks/backend.yml').read_text())[-1]['block']
assert backend[-3]['name']=='Require the local AdGuard identity, filtering and UDP/TCP answers'
assert backend[-1]['ansible.builtin.systemd_service']['name']=='desk-dns-activity'
assert 'desk_dns_activity_script.changed or desk_dns_activity_unit.changed' in backend[-1]['ansible.builtin.systemd_service']['state']
assert all('desk-dns-activity' not in (role/'templates'/name).read_text() for name in ('desk-adguard.service.j2','desk-dnsdist.service.j2','desk-dns-vip.service.j2'))

print(f'PASS: {len(yamls)} YAML files, {len(pyfiles)} Python ASTs, {3 * len(list(role.joinpath('templates').glob('*.j2'))) + 9} rendered templates, check-mode/rollout isolation fixtures')

for node, address in nodes.items():
    values={**defaults, 'desk_dns_nodes':nodes,'inventory_hostname':node,
            'cluster_ip':address['private'],'ansible_host':address['management'],
            'desk_dns_ipv6_vip_enabled':True,'desk_dns_emergency_enabled':True,
            'desk_dns_client_identity_enabled':True}
    config=env.from_string((role/'templates/dnsdist.conf.j2').read_text()).render(**values)
    assert 'addAction(PoolAvailableRule(""), PoolAction(""))' in config
    assert config.index('PoolAvailableRule') < config.index('AllRule(), PoolAction("emergency")')
    assert 'address="1.1.1.1:53"' in config and 'rise=3' in config
    assert 'frontend.desk-dns.test.' in config and '[::]:53' in config
print('PASS: emergency pool and native-first fallback rules rendered for all 3 nodes')
