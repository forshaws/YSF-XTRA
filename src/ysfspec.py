#!/usr/bin/env python3
"""ysfspec: a voice spectrum (0 to 4 kHz) for YSF V/D type 2 voice, read from the vocoder parameters
inside the frames, for the dashboard's SCOPE tab.

Python 3.5+, standard library only. It listens to nothing itself: ysfmsgd.py hands it each 20 ms
vocoder frame. It decodes no audio. For each frame it rebuilds the spectral envelope the AMBE+2
vocoder transmits (the loudness of each voice harmonic) and returns it as NB bands of 200 Hz.
The values are relative decibels of the voice, not of the radio signal. Voice full rate (DT=3)
uses a different vocoder and is not read.

The decoding steps (pitch, gain, PRBA and HOC vectors, inverse DCT, prediction from the previous
frame) follow the AMBE+2 decoder in mbelib (ISC licence); the tables are in ysfspec_tab.py.

  python3 ysfspec.py --replay cap_VOICE.jsonl        # one line of NB numbers per 20 ms frame
"""

import argparse
import json
import math
import sys

import ysflevel as lv
import ysfspec_tab as T

NB = 20                       # bands over 0 to 4 kHz
BAND_HZ = 4000.0 / NB
DB = 6.0206                   # one unit of log2 amplitude
NEW_STREAM_S = 1.0

# cosine tables, built once
_COS_PRBA = [[(1.0 if m == 1 else 2.0) * math.cos(math.pi * (m - 1) * (i - 0.5) / 8.0)
              for m in range(1, 9)] for i in range(1, 9)]
_COS_BLOCK = {}


def _cos_block(ji):
    t = _COS_BLOCK.get(ji)
    if t is None:
        t = [[(1.0 if k == 1 else 2.0) * math.cos(math.pi * (k - 1) * (j - 0.5) / ji)
              for k in range(1, ji + 1)] for j in range(1, ji + 1)]
        _COS_BLOCK[ji] = t
    return t


class Params(object):
    """What the next frame is predicted from (mbelib's prev_mp)."""
    __slots__ = ("L", "gamma", "log2")

    def __init__(self):
        self.L = 30
        self.gamma = 0.0
        self.log2 = [0.0] * 60


def _bits(d, idx):
    v = 0
    for i in idx:
        v = (v << 1) | d[i]
    return v


def decode(d, prev):
    """49 vocoder bits -> (status, f0, L, gamma, log2Ml list, Vl list).
    status: 0 voice or silence frame, 2 erasure, 3 tone (mbelib's numbers)."""
    b0 = d[0] << 6 | d[1] << 5 | d[2] << 4 | d[3] << 3 | d[37] << 2 | d[38] << 1 | d[39]
    silence = False
    if 120 <= b0 <= 123:
        return 2, 0, 0, 0, None, None
    if b0 >= 126:
        return 3, 0, 0, 0, None, None
    if b0 >= 124:
        silence = True
        f0 = 1.0 / 32
        L = 14
        w0 = 2 * math.pi / 32
    else:
        f0 = T.AmbeW0table[b0]
        L = T.AmbeLtable[b0]
        w0 = f0 * 2 * math.pi
    unvc = 0.2046 / math.sqrt(w0)
    b1 = d[4] << 4 | d[5] << 3 | d[6] << 2 | d[7] << 1 | d[35]
    vl = [0] * 58
    if not silence:
        vuv = T.AmbeVuv[b1]
        for l in range(1, L + 1):
            vl[l] = vuv[min(7, int(l * 16.0 * f0))]
    b2 = d[8] << 4 | d[9] << 3 | d[10] << 2 | d[11] << 1 | d[36]
    gamma = lv.DELTA_GAIN[b2] + 0.5 * prev.gamma

    b3 = _bits(d, (12, 13, 14, 15, 16, 17, 18, 19, 40))
    b4 = _bits(d, (20, 21, 22, 23, 41, 42, 43))
    g = [0.0, 0.0] + list(T.AmbePRBA24[b3]) + list(T.AmbePRBA58[b4])      # Gm[1..8]; Gm[1] = 0
    r = [sum(g[m] * _COS_PRBA[i][m - 1] for m in range(1, 9)) for i in range(8)]      # Ri[1..8]
    rc = 1.0 / (2.0 * math.sqrt(2.0))
    cik = [None] * 5
    for blk in range(4):
        a, b = r[2 * blk], r[2 * blk + 1]
        cik[blk + 1] = [0.0, 0.5 * (a + b), rc * (a - b)] + [0.0] * 16

    b5 = d[24] << 4 | d[25] << 3 | d[26] << 2 | d[27] << 1 | d[44]
    b6 = d[28] << 3 | d[29] << 2 | d[30] << 1 | d[45]
    b7 = d[31] << 3 | d[32] << 2 | d[33] << 1 | d[46]
    b8 = d[34] << 2 | d[47] << 1 | d[48]
    ji = T.AmbeLmprbl[L]
    hoc = (T.AmbeHOCb5[b5], T.AmbeHOCb6[b6], T.AmbeHOCb7[b7], T.AmbeHOCb8[b8])
    tl = [0.0]
    for i in range(1, 5):
        n = ji[i - 1]
        c = cik[i]
        for k in range(3, n + 1):
            c[k] = hoc[i - 1][k - 3] if k <= 6 else 0.0
        table = _cos_block(n) if n else []
        for j in range(n):
            row = table[j]
            tl.append(sum(row[k] * c[k + 1] for k in range(n)))

    # predict from the previous frame's log magnitudes
    pl = prev.log2[:]
    if L > prev.L:
        for l in range(prev.L + 1, L + 1):
            pl[l] = pl[prev.L]
    pl[0] = pl[1]
    s43 = 0.0
    fl = []
    for l in range(1, L + 1):
        fk = float(prev.L) / L * l
        ik = int(fk)
        dl = fk - ik
        fl.append((ik, dl))
        s43 += (1.0 - dl) * pl[ik] + dl * pl[ik + 1]
    s43 *= 0.65 / L
    s42 = sum(tl[1:L + 1]) / L
    big = gamma - 0.5 * math.log(L, 2) - s42
    log2 = [0.0] * 60
    for l in range(1, L + 1):
        ik, dl = fl[l - 1]
        log2[l] = tl[l] + 0.65 * (1.0 - dl) * pl[ik] + 0.65 * dl * pl[ik + 1] - s43 + big
    return 0, f0, L, gamma, log2, vl, unvc


