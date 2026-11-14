#!/usr/bin/env python3
"""ysfimg: make a radio-sized picture from any image and send it to a radio through the reflector.

Resizes an image to 160x120 or 320x240 (the two sizes a camera-microphone radio uses), saves it as a
baseline JPEG at LOW, MEDIUM or HIGH quality, and sends it as a C4FM picture transfer to one radio
or to everyone, as a client logged in under your own callsign and radio ID.

Needs Pillow (sudo apt install python3-pil, or pip install pillow) and ysfnews.py, ysfpicsend.py,
ysfsend.py, ysfpic.py, ysfpos.py, ysfdec.py and ysfmsgstore.py in the same folder. Python 3.5+.
Sends only under the callsign and radio ID you give it - use your own.

  1. Dry run: builds the picture, saves it for you to look at, sends nothing:
       python3 ysfimg.py photo.jpg --size 160x120 --quality medium --save preview.jpg \\
           --call N0CALL --radio-id 12345 --to-call M0RWV --to-id EB4XU --dry out.jsonl

  2. Live, to one radio (logs in to the reflector, sends, logs out):
       python3 ysfimg.py photo.jpg --size 160x120 --quality medium --live \\
           --host 127.0.0.1 --port 42002 --call N0CALL --radio-id 12345 --to-call M0RWV --to-id EB4XU

A picture to everyone on the reflector needs --all instead of --to-call and --to-id. LOW, MEDIUM and
HIGH give about the file sizes the radio's own LOW (2.5 KB at 160x120) and HIGH (18 KB at 320x240)
produce; the time on air is printed before anything is sent. The image's EXIF data (including any
GPS position) is not copied into the JPEG.
"""

import argparse
import json
import os
import sys
import time

import ysfmsgstore
import ysfnews
import ysfpic
import ysfpicsend

SIZES = {"160": (160, 120), "160x120": (160, 120), "320": (320, 240), "320x240": (320, 240)}
QUALITY = {"low": 45, "medium": 70, "high": 90}          # JPEG quality (Pillow scale)
GAP = 1.5                                                  # seconds between the streams of a picture (0.7 s and less did not arrive on radios)
FRAME_S = 0.1                                              # seconds per frame on air


def upright(im):
    """Turn a photo upright from its EXIF orientation; works on old and new Pillow."""
    from PIL import Image, ImageOps
    if hasattr(ImageOps, "exif_transpose"):
        return ImageOps.exif_transpose(im)
    try:
        o = (im._getexif() or {}).get(274)
    except Exception:
        o = None
    ops = {2: "FLIP_LEFT_RIGHT", 3: "ROTATE_180", 4: "FLIP_TOP_BOTTOM", 5: "TRANSPOSE",
           6: "ROTATE_270", 7: "TRANSVERSE", 8: "ROTATE_90"}
    m = getattr(Image, ops.get(o, ""), None)
    return im.transpose(m) if m is not None else im


def avoid_short_tail(jpeg):
    """Pad the JPEG with a comment segment if its last 1 KB chunk would be tiny.

    A last chunk under about 250 bytes goes out as one short block, a layout never checked against a
    real radio (the radio's own pictures of 511 and 619 byte last chunks were). The comment is ignored
    by every decoder."""
    r = len(jpeg) % ysfpicsend.CHUNK
    if r == 0 or r >= 300:
        return jpeg
    add = max(300 - r, 4)                                  # total bytes to add, including the 4 byte header
    seg = b"\xff\xfe" + (add - 2).to_bytes(2, "big") + b" " * (add - 4)
    return jpeg[:20] + seg + jpeg[20:]                     # after SOI (2) + APP0 (2 + 16)


