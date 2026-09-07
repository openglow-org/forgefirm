"""Pulse files for the offline cloud service: a job the machine will run
without the service having cut it.

A pulse file is a header (`\\x80GF1`, a little-endian total length, then
8-byte records of a 4-character tag and a uint32) followed by the step
stream, one byte per tick of the header's STfr (the kernel feeder
contract: bit 0 X step, bit 1 X dir, bit 2 Y step, bit 3 Y dir, bit 4
LASER, bits 5-6 Z, bit 7 marks a power byte whose low 7 bits set the
duty). The header here is the one a factory print of this machine type
carries (134 tags, MCsn 0 = not locked to a serial, PDfm 0), so the
client's header check, its job limits (the coolant window, the air
assist floor), its fan duties and its stepper settings all come from the
same place a service job's do. The stream is a square traced at a steady
feed with the laser never commanded: a leading power byte of zero, no
LASER bit anywhere. Nothing in a file from here can put energy into the
tube; the arm and the latch are exercised, the beam is not.

A file longer than the kernel ring (32 MiB of ticks) is written gzip-
compressed, whole file in the stream, as the service serves one: the
client inflates it as the ring drains, and the gzip trailer gives it the
job's length for its progress report.
"""
import gzip
import struct

STEPS_PER_MM = 53.333                 # x8 microstepping: the service's scale, which cloud mode runs at

# The header of a factory print for this machine type, as captured.
PRINT_HEADER = {
    "AAfc": 3632880242, "AAid": 204, "AAin": 0, "AAix": 0, "AArd": 1023, "AArn": 0,
    "AArx": 64500, "AAsn": 0, "AAsx": 0, "AAwd": 1023, "AAwn": 0, "AAwx": 64500,
    "BDbs": 0, "BDpe": 0, "BDps": 0, "BTcx": 12800, "BTfc": 4294711296, "BTfo": 4294942296,
    "BTin": 2147483648, "BTix": 2147483647, "BTrn": 2147483648, "BTrx": 9440,
    "BTwn": 2147483648, "BTwx": 8928, "BTxb": 15, "CCrp": 10000, "CCup": 1, "CCwp": 5000,
    "CFrh": 1, "CMin": 10000, "CMix": 50000, "CMrn": 5000, "CMrx": 33000, "CMts": 0,
    "CMwn": 7000, "CMwx": 31000, "EFid": 0, "EFin": 0, "EFix": 0, "EFrd": 65535, "EFrn": 0,
    "EFrx": 0, "EFwd": 65535, "EFwn": 0, "EFwx": 0, "FTcx": 30000, "FTin": 2147483648,
    "FTix": 2147483647, "FTrn": 2147483648, "FTrx": 14575, "FTwn": 2147483648, "FTwx": 14075,
    "FTxb": 15, "HAai": 0, "HAar": 0, "HAsi": 2, "HAsr": 4, "HAxi": 0, "HAxr": 132, "HAyi": 0,
    "HAyr": 112, "HAzi": 0, "HAzr": 0, "HIix": 12, "HIrx": 1023, "HTcx": 12800, "HTfc": 51200,
    "HTfo": 5000, "HTin": 2147483648, "HTix": 2147483647, "HTrn": 2147483648, "HTrx": 9440,
    "HTwn": 2147483648, "HTwx": 8928, "HTxb": 15, "IFid": 0, "IFin": 0, "IFix": 0,
    "IFrd": 43278, "IFrn": 0, "IFrx": 0, "IFwd": 43278, "IFwn": 0, "IFwx": 0, "IRpd": 1000,
    "IRwb": 3, "IRwc": 688, "IRwx": 275, "IRxb": 3, "IRxc": 1022, "IRxx": 374, "IRyb": 3,
    "IRyc": 0, "IRyx": 0, "IRzb": 3, "IRzc": 0, "IRzx": 0, "ITcx": 19200, "ITfc": 153600,
    "ITfo": 0, "ITin": 2147483648, "ITix": 2147483647, "ITrn": 2147483648, "ITrx": 15840,
    "ITwn": 2147483648, "ITwx": 15328, "ITxb": 15, "LTcx": 12800, "LTfc": 51200, "LTfo": 5000,
    "LTin": 2147483648, "LTix": 2147483647, "LTrn": 2147483648, "LTrx": 9440,
    "LTwn": 2147483648, "LTwx": 8928, "LTxb": 15, "MCsn": 0, "PCid": 19795, "PDct": 5,
    "PDfm": 0, "PTmn": 0, "PTmx": 1023, "STfr": 10000, "TRuc": 0, "XSdm": 1, "XShc": 33,
    "XSmm": 8, "XSrc": 135, "YSdm": 1, "YShc": 5, "YSmm": 8, "YSrc": 22, "ZSmd": 0,
}

