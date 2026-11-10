# Copyright 2026 514 LLC d/b/a OpenGlow
# Written by Scott Wiederhold
# https://community.openglow.org
# SPDX-License-Identifier:    MIT

"""cooling.* - the cooling engine on the machine: the air-assist ground
shift on the coolant readings, and the fail tier ending the controller on
the head's own move inside an armed window. The engine's decisions on a
reading a host can emulate (the gate settings, the phases' fan duties, the
TEC, the fire and crash watches' tiers and gates) are forgectrl's
cool_gate_test."""
import time

from ..catalog import test
from .. import hw

_COOL_COVERS = [("forgectrl", "src/cool.*"), ("forgectrl", "src/coolfmt.*"), ("forgectrl", "src/diag.*"),
                ("forgectrl", "src/led.*"),
                ("forgectrl", "src/gates.*"), ("forgectrl", "src/airflow.*"),
                ("forgectrl", "src/accel.*"),
                ("forgectrl", "src/settings.*"),
                ("forgectrl", "src/status.*"), ("forgectrl", "src/ui/**"),
                ("grblhal-glowforge", "src/glowforge_cooling.*"),
                ("kernel-module-glowforge", "src/thermal*"),
                ("kernel-module-glowforge", "src/heater_lease.h"),
                ("kernel-module-glowforge", "src/pic*")]


@test("cooling.aa-offset-calibrate", title="The air-assist ground shift on the coolant readings measures cleanly",
      subsystem="cooling", kind="auto", est_min=6,
      covers=_COOL_COVERS, requires=["kernel.latch-locked-idle"],
      steps=["Coolant loop normal (pump on); the machine idle. The controller is suspended by "
             "forgectrl for the duration; the tool first waits for a stationary loop (up to "
             "7 minutes), then the air assist cycles three times."],
      description="forgectrl's aa-offset-calibrate diagnostic: the loop settled to a "
                  "stationary gate, then the air-assist fan stepped idle to run and back "
                  "three times with the tube dark and the heater off, both coolant sensors read "
                  "at every edge. The fan's return current shifts both readings by a number of "
                  "ADC counts the engine adds back (cool_aa_offset_counts). PASS = the tool "
                  "reports a recommendation, the six edges agree within its spread limit, and "
                  "the value sits inside the setting's legal range. The setting is not written.")
def aa_offset_calibrate(ctx):
    fc = ctx.forgectrl
    ev = ctx.evidence
    st, body = fc.get("/diag/status")
    ctx.check(st == 200 and isinstance(body, dict), "GET /diag/status -> %s", st)
    ctx.check(not body.get("running"), "a diagnostic is already running (%s)", body.get("tool"))
    st, body = fc.post("/diag/aa-offset-calibrate")
    ctx.log("POST /diag/aa-offset-calibrate -> %s %s", st, body if isinstance(body, dict) else "")
    ctx.check(st == 202 and isinstance(body, dict) and body.get("started") is True,
              "could not start aa-offset-calibrate (%s %s)", st, body)
    result = None
    last_phase = None
    t0 = time.time()
    try:
        while time.time() - t0 < AA_CAL_TIMEOUT_S:
            ctx.checkpoint()
            st, d = fc.get("/diag/status")
            if st == 200 and isinstance(d, dict):
                if d.get("phase") != last_phase:
                    last_phase = d.get("phase")
                    ctx.log("phase: %s", last_phase)
                if not d.get("running") and d.get("result") is not None:
                    result = d.get("result")
                    for line in d.get("log", [])[-10:]:
                        ctx.log("  diag: %s", line)
                    break
            time.sleep(2)
    except BaseException:
        fc.post("/diag/abort")
        raise
    ctx.check(result is not None, "aa-offset-calibrate did not finish within %d s", AA_CAL_TIMEOUT_S)
    ev["result"] = result
    ctx.check("error" not in result, "aa-offset-calibrate error: %s", result.get("error"))
    ctx.log("offset %s counts, spread %s, recommend %s, edges %s", result.get("offset_counts"),
            result.get("spread_counts"), result.get("recommend"), result.get("steps"))
    ctx.check("recommend" in result, "no recommendation in %s", result)
    ctx.check(0 <= float(result["recommend"]) <= 60,
              "recommendation %s outside the setting's range 0..60", result["recommend"])
    ctx.check(fc.wait_idle(120, abort=ctx.aborted), "machine did not return to idle after the diagnostic")


