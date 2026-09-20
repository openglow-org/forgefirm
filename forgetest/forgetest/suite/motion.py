# Copyright 2026 514 LLC d/b/a OpenGlow
# Written by Scott Wiederhold
# https://community.openglow.org
# SPDX-License-Identifier:    MIT

"""motion.* - the motion controller under grblHAL: dry motion, no emission.

Ported from `scripts/bench/pacing_test.py` (protocol-loop pacing) and
`scripts/bench/bench_m2.py` (motion-quality bench). Every move is relative
and round-trip; the laser stays latched (the tests never touch it); the
suite is the only Grbl client while a test runs.
"""
import json
import os
import struct
import time

from ..catalog import test
from .. import baseline as _baseline
from .. import hw
from ..runner import Failed

_MOTION_COVERS = [("grblhal-glowforge", "src/**"), ("kernel-module-glowforge", "**"),
                  ("forgectrl", "src/super.*"), ("forgectrl", "src/liveness.*")]


def controller_pid():
    pids = hw.pidof("grblHAL_glowfor")
    if not pids:
        try:
            st, m = hw.Forgectrl().get("/mode")
            where = ("forgectrl reports mode=%s controller=%s" % (m.get("mode"), m.get("controller"))
                     if st == 200 and isinstance(m, dict) else "forgectrl /mode -> %s" % st)
        except hw.HwError as e:
            where = "forgectrl unreachable (%s)" % e
        raise Failed("controller process not found (grblHAL_glowforge); %s" % where)
    return pids[0]


def cpu_ticks(pid):
    with open("/proc/%d/stat" % pid) as f:
        s = f.read().split()
    return int(s[13]) + int(s[14])          # utime + stime (all threads)


def cpu_percent(ctx, pid, window):
    import os
    hz = os.sysconf("SC_CLK_TCK")
    a = cpu_ticks(pid)
    ctx.sleep(window)
    b = cpu_ticks(pid)
    return 100.0 * (b - a) / (hz * window)


def wait_state(ctx, g, prefix, timeout):
    end = time.time() + timeout
    while time.time() < end:
        ctx.checkpoint()
        st = g.status_report()
        if st["state"].startswith(prefix):
            return st
        time.sleep(0.1)
    return None


def wait_state_text(ctx, g, prefix, timeout):
    """wait_state, keeping everything the controller said while polling.
    The driver reports a press with a [MSG:] line the moment it acts on it -
    inside the poll window - so a test that wants both the state and the
    message has to collect them together."""
    end = time.time() + timeout
    text = ""
    while time.time() < end:
        ctx.checkpoint()
        st = g.status_report()
        text += g.drain()
        if st["state"].startswith(prefix):
            return st, text
        time.sleep(0.1)
    return None, text + g.drain()


def wait_left_state(ctx, g, prefix, timeout):
    """The first report whose state is no longer `prefix`, with the text.
    Leaving a state is what proves a command was acted on; catching the
    state it moves INTO is a race whenever the remaining work is short."""
    end = time.time() + timeout
    text = ""
    while time.time() < end:
        ctx.checkpoint()
        st = g.status_report()
        text += g.drain()
        if not st["state"].startswith(prefix):
            return st, text
        time.sleep(0.1)
    return None, text + g.drain()


def wait_idle(ctx, g, timeout=30.0, poll=0.05, grace=0.3):
    """Poll until Idle; returns (peak_feed_mm_min, states_seen, final_report).
    An Idle report inside the first `grace` seconds counts only once a
    non-Idle state was seen: a move just commanded may not have started."""
    peak = 0.0
    states = []
    t0 = time.time()
    deadline = t0 + timeout
    st = None
    while time.time() < deadline:
        ctx.checkpoint()
        st = g.status_report()
        state = st["state"]
        if not states or states[-1] != state:
            states.append(state)
        f = st.get("FS") or st.get("F")
        if f:
            try:
                peak = max(peak, float(str(f).split(",")[0]))
            except ValueError:
                pass
        if state.startswith("Idle") and (time.time() - t0 >= grace or len(states) > 1):
            return peak, states, st
        time.sleep(poll)
    return peak, states + ["TIMEOUT"], st


def machine_idle(ctx, timeout=15.0):
    """The machine itself idle - the kernel has played out the stream
    depth and the decel tail behind grblHAL's Idle. Every motion test ends
    on this, so it hands the machine back at rest."""
    ok = ctx.forgectrl.wait_idle(timeout, abort=ctx.aborted)
    ctx.check(ok, "the machine did not return to idle within %.0f s of the last move", timeout)


def clean_slate(ctx, g):
    st = g.status_report()
    ctx.log("connect: %s", st["state"])
    if any(k in st["state"] for k in ("Alarm", "Door", "Hold")):
        g.realtime(0x18)                     # soft reset
        ctx.sleep(2)
        g.drain()
        st = g.status_report()
        if "Alarm" in st["state"]:
            ctx.log("unlock: %s", g.command("$X"))
    st = g.status_report()
    ctx.check(st["state"].startswith("Idle"), "controller is %s, expected Idle", st["state"])
    clear_error(ctx, g)
    return st


def clear_error(ctx, g):
    """Clear a latched line error before the test's first move.

    Soft limits are armed after a home (the driver's, since the bed has
    no switches), so a jog past the bed is refused with error:15 - and
    grblHAL then answers error:15 to every following G-code line, across
    a fresh connection, until a blank line acknowledges it ($ commands
    and the ? report are unaffected, so a sender that queries on connect
    self-clears; a bare G-code stream does not). A prior test's or the
    baseline hand-back's rejected jog would otherwise make this test's
    first move fail with a stale error unrelated to the move. A blank
    line clears it and returns ok. Harmless and idempotent at Idle."""
    r = g.command("")
    ctx.check(r and r[-1] == "ok" and not any(x.startswith("error") for x in r),
              "the controller answered %s to a blank line at the start - not a clean parser", r)


MOVE_START_S = 2.0      # Run is there within 100 ms; a loaded board gets this long


def start_move(ctx, g, line):
    """Start a travel job with `line` and see the controller in Run. The
    wait is bounded, not a fixed sleep. When Run never comes, the failure
    names what happened instead: the reply (a refused block answers
    error), the state the controller sits in (Hold, Door, Alarm, or an
    Idle that never took the block), every line it said meanwhile, and
    the machine as forgectrl sees it - the evidence a bare "did not
    start" leaves out. Recorded in the evidence either way."""
    reply = g.command(line, timeout=1.0)
    end = time.time() + MOVE_START_S
    t0 = time.time()
    said = ""
    st = None
    while time.time() < end:
        ctx.checkpoint()
        st = g.status_report()
        if st["state"].startswith("Run"):
            break
        said += g.drain()
        time.sleep(0.1)
    rec = {"line": line, "reply": reply, "state": st["state"] if st else None,
           "after_s": round(time.time() - t0, 2)}
    ctx.evidence.setdefault("move_start", []).append(rec)
    if st is not None and st["state"].startswith("Run"):
        return st
    msgs = [ln.strip() for ln in said.splitlines() if ln.strip()]
    rec["said"] = msgs
    s = ctx.forgectrl.status() or {}
    gr = ((s.get("grbl") or {}).get("report") or {})
    rec["forgectrl"] = {"state": s.get("state"), "switches": s.get("switches"),
                        "laser_locked": s.get("laser_locked"), "grbl": gr.get("state"),
                        "alarm": gr.get("alarm"), "sender": (gr.get("sender") or {}).get("connected")}
    ctx.check(False, "the move did not start: %s answered %s; controller %s %.1f s later, said %s; "
                     "forgectrl sees kernel %s, grbl %s (alarm %s), switches %s, laser locked %s",
              line, reply, st["state"] if st else "no report", rec["after_s"], msgs or "nothing",
              s.get("state"), gr.get("state"), gr.get("alarm"), s.get("switches"), s.get("laser_locked"))


@test("motion.pacing", title="Protocol-loop pacing (idle, parked, moving) and hold/resume position",
      subsystem="motion", kind="auto", mode="grbl", est_min=1,
      covers=_MOTION_COVERS, requires=["kernel.latch-locked-idle"],
      steps=["Bed clear; the head needs 30 mm of free +X travel."],
      description="Idle CPU is low; a job parked in a completed feed hold is coarse-paced (not "
                  "busy-spinning at the motion rate); active motion is tight-paced; a feed-hold "
                  "mid-move then resume preserves position (the feeder never starves).")
def pacing(ctx):
    dist, feed = 30.0, 600.0
    pid = controller_pid()
    ev = ctx.evidence
    ev["controller_pid"] = pid
    with ctx.grbl() as g:
        clean_slate(ctx, g)
        g.command("M5")
        g.command("G91")
        idle = cpu_percent(ctx, pid, 3)
        ev["idle_cpu_pct"] = round(idle, 1)
        ctx.log("[1] idle CPU = %.1f%%", idle)

        start = g.status_report().get("MPos")
        ctx.check(start, "no MPos in the status report")
        g.command("G1 X%.3f F%.0f" % (dist, feed), timeout=0.5)
        ctx.sleep(0.6)
        moving = cpu_percent(ctx, pid, 1.0)
        st_mv = g.status_report()["state"]
        ev["moving_cpu_pct"] = round(moving, 1)
        ctx.log("[3] state=%s CPU during move = %.1f%%", st_mv, moving)

        g.realtime(ord("!"))                 # feed hold
        wait_state(ctx, g, "Hold", 5)
        ctx.sleep(1.5)                       # decel completes (Hold:1 -> Hold:0)
        held = g.status_report()
        parked = cpu_percent(ctx, pid, 3)
        ev["held_state"] = held["state"]
        ev["parked_cpu_pct"] = round(parked, 1)
        ctx.log("[2] %s: CPU parked in Hold = %.1f%%", held["state"], parked)

        g.realtime(ord("~"))                 # resume
        peak, states, st = wait_idle(ctx, g, 30)
        ctx.check("TIMEOUT" not in states, "did not return to Idle after the resume: %s", states)
        end = st.get("MPos")
        moved = end[0] - start[0]
        ev["moved_mm"] = round(moved, 3)
        ctx.log("[4] start X=%.3f end X=%.3f moved=%.3f (expect %.1f)", start[0], end[0], moved, dist)
        # return to the starting position
        g.command("G1 X%.3f F%.0f" % (-dist, feed), timeout=0.5)
        wait_idle(ctx, g, 30)
        g.command("G90")
        final = g.status_report().get("MPos")
        ev["final_drift_mm"] = round(final[0] - start[0], 3) if final else None
    machine_idle(ctx)

    ctx.check(abs(moved - dist) < 0.05, "hold+resume lost steps: moved %.3f of %.1f mm", moved, dist)
    ctx.check(parked < moving * 0.5 and parked < 8.0,
              "parked Hold is not coarse-paced: %.1f%% (moving %.1f%%)", parked, moving)
    ctx.check(idle < 8.0, "idle CPU %.1f%%", idle)
    ctx.log("PASS: idle %.1f%%, moving %.1f%%, parked %.1f%%, hold/resume exact", idle, moving, parked)


@test("motion.jog-roundtrip", title="Motion quality: bounded jogs, max rate, diagonal, hold/resume",
      subsystem="motion", kind="auto", mode="grbl", est_min=3,
      covers=_MOTION_COVERS, requires=["kernel.latch-locked-idle"],
      steps=["Setup: the head parked with at least 60 mm of free +X and 40 mm of free +Y travel; "
             "bed clear, lid closed."],
      description="Sanity jogs (X, Y 40 mm out/back at F2400), max-rate X out/back (60 mm at "
                  "F12000), a diagonal out/back, then a G1 with a feed-hold/resume in the middle. "
                  "No jog refused, every move returns to Idle, position drift within 0.05 mm, and "
                  "the head accelerometer - the supervisor's own motion witness - saw the head move "
                  "on every jog it could sample; the counters alone are not proof of motion.")
def jog_roundtrip(ctx):
    ev = ctx.evidence
    accel = hw.AccelSampler()
    ctx.check(accel.available, "no head accelerometer found (i2c %s): the motion witness is missing",
              hw.HEAD_ACCEL_I2C)
    with ctx.grbl() as g, accel:
        st0 = clean_slate(ctx, g)
        start = st0.get("MPos")
        ctx.check(start, "no MPos in the status report")
        ev["start"] = start
        moves = []
        for name, out, back in (
                ("X sanity 40mm", "$J=G91X40F2400", "$J=G91X-40F2400"),
                ("Y sanity 40mm", "$J=G91Y40F2400", "$J=G91Y-40F2400"),
                ("X max-rate 60mm", "$J=G91X60F12000", "$J=G91X-60F12000"),
                ("diag 40mm", "$J=G91X40Y40F8000", "$J=G91X-40Y-40F8000")):
            for jog in (out, back):
                t0 = time.time()
                r = g.command(jog)
                ctx.check(not any(x.startswith("error") for x in r), "%s: jog refused: %s", name, r)
                peak, states, _ = wait_idle(ctx, g)
                t1 = time.time()
                p2px, p2py, n = accel.p2p(t0, t1)
                leg = "out" if jog == out else "back"
                ctx.log("%s %s: peak %.0f mm/min, states %s; accel p2p x=%d y=%d over %d samples",
                        name, leg, peak, states, p2px, p2py, n)
                moves.append({"name": name, "leg": leg, "peak": peak, "states": states,
                              "accel_p2p": [p2px, p2py], "accel_samples": n, "t0": t0, "t1": t1})
                ctx.check("TIMEOUT" not in states, "%s %s did not return to Idle", name, leg)
        ev["moves"] = moves
        # The witness sees the ramps, not the travel: a sysfs read lands
        # two or three samples in a one-second leg, and two samples on
        # the constant-velocity stretch read near the idle level with the
        # head in full flight (bench: 17 samples over the eight legs, a
        # ramp caught on three of them). So the verdict is over the
        # sequence: the head moved during the jogs (p2p over all the
        # legs), and on more than one of them, never one leg alone.
        p2px, p2py, n = accel.p2p(moves[0]["t0"], moves[-1]["t1"])
        moving = [m for m in moves if max(m["accel_p2p"]) >= hw.ACCEL_P2P_MOVING]
        ev["accel_overall"] = {"p2p": [p2px, p2py], "samples": n, "errors": accel.errors}
        ev["accel_moving_legs"] = [m["name"] + " " + m["leg"] for m in moving]
        ctx.log("accel over all %d legs: p2p x=%d y=%d (%d samples, %d errors); motion seen on %d legs",
                len(moves), p2px, p2py, n, accel.errors, len(moving))
        ctx.check(n >= 8, "the accelerometer landed only %d samples over the jogs (%d read errors): "
                  "the motion witness is not reading", n, accel.errors)
        ctx.check(max(p2px, p2py) >= hw.ACCEL_P2P_MOVING,
                  "the head did not move during the jogs (accel p2p x=%d y=%d, below %d): the counters "
                  "ran without the gantry", p2px, p2py, hw.ACCEL_P2P_MOVING)
        ctx.check(len(moving) >= 2, "the accelerometer saw motion on only %d leg(s) of %d (one jolt is "
                  "not a gantry moving on every jog)", len(moving), len(moves))
        maxrate = max(m["peak"] for m in moves if m["name"].startswith("X max-rate"))
        ev["max_rate_peak"] = maxrate
        ctx.check(maxrate >= 6000, "max-rate jog peaked at only %.0f mm/min", maxrate)

        # feed-hold mid-move: G1 at 600 mm/min takes 3 s for 30 mm
        g.command("G91")
        g.command("G1X30F600", timeout=0.5)
        ctx.sleep(1.0)
        g.realtime(ord("!"))
        ctx.sleep(0.8)
        held = g.status_report()
        ev["held_state"] = held["state"]
        ctx.log("after !: %s", held["state"])
        g.realtime(ord("~"))
        peak, states, _ = wait_idle(ctx, g)
        ctx.log("after ~: states %s", states)
        g.command("G1X-30F2400", timeout=0.5)
        wait_idle(ctx, g)
        g.command("G90")
        final = g.status_report().get("MPos")
        ev["final"] = final
        drift = max(abs(a - b) for a, b in zip(final[:2], start[:2]))
        ev["drift_mm"] = round(drift, 3)
        ctx.log("final drift %.3f mm (start %s, final %s)", drift, start, final)
    machine_idle(ctx)
    ctx.check("Hold" in held["state"], "feed hold did not park (state %s)", held["state"])
    ctx.check(drift <= 0.05, "position drift %.3f mm", drift)
    ctx.log("PASS: %d jogs, peak %.0f mm/min, hold parked, drift %.3f mm, accel p2p %d over the jogs, "
            "motion on %d of %d legs", len(moves), maxrate, drift, max(p2px, p2py), len(moving), len(moves))


