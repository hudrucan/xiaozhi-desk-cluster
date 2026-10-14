#!/usr/bin/env python3
"""Show two status characters and a stable node number on the Desk FD6551."""

import argparse
from contextlib import closing
import fcntl
import importlib.machinery
import importlib.util
import ipaddress
import json
import logging
import math
import os
from pathlib import Path
import re
import signal
import subprocess
import threading
import time
from http.client import HTTPConnection, HTTPException


CONFIG_PATH = "/etc/xiaozhi-node-panel.json"
DRIVER_PATH = "/usr/libexec/deskbox-front-panel"
MAX_JSON_BYTES = 65536
GLYPHS = {"-": 0x40, "i": 0x04, "P": 0x73, "n": 0x54, "t": 0x78,
          "C": 0x39, "o": 0x5C, "d": 0x5E, "L": 0x38}
ROLE_INTERVAL_SECONDS = 1.5
STATUS_INTERVAL_SECONDS = 1.0
FRAME_INTERVAL_SECONDS = 0.05
DNS_PULSE_SECONDS = 0.15
DNS_PULSE_PERIOD_SECONDS = 0.5
DNS_EVENT_MAX_AGE_SECONDS = 0.75
LOGGER = logging.getLogger("xiaozhi-node-panel")


def read_json(path):
    with open(path, "rb") as stream:
        data = stream.read(MAX_JSON_BYTES + 1)
    if len(data) > MAX_JSON_BYTES:
        raise ValueError("panel JSON exceeds size limit")
    return json.loads(data)


def load_config(path):
    config = read_json(path)
    if not isinstance(config, dict):
        raise ValueError("panel configuration must be an object")
    number = config.get("node_number")
    if type(number) is not int or not 1 <= number <= 99:
        raise ValueError("panel node number must be 1..99")
    for name in ("management_interface", "cluster_interface"):
        if not isinstance(config.get(name), str) or not re.fullmatch(
            r"[A-Za-z0-9_.-]{1,15}", config[name]
        ):
            raise ValueError("invalid panel interface")
    if "worker_activity_path" in config and config["worker_activity_path"] != "/run/xiaozhi-worker/activity.json":
        raise ValueError("invalid worker activity path")
    path = config.get("ingress_state_file")
    if not isinstance(path, str) or not os.path.isabs(path):
        raise ValueError("panel ingress snapshot path must be absolute")
    if "mqtt_status_host" in config:
        if not isinstance(config["mqtt_status_host"], str):
            raise ValueError("invalid local gateway status address")
        host = ipaddress.IPv4Address(config["mqtt_status_host"])
        if host.is_unspecified or host.is_multicast:
            raise ValueError("invalid local gateway status address")
        port = config.get("mqtt_status_port")
        if type(port) is not int or not 1024 <= port <= 65535:
            raise ValueError("invalid local gateway status port")
        if not isinstance(config.get("gateway_id"), str) or not re.fullmatch(
            r"[A-Za-z0-9_.-]{1,128}", config["gateway_id"]
        ):
            raise ValueError("invalid local gateway identity")
    if "core_status_host" in config:
        if not isinstance(config["core_status_host"], str):
            raise ValueError("invalid local core status address")
        host = ipaddress.IPv4Address(config["core_status_host"])
        if host.is_unspecified or host.is_multicast:
            raise ValueError("invalid local core status address")
        port = config.get("core_status_port")
        if type(port) is not int or not 1024 <= port <= 65535:
            raise ValueError("invalid local core status port")
        if not isinstance(config.get("core_id"), str) or not re.fullmatch(
            r"[A-Za-z0-9_-]{1,128}", config["core_id"]):
            raise ValueError("invalid local core identity")
    for key, expected in (("dns_state_file", "/etc/desk-dns/vip.json"),
                          ("dns_activity_path", "/run/desk-dns/activity.json"),
                          ("vpn_state_file", "/etc/desk-vpn/public.json")):
        if key in config and config[key] != expected:
            raise ValueError("invalid panel metadata path")
    return config


def management_addresses(config):
    """One bounded local address snapshot for every role in a poll."""
    try:
        result = subprocess.run(
            ["/usr/sbin/ip", "-j", "-4", "address", "show", "dev",
             config["management_interface"]],
            capture_output=True,
            text=True,
            timeout=2,
            check=False,
        )
        if result.returncode != 0 or len(result.stdout) > MAX_JSON_BYTES:
            return []
        return json.loads(result.stdout)
    except (OSError, ValueError, TypeError, KeyError, subprocess.SubprocessError):
        return []


