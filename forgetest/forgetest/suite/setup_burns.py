# Copyright 2026 514 LLC d/b/a OpenGlow
# Written by Scott Wiederhold
# https://community.openglow.org
# SPDX-License-Identifier:    MIT

"""setup.focus - the setup's focus burn (a live wizard), driven the way
the page drives it: POST /wiz/<id>/start, GET /wiz/dark polled, the
prompts answered from here, the burn armed by a press, the result
judged, and the setting the burn wrote put back as found. The burn
stands alone on its own scrap of wood in the back-left corner, drawn
from the head's home against the gantry stops, and re-runs on the real
record, so a completed burn completes again at the same version.

The operator's part is the scrap and one press on the machine at the
start (the presence check of the ready gate; with the bench actuator up
it then makes the arm press). The judgment a person makes by eye on the
scrap (the narrowest line) is answered here with a fixed pick: the test
proves the wizard's mechanics and the emission witnesses, not the
operator's eye, and the setting it writes is restored. The page is where
the real numbers go in.
"""
import re
import time

from .. import hw
from ..catalog import test
from .setup_dark import run_check, Restore, FALLBACK_WINDOW, LENS_WINDOW_KEYS, dark

BURN_COVERS = [("forgectrl", "src/wizlive.*"), ("forgectrl", "src/wizrun.h"),
               ("forgectrl", "src/lens.*"), ("forgectrl", "src/sheet.*"),
               ("forgectrl", "src/lenshome.*"),
               ("forgectrl", "src/font_hershey.*"), ("forgectrl", "src/jobstream.*"),
               ("forgectrl", "src/jobrun.*"), ("forgectrl", "src/lease.*"),
               ("forgectrl", "src/curverec.*"), ("forgectrl", "src/wizdark.*"),
               ("forgectrl", "src/wiz.*"), ("forgectrl", "src/setup.*"),
               ("forgectrl", "src/main.c"), ("forgectrl", "src/ui/wizard.*"),
               ("forgectrl", "src/super.c"),
               ("forgectrl", "src/status.c"), ("forgectrl", "src/cool.c"),
               ("grblhal-glowforge", "src/glowforge_laser.c"),
               ("grblhal-glowforge", "src/glowforge_status.c"),
               ("grblhal-glowforge", "src/glowforge_homing.*"),
               ("grblhal-glowforge", "src/glowforge_io.*")]

# Every burn asks first that the gantry stops are installed and the head
# is at its home against them; the bench's head stands there, and every
# burn's tail brings it back. head_at_home reads the head's place from
# /status before the wizard starts and judges it at X0 Y0, within
# HOME_TOL_MM, when a manual home references X and Y.
HOME = "The head is home"
HOME_TOL_MM = 0.05
EDGE_KEY = "lens_hall_edge_z_mm"
FOCUS_KEYS = ["thickness_mm", "z_passes", "ladder_z", "window", "emission", "pick", "picked_lines",
              "pick_half_steps", "edge_z_mm", "steps_per_mm", "max_height_mm", "focus_range_mm", "summary"]
# pic/hv_current above this: the discharge is present (the daemon's own
# rule for a lit burn, jobstream.h JOBSTREAM_HV_ON).
HV_ON = 30


class Witnesses:
    """The two emission witnesses read from sysfs by the test itself at
    every poll of the burn: the kernel's LASER_ON sample count
    (cnc/laser_on_sampled) and the tube current (pic/hv_current), each
    at its peak, and how many polls read both. The burn's result reports
    the daemon's own reading of the same two; this one is taken past the
    daemon."""

    def __init__(self):
        self.laser_on_max = None
        self.hv_max = None
        self.reads = 0

    def __call__(self, _doc):
        lon = hw.sysfs_int("cnc/laser_on_sampled")
        hv = hw.sysfs_int("pic/hv_current")
        if lon is not None:
            self.laser_on_max = lon if self.laser_on_max is None else max(self.laser_on_max, lon)
        if hv is not None:
            self.hv_max = hv if self.hv_max is None else max(self.hv_max, hv)
        if lon is not None and hv is not None:
            self.reads += 1

    def record(self):
        return {"laser_on_max": self.laser_on_max, "hv_max": self.hv_max, "reads": self.reads}


