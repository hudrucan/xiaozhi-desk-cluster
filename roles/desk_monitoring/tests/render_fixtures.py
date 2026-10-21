"""Validate optional monitoring templates and preserved ingress composition."""
import ast
from pathlib import Path
import jinja2
import yaml

root = Path(__file__).resolve().parents[3]
role = root / 'roles/desk_monitoring'
for path in list(role.rglob('*.yml')) + [root / 'playbooks/monitoring/monitoring.yml',root / 'playbooks/monitoring/monitoring-sync.yml']:
    yaml.safe_load(path.read_text())
for path in role.rglob('*.py'):
    ast.parse(path.read_text(), filename=str(path))
env = jinja2.Environment(undefined=jinja2.StrictUndefined, trim_blocks=True, lstrip_blocks=True)
values = {'desk_monitoring_version': '0.21.0', 'desk_monitoring_vip': '192.168.1.186',
          'desk_monitoring_hub_port': 8090, 'desk_monitoring_agent_port': 45876,
          'desk_monitoring_proxy_port':8091,'desk_monitoring_config_port':8092,'desk_monitoring_peer_port':8094,
          'desk_monitoring_crypto_directory':'/opt/desk-monitoring-sync/46.0.3/vendor',
          'cluster_ip': '10.10.10.11', 'groups': {'desk_monitoring': ['deskb1x', 'deskb2x', 'deskb3x'],
                                               'xiaozhi_ingress': ['deskb1x', 'deskb2x', 'deskb3x']},
          'hostvars': {f'deskb{n}x': {'cluster_ip': f'10.10.10.1{n}'} for n in (1, 2, 3)},
          'xiaozhi_ingress_stats_socket': '/run/xiaozhi-haproxy.sock',
          'xiaozhi_ingress_vip': '192.168.1.186', 'xiaozhi_ingress_port': 80,
          'xiaozhi_ingress_backend_port': 8004}
for path in (role / 'templates').glob('*.j2'):
    rendered = env.from_string(path.read_text()).render(values)
    assert rendered.strip()
    if '.service.' in path.name:
        assert 'User=beszel-' in rendered and 'NoNewPrivileges=true' in rendered
        assert ('Slice=desk-monitorconfig.slice' if path.name in ('beszel-config-sync.service.j2','beszel-ui-proxy.service.j2') else 'Slice=desk-monitoring.slice') in rendered
ingress = root / 'roles/xiaozhi_ingress/templates/haproxy.cfg.j2'
for enabled in (False, True):
    for shared in (False, True):
        rendered = env.from_string(ingress.read_text()).render(values, xiaozhi_ingress_monitoring_enabled=enabled, desk_monitoring_sync_enabled=shared)
        assert ('backend desk_monitoring_hubs' in rendered) is enabled
        assert 'bind 192.168.1.186:80' in rendered
        assert 'path_beg /internal/' in rendered
        if enabled:
            assert f'10.10.10.11:{8091 if shared else 8090}' in rendered
            Path('/private/tmp/beszel-trial/ingress-candidate.cfg').write_text(rendered)
print('Monitoring YAML/Python/templates and ingress enabled/disabled fixtures PASS')
