# Copyright 2026 514 LLC d/b/a OpenGlow
# Written by Scott Wiederhold
# https://community.openglow.org
# SPDX-License-Identifier:    MIT

"""cooling.* - the cooling engine on the machine: the air-assist ground
shift on the coolant readings, flow verification through forgectrl's
diagnostics runner (the same check the fire gate runs) and under a lit
tube, and the fail tier ending the controller on the head's own move
inside an armed window. The engine's decisions on a reading a host can
emulate (the gate settings, the phases' fan duties, the TEC, the fire and
crash watches' tiers and gates) are forgectrl's cool_flow_test and
cool_gate_test."""
import os
import time

from ..catalog import test
from .. import hw

_COOL_COVERS = [("forgectrl", "src/cool.*"), ("forgectrl", "src/coolfmt.*"), ("forgectrl", "src/diag.*"),
                ("forgectrl", "src/gates.*"), ("forgectrl", "src/airflow.*"),
                ("forgectrl", "src/accel.*"),
                ("forgectrl", "src/settings.*"),
                ("forgectrl", "src/status.*"), ("forgectrl", "src/ui/**"),
                ("grblhal-glowforge", "src/glowforge_cooling.*"),
                ("kernel-module-glowforge", "src/thermal*"),
                ("kernel-module-glowforge", "src/pic*")]


