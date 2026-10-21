# Automatic LAN DNS over IPv4 and IPv6

DNS IPv4 `192.168.1.250` and link-local IPv6 `fe80::250` share the existing
DNS VRRP election. The IPv6 address is attached with
`virtual_ipaddress_excluded`; it does not introduce a separate leader or a
permanent node preference. The Xiaozhi VIP and processes remain independent.

The management interface accepts UDP/TCP DNS from LAN link-local IPv6 clients.
The original IPv6 client address is forwarded over authenticated private DoH to
one of the three equal-weight AdGuard backends and appears in the unified UI at
`http://192.168.1.186/dns/`. This endpoint does not accept global IPv6 clients.
The panel's existing DNS role continues to follow the paired DNS VIP owner.

## Rollout

The inventory enables `desk_dns_ipv6_vip_enabled: true`; the role default remains
false. For an existing real-client DNS deployment:

```bash
ansible-playbook -i inventory.ini playbooks/dns/dns-ipv6.yml
```

The playbook freezes the current election, interface, DNS VIP service states and
Xiaozhi process/configuration snapshot. It checks for a conflicting IPv6 neighbor,
prepares every AdGuard access list before changing any frontend, then validates
and activates each DNS frontend serially. It preserves all existing access-list
settings and changes only the added LAN IPv6 range.

Only after every frontend passes IPv4 and IPv6 UDP/TCP checks does it install the
paired VRRP configuration. Active DNS VIP instances restart only when their
configuration changes; intentionally inactive instances stay inactive. Frontend,
access-list and VIP activation failures restore the affected node's previous
settings. Each phase verifies that protected Xiaozhi processes and configuration
have not changed. The final checks require both DNS addresses on exactly one
node and all three backends reachable through both VIP families.

## Router settings: user applies these manually

The router must stop advertising ISP IPv6 DNS in addition to the cluster DNS.
Do not change WAN connectivity, router advertisements, IPv6 address allocation,
or address-prefix configuration.

Under **IPv6 -> LAN Address Configuration -> DNS Source on the LAN Side**:

- Source: **Static configuration**.
- Preferred DNS: **`fe80::250`**.
- Spare DNS: leave blank if accepted; otherwise use **`fe80::250`** again.

Keep IPv4 DHCP DNS at **`192.168.1.250`**. Apply the router setting, then reconnect
Wi-Fi or renew clients' network leases so they receive the new resolver list.
An iPhone's automatic DNS list should contain the cluster IPv4/IPv6 resolvers and
no ISP IPv6 resolvers. Per-device manual DNS is unnecessary for this DHCP/RA path.
If the router rejects the link-local address, retain the existing configuration
and inspect its validation error before choosing a different IPv6 endpoint.

## Direct LAN checks

Link-local addresses require a local interface scope in command-line tools. On a
Mac connected through `en0`:

```bash
dig @fe80::250%en0 example.org A
dig +tcp @fe80::250%en0 example.org A
```

On a node use `%wlan0` instead. Ordinary client DNS settings receive their scope
from the network interface; do not put `%en0` or `%wlan0` in the modem DNS field.
