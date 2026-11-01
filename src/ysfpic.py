"""ysfpic: receive and keep pictures sent from a Yaesu camera microphone.

Used by ysfmsgd.py (receive) and ysfradio.py (show). Python 3.5+, standard library only.

A picture arrives as several YSF data streams from one callsign (see
docs/C4FM-PICTURE-FORMAT.md): info records Gh_ and Nd_, data chunks Nb_ numbered
consecutively (1024 JPEG bytes each, the last one shorter), and an end record Ne_ that
carries the 16-bit sum of the JPEG bytes. Only senders on the whitelist are kept.
"""

import collections
import json
import os
import re
import sys
import threading
import time

from ysfmsgstore import CALL_RE, _FileLock

RID_RE = re.compile(r"^[A-Za-z0-9]{5}$")
MAX_PICS = 20
MAX_BYTES = 65536
MAX_DIM = 640
SESSION_GAP = 120                                      # seconds allowed between streams of one picture
ZERO40 = b"\0" * 40


def sum16(b):
    return sum(b) & 0xFFFF


def jpeg_info(b):
    """(width, height) of a baseline/progressive JPEG with SOI first and EOI last, else None."""
    if len(b) < 128 or b[:2] != b"\xff\xd8" or b[-2:] != b"\xff\xd9":
        return None
    i = 2
    while i + 4 <= len(b):
        if b[i] != 0xFF:
            return None
        m = b[i + 1]
        if m in (0xC0, 0xC1, 0xC2):
            h = (b[i + 5] << 8) | b[i + 6]
            w = (b[i + 7] << 8) | b[i + 8]
            return (w, h) if 0 < w <= MAX_DIM and 0 < h <= MAX_DIM else None
        if m == 0xDA:
            return None
        i += 2 + ((b[i + 2] << 8) | b[i + 3])
    return None


def _atomic(path, obj):
    tmp = path + ".tmp"
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o640)
    with os.fdopen(fd, "w") as f:
        json.dump(obj, f)
    os.replace(tmp, path)


def _read_json(path):
    try:
        with open(path, "r") as f:
            d = json.load(f)
        return d if isinstance(d, dict) else {}
    except (OSError, ValueError):
        return {}


class PicFile(object):
    """Index file <dir>/pictures.json plus one <dir>/<id>.jpg per picture."""

    def __init__(self, folder, keep_days=7, max_pics=MAX_PICS):
        self.dir = folder
        self.index = os.path.join(folder, "pictures.json")
        self.keep = keep_days * 86400
        self.max = max_pics
        self.lock = threading.Lock()

    def _load(self):
        d = _read_json(self.index)
        pics = []
        for p in d.get("pics", []):
            try:
                if (isinstance(p["id"], int) and not isinstance(p["id"], bool) and
                        isinstance(p["t"], (int, float)) and CALL_RE.match(p["call"]) and
                        isinstance(p["ok"], bool) and isinstance(p["w"], int) and
                        isinstance(p["h"], int) and isinstance(p["size"], int)):
                    pics.append(p)
            except (KeyError, TypeError):
                pass
        nxt = d.get("next", 1)
        if not isinstance(nxt, int) or nxt < 1:
            nxt = 1
        return pics, max(nxt, max([p["id"] for p in pics] or [0]) + 1)

    def _name(self, pid):
        return os.path.join(self.dir, "%d.jpg" % pid)

    def _save(self, pics, nxt, drop):
        _atomic(self.index, {"v": 1, "next": nxt, "pics": pics})
        for pid in drop:
            try:
                os.unlink(self._name(pid))
            except OSError:
                pass

    def add(self, call, jpeg, ok, w, h, now=None):
        now = time.time() if now is None else now
        if not CALL_RE.match(call):
            return None
        with self.lock, _FileLock(self.index):
            os.makedirs(self.dir, exist_ok=True)
            pics, nxt = self._load()
            fd = os.open(self._name(nxt), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o640)
            with os.fdopen(fd, "wb") as f:
                f.write(jpeg)
            pics.append({"id": nxt, "t": round(now, 1), "call": call, "ok": bool(ok),
                         "w": w, "h": h, "size": len(jpeg)})
            keep = [p for p in pics if now - p["t"] <= self.keep][-self.max:]
            gone = set(p["id"] for p in pics) - set(p["id"] for p in keep)
            self._save(keep, nxt + 1, gone)
            return nxt

    def delete(self, pid):
        with self.lock, _FileLock(self.index):
            pics, nxt = self._load()
            keep = [p for p in pics if p["id"] != pid]
            if len(keep) == len(pics):
                return False
            self._save(keep, nxt, [pid])
            return True

    def get(self):
        """Newest first, public fields only."""
        pics, _ = self._load()
        return [{"id": p["id"], "t": p["t"], "call": p["call"], "ok": p["ok"],
                 "w": p["w"], "h": p["h"], "size": p["size"]} for p in pics][::-1]

    def read(self, pid):
        """JPEG bytes of a listed picture, else None."""
        if not any(p["id"] == pid for p in self._load()[0]):
            return None
        try:
            with open(self._name(pid), "rb") as f:
                return f.read(MAX_BYTES + 1)
        except OSError:
            return None


