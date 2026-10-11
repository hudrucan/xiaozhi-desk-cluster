#!/usr/bin/python3
"""Announce the applied VIP to DNS peers only while this node owns it."""
import argparse
import ipaddress
import json
import re
import socket
import struct
import subprocess
import sys
from pathlib import Path

MAX_BYTES = 16384


def read_json(path):
    with Path(path).open('rb') as source:
        data = source.read(MAX_BYTES + 1)
    if len(data) > MAX_BYTES:
        raise ValueError('Oversize metadata')
    return json.loads(data)


def mac_bytes(value):
    if not isinstance(value, str) or not re.fullmatch(r'(?:[0-9a-fA-F]{2}:){5}[0-9a-fA-F]{2}', value):
        raise ValueError('Invalid MAC')
    result = bytes.fromhex(value.replace(':', ''))
    if result == bytes(6) or result[0] & 1:
        raise ValueError('MAC must be unicast')
    return result


def announcement_frames(config, snapshot, links):
    """Pure ownership gate: no local VIP means no ARP frames, even on standby."""
    interface = config['interface']
    if not isinstance(interface, str) or not re.fullmatch(r'[A-Za-z0-9_.-]{1,15}', interface) or interface == 'lo':
        raise ValueError('Invalid interface')
    vip = ipaddress.IPv4Address(snapshot['vip'])
    local = ipaddress.IPv4Address(config['local_ip'])
    peers = config['peers']
    if not isinstance(peers, list) or len(peers) != 2:
        raise ValueError('Require two DNS peers')
    targets = [(ipaddress.IPv4Address(peer['ip']), mac_bytes(peer['mac'])) for peer in peers]
    if len({local, vip, *(ip for ip, _ in targets)}) != 4 or len({mac for _, mac in targets}) != 2:
        raise ValueError('Addresses must be distinct')
    matches = [link for link in links if link.get('ifname') == interface]
    if len(matches) != 1:
        raise ValueError('Missing interface')
    link = matches[0]
    local_addresses = [addr for addr in link.get('addr_info', []) if addr.get('family') == 'inet']
    management = [addr for addr in local_addresses if addr.get('local') == str(local)]
    if len(management) != 1:
        raise ValueError('Missing management address')
    network = ipaddress.IPv4Network(f"{local}/{management[0]['prefixlen']}", strict=False)
    if any(ip not in network or ip in (network.network_address, network.broadcast_address) for ip in (vip, *(ip for ip, _ in targets))):
        raise ValueError('Invalid management subnet')
    source = mac_bytes(link['address'])
    if source in {mac for _, mac in targets}:
        raise ValueError('Duplicate MAC')
    if 'UP' not in link.get('flags', []) or not any(addr.get('local') == str(vip) for addr in local_addresses):
        return []
    frames = []
    for target_ip, target_mac in targets:
        ethernet = target_mac + source + b'\x08\x06'
        arp = struct.pack('!HHBBH6s4s6s4s', 1, 0x0800, 6, 4, 2, source, vip.packed, target_mac, target_ip.packed)
        frames.append((ethernet + arp).ljust(60, b'\x00'))
    return frames


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--config', required=True)
    parser.add_argument('--snapshot', required=True)
    args = parser.parse_args()
    try:
        config, snapshot = read_json(args.config), read_json(args.snapshot)
        result = subprocess.run(['/usr/sbin/ip', '-j', 'address', 'show', 'dev', config['interface']],
                                capture_output=True, check=True, timeout=1)
        if len(result.stdout) > MAX_BYTES:
            raise ValueError('Oversize address metadata')
        links = json.loads(result.stdout)
        frames = announcement_frames(config, snapshot, links)
        if not frames:
            return 0
        with socket.socket(socket.AF_PACKET, socket.SOCK_RAW, socket.htons(0x0806)) as sender:
            sender.settimeout(1)
            sender.bind((config['interface'], 0))
            # Re-check ownership after opening the socket, immediately before sending.
            current = subprocess.run(['/usr/sbin/ip', '-j', 'address', 'show', 'dev', config['interface']],
                                     capture_output=True, check=True, timeout=1)
            if len(current.stdout) > MAX_BYTES:
                raise ValueError('Oversize address metadata')
            frames = announcement_frames(config, read_json(args.snapshot), json.loads(current.stdout))
            for frame in frames:
                sender.send(frame)
        return 0
    except (OSError, ValueError, KeyError, TypeError, subprocess.SubprocessError):
        print('DNS peer ARP announcement unavailable', file=sys.stderr)
        return 1


if __name__ == '__main__':
    sys.exit(main())
