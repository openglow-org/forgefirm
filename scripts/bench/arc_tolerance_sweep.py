#!/usr/bin/env python3
# Copyright 2026 514 LLC d/b/a OpenGlow
# Written by Scott Wiederhold
# https://community.openglow.org
# SPDX-License-Identifier:    MIT
"""How fine an arc the one core can plan: a $12 ladder on the 9 in circle (on the board).

Usage: arc_tolerance_sweep.py [--mode M] [tolerances...]
       (default: --mode 16, the ladder 0.002 0.001 0.0005 0.00025 0.0001)

grblHAL traces an arc as chords whose sagitta is $12, so the chord length
is about 2 sqrt(2 r $12) and every chord is a planner block: on the 9 in
circle at F12000 the default 0.002 mm gives 1.35 mm chords and 148 block
boundaries a second, which is the tone the circle plays at every
microstep mode. A finer $12 moves the tone up and asks the single core
for more blocks a second. This drill finds where that stops holding:
with the machine silent (the engine's quiet hold, the fixed 10 s wait)
and the head accelerometer listening, it moves from home to (9, 9) in,
then for each $12 in the ladder runs the 9 in circle from its mid-bottom
at F12000 and reports the chords, the boundaries a second, the circle
time against the ideal, the lowest feed in the middle of the circle and
the fewest free planner blocks (a starved planner shows as both), the
controller CPU over the circle, any clamped-event line the driver logged,
cnc/underruns, and the accelerometer's cruise RMS. On every exit path
(the bench page's Abort and a lost terminal included, each step in its
own try) the quiet hold is released and the accelerometer put back; then,
once the controller reports Idle and the kernel is idle (a dropped sender
does not stop a circle in flight, and the controller takes no $ write in
motion), $12 and the XY microstep mode (an unset key unset again) go back
to what they were, each write checked, and the head returns home from
where it stands at rest. When the machine does not stop within 120 s,
none of those three is attempted, the tool says so, and it exits 1.

Needs the controller in GRBL mode, homed and standing at home, the lid
closed, no other Grbl client, and the bed clear across the circle.
"""
import json
import math
import os
import re
import subprocess
import sys
import time

from gfbench import data_path, exit_on_signal, put_back
from xy_pattern_accel import (Accel, Sampler, Grbl, fail, mode_found, preflight, quiet, restore_mode,
                              set_mode, settle, stats, sysfs, window, FEED, IN, QUIET_S, TRIM_S)

LOG = "/data/log/forgefirm/grblhal/grblhal.log"
RADIUS = 4.5 * IN
CIRCLE = "G2 X0 Y0 I0 J%.3f F%d" % (-RADIUS, FEED)
DEFAULT_LADDER = [0.002, 0.001, 0.0005, 0.00025, 0.0001]


def parse_args(argv):
    mode = 16
    tols = []
    it = iter(argv)
    for a in it:
        if a == "--mode":
            mode = int(next(it))
        else:
            tols += [float(x) for x in a.split()]
    return mode, tols or DEFAULT_LADDER


def controller_pid():
    out = subprocess.run(["pidof", "grblHAL_glowforge"], capture_output=True, text=True).stdout.split()
    return int(out[0]) if out else None


def cpu_ticks(pid):
    with open("/proc/%d/stat" % pid) as f:
        s = f.read().split()
    return int(s[13]) + int(s[14])


def grbl_setting(g, key):
    g.s.sendall((key + "\n").encode())
    g.s.settimeout(1.0)
    buf = ""
    end = time.time() + 1.5
    while time.time() < end and "ok" not in buf:
        try:
            buf += g.s.recv(4096).decode(errors="replace")
        except OSError:
            break
    m = re.search(r"^%s=(\S+)" % re.escape(key), buf, re.M)
    return m.group(1) if m else None


def chords(tol):
    seg = math.floor(math.pi * RADIUS / math.sqrt(tol * (2 * RADIUS - tol)))
    return seg, 2 * math.pi * RADIUS / seg


