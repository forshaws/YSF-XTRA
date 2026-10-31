#!/usr/bin/env python3
"""ysfdiff: find which bytes of a YSF frame depend on the text message sent.

Input: one or more captures from ysfcap.py, each holding the SAME message sent
several times:

    python3 ysfdiff.py HELLO=cap_HELLO.jsonl WORLD=cap_WORLD.jsonl ...

Frames are aligned by the frame-counter byte. For every frame number and byte
position the report shows:

    .  identical in every repeat of every message  (sync, constants)
    S  identical across repeats of a message but different between messages
       -> candidate text-carrying bytes
    v  varies between repeats of the same message  (voice, noise)
    -  not present in enough repeats to judge

Then, for rows that contain S, the S bytes for each message are printed in hex
and binary so bit-level patterns can be compared.
Standard library only, Python 3.5+.
"""

import json
import sys


def load(path, gap=1.5):
    """Return a list of transmissions; each is {frame_number: bytes}."""
    txs = []
    cur = None
    last_t = None
    last_ctr = None
    with open(path) as f:
        for line in f:
            r = json.loads(line)
            new = cur is None or (r["t"] - last_t) > gap or r["ctr"] < last_ctr
            if new:
                cur = {}
                txs.append(cur)
            cur[r["ctr"] >> 1] = bytes.fromhex(r["data"])
            last_t, last_ctr = r["t"], r["ctr"]
    return txs


def analyse(msgs):
    """msgs: {label: [tx, ...]} -> (grid rows, candidates)"""
    frames = set()
    for txs in msgs.values():
        for tx in txs:
            frames.update(tx.keys())
    rows = {}
    cands = {}
    for fn in sorted(frames):
        row = []
        for pos in range(120):
            per_label = {}
            ok = True
            for label, txs in msgs.items():
                vals = [tx[fn][pos] for tx in txs if fn in tx]
                if len(vals) < 2:
                    ok = False
                    break
                if len(set(vals)) > 1:
                    per_label[label] = None
                else:
                    per_label[label] = vals[0]
            if not ok:
                row.append("-")
            elif None in per_label.values():
                row.append("v")
            elif len(set(per_label.values())) > 1:
                row.append("S")
                cands.setdefault(fn, []).append((pos, per_label))
            else:
                row.append(".")
        rows[fn] = "".join(row)
    return rows, cands


def main(argv):
    if len(argv) < 3 or not all("=" in a for a in argv[1:]):
        print(__doc__)
        return 2
    msgs = {}
    for a in argv[1:]:
        label, path = a.split("=", 1)
        txs = load(path)
        msgs.setdefault(label, []).extend(txs)
        print("%s: %d transmission(s), %d frame(s) in the longest"
              % (label, len(txs), max([len(t) for t in txs] or [0])))
    if len(msgs) < 2:
        print("need at least two different messages to compare")
        return 2
    rows, cands = analyse(msgs)
    print("\nframe  bytes 0..119 (10 per group)")
    for fn, row in rows.items():
        groups = " ".join(row[i:i + 10] for i in range(0, 120, 10))
        print("%4d   %s" % (fn, groups))
    if not cands:
        print("\nno message-dependent bytes found (check repeats and labels)")
        return 0
    labels = sorted(msgs)
    print("\ncandidate bytes (frame, offset): " + "  ".join(labels))
    for fn, lst in sorted(cands.items()):
        for pos, per in lst:
            print("%4d %3d : " % (fn, pos) +
                  "  ".join("%s=%02x/%s" % (l, per[l], format(per[l], "08b")) for l in labels))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
