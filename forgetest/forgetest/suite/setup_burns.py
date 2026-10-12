# Copyright 2026 514 LLC d/b/a OpenGlow
# Written by Scott Wiederhold
# https://community.openglow.org
# SPDX-License-Identifier:    MIT

"""setup.focus, setup.floor - the setup's two burns (the live wizards),
driven the way the page drives them: POST /wiz/<id>/start, GET /wiz/dark
polled, the prompts answered from here, the burn armed by a press, the
result judged, and the setting the burn wrote put back as found. Each
burn stands alone on its own scrap of wood in the back-left corner,
drawn from the head's home against the gantry stops, and re-runs on the
real record, so a completed burn completes again at the same version.

The operator's part is the scrap and one press on the machine at the
start (the presence check of the ready gate; with the bench actuator up
it then makes the arm press). The judgment a person makes by eye on the
scrap (the narrowest line, the faintest rung) is answered here with a
fixed pick: the test proves each wizard's mechanics and the emission
witnesses, not the operator's eye, and the setting it writes is
restored. The page is where the real numbers go in.
"""
import re

from ..catalog import test
from .setup_dark import run_check, Restore, FALLBACK_WINDOW, LENS_WINDOW_KEYS

BURN_COVERS = [("forgectrl", "src/wizlive.*"), ("forgectrl", "src/wizrun.h"),
               ("forgectrl", "src/lens.*"), ("forgectrl", "src/sheet.*"),
               ("forgectrl", "src/font_hershey.*"), ("forgectrl", "src/jobstream.*"),
               ("forgectrl", "src/jobrun.*"), ("forgectrl", "src/lease.*"),
               ("forgectrl", "src/curverec.*"), ("forgectrl", "src/wizdark.*"),
               ("forgectrl", "src/wiz.*"), ("forgectrl", "src/setup.*"),
               ("forgectrl", "src/main.c"), ("forgectrl", "src/ui/wizard.*"),
               ("forgectrl", "src/super.c"),
               ("forgectrl", "src/status.c"), ("forgectrl", "src/cool.c"),
               ("forgectrl", "src/accel.*"), ("forgectrl", "src/wizcalc.*"),
               ("grblhal-glowforge", "src/glowforge_laser.c"),
               ("grblhal-glowforge", "src/glowforge_status.c"),
               ("grblhal-glowforge", "src/glowforge_homing.*"),
               ("grblhal-glowforge", "src/glowforge_io.*")]

# Every burn asks first that the gantry stops are installed and the head
# is at its home against them; the bench's head stands there, and every
# burn's tail brings it back.
HOME = "The head is home"
EDGE_KEY = "lens_hall_edge_z_mm"
FLOOR_KEY = "laser_floor_density"
FOCUS_KEYS = ["thickness_mm", "z_passes", "ladder_z", "window", "emission", "pick", "picked_lines",
              "pick_half_steps", "edge_z_mm", "steps_per_mm", "max_height_mm", "focus_range_mm", "summary"]
FLOOR_KEYS = ["thickness_mm", "z_passes", "emission", "faintest_density", "floor_density", "summary"]


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


def emission_ok(ctx, last):
    """The three witnesses of the burn, from the wizard's result."""
    e = (last.get("result") or {}).get("emission") or {}
    ctx.evidence["emission"] = e
    ctx.check(e.get("hv_max", 0) > 30, "the tube current never rose (%s)", e.get("hv_max"))
    ctx.check(e.get("laser_on_samples", 0) > 0, "the kernel never sampled LASER_ON")
    ctx.check(e.get("thermopile_delta", 0) >= 100, "the thermopile did not see the beam (%s)",
              e.get("thermopile_delta"))


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
    and the pick in `pick`), the witnesses, and the keys of the result in
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
        last = run_check(ctx, wid, on_prompt, 900)
    finally:
        ctx.clear_notice()
    emission_ok(ctx, last)
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
      description="The focus burn as a fresh head runs it: the edge setting is cleared first, so "
                  "the burn starts from the bench reference's edge, while the lens window stays "
                  "the one the Lens travel check wrote (or the fallback). POST "
                  "/wiz/laser.focus/start is answered that the gantry stops are installed, "
                  "references the lens, takes the controller in loopback posture, is answered that "
                  "the head is home and that the scrap is in the corner, and takes the thickness "
                  "in the machine's units (3.2 mm or 0.125 in); its program declares X0 Y0 at the "
                  "head and its tail returns there. With its preview and program served by GET "
                  "/wiz/sheet.svg and /wiz/sheet.gcode and every Z the program commands inside the "
                  "reach /status reports, it burns twelve lines after the press, from the top of "
                  "the window to the bottom, seen by the three witnesses (the tube current, the "
                  "kernel's LASER_ON samples, the thermopile rise). The pick is line 11. The "
                  "result carries its keys; the ladder ran in the window the machine held, which "
                  "the burn leaves as found (it writes the edge alone, the one its result "
                  "reports); every ladder height lies in the reach; the program served afterward "
                  "agrees with /status; the record carries laser.focus at version 1. The edge is "
                  "restored at the end.")
