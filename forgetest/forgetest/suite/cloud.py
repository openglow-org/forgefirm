# Copyright 2026 514 LLC d/b/a OpenGlow
# Written by Scott Wiederhold
# https://community.openglow.org
# SPDX-License-Identifier:    MIT

"""cloud.* - the controller mode switch and the optional Glowforge web-service
mode (gfcloud daemon, gfhome homing runner). The mode-switch test makes the
grbl -> cloud -> grbl round trip with the connect-time hunt run lid-open and
the web-service homing ($H) on the way back; the job-behavior tests run in
cloud mode and leave the machine there (see enter_cloud)."""
import json
import os
import socket
import threading
import time

from ..catalog import test
from .. import hw
from .. import puls
from ..baseline import read_position, read_program_total

# Coverage maps of the cloud tests, by what each proves. Globs anchor at
# the repository root; forgefirm-app and python3-gfhardware are two
# recipes over the one repository (the same file list under both names),
# so app paths are named under forgefirm-app and library paths under
# python3-gfhardware. The one real print keeps the coarse maps: it is
# the integration and the floor the coverage lint needs, so whatever
# the finer maps leave out still re-requires it.
_SUPERVISOR = [("forgectrl", "src/super.c"), ("forgectrl", "src/main.c")]
_CLOUD_ALL = [("forgefirm-app", "**"), ("python3-gfhardware", "**"), ("python3-gfutilities", "**")] + _SUPERVISOR

# The cloud client's common ground: its entry, the machine glue, the
# config it starts from, the library's package and identity, the
# cooling reporter every controller runs, and gfutilities' core.
_CLIENT = [("forgefirm-app", "forgefirm-app/gfcloud.py"),
           ("forgefirm-app", "forgefirm-app/ffmachine.py"),
           ("forgefirm-app", "forgefirm-app/gfhome.conf.sample"),
           ("python3-gfhardware", "gfhardware/__init__.py"),
           ("python3-gfhardware", "gfhardware/_common.py"),
           ("python3-gfhardware", "gfhardware/id.py"),
           ("python3-gfhardware", "gfhardware/coolsvc.py"),
           ("python3-gfutilities", "gfutilities/__init__.py"),
           ("python3-gfutilities", "gfutilities/_common.py"),
           ("python3-gfutilities", "gfutilities/configuration.py"),
           ("python3-gfutilities", "gfutilities/device/__init__.py"),
           ("python3-gfutilities", "gfutilities/device/basemachine.py"),
           ("python3-gfutilities", "gfutilities/device/settings.py"),
           ("python3-gfutilities", "gfutilities/service/__init__.py"),
           ("python3-gfutilities", "gfutilities/service/dispatch.py"),
           ("python3-gfutilities", "gfutilities/service/websocket.py")]

# The web session: sign-in, the firmware check, the service loop.
_WEB_SESSION = [("python3-gfutilities", "gfutilities/service/authentication.py"),
                ("python3-gfutilities", "gfutilities/service/gfuiservice.py")]

# The service protocol, answered by the emulator: the web session, the
# emulator and its fixtures, no hardware behind it.
_SERVICE_LAYER = _CLIENT + _WEB_SESSION + [
    ("python3-gfutilities", "gfutilities/device/emulator.py"),
    ("python3-gfutilities", "examples/**")] + _SUPERVISOR

# The machine's print behavior under the offline service: the run loop
# and the hardware it drives, the offline dispatch, the pulse path. Not
# the web session, not the emulator, not the cameras.
_MACHINE_RUN = _CLIENT + [
    ("python3-gfhardware", "gfhardware/machine.py"),
    ("python3-gfhardware", "gfhardware/feeder.py"),
    ("python3-gfhardware", "gfhardware/cnc.py"),
    ("python3-gfhardware", "gfhardware/switches.py"),
    ("python3-gfhardware", "gfhardware/input/**"),
    ("python3-gfhardware", "gfhardware/src/evdev.c"),
    ("python3-gfhardware", "gfhardware/z_axis.py"),
    ("python3-gfhardware", "gfhardware/leds.py"),
    ("python3-gfhardware", "gfhardware/cooling.py"),
    ("python3-gfutilities", "gfutilities/service/offline.py"),
    ("python3-gfutilities", "gfutilities/puls/**")] + _SUPERVISOR

# The service and the machine on the homing path: the real session, the
# hunt with its captures and motions, the web-service homing from GRBL
# mode (gfhome). The whole hardware library, since a hunt drives the
# cameras, the lens, the switches and the feeder. Not the offline
# service, not the emulator.
_HOMING_PATH = _CLIENT + _WEB_SESSION + [
    ("forgefirm-app", "forgefirm-app/**"),
    ("python3-gfhardware", "gfhardware/**"),
    ("python3-gfutilities", "gfutilities/puls/**")] + _SUPERVISOR

GF_LATEST = "/data/forgefirm/gf-latest.json"
GFCLOUD_LOG = "/data/log/forgefirm/gfcloud/gfcloud.log"
FORGECTRL_LOG = "/data/log/forgefirm/forgectrl/forgectrl.log"
# The client names the limits it derived from the pulse header once per
# job; the engine names the effective set whenever it changes.
LIMITS_MARK = "job limits from the header: "
EFFECTIVE_MARK = "effective limits: coolant ceiling "
SESSION_MARKS = ("authenticate_machine SUCCESS", "ws_connect ESTABLISHED")
# The service's connect-time hunt, as the client logs its request: a few
# seconds after the controller starts, right behind the session.
HUNT_REQUEST = "service action request: hunt"


def log_size(path):
    return hw.log_size(path)


def session_lines(path, offset):
    """New gfcloud log lines since offset that carry a session mark."""
    try:
        with open(path, "rb") as f:
            f.seek(offset)
            data = f.read().decode("utf-8", "replace")
    except OSError:
        return []
    return [ln.strip()[:160] for ln in data.splitlines() if any(m in ln for m in SESSION_MARKS)]


def session_established(lines):
    return all(any(m in ln for ln in lines) for m in SESSION_MARKS)


def wait_mode(ctx, fc, want_mode, want_controller="running", timeout=90, poll=1.0):
    t0 = time.monotonic()
    last = None
    while time.monotonic() - t0 < timeout:
        ctx.checkpoint()
        st, m = fc.get("/mode")
        if st == 200 and isinstance(m, dict):
            last = m
            if m.get("mode") == want_mode and m.get("controller") == want_controller:
                return m
            if m.get("controller") == "motion-fault":
                break
        time.sleep(poll)
    return last


GFHOME_LOG = "/data/log/forgefirm/gfhome/gfhome.log"
HOMING_TIMEOUT_S = 600


def judge_hunt_with_lid_open(ctx, ev, offset):
    """The connect-time hunt from `offset` on: its terminal line is
    :completed, nothing before it was refused for the lid, and the lens
    homed (the Z cycle is the part of a hunt the lid would have gated).
    Returns the hunt's terminal line."""
    hunt_line = wait_action_finished(ctx, offset, "hunt", HUNT_TIMEOUT_S)
    ev["hunt_line"] = message(hunt_line)
    ctx.check(hunt_line, "the service sent no hunt (or it never finished) within %d s of the session",
              HUNT_TIMEOUT_S)
    lines = window_lines(ctx, GFCLOUD_LOG, offset, "that nothing before the hunt's end was refused for the lid")
    hunt_i = action_finish_index(lines, "hunt")
    refused = [ln for ln in lines[:hunt_i] if "unsafe to move" in ln]
    ev["refusals_before_hunt_end"] = len(refused)
    ctx.check(not refused, "the hunt was refused for the lid (%d 'unsafe to move')", len(refused))
    ctx.check(COMPLETED in hunt_line, "the hunt did not complete: %s", ev["hunt_line"])
    ev["lens_homed"] = any("starting z homing cycle" in ln for ln in lines[:hunt_i])
    ctx.check(ev["lens_homed"], "the hunt did not home the lens (no Z homing cycle before its end)")
    ctx.log("hunt with the lid open: %s (lens homed)", ev["hunt_line"])
    return hunt_line


def gfhome_homing(ctx, ev, g):
    """$H in grbl mode with homing_mode = gfcloud: gfhome runs the
    web-service homing session with its own head-accelerometer motion
    witness. Homed within the session timeout, and gfhome's own
    completion line (with its count of motion windows) is the proof the
    head traveled under the service's corrections."""
    st = g.status_report()["state"]
    ctx.check(st.startswith("Idle") or st.startswith("Alarm"), "controller is %s", st)
    if st.startswith("Alarm"):
        g.command("$X")
    home_offset = log_size(GFHOME_LOG)
    t0 = time.monotonic()
    g.send_raw(b"$H\n")
    homed = False
    state = gs = None
    while time.monotonic() - t0 < HOMING_TIMEOUT_S:
        ctx.checkpoint()
        s = ctx.forgectrl.status()
        state = s.get("state")
        try:
            gs = g.status_report()["state"]
        except hw.HwError:
            gs = "?"
        if s.get("homed") and gs.startswith("Idle"):
            homed = True
            break
        if gs.startswith("Alarm"):
            break
        time.sleep(2)
    ev["homing_s"] = round(time.monotonic() - t0, 1)
    ev["homed"] = homed
    ctx.log("homing: homed=%s after %.1f s (kernel %s, grbl %s)", homed, ev["homing_s"], state, gs)
    ctx.check(homed, "homing did not complete (grbl %s)", gs)
    done = [ln.strip()[:160] for ln in log_lines_since(GFHOME_LOG, home_offset) if "homing complete" in ln]
    ev["gfhome_complete"] = done[-1] if done else None
    ctx.log("gfhome: %s", ev["gfhome_complete"])
    ctx.check(done, "gfhome logged no 'homing complete' line (its accelerometer witness never saw the "
                    "head move, or the session ended another way)")
    # homed: the counters are re-anchored at the corner, where the head stays
    ctx.counters_rezeroed()
    ctx.check(ctx.forgectrl.wait_idle(15, abort=ctx.aborted), "machine not idle after homing")
    # The driver answers $H when the session ends; that ok (and the
    # session's messages) sit in the buffer behind the status reports and
    # would pass for the reply to the caller's next command.
    ack = " | ".join(ln.strip() for ln in g.drain().splitlines() if ln.strip())
    ctx.log("$H acknowledged: %s", ack[:160] or "(nothing buffered)")


