"""commission.check-* - the setup's checks (the dark wizards), driven the
way the page drives them: POST /wiz/<id>/start, GET /wiz/dark polled,
the prompts answered from here (the bench fixture works the lid and the
button; a confirmation is answered yes once the snapshot exists), the
result judged, and every setting a check wrote put back as found. Each
check re-runs on the real record: a completed check completes again at
the same version, so the record reads as before.
"""
import json
import os
import time
from ..catalog import test
from .. import hw
from ..baseline import read_position
from .commission import wiz, Restore  # noqa: F401 - Restore is re-exported for the sheet

POLL_S = 1.0
DARK_COVERS = [("forgectrl", "src/wizdark.*"), ("forgectrl", "src/wizcalc.*"),
               ("forgectrl", "src/wiz.*"), ("forgectrl", "src/commission.*"),
               ("forgectrl", "src/main.c"), ("forgectrl", "src/ui/wizard.*")]


def dark(fc):
    st, body = fc.get("/wiz/dark")
    if st != 200 or not isinstance(body, dict):
        raise hw.HwError("GET /wiz/dark -> %s" % st)
    return body


def start(ctx, fc, wid):
    ev = ctx.evidence
    st, body = fc.post("/wiz/%s/start" % wid)
    if st == 409 and dark(fc).get("id") == wid and dark(fc).get("running"):
        # The same check left running by an earlier run of this test (an
        # abort mid-wait): this run owns the machine, so it ends that one
        # and starts its own.
        ctx.log("the %s check is still running from an earlier run: aborting it", wid)
        fc.post("/wiz/%s/abort" % wid)
        ctx.wait_for(lambda: not dark(fc).get("running"), 30)
        st, body = fc.post("/wiz/%s/start" % wid)
    ctx.log("POST /wiz/%s/start -> %s %s", wid, st, body if isinstance(body, dict) else "")
    ctx.check(st == 200, "the %s check did not start (%s %s)", wid, st, body)
    ev["started"] = st == 200


def answer(fc, wid, prompt, value):
    return fc.post("/wiz/%s/answer" % wid, data={"seq": str(prompt["seq"]), "value": value})


def run_check(ctx, wid, on_prompt, timeout_s):
    """Start `wid` and follow it to its end. on_prompt(prompt) is called
    once per prompt (by sequence number) and returns the value to answer
    with, or None when it handled the prompt another way (a fixture
    action). Returns the final status document."""
    fc = ctx.forgectrl
    ev = ctx.evidence
    start(ctx, fc, wid)
    t0 = time.time()
    seen = set()
    phases = []
    last = None
    try:
        while time.time() - t0 < timeout_s:
            ctx.checkpoint()
            d = dark(fc)
            if d.get("id") != wid:
                raise hw.HwError("another check took the slot: %s" % d.get("id"))
            if d.get("phase") and (not phases or phases[-1] != d["phase"]):
                phases.append(d["phase"])
                ctx.log("%s: %s", wid, d["phase"])
            p = d.get("prompt")
            if p and p.get("seq") not in seen:
                seen.add(p["seq"])
                ctx.log("prompt %s (%s): %s", p.get("id"), p.get("kind"), p.get("text"))
                value = on_prompt(p)
                if value is not None:
                    st, body = answer(fc, wid, p, value)
                    ctx.check(st == 200, "the answer to %s -> %s %s", p.get("id"), st, body)
            if not d.get("running"):
                last = d
                break
            time.sleep(POLL_S)
    except BaseException:
        # An abort or a failure on the way out leaves no check running
        # behind this test: the next run must be able to start its own.
        fc.post("/wiz/%s/abort" % wid)
        raise
    ev["elapsed_s"] = round(time.time() - t0, 1)
    ev["phases"] = phases[-12:]
    ctx.check(last is not None, "the %s check did not end within %d s", wid, timeout_s)
    if last is None:
        fc.post("/wiz/%s/abort" % wid)
        return {}
    ev["log"] = last.get("log", [])[-12:]
    ev["error"] = last.get("error")
    ev["result"] = last.get("result")
    ctx.check(not last.get("error"), "the %s check failed: %s", wid, last.get("error"))
    ctx.check(isinstance(last.get("result"), dict), "the %s check ended without a result", wid)
    w = wiz(fc)
    ctx.check((w.get("versions") or {}).get(wid) == 1, "the record does not carry %s at version 1", wid)
    return last


