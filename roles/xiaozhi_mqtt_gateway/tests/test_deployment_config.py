"""Offline deployment input fixtures; no host/network/runtime execution."""

import copy
import importlib.util
import json
from pathlib import Path
import subprocess
import sys
import unittest


VALIDATOR = Path(__file__).resolve().parents[1] / "files/deployment_config.py"
spec = importlib.util.spec_from_file_location("deployment_config", VALIDATOR)
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)


def inputs():
    return {
        "revision": "a" * 40,
        "node_version": "24.21.0",
        "node_sha256": "b" * 64,
        "gateway_id": "deskb1x",
        "management_ip": "192.168.1.101", "cluster_ip": "10.10.10.11",
        "interfaces": [{"addr_info": [{"family": "inet", "local": "192.168.1.101"}]},
                       {"addr_info": [{"family": "inet", "local": "10.10.10.11"}]}],
        "mqtt_port": 1883, "udp_port": 1883, "http_port": 8007,
        "chat_servers": ["ws://10.10.10.11:8000/xiaozhi/v1/"],
    }


class DeploymentInputs(unittest.TestCase):
    def reject(self, **changes):
        candidate = inputs()
        candidate.update(changes)
        with self.assertRaises((ValueError, TypeError, KeyError, AttributeError)):
            module.validate(candidate)

    def test_same_tcp_udp_port_and_private_core(self):
        self.assertEqual(module.validate(inputs()), {
            "backend_targets": [{"host": "10.10.10.11", "port": 8000}]})

    def test_wss_ipv6_and_explicit_local_core(self):
        candidate = inputs()
        candidate["chat_servers"] = ["wss://core.example.org/xiaozhi/v1/",
                                     "ws://[fd00::11]:8000/xiaozhi/v1/",
                                     "ws://127.0.0.1:8000/xiaozhi/v1/"]
        self.assertEqual([target["port"] for target in module.validate(candidate)["backend_targets"]],
                         [443, 8000, 8000])

    def test_source_and_runtime_pins(self):
        for key, values in {"revision": ["main", "a" * 39, "../release"],
                            "node_version": ["20.18.2", "24/latest", "24.21.0\n"],
                            "node_sha256": ["sha256", "b" * 63]}.items():
            for value in values:
                with self.subTest(key=key, value=value):
                    self.reject(**{key: value})

    def test_identity_and_management_address(self):
        for value in ["../node", "node\n", "", "n" * 129]:
            self.reject(gateway_id=value)
        for value in ["127.0.0.1", "0.0.0.0", "224.0.0.1", "::1", 3232235877]:
            self.reject(management_ip=value)

    def test_address_must_be_owned_once(self):
        self.reject(interfaces=[])
        self.reject(interfaces=inputs()["interfaces"] * 2)
        self.reject(interfaces=[{"addr_info": [{"family": "inet", "local": "192.168.1.102"}]}])

    def test_private_address_must_be_distinct_and_owned(self):
        for value in ("192.168.1.101", "127.0.0.1", "0.0.0.0", "224.0.0.1",
                      "8.8.8.8", "::1", 167774731):
            self.reject(cluster_ip=value)
        self.reject(cluster_ip="10.10.10.12")

    def test_unprivileged_ports_and_http_collision(self):
        for key in ("mqtt_port", "udp_port", "http_port"):
            for value in (0, 80, 65536, True, "1883", 1883.0):
                self.reject(**{key: value})
        self.reject(http_port=1883)

    def test_backend_list_bound_and_duplicates(self):
        for value in ([], "ws://core.example", ["ws://core.example"] * 2,
                      [f"ws://core{i}.example" for i in range(4)]):
            self.reject(chat_servers=value)

    def test_credentials_and_gateway_query_marker_rejected(self):
        for value in ("ws://user:SECRET@core.example/x/", "ws://core.example/x/?foo=bar",
                      "ws://core.example/x/?from=mqtt_gateway", "ws://core.example/x/?",
                      "ws://core.example/x/#", "ws://core.example/x/#fragment"):
            self.reject(chat_servers=[value])

    def test_invalid_or_ambiguous_backends(self):
        for value in ("http://core.example/x/", "ws://core.example:/x/",
                      "ws://core.example:0/x/", "ws://core.example:65536/x/",
                      "ws://core.example/\n", "ws://core.example/\x7f",
                      "ws://127.1/x/", "ws://0x7f000001/x/", "ws://0.0.0.0/x/",
                      "ws://224.0.0.1/x/", "ws://[fe80::1%eth0]/x/", None):
            self.reject(chat_servers=[value])

    def test_validation_does_not_mutate_public_inputs(self):
        candidate = inputs()
        previous = copy.deepcopy(candidate)
        module.validate(candidate)
        self.assertEqual(candidate, previous)

    def test_cli_safe_failure_and_size_bound(self):
        for payload in (b"X" * 65537, b'{"password":"SENTINEL_PRIVATE"}', b"[]", b"not JSON"):
            result = subprocess.run([sys.executable, str(VALIDATOR)], input=payload,
                                    capture_output=True, check=False)
            self.assertEqual(result.returncode, 1)
            self.assertEqual(result.stdout, b"")
            self.assertNotIn(b"SENTINEL_PRIVATE", result.stderr)
            self.assertNotIn(b"Traceback", result.stderr)

    def test_cli_valid_public_output(self):
        result = subprocess.run([sys.executable, str(VALIDATOR)],
                                input=json.dumps(inputs()).encode(), capture_output=True, check=True)
        self.assertEqual(json.loads(result.stdout), module.validate(inputs()))


if __name__ == "__main__":
    unittest.main()