# ---------------------------------------------------------- microstep modes

def grbl_setting(g, key):
    """One $-setting's value from the controller, or None."""
    for line in g.command(key):
        if line.startswith(key + "="):
            try:
                return float(line[len(key) + 1:].split()[0])
            except ValueError:
                return None
    return None


def set_xy_mode(ctx, fc, value):
    """Store xy_microsteps (empty = clear it) and wait for the GRBL
    controller forgectrl restarts for it to come back configured, with
    the Grbl port answering. Returns the /mode body the machine settled
    on."""
    if value:
        st, body = fc.post("/settings", data={"xy_microsteps": value})
    else:
        st, body = fc.post("/settings", params={"xy_microsteps": ""})
    ctx.check(st == 200, "could not set xy_microsteps=%r (%s: %s)", value or "(clear)", st, body)
    mode = int(value or _baseline.XY_MODE_DEFAULT)
    ctx.sleep(2.0)                              # the controller's stop is under way
    bl = _baseline.Baseline(ctx.log, abort=ctx.run.aborted.is_set)
    body = bl.wait_settled()
    ctx.check(isinstance(body, dict) and body.get("controller") == "running",
              "the controller did not come back after the xy_microsteps write (/mode: %s)", body)
    ctx.check(_baseline.wait_controller_configured(ctx.log, body, xy_mode=mode),
              "the controller came back but did not apply its x%d config", mode)
    end = time.time() + _baseline.GRBL_PORT_S
    while True:
        ctx.checkpoint()
        try:
            with hw.Grbl():
                return body
        except OSError:
            if time.time() > end:
                raise Failed("the Grbl port did not answer within %d s of the restart"
                             % _baseline.GRBL_PORT_S)
            time.sleep(1.0)


@test("motion.microstep-modes",
      title="XY microstep modes: the setting drives the drivers, the scale and the tick",
      subsystem="motion", kind="auto", mode="grbl", est_min=5,
      covers=_MOTION_COVERS + [("forgectrl", "src/main.c"), ("forgectrl", "src/settings.*")],
      requires=["motion.jog-roundtrip"],
      steps=["Bed clear, lid closed; the head needs 40 mm of free +X travel."],
      description="One setting, xy_microsteps, is the XY scale. For each mode it admits (8, 16, "
                  "32): the save restarts the idle controller; the kernel reads the mode on both "
                  "axes with the mode's machine tick and stop ramp; $100/$101 are the mode's and a "
                  "typed $100 is overwritten on the spot; a 40 mm jog out and back at the top speed "
                  "returns to Idle with the kernel counters, over the mode, agreeing with the "
                  "commanded travel, no drift, and the head accelerometer seeing the head move. "
                  "The setting is put back as found, with the controller restarted for it.")
def microstep_modes(ctx):
    ev = ctx.evidence
    fc = ctx.forgectrl
    ctx.check(hw.AccelSampler().available,
              "no head accelerometer found (i2c %s): the motion witness is missing", hw.HEAD_ACCEL_I2C)
    was = (fc.settings() or {}).get("xy_microsteps") or ""
    ev["xy_microsteps_before"] = was or "(unset)"
    results = []
    try:
        for mode in _baseline.XY_MODES:
            ctx.checkpoint()
            ctx.log("--- x%d ---", mode)
            set_xy_mode(ctx, fc, str(mode))
            r = {"mode": mode, "sysfs": {}}
            results.append(r)                   # in the evidence even when a check fails
            for attr, want in _baseline.fixed_sysfs(mode):
                if attr not in ("cnc/x_mode", "cnc/y_mode", "cnc/step_freq", "cnc/ramp_rate"):
                    continue
                got = hw.sysfs_read(attr)
                r["sysfs"][attr] = got
                ctx.check(got == want, "x%d: %s reads %s, want %s", mode, attr, got, want)
            spm = _baseline.xy_steps_per_mm(mode)
            accel = hw.AccelSampler()
            with ctx.grbl() as g, accel:
                clean_slate(ctx, g)
                for key in ("$100", "$101"):
                    v = grbl_setting(g, key)
                    r[key] = v
                    ctx.check(v is not None and abs(v - spm) < 0.001,
                              "x%d: %s=%s, want %.3f", mode, key, v, spm)
                g.command("$100=53.333")
                v = grbl_setting(g, "$100")
                r["typed_100"] = v
                ctx.check(v is not None and abs(v - spm) < 0.001,
                          "x%d: a typed $100 stuck at %s (want %.3f: the scale is the mode's)", mode, v, spm)

                start = g.status_report()["MPos"]
                k0 = kernel_xy_mm(ctx)
                t0 = time.time()
                rr = g.command("$J=G91X40F12000")
                ctx.check(not any(x.startswith("error") for x in rr), "x%d: jog refused: %s", mode, rr)
                peak, states, _ = wait_idle(ctx, g, 30)
                ctx.check("TIMEOUT" not in states, "x%d: the jog out did not return to Idle: %s", mode, states)
                # grblHAL's Idle leads the kernel by the stream depth and
                # the decel tail: the counters are read once the machine
                # itself is idle.
                machine_idle(ctx)
                out_mm = kernel_xy_mm(ctx)[0] - k0[0]
                r["kernel_out_mm"] = round(out_mm, 3)
                r["peak"] = peak
                ctx.check(abs(out_mm - 40.0) <= 0.2,
                          "x%d: the kernel counters say %.3f mm for a 40 mm jog: the scale and the "
                          "drivers' mode disagree", mode, out_mm)
                rr = g.command("$J=G91X-40F12000")
                ctx.check(not any(x.startswith("error") for x in rr), "x%d: jog back refused: %s", mode, rr)
                peak2, states, _ = wait_idle(ctx, g, 30)
                ctx.check("TIMEOUT" not in states, "x%d: the jog back did not return to Idle: %s", mode, states)
                machine_idle(ctx)
                t1 = time.time()
                final = g.status_report()["MPos"]
                drift = abs(final[0] - start[0])
                p2px, p2py, n = accel.p2p(t0, t1)
                r.update({"drift_mm": round(drift, 3), "accel_p2p": [p2px, p2py], "accel_samples": n})
                ctx.log("x%d: out %.3f mm by the kernel, peak %.0f/%.0f mm/min, drift %.3f mm, "
                        "accel p2p x=%d y=%d over %d samples",
                        mode, out_mm, peak, peak2, drift, p2px, p2py, n)
                ctx.check(drift <= 0.05, "x%d: position drift %.3f mm", mode, drift)
                ctx.check(max(peak, peak2) >= 6000, "x%d: the jog peaked at only %.0f mm/min", mode,
                          max(peak, peak2))
                ctx.check(max(p2px, p2py) >= hw.ACCEL_P2P_MOVING,
                          "x%d: the head did not move during the jog (accel p2p x=%d y=%d, below %d): "
                          "the counters ran without the gantry", mode, p2px, p2py, hw.ACCEL_P2P_MOVING)
            machine_idle(ctx)
    finally:
        ev["modes"] = results
        set_xy_mode(ctx, fc, was)
        ev["xy_microsteps_after"] = (fc.settings() or {}).get("xy_microsteps") or "(unset)"
    ctx.log("PASS: %s", "; ".join("x%d: %.3f mm, drift %.3f, accel %d"
                                  % (r["mode"], r["kernel_out_mm"], r["drift_mm"], max(r["accel_p2p"]))
                                  for r in results))


# ---------------------------------------------------------------- liveness

@test("motion.liveness-probe", title="Supervisor motion-liveness verdict", subsystem="motion",
      kind="auto", est_min=1,
      covers=[("forgectrl", "src/super.c"), ("forgectrl", "src/liveness.c"), ("kernel-module-glowforge", "**")],
      requires=["kernel.latch-locked-idle"],
      steps=["Bed clear, lid closed: the probe jogs the head 15 mm out and back (+X first); "
             "forgectrl is restarted once for a fresh probe."],
      description="forgectrl's supervisor reports the head-accelerometer liveness probe as "
                  "verified for the running controller (the DRV8825s are not wedged); when the "
                  "probe was skipped at spawn, the controller is respawned once so it runs. Then "
                  "the regression: with every axis masked (cnc/motor_lock=15, as a bench tool "
                  "may leave it) forgectrl is restarted and its fresh probe must still read "
                  "MOTION OK - the probe unmasks the axes itself - with the head-accel p2p at "
                  "or above the moving threshold.")
def liveness_probe(ctx):
    fc = ctx.forgectrl
    ev = ctx.evidence
    _liveness_verdict(ctx, fc, ev)
    _liveness_masked_restart(ctx, fc, ev)


def _liveness_verdict(ctx, fc, ev):
    st, m = fc.get("/mode")
    ctx.check(st == 200 and isinstance(m, dict), "GET /mode -> %s", st)
    ev["mode_before"] = m
    ctx.log("mode: %s", m)
    ctx.check(m.get("motion") != "fault", "supervisor reports motion-fault: %s", m)
    if m.get("motion") != "verified":
        ctx.log("probe %s at the last spawn - respawning the controller so it runs", m.get("motion"))
        st, body = fc.post("/controller/stop")
        ctx.check(st == 200, "POST /controller/stop -> %s", st)
        ctx.sleep(2)
        st, body = fc.post("/controller/start")
        ctx.check(st == 200, "POST /controller/start -> %s", st)
        t0 = time.time()
        while time.time() - t0 < 60:
            ctx.sleep(1)
            st, m = fc.get("/mode")
            if isinstance(m, dict) and m.get("controller") == "running":
                break
        ctx.sleep(3)
        st, m = fc.get("/mode")
    ev["mode_after"] = m
    ctx.log("mode after: %s", m)
    ctx.check(m.get("controller") == "running", "controller is %s", m.get("controller"))
    ctx.check(m.get("motion") == "verified", "liveness is %r, expected verified", m.get("motion"))


FORGECTRL_LOG = "/data/log/forgefirm/forgectrl/forgectrl.log"


def _log_offset(path):
    try:
        return os.path.getsize(path)
    except OSError:
        return 0


def _log_lines(path, offset, needle):
    try:
        with open(path, "rb") as f:
            f.seek(offset)
            data = f.read().decode("utf-8", "replace")
    except OSError:
        return []
    return [ln.strip() for ln in data.splitlines() if needle in ln]


def _probe_lines(path, offset):
    return _log_lines(path, offset, "liveness probe:")


def _liveness_masked_restart(ctx, fc, ev):
    """The regression: a leftover motor_lock must not read as a wedge."""
    ctx.check(fc.wait_idle(15, abort=ctx.aborted), "machine not idle before the masked restart")
    x0 = _kernel_x_mm(ctx)
    hw.sysfs_write("cnc/motor_lock", "15")
    ctx.log("masked every axis (cnc/motor_lock=15); restarting forgectrl for a fresh probe")
    off = _log_offset(FORGECTRL_LOG)
    rc, out = hw.initd("forgectrl", "restart")
    ctx.check(rc == 0, "forgectrl restart -> rc %s", rc)
    m = None
    t0 = time.time()
    while time.time() - t0 < 150:
        ctx.checkpoint()
        try:
            st, m = fc.get("/mode")
        except hw.HwError:
            m = None                # the daemon is still coming up
        if isinstance(m, dict) and ((m.get("controller") == "running" and m.get("motion") == "verified")
                                    or m.get("controller") == "motion-fault"):
            break
        ctx.sleep(1)
    # The probe's own line reaches the file through rsyslog a moment after
    # the supervisor reports its verdict: wait for it, briefly.
    t1 = time.time()
    lines = _probe_lines(FORGECTRL_LOG, off)
    while not lines and time.time() - t1 < 10:
        ctx.sleep(0.5)
        lines = _probe_lines(FORGECTRL_LOG, off)
    for ln in lines:
        ctx.log("  %s", ln.split(" INFO ", 1)[-1] if " INFO " in ln else ln[-160:])
    ev["masked_restart"] = {"mode": m, "probe_lines": lines[-4:], "motor_lock_after": ctx.sysfs("cnc/motor_lock")}
    ctx.check(m and m.get("controller") == "running" and m.get("motion") == "verified",
              "fresh probe under a leftover mask did not verify motion: %s", m)
    ctx.check(lines and "MOTION OK" in lines[0],
              "the first probe after the restart was not MOTION OK: %s", lines[:1])
    ctx.check(len(lines) == 1, "the probe needed the recovery ladder (%d probes) - a false dead verdict", len(lines))
    ctx.check(ctx.sysfs("cnc/motor_lock") == "0", "motor_lock reads %s after the controller start (expected 0)",
              ctx.sysfs("cnc/motor_lock"))
    ctx.check(fc.wait_idle(15, abort=ctx.aborted), "machine not idle after the probe")
    x1 = _kernel_x_mm(ctx)
    ctx.log("kernel X %s -> %s mm across the probe (out and back)", x0, x1)
    ctx.log("PASS: masked restart probed MOTION OK on the first try, mask cleared, controller up")


