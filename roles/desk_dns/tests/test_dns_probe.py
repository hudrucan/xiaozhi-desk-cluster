import importlib.util
import contextlib
import io
import json
import sys
from pathlib import Path
import socket
import struct
import unittest
from unittest.mock import patch


ROLE = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('desk_dns_probe', ROLE / 'files/dns_probe.py')
probe = importlib.util.module_from_spec(spec)
spec.loader.exec_module(probe)
arp_spec = importlib.util.spec_from_file_location('desk_dns_arp', ROLE / 'files/peer_arp.py')
arp = importlib.util.module_from_spec(arp_spec)
arp_spec.loader.exec_module(arp)


def config():
    return {'nodes': {f'deskb{i}x': {'private': f'10.10.10.{10+i}',
                                  'management': f'192.168.1.{100+i}'} for i in range(1, 4)},
            'vip': '192.168.1.250', 'vrid': 52, 'backend_port': 5354,
            'test_port': 5353, 'web_port': 3001, 'upstreams': ['1.1.1.1']}


def response(query, ip='127.0.0.1'):
    return (struct.pack('!6H', struct.unpack_from('!H', query)[0], 0x8180, 1, 1, 0, 0)
            + query[12:] + b'\xc0\x0c' + struct.pack('!HHIH', 1, 1, 30, 4)
            + socket.inet_aton(ip))


