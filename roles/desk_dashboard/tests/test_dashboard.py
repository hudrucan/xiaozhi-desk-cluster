"""Dashboard templates and route isolation; no hardware or external providers."""
import ast
import importlib.util
from pathlib import Path
import unittest
import jinja2
import yaml
from ansible.module_utils.parsing.convert_bool import boolean

ROOT = Path(__file__).resolve().parents[3]

def module(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    value = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(value)
    return value

proxy = module('dashboard_proxy', ROOT / 'roles/desk_monitoring/files/install_proxy.py')
guard = module('dashboard_guard', ROOT / 'roles/desk_ui/files/ui_guard.py')

class DashboardTests(unittest.TestCase):
    def test_dashboard_route_survives_monitoring_and_dns_reruns(self):
        current = 'frontend xiaozhi_settings\n    http-request deny deny_status 404 if { path_beg /internal/ }\n    default_backend xiaozhi_control_planes\nbackend xiaozhi_control_planes\n    server deskb1x 10.10.10.11:8004 check\n'
        monitoring = 'backend desk_monitoring_hubs\n    server deskb1x 10.10.10.11:8090 check\n'
        dns = 'backend desk_adguard_ui\n    server dns_vip 192.168.1.250:3001 check\n'
        dashboard = 'backend desk_dashboard\n    server deskb1x 10.10.10.11:8010 check\n'
        wanted = proxy.candidate(current, monitoring, dns, dashboard)
        self.assertEqual(guard.base(wanted), current)
        self.assertEqual(proxy.candidate(wanted, monitoring), wanted)
        self.assertEqual(proxy.candidate(wanted, monitoring, dns), wanted)
        self.assertEqual(proxy.candidate(wanted, monitoring, dns, dashboard), wanted)
        self.assertIn('use_backend desk_dashboard if { path / /api/cluster/status /api/cluster/panel }', wanted)
        self.assertNotIn('path_beg /api/cluster/status', wanted.split('use_backend desk_dashboard')[1])
        self.assertNotIn('path_beg /api/cluster/panel', wanted.split('use_backend desk_dashboard')[1])
        self.assertNotIn('/local', wanted)
        self.assertLess(wanted.index('path_beg /internal/'), wanted.index('use_backend desk_dashboard'))
        self.assertLess(wanted.rfind('    http-request '), wanted.find('    use_backend '))

    def test_control_route_is_exact_and_survives_other_ui_reruns(self):
        current='frontend xiaozhi_settings\n    http-request deny deny_status 404 if { path_beg /internal/ }\n    default_backend xiaozhi_control_planes\n'
        dashboard='backend desk_dashboard\n    server deskb1x 10.10.10.11:8010 check\nbackend desk_node_control\n    server deskb1x 10.10.10.11:8011 check\n'
        wanted=proxy.candidate(current,'backend desk_monitoring_hubs\n','backend desk_adguard_ui\n',dashboard)
        self.assertIn('use_backend desk_node_control if { path /api/cluster/operations /api/cluster/operations/prepare /api/cluster/operations/reboot /api/cluster/operations/resume }',wanted)
        self.assertNotIn('path_beg /peer/',wanted)
        self.assertEqual(proxy.candidate(wanted,'backend desk_monitoring_hubs\n'),wanted)
        self.assertEqual(guard.base(wanted),current)

    def test_metadata_and_page_have_no_mock_values_or_secret_input(self):
        env = jinja2.Environment(undefined=jinja2.StrictUndefined)
        import json
        env.filters['to_json'] = json.dumps
        env.filters['bool'] = boolean
        template = env.from_string(ROOT.joinpath('roles/desk_ui/templates/index.html.j2').read_text())
        members = [{'id':f'deskb{i}x', 'private':f'10.10.10.{i+10}', 'management':f'192.168.1.{i+100}'} for i in (1,2,3)]
        page = template.render(desk_dashboard_enabled=True, dashboard_members=members)
        self.assertIn('Cluster timeline', page)
        self.assertIn('Ownership & connections', page)
        self.assertIn("fetch('/api/cluster/status'", page)
        self.assertIn('document.hidden', page)
        for obsolete in ('Mock data', 'RTF 0.86', 'Segment 2 / 4', 'Tweak', 'window.openai'):
            self.assertNotIn(obsolete, page)
        self.assertNotIn('/api/cluster/status', template.render(desk_dashboard_enabled=False))

    def test_yaml_and_python_sources_parse(self):
        for path in ROOT.joinpath('roles/desk_dashboard').rglob('*.yml'):
            yaml.safe_load(path.read_text())
        yaml.safe_load(ROOT.joinpath('playbooks/dashboard/desk-dashboard.yml').read_text())
        for path in [ROOT/'roles/desk_monitoring/files/install_proxy.py', ROOT/'roles/desk_ui/files/ui_guard.py']:
            ast.parse(path.read_text())

if __name__ == '__main__':
    unittest.main()
