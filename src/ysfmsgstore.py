"""ysfmsgstore: tiny shared store for decoded broadcast text messages.

Written by ysfmsgd.py (capture service), read by ysfradio.py (dashboard).
The file is JSON: {"v": 1, "next": <int>, "msgs": [{"id", "t", "call", "text", "k"}, ...]}
oldest first. Python 3.5+, standard library only.
"""

import fcntl
import json
import os
import re
import threading
import time

MAX_MSGS = 20
CALL_RE = re.compile(r"^[A-Z0-9/-]{3,12}$")


def clean_text(s):
    """Printable ASCII only, collapsed trailing space, at most 80 characters."""
    s = "".join(c for c in s if 32 <= ord(c) <= 126)
    return s.rstrip()[:80]


def _valid(m):
    try:
        return (isinstance(m["id"], int) and not isinstance(m["id"], bool) and
                isinstance(m["t"], (int, float)) and
                isinstance(m["call"], str) and CALL_RE.match(m["call"]) is not None and
                isinstance(m["text"], str))
    except (KeyError, TypeError):
        return False


def _load(path):
    try:
        with open(path, "r") as f:
            d = json.load(f)
        msgs = [m for m in d.get("msgs", []) if _valid(m)]
        nxt = d.get("next", 1)
        if not isinstance(nxt, int) or nxt < 1:
            nxt = 1
        return msgs, max(nxt, max([m["id"] for m in msgs] or [0]) + 1)
    except (OSError, ValueError, AttributeError):
        return [], 1


class _FileLock(object):
    """Exclusive lock on <path>.lock, so ysfmsgd, the dashboard and ysfboard.py never
    rewrite the same file at the same moment. If the lock file cannot be opened the
    write goes ahead unlocked."""

    def __init__(self, path):
        self.path = path + ".lock"
        self.f = None

    def __enter__(self):
        try:
            self.f = open(self.path, "a")
            fcntl.flock(self.f, fcntl.LOCK_EX)
        except OSError:
            self.f = None
        return self

    def __exit__(self, *a):
        if self.f is not None:
            try:
                fcntl.flock(self.f, fcntl.LOCK_UN)
            finally:
                self.f.close()
        return False


def _write(path, nxt, msgs):
    tmp = path + ".tmp"
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o640)
    with os.fdopen(fd, "w") as f:
        json.dump({"v": 1, "next": nxt, "msgs": msgs}, f)
    os.replace(tmp, path)


class MsgFile(object):
    """Writer side."""

    def __init__(self, path, keep_days=7):
        self.path = path
        self.keep = keep_days * 86400
        self.lock = threading.Lock()

    def add(self, call, text, key, now=None):
        """Store a message. Returns its id, or None if it was a duplicate."""
        now = time.time() if now is None else now
        text = clean_text(text)
        if not text or not CALL_RE.match(call):
            return None
        with self.lock, _FileLock(self.path):
            msgs, nxt = _load(self.path)
            if any(m.get("k") == key for m in msgs[-MAX_MSGS:]):
                return None
            msgs.append({"id": nxt, "t": round(now, 1), "call": call, "text": text, "k": key})
            msgs = [m for m in msgs if now - m["t"] <= self.keep][-MAX_MSGS:]
            _write(self.path, nxt + 1, msgs)
            return nxt

    def delete(self, mid):
        """Remove the item with this id. Returns True if it existed."""
        with self.lock, _FileLock(self.path):
            msgs, nxt = _load(self.path)
            keep = [m for m in msgs if m["id"] != mid]
            if len(keep) == len(msgs):
                return False
            _write(self.path, nxt, keep)
            return True


class MsgReader(object):
    """Reader side: re-reads the file only when it changes."""

    def __init__(self, path):
        self.path = path
        self.sig = None
        self.msgs = []
        self.lock = threading.Lock()

    def get(self):
        """Newest first, at most MAX_MSGS, public fields only."""
        with self.lock:
            try:
                st = os.stat(self.path)
                sig = (st.st_mtime, st.st_size)
            except OSError:
                self.sig, self.msgs = None, []
                return []
            if sig != self.sig:
                msgs, _ = _load(self.path)
                self.msgs = [{"id": m["id"], "t": m["t"], "call": m["call"],
                              "text": clean_text(m["text"])} for m in msgs[-MAX_MSGS:]][::-1]
                self.sig = sig
            return list(self.msgs)


POS_MAX = 500