def head_at_home(ctx):
    """The head's place on record before the wizard starts, read from
    /status under the campaign's own controller: the wizard restarts the
    controller before its home prompt, and a fresh controller drops the
    X/Y reference, so a read at that prompt places nothing. Judged at
    X0 Y0 within HOME_TOL_MM when X and Y are referenced by a manual
    home, the one provider that declares X0 Y0 at the gantry stops.
    Unreferenced, the head's place rests on the operator's word at the
    ready gate, and the record says so."""
    fc = ctx.forgectrl
    read = {}

    def served():
        st = fc.status()
        if not isinstance(st.get("pos"), dict):
            return False
        read.update(st)
        return True
    took = ctx.wait_for(served, 10)
    ctx.check(took is not None, "/status served no head position within 10 s: the head's place cannot "
              "go on record before the burn")
    pos = read.get("pos") or {}
    axes = read.get("homed_axes")
    source = read.get("home_source")
    referenced = isinstance(axes, int) and (axes & 3) == 3 and source == "manual"
    ctx.evidence["head_at_start"] = {"pos": pos, "homed_axes": axes, "home_source": source,
                                     "judged": referenced}
    if not referenced:
        ctx.log("the head's place rests on the operator's word at the ready gate (X and Y unreferenced: "
                "homed_axes %s, home_source %s, pos %s)", axes, source, pos)
        return
    x, y = pos.get("x"), pos.get("y")
    at_home = (isinstance(x, (int, float)) and isinstance(y, (int, float))
               and abs(x) <= HOME_TOL_MM and abs(y) <= HOME_TOL_MM)
    ctx.check(at_home, "the head is not at its home against the gantry stops: /status reads pos %s with X "
              "and Y referenced by a manual home; home it there before the burn", pos)
    ctx.log("the head is home on record: pos %s, X and Y referenced by a manual home", pos)


def preview_ok(ctx, wid):
    """The page's preview and the program for the burn are served."""
    fc = ctx.forgectrl
    st, body = fc.get("/wiz/sheet.svg?card=%s" % wid, raw=True)
    ctx.check(st == 200 and body.lstrip().startswith(b"<svg"), "no preview for %s (%s)", wid, st)
    ctx.evidence["preview_bytes"] = len(body)
    st, body = fc.get("/wiz/sheet.gcode?card=%s" % wid, raw=True)
    # The text is M4 at the mark dose; the ladders bring their own M3 or
    # M4 lines. A program is one with a laser-on command.
    ctx.check(st == 200 and re.search(rb"^M[34] S\d", body or b"", re.M) is not None,
              "no program for %s (%s)", wid, st)
    ctx.evidence["program_lines"] = body.count(b"\n")
    # Every Z the program commands is a height the head reaches in the
    # window the machine holds now: the Lens travel check's, or the
    # fallback on a head whose travel is not known yet. The controller
    # refuses a Z outside it with ALARM:2, and it refuses it while the
    # body is still burning, so the burn dies where the parser is and not
    # where the bad line is.
    lens = (fc.status() or {}).get("lens") or {}
    lo, hi = lens.get("reach_min"), lens.get("reach_max")
    zs = [float(m) for m in re.findall(rb"^G0 Z(-?[0-9.]+)", body or b"", re.M)]
    ctx.evidence.setdefault("program_z", {})[wid] = {
        "z": zs, "reach": [lo, hi], "stops_found": lens.get("stops_found")}
    ctx.check(lo is not None and hi is not None, "/status carries no lens reach: %s", lens)
    if lo is not None and hi is not None:
        out = [z for z in zs if z < lo - 0.01 or z > hi + 0.01]
        ctx.check(not out, "%s commands Z outside the lens window %.2f..%.2f: %s",
                  wid, lo, hi, out)
    # The tail the wizard streams after the body: the lens back on the
    # reference the program's head declared, the head back to X0 Y0, the
    # G92 origin cleared (the controller keeps a G92 offset and restores it
    # at its next start, where it would shift a sender's own work offsets),
    # and the program end.
    lines = [ln.strip() for ln in (body or b"").decode("utf-8", "replace").splitlines()]
    tail = [ln for ln in lines if ln and not ln.startswith(";")][-4:]
    edge = lens.get("edge_z")
    want = ["G0 Z%.2f" % edge, "G0 X0 Y0", "G92.1", "M2"] if isinstance(edge, (int, float)) else None
    ctx.evidence.setdefault("program_tail", {})[wid] = tail
    ctx.check(tail == want, "the %s program ends %s, not the tail %s", wid, tail, want)


