# Automatic LAN DNS without per-device configuration

The router currently advertises ISP IPv6 DNS alongside cluster IPv4 DNS. It
rejects an empty static IPv6 resolver. Do not change WAN connectivity, router
advertisement, IPv6 address allocation or per-device DNS settings.

Proposed cluster extension (not deployed): `fe80::250` on the same management
link and same owner as IPv4 DNS VIP `192.168.1.250`. Keepalived's existing IPv4
VRRP election can attach an IPv6 address in `virtual_ipaddress_excluded`; no new
node preference or independent IPv6 election is introduced. dnsdist must accept
LAN link-local IPv6 queries, preserve those client IPs over the same authenticated
private DoH backends, and keep global/WAN IPv6 sources excluded.

Required checks before the user changes router DNS:
- existing DHCPv6/router neighbour data contains no owner of `fe80::250`;
- candidate Keepalived and dnsdist configurations validate on pinned binaries;
- current intentionally inactive DNS VIP instances remain inactive;
- exactly one owner has both DNS VIP addresses;
- IPv6 UDP and TCP test queries from the management LAN reach all three backends;
- aggregated logs retain the original IPv6 device address;
- Xiaozhi service/configuration snapshots remain unchanged.

Only after these checks pass, the user sets:
IPv6 -> LAN Address Configuration -> DNS Source on the LAN Side:
Static configuration; Preferred DNS: `fe80::250`; Spare DNS: blank if accepted,
otherwise the same `fe80::250`. IPv4 DHCP DNS remains `192.168.1.250`.
The user performs all modem edits and reconnects/renews client network leases.

The planned IPv6 endpoint is pending explicit approval after the automatic
approval review rejected immediate rollout of an unverified new IPv6 VIP.
