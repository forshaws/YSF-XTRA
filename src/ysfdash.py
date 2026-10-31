#!/usr/bin/env python3
"""
ysfdash - read-only web dashboard for a G4KLX YSFReflector.

Tails /var/log/YSFReflector/YSFReflector-YYYY-MM-DD.log and serves:
  /             dashboard page
  /api/status   JSON snapshot
  /healthz      plain "ok"

Standard library only. Compatible with Python 3.5+ (Raspbian 9).
Read-only: never writes to the reflector, never runs shell commands.
"""

import argparse
import glob
import html
import json
import os
import re
import socketserver
import threading
import time
from collections import defaultdict, deque
from datetime import datetime
from http.server import BaseHTTPRequestHandler, HTTPServer

TS_FMT = "%Y-%m-%d %H:%M:%S"

LINE_RE = re.compile(r"^[A-Z]: (\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2})\.\d+ (.*)$")
RX_RE = re.compile(r"^Received data from (\S+)\s+to (\S+)\s+at (\S+)")
LINK_RE = re.compile(
    r"^\s+([^\s:]+)\s*:\s*([\d.]+):(\d+)\s+(\d+)/(\d+)\s*:\s*"
    r"(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2})"
)
LINK_HDR = "Currently linked repeaters/gateways:"
EOT = "Received end of transmission"

MAX_TX_KEPT = 5000
LAST_HEARD_SHOWN = 25
TOP_SHOWN = 10
NOW_TALKING_TIMEOUT = 180  # seconds without an end-of-transmission
STALE_LOG_SECONDS = 300    # reflector prints its linked block every ~2 min


def clean(s):
    """Keep only characters valid in callsigns / destinations."""
    return re.sub(r"[^A-Za-z0-9/*\-_ ]", "", s)[:20]


class State(object):
    def __init__(self):
        self.lock = threading.Lock()
        self.ref = None            # datetime of the last parsed log line
        self.linked = []
        self.linked_asat = None
        self.pending = None        # linked block being read
        self.pending_ts = None
        self.current = None        # transmission in progress
        self.txs = deque(maxlen=MAX_TX_KEPT)
        self.logfile = None
        self.log_mtime = None

    def new_day(self, name):
        with self.lock:
            self.txs.clear()
            self.current = None
            self.pending = None
            self.logfile = name

    def _finish(self, end):
        c = self.current
        if c is None:
            return
        dur = max(0.0, (end - c["start"]).total_seconds())
        self.txs.append({"start": c["start"], "src": c["src"],
                         "dest": c["dest"], "gw": c["gw"], "dur": dur})
        self.current = None

    def handle(self, line):
        m = LINE_RE.match(line.rstrip("\r\n"))
        if not m:
            return
        try:
            ts = datetime.strptime(m.group(1), TS_FMT)
        except ValueError:
            return
        rest = m.group(2)
        with self.lock:
            self.ref = ts
            if self.pending is not None:
                lm = LINK_RE.match(rest)
                if lm:
                    try:
                        since = datetime.strptime(lm.group(6), TS_FMT)
                    except ValueError:
                        since = None
                    self.pending.append({
                        "call": clean(lm.group(1)), "ip": lm.group(2),
                        "since": since,
                        "keepalive": "%s/%s" % (lm.group(4), lm.group(5)),
                    })
                    return
                self.linked = self.pending
                self.linked_asat = self.pending_ts
                self.pending = None
            if rest.startswith(LINK_HDR):
                self.pending = []
                self.pending_ts = ts
                return
            rm = RX_RE.match(rest)
            if rm:
                self._finish(ts)
                self.current = {"start": ts, "src": clean(rm.group(1)),
                                "dest": clean(rm.group(2)),
                                "gw": clean(rm.group(3))}
                return
            if rest.startswith(EOT):
                self._finish(ts)

    def snapshot(self, show_ip, name):
        with self.lock:
            ref = self.ref
            now_talking = None
            if self.current is not None and ref is not None:
                el = (ref - self.current["start"]).total_seconds()
                if el <= NOW_TALKING_TIMEOUT:
                    now_talking = {"src": self.current["src"],
                                   "dest": self.current["dest"],
                                   "gw": self.current["gw"],
                                   "elapsed": max(0, int(el))}
            linked = []
            for r in self.linked:
                since = r["since"]
                cf = None
                if since and self.linked_asat:
                    cf = int((self.linked_asat - since).total_seconds())
                row = {"call": r["call"], "keepalive": r["keepalive"],
                       "since": since.strftime(TS_FMT) if since else "",
                       "connected_for": cf}
                if show_ip:
                    row["ip"] = r["ip"]
                linked.append(row)
            recent = list(self.txs)[-LAST_HEARD_SHOWN:]
            recent.reverse()
            last_heard = [{"time": t["start"].strftime("%H:%M:%S"),
                           "src": t["src"], "dest": t["dest"], "gw": t["gw"],
                           "dur": round(t["dur"], 1)} for t in recent]
            agg = defaultdict(lambda: [0, 0.0])
            for t in self.txs:
                a = agg[t["src"]]
                a[0] += 1
                a[1] += t["dur"]
            top = sorted(agg.items(), key=lambda kv: (-kv[1][0], -kv[1][1]))
            top = [{"call": k, "count": v[0], "seconds": int(v[1])}
                   for k, v in top[:TOP_SHOWN]]
            age = None
            if self.log_mtime is not None:
                age = int(time.time() - self.log_mtime)
            return {
                "name": name,
                "as_at": ref.strftime(TS_FMT) if ref else None,
                "log_file": self.logfile,
                "log_age": age,
                "stale": age is None or age > STALE_LOG_SECONDS,
                "now_talking": now_talking,
                "linked": linked,
                "linked_as_at": (self.linked_asat.strftime("%H:%M:%S")
                                 if self.linked_asat else None),
                "last_heard": last_heard,
                "top": top,
                "tx_today": len(self.txs),
            }


