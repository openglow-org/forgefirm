#!/usr/bin/env python3
# Copyright 2026 514 LLC d/b/a OpenGlow
# Written by Scott Wiederhold
# https://community.openglow.org
# SPDX-License-Identifier:    MIT
"""Host-side verification of the manual homing provider and the motor release.

Runs the native grblHAL_glowforge binary in null-sink mode with GFSINK_DUMP
capturing the shipped byte stream and GFSINK_ATTR_LOG capturing the sysfs
writes the hardware would get (the release is the X and Y step currents at 0). An operator's hands are on a released gantry,
so the rule these cases pin is that nothing moves it and nothing but the
operator's own $ME or a manual $H energizes it:

  1. $H with homing_mode = manual ships no step, declares X0 Y0 where the
     head stands, marks X and Y homed and turns their soft limits on, leaves
     Z where it was, and tells the sender the home was set by hand
  2. $H is refused while a program runs
  3. $MD writes the release, drops the X and Y reference at once, and locks
     the machine in the alarm state; it is refused under an open armed window
  4. while released, every motion source is refused and ships nothing: a
     sender jog, a sender G0, a program line, a port jog, and a homing runner;
     $X cannot unlock it and a soft reset does not either
  5. only $ME and a manual $H write the energize, each exactly once; after
     $ME the position is still unknown, and after the $H it is homed
  6. the port's panel operations (release, energize, home) do the same with
     a sender connected, and the sender's response count stays exact; home
     is refused unless homing_mode = manual
  7. manual_home_x and manual_home_y: the coordinate the stop blocks stand
     for, the origin by default and never negative; the envelope starts at
     the blocks, and an out-of-range offset is clamped
  9. a controller that starts over a released gantry (the marker in the
     state directory) comes up locked and writes no current until $ME
  8. each provider reads its own offsets and no other's: with both pairs set,
     a manual home declares manual_home_* and a camera home declares
     gfcloud_home_*, which may be negative, with the envelope reaching back
     to it

Usage: manual_home_test.py [path/to/grblHAL_glowforge]
"""
import os
import shutil
import signal
import subprocess
import tempfile
import threading
import time

from ctlport_test import BIN, PortClient, Sender, fail, publish_verdicts

PORT = 2396
NOTICE = "Manual home: position set where the head was placed"
RELEASED = "Motors released"
RUNNER_MARK = "runner-ran"


