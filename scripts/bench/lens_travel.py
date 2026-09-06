#!/usr/bin/env python3
"""Lens travel drill: where the carriage's stops sit against the hall edge.

Runs on the board with the controller stopped (through forgectrl, so the
supervisor holds it), steps the lens over sysfs the way the focus card's
lens home does, and counts, under each condition and for several rounds:

  band    the hall's hysteresis: from the rising edge, the half-steps
          down until the hall leaves home, and back up until it reads
          home again
  below   the half-steps from the bottom stop up to the hall edge
          (down off the edge, a drive of N steps onto the stop, the stall
          takes the rest, then up until the hall reads home)
  above   the half-steps from the hall edge up to the top stop (a drive
          of 36 half-steps up from the edge, down until the hall leaves
          home, then up until it reads home again; the top sits the
          difference above the rising edge)

Conditions: the focus card's (half-step, run current, the whole travel
driven onto the stop), a short drive that may not reach the stop, a long
drive well past it, and the factory's lens home mode (full steps at the
run current; the counts are doubled to half-steps). The current is the
run current throughout: the lens does not rise at the hold current.

Lens motion only; nothing fires. The posture found is put back (motor
lock, current, mode, enable) and the controller restarted.

    lens_travel.py [--rounds N] [--only wizard|short|long|full] [--no-above]
"""
import argparse
import json
import sys
import time
import urllib.request

SYS = "/sys/glowforge/"
TOKEN_FILE = "/data/forgefirm/panel.token"
BASE = "http://127.0.0.1"
STEP_S = 0.180          # the cadence of every lens reference (the service's and the card's)
TRAVEL_HALF = 36        # the carriage's travel in half-steps of its screw

CONDITIONS = [
    # key, label, z_mode (1 half, 0 full), the drive onto the stop in that mode's steps
    ("wizard", "half-step, run current, drive 36 half-steps onto the stop (the focus card)", 1, 36),
    ("short",  "half-step, run current, drive 16 half-steps (may not reach the stop)", 1, 16),
    ("long",   "half-step, run current, drive 60 half-steps (well past the stop)", 1, 60),
    ("full",   "full-step, run current, drive 18 full steps (the factory's lens home mode)", 0, 18),
]


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


def step(up):
    wr("cnc/z_step", "1" if up else "0")
    time.sleep(STEP_S)


SETTLE_S = 0.0          # extra rest before every hall read (--settle)


def until(home, up, limit):
    """Step `up` until the hall reads `home`; the steps taken, or -1."""
    for n in range(limit):
        if SETTLE_S:
            time.sleep(SETTLE_S)
        if at_home() == home:
            return n
        step(up)
    return -1


CURRENT = "high"        # high | low | low-down (--current)


def count_below(drive):
    """Down off the edge, `drive` steps onto the stop, up to the edge.
    Returns (count, the steps it took to leave home, error)."""
    if CURRENT == "low-down":
        wr("head/z_current", "1")
    left = until(False, False, 60)
    if left < 0:
        return None, left, "the hall never left home going down"
    for _ in range(drive):
        step(False)
    if CURRENT == "low-down":
        wr("head/z_current", "0")
        time.sleep(0.1)
    c = until(True, True, 80)
    return (c if c >= 0 else None), left, ("" if c >= 0 else "the hall never read home going up")


def to_edge():
    """Put the lens on the rising edge: below it if needed, then up."""
    if at_home():
        if until(False, False, 60) < 0:
            return False
    return until(True, True, 80) >= 0


def count_band():
    """From the rising edge: down until the hall leaves home, then up
    until it reads home again."""
    if not to_edge():
        return None, None, "could not find the edge"
    down = until(False, False, 60)
    if down < 0:
        return None, None, "the hall never left home going down"
    up = until(True, True, 60)
    return down, (up if up >= 0 else None), ("" if up >= 0 else "the hall never read home going up")


