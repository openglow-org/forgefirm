"""cooling.* - the cooling engine: flow verification through forgectrl's
diagnostics runner (the same check the fire gate runs), the fan profile
returning to idle after motion, the gate settings (a value inside the
legal range trips the gate, the far end of the range turns it off by
value, and both are said out loud: the settings reply, /status, the
engine's run-start log line), and the airflow gates (a fan under its
floor past the spin-up grace is a fault for the rest of the run)."""
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


IDLE_DUTY = {"thermal/exhaust_pwm": 0, "thermal/intake_pwm": 0}   # forgectrl's idle posture
TACH_KEYS = ("exhaust", "intake_1", "intake_2")
SAMPLE_S = 5                # tach sampling period (the big exhaust fan coasts for tens of seconds)
STABLE_SAMPLES = 3          # consecutive agreeing samples that make an idle reference
IDLE_REF_TIMEOUT_S = 180    # a previous test's cooldown + spin-down
COOLDOWN_TIMEOUT_S = 240    # the engine's smoke phase + idle drop + spin-down
RESTART_IDLE_TIMEOUT_S = 90  # a daemon restart: the controller's start, its liveness probe, the engine's tick


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


def _duties():
    return {k: hw.sysfs_int(k) for k in IDLE_DUTY}


def _tach_close(now, ref):
    """Every tach at or below its idle reference, within the tolerance a
    tach reading wanders by (150 rpm or 15 %). Lower is quieter, never a
    fault: the reference is an idle level, not a target."""
    return all(now.get(k, 0) - ref.get(k, 0) <= max(150, 0.15 * max(ref.get(k, 0), 1))
               for k in TACH_KEYS)


def _tach_stable(a, b):
    return all(abs(a.get(k, 0) - b.get(k, 0)) <= max(100, 0.10 * max(a.get(k, 0), 1)) for k in TACH_KEYS)


@test("cooling.fans-quiet-after-motion", title="Fan profile returns to idle after motion, after M8/M9, "
                                                  "and after a daemon restart on a busy machine",
      subsystem="cooling", kind="auto", mode="grbl", est_min=5,
      covers=_COOL_COVERS + [("forgectrl", "src/super.c")], requires=["motion.pacing"],
      steps=["Bed clear; the head needs 20 mm of free +X travel."],
      description="A dry jog and an M8/M9 cycle must not leave the run fan profile on: within "
                  "the cooldown the engine is back at its idle duty and the exhaust/intake tachs "
                  "are back at (or below) the idle level they held before the test. The idle "
                  "reference is taken only once the engine is idle and the tachs have stopped "
                  "changing, so a previous test's spin-down cannot be mistaken for idle. Then a "
                  "forgectrl restart with the kernel not idle (a takeover's safe state): the "
                  "engine starts in its cooldown posture and must take the idle duties back once "
                  "the machine is idle, not keep the exhaust at cooldown duty for good.")
def fans_quiet(ctx):
    fc = ctx.forgectrl
    ev = ctx.evidence

    def fans():
        s = fc.status()
        return dict(s.get("fans") or {})

    def phase():
        st, c = fc.get("/cool/status")
        return (c or {}).get("phase") if st == 200 and isinstance(c, dict) else None

    # The idle reference: engine idle, idle duty applied, and tachs that
    # have stopped changing (STABLE_SAMPLES consecutive samples that
    # agree). A test that ran just before leaves the fans spinning down
    # for tens of seconds after the engine goes idle; a reference taken
    # then is not idle, and two samples can agree by chance mid-coast.
    t0 = time.time()
    before = None
    recent = []
    while time.time() - t0 < IDLE_REF_TIMEOUT_S:
        ctx.sleep(SAMPLE_S)
        now, ph, d = fans(), phase(), _duties()
        ctx.log("  idle ref: phase %s duty %s fans %s", ph, d, now)
        recent = (recent + [now])[-STABLE_SAMPLES:] if ph == "idle" and d == IDLE_DUTY else []
        if len(recent) == STABLE_SAMPLES and all(_tach_stable(a, b) for a, b in zip(recent, recent[1:])):
            before = now
            break
    ev["before"] = before
    ev["idle_ref_s"] = round(time.time() - t0, 1)
    ctx.check(before is not None, "the fans never settled to an idle reference within %d s (last %s, "
              "phase %s, duty %s)", IDLE_REF_TIMEOUT_S, recent[-1:] or None, phase(), _duties())
    ctx.log("idle reference after %s s: %s", ev["idle_ref_s"], before)

    with ctx.grbl() as g:
        st = g.status_report()
        ctx.check(st["state"].startswith("Idle"), "controller is %s", st["state"])
        g.command("G91")
        g.command("$J=G91X20F2400")
        t0 = time.time()
        while time.time() - t0 < 30 and not g.status_report()["state"].startswith("Idle"):
            ctx.sleep(0.2)
        g.command("$J=G91X-20F2400")
        while time.time() - t0 < 60 and not g.status_report()["state"].startswith("Idle"):
            ctx.sleep(0.2)
        g.command("M8")
        ctx.sleep(3)
        during = fans()
        ev["during_m8"] = during
        ev["duty_m8"] = _duties()
        ctx.log("fans during M8: %s (duty %s)", during, ev["duty_m8"])
        ctx.check(ev["duty_m8"] != IDLE_DUTY, "M8 did not raise the fan duty off idle: %s", ev["duty_m8"])
        g.command("M9")
        g.command("G90")
    # Cooldown: the engine's smoke phase, then the drop to idle duty, then
    # the tachs coast down. Every sample is logged: a quiet pane here
    # looks like a hang.
    settle = None
    t0 = time.time()
    while time.time() - t0 < COOLDOWN_TIMEOUT_S:
        ctx.sleep(SAMPLE_S)
        now, ph, d = fans(), phase(), _duties()
        ctx.log("  cooldown +%3.0f s: phase %s duty %s fans %s", time.time() - t0, ph, d, now)
        if ph == "idle" and d == IDLE_DUTY and _tach_close(now, before):
            settle = time.time() - t0
            break
    ev["after"] = fans()
    ev["duty_after"] = _duties()
    ev["settle_s"] = round(settle, 1) if settle is not None else None
    ctx.log("fans after: %s duty %s (settled in %s s)", ev["after"], ev["duty_after"], ev["settle_s"])
    ctx.check(settle is not None, "fans did not return to the idle profile within %d s: %s, duty %s, "
              "phase %s (idle reference %s)", COOLDOWN_TIMEOUT_S, ev["after"], ev["duty_after"], phase(), before)

    # A daemon restart while the kernel is not idle: forgectrl stopped, the
    # kernel put in the takeover's safe state (disabled), forgectrl started.
    # The engine judges the machine busy and takes the cooldown airflow; the
    # controller's start enables the kernel and the machine is idle again,
    # and the engine must follow it to the idle duties.
    rc, out = hw.initd("forgectrl", "stop")
    ctx.log("forgectrl stop -> rc %s", rc)
    ctx.check(rc == 0, "forgectrl stop failed: %s", (out or "").strip()[:200])
    hw.sysfs_write("cnc/disable", "1")
    ev["kstate_at_restart"] = hw.sysfs_read("cnc/state")
    rc, out = hw.initd("forgectrl", "start")
    ctx.log("forgectrl start with cnc/state=%s -> rc %s", ev["kstate_at_restart"], rc)
    ctx.check(rc == 0, "forgectrl start failed: %s", (out or "").strip()[:200])
    back = None
    t0 = time.time()
    while time.time() - t0 < RESTART_IDLE_TIMEOUT_S:
        ctx.sleep(SAMPLE_S)
        st, m = fc.get("/mode")
        d = _duties()
        ctrl = m.get("controller") if isinstance(m, dict) else st
        ctx.log("  after the restart +%3.0f s: controller %s duty %s", time.time() - t0, ctrl, d)
        if ctrl == "running" and d == IDLE_DUTY:
            back = time.time() - t0
            break
    ev["restart_idle_s"] = round(back, 1) if back is not None else None
    ev["duty_after_restart"] = _duties()
    ctx.check(back is not None, "the engine kept the busy-start airflow after the restart (duty %s "
              "after %d s)", ev["duty_after_restart"], RESTART_IDLE_TIMEOUT_S)


