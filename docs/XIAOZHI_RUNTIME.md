# Xiaozhi runtime and deployment

Three separate layers run under distinct systemd identities: control-plane
Settings/Cloud reconciliation, MQTT/UDP gateways and conversation cores/workers.
Source revisions and Cloud/runtime configuration revisions are different values.
Role defaults and explicit deployment vars are authoritative; private operator
overrides are required to describe a particular installed voice deployment.

## Foundation and configuration

`site.yml` installs NATS, `xiaozhi-worker`, `xiaozhi-control-plane` and ingress.
The explicit inventory groups select membership. Services use root-owned pinned
Git checkouts and isolated venvs. Default workers provide ping only; the default
core is transport-only. A full-site run without voice overrides can restore that
default. Keep the same reviewed overrides on subsequent convergence.

The Settings process binds the private address on port 8004 and reconciles Cloud
every 45 seconds. It does not initialize providers or conversation models.
Vault supplies `vault_nats_client_password`, `vault_xiaozhi_cloud_folder_id`,
`vault_xiaozhi_cloud_manifest_file_id` and `vault_xiaozhi_cloud_credentials`.
The optional `vault_xiaozhi_cloud_memory_manifest_file_id` retains the common
Memory authority; do not create a different manifest per node.

Managed roots are `/opt/xiaozhi-control-plane`, `/opt/xiaozhi-worker` and
`/opt/xiaozhi-core`; provider opt-ins use versioned release roots. Credentials and
bootstrap are private; only explicitly allowed service data/cache paths are
writable. Never copy developer runtime data onto a node. Public defaults and
`examples/` contain no keys. Apply with the normal Vault workflow:

```sh
ansible-playbook -i inventory.ini site.yml --tags control-plane --limit xiaozhi_control_planes --ask-vault-pass
ansible-playbook -i inventory.ini site.yml --tags worker --limit xiaozhi_workers --ask-vault-pass
```

Control-plane convergence requires the three assigned members, successful live
Cloud reconciliation, shared V2 authority/revision and NATS connectivity. Cached
HTTP health alone does not prove Cloud access. See [OAuth recovery](CLOUD_OAUTH.md).

## Ingress and MQTT

`ingress.yml` reads one agreed desired VIP/revision from all control planes,
freezes it before mutation and checks it again during serial apply. The interface
comes from the inventory management address. HAProxy serves the VIP; unicast
Keepalived VRID 51 uses `nopreempt`. Backend health needs at least one usable
Settings backend; deployment verification requires all three.

`/etc/xiaozhi-ingress.json` records the applied VIP, including partial attempts.
A changed desired VIP stops a rerun before mutation. Do not delete that guard to
force migration. Keep the address outside router DHCP allocation; router changes
are manual. The [Wi-Fi driver](DESK_WIFI_DRIVER.md) must be selected and running
before VIP activation. Owner-only GARP and peer ARP announcements support moving
addresses; adapter/MAC replacement requires fresh ingress preflight.

```sh
ansible-playbook -i inventory.ini ingress.yml
ansible-playbook -i inventory.ini verify-ingress.yml
```

`verify-ingress-failover.yml` is an explicit interruption exercise: it stops the
current owner's election service, checks a new unique owner and restores the
old service in `always`. It is never part of routine apply. If the controller is
lost, restore `keepalived` on the original owner before repeating.

The gateways use private MQTT TCP `:1883`, private health `:8007`, and each node's
management IP `:1883` for encrypted UDP. HAProxy's MQTT fragment prefers the two
remote gateways and uses its local gateway as backup. This applies to new TCP
connections; established sessions stay attached until disconnect/failure.
Marked-down backends close old TCP sessions so firmware can reconnect.

Use `mqtt-gateway.yml` then `mqtt-ingress.yml`. Gateway configuration supports
explicit external `chat_servers` or `xiaozhi_mqtt_gateway_cluster_core: true`,
which derives the symmetric core endpoints. The empty external default rejects
an unconfigured apply. Start from [gateway vars](../examples/mqtt-gateway.vars.yml)
and supply matching Vault `vault_mqtt_signature_key` / `vault_mqtt_server_secret`.
The latter must match the core auth key. Secrets are never public vars.