# ------------------------------------------------- the gate and the lid

def _wait_controller(ctx, fc, states, timeout, motion=None):
    """GET /mode until the controller state is one of `states` (and, for a
    running one, the motion verdict is `motion` when given). Tolerates a
    daemon that is still coming up. Returns the last document read."""
    m = {}
    t0 = time.time()
    while time.time() - t0 < timeout:
        ctx.checkpoint()
        try:
            st, m = fc.get("/mode")
        except hw.HwError:
            m = None
        if isinstance(m, dict) and m.get("controller") in states:
            if motion is None or m.get("controller") != "running" or m.get("motion") == motion:
                return m
        ctx.sleep(1)
    return m if isinstance(m, dict) else {}


def _button_led(ctx, window_s=1.2):
    """The commanded level of the button's red, green, and blue channels
    (ledtrig_smooth's target), sampled over a blink period or two and
    reduced to the maximum per channel: with a pulse set, target reads 0
    through the off half of every blink. None when the LEDs are not
    readable."""
    best = None
    t0 = time.time()
    while time.time() - t0 < window_s:
        out = []
        for ch in (1, 2, 3):
            try:
                with open("/sys/class/leds/button_led_%d/target" % ch, "r", encoding="utf-8") as f:
                    out.append(int(f.read().strip()))
            except (OSError, ValueError):
                return None
        best = out if best is None else [max(a, b) for a, b in zip(best, out)]
        ctx.sleep(0.1)
    return best


@test("motion.gate-waits-for-lid", title="The motion gate waits for the lid", subsystem="motion",
      kind="operator", est_min=2,
      covers=[("forgectrl", "src/super.*"), ("forgectrl", "src/liveness.*"), ("forgectrl", "src/led.*"),
              ("forgectrl", "src/ui/panel.js")],
      requires=["motion.liveness-probe"], actions=["lid"],
      steps=["The lid opens, forgectrl is restarted (the gate runs at the first spawn of a "
             "session: power-on, a daemon restart), the lid closes: the head does not move "
             "while the lid is open, then makes the 15 mm probe move (+X first)."],
      description="With the lid open when forgectrl starts (power-on with the lid up, or a "
                  "daemon restart) the supervisor starts nothing: GET /mode reports controller "
                  "waiting with why naming the lid, motion unverified, no pid, the button blinks "
                  "amber, and no probe line reaches the log. When the lid closes the probe runs "
                  "(MOTION OK), the lens takes its reference, and the controller comes up "
                  "verified with the button handed back.")
def gate_waits_for_lid(ctx):
    fc = ctx.forgectrl
    ev = ctx.evidence
    ctx.check(fc.wait_idle(15, abort=ctx.aborted), "machine not idle at the start")
    try:
        ctx.act("lid", "open")
        off = _log_offset(FORGECTRL_LOG)
        rc, out = hw.initd("forgectrl", "restart")
        ctx.check(rc == 0, "forgectrl restart -> rc %s", rc)
        m = _wait_controller(ctx, fc, ("waiting", "running", "motion-fault"), 60)
        ev["waiting"] = m
        ctx.log("mode with the lid open: %s", m)
        ctx.check(m.get("controller") == "waiting",
                  "controller is %r with the lid open, expected waiting", m.get("controller"))
        ctx.check("lid" in (m.get("why") or ""), "why does not name the lid: %r", m.get("why"))
        ctx.check(m.get("motion") == "unverified", "motion reads %r while waiting", m.get("motion"))
        ctx.check(not m.get("pid"), "a controller (pid %s) runs while the gate waits", m.get("pid"))
        led = _button_led(ctx)
        ev["led_waiting"] = led
        ctx.log("button LED targets while waiting (r, g, b): %s", led)
        ctx.check(led is not None and led[0] > 0 and led[1] > 0 and led[2] == 0,
                  "the button does not blink amber while the gate waits: %s", led)
        ctx.sleep(3)
        st, m2 = fc.get("/mode")
        ctx.check(isinstance(m2, dict) and m2.get("controller") == "waiting", "the wait did not hold: %s", m2)
        lines = _probe_lines(FORGECTRL_LOG, off)
        ctx.check(not lines, "the probe ran with the lid open: %s", lines[:1])

        t_close = time.time()
        ctx.act("lid", "close")
        m = _wait_controller(ctx, fc, ("running", "motion-fault"), 150, motion="verified")
        ev["after_close"] = m
        ev["close_to_running_s"] = round(time.time() - t_close, 1)
        ctx.log("mode %.1f s after the lid closed: %s", time.time() - t_close, m)
        ctx.check(m.get("controller") == "running" and m.get("motion") == "verified",
                  "the controller did not come up verified after the lid closed: %s", m)
        t1 = time.time()
        lines = _probe_lines(FORGECTRL_LOG, off)
        while not lines and time.time() - t1 < 10:
            ctx.sleep(0.5)
            lines = _probe_lines(FORGECTRL_LOG, off)
        ev["probe_lines"] = lines[-3:]
        ctx.check(lines and "MOTION OK" in lines[0],
                  "the probe after the lid closed was not MOTION OK: %s", lines[:1])
        led = _button_led(ctx)
        ev["led_running"] = led
        ctx.check(led is None or led[0] == 0 or led[2] > 0,
                  "the amber wait pattern is still on the button after the start: %s", led)
    finally:
        if ctx.switch("lid") is False:
            ctx.act("lid", "close", fail=False)
        m = _wait_controller(ctx, fc, ("running", "motion-fault"), 150)
        if m.get("controller") != "running":
            fc.post("/controller/start")
            _wait_controller(ctx, fc, ("running", "motion-fault"), 150)
    ctx.check(fc.wait_idle(15, abort=ctx.aborted), "machine not idle after the drill")


# ---------------------------------------------------------------- cancel / abort

@test("motion.cancel-abort", title="Jog cancel and controlled abort recover cleanly", subsystem="motion",
      kind="auto", mode="grbl", est_min=2,
      covers=_MOTION_COVERS, requires=["motion.pacing"],
      steps=["Bed clear; the head needs 40 mm of free +X travel."],
      description="A jog-cancel (0x85) stops a jog short of its target and returns to Idle with "
                  "position preserved; a ^X abort mid-move (what the sender's Stop sends) "
                  "decelerates under control into Alarm with machine position retained, leaves the "
                  "head where it stopped - no return-to-start, that is the lid policy's move alone - "
                  "and $X recovers to Idle with a subsequent jog running (no driver wedge: the rail "
                  "never cycled).")
def cancel_abort(ctx):
    ev = ctx.evidence
    with ctx.grbl() as g:
        clean_slate(ctx, g)
        start = g.status_report()["MPos"]
        # jog cancel
        g.command("$J=G91X40F2400")
        ctx.sleep(0.4)
        g.realtime(0x85)
        st = wait_state(ctx, g, "Idle", 5)
        ctx.check(st is not None, "not Idle within 5 s of the jog cancel")
        p1 = st["MPos"]
        moved1 = p1[0] - start[0]
        ev["cancel_moved_mm"] = round(moved1, 3)
        ctx.log("jog cancel: moved %.3f mm of 40 (state %s)", moved1, st["state"])
        ctx.check(0.5 < moved1 < 39.0, "jog cancel did not stop short of the target (%.3f mm)", moved1)
        # ^X abort mid-move
        g.command("G91")
        g.command("G1X30F600", timeout=0.5)
        ctx.sleep(1.0)
        g.realtime(0x18)
        st = wait_state(ctx, g, "Alarm", 5)
        ctx.check(st is not None, "^X did not land in Alarm within 5 s")
        p2 = st["MPos"]
        moved2 = p2[0] - p1[0]
        ev["abort_moved_mm"] = round(moved2, 3)
        ctx.log("^X abort: state %s, moved %.3f mm of 30, position retained %s", st["state"], moved2, p2)
        ctx.check(0.5 < moved2 < 29.5, "abort position not retained/plausible (%.3f mm)", moved2)
        r = g.command("$X")
        ctx.log("$X -> %s", r)
        st = wait_state(ctx, g, "Idle", 5)
        ctx.check(st is not None, "$X did not recover to Idle")
        # A sender abort is not a lid cancel: it stops where it stopped and
        # the head stays there. Only the lid/interlock policy returns to the
        # job start, and an unasked-for return move would be a surprise to
        # whoever pressed Stop.
        text = drain_text(g, 2.0)
        ev["abort_returned_home"] = "returned to the job start" in text
        p3 = g.status_report()["MPos"]
        ev["abort_drift_after_recovery_mm"] = round(abs(p3[0] - p2[0]), 3)
        ctx.log("after $X: %s (moved %.3f mm since the abort)", p3, ev["abort_drift_after_recovery_mm"])
        ctx.check(not ev["abort_returned_home"], "a sender abort triggered the return-to-start motion")
        ctx.check(ev["abort_drift_after_recovery_mm"] <= 0.05,
                  "the head moved %.3f mm on its own after a sender abort",
                  ev["abort_drift_after_recovery_mm"])
        # a jog after the abort proves the drivers are alive; return to start
        back = -(p2[0] - start[0])
        r = g.command("$J=G91X%.3fF2400" % back)
        ctx.check(not any(x.startswith("error") for x in r), "return jog refused: %s", r)
        peak, states, st = wait_idle(ctx, g, 30)
        ctx.check("TIMEOUT" not in states, "return jog did not complete: %s", states)
        final = st["MPos"]
        drift = abs(final[0] - start[0])
        ev["final_drift_mm"] = round(drift, 3)
        ctx.log("returned: drift %.3f mm", drift)
        ctx.check(drift <= 0.05, "position drift %.3f mm after cancel/abort/return", drift)
        g.command("G90")
    machine_idle(ctx)


# ---------------------------------------------------------------- dead-man

def _kernel_x_mm(ctx):
    pos = (ctx.forgectrl.status().get("pos") or {})
    return pos.get("x")


def _return_x(ctx, delta_mm):
    """Jog back by the kernel-measured X delta (grbl's own position may be
    untrusted after a kill; the kernel counters kept counting)."""
    if delta_mm is None or abs(delta_mm) < 0.05:
        return
    with ctx.grbl() as g:
        st = g.status_report()["state"]
        if st.startswith("Alarm"):
            g.command("$X")
        g.command("$J=G91X%.3fF1200" % (-delta_mm))
        wait_idle(ctx, g, 30)
    machine_idle(ctx)


@test("motion.deadman", title="Dead-man: controller kill, controller hang, forgectrl restart mid-move, "
                             "a kill during the web-service homing",
      subsystem="motion", kind="auto", mode="grbl", est_min=6,
      covers=_MOTION_COVERS + [("forgectrl", "src/main.c"), ("forgectrl", "init/**")],
      requires=["motion.cancel-abort", "kernel.k1-k2"],
      steps=["Bed clear; the head needs 40 mm of free +X travel and must not be at the left rail. "
             "With cloud mode enabled the test also homes through the web service and kills the "
             "controller mid-homing (about a minute); the head ends wherever the homing was."],
      description="SIGKILL of the controller mid-move: the supervisor reaps it, safes (cnc/stop, "
                  "latch relocked - it never unlocked), and respawns within seconds. SIGSTOP (a "
                  "hang) mid-move: the ring drains into a kernel underrun (fast halt, latch "
                  "locked); the process resumed from the hang recovers on a soft reset and an unlock "
                  "(the alarm is critical, so $X alone is refused; the reset acknowledges the "
                  "fault to the stream and clears the stale ring) and moves again without a "
                  "restart. A short SIGSTOP (100 ms, inside the kernel's 200 ms queue) mid-move: "
                  "no underrun, but the step producer comes back later than its lead and its "
                  "late events clamp; unarmed that is a warning in the controller's log and the "
                  "move completes with every step (the armed case faults, proven on the host). "
                  "forgectrl restart mid-move: the busy controller finishes the move unmanaged "
                  "and the new daemon retakes supervision at idle. After each drill the head is "
                  "jogged back by the kernel-measured distance. SIGKILL during $H (the web-service "
                  "homing, with cloud mode enabled): the homing runner the controller left behind "
                  "is ended before the respawn, the kernel is idle when the new controller starts, "
                  "and the pulse device has one controller on it.")
