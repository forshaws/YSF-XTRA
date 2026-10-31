#!/usr/bin/env python3
"""Backend pieces for the ysfradio MENU: PIN login, DMR bridge control, APRS positions.

Standard library only, Python 3.5+.
"""

import base64
import hashlib
import hmac
import json
import math
import os
import re
import socket
import subprocess
import threading
import time

CALL_BASE_RE = re.compile(r"^[A-Z0-9]{1,3}[0-9][A-Z]{1,4}$")


# ---------------------------------------------------------------- PIN / sessions

PBKDF2_ITER = 200000


def make_pin_record(pin):
    salt = os.urandom(16)
    dk = hashlib.pbkdf2_hmac("sha256", pin.encode("utf-8"), salt, PBKDF2_ITER)
    return "pbkdf2_sha256$%d$%s$%s" % (
        PBKDF2_ITER,
        base64.b64encode(salt).decode("ascii"),
        base64.b64encode(dk).decode("ascii"))


def check_pin_record(record, pin):
    try:
        algo, iters, salt, want = record.strip().split("$")
        if algo != "pbkdf2_sha256":
            return False
        iters = int(iters)
        if not 1000 <= iters <= 5000000:
            return False
        dk = hashlib.pbkdf2_hmac("sha256", pin.encode("utf-8"),
                                 base64.b64decode(salt), iters)
        return hmac.compare_digest(dk, base64.b64decode(want))
    except (ValueError, TypeError):
        return False


def write_pin_file(path, pin):
    tmp = path + ".tmp"
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o640)
    with os.fdopen(fd, "w") as f:
        f.write(make_pin_record(pin) + "\n")
    os.rename(tmp, path)


class Auth(object):
    """PIN check, in-memory sessions, per-IP lockout."""

    SESSION_IDLE = 20 * 60
    SESSION_MAX = 8 * 3600
    MAX_FAILS = 5
    LOCKOUT = 5 * 60
    MAX_SESSIONS = 20

    def __init__(self, pin_file):
        self.pin_file = pin_file
        self.record = None
        self.lock = threading.Lock()
        self.sessions = {}   # token -> [created, last_seen, csrf]
        self.fails = {}      # ip -> [count, locked_until, last_fail]
        self.recent = []     # timestamps of recent PIN checks, all IPs
        self.reload()

    def reload(self):
        try:
            with open(self.pin_file) as f:
                self.record = f.read().strip() or None
        except (OSError, TypeError):
            self.record = None

    @property
    def enabled(self):
        return self.record is not None

    def _prune(self, now):
        for t in [t for t, s in self.sessions.items()
                  if now - s[1] > self.SESSION_IDLE or now - s[0] > self.SESSION_MAX]:
            del self.sessions[t]
        for ip in [ip for ip, f in self.fails.items() if f[1] < now and now - f[2] > 3600]:
            del self.fails[ip]
        if len(self.fails) > 5000:
            self.fails.clear()

    def login(self, ip, pin):
        """Return (token, csrf, error). error is a short string or None."""
        now = time.time()
        with self.lock:
            self._prune(now)
            f = self.fails.get(ip)
            if f and f[1] > now:
                return None, None, "locked"
            self.recent = [t for t in self.recent if now - t < 60]
            if len(self.recent) >= 15:       # global cap: PBKDF2 is deliberately slow
                return None, None, "locked"
            self.recent.append(now)
        if not self.enabled:
            return None, None, "disabled"
        ok = check_pin_record(self.record, pin)
        with self.lock:
            now = time.time()
            if not ok:
                f = self.fails.setdefault(ip, [0, 0, now])
                f[0] += 1
                f[2] = now
                if f[0] >= self.MAX_FAILS:
                    f[0] = 0
                    f[1] = now + self.LOCKOUT
                    return None, None, "locked"
                return None, None, "bad"
            self.fails.pop(ip, None)
            if len(self.sessions) >= self.MAX_SESSIONS:
                oldest = min(self.sessions, key=lambda t: self.sessions[t][1])
                del self.sessions[oldest]
            token = base64.urlsafe_b64encode(os.urandom(24)).decode("ascii")
            csrf = base64.urlsafe_b64encode(os.urandom(18)).decode("ascii")
            self.sessions[token] = [now, now, csrf]
            return token, csrf, None

    def check(self, token, csrf=None):
        """Valid session? If csrf is given it must match too. Returns csrf or None."""
        if not token:
            return None
        now = time.time()
        with self.lock:
            self._prune(now)
            for t, s in self.sessions.items():
                if hmac.compare_digest(t, token):
                    if csrf is not None and not hmac.compare_digest(s[2], csrf):
                        return None
                    s[1] = now
                    return s[2]
        return None

    def logout(self, token):
        with self.lock:
            for t in list(self.sessions):
                if hmac.compare_digest(t, token or ""):
                    del self.sessions[t]


