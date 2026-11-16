#!/usr/bin/env python3
"""ysflevel: a level (VU) meter for YSF voice, read from the vocoder gain inside the frames.

Linux, Python 3.5+, standard library only. It only listens: it never sends anything and never
touches the reflector. It decodes no audio: for each 20 ms vocoder frame of a V/D type 2 voice
stream it reads the gain value the sending radio's vocoder puts in the frame, which follows how loud
the speech is. This is a loudness meter, not a modulation (deviation) meter: the network frames do
not carry the radio signal itself. The scale is relative (see --floor and --full).

Only V/D type 2 voice (FICH DT=2, the usual mode of a camera-microphone radio) is read. Voice full
rate (DT=3) uses a different vocoder and is shown as "no level".

  1. A capture made with ysfcap.py, shown as a bar every 100 ms:
       python3 ysflevel.py --replay cap_VOICE.jsonl

  2. Live (needs root, listens on the reflector port):
       sudo python3 ysflevel.py --live --port 42002

Sources: the voice channel layout (26 x 4 interleave, whitening pattern, triple bits) is the one
published in G4KLX's MMDVMHost (YSFPayload.cpp, GPL-2.0), written here from scratch; the gain table
and bit positions are those of the mbelib AMBE+2 decoder (ISC licence). Check that this fits the
licence you put on the repository.
"""

import argparse
import json
import re
import socket
import struct
import sys
import time

import ysfdec as y

PKT_OUTGOING = 4
# the bit layout of a V/D type 2 voice channel: 104 bits, de-interleaved (26 x 4), un-whitened,
# then 27 bit triples (each bit sent three times) and 22 plain bits: 49 vocoder bits
WHITE = y.bytes_to_bits(bytes([0x93, 0xD7, 0x51, 0x21, 0x9C, 0x2F, 0x6C, 0xD0, 0xEF, 0x0F, 0xF8, 0x3D,
                               0xF1, 0x73, 0x20, 0x94, 0xED, 0x1E, 0x7C, 0xD8]))[:104]
