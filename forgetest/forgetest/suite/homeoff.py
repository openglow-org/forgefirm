# Copyright 2026 514 LLC d/b/a OpenGlow
# Written by Scott Wiederhold
# https://community.openglow.org
# SPDX-License-Identifier:    MIT

"""A camera home's offset pair, on the machine.

Its own module, so that no other test's fingerprint moves. The homing is
the web-service session cloud.mode-switch runs; the head ends parked at
the home, as it does there.
"""

from ..catalog import test
from .cloud import _HOMING_PATH, gfhome_homing
from .motion import wait_state

HOME_X, HOME_Y = 4.5, -3.25


@test("homing.cloud-offsets", title="A camera home declares its offset pair, and the envelope reaches back to a "
                                     "negative one",
      subsystem="homing", kind="auto", mode="grbl", est_min=5,
      covers=_HOMING_PATH + [("grblhal-glowforge", "src/glowforge_homing.*")],
      requires=["forgectrl.auth", "motion.pacing"],
      steps=["Bed clear, lid closed; cloud credentials configured; the machine on the network. The head ends "
             "parked at the home, as it does after cloud.mode-switch."],
      description="With cloud mode on, homing_mode = gfcloud and the camera-home offsets set to (4.5, -3.25), "
                  "$H runs the web-service homing session. When it ends, the controller declares the position "
                  "(4.5, -3.25): a camera-home offset may be negative, and the declared position is snapped to "
                  "the step grid. The work envelope reaches back to the home on the negative axis, since the head "
                  "stands there: a jog 2 mm out in +Y and 2 mm back ends at the home, accepted, and the same in "
                  "X. The homing mode and the offsets are put back as found. That an offset past the axis travel "
                  "is refused, and the envelope's edge on the grid, are the driver's unit and harness cases.")
def cloud_offsets(ctx):
    fc = ctx.forgectrl
    ev = ctx.evidence
    s0 = fc.settings() or {}
    ctx.check(s0.get("cloud_enabled") == "1", "cloud mode is off: a camera home needs it on")
    found = {k: s0.get(k) or "" for k in ("homing_mode", "gfcloud_home_x", "gfcloud_home_y")}
    ev["found"] = found
    try:
        for k, v in (("homing_mode", "gfcloud"), ("gfcloud_home_x", str(HOME_X)), ("gfcloud_home_y", str(HOME_Y))):
            st, body = fc.post("/settings", data={k: v})
            ctx.check(st == 200, "%s=%s -> %s %s", k, v, st, body)
        with ctx.grbl() as g:
            gfhome_homing(ctx, ev, g)
            mpos = g.status_report().get("MPos") or (None, None, None)
            ev["declared"] = mpos
            ctx.log("the camera home declared %s", mpos)
            ctx.check(mpos[0] is not None and abs(mpos[0] - HOME_X) < 0.02 and abs(mpos[1] - HOME_Y) < 0.02,
                      "the camera home did not declare (%.2f, %.2f): %s", HOME_X, HOME_Y, mpos)
            jogs = {}
            for axis, home in (("Y", HOME_Y), ("X", HOME_X)):
                out = g.command("$J=G91 G21 %s2 F1000" % axis, timeout=10)
                wait_state(ctx, g, "Idle", 20)
                back = g.command("$J=G91 G21 %s-2 F1000" % axis, timeout=10)
                wait_state(ctx, g, "Idle", 20)
                at = g.status_report().get("MPos")
                jogs[axis] = {"out": out[-1:], "back": back[-1:], "at": at}
                ctx.check(out[-1:] == ["ok"] and back[-1:] == ["ok"],
                          "a jog out and back to the home in %s was refused: %s", axis, jogs[axis])
                i = "XY".index(axis)
                ctx.check(at and abs(at[i] - home) < 0.02, "the jog back in %s did not end at the home: %s", axis, at)
            ev["jogs"] = jogs
            ctx.log("out and back to the home: %s", jogs)
    finally:
        # The settings are refused for a moment after the suite's Grbl client closes; each is put back when taken.
        for k, v in found.items():
            def put():
                st_, body_ = (fc.post("/settings", params={k: ""}) if v == "" else fc.post("/settings", data={k: v}))
                if st_ != 200:
                    ctx.log("restore %s=%r -> %s %s", k, v, st_, body_)
                return st_ == 200 or None
            took = ctx.wait_for(put, 20, poll=0.5)
            ctx.log("restore %s=%r -> %s", k, v, "taken" if took is not None else "not taken")
