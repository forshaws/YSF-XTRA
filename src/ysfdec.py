#!/usr/bin/env python3
"""ysfdec: decode captured YSF (C4FM) frames per Yaesu spec v1.02 and show plain data.

    python3 ysfdec.py cap_HELLO.jsonl            # decode every frame
    python3 ysfdec.py cap_HELLO.jsonl --ok       # only units whose CRC passed
    python3 ysfdec.py --selftest                 # encoder/decoder round trip

Frame (120 bytes = 960 bits): sync 40 | FICH 200 | 720 data-channel bits.
FICH: 32 bits + CRC16 -> 4x Golay(24,12) -> +4 zero -> rate 1/2 K=5 -> dibit
interleave (5 wide, 20 deep).  DCH unit: 160 bits -> whiten PN(9,5) -> +CRC16
-> +4 zero -> rate 1/2 K=5 -> interleave (9 wide, 20 deep) = 360 bits.

Where the spec is ambiguous (convolution tap order, CRC before/after whitening,
CRC bit order, where the unit sits in each frame type) several readings are
tried and the one whose CRC checks out is reported.  Standard library only.
"""

import json
import sys

SYNC = bytes.fromhex("D471C9634D")

# ---------------------------------------------------------------- bit helpers


def bytes_to_bits(b):
    return [(x >> (7 - i)) & 1 for x in b for i in range(8)]


def bits_to_bytes(bits):
    out = bytearray()
    for i in range(0, len(bits) - 7, 8):
        v = 0
        for k in range(8):
            v = (v << 1) | bits[i + k]
        out.append(v)
    return bytes(out)


# ---------------------------------------------------------------- CRC16


def crc16(bits):
    crc = 0
    for b in bits:
        fb = ((crc >> 15) & 1) ^ b
        crc = (crc << 1) & 0xFFFF
        if fb:
            crc ^= 0x1021
    return crc ^ 0xFFFF


def int_bits(v, n=16):
    return [(v >> (n - 1 - i)) & 1 for i in range(n)]


# ---------------------------------------------------------------- whitening


def whitening(n):
    s = [1, 0, 0, 1, 0, 0, 1, 1, 1]   # S0..S8 from 1 1 1 0 0 1 0 0 1 (S8..S0)
    out = []
    for _ in range(n):
        out.append(s[0])
        s = s[1:] + [s[0] ^ s[4]]      # x^9 + x^5 + 1
    return out


# ---------------------------------------------------------------- convolution
# Two readings of G1 = x^4+x^3+1, G2 = x^4+x^2+x+1 (delay taps, 0 = current bit)
TAPSETS = {
    "A": ((0, 3, 4), (0, 1, 2, 4)),
    "B": ((0, 1, 4), (0, 2, 3, 4)),
}


def conv_encode(bits, taps="A"):
    g1, g2 = TAPSETS[taps]
    hist = [0, 0, 0, 0, 0]     # hist[d] = bit delayed by d
    out = []
    for b in bits + [0, 0, 0, 0]:
        hist = [b] + hist[:4]
        out.append(sum(hist[d] for d in g1) & 1)
        out.append(sum(hist[d] for d in g2) & 1)
    return out


def _out_table(taps):
    g1, g2 = TAPSETS[taps]
    tab = {}
    for state in range(16):            # state bits: delay1 = bit3 ... delay4 = bit0
        for b in (0, 1):
            hist = [b] + [(state >> (3 - i)) & 1 for i in range(4)]
            tab[(state, b)] = (sum(hist[d] for d in g1) & 1,
                               sum(hist[d] for d in g2) & 1,
                               ((state >> 1) | (b << 3)) & 15)
    return tab


