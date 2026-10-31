#!/usr/bin/env python3
"""ysfcap: passively record YSF data frames sent to a reflector by ONE callsign.

Linux, Python 3.5+, standard library only. Needs root (raw packet socket).
It only listens: it never sends anything and never touches the reflector.

Only frames whose source callsign equals --call are kept; everything else is
discarded as soon as the header is read. Use it with your own test transmissions.

Example:
    sudo python3 ysfcap.py --call YOURCALL --label HELLO --out cap_HELLO.jsonl
    (send the same text message 3 times from the radio, then Ctrl+C)

Each output line is JSON: t, label, gw, src, dst, ctr (frame counter byte),
data (the 120-byte frame payload as hex).
"""

import argparse
import json
import os
import re
import socket
import struct
import sys
import time

PKT_OUTGOING = 4


def parse_frame(frame, port):
    """Ethernet frame -> UDP payload bytes if it is IPv4/UDP to `port`, else None."""
    if len(frame) < 14 + 20 + 8:
        return None
    if struct.unpack("!H", frame[12:14])[0] != 0x0800:
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


def parse_ysfd(payload):
    """155-byte YSFD packet -> dict, else None."""
    if len(payload) != 155 or payload[:4] != b"YSFD":
        return None
    return {
        "gw": field(payload[4:14]),
        "src": field(payload[14:24]),
        "dst": field(payload[24:34]),
        "ctr": payload[34],
        "data": payload[35:155].hex(),
    }


def main():
    p = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    p.add_argument("--call", required=True, help="only keep frames from this source callsign")
    p.add_argument("--label", required=True, help="name for this capture, e.g. the text you send")
    p.add_argument("--out", required=True, help="output file (JSON lines, appended)")
    p.add_argument("--port", type=int, default=42002, help="reflector UDP port (default 42002)")
    args = p.parse_args()
    if not re.match(r"^[A-Za-z0-9/]{3,10}$", args.call):
        raise SystemExit("--call must be a callsign")
    want = args.call.upper()
    if not hasattr(socket, "AF_PACKET"):
        raise SystemExit("needs Linux")
    try:
        s = socket.socket(socket.AF_PACKET, socket.SOCK_RAW, socket.ntohs(3))
    except PermissionError:
        raise SystemExit("run with sudo")

    fd = os.open(args.out, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
    out = os.fdopen(fd, "a")
    kept = 0
    print("listening for %s on UDP %d, label %r; Ctrl+C to stop" % (want, args.port, args.label))
    try:
        while True:
            frame, addr = s.recvfrom(65535)
            if addr[2] == PKT_OUTGOING:
                continue
            payload = parse_frame(frame, args.port)
            if payload is None:
                continue
            rec = parse_ysfd(payload)
            if rec is None or rec["src"].upper() != want:
                continue
            rec["t"] = round(time.time(), 3)
            rec["label"] = args.label
            out.write(json.dumps(rec) + "\n")
            out.flush()
            kept += 1
            if kept % 50 == 0:
                sys.stdout.write("\r%d frames" % kept)
                sys.stdout.flush()
    except KeyboardInterrupt:
        pass
    finally:
        out.close()
        print("\nsaved %d frames to %s" % (kept, args.out))


if __name__ == "__main__":
    main()
