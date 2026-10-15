#!/usr/bin/python3
"""Read-only checks for the pre-provisioned MPI3501 on Raspberry Pi Zero W."""
import argparse
import fcntl
import hashlib
import json
import os
from pathlib import Path
import stat
import struct


def require(condition, message):
    if not condition:
        raise ValueError(message)


def cells(node, name):
    data = (node / name).read_bytes()
    return struct.unpack('>' + 'I' * (len(data) // 4), data)


def check(args):
    digest = hashlib.sha256(Path(args.overlay).read_bytes()).hexdigest()
    require(len(args.overlay_sha256) == 64 and digest == args.overlay_sha256,
            'Overlay hash differs or is unset; audit the overlay before deploying')
    for device, driver, compatible in (
        ('spi0.0', 'fb_ili9486', b'ilitek,ili9486'),
        ('spi0.1', 'ads7846', b'ti,ads7846'),
    ):
        spi = Path('/sys/bus/spi/devices') / device
        require((spi / 'driver').resolve().name == driver,
                f'{device} must be bound to {driver}')
        require(compatible in (spi / 'of_node/compatible').read_bytes().split(b'\0'),
                f'{device} has an unexpected device-tree compatible')

    lcd = Path('/sys/bus/spi/devices/spi0.0/of_node')
    touch = Path('/sys/bus/spi/devices/spi0.1/of_node')
    for name, expected in (
        ('rotate', 270), ('spi-max-frequency', 16000000),
        ('regwidth', 16), ('buswidth', 8), ('fps', 30), ('txbuflen', 32768),
    ):
        require(cells(lcd, name) == (expected,), f'Unexpected LCD {name}')
    require(cells(lcd, 'reset-gpios')[1:] == (25, 1), 'Unexpected LCD reset GPIO')
    require(cells(lcd, 'dc-gpios')[1:] == (24, 0), 'Unexpected LCD DC GPIO')
    require(cells(touch, 'spi-max-frequency') == (2000000,), 'Unexpected touch SPI clock')
    require(cells(touch, 'interrupts') == (17, 2), 'Unexpected touch IRQ')
    require(cells(touch, 'pendown-gpio')[1:] == (17, 1), 'Unexpected touch pendown GPIO')

    framebuffer = Path(args.framebuffer).resolve(strict=True)
    require(stat.S_ISCHR(framebuffer.stat().st_mode), 'Framebuffer is not a character device')
    sysfb = Path('/sys/class/graphics') / framebuffer.name
    require((sysfb / 'name').read_text().strip() == 'fb_ili9486', 'Wrong framebuffer driver')
    require(int((sysfb / 'stride').read_text()) == 960, 'Expected framebuffer stride 960')
    fd = os.open(framebuffer, os.O_RDONLY)
    try:
        v = struct.unpack('40I', fcntl.ioctl(fd, 0x4600, bytes(160)))
    finally:
        os.close(fd)
    require((v[:6], v[6], v[8:10], v[11:13], v[14:16]) ==
            ((480, 320, 480, 320, 0, 0), 16, (11, 5), (5, 6), (0, 5)),
            'Expected 480x320 RGB565 without viewport offsets')
    event = Path(args.touch).resolve(strict=True)
    require(stat.S_ISCHR(event.stat().st_mode), 'Touch input is not a character device')
    input_path = Path('/sys/class/input') / event.name / 'device'
    require(input_path.resolve() == (Path('/sys/bus/spi/devices/spi0.1/input') /
            input_path.resolve().name).resolve(), 'Touch input does not belong to spi0.1')
    require((input_path / 'name').read_text().strip() == 'ADS7846 Touchscreen',
            'Wrong touch input device')
    print(json.dumps({'framebuffer': str(framebuffer), 'touch': str(event),
                      'overlay_sha256': digest, 'status': 'PASS'}))


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    for option in ('framebuffer', 'touch', 'overlay', 'overlay-sha256'):
        parser.add_argument('--' + option, required=True)
    try:
        check(parser.parse_args())
    except (OSError, ValueError, struct.error) as error:
        raise SystemExit(f'MPI3501 preflight failed: {error}')