def emission_ok(ctx, last, seen):
    """The two witnesses of the burn, from the wizard's result and from
    the test's own reads of them at every poll (`seen`, a Witnesses).
    The head's beam sensor is recorded, never required: the result
    reports its rise and whether it read as the beam seen."""
    e = (last.get("result") or {}).get("emission") or {}
    ctx.evidence["emission"] = e
    ctx.check(e.get("hv_max", 0) > HV_ON, "the tube current never rose (%s)", e.get("hv_max"))
    ctx.check(e.get("laser_on_samples", 0) > 0, "the kernel never sampled LASER_ON")
    ctx.check(isinstance(e.get("thermopile_delta"), int) and isinstance(e.get("beam_sensor_seen"), bool),
              "the result does not record the beam sensor: %s", e)
    ctx.log("the beam sensor rose %s (seen: %s)", e.get("thermopile_delta"), e.get("beam_sensor_seen"))
    w = seen.record()
    ctx.check(w["reads"] > 0, "the test could not read the witnesses from sysfs during the burn")
    ctx.check((w["laser_on_max"] or 0) > 0,
              "the kernel's LASER_ON count never read nonzero from here during the burn (%d reads); the "
              "burn reports %s", w["reads"], e.get("laser_on_samples"))
    ctx.check((w["hv_max"] or 0) > HV_ON,
              "the tube current never read over %d from here during the burn (peak %s over %d reads); the "
              "burn reports %s", HV_ON, w["hv_max"], w["reads"], e.get("hv_max"))


def thickness_answer(ctx):
    """The thickness the burn is answered with, in the machine's units
    (the number prompts read ui_units), and the millimeters the record
    must carry for it."""
    imperial = (ctx.forgectrl.settings() or {}).get("ui_units") == "imperial"
    return ("0.125", 3.175) if imperial else ("3.2", 3.2)


def burn(ctx, wid, arm, pick, want):
    """One burn: the preview, then the burn after the press with every
    prompt answered (the gantry stops installed, the head at home, the
    scrap in the corner, its thickness in the machine's units, the arm,
    and the pick in `pick`), the witnesses (the burn's own, and the two
    read from sysfs here at every poll), and the keys of the result in
    `want`. run_check makes the arm press when the burn asks for it. A
    prompt the test does not answer, or one asked again because its
    answer was refused, ends the burn. Returns the result."""
    answer, want_mm = thickness_answer(ctx)
    ctx.evidence["thickness_answer"] = answer
    answers = {"gantry-stops": "Yes", "home": HOME, "scrap": "Continue", "thickness": answer,
               arm: "Continue"}
    answers.update(pick)
    preview_ok(ctx, wid)
    asked = set()
    seen = Witnesses()

    def on_prompt(p):
        pid = p.get("id")
        if pid == "press":
            return None
        if pid not in answers:
            # the motors found released ask for the head pushed home by
            # hand, which the bench's head never needs
            ctx.fail("the %s burn asked %s, which the test does not answer: %s", wid, pid, p.get("text"))
        if pid in asked:
            ctx.fail("the %s burn asked %s again: it refused %r", wid, pid, answers[pid])
        asked.add(pid)
        return answers[pid]

    try:
        last = run_check(ctx, wid, on_prompt, 900, on_poll=seen)
    finally:
        ctx.clear_notice()
        # What the test read itself goes on record however the burn ends:
        # reported dark, failed, timed out, or aborted.
        w = seen.record()
        ctx.evidence["emission_seen"] = w
        ctx.log("seen from here: LASER_ON count peak %s, tube current peak %s over %d polls",
                w["laser_on_max"], w["hv_max"], w["reads"])
    emission_ok(ctx, last, seen)
    r = last.get("result") or {}
    missing = [k for k in want if k not in r]
    ctx.check(not missing, "the result lacks %s: %s", missing, sorted(r))
    ctx.check(abs((r.get("thickness_mm") or 0) - want_mm) < 0.01,
              "thickness %s mm, expected %s (answered %s)", r.get("thickness_mm"), want_mm, answer)
    ctx.check(len(r.get("z_passes") or []) >= 1, "the lens was not referenced: %s", r.get("z_passes"))
    return r


def window_held(ctx):
    """The lens window the machine holds: the two settings, and the lens
    block of /status, which reads them (the fallback where they are
    unset)."""
    fc = ctx.forgectrl
    s = fc.settings() or {}
    lens = (fc.status() or {}).get("lens") or {}
    return {"settings": {k: s.get(k) for k in LENS_WINDOW_KEYS},
            "window": (lens.get("below"), lens.get("above")), "stops_found": lens.get("stops_found")}


