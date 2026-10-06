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
import os
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
          "C": 0x39, "o": 0x5C}
ROLE_INTERVAL_SECONDS = 1.5
STATUS_INTERVAL_SECONDS = 1.0
FRAME_INTERVAL_SECONDS = 0.25
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
    return config


def local_vip_owner(config):
    """Read applied ingress state and actual addresses; never query Cloud/HTTP."""
    try:
        state = read_json(config["ingress_state_file"])
        vip = str(ipaddress.IPv4Address(state["vip"]))
        result = subprocess.run(
            ["/usr/sbin/ip", "-j", "-4", "address", "show", "dev",
             config["management_interface"]],
            capture_output=True,
            text=True,
            timeout=2,
            check=False,
        )
        if result.returncode != 0 or len(result.stdout) > MAX_JSON_BYTES:
            return False
        interfaces = json.loads(result.stdout)
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


def display_text(node_number, vip_owner, mqtt_active=False, core_active=False, phase=0):
    roles = []
    if mqtt_active:
        roles.append("nt")
    if core_active:
        roles.append("Co")
    if vip_owner:
        roles.append("iP")
    status = roles[phase % len(roles)] if roles else "--"
    return status + f"{node_number:02d}"


def segments_for(text, driver):
    return tuple(GLYPHS[value] if value in GLYPHS else driver.DIGITS[value]
                 for value in text)


def activity_icons(driver, *, asr_active=False, llm_active=False,
                   vlm_active=False, tts_active=False, blink_on=True):
    """Activity policy, independent of transport/VIP roles.

    Worker telemetry is not wired yet: the live caller uses idle defaults.
    A blink-off frame must not show Pause while a job is still running.
    """
    if not (asr_active or llm_active or vlm_active or tts_active):
        return driver.ICON_PAUSE
    icons = driver.ICON_CLOCK if asr_active else 0
    if tts_active or ((llm_active or vlm_active) and blink_on):
        icons |= driver.ICON_PLAY
    return icons


def connectivity_icons(config, driver):
    # No worker activity source exists yet; every live node is compute-idle.
    icons = activity_icons(driver)
    if driver.carrier(config["management_interface"]):
        icons |= driver.ICON_WIFI
    if driver.carrier(config["cluster_interface"]):
        icons |= driver.ICON_LAN
    if driver.external_usb_present():
        icons |= driver.ICON_USB
    # Colon remains off; activity icons never depend on transport roles.
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
            owner = mqtt_active = core_active = False
            icons = 0
            next_status_poll = 0.0
            rotation_started = 0.0
            previous_roles = None
            while not stopping.is_set():
                now = time.monotonic()
                if not placeholder and now >= next_status_poll:
                    owner = local_vip_owner(config)
                    mqtt_active = mqtt_connection_active(config)
                    core_active = core_session_active(config)
                    icons = connectivity_icons(config, driver)
                    now = time.monotonic()
                    next_status_poll = now + STATUS_INTERVAL_SECONDS
                roles = (owner, mqtt_active, core_active)
                if roles != previous_roles:
                    # Restart the sequence when ownership/activity changes.
                    rotation_started = now
                    previous_roles = roles
                phase = int((now - rotation_started) / ROLE_INTERVAL_SECONDS)
                segments = segments_for(display_text(
                    config["node_number"], owner, mqtt_active, core_active, phase), driver)
                if segments != previous_segments:
                    for index, (address, value) in enumerate(zip(driver.DIGIT_ADDRS, segments)):
                        if previous_segments is None or value != previous_segments[index]:
                            driver.write_register(address, value)
                    previous_segments = segments
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