GATE_KEYS = ("cool_temp_max", "cool_temp_resume")
VERDICT_WAIT_S = 20         # the engine reloads settings at run start and ticks at 1 Hz
SESSION_END_WAIT_S = 15     # M9 -> the engine's phase leaves "run" (1 Hz reports, 1 Hz ticks)
GATE_LOG_LINES = "400"      # how far back the run-start gate lines can sit in the forgectrl log


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


def _session_ended(ctx, fc, what):
    """After M9 the GRBL client's next report ends the engine's run
    session; the client reports at 1 Hz and the engine samples at 1 Hz,
    so an M8 sent inside that window is not a new session and nothing is
    re-read. Wait until the phase has left "run"."""
    t0 = time.time()
    while time.time() - t0 < SESSION_END_WAIT_S:
        ctx.sleep(1)
        if _cool(fc).get("phase") != "run":
            return True
    ctx.log("%s: the engine is still in phase run %d s after M9", what, SESSION_END_WAIT_S)
    return False


def _run_session(ctx, g, fc, until, what, wait=None):
    """M8 opens a run session (the engine re-reads its settings there and
    ticks the gates at 1 Hz); wait for `until(cool)` to hold, then M9 and
    wait for the session to end, so the next M8 is a new one."""
    ctx.check(_cool(fc).get("phase") != "run", "%s: a run session is already open", what)
    g.command("M8")
    try:
        t0 = time.time()
        c = {}
        while time.time() - t0 < (wait or VERDICT_WAIT_S):
            ctx.sleep(1)
            c = _cool(fc)
            if until(c):
                break
        ctx.log("%s: verdict %s fire_ok %s hold %s gates_off %s (after %.0f s)", what,
                c.get("verdict"), c.get("fire_ok"), c.get("hold"), c.get("gates_off"), time.time() - t0)
        return c
    finally:
        g.command("M9")
        _session_ended(ctx, fc, what)


def _after_session(ctx, fc, wait=5, until=None):
    """The engine's state a few ticks after a session ended: the first
    sample out of phase run that satisfies `until` (default: verdict OK),
    or the last sample taken."""
    until = until or (lambda c: c.get("verdict") == "OK")
    c = {}
    t0 = time.time()
    while time.time() - t0 < wait:
        ctx.sleep(1)
        c = _cool(fc)
        if c.get("phase") != "run" and until(c):
            break
    return c


def _tail_has(fc, needle):
    st, body = fc.get("/logs/tail", params={"name": "forgectrl", "lines": GATE_LOG_LINES})
    text = body.get("text", "") if st == 200 and isinstance(body, dict) else ""
    return needle in text


@test("cooling.gate-off", title="A gate setting trips inside its range and is off at its far end",
      subsystem="cooling", kind="auto", mode="grbl", est_min=3,
      covers=_COOL_COVERS, requires=["kernel.latch-locked-idle"],
      steps=["Machine idle, coolant at room temperature (above 8 C). The test writes the coolant "
             "ceiling and resume gate and restores them; three short M8/M9 cycles spin the fans."],
      description="The coolant ceiling is a plain setting with a wide legal range whose top "
                  "turns the gate off by value. Set just above its legal minimum it must trip "
                  "(OVERTEMP, hold, fire blocked) at the next run start; set to its top the "
                  "engine must skip the gate (verdict OK), report it in gates_off on /status "
                  "and /cool/status, say so in the settings reply, and log the run-start line; "
                  "restored, everything reads as before. Alongside: /status carries the watched "
                  "board temperatures (chassis and SoC die in degrees, supply as a raw count, the "
                  "CPU unthrottled) and every run session ends with them ranged into one log line.")
def gate_off(ctx):
    fc = ctx.forgectrl
    ev = ctx.evidence
    before = fc.settings()
    orig = {k: before.get(k, "") for k in GATE_KEYS}
    ev["orig"] = orig
    ctx.log("original: %s", orig)
    st0 = fc.status()
    up = (st0.get("coolant") or {}).get("up_c")
    ctx.check(up is not None and up > 8.0, "coolant too cold for the trip leg (up_c %s)", up)
    # The board temperatures ride /status, watched and not gated: the
    # chassis in degrees, the supply as its raw count.
    temps = st0.get("temps") or {}
    ev["temps"] = temps
    ctx.check(isinstance(temps.get("chassis_c"), (int, float)) and 5.0 <= temps["chassis_c"] <= 80.0,
              "/status temps.chassis_c is not a plausible chassis temperature: %s", temps)
    ctx.check(isinstance(temps.get("supply_raw"), int) and 0 <= temps["supply_raw"] <= 1023,
              "/status temps.supply_raw is not a 10-bit count: %s", temps)
    ctx.check(isinstance(temps.get("soc_c"), (int, float)) and 10.0 <= temps["soc_c"] <= 100.0,
              "/status temps.soc_c is not a plausible die temperature: %s", temps)
    ctx.check(temps.get("soc_throttle") == 0,
              "the CPU is throttled (or its cooling device is missing) at idle: %s", temps)
    c0 = _cool(fc)
    ctx.check(c0.get("verdict") == "OK", "engine is not at OK before the test: %s", c0)
    ctx.check(c0.get("gates_off") == [], "a gate is already off: %s", c0.get("gates_off"))
    g_default = (before.get("gates") or {}).get("cool_temp_max") or {}
    ctx.check(g_default.get("gate") == "coolant_max" and g_default.get("off") == "high",
              "settings reply does not describe the ceiling as the coolant_max gate, off at its top: %s", g_default)
    top = g_default.get("hi")
    bottom = g_default.get("lo")
    ctx.check(isinstance(top, (int, float)) and isinstance(bottom, (int, float)), "no range in the reply: %s", g_default)

    restored = False
    with ctx.grbl() as grbl:
        st = grbl.status_report()
        ctx.check(st["state"].startswith("Idle"), "controller is %s", st["state"])
        try:

            # Leg 1: a ceiling the coolant is already over. Legal, outside
            # the band (warned), and it must trip at the next run start.
            _set_gates(ctx, fc, {"cool_temp_max": str(bottom + 1), "cool_temp_resume": str(bottom)})
            state, val = _gate_state(fc, "cool_temp_max")
            ev["trip_state"] = state
            ctx.check(state == "warn", "a ceiling of %s reports state %r, expected warn", val, state)
            c = _run_session(ctx, grbl, fc, lambda c: c.get("verdict") == "OVERTEMP", "trip leg")
            ev["trip"] = c
            ctx.check(c.get("verdict") == "OVERTEMP", "ceiling %s C with coolant at %.1f C did not trip: %s",
                      bottom + 1, up, c)
            ctx.check(c.get("fire_ok") is False and c.get("hold") is True,
                      "OVERTEMP without fire blocked and a hold: %s", c)
            ctx.check(c.get("gates_off") == [], "a tripped gate is not an off gate: %s", c.get("gates_off"))

            # Leg 2: the ceiling at its top. Off by value: no gate, verdict
            # back to OK at the next run start, and said out loud.
            _set_gates(ctx, fc, {"cool_temp_max": str(top), "cool_temp_resume": orig["cool_temp_resume"]})
            state, val = _gate_state(fc, "cool_temp_max")
            ev["off_state"] = state
            ctx.check(state == "off", "a ceiling of %s reports state %r, expected off", val, state)
            c = _run_session(ctx, grbl, fc, lambda c: c.get("verdict") == "OK" and c.get("gates_off"), "off leg")
            ev["off"] = c
            ctx.check(c.get("verdict") == "OK", "ceiling at %s did not clear the gate: %s", top, c)
            ctx.check(c.get("gates_off") == ["coolant_max"], "/cool/status gates_off %s, expected [coolant_max]",
                      c.get("gates_off"))
            s_off = fc.status().get("gates_off")
            ctx.check(s_off == ["coolant_max"], "/status gates_off %s, expected [coolant_max]", s_off)
            ctx.check(_tail_has(fc, "gate coolant_max OFF: cool_temp_max = %g" % top),
                      "the run-start log line for the off gate is missing from the forgectrl log")

            # Restore, and prove the restore: the next run start reloads.
            _set_gates(ctx, fc, orig)
            restored = True
            state, val = _gate_state(fc, "cool_temp_max")
            c = _run_session(ctx, grbl, fc, lambda c: c.get("verdict") == "OK" and not c.get("gates_off"),
                             "restored")
            ev["restored"] = c
            ctx.check(c.get("verdict") == "OK" and c.get("gates_off") == [],
                      "engine did not return to OK with no gate off after the restore: %s", c)
            ctx.log("restored ceiling %s reports state %s", val, state)
            # Every run session ends with the board temperatures ranged
            # into one line.
            ctx.check(_tail_has(fc, "temps this job: chassis ") and _tail_has(fc, ", soc "),
                      "the run-end board-temperature line (chassis, soc, supply) is missing from the "
                      "forgectrl log")
        finally:
            if not restored:
                # The engine reads settings at run start only: restoring
                # the file is not enough, a hold taken against the test's
                # ceiling would stand until the operator's next job.
                st, body = fc.post("/settings", params=orig)
                ctx.log("restore on failure: POST /settings %s -> %s", orig, st)
                try:
                    c = _run_session(ctx, grbl, fc,
                                     lambda c: c.get("verdict") == "OK" and not c.get("gates_off"),
                                     "restore on failure")
                    ctx.log("restore on failure: engine %s gates_off %s", c.get("verdict"), c.get("gates_off"))
                except Exception as e:      # the original failure is the one to report
                    ctx.log("restore on failure: run session did not complete (%s)", e)
    after = fc.settings()
    ctx.check(all(after.get(k, "") == orig[k] for k in GATE_KEYS),
              "settings not restored: %s", {k: after.get(k) for k in GATE_KEYS})
    ctx.check(fc.wait_idle(60, abort=ctx.aborted), "machine did not return to idle")