def deadman(ctx):
    import os as _os
    import signal as _signal
    fc = ctx.forgectrl
    ev = ctx.evidence

    def latch_locked():
        v = hw.sysfs_int("cnc/interlock_circuit")
        return v is not None and bool(v & (1 << 3))

    def wait_running(timeout=30, not_pid=None):
        """A running controller; with not_pid, one other than that pid (a
        killed controller can still read as running until it is reaped)."""
        t0 = time.time()
        while time.time() - t0 < timeout:
            st, m = fc.get("/mode")
            if (isinstance(m, dict) and m.get("controller") == "running" and m.get("pid")
                    and m.get("pid") != not_pid):
                return m
            ctx.sleep(0.5)
        return None

    # ---- 1. SIGKILL mid-move
    m0 = wait_running(10)
    ctx.check(m0, "controller not running")
    pid0 = m0["pid"]
    x0 = _kernel_x_mm(ctx)
    unlocked_seen = False
    with ctx.grbl() as g:
        clean_slate(ctx, g)
        g.command("G91")
        g.command("G1X30F300", timeout=0.5)
        ctx.sleep(1.0)
        _os.kill(pid0, _signal.SIGKILL)
        ctx.log("SIGKILL sent to controller pid %d mid-move", pid0)
    t0 = time.time()
    while time.time() - t0 < 15:
        if not latch_locked():
            unlocked_seen = True
        st, m = fc.get("/mode")
        if isinstance(m, dict) and m.get("controller") == "running" and m.get("pid") != pid0:
            break
        ctx.sleep(0.2)
    m1 = wait_running(30)
    respawn_s = round(time.time() - t0, 1)
    ev["sigkill"] = {"old_pid": pid0, "new": m1, "respawn_s": respawn_s, "unlocked_seen": unlocked_seen}
    ctx.log("after SIGKILL: respawned as %s in %s s; latch unlocked seen: %s", m1, respawn_s, unlocked_seen)
    ctx.check(m1 and m1.get("pid") != pid0, "supervisor did not respawn the controller")
    ctx.check(not unlocked_seen, "the latch unlocked during the kill/respawn")
    ctx.check(m1.get("motion") != "fault", "motion fault after the respawn")
    ctx.sleep(3)
    x1 = _kernel_x_mm(ctx)
    ctx.log("kernel X: %s -> %s mm", x0, x1)
    _return_x(ctx, (x1 - x0) if (x0 is not None and x1 is not None) else None)

    # ---- 2. SIGSTOP (hang) mid-move -> kernel underrun
    m1 = wait_running(10)
    pid1 = m1["pid"]
    x0 = _kernel_x_mm(ctx)
    underruns0 = hw.sysfs_int("cnc/underruns", 0)
    ring_free_idle = hw.sysfs_int("cnc/free")
    with ctx.grbl() as g:
        clean_slate(ctx, g)
        g.command("G91")
        g.command("G1X30F300", timeout=0.5)
        ctx.sleep(1.0)
        _os.kill(pid1, _signal.SIGSTOP)
        ctx.log("SIGSTOP sent to controller pid %d mid-move", pid1)
        t0 = time.time()
        kstate = None
        while time.time() - t0 < 10:
            kstate = hw.sysfs_read("cnc/state")
            if kstate == "underrun":
                break
            ctx.sleep(0.05)
        halt_s = round(time.time() - t0, 2)
        ev["sigstop"] = {"kernel_state": kstate, "halt_s": halt_s, "latch_locked": latch_locked(),
                         "underruns": hw.sysfs_int("cnc/underruns", 0)}
        ctx.log("after SIGSTOP: kernel %s in %s s, latch locked %s, underruns %s -> %s",
                kstate, halt_s, latch_locked(), underruns0, ev["sigstop"]["underruns"])
        # The controller comes back from the hang to find its stream
        # faulted and the core in a critical alarm (17, motor fault). The
        # core refuses an unlock until a soft reset (error 79), and the
        # reset is what the stream takes as the operator's acknowledgment:
        # the kernel is stopped and re-armed, the stale ring cleared, the
        # producer armed again. Reset, unlock, and the controller moves
        # again without a restart (the position is not trusted until a
        # re-home, so the move is a jog).
        _os.kill(pid1, _signal.SIGCONT)
        ctx.sleep(1.0)
        st = g.status_report()["state"]
        ev["sigstop"]["state_after_cont"] = st
        ctx.log("controller resumed: state %s", st)
        ctx.check(st.startswith("Alarm"), "the resumed controller is %s, expected Alarm", st)
        early = g.command("$X")
        ev["sigstop"]["unlock_before_reset"] = early
        ctx.log("$X before the reset: %s", early)
        ctx.check(any(l.startswith("error:79") for l in early),
                  "$X before the reset should be refused as a critical event, got %s", early)
        g.realtime(0x18)
        ctx.sleep(2)
        g.drain()
        st = g.status_report()["state"]
        ctx.log("after the soft reset: %s", st)
        unlock = g.command("$X")
        ev["sigstop"]["unlock_after_reset"] = unlock
        ctx.log("$X after the reset: %s", unlock)
        ctx.check(unlock and unlock[-1] == "ok", "$X after the reset was refused (%s)", unlock)
        st = g.status_report()["state"]
        ctx.check(st.startswith("Idle"), "controller is %s after the reset and unlock, expected Idle", st)
        free = hw.sysfs_int("cnc/free")
        ev["sigstop"]["ring_free_after_reset"] = free
        ctx.log("ring after the reset: free %s (idle %s)", free, ring_free_idle)
        ctx.check(free == ring_free_idle, "the reset did not clear the stale ring (free %s, idle %s)",
                  free, ring_free_idle)
        g.command("$J=G91X-5F1200")
        peak, states, st = wait_idle(ctx, g, 15)
        ev["sigstop"]["recovery_states"] = states
        ctx.check("TIMEOUT" not in states and not str((st or {}).get("state", "")).startswith("Alarm"),
                  "the controller did not move again after the reset and unlock (states %s)", states)
    ctx.check(kstate == "underrun", "the ring did not drain into a kernel underrun (state %s)", kstate)
    ctx.check(latch_locked(), "latch unlocked after the underrun")
    m2 = wait_running(10)
    ev["sigstop"]["after"] = m2
    ctx.check(m2 and m2.get("pid") == pid1, "the recovered controller was replaced (%s)", m2)
    ctx.sleep(3)
    x1 = _kernel_x_mm(ctx)
    _return_x(ctx, (x1 - x0) if (x0 is not None and x1 is not None) else None)

    # ---- 2b. a short hang (inside the kernel queue): late events clamp, a warning, the move completes
    m2 = wait_running(10)
    pid2 = m2["pid"]
    x0 = _kernel_x_mm(ctx)
    underruns0 = hw.sysfs_int("cnc/underruns", 0)
    log0 = _log_offset(GRBLHAL_LOG)
    with ctx.grbl() as g:
        clean_slate(ctx, g)
        g.command("G91")
        g.command("G1X30F300", timeout=0.5)     # ~6 s of motion
        ctx.sleep(1.0)
        _os.kill(pid2, _signal.SIGSTOP)
        time.sleep(0.1)
        _os.kill(pid2, _signal.SIGCONT)
        ctx.log("SIGSTOP 100 ms sent to controller pid %d mid-move", pid2)
        peak, states, st = wait_idle(ctx, g, 30)
        g.command("G90")
    machine_idle(ctx)                           # the kernel's tail plays out behind grbl's Idle
    warned = None
    t0 = time.time()
    while time.time() - t0 < 5 and not warned:
        lines = _log_lines(GRBLHAL_LOG, log0, "late events clamped")
        warned = lines[-1] if lines else None
        if not warned:
            ctx.sleep(0.5)
    x1 = _kernel_x_mm(ctx)
    ev["short_stall"] = {"states": states, "underruns": hw.sysfs_int("cnc/underruns", 0) - underruns0,
                         "warning": warned, "kernel_dx_mm": round((x1 - x0), 3) if (x0 is not None and x1 is not None) else None,
                         "state_after": (st or {}).get("state")}
    ctx.log("short stall: states %s, underruns +%s, warning %r, kernel dx %s mm", states,
            ev["short_stall"]["underruns"], warned, ev["short_stall"]["kernel_dx_mm"])
    ctx.check("TIMEOUT" not in states and str((st or {}).get("state", "")).startswith("Idle"),
              "the move did not complete after the short stall (states %s)", states)
    ctx.check(ev["short_stall"]["underruns"] == 0, "the short stall drained the ring (underruns +%s)",
              ev["short_stall"]["underruns"])
    ctx.check(warned, "the controller did not warn about the clamped events after the short stall")
    ctx.check(ev["short_stall"]["kernel_dx_mm"] is not None and abs(ev["short_stall"]["kernel_dx_mm"] - 30.0) < 0.3,
              "the kernel counted %s mm for a 30 mm move after the short stall", ev["short_stall"]["kernel_dx_mm"])
    ctx.check(latch_locked(), "latch unlocked after the short stall")
    _return_x(ctx, (x1 - x0) if (x0 is not None and x1 is not None) else None)

    # ---- 3. forgectrl restart mid-move: the move finishes, supervision retaken at idle
    m2 = wait_running(10)
    pid2 = m2["pid"]
    x0 = _kernel_x_mm(ctx)
    with ctx.grbl() as g:
        clean_slate(ctx, g)
        g.command("G91")
        g.command("G1X30F300", timeout=0.5)     # ~6 s of motion
        ctx.sleep(1.0)
        rc, out = hw.initd("forgectrl", "restart")
        ctx.log("forgectrl restart mid-move -> rc %s", rc)
        peak, states, st = wait_idle(ctx, g, 30)
        ev["restart"] = {"rc": rc, "states": states}
        ctx.log("move after the restart: states %s", states)
        ctx.check("TIMEOUT" not in states, "the move did not finish after the forgectrl restart")
        g.command("G90")
    t0 = time.time()
    m3 = None
    while time.time() - t0 < 60:
        st, m3 = fc.get("/mode")
        if isinstance(m3, dict) and m3.get("controller") == "running":
            break
        ctx.sleep(1)
    ev["restart"]["mode_after"] = m3
    ctx.log("mode after restart: %s", m3)
    ctx.check(m3 and m3.get("controller") == "running", "supervision not retaken after the restart: %s", m3)
    ctx.check(m3.get("motion") == "verified", "motion not verified after the retake: %s", m3)
    # the retake, by design: the busy controller (unmanaged, its own fd carrying
    # the dead-man) finished its move; at idle the new supervisor stopped it,
    # re-probed motion, and started a supervised one under the broker
    ev["restart"]["replaced_at_idle"] = m3.get("pid") != pid2
    ctx.log("retake: unmanaged pid %s finished the move; supervised pid %s started at idle",
            pid2, m3.get("pid"))
    ctx.check(latch_locked(), "latch unlocked after the restart drill")
    x1 = _kernel_x_mm(ctx)
    _return_x(ctx, (x1 - x0) if (x0 is not None and x1 is not None) else None)
    machine_idle(ctx)

    # ---- 4. SIGKILL during $H: the homing runner is ended, the kernel idle, one writer
    homing_kill(ctx, wait_running)

    ctx.log("PASS: kill respawned in %s s, hang -> underrun in %s s, short stall warned and completed, "
            "restart retook supervision (pid %s), the kill during $H ended the runner (%s)",
            respawn_s, halt_s, m3.get("pid"), ev.get("homing_kill", {}).get("runner_gone_s", "skipped"))


def _pulse_holders():
    """The processes holding the pulse device open, by name."""
    out = []
    for pid in os.listdir("/proc"):
        if not pid.isdigit():
            continue
        try:
            for fd in os.listdir("/proc/%s/fd" % pid):
                if os.readlink("/proc/%s/fd/%s" % (pid, fd)) == "/dev/glowforge":
                    with open("/proc/%s/comm" % pid) as f:
                        out.append(f.read().strip())
                    break
        except OSError:
            continue
    return out


def homing_kill(ctx, wait_running):
    """SIGKILL the GRBL controller while gfhome (the web-service homing
    runner, its own process group on the inherited pulse fd) is moving
    the head. The supervisor must end the runner before it starts another
    controller, or two processes write the ring. Skipped when cloud mode
    is not enabled on the machine (the runner needs the service)."""
    import os as _os
    import signal as _signal
    fc = ctx.forgectrl
    ev = ctx.evidence
    before = fc.settings()
    if str(before.get("cloud_enabled", "")).lower() not in ("1", "true", "yes", "on"):
        ctx.log("kill during $H: skipped, cloud mode is not enabled on this machine")
        return
    hm = before.get("homing_mode", "")
    ev["homing_kill"] = {"homing_mode": hm}
    if hm != "gfcloud":
        st, body = fc.post("/settings", data={"homing_mode": "gfcloud"})
        ctx.check(st == 200, "homing_mode=gfcloud -> %s %s", st, body)
    try:
        m4 = wait_running(10)
        ctx.check(m4, "controller not running before the homing drill")
        pid4 = m4["pid"]
        off = _log_offset(FORGECTRL_LOG)
        runner_up = None
        try:
            with ctx.grbl() as g:
                clean_slate(ctx, g)
                g.send_raw(b"$H\n")
                runner_up = ctx.wait_for(lambda: bool(hw.pidof("gfhome.py")), 30)
                ctx.check(runner_up is not None, "the homing runner never started for $H")
                ctx.sleep(3.0)                  # into the session: the runner drives the head
                ctx.log("SIGKILL sent to controller pid %d during $H (runner pid %s)",
                        pid4, hw.pidof("gfhome.py"))
                _os.kill(pid4, _signal.SIGKILL)
        except (hw.HwError, OSError) as e:
            ctx.log("the Grbl connection ended with the kill: %s", e)
        t_kill = time.time()
        gone_s = ctx.wait_for(lambda: not hw.pidof("gfhome.py"), 15)
        ev["homing_kill"]["runner_gone_s"] = gone_s
        ctx.check(gone_s is not None, "the homing runner outlived its controller")
        m5 = None
        while time.time() - t_kill < 60:
            st, m5 = fc.get("/mode")
            if isinstance(m5, dict) and m5.get("controller") == "running" and m5.get("pid") != pid4:
                break
            ctx.sleep(0.5)
        kstate = hw.sysfs_read("cnc/state")
        holders = _pulse_holders()
        lines = _log_lines(FORGECTRL_LOG, off, "homing runner")
        halted = _log_lines(FORGECTRL_LOG, off, "halting it")
        ev["homing_kill"].update({"respawn_s": round(time.time() - t_kill, 1), "mode_after": m5,
                                  "kernel_state_at_respawn": kstate, "pulse_holders": holders,
                                  "super_lines": [ln[-160:] for ln in lines[-3:]]})
        ctx.log("kill during $H: runner gone in %s s, respawned as %s, kernel %s, pulse held by %s",
                gone_s, m5 and m5.get("pid"), kstate, holders)
        ctx.check(m5 and m5.get("pid") != pid4, "no respawn after the kill during $H: %s", m5)
        ctx.check(lines, "the supervisor did not log the runner's end")
        ctx.check(not halted, "the kernel had to be halted for the respawn: %s", halted[-1:])
        ctx.check(kstate in ("idle", "disabled"), "the kernel was %s when the new controller started", kstate)
        ctx.check("gfhome.py" not in holders and holders.count("grblHAL_glowfor") <= 1,
                  "more than one controller on the pulse device: %s", holders)
        ctx.check(fc.wait_idle(15, abort=ctx.aborted), "machine not idle after the drill")
    finally:
        if hm != "gfcloud":
            st, body = (fc.post("/settings", params={"homing_mode": ""}) if not hm
                        else fc.post("/settings", data={"homing_mode": hm}))
            ctx.log("restore homing_mode=%r -> %s", hm, st)


@test("motion.respawn-gate", title="A respawn waits for the lid the way a first spawn does",
      subsystem="motion", kind="operator", mode="grbl", est_min=2,
      covers=_MOTION_COVERS, requires=["motion.deadman", "motion.gate-waits-for-lid"], actions=["lid"],
      steps=["Bed clear. Open the lid when told and close it when told; nothing moves."],
      description="The enclosure check runs before every controller spawn, respawns included. "
                  "With the lid open, the controller is killed: the supervisor safes the machine "
                  "(latch locked), reports waiting with why naming the lid, and starts no controller "
                  "while the lid stays open. When the lid closes the controller comes back verified, "
                  "without a second motion probe (the probe is once per broker hold).")