def make_jpeg(path, size, quality, fit="crop"):
    """Any image file -> baseline JPEG bytes of exactly `size`, no metadata."""
    try:
        from PIL import Image, ImageOps
    except ImportError:
        raise SystemExit("needs Pillow: sudo apt install python3-pil   (or: pip install pillow)")
    import io
    try:
        im = Image.open(path)
        im.load()
    except (OSError, ValueError) as e:
        raise SystemExit("cannot read %s as an image: %s" % (path, e))
    im = upright(im)                                       # phone photos: turn them upright
    if im.mode in ("RGBA", "LA", "P"):
        im = im.convert("RGBA")
        bg = Image.new("RGBA", im.size, (255, 255, 255, 255))
        im = Image.alpha_composite(bg, im)
    im = im.convert("RGB")
    if fit == "pad":
        k = min(size[0] / float(im.size[0]), size[1] / float(im.size[1]))
        w, h = max(1, int(round(im.size[0] * k))), max(1, int(round(im.size[1] * k)))
        im = im.resize((min(w, size[0]), min(h, size[1])), Image.LANCZOS)
        bg = Image.new("RGB", size, (0, 0, 0))
        bg.paste(im, ((size[0] - im.size[0]) // 2, (size[1] - im.size[1]) // 2))
        im = bg
    else:
        im = ImageOps.fit(im, size, method=Image.LANCZOS, centering=(0.5, 0.5))
    out = io.BytesIO()
    im.save(out, "JPEG", quality=QUALITY[quality], subsampling=2, optimize=False, progressive=False)
    jpeg = avoid_short_tail(out.getvalue())
    if jpeg[:4] != b"\xff\xd8\xff\xe0" or jpeg[-2:] != b"\xff\xd9" or len(jpeg) > ysfpic.MAX_BYTES:
        raise SystemExit("internal error: the JPEG is not a baseline JFIF file of a sendable size")
    return jpeg


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("image", help="image file (JPEG, PNG, ... anything Pillow reads)")
    ap.add_argument("--size", default="160x120", choices=sorted(SIZES), help="160x120 (default) or 320x240")
    ap.add_argument("--quality", default="medium", choices=sorted(QUALITY), help="low, medium (default) or high")
    ap.add_argument("--fit", default="crop", choices=("crop", "pad"),
                    help="crop = fill the picture and trim the edges (default); pad = keep it all, black bars")
    ap.add_argument("--save", metavar="FILE", help="also write the resized JPEG to FILE")
    ap.add_argument("--call", help="your callsign")
    ap.add_argument("--radio-id", help="your radio ID (5 characters)")
    ap.add_argument("--to-call", help="destination callsign")
    ap.add_argument("--to-id", help="destination radio ID (5 characters)")
    ap.add_argument("--all", action="store_true", help="send to everyone on the reflector instead of one radio")
    ap.add_argument("--lat", type=float, default=0.0, help="latitude the picture is sent from (default 0)")
    ap.add_argument("--lon", type=float, default=0.0, help="longitude the picture is sent from (default 0)")
    ap.add_argument("--gateway", default=None, help="gateway name on the wire (default: --call)")
    ap.add_argument("--dst", default="ALL", help="network destination field (default ALL)")
    ap.add_argument("--dry", metavar="FILE", help="write the packets to FILE (ysfcap format) and exit")
    ap.add_argument("--live", action="store_true", help="actually send to the reflector")
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=42002)
    ap.add_argument("--gap", type=float, default=GAP,
                    help="seconds between the streams of the picture (default %.1f; radios dropped pictures sent with 0.7 s or less)" % GAP)
    ap.add_argument("--seq-file", default=os.path.expanduser("~/.ysfsend_seq"))
    a = ap.parse_args()

    if not (a.dry or a.live):
        if not a.save:
            ap.error("choose --dry FILE, --live or --save FILE")
    size = SIZES[a.size]
    jpeg = make_jpeg(a.image, size, a.quality, a.fit)
    if a.save:
        with open(a.save, "wb") as f:
            f.write(jpeg)
        print("saved %s: %dx%d, %s quality, %d bytes" % (a.save, size[0], size[1], a.quality, len(jpeg)))
    if not (a.dry or a.live):
        return 0

    call = (a.call or "").upper()
    if not ysfmsgstore.CALL_RE.match(call):
        ap.error("--call (your callsign) is required")
    if not a.radio_id or not ysfmsgstore.RID_RE.match(a.radio_id):
        ap.error("--radio-id must be exactly 5 letters or digits")
    if a.all:
        if a.to_call or a.to_id:
            ap.error("--all cannot be combined with --to-call / --to-id")
        to_call, to_id = "*" * 10, ""
    else:
        to_call = (a.to_call or "").upper()
        if not ysfmsgstore.CALL_RE.match(to_call) or not a.to_id or not ysfmsgstore.RID_RE.match(a.to_id):
            ap.error("give the radio to send to with --to-call and --to-id, or use --all for everyone")
        to_id = a.to_id
    gw = (a.gateway or call).upper()
    sender = ysfnews.Sender({"gw": gw, "dst": a.dst.upper()}, call, a.radio_id, a.host, a.port, a.seq_file, a.gap)
    streams = sender.pic_packets(jpeg, time.time(), to_call, to_id, a.lat, a.lon)
    npk = sum(len(s) for s in streams)
    secs = npk * FRAME_S + len(streams) * a.gap
    print("picture: %dx%d %s, %d bytes, %d chunks, %d frames, about %d s on air, to %s" %
          (size[0], size[1], a.quality, len(jpeg), -(-len(jpeg) // 1024), npk, round(secs),
           "everyone" if a.all else "%s %s" % (to_call, to_id)))
    if a.lat == 0.0 and a.lon == 0.0:
        print("note: sent from position 0,0; add --lat and --lon to send from your own position")

    if a.dry:
        t0 = time.time()
        k = 0
        with open(a.dry, "w") as f:
            for st in streams:
                for p in st:
                    f.write(json.dumps({"t": round(t0 + FRAME_S * k, 3), "label": "DRY", "gw": gw, "src": call,
                                        "dst": a.dst.upper(), "ctr": p[34], "data": p[35:].hex()}) + "\n")
                    k += 1
                k += int(a.gap / FRAME_S)
        print("wrote %d frames to %s (nothing sent)" % (npk, a.dry))
        return 0

    n = sender._deliver(streams, a.gap)
    if n:
        print("sent %d streams as %s -> %s (gateway %s)" % (n, call, "everyone" if a.all else to_call, gw))
        return 0
    print("nothing was sent")
    return 1


if __name__ == "__main__":
    sys.exit(main())