def newest_log(log_dir, prefix):
    files = glob.glob(os.path.join(log_dir, prefix + "-*.log"))
    return max(files) if files else None


def tail_loop(state, log_dir, prefix, stop):
    f = None
    path = None
    buf = ""
    while not stop.is_set():
        try:
            if f is not None:
                chunk = f.read()
                if chunk:
                    buf += chunk
                    parts = buf.split("\n")
                    buf = parts.pop()
                    for ln in parts:
                        state.handle(ln)
                try:
                    state.log_mtime = os.fstat(f.fileno()).st_mtime
                except OSError:
                    pass
            newest = newest_log(log_dir, prefix)
            if newest is not None and newest != path:
                if f is not None:
                    f.close()
                f = open(newest, "r", errors="replace")
                path = newest
                buf = ""
                state.new_day(os.path.basename(newest))
        except (OSError, IOError):
            if f is not None:
                try:
                    f.close()
                except OSError:
                    pass
            f = None
            path = None
        stop.wait(0.5)


CSS = """
:root{--bg:#10151c;--panel:#18202b;--line:#26323f;--fg:#e4eaf0;--mut:#8696a7;
--acc:#4cc2ff;--ok:#4cd98a;--bad:#ff6b6b}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--fg);
font:15px/1.45 system-ui,-apple-system,"Segoe UI",Roboto,sans-serif}
header{padding:18px 20px;border-bottom:1px solid var(--line);display:flex;
flex-wrap:wrap;gap:8px 20px;align-items:baseline;justify-content:space-between}
h1{margin:0;font-size:20px}
.meta{color:var(--mut);font-size:13px}
.dot{display:inline-block;width:9px;height:9px;border-radius:50%;
background:var(--bad);margin-right:6px}
.dot.ok{background:var(--ok)}
main{padding:16px;display:grid;gap:16px;max-width:1200px;margin:0 auto;
grid-template-columns:repeat(auto-fit,minmax(340px,1fr))}
section{background:var(--panel);border:1px solid var(--line);border-radius:8px;
padding:14px 16px;overflow-x:auto}
section.wide{grid-column:1/-1}
h2{margin:0 0 10px;font-size:13px;letter-spacing:.06em;text-transform:uppercase;
color:var(--mut);font-weight:600}
table{width:100%;border-collapse:collapse;font-size:14px}
th,td{text-align:left;padding:5px 8px;border-bottom:1px solid var(--line);
white-space:nowrap}
th{color:var(--mut);font-weight:500;font-size:12px}
td.muted{color:var(--mut)}
#talk{font-size:22px;min-height:32px}
#talk.live{color:var(--ok)}
#talk.idle{color:var(--mut);font-size:16px}
"""

