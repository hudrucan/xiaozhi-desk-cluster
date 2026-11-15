"""Periodic unit filters retain operator diagnostics without success chatter."""
import contextlib
import importlib.util
import io
from pathlib import Path
import unittest
from unittest.mock import patch

import jinja2
import yaml

ROOT = Path(__file__).resolve().parents[1]


class NoticeUnits(unittest.TestCase):
    def unit(self, path):
        return jinja2.Template((ROOT / path).read_text(), undefined=jinja2.StrictUndefined).render(xiaozhi_ingress_state_file='/etc/xiaozhi-ingress/state.json')

    def test_rendered_units_keep_all_stdout_diagnostics_except_ddns_routine_info(self):
        paths = ['roles/xiaozhi_ingress/templates/peer-arp.service.j2',
                 'roles/desk_dns/templates/desk-dns-sync.service.j2',
                 'roles/desk_dns/templates/desk-dns-filter-refresh.service.j2']
        units = [self.unit(p) for p in paths]
        tasks = yaml.safe_load((ROOT / 'roles/desk_dns/tasks/peer_arp.yml').read_text())
        units += [t['ansible.builtin.copy']['content'] for t in tasks
                  if t.get('ansible.builtin.copy', {}).get('dest', '').endswith('.service')]
        self.assertEqual(len(units), 4)
        for unit in units:
            with self.subTest(unit=unit.splitlines()[1]):
                service = unit.split('[Service]\n', 1)[1]
                self.assertIn('LogLevelMax=notice\n', service)
                self.assertIn('SyslogLevel=notice\n', service)
                self.assertIn('Type=oneshot\n', service)
        ddns = self.unit('roles/desk_vpn_ddns/templates/desk-vpn-ddns.service.j2')
        self.assertIn('LogLevelMax=notice\n', ddns)
        self.assertIn('SyslogLevel=info\n', ddns)
        self.assertIn('SyslogLevelPrefix=yes\n', ddns)

    def test_ddns_changes_and_failures_have_priorities_above_notice_cutoff(self):
        spec = importlib.util.spec_from_file_location('notice_ddns', ROOT / 'roles/desk_vpn_ddns/files/ddns.py')
        ddns = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(ddns)
        for outcome in ('updated', 'ownership_changed', 'unchanged', 'standby', 'failed'):
            stdout = io.StringIO()
            with self.subTest(outcome=outcome), patch.object(ddns, 'is_owner', return_value=outcome != 'standby'), patch.object(ddns, 'load', return_value={}), patch.object(ddns, 'credentials', return_value={'api_token': 'fixture'}), patch.object(ddns, 'Client'), patch.object(ddns, 'reconcile', return_value=outcome, side_effect=ddns.Failure('network_unavailable') if outcome == 'failed' else None), patch.dict(ddns.os.environ, CREDENTIALS_DIRECTORY='/fixture'), contextlib.redirect_stdout(stdout):
                code = ddns.main()
            line = stdout.getvalue()
            # SyslogLevelPrefix defaults an unprefixed line to unit info (6).
            level = int(line[1]) if line.startswith('<') else 6
            self.assertEqual(level <= 5, outcome in ('updated', 'ownership_changed', 'failed'))
            self.assertEqual(code, 1 if outcome == 'failed' else 0)
            self.assertNotIn('fixture', line)
