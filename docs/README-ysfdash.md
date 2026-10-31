# ysfdash

Read-only web dashboard for a G4KLX YSFReflector. Single Python file, standard library only, Python 3.5+.

## Install (Raspbian 9)

```
sudo useradd -r -s /usr/sbin/nologin ysfdash
sudo mkdir /opt/ysfdash
sudo cp ysfdash.py /opt/ysfdash/
sudo cp ysfdash.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now ysfdash
```

Dashboard: `http://<pi>:8080/`

## Test without systemd

```
python3 ysfdash.py --log-dir /var/log/YSFReflector --port 8080
```

## Options

| Option | Default | Purpose |
|---|---|---|
| `--log-dir` | `/var/log/YSFReflector` | Log directory |
| `--prefix` | `YSFReflector` | Log file prefix; newest `<prefix>-*.log` is followed |
| `--host` | `127.0.0.1` | Bind address (the service file uses `0.0.0.0`) |
| `--port` | `8080` | Listen port |
| `--name` | `CCARC YSF Reflector` | Page title |
| `--show-ip` | off | Show gateway IP addresses |

## Endpoints

| Path | Returns |
|---|---|
| `/` | Dashboard page (polls `api/status` every 3 s) |
| `/api/status` | JSON: now talking, linked gateways, last 25 heard, top callers today, totals |
| `/healthz` | `ok` |

GET and HEAD only; all other paths return 404.

## Behaviour

- Reads the log from the start of the newest daily file on startup and follows it, switching file at day rollover.
- Linked gateway list comes from the periodic `Currently linked repeaters/gateways` block; "Connected" is measured from the block's own timestamp.
- "Now talking" clears on `Received end of transmission` or after 180 s.
- State is held in memory; a restart rebuilds it from today's log.
- Status shows "No recent log activity" if the log file has not been written for 5 minutes.

## Security

- Read-only: opens the log for reading, never writes, never runs commands.
- Runs as unprivileged user `ysfdash` (group `adm` for log read access; adjust if the log directory has different permissions). systemd hardening: `NoNewPrivileges`, `ProtectSystem=strict`, `ProtectHome`, `PrivateTmp`.
- No request body, form or upload handling. Query strings are ignored.
- Callsign fields are filtered to `A-Z a-z 0-9 / * - _` and rendered with `textContent`.
- Response headers: strict CSP (`default-src 'none'`, scripts and styles from self only), `nosniff`, `no-referrer`, `no-store`.
- Gateway IP addresses are hidden unless `--show-ip` is set.
- For internet exposure, bind to `127.0.0.1` and put a TLS reverse proxy in front.
