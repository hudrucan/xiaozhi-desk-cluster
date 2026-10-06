"""Validate public gateway deployment inputs without importing the runtime."""

import ipaddress
import json
import re
import sys
from urllib.parse import urlsplit


def validate(config):
    if not isinstance(config, dict):
        raise ValueError
    if not re.fullmatch(r"[0-9a-f]{40}", config["revision"]):
        raise ValueError
    if not re.fullmatch(r"24\.\d+\.\d+", config["node_version"]):
        raise ValueError
    if not re.fullmatch(r"[0-9a-f]{64}", config["node_sha256"]):
        raise ValueError
    if not re.fullmatch(r"[A-Za-z0-9_.-]{1,128}", config["gateway_id"]):
        raise ValueError
    if not isinstance(config["management_ip"], str):
        raise ValueError
    address = ipaddress.IPv4Address(config["management_ip"])
    if address.is_unspecified or address.is_multicast or address.is_loopback:
        raise ValueError
    interfaces = config["interfaces"]
    if sum(info.get("family") == "inet" and info.get("local") == str(address)
           for interface in interfaces for info in interface.get("addr_info", [])) != 1:
        raise ValueError
    if not isinstance(config["cluster_ip"], str):
        raise ValueError
    private = ipaddress.IPv4Address(config["cluster_ip"])
    if (not private.is_private or private.is_unspecified or private.is_multicast
            or private.is_loopback or private == address):
        raise ValueError
    if sum(info.get("family") == "inet" and info.get("local") == str(private)
           for interface in interfaces for info in interface.get("addr_info", [])) != 1:
        raise ValueError
    for key in ("mqtt_port", "udp_port", "http_port"):
        port = config[key]
        if type(port) is not int or not 1024 <= port <= 65535:
            raise ValueError
    if config["http_port"] == config["mqtt_port"]:
        raise ValueError
    servers = config["chat_servers"]
    if not isinstance(servers, list) or not 1 <= len(servers) <= 3:
        raise ValueError
    if len(set(servers)) != len(servers):
        raise ValueError
    targets = []
    for server in servers:
        if not isinstance(server, str) or any(char.isspace() or ord(char) < 32 or ord(char) == 127 for char in server):
            raise ValueError
        url = urlsplit(server)
        if (url.scheme not in ("ws", "wss") or not url.hostname
                or url.username is not None or url.password is not None
                or "?" in server or "#" in server or url.netloc.endswith(":")):
            raise ValueError
        host = url.hostname
        try:
            backend_ip = ipaddress.ip_address(host)
        except ValueError:
            if (len(host) > 253 or re.fullmatch(r"(?:[0-9]+|0[xX][0-9a-fA-F]+)", host.rstrip(".").split(".")[-1])
                    or any(not re.fullmatch(r"[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?", part)
                           for part in host.rstrip(".").split("."))):
                raise ValueError
        else:
            if backend_ip.is_unspecified or backend_ip.is_multicast or "%" in host:
                raise ValueError
        port = url.port if url.port is not None else (443 if url.scheme == "wss" else 80)
        if not 1 <= port <= 65535:
            raise ValueError
        targets.append({"host": host, "port": port})
    return {"backend_targets": targets}


def main():
    try:
        payload = sys.stdin.buffer.read(65537)
        if len(payload) > 65536:
            raise ValueError
        result = validate(json.loads(payload))
    except (ValueError, TypeError, KeyError, AttributeError):
        print("Invalid gateway deployment inputs: require a reviewed full SHA, pinned Node 24 checksum, distinct local management/private IPv4s, unprivileged ports and 1-3 credential/query-free remote WS/WSS backends", file=sys.stderr)
        raise SystemExit(1)
    print(json.dumps(result, separators=(",", ":")))


if __name__ == "__main__":
    main()
