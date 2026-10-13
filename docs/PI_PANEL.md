# External Raspberry Pi LCD panel

A dedicated read-only client for Raspberry Pi Zero W / Waveshare 3.5inch RPi LCD
(C), 480×320 RGB565. It runs on the display Pi, never on the three cluster nodes.
The current device is `zerow`, `192.168.1.115`, user `pi`.

Deploy explicitly:

```sh
ansible-playbook -i pi-panel.ini pi-panel.yml
```

The separate `pi_panels` inventory is not included in the normal cluster site
play. The role retains the installed LCD/touch overlays and OS configuration;
it does not flash, reboot or change network settings. Existing framebuffer
`/dev/fb1` and the stable ADS7846 `/dev/input/by-path` symlink are required.

The application reads only `GET /api/cluster/panel` from the management VIP,
at five-second intervals, with an 8 KiB body cap and a three-second request
budget. It rejects redirects and does not inherit proxy settings. Overview,
node details and the last three observations are rendered locally. The client
has no POST, reboot or maintenance action. The API remains independently hosted
by the existing cluster; this Pi is not a fourth HA participant.

Pillow rasterizes the UI without a desktop/browser. A small native RGB565
packing helper avoids per-pixel Python conversion. The renderer skips unchanged
state and writes only changed rectangle rows to the framebuffer. Snapshot and
page state stay in RAM, without history writes to the SD card. Data age advances
locally during request failures; expired nodes lose owner/activity claims and
become unavailable, rather than displaying zero CPU or idle jobs as live state.

Systemd runs the application as `pi`, with access to display/input devices,
a 64 MiB memory ceiling and 30% one-core CPU ceiling. Its fixed pre/post helpers
switch only tty1 between graphics/text to keep the fbcon cursor out of the UI;
those helpers require `CAP_SYS_TTY_CONFIG`. The main Python process runs without
ambient capabilities. Stop the service to return to the console:

```sh
sudo systemctl stop desk-cluster-panel
```

Mounting rotation is client-side (0 or 180 degrees), applied to both output and
input, so the existing kernel driver rotation remains intact. Resistive touch
orientation/calibration must be verified on the physically mounted screen.

Focused tests:

```sh
python3 -m unittest discover -s roles/desk_pi_panel/tests -v
```

Pi live check: native red/green/blue/white packing and panel API validation
passed. Three full UI rasterizations used a median 237.8 ms CPU time on the
original ARMv6 Zero W (excluding framebuffer I/O). This is an initial measurement,
not a whole-device or touch latency guarantee. Mounting rotation is confirmed as 180 degrees in `host_vars/zerow.yml`.
Physical touch alignment is checked by four-point calibration, using three
points for an affine raw-to-framebuffer transform and a fourth independent
check (reject above 30 pixels). The transform is saved with the Pi host variables
after user input; touch mapping applies mounting rotation afterward.
A 15-second steady-state sample measured 6.00% one-core CPU and 28.8 MiB RSS.
These measurements are specific to this installed Pi and current snapshots.


Mounting/calibration rollout on 2026-10-13: the user supplied four touch points.
The affine transform is persisted in `host_vars/zerow.yml`, together with a
180-degree mounting rotation, and matches the applied Pi configuration. The
fourth independent point had 23.72 pixels of error (accepted threshold 30);
the user subsequently verified the mounted image orientation and all three
Overview/Nodes/Events buttons as correct (physical check PASS). The
panel service is active/running with zero automatic restarts after applying
calibration; nine focused data/format/touch tests pass. Only this Pi's client
configuration and service were updated. No cluster or LCD driver change.


## On-screen recalibration

Open **Settings → Calibrate touch → Start** on the Pi LCD. Hold each gold
target briefly (at least 120 ms and five sensor samples), then lift. Four corner
medians fit an affine mapping; the centre is an independent validation point.
Corner residual and centre error must both be ≤16 pixels. Unstable or brief taps
retry the same target. The review screen shows the measured error and a test
area whose crosshair follows the candidate mapping. **Apply** commits it;
**Retry** discards the candidate; **Cancel**, a 90-second inactivity timeout or
holding anywhere for two seconds preserves the existing mapping. Navigation
buttons during calibration use the previously active mapping.

Only Apply writes `/var/lib/desk-cluster-panel/calibration.json` (mode 0600),
with an atomic replacement. The panel's user-owned systemd StateDirectory is
the only additional writable path. Startup validates this small local override;
absent/invalid files retain the Ansible fallback. Provisioning does not overwrite
the local override. The root-owned service/config and cluster APIs stay read-only
to the panel. No new port, daemon, SSH command, reboot or cluster mutation is
added by calibration. The existing manual helper remains available as a reserve.


Calibration UI acceptance on 2026-10-13: 20 focused offline tests pass, covering
rotated/swapped raw axes, noisy taps, independent-centre rejection, singular
fits, Apply-only writes, disk failure, reload persistence and Cancel/timeout.
Settings, confirmation, target and review frames were rendered at 480×320.
The Pi-only Ansible rollout completed; service is active/running with zero
automatic restarts. The state directory is owned by `pi`; a disposable local
write/restore check passed and the pre-existing mapping was retained. Physical
recalibration through the new UI is PASS: the user confirmed Calibrate/Apply
and correct touch. The saved mapping has 7.89 pixels independent validation
error, mode 0600 and owner `pi`.

Panel buttons use compact 48-pixel action heights (44 pixels for target Cancel),
centred text based on glyph bounds, and shared rectangles for rendering and
touch hit areas. Navigation and node tabs also centre labels on both axes.

LCD layout: all three data tabs share 14-pixel outer padding, a vertically
centred header with right-aligned state, and centred navigation/node labels.
Node details separate resource, service and configuration columns into three
outlined blocks. Events use evenly spaced rows with aligned timestamps.
