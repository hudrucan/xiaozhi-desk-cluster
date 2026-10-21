# Front-panel API v1

`GET http://192.168.1.186/api/cluster/panel` is the compact, read-only endpoint
for the Pi panel and other display clients. Use the applied management VIP if it changes;
do not bind the client to a particular node. Every collector serves the same
versioned projection from its own bounded cluster snapshot. Samples can differ
by a few seconds after failover; clients must use freshness, not pin an observer.
JSON is UTF-8, without compression, at most **8192 bytes**
and has a Content-Length header. No parameter, upload, mutation or CORS endpoint
is provided. The existing trusted-LAN access restriction is retained; peer APIs
and credentials are not exposed. Do not forward this endpoint to the Internet.

The existing collector/peer loop supplies its cached snapshot. A panel request
adds no probes, packet capture, database writes or model activity. The original
`/api/cluster/status` response and dashboard behavior remain compatible.

## Polling and freshness

- Check `protocol == "desk-cluster-panel-v1"`; ignore unrecognized optional fields.
- Poll according to `poll_after_ms` (5000), with one outstanding request at a time.
  Use a 3-second request timeout; on connection failure retry after 5 seconds,
  backing off to at most 30 seconds with jitter. Never poll in the rendering task.
- `HTTP 200` can be `ready` or `degraded`. A degraded panel remains useful when
  one node is missing, a component is unavailable, routing is unconfirmed or
  emergency DNS fallback is active. Configuration sync status is separate.
- `HTTP 503` still has the v1 JSON shape with `state: unavailable`, three node
  identities and cleared live claims. It means the cached aggregate is missing,
  invalid or older than 20 seconds. Render these cleared values immediately.
  `Retry-After: 5` is supplied.
- On HTTP errors, timeout, invalid JSON, size greater than 8192, or wrong protocol,
  retain the last view only as **stale**. After `stale_after_ms` (20000) without a
  fresh sample, hide ownership lines, busy indicators and numerical readings.
  Never keep displaying a stale owner as current. Track this with the display's
  monotonic clock, so initial Wi-Fi/NTP time is not required.
- `age_ms` is age at response time, not round-trip latency. Snapshot `observed_at`
  is ISO 8601 UTC or null at startup. Per-node age can exceed 20000 for an
  unavailable node. A null age means no observation; small future clock skew is
  clamped to zero. Clock skew beyond 5 seconds makes telemetry unavailable.
- A job shorter than the five-second collection interval may be missed. DNS
  eligibility is a sampled pool, not attribution of a specific DNS request.

## Stable fields

| Field | Meaning |
| --- | --- |
| `state` | `ready`, `degraded`, `unavailable` |
| `owners.xiaozhi`, `owners.dns` | `{state: known/conflict/unknown, node: ID or null}`; observed owners, never an elected preference |
| `dns.mode` | `adguard`, `fallback`, `unavailable`, `unknown` |
| `dns.eligible` | Node IDs confirmed eligible by fresh DNS-owner telemetry; unavailable/stale nodes excluded |
| `dns.fallback_resolver` | `1.1.1.1` in known emergency fallback, otherwise null |
| `nodes` | Three nodes in configured membership order; each has stable `id` and management `ip` |
| `nodes[].online` | Fresh valid node telemetry, not a guarantee that every application is healthy |
| `nodes[].cpu_percent`, `memory_percent`, `temperature_c` | Numeric readings rounded to one decimal; unknown is null |
| `nodes[].vip` | `xiaozhi` / `dns` bool or null; duplicates remain visible while top-level owner reports conflict |
| `nodes[].jobs` | `asr`, `llm_vlm`, `tts`: sampled concurrent job counts; 0 idle, null unknown. Vision shares LLM counter |
| `nodes[].mqtt_clients`, `conversations` | Observed gateway/core counts or null |
| `nodes[].dns_eligible` | true/false only with fresh confirmed routing; null when unconfirmed or node unavailable |
| `nodes[].dns_queries_per_second` | Observed query rate, rounded to one decimal; not bandwidth |
| `nodes[].admission_paused` | `worker`, `core`, `mqtt`: individual acknowledged pause flags or null |
| `nodes[].components` | `worker`, `core`, `mqtt`, `dns`: `ready`, `unavailable`, `unknown` |
| `nodes[].configuration` | `xiaozhi`, `dns`, `monitoring`: `{state: synced/pending/failed/unknown, revision: integer or null}` |
| `client` | Robot Web Control state `online`, `offline`, `unknown` and `server_connected` bool/null |
| `events` | Up to three latest observed cluster events: UTC `at`, `node`, `kind`, `message` |

All keys are present in unavailable node records. Unknown is always distinct
from zero/false. Boot IDs, private endpoints, source/bundle details, transcript,
audio, raw logs, passwords and tokens are excluded. Events use the existing
bounded six-hour RAM history; this is not a durable audit trail. Messages can be
Unicode: bound display text and wrap or truncate safely by codepoint.

For a small screen, render three node tiles, two owner badges, and one selected
node's details. Keep DNS health/configuration/events on touch drill-down screens;
there is no reason to refresh every LVGL object on every poll. Decode into a
bounded model off the UI task, hand it across an RTOS queue, and redraw only
changed fields. Reserve the network response buffer for 8192 bytes plus a trailing
NUL. A JSON parser may need additional arena memory; choose its budget based on
actual serialized and parsed sizes rather than assuming 8192 bytes is sufficient
for both. No display resolution, controller, pins or touch driver is chosen here.

## Actions

v1 is intentionally read-only. A tap selects a node/details locally. Do not
reproduce reboot/maintenance by SSH or arbitrary requests from the screen.
Physical-screen actions need a separately designed confirmation/authentication
contract; the existing browser-only node action flow is not part of this API.

## Deployment

```sh
ansible-playbook -i inventory.ini playbooks/dashboard/panel-api.yml
curl --max-time 3 http://192.168.1.186/api/cluster/panel
```

The rollout compiles only the existing Go collector on the controller, replaces
and restarts it serially, then validates HTTP and MQTT before gracefully reloading
the exact public route. Binary/route rollback staging is in `/run` and removed
when complete. Voice, DNS, VIP, credentials, models, policy and firmware remain
unchanged. Existing dashboard/monitoring/DNS reruns retain the exact panel route.

## Home Assistant projection

`owners.homeassistant` uses the existing known/conflict/unknown shape and counts
fresh active/starting/checkpointing Core owners. A missing owner does not imply
that a standby is active. `nodes[].homeassistant` is optional and contains only:

- `state`, `ready`, `sync`, `checkpoint_revision`, nullable `pending_disk_changes`;
- `age_ms`, and `checkpoint_age_ms` for display only.

Exclude metadata at 15 seconds even when the node itself is still online. Derive
the replica count from fresh, synced nodes matching the newest observed revision;
do not count an older bundle as synced to the new revision. No auth material,
backup content, private peer endpoint or additional probes are exposed. The v1
protocol and 8192-byte budget remain unchanged. `playbooks/dashboard/panel-homeassistant.yml` updates
only the cached collectors serially with rollback and process-identity guards.
