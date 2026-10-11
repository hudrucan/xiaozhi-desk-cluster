import ast
import json
from pathlib import Path
import yaml
from jinja2 import Environment, StrictUndefined

root=Path(__file__).resolve().parents[3]
role=root/'roles/desk_dns'
defaults=yaml.safe_load((role/'defaults/main.yml').read_text())
yamls=[root/'dns.yml', root/'verify-dns.yml', *role.rglob('*.yml')]
for path in yamls:
    yaml.safe_load(path.read_text())
pyfiles=list(role.rglob('*.py'))
for path in pyfiles:
    ast.parse(path.read_text(), filename=str(path))
env=Environment(undefined=StrictUndefined, keep_trailing_newline=True)
env.filters['to_json']=json.dumps
nodes={f'deskb{i}x':{'private':f'10.10.10.{10+i}','management':f'192.168.1.{100+i}'} for i in range(1,4)}
for node, address in nodes.items():
    values={**defaults,'desk_dns_nodes':nodes,'inventory_hostname':node,
            'cluster_ip':address['private'],'ansible_host':address['management'],
            'desk_dns_snapshot':{'topology':{'interface':'wlan0','prefix':24}},
            'desk_dns_election':{'deskb1x':100,'deskb2x':120,'deskb3x':110},
            'desk_dns_admin_password_hash':'$2b$12$'+'A'*53}
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
        if path.name=='desk-dns-activity.service.j2':
            assert 'CapabilityBoundingSet=CAP_NET_RAW' in rendered
            assert 'CPUQuota=5%' in rendered and 'MemoryMax=32M' in rendered
            assert 'RestrictAddressFamilies=AF_UNIX AF_PACKET' in rendered
            assert 'Requires=' not in rendered and 'PartOf=' not in rendered
            assert f'--node {node}' in rendered
        if path.name.endswith('.service.j2'):
            assert 'Slice=desk-dns.slice' in rendered
            assert '[Service]' in rendered and 'ExecStart=' in rendered
# Production mutations must remain within explicit non-check-mode blocks.
for filename in ('backend.yml','frontend.yml','vip.yml'):
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

print(f'PASS: {len(yamls)} YAML files, {len(pyfiles)} Python ASTs, 27 rendered templates, check-mode/rollout isolation fixtures')
