#!/usr/bin/env python3
# Copyright 2026 514 LLC d/b/a OpenGlow
# Written by Scott Wiederhold
# https://community.openglow.org
# SPDX-License-Identifier:    MIT
"""Host-side verification of the M-codes an extension package answers.

Runs the native grblHAL_glowforge binary in null-sink mode, with the
scripted sender and the port client of ctlport_test.py standing in for a
sender and for the machine daemon:

  1. the table: the port's mcodes op takes "-" or numbers from M160 to
     M179, each once, the whole range included, and refuses every other
     form, leaving the table as it was
  2. a number nothing answers is an unsupported command where the line is
     parsed (error:20), and so is every number outside the table
  3. an answered M-code is a barrier: the sender's line waits for its ok,
     the port's state names the M-code with its P, Q and R words and its
     seq, the head does not move, a port jog is refused (busy:mcode), and
     the answer lets the line finish with a [MSG:] of its words; a second
     answer, or one under another seq, is stale, and one out of form is
     refused
  4. an answer that says the work was not done holds the job with a
     [MSG:] of its words, and a cycle start resumes it
  5. a soft reset ends the wait at once, and the controller answers the
     next line
  6. no answer in GFMCODE_WAIT_S holds the job
  7. the barrier is dark: under an open armed window with M3 S500, a job
     waiting at an M-code ships no FIRE tick, the window stays open across
     the wait, and the next laser move fires again

Usage: mcode_test.py [path/to/grblHAL_glowforge]
"""
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import ctlport_test as ct  # noqa: E402

fail = ct.fail
WAIT_S = 30.0


def send_nowait(s, line):
    """A sender line whose response is not waited for; the count before it."""
    n = s.sender.count()
    s.sender.sock.sendall((line + "\n").encode())
    return n


def wait_pending(s, timeout=5.0):
    end = time.time() + timeout
    while time.time() < end:
        st = s.port.state()
        if st.get("mcode"):
            return st
        time.sleep(0.05)
    fail("no M-code waits in the port's state: %r" % s.port.state())


def test_table(s):
    if s.port.state().get("mcode", "missing") is not None:
        fail("[table] the state carries no mcode null when nothing waits: %r" % s.port.state())
    for good in ("-", ",".join(str(n) for n in range(160, 180)), "160", "160,179,165"):
        r = s.port.request("mcodes " + good)
        if r != "ok":
            fail("[table] mcodes %s -> %r" % (good, r))
    for bad in ("159", "180", "160,160", "160,", ",160", "16a", "0160", "160 161", "1600", "160,,161", ""):
        r = s.port.request("mcodes " + bad)
        if r != "error:invalid":
            fail("[table] mcodes %r -> %r, not error:invalid" % (bad, r))
    # the refusals left the last good table: 160, 179 and 165
    n = send_nowait(s, "M165")
    m = wait_pending(s)["mcode"]
    if m.get("code") != 165:
        fail("[table] a refused list changed the table: %r" % m)
    s.port.request("mcode_result %d ok" % m["seq"])
    end = time.time() + 3
    while s.sender.count() == n and time.time() < end:
        time.sleep(0.02)
    if s.sender.responses[n:n + 1] != ["ok"]:
        fail("[table] M165 got %r" % s.sender.responses[n:])
    print("PASS [table]: the port takes '-' and numbers from M160 to M179, the whole range; 11 other forms "
          "refused, the table left as it was")


def test_unanswered(s):
    s.port.request("mcodes 160")
    # The core holds a parser error against every later block until an empty
    # line (the sync) or a reset: after an unsupported command a job halts.
    for line in ("M161", "M179", "M159", "M180 P1"):
        r = s.sender.send(line)
        if r != "error:20":
            fail("[unanswered] %s with only M160 answered got %r, not error:20" % (line, r))
        s.sender.send("")
    s.port.request("mcodes -")
    r = s.sender.send("M160")
    if r != "error:20":
        fail("[unanswered] M160 with nothing answered got %r" % r)
    s.sender.send("")
    print("PASS [unanswered]: a number nothing answers is error:20 where it is parsed")


def test_answered(s):
    s.port.request("mcodes 160,161")
    s.sender.send("G91")
    s.sender.send("G0 X3")
    s.sender.wait_state("Idle")
    n = send_nowait(s, "M160 P2 Q3.5 R-1")
    st = wait_pending(s)
    m = st["mcode"]
    if m.get("code") != 160 or m.get("words") != {"P": 2, "Q": 3.5, "R": -1} or not m.get("seq"):
        fail("[answered] the state names %r" % m)
    x0 = st["mpos"]
    time.sleep(1.0)
    st2 = s.port.state()
    if st2["mpos"] != x0 or st2["state"] != "Idle":
        fail("[answered] the head moved or the state is %s while the job waits: %r -> %r"
             % (st2["state"], x0, st2["mpos"]))
    if s.sender.count() != n:
        fail("[answered] the M-code line got its response before the answer: %r" % s.sender.responses[n:])
    r = s.port.request("jog G91 X1 F600")
    if r != "busy:mcode":
        fail("[answered] a port jog while the job waits -> %r, not busy:mcode" % r)
    for bad in ("mcode_result %d maybe" % m["seq"], "mcode_result x ok", "mcode_result 0 ok",
                "mcode_result %d ok [bracket]" % m["seq"], "mcode_result %d ok %s" % (m["seq"], "w" * 97)):
        r = s.port.request(bad)
        if r not in ("error:invalid", "error:stale"):
            fail("[answered] %r -> %r" % (bad, r))
    if s.port.request("mcode_result %d ok" % (m["seq"] + 7)) != "error:stale":
        fail("[answered] an answer under another seq was taken")
    if s.port.state().get("mcode") is None:
        fail("[answered] a refused answer ended the wait")
    r = s.port.request("mcode_result %d ok all set" % m["seq"])
    if r != "ok":
        fail("[answered] the answer -> %r" % r)
    end = time.time() + 3
    while s.sender.count() == n and time.time() < end:
        time.sleep(0.02)
    if s.sender.responses[n:n + 1] != ["ok"]:
        fail("[answered] the M-code line got %r after the answer" % s.sender.responses[n:])
    if not s.sender.saw("M160: done: all set"):
        fail("[answered] the sender got no [MSG:] of the answer's words")
    if s.port.request("mcode_result %d ok" % m["seq"]) != "error:stale":
        fail("[answered] a second answer was taken")
    if s.port.state().get("mcode") is not None:
        fail("[answered] the state still names an M-code")
    if s.sender.send("G0 X-3") != "ok":
        fail("[answered] the job did not go on")
    print("PASS [answered]: the line waited for the answer with the head still and a port jog "
          "refused; the state named M160 with P, Q and R; the answer let it go on with its words")


