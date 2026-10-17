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
            ICON_COLON=0x10, ICON_CLOCK=0x01, ICON_PLAY=0x08, ICON_PAUSE=0x04, ICON_WIFI=0x40,
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

    def test_worker_activity_freshness_and_icons(self):
        path = Path(self.directory.name) / "activity.json"
        self.config.update(worker_activity_path=str(path), core_id="deskb1x")
        value = {"protocol":"xiaozhi-worker-activity-v1", "worker_id":"deskb1x",
            "instance":"a"*32,"boot_id":"fixture-boot", "monotonic":10.0,"asr":1,"llm":1}
        with patch.object(panel.Path, "read_text", return_value="fixture-boot"), patch.object(panel.time, "monotonic", return_value=10.0):
            path.write_text(json.dumps(value))
            self.assertEqual(panel.worker_icons(self.config,self.driver()), 0x01 | 0x08)
            value.update(asr=0,llm=0);path.write_text(json.dumps(value))
            self.assertEqual(panel.worker_icons(self.config,self.driver()), 0x04)
            value.update(monotonic=6.0);path.write_text(json.dumps(value))
            self.assertEqual(panel.worker_icons(self.config,self.driver()), 0)
            value.update(monotonic=10.0,boot_id="old-boot");path.write_text(json.dumps(value))
            self.assertIsNone(panel.worker_activity(self.config))

    def test_optional_tts_counter_keeps_play_lit_during_llm_blink_off(self):
        path = Path(self.directory.name) / "activity.json"
        self.config.update(worker_activity_path=str(path), core_id="deskb1x")
        value = {"protocol":"xiaozhi-worker-activity-v1", "worker_id":"deskb1x",
            "instance":"a"*32, "boot_id":"fixture-boot", "monotonic":10.5,
            "asr":0, "llm":1, "tts":1}
        with patch.object(panel.Path, "read_text", return_value="fixture-boot"), patch.object(panel.time, "monotonic", return_value=10.5):
            path.write_text(json.dumps(value))
            self.assertEqual(panel.worker_icons(self.config, self.driver()), 0x08)
            for invalid in (-1, 2, True, "1"):
                value['tts'] = invalid
                path.write_text(json.dumps(value))
                self.assertIsNone(panel.worker_activity(self.config))

    def test_worker_blink_does_not_show_pause_while_active(self):
        with patch.object(panel, "worker_activity", return_value={"asr":0,"llm":1}), patch.object(panel.time, "monotonic", return_value=10.5):
            self.assertEqual(panel.worker_icons(self.config,self.driver()),0)
        with patch.object(panel, "worker_activity", return_value={"asr":1,"llm":1}), patch.object(panel.time, "monotonic", return_value=10.5):
            self.assertEqual(panel.worker_icons(self.config,self.driver()),0x01)

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
        self.assertEqual(panel.connectivity_icons(self.config, driver), 0x66)
        driver.carrier.return_value = False
        driver.external_usb_present.return_value = False
        self.assertEqual(panel.connectivity_icons(self.config, driver), 0x04)

    def test_activity_icons_are_independent_of_fixed_roles(self):
        driver = self.driver()
        for mqtt in (False, True):
            for core in (False, True):
                for vip in (False, True):
                    panel.display_text(2, vip, mqtt, core)
                    self.assertEqual(panel.activity_icons(driver), driver.ICON_PAUSE)
        self.assertEqual(panel.activity_icons(driver, asr_active=True), driver.ICON_CLOCK)
        for kind in ("llm_active", "vlm_active"):
            self.assertEqual(panel.activity_icons(driver, **{kind: True}), driver.ICON_PLAY)
            # Blink dark phase must not falsely show an idle/Pause icon.
            self.assertEqual(panel.activity_icons(driver, blink_on=False, **{kind: True}), 0)
        self.assertEqual(panel.activity_icons(driver, tts_active=True, blink_on=False), driver.ICON_PLAY)
        self.assertEqual(panel.activity_icons(driver, asr_active=True, llm_active=True),
                         driver.ICON_CLOCK | driver.ICON_PLAY)
        self.assertEqual(panel.activity_icons(driver, llm_active=True, tts_active=True, blink_on=False),
                         driver.ICON_PLAY)

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
                patch.object(panel.time, "monotonic", side_effect=[0, 0, 1.5, 1.5, 3, 3]), \
                patch.object(panel, "local_vip_owner", side_effect=[True, True, False]):
            panel.run(self.config, driver)
        self.assertEqual(driver.write_register.call_count, 7)
        frames = [call.args for call in driver.write_register.call_args_list]
        self.assertEqual(frames[0], (0x68, 0x04))
        self.assertEqual(frames[1], (0x6A, 0x73))
        self.assertEqual(frames[5], (0x68, 0x40))
        self.assertEqual(frames[6], (0x6A, 0x40))
        self.assertEqual(stop.wait.call_count, 3)

    def test_mqtt_and_vip_status_are_independent(self):
        self.assertEqual(panel.display_text(2, False, True), "nt02")
        self.assertEqual(panel.display_text(2, True, True), "nt02")
        self.assertEqual(panel.segments_for("nt02", self.driver()), (0x54, 0x78, 0x3F, 0x5B))

    def mqtt_config(self):
        return {**self.config, "mqtt_status_host": "10.10.10.11",
                "mqtt_status_port": 8007, "gateway_id": "deskb1x"}

    def mqtt_response(self, payload, status=200):
        client = Mock()
        client.getresponse.return_value.status = status
        client.getresponse.return_value.read.return_value = payload
        return client

    def test_authenticated_mqtt_connection_is_visible_without_audio_session(self):
        for count, expected in ((0, False), (1, True), (2, True), (True, False), (-1, False), ("1", False)):
            data = json.dumps({"gateway_id": "deskb1x", "status": "ready",
                               "active_mqtt_connections": count,
                               "active_websocket_sessions": 0}).encode()
            client = self.mqtt_response(data)
            with patch.object(panel, "HTTPConnection", return_value=client) as connection:
                self.assertEqual(panel.mqtt_connection_active(self.mqtt_config()), expected)
                connection.assert_called_once_with("10.10.10.11", 8007, timeout=0.3)
                client.request.assert_called_once_with("GET", "/status")
                client.close.assert_called_once()

    def test_bad_or_unavailable_status_does_not_claim_mqtt(self):
        payloads = (b'{', b'[]', b'{}', b'x' * (panel.MAX_JSON_BYTES + 1),
                    b'{"gateway_id":"deskb2x","status":"ready","active_mqtt_connections":1}',
                    b'{"gateway_id":"deskb1x","status":"stopping","active_mqtt_connections":1}')
        for payload in payloads:
            with patch.object(panel, "HTTPConnection", return_value=self.mqtt_response(payload)):
                self.assertFalse(panel.mqtt_connection_active(self.mqtt_config()))
        with patch.object(panel, "HTTPConnection", return_value=self.mqtt_response(b'{}', 302)):
            self.assertFalse(panel.mqtt_connection_active(self.mqtt_config()))
        with patch.object(panel, "HTTPConnection", side_effect=TimeoutError):
            self.assertFalse(panel.mqtt_connection_active(self.mqtt_config()))
        with patch.object(panel, "HTTPConnection") as connection:
            self.assertFalse(panel.mqtt_connection_active(self.config))
            connection.assert_not_called()

    def test_mqtt_connect_disconnect_updates_panel(self):
        driver = self.driver()
        stop = Mock()
        stop.is_set.side_effect = [False, False, False, True]
        with patch.object(panel.signal, "signal"), patch.object(panel.threading, "Event", return_value=stop), \
                patch.object(panel.time, "monotonic", side_effect=[0, 0, 1.5, 1.5, 3, 3]), \
                patch.object(panel, "local_vip_owner", return_value=True), \
                patch.object(panel, "mqtt_connection_active", side_effect=[False, True, False]):
            panel.run(self.config, driver)
        frames = driver.write_register.call_args_list
        self.assertEqual(frames[0].args, (0x68, 0x04))
        self.assertEqual(frames[5].args, (0x68, 0x54))
        self.assertEqual(frames[7].args, (0x68, 0x04))
        self.assertEqual(stop.wait.call_count, 3)

    def test_placeholder_never_queries_gateway(self):
        with patch.object(panel.signal, "signal"), patch.object(panel, "mqtt_connection_active") as query:
            panel.run(self.mqtt_config(), self.driver(), placeholder=True)
        query.assert_not_called()

    def test_invalid_gateway_config_rejected(self):
        path = Path(self.directory.name) / "panel.json"
        for changes in ({"mqtt_status_host": "bad"}, {"mqtt_status_host": "0.0.0.0"},
                        {"mqtt_status_host": 167774731}, {"mqtt_status_port": True},
                        {"mqtt_status_port": 80}, {"gateway_id": "../node"}):
            path.write_text(json.dumps({**self.mqtt_config(), **changes}))
            with self.assertRaises(ValueError):
                panel.load_config(path)

    def core_config(self):
        return {**self.config, "core_status_host": "10.10.10.11",
                "core_status_port": 8000, "core_id": "deskb1x"}

    def test_full_width_roles_cover_all_combinations(self):
        for mqtt, core, vip, expected in (
            (False, False, False, ["--"]),
            (True, False, False, ["nt"]),
            (False, True, False, ["Co"]),
            (False, False, True, ["iP"]),
            (True, True, False, ["nt", "Co"]),
            (True, False, True, ["nt", "iP"]),
            (False, True, True, ["Co", "iP"]),
            (True, True, True, ["nt", "Co", "iP"]),
        ):
            for phase in range(6):
                self.assertEqual(panel.display_text(2, vip, mqtt, core, phase),
                                 expected[phase % len(expected)] + "02")
        self.assertEqual(panel.segments_for("Co03", self.driver()),
                         (0x39, 0x5C, 0x3F, 0x4F))
        self.assertEqual(panel.segments_for("--02", self.driver()),
                         (0x40, 0x40, 0x3F, 0x5B))

    def test_three_roles_rotate_without_rewriting_node_digits(self):
        driver = self.driver()
        stop = Mock()
        stop.is_set.side_effect = [False] * 4 + [True]
        with patch.object(panel.signal, "signal"), \
                patch.object(panel.threading, "Event", return_value=stop), \
                patch.object(panel.time, "monotonic", side_effect=[0, 0, 1.5, 1.5, 3, 3, 4.5, 4.5]), \
                patch.object(panel, "local_vip_owner", return_value=True), \
                patch.object(panel, "mqtt_connection_active", return_value=True), \
                patch.object(panel, "core_session_active", return_value=True):
            panel.run(self.config, driver)
        calls = [call.args for call in driver.write_register.call_args_list]
        self.assertEqual([value for address, value in calls if address == 0x68],
                         [0x54, 0x39, 0x04, 0x54])
        self.assertEqual([value for address, value in calls if address == 0x6A],
                         [0x78, 0x5C, 0x73, 0x78])
        self.assertEqual([value for address, value in calls if address == 0x6C], [0x3F])
        self.assertEqual([value for address, value in calls if address == 0x6E], [0x06])

    def test_fast_frame_ticks_do_not_increase_network_polling(self):
        driver = self.driver()
        stop = Mock()
        stop.is_set.side_effect = [False] * 4 + [True]
        with patch.object(panel.signal, "signal"), \
                patch.object(panel.threading, "Event", return_value=stop), \
                patch.object(panel.time, "monotonic", side_effect=[0, 0, 0.25, 0.5, 0.75]), \
                patch.object(panel, "local_vip_owner", return_value=True) as vip, \
                patch.object(panel, "mqtt_connection_active", return_value=True) as mqtt, \
                patch.object(panel, "core_session_active", return_value=False) as core:
            panel.run(self.config, driver)
        vip.assert_called_once()
        mqtt.assert_called_once()
        core.assert_called_once()
        self.assertEqual(driver.write_register.call_count, 5)
        self.assertEqual(stop.wait.call_args.args, (panel.FRAME_INTERVAL_SECONDS,))

    def test_core_requires_established_local_session_and_valid_status(self):
        base = {"protocol": "xiaozhi-core-transport-v1", "core_id": "deskb1x", "status": "ready", "active_sessions": 1}
        for changes, expected in (({}, True), ({"active_sessions": 0}, False),
                                  ({"active_sessions": True}, False), ({"active_sessions": -1}, False),
                                  ({"core_id": "deskb2x"}, False), ({"protocol": "wrong"}, False),
                                  ({"status": "stopping"}, False)):
            payload = json.dumps({**base, **changes}).encode()
            with patch.object(panel, "HTTPConnection", return_value=self.mqtt_response(payload)) as request:
                self.assertEqual(panel.core_session_active(self.core_config()), expected)
                request.assert_called_once_with("10.10.10.11", 8000, timeout=0.3)
        for payload in (b"[]", b"{", b"x" * (panel.MAX_JSON_BYTES + 1)):
            with patch.object(panel, "HTTPConnection", return_value=self.mqtt_response(payload)):
                self.assertFalse(panel.core_session_active(self.core_config()))
        with patch.object(panel, "HTTPConnection", side_effect=TimeoutError):
            self.assertFalse(panel.core_session_active(self.core_config()))
        with patch.object(panel, "HTTPConnection", return_value=self.mqtt_response(b"{}", 302)):
            self.assertFalse(panel.core_session_active(self.core_config()))

    def test_placeholder_never_queries_core(self):
        with patch.object(panel.signal, "signal"), patch.object(panel, "core_session_active") as query:
            panel.run(self.core_config(), self.driver(), placeholder=True)
        query.assert_not_called()

    def test_core_session_end_removes_Co_preserving_vip(self):
        driver = self.driver()
        stop = Mock()
        stop.is_set.side_effect = [False, False, False, True]
        with patch.object(panel.signal, "signal"), patch.object(panel.threading, "Event", return_value=stop), \
                patch.object(panel.time, "monotonic", side_effect=[0, 0, 1.5, 1.5, 3, 3]), \
                patch.object(panel, "local_vip_owner", return_value=True), \
                patch.object(panel, "core_session_active", side_effect=[False, True, False]):
            panel.run(self.config, driver)
        frames = driver.write_register.call_args_list
        self.assertEqual(frames[0].args, (0x68, 0x04))
        self.assertEqual(frames[5].args, (0x68, 0x39))
        self.assertEqual(frames[7].args, (0x68, 0x04))

    def test_invalid_core_configuration_rejected(self):
        path = Path(self.directory.name) / "panel.json"
        for changes in ({"core_status_host": "bad"}, {"core_status_host": "0.0.0.0"},
                        {"core_status_port": True}, {"core_status_port": 80}, {"core_id": "../node"}):
            path.write_text(json.dumps({**self.core_config(), **changes}))
            with self.assertRaises(ValueError):
                panel.load_config(path)


