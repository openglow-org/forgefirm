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
    """One null-sink controller process with its own settings store."""

    def __init__(self):
        self.workdir = tempfile.mkdtemp(prefix="z-envelope-")
        env = dict(os.environ, GF_STATE_DIR=self.workdir, FFLOG_STDERR="1")
        for key in ("GFSINK", "GF_SWITCH_FILE", "GF_VERDICT_FILE"):
            env.pop(key, None)
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
    print("PASS z_envelope_test")


if __name__ == "__main__":
    main()