def focus_window_ok(ctx, r, before):
    """The ladder burned in the window the machine held before the burn
    (`before`, from window_held): the Lens travel check's, or the fallback
    while the settings are unset. The burn left that window as it found
    it and wrote the edge alone, the one its result reports; every ladder
    height lies in the reach, and the program served now agrees with
    /status."""
    fc = ctx.forgectrl
    ev = ctx.evidence
    w = r.get("window") or {}
    window = (w.get("below"), w.get("above"))
    after = window_held(ctx)
    ctx.check(after["settings"] == before["settings"], "the focus burn wrote the lens window: %s, was %s",
              after["settings"], before["settings"])
    ctx.check(window == before["window"], "the ladder ran in %s/%s, the machine holds %s",
              window[0], window[1], before["window"])
    held = before["settings"]
    if before["stops_found"]:
        ctx.check(tuple(str(held[k]) for k in LENS_WINDOW_KEYS) == tuple(str(v) for v in window),
                  "the ladder ran in %s/%s, the settings hold %s", window[0], window[1], held)
    else:
        ctx.check(window == FALLBACK_WINDOW, "the settings hold no window (%s), but the ladder ran in %s/%s",
                  held, window[0], window[1])
    edge = (fc.settings() or {}).get(EDGE_KEY)
    try:
        edge_ok = abs(float(edge) - float(r.get("edge_z_mm"))) < 0.006
    except (TypeError, ValueError):
        edge_ok = False
    ctx.check(edge_ok, "%s reads %r, the burn reports %s", EDGE_KEY, edge, r.get("edge_z_mm"))
    zs = r.get("ladder_z") or []
    lo, hi = w.get("reach_min"), w.get("reach_max")
    out = [z for z in zs if lo is None or hi is None or z < lo - 0.01 or z > hi + 0.01]
    ctx.check(len(zs) == 12 and not out, "ladder heights outside the reach %s..%s: %s", lo, hi, out or zs)
    first = dict(ev.get("program_z") or {}).get("laser.focus")
    preview_ok(ctx, "laser.focus")
    ev["focus_window"] = {"window": w, "ladder_z": zs, "held": before, "edge": edge,
                          "program_z_before": first,
                          "program_z_after": (ev.get("program_z") or {}).get("laser.focus"),
                          "preview_bytes": ev.pop("preview_bytes", None),
                          "program_lines": ev.pop("program_lines", None)}


@test("setup.focus", title="The focus burn: the ladder in the lens window, and the edge it writes",
      subsystem="setup", kind="live", hardware="takeover", mode="grbl", est_min=10,
      covers=BURN_COVERS,
      requires=["forgectrl.auth", "setup.check-motion", "laser.emission-witness", "setup.check-lens"],
      actions=["button"], hands=["scrap"],
      steps=["The gantry stops installed and the head at its home against them. A fresh flat scrap "
             "of wood about 3 mm (1/8 in) thick and at least 78 x 68 mm (3.1 x 2.7 in), pushed into "
             "the back-left corner of the cut area: as far left as it goes, its top edge at the top "
             "of the cut area; lid closed. Eye protection on, exhaust on, extinguisher in reach.",
             "Start the test and press the machine's button once. That press says you are at the "
             "machine, and the bench actuator makes the arm press after it, when the burn itself "
             "asks for it. With no actuator the start is the Ready answer instead, and you press "
             "the button again at the arm, as the page asks.",
             "Nothing else: the test answers every prompt itself and puts back the setting the "
             "burn writes."],
      description="The focus burn as a fresh head runs it. Before anything starts, the head's "
                  "place is read from /status and recorded: judged at X0 Y0 when a manual home "
                  "references X and Y, else left to the operator's word at the ready gate. The "
                  "edge setting is cleared, so the burn starts from the bench reference's edge, "
                  "while the lens window stays the one the Lens travel check wrote (or the "
                  "fallback). POST "
                  "/wiz/laser.focus/start is answered that the gantry stops are installed, "
                  "references the lens, takes the controller in loopback posture, is answered that "
                  "the head is home and that the scrap is in the corner, and takes the thickness "
                  "in the machine's units (3.2 mm or 0.125 in); its program declares X0 Y0 at the "
                  "head, and its tail puts the lens back on the reference, returns the head there, "
                  "clears the G92 origin (G92.1), and ends (M2). With its preview and program "
                  "served by GET /wiz/sheet.svg and /wiz/sheet.gcode, every Z the program commands "
                  "inside the reach /status reports, and the program ending in that tail at the "
                  "edge /status reports, it burns twelve lines after the press, from the top of "
                  "the window to the bottom, seen by the two witnesses (the tube current and the "
                  "kernel's LASER_ON samples) as the burn reports them and as the test reads them "
                  "from sysfs itself at every poll, with the beam sensor's rise recorded and not "
                  "required. The pick is line 11. The "
                  "result carries its keys; the ladder ran in the window the machine held, which "
                  "the burn leaves as found (it writes the edge alone, the one its result "
                  "reports); every ladder height lies in the reach; the program served afterward "
                  "agrees with /status; the record carries laser.focus at version 1. The edge and "
                  "the window are restored at the end. grblHAL reads them only at its start, and "
                  "the burn started it before the restore, so the controller is stopped and "
                  "started again on the settings as found (on every exit path), and it must be "
                  "running with motion verified under a new process.")
