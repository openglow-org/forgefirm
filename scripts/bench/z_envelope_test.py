#!/usr/bin/env python3
"""Host-side verification that the Z envelope survives a settings write.

The Z soft limit belongs to the driver, not to $20. glowforge_homing.c
owns sys.work_envelope, sys.homed and sys.soft_limits for Z: Z is always
referenced, to the lens hall edge or to where the lens stands, and the
core knows neither. The core recomputes both masks from the settings and
drops Z when it does, so driver.c re-applies the envelope from the
settings-changed chain, after the rest of the chain has run.

The core drops Z in two places this harness drives:

  $20  clears sys.soft_limits for every axis in the setter
  $13x clears sys.homed for the axis as well, when the value changes

Runs the native grblHAL_glowforge binary in null-sink mode (no hardware,
no root) and drives it over TCP:

  1. the envelope is in force from boot, with $20 off: an unreferenced Z
     is collapsed to where the lens stands, so a Z move alarms
  2. it is collapsed, not merely present: a move each way alarms
  3. it is Z alone: X and Y follow $20 as the core intends, and stay free
  4. $20=0, soft limits off for every axis, does not free Z
  5. a $132 write, which un-homes Z and clears the mask in the setter,
     does not free Z either
  6. X and Y are still free after both writes

A referenced lens opens the envelope to the window the shared settings
hold, the same keys the daemon's wizards build their Z from. forgectrl
leaves the lens on its hall edge and a marker in the state directory
before the controller starts; the driver takes it as it loads its
settings and places the edge on its step grid:

  7. the marker with the edge alone: Z sits at the edge's grid step, the
     fallback window (10 below, 12 above) runs to its ends, and two
     half-steps past either end alarms
  8. the marker with the focus card's stops (14 below, 20 above): the
     wider window runs to its ends, two half-steps past either alarms
  9. a stop count out of range (41) falls back on its side alone

The X and Y soft limits are the driver's as well: off while the
position is not trusted (the core's $20 cannot be turned on with $22
off), on after a home, when the envelope is the bed. A scripted home
(homing_mode = gfcloud with a runner that exits 0, the host hook) sets
them:

 10. after the home a program move past X or Y max raises ALARM:2
     before any motion, a jog past it is refused with error 15, and a
     move inside the bed runs; a stream fault or a controller start
     drops them again with the reference

The binary keeps its settings in EEPROM.DAT in the working directory, so
each run starts from defaults in a temporary directory and leaves
nothing behind.

Usage: z_envelope_test.py [path-to-binary]
       (default ./build-native/grblHAL_glowforge)
"""
import os
import re
import shutil
import socket
import subprocess
import sys
import tempfile
import time

BIN = os.path.abspath(sys.argv[1] if len(sys.argv) > 1 else "build-native/grblHAL_glowforge")
PORT = 2400

ALARM_SOFT_LIMIT = "ALARM:2"


def fail(msg):
    print("FAIL: %s" % msg)
    sys.exit(1)


