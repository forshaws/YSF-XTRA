#!/usr/bin/env python3
"""ysfsend: send ONE C4FM text message (broadcast, GPS-off record) to a YSF reflector.

Needs ysfdec.py in the same folder. Python 3.5+, standard library only.
Sends only under the callsign and radio ID you give it - use your own.

Steps (nothing is transmitted until you add --live):

  1. Check the encoder against a real capture of YOUR radio (byte-for-byte):
       python3 ysfsend.py --check cap_HELLO.jsonl

  2. Dry run - builds the 7 frames, writes them to a file, decodes them back:
       python3 ysfsend.py --template cap_HELLO.jsonl --text "CQ TEST" --dry out.jsonl

  3. Live (logs in to the reflector as a client, sends, logs out):
       python3 ysfsend.py --template cap_HELLO.jsonl --text "CQ TEST" --live \\
           --host 127.0.0.1 --port 42002 --call N0CALL --radio-id 12345

--template supplies the gateway name, destination and FICH bits copied from a
real transmission by your radio (a GPS-off message, 7 frames). Without it the
FICH is guessed and may not be accepted by a radio.
"""

import argparse
import json
import os
import socket
import sys
import time

import ysfdec as y

NFRAMES = 7           # header, FN0..FN4, terminator


# ------------------------------------------------------------------ record

def build_record(text, seq, stamp, call, radio_id, to_call="*" * 10, to_id=""):
    """200-byte GPS-off record. stamp = 'YYMMDDHHMM' (10 digits)."""
    rid = radio_id.ljust(5)[:5]
    c10 = call.ljust(10)[:10]
    rec = bytearray(b" " * 200)
    rec[0:10] = ("*****" + rid).encode("ascii")
    rec[10:20] = c10.encode("ascii")
    rec[20:30] = c10.encode("ascii")
    rec[30:40] = c10.encode("ascii")
    rec[55:60] = rid.encode("ascii")
    rec[60] = (seq - 16) & 0xFF
    rec[61:64] = b"Ge_"
    tail = "3%05d0%s00%s00" % (seq, stamp, stamp)
    rec[64:64 + len(tail)] = tail.encode("ascii")
    rec[95:100] = to_id.ljust(5)[:5].encode("ascii")
    rec[100:110] = to_call.ljust(10)[:10].encode("ascii")
    rec[110:190] = text.ljust(80)[:80].encode("ascii")
    rec[190] = 3
    rec[191] = sum(rec[60:191]) & 0xFF
    rec[192:200] = b"\0" * 8
    return bytes(rec)


# ------------------------------------------------------------------ frames

def place_units(head20, tail20):
    h = y.unit_encode(y.bytes_to_bits(head20), "A", "white", 9)
    t = y.unit_encode(y.bytes_to_bits(tail20), "A", "white", 9)
    dch = [0] * 720
    for i in range(5):
        dch[i * 144:i * 144 + 72] = h[i * 72:(i + 1) * 72]
        dch[i * 144 + 72:i * 144 + 144] = t[i * 72:(i + 1) * 72]
    return dch


def guess_fich(fi, fn):
    """Guessed FICH (used only without --template)."""
    vals = {"FI": fi, "CS": 0, "CM": 0, "BN": 0, "BT": 0, "FN": fn, "FT": 4, "Rev": 0,
            "Dev": 0, "MR": 0, "VoIP": 0, "DT": 1, "SQL": 0, "SC": 0}
    bits = []
    for name, n in y.FICH_FIELDS:
        bits += y.int_bits(vals[name], n)
    return y.fich_encode(bits)


def build_frames(record, fich_list=None):
    """Return the 7 frames (120 bytes each) for a 200-byte record."""
    plan = [(0, 0), (1, 0), (1, 1), (1, 2), (1, 3), (1, 4), (2, 0)]   # (FI, FN)
    frames = []
    for k, (fi, fn) in enumerate(plan):
        seg = record[40 * fn:40 * fn + 40]
        dch = place_units(seg[:20], seg[20:])
        fich = fich_list[k] if fich_list else guess_fich(fi, fn)
        bits = y.bytes_to_bits(y.SYNC) + fich + dch
        frames.append(y.bits_to_bytes(bits))
    return frames