def count_above(drive):
    """From the edge, `drive` steps up onto the top stop, down until the
    hall leaves home (a), up until it reads home again (b): the top is
    a - b above the rising edge. The lens ends on the edge."""
    if not to_edge():
        return None, None, "could not find the edge first"
    for _ in range(drive):
        step(True)
    a = until(False, False, 80)
    if a < 0:
        return None, None, "the hall never left home coming down"
    b = until(True, True, 60)
    if b < 0:
        return a, None, "the hall never read home going back up"
    return a, b, ""


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--rounds", type=int, default=3)
    ap.add_argument("--only", choices=[c[0] for c in CONDITIONS])
    ap.add_argument("--no-above", action="store_true", help="skip the top-stop count")
    ap.add_argument("--settle", type=int, default=0, help="extra ms of rest before every hall read")
    ap.add_argument("--cadence", type=int, default=180, help="ms per step (180 is every reference's)")
    ap.add_argument("--ladder", default=None,
                    help="instead of the conditions: comma-separated half-step drives below the "
                         "leave-home point, each counted back up; half-step mode, run current")
    ap.add_argument("--full", action="store_true", help="the ladder in full-step mode")
    ap.add_argument("--current", choices=["high", "low", "low-down"], default="high",
                    help="the drive current: the run current, the hold current, or the hold "
                         "current for the drive onto the stop and the run current for the count")
    ap.add_argument("--park",
                    help="park the lens for a depth-gauge measurement: bottom, top, edge-N or "
                         "edge+N (N half-steps from the rising edge, stall-free; the controller "
                         "stays stopped), or edge (back on the hall edge; the controller restarts)")
    args = ap.parse_args()
    global SETTLE_S, STEP_S, CURRENT
    SETTLE_S = args.settle / 1000.0
    STEP_S = args.cadence / 1000.0
    CURRENT = args.current
    print("step cadence %d ms, extra settle before each hall read %d ms, current %s"
          % (STEP_S * 1000, args.settle, CURRENT))

    dark = api("GET", "/wiz/dark")
    if dark.get("running"):
        sys.exit("a wizard is running: %s" % dark.get("id"))
    mode = api("GET", "/mode")
    print("mode before:", mode)
    found = {a: rd(a) for a in ("cnc/motor_lock", "head/z_current", "head/z_mode", "head/z_enable")}
    print("posture found:", found, "hall:", rd("head/hall_sensor"))

    stopped = False
    results = []
    try:
        if mode.get("controller") == "running":
            print("stopping the controller:", api("POST", "/controller/stop"))
            stopped = True
            time.sleep(2)
        wr("cnc/motor_lock", "0")
        wr("head/z_current", "1" if CURRENT == "low" else "0")
        wr("head/z_enable", "0")
        if args.park:
            wr("head/z_mode", "1")
            time.sleep(0.2)
            if args.park == "bottom":
                left = until(False, False, 60)
                for _ in range(24):
                    step(False)
                print("parked on the bottom stop (left home after %d, then 24 half-steps down)" % left)
            elif args.park == "top":
                to_edge()
                for _ in range(26):
                    step(True)
                print("parked on the top stop (26 half-steps up from the rising edge)")
            elif args.park.startswith("edge-") or args.park.startswith("edge+"):
                n = int(args.park[5:])
                up = args.park[4] == "+"
                to_edge()
                for _ in range(n):
                    step(up)
                print("parked %d half-steps %s the rising edge, stall-free" % (n, "above" if up else "below"))
            else:
                wr("head/z_current", "0")
                to_edge()
                print("parked on the hall's rising edge")
                stopped = True      # the controller restarts whatever state it was found in
            if args.park != "edge":
                # the hold posture keeps the lens where it is; the controller
                # stays stopped so nothing moves the head during the measurement
                for a, v in found.items():
                    wr(a, v)
                print("posture:", {a: rd(a) for a in found}, "hall:", rd("head/hall_sensor"))
                print("the controller stays stopped: run --park edge to return and restart it")
                stopped = False
                found = {}
            return
            mult = 2 if args.full else 1
            wr("head/z_mode", 0 if args.full else 1)
            time.sleep(0.2)
            print("\n== ladder of drives below the leave-home point (%s-step mode)"
                  % ("full" if args.full else "half"))
            for r in range(args.rounds):
                row = []
                for n in [int(x) for x in args.ladder.split(",")]:
                    c, left, err = count_below(n // mult)
                    row.append((n, None if c is None else c * mult, left * mult))
                print("  round %d: " % (r + 1) + "  ".join("drive %d -> up %s (left %d)" % t for t in row))
            args.only = "none"
        for key, label, zmode, drive in CONDITIONS:
            if args.only and key != args.only:
                continue
            wr("head/z_mode", zmode)
            time.sleep(0.2)
            mult = 1 if zmode == 1 else 2
            unit = "half-steps" if mult == 1 else "full steps"
            print("\n== %s" % label)
            bands = []
            for r in range(args.rounds):
                d, u, err = count_band()
                if d is None or u is None:
                    print("  band %d: FAILED: %s (down %s)" % (r + 1, err, d))
                    continue
                bands.append((d * mult, u * mult))
                print("  band %d: home for %d half-steps below the edge going down, home again %d "
                      "half-steps up" % (r + 1, d * mult, u * mult))
            below = []
            for r in range(args.rounds):
                t0 = time.time()
                c, left, err = count_below(drive)
                if c is None:
                    print("  round %d: FAILED: %s" % (r + 1, err))
                    below.append(None)
                    continue
                below.append(c * mult)
                print("  round %d: the edge %d half-steps above the bottom stop (%d %s; left home "
                      "after %d; %.1f s)" % (r + 1, c * mult, c, unit, left * mult, time.time() - t0))
            above = None
            if not args.no_above:
                for r in range(2):
                    t0 = time.time()
                    a, b, err = count_above(TRAVEL_HALF // mult)
                    if a is None or b is None:
                        print("  top %d: FAILED: %s (a=%s b=%s)" % (r + 1, err, a, b))
                        continue
                    above = (a - b) * mult
                    print("  top %d: down %d half-steps to leave home, %d back up to the edge: the top "
                          "stop %d half-steps above the rising edge (%.1f s)"
                          % (r + 1, a * mult, b * mult, above, time.time() - t0))
            results.append((key, bands, below, above))
        wr("head/z_mode", "1")
        wr("head/z_current", "0")
        # leave the lens on the edge, as every reference does
        to_edge()
    finally:
        for a, v in found.items():
            try:
                wr(a, v)
            except OSError as e:
                print("restore %s=%s failed: %s" % (a, v, e))
        print("\nposture restored:", {a: rd(a) for a in found}, "hall:", rd("head/hall_sensor"))
        if stopped:
            print("starting the controller:", api("POST", "/controller/start"))
            time.sleep(4)
            print("mode after:", api("GET", "/mode"))

    print("\n%-8s %-22s %-22s %-6s %s" % ("cond", "band down/up", "below (half-steps)", "above", "total"))
    for key, bands, below, above in results:
        ok = [b for b in below if b is not None]
        tot = (ok[-1] + above) if ok and above is not None else None
        print("%-8s %-22s %-22s %-6s %s" % (key, bands, below, above, tot))


if __name__ == "__main__":
    main()
