# Native HA HTTPS

HTTPS is an opt-in deployment, absent from `site.yml`. Its default phase is
read-only `preflight`. Every modifying phase also requires
`desk_https_apply_approved: true` after separate production approval. Nothing in
this guide establishes live certificate readiness or authorizes a rollout.

## Routing and trust

The existing management VIP `192.168.1.186:443` runs HAProxy TCP passthrough to
three native ARM64 Caddy instances on `10.10.10.11–13:8443`. Each of the four
SNI names has its own three-member backend with trusted-certificate, hostname
and SNI health checks. The three legacy hostnames require HTTP 200 through Caddy and their
local Unix sockets; herdr uses Caddy-only synthetic health. Cluster ingress health ends at the HAProxy frontend and is
independent of Dashboard state; NTFY and HA also require their existing application
readiness. A live TLS listener
alone cannot keep a node UP. Data and health checks send PROXY v2; `check-ssl` applies
only to probes, never a second TLS layer on client traffic. Keepalived election,
MQTT, HTTP listeners, NTFY base URL and existing client settings are unchanged.

Caddy's standard PROXY listener accepts both protocol versions. The small
`desk_proxy_v2` listener guard requires a v2 preamble, followed by the standard
parser's exact three-peer allowlist and TLS. Missing/v1 headers, malformed
commands and untrusted peers fail before serving requests. Header deadlines
remain owned by the standard parser, including incomplete v2 payloads.

Routing inspection must run on every node before activation. For the three legacy hostnames, the selected
upstream is a local HAProxy Unix listener, rather than a mesh-sourced connection
to the management VIP. Owner/standby routes differ, and the existing HTTP source
ACLs correctly reject mesh sources. The local listeners avoid dependence on
asymmetric LAN/mesh return routing or `rp_filter` changes. No source ACL is widened.

Each Unix socket is owned by `desk-caddy`, mode `0600`, and uses `accept-proxy`
on the **same existing HAProxy frontend**. Caddy sends PROXY v2 with the original
client address, so the existing `src` ACL still decides access. Forwarding
headers are scrubbed/rebuilt; the existing TCP HTTP binds do not accept PROXY.
The private second ntfy hop preserves HTTPS client identity only from known
cluster peers with the sanitized HTTPS envelope. Legacy HTTP keeps its existing
ntfy identity/rate-limit boundary.

`GET /__desk_https_health` is reserved for health checks. Caddy rejects this path
for public client addresses, ignoring spoofed forwarding headers. Only a v2
peer-address request on the permissioned Unix listener receives cluster ingress
health directly from HAProxy, without changing its source or calling Dashboard.
NTFY and HA retain their existing readiness paths (`/ready` and
`/api/desk_cluster/ready`) and probe-only loopback source. Other requests retain
their original source and all client ACLs.
Two failed checks withdraw that hostname's node; two successful checks restore it.

| Host | Existing client paths retained | Local frontend |
| --- | --- | --- |
| `cluster.huynm.dev` | `/`, exact `/api/cluster/status`, `/api/cluster/panel`, node operation endpoints | Settings `:80` routing via `desk-https-cluster.sock` |
| same | `/xiaozhi/`, Settings aliases, `/xiaozhi/ota/` bootstrap exemption | same |
| same | `/dns/` assets and certified `/dns/control/*` API, cookie/redirect rewrites | same |
| same | `/monitor/` assets/API/realtime SSE, sticky hub selection | same |
| same | `/vpn/` assets and permitted `/vpn/api/*` | same |
| same | `/network/`, authenticated `/network/server/*`, `/network/desk/*`, SSE | same |
| `ntfy.cluster.huynm.dev` | `/`, `/static/*`, topic publish/read/auth, `/json`, `/sse`, `/ws`, permitted account/admin APIs | `:2586` routing via `desk-https-ntfy.sock` |
| `ha.cluster.huynm.dev` | native root/auth/assets/dashboards, `/api/websocket`, `desk_cluster/*` WS commands | local `:8123` via `desk-https-ha.sock` |
| same | `/webrtc/*`, signed `/api/webrtc/ws`, session-protected `/api/webrtc/hls/*` | same |
| same | recording/thumbnail/session APIs and capability-scoped `/desk-camera-media/v1/*` | same |

