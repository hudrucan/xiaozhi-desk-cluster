#!/usr/bin/env python3
"""Show two status characters and a stable node number on the Desk FD6551."""

import argparse
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


CONFIG_PATH = "/etc/xiaozhi-node-panel.json"
DRIVER_PATH = "/usr/libexec/deskbox-front-panel"
MAX_JSON_BYTES = 65536
GLYPHS = {"-": 0x40, "i": 0x04, "P": 0x73}
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


def display_text(node_number, vip_owner):
    # Keep both status cells reserved; provider scheduling is not implemented yet.
    return ("iP" if vip_owner else "--") + f"{node_number:02d}"


def segments_for(text, driver):
    return tuple(GLYPHS[value] if value in GLYPHS else driver.DIGITS[value]
                 for value in text)


def connectivity_icons(config, driver):
    icons = 0
    if driver.carrier(config["management_interface"]):
        icons |= driver.ICON_WIFI
    if driver.carrier(config["cluster_interface"]):
        icons |= driver.ICON_LAN
    if driver.external_usb_present():
        icons |= driver.ICON_USB
    # Never enable the clock/colon bits, including when the clock is synchronized.
    return icons & ~(driver.ICON_COLON | driver.ICON_CLOCK)


def load_driver():
    loader = importlib.machinery.SourceFileLoader("deskbox_fd6551", DRIVER_PATH)
    spec = importlib.util.spec_from_loader(loader.name, loader)
    driver = importlib.util.module_from_spec(spec)
    loader.exec_module(driver)
    # Loading the existing module does not call its clock main() or initialize GPIO.
    for name in ("initialize", "write_register", "DIGIT_ADDRS", "DIGITS",
                 "ICON_ADDR", "ICON_COLON", "ICON_CLOCK", "ICON_WIFI",
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
            while not stopping.is_set():
                owner = False if placeholder else local_vip_owner(config)
                segments = segments_for(display_text(config["node_number"], owner), driver)
                icons = 0 if placeholder else connectivity_icons(config, driver)
                if segments != previous_segments:
                    for address, value in zip(driver.DIGIT_ADDRS, segments):
                        driver.write_register(address, value)
                    previous_segments = segments
                if icons != previous_icons:
                    driver.write_register(driver.ICON_ADDR, icons)
                    previous_icons = icons
                if owner != previous_owner and not placeholder:
                    LOGGER.info("node=%02d local_vip_owner=%s", config["node_number"], owner)
                    previous_owner = owner
                if placeholder:
                    break
                stopping.wait(1)
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