JS = r"""
(function () {
  'use strict';
  function $(id) { return document.getElementById(id); }
  function fmt(s) {
    if (s === null || s === undefined) { return ''; }
    s = Math.max(0, Math.round(s));
    var d = Math.floor(s / 86400), h = Math.floor(s % 86400 / 3600),
        m = Math.floor(s % 3600 / 60), x = s % 60;
    if (d > 0) { return d + 'd ' + h + 'h'; }
    if (h > 0) { return h + 'h ' + m + 'm'; }
    if (m > 0) { return m + 'm ' + x + 's'; }
    return x + 's';
  }
  function fill(id, rows, cols, empty) {
    var tb = $(id);
    while (tb.firstChild) { tb.removeChild(tb.firstChild); }
    if (!rows.length) {
      var tr0 = document.createElement('tr'), td0 = document.createElement('td');
      td0.colSpan = cols; td0.className = 'muted'; td0.textContent = empty;
      tr0.appendChild(td0); tb.appendChild(tr0); return;
    }
    rows.forEach(function (r) {
      var tr = document.createElement('tr');
      r.forEach(function (c) {
        var td = document.createElement('td'); td.textContent = c;
        tr.appendChild(td);
      });
      tb.appendChild(tr);
    });
  }
  function render(d) {
    var talk = $('talk');
    if (d.now_talking) {
      var n = d.now_talking;
      talk.className = 'live';
      talk.textContent = n.src + ' \u2192 ' + n.dest + ' via ' + n.gw +
                         ' (' + fmt(n.elapsed) + ')';
    } else {
      talk.className = 'idle';
      talk.textContent = 'Idle';
    }
    $('dot').className = 'dot' + (d.stale ? '' : ' ok');
    $('state').textContent = d.stale ? 'No recent log activity' : 'Live';
    $('asat').textContent = d.as_at ? 'Log time ' + d.as_at : '';
    $('count').textContent = d.linked.length + ' linked \u00b7 ' +
                             d.tx_today + ' transmissions today';
    var hasIp = d.linked.length && d.linked[0].ip !== undefined;
    $('ipcol').style.display = hasIp ? '' : 'none';
    fill('linked', d.linked.map(function (r) {
      var row = [r.call];
      if (hasIp) { row.push(r.ip); }
      row.push(fmt(r.connected_for), r.keepalive);
      return row;
    }), hasIp ? 4 : 3, 'No linked repeaters/gateways reported yet');
    fill('heard', d.last_heard.map(function (r) {
      return [r.time, r.src, r.dest, r.gw, r.dur + 's'];
    }), 5, 'No transmissions yet today');
    fill('top', d.top.map(function (r) {
      return [r.call, String(r.count), fmt(r.seconds)];
    }), 3, 'No transmissions yet today');
  }
  function poll() {
    var x = new XMLHttpRequest();
    x.open('GET', 'api/status', true);
    x.onload = function () {
      if (x.status === 200) {
        try { render(JSON.parse(x.responseText)); } catch (e) {}
      }
    };
    x.send();
  }
  poll();
  setInterval(poll, 3000);
})();
"""