def local_vip_owner(config, state_key="ingress_state_file", interfaces=None):
    """Read applied VIP metadata and actual addresses; never query Cloud/HTTP."""
    try:
        state = read_json(config[state_key])
        vip = str(ipaddress.IPv4Address(state["vip"]))
        if interfaces is None:
            interfaces = management_addresses(config)
        return any(
            interface["ifname"] == config["management_interface"]
            and any(address.get("family") == "inet" and address.get("local") == vip
                    for address in interface["addr_info"])
            for interface in interfaces
        )
    except (OSError, ValueError, TypeError, KeyError, AttributeError,
            subprocess.SubprocessError):
        # Missing ingress, invalid metadata or a failed query must never claim VIP.
        return False


def mqtt_connection_active(config):
    """Show the local owner of an authenticated MQTT connection, including idle."""
    if "mqtt_status_host" not in config:
        return False
    try:
        # HTTPConnection neither follows redirects nor uses proxy environment variables.
        with closing(HTTPConnection(config["mqtt_status_host"],
                                    config["mqtt_status_port"], timeout=0.3)) as client:
            client.request("GET", "/status")
            response = client.getresponse()
            if response.status != 200:
                return False
            data = response.read(MAX_JSON_BYTES + 1)
        if len(data) > MAX_JSON_BYTES:
            return False
        status = json.loads(data)
        count = status.get("active_mqtt_connections")
        return (status.get("gateway_id") == config["gateway_id"]
                and status.get("status") == "ready"
                and type(count) is int and count > 0)
    except (OSError, ValueError, TypeError, KeyError, AttributeError, HTTPException):
        return False


def core_session_active(config):
    """Show Co only for an established session owned by this local core."""
    if "core_status_host" not in config:
        return False
    try:
        with closing(HTTPConnection(config["core_status_host"],
                                    config["core_status_port"], timeout=0.3)) as client:
            client.request("GET", "/status")
            response = client.getresponse()
            if response.status != 200:
                return False
            data = response.read(MAX_JSON_BYTES + 1)
        if len(data) > MAX_JSON_BYTES:
            return False
        status = json.loads(data)
        count = status.get("active_sessions")
        return (status.get("protocol") == "xiaozhi-core-transport-v1"
                and status.get("core_id") == config["core_id"]
                and status.get("status") == "ready"
                and type(count) is int and count > 0)
    except (OSError, ValueError, TypeError, KeyError, AttributeError, HTTPException):
        return False


def display_text(node_number, vip_owner, mqtt_active=False, core_active=False, phase=0, dns_owner=False, vpn_owner=False):
    roles = []
    if mqtt_active:
        roles.append("nt")
    if core_active:
        roles.append("Co")
    if vip_owner:
        roles.append("iP")
    if dns_owner:
        roles.append("dn")
    if vpn_owner:
        roles.append("tL")
    status = roles[phase % len(roles)] if roles else "--"
    return status + f"{node_number:02d}"


def segments_for(text, driver):
    return tuple(GLYPHS[value] if value in GLYPHS else driver.DIGITS[value]
                 for value in text)


def activity_icons(driver, *, asr_active=False, llm_active=False,
                   vlm_active=False, tts_active=False, blink_on=True):
    """Activity policy, independent of transport/VIP roles.

    Fresh local worker telemetry supplies compute activity independently.
    A blink-off frame must not show Pause while a job is still running.
    """
    if not (asr_active or llm_active or vlm_active or tts_active):
        return driver.ICON_PAUSE
    icons = driver.ICON_CLOCK if asr_active else 0
    if tts_active or ((llm_active or vlm_active) and blink_on):
        icons |= driver.ICON_PLAY
    return icons


def worker_activity(config):
    path = config.get("worker_activity_path")
    if path is None:
        return {"asr": 0, "llm": 0}  # Legacy panel configuration.
    try:
        value = read_json(path)
        boot = Path("/proc/sys/kernel/random/boot_id").read_text().strip()
        if (not isinstance(value, dict) or set(value) not in (
                    {"protocol", "worker_id", "instance", "boot_id", "monotonic", "asr", "llm"},
                    {"protocol", "worker_id", "instance", "boot_id", "monotonic", "asr", "llm", "tts"})
                or value["protocol"] != "xiaozhi-worker-activity-v1"
                or value["worker_id"] != config.get("core_id") or value["boot_id"] != boot
                or not isinstance(value["instance"], str) or not re.fullmatch('[0-9a-f]{32}', value["instance"])
                or type(value["monotonic"]) not in (float, int)
                or not 0 <= time.monotonic() - value["monotonic"] <= 3
                or type(value.get("tts", 0)) is not int or not 0 <= value.get("tts", 0) <= 1
                or any(type(value[k]) is not int or not 0 <= value[k] <= 2 for k in ("asr", "llm"))):
            return None
        return value
    except (OSError, ValueError, KeyError, TypeError):
        return None


