#!/usr/bin/env python3
# Copyright 2026 514 LLC d/b/a OpenGlow
# Written by Scott Wiederhold
# https://community.openglow.org
# SPDX-License-Identifier:    MIT
"""Host-side verification of the measured work envelope and the bed check's port op.

Runs the native grblHAL_glowforge binary in null-sink mode, with the
scripted sender and the port client of ctlport_test.py, homing manually
(nothing moves; X and Y become homed and the envelope is set):

  1. envelope_x_mm and _y set the far edges at a home, and a jog past one
     is the core's error:15 (an unset key being the travel, and a key held
     to GFHOME_ENVELOPE_MIN_MM and to the travel plus
     GFHOME_ENVELOPE_EXTRA_MM, are lens_home_test.c's in grblHAL-glowforge)
  2. the port's envelope open: refused before a home (error:homed); after
     one, the port's jogs reach the travel plus 30 mm for X and Y, and the
     port's state says the envelope is open; envelope apply puts the keys'
     edges back without a home
  3. an open envelope is the port's jogs' alone: a status poll and an
     empty line leave it open, and the sender's first line closes it before
     the core reads the line (the line then answered ok, and the keys'
     edges in force), a port jog it meets canceled
  4. a soft reset and the port client going away each close it
  5. envelope open is refused under an open armed window (busy:state)
  6. late step events outside a window drop a reference when they owed
     its axis a full step, and only that one, and the sender is told
     which: a stall the controller is asked for (GFSINK_STALL_MS) in a fast
     X jog drops X and Y (they are no longer homed, and a jog past the X
     travel runs) and keeps the lens (a lens jog runs); one in a lens jog
     drops the lens (a lens jog is refused, error:15) and keeps X and Y
     (still homed). The threshold is the microstep mode read at the start: one
     stall in a slow X jog owes X a full step at x8 and drops X and Y, and
     owes under one at x32 and keeps them, judged against each run's
     logged count of late events (a count outside [8, 31] proves nothing,
     and the pair runs once more). A dropped lens holds one step: while its
     jog still runs, the blocks planned before the drop run to their end
     whatever a G53 lens jog sent then asks, and an X-only jog sent then
     runs; a soft reset then stops it short, and it is held there; at
     rest, after late steps and after a stream fault in a lit
     move (the hook's path for a kernel underrun too), lens jogs are
     refused and an X jog inside the travel runs. These sessions ask for
     the stall, so the starve rule never reads them.

Every case but the not-homed one and the late-steps ones stands on a home:
a check that fails after a starve dropped the homing reference runs it
once more from a new controller (ctlport_test's rule).

Usage: envelope_test.py [path/to/grblHAL_glowforge]
"""
import os
import re
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import ctlport_test as ct  # noqa: E402

fail = ct.fail
TRAVEL_X, TRAVEL_Y, EXTRA = 495.0, 279.0, 30.0


def conf(s, **keys):
    with open(os.path.join(s.workdir, "forgefirm.conf"), "w") as f:
        f.write("cool_fan_grace_s = 0\nlaser_disarm_s = 30\nhoming_mode = manual\n")
        for k, v in keys.items():
            f.write("%s = %s\n" % (k, v))


def home(s):
    if s.sender.send("$H", timeout=10) != "ok":
        fail("manual $H refused")
    s.sender.wait_state("Idle")


def jog_to(s, axis, mm):
    """The sender's absolute jog; its response (ok or error:N), and back to 10 mm when it ran."""
    r = s.sender.send("$J=G90 G21 %s%.3f F6000" % (axis, mm))
    s.sender.wait_state("Idle")
    if r == "ok":
        s.sender.send("$J=G90 G21 %s10 F6000" % axis)
        s.sender.wait_state("Idle")
    return r


def edge(s, axis, inside, outside, what):
    a, b = jog_to(s, axis, inside), jog_to(s, axis, outside)
    if a != "ok" or b != "error:15":
        fail("[%s] %s: a jog to %.2f -> %s, to %.2f -> %s" % (what, axis, inside, a, outside, b))


def port_idle(s, timeout=20.0):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        st = s.port.state()
        if st["state"] == "Idle" and not st["port_jog"]:
            return st
        time.sleep(0.05)
    fail("the port never saw Idle: %r" % s.port.state())


def port_jog_to(s, axis, mm):
    """The port's absolute jog, which leaves an open envelope open."""
    r = s.port.request("jog G90 G21 %s%.3f F6000" % (axis, mm))
    port_idle(s)
    if r == "ok":
        s.port.request("jog G90 G21 %s10 F6000" % axis)
        port_idle(s)
    return r


