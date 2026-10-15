# Copyright 2026 514 LLC d/b/a OpenGlow
# Written by Scott Wiederhold
# https://community.openglow.org
# SPDX-License-Identifier:    MIT

"""setup.check-* - the setup's checks (the dark wizards), driven the
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
from .setup import wiz, Restore, INTAKE_KEY

POLL_S = 1.0
# What the machine's own press prompt says, used when it sends no text.
PRESS_TEXT = "The button is lit white. Press it now: the laser fires after your press."
# How long the purge fan's current is given to come up after the airflow
# check switches it back on. The bench reference draws its off current
# (74) for the first seconds and settles near 630.
PURGE_SPINUP_S = 30
# The checks' press prompts read the button (button.c) and light it
# (led.c) on the machine; the switches check reads both back.
DARK_COVERS = [("forgectrl", "src/wizdark.*"), ("forgectrl", "src/wizcalc.*"),
               ("forgectrl", "src/wiz.*"), ("forgectrl", "src/setup.*"),
               ("forgectrl", "src/main.c"), ("forgectrl", "src/ui/wizard.*"),
               ("forgectrl", "src/button.*"), ("forgectrl", "src/led.*")]
# The lens window: the free half-steps below and above the hall edge the
# Lens travel check writes, and the fallback the lens frame holds while
# they are unset (lens.h).
LENS_WINDOW_KEYS = ["lens_stop_below_steps", "lens_stop_above_steps"]
FALLBACK_WINDOW = (10, 12)


def intakes_counted():
    """Precheck: the intake fan count is set. The setup gate holds every
    controller without it, and a test that puts the count back as found
    would put an unset count back and close the gate on the tests after
    it."""
    try:
        s = hw.Forgectrl().settings() or {}
    except hw.HwError as e:
        return "forgectrl unreachable: %s" % e
    if not s.get(INTAKE_KEY):
        return ("the intake fans are not counted, so the setup gate holds every controller; this "
                "test puts the count back as it finds it, and an uncounted machine handed back "
                "would hold the controllers of every test after it")
    return None


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
                elif p.get("id") == "press":
                    # The arm press, asked for by the machine itself: the
                    # check opens this prompt when the button is lit and
                    # the tube is still dark. Nothing answers it - the
                    # press does, and the discharge closes it.
                    ctx.press_now(p.get("text") or PRESS_TEXT)
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


@test("setup.check-switches", title="The switches check follows the lid and the button",
      subsystem="setup", kind="operator", est_min=3,
      covers=DARK_COVERS + [("forgectrl", "src/status.c")],
      requires=["forgectrl.auth"], actions=["lid", "button", "interlock"],
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


@test("setup.check-airflow", title="The airflow check measures the fans and sets the floors",
      subsystem="setup", kind="auto", hardware="takeover", est_min=3,
      covers=DARK_COVERS + [("forgectrl", "src/cool.c"), ("forgectrl", "src/airflow.*"),
                            ("forgectrl", "src/gates.c"), ("forgectrl", "src/super.c")],
      requires=["forgectrl.auth"], precheck=intakes_counted,
      description="POST /wiz/airflow/start: the controller stops, the fans run at the cut profile "
                  "for 35 s, the purge is measured on and off, and the five floors are written: the "
                  "four at 55 percent of steady and the grace from the slowest spin-up. The intake "
                  "fan count is the machine's: intake 2 turning proves two fans, both intakes are "
                  "judged, and the count is written as 2; intake 2 silent on a machine counted with "
                  "one is reported not fitted and not turning, intake 1 alone sets the intake "
                  "floor, and the count is not written; intake 2 silent on a machine counted with "
                  "two fails the check. Both intakes turn on the bench reference, so the bench "
                  "path writes 2. Each fan judged must read at least 1000 rpm. The settings are put "
                  "back as found at the end, the purge fan is on again and drawing over the floor "
                  "the check just wrote, and the controller must be running again.")
def check_airflow(ctx):
    keys = ["cool_tach_exhaust_min_rpm", "cool_tach_intake_min_rpm", "cool_tach_air_assist_min_rpm",
            "cool_purge_min_current", "cool_fan_grace_s"]
    with Restore(ctx, keys + ["cool_intake_fans"]) as held:
        counted = held.before.get("cool_intake_fans")
        last = run_check(ctx, "airflow", lambda p: None, 240)
        r = last.get("result") or {}
        fans = r.get("fans") or {}
        intakes = r.get("intake_fans")
        silent2 = (fans.get("intake 2") or {}).get("turning") is False
        ctx.evidence["intakes"] = {"counted": counted, "found": intakes, "intake_2_silent": silent2}
        ctx.check(intakes in (1, 2), "the result carries no intake fan count: %r", intakes)
        if silent2:
            # one intake fan, as the machine is counted (on a machine
            # counted with two the check itself fails, and run_check says so)
            ctx.check(counted == "1" and intakes == 1,
                      "intake 2 gave no reading on a machine counted with %r intake fans, and the check "
                      "found %r", counted, intakes)
        else:
            ctx.check(intakes == 2, "intake 2 turned, and the check found %r intake fans", intakes)
        ctx.log("intake fans: %s (the machine counted %s)", intakes, counted)
        for name in ("exhaust", "intake 1", "intake 2", "air assist"):
            f = fans.get(name) or {}
            if name == "intake 2" and silent2:
                ctx.check(f == {"fitted": False, "turning": False}, "intake 2 with one intake fan: %s", f)
                continue
            ctx.check(f.get("ok") is True, "%s: %s", name, f)
            ctx.log("%s: %s rpm steady, 90 percent at %s s", name, f.get("steady_rpm"), f.get("spinup_s"))
        floors = r.get("floors") or {}
        for k in keys:
            ctx.check(k in floors, "no floor for %s: %s", k, floors)
        s = ctx.forgectrl.settings() or {}
        for k in keys:
            ctx.check(s.get(k) == floors.get(k), "%s reads %r, the check wrote %r", k, s.get(k), floors.get(k))
        # The count: 2 written when intake 2 turned; left as counted when it
        # was silent.
        want = counted if silent2 else "2"
        ctx.check(s.get("cool_intake_fans") == want,
                  "cool_intake_fans reads %r, expected %r (intake 2 %s, the machine counted %r)",
                  s.get("cool_intake_fans"), want, "silent" if silent2 else "turning", counted)
        ctx.log("floors: %s", json.dumps(floors))
        # The check switches purge air off to read its off current. Leaving
        # it off costs the next job an airflow hold mid-cut, judged against
        # the very floor written above, and nothing puts it back until the
        # daemon restarts: the engine's idle phase never re-applies its own
        # duties. Prove the machine is whole, not just measured.
        purge_on = ctx.sysfs("head/purge_air")
        ctx.check(purge_on == "1", "purge air reads %r after the check, expected 1 (on)", purge_on)

        def purge_gate():
            return ((ctx.forgectrl.get("/cool/status")[1] or {}).get("fan_gates") or {}).get("purge") or {}

        # The check read its off current with the fan off, and the stand-down
        # commands it back on. The current follows the command, it does not
        # arrive with it: read in the same second and what comes back is the
        # off current the check just measured. Wait for the draw the way the
        # controller below is waited for.
        took = ctx.wait_for(lambda: (purge_gate().get("reading") or 0) >= (purge_gate().get("floor") or 0),
                            PURGE_SPINUP_S)
        gate = purge_gate()
        ctx.evidence["purge_after"] = {"purge_air": purge_on, "gate": gate, "took_s": took}
        ctx.log("purge after the check: commanded %s, drawing %s against the %s floor (%s)",
                purge_on, gate.get("reading"), gate.get("floor"),
                "after %.1f s" % took if took is not None else "never reached the floor")
        ctx.check(took is not None,
                  "purge draws %s after %d s, under the %s floor the check just wrote: the next job "
                  "would be held", gate.get("reading"), PURGE_SPINUP_S, gate.get("floor"))
    ok = ctx.wait_for(lambda: (ctx.forgectrl.get("/mode")[1] or {}).get("controller") == "running", 60)
    ctx.check(ok is not None, "the controller did not come back after the check")


@test("setup.check-motion", title="The motion check proves the rail, the lens reference, and the jogs",
      subsystem="setup", kind="auto", hardware="takeover", est_min=5,
      covers=DARK_COVERS + [("forgectrl", "src/super.c"), ("forgectrl", "src/liveness.c"),
                            ("forgectrl", "src/lenshome.*"),
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


@test("setup.check-lens", title="The lens travel check finds the stops and writes the window",
      subsystem="setup", kind="auto", hardware="takeover", est_min=4,
      covers=DARK_COVERS + [("forgectrl", "src/lensfind.*"), ("forgectrl", "src/lens.*"),
                            ("forgectrl", "src/lenshome.*"), ("forgectrl", "src/accel.c"),
                            ("forgectrl", "src/cool.c"), ("forgectrl", "src/super.c")],
      requires=["forgectrl.auth", "setup.check-motion"],
      description="POST /wiz/lens.travel/start: the controller stops, the lens takes its hall-edge "
                  "reference, the fans stop and the head accelerometer listens while the lens "
                  "steps to each stop and back (nothing fires), the two window settings are "
                  "written, and the controller comes back. No question is asked. The result "
                  "carries the reference passes, the stops (found, the free half-steps each way, "
                  "the contact counts, or why not), and the window: the stops found, or the "
                  "fallback (10 half-steps below, 12 above) when they were not. The settings and "
                  "the lens block of /status hold that window, the record carries lens.travel at "
                  "version 1, and the two settings are put back as found. grblHAL reads the "
                  "window only at its start, and the check started it before the put-back, so in "
                  "GRBL mode the controller is stopped and started again on the window as found "
                  "(on every exit path), and it must be running with motion verified under a new "
                  "process.")
def check_lens(ctx):
    fc = ctx.forgectrl
    ev = ctx.evidence
    restart = {}

    def on_prompt(p):
        ctx.fail("the lens travel check asked a question: %s", p)

    def restart_on_found():
        """A fresh controller start on the settings as found, once the
        check has ended. Plain waits, not the run's, so an abort gets it
        too; a check still ending starts its own controller after the
        put-back, which reads them already."""
        try:
            t_end = time.time() + 120
            while time.time() < t_end and dark(fc).get("running"):
                time.sleep(1)
            if dark(fc).get("running"):
                restart["skipped"] = "the check is still ending"
                ctx.log("the check is still ending: its own controller start reads the settings as found")
                return
            st, m = fc.get("/mode")
            m = m if isinstance(m, dict) else {}
            restart.update(mode=m.get("mode"), pid=m.get("pid"))
            if m.get("mode") != "grbl":
                return                      # the cloud client reads no lens window
            restart["stop"], body = fc.post("/controller/stop")
            ctx.log("restarting the controller on the window as found: POST /controller/stop -> %s %s",
                    restart["stop"], body)
            for _ in range(10):
                restart["start"], body = fc.post("/controller/start")
                if restart["start"] != 409:
                    break
                time.sleep(1)               # the check's lease goes just after its end
            ctx.log("POST /controller/start -> %s %s", restart["start"], body)
        except hw.HwError as e:
            restart["error"] = str(e)
            ctx.log("the controller could not be restarted on the window as found: %s", e)
    try:
        with Restore(ctx, LENS_WINDOW_KEYS):
            last = run_check(ctx, "lens.travel", on_prompt, 300)
            r = last.get("result") or {}
            ctx.check(len(r.get("z_passes") or []) >= 1, "the lens was not referenced: %s", r.get("z_passes"))
            stops, w = r.get("stops") or {}, r.get("window") or {}
            ctx.check(isinstance(stops.get("found"), bool), "the result files no stops: %s", r)
            window = (w.get("below"), w.get("above"))
            if stops.get("found"):
                ctx.check(window == (stops.get("below"), stops.get("above")) and w.get("found") is True,
                          "the window %s is not the stops found %s", w, stops)
            else:
                ctx.check(window == FALLBACK_WINDOW and w.get("found") is False,
                          "the stops were not found (%s), but the window is %s", stops.get("why"), w)
            s = fc.settings() or {}
            held = tuple(str(s.get(k)) for k in LENS_WINDOW_KEYS)
            lens = (fc.status() or {}).get("lens") or {}
            ev["window"] = {"stops": stops, "window": w, "settings": held, "status": lens}
            ctx.check(held == tuple(str(v) for v in window), "the settings hold %s, the check wrote %s",
                      held, window)
            ctx.check((lens.get("below"), lens.get("above")) == window,
                      "/status reads the window %s/%s, the check wrote %s", lens.get("below"), lens.get("above"),
                      window)
            ctx.log("lens travel: %s half-steps below the edge, %s above (%s); contact %s/%s", window[0],
                    window[1], "the stops found" if stops.get("found") else "the fallback: %s" % stops.get("why"),
                    stops.get("contact_below"), stops.get("contact_above"))
    finally:
        restart_on_found()
        ev["restart"] = dict(restart)
    if restart.get("mode") == "grbl":
        ctx.check(restart.get("stop") == 200 and restart.get("start") == 200,
                  "the controller was not restarted on the window as found: %s", restart)

    def back():
        st, m = fc.get("/mode")
        m = m if isinstance(m, dict) else {}
        return (m.get("controller") == "running" and m.get("motion") == "verified"
                and (restart.get("start") != 200 or m.get("pid") != restart.get("pid")))
    ok = ctx.wait_for(back, 150)
    ctx.check(ok is not None, "the controller did not come back after the check")


@test("setup.cloud-header-capture", title="The cloud header check takes one print's envelope",
      subsystem="setup", kind="operator", hardware="takeover", mode="grbl", est_min=8,
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
