#!/usr/bin/env python3
# Copyright 2026 514 LLC d/b/a OpenGlow
# Written by Scott Wiederhold
# https://community.openglow.org
# SPDX-License-Identifier:    MIT
"""Host-side verification of the measured work envelope and the bed check's port op.

Runs the native grblHAL_glowforge binary in null-sink mode, with the
scripted sender and the port client of ctlport_test.py, homing manually
(nothing moves; X and Y become homed and the envelope is set):

  1. unset keys: X's and Y's far edges are the travel ($130, $131)
  2. envelope_x_mm and _y set the far edges at a home, and a jog past one
     is the core's error:15
  3. a key below GFHOME_ENVELOPE_MIN_MM is held to it, and one past the
     travel plus GFHOME_ENVELOPE_EXTRA_MM is held to that
  4. the port's envelope open: refused before a home (error:homed); after
     one, the port's jogs reach the travel plus 30 mm for X and Y, and the
     port's state says the envelope is open; envelope apply puts the keys'
     edges back without a home
  5. an open envelope is the port's jogs' alone: a status poll and an
     empty line leave it open, and the sender's first line closes it before
     the core reads the line (the line then answered ok, and the keys'
     edges in force), a port jog it meets canceled
  6. a soft reset and the port client going away each close it
  7. envelope open is refused under an open armed window (busy:state)

Usage: envelope_test.py [path/to/grblHAL_glowforge]
"""
import os
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
    end = time.time() + timeout
    while time.time() < end:
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
    end = time.time() + seconds
    while not s.sender.saw(needle) and time.time() < end:
        time.sleep(0.05)
    return s.sender.saw(needle)


def test_unset(s):
    conf(s)
    home(s)
    edge(s, "X", TRAVEL_X - 1, TRAVEL_X + 1, "unset")
    edge(s, "Y", TRAVEL_Y - 1, TRAVEL_Y + 1, "unset")
    print("PASS [unset]: with no key the far edges are the travel, %.0f and %.0f" % (TRAVEL_X, TRAVEL_Y))


def test_measured(s):
    conf(s, envelope_x_mm=480, envelope_y_mm=284.5)
    home(s)
    edge(s, "X", 479, 481, "measured")
    edge(s, "Y", 284, 285, "measured")
    print("PASS [measured]: envelope_x_mm 480 and envelope_y_mm 284.5 (past the travel) are the far edges at a home")


def test_bounds(s):
    conf(s, envelope_x_mm=10, envelope_y_mm=900)
    home(s)
    edge(s, "X", 49, 51, "bounds")
    edge(s, "Y", TRAVEL_Y + EXTRA - 1, TRAVEL_Y + EXTRA + 1, "bounds")
    print("PASS [bounds]: a key below 50 mm is held to 50, one past the travel plus 30 mm to that")


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
    s = ct.Session(laser=True)
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


def main():
    if not os.path.exists(ct.BIN):
        fail("no controller binary at %s" % ct.BIN)
    s = ct.Session()
    try:
        test_unset(s)
        test_measured(s)
        test_bounds(s)
        test_open_apply(s)
        test_sender_closes(s)
        test_reset_closes(s)
    finally:
        s.close()
    test_not_homed()
    test_armed()
    print("ALL PASS")


if __name__ == "__main__":
    main()
