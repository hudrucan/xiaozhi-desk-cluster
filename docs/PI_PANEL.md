# External Raspberry Pi LCD panel

A dedicated read-only client for Raspberry Pi Zero W / LCDWiki/goodtft MPI3501
(ILI9486 + XPT2046), 480×320 RGB565. It runs on the display Pi, never on the three cluster nodes.
The current device is `zerow`, `192.168.1.115`, user `pi`.

Deploy explicitly:

```sh
ansible-playbook -i pi-panel.ini pi-panel.yml
```

The separate `pi_panels` inventory is not included in the normal cluster site
play. The role retains the installed LCD/touch overlays and OS configuration;
it does not flash, reboot or change network settings. Existing framebuffer
`/dev/fb1` and the stable ADS7846 `/dev/input/by-path` symlink are required.
This is an application deployment role, not provisioning for a fresh OS image.
Before any package/file changes, a read-only preflight verifies the overlay
SHA-256 pinned in `host_vars/zerow.yml`, live driver bindings, SPI/GPIO settings,
RGB565 framebuffer geometry and the touch input's SPI parent. It also runs in
check mode. An unexpected driver/overlay change requires an audit, not a bypass.
Run only that check with:

```sh
ansible-playbook -i pi-panel.ini pi-panel.yml --tags panel-preflight
```

The application reads only `GET /api/cluster/panel` from the management VIP,
at five-second intervals, with an 8 KiB body cap and a three-second request
budget. It rejects redirects and does not inherit proxy settings. Overview,
node details and the last three observations are rendered locally. The client
has no POST, reboot or maintenance action. The API remains independently hosted
by the existing cluster; this Pi is not a fourth HA participant.

The five bottom tabs are Overview, Nodes, Network, Events and Settings. Network
shows the fresh collector owner, LAN scope and actual scan schedule, plus each
replica's collector/starting/cold-standby role, checkpoint and Synced/Pending/
Warming up status. A replica behind the newest observed checkpoint is Pending.
Network metadata expires independently at 15 seconds, even while node telemetry
remains current. Missing Network data does not hide the other telemetry.
No device list, scan action or extra polling endpoint is added to the Pi.
Overview puts all held roles (Xiaozhi, DNS, VPN, Network collector and Monitor) inside
each node card, wrapping into at most two lines. The old two owner-summary rows
are removed. Multiple roles on one node are all retained; `!` marks conflicting
owner claims. Network labels expire independently of VIP/node telemetry.
Monitor identifies the ready hub preferred by the current fresh UI owner, using
the same placement metadata as the web dashboard. All monitoring hubs can run
simultaneously; this label is a preference rather than exclusive ownership.
If needed, Network/Monitor are shortened to Net/Mon to keep all five roles within two rows.
RAM and temperature share one 13-pixel, high-contrast line on Overview;
the format is `30% RAM - 42°C`, without a Temp suffix.
CPU remains 16 pixels, with room retained for two role-label rows.
In Nodes, **Status** shows resources, five service-readiness columns (including
VPN) and configuration sync. **Services** shows each VIP's Owner/Standby/
Unconfirmed role, VPN eligibility, and that node's Network role, LAN/scan scope,
checkpoint and sync/warmup state. Both views use the same node selection.
Both views leave 10 pixels above and below the Status/Services selector. Resource values
use 14-pixel text so the three metric cells have more breathing room.

The compact endpoint includes optional Network metadata from the collector's
existing cache, within the same 8 KiB limit. For an existing cluster, deploy only
that collector projection (including monitoring preference) before updating the Pi:

```sh
ansible-playbook -i inventory.ini panel-network.yml
ansible-playbook -i pi-panel.ini pi-panel.yml
```

`panel-network.yml` rolls through the three dashboard collectors with binary
backups and protected-service checks. It does not change routes or restart
the Network controller. The Pi role backs up changed `panel.py` versions;
its local calibration override is preserved.
For later renderer-only changes, `ansible-playbook -i pi-panel.ini pi-panel.yml
--tags panel-ui` runs preflight and updates/restarts only the Pi client, skipping
package installation and native compilation. An unchanged renderer does not
restart the service.

Pillow rasterizes the UI without a desktop/browser. A small native RGB565
packing helper avoids per-pixel Python conversion. The renderer skips unchanged
state and writes only changed rectangle rows to the framebuffer. Snapshot and
page state stay in RAM, without history writes to the SD card. Data age advances
locally during request failures; expired nodes lose owner/activity claims and
become unavailable, rather than displaying zero CPU or idle jobs as live state.

