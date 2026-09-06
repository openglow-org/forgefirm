#!/usr/bin/env python3
"""Lens stop detection by the head accelerometer: a bench drill.

Steps the lens one half-step at a time from the hall's rising edge toward
each stop and a little past it, sampling the head accelerometer through
the step's whole cadence window, and prints per step the peak-to-peak on
each axis. A normal step rings a little; a stall against a stop (the
rotor slipping, the carriage still) should ring differently. If it does,
a shipped lens move could find a stop with a single slipped step instead
of driving onto it.

The accelerometer (an ST LIS2HH12 on i2c-3 at 0x1e, the crash watch's
chip) is read the way the crash watch reads it: straight over the bus in
six-byte bursts, its rate register set to 800 Hz for the run and put
back after. The iio path waits a sample period per read and is far too
slow for this.

Runs on the board with the controller stopped through forgectrl and
restarted after (the same handling as lens_travel.py). Lens motion only;
nothing fires. Stall drills run on the bench reference machine only.

    lens_stop_accel.py [--past N] [--window MS] [--save FILE]
"""
import argparse
import fcntl
import json
import os
import struct
import sys
import time
import urllib.request

SYS = "/sys/glowforge/"
TOKEN_FILE = "/data/forgefirm/panel.token"
BASE = "http://127.0.0.1"
STEP_S = 0.180

I2C_DEV = "/dev/i2c-3"
I2C_SLAVE_FORCE = 0x0706
ADDR = 0x1E
CTRL1 = 0x20            # ODR [6:4], BDU, XYZ enables
CTRL1_RUN = 0x6F        # 800 Hz, BDU, XYZ on (the crash watch's run value)
OUT_X_L = 0x28
AUTO_INC = 0x80


def rd(attr):
    with open(SYS + attr) as f:
        return f.read().strip()


def wr(attr, value):
    with open(SYS + attr, "w") as f:
        f.write(str(value))


def api(method, path):
    with open(TOKEN_FILE) as f:
        token = f.read().strip()
    req = urllib.request.Request(BASE + path, method=method, headers={"X-ForgeFIRM-Token": token})
    with urllib.request.urlopen(req, timeout=15) as r:
        raw = r.read().decode()
    return json.loads(raw) if raw.strip().startswith("{") else raw


def at_home():
    return rd("head/hall_sensor") == "0"


class Accel:
    """The chip over the bus: bursts of x, y, z at up to the bus rate."""

    def __init__(self):
        self.fd = os.open(I2C_DEV, os.O_RDWR)
        fcntl.ioctl(self.fd, I2C_SLAVE_FORCE, ADDR)
        self.ctrl1_found = self.reg(CTRL1)

    def reg(self, r):
        os.write(self.fd, bytes([r]))
        return os.read(self.fd, 1)[0]

    def set_reg(self, r, v):
        os.write(self.fd, bytes([r, v]))

    def start(self):
        self.set_reg(CTRL1, CTRL1_RUN)
        time.sleep(0.02)

    def restore(self):
        self.set_reg(CTRL1, self.ctrl1_found)

    def read(self):
        os.write(self.fd, bytes([OUT_X_L | AUTO_INC]))
        return struct.unpack("<hhh", os.read(self.fd, 6))

    def sample(self, seconds, keep=None):
        """Peak-to-peak per axis and the sample count over `seconds`;
        the samples appended to `keep` when given."""
        lo = [None] * 3
        hi = [None] * 3
        n = 0
        end = time.monotonic() + seconds
        while time.monotonic() < end:
            v = self.read()
            if keep is not None:
                keep.append(v)
            for i in range(3):
                if lo[i] is None or v[i] < lo[i]:
                    lo[i] = v[i]
                if hi[i] is None or v[i] > hi[i]:
                    hi[i] = v[i]
            n += 1
        return tuple(hi[i] - lo[i] for i in range(3)), n