CRIT_KEYS = ("cool_temp_max", "cool_temp_resume", "cool_temp_critical_c")


LOW_KEYS = ("cool_temp_min", "cool_temp_start")
WARMUP_ABOVE_C = 1.0        # the start gate this far above the loop: a few heater minutes
WARMUP_RELEASE_S = 720      # the flow heater warms the bulk 0.4 to 0.8 C a minute
HEATER_PWM = "thermal/heater_pwm"


def _hold_session(ctx, g, fc, until, what, wait):
    """M8 opens a run session and leaves it open: wait for `until(cool)`
    and return the sample. The caller ends the session."""
    ctx.check(_cool(fc).get("phase") != "run", "%s: a run session is already open", what)
    g.command("M8")
    t0 = time.time()
    c = {}
    while time.time() - t0 < wait:
        ctx.sleep(1)
        c = _cool(fc)
        if until(c):
            break
    ctx.log("%s: verdict %s phase %s fire_ok %s hold %s (after %.0f s)", what, c.get("verdict"),
            c.get("phase"), c.get("fire_ok"), c.get("hold"), time.time() - t0)
    return c


@test("cooling.floor-and-warm-up", title="The coolant floor blocks fire and the warm-up gate holds a cold start",
      subsystem="cooling", kind="auto", mode="grbl", est_min=9,
      covers=_COOL_COVERS, requires=["kernel.latch-locked-idle", "cooling.gate-off"],
      steps=["Machine idle, coolant at room temperature (10 to 30 C). The test writes the coolant "
             "floor and the warm-up gate and restores them; four M8/M9 sessions, the first with "
             "the loop heater on for a few minutes."],
      description="Both low-side gates, proven at room temperature by moving them above the "
                  "loop. Warm-up: the start gate set 1 C above the coolant makes the next run "
                  "session hold (WARMUP, hold, fire blocked, phase warm-up) with the loop heater "
                  "on and the fans idle; the coolant climbs to the gate and the session releases "
                  "into run with the verdict OK, the heater off and the run fans up. Floor: with "
                  "the start gate off, a floor set 2 C above the coolant makes the next session "
                  "hold COLD with fire blocked. Both at 0 are off, reported in gates_off with the "
                  "run-start log line, and the session reads OK. Restored, everything reads as "
                  "before.")
def floor_and_warm_up(ctx):
    fc = ctx.forgectrl
    ev = ctx.evidence
    before = fc.settings()
    orig = {k: before.get(k, "") for k in LOW_KEYS}
    ev["orig"] = orig
    ctx.log("original: %s", orig)
    up = (fc.status().get("coolant") or {}).get("up_c")
    ctx.check(up is not None and 10.0 <= up <= 30.0, "coolant outside the test's range (up_c %s)", up)
    c0 = _cool(fc)
    ctx.check(c0.get("verdict") == "OK" and c0.get("gates_off") == [],
              "engine is not at OK with every gate on before the test: %s", c0)
    gates = before.get("gates") or {}
    g_min, g_start = gates.get("cool_temp_min") or {}, gates.get("cool_temp_start") or {}
    ctx.check(g_min.get("gate") == "coolant_min" and g_min.get("off") == "low",
              "settings reply does not describe cool_temp_min as the coolant_min gate, off at zero: %s", g_min)
    ctx.check(g_start.get("gate") == "warm_up" and g_start.get("off") == "low",
              "settings reply does not describe cool_temp_start as the warm_up gate, off at zero: %s", g_start)

    restored = False
    with ctx.grbl() as grbl:
        ctx.check(grbl.status_report()["state"].startswith("Idle"), "controller is not idle")
        try:
            # Leg 1: the warm-up hold and its release. The gate sits just
            # above the loop, so the heater reaches it in a few minutes.
            gate = round(up + WARMUP_ABOVE_C, 1)
            _set_gates(ctx, fc, {"cool_temp_start": str(gate), "cool_temp_min": orig["cool_temp_min"]})
            c = _hold_session(ctx, grbl, fc, lambda c: c.get("verdict") == "WARMUP", "warm-up hold", VERDICT_WAIT_S)
            ev["warmup_hold"] = c
            heater = hw.sysfs_int(HEATER_PWM)
            duty = _duties()
            ev["warmup_heater_pwm"] = heater
            ev["warmup_duty"] = duty
            ctx.check(c.get("verdict") == "WARMUP", "start gate %s C over coolant %.1f C did not hold the session: %s",
                      gate, up, c)
            ctx.check(c.get("fire_ok") is False and c.get("hold") is True, "WARMUP without fire blocked and a hold: %s", c)
            ctx.check(c.get("phase") == "warm-up", "phase %r during the warm-up hold, expected warm-up", c.get("phase"))
            ctx.check(heater is not None and heater > 0, "the loop heater is not on during the warm-up (%s=%s)", HEATER_PWM, heater)
            ctx.check(duty == IDLE_DUTY, "the fans are not idle during the warm-up: %s", duty)
            t0 = time.time()
            c = {}
            while time.time() - t0 < WARMUP_RELEASE_S:
                ctx.checkpoint()
                ctx.sleep(5)
                c = _cool(fc)
                if c.get("verdict") != "WARMUP" or c.get("phase") != "warm-up":
                    break
            ev["warmup_release_s"] = round(time.time() - t0)
            ev["warmup_release"] = c
            ctx.log("warm-up release after %s s: verdict %s phase %s up %s (gate %s)", ev["warmup_release_s"],
                    c.get("verdict"), c.get("phase"), c.get("up_c"), gate)
            ctx.check(c.get("verdict") == "OK" and c.get("phase") == "run",
                      "the warm-up did not release into a run session within %d s: %s", WARMUP_RELEASE_S, c)
            ctx.check(ev["warmup_release_s"] >= 60,
                      "the warm-up released in %s s: an instant reading, not the bulk (the release "
                      "judges a one-minute rolling minimum)", ev["warmup_release_s"])
            ctx.check(c.get("fire_ok") is True and c.get("hold") is False, "released warm-up without fire_ok and no hold: %s", c)
            ctx.sleep(2)
            heater = hw.sysfs_int(HEATER_PWM)
            duty = _duties()
            ev["run_heater_pwm"] = heater
            ev["run_duty"] = duty
            ctx.check(heater == 0, "the loop heater stayed on after the release (%s=%s)", HEATER_PWM, heater)
            ctx.check(duty != IDLE_DUTY, "the run fans did not come up at the release: %s", duty)
            ctx.check(_tail_has(fc, "warm-up complete: coolant "), "the warm-up release line is missing from the forgectrl log")
            grbl.command("M9")
            _session_ended(ctx, fc, "warm-up")

            # Leg 2: the floor. The start gate off, the floor above the
            # (now warmer) loop.
            up2 = (fc.status().get("coolant") or {}).get("up_c") or up
            floor = round(min(up2 + 2.0, 32.0), 1)
            ctx.check(up2 < floor, "coolant %.1f C leaves no room under the ceiling for a floor above it", up2)
            _set_gates(ctx, fc, {"cool_temp_start": "0", "cool_temp_min": str(floor)})
            c = _run_session(ctx, grbl, fc, lambda c: c.get("verdict") == "COLD", "floor")
            ev["floor"] = c
            ctx.check(c.get("verdict") == "COLD", "floor %s C over coolant %.1f C did not trip: %s", floor, up2, c)
            ctx.check(c.get("fire_ok") is False and c.get("hold") is True, "COLD without fire blocked and a hold: %s", c)
            ctx.check(c.get("gates_off") == ["warm_up"], "gates_off %s with the start gate at 0, expected [warm_up]",
                      c.get("gates_off"))

            # Leg 3: both at zero: off, said so, and the session reads OK.
            _set_gates(ctx, fc, {"cool_temp_start": "0", "cool_temp_min": "0"})
            c = _run_session(ctx, grbl, fc, lambda c: c.get("verdict") == "OK" and len(c.get("gates_off") or []) == 2,
                             "both off")
            ev["both_off"] = c
            ctx.check(c.get("verdict") == "OK", "gates at 0 did not clear the hold: %s", c)
            ctx.check(sorted(c.get("gates_off") or []) == ["coolant_min", "warm_up"],
                      "gates_off %s, expected [coolant_min, warm_up]", c.get("gates_off"))
            ctx.check(_tail_has(fc, "gate coolant_min OFF: cool_temp_min = 0"),
                      "the run-start log line for the floor off is missing from the forgectrl log")

            # Restore, and prove the restore at the next run start.
            _set_gates(ctx, fc, orig)
            restored = True
            c = _run_session(ctx, grbl, fc, lambda c: c.get("verdict") == "OK" and not c.get("gates_off"), "restored")
            ev["restored"] = c
            ctx.check(c.get("verdict") == "OK" and c.get("gates_off") == [],
                      "engine did not return to OK with no gate off after the restore: %s", c)
        finally:
            if not restored:
                st, body = fc.post("/settings", params=orig)
                ctx.log("restore on failure: POST /settings %s -> %s", orig, st)
                try:
                    grbl.command("M9")
                    _session_ended(ctx, fc, "restore on failure")
                    c = _run_session(ctx, grbl, fc,
                                     lambda c: c.get("verdict") == "OK" and not c.get("gates_off"),
                                     "restore on failure")
                    ctx.log("restore on failure: engine %s gates_off %s", c.get("verdict"), c.get("gates_off"))
                except Exception as e:      # the original failure is the one to report
                    ctx.log("restore on failure: run session did not complete (%s)", e)
    after = fc.settings()
    ctx.check(all(after.get(k, "") == orig[k] for k in LOW_KEYS),
              "settings not restored: %s", {k: after.get(k) for k in LOW_KEYS})
    ctx.check(hw.sysfs_int(HEATER_PWM) == 0, "the loop heater is on after the test")
    ctx.check(fc.wait_idle(60, abort=ctx.aborted), "machine did not return to idle")


