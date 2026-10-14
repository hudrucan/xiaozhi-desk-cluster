# Single-node WireGuard trial

Opt-in only: `vpn-trial.yml` is separate from site provisioning. Node 3
192.168.1.103 is selected because it currently owns neither management VIP
192.168.1.186 nor DNS VIP 192.168.1.250. No VPN VIP, election or config sync yet.
Kernel WireGuard and wireguard-tools already exist on all three nodes.

Generate credentials once with Python and the cryptography library:

```sh
python3 scripts/create-vpn-trial-secret.py
ansible-playbook -i inventory.ini vpn-trial.yml
```

Credentials and profiles live in ignored `secret/vpn-trial/`, with 0600 files.
Never commit profiles, QR images or private keys. The service is NOT enabled
at boot during this trial. Start explicitly again after reboot if needed.
The running trial stays on node 3; avoiding other VIPs is a deployment check,
not yet a dynamic placement policy.

Import `iphone-lan.conf` in the iPhone WireGuard app, enable it over home Wi-Fi.
Try pinging tunnel gateway 10.77.0.1 and opening http://192.168.1.186/.
Current full-LAN scope: all protocols/ports to 192.168.1.0/24 plus gateway ICMP.
The original restricted dashboard/DNS pilot is recorded in acceptance below.
Do not set a client-wide DNS override during the initial reachability test.
Existing default routes, resolver settings and IPv4 forwarding are unchanged.
Custom iptables chains apply only to wgdesk0. Scoped forwarding and local-input SNAT use .103 so LAN replies
and the dashboard's trusted-LAN check work; AdGuard initially sees .103 for
this peer. Real VPN peer DNS identity is a later design task.

After LAN success, the user manually adds one modem port mapping:
WAN 2_INTERNET_R_VID_35, UDP 51820 external/internal, host 192.168.1.103.
Then disable Wi-Fi, import `iphone-wan.conf`, and repeat over 4G/5G.
The temporary WAN endpoint is 171.248.168.14; DDNS comes later.
Never enable the LAN and WAN profiles simultaneously (they share one peer).
No modem change is performed by Ansible.

Stop/rollback the isolated trial:

```sh
sudo systemctl stop desk-vpn-trial
```

This removes wgdesk0 and only its four dedicated firewall chains and their scoped jumps.
It does not restart voice, DNS or VIP services. To remove the trial completely,
stop it first, remove its unit and its two isolated directories, then daemon-reload.

Still to verify with the user: repeated Wi-Fi/mobile roaming,
throughput and latency under sustained transfer. The user explicitly skipped
Cake TTS benchmarking for this trial. Kernel transport is not constrained by a systemd CPU quota; any future
bandwidth cap must be selected from benchmark evidence. No HA claim yet.

Acceptance on 2026-10-14: syntax and scoped firewall regression checks PASS.
Node 3 rollout PASS with protected voice/DNS/VIP runtime and policy unchanged.
A temporary network namespace acted as an encrypted client using the real peer:
gateway ICMP, dashboard HTTP 200, DNS VIP answer and denied SSH port PASS.
The namespace and temporary remote key file were removed. This checks real
WireGuard encryption, routing and firewall; it does not validate the Wi-Fi path,
modem forwarding, WAN connectivity or TTS RTF.
The user subsequently confirmed a new handshake and dashboard access from
the iPhone on home Wi-Fi (LAN PASS). This is not a 4G/5G test.

Offline regression command:

```sh
python3 roles/desk_vpn_trial/tests/test_firewall.py
```

WAN acceptance on 2026-10-14: the user manually applied the UDP 51820 mapping
to node 3, then confirmed dashboard access on the iPhone with Wi-Fi disabled
and Desk WAN enabled over cellular. Server inspection confirmed an external
IPv4 peer endpoint, a 12-second-old handshake and increasing transfer counters.
LAN and WAN reachability PASS. This does not establish throughput, CPU/TTS RTF
or HA failover performance. No modem setting was changed by the agent.

CPU observation on node 3, 2026-10-14: 15.11 seconds without VPN transfer
measured 10.58% whole-host CPU on four cores, 0% I/O wait and 0.24% IRQ/softirq.
A subsequent 30-second window with light peer traffic (7,028 RX / 33,932 TX
bytes, approximately 1.4 KiB/s combined) measured 11.61% whole-host CPU.
These are observational host averages, not isolated WireGuard CPU or throughput
benchmarks; background service variation cannot be separated. The largest
observed userspace process was deskbox-front-panel at 5.30% of one core
(approximately 1.33% of total CPU). No TTS job, service stop or synthetic load
was generated. User requested no TTS benchmark.

Full LAN extension: iPhone AllowedIPs is 10.77.0.1/32, 192.168.1.0/24.
No default route, private cluster 10.10.10.0/24 or IPv6 route is added.
The authenticated .2 peer may access any IPv4 LAN host/port, including node 3
itself. Local-input SNAT preserves existing trusted-LAN web checks without
changing HAProxy ACLs. Router and Internet DNS settings remain unchanged.
Refresh existing profiles without rekeying:

```sh
python3 scripts/create-vpn-trial-secret.py --profiles-only
```

Existing iPhone profiles must change Allowed IPs to the new list (or be
re-imported). The modem UDP mapping remains unchanged. Firewall updates parse
the candidate before applying, keep existing interface-scoped jumps, and
replace only the role-owned chains using iptables-restore --noflush.
PostDown removes rules only after the tunnel disappears.

Full-LAN live acceptance on 2026-10-14: deployment and protected service/policy
guard PASS. Encrypted client namespace checks PASS: dashboard .186 HTTP 200,
DNS VIP reply, SSH banners from .101/.102/.103, own-node temporary HTTP server
with the expected LAN source identity, and modem .1 HTTPS TCP connection.
Private cluster had no client route. The direct-node port-80 checks were removed
after connection refusals confirmed that the existing web ingress listens only
on its VIP, not the physical node addresses; no ingress listener was changed.
The temporary HTTP server, client namespace and remote test profile were cleaned
up. The user subsequently updated the iPhone Allowed IPs and confirmed modem
and dashboard access over cellular (full-LAN physical client check PASS). Existing credentials and modem port mapping are preserved.