class Session:
    def __init__(self):
        self.workdir = tempfile.mkdtemp(prefix="manual-home-")
        self.conf = os.path.join(self.workdir, "forgefirm.conf")
        self.dump = os.path.join(self.workdir, "stream.bin")
        self.attr_log = os.path.join(self.workdir, "attr.log")
        self.set_mode("manual")
        verdict = os.path.join(self.workdir, "cooling.state")
        self.stop = threading.Event()
        self.pub = threading.Thread(target=publish_verdicts, args=(verdict, self.stop), daemon=True)
        self.pub.start()
        env = dict(os.environ, GFHOME_CONF=self.conf, GF_STATE_DIR=self.workdir,
                   GF_VERDICT_FILE=verdict, GFSINK_DUMP=self.dump,
                   GFSINK_ATTR_LOG=self.attr_log, FFLOG_STDERR="1")
        env.pop("GFSINK", None)
        env.pop("GF_SWITCH_FILE", None)
        self.env = env
        self.proc = subprocess.Popen([BIN, "-p", str(PORT)], cwd=self.workdir, env=env,
                                     stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        self.sender = Sender(PORT)
        path = os.path.join(self.workdir, "grbl.ctl")
        for _ in range(50):
            if os.path.exists(path):
                break
            time.sleep(0.1)
        self.port = PortClient(path)
        # A controller writes the hold currents at its start; what the cases
        # count comes after that.
        self.base = 0
        start = self.attr_writes()
        if start != [("33", "5")]:
            fail("the controller's start wrote %r to the step currents, not the hold currents" % start)
        self.base = len(start)

    def set_mode(self, mode, extra=""):
        """The driver re-reads the file at every $H."""
        with open(self.conf, "w") as f:
            f.write("cool_fan_grace_s = 0\nlaser_disarm_s = 1\nhoming_mode = %s\n"
                    "gfcloud_home_cmd = touch %s\n%s"
                    % (mode, os.path.join(self.workdir, RUNNER_MARK), extra))

    def motion_ticks(self):
        """Step and FIRE ticks shipped so far (a byte with bit 7 set is a power byte)."""
        try:
            with open(self.dump, "rb") as f:
                data = f.read()
        except OSError:
            return 0
        return sum(1 for b in data if not b & 0x80 and b & 0x35)

    def attr_writes(self):
        """The X and Y step-current writes the hardware would have got, in
        order, as (x, y) pairs: ("0", "0") is a release, ("33", "5") the hold
        currents, which is what an energize writes."""
        vals = {"pic/x_step_current": [], "pic/y_step_current": []}
        try:
            with open(self.attr_log) as f:
                for line in f:
                    parts = line.split()
                    if len(parts) == 2 and parts[0] in vals:
                        vals[parts[0]].append(parts[1])
        except OSError:
            pass
        return list(zip(vals["pic/x_step_current"], vals["pic/y_step_current"]))[self.base:]

    def respawn(self, hard=True):
        """End the controller (SIGKILL when hard: it gets no chance to tidy
        up) and start another over the same state directory."""
        self.port.close()
        self.sender.close()
        if hard:
            self.proc.kill()
        else:
            self.proc.send_signal(signal.SIGINT)
        self.proc.wait(10)
        self.proc = subprocess.Popen([BIN, "-p", str(PORT)], cwd=self.workdir, env=self.env,
                                     stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        self.sender = Sender(PORT)
        time.sleep(0.5)
        self.port = PortClient(os.path.join(self.workdir, "grbl.ctl"))

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


def refused(s, line):
    """Send a line that must be refused, and return its error. The core holds
    a parser error against every g-code line that follows until an empty line
    acknowledges it (the streaming contract), so the acknowledgment is sent
    here: the next line is then judged on its own merits."""
    r = s.sender.send(line)
    if r.startswith("error"):
        if s.sender.send("") != "ok":
            fail("the empty line that acknowledges %s did not get ok" % r)
        return r
    return None


def test_manual_home(s):
    s.sender.send("G91")
    s.sender.send("G0 X20 Y10")                 # away from wherever X0 Y0 was
    s.sender.wait_state("Idle")
    before = s.port.state()
    if abs(before["mpos"][0]) < 1:
        fail("[manual-home] the head did not move before the home: %r" % before)
    if before["homed"] & 3:
        fail("[manual-home] X or Y homed before any home: %r" % before)
    if s.sender.send("$J=G91 X-100 F3000") != "ok":     # unhomed: no X soft limit yet
        fail("[manual-home] an unhomed jog past X0 was refused")
    s.sender.wait_state("Idle")
    time.sleep(0.5)
    ticks = s.motion_ticks()
    z_before = s.port.state()["mpos"][2]

    if s.sender.send("$H") != "ok":
        fail("[manual-home] $H under manual did not get ok")
    time.sleep(0.5)
    st = s.port.state()
    if s.motion_ticks() != ticks:
        fail("[manual-home] $H shipped %d motion ticks" % (s.motion_ticks() - ticks))
    if st["mpos"][0] != 0 or st["mpos"][1] != 0:
        fail("[manual-home] X and Y are not 0 after the home: %r" % st)
    if st["homed"] & 3 != 3:
        fail("[manual-home] X and Y are not marked homed: %r" % st)
    if st["mpos"][2] != z_before:
        fail("[manual-home] Z changed across the home: %r -> %r" % (z_before, st["mpos"][2]))
    if st["state"] != "Idle":
        fail("[manual-home] not idle after the home: %r" % st)
    if not s.sender.saw(NOTICE):
        fail("[manual-home] the sender was not told the home was set by hand")
    r = refused(s, "$J=G91 X-5 F600")           # off the bed: the soft limit is on now
    if r != "error:15":
        fail("[manual-home] a jog past X0 after the home got %r, not error:15" % r)
    print("PASS [manual-home]: $H shipped nothing, declared X0 Y0, homed X and Y with soft "
          "limits on, kept Z, and told the sender")


def test_home_refused_in_cycle(s):
    s.sender.send("G90")
    s.sender.send("G1 X40 F300")
    time.sleep(0.5)
    if s.port.state()["state"] != "Run":
        fail("[home-in-cycle] the program is not running: %r" % s.port.state())
    r = refused(s, "$H")
    if r is None:
        fail("[home-in-cycle] $H during a program was accepted")
    if s.port.state()["homed"] & 3 != 3:
        fail("[home-in-cycle] the refused $H dropped the reference")
    s.sender.wait_state("Idle", timeout=15.0)
    print("PASS [home-in-cycle]: $H during a program got %s and changed nothing" % r)


def test_release_refused_armed(s):
    s.sender.send("M3 S100")
    s.sender.send("G1 X45 F1500")
    s.sender.wait_state("Idle")                 # motion done, the armed window still open
    if not s.sender.saw("laser armed"):
        fail("[release-armed] the laser line did not arm, so the case proves nothing")
    r = refused(s, "$MD")
    if r is None:
        fail("[release-armed] $MD under an open armed window was accepted")
    if s.attr_writes():
        fail("[release-armed] the refused $MD wrote the release: %r" % s.attr_writes())
    s.sender.send("M5")
    time.sleep(2.5)                             # past laser_disarm_s: the window closes
    print("PASS [release-armed]: $MD under an open armed window got %s and wrote nothing" % r)


def test_release_locks(s):
    if s.sender.send("$MD") != "ok":
        fail("[release] $MD at idle was refused")
    st = s.port.state()
    if s.attr_writes() != [("0", "0")]:
        fail("[release] the release was not written once: %r" % s.attr_writes())
    if not st["released"] or st["state"] != "Alarm" or st["homed"] & 3:
        fail("[release] not released, locked and unreferenced: %r" % st)
    if not st["homed"] & 4:
        fail("[release] Z lost its reference: %r" % st)
    if not s.sender.saw(RELEASED):
        fail("[release] the sender was not told")

    ticks = s.motion_ticks()
    x0 = st["mpos"][0]
    for line in ("$J=G91 X5 F600", "G0 X5", "G1 X5 F500", "G91 G1 Y3 F500"):
        if refused(s, line) is None:
            fail("[release] the sender's %r was accepted while released" % line)
    s.quiet()
    r = s.port.request("jog G91 X5 F600")
    if r != "busy:released":
        fail("[release] a port jog got %r while released" % r)
    if refused(s, "$X") is None:
        fail("[release] $X was accepted while released")
    s.set_mode("gfcloud")
    r = refused(s, "$H")
    s.set_mode("manual")
    if r is None or os.path.exists(os.path.join(s.workdir, RUNNER_MARK)):
        fail("[release] a homing runner was not refused while released (%r)" % r)
    s.sender.realtime(b"\x18")
    time.sleep(1.0)
    r = refused(s, "$X")
    st = s.port.state()
    if r is None or st["state"] != "Alarm" or not st["released"]:
        fail("[release] a soft reset and $X unlocked a released machine: %r %r" % (r, st))
    if refused(s, "G0 X5") is None:
        fail("[release] G0 after a soft reset was accepted while released")
    time.sleep(0.5)
    if s.motion_ticks() != ticks or s.port.state()["mpos"][0] != x0:
        fail("[release] something moved while released")
    if s.attr_writes() != [("0", "0")]:
        fail("[release] something other than $ME or $H wrote the step currents: %r" % s.attr_writes())
    print("PASS [release]: released, locked in alarm, X and Y unreferenced, Z kept; a sender "
          "jog, G0, program lines, a port jog, a homing runner, $X and a soft reset all "
          "refused; nothing shipped, nothing energized")


def test_energize(s):
    if s.sender.send("$ME") != "ok":
        fail("[energize] $ME was refused")
    st = s.port.state()
    if s.attr_writes() != [("0", "0"), ("33", "5")]:
        fail("[energize] the energize was not written once: %r" % s.attr_writes())
    if st["released"] or st["state"] != "Idle" or st["homed"] & 3:
        fail("[energize] not energized, idle and still unreferenced: %r" % st)
    if s.sender.send("$ME") != "ok" or s.attr_writes() != [("0", "0"), ("33", "5")]:
        fail("[energize] a second $ME wrote again: %r" % s.attr_writes())
    if s.sender.send("$J=G91 X1 F600") != "ok":
        fail("[energize] a jog after $ME was refused")
    s.sender.wait_state("Idle")

    s.sender.send("$MD")
    if s.sender.send("$H") != "ok":
        fail("[energize] a manual $H while released was refused")
    st = s.port.state()
    if s.attr_writes() != [("0", "0"), ("33", "5"), ("0", "0"), ("33", "5")]:
        fail("[energize] the manual $H did not energize exactly once: %r" % s.attr_writes())
    if st["released"] or st["state"] != "Idle" or st["homed"] & 3 != 3:
        fail("[energize] not energized, idle and homed after the manual $H: %r" % st)
    print("PASS [energize]: $ME energized once and left X and Y unreferenced; a manual $H "
          "energized once and homed")


def test_port_panel_ops(s):
    s.sender.wait_state("Idle")
    s.quiet()
    before = s.sender.count()
    for op, want in (("release", True), ("energize", False), ("release", True)):
        if s.port.request(op) != "ok":
            fail("[port] %s was refused" % op)
        if s.port.state()["released"] != want:
            fail("[port] %s left released=%r" % (op, not want))
    s.set_mode("gfcloud")
    r = s.port.request("home")
    s.set_mode("manual")
    if r != "error:mode" or os.path.exists(os.path.join(s.workdir, RUNNER_MARK)):
        fail("[port] home under gfcloud got %r" % r)
    if s.port.request("home") != "ok":
        fail("[port] home under manual was refused")
    st = s.port.state()
    if st["released"] or st["homed"] & 3 != 3 or st["state"] != "Idle":
        fail("[port] not energized, homed and idle after the port's home: %r" % st)
    if s.sender.count() != before:
        fail("[port] the sender got %d responses for the port's commands"
             % (s.sender.count() - before))
    if s.sender.send("G0 X1") != "ok":
        fail("[port] the sender's next line did not get ok")
    print("PASS [port]: release, energize and home work through the port with a sender "
          "connected, home is refused outside manual mode, and the sender's count is exact")


def test_restart_while_released(s):
    """A controller that dies under a released gantry is replaced by one that
    takes the release over before it writes its first current: it comes up
    locked, the hold currents are never written, and $ME still ends it."""
    s.sender.wait_state("Idle")
    if s.sender.send("$MD") != "ok":
        fail("[restart] $MD was refused")
    before = s.attr_writes()
    marker = os.path.join(s.workdir, "motors.released")
    if not os.path.exists(marker):
        fail("[restart] the release left no marker in the state directory")
    s.respawn(hard=True)
    st = s.port.state()
    if not st["released"] or st["state"] != "Alarm":
        fail("[restart] the new controller did not take the release over: %r" % st)
    started = s.attr_writes()[len(before):]
    if started != [("0", "0")]:
        fail("[restart] the new controller's start wrote %r to a released gantry, not 0 and 0" % started)
    if refused(s, "$X") is None or refused(s, "$J=G91 X5 F600") is None:
        fail("[restart] the new controller let a released machine be unlocked or jogged")
    if s.sender.send("$ME") != "ok":
        fail("[restart] $ME was refused after the restart")
    st = s.port.state()
    if st["released"] or st["state"] != "Idle" or s.attr_writes()[-1] != ("33", "5") or os.path.exists(marker):
        fail("[restart] $ME did not energize and clear the marker: %r %r" % (st, s.attr_writes()[-1:]))
    s.respawn(hard=False)
    st = s.port.state()
    if st["released"] or st["state"] != "Idle":
        fail("[restart] a controller started with no marker came up released: %r" % st)
    print("PASS [restart]: a controller killed under a released gantry was replaced by one that came "
          "up locked and wrote 0 to X and Y at its start; $ME energized and cleared the marker; the next start was normal")


def test_stop_block_offsets(s):
    """manual_home_x and _y are the coordinate the stop blocks stand for: the
    origin by default, never negative, and the wall the envelope starts at."""
    s.sender.wait_state("Idle")
    s.set_mode("manual", "manual_home_x = 12.5\nmanual_home_y = 8\n")
    if s.sender.send("$H") != "ok":
        fail("[offsets] $H under manual with offsets was refused")
    st = s.port.state()
    if abs(st["mpos"][0] - 12.5) > 0.01 or abs(st["mpos"][1] - 8.0) > 0.01:
        fail("[offsets] the home declared %r, not (12.5, 8)" % st["mpos"][:2])
    if s.sender.send("$J=G91 X5 Y5 F3000") != "ok":
        fail("[offsets] a jog from the blocks onto the bed was refused")
    s.sender.wait_state("Idle")
    if s.sender.send("$J=G91 X-5 Y-5 F3000") != "ok":
        fail("[offsets] the jog back to the blocks was refused")
    s.sender.wait_state("Idle")
    for line in ("$J=G91 X-1 F600", "$J=G91 Y-1 F600"):
        r = refused(s, line)
        if r != "error:15":
            fail("[offsets] %s, behind the blocks, got %r, not error:15" % (line, r))
    s.set_mode("manual", "manual_home_x = -10.3\nmanual_home_y = 1e9\n")
    if s.sender.send("$H") != "ok":
        fail("[offsets] $H with out-of-range offsets was refused")
    st = s.port.state()
    if st["mpos"][0] != 0 or st["mpos"][1] > 1000:
        fail("[offsets] a negative or oversized offset was not clamped: %r" % st["mpos"][:2])
    s.set_mode("manual")
    print("PASS [offsets]: a manual home declared (12.5, 8), the envelope started at the blocks, "
          "a negative offset became the origin and an oversized one the axis travel")


def test_offsets_belong_to_their_provider(s):
    """Both pairs set at once: a manual home declares manual_home_* and never
    looks at gfcloud_home_*, and a camera home (a stand-in runner that exits 0)
    declares gfcloud_home_* and never looks at manual_home_*. A camera home may
    lie behind the origin, and the envelope reaches back to it."""
    both = "manual_home_x = 7\nmanual_home_y = 9\ngfcloud_home_x = -4.5\ngfcloud_home_y = 3.25\n"
    s.sender.wait_state("Idle")
    s.set_mode("manual", both)
    if s.sender.send("$H") != "ok":
        fail("[own-offsets] the manual home was refused")
    st = s.port.state()
    if abs(st["mpos"][0] - 7.0) > 0.01 or abs(st["mpos"][1] - 9.0) > 0.01:
        fail("[own-offsets] a manual home declared %r, not its own (7, 9)" % st["mpos"][:2])
    s.set_mode("gfcloud", both)
    if s.sender.send("$H", timeout=30.0) != "ok":
        fail("[own-offsets] the camera home (stand-in runner) was refused")
    s.sender.wait_state("Idle")
    st = s.port.state()
    if abs(st["mpos"][0] + 4.5) > 0.01 or abs(st["mpos"][1] - 3.25) > 0.01:
        fail("[own-offsets] a camera home declared %r, not its own (-4.5, 3.25)" % st["mpos"][:2])
    if s.sender.send("$J=G91 X2 F3000") != "ok":
        fail("[own-offsets] a jog from a camera home behind the origin was refused")
    s.sender.wait_state("Idle")
    if s.sender.send("$J=G91 X-2 F3000") != "ok":
        fail("[own-offsets] the jog back to the camera home was refused")
    s.sender.wait_state("Idle")
    r = refused(s, "$J=G91 X-1 F600")
    if r != "error:15":
        fail("[own-offsets] a jog behind the camera home got %r, not error:15" % r)
    r = refused(s, "$J=G91 Y-4 F600")
    if r != "error:15":
        fail("[own-offsets] a jog to Y-0.75, off the bed, got %r, not error:15" % r)
    s.set_mode("manual")
    print("PASS [own-offsets]: with both pairs set, a manual home declared (7, 9) and a camera home "
          "(-4.5, 3.25); the envelope reached back to the camera home on X and stopped at the origin on Y")


def main():
    if not os.path.exists(BIN):
        fail("no controller binary at %s" % BIN)
    s = Session()
    try:
        test_manual_home(s)
        test_home_refused_in_cycle(s)
        test_release_refused_armed(s)
        test_release_locks(s)
        test_energize(s)
        test_port_panel_ops(s)
        test_restart_while_released(s)
        test_stop_block_offsets(s)
        test_offsets_belong_to_their_provider(s)
    finally:
        s.close()
    print("ALL PASS")


if __name__ == "__main__":
    main()
