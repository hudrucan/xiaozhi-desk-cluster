"""DNS panel fixtures use only local metadata and mocked GPIO/network calls."""
import importlib.util
import json
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch


spec = importlib.util.spec_from_file_location('dns_panel', Path(__file__).resolve().parents[1] / 'files/node_panel.py')
panel = importlib.util.module_from_spec(spec)
spec.loader.exec_module(panel)


class DnsPanelTests(unittest.TestCase):
    def setUp(self):
        self.directory = TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.path = Path(self.directory.name) / 'activity.json'
        self.config = {'node_number': 2, 'management_interface': 'wlan0', 'cluster_interface': 'end0',
                       'ingress_state_file': str(Path(self.directory.name) / 'ingress.json'),
                       'dns_activity_path': str(self.path), 'core_id': 'deskb2x'}

    def metadata(self, now=10, queries=1, last=10):
        return {'protocol': 'desk-dns-activity-v1', 'node_id': 'deskb2x', 'instance': 'a' * 32,
                'boot_id': 'current-boot', 'monotonic': now, 'queries': queries, 'last_query': last}

    def test_dn_owner_rotates_with_existing_roles_and_keeps_node_digits(self):
        driver = SimpleNamespace(DIGITS={'0': 0x3F, '2': 0x5B})
        self.assertEqual(panel.display_text(2, False, dns_owner=True), 'dn02')
        self.assertEqual(panel.segments_for('dn02', driver), (0x5E, 0x54, 0x3F, 0x5B))
        self.assertEqual([panel.display_text(2, True, True, True, n, True) for n in range(5)],
                         ['nt02', 'Co02', 'iP02', 'dn02', 'nt02'])
        self.assertEqual(panel.display_text(2, False), '--02')

    def test_dns_vip_uses_dns_snapshot_and_actual_management_address(self):
        state = Path(self.directory.name) / 'dns-vip.json'
        state.write_text('{"vip":"192.168.1.250"}')
        settings = {**self.config, 'dns_state_file': str(state)}
        for address, expected in (('192.168.1.102', False), ('192.168.1.186', False), ('192.168.1.250', True)):
            result = SimpleNamespace(returncode=0, stdout=json.dumps([{'ifname': 'wlan0',
                                     'addr_info': [{'family': 'inet', 'local': address}]}]))
            with patch.object(panel.subprocess, 'run', return_value=result):
                self.assertEqual(panel.local_vip_owner(settings, 'dns_state_file'), expected)
        state.unlink()
        self.assertFalse(panel.local_vip_owner(settings, 'dns_state_file'))

    def test_activity_rejects_stale_wrong_boot_node_and_invalid_counters(self):
        base = self.metadata()
        with patch.object(panel.Path, 'read_text', return_value='current-boot'):
            self.path.write_text(json.dumps(base))
            self.assertEqual(panel.dns_activity(self.config, 10), base)
            for update in ({'monotonic': 7.9}, {'monotonic': 11}, {'monotonic': float('nan')},
                           {'node_id': 'deskb1x'}, {'boot_id': 'previous-boot'}, {'instance': 'bad'},
                           {'queries': True}, {'queries': -1}, {'queries': 2**63},
                           {'last_query': 11}, {'last_query': float('inf')}, {'last_query': None}):
                self.path.write_text(json.dumps({**base, **update}))
                with self.subTest(update=update):
                    self.assertIsNone(panel.dns_activity(self.config, 10))
            for content in ('{', '[]', ' ' * (panel.MAX_JSON_BYTES + 1)):
                self.path.write_text(content)
                self.assertIsNone(panel.dns_activity(self.config, 10))
        self.path.unlink()
        self.assertIsNone(panel.dns_activity(self.config, 10))
        self.assertIsNone(panel.dns_activity({}, 10))

    def test_idle_heartbeats_and_old_queries_do_not_flash(self):
        pulse = panel.DnsPulse()
        for now in (10, 10.5, 11):
            self.assertFalse(pulse.update(self.metadata(now, 0, None), now))
        self.assertFalse(pulse.update(self.metadata(12, 100, 10), 12))
        self.assertFalse(pulse.update({**self.metadata(12, 1, 10), 'instance': 'b' * 32}, 12))

    def test_pulses_last_150ms_and_continuous_load_is_capped_at_two_hz(self):
        pulse = panel.DnsPulse()
        starts, previous = [], False
        for index in range(40):
            now = round(index * .05, 3)
            on = pulse.update(self.metadata(now, index + 1, now), now)
            if on and not previous:
                starts.append(now)
            if not on and previous:
                self.assertAlmostEqual(now - starts[-1], .15)
            previous = on
        self.assertEqual(starts, [0, .5, 1, 1.5])

    def test_missing_metadata_clears_pending_and_colon_without_replaying_old_query(self):
        pulse = panel.DnsPulse()
        self.assertTrue(pulse.update(self.metadata(), 10))
        self.assertFalse(pulse.update(None, 10.05))
        self.assertFalse(pulse.update(self.metadata(11, 1, 10), 11))

    def test_frame_loop_composes_colon_with_play_without_rewriting_role_digits(self):
        driver = SimpleNamespace(DIGITS={'0': 0x3F, '2': 0x5B}, DIGIT_ADDRS=(0x68, 0x6A, 0x6C, 0x6E),
            ICON_ADDR=0x66, ICON_COLON=0x10, ICON_CLOCK=1, ICON_PLAY=8, ICON_PAUSE=4, ICON_WIFI=0x40,
            initialize=Mock(), write_register=Mock(), GPIO_FDS={},
            LOCK_PATH=str(Path(self.directory.name) / 'panel.lock'))
        stop = Mock()
        stop.is_set.side_effect = [False] * 5 + [True]
        with patch.object(panel.signal, 'signal'), patch.object(panel.threading, 'Event', return_value=stop), \
             patch.object(panel.time, 'monotonic', side_effect=[0, 0, .05, .1, .15, .2]), \
             patch.object(panel, 'local_vip_owner', return_value=False), \
             patch.object(panel, 'mqtt_connection_active', return_value=False), \
             patch.object(panel, 'core_session_active', return_value=False), \
             patch.object(panel, 'connectivity_icons', return_value=0x48), \
             patch.object(panel, 'worker_icons', return_value=8), \
             patch.object(panel, 'dns_activity', return_value=self.metadata(0, 1, 0)):
            panel.run(self.config, driver)
        calls = [call.args for call in driver.write_register.call_args_list]
        self.assertEqual([value for address, value in calls if address == 0x66], [0x58, 0x48])
        for address in driver.DIGIT_ADDRS:
            self.assertEqual(len([call for call in calls if call[0] == address]), 1)

    def test_dns_metadata_paths_are_fixed_and_opt_in_config_is_valid(self):
        path = Path(self.directory.name) / 'panel.json'
        base = {**self.config, 'dns_state_file': '/etc/desk-dns/vip.json',
                'dns_activity_path': '/run/desk-dns/activity.json'}
        path.write_text(json.dumps(base))
        self.assertEqual(panel.load_config(path), base)
        for key in ('dns_state_file', 'dns_activity_path'):
            path.write_text(json.dumps({**base, key: '/etc/shadow'}))
            with self.assertRaises(ValueError):
                panel.load_config(path)


if __name__ == '__main__':
    unittest.main()
