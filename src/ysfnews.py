#!/usr/bin/env python3
"""ysfnews: answer News Station downloads on the reflector with text messages.

When a radio asks the reflector for a News Station "download - message", ysfmsgd.py
queues the requester's callsign and radio ID (--news-out). This service reads the
queue and sends the lines of a news file to that radio as ordinary C4FM text messages,
addressed to its callsign and radio ID. The radio jumps to its message log.

Needs ysfsend.py, ysfdec.py and ysfmsgstore.py in the same folder. Python 3.5+, stdlib
only. No privileges needed: it sends UDP to the reflector like any client (--host
127.0.0.1 on the reflector's own machine) and logs in under --call.

Sends only under the callsign and radio ID you give it - use your own or your club's.
Check that automatic messages are allowed under your licence terms.

Uploads from radios (ysfmsgd --board-out) are appended after the news file, newest last,
as "CALL dd/mm: text".

news file: one message per line, at most 80 printable ASCII characters; blank lines and
lines starting with # are ignored. {call} is replaced by the requester's callsign.
The file is read on every request, so edits take effect at once.
"""

import argparse
import collections
import os
import socket
import sys
import time

import ysfmsgstore
import ysfsend as S


def log(msg):
    sys.stderr.write("ysfnews: %s\n" % msg)
    sys.stderr.flush()


def read_news(path, call, max_lines):
    """Lines of the news file -> list of message texts (printable ASCII, <= 80 characters)."""
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as f:
            raw = f.read(16384)
    except OSError:
        return []
    out = []
    for ln in raw.splitlines():
        ln = ln.strip()
        if not ln or ln.startswith("#"):
            continue
        ln = ysfmsgstore.clean_text(ln.replace("{call}", call))
        if ln:
            out.append(ln)
        if len(out) >= max_lines:
            break
    return out


def read_board(path, n):
    """Newest n uploads as 'CALL dd/mm: text' (<= 80 characters), oldest first, so the
    newest ends up on top of the radio's message list."""
    if not path or n < 1:
        return []
    out = []
    for m in ysfmsgstore.MsgReader(path).get()[:n]:          # newest first
        head = "%s %s: " % (m["call"], time.strftime("%d/%m", time.localtime(m["t"])))
        out.append(ysfmsgstore.clean_text(head + m["text"]))
    return out[::-1]


