#!/usr/bin/env python3
# Copyright 2026 514 LLC d/b/a OpenGlow
# Written by Scott Wiederhold
# https://community.openglow.org
# SPDX-License-Identifier:    MIT
"""Host-side verification of the crumb tray's mode (glowforge_tray.c).

With the tray out the work sits on the floor of the cut area, and Z is the
focal height above that floor: the Z position and the Z envelope move up by
tray_offset_mm on the lens step grid (1.35" is 100 half-steps). M103 P1 sets
the tray out, M103 P0 sets it in, and the controller port's "tray out|in"
runs the same line for the panel. Runs the native grblHAL_glowforge binary
in null-sink mode, with GFSINK_DUMP capturing the shipped stream, a scripted
sender on the Grbl socket, and the port client of ctlport_test.py:

  1. M103 P1 and P0 shift the reported Z by the offset's whole half-steps,
     tell the sender [MSG:Tray out] and [MSG:Tray in], keep Z referenced,
     write and remove the marker beside the shared config, and ship no
     step tick; asking for the mode already set changes nothing and says so
  2. the envelope moves with the position: with the tray out the ends of
     the lens's reach, shifted, run and two half-steps past them are
     refused, and a jog to a tray-in Z is refused; with the tray in again
     the same jog runs
  3. a missing P, a P other than 0 or 1, and an axis word on the line are
     errors (28, 39, 31) that change nothing
  4. the M-code is a barrier: behind a move it waits for the move to end,
     and the frame changes only once the machine is idle
  5. the port's tray op switches the mode with a sender connected, the
     sender's response count stays exact and it is told the mode; the op
     is refused out of form (error:invalid), during a program
     (busy:state), and while a job waits at a package's M-code
     (busy:mcode)
  6. the mode survives a soft reset, and a controller restart: a start with
     the marker present takes the lens reference in the tray-out frame,
     and one without it in the tray-in frame
  7. tray_offset_mm sets the shift, held to 13 to 60 mm
  8. an unreferenced lens shifts the same way, and stays pinned where it
     reads
  9. a camera home with the tray out declares Z the offset higher, and
     hands the runner the same park: the lens parks in the same place

The kernel-idle half of the barrier (the wait a package's M-code makes)
needs the pulse device: a null sink is always idle.

Usage: tray_test.py [path/to/grblHAL_glowforge]
"""
import os
import shutil
import signal
import subprocess
import sys
import tempfile
import threading
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import ctlport_test as ct  # noqa: E402

BIN = ct.BIN
fail = ct.fail
PORT = 2395
SPM = 2.922
EDGE = 3.35                 # the edge's grid step is 10
EDGE_K = 10
SHIFT_K = 100               # the default offset, 34.29 mm
DOWN, UP = 10, 12           # the fallback window


def zk(k):
    return k / SPM


