# Copyright 2026 514 LLC d/b/a OpenGlow
# Written by Scott Wiederhold
# https://community.openglow.org
# SPDX-License-Identifier:    MIT

"""A camera home's offset pair, on the machine.

Its own module, so that no other test's fingerprint moves. The homing is
the web-service session cloud.mode-switch runs. The helpers for a test
that lets the service move the head live here too: the motions ran whole,
and the head goes back to where the test found it, by the travel the
service's motions logged. No counter reading can say where that was: every
service motion zeroes the step counters at its start, and a home and every
controller start zero them again.
"""

import re
import sys

from ..baseline import XY_STEPS_PER_MM, counter_frame, counter_steps_per_mm, read_position
from ..catalog import test
from ..runner import Failed
from . import cloud as logs     # the log paths, read at each call
from .cloud import RETURN_MAX_MM, _HOMING_PATH, gfhome_homing, log_lines_since, log_size
from .motion import _drop_reference, clean_slate, machine_idle, wait_idle, wait_state

HOME_X, HOME_Y = 4.5, -3.25
# The client's own record of a motion's end: the kernel counters, which
# the motion zeroed at its start (the "(actual/expected)" line of the same
# motion is not this one). A print's park keeps the counters and drives
# them back, so the line after it is the print's net travel.
MOTION_END = re.compile(r"end positions \((-?\d+), (-?\d+), (-?\d+)\)")


def session_mark(path=None):
    """Where a client's log ends now (gfhome's by default): the start of
    the next session's record."""
    return log_size(path or logs.GFHOME_LOG)


def session_travel(offset, path=None):
    """The X/Y travel of the service motions logged in a client's log
    (gfhome's by default) since offset, in steps, or None when it cannot be
    known. Every service motion zeroes the kernel counters at its start and
    logs where they ended, so the travel is the sum of those ends. A service
    motion runs at x8 or not at all (the client refuses any other mode), so
    the steps are x8 steps. A motion refused before it moved logs no end
    and moved nothing; a motion with no "end motion" (one that raised after
    it moved, or a client killed inside it) leaves the travel unknown, and
    so does a log rotated under the run."""
    path = path or logs.GFHOME_LOG
    if log_size(path) < offset:
        return None
    x = y = 0
    inside, end = False, None
    for ln in log_lines_since(path, offset):
        ln = ln.rstrip()
        m = MOTION_END.search(ln)
        if ln.endswith(" start motion"):
            if inside:
                return None
            inside, end = True, None
        elif m and inside:
            end = (int(m.group(1)), int(m.group(2)))
        elif ln.endswith(" end motion") and inside:
            if end:
                x, y = x + end[0], y + end[1]
            inside = False
    return None if inside else (x, y)


def judge_whole_motions(ctx, ev, offset):
    """Every service motion of the homing session since offset ran whole.
    One that stopped short (the cooling engine's stop, a fault) left the
    head short of the camera home, and the position declared there is
    false."""
    short = [ln.strip()[:160] for ln in log_lines_since(logs.GFHOME_LOG, offset)
             if "run stopped short" in ln
             or ("_finish_action motion [" in ln and 'event ":completed"' not in ln)]
    ev["short_motions"] = short
    ctx.check(not short, "a service motion of the homing did not run whole, so the head stopped short of "
                         "the camera home and the declared position is false: %s", short)


def camera_home_return(ctx, ev, offset):
    """Hand the head back to where the test found it after gfhome_homing
    ran from gfhome's log offset, whatever the session decided. The head is
    the session's logged travel from there, plus, once homed, whatever the
    counters read since: the home zeroed them where the session left the
    head and wrote the anchor, and a controller start since would have
    removed it. Unhomed, the counters still hold the last motion's end,
    which the travel already counts. The camera home is dropped first,
    since its envelope need not hold the spot the head came from."""
    def back():
        now = [0, 0, 0]
        if ev.get("homed"):
            now = read_position() if counter_frame() is not None else None
        _drop_reference(ctx, ctx.forgectrl)
        # Read after the stop: a runner the stop ended has written its last line.
        travel = session_travel(offset)
        rec = ev["return"] = {"session_travel_steps": travel, "counters_since_home": now}
        ctx.check(travel is not None and now is not None,
                  "the homing session's travel cannot be known (gfhome log %s, counters since the home %s): "
                  "the head is left where it stands and the hand-back moves nothing", travel, now)
        _jog_back(ctx, rec, travel, now)
    _hand_back(ctx, ev, "return", back)


def cloud_mode_return(ctx, ev, offset):
    """Hand the head back to where the test found it after a stay in cloud
    mode that began at the cloud client's log offset, on the GRBL
    controller the switch back started: that start zeroed the counters
    where the service left the head, and they hold whatever moved since.
    The client's record of its motions is the rest (a hunt's lens travel is
    Z, which a hand-back never touches)."""
    def back():
        travel = session_travel(offset, logs.GFCLOUD_LOG)
        now = read_position()
        rec = ev["cloud_return"] = {"session_travel_steps": travel, "counters_since_start": now}
        ctx.check(travel is not None and now is not None,
                  "the travel in cloud mode cannot be known (gfcloud log %s, counters %s): the head is left "
                  "where it stands and the hand-back moves nothing", travel, now)
        _jog_back(ctx, rec, travel, now)
    _hand_back(ctx, ev, "cloud_return", back)