class Allow(object):
    """Whitelist of senders: [{"call": "M0RWV", "id": "EB4XU"}]; id "" = any radio."""

    def __init__(self, path):
        self.path = path
        self.sig = None
        self.items = []
        self.lock = threading.Lock()

    def _load(self):
        try:
            st = os.stat(self.path)
            sig = (st.st_mtime, st.st_size)
        except OSError:
            self.sig, self.items = None, []
            return
        if sig == self.sig:
            return
        out = []
        for e in _read_json(self.path).get("allow", []):
            try:
                c, i = e["call"], e.get("id", "")
                if CALL_RE.match(c) and (i == "" or RID_RE.match(i)):
                    out.append({"call": c, "id": i})
            except (KeyError, TypeError, AttributeError):
                pass
        self.items, self.sig = out, sig

    def list(self):
        with self.lock:
            self._load()
            return [dict(e) for e in self.items]

    def allowed(self, call, rid):
        with self.lock:
            self._load()
            return any(e["call"] == call and (e["id"] == "" or e["id"] == rid) for e in self.items)

    def _write(self, items):
        _atomic(self.path, {"v": 1, "allow": items})
        self.sig = None

    def add(self, call, rid=""):
        call = call.upper()
        if not CALL_RE.match(call) or not (rid == "" or RID_RE.match(rid)):
            return False
        with self.lock, _FileLock(self.path):
            self.sig = None
            self._load()
            items = [e for e in self.items if not (e["call"] == call and e["id"] == rid)]
            if len(items) >= 200:
                return False
            items.append({"call": call, "id": rid})
            self._write(items)
        return True

    def remove(self, call, rid=""):
        with self.lock, _FileLock(self.path):
            self.sig = None
            self._load()
            items = [e for e in self.items if not (e["call"] == call and e["id"] == rid)]
            if len(items) == len(self.items):
                return False
            self._write(items)
        return True


def _blocks(frames):
    """frames: [(ctr, FN, FT, 40 bytes)] of one stream in arrival order. Missing frames are
    found from gaps in the counter (it steps by 2) and come back as None.
    Returns [(FT, {FN: bytes or None})], one entry per 8-frame (or shorter) record block."""
    seq = []
    prev_idx, cur_fn, cur_ft = 0, None, None
    seen = set()
    for ctr, fn, ft, data in frames:
        idx = ctr >> 1
        if ctr in seen or idx <= prev_idx:
            continue
        seen.add(ctr)
        for _ in range(max(0, min(idx - prev_idx - 1, 64))):
            if cur_fn is None:
                cur_fn, cur_ft = 0, ft
            else:
                cur_fn = cur_fn + 1 if cur_fn < cur_ft else 0
            seq.append((cur_fn, cur_ft, None))
        seq.append((fn, ft, data))
        cur_fn, cur_ft, prev_idx = fn, ft, idx
    blocks = []
    for fn, ft, data in seq:
        if fn == 0 or not blocks:
            blocks.append((ft, {}))
        blocks[-1][1][fn] = data
    return blocks