`core.yml` deploys the isolated private `:8000` core serially. At each new hello,
the gateway prefers a remote core away from the observed UI VIP, then a remote
VIP owner, then local fallback. Conflicting VIP observations block admission.
Established conversations are not migrated or replayed after core/gateway loss.
Transport-only readiness does not imply provider readiness.

`mqtt-bootstrap.yml` opts control planes into `/xiaozhi/ota/`, issuing signed
MQTT credentials for the applied VIP. Retain
`xiaozhi_control_plane_mqtt_bootstrap: true` in deployment vars. It does not serve
firmware updates or Vision. A robot needs bootstrap, MQTT hello and UDP audio to
reach a configured voice core; successful MQTT alone proves no provider behavior.
LAN MQTT and Settings HTTP use the trusted network boundary, without public TLS.

## Provider runtime

| Entrypoint | Required configuration and behavior |
| --- | --- |
| `worker-llm.yml` | Explicit text provider, matching validated Cloud revision and local key; uses `examples/worker-llm.vars.yml` |
| `worker-asr.yml` | Combined ASR/LLM worker, explicit source SHA, existing VAD path/checksum and ASR assets; no model download |
| `worker-tts.yml` | Combined voice worker, frozen controller TTS bundle and verified model/tokens/phonemes; see `examples/worker-tts.vars.yml` |
| `core-tts.yml` | Separate core audio activation after matching worker readiness and synthesis acceptance |

Keep reviewed immutable source SHAs in the corresponding revision variables.
ASR export reads selected ASR/VAD only. Local Sherpa assets must exist; provider,
asset or credential mismatch fails activation. Core `xiaozhi_core_asr_revision`
must equal the exported revision; null keeps voice coordination disabled.

TTS staging verifies the complete model tree and atomically publishes a new
versioned directory. Existing trees are never overwritten; mismatch needs a new
provisioned generation. Soundbank requires the complete frozen audio manifest
and verified blobs. Core copies only the audio generation, without accessing
control-plane credentials or fetching Drive content. Keep Python 3.11 for audioop.
Native model initialization precedes NATS subscriptions; readiness has a bounded
longer startup window for TTS.

Workers execute distributed provider jobs over Core NATS. A lost worker fails the
current turn; later turns may choose a different worker. Parallel TTS segments
retain ordered core playback and bounded cancellation cleanup. Soundbank hits
play through that pipeline without consuming a synthesis slot; a corrupt matched
recording fails explicitly. Model/dependency installation remains separate from
Cloud runtime Apply.

`verify-worker-llm.yml` sends real provider requests and can consume quota; it is
not an offline check. Use matching example/local vars and Vault files for
playbook syntax or check mode. Check mode can read Cloud state but does not export
secrets, install models or activate services.

## Shared Settings operations

`settings-secrets.yml` enables all-node provider key provisioning. Keep
`xiaozhi_control_plane_secret_provisioning: true` and a common encrypted
`vault_xiaozhi_secret_provision_key` (64 hex characters). **Save key on all 3 nodes**
is separate from ordinary Config Save: every node durably stores the new immutable
reference before Cloud CAS publication. Partial failure retains old references;
CAS conflict requires Sync before retry. Keys never return through status/GET.
Private peer commands and acknowledgements use authenticated encryption and fixed
peer identities; ingress blocks `/internal/`.

For an installed combined voice cluster,
`xiaozhi_control_plane_runtime_apply: true` installs the local boot guards and
permission-restricted Unix installer. Save publishes desired state; **Apply
runtime** validates all nodes, pauses cores, stops workers, installs bundles,
verifies workers serially and reopens cores. Expect a conversation interruption.
Settings, NATS, MQTT and VIP remain available. No provider runs in the control plane.