def run_circle(g, pid):
    """The circle, timed by the state transitions and watched at about
    40 Hz for the feed and the planner's free blocks."""
    k0 = int(sysfs("cnc/underruns"))
    log_off = os.path.getsize(LOG) if os.path.exists(LOG) else 0
    c0 = cpu_ticks(pid)
    g.s.sendall((CIRCLE + "\n").encode())
    t_run = t_idle = None
    feeds, bfs = [], []
    end = time.time() + 120
    while time.time() < end:
        g.s.sendall(b"?")
        g.s.settimeout(1.0)
        buf = ""
        t_end = time.time() + 1.0
        while time.time() < t_end and ">" not in buf:
            try:
                buf += g.s.recv(4096).decode(errors="replace")
            except OSError:
                break
        if "error:" in buf or "ALARM" in buf:
            fail("the controller answered %r" % buf.strip()[-200:])
        m = re.search(r"<(\w+)[^>]*", buf)
        st = m.group(1) if m else None
        now = time.monotonic()
        f = re.search(r"FS:([\d.]+),", buf)
        b = re.search(r"Bf:(\d+),", buf)
        if st and st.startswith("Run"):
            if t_run is None:
                t_run = now
            if f:
                feeds.append((now, float(f.group(1))))
            if b:
                bfs.append((now, int(b.group(1))))
        elif st and st.startswith("Idle") and t_run is not None:
            t_idle = now
            break
        elif st and st.startswith("Alarm"):
            fail("alarm during the circle: %s" % st)
        time.sleep(0.02)
    if t_idle is None:
        fail("the circle did not finish")
    cpu = 100.0 * (cpu_ticks(pid) - c0) / (os.sysconf("SC_CLK_TCK") * (t_idle - t_run))
    g.drain()
    clamped = []
    if os.path.exists(LOG):
        with open(LOG, errors="replace") as fh:
            fh.seek(log_off)
            clamped = [l.strip() for l in fh if "late events clamped" in l or "underrun" in l]
    # the middle of the circle: the ramps at each end left out
    mid = [(t, v) for t, v in feeds if t_run + TRIM_S <= t <= t_idle - TRIM_S]
    mid_bf = [v for t, v in bfs if t_run + TRIM_S <= t <= t_idle - TRIM_S]
    return {"t_run": t_run, "t_idle": t_idle, "seconds": round(t_idle - t_run, 2), "cpu_pct": round(cpu, 1),
            "peak_feed": max((v for _, v in feeds), default=0), "min_mid_feed": min((v for _, v in mid), default=0),
            "min_free_blocks": min(mid_bf, default=None), "max_free_blocks": max(mid_bf, default=None),
            "status_samples": len(feeds), "clamped": clamped, "underruns": [k0, int(sysfs("cnc/underruns"))]}


def wait_at_rest(g):
    """The controller Idle and the kernel idle, or raise: $12, the head and
    the mode are put back only at rest."""
    if not settle(g):
        raise RuntimeError("$12, the head and the XY mode are NOT put back")


def restore_tol(g, orig):
    """$12 back to the value read at the start (None: never read, nothing
    changed), the write's reply and the value read back checked."""
    if g is None or orig is None:
        return
    r = g.cmd("$12=%.6f" % float(orig))
    got = grbl_setting(g, "$12")
    if "ok" not in r or got is None or abs(float(got) - float(orig)) > 0.0006:
        raise RuntimeError("$12 is NOT put back: the write answered %r, $12 reads %s (was %s)"
                           % (r.strip(), got, orig))
    print("$12 restored to %s" % got)


def return_home(g, home):
    """The head back home from where it stands at rest, absolute mode
    back. Raises, moving nothing, unless the controller reports Idle."""
    st, pos, _ = g.status()
    if not st or not st.startswith("Idle") or pos is None:
        raise RuntimeError("the controller is %s, not Idle: the head is not moved" % st)
    if abs(pos[0] - home[0]) > 0.05 or abs(pos[1] - home[1]) > 0.05:
        g.run_leg("G1 X%.3f Y%.3f F%d" % (home[0] - pos[0], home[1] - pos[1], FEED))
        print("head back at home: MPos %s" % (g.status()[1],))
    g.cmd("G90")


