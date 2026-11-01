#!/usr/bin/env python3
"""ysfmsgd: watch the YSF reflector's UDP port, decode BROADCAST text messages
and keep the last few for the dashboard (ysfradio.py --messages).

Needs ysfdec.py and ysfmsgstore.py in the same folder. Python 3.5+, stdlib only.
Linux only: it uses a packet socket (CAP_NET_RAW) with a kernel filter for
"UDP to <port>", so only reflector traffic is ever read. It never transmits.

Only broadcast messages (addressed to everyone) with a valid checksum are kept.
Messages addressed to a callsign are discarded unread. Only the sender callsign,
the time received and the text are stored. Old entries are dropped after
--keep-days days; at most 20 are kept.

A WIRES-X News Station download request (--news-out) is queued as the requester's callsign
and radio ID only; the separate ysfnews.py service answers it.

A News Station upload (--board-out, --board-room-id) is a text message addressed to the
room ID. It is public by design: the sender callsign, time and text are kept (at most 20,
--board-days days) and ysfnews.py sends them to the next radios that download.

A message sent with the radio's position attached also updates that callsign's
latest position (--positions-out), the same position the radio sends to everyone
on the reflector. Only the latest position per callsign is kept.
"""

import argparse
import collections
import ctypes
import os
import signal
import socket
import struct
import sys
import time

import ysfdec as y
import ysfmsgstore
import ysfpic
import ysfpos

SO_ATTACH_FILTER = 26
PKT_OUTGOING = 4
ALL = "**********"


def bpf_program(port):
    """Classic BPF for: ip and udp and dst port <port> and not a fragment."""
    return [
        (0x28, 0, 0, 12),          # 0 ldh [12]
        (0x15, 0, 8, 0x0800),      # 1 jeq ip, else drop
        (0x30, 0, 0, 23),          # 2 ldb [23]
        (0x15, 0, 6, 17),          # 3 jeq udp, else drop
        (0x28, 0, 0, 20),          # 4 ldh [20]
        (0x45, 4, 0, 0x1FFF),      # 5 jset fragment, drop
        (0xB1, 0, 0, 14),          # 6 ldxb 4*([14]&0xf)
        (0x48, 0, 0, 16),          # 7 ldh [x+16]
        (0x15, 0, 1, port),        # 8 jeq port, else drop
        (0x06, 0, 0, 262144),      # 9 accept
        (0x06, 0, 0, 0),           # 10 drop
    ]


def attach_filter(sock, port):
    prog = bpf_program(port)
    arr = (ctypes.c_ubyte * (8 * len(prog)))()
    raw = b"".join(struct.pack("HBBI", c, jt, jf, k) for c, jt, jf, k in prog)
    ctypes.memmove(arr, raw, len(raw))

    class SockFprog(ctypes.Structure):
        _fields_ = [("len", ctypes.c_ushort), ("filter", ctypes.c_void_p)]

    fp = SockFprog(len(prog), ctypes.cast(arr, ctypes.c_void_p))
    libc = ctypes.CDLL(None, use_errno=True)
    r = libc.setsockopt(sock.fileno(), socket.SOL_SOCKET, SO_ATTACH_FILTER,
                        ctypes.byref(fp), ctypes.sizeof(fp))
    if r != 0:
        raise OSError(ctypes.get_errno(), "SO_ATTACH_FILTER failed")


def udp_payload(frame, port):
    """Ethernet frame -> UDP payload (IPv4/UDP to `port`), else None."""
    if len(frame) < 42 or struct.unpack("!H", frame[12:14])[0] != 0x0800:
        return None
    ip = frame[14:]
    if ip[0] >> 4 != 4 or ip[9] != 17:
        return None
    ihl = (ip[0] & 0x0F) * 4
    if len(ip) < ihl + 8:
        return None
    dport, ulen = struct.unpack("!HH", ip[ihl + 2:ihl + 6])
    if dport != port or ulen < 8:
        return None
    return ip[ihl + 8:ihl + ulen]


def field(b):
    return b.decode("ascii", "replace").replace("\x00", " ").strip()


