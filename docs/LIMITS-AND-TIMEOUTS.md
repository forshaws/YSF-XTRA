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

Messages dropped without a log line: over the hourly limit, duplicate (same sender, sequence and stamp), addressed to a specific callsign, bad checksum. The checksum is accepted when the sum starts at byte 22 (FT5D) or byte 13 (FT3D).

## News Station replies (`ysfnews`)

| Limit | Default | Option |
|---|---|---|
| Same radio answered again | after 120 s | `--cooldown` |
| Requests answered per hour, all radios | 30 | `--per-hour` |
| Messages per request | 5 | `--max-lines` |
| Uploads included per request | 5 | `--board-lines` |
| Wait before replying | 1.5 s | `--delay` |
| Gap between messages | 4 s | `--gap` |

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
