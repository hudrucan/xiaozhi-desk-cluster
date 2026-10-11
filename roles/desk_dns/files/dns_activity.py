#!/usr/bin/env python3
"""Passive, bounded DNS backend activity metadata; never stores query contents."""
import argparse
import ctypes
import ipaddress
import json
import os
from pathlib import Path
import re
import signal
import socket
import struct
import threading
import time
import uuid


MAX_PACKET = 4096
MAX_MESSAGE = 4096
MAX_FLOWS = 32
FLOW_TTL = 2.0
INTERNAL_SUFFIX = '.desk-dns.test'
PUBLISH_INTERVAL = 0.25


def real_query(data):
    if not 12 <= len(data) <= MAX_MESSAGE:
        return False
    _, flags, questions, _, _, _ = struct.unpack_from('!6H', data)
    if flags & 0xF800 or questions != 1:
        return False
    labels, pos, size = [], 12, 0
    while pos < len(data):
        length = data[pos]
        pos += 1
        if length == 0:
            if pos + 4 > len(data) or not labels:
                return False
            name = b'.'.join(labels).lower()
            return name != b'desk-dns.test' and not name.endswith(INTERNAL_SUFFIX.encode('ascii'))
        if length > 63 or pos + length > len(data):
            return False
        size += length + 1
        if size > 254:
            return False
        labels.append(data[pos:pos + length])
        pos += length
    return False


class BackendQueries:
    def __init__(self, address, port, allowed_sources=None):
        self.address = ipaddress.IPv4Address(address).packed
        self.port = port
        self.allowed_sources = {ipaddress.IPv4Address(ip).packed for ip in allowed_sources} if allowed_sources else None
        self.flows = {}

    def feed(self, packet, now):
        # AF_PACKET/SOCK_DGRAM removes the link header; this is an IPv4 packet.
        if len(packet) < 20 or packet[0] >> 4 != 4:
            return 0
        ihl = (packet[0] & 15) * 4
        total = struct.unpack_from('!H', packet, 2)[0]
        if (ihl < 20 or not ihl <= total <= len(packet) or packet[16:20] != self.address
                or struct.unpack_from('!H', packet, 6)[0] & 0x3FFF):
            return 0  # Fragmented queries are intentionally not reassembled here.
        if self.allowed_sources is not None and packet[12:16] not in self.allowed_sources:
            return 0
        payload = packet[ihl:total]
        if len(payload) < 8 or struct.unpack_from('!H', payload, 2)[0] != self.port:
            return 0
        if packet[9] == 17:
            size = struct.unpack_from('!H', payload, 4)[0]
            return int(8 <= size <= len(payload) and real_query(payload[8:size]))
        if packet[9] != 6 or len(payload) < 20:
            return 0
        header = (payload[12] >> 4) * 4
        if not 20 <= header <= len(payload):
            return 0
        key = (packet[12:16], payload[:2])
        for old in list(self.flows):
            if now - self.flows[old][2] > FLOW_TTL:
                del self.flows[old]
        flags = payload[13]
        if flags & 0x07:  # SYN, FIN or RST starts/ends a flow; no DNS on those frames.
            self.flows.pop(key, None)
            return 0
        data = payload[header:]
        if not data:
            return 0
        sequence = struct.unpack_from('!I', payload, 4)[0]
        previous = self.flows.get(key)
        if previous:
            expected, buffered, _ = previous
            if expected is None:
                self.flows[key] = (None, b'', now)
                return 0
            overlap = (expected - sequence) & 0xFFFFFFFF
            if overlap >= 0x80000000:  # Gap/out of order: don't invent delivered queries.
                self.flows[key] = (None, b'', now)
                return 0
            if overlap >= len(data):
                return 0  # Retransmission already counted.
            data = data[overlap:]
        else:
            buffered = b''
            if len(self.flows) >= MAX_FLOWS:
                del self.flows[min(self.flows, key=lambda k: self.flows[k][2])]
        expected = (sequence + len(payload[header:])) & 0xFFFFFFFF
        buffered += data
        if len(buffered) > MAX_MESSAGE + 2:
            self.flows[key] = (None, b'', now)
            return 0
        count = 0
        while len(buffered) >= 2:
            size = struct.unpack_from('!H', buffered)[0]
            if not 12 <= size <= MAX_MESSAGE:
                self.flows[key] = (None, b'', now)
                return 0
            if len(buffered) < size + 2:
                break
            count += int(real_query(buffered[2:size + 2]))
            buffered = buffered[size + 2:]
        self.flows[key] = (expected, buffered, now)
        return count