def step_sampled(accel, up, window_s, keep=None):
    """One half-step, the accelerometer sampled through the window."""
    wr("cnc/z_step", "1" if up else "0")
    p2p, n = accel.sample(window_s, keep)
    rest = STEP_S - window_s
    if rest > 0:
        time.sleep(rest)
    return p2p, n


def until(home, up, limit):
    for n in range(limit):
        if at_home() == home:
            return n
        wr("cnc/z_step", "1" if up else "0")
        time.sleep(STEP_S)
    return -1


def to_edge():
    if at_home():
        if until(False, False, 60) < 0:
            return False
    return until(True, True, 80) >= 0


def fmt(p2p):
    return "x %5d  y %5d  z %5d" % p2p


def run_leg(accel, up, count, window_s, expect, trace):
    """From the edge, `count` single steps `up` or down, each sampled."""
    rows = []
    label = "above" if up else "below"
    print("\n== %s the edge, one half-step at a time (the bench stop is about %d %s)" % (label, expect, label))
    for i in range(1, count + 1):
        keep = []
        p2p, n = step_sampled(accel, up, window_s, keep)
        k = i if up else -i
        rows.append((i, p2p))
        trace.append({"k": k, "p2p": p2p, "samples": keep})
        mark = " <- past the expected stop" if i > expect else ""
        print("  %+3d: %s  (%d samples)%s" % (k, fmt(p2p), n, mark))
    return rows


