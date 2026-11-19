#!/usr/bin/env python3
"""ysfmon: the server side of the dashboard's MON button (listen to the reflector in the browser).

Python 3.5+, standard library only. ysfmsgd.py sends the vocoder bits of each voice packet to a local
socket (<state-dir>/monitor.sock); this module receives them and hands each packet on, unchanged, to
every open /api/monitor stream. It decodes nothing: ysfmon.js in the browser makes the sound, so the
cost of a listener is about 350 bytes a second.

A stream is a plain HTTP response that stays open. It carries records of one kind each:
  00             keep-alive, every few seconds (a dead connection shows up when it is written)
  01 + 35 bytes  one voice packet: five frames of 49 vocoder bits, 7 bytes each
  02             the talker changed or the transmission ended: the listener starts its decoder afresh
"""

import os
import socket
import threading
import time

MAX_LISTENERS = 200
PER_IP = 3
RING = 120
KEEPALIVE = 2.0


class Monitor(object):
    def __init__(self, sock_path, maxn):
        self.sock_path = sock_path
        self.maxn = maxn
        self.cond = threading.Condition()
        self.seq = 0
        self.ring = []                       # (seq, record)
        self.active = []                     # listener ids, oldest first
        self.ips = {}                        # id -> ip
        self.nextid = 1

    # ---- the packets in ----
    def publish(self, rec):
        with self.cond:
            self.seq += 1
            self.ring.append((self.seq, rec))
            del self.ring[:-RING]
            self.cond.notify_all()

    def run(self, stop):
        """Receive datagrams from ysfmsgd. Meant for a daemon thread."""
        s = socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM)
        try:
            try:
                os.unlink(self.sock_path)
            except OSError:
                pass
            s.bind(self.sock_path)
            os.chmod(self.sock_path, 0o660)
        except OSError as e:
            import sys
            sys.stderr.write("ysfmon: cannot listen on %s: %s (MON is off)\n" % (self.sock_path, e))
            sys.stderr.flush()
            return
        s.settimeout(1.0)
        while not stop.is_set():
            try:
                d = s.recv(64)
            except socket.timeout:
                continue
            except OSError:
                break
            if d[:1] == b"\x01" and len(d) == 36:
                self.publish(d)
            elif d[:1] == b"\x02":
                self.publish(b"\x02")
        try:
            os.unlink(self.sock_path)
        except OSError:
            pass

    # ---- the listeners ----
    def count(self):
        with self.cond:
            return len(self.active)

    def set_max(self, n):
        with self.cond:
            self.maxn = n
            del self.active[max(0, n):]      # the newest listeners above the limit are dropped
            self.cond.notify_all()

    def join(self, ip):
        """A new listener's id, or None when the limit is reached (or this address has too many)."""
        with self.cond:
            if len(self.active) >= self.maxn or len(self.active) >= MAX_LISTENERS:
                return None
            if sum(1 for i in self.active if self.ips.get(i) == ip) >= PER_IP:
                return None
            lid = self.nextid
            self.nextid += 1
            self.active.append(lid)
            self.ips[lid] = ip
            return lid

    def leave(self, lid):
        with self.cond:
            if lid in self.active:
                self.active.remove(lid)
            self.ips.pop(lid, None)

    def records(self, lid):
        """Yield byte strings for one listener until it is dropped. New listeners get no backlog."""
        with self.cond:
            seq = self.seq
        while True:
            out = []
            with self.cond:
                if lid not in self.active:
                    return
                if self.seq == seq:
                    self.cond.wait(KEEPALIVE)
                if lid not in self.active:
                    return
                if self.seq - seq > RING:    # fell behind: skip ahead, restart the decoder
                    out.append(b"\x02")
                for n, rec in self.ring:
                    if n > seq:
                        out.append(rec)
                seq = self.seq
            yield b"".join(out) if out else b"\x00"
