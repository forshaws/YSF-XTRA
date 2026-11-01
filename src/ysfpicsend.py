#!/usr/bin/env python3
"""ysfpicsend: build the C4FM data streams of a camera-microphone picture, for ysfnews.py.

A picture is sent as several YSF data streams (docs/C4FM-PICTURE-FORMAT.md): an info
record Gh_, a description record Nd_, the JPEG in chunks of 1024 bytes (Nb_) and an end
record Ne_. This module builds those records and turns them into YSFD packets. It does
no network I/O itself.

Check the builder against a real capture of your radio sending a picture (byte for byte):

    python3 ysfpicsend.py --check cap_pic.jsonl

Python 3.5+, standard library only. Needs ysfdec.py, ysfpic.py and ysfpos.py alongside.
"""

import argparse
import json
import sys
import time

import ysfdec as y
import ysfpic
import ysfpos

CHUNK = 1024
FIRST = 250            # JPEG bytes in the first block of a chunk (offset 70-319)
NEXT = 259             # JPEG bytes in each continuation block (offset 61-319)
ZERO = b"\0"


def _head(call, rid, b60):
    """Bytes 0-60 of every block: *****+radio ID, callsign three times, radio ID, record number."""
    rid = rid.ljust(5)[:5]
    c10 = call.ljust(10)[:10]
    h = bytearray(b" " * 61)
    h[0:10] = ("*****" + rid).encode("ascii")
    h[10:20] = c10.encode("ascii")
    h[20:30] = c10.encode("ascii")
    h[30:40] = c10.encode("ascii")
    h[55:60] = rid.encode("ascii")
    h[60] = b60 & 0xFF
    return h


def _pad(buf):
    """Zero fill to a whole number of 40-byte frames."""
    n = -len(buf) % 40
    return bytes(buf) + ZERO * n


def _chk(parts):
    return sum(sum(p) for p in parts) & 0xFF


def gh_block(call, rid, b60, seq, stamp_a, stamp_b, to_id, to_call, shown, lat, lon, alt_m=0, speed=0, course=0):
    """Info record: sender position, sequence number, time stamps, destination, capture time text."""
    b = _head(call, rid, b60)
    b += b"Gh_" + ysfpos.encode_block(lat, lon, speed, course, alt_m)
    b += ("%05d0%s00%s00" % (seq, stamp_a, stamp_b)).encode("ascii")
    b += to_id.ljust(5)[:5].encode("ascii") + to_call.ljust(10)[:10].encode("ascii")
    b += shown.encode("ascii")
    b += b"\x03"
    b.append(_chk([b[22:-1]]))
    return _pad(b)


def nd_block(call, rid, b60, lat, lon, size, stamp14, name, shown, alt_m=0, speed=0, course=0, pos2=None):
    """Description record: file size, time stamp, file name, position again, capture time text."""
    b = _head(call, rid, b60)
    b += b"Nd_" + ysfpos.encode_block(lat, lon, speed, course, alt_m)
    b += b"P\x00\x000\x00" + size.to_bytes(4, "big")
    b += stamp14.encode("ascii") + b"H" + name.ljust(15)[:15].encode("ascii")
    lat2, lon2 = pos2 if pos2 else (lat, lon)             # the radio takes a second GPS fix here
    b += ysfpos.encode_block(lat2, lon2, 0, 0, None, symbol=b"YY")[1:]
    b += shown.encode("ascii")
    b += b"\x03"
    b.append(_chk([b[22:-1]]))
    return _pad(b)


