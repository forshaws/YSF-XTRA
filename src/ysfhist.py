#!/usr/bin/env python3
"""ysfhist: a long memory for the dashboard's HEARD tab, built from the reflector's own log files.

Python 3.5+, standard library only. Read-only on the logs. It keeps two things in one small file
(<state-dir>/history.json):

  ALL     every callsign ever seen on the reflector, with the first and last time, how many times it
          was heard transmitting, and whether it was ever heard (T) or only connected (L). A callsign
          counts when it transmits, is listed as linked, or is logged as added.
  ACTIVE  the number of transmissions in each hour of the day for the last 30 days, shown as an
          average per hour.

On the first start (no history file) every log file in the log folder is read, oldest first, so the
lists are full at once. On later starts only the logs from the last recorded day on are read again,
and the newest log is then followed as it grows. Times are the log's own times, as written.

  python3 ysfhist.py --log-dir /var/log/YSFReflector          # print what it finds
"""

import argparse
import glob
import json
import os
import re
import sys
import threading
from datetime import datetime, timedelta

import ysfdash

ADD_RE = re.compile(r"^Adding (\S+)")
WATCHDOG = "Network watchdog has expired"
CALL_OK = re.compile(r"^[A-Z0-9][A-Z0-9/_-]{1,11}$")
KEEP_DAYS = 60
WINDOW_DAYS = 30
MAX_CALLS = 20000
SAVE_EVERY = 30.0
MAX_TX_S = 900.0


