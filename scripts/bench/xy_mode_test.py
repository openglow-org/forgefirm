#!/usr/bin/env python3
"""Host-side verification of the XY microstep mode wiring.

The XY microstep mode is one number in the shared config, xy_microsteps
(8, 16 or 32), and the driver derives three things from it at start and
never takes them typed: $100/$101, the machine tick, and the kernel stop
ramp. This harness drives the native grblHAL_glowforge binary in
null-sink mode (no hardware, no root) over TCP, one process per config:

  1. no key: x8, $100/$101 = 53.333, the 28160 Hz tick
  2. xy_microsteps = 16: 106.667, the 56320 Hz tick; a typed $100 is
     overwritten on the spot; $110 is left alone under a tick that
     carries it
  3. xy_microsteps = 32: 213.333, the 112640 Hz tick
  4. a value that is not a mode: x8 with a warning in the log
  5. GFSINK_RATE lowered under the mode's tick: $110/$111 are held at
     the feed the tick carries, one step per tick per axis

The binary keeps its settings in EEPROM.DAT in the working directory, so
each run starts from defaults in a temporary directory and leaves
nothing behind.

Usage: xy_mode_test.py [path-to-binary]
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
PORT = 2401


def fail(msg):
    print("FAIL: %s" % msg)
    sys.exit(1)


class Session:
    """One null-sink controller process with its own settings store and
    its own shared config file."""

    def __init__(self, conf=None, env_extra=None):
        self.workdir = tempfile.mkdtemp(prefix="xy-mode-")
        env = dict(os.environ, GF_STATE_DIR=self.workdir, FFLOG_STDERR="1",
                   FFLOG_LEVEL="info")
        for key in ("GFSINK", "GFSINK_RATE", "GF_SWITCH_FILE", "GF_VERDICT_FILE"):
            env.pop(key, None)
        conf_path = os.path.join(self.workdir, "forgefirm.conf")
        with open(conf_path, "w") as f:
            f.write(conf or "")
        env["GFHOME_CONF"] = conf_path
        env.update(env_extra or {})
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
        self.sock.sendall((line + "\n").encode())
        return self.read(timeout)

    def setting(self, key):
        reply = self.send(key)
        m = re.search(r"^%s=(\S+)" % re.escape(key), reply, re.M)
        if not m:
            fail("no value reported for %s: %r" % (key, reply))
        return float(m.group(1))

    def write_setting(self, assignment):
        reply = self.send(assignment)
        if "ok" not in reply:
            fail("settings write %r refused: %r" % (assignment, reply))

    def close(self):
        """Stop the process and return everything it logged."""
        try:
            self.sock.close()
        except OSError:
            pass
        self.proc.terminate()
        try:
            _out, err = self.proc.communicate(timeout=5)
        except subprocess.TimeoutExpired:
            self.proc.kill()
            _out, err = self.proc.communicate()
        shutil.rmtree(self.workdir, ignore_errors=True)
        return (err or b"").decode(errors="replace")


def check(ok, ok_msg, fail_msg):
    if not ok:
        fail(fail_msg)
    print("ok   %s" % ok_msg)


def near(got, want, tol=0.0005):
    return abs(got - want) <= tol


def expect_scale(s, mode, spm):
    for key in ("$100", "$101"):
        got = s.setting(key)
        check(near(got, spm), "%s=%.3f at x%d" % (key, got, mode),
              "%s is %.3f at x%d, want %.3f" % (key, got, mode, spm))


def expect_log(log, needle, what):
    check(needle in log, what, "the log does not say %r:\n%s" % (needle, log))


def case_default():
    s = Session()
    try:
        expect_scale(s, 8, 53.333)
        rate = s.setting("$110")
        check(near(rate, 12000.0), "$110=%.0f stands at x8" % rate,
              "$110 is %.3f at x8, want 12000" % rate)
    finally:
        log = s.close()
    expect_log(log, "x8 microsteps, 28160 Hz machine tick", "no key: x8, the 28160 Hz tick")


def case_x16():
    s = Session("xy_microsteps = 16\n")
    try:
        expect_scale(s, 16, 106.667)
        # A typed $100 is accepted and overwritten on the spot: the scale
        # is the mode's, never the sender's.
        s.write_setting("$100=53.333")
        got = s.setting("$100")
        check(near(got, 106.667), "a typed $100 is back at %.3f" % got,
              "a typed $100 stuck at %.3f under x16" % got)
        rate = s.setting("$110")
        check(near(rate, 12000.0), "$110=%.0f stands under a tick that carries it" % rate,
              "$110 is %.3f under x16, want 12000" % rate)
    finally:
        log = s.close()
    expect_log(log, "x16 microsteps, 56320 Hz machine tick", "x16: the 56320 Hz tick")


def case_x32():
    s = Session("xy_microsteps = 32\n")
    try:
        expect_scale(s, 32, 213.333)
    finally:
        log = s.close()
    expect_log(log, "x32 microsteps, 112640 Hz machine tick", "x32: the 112640 Hz tick")


def case_invalid():
    s = Session("xy_microsteps = 24\n")
    try:
        expect_scale(s, 8, 53.333)
    finally:
        log = s.close()
    expect_log(log, "xy_microsteps '24' is not 8, 16 or 32", "a value that is not a mode is refused with a warning")
    expect_log(log, "x8 microsteps, 28160 Hz machine tick", "and the machine runs at x8")


def case_low_tick():
    # 1000 Hz carries 1000 steps/s per axis: 562.5 mm/min at 106.667
    # steps/mm. $110/$111 are held there, and a typed value above it
    # comes back held.
    s = Session("xy_microsteps = 16\n", {"GFSINK_RATE": "1000"})
    try:
        expect_scale(s, 16, 106.667)
        for key in ("$110", "$111"):
            got = s.setting(key)
            check(near(got, 562.5, 0.05), "%s held at %.3f under a 1000 Hz tick" % (key, got),
                  "%s is %.3f under a 1000 Hz tick, want 562.5" % (key, got))
        s.write_setting("$110=6000")
        got = s.setting("$110")
        check(near(got, 562.5, 0.05), "a typed $110=6000 comes back held at %.3f" % got,
              "a typed $110=6000 stuck at %.3f under a 1000 Hz tick" % got)
    finally:
        log = s.close()
    expect_log(log, "1000 Hz machine tick", "GFSINK_RATE took the tick to 1000 Hz")
    expect_log(log, "held at the ceiling", "the hold is logged")


def main():
    if not os.path.isfile(BIN):
        fail("no controller binary at %s" % BIN)
    case_default()
    case_x16()
    case_x32()
    case_invalid()
    case_low_tick()
    print("PASS xy_mode_test")


if __name__ == "__main__":
    main()
