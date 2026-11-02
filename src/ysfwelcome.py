"""ysfwelcome: decides when a station gets a welcome message from the reflector.

Used by ysfmsgd.py, which feeds it what it hears on the reflector's UDP port. It sends nothing
itself: a welcome is queued (call, radio ID, number of stations linked) and ysfnews.py sends it.

A welcome is queued for a station when:
  * it links to the reflector (a poll from a callsign not heard for 60 seconds) and nothing
    was received from any station for the last `quiet` seconds, or
  * it sends the text message CHECK to everyone (answered at any time).

A link carries no radio ID, only the callsign. The radio ID is learned from what a radio
transmits (voice and text messages). A station that linked after a quiet period is welcomed
at once if a radio ID of that callsign was heard before, and in any case when a radio with an
ID not yet welcomed transmits within `wait` seconds of the link. Each callsign + radio ID
pair is welcomed once per link, so a second radio under one callsign gets its own welcome.
Radio IDs are kept in memory only.

Python 3.5+, stdlib only.
"""

import re
import time

RID_RE = re.compile(r"^[A-Za-z0-9]{5}$")

LINK_TIMEOUT = 60            # seconds without a poll before a station counts as unlinked
HANDSHAKE = 5                # seconds: a station's data this close to its own link is link data
ID_KEEP = 7 * 86400          # forget learned radio IDs after this long


class Welcome(object):
    def __init__(self, queue, quiet=300, wait=600, cooldown=3600, check_cooldown=30,
                 self_call="", now=None):
        self.queue = queue                   # ysfmsgstore.NewsQueue
        self.quiet = quiet
        self.wait = wait
        self.cooldown = cooldown
        self.check_cooldown = check_cooldown
        self.self_call = self_call.upper()
        now = time.time() if now is None else now
        self.start = now                     # a start-up counts as traffic: no welcomes in the first `quiet` seconds
        self.last_traffic = now              # latest traffic from any station
        self.by_gw = {}                      # gateway callsign -> [latest traffic, latest traffic more than HANDSHAKE s before that]
        self.log = None                      # optional function(text) for one line per link
        self.polls = {}                      # gateway callsign -> time of its last poll
        self.sess = {}                       # gateway callsign -> {"t": link time, "done": set of (call, rid)}
        self.ids = {}                        # gateway callsign -> {(call, rid): time heard}
        self.last = {}                       # (kind, call, rid) -> time queued

    # -- what ysfmsgd hears ------------------------------------------------------------------

    def mine(self, call, local):
        """True for the reflector's own sender (ysfnews.py), which must not trigger a welcome."""
        return bool(local and self.self_call and call.upper() == self.self_call)

    def poll(self, call, unlink, local, now):
        """A YSFP (keep-alive / link) or YSFU (unlink) packet from a station."""
        call = call.strip().upper()
        if not call or self.mine(call, local):
            return
        if unlink:
            self.polls.pop(call, None)
            self.sess.pop(call, None)
            return
        prev = self.polls.get(call)
        self.polls[call] = now
        if prev is None or now - prev > LINK_TIMEOUT:
            self.linked(call, now)

    def last_traffic_before(self, gw, now):
        """Time of the latest traffic from any station, or the start-up time. Data from `gw`
        itself in the HANDSHAKE seconds before `now` is its own link data and does not count."""
        out = [self.start]
        for g, (t, prev) in self.by_gw.items():
            out.append(prev if g == gw and now - t <= HANDSHAKE else t)
        return max(out)

    def linked(self, gw, now):
        self.sess.pop(gw, None)
        ago = now - self.last_traffic_before(gw, now)
        if ago < self.quiet:
            if self.log:
                self.log("link from %s: not welcomed, other traffic %d s ago (needs %d s quiet)" % (gw, ago, self.quiet))
            return
        s = self.sess[gw] = {"t": now, "done": set()}
        if self.log:
            self.log("link from %s: quiet for %d s, welcome %s" %
                     (gw, ago, "to a known radio ID" if self.ids.get(gw) else "waits for its first transmission"))
        known = self.ids.get(gw)
        if known:
            (call, rid), t = max(known.items(), key=lambda kv: kv[1])
            if now - t < ID_KEEP:
                self.welcome(gw, call, rid, now)

    def traffic(self, gw, src, local, now):
        """Any data from a station. Resets the quiet timer."""
        if self.mine(gw, local) or self.mine(src, local):
            return
        self.last_traffic = now
        gw = gw.strip().upper()
        if len(self.by_gw) > 256:
            self.by_gw = {g: v for g, v in self.by_gw.items() if now - v[0] < self.quiet}
        t = self.by_gw.get(gw)
        self.by_gw[gw] = [now, t[1] if t is None or now - t[0] <= HANDSHAKE else t[0]] if t else [now, 0]

    def heard_id(self, gw, src, rid, local, now):
        """The radio ID of a transmitting radio, from its voice or its text message."""
        gw, src = gw.strip().upper(), src.strip().upper()
        if self.mine(gw, local) or self.mine(src, local) or not RID_RE.match(rid):
            return
        ids = self.ids.setdefault(gw, {})
        ids[(src, rid)] = now
        if len(self.ids) > 512:
            self.ids = {g: d for g, d in self.ids.items() if any(now - t < ID_KEEP for t in d.values())}
        s = self.sess.get(gw)
        if s and now - s["t"] <= self.wait:
            self.welcome(gw, src, rid, now)

    def check(self, gw, src, rid, local, now):
        """The text message CHECK sent to everyone: answered like a welcome. A station that
        checks in is not welcomed again for the same link."""
        gw, src = gw.strip().upper(), src.strip().upper()
        if self.mine(src, local) or not RID_RE.match(rid):
            return False
        s = self.sess.get(gw)
        if s is not None:
            s["done"].add((src, rid))
        return self.enqueue("check", src, rid, now, self.check_cooldown)

    # -- queueing ----------------------------------------------------------------------------

    def welcome(self, gw, call, rid, now):
        s = self.sess.get(gw)
        if s is None or (call, rid) in s["done"]:
            return False
        s["done"].add((call, rid))
        return self.enqueue("join", call, rid, now, self.cooldown)

    def enqueue(self, kind, call, rid, now, cooldown):
        k = (kind, call, rid)
        if now - self.last.get(k, -1e9) < cooldown:
            return False
        if len(self.last) > 1024:
            self.last = {a: t for a, t in self.last.items() if now - t < self.cooldown}
        n = self.count(now)
        if self.queue.add(call, rid, now, {"kind": kind, "n": n}) is None:
            return False
        self.last[k] = now
        return True

    def count(self, now):
        """Stations linked now: callsigns that polled in the last LINK_TIMEOUT seconds."""
        for g in [g for g, t in self.polls.items() if now - t > LINK_TIMEOUT]:
            del self.polls[g]
        return len(self.polls)
