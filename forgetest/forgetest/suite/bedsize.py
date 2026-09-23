# Copyright 2026 514 LLC d/b/a OpenGlow
# Written by Scott Wiederhold
# https://community.openglow.org
# SPDX-License-Identifier:    MIT

"""The Setup page's bed check, on the machine.

Its own module, so that no other test's fingerprint moves. The check is
driven from here inside the travel the machine already knows: a few jogs
out from the camera home, never toward an end, so it needs no operator.
Measuring the real ends is the operator's own run of the check. The
homing is the web-service session homing.cloud-offsets runs.
"""

import json
import time

from ..catalog import test
from .cloud import _HOMING_PATH, gfhome_homing
from .setup import read_file, record_path, write_file
from .setup_dark import DARK_COVERS, answer, dark, start

WID = "motion.envelope"
END = "This is the end"
# The operator's answers, as the page sends them: out 60.1 mm in X and
# 61.1 mm in Y from the home, the fine steps included. A far edge is 50 mm
# at the least, so the ends are well past that and well inside the bed.
X_STEPS = ["X+10"] * 6 + ["X+1", "X+0.1", "X-1"]
Y_STEPS = ["Y+10"] * 6 + ["Y+1", "Y+0.1"]
X_OUT, Y_OUT = 60.1, 61.1
MARGIN_MM = 1.0
# The head is back on the home's step: half a step (213.3 steps/mm) and
# the port's three decimals.
HOME_TOL_MM = 0.003
KEYS =("envelope_x_mm", "envelope_y_mm", "homing_mode")
CLOSED = "closed the check's envelope"
CLOSED_MSG = "Bed check envelope closed: a sender line"


def _port(fc):
    st, body = fc.get("/motion/state")
    return body if st == 200 and isinstance(body, dict) else {}


def _drive(ctx, fc, x_steps, y_steps, hook=None, timeout=300):
    """Start the check and answer each prompt from the steps in turn, then
    This is the end; hook(prompt_id, n) runs before the answer to the n-th
    prompt. The final status document, and the answers given."""
    # The daemon calls the machine busy for a moment after the last jog.
    ctx.check(fc.wait_idle(timeout=30, abort=ctx.aborted), "the machine is not idle: %s", fc.status().get("state"))
    start(ctx, fc, WID)
    queue = {"x-end": list(x_steps) + [END], "y-end": list(y_steps) + [END]}
    seen, given = set(), []
    t0 = time.time()
    try:
        while time.time() - t0 < timeout:
            ctx.checkpoint()
            d = dark(fc)
            p = d.get("prompt")
            if d.get("running") and p and p.get("seq") not in seen:
                seen.add(p["seq"])
                pid = p.get("id")
                ctx.check(pid in queue and queue[pid], "the check asked %s: %s (the lid must be closed)",
                          pid, p.get("text"))
                ctx.check(p.get("kind") == "jog" and END in (p.get("options") or []),
                          "the %s prompt is not a jog with %r: %s", pid, END, p)
                if hook:
                    hook(pid, len(given))
                value = queue[pid].pop(0)
                st, body = answer(fc, WID, p, value)
                ctx.check(st == 200, "the answer %s to %s -> %s %s", value, pid, st, body)
                given.append(value)
            if not d.get("running"):
                return d, given
            time.sleep(0.3)
    except BaseException:
        fc.post("/wiz/%s/abort" % WID)
        raise
    fc.post("/wiz/%s/abort" % WID)
    ctx.fail("the bed check did not end within %d s", timeout)


def _jog(fc, dx=0.0, dy=0.0):
    params = {"feed": "1200"}
    if dx:
        params["x"] = "%.3f" % dx
    if dy:
        params["y"] = "%.3f" % dy
    return fc.post("/motion/jog", params=params)


def _settled(ctx, fc):
    """After the suite's Grbl client closes, the daemon calls the machine
    busy for a moment: the check and a settings write each wait for it."""
    ctx.wait_for(lambda: _port(fc).get("sender") is False, 20, poll=0.5)
    ctx.check(fc.wait_idle(timeout=30, abort=ctx.aborted), "the machine is not idle: %s", fc.status().get("state"))


def _put_back(ctx, fc, found):
    for k in KEYS:
        v = found.get(k) or ""

        def put():
            st_, body_ = fc.post("/settings", params={k: ""}) if v == "" else fc.post("/settings", data={k: v})
            if st_ != 200:
                ctx.log("restore %s=%r -> %s %s", k, v, st_, body_)
            return st_ == 200 or None
        took = ctx.wait_for(put, 20, poll=0.5)
        ctx.log("restore %s=%r -> %s", k, v, "taken" if took is not None else "not taken")