@test("cloud.mode-switch", title="Controller mode switch grbl -> cloud -> grbl, the connect-time hunt "
                                 "with the lid open, and the web-service homing",
      subsystem="cloud", kind="operator", mode="grbl", est_min=10,
      covers=_HOMING_PATH + [("forgectrl", "src/cool.*"), ("forgectrl", "src/airflow.*"),
                             ("grblhal-glowforge", "src/**")],
      requires=["forgectrl.auth", "motion.pacing"], actions=["lid"],
      steps=["Bed clear, lid closed; cloud credentials configured; the machine on the network. "
             "The test turns cloud mode and the gfcloud homing on itself when they are off, and "
             "puts the settings back at the end.",
             "Open the lid the moment you are told, with a hand ready on it: the cloud client's "
             "first hunt begins a few seconds after its controller starts and must find the lid "
             "open. Leave it open through the connect and the hunt; close it when told. Nothing "
             "else: the switch back and the $H homing run on their own, and the head ends where the "
             "test found it."],
      description="One round trip with the two service-driven motions on it. POST /mode switches "
                  "to the cloud controller with the lid closed (no controller starts with the "
                  "enclosure open): gfcloud comes up under supervision, authenticates and "
                  "establishes its service session (its own log lines are the evidence; the "
                  "connect-time firmware probe is recorded when configured). The lid is opened the "
                  "moment the controller is up, before the client requests its connect-time hunt "
                  "(a hunt requested before the lid was open fails the test), so the hunt runs "
                  "with the lid OPEN, as the factory's does - it completes, nothing before its "
                  "end is refused for the lid, the lens homes - and with the extraction fans off it "
                  "is measured by the airflow gates but not judged (no AIRFLOW, the exhaust row "
                  "reads unjudged); the camera service survives the switch. The lid closed, the "
                  "service's re-hunt is waited out; switching back brings grblHAL up with the Grbl "
                  "port open and Idle, and the head returns to its start by the travel the "
                  "client logged for the service's motions. Then $H with "
                  "homing_mode = gfcloud runs gfhome, the web-service homing session with the "
                  "head-accelerometer motion witness: the controller returns to Idle with "
                  "homed:true, gfhome reports the homing complete with the motion it saw, and "
                  "every service motion of the session ran whole. The camera home is then "
                  "dropped and the head goes back to where the test found it, by the travel the "
                  "session's motions logged.")
def mode_switch(ctx):
    # The hand-back helpers, imported here so that no other cloud test's
    # fingerprint moves.
    from .homeoff import camera_home_return, cloud_mode_return, judge_whole_motions, session_mark
    fc = ctx.forgectrl
    ev = ctx.evidence
    st, m0 = fc.get("/mode")
    ctx.check(st == 200 and isinstance(m0, dict), "GET /mode -> %s", st)
    ev["mode_before"] = m0
    ctx.log("mode before: %s", m0)
    ctx.check(m0.get("mode") == "grbl", "start this test in grbl mode (now %s)", m0.get("mode"))
    # Cloud mode exists only while cloud_enabled is on: the test turns it on
    # with the phrase the cloud step asks for when it is off, and puts it
    # back last (after homing_mode, which forgectrl takes to none when
    # cloud mode goes off).
    cloud = (fc.settings() or {}).get("cloud_enabled", "")
    ev["cloud_enabled_before"] = cloud
    if cloud != "1":
        st, body = fc.post("/settings", data={"cloud_enabled": "1", "phrase": hw.CLOUD_PHRASE})
        ctx.log("cloud mode turned on for the test (cloud_enabled was %r) -> %s", cloud, st)
        ctx.check(st == 200, "cloud_enabled=1 for the test -> %s %s", st, body)
    try:
        st, cam0 = fc.get("/cam/status")
        ev["cam_before"] = cam0
        probe_before = None
        try:
            probe_before = os.stat(GF_LATEST).st_mtime
        except OSError:
            pass
        lamp0 = hw.sysfs_read("pic/lid_led")
        ctx.check(ctx.switch("lid") is True, "close the lid first: no controller starts with it open")

        # The switch is made with the enclosure closed (the supervisor holds
        # every spawn until it is). The lid opens the moment the controller is
        # up: the client requests its connect-time hunt a few seconds after
        # its start, right behind its session, and the hunt must find the lid
        # open. The poll is tight so the window is not spent waiting.
        log_offset = log_size(GFCLOUD_LOG)
        st, body = fc.post("/mode", data={"controller": "cloud"})
        ctx.log("POST /mode controller=cloud -> %s %s", st, body)
        ctx.check(st == 200, "mode switch to cloud refused: %s %s", st, body)
        m = wait_mode(ctx, fc, "cloud", timeout=90, poll=0.2)
        ev["mode_cloud"] = m
        ctx.log("mode after switch: %s", m)
        ctx.check(m and m.get("mode") == "cloud" and m.get("controller") == "running",
                  "cloud controller did not come up: %s", m)
        ctx.act("lid", "open", text="Now, at once: the cloud client's first hunt begins a few seconds "
                "after its controller starts and must find the lid open. Leave it open through the "
                "connect and the hunt.")
        # The order is the proof: the hunt's request is not in the log yet
        # when the lid reads open.
        ev["hunt_before_lid_open"] = any(HUNT_REQUEST in ln for ln in window_lines(
            ctx, GFCLOUD_LOG, log_offset, "that the client had not requested its hunt before the lid opened"))
        ctx.check(not ev["hunt_before_lid_open"],
                  "the client requested its hunt before the lid was open (the hunt follows the "
                  "controller's start by a few seconds): the lid opened too late, so a hunt with the "
                  "lid open is not proven")
        # the client's own session lines are the evidence of a live cloud session
        session = wait_session(ctx, log_offset)
        probe = None
        ev["session"] = session
        for ln in session:
            ctx.log("  gfcloud: %s", ln.split(" ", 1)[-1] if " " in ln else ln)
        try:
            mt = os.stat(GF_LATEST).st_mtime
            if probe_before is None or mt > probe_before:
                with open(GF_LATEST) as f:
                    probe = json.load(f)
        except (OSError, ValueError):
            pass
        ev["gf_probe"] = probe
        ctx.log("cloud session established: %s; firmware probe: %s", session_established(session), probe)
        ctx.check(session_established(session),
                  "the cloud client never established its service session (no credentials, no "
                  "network, or the service refused) - cloud mode not proven")
        st, cam1 = fc.get("/cam/status")
        ev["cam_during_cloud"] = cam1
        ctx.check(st == 200 and isinstance(cam1, dict), "camera status lost during cloud mode (%s)", st)
        # The connect-time hunt reports a run to the cooling engine with the
        # exhaust and the intakes commanded off (the factory's motion profile):
        # the gates measure it and judge nothing, whatever the floors say.
        hunt, samples = watch_hunt_gates(ctx, log_offset, HUNT_TIMEOUT_S)
        ev["hunt"] = hunt
        ev["hunt_gates"] = samples
        ctx.check(hunt, "the connect-time hunt did not finish within %d s", HUNT_TIMEOUT_S)
        run = [c for c in samples if c.get("phase") == "run"]
        ctx.log("hunt: %d status samples, %d in phase run; verdicts %s", len(samples), len(run),
                sorted({c.get("verdict") for c in samples}))
        ctx.check(run, "no /cool/status sample saw the hunt as a run session (the client did not "
                       "report run, or the hunt was shorter than the 1 s poll)")
        ctx.check(all(c.get("verdict") != "AIRFLOW" for c in samples),
                  "a hunt with its extraction fans off tripped an airflow gate")
        exh = [((c.get("fan_gates") or {}).get("exhaust") or {}) for c in run]
        ctx.check(exh and all(g.get("state") == "unjudged" for g in exh),
                  "the exhaust gate judged a hunt: states %s", sorted({g.get("state") for g in exh}))
        ctx.check(exh and all(g.get("reading", 0) < g.get("floor", 0) for g in exh),
                  "the exhaust was not off during the hunt (readings %s), so the unjudged state "
                  "proves nothing", sorted({g.get("reading") for g in exh}))
        # ...and it ran with the lid open, as the factory's does
        judge_hunt_with_lid_open(ctx, ev, log_offset)
        ctx.act("lid", "close", text="The service now re-finds the head: several moves with lid images "
                "between them, which the test waits out.")
        settle_cloud(ctx, log_offset)
        lines = log_lines_since(GFCLOUD_LOG, log_offset)
        ev["motions_after_lid_close"] = sum(1 for ln in lines if "motion [" in ln and COMPLETED in ln)
        ctx.log("%d service motion(s) completed after the lid closed", ev["motions_after_lid_close"])

        st, body = fc.post("/mode", data={"controller": "grbl"})
        ctx.log("POST /mode controller=grbl -> %s %s", st, body)
        ctx.check(st == 200, "mode switch back to grbl refused: %s %s", st, body)
        m = wait_mode(ctx, fc, "grbl", timeout=120)
        ev["mode_after"] = m
        ctx.log("mode after switch back: %s", m)
        ctx.check(m and m.get("mode") == "grbl" and m.get("controller") == "running",
                  "grbl controller did not come back: %s", m)
        ctx.check(m.get("motion") != "fault", "motion fault after the switch")
        ctx.sleep(3)
        ctx.check(hw.grbl_port_open(), "Grbl port not open after the switch back")
        with ctx.grbl() as g:
            st = g.status_report()["state"]
            ev["grbl_state"] = st
            ctx.log("grbl state after: %s", st)
            ctx.check(st.startswith("Idle") or st.startswith("Alarm"), "grbl reports %s", st)
        st, cam2 = fc.get("/cam/status")
        ev["cam_after"] = cam2
        ctx.check(st == 200, "camera status lost after the switch back")
        # The service moved the head in cloud mode, and the controller's start
        # zeroed the counters where it left it: the client's record brings it back.
        cloud_mode_return(ctx, ev, log_offset)
        # cloud mode sets its own lid-lamp level (LLvl) and leaves it: hand back the level found
        lamp1 = hw.sysfs_read("pic/lid_led")
        ev["lid_lamp"] = {"before": lamp0, "after_cloud": lamp1}
        if lamp0 is not None and lamp1 != lamp0:
            hw.sysfs_write("pic/lid_led", lamp0)
            ctx.log("lid lamp: cloud mode left %s, restored %s", lamp1, lamp0)

        # -- the web-service homing from grbl mode --------------------------------
        ev["homing_mode"] = (fc.settings() or {}).get("homing_mode")
        if ev["homing_mode"] != "gfcloud":
            st, body = fc.post("/settings", data={"homing_mode": "gfcloud"})
            ctx.log("homing_mode=gfcloud for the homing -> %s %s", st, body if isinstance(body, str) else "")
            ctx.check(st == 200, "homing_mode=gfcloud -> %s %s", st, body)
        session_at = session_mark()
        try:
            with ctx.grbl() as g:
                gfhome_homing(ctx, ev, g)
                judge_whole_motions(ctx, ev, session_at)
        finally:
            if ev["homing_mode"] != "gfcloud":
                # Back to exactly what the machine had: unset is the empty
                # string, and clearing a key needs the query-string form.
                # "none" is a value, and writing it where the machine had
                # nothing is a leftover the hand-back reports.
                st, body = (fc.post("/settings", params={"homing_mode": ""})
                            if not ev["homing_mode"]
                            else fc.post("/settings", data={"homing_mode": ev["homing_mode"]}))
                ctx.log("restore homing_mode=%r -> %s", ev["homing_mode"], st)
            camera_home_return(ctx, ev, session_at)
        ctx.log("PASS: grbl -> cloud (session, hunt with the lid open, lens homed, airflow unjudged) -> "
                "grbl (port open, %s), then $H homed in %.1f s", ev["grbl_state"], ev["homing_s"])
    finally:
        if cloud != "1":
            st, _ = (fc.post("/settings", params={"cloud_enabled": ""}) if not cloud
                     else fc.post("/settings", data={"cloud_enabled": cloud}))
            ctx.log("restore cloud_enabled=%r -> %s", cloud, st)


