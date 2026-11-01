"""ysfpos: decode and build the 19-byte GPS block of a Yaesu C4FM text message.

The block (record bytes 64 to 82 in a GPS-attached message) is an APRS Mic-E position:

  byte  0      '3'                      constant, same byte as in a message without GPS
  bytes 1-6    Mic-E "destination" field: six latitude digits (DDMMmm) with flags; trailing
               digits may be blank (K, L or Z) when the radio sends a reduced-resolution position
  bytes 7-9    longitude degrees, minutes, hundredths of a minute (each +28)
  bytes 10-12  speed and course (each +28)
  byte  13     symbol code             '[' (person)
  byte  14     symbol table            '/' (primary)
  bytes 15-18  altitude: 3 base-91 characters then '}', or 4 spaces if none

Mic-E is published in the APRS 1.0.1 specification (chapter 10). Python 3.5+, no dependencies.
"""

BLOCK_LEN = 19


def _digit(ch):
    """Mic-E destination character -> (digit, flag); digit is None for a blank (position
    ambiguity). '0'-'9' flag 0; 'A'-'J' and 'P'-'Y' flag 1; 'L' blank flag 0;
    'K' and 'Z' blank flag 1."""
    if 0x30 <= ch <= 0x39:
        return ch - 0x30, 0
    if 0x41 <= ch <= 0x4A:
        return ch - 0x41, 1
    if 0x50 <= ch <= 0x59:
        return ch - 0x50, 1
    if ch == 0x4C:
        return None, 0
    if ch in (0x4B, 0x5A):
        return None, 1
    raise ValueError("not a Mic-E digit: 0x%02x" % ch)


def decode_block(b):
    """19 bytes -> dict(lat, lon, speed_kn, course, alt_m, symbol, msg_bits). lat/lon in
    signed decimal degrees (north and east positive). alt_m is None when absent."""
    if len(b) != BLOCK_LEN or b[0] not in (0x33, 0x25):      # '3' (FT5D, FT3D), '%' (FTM-400)
        raise ValueError("not a GPS block")
    dig, flag = zip(*[_digit(c) for c in b[1:7]])
    amb = 0                                   # blanked digits from the right (position ambiguity)
    while amb < 6 and dig[5 - amb] is None:
        amb += 1
    if any(x is None for x in dig[:6 - amb]):
        raise ValueError("blank digit inside the latitude")
    dig = [0 if x is None else x for x in dig]
    lat_min = dig[2] * 10 + dig[3] + (dig[4] * 10 + dig[5]) / 100.0
    if amb:
        lat_min += (10 ** amb) / 200.0         # centre of the area the blanks leave open
    lat = dig[0] * 10 + dig[1] + lat_min / 60.0
    if not flag[3]:
        lat = -lat
    lon_off = flag[4]
    west = flag[5]
    d = b[7] - 28
    if lon_off:
        d += 100
    if d >= 190:
        d -= 190
    elif d >= 180:
        d -= 80
    m = b[8] - 28
    if m >= 60:
        m -= 60
    h = b[9] - 28
    lon = d + (m + h / 100.0) / 60.0
    if west:
        lon = -lon
    sp, dc, se = b[10] - 28, b[11] - 28, b[12] - 28
    speed = sp * 10 + dc // 10
    if speed >= 800:
        speed -= 800
    course = (dc % 10) * 100 + se
    if course >= 400:
        course -= 400
    alt = None
    if b[18] == 0x7D and all(33 <= c <= 123 for c in b[15:18]):
        alt = (b[15] - 33) * 8281 + (b[16] - 33) * 91 + (b[17] - 33) - 10000
    return {"lat": lat, "lon": lon, "speed_kn": speed, "course": course, "alt_m": alt,
            "symbol": chr(b[14]) + chr(b[13]), "msg_bits": (flag[0], flag[1], flag[2]), "amb": amb}


def encode_block(lat, lon, speed_kn=0, course=0, alt_m=None, msg_bits=(1, 0, 1), symbol=b"/["):
    """Signed decimal degrees -> 19-byte block (position rounded to 0.01 minute).
    symbol = table byte then code byte, default primary-table person."""
    a = abs(lat)
    deg = int(a)
    mins = int(round((a - deg) * 6000.0))             # hundredths of a minute
    if mins >= 6000:
        deg, mins = deg + 1, mins - 6000
    if deg > 90 or (deg == 90 and mins):
        raise ValueError("latitude out of range")
    digs = [deg // 10, deg % 10, mins // 1000, (mins // 100) % 10, (mins // 10) % 10, mins % 10]
    o = abs(lon)
    ld = int(o)
    lm = int(round((o - ld) * 6000.0))
    if lm >= 6000:
        ld, lm = ld + 1, lm - 6000
    if ld >= 180:
        raise ValueError("longitude out of range")
    if ld <= 9:
        lon_off, lonb = 1, ld + 118
    elif ld <= 99:
        lon_off, lonb = 0, ld + 28
    elif ld <= 109:
        lon_off, lonb = 1, ld + 8
    else:
        lon_off, lonb = 1, ld - 72
    flags = [msg_bits[0], msg_bits[1], msg_bits[2], 1 if lat >= 0 else 0, lon_off, 1 if lon < 0 else 0]
    dest = bytes(((0x50 if f else 0x30) + dg) for dg, f in zip(digs, flags))
    mm, hh = lm // 100, lm % 100
    sp = int(speed_kn)
    if sp < 200:
        sp += 800                                      # the radio sends 0-199 kn as 800-999
    cr = int(course) % 360 + 400                       # and course with 400 added
    spd = [sp // 10, (sp % 10) * 10 + cr // 100, cr % 100]
    out = (b"3" + dest + bytes([lonb, (mm + 60 if mm < 10 else mm) + 28, hh + 28]) +
           bytes(c + 28 for c in spd) + bytes([symbol[1], symbol[0]]))
    if alt_m is None:
        out += b"    "
    else:
        v = int(round(alt_m)) + 10000
        if not 0 <= v < 91 ** 3:
            raise ValueError("altitude out of range")
        out += bytes([33 + v // 8281, 33 + (v // 91) % 91, 33 + v % 91, 0x7D])
    return out
