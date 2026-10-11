"""Offline DNS activity fixtures: no sockets, service changes or real queries."""
import ast
import importlib.util
import ipaddress
import json
from pathlib import Path
import stat
import struct
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import Mock, patch


ROLE = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('dns_activity', ROLE / 'files/dns_activity.py')
activity = importlib.util.module_from_spec(spec)
spec.loader.exec_module(activity)


def question(name='example.com', flags=0x0100, count=1):
    labels = b''.join(bytes([len(part)]) + part.encode('ascii') for part in name.split('.'))
    return struct.pack('!6H', 1, flags, count, 0, 0, 0) + labels + b'\0' + struct.pack('!HH', 1, 1)


def ipv4(data, protocol=17, source='10.10.10.11', dest='10.10.10.12', fragment=0, options=b''):
    header = bytearray(20 + len(options))
    header[0] = 0x40 | (len(header) // 4)
    struct.pack_into('!HHH', header, 2, len(header) + len(data), 1, fragment)
    header[8:10] = bytes((64, protocol))
    header[12:16] = ipaddress.IPv4Address(source).packed
    header[16:20] = ipaddress.IPv4Address(dest).packed
    header[20:] = options
    return bytes(header) + data


def udp(data=None, port=5354, **kwargs):
    if data is None:
        data = question()
    return ipv4(struct.pack('!HHHH', 12345, port, 8 + len(data), 0) + data, **kwargs)


def tcp(data=b'', sequence=100, flags=0x18, sport=12345, port=5354, **kwargs):
    header = struct.pack('!HHIIBBHHH', sport, port, sequence, 0, 0x50, flags, 65535, 0, 0)
    return ipv4(header + data, protocol=6, **kwargs)


def run_filter(packet):
    """Interpret only the classic BPF instructions used by this socket filter."""
    program = activity.filter_program('10.10.10.12', 5354)
    accumulator = index = pc = 0
    for _ in range(64):
        code, yes, no, value = program[pc]
        if code in (0x30, 0x20, 0x28, 0x48, 0xB1):
            offset = value + (index if code == 0x48 else 0)
            width = 4 if code == 0x20 else 2 if code in (0x28, 0x48) else 1
            if offset + width > len(packet):
                return 0
            loaded = int.from_bytes(packet[offset:offset + width], 'big')
            if code == 0xB1:
                index = (loaded & 15) * 4
            else:
                accumulator = loaded
        elif code == 0x54:
            accumulator &= value
        elif code == 0x15:
            pc += yes if accumulator == value else no
        elif code == 0x06:
            return value
        else:
            raise AssertionError('Unknown BPF instruction')
        pc += 1
    raise AssertionError('Non-terminating BPF filter')


class ActivityTests(unittest.TestCase):
    def setUp(self):
        self.decoder = activity.BackendQueries('10.10.10.12', 5354,
                                             ['10.10.10.11', '10.10.10.12', '10.10.10.13', '127.0.0.1'])

    def test_normal_query_and_all_internal_probes(self):
        self.assertTrue(activity.real_query(question()))
        for name in ('health.desk-dns.test', 'who.desk-dns.test', 'blocked.desk-dns.test',
                     'DESK-DNS.TEST', 'Anything.Desk-Dns.Test'):
            with self.subTest(name=name):
                self.assertFalse(activity.real_query(question(name)))

    def test_malformed_responses_opcodes_and_oversize_are_ignored(self):
        values = [b'', b'x' * 11, b'x' * 4097, question(flags=0x8100),
                  question(flags=0x0900), question(count=0), question(count=2),
                  question()[:-3], question('a' * 64), question('.'),
                  struct.pack('!6H', 1, 0x100, 1, 0, 0, 0) + b'\xc0\x0c\0\1\0\1']
        for value in values:
            with self.subTest(size=len(value)):
                self.assertFalse(activity.real_query(value))

    def test_udp_only_local_backend_and_allowed_frontends(self):
        self.assertEqual(self.decoder.feed(udp(), 1), 1)
        self.assertEqual(self.decoder.feed(udp(source='127.0.0.1'), 1), 1)
        for packet in (udp(port=53), udp(dest='10.10.10.13'), udp(source='192.168.1.128'),
                       udp(fragment=0x2000), udp(fragment=1), udp()[:-1], b''):
            self.assertEqual(self.decoder.feed(packet, 1), 0)
        packet = bytearray(udp())
        packet[0] = 0x60
        self.assertEqual(self.decoder.feed(bytes(packet), 1), 0)
        self.assertEqual(self.decoder.feed(udp(question('health.desk-dns.test')), 1), 0)

    def test_tcp_segmentation_retransmission_and_pipelining(self):
        message = question()
        framed = struct.pack('!H', len(message)) + message
        self.assertEqual(self.decoder.feed(tcp(framed[:7]), 1), 0)
        self.assertEqual(self.decoder.feed(tcp(framed[:7]), 1.1), 0)
        self.assertEqual(self.decoder.feed(tcp(framed[7:], sequence=107), 1.2), 1)
        self.assertEqual(self.decoder.feed(tcp(framed[7:], sequence=107), 1.3), 0)
        self.assertEqual(self.decoder.feed(tcp(framed * 2, sequence=100 + len(framed)), 1.4), 2)

    def test_tcp_health_filter_and_connection_reset(self):
        message = question('who.desk-dns.test')
        framed = struct.pack('!H', len(message)) + message
        self.assertEqual(self.decoder.feed(tcp(framed), 1), 0)
        self.assertEqual(self.decoder.feed(tcp(flags=0x04), 1.1), 0)
        self.assertFalse(self.decoder.flows)
        real = question()
        self.assertEqual(self.decoder.feed(tcp(struct.pack('!H', len(real)) + real), 1.2), 1)

    def test_tcp_gaps_fail_closed_and_new_syn_recovers(self):
        message = question()
        framed = struct.pack('!H', len(message)) + message
        self.assertEqual(self.decoder.feed(tcp(framed[:2]), 1), 0)
        self.assertEqual(self.decoder.feed(tcp(framed, sequence=110), 1.1), 0)
        self.assertEqual(self.decoder.feed(tcp(framed, sequence=150), 1.2), 0)
        self.assertEqual(self.decoder.feed(tcp(flags=0x02), 1.3), 0)
        self.assertEqual(self.decoder.feed(tcp(framed), 1.4), 1)

    def test_tcp_flow_buffers_and_tracking_are_bounded(self):
        for port in range(10000, 10100):
            self.decoder.feed(tcp(b'\0', sport=port), 1)
        self.assertEqual(len(self.decoder.flows), activity.MAX_FLOWS)
        self.decoder.feed(tcp(b'\0', sport=12345), 4)
        self.assertEqual(len(self.decoder.flows), 1)
        self.assertEqual(self.decoder.feed(tcp(b'x' * 4099, sport=12345, sequence=101), 4.1), 0)
        self.assertTrue(all(len(buffer) <= activity.MAX_MESSAGE + 2
                            for _, buffer, _ in self.decoder.flows.values()))

    def test_tcp_sequence_wrap_is_supported(self):
        message = question()
        framed = struct.pack('!H', len(message)) + message
        self.assertEqual(self.decoder.feed(tcp(framed[:7], sequence=2**32 - 4), 1), 0)
        self.assertEqual(self.decoder.feed(tcp(framed[7:], sequence=3), 1.1), 1)

    def test_bpf_limits_copy_to_backend_udp_tcp_and_handles_ip_options(self):
        for packet in (udp(), udp(options=b'\1' * 4), tcp(), tcp(options=b'\1' * 4)):
            self.assertEqual(run_filter(packet), activity.MAX_PACKET)
        for packet in (udp(dest='10.10.10.13'), udp(port=53), tcp(port=53),
                       udp(fragment=0x2000), udp(fragment=1), udp(protocol=1), b''):
            self.assertEqual(run_filter(packet), 0)

    def test_filter_is_attached_then_locked_without_real_socket(self):
        sock = Mock()
        sock.fileno.return_value = 123
        libc = Mock()
        libc.setsockopt.return_value = 0
        with patch.object(activity.ctypes, 'CDLL', return_value=libc):
            activity.attach_filter(sock, '10.10.10.12', 5354)
        self.assertEqual(libc.setsockopt.call_args.args[:3], (123, activity.socket.SOL_SOCKET, 26))
        sock.setsockopt.assert_called_once_with(activity.socket.SOL_SOCKET, 44, 1)

    def test_filter_failure_does_not_enable_unfiltered_collector(self):
        libc = Mock()
        libc.setsockopt.return_value = -1
        sock = Mock()
        with patch.object(activity.ctypes, 'CDLL', return_value=libc):
            with self.assertRaises(OSError):
                activity.attach_filter(sock, '10.10.10.12', 5354)
        sock.setsockopt.assert_not_called()

    def test_publisher_has_only_private_atomic_activity_metadata(self):
        with TemporaryDirectory() as directory:
            path = Path(directory) / 'activity.json'
            activity.publish(path, 'deskb2x', 'a' * 32, 'boot', 2, 9.5, 10)
            self.assertEqual(json.loads(path.read_text()), {
                'protocol': 'desk-dns-activity-v1', 'node_id': 'deskb2x',
                'instance': 'a' * 32, 'boot_id': 'boot', 'queries': 2,
                'last_query': 9.5, 'monotonic': 10})
            self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)
            self.assertFalse(path.with_suffix('.tmp').exists())
            self.assertNotIn('example.com', path.read_text())

    def test_query_metadata_drives_panel_and_probe_does_not_retrigger(self):
        panel_path = ROLE.parent / 'xiaozhi_panel/files/node_panel.py'
        panel_spec = importlib.util.spec_from_file_location('dns_panel_integration', panel_path)
        panel = importlib.util.module_from_spec(panel_spec)
        panel_spec.loader.exec_module(panel)
        with TemporaryDirectory() as directory:
            path = Path(directory) / 'activity.json'
            config = {'dns_activity_path': str(path), 'core_id': 'deskb2x'}
            pulse = panel.DnsPulse()
            count = self.decoder.feed(udp(), 10)
            activity.publish(path, 'deskb2x', 'a' * 32, 'boot', count, 10, 10)
            with patch.object(panel.Path, 'read_text', return_value='boot'):
                self.assertTrue(pulse.update(panel.dns_activity(config, 10), 10))
                self.assertFalse(pulse.update(panel.dns_activity(config, 10.2), 10.2))
                count += self.decoder.feed(udp(question('health.desk-dns.test')), 10.5)
                activity.publish(path, 'deskb2x', 'a' * 32, 'boot', count, 10, 10.5)
                self.assertFalse(pulse.update(panel.dns_activity(config, 10.5), 10.5))

    def test_collector_receives_only_and_transport_units_do_not_depend_on_it(self):
        tree = ast.parse((ROLE / 'files/dns_activity.py').read_text())
        observer = next(node for node in tree.body if isinstance(node, ast.FunctionDef) and node.name == 'observe_http_counter')
        tree.body.remove(observer)
        # Packet observation remains receive-only. The DoH observer can only ask
        # the local UNIX stats socket for its fixed numeric request counter.
        calls = [node for node in ast.walk(observer) if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)]
        socket_calls = [node for node in calls if node.func.attr == 'socket']
        self.assertEqual(len(socket_calls), 1)
        self.assertEqual(socket_calls[0].args[0].attr, 'AF_UNIX')
        connects = [node for node in calls if node.func.attr == 'connect']
        self.assertEqual([node.args[0].value for node in connects], ['/run/desk-dns-backend/admin.sock'])
        sends = [node for node in calls if node.func.attr == 'sendall']
        self.assertEqual([node.args[0].value for node in sends], [b'show stat\n'])
        methods = {node.func.attr for node in ast.walk(tree)
                   if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)}
        self.assertIn('recvfrom', methods)
        self.assertFalse(methods & {'send', 'sendto', 'sendall', 'connect', 'bind', 'listen'})
        unit = (ROLE / 'templates/desk-dns-activity.service.j2').read_text()
        self.assertIn('CPUQuota=5%', unit)
        self.assertIn('MemoryMax=32M', unit)
        self.assertIn('CapabilityBoundingSet=CAP_NET_RAW', unit)
        self.assertNotIn('Requires=', unit)
        for name in ('desk-adguard', 'desk-dnsdist', 'desk-dns-vip'):
            self.assertNotIn('desk-dns-activity', (ROLE / f'templates/{name}.service.j2').read_text())


if __name__ == '__main__':
    unittest.main()


class NativeCounterTests(unittest.TestCase):
    def test_native_stats_counts_only_real_dns_client_backend(self):
        raw = b'# pxname,svname,stot,\nprivate_dns_backend,FRONTEND,200,\nadguard_doh,BACKEND,100,\nadguard_clients,local,42,\nadguard_clients,BACKEND,42,\n'
        self.assertEqual(activity.http_client_count(raw), 42)
        self.assertEqual(activity.http_client_count(b'# pxname,svname,stot,\nadguard_doh,BACKEND,100,\n'), 0)

    def test_invalid_native_counter_is_rejected(self):
        for value in ('-1', 'invalid'):
            with self.assertRaises(ValueError):
                activity.http_client_count(('# pxname,svname,stot,\nadguard_clients,BACKEND,' + value + ',\n').encode())