TEC_KEYS = ("cool_tec_present", "cool_tec_on_c", "cool_tec_off_c")
TEC_ATTR = "thermal/tec_on"
TEC_WAIT_S = 20             # settings re-read at run start, 1 Hz ticks


@test("cooling.tec-drive", title="The TEC runs on its hysteresis only with airflow and only when declared fitted",
      subsystem="cooling", kind="auto", mode="grbl", est_min=5,
      covers=_COOL_COVERS, requires=["kernel.latch-locked-idle"],
      steps=["Machine idle, coolant at room temperature (10 to 30 C). The test declares the TEC "
             "fitted, moves its thresholds under the loop, runs two short M8/M9 sessions and "
             "restores everything. On a machine without the part the line toggles into nothing."],
      description="TEC policy at room temperature, thresholds moved under the loop so the "
                  "hysteresis wants the cooler on. Declared fitted, a run session drives "
                  "thermal/tec_on to 1 (airflow up) and the session's end returns it to 0 (no "
                  "airflow at idle). Declared not fitted, the same session leaves the line at 0. "
                  ""
                  "The cross-check keeps off under on. The line has no readback, so the last written value is "
                  "what the test reads; the drive rule is the engine's, never the cloud's.")
def tec_drive(ctx):
    fc = ctx.forgectrl
    ev = ctx.evidence
    before = fc.settings()
    orig = {k: before.get(k, "") for k in TEC_KEYS}
    ev["orig"] = orig
    ctx.log("original: %s", orig)
    up = (fc.status().get("coolant") or {}).get("up_c")
    ctx.check(up is not None and 10.0 <= up <= 30.0, "coolant outside the test's range (up_c %s)", up)
    ctx.check(hw.sysfs_int(TEC_ATTR) == 0, "the TEC line is not 0 at idle before the test")
    on_c, off_c = round(up - 2.0, 1), round(up - 4.0, 1)

    # The cross-check, before anything runs: off must sit under on. The
    # floor relation is the engine's runtime clamp, not a settings rule.
    st, body = fc.post("/settings", params={"cool_tec_on_c": "18", "cool_tec_off_c": "19"})
    ctx.check(st == 400, "an off threshold over the on threshold was accepted (%s %s)", st, body)

    restored = False
    with ctx.grbl() as grbl:
        ctx.check(grbl.status_report()["state"].startswith("Idle"), "controller is not idle")
        try:
            # Leg 1: fitted, thresholds under the loop: on with the session's
            # airflow, off again at idle.
            _set_gates(ctx, fc, {"cool_tec_present": "1", "cool_tec_on_c": str(on_c),
                                 "cool_tec_off_c": str(off_c)})
            ctx.check(_cool(fc).get("phase") != "run", "a run session is already open")
            grbl.command("M8")
            t0 = time.time()
            tec = 0
            while time.time() - t0 < TEC_WAIT_S:
                ctx.sleep(1)
                tec = hw.sysfs_int(TEC_ATTR)
                if tec:
                    break
            ev["tec_on_after_s"] = round(time.time() - t0)
            ctx.log("fitted, in session: %s=%s after %s s", TEC_ATTR, tec, ev["tec_on_after_s"])
            ctx.check(tec == 1, "the TEC did not come on in a run session with its threshold under the loop")
            ctx.check(_tail_has(fc, "TEC on: coolant "), "the TEC-on line is missing from the forgectrl log")
            grbl.command("M9")
            _session_ended(ctx, fc, "tec leg 1")
            t0 = time.time()
            while time.time() - t0 < 30:
                ctx.sleep(1)
                if _cool(fc).get("phase") == "idle" and hw.sysfs_int(TEC_ATTR) == 0:
                    break
            tec = hw.sysfs_int(TEC_ATTR)
            ev["tec_after_session"] = tec
            ctx.log("after the session: phase %s %s=%s", _cool(fc).get("phase"), TEC_ATTR, tec)
            ctx.check(tec == 0, "the TEC stayed on with no airflow (idle after the session)")

            # Leg 2: not fitted: the same session leaves the line alone.
            _set_gates(ctx, fc, {"cool_tec_present": "0", "cool_tec_on_c": str(on_c),
                                 "cool_tec_off_c": str(off_c)})
            c = _run_session(ctx, grbl, fc, lambda c: c.get("phase") == "run", "tec leg 2")
            tec = hw.sysfs_int(TEC_ATTR)
            ev["tec_not_fitted"] = tec
            ctx.check(tec == 0, "the TEC was driven while declared not fitted (%s=%s)", TEC_ATTR, tec)

            # Restore, and prove the next session reads it back.
            _set_gates(ctx, fc, orig)
            restored = True
            c = _run_session(ctx, grbl, fc, lambda c: c.get("verdict") == "OK", "restored")
            ev["restored"] = c
        finally:
            if not restored:
                st, body = fc.post("/settings", params=orig)
                ctx.log("restore on failure: POST /settings %s -> %s", orig, st)
                try:
                    grbl.command("M9")
                    _session_ended(ctx, fc, "restore on failure")
                except Exception as e:
                    ctx.log("restore on failure: session end did not complete (%s)", e)
    after = fc.settings()
    ctx.check(all(after.get(k, "") == orig[k] for k in TEC_KEYS),
              "settings not restored: %s", {k: after.get(k) for k in TEC_KEYS})
    ctx.check(hw.sysfs_int(TEC_ATTR) == 0, "the TEC line is not 0 after the test")
    ctx.check(fc.wait_idle(60, abort=ctx.aborted), "machine did not return to idle")


