#!/usr/bin/env python3
"""
ysfradio - transceiver-style display for a G4KLX YSFReflector.

Companion to ysfdash.py (must sit in the same directory: it reuses the
log parser). Adds Raspberry Pi system gauges (CPU, temperature, memory,
load, uptime) read from /proc and /sys.

Read-only. Standard library only. Python 3.5+.
"""

import argparse
import html
import json
import os
import re
import ssl
import sys
import threading
import time

from datetime import datetime
from http.cookies import SimpleCookie
from http.server import BaseHTTPRequestHandler
from urllib.parse import urlparse

import ysfdash
import ysfmenu
import ysfmsgstore
import ysfpic
from ysfdash import State, ThreadedServer, tail_loop

SYS_INTERVAL = 2.0
ysfdash.LAST_HEARD_SHOWN = 60  # scrollable history of recent transmissions
WATCHDOG = "Network watchdog has expired"


class RadioState(State):
    """State that also ends the transmission in progress when the reflector logs a
    network watchdog expiry (a station that stops without an end-of-transmission)."""

    def handle(self, line):
        State.handle(self, line)
        m = ysfdash.LINE_RE.match(line.rstrip("\r\n"))
        if m and m.group(2).startswith(WATCHDOG):
            try:
                ts = datetime.strptime(m.group(1), ysfdash.TS_FMT)
            except ValueError:
                return
            with self.lock:
                self._finish(ts)


class SysStats(object):
    """Samples CPU, temperature, memory, load and uptime from /proc and /sys."""

    def __init__(self):
        self.lock = threading.Lock()
        self.data = {}
        self._prev = None

    def _cpu(self):
        with open("/proc/stat") as f:
            parts = f.readline().split()
        vals = [int(x) for x in parts[1:9]]
        idle = vals[3] + vals[4]
        total = sum(vals)
        prev = self._prev
        self._prev = (idle, total)
        if prev is None:
            return None
        dt = total - prev[1]
        di = idle - prev[0]
        if dt <= 0:
            return None
        return round(100.0 * (dt - di) / dt, 1)

    def update(self):
        d = {"cpu": None, "temp": None, "load": None,
             "mem": None, "uptime": None}
        try:
            d["cpu"] = self._cpu()
        except (OSError, ValueError, IndexError):
            pass
        try:
            with open("/sys/class/thermal/thermal_zone0/temp") as f:
                d["temp"] = round(int(f.read().strip()) / 1000.0, 1)
        except (OSError, ValueError):
            pass
        try:
            with open("/proc/loadavg") as f:
                d["load"] = float(f.read().split()[0])
        except (OSError, ValueError, IndexError):
            pass
        try:
            info = {}
            with open("/proc/meminfo") as f:
                for line in f:
                    k, _, v = line.partition(":")
                    if k in ("MemTotal", "MemAvailable", "MemFree"):
                        info[k] = int(v.split()[0])
            avail = info.get("MemAvailable", info.get("MemFree"))
            if avail is not None and info.get("MemTotal"):
                d["mem"] = round(100.0 * (1 - float(avail) / info["MemTotal"]), 1)
        except (OSError, ValueError, IndexError):
            pass
        try:
            with open("/proc/uptime") as f:
                d["uptime"] = int(float(f.read().split()[0]))
        except (OSError, ValueError, IndexError):
            pass
        with self.lock:
            self.data = d

    def snapshot(self):
        with self.lock:
            return dict(self.data)


def sys_loop(stats, stop):
    while not stop.is_set():
        stats.update()
        stop.wait(SYS_INTERVAL)


class TLSContext(object):
    """Server TLS context that reloads the certificate when the files change
    (e.g. after a Let's Encrypt renewal), without restarting the service."""

    def __init__(self, cert, key):
        self.cert = cert
        self.key = key
        self.lock = threading.Lock()
        self.stamp = None
        self.ctx = None
        self.load()

    def _stamp(self):
        return (os.stat(self.cert).st_mtime, os.stat(self.key).st_mtime)

    def load(self):
        ctx = ssl.create_default_context(ssl.Purpose.CLIENT_AUTH)
        ctx.options |= ssl.OP_NO_TLSv1 | ssl.OP_NO_TLSv1_1
        ctx.load_cert_chain(self.cert, self.key)
        self.ctx = ctx
        self.stamp = self._stamp()

    def get(self):
        try:
            if self._stamp() != self.stamp:
                with self.lock:
                    if self._stamp() != self.stamp:
                        self.load()
        except (OSError, ssl.SSLError):
            pass  # keep serving the certificate already loaded
        return self.ctx


class QuietServer(ThreadedServer):
    """Threaded server that ignores routine connection/TLS errors from scanners."""

    def handle_error(self, request, client_address):
        et = sys.exc_info()[0]
        if et is not None and issubclass(et, OSError):
            return
        ThreadedServer.handle_error(self, request, client_address)


HOST_RE = re.compile(r"^[A-Za-z0-9]([A-Za-z0-9.-]{0,251}[A-Za-z0-9])?(:\d{1,5})?$")


def make_redirect_handler(args):
    """Plain-HTTP listener that only redirects to the HTTPS site."""

    class Redirect(BaseHTTPRequestHandler):
        server_version = "ysfradio"
        sys_version = ""
        timeout = 15

        def _go(self):
            host = args.public_host
            if not host:
                h = self.headers.get("Host", "")
                if not HOST_RE.match(h):
                    self.send_error(400)
                    return
                host = h.split(":", 1)[0]
            if args.port != 443:
                host = "%s:%d" % (host, args.port)
            path = re.sub(r"[^\x21-\x7e]", "", self.path)
            if not path.startswith("/"):
                path = "/"
            self.send_response(301)
            self.send_header("Location", "https://%s%s" % (host, path))
            self.send_header("Content-Length", "0")
            self.end_headers()

        do_GET = _go
        do_HEAD = _go

        def log_message(self, fmt, *a):
            pass

    return Redirect


GRID_RE = re.compile(r"^([A-R])([A-R])(\d)(\d)(?:([A-X])([A-X]))?$")


def grid_to_latlon(g):
    """Centre of a Maidenhead locator (4 or 6 characters), or None."""
    m = GRID_RE.match(g.upper())
    if not m:
        return None
    lon = (ord(m.group(1)) - 65) * 20 + int(m.group(3)) * 2 - 180.0
    lat = (ord(m.group(2)) - 65) * 10 + int(m.group(4)) - 90.0
    if m.group(5):
        lon += (ord(m.group(5)) - 65) * (2.0 / 24) + (2.0 / 24) / 2
        lat += (ord(m.group(6)) - 65) * (1.0 / 24) + (1.0 / 24) / 2
    else:
        lon += 1.0
        lat += 0.5
    return round(lat, 4), round(lon, 4)


