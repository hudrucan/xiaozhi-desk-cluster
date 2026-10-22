# NTFY

Three RK3528 peers run native ARM64 `ntfy-rs`, a Rust HA auth adapter, and the
same pinned official static Web UI with a topic-API compatibility patch. Static
files are served by the existing Rust adapter. Ansible owns TOML and static
configuration; runtime users, password hashes, token identities and ACLs are
replicated as logical auth state in the encrypted journal.

In the Web UI, open **Server users** in the navigation (`/server-users`) and authenticate as an existing
administrator. This manages cluster users, password resets, topic ACLs and bearer
tokens through the Rust quorum API. Create a regular user, then grant Read on
`desk-events` for an iPhone subscriber. Admin credentials stay in page memory;
existing password hashes and bearer tokens are never listed. Copy newly issued
tokens when displayed. A password change preserves identity, tokens and ACLs;
update that password in each client separately. After an unacknowledged Save,
refresh and inspect the state before retrying; do not automatically reissue tokens.

## Access and HA

Open `http://192.168.1.186:2586/` directly or through the dashboard's NTFY card.
This is the existing management VIP, managed by Keepalived. HAProxy prefers the
local ready replica and falls back to healthy peers; there is no NTFY-specific
leader. The base URL stays unchanged through failover, including for the ntfy
iPhone app over WireGuard.

An auth Save succeeds only after at least two of three replicas commit **and
apply**, including the receiving replica. Returning replicas must catch up before
readiness permits ingress traffic. Registration and self-service ACL changes are
blocked; administrative mutations go through the Rust quorum API.

## Build, deploy and recover

Run from the repository root with the existing inventory and `secret/ntfy/`.
The build needs Rust, the ARM64 Linux target/linker, Python 3, Node.js and npm.
For cross compilation, configure the ARM64 C linker or use the supported
`DESK_NTFY_ZIG=1` option with `cargo-zigbuild` and Zig installed.

```sh
scripts/build-ntfy.sh /private/tmp/desk-ntfy-package
ansible-playbook -i inventory.ini playbooks/notifications/ntfy.yml \
  -e desk_ntfy_package=/private/tmp/desk-ntfy-package
```

The build pins upstream sources and checksums, builds the official frontend, and
packages the native binary and static assets. Deployment verifies the ARM64 ELF
and artifact digests before activation. The playbook requires all three members.

For existing journals, the playbook first starts stopped services using
`state: started`, leaving active peers running. All start operations finish before
readiness waits, so **0/3, 1/3 and 2/3 active** can recover quorum before any rolling
restart. **3/3 active** proceeds without recovery restarts. Upgrades then use
`serial: 1`, requiring each replica to become ready before the next update.
Fresh bootstrap installs and starts all three replicas before waiting for quorum.
Use the same playbook and a known-good package for recovery; do not reinitialize
auth state. Missing journal/configuration requires investigation and trusted
backups, not a new seed.

On each node, verify `curl -fsS http://127.0.0.1:2590/ready` returns HTTP 200 with
`{"ready":true}`. This covers auth freshness/apply and native child health;
`systemctl is-active desk-ntfy` alone is insufficient. Checking `/ready` through
the VIP verifies only the selected backend, not every replica.

## Publishers and Events

`desk-beszel` and `desk-homeassistant` have separate write-only publisher tokens
for `desk-events`, stored in ignored `secret/ntfy/publishers.json`. Issue or revoke
them through the administrative quorum API; do not modify SQLite directly.

```sh
ansible-playbook -i inventory.ini playbooks/notifications/ntfy-integrations.yml
```

Beszel uses its native notification integration; only the management VIP owner
sends automatic alerts to avoid delivery from all three hubs. The active HA Core
forwards `persistent_notification` added/updated events. The integration playbook
checkpoints HA only when its staged package or hook changes. Pi Events consumes
notifications through the existing bounded, polling, read-only dashboard API.

## Limits and rollback

- Message history is a local SQLite cache, best-effort and unreplicated. Failover
  may lose history/messages even though auth identities are preserved.
- Web UI preferences, subscriptions and connection credentials live in the
  browser; they are not synchronized to other browsers/devices. Its “Manage
  users” section manages client credentials, not cluster accounts.
- The public endpoint is HTTP. Viewing/publishing works, but browser OS
  notifications require HTTPS. HTTPS/Web Push and real iPhone/APNs delivery have
  not been verified; `upstream-base-url` is configured as `https://ntfy.sh`.
- Before deployment, retain the prior binary, static release link, NTFY ingress
  and configuration, plus protected backups of `secret/ntfy/`, node
  `/etc/desk-ntfy/` and `/var/lib/desk-ntfy/journal.enc`. Never copy live SQLite.
  Roll back compatible code/static configuration on affected nodes while keeping
  the latest journal, seed, credentials and token identities. Never reset or
  restore an older journal to make a binary start; require catch-up/readiness
  before serving traffic again.
