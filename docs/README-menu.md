# ysfradio MENU: DMR X-link toggle, location and APRS distance

New in this version:

- **MENU** button (top right, or press `M`). Works with mouse, touch, arrow keys, Enter and Esc.
- **Tile menu**: the main MENU is a grid of touch tiles. Columns and rows follow the screen size. When there are more tiles than fit, FWD (lower right) and BACK (lower left) change page and the page number is shown between them; BACK on the first page closes the menu. Arrow keys move between tiles, Enter opens one, PageUp and PageDown change page, Esc closes the menu. LOG, PIC SENDERS, the confirmation and the PIN keypad keep their list and keypad layouts, each with a BACK button that returns to the tile you came from.
- **DMR badge** in the top bar: `DMR ON` (green), `DMR ...` (blinking while starting/stopping), `DMR OFF`.
  Hidden if `--bridge-unit` is not set.
- **DMR X-LINK** menu item: starts/stops `mmdvm_bridge_ysf.service`. Needs the admin PIN (on-screen keypad).
- **MY LOCATION**: `OFF` / `NODE` (distance from the club) / `GPS` (the visitor's own position, asked from
  their browser; it never leaves their device).
- **DISTANCE UNIT**: KM / MILES. Remembered in the visitor's browser.
- **Distance + bearing** on the LAST/TO line ("147km NE") and a distance column in HEARD,
  from APRS positions (needs `--aprs`).
- **RADAR view** (button on the route line, MENU > RADAR VIEW, or press `R`): tiles with distance and
  bearing arrows beside a compass plot of stations with known positions, centred on NODE or the
  visitor's GPS position. RANGE cycles AUTO and fixed ranges; Esc or BACK closes it.

## Install on the Pi (files already copied to your home directory)

```
# 1. state directory (must exist before the service starts)
sudo mkdir -p /var/lib/ysfradio
sudo chown ysfdash:ysfdash /var/lib/ysfradio
sudo chmod 750 /var/lib/ysfradio

# 2. root helper that does the actual start/stop
sudo install -m 755 -o root -g root ysf-bridge-ctl /usr/local/sbin/ysf-bridge-ctl
sudo cp ysf-bridge-ctl.path ysf-bridge-ctl.service /etc/systemd/system/

# 3. dashboard files
sudo cp ysfradio.py ysfmenu.py /opt/ysfdash/
sudo cp ysfradio.service /etc/systemd/system/

# 4. admin PIN (6-12 digits)
sudo -u ysfdash python3 /opt/ysfdash/ysfradio.py --make-pin

# 5. go
sudo systemctl daemon-reload
sudo systemctl enable --now ysf-bridge-ctl.path
sudo systemctl restart ysfradio
```

Then open your dashboard (for example https://reflector.example.org/dash/), press MENU, DMR X-LINK, enter the PIN, confirm.

Watch it work: `journalctl -u ysf-bridge-ctl -f`

## How the toggle works (no sudo, no root web server)

The dashboard runs as the unprivileged `ysfdash` user and cannot start services. It writes
`start` or `stop` to `/var/lib/ysfradio/bridge.request`. A systemd path unit notices the file and runs
`/usr/local/sbin/ysf-bridge-ctl` as root, which accepts only those two words and acts only on the unit
named inside the script. Status is read with `systemctl is-active mmdvm_bridge_ysf.service`.
You can retire the old PHP toggle and its sudoers entry.

## Security

- PIN stored as PBKDF2-SHA256 (200,000 rounds) in `/var/lib/ysfradio/pin`, never in the service file.
- 5 wrong PINs from one address locks that address for 5 minutes, plus a global cap of 15 checks a minute.
- Session cookie: HttpOnly, SameSite=Strict, Secure, 20 min idle / 8 h max, held in memory
  (a restart logs everyone out). State-changing requests also need a CSRF token, a custom header,
  a JSON body, and a matching Origin if the browser sends one.
- The PIN is refused entirely unless HTTPS is on (or the server is bound to localhost).
- Bridge requests are limited to one every 3 seconds.
- Everything public stays GET only and read-only, as before.

## APRS distance

`--aprs` opens a read-only APRS-IS connection (`pass -1`, cannot transmit) and asks only for packets from
callsigns recently heard on the reflector (`--aprs-max`, default 40). It understands plain, compressed
and Mic-E position packets, keeps the latest position per callsign for 30 days in
`/var/lib/ysfradio/aprs.json`, and gives the page positions rounded to about 1 km.

- Only stations that actually beacon a position to APRS show a distance; others show a blank.
- Positions are already public on APRS, but if you would rather not show them, leave `--aprs` off.
- I could not confirm from the APRS-IS documentation how many callsigns one `b/` filter may hold.
  If you see fewer positions than expected, lower `--aprs-max`.

## New options

| Option | Default | Purpose |
|---|---|---|
| `--bridge-unit` | off | systemd unit the MENU may start/stop |
| `--state-dir` | `/var/lib/ysfradio` | PIN file, bridge request, APRS cache |
| `--pin-file` | `<state-dir>/pin` | override PIN file location |
| `--make-pin` | | prompt for a PIN, write it, exit |
| `--aprs` | off | enable APRS position lookups |
| `--aprs-server` | `rotate.aprs2.net:14580` | APRS-IS server |
| `--aprs-max` | 40 | callsigns to watch |

## Troubleshooting

- `ysfradio` fails with status 226/NAMESPACE: `/var/lib/ysfradio` does not exist yet (step 1).
- Badge always `DMR OFF`: run `systemctl is-active mmdvm_bridge_ysf.service` as your own user and check
  the unit name is exactly right.
- MENU says `ADMIN PIN NOT SET ON SERVER`: step 4 not done, or HTTPS is off.
- Toggle accepted but nothing happens: `systemctl status ysf-bridge-ctl.path` and
  `journalctl -u ysf-bridge-ctl`.
