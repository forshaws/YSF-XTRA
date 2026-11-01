# Limits, rate controls and timeouts

Options are set on the service command line (`sudo systemctl edit --full <service>`, then `sudo systemctl restart <service>`). "Fixed" values are constants in the source.

## Messages (`ysfmsgd`)

| Limit | Default | Option |
|---|---|---|
| Stored per callsign per hour | 10 | `--per-hour` |
| News Station uploads kept per callsign per hour | 3 | `--board-per-hour` |
| Message retention | 7 days | `--keep-days` |
| News Station upload retention | 30 days | `--board-days` |
| Position retention | 30 days | `--positions-days` |
| Messages shown in the dashboard log | newest 20 | fixed (`MAX_MSGS`, `ysfmsgstore.py`) |
| Positions held | 500 | fixed (`POS_MAX`) |
| News request queue | 50 | fixed (`NEWS_MAX`) |
| Repeat News request from the same radio | ignored within 5 s | fixed |

A text message that is not stored is logged by `ysfmsgd` as `message from CALL not stored: REASON` (callsign and reason only, never the text), at most once per 10 s per callsign and reason. Reasons: frames lost on the way, record layout not recognised, checksum does not match, addressed to a callsign or radio ID, over the hourly limit, duplicate (same sender, sequence and stamp) or empty text. The checksum is the sum of bytes 60 up to and including the ETX.

## News Station replies (`ysfnews`)

| Limit | Default | Option |
|---|---|---|
| Same radio answered again | after 120 s | `--cooldown` |
| Requests answered per hour, all radios | 30 | `--per-hour` |
| Messages per request | 5 | `--max-lines` |
| Uploads included per request | 5 | `--board-lines` |
| Wait before replying | 1.5 s | `--delay` |
| Gap between messages | 4 s | `--gap` |

## Pictures (`ysfmsgd`, `ysfradio`)

| Limit | Default | Option |
|---|---|---|
| Kept per callsign per hour | 5 | `--pictures-per-hour` |
| Retention | 7 days | `--pictures-days` |
| Pictures kept | newest 20 | fixed (`MAX_PICS`, `ysfpic.py`) |
| Size of one picture | 64 KB | fixed (`MAX_BYTES`) |
| Picture size in pixels | 640x480 at most | fixed (`MAX_DIM`) |
| Time allowed between the streams of one picture | 120 s | fixed (`SESSION_GAP`) |
| Whitelist entries | 200 | fixed |

Pictures from callsigns (and radio IDs, when the entry has one) not on the whitelist are ignored and logged once per 5 minutes. A picture addressed to a callsign is ignored; only pictures to ALL or to the News Station room are kept.

## News Station picture replies (`ysfnews`)

| Limit | Default | Option |
|---|---|---|
| Picture requests answered per hour, all radios | 10 | `--pic-per-hour` |
| Same radio asking again | 120 s | `--cooldown` |
| Wait before replying | 1.5 s | `--delay` |
| Gap between the streams of the picture | 0.6 s | fixed |
| Time on air | about 13 s for a 160x120 LOW picture | fixed (100 ms per frame) |
## Dashboard (`ysfradio`, `ysfdash`)

| Setting | Default | Option |
|---|---|---|
| Talk meter full scale | 60 s | `--tot` (10–600) |
| Row fade after a transmission | 20 s | `--fade` |
| "Now talking" timeout without end-of-transmission | 180 s | fixed (`NOW_TALKING_TIMEOUT`) |
| "Now talking" cleared by | end of transmission, or `Network watchdog has expired` | fixed |
| Log treated as stale after | 300 s | fixed (`STALE_LOG_SECONDS`) |
| Transmissions remembered | 5000 | fixed (`MAX_TX_KEPT`) |
| Last-heard rows shown | 60 | fixed (`LAST_HEARD_SHOWN`) |
| Browser poll interval | 2 s (`ysfradio`), 3 s (`ysfdash`) | fixed |
| Log file check interval | 2 s | fixed (`SYS_INTERVAL`) |

## Admin login (`ysfmenu`)

| Limit | Value |
|---|---|
| Failed PIN attempts per address before lockout | 5 |
| PIN checks, all addresses | 15 per 60 s |
| Concurrent sessions | 20 |
| Session idle timeout | 20 min |
| Session maximum length | 8 h |

## APRS map

| Setting | Default | Option |
|---|---|---|
| Stations watched | 40 | `--aprs-max` (1–200) |
| Callsigns tracked | 5000 | fixed (`MAX_CALLS`) |
