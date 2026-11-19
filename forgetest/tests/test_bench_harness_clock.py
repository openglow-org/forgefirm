# Copyright 2026 514 LLC d/b/a OpenGlow
# Written by Scott Wiederhold
# https://community.openglow.org
# SPDX-License-Identifier:    MIT

"""The null-sink harnesses under scripts/bench, which grblHAL-glowforge's
CI runs, time their waits on the monotonic clock, the one the controller
runs on. A host's wall clock can step forward by seconds when its time is
synced, and a wait timed on it ends before the controller has had its time
to answer: the harness then fails a controller that answered in a
millisecond.

Each wait ctlport_test.py shares with the other harnesses runs here against
a stand-in that answers 0.1 s after it is asked, with the harness's wall
clock stepped 2.4 s forward right after the wait's first read of it: where
a 2 to 3 s step lands when it falls inside a 2 s wait. The wait must still
return the answer. The harnesses' own waits are copies of the same loops,
and no harness names a source of the wall clock: time.time or
time.time_ns, a name imported from time or the module under another name,
datetime, or CLOCK_REALTIME."""
import contextlib
import importlib.util
import io
import os
import re
import socket
import sys
import threading
import time as real_time
import types
import unittest
from unittest import mock

REPO = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
BENCH = os.path.join(REPO, "scripts", "bench")
# The harnesses grblHAL-glowforge's CI runs (its build.yml).
HARNESSES = ("laser_stream_test", "laser_lifecycle_test", "z_envelope_test", "xy_mode_test",
             "planner_blocks_test", "ctlport_test", "mcode_test", "envelope_test",
             "manual_home_test", "cool_report_test", "tray_test")
STEP_S = 2.4        # a wall-clock step as a synced host takes it; each wait is given 2 s
ANSWER_S = 0.1      # how long the stand-in takes to answer
TIMEOUT_S = 2.0
# The ways a harness can name the wall clock.
WALL_CLOCK = re.compile(r"\btime\.time(_ns)?\b|\bfrom\s+time\s+import\b|\bimport\s+time\s+as\b"
                        r"|\bdatetime\b|\bCLOCK_REALTIME")


def load_ctlport():
    """ctlport_test.py as a module of its own, run from its source (no
    bytecode is written next to it)."""
    spec = importlib.util.spec_from_file_location("bench_ctlport_test",
                                                  os.path.join(BENCH, "ctlport_test.py"))
    mod = importlib.util.module_from_spec(spec)
    saved = sys.dont_write_bytecode
    sys.dont_write_bytecode = True
    try:
        spec.loader.exec_module(mod)
    finally:
        sys.dont_write_bytecode = saved
    return mod


class SteppedClock:
    """The time module as the harness sees it: the real one, except that
    the wall clock (time() and time_ns()) steps STEP_S forward right after
    its first read."""

    def __init__(self):
        self.reads = 0

    def time(self):
        self.reads += 1
        return real_time.time() + (STEP_S if self.reads > 1 else 0.0)

    def time_ns(self):
        return int(self.time() * 1e9)

    def __getattr__(self, name):
        return getattr(real_time, name)


class StandIn:
    """A controller on a loopback port: every '?' gets an Idle report and
    every line an ok, ANSWER_S after it arrives."""

    def __init__(self):
        self.srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self.srv.bind(("127.0.0.1", 0))
        self.srv.listen(1)
        self.port = self.srv.getsockname()[1]
        threading.Thread(target=self.serve, daemon=True).start()

    def serve(self):
        try:
            conn, _ = self.srv.accept()
        except OSError:
            return
        with conn:
            while True:
                try:
                    data = conn.recv(4096)
                except OSError:
                    return
                if not data:
                    return
                for b in data:
                    if b in b"?\n":
                        real_time.sleep(ANSWER_S)
                        conn.sendall(b"<Idle|MPos:0.000,0.000,0.000|FS:0,0>\r\n" if b == ord("?")
                                     else b"ok\r\n")

    def close(self):
        self.srv.close()


class FakePort:
    """The port's client, its jog ending ANSWER_S after it was made."""

    def __init__(self):
        self.end = real_time.monotonic() + ANSWER_S

    def state(self):
        done = real_time.monotonic() >= self.end
        return {"state": "Idle" if done else "Jog", "port_jog": not done}


class SharedWaits(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.ct = load_ctlport()
        cls.peer = StandIn()
        with contextlib.redirect_stdout(io.StringIO()):
            cls.sender = cls.ct.Sender(cls.peer.port)

    @classmethod
    def tearDownClass(cls):
        cls.sender.close()
        cls.peer.close()
        cls.sender.thread.join(2)

    def stepped(self, wait):
        """wait() under the stepped wall clock; a wait that ends early
        calls the harness's fail(), which exits."""
        out = io.StringIO()
        with mock.patch.object(self.ct, "time", SteppedClock()), contextlib.redirect_stdout(out):
            try:
                return wait()
            except SystemExit:
                self.fail("the wait ended at a %.1f s wall-clock step: %s"
                          % (STEP_S, out.getvalue().strip()))

    def test_status_poll(self):
        self.assertEqual(self.stepped(self.sender.state), "Idle")

    def test_send(self):
        self.assertEqual(self.stepped(lambda: self.sender.send("G4 P0", timeout=TIMEOUT_S)), "ok")

    def test_wait_state(self):
        self.stepped(lambda: self.sender.wait_state("Idle", timeout=TIMEOUT_S))

    def test_port_idle(self):
        session = types.SimpleNamespace(port=FakePort())
        st = self.stepped(lambda: self.ct.Session.port_idle(session, timeout=TIMEOUT_S))
        self.assertEqual(st["state"], "Idle")

    def test_starve_summary(self):
        """The clamp's summary line follows the clamp line once the stream
        has ramped down; the wait for it is how the starve's worst lag gets
        into the rerun's line, the only thing the wait produces."""
        r, w = os.pipe()
        rf = os.fdopen(r, "rb")
        self.addCleanup(rf.close)
        watch = self.ct.StderrWatch(types.SimpleNamespace(stderr=rf), "clock")
        with os.fdopen(w, "wb", buffering=0) as wf:
            wf.write(("gfstream: %s (max lead 4.0 ms)\n" % self.ct.CLAMP_LINE).encode())
            end = real_time.monotonic() + 5.0
            while not watch.clamped and real_time.monotonic() < end:
                real_time.sleep(0.01)
            self.assertTrue(watch.clamped)
            later = threading.Timer(ANSWER_S, wf.write,
                                    args=(b"gfstream: run: 3 late events clamped, max behind 25.1 ms\n",))
            later.start()
            try:
                with self.assertRaises(self.ct.HostStarved) as raised:
                    self.stepped(watch.check)
            finally:
                later.join()
        watch.thread.join(2)
        self.assertIn("max behind 25.1 ms", str(raised.exception))


class NoWallClock(unittest.TestCase):
    def test_no_harness_names_the_wall_clock(self):
        hits = {}
        for name in HARNESSES:
            with open(os.path.join(BENCH, name + ".py")) as f:
                lines = [n for n, line in enumerate(f, 1) if WALL_CLOCK.search(line)]
            if lines:
                hits[name + ".py"] = lines
        self.assertEqual(hits, {}, "harnesses that name the wall clock, by line")


if __name__ == "__main__":
    unittest.main()