def _hand_back(ctx, ev, key, back):
    """Run a hand-back from a test's finally. The first failure wins: when
    the test has failed already (the exception in flight), a hand-back that
    fails as well is logged and recorded, and the test's own failure is the
    one reported."""
    first = sys.exc_info()[1]
    try:
        back()
    except Failed as e:
        if first is None:
            raise
        ev.setdefault(key, {})["failed"] = str(e)
        ctx.log("the head is not back, and the run has failed already: %s", e)


def _jog_back(ctx, rec, travel, since):
    """Jog the head back by the service's travel (x8 steps) and what the
    counters read since (the controller's own steps), on a controller
    without a home. One more start then zeroes the counters where the head
    began, which the rest of the catalog counts on."""
    spm = counter_steps_per_mm()
    dx = travel[0] / XY_STEPS_PER_MM + since[0] / spm
    dy = travel[1] / XY_STEPS_PER_MM + since[1] / spm
    rec["mm"] = [round(dx, 3), round(dy, 3)]
    ctx.log("the head is %.3f/%.3f mm from where the test found it", dx, dy)
    ctx.check(abs(dx) <= RETURN_MAX_MM and abs(dy) <= RETURN_MAX_MM,
              "a travel of %.1f/%.1f mm exceeds %.0f mm - not jogging back", dx, dy, RETURN_MAX_MM)
    if abs(dx) >= 0.05 or abs(dy) >= 0.05:
        with ctx.grbl() as g:
            clean_slate(ctx, g)
            start = read_position()
            r = g.command("$J=G91 G21 X%.3f Y%.3f F2400" % (-dx, -dy))
            ctx.check(not any(x.startswith("error") for x in r), "the return jog was refused: %s", r)
            states = wait_idle(ctx, g, timeout=60)[1]
            ctx.check(states[-1:] != ["TIMEOUT"], "the return jog did not end: %s", states)
        machine_idle(ctx)
        end = read_position()
        rec["counters_across_jog"] = [start, end]
        ctx.check(start is not None and end is not None and abs((end[0] - start[0]) / spm + dx) < 0.1
                  and abs((end[1] - start[1]) / spm + dy) < 0.1,
                  "the return jog did not travel %.3f/%.3f mm: counters %s -> %s", -dx, -dy, start, end)
        _drop_reference(ctx, ctx.forgectrl)
    ctx.counters_rezeroed()
    ctx.log("head returned to where the test found it (%.3f/%.3f mm)", -dx, -dy)


@test("homing.cloud-offsets", title="A camera home declares its offset pair, and the envelope reaches back to a "
                                     "negative one",
      subsystem="homing", kind="auto", mode="grbl", est_min=7,
      covers=_HOMING_PATH + [("grblhal-glowforge", "src/glowforge_homing.*"),
                             ("grblhal-glowforge", "src/glowforge_cooling.*")],
      requires=["forgectrl.auth", "motion.pacing"],
      steps=["Bed clear, lid closed; cloud credentials configured; the machine on the network. The head "
             "travels to the camera home and back to where it was."],
      description="With cloud mode on, homing_mode = gfcloud and the camera-home offsets set to (4.5, -3.25), "
                  "$H runs the web-service homing session. Every service motion of the session runs whole: "
                  "the runner reports to the cooling engine in the controller's place, and one stopped short "
                  "(the engine's stop, a fault) leaves the head short of the home and the position declared "
                  "there false. When the session ends, the controller declares the position (4.5, -3.25): a "
                  "camera-home offset may be negative, and the declared position is snapped to the step "
                  "grid. The work envelope reaches back to the home on the negative axis, since the head "
                  "stands there: a jog 2 mm out in +Y and 2 mm back ends at the home, accepted, and the same "
                  "in X. The homing mode and the offsets are put back as found, the camera home is dropped, "
                  "and the head is jogged back to where the test found it by the travel the session's "
                  "motions logged; a travel that cannot be known fails the test and moves nothing. That an "
                  "offset past the axis travel is refused, and the envelope's edge on the grid, are the "
                  "driver's unit and harness cases.")
def cloud_offsets(ctx):
    fc = ctx.forgectrl
    ev = ctx.evidence
    s0 = fc.settings() or {}
    ctx.check(s0.get("cloud_enabled") == "1", "cloud mode is off: a camera home needs it on")
    found = {k: s0.get(k) or "" for k in ("homing_mode", "gfcloud_home_x", "gfcloud_home_y")}
    ev["found"] = found
    session_at = None
    try:
        for k, v in (("homing_mode", "gfcloud"), ("gfcloud_home_x", str(HOME_X)), ("gfcloud_home_y", str(HOME_Y))):
            st, body = fc.post("/settings", data={k: v})
            ctx.check(st == 200, "%s=%s -> %s %s", k, v, st, body)
        with ctx.grbl() as g:
            session_at = session_mark()
            gfhome_homing(ctx, ev, g)
            judge_whole_motions(ctx, ev, session_at)
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
        if session_at is not None:
            camera_home_return(ctx, ev, session_at)