def viterbi(coded, taps="A"):
    """Hard-decision Viterbi. Returns (bits incl. 4 tail, errors)."""
    tab = _out_table(taps)
    n = len(coded) // 2
    INF = 10 ** 9
    metric = [INF] * 16
    metric[0] = 0
    back = []
    for i in range(n):
        r1, r2 = coded[2 * i], coded[2 * i + 1]
        new = [INF] * 16
        bp = [None] * 16
        for st in range(16):
            m = metric[st]
            if m >= INF:
                continue
            for b in (0, 1):
                o1, o2, ns = tab[(st, b)]
                c = m + (o1 != r1) + (o2 != r2)
                if c < new[ns]:
                    new[ns] = c
                    bp[ns] = (st, b)
        metric = new
        back.append(bp)
    st = 0                             # tail forces state 0
    err = metric[0]
    bits = []
    for i in range(n - 1, -1, -1):
        pst, b = back[i][st]
        bits.append(b)
        st = pst
    bits.reverse()
    return bits, err


# ---------------------------------------------------------------- interleave


def interleave(dibits, width):
    """Encoder side: write rows of `width`, read columns."""
    n = len(dibits)
    rows = n // width
    return [dibits[r * width + c] for c in range(width) for r in range(rows)]


def deinterleave(dibits, width):
    n = len(dibits)
    rows = n // width
    out = [None] * n
    k = 0
    for c in range(width):
        for r in range(rows):
            out[r * width + c] = dibits[k]
            k += 1
    return out


def to_dibits(bits):
    return [(bits[i] << 1) | bits[i + 1] for i in range(0, len(bits), 2)]


def from_dibits(d):
    out = []
    for x in d:
        out += [x >> 1, x & 1]
    return out


# ---------------------------------------------------------------- Golay(24,12)

_P = """110001110101 011000111011 111101101000 011110110100 001111011010
110110011001 011011001101 001101100111 110111000110 101010010111
100100111110 100011101011""".split()
GOLAY_P = [[int(c) for c in r] for r in _P]


def golay_encode(d12):
    p = [0] * 12
    for i, bit in enumerate(d12):
        if bit:
            p = [x ^ y for x, y in zip(p, GOLAY_P[i])]
    return d12 + p


_GOLAY_BOOK = None


def golay_decode(cw24):
    """Nearest codeword (brute force over 4096 words). Returns (d12, dist)."""
    global _GOLAY_BOOK
    if _GOLAY_BOOK is None:
        _GOLAY_BOOK = []
        for v in range(4096):
            d = int_bits(v, 12)
            _GOLAY_BOOK.append((int("".join(map(str, golay_encode(d))), 2), d))
    x = int("".join(map(str, cw24)), 2)
    best = min(_GOLAY_BOOK, key=lambda e: bin(e[0] ^ x).count("1"))
    return best[1], bin(best[0] ^ x).count("1")


# ---------------------------------------------------------------- FICH


def fich_encode(bits32, taps="A"):
    d = bits32 + int_bits(crc16(bits32))
    cw = []
    for i in range(0, 48, 12):
        cw += golay_encode(d[i:i + 12])
    coded = conv_encode(cw, taps)                      # 100 bits in -> 200
    return from_dibits(interleave(to_dibits(coded), 5))


def fich_decode(bits200, taps="A"):
    coded = from_dibits(deinterleave(to_dibits(bits200), 5))
    dec, verr = viterbi(coded, taps)
    cw = dec[:96]
    d = []
    gerr = 0
    for i in range(0, 96, 24):
        x, e = golay_decode(cw[i:i + 24])
        d += x
        gerr += e
    data, crc = d[:32], d[32:48]
    ok = int_bits(crc16(data)) == crc
    return data, ok, verr, gerr


FICH_FIELDS = [("FI", 2), ("CS", 2), ("CM", 2), ("BN", 2), ("BT", 2), ("FN", 3),
               ("FT", 3), ("Rev", 1), ("Dev", 1), ("MR", 3), ("VoIP", 1),
               ("DT", 2), ("SQL", 1), ("SC", 7)]


def fich_fields(bits32):
    out = {}
    i = 0
    for name, n in FICH_FIELDS:
        v = 0
        for b in bits32[i:i + n]:
            v = (v << 1) | b
        out[name] = v
        i += n
    return out


# ---------------------------------------------------------------- DCH unit


def unit_encode(data160, taps="A", crc_over="white", width=9):
    w = whitening(len(data160))
    white = [a ^ b for a, b in zip(data160, w)]
    body = white + int_bits(crc16(white if crc_over == "white" else data160))
    coded = conv_encode(body, taps)
    return from_dibits(interleave(to_dibits(coded), width))


