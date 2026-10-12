# DNS health and safe node maintenance

Runtime source commits: server `7f757cd4`, MQTT gateway `ce8929a`.

This change is source-only pending operator verification. No tests, Go build,
provider requests, service deployment or physical reboot were run for this feature.

## Dashboard DNS health

The dashboard reads `status.desk-dns.test` TXT on each frontend and both DNS VIP
families every 15 seconds. dnsdist answers this reserved name locally with a
bounded eligibility mask from its actual backend health states. Normal DNS pool
selection/fallback rules stay unchanged; no management API or credentials are
exposed. The name is limited to local/private sources (IPv6 link-local sources
may also read the nonsecret status). It is not forwarded to AdGuard/upstream,
so it does not appear as a device query in AdGuard logs or backend activity.

The UI shows IPv4/IPv6 reachability from the three nodes, the elected owner's
probe latency, eligible AdGuard backends, and a warning when all AdGuard nodes
are down and the existing Cloudflare emergency pool is selected. Probe latency
is frontend probe latency, not measured device-query/upstream latency. IPv4
probes from peer nodes exercise normal ARP resolution. Local owner probes alone
cannot prove LAN reachability. Unknown/stale telemetry stays unknown.

Probe success confirms frontend reachability and exposes the current pool
eligibility; it does not prove that upstream Internet resolution is working.

The DNS VIP, IPv6 VIP and frontend test port remain the current deployed
`.250`, `fe80::250` and `5353` contract; the fallback label is the existing
`1.1.1.1` policy. Updating those values requires changing the collector pins too.

## Maintenance actions

Node details offer Maintenance, Reboot node, and Resume while maintenance is
active. Each start/reboot uses a fresh signed confirmation bound to node, boot,
action and expiry; Resume also asks for confirmation. Both surviving nodes hold
an exclusive durable lease. Only one node can enter maintenance at a time.

1. The controller writes a root-owned boot-local admission flag under
   `/run/desk-node-maintenance/`. It contains only node/boot/expiry metadata.
2. Worker removes only new-job NATS queue subscriptions. Existing ASR audio,
   vision chunks, stream ACK/cancel and TTS PCM pull paths remain available.
   An admission ACK already in flight survives NATS consumer cancellation.
   New targeted TTS admissions report busy so other workers are selected.
3. Core rejects new WebSocket sessions; gateways exclude paused cores, reject
   new MQTT connections/hellos and close only idle MQTT transports without a
   bridge. Active bridges and UDP conversations are preserved.
4. Controller waits for acknowledged pause, zero ASR/LLM/VLM/TTS jobs and zero
   core/MQTT connections. It checks the two survivors remain healthy, synced
   and holding the same lease. A busy conversation is allowed up to four minutes
   to finish naturally; timeout resumes admission instead of forcing a reboot.
5. Only after drain, controller stops the previously active `keepalived` and
   `desk-dns-vip` election services and waits for both VIPs to have exactly one
   healthy surviving owner. DNS backends remain running during this stage.
6. Maintenance-only stays paused with Resume and a separately confirmed Reboot
   available. Reboot mode reboots only after handoff. Native backend checks
   continue controlling DNS backend eligibility.
7. Maintenance expires after ten minutes. Controller restores admission and
   previously active election services on cancel/expiry or return after reboot.
   Worker/core/gateway validate the flag's boot and expiry independently, so
   a stale flag cannot indefinitely block admission. VIP services still use
   their existing readiness probes; recovery does not force VIP ownership.

No protocol/firmware changes, new daemon, database or JetStream are added.
The existing root node controller gains one root-owned RuntimeDirectory and
fixed start/stop actions for two known services; telemetry remains read-only.

## Operator verification

Small offline checks before any rollout:

```sh
# Cluster repository: fake systemctl/reboot; no real node operations.
python3 -m unittest discover -s roles/desk_node_control/tests
python3 -m unittest discover -s roles/desk_dashboard/tests
GO111MODULE=off go test ./roles/desk_dashboard/files
ansible-playbook -i inventory.ini dns-health.yml --syntax-check
# Server repository: use its existing venv; fake NATS/providers.
cd ../xiaozhi-esp32-server/main/xiaozhi-server
for suite in worker_asr worker_llm worker_llm_stream worker_tts_segments voice_transport; do
  python -m unittest discover -s tests -p "test_${suite}.py" || break
done
```

After offline results, deploy server worker/core and MQTT gateway revisions
serially with no active session on the restarting node, then deploy the
collector/controller/page with `desk-dashboard.yml`. Maintenance stays disabled
until all nodes report support and healthy/synced state. The separate
`dns-health.yml` validates DNS candidates and adds only the metadata rules,
serially. It neither rebuilds filters nor modifies resolver/policy/VIP settings.
Do not deploy a new controller/page before the runtime revisions support pause.

One physical acceptance run then needs a real conversation: enter maintenance
on its node and verify it finishes, peers receive new jobs, both VIPs move,
Resume recovers admission, and confirmed reboot returns healthy/synced. Also
verify blocked/long conversations time out without forced reboot. These checks
remain NOT RUN until reported by the operator.