def test_fail_holds(s):
    s.sender.wait_state("Idle")
    n = send_nowait(s, "M161")
    m = wait_pending(s)["mcode"]
    s.port.request("mcode_result %d fail the exhaust did not start" % m["seq"])
    end = time.time() + 3
    while s.sender.count() == n and time.time() < end:
        time.sleep(0.02)
    s.sender.wait_state("Hold", timeout=5)
    if not s.sender.saw("M161: the exhaust did not start: the job is held"):
        fail("[fail] the sender got no [MSG:] of why the job is held")
    s.sender.realtime(b"~")
    s.sender.wait_state("Idle", timeout=5)
    if s.sender.send("G0 X1") != "ok":
        fail("[fail] the job did not go on after the resume")
    print("PASS [fail]: an answer that the work was not done held the job with its words; a cycle "
          "start resumed it")


def test_reset_ends_wait(s):
    s.sender.wait_state("Idle")
    send_nowait(s, "M160")
    wait_pending(s)
    t0 = time.time()
    s.sender.realtime(b"\x18")
    end = time.time() + 3
    while time.time() < end and s.port.state().get("mcode") is not None:
        time.sleep(0.02)
    dt = time.time() - t0
    if s.port.state().get("mcode") is not None:
        fail("[reset] the wait outlived a soft reset")
    time.sleep(0.5)
    s.sender.send("$X")
    if s.sender.send("G0 X1") != "ok":
        fail("[reset] the controller did not answer after the reset")
    print("PASS [reset]: a soft reset ended the wait in %.2f s" % dt)


def test_timeout_holds(s):
    s.port.request("mcodes 160")
    s.sender.wait_state("Idle")
    t0 = time.time()
    send_nowait(s, "M160")
    wait_pending(s)
    s.sender.wait_state("Hold", timeout=WAIT_S + 5)
    dt = time.time() - t0
    if dt < WAIT_S - 1:
        fail("[timeout] the job was held after %.1f s, not after the wait" % dt)
    if not s.sender.saw("M160 had no answer from its extension in 30 s: the job is held"):
        fail("[timeout] the sender got no [MSG:] of the timeout")
    s.sender.realtime(b"~")
    s.sender.wait_state("Idle", timeout=5)
    print("PASS [timeout]: with no answer the job was held after %.1f s, and resumed" % dt)


def test_dark():
    s = ct.Session(laser=True)
    try:
        s.port.request("mcodes 160")
        s.sender.send("G91")
        s.sender.send("M3 S500")
        s.sender.send("G1 X10 F1500")
        s.sender.wait_state("Idle")
        time.sleep(0.5)
        if not s.sender.saw("laser armed"):
            fail("[dark] the laser line did not arm: the case would prove nothing")
        steps0, fire0 = s.ticks()
        if fire0 == 0:
            fail("[dark] the armed G1 shipped no FIRE tick: the dump cannot see emission")
        send_nowait(s, "M160")
        m = wait_pending(s)["mcode"]
        time.sleep(3.0)
        steps1, fire1 = s.ticks()
        s.port.request("mcode_result %d ok" % m["seq"])
        time.sleep(0.3)
        s.sender.send("G1 X5 F1500")
        s.sender.wait_state("Idle")
        time.sleep(0.5)
        steps2, fire2 = s.ticks()
        if fire1 != fire0 or steps1 != steps0:
            fail("[dark] the job waiting at M160 under M3 S500 shipped %d FIRE ticks and %d steps"
                 % (fire1 - fire0, steps1 - steps0))
        if s.sender.saw("laser disarmed"):
            fail("[dark] the armed window closed across the wait")
        if fire2 <= fire1:
            fail("[dark] the laser move after the answer shipped no FIRE: the case would prove nothing")
        print("PASS [dark]: 3 s waiting at M160 under an open window with M3 S500 shipped no FIRE tick "
              "and no step; the window stayed open and the next move fired (%d ticks)" % (fire2 - fire1))
    finally:
        s.close()


def main():
    if not os.path.exists(ct.BIN):
        fail("no controller binary at %s" % ct.BIN)
    s = ct.Session()
    try:
        test_table(s)
        test_unanswered(s)
        test_answered(s)
        test_fail_holds(s)
        test_reset_ends_wait(s)
        test_timeout_holds(s)
    finally:
        s.close()
    test_dark()
    print("ALL PASS")


if __name__ == "__main__":
    main()