def port_edge(s, axis, inside, outside, what):
    a, b = port_jog_to(s, axis, inside), port_jog_to(s, axis, outside)
    if a != "ok" or b != "error:15":
        fail("[%s] %s: a port jog to %.2f -> %s, to %.2f -> %s" % (what, axis, inside, a, outside, b))


def envelope_open(s):
    return s.port.state()["envelope_open"]


def open_envelope(s, what):
    s.quiet()
    r = s.port.request("envelope open")
    if r != "ok" or not envelope_open(s):
        fail("[%s] envelope open after a home -> %r, state %r" % (what, r, s.port.state()))


def saw_within(s, needle, seconds=2.0):
    end = time.monotonic() + seconds
    while not s.sender.saw(needle) and time.monotonic() < end:
        time.sleep(0.05)
    return s.sender.saw(needle)


def test_measured(s):
    conf(s, envelope_x_mm=480, envelope_y_mm=284.5)
    home(s)
    edge(s, "X", 479, 481, "measured")
    edge(s, "Y", 284, 285, "measured")
    print("PASS [measured]: envelope_x_mm 480 and envelope_y_mm 284.5 (past the travel) are the far edges at a home")


def test_open_apply(s):
    conf(s, envelope_x_mm=480, envelope_y_mm=250)
    home(s)
    before = s.sender.saw("Envelope open for the bed check")
    open_envelope(s, "open")
    if saw_within(s, "Envelope open for the bed check") <= before:
        fail("[open] the sender got no [MSG:] for the open envelope: %r" % s.sender.other[-4:])
    port_edge(s, "X", TRAVEL_X + EXTRA - 1, TRAVEL_X + EXTRA + 1, "open")
    port_edge(s, "Y", TRAVEL_Y + EXTRA - 1, TRAVEL_Y + EXTRA + 1, "open")
    if not envelope_open(s):
        fail("[open] the port's own jogs closed the envelope")
    r = s.port.request("envelope apply")
    if r != "ok" or envelope_open(s):
        fail("[apply] envelope apply -> %r, state %r" % (r, s.port.state()))
    edge(s, "X", 479, 481, "apply")
    edge(s, "Y", 249, 251, "apply")
    print("PASS [open-apply]: open, the port's jogs reach the travel plus 30 mm; apply, the keys' edges again, "
          "no home")


def test_sender_closes(s):
    conf(s, envelope_x_mm=480, envelope_y_mm=250)
    home(s)
    open_envelope(s, "sender")
    s.sender.state()                        # '?' is realtime
    s.sender.realtime(b"\n")                # and an empty line no claim on the machine
    time.sleep(0.5)
    if not envelope_open(s):
        fail("[sender] a status poll or an empty line closed the envelope")
    before = s.sender.saw("Bed check envelope closed")
    r = s.sender.send("$J=G90 G21 X%.3f F6000" % (TRAVEL_X + EXTRA - 1))
    s.sender.wait_state("Idle")
    if r != "error:15":
        fail("[sender] the sender's jog into the open envelope's margin -> %r: the core read it before the "
             "envelope closed" % r)
    if envelope_open(s) or saw_within(s, "Bed check envelope closed") <= before:
        fail("[sender] the sender's line did not close the envelope: %r" % s.port.state())
    edge(s, "X", 479, 481, "sender")
    # A port jog running when the sender's line arrives is canceled first.
    open_envelope(s, "sender-jog")
    x0 = s.port.state()["mpos"][0]
    if s.port.request("jog G91 G21 X300 F6000") != "ok":
        fail("[sender-jog] the long port jog was refused")
    time.sleep(0.7)
    # A jog line: the core holds a G-code line's parser error for the next
    # one, and the edge above ended in error:15.
    r = s.sender.send("$J=G90 G21 X20 F6000", timeout=10.0)
    s.sender.wait_state("Idle")
    st = s.port.state()
    if r != "ok" or st["envelope_open"] or st["port_jog"] or abs(st["mpos"][0] - 20) > 0.01 or x0 > 20:
        fail("[sender-jog] the sender's line during a port jog -> %r, state %r" % (r, st))
    print("PASS [sender]: a status poll and an empty line leave the envelope open; the sender's first line "
          "closes it before the core reads it, a running port jog canceled first")