Systemd runs the application as `pi`, with access to display/input devices,
without CPU quota or memory limit: this Pi is dedicated to the panel.
`Nice=10` and `CPUWeight=10` remain scheduling preferences, not CPU ceilings.
The task-count limit and service sandbox remain in place. Its fixed pre/post helpers
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

Historical Waveshare checks (2026-10-13, before the MPI3501 replacement):
native red/green/blue/white packing and panel API validation
passed. Three full UI rasterizations used a median 237.8 ms CPU time on the
original ARMv6 Zero W (excluding framebuffer I/O). This is an initial measurement,
not a whole-device or touch latency guarantee. Mounting rotation is confirmed as 180 degrees in `host_vars/zerow.yml`.
The old manual helper checks touch alignment by four-point calibration, using three
points for an affine raw-to-framebuffer transform and a fourth independent
check (reject above 30 pixels). The transform is saved with the Pi host variables
after user input; touch mapping applies mounting rotation afterward.
A 15-second steady-state sample measured 6.00% one-core CPU and 28.8 MiB RSS.
These measurements are specific to this installed Pi and current snapshots.


Mounting/calibration rollout on 2026-10-13: the user supplied four touch points.
The historical affine transform was persisted in `host_vars/zerow.yml`, together with a
180-degree mounting rotation. It has since been replaced by MPI3501 calibration. The
fourth independent point had 23.72 pixels of error (accepted threshold 30);
the user subsequently verified the mounted image orientation and all three
Overview/Nodes/Events buttons as correct (physical check PASS). The
panel service is active/running with zero automatic restarts after applying
calibration; nine focused data/format/touch tests pass. Only this Pi's client
configuration and service were updated. No cluster or LCD driver change.


## Fetch interval

Open **Settings → Fetch interval**, enter whole seconds on the numeric keypad,
then tap **Apply**. The range is **1–60 seconds**, default **5 seconds**.
**<** deletes a digit and **Clear** empties the field. Invalid input is rejected.
Leaving without Apply discards the draft. A successful Apply saves atomically
to `/var/lib/desk-cluster-panel/fetch.json` (0600), independently of calibration
and timeout, and wakes the single poll worker to fetch again. Save failures keep
the current interval. Restart restores the preference; Ansible preserves it.

The interval is a delay after each completed GET (including a failed request).
Only one request runs at a time; the existing three-second HTTP timeout and 8 KiB
response limit remain. Collector refresh/cache cadence is independent. Longer
intervals can make metadata expire between requests; existing freshness rules
remain in force, including 15 seconds for Network and 20 seconds for node data.
Fetching continues when the picture is blanked by the idle timeout.

Fetch interval validation: all 50 panel tests pass, including worker wake-up,
input bounds, atomic save failure and independent preference persistence.
The numeric keypad and validation states were rendered at 480×320. Physical
input and Apply checks remain NOT RUN.

## Picture timeout

In **Settings → Screen timeout**, open the dropdown and choose **Off**, **30 sec**,
**1 min**, **2 min**, **5 min**, **10 min**, **15 min** or **30 min**. Swipe the
dropdown to reach more options; opening it brings the current selection into
view. Selecting an option closes the dropdown, and tapping outside dismisses it.
Off is the default. A selection is saved
immediately and atomically in `/var/lib/desk-cluster-panel/display.json`, mode
0600, independently of calibration. A failed save leaves the previous timeout
active and shows an error. Ansible does not overwrite this local preference.

After inactivity, the client writes one black frame and suspends rendering.
The existing cached API polling continues. Touch-down wakes the display and
redraws the latest cached state; that entire first gesture is consumed so it
cannot activate a button. A held touch keeps the display awake. Calibration
temporarily suspends the idle countdown. Restarting starts with the screen awake
and restores the saved timeout.