def nb_blocks(call, rid, b60, num, data):
    """One data chunk (at most 1024 bytes) -> its blocks: 250 bytes in the first, 259 in each next."""
    n = len(data)
    first = _head(call, rid, b60) + b"Nb_3P" + num.to_bytes(2, "big") + (0 if n == CHUNK else n).to_bytes(2, "big")
    first += data[:FIRST]
    rest = data[FIRST:]
    pieces = [rest[i:i + NEXT] for i in range(0, len(rest), NEXT)]
    tail = b"\x03"                                       # ETX, check byte, one pad byte after the data
    blocks = [first]
    for p in pieces:
        blocks.append(_head(call, rid, 0) + p)
    cs = _chk([bytes(first[22:]), data[FIRST:]])
    tail += bytes([cs]) + ZERO
    last = blocks[-1] + tail
    cap = 320 if len(blocks) > 1 else 320
    # the tail may spill into a block of its own only when the data ends exactly on a block boundary
    if len(last) > cap:
        over = last[cap:]
        blocks[-1] = last[:cap]
        blocks.append(_head(call, rid, 0) + over)
    else:
        blocks[-1] = last
    out = []
    for i, b in enumerate(blocks):
        b = bytes(b)
        if i == len(blocks) - 1:
            while b.endswith(ZERO) and len(b) > 0 and len(b) % 40 == 0 and False:
                b = b[:-1]
        out.append(_pad(b))
    # trim the last block's trailing all-zero frames the radio does not send
    last = out[-1]
    keep = max(len(bytes(last).rstrip(ZERO)), 1)
    out[-1] = last[:((keep + 39) // 40) * 40]
    return out


def ne_block(call, rid, b60, endnum, total_sum, nchunks):
    b = _head(call, rid, b60)
    b += b"Ne_3P" + endnum.to_bytes(2, "big") + (2 * (nchunks - 1)).to_bytes(2, "big")
    b += (total_sum & 0xFFFF).to_bytes(2, "big") + b"\x03"
    b.append(_chk([b[22:-1]]))
    return _pad(b)


def build_picture(jpeg, call, rid, seq, when, lat, lon, to_id="", to_call="*" * 10, name_no=1,
                  first_chunk=1, alt_m=0, base=0, shown_t=None):
    """The streams of one picture: a list of streams, each a list of blocks (bytes)."""
    stamp_a = time.strftime("%y%m%d%H%M", time.localtime(when))
    stamp_b = time.strftime("%y%m%d%H%M", time.localtime(when + 60))
    shown = time.strftime("%Y/%m/%d %H:%M", time.localtime(shown_t if shown_t is not None else when))
    stamp14 = time.strftime("%Y%m%d%H%M%S", time.localtime(when))
    name = "%s%06d.jpg" % (rid.ljust(5)[:5], name_no)
    streams = [[gh_block(call, rid, base, seq, stamp_a, stamp_b, to_id, to_call, shown, lat, lon, alt_m)],
               [nd_block(call, rid, base + 3, lat, lon, len(jpeg), stamp14, name, shown, alt_m)]]
    chunks = [jpeg[i:i + CHUNK] for i in range(0, len(jpeg), CHUNK)]
    for i, c in enumerate(chunks):
        streams.append(nb_blocks(call, rid, base + 4, first_chunk + i, c))
    streams.append([ne_block(call, rid, base + 5, first_chunk + len(chunks), ysfpic.sum16(jpeg), len(chunks))])
    return streams


# ------------------------------------------------------------------ frames

def stream_frames(blocks, fich_list=None):
    """Blocks of one stream -> 120-byte frames: header, every data frame, terminator.
    FICH: FI, CS=2, CM=1, BN=block number, BT=blocks-1, FN, FT=last frame of the block, DT=1."""
    bt = len(blocks) - 1
    plan = []                                            # (FI, BN, FN, FT, 40-byte segment)
    first_seg = blocks[0][:40]
    plan.append((0, 0, 0, len(blocks[0]) // 40 - 1, first_seg))
    for bn, blk in enumerate(blocks):
        ft = len(blk) // 40 - 1
        for fn in range(ft + 1):
            plan.append((1, bn, fn, ft, blk[40 * fn:40 * fn + 40]))
    plan.append((2, bt, 0, len(blocks[-1]) // 40 - 1, first_seg))
    frames = []
    for k, (fi, bn, fn, ft, seg) in enumerate(plan):
        vals = {"FI": fi, "CS": 2, "CM": 1, "BN": bn, "BT": bt if bt else 0, "FN": fn, "FT": ft, "Rev": 0,
                "Dev": 0, "MR": 0, "VoIP": 0, "DT": 1, "SQL": 0, "SC": 0}
        bits = []
        for name, n in y.FICH_FIELDS:
            bits += y.int_bits(vals[name], n)
        fich = y.fich_encode(bits)
        units = _units(seg[:20], seg[20:])
        frames.append(y.bits_to_bytes(y.bytes_to_bits(y.SYNC) + fich + units))
    return frames


def _units(head20, tail20):
    h = y.unit_encode(y.bytes_to_bits(head20), "A", "white", 9)
    t = y.unit_encode(y.bytes_to_bits(tail20), "A", "white", 9)
    dch = [0] * 720
    for i in range(5):
        dch[i * 144:i * 144 + 72] = h[i * 72:(i + 1) * 72]
        dch[i * 144 + 72:i * 144 + 144] = t[i * 72:(i + 1) * 72]
    return dch


def packets(gw, src, dst, frames):
    n = len(frames)
    out = []
    for k, fr in enumerate(frames):
        ctr = (k << 1) | (1 if k == n - 1 else 0)
        out.append(b"YSFD" + gw.ljust(10)[:10].encode("ascii") + src.ljust(10)[:10].encode("ascii") +
                   dst.ljust(10)[:10].encode("ascii") + bytes([ctr]) + fr)
    return out


# ------------------------------------------------------------------ check

def _captured_streams(path):
    """ysfcap file -> [(blocks as bytes, raw frames)] per stream, using the receiver's own block logic."""
    import ysfmsgd
    import ysfmsgstore
    rows = [json.loads(l) for l in open(path) if l.startswith("{")]
    dec = ysfmsgd.Decoder(ysfmsgstore.MsgFile("/tmp/ysfpicsend-check.json"), pics=ysfpic.PicRx(
        ysfpic.PicFile("/tmp/ysfpicsend-check"), ysfpic.Allow("/tmp/ysfpicsend-check.json")))
    streams, cur = [], None
    got = []

    class Tap(ysfpic.PicRx):
        def stream_end(self, gw, src, frames, now):
            got.append(frames)
            return None
    dec.pics = Tap(None, None)
    raws = []
    last = None
    for r in rows:
        if cur is None or r["ctr"] <= last:
            cur = []
            raws.append(cur)
        cur.append(bytes.fromhex(r["data"]))
        last = r["ctr"]
        dec.packet(b"YSFD" + r["gw"].ljust(10).encode() + r["src"].ljust(10).encode() +
                   r["dst"].ljust(10).encode() + bytes([r["ctr"]]) + bytes.fromhex(r["data"]), now=r["t"])
    out = []
    for frames, raw in zip(got, raws):
        blocks = []
        for ft, fr in ysfpic._blocks(frames):
            blocks.append(b"".join(fr[k] for k in range(ft + 1)) if all(fr.get(k) is not None for k in range(ft + 1)) else None)
        out.append((blocks, raw))
    return out


def cmd_check(path):
    streams = _captured_streams(path)
    bad = 0
    for i, (blocks, raw) in enumerate(streams):
        if any(b is None for b in blocks):
            print("stream %d: frames lost in the capture, skipped" % (i + 1))
            continue
        mine = stream_frames(blocks)
        ok = mine == raw
        bad += 0 if ok else 1
        print("stream %d: %d blocks, %d frames: %s" % (
            i + 1, len(blocks), len(raw), "frames match the capture byte for byte" if ok else "FRAMES DIFFER"))
    if not streams:
        print("no streams found")
        return 1
    # rebuild the records from the fields the capture shows
    recs = [b for blocks, _ in streams for b in blocks if b is not None]
    gh = next((b for b in recs if b[61:64] == b"Gh_"), None)
    nd = next((b for b in recs if b[61:64] == b"Nd_"), None)
    if gh and nd:
        call = gh[10:20].decode("latin-1").strip()
        rid = gh[5:10].decode("latin-1")
        p = ysfpos.decode_block(gh[64:83])
        seq = int(gh[83:88])
        sa, sb = gh[89:99].decode(), gh[101:111].decode()
        to_id, to_call = gh[113:118].decode(), gh[118:128].decode()
        shown = gh[128:144].decode()
        b60 = gh[60]
        size = int.from_bytes(nd[88:92], "big")
        stamp14 = nd[92:106].decode()
        name = nd[107:122].decode()
        g2 = gh_block(call, rid, b60, seq, sa, sb, to_id, to_call, shown, p["lat"], p["lon"], p["alt_m"] or 0,
                      p["speed_kn"], p["course"])
        try:
            p2 = ysfpos.decode_block(b"3" + nd[122:140])
            pos2 = (p2["lat"], p2["lon"])
        except ValueError:
            pos2 = None
        pn = ysfpos.decode_block(nd[64:83])                  # each record carries its own GPS reading
        d2 = nd_block(call, rid, nd[60], pn["lat"], pn["lon"], size, stamp14, name, shown, pn["alt_m"] or 0,
                      pn["speed_kn"], pn["course"], pos2)
        print("Gh_ record rebuilt: %s" % ("matches" if g2 == gh else "DIFFERS"))
        print("Nd_ record rebuilt: %s" % ("matches" if d2 == nd else "DIFFERS"))
        bad += (g2 != gh) + (d2 != nd)
    chunks = {}
    for b in recs:
        if b[61:64] == b"Nb_":
            pass
    # Nb_ / Ne_: take the data back out of each chunk's blocks and rebuild
    i = 0
    nb_ok = nb_n = 0
    for blocks, _ in streams:
        if blocks and all(b is not None for b in blocks) and blocks[0][61:64] == b"Nb_":
            f = blocks[0]
            num = int.from_bytes(f[66:68], "big")
            ln = int.from_bytes(f[68:70], "big") or CHUNK
            data = f[70:70 + FIRST]
            for b in blocks[1:]:
                data += b[61:]
            data = data[:ln]
            mine = nb_blocks(f[10:20].decode().strip(), f[5:10].decode(), f[60], num, data)
            nb_n += 1
            same = (len(mine) == len(blocks) and all(m == b for m, b in zip(mine, blocks)))
            nb_ok += 1 if same else 0
            if not same:
                print("  Nb_ chunk %d differs (built %s blocks, captured %s)" % (num, [len(m) for m in mine], [len(b) for b in blocks]))
    print("Nb_ chunks rebuilt: %d of %d match" % (nb_ok, nb_n))
    bad += nb_n - nb_ok
    ne = next((b for b in recs if b[61:64] == b"Ne_"), None)
    if ne:
        n2 = ne_block(ne[10:20].decode().strip(), ne[5:10].decode(), ne[60], int.from_bytes(ne[66:68], "big"),
                      int.from_bytes(ne[70:72], "big"), int.from_bytes(ne[68:70], "big") // 2 + 1)
        print("Ne_ record rebuilt: %s" % ("matches" if n2 == ne else "DIFFERS"))
        bad += n2 != ne
    return 1 if bad else 0


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--check", metavar="CAPTURE", required=True,
                    help="ysfcap file of your radio sending a picture; rebuild it and compare")
    a = ap.parse_args()
    sys.exit(cmd_check(a.check))


if __name__ == "__main__":
    main()