class Sender(object):
    """Builds and sends directed text messages as one logged-in session."""

    def __init__(self, tpl, call, rid, host, port, seq_file, gap):
        self.tpl, self.call, self.rid = tpl, call, rid
        self.addr = (host, port)
        self.seq_file = seq_file
        self.gap = gap
        self.gw = tpl["gw"].upper()
        self.dst = tpl["dst"].upper()

    def packets(self, text, to_call, to_id):
        seq = S.next_seq(self.seq_file)
        rec = S.build_record(text, seq, time.strftime("%y%m%d%H%M"), self.call, self.rid,
                             to_call, to_id)
        frames = S.build_frames(rec, self.tpl["fich"])
        return [S.packet(self.gw, self.call, self.dst, k, frames[k]) for k in range(S.NFRAMES)]

    def send(self, texts, to_call, to_id):
        """Log in, send every text, log out. Returns the number sent."""
        pkts = [self.packets(t, to_call, to_id) for t in texts]
        poll = b"YSFP" + self.call.ljust(10)[:10].encode("ascii")
        bye = b"YSFU" + self.call.ljust(10)[:10].encode("ascii")
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.settimeout(3)
        try:
            s.sendto(poll, self.addr)
            try:
                r, _ = s.recvfrom(1024)
            except socket.timeout:
                log("no reply from the reflector at %s:%d, nothing sent" % self.addr)
                return 0
            if r[:4] != b"YSFP":
                log("unexpected reply %r, nothing sent" % r[:8])
                return 0
            time.sleep(0.3)
            sent = 0
            for group in pkts:
                for p in group:
                    s.sendto(p, self.addr)
                    time.sleep(0.1)
                sent += 1
                time.sleep(self.gap)
            s.sendto(bye, self.addr)
            return sent
        finally:
            s.close()


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--template", required=True,
                    help="ysfcap file with a real 7-frame GPS-off text message from your radio")
    ap.add_argument("--call", required=True, help="callsign the messages are sent as (yours or the club's)")
    ap.add_argument("--radio-id", required=True, help="5-character radio ID the messages are sent as")
    ap.add_argument("--queue", default="/var/lib/ysfradio/newsreq.json")
    ap.add_argument("--news", default="/var/lib/ysfradio/news.txt")
    ap.add_argument("--board", default="/var/lib/ysfradio/newsboard.json",
                    help="News Station uploads written by ysfmsgd (missing file = none)")
    ap.add_argument("--board-lines", type=int, default=5, help="most uploads sent per request (0 = none)")
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=42002)
    ap.add_argument("--seq-file", default="/var/lib/ysfradio/news-seq")
    ap.add_argument("--cooldown", type=int, default=120, help="seconds before the same radio is answered again")
    ap.add_argument("--per-hour", type=int, default=30, help="most requests answered per hour, all radios")
    ap.add_argument("--max-lines", type=int, default=5, help="most messages per request")
    ap.add_argument("--delay", type=float, default=1.5, help="seconds to wait after a request before replying")
    ap.add_argument("--gap", type=float, default=4.0, help="seconds between messages (a radio drops texts sent closer together)")
    ap.add_argument("--dry-run", action="store_true", help="log what would be sent, send nothing")
    a = ap.parse_args()

    call = a.call.upper()
    if not ysfmsgstore.CALL_RE.match(call):
        raise SystemExit("--call is not a valid callsign")
    if not ysfmsgstore.RID_RE.match(a.radio_id.upper()):
        raise SystemExit("--radio-id must be 5 letters or digits")
    tpl = S.load_template(a.template)
    if not tpl:
        raise SystemExit("no 7-frame transmission found in %s" % a.template)
    sender = Sender(tpl, call, a.radio_id.upper(), a.host, a.port, a.seq_file, a.gap)

    if not os.path.exists(a.seq_file):
        with open(a.seq_file, "w") as f:
            f.write("60000")                             # keeps clear of the numbers ysfsend.py uses
    reqs, nxt = ysfmsgstore.NewsQueue.load(a.queue)
    last_id = max([r["id"] for r in reqs] or [0])        # requests from before start-up are not answered
    seen = {}                                            # radio ID -> time answered
    hour = collections.deque()
    sig = None
    log("watching %s, news file %s, sending as %s %s" % (a.queue, a.news, call, a.radio_id.upper()))
    while True:
        try:
            st = os.stat(a.queue)
            cur = (st.st_mtime, st.st_size)
        except OSError:
            cur = None
        if cur != sig:
            sig = cur
            for r in ysfmsgstore.NewsQueue.read_after(a.queue, last_id):
                last_id = r["id"]
                now = time.time()
                if now - r["t"] > 30:
                    continue                             # stale
                while hour and now - hour[0] > 3600:
                    hour.popleft()
                if len(hour) >= a.per_hour:
                    log("hourly limit reached, ignoring request from %s" % r["call"])
                    continue
                if now - seen.get(r["rid"], 0) < a.cooldown:
                    log("%s asked again within %d s, ignored" % (r["call"], a.cooldown))
                    continue
                texts = read_news(a.news, r["call"], max(1, a.max_lines)) + read_board(a.board, a.board_lines)
                if not texts:
                    log("news file empty or missing, nothing to send to %s" % r["call"])
                    continue
                seen[r["rid"]] = now
                if len(seen) > 1024:
                    seen = {k: v for k, v in seen.items() if now - v < a.cooldown}
                hour.append(now)
                time.sleep(max(0.0, a.delay))
                if a.dry_run:
                    log("dry run: would send %d message(s) to %s %s: %r" % (len(texts), r["call"], r["rid"], texts))
                    continue
                n = sender.send(texts, r["call"], r["rid"])
                log("sent %d of %d message(s) to %s %s" % (n, len(texts), r["call"], r["rid"]))
        time.sleep(0.5)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        pass