# Registered before the heater tools: a queue runs the catalog in this
# order, and a heater trial leaves a warm slug circulating past the
# coolant sensors for minutes, which the tool's edges read as disagreement.
@test("cooling.aa-offset-calibrate", title="The air-assist ground shift on the coolant readings measures cleanly",
      subsystem="cooling", kind="auto", est_min=6,
      covers=_COOL_COVERS, requires=["kernel.latch-locked-idle"],
      steps=["Coolant loop normal (pump on); the machine idle. The controller is suspended by "
             "forgectrl for the duration; the tool first waits for a stationary loop (up to "
             "7 minutes after a heater trial), then the air assist cycles three times."],
      description="forgectrl's aa-offset-calibrate diagnostic: the loop settled to the flow "
                  "tools' stationary gate, then the air-assist fan stepped idle to run and back "
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


@test("cooling.flow-verify", title="Coolant flow check separates flow from no-flow",
      subsystem="cooling", kind="auto", est_min=4,
      covers=_COOL_COVERS, requires=["kernel.latch-locked-idle"],
      steps=["Coolant loop normal (pump on); the machine idle. The controller is suspended by "
             "forgectrl for the duration (about 3 minutes)."],
      description="forgectrl's flow-verify diagnostic: one heater window with the pump on and "
                  "one with it commanded off, judged against the configured threshold. PASS = "
                  "the threshold separates the two readings with the margins forgectrl reports; "
                  "a thin margin is recorded as a warning.")
def flow_verify(ctx):
    fc = ctx.forgectrl
    ev = ctx.evidence
    st, body = fc.get("/diag/status")
    ctx.check(st == 200 and isinstance(body, dict), "GET /diag/status -> %s", st)
    ctx.check(not body.get("running"), "a diagnostic is already running (%s)", body.get("tool"))
    st, body = fc.post("/diag/flow-verify")
    ctx.log("POST /diag/flow-verify -> %s %s", st, body if isinstance(body, dict) else "")
    ctx.check(st == 202 and isinstance(body, dict) and body.get("started") is True,
              "could not start flow-verify (%s %s)", st, body)
    last_phase = None
    result = None
    t0 = time.time()
    try:
        while time.time() - t0 < 900:
            ctx.checkpoint()
            st, d = fc.get("/diag/status")
            if st == 200 and isinstance(d, dict):
                if d.get("phase") != last_phase:
                    last_phase = d.get("phase")
                    ctx.log("phase: %s (down %.1f C, up %.1f C)", last_phase, d.get("down_c", 0), d.get("up_c", 0))
                if not d.get("running") and d.get("result") is not None:
                    result = d.get("result")
                    for line in d.get("log", [])[-12:]:
                        ctx.log("  diag: %s", line)
                    break
            time.sleep(2)
    except BaseException:
        fc.post("/diag/abort")
        raise
    ctx.check(result is not None, "flow-verify did not finish within 15 minutes")
    ev["result"] = result
    ctx.check("error" not in result, "flow-verify error: %s", result.get("error"))
    ctx.log("verdict: pass=%s threshold=%s flow_rise=%s noflow_rise=%s margins %s/%s thin=%s",
            result.get("pass"), result.get("threshold"), result.get("flow_rise"),
            result.get("noflow_rise"), result.get("margin_flow"), result.get("margin_noflow"),
            result.get("thin_margin"))
    ctx.check(result.get("pass") is True, "the threshold does not separate flow from no-flow: %s", result)
    if result.get("thin_margin"):
        ctx.log("WARNING: thin margin - run flow-calibrate")
    ctx.check(fc.wait_idle(120, abort=ctx.aborted), "machine did not return to idle after the diagnostic")


AA_CAL_TIMEOUT_S = 540      # the tool's 420 s settle limit plus its six edges


def _cool(fc):
    st, c = fc.get("/cool/status")
    return c if st == 200 and isinstance(c, dict) else {}


def _gate_state(fc, key):
    g = (fc.settings().get("gates") or {}).get(key) or {}
    return g.get("state"), g.get("value")


def _set_gates(ctx, fc, values):
    """POST the gate settings and confirm the reply carries them."""
    st, body = fc.post("/settings", params=values)
    ctx.check(st == 200 and isinstance(body, dict), "POST /settings %s -> %s %s", values, st, body)
    for k, v in values.items():
        ctx.check(body.get(k) == v, "settings reply has %s=%r, posted %r", k, body.get(k), v)
    return body


CRASH_KEYS = ("cool_accel_x_alert", "cool_accel_y_alert", "cool_accel_abort")


# The flow check under a lit tube. With a prompt press the tube is lit for
# most of the arm-time window, which adds about 1.5 C to the heater rise at
# full power; the engine takes that share off from the tube current before
# the limit (cool_laser_heat_*), reads its baseline and end as means, and
# says so on the verdict line: "verified (heater rise 11.3 C, dT 9.6 C;
# laser 0.8 off 12.0)". The share on the line is the proof the window and
# the fire overlapped; the judged rise inside the dark band is the proof
# the share was right.
LOAD_FEED = 1500                    # mm/min: 25 mm/s, the engrave dose that does not char
LOAD_PITCH = 0.3                    # mm between lines
LOAD_WIDTH = 30.0                   # mm, the line length (+X)
LOAD_LINES = 27                     # two 30 x 4 mm fills back to back, about 35 s lit
LOAD_SHARE_MIN_C = 0.3              # the tube was lit inside the window
LOAD_MARGIN_C = 1.0                 # the judged rise this far under the limit
LOAD_VERDICT_WAIT_S = 120           # the window opens ~15 s into the session and runs 50 s
_LOAD_LINE_RX = None


def _load_verdict_rx():
    global _LOAD_LINE_RX
    if _LOAD_LINE_RX is None:
        import re
        _LOAD_LINE_RX = re.compile(
            r"(COOLANT FLOW SUSPECT|COOLANT FLOW FAULT|coolant flow verified|"
            r"coolant flow suspicion cleared|coolant flow recovered)[^\n]*?heater rise ([0-9.]+) C"
            r"(?: \(limit ([0-9.]+))?, dT ([0-9.]+)(?: C)?(?:; laser ([0-9.]+) off ([0-9.]+))?\)?")
    return _LOAD_LINE_RX


def _load_fill():
    lines = []
    for i in range(LOAD_LINES):
        lines.append("G1 X%g F%d" % (LOAD_WIDTH if i % 2 == 0 else -LOAD_WIDTH, LOAD_FEED))
        if i < LOAD_LINES - 1:
            lines.append("G1 Y%g F%d" % (LOAD_PITCH, LOAD_FEED))
    return lines


def _log_since(path, offset):
    """The text a log gained since byte `offset`. A count of lines in a
    tail of fixed length cannot tell a new line from an old one: the new
    line comes in at the bottom as an old one leaves at the top. A file now
    shorter than the offset was rotated under the test, and all of it is
    newer."""
    try:
        with open(path, "rb") as f:
            if os.fstat(f.fileno()).st_size >= offset:
                f.seek(offset)
            return f.read().decode("utf-8", "replace")
    except OSError:
        return ""


@test("cooling.flow-under-load", title="The flow check reads true with the tube lit through its window",
      subsystem="cooling", kind="live", mode="grbl", est_min=4,
      covers=_COOL_COVERS + [("forgectrl", "src/main.c")],
      requires=["laser.emission-witness", "cooling.flow-verify"], actions=["button"],
      steps=["Scrap under the head with 35 mm of free +X and 10 mm of +Y travel; lid closed; exhaust on.",
             "Press the physical button AS SOON AS it lights white: the check must run while the "
             "tube is lit, and the share on the verdict line is the proof it did."],
      description="Two 30 x 4 mm fills at full power on the press (about 35 s lit), the window "
                  "held open until the engine's flow verdict lands. The verdict must be "
                  "'verified' with the laser's share on the line (at least 0.3 C: the tube was "
                  "lit inside the window) and the judged rise at least 1 C under the limit, "
                  "where the same check read within tenths of the limit before the engine took "
                  "the tube's share off and read its baseline as a mean.")
def flow_under_load(ctx):
    from .laser import sample, prepare, LiveJob, ARM_CUE, run_and_sample, wait_disarm
    from .motion import FORGECTRL_LOG, _log_offset
    fc = ctx.forgectrl
    ev = ctx.evidence
    state, check_s = _gate_state(fc, "cool_flow_check_s")
    _st, limit = _gate_state(fc, "cool_flow_rise")
    ctx.check(state != "off" and check_s, "the flow check is off (cool_flow_check_s=%s)", check_s)
    ctx.check(limit, "cool_flow_rise unreadable")
    ev.update({"check_s": check_s, "limit_c": limit})
    off = _log_offset(FORGECTRL_LOG)     # the verdict is a line written after this
    # The fill ends one line length out and (LOAD_LINES - 1) pitches up;
    # the job brings the head back so the baseline finds it where it began.
    back = "G0 X%g Y%g" % (-LOAD_WIDTH if LOAD_LINES % 2 else 0.0, -LOAD_PITCH * (LOAD_LINES - 1))
    job = ["G91", "G21", "M3 S1000"] + _load_fill() + ["M5", back]
    line = None
    heater_seen = False
    with ctx.grbl() as g, LiveJob(ctx, g):
        prepare(ctx, g)
        base = sample(ctx)
        ctx.check(base, "forgectrl /status or /cool/status unavailable")
        ctx.check(not base["emission"], "emission_samples nonzero before the job (%s)", base["emission"])
        ctx.ready(ARM_CUE % "35 mm +X and 10 mm +Y")
        ctx.arm_press()
        t_job = time.time()
        try:
            samples = run_and_sample(ctx, g, job, overall_timeout=240)
        finally:
            ctx.clear_notice()
        emis = [s["emission"] for s in samples if s["emission"] is not None]
        ev["emission_peak"] = max(emis) if emis else 0
        # An arm refused by a gate names itself: the engine's verdict left OK
        # during the wait (AIRFLOW, OVERTEMP, a flow FAULT).
        gates = sorted({s["verdict"] for s in samples if s.get("verdict") not in (None, "OK")})
        ev["verdicts_seen"] = gates
        ctx.check(ev["emission_peak"] > 0,
                  "no emission witnessed: %s" % ("the engine held the run (%s) before the arm"
                                                  % ", ".join(gates) if gates
                                                  else "arm refused, or no button press"))
        # The window stays open (no M2) until the check has judged.
        t0 = time.time()
        while time.time() - t0 < LOAD_VERDICT_WAIT_S:
            ctx.checkpoint()
            if hw.sysfs_int("thermal/heater_pwm", 0) > 0:
                heater_seen = True
            m = None
            for m in _load_verdict_rx().finditer(_log_since(FORGECTRL_LOG, off)):
                pass
            if m:
                line = m.group(0)
                break
            ctx.sleep(1)
        ev["heater_seen"] = heater_seen
        ctx.log("M2: %s", g.command("G90\nM2", timeout=10))
        dt = wait_disarm(ctx, 30)
        ev["disarm_after_m2_s"] = round(dt, 1) if dt is not None else None
    ctx.check(line, "the engine published no flow verdict within %d s of the job (heater seen: %s)",
              LOAD_VERDICT_WAIT_S, heater_seen)
    m = _load_verdict_rx().search(line)
    kind, rise, _lim, dtc, share, raw = m.groups()
    rise = float(rise)
    share = float(share) if share else 0.0
    raw = float(raw) if raw else rise
    ev.update({"verdict_line": line, "kind": kind, "rise_c": rise, "dt_c": float(dtc),
               "laser_share_c": share, "raw_rise_c": raw, "job_to_verdict_s": round(time.time() - t_job, 1)})
    ctx.log("engine: %s", line)
    ctx.check(kind == "coolant flow verified", "the check did not verify flow under load: %s", line)
    ctx.check(share >= LOAD_SHARE_MIN_C,
              "the tube's share on the line is %.1f C (under %.1f): the window and the fire did not "
              "overlap - press the button as soon as it lights", share, LOAD_SHARE_MIN_C)
    ctx.check(rise <= float(limit) - LOAD_MARGIN_C,
              "judged rise %.1f C is within %.1f C of the %.1f C limit", rise, LOAD_MARGIN_C, float(limit))
    ctx.check(dt is not None, "the window did not close after M2")
    ctx.log("PASS: lit through the window, raw rise %.1f C, laser %.1f C off, judged %.1f C against %.1f "
            "(dT %.1f), window closed %.1f s after M2", raw, share, rise, float(limit), float(dtc), dt)


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
    finally:
        fc.wait_idle(30, abort=ctx.aborted)
        st, body = fc.post("/settings", params=orig)
        ctx.log("restore thresholds: POST /settings %s -> %s", orig, st)
    after = fc.settings()
    ctx.check(all(after.get(k, "") == orig[k] for k in CRASH_KEYS),
              "thresholds not restored: %s", {k: after.get(k) for k in CRASH_KEYS})
    ctx.log("PASS: the crash tier stopped the job, locked the latch, and the supervisor restarted "
            "the controller (pid %s -> %s)", pid0, m1 and m1.get("pid"))
