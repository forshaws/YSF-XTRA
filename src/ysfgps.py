#!/usr/bin/env python3
"""ysfgps: list the GPS block of GPS-attached text messages and compare it with the
positions you set on the radio. Needs ysfdec.py in the same folder. Python 3.5+.

Capture first (one run, send every test message in order, then Ctrl+C):
    sudo python3 ysfcap.py --call YOURCALL --label GPS --out cap_GPS.jsonl

Then:
    python3 ysfgps.py cap_GPS.jsonl                    # decoded positions and raw blocks
    python3 ysfgps.py cap_GPS.jsonl --truth truth.txt  # add your positions, show what changed

truth.txt: one line per message, in the order you sent them, exactly as the radio
shows the position (blank lines and lines starting with # are ignored):
    N 54 14.954  W 0 23.416
    N 54 14.954  W 0 23.416  93      (optional last number = altitude in metres)
Messages without GPS attached are skipped on both sides only if you leave them out
of truth.txt; the tool prints how many GPS messages it found so you can check.

Only the sender's own transmissions should be captured (ysfcap keeps one callsign).
"""

import argparse
import re
import sys

import ysfdec as y
import ysfpos

BLOCK = slice(64, 83)      # '3' + 18 bytes, before the 5-digit sequence number
SEQ = slice(83, 88)
STAMP = slice(89, 99)


def gps_records(paths):
    out = []
    for p in paths:
        for cur in y.records(p):
            if not cur or sorted(cur) != list(range(len(cur))):
                continue
            rec = b"".join(cur[i] for i in range(len(cur)))
            pr = y.parse_record(rec)
            if not pr or not pr["ok"] or rec[61:63] != b"Gf":
                continue
            out.append({"rec": rec, "text": pr["text"], "block": rec[BLOCK],
                        "seq": rec[SEQ].decode("latin-1"), "stamp": rec[STAMP].decode("latin-1")})
    return out


def show_char(b):
    return chr(b) if 33 <= b <= 126 else "."


def parse_truth(path):
    rows = []
    for ln in open(path):
        ln = ln.split("#")[0].strip()
        if not ln:
            continue
        m = re.match(r"^([NS])\s*(\d+)\D+?([\d.]+)\D*?([EW])\s*(\d+)\D+?([\d.]+)\D*?(?:\s(\d+(?:\.\d+)?))?\s*$", ln, re.I)
        if not m:
            raise SystemExit("cannot read truth line: %r" % ln)
        ns, d1, m1, ew, d2, m2, alt = m.groups()
        rows.append({"ns": ns.upper(), "lat_d": int(d1), "lat_m": float(m1),
                     "ew": ew.upper(), "lon_d": int(d2), "lon_m": float(m2),
                     "alt": float(alt) if alt else None, "txt": ln})
    return rows


def fmt_lat(v):
    a = abs(v)
    return "%s %d\u00b0%05.2f'" % ("N" if v >= 0 else "S", int(a), (a - int(a)) * 60)


def fmt_lon(v):
    a = abs(v)
    return "%s %d\u00b0%05.2f'" % ("E" if v >= 0 else "W", int(a), (a - int(a)) * 60)


def diff_idx(a, b):
    return [i for i in range(min(len(a), len(b))) if a[i] != b[i]]


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("captures", nargs="+")
    ap.add_argument("--truth", help="file with the positions you set, one per message")
    a = ap.parse_args()

    recs = gps_records(a.captures)
    print("%d GPS-attached messages found" % len(recs))
    if not recs:
        return 1
    truth = parse_truth(a.truth) if a.truth else None
    if truth is not None and len(truth) != len(recs):
        print("WARNING: %d positions in %s but %d GPS messages captured - pairing by order will be wrong"
              % (len(truth), a.truth, len(recs)))

    print()
    print("Decoded as APRS Mic-E (see ysfpos.py):")
    for i, r in enumerate(recs):
        try:
            d = ysfpos.decode_block(r["block"])
            alt = "alt %d m" % d["alt_m"] if d["alt_m"] is not None else "no altitude"
            dec = "%s %s  %d kn  course %d  %s" % (fmt_lat(d["lat"]), fmt_lon(d["lon"]),
                                                  d["speed_kn"], d["course"], alt)
        except ValueError as e:
            dec = "(cannot decode: %s)" % e
        t = ("   you set: " + truth[i]["txt"]) if truth and i < len(truth) else ""
        print("%4d %s %s  %s%s" % (i + 1, r["seq"], r["stamp"][6:10], dec, t))

    print()
    print("   # seq   time        block (19 bytes, hex)                   chars")
    for i, r in enumerate(recs):
        b = r["block"]
        print("%4d %s %s %s  %-19s" % (i + 1, r["seq"], r["stamp"][6:10], b.hex(),
                                       "".join(show_char(c) for c in b)))

    print()
    print("byte positions that changed against the FIRST message (index 0 = the leading '3'):")
    base = recs[0]["block"]
    for i, r in enumerate(recs[1:], 2):
        d = diff_idx(base, r["block"])
        extra = ""
        if truth and i <= len(truth):
            extra = "   <- " + truth[i - 1]["txt"]
        print("%4d  %-40s%s" % (i, ",".join(str(x) for x in d) or "(none)", extra))

    print()
    print("byte positions that changed against the PREVIOUS message:")
    for i in range(1, len(recs)):
        d = diff_idx(recs[i - 1]["block"], recs[i]["block"])
        print("%4d  %s" % (i + 1, ",".join(str(x) for x in d) or "(none)"))

    print()
    print("per byte position: distinct values seen (index: values)")
    for pos in range(19):
        vals = sorted(set(r["block"][pos] for r in recs))
        if len(vals) > 1:
            print("%3d: %s" % (pos, " ".join("%02x" % v for v in vals)))
    return 0


if __name__ == "__main__":
    sys.exit(main())