def latlon_to_grid(lat, lon):
    """6-character Maidenhead locator for a latitude/longitude."""
    lo = lon + 180.0
    la = lat + 90.0
    f1 = min(17, int(lo // 20))
    f2 = min(17, int(la // 10))
    s1 = min(9, int((lo % 20) // 2))
    s2 = min(9, int(la % 10))
    u1 = min(23, int((lo % 2) * 12))
    u2 = min(23, int((la % 1) * 24))
    return "%s%s%d%d%s%s" % (chr(65 + f1), chr(65 + f2), s1, s2,
                             chr(65 + u1), chr(65 + u2))


CSS = """
:root{--red:#ff2440;--blue:#2f7bff;--white:#f4f4f4;--dim:#59616e;--dark:#2a2f38;--ink:#000}
*{box-sizing:border-box}
html,body,*{-webkit-user-select:none;user-select:none;-webkit-touch-callout:none;-webkit-tap-highlight-color:transparent}
html,body{margin:0;height:100%;background:#070707}
html{-webkit-text-size-adjust:100%;text-size-adjust:100%}
body{display:flex;
font-family:"DejaVu Sans Mono",Menlo,Consolas,"Courier New",monospace;color:var(--white)}
a{color:inherit;text-decoration:none}
a:hover{text-decoration:underline}
.hidden{display:none!important}
.screen{--u:min(0.98vw,calc(var(--vh,1vh)*1.63));margin:auto;position:relative;width:calc(var(--u)*100);
aspect-ratio:5/3;background:var(--ink);border:calc(var(--u)*.7) solid #1b1b1d;
border-radius:calc(var(--u)*1.6);overflow:hidden;display:grid;
grid-template-rows:7fr 25fr 6fr 7fr 5fr 40fr;
box-shadow:0 0 calc(var(--u)*4) rgba(47,123,255,.18),inset 0 0 calc(var(--u)*6) rgba(0,0,0,.9)}
.screen::after{content:"";position:absolute;left:0;top:0;right:0;bottom:0;pointer-events:none;
background:repeating-linear-gradient(0deg,rgba(255,255,255,.035) 0,rgba(255,255,255,.035) 1px,transparent 1px,transparent 3px)}
.top{display:flex;align-items:center;gap:calc(var(--u)*1.4);padding:0 calc(var(--u)*1.6);
background:var(--red);color:#fff;font-weight:700;font-size:calc(var(--u)*2.3);white-space:nowrap;overflow:hidden}
.top .sp{flex:1}
.badge{background:#fff;color:var(--red);padding:0 calc(var(--u)*1.2);border-radius:calc(var(--u)*.4)}
.badge.blue{background:var(--blue);color:#fff}
.badge.off{background:#222;color:#888}
.loc{display:inline-flex;align-items:center;gap:calc(var(--u)*.6)}
.pin{width:calc(var(--u)*1.7);height:calc(var(--u)*2.3)}
.vfo{display:grid;grid-template-columns:8fr 70fr 22fr;align-items:center;
padding:0 calc(var(--u)*1.6);border-bottom:1px solid #1d1d20}
.band{font-size:calc(var(--u)*6);font-weight:700;color:var(--blue)}
.call{font-weight:700;letter-spacing:.02em;white-space:nowrap;line-height:1;
font-size:calc(var(--u)*14);color:var(--dim);transition:color 2s ease}
.call.live{color:var(--white);text-shadow:0 0 calc(var(--u)*1.2) rgba(255,255,255,.35);transition:none}
.call.old{color:var(--dark)}
.rid{font-size:calc(var(--u)*2.4);font-weight:700;color:var(--blue);min-height:calc(var(--u)*3);white-space:nowrap}
.call.mid{font-size:calc(var(--u)*11)}
.call.small{font-size:calc(var(--u)*7.5)}
.side{text-align:right}
.lamp{display:inline-block;font-weight:700;font-size:calc(var(--u)*2.6);padding:0 calc(var(--u)*1.2);
border:1px solid #333;color:#444;border-radius:calc(var(--u)*.4)}
.lamp.on{background:var(--red);border-color:var(--red);color:#fff;
box-shadow:0 0 calc(var(--u)*1.6) var(--red)}
.timer{font-size:calc(var(--u)*6);font-weight:700;margin-top:calc(var(--u)*.6);color:var(--white)}
.timer.idle{color:var(--dim)}
.route{display:flex;align-items:center;padding:0 calc(var(--u)*1.6);
font-size:calc(var(--u)*3.2);color:var(--white);white-space:nowrap;overflow:hidden}
.route b{color:var(--blue);margin-right:calc(var(--u)*1)}
#dest{flex:0 1 auto;min-width:0;overflow:hidden;text-overflow:ellipsis}
.route .rdist,.route .gmk{flex:0 0 auto}
.refl{display:flex;align-items:center;justify-content:space-between;background:var(--blue);
color:#fff;padding:0 calc(var(--u)*1.6);font-weight:700;font-size:calc(var(--u)*3.6);
letter-spacing:.06em;text-transform:uppercase;white-space:nowrap;overflow:hidden}
.refl span.n{font-size:calc(var(--u)*2.6);letter-spacing:0}
.meter{display:flex;align-items:center;gap:calc(var(--u)*.5);padding:0 calc(var(--u)*1.6)}
.seg{flex:1;height:55%;background:#16181d}
.seg.on{background:var(--white)}
.seg.on.hi{background:var(--red)}
.panels{display:grid;grid-template-columns:30fr 38fr 32fr;min-height:0}
.panel{display:flex;flex-direction:column;padding:calc(var(--u)*1) calc(var(--u)*1.6) 0;
border-right:1px solid #1d1d20;overflow:hidden;min-width:0;min-height:0}
.panel:last-child{border-right:0}
.panel h2{display:flex;justify-content:space-between;margin:0 0 calc(var(--u)*.8);
font-size:calc(var(--u)*2.2);color:var(--red);letter-spacing:.12em;font-weight:700}
.panel h2 .ar{color:var(--dim);letter-spacing:0}
.rows{flex:1;min-height:0;overflow-y:auto;font-size:calc(var(--u)*2.7);line-height:1.5;
scrollbar-width:none;-ms-overflow-style:none;user-select:none;-webkit-user-select:none;
touch-action:pan-y;cursor:grab}
.rows::-webkit-scrollbar{display:none}
.r{display:flex;justify-content:space-between;gap:calc(var(--u)*1);white-space:nowrap}
.r .a{color:var(--white)}
.r .b{color:var(--dim)}
.r .c{color:var(--blue)}
.g{display:grid;grid-template-columns:calc(var(--u)*7) 1fr calc(var(--u)*9);align-items:center;
gap:calc(var(--u)*1);font-size:calc(var(--u)*2.7);line-height:1.55}
.g .k{color:var(--blue);font-weight:700}
.g .v{text-align:right;color:var(--white)}
.bar{height:calc(var(--u)*1.5);background:#16181d}
.bar i{display:block;height:100%;width:0;background:var(--white)}
.bar i.hot{background:var(--red)}
.g .v.hot{color:var(--red)}
.r .d{color:var(--red);flex:0 0 calc(var(--u)*8.5);text-align:right}
.rows[data-k="adb"] .r{display:grid;column-gap:calc(var(--u)*.6);grid-template-columns:minmax(0,1fr) calc(var(--u)*10.5) calc(var(--u)*9.7)}
.rows[data-k="bac"] .r{display:grid;column-gap:calc(var(--u)*.6);grid-template-columns:calc(var(--u)*13.5) minmax(0,1fr) calc(var(--u)*6.5)}
.rows[data-k="bacd"] .r{display:grid;column-gap:calc(var(--u)*.6);grid-template-columns:calc(var(--u)*8.5) minmax(0,1fr) calc(var(--u)*6.5) calc(var(--u)*10.5)}
.rows[data-k="adb"] .b,.rows[data-k] .c,.rows[data-k] .d{text-align:right}
.panels{grid-template-columns:31fr 40fr 29fr}
.top>*{flex:0 0 auto}
.top #nlinked{flex:0 1 auto;min-width:0;overflow:hidden;text-overflow:ellipsis}
.menubtn{font:inherit;font-weight:700;color:#fff;background:#7d0c1d;border:1px solid #fff;
border-radius:calc(var(--u)*.5);padding:0 calc(var(--u)*1.4);cursor:pointer;letter-spacing:.1em;
box-shadow:inset 0 calc(var(--u)*-.3) 0 rgba(0,0,0,.4)}
.menubtn:hover,.menubtn:focus-visible{background:#fff;color:var(--red);outline:0}
.badge.dmr{background:#19d36b;color:#00170a;box-shadow:0 0 calc(var(--u)*1.2) rgba(25,211,107,.6)}
.badge.dmr.wait{background:#7a5a00;color:#ffd35a;box-shadow:none;animation:blink 1s steps(2) infinite}
@keyframes blink{50%{opacity:.3}}
.rdist{margin-left:auto;padding-left:calc(var(--u)*2);font-weight:700;color:var(--red)}
.menu{position:absolute;left:0;top:0;right:0;bottom:0;z-index:5;background:var(--ink);
display:flex;flex-direction:column}
.mh{display:flex;justify-content:space-between;background:var(--blue);color:#fff;font-weight:700;
font-size:calc(var(--u)*3.6);letter-spacing:.14em;padding:calc(var(--u)*.8) calc(var(--u)*1.6)}
.mh span:last-child{font-size:calc(var(--u)*2.4);letter-spacing:0;align-self:center}
.mb{flex:1;min-height:0;overflow-y:auto;padding:calc(var(--u)*1.2) calc(var(--u)*1.6);scrollbar-width:none}
.mb::-webkit-scrollbar{display:none}
.mi{display:flex;justify-content:space-between;gap:calc(var(--u)*2);cursor:pointer;
font-size:calc(var(--u)*3.4);padding:calc(var(--u)*.6) calc(var(--u)*1.2);color:var(--white);
border-left:calc(var(--u)*.5) solid transparent;user-select:none;-webkit-user-select:none}
.mi .mv{color:var(--blue)}
.mi.dis{color:var(--dim)}
.mi.sel{background:var(--blue);color:#fff;border-left-color:var(--red)}
.mi.sel .mv{color:#fff}
.mlab{padding:calc(var(--u)*1.2) calc(var(--u)*1.6) calc(var(--u)*.4);font-size:calc(var(--u)*2.2);color:var(--blue);font-weight:700}
.mfield{display:block;width:calc(100% - var(--u)*3.2);margin:0 calc(var(--u)*1.6);font:inherit;font-size:calc(var(--u)*3);color:#fff;background:#14161b;border:1px solid #3a3f4b;padding:calc(var(--u)*.8);-webkit-user-select:text;user-select:text}
.mfield:focus{outline:0;border-color:var(--blue)}
.pimg{display:block;width:100%;max-width:calc(var(--u)*64);height:auto;margin-top:calc(var(--u)*.8);border:1px solid #3a3f4b}
.mb.pic{overflow:hidden;display:flex;flex-direction:column}
.mb.pic .mr{flex:1;min-height:0;display:flex;flex-direction:column;cursor:default}
.mb.pic .pimg{flex:1;min-height:0;width:100%;max-width:none;height:auto;object-fit:contain;object-position:center;margin-top:calc(var(--u)*.8);border:0}
.about{padding:calc(var(--u)*2) calc(var(--u)*1.6);font-size:calc(var(--u)*2.8);line-height:1.7;text-align:center}
.about a{color:var(--blue);text-decoration:underline;word-break:break-all}
.mq{text-align:center;font-size:calc(var(--u)*3.4);margin:calc(var(--u)*1) 0 calc(var(--u)*1.4)}
.dots{text-align:center;font-size:calc(var(--u)*5.5);font-weight:700;letter-spacing:.3em;
height:calc(var(--u)*7);color:var(--white)}
.pad{display:grid;grid-template-columns:repeat(3,calc(var(--u)*12));gap:calc(var(--u)*.9);justify-content:center}
.key{font:inherit;font-weight:700;color:#fff;background:#14161b;border:1px solid #3a3f4b;
border-radius:calc(var(--u)*.6);cursor:pointer;font-size:calc(var(--u)*3.4);height:calc(var(--u)*5.6);
box-shadow:inset 0 calc(var(--u)*-.3) 0 rgba(0,0,0,.5)}
.key:hover,.key:focus-visible,.key:active{background:var(--blue);outline:0}
.key.red{background:#5b0a16;border-color:var(--red)}
.mmsg{min-height:calc(var(--u)*4);padding:0 calc(var(--u)*1.6);font-size:calc(var(--u)*2.6);
color:var(--red);font-weight:700;text-align:center}
.mf{display:flex;gap:calc(var(--u)*1);justify-content:flex-end;padding:calc(var(--u)*.8) calc(var(--u)*1.6);
border-top:1px solid #1d1d20}
.mf .key{height:calc(var(--u)*5);padding:0 calc(var(--u)*2.4);font-size:calc(var(--u)*2.6)}
.gmk{font-size:.75em;line-height:1.15;margin-left:calc(var(--u)*1.4);padding:0 calc(var(--u)*1)}
.gmb{flex:1;min-height:0;display:flex;gap:calc(var(--u)*1.6);padding:calc(var(--u)*1.2) calc(var(--u)*1.6)}
.gmt{flex:0 0 46%;min-width:0;display:grid;grid-template-columns:1fr 1fr;grid-auto-rows:min-content;
gap:calc(var(--u)*.9);align-content:start;overflow-y:auto;scrollbar-width:none}
.gmt::-webkit-scrollbar{display:none}
.tile{background:#2c2f36;border-radius:calc(var(--u)*.7);padding:calc(var(--u)*.6) calc(var(--u)*1);min-width:0}
.tile.hot{background:#d94a2f}
.tile .tc{display:block;font-size:calc(var(--u)*3.2);white-space:nowrap;overflow:hidden}
.tile .td{display:flex;justify-content:space-between;font-size:calc(var(--u)*3);white-space:nowrap}
.tile .ta{display:inline-block;font-style:normal;font-weight:700}
.gmsvg{flex:1;min-width:0;min-height:0}
.rg{fill:none;stroke:#59616e;stroke-width:.8}
.rx{stroke:#3a3f4b;stroke-width:.6}
.rl{fill:#f4f4f4;font-size:9px;font-weight:700;text-anchor:middle;font-family:inherit}
.rl.n{fill:var(--red)}
.rb{fill:#9aa0a8}
.rb.hot{fill:var(--red)}
.ra{fill:#3ddc84}
.rt{fill:#f4f4f4;font-size:7px;font-family:inherit}
.rme{fill:var(--red)}
.mailb{font:inherit;font-weight:700;border:0;cursor:pointer;background:#ffd35a;color:#2a1c00;
display:inline-flex;align-items:center;gap:calc(var(--u)*.6);animation:mp 1.6s steps(2) infinite}
.mailb:hover,.mailb:focus-visible{background:#fff;outline:0}
@keyframes mp{50%{opacity:.55}}
.env{width:calc(var(--u)*2.6);height:calc(var(--u)*1.9)}
.mgl{padding:0}
.mr{padding:calc(var(--u)*.8) calc(var(--u)*1.6);border-left:calc(var(--u)*.5) solid transparent;
border-bottom:1px solid #1d1d20;cursor:pointer;user-select:none;-webkit-user-select:none}
.mr.sel{background:var(--blue);border-left-color:var(--red)}
.mrh{display:flex;align-items:center;gap:calc(var(--u)*1.2);font-size:calc(var(--u)*3.2);font-weight:700}
.mrt{margin-left:auto;font-weight:400;color:var(--dim)}
.mr.sel .mrt{color:#fff}
.mnew{background:var(--red);color:#fff;padding:0 calc(var(--u)*.9);border-radius:calc(var(--u)*.4);
font-size:calc(var(--u)*2.3)}
.mx{margin-top:calc(var(--u)*.4);font-size:calc(var(--u)*3);white-space:pre-wrap;overflow-wrap:anywhere;color:var(--white)}
@media (max-aspect-ratio:1/1){
html,body{height:auto;min-height:100%}
body{display:block;-webkit-text-size-adjust:100%;text-size-adjust:100%}
.screen{--u:1.45vw;margin:0;width:100%;aspect-ratio:auto;min-height:100vh;border:0;border-radius:0;
display:block;box-shadow:none}
.top{flex-wrap:wrap;row-gap:calc(var(--u)*.8);padding:calc(var(--u)*1.4) calc(var(--u)*1.6);
font-size:calc(var(--u)*3)}
.top .sp{flex:0 0 100%;height:0}
#clock{margin-left:auto}
.vfo{padding:calc(var(--u)*2) calc(var(--u)*1.6)}
.route{padding:calc(var(--u)*1.4) calc(var(--u)*1.6);font-size:calc(var(--u)*3.4);flex-wrap:wrap;row-gap:calc(var(--u)*.8)}
#dest{white-space:nowrap}
.rdist{margin-left:0}
.gmk{margin-left:auto}
.refl{padding:calc(var(--u)*1.4) calc(var(--u)*1.6)}
.refl span.n{display:none}
.meter{height:calc(var(--u)*5)}
.panels{display:block}
.panel{height:calc(var(--u)*32);border-right:0;border-bottom:1px solid #1d1d20}
.panel:last-child{height:auto;padding-bottom:calc(var(--u)*2)}
.panels{display:block}
.menu{position:fixed}
.rdist{padding-left:calc(var(--u)*1)}
.pad{grid-template-columns:repeat(3,calc(var(--u)*16))}
.key{height:calc(var(--u)*8)}
.mf .key{height:calc(var(--u)*7)}
.mrh{font-size:calc(var(--u)*3.8)}
.mx{font-size:calc(var(--u)*3.8)}
.gmb{flex-direction:column;overflow-y:auto}
.gmsvg{order:-1;flex:0 0 auto;width:100%;height:auto;aspect-ratio:1}
.gmt{flex:0 0 auto;overflow:visible}
}
"""

JS = r"""
(function () {
  'use strict';
  var FADE = 20;
  /* size the display to the visible viewport (iOS Safari's 100vh includes the hidden toolbar) */
  function fit() { document.documentElement.style.setProperty('--vh', (window.innerHeight / 100) + 'px'); }
  fit();
  window.addEventListener('resize', fit);
  window.addEventListener('orientationchange', function () { setTimeout(fit, 250); });
  if (window.visualViewport) { window.visualViewport.addEventListener('resize', fit); }
  var CALL_RE = /^[A-Z0-9]{1,3}[0-9][A-Z]{1,4}(\/[A-Z0-9]{1,4})?$/;
  function $(id) { return document.getElementById(id); }
  function pad(n) { return (n < 10 ? '0' : '') + n; }
  function mmss(s) { s = Math.max(0, Math.floor(s)); return pad(Math.floor(s / 60)) + ':' + pad(s % 60); }
  function fmt(s) {
    if (s === null || s === undefined) { return '--'; }
    s = Math.max(0, Math.round(s));
    var d = Math.floor(s / 86400), h = Math.floor(s % 86400 / 3600), m = Math.floor(s % 3600 / 60);
    if (d > 0) { return d + 'd' + pad(h) + 'h'; }
    if (h > 0) { return h + 'h' + pad(m) + 'm'; }
    return m + 'm' + pad(s % 60) + 's';
  }
  function clear(el) { while (el.firstChild) { el.removeChild(el.firstChild); } }

  /* callsign -> QRZ.com link in a new window (only for valid-looking callsigns) */
  function callNode(text) {
    var up = String(text).toUpperCase();
    if (!CALL_RE.test(up)) { return document.createTextNode(text); }
    var a = document.createElement('a');
    a.href = 'https://www.qrz.com/db/' + encodeURIComponent(up.split('/')[0]);
    a.target = '_blank';
    a.rel = 'noopener noreferrer';
    a.title = 'QRZ.com: ' + up;
    a.textContent = text;
    return a;
  }

  var SEGS = 20, FULL = 60;
  var meter = $('meter');
  var segs = [];
  for (var i = 0; i < SEGS; i++) {
    var sg = document.createElement('span');
    sg.className = 'seg';
    meter.appendChild(sg);
    segs.push(sg);
  }
  var talkStart = null, curKey = null, lastDur = 0;
  var live = false, idleSince = null, lastCall = null, sizeCls = '';

  function setMeter(sec) {
    var lit = Math.min(SEGS, Math.round(sec / FULL * SEGS));
    for (var j = 0; j < SEGS; j++) {
      segs[j].className = 'seg' + (j < lit ? ' on' : '') + (j >= SEGS * 0.75 ? ' hi' : '');
    }
  }

  /* big callsign: white while live, grey after TX, darker after FADE seconds */
  var talkId = '';
  function destInfo(d) {
    d = String(d || '');
    if (d === '' || /^\*+$/.test(d)) { return {to: 'ALL', id: ''}; }
    if (d.indexOf('*****') === 0) { return {to: 'ALL', id: d.slice(5)}; }
    return {to: d, id: ''};
  }
  function paintCall() {
    var cls = 'call' + sizeCls;
    if (live) { cls += ' live'; }
    else if (idleSince === null || Date.now() - idleSince > FADE * 1000) { cls += ' old'; }
    var c = $('call');
    if (c.className !== cls) { c.className = cls; }
  }

  /* scrolling lists */
  var sigs = {};
  function arrows(boxId, arId) {
    var b = $(boxId), max = b.scrollHeight - b.clientHeight;
    var up = b.scrollTop > 3, dn = max > 3 && b.scrollTop < max - 3;
    $(arId).textContent = (up ? '▲' : '') + (dn ? '▼' : '');
  }
  function rows(id, list, classes) {
    var sig = JSON.stringify(list);
    if (sigs[id] === sig) { return; }
    sigs[id] = sig;
    var box = $(id), st = box.scrollTop;
    box.setAttribute('data-k', classes.join(''));
    clear(box);
    list.forEach(function (r) {
      var div = document.createElement('div'); div.className = 'r';
      r.forEach(function (c, idx) {
        var sp = document.createElement('span');
        sp.className = classes[idx] || 'b';
        if (c !== null && typeof c === 'object') { sp.appendChild(callNode(c.call)); }
        else { sp.textContent = c; }
        div.appendChild(sp);
      });
      box.appendChild(div);
    });
    box.scrollTop = st;
  }
  function dragScroll(el) {
    var down = false, sy = 0, st = 0, moved = false;
    el.addEventListener('mousedown', function (e) { down = true; moved = false; sy = e.clientY; st = el.scrollTop; });
    window.addEventListener('mousemove', function (e) {
      if (!down) { return; }
      var dy = e.clientY - sy;
      if (Math.abs(dy) > 4) { moved = true; }
      el.scrollTop = st - dy;
    });
    window.addEventListener('mouseup', function () { down = false; });
    el.addEventListener('click', function (e) {
      if (moved) { e.preventDefault(); e.stopPropagation(); moved = false; }
    }, true);
  }
  dragScroll($('linked'));
  dragScroll($('heard'));
  $('linked').addEventListener('scroll', function () { arrows('linked', 'lkar'); });
  $('heard').addEventListener('scroll', function () { arrows('heard', 'hdar'); });

  function gauge(key, val, max, unit, hotAt) {
    var v = $(key + 'v'), b = $(key + 'b');
    if (val === null || val === undefined) {
      v.textContent = '--'; b.style.width = '0%'; return;
    }
    var hot = val >= hotAt;
    v.textContent = val + unit;
    v.className = 'v' + (hot ? ' hot' : '');
    b.style.width = Math.min(100, val / max * 100) + '%';
    b.className = hot ? 'hot' : '';
  }

  /* ---- MENU, location and distance ---- */
  var store = {
    get: function (k, d) { try { var v = window.localStorage.getItem('ysf.' + k); return v === null ? d : v; } catch (e) { return d; } },
    set: function (k, v) { try { window.localStorage.setItem('ysf.' + k, v); } catch (e) {} }
  };
  var LOCS = ['OFF', 'NODE', 'GPS'];
  var opt = {units: store.get('units', 'KM') === 'MI' ? 'MI' : 'KM',
             loc: LOCS.indexOf(store.get('loc', 'NODE')) >= 0 ? store.get('loc', 'NODE') : 'NODE'};
  var last = null, admin = null, dmrPending = null;
  var gps = null, gpsErr = null, gpsAt = 0;
  var menuOpen = false, view = 'list', sel = 0, pin = '', msg = '', wantDmr = false;
  var wantAllow = false, allowList = [], allowArm = -1, allowArmAt = 0;

  function origin() {
    if (opt.loc === 'GPS') { return gps; }
    if (opt.loc === 'NODE' && last && last.loc) { return {lat: last.loc.lat, lon: last.loc.lon}; }
    return null;
  }
  function requestGps() {
    if (!navigator.geolocation) { gpsErr = 'N/A'; return; }
    navigator.geolocation.getCurrentPosition(function (p) {
      gps = {lat: p.coords.latitude, lon: p.coords.longitude};
      gpsAt = Date.now(); gpsErr = null;
      if (last) { render(last); }
      if (menuOpen) { drawMenu(); }
    }, function (e) {
      gpsErr = e.code === 1 ? 'DENIED' : 'NO FIX';
      if (e.code === 1) { opt.loc = 'NODE'; store.set('loc', 'NODE'); }
      if (menuOpen) { drawMenu(); }
    }, {enableHighAccuracy: false, timeout: 15000, maximumAge: 300000});
  }
  if (opt.loc === 'GPS' && navigator.permissions && navigator.permissions.query) {
    try {
      navigator.permissions.query({name: 'geolocation'}).then(function (r) {
        if (r.state === 'granted') { requestGps(); } else { opt.loc = 'NODE'; }
      });
    } catch (e) { opt.loc = 'NODE'; }
  } else if (opt.loc === 'GPS') { opt.loc = 'NODE'; }

  /* ---- LOG > MESSAGE ---- */
  var seenId = parseInt(store.get('seen', '-1'), 10);
  if (isNaN(seenId)) { seenId = -1; }
  var msgOpen = false, msgs = [], msel = 0, msgLoaded = false, msgNewFrom = 0, msgLatest = 0, logKind = 'msg', delArm = 0, pvKey = -1;
  var MON = ['JAN', 'FEB', 'MAR', 'APR', 'MAY', 'JUN', 'JUL', 'AUG', 'SEP', 'OCT', 'NOV', 'DEC'];
  var pseenId = parseInt(store.get('pseen', '-1'), 10);
  if (isNaN(pseenId)) { pseenId = -1; }
  function picUnread() {
    var n = 0;
    if (last && last.pics) { last.pics.ids.forEach(function (i) { if (i > pseenId) { n++; } }); }
    return n;
  }
  function picUpdate(d) {
    if (!d.pics) { $('picb').className = 'badge mailb hidden'; return; }
    if (pseenId < 0) { pseenId = d.pics.latest; store.set('pseen', String(pseenId)); }
    var n = picUnread();
    $('picb').className = 'badge mailb' + (n ? '' : ' hidden');
    $('picn').textContent = n > 9 ? '9+' : String(n);
    if (msgOpen && logKind === 'pic' && d.pics.latest !== msgLatest) { loadMsgs(false); }
  }
  function unreadCount() {
    var n = 0;
    if (last && last.msg) { last.msg.ids.forEach(function (i) { if (i > seenId) { n++; } }); }
    return n;
  }
  function markSeen() {
    if (last && last.msg) {
      seenId = Math.max(seenId, last.msg.latest); store.set('seen', String(seenId)); mailUpdate(last);
    }
  }
  function mailUpdate(d) {
    if (!d.msg) { $('mail').className = 'badge mailb hidden'; return; }
    if (seenId < 0) { seenId = d.msg.latest; store.set('seen', String(seenId)); }
    var n = unreadCount();
    $('mail').className = 'badge mailb' + (n ? '' : ' hidden');
    $('mailn').textContent = n > 9 ? '9+' : String(n);
    if (msgOpen && logKind === 'msg' && d.msg.latest !== msgLatest) { loadMsgs(false); }
  }
  function newsUpdate(d) {
    if (msgOpen && logKind === 'news' && d.news && d.news.latest !== msgLatest) { loadMsgs(false); }
  }
  function msgTime(t) {
    var d = new Date(t * 1000);
    return pad(d.getHours()) + ':' + pad(d.getMinutes()) + '  ' + d.getDate() + ' ' + MON[d.getMonth()];
  }
  function buildMsg() {
    var b = $('mgb'); clear(b);
    b.className = 'mb mgl' + (logKind === 'pic' ? ' pic' : '');
    pvKey = msel;
    if (!msgs.length) {
      var e = document.createElement('div'); e.className = 'mq';
      e.textContent = msgLoaded ? (logKind === 'news' ? 'NO NEWS' : (logKind === 'pic' ? 'NO PICTURES' : 'NO MESSAGES')) : 'LOADING...';
      b.appendChild(e);
    }
    msgs.forEach(function (m, i) {
      if (logKind === 'pic' && i !== msel) { return; }          // pictures: one at a time, fitted to the screen
      var r = document.createElement('div'); r.className = 'mr' + (i === msel ? ' sel' : '');
      var h = document.createElement('div'); h.className = 'mrh';
      var c = document.createElement('span'); c.textContent = m.call; h.appendChild(c);
      if (m.id > msgNewFrom) { var nw = document.createElement('span'); nw.className = 'mnew'; nw.textContent = 'NEW'; h.appendChild(nw); }
      var t = document.createElement('span'); t.className = 'mrt'; t.textContent = msgTime(m.t); h.appendChild(t);
      if (logKind === 'pic' && !m.ok) { var dm = document.createElement('span'); dm.className = 'mnew'; dm.textContent = 'DAMAGED'; h.insertBefore(dm, t); }
      var x = document.createElement('div'); x.className = 'mx';
      x.textContent = logKind === 'pic' ? m.w + 'x' + m.h + '  ' + (m.size / 1024).toFixed(1) + 'KB' : m.text;
      r.appendChild(h); r.appendChild(x);
      if (logKind === 'pic') {
        var im = document.createElement('img'); im.className = 'pimg'; im.alt = 'Picture from ' + m.call;
        im.loading = 'lazy'; im.src = 'pic/' + m.id + '.jpg'; r.appendChild(im);
      }
      r.addEventListener('click', function () { msel = i; selMsg(); });
      b.appendChild(r);
    });
    selMsg();
  }
  function selMsg() {
    if (logKind === 'pic' && msgs.length && pvKey !== msel) { buildMsg(); return; }
    var kids = $('mgb').children;
    for (var i = 0; logKind !== 'pic' && i < kids.length; i++) {
      if (kids[i].className.indexOf('mr') === 0) { kids[i].className = 'mr' + (i === msel ? ' sel' : '') ; }
    }
    $('mgpos').textContent = msgs.length ? (msel + 1) + '/' + msgs.length : '';
    disarmDel();
    var el = kids[logKind === 'pic' ? 0 : msel];
    if (msgs.length && el && el.scrollIntoView) { try { el.scrollIntoView({block: 'nearest'}); } catch (e) {} }
  }
  function moveMsg(dir) {
    if (!msgs.length) { return; }
    msel = Math.max(0, Math.min(msgs.length - 1, msel + dir)); selMsg();
  }
  function loadMsgs(first) {
    var x = new XMLHttpRequest();
    x.open('GET', logKind === 'news' ? 'api/news' : (logKind === 'pic' ? 'api/pictures' : 'api/messages'), true);
    x.onload = function () {
      if (x.status !== 200 || !msgOpen) { return; }
      var j = {}; try { j = JSON.parse(x.responseText); } catch (e) { return; }
      var keep = (!first && msgs[msel]) ? msgs[msel].id : null;
      msgs = j.msgs || j.pics || []; msgLoaded = true; msgLatest = msgs.length ? msgs[0].id : 0;
      msel = 0;
      if (keep !== null) { for (var i = 0; i < msgs.length; i++) { if (msgs[i].id === keep) { msel = i; } } }
      buildMsg();
      if (first && logKind === 'msg') { markSeen(); }
    };
    x.send();
  }
  function openMsg(kind, fromLog) {
    msgFromLog = fromLog ? sel + 1 : 0;
    closeGm(); if (menuOpen) { closeMenu(); }
    logKind = kind === 'news' ? 'news' : (kind === 'pic' ? 'pic' : 'msg');
    msgOpen = true; msgs = []; msel = 0; msgLoaded = false; msgLatest = 0;
    msgNewFrom = logKind === 'msg' ? seenId : (logKind === 'pic' ? pseenId : 1e15);
    $('mgt').textContent = logKind === 'news' ? 'LOG NEWS' : (logKind === 'pic' ? 'LOG PICTURES' : 'LOG MSG');
    $('mg').className = 'menu'; buildMsg(); loadMsgs(true); $('mback').focus();
  }
  var msgFromLog = 0;
  function closeMsg() {
    if (!msgOpen) { return; }
    msgOpen = false; $('mg').className = 'menu hidden'; disarmDel();
    if (logKind === 'msg') { markSeen(); }
    if (logKind === 'pic' && last && last.pics) { pseenId = Math.max(pseenId, last.pics.latest); store.set('pseen', String(pseenId)); picUpdate(last); }
    if (msgFromLog) { var s0 = msgFromLog - 1; msgFromLog = 0; openMenu(); view = 'log'; sel = s0; drawMenu(); return; }
    $('menubtn').focus();
  }
  function disarmDel() {
    delArm = 0;
    var b = $('mdel'); b.textContent = 'DEL';
    b.className = 'key' + (admin && msgs.length ? ' red' : ' hidden');
  }
  function delSel() {
    if (!admin || !msgs.length) { return; }
    var m = msgs[msel];
    if (!delArm) {
      delArm = Date.now(); $('mdel').textContent = 'SURE?';
      setTimeout(function () { if (delArm && Date.now() - delArm >= 2900) { disarmDel(); } }, 3000);
      return;
    }
    disarmDel();
    post('api/delete', {kind: logKind, id: m.id}, function (code) {
      if (code === 200) { loadMsgs(false); }
      else if (code === 401) { admin = null; disarmDel(); $('mgpos').textContent = 'LOGIN NEEDED'; }
      else { $('mgpos').textContent = 'DELETE FAILED'; }
    });
  }
  $('mail').addEventListener('click', function () { openMsg('msg'); });
  $('picb').addEventListener('click', function () { openMsg('pic'); });
  $('mdel').addEventListener('click', delSel);
  $('mback').addEventListener('click', closeMsg);
  $('mtop').addEventListener('click', function () { msel = 0; selMsg(); });
  $('mup').addEventListener('click', function () { moveMsg(-1); });
  $('mdn').addEventListener('click', function () { moveMsg(1); });

  var PTS = ['N', 'NE', 'E', 'SE', 'S', 'SW', 'W', 'NW'];
  function distText(call, withDir) {
    var o = origin(), p = last && last.pos && last.pos[call];
    if (!o || !p) { return ''; }
    var R = Math.PI / 180, la1 = o.lat * R, la2 = p[0] * R, dlo = (p[1] - o.lon) * R, dla = la2 - la1;
    var h = Math.sin(dla / 2) * Math.sin(dla / 2) + Math.cos(la1) * Math.cos(la2) * Math.sin(dlo / 2) * Math.sin(dlo / 2);
    var km = 2 * 6371 * Math.asin(Math.min(1, Math.sqrt(h)));
    var y = Math.sin(dlo) * Math.cos(la2);
    var x = Math.cos(la1) * Math.sin(la2) - Math.sin(la1) * Math.cos(la2) * Math.cos(dlo);
    var brg = (Math.atan2(y, x) / R + 360) % 360;
    var v = opt.units === 'MI' ? km * 0.621371 : km;
    var t = v < 1 ? '<1' : (v < 10 ? v.toFixed(1) : String(Math.round(v)));
    t += opt.units === 'MI' ? 'mi' : 'km';
    return withDir ? t + ' ' + PTS[Math.round(brg / 45) % 8] : t;
  }

  function post(url, obj, cb) {
    var x = new XMLHttpRequest();
    x.open('POST', url, true);
    x.setRequestHeader('Content-Type', 'application/json');
    x.setRequestHeader('X-Requested-With', 'ysfradio');
    if (admin) { x.setRequestHeader('X-CSRF', admin); }
    x.onload = function () { var j = {}; try { j = JSON.parse(x.responseText); } catch (e) {} cb(x.status, j); };
    x.onerror = function () { cb(0, {}); };
    x.send(JSON.stringify(obj));
  }

  function menuItems() {
    var d = last || {}, st = d.dmr, l = [];
    l.push({k: 'dmr', label: 'DMR X-LINK', val: st === null || st === undefined ? 'N/A' : (st === 'active' ? 'ON' : 'OFF'),
            dis: st === null || st === undefined});
    l.push({k: 'loc', label: 'MY LOCATION', val: opt.loc + (opt.loc === 'GPS' ? (gpsErr ? ' ' + gpsErr : (gps ? ' OK' : ' ...')) : '')});
    l.push({k: 'units', label: 'DISTANCE UNIT', val: opt.units === 'MI' ? 'MILES' : 'KM'});
    l.push({k: 'gm', label: 'RADAR VIEW', val: d.aprs ? 'OPEN' : 'N/A', dis: !d.aprs});
    l.push({k: 'log', label: 'LOG', val: (d.msg || d.news || d.pics) ? (unreadCount() ? unreadCount() + ' NEW' : 'OPEN') : 'N/A', dis: !(d.msg || d.news || d.pics)});
    l.push({k: 'aprs', label: 'APRS POSITIONS', val: d.aprs ? (d.aprs.connected ? 'LIVE ' : 'OFFLINE ') + d.aprs.known : 'N/A', dis: true});
    l.push({k: 'allow', label: 'PIC SENDERS', val: d.pics ? (admin ? String(allowList.length) : 'ADMIN') : 'N/A', dis: !d.pics});
    l.push({k: 'admin', label: 'ADMIN', val: admin ? 'LOGOUT' : (d.auth ? 'LOGIN' : 'NO PIN SET'), dis: !admin && !d.auth});
    l.push({k: 'about', label: 'ABOUT', val: ''});
    l.push({k: 'exit', label: 'EXIT', val: ''});
    return l;
  }
  function confirmItems() {
    var on = last && last.dmr === 'active';
    return [{k: 'yes', label: on ? 'YES - STOP DMR X-LINK' : 'YES - START DMR X-LINK', val: ''},
            {k: 'no', label: 'NO - CANCEL', val: ''}];
  }
  function logItems() {
    var d = last || {}, n = d.msg ? d.msg.ids.length : 0, u = unreadCount();
    var nn = d.news ? d.news.ids.length : 0;
    return [{k: 'message', label: 'MESSAGE', val: d.msg ? n + (u ? '  ' + u + ' NEW' : '') : 'N/A', dis: !d.msg},
            {k: 'news', label: 'NEWS', val: d.news ? String(nn) : 'N/A', dis: !d.news},
            {k: 'pics', label: 'PICTURES', val: d.pics ? String(d.pics.ids.length) : 'N/A', dis: !d.pics},
            {k: 'back', label: 'BACK', val: ''}];
  }
  function allowItems() {
    var l = allowList.map(function (e) { return {k: 'ent', label: e.call, val: e.id || 'ANY RADIO'}; });
    l.push({k: 'add', label: 'ADD SENDER', val: ''});
    l.push({k: 'back', label: 'BACK', val: ''});
    return l;
  }
  function loadAllow() {
    post('api/picallow', {op: 'list'}, function (code, j) {
      if (code === 200) { allowList = j.allow || []; }
      else if (code === 401) { admin = null; view = 'pin'; wantAllow = true; pin = ''; msg = 'SESSION EXPIRED'; }
      if (menuOpen) { drawMenu(); }
    });
  }
  function submitAllow(op, call, id) {
    post('api/picallow', {op: op, call: call, id: id}, function (code, j) {
      if (code === 200) { allowList = j.allow || []; msg = op === 'add' ? 'ADDED ' + call : 'REMOVED ' + call; view = 'allow'; sel = 0; }
      else if (code === 401) { admin = null; view = 'pin'; wantAllow = true; pin = ''; msg = 'SESSION EXPIRED'; }
      else if (code === 400) { msg = 'BAD CALLSIGN OR RADIO ID'; }
      else if (code === 404) { msg = 'NOT FOUND'; }
      else { msg = 'REQUEST FAILED'; }
      if (menuOpen) { drawMenu(); }
    });
  }
  function curItems() {
    return view === 'confirm' ? confirmItems() : (view === 'log' ? logItems() : (view === 'allow' ? allowItems() : menuItems()));
  }

  function drawMenu() {
    var body = $('mbody'), title = $('mtitle');
    clear(body);
    $('mmsg').textContent = msg;
    if (view === 'pin') {
      title.textContent = 'ADMIN PIN';
      var q = document.createElement('div'); q.className = 'dots';
      q.textContent = pin.length ? new Array(pin.length + 1).join('*') : '----';
      body.appendChild(q);
      var pad = document.createElement('div'); pad.className = 'pad';
      ['1', '2', '3', '4', '5', '6', '7', '8', '9', 'CLR', '0', 'ENT'].forEach(function (k) {
        var b = document.createElement('button');
        b.type = 'button'; b.className = 'key' + (k.length > 1 ? ' red' : ''); b.textContent = k;
        b.addEventListener('click', function () { padKey(k); });
        pad.appendChild(b);
      });
      body.appendChild(pad);
      return;
    }
    if (view === 'addcall') {
      title.textContent = 'ADD SENDER';
      $('mnode').textContent = (last && last.node) || '';
      var l1 = document.createElement('div'); l1.className = 'mlab'; l1.textContent = 'CALLSIGN';
      var i1 = document.createElement('input'); i1.id = 'acall'; i1.className = 'mfield'; i1.maxLength = 12;
      i1.setAttribute('autocapitalize', 'characters'); i1.setAttribute('autocomplete', 'off'); i1.setAttribute('spellcheck', 'false');
      var l2 = document.createElement('div'); l2.className = 'mlab'; l2.textContent = 'RADIO ID (OPTIONAL, 5 CHARACTERS)';
      var i2 = document.createElement('input'); i2.id = 'aid'; i2.className = 'mfield'; i2.maxLength = 5;
      i2.setAttribute('autocomplete', 'off'); i2.setAttribute('spellcheck', 'false'); i2.setAttribute('autocapitalize', 'off');
      body.appendChild(l1); body.appendChild(i1); body.appendChild(l2); body.appendChild(i2);
      try { i1.focus(); } catch (e) {}
      return;
    }
    if (view === 'about') {
      title.textContent = 'ABOUT';
      $('mnode').textContent = (last && last.node) || '';
      var ab = document.createElement('div'); ab.className = 'about';
      var l1 = document.createElement('div'); l1.textContent = 'YSF-XTRA';
      var l2 = document.createElement('div'); l2.textContent = 'Written by Scot Forshaw (M0RWV)';
      var l3 = document.createElement('a');
      l3.href = 'https://github.com/forshaws/YSF-XTRA'; l3.target = '_blank'; l3.rel = 'noopener noreferrer';
      l3.textContent = 'github.com/forshaws/YSF-XTRA';
      ab.appendChild(l1); ab.appendChild(l2); ab.appendChild(l3);
      body.appendChild(ab);
      return;
    }
    var items = curItems();
    if (sel >= items.length) { sel = items.length - 1; }
    title.textContent = view === 'confirm' ? 'CONFIRM' : (view === 'log' ? 'LOG' : (view === 'allow' ? 'PIC SENDERS' : 'MENU'));
    $('mnode').textContent = (last && last.node) || '';
    if (view === 'confirm') {
      var cq = document.createElement('div'); cq.className = 'mq';
      cq.textContent = 'DMR / ALLSTAR X-LINK';
      body.appendChild(cq);
    }
    items.forEach(function (it, i) {
      var div = document.createElement('div');
      div.className = 'mi' + (i === sel ? ' sel' : '') + (it.dis ? ' dis' : '');
      var a = document.createElement('span'); a.textContent = it.label;
      var b = document.createElement('span'); b.className = 'mv'; b.textContent = it.val;
      div.appendChild(a); div.appendChild(b);
      div.addEventListener('click', function () { sel = i; drawMenu(); activate(); });
      body.appendChild(div);
    });
  }
  function openMenu() { closeGm(); menuOpen = true; view = 'list'; sel = 0; pin = ''; msg = ''; wantAllow = false; $('menu').className = 'menu'; drawMenu(); $('ksel').focus(); }
  function closeMenu() { menuOpen = false; $('menu').className = 'menu hidden'; $('menubtn').focus(); }
  function back() {
    if (view === 'addcall') { view = 'allow'; msg = ''; drawMenu(); }
    else if (view !== 'list') { view = 'list'; pin = ''; msg = ''; wantAllow = false; drawMenu(); } else { closeMenu(); }
  }
  function move(dir) {
    if (view === 'pin' || view === 'about' || view === 'addcall') { return; }
    var n = curItems().length;
    sel = (sel + dir + n) % n; drawMenu();
  }
  function sendBridge() {
    var action = last && last.dmr === 'active' ? 'stop' : 'start';
    msg = 'SENDING...'; view = 'list'; drawMenu();
    post('api/bridge', {action: action}, function (code) {
      if (code === 200) {
        dmrPending = {want: action === 'start' ? 'active' : 'inactive', until: Date.now() + 25000};
        msg = action === 'start' ? 'STARTING DMR X-LINK' : 'STOPPING DMR X-LINK';
      } else if (code === 401) {
        admin = null; view = 'pin'; wantDmr = true; pin = ''; msg = 'SESSION EXPIRED';
      } else if (code === 429) { msg = 'TOO FAST - WAIT'; }
      else { msg = 'REQUEST FAILED'; }
      if (menuOpen) { drawMenu(); }
    });
  }
  function submitPin() {
    if (pin.length < 4) { msg = 'PIN TOO SHORT'; drawMenu(); return; }
    var p = pin; pin = ''; msg = 'CHECKING...'; drawMenu();
    post('api/login', {pin: p}, function (code, j) {
      if (code === 200 && j.csrf) {
        admin = j.csrf; msg = 'ADMIN OK';
        if (wantAllow) { wantAllow = false; wantDmr = false; view = 'allow'; sel = 0; loadAllow(); }
        else { view = wantDmr ? 'confirm' : 'list'; sel = 0; wantDmr = false; }
      } else if (code === 429) { msg = 'LOCKED - TRY LATER'; }
      else if (code === 403) { msg = 'WRONG PIN'; }
      else { msg = 'LOGIN UNAVAILABLE'; }
      drawMenu();
    });
  }
  function padKey(k) {
    if (k === 'CLR') { pin = ''; msg = ''; }
    else if (k === 'ENT') { submitPin(); return; }
    else if (pin.length < 12) { pin += k; }
    drawMenu();
  }
  function activate() {
    var it = curItems()[sel]; if (!it && view !== 'about' && view !== 'addcall') { return; }
    msg = '';
    if (view === 'about') { view = 'list'; drawMenu(); return; }
    if (view === 'addcall') {
      var c = ($('acall').value || '').toUpperCase().replace(/\s+/g, ''), rid = ($('aid').value || '').replace(/\s+/g, '');
      if (!/^[A-Z0-9\/-]{3,12}$/.test(c) || !(rid === '' || /^[A-Za-z0-9]{5}$/.test(rid))) { msg = 'BAD CALLSIGN OR RADIO ID'; drawMenu(); return; }
      submitAllow('add', c, rid); return;
    }
    if (view === 'allow') {
      if (it.k === 'ent') {
        var e = allowList[sel];
        if (allowArm === sel && Date.now() - allowArmAt < 3000) { allowArm = -1; submitAllow('remove', e.call, e.id); return; }
        allowArm = sel; allowArmAt = Date.now(); msg = 'SELECT AGAIN TO REMOVE ' + e.call;
      } else if (it.k === 'add') { view = 'addcall'; }
      else { view = 'list'; sel = 0; }
      drawMenu(); return;
    }
    if (view === 'confirm') {
      if (it.k === 'yes') { sendBridge(); return; }
      view = 'list'; drawMenu(); return;
    }
    if (view === 'log') {
      if (it.k === 'message' && !it.dis) { var s1 = sel; closeMenu(); sel = s1; openMsg('msg', true); return; }
      if (it.k === 'news' && !it.dis) { var s2 = sel; closeMenu(); sel = s2; openMsg('news', true); return; }
      if (it.k === 'pics' && !it.dis) { var s4 = sel; closeMenu(); sel = s4; openMsg('pic', true); return; }
      if (it.dis) { drawMenu(); return; }
      view = 'list'; sel = 0; drawMenu(); return;
    }
    var d = last || {};
    if (it.k === 'dmr') {
      if (d.dmr === null || d.dmr === undefined) { msg = 'DMR CONTROL NOT CONFIGURED'; }
      else if (!d.auth) { msg = 'ADMIN PIN NOT SET ON SERVER'; }
      else if (!admin) { view = 'pin'; pin = ''; wantDmr = true; }
      else { view = 'confirm'; sel = 0; }
    } else if (it.k === 'loc') {
      opt.loc = LOCS[(LOCS.indexOf(opt.loc) + 1) % LOCS.length];
      store.set('loc', opt.loc);
      if (opt.loc === 'GPS') { gpsErr = null; if (gps) { gpsAt = 0; } requestGps(); }
      if (last) { render(last); }
    } else if (it.k === 'gm') {
      if (d.aprs) { var s3 = sel; closeMenu(); sel = s3; openGm(true); return; }
      msg = 'NEEDS APRS ON THE SERVER';
    } else if (it.k === 'log') {
      if (!d.msg && !d.news && !d.pics) { msg = 'NEEDS --messages, --news OR --pictures ON THE SERVER'; } else { view = 'log'; sel = 0; }
    } else if (it.k === 'units') {
      opt.units = opt.units === 'MI' ? 'KM' : 'MI'; store.set('units', opt.units);
      if (last) { render(last); }
    } else if (it.k === 'admin') {
      if (admin) { post('api/logout', {}, function () { admin = null; msg = 'LOGGED OUT'; drawMenu(); }); }
      else if (d.auth) { view = 'pin'; pin = ''; wantDmr = false; }
      else { msg = 'ADMIN PIN NOT SET ON SERVER'; }
    } else if (it.k === 'allow') {
      if (!d.pics) { msg = 'NEEDS --pictures ON THE SERVER'; }
      else if (!admin) {
        if (!d.auth) { msg = 'ADMIN PIN NOT SET ON SERVER'; }
        else { view = 'pin'; pin = ''; wantDmr = false; wantAllow = true; }
      } else { view = 'allow'; sel = 0; loadAllow(); }
    } else if (it.k === 'about') { view = 'about'; }
    else if (it.k === 'exit') { closeMenu(); return; }
    drawMenu();
  }
  $('menubtn').addEventListener('click', openMenu);
  $('kback').addEventListener('click', back);
  $('kup').addEventListener('click', function () { move(-1); });
  $('kdn').addEventListener('click', function () { move(1); });
  $('ksel').addEventListener('click', function () { if (view === 'pin') { submitPin(); } else { activate(); } });
  document.addEventListener('contextmenu', function (e) { e.preventDefault(); });
  document.addEventListener('selectstart', function (e) { if (!e.target || e.target.tagName !== 'INPUT') { e.preventDefault(); } });
  document.addEventListener('keydown', function (e) {
    if (e.ctrlKey || e.metaKey || e.altKey) { return; }
    if (msgOpen) {
      if (e.key === 'Escape' || e.key === 'l' || e.key === 'L') { closeMsg(); }
      else if (e.key === 'ArrowUp') { moveMsg(-1); }
      else if (e.key === 'ArrowDown') { moveMsg(1); }
      else if (e.key === 'Home') { msel = 0; selMsg(); }
      else if (e.key === 'Delete') { delSel(); }
      else { return; }
      e.preventDefault(); return;
    }
    if (gmOpen) {
      if (e.key === 'Escape' || e.key === 'r' || e.key === 'R') { backGm(); e.preventDefault(); }
      return;
    }
    if (!menuOpen) {
      if (e.key === 'm' || e.key === 'M') { openMenu(); e.preventDefault(); }
      else if ((e.key === 'r' || e.key === 'R') && last && last.aprs) { openGm(); e.preventDefault(); }
      else if ((e.key === 'l' || e.key === 'L') && last && last.msg) { openMsg('msg'); e.preventDefault(); }
      else if ((e.key === 'n' || e.key === 'N') && last && last.news) { openMsg('news'); e.preventDefault(); }
      return;
    }
    if (e.key === 'Escape') { back(); }
    else if (view === 'addcall') { if (e.key === 'Enter') { activate(); } else { return; } }
    else if (view === 'pin') {
      if (e.key >= '0' && e.key <= '9' && e.key.length === 1) { padKey(e.key); }
      else if (e.key === 'Backspace') { pin = pin.slice(0, -1); drawMenu(); }
      else if (e.key === 'Enter') { submitPin(); }
      else { return; }
    }
    else if (e.key === 'ArrowUp') { move(-1); }
    else if (e.key === 'ArrowDown') { move(1); }
    else if (e.key === 'Enter' || e.key === ' ') {
      if (document.activeElement && document.activeElement.tagName === 'BUTTON' && document.activeElement.id !== 'ksel') { return; }
      activate();
    }
    else { return; }
    e.preventDefault();
  });

  /* ---- RADAR view: tiles + compass of stations with a known position ---- */
  var gmOpen = false, gmIdx = 0;
  gmIdx = parseInt(store.get('range', '0'), 10) || 0;
  var GM_RANGES = [0, 10, 25, 50, 100, 250, 500, 1000, 5000, 20000];
  if (!(gmIdx >= 0 && gmIdx < GM_RANGES.length)) { gmIdx = 0; }
  var SVGNS = 'http://www.w3.org/2000/svg';
  function geoTo(call) {
    var o = origin(), p = last && last.pos && last.pos[call];
    if (!o || !p) { return null; }
    var R = Math.PI / 180, la1 = o.lat * R, la2 = p[0] * R, dlo = (p[1] - o.lon) * R, dla = la2 - la1;
    var h = Math.sin(dla / 2) * Math.sin(dla / 2) + Math.cos(la1) * Math.cos(la2) * Math.sin(dlo / 2) * Math.sin(dlo / 2);
    var y = Math.sin(dlo) * Math.cos(la2);
    var x = Math.cos(la1) * Math.sin(la2) - Math.sin(la1) * Math.cos(la2) * Math.cos(dlo);
    return {km: 2 * 6371 * Math.asin(Math.min(1, Math.sqrt(h))), brg: (Math.atan2(y, x) / R + 360) % 360};
  }
  function unitF() { return opt.units === 'MI' ? 1.609344 : 1; }
  function fmtKm(km) {
    var v = km / unitF();
    return (v < 1 ? '<1' : (v < 10 ? v.toFixed(1) : String(Math.round(v)))) + (opt.units === 'MI' ? 'mi' : 'km');
  }
  function gmStations() {
    var seen = {}, out = [], d = last;
    if (!d) { return out; }
    function add(call, tx) {
      if (seen[call]) { return; }
      seen[call] = true;
      var g = geoTo(call);
      if (g) {
        var hot = tx || (!d.now_talking && d.last_heard.length > 0 && d.last_heard[0].src === call);
        out.push({call: call, km: g.km, brg: g.brg, tx: tx, hot: hot});
      }
    }
    if (d.now_talking) { add(d.now_talking.src, true); }
    d.last_heard.forEach(function (r) { add(r.src, false); });
    d.linked.forEach(function (r) { add(r.call, false); });
    return out;
  }
  function svgEl(name, attrs, text) {
    var e = document.createElementNS(SVGNS, name);
    Object.keys(attrs).forEach(function (k) { e.setAttribute(k, attrs[k]); });
    if (text) { e.textContent = text; }
    return e;
  }
  function drawGm() {
    var tiles = $('gmtiles'), svg = $('gmsvg');
    clear(tiles); clear(svg);
    var st = gmStations();
    $('gmmsg').textContent = '';
    if (!origin()) {
      $('gmrange').textContent = '';
      $('gmmsg').textContent = 'SET MY LOCATION TO NODE OR GPS IN MENU';
      return;
    }
    if (!st.length) {
      $('gmrange').textContent = '';
      $('gmmsg').textContent = 'NO STATION POSITIONS YET';
    }
    var f = unitF(), maxkm = 0, i;
    var kms = st.map(function (x) { return x.km; }).sort(function (a, b) { return a - b; });
    maxkm = kms.length ? kms[Math.min(5, kms.length - 1)] : 0;   /* auto range covers the nearest six */
    var R = GM_RANGES[gmIdx];
    if (gmIdx === 0) {
      R = GM_RANGES[GM_RANGES.length - 1];
      for (i = 1; i < GM_RANGES.length; i++) {
        if (GM_RANGES[i] * f >= maxkm * 1.05) { R = GM_RANGES[i]; break; }
      }
    }
    var rkm = R * f;
    $('gmrange').textContent = (gmIdx === 0 ? 'AUTO ' : '') + R + (opt.units === 'MI' ? 'mi' : 'km');

    st.slice(0, 8).forEach(function (x, n) {
      var t = document.createElement('div');
      t.className = 'tile' + (x.hot ? ' hot' : '');
      var c = document.createElement('span'); c.className = 'tc'; c.appendChild(callNode(x.call));
      var dd = document.createElement('span'); dd.className = 'td';
      var ar = document.createElement('i'); ar.className = 'ta'; ar.textContent = '↑';
      ar.style.transform = 'rotate(' + Math.round(x.brg) + 'deg)';
      var km = document.createElement('span'); km.textContent = fmtKm(x.km);
      dd.appendChild(ar); dd.appendChild(km);
      t.appendChild(c); t.appendChild(dd);
      tiles.appendChild(t);
    });

    svg.appendChild(svgEl('circle', {cx: 100, cy: 100, r: 92, 'class': 'rg'}));
    svg.appendChild(svgEl('circle', {cx: 100, cy: 100, r: 46, 'class': 'rg'}));
    svg.appendChild(svgEl('line', {x1: 100, y1: 8, x2: 100, y2: 192, 'class': 'rx'}));
    svg.appendChild(svgEl('line', {x1: 8, y1: 100, x2: 192, y2: 100, 'class': 'rx'}));
    svg.appendChild(svgEl('text', {x: 100, y: 7, 'class': 'rl n'}, 'N'));
    svg.appendChild(svgEl('text', {x: 100, y: 199, 'class': 'rl'}, 'S'));
    svg.appendChild(svgEl('text', {x: 196, y: 103, 'class': 'rl'}, 'E'));
    svg.appendChild(svgEl('text', {x: 4, y: 103, 'class': 'rl'}, 'W'));
    var out = 0, placed = [];
    st.forEach(function (x, n) {
      if (x.km > rkm) { out++; return; }
      var r = Math.max(14, 92 * x.km / rkm), a = x.brg * Math.PI / 180;
      var px = 100 + r * Math.sin(a), py = 100 - r * Math.cos(a);
      svg.appendChild(svgEl('circle', {cx: px.toFixed(1), cy: py.toFixed(1), r: 8, 'class': 'rb' + (x.hot ? ' hot' : '')}));
      svg.appendChild(svgEl('polygon', {points: '0,-6 3.6,5 0,2.4 -3.6,5', 'class': 'ra',
        transform: 'translate(' + px.toFixed(1) + ',' + py.toFixed(1) + ') rotate(' + Math.round(x.brg) + ')'}));
      var clash = placed.some(function (q) { return Math.abs(q[0] - px) < 30 && Math.abs(q[1] - py) < 9; });
      if (n < 10 && !clash) {
        placed.push([px, py]);
        var right = px < 100;
        svg.appendChild(svgEl('text', {x: (px + (right ? -11 : 11)).toFixed(1), y: (py + 2.5).toFixed(1), 'class': 'rt',
          'text-anchor': right ? 'end' : 'start'}, x.call));
      }
    });
    svg.appendChild(svgEl('polygon', {points: '0,-8 6,7 0,3 -6,7', 'class': 'rme', transform: 'translate(100,100)'}));
    if (out) { $('gmmsg').textContent = '+' + out + ' BEYOND RANGE'; }
  }
  var gmFromMenu = 0;
  function openGm(fromMenu) { gmFromMenu = fromMenu === true ? sel + 1 : 0; closeMenu(); gmOpen = true; $('gm').className = 'menu'; drawGm(); $('grange').focus(); }
  function closeGm() { if (!gmOpen) { return; } gmOpen = false; $('gm').className = 'menu hidden'; $('gmbtn').focus(); }
  function backGm() {
    var s0 = gmFromMenu - 1; if (!gmOpen) { return; }
    closeGm();
    if (s0 >= 0) { gmFromMenu = 0; openMenu(); sel = s0; drawMenu(); }
  }
  $('gmbtn').addEventListener('click', function () { openGm(); });
  $('gback').addEventListener('click', backGm);
  $('grange').addEventListener('click', function () { gmIdx = (gmIdx + 1) % GM_RANGES.length; store.set('range', String(gmIdx)); drawGm(); });

  var nodeShown = null;
  function render(d) {
    if (d.fade) { FADE = d.fade; }
    if (d.tot) { FULL = d.tot; }
    last = d; admin = d.admin || null;
    var nt = d.now_talking, lh = d.last_heard.length ? d.last_heard[0] : null;
    var call = nt ? nt.src : (lh ? lh.src : '------');
    if (call !== lastCall) {
      var c = $('call');
      clear(c);
      c.appendChild(callNode(call));
      lastCall = call;
    }
    sizeCls = call.length > 8 ? ' small' : (call.length > 6 ? ' mid' : '');
    if (!nt && live) { idleSince = Date.now(); }
    live = !!nt;
    paintCall();
    $('lamp').className = 'lamp' + (nt ? ' on' : '');
    if (nt) {
      var di = destInfo(nt.dest); talkId = di.id;
      var key = nt.src + '|' + nt.dest + '|' + nt.gw;
      if (talkStart === null || key !== curKey) {
        talkStart = Date.now() - nt.elapsed * 1000;
        curKey = key;
      }
      $('rlab').textContent = 'TO';
      $('dest').textContent = di.to + '   VIA ' + nt.gw;
    } else {
      talkStart = null; talkId = '';
      curKey = null;
      lastDur = lh ? lh.dur : 0;
      $('rlab').textContent = lh ? 'LAST' : '';
      $('dest').textContent = lh ? (lh.time + '   TO ' + destInfo(lh.dest).to + '   VIA ' + lh.gw)
                                 : 'WAITING FOR TRAFFIC';
    }
    $('rid').textContent = nt && talkId ? 'ID ' + talkId : '';
    $('rdist').textContent = distText(call, true);
    $('timer').className = 'timer' + (nt ? '' : ' idle');
    if (!nt) { $('timer').textContent = mmss(lastDur); setMeter(lastDur); }

    $('rname').textContent = d.name;
    if (d.node !== nodeShown) {
      var n = $('node'); clear(n);
      if (d.node) { n.appendChild(callNode(d.node)); }
      nodeShown = d.node;
    }
    var loc = $('loc');
    if (d.loc) {
      loc.href = 'https://www.openstreetmap.org/?mlat=' + d.loc.lat + '&mlon=' + d.loc.lon +
                 '#map=12/' + d.loc.lat + '/' + d.loc.lon;
      $('locv').textContent = d.loc.grid;
      loc.className = 'loc';
    } else {
      loc.className = 'loc hidden';
    }
    $('link').className = 'badge' + (d.stale ? ' off' : ' blue');
    var dm = $('dmr'), ds = d.dmr;
    if (ds === null || ds === undefined) { dm.className = 'badge off hidden'; }
    else {
      var pend = dmrPending && dmrPending.until > Date.now() && ds !== dmrPending.want;
      if (!pend) { dmrPending = null; }
      if (pend || ds === 'activating' || ds === 'deactivating') { dm.className = 'badge dmr wait'; dm.textContent = 'DMR ...'; }
      else if (ds === 'active') { dm.className = 'badge dmr'; dm.textContent = 'DMR ON'; }
      else { dm.className = 'badge off'; dm.textContent = ds === 'failed' ? 'DMR FAIL' : 'DMR OFF'; }
    }
    $('link').textContent = d.stale ? 'NO LOG' : 'LIVE';
    $('nlinked').textContent = d.linked.length + ' LINKED';

    var showD = !!origin() && !!d.aprs;
    rows('linked', d.linked.map(function (r) {
      return showD ? [{call: r.call}, distText(r.call, false), fmt(r.connected_for)]
                   : [{call: r.call}, fmt(r.connected_for)];
    }), showD ? ['a', 'd', 'b'] : ['a', 'b']);
    rows('heard', d.last_heard.map(function (r) {
      var row = [showD ? r.time.slice(0, 5) : r.time, {call: r.src}, Math.round(r.dur) + 's'];
      if (showD) { row.push(distText(r.src, false)); }
      return row;
    }), ['b', 'a', 'c', 'd']);
    arrows('linked', 'lkar');
    arrows('heard', 'hdar');

    var s = d.system || {};
    gauge('cpu', s.cpu, 100, '%', 85);
    gauge('tmp', s.temp, 85, '°C', 70);
    gauge('mem', s.mem, 100, '%', 90);
    $('loadv').textContent = (s.load === null || s.load === undefined) ? '--' : s.load.toFixed(2);
    $('upv').textContent = fmt(s.uptime);
    if (menuOpen && view !== 'pin' && view !== 'addcall') { drawMenu(); }
    mailUpdate(d);
    picUpdate(d);
    newsUpdate(d);
    $('gmbtn').className = 'menubtn gmk' + (d.aprs ? '' : ' hidden');
    if (gmOpen) { drawGm(); }
  }
  function tick() {
    if (talkStart !== null) {
      var sec = (Date.now() - talkStart) / 1000;
      $('timer').textContent = mmss(sec);
      setMeter(sec);
    }
    paintCall();
    var t = new Date();
    $('clock').textContent = pad(t.getHours()) + ':' + pad(t.getMinutes()) + ':' + pad(t.getSeconds());
  }
  function poll() {
    var x = new XMLHttpRequest();
    if (opt.loc === 'GPS' && gps && Date.now() - gpsAt > 300000) { gpsAt = Date.now(); requestGps(); }
    x.open('GET', 'api/status', true);
    x.onload = function () {
      if (x.status === 200) { try { render(JSON.parse(x.responseText)); } catch (e) {} }
    };
    x.send();
  }
  poll(); tick();
  setInterval(poll, 2000);
  setInterval(tick, 200);
})();
"""

INDEX = """<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>{title}</title><link rel="stylesheet" href="style.css"></head>
<body>
<div class="screen">
<div class="top"><span class="badge">YSF</span><span id="link" class="badge off">NO LOG</span><span id="dmr" class="badge off hidden">DMR</span>
<button id="mail" class="badge mailb hidden" type="button" title="New messages" aria-label="New messages"><svg class="env" viewBox="0 0 20 14" aria-hidden="true"><rect x="1" y="1" width="18" height="12" rx="1.5" fill="none" stroke="currentColor" stroke-width="1.8"/><polyline points="1.5,2 10,8 18.5,2" fill="none" stroke="currentColor" stroke-width="1.8"/></svg><span id="mailn"></span></button>
<button id="picb" class="badge mailb hidden" type="button" title="New pictures" aria-label="New pictures"><svg class="env" viewBox="0 0 20 14" aria-hidden="true"><rect x="1" y="1" width="18" height="12" rx="1.5" fill="none" stroke="currentColor" stroke-width="1.8"/><polyline points="3,11 8,6 11,9 14,5.5 17,11" fill="none" stroke="currentColor" stroke-width="1.6"/><circle cx="14.5" cy="4" r="1.2" fill="currentColor"/></svg><span id="picn"></span></button>
<span id="node"></span>
<a id="loc" class="loc hidden" href="#" target="_blank" rel="noopener noreferrer" title="Node location">
<svg class="pin" viewBox="0 0 12 16" aria-hidden="true"><path fill="currentColor" d="M6 0C2.7 0 0 2.6 0 5.8 0 10 6 16 6 16s6-6 6-10.2C12 2.6 9.3 0 6 0zm0 8.2a2.4 2.4 0 1 1 0-4.8 2.4 2.4 0 0 1 0 4.8z"/></svg>
<span id="locv"></span></a>
<span class="sp"></span><span id="nlinked"></span><span id="clock"></span>
<button id="menubtn" class="menubtn" type="button">MENU</button></div>
<div class="vfo"><div class="band">A</div><div id="call" class="call old">------</div>
<div class="side"><div id="lamp" class="lamp">BUSY</div><div id="timer" class="timer idle">00:00</div><div id="rid" class="rid"></div></div></div>
<div class="route"><b id="rlab">LAST</b><span id="dest"></span><span id="rdist" class="rdist"></span><button id="gmbtn" class="menubtn gmk hidden" type="button">RADAR</button></div>
<div class="refl"><span id="rname"></span><span class="n">DIGITAL VOICE</span></div>
<div id="meter" class="meter"></div>
<div class="panels">
<div class="panel"><h2><span>LINKED</span><span id="lkar" class="ar"></span></h2><div id="linked" class="rows"></div></div>
<div class="panel"><h2><span>HEARD</span><span id="hdar" class="ar"></span></h2><div id="heard" class="rows"></div></div>
<div class="panel"><h2><span>SYSTEM</span></h2>
<div class="g"><span class="k">CPU</span><div class="bar"><i id="cpub"></i></div><span id="cpuv" class="v">--</span></div>
<div class="g"><span class="k">TMP</span><div class="bar"><i id="tmpb"></i></div><span id="tmpv" class="v">--</span></div>
<div class="g"><span class="k">MEM</span><div class="bar"><i id="memb"></i></div><span id="memv" class="v">--</span></div>
<div class="g"><span class="k">LOAD</span><span></span><span id="loadv" class="v">--</span></div>
<div class="g"><span class="k">UP</span><span></span><span id="upv" class="v">--</span></div>
</div></div>
<div id="menu" class="menu hidden"><div class="mh"><span id="mtitle">MENU</span><span id="mnode"></span></div>
<div id="mbody" class="mb"></div><div id="mmsg" class="mmsg"></div>
<div class="mf"><button id="kback" class="key" type="button">BACK</button><button id="kup" class="key" type="button">&#9650;</button><button id="kdn" class="key" type="button">&#9660;</button><button id="ksel" class="key red" type="button">SELECT</button></div></div>
<div id="mg" class="menu hidden"><div class="mh"><span id="mgt">LOG MSG</span><span id="mgpos"></span></div>
<div id="mgb" class="mb mgl"></div>
<div class="mf"><button id="mback" class="key" type="button">BACK</button><button id="mdel" class="key hidden" type="button">DEL</button><button id="mtop" class="key" type="button">TOP</button><button id="mup" class="key" type="button">&#9650;</button><button id="mdn" class="key red" type="button">&#9660;</button></div></div>
<div id="gm" class="menu hidden"><div class="mh"><span>RADAR</span><span id="gmrange"></span></div>
<div class="gmb"><div id="gmtiles" class="gmt"></div><svg id="gmsvg" class="gmsvg" viewBox="0 0 200 200" aria-label="Radar view"></svg></div>
<div id="gmmsg" class="mmsg"></div>
<div class="mf"><button id="gback" class="key" type="button">BACK</button><button id="grange" class="key red" type="button">RANGE</button></div></div>
</div>
<script src="app.js"></script></body></html>
"""

CSP = ("default-src 'none'; script-src 'self'; style-src 'self'; "
       "img-src 'self'; connect-src 'self'; base-uri 'none'; frame-ancestors 'none'")


def make_handler(state, stats, args, loc, tls=None, menu=None):
    index = INDEX.format(title=html.escape(args.name)).encode("utf-8")

    class Handler(BaseHTTPRequestHandler):
        server_version = "ysfradio"
        sys_version = ""
        timeout = 15

        def setup(self):
            if tls is not None:
                self.request.settimeout(15)
                self.request = tls.get().wrap_socket(self.request, server_side=True)
            BaseHTTPRequestHandler.setup(self)

        def _send(self, code, ctype, body, head_only=False, extra=None):
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("Content-Security-Policy", CSP)
            self.send_header("Referrer-Policy", "no-referrer")
            for k, v in (extra or ()):
                self.send_header(k, v)
            self.end_headers()
            if not head_only:
                self.wfile.write(body)

        def _route(self, head_only):
            path = self.path.split("?", 1)[0]
            base = args.base
            if base:
                if path == base:
                    self.send_response(301)
                    self.send_header("Location", base + "/")
                    self.send_header("Content-Length", "0")
                    self.end_headers()
                    return
                if path.startswith(base + "/"):
                    path = path[len(base):]
            if path in ("/", "/index.html"):
                self._send(200, "text/html; charset=utf-8", index, head_only)
            elif path == "/style.css":
                self._send(200, "text/css; charset=utf-8",
                           CSS.encode("utf-8"), head_only)
            elif path == "/app.js":
                self._send(200, "application/javascript; charset=utf-8",
                           JS.encode("utf-8"), head_only)
            elif path == "/api/status":
                d = state.snapshot(False, args.name)
                d["system"] = stats.snapshot()
                d["node"] = args.node
                d["fade"] = args.fade
                d["tot"] = args.tot
                d["loc"] = loc
                self._menu_status(d)
                d["msg"] = self._msg_status("msgreader")
                d["news"] = self._msg_status("newsreader")
                d["pics"] = self._msg_status("picreader")
                body = json.dumps(d).encode("utf-8")
                self._send(200, "application/json", body, head_only)
            elif path in ("/api/messages", "/api/news"):
                rd = getattr(args, "msgreader" if path == "/api/messages" else "newsreader", None)
                if rd is None:
                    self._send(404, "text/plain", b"not found\n", head_only)
                else:
                    self._send(200, "application/json",
                               json.dumps({"msgs": rd.get()}).encode("utf-8"), head_only)
            elif path == "/api/pictures":
                rd = getattr(args, "picreader", None)
                if rd is None:
                    self._send(404, "text/plain", b"not found\n", head_only)
                else:
                    self._send(200, "application/json",
                               json.dumps({"pics": rd.get()}).encode("utf-8"), head_only)
            elif re.match(r"^/pic/[0-9]{1,9}\.jpg$", path):
                rd = getattr(args, "picreader", None)
                data = rd.read(int(path[5:-4])) if rd is not None else None
                if data is None:
                    self._send(404, "text/plain", b"not found\n", head_only)
                else:
                    self._send(200, "image/jpeg", data, head_only)
            elif path == "/healthz":
                self._send(200, "text/plain", b"ok\n", head_only)
            else:
                self._send(404, "text/plain", b"not found\n", head_only)

        def _token(self):
            c = self.headers.get("Cookie")
            if not c:
                return None
            try:
                sc = SimpleCookie()
                sc.load(c)
                m = sc.get("ysfsid")
                return m.value if m else None
            except Exception:
                return None

        def _msg_status(self, attr):
            rd = getattr(args, attr, None)
            if rd is None:
                return None
            m = rd.get()
            return {"ids": [x["id"] for x in m], "latest": m[0]["id"] if m else 0}

        def _menu_status(self, d):
            d["auth"] = bool(menu and menu.auth and menu.auth.enabled)
            d["admin"] = None
            d["dmr"] = None
            d["aprs"] = None
            d["pos"] = {}
            if not menu:
                return
            if menu.auth:
                d["admin"] = menu.auth.check(self._token())
            if menu.bridge:
                d["dmr"] = menu.bridge.status()
            if menu.aprs:
                d["aprs"] = menu.aprs.status()
                calls = [r["src"] for r in d["last_heard"]] + [r["call"] for r in d["linked"]]
                if d["now_talking"]:
                    calls.append(d["now_talking"]["src"])
                d["pos"] = menu.aprs.lookup(calls)
                pr = getattr(args, "posreader", None)
                if pr:
                    rp = pr.get()
                    for c in calls:
                        b = c.upper().split("/")[0].split("-")[0]
                        v = rp.get(b)
                        a = menu.aprs.pos.get(b)
                        if v and (a is None or v[2] >= a[2]):
                            d["pos"][c] = [round(v[0], 2), round(v[1], 2)]

        def _json(self, code, obj, extra=None):
            self._send(code, "application/json", json.dumps(obj).encode("utf-8"), False, extra)

        def _cookie(self, value, max_age):
            path = (args.base or "") + "/"
            c = "ysfsid=%s; Path=%s; HttpOnly; SameSite=Strict; Max-Age=%d" % (value, path, max_age)
            if tls is not None:
                c += "; Secure"
            return [("Set-Cookie", c)]

        def do_POST(self):
            path = self.path.split("?", 1)[0]
            base = args.base
            if base and path.startswith(base + "/"):
                path = path[len(base):]
            if menu is None or path not in ("/api/login", "/api/logout", "/api/bridge", "/api/delete", "/api/picallow"):
                return self._json(404, {"error": "not found"})
            if self.headers.get("X-Requested-With") != "ysfradio":
                return self._json(403, {"error": "forbidden"})
            origin = self.headers.get("Origin")
            if origin and urlparse(origin).netloc != self.headers.get("Host", ""):
                return self._json(403, {"error": "forbidden"})
            if not (self.headers.get("Content-Type") or "").startswith("application/json"):
                return self._json(415, {"error": "json only"})
            try:
                n = int(self.headers.get("Content-Length", ""))
            except ValueError:
                return self._json(411, {"error": "length required"})
            if not 0 <= n <= 512:
                return self._json(413, {"error": "too large"})
            try:
                obj = json.loads(self.rfile.read(n).decode("utf-8") or "{}")
            except ValueError:
                return self._json(400, {"error": "bad json"})
            if not isinstance(obj, dict):
                return self._json(400, {"error": "bad json"})
            auth = menu.auth
            if auth is None or not auth.enabled:
                return self._json(404, {"error": "admin disabled"})
            if path == "/api/login":
                pin = obj.get("pin")
                if not isinstance(pin, str) or not re.match(r"^[0-9]{4,12}$", pin):
                    return self._json(403, {"error": "bad"})
                tok, csrf, err = auth.login(self.client_address[0], pin)
                if err == "locked":
                    return self._json(429, {"error": "locked"})
                if err:
                    return self._json(403, {"error": "bad"})
                return self._json(200, {"ok": True, "csrf": csrf},
                                  self._cookie(tok, auth.SESSION_MAX))
            token = self._token()
            if auth.check(token, self.headers.get("X-CSRF", "")) is None:
                return self._json(401, {"error": "login required"})
            if path == "/api/logout":
                auth.logout(token)
                return self._json(200, {"ok": True}, self._cookie("", 0))
            if path == "/api/delete":
                store = {"msg": getattr(args, "msgwriter", None),
                         "news": getattr(args, "newswriter", None),
                         "pic": getattr(args, "picreader", None)}.get(obj.get("kind"))
                mid = obj.get("id")
                if store is None:
                    return self._json(404, {"error": "not enabled"})
                if not isinstance(mid, int) or isinstance(mid, bool):
                    return self._json(400, {"error": "bad id"})
                try:
                    found = store.delete(mid)
                except OSError:
                    return self._json(500, {"error": "cannot write"})
                return self._json(200 if found else 404, {"ok": found})
            if path == "/api/picallow":
                al = getattr(args, "picallow", None)
                if al is None:
                    return self._json(404, {"error": "not enabled"})
                op, call = obj.get("op"), obj.get("call", "")
                rid = obj.get("id", "")
                try:
                    if op == "list":
                        return self._json(200, {"allow": al.list()})
                    if op not in ("add", "remove") or not isinstance(call, str) or not isinstance(rid, str):
                        return self._json(400, {"error": "bad request"})
                    call = call.upper()
                    if not ysfmsgstore.CALL_RE.match(call) or not (rid == "" or ysfpic.RID_RE.match(rid)):
                        return self._json(400, {"error": "bad callsign or radio ID"})
                    ok = al.add(call, rid) if op == "add" else al.remove(call, rid)
                except OSError:
                    return self._json(500, {"error": "cannot write"})
                return self._json(200 if ok else 404, {"ok": ok, "allow": al.list()})
            if menu.bridge is None:
                return self._json(404, {"error": "bridge not configured"})
            action = obj.get("action")
            if action not in ("start", "stop"):
                return self._json(400, {"error": "bad action"})
            with menu.lock:
                now = time.time()
                if now - menu.last_req < 3:
                    return self._json(429, {"error": "too fast"})
                menu.last_req = now
            try:
                menu.bridge.request(action)
            except OSError:
                return self._json(500, {"error": "cannot write request"})
            return self._json(200, {"ok": True})

        def do_GET(self):
            self._route(False)

        def do_HEAD(self):
            self._route(True)

        def log_message(self, fmt, *a):
            pass

    return Handler


def resolve_location(args):
    """Return {'grid','lat','lon'} (lat/lon = centre of the 6-char square) or None."""
    if args.lat is not None and args.lon is not None:
        if not (-90 <= args.lat <= 90 and -180 <= args.lon <= 180):
            raise SystemExit("--lat/--lon out of range")
        grid = latlon_to_grid(args.lat, args.lon)
    elif args.grid:
        grid = args.grid.upper()
    else:
        return None
    centre = grid_to_latlon(grid)
    if centre is None:
        raise SystemExit("--grid must be a Maidenhead locator such as IO91 or IO91WM")
    return {"grid": grid, "lat": centre[0], "lon": centre[1]}


class Menu(object):
    def __init__(self, auth, bridge, aprs):
        self.auth = auth
        self.bridge = bridge
        self.aprs = aprs
        self.lock = threading.Lock()
        self.last_req = 0


def feed_loop(state, aprs, stop):
    stop.wait(3)
    while not stop.is_set():
        d = state.snapshot(False, "")
        calls = [d["now_talking"]["src"]] if d["now_talking"] else []
        calls += [r["src"] for r in d["last_heard"]] + [r["call"] for r in d["linked"]]
        aprs.note_heard(calls)
        stop.wait(10)


def make_pin_cli(state_dir):
    import getpass
    path = os.path.join(state_dir, "pin")
    a = getpass.getpass("New admin PIN (6-12 digits): ")
    b = getpass.getpass("Repeat PIN: ")
    if a != b or not re.match(r"^[0-9]{6,12}$", a):
        raise SystemExit("PINs differ or are not 6-12 digits")
    ysfmenu.write_pin_file(path, a)
    print("PIN written to %s" % path)


def main():
    p = argparse.ArgumentParser(description="YSFReflector transceiver-style display")
    p.add_argument("--log-dir", default="/var/log/YSFReflector")
    p.add_argument("--prefix", default="YSFReflector")
    p.add_argument("--host", default="127.0.0.1")
    p.add_argument("--port", type=int, default=8080)
    p.add_argument("--name", default="CCARC YSF Reflector",
                   help="reflector name shown on the blue banner")
    p.add_argument("--node", default="",
                   help="node/club callsign shown in the top bar")
    p.add_argument("--base", default="",
                   help="serve under a URL path prefix, e.g. /dash (also works behind a proxy that strips it)")
    p.add_argument("--fade", type=int, default=20,
                   help="seconds after a transmission before the callsign darkens (default 20)")
    p.add_argument("--tot", type=int, default=60,
                   help="seconds the talk meter takes to fill, set to your reflector's time-out (default 60)")
    p.add_argument("--grid", default="",
                   help="Maidenhead locator shown with a map pin, e.g. IO91 or IO91WM")
    p.add_argument("--lat", type=float, help="latitude (alternative to --grid)")
    p.add_argument("--lon", type=float, help="longitude (alternative to --grid)")
    p.add_argument("--cert", default="", help="TLS certificate (PEM, full chain) to serve HTTPS")
    p.add_argument("--key", default="", help="TLS private key (PEM)")
    p.add_argument("--redirect-port", type=int, default=0,
                   help="also listen on this plain-HTTP port (e.g. 80) and redirect to HTTPS")
    p.add_argument("--public-host", default="",
                   help="host name used in redirects (default: the request's Host header)")
    p.add_argument("--state-dir", default="/var/lib/ysfradio",
                   help="writable directory for the PIN file, bridge request and APRS cache")
    p.add_argument("--bridge-unit", default="",
                   help="systemd unit the MENU may start/stop, e.g. mmdvm_bridge_ysf.service")
    p.add_argument("--pin-file", default="", help="admin PIN file (default: <state-dir>/pin)")
    p.add_argument("--make-pin", action="store_true", help="set the admin PIN, then exit")
    p.add_argument("--aprs", action="store_true",
                   help="look up positions of heard stations on APRS-IS (read-only) for distance display")
    p.add_argument("--aprs-server", default="rotate.aprs2.net:14580")
    p.add_argument("--aprs-max", type=int, default=40,
                   help="how many recently heard callsigns to watch on APRS-IS (default 40)")
    p.add_argument("--messages", action="store_true",
                   help="show broadcast text messages decoded by ysfmsgd.py (MENU > LOG > MESSAGE)")
    p.add_argument("--messages-file", default="",
                   help="messages file written by ysfmsgd.py (default: <state-dir>/messages.json)")
    p.add_argument("--news", action="store_true",
                   help="show News Station uploads kept by ysfmsgd.py (MENU > LOG > NEWS)")
    p.add_argument("--news-file", default="",
                   help="news board file written by ysfmsgd.py (default: <state-dir>/newsboard.json)")
    p.add_argument("--pictures", action="store_true",
                   help="show pictures kept by ysfmsgd.py and manage the sender whitelist (MENU > LOG > PICTURES)")
    p.add_argument("--pictures-dir", default="",
                   help="picture folder written by ysfmsgd.py (default: <state-dir>/pictures)")
    p.add_argument("--pictures-allow", default="",
                   help="whitelist file (default: <state-dir>/picture-allow.json)")
    p.add_argument("--positions", action="store_true",
                   help="use positions the radios transmit, decoded by ysfmsgd.py, as well as APRS-IS")
    p.add_argument("--positions-file", default="",
                   help="positions file written by ysfmsgd.py (default: <state-dir>/positions.json)")
    args = p.parse_args()
    if args.make_pin:
        make_pin_cli(args.state_dir)
        return
    args.msgreader = None
    args.msgwriter = args.newsreader = args.newswriter = None
    if args.messages or args.messages_file:
        mpath = args.messages_file or os.path.join(args.state_dir, "messages.json")
        args.msgreader = ysfmsgstore.MsgReader(mpath)
        args.msgwriter = ysfmsgstore.MsgFile(mpath)
    if args.news or args.news_file:
        npath = args.news_file or os.path.join(args.state_dir, "newsboard.json")
        args.newsreader = ysfmsgstore.MsgReader(npath)
        args.newswriter = ysfmsgstore.MsgFile(npath)
    args.picreader = args.picallow = None
    if args.pictures or args.pictures_dir:
        args.picreader = ysfpic.PicFile(args.pictures_dir or os.path.join(args.state_dir, "pictures"))
        args.picallow = ysfpic.Allow(args.pictures_allow or os.path.join(args.state_dir, "picture-allow.json"))
    args.posreader = None
    if args.positions or args.positions_file:
        args.posreader = ysfmsgstore.PosReader(
            args.positions_file or os.path.join(args.state_dir, "positions.json"))
    args.name = args.name[:40]
    args.node = re.sub(r"[^A-Za-z0-9/]", "", args.node)[:12].upper()
    args.fade = max(1, min(600, args.fade))
    args.tot = max(10, min(600, args.tot))
    args.base = args.base.rstrip("/")
    if args.base and not re.match(r"^/[A-Za-z0-9_-]+$", args.base):
        raise SystemExit("--base must look like /dash")
    loc = resolve_location(args)
    tls = None
    if args.cert or args.key:
        if not (args.cert and args.key):
            raise SystemExit("--cert and --key must be given together")
        try:
            tls = TLSContext(args.cert, args.key)
        except (OSError, ssl.SSLError) as e:
            raise SystemExit("cannot load certificate/key: %s" % e)
    if args.redirect_port and tls is None:
        raise SystemExit("--redirect-port needs --cert and --key")
    if args.public_host and not re.match(r"^[A-Za-z0-9]([A-Za-z0-9.-]{0,251}[A-Za-z0-9])?$", args.public_host):
        raise SystemExit("--public-host must be a plain host name")

    state = RadioState()
    stats = SysStats()
    stop = threading.Event()

    pin_file = args.pin_file or os.path.join(args.state_dir, "pin")
    auth = ysfmenu.Auth(pin_file)
    loopback = args.host in ("127.0.0.1", "localhost", "::1")
    if auth.enabled and tls is None and not loopback:
        sys.stderr.write("ysfradio: admin PIN disabled because HTTPS is not enabled "
                         "(the PIN would cross the network in clear)\n")
        auth.record = None
    bridge = None
    if args.bridge_unit:
        bridge = ysfmenu.BridgeControl(args.bridge_unit,
                                       os.path.join(args.state_dir, "bridge.request"))
    aprs = None
    if args.aprs:
        aprs = ysfmenu.AprsTracker(args.aprs_server, args.node,
                                   os.path.join(args.state_dir, "aprs.json"),
                                   max(1, min(200, args.aprs_max)))
        for target, targs in ((aprs.run, (stop,)), (feed_loop, (state, aprs, stop))):
            t = threading.Thread(target=target, args=targs)
            t.daemon = True
            t.start()
    menu = Menu(auth, bridge, aprs)
    for target, targs in ((tail_loop, (state, args.log_dir, args.prefix, stop)),
                          (sys_loop, (stats, stop))):
        t = threading.Thread(target=target, args=targs)
        t.daemon = True
        t.start()

    srv = QuietServer((args.host, args.port), make_handler(state, stats, args, loc, tls, menu))
    if args.redirect_port:
        rsrv = QuietServer((args.host, args.redirect_port), make_redirect_handler(args))
        rt = threading.Thread(target=rsrv.serve_forever)
        rt.daemon = True
        rt.start()
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        stop.set()
        if aprs:
            aprs.save()
        srv.server_close()


if __name__ == "__main__":
    main()
