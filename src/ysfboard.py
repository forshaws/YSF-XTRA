#!/usr/bin/env python3
"""ysfboard: list, add, edit and delete items on the News Station board.

Needs ysfmsgstore.py in the same folder. Python 3.5+, standard library only.
Run it as the service user so the file keeps its owner:

    sudo -u ysfdash python3 /opt/ysfdash/ysfboard.py list
    sudo -u ysfdash python3 /opt/ysfdash/ysfboard.py add N0CALL "Net moved to Wednesday"
    sudo -u ysfdash python3 /opt/ysfdash/ysfboard.py edit 3 "New wording"
    sudo -u ysfdash python3 /opt/ysfdash/ysfboard.py del 3
    sudo -u ysfdash python3 /opt/ysfdash/ysfboard.py clear

Text is printable ASCII, at most 80 characters (shown to radios as "CALL dd/mm: text",
which is itself cut to 80). Changes apply to the next download, with no restart.
"""

import argparse
import json
import os
import sys
import time

import ysfmsgstore as S

DEFAULT = "/var/lib/ysfradio/newsboard.json"


def load(path):
    try:
        with open(path) as f:
            json.load(f)
    except OSError:
        pass                                     # no file yet: start empty
    except ValueError:
        raise SystemExit("%s is not valid JSON; not changing it" % path)
    msgs, nxt = S._load(path)
    return msgs, nxt


def save(path, msgs, nxt):
    S._write(path, nxt, msgs[-S.MAX_MSGS:])


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--file", default=DEFAULT)
    sub = ap.add_subparsers(dest="cmd")
    sub.required = True
    sub.add_parser("list")
    p = sub.add_parser("add")
    p.add_argument("call")
    p.add_argument("text")
    p = sub.add_parser("edit")
    p.add_argument("id", type=int)
    p.add_argument("text")
    p = sub.add_parser("del")
    p.add_argument("id", type=int)
    sub.add_parser("clear")
    a = ap.parse_args()
    with S._FileLock(a.file):
        return run(a)


def run(a):
    msgs, nxt = load(a.file)
    if a.cmd == "list":
        if not msgs:
            print("(board is empty)")
        for m in msgs:
            print("%4d  %s  %-8s  %s" % (m["id"], time.strftime("%d/%m %H:%M", time.localtime(m["t"])),
                                         m["call"], m["text"]))
        return 0
    if a.cmd == "add":
        call, text = a.call.upper(), S.clean_text(a.text)
        if not S.CALL_RE.match(call) or not text:
            raise SystemExit("need a valid callsign and non-empty printable ASCII text")
        now = time.time()
        msgs.append({"id": nxt, "t": round(now, 1), "call": call, "text": text, "k": "manual|%d" % nxt})
        save(a.file, msgs, nxt + 1)
        print("added as id %d" % nxt)
        return 0
    if a.cmd == "edit":
        text = S.clean_text(a.text)
        hit = [m for m in msgs if m["id"] == a.id]
        if not hit or not text:
            raise SystemExit("no item %d, or empty text" % a.id)
        hit[0]["text"] = text
        save(a.file, msgs, nxt)
        print("edited id %d" % a.id)
        return 0
    if a.cmd == "del":
        keep = [m for m in msgs if m["id"] != a.id]
        if len(keep) == len(msgs):
            raise SystemExit("no item %d" % a.id)
        save(a.file, keep, nxt)
        print("deleted id %d" % a.id)
        return 0
    save(a.file, [], nxt)
    print("board cleared")
    return 0


if __name__ == "__main__":
    sys.exit(main())