def ctr_for(k):
    return (k << 1) | (1 if k == NFRAMES - 1 else 0)


def packet(gw, src, dst, k, frame):
    return (b"YSFD" + gw.ljust(10)[:10].encode("ascii") + src.ljust(10)[:10].encode("ascii") +
            dst.ljust(10)[:10].encode("ascii") + bytes([ctr_for(k)]) + frame)


# ------------------------------------------------------------------ template

def load_template(path):
    """First 7-frame transmission in a ysfcap file -> dict(gw, src, dst, frames, fich, record)."""
    txs, cur, last = [], None, None
    with open(path) as f:
        for line in f:
            r = json.loads(line)
            if cur is None or r["ctr"] <= last:
                cur = []
                txs.append(cur)
            cur.append(r)
            last = r["ctr"]
    for tx in txs:
        if len(tx) == NFRAMES and [r["ctr"] for r in tx] == [ctr_for(k) for k in range(NFRAMES)]:
            frames = [bytes.fromhex(r["data"]) for r in tx]
            fich = [y.split_frame(fr)[1] for fr in frames]
            # assemble the record from frames 1..5 (FN0..FN4)
            rec = b""
            for fr in frames[1:6]:
                u = {x["name"]: x for x in y.decode_frame(fr)["units"]}
                if not (u["vd1-head72"]["ok"] and u["vd1-tail72"]["ok"]):
                    rec = None
                    break
                rec += u["vd1-head72"]["bytes"] + u["vd1-tail72"]["bytes"]
            return {"gw": tx[0]["gw"], "src": tx[0]["src"], "dst": tx[0]["dst"],
                    "frames": frames, "fich": fich, "record": rec}
    return None


def cmd_check(path):
    t = load_template(path)
    if not t or not t["record"]:
        print("no complete 7-frame GPS-off transmission found in %s" % path)
        return 1
    print("template: gw=%r src=%r dst=%r" % (t["gw"], t["src"], t["dst"]))
    mine = build_frames(t["record"], t["fich"])
    bad = [k for k in range(NFRAMES) if mine[k] != t["frames"][k]]
    print("frames re-encoded from the decoded record: %s" %
          ("ALL 7 MATCH the capture byte for byte" if not bad else "MISMATCH in frames %s" % bad))
    # record builder
    rec = t["record"]
    pr = y.parse_record(rec)
    seq = int(rec[65:70])
    stamp = rec[71:81].decode("ascii")
    rid = rec[5:10].decode("ascii").strip()
    call = rec[10:20].decode("ascii").strip()
    rebuilt = build_record(pr["text"], seq, stamp, call, rid)
    ok = rebuilt == rec
    print("record builder reproduces the captured record: %s" % ("YES" if ok else "NO"))
    if not ok:
        for i in range(200):
            if rebuilt[i] != rec[i]:
                print("   byte %d: built %02x captured %02x" % (i, rebuilt[i], rec[i]))
    return 0 if (not bad and ok) else 1


# ------------------------------------------------------------------ send

def check_text(text):
    if not text or len(text) > 80:
        raise SystemExit("text must be 1-80 characters")
    if any(ord(c) < 32 or ord(c) > 126 for c in text):
        raise SystemExit("text must be printable ASCII")


