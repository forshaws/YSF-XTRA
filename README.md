# YSF-XTRA

Extras for a YSF (Yaesu System Fusion) reflector: a radio-style web dashboard, a documented decoder for the text messages and GPS positions that Yaesu C4FM radios send through it, and a News Station that answers WIRES-X news requests with text messages and keeps uploads from your users.

Python 3.5+, standard library only. Written for a Raspberry Pi 3B+ running a G4KLX YSFReflector; works on any Linux host that can read the reflector log.

## What is in it

**Dashboard** (`src/ysfradio.py`, built on `src/ysfdash.py` and `src/ysfmenu.py`)

- Now talking, last heard, linked gateways, top callers; HTTPS with a strict content security policy.
- MENU: DMR X-link toggle (admin PIN), my location (node or the visitor's own GPS), distance unit.
- Distance and bearing to heard stations, and a RADAR view, from APRS-IS positions and from positions the radios themselves transmit.
- MENU > LOG > MESSAGE: the last 20 broadcast text messages, newest first, with an unread icon in the top bar.
- MENU > LOG > NEWS: the News Station uploads kept on the reflector (see below).
- Admin (PIN): a `DEL` key on the MESSAGE and NEWS lists deletes the selected item.

**Decoder** (`src/ysfmsgd.py`, `src/ysfdec.py`, `src/ysfpos.py`)

- Reads the reflector's UDP port with a kernel packet filter (needs `CAP_NET_RAW`, never transmits).
- Decodes broadcast text messages and the position carried in GPS-attached messages and in voice transmissions.
- Writes `messages.json` and `positions.json` for the dashboard. Directed message text is discarded unread.
- Also recognises WIRES-X News Station requests: a download request is queued for `ysfnews.py`, and an upload (a text addressed to the News Station room ID) is kept as a public news item.

**News Station responder** (`src/ysfnews.py`, `docs/README-news.md`)

- A radio's News Station "download, message" is answered with the lines of `news.txt` (`examples/news.txt` is a generic welcome to edit), then the latest uploads from other radios, as directed text messages under your own callsign and radio ID. The radio shows them in its message log.
- A radio's "upload, message" is stored on the reflector, so stations that connect later can download it, like a small bulletin board. At most 20 items for 30 days, 3 per callsign per hour.
- Voice and picture downloads are not answered.

**Research tools** (`src/`)

| Tool | Purpose |
|---|---|
| `ysfcap.py` | Passive capture of the frames sent by one callsign to a JSON lines file |
| `ysfdec.py` | Frame, FICH, data unit and record decoder (`--records`, `--ok`, `--selftest`) |
| `ysfdiff.py` | Shows which bytes change between repeated captures |
| `ysfsend.py` | Builds and sends one text message, broadcast or directed (`--to-call`, `--to-id`), under your own callsign (`--check`, `--dry`, `--live`) |
| `ysfpos.py` | Decodes and builds the APRS Mic-E position block |
| `ysfgps.py` | Lists decoded positions in a capture and compares them with positions you set |
| `ysfreplay.py` | Feeds a capture through the decoder and records the positions in it |

**Operator tools** (`src/`)

| Tool | Purpose |
|---|---|
| `ysfboard.py` | List, add, edit, delete and clear News Station board items (`sudo -u ysfdash python3 /opt/ysfdash/ysfboard.py list`) |

## Documentation

| File | Content |
|---|---|
| `docs/C4FM-TEXT-MESSAGE-FORMAT.md` | The text message format, frame coding, checksum, addressing, GPS block, voice position |
| `docs/README-ysfdash.md` | The original read-only dashboard (`ysfdash.py`) |
| `docs/README-menu.md` | MENU, DMR toggle, location, distance and RADAR; install and options |
| `docs/README-messages.md` | LOG > MESSAGE, LOG > NEWS, admin delete, radio positions; install, options, what is kept |
| `docs/README-news.md` | News Station responder: download, upload, news file, board tool; install and options |
| `docs/GPS-TEST-PLAN.md` | The controlled test used to decode the GPS block |

## Install (summary)

Create the user and folders, copy `src/*.py` to `/opt/ysfdash/`, install the units from `systemd/`, then enable them.

```
sudo useradd -r -s /usr/sbin/nologin ysfdash
sudo mkdir -p /opt/ysfdash /var/lib/ysfradio
sudo chown ysfdash:ysfdash /var/lib/ysfradio
sudo chmod 750 /var/lib/ysfradio
sudo cp src/*.py /opt/ysfdash/
sudo cp systemd/ysfradio.service systemd/ysfmsg.service /etc/systemd/system/
sudoedit /etc/systemd/system/ysfradio.service     # node, name, grid, host, certificate paths
sudo systemctl daemon-reload
sudo systemctl enable --now ysfmsg ysfradio
```

`systemd/ysfradio.service` uses placeholder values (`N0CALL`, `IO91`, `reflector.example.org`).

For the News Station, also set `--board-room-id` in `systemd/ysfmsg.service` and install `systemd/ysfnews.service` with your own callsign and radio ID: see `docs/README-news.md`.

The DMR toggle also needs `bin/ysf-bridge-ctl` and the two `ysf-bridge-ctl` units: see `docs/README-menu.md`. The admin PIN is set with `ysfradio.py --make-pin`.

## Web endpoints (GET and HEAD only)

| Path | Returns |
|---|---|
| `/` | Dashboard page |
| `/api/status` | JSON: now talking, linked gateways, last heard, top callers, positions, menu state |
| `/api/messages` | JSON: the last 20 broadcast messages, newest first (`--messages`) |
| `/api/news` | JSON: the News Station uploads, newest first (`--news`) |
| `/healthz` | `ok` |

Admin actions (DMR toggle, `POST /api/delete` for messages and news items) are POST requests behind the PIN, a session cookie and a CSRF header: see `docs/README-menu.md` and `docs/README-messages.md`.

## Security

- The dashboard process runs as an unprivileged user with `CAP_NET_BIND_SERVICE` only. The packet-capture process is separate and holds `CAP_NET_RAW` only.
- Text from radios is shown with `textContent`. There is no `innerHTML`, no inline script and no inline style, and the content security policy allows scripts and styles from the page's own origin only. Checked with hostile payloads in message text, callsigns and the positions file.
- Messages are limited to 80 printable ASCII characters, sender callsigns must match a strict pattern, positions must be numbers in range. At most 20 messages, 7 days, 10 per callsign per hour. News items: 20, 30 days, 3 per callsign per hour.
- The decoder only records what the radios transmit to the reflector, which is already visible to everyone connected to it. It stores the sender callsign, time, text and latest position per callsign (30 days). Directed messages are discarded unread; the only exception is a News Station upload, which is public by design.
- The News Station responder (`ysfnews.py`) is a separate service with no privileges. It answers only requests that name a valid callsign and radio ID, once per radio per 2 minutes and at most 30 requests an hour, and sends only under the callsign and radio ID you configure.
- Files shared between the services are changed under a lock, so simultaneous writes are not lost.
- YSF has no authentication: anyone can send a message or position under any callsign, as with APRS-IS. The dashboard shows what the packet says. If the reflector blocks or mutes a source, the decoder still sees that source's packets because it reads the port before the reflector decides.

## Tested on

FT5D (sending and receiving, voice and text, News Station upload and download), FT3D (receiving a generated text message), a Raspberry Pi 3B+ with Raspbian 9 and Python 3.5. Other radio models, other firmware, voice modes other than voice/data mode 2, and image or data messages are untested.

## Responsible use

Transmit only under your own callsign and licence conditions. `ysfsend.py` and `ysfnews.py` take the callsign and radio ID you give them: use your own. `ysfnews.py` sends messages automatically without an operator at the controls: check that your licence permits that before running it. Nothing here defeats encryption, signing or access control; none was found. Not affiliated with or endorsed by Yaesu Musen Co., Ltd. "Yaesu" and "System Fusion" are trademarks of their owners. No Yaesu firmware or confidential material was used; the formats were derived from the published C4FM air interface specification and from the author's own radios and reflector.

## Credits

Reverse-engineering and verification: Scot Forshaw (M0RWV). Analysis and implementation: Claude (Anthropic), working from captures made on the author's own equipment.

## Licence

Not yet chosen.
