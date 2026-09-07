#!/usr/bin/env python3
"""Host-side verification that a deep planner buffer starts.

$398 (planner buffer blocks) is a reboot-required setting with a range of
30 to 1000. The core links the block ring at start, one block after
another, and the index that walks the ring has to hold every block
number up to the buffer size: a byte-wide index never reaches 255, and a
buffer of 255 blocks or more then spins the controller at start. The
main thread sits at 100 percent in the planner reset, the Grbl port
accepts a connection (the listener is its own thread) and never answers,
and the supervisor still reports the controller running.

Runs the native grblHAL_glowforge binary in null-sink mode (no hardware,
no root) and drives it over TCP, keeping one settings store across
restarts:

  1. a fresh store starts and reports its default $398
  2. $398=400 is accepted and read back
  3. the controller restarted on that store answers on the port, reports
     400 blocks, and the status report shows a planner that deep
  4. the same at $398=1000, the top of the range
  5. a motion line runs to completion on the deep buffer

A controller that connects and never answers after the restart is the
failure this harness exists for. Every exit kills the controller it
started, so a spinning one never outlives the run or holds the port.

The binary keeps its settings in EEPROM.DAT in the working directory, so
each run starts from defaults in a temporary directory and leaves
nothing behind.

Usage: planner_blocks_test.py [path-to-binary]
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
CONNECT_S = 8.0
ANSWER_S = 5.0

DEEP = 400
TOP = 1000

_live = None            # the controller process a failure has to take down


def fail(msg):
    print("FAIL: %s" % msg)
    if _live is not None:
        _live.stop()
    sys.exit(1)


class Session:
    """One null-sink controller process on a settings store that outlives it."""

    def __init__(self, workdir):
        global _live
        self.workdir = workdir
        env = dict(os.environ, GF_STATE_DIR=self.workdir, FFLOG_STDERR="1")
        for key in ("GFSINK", "GF_SWITCH_FILE", "GF_VERDICT_FILE"):
            env.pop(key, None)
        self.proc = subprocess.Popen([BIN, "-p", str(PORT)],
                                     cwd=self.workdir, env=env,
                                     stdout=subprocess.DEVNULL,
                                     stderr=subprocess.PIPE)
        _live = self
        self.sock = self.connect()
        self.read(1.0)                  # banner and any boot messages

    def connect(self):
        end = time.time() + CONNECT_S
        while time.time() < end:
            try:
                return socket.create_connection(("127.0.0.1", PORT), timeout=1)
            except OSError:
                time.sleep(0.1)
        err = b""
        if self.proc.poll() is not None:
            err = self.proc.stderr.read() or b""
        fail("the controller never opened the port within %.0f s (exit=%s)\n%s"
             % (CONNECT_S, self.proc.poll(), err.decode(errors="replace")))

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

    def answers(self):
        """True when the controller answers a settings query at all."""
        return "$398=" in self.send("$398", ANSWER_S)

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

    def report(self):
        return self.send("?")

    def state(self):
        m = re.search(r"<(\w+)", self.report())
        return m.group(1) if m else "?"

    def planner_free(self):
        """The planner blocks the status report shows free (Bf:<blocks>,<rx>)."""
        m = re.search(r"\|Bf:(\d+),", self.report())
        if not m:
            fail("no buffer field in the status report")
        return int(m.group(1))

    def wait_idle(self, timeout=5.0):
        end = time.time() + timeout
        while time.time() < end:
            if self.state() == "Idle":
                return True
            time.sleep(0.1)
        return False

    def stop(self):
        global _live
        try:
            self.sock.close()
        except (OSError, AttributeError):
            pass
        self.proc.terminate()
        try:
            self.proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            self.proc.kill()
            self.proc.wait()
        _live = None


def check(ok, ok_msg, fail_msg):
    if not ok:
        fail(fail_msg)
    print("ok   %s" % ok_msg)


def restart_on(workdir, blocks):
    """Write $398, restart on the same store, and prove the controller answers."""
    s = Session(workdir)
    s.write_setting("$398=%d" % blocks)
    check(s.setting("$398") == str(blocks),
          "$398=%d accepted" % blocks, "$398=%d not read back" % blocks)
    s.stop()

    s = Session(workdir)
    check(s.answers(),
          "the restarted controller answers at $398=%d" % blocks,
          "the controller restarted at $398=%d accepts the connection and never "
          "answers within %.0f s: the start spins" % (blocks, ANSWER_S))
    check(s.setting("$398") == str(blocks),
          "it reports $398=%d" % blocks, "it reports another $398")
    s.write_setting("$10=%d" % (int(s.setting("$10")) | 2))     # buffer state in the report
    free = s.planner_free()
    check(free >= blocks - 1,
          "the status report shows %d planner blocks free" % free,
          "the status report shows %d planner blocks free, expected at least %d"
          % (free, blocks - 1))
    return s


def main():
    if not os.path.isfile(BIN):
        fail("no controller binary at %s" % BIN)
    workdir = tempfile.mkdtemp(prefix="planner-blocks-")
    try:
        s = Session(workdir)
        check(s.answers(), "a fresh store starts and answers",
              "a fresh store does not answer")
        print("     default $398=%s" % s.setting("$398"))
        s.stop()

        s = restart_on(workdir, DEEP)
        s.stop()

        s = restart_on(workdir, TOP)
        reply = s.send("G91 G1 X1 F600", 1.5)
        check("ok" in reply and "error" not in reply and "ALARM" not in reply,
              "a motion line is accepted on the deep buffer",
              "the motion line was refused: %r" % reply)
        check(s.wait_idle(), "the move completes and the controller is Idle",
              "the controller did not return to Idle after the move")
        s.stop()
        print("PASS: the planner buffer starts at %d and %d blocks" % (DEEP, TOP))
    finally:
        if _live is not None:
            _live.stop()
        shutil.rmtree(workdir, ignore_errors=True)


if __name__ == "__main__":
    main()