@test("cloud.service-protocol", title="The service protocol, answered by the emulator in this "
                                       "machine's identity",
      subsystem="cloud", kind="operator", est_min=5,
      covers=_SERVICE_LAYER,
      requires=["forgectrl.auth"], hands=["app"],
      steps=["Cloud credentials configured; the machine on the network; the app open in a browser "
             "(anyone can drive it, at the machine or not: nothing here moves, arms, or fires).",
             "When told, set up any small job in the app and press Print; the emulator runs it at "
             "once. Then confirm the app shows the print complete."],
      description="The Glowforge service, end to end, with the machine emulated: the cloud client "
                  "restarted as gfutilities' Emulator in this machine's identity signs in, passes "
                  "the firmware check, opens the WebSocket, answers the connect-time hunt and the "
                  "service's image requests with canned frames, and runs a print from the app "
                  "through the same download path a job takes (the pulse header parsed, the "
                  "serial and format checked) and the same lifecycle events, to ':completed'. "
                  "What the service accepts is what the app shows. The real client is restarted "
                  "afterward and its connect-time hunt waited out. The machine's own side of a "
                  "print is the other cloud tests' to prove.")
def service_protocol(ctx):
    ev = ctx.evidence
    offset = enter_emulator(ctx)
    try:
        service_protocol_body(ctx, ev, offset)
    finally:
        leave_emulator(ctx)
        import shutil
        shutil.rmtree(EMULATOR_WORK, ignore_errors=True)


def service_protocol_body(ctx, ev, offset):
    # the service's connect-time hunt, answered at once
    hunt = wait_action_finished(ctx, offset, "hunt", HUNT_TIMEOUT_S)
    ev["hunt"] = message(hunt)
    ctx.check(hunt, "the service sent no connect-time hunt (or it never finished) within %d s", HUNT_TIMEOUT_S)
    ctx.check(COMPLETED in hunt, "the emulator's hunt did not complete: %s", ev["hunt"])
    # and the image it asks for after, uploaded from the canned frames
    got = wait_log(ctx, offset, ["img_upload COMPLETE"], 120)
    ctx.check(got["img_upload COMPLETE"], "no image upload completed after the hunt")
    ctx.notice("In the app the machine shows Ready (its bed image is the emulator's canned frame). "
               "Set up any small job and press Print. Nothing moves here; the test watches the "
               "service's print reach the emulator and complete.")
    try:
        got = wait_log(ctx, offset, ["service action request: print"], 600)
        ctx.check(got["service action request: print"], "no print reached the machine within 600 s")
        fin = wait_action_finished(ctx, offset, "print", 300)
    finally:
        ctx.clear_notice()
    ev["print"] = message(fin)
    ctx.check(fin, "the print did not finish within 300 s of reaching the machine")
    ctx.check(COMPLETED in fin, "the print did not complete: %s", ev["print"])
    lines = log_lines_since(GFCLOUD_LOG, offset)
    ev["images_uploaded"] = sum(1 for ln in lines if "img_upload COMPLETE" in ln)
    ev["pulse"] = next((message(ln) for ln in lines if "pulse data is" in ln), None)
    ctx.log("print: %s; %d images uploaded; %s", ev["print"], ev["images_uploaded"], ev["pulse"])
    ctx.check(ev["pulse"], "the print's pulse data was not downloaded and parsed")
    infos = []
    try:
        infos = sorted(f for f in os.listdir(EMULATOR_WORK) if f.endswith(".info"))
    except OSError:
        pass
    ev["downloads"] = infos
    header = None
    if infos:
        try:
            with open(os.path.join(EMULATOR_WORK, infos[-1])) as f:
                header = (json.load(f) or {}).get("header_data") or {}
        except (OSError, ValueError):
            header = None
    ev["header_tags"] = len(header) if header else 0
    ev["header_stfr"] = (header or {}).get("STfr")
    ctx.check(header and header.get("STfr"), "the downloaded job's header was not parsed (%s)", infos)
    ctx.log("the job's header: %d tags, STfr %s", ev["header_tags"], ev["header_stfr"])
    ctx.confirm("Does the app show the print complete?")
    ctx.log("PASS: session, hunt, %d image upload(s), a print downloaded (%d-tag header) and "
            "completed, all against the real service with no hardware behind it",
            ev["images_uploaded"], ev["header_tags"])


# ---- lid / button behavior of a cloud job (the factory's) ------------------
#
# These tests run IN cloud mode and stay there: enter_cloud() reuses a live
# cloud session when the machine is already in cloud mode (the operator
# switched once, on the panel or through an earlier test) and switches -
# once, declaring the change to the baseline - only when it finds GRBL
# mode. Nothing switches back; the operator does, when done. Each test
# judges the gfcloud log from its own window (the offset enter_cloud
# returns) and waits for the service's deferred moves (the re-hunt after a
# lid close, the hunt after a print) to finish before it ends, so the next
# run - or the operator - gets a quiet machine.

# The offline service (gfcloud --offline, or the marker file at start):
# the machine driven from a local socket, no web session at all. Its log
# mark counts among the websocket state marks as "not connected".
OFFLINE_MARK = "OFFLINE service"
OFFLINE_MARKER = "/run/gfcloud-offline"
OFFLINE_SOCKET = "/run/gfcloud-offline.sock"
JOB_DIR = "/tmp/forgetest"          # the jobs the offline tests write (tmpfs)
# The emulator (gfcloud --emulate, or the marker at start): the real
# service driven in this machine's identity with no hardware behind it.
EMULATE_MARK = "EMULATE: the emulator"
EMULATE_MARKER = "/run/gfcloud-emulate"
EMULATOR_WORK = "/tmp/gfcloud-emulate"
# No connect-time hunt (gfcloud --no-hunt, or the marker at start): the
# first settings report in the reconnect form, the service keeps the head
# position it has. For a restart between tests that do not home; the
# homing tests and the one real print get their hunt.
NOHUNT_MARK = "NO-HUNT:"
NOHUNT_MARKER = "/run/gfcloud-nohunt"
HUNT_DONE = "hunt ["
WS_MARKS = ("RX-EVENT: ready", "RX-EVENT: closed", "RECONNECTING", "CLOSING", OFFLINE_MARK)
# The service at work: every line of a service action, the requests, the image
# uploads and the ends included. Between a lid image and its next move the
# service is thinking and the log is silent, so the ends count: the quiet is
# measured from the last line of any action.
ACTIVITY_MARKS = ("service action request:", "img_upload COMPLETE", "finished with event",
                  "start motion", "end motion", "start return home", "return home complete",
                  "starting run", "finished run", "starting z homing cycle")
LOG_TAIL_BYTES = 4 << 20
# The service's think time after an image, on the bench reference: over 1901
# motion, lid-image and hunt requests, half came within 1.6 s of the line before,
# 99 percent within 12.8 s, and two after more than 30 s (32.8 and 62.2 s).
QUIET_S = 30
QUIET_TIMEOUT_S = 180
HUNT_TIMEOUT_S = 180


def log_lines_since(path, offset):
    """New gfcloud log lines since offset (each 'ISO-time gfcloud[pid] LEVEL
    where message'), across a rotation (hw.log_window); none while the
    window cannot be read, for a line the test waits to see."""
    text = hw.log_window(path, offset)
    return text.splitlines() if text is not None else []


def window_lines(ctx, path, offset, what):
    """The log lines since offset, for a check that a line was not
    written: a window that cannot be read fails the test, since an
    absence read on nothing shows nothing."""
    text = hw.log_window(path, offset)
    ctx.check(text is not None, "%s since the test's mark cannot be read (rotated away, or unreadable): "
              "%s is not shown", path, what)
    return text.splitlines()


def log_tail(path, max_bytes=LOG_TAIL_BYTES):
    """The last max_bytes of the log, as lines (the first may be partial)."""
    size = log_size(path)
    return log_lines_since(path, max(0, size - max_bytes))


def line_time(line):
    """Seconds (float) from the log line's ISO timestamp, or None."""
    try:
        ts = line.split(" ", 1)[0]
        head, frac = ts[:19], ts[19:]
        micro = 0.0
        if frac.startswith("."):
            digits = ""
            for ch in frac[1:]:
                if ch.isdigit():
                    digits += ch
                else:
                    break
            micro = float("0." + digits) if digits else 0.0
        return time.mktime(time.strptime(head, "%Y-%m-%dT%H:%M:%S")) + micro
    except (ValueError, IndexError):
        return None


def wait_log(ctx, offset, needles, timeout, poll=0.5):
    """Wait until every needle has appeared in the gfcloud log since offset;
    returns {needle: first matching line or None}."""
    found = {n: None for n in needles}
    t0 = time.monotonic()
    while time.monotonic() - t0 < timeout:
        ctx.checkpoint()
        for ln in log_lines_since(GFCLOUD_LOG, offset):
            for n in needles:
                if found[n] is None and n in ln:
                    found[n] = ln
        if all(found.values()):
            break
        time.sleep(poll)
    return found


# What the client logs when it turns a print away before the button wait
# (machine._safe_to_move): the lid or the interlock, a machine that is not
# idle, the coolant above the start ceiling of its own configuration
# (THERMAL.max_start_temp), a coolant sensor that reads nothing.
REFUSED_BEFORE_THE_BUTTON = ("unsafe to move", "machine is not idle", "machine temp is too high",
                             "coolant sensor reads invalid")


def wait_button_wait(ctx, offset, timeout, also=()):
    """Wait for the client's button wait (and the `also` needles). A print
    the client turns away never gets there: it logs why and finishes at
    once, so the wait ends on that finish line too and the failure carries
    the client's own reason, not two minutes of silence. Returns wait_log's
    result."""
    needles = list(also) + ["waiting for button"]
    found = {n: None for n in needles}
    t0 = time.monotonic()
    while time.monotonic() - t0 < timeout:
        ctx.checkpoint()
        lines = log_lines_since(GFCLOUD_LOG, offset)
        for ln in lines:
            for n in needles:
                if found[n] is None and n in ln:
                    found[n] = ln
        if all(found.values()):
            break
        if found["waiting for button"] is None and action_finish_index(lines, "print") is not None:
            why = [message(ln) for ln in lines if any(r in ln for r in REFUSED_BEFORE_THE_BUTTON)]
            fin = lines[action_finish_index(lines, "print")]
            ctx.fail("the client turned the print away before the button wait: %s (%s)",
                     "; ".join(why[-3:]) or "it logged no reason", message(fin))
        time.sleep(0.5)
    ctx.check(found["waiting for button"], "the print never reached the button wait")
    return found


def log_has(offset, needle):
    """True when the gfcloud log carries needle since offset (one read;
    the condition an `act` waits on)."""
    return any(needle in ln for ln in log_lines_since(GFCLOUD_LOG, offset))


def action_finish_index(lines, action):
    """Index of the first '<action> [id]: finished with event ...' line, or None."""
    return next((i for i, ln in enumerate(lines)
                 if (action + " [") in ln and "finished with event" in ln), None)


