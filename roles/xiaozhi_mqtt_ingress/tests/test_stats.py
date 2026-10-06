"""Offline HAProxy stats fixtures; do not open sockets or start services."""
import importlib.util
from pathlib import Path
import unittest

spec = importlib.util.spec_from_file_location(
    "ingress_probe", Path(__file__).resolve().parents[2] / "xiaozhi_ingress/files/ingress_probe.py")
probe = importlib.util.module_from_spec(spec)
spec.loader.exec_module(probe)
NODES = ["deskb1x", "deskb2x", "deskb3x"]


def payload(frontend, backend, statuses):
    return ("# pxname,svname,status\n" + f"{frontend},FRONTEND,OPEN\n"
            + "".join(f"{backend},{node},{status}\n" for node, status in zip(NODES, statuses)))


class StatsFixtures(unittest.TestCase):
    def test_settings_defaults_remain_compatible(self):
        data = payload("xiaozhi_settings", "xiaozhi_control_planes", ["UP"] * 3)
        self.assertTrue(probe.stats_ready(data, NODES, True))
        data = payload("xiaozhi_settings", "xiaozhi_control_planes", ["UP", "DOWN", "DOWN"])
        self.assertTrue(probe.stats_ready(data, NODES))
        self.assertFalse(probe.stats_ready(data, NODES, True))

    def test_mqtt_pool_is_verified_independently(self):
        settings = payload("xiaozhi_settings", "xiaozhi_control_planes", ["UP"] * 3)
        self.assertFalse(probe.stats_ready(settings, NODES, True, "xiaozhi_mqtt", "xiaozhi_mqtt_gateways"))
        mqtt = payload("xiaozhi_mqtt", "xiaozhi_mqtt_gateways", ["UP"] * 3)
        self.assertTrue(probe.stats_ready(mqtt, NODES, True, "xiaozhi_mqtt", "xiaozhi_mqtt_gateways"))

    def test_dead_or_missing_backend_fails_strict_mqtt_verification(self):
        for statuses in (["UP", "UP", "DOWN"], ["UP", "UP"]):
            data = payload("xiaozhi_mqtt", "xiaozhi_mqtt_gateways", statuses)
            self.assertFalse(probe.stats_ready(data, NODES, True, "xiaozhi_mqtt", "xiaozhi_mqtt_gateways"))

    def test_malformed_closed_or_duplicate_frontend_fails(self):
        valid = payload("xiaozhi_mqtt", "xiaozhi_mqtt_gateways", ["UP"] * 3)
        for data in ("", "not csv", valid.replace("OPEN", "STOP"),
                     valid + "xiaozhi_mqtt,FRONTEND,OPEN\n"):
            self.assertFalse(probe.stats_ready(data, NODES, True, "xiaozhi_mqtt", "xiaozhi_mqtt_gateways"))


if __name__ == "__main__":
    unittest.main()