AA_CAL_TIMEOUT_S = 540      # the tool's 420 s settle limit plus its six edges


def _cool(fc):
    st, c = fc.get("/cool/status")
    return c if st == 200 and isinstance(c, dict) else {}


def _set_gates(ctx, fc, values):
    """POST the gate settings and confirm the reply carries them."""
    st, body = fc.post("/settings", params=values)
    ctx.check(st == 200 and isinstance(body, dict), "POST /settings %s -> %s %s", values, st, body)
    for k, v in values.items():
        ctx.check(body.get(k) == v, "settings reply has %s=%r, posted %r", k, body.get(k), v)
    return body


CRASH_KEYS = ("cool_accel_x_alert", "cool_accel_y_alert", "cool_accel_abort")


@test("cooling.fail-tier-stop", title="A fail-tier verdict ends the controller: CRASH stops motion, "
                                      "locks the laser, and the supervisor restarts the controller",
      subsystem="cooling", kind="operator", mode="grbl", est_min=4,
      covers=_COOL_COVERS + [("forgectrl", "src/super.*")],
      requires=["motion.deadman"], actions=["button"],
      steps=["Bed clear, lid closed, 40 mm of free +X travel. Nothing fires: the job is dark (S0).",
             "Press the physical button when it lights white, once. The head moves and stops on "
             "its own; the controller restarts."],
      description="The engine's fail tiers (a lid IR fire signal, a head crash signal) do not "
                  "leave the job to the controller: after the kernel writes (motion stopped, the "
                  "latch locked) the engine ends the controller through the supervisor, which "
                  "starts it again, so no run start can relight what was locked and the sender "
                  "sees the job end. Provable without emission or a physical crash: the crash "
                  "watch's thresholds at their lowest (1) make the head's own move trip the abort "
                  "generator inside an armed, dark (S0) job. Expected: the engine logs the crash "
                  "signal, the supervisor logs the stop and starts a new controller (new pid), the "
                  "latch is locked, the kernel idle, no emission, the crash fault ends with the run "
                  "session (the engine's next tick after the controller is gone), and the "
                  "thresholds are put back.")