X_STEP, X_DIR, Y_STEP, Y_DIR, LASER = 0x01, 0x02, 0x04, 0x08, 0x10
POWER = 0x80


def header_bytes(tags=None):
    """The header record block, the template with `tags` laid over it."""
    h = dict(PRINT_HEADER)
    if tags:
        h.update(tags)
    recs = b"".join(k.encode("ascii") + struct.pack("<I", int(v)) for k, v in sorted(h.items()))
    total = 8 + len(recs)
    return bytes([POWER]) + b"GF1" + struct.pack("<I", total) + recs


def leg(dx_steps, dy_steps, period):
    """One straight leg: Bresenham over the major axis, one step every
    `period` ticks, laser never commanded. X: DIR set = negative; Y: DIR
    set = positive (the hardware convention)."""
    ax, ay = abs(dx_steps), abs(dy_steps)
    major = max(ax, ay)
    if major == 0:
        return b""
    xbit = X_STEP | (X_DIR if dx_steps < 0 else 0)
    ybit = Y_STEP | (Y_DIR if dy_steps > 0 else 0)
    out = bytearray()
    gap = bytes(period - 1)
    err = 0
    x_major = ax >= ay
    minor = ay if x_major else ax
    for _ in range(major):
        b = xbit if x_major else ybit
        err += minor
        if err >= major:
            err -= major
            b |= ybit if x_major else xbit
        out.append(b)
        out += gap
    return bytes(out)


def square_stream(side_mm=40.0, feed_mm_min=600.0, step_hz=10000, repeats=1):
    """A closed square traced `repeats` times at a steady feed: +X, +Y,
    -X, -Y, the head back where it started. Leads with a power byte of
    zero, as the feeder contract wants before any laser-on byte (there
    is none)."""
    steps = int(round(side_mm * STEPS_PER_MM))
    period = max(1, int(round(step_hz / (feed_mm_min / 60.0 * STEPS_PER_MM))))
    one = (leg(steps, 0, period) + leg(0, steps, period)
           + leg(-steps, 0, period) + leg(0, -steps, period))
    return bytes([POWER]) + bytes(period) + one * repeats


def seconds_of(stream, step_hz=10000):
    return len(stream) / float(step_hz)


def write_job(path, side_mm=40.0, feed_mm_min=600.0, seconds=None, step_hz=10000,
              compress=None, tags=None):
    """Write a job to `path`: a square of `side_mm` at `feed_mm_min`,
    repeated until the stream is at least `seconds` long (one square if
    None). Compressed when `compress` is true, or, left None, when the
    stream is longer than the kernel ring (32 MiB of ticks). Returns
    (payload bytes, seconds, compressed)."""
    one = square_stream(side_mm, feed_mm_min, step_hz, 1)
    repeats = 1
    if seconds:
        per = seconds_of(one, step_hz)
        repeats = max(1, int(seconds / per) + (1 if seconds % per else 0))
    body = square_stream(side_mm, feed_mm_min, step_hz, repeats)
    head = header_bytes(dict(tags or {}, STfr=step_hz))
    if compress is None:
        compress = len(body) > 32 * 1024 * 1024
    data = head + body
    if compress:
        data = gzip.compress(data, compresslevel=6)
    with open(path, "wb") as f:
        f.write(data)
    return len(body), seconds_of(body, step_hz), compress


def parse(data):
    """(header dict, payload) of a raw or gzip-compressed pulse file."""
    if data[:2] == b"\x1f\x8b":
        data = gzip.decompress(data)
    if data[1:4] != b"GF1":
        raise ValueError("not a GF1 pulse file")
    total = struct.unpack_from("<I", data, 4)[0]
    tags = {data[p:p + 4].decode("ascii"): struct.unpack_from("<I", data, p + 4)[0]
            for p in range(8, total - 7, 8)}
    return tags, data[total:]