def check_pause_resume(ctx, got, offset, what="the run"):
    """The pause and the resume, judged on the run loop's lines (got is
    wait_log's result over PAUSE_LINES + RESUME_LINES)."""
    ctx.check(got["button pressed mid-run; pausing"], "the press did not pause %s", what)
    ctx.check(got["paused at"], "the pause did not settle (no 'paused at')")
    ctx.check(got["button pressed while paused"], "the second press was not seen while paused")
    refused = [ln for ln in window_lines(ctx, GFCLOUD_LOG, offset, "that the resume was not refused")
               if RESUME_REFUSED in ln]
    ctx.check(not refused, "the resume was refused: %s", message(refused[0]) if refused else "")
    ctx.check(got["resuming (laser lead"], "the second press did not resume (no retraced restart logged)")


def wait_action_finished(ctx, offset, action, timeout, poll=0.5):
    """The action's own terminal line ('<action> [id]: finished with event
    ":completed"' / '":cancelled"'), or None within timeout."""
    t0 = time.monotonic()
    while time.monotonic() - t0 < timeout:
        ctx.checkpoint()
        lines = log_lines_since(GFCLOUD_LOG, offset)
        i = action_finish_index(lines, action)
        if i is not None:
            return lines[i]
        time.sleep(poll)
    return None


def watch_hunt_gates(ctx, offset, timeout):
    """Wait for the action's terminal line like wait_action_finished,
    sampling /cool/status twice a second meanwhile (a hunt's run phase
    can be a few seconds). Returns (line or None, samples)."""
    fc = ctx.forgectrl
    samples = []
    t0 = time.monotonic()
    while time.monotonic() - t0 < timeout:
        ctx.checkpoint()
        st, c = fc.get("/cool/status")
        if st == 200 and isinstance(c, dict):
            samples.append(c)
        lines = log_lines_since(GFCLOUD_LOG, offset)
        i = action_finish_index(lines, "hunt")
        if i is not None:
            return lines[i], samples
        time.sleep(0.5)
    return None, samples


def message(line):
    """The message part of a log line (after 'ISO-time gfcloud[pid]')."""
    return line.split(" ", 2)[-1] if line else None


PROGRESS_MARK = "print:progress: reporting against "


def progress_denominator(lines):
    """The length the print reported its progress against, or None.

    The client names it once per job, which is the number worth having: it
    is what every progress frame of that job divides by.
    """
    for ln in lines:
        if PROGRESS_MARK in ln:
            try:
                return int(ln.split(PROGRESS_MARK, 1)[1].split()[0])
            except (IndexError, ValueError):
                return None
    return None


def session_live(pid):
    """(live, detail): the running cloud client (gfcloud[pid]) has a live
    service session when its last websocket state line is 'ready' - a
    later 'closed'/'RECONNECTING'/'CLOSING' means it is not connected
    now. Without any line for that pid the newest lines of the log stand
    in (the client may log under a wrapper's pid)."""
    tag = "gfcloud[%s]" % pid if pid else None
    last_pid = last_any = None
    emulator = False
    for ln in log_tail(GFCLOUD_LOG):
        if EMULATE_MARK in ln and (not tag or tag in ln):
            emulator = True
        for m in WS_MARKS:
            if m in ln:
                last_any = m
                if tag and tag in ln:
                    last_pid = m
    last = last_pid if last_pid is not None else last_any
    where = "pid %s" % pid if last_pid is not None else "newest lines"
    if last == OFFLINE_MARK:
        return False, "%s: offline service, no web session" % where
    if emulator:
        # the emulator's session is live, and it is not the machine's
        return False, "%s: the emulator, not the machine" % where
    return last == "RX-EVENT: ready", "%s: last websocket state %s" % (where, last)


def session_hunted(pid):
    """The running cloud client (gfcloud[pid]) has homed the machine under
    the service: a hunt of its own finished (the connect-time one, or a
    later re-hunt), and it is not the emulator. A client started without
    the hunt, or one whose lines have left the log tail, has not - the
    service may hold a head position the machine no longer has."""
    tag = "gfcloud[%s]" % pid
    hunted = False
    for ln in log_tail(GFCLOUD_LOG):
        if tag not in ln:
            continue
        if EMULATE_MARK in ln:
            return False
        if HUNT_DONE in ln and "finished with event" in ln and COMPLETED in ln:
            hunted = True
    return hunted


def client_offline(pid):
    """The running cloud client is the offline service."""
    live, detail = session_live(pid)
    return (not live) and "offline" in detail


def wait_quiet(ctx, offset, quiet_s=None, timeout=None):
    """The service's moves are over: the machine idle and no new line of any
    service action in the log (ACTIVITY_MARKS) for quiet_s (default QUIET_S;
    timeout QUIET_TIMEOUT_S). False on timeout."""
    quiet_s = QUIET_S if quiet_s is None else quiet_s
    timeout = QUIET_TIMEOUT_S if timeout is None else timeout
    fc = ctx.forgectrl
    t0 = time.monotonic()
    n_seen = -1
    last_change = t0
    while time.monotonic() - t0 < timeout:
        ctx.checkpoint()
        lines = log_lines_since(GFCLOUD_LOG, offset)
        n = sum(1 for ln in lines if any(m in ln for m in ACTIVITY_MARKS))
        if n != n_seen:
            n_seen = n
            last_change = time.monotonic()
        if fc.status().get("state") == "idle" and time.monotonic() - last_change >= quiet_s:
            return True
        time.sleep(0.5)
    return False


def wait_session(ctx, offset, timeout=120):
    session = []
    t0 = time.monotonic()
    while time.monotonic() - t0 < timeout:
        ctx.checkpoint()
        session = session_lines(GFCLOUD_LOG, offset)
        if session_established(session):
            return session
        time.sleep(2)
    return session


def fresh_cloud_connect(ctx, hunt=True):
    """A NEW cloud client with a fresh service session: in cloud mode the
    controller is restarted through the supervisor's stop/start lever; in
    GRBL mode the mode is switched (and the change declared - the machine
    stays in cloud mode). Its connect-time hunt follows unless hunt is
    False (the no-hunt marker, for a start where the service already
    knows the head). Returns the log offset from before the connect, so
    the caller sees the whole session."""
    fc = ctx.forgectrl
    st, m = fc.get("/mode")
    ctx.check(st == 200 and isinstance(m, dict), "GET /mode -> %s", st)
    offset = log_size(GFCLOUD_LOG)
    if not hunt:
        marker_on(ctx, NOHUNT_MARKER)
    if m.get("mode") == "cloud":
        ctx.log("cloud mode: restarting the cloud client for a fresh connect (was pid %s)", m.get("pid"))
        st, body = fc.post("/controller/stop")
        ctx.check(st == 200, "controller stop refused: %s %s", st, body)
        m = wait_mode(ctx, fc, "cloud", want_controller="standby", timeout=30)
        ctx.check(m and m.get("controller") == "standby", "the cloud client did not stop: %s", m)
        st, body = fc.post("/controller/start")
        ctx.check(st == 200, "controller start refused: %s %s", st, body)
    else:
        ctx.log("%s mode: switching to cloud", m.get("mode"))
        st, body = fc.post("/mode", data={"controller": "cloud"})
        ctx.check(st == 200, "mode switch to cloud refused: %s %s", st, body)
    m = wait_mode(ctx, fc, "cloud", timeout=90)
    ctx.check(m and m.get("mode") == "cloud" and m.get("controller") == "running",
              "cloud controller did not come up: %s", m)
    ctx.mode_changed("cloud")
    session = wait_session(ctx, offset)
    ctx.check(session_established(session),
              "the cloud client never established its service session (no credentials, no "
              "network, or the service refused)")
    ctx.log("cloud session established (pid %s)", m.get("pid"))
    return offset


def enter_cloud(ctx):
    """Cloud mode with a live service session, the machine homed under
    it, and quiet. An existing cloud session is reused when the client
    has hunted the machine itself (never the emulator's session, never a
    no-hunt start: the service would be holding a head position the
    machine may not have, and what follows is the one real print); from
    GRBL mode the switch is made once (its connect-time hunt is waited
    out) and the machine stays in cloud mode. Returns the log offset
    where the test's own window begins."""
    fc = ctx.forgectrl
    st, m = fc.get("/mode")
    ctx.check(st == 200 and isinstance(m, dict), "GET /mode -> %s", st)
    offline_marker_off(ctx)
    live = detail = None
    hunted = False
    if m.get("mode") == "cloud" and m.get("controller") == "running":
        live, detail = session_live(m.get("pid"))
        ctx.check(live or "offline" in detail,
                  "cloud mode is up (pid %s) but the client has no live service session (%s) - "
                  "check credentials and network, or restart the controller", m.get("pid"), detail)
        hunted = live and session_hunted(m.get("pid"))
    if live and hunted:
        ctx.log("cloud mode already up (pid %s), service session live and the machine hunted "
                "under it - reusing it", m.get("pid"))
        offset = log_size(GFCLOUD_LOG)
    else:
        if live:
            ctx.log("the running client (pid %s) never hunted the machine under the service: "
                    "restarting it with the hunt", m.get("pid"))
        elif detail:
            ctx.log("the running client is the offline service: restarting it with the service")
        offset = fresh_cloud_connect(ctx)
        hunt = wait_action_finished(ctx, offset, "hunt", HUNT_TIMEOUT_S)
        ctx.check(hunt, "the service sent no connect-time hunt (or it never finished) within %d s",
                  HUNT_TIMEOUT_S)
        ctx.log("connect-time hunt: %s", message(hunt))
    ctx.check(wait_quiet(ctx, offset), "the cloud client was still running service moves after %d s",
              QUIET_TIMEOUT_S)
    return log_size(GFCLOUD_LOG)


def marker_on(ctx, path):
    """A one-start marker for the next cloud client: the client that reads
    it takes it down. Written before the start; offline_marker_off is the
    belt-and-braces removal once the client's own line is in."""
    with open(path, "w") as f:
        f.write("forgetest %s\n" % ctx.test.id)


def offline_marker_off(ctx):
    """No client started from here on comes up offline, as the emulator,
    or without its hunt. The client takes a marker it read down itself;
    this catches one it never read (a start that failed)."""
    for path, what in ((OFFLINE_MARKER, "offline"), (EMULATE_MARKER, "emulate"),
                       (NOHUNT_MARKER, "no-hunt")):
        try:
            os.remove(path)
            ctx.log("%s marker removed", what)
        except OSError:
            pass