There is no direct HTTPS route to core/gateway diagnostics, NATS monitoring,
go2rtc/relay APIs, private camera `:20226`, DNS sync/private transport, Beszel
config/peer ports, VPN native/peer APIs or HA agents. The existing denial of
`/internal/` remains, with edge denial of internal peer/sync/observe/local/native
namespaces. Public UI APIs such as `/dns/control/*` and `/network/server/*` remain
available through their existing validation boundaries. WebRTC media LAN/VPN
transport is unchanged; MSE/signaling and browser HTTP assets traverse HA.

## Herdr on the LAN PC

`https://herdr.huynm.dev` follows VIP `192.168.1.186:443` through cluster
Caddy to ordinary HTTP `192.168.1.168:7317`. This separate route sends no
upstream PROXY preamble. Upstream connection establishment is limited to 5s, without read or response timeouts on WebSocket terminals. Caddy uses the inbound PROXY-derived address to admit
only LAN `192.168.1.0/24` and WireGuard `10.77.0.0/24` application clients (others receive 403), scrubs identity
headers, and supplies the HTTPS forwarding envelope. Keep the PC's pairing/token authentication.
No PC firewall change is needed if its existing LAN rule permits the actual node
sources; otherwise review only those sources on TCP 7317 separately.

The reserved health path returns 200 locally only for trusted mesh identities,
including PROXY LOCAL; client identities receive 404. It does not call the PC.
PC failure can return 502 for herdr while all twelve TLS health members stay UP.
The role's verification gate deliberately requires only the nine legacy members;
Keepalived readiness does not depend on herdr or PC uptime. HTTP upgrade supports
WebSocket transport; real Ovis origin/cookie/authenticated WSS and reconnect,
PWA/service worker and push remain live acceptance checks.

Add exactly one **manually owned** certified AdGuard UI rewrite,
`herdr.huynm.dev → 192.168.1.186`, last, after trusted TLS and application checks
using an authorized client's temporary hostname mapping. The `dns` phase and
ownership journal continue managing only the three legacy names. Confirm no
pre-existing herdr record or journal ownership before rollout; investigate any
collision rather than infer ownership. DNS-01 does not need public A/AAAA or
inbound public web ports. The hostname becomes public in certificate transparency;
the existing Cloudflare DNS-edit token remains zone-scoped.

For an existing installation, approve baseline inspection first, then Caddy and
ingress separately. Do not use `--limit`: preflight requires the complete cluster.
Use `--step` for each modifying phase, with explicit order
**deskb1x → deskb2x → deskb3x**. Before each Caddy activation, validate the candidate
in the service environment and require the other six legacy members UP. After
each restart, require all nine legacy members UP across every HAProxy and trusted
herdr TLS/mesh health on that node before advancing. `serial: 1` does not enforce
these manual recovery gates. Activation restarts Caddy and interrupts existing
sessions on that node, including old sites. HAProxy's one-hour inactivity limit
still applies to WebSockets. Before each ingress reload, validate full HTTP+MQTT;
then require all nine legacy and three herdr members UP. Validate trusted TLS and
actual application/WSS through the VIP before publishing the manual rewrite.

For **herdr-only rollback**, remove only its manual rewrite through certified UI
(or restore its captured prior state), then remove its SNI/backend fragment through
the full composer with HTTP+MQTT validation and serial reload. Remove only its
Caddy site with serial validation/restarts and legacy recovery gates; revert names
in defaults/preflight together and retain certificates. Keep the explicit legacy
verify selector. Never use whole-role `rollback`, `--remove-https`, or DNS-helper
rollback for one hostname. The manual rewrite creates no journal entry. If prior
ownership exists, reconcile and clean that exact record first, then atomically
remove only its journal row; do not invalidate the journal by narrowing names first.

## Build and certificate lifecycle

The checked-in Go module and checksums pin Caddy `2.11.7`, Cloudflare module
`0.2.4`, and Go `1.26.9`. A direct Go main imports the standard Caddy modules,
Cloudflare and the v2 guard; no additional build wrapper or runtime service is
needed. On Linux, export `DESK_BUILD_ROOT=/tmp/desk-build`, create that directory, and pass `-e desk_build_root="$DESK_BUILD_ROOT"` to Ansible. Build on the controller:

```sh
scripts/build-caddy.sh "${DESK_BUILD_ROOT:-/private/tmp}/desk-caddy-package"
```

The package contains `caddy`, `manifest.json` with SHA-256, and `build.txt`.
Ansible verifies the controller artifact before replacement and checks ARM64 ELF,
hash and required modules again on the node. It never runs a remote source build.

