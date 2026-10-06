# Xiaozhi Desk Cluster

Ansible owns the three RK3528 desk boxes. Wi-Fi provides SSH management and
Internet access; the `end0` Ethernet fabric is the private cluster LAN. Do not
configure node identity, networking, SSH or NATS manually outside Ansible.

| Node      | Management address (`inventory.ini`) | Private address (`host_vars`) |
| --------- | ------------------------------------ | ----------------------------- |
| `deskb1x` | `192.168.1.101`                      | `10.10.10.11`                 |
| `deskb2x` | `192.168.1.102`                      | `10.10.10.12`                 |
| `deskb3x` | `192.168.1.103`                      | `10.10.10.13`                 |

Host-specific private addressing remains authoritative in
`host_vars/<node>.yml` as `cluster_ip`; NATS templates and checks consume those
values. `group_vars/deskboxes.yml` retains the existing interface/connection
settings and adds password references to Vault variables.

## Core NATS

All three nodes run **nats-server v2.15.0**, installed from the official
[ARM64 Debian release](https://github.com/nats-io/nats-server/releases/tag/v2.15.0).
The role accepts only this pinned release and SHA256:

```text
nats-server-v2.15.0-arm64.deb
7e37abbab18910ebc6d7a4733546d061d1098c7ee35881a92ce9d451f5804d92
```

The role checks Debian OS family, ARM64 facts and `dpkg --print-architecture`
before installation. It downloads to `/var/cache/nats-server` only when the
installed package differs, verifies the cached checksum before `apt` installation,
and permits an explicit downgrade to the pinned version. Matching cached content
is reused. The package is held against repository-driven upgrades. Changing the
version later requires deliberately updating the role's pin, checksum and URL
assertions together; it never follows `latest` or a Debian/Armbian NATS version.

The official package supplies `/usr/bin/nats-server` and no systemd unit. Ansible
provides the `/usr/sbin/nats-server` symlink required by the managed service and
owns `/etc/systemd/system/nats-server.service`.

| Listener                    | Port       | Bind address on each node |
| --------------------------- | ---------- | ------------------------- |
| Application clients         | `4222/TCP` | `cluster_ip`              |
| Authenticated server routes | `6222/TCP` | `cluster_ip`              |
| HTTP monitoring             | `8222/TCP` | `cluster_ip`              |

Cluster name is `desk-cluster`; each server name is `inventory_hostname`.
`max_payload` is `8MB`, and debug/trace are disabled. Each node seeds routes to
the other two nodes using `hostvars[peer].cluster_ip`, excluding itself. Explicit
route advertisement also uses the private IP. No listener binds to Wi-Fi or
`0.0.0.0`. Preflight requires distinct `10.10.10.0/24` addresses and checks that
the local `cluster_ip/24` exists on the private interface.

This is **Core NATS only**. JetStream is disabled by omission: there is no
JetStream block, storage directory or persistence setup. Core NATS does not
persist messages for disconnected subscribers. Future consumers are
`xiaozhi-core` and `xiaozhi-worker` for internal RPC and streaming-operation
control. The MQTT gateway is not a NATS client.

Public defaults live in `roles/nats/defaults/main.yml`: version, release URL and
checksum, listener ports, cluster name, payload size, usernames and verification
retry settings. Override shared listener/user settings in group vars, not per-host
address copies. `nats_client_user` defaults to `xiaozhi`; `nats_route_user` defaults
to `nats-route`. The V1 client identity has no subject restrictions. Subject-level
permissions will follow the RPC contract, including request/reply `_INBOX.*` needs.

## Vault and transport policy

The operator must add these string variables to the existing **encrypted**
`vars/vault.yml` using their normal Ansible Vault workflow:

- `vault_nats_client_password`: nonempty application/client password.
- `vault_nats_route_password`: nonempty server-route password.

Group vars reference them through `nats_client_password` and
`nats_route_password`. Do not substitute plaintext public variables or commit
unencrypted passwords. Secret assertions, configuration rendering and NATS
configuration validation use `no_log`; secret template diffs are disabled.

Route usernames and passwords must use only ASCII letters, digits, `.`, `_`, `~`
or `-` because credentials appear in `nats://user:password@address:port` URLs.
Preflight rejects other characters instead of guessing URL-encoding behavior.
Application and route usernames must differ. The application password is quoted
and escaped in the generated configuration and is not used in route URLs.

V1 intentionally omits TLS because the Ethernet fabric is physically private.
**Credentials and NATS traffic are not encrypted on this link.** Monitoring HTTP
also has no authentication; its private bind is the access boundary. Do not expose
these ports through Wi-Fi, routing or port forwarding to an untrusted network.
Add TLS and certificate lifecycle management if that trust boundary changes.

## Service and deployment order

NATS runs as the unprivileged system user/group `nats`, without a login or home
directory. `/etc/nats-server.conf` is owned `nats:nats`, mode `0600`, so the service
can read it while other users cannot. `root:nats` with `0600` would prevent the
service user from reading it. The unit makes the filesystem read-only to the
service with `ProtectSystem=strict`; no writable JetStream storage is added.
Other hardening includes no privilege escalation, private temporary/device views,
protected home/kernel settings, empty capabilities and IPv4/Unix address families.
Logs go to journald. Package service actions are suppressed during installation.

Execution is `/usr/sbin/nats-server -c /etc/nats-server.conf`, with validation in
`ExecStartPre`. The service is enabled at boot and restarts on failure after three
seconds. Systemd sends `SIGUSR2` for
[NATS lame-duck shutdown](https://docs.nats.io/learn/deployment/rolling-upgrades):
five-second grace and a thirty-second drain, with a forty-five-second stop timeout.

`site.yml` runs:

1. Existing identity, network and access roles.
2. Existing SSH hardening with `serial: 1`, unchanged.
3. The NATS role in its own `serial: 1` play, stopping the rollout on failure.
4. A separate read-only NATS verification play after all serial batches finish.
5. A separate `worker`-tagged play for the explicit `xiaozhi_workers` group.
6. A `control-plane`-tagged `serial: 1` play for `xiaozhi_control_planes`.
7. Final read-only control-plane verification from one deskbox over the private LAN.
8. An `ingress`-tagged all-node preflight, serial ingress rollout and final verification.

The config template uses `nats-server -t -c <candidate>` before replacing the
live config. Ordered handlers validate `/etc/nats-server.conf` again before
daemon-reload and restart. Validation failure stops that host and the rollout;
it does not restart a healthy service with a bad generated config. Unchanged
package/config/unit causes no restart. Handlers are flushed before local health
is checked so a node becomes locally ready before advancing the serial batch.
That local check does not require peers during first installation.

After review and Vault preparation, the operator can apply the full inventory:

```sh
ansible-playbook -i inventory.ini site.yml --ask-vault-pass
```

The controller needs Ansible and `community.general` for the existing network
role. Target hosts need Debian ARM64, systemd, Python and APT support. Use the
existing `bootstrap.yml` access workflow when required. First installation must
cover all three nodes before full-cluster route verification can succeed.

## Verification

The NATS verification play includes `roles/nats/tasks/verify.yml`, a separate read-only entry
point that reuses role defaults without rerunning installation tasks. From each
managed node it checks:

- `systemctl is-active nats-server`.
- HTTP 200 from its private `/healthz`, bypassing management HTTP proxies.
- `/routez` eventually reports exactly the other two distinct server names and
  private IPs, and two distinct remote server IDs.

The v2.15.0 [`Routez`/`RouteInfo` schema](https://github.com/nats-io/nats-server/blob/v2.15.0/server/monitor.go)
exposes `server_name`, `num_routes`, and `routes` entries with `remote_name`,
`remote_id` and `ip`. `num_routes` counts TCP route connections, including pools;
it is not an exact peer count. Verification deduplicates names/IDs/IPs instead of
requiring `num_routes == 2`. It retries transient convergence after the final
node starts. It changes no configuration or services.

Repository review can parse YAML and render templates with placeholder passwords
without opening Vault or connecting to nodes. A full syntax check is normally:

```sh
ansible-playbook -i inventory.ini site.yml --syntax-check --ask-vault-pass
```

When Vault access is unavailable or forbidden, use a temporary copy of the
playbook that omits `vars_files` solely for structural syntax checking; that
cannot prove Vault values resolve. Do not weaken the real playbook or decrypt
Vault merely for static review. Runtime config validation, systemd compatibility
and route/health checks still require a separately approved deployment.

## Standalone workers

The explicit `[xiaozhi_workers]` inventory group contains `deskb1x`, `deskb2x`
and `deskb3x`; all three workers are deployed/eligible. Worker membership is
explicit and is not inferred from `deskboxes`.

`roles/xiaozhi_worker` deploys source from
[`hudrucan/xiaozhi-desk-robot-server`](https://github.com/hudrucan/xiaozhi-desk-robot-server)
at the full, immutable commit
`e586c725600dd79a3e24ec7f763bb32d7f9e3c9f`. The public
`xiaozhi_worker_revision` must be a full 40-character SHA, never a branch or
`latest`. A future source update changes that pin deliberately. Checkout is
root-owned; local working copies and rsync are not deployment sources.

| Path                                         | Purpose                                            |
| -------------------------------------------- | -------------------------------------------------- |
| `/opt/xiaozhi-worker/repo`                   | Pinned source checkout                             |
| `/opt/xiaozhi-worker/venv`                   | Dedicated Python 3.11 environment                  |
| `/etc/xiaozhi-worker.env`                    | Existing NATS client credentials, root:root `0600` |
| `/etc/systemd/system/xiaozhi-worker.service` | Managed worker service                             |

Only `main/xiaozhi-server/requirements-worker.txt` is installed into the venv:
currently `nats-py==2.16.0`. Full `requirements.txt`, providers, FFmpeg, Opus
and model packages are not installed. APT prerequisites are `git`, `python3`,
`python3-venv`, `ca-certificates` and `python3-packaging`; the last supports
Ansible's pip module on the system interpreter. Pinned requirements use
`state: present`, without upgrades or forced reinstalls.

The environment's three NATS URLs are derived from sorted `deskboxes` and
their existing `hostvars[host].cluster_ip`; no address copies are added to vars.
The worker reuses `nats_client_user` (default `xiaozhi`) and
`nats_client_password`, which must equal `vault_nats_client_password`. The role
reads the NATS role's public username/port defaults without executing NATS
deployment and honors existing overrides. No separate worker password is added.
`XIAOZHI_WORKER_ID` is `inventory_hostname`. The environment template quotes
values and escapes backslashes/double quotes for systemd; secret assertions,
rendering and the request probe use `no_log`, with secret diffs disabled.

The unprivileged `xiaozhi-worker` system user/group has no login or home.
Systemd runs `/opt/xiaozhi-worker/venv/bin/python worker.py` from
`/opt/xiaozhi-worker/repo/main/xiaozhi-server`, with no stdin/TTY and journald
output. The service starts after network-online and local NATS, is enabled at
boot, and restarts on failure after three seconds. SIGTERM permits worker drain,
with a sixty-second stop timeout. Hardening includes `NoNewPrivileges`,
`PrivateTmp`, `ProtectHome`, `ProtectSystem=strict`, no capabilities and only
Unix/IPv4 sockets. Python bytecode writes are disabled; no runtime write path is
added. Source, venv dependency, environment or unit changes notify restart;
only unit changes notify daemon-reload. Unchanged services remain started.

The worker play loads the existing encrypted `vars/vault.yml`. Preflight checks
explicit membership, the three private NATS nodes, nonempty matching Vault
credentials, the full source SHA, Debian ARM64 and Python 3.11 before mutations.
For check/apply, limit to the worker group as well as selecting its tag so
unrelated plays cannot gather facts on non-worker hosts:

```sh
ansible-playbook -i inventory.ini site.yml --check --tags worker --limit xiaozhi_workers --ask-vault-pass
ansible-playbook -i inventory.ini site.yml --tags worker --limit xiaozhi_workers --ask-vault-pass
```

Check mode reports the source/venv desired state and evaluates environment/unit
templates without creating accounts/directories, installing packages, checking
out source, invoking pip or running service handlers/probes. These dependent
steps wait for a real apply; `check_mode: false` is not used to force mutations.

After deployment, `tasks/verify.yml` checks `systemctl is-active xiaozhi-worker`
and uses the worker venv's `nats-py` for a read-only targeted request to
`xiaozhi.v1.worker.<inventory_hostname>.ping`. It requires the
JSON object to equal protocol `xiaozhi-worker-v1`, the corresponding worker ID, status
`ok` and `capabilities: []`. Request timeout is three seconds, connect timeout
is eight seconds overall, and each probe is bounded to fifteen seconds with
finite retries. No NATS CLI or balanced queue-distribution check is added.
This deploys only the transport skeleton; no normal `app.py`,
providers, core dispatcher, gateway, JetStream or containers are started.

## Standalone control-plane backends

The explicit `[xiaozhi_control_planes]` group contains all three deskboxes.
`roles/xiaozhi_control_plane` defines the same standalone Settings/control process
on each node, pinned to server revision
`fda3c3c62e79e8ad3d03f7346203694feef2f9ab`. It never runs normal `app.py`, workers
or provider/model initialization. The `control-plane` play follows NATS with
`serial: 1` and `any_errors_fatal: true`; preflight requires all three group members
to be selected, Debian ARM64 and the system Python 3.11 interpreter.

| Path | Ownership / purpose |
| --- | --- |
| `/opt/xiaozhi-control-plane/repo` | Root-owned pinned checkout |
| `/opt/xiaozhi-control-plane/venv` | Root-owned dedicated Python environment |
| `/etc/xiaozhi-control-plane.env` | Root:root `0600`; systemd reads NATS credentials |
| `/etc/xiaozhi-control-plane/drive-credentials.json` | Root:service-group `0640`, private directory `0750` |
| `repo/main/xiaozhi-server/data/bootstrap.yaml` | Root:service-group `0640`; node identity and private Cloud authority |
| `repo/main/xiaozhi-server/data/{cloud-config,node-secrets,cloud-soundbank}` | Service-owned `0700`; the only persistent service write paths |

The source `data/` parent is root:service-group `0750`; the service cannot replace
its managed bootstrap or write elsewhere in the checkout/venv. Runtime paths
are already Git-ignored by the pinned source. Ansible creates fresh state; it
does not copy developer-machine `data/` or generate runtime Soundbank files.
The `xiaozhi-control-plane` system user/group has no login shell or home.

APT installs only `git`, `python3`, `python3-venv`, `python3-packaging`,
`ca-certificates` and `libopus0`. Pip installs only
`main/xiaozhi-server/requirements-control-plane.txt`, without full server
requirements, provider models or FFmpeg. Opus is retained for strict P3 validation
when Settings explicitly edits Soundbank; unrelated V2 Settings Save needs no
local Soundbank bytes. Nodes need Internet/package access and read access to the
pinned Git repository.

Populate the existing **encrypted** `vars/vault.yml` locally. No private values
belong in public defaults, inventory, host vars or this README:

- `vault_xiaozhi_cloud_folder_id`: the shared Cloud Config folder.
- `vault_xiaozhi_cloud_manifest_file_id`: the same Config manifest on all nodes.
- `vault_xiaozhi_cloud_credentials`: a YAML credential mapping supported by
  Google Auth, rendered with `to_nice_json`; it must authorize live reads and
  CAS-last Config publication to that authority.
- Optional `vault_xiaozhi_cloud_memory_manifest_file_id`: rendered as
  `google_drive.memory_manifest_file_id` only when supplied; Memory is not required.
- Existing `vault_nats_client_password`: reused unchanged through
  `nats_client_password`; no separate control-plane password is added.

NATS username/port come from the NATS public defaults and honor the same shared
overrides as workers. The NATS server list comes from sorted `deskboxes` and
`hostvars.cluster_ip`; no private-address copies or worker identity are added.
Secret assertions/candidate rendering use `no_log`; credential, bootstrap and
password-bearing environment writes also disable diffs. Each bootstrap sets
`config_provider: google_drive`, `node_id: inventory_hostname`, and absolute
credentials path `/etc/xiaozhi-control-plane/drive-credentials.json`.

Each backend binds **only** its `cluster_ip:8004` on `end0` with
`XIAOZHI_CONTROL_PLANE_ALLOW_REMOTE=true`. Reconciliation defaults to 45 seconds.
No control-plane listener binds `0.0.0.0` or the `wlan0` management address.
Settings APIs are unauthenticated within this trusted private backend segment.
The ingress foundation below exposes them to the trusted management LAN;
do not route/expose either segment to an untrusted network.
The current initial Cloud desired VIP is `192.168.1.186`; it is desired state,
not proof of an active address. Ansible does not own or pin the VIP value. Verification
requires all three nodes to report the same non-empty value and trusts server-side
Cloud Config validation for address validity. Changing VIP through unified Settings
does not require an Ansible expected-value change.

Systemd wants/starts after network-online and local NATS, runs the dedicated venv's
`python control_plane.py` from `repo/main/xiaozhi-server`, uses journald and no
stdin/TTY, disables bytecode writes, restarts on failure and sends SIGTERM with
a 120-second graceful stop window. Hardening mirrors workers, including no
capabilities and a read-only filesystem except the three managed data directories.
Unix, IPv4 and IPv6 sockets support Drive/NATS clients.

After independent review and local Vault preparation:

```sh
ansible-playbook -i inventory.ini site.yml --check --tags control-plane --limit xiaozhi_control_planes --ask-vault-pass
ansible-playbook -i inventory.ini site.yml --tags control-plane --limit xiaozhi_control_planes --ask-vault-pass
```

Check mode evaluates preflight and all templates, including private candidates
without printing them. It skips account/package/source/venv/data/credential writes,
service actions and live probes, and never forces changes with `check_mode: false`.
Successful check mode cannot prove Cloud credentials, topology or runtime readiness.

Apply flushes ordered reload/restart handlers, starts the unchanged service and
checks `systemctl is-active`, local `/healthz` and `/api/cluster` on the private
address before advancing. Health is a cheap validated desired-cache/assignment
check; it can remain healthy offline. Deployment additionally requires a
successful live Cloud reconciliation, connected NATS, matching node identity and
no conversation runtime. Missing assignments fail with a non-secret message;
Ansible never adds them.

After all three serial batches finish, the final read-only play probes all six
health/status URLs **from `deskb1x` over the private LAN**, bypassing HTTP proxies.
Bounded retries permit startup convergence. It requires health 200 on every node,
matching identities, Google Drive, shared V2 scope, one common desired revision,
one non-empty Cloud-configured VIP, connected NATS and `runtime_revision: null` with
runtime capability disabled. A recorded active revision is allowed and does not
mean this process started conversation runtime.

V1/legacy scope fails final verification with an explicit migration message.
Missing topology assignments, divergence or Cloud access errors also fail read-only.
Handle these in a separate explicitly reviewed Cloud migration/topology operation,
then rerun; this role does not POST sync/migration or change Cloud state. Only
healthy shared V2 revision agreement on all three nodes permits ingress deployment.

## Management ingress foundation

`[xiaozhi_ingress]` explicitly contains all three control-plane nodes. Run
`ingress.yml` to deploy only this foundation, or select the `ingress` tag in
`site.yml`. Ingress is intentionally part of default full-site convergence:
an untagged `site.yml` apply includes it after control-plane verification.
The `ingress` tag selects these plays; it does not make them opt-in. Use
`--skip-tags ingress` to omit ingress from a full-site run.
No Vault credentials are needed by the standalone ingress playbooks;
they read only the safe local control-plane status API. All three backends must
already be deployed, healthy, reconciled to shared V2, and agree on one revision
and a non-empty `ingress.configured_vip`. Legacy migration is a separate operation.
A status sample can overlap periodic Cloud reconciliation. Preflight retries the
read-only status GET while its state is `refreshing` (up to ten retries with a
one-second delay and the existing three-second request timeout). After that it
still requires `ready`, synced shared V2 and a successful live reconciliation;
an error, invalid snapshot or persistent refresh fails closed. It does not
trigger a Cloud sync, accept cached state as ready or start the failover test.

The VIP comes from **Cloud desired state**, never an Ansible default/expected
address. `192.168.1.186` is the current initial Cloud value only. Preflight reads
all three local `/api/cluster` endpoints before any mutation and freezes the
agreed value for that rollout. Immediately before each serial node's first
mutation, a read-only local status check requires its current desired revision
and configured VIP to match its frozen preflight values. Final verification
re-reads all three control planes and requires the same frozen revision/VIP
before reporting success. These checks never update the frozen values; drift or
an unreadable status stops rollout/verification without dynamic VIP migration.
It does not PUT Config, migrate Cloud state, or
materialize Soundbank assets. The interface/prefix is discovered from the unique
local IPv4 address matching `ansible_host` in inventory (currently `wlan0`),
rather than assuming a NIC name or using the private Ethernet default route.
Every management address and the VIP must share a usable management subnet;
the VIP cannot equal any node address. Server-side Cloud validation remains the
authority for the configured address contract.

The role installs Debian `haproxy` and `keepalived` packages with automatic service
starts suppressed. It owns `/etc/haproxy/haproxy.cfg`,
`/etc/keepalived/keepalived.conf`, a root-owned Python standard-library readiness
probe and a sysctl file enabling IPv4 nonlocal binding. This lets HAProxy bind
only `VIP:80` on standby nodes as well as the owner. Port 80 is configurable via
`xiaozhi_ingress_port`; backend port defaults to the existing control-plane port
8004. Backends come from sorted inventory `cluster_ip` values: currently
`10.10.10.11:8004`, `10.10.10.12:8004`, `10.10.10.13:8004`. HTTP round-robin health
checks require `/healthz` status 200. No public stats/admin listener is added;
HAProxy stats are available only through a root-accessible Unix socket.

Keepalived uses unicast VRRP between the three inventory management addresses,
VRID 51 by default (`xiaozhi_ingress_vrid`; reserve a unique VRID on this LAN).
All instances start in `BACKUP`; sorted-node priorities 150/140/130 are election
preferences, with `nopreempt` to avoid automatic failback to a recovered node.
The current VIP owner sends two gratuitous ARP messages every 30 seconds
(`xiaozhi_ingress_garp_refresh_seconds` / `xiaozhi_ingress_garp_refresh_repeat`)
to refresh neighbor/AP learning. Only the MASTER instance sends these periodic
announcements; a newly elected owner advertises its own MAC. The same settings
are required across all nodes (positive integer interval up to one day and
one to five repetitions). This uses Keepalived's native GARP mechanism, without
a cron job or a static mapping to one node. It does not prove every Wi-Fi
firmware will forward ARP correctly; validate idle-cache and failover behavior.
There is no permanent master. A bounded readiness script enters FAULT when
HAProxy has no open frontend or no healthy backend; at least one healthy backend
keeps ingress eligible. A failed local control-plane alone therefore need not
remove a working proxy serving another node. Deployment verification is stricter:
every proxy must see all three named backends UP. Cached `/healthz` validates the
local desired snapshot/assignment and does not require NATS to remain connected.

Management LAN devices can access Settings through `http://<Cloud VIP>/settings/`.
This foundation provides HTTP without authentication/TLS, using the existing
trusted-LAN Settings contract. It requires LAN/AP support for moving IPv4/GARP,
and bidirectional VRRP IP protocol 112 between the management peers. It does not
change firewall, Wi-Fi, private Ethernet, workers, providers or conversation
runtime. The control-plane status/UI still reports Cloud ingress as desired-only;
Ansible's address/service verification is what establishes actual VIP ownership.

After review, from a controller with direct management-LAN reachability:

```sh
ansible-playbook -i inventory.ini ingress.yml --check
ansible-playbook -i inventory.ini ingress.yml
ansible-playbook -i inventory.ini verify-ingress.yml
```

Select all three nodes; limited/partial deployment is rejected. Check mode makes
only read-only local status/health GETs, address queries and snapshot reads, then
renders candidates. It installs/downloads nothing, writes nothing, and changes no
services/sysctls. A static syntax check (`--syntax-check`) does not contact nodes.
During apply, package config validators run before candidate replacement;
HAProxy is started/reloaded and all three expected backends must be UP before
the validated Keepalived candidate is installed and its service explicitly
started/restarted. Keepalived has no restart handler: failed backend verification
cannot newly activate/restart it or replace its configuration. Changed
Keepalived configuration restarts the service; unchanged configuration only
ensures it is started. Rollout uses `serial: 1` with failure stopping further
nodes. Unchanged configuration does not
reload/restart services. Final read-only checks sample all three address lists
until exactly one VIP assignment is observed, require all three proxy backend
pools UP, and GET VIP `/healthz` and `/settings/` from the management controller. Health uses
a three-second request timeout; the Settings HTML transfer allows ten seconds
(`xiaozhi_ingress_settings_timeout`). Both checks use bounded
`xiaozhi_ingress_verify_retries` / `xiaozhi_ingress_verify_delay` retries and
require HTTP 200; repeated Settings timeouts still fail verification.

Failover is a separate, explicit service interruption exercise, never part of
normal apply or check mode:

```sh
ansible-playbook -i inventory.ini verify-ingress-failover.yml
```

It verifies the initial cluster, discovers the actual owner, stops only that
node's Keepalived, requires exactly one different owner and working VIP HTTP,
and restores the original service in an `always` block even when verification
fails. Successful completion rechecks the whole restored cluster. This proves
service-level failover, not a physical power-loss test.
If the controller is killed or loses SSH during the exercise, restore Keepalived
on the reported original node with `systemctl start keepalived` before repeating
verification.

For deployment acceptance, capture VIP-only ARP on the elected owner's discovered
management interface and confirm periodic announcements at the configured
interval. Check VIP HTTP from the management controller after a quiet period
longer than the LAN's ARP cache age; a brief health pass is not that proof.
Run the explicit failover exercise separately and check the new owner is unique,
HTTP recovers, and recovery does not force failback. Observe the management
router/AP behavior without pinning the VIP to one node's MAC.

Keep the current Cloud VIP outside the management DHCP allocation range to
prevent a later DHCP lease collision. Exclude the floating address from the pool;
a fixed DHCP reservation or static ARP binding to one node is unsuitable for
failover. DHCP pool administration is an operator/router task, outside this role.

`/etc/xiaozhi-ingress.json` records the public deployment VIP before configuration
replacement, including partial attempts. It is a safety guard, not Cloud desired
state. A rerun with a changed Cloud VIP stops before mutations; this iteration
does not perform live VIP migration, old/new two-phase cutover or runtime watching.
Changing Settings remains valid, but applying that change to existing ingress
requires a separately reviewed migration. Never delete this guard to force a
live cutover. Configuration changes other than the VIP can still be rerun.

Configuration references: [HAProxy 2.6 manual](https://docs.haproxy.org/2.6/configuration.html),
[Keepalived VRRP configuration](https://www.keepalived.org/documentation/keepalived-conf/),
and [Linux nonlocal binding](https://kernel.org/doc/html/latest/networking/ip-sysctl.html).

## Front panel: node identity and VIP ownership

The four-digit FD6551 panel uses two status cells on the left and a stable
two-digit node number on the right. The colon and clock icon stay off:

| Node | No session / no VIP | No session / VIP owner | MQTT session / no VIP | MQTT session / VIP owner |
| --- | --- | --- | --- | --- |
| deskb1x | `-- 01` | `iP 01` | `t- 01` | `tP 01` |
| deskb2x | `-- 02` | `iP 02` | `t- 02` | `tP 02` |
| deskb3x | `-- 03` | `iP 03` | `t- 03` | `tP 03` |

The lowercase `i` lights only the lower-right vertical segment, distinct
from the two-segment digit `1`. The space represents the physical gap between
the two digit pairs. Node numbers
are explicit `xiaozhi_panel_node_number` host variables, independent of VIP
election or IP changes. The lowercase `t` uses segments 0x78 and marks an established local MQTT-to-core
transport session. An idle persistent MQTT connection does not light it. If a
node also owns the VIP, `tP` preserves the ownership marker while that session
is open; goodbye, backend close or disconnect clears `t`. ASR/LLM/VLM/TTS
provider activity is not implemented yet. Existing Wi-Fi, private-LAN and USB indicators
are preserved.

Panel deployment is opt-in through `panel.yml`; `site.yml` does not import it.
It requires the existing Desk Box FD6551 image driver and both front-panel units.
Ansible checks the driver's known SHA-256 before changes; if the image driver
changes, review the GPIO mapping/API before updating the checksum. The wrapper
reuses that driver's register/GPIO functions and exclusive lock without copying
or replacing the image driver. Systemd drop-ins replace the clock entrypoint;
there is still only one continuous panel writer. The early-boot unit shows
`-- NN`, with all icons off, until the normal service starts.

The normal service checks local IPv4 addresses once per loop (one-second pause,
two-second address-query timeout). `iP` means the management interface actually
holds the VIP recorded in the applied ingress snapshot, normally
`/etc/xiaozhi-ingress.json`; it does not mean this node has permanent master
status or that backend health has been verified. Missing/invalid snapshots or a
failed local address query show `-- NN`. It reads no Cloud credentials, makes no NATS requests and never changes network
addresses or Keepalived state. It also reads `/status` from its own gateway at
its local private IPv4/health port, with a 0.3-second socket timeout and bounded
JSON. Proxies and redirects are not used. A matching `gateway_id`, ready status
and positive integer `active_websocket_sessions` light `t`. Missing/unreachable
gateway status clears the session indicator without disrupting VIP display.
Existing configs without gateway fields still work with VIP-only display.
Polling is about once per second; very short sessions can fall between polls.
No VIP address is pinned in the panel role. This indicator follows local
ownership after failover; it does not implement dynamic VIP migration.

```sh
ansible-playbook -i inventory.ini panel.yml --syntax-check
ansible-playbook -i inventory.ini panel.yml --check --diff
ansible-playbook -i inventory.ini panel.yml
```

Check mode reads existing driver/unit metadata and local address lists and renders
the public candidate; it writes no files/GPIO and changes no services. It does not
query the gateway in check mode. Apply
restarts the normal panel service only when its script/config/drop-ins change.
It enables the early unit for future boots without restarting that unit during
apply. Neither ingress services nor node power are changed. A panel-only rollout
may use `--limit deskb1x` first. Check visually that the two digit pairs and `iP`
are correct before rolling out to the other nodes; physical panel validation is
operator-owned.

Logic fixtures run without GPIO access, network connections or service changes:

```sh
python3 -m unittest discover -s roles/xiaozhi_panel/tests -v
```

To restore the image clock, remove only the two `10-xiaozhi-node.conf` drop-ins
installed by this role, run `systemctl daemon-reload`, then restart
`deskbox-front-panel.service`. Do not run the image clock manually alongside the
node panel. `site.yml` continues to leave this opt-in choice alone.

## Symmetric MQTT gateways and session indicator

All three nodes run the same gateway; none is a permanent MQTT master. Deployment
is opt-in through `mqtt-gateway.yml`, which preflights all three nodes before its
serial rollout. `site.yml` does not import it. The gateway is the existing Xiaozhi
MQTT/UDP transport bridge, not a general persistent broker or NATS worker.

```text
Robot MQTT TCP -> Cloud-owned VIP:1883 -> HAProxy: remote gateways first
                                         | -> deskb1x gateway
                                         | -> deskb2x gateway
                                         ` -> deskb3x gateway
Selected gateway hello -> its own management IP:1883 for encrypted UDP
Gateway -> existing conversation core WebSocket
```

Gateway MQTT binds to each private `10.10.10.11/12/13:1883`; health/status binds to
that same private address on 8007. UDP binds to the corresponding primary
management `192.168.1.101/102/103:1883`, and `PUBLIC_IP` advertises that gateway's
own address. MQTT TCP and its UDP session therefore reach the same process.
The node holding the VIP can differ from the gateway handling the robot.

Legacy external-core mode requires a usable **normal conversation core**. For the
new isolated cluster transport mode, use the opt-in core rollout described below.
The standalone control plane on 8004 serves Settings only; it is not a WebSocket conversation backend,
robot bootstrap or camera/VLM endpoint. Configure the actual existing core WS/WSS
URL including its path, without credentials, queries or fragments. The gateway
adds its existing sole `?from=mqtt_gateway` marker. A localhost backend is valid
only if that core actually runs on each gateway node. Apply checks backend TCP
reachability before installation; it does not prove authentication or providers.

Copy [examples/mqtt-gateway.vars.yml](examples/mqtt-gateway.vars.yml) outside the
checkout and fill the core backend URL list. The reviewed gateway source is pinned
to `8ac1cec4637b4c65f1122c808bb95154fbcd6d51`, which provides explicit
MQTT/UDP/HTTP listener binding and `listener_ports` status metadata. The empty
backend default rejects an unconfigured apply before host changes. Future source
updates require a reviewed/pushed full 40-character SHA. All three nodes must use
the same source/runtime/backend/port settings.

The role installs the official ARM64 Node.js 24.21.0 runtime in a private versioned
directory with its [published SHA-256](https://nodejs.org/en/blog/release/v24.21.0).
It leaves global Node installations alone. Dependencies come from the committed
lockfile via `npm ci --omit=dev --ignore-scripts --no-audit --no-fund`; current pure
JavaScript dependencies require no lifecycle build scripts. Systemd runs a
root-owned release as a dedicated unprivileged user, with SIGTERM drain and
bounded stop time. Unchanged source, dependency stamp, config, environment and
unit do not restart the gateway. Changes may interrupt sessions; this role does
not implement automatic rollback or session replication.

Provision the **existing core keys** through encrypted `vars/vault.yml` using the
operator's normal Vault workflow; do not generate unrelated gateway keys:

| Encrypted variable | Gateway environment | Must match |
| --- | --- | --- |
| `vault_mqtt_signature_key` | `MQTT_SIGNATURE_KEY` | Core `server.mqtt_signature_key` |
| `vault_mqtt_server_secret` | `SERVER_SECRET` | Core `server.auth_key` |

Secrets are written to `/etc/xiaozhi-mqtt-gateway.env` as root:root 0600 and never
diffed or printed. Secure MQTT authentication remains enabled. No secret belongs
in the public variables file. MQTT is plain TCP on the trusted LAN; do not expose
it through WAN port forwarding.

`mqtt-ingress.yml` separately adds a TCP frontend on the current frozen Cloud VIP,
using `/etc/haproxy/xiaozhi-mqtt.cfg` and a systemd drop-in that loads both the
existing Settings configuration and the new MQTT fragment. Ordinary ingress and
full-site reruns keep loading the fragment. Ansible still does not own the VIP.
All three local gateway statuses must be ready with matching identity/listener
ports before any HAProxy change. Every serial node rechecks Cloud revision/VIP
and all gateway statuses before mutation. The fragment is validated together
with the existing base configuration. Initial activation restarts HAProxy to
change its startup arguments and can briefly interrupt Settings access; later
fragment-only changes reload it. Unchanged files cause no reload/restart.

Each HAProxy marks its **own local gateway as `backup`** and gives the two remote
gateways equal round-robin weights. Only the node owning the floating VIP receives
new frontend connections, so this keeps new MQTT sessions off the Settings VIP
owner while either remote gateway is healthy. For example, when deskb1x holds the
VIP, deskb2x/deskb3x receive new sessions; deskb1x is used only if both remote
gateways are unavailable. When VIP moves to deskb2x, its existing configuration
already prefers deskb1x/deskb3x. No leader is pinned and no ownership watcher,
NATS event or config rewrite is needed on failover.

This uses HAProxy's [backup server semantics](https://docs.haproxy.org/2.6/configuration.html#5.2-backup)
and `/readyz` checks on private port 8007. Busy-load thresholds are not implemented:
a gateway with a session remains eligible while ready. Existing sessions are not
moved to enforce separation; this policy applies to **new TCP connections**, and
a fallback session stays on the VIP owner's gateway until it ends or reconnects.
A persistent MQTT connection stays on its chosen
gateway across multiple turns. Each hello opens a transport session, and the
panel shows `t- NN` or `tP NN` while that local backend bridge is established;
it returns to `-- NN` or `iP NN` when the session ends. This is transport state,
not ASR/LLM/VLM/TTS compute activity. One robot does not use all three gateways
simultaneously. `t` is not a promise that a provider is responding successfully.

Existing Keepalived election/health policy is unchanged: Settings availability
continues to govern VIP ownership, and gateway readiness governs TCP backend
selection. A gateway failure does not need to move the VIP; subsequent MQTT
connections can use another healthy gateway. Node/VIP failure interrupts TCP,
then firmware reconnects and establishes a fresh hello/UDP key/session. Existing
sessions are not migrated, and core-server HA is not provided here. Firmware's
MQTT reconnect timing remains unchanged: its current disconnect timer is 60
seconds and attempts reconnect only when the application is idle. Actual recovery
may take longer and must be measured on the robot; rapid automatic recovery is
not yet proven. All gateways down means MQTT unavailable while Settings can remain
available.

After source review/push and encrypted provisioning, run these **separate** steps:

```sh
ansible-playbook -i inventory.ini mqtt-gateway.yml --syntax-check --ask-vault-pass
ansible-playbook -i inventory.ini mqtt-gateway.yml --check --diff --ask-vault-pass -e @/path/to/mqtt-gateway.vars.yml
ansible-playbook -i inventory.ini mqtt-gateway.yml --ask-vault-pass -e @/path/to/mqtt-gateway.vars.yml
ansible-playbook -i inventory.ini mqtt-ingress.yml --check --diff
ansible-playbook -i inventory.ini mqtt-ingress.yml
ansible-playbook -i inventory.ini panel.yml --check --diff
ansible-playbook -i inventory.ini panel.yml
```

Check mode only validates inputs, reads existing local metadata/status and renders
candidates in memory. It installs/downloads nothing, writes no host files and
changes no services. MQTT ingress check mode requires the gateways/base ingress
to already be installed; it cannot simulate their network readiness. Apply
verifies local gateway status and all three HAProxy MQTT backend pools plus
retained Settings/VIP/Cloud invariants. These prove deployment readiness, not a
working robot turn. No Cloud body or bootstrap setting is automatically edited.

Operator acceptance:

1. Through the existing core configuration workflow, set `server.mqtt_gateway` to
   the **current Cloud VIP plus `:1883`** (initially `192.168.1.186:1883`). Keep the
   real core HTTP/bootstrap URL and `/mcp/vision/explain`; Settings does not serve
   those routes. Ensure the matching signing/auth keys are provisioned.
2. Have the robot fetch bootstrap. Verify MQTT + hello, UDP audio both ways and
   one full ASR/LLM/TTS turn. The selected gateway panel should show `t-`/`tP`
   while the session is open, then clear `t` when it closes.
3. Confirm abort/reconnect, device MCP `tools/list` and a real `tools/call`, plus
   camera capture/explain through the **core Vision HTTP** path. MQTT does not
   proxy that HTTP image upload.
4. On deliberate new TCP connections, verify only the two gateways other than
   the current VIP owner receive sessions while either is healthy. With both
   remote gateways unavailable, verify the owner's local backup accepts a new
   session. After VIP changes owner, verify new connections prefer its two peers.
   Restore remote gateways and check new connections use them again; established
   fallback sessions may remain local. Separately stop/unplug the active gateway or VIP owner and verify reconnect,
   fresh hello/UDP session and another complete turn. Record recovery time; code
   inspection cannot prove timing or physical failover behavior.

No firmware build/flash is required solely for these deployment settings. Accept
this transport baseline before remote provider execution. Runtime/core endpoints
and encrypted keys must be supplied before live deployment; no deskbox mutation
is implied by preparing this code. Offline logic fixtures open no sockets:

```sh
python3 -m unittest discover -s roles/xiaozhi_mqtt_gateway/tests -v
python3 -m unittest discover -s roles/xiaozhi_mqtt_ingress/tests -v
python3 -m unittest discover -s roles/xiaozhi_panel/tests -v
```

## Symmetric transport cores and panel ownership

`core.yml` is opt-in and serial; `site.yml` remains unchanged. Every deskbox gets
an isolated `xiaozhi-core` process on its private address, port 8000. This runs
`core_server.py` with only `requirements-core.txt` (aiohttp), not `app.py` or any
provider. The role requires a reviewed full server SHA, root-owned release/venv,
a dedicated unprivileged user and the same encrypted `vault_mqtt_server_secret`
used by the gateways. Check mode only validates and renders; it does not create
accounts/directories, install/download, start listeners or change services.

Core admission uses the existing gateway HMAC bearer contract plus the three
private gateway source-IP allowlist. Active session counts start only after a
valid hello and clear on disconnect/shutdown. The core observes actual local VIP
ownership from the public applied ingress snapshot and management interface; it
neither owns nor pins Cloud VIP state. Missing ownership observation reports
unknown and prevents gateway selection of that core.

Set `xiaozhi_mqtt_gateway_cluster_core: true` in the external public vars file.
The role derives three WS/status endpoints from the symmetric inventory; legacy
mode defaults to false and continues to use explicit external `chat_servers`.
Cluster mode requires a new reviewed gateway SHA containing `core-selector.js`;
the previous a7c6826 source is intentionally rejected before mutation. All three
core status identities/readiness/VIP observations are checked before gateway
changes. A mode-specific list cannot select outside its declared core nodes.

At each new gateway hello, concurrent bounded probes rank cores as:

1. Other node, not the observed VIP owner.
2. Other node holding VIP, if the preferred core cannot establish a session.
3. Local core, only after remote establishment fails (VIP local is the last tier).

Equal peers rotate from randomized startup position. With all three nodes healthy
and gateway already placed away from VIP, the third node is the preferred core.
This is not a permanent assignment: a node can own several sessions, and fallback
can combine roles. No busy threshold is implemented. Multiple reported VIP owners
block new core selection. VIP observation is sampled once per second, so a role
transition is not atomic with hello. Established sessions keep their core through
VIP/config changes; a failed core requires a fresh session, with no replication.

Panel marks an established local core session as **`C- NN`**, or **`CP NN`** when
also holding VIP. Uppercase C lights top, both left and bottom segments. Gateway
remains `t- NN`/`tP NN`; idle stays `-- NN`/`iP NN`. If local gateway and core are
both active without VIP, show `tC NN` continuously. When all three roles are
active, alternate `tC NN` and `iP NN` each polling cycle. The colon remains off. Local bounded status polls verify protocol,
identity, readiness and a positive integer session count; errors clear the role
indicator. Provider execution is not represented by either transport indicator.

The reviewed transport core is pinned to `ba4c5ae4729eb3ac27a726cbcedc9d6831eafebe` and gateway to
`8ac1cec4637b4c65f1122c808bb95154fbcd6d51`. Copy the public example outside the checkout
and use it for the opt-in cluster rollout:

```sh
ansible-playbook -i inventory.ini core.yml --check --diff --ask-vault-pass -e @/path/to/mqtt-gateway.vars.yml
ansible-playbook -i inventory.ini core.yml --ask-vault-pass -e @/path/to/mqtt-gateway.vars.yml
ansible-playbook -i inventory.ini mqtt-gateway.yml --check --diff --ask-vault-pass -e @/path/to/mqtt-gateway.vars.yml
ansible-playbook -i inventory.ini mqtt-gateway.yml --ask-vault-pass -e @/path/to/mqtt-gateway.vars.yml
ansible-playbook -i inventory.ini mqtt-ingress.yml
ansible-playbook -i inventory.ini panel.yml
```

The isolated core is **transport-only**: hello/session/ping, framing validation
and incoming audio counters. It counts/discards audio and returns an explicit
conversation-runtime-unavailable error for unsupported requests. It does not
claim ASR/LLM/VLM/TTS/MCP support, NATS worker dispatch, bootstrap/OTA or Vision.
Transport readiness is not conversation readiness. Deployments must not repoint
robot bootstrap/vision to this process. Preserve the existing `app.py` runtime
until the future conversation coordinator, bootstrap and worker RPC are ready.
Physical panel and robot checks remain operator-owned.