def restart_client(ctx, marker=None, who="", hunt=True):
    """The cloud client started fresh - restarted in cloud mode, switched to
    from GRBL mode (the change declared) - under `marker` if one is given,
    and under the no-hunt marker when hunt is False. A marker stays in
    place until the client has read it (the client takes it down; the
    caller waits for the client's own line and takes it down as well if
    the start failed - the supervisor reports the client running seconds
    before Python has finished importing). Returns the log offset from
    before the start."""
    fc = ctx.forgectrl
    st, m = fc.get("/mode")
    ctx.check(st == 200 and isinstance(m, dict), "GET /mode -> %s", st)
    offset = log_size(GFCLOUD_LOG)
    if marker:
        marker_on(ctx, marker)
    if not hunt:
        marker_on(ctx, NOHUNT_MARKER)
    if m.get("mode") == "cloud":
        ctx.log("cloud mode: restarting the client%s (was pid %s)", who, m.get("pid"))
        st, body = fc.post("/controller/stop")
        ctx.check(st == 200, "controller stop refused: %s %s", st, body)
        m = wait_mode(ctx, fc, "cloud", want_controller="standby", timeout=30)
        ctx.check(m and m.get("controller") == "standby", "the cloud client did not stop: %s", m)
        st, body = fc.post("/controller/start")
        ctx.check(st == 200, "controller start refused: %s %s", st, body)
    else:
        ctx.log("%s mode: switching to cloud%s", m.get("mode"), who)
        st, body = fc.post("/mode", data={"controller": "cloud"})
        ctx.check(st == 200, "mode switch to cloud refused: %s %s", st, body)
    m = wait_mode(ctx, fc, "cloud", timeout=90)
    ctx.check(m and m.get("mode") == "cloud" and m.get("controller") == "running",
              "cloud controller did not come up: %s", m)
    ctx.mode_changed("cloud")
    return offset


def enter_emulator(ctx):
    """The real service with the emulator in this machine's identity: the
    client restarted under the emulate marker (one start), its session
    established. Returns the log offset from before the start, so the
    caller sees the connect. The caller restarts the real client when
    done (leave_emulator)."""
    offset = restart_client(ctx, marker=EMULATE_MARKER, who=" as the emulator")
    try:
        got = wait_log(ctx, offset, [EMULATE_MARK], 60)
        ctx.check(got[EMULATE_MARK], "the client did not come up as the emulator within 60 s")
    finally:
        offline_marker_off(ctx)
    session = wait_session(ctx, offset)
    ctx.check(session_established(session),
              "the emulator never established its service session (no credentials, no network, "
              "or the service refused)")
    ctx.log("emulator session established: %s", "; ".join(ln.split(" ", 1)[-1][:80] for ln in session))
    return offset


def leave_emulator(ctx):
    """The real client back without its connect-time hunt (nothing moved
    while the emulator had the session, and a test that prints hunts
    first regardless), its session waited for, so the machine is what
    the next test expects."""
    offline_marker_off(ctx)
    offset = restart_client(ctx, who=" with the machine, no hunt", hunt=False)
    try:
        got = wait_log(ctx, offset, [NOHUNT_MARK], 60)
        ctx.check(got[NOHUNT_MARK], "the real client did not come up without the hunt within 60 s")
    finally:
        offline_marker_off(ctx)
    session = wait_session(ctx, offset)
    ctx.check(session_established(session), "the real client did not establish its session after the emulator")
    ctx.check(wait_quiet(ctx, offset), "the service was still moving the head %d s after the real "
              "client came back", QUIET_TIMEOUT_S)
    ctx.log("the machine is back under the real client, no hunt")


def enter_offline(ctx):
    """Cloud mode with the OFFLINE service: the machine driven from the
    local socket, no web session. A running offline client is reused;
    otherwise the marker is set and the client started fresh (restarted
    in cloud mode, switched to from GRBL mode, the change declared) and
    its listener waited for. The marker is taken down again at once: it
    only ever applies to that one start. Returns the log offset where the
    test's own window begins."""
    fc = ctx.forgectrl
    st, m = fc.get("/mode")
    ctx.check(st == 200 and isinstance(m, dict), "GET /mode -> %s", st)
    if m.get("mode") == "cloud" and m.get("controller") == "running" and client_offline(m.get("pid")):
        ctx.log("the offline service is already up (pid %s) - reusing it", m.get("pid"))
        return log_size(GFCLOUD_LOG)
    offset = restart_client(ctx, marker=OFFLINE_MARKER, who=" offline")
    try:
        got = wait_log(ctx, offset, [OFFLINE_MARK], 60)
        ctx.check(got[OFFLINE_MARK], "the client did not come up as the offline service within 60 s")
        ctx.log("offline service up: %s", message(got[OFFLINE_MARK]))
    finally:
        offline_marker_off(ctx)
    ctx.check(wait_quiet(ctx, offset), "the machine was not quiet %d s after the offline start", QUIET_TIMEOUT_S)
    return log_size(GFCLOUD_LOG)


class Offline:
    """The socket the offline service takes actions on: send the
    service's messages, read the machine's events."""

    def __init__(self, path=None):
        self.path = path or OFFLINE_SOCKET
        self.sock = None
        self.buf = b""
        self.events = []

    def __enter__(self):
        self.sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.sock.settimeout(5)
        self.sock.connect(self.path)
        self.sock.settimeout(0.05)
        return self

    def __exit__(self, *a):
        if self.sock:
            try:
                self.sock.close()
            except OSError:
                pass
        return False

    def send(self, msg):
        self.sock.sendall((json.dumps(msg) + "\n").encode())

    def poll(self):
        """Collect whatever events have arrived; returns the new ones."""
        new = []
        try:
            while True:
                d = self.sock.recv(65536)
                if not d:
                    break
                self.buf += d
        except (socket.timeout, OSError):
            pass
        while b"\n" in self.buf:
            line, self.buf = self.buf.split(b"\n", 1)
            if line.strip():
                try:
                    ev = json.loads(line.decode("utf-8", "replace"))
                except ValueError:
                    ev = {"raw": line.decode("utf-8", "replace")}
                self.events.append(ev)
                new.append(ev)
        return new

    current = None                  # the print last handed over (end_print cancels it)

    def print_ready(self, action_id, path, settings=None):
        self.current = action_id
        self.send({"id": action_id, "action_type": "print", "status": "ready",
                   "motion_url": "file://" + path, "settings": settings or {}})

    def cancel(self, action_id, action_type="print"):
        self.send({"id": action_id, "action_type": action_type, "status": "cancelled"})


def offline_events(off):
    """Every event the offline service has handed back so far, by name."""
    off.poll()
    return [e.get("event") for e in off.events if e.get("event")]


def offline_cleanup(ctx):
    """The jobs the offline tests wrote are gone when the test is (tmpfs
    would lose them at reboot; the bench rule is sooner)."""
    import shutil
    if os.path.isdir(JOB_DIR):
        shutil.rmtree(JOB_DIR, ignore_errors=True)
        ctx.log("offline jobs removed from %s", JOB_DIR)


def offline_job(ctx, name, **kw):
    """Write a synthesized, laser-never-commanded job for the offline
    service and return its path (under JOB_DIR, tmpfs)."""
    os.makedirs(JOB_DIR, exist_ok=True)
    path = os.path.join(JOB_DIR, name)
    n, seconds, compressed = puls.write_job(path, **kw)
    ctx.log("job %s: %d payload bytes, %.0f s at STfr %d%s", name, n, seconds,
            kw.get("step_hz", 10000), " (gzip)" if compressed else "")
    ctx.evidence.setdefault("jobs", {})[name] = {"payload_bytes": n, "seconds": round(seconds, 1),
                                                  "compressed": compressed}
    return path


def offline_start_print(ctx, off, action_id, path, offset, settings=None):
    """Hand the offline service a print and take it to its run: the
    client loads the job, lights the button and waits; the operator's
    press arms it and the run starts. Returns the 'starting run' line."""
    off.print_ready(action_id, path, settings)
    wait_button_wait(ctx, offset, 120)
    ctx.act("button", "press", text="The button is lit white: the press arms the print and the "
            "head starts to move. Nothing fires: the job commands no laser.",
            until=lambda: wait_print_running(ctx, offset, 0.1) is not None, timeout=180)
    got = wait_print_running(ctx, offset, 10)
    ctx.check(got, "the print did not start after the press")
    return got


def settle_cloud(ctx, offset):
    """End of a cloud test: the service's follow-up moves (a hunt after a
    print, the re-hunt after a lid close) done and the machine idle."""
    ctx.check(wait_quiet(ctx, offset), "the service was still moving the head %d s after the test",
              QUIET_TIMEOUT_S)
    ctx.log("cloud mode stays up; the machine is quiet")


def wait_print_running(ctx, offset, timeout):
    """The PRINT is running: a "starting run" line after the print's button
    wait (the connect-time hunt and the service's moves before it are runs
    too, and must not be mistaken for the print). Returns the line or None."""
    t0 = time.monotonic()
    while time.monotonic() - t0 < timeout:
        ctx.checkpoint()
        lines = log_lines_since(GFCLOUD_LOG, offset)
        wait_i = next((i for i, ln in enumerate(lines) if "waiting for button" in ln), None)
        if wait_i is not None:
            run = next((ln for ln in lines[wait_i:] if "starting run" in ln), None)
            if run:
                return run
        time.sleep(0.5)
    return None


def latch_locked():
    ilk = hw.sysfs_int("cnc/interlock_circuit")
    return ilk is not None and bool(ilk & (1 << 3))


# ---------------------------------------------------------------- emission witnesses

# cnc/laser_on_sampled is the kernel's own count of LASER_ON over a window of
# about one second (255 samples at 255 Hz), latched once a window: read at
# least twice a window, no window goes unread; and the window under way when
# a print's body ends latches up to a window later, so the reads go on for a
# window and a poll after it.
LASER_ON_WINDOW_S = 1.0


def emission_poll_s():
    return 0.4 * LASER_ON_WINDOW_S


def emission_tail_s():
    return LASER_ON_WINDOW_S + emission_poll_s()


def emission_hits(samples):
    """The samples of a dark print that show LASER_ON: (t, laser_on_sampled)
    pairs whose count is not a clean 0 (a count, or a read that failed)."""
    return [s for s in samples if s[1] != "0"]


class EmissionWatch:
    """The kernel's LASER_ON count read through a dark print, from before
    its arm press to past its end. The print's chain is armed (the press
    cleared the button latch, the client unlocked the latch), so any FIRE
    byte the job or the client put in play would be LASER_ON. The kernel
    samples LASER_ON 255 times a second and counts the samples that read it
    active, so a LASER_ON longer than one sample period (about 3.9 ms) is
    counted and a shorter one can fall between two samples. The count is
    read at least twice a window through the body, then for a window and a
    poll after it (emission_tail_s), and once more at the end, so the
    window under way when the body ends is read after it latches. check()
    fails the test on any count."""

    def __init__(self, ctx, tag):
        self.ctx = ctx
        self.tag = tag
        self.samples = []
        self._stop = threading.Event()
        self._th = None

    def _sample(self):
        self.samples.append((round(time.monotonic() - self._t0, 1), hw.sysfs_read("cnc/laser_on_sampled")))

    def __enter__(self):
        self._t0 = time.monotonic()
        self._sample()                          # the first read before anything is asked of the print

        def loop():
            while not self._stop.wait(emission_poll_s()):
                self._sample()
        self._th = threading.Thread(target=loop, daemon=True, name="forgetest-emission-watch")
        self._th.start()
        return self

    def __exit__(self, *exc):
        time.sleep(emission_tail_s())           # the window under way at the body's end latches
        self._stop.set()
        if self._th is not None:
            self._th.join(5)
        self._sample()                          # and the last, the print's whole span covered
        return False

    def check(self):
        hits = emission_hits(self.samples)
        self.ctx.evidence[self.tag + "_emission"] = {"samples": len(self.samples), "hits": hits[:10]}
        self.ctx.log("[%s] LASER_ON count read %d times through the dark print: %d not zero",
                     self.tag, len(self.samples), len(hits))
        self.ctx.check(self.samples, "[%s] the LASER_ON count was never read", self.tag)
        self.ctx.check(not hits, "[%s] LASER_ON counted during a dark print: %s", self.tag, hits[:5])