TABLE = [(i // 26) + 4 * (i % 26) for i in range(104)]
# change of gain for each value of the 5 bit gain field, in log2 units of amplitude (AMBE+2)
DELTA_GAIN = [-2.0, -0.67, 0.297941, 0.663728, 1.036829, 1.438136, 1.890077, 2.227970,
              2.478289, 2.667544, 2.793619, 2.893261, 3.020630, 3.138586, 3.237579, 3.322570,
              3.432367, 3.571863, 3.696650, 3.814917, 3.920932, 4.022503, 4.123569, 4.228291,
              4.370569, 4.543700, 4.707695, 4.848879, 5.056757, 5.326468, 5.777581, 6.874496]
DB_PER_UNIT = 6.02            # one unit of gain doubles the amplitude
NEW_STREAM_S = 1.0            # a gap this long starts a new transmission (gain history restarts)


def vocoder_bits(bits, j):
    """The 49 vocoder bits of voice channel j (0-4) of a frame given as 960 bits."""
    raw = bits[240 + 144 * j + 40:240 + 144 * j + 144]
    v = [a ^ b for a, b in zip([raw[TABLE[i]] for i in range(104)], WHITE)]
    return [v[3 * t + 1] for t in range(27)] + v[81:103]


class Levels(object):
    """Turns voice frames into levels, keeping the gain history of each talker."""

    def __init__(self):
        self.state = {}                                   # key -> [gamma, time of last frame]

    def frame(self, key, frame, now):
        """120-byte frame -> list of five (dB, kind) tuples, or None if it is not V/D type 2 voice.
        kind is 'voice', 'silence', 'erasure' or 'tone'."""
        if len(frame) != 120:
            return None
        bits = y.bytes_to_bits(frame)
        data, ok, _verr, _gerr = y.fich_decode(bits[40:240], "A")
        if not ok:
            return None
        f = y.fich_fields(data)
        if f["DT"] != 2 or f["FI"] != 1:
            return None
        return self.channels(key, bits, now)

    def channels(self, key, bits, now):
        """The five (dB, kind) tuples of a frame already known to be V/D type 2 voice (960 bits)."""
        st = self.state.get(key)
        if st is None or now - st[1] > NEW_STREAM_S:
            st = [0.0, now]
        out = []
        for j in range(5):
            d = vocoder_bits(bits, j)
            b0 = d[0] << 6 | d[1] << 5 | d[2] << 4 | d[3] << 3 | d[37] << 2 | d[38] << 1 | d[39]
            b2 = d[8] << 4 | d[9] << 3 | d[10] << 2 | d[11] << 1 | d[36]
            if 120 <= b0 <= 123:
                out.append((None, "erasure"))
                continue
            if b0 >= 126:
                out.append((None, "tone"))
                continue
            st[0] = DELTA_GAIN[b2] + 0.5 * st[0]
            out.append((st[0] * DB_PER_UNIT, "silence" if b0 >= 124 else "voice"))
        st[1] = now
        if len(self.state) > 64:
            self.state.clear()
        self.state[key] = st
        return out


class Meter(object):
    """VU ballistics on a 0..1 scale: quick to rise, slow to fall, with a peak marker."""

    def __init__(self, floor, full, fall=0.06):
        self.floor, self.full, self.fall = floor, full, fall
        self.value = 0.0
        self.peak = 0.0
        self.peak_t = 0.0

    def push(self, db, now):
        x = 0.0 if db is None else min(1.0, max(0.0, (db - self.floor) / float(self.full - self.floor)))
        self.value = x if x > self.value else self.value + (x - self.value) * self.fall
        if self.value >= self.peak or now - self.peak_t > 1.0:
            self.peak, self.peak_t = self.value, now
        return self.value


def bar(value, peak, width=40, colour=False):
    n = int(round(value * width))
    p = min(width - 1, int(round(peak * width)))
    cells = []
    for i in range(width):
        c = "#" if i < n else ("|" if i == p else "-")
        if colour and c == "#":
            c = ("\033[32m#\033[0m" if i < width * 0.6 else "\033[33m#\033[0m" if i < width * 0.85
                 else "\033[31m#\033[0m")
        cells.append(c)
    return "".join(cells)


# ---------------------------------------------------------------- input


def parse_frame(frame, port):
    """Ethernet frame -> UDP payload bytes if it is IPv4/UDP to `port`, else None."""
    if len(frame) < 14 + 20 + 8 or struct.unpack("!H", frame[12:14])[0] != 0x0800:
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


def text(b):
    return b.decode("ascii", "replace").replace("\x00", " ").strip()


def replay(path):
    with open(path) as f:
        for line in f:
            if not line.startswith("{"):
                continue
            try:
                r = json.loads(line, strict=False)
                data = bytes.fromhex(r["data"])
                if len(data) == 120:
                    yield r["gw"], r["src"], data, float(r["t"])
            except (ValueError, KeyError, TypeError):
                continue


def live(port):
    if not hasattr(socket, "AF_PACKET"):
        raise SystemExit("needs Linux")
    try:
        s = socket.socket(socket.AF_PACKET, socket.SOCK_RAW, socket.ntohs(3))
    except PermissionError:
        raise SystemExit("run with sudo")
    while True:
        frame, addr = s.recvfrom(65535)
        if addr[2] == PKT_OUTGOING:
            continue
        p = parse_frame(frame, port)
        if p is not None and len(p) == 155 and p[:4] == b"YSFD":
            yield text(p[4:14]), text(p[14:24]), p[35:155], time.time()


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    src = ap.add_mutually_exclusive_group(required=True)
    src.add_argument("--replay", metavar="FILE", help="a ysfcap.py capture (JSON lines)")
    src.add_argument("--live", action="store_true", help="listen on the reflector port (needs root)")
    ap.add_argument("--port", type=int, default=42002, help="reflector UDP port (default 42002)")
    ap.add_argument("--call", help="show only this source callsign")
    ap.add_argument("--floor", type=float, default=0.0, help="level shown as an empty bar, dB (default 0)")
    ap.add_argument("--full", type=float, default=40.0, help="level shown as a full bar, dB (default 40)")
    ap.add_argument("--colour", action="store_true", help="colour the bar (live)")
    ap.add_argument("--numbers", action="store_true", help="also print the dB value of every 20 ms frame")
    a = ap.parse_args()
    if a.call and not re.match(r"^[A-Za-z0-9/]{3,10}$", a.call):
        raise SystemExit("--call must be a callsign")
    want = a.call.upper() if a.call else None

    lv = Levels()
    meters = {}
    last = {}
    stream = replay(a.replay) if a.replay else live(a.port)
    t0 = None
    try:
        for gw, call, data, t in stream:
            if want and call.upper() != want:
                continue
            key = (gw, call)
            res = lv.frame(key, data, t)
            if res is None:
                continue
            m = meters.get(key)
            if m is None:
                m = meters[key] = Meter(a.floor, a.full)
            for k, (db, kind) in enumerate(res):
                v = m.push(db, t + 0.02 * k)
            if t0 is None:
                t0 = t
            dbs = [db for db, _ in res if db is not None]
            avg = sum(dbs) / len(dbs) if dbs else float("nan")
            line = "%-10s [%s] %5.1f dB" % (call, bar(m.value, m.peak, 40, a.colour), avg)
            if a.numbers:
                line += "  " + " ".join("%5.1f" % db if db is not None else "  n/a" for db, _ in res)
            if a.replay or a.numbers:
                print("%6.2fs %s" % (t - t0, line))
            else:
                sys.stdout.write("\r" + line + "   ")
                sys.stdout.flush()
    except KeyboardInterrupt:
        pass
    if a.live:
        print()
    return 0


if __name__ == "__main__":
    sys.exit(main())
