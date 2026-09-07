"""commission.sheet - the setup's sheet (the live wizards) in one run,
driven the way the page drives them: POST /wiz/<id>/start, GET /wiz/dark
polled, the prompts answered from here, each burn armed by a press, the
result judged, and every setting a wizard wrote put back as found. One
piece of wood at least 200 x 150 mm (8 x 6 in) carries the placement,
the frame, and the five cards. Each wizard re-runs on the real record,
so a completed one completes again at the same version.

The operator's part is one press on the machine at the start (the
presence check of the ready gate; with the bench actuator up it then
makes every arm press) and nothing else. The judgments a person makes
by eye on the sheet (the thinnest line, the faintest rung, the best
corner, the frame's fit) are answered here with a middle option or the
witnesses: the test proves each wizard's mechanics and the emission
witnesses, not the operator's eye, and the settings it writes are
restored. The page is where the real numbers go in.
"""
import re

from ..catalog import test
from .commission_dark import run_check, Restore

SHEET_COVERS = [("forgectrl", "src/wizlive.*"), ("forgectrl", "src/wizrun.h"),
                ("forgectrl", "src/sheet.*"),
                ("forgectrl", "src/font_hershey.*"), ("forgectrl", "src/jobstream.*"),
                ("forgectrl", "src/curverec.*"), ("forgectrl", "src/wizdark.*"),
                ("forgectrl", "src/wiz.*"), ("forgectrl", "src/commission.*"),
                ("forgectrl", "src/main.c"), ("forgectrl", "src/ui/wizard.*"),
                ("forgectrl", "src/super.c"),
                ("forgectrl", "src/status.c"), ("forgectrl", "src/cool.c"),
                ("forgectrl", "src/accel.*"), ("forgectrl", "src/wizcalc.*"),
                ("grblhal-glowforge", "src/glowforge_laser.c"),
                ("grblhal-glowforge", "src/glowforge_status.c"),
                ("grblhal-glowforge", "src/glowforge_homing.*"),
                ("grblhal-glowforge", "src/glowforge_io.*")]

# The live wizards in the order the sheet burns them: the wizard id, the
# answers to its prompts (a middle pick where a person would look at the
# sheet), the keys its result must carry, and how long the bench actuator
# waits for the button to light before the arm press goes to a person.
# The flow-load card settles the coolant loop before it lights the button
# (60 to 240 s); the others light within seconds of the lens reference.
CARDS = [
    ("sheet.frame", {"frame-arm": "Continue", "frame-ok": "Yes"}, ["mark_s", "mark_feed"], 120),
    ("laser.focus", {"focus-arm": "Continue", "focus-pick": "11", "thickness": "Keep"},
     ["pick", "thickness_mm", "pick_half_steps", "edge_z_mm", "steps_per_mm", "max_height_mm",
      "focus_range_mm", "stops"], 120),
    ("laser.floor", {"floor-arm": "Continue", "floor-pick": "8"}, ["faintest_density", "floor_density"], 120),
    ("laser.dose-curve", {"dose-arm": "Continue"}, ["points", "curve"], 120),
    ("laser.corner", {"corner-arm": "Continue", "corner-pick": "1.50"}, ["gamma"], 120),
    ("cooling.flow-load", {"load-arm": "Continue"}, ["lit_s", "dose_raw_s", "peak_c", "k_density", "k_cw"], 420),
]
# Every setting a card writes, restored as found when the run ends.
CARD_SETTINGS = ["lens_hall_edge_z_mm", "lens_stop_below_steps", "lens_stop_above_steps",
                 "laser_floor_density", "laser_dose_curve", "laser_corner_gamma",
                 "cool_laser_heat_density", "cool_laser_heat_cw"]
# The evidence run_check and the witnesses write at the top level, moved
# under the card's own key once it is done.
CARD_EVIDENCE = ("started", "elapsed_s", "phases", "log", "error", "result",
                 "preview_bytes", "program_lines", "emission")


def preview_ok(ctx, wid):
    """The page's preview and the program for the wizard are served."""
    fc = ctx.forgectrl
    st, body = fc.get("/wiz/sheet.svg?card=%s" % wid, raw=True)
    ctx.check(st == 200 and body.lstrip().startswith(b"<svg"), "no preview for %s (%s)", wid, st)
    ctx.evidence["preview_bytes"] = len(body)
    st, body = fc.get("/wiz/sheet.gcode?card=%s" % wid, raw=True)
    # The text and the frame are M4 at the mark dose; the patterns bring
    # their own M3 or M4 lines. A program is one with a laser-on command.
    ctx.check(st == 200 and re.search(rb"^M[34] S\d", body or b"", re.M) is not None,
              "no program for %s (%s)", wid, st)
    ctx.evidence["program_lines"] = body.count(b"\n")


def emission_ok(ctx, last):
    """The three witnesses of the burn, from the wizard's result."""
    e = (last.get("result") or {}).get("emission") or {}
    ctx.evidence["emission"] = e
    ctx.check(e.get("hv_max", 0) > 30, "the tube current never rose (%s)", e.get("hv_max"))
    ctx.check(e.get("laser_on_samples", 0) > 0, "the kernel never sampled LASER_ON")
    ctx.check(e.get("thermopile_delta", 0) >= 100, "the thermopile did not see the beam (%s)",
              e.get("thermopile_delta"))


def file_card(ctx, wid):
    """Move the card's evidence under its own key."""
    ev = ctx.evidence
    ev.setdefault("cards", {})[wid] = {k: ev.pop(k) for k in CARD_EVIDENCE if k in ev}