def _back_home(ctx, fc, home):
    """The head back where the home put it: one jog to the home in machine
    coordinates, from a Grbl client. A relative jog aimed at the home from
    the port's position can be refused whole: the port reports the step
    counters, the core adds a relative jog to its own position, which the
    check's jogs leave up to half a step off the counters, and the envelope
    begins at the home, so the target can land microns behind it (error:15).
    Where the head stands after, from the port; None when unread."""
    # 0.5 um inside an envelope that begins at the home (a camera home
    # behind the origin), far under half a step: the head lands on the
    # home's own step.
    line = "$J=G90 G53 X%.4f Y%.4f F1200" % (home[0] + 0.0005, home[1] + 0.0005)
    try:
        with ctx.grbl() as g:
            reply = g.command(line, timeout=10)
            ctx.log("the jog back to the home: %s -> %s", line, " | ".join(reply))
            if reply[-1:] == ["ok"]:
                t0 = time.time()
                ctx.wait_for(lambda: time.time() - t0 > 0.5 and g.status_report()["state"].startswith("Idle"),
                             60, poll=0.2)
    except (OSError, ValueError) as e:
        ctx.log("the jog back to the home: %s", e)
    ctx.wait_for(lambda: _port(fc).get("sender") is False and _port(fc).get("state") == "Idle", 20, poll=0.5)
    at = _port(fc).get("mpos")
    ctx.log("the head after the jog back: %s (the home %s)", at, home)
    return at


def _port_idle(ctx, fc):
    ok = ctx.wait_for(lambda: (lambda s: s.get("state") == "Idle" and not s.get("port_jog"))(_port(fc)), 30,
                      poll=0.1)
    ctx.check(ok is not None, "the port jog did not end: %s", _port(fc))


def _edge(ctx, fc, ev, axis, edge):
    """From where the head stands, a jog 0.5 mm short of the far edge runs
    and comes back, and one 0.5 mm past it is refused with nothing moved."""
    s = _port(fc)
    at = s["mpos"]["XY".index(axis)]
    short, past = edge - 0.5 - at, edge + 0.5 - at
    kw = (lambda d: {"dx": d}) if axis == "X" else (lambda d: {"dy": d})
    st1, b1 = _jog(fc, **kw(short))
    _port_idle(ctx, fc)
    st2, b2 = _jog(fc, **kw(-short))
    _port_idle(ctx, fc)
    st3, b3 = _jog(fc, **kw(past))
    _port_idle(ctx, fc)
    back = _port(fc)["mpos"]["XY".index(axis)]
    ev["edge_" + axis] = {"edge": edge, "short": [st1, st2], "past": [st3, b3], "at": [at, back]}
    ctx.check(st1 == 200 and st2 == 200, "a jog 0.5 mm short of the %s edge %.2f and back -> %s %s, %s %s",
              axis, edge, st1, b1, st2, b2)
    ctx.check(st3 == 409 and "envelope" in json.dumps(b3) and abs(back - at) < 0.02,
              "a jog 0.5 mm past the %s edge %.2f -> %s %s, the head at %.3f from %.3f", axis, edge, st3, b3, back, at)


@test("setup.check-envelope", title="The bed check measures the far edges from the operator's jogs, and a Grbl "
                                     "client's line ends it with nothing written",
      subsystem="setup", kind="auto", mode="grbl", est_min=10,
      covers=DARK_COVERS + _HOMING_PATH + [("forgectrl", "src/grblport.*"),
                                           ("grblhal-glowforge", "src/glowforge_homing.*"),
                                           ("grblhal-glowforge", "src/ctlport.*"),
                                           ("grblhal-glowforge", "src/driver.c")],
      requires=["forgectrl.auth", "motion.pacing"],
      steps=["Bed clear, lid closed; cloud credentials configured; the machine on the network. The head moves "
             "at most about 62 mm out from the camera home in X and in Y."],
      description="With cloud mode on, the machine is homed with the camera (the web-service session). "
                  "POST /wiz/motion.envelope/start runs the Bed size check; its prompts are jogs with This is the "
                  "end among the options, and are answered as the page would, out 60.1 mm in X and 61.1 mm in Y "
                  "(steps of 10, 1, and 0.1 mm). The check ends complete: the ends it reports are the home plus "
                  "those, the far edges 1 mm short of them are written as envelope_x_mm and envelope_y_mm, and the "
                  "controller holds them at once, without a home: a panel jog 0.5 mm short of each edge runs, "
                  "and one 0.5 mm past it is refused with nothing moved. A second run is opened, and while its "
                  "first prompt waits a Grbl client sends one line, which draws the controller's word that it "
                  "closed the envelope and then ok; the port's state then says the envelope is closed, and the "
                  "next answer ends the check in words, with the keys as the "
                  "first run left them and the edges still in force. The keys, the homing mode, and the setup "
                  "record are put back as found, the record under a restart, and the head goes back to the "
                  "home by a Grbl client's jog in machine coordinates; the run fails unless the port reads "
                  "the head on the home's step before the restart.")