class Decoder(object):
    """Turns YSFD packets into stored messages."""

    def __init__(self, store, per_hour=10, pos=None, news=None, board=None, board_room="", board_per_hour=3, pics=None):
        self.store = store
        self.pics = pics                    # ysfpic.PicRx or None
        self.pos = pos
        self.news = news
        self.news_last = {}
        self.board = board                  # MsgFile for News Station uploads
        self.board_room = board_room        # room ID the radios upload to
        self.board_per_hour = board_per_hour
        self.up_sent = collections.defaultdict(collections.deque)
        self.asm = {}                       # (gw, src) -> {"t": last seen, "segs": {fn: bytes}}
        self.fich_cache = {}
        self.vseg = {}                      # (gw, src) -> FN6 bytes waiting for FN7
        self.vlog = {}
        self.sent = collections.defaultdict(collections.deque)
        self.per_hour = per_hour

    def fich(self, raw25):
        hit = self.fich_cache.get(raw25)
        if hit is not None:
            return hit[0]
        bits = y.bytes_to_bits(raw25)
        coded = y.from_dibits(y.deinterleave(y.to_dibits(bits), 5))
        dec, _ = y.viterbi(coded, "A")
        d = []
        for i in range(0, 96, 24):          # Golay words are systematic: data bits come first
            d += dec[i:i + 12]
        ok = y.int_bits(y.crc16(d[:32])) == d[32:48]
        res = y.fich_fields(d[:32]) if ok else None
        if len(self.fich_cache) > 4096:
            self.fich_cache.clear()
        self.fich_cache[raw25] = (res,)
        return res

    def packet(self, p, now=None):
        """Feed one UDP payload. Returns a stored message id when one completes."""
        if len(p) != 155 or p[:4] != b"YSFD":
            return None
        now = time.time() if now is None else now
        gw, src = field(p[4:14]), field(p[14:24])
        ctr, frame = p[34], p[35:155]
        if frame[:5] != y.SYNC:
            return None
        f = self.fich(frame[5:30])
        if f and f["DT"] == 2 and self.pos is not None:
            return self.voice(gw, src, f, frame, now)
        if not f or f["DT"] != 1:
            return None
        key = (gw, src)
        a = self.asm.get(key)
        if a is None or now - a["t"] > 3 or f["FI"] == 0:
            a = self.asm[key] = {"t": now, "segs": {}, "frames": []}
        a["t"] = now
        if f["FI"] == 1 and (f["FN"] not in a["segs"] or self.pics is not None):
            bits = y.bytes_to_bits(frame)[240:960]
            parts = []
            for lo in (0, 72):
                g = []
                for i in range(5):
                    g += bits[i * 144 + lo:i * 144 + lo + 72]
                r = y.unit_decode(g, 9, 160)
                if not r["ok"]:
                    return None
                parts.append(y.bits_to_bytes(r["data"]))
            a["segs"].setdefault(f["FN"], parts[0] + parts[1])
            if self.pics is not None:
                a["frames"].append((ctr, f["FN"], f["FT"], parts[0] + parts[1]))
        if ctr & 1:                          # end flag: terminator frame
            del self.asm[key]
            if self.pics is not None and a["frames"]:
                pid = self.pics.stream_end(gw, src, a["frames"], now)
                if pid is not None:
                    return None
            return self.finish(src, a["segs"], now)
        self.gc(now)
        return None

    def voice(self, gw, src, f, frame, now):
        """Voice/data mode 2: the radio's position rides in frames FN6 and FN7 of the
        8-frame cycle (10 data bytes each). Joined they are: 4 bytes, 13 bytes of Mic-E
        (marker '3', latitude, longitude, speed, course), 0x20, 0x03, checksum."""
        if f["FI"] != 1 or f["FN"] not in (6, 7):
            return None
        bits = y.bytes_to_bits(frame)[240:960]
        g = []
        for i in range(5):
            g += bits[i * 144:i * 144 + 40]
        r = y.unit_decode(g, 5, 80)
        if not r["ok"]:
            return None
        key = (gw, src)
        v = self.vseg.get(key)
        if f["FN"] == 6:
            self.vseg[key] = {"t": now, "fn6": y.bits_to_bytes(r["data"])}
            if len(self.vseg) > 64:
                for k in [k for k, a in self.vseg.items() if now - a["t"] > 5]:
                    del self.vseg[k]
            return None
        if not v or now - v["t"] > 2:
            return None
        del self.vseg[key]
        b = v["fn6"] + y.bits_to_bytes(r["data"])
        if len(b) != 20 or b[4] != 0x33 or b[18] != 0x03 or (sum(b[:19]) & 0xFF) != b[19]:
            return None
        try:
            p = ysfpos.decode_block((b[4:17] + b"[/    ")[:19])
        except ValueError:
            return None
        if self.pos.update(src, p["lat"], p["lon"], "voice", now):
            if now - self.vlog.get(src, 0) > 60:
                sys.stderr.write("ysfmsgd: position updated for %s (from voice)\n" % src)
                sys.stderr.flush()
            self.vlog[src] = now
        return None

    def position(self, src, rec, now):
        """A GPS-attached message carries the sender's position (APRS Mic-E block)."""
        if self.pos is None or rec[61:63] != b"Gf" or len(rec) < 83:
            return
        try:
            p = ysfpos.decode_block(rec[64:83])
        except ValueError:
            return
        if self.pos.update(src, p["lat"], p["lon"], "msg", now):
            sys.stderr.write("ysfmsgd: position updated for %s (from message)\n" % src)
            sys.stderr.flush()

    def news_request(self, src, rec, now):
        """A WIRES-X News Station download (message) request: command ']l_', payload '3' (FT5D) or '0' (FT3D)
        + room ID + station ID + '120' ..., ETX, checksum. Only the requester's callsign
        and radio ID are queued."""
        if self.news is None or rec[61:64] != b"]l_" or not rec[65:75].isdigit():
            return
        etx = rec.find(b"\x03", 64)
        if etx < 0 or etx + 1 >= len(rec) or rec[etx + 1] not in (sum(rec[22:etx]) & 0xFF, sum(rec[13:etx]) & 0xFF):
            return                                    # checksum starts at byte 22 (FT5D) or 13 (FT3D)
        if rec[64:65] not in (b"3", b"0"):             # 3 = FT5D, 0 = FT3D: message downloads
            sys.stderr.write("ysfmsgd: news request type %r not answered\n" % rec[64:65].decode("latin-1"))
            sys.stderr.flush()
            return
        if rec[5:10] != rec[55:60]:
            return
        rid, call = field(rec[5:10]), field(rec[10:20])
        if now - self.news_last.get(rid, 0) < 5:
            return
        if len(self.news_last) > 256:
            self.news_last.clear()
        self.news_last[rid] = now
        if self.news.add(call, rid, now) is not None:
            sys.stderr.write("ysfmsgd: news request from %s (payload %r)\n" %
                             (call, rec[64:etx].decode("latin-1")))
            sys.stderr.flush()

    def upload(self, src, pr, rec, now):
        """A News Station upload: a text message addressed to the room ID. Public by
        design, so the callsign, time and text are kept for the next downloaders."""
        dq = self.up_sent[src]
        while dq and now - dq[0] > 3600:
            dq.popleft()
        if len(dq) >= self.board_per_hour:
            return None
        o = 18 if rec[61:63] == b"Gf" else 0
        k = "%s|%s|%s" % (src, rec[65 + o:70 + o].decode("latin-1"), rec[71 + o:81 + o].decode("latin-1"))
        mid = self.board.add(src, pr["text"], k, now)
        if mid is not None:
            dq.append(now)
            sys.stderr.write("ysfmsgd: news upload from %s\n" % src)
            sys.stderr.flush()
        return None

    def gc(self, now):
        for k in [k for k, a in self.asm.items() if now - a["t"] > 5]:
            del self.asm[k]

    def finish(self, src, segs, now):
        n = max(segs) + 1 if segs else 0
        if n in (2, 3) and all(i in segs for i in range(n)):     # control records, not text
            self.news_request(src, b"".join(segs[i] for i in range(n)), now)
            return None
        if n < 3 or any(i not in segs for i in range(n)):
            return None
        rec = b"".join(segs[i] for i in range(n))
        if ysfpic.PicRx.is_control(rec):
            return None                      # picture transfer records, handled by ysfpic
        pr = y.parse_record(rec)
        if not pr or not pr["ok"]:
            return None
        self.position(src, rec, now)
        if self.board is not None and self.board_room and pr["to_id"] == self.board_room:
            return self.upload(src, pr, rec, now)
        if pr["to_call"] != ALL or pr["to_id"]:
            return None                       # directed message: never stored
        dq = self.sent[src]
        while dq and now - dq[0] > 3600:
            dq.popleft()
        if len(dq) >= self.per_hour:
            return None
        o = 18 if rec[61:63] == b"Gf" else 0         # a GPS block moves the fields after it
        k = "%s|%s|%s" % (src, rec[65 + o:70 + o].decode("latin-1"), rec[71 + o:81 + o].decode("latin-1"))
        mid = self.store.add(src, pr["text"], k, now)
        if mid is not None:
            dq.append(now)
        return mid


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--port", type=int, default=42002, help="reflector UDP port (default 42002)")
    ap.add_argument("--out", default="/var/lib/ysfradio/messages.json")
    ap.add_argument("--keep-days", type=int, default=7)
    ap.add_argument("--per-hour", type=int, default=10, help="max stored per callsign per hour")
    ap.add_argument("--positions-out", default="/var/lib/ysfradio/positions.json",
                    help="file for positions the radios transmit (empty = do not record positions)")
    ap.add_argument("--positions-days", type=int, default=30, help="forget positions older than this")
    ap.add_argument("--news-out", default="",
                    help="queue file for News Station requests, read by ysfnews.py (empty = off)")
    ap.add_argument("--board-out", default="",
                    help="file for News Station uploads, read by ysfnews.py (empty = uploads are discarded)")
    ap.add_argument("--board-room-id", default="",
                    help="room ID the radios upload to (5 characters, the destination ID of the upload)")
    ap.add_argument("--board-per-hour", type=int, default=3, help="most uploads kept per callsign per hour")
    ap.add_argument("--board-days", type=int, default=30, help="drop uploads older than this")
    ap.add_argument("--pictures-dir", default="",
                    help="folder for pictures from camera microphones (empty = pictures are not kept)")
    ap.add_argument("--pictures-allow", default="/var/lib/ysfradio/picture-allow.json",
                    help="whitelist file; only these callsigns' pictures are kept (edited in the dashboard admin)")
    ap.add_argument("--pictures-per-hour", type=int, default=5, help="most pictures kept per callsign per hour")
    ap.add_argument("--pictures-days", type=int, default=7, help="drop pictures older than this")
    a = ap.parse_args()
    if not hasattr(socket, "AF_PACKET"):
        raise SystemExit("needs Linux")
    try:
        s = socket.socket(socket.AF_PACKET, socket.SOCK_RAW, socket.ntohs(3))
    except PermissionError:
        raise SystemExit("needs CAP_NET_RAW (run as root or via the systemd unit)")
    attach_filter(s, a.port)
    pos = ysfmsgstore.PosFile(a.positions_out, max(1, a.positions_days)) if a.positions_out else None
    news = ysfmsgstore.NewsQueue(a.news_out) if a.news_out else None
    board = ysfmsgstore.MsgFile(a.board_out, max(1, a.board_days)) if a.board_out and a.board_room_id else None
    pics = None
    if a.pictures_dir:
        pics = ysfpic.PicRx(ysfpic.PicFile(a.pictures_dir, max(1, a.pictures_days)),
                            ysfpic.Allow(a.pictures_allow), max(1, a.pictures_per_hour))
    dec = Decoder(ysfmsgstore.MsgFile(a.out, max(1, a.keep_days)), a.per_hour, pos, news,
                  board, a.board_room_id, a.board_per_hour, pics)
    signal.signal(signal.SIGTERM, lambda *x: sys.exit(0))
    sys.stderr.write("ysfmsgd: watching UDP %d, writing %s\n" % (a.port, a.out))
    sys.stderr.flush()
    while True:
        frame, addr = s.recvfrom(65535)
        if addr[2] == PKT_OUTGOING:
            continue
        p = udp_payload(frame, a.port)
        if p is None:
            continue
        try:
            mid = dec.packet(p)
        except Exception as e:               # never die on a malformed frame
            sys.stderr.write("ysfmsgd: %s\n" % e)
            continue
        if mid is not None:
            sys.stderr.write("ysfmsgd: stored message %d\n" % mid)
            sys.stderr.flush()


if __name__ == "__main__":
    main()