def unit_decode(bits, width=9, nbits=160):
    """Try all readings; return dict(ok, data, taps, crc_over, crc_order, verr)."""
    coded = from_dibits(deinterleave(to_dibits(bits), width))
    best = None
    for taps in ("A", "B"):
        dec, verr = viterbi(coded, taps)
        body = dec[:nbits + 16]
        w = whitening(nbits)
        white, crc = body[:nbits], body[nbits:]
        plain = [a ^ b for a, b in zip(white, w)]
        for crc_over, src in (("white", white), ("plain", plain)):
            c = crc16(src)
            for order, want in (("msb", int_bits(c)), ("lsb", int_bits(c)[::-1])):
                if want == crc:
                    return {"ok": True, "data": plain, "taps": taps, "crc_over": crc_over,
                            "crc_order": order, "verr": verr}
        cand = {"ok": False, "data": plain, "taps": taps, "crc_over": "?", "crc_order": "?",
                "verr": verr}
        if best is None or verr < best["verr"]:
            best = cand
    return best


# ---------------------------------------------------------------- frame layout


def split_frame(frame):
    bits = bytes_to_bits(frame)
    return bits[:40], bits[40:240], bits[240:960]


def printable(b):
    return "".join(chr(x) if 32 <= x < 127 else "." for x in b)


def decode_frame(frame, taps_hint="A"):
    """Return dict with sync ok, fich info and list of decoded units."""
    res = {"sync": frame[:5] == SYNC}
    sync, fich, dch = split_frame(frame)
    data, ok, verr, gerr = fich_decode(fich, taps_hint)
    res["fich_ok"] = ok
    res["fich_err"] = (verr, gerr)
    res["fich"] = fich_fields(data) if ok else None
    res["units"] = []
    dt = res["fich"]["DT"] if ok else None
    res["dt"] = dt
    cands = []
    # Data FR / generic: two consecutive 360-bit units
    cands.append(("seq-unit0", dch[:360], 9, 160))
    cands.append(("seq-unit1", dch[360:], 9, 160))
    # V/D type 1: one unit spread over five 144-bit channels (72 bits each)
    for name, lo in (("vd1-head72", 0), ("vd1-tail72", 72)):
        g = []
        for i in range(5):
            g += dch[i * 144 + lo:i * 144 + lo + 72]
        cands.append((name, g, 9, 160))
    # V/D type 2: 200-bit unit spread over five channels (40 bits each)
    for name, lo in (("vd2-head40", 0), ("vd2-tail40", 104)):
        g = []
        for i in range(5):
            g += dch[i * 144 + lo:i * 144 + lo + 40]
        cands.append((name, g, 5, 80))
    for name, bits, width, nb in cands:
        r = unit_decode(bits, width, nb)
        r["name"] = name
        r["bytes"] = bits_to_bytes(r["data"])
        res["units"].append(r)
    return res


# ---------------------------------------------------------------- CLI


def selftest():
    import random
    rnd = random.Random(7)
    for taps in "AB":
        f = [rnd.getrandbits(1) for _ in range(32)]
        f[0:2] = [0, 1]
        enc = fich_encode(f, taps)
        d, ok, ve, ge = fich_decode(enc, taps)
        assert ok and d == f and ve == 0, "fich round trip"
        enc[10] ^= 1
        enc[77] ^= 1
        enc[150] ^= 1
        d, ok, ve, ge = fich_decode(enc, taps)
        assert ok and d == f, "fich error correction"
        for crc_over in ("white", "plain"):
            data = [rnd.getrandbits(1) for _ in range(160)]
            enc = unit_encode(data, taps, crc_over)
            r = unit_decode(enc)
            assert r["ok"] and r["data"] == data and r["taps"] == taps, (taps, crc_over, r)
    # whitening table check (known first bytes)
    assert bits_to_bytes(whitening(40)).hex() == "93d751219c"
    # golay min distance 8
    mind = min(sum(golay_encode(int_bits(v, 12))) for v in range(1, 4096))
    assert mind == 8, mind
    print("selftest ok")
    return 0