FIRE_KEYS = ("cool_fire_q1_alert", "cool_fire_q1_critical", "cool_fire_q2_alert", "cool_fire_q2_critical")


def _tail_wait(ctx, fc, needle, wait=8):
    """A log line lands through rsyslog a beat after the engine acts;
    poll the tail instead of reading it once."""
    t0 = time.time()
    while time.time() - t0 < wait:
        if _tail_has(fc, needle):
            return True
        ctx.sleep(1)
    return False

FIRE_WAIT_S = 25            # settings re-read at run start, two-tick breach, 1 Hz


@test("cooling.fire-watch-tiers", title="The lid-IR fire watch pauses at its alert and fails at its critical",
      subsystem="cooling", kind="auto", mode="grbl", est_min=6,
      covers=_COOL_COVERS, requires=["kernel.latch-locked-idle"],
      steps=["Machine idle, lid closed, lid lamp at its resting level. The test moves the flame "
             "thresholds under the lamp's own reading and restores them; three M8/M9 sessions, "
             "no fire."],
      description="The factory-shaped fire watch, proven with the lid lamp as the flame "
                  "stand-in: the four lid-IR channels read the resting lamp at a known level, "
                  "so a q1 alert moved under that level must hold the session (FLAME, hold, "
                  "fire blocked, fire_watch alert) and release once the threshold is restored "
                  "mid-session; a q1 critical moved under it must stop motion, lock the laser "
                  "latch and latch FIRE for the rest of the session; all four thresholds at 0 "
                  "read as the four flame gates off and the watch reads watch. Restored, the "
                  "watch reads armed at OK.")
def fire_watch_tiers(ctx):
    fc = ctx.forgectrl
    ev = ctx.evidence
    before = fc.settings()
    orig = {k: before.get(k, "") for k in FIRE_KEYS}
    ev["orig"] = orig
    ctx.log("original: %s", orig)
    st0 = fc.status()
    irs = st0.get("lid_ir")
    ctx.check(isinstance(irs, list) and len(irs) == 4 and all(isinstance(v, int) for v in irs),
              "/status lid_ir is not four readings: %s", irs)
    q1 = sorted(irs)[0]
    ev["lid_ir_idle"] = irs
    ctx.check(20 <= q1 <= 250, "the resting lamp reads %s on the lowest channel; the test needs 20 to 250", q1)
    c0 = _cool(fc)
    ctx.check(c0.get("verdict") == "OK" and c0.get("fire_watch") == "armed",
              "engine not at OK with the watch armed before the test: %s", c0)

    restored = False
    with ctx.grbl() as grbl:
        ctx.check(grbl.status_report()["state"].startswith("Idle"), "controller is not idle")
        try:
            # Leg 1: the pause tier. The q1 alert under the lamp reading
            # holds the session; restoring it mid-session releases the hold.
            _set_gates(ctx, fc, {"cool_fire_q1_alert": str(max(1, q1 - 10))})
            grbl.command("M8")
            t0 = time.time()
            c = {}
            while time.time() - t0 < FIRE_WAIT_S:
                ctx.sleep(1)
                c = _cool(fc)
                if c.get("verdict") == "FLAME":
                    break
            ev["alert"] = c
            ctx.log("alert leg: verdict %s fire_watch %s hold %s fire_ok %s", c.get("verdict"),
                    c.get("fire_watch"), c.get("hold"), c.get("fire_ok"))
            ctx.check(c.get("verdict") == "FLAME", "q1 alert under the lamp did not hold the session: %s", c)
            ctx.check(c.get("hold") is True and c.get("fire_ok") is False, "FLAME without a hold and fire blocked: %s", c)
            ctx.check(c.get("fire_watch") == "alert", "fire_watch %r during the alert, expected alert", c.get("fire_watch"))
            ctx.check(_tail_wait(ctx, fc, "lid IR flame alert (quartiles "),
                      "the flame-alert line is missing from the forgectrl log")
            # The release: the settings re-read is at run start, so the
            # release lever mid-session is the env-free path - restore the
            # threshold and end the session; the alert must not survive
            # into the next one.
            _set_gates(ctx, fc, {"cool_fire_q1_alert": orig["cool_fire_q1_alert"]})
            grbl.command("M9")
            _session_ended(ctx, fc, "alert leg")
            c = _run_session(ctx, grbl, fc, lambda c: c.get("verdict") == "OK", "post-alert")
            ctx.check(c.get("verdict") == "OK", "the alert survived into a fresh session: %s", c)

            # Leg 2: the fail tier. The q1 critical under the lamp reading
            # stops the session and latches FIRE until it ends.
            _set_gates(ctx, fc, {"cool_fire_q1_critical": str(max(2, q1 - 5)),
                                 "cool_fire_q1_alert": str(max(1, q1 - 10))})
            grbl.command("M8")
            t0 = time.time()
            c = {}
            while time.time() - t0 < FIRE_WAIT_S:
                ctx.sleep(1)
                c = _cool(fc)
                if c.get("verdict") == "FIRE":
                    break
            ev["critical"] = c
            ctx.log("critical leg: verdict %s fire_watch %s", c.get("verdict"), c.get("fire_watch"))
            ctx.check(c.get("verdict") == "FIRE", "q1 critical under the lamp did not latch FIRE: %s", c)
            ctx.check(c.get("fire_watch") == "ALARM", "fire_watch %r during FIRE, expected ALARM", c.get("fire_watch"))
            ilk = hw.sysfs_int("cnc/interlock_circuit")
            ev["interlock_circuit"] = ilk
            ctx.check(ilk is not None and (ilk & 8), "the laser latch is not locked under FIRE (interlock_circuit=%s)", ilk)
            ctx.check(_tail_wait(ctx, fc, "LID IR FIRE SIGNAL (quartiles "),
                      "the FIRE line is missing from the forgectrl log")
            _set_gates(ctx, fc, {"cool_fire_q1_critical": orig["cool_fire_q1_critical"],
                                 "cool_fire_q1_alert": orig["cool_fire_q1_alert"]})
            grbl.command("M9")
            _session_ended(ctx, fc, "critical leg")

            # Leg 3: all four at zero: the watch off, said so, session OK.
            _set_gates(ctx, fc, {k: "0" for k in FIRE_KEYS})
            c = _run_session(ctx, grbl, fc, lambda c: c.get("verdict") == "OK" and len(c.get("gates_off") or []) == 4,
                             "watch off")
            ev["off"] = c
            ctx.check(c.get("verdict") == "OK", "the watch at zero did not read OK: %s", c)
            ctx.check(sorted(c.get("gates_off") or []) == ["flame_q1_alert", "flame_q1_critical",
                                                           "flame_q2_alert", "flame_q2_critical"],
                      "gates_off %s, expected the four flame gates", c.get("gates_off"))
            ctx.check(c.get("fire_watch") == "watch", "fire_watch %r with every tier at 0, expected watch",
                      c.get("fire_watch"))

            # Restore, and prove the watch is armed again.
            _set_gates(ctx, fc, orig)
            restored = True
            c = _run_session(ctx, grbl, fc,
                             lambda c: c.get("verdict") == "OK" and not c.get("gates_off"), "restored")
            ev["restored"] = c
            ctx.check(c.get("verdict") == "OK" and c.get("fire_watch") == "armed",
                      "engine did not return to OK with the watch armed: %s", c)
        finally:
            if not restored:
                st, body = fc.post("/settings", params=orig)
                ctx.log("restore on failure: POST /settings %s -> %s", orig, st)
                try:
                    grbl.command("M9")
                    _session_ended(ctx, fc, "restore on failure")
                    c = _run_session(ctx, grbl, fc,
                                     lambda c: c.get("verdict") == "OK" and not c.get("gates_off"),
                                     "restore on failure")
                    ctx.log("restore on failure: engine %s gates_off %s", c.get("verdict"), c.get("gates_off"))
                except Exception as e:
                    ctx.log("restore on failure: run session did not complete (%s)", e)
    after = fc.settings()
    ctx.check(all(after.get(k, "") == orig[k] for k in FIRE_KEYS),
              "settings not restored: %s", {k: after.get(k) for k in FIRE_KEYS})
    ctx.check(fc.wait_idle(60, abort=ctx.aborted), "machine did not return to idle")