def summarize(rows, expect):
    free = [p for i, p in rows if i <= expect - 2]
    stall = [p for i, p in rows if i > expect]
    if not free or not stall:
        return
    for i, a in enumerate("xyz"):
        fv = sorted(p[i] for p in free)
        sv = sorted(p[i] for p in stall)
        print("  %s: free steps p2p min %d median %d max %d; past the stop min %d median %d max %d"
              % (a, fv[0], fv[len(fv) // 2], fv[-1], sv[0], sv[len(sv) // 2], sv[-1]))


def find_stop(accel, up, window_s, limit, thresh):
    """Step `up` or down from the edge one half-step at a time until the
    carriage stops moving. A free step rings strongly on every second
    half-step (the parity is learned from the first two), so contact is
    called the moment a strong-parity step rings under `thresh`; a burst
    over four times `thresh` is a rotor slip, which means the stop was
    reached a step or two earlier. Returns (count, slipped), or (-1, 0)."""
    sums = []
    strong_parity = None
    for i in range(1, limit + 1):
        p2p, n = step_sampled(accel, up, window_s)
        total = sum(p2p)
        sums.append(total)
        k = i if up else -i
        note = ""
        if total > 4 * thresh:
            note = "  <- slip burst"
        elif strong_parity is None and i == 2:
            strong_parity = 2 if sums[1] > sums[0] else 1
            note = "  (strong steps are the %s ones)" % ("even" if strong_parity == 2 else "odd")
        print("  %+3d: %s  sum %6d%s" % (k, fmt(p2p), total, note))
        if total > 4 * thresh:
            return i, 1
        if strong_parity is not None and i % 2 == strong_parity % 2 and total < thresh:
            return i, 0
    return -1, 0


def find_leg(accel, up, window_s, limit, thresh, back):
    label = "top" if up else "bottom"
    print("\n== finding the %s stop by the ring (contact = two quiet steps in a row, sum under %d)"
          % (label, thresh))
    if not to_edge():
        print("  could not find the edge first")
        return None
    i, slipped = find_stop(accel, up, window_s, limit, thresh)
    if i < 0:
        print("  no contact within %d half-steps" % limit)
        return None
    for _ in range(back):
        wr("cnc/z_step", "0" if up else "1")
        time.sleep(STEP_S)
    print("  contact called at %+d%s; backed off %d" % (i if up else -i, " by a slip" if slipped else "", back))
    # The proof of gentleness: the count back to the edge equals the
    # steps taken only if nothing slipped.
    if up:
        c = until(False, False, 80)
        c2 = until(True, True, 60)
        print("  back down to leave home: %d, up to the edge: %d (expected %d and the band)"
              % (c, c2, i - back))
    else:
        c = until(True, True, 80)
        print("  back up to the edge: %d (expected %d: no slip)" % (c, i - back))
    return i


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--past", type=int, default=6, help="half-steps past each expected stop")
    ap.add_argument("--window", type=int, default=170, help="ms sampled after each step (of the 180 cadence)")
    ap.add_argument("--below", type=int, default=18, help="the bench bottom stop, half-steps below the edge")
    ap.add_argument("--above", type=int, default=20, help="the bench top stop, half-steps above the edge")
    ap.add_argument("--save", default=None, help="write the per-step sample traces to this JSON file")
    ap.add_argument("--find", type=int, default=0,
                    help="instead of the legs: find each stop by the ring this many times")
    ap.add_argument("--thresh", type=int, default=8000, help="the quiet-step threshold on the summed p2p")
    ap.add_argument("--back", type=int, default=2, help="half-steps to back off after contact")
    args = ap.parse_args()
    window_s = min(args.window, 180) / 1000.0

    dark = api("GET", "/wiz/dark")
    if dark.get("running"):
        sys.exit("a wizard is running: %s" % dark.get("id"))
    mode = api("GET", "/mode")
    print("mode before:", mode)
    accel = Accel()
    print("accelerometer: %s at 0x%02x, CTRL1 found 0x%02x" % (I2C_DEV, ADDR, accel.ctrl1_found))
    found = {a: rd(a) for a in ("cnc/motor_lock", "head/z_current", "head/z_mode", "head/z_enable")}
    stopped = False
    trace = []
    try:
        if mode.get("controller") == "running":
            print("stopping the controller:", api("POST", "/controller/stop"))
            stopped = True
            time.sleep(2)
        accel.start()
        p2p, n = accel.sample(2.0)
        print("at rest, 2 s: %s  (%d samples, %.0f Hz)" % (fmt(p2p), n, n / 2.0))
        wr("cnc/motor_lock", "0")
        wr("head/z_current", "0")
        wr("head/z_mode", "1")
        wr("head/z_enable", "0")
        time.sleep(0.3)
        p2p, n = accel.sample(1.0)
        print("energized, at rest, 1 s: %s" % fmt(p2p))
        if not to_edge():
            sys.exit("could not find the hall edge")

        if args.find:
            for r in range(args.find):
                print("### find round %d" % (r + 1))
                find_leg(accel, False, window_s, 30, args.thresh, args.back)
                find_leg(accel, True, window_s, 32, args.thresh, args.back)
            to_edge()
            print("  back on the edge")
            return

        below = run_leg(accel, False, args.below + args.past, window_s, args.below, trace)
        summarize(below, args.below)
        back = until(True, True, 80)
        print("  back up to the edge: %d half-steps" % back)

        above = run_leg(accel, True, args.above + args.past, window_s, args.above, trace)
        summarize(above, args.above)
        to_edge()
        print("  back on the edge")
    finally:
        try:
            accel.restore()
        except OSError as e:
            print("restore of the accelerometer rate failed: %s" % e)
        for a, v in found.items():
            try:
                wr(a, v)
            except OSError as e:
                print("restore %s=%s failed: %s" % (a, v, e))
        print("posture restored:", {a: rd(a) for a in found}, "hall:", rd("head/hall_sensor"),
              "CTRL1 0x%02x" % accel.reg(CTRL1))
        if stopped:
            print("starting the controller:", api("POST", "/controller/start"))
            time.sleep(4)
            print("mode after:", api("GET", "/mode"))
        if args.save:
            with open(args.save, "w") as f:
                json.dump(trace, f)
            print("traces saved to", args.save)


if __name__ == "__main__":
    main()