# ---------------------------------------------------------------- DMR bridge

UNIT_RE = re.compile(r"^[A-Za-z0-9_.@-]{1,60}\.service$")


class BridgeControl(object):
    """Read status with `systemctl is-active`; change it by dropping a request file
    that a root-owned systemd path unit acts on (this process holds no privileges)."""

    def __init__(self, unit, request_file, systemctl="systemctl"):
        if not UNIT_RE.match(unit):
            raise ValueError("bad unit name")
        self.unit = unit
        self.request_file = request_file
        self.systemctl = systemctl
        self._cache = (0, "unknown")
        self._lock = threading.Lock()

    def status(self, max_age=3):
        with self._lock:
            now = time.time()
            if now - self._cache[0] < max_age:
                return self._cache[1]
        try:
            p = subprocess.run([self.systemctl, "is-active", self.unit],
                               stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                               timeout=5)
            out = p.stdout.decode("ascii", "replace").strip()
        except (OSError, subprocess.SubprocessError):
            out = "unknown"
        if out not in ("active", "inactive", "activating", "deactivating",
                       "failed", "reloading"):
            out = "unknown"
        with self._lock:
            self._cache = (time.time(), out)
        return out

    def request(self, action):
        if action not in ("start", "stop"):
            raise ValueError("bad action")
        tmp = self.request_file + ".tmp"
        with open(tmp, "w") as f:
            f.write(action + "\n")
        os.rename(tmp, self.request_file)
        with self._lock:
            self._cache = (0, "unknown")


# ---------------------------------------------------------------- APRS parsing

def _b91(s):
    v = 0
    for ch in s:
        o = ord(ch) - 33
        if not 0 <= o < 91:
            raise ValueError("bad base91")
        v = v * 91 + o
    return v


def _ok(lat, lon):
    if -90 <= lat <= 90 and -180 <= lon <= 180 and not (lat == 0 and lon == 0):
        return lat, lon
    return None


def parse_uncompressed(s):
    """s starts at the latitude: DDMM.mmN/DDDMM.mmE$"""
    if len(s) < 18:
        return None
    lat_s, lon_s = s[0:8], s[9:18]
    try:
        lat_s = lat_s.replace(" ", "0")
        lon_s = lon_s.replace(" ", "0")
        lat = int(lat_s[0:2]) + float(lat_s[2:7]) / 60.0
        if lat_s[7] == "S":
            lat = -lat
        elif lat_s[7] != "N":
            return None
        lon = int(lon_s[0:3]) + float(lon_s[3:8]) / 60.0
        if lon_s[8] == "W":
            lon = -lon
        elif lon_s[8] != "E":
            return None
    except ValueError:
        return None
    return _ok(lat, lon)


def parse_compressed(s):
    """s starts at the symbol table char: T YYYY XXXX C cs T"""
    if len(s) < 10:
        return None
    try:
        lat = 90.0 - _b91(s[1:5]) / 380926.0
        lon = -180.0 + _b91(s[5:9]) / 190463.0
    except ValueError:
        return None
    return _ok(lat, lon)