This blanks the picture only: the LED backlight stays on. LCDWiki's MPI3501
pinout provides no BL/PWM signal, and the current Linux `fb_ili9486` driver
does not implement the fbtft blank callback. No GPIO, driver, kernel or boot
change is made. References: [LCDWiki](https://www.lcdwiki.com/3.5inch_RPi_Display),
[ILI9486 driver](https://raw.githubusercontent.com/raspberrypi/linux/rpi-6.12.y/drivers/staging/fbtft/fb_ili9486.c),
[fbtft blank handling](https://raw.githubusercontent.com/raspberrypi/linux/rpi-6.12.y/drivers/staging/fbtft/fbtft-core.c).

## On-screen recalibration

Settings has separate **Touch calibration**, **Screen timeout** and **Fetch interval** menu entries.
Each opens only its own options, with a **<** icon to the left of the title
returning to the Settings menu. Its touch target is 44×44 pixels.
The menu uses list rows separated by dividers. Adding an entry to `SETTINGS_ITEMS`
automatically extends the scrollable list; its header and bottom navigation stay
fixed. Swipe vertically to scroll when the rows exceed the viewport. A scrollbar
indicates position, and a swipe never opens an item on release.

Settings/timeout validation: all 44 panel tests pass, and the Pi service is
active after deployment. The saved calibration and 10-minute timeout were
retained. The user accepted the revised Back icon and timeout dropdown UI on
2026-10-16 (PASS). Full physical touch/wake and scrolling checks remain NOT RUN.

Open **Settings → Touch calibration → Calibrate touch → Start** on the Pi LCD. Hold each gold
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

LCD layout: all data tabs share 14-pixel outer padding, a vertically
centred header with right-aligned state, and centred navigation/node labels.
Node details separate resource, service and configuration columns into three
outlined blocks. Events use evenly spaced rows with aligned timestamps.

## MPI3501 audit and cleanup — 2026-10-15

The live kernel is `6.12.109+rpt-rpi-v6`. The LCD-show `tft35a.dtbo` binds
`fb_ili9486` at SPI0 CS0 and `ads7846` at CS1. LCD SPI remains 16 MHz, touch SPI
2 MHz, LCD rotation 270 degrees, with reset GPIO25, DC GPIO24 and touch IRQ17.
The client mounting rotation remains 180 degrees. No driver, pinout, kernel,
network or package change was needed.

The failed `fbcp` launch and its `sleep 7` were removed from `/etc/rc.local`.
The unused binary `/usr/local/bin/fbcp`, legacy `/etc/inittab` and both X11
`99-calibration.conf` files were archived. Only the overridden `hdmi_mode=1`
line was removed from boot config; HDMI mode87 and fb0 remain to preserve fb1
numbering. No reboot has been performed to validate the boot edit.

Backups, including the original service, boot/client config and calibration,
are in `/var/backups/mpi3501-20261015T120056Z`. To restore the complete
pre-cleanup state over SSH:

```sh
ssh pi@192.168.1.115 'sudo sh /var/backups/mpi3501-20261015T120056Z/rollback.sh'
```

That rollback also restores the old Waveshare touch mapping and CPU quota;
it is not a way to retain the new MPI3501 calibration.

Both `CPUQuota=30%` and `MemoryMax=64M` were removed at the user's request.
The old memory limit had not been enforced: the running kernel disables the
memory cgroup controller. No memory-cgroup boot setting was changed.

The user completed five-point calibration and chose Apply. The recovery session
saved 10.33 px error; a subsequent on-screen Apply saved an improved 3.01 px,
which is the current maximum of corner residual and independent centre error
(limit 16 px), with `calibration.json` owned by `pi` and mode0600. The latest transform is also
the Ansible fallback in `host_vars/zerow.yml`; the local override still wins.
The panel resumed automatically with no automatic restarts. This confirms
calibration completion; it is not a new physical validation of every panel button
or a resolution of backlight flicker. The pre-driver backlight flicker remains
unresolved; the overlay exposes no backlight GPIO/PWM.
The [LCDWiki MPI3501 documentation](https://www.lcdwiki.com/3.5inch_RPi_Display)
lists LED backlight current as 120 mA but no backlight control pin or software
brightness procedure; physical pin12 (GPIO18) is NC. Here MPI3501 is the actual
LCDWiki/goodtft board replacing the Waveshare LCD, not an unidentified MPI3501
copy. Any added dimming control requires inspecting the backlight circuit before
considering hardware modification; assigning PWM to an NC pin cannot provide it.

APT/dpkg consistency checks passed during the audit. Xorg/calibrator packages
remain installed pending a separate dependency review; none are needed by this
evdev/framebuffer client. The LCD-show checkout and its original backups are
retained as provenance, not executed during deployment.

The Network projection rollout passed on all three dashboard collectors with
protected-service guards passing and Network controller PIDs/restart counters
unchanged. The Pi rollout changed only the Python renderer and restarted its
service; the native packing helper was not rebuilt. All 30 panel/calibration
unit tests and the Go collector tests passed. The Network layout was inspected
at 480×320 for current, lagging, warming and expired metadata, along with the
Overview role labels and both Nodes views. Physical use of the new Network tab
and Nodes Status/Services controls remains a user check.