class ProbeTests(unittest.TestCase):
    def test_reserved_vip_and_complete_membership(self):
        self.assertEqual(probe.validate(config())['vip'], '192.168.1.250')
        for bad in ('192.168.1.101', '192.168.1.1', '192.168.1.255', '10.10.10.250'):
            value = config()
            value['vip'] = bad
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                probe.validate(value)
        value = config()
        del value['nodes']['deskb3x']
        with self.assertRaises(ValueError):
            probe.validate(value)

    def test_topology_reads_mac_from_link_when_ipv4_output_omits_it(self):
        from types import SimpleNamespace
        def command(*args):
            outputs = {
                ('ip', '-j', '-4', 'route', 'get', '192.168.1.250'): '[{"dev":"lo"}]',
                ('ip', '-j', '-4', 'addr', 'show'): '[{"ifname":"lo","addr_info":[{"family":"inet","local":"127.0.0.1","prefixlen":8}]},{"ifname":"wlan0","addr_info":[{"family":"inet","local":"192.168.1.101","prefixlen":24}]}]',
                ('ip', '-j', 'link', 'show', 'dev', 'wlan0'): '[{"address":"aa:bb:cc:dd:ee:ff"}]',
                ('/usr/sbin/keepalived', '--version'): '',
                ('systemctl', 'show', 'desk-adguard', '--property=MainPID'): 'MainPID=0',
                ('systemctl', 'show', 'desk-dnsdist', '--property=MainPID'): 'MainPID=0',
                ('systemctl', 'show', 'desk-dns-ui', '--property=MainPID'): 'MainPID=0',
                ('ss', '-H', '-lnutp'): '',
                ('apt-cache', 'policy', 'dnsdist'): 'Installed: (none)\nCandidate: 1.7.3-2',
                ('apt-get', '--simulate', '--no-install-recommends', 'install', 'dnsdist=1.7.3-2'): 'Inst dnsdist (1.7.3-2)',
            }
            return outputs[args]
        def read(path):
            return '1' if str(path).startswith('/proc/') else '{"vip":"192.168.1.186"}'
        with patch.object(probe, 'run', side_effect=command), \
             patch.object(probe.subprocess, 'run', return_value=SimpleNamespace(returncode=3)), \
             patch.object(probe.Path, 'rglob', return_value=[]), \
             patch.object(probe.Path, 'exists', return_value=False), \
             patch.object(probe.Path, 'read_text', autospec=True, side_effect=read):
            result=probe.topology(config(), 'deskb1x')
        self.assertEqual(result['mac'], 'aa:bb:cc:dd:ee:ff')
        self.assertEqual(result['dnsdist_version'], '1.7.3-2')
        self.assertEqual(result['interface'], 'wlan0')

    def test_private_or_named_upstreams_and_shared_vrid_rejected(self):
        for upstream in ('10.10.10.11', '192.168.1.1', 'resolver.example'):
            value = config()
            value['upstreams'] = [upstream]
            with self.subTest(upstream=upstream), self.assertRaises(ValueError):
                probe.validate(value)
        value = config()
        value['vrid'] = 51
        with self.assertRaises(ValueError):
            probe.validate(value)

    def test_compressed_dns_answer_and_question_contract(self):
        query = probe.request(probe.HEALTH, 123)
        self.assertEqual(probe.answers(response(query), query), ['127.0.0.1'])
        wrong_question = probe.request(probe.IDENTITY, 123)
        with self.assertRaises(ValueError):
            probe.answers(response(wrong_question), query)

    def test_truncated_oversize_pointer_mismatch_and_rcode_rejected(self):
        query = probe.request(probe.HEALTH, 123)
        good = response(query)
        invalid_pointer = good[:len(query)] + b'\xff\xff' + good[len(query)+2:]
        for data in (b'', good[:-1], good + bytes(4096), invalid_pointer,
                     b'\x00\x00' + good[2:], good[:3] + b'\x83' + good[4:]):
            with self.subTest(size=len(data)), self.assertRaises(ValueError):
                probe.answers(data, query)

    def test_query_name_is_bounded(self):
        for name in ('', 'a' * 64, '.'.join(['a' * 63] * 5), 'bad name.test'):
            with self.subTest(name=name), self.assertRaises(ValueError):
                probe.request(name, 1)

    def test_direct_backend_must_return_its_own_identity(self):
        def fake(host, port, name, tcp):
            return [dict(zip((probe.HEALTH, probe.BLOCKED, probe.IDENTITY),
                             ('127.0.0.1', '0.0.0.0', '10.10.10.12')))[name]]
        with patch.object(probe, 'query', side_effect=fake), self.assertRaises(ValueError):
            probe.probe(config(), '10.10.10.11', 5354, direct=True)

    def test_balanced_probe_checks_both_transports_and_all_nodes(self):
        counts = {False: 0, True: 0}
        def fake(host, port, name, tcp):
            if name == probe.HEALTH:
                return ['127.0.0.1']
            if name == probe.BLOCKED:
                return ['0.0.0.0']
            counts[tcp] += 1
            return [f'10.10.10.{11 + counts[tcp] % 3}']
        with patch.object(probe, 'query', side_effect=fake):
            probe.probe(config(), '192.168.1.250', 53, balanced=True)
        self.assertEqual(counts, {False: 12, True: 12})

    def test_runtime_ready_allows_one_backend_but_deployment_does_not(self):
        def fake(host, port, name, tcp):
            return [{probe.HEALTH: '127.0.0.1', probe.BLOCKED: '0.0.0.0',
                     probe.IDENTITY: '10.10.10.12'}[name]]
        with patch.object(probe, 'query', side_effect=fake):
            probe.probe(config(), '192.168.1.102', 5353)
            with self.assertRaises(ValueError):
                probe.probe(config(), '192.168.1.102', 5353, balanced=True)

    def test_election_reuses_existing_mapping_after_partial_rollout(self):
        existing = {'deskb1x': 110, 'deskb2x': 120, 'deskb3x': 100}
        with patch.object(probe.secrets, 'SystemRandom') as draw:
            self.assertEqual(probe.election([None, existing, None], config()['nodes']), existing)
            draw.assert_not_called()

    def test_election_draws_once_without_sorted_node_preference(self):
        with patch.object(probe.secrets, 'SystemRandom') as draw:
            draw.return_value.sample.return_value = [100, 120, 110]
            value = probe.election([None, None, None], config()['nodes'])
        self.assertEqual(value['deskb2x'], 120)
        self.assertEqual(sorted(value.values()), [100, 110, 120])

    def test_conflicting_or_invalid_persisted_election_rejected(self):
        one = {'deskb1x': 110, 'deskb2x': 120, 'deskb3x': 100}
        two = {'deskb1x': 120, 'deskb2x': 110, 'deskb3x': 100}
        for maps in ([one, two], [{'deskb1x': 110}], [{**one, 'deskb3x': 110}]):
            with self.subTest(maps=maps), self.assertRaises(ValueError):
                probe.election(maps, config()['nodes'])

    def test_xiaozhi_penalty_always_loses_to_healthy_non_owner(self):
        ranks = (100, 110, 120)
        self.assertLess(max(ranks) - 50, min(ranks))
        links = [{'addr_info': [{'family': 'inet', 'local': '192.168.1.186'}]}]
        self.assertTrue(probe.owns(links, '192.168.1.186'))
        self.assertFalse(probe.owns(links, '192.168.1.250'))

    def test_arp_announces_dns_only_on_current_owner(self):
        settings = {'interface': 'wlan0', 'local_ip': '192.168.1.101', 'peers': [
            {'ip': '192.168.1.102', 'mac': '02:00:00:00:00:02'},
            {'ip': '192.168.1.103', 'mac': '02:00:00:00:00:03'}]}
        links = [{'ifname': 'wlan0', 'flags': ['UP'], 'address': '02:00:00:00:00:01',
                  'addr_info': [{'family': 'inet', 'local': '192.168.1.101', 'prefixlen': 24}]}]
        self.assertEqual(arp.announcement_frames(settings, {'vip': '192.168.1.250'}, links), [])
        links[0]['addr_info'].append({'family': 'inet', 'local': '192.168.1.250', 'prefixlen': 24})
        frames = arp.announcement_frames(settings, {'vip': '192.168.1.250'}, links)
        self.assertEqual(len(frames), 2)
        self.assertTrue(all(socket.inet_aton('192.168.1.250') in frame for frame in frames))
        self.assertTrue(all(socket.inet_aton('192.168.1.186') not in frame for frame in frames))


    def test_guard_rejects_process_config_or_resolver_drift(self):
        previous = {'units': {'unit': {'MainPID': '123'}}, 'digests': {'file': 'before'}, 'nonlocal_bind': '1'}
        value = {**config(), 'snapshot': {'protected': previous}}
        for current in ({**previous, 'units': {}}, {**previous, 'digests': {}},
                        {**previous, 'nonlocal_bind': '0'}):
            with patch.object(sys, 'argv', ['probe', 'guard', '--config', '-']), \
                 patch.object(sys, 'stdin', io.StringIO(json.dumps(value))), \
                 patch.object(probe, 'protected_state', return_value=current), \
                 self.assertRaises(ValueError):
                probe.main()
        with patch.object(sys, 'argv', ['probe', 'guard', '--config', '-']), \
             patch.object(sys, 'stdin', io.StringIO(json.dumps(value))), \
             patch.object(probe, 'protected_state', return_value=previous), \
             contextlib.redirect_stdout(io.StringIO()):
            probe.main()

    def test_preference_follows_current_xiaozhi_vip_without_pinning_initial_ip(self):
        for local, status in (('192.168.1.187', 1), ('192.168.1.186', 0)):
            links = [{'addr_info': [{'family': 'inet', 'local': local}]}]
            with patch.object(sys, 'argv', ['probe', 'prefer', '--config', '-']), \
                 patch.object(sys, 'stdin', io.StringIO(json.dumps(config()))), \
                 patch.object(probe.Path, 'read_text', return_value=json.dumps({'vip': '192.168.1.187'})), \
                 patch.object(probe, 'run', return_value=json.dumps(links)), \
                 self.assertRaises(SystemExit) as stopped:
                probe.main()
            self.assertEqual(stopped.exception.code, status)

    def test_tcp_query_accepts_fragmented_response(self):
        query = probe.request(probe.HEALTH, 123)
        packet = response(query)
        size = struct.pack('!H', len(packet))
        with patch.object(probe.socket, 'socket') as factory, \
             patch.object(probe.secrets, 'randbelow', return_value=123):
            sock = factory.return_value.__enter__.return_value
            sock.recv.side_effect = [size[:1], size[1:], packet[:5], packet[5:]]
            self.assertEqual(probe.query('192.168.1.250', 53, probe.HEALTH, True), ['127.0.0.1'])
            sock.sendall.assert_called_once_with(struct.pack('!H', len(query)) + query)

    def test_tcp_query_rejects_oversize_and_incomplete_reply(self):
        for reads in ([struct.pack('!H', 4097)], [b'']):
            with patch.object(probe.socket, 'socket') as factory:
                factory.return_value.__enter__.return_value.recv.side_effect = reads
                with self.assertRaises(ValueError):
                    probe.query('192.168.1.250', 53, probe.HEALTH, True)

if __name__ == '__main__':
    unittest.main()
