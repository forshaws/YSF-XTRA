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
import ysfhist
import ysfmon
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
html,body{margin:0;height:100%;background:#070707;overflow:hidden}
html{-webkit-text-size-adjust:100%;text-size-adjust:100%}
body{display:flex;
font-family:"DejaVu Sans Mono",Menlo,Consolas,"Courier New",monospace;color:var(--white)}
a{color:inherit;text-decoration:none}
a:hover{text-decoration:underline}
.hidden{display:none!important}
.screen{--u:min(0.98vw,calc(var(--vh,1vh)*1.63));margin:auto;position:relative;width:calc(var(--u)*100);
height:calc(var(--u)*60);background:var(--ink);border:calc(var(--u)*.7) solid #1b1b1d;
border-radius:calc(var(--u)*1.6);overflow:hidden;display:grid;
grid-template-rows:minmax(0,7fr) minmax(0,22fr) minmax(0,9fr) minmax(0,6fr) minmax(0,7fr) minmax(0,32fr) minmax(0,7fr);
box-shadow:0 0 calc(var(--u)*4) rgba(47,123,255,.18),inset 0 0 calc(var(--u)*6) rgba(0,0,0,.9)}
.screen::after{content:"";position:absolute;left:0;top:0;right:0;bottom:0;pointer-events:none;
background:repeating-linear-gradient(0deg,rgba(255,255,255,.035) 0,rgba(255,255,255,.035) 1px,transparent 1px,transparent 3px)}
.top{display:flex;align-items:center;gap:calc(var(--u)*1.4);padding:0 calc(var(--u)*1.6);
background:var(--red);color:#fff;font-weight:700;font-size:calc(var(--u)*2.3);white-space:nowrap;overflow:hidden}
.top .sp{flex:1}
.badge{background:#fff;color:var(--red);padding:0 calc(var(--u)*1.2);border-radius:calc(var(--u)*.4)}
.badge.blue{background:var(--blue);color:#fff}
.badge.off{background:#222;color:#888}
.badge.mon{display:inline-flex;align-items:center;gap:calc(var(--u)*.6)}
.spk{width:calc(var(--u)*2.8);height:calc(var(--u)*2)}
.loc{display:inline-flex;align-items:center;gap:calc(var(--u)*.6)}
.pin{width:calc(var(--u)*1.7);height:calc(var(--u)*2.3)}
.vfo{display:grid;grid-template-columns:8fr 70fr 22fr;align-items:center;
padding:0 calc(var(--u)*1.6);border-bottom:1px solid #1d1d20}
.band{font-size:calc(var(--u)*6);font-weight:700;color:var(--blue)}
.call{font-weight:700;letter-spacing:.02em;white-space:nowrap;line-height:1;
font-size:calc(var(--u)*14);color:var(--dim);transition:color 2s ease}
.call.live{color:var(--white);text-shadow:0 0 calc(var(--u)*1.2) rgba(255,255,255,.35);transition:none}
.call.old{color:var(--dark)}
.rid{font-size:calc(var(--u)*2.4);font-weight:700;color:var(--blue);min-height:calc(var(--u)*2.6);line-height:1.1;white-space:nowrap}
.call.mid{font-size:calc(var(--u)*11)}
.call.small{font-size:calc(var(--u)*7.5)}
.side{text-align:right;align-self:center}
.lamp{display:inline-block;font-weight:700;font-size:calc(var(--u)*2.6);padding:0 calc(var(--u)*1.2);
border:1px solid #333;color:#444;border-radius:calc(var(--u)*.4)}
.lamp.on{background:var(--red);border-color:var(--red);color:#fff;
box-shadow:0 0 calc(var(--u)*1.6) var(--red)}
.timer{font-size:calc(var(--u)*5.4);font-weight:700;line-height:1.15;margin-top:calc(var(--u)*.4);color:var(--white)}
.timer.idle{color:var(--dim)}
.timer.warn{color:#ffd35a}
.timer.hot{color:#ff7a3c}
.timer.over{color:var(--red);animation:mp 1s steps(2) infinite}
.route{display:flex;align-items:center;padding:0 calc(var(--u)*1.6);
font-size:calc(var(--u)*3.2);color:var(--white);white-space:nowrap;overflow:hidden}
.route b{color:var(--blue);margin-right:calc(var(--u)*1)}
#dest{flex:0 1 auto;min-width:0;overflow:hidden;text-overflow:ellipsis}
.route .rdist,.route .gmk{flex:0 0 auto}
.refl{display:flex;align-items:center;justify-content:space-between;background:var(--blue);
color:#fff;padding:0 calc(var(--u)*1.6);font-weight:700;font-size:calc(var(--u)*3.6);
letter-spacing:.06em;text-transform:uppercase;white-space:nowrap;overflow:hidden}
.refl span.n{font-size:calc(var(--u)*2.6);letter-spacing:0}
.refl span.n.act{background:#ffd35a;color:#2a1c00;padding:0 calc(var(--u)*1.2);border-radius:calc(var(--u)*.4);animation:mp 1.6s steps(2) infinite}
.panels{display:block;min-height:0;overflow:hidden}
.panel.off{display:none}
.scw{flex:1 1 0;min-height:0;position:relative}
#hactw{max-width:calc(var(--u)*70)}
.hidden{display:none}
.scw canvas{position:absolute;left:0;top:0;width:100%;height:100%;display:block}
.panel{height:100%;display:flex;flex-direction:column;padding:calc(var(--u)*1) calc(var(--u)*1.6) 0;
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
.r.sub{opacity:.7}
.r .tg{cursor:pointer;color:var(--blue)}
.g{display:grid;grid-template-columns:calc(var(--u)*7) 1fr calc(var(--u)*9);align-items:center;
gap:calc(var(--u)*1);font-size:calc(var(--u)*2.7);line-height:1.55}
.g .k{color:var(--blue);font-weight:700}
.g .v{text-align:right;color:var(--white)}
.panel.sysp .g{max-width:calc(var(--u)*60)}
.panel .rows{max-width:calc(var(--u)*70)}
.bar{height:calc(var(--u)*1.5);background:#16181d}
.bar i{display:block;height:100%;width:0;background:var(--white)}
.bar i.hot{background:var(--red)}
.g .v.hot{color:var(--red)}
.r .d{color:var(--red);flex:0 0 calc(var(--u)*8.5);text-align:right}
.rows[data-k="adb"] .r{display:grid;column-gap:calc(var(--u)*.6);grid-template-columns:minmax(0,1fr) calc(var(--u)*10.5) calc(var(--u)*9.7)}
.rows[data-k="bac"] .r{display:grid;column-gap:calc(var(--u)*.6);grid-template-columns:calc(var(--u)*13.5) minmax(0,1fr) calc(var(--u)*6.5)}
.rows[data-k="bacd"] .r{display:grid;column-gap:calc(var(--u)*.6);grid-template-columns:calc(var(--u)*8.5) minmax(0,1fr) calc(var(--u)*6.5) calc(var(--u)*10.5)}
.rows[data-k="adb"] .b,.rows[data-k] .c,.rows[data-k] .d{text-align:right}
.top>*{flex:0 0 auto}
.top #nlinked{flex:0 1 auto;min-width:0;overflow:hidden;text-overflow:ellipsis}
.lvl{display:grid;grid-template-columns:8fr 70fr 22fr;align-items:center;padding:0 calc(var(--u)*1.6);
border-bottom:1px solid #1d1d20;min-height:0}
.lvl .lk{font-size:calc(var(--u)*2.4);font-weight:700;color:var(--blue)}
.lvl .lm{display:flex;align-items:flex-end;gap:calc(var(--u)*1.6);width:60%;min-width:0}
.lvl .lbx{flex:1;min-width:0;display:flex;flex-direction:column;gap:calc(var(--u)*.3)}
.lvl .lt{display:flex;height:calc(var(--u)*1.5);border-bottom:1px solid #8a93a1;border-right:1px solid #8a93a1}
.lvl .gt{flex:1;align-self:flex-end;height:45%;border-left:1px solid #8a93a1}
.lvl .gt.m{height:100%;border-left-width:2px;border-left-color:var(--white)}
.lvl .gt.hi{border-left-color:var(--blue)}
.lvl .lb{display:flex;gap:calc(var(--u)*.25);height:calc(var(--u)*2.2)}
.lvl .ls{flex:1;background:#16181d}
.lvl .ls.on{background:#19d36b}
.lvl .ls.on.a{background:#ffd35a}
.lvl .ls.on.r{background:var(--red)}
.lvl .ls.pk{background:var(--white)}
.lvl .lv{flex:0 0 auto;min-width:calc(var(--u)*9);text-align:right;font-size:calc(var(--u)*2.8);font-weight:700;color:var(--white);white-space:nowrap;line-height:1}
.lvl .lv.idle{color:var(--dim)}
.lvl .lv.hot{color:var(--red)}
.tabs{display:grid;grid-template-rows:minmax(0,1fr);grid-template-columns:repeat(7,minmax(0,1fr));gap:calc(var(--u)*.6);padding:calc(var(--u)*.45) calc(var(--u)*1.6);
background:#0b0c0f;border-top:1px solid #1d1d20;min-height:0}
.tab{overflow:hidden;font:inherit;line-height:1;font-weight:700;font-size:calc(var(--u)*2.7);letter-spacing:.08em;color:var(--white);
background:#1a1c22;border:1px solid #3a3f4a;border-radius:calc(var(--u)*.5);cursor:pointer;padding:0;min-width:0;
box-shadow:inset 0 calc(var(--u)*-.3) 0 rgba(0,0,0,.45)}
.tab:hover,.tab:focus-visible{background:#2a2f38;outline:0}
.tab.on{background:var(--blue);border-color:var(--blue);color:#fff}
.tab.menu-t{background:#7d0c1d;border-color:#fff}
.tab.menu-t:hover,.tab.menu-t:focus-visible{background:#fff;color:var(--red)}
.tab.dis{color:#444;border-color:#26292f;cursor:default}

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
.mb.tl{display:grid;overflow:hidden;gap:calc(var(--u)*1.2);grid-template-columns:repeat(var(--c,3),minmax(0,1fr));grid-template-rows:repeat(var(--r,2),minmax(0,1fr))}
.tile{display:flex;flex-direction:column;align-items:center;justify-content:center;gap:calc(var(--u)*.6);min-width:0;min-height:0;overflow:hidden;
cursor:pointer;text-align:center;padding:calc(var(--u)*.8);color:#fff;font-weight:700;font-size:calc(var(--u)*3.3);letter-spacing:.05em;line-height:1.15;
background:#0d1830;border:calc(var(--u)*.3) solid #2a4a86;border-radius:calc(var(--u)*1.4);
box-shadow:inset 0 calc(var(--u)*-.5) 0 rgba(0,0,0,.45)}
.tile .tv{font-size:calc(var(--u)*2.5);color:var(--blue);letter-spacing:.04em}
.tile:active{transform:scale(.97)}
.tile.sel{background:var(--blue);border-color:#fff}
.tile.sel .tv{color:#fff}
.tile.on .tv{color:#19d36b}
.tile.new .tv{color:var(--red)}
.tile.sel.on .tv,.tile.sel.new .tv{color:#fff}
.tile.dis{color:var(--dim);background:#0a0d14;border-color:#1f2430}
.tile.dis .tv{color:var(--dim)}
.tile.dis.sel{background:#1a2438;border-color:var(--dim)}
.mf.tiles{justify-content:space-between;align-items:center}
.mf.tiles .key{min-width:calc(var(--u)*16)}
.mpg{flex:1;text-align:center;font-size:calc(var(--u)*2.6);font-weight:700;color:var(--dim);letter-spacing:.2em}
.uprobe{position:absolute;left:0;top:0;width:var(--u);height:0;visibility:hidden;pointer-events:none}
@media (max-aspect-ratio:1/1){
html,body{height:auto;min-height:100%}
body{display:block;-webkit-text-size-adjust:100%;text-size-adjust:100%}
.screen{--u:1.45vw;margin:0;width:100%;aspect-ratio:auto;height:calc(var(--vh,1vh)*100);min-height:0;border:0;border-radius:0;
display:flex;flex-direction:column;box-shadow:none}
.screen>*{flex:none}
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
.refl span.n.act{display:inline}
.lvl{height:calc(var(--u)*11);padding:calc(var(--u)*1.4) calc(var(--u)*1.6)}
.lvl .lm{width:100%}
.lvl .lt{height:calc(var(--u)*2.2)}
.lvl .lb{height:calc(var(--u)*3.4)}
.lvl .lv{font-size:calc(var(--u)*3.8);min-width:calc(var(--u)*13)}
.tabs{height:calc(var(--u)*19.5);grid-template-columns:repeat(4,minmax(0,1fr));grid-template-rows:repeat(2,minmax(0,1fr));padding:calc(var(--u)*1.2) calc(var(--u)*1.6)}
.tab{font-size:calc(var(--u)*3)}
.panels{flex:1 1 0;min-height:0;display:flex;overflow:hidden}
.panel{flex:1;height:auto;min-height:0;border-right:0}
.panel:last-child{padding-bottom:calc(var(--u)*1)}
.panel.sysp{overflow-y:auto}
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
  function fit() {
    var h = Math.min(window.innerHeight, document.documentElement.clientHeight || window.innerHeight);
    document.documentElement.style.setProperty('--vh', (h / 100) + 'px');
    window.scrollTo(0, 0);
  }
  fit();
  window.addEventListener('resize', fit);
  window.addEventListener('orientationchange', function () { setTimeout(fit, 250); });
  /* Safari reports a stale height while it is still laying out its toolbars: measure again after load and when the viewport changes */
  window.addEventListener('load', function () { fit(); setTimeout(fit, 150); setTimeout(fit, 600); });
  if (window.visualViewport) { window.visualViewport.addEventListener('resize', fit); }
  setTimeout(fit, 100);
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

  var FULL = 180;
  var talkStart = null, curKey = null, lastDur = 0;
  var live = false, idleSince = null, lastCall = null, sizeCls = '';

  function paintTimer(sec, liveNow) {
    var c = 'timer', f = FULL > 0 ? sec / FULL : 0, el = $('timer');
    if (!liveNow) { c += ' idle'; }
    else if (f >= 1) { c += ' over'; }
    else if (f >= 0.85) { c += ' hot'; }
    else if (f >= 0.6) { c += ' warn'; }
    if (el.className !== c) { el.className = c; }
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
  function rows(id, list, classes, meta) {
    var sig = JSON.stringify(list) + (meta ? JSON.stringify(meta) : '');
    if (sigs[id] === sig) { return; }
    sigs[id] = sig;
    var box = $(id), st = box.scrollTop;
    box.setAttribute('data-k', classes.join(''));
    clear(box);
    list.forEach(function (r) {
      var m = meta ? meta[list.indexOf(r)] : null;
      var div = document.createElement('div'); div.className = 'r' + (m && m.sub ? ' sub' : '');
      r.forEach(function (c, idx) {
        var sp = document.createElement('span');
        sp.className = classes[idx] || 'b';
        if (c !== null && typeof c === 'object' && c.tg) {
          sp.className += ' tg'; sp.setAttribute('data-g', c.tg); sp.textContent = c.t;
        } else if (c !== null && typeof c === 'object') { sp.appendChild(callNode(c.call)); }
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
  dragScroll($('hall'));
  $('linked').addEventListener('scroll', function () { arrows('linked', 'lkar'); });
  $('heard').addEventListener('scroll', function () { arrows('heard', 'hdar'); });
  $('hall').addEventListener('scroll', function () { arrows('hall', 'hdar'); });

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
  var wantAllow = false, allowList = [], allowArm = -1, allowArmAt = 0, listSel = 0, allowLoaded = false;

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
      if (logKind === 'pic' && m.kind === 'news') { var kn = document.createElement('span'); kn.className = 'mnew'; kn.textContent = 'NEWS'; h.insertBefore(kn, t); }
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
    l.push({k: 'scg', label: 'SCOPE GAIN', val: spGain, dis: !d.lvl});
    l.push({k: 'gm', label: 'RADAR VIEW', val: d.aprs ? 'OPEN' : 'N/A', dis: !d.aprs});
    l.push({k: 'log', label: 'LOG', val: (d.msg || d.news || d.pics) ? (unreadCount() ? unreadCount() + ' NEW' : 'OPEN') : 'N/A', dis: !(d.msg || d.news || d.pics)});
    l.push({k: 'aprs', label: 'APRS POSITIONS', val: d.aprs ? (d.aprs.connected ? 'LIVE ' : 'OFFLINE ') + d.aprs.known : 'N/A', dis: true});
    l.push({k: 'allow', label: 'PIC SENDERS', val: d.pics ? (admin ? (allowLoaded ? String(allowList.length) : '...') : 'ADMIN') : 'N/A', dis: !d.pics});
    l.push({k: 'monmax', label: 'MON LIMIT', val: d.mon ? (d.mon.max > 0 ? d.mon.max + ' MAX' : 'OFF') : 'N/A', dis: !d.mon});
    l.push({k: 'tot', label: 'TIME LIMIT', val: FULL > 0 ? (FULL % 60 === 0 ? FULL / 60 + ' MIN' : FULL + ' SEC') : 'OFF'});
    l.push({k: 'admin', label: 'ADMIN', val: admin ? 'LOGOUT' : (d.auth ? 'LOGIN' : 'NO PIN SET'), dis: !admin && !d.auth});
    l.push({k: 'about', label: 'ABOUT', val: ''});
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
      if (code === 200) { allowList = j.allow || []; allowLoaded = true; }
      else if (code === 401) { admin = null; allowLoaded = false; if (view === 'list') { msg = 'SESSION EXPIRED'; } else { view = 'pin'; wantAllow = true; pin = ''; msg = 'SESSION EXPIRED'; } }
      if (menuOpen) { drawMenu(); }
    });
  }
  function submitAllow(op, call, id) {
    post('api/picallow', {op: op, call: call, id: id}, function (code, j) {
      if (code === 200) { allowList = j.allow || []; allowLoaded = true; msg = op === 'add' ? 'ADDED ' + call : 'REMOVED ' + call; view = 'allow'; sel = 0; }
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
    body.className = 'mb';
    var tiles = view === 'list';
    $('mf').className = tiles ? 'mf tiles' : 'mf';
    ['kup', 'kdn', 'ksel'].forEach(function (id) { $(id).className = tiles ? 'key hidden' : (id === 'ksel' ? 'key red' : 'key'); });
    $('kfwd').className = 'key hidden';
    $('mpg').textContent = '';
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
    if (view === 'list') { drawTiles(items); return; }
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
  /* ---- main menu as touch tiles: columns and rows follow the size of the screen, FWD / BACK page through them ---- */
  var tcols = 3, tper = 6;
  function tileGrid() {
    var body = $('mbody'), pr = $('uprobe'), u = pr ? pr.getBoundingClientRect().width : 0;
    var w = body.clientWidth, h = body.clientHeight;
    if (!u || !w || !h) { return {c: 3, r: 2}; }
    w = w / u - 3.2; h = h / u - 2.8;                  // inside the padding, in display units
    var c = Math.max(1, Math.min(4, Math.floor((w + 1.2) / (26 + 1.2))));
    var r = Math.max(1, Math.min(6, Math.floor((h + 1.2) / (15 + 1.2))));
    return {c: c, r: r};
  }
  function drawTiles(items) {
    var body = $('mbody'), g = tileGrid(), per = g.c * g.r;
    tcols = g.c; tper = per;
    var pages = Math.ceil(items.length / per), page = Math.floor(sel / per);
    body.className = 'mb tl';
    body.style.setProperty('--c', String(g.c));
    body.style.setProperty('--r', String(g.r));
    items.slice(page * per, page * per + per).forEach(function (it, j) {
      var i = page * per + j, v = String(it.val || '');
      var div = document.createElement('div');
      div.className = 'tile' + (i === sel ? ' sel' : '') + (it.dis ? ' dis' : '') + (v === 'ON' ? ' on' : '') + (/ NEW$/.test(v) ? ' new' : '');
      var a = document.createElement('div'); a.textContent = it.label;
      div.appendChild(a);
      if (v) { var b = document.createElement('div'); b.className = 'tv'; b.textContent = v; div.appendChild(b); }
      div.addEventListener('click', function () { sel = i; drawMenu(); activate(); });
      body.appendChild(div);
    });
    $('mpg').textContent = pages > 1 ? (page + 1) + ' / ' + pages : '';
    if (page < pages - 1) { $('kfwd').className = 'key'; }
  }
  function tilePage(dir, keep) {
    var n = curItems().length, page = Math.floor(sel / tper), pages = Math.ceil(n / tper), np = page + dir;
    if (np < 0) { if (!keep) { closeMenu(); } return; }
    if (np >= pages) { return; }
    sel = Math.min(n - 1, np * tper); msg = ''; drawMenu();
  }
  function moveTile(dc, dr) {
    var n = curItems().length, s = sel + dc + dr * tcols;
    if (dc) { s = (s + n) % n; } else if (s < 0 || s >= n) { return; }
    sel = s; drawMenu();
  }
  function openMenu() { closeGm(); menuOpen = true; view = 'list'; sel = 0; pin = ''; msg = ''; wantAllow = false; $('menu').className = 'menu'; drawMenu(); if (admin && last && last.pics) { loadAllow(); } if (document.activeElement && document.activeElement.blur) { document.activeElement.blur(); } }
  function closeMenu() { menuOpen = false; $('menu').className = 'menu hidden'; $('menubtn').focus(); }
  function back() {
    if (view === 'addcall') { view = 'allow'; msg = ''; drawMenu(); }
    else if (view !== 'list') { view = 'list'; sel = listSel; pin = ''; msg = ''; wantAllow = false; drawMenu(); } else { closeMenu(); }
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
        else { view = wantDmr ? 'confirm' : 'list'; sel = wantDmr ? 0 : listSel; wantDmr = false; if (last && last.pics) { loadAllow(); } }
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
    if (view === 'list') { listSel = sel; }
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
      else { view = 'list'; sel = listSel; }
      drawMenu(); return;
    }
    if (view === 'confirm') {
      if (it.k === 'yes') { sendBridge(); return; }
      view = 'list'; sel = listSel; drawMenu(); return;
    }
    if (view === 'log') {
      if (it.k === 'message' && !it.dis) { var s1 = sel; closeMenu(); sel = s1; openMsg('msg', true); return; }
      if (it.k === 'news' && !it.dis) { var s2 = sel; closeMenu(); sel = s2; openMsg('news', true); return; }
      if (it.k === 'pics' && !it.dis) { var s4 = sel; closeMenu(); sel = s4; openMsg('pic', true); return; }
      if (it.dis) { drawMenu(); return; }
      view = 'list'; sel = listSel; drawMenu(); return;
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
    } else if (it.k === 'scg') {
      if (!d.lvl) { msg = 'NEEDS --levels ON THE SERVER'; }
      else { spGain = spGain === 'LOW' ? 'MED' : (spGain === 'MED' ? 'HIGH' : 'LOW'); store.set('scgain', spGain); spPk = 0; }
    } else if (it.k === 'units') {
      opt.units = opt.units === 'MI' ? 'KM' : 'MI'; store.set('units', opt.units);
      if (last) { render(last); }
    } else if (it.k === 'admin') {
      if (admin) { post('api/logout', {}, function () { admin = null; allowLoaded = false; msg = 'LOGGED OUT'; drawMenu(); }); }
      else if (d.auth) { view = 'pin'; pin = ''; wantDmr = false; }
      else { msg = 'ADMIN PIN NOT SET ON SERVER'; }
    } else if (it.k === 'allow') {
      if (!d.pics) { msg = 'NEEDS --pictures ON THE SERVER'; }
      else if (!admin) {
        if (!d.auth) { msg = 'ADMIN PIN NOT SET ON SERVER'; }
        else { view = 'pin'; pin = ''; wantDmr = false; wantAllow = true; }
      } else { view = 'allow'; sel = 0; loadAllow(); }
    } else if (it.k === 'monmax') {
      if (!admin) { msg = d.auth ? 'LOGIN AS ADMIN TO CHANGE THE MON LIMIT' : 'ADMIN PIN NOT SET ON SERVER'; }
      else {
        var cm = d.mon ? d.mon.max : 0, nm = 0;
        [5, 10, 20, 50].forEach(function (v) { if (nm === 0 && v > cm) { nm = v; } });
        post('api/settings', {mon: nm}, function (code, j) {
          if (code === 200 && typeof j.mon === 'number') { if (last && last.mon) { last.mon.max = j.mon; } msg = ''; }
          else if (code === 401) { admin = null; msg = 'SESSION EXPIRED'; }
          else { msg = 'COULD NOT SAVE'; }
          if (menuOpen) { drawMenu(); }
        });
      }
    } else if (it.k === 'tot') {
      if (!admin) { msg = d.auth ? 'LOGIN AS ADMIN TO CHANGE THE TIME LIMIT' : 'ADMIN PIN NOT SET ON SERVER'; }
      else {
        var TOTS = [60, 120, 180, 300, 600, 0], nx = TOTS[(TOTS.indexOf(FULL) + 1) % TOTS.length];
        post('api/settings', {tot: nx}, function (code, j) {
          if (code === 200 && typeof j.tot === 'number') { FULL = j.tot; if (last) { last.tot = j.tot; } msg = ''; }
          else if (code === 401) { admin = null; msg = 'SESSION EXPIRED'; }
          else { msg = 'COULD NOT SAVE'; }
          if (menuOpen) { drawMenu(); }
        });
      }
    } else if (it.k === 'about') { view = 'about'; }
    else if (it.k === 'exit') { closeMenu(); return; }
    drawMenu();
  }
  $('menubtn').addEventListener('click', openMenu);
  $('kback').addEventListener('click', function () { if (view === 'list') { tilePage(-1); } else { back(); } this.blur(); });
  $('kfwd').addEventListener('click', function () { tilePage(1); this.blur(); });
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
    if (e.key === 'Escape') { if (view === 'list') { closeMenu(); } else { back(); } }
    else if (view === 'list' && e.key === 'ArrowLeft') { moveTile(-1, 0); }
    else if (view === 'list' && e.key === 'ArrowRight') { moveTile(1, 0); }
    else if (view === 'list' && e.key === 'ArrowUp') { moveTile(0, -1); }
    else if (view === 'list' && e.key === 'ArrowDown') { moveTile(0, 1); }
    else if (view === 'list' && e.key === 'PageUp') { tilePage(-1, true); }
    else if (view === 'list' && e.key === 'PageDown') { tilePage(1); }
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
  $('gmbtn').addEventListener('click', function () { if (this.className.indexOf('dis') < 0) { openGm(); } });
  $('gback').addEventListener('click', backGm);
  $('grange').addEventListener('click', function () { gmIdx = (gmIdx + 1) % GM_RANGES.length; store.set('range', String(gmIdx)); drawGm(); });

  /* bottom tabs: one list (or the voice scope) at a time */
  /* LAST HEARD: consecutive transmissions from one station collapse into one row (newest time, count);
     the count opens the full run. Closed by default. */
  var openG = {};
  function paintLast(d) {
    var showD = !!origin() && !!d.aprs, L = d.last_heard, out = [], meta = [], i = 0, seen = {};
    function row(r, sub) {
      var x = [showD ? r.time.slice(0, 5) : r.time, {call: r.src}, Math.round(r.dur) + 's'];
      if (showD) { x.push(distText(r.src, false)); }
      return x;
    }
    while (i < L.length) {
      var j = i;
      while (j + 1 < L.length && L[j + 1].src === L[i].src) { j++; }
      var n = j - i + 1, key = L[j].src + '@' + L[j].time;
      if (n === 1) { out.push(row(L[i])); meta.push({}); }
      else {
        seen[key] = 1;
        var open = !!openG[key], tot = 0, k;
        for (k = i; k <= j; k++) { tot += L[k].dur; }
        var h = [showD ? L[i].time.slice(0, 5) : L[i].time, {call: L[i].src}, {tg: key, t: (open ? '\u25BE ' : '\u25B8 ') + n + '\u00D7'}];
        if (showD) { h.push(distText(L[i].src, false)); }
        out.push(h); meta.push({});
        if (open) {
          for (k = i; k <= j; k++) {
            var c = row(L[k]); c[1] = ''; out.push(c); meta.push({sub: 1});
          }
        }
      }
      i = j + 1;
    }
    for (var kk in openG) { if (!seen[kk]) { delete openG[kk]; } }
    rows('heard', out, showD ? ['b', 'a', 'c', 'd'] : ['b', 'a', 'c'], meta);
  }
  $('heard').addEventListener('click', function (e) {
    var t = e.target;
    if (t && t.getAttribute && t.getAttribute('data-g')) {
      var k = t.getAttribute('data-g');
      if (openG[k]) { delete openG[k]; } else { openG[k] = 1; }
      if (last) { paintLast(last); arrows('heard', 'hdar'); }
    }
  });
  /* MON: listen to the reflector. ysfmon.js (decoder and player) is fetched the first time it is used. */
  var monP = null, monOn = false, monTry = 0, monNote = '', monNoteT = 0, monRetry = 0;
  function monAvail() { return !!(last && last.mon && last.mon.max > 0); }
  function monPaint() {
    var b = $('monbtn'), c = 'tab' + (monOn ? ' on' : '') + (monOn || monAvail() ? '' : ' dis'), t = 'MON';
    if (monNote && Date.now() < monNoteT) { t = monNote; }
    if (b.className !== c) { b.className = c; }
    if (b.textContent !== t) { b.textContent = t; }
    var d = last, mb = $('monb');
    if (!d || !d.mon) { mb.className = 'badge off mon hidden'; return; }
    var n = d.mon.n || 0, cls = 'badge mon' + (n > 0 ? ' blue' : ' off');
    if (mb.className !== cls) { mb.className = cls; }
    $('monn').textContent = String(n);
    mb.title = n + (n === 1 ? ' listener' : ' listeners') + ' on MON';
  }
  function monSay(t) { monNote = t; monNoteT = Date.now() + 3000; monPaint(); setTimeout(monPaint, 3100); }
  function monStop() {
    monOn = false; clearTimeout(monRetry);
    if (monP) { monP.stop(); }
    monPaint();
  }
  function monConnect() {
    monP.start('api/monitor', function (st) {
      if (!monOn) { return; }
      if (st === 'ON') { monTry = 0; monPaint(); }
      else if (st === 'FULL') { monStop(); monSay('FULL'); }
      else if (st === 'END' || st === 'ERROR') {
        if (++monTry > 20) { monStop(); monSay('ERR'); return; }
        monRetry = setTimeout(function () { if (monOn) { monConnect(); } }, 3000);
      } else { monStop(); monSay('N/A'); }
    });
  }
  function monStart() {
    function go() {
      if (!monP) { monP = new window.YSFMon.Player(); }
      monOn = true; monTry = 0; monPaint(); monConnect();
    }
    if (window.YSFMon) { go(); return; }
    var s = document.createElement('script');
    s.src = 'mon.js'; s.onload = go; s.onerror = function () { monSay('ERR'); };
    document.head.appendChild(s);
  }
  $('monbtn').addEventListener('click', function () {
    this.blur();
    if (monOn) { monStop(); } else if (monAvail()) { monStart(); }
  });

  var TABS = ['linked', 'heard', 'sys', 'scope'], curTab = 'linked';
  function scopeOk() { return !!(last && last.lvl); }
  function paintTabs() {
    TABS.forEach(function (n) {
      var c = 'tab' + (n === curTab ? ' on' : '') + (n === 'scope' && !scopeOk() ? ' dis' : '');
      if ($('t-' + n).className !== c) { $('t-' + n).className = c; }
    });
  }
  function setTab(k) {
    if (TABS.indexOf(k) < 0) { k = 'linked'; }
    curTab = k;
    if (k !== 'scope') { store.set('tab', k); }
    TABS.forEach(function (n) {
      $('p-' + n).className = 'panel' + (n === 'sys' ? ' sysp' : (n === 'scope' ? ' scp' : '')) + (n === k ? '' : ' off');
    });
    paintTabs();
    arrows('linked', 'lkar'); arrows('heard', 'hdar');
    scopeShow(k === 'scope');
    if (k !== 'heard') { heardMode = 0; }
    if (typeof paintHeard === 'function') { paintHeard(); }
  }
  /* HEARD button: last heard -> ALL (every callsign ever) -> ACTIVE (24 h chart) -> back */
  var heardMode = 0, HMODES = ['HEARD', 'ALL', 'ACTIVE'], histD = null, histTimer = 0;
  var MON = ['JAN', 'FEB', 'MAR', 'APR', 'MAY', 'JUN', 'JUL', 'AUG', 'SEP', 'OCT', 'NOV', 'DEC'];
  function histOk() { return !!(last && last.hist); }
  function paintHeard() {
    var m = heardMode;
    $('heard').className = 'rows' + (m === 0 ? '' : ' hidden');
    $('hall').className = 'rows' + (m === 1 ? '' : ' hidden');
    $('hactw').className = 'scw' + (m === 2 ? '' : ' hidden');
    $('hdt').textContent = m === 1 && histD ? 'ALL  ' + histD.total : (m === 2 && histD ? 'ACTIVE  AVG TX PER HOUR' : HMODES[m]);
    $('t-heard').textContent = curTab === 'heard' ? HMODES[m] : 'HEARD';
    if (histTimer) { clearInterval(histTimer); histTimer = 0; }
    if (m > 0 && curTab === 'heard') { histFetch(); histTimer = setInterval(histFetch, 30000); }
    histPaint();
    arrows(m === 1 ? 'hall' : 'heard', 'hdar');
  }
  function histFetch() {
    var x = new XMLHttpRequest();
    x.open('GET', 'api/history', true);
    x.onload = function () {
      if (x.status !== 200) { return; }
      try { histD = JSON.parse(x.responseText); } catch (e) { return; }
      histPaint();
    };
    x.send();
  }
  function histWhen(t) {
    var d = parseInt(t.slice(8, 10), 10), m = parseInt(t.slice(5, 7), 10) - 1;
    return (d < 10 ? '0' : '') + d + ' ' + (MON[m] || '') + ' ' + t.slice(11, 16);
  }
  function histPaint() {
    var m = heardMode;
    if (m === 0 || !histD) { return; }
    $('hdt').textContent = m === 1 ? 'ALL  ' + histD.total : (histD.days ? 'ACTIVE  AVG TX PER HOUR' : 'ACTIVE');
    if (m === 1) {
      rows('hall', histD.all.map(function (r) { return [{call: r[0]}, r[3] + (r[4] === 'L' ? ' LINK' : ' TX'), histWhen(r[1])]; }), ['a', 'c', 'b']);
      arrows('hall', 'hdar');
    } else {
      $('hdar').textContent = histD.days ? histD.days + ' DAY' + (histD.days === 1 ? '' : 'S') : '';
      histChart();
    }
  }
  function histChart() {
    var cv = $('hact'), w = cv.parentNode.clientWidth, h = cv.parentNode.clientHeight;
    if (!w || !h || !histD) { return; }
    var dpr = Math.min(2, window.devicePixelRatio || 1);
    if (cv.width !== Math.round(w * dpr) || cv.height !== Math.round(h * dpr)) { cv.width = Math.round(w * dpr); cv.height = Math.round(h * dpr); }
    var g = cv.getContext('2d'); if (!g) { return; }
    if (!spLut) { spLut = spColours(); }
    g.setTransform(dpr, 0, 0, dpr, 0, 0); g.clearRect(0, 0, w, h);
    var lab = Math.max(10, Math.min(14, h * 0.09)), x0 = lab * 2.6, top = lab * 1.2, bot = h - lab * 1.8, hh = bot - top, ww = w - x0 - 2;
    g.font = '700 ' + lab + 'px "DejaVu Sans Mono",Menlo,Consolas,monospace';
    if (!histD.days) {
      g.fillStyle = '#8a93a1'; g.textAlign = 'center'; g.textBaseline = 'middle';
      g.fillText(histD.building ? 'READING THE LOGS...' : 'NO LOG HISTORY YET', w / 2, h / 2); return;
    }
    var mx = 0, pk = 0, i;
    for (i = 0; i < 24; i++) { if (histD.hours[i] > mx) { mx = histD.hours[i]; pk = i; } }
    var top2 = Math.max(1, Math.ceil(mx * 1.1));
    var step = top2 <= 4 ? 1 : (top2 <= 10 ? 2 : (top2 <= 30 ? 5 : (top2 <= 100 ? 20 : 50)));
    top2 = Math.ceil(top2 / step) * step;
    g.strokeStyle = 'rgba(130,150,200,0.28)'; g.lineWidth = 1; g.fillStyle = '#8a93a1'; g.textAlign = 'right'; g.textBaseline = 'middle';
    for (var v = 0; v <= top2; v += step) {
      var y = bot - hh * v / top2;
      g.beginPath(); g.moveTo(x0, y); g.lineTo(w, y); g.stroke();
      g.fillText(String(v), x0 - lab * 0.5, y);
    }
    var bw = ww / 24, nowH = new Date().getHours();
    for (i = 0; i < 24; i++) {
      var f = histD.hours[i] / top2, bh = hh * f;
      if (bh > 0.5) {
        g.fillStyle = spLut[Math.min(63, Math.round(Math.min(1, histD.hours[i] / Math.max(1, mx)) * 50) + 8)];
        g.fillRect(x0 + i * bw + bw * 0.12, bot - bh, bw * 0.76, bh);
      }
      if (i === nowH) { g.strokeStyle = '#ffffff'; g.lineWidth = 1.5; g.strokeRect(x0 + i * bw + bw * 0.08, top, bw * 0.84, hh); }
    }
    g.fillStyle = '#8a93a1'; g.textAlign = 'center'; g.textBaseline = 'top';
    for (i = 0; i < 24; i += 3) { g.fillText(i < 10 ? '0' + i : String(i), x0 + (i + 0.5) * bw, bot + lab * 0.4); }
    g.fillStyle = '#f4f4f4'; g.textAlign = 'left'; g.textBaseline = 'bottom';
    g.fillText('PEAK ' + (pk < 10 ? '0' : '') + pk + ':00   ' + histD.hours[pk].toFixed(1) + ' TX/HR', x0, top - 2);
  }
  window.addEventListener('resize', function () { if (heardMode === 2) { histChart(); } });
  TABS.forEach(function (n) {
    $('t-' + n).addEventListener('click', function () {
      if (this.className.indexOf('dis') >= 0) { return; }
      if (n === 'heard' && curTab === 'heard' && histOk()) { heardMode = (heardMode + 1) % 3; paintHeard(); this.blur(); return; }
      setTab(n); this.blur();
    });
  });

  /* voice spectrum waterfall (rows come from ysfmsgd.py through api/level) */
  var SPN = 20, SPD = 46, SP_LO = 10, SP_HI = 48, SPAL = '0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz-_';
  var spQ = [], spHist = [], spLast = null, spRaf = 0, spT = 0, spAcc = 0, spOn = false, spLut = null;
  function spColours() {
    var st = [[0, 22, 48, 122], [0.25, 47, 123, 255], [0.5, 25, 211, 107], [0.75, 255, 211, 90], [1, 255, 36, 64]], out = [];
    for (var i = 0; i < 64; i++) {
      var x = i / 63, k = 1;
      while (k < st.length - 1 && x > st[k][0]) { k++; }
      var a = st[k - 1], b = st[k], f = (x - a[0]) / (b[0] - a[0]);
      out.push('rgb(' + Math.round(a[1] + (b[1] - a[1]) * f) + ',' + Math.round(a[2] + (b[2] - a[2]) * f) + ',' + Math.round(a[3] + (b[3] - a[3]) * f) + ')');
    }
    return out;
  }
  var SPG = {LOW: 8, MED: 3, HIGH: -3}, spGain = store.get('scgain', 'MED'), spPk = 0, spSm = null, spPend = null;
  if (!SPG.hasOwnProperty(spGain)) { spGain = 'MED'; }
  function spDecode(str) {
    var r = [], mx = 0;
    for (var i = 0; i < SPN; i++) {
      var q = SPAL.indexOf(str.charAt(i)); if (q < 0) { q = 0; }
      r.push(q); if (q > mx) { mx = q; }
    }
    /* reference follows the recent peak (any mic gain), never below SP_HI, and falls slowly */
    spPk = Math.max(mx, spPk - 0.02);
    var hi = Math.max(SP_HI, spPk) + SPG[spGain], lo = hi - 38, o = [];
    for (i = 0; i < SPN; i++) { var v = (r[i] - lo) / (hi - lo); o.push(v < 0 ? 0 : (v > 1 ? 1 : v)); }
    /* smooth across frequency, then rise quickly and fall slowly in time, like an RF waterfall */
    var f = [];
    for (i = 0; i < SPN; i++) { f.push(0.25 * o[Math.max(0, i - 1)] + 0.5 * o[i] + 0.25 * o[Math.min(SPN - 1, i + 1)]); }
    if (!spSm) { spSm = f.slice(); }
    for (i = 0; i < SPN; i++) { spSm[i] += (f[i] - spSm[i]) * (f[i] > spSm[i] ? 0.55 : 0.16); }
    return spSm.slice();
  }
  function spPush(j) {
    if (!j || typeof j.n !== 'number' || !j.sp || !j.sp.length) { return; }
    var take = spLast === null ? 0 : j.n - spLast;
    if (take < 0 || take > j.sp.length) { take = j.sp.length; }
    spLast = j.n;
    if (!spOn) { return; }
    for (var i = j.sp.length - take; i < j.sp.length; i++) {
      var rw = spDecode(j.sp[i]);
      if (!spPend) { spPend = rw; continue; }          /* two 40 ms rows make one, about 12 lines a second */
      for (var k = 0; k < SPN; k++) { spPend[k] = (spPend[k] + rw[k]) / 2; }
      spQ.push(spPend); spPend = null;
    }
    if (spQ.length > 24) { spQ.splice(0, spQ.length - 24); }
    if (take && !spRaf) { spT = 0; spRaf = requestAnimationFrame(spFrame); }
  }
  function spFrame(t) {
    spRaf = 0;
    if (!spOn) { return; }
    var dt = spT ? Math.min(0.2, (t - spT) / 1000) : 0.04;
    spT = t;
    var rate = spQ.length ? 12 + spQ.length * 1.5 : 6;
    spAcc += dt * rate;
    var moved = false;
    while (spAcc >= 1) {
      spAcc -= 1;
      var row = spQ.length ? spQ.shift() : null;
      if (!row && !spHist.length) { spAcc = 0; break; }
      if (row) { spHist.unshift(row); } else { spHist.unshift(null); }
      if (spHist.length > SPD) { spHist.pop(); }
      moved = true;
    }
    spDraw();
    var busy = spQ.length > 0;
    for (var i = 0; i < spHist.length && !busy; i++) { if (spHist[i]) { busy = true; } }
    if (busy) { spRaf = requestAnimationFrame(spFrame); } else { spHist = []; spT = 0; spDraw(); }
  }
  function spDraw() {
    var cv = $('scope'), w = cv.parentNode.clientWidth, h = cv.parentNode.clientHeight;
    if (!w || !h) { return; }
    var dpr = Math.min(2, window.devicePixelRatio || 1);
    if (cv.width !== Math.round(w * dpr) || cv.height !== Math.round(h * dpr)) { cv.width = Math.round(w * dpr); cv.height = Math.round(h * dpr); }
    var g = cv.getContext('2d');
    if (!g) { return; }
    if (!spLut) { spLut = spColours(); }
    g.setTransform(dpr, 0, 0, dpr, 0, 0);
    g.clearRect(0, 0, w, h);
    var lab = Math.max(10, Math.min(14, h * 0.07)), top = h * 0.03, bot = h - lab * 1.9, hh = bot - top;
    var cx = w / 2, w2 = w * 0.47, sb = 0.3, vy = top + hh * 0.1, hmax = hh * 0.72, k = 1 / sb - 1;
    function sOf(r) { return 1 / (1 + k * r / (SPD - 1)); }
    function px(xn, s) { return cx + xn * w2 * s; }
    function py(hn, s) { return vy + (bot - vy) * s - hn * hmax * s; }
    function ln(x1, y1, x2, y2) { g.beginPath(); g.moveTo(x1, y1); g.lineTo(x2, y2); g.stroke(); }
    var r, n, hn, xn;
    /* walls and floor: a grid box that narrows to the back */
    g.lineWidth = 1;
    g.strokeStyle = 'rgba(130,150,200,0.45)';
    for (n = 0; n <= 4; n++) { xn = -1 + n * 0.5; ln(px(xn, 1), py(0, 1), px(xn, sb), py(0, sb)); }
    for (n = 0; n <= SPD - 1; n += 5) { r = sOf(n); ln(px(-1, r), py(0, r), px(1, r), py(0, r)); }
    ln(px(-1, sb), py(0, sb), px(1, sb), py(0, sb));
    g.strokeStyle = 'rgba(130,150,200,0.34)';
    for (n = 1; n <= 4; n++) {
      hn = n / 4;
      ln(px(-1, sb), py(hn, sb), px(1, sb), py(hn, sb));
      ln(px(-1, 1), py(hn, 1), px(-1, sb), py(hn, sb));
      ln(px(1, 1), py(hn, 1), px(1, sb), py(hn, sb));
    }
    for (n = 0; n <= 4; n++) { xn = -1 + n * 0.5; ln(px(xn, sb), py(0, sb), px(xn, sb), py(1, sb)); }
    ln(px(-1, 1), py(0, 1), px(-1, 1), py(1, 1));
    ln(px(1, 1), py(0, 1), px(1, 1), py(1, 1));
    /* the voice: separate spikes, back to front, shrinking, dimming and damping towards the centre */
    var NBAR = 121;
    for (r = SPD - 1; r >= 0; r--) {
      var row = spHist[r]; if (!row) { continue; }
      var s = sOf(r), z = r / (SPD - 1), damp = Math.exp(-1.0 * z), bw = Math.max(1, w * 0.0026 * s);
      g.globalAlpha = 1 - 0.72 * z;
      var yb = py(0, s), prev = 0;
      for (n = 0; n < NBAR; n++) {
        var f = Math.abs(2 * n / (NBAR - 1) - 1) * (SPN - 1), a = Math.floor(f), b = Math.min(SPN - 1, a + 1), v = row[a] + (row[b] - row[a]) * (f - a);
        v = Math.pow(v, 1.3);
        if (v < 0.05) { continue; }
        var bh = v * hmax * s * damp, x = px(-1 + 2 * n / (NBAR - 1), s);
        g.fillStyle = spLut[Math.min(63, Math.round(v * 63))];
        g.fillRect(x - bw / 2, yb - bh, bw, bh);
        if (v > 0.5) { g.fillStyle = '#ffffff'; g.fillRect(x - bw / 2, yb - bh, bw, Math.max(1, bw * 0.6)); }
      }
    }
    g.globalAlpha = 1;
    /* carrier line down the middle, like the radio's red marker */
    g.strokeStyle = 'rgba(255,36,64,0.75)'; g.lineWidth = 1.5;
    ln(px(0, sb), py(0, sb), px(0, sb), py(1, sb));
    ln(px(0, 1), py(0, 1), px(0, sb), py(0, sb));
    /* newest line along the front in red, like the radio's baseline */
    g.strokeStyle = '#ff2440'; g.lineWidth = 2;
    ln(px(-1, 1), py(0, 1), px(1, 1), py(0, 1));
    g.font = '700 ' + lab + 'px "DejaVu Sans Mono",Menlo,Consolas,monospace';
    g.fillStyle = '#8a93a1';
    g.textBaseline = 'top';
    var xs = ['4k', '2k', '0', '2k', '4k'];
    for (var q = 0; q < 5; q++) {
      g.textAlign = q === 0 ? 'left' : (q === 4 ? 'right' : 'center');
      g.fillText(xs[q], px(-1 + q / 2, 1), bot + lab * 0.5);
    }
  }
  function scopeShow(on) {
    spOn = on;
    spQ = []; spHist = []; spAcc = 0; spT = 0; spSm = null; spPend = null;
    if (spRaf) { cancelAnimationFrame(spRaf); spRaf = 0; }
    if (on) { spDraw(); }
  }
  window.addEventListener('resize', function () { if (spOn && !spRaf) { spDraw(); } });
  setTab(store.get('tab', 'linked'));

  /* voice level meter under the callsign (loudness from ysfmsgd.py --level-out) */
  var LV = null, LVN = 40, lsegs = [], lvVal = 0, lvPeak = 0, lvPeakT = 0, lvBusy = false, lvLast = 0;
  (function () {
    var b = $('lvlbar');
    for (var i = 0; i < LVN; i++) { var sp = document.createElement('span'); sp.className = 'ls'; b.appendChild(sp); lsegs.push(sp); }
  })();
  function lvlScale() {
    var t = $('lvlscale'); clear(t);
    for (var k = 0; k < LVN; k++) {
      var sp = document.createElement('span');
      sp.className = 'gt' + (k % 10 === 0 ? ' m' : '') + (k >= LVN * 0.6 ? ' hi' : '');
      t.appendChild(sp);
    }
  }
  function lvlPaint(txt) {
    var on = Math.round(lvVal * LVN), pk = Math.min(LVN - 1, Math.round(lvPeak * LVN) - 1);
    for (var i = 0; i < LVN; i++) {
      var c = 'ls';
      if (i < on) { c += ' on' + (i >= LVN * 0.85 ? ' r' : (i >= LVN * 0.6 ? ' a' : '')); }
      else if (i === pk && lvPeak > 0.02) { c += ' pk'; }
      if (lsegs[i].className !== c) { lsegs[i].className = c; }
    }
    var v = $('lvlval');
    v.textContent = txt;
    v.className = 'lv' + (txt === '-- dB' ? ' idle' : (lvVal > 0.85 ? ' hot' : ''));
  }
  function lvlPush(j) {
    var now = Date.now(), span = LV.full - LV.floor;
    var x = j ? Math.min(1, Math.max(0, (j.db - LV.floor) / span)) : 0;
    var xp = j ? Math.min(1, Math.max(0, (j.pk - LV.floor) / span)) : 0;
    lvVal = x > lvVal ? x : lvVal + (x - lvVal) * 0.45;
    if (lvVal < 0.01) { lvVal = 0; }
    if (xp >= lvPeak || now - lvPeakT > 1200) { lvPeak = Math.max(xp, lvVal); lvPeakT = now; }
    lvlPaint(j ? Math.round(j.db) + ' dB' : '-- dB');
  }
  function lvlPoll() {
    if (!LV || lvBusy) { return; }
    var now = Date.now();
    if (talkStart === null && lvVal === 0 && now - lvLast < 1000) { return; }
    lvLast = now; lvBusy = true;
    var x = new XMLHttpRequest();
    x.open('GET', 'api/level', true);
    x.onload = function () {
      var j = null;
      if (x.status === 200) { try { j = JSON.parse(x.responseText); } catch (e) {} }
      lvlPush(j);
      spPush(j);
    };
    x.onloadend = function () { lvBusy = false; };
    x.send();
  }
  setInterval(lvlPoll, 200);

  var nodeShown = null;
  function render(d) {
    if (d.fade) { FADE = d.fade; }
    if (typeof d.tot === 'number') { FULL = d.tot; }
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
    var act = (nt && d.act && d.act.call === nt.src) ? d.act.kind : '';   // pic / data while a transfer is in progress
    $('lamp').textContent = act ? 'DATA' : 'BUSY';
    $('mode').textContent = act === 'pic' ? 'PICTURE DATA' : (act === 'data' ? 'MESSAGE DATA' : 'DIGITAL VOICE');
    $('mode').className = 'n' + (act ? ' act' : '');
    if (nt) {
      var di = destInfo(nt.dest); talkId = di.id;
      var key = nt.src + '|' + nt.dest + '|' + nt.gw;
      if (talkStart === null || key !== curKey) {
        talkStart = Date.now() - nt.elapsed * 1000;
        curKey = key;
      }
      $('rlab').textContent = act === 'pic' ? 'PICTURE TO' : (act === 'data' ? 'MESSAGE TO' : 'TO');
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
    if (!nt) { $('timer').textContent = mmss(lastDur); paintTimer(lastDur, false); }
    else { paintTimer(talkStart === null ? 0 : (Date.now() - talkStart) / 1000, true); }

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
    paintLast(d);
    arrows('linked', 'lkar');
    if (heardMode === 0) { arrows('heard', 'hdar'); }

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
    $('gmbtn').className = 'tab' + (d.aprs ? '' : ' dis');
    monPaint();
    paintTabs();
    if (curTab === 'scope' && !d.lvl) { setTab('linked'); }
    if (d.lvl && (!LV || LV.floor !== d.lvl.floor || LV.full !== d.lvl.full)) { LV = d.lvl; lvlScale(); }
    else if (!d.lvl) { LV = null; }
    if (gmOpen) { drawGm(); }
  }
  function tick() {
    if (talkStart !== null) {
      var sec = (Date.now() - talkStart) / 1000;
      $('timer').textContent = mmss(sec);
      paintTimer(sec, true);
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
<span id="monb" class="badge off mon hidden" title="Listeners on MON"><svg class="spk" viewBox="0 0 20 14" aria-hidden="true"><path d="M1 4.5h3.5L9 1v12L4.5 9.5H1z" fill="currentColor"/><path d="M12 4.2a3.6 3.6 0 0 1 0 5.6M14.6 2a6.6 6.6 0 0 1 0 10" fill="none" stroke="currentColor" stroke-width="1.6" stroke-linecap="round"/></svg><span id="monn">0</span></span>
<button id="mail" class="badge mailb hidden" type="button" title="New messages" aria-label="New messages"><svg class="env" viewBox="0 0 20 14" aria-hidden="true"><rect x="1" y="1" width="18" height="12" rx="1.5" fill="none" stroke="currentColor" stroke-width="1.8"/><polyline points="1.5,2 10,8 18.5,2" fill="none" stroke="currentColor" stroke-width="1.8"/></svg><span id="mailn"></span></button>
<button id="picb" class="badge mailb hidden" type="button" title="New pictures" aria-label="New pictures"><svg class="env" viewBox="0 0 20 14" aria-hidden="true"><rect x="1" y="1" width="18" height="12" rx="1.5" fill="none" stroke="currentColor" stroke-width="1.8"/><polyline points="3,11 8,6 11,9 14,5.5 17,11" fill="none" stroke="currentColor" stroke-width="1.6"/><circle cx="14.5" cy="4" r="1.2" fill="currentColor"/></svg><span id="picn"></span></button>
<span id="node"></span>
<a id="loc" class="loc hidden" href="#" target="_blank" rel="noopener noreferrer" title="Node location">
<svg class="pin" viewBox="0 0 12 16" aria-hidden="true"><path fill="currentColor" d="M6 0C2.7 0 0 2.6 0 5.8 0 10 6 16 6 16s6-6 6-10.2C12 2.6 9.3 0 6 0zm0 8.2a2.4 2.4 0 1 1 0-4.8 2.4 2.4 0 0 1 0 4.8z"/></svg>
<span id="locv"></span></a>
<span class="sp"></span><span id="nlinked"></span><span id="clock"></span>
</div>
<div class="vfo"><div class="band">A</div><div id="call" class="call old">------</div>
<div class="side"><div id="lamp" class="lamp">BUSY</div><div id="timer" class="timer idle">00:00</div><div id="rid" class="rid"></div></div></div>
<div class="lvl"><span class="lk">LVL</span><div class="lm"><div class="lbx"><div id="lvlscale" class="lt"></div><div id="lvlbar" class="lb"></div></div><span id="lvlval" class="lv idle">-- dB</span></div><span></span></div>
<div class="route"><b id="rlab">LAST</b><span id="dest"></span><span id="rdist" class="rdist"></span></div>
<div class="refl"><span id="rname"></span><span id="mode" class="n">DIGITAL VOICE</span></div>
<div class="panels">
<div id="p-linked" class="panel"><h2><span>LINKED</span><span id="lkar" class="ar"></span></h2><div id="linked" class="rows"></div></div>
<div id="p-heard" class="panel off"><h2><span id="hdt">HEARD</span><span id="hdar" class="ar"></span></h2><div id="heard" class="rows"></div><div id="hall" class="rows hidden"></div><div id="hactw" class="scw hidden"><canvas id="hact"></canvas></div></div>
<div id="p-scope" class="panel scp off"><h2><span>SCOPE</span><span class="ar">VOICE kHz</span></h2><div class="scw"><canvas id="scope"></canvas></div></div>
<div id="p-sys" class="panel sysp off"><h2><span>SYSTEM</span></h2>
<div class="g"><span class="k">CPU</span><div class="bar"><i id="cpub"></i></div><span id="cpuv" class="v">--</span></div>
<div class="g"><span class="k">TMP</span><div class="bar"><i id="tmpb"></i></div><span id="tmpv" class="v">--</span></div>
<div class="g"><span class="k">MEM</span><div class="bar"><i id="memb"></i></div><span id="memv" class="v">--</span></div>
<div class="g"><span class="k">LOAD</span><span></span><span id="loadv" class="v">--</span></div>
<div class="g"><span class="k">UP</span><span></span><span id="upv" class="v">--</span></div>
</div></div>
<div class="tabs"><button id="menubtn" class="tab menu-t" type="button">MENU</button><button id="t-linked" class="tab on" type="button">LINKED</button><button id="t-heard" class="tab" type="button">HEARD</button><button id="t-sys" class="tab" type="button">SYS</button><button id="t-scope" class="tab dis" type="button">SCOPE</button><button id="gmbtn" class="tab dis" type="button">RADAR</button><button id="monbtn" class="tab dis" type="button">MON</button></div>
<div id="menu" class="menu hidden"><div class="mh"><span id="mtitle">MENU</span><span id="mnode"></span></div>
<div id="mbody" class="mb"></div><div id="mmsg" class="mmsg"></div>
<div id="mf" class="mf"><button id="kback" class="key" type="button">BACK</button><span id="mpg" class="mpg"></span><button id="kup" class="key" type="button">&#9650;</button><button id="kdn" class="key" type="button">&#9660;</button><button id="ksel" class="key red" type="button">SELECT</button><button id="kfwd" class="key hidden" type="button">FWD &#9654;</button></div><div class="uprobe" id="uprobe"></div></div>
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
            elif path == "/mon.js" and getattr(args, "mon_js", None):
                self._send(200, "application/javascript; charset=utf-8", args.mon_js, head_only)
            elif path == "/api/monitor" and getattr(args, "mon", None) is not None and not head_only:
                self._monitor()
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
                d["act"] = _activity(args.activity_file)
                d["hist"] = getattr(args, "hist", None) is not None
                d["mon"] = ({"n": args.mon.count(), "max": args.mon_max}
                            if getattr(args, "mon", None) is not None else None)
                d["lvl"] = {"floor": args.level_floor, "full": args.level_full} if args.levels else None
                body = json.dumps(d).encode("utf-8")
                self._send(200, "application/json", body, head_only)
            elif path == "/api/history":
                h = getattr(args, "hist", None)
                if h is None:
                    self._send(404, "text/plain", b"not found\n", head_only)
                else:
                    self._send(200, "application/json", json.dumps(h.snapshot()).encode("utf-8"), head_only)
            elif path == "/api/level":
                body = json.dumps(_level(args.level_file) if args.levels else None).encode("utf-8")
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

        def _monitor(self):
            """One MON listener: an open response that carries voice packets until the client leaves."""
            mon = args.mon
            lid = mon.join(self.client_address[0])
            if lid is None:
                return self._send(503, "text/plain; charset=utf-8", b"monitor limit reached")
            try:
                self.send_response(200)
                self.send_header("Content-Type", "application/octet-stream")
                self.send_header("Cache-Control", "no-store, no-transform")
                self.send_header("X-Accel-Buffering", "no")
                self.send_header("X-Content-Type-Options", "nosniff")
                self.send_header("Content-Security-Policy", CSP)
                self.send_header("Connection", "close")
                self.end_headers()
                self.wfile.write(b"\x00")
                for rec in mon.records(lid):
                    self.wfile.write(rec)
            except (OSError, ValueError):
                pass
            finally:
                mon.leave(lid)
                self.close_connection = True

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
            if menu is None or path not in ("/api/login", "/api/logout", "/api/bridge", "/api/delete", "/api/picallow", "/api/settings"):
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
                audit(self, "login " + (err or "ok"))
                if err == "locked":
                    return self._json(429, {"error": "locked"})
                if err:
                    return self._json(403, {"error": "bad"})
                return self._json(200, {"ok": True, "csrf": csrf},
                                  self._cookie(tok, auth.SESSION_MAX))
            token = self._token()
            if auth.check(token, self.headers.get("X-CSRF", "")) is None:
                audit(self, "refused %s (no valid login)" % path)
                return self._json(401, {"error": "login required"})
            if path == "/api/logout":
                audit(self, "logout")
                auth.logout(token)
                return self._json(200, {"ok": True}, self._cookie("", 0))
            if path == "/api/settings":
                cur = load_settings(args.settings_file)
                if "tot" in obj:
                    tot = obj.get("tot")
                    if not isinstance(tot, int) or isinstance(tot, bool) or not (tot == 0 or 10 <= tot <= 600):
                        return self._json(400, {"error": "bad value"})
                    cur["tot"] = tot
                elif "mon" in obj and args.mon is not None:
                    mon = obj.get("mon")
                    if not isinstance(mon, int) or isinstance(mon, bool) or not (0 <= mon <= ysfmon.MAX_LISTENERS):
                        return self._json(400, {"error": "bad value"})
                    cur["mon"] = mon
                else:
                    return self._json(400, {"error": "bad value"})
                try:
                    save_settings(args.settings_file, cur)
                except OSError:
                    return self._json(500, {"error": "cannot write"})
                if "tot" in obj:
                    args.tot = cur["tot"]
                    audit(self, "time limit set to %d" % args.tot)
                else:
                    args.mon_max = cur["mon"]
                    args.mon.set_max(args.mon_max)
                    audit(self, "monitor limit set to %d" % args.mon_max)
                return self._json(200, {"ok": True, "tot": args.tot, "mon": args.mon_max})
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
                audit(self, "delete %s %d found=%s" % (obj.get("kind"), mid, found))
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
                audit(self, "picture sender %s %s id=%s ok=%s" % (op, call, rid or "-", ok))
                return self._json(200 if ok else 404, {"ok": ok, "allow": al.list()})
            if menu.bridge is None:
                return self._json(404, {"error": "bridge not configured"})
            action = obj.get("action")
            if action not in ("start", "stop"):
                return self._json(400, {"error": "bad action"})
            audit(self, "bridge %s" % action)
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


def _activity(path):
    """What ysfmsgd.py says is being received right now ({"kind": "pic"|"data", "call"}), else None.
    Ignored when older than 6 seconds, so a stopped ysfmsgd or a failed transfer cannot leave it stuck."""
    try:
        with open(path, "r") as f:
            a = json.loads(f.read(512))
        if (isinstance(a, dict) and a.get("kind") in ("pic", "data") and isinstance(a.get("call"), str)
                and isinstance(a.get("t"), (int, float)) and 0 <= time.time() - a["t"] <= 6):
            return {"kind": a["kind"], "call": a["call"]}
    except (OSError, ValueError):
        pass
    return None


SPROW = re.compile(r"^[0-9A-Za-z_-]{20}$")


def _level(path):
    """The loudness ysfmsgd.py last wrote ({"db", "pk", "call"}), or None when nobody is talking.
    Ignored when older than 1.2 seconds."""
    try:
        with open(path, "r") as f:
            a = json.loads(f.read(4096))
        if (isinstance(a, dict) and isinstance(a.get("db"), (int, float)) and isinstance(a.get("pk"), (int, float))
                and isinstance(a.get("t"), (int, float)) and 0 <= time.time() - a["t"] <= 1.2):
            out = {"db": a["db"], "pk": a["pk"], "call": str(a.get("call", ""))[:12]}
            sp, n = a.get("sp"), a.get("n")
            if (isinstance(n, int) and not isinstance(n, bool) and isinstance(sp, list) and len(sp) <= 15
                    and all(isinstance(r, str) and SPROW.match(r) for r in sp)):
                out["n"], out["sp"] = n, sp
            return out
    except (OSError, ValueError):
        pass
    return None


def load_settings(path):
    """Settings the admin changed from the menu ({"tot": seconds, "mon": listeners}); {} when none are saved."""
    out = {}
    try:
        with open(path, "r") as f:
            a = json.loads(f.read(512))
        if isinstance(a, dict):
            t, m = a.get("tot"), a.get("mon")
            if isinstance(t, int) and not isinstance(t, bool) and (t == 0 or 10 <= t <= 600):
                out["tot"] = t
            if isinstance(m, int) and not isinstance(m, bool) and 0 <= m <= ysfmon.MAX_LISTENERS:
                out["mon"] = m
    except (OSError, ValueError):
        pass
    return out


def save_settings(path, d):
    tmp = path + ".tmp"
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o644)
    with os.fdopen(fd, "w") as f:
        json.dump(d, f)
    os.replace(tmp, path)


def audit(handler, what):
    """One line per admin action in the journal (stderr): who from, what. The forwarded-for header is
    logged as sent and is not trusted for anything."""
    try:
        xff = (handler.headers.get("X-Forwarded-For") or "")[:60]
        sys.stderr.write("ysfradio: AUDIT %s from %s%s\n" % (
            what, handler.client_address[0], (" xff=" + xff.replace("\n", " ")) if xff else ""))
        sys.stderr.flush()
    except (OSError, ValueError):
        pass


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
    p.add_argument("--tot", type=int, default=180,
                   help="your reflector's talk time limit in seconds (default 180, 0 = none): the timer digits "
                        "turn amber, orange and red as it runs out. An admin can change it from the menu "
                        "(TIME LIMIT), and that choice is kept in <state-dir>/settings.json")
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
    p.add_argument("--activity-file", default="",
                   help="file written by ysfmsgd.py that marks picture/data transfers (default: <state-dir>/activity.json)")
    p.add_argument("--monitors", type=int, default=10, metavar="N",
                   help="most listeners the MON button allows at once (default 10, 0 = off); the admin can change "
                        "it from the menu (MON LIMIT) and that choice is kept in <state-dir>/settings.json")
    p.add_argument("--no-monitor", action="store_true",
                   help="turn off the MON button (needs ysfmon.js beside this file and --level-out on ysfmsgd.py "
                        "or --monitor-out there)")
    p.add_argument("--monitor-sock", default="",
                   help="local socket ysfmsgd.py sends voice to (default: <state-dir>/monitor.sock)")
    p.add_argument("--no-history", action="store_true",
                   help="turn off the HEARD tab's ALL and ACTIVE lists (they are built from the reflector logs "
                        "and kept in <state-dir>/history.json)")
    p.add_argument("--history-file", default="", help="history file (default: <state-dir>/history.json)")
    p.add_argument("--levels", action="store_true",
                   help="show the voice level meter under the callsign (needs ysfmsgd.py --level-out)")
    p.add_argument("--level-file", default="",
                   help="level file written by ysfmsgd.py (default: <state-dir>/level.json)")
    p.add_argument("--level-floor", type=float, default=0.0, help="level shown as an empty meter, dB (default 0)")
    p.add_argument("--level-full", type=float, default=40.0, help="level shown as a full meter, dB (default 40)")
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
    args.activity_file = args.activity_file or os.path.join(args.state_dir, "activity.json")
    args.level_file = args.level_file or os.path.join(args.state_dir, "level.json")
    args.levels = bool(args.levels or args.level_file != os.path.join(args.state_dir, "level.json"))
    if args.level_full <= args.level_floor + 1:
        raise SystemExit("--level-full must be above --level-floor")
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
    args.tot = 0 if args.tot <= 0 else max(10, min(600, args.tot))
    args.settings_file = os.path.join(args.state_dir, "settings.json")
    args.tot = load_settings(args.settings_file).get("tot", args.tot)
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
    args.mon, args.mon_js = None, None
    args.mon_max = max(0, min(ysfmon.MAX_LISTENERS, args.monitors))
    args.mon_max = load_settings(args.settings_file).get("mon", args.mon_max)
    js_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "ysfmon.js")
    if not args.no_monitor:
        try:
            with open(js_path, "rb") as f:
                args.mon_js = f.read()
            args.mon = ysfmon.Monitor(args.monitor_sock or os.path.join(args.state_dir, "monitor.sock"), args.mon_max)
            t = threading.Thread(target=args.mon.run, args=(stop,))
            t.daemon = True
            t.start()
        except OSError as e:
            sys.stderr.write("ysfradio: MON is off (%s)\n" % e)
            args.mon_js = None
    args.hist = None
    if not args.no_history:
        args.hist = ysfhist.History(args.history_file or os.path.join(args.state_dir, "history.json"),
                                    args.log_dir, args.prefix)
        t = threading.Thread(target=args.hist.run, args=(stop,))
        t.daemon = True
        t.start()
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