def thickness_answer(ctx):
    """The thickness the placement is answered with, in the machine's
    units (the number prompts read ui_units), and the millimeters the
    record must carry for it."""
    imperial = (ctx.forgectrl.settings() or {}).get("ui_units") == "imperial"
    return ("0.125", 3.175) if imperial else ("3.2", 3.2)


def place(ctx):
    """The placement: the lens reference, two jogs, the origin at the
    head's position, the full sheet, a thickness. Nothing fires."""
    jogs = ["X+10", "X-10"]
    answer, want_mm = thickness_answer(ctx)
    ctx.evidence["thickness_answer"] = answer

    def on_prompt(p):
        # Each jog answer closes the prompt and the wizard asks again with
        # the next sequence number, so the pad is answered one move at a
        # time until the origin is set.
        if p.get("id") == "place":
            return jogs.pop(0) if jogs else "Set origin"
        if p.get("id") == "sheet-kind":
            return "Full sheet"
        if p.get("id") == "thickness":
            return answer
        return None

    last = run_check(ctx, "sheet.place", on_prompt, 300)
    r = last.get("result") or {}
    ctx.check("origin_x" in r and "origin_y" in r, "no origin in the result: %s", r)
    ctx.check((r.get("steps_per_mm") or {}).get("x", 0) > 0, "no steps per mm: %s", r)
    ctx.check(r.get("z_referenced") is True, "the lens was not referenced")
    ctx.check(abs((r.get("thickness_mm") or 0) - want_mm) < 0.01,
              "thickness %s mm, expected %s (answered %s)", r.get("thickness_mm"), want_mm, answer)
    ctx.check(r.get("alone") is False, "alone %s", r.get("alone"))
    ctx.log("placed: datum at %s, %s; steps per mm %s", r.get("origin_x"), r.get("origin_y"),
            r.get("steps_per_mm"))
    file_card(ctx, "sheet.place")


def burn(ctx, wid, answers, want, lit_s):
    """One live card: the preview, the burn after the press with the
    prompts answered from `answers`, the witnesses, the keys of the
    result in `want`. The actuator waits `lit_s` for the button to light."""
    preview_ok(ctx, wid)
    ctx.arm_press(lit_timeout=lit_s)

    def on_prompt(p):
        return answers.get(p.get("id"))

    try:
        last = run_check(ctx, wid, on_prompt, 900)
    finally:
        ctx.clear_notice()
    emission_ok(ctx, last)
    r = last.get("result") or {}
    missing = [k for k in want if k not in r]
    ctx.check(not missing, "the result lacks %s: %s", missing, sorted(r))
    ctx.log("%s: %s, emission %s", wid, {k: r.get(k) for k in want}, ctx.evidence.get("emission"))
    file_card(ctx, wid)


@test("commission.sheet", title="The commissioning sheet: the placement, the frame, and the five cards",
      subsystem="commission", kind="live", hardware="takeover", mode="grbl", est_min=35,
      covers=SHEET_COVERS,
      requires=["forgectrl.auth", "commission.check-motion", "laser.emission-witness", "cooling.flow-verify"],
      actions=["button"], hands=["scrap"],
      steps=["A piece of wood at least 200 x 150 mm (8 x 6 in) on the bed, pushed as far left as "
             "it goes with its top edge at the top of the cut area (the head's home corner); "
             "lid closed. Eye protection on, exhaust on, extinguisher in reach.",
             "Start the test and press the machine's button once. That press says you are at the "
             "machine, and the bench actuator makes every arm press after it. With no actuator "
             "the start is the Ready answer instead, and you press the button each time it lights "
             "white: six times, the frame and the five cards.",
             "Nothing else: the test answers every prompt itself and puts back every setting the "
             "cards write. It runs about half an hour, mostly the coolant settle and the dark "
             "tails."],
      description="One run over the sheet's seven wizards on one piece of wood. "
                  "POST /wiz/sheet.place/start references the lens, takes the controller in "
                  "loopback posture, jogs +X 10 and -X 10, and sets the origin at the head "
                  "(Full sheet; the thickness answered in the machine's units, 3.2 mm or "
                  "0.125 in). Then, each after the press and each with its preview "
                  "and program served by GET /wiz/sheet.svg and /wiz/sheet.gcode: the frame "
                  "(180 x 130 mm) and the header band at the mark dose; the focus card (the "
                  "lens homed on its bottom stop, the hall edge counted, twelve lines down the "
                  "travel; the pick is line 11, the thickness kept); the floor card (twelve "
                  "rungs from 2 to 24 percent with the floor and the curve off for the job; the "
                  "pick is 8, the floor written is 10); the dose card (seven rungs sampled at "
                  "25 Hz, the curve fit on 20 s of dark); the corner card (five gammas reloaded "
                  "through M102 inside one armed job; the pick is 1.50); and the flow-load card "
                  "(the coolant loop settled, the box and the patch at 60 percent, the "
                  "downstream peak over 100 s of dark as the coefficient). Every burn must be "
                  "seen by the three witnesses (the tube current, the kernel's LASER_ON samples, "
                  "the thermopile rise); every result must carry its keys; the record must "
                  "carry each wizard at version 1; the seven settings the cards write are "
                  "restored at the end.")
def sheet(ctx):
    ctx.ready("The sheet: the placement, then the frame and the five cards burn after a press "
              "each. The bench presses when the button lights.")
    place(ctx)
    with Restore(ctx, CARD_SETTINGS):
        for wid, answers, want, lit_s in CARDS:
            burn(ctx, wid, answers, want, lit_s)
    ctx.log("PASS: the sheet's seven wizards ran on one piece; settings restored")