def test_reset_closes(s):
    conf(s, envelope_x_mm=480, envelope_y_mm=250)
    home(s)
    open_envelope(s, "reset")
    s.sender.realtime(b"\x18")
    time.sleep(1.0)
    if envelope_open(s):
        fail("[reset] a soft reset left the envelope open")
    if s.sender.state() == "Alarm":
        s.sender.send("$X")
    s.sender.wait_state("Idle")
    edge(s, "X", 479, 481, "reset")
    open_envelope(s, "client")
    s.port.close()
    s.port = ct.PortClient(s.path)
    if envelope_open(s):
        fail("[client] the port client went away and the envelope stayed open")
    print("PASS [reset]: a soft reset and the port client going away each closed the open envelope")


def test_not_homed():
    s = ct.Session()
    try:
        conf(s)
        s.quiet()
        r = s.port.request("envelope open")
        if r != "error:homed":
            fail("[not-homed] envelope open before a home -> %r" % r)
        print("PASS [not-homed]: envelope open before a home -> error:homed")
    finally:
        s.close()


def test_armed():
    s = ct.Session(laser=True, homed=True)
    try:
        conf(s)
        home(s)
        s.sender.send("G91")
        s.sender.send("M3 S500")
        s.sender.send("G1 X5 F1500")
        s.sender.wait_state("Idle")
        time.sleep(0.5)
        if not s.sender.saw("laser armed"):
            fail("[armed] the laser line did not arm: the case would prove nothing")
        s.quiet()
        r = s.port.request("envelope open")
        if r != "busy:state":
            fail("[armed] envelope open under an open armed window -> %r" % r)
        print("PASS [armed]: envelope open under an open armed window -> busy:state")
    finally:
        s.close()


STALL_MODE_MS = 300     # the one stall the x8 and x32 cases share
DROP_XY_MSG = "late step events: X and Y are no longer trusted - home again"
DROP_Z_MSG = "late step events: Z is no longer trusted - the lens holds until it is referenced again"


def status(s):
    """The sender's status report: its state and its machine position."""
    n = len(s.sender.other)
    state = s.sender.state()
    for line in reversed(s.sender.other[n:]):
        m = re.match(r"<([A-Za-z]+)\|MPos:([-\d.]+),([-\d.]+),([-\d.]+)", line)
        if m:
            return m.group(1), [float(v) for v in m.group(2, 3, 4)]
    return state, None


def lens_moves_on(s, z0, up, reset):
    """While the lens jog under the stall still runs, as soon as the sender
    is told the lens is dropped: a G53 lens jog to the Z the status shows
    then, and an X-only jog. Every block planned before the drop runs, so
    the lens ends at the jog's end whatever the G53 jog asked; the X jog
    runs. With reset, a soft reset (and $X) instead, which stops the lens
    short of the jog's end: the hold follows it there (the jogs after the
    drop are judged from it). Returns (judged: the drop came with the jog
    still running, and a reset stopped it short), and the failure, or when
    not judged, why."""
    spm = float(next(o for o in s.sender.other if o.startswith("$102=")).split("=")[1])
    end_z = round(round((z0 + up) * spm) / spm, 3)
    end = time.monotonic() + 10.0
    while not s.sender.saw(DROP_Z_MSG) and time.monotonic() < end:
        time.sleep(0.01)
    state, pos = status(s)
    if state != "Jog" or pos is None:
        return False, "the drop was told at %s, Z %s" % (state, pos and pos[2])
    if reset:
        s.sender.realtime(b"\x18")
        time.sleep(1.0)
        if s.sender.state() == "Alarm":
            s.sender.send("$X")
        s.sender.wait_state("Idle", timeout=30.0)
        _, last = status(s)
        if last[2] < end_z - 0.5 / spm:
            return True, None
        return False, "the drop was told at Z%.3f, and the reset left the lens at Z%.3f (the jog's end %.3f)" \
            % (pos[2], last[2], end_z)
    g53 = s.sender.send("$J=G53 G21 Z%.5f F200" % pos[2])
    xonly = s.sender.send("$J=G91 G21 X1 F6000")
    s.sender.wait_state("Idle", timeout=30.0)
    _, last = status(s)
    if xonly != "ok" or abs(last[2] - end_z) > 0.002 or abs(last[0] - 1.0) > 0.01:
        return True, ("the lens dropped at Z%.3f with the jog running: a G53 jog there -> %s, an X-only jog -> %s; "
                      "the lens ended at Z%.3f (the jog's end %.3f), X at %.3f" % (pos[2], g53, xonly, last[2],
                                                                                  end_z, last[0]))
    return True, None