def parse_mice(dest, info):
    """dest = destination call (6+ chars), info = payload starting at the type char."""
    d = dest.split("-")[0]
    if len(d) < 6 or len(info) < 9:
        return None
    digits = []
    flags = []
    for ch in d[:6]:
        if "0" <= ch <= "9":
            digits.append(ord(ch) - 48)
            flags.append(False)
        elif "A" <= ch <= "J":
            digits.append(ord(ch) - 65)
            flags.append(True)
        elif "P" <= ch <= "Y":
            digits.append(ord(ch) - 80)
            flags.append(True)
        elif ch in "KLZ":
            digits.append(0)
            flags.append(ch != "L")
        else:
            return None
    north = flags[3]
    lon_off = flags[4]
    west = flags[5]
    lat = digits[0] * 10 + digits[1] + (digits[2] * 10 + digits[3] + (digits[4] * 10 + digits[5]) / 100.0) / 60.0
    if not north:
        lat = -lat
    deg = ord(info[1]) - 28
    if lon_off:
        deg += 100
    if 180 <= deg <= 189:
        deg -= 80
    elif 190 <= deg <= 199:
        deg -= 190
    mn = ord(info[2]) - 28
    if mn >= 60:
        mn -= 60
    hund = ord(info[3]) - 28
    if not (0 <= deg <= 179 and 0 <= mn < 60 and 0 <= hund <= 99):
        return None
    lon = deg + (mn + hund / 100.0) / 60.0
    if west:
        lon = -lon
    return _ok(lat, lon)


def _is_compressed(body):
    # uncompressed starts with a latitude digit; compressed starts with the table id
    return bool(body) and not body[0].isdigit() and body[0] != " "


def parse_aprs_packet(line):
    """Return (source_callsign_with_ssid, lat, lon) or None."""
    if not line or line[0] == "#":
        return None
    try:
        head, payload = line.split(":", 1)
        src, rest = head.split(">", 1)
    except ValueError:
        return None
    if not payload or len(src) > 9:
        return None
    dest = rest.split(",", 1)[0]
    t = payload[0]
    pos = None
    if t in "!=":
        body = payload[1:]
        pos = parse_compressed(body) if _is_compressed(body) else parse_uncompressed(body)
    elif t in "/@":
        body = payload[8:]
        pos = parse_compressed(body) if _is_compressed(body) else parse_uncompressed(body)
    elif t in "`'":
        pos = parse_mice(dest, payload)
    if pos is None:
        return None
    return src.upper(), pos[0], pos[1]


# ---------------------------------------------------------------- APRS tracker