@test("commission.check-switches", title="The switches check follows the lid and the button",
      subsystem="commission", kind="operator", est_min=3,
      covers=DARK_COVERS + [("forgectrl", "src/status.c")],
      requires=["forgectrl.auth"], actions=["lid", "button"],
      description="POST /wiz/switches/start; the check asks for the lid to open and close and for "
                  "a press, each as a 'wait' prompt that the switch edge itself answers; the bench "
                  "fixture works the lid and the button. On a Basic or Plus the interlock loop reads "
                  "satisfied without a prompt. The result says lid, button, head present, and "
                  "whether the HV enable readback followed the lid; the record carries switches at "
                  "version 1.")
def check_switches(ctx):
    def on_prompt(p):
        pid = p.get("id", "")
        if pid in ("lid-open", "lid-close-first", "lid-close"):
            ctx.act("lid", "open" if pid == "lid-open" else "close",
                    until=lambda: (dark(ctx.forgectrl).get("prompt") or {}).get("seq") != p["seq"],
                    text=p.get("text", ""))
        elif pid == "button-press":
            ctx.act("button", "press",
                    until=lambda: (dark(ctx.forgectrl).get("prompt") or {}).get("id") != "button-press",
                    text=p.get("text", ""))
        elif pid == "button-release":
            pass                                    # the fixture's press releases on its own
        elif pid.startswith("interlock-"):
            ctx.act("interlock", "open" if pid == "interlock-open" else "close",
                    until=lambda: (dark(ctx.forgectrl).get("prompt") or {}).get("seq") != p["seq"],
                    text=p.get("text", ""))
        return None
    last = run_check(ctx, "switches", on_prompt, 600)
    r = last.get("result") or {}
    ctx.check(r.get("lid") is True and r.get("button") is True, "lid or button not proven: %s", r)
    ctx.check(r.get("head_present") is True, "the head did not read present: %s", r)
    ctx.log("switches: %s", json.dumps(r))


@test("commission.check-sensors", title="The sensors check reads a plausible machine at rest",
      subsystem="commission", kind="auto", est_min=2,
      covers=DARK_COVERS + [("forgectrl", "src/status.c"), ("forgectrl", "src/accel.c"),
                            ("forgectrl", "src/cool.c")],
      requires=["forgectrl.auth"],
      description="POST /wiz/sensors/start; ten seconds of readings, then the room-temperature "
                  "prompt, answered Skip so no offset is written. The result carries both coolant "
                  "temperatures, the chassis and SoC, the lid IR maxima, the accelerometer event "
                  "count, the supply power-good, the HV current, and the idle fan speeds; the "
                  "record carries sensors at version 1 and cool_temp_offset_c reads as before.")
def check_sensors(ctx):
    with Restore(ctx, ["cool_temp_offset_c"]):
        last = run_check(ctx, "sensors", lambda p: "Skip" if p.get("id") == "room-temp" else None, 120)
        r = last.get("result") or {}
        for k in ("coolant_down_c", "coolant_up_c", "lid_ir_max", "laser_pgood", "hv_current_max",
                  "exhaust_rpm_idle", "intake_rpm_idle"):
            ctx.check(k in r, "the result lacks %s: %s", k, r)
        ctx.check(r.get("laser_pgood") == 1, "power-good read %s", r.get("laser_pgood"))
        ctx.log("sensors: coolant %s/%s C, IR %s, HV %s", r.get("coolant_down_c"), r.get("coolant_up_c"),
                r.get("lid_ir_max"), r.get("hv_current_max"))