# A window counted after the pause's lock began after it: the count latches
# once a window of about a second, and the window under way at the lock is
# visible until the next one latches.
PAUSE_COUNT_SETTLE_S = 2.0


def pause_dark_why(samples):
    """None when a lit print's pause stayed dark from the lock that began it
    to the resume press, else why not. samples: (t, interlock_circuit,
    laser_enable, laser_on, laser_on_sampled) read from the pause press's
    line on. The judgment starts at the first sample that reads the latch
    locked (the pause locks it before its stop and its backward run, and
    unlocks it for the resume press): from there FIRE and LASER_ON read 0
    in every sample, and the LASER_ON count reads 0 in every window that
    began after the lock."""
    start = next((i for i, s in enumerate(samples) if s[1] is not None and s[1] & (1 << 3)), None)
    if start is None:
        return "the pause's lock was never read (%d samples)" % len(samples)
    t_lock = samples[start][0]
    lit = [s for s in samples[start:] if s[2] != "0" or s[3] != "0"]
    if lit:
        return "FIRE or LASER_ON read during the pause: %s" % lit[:3]
    counted = [s for s in samples[start:] if s[0] - t_lock >= PAUSE_COUNT_SETTLE_S and s[4] != "0"]
    if counted:
        return "LASER_ON counted during the pause: %s" % counted[:3]
    return None


class PauseWatch:
    """The tight FIRE readback of a lit print's pause, from the pause press
    to the moment before the resume press is asked for."""

    def __init__(self):
        self.samples = []
        self._stop = threading.Event()
        self._th = None

    def __enter__(self):
        t0 = time.monotonic()

        def loop():
            while not self._stop.is_set():
                self.samples.append((round(time.monotonic() - t0, 4), hw.sysfs_int("cnc/interlock_circuit"),
                                     hw.sysfs_read("cnc/laser_enable"), hw.sysfs_read("cnc/laser_on"),
                                     hw.sysfs_read("cnc/laser_on_sampled")))
                self._stop.wait(0.002)
        self._th = threading.Thread(target=loop, daemon=True, name="forgetest-pause-watch")
        self._th.start()
        return self

    def __exit__(self, *exc):
        self._stop.set()
        if self._th is not None:
            self._th.join(5)
        return False


# A print a test leaves behind: the bound on its cancel's park and relock,
# and on the controller's stop after it.
PARK_END_S = 120
STOP_END_S = 30


def job_alive(fc):
    """A job is still on the machine: the latch unlocked, the kernel
    playing, or the cooling engine's window armed."""
    if not latch_locked() or hw.sysfs_read("cnc/state") == "running":
        return True
    try:
        st, cs = fc.get("/cool/status")
    except hw.HwError:
        return True
    return not (st == 200 and isinstance(cs, dict)) or bool(cs.get("armed"))


def _until(cond, timeout):
    """Poll cond until it holds or the timeout: not abort-aware, since it
    runs after an Abort, which it must not cut short."""
    t0 = time.monotonic()
    while time.monotonic() - t0 < timeout:
        if cond():
            return True
        time.sleep(0.5)
    return False


def end_print(ctx, off=None):
    """A print the test leaves on a failure, an error or an Abort is ended
    before the failure is reported. The offline service's print is canceled
    as the app cancels one: a stop and a park, never a button press, which
    during a pause resumes the print. The app's print, or one the cancel did
    not end, ends with the controller's stop, and the controller is left
    stopped: the hand-back starts it again without the service's hunt. Then
    the relock and the end of motion are waited for, bounded."""
    fc = ctx.forgectrl
    if not job_alive(fc):
        return
    action = getattr(off, "current", None)
    if action is not None:
        try:
            off.cancel(action)
            ctx.log("the print left running is canceled (action %s)", action)
        except OSError as e:
            ctx.log("the cancel of action %s could not be sent: %s", action, e)
        if _until(lambda: not job_alive(fc), PARK_END_S):
            ctx.log("the canceled print parked and the latch is locked")
            return
    try:
        st = fc.post("/controller/stop")[0]
    except hw.HwError as e:
        st = str(e)
    ended = _until(lambda: not job_alive(fc), STOP_END_S)
    ctx.log("the print left running is ended by the controller's stop (%s): %s",
            st, "ended" if ended else "STILL RUNNING")


APP_PRINT_CUE = ("In the Glowforge app: scrap on the bed, lid closed, a SMALL engrave or score job "
                 "(about 30 s) set up. Click Done here, then press Print in the app and press the "
                 "physical button when it lights white.")
OFFLINE_STEP = ("The machine in cloud mode under the OFFLINE service (the test starts it: no "
                "account, no network, no job from the app; the job is a square the laser never "
                "fires on). Nothing on the bed is needed. The client is left offline in cloud "
                "mode; the next test that needs the service, a mode switch, or a controller "
                "restart brings the service back.")
ARM_STEP = ("Press the physical button when it lights white (the arm) - once per print. The "
            "latch is unlocked from then to the end of the run, so the test is a live one even "
            "though the job carries no laser command.")

CANCELLED = 'finished with event ":cancelled"'
COMPLETED = 'finished with event ":completed"'
# The run loop's own lines for a button pause and the resume that follows:
# the press, the settled hold, the second press, and the retraced restart
# with its laser lead (logged by _resume_retraced, on its own line). A
# resume the kernel refuses logs "resume refused" instead and cancels.
PAUSE_LINES = ("button pressed mid-run; pausing", "paused at")
RESUME_LINES = ("button pressed while paused", "resuming (laser lead")
RESUME_REFUSED = "resume refused"
# A button press during a print is seen through the run loop's own line;
# the judgment of what it did (check_pause_resume) follows the wait.
PRESS_TIMEOUT_S = 90
# A pause's stop and backward run, from the press to "paused at".
PAUSE_SETTLE_S = 30
CLOUD_STEP = ("Cloud credentials configured; the machine in cloud mode (the test switches once from "
              "GRBL mode and stays in cloud mode; switch back on the panel when done).")
LID_STEP = "Lid and interlock steps are standing instructions: the test watches the switches."


def judge_abort_tail(ctx, ev, offset, tag, fin):
    """The tail every cloud abort shares: the park ran to completion, the head
    is back at the job start by the KERNEL counters (cloud clears them at every
    job start, so a completed park reads back at zero - stale ring bytes played
    ahead of it would not), the print ended ':cancelled', the latch is locked
    and the armed window closed."""
    ctx.check(ctx.forgectrl.wait_idle(15, abort=ctx.aborted), "[%s] machine not idle after the park", tag)
    kpos = read_position()
    ev[tag + "_counters_after_park"] = kpos
    ctx.log("[%s] kernel counters after the park: %s", tag, kpos)
    ctx.check(kpos is not None and abs(kpos[0]) <= 3 and abs(kpos[1]) <= 3,
              "[%s] the head did not come back to the job start (kernel counters %s)", tag, kpos)
    ctx.check(fin and CANCELLED in fin, "[%s] the print did not end ':cancelled': %s", tag,
              message(fin) or "no finish line")
    st, cs = ctx.forgectrl.get("/cool/status")
    ev[tag + "_armed_after"] = cs.get("armed") if isinstance(cs, dict) else None
    ev[tag + "_latch_locked"] = latch_locked()
    ctx.check(not ev[tag + "_armed_after"], "[%s] armed window still open after the abort", tag)
    ctx.check(ev[tag + "_latch_locked"], "[%s] kernel latch not locked after the abort", tag)


@test("cloud.lid-interlock-abort", title="Lid and interlock each abort a cloud print; the park ignores "
                                         "an open lid",
      subsystem="cloud", kind="live", est_min=11,
      covers=_MACHINE_RUN,
      requires=["laser.emission-witness"], actions=["button", "lid", "interlock"],
      steps=[OFFLINE_STEP, ARM_STEP, LID_STEP,
             "The head needs 40 mm of free +X and +Y travel. The test runs TWO prints.",
             "Be able to open the remote-interlock loop for the second print: unplug the Pro's "
             "interlock plug, or pull the jumper at J8 on a Basic/Plus. Restore it at the end.",
             "Print 1: press the button to start, open the lid when told. Print 2: press the "
             "button to start, open the interlock when told, then open the lid as well while "
             "the head is parking."],
      description="Both enclosure triggers, on one setup, behaving as the factory's do. The lid: the "
                  "edge reaches the controlled stop within milliseconds, the head returns home at "
                  "once with the lid still open, the latch relocks and the armed window closes, and "
                  "the job ends ':cancelled'. The remote-interlock loop: the same tail, and opening "
                  "the lid while THAT park is running does not interrupt it - the park is deliberately "
                  "immune, so it always reports complete.")
def lid_interlock_abort(ctx):
    ev = ctx.evidence
    sw = (ctx.forgectrl.status().get("switches") or {})
    ctx.check(sw.get("interlock_ok"), "the interlock loop already reads open - close it before this test")
    offset = enter_offline(ctx)
    job = offline_job(ctx, "abort.puls", seconds=40)
    off = Offline().__enter__()
    try:
        with EmissionWatch(ctx, "prints") as watch:
            lid_interlock_abort_body(ctx, ev, off, job, offset)
        watch.check()
    except BaseException:
        end_print(ctx, off)
        raise
    finally:
        off.__exit__(None, None, None)
        offline_cleanup(ctx)