class AprsTracker(object):
    """Read-only APRS-IS client. Asks the server only for packets from callsigns that
    have been heard on the reflector, and keeps the latest position of each."""

    TTL = 30 * 86400
    MAX_CALLS = 5000

    def __init__(self, server, login_call, cache_file, max_watch=40):
        host, _, port = server.partition(":")
        self.host = host
        self.port = int(port or 14580)
        self.login_call = login_call or "N0CALL"
        self.cache_file = cache_file
        self.max_watch = max_watch
        self.lock = threading.Lock()
        self.pos = {}        # base call -> [lat, lon, ts]
        self.watch = []      # most-recent-first base calls
        self.watch_dirty = False
        self.connected = False
        self.dirty = False
        self._load()

    def _load(self):
        if not self.cache_file:
            return
        try:
            with open(self.cache_file) as f:
                d = json.load(f)
            now = time.time()
            for c, v in d.items():
                if CALL_BASE_RE.match(c) and now - v[2] < self.TTL:
                    self.pos[c] = [float(v[0]), float(v[1]), float(v[2])]
        except (OSError, ValueError, TypeError, IndexError):
            pass

    def save(self):
        if not self.cache_file or not self.dirty:
            return
        with self.lock:
            data = dict(self.pos)
            self.dirty = False
        tmp = self.cache_file + ".tmp"
        try:
            with open(tmp, "w") as f:
                json.dump(data, f)
            os.rename(tmp, self.cache_file)
        except OSError:
            pass

    def note_heard(self, calls):
        """Tell the tracker which callsigns to look for (most recent first)."""
        out = []
        for c in calls:
            b = c.upper().split("/")[0].split("-")[0]
            if CALL_BASE_RE.match(b) and b not in out:
                out.append(b)
            if len(out) >= self.max_watch:
                break
        with self.lock:
            if out != self.watch:
                self.watch = out
                self.watch_dirty = True

    def update(self, src, lat, lon):
        base = src.split("-")[0]
        if not CALL_BASE_RE.match(base):
            return
        with self.lock:
            if base not in self.watch:
                return
            if len(self.pos) >= self.MAX_CALLS and base not in self.pos:
                oldest = min(self.pos, key=lambda c: self.pos[c][2])
                del self.pos[oldest]
            self.pos[base] = [lat, lon, time.time()]
            self.dirty = True

    def lookup(self, calls):
        """{call: [lat, lon]} rounded to 2 decimals (about 1 km)."""
        out = {}
        with self.lock:
            for c in calls:
                b = c.upper().split("/")[0].split("-")[0]
                p = self.pos.get(b)
                if p:
                    out[c] = [round(p[0], 2), round(p[1], 2)]
        return out

    def status(self):
        with self.lock:
            return {"on": True, "connected": self.connected, "known": len(self.pos)}

    # -- network thread
    def _filter_line(self):
        with self.lock:
            calls = list(self.watch)
            self.watch_dirty = False
        if not calls:
            return None
        return "#filter b/" + "/".join(c + "*" for c in calls)

    def run(self, stop):
        backoff = 10
        while not stop.is_set():
            try:
                self._session(stop)
                backoff = 10
            except (OSError, ValueError):
                pass
            self.connected = False
            self.save()
            stop.wait(backoff)
            backoff = min(300, backoff * 2)

    def _session(self, stop):
        s = socket.create_connection((self.host, self.port), timeout=20)
        try:
            s.settimeout(5)
            buf = b""
            login = "user %s pass -1 vers ysfradio 1.0\r\n" % self.login_call
            s.sendall(login.encode("ascii"))
            self.connected = True
            last_filter = None
            last_save = last_ping = last_rx = time.time()
            while not stop.is_set():
                now = time.time()
                if self.watch_dirty or last_filter is None:
                    fl = self._filter_line()
                    if fl and fl != last_filter:
                        s.sendall((fl + "\r\n").encode("ascii"))
                        last_filter = fl
                    elif fl is None:
                        last_filter = ""
                if now - last_ping > 300:
                    s.sendall(b"#ysfradio keepalive\r\n")
                    last_ping = now
                if now - last_save > 120:
                    self.save()
                    last_save = now
                if now - last_rx > 180:
                    return              # server went quiet: reconnect
                try:
                    chunk = s.recv(4096)
                except socket.timeout:
                    continue
                if not chunk:
                    return
                last_rx = time.time()
                buf += chunk
                if len(buf) > 65536:
                    buf = buf[-4096:]
                while b"\n" in buf:
                    raw, buf = buf.split(b"\n", 1)
                    r = parse_aprs_packet(raw.decode("latin-1").rstrip("\r"))
                    if r:
                        self.update(*r)
        finally:
            try:
                s.close()
            except OSError:
                pass


# ---------------------------------------------------------------- helpers for tests

def haversine_km(a, b):
    la1, lo1, la2, lo2 = map(math.radians, (a[0], a[1], b[0], b[1]))
    h = math.sin((la2 - la1) / 2) ** 2 + math.cos(la1) * math.cos(la2) * math.sin((lo2 - lo1) / 2) ** 2
    return 2 * 6371.0 * math.asin(math.sqrt(h))
