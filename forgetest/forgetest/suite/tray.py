# Copyright 2026 514 LLC d/b/a OpenGlow
# Written by Scott Wiederhold
# https://community.openglow.org
# SPDX-License-Identifier:    MIT

"""The crumb tray's mode, on the machine.

Its own module, so that no other test's fingerprint moves. With the tray
out, the GRBL controller's Z is the focal height above the floor of the cut
area: its Z position and its Z range move up by tray_offset_mm on the lens
step grid, and nothing moves. The mode persists as the controller's marker
in the data directory. That it survives a controller restart is the
driver's harness case (tray_test.py): a restart here would drop the X and
Y reference, and the machine would not be handed back as it was found.
"""

import math
import os
import sys
import time

from ..catalog import test
from .. import hw
from .motion import (_kernel_position, _port_state, _words, clean_slate, drain_text, machine_idle)

TRAY_MARKER = "/data/forgefirm/tray.out"
LENS_SPM = 2.922            # $102: the lens screw's half-steps per mm
SETUP_CARD = "sheet.place"  # the first setup card: it asks before it moves anything


def _grid_mm(offset_mm):
    """The offset on the lens step grid, the controller's way (lroundf)."""
    return math.floor(offset_mm * LENS_SPM + 0.5) / LENS_SPM


def _lens(fc):
    return fc.status().get("lens") or {}


def _judge(ctx, fc, ev, key, mode, found, shift):
    """The machine reads the tray `mode`, with Z and the reach moved by
    `shift` mm from what the test found."""
    s = fc.status()
    lens, pos = s.get("lens") or {}, s.get("pos") or {}
    port = _port_state(fc)
    rec = ev[key] = {"lens": lens, "pos_z": pos.get("z"), "port_tray": port.get("tray"),
                     "port_z": port.get("mpos", [None] * 3)[2], "marker": os.path.exists(TRAY_MARKER),
                     "home": [s.get("homed_axes"), s.get("home_source")]}
    ctx.log("%s: %s", key, rec)
    ctx.check(lens.get("tray") == mode and port.get("tray") == mode,
              "%s: the status says tray %s and the port %s, not %s", key, lens.get("tray"), port.get("tray"), mode)
    ctx.check(rec["marker"] == (mode == "out"), "%s: the marker %s is %s with the tray %s", key, TRAY_MARKER,
              "present" if rec["marker"] else "absent", mode)
    for k in ("reach_min", "reach_max"):
        ctx.check(lens.get(k) is not None and abs(lens[k] - (found["lens"][k] + shift)) < 0.011,
                  "%s: %s reads %s, not %.2f", key, k, lens.get(k), found["lens"][k] + shift)
    ctx.check(pos.get("z") is not None and abs(pos["z"] - (found["pos_z"] + shift)) < 0.006,
              "%s: the status Z reads %s, not %.2f", key, pos.get("z"), found["pos_z"] + shift)
    ctx.check(rec["port_z"] is not None and abs(rec["port_z"] - (found["port_z"] + shift)) < 0.002,
              "%s: the controller's Z reads %s, not %.3f", key, rec["port_z"], found["port_z"] + shift)
    ctx.check(rec["home"] == found["home"], "%s: the reference changed across the switch: %s, not %s",
              key, rec["home"], found["home"])


def _tray_route(fc, mode):
    st, body = fc.post("/motion/tray", params={"tray": mode})
    return st, _words(body)


def _card_refused(ctx, fc, ev):
    """A setup card is refused while the tray is out. The machine is idle a
    moment after the suite's Grbl client closes; until then the refusal is
    the idle rule's. A card that started anyway is ended at once: the first
    one asks before it moves anything."""
    path = "/wiz/%s/start" % SETUP_CARD
    deadline = time.time() + 20
    while True:
        st, body = fc.post(path)
        words = _words(body)
        if st == 200:
            fc.post("/wiz/%s/abort" % SETUP_CARD)
            ctx.wait_for(lambda: not (fc.get("/wiz/dark")[1] or {}).get("running"), 20, poll=0.5)
        if st != 409 or "not idle" not in words or time.time() > deadline:
            break
        ctx.sleep(0.5)
    ev["setup_card"] = [st, words]
    ctx.log("a setup card started with the tray out -> %s %s", st, words)
    ctx.check(st == 409 and "crumb tray" in words, "a setup card started with the tray out -> %s %s", st, words)


@test("motion.tray", title="The crumb tray: tray out moves Z and its range up by the offset, and nothing moves",
      subsystem="motion", kind="auto", mode="grbl", est_min=2,
      covers=[("grblhal-glowforge", "src/glowforge_tray.*"), ("grblhal-glowforge", "src/glowforge_homing.*"),
              ("grblhal-glowforge", "src/ctlport.*"), ("grblhal-glowforge", "src/glowforge_io.*"),
              ("forgectrl", "src/tray.*"), ("forgectrl", "src/grblport.*"), ("forgectrl", "src/status.*"),
              ("forgectrl", "src/main.c"), ("forgectrl", "src/wizdark.*"), ("forgectrl", "src/ui/panel.js"),
              ("forgectrl", "src/ui/index.html")],
      requires=["motion.port-jog"],
      steps=["Nothing moves; the crumb tray can stay where it is."],
      description="The tray mode is switched to the other one and back, through the panel's route and "
                  "through M103 from a Grbl client, both ways. Each switch changes the numbers and "
                  "moves nothing: /status says the tray, its Z and the lens reach move by the offset on "
                  "the lens step grid (1.35 in is 100 half-steps, 34.22 mm), the reference and what set it "
                  "stand, the controller port's state "
                  "agrees, the marker in the data directory follows, the client is told [MSG:Tray in] or "
                  "[MSG:Tray out] and its next line draws ok, and the kernel's position and byte counters "
                  "do not move. In the other mode, a jog to the Z the lens read before the switch is "
                  "refused (error:15): the two Z ranges never overlap; back in the mode found, the same "
                  "jog is taken. A setup card started with the tray out is refused in the operator's "
                  "words, with nothing sent. The page "
                  "the panel serves carries the switch. The tray mode is put back as found in a finally. "
                  "The controller is not restarted: that would drop the X and Y reference. That the mode "
                  "survives a restart, the errors of M103, and its wait for the move before it are the "
                  "driver's harness cases.")