def focus(ctx):
    fc = ctx.forgectrl
    ev = ctx.evidence
    ctx.ready("The focus burn: twelve lines on the scrap after a press. The bench presses when the "
              "button lights.")
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
    ctx.log("PASS: the focus burn ran in the window the machine holds and wrote the edge alone; "
            "settings restored")


@test("setup.floor", title="The laser floor burn: the rungs and the floor it writes",
      subsystem="setup", kind="live", hardware="takeover", mode="grbl", est_min=10,
      covers=BURN_COVERS,
      requires=["forgectrl.auth", "setup.check-motion", "laser.emission-witness"],
      actions=["button"], hands=["scrap"],
      steps=["The gantry stops installed and the head at its home against them. A fresh flat scrap "
             "of wood about 3 mm (1/8 in) thick and at least 86 x 68 mm (3.4 x 2.7 in), pushed into "
             "the back-left corner of the cut area: as far left as it goes, its top edge at the top "
             "of the cut area; lid closed. Eye protection on, exhaust on, extinguisher in reach.",
             "Start the test and press the machine's button once. That press says you are at the "
             "machine, and the bench actuator makes the arm press after it, when the burn itself "
             "asks for it. With no actuator the start is the Ready answer instead, and you press "
             "the button again at the arm, as the page asks.",
             "Nothing else: the test answers every prompt itself and puts back the setting the "
             "burn writes."],
      description="POST /wiz/laser.floor/start on a fresh scrap, set up as the focus burn is: "
                  "answered that the gantry stops are installed, the lens referenced, the "
                  "controller in loopback posture, answered that the head is home and that the "
                  "scrap is in the corner, the thickness in the machine's units (3.2 mm or 0.125 "
                  "in); its program declares X0 Y0 at the head and its tail returns there. With "
                  "its preview and program served and every Z the program commands inside the "
                  "reach /status reports, it burns twelve rungs from 2 to 24 percent density at "
                  "the focal point on the scrap's surface, the floor and the curve off for the "
                  "job, after the press, seen by the three witnesses. The pick is 8, so the floor "
                  "written is 10 (two above it) and the setting reads it; the result carries its "
                  "keys; the record carries laser.floor at version 1. laser_floor_density is "
                  "restored at the end.")
def floor(ctx):
    fc = ctx.forgectrl
    ctx.ready("The laser floor burn: twelve rungs on the scrap after a press. The bench presses when "
              "the button lights.")
    with Restore(ctx, [FLOOR_KEY]):
        r = burn(ctx, "laser.floor", "floor-arm", {"floor-pick": "8"}, FLOOR_KEYS)
        ctx.check(r.get("faintest_density") == 8 and r.get("floor_density") == 10,
                  "the floor reads %s from the faintest rung %s, answered 8", r.get("floor_density"),
                  r.get("faintest_density"))
        held = (fc.settings() or {}).get(FLOOR_KEY)
        try:
            held_ok = float(held) == r.get("floor_density")
        except (TypeError, ValueError):
            held_ok = False
        ctx.check(held_ok, "%s reads %r, the burn wrote %s", FLOOR_KEY, held, r.get("floor_density"))
        ctx.log("floor: the faintest rung %s, the floor %s", r.get("faintest_density"), r.get("floor_density"))
    ctx.log("PASS: the floor burn wrote two above the faintest rung; settings restored")
