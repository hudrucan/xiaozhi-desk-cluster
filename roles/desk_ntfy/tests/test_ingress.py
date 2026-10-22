"""Exercise the shared proxy composer and local-first failover configuration."""
import importlib.util
from pathlib import Path
import unittest
import json
import tomllib
import jinja2
import yaml

ROOT = Path(__file__).resolve().parents[3]
spec = importlib.util.spec_from_file_location('ntfy_proxy', ROOT / 'roles/desk_monitoring/files/install_proxy.py')
proxy = importlib.util.module_from_spec(spec)
spec.loader.exec_module(proxy)


class Ingress(unittest.TestCase):
    def test_each_node_prefers_itself_and_fences_native_with_adapter_health(self):
        env = jinja2.Environment(trim_blocks=True, lstrip_blocks=True, undefined=jinja2.StrictUndefined)
        template = env.from_string((ROOT / 'roles/desk_ntfy/templates/ingress.cfg.j2').read_text())
        nodes = ['deskb1x', 'deskb2x', 'deskb3x']
        for local in nodes:
            config = template.render(inventory_hostname=local, cluster_ip='10.10.10.1' + str(nodes.index(local)+1),
                desk_ntfy_vip='192.168.1.186', groups={'deskboxes': nodes},
                hostvars={n: {'cluster_ip': '10.10.10.1'+str(i+1)} for i,n in enumerate(nodes)})
            replicas = config.split('\nbackend desk_ntfy_replicas\n')[1].split('frontend desk_ntfy_node')[0]
            for node in nodes:
                line = next(line for line in replicas.splitlines() if line.startswith('    server '+node+' '))
                self.assertEqual(' backup' in line, node != local)
            self.assertIn('server local 127.0.0.1:2587 check port 2590', config)
            self.assertIn('option httpchk GET /ready', replicas)
            self.assertIn('disable-l7-retry unless { method GET HEAD }', replicas)
            self.assertIn('http-request deny if { path /v1/account } { method POST }', config)
            self.assertIn('http-request deny if { path_beg /v1/account/access } !{ method GET }', config)
            self.assertIn('bind 192.168.1.186:2586', config)
            (Path('/tmp') / ('desk-ntfy-'+local+'.cfg')).write_text('global\n    maxconn 64\ndefaults\n    mode http\n    timeout connect 2s\n    timeout server 90s\n    timeout client 90s\n'+config+'\n')

    def test_other_ui_reruns_retain_ntfy_and_ntfy_rerun_is_idempotent(self):
        current = 'frontend xiaozhi_settings\n    default_backend xiaozhi_control_planes\nbackend xiaozhi_control_planes\n    server core 10.10.10.11:8004\n'
        fragment = '# BEGIN DESK NTFY\nfrontend desk_ntfy_vip\n    bind 192.168.1.186:2586\n# END DESK NTFY\n'
        first = proxy.candidate(current, 'backend desk_monitoring_hubs\n', ntfy_config=fragment)
        self.assertEqual(first.count(fragment), 1)
        self.assertEqual(proxy.candidate(first, 'backend desk_monitoring_hubs\n'), first)
        self.assertEqual(proxy.candidate(first, 'backend desk_monitoring_hubs\n', ntfy_config=fragment), first)

    def test_deployment_remains_native_and_opt_in(self):
        for file in (ROOT / 'roles/desk_ntfy').rglob('*.yml'):
            yaml.safe_load(file.read_text())
        yaml.safe_load((ROOT / 'playbooks/notifications/ntfy.yml').read_text())
        unit = (ROOT / 'roles/desk_ntfy/templates/desk-ntfy.service.j2').read_text()
        self.assertIn('ExecStart=/opt/desk-ntfy/desk-ntfy serve', unit)
        self.assertNotIn('python', unit.lower())
        self.assertNotIn('ntfy', (ROOT / 'site.yml').read_text())

    def test_static_toml_keeps_ios_identity_and_local_only_databases(self):
        env=jinja2.Environment(undefined=jinja2.StrictUndefined)
        env.filters['to_json']=json.dumps
        template=env.from_string((ROOT/'roles/desk_ntfy/templates/server.toml.j2').read_text())
        for _ in range(3):
            config=tomllib.loads(template.render(desk_ntfy_base_url='http://192.168.1.186:2586'))
            self.assertEqual(config['base-url'],'http://192.168.1.186:2586')
            self.assertEqual(config['upstream-base-url'],'https://ntfy.sh')
            self.assertEqual(config['listen-http'],'127.0.0.1:2587')
            self.assertNotEqual(config['auth-file'],config['cache-file'])


if __name__ == '__main__':
    unittest.main()