@test("commission.check-airflow", title="The airflow check measures the fans and sets the floors",
      subsystem="commission", kind="auto", hardware="takeover", est_min=3,
      covers=DARK_COVERS + [("forgectrl", "src/cool.c"), ("forgectrl", "src/airflow.*"),
                            ("forgectrl", "src/gates.c"), ("forgectrl", "src/super.c")],
      requires=["forgectrl.auth", "cooling.fan-gate-trips"],
      description="POST /wiz/airflow/start: the controller stops, the fans run at the cut profile "
                  "for 35 s, the purge is measured on and off, and five settings are written: the "
                  "four floors at 55 percent of steady and the grace from the slowest spin-up. Each "
                  "fan must read at least 1000 rpm. The five settings are put back as found at the "
                  "end, the purge fan is on again and drawing over the floor the check just wrote, "
                  "and the controller must be running again.")
def check_airflow(ctx):
    keys = ["cool_tach_exhaust_min_rpm", "cool_tach_intake_min_rpm", "cool_tach_air_assist_min_rpm",
            "cool_purge_min_current", "cool_fan_grace_s"]
    with Restore(ctx, keys):
        last = run_check(ctx, "airflow", lambda p: None, 240)
        r = last.get("result") or {}
        fans = r.get("fans") or {}
        for name in ("exhaust", "intake 1", "intake 2", "air assist"):
            f = fans.get(name) or {}
            ctx.check(f.get("ok") is True, "%s: %s", name, f)
            ctx.log("%s: %s rpm steady, 90 percent at %s s", name, f.get("steady_rpm"), f.get("spinup_s"))
        floors = r.get("floors") or {}
        for k in keys:
            ctx.check(k in floors, "no floor for %s: %s", k, floors)
        s = ctx.forgectrl.settings() or {}
        for k in keys:
            ctx.check(s.get(k) == floors.get(k), "%s reads %r, the check wrote %r", k, s.get(k), floors.get(k))
        ctx.log("floors: %s", json.dumps(floors))
        # The check switches purge air off to read its off current. Leaving
        # it off costs the next job an airflow hold mid-cut, judged against
        # the very floor written above, and nothing puts it back until the
        # daemon restarts: the engine's idle phase never re-applies its own
        # duties. Prove the machine is whole, not just measured.
        purge_on = ctx.sysfs("head/purge_air")
        ctx.check(purge_on == "1", "purge air reads %r after the check, expected 1 (on)", purge_on)
        gate = ((ctx.forgectrl.get("/cool/status")[1] or {}).get("fan_gates") or {}).get("purge") or {}
        ctx.evidence["purge_after"] = {"purge_air": purge_on, "gate": gate}
        ctx.log("purge after the check: commanded %s, drawing %s against the %s floor",
                purge_on, gate.get("reading"), gate.get("floor"))
        ctx.check((gate.get("reading") or 0) >= (gate.get("floor") or 0),
                  "purge draws %s, under the %s floor the check just wrote: the next job would be held",
                  gate.get("reading"), gate.get("floor"))
    ok = ctx.wait_for(lambda: (ctx.forgectrl.get("/mode")[1] or {}).get("controller") == "running", 60)
    ctx.check(ok is not None, "the controller did not come back after the check")


@test("commission.check-cameras", title="The cameras check captures both cameras",
      subsystem="commission", kind="operator", est_min=2,
      covers=DARK_COVERS + [("forgectrl", "src/cam.c")],
      requires=["forgectrl.auth", "camera.snapshot"], actions=["lid"],
      description="POST /wiz/cameras/start with the lid closed: a lid snapshot, the question, a "
                  "head snapshot, the question. Each question is answered yes once GET /wiz/shot "
                  "serves the snapshot as image/jpeg. The result names the sensor and says both "
                  "views were accepted.")
