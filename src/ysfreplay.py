#!/usr/bin/env python3
"""ysfreplay: feed ysfcap capture files through the ysfmsgd decoder, so positions in a
capture are recorded as if they had just been heard (stamped with the time of capture).

Needs ysfdec.py, ysfpos.py, ysfmsgstore.py and ysfmsgd.py in the same folder. Python 3.5+.
Run it as the service user so the files keep their owner:

    sudo -u ysfdash python3 /opt/ysfdash/ysfreplay.py /tmp/cap_mike.jsonl

Only positions are written (voice frames and GPS-attached messages). Use --list to
print what a capture contains without writing anything.
"""

import argparse
import json
import os
import sys
import tempfile

import ysfmsgd
import ysfmsgstore
import ysfpos


def frames(path):
    with open(path) as f:
        for line in f:
            if not line.startswith("{"):
                continue
            try:
                r = json.loads(line, strict=False)
                data = bytes.fromhex(r["data"])
                if len(data) != 120:
                    continue
                yield (b"YSFD" + r["gw"].ljust(10)[:10].encode("latin-1") +
                       r["src"].ljust(10)[:10].encode("latin-1") +
                       r["dst"].ljust(10)[:10].encode("latin-1") + bytes([r["ctr"]]) + data, float(r["t"]))
            except (ValueError, KeyError, TypeError):
                continue                          # damaged or cut-off line


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("captures", nargs="+")
    ap.add_argument("--positions", default="/var/lib/ysfradio/positions.json")
    ap.add_argument("--list", action="store_true", help="show positions found, write nothing")
    a = ap.parse_args()

    tmp = tempfile.mkdtemp()
    pos = ysfmsgstore.PosFile(os.path.join(tmp, "p.json") if a.list else a.positions)
    dec = ysfmsgd.Decoder(ysfmsgstore.MsgFile(os.path.join(tmp, "m.json")), 10, pos)
    n = 0
    for path in a.captures:
        for pkt, t in frames(path):
            dec.packet(pkt, t)
            n += 1
    found = ysfmsgstore.PosReader(os.path.join(tmp, "p.json") if a.list else a.positions).get()
    print("%d frames read" % n)
    for call, (lat, lon, ts) in sorted(found.items()):
        print("  %-8s %.5f %.5f" % (call, lat, lon))
    if not found:
        print("no positions found")
    return 0


if __name__ == "__main__":
    sys.exit(main())
