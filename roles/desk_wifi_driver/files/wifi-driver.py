#!/usr/bin/python3
"""Read-only checks for the exact Desk Box VIP-capable WLAN module."""
import argparse
import hashlib
import ipaddress
import json
import platform
import subprocess
import sys
from pathlib import Path

KERNEL = '6.1.174-rk35xx-ophub'
MODULE_SHA = '84438521b1ea754c6b43a37276e567b266781290ac7444c0b49f9e3b6a4b0682'
STOCK_SHA = '81cfe6c319eb5e2ce4b05963913978285e36011048f4053adab0c5fd58ec8a98'
SOURCE_SHA = '4884d3c6948f1fd461aff4441a147b5460d2551b'
SRCVERSION = '3F7CE2E19BE16E3704587B4'
STOCK_SRCVERSION = 'DF1C5AEDFA29A5642F83418'
TREE = Path('/lib/modules') / KERNEL
STOCK = TREE / 'kernel/drivers/net/wireless/aicsemi/aic8800/sdio/fdrv/aic8800_sdio_wlan.ko'
OVERRIDE = TREE / 'updates/deskbox/aic8800_sdio_wlan.ko'


def require(condition, message):
    if not condition:
        raise ValueError(message)


def run(*argv, check=True):
    return subprocess.run(argv, capture_output=True, text=True, timeout=30, check=check)


def sha(path):
    digest = hashlib.sha256()
    with path.open('rb') as stream:
        for data in iter(lambda: stream.read(1024 * 1024), b''):
            digest.update(data)
    return digest.hexdigest()


def validate_manifest(value):
    require(value.get('kernel_release') == KERNEL and value.get('module_sha256') == MODULE_SHA,
            'Unreviewed kernel or module pin')
    require(value.get('source_repository') == 'unifreq/linux-6.1.y-rockchip'
            and value.get('source_commit') == SOURCE_SHA and value.get('arp_offload') is False,
            'Unreviewed driver source or ARP offload setting')
    require(value.get('vermagic') == KERNEL + ' SMP mod_unload aarch64'
            and value.get('driver_version') == '6.4.3.0'
            and set(value.get('depends', '').split(',')) == {'aic8800_sdio_bsp', 'cfg80211'},
            'Unexpected module ABI, driver version or dependencies')


def artifact(path):
    require(sha(path) == MODULE_SHA, 'Module checksum mismatch')
    for field, expected in [('vermagic', KERNEL + ' SMP mod_unload aarch64'),
                            ('srcversion', SRCVERSION), ('version', '6.4.3.0')]:
        require(run('/usr/sbin/modinfo', '-F', field, str(path)).stdout.strip() == expected,
                'Module metadata mismatch: ' + field)
    dependencies = run('/usr/sbin/modinfo', '-F', 'depends', str(path)).stdout.strip()
    require(set(dependencies.split(',')) == {'aic8800_sdio_bsp', 'cfg80211'},
            'Unexpected module dependencies')
    return {'artifact_valid': True}


def inspect(management):
    ipaddress.IPv4Address(management)
    require(platform.machine() in ('aarch64', 'arm64') and platform.release() == KERNEL,
            'Only the reviewed Desk Box ARM64 kernel is supported; build a matching module before changing kernel')
    release = Path('/etc/armbian-release').read_text()
    require('BOARD_NAME="RK.Deskbox"' in release and 'BOARDFAMILY="rk3528"' in release,
            'Unsupported image/board')
    require(sha(STOCK) == STOCK_SHA, 'Stock module changed; refusing to replace or remove it')
    override_sha = sha(OVERRIDE) if OVERRIDE.exists() else None
    require(override_sha in (None, MODULE_SHA), 'Unrecognized existing WLAN override; preserve and review it')
    links = json.loads(run('/usr/sbin/ip', '-j', 'addr', 'show').stdout)
    interfaces = [link for link in links if any(a.get('local') == management for a in link.get('addr_info', []))]
    require(len(interfaces) == 1, 'Management address must identify exactly one interface')
    interface = interfaces[0]['ifname']
    require((Path('/sys/class/net') / interface / 'device/driver').resolve().name == 'aicwf_sdio',
            'Management interface is not using the reviewed AIC8800 SDIO driver')
    runtime = Path('/sys/module/aic8800_sdio_wlan/srcversion').read_text().strip()
    require(runtime in (STOCK_SRCVERSION, SRCVERSION), 'Unrecognized running WLAN module')
    with Path('/proc/kallsyms').open() as symbols:
        offload = any(len(parts := line.split()) >= 3 and 'arpoffload' in parts[2] for line in symbols)
    selected = run('/usr/sbin/modinfo', '-F', 'filename', 'aic8800_sdio_wlan').stdout.strip()
    return {'kernel': KERNEL, 'interface': interface, 'override_sha256': override_sha,
            'selected_override': Path(selected).resolve() == OVERRIDE.resolve(),
            'runtime_srcversion': runtime, 'arp_offload_symbols': offload,
            'runtime_ready': runtime == SRCVERSION and not offload}


def verify(management):
    result = inspect(management)
    require(result['override_sha256'] == MODULE_SHA and result['selected_override'] and result['runtime_ready'],
            'Fixed WLAN module must be selected AND running before VIP activation; run wifi-driver.yml and activate with a controlled reboot')
    return result


def can_reboot(management):
    result = inspect(management)
    links = json.loads(run('/usr/sbin/ip', '-j', 'addr', 'show', 'dev', result['interface']).stdout)
    addresses = links[0].get('addr_info', [])
    require(not any((a.get('family') == 'inet' and a.get('local') != management)
                    or a.get('local') == 'fe80::250' for a in addresses),
            'Node has an additional address/VIP; use the existing controlled HA reboot workflow')
    services = run('/usr/bin/systemctl', 'is-active', 'keepalived', 'desk-dns-vip',
                   'xiaozhi-worker', 'xiaozhi-core', 'xiaozhi-mqtt-gateway', check=False)
    require('active' not in services.stdout.splitlines(),
            'Automatic activation is only for an unconfigured node; live services need the controlled HA reboot workflow')
    initramfs = run('/usr/bin/lsinitramfs', '/boot/initrd.img-' + KERNEL).stdout
    require(not any(Path(line).name == 'aic8800_sdio_wlan.ko' for line in initramfs.splitlines()),
            'WLAN is embedded in this initramfs; review/regenerate it before activation')
    return {'reboot_safe': True, 'interface': result['interface']}


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('action', choices=['inspect', 'artifact', 'verify', 'can-reboot'])
    parser.add_argument('--management')
    parser.add_argument('--path', type=Path)
    args = parser.parse_args()
    try:
        validate_manifest(json.load(sys.stdin))
        result = artifact(args.path) if args.action == 'artifact' else {
            'inspect': inspect, 'verify': verify, 'can-reboot': can_reboot}[args.action](args.management)
        print(json.dumps(result))
    except (ValueError, OSError, subprocess.SubprocessError) as error:
        parser.exit(1, str(error) + '\n')


if __name__ == '__main__':
    main()
