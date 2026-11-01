# News Station responder (ysfnews)

Answers a radio's WIRES-X News Station "download, message" request with text messages from a news file, and "download, picture" with the newest picture uploaded to the News Station. The radio jumps to its message log and shows the messages.

How it works:
- `ysfmsgd.py` (with `--news-out`) recognises the download request and queues only the requester's callsign and radio ID in `/var/lib/ysfradio/newsreq.json`.
- `ysfnews.py` reads the queue and sends each line of `news.txt` to that callsign and radio ID as a directed text message, under the callsign and radio ID you give it (`--call`, `--radio-id`). Use your own or your club's, and check that automatic messages are allowed under your licence terms.
- A radio's News Station upload (a text message addressed to the room ID) is kept as a public news item: callsign, time and text. The next radios that download get the latest items after the lines of `news.txt`, as `CALL dd/mm: text`.
- Other directed messages are discarded unread and never shown on the dashboard.
- A "download, picture" request is queued separately (`ysfmsgd --picreq-out`, `/var/lib/ysfradio/picreq.json`). `ysfnews.py --pic-queue` sends the newest picture a whitelisted radio uploaded to the News Station (see README section 7) to the requester, as a directed picture transfer built by `ysfpicsend.py`, under the same callsign and radio ID as the messages. With no picture yet, the radio gets the text `NO NEWS PICTURE YET`. This reply is experimental: it is built to match captures of an FT5D sending pictures, and the radio's acceptance of it as the answer to a download is being tested.
- Voice downloads are not answered.

## Install (on the reflector host)

From the repository folder:

```
sudo cp src/ysfdec.py src/ysfpos.py src/ysfsend.py src/ysfmsgstore.py src/ysfpic.py src/ysfpicsend.py src/ysfmsgd.py src/ysfnews.py src/ysfboard.py /opt/ysfdash/
sudo cp examples/news.txt /var/lib/ysfradio/news.txt
sudo cp cap_HELLO.jsonl /var/lib/ysfradio/news-template.jsonl
sudo chown ysfdash:ysfdash /var/lib/ysfradio/news.txt /var/lib/ysfradio/news-template.jsonl
sudoedit systemd/ysfmsg.service systemd/ysfnews.service     # room ID, callsign, radio ID
sudo cp systemd/ysfmsg.service systemd/ysfnews.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl restart ysfmsg
sudo systemctl enable --now ysfnews
```

For the dashboard list and delete, add `--news` to the `ysfradio.py` line in `ysfradio.service` and restart `ysfradio`.

- `cap_HELLO.jsonl` is a ysfcap capture of a plain text message (no GPS, 7 frames) sent from your radio. It supplies the frame headers.
- `--call` and `--radio-id` in `ysfnews.service` are the callsign and 5-character radio ID the messages are sent as. Use your own or your club's. The supplied unit has placeholders.
- `--board-room-id` in `ysfmsg.service` is the room ID your radios put in an upload as the destination ID. To find it, capture one upload (`sudo python3 ysfcap.py --call YOURCALL --out up.jsonl`, press UPLOAD on the radio, Ctrl+C) and run `python3 ysfdec.py --records up.jsonl`: the last record shows `id='NNNNN'`. Without `--board-out` and `--board-room-id`, uploads are discarded.
- If `/opt/ysfdash/ysfsend.py` is your own modified copy, `ysfnews.py` needs only `build_record`, `build_frames`, `packet`, `load_template`, `next_seq`, `ctr_for` and `NFRAMES` from it.

## news.txt

One message per line, at most 80 printable ASCII characters. Blank lines and lines starting with `#` are ignored. `{call}` is replaced by the requester's callsign. The file is read on every request.

## Reading and deleting on the dashboard

With `--news` on `ysfradio.py`, MENU > LOG > NEWS lists the uploads. An admin (MENU > ADMIN) gets a `DEL` key to delete the selected item; the same applies to LOG > MESSAGE. See `README-messages.md`.

## Editing the board

```
sudo -u ysfdash python3 /opt/ysfdash/ysfboard.py list
sudo -u ysfdash python3 /opt/ysfdash/ysfboard.py add N0CALL "Net moved to Wednesday"
sudo -u ysfdash python3 /opt/ysfdash/ysfboard.py edit 3 "New wording"
sudo -u ysfdash python3 /opt/ysfdash/ysfboard.py del 3
sudo -u ysfdash python3 /opt/ysfdash/ysfboard.py clear
```

Changes apply to the next download, with no restart. Install: `sudo cp ysfboard.py /opt/ysfdash/`.

## Options (ysfnews.py)

| Option | Default | Meaning |
|---|---|---|
| `--template FILE` | required | ysfcap file with a real 7-frame text message |
| `--call`, `--radio-id` | required | identity the messages are sent as |
| `--queue FILE` | `/var/lib/ysfradio/newsreq.json` | request queue written by ysfmsgd |
| `--news FILE` | `/var/lib/ysfradio/news.txt` | message lines |
| `--host`, `--port` | `127.0.0.1`, `42002` | the reflector |
| `--cooldown S` | 120 | seconds before the same radio is answered again |
| `--per-hour N` | 30 | most requests answered per hour, all radios |
| `--max-lines N` | 5 | most messages per request |
| `--delay S`, `--gap S` | 1.5, 4.0 | wait before replying, wait between messages (an FT5D dropped messages sent 1 s apart) |
| `--board FILE` | `/var/lib/ysfradio/newsboard.json` | uploads written by ysfmsgd (missing = none) |
| `--board-lines N` | 5 | most uploads sent per request (0 = none) |
| `--pic-queue FILE` | off | picture download queue written by `ysfmsgd --picreq-out`; turns on picture replies (needs `--lat` and `--lon`) |
| `--pictures-dir DIR` | `/var/lib/ysfradio/pictures` | picture folder written by `ysfmsgd` |
| `--lat`, `--lon` | none | position the picture is sent from (your node's, in decimal degrees) |
| `--pic-per-hour N` | 10 | most picture requests answered per hour, all radios |
| `--dry-run` | off | log what would be sent, send nothing |

Options (ysfmsgd.py): `--picreq-out FILE` (queue for picture downloads), `--board-out FILE`, `--board-room-id ID`, `--board-per-hour N` (default 3 per callsign), `--board-days N` (default 30). At most 20 items are kept.

## Check it

```
journalctl -u ysfmsg -u ysfnews -f
```

On an upload: `ysfmsgd: news upload from N0CALL`. On a download: `ysfmsgd: news request from N0CALL (payload '31456117989120     0')`, then `ysfnews: sent 3 of 3 message(s) to N0CALL 12345`.

Requests made before the service started are not answered. A radio asking again inside the cooldown is ignored.