def check_cameras(ctx):
    fc = ctx.forgectrl
    ev = ctx.evidence
    ctx.act("lid", "close")
    shots = {}

    def on_prompt(p):
        pid = p.get("id", "")
        if pid in ("lid-view", "head-view"):
            cam = "lid" if pid == "lid-view" else "head"
            st, body = fc.get("/wiz/shot", params={"cam": cam}, raw=True)
            shots[cam] = {"status": st, "bytes": len(body) if body else 0,
                          "jpeg": bool(body) and body[:2] == b"\xff\xd8"}
            ctx.check(st == 200 and shots[cam]["jpeg"], "no %s snapshot to judge: %s", cam, shots[cam])
            return "Yes"
        if pid == "lid-close":
            ctx.act("lid", "close", until=lambda: (dark(fc).get("prompt") or {}).get("seq") != p["seq"])
        return None
    last = run_check(ctx, "cameras", on_prompt, 180)
    ev["shots"] = shots
    r = last.get("result") or {}
    ctx.check(r.get("lid_ok") is True and r.get("head_ok") is True, "a view was not accepted: %s", r)
    ctx.check(bool(r.get("sensor")) and r.get("sensor") != "unknown", "no sensor named: %s", r)
    ctx.log("cameras: %s, lid %d bytes, head %d bytes", r.get("sensor"),
            shots.get("lid", {}).get("bytes", 0), shots.get("head", {}).get("bytes", 0))


@test("commission.check-motion", title="The motion check proves the rail, the lens reference, and the jogs",
      subsystem="commission", kind="auto", hardware="takeover", est_min=5,
      covers=DARK_COVERS + [("forgectrl", "src/super.c"), ("forgectrl", "src/liveness.c"),
                            ("forgectrl", "src/lenshome.c"),
                            ("forgectrl", "src/accel.c"), ("forgectrl", "src/cool.c")],
      requires=["forgectrl.auth", "motion.pacing"],
      description="POST /wiz/motion/start: the controller stops, the liveness probe runs and the "
                  "lens takes its hall-edge reference behind it (a lens that cannot reach its "
                  "edge is a motion fault, and no controller starts), the lens "
                  "finds the hall reference in five agreeing passes, the controller comes back in "
                  "loopback posture, and the head jogs 50 mm each way on X and Y with the "
                  "accelerometer as the witness. The one prompt (the jogs are about to move) is "
                  "answered Continue. The result carries the probe line, the passes, and a "
                  "witnessed reading for each jog; the controller must be back in its normal "
                  "posture, and the kernel position counters must read what they read before: "
                  "every move played to its end (the probe's return leg under the cooling "
                  "engine's dead-man, the last jog under the controller stop).")
def check_motion(ctx):
    fc = ctx.forgectrl
    before = read_position()
    ctx.notice("The head moves 50 mm each way on X and on Y. Keep the bed clear.")
    last = run_check(ctx, "motion", lambda p: "Continue" if p.get("id") == "jogs" else None, 420)
    ctx.clear_notice()
    r = last.get("result") or {}
    ctx.check(r.get("z_referenced") is True, "the lens was not referenced: %s", r)
    ctx.check(len(r.get("z_passes") or []) == 5, "not five reference passes: %s", r.get("z_passes"))
    moves = r.get("moves") or {}
    for name in ("+X", "-X", "+Y", "-Y"):
        m = moves.get(name) or {}
        ctx.check(m.get("witnessed") is True, "the %s jog was not witnessed: %s", name, m)
    ok = ctx.wait_for(lambda: (fc.get("/mode")[1] or {}).get("local") is False
                      and (fc.get("/mode")[1] or {}).get("controller") == "running", 90)
    ctx.check(ok is not None, "the controller is not back in its normal posture")
    after = read_position()
    if before is not None and after is not None:
        ctx.check(all(abs(a - b) <= 2 for a, b in zip(after[:2], before[:2])),
                  "the check lost motion: position %s before, %s after (steps)", before, after)
    ctx.log("motion: probe '%s', passes %s, rest %s, moves %s", r.get("probe"), r.get("z_passes"),
            r.get("rest"), {k: (v.get("p2p_lp_x"), v.get("p2p_lp_y")) for k, v in moves.items()})