CRASH_KEYS = ("cool_accel_x_alert", "cool_accel_y_alert", "cool_accel_abort")
CRASH_GATES = ["crash_abort", "crash_x_alert", "crash_y_alert"]


@test("cooling.crash-watch-plumbing", title="The crash watch stays off the accelerometer unarmed and its gates say off",
      subsystem="cooling", kind="auto", mode="grbl", est_min=3,
      covers=_COOL_COVERS, requires=["kernel.latch-locked-idle"],
      steps=["Machine idle, lid closed. The test writes the three crash thresholds and restores "
             "them; two M8/M9 sessions, no fire, no motion."],
      description="The head-accelerometer crash watch's plumbing, provable without an armed "
                  "window: /cool/status carries accel_watch at watch with the factory-seeded "
                  "thresholds standing; an unarmed run session never arms the watch (the "
                  "coexistence contract: liveness and homing own the accel outside the armed "
                  "window), so accel_watch reads watch through it; the three thresholds at 0 "
                  "read as the three crash gates off by value (gates_off names them) with the "
                  "session OK; a threshold past the register range is refused; restored, the "
                  "gates read ok. The tiers themselves (BUMP, CRASH) trip only on a physical "
                  "knock inside an armed window and are commissioned at the bench, not here.")
def crash_watch_plumbing(ctx):
    fc = ctx.forgectrl
    ev = ctx.evidence
    before = fc.settings()
    orig = {k: before.get(k, "") for k in CRASH_KEYS}
    ev["orig"] = orig
    ctx.log("original: %s", orig)
    gates = (before.get("gates") or {})
    for k in CRASH_KEYS:
        g = gates.get(k) or {}
        ctx.check(g.get("gate", "").startswith("crash_"), "settings gates has no %s row: %s", k, g)
    c0 = _cool(fc)
    ctx.check(c0.get("accel_watch") == "watch",
              "accel_watch %r at idle with thresholds set, expected watch", c0.get("accel_watch"))

    st, body = fc.post("/settings", params={"cool_accel_x_alert": "300"})
    ctx.check(st != 200 or body.get("cool_accel_x_alert") != "300",
              "a threshold past the register range (300) was accepted")

    restored = False
    with ctx.grbl() as grbl:
        ctx.check(grbl.status_report()["state"].startswith("Idle"), "controller is not idle")
        try:
            # Leg 1: an unarmed session never arms the watch.
            c = _run_session(ctx, grbl, fc, lambda c: c.get("phase") == "run", "unarmed session")
            ev["unarmed"] = c
            ctx.check(c.get("accel_watch") == "watch",
                      "accel_watch %r in an unarmed run session, expected watch", c.get("accel_watch"))
            ctx.check(c.get("verdict") == "OK", "unarmed session not OK: %s", c)

            # Leg 2: the three thresholds at zero are the gates off.
            _set_gates(ctx, fc, {k: "0" for k in CRASH_KEYS})
            c = _run_session(ctx, grbl, fc,
                             lambda c: c.get("verdict") == "OK" and
                             set(CRASH_GATES) <= set(c.get("gates_off") or []),
                             "watch off")
            ev["off"] = c
            ctx.check(set(CRASH_GATES) <= set(c.get("gates_off") or []),
                      "gates_off %s does not name the three crash gates", c.get("gates_off"))
            ctx.check(c.get("verdict") == "OK", "the watch at zero did not read OK: %s", c)

            # Restore, and prove the gates read ok again.
            _set_gates(ctx, fc, orig)
            restored = True
            c = _run_session(ctx, grbl, fc,
                             lambda c: not (set(CRASH_GATES) & set(c.get("gates_off") or [])),
                             "restored")
            ev["restored"] = c
            ctx.check(not (set(CRASH_GATES) & set(c.get("gates_off") or [])),
                      "the crash gates stayed off after the restore: %s", c.get("gates_off"))
        finally:
            if not restored:
                st, body = fc.post("/settings", params=orig)
                ctx.log("restore on failure: POST /settings %s -> %s", orig, st)
    after = fc.settings()
    ctx.check(all(after.get(k, "") == orig[k] for k in CRASH_KEYS),
              "settings not restored: %s", {k: after.get(k) for k in CRASH_KEYS})
    ctx.check(fc.wait_idle(60, abort=ctx.aborted), "machine did not return to idle")


@test("cooling.critical-tier", title="The coolant critical line is a fault above the ceiling's pause",
      subsystem="cooling", kind="auto", mode="grbl", est_min=3,
      covers=_COOL_COVERS + [("forgectrl", "src/main.c")], requires=["cooling.gate-off"],
      steps=["Machine idle, coolant at room temperature (above 8 C). The test writes the coolant "
             "ceiling, the resume gate and the critical line and restores them; three short M8/M9 "
             "cycles spin the fans."],
      description="Two tiers on the upstream coolant sensor: the ceiling pauses (OVERTEMP, resume "
                  "below the resume gate), the critical line above it is a fault. With the ceiling "
                  "and the critical line both under the coolant's temperature a run session must "
                  "read CRITICAL rather than OVERTEMP (fire blocked, hold, no resume), the fault "
                  "must end with the session, and the settings API must refuse a critical line at "
                  "or below the ceiling; with the critical line at its top the gate is off by "
                  "value (gates_off names it) and the ceiling alone pauses; restored, the next "
                  "session runs OK with nothing off.")