def check_envelope(ctx):
    fc = ctx.forgectrl
    ev = ctx.evidence
    s0 = fc.settings() or {}
    ctx.check(s0.get("cloud_enabled") == "1", "cloud mode is off: a camera home needs it on")
    raw = read_file(record_path())
    found = {k: s0.get(k) or "" for k in KEYS}
    ev["found"] = found
    home = back = None
    try:
        st, body = fc.post("/settings", data={"homing_mode": "gfcloud"})
        ctx.check(st == 200, "homing_mode=gfcloud -> %s %s", st, body)
        with ctx.grbl() as g:
            gfhome_homing(ctx, ev, g)
            home = g.status_report().get("MPos")
        ev["home"] = home
        ctx.log("the camera home declared %s", home)
        ctx.check(home and home[0] is not None, "no position after the home: %s", home)
        _settled(ctx, fc)

        last, given = _drive(ctx, fc, X_STEPS, Y_STEPS)
        r = last.get("result") or {}
        ev["run1"] = {"answers": given, "error": last.get("error"), "result": r, "log": last.get("log", [])[-8:]}
        ctx.log("the bed check ended: %s", json.dumps(r))
        ctx.check(not last.get("error"), "the bed check failed: %s", last.get("error"))
        want_x, want_y = home[0] + X_OUT, home[1] + Y_OUT
        ctx.check(abs((r.get("x_end_mm") or -1) - want_x) < 0.02 and abs((r.get("y_end_mm") or -1) - want_y) < 0.02,
                  "the ends are not the home plus the jogs (%.2f, %.2f): %s", want_x, want_y, r)
        edge_x, edge_y = round(want_x - MARGIN_MM, 2), round(want_y - MARGIN_MM, 2)
        s1 = fc.settings() or {}
        ev["keys_after_run1"] = {k: s1.get(k) for k in ("envelope_x_mm", "envelope_y_mm")}
        ctx.check(abs(float(s1.get("envelope_x_mm") or 0) - edge_x) < 0.011 and
                  abs(float(s1.get("envelope_y_mm") or 0) - edge_y) < 0.011,
                  "the keys are not the ends less %.1f mm (%.2f, %.2f): %s", MARGIN_MM, edge_x, edge_y,
                  ev["keys_after_run1"])
        ctx.check(_port(fc).get("envelope_open") is False, "the envelope is still open after the check: %s",
                  _port(fc))
        _edge(ctx, fc, ev, "X", edge_x)
        _edge(ctx, fc, ev, "Y", edge_y)

        # A Grbl client's line while the envelope is open ends the check.
        said = {}

        def client_line(pid, n):
            if n:
                return
            ctx.check(_port(fc).get("envelope_open") is True, "the second run did not open the envelope: %s",
                      _port(fc))
            with ctx.grbl() as g2:
                said["reply"] = g2.command("G90", timeout=10)
                said["text"] = g2.drain()
            said["port"] = _port(fc)

        last2, given2 = _drive(ctx, fc, ["X+1"], [], hook=client_line)
        ev["run2"] = {"answers": given2, "said": said, "error": last2.get("error"), "log": last2.get("log", [])[-6:]}
        ctx.log("the second run: %s", json.dumps(ev["run2"]))
        ctx.check((said.get("reply") or [])[-1:] == ["ok"], "the client's line did not draw ok: %s", said)
        ctx.check(any(CLOSED_MSG in l for l in (said.get("reply") or [])) or CLOSED_MSG in (said.get("text") or ""),
                  "the client was not told its line closed the envelope: %s", said)
        ctx.check((said.get("port") or {}).get("envelope_open") is False,
                  "the client's line did not close the envelope: %s", said.get("port"))
        ctx.check(CLOSED in (last2.get("error") or ""), "the second run did not end on the closed envelope: %s",
                  last2.get("error"))
        s2 = fc.settings() or {}
        ctx.check({k: s2.get(k) for k in ("envelope_x_mm", "envelope_y_mm")} == ev["keys_after_run1"],
                  "the second run wrote the keys: %s", s2)
        _settled(ctx, fc)
        _edge(ctx, fc, ev, "X", edge_x)
    finally:
        fc.post("/wiz/%s/abort" % WID)
        if home and home[0] is not None:
            ctx.wait_for(lambda: not dark(fc).get("running"), 30, poll=0.5)
            back = _back_home(ctx, fc, home)
        _put_back(ctx, fc, found)
        # The check records itself when it completes; the record goes back
        # as found under a restart, which also ends the envelope it set.
        if read_file(record_path()) != raw:
            with ctx.takeover():
                write_file(record_path(), raw)
            ctx.log("the previous setup record is back under a restart")
    # Judged from the reading before the restart: the restart zeroes the
    # counters where the head stands, so the baseline's position check
    # after it cannot see a head left out.
    ev["back"] = back
    ctx.check(back and all(abs(back[i] - home[i]) < HOME_TOL_MM for i in (0, 1)),
              "the head is not back at the home %s: %s", home, back)