class Session:
    """One null-sink controller process with its own settings store.
    `conf` (key: value) is the shared config the driver reads; with
    `referenced` the lens marker forgectrl leaves is in the state
    directory, so the driver takes the lens reference at its start."""

    def __init__(self, conf=None, referenced=False):
        self.workdir = tempfile.mkdtemp(prefix="z-envelope-")
        env = dict(os.environ, GF_STATE_DIR=self.workdir, FFLOG_STDERR="1")
        for key in ("GFSINK", "GF_SWITCH_FILE", "GF_VERDICT_FILE"):
            env.pop(key, None)
        conf_path = os.path.join(self.workdir, "forgefirm.conf")
        with open(conf_path, "w") as f:
            for k, v in (conf or {}).items():
                f.write("%s = %s\n" % (k, v))
        env["GFHOME_CONF"] = conf_path
        if referenced:
            with open(os.path.join(self.workdir, "lens.home"), "w") as f:
                f.write("edge 0 3 3\n")
        self.proc = subprocess.Popen([BIN, "-p", str(PORT)],
                                     cwd=self.workdir, env=env,
                                     stdout=subprocess.DEVNULL,
                                     stderr=subprocess.PIPE)
        self.sock = self.connect()
        self.read(1.0)                  # banner and any boot messages

    def connect(self):
        for _ in range(50):
            try:
                return socket.create_connection(("127.0.0.1", PORT), timeout=1)
            except OSError:
                time.sleep(0.1)
        err = b""
        if self.proc.poll() is not None:
            err = self.proc.stderr.read() or b""
        fail("cannot connect to the controller (exit=%s)\n%s"
             % (self.proc.poll(), err.decode(errors="replace")))

    def read(self, timeout=0.8):
        out = b""
        end = time.time() + timeout
        while time.time() < end:
            self.sock.settimeout(max(0.05, end - time.time()))
            try:
                data = self.sock.recv(4096)
            except (socket.timeout, OSError):
                break
            if not data:
                break
            out += data
        return out.decode(errors="replace")

    def send(self, line, timeout=1.0):
        self.sock.sendall(b"?" if line == "?" else (line + "\n").encode())
        return self.read(timeout)

    def setting(self, key):
        reply = self.send(key)
        m = re.search(r"^%s=(\S+)" % re.escape(key), reply, re.M)
        if not m:
            fail("no value reported for %s: %r" % (key, reply))
        return m.group(1)

    def write_setting(self, assignment):
        reply = self.send(assignment)
        if "ok" not in reply:
            fail("settings write %r refused: %r" % (assignment, reply))

    def state(self):
        m = re.search(r"<(\w+)", self.send("?"))
        return m.group(1) if m else "?"

    def mpos_z(self):
        """The reported machine Z (no work offset is set in a session)."""
        m = re.search(r"[MW]Pos:(-?[\d.]+),(-?[\d.]+),(-?[\d.]+)", self.send("?"))
        if not m:
            fail("no position in the status report")
        return float(m.group(3))

    def wait_idle(self, timeout=15.0):
        """A move that ran plays out before the next line is judged: the
        soft limit on a moving machine holds first and alarms after."""
        end = time.time() + timeout
        while time.time() < end:
            if self.state() == "Idle":
                return
            time.sleep(0.1)
        fail("the controller did not return to Idle")

    def move(self, gcode):
        """Run one move. Returns True when the soft limit blocked it.
        An alarm needs a reset and an unlock before the next move."""
        reply = self.send(gcode, 1.5)
        if ALARM_SOFT_LIMIT in reply:
            self.recover()
            return True
        if "ok" not in reply:
            fail("unexpected reply to %r: %r" % (gcode, reply))
        return False

    def recover(self):
        self.sock.sendall(b"\x18")
        self.read(1.0)
        self.send("$X")
        if self.state() != "Idle":
            fail("controller did not return to Idle after an alarm")

    def close(self):
        try:
            self.sock.close()
        except OSError:
            pass
        self.proc.terminate()
        try:
            self.proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            self.proc.kill()
        shutil.rmtree(self.workdir, ignore_errors=True)


def check(ok, ok_msg, fail_msg):
    if not ok:
        fail(fail_msg)
    print("ok   %s" % ok_msg)


def blocked_both_ways(s, when):
    check(s.move("G0 Z1"), "Z+ blocked %s" % when, "a Z+ move ran %s" % when)
    check(s.move("G0 Z-1"), "Z- blocked %s" % when, "a Z- move ran %s" % when)


def free_in_xy(s, when):
    ran = not s.move("G0 X1") and not s.move("G0 Y1")
    check(ran, "X and Y free %s" % when, "an X or Y move was blocked %s" % when)


def window_holds(s, steps, below, above, what):
    """The envelope of a referenced lens whose edge sits on grid step
    `steps`: the ends of the reach (`below` and `above` half-steps from
    the edge) run, two half-steps past either end alarms. The controller
    keeps a half-step of slack beyond the reach, so one past is not
    judged here; two is outside on every head."""
    spm = float(s.setting("$102"))
    top, bottom = (steps + above) / spm, (steps - below) / spm
    s.wait_idle()
    check(not s.move("G0 Z%.3f" % top), "%s: the top of the reach, Z %.3f, runs" % (what, top),
          "%s: Z %.3f, the top of the reach, was blocked" % (what, top))
    s.wait_idle()
    check(not s.move("G0 Z%.3f" % bottom), "%s: the bottom of the reach, Z %.3f, runs" % (what, bottom),
          "%s: Z %.3f, the bottom of the reach, was blocked" % (what, bottom))
    over, under = (steps + above + 2) / spm, (steps - below - 2) / spm
    s.wait_idle()
    check(s.move("G0 Z%.3f" % over), "%s: two half-steps over the top, Z %.3f, alarms" % (what, over),
          "%s: Z %.3f, two half-steps over the top, ran" % (what, over))
    s.wait_idle()
    check(s.move("G0 Z%.3f" % under), "%s: two half-steps under the bottom, Z %.3f, alarms" % (what, under),
          "%s: Z %.3f, two half-steps under the bottom, ran" % (what, under))