def filter_program(address, port):
    # Classic BPF over the IPv4 header. Restrict the COPY delivered to this socket
    # to UDP/TCP destined for this node's private AGH port, including loopback.
    return [
        (0x30, 0, 0, 0), (0x54, 0, 0, 0xF0), (0x15, 0, 12, 0x40),
        (0x30, 0, 0, 9), (0x15, 1, 0, 17), (0x15, 0, 9, 6),
        (0x20, 0, 0, 16), (0x15, 0, 7, int(ipaddress.IPv4Address(address))),
        (0x28, 0, 0, 6), (0x54, 0, 0, 0x3FFF), (0x15, 0, 4, 0),
        (0xB1, 0, 0, 0), (0x48, 0, 0, 2), (0x15, 0, 1, port),
        (0x06, 0, 0, MAX_PACKET), (0x06, 0, 0, 0),
    ]


def attach_filter(sock, address, port):
    class Instruction(ctypes.Structure):
        _fields_ = [('code', ctypes.c_ushort), ('jt', ctypes.c_ubyte),
                    ('jf', ctypes.c_ubyte), ('k', ctypes.c_uint32)]

    class Program(ctypes.Structure):
        _fields_ = [('length', ctypes.c_ushort), ('instructions', ctypes.POINTER(Instruction))]

    code = filter_program(address, port)
    instructions = (Instruction * len(code))(*(Instruction(*row) for row in code))
    program = Program(len(code), instructions)
    libc = ctypes.CDLL(None, use_errno=True)
    libc.setsockopt.argtypes = [ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_void_p, ctypes.c_uint32]
    libc.setsockopt.restype = ctypes.c_int
    if libc.setsockopt(sock.fileno(), socket.SOL_SOCKET, 26,
                       ctypes.byref(program), ctypes.sizeof(program)) != 0:
        raise OSError(ctypes.get_errno(), 'DNS socket filter unavailable')
    sock.setsockopt(socket.SOL_SOCKET, 44, 1)  # SO_LOCK_FILTER: cannot widen this capture filter.


def publish(path, node_id, instance, boot_id, count, last_query, now):
    value = {'protocol': 'desk-dns-activity-v1', 'node_id': node_id, 'instance': instance,
             'boot_id': boot_id, 'monotonic': now, 'queries': count, 'last_query': last_query}
    temporary = path.with_suffix('.tmp')
    with temporary.open('w', encoding='ascii') as target:
        os.chmod(temporary, 0o600)
        json.dump(value, target, separators=(',', ':'))
    os.replace(temporary, path)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--config', default='/etc/desk-dns/public.json')
    parser.add_argument('--node', required=True)
    args = parser.parse_args()
    if not re.fullmatch(r'deskb[123]x', args.node):
        raise ValueError('Invalid DNS node identity')
    with Path(args.config).open('rb') as source:
        raw = source.read(65537)
    if len(raw) > 65536:
        raise ValueError('Oversize DNS metadata')
    config = json.loads(raw)
    address = str(ipaddress.IPv4Address(config['nodes'][args.node]['private']))
    port = config['backend_port']
    if type(port) is not int or port != 5354:
        raise ValueError('Unsupported DNS backend port')
    decoder = BackendQueries(address, port, [node['private'] for node in config['nodes'].values()] + ['127.0.0.1'])
    output = Path('/run/desk-dns/activity.json')
    instance = uuid.uuid4().hex
    boot_id = Path('/proc/sys/kernel/random/boot_id').read_text().strip()
    stopping = threading.Event()
    signal.signal(signal.SIGTERM, lambda *_: stopping.set())
    signal.signal(signal.SIGINT, lambda *_: stopping.set())
    count, last_query, next_publish = 0, None, 0.0
    with socket.socket(socket.AF_PACKET, socket.SOCK_DGRAM, socket.htons(0x0800)) as capture:
        attach_filter(capture, address, port)
        capture.setsockopt(263, 23, 1)  # SOL_PACKET, PACKET_IGNORE_OUTGOING.
        capture.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, 65536)
        capture.settimeout(0.05)
        while not stopping.is_set():
            try:
                packet, metadata = capture.recvfrom(MAX_PACKET)
                now = time.monotonic()
                # Also gate outgoing copies in user space (including loopback).
                matched = decoder.feed(packet, now) if metadata[2] != 4 else 0
                if matched:
                    count = min(count + matched, 2 ** 63 - 1)
                    last_query = now
            except socket.timeout:
                now = time.monotonic()
            if now >= next_publish:
                publish(output, args.node, instance, boot_id, count, last_query, now)
                next_publish = now + PUBLISH_INTERVAL
    output.unlink(missing_ok=True)


if __name__ == '__main__':
    try:
        main()
    except (OSError, ValueError, TypeError, KeyError):
        raise SystemExit('DNS activity observer unavailable; DNS forwarding is unaffected')
