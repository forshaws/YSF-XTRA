# Text messages: MENU > LOG > MESSAGE

The dashboard shows broadcast text messages (Yaesu System Fusion "MESSAGE") sent through the reflector.

- **MENU > LOG > MESSAGE** (or press `L`) opens a LOG MSG screen: newest first, at most 20, sender callsign, time, text.
  Up/Down arrow keys or the on-screen keys scroll, `TOP` jumps to the newest, `BACK` or Esc closes it.
- An envelope icon with a count appears in the top bar when there are messages you have not opened. Click it to open the log.
  "Seen" is remembered per browser, so each visitor has their own count. A first-time visitor starts with none unread.
- The LOG line in the MENU shows `OPEN` or the number of new messages, and `N/A` if the feature is off.

## LOG > NEWS and admin delete

- **MENU > LOG > NEWS** (or press `N`) lists the News Station uploads kept by `ysfmsgd.py` (see `README-news.md`), newest first, in the same LOG screen as the messages. Needs `ysfradio.py --news`. `/api/news` returns them like `/api/messages`.
- When logged in as admin (MENU > ADMIN, PIN), a `DEL` key appears on the MESSAGE and NEWS screens. Press `DEL`, then `SURE?` within 3 seconds, to delete the selected item. The `Delete` key does the same. Moving the selection cancels it.
- Deleting is `POST /api/delete` with `{"kind":"msg"|"news","id":N}`. It needs the admin session cookie and the CSRF header, like the DMR control. Without a login it returns 401.
- `ysfmsgd.py`, the dashboard and `ysfboard.py` take a lock (`<file>.lock`) around every change, so simultaneous writes are not lost.

## How it works

Two separate processes, so the web server gets no extra privileges:

| Process | Runs as | Privilege | Job |
|---|---|---|---|
| `ysfmsgd.py` (`ysfmsg.service`) | `ysfdash` | `CAP_NET_RAW` only | Watches the reflector's UDP port with a kernel filter, decodes text messages, writes `/var/lib/ysfradio/messages.json` |
| `ysfradio.py --messages` | `ysfdash` | `CAP_NET_BIND_SERVICE` only | Reads that file and serves it |

New endpoint (GET, read-only, public like the rest of the dashboard): `/api/messages` returns `{"msgs":[{"id","t","call","text"}, ...]}` newest first. `/api/status` gains `msg: {"ids":[...],"latest":N}`.

## What is kept

- Only broadcast messages (addressed to everyone) with a valid checksum. Messages addressed to a callsign are discarded unread, except News Station uploads (addressed to the configured room ID), which are public by design (`README-news.md`).
- Only the sender callsign (as heard on the reflector), the time received and the text (printable ASCII, 80 characters). No radio IDs, no GPS data, no addresses.
- At most 20 messages, none older than 7 days (`--keep-days`), at most 10 per callsign per hour (`--per-hour`).
- The browser shows text with `textContent` only, so message content cannot inject markup.

## Install (files already copied to your home directory)

```
sudo cp ysfdec.py ysfpos.py ysfmsgstore.py ysfmsgd.py ysfradio.py /opt/ysfdash/
sudo cp ysfmsg.service ysfradio.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now ysfmsg
sudo systemctl restart ysfradio
```

`/var/lib/ysfradio` already exists from the MENU install. Restarting `ysfradio` logs the admin session out.

Check it: `journalctl -u ysfmsg -f` prints `stored message N` when one arrives. `curl -s http://127.0.0.1:8080/api/messages` (or the HTTPS address) shows the list.

## Positions from the radios

The radio's position is sent in every voice transmission (voice/data mode 2) and in text messages sent with the position attached (APRS Mic-E, see `C4FM-TEXT-MESSAGE-FORMAT.md` sections 5.6 and 5.7). `ysfmsgd.py` decodes it and keeps the latest position per callsign in `/var/lib/ysfradio/positions.json`. With `--positions` on `ysfradio.py` the dashboard uses it for distance and RADAR VIEW exactly like a position from APRS-IS; where both exist, the newer one is used. It works for any voice transmission and any text message with a valid checksum, broadcast or directed (only the sender's position is used; directed text is still discarded).

- Voice positions are recorded at most once every 10 seconds per callsign, and the log line `position updated for CALL (from voice)` appears at most once a minute.
- Needs `--aprs` as well, because the distance and RADAR features are tied to it.
- Positions are shown at about 1 km resolution (two decimals of a degree), the same as APRS positions.
- Kept for 30 days (`--positions-days`), one position per callsign, at most 500 callsigns. A position of exactly 0,0 is ignored as "no fix".
- `--positions-out ""` on `ysfmsgd.py` turns position recording off.

## Options

| Option | Default | Purpose |
|---|---|---|
| `ysfradio.py --messages` | off | enable the LOG > MESSAGE feature |
| `ysfradio.py --news` | off | enable LOG > NEWS (reads `<state-dir>/newsboard.json`, `--news-file` to change) |
| `ysfradio.py --messages-file` | `<state-dir>/messages.json` | messages file written by `ysfmsgd.py` |
| `ysfmsgd.py --port` | 42002 | reflector UDP port to watch |
| `ysfmsgd.py --out` | `/var/lib/ysfradio/messages.json` | output file |
| `ysfmsgd.py --keep-days` | 7 | drop messages older than this |
| `ysfmsgd.py --positions-out` | `/var/lib/ysfradio/positions.json` | latest position per callsign (empty = off) |
| `ysfmsgd.py --positions-days` | 30 | forget positions older than this |
| `ysfradio.py --positions` | off | use radio-reported positions |
| `ysfradio.py --positions-file` | `<state-dir>/positions.json` | positions file written by `ysfmsgd.py` |
| `ysfmsgd.py --per-hour` | 10 | maximum stored per callsign per hour |

## Notes

- If your reflector uses a different UDP port, change `--port` in `ysfmsg.service`.
- To turn the feature off, stop `ysfmsg`, remove `--messages` from `ysfradio.service`, and delete `/var/lib/ysfradio/messages.json`.
- Message format: see `C4FM-TEXT-MESSAGE-FORMAT.md`.