An interrupted apply retains root-only journals/backups and prevents partial
runtimes starting at boot. **Recover runtime** restores the previous bundles on
all prepared nodes; every member must be reachable. Do not reopen cores before
worker verification. Unsupported models/dependencies and VIP changes require
separate deployment. Ansible core deployment refuses an interrupted apply and
requires the current frozen configuration revision.

Memory CRUD uses one shared Cloud Memory authority, CAS publication, NATS hints
and periodic refresh. Runtime activation is needed to enable `manage_memory`;
later CRUD needs no restart. Soundbank authoring at `/xiaozhi/#soundbank` uses
existing TTS workers for draft generation, Save for Cloud entries/audio, then
Apply runtime for active voice. `soundbank-authoring.yml` updates only control
planes on an existing compatible deployment.

Compatible voice source supports firmware device MCP tools, session-bound Vision
uploads, response emotion and conversation inactivity. Upgrade all workers and
source readers before new core protocols or runtime fields are activated. Code
rollout is separate from Apply runtime. Device tools and HA automation are distinct.
The inactivity timeout uses `close_connection_no_voice_time` (default 120 seconds):
speech/text resets activity; pings/silence do not; LLM/tool/TTS work is protected.
Ending a conversation keeps MQTT connected.

Server contracts live in the server repository under `docs/cluster-device-mcp.md`,
`cluster-vision.md`, `cluster-memory.md`, `cluster-emotion.md`,
`cluster-conversation-idle.md`, `cluster-worker-tts.md` and
`cluster-soundbank-authoring.md`.

## Source updates and recovery

`vision-source.yml -e xiaozhi_vision_revision=<reviewed-pushed-server-SHA>` stages
Git worktrees, verifies unchanged requirements and preserves installed runtime
bundles/environment digests and Cloud revision. Workers activate first, control
planes next, cores last, serially with readiness and per-node rollback. Existing
data roots are exposed through read-only namespace mounts; no model copy occurs.

`maintenance-source.yml` requires explicit immutable server/gateway SHAs and idle
worker/core/gateway admission. It preserves bundles and credentials while
installing compatible maintenance source. See [maintenance](CLUSTER_EXPERIENCE.md)
for DNS gates and controller deployment order. These entrypoints are source
updates, not general dependency upgrades. Retain any venv still referenced by a
release. Inspect installed source and runtime metadata before choosing a revision;
role defaults do not establish which optional source is running.

Restore compatible source/env/bundle generations together. An old source must not
read newer incompatible runtime fields. Preserve node-local secrets, Cloud
pointers and root-owned rollback journals. Session state is ephemeral; recovery
requires a fresh firmware conversation. No guaranteed recovery latency follows
from service readiness.

## FD6551 node panel

`panel.yml` is opt-in and requires the pinned image driver and existing panel
units. It replaces their entrypoints through drop-ins, retaining one continuous
writer. Node numbers come from host vars, never election preference.

| Indication | Meaning |
| --- | --- |
| `nt` | Authenticated local MQTT connection, including idle |
| `Co` | Established local core session |
| `iP`, `dn`, `tL` | Actual local UI, DNS or VPN VIP ownership |
| `--` | No observed role |
| Clock / blinking Play / steady Play | ASR / LLM-VLM / TTS activity |
| Pause | Fresh confirmed idle worker |
| Colon pulse | Fresh DNS backend activity |

Roles rotate about every 1.5 seconds; node digits stay fixed. Enable
`xiaozhi_panel_worker_activity_enabled` with combined workers (shared inventory
already selects it). Missing/stale activity shows no compute claim; connection
counts never imply provider activity. Very short jobs can fall between polls.
The panel reads bounded local status and RAM metadata, not credentials or Cloud.
To restore the image clock, remove the two managed `10-xiaozhi-node.conf`
drop-ins, reload systemd and restart `deskbox-front-panel.service`; never run two
writers. Logic tests in `roles/xiaozhi_panel/tests` require no GPIO access.
