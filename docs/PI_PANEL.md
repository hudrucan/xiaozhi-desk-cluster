# External Raspberry Pi LCD panel

A dedicated read-only client for Raspberry Pi Zero W / LCDWiki/goodtft MPI3501
(ILI9486 + XPT2046), 480×320 RGB565. It runs on the display Pi, never on the three cluster nodes.
The current device is `zerow`, `192.168.1.115`, user `pi`.

Deploy explicitly from the repository root (loads `ansible.cfg` and the shared
`roles/` search path):

```sh
ansible-playbook -i playbooks/pi-panel/inventory.ini playbooks/pi-panel/deploy.yml
```

The separate `pi_panels` inventory is not included in the normal cluster site
play. The role retains the installed LCD/touch overlays and OS configuration;
it does not flash, reboot or change network settings. Existing framebuffer
`/dev/fb1` and the stable ADS7846 `/dev/input/by-path` symlink are required.
This is an application deployment role, not provisioning for a fresh OS image.
Before any package/file changes, a read-only preflight verifies the overlay
SHA-256 pinned in `playbooks/pi-panel/host_vars/zerow.yml`, live driver bindings, SPI/GPIO settings,
RGB565 framebuffer geometry and the touch input's SPI parent. It also runs in
check mode. An unexpected driver/overlay change requires an audit, not a bypass.
Run only that check with:

```sh
ansible-playbook -i playbooks/pi-panel/inventory.ini playbooks/pi-panel/deploy.yml --tags panel-preflight
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
Overview puts all held roles (Xiaozhi, DNS, VPN, Network collector, Monitor and HA) inside
each node card, wrapping into at most two lines. Multiple roles on one node are all retained; `!` marks conflicting
owner claims. Network labels expire independently of VIP/node telemetry.
Monitor identifies the ready hub preferred by the current fresh UI owner, using
the same placement metadata as the web dashboard. All monitoring hubs can run
simultaneously; this label is a preference rather than exclusive ownership.
If needed, Network/Monitor are shortened to Net/Mon to keep all six roles within two rows.
RAM and temperature share one 13-pixel, high-contrast line on Overview;
the format is `30% RAM - 42°C`, without a Temp suffix.
CPU remains 16 pixels, with room retained for two role-label rows.
In Nodes, **Status** shows resources, five service-readiness columns (including
VPN) and configuration sync. **Services** shows each VIP's Owner/Standby/
Unconfirmed role, VPN eligibility, and compact Network and Home Assistant cards with role, checkpoint and sync state.
The Network tab retains the LAN/scan scope details. Both views use the same node selection.
Both views leave 10 pixels above and below the Status/Services selector. Resource values
use 14-pixel text so the three metric cells have more breathing room.

The compact endpoint includes optional Network metadata from the collector's
existing cache, within the same 8 KiB limit. For an existing cluster, deploy only
that collector projection (including monitoring preference) before updating the Pi:

```sh
ansible-playbook -i inventory.ini playbooks/dashboard/panel-network.yml
ansible-playbook -i playbooks/pi-panel/inventory.ini playbooks/pi-panel/deploy.yml
```

`playbooks/dashboard/panel-network.yml` rolls through the three dashboard collectors with binary
backups and protected-service checks. It does not change routes or restart
the Network controller. The Pi role backs up changed `panel.py` versions;
its local calibration override is preserved.
For later renderer-only changes, `ansible-playbook -i playbooks/pi-panel/inventory.ini playbooks/pi-panel/deploy.yml
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

## Local Pi system and clock