class PosFile(object):
    """Writer side for radio-reported positions: latest position per base callsign.
    File: {"v": 1, "pos": {"N0CALL": [lat, lon, ts, "msg"], ...}}"""

    def __init__(self, path, keep_days=30):
        self.path = path
        self.keep = keep_days * 86400
        self.lock = threading.Lock()
        self.last = {}                                   # call -> (lat, lon, ts) last written

    def update(self, call, lat, lon, src, now=None):
        now = time.time() if now is None else now
        base = call.upper().split("/")[0].split("-")[0]
        if not BASE_RE.match(base) or not (-90 <= lat <= 90 and -180 <= lon <= 180):
            return False
        if lat == 0 and lon == 0:
            return False                                  # null island: no fix
        with self.lock:
            prev = self.last.get(base)
            if prev and prev[0] == lat and prev[1] == lon and now - prev[2] < 60:
                return False                              # same position, just seen
            if prev and src == "voice" and now - prev[2] < 10:
                return False                              # voice repeats it every second
            try:
                with open(self.path, "r") as f:
                    pos = json.load(f).get("pos", {})
            except (OSError, ValueError, AttributeError):
                pos = {}
            pos = {c: v for c, v in pos.items() if _pos_ok(c, v) and now - v[2] <= self.keep}
            pos[base] = [round(lat, 5), round(lon, 5), round(now, 1), src]
            if len(pos) > POS_MAX:
                for c in sorted(pos, key=lambda c: pos[c][2])[:len(pos) - POS_MAX]:
                    del pos[c]
            tmp = self.path + ".tmp"
            fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o640)
            with os.fdopen(fd, "w") as f:
                json.dump({"v": 1, "pos": pos}, f)
            os.replace(tmp, self.path)
            self.last[base] = (lat, lon, now)
            return True


BASE_RE = re.compile(r"^[A-Z0-9]{3,8}$")


def _pos_ok(c, v):
    try:
        return (BASE_RE.match(c) is not None and len(v) >= 3 and
                -90 <= float(v[0]) <= 90 and -180 <= float(v[1]) <= 180 and float(v[2]) > 0)
    except (TypeError, ValueError):
        return False


class PosReader(object):
    """Reader side: re-reads the file only when it changes."""

    def __init__(self, path):
        self.path = path
        self.sig = None
        self.pos = {}
        self.lock = threading.Lock()

    def get(self):
        """{base call: (lat, lon, ts)}"""
        with self.lock:
            try:
                st = os.stat(self.path)
                sig = (st.st_mtime, st.st_size)
            except OSError:
                self.sig, self.pos = None, {}
                return {}
            if sig != self.sig:
                try:
                    with open(self.path, "r") as f:
                        raw = json.load(f).get("pos", {})
                    self.pos = {c: (float(v[0]), float(v[1]), float(v[2]))
                                for c, v in raw.items() if _pos_ok(c, v)}
                except (OSError, ValueError, AttributeError):
                    self.pos = {}
                self.sig = sig
            return dict(self.pos)


NEWS_MAX = 50
RID_RE = re.compile(r"^[A-Za-z0-9]{5}$")


class NewsQueue(object):
    """News Station download requests, newest last.
    Written by ysfmsgd.py (add), read by ysfnews.py (read_after).
    File: {"v": 1, "next": <int>, "req": [{"id", "t", "call", "rid"}, ...]}"""

    def __init__(self, path):
        self.path = path
        self.lock = threading.Lock()

    @staticmethod
    def load(path):
        try:
            with open(path, "r") as f:
                d = json.load(f)
            req = [r for r in d.get("req", []) if NewsQueue._ok(r)]
            nxt = d.get("next", 1)
            if not isinstance(nxt, int) or nxt < 1:
                nxt = 1
            return req, max(nxt, max([r["id"] for r in req] or [0]) + 1)
        except (OSError, ValueError, AttributeError):
            return [], 1

    @staticmethod
    def _ok(r):
        try:
            return (isinstance(r["id"], int) and not isinstance(r["id"], bool) and
                    isinstance(r["t"], (int, float)) and
                    isinstance(r["call"], str) and CALL_RE.match(r["call"]) is not None and
                    isinstance(r["rid"], str) and RID_RE.match(r["rid"]) is not None)
        except (KeyError, TypeError):
            return False

    def add(self, call, rid, now=None, extra=None):
        """Queue a request. Returns its id, or None if the call or radio ID is not valid.
        extra: more fields kept with the request (welcome messages: kind and station count)."""
        now = time.time() if now is None else now
        if not CALL_RE.match(call) or not RID_RE.match(rid):
            return None
        with self.lock:
            req, nxt = self.load(self.path)
            r = {"id": nxt, "t": round(now, 1), "call": call, "rid": rid}
            for k, v in (extra or {}).items():
                if k not in r:
                    r[k] = v
            req.append(r)
            req = req[-NEWS_MAX:]
            tmp = self.path + ".tmp"
            fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o640)
            with os.fdopen(fd, "w") as f:
                json.dump({"v": 1, "next": nxt + 1, "req": req}, f)
            os.replace(tmp, self.path)
            return nxt

    @staticmethod
    def read_after(path, after_id):
        """Requests with an id above after_id, oldest first."""
        req, _ = NewsQueue.load(path)
        return [r for r in req if r["id"] > after_id]