def respawn_gate(ctx):
    import os as _os
    import signal as _signal
    fc = ctx.forgectrl
    ev = ctx.evidence
    ctx.check(fc.wait_idle(15, abort=ctx.aborted), "machine not idle at the start")
    st, m0 = fc.get("/mode")
    ctx.check(st == 200 and isinstance(m0, dict) and m0.get("controller") == "running", "controller not running: %s", m0)
    pid0 = m0.get("pid")
    off = _log_offset(FORGECTRL_LOG)
    ctx.act("lid", "open")
    try:
        ctx.sleep(1.0)
        _os.kill(pid0, _signal.SIGKILL)
        t_kill = time.time()
        ctx.log("SIGKILL sent to controller pid %d with the lid open", pid0)
        waited = ctx.wait_for(lambda: (fc.get("/mode")[1] or {}).get("controller") == "waiting", 15)
        st, m1 = fc.get("/mode")
        ev["after_kill"] = {"waiting_s": waited, "mode": m1}
        ctx.log("mode %.1f s after the kill: %s", time.time() - t_kill, m1)
        ctx.check(waited is not None, "the supervisor did not report waiting for the lid: %s", m1)
        ctx.check("lid" in (m1.get("why") or ""), "why does not name the lid: %r", m1.get("why"))
        ilk = hw.sysfs_int("cnc/interlock_circuit")
        ctx.check(ilk is not None and ilk & (1 << 3), "latch not locked after the kill")
        ctx.sleep(4.0)
        st, m2 = fc.get("/mode")
        ctx.check(isinstance(m2, dict) and m2.get("controller") == "waiting" and not m2.get("pid"),
                  "the wait did not hold with the lid open: %s", m2)
    finally:
        ctx.act("lid", "close")
    t_close = time.time()
    came = ctx.wait_for(lambda: ((fc.get("/mode")[1] or {}).get("controller") == "running"
                                 and (fc.get("/mode")[1] or {}).get("pid") != pid0), 60)
    st, m3 = fc.get("/mode")
    ev["after_close"] = {"running_s": came, "mode": m3}
    ctx.log("mode %.1f s after the lid closed: %s", time.time() - t_close, m3)
    ctx.check(came is not None and m3.get("motion") == "verified",
              "the controller did not come back verified after the lid closed: %s", m3)
    lines = _probe_lines(FORGECTRL_LOG, off)
    ev["probe_lines"] = lines[-2:]
    ctx.check(not lines, "the motion probe ran again for the respawn: %s", lines[:1])
    ctx.check(fc.wait_idle(15, abort=ctx.aborted), "machine not idle at the end")
    ctx.log("PASS: the respawn waited %.1f s for the lid, then came back verified in %.1f s with no probe",
            t_close - t_kill, came)


@test("motion.soft-limits", title="After a home the bed is the X/Y envelope",
      subsystem="motion", kind="auto", mode="grbl", est_min=3,
      covers=_MOTION_COVERS, requires=["motion.jog-roundtrip", "cloud.mode-switch"],
      steps=["Bed clear, lid closed. The test homes the machine through the web service if it is "
             "not homed (about a minute), then sends moves the controller must refuse."],
      description="The machine has no limit switches, so the core's $20 cannot be turned on and "
                  "the X/Y soft limits are the driver's: off while the position is not trusted, on "
                  "after a home, when the envelope is the bed ($130 by $131 from the home corner). "
                  "Homed, a program move 5 mm past X max or Y max, or past the near edge, raises "
                  "ALARM:2 before any motion (the kernel counters do not move), a jog past the bed "
                  "is refused with error 15, and a move inside the bed runs. The Z envelope is the "
                  "lens window, as before.")
def soft_limits(ctx):
    from .cloud import gfhome_homing
    fc = ctx.forgectrl
    ev = ctx.evidence
    with ctx.grbl() as g:
        clean_slate(ctx, g)
        if not fc.status().get("homed"):
            gfhome_homing(ctx, ev, g)
        ctx.check(fc.status().get("homed"), "the machine is not homed")
        x_travel = float(grbl_setting(g, "$130"))
        y_travel = float(grbl_setting(g, "$131"))
        ev["travel"] = {"x": x_travel, "y": y_travel}
        k0 = kernel_xy_mm(ctx)

        def refused(cmd):
            lines = g.command(cmd, timeout=2)
            ctx.sleep(0.5)
            text = "\n".join(lines) + g.drain()
            k1 = kernel_xy_mm(ctx)
            moved = max(abs(k1[0] - k0[0]), abs(k1[1] - k0[1]))
            st = g.status_report()["state"]
            rec = {"cmd": cmd, "reply": lines, "alarm": "ALARM:2" in text, "moved_mm": round(moved, 3), "state": st}
            ctx.log("%s", rec)
            ctx.check(rec["alarm"], "%s was not refused with ALARM:2: %s", cmd, lines)
            ctx.check(moved < 0.05, "%s moved the kernel %.3f mm before the alarm", cmd, moved)
            g.realtime(0x18)
            ctx.sleep(1.5)
            g.drain()
            unlock = g.command("$X")
            ctx.check(unlock and unlock[-1] == "ok", "$X after the soft-limit alarm: %s", unlock)
            st = g.status_report()["state"]
            ctx.check(st.startswith("Idle"), "controller is %s after the recovery", st)
            return rec

        ev["refused"] = [refused("G90 G1 X%.1f F600" % (x_travel + 5)),
                         refused("G90 G1 Y%.1f F600" % (y_travel + 5)),
                         refused("G90 G1 X-1 F600")]
        # Homed still: the soft-limit alarm and its reset keep the reference.
        ctx.check(fc.status().get("homed"), "the soft-limit alarm and the reset un-homed the machine")
        jog = g.command("$J=G91X%.1fF1200" % (x_travel + 5))
        ev["jog"] = jog
        ctx.check(any(l.startswith("error:15") for l in jog), "a jog past the bed was not refused with error 15: %s", jog)
        # The core answers the line after an error with that error again
        # until an empty line clears it (the sender's acknowledgment).
        g.command("")
        g.command("G90 G1 X10 Y10 F600", timeout=0.5)
        peak, states, st = wait_idle(ctx, g, 20)
        ev["inside"] = states
        ctx.check("TIMEOUT" not in states, "a move inside the bed did not run")
        g.command("G90 G1 X0 Y0 F600", timeout=0.5)
        wait_idle(ctx, g, 20)
        k1 = kernel_start(ctx)                  # at rest: grbl's Idle leads the kernel's tail
        ev["kernel_back_mm"] = [round(k1[0] - k0[0], 3), round(k1[1] - k0[1], 3)]
        ctx.check(max(abs(k1[0] - k0[0]), abs(k1[1] - k0[1])) < 0.1, "the head did not come back to the corner: %s",
                  ev["kernel_back_mm"])
    ctx.log("PASS: X max, Y max and X min refused with ALARM:2 and no motion, the jog refused with "
            "error 15, a move inside the bed ran")


# ------------------------------------------------- lid / button (the factory's)

_LID_COVERS = _MOTION_COVERS + [("grblhal-glowforge", "src/glowforge_switches.c"),
                                ("grblhal-glowforge", "src/glowforge_switch_map.h"),
                                ("grblhal-glowforge", "src/glowforge_laser.c")]


def kernel_xy_mm(ctx):
    """The kernel's own position counters, in mm (forgectrl /status pos):
    what the machine physically did, independent of what grbl believes."""
    pos = (ctx.forgectrl.status().get("pos") or {})
    return float(pos.get("x", 0.0)), float(pos.get("y", 0.0))


def kernel_start(ctx, timeout=15.0):
    """The kernel counters AT REST, for use as a reference point.

    grblHAL reports Idle when its planner is empty; the kernel is still
    playing the stream depth and the decel tail behind that. Sampling the
    counters in that window records a position the head is only passing
    through, and every later comparison is then measured against a
    transient - which reads exactly like the failure this reference exists
    to catch (a move counted but never played)."""
    machine_idle(ctx, timeout)
    return kernel_xy_mm(ctx)


def check_kernel_returned(ctx, ev, k0, tol_mm=0.1, tag=""):
    """After a return-to-start: the kernel counters must be back where the
    job started too. grbl's own drift can read 0.000 while the head never
    moved (a run the kernel did not take), which is exactly the failure
    that must not pass."""
    k1 = kernel_xy_mm(ctx)
    kdrift = max(abs(k1[0] - k0[0]), abs(k1[1] - k0[1]))
    ev[(tag + "_" if tag else "") + "kernel_drift_mm"] = round(kdrift, 3)
    ctx.log("kernel counters: start (%.2f, %.2f) -> now (%.2f, %.2f), drift %.3f mm",
            k0[0], k0[1], k1[0], k1[1], kdrift)
    ctx.check(kdrift <= tol_mm,
              "the kernel counters did not return to the job start (drift %.3f mm) - "
              "the return move was counted by grbl but not played by the machine", kdrift)


class Watch:
    """Conditions over the controller's state for Context.act / wait_for
    that also keep everything the controller said while they were polled
    (the `[MSG:]` lines a pause or a cancel reports)."""

    def __init__(self, g):
        self.g = g
        self.text = ""
        self.last = None

    def poll(self):
        self.text += self.g.drain()
        self.last = self.g.status_report()
        return self.last

    def in_state(self, prefix):
        return lambda: self.poll()["state"].startswith(prefix)

    def left_state(self, prefix):
        return lambda: not self.poll()["state"].startswith(prefix)


def drain_text(g, seconds):
    """Everything the controller said in the next `seconds`."""
    end = time.time() + seconds
    text = ""
    while time.time() < end:
        text += g.drain()
        time.sleep(0.1)
    return text


def expect_cancel_and_return(ctx, g, ev, start, k0, why, tag):
    """The cancel policy's whole tail, shared by every trigger that ends a
    job this way (lid or interlock, from Run or from a hold): the reason is
    reported, the controller resets without an alarm and with the position
    kept, and the head goes back to where the job started - which the KERNEL
    counters have to confirm, not grbl's belief about them."""
    text = drain_text(g, 3.0)
    msgs = [ln for ln in text.splitlines()
            if ln.startswith("[MSG:") or "help]" in ln or ln.startswith("ALARM")]
    ev[tag + "_messages"] = msgs
    ctx.log("[%s] controller: %s", tag, msgs)
    cancel_msg = "%s - job canceled" % why
    ctx.check(cancel_msg in text, "[%s] the job was not canceled with %r as the reason", tag, why)
    ctx.check("help]" in text, "[%s] no reset banner after the cancel (the sender must see the job end)", tag)
    ctx.check("ALARM" not in text, "[%s] an alarm was raised on the cancel (position should be kept)", tag)
    t0 = time.time()
    returned = "returned to the job start" in text
    while not returned and time.time() - t0 < 30:
        ctx.checkpoint()
        text += g.drain()
        returned = "returned to the job start" in text
        time.sleep(0.2)
    ev[tag + "_returned_message"] = returned
    ctx.check(returned, "[%s] the head did not report returning to the job start within 30 s", tag)
    st = wait_state(ctx, g, "Idle", 5)
    ctx.check(st is not None, "[%s] not Idle after the return (state %s)", tag,
              g.status_report()["state"])
    drift = max(abs(st["MPos"][i] - start[i]) for i in range(2))
    ev[tag + "_drift_mm"] = round(drift, 3)
    ctx.log("[%s] back at the job start: drift %.3f mm", tag, drift)
    ctx.check(drift <= 0.05, "[%s] head not back at the job start (drift %.3f mm)", tag, drift)
    machine_idle(ctx, 10)
    check_kernel_returned(ctx, ev, k0, tag=tag)
    return drift


@test("motion.button-hold-resume", title="The button pauses and resumes a job",
      subsystem="motion", kind="operator", mode="grbl", est_min=2,
      covers=_LID_COVERS, requires=["motion.pacing"], actions=["button"],
      steps=["Bed clear; the head needs 40 mm of free +X travel. No laser is involved.",
             "On Ready the head starts an 8 s move: press the button once while it moves (the "
             "job holds), then once more when told (it resumes and finishes)."],
      description="A travel job is running; one press of the big button feed-holds it (the sender "
                  "sees Hold), the next press resumes it (Run) and the move completes with its "
                  "position intact - the factory's pause/resume on the machine.")
def button_hold_resume(ctx):
    ev = ctx.evidence
    with ctx.grbl() as g:
        clean_slate(ctx, g)
        start = g.status_report()["MPos"]
        ctx.ready("On Ready the head starts an 8 s move along +X. Press the button ONCE while it "
                  "moves; the job holds and the test sees it.")
        g.command("M5")
        g.command("G91")
        start_move(ctx, g, "G1X40F300")               # an 8 s move
        g.drain()                                     # the message window opens here
        w = Watch(g)
        ctx.act("button", "press", until=w.in_state("Hold"), timeout=12, fail=False)
        st, text = w.last, w.text
        ctx.check(st is not None and st["state"].startswith("Hold"),
                  "the press did not hold the job (state %s)", st["state"] if st else "?")
        ev["held_state"] = st["state"]
        ev["held_at_mm"] = round(st["MPos"][0] - start[0], 3)
        ev["pause_message"] = "job paused" in text
        ctx.log("held: %s at %.3f mm of 40; message seen: %s", st["state"], ev["held_at_mm"],
                ev["pause_message"])
        # The press is proven by the job LEAVING the hold. Catching it in Run
        # is a race: a pause late in the move leaves a fraction of a second of
        # travel, which can be over before the next poll - the machine did
        # exactly the right thing and the test would still have called it a
        # failure.
        w = Watch(g)
        ctx.act("button", "press", text="The head is stopped: the press resumes the move.",
                until=w.left_state("Hold"), timeout=60, fail=False)
        st, text = w.last, w.text
        if st is not None and st["state"].startswith("Hold"):
            st = None
        ev["state_after_resume"] = st["state"] if st else None
        ev["resume_message"] = "job resumed" in text
        ctx.check(st is not None, "the second press did not resume the job (still held: %s)",
                  g.status_report()["state"])
        # Leaving the hold for Run or Idle is the resume; leaving it for Alarm
        # or Door is something else entirely, and must not read as a pass.
        ctx.check(st["state"].startswith(("Run", "Idle")),
                  "the job left the hold into %s, not into motion", st["state"])
        ctx.log("resumed: %s; message seen: %s", ev["state_after_resume"], ev["resume_message"])
        peak, states, st = wait_idle(ctx, g, 40)
        ctx.check("TIMEOUT" not in states, "the resumed move did not complete: %s", states)
        moved = st["MPos"][0] - start[0]
        ev["moved_mm"] = round(moved, 3)
        ctx.log("move completed after pause/resume: %.3f mm of 40", moved)
        ctx.check(abs(moved - 40.0) <= 0.05, "the resumed move did not land on its target (%.3f mm)", moved)
        g.command("$J=G91X-40F2400")
        wait_idle(ctx, g, 30)
        g.command("G90")
    machine_idle(ctx)
    ctx.log("PASS: button press held the job (%s), the next press resumed it, target reached", ev["held_state"])