def lid_interlock_abort_body(ctx, ev, off, job, offset):
    # -- print 1: the lid ----------------------------------------------------
    offline_start_print(ctx, off, 9001, job, offset)
    ctx.act("lid", "open", text="The head is moving: leave the lid open until the head has returned "
            "to the corner.", timeout=60)
    needles = ["lid opened", "lid opened mid-run; stopping motion", "start return home",
               "return home complete"]
    got = wait_log(ctx, offset, needles, 90)
    fin = wait_action_finished(ctx, offset, "print", 60)
    got["print finished"] = fin
    ev["lid_log"] = {k: message(v) for k, v in got.items()}
    for k, v in got.items():
        ctx.log("  [lid] %s: %s", k, "seen" if v else "MISSING")
    ctx.check(got["lid opened mid-run; stopping motion"], "the lid open did not stop the run")
    # The lid edge that stopped the run is the LAST "lid opened" edge line
    # before the stop line (an earlier open, e.g. to place the scrap, is
    # not the one).
    lines = log_lines_since(GFCLOUD_LOG, offset)
    stop_i = next((i for i, ln in enumerate(lines) if "lid opened mid-run; stopping motion" in ln), None)
    edge_line = None
    if stop_i is not None:
        edge_line = next((ln for ln in reversed(lines[:stop_i])
                          if "_switch_event lid opened" in ln or ln.rstrip().endswith(" lid opened")), None)
    t_edge = line_time(edge_line) if edge_line else None
    t_stop = line_time(lines[stop_i]) if stop_i is not None else None
    ctx.check(t_edge is not None and t_stop is not None,
              "the lid edge and the stop it drove are not both on record with a time (edge %r, stop %r): "
              "the edge-to-stop time cannot be judged", message(edge_line),
              message(lines[stop_i]) if stop_i is not None else None)
    ev["edge_to_stop_ms"] = round((t_stop - t_edge) * 1000, 1)
    ctx.log("lid edge -> stop: %s ms", ev["edge_to_stop_ms"])
    ctx.check(ev["edge_to_stop_ms"] < 60, "stop was not edge-driven (%s ms after the lid edge)",
              ev["edge_to_stop_ms"])
    ctx.check(got["start return home"] and got["return home complete"],
              "the park did not run to completion with the lid open")
    judge_abort_tail(ctx, ev, offset, "lid", fin)
    ctx.act("lid", "close")
    settle_cloud(ctx, offset)
    ev["events_print1"] = offline_events(off)

    # -- print 2: the interlock, with the lid opened during the park ---------
    offset = log_size(GFCLOUD_LOG)
    offline_start_print(ctx, off, 9002, job, offset)
    ctx.act("interlock", "open", text="The head is moving.", timeout=60)
    sw = (ctx.forgectrl.status().get("switches") or {})
    ev["interlock_ok_after_pull"] = sw.get("interlock_ok")
    ctx.check(sw.get("interlock_ok") is False,
              "the interlock still reads closed - the loop was not opened (switches: %s)", sw)
    stop_line = "interlock opened mid-run; stopping motion"
    got = wait_log(ctx, offset, [stop_line, "start return home"], 90)
    ctx.check(got[stop_line], "the interlock open did not stop the run")
    ctx.check(got["start return home"], "the abort did not start the return home")
    ctx.act("lid", "open", text="The head is on its way back: leave both open until it has stopped.",
            timeout=60)
    done = wait_log(ctx, offset, ["return home complete"], 90)
    fin = wait_action_finished(ctx, offset, "print", 60)
    ev["interlock_log"] = {stop_line: message(got[stop_line]),
                           "start return home": message(got["start return home"]),
                           "return home complete": message(done["return home complete"]),
                           "print finished": message(fin)}
    for k, v in ev["interlock_log"].items():
        ctx.log("  [interlock] %s: %s", k, "seen" if v else "MISSING")
    ctx.check(done["return home complete"],
              "the park did not run to completion with the lid opened during it")
    sw = (ctx.forgectrl.status().get("switches") or {})
    ev["switches_at_return"] = {"lid": sw.get("lid"), "interlock_ok": sw.get("interlock_ok")}
    ctx.check(sw.get("lid") is False,
              "the lid was not open at the end of the park - the park's immunity was not exercised")
    judge_abort_tail(ctx, ev, offset, "interlock", fin)
    ctx.act("lid", "close")
    ctx.act("interlock", "close")
    sw = (ctx.forgectrl.status().get("switches") or {})
    ev["restored"] = {"lid": sw.get("lid"), "interlock_ok": sw.get("interlock_ok")}
    ctx.check(sw.get("interlock_ok"), "the interlock loop is still open - restore it before continuing")
    settle_cloud(ctx, offset)
    ev["events_print2"] = offline_events(off)
    ctx.log("PASS: lid open -> stop in %s ms and park with the lid open; interlock open -> the same "
            "tail with the park running through a lid edge; both prints ':cancelled'",
            ev.get("edge_to_stop_ms"))


# ---------------------------------------------------------------- the dark print (exthost.armed-freeze)

def dark_print_body(ctx, ev, off, job, offset, fc):
    """The dark print exthost.armed-freeze runs, with the kernel's LASER_ON
    count read from before its arm press to its end."""
    with EmissionWatch(ctx, "dark print") as watch:
        _dark_print(ctx, ev, off, job, offset, fc)
    watch.check()


def _dark_print(ctx, ev, off, job, offset, fc):
    # Before the print the latch is locked. At the button wait the armed
    # session is open, the cooling verdict has passed and the client has
    # released the lock: the hardware's own button latch clears only on a
    # press taken with the lock released, so a lock still held here means a
    # press that could not count.
    ev["latch_locked_before"] = latch_locked()
    ctx.check(ev["latch_locked_before"], "the latch is unlocked before the print")
    off.print_ready(9005, job)
    wait_button_wait(ctx, offset, 120)
    ev["latch_locked_at_button"] = latch_locked()
    ctx.check(ev["latch_locked_at_button"] is False,
              "the latch is locked at the button wait: the press could not count")
    ctx.act("button", "press", text="The button is lit: press it. The print runs dark and finishes.",
            until=lambda: wait_print_running(ctx, offset, 0.1) is not None, timeout=PRESS_TIMEOUT_S)
    run = wait_print_running(ctx, offset, 60)
    ctx.check(run is not None, "the print did not start after the press")
    ctx.sleep(1.0)
    ev["latch_locked_in_run"] = latch_locked()
    ctx.check(ev["latch_locked_in_run"] is False, "the latch is locked during the run")
    fin = wait_action_finished(ctx, offset, "print", 180)
    ev["print finished"] = message(fin)
    ctx.check(fin and COMPLETED in fin, "the print did not complete: %s", message(fin) or "no finish line")
    st, cs = fc.get("/cool/status")
    ev["armed_after"] = cs.get("armed") if isinstance(cs, dict) else None
    ctx.check(not ev["armed_after"], "armed window still open after the print")
    ev["latch_locked_after"] = latch_locked()
    ctx.check(ev["latch_locked_after"], "the latch is unlocked after the print")
    ev["events"] = offline_events(off)
    ctx.log("PASS: locked before the print, unlocked at the button and through the run, locked after it; "
            "the print completed")


@test("cloud.pause-resume", title="Button pauses and resumes a cloud print (factory backtrack + lead)",
      subsystem="cloud", kind="live", est_min=8,
      # the latch read back while paused is the kernel's lock line, held
      # while any of its holders holds, wherever the module keeps them; the
      # warm-up and the rest are the cooling engine's phases
      covers=_CLOUD_ALL + [("forgectrl", "src/cool.*"), ("kernel-module-glowforge", "**")],
      requires=["laser.emission-witness"], actions=["button"],
      steps=[CLOUD_STEP, "The app open; scrap on the bed and a small engrave/score job (about 60 s) ready.",
             "Print from the app and press the button when it lights; a few seconds into the run "
             "the test asks for a press (pause) and, once the head has stopped, another (resume); "
             "let the job finish and confirm it in the app."],
      description="Pressing the button during a cloud print pauses it the factory way - controlled "
                  "stop, backtrack with the laser off, print:paused - and the next press resumes "
                  "with the laser-off lead, print:resumed; the job then completes and parks. The "
                  "latch is locked for the stop and the backward run and unlocked again once the "
                  "pause has settled: read from sysfs between the two presses, it is unlocked while "
                  "the print waits paused (the cooling engine's armed window is recorded beside it), "
                  "and the resume press comes after that read. From the pause press until the resume "
                  "press is asked for, the FIRE line, LASER_ON and the kernel's LASER_ON count are read "
                  "tight: from the pause's lock on, the stop, the backward run and the wait read dark. "
                  "The same "
                  "print is where the job's lifecycle shows: a print warms up before its first "
                  "fire and rests after its park, both non-zero, while the connect-time hunt in "
                  "the same session does neither. It is also where the app's progress bar shows: "
                  "the run reports how far it has gotten, against the job's own length.")
def pause_resume(ctx):
    ev = ctx.evidence
    offset = enter_cloud(ctx)
    try:
        pause_resume_body(ctx, ev, offset)
    except BaseException:
        end_print(ctx)
        raise