class PicRx(object):
    """Collects the streams of one picture per sender and stores it when the end record arrives."""

    def __init__(self, store, allow, per_hour=5, max_bytes=MAX_BYTES, log=None):
        self.store = store
        self.allow = allow
        self.per_hour = per_hour
        self.max_bytes = max_bytes
        self.sess = {}                                   # (gw, src) -> session
        self.sent = collections.defaultdict(collections.deque)
        self.warned = {}
        self.log = log or (lambda s: (sys.stderr.write(s + "\n"), sys.stderr.flush()))

    @staticmethod
    def is_control(rec):
        return rec[61:64] in (b"Gh_", b"Nd_", b"Nb_", b"Ne_")

    def _chunk_lost(self, miss, base, hdr, length):
        """True if a missing frame hid data bytes of this chunk (base = data bytes already held)."""
        for k in miss:
            lo = base + max(0, k * 40 - hdr)
            if k * 40 + 40 > hdr and lo < length:
                return True
        return False

    def stream_end(self, gw, src, frames, now):
        blocks = _blocks(frames)
        if not blocks:
            return None
        key = (gw, src)
        s = self.sess.get(key)
        if s is not None and now - s["t"] > SESSION_GAP:
            self.log("ysfmsgd: picture session from %s dropped after %d s gap, %d chunks discarded"
                     % (src, now - s["t"], len(s["chunks"])))
            s = self.sess[key] = None
        cur = None
        result = None
        first = True
        for ft, fr in blocks:
            n = ft + 1
            buf = b"".join(fr.get(k) or ZERO40 for k in range(n))
            miss = [k for k in range(n) if fr.get(k) is None]
            tag, kind = buf[61:64], buf[64:66]
            if first and cur is None and ({0, 1} & set(miss)):
                if s is None:
                    s = self.sess[key] = {"t": now, "chunks": {}, "ok": None, "rid": "", "hdrlost": 0, "streams": 0}
                s["hdrlost"] += 1
            first = False
            if tag == b"Nd_" and buf[83:84] == b"P" and not ({0, 1, 2} & set(miss)):
                # description record: starts a new picture and gives its size in bytes
                size = int.from_bytes(buf[88:92], "big")
                s = self.sess[key] = {"t": now, "chunks": {}, "ok": None, "rid": "", "hdrlost": 0, "streams": 0,
                                      "size": size if 0 < size <= self.max_bytes else 0}
                continue
            if kind == b"3P" and tag in (b"Nb_", b"Ne_") and not ({0, 1} & set(miss)):
                cnt = int.from_bytes(buf[66:68], "big")
                if tag == b"Nb_":
                    if cur is not None:
                        s["chunks"][cur["cnt"]] = self._close(cur)
                    rid = buf[55:60].decode("latin-1")
                    if s is None or (s["chunks"] and cnt <= min(s["chunks"])):
                        s = self.sess[key] = {"t": now, "chunks": {}, "ok": None, "rid": rid, "hdrlost": 0, "streams": 0}
                    if s["ok"] is None:
                        s["ok"] = self.allow.allowed(src, rid)
                        if not s["ok"] and now - self.warned.get(src, 0) > 300:
                            self.warned[src] = now
                            self.log("ysfmsgd: picture from %s (%s) ignored: not on the whitelist" % (src, rid))
                    length = int.from_bytes(buf[68:70], "big") or 1024
                    length = min(length, 1024)
                    cur = {"cnt": cnt, "len": length, "data": bytearray(buf[70:]), "lost": False}
                    if self._chunk_lost(miss, 0, 70, length):
                        cur["lost"] = True
                else:
                    if cur is not None:
                        s["chunks"][cur["cnt"]] = self._close(cur)
                        cur = None
                    if s is not None:
                        result = self._finish(key, s, cnt, int.from_bytes(buf[70:72], "big"), src, now)
                continue
            if cur is not None:                           # continuation block of the open chunk
                base = len(cur["data"])
                if self._chunk_lost(miss, base, 61, cur["len"]):
                    cur["lost"] = True
                cur["data"] += buf[61:]
        if cur is not None and s is not None:
            s["chunks"][cur["cnt"]] = self._close(cur)
        if s is not None:
            s["t"] = now
        return result

    @staticmethod
    def _close(c):
        data = bytes(c["data"][:c["len"]])
        lost = c["lost"] or len(data) < c["len"]
        return (data + b"\0" * (c["len"] - len(data)), lost)

    def _finish(self, key, s, endcnt, total, src, now):
        self.sess.pop(key, None)
        if not s["ok"]:
            return None
        # chunk numbers do not always start at 1: take the run from the first one received up to the end record
        size = s.get("size", 0)
        if size:                                    # the description record says how many chunks there are
            first = endcnt - (size + 1023) // 1024
        else:
            first = min(s["chunks"]) if s["chunks"] else endcnt
        want = list(range(first, endcnt))
        if not want or first < 0 or any(c not in s["chunks"] for c in want):
            self.log("ysfmsgd: picture from %s incomplete, not stored: needs chunks %s, received %s, "
                     "streams with lost header frames %d" % (
                         src, "%d-%d" % (first, endcnt - 1) if want else "?", sorted(s["chunks"]) or "none",
                         s.get("hdrlost", 0)))
            return None
        jpeg = b"".join(s["chunks"][c][0] for c in want)
        if len(jpeg) > self.max_bytes:
            return None
        lost = any(s["chunks"][c][1] for c in want)
        ok = (not lost) and sum16(jpeg) == total
        info = jpeg_info(jpeg)
        if info is None:
            self.log("ysfmsgd: picture from %s is not a valid JPEG, not stored (%d bytes, record says %d, chunks %s, "
                     "starts %s, ends %s)" % (src, len(jpeg), size, want, jpeg[:4].hex(), jpeg[-2:].hex()))
            return None
        dq = self.sent[src]
        while dq and now - dq[0] > 3600:
            dq.popleft()
        if len(dq) >= self.per_hour:
            self.log("ysfmsgd: picture from %s over the hourly limit, not stored" % src)
            return None
        pid = self.store.add(src, jpeg, ok, info[0], info[1], now)
        if pid is not None:
            dq.append(now)
            self.log("ysfmsgd: stored picture %d from %s (%dx%d, %d bytes%s)" %
                     (pid, src, info[0], info[1], len(jpeg), "" if ok else ", DAMAGED"))
        return pid