def worker_icons(config, driver):
    activity = worker_activity(config)
    if activity is None:
        return 0  # Unknown/stale activity must not claim compute idle.
    return activity_icons(driver, asr_active=activity["asr"] > 0,
        llm_active=activity["llm"] > 0, tts_active=activity.get("tts", 0) > 0, blink_on=not activity["llm"] or int(time.monotonic() * 2) % 2 == 0)


def dns_activity(config, now):
    path = config.get("dns_activity_path")
    if path is None:
        return None
    try:
        value = read_json(path)
        boot = Path("/proc/sys/kernel/random/boot_id").read_text().strip()
        if (not isinstance(value, dict) or set(value) != {
                "protocol", "node_id", "instance", "boot_id", "monotonic", "queries", "last_query"}
                or value["protocol"] != "desk-dns-activity-v1"
                or value["node_id"] != config.get("core_id") or value["boot_id"] != boot
                or not isinstance(value["instance"], str) or not re.fullmatch('[0-9a-f]{32}', value["instance"])
                or type(value["monotonic"]) not in (float, int) or not math.isfinite(value["monotonic"])
                or not 0 <= now - value["monotonic"] <= 2
                or type(value["queries"]) is not int or not 0 <= value["queries"] < 2 ** 63):
            return None
        last = value["last_query"]
        if last is None:
            return value if value["queries"] == 0 else None
        if (type(last) not in (float, int) or not math.isfinite(last)
                or not 0 <= last <= value["monotonic"] or value["queries"] == 0):
            return None
        return value
    except (OSError, ValueError, KeyError, TypeError):
        return None


class DnsPulse:
    """Coalesce bursts; cap colon pulses at two per second without blocking."""
    def __init__(self):
        self.instance = None
        self.queries = 0
        self.pending = False
        self.on_until = 0.0
        self.next_allowed = 0.0

    def update(self, value, now):
        if value is None:
            self.pending = False
            self.on_until = 0.0
            return False
        changed = value["instance"] != self.instance or value["queries"] > self.queries
        self.instance, self.queries = value["instance"], value["queries"]
        recent = value["last_query"] is not None and 0 <= now - value["last_query"] <= DNS_EVENT_MAX_AGE_SECONDS
        if changed and recent:
            self.pending = True
        if not recent:
            self.pending = False
        if self.pending and now >= self.next_allowed:
            self.pending = False
            self.on_until = now + DNS_PULSE_SECONDS
            self.next_allowed = now + DNS_PULSE_PERIOD_SECONDS
        return now < self.on_until


def connectivity_icons(config, driver):
    icons = worker_icons(config, driver)
    if driver.carrier(config["management_interface"]):
        icons |= driver.ICON_WIFI
    if driver.carrier(config["cluster_interface"]):
        icons |= driver.ICON_LAN
    if driver.external_usb_present():
        icons |= driver.ICON_USB
    # Colon is composed separately from DNS metadata; compute icons keep their policy.
    return icons & ~driver.ICON_COLON


def load_driver():
    loader = importlib.machinery.SourceFileLoader("deskbox_fd6551", DRIVER_PATH)
    spec = importlib.util.spec_from_loader(loader.name, loader)
    driver = importlib.util.module_from_spec(spec)
    loader.exec_module(driver)
    # Loading the existing module does not call its clock main() or initialize GPIO.
    for name in ("initialize", "write_register", "DIGIT_ADDRS", "DIGITS",
                 "ICON_ADDR", "ICON_COLON", "ICON_CLOCK", "ICON_PLAY", "ICON_PAUSE", "ICON_WIFI",
                 "ICON_LAN", "ICON_USB", "carrier", "external_usb_present",
                 "LOCK_PATH", "GPIO_FDS"):
        if not hasattr(driver, name):
            raise ValueError("unsupported FD6551 driver API")
    if len(driver.DIGIT_ADDRS) != 4:
        raise ValueError("panel requires four digit addresses")
    return driver


