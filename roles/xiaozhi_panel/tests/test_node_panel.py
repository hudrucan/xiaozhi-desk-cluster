"""Panel logic fixtures: no GPIO access, network connections or service changes."""

import importlib.util
import json
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch


SPEC = importlib.util.spec_from_file_location(
    "node_panel", Path(__file__).resolve().parents[1] / "files" / "node_panel.py"
)
panel = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(panel)


class PanelFixtures(unittest.TestCase):
    def setUp(self):
        self.directory = TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.state = Path(self.directory.name) / "ingress.json"
        self.state.write_text('{"vip":"192.168.1.186"}')
        self.config = {
            "node_number": 1,
            "management_interface": "wlan0",
            "cluster_interface": "end0",
            "ingress_state_file": str(self.state),
        }

    def driver(self):
        return SimpleNamespace(
            DIGITS={"0": 0x3F, "1": 0x06, "2": 0x5B, "3": 0x4F},
            DIGIT_ADDRS=(0x68, 0x6A, 0x6C, 0x6E), ICON_ADDR=0x66,
            ICON_COLON=0x10, ICON_CLOCK=0x01, ICON_WIFI=0x40,
            ICON_LAN=0x20, ICON_USB=0x02,
            carrier=Mock(return_value=True), external_usb_present=Mock(return_value=True),
            initialize=Mock(), write_register=Mock(), GPIO_FDS={},
            LOCK_PATH=str(Path(self.directory.name) / "panel.lock"),
        )

    def address_result(self, vip=True, interface="wlan0"):
        return SimpleNamespace(returncode=0, stdout=json.dumps([{
            "ifname": interface,
            "addr_info": [{"family": "inet", "local":
                           "192.168.1.186" if vip else "192.168.1.101"}],
        }]))

    def test_node_identity_and_vip_glyphs(self):
        for node in (1, 2, 3):
            self.assertEqual(panel.display_text(node, False), f"--{node:02d}")
            self.assertEqual(panel.display_text(node, True), f"iP{node:02d}")
        self.assertEqual(panel.segments_for("iP01", self.driver()), (0x04, 0x73, 0x3F, 0x06))

    def test_only_actual_management_address_claims_vip(self):
        for owner, interface, expected in ((True, "wlan0", True),
                                            (False, "wlan0", False),
                                            (True, "end0", False)):
            with patch.object(panel.subprocess, "run", return_value=self.address_result(owner, interface)) as query:
                self.assertEqual(panel.local_vip_owner(self.config), expected)
                self.assertEqual(query.call_args.args[0],
                                 ["/usr/sbin/ip", "-j", "-4", "address", "show", "dev", "wlan0"])
                self.assertEqual(query.call_args.kwargs["timeout"], 2)

    def test_snapshot_is_applied_vip_not_a_pinned_address(self):
        self.state.write_text('{"vip":"192.168.1.199"}')
        with patch.object(panel.subprocess, "run", return_value=self.address_result()):
            self.assertFalse(panel.local_vip_owner(self.config))
        result = self.address_result()
        result.stdout = result.stdout.replace("192.168.1.186", "192.168.1.199")
        with patch.object(panel.subprocess, "run", return_value=result):
            self.assertTrue(panel.local_vip_owner(self.config))

    def test_missing_invalid_oversize_snapshot_does_not_claim_vip(self):
        for data in ('{', '[]', '{}', '{"vip":"bad"}', ' ' * (panel.MAX_JSON_BYTES + 1)):
            self.state.write_text(data)
            with patch.object(panel.subprocess, "run") as query:
                self.assertFalse(panel.local_vip_owner(self.config))
                query.assert_not_called()
        self.state.unlink()
        self.assertFalse(panel.local_vip_owner(self.config))

    def test_failed_or_malformed_address_query_does_not_claim_vip(self):
        for result in (SimpleNamespace(returncode=1, stdout=""),
                       SimpleNamespace(returncode=0, stdout="{"),
                       SimpleNamespace(returncode=0, stdout='[{"ifname":"wlan0"}]'),
                       SimpleNamespace(returncode=0, stdout="x" * (panel.MAX_JSON_BYTES + 1))):
            with patch.object(panel.subprocess, "run", return_value=result):
                self.assertFalse(panel.local_vip_owner(self.config))
        with patch.object(panel.subprocess, "run", side_effect=panel.subprocess.TimeoutExpired("ip", 2)):
            self.assertFalse(panel.local_vip_owner(self.config))

    def test_colon_clock_off_connectivity_icons_preserved(self):
        driver = self.driver()
        self.assertEqual(panel.connectivity_icons(self.config, driver), 0x62)
        driver.carrier.return_value = False
        driver.external_usb_present.return_value = False
        self.assertEqual(panel.connectivity_icons(self.config, driver), 0)

    def test_invalid_node_config_rejected_before_driver_loading(self):
        path = Path(self.directory.name) / "panel.json"
        for number in (0, 100, True, "1"):
            path.write_text(json.dumps({**self.config, "node_number": number}))
            with self.assertRaises(ValueError):
                panel.load_config(path)
        for interface in ("wlan0;bad", "../wlan0", "", "a" * 16):
            path.write_text(json.dumps({**self.config, "management_interface": interface}))
            with self.assertRaises(ValueError):
                panel.load_config(path)
        path.write_text(json.dumps(self.config))
        self.assertEqual(panel.load_config(path), self.config)

    def test_early_placeholder_never_queries_vip(self):
        driver = self.driver()
        with patch.object(panel.signal, "signal"), patch.object(panel, "local_vip_owner") as query:
            panel.run(self.config, driver, placeholder=True)
        query.assert_not_called()
        self.assertEqual(driver.write_register.call_args_list[-1].args, (0x66, 0))
        self.assertEqual([call.args[1] for call in driver.write_register.call_args_list[:4]],
                         [0x40, 0x40, 0x3F, 0x06])

    def test_failover_updates_status_without_rewriting_unchanged_frames(self):
        driver = self.driver()
        stop = Mock()
        stop.is_set.side_effect = [False, False, False, True]
        with patch.object(panel.signal, "signal"), patch.object(panel.threading, "Event", return_value=stop), \
                patch.object(panel, "local_vip_owner", side_effect=[True, True, False]):
            panel.run(self.config, driver)
        self.assertEqual(driver.write_register.call_count, 9)
        frames = [call.args for call in driver.write_register.call_args_list]
        self.assertEqual(frames[0], (0x68, 0x04))
        self.assertEqual(frames[1], (0x6A, 0x73))
        self.assertEqual(frames[5], (0x68, 0x40))
        self.assertEqual(frames[6], (0x6A, 0x40))
        self.assertEqual(stop.wait.call_count, 3)


if __name__ == "__main__":
    unittest.main()
