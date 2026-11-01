# C4FM picture format (Yaesu camera microphone)

How a picture sent from a Yaesu radio with a camera microphone travels through a YSF reflector. Read together with `C4FM-TEXT-MESSAGE-FORMAT.md`: a picture uses the same Data FR stream and record layout as a text message.

## 1. Status

Observed on an FT5D with a camera microphone, through a Pi-Star hotspot to a pYSFReflector, picture sent to ALL.

| Capture | Size | Quality | Result |
|---|---|---|---|
| PIC1 | 160x120 | LOW | 2559 bytes, 3 chunks, rebuilt and verified |
| PIC2 | 320x240 | HIGH | 18027 bytes, 18 chunks, rebuilt; 2 of 614 frames lost in transit, detected by the check sum |

Not captured: NORMAL quality, 160x120 HIGH, 320x240 LOW, FT3D, FTM400, a picture sent to a single callsign, and a radio downloading a picture from the News Station. Size and quality only change the number of chunks in the captures made.

## 2. Transport

A picture is a series of YSF data streams from one callsign. Each stream is a Data FR transmission (FICH `DT=1`): a header frame (`FI=0`), data frames (`FI=1`) and a terminator frame (`FI=2`). The frame counter byte of the YSFD packet steps by 2 and is odd on the terminator, so a lost frame shows as a gap in the counter.

Each data frame carries 40 bytes. Frames are grouped in blocks of `FT+1` frames (`FN=0` to `FT`): `FT=7` gives a 320-byte block; the last block of a stream may be shorter (`FT=1`, `FT=3`, `FT=4` seen). The frame number `FN` wraps to 0 at the start of each block, so one stream holds several blocks.

## 3. Block layout

| Offset | Content |
|---|---|
| 0-4 | `*****` |
| 5-9 | sender radio ID (case sensitive, e.g. `EB4XU`) |
| 10-19 | sender callsign, space padded |
| 20-54 | `M0RWV` repeated and padding, as in text records |
| 55-59 | sender radio ID again |
| 60 | record number byte (see section 4) |
| 61-63 | record tag |
| 64-65 | `3P` for `Nb_` and `Ne_` |
| 66-67 | chunk number, big endian |
| 68-69 | chunk length (`Nb_`) or second field (`Ne_`) |
| 70-... | JPEG data (`Nb_`) |

The first block of a record carries the full header above. Continuation blocks of the same record repeat bytes 0-59 with byte 60 = `0x00` and carry data from offset 61. The `FN=0` frame of a continuation block is header only.

## 4. Records

One picture is sent as this sequence of streams:

| Order | Tag | Content |
|---|---|---|
| 1 | `Gh_` | Info record: position block, sender time stamp, destination `**********`, text of the capture time (`2026/10/31 19:21`), ETX, check byte. One 4-frame block. |
| 2 | `Nd_` | Description record: position block, then `P`, file size, time stamp, file name, capture time text, ETX, check byte. One 4-frame block. |
| 3... | `Nb_` | Data chunks 1 to N. |
| last | `Ne_` | End record. |

Byte 60 of the first block differs by record: PIC1 `00 / 03 / 04 / 05` for `Gh_ / Nd_ / Nb_ / Ne_`, PIC2 `41 / 44 / 45 / 46`. The steps between records are the same; the base differs between transfers (0x00, 0x41).

`Nd_` fields (offsets in the first block):

| Offset | Content |
|---|---|
| 83 | `P` |
| 88-91 | JPEG size in bytes, big endian (`0x09FF` = 2559, `0x466B` = 18027) |
| 92-105 | time stamp `YYYYMMDDhhmmss` |
| 106 | `H` (the same in the 160x120 LOW and 320x240 HIGH captures) |
| 107-121 | file name: radio ID + 6-digit picture number + `.jpg` (`EB4XU000001.jpg`, `EB4XU000002.jpg`) |

## 5. Data chunks (`Nb_`)

- Chunks are numbered in bytes 66-67. The first chunk is 1 in the PIC1 and PIC2 captures, but later pictures from the same radio started at 36 and 46 (3 chunks each), so the start number varies.
- Each chunk carries 1024 bytes of the JPEG, spread over four blocks: 250 bytes in the first block (offset 70-319) and 259 bytes in each of the next three (offset 61-319). The last chunk carries the remainder (619 bytes in PIC2).
- Bytes 68-69 hold the chunk length: `0000` for a full 1024-byte chunk, the actual length for the last chunk (`01FF` = 511 in PIC1, `026B` = 619 in PIC2).
- After the data come `0x03` (ETX), a check byte and `0x00` padding.
- The check byte is the sum of the bytes from offset 22 of the first block up to the ETX, modulo 256, taking the data bytes of the continuation blocks without their repeated headers. This matches the text message rule. It held for the PIC1 chunk tested.

## 6. End record (`Ne_`)

| Offset | Content |
|---|---|
| 66-67 | last chunk number + 1 (the picture's chunks run from its first chunk number to this value minus 1) |
| 68-69 | second field: `0004` for PIC1 (3 chunks), `0022` for PIC2 (18 chunks), equal to 2 x (chunks - 1) in both |
| 70-71 | 16-bit sum of all JPEG bytes, big endian (`0xA31A`, `0xDF86`) |
| 72 | ETX `03` |
| 73 | check byte |

## 7. Picture data

The concatenated chunk data is a baseline JPEG: it starts `FF D8 FF E0` (JFIF) and ends `FF D9`. PIC1 is 160x120, PIC2 is 320x240.

## 8. Receiving a picture

1. Collect the `FI=1` frames of each stream, in counter order. Count missing frames from gaps in the counter.
2. Split into blocks at `FN=0`. A block whose bytes 61-65 are `Nb_3P` starts a chunk. Take the chunk number and length, then append 250 bytes from the first block and 259 bytes from each continuation block, up to the chunk length.
3. On `Ne_`, join the chunks from the first received number to (end number - 1), compare the 16-bit sum with bytes 70-71, and check the JPEG starts `FF D8` and ends `FF D9`.
4. A missing frame inside a chunk leaves 40 bytes unknown. The sum then differs from the end record. The picture can still be shown with a glitch band; mark it damaged.
5. A missing `Ne_`, or a chunk lost entirely, leaves no complete picture.

`src/ysfpic.py` implements this; `src/ysfmsgd.py` feeds it.

## 9. Unknown

- Meaning of byte 60 and of the second `Ne_` field (it matched 2 x (chunks - 1) in both captures).
- What the `H` at `Nd_` offset 106 selects.
- What a radio sends to ask the News Station for a picture and what it expects back.
- Behaviour for pictures sent to a single callsign or to a room.
- Whether the FT3D and FTM400 send the same records.
