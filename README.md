# Xiaozhi Desk Cluster

Ansible owns the three RK3528 desk boxes. Wi-Fi provides SSH management and
Internet access; the `end0` Ethernet fabric is the private cluster LAN. Do not
configure node identity, networking, SSH or NATS manually outside Ansible.

| Node | Management address (`inventory.ini`) | Private address (`host_vars`) |
| --- | --- | --- |
| `deskb1x` | `192.168.1.101` | `10.10.10.11` |
| `deskb2x` | `192.168.1.102` | `10.10.10.12` |
| `deskb3x` | `192.168.1.103` | `10.10.10.13` |

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

| Listener | Port | Bind address on each node |
| --- | --- | --- |
| Application clients | `4222/TCP` | `cluster_ip` |
| Authenticated server routes | `6222/TCP` | `cluster_ip` |
| HTTP monitoring | `8222/TCP` | `cluster_ip` |

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

The final play includes `roles/nats/tasks/verify.yml`, a separate read-only entry
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