INDEX = """<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>{name}</title><link rel="stylesheet" href="style.css"></head>
<body>
<header><h1>{name}</h1>
<div class="meta"><span id="dot" class="dot"></span><span id="state">Connecting</span>
&middot; <span id="asat"></span> &middot; <span id="count"></span></div></header>
<main>
<section class="wide"><h2>Now talking</h2><div id="talk" class="idle">Idle</div></section>
<section><h2>Linked repeaters / gateways</h2>
<table><thead><tr><th>Callsign</th><th id="ipcol" style="display:none">Address</th>
<th>Connected</th><th>Keepalive</th></tr></thead><tbody id="linked"></tbody></table></section>
<section><h2>Last heard</h2>
<table><thead><tr><th>Time</th><th>From</th><th>To</th><th>Via</th><th>Length</th></tr></thead>
<tbody id="heard"></tbody></table></section>
<section><h2>Most active today</h2>
<table><thead><tr><th>Callsign</th><th>Transmissions</th><th>Airtime</th></tr></thead>
<tbody id="top"></tbody></table></section>
</main><script src="app.js"></script></body></html>
"""

CSP = ("default-src 'none'; script-src 'self'; style-src 'self'; "
       "connect-src 'self'; base-uri 'none'; frame-ancestors 'none'")


class ThreadedServer(socketserver.ThreadingMixIn, HTTPServer):
    daemon_threads = True
    allow_reuse_address = True


def make_handler(state, args):
    index = INDEX.format(name=html.escape(args.name)).encode("utf-8")

    class Handler(BaseHTTPRequestHandler):
        server_version = "ysfdash"
        sys_version = ""

        def _send(self, code, ctype, body, head_only=False):
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("Content-Security-Policy", CSP)
            self.send_header("Referrer-Policy", "no-referrer")
            self.end_headers()
            if not head_only:
                self.wfile.write(body)

        def _route(self, head_only):
            path = self.path.split("?", 1)[0]
            if path in ("/", "/index.html"):
                self._send(200, "text/html; charset=utf-8", index, head_only)
            elif path == "/style.css":
                self._send(200, "text/css; charset=utf-8",
                           CSS.encode("utf-8"), head_only)
            elif path == "/app.js":
                self._send(200, "application/javascript; charset=utf-8",
                           JS.encode("utf-8"), head_only)
            elif path == "/api/status":
                body = json.dumps(state.snapshot(args.show_ip, args.name)
                                  ).encode("utf-8")
                self._send(200, "application/json", body, head_only)
            elif path == "/healthz":
                self._send(200, "text/plain", b"ok\n", head_only)
            else:
                self._send(404, "text/plain", b"not found\n", head_only)

        def do_GET(self):
            self._route(False)

        def do_HEAD(self):
            self._route(True)

        def log_message(self, fmt, *a):
            pass

    return Handler


def main():
    p = argparse.ArgumentParser(description="YSFReflector web dashboard")
    p.add_argument("--log-dir", default="/var/log/YSFReflector")
    p.add_argument("--prefix", default="YSFReflector",
                   help="log file name prefix (default YSFReflector)")
    p.add_argument("--host", default="127.0.0.1")
    p.add_argument("--port", type=int, default=8080)
    p.add_argument("--name", default="CCARC YSF Reflector")
    p.add_argument("--show-ip", action="store_true",
                   help="show linked gateway IP addresses (off by default)")
    args = p.parse_args()

    state = State()
    stop = threading.Event()
    t = threading.Thread(target=tail_loop,
                         args=(state, args.log_dir, args.prefix, stop))
    t.daemon = True
    t.start()

    srv = ThreadedServer((args.host, args.port), make_handler(state, args))
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        stop.set()
        srv.server_close()


if __name__ == "__main__":
    main()