class History(object):
    def __init__(self, path, log_dir, prefix="YSFReflector"):
        self.path = path
        self.log_dir = log_dir
        self.prefix = prefix
        self.lock = threading.Lock()
        self.calls = {}                  # call -> [first, last, tx count, "T" or "L"]
        self.act = {}                    # call or gateway -> time it last transmitted
        self.act_new = False             # the file had no 'act' yet: scan the old logs once for it
        self.days = {}                   # "YYYY-MM-DD" -> [[24 tx counts], [24 seconds on air]]
        self.hw = ""                     # time of the last log line taken in
        self.at_hw = set()               # transmissions already counted that ended in that second
        self.loaded = False
        self.building = True
        self.dirty = False
        self.failed = False

    # ---- file ----
    def load(self):
        try:
            with open(self.path, "r") as f:
                a = json.load(f)
            calls, days = a["calls"], a["days"]
            if not isinstance(calls, dict) or not isinstance(days, dict):
                raise ValueError("shape")
            for k, v in calls.items():
                if not (isinstance(v, list) and len(v) == 4):
                    raise ValueError("call")
            for k, v in days.items():
                if not (isinstance(v, list) and len(v) == 2 and len(v[0]) == 24 and len(v[1]) == 24):
                    raise ValueError("day")
            self.calls, self.days, self.hw = calls, days, str(a.get("hw", ""))
            self.at_hw = set(str(x) for x in a.get("at", []))
            act = a.get("act")
            if isinstance(act, dict):
                self.act = dict((str(k), str(v)) for k, v in act.items() if CALL_OK.match(str(k)))
            else:
                self.act_new = True
            self.loaded = True
        except (OSError, ValueError, KeyError, TypeError):
            self.calls, self.days, self.hw, self.loaded = {}, {}, "", False
            self.at_hw = set()
            self.act = {}

    def save(self):
        with self.lock:
            if not self.dirty:
                return
            if len(self.calls) > MAX_CALLS:
                keep = sorted(self.calls.items(), key=lambda kv: kv[1][1], reverse=True)[:MAX_CALLS]
                self.calls = dict(keep)
            for d in sorted(self.days)[:-KEEP_DAYS]:
                del self.days[d]
            if len(self.act) > MAX_CALLS:
                keep = sorted(self.act.items(), key=lambda kv: kv[1], reverse=True)[:MAX_CALLS]
                self.act = dict(keep)
            data = json.dumps({"v": 1, "hw": self.hw, "at": sorted(self.at_hw), "calls": self.calls,
                               "days": self.days, "act": self.act})
            self.dirty = False
        try:
            tmp = self.path + ".tmp"
            fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o644)
            with os.fdopen(fd, "w") as f:
                f.write(data)
            os.replace(tmp, self.path)
        except OSError as e:
            self.dirty = True
            if not self.failed:
                self.failed = True
                sys.stderr.write("ysfhist: cannot write %s: %s (history is kept in memory only)\n" % (self.path, e))
                sys.stderr.flush()

    # ---- recording ----
    def _seen(self, ts, call, how, n=0):
        call = call.upper()
        if not CALL_OK.match(call):
            return
        with self.lock:
            r = self.calls.get(call)
            if r is None:
                r = self.calls[call] = [ts, ts, 0, how]
            if ts < r[0]:
                r[0] = ts
            if ts > r[1]:
                r[1] = ts
            if how == "T":
                r[3] = "T"
            r[2] += n
            self.dirty = True

    def _act(self, ts, *calls):
        with self.lock:
            for c in calls:
                c = c.upper()
                if CALL_OK.match(c) and ts > self.act.get(c, ""):
                    self.act[c] = ts
                    self.dirty = True

    def last_act(self, call):
        with self.lock:
            return self.act.get(call.upper())

    def _tx(self, start, src, dur):
        self._seen(start, src, "T", 1)
        day, hour = start[:10], int(start[11:13])
        with self.lock:
            d = self.days.get(day)
            if d is None:
                d = self.days[day] = [[0] * 24, [0] * 24]
            d[0][hour] += 1
            d[1][hour] += int(dur)
            self.dirty = True

    def _finish(self, st, ts, skip):
        cur = st["cur"]
        st["cur"] = None
        if cur is None or skip:
            return
        try:
            dur = (datetime.strptime(ts, ysfdash.TS_FMT) - datetime.strptime(cur[0], ysfdash.TS_FMT)).total_seconds()
        except ValueError:
            dur = 0
        key = "%s|%s|%s" % (cur[0], cur[1], ts)
        if ts == self.hw:
            if key in self.at_hw:
                return
            self.at_hw.add(key)
        self._tx(cur[0], cur[1], max(0.0, min(MAX_TX_S, dur)))

    def feed(self, line, st):
        m = ysfdash.LINE_RE.match(line.rstrip("\r\n"))
        if not m:
            return
        ts, rest = m.group(1), m.group(2)
        skip = ts < self.hw
        if ts > self.hw:
            self.hw = ts
            self.at_hw = set()
        self._line(ts, rest, st, skip)

    def _line(self, ts, rest, st, skip):
        if st["pend"]:
            lm = ysfdash.LINK_RE.match(rest)
            if lm:
                if not skip:
                    self._seen(ts, ysfdash.clean(lm.group(1)).strip(), "L")
                return
            st["pend"] = False
        if rest.startswith(ysfdash.LINK_HDR):
            st["pend"] = True
            return
        rm = ysfdash.RX_RE.match(rest)
        if rm:
            self._finish(st, ts, skip)
            src, gw = ysfdash.clean(rm.group(1)).strip(), ysfdash.clean(rm.group(3)).strip()
            st["cur"] = (ts, src)
            self._act(ts, src, gw)
            return
        if rest.startswith(ysfdash.EOT) or rest.startswith(WATCHDOG):
            self._finish(st, ts, skip)
            return
        am = ADD_RE.match(rest)
        if am and not skip:
            self._seen(ts, ysfdash.clean(am.group(1)).strip(), "L")

    # ---- reading the logs ----
    def _files(self):
        return sorted(glob.glob(os.path.join(self.log_dir, self.prefix + "-*.log")))

    @staticmethod
    def _new_state():
        return {"pend": False, "cur": None}

    def _read_file(self, path):
        st = self._new_state()
        try:
            with open(path, "r", errors="replace") as f:
                for line in f:
                    self.feed(line, st)
        except (OSError, IOError):
            pass

    def _scan_act(self, path):
        try:
            with open(path, "r", errors="replace") as f:
                for line in f:
                    m = ysfdash.LINE_RE.match(line.rstrip("\r\n"))
                    if not m:
                        continue
                    rm = ysfdash.RX_RE.match(m.group(2))
                    if rm:
                        self._act(m.group(1), ysfdash.clean(rm.group(1)).strip(), ysfdash.clean(rm.group(3)).strip())
        except (OSError, IOError):
            pass

    def run(self, stop):
        """Read the old logs, then follow the newest one. Meant for a daemon thread."""
        self.load()
        files = self._files()
        if self.loaded and self.act_new:               # history from before 'act' existed: fill it from the logs once
            for p in files:
                self._scan_act(p)
            self.act_new = False
        newest = files[-1] if files else None
        first_day = self.hw[:10]
        for p in files[:-1]:
            day = os.path.basename(p)[len(self.prefix) + 1:-4]
            if not self.loaded or day >= first_day:
                self._read_file(p)
        self.building = False
        self.save()
        f, path, buf, st, last_save = None, None, "", None, 0.0
        import time
        while not stop.is_set():
            try:
                if f is not None:
                    chunk = f.read()
                    if chunk:
                        buf += chunk
                        parts = buf.split("\n")
                        buf = parts.pop()
                        for ln in parts:
                            self.feed(ln, st)
                files = self._files()
                if files and files[-1] != path:
                    if f is not None:
                        f.close()
                    f = open(files[-1], "r", errors="replace")
                    path, buf, st = files[-1], "", self._new_state()
            except (OSError, IOError):
                if f is not None:
                    try:
                        f.close()
                    except OSError:
                        pass
                f, path = None, None
            if time.time() - last_save >= SAVE_EVERY:
                last_save = time.time()
                self.save()
            stop.wait(1.0)
        self.save()

    # ---- what the dashboard asks for ----
    def snapshot(self, limit=3000):
        with self.lock:
            rows = sorted(self.calls.items(), key=lambda kv: kv[1][1], reverse=True)
            allc = [[k, v[1], v[0], v[2], v[3]] for k, v in rows[:limit]]
            total = len(rows)
            if self.hw:
                try:
                    end = datetime.strptime(self.hw[:10], "%Y-%m-%d")
                except ValueError:
                    end = datetime.now()
            else:
                end = datetime.now()
            keys = [(end - timedelta(days=i)).strftime("%Y-%m-%d") for i in range(WINDOW_DAYS)]
            have = [self.days[k] for k in keys if k in self.days]
            nd = len(have)
            hours = [round(sum(d[0][h] for d in have) / float(nd), 2) if nd else 0 for h in range(24)]
            secs = [int(sum(d[1][h] for d in have) / float(nd)) if nd else 0 for h in range(24)]
            return {"all": allc, "total": total, "hours": hours, "secs": secs, "days": nd,
                    "building": self.building}


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--log-dir", default="/var/log/YSFReflector")
    ap.add_argument("--prefix", default="YSFReflector")
    ap.add_argument("--file", default="", help="history file to write (default: none, print only)")
    a = ap.parse_args()
    h = History(a.file or os.devnull, a.log_dir, a.prefix)
    h.hw = ""
    for p in h._files():
        h._read_file(p)
    s = h.snapshot(20)
    print("callsigns:", s["total"], " days:", s["days"])
    for r in s["all"]:
        print("%-12s last %s  first %s  tx %d %s" % (r[0], r[1], r[2], r[3], r[4]))
    print("avg tx per hour:", " ".join("%.1f" % x for x in s["hours"]))
    return 0


if __name__ == "__main__":
    sys.exit(main())