class Session:
    """One null-sink controller over its own state directory. `conf` is the
    shared config; with `referenced` the lens marker forgectrl leaves is
    there, so the controller takes the lens reference at its start."""

    def __init__(self, conf=None, referenced=True):
        self.workdir = tempfile.mkdtemp(prefix="tray-")
        self.conf = os.path.join(self.workdir, "forgefirm.conf")
        self.dump = os.path.join(self.workdir, "stream.bin")
        self.marker = os.path.join(self.workdir, "tray.out")
        self.write_conf(conf or {})
        if referenced:
            with open(os.path.join(self.workdir, "lens.home"), "w") as f:
                f.write("edge 0 3 3\n")
        verdict = os.path.join(self.workdir, "cooling.state")
        self.stop = threading.Event()
        self.pub = threading.Thread(target=ct.publish_verdicts, args=(verdict, self.stop), daemon=True)
        self.pub.start()
        env = dict(os.environ, GFHOME_CONF=self.conf, GF_STATE_DIR=self.workdir,
                   GF_VERDICT_FILE=verdict, GFSINK_DUMP=self.dump, FFLOG_STDERR="1")
        env.pop("GFSINK", None)
        env.pop("GF_SWITCH_FILE", None)
        self.env = env
        self.start()

    def write_conf(self, conf):
        with open(self.conf, "w") as f:
            f.write("cool_fan_grace_s = 0\nlaser_disarm_s = 1\nlens_hall_edge_z_mm = %.2f\n" % EDGE)
            for k, v in conf.items():
                f.write("%s = %s\n" % (k, v))

    def start(self):
        self.proc = subprocess.Popen([BIN, "-p", str(PORT)], cwd=self.workdir, env=self.env,
                                     stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        self.sender = ct.Sender(PORT)
        path = os.path.join(self.workdir, "grbl.ctl")
        for _ in range(50):
            if os.path.exists(path):
                break
            time.sleep(0.1)
        else:
            fail("the controller did not create %s" % path)
        self.port = ct.PortClient(path)
        # The lens reference is taken on the first pass of the realtime hook.
        time.sleep(0.3)

    def restart(self, hard):
        """End the controller (SIGKILL when hard) and start another over the
        same state directory and shared config."""
        self.port.close()
        self.sender.close()
        if hard:
            self.proc.kill()
        else:
            self.proc.send_signal(signal.SIGINT)
        self.proc.wait(10)
        self.start()

    def state(self):
        return self.port.state()

    def z(self):
        return self.state()["mpos"][2]

    def tray(self):
        return self.state()["tray"]

    def ticks(self):
        """Step ticks shipped so far (a byte with bit 7 set is a power byte)."""
        try:
            with open(self.dump, "rb") as f:
                data = f.read()
        except OSError:
            return 0
        return sum(1 for b in data if not b & 0x80 and b & 0x25)

    def messages(self, text, want=None):
        """How many [MSG:<text>] lines the sender has had. With `want`, wait
        up to a second for that many: the port answers at once, and the
        sender's lines go out on the controller's next pass."""
        end = time.time() + (1.0 if want is not None else 0)
        while True:
            n = self.sender.saw("[MSG:%s]" % text)
            if want is None or n >= want or time.time() > end:
                return n
            time.sleep(0.02)

    def quiet(self):
        time.sleep(0.45)

    def close(self):
        self.port.close()
        self.sender.close()
        self.proc.send_signal(signal.SIGINT)
        try:
            self.proc.wait(5)
        except subprocess.TimeoutExpired:
            self.proc.kill()
        self.stop.set()
        self.pub.join(2)
        shutil.rmtree(self.workdir, ignore_errors=True)


def expect_z(s, k, what):
    z = s.z()
    if abs(z - zk(k)) > 0.002:
        fail("[%s] Z reads %.3f, not %.3f (%d half-steps)" % (what, z, zk(k), k))


def refused(s, line):
    """Send a line that must be refused, and return its error, acknowledged
    with an empty line (the core holds a parser error against the next
    g-code line until then)."""
    r = s.sender.send(line)
    if r.startswith("error"):
        if s.sender.send("") != "ok":
            fail("the empty line that acknowledges %s did not get ok" % r)
        return r
    return None


def jog_to(s, k):
    """An absolute jog to half-step k: its response."""
    r = s.sender.send("$J=G90 G21 Z%.4f F200" % zk(k))
    if r == "ok":
        s.sender.wait_state("Idle")
    elif s.sender.send("") != "ok":
        fail("the empty line after a refused jog did not get ok")
    return r


def test_switch(s):
    expect_z(s, EDGE_K, "switch")
    st = s.state()
    if st["tray"] != "in" or not st["homed"] & 4:
        fail("[switch] not tray in with Z referenced at the start: %r" % st)
    ticks = s.ticks()
    if s.sender.send("M103 P1") != "ok":
        fail("[switch] M103 P1 was refused")
    st = s.state()
    if st["tray"] != "out" or not st["homed"] & 4:
        fail("[switch] not tray out with Z referenced after M103 P1: %r" % st)
    expect_z(s, EDGE_K + SHIFT_K, "switch")
    if not s.messages("Tray out", 1):
        fail("[switch] the sender was not told [MSG:Tray out]")
    if not os.path.exists(s.marker):
        fail("[switch] M103 P1 left no marker beside the shared config")
    if s.sender.send("M103 P1") != "ok" or s.tray() != "out":
        fail("[switch] a second M103 P1 was refused or changed the mode")
    expect_z(s, EDGE_K + SHIFT_K, "switch: the mode already set")
    if s.messages("Tray out", 2) != 2:
        fail("[switch] the mode already set was not told again")
    if s.sender.send("M103 P0") != "ok" or s.tray() != "in":
        fail("[switch] M103 P0 did not set the tray in")
    expect_z(s, EDGE_K, "switch: back in")
    if os.path.exists(s.marker) or not s.messages("Tray in", 1):
        fail("[switch] M103 P0 left the marker or did not tell the sender")
    time.sleep(0.3)
    if s.ticks() != ticks:
        fail("[switch] the switches shipped %d step ticks" % (s.ticks() - ticks))
    print("PASS [switch]: M103 P1 moved Z up %d half-steps and P0 back, the sender was told, the "
          "marker followed, and no step shipped" % SHIFT_K)


def test_envelope(s):
    s.sender.send("M103 P1")
    base = EDGE_K + SHIFT_K
    for k, want, what in ((base + UP, "ok", "the top of the reach"),
                          (base + UP + 2, "error:15", "two half-steps over the top"),
                          (base - DOWN, "ok", "the bottom of the reach"),
                          (base - DOWN - 2, "error:15", "two half-steps under the bottom"),
                          (EDGE_K + UP, "error:15", "a tray-in Z")):
        r = jog_to(s, k)
        if r != want:
            fail("[envelope] tray out: a jog to %s (Z %.3f) got %r, not %s" % (what, zk(k), r, want))
    if jog_to(s, base) != "ok":
        fail("[envelope] the jog back to the edge was refused")
    s.sender.send("M103 P0")
    expect_z(s, EDGE_K, "envelope")
    if jog_to(s, EDGE_K + UP) != "ok":
        fail("[envelope] tray in: the jog to the same tray-in Z was refused")
    if jog_to(s, EDGE_K) != "ok":
        fail("[envelope] the jog back to the edge was refused")
    print("PASS [envelope]: with the tray out the shifted reach ran to its ends, past them and a "
          "tray-in Z were refused; with the tray in the tray-in Z ran")


def test_errors(s):
    ticks = s.ticks()
    for line, want in (("M103", "error:28"), ("M103 P2", "error:39"), ("M103 P0.5", "error:39"),
                       ("M103 P-1", "error:39"), ("M103 P1 Z5", "error:31"), ("M103 P1 X5", "error:31")):
        r = refused(s, line)
        if r != want:
            fail("[errors] %r got %r, not %s" % (line, r, want))
        if s.tray() != "in" or os.path.exists(s.marker):
            fail("[errors] %r changed the mode" % line)
    expect_z(s, EDGE_K, "errors")
    time.sleep(0.3)
    if s.ticks() != ticks:
        fail("[errors] the refused lines shipped %d step ticks" % (s.ticks() - ticks))
    print("PASS [errors]: a missing P, P2, P0.5, P-1 and an axis word were refused and changed nothing")


def test_barrier(s):
    """Behind a move the switch waits for it to end."""
    x0 = s.state()["mpos"][0]
    s.sender.send("G91 G1 X10 F300")                # about two seconds
    n = s.sender.count()
    s.sender.sock.sendall(b"M103 P1\n")
    seen_run = False
    end = time.time() + 10
    while time.time() < end:
        st = s.port.state()
        if st["state"] == "Run":
            seen_run = True
            if st["tray"] != "in" or abs(st["mpos"][2] - zk(EDGE_K)) > 0.002:
                fail("[barrier] the frame changed while the move before M103 ran: %r" % st)
        if st["tray"] == "out":
            break
        time.sleep(0.05)
    if not seen_run:
        fail("[barrier] the move was never seen running, so the case proves nothing")
    st = s.port.state()
    if st["tray"] != "out" or st["state"] != "Idle" or abs(st["mpos"][0] - x0 - 10) > 0.01:
        fail("[barrier] the switch did not land after the move ended: %r" % st)
    for _ in range(100):
        if s.sender.count() > n:
            break
        time.sleep(0.05)
    s.sender.send("G90")
    s.sender.send("M103 P0")
    print("PASS [barrier]: M103 behind a move changed the frame only once the move had ended")


def test_port(s):
    s.sender.wait_state("Idle")
    s.quiet()
    before = s.sender.count()
    told = s.messages("Tray out")
    r = s.port.request("tray out")
    if r != "ok" or s.tray() != "out":
        fail("[port] tray out got %r" % r)
    expect_z(s, EDGE_K + SHIFT_K, "port")
    if s.port.request("tray out") != "ok":
        fail("[port] a second tray out was refused")
    if s.messages("Tray out", told + 2) != told + 2:
        fail("[port] the sender was not told the mode each time")
    if s.port.request("tray sideways") != "error:invalid":
        fail("[port] a tray op out of form was not refused")
    if s.sender.count() != before:
        fail("[port] the sender got %d responses for the port's tray ops" % (s.sender.count() - before))

    s.sender.send("G91 G1 X-10 F300")
    time.sleep(0.4)
    r = s.port.request("tray in")
    if r != "busy:state" or s.tray() != "out":
        fail("[port] tray in during a program got %r" % r)
    s.sender.wait_state("Idle")
    s.sender.send("G90")

    if s.port.request("mcodes 160") != "ok":
        fail("[port] the M-code table was refused")
    n = s.sender.count()
    s.sender.sock.sendall(b"M160 P1\n")
    for _ in range(100):
        st = s.state()
        if st.get("mcode"):
            break
        time.sleep(0.05)
    else:
        fail("[port] no M-code waits: %r" % s.state())
    r = s.port.request("tray in")
    if r != "busy:mcode" or s.tray() != "out":
        fail("[port] tray in while a job waits at an M-code got %r" % r)
    if s.port.request("mcode_result %d ok" % st["mcode"]["seq"]) != "ok":
        fail("[port] the M-code's answer was refused")
    for _ in range(100):
        if s.sender.count() > n:
            break
        time.sleep(0.05)
    s.port.request("mcodes -")
    s.quiet()
    before = s.sender.count()
    if s.port.request("tray in") != "ok" or s.tray() != "in":
        fail("[port] tray in was refused")
    expect_z(s, EDGE_K, "port")
    if s.sender.count() != before or s.sender.send("G0 X0") != "ok":
        fail("[port] the sender's count was disturbed by the tray op")
    s.sender.wait_state("Idle")
    print("PASS [port]: tray out and in through the port with a sender connected, its count exact "
          "and the mode told; refused out of form, during a program, and at a package's M-code")


def test_persistence(s):
    s.sender.send("M103 P1")
    s.sender.realtime(b"\x18")
    time.sleep(1.0)
    s.sender.send("$X")
    if s.tray() != "out":
        fail("[persistence] a soft reset set the tray in")
    expect_z(s, EDGE_K + SHIFT_K, "persistence: soft reset")
    s.restart(hard=True)
    st = s.state()
    if st["tray"] != "out" or not st["homed"] & 4:
        fail("[persistence] a restart over the marker came up %r" % st)
    expect_z(s, EDGE_K + SHIFT_K, "persistence: restart with the tray out")
    if jog_to(s, EDGE_K + SHIFT_K + UP) != "ok" or jog_to(s, EDGE_K + UP) != "error:15":
        fail("[persistence] the restarted controller's envelope is not the tray-out one")
    jog_to(s, EDGE_K + SHIFT_K)
    if s.port.request("tray in") != "ok":
        fail("[persistence] tray in was refused after the restart")
    expect_z(s, EDGE_K, "persistence: in after the restart")
    s.restart(hard=False)
    if s.tray() != "in":
        fail("[persistence] a restart with no marker came up tray out")
    expect_z(s, EDGE_K, "persistence: restart with the tray in")
    print("PASS [persistence]: the tray out survived a soft reset and a controller kill, the new "
          "controller referenced Z in the tray-out frame, and a start with no marker in the tray-in frame")


def test_offset():
    for key, k in (("20", 58), ("5", 38), ("99", 175)):
        s = Session(conf={"tray_offset_mm": key})
        try:
            s.sender.send("M103 P1")
            expect_z(s, EDGE_K + k, "offset %s" % key)
            s.sender.send("M103 P0")
            expect_z(s, EDGE_K, "offset %s: back in" % key)
        finally:
            s.close()
    print("PASS [offset]: tray_offset_mm 20 moved Z 58 half-steps, 5 was held to 13 mm (38), "
          "99 to 60 mm (175)")


def test_unreferenced():
    s = Session(referenced=False)
    try:
        z0 = s.z()
        k0 = round(z0 * SPM)
        s.sender.send("M103 P1")
        expect_z(s, k0 + SHIFT_K, "unreferenced")
        for step in ("1", "-1"):
            r = s.sender.send("$J=G91 G21 Z%s F200" % step)
            if r != "error:15":
                fail("[unreferenced] a Z jog of %s got %r: the pinned envelope did not follow" % (step, r))
            s.sender.send("")
        s.sender.send("M103 P0")
        expect_z(s, k0, "unreferenced: back in")
    finally:
        s.close()
    print("PASS [unreferenced]: an unreferenced lens shifted the same way and stayed pinned where it read")


def test_camera_home():
    s = Session(conf={"homing_mode": "gfcloud"})
    park = os.path.join(s.workdir, "park.log")
    s.write_conf({"homing_mode": "gfcloud", "gfcloud_home_cmd": "echo $GFHOME_PARK_HALF_STEPS >> %s" % park})
    try:
        z = {}
        for mode in ("P0", "P1"):
            s.sender.send("M103 " + mode)
            if s.sender.send("$H") != "ok":
                fail("[camera-home] $H with M103 %s was refused" % mode)
            s.sender.wait_state("Idle")
            z[mode] = s.z()
        with open(park) as f:
            parks = f.read().split()
        if len(parks) != 2 or parks[0] != parks[1]:
            fail("[camera-home] the runner was handed parks %r, not the same one twice" % parks)
        k = EDGE_K + int(parks[0])
        if abs(z["P0"] - zk(k)) > 0.002 or abs(z["P1"] - zk(k + SHIFT_K)) > 0.002:
            fail("[camera-home] Z after the homes read %r, not %.3f and %.3f" % (z, zk(k), zk(k + SHIFT_K)))
        if jog_to(s, EDGE_K + SHIFT_K + UP) != "ok" or jog_to(s, EDGE_K + UP) != "error:15":
            fail("[camera-home] the envelope after a tray-out home is not the tray-out one")
        s.sender.send("M103 P0")
    finally:
        s.close()
    print("PASS [camera-home]: the park handed to the runner was %s half-steps in both modes, and Z "
          "after the home read %.3f tray in, %.3f tray out" % (parks[0], z["P0"], z["P1"]))


def main():
    if not os.path.exists(BIN):
        fail("no controller binary at %s" % BIN)
    s = Session()
    try:
        if s.sender.send("$102") != "ok" or not s.sender.saw("$102=2.922"):
            fail("$102 is not 2.922: this harness needs the default")
        test_switch(s)
        test_envelope(s)
        test_errors(s)
        test_barrier(s)
        test_port(s)
        test_persistence(s)
    finally:
        s.close()
    test_offset()
    test_unreferenced()
    test_camera_home()
    print("ALL PASS")


if __name__ == "__main__":
    main()