def next_seq(path):
    try:
        with open(path) as f:
            n = int(f.read().strip())
    except (OSError, ValueError):
        n = 40000
    n += 1
    if n > 99999:
        n = 40000
    with open(path, "w") as f:
        f.write(str(n))
    return n


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--check", metavar="CAPTURE", help="verify encoder against a real capture and exit")
    ap.add_argument("--template", help="ysfcap file with a real 7-frame GPS-off transmission")
    ap.add_argument("--text", help="message (1-80 printable ASCII characters)")
    ap.add_argument("--call", default=None, help="your callsign (default: from template)")
    ap.add_argument("--radio-id", default=None, help="your radio ID (5 characters, required to send or build)")
    ap.add_argument("--to-call", default="*" * 10, help="destination callsign, default ********** = everyone")
    ap.add_argument("--to-id", default="", help="destination radio ID (5 characters), blank = everyone")
    ap.add_argument("--gateway", default=None, help="gateway name on the wire (default: from template, else --call)")
    ap.add_argument("--dst", default=None, help="network destination field (default: from template, else ALL)")
    ap.add_argument("--dry", metavar="FILE", help="write the packets to FILE (ysfcap format) and exit")
    ap.add_argument("--live", action="store_true", help="actually send to the reflector")
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=42002)
    ap.add_argument("--seq-file", default=os.path.expanduser("~/.ysfsend_seq"))
    a = ap.parse_args()

    if a.check:
        return cmd_check(a.check)
    if not a.text:
        ap.error("--text is required")
    check_text(a.text)
    if not (a.dry or a.live):
        ap.error("choose --dry FILE or --live")

    tpl = load_template(a.template) if a.template else None
    if a.template and not tpl:
        raise SystemExit("no 7-frame transmission found in the template")
    call = (a.call or (tpl and tpl["src"]) or "").upper()
    if not call:
        raise SystemExit("--call is required without a template")
    gw = (a.gateway or (tpl and tpl["gw"]) or call).upper()
    dst = (a.dst or (tpl and tpl["dst"]) or "ALL").upper()
    rid = a.radio_id
    if not rid or len(rid) != 5:
        raise SystemExit("--radio-id must be exactly 5 characters")

    seq = next_seq(a.seq_file)
    stamp = time.strftime("%y%m%d%H%M")
    rec = build_record(a.text, seq, stamp, call, rid, a.to_call, a.to_id)
    frames = build_frames(rec, tpl["fich"] if tpl else None)
    pkts = [packet(gw, call, dst, k, frames[k]) for k in range(NFRAMES)]
    if not tpl:
        print("WARNING: no --template, FICH bits are guessed")

    # decode our own frames back as a sanity check
    back = b""
    for fr in frames[1:6]:
        u = {x["name"]: x for x in y.decode_frame(fr)["units"]}
        if not (u["vd1-head72"]["ok"] and u["vd1-tail72"]["ok"]):
            raise SystemExit("internal error: built frame does not decode")
        back += u["vd1-head72"]["bytes"] + u["vd1-tail72"]["bytes"]
    pr = y.parse_record(back)
    print("decoded back: text=%r to=%r checksum=%s seq=%d stamp=%s" %
          (pr["text"], pr["to_call"], "OK" if pr["ok"] else "BAD", seq, stamp))

    if a.dry:
        t0 = time.time()
        with open(a.dry, "w") as f:
            for k, p in enumerate(pkts):
                f.write(json.dumps({"t": round(t0 + 0.1 * k, 3), "label": "DRY", "gw": gw, "src": call,
                                    "dst": dst, "ctr": p[34], "data": p[35:].hex()}) + "\n")
        print("wrote %d frames to %s (nothing sent)" % (len(pkts), a.dry))
        return 0

    poll = b"YSFP" + call.ljust(10)[:10].encode("ascii")
    bye = b"YSFU" + call.ljust(10)[:10].encode("ascii")
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    s.settimeout(3)
    addr = (a.host, a.port)
    s.sendto(poll, addr)
    try:
        r, _ = s.recvfrom(1024)
    except socket.timeout:
        raise SystemExit("no reply from the reflector at %s:%d - nothing sent" % addr)
    if r[:4] != b"YSFP":
        raise SystemExit("unexpected reply %r - nothing sent" % r[:8])
    print("logged in: %s" % r[4:].decode("ascii", "replace").strip())
    time.sleep(0.3)
    for k, p in enumerate(pkts):
        s.sendto(p, addr)
        time.sleep(0.1)
    print("sent %d frames as %s -> %s (gateway %s)" % (len(pkts), call, dst, gw))
    time.sleep(0.5)
    s.sendto(bye, addr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