def critical_tier(ctx):
    fc = ctx.forgectrl
    ev = ctx.evidence
    before = fc.settings()
    orig = {k: before.get(k, "") for k in CRIT_KEYS}
    ev["orig"] = orig
    ctx.log("original: %s", orig)
    up = (fc.status().get("coolant") or {}).get("up_c")
    ctx.check(up is not None and up > 8.0, "coolant too cold for the trip leg (up_c %s)", up)
    c0 = _cool(fc)
    ctx.check(c0.get("verdict") == "OK" and c0.get("gates_off") == [],
              "engine is not at OK with every gate on before the test: %s", c0)
    gates = before.get("gates") or {}
    ceil = gates.get("cool_temp_max") or {}
    crit = gates.get("cool_temp_critical_c") or {}
    ctx.check(crit.get("gate") == "coolant_critical" and crit.get("off") == "high",
              "settings reply does not describe the critical line as the coolant_critical gate, off at "
              "its top: %s", crit)
    bottom, top = ceil.get("lo"), crit.get("hi")
    ctx.check(isinstance(bottom, (int, float)) and isinstance(top, (int, float)),
              "no ranges in the reply: %s %s", ceil, crit)
    ctx.check(isinstance(crit.get("lo"), (int, float)) and crit.get("lo") > bottom,
              "the critical line's floor %s is not above the ceiling's %s", crit.get("lo"), bottom)

    # The cross-check: a critical line at or below the ceiling is refused
    # before anything is written.
    st, body = fc.post("/settings", params={"cool_temp_max": "33", "cool_temp_critical_c": "33"})
    ev["cross_check"] = {"status": st, "body": body}
    if st == 200:
        fc.post("/settings", params=orig)       # undo before failing
    ctx.check(st == 400, "a critical line equal to the ceiling was accepted: %s %s", st, body)
    after = fc.settings()
    ctx.check(all(after.get(k, "") == orig[k] for k in CRIT_KEYS),
              "the refused POST changed a setting: %s", {k: after.get(k) for k in CRIT_KEYS})
    # A ceiling at its off end is no ceiling: the default line stands
    # alone, and the POST that turns the ceiling off is accepted.
    st, body = fc.post("/settings", params={"cool_temp_max": str(ceil.get("hi"))})
    ev["ceiling_off_accepted"] = st
    if st == 200:
        fc.post("/settings", params=orig)
    ctx.check(st == 200, "the ceiling at its off end %s was refused against the critical line: %s %s",
              ceil.get("hi"), st, body)

    restored = False
    with ctx.grbl() as grbl:
        st = grbl.status_report()
        ctx.check(st["state"].startswith("Idle"), "controller is %s", st["state"])
        try:
            # Leg 1: ceiling and critical line both under the coolant's
            # temperature. The fail tier wins: CRITICAL, not OVERTEMP.
            low = {"cool_temp_max": str(bottom + 1), "cool_temp_resume": str(bottom),
                   "cool_temp_critical_c": str(bottom + 2)}
            _set_gates(ctx, fc, low)
            c = _run_session(ctx, grbl, fc, lambda c: c.get("verdict") == "CRITICAL", "critical leg")
            ev["critical"] = c
            ctx.check(c.get("verdict") == "CRITICAL",
                      "critical line %s C with coolant at %.1f C did not fault (verdict %s): %s",
                      bottom + 2, up, c.get("verdict"), c)
            ctx.check(c.get("fire_ok") is False and c.get("hold") is True,
                      "CRITICAL without fire blocked and a hold: %s", c)
            ctx.check(c.get("resume_ok") is not True, "a coolant fault offered a resume: %s", c)
            ctx.check("CRITICAL" in (c.get("reason") or "") and "coolant" in (c.get("reason") or ""),
                      "the reason does not name the tier and the coolant: %r", c.get("reason"))
            ctx.check(c.get("gates_off") == [], "a tripped gate is not an off gate: %s", c.get("gates_off"))
            # The fault ends with the session; the ceiling, still under the
            # coolant, keeps its pause (OVERTEMP), never CRITICAL.
            c = _after_session(ctx, fc, until=lambda c: c.get("verdict") != "CRITICAL")
            ev["critical_after"] = c
            ctx.check(c.get("verdict") == "OVERTEMP",
                      "after the faulted session the engine reads %s, expected the ceiling's OVERTEMP: %s",
                      c.get("verdict"), c)
            ctx.check("CRITICAL" not in (c.get("reason") or ""),
                      "the reason still names the ended critical fault under the ceiling's hold: %r",
                      c.get("reason"))

            # Leg 2: the critical line at its top is the gate off; the
            # ceiling alone pauses, and gates_off says so.
            _set_gates(ctx, fc, {"cool_temp_critical_c": str(top)})
            c = _run_session(ctx, grbl, fc,
                             lambda c: c.get("verdict") == "OVERTEMP" and "coolant_critical" in (c.get("gates_off") or []),
                             "critical off leg")
            ev["critical_off"] = c
            ctx.check(c.get("verdict") == "OVERTEMP",
                      "with the critical line off the ceiling did not pause (verdict %s): %s", c.get("verdict"), c)
            ctx.check("coolant_critical" in (c.get("gates_off") or []),
                      "gates_off %s lacks coolant_critical", c.get("gates_off"))
            ctx.check(_tail_has(fc, "gate coolant_critical OFF: cool_temp_critical_c = %g" % top),
                      "the run-start log line for the off gate is missing from the forgectrl log")

            # Restore, and prove it: OK, nothing off.
            _set_gates(ctx, fc, orig)
            restored = True
            c = _run_session(ctx, grbl, fc, lambda c: c.get("verdict") == "OK" and not c.get("gates_off"),
                             "restored")
            ev["restored"] = c
            ctx.check(c.get("verdict") == "OK" and c.get("gates_off") == [],
                      "engine did not return to OK with every gate on after the restore: %s", c)
        finally:
            if not restored:
                st, body = fc.post("/settings", params=orig)
                ctx.log("restore on failure: POST /settings %s -> %s", orig, st)
                try:
                    c = _run_session(ctx, grbl, fc,
                                     lambda c: c.get("verdict") == "OK" and not c.get("gates_off"),
                                     "restore on failure")
                    ctx.log("restore on failure: engine %s gates_off %s", c.get("verdict"), c.get("gates_off"))
                except Exception as e:      # the original failure is the one to report
                    ctx.log("restore on failure: run session did not complete (%s)", e)
    after = fc.settings()
    ctx.check(all(after.get(k, "") == orig[k] for k in CRIT_KEYS),
              "settings not restored: %s", {k: after.get(k) for k in CRIT_KEYS})


FAN_KEYS = ("cool_tach_exhaust_min_rpm", "cool_purge_min_current", "cool_fan_grace_s")
FAN_GRACE_S = "8"           # past the intakes' 7 s to 90 percent, so only the leg's floor trips
FAN_TRIP_WAIT_S = 20        # grace + three ticks, with slack for the 1 Hz pipeline


def _fan_gate(c, name):
    return ((c.get("fan_gates") or {}).get(name) or {})


@test("cooling.fan-gate-trips", title="A fan under its floor past the grace is a fault; a floor of zero is off",
      subsystem="cooling", kind="auto", mode="grbl", est_min=4,
      covers=_COOL_COVERS + [("forgectrl", "src/main.c")], requires=["cooling.gate-off"],
      steps=["Machine idle, fans quiet. The test writes the exhaust floor, the purge current floor and "
             "the spin-up grace and restores them; four short M8/M9 cycles spin the fans."],
      description="The airflow gates judge a fan commanded at the cut fan profile, which a bare M8 "
                  "applies, armed or not. An exhaust floor no fan "
                  "can meet must trip AIRFLOW after the grace plus three ticks (hold, fire blocked, no "
                  "resume while the session lasts) and the fault must end with the session (verdict OK, "
                  "no hold, once the session is over); a purge current floor at the ADC rail must trip "
                  "the same way; a floor of zero must read off in gates_off and trip nothing; restored, "
                  "the next session runs OK with every fan reading at or above its floor.")