def fail_tier_stop(ctx):
    from .laser import prepare, stream, sample
    from .motion import FORGECTRL_LOG, _log_lines, _log_offset
    fc = ctx.forgectrl
    ev = ctx.evidence
    before = fc.settings()
    orig = {k: before.get(k, "") for k in CRASH_KEYS}
    ev["orig"] = orig
    ctx.check(fc.wait_idle(15, abort=ctx.aborted), "machine not idle at the start")
    st, m0 = fc.get("/mode")
    ctx.check(st == 200 and isinstance(m0, dict) and m0.get("controller") == "running", "controller not running: %s", m0)
    pid0 = m0.get("pid")
    _set_gates(ctx, fc, {k: "1" for k in CRASH_KEYS})
    off = _log_offset(FORGECTRL_LOG)
    t_arm = None
    try:
        try:
            with ctx.grbl() as g:
                prepare(ctx, g)
                ctx.ready("DARK JOB (S0, nothing fires). Bed clear with 40 mm of free +X travel, lid closed. "
                          "The head moves 20 mm and the controller restarts on its own.")
                stream(g, ["G91", "G21", "M3 S0", "G4 P1", "G1 X20 F1200", "G1 X-20 F1200", "M5", "G90", "M2"])
                ctx.arm_press()
                # The window is the engine's armed session. /cool/status's "armed" is
                # the controller's last report, cleared the moment the supervisor stops
                # it, and with the thresholds at 1 the abort generator can trip on the
                # first poll after the watch arms (the head at rest under the run fans):
                # a window of tens of milliseconds the 0.25 s poll can miss. The engine
                # logs the watch arming only inside an armed session, so that line is
                # the window's witness as well.
                t_arm = ctx.wait_for(lambda: bool((sample(ctx) or {}).get("armed"))
                                     or bool(_log_lines(FORGECTRL_LOG, off, "crash watch armed")), 240)
                ctx.clear_notice()
                ctx.check(t_arm is not None, "the window never opened (arm refused, or no press)")
                t0 = time.time()
                tripped = ctx.wait_for(lambda: _cool(fc).get("verdict") == "CRASH"
                                       or (fc.get("/mode")[1] or {}).get("pid") not in (None, pid0), 30)
                ctx.log("the crash tier tripped after %s s", tripped)
        except (hw.HwError, OSError) as e:
            ctx.log("the Grbl connection ended with the restart: %s", e)
        t_kill = time.time()
        m1 = None
        while time.time() - t_kill < 60:
            st, m1 = fc.get("/mode")
            if isinstance(m1, dict) and m1.get("controller") == "running" and m1.get("pid") != pid0:
                break
            ctx.sleep(0.5)
        # the daemon's lines reach the log file through syslog, a moment
        # after the events they name
        ctx.wait_for(lambda: bool(_log_lines(FORGECTRL_LOG, off, "HEAD CRASH SIGNAL"))
                     and bool(_log_lines(FORGECTRL_LOG, off, "head crash signal - the controller is stopped")), 15)
        engine = _log_lines(FORGECTRL_LOG, off, "HEAD CRASH SIGNAL")
        super_ = _log_lines(FORGECTRL_LOG, off, "head crash signal - the controller is stopped")
        smp = sample(ctx)
        ilk = hw.sysfs_int("cnc/interlock_circuit")
        ev["result"] = {"mode_after": m1, "engine_lines": [ln[-160:] for ln in engine[-2:]],
                        "super_lines": [ln[-160:] for ln in super_[-2:]],
                        "emission": smp and smp["emission"], "kernel": smp and smp["kstate"],
                        "latch_locked": ilk is not None and bool(ilk & (1 << 3))}
        ctx.log("after the crash tier: %s", ev["result"])
        ctx.check(engine, "the engine did not log the crash signal")
        ctx.check(super_, "the supervisor did not log the fail-tier stop")
        ctx.check(m1 and m1.get("pid") != pid0, "the controller was not stopped and started again: %s", m1)
        ctx.check(ev["result"]["latch_locked"], "latch not locked after the crash tier")
        ctx.check(not (smp and smp["emission"]), "emission during a dark job: %s", smp and smp["emission"])
        ctx.check(fc.wait_idle(30, abort=ctx.aborted), "machine not idle after the restart")
        # The crash fault is the run session's: the engine ends the session
        # on its next 1 Hz tick after the controller is gone, and the fault
        # and its hold go with it. The supervisor has the new controller up
        # inside that second, so a test that returned here would hand the
        # machine back with the fault it raised still standing.
        cleared = ctx.wait_for(lambda: _cool(fc).get("verdict") != "CRASH", 10)
        ev["result"]["fault_cleared_s"] = cleared
        ctx.log("the crash fault ended with the run session after %s s: %s", cleared,
                {k: _cool(fc).get(k) for k in ("phase", "verdict", "hold", "armed")})
        ctx.check(cleared is not None, "the crash fault did not end with the run session: %s", _cool(fc))
        # It blocks the next job until the operator has seen it.
        cs = _cool(fc)
        ev["result"]["after_session"] = {k: cs.get(k) for k in ("verdict", "fire_ok", "attention")}
        ctx.check(cs.get("verdict") == "ACK" and cs.get("fire_ok") is False,
                  "the crash fault does not block the next job: %s", cs)
        red = hw.button_pulses()
        ev["result"]["button_pulses"] = red
        ctx.check(red[0] and not red[1] and not red[2], "the button is not flashing red: %s", red)
        st, _ = fc.post("/cool/attention/ack")
        ctx.check(st == 200, "the acknowledgment was refused: %s", st)
        freed = ctx.wait_for(lambda: _cool(fc).get("verdict") == "OK", 5)
        ctx.check(freed is not None, "the acknowledgment did not free the machine: %s", _cool(fc))
        ctx.check(ctx.wait_for(lambda: hw.button_lit() is False, 5) is not None,
                  "the button is still lit after the acknowledgment")
    finally:
        fc.wait_idle(30, abort=ctx.aborted)
        st, body = fc.post("/settings", params=orig)
        ctx.log("restore thresholds: POST /settings %s -> %s", orig, st)
    after = fc.settings()
    ctx.check(all(after.get(k, "") == orig[k] for k in CRASH_KEYS),
              "thresholds not restored: %s", {k: after.get(k) for k in CRASH_KEYS})
    ctx.log("PASS: the crash tier stopped the job, locked the latch, and the supervisor restarted "
            "the controller (pid %s -> %s)", pid0, m1 and m1.get("pid"))