def tray(ctx):
    fc = ctx.forgectrl
    ev = ctx.evidence
    s0 = fc.status()
    found_mode = (s0.get("lens") or {}).get("tray")
    ctx.check(found_mode in ("in", "out"), "the status carries no tray mode: %s", s0.get("lens"))
    other = "out" if found_mode == "in" else "in"
    p_found = "P1" if found_mode == "out" else "P0"
    p_other = "P1" if other == "out" else "P0"
    shift = _grid_mm((s0.get("lens") or {}).get("tray_offset_mm") or 34.29) * (1 if other == "out" else -1)
    machine_idle(ctx)
    found = {"lens": s0.get("lens"), "pos_z": (s0.get("pos") or {}).get("z"),
             "port_z": _port_state(fc)["mpos"][2], "kernel": _kernel_position(),
             "home": [s0.get("homed_axes"), s0.get("home_source")]}
    ev["found"] = {"tray": found_mode, "shift_mm": round(shift, 4), **found}
    ctx.log("found the tray %s; switching to %s moves Z %+.3f mm", found_mode, other, shift)
    ctx.check(found["pos_z"] is not None, "the status carries no Z")

    st, page = fc.get("/", raw=True)
    text = page.decode("utf-8", "replace") if isinstance(page, bytes) else str(page)
    for want in ('id="traybtn"', "function trayToggle", "/motion/tray?tray="):
        ctx.check(st == 200 and want in text, "the panel page carries no %s", want)

    switched = False
    try:
        if found_mode == "out":
            _card_refused(ctx, fc, ev)
        with ctx.grbl() as g:
            clean_slate(ctx, g)
            ctx.sleep(0.6)                  # past the port's sender-quiet interval

            # Through the panel's route.
            st, words = _tray_route(fc, other)
            ctx.check(st == 200, "POST /motion/tray tray=%s -> %s %s", other, st, words)
            switched = True
            said = drain_text(g, 0.5)
            ctx.check("[MSG:Tray %s]" % other in said, "the client was not told the tray is %s: %r", other, said)
            _judge(ctx, fc, ev, "route_" + other, other, found, shift)
            r = g.command("G4 P0")
            ctx.check(r[-1:] == ["ok"], "the client's line after the switch drew %s", r)

            # The Z the lens read before the switch is outside this mode's range.
            jog = "$J=G90 G21 Z%.3f F200" % found["port_z"]
            r = g.command(jog)
            ev["jog_" + other] = [jog, r]
            ctx.check(r[-1:] == ["error:15"], "with the tray %s, %s drew %s, not error:15", other, jog, r)
            g.command("")                   # the empty line that acknowledges the error

            # Back through M103, from the client.
            r = g.command("M103 " + p_found)
            ev["m103_" + found_mode] = r
            ctx.check(r[-1:] == ["ok"] and "[MSG:Tray %s]" % found_mode in r,
                      "M103 %s drew %s", p_found, r)
            switched = False
            _judge(ctx, fc, ev, "m103_" + found_mode, found_mode, found, 0.0)
            r = g.command(jog)
            ev["jog_" + found_mode] = [jog, r]
            ctx.check(r[-1:] == ["ok"], "with the tray %s again, %s drew %s", found_mode, jog, r)
            machine_idle(ctx)

            # And the other way: M103 out, the route back.
            r = g.command("M103 " + p_other)
            ev["m103_" + other] = r
            ctx.check(r[-1:] == ["ok"] and "[MSG:Tray %s]" % other in r, "M103 %s drew %s", p_other, r)
            switched = True
            _judge(ctx, fc, ev, "m103_" + other, other, found, shift)

        if other == "out":
            _card_refused(ctx, fc, ev)

        st, words = _tray_route(fc, found_mode)
        ctx.check(st == 200, "POST /motion/tray tray=%s -> %s %s", found_mode, st, words)
        switched = False
        _judge(ctx, fc, ev, "route_" + found_mode, found_mode, found, 0.0)
    finally:
        if switched:
            # The route is refused for a moment after the suite's Grbl client closes.
            first = sys.exc_info()[1]
            took = ctx.wait_for(lambda: _tray_route(fc, found_mode)[0] == 200 or None, 20, poll=0.5)
            ctx.log("put the tray back %s -> %s", found_mode, "taken" if took is not None else "not taken")
            ev["put_back"] = took is not None
            if first is None:
                ctx.check(took is not None, "the tray mode is left %s, not put back %s", other, found_mode)
    machine_idle(ctx)
    k1 = _kernel_position()
    ev["kernel"] = [found["kernel"], k1]
    ctx.check(k1 == found["kernel"], "the kernel moved across the switches: %s -> %s", found["kernel"], k1)
    ctx.log("PASS: the tray mode switched both ways through the route and through M103, Z and the reach "
            "moved by %.3f mm and back, nothing moved, and it is %s as found", abs(shift), found_mode)