def late_steps(stall_ms, jog, what, mode=32, mid=None, pre=(), up=0.0):
    """A controller at microstep mode `mode`, its lens referenced on the hall
    edge, homed, then asked for one producer stall of stall_ms, half a
    second into its first run longer than that, and one jog under it. The
    mode is read at the controller's start, so the settings are written
    before it. Returns what the sender was told (X and Y's drop, the
    lens's), whether X and Y are still homed, the run's logged count of
    late events, how the jogs after the drop and a jog past the X travel
    were answered, and with mid, lens_moves_on's verdict."""
    s = ct.Session(env_extra={"GFSINK_STALL_MS": str(stall_ms)}, conf_extra="xy_microsteps = %d\n" % mode,
                   state_files={"lens.home": "edge 0 3 3\n"})
    try:
        home(s)
        r = {}
        for line in pre:
            if s.sender.send(line, timeout=10.0) != "ok":
                fail("[%s] %s was refused" % (what, line))
            s.sender.wait_state("Idle", timeout=30.0)
        if mid:
            s.sender.send("$102")
            z0 = status(s)[1][2]
        if s.sender.send(jog, timeout=10.0) != "ok":
            fail("[%s] the jog under the stall was refused" % what)
        if mid:
            r["mid"] = lens_moves_on(s, z0, up, mid == "reset")
        s.sender.wait_state("Idle", timeout=30.0)
        end = time.monotonic() + 3.0
        while not re.search(r"run: \d+ late events clamped", s.watch.text()) and time.monotonic() < end:
            time.sleep(0.05)
        m = re.search(r"run: (\d+) late events clamped", s.watch.text())
        if not m:
            fail("[%s] the stall clamped no late event: the case proves nothing" % what)
        r["count"] = int(m.group(1))
        r["told"] = (bool(saw_within(s, DROP_XY_MSG, 1.0)), bool(saw_within(s, DROP_Z_MSG, 0.2)))
        r["xy_homed"] = s.port.state()["homed"] & 3 == 3
        r["lens"], r["small"], r["inside"] = after_drop(s)
        r["past"] = jog_to(s, "X", TRAVEL_X + 10)
        return r
    finally:
        s.close()


def after_drop(s):
    """The jogs after a drop: a lens jog of half a millimeter down, one of
    0.2 mm (under one 0.34 mm half-step, but neither rounds to the step a
    held lens stands on, so a held lens refuses both), and an X jog of 5 mm
    inside the travel. Their answers."""
    out = []
    for jog in ("$J=G91 G21 Z-0.5 F200", "$J=G91 G21 Z-0.2 F200", "$J=G91 G21 X5 F6000"):
        out.append(s.sender.send(jog))
        s.sender.wait_state("Idle")
    return tuple(out)


def judge_late(r, what, want):
    """A late_steps result, judged: want is (X and Y dropped, the lens
    dropped). Dropped X and Y are no longer homed and a jog past the X
    travel runs; kept ones stay homed. A dropped lens refuses both lens
    jogs, a kept one runs them. An X jog inside the travel runs whatever
    was dropped."""
    drop_xy, drop_z = want
    zwant = "error:15" if drop_z else "ok"
    if r["told"] != want or r["xy_homed"] == drop_xy or r["lens"] != zwant or r["small"] != zwant or \
            r["inside"] != "ok" or r["past"] != ("ok" if drop_xy else "error:15"):
        fail("[%s] want X and Y %s and the lens %s (%d late events): told X and Y, the lens %r; X and Y homed "
             "%r; lens jogs -> %s, %s; an X jog inside the travel -> %s; a jog past the X travel -> %s"
             % (what, "dropped" if drop_xy else "kept", "dropped" if drop_z else "kept", r["count"], r["told"],
                r["xy_homed"], r["lens"], r["small"], r["inside"], r["past"]))


def expect_late(stall_ms, jog, what, want):
    judge_late(late_steps(stall_ms, jog, what), what, want)


def expect_lens_drop(mid):
    """The lens dropped with its jog running (late steps that owed it two
    half-steps), judged after the drop as expect_late does and, while the
    jog runs, by lens_moves_on (mid "run": lines sent then; "reset": a soft
    reset then). A drop that came after the jog had ended, or a reset that
    did not stop it short, proves nothing about the motion and runs once
    more."""
    if mid == "run":
        what, pre, up = "late-z", (), 3.4
    else:
        # From three millimeters under the edge, so that most of the jog is
        # still to come when the drop is told.
        what, pre, up = "late-z-reset", ("$J=G91 G21 Z-1.0 F200",) * 3, 6.5
    for attempt in (1, 2):
        r = late_steps(500, "$J=G91 G21 Z%.1f F200" % up, what, mid=mid, pre=pre, up=up)
        judged, failure = r["mid"]
        if judged:
            if failure:
                fail("[%s] %s" % (what, failure))
            judge_late(r, what, (False, True))
            return
        print("RERUN: [%s] %s: the motion case proves nothing" % (what, failure))
    fail("[%s] twice the drop came too late in the jog: the case proves nothing" % what)


