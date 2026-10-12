"""Offline activation guards; no module loading, reboot or network calls."""
import importlib.util
import json
from pathlib import Path
from types import SimpleNamespace
import unittest
from unittest.mock import patch, Mock

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('wifi_driver', ROOT / 'files/wifi-driver.py')
driver = importlib.util.module_from_spec(spec)
spec.loader.exec_module(driver)


class DriverTests(unittest.TestCase):
    def test_manifest_rejects_unreviewed_kernel_source_and_feature(self):
        manifest = json.loads((ROOT / 'files/driver-manifest.json').read_text())
        driver.validate_manifest(manifest)
        for key, value in [('kernel_release', '6.1.175-rk35xx-ophub'),
                           ('module_sha256', '0' * 64), ('arp_offload', True),
                           ('arp_offload', 0), ('source_commit', '0' * 40),
                           ('driver_version', '6.4.4.0'), ('depends', 'unexpected')]:
            with self.subTest(key=key, value=value), self.assertRaises(ValueError):
                driver.validate_manifest({**manifest, key: value})

    def test_wrong_checksum_is_rejected_before_modinfo(self):
        with patch.object(driver, 'sha', return_value='0' * 64), patch.object(driver, 'run') as run:
            with self.assertRaisesRegex(ValueError, 'checksum'):
                driver.artifact(Path('/candidate.ko'))
            run.assert_not_called()

    def test_correct_checksum_still_requires_matching_abi(self):
        with patch.object(driver, 'sha', return_value=driver.MODULE_SHA), \
             patch.object(driver, 'run', return_value=SimpleNamespace(stdout='different kernel')):
            with self.assertRaisesRegex(ValueError, 'metadata mismatch'):
                driver.artifact(Path('/candidate.ko'))

    def test_verify_requires_running_module_not_only_installed_file(self):
        healthy = {'override_sha256': driver.MODULE_SHA, 'selected_override': True, 'runtime_ready': True}
        with patch.object(driver, 'inspect', return_value=healthy):
            self.assertEqual(driver.verify('192.168.1.101'), healthy)
        for key, value in [('override_sha256', None), ('selected_override', False), ('runtime_ready', False)]:
            with self.subTest(key=key), patch.object(driver, 'inspect', return_value={**healthy, key: value}):
                with self.assertRaisesRegex(ValueError, 'selected AND running'):
                    driver.verify('192.168.1.101')

    def reboot(self, addresses, services='inactive\ninactive\n', initramfs='usr/lib/firmware/regulatory.db\n'):
        replies = [SimpleNamespace(stdout=json.dumps([{'addr_info': addresses}])),
                   SimpleNamespace(stdout=services), SimpleNamespace(stdout=initramfs)]
        with patch.object(driver, 'inspect', return_value={'interface': 'wlan0'}), \
             patch.object(driver, 'run', side_effect=replies):
            return driver.can_reboot('192.168.1.101')

    def test_activation_requires_no_ipv4_or_ipv6_vip(self):
        primary = {'family': 'inet', 'local': '192.168.1.101'}
        for vip in [{'family': 'inet', 'local': '192.168.1.186'},
                    {'family': 'inet', 'local': '192.168.1.250'},
                    {'family': 'inet6', 'local': 'fe80::250'}]:
            with self.subTest(vip=vip), self.assertRaisesRegex(ValueError, 'additional address/VIP'):
                self.reboot([primary, vip])

    def test_active_cluster_services_cannot_be_automatically_rebooted(self):
        with self.assertRaisesRegex(ValueError, 'live services'):
            self.reboot([{'family': 'inet', 'local': '192.168.1.101'}], services='inactive\nactive\n')

    def test_embedded_old_module_needs_deliberate_initramfs_migration(self):
        with self.assertRaisesRegex(ValueError, 'embedded'):
            self.reboot([{'family': 'inet', 'local': '192.168.1.101'}],
                        initramfs='usr/lib/modules/kernel/aic8800_sdio_wlan.ko\n')

    def test_only_unconfigured_node_can_use_explicit_activation(self):
        self.assertTrue(self.reboot([{'family': 'inet', 'local': '192.168.1.101'},
                                    {'family': 'inet6', 'local': 'fe80::1234'}])['reboot_safe'])


if __name__ == '__main__':
    unittest.main()