@test("motion.lid-cancel-home", title="Lid open during a job - running or paused - cancels it and returns "
                                     "to the job start",
      subsystem="motion", kind="operator", mode="grbl", est_min=5,
      covers=_LID_COVERS, requires=["motion.pacing", "motion.cancel-abort"],
      actions=["lid", "button"],
      steps=["Bed clear; the head needs 40 mm of free +X travel. No laser is involved.",
             "Twice, on Ready the head starts an 8 s move. First: open the lid while it moves and "
             "leave it open until the head has come back, then close it. Second: press the button "
             "once while it moves (pause), then open the lid, leave it open until the head is back, "
             "and close it."],
      description="A travel job is running when the lid opens: the job parks (planned deceleration), "
                  "the reason is reported, the controller resets (position kept, no alarm - the "
                  "sender's job is over), and the head returns on its own to where the job started "
                  "with the lid still open; the controller ends Idle at the start position. The same "
                  "job paused on the button takes the same path - a lid open from the hold cancels "
                  "and returns, it never resumes. With lid_policy=cancel (the default).")
def lid_cancel_home(ctx):
    ev = ctx.evidence
    policy = (ctx.forgectrl.settings() or {}).get("lid_policy") or "cancel"
    ev["lid_policy"] = policy
    ctx.check(policy == "cancel", "lid_policy is %r; this test needs cancel", policy)
    with ctx.grbl() as g:
        clean_slate(ctx, g)
        k0 = kernel_start(ctx)
        start = g.status_report()["MPos"]
        ev["kernel_start"] = k0
        ev["start"] = start
        ctx.ready("On Ready the head starts an 8 s move along +X. Open the lid while it moves and "
                  "leave it open until the head has come back on its own.")
        g.command("M5")
        g.command("G91")
        start_move(ctx, g, "G1X40F300")               # an 8 s move
        g.drain()                                     # the message window opens here
        ctx.act("lid", "open", text="Leave it open until the head has come back.", timeout=12)
        drift = expect_cancel_and_return(ctx, g, ev, start, k0, "lid opened", "running")
        sw = (ctx.forgectrl.status().get("switches") or {})
        ev["lid_at_return"] = sw.get("lid")
        ctx.act("lid", "close")
        ctx.sleep(1)
        # a jog afterward proves the controller is usable without $X
        r = g.command("$J=G91X5F1200")
        ctx.check(not any(x.startswith("error") for x in r), "jog refused after the cancel: %s", r)
        wait_idle(ctx, g, 15)
        g.command("$J=G91X-5F1200")
        wait_idle(ctx, g, 15)

        # -- the same cancel, entered from a hold ---------------------------
        # A job paused on the button must not be resumable past a lid open:
        # the armed window and the hardware button latch have to agree, so
        # the lid ends the job here exactly as it does from Run.
        # The reference for this phase is taken only once the MACHINE is at
        # rest: the jogs above waited for grblHAL's Idle, which arrives while
        # the kernel is still playing their tail.
        k1 = kernel_start(ctx)
        start2 = g.status_report()["MPos"]
        ev["hold_start"] = start2
        ctx.ready("On Ready the head starts the 8 s move again. Press the button ONCE while it "
                  "moves (the job pauses); then open the lid and leave it open until the head has "
                  "come back.")
        g.command("G91")                                  # the reset restored G90
        g.command("G1X40F300", timeout=0.5)
        ctx.sleep(0.5)
        ctx.check(g.status_report()["state"].startswith("Run"), "the second move did not start")
        g.drain()
        w = Watch(g)
        ctx.act("button", "press", text="The job pauses.", until=w.in_state("Hold"), timeout=12,
                fail=False)
        st, held = w.last, w.text
        ctx.check(st is not None and st["state"].startswith("Hold"),
                  "the press did not hold the job (state %s)", st["state"] if st else "?")
        ev["hold_state"] = st["state"]
        ev["hold_pause_message"] = "job paused" in held
        ctx.log("paused: %s; message seen: %s", st["state"], ev["hold_pause_message"])
        g.drain()
        ctx.act("lid", "open", text="The job is paused: leave the lid open until the head has come "
                "back.", timeout=60)
        hold_drift = expect_cancel_and_return(ctx, g, ev, start2, k1, "lid opened", "hold")
        ctx.check(not g.status_report()["state"].startswith("Hold"),
                  "the controller is still holding after the lid canceled the paused job")
        ctx.act("lid", "close")
        ctx.sleep(1)
        r = g.command("$J=G91X5F1200")
        ctx.check(not any(x.startswith("error") for x in r), "jog refused after the paused cancel: %s", r)
        wait_idle(ctx, g, 15)
        g.command("$J=G91X-5F1200")
        wait_idle(ctx, g, 15)
        g.command("G90")
    machine_idle(ctx)
    ctx.log("PASS: lid open canceled the job from Run (drift %.3f mm) and from the hold (drift %.3f mm), "
            "reset without alarm, head returned to the start both times", drift, hold_drift)


@test("motion.interlock-cancel-home", title="The interlock loop cancels a job like the lid and returns to "
                                           "the job start",
      subsystem="motion", kind="operator", mode="grbl", est_min=4,
      covers=_LID_COVERS, requires=["motion.lid-cancel-home"], actions=["interlock"],
      steps=["Bed clear; the head needs 60 mm of free +X travel. No laser is involved.",
             "Be able to open the remote-interlock loop: unplug the Pro's interlock plug, or pull the "
             "jumper at J8 on a Basic/Plus.",
             "On Ready the head starts a 12 s move: open the interlock while it moves and leave it "
             "open until the head has come back, then restore it."],
      description="The remote-interlock loop is the lid's equal in the cancel policy: opening it mid-job "
                  "cancels the job with 'interlock open' named as the reason - the lid's own message would "
                  "be wrong here - and sends the head back to the job start with the loop still open. "
                  "(An open lid not stopping the park is the lid test's park, which runs with the lid open "
                  "throughout; in GRBL mode the park is a rapid too short to open a lid during, so the "
                  "mid-park lid edge is the cloud test's.)")
def interlock_cancel_home(ctx):
    ev = ctx.evidence
    policy = (ctx.forgectrl.settings() or {}).get("lid_policy") or "cancel"
    ev["lid_policy"] = policy
    ctx.check(policy == "cancel", "lid_policy is %r; this test needs cancel", policy)
    sw = (ctx.forgectrl.status().get("switches") or {})
    ctx.check(sw.get("interlock_ok"), "the interlock loop already reads open - close it before this test")
    with ctx.grbl() as g:
        clean_slate(ctx, g)
        k0 = kernel_start(ctx)
        start = g.status_report()["MPos"]
        ev["start"] = start
        ev["kernel_start"] = k0
        ctx.ready("On Ready the head starts a 12 s move along +X. Open the interlock loop while it "
                  "moves and leave it open until the head has come back on its own.")
        g.command("M5")
        g.command("G91")
        start_move(ctx, g, "G1X60F300")               # a 12 s move
        g.drain()
        ctx.act("interlock", "open", text="Leave it open until the head has come back.", timeout=16)
        sw = (ctx.forgectrl.status().get("switches") or {})
        ev["interlock_ok_after_pull"] = sw.get("interlock_ok")
        ctx.check(sw.get("interlock_ok") is False,
                  "the interlock still reads closed - the loop was not opened (switches: %s)", sw)
        drift = expect_cancel_and_return(ctx, g, ev, start, k0, "interlock open", "interlock")
        sw = (ctx.forgectrl.status().get("switches") or {})
        ev["switches_at_return"] = {"lid": sw.get("lid"), "interlock_ok": sw.get("interlock_ok")}
        ctx.log("at the end of the park: %s", ev["switches_at_return"])
        ctx.check(sw.get("interlock_ok") is False,
                  "the interlock was closed again before the park finished - the park ran with the loop "
                  "restored, not open")
        ctx.act("interlock", "close")
        ctx.sleep(1)
        sw = (ctx.forgectrl.status().get("switches") or {})
        ev["restored"] = {"lid": sw.get("lid"), "interlock_ok": sw.get("interlock_ok")}
        ctx.check(sw.get("interlock_ok"), "the interlock loop is still open - restore it before continuing")
        r = g.command("$J=G91X5F1200")
        ctx.check(not any(x.startswith("error") for x in r), "jog refused after the cancel: %s", r)
        wait_idle(ctx, g, 15)
        g.command("$J=G91X-5F1200")
        wait_idle(ctx, g, 15)
        g.command("G90")
    machine_idle(ctx)
    ctx.log("PASS: interlock open canceled the job with its own reason and the head returned to the "
            "start (drift %.3f mm) with the loop still open", drift)


@test("motion.lid-policy-hold", title="lid_policy=hold parks the job in Door and a cycle start resumes it",
      subsystem="motion", kind="operator", mode="grbl", est_min=4,
      covers=_LID_COVERS + [("forgectrl", "src/settings.*")], requires=["motion.lid-cancel-home"],
      actions=["lid"],
      steps=["Bed clear; the head needs 40 mm of free +X travel. No laser is involved.",
             "On Ready the head starts an 8 s move: open the lid while it moves (the job parks in "
             "Door), then close it when told; the job finishes after that."],
      description="The other lid policy, kept for senders that expect stock grblHAL: with "
                  "lid_policy=hold a lid open parks the job in the door state and holds it there - "
                  "no cancel, no return home - and once the lid is closed a cycle start finishes the "
                  "move with its position intact. The setting is restored to cancel at the end.")
def lid_policy_hold(ctx):
    ev = ctx.evidence
    fc = ctx.forgectrl
    # What the machine had, exactly: an unset lid_policy reads as the empty
    # string and behaves as cancel, and writing the word back where the
    # machine had nothing is a leftover the hand-back reports. The default
    # is for reading the value, never for restoring it.
    was = (fc.settings() or {}).get("lid_policy", "")
    ev["lid_policy_before"] = was
    ev["lid_policy_in_force"] = was or "cancel"
    st, _b = fc.post("/settings", data={"lid_policy": "hold"})
    ctx.check(st == 200, "could not set lid_policy=hold (%s)", st)
    ctx.check(((fc.settings() or {}).get("lid_policy")) == "hold", "lid_policy did not take")
    try:
        with ctx.grbl() as g:
            clean_slate(ctx, g)
            start = g.status_report()["MPos"]
            ctx.ready("On Ready the head starts an 8 s move along +X. Open the lid while it moves; "
                      "the job parks in the door state and waits.")
            g.command("M5")
            g.command("G91")
            start_move(ctx, g, "G1X40F300")           # an 8 s move
            g.drain()
            ctx.act("lid", "open", timeout=12)
            st, text = wait_state_text(ctx, g, "Door", 8)
            ev["door_state"] = st["state"] if st else g.status_report()["state"]
            ctx.check(st is not None, "the lid did not park the job in Door (state %s)", ev["door_state"])
            ev["messages"] = [ln for ln in text.splitlines() if ln.startswith("[MSG:")]
            ctx.check("job canceled" not in text, "the job was canceled under lid_policy=hold: %s", ev["messages"])
            ctx.check("returned to the job start" not in text,
                      "the head returned to the job start under lid_policy=hold")
            held = g.status_report()["MPos"]
            ev["parked_at"] = held
            ctx.log("parked in %s at %s", ev["door_state"], held)
            ctx.act("lid", "close")
            ctx.sleep(1)
            ev["state_after_close"] = g.status_report()["state"]
            ctx.log("after the lid closed: %s (a cycle start is needed)", ev["state_after_close"])
            g.realtime(0x7E)                              # ~ cycle start
            peak, states, st = wait_idle(ctx, g, 30)
            ev["states_after_resume"] = states
            ctx.check("TIMEOUT" not in states, "the job did not finish after the resume: %s", states)
            final = st["MPos"]
            moved = final[0] - start[0]
            ev["moved_mm"] = round(moved, 3)
            ctx.log("finished: moved %.3f mm of 40 (states %s)", moved, states)
            ctx.check(abs(moved - 40.0) <= 0.05,
                      "the resumed job did not finish its move (%.3f mm of 40)", moved)
            g.command("$J=G91X-40F1200")
            wait_idle(ctx, g, 30)
            g.command("G90")
        machine_idle(ctx)
    finally:
        # An empty value clears the key, and only the query-string form
        # carries one: an empty form field never reaches the request, and
        # the write is refused with "no known setting in request".
        st, _b = (fc.post("/settings", params={"lid_policy": ""}) if not was
                  else fc.post("/settings", data={"lid_policy": was}))
        ev["lid_policy_restored"] = (fc.settings() or {}).get("lid_policy", "")
        ctx.log("lid_policy restored to %s", ev["lid_policy_restored"])
    ctx.check(ev["lid_policy_restored"] == was, "lid_policy was not restored to %r", was)
    ctx.log("PASS: lid_policy=hold parked the job in Door and the cycle start finished it (%.3f mm)",
            ev["moved_mm"])


GRBLHAL_LOG = "/data/log/forgefirm/grblhal/grblhal.log"
SCHED_FIFO = 1


def _thread_sched(pid):
    """(tid, policy, rt_priority) for every thread of pid. Those are fields
    41 and 40 of /proc/<tid>/stat; comm can hold spaces and parentheses, so
    the fields are indexed from the last ')' - rest[0] is field 3."""
    out = []
    for tid in sorted(os.listdir("/proc/%d/task" % pid)):
        try:
            with open("/proc/%d/task/%s/stat" % (pid, tid)) as f:
                s = f.read()
        except OSError:
            continue
        rest = s[s.rindex(")") + 1:].split()
        if len(rest) >= 39:
            out.append((tid, int(rest[38]), int(rest[37])))
    return out


@test("motion.step-timing-under-load",
      title="Step timing holds while userspace competes for the core",
      subsystem="motion", kind="auto", mode="grbl", est_min=2,
      covers=_MOTION_COVERS, requires=["kernel.latch-locked-idle", "motion.jog-roundtrip"],
      steps=["Bed clear, lid closed; the head needs >= 40 mm of free +X travel."],
      description="The board has one core, so the thread that stamps steps onto the pulse grid "
                  "has to outrank ordinary userspace: when its virtual clock slips behind wall "
                  "clock past the queue depth, late events clamp forward and the backlog ships "
                  "one step per machine tick - a burst no motor follows, while cnc/underruns "
                  "stays 0 because the ring never runs dry. Asserts the producer and the shipper "
                  "both hold SCHED_FIFO, then drives 2000 mm/min round trips against a deliberate "
                  "nice-5 CPU hog and requires the controller to report no clamped events.")