def fan_gate_trips(ctx):
    fc = ctx.forgectrl
    ev = ctx.evidence
    before = fc.settings()
    orig = {k: before.get(k, "") for k in FAN_KEYS}
    ev["orig"] = orig
    ctx.log("original: %s", orig)
    gates = before.get("gates") or {}
    exh = gates.get("cool_tach_exhaust_min_rpm") or {}
    prg = gates.get("cool_purge_min_current") or {}
    ctx.check(exh.get("gate") == "exhaust" and exh.get("off") == "low",
              "settings reply does not describe the exhaust floor as the exhaust gate, off at zero: %s", exh)
    ctx.check(prg.get("gate") == "purge" and prg.get("off") == "low",
              "settings reply does not describe the purge floor as the purge gate, off at zero: %s", prg)
    c0 = _cool(fc)
    ctx.check(c0.get("verdict") == "OK" and c0.get("gates_off") == [],
              "engine is not at OK with every gate on before the test: %s", c0)

    restored = False
    with ctx.grbl() as grbl:
        st = grbl.status_report()
        ctx.check(st["state"].startswith("Idle"), "controller is %s", st["state"])
        try:
            # Leg 1: an exhaust floor at the legal maximum. No fan reaches it,
            # so after the grace and three ticks the gate trips a fault.
            _set_gates(ctx, fc, {"cool_tach_exhaust_min_rpm": str(exh.get("hi")), "cool_fan_grace_s": FAN_GRACE_S})
            c = _run_session(ctx, grbl, fc, lambda c: c.get("verdict") == "AIRFLOW", "exhaust trip leg",
                             wait=FAN_TRIP_WAIT_S)
            ev["exhaust_trip"] = {"cool": c, "gate": _fan_gate(c, "exhaust")}
            ctx.check(c.get("verdict") == "AIRFLOW", "an exhaust floor of %s rpm did not trip: %s", exh.get("hi"), c)
            ctx.check(c.get("fire_ok") is False and c.get("hold") is True,
                      "AIRFLOW without fire blocked and a hold: %s", c)
            ctx.check(c.get("resume_ok") is not True, "a fan fault offered a resume: %s", c)
            ctx.check(_fan_gate(c, "exhaust").get("state") == "TRIPPED",
                      "the exhaust gate does not read TRIPPED: %s", c.get("fan_gates"))
            ctx.check("exhaust" in (c.get("reason") or ""), "the reason does not name the fan: %r", c.get("reason"))
            # The fault is the session's: with the session over, the
            # verdict is back to OK and nothing holds (jogs and the next
            # job's pre-check must not see a fan fault at idle).
            c = _after_session(ctx, fc)
            ev["exhaust_trip_after"] = c
            ctx.check(c.get("verdict") == "OK" and c.get("hold") is False,
                      "the fan fault outlived its run session: %s", c)

            # Leg 2: the purge fan by current, floor at the ADC rail.
            _set_gates(ctx, fc, {"cool_tach_exhaust_min_rpm": orig["cool_tach_exhaust_min_rpm"],
                                 "cool_purge_min_current": str(prg.get("hi"))})
            c = _run_session(ctx, grbl, fc, lambda c: c.get("verdict") == "AIRFLOW", "purge trip leg",
                             wait=FAN_TRIP_WAIT_S)
            ev["purge_trip"] = {"cool": c, "gate": _fan_gate(c, "purge")}
            ctx.check(c.get("verdict") == "AIRFLOW" and _fan_gate(c, "purge").get("state") == "TRIPPED",
                      "a purge current floor of %s did not trip: %s", prg.get("hi"), c)

            # Leg 3: the exhaust floor at zero is the gate off: nothing trips,
            # gates_off says so, and the other fans are judged on their own.
            _set_gates(ctx, fc, {"cool_tach_exhaust_min_rpm": "0",
                                 "cool_purge_min_current": orig["cool_purge_min_current"]})
            c = _run_session(ctx, grbl, fc, lambda c: c.get("verdict") == "OK" and "exhaust" in (c.get("gates_off") or [])
                             and _fan_gate(c, "exhaust").get("state") == "off",
                             "exhaust off leg")
            ev["exhaust_off"] = {"cool": c, "gate": _fan_gate(c, "exhaust")}
            ctx.check(c.get("verdict") == "OK", "the exhaust floor at zero did not clear the gate: %s", c)
            ctx.check("exhaust" in (c.get("gates_off") or []), "gates_off %s lacks exhaust", c.get("gates_off"))
            ctx.check(_fan_gate(c, "exhaust").get("state") == "off",
                      "the exhaust gate does not read off: %s", c.get("fan_gates"))

            # Restore, and prove it: every fan at or above its floor, nothing off.
            _set_gates(ctx, fc, orig)
            restored = True
            # The restored grace is the shipped one, so this leg waits it out.
            c = _run_session(ctx, grbl, fc,
                             lambda c: c.get("verdict") == "OK" and not c.get("gates_off")
                             and all(g.get("state") == "ok" for g in (c.get("fan_gates") or {}).values()),
                             "restored", wait=FAN_TRIP_WAIT_S + 20)
            ev["restored"] = c.get("fan_gates")
            ctx.check(c.get("verdict") == "OK" and c.get("gates_off") == [],
                      "engine did not return to OK with every gate on after the restore: %s", c)
            bad = {k: g for k, g in (c.get("fan_gates") or {}).items() if g.get("state") != "ok"}
            ctx.check(not bad, "fans not at or above their floors after the grace: %s", bad)
            ctx.log("fans at run duty: %s", {k: "%s/%s" % (g.get("reading"), g.get("floor"))
                                              for k, g in (c.get("fan_gates") or {}).items()})
        finally:
            if not restored:
                st, body = fc.post("/settings", params=orig)
                ctx.log("restore on failure: POST /settings %s -> %s", orig, st)
                try:
                    c = _run_session(ctx, grbl, fc, lambda c: c.get("verdict") == "OK" and not c.get("gates_off"),
                                     "restore on failure")
                    ctx.log("restore on failure: engine %s gates_off %s", c.get("verdict"), c.get("gates_off"))
                except Exception as e:      # the original failure is the one to report
                    ctx.log("restore on failure: run session did not complete (%s)", e)
    after = fc.settings()
    ctx.check(all(after.get(k, "") == orig[k] for k in FAN_KEYS),
              "settings not restored: %s", {k: after.get(k) for k in FAN_KEYS})
    ctx.check(fc.wait_idle(60, abort=ctx.aborted), "machine did not return to idle")


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
LOAD_LOG_LINES = "300"
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


def _forgectrl_tail(fc):
    st, body = fc.get("/logs/tail", params={"name": "forgectrl", "lines": LOAD_LOG_LINES})
    return body.get("text", "") if st == 200 and isinstance(body, dict) else ""


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
    fc = ctx.forgectrl
    ev = ctx.evidence
    state, check_s = _gate_state(fc, "cool_flow_check_s")
    _st, limit = _gate_state(fc, "cool_flow_rise")
    ctx.check(state != "off" and check_s, "the flow check is off (cool_flow_check_s=%s)", check_s)
    ctx.check(limit, "cool_flow_rise unreadable")
    ev.update({"check_s": check_s, "limit_c": limit})
    before = _forgectrl_tail(fc).count("heater rise")
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
            text = _forgectrl_tail(fc)
            if text.count("heater rise") > before:
                m = None
                for m in _load_verdict_rx().finditer(text):
                    pass
                line = m.group(0) if m else None
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


@test("cooling.fan-duty-readback", title="A fan duty a device lost is read back and put back inside a tick",
      subsystem="cooling", kind="auto", mode="grbl", est_min=2,
      covers=_COOL_COVERS + [("kernel-module-glowforge", "src/head*")],
      requires=["kernel.latch-locked-idle"],
      description="M8 opens a run session and every fan reads its run duty back from its device. "
                  "The head's air-assist register is then written back to the idle duty behind the "
                  "engine's back (what a head reset or a dropped transaction leaves) and the exhaust "
                  "PWM to zero: within a few ticks each reads its run duty again, the log names each "
                  "loss and its put-back, and the session stays OK with no airflow fault (a fan put "
                  "back gets its spin-up grace). Then M9 and the idle duties.")
def fan_duty_readback(ctx):
    fc = ctx.forgectrl
    ev = ctx.evidence
    run = {"head/air_assist_pwm": 1023, "thermal/exhaust_pwm": 65535, "thermal/intake_pwm": 43278}
    with ctx.grbl() as g:
        c = _hold_session(ctx, g, fc, lambda c: c.get("phase") == "run", "readback", 20)
        try:
            ctx.check(c.get("phase") == "run", "no run session opened: %s", c)
            ctx.sleep(2)
            before = {k: hw.sysfs_int(k) for k in run}
            ctx.log("duties at run: %s", before)
            ev["run_duties"] = before
            ctx.check(all(before.get(k) == v for k, v in run.items()),
                      "not every fan reads its run duty: %s", before)
            hw.sysfs_write("head/air_assist_pwm", 204)
            hw.sysfs_write("thermal/exhaust_pwm", 0)
            lost = {k: hw.sysfs_int(k) for k in ("head/air_assist_pwm", "thermal/exhaust_pwm")}
            ctx.log("written behind the engine's back: %s", lost)
            back = {}
            t0 = time.time()
            while time.time() - t0 < 6:
                ctx.sleep(0.5)
                back = {k: hw.sysfs_int(k) for k in ("head/air_assist_pwm", "thermal/exhaust_pwm")}
                if back.get("head/air_assist_pwm") == 1023 and back.get("thermal/exhaust_pwm") == 65535:
                    break
            took = round(time.time() - t0, 1)
            ev["put_back"] = {"duties": back, "after_s": took}
            ctx.log("put back after %s s: %s", took, back)
            ctx.check(back.get("head/air_assist_pwm") == 1023, "the air-assist duty was not put back: %s", back)
            ctx.check(back.get("thermal/exhaust_pwm") == 65535, "the exhaust duty was not put back: %s", back)
            ctx.sleep(2)
            ctx.check(_tail_has(fc, "head/air_assist_pwm read 204 with 1023 commanded: put back"),
                      "the air-assist loss is not named in the log")
            ctx.check(_tail_has(fc, "thermal/exhaust_pwm read 0 with 65535 commanded: put back"),
                      "the exhaust loss is not named in the log")
            for _ in range(8):
                ctx.sleep(1)
                c = _cool(fc)
                if c.get("verdict") != "OK":
                    break
            ev["verdict_after"] = {"verdict": c.get("verdict"), "hold": c.get("hold"), "reason": c.get("reason")}
            ctx.check(c.get("verdict") == "OK" and not c.get("hold"),
                      "the session did not stay OK after the put-back: %s %s", c.get("verdict"), c.get("reason"))
        finally:
            g.command("M9")
            _session_ended(ctx, fc, "readback")
    idle = {k: hw.sysfs_int(k) for k in run}
    ev["idle_duties"] = idle
    ctx.log("duties at idle: %s", idle)
