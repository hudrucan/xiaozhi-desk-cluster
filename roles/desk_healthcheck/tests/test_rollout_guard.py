"""Ensure native probe substitutions do not alter elections or failover timing."""
import ast
import importlib.util
from pathlib import Path
import unittest
import jinja2
import yaml

ROOT = Path(__file__).resolve().parents[3]
spec = importlib.util.spec_from_file_location('rollout_guard', ROOT / 'roles/desk_healthcheck/files/rollout_guard.py')
guard = importlib.util.module_from_spec(spec)
spec.loader.exec_module(guard)

class RolloutTests(unittest.TestCase):
    def test_templates_change_only_probe_commands(self):
        values = {
            'inventory_hostname': 'deskb2x', 'ansible_host': '192.168.1.102',
            'groups': {'xiaozhi_ingress': ['deskb1x', 'deskb2x', 'deskb3x']},
            'hostvars': {f'deskb{i}x': {'ansible_host': f'192.168.1.10{i}'} for i in (1, 2, 3)},
            'xiaozhi_ingress_probe': '/usr/local/libexec/xiaozhi-ingress-probe',
            'xiaozhi_ingress_stats_socket': '/run/xiaozhi-haproxy.sock',
            'xiaozhi_ingress_management': {'interface': 'wlan0', 'prefix': 24},
            'xiaozhi_ingress_vrid': 51, 'xiaozhi_ingress_vip': '192.168.1.186',
            'xiaozhi_ingress_garp_refresh_seconds': 30, 'xiaozhi_ingress_garp_refresh_repeat': 2,
            'desk_dns_test_port': 5353, 'desk_dns_snapshot': {'topology': {'interface': 'wlan0', 'prefix': 24}},
            'desk_dns_vrid': 52, 'desk_dns_election': {'deskb2x': 110},
            'desk_dns_nodes': {f'deskb{i}x': {'management': f'192.168.1.10{i}'} for i in (1, 2, 3)},
            'desk_dns_vip': '192.168.1.250', 'desk_dns_ipv6_vip_enabled': True, 'desk_dns_ipv6_vip': 'fe80::250',
        }
        env = jinja2.Environment(undefined=jinja2.StrictUndefined, trim_blocks=True, lstrip_blocks=True)
        before, after = {'configs': {}}, {'configs': {}}
        for path, role in zip(guard.PATHS, ('xiaozhi_ingress', 'desk_dns')):
            template = env.from_string((ROOT / 'roles' / role / 'templates/keepalived.conf.j2').read_text())
            before['configs'][path] = template.render(values, desk_healthcheck_native_enabled=False)
            after['configs'][path] = template.render(values, desk_healthcheck_native_enabled=True)
            self.assertEqual(guard.normalize(before['configs'][path]), guard.normalize(after['configs'][path]))
        guard.candidate(before, after)
        after['configs'][guard.PATHS[1]] = after['configs'][guard.PATHS[1]].replace('priority 110', 'priority 120')
        with self.assertRaises(ValueError):
            guard.candidate(before, after)

    def test_yaml_and_python_sources_are_valid(self):
        for path in [ROOT / 'playbooks/cluster/healthchecks.yml', *ROOT.joinpath('roles/desk_healthcheck').rglob('*.yml')]:
            yaml.safe_load(path.read_text())
        ast.parse(ROOT.joinpath('roles/desk_healthcheck/files/rollout_guard.py').read_text())

if __name__ == '__main__':
    unittest.main()
