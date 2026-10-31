# GPS block: controlled test

Goal: find out how the 19-byte GPS block in a GPS-attached text message encodes latitude, longitude and the other fields. Method: send messages with known manual positions that change one digit at a time, then compare the blocks.

## Radio

1. APRS menu, item 24 MY POSITION: set it to Manual and enter the position on the `LAT` and `LON` lines (degrees, minutes to two decimals). The `('00")` line below each is the seconds form of the same value; leave it alone.
2. Send every message as a text to ALL with the position attached, the same way as the earlier GPS test.
3. Use the text `T01`, `T02`, ... so each message can be matched to its line below.

The manual entry resolves 0.01 minute (about 18 m). If the block does not change when the manual position changes, the radio only attaches a real fix. Stop and tell me.

## Capture on the Pi

One run for the whole series:

```
sudo python3 ysfcap.py --call YOURCALL --label GPS --out cap_GPS.jsonl
```

Send the 20 messages in order, wait about 5 seconds between them, then press Ctrl+C.

## Positions

`gps_truth_template.txt` holds them in sending order (20 lines):

| # | Latitude | Longitude |
|---|---|---|
| 1 | N 0°00.00 | E 0°00.00 |
| 2 | N 0°00.01 | E 0°00.00 |
| 3 | N 0°00.02 | E 0°00.00 |
| 4 | N 0°00.10 | E 0°00.00 |
| 5 | N 0°01.00 | E 0°00.00 |
| 6 | N 0°10.00 | E 0°00.00 |
| 7 | N 1°00.00 | E 0°00.00 |
| 8 | N 10°00.00 | E 0°00.00 |
| 9 | N 54°14.95 | E 0°00.00 |
| 10 | S 54°14.95 | E 0°00.00 |
| 11 | N 0°00.00 | E 0°00.01 |
| 12 | N 0°00.00 | E 0°00.02 |
| 13 | N 0°00.00 | E 0°00.10 |
| 14 | N 0°00.00 | E 0°01.00 |
| 15 | N 0°00.00 | E 0°10.00 |
| 16 | N 0°00.00 | E 1°00.00 |
| 17 | N 0°00.00 | E 10°00.00 |
| 18 | N 0°00.00 | E 100°00.00 |
| 19 | N 0°00.00 | W 0°23.42 |
| 20 | N 54°14.95 | W 0°23.42 |

Messages 1 to 10 are enough to decode latitude. If the radio refuses an all-zero position, use N 10°00.00 / E 10°00.00 as the base and change the lines in the truth file to match.

## Analyse

```
python3 ysfgps.py cap_GPS.jsonl --truth gps_truth_template.txt
```

Send me the output. The tool lists each block, which byte positions changed against the first message and against the previous one, and the distinct values seen per position. It checks that the number of GPS messages equals the number of lines in the truth file.

## Optional: altitude

With a real GPS fix, send 3 messages a minute apart and note the altitude the radio displays each time. Add them as the last number on each truth line, for example `N 54 14.95  W 0 23.42  93`.

## Current observations (5 records, real fix)

- Within the 19-byte block (index 0 is a leading `3`), bytes 6 and 9 step by one now and then, and bytes 11 and 12 change on every message. This fits the low digits of a drifting position.
- Byte 17 took the values 0x65, 0x7b, 0x61 against displayed altitudes of about 93 m and 74 m for the last two. This is not yet a fit.
- Bytes 0 to 5, 7, 8, 10, 13 to 16 and 18 were constant in all five records.