def focus(ctx):
    fc = ctx.forgectrl
    ev = ctx.evidence
    restart = {}

    def restart_on_found():
        """A fresh controller start on the settings as found, once the
        burn has ended. Plain waits, not the run's, so an abort gets it
        too; a burn still ending starts its own controller after the
        restore, which reads them already."""
        try:
            t_end = time.time() + 120
            while time.time() < t_end and dark(fc).get("running"):
                time.sleep(1)
            if dark(fc).get("running"):
                restart["skipped"] = "the burn is still ending"
                ctx.log("the burn is still ending: its own controller start reads the settings as found")
                return
            st, m = fc.get("/mode")
            m = m if isinstance(m, dict) else {}
            restart.update(mode=m.get("mode"), pid=m.get("pid"))
            if m.get("mode") != "grbl":
                return                      # the cloud client reads neither the edge nor the window
            restart["stop"], body = fc.post("/controller/stop")
            ctx.log("restarting the controller on the edge and the window as found: POST "
                    "/controller/stop -> %s %s", restart["stop"], body)
            for _ in range(10):
                restart["start"], body = fc.post("/controller/start")
                if restart["start"] != 409:
                    break
                time.sleep(1)               # the burn's lease goes just after its end
            ctx.log("POST /controller/start -> %s %s", restart["start"], body)
        except hw.HwError as e:
            restart["error"] = str(e)
            ctx.log("the controller could not be restarted on the settings as found: %s", e)
    ctx.ready("The focus burn: twelve lines on the scrap after a press. The bench presses when the "
              "button lights.")
    head_at_home(ctx)
    try:
        with Restore(ctx, [EDGE_KEY] + LENS_WINDOW_KEYS):
            # A fresh head's focus: the edge unknown, so the burn runs from the
            # bench reference's number and writes the head's own. The window
            # stays: it is the Lens travel check's.
            st, _ = fc.post("/settings", params={EDGE_KEY: ""})
            ctx.check(st == 200, "clearing %s -> %s", EDGE_KEY, st)
            before = window_held(ctx)
            ev["lens_fresh"] = (fc.status() or {}).get("lens") or {}
            r = burn(ctx, "laser.focus", "focus-arm", {"focus-pick": "11"}, FOCUS_KEYS)
            ctx.check(r.get("pick") == 11 and r.get("picked_lines") == [11, 11],
                      "the pick reads %s (lines %s), answered 11", r.get("pick"), r.get("picked_lines"))
            ctx.check("stops" not in r, "the focus burn searched for the stops: %s", r.get("stops"))
            focus_window_ok(ctx, r, before)
            ctx.log("focus: line %s, the edge at %s mm, the window %s/%s", r.get("pick"), r.get("edge_z_mm"),
                    (r.get("window") or {}).get("below"), (r.get("window") or {}).get("above"))
    finally:
        restart_on_found()
        ev["restart"] = dict(restart)
    if restart.get("mode") == "grbl":
        ctx.check(restart.get("stop") == 200 and restart.get("start") == 200,
                  "the controller was not restarted on the settings as found: %s", restart)

    def back():
        st, m = fc.get("/mode")
        m = m if isinstance(m, dict) else {}
        return (m.get("controller") == "running" and m.get("motion") == "verified"
                and (restart.get("start") != 200 or m.get("pid") != restart.get("pid")))
    ok = ctx.wait_for(back, 150)
    ctx.check(ok is not None, "the controller did not come back on the settings as found")
    ctx.log("PASS: the focus burn ran in the window the machine holds and wrote the edge alone; "
            "settings restored and the controller restarted on them")