class ReadCacheTests(unittest.TestCase):
    def setUp(self):
        self.directory = TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.path = Path(self.directory.name) / "activity.json"
        self.tick = [0.0]
        self.cache = panel.ReadCache(monotonic=lambda: self.tick[0])
        self.config = {"worker_activity_path": str(self.path), "dns_activity_path": str(self.path), "core_id": "deskb1x"}

    def worker(self, **changes):
        value = {"protocol": "xiaozhi-worker-activity-v1", "worker_id": "deskb1x", "instance": "a" * 32,
                 "boot_id": "fixture-boot", "monotonic": 10.0, "asr": 1, "llm": 0}
        value.update(changes)
        return value

    def test_unchanged_file_is_parsed_once_until_max_age_and_changes_are_seen(self):
        self.path.write_text(json.dumps({"n": 1}))
        with patch.object(panel, "read_json", wraps=panel.read_json) as read:
            first = self.cache.json(str(self.path))
            self.tick[0] += 0.1
            self.assertIs(self.cache.json(str(self.path)), first)
            self.assertEqual(read.call_count, 1)
            self.path.write_text(json.dumps({"n": 22}))  # different size: a new stat key
            self.assertEqual(self.cache.json(str(self.path)), {"n": 22})
            self.assertEqual(read.call_count, 2)
            self.tick[0] += panel.READ_CACHE_MAX_AGE_SECONDS
            self.cache.json(str(self.path))
            self.assertEqual(read.call_count, 3)

    def test_errors_match_read_json_and_are_never_cached(self):
        with self.assertRaises(OSError):self.cache.json(str(self.path))
        self.path.write_text("{")
        with self.assertRaises(ValueError):self.cache.json(str(self.path))
        self.path.write_text(json.dumps({"ok": True}))
        self.assertEqual(self.cache.json(str(self.path)), {"ok": True})
        self.path.write_text(" " * (panel.MAX_JSON_BYTES + 1))
        with self.assertRaises(ValueError):self.cache.json(str(self.path))
        self.assertEqual(self.cache.entries, {})

    def test_boot_id_is_read_once(self):
        with patch.object(panel.Path, "read_text", return_value="fixture-boot\n") as read:
            self.assertEqual(self.cache.boot_id(), "fixture-boot")
            self.assertEqual(self.cache.boot_id(), "fixture-boot")
            self.assertEqual(read.call_count, 1)

    def test_cached_readers_return_what_uncached_readers_return(self):
        steps = [(self.worker(), 10.0), (self.worker(monotonic=6.0), 10.0), (self.worker(boot_id="old-boot"), 10.0),
                 (self.worker(asr=2, llm=2, tts=1), 10.0), ({"junk": 1}, 10.0), (self.worker(), 10.0)]
        dns = {"protocol": "desk-dns-activity-v1", "node_id": "deskb1x", "instance": "b" * 32, "boot_id": "fixture-boot",
               "monotonic": 10.0, "queries": 3, "last_query": 9.9}
        steps += [(dns, 10.5), (dict(dns, queries=4), 10.5), (dict(dns, monotonic=1.0), 10.5)]
        with patch.object(panel.Path, "read_text", return_value="fixture-boot"), patch.object(panel.time, "monotonic", return_value=10.0):
            for value, now in steps:
                self.path.write_text(json.dumps(value))
                self.tick[0] += 1  # beyond max_age, so even a coarse mtime tick cannot hide the rewrite
                with patch.object(panel, "_CACHE", None):
                    expected = (panel.worker_activity(self.config), panel.dns_activity(self.config, now))
                with patch.object(panel, "_CACHE", self.cache):
                    self.assertEqual((panel.worker_activity(self.config), panel.dns_activity(self.config, now)), expected)

    def test_freshness_is_judged_on_every_call_even_when_the_file_is_cached(self):
        self.path.write_text(json.dumps(self.worker()))
        with patch.object(panel.Path, "read_text", return_value="fixture-boot"), patch.object(panel, "_CACHE", self.cache):
            with patch.object(panel.time, "monotonic", return_value=10.0):
                self.assertIsNotNone(panel.worker_activity(self.config))
            with patch.object(panel.time, "monotonic", return_value=13.5), patch.object(panel, "read_json") as read:
                self.assertIsNone(panel.worker_activity(self.config))
                read.assert_not_called()

    def test_cache_is_enabled_only_inside_the_frame_loop(self):
        seen = []
        driver = SimpleNamespace(DIGITS={"0": 0x3F, "2": 0x5B}, DIGIT_ADDRS=(0x68, 0x6A, 0x6C, 0x6E), ICON_ADDR=0x66,
            ICON_COLON=0x10, ICON_CLOCK=1, ICON_PLAY=8, ICON_PAUSE=4, ICON_WIFI=0x40, initialize=Mock(), write_register=Mock(),
            GPIO_FDS={}, LOCK_PATH=str(Path(self.directory.name) / "panel.lock"))
        stop = Mock()
        stop.is_set.side_effect = [False, True]
        config = {"node_number": 2, "management_interface": "wlan0", "cluster_interface": "end0"}
        self.assertIsNone(panel._CACHE)
        with patch.object(panel.signal, "signal"), patch.object(panel.threading, "Event", return_value=stop), \
             patch.object(panel, "local_vip_owner", return_value=False), patch.object(panel, "mqtt_connection_active", return_value=False), \
             patch.object(panel, "core_session_active", return_value=False), patch.object(panel, "connectivity_icons", return_value=0), \
             patch.object(panel, "management_addresses", return_value=[]), patch.object(panel, "worker_icons", side_effect=lambda *a: seen.append(panel._CACHE) or 0), \
             patch.object(panel, "dns_activity", return_value=None):
            panel.run(config, driver)
        self.assertIsInstance(seen[0], panel.ReadCache)
        self.assertIsNone(panel._CACHE)


if __name__ == "__main__":
    unittest.main()
