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
datetime, or CLOCK_REALTIME.

The sender's wait for Idle after a jog it sent waits for the jog: a loaded
host can leave a jog planned and not yet started for several polls, each
answering Idle at the old position, and a wait that took those for the
jog's end sends the next jog into a moving machine. The old position is
the one a poll gives just before the jog goes, so a frame shift or the
port's jog since the sender last polled is not taken for the jog."""
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


class LateJog(StandIn):
    """A controller whose motion starts late: after the ok of a jog ($J=),
    a home ($H) or a G0 or G1 move, the first LATE polls answer Idle where
    the head stood, the next two Jog on the way, and the rest Idle at the
    motion's end, 5 mm on. An M103 line shifts
    the reported frame 3 mm with no motion, as the tray's mode does, and
    `x` moved from outside is the port's jog. Answers at once."""

    LATE = 4

    def __init__(self):
        self.polls_after_jog = None
        self.x = 0.0                # where the head reads before the jog
        super().__init__()

    def serve(self):
        try:
            conn, _ = self.srv.accept()
        except OSError:
            return
        line = b""
        with conn:
            while True:
                try:
                    data = conn.recv(4096)
                except OSError:
                    return
                if not data:
                    return
                for b in data:
                    if b == ord("?"):
                        n = self.polls_after_jog
                        if n is None or n < self.LATE:
                            st, x = "Idle", self.x
                        elif n < self.LATE + 2:
                            st, x = "Jog", self.x + (n - self.LATE + 1)
                        else:
                            st, x = "Idle", self.x + 5.0
                        if n is not None:
                            self.polls_after_jog = n + 1
                        conn.sendall(b"<%s|MPos:%.3f,0.000,0.000|FS:0,0>\r\n" % (st.encode(), x))
                    elif b == ord("\n"):
                        if line.startswith(b"M103"):
                            self.x += 3.0
                        if line.startswith((b"$J=", b"$H", b"G0 ", b"G1 ")):
                            self.polls_after_jog = 0
                        line = b""
                        conn.sendall(b"ok\r\n")
                    else:
                        line += bytes([b])


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


class JogWaits(unittest.TestCase):
    def test_the_idle_before_a_jog_starts_is_not_its_end(self):
        # After the sender's last poll, nothing; a frame shift (M103, no
        # motion); the port's jog. Each moves the reported position, or
        # not, before the sender's own jog, whose first polls answer Idle.
        # And the sender's other motion lines, a G0 move and a home.
        ct = load_ctlport()

        def frame_shift(sender, peer):
            self.assertEqual(sender.send("M103 P0", timeout=TIMEOUT_S), "ok")

        def port_move(sender, peer):
            peer.x += 2.0

        jog = "$J=G91 X5 F600"
        for name, between, line in (("control", lambda s, p: None, jog), ("frame shift", frame_shift, jog),
                                    ("port move", port_move, jog), ("g-code move", lambda s, p: None, "G0 X5"),
                                    ("home", lambda s, p: None, "$H")):
            with self.subTest(name):
                peer = LateJog()
                self.addCleanup(peer.close)
                with contextlib.redirect_stdout(io.StringIO()):
                    sender = ct.Sender(peer.port)
                self.addCleanup(sender.close)
                sender.state()                              # where the head stands
                between(sender, peer)
                self.assertEqual(sender.send(line, timeout=TIMEOUT_S), "ok")
                sender.wait_state("Idle", timeout=TIMEOUT_S)
                self.assertGreater(peer.polls_after_jog, LateJog.LATE + 2, "the wait ended before the jog ran")


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
