# VPN UI and shared configuration

`http://192.168.1.186/vpn/` provides the wg-easy v15.3.0 client UI through the existing UI VIP. The dashboard title is VPN; WireGuard identifies its transport. Each node runs an unprivileged native Node service, with no Docker daemon. UI replicas are independent of the owner of VPN VIP `192.168.1.254`.

The public route is limited to the existing trusted LAN/VPN ingress. Browser requests do not contain the internal administrator password or native session cookie. The adapter logs into wg-easy using its ordinary session API on loopback and strips browser authentication headers. Mutations require a same-origin request. The native service has no network-administration capabilities or external network access.

## Configuration path

A mutation runs under a local lock through wg-easy validation, then exports a bounded logical SQLite snapshot. Infrastructure and authentication rows are pinned; client keys, routes and hooks are validated before replication. The encrypted configuration-only Paxos journal requires two independent accept acknowledgments, carries forward interrupted proposals and verifies commit certificates. An acknowledged configuration is retained if a replica temporarily cannot apply it; the UI reports Applied/Pending rather than suggesting the chosen update was lost.

Peers reconcile on a ten-second wakeup or commit notification. Stable reconciliation does not rewrite SQLite or the journal. Native session health is cached for ten seconds; cluster status polling stops when its page is hidden. Peer RPC is authenticated with replay-protected HMAC over TLS on the private network. SQLite updates run transactionally as the native UI account; live databases are not copied. `wg syncconf` updates only the existing tunnel configuration, retaining sessions of unchanged peers. The readiness helper verifies the exact configured peer set and rejects pending or stale apply status. Returning nodes catch up before declaring eligibility; an unconfirmed fresh seed needs another member. A previously committed checkpoint can continue serving when writes lack quorum.

The original server identity and iPhone keys remain intact. New clients use PSKs. Endpoint recovery preserves each peer's PSK through a private stdin pipe and retains its keepalive, without storing transport sessions or putting keys in command arguments. Expected membership reloads from the applied public configuration.

## Supported UI scope

- Create, rename/edit, enable, disable and delete clients.
- Download client configuration or QR code; endpoint is `vpn.huynm.dev:51820`.
- Full IPv4 LAN split tunnel: `10.77.0.1/32, 192.168.1.0/24`.
- Edit client/default DNS and client keepalive. The configured MTU is 1380.
- View owner handshake/transfer telemetry and per-node configuration progress.

Server key, port, tunnel CIDRs, firewall and shell hooks remain cluster-managed. Account/TOTP changes, expiry and one-time sharing are not exposed. The bounded initial deployment supports at most 32 clients. Existing iPhone profiles do not need re-importing.

## Reproduce and deploy

```sh
python3 scripts/build-vpn-ui.py --node /path/to/node --pnpm /path/to/pnpm
python3 roles/desk_vpn_ui/files/seed.py --repo "$PWD" --upstream /private/tmp/desk-wg-easy-build --node /path/to/node
ansible-playbook -i inventory.ini vpn-ui.yml
```

The build pins upstream commit `2dc8ba779216929c10c1998341d36963fe0eca7a`, applies the tracked patch and installs the frozen dependency lock. Node ARM64 archives use official SHA-256 checksums; native npm packages use registry integrity checks. The exact modified upstream source and license ship at `/opt/desk-vpn-ui/app/wg-easy-source.tar.gz`. The native build has no authentication bypass patch.

Keep ignored `secret/vpn-ui/` and the existing ignored tunnel credentials backed up. Shared encryption/session identity must survive a rebuild. Journal state is under `/var/lib/desk-vpn-ui-sync/`; native SQLite is under `/var/lib/desk-vpn-ui/`. Foundation `vpn-ha.yml` retains UI-managed peers instead of restoring the original one-peer seed.

`vpn-ui.yml` preserves protected Xiaozhi/DNS runtime state and validates both HTTP and MQTT HAProxy fragments before a graceful reload. Other UI deployments retain the VPN route. CPU quotas cap native UI at 35% of one CPU and the adapter at 20%; the existing idle workers retain priority. Credential namespaces are excluded from dashboard and node-control metadata services.

Rolling upgrades stage the full hashed asset set on every replica before restarting any application. The adapter serves staged assets directly, retaining hashes used by existing browser sessions; UI affinity reduces cross-version requests. HAProxy retries failed reads on another replica, while Save/CRUD methods disable layer-seven retries to avoid duplicate mutations.

## Offline regression tests

With the existing Python cryptography dependency installed, run:

```sh
python3 -m unittest discover -s roles/desk_vpn_ui/tests -p 'test_*.py' -v
```

The tests use the real SQL migrations bundled under `tests/fixtures/wg-easy/`,
pinned to the same revision as the build script and checked against recorded
SHA256 hashes. Databases and synthetic keys are created only in temporary
directories. No upstream checkout, network access or production credentials are
needed. An explicit `WG_EASY_SOURCE` still selects an external source checkout;
missing migrations fail instead of silently falling back to the bundled fixture.
See the fixture README for provenance, licensing and pin-update instructions.

## Validation recorded on 2026-10-14

PASS: seven focused sync/asset tests, seven ingress preservation checks, existing topology and timeline regressions, Go readiness and PSK recovery tests, and Ansible syntax checks. PASS: real create/edit/export QR/config/disable/enable/delete from different nodes; temporary peers removed and the original iPhone retained. PASS: native UI stopped on node 1 with its affinity cookie; HTML/client API/bundle requests continued through other replicas. PASS: a Save with node-3 adapter offline was chosen by the other two; node 3 caught up on restart, then the temporary peer was deleted.

All three native UIs and adapters are active at revision 7 with three Applied acknowledgments, and native VPN readiness passes on each node. Thirty-second measurements with the VPN page closed showed combined native UI plus adapter CPU of 1.53–2.08% of one CPU, approximately 0.38–0.52% of a four-core node; combined memory was 71–91 MiB. This is a short idle sample, not a load-test guarantee. The existing real iPhone WAN failover was previously validated; no power-cut or new-PSK-client WAN failover was performed in this UI rollout.

The initial mixed-version rolling upgrade caused missing dynamic JavaScript bundles and a blank page. Reproduced per-node 404s identified the cause. The fixed deployment stages immutable bundles on all three replicas before code changes, and the adapter serves those bundles without depending on the running Nitro manifest. The current page renders its original client and the 3/3 Applied state.