def records(path):
    """Assemble Data-FR text records. A transmission starts at FI=0 and ends at FI=2;
    per frame number FN the first copy wins (FN0 is sent twice). 40 bytes per frame."""
    out = []
    cur = None
    with open(path) as f:
        for line in f:
            r = json.loads(line)
            d = decode_frame(bytes.fromhex(r["data"]))
            if not d["fich"] or d["dt"] != 1:
                continue
            fi, fn = d["fich"]["FI"], d["fich"]["FN"]
            if fi == 0 or cur is None:
                cur = {}
                out.append(cur)
            u = {x["name"]: x for x in d["units"]}
            if u["vd1-head72"]["ok"] and u["vd1-tail72"]["ok"] and fn not in cur:
                cur[fn] = u["vd1-head72"]["bytes"] + u["vd1-tail72"]["bytes"]
    return out


def parse_record(buf):
    """Return dict(text, to_call, to_id, ok, etx) for an assembled record, or None.
    Layout (from the end): 03 | checksum; text = the 80 bytes before 03; the 10 bytes
    before the text hold the addressee callsign (********** = everyone); the 5 bytes
    before that hold the addressee radio ID (spaces if none)."""
    body = buf.rstrip(b"\0")
    if len(body) < 100 or body[-2] != 3:
        return None
    etx = len(body) - 2
    t0 = etx - 80
    return {
        "text": buf[t0:etx].decode("latin-1").rstrip(),
        "to_call": buf[t0 - 10:t0].decode("latin-1").strip(),
        "to_id": buf[t0 - 15:t0 - 10].decode("latin-1").strip(),
        "ok": body[-1] in (sum(buf[22:etx]) & 0xFF, sum(buf[13:etx]) & 0xFF),   # FT5D sums from 22, FT3D from 13
        "etx": etx,
    }


def show_records(paths):
    for p in paths:
        for i, rec in enumerate(records(p)):
            if not rec:
                continue
            n = max(rec) + 1
            missing = [k for k in range(n) if k not in rec]
            buf = b"".join(rec.get(k, b"?" * 40) for k in range(n))
            print("%s #%d  frames=%s%s" % (p, i + 1, sorted(rec),
                                          "  MISSING %s" % missing if missing else ""))
            for off in range(0, len(buf), 20):
                print("   %3d  %s |%s|" % (off, buf[off:off + 20].hex(), printable(buf[off:off + 20])))
            pr = None if missing else parse_record(buf)
            if pr:
                print("   TEXT = %r  to=%r id=%r  checksum %s" % (
                    pr["text"], pr["to_call"], pr["to_id"], "OK" if pr["ok"] else "BAD"))


def main(argv):
    if "--selftest" in argv:
        return selftest()
    if "--records" in argv:
        show_records([a for a in argv[1:] if not a.startswith("--")])
        return 0
    paths = [a for a in argv[1:] if not a.startswith("--")]
    only_ok = "--ok" in argv
    if not paths:
        print(__doc__)
        return 2
    for p in paths:
        with open(p) as f:
            for line in f:
                r = json.loads(line)
                frame = bytes.fromhex(r["data"])
                d = decode_frame(frame)
                head = "%s ctr=%3d %s->%s sync=%s" % (r.get("label", ""), r["ctr"], r["src"],
                                                      r["dst"], "ok" if d["sync"] else "BAD")
                if d["fich"]:
                    fi = d["fich"]
                    head += " FICH ok FI=%d DT=%d FN=%d FT=%d" % (fi["FI"], fi["DT"], fi["FN"], fi["FT"])
                else:
                    head += " FICH bad (viterbi/golay err %s)" % (d["fich_err"],)
                shown = False
                for u in d["units"]:
                    if u["ok"] or not only_ok:
                        if not shown:
                            print(head)
                            shown = True
                        print("   %-11s %s taps=%s crc=%s/%s verr=%d  %s |%s|" % (
                            u["name"], "CRC-OK " if u["ok"] else "crc-bad", u["taps"], u["crc_over"],
                            u["crc_order"], u["verr"], u["bytes"].hex(), printable(u["bytes"])))
                if not shown and not only_ok:
                    print(head)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
