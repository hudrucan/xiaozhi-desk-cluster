"""Offline ownership, packet and bounds checks; no raw sockets or live hosts."""
import importlib.util
import struct
import unittest
from pathlib import Path

spec = importlib.util.spec_from_file_location('peer_arp', Path(__file__).parents[1] / 'files/peer_arp.py')
arp = importlib.util.module_from_spec(spec)
spec.loader.exec_module(arp)


class PeerARPTests(unittest.TestCase):
    def setUp(self):
        self.config = {'interface': 'wlan0', 'local_ip': '192.168.1.103', 'peers': [
            {'ip': '192.168.1.101', 'mac': '02:00:00:00:00:01'},
            {'ip': '192.168.1.102', 'mac': '02:00:00:00:00:02'}]}
        self.snapshot = {'vip': '192.168.1.186'}
        self.link = {'ifname': 'wlan0', 'address': '02:00:00:00:00:03', 'flags': ['UP'],
                     'addr_info': [{'family': 'inet', 'local': '192.168.1.103', 'prefixlen': 24}]}

    def frames(self):
        return arp.announcement_frames(self.config, self.snapshot, [self.link])

    def own(self):
        self.link['addr_info'].append({'family': 'inet', 'local': self.snapshot['vip'], 'prefixlen': 24})

    def test_standby_sends_nothing(self):
        self.assertEqual(self.frames(), [])

    def test_owner_announces_legitimate_vip_to_each_peer_only(self):
        self.own()
        frames = self.frames()
        self.assertEqual(len(frames), 2)
        for frame, peer in zip(frames, self.config['peers']):
            self.assertEqual(len(frame), 60)
            self.assertEqual(frame[:6], arp.mac_bytes(peer['mac']))
            self.assertEqual(frame[6:12], arp.mac_bytes(self.link['address']))
            payload = struct.unpack('!HHBBH6s4s6s4s', frame[14:42])
            self.assertEqual(payload[:5], (1, 0x800, 6, 4, 2))
            self.assertEqual(payload[6], bytes([192, 168, 1, 186]))
            self.assertEqual(payload[8], bytes(map(int, peer['ip'].split('.'))))

    def test_losing_ownership_or_link_stops_announcements(self):
        self.own()
        self.assertEqual(len(self.frames()), 2)
        self.link['addr_info'].pop()
        self.assertEqual(self.frames(), [])
        self.own()
        self.link['flags'] = []
        self.assertEqual(self.frames(), [])

    def test_vip_is_taken_from_applied_snapshot(self):
        self.snapshot['vip'] = '192.168.1.190'
        self.own()
        self.assertEqual(self.frames()[0][28:32], bytes([192, 168, 1, 190]))

    def test_invalid_or_unsafe_metadata_fails_closed(self):
        for vip in ('10.10.10.11', '192.168.1.255', '192.168.1.101', 'bad'):
            self.snapshot['vip'] = vip
            with self.assertRaises(ValueError):
                self.frames()
        for mac in ('ff:ff:ff:ff:ff:ff', '00:00:00:00:00:00', 'bad'):
            with self.assertRaises(ValueError):
                arp.mac_bytes(mac)

    def test_missing_management_or_wrong_interface_fails_closed(self):
        self.link['ifname'] = 'end0'
        with self.assertRaises(ValueError):
            self.frames()


if __name__ == '__main__':
    unittest.main()