def run(config, driver, placeholder=False):
    stopping = threading.Event()
    signal.signal(signal.SIGTERM, lambda *_: stopping.set())
    signal.signal(signal.SIGINT, lambda *_: stopping.set())
    os.makedirs(os.path.dirname(driver.LOCK_PATH), exist_ok=True)
    with open(driver.LOCK_PATH, "w", encoding="ascii") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        try:
            driver.initialize()
            previous_segments = None
            previous_icons = None
            previous_owner = None
            previous_mqtt = None
            previous_core = None
            previous_dns_owner = None
            dns_owner = vpn_owner = False
            previous_vpn_owner = None
            dns_pulse = DnsPulse()
            owner = mqtt_active = core_active = False
            icons = 0
            next_status_poll = 0.0
            rotation_started = 0.0
            previous_roles = None
            while not stopping.is_set():
                now = time.monotonic()
                if not placeholder and now >= next_status_poll:
                    addresses = management_addresses(config)
                    owner = local_vip_owner(config, interfaces=addresses)
                    dns_owner = local_vip_owner(config, "dns_state_file", addresses) if "dns_state_file" in config else False
                    vpn_owner = local_vip_owner(config, "vpn_state_file", addresses) if "vpn_state_file" in config else False
                    mqtt_active = mqtt_connection_active(config)
                    core_active = core_session_active(config)
                    icons = connectivity_icons(config, driver)
                    now = time.monotonic()
                    next_status_poll = now + STATUS_INTERVAL_SECONDS
                roles = (owner, mqtt_active, core_active, dns_owner, vpn_owner)
                if roles != previous_roles:
                    # Restart the sequence when ownership/activity changes.
                    rotation_started = now
                    previous_roles = roles
                phase = int((now - rotation_started) / ROLE_INTERVAL_SECONDS)
                segments = segments_for(display_text(
                    config["node_number"], owner, mqtt_active, core_active, phase, dns_owner, vpn_owner), driver)
                if segments != previous_segments:
                    for index, (address, value) in enumerate(zip(driver.DIGIT_ADDRS, segments)):
                        if previous_segments is None or value != previous_segments[index]:
                            driver.write_register(address, value)
                    previous_segments = segments
                if not placeholder:
                    icons = (icons & ~(driver.ICON_CLOCK | driver.ICON_PLAY | driver.ICON_PAUSE)) | worker_icons(config, driver)
                icons &= ~driver.ICON_COLON
                if not placeholder and dns_pulse.update(dns_activity(config, now), now):
                    icons |= driver.ICON_COLON
                if icons != previous_icons:
                    driver.write_register(driver.ICON_ADDR, icons)
                    previous_icons = icons
                if owner != previous_owner and not placeholder:
                    LOGGER.info("node=%02d local_vip_owner=%s", config["node_number"], owner)
                    previous_owner = owner
                if mqtt_active != previous_mqtt and not placeholder:
                    LOGGER.info("node=%02d mqtt_connection_active=%s", config["node_number"], mqtt_active)
                    previous_mqtt = mqtt_active
                if core_active != previous_core and not placeholder:
                    LOGGER.info("node=%02d core_session_active=%s", config["node_number"], core_active)
                    previous_core = core_active
                if dns_owner != previous_dns_owner and not placeholder:
                    LOGGER.info("node=%02d dns_vip_owner=%s", config["node_number"], dns_owner)
                    previous_dns_owner = dns_owner
                if vpn_owner != previous_vpn_owner and not placeholder:
                    LOGGER.info("node=%02d vpn_vip_owner=%s", config["node_number"], vpn_owner)
                    previous_vpn_owner = vpn_owner
                if placeholder:
                    break
                stopping.wait(FRAME_INTERVAL_SECONDS)
        finally:
            for descriptor in driver.GPIO_FDS.values():
                os.close(descriptor)
            driver.GPIO_FDS.clear()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--placeholder", action="store_true")
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(name)s: %(message)s")
    if os.geteuid() != 0:
        raise SystemExit("xiaozhi-node-panel must run as root")
    try:
        config = load_config(CONFIG_PATH)
        driver = load_driver()
        run(config, driver, args.placeholder)
    except (OSError, ValueError, ImportError, AttributeError):
        raise SystemExit("panel startup failed; check configuration, FD6551 driver and GPIO lock")


if __name__ == "__main__":
    main()