def main():
    exit_on_signal()
    mode, ladder = parse_args(sys.argv[1:])
    home = preflight()
    found = mode_found()
    g = accel = sampler = None
    orig = None
    rows = []
    failed = []
    stamp = time.strftime("%Y%m%d%H%M%S")
    try:
        set_mode(mode)
        pid = controller_pid()
        g = Grbl()
        st, pos, _ = g.status()
        if not st or not st.startswith("Idle"):
            fail("controller is %s, not Idle" % st)
        if abs(pos[0] - home[0]) > 0.05 or abs(pos[1] - home[1]) > 0.05:
            fail("the head is not at home (MPos %s): run $H first" % (pos,))
        orig = grbl_setting(g, "$12")
        if orig is None:
            fail("could not read $12")
        print("x%d: step_freq %s; $12 now %s; the ladder %s" % (mode, sysfs("cnc/step_freq"), orig, ladder))
        print("quiet hold: %s" % quiet(True))
        time.sleep(QUIET_S)
        accel = Accel()
        accel.start()
        sampler = Sampler(accel)
        sampler.start()
        g.cmd("G21")
        g.cmd("G91")
        g.run_leg("G1 X%.3f Y%.3f F%d" % (9 * IN, 9 * IN, FEED))
        ideal = 2 * math.pi * RADIUS * 60.0 / FEED
        for tol in ladder:
            # plain decimals only: the controller reads no exponent, so a
            # "5e-05" would land as 5 mm
            r = g.cmd("$12=%.6f" % tol)
            got = grbl_setting(g, "$12")
            if "ok" not in r or got is None:
                fail("$12=%g refused: %r" % (tol, r.strip()))
            # $$ shows three decimals; the stored float keeps what was typed
            # (the core parses the value with strtof and rounds nothing).
            if abs(float(got) - tol) > 0.0006:
                fail("$12=%g reads back as %s" % (tol, got))
            seg, chord = chords(tol)
            time.sleep(1.5)
            res = run_circle(g, pid)
            xs, ys = window(sampler.samples, res["t_run"] + TRIM_S, res["t_idle"] - TRIM_S)
            res.update({"tol": tol, "reported": got, "chords": seg, "chord_mm": round(chord, 3),
                        "boundaries_per_s": round(seg / ideal, 0), "accel_x": stats(xs), "accel_y": stats(ys)})
            rows.append(res)
            ok = (res["min_mid_feed"] >= 0.97 * FEED and not res["clamped"] and res["underruns"][0] == res["underruns"][1]
                  and res["seconds"] <= ideal + 0.6)
            res["result"] = "PASS" if ok else "FAIL"
            print("$12=%-8g %5d chords of %.3f mm, %4.0f/s | circle %.2f s (ideal %.2f) | mid feed min %5.0f | "
                  "planner free min %s max %s | CPU %5.1f %% | clamped %d | underruns %d->%d | accel x rms %7s y rms %7s | %s"
                  % (tol, seg, chord, res["boundaries_per_s"], res["seconds"], ideal, res["min_mid_feed"],
                     res["min_free_blocks"], res["max_free_blocks"], res["cpu_pct"], len(res["clamped"]),
                     res["underruns"][0], res["underruns"][1], res["accel_x"]["rms"], res["accel_y"]["rms"],
                     res["result"]))
            for l in res["clamped"][:3]:
                print("     ", l)
            time.sleep(2.0)
    finally:
        at_rest = []
        failed = put_back(
            ("release the quiet hold (the engine drops it itself within 600 s)", lambda: quiet(False)),
            ("stop the sampler", lambda: sampler is not None and sampler.stop()),
            ("put the accelerometer back", lambda: accel is not None and accel.restore()),
            ("wait for the machine to stop", lambda: at_rest.append(wait_at_rest(g))),
            ("restore $12", lambda: at_rest and restore_tol(g, orig)),
            ("return the head home", lambda: at_rest and g is not None and return_home(g, home)),
            ("close the Grbl connection", lambda: g is not None and g.close()),
            ("store the mode found", lambda: at_rest and restore_mode(found)))
    path = data_path("arc_tolerance_sweep_x%d_%s.json" % (mode, stamp))
    with open(path, "w") as f:
        json.dump({"mode": mode, "feed": FEED, "radius_mm": RADIUS, "rows": rows}, f)
    print("record: %s" % path)
    return 0 if rows and not failed and all(r["result"] == "PASS" for r in rows) else 1


if __name__ == "__main__":
    sys.exit(main())
