# Core NATS

The three deskboxes form `desk-cluster` over their private `cluster_ip` addresses.
`site.yml` applies the `nats` role serially, checks each local service, then runs
read-only cluster verification. NATS is the internal RPC/stream-control transport;
MQTT gateways are separate transports. JetStream is disabled: messages for an
offline subscriber are not persisted.

## Package, listeners and credentials

`roles/nats/defaults/main.yml` pins nats-server 2.15.0 and the official ARM64 Debian
package checksum `7e37abbab18910ebc6d7a4733546d061d1098c7ee35881a92ce9d451f5804d92`.
The role verifies Debian/ARM64, checks cached downloads, installs only the pinned
package and holds it against automatic upgrades. Update version, URL and checksum
assertions together. Matching packages/configuration cause no restart.

| Listener | Private port |
| --- | --- |
| Application clients | 4222/TCP |
| Authenticated server routes | 6222/TCP |
| HTTP monitoring | 8222/TCP |

All listeners bind the node's private IP; routes derive both peers from inventory.
Preflight requires distinct `10.10.10.0/24` addresses on the private interface.
`max_payload` is 8 MB, debug/trace are disabled, and each server name is its
inventory hostname. No listener is exposed on Wi-Fi by the role.

Encrypted `vars/vault.yml` supplies `vault_nats_client_password` and
`vault_nats_route_password`; shared vars map them to the role. Application and
route usernames must differ. Route credentials permit ASCII letters/digits and
`._~-` only because they enter route URLs. Candidate configuration and secret
assertions use `no_log`, with secret diffs disabled.

The application identity is unrestricted. Optional Home Assistant and Network
scan identities use scoped subjects and response allowances. With their shared
flags enabled, NATS template rendering requires the existing ignored
`secret/homeassistant/nats.json` and `secret/network/scan-nats.json`; retain them
for full-site convergence. Do not regenerate credentials on a running cluster.

The physically private Ethernet fabric is the trust boundary: NATS traffic and
credentials are not TLS-encrypted; monitoring HTTP is unauthenticated. Do not
route these ports to untrusted networks. Service-specific signed/encrypted RPC
contracts do not make all NATS traffic confidential.

## Service and recovery

Ansible owns `/etc/nats-server.conf` (`nats:nats`, 0600), the managed systemd unit
and the `/usr/sbin/nats-server` symlink to the packaged binary. The dedicated
unprivileged account has no home/login, capabilities or persistent message store.
Systemd validates config before startup, uses a read-only filesystem and journals
logs. Failure restarts after three seconds.

Candidates pass `nats-server -t -c` before replacement; ordered handlers validate
again before restart. A failed candidate stops convergence without restarting
with invalid config. SIGUSR2 uses a five-second grace and thirty-second lame-duck
drain inside a forty-five-second stop timeout. Avoid taking multiple members
down together; lost in-flight Core NATS requests are not replayed by storage.

Inspect `systemctl status nats-server`, `journalctl -u nats-server` and the private
`/healthz` / `/routez`. Restore matching configuration/credentials through Ansible,
one node at a time. There is no JetStream recovery directory to restore.

## Verification

`roles/nats/tasks/verify.yml` checks active systemd state, private HTTP health,
and exactly the two expected peer names/IPs plus distinct remote IDs. Route
connections can be pooled, so `num_routes == 2` is not the peer-count contract.
Bounded retries allow startup convergence; verification changes no service.

```sh
ansible-playbook -i inventory.ini site.yml --syntax-check --ask-vault-pass
```

Syntax checking does not contact nodes. Without Vault access, structural checks
can use a temporary copy omitting `vars_files`; they cannot prove secret
resolution. Preserve the real encrypted Vault and playbook.