def referenced_cases():
    """The lens referenced by forgectrl's marker: the envelope is the
    window the shared settings hold, as the daemon's wizards read it."""
    edge = 3.35
    conf = {"lens_hall_edge_z_mm": "%.2f" % edge}
    s = Session(conf=conf, referenced=True)
    try:
        spm = float(s.setting("$102"))
        steps = int(round(edge * spm))
        z = s.mpos_z()
        check(abs(z - steps / spm) < 0.002,
              "referenced: Z sits at the edge's grid step, %.3f" % z,
              "referenced: Z reads %.3f, not the edge's grid step %.3f" % (z, steps / spm))
        window_holds(s, steps, 10, 12, "the fallback window")
        free_in_xy(s, "with the lens referenced")
    finally:
        s.close()

    s = Session(conf=dict(conf, lens_stop_below_steps="14", lens_stop_above_steps="20"), referenced=True)
    try:
        window_holds(s, steps, 14, 20, "the focus card's window")
    finally:
        s.close()

    s = Session(conf=dict(conf, lens_stop_below_steps="41", lens_stop_above_steps="20"), referenced=True)
    try:
        window_holds(s, steps, 10, 20, "a below count out of range")
    finally:
        s.close()


def homed_cases():
    """After a scripted gfcloud home the bed is the X/Y envelope."""
    conf = {"lens_hall_edge_z_mm": "3.35", "homing_mode": "gfcloud", "gfcloud_home_cmd": "true"}
    s = Session(conf=conf, referenced=True)
    try:
        free_in_xy(s, "before the home")
        x_travel = float(s.setting("$130"))
        y_travel = float(s.setting("$131"))
        reply = s.send("$H", 5.0)
        if "ok" not in reply:
            fail("the scripted home was refused: %r" % reply)
        s.wait_idle()
        st = s.send("?")
        m = re.search(r"MPos:(-?[\d.]+),(-?[\d.]+)", st)
        check(m and abs(float(m.group(1))) < 0.01 and abs(float(m.group(2))) < 0.01,
              "homed: X and Y sit at the origin", "homed: X/Y are not at the origin: %r" % st)
        check(s.move("G90 G0 X%.1f" % (x_travel + 5)), "homed: X past the bed alarms",
              "homed: a move past X max ran")
        s.wait_idle()
        check(s.move("G90 G0 Y%.1f" % (y_travel + 5)), "homed: Y past the bed alarms",
              "homed: a move past Y max ran")
        s.wait_idle()
        check(s.move("G90 G0 X-1"), "homed: X past the near edge alarms",
              "homed: a move past X min ran")
        s.wait_idle()
        reply = s.send("$J=G91X%.1fF1200" % (x_travel + 5), 1.5)
        check("error:15" in reply, "homed: a jog past the bed is refused with error 15",
              "homed: a jog past the bed was not refused with error 15: %r" % reply)
        # The core answers the line after an error with that error again
        # until an empty line (or a $ command) clears it: the sender's
        # acknowledgment at compatibility level 0.
        s.send("", 1.0)
        check(not s.move("G90 G0 X10 Y10"), "homed: a move inside the bed runs",
              "homed: a move inside the bed was blocked")
        s.wait_idle()
        # A settings write does not drop them: the driver re-applies the
        # mask after the core clears it.
        s.write_setting("$20=0")
        check(s.move("G90 G0 X%.1f" % (x_travel + 5)), "homed: X past the bed still alarms after a $20 write",
              "homed: a $20 write freed X")
        s.wait_idle()
    finally:
        s.close()


def main():
    if not os.path.isfile(BIN):
        fail("no controller binary at %s" % BIN)
    s = Session()
    try:
        soft = s.setting("$20")
        check(soft == "0", "$20=0 at boot, so the Z envelope is the driver's alone",
              "$20 is %s, not 0: this harness needs the default" % soft)

        blocked_both_ways(s, "at boot")
        free_in_xy(s, "at boot")

        # $20 clears sys.soft_limits for every axis inside the setter.
        s.write_setting("$20=0")
        blocked_both_ways(s, "after a $20 write")

        # $13x clears sys.homed for the axis too, but only when the value
        # changes, so move it and put it back. Both writes drop Z.
        z_travel = s.setting("$132")
        s.write_setting("$132=%.3f" % (float(z_travel) + 1.0))
        blocked_both_ways(s, "after a $132 write")
        s.write_setting("$132=%s" % z_travel)
        blocked_both_ways(s, "after $132 is restored")
        check(s.setting("$132") == z_travel, "$132 restored to %s" % z_travel,
              "$132 did not go back to %s" % z_travel)

        free_in_xy(s, "after the settings writes")
    finally:
        s.close()
    referenced_cases()
    homed_cases()
    print("PASS z_envelope_test")


if __name__ == "__main__":
    main()