def pause_resume_body(ctx, ev, offset):
    fc_offset = log_size(FORGECTRL_LOG)
    ctx.instruct(APP_PRINT_CUE)
    got = wait_print_running(ctx, offset, 300)
    ctx.check(got, "the print never reached its run within 300 s (not started, or the button not pressed)")
    # The pause locks the latch for its stop and its backward run, and the
    # client logs "paused at" once it has unlocked it again for the resume
    # press. Read here, between the two presses, from sysfs: a relock by any
    # writer (the client, forgectrl's safing, the engine on a lapsed report)
    # shows, not only one the client logs. The engine's armed window is
    # forgectrl's own bookkeeping, recorded beside it. The FIRE line and
    # the LASER_ON count are read tight from before the pause press to just
    # before the resume press is asked for: the pause is dark from its lock
    # on (pause_dark_why); the lit cut before the lock is not judged.
    with PauseWatch() as pw:
        ctx.act("button", "press", text="The head is moving: the press pauses the print.",
                until=lambda: log_has(offset, PAUSE_LINES[0]), timeout=PRESS_TIMEOUT_S, fail=False,
                press="pause")
        paused = wait_log(ctx, offset, [PAUSE_LINES[1]], PAUSE_SETTLE_S)[PAUSE_LINES[1]]
        ctx.check(paused, "the pause did not settle within %d s (no 'paused at')", PAUSE_SETTLE_S)
        ilk = hw.sysfs_int("cnc/interlock_circuit")
        st, cs = ctx.forgectrl.get("/cool/status")
        ev["while_paused"] = {"interlock_circuit": ilk,
                              "armed": cs.get("armed") if st == 200 and isinstance(cs, dict) else None}
        ctx.log("while paused: %s", ev["while_paused"])
        ctx.check(ilk is not None and not ilk & (1 << 3),
                  "the latch is not unlocked while the print waits paused (interlock_circuit %s): the resume "
                  "press could not count", ilk)
    dark = pause_dark_why(pw.samples)
    ev["pause_witness"] = {"samples": len(pw.samples),
                           "span_s": pw.samples[-1][0] if pw.samples else None, "why": dark}
    ctx.log("pause witness: %d samples over %s s: %s", len(pw.samples), ev["pause_witness"]["span_s"],
            dark or "dark from the lock on")
    ctx.check(dark is None, "the pause was not dark: %s", dark)
    ctx.act("button", "press", text="The print is paused (the head backed up a few millimeters): "
            "the press resumes it.",
            until=lambda: log_has(offset, RESUME_LINES[0]) or log_has(offset, RESUME_REFUSED),
            timeout=PRESS_TIMEOUT_S, fail=False)
    got = wait_log(ctx, offset, list(PAUSE_LINES + RESUME_LINES), 10)
    ev["log"] = {k: bool(v) for k, v in got.items()}
    check_pause_resume(ctx, got, offset)
    st, cs = ctx.forgectrl.get("/cool/status")
    ev["armed_after_resume"] = cs.get("armed") if isinstance(cs, dict) else None
    ctx.log("armed after the resume: %s", ev["armed_after_resume"])
    fin = wait_action_finished(ctx, offset, "print", 300)
    got = wait_log(ctx, offset, ["return home complete"], 5)
    ev["log_end"] = {"return home complete": bool(got["return home complete"]),
                     "print finished": message(fin)}
    ctx.check(fin, "the print did not finish within 300 s of the resume")
    ctx.check(COMPLETED in fin, "the print did not complete after the resume: %s", message(fin))
    ctx.check(got["return home complete"], "the post-print park did not complete")
    relocked = [ln for ln in window_lines(ctx, GFCLOUD_LOG, offset, "that the pause neither relocked nor "
                                          "canceled the job")
                if "relocking the laser" in ln or ("print [" in ln and CANCELLED in ln)]
    ev["relock_or_cancel_lines"] = len(relocked)
    ctx.check(not relocked, "the pause relocked or canceled the job (%s)", relocked[:2])

    # The job's lifecycle, from the same print: a warm-up before the first
    # fire and a rest after the park are equipment protection the service
    # assumes has happened, and a machine configured to skip them says so in
    # the log rather than quietly not doing them.
    lines = log_lines_since(GFCLOUD_LOG, offset)
    holds = {"warm up": None, "cool down": None}
    for ln in lines:
        for phase in holds:
            if holds[phase] is None and ("%s: holding" % phase) in ln:
                holds[phase] = ln.strip()[:160]
            if holds[phase] is None and ("%s: skipped" % phase) in ln:
                holds[phase] = ln.strip()[:160]
    ev["lifecycle"] = holds
    ctx.log("warm-up: %s", holds["warm up"])
    ctx.log("rest: %s", holds["cool down"])
    for phase in ("warm up", "cool down"):
        ctx.check(holds[phase] is not None, "the print logged no %s at all", phase)
        ctx.check(holds[phase] and "holding" in holds[phase],
                  "the print skipped its %s (%s): the config still carries a zero",
                  phase, holds[phase])
    # A hunt is not a print: the connect-time hunt ran in this same session
    # and must not have held for either period.
    hunt_end = next((i for i, ln in enumerate(lines) if "waiting for button" in ln), len(lines))
    early_holds = [ln.strip()[:120] for ln in lines[:hunt_end]
                   if "warm up: holding" in ln or "cool down: holding" in ln]
    ev["holds_before_the_print"] = early_holds
    ctx.check(not early_holds, "a hunt or motion held for a print's periods: %s", early_holds[:2])

    # The app's progress bar, from the same print. The machine reports where
    # it has gotten to every 30 s and at every phase change, against the
    # job's own length, which it names once when the run starts.
    declared = progress_denominator(lines)
    ev["progress_total"] = declared
    ctx.log("progress reported against %s bytes", declared)
    ctx.check(declared is not None, "the print reported no progress at all")
    ctx.check(declared and declared > 0, "the print reported progress against %s bytes", declared)
    ctx.confirm("In the app: did the print's progress advance while it cut (not standing still, "
                "not jumping straight to nearly finished), and does the app show it complete?")

    # The job's envelope, from the same print: the client derives the
    # limits the header carries (a cut job carries the coolant window and
    # the air-assist tach maximum) and hands them to the engine with every
    # report, and the engine names the effective set it is running on,
    # with the header's ceiling beside its own.
    # The session's hunts and motions carry their own (looser) windows; the
    # line that matters is the print's, the first after its action request.
    print_at = max((i for i, ln in enumerate(lines) if "service action request: print" in ln), default=-1)
    limits = next((ln.split(LIMITS_MARK, 1)[1].strip() for ln in lines[print_at + 1:] if LIMITS_MARK in ln), None)
    ev["header_limits"] = limits
    ctx.log("job limits from the header (the print's): %s", limits)
    ctx.check(limits is not None, "the client named no job limits from the header")
    ctx.check("coolant_max_c=" in limits, "the header's coolant ceiling did not reach the engine: %s", limits)
    eff = [ln.strip()[:200] for ln in log_lines_since(FORGECTRL_LOG, fc_offset) if EFFECTIVE_MARK in ln]
    with_header = [ln for ln in eff if "header " in ln and "header none" not in ln]
    ev["effective_limits"] = with_header[-1:] + eff[-1:]
    ctx.log("engine effective limits: %s", with_header[-1] if with_header else None)
    ctx.check(with_header,
              "the engine never resolved an effective ceiling against the header's: %s", eff[-2:])

    settle_cloud(ctx, offset)
    ctx.log("PASS: button pause/resume mid-print, warm-up and rest observed, progress reported, "
            "the job's limits passed through, job completed and parked")


@test("cloud.oversize-stream", title="A print longer than the ring is fed while it plays",
      subsystem="cloud", kind="live", est_min=12,
      covers=_MACHINE_RUN,
      requires=["cloud.lid-interlock-abort", "cloud.pause-resume"], actions=["button"],
      steps=[OFFLINE_STEP, ARM_STEP,
             "The head needs 40 mm of free +X and +Y travel. The job is an hour of squares, "
             "longer than the ring holds; the test lets it run for about two minutes, asks for a "
             "press (pause) and another (resume), then cancels it the way the app would."],
      description="The service sends one pulse file for a print however long it is, and a long one "
                  "is several times the ring: the machine holds the job in memory, fills the ring, "
                  "starts, and tops the ring up as it drains. This checks the signature of that - "
                  "the device in live-feed mode, the kernel's program total growing during the run, "
                  "and no underrun - that progress is reported against the job's length rather "
                  "than that growing total, that a live-fed print pauses and resumes the factory "
                  "way, the ring having kept the history to back into, and that the job cancels "
                  "cleanly.")
def oversize_stream(ctx):
    ev = ctx.evidence
    offset = enter_offline(ctx)
    job = offline_job(ctx, "long.puls", seconds=3500)
    before = hw.sysfs_int("cnc/underruns", 0)
    ev["underruns_before"] = before
    off = Offline().__enter__()
    try:
        with EmissionWatch(ctx, "print") as watch:
            oversize_stream_body(ctx, ev, off, job, offset, before)
        watch.check()
    except BaseException:
        end_print(ctx, off)
        raise
    finally:
        off.__exit__(None, None, None)
        offline_cleanup(ctx)


def oversize_stream_body(ctx, ev, off, job, offset, before):
    offline_start_print(ctx, off, 9004, job, offset)

    # The load says so in as many words, and the device is in live-feed mode.
    got = wait_log(ctx, offset, ["job is longer than the ring"], 30)
    ev["log"] = {k: bool(v) for k, v in got.items()}
    ctx.check(got["job is longer than the ring"],
              "the job fit the ring: pick a longer one, this test needs a job the ring cannot hold")
    streaming = hw.sysfs_int("cnc/streaming", 0)
    ev["streaming"] = streaming
    ctx.check(streaming == 1, "cnc/streaming is %s during a live-fed run", streaming)

    # The proof the feed is live: the kernel's idea of how long the program is
    # keeps growing while it plays.
    first = read_program_total()
    ctx.log("program total at the start of the run: %s bytes", first)
    ctx.notice("Cutting: the test watches the ring being topped up for two minutes. Do nothing yet.")
    try:
        ctx.sleep(120)
    finally:
        ctx.clear_notice()
    grown = read_program_total()
    ev["total_first"], ev["total_later"] = first, grown
    ctx.log("program total two minutes in: %s bytes (+%s)", grown, (grown or 0) - (first or 0))
    ctx.check(first is not None and grown is not None and grown > first,
              "the program total did not grow during the run (%s -> %s): the ring was not being "
              "topped up", first, grown)
    after = hw.sysfs_int("cnc/underruns", 0)
    ev["underruns_during"] = after
    ctx.check(after == before, "the ring ran dry during the run (underruns %s -> %s)", before, after)

    # Progress on a live feed is where a moving denominator would show. The
    # kernel's program total is what just grew, and a bar divided by it would
    # sit near full from the first frame to the last; the job's own length is
    # what the report divides by, and on a job this long it is the larger
    # number by a wide margin.
    declared = progress_denominator(log_lines_since(GFCLOUD_LOG, offset))
    ev["progress_total"] = declared
    ctx.log("progress reported against %s bytes, kernel program total %s", declared, grown)
    ctx.check(declared is not None, "the print reported no progress at all")
    ctx.check(declared and first is not None and declared > first,
              "progress is being reported against %s bytes, which is the ring's count (%s), not "
              "the job", declared, first)

    # The pause on a live feed: the ring keeps the history to back into, so a
    # streamed print retraces and leads back on exactly like a preloaded one.
    ev["max_backtrack"] = hw.sysfs_int("cnc/max_backtrack", 0)
    ctx.log("max_backtrack while the feed runs: %s steps", ev["max_backtrack"])
    ctx.act("button", "press", text="The press pauses the live-fed print.",
            until=lambda: log_has(offset, PAUSE_LINES[0]), timeout=PRESS_TIMEOUT_S, fail=False,
            press="pause")
    ctx.act("button", "press", text="The print is paused (the head backed up a few millimeters): "
            "the press resumes it.",
            until=lambda: log_has(offset, RESUME_LINES[0]) or log_has(offset, RESUME_REFUSED),
            timeout=PRESS_TIMEOUT_S, fail=False)
    got = wait_log(ctx, offset, list(PAUSE_LINES + RESUME_LINES), 10)
    ev["pause_log"] = {k: bool(v) for k, v in got.items()}
    check_pause_resume(ctx, got, offset, what="the live-fed run")
    backtracked = [ln for ln in window_lines(ctx, GFCLOUD_LOG, offset, "that the live-fed pause could back up")
                   if "backtrack refused" in ln]
    ev["backtrack_refused_lines"] = backtracked[:2]
    ctx.check(not backtracked, "the live-fed pause could not back up (%s)", backtracked[:1])
    ctx.check(hw.sysfs_int("cnc/underruns", 0) == before,
              "the pause or the resume starved the ring")

    # The app's cancel is how a print this long ends, and it is the one
    # place the service-side cancel is exercised: the same tail as a lid
    # or interlock abort - stop, park back to the job start, relock,
    # disarm, ':cancelled' - judged in full.
    off.cancel(9004)
    ctx.log("canceled the print as the app would")
    svc_stop = "action canceled mid-run; stopping motion"
    got = wait_log(ctx, offset, [svc_stop, "start return home", "return home complete"], 300)
    fin = wait_action_finished(ctx, offset, "print", 60)
    ev["service_cancel"] = {k: message(v) for k, v in got.items()}
    ev["print_finished"] = message(fin)
    for k, v in ev["service_cancel"].items():
        ctx.log("  [cancel] %s: %s", k, "seen" if v else "MISSING")
    ctx.check(got[svc_stop], "the app's cancel did not stop the run")
    ctx.check(got["return home complete"], "the canceled print did not park to completion")
    judge_abort_tail(ctx, ev, offset, "app", fin)
    ctx.check(hw.sysfs_int("cnc/streaming", 0) == 0,
              "the device was left in live-feed mode after the job ended")
    ctx.check(hw.sysfs_int("cnc/underruns", 0) == before,
              "the cancel produced an underrun")
    ev["button_dark"] = hw.button_lit()
    ctx.check(ev["button_dark"] is False, "the button is still lit after the cancel (%s)", ev["button_dark"])
    settle_cloud(ctx, offset)
    ev["events"] = offline_events(off)
    ctx.log("PASS: a job longer than the ring ran live-fed, total grew, no underrun; the app's cancel "
            "stopped it, parked, relocked and reported ':cancelled'")