class Spectrum(object):
    """Turns vocoder frames into band spectra, keeping the prediction state of each talker."""

    def __init__(self):
        self.state = {}                                   # key -> [Params, time of last frame]

    def channel(self, key, d, now):
        """49 vocoder bits of one 20 ms frame -> list of NB values in dB (relative), or None."""
        st = self.state.get(key)
        if st is None or now - st[1] > NEW_STREAM_S:
            st = [Params(), now]
        st[1] = now
        if len(self.state) > 64:
            self.state.clear()
        self.state[key] = st
        res = decode(d, st[0])
        if res[0] != 0:
            st[0] = Params()                              # mbelib restarts its prediction after an erasure or tone
            return None
        _s, f0, L, gamma, log2, vl, unvc = res
        p = st[0]
        p.L, p.gamma, p.log2 = L, gamma, log2
        return self.bands(f0, L, log2, vl, unvc)

    @staticmethod
    def bands(f0, L, log2, vl, unvc):
        """Harmonic amplitudes -> NB bands of BAND_HZ, interpolated between harmonics (dB)."""
        hz = f0 * 8000.0
        un = DB * math.log(unvc, 2)
        h = [DB * log2[l] + (0.0 if vl[l] else un) for l in range(1, L + 1)]
        out = []
        n = 0
        for i in range(NB):
            f = (i + 0.5) * BAND_HZ
            x = f / hz - 1.0                              # harmonic index, 0 = the first
            if x <= 0:
                out.append(h[0])
                continue
            n = int(x)
            if n >= L - 1:
                out.append(h[L - 1])
                continue
            fr = x - n
            out.append(h[n] * (1.0 - fr) + h[n + 1] * fr)
        return out


def frame_channels(frame):
    """120-byte frame -> list of five 49-bit lists (or None when it is not V/D type 2 voice)."""
    import ysfdec as y
    if len(frame) != 120:
        return None
    bits = y.bytes_to_bits(frame)
    data, ok, _v, _g = y.fich_decode(bits[40:240], "A")
    if not ok:
        return None
    f = y.fich_fields(data)
    if f["DT"] != 2 or f["FI"] != 1:
        return None
    return [lv.vocoder_bits(bits, j) for j in range(5)]


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--replay", metavar="FILE", required=True, help="a ysfcap.py capture (JSON lines)")
    ap.add_argument("--call", help="show only this source callsign")
    a = ap.parse_args()
    sp = Spectrum()
    for gw, call, data, t in lv.replay(a.replay):
        if a.call and call.upper() != a.call.upper():
            continue
        chans = frame_channels(data)
        if chans is None:
            continue
        for j, d in enumerate(chans):
            row = sp.channel((gw, call), d, t + 0.02 * j)
            print("%.2f %s" % (t + 0.02 * j, " ".join("%3.0f" % v for v in row) if row else "-"))
    return 0


if __name__ == "__main__":
    sys.exit(main())
