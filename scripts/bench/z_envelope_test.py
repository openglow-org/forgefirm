#!/usr/bin/env python3
# Copyright 2026 514 LLC d/b/a OpenGlow
# Written by Scott Wiederhold
# https://community.openglow.org
# SPDX-License-Identifier:    MIT
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

Two settings a sender writes could move the machine with no reference.
The core's homing cycle drives an axis toward a switch this machine does
not have, and the core runs the stored startup lines at every start:

 11. with $22=3 written, $H under homing_mode none and $HX under every
     mode (none, manual, gfcloud, switches) are refused, and no step
     ships: $22's homing bits are pinned off
 12. a startup line takes modal-state words only, in every word of every
     block: with the motors released (Alarm), $N0=G0X10, $N0=M103P1,
     $N0=G21G0X10, $N0=G21X10, $N0=G21(c)X10 (an axis word behind a
     comment), $N0=G21|G0X10 and $N1=G0X10 are refused, and $ME ships no
     step; $N0=G21G90 and $N1=G21G91 are stored. Stored lines
     that hold more (G21|G0X10, a motion block behind a modal one, and
     G21X10, an axis word behind a modal one, written into the settings
     file where the two stored lines sit) are cleared at the next start:
     the start ships no step, the referenced lens stays on its edge, and
     $N lists both slots empty

The binary keeps its settings in EEPROM.DAT in the working directory, so
each run starts from defaults in a temporary directory and leaves
nothing behind.

