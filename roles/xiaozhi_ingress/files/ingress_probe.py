#!/usr/bin/python3
"""Bounded, credential-free ingress topology and HAProxy readiness checks."""
import argparse
import csv
import io
import ipaddress
import json
import re
import socket
import sys
import time

MAX_BYTES = 1024 * 1024


def topology(addresses, management, vip, private_interface):
    management = ipaddress.IPv4Address(management)
    vip = ipaddress.IPv4Address(vip)
    matches = [(link, addr) for link in addresses for addr in link.get("addr_info", [])
               if addr.get("family") == "inet" and addr.get("local") == str(management)]
    if len(matches) != 1:
        raise ValueError("Management address must belong to exactly one interface")
    link, addr = matches[0]
    interface = link["ifname"]
    if not re.fullmatch(r"[A-Za-z0-9_.-]{1,15}", interface) or interface in ("lo", private_interface):
        raise ValueError("Management interface must be distinct from private/loopback interfaces")
    if "UP" not in link.get("flags", []):
        raise ValueError("Management interface must be administratively up")
    network = ipaddress.IPv4Network(f"{management}/{addr['prefixlen']}", strict=False)
    if vip not in network or vip in (management, network.network_address, network.broadcast_address):
        raise ValueError("Cloud VIP must be a separate usable address on the management subnet")
    return {"interface": interface, "prefix": network.prefixlen, "network": str(network)}


def stats_ready(payload, expected, require_all=False,
                frontend="xiaozhi_settings", backend="xiaozhi_control_planes"):
    # HAProxy 'show stat' is CSV with a '# ' prefix on its header.
    rows = list(csv.DictReader(io.StringIO(payload.removeprefix("# "))))
    frontends = [r for r in rows if r.get("pxname") == frontend and r.get("svname") == "FRONTEND"]
    backends = [r for r in rows if r.get("pxname") == backend]
    servers = [r for r in backends if r.get("svname") in expected]
    if len(frontends) != 1 or frontends[0].get("status") != "OPEN":
        return False
    if len(servers) != len(expected) or {r["svname"] for r in servers} != set(expected):
        return False
    healthy = [r for r in servers if r.get("status") == "UP"]
    return len(healthy) == len(expected) if require_all else bool(healthy)


def read_stats(path):
    deadline = time.monotonic() + 2
    data = bytearray()
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as client:
        client.settimeout(2)
        client.connect(path)
        client.sendall(b"show stat\n")
        client.shutdown(socket.SHUT_WR)
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise ValueError("HAProxy stats timeout")
            client.settimeout(remaining)
            chunk = client.recv(min(65536, MAX_BYTES + 1 - len(data)))
            if not chunk:
                break
            data.extend(chunk)
            if len(data) > MAX_BYTES:
                raise ValueError("HAProxy stats too large")
    return data.decode("utf-8")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    network = sub.add_parser("topology")
    network.add_argument("--management", required=True)
    network.add_argument("--vip", required=True)
    network.add_argument("--private-interface", required=True)
    stats = sub.add_parser("stats")
    stats.add_argument("--socket", required=True)
    stats.add_argument("--nodes", nargs="+", required=True)
    stats.add_argument("--all", action="store_true")
    stats.add_argument("--frontend", default="xiaozhi_settings")
    stats.add_argument("--backend", default="xiaozhi_control_planes")
    args = parser.parse_args()
    try:
        if args.command == "topology":
            source = sys.stdin.read(MAX_BYTES + 1)
            if len(source) > MAX_BYTES:
                raise ValueError("Address inventory too large")
            print(json.dumps(topology(json.loads(source), args.management, args.vip, args.private_interface)))
            return 0
        return 0 if stats_ready(read_stats(args.socket), args.nodes, args.all,
                               args.frontend, args.backend) else 1
    except (OSError, ValueError, KeyError, TypeError, csv.Error):
        # Do not print arbitrary subprocess/socket data or exceptions.
        print("Ingress probe failed: invalid topology or HAProxy is not ready", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