Provide an ignored `secret/desk-https/cloudflare.env`, one unquoted line:
`CLOUDFLARE_API_TOKEN=<scoped token>`. The format guard accepts legacy 40-character
tokens and the 53-character `cfut_`/`cfat_` formats (prefix, 40-character payload,
8-character checksum). It rejects `cfk_` Global API Keys, malformed lengths,
quoted values and extra assignments. This is a structural check; Cloudflare
verifies validity and active status through its read-only token verification API.
See [Cloudflare token formats](https://developers.cloudflare.com/fundamentals/api/get-started/token-formats/).
Use only zone read and DNS edit
permissions for the relevant zone, never a global API key or cloudflared account
credential. Provisioning/rotation requires separate approval. Token operations
are `no_log`/`diff: false`; malformed input is rejected before Caddy can echo it.

The unit runs as `desk-caddy`, without privileged-port capability. Persistent
storage is `/var/lib/desk-caddy` (`0700`, `XDG_DATA_HOME`), independently on each
node. Keep the entire directory, including ACME accounts and keys, across
restarts, upgrades and rollback. Caddy owns issuance/renewal; no cron, shared
storage, Redis, Consul or new leader is introduced. Config autosave is disabled;
admin is bound only to `127.0.0.1:20219`.

Only DNS-01 is enabled, using explicit public propagation resolvers. Initial
installation is serial. Renewals remain independent, so serial Ansible is not a
renewal lock: the pinned provider's concurrent TXT append/exact-value cleanup is
tested with three independent providers and overlapping TXT content. Failed
renewal must retain the previous certificate; do not delete storage or force
production renewals to test it. Real CA issuance/renewal and token scope remain
live acceptance checks after approval.

## Ordered deployment after approval

Use one reviewed public vars file for the ACME email/package paths and phase
options; keep the token in the ignored file. Do not persist approval in shared
inventory. Run from the repository root with the normal inventory/Vault setup.

1. `ansible-playbook -i inventory.ini playbooks/https/caddy.yml` performs read-only
   preflight on all three nodes: VIP ownership/snapshot, source routes,
   `rp_filter`, legacy source-ACL responses and HTTP/MQTT service composition.
2. Select `desk_https_phase=caddy`, with explicit approval and ACME email. Install
   the pinned native services serially. Wait for trusted certificates on every
   node/name; process start alone is not certificate readiness.
3. Select `compatibility`. Its whole-cluster TLS/negative-PROXY gate runs before
   mutation. It installs permissioned local listeners, existing origin boundary
   fixes, origin-aware navigation and the cached patched Beszel UI serially,
   without exposing public `:443`.
4. Finish the HA panel asset update through the existing stopped-Core component
   activation/checkpoint workflow. Compatibility stages `panel.js`; it does not
   hot-patch or restart Core. Retain existing HA configuration, authentication,
   camera authorities and checkpoints.
5. Select `ingress`. Before changing the first HAProxy, all nodes must pass the
   complete Caddy-to-local-backend matrix, valid data/health v2 headers, rejected
   missing/v1/untrusted headers, and spoofed-forwarding-header source-ACL tests.
   Then install `:443` serially, validate HTTP+MQTT together and gracefully reload.
6. Select `dns`. Public TLS/data is checked using SNI against the VIP before DNS
   edits. Only the named coordinator adds the three exact private rewrites through
   the existing certified policy API. It waits for idle/all-three readback;
   HTTP 202 is pending. Conflicts stop without replacing live UI policy.
7. Select `verify` and complete browser/media/failover acceptance below.
8. Optional client migration is a separate decision. Do not redirect HTTP or
   enable HSTS during the initial deployment.

For a modifying phase, supply both the selected phase and
`desk_https_apply_approved=true` only after that phase is approved. The preflight
play is non-serial; every modifying play is `serial: 1`, `any_errors_fatal: true`.
Rollback uses separate reachability discovery instead of requiring full-cluster
deployment preflight. Its discovery tolerates unreachable nodes; approval remains
mandatory and the serial mutation play still stops on a local failure.

## Compatibility, acceptance and rollback

Dashboard links switch to the HA/NTFY HTTPS names only on the cluster HTTPS
origin. HA's maintenance link follows the same rule after its normal component
activation. Beszel's generated browser agent URL follows the HTTPS origin and
`/monitor/` without changing existing agents. Node-control and NetAlertX accept
the exact HTTPS origin with verified Host/scheme, retaining HTTP-origin behavior
and mutation confirmation/quorum rules. Do not remove Origin or enable broad CORS.

Legacy NTFY `base-url`, HA `internal_url`, Pi panel URL, report/canonical server
URLs and server-to-server HTTP calls remain unchanged. Check browser requests,
redirects, cookies, generated links and OAuth callbacks explicitly; do not apply
blanket response-body rewrites or treat every server-side HTTP URL as mixed content.

Acceptance after approval includes:

- Trusted chain, SAN/SNI and expiry for all nine node/name combinations, including
  v2 address/LOCAL health header forms; all nine HAProxy servers UP.
- Every inventory path above, static assets, navigation, account/admin mutation,
  DNS policy readback, NetAlertX settings/scan, and node-operation prepare with no
  actual reboot. Reject foreign Origin, wrong Host and missing confirmation.
- NTFY JSON/SSE/WS, Beszel SSE, HA WS, WebRTC/HLS, camera thumbnails, recording
  Range requests and media touch/close. No mixed content or silent downgrade.
- LAN/VPN permissions exactly match existing ACLs; forged forwarding headers do
  not make an outside client trusted. No private API/port becomes reachable.
- Under separately approved failure injection, stop one Caddy and verify new
  requests use two survivors, then restore it. Fail the VIP owner and verify new
  HTTPS connections/reconnects, plus unchanged HTTP/MQTT/NTFY behavior. Existing
  WebSocket/TCP sessions are not promised seamless migration.
- Rerun each phase and the ordinary ingress/monitoring/UI/VPN/network/NTFY/HA
  composers. No duplicate HTTPS/socket block, unnecessary restart, new cert or
  policy write. Full ingress reruns retain VPN and exact panel routes as well.

Rollback is separately approved `desk_https_phase=rollback`: on reachable nodes,
remove only the marked HTTPS/socket rules and verified deployment-owned DNS records, validate
HTTP+MQTT together, then reload serially. Matching pre-existing rewrites are never
claimed. A conflicting later UI edit leaves DNS cleanup pending.
DNS cleanup is attempted once by the first reachable node. If that node lacks
the original ownership journal (including when deskb1x is down), the journal is
invalid, policy cannot be certified, or a UI conflict exists, DNS is retained and
reported `pending`; local ingress rollback continues. Never infer ownership from
a matching live record. Recover the original journal/node and rerun cleanup.
Unreachable nodes are also reported pending and must be rolled back after recovery.
The journal fsyncs ownership before the write, so an interrupted policy request
can be reconciled. Do not restore an old global AdGuard config, touch Keepalived,
delete cert storage or roll back unrelated changes. Caddy may remain running on
its private, v2-only listener while certificates continue renewing.

The HTTP composer restores its previous candidate on reload failure. Caddy
activation restores replaced inputs/service state if systemd activation fails.
Always investigate the original error before retrying; do not bypass preflight.

## Focused offline checks

Use a Python environment with the repository's existing Ansible/Jinja/YAML test
dependencies. The native binary is only a local test build; production packaging
always targets Linux ARM64.

```sh
cd roles/desk_https/caddy
go test -mod=readonly ./...
go build -mod=readonly -o "${DESK_BUILD_ROOT:-/private/tmp}/desk-caddy-test" ./cmd/desk-caddy
cd ../../..
CADDY_BIN="${DESK_BUILD_ROOT:-/private/tmp}/desk-caddy-test" HAPROXY_BIN=/path/to/native/ssl-enabled/haproxy python3 -m unittest discover -s roles/desk_https/tests -v
node roles/desk_https/tests/test_links.js
ansible-playbook -i inventory.ini playbooks/https/caddy.yml --syntax-check
git diff --check
```

TLS/transport tests use temporary loopback/Unix sockets and generated local
certificates, never production or public DNS. The Caddy adapter is exercised with
a synthetic token. The health fixture uses three real Caddys and local HAProxy
processes; Dashboard 503 must leave cluster ingress UP while other UI paths work.
Stopping one local proxy must withdraw its three legacy members while TLS
still works and herdr remains UP; restarting restores the legacy members. The
separate loopback PC fixture tests plain HTTP, failure isolation/recovery and
WebSocket upgrade/echo/reconnect; transport fixtures verify source/header gates. The rollback
fixture executes the play logic with command actions replaced, testing loss of
deskb2x and deskb1x without performing system/DNS writes. Existing monitoring, NTFY ingress, dashboard, UI, node-control,
HA proxy and playbook-layout regression tests remain applicable.

The device UI `http://192.168.1.128:8080/` remains an explicit blocker: no device
hostname or device proxy is added. Detailed Xiaozhi Settings API/WS/callback source
lives in the separately pinned server checkout; its complete browser compatibility
also requires verification against that exact deployed revision.