def step_timing_under_load(ctx):
    import subprocess

    ev = ctx.evidence
    pid = controller_pid()

    threads = _thread_sched(pid)
    rt = sorted(prio for _tid, pol, prio in threads if pol == SCHED_FIFO)
    ev["threads"] = len(threads)
    ev["rt_priorities"] = rt
    ctx.log("controller threads: %d, SCHED_FIFO priorities: %s", len(threads), rt)
    ctx.check(len(rt) >= 2,
              "expected the stream producer and the shipper on SCHED_FIFO, found %d of %d "
              "threads at real time (%s): step timing is exposed to ordinary userspace",
              len(rt), len(threads), rt)

    off = _log_offset(GRBLHAL_LOG)
    load = None
    try:
        # One SCHED_OTHER hog at the same nice as forgectrl's HTTP threads:
        # the realistic competitor, and the one the fix must outrank. The
        # image has no `nice` binary (BusyBox ships renice only), so the
        # niceness is applied from here once the child exists, and the value
        # that actually took is recorded - a hog left at nice 0 would be a
        # harsher test than intended, and one left unset must not pass
        # silently.
        load = subprocess.Popen(["sh", "-c", "while :; do :; done"],
                                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        try:
            os.setpriority(os.PRIO_PROCESS, load.pid, 5)
            hog_nice = os.getpriority(os.PRIO_PROCESS, load.pid)
        except OSError as exc:
            hog_nice = None
            ctx.log("could not set the hog niceness: %s", exc)
        ev["hog_nice"] = hog_nice
        ctx.check(hog_nice == 5, "CPU hog is at nice %s, expected 5", hog_nice)
        ctx.log("CPU hog started (pid %d, nice %s)", load.pid, hog_nice)
        with ctx.grbl() as g:
            clean_slate(ctx, g)
            ctrl0 = cpu_ticks(pid)
            t0 = time.time()
            legs = 0
            for _i in range(10):
                ctx.checkpoint()
                for jog in ("$J=G91X40F2000", "$J=G91X-40F2000"):
                    r = g.command(jog)
                    ctx.check(not any(x.startswith("error") for x in r),
                              "jog refused under load: %s", r)
                    _peak, states, _ = wait_idle(ctx, g, 30)
                    ctx.check("TIMEOUT" not in states, "a leg did not return to Idle under load")
                    legs += 1
            elapsed = time.time() - t0
            hz = os.sysconf("SC_CLK_TCK")
            ev["legs"] = legs
            ev["motion_s"] = round(elapsed, 1)
            ev["controller_cpu_pct"] = round(100.0 * (cpu_ticks(pid) - ctrl0) / (hz * elapsed), 1)
            ctx.log("%d legs in %.1f s, controller CPU %.1f %%",
                    legs, elapsed, ev["controller_cpu_pct"])
            machine_idle(ctx)
    finally:
        if load is not None:
            load.kill()
            load.wait()
            ctx.log("CPU hog stopped")

    clamped = _log_lines(GRBLHAL_LOG, off, "late events clamped")
    ev["clamp_lines"] = clamped
    ctx.check(not clamped,
              "step generation was starved while userspace competed for the core: %s",
              "; ".join(clamped))
    ctx.log("PASS: %d legs at 2000 mm/min against a nice-5 CPU hog, no clamped events",
            ev["legs"])


# ------------------------------------------------- manual home, motor release

HOLD_CURRENTS = ("33", "5")     # pic/x_step_current, pic/y_step_current at idle
MANUAL_NOTICE = "Manual home: position set where the head was placed"


def _step_currents():
    return (hw.sysfs_read("pic/x_step_current"), hw.sysfs_read("pic/y_step_current"))


def _rail_lines():
    """How many times the kernel has logged the 40 V supply going on or off."""
    rc, out = hw.run(["dmesg"])
    if rc != 0:
        return -1
    return sum(1 for line in out.splitlines() if "40V on" in line or "40V off" in line)


def _kernel_position():
    """The kernel's position record: (x, y, z) in steps, then the bytes it
    has played and the bytes it was handed. Nothing moves without the last
    two moving, which makes them the witness for "nothing was shipped": the
    head accelerometer cannot say it, since the rotors relax off their held
    microstep when the drivers let go, when the currents drop to hold, and at
    a reset, and the head feels each of those."""
    with open(hw.sysfs_root() + "cnc/position", "rb") as f:
        raw = f.read(32)
    return struct.unpack_from("<3i", raw, 0) + struct.unpack_from("<2I", raw, 12)


def _refused(ctx, g, line):
    """Send a line that must be refused; acknowledge the parser error it
    leaves (clear_error has the why) and return the error."""
    r = g.command(line)
    err = next((x for x in r if x.startswith("error")), None)
    if err is not None:
        g.command("")
    return err


def _drop_reference(ctx, fc):
    """A false home is not left behind: a fresh controller starts unhomed."""
    fc.post("/controller/stop")
    _wait_controller(ctx, fc, ("stopped", "standby"), 30)
    fc.post("/controller/start")
    m = _wait_controller(ctx, fc, ("running",), 120, motion="verified")
    ctx.check(m.get("controller") == "running", "the controller did not come back: %s", m)


@test("motion.release", title="Motor release: nothing moves a released gantry, only the operator energizes it",
      subsystem="motion", kind="auto", hardware="takeover", mode="grbl", est_min=8,
      covers=_MOTION_COVERS + [("forgectrl", "src/status.*"), ("forgectrl", "src/wizdark.*")],
      requires=["motion.jog-roundtrip"],
      steps=["Bed clear; the head needs 50 mm of free travel each way on X and Y. Nobody touches the gantry."],
      description="$MD releases X and Y by taking their step currents to 0, with the 40 V rail "
                  "untouched (no supply line in the kernel log, cnc/state idle, no fault), drops the "
                  "X and Y reference and locks the machine in alarm. While released a jog, a G0 and "
                  "$X are refused, a soft reset does not unlock it, and forgectrl refuses a switch "
                  "to cloud mode; the kernel's position record "
                  "is the witness that nothing was shipped (no step and no byte played), and the head "
                  "accelerometer stays under the moving threshold in the window after the release. "
                  "$ME energizes with no fault. The drivers are then proven alive the way the machine "
                  "proves it to itself: the Setup motion check runs the liveness probe on the held "
                  "device and the witnessed 50 mm jogs.")
def motor_release(ctx):
    from .setup_dark import run_check

    ev = ctx.evidence
    fc = hw.Forgectrl()
    accel = hw.AccelSampler()
    ctx.check(accel.available, "no head accelerometer found (i2c %s): the motion witness is missing",
              hw.HEAD_ACCEL_I2C)
    rail0 = _rail_lines()
    ctx.check(rail0 >= 0, "the kernel log is unreadable: the rail witness is missing")
    with ctx.grbl() as g, accel:
        clean_slate(ctx, g)
        r = g.command("$MD")
        ctx.check(not any(x.startswith("error") for x in r), "$MD refused at idle: %s", r)
        text = "\n".join(r) + drain_text(g, 1.0)
        ev["released_currents"] = list(_step_currents())
        ev["state_released"] = g.status_report()["state"]
        ev["kernel_state_released"] = hw.sysfs_read("cnc/state")
        ev["faults_released"] = hw.sysfs_int("cnc/faults")
        ev["homed_axes_released"] = fc.status().get("homed_axes")
        ctx.log("$MD: step currents %s, %s, cnc/state %s, faults %s, homed_axes %s",
                ev["released_currents"], ev["state_released"], ev["kernel_state_released"],
                ev["faults_released"], ev["homed_axes_released"])
        ctx.check(ev["released_currents"] == ["0", "0"],
                  "the step currents read %s after $MD, not 0 and 0", ev["released_currents"])
        ctx.check(ev["state_released"].startswith("Alarm"), "not locked in alarm: %s", ev["state_released"])
        ctx.check(ev["kernel_state_released"] == "idle", "the kernel left idle: %s", ev["kernel_state_released"])
        ctx.check(ev["faults_released"] == 0, "a driver fault on release: %s", ev["faults_released"])
        ctx.check(not (ev["homed_axes_released"] or 0) & 3, "X or Y still referenced while released")
        ctx.check("Motors released" in text, "the sender was not told the motors are released")

        # The rotors have relaxed by now: from here on the head is still.
        ctx.sleep(1.0)
        k0 = _kernel_position()
        t0 = time.time()
        refusals = {}
        for line in ("$J=G91X10F2400", "G0X10", "$X"):
            refusals[line] = _refused(ctx, g, line)
            ctx.check(refusals[line], "%s was accepted while the motors are released", line)
        ctx.sleep(1.0)
        p2px, p2py, n = accel.p2p(t0)
        ev["released_accel_p2p"] = [p2px, p2py, n]
        ctx.check(n > 0, "no accelerometer samples in the released window")
        ctx.check(max(p2px, p2py) < hw.ACCEL_P2P_MOVING,
                  "the head moved while released (accel p2p x=%d y=%d)", p2px, p2py)
        g.realtime(0x18)
        ctx.sleep(2)
        g.drain()
        refusals["^X then $X"] = _refused(ctx, g, "$X")
        ctx.check(refusals["^X then $X"], "a soft reset and $X unlocked a released machine")
        ev["state_after_reset"] = g.status_report()["state"]
        ctx.check(ev["state_after_reset"].startswith("Alarm"),
                  "a soft reset unlocked a released machine: %s", ev["state_after_reset"])
        k1 = _kernel_position()
        ev["refusals"] = refusals
        ev["kernel_position"] = [list(k0), list(k1)]
        ctx.log("refused while released: %s; accel p2p x=%d y=%d over %d samples; kernel record %s -> %s",
                refusals, p2px, p2py, n, k0, k1)
        ctx.check(k1 == k0, "the kernel played something while released: %s -> %s", k0, k1)

        # forgectrl reads the release too: cloud homing moves the head, so the
        # switch is refused (for the release where cloud mode is enabled, for
        # cloud mode being off where it is not; the mode stays either way).
        st, body = fc.post("/mode", data={"controller": "cloud"})
        text = body if isinstance(body, str) else json.dumps(body)
        cloud_on = str(fc.settings().get("cloud_enabled", "")).lower() in ("1", "true", "yes", "on")
        ev["mode_cloud_released"] = {"status": st, "body": text, "cloud_enabled": cloud_on}
        ctx.log("POST /mode controller=cloud while released -> %s %s", st, text)
        ctx.check(st == 409, "a switch to cloud mode while released -> %s, expected 409", st)
        if cloud_on:
            ctx.check("released" in text, "the refusal does not name the release: %r", text)
        ctx.check(fc.get("/mode")[1].get("mode") == "grbl", "the mode changed under a released gantry")
        ctx.check(_step_currents() == ("0", "0"),
                  "something energized the motors: step currents %s", _step_currents())

        r = g.command("$ME")
        ctx.check(not any(x.startswith("error") for x in r), "$ME refused: %s", r)
        ctx.sleep(0.5)
        ev["energized_currents"] = list(_step_currents())
        ev["faults_energized"] = hw.sysfs_int("cnc/faults")
        ev["state_energized"] = g.status_report()["state"]
        ctx.check(tuple(ev["energized_currents"]) == HOLD_CURRENTS,
                  "the step currents read %s after $ME, not the hold currents", ev["energized_currents"])
        ctx.check(ev["faults_energized"] == 0, "a driver fault on energize: %s", ev["faults_energized"])
        ctx.check(ev["state_energized"].startswith("Idle"), "not idle after $ME: %s", ev["state_energized"])

    # The drivers are alive: the machine's own witness says so. The liveness
    # probe is built for the accelerometer (a slow, rough move sampled for
    # seconds); a quick smooth jog is not, and reads as nothing half the time.
    ctx.notice("The head moves 50 mm each way on X and on Y. Keep the bed clear.")
    last = run_check(ctx, "motion", lambda p: "Continue" if p.get("id") == "jogs" else None, 420)
    ctx.clear_notice()
    res = last.get("result") or {}
    ev["probe"] = res.get("probe")
    ev["moves"] = res.get("moves")
    ctx.log("after energize: probe '%s', moves %s", res.get("probe"),
            {k: v.get("witnessed") for k, v in (res.get("moves") or {}).items()})
    ctx.check(res.get("probe") and last.get("result") is not None and not last.get("error"),
              "the motion check did not complete after the energize: %s", last.get("error") or last)
    for name in ("+X", "-X", "+Y", "-Y"):
        m = (res.get("moves") or {}).get(name) or {}
        ctx.check(m.get("witnessed") is True,
                  "the %s jog was not witnessed after the energize: the drivers did not come back (%s)", name, m)
    ok = ctx.wait_for(lambda: (fc.get("/mode")[1] or {}).get("local") is False
                      and (fc.get("/mode")[1] or {}).get("controller") == "running", 90)
    ctx.check(ok is not None, "the controller is not back in its normal posture")
    ev["rail_lines"] = [rail0, _rail_lines()]
    ctx.check(ev["rail_lines"][0] == ev["rail_lines"][1],
              "the kernel logged the 40 V supply changing across a release: %s", ev["rail_lines"])
    machine_idle(ctx)
    ctx.log("PASS: released with the rail untouched, every motion refused and nothing shipped, "
            "energized with no fault, and the liveness probe and the witnessed jogs say the drivers are alive")


@test("homing.manual", title="Manual home: $H declares the stop-block position and moves nothing",
      subsystem="motion", kind="auto", mode="grbl", est_min=4,
      covers=_MOTION_COVERS + [("forgectrl", "src/status.*"), ("forgectrl", "src/main.c")],
      requires=["motion.jog-roundtrip"],
      steps=["Bed clear; the head needs 30 mm of free +X travel."],
      description="With homing_mode = manual, $H ships nothing (the kernel's position record shows no "
                  "byte played across it), sets X and Y to manual_home_x and manual_home_y (the origin "
                  "when they are unset) where the head stands, marks X and Y homed with their soft "
                  "limits on (a jog past the envelope is refused with error 15), leaves Z where it "
                  "was, tells the sender the home was set by hand, and the anchor's source reads back "
                  "as manual through forgectrl. Whatever a check decides, the false reference is "
                  "dropped and the head returned before the machine is handed back.")
def manual_home(ctx):
    ev = ctx.evidence
    fc = hw.Forgectrl()
    settings = fc.settings() or {}
    ev["homing_mode"] = settings.get("homing_mode")
    want = []
    for key in ("manual_home_x", "manual_home_y"):
        try:
            want.append(float(settings.get(key) or 0.0))
        except ValueError:
            want.append(0.0)
    ev["manual_home"] = want
    moved_out = False
    st, body = fc.post("/settings", data={"homing_mode": "manual"})
    ctx.check(st == 200, "homing_mode=manual -> %s %s", st, body)
    try:
        with ctx.grbl() as g:
            clean_slate(ctx, g)
            machine_idle(ctx)
            k_start = _kernel_position()
            r = g.command("$J=G91X30F2400")         # away from wherever the origin was
            ctx.check(not any(x.startswith("error") for x in r), "the outbound jog was refused: %s", r)
            moved_out = True
            wait_idle(ctx, g)
            z0 = g.status_report()["MPos"][2]
            ctx.sleep(1.5)                          # the kernel has played the jog's tail
            k0 = _kernel_position()
            r = g.command("$H", timeout=15)
            ctx.check(not any(x.startswith("error") for x in r), "$H under manual refused: %s", r)
            text = "\n".join(r) + drain_text(g, 1.5)
            k1 = _kernel_position()
            rep = g.status_report()
            st = fc.status()
            ev.update(kernel_position=[list(k0), list(k1)], mpos=rep["MPos"], state=rep["state"],
                      homed_axes=st.get("homed_axes"), home_source=st.get("home_source"),
                      pos=st.get("pos"))
            ctx.log("$H: %s MPos %s; kernel record %s -> %s; forgectrl homed_axes %s source %s pos %s",
                    rep["state"], rep["MPos"], k0, k1, ev["homed_axes"], ev["home_source"], ev["pos"])
            ctx.check(k1[3:] == k0[3:], "$H played pulse bytes: %s -> %s", k0[3:], k1[3:])
            ctx.check(k1[0] == 0 and k1[1] == 0, "the kernel counters were not cleared: %s", k1)
            # The home cleared the counters out here, not where the run found
            # the head: the hand-back is told what the start reads in the new
            # frame, or it would "return" the head by the length of the jog.
            ctx.counters_rezeroed([k_start[i] - k0[i] for i in range(3)])
            ctx.check(abs(rep["MPos"][0] - want[0]) < 0.01 and abs(rep["MPos"][1] - want[1]) < 0.01,
                      "X and Y are %s, not manual_home %s", rep["MPos"][:2], want)
            ctx.check(rep["MPos"][2] == z0, "Z changed across the home: %s -> %s", z0, rep["MPos"][2])
            ctx.check(rep["state"].startswith("Idle"), "not idle after the home: %s", rep["state"])
            ctx.check(MANUAL_NOTICE in text, "the sender was not told the home was set by hand")
            ctx.check((ev["homed_axes"] or 0) & 3 == 3, "forgectrl does not show X and Y homed")
            ctx.check(ev["home_source"] == "manual", "the anchor's source reads %r", ev["home_source"])
            ctx.check(ev["pos"] and abs(ev["pos"]["x"] - want[0]) < 0.02 and abs(ev["pos"]["y"] - want[1]) < 0.02,
                      "forgectrl's position is not the declared one: %s", ev["pos"])
            # The blocks are a wall: the envelope starts where the head stands.
            ev["past_envelope"] = _refused(ctx, g, "$J=G91X-5F600")
            ctx.check(ev["past_envelope"] == "error:15",
                      "a jog past the envelope after the home got %s, not error:15", ev["past_envelope"])
    finally:
        # Whatever a check above decided, the machine is handed back as it was
        # found: the setting restored, the false home dropped, the head returned.
        st, body = (fc.post("/settings", params={"homing_mode": ""}) if not ev["homing_mode"]
                    else fc.post("/settings", data={"homing_mode": ev["homing_mode"]}))
        ctx.log("restore homing_mode=%r -> %s", ev["homing_mode"], st)
        if moved_out:
            _drop_reference(ctx, fc)
            with ctx.grbl() as g:
                clean_slate(ctx, g)
                r = g.command("$J=G91X-30F2400")    # back to where the test found the head
                ctx.check(not any(x.startswith("error") for x in r), "the return jog was refused: %s", r)
                wait_idle(ctx, g)
    machine_idle(ctx)
    ctx.log("PASS: a manual home shipped nothing, declared %s, turned the soft limits on, kept Z, "
            "and reads back as manual", want)


def _port_state(fc):
    st, body = fc.get("/motion/state")
    if st != 200 or not isinstance(body, dict):
        raise hw.HwError("forgectrl /motion/state -> %s %s" % (st, body))
    return body


def _port_idle(ctx, fc, timeout=30.0):
    """The port's own word that its jog is over. The suite's Grbl client
    sends no line while a port jog runs: a line from it is what cancels one."""
    ok = ctx.wait_for(lambda: (lambda s: s["state"] == "Idle" and not s["port_jog"])(_port_state(fc)),
                      timeout, poll=0.1)
    ctx.check(ok is not None, "the port jog did not end: %s", _port_state(fc))
    machine_idle(ctx)


def _words(body):
    return body if isinstance(body, str) else json.dumps(body)


@test("motion.port-jog", title="The controller port: the panel jogs beside a connected Grbl client",
      subsystem="motion", kind="auto", mode="grbl", est_min=4,
      covers=_MOTION_COVERS + [("forgectrl", "src/grblport.*"), ("forgectrl", "src/main.c"),
                               ("forgectrl", "src/status.*")],
      requires=["motion.jog-roundtrip"],
      steps=["Bed clear; the head needs 180 mm of free travel toward +X. Nobody touches the gantry."],
      description="With the suite connected as the Grbl client, POST /motion/jog moves the head by "
                  "what was asked (the kernel's counters are the witness), the client is told "
                  "([MSG:Panel jog]) and is not displaced: its next line still draws ok. "
                  "GET /motion/state agrees with the kernel. With the client polling the way "
                  "LightBurn does ('?' and an end of line), no port jog is refused for it, a 30 mm "
                  "jog runs whole, and every poll draws its ok. POST /motion/cancel stops a long jog "
                  "short. The client goes first: its line during a port jog cancels the jog and "
                  "draws ok, never an error. A jog past the per-request bound is refused with "
                  "nothing moved. POST /motion/release takes the step currents to 0 and /status "
                  "says so, a jog is then refused in words, and POST /motion/energize restores "
                  "the hold currents. The head is returned to where it was found.")
def port_jog(ctx):
    ev = ctx.evidence
    fc = hw.Forgectrl()
    with ctx.grbl() as g:
        clean_slate(ctx, g)
        x0, y0 = kernel_start(ctx)
        ctx.sleep(0.6)                      # past the port's sender-quiet interval

        # A bounded jog, beside the client.
        st, body = fc.post("/motion/jog", params={"x": "10", "feed": "2400"})
        ctx.check(st == 200, "POST /motion/jog x=10 -> %s %s", st, _words(body))
        _port_idle(ctx, fc)
        x1, y1 = kernel_xy_mm(ctx)
        ps = _port_state(fc)
        ev["jog"] = {"kernel": [x0, x1], "port_mpos": ps.get("mpos"), "sender": ps.get("sender")}
        ctx.log("port jog +10: kernel X %.3f -> %.3f, port state %s", x0, x1, ps)
        ctx.check(abs((x1 - x0) - 10.0) < 0.1, "the kernel moved %.3f mm, not 10", x1 - x0)
        ctx.check(abs(y1 - y0) < 0.05, "Y moved on an X jog: %.3f -> %.3f", y0, y1)
        ctx.check(ps.get("sender") is True, "the port does not see the connected client: %s", ps)
        text = drain_text(g, 0.5)
        ctx.check("Panel jog" in text, "the client was not told of the panel's jog: %r", text)
        r = g.command("G4 P0")
        ctx.check("ok" in r and not any(x.startswith("error") for x in r),
                  "the client was displaced or refused after a port jog: %s", r)

        # The client polls the way LightBurn does: '?' with an end of line
        # behind it. The '?' is a realtime character; the end of line is an
        # empty line, and an empty line is not the client speaking. No jog
        # is refused for it, a jog runs whole under it, each poll draws its ok.
        ctx.sleep(0.6)
        g.drain()
        polls, refused = 0, []
        for _ in range(8):
            g.send_raw(b"?\n")
            polls += 1
            st, body = fc.post("/motion/jog", params={"x": "0.5", "feed": "3000"})
            if st != 200:
                refused.append(_words(body))
            ctx.sleep(0.12)
        ctx.check(not refused, "%d of 8 port jogs were refused under a '?'+EOL poll: %s", len(refused), refused[:2])
        _port_idle(ctx, fc)
        xa = kernel_xy_mm(ctx)[0]
        st, body = fc.post("/motion/jog", params={"x": "30", "feed": "1200"})    # 1.5 s: several polls long
        ctx.check(st == 200, "the jog under the poll -> %s %s", st, _words(body))
        in_jog = 0
        deadline = time.time() + 6
        while time.time() < deadline:
            g.send_raw(b"?\n")
            polls += 1
            ctx.sleep(0.25)
            ps = _port_state(fc)
            if ps["state"] == "Idle" and not ps["port_jog"]:
                break
            in_jog += 1
        _port_idle(ctx, fc)
        xb = kernel_xy_mm(ctx)[0]
        oks = sum(1 for line in drain_text(g, 1.0).splitlines() if line.strip() == "ok")
        ev["poll"] = {"polls": polls, "oks": oks, "polls_in_jog": in_jog, "kernel": [xa, xb]}
        ctx.log("'?'+EOL poll: 8 of 8 jogs accepted; a 30 mm jog moved %.3f with %d polls into it; "
                "%d polls drew %d oks", xb - xa, in_jog, polls, oks)
        ctx.check(in_jog >= 3, "only %d polls landed inside the jog: the case did not test it", in_jog)
        ctx.check(abs((xb - xa) - 30.0) < 0.1, "a status poll cut the port jog short: moved %.3f of 30", xb - xa)
        ctx.check(oks == polls, "%d polls drew %d oks", polls, oks)
        x1 = xb

        # Past the bound: refused at the door, nothing moved.
        st, body = fc.post("/motion/jog", params={"x": "101"})
        ev["past_bound"] = [st, _words(body)]
        ctx.check(st == 400, "a 101 mm jog -> %s %s, expected 400", st, _words(body))
        ctx.check(abs(kernel_xy_mm(ctx)[0] - x1) < 0.01, "a refused jog moved the head")

        # The cancel stops a long jog short.
        ctx.sleep(0.6)
        st, body = fc.post("/motion/jog", params={"x": "40", "feed": "600"})
        ctx.check(st == 200, "the long jog -> %s %s", st, _words(body))
        ctx.sleep(1.0)
        st, body = fc.post("/motion/cancel")
        ctx.check(st == 200, "POST /motion/cancel -> %s %s", st, _words(body))
        _port_idle(ctx, fc)
        x2 = kernel_xy_mm(ctx)[0]
        ev["cancel"] = [x1, x2]
        ctx.log("canceled a 40 mm jog at %.3f mm", x2 - x1)
        ctx.check(1.0 < x2 - x1 < 35.0, "the cancel did not stop the jog short: moved %.3f of 40", x2 - x1)

        # The client goes first: its line cancels the port's jog and draws ok.
        # A fast jog, so the cancel is a real deceleration: the core stays in
        # the jog state for the length of it, and a client line read in that
        # window is the error:9 the hold exists to prevent. A slow jog stops
        # at once and would pass with no hold at all.
        g.drain()
        ctx.sleep(0.6)
        st, body = fc.post("/motion/jog", params={"x": "100", "feed": "6000"})
        ctx.check(st == 200, "the fast jog -> %s %s", st, _words(body))
        ctx.sleep(0.3)
        r = g.command("G4 P0", timeout=15)
        _port_idle(ctx, fc)
        x3 = kernel_xy_mm(ctx)[0]
        ev["sender_first"] = {"reply": r, "kernel": [x2, x3]}
        ctx.log("the client's line during a port jog drew %s; the jog stopped at %.3f of 100", r, x3 - x2)
        ctx.check("ok" in r and not any(x.startswith("error") for x in r),
                  "the client's line during a port jog drew %s, not ok", r)
        ctx.check(2.0 < x3 - x2 < 95.0, "the client's line did not cancel the port jog: moved %.3f", x3 - x2)

        # The panel's own: the release and its end, by their routes.
        ctx.sleep(0.6)
        st, body = fc.post("/motion/release")
        ctx.check(st == 200, "POST /motion/release -> %s %s", st, _words(body))
        ctx.sleep(0.5)
        ev["released"] = {"currents": list(_step_currents()),
                          "status": fc.status().get("motors_released"),
                          "port": _port_state(fc).get("released")}
        ctx.check(ev["released"]["currents"] == ["0", "0"],
                  "the step currents read %s after the release", ev["released"]["currents"])
        ctx.check(ev["released"]["status"] is True and ev["released"]["port"] is True,
                  "the release is not reported: %s", ev["released"])
        k0 = _kernel_position()
        st, body = fc.post("/motion/jog", params={"x": "1"})
        ev["jog_released"] = [st, _words(body)]
        ctx.check(st == 409 and "released" in _words(body),
                  "a jog while released -> %s %s", st, _words(body))
        ctx.check(_kernel_position() == k0, "the kernel played something while released")
        st, body = fc.post("/motion/energize")
        ctx.check(st == 200, "POST /motion/energize -> %s %s", st, _words(body))
        ctx.sleep(0.5)
        ev["energized"] = {"currents": list(_step_currents()),
                           "status": fc.status().get("motors_released"),
                           "faults": hw.sysfs_int("cnc/faults")}
        ctx.check(tuple(ev["energized"]["currents"]) == HOLD_CURRENTS and ev["energized"]["status"] is False,
                  "not energized: %s", ev["energized"])
        ctx.check(ev["energized"]["faults"] == 0, "a driver fault on energize: %s", ev["energized"]["faults"])

        # Back to where the head was found, by the kernel's own measure.
        ctx.sleep(0.6)
        for _ in range(3):                  # one request moves 100 mm at most
            back = x0 - kernel_xy_mm(ctx)[0]
            if abs(back) < 0.05:
                break
            st, body = fc.post("/motion/jog", params={"x": "%.3f" % max(-100.0, min(100.0, back)),
                                                     "feed": "2400"})
            ctx.check(st == 200, "the return jog -> %s %s", st, _words(body))
            _port_idle(ctx, fc)
            ctx.sleep(0.6)
        check_kernel_returned(ctx, ev, (x0, y0), tag="port")
    machine_idle(ctx)
    ctx.log("PASS: the port jogged beside the client, the cancel and the client's own line each stopped "
            "a jog short, the bound held, and the release and the energize went through their routes")
