# AIC8800 ARP offload and movable VIPs

The reviewed AIC8800 SDIO driver enables firmware ARP offload using the IPv4
address in a DHCP ACK. It does not synchronize secondary addresses such as the
management and DNS VIPs. ARP offload is an intentional feature, but this
implementation is incompatible with our movable VIPs: clients can resolve a
node's primary address while failing to learn the VIP's MAC.

Source: `unifreq/linux-6.1.y-rockchip` commit
`4884d3c6948f1fd461aff4441a147b5460d2551b`,
`drivers/net/wireless/aicsemi/aic8800/sdio/fdrv/rwnx_rx.c:1501`.
`arpoffload_proc()` calls `rwnx_send_arpoffload_en_req()` with `dhcph->yiaddr`.
The driver Makefile enables `CONFIG_ARP_OFFLOAD=y`. This proves the host-side
limitation; it does not establish that the closed Wi-Fi firmware is generally
broken or that every ARP issue has this cause.

## Reviewed module

The source-image repository's `build-aic8800-vip.yml` workflow builds only the
WLAN module with `CONFIG_ARP_OFFLOAD=n`, using pinned source and kernel headers.
No kernel image, BSP module, firmware blob or radio tuning is changed.

- Kernel: `6.1.174-rk35xx-ophub`, ARM64.
- Driver: `6.4.3.0`.
- Module SHA256: `84438521b1ea754c6b43a37276e567b266781290ac7444c0b49f9e3b6a4b0682`.
- Runtime srcversion: `3F7CE2E19BE16E3704587B4`.
- Original stock SHA256: `81cfe6c319eb5e2ce4b05963913978285e36011048f4053adab0c5fd58ec8a98`.
- Durable release: [deskbox-aic8800-vip-6.1.174-v1](https://github.com/hudrucan/xiaozhi-desk-box/releases/tag/deskbox-aic8800-vip-6.1.174-v1).

The release retains the original CI module, manifest and checksum. It is not a
replacement Armbian image and is not marked as the latest image release.
Ansible checks the exact SHA256 and module ABI before installation; changing
kernel requires a reviewed module build and coordinated update of the pins.

## Ansible ownership

`playbooks/cluster/wifi-driver.yml` runs the role serially. `site.yml` includes it after initial
network/access setup and before cluster services. Standalone ingress and DNS VIP
activation also require the fixed module to be selected **and running**.

```sh
ansible-playbook -i inventory.ini playbooks/cluster/wifi-driver.yml --check
ansible-playbook -i inventory.ini playbooks/cluster/wifi-driver.yml
```

The role validates board/kernel/stock checksum, adopts a matching deployed
module into its cache or downloads the pinned release, verifies ABI, and owns
`/lib/modules/6.1.174-rk35xx-ophub/updates/deskbox/aic8800_sdio_wlan.ko` and
`/etc/desk-wifi-driver.json`. It preserves the original module under `kernel/`.
Matching reruns perform no reboot, module reload or service restart.

For a newly flashed, unconfigured box, explicitly allow serial activation:

```sh
ansible-playbook -i inventory.ini playbooks/cluster/wifi-driver.yml --limit deskb1x \
  -e desk_wifi_driver_reboot=true
```

Automatic activation is refused if the node owns an additional address/VIP,
runs cluster services, or its initramfs embeds WLAN. This mode is for fresh
provisioning. A live cluster needing activation must use a controlled, confirmed
HA reboot; the role stops before VIP activation until the right module is
running. Hot-unloading this SDIO driver is deliberately avoided.

Keeping the original image is supported: after flashing, apply this role before
activating either VIP. Existing overrides persist through normal reboots, but
not through flashing or replacing the kernel. The existing Armbian image-build
workflow remains unchanged.

## Validation and rollback

The original stock module remains available for recovery. To roll back, first
move both VIPs away from the target, remove only the matching external override,
run `depmod -a 6.1.174-rk35xx-ophub`, then perform a controlled reboot. A restored
stock node must not resume holding VIPs while this incompatibility remains.
There is no LAN-scanning or per-client ARP workaround daemon.

Offline regression checks:

```sh
python3 -m unittest discover -s roles/desk_wifi_driver/tests
ansible-playbook -i inventory.ini playbooks/cluster/wifi-driver.yml --syntax-check
```