The referenced and homed cases (7 to 12) stand on the lens reference or
the home: a check that fails after a starve dropped the homing reference
runs them once more from new controllers (ctlport_test's rule).

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

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import ctlport_test as ct  # noqa: E402

BIN = os.path.abspath(sys.argv[1] if len(sys.argv) > 1 else "build-native/grblHAL_glowforge")


def free_port():
    """A TCP port nothing on this host holds: two harness runs on one host
    never meet on a fixed number."""
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


PORT = free_port()

ALARM_SOFT_LIMIT = "ALARM:2"


def fail(msg):
    # A check that fails in a session the host starved fails on the starve.
    ct.watch_fail_check()
    print("FAIL: %s" % msg)
    sys.exit(1)


class Session:
    """One null-sink controller process with its own settings store.
    `conf` (key: value) is the shared config the driver reads; with
    `referenced` the lens marker forgectrl leaves is in the state
    directory, so the driver takes the lens reference at its start, and
    the session stands on it: a dropped homing reference is a starve
    when a check fails after it (ctlport_test's rule); the armed clamp is
    one at every read."""

    def __init__(self, conf=None, referenced=False, workdir=None):
        self.workdir = workdir or tempfile.mkdtemp(prefix="z-envelope-")
        env = dict(os.environ, GF_STATE_DIR=self.workdir, FFLOG_STDERR="1")
        for key in ("GFSINK", "GF_SWITCH_FILE", "GF_VERDICT_FILE"):
            env.pop(key, None)
        self.conf_path = os.path.join(self.workdir, "forgefirm.conf")
        self.write_conf(conf)
        env["GFHOME_CONF"] = self.conf_path
        self.dump = os.path.join(self.workdir, "stream.bin")
        env["GFSINK_DUMP"] = self.dump
        if referenced:
            with open(os.path.join(self.workdir, "lens.home"), "w") as f:
                f.write("edge 0 3 3\n")
        self.proc = subprocess.Popen([BIN, "-p", str(PORT)],
                                     cwd=self.workdir, env=env,
                                     stdout=subprocess.DEVNULL,
                                     stderr=subprocess.PIPE)
        self.watch = ct.WATCH = ct.StderrWatch(self.proc, "z-envelope", homed=referenced)
        self.sock = self.connect()
        self.read(1.0)                  # banner and any boot messages

    def write_conf(self, conf):
        """The shared config; the driver reads homing_mode at every $H."""
        with open(self.conf_path, "w") as f:
            for k, v in (conf or {}).items():
                f.write("%s = %s\n" % (k, v))

    def connect(self):
        for _ in range(50):
            try:
                return socket.create_connection(("127.0.0.1", PORT), timeout=1)
            except OSError:
                time.sleep(0.1)
        if self.proc.poll() is not None:
            self.watch.thread.join(1)
        fail("cannot connect to the controller (exit=%s)\n%s"
             % (self.proc.poll(), self.watch.text()))

    def read(self, timeout=0.8):
        ct.watch_check()
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

    def ask(self, line, timeout=2.0):
        """send(), returning once the answer is whole: an ok, an error, or,
        for '?', the status report."""
        self.sock.sendall(b"?" if line == "?" else (line + "\n").encode())
        out, end = "", time.time() + timeout
        while time.time() < end:
            out += self.read(0.05)
            if (">" in out if line == "?" else re.search(r"^(ok|error:\d+)\r?$", out, re.M)):
                break
        return out

    def quick_state(self):
        m = re.search(r"<(\w+)", self.ask("?"))
        return m.group(1) if m else "?"

    def quick_z(self):
        m = re.search(r"[MW]Pos:(-?[\d.]+),(-?[\d.]+),(-?[\d.]+)", self.ask("?"))
        if not m:
            fail("no position in the status report")
        return float(m.group(3))

    def setting(self, key):
        reply = self.send(key)
        m = re.search(r"^%s=(\S+)" % re.escape(key), reply, re.M)
        if not m:
            fail("no value reported for %s: %r" % (key, reply))
        return m.group(1)

    def write_setting(self, assignment):
        reply = self.send(assignment)
        if not re.search(r"^ok\r?$", reply, re.M):
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
        if not re.search(r"^ok\r?$", reply, re.M):
            fail("unexpected reply to %r: %r" % (gcode, reply))
        return False

    def steps(self):
        """Step ticks shipped so far: a tick byte (bit 7 clear) with an X, Y
        or Z step bit (0x25) set, as ctlport_test.py counts them."""
        try:
            with open(self.dump, "rb") as f:
                data = f.read()
        except OSError:
            return 0
        return sum(1 for b in data if not b & 0x80 and b & 0x25)

    def recover(self):
        self.sock.sendall(b"\x18")
        self.read(1.0)
        self.send("$X")
        if self.state() != "Idle":
            fail("controller did not return to Idle after an alarm")

    def close(self, keep=False):
        try:
            self.sock.close()
        except OSError:
            pass
        self.proc.terminate()
        try:
            self.proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            self.proc.kill()
        self.watch.thread.join(2)
        if ct.WATCH is self.watch:
            ct.WATCH = None
        if not keep:
            shutil.rmtree(self.workdir, ignore_errors=True)
        self.watch.check()              # the armed clamp's starve after the case's last read


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

        def refused_still(gcode, what):
            # ALARM:2 before any motion: the shipped stream gains no step.
            n0 = s.steps()
            blocked = s.move(gcode)
            n1 = s.steps()
            check(blocked and n1 == n0, "homed: %s alarms before any motion" % what,
                  "homed: %s %s, %d step ticks shipped" % (what, "alarmed" if blocked else "ran", n1 - n0))
            s.wait_idle()

        refused_still("G90 G0 X%.1f" % (x_travel + 5), "a move past X max")
        refused_still("G90 G0 Y%.1f" % (y_travel + 5), "a move past Y max")
        refused_still("G90 G0 X-1", "a move past X min")
        reply = s.send("$J=G91X%.1fF1200" % (x_travel + 5), 1.5)
        check("error:15" in reply, "homed: a jog past the bed is refused with error 15",
              "homed: a jog past the bed was not refused with error 15: %r" % reply)
        # The core answers the line after an error with that error again
        # until an empty line (or a $ command) clears it: the sender's
        # acknowledgment at compatibility level 0.
        s.send("", 1.0)
        n0 = s.steps()
        check(not s.move("G90 G0 X10 Y10"), "homed: a move inside the bed runs",
              "homed: a move inside the bed was blocked")
        s.wait_idle()
        # The step count the refusals were judged by does see motion.
        check(s.steps() > n0, "homed: the move inside the bed shipped steps",
              "homed: the move inside the bed shipped no step: the stream dump sees no motion")
        # A settings write does not drop them: the driver re-applies the
        # mask after the core clears it.
        s.write_setting("$20=0")
        check(s.move("G90 G0 X%.1f" % (x_travel + 5)), "homed: X past the bed still alarms after a $20 write",
              "homed: a $20 write freed X")
        s.wait_idle()
    finally:
        s.close()


def homing_cases():
    """Rule 11: $22 written by a sender does not reach the core's homing
    cycle, under any homing mode."""
    s = Session(conf={"lens_hall_edge_z_mm": "3.35"}, referenced=True)
    try:
        z0 = s.quick_z()
        reply = s.ask("$22=3")          # enabled and single-axis commands, as a sender writes it
        check(re.search(r"^ok\r?$", reply, re.M), "$22=3 is accepted", "$22=3: %r" % reply)
        reply = s.ask("$H")
        check("error:5" in reply, "$H under homing_mode none is refused with $22=3 written",
              "$H under homing_mode none with $22=3: %r" % reply)
        for mode in ("none", "manual", "gfcloud", "switches"):
            s.write_conf({"lens_hall_edge_z_mm": "3.35", "homing_mode": mode,
                          "gfcloud_home_cmd": "true"})
            reply = s.ask("$HX")
            check("error:5" in reply, "$HX under homing_mode %s is refused" % mode,
                  "$HX under homing_mode %s: %r" % (mode, reply))
        s.ask("")                       # the error the core holds until an empty line
        time.sleep(0.5)
        n, z = s.steps(), s.quick_z()
        check(n == 0 and abs(z - z0) < 0.001, "no step shipped, Z still %.3f" % z,
              "%d step ticks shipped, Z %.3f from %.3f" % (n, z, z0))
    finally:
        s.close()


def nvs_crc(data):
    """The core's settings-store checksum (a Modbus CRC-16)."""
    crc = 0xFFFF
    for b in data:
        crc ^= b
        for _ in range(8):
            crc = (crc >> 1) ^ 0xA001 if crc & 1 else crc >> 1
    return crc


def stored_line_at(data, text):
    """Where the settings file holds the startup line `text`, and the line's
    length: the core stores a fixed-length line with its CRC-16 after it,
    low byte first."""
    needle = text.encode() + b"\0"
    i = data.find(needle)
    while i >= 0:
        for size in (70, 60):
            end = i + size
            if end + 2 <= len(data) and nvs_crc(data[i:end]) == data[end] | data[end + 1] << 8:
                return i, size
        i = data.find(needle, i + 1)
    fail("the settings file does not hold the startup line %r" % text)


def startup_line(s, n):
    m = re.search(r"^\$N%d=([^\r\n]*)" % n, s.ask("$N"), re.M)
    if not m:
        fail("$N lists no $N%d" % n)
    return m.group(1)


def startup_line_cases():
    """Rule 12: a startup line is modal state only, stored or already in
    the settings file."""
    s = Session()
    stored = False
    try:
        s.ask("$MD")
        st = s.quick_state()
        check(st == "Alarm", "the motors are released (Alarm)", "$MD left the state %s" % st)
        for line in ("$N0=G0X10", "$N0=M103P1", "$N0=G21G0X10", "$N0=G21X10", "$N0=G21(c)X10",
                     "$N0=G21|G0X10", "$N1=G0X10"):
            reply = s.ask(line)
            check("error" in reply, "%s is refused" % line, "%s: %r" % (line, reply))
        s.ask("$ME")
        time.sleep(0.5)
        st, n = s.quick_state(), s.steps()
        check(st == "Idle" and n == 0, "$ME ships no step",
              "after $ME: state %s, %d step ticks shipped" % (st, n))
        reply = s.ask("$N0=G21G90")
        check(re.search(r"^ok\r?$", reply, re.M) and startup_line(s, 0) == "G21G90",
              "$N0=G21G90 is stored", "$N0=G21G90 was not stored: %r" % reply)
        reply = s.ask("$N1=G21G91")
        check(re.search(r"^ok\r?$", reply, re.M) and startup_line(s, 1) == "G21G91",
              "$N1=G21G91 is stored", "$N1=G21G91 was not stored: %r" % reply)
        time.sleep(0.5)                 # the settings file is written at Idle
        stored = True
    finally:
        s.close(keep=stored)            # the settings file goes on to the next start

    # Lines $N0 and $N1 refuse, written straight into the settings store.
    path = os.path.join(s.workdir, "EEPROM.DAT")
    data = bytearray(open(path, "rb").read())
    for held, planted in (("G21G90", b"G21|G0X10"),     # a modal block, then a motion block
                          ("G21G91", b"G21X10")):       # a modal word, then an axis word
        at, size = stored_line_at(bytes(data), held)
        line = planted.ljust(size, b"\0")
        crc = nvs_crc(line)
        data[at:at + size + 2] = line + bytes((crc & 0xFF, crc >> 8))
    with open(path, "wb") as f:
        f.write(data)

    s = Session(conf={"lens_hall_edge_z_mm": "3.35"}, referenced=True, workdir=s.workdir)
    try:
        m = re.search(r"^\$102=(\S+)", s.ask("$102"), re.M)
        spm = float(m.group(1)) if m else fail("no value reported for $102")
        edge = round(3.35 * spm) / spm
        n, z = s.steps(), s.quick_z()
        check(n == 0 and abs(z - edge) < 0.002, "the start ran no stored motion: no step, Z on the edge",
              "the start shipped %d step ticks, Z %.3f (the edge %.3f)" % (n, z, edge))
        for slot in (0, 1):
            held = startup_line(s, slot)
            check(held == "", "the stored line $N%d was cleared" % slot,
                  "$N%d still holds %r" % (slot, held))
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
    ct.run_case(referenced_cases)
    ct.run_case(homed_cases)
    ct.run_case(homing_cases)
    ct.run_case(startup_line_cases)
    print("PASS z_envelope_test")


if __name__ == "__main__":
    main()