@test("commission.check-flow-verify", title="The flow check runs as a setup check",
      subsystem="commission", kind="auto", hardware="takeover", est_min=5,
      covers=DARK_COVERS + [("forgectrl", "src/diag.c")],
      requires=["forgectrl.auth", "cooling.aa-offset-calibrate", "cooling.flow-verify"],
      description="POST /wiz/cooling.flow-verify/start drives the flow-verify diagnostic through the "
                  "check runner: the diagnostic's phases show as the check's, its result becomes the "
                  "check's result with pass true, and the record carries cooling.flow-verify at "
                  "version 1. A thin margin recommends the calibration in the record's flags. The "
                  "air-assist offset calibration is named first among the prerequisites so a queue "
                  "keeps it ahead of every heater tool this check pulls forward.")
def check_flow_verify(ctx):
    last = run_check(ctx, "cooling.flow-verify", lambda p: None, 600)
    r = last.get("result") or {}
    ctx.check(r.get("pass") is True, "the flow check did not pass: %s", r)
    ctx.log("flow check: threshold %s, flow %s, no-flow %s, thin %s", r.get("threshold"),
            r.get("flow_rise"), r.get("noflow_rise"), r.get("thin_margin"))


@test("commission.cloud-header-capture", title="The cloud header check takes one print's envelope",
      subsystem="commission", kind="operator", hardware="takeover", mode="grbl", est_min=8,
      covers=DARK_COVERS + [("forgectrl", "src/super.c"),
                            ("python3-gfhardware", "gfhardware/machine.py"),
                            ("python3-gfhardware", "forgefirm-app/gfcloud.py"),
                            ("python3-gfhardware", "forgefirm-app/ffmachine.py")],
      requires=["forgectrl.auth", "cloud.mode-switch"], hands=["workstation"],
      steps=["The Glowforge app open on the workstation, signed in to this machine. The test "
             "turns cloud mode on itself and puts it back.",
             "Once the app shows the machine online, place any small design on the bed image "
             "and press Print. That is the only action: the check takes the print's header and "
             "cancels the print before it arms. Do not press the machine's button.",
             "The machine returns to GRBL mode on its own."],
      description="POST /wiz/cloud.header/start with cloud mode on: the wizard writes the "
                  "client's one-start capture marker, starts cloud mode in the wizard's posture, "
                  "asks for a print, and takes the header the client writes to the run directory "
                  "before it cancels the print. The result carries the tag count, the "
                  "calibration-bearing tags, the limits, and the machine's own numbers; the log "
                  "has the tagged block; the machine is back in GRBL mode with the marker gone.")
def cloud_header_capture(ctx):
    fc = ctx.forgectrl
    ev = ctx.evidence
    with Restore(ctx, ["cloud_enabled", "homing_mode", "controller_mode"]):
        s = fc.settings() or {}
        if s.get("cloud_enabled") != "1":
            st, body = fc.post("/settings", data={"cloud_enabled": "1"})
            ctx.check(st == 200, "cloud_enabled=1 -> %s %s", st, body)
            ctx.log("cloud mode turned on for the test")
        ctx.notice("Print any small design from the Glowforge app once it shows the machine "
                   "online. Do not press the machine's button.")
        last = run_check(ctx, "cloud.header",
                         lambda p: "Continue" if p.get("id") == "print" else None, 900)
        ctx.clear_notice()
        r = last.get("result") or {}
        ctx.check(isinstance(r.get("tag_count"), int) and r["tag_count"] > 100,
                  "the header carried %s tags", r.get("tag_count"))
        kept = r.get("calibration_tags") or {}
        ctx.check(len(kept) > 10, "only %d calibration-bearing tags kept", len(kept))
        ev["kept"] = sorted(kept)[:40]
        ctx.check(not os.path.exists("/run/gfcloud-capture"), "the capture marker is still down")
        ok = ctx.wait_for(lambda: (fc.get("/mode")[1] or {}).get("mode") == "grbl"
                          and (fc.get("/mode")[1] or {}).get("controller") == "running", 120)
        ctx.check(ok is not None, "the machine did not return to GRBL mode")
        ctx.log("header: %d tags, %d kept; job %s", r.get("tag_count", 0), len(kept), r.get("job_id"))