**Settings → Pi system** shows the Pi's own CPU utilization (counter delta),
RAM used/total (using MemAvailable), CPU temperature, current CPU frequency,
uptime and firmware throttle/power flags. A background worker samples about
every two seconds while the page is visible and awake. The first CPU sample
has no delta yet and shows a dash. Missing sensors show unavailable values.
Current flags and sticky flags since boot are displayed separately; unknown
firmware bits are treated as warnings. Low V means under-voltage, Freq cap means
frequency capped, Throttle means throttled and Temp means soft temperature limit.
Flag definitions follow the [Raspberry Pi vcgencmd documentation](https://www.raspberrypi.com/documentation/computers/os.html#get_throttled).

The panel's existing video group gets access only to `/dev/vcio` for firmware
queries, via a persistent udev rule and the service's device allowlist.
**Reboot Pi** opens a confirmation dialog. Cancel, Back or 30 seconds of waiting
discards it. Confirm sends only the fixed, normal `systemctl reboot` command in
a background worker. Duplicate requests are ignored; failures are shown and can
be retried. The wake gesture never activates the button. Polkit permits the
panel user only the login1 reboot/reboot-multiple-sessions actions, without
granting general unit management or bypassing shutdown inhibitors.

Overview shows local time next to status, e.g. **Ready · 8:36 AM**, with explicit
12-hour AM/PM formatting and minute refresh independent of API polling. It uses the Pi OS timezone. Configure timezone and NTP through the OS;
the panel role does not manage them.

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

Settings has separate **Touch calibration**, **Screen timeout**, **Fetch interval**
and **Pi system** menu entries.
Each opens only its own options, with a **<** icon to the left of the title
returning to the Settings menu. Its touch target is 44×44 pixels.
The menu uses list rows separated by dividers. Adding an entry to `SETTINGS_ITEMS`
automatically extends the scrollable list; its header and bottom navigation stay
fixed. Swipe vertically to scroll when the rows exceed the viewport. A scrollbar
indicates position, and a swipe never opens an item on release.

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


Panel buttons use compact 48-pixel action heights (44 pixels for target Cancel),
centred text based on glyph bounds, and shared rectangles for rendering and
touch hit areas. Navigation and node tabs also centre labels on both axes.

LCD layout: all data tabs share 14-pixel outer padding, a vertically
centred header with right-aligned state, and centred navigation/node labels.
Node details separate resource, service and configuration columns into three
outlined blocks. Events use evenly spaced rows with aligned timestamps.

## Home Assistant display

The compact API includes optional per-node Home Assistant metadata. Overview
shows the current Core owner with an HA role and a bottom status line with
readiness/pending status, revision and matching replica count. Nodes → Services
shows Home Assistant alongside Network, including standby replicas. Metadata
expires independently after 15 seconds; conflicting owners are explicit rather
than rendered as Ready. No extra tab, API polling thread or control action is
added. Manual Sync config remains in the native HA Cluster panel.

For an existing installation:

```sh
ansible-playbook -i inventory.ini playbooks/dashboard/panel-homeassistant.yml
ansible-playbook -i playbooks/pi-panel/inventory.ini playbooks/pi-panel/deploy.yml --tags panel-ui
```

The collector upgrade guards Network and HA controller process identities, and
the Pi update preserves rotation, calibration and local display preferences.

## Display hardware and recovery

`playbooks/pi-panel/host_vars/zerow.yml` pins the MPI3501 overlay checksum, input path, 180-degree
client mounting rotation and fallback touch transform. The local calibration
file takes precedence. The application requires the existing SPI framebuffer
and ADS7846 driver; deployment preflight checks their bindings and geometry.
Preserve the installed overlay/boot configuration when rebuilding the client.

The board exposes no backlight GPIO/PWM in its supported configuration. Picture
timeout does not dim the LED, and backlight flicker remains unresolved. Do not
assign PWM to the board's NC pin. Driver or hardware changes need their own
inspection. The framebuffer client does not require Xorg/calibrator packages.

If the UI fails, stop `desk-cluster-panel` over SSH to restore the console;
inspect its journal and rerun `playbooks/pi-panel/deploy.yml --tags panel-preflight` before
reapplying. Preserve `/var/lib/desk-cluster-panel/` preferences. Invalid local
calibration falls back to the host vars; use the on-screen calibration flow to
replace a valid but unsuitable mapping.