def expect_late_pair():
    """One stall in a slow X jog at x8 and at x32, judged against each
    run's logged count of late events (each an X microstep here): a count
    in [8, 31] owes X a full step at x8 and under one at x32, so x8 must
    drop X and Y and x32 must keep them. A count outside it proves nothing
    about the pair, which runs once more."""
    jog = "$J=G91 G21 X2 F60"
    for attempt in (1, 2):
        r8 = late_steps(STALL_MODE_MS, jog, "late-x8", mode=8)
        r32 = late_steps(STALL_MODE_MS, jog, "late-x32", mode=32)
        if all(8 <= r["count"] <= 31 for r in (r8, r32)):
            judge_late(r8, "late-x8", (True, False))
            judge_late(r32, "late-x32", (False, False))
            return
        print("RERUN: [late-x8/x32] late events %d at x8, %d at x32, outside [8, 31]: the pair proves nothing"
              % (r8["count"], r32["count"]))
    fail("[late-x8/x32] the late events fell outside [8, 31] twice: the pair proves nothing")


def late_fault():
    """A stream fault in a lit move with Z (the producer stalled inside the
    armed window: the hook's fault path, the one a kernel underrun takes):
    the reference drops, ALARM:17, then a reset and $X. The lens is held
    where the move left it: both lens jogs are refused, and an X jog inside
    the travel runs (X and Y are no longer homed, by the fault)."""
    s = ct.Session(laser=True, env_extra={"GFSINK_STALL_MS": "300"}, state_files={"lens.home": "edge 0 3 3\n"})
    s.watch.armed_check = False             # the session asks for the stall
    try:
        home(s)
        for line in ("G91", "M3 S500", "G1 X20 Z3.4 F600"):
            s.sender.send(line, timeout=10.0)
        end = time.monotonic() + 15.0
        while s.sender.state() != "Alarm" and time.monotonic() < end:
            time.sleep(0.1)
        if "stream fault" not in s.watch.text():
            fail("[late-fault] the stall in the lit move faulted nothing: the case proves nothing")
        s.sender.realtime(b"\x18")
        time.sleep(1.0)
        for line in ("$X", "G90", "M5"):
            s.sender.send(line)
        s.sender.wait_state("Idle")
        lens, small, inside = after_drop(s)
        if (lens, small, inside) != ("error:15", "error:15", "ok"):
            fail("[late-fault] after the fault's drop: lens jogs -> %s, %s; an X jog inside the travel -> %s"
                 % (lens, small, inside))
    finally:
        s.close()


def test_late_steps():
    # X at 100 mm/s, 190 ms past the lead: about 19 mm, 127 full steps.
    expect_late(200, "$J=G90 G21 X300 F6000", "late-xy", (True, False))
    # The lens up at 200 mm/min, 490 ms past the lead: about five late
    # half-steps (its full step is two), the drop told with the jog running;
    # then a longer one, cut short by a reset at the drop.
    expect_lens_drop("run")
    expect_lens_drop("reset")
    # X at 1 mm/s, 290 ms past the lead: about 14 late steps at x8, over
    # its full step of eight; about 18 at x32, under its thirty-two.
    expect_late_pair()
    late_fault()
    print("PASS [late-steps]: late steps that owed X a full step dropped X and Y alone, ones that owed the "
          "lens two half-steps dropped the lens alone, the sender told which; the lens's planned motion ran "
          "to its end and an X-only line ran during it; one stall's late steps owed X a full step at x8 and "
          "kept the reference at x32; a dropped lens, by late steps or by a stream fault, refused Z and let "
          "X jog")


def homed_cases():
    """The cases that stand on a home, on one controller: a check that
    fails after a host starve dropped the reference runs them once more
    from a new one."""
    s = ct.Session(homed=True)
    try:
        test_measured(s)
        test_open_apply(s)
        test_sender_closes(s)
        test_reset_closes(s)
    finally:
        s.close()


def main():
    if not os.path.exists(ct.BIN):
        fail("no controller binary at %s" % ct.BIN)
    ct.run_case(homed_cases)
    test_not_homed()
    test_late_steps()
    ct.run_case(test_armed)     # armed: a host starve runs it once more
    print("ALL PASS")


if __name__ == "__main__":
    main()
