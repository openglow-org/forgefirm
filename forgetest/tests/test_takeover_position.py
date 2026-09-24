# Copyright 2026 514 LLC d/b/a OpenGlow
# Written by Scott Wiederhold
# https://community.openglow.org
# SPDX-License-Identifier:    MIT

"""A takeover judges the head where the baseline cannot. forgectrl's start
at the end of a takeover is a controller start, which re-zeroes the step
counters wherever the head stands, so after it a head left out reads as
home. The takeover reads the counters as the controller goes away (against
where the run expects the head) and before it comes back (against where the
takeover found it); a miss is a leftover that fails the run, and the head is
never moved on it. Runs against a fake sysfs tree and a fake anchor file."""
import os
import shutil
import struct
import tempfile
import unittest

from forgetest import baseline, hw, runner


class FakeRun:
    def __init__(self, cap):
        self.baseline_captured = cap
        self.lines = []

    def log(self, s):
        self.lines.append(s)


class TakeoverPositionTests(unittest.TestCase):
    def setUp(self):
        # the clean machine test_baseline.py builds, so the post pass
        # finds nothing but what a test here leaves
        self.tmp = tempfile.mkdtemp(prefix="forgetest-tko-")
        self.sysfs = os.path.join(self.tmp, "sysfs") + os.sep
        self.leds = os.path.join(self.tmp, "leds") + os.sep
        for group in ("cnc", "pic", "head", "thermal"):
            os.makedirs(self.sysfs + group)
        for name in baseline.BUTTON_LEDS + ("lid_led",):
            os.makedirs(self.leds + name)
            with open(self.leds + name + "/brightness", "w") as f:
                f.write("0")
        for attr, val in baseline.fixed_sysfs() + baseline.IDLE_READBACKS:
            self._attr(attr, val)
        self._attr("cnc/interlock_circuit", "45")
        self._attr("pic/lid_led", "0")
        self._pos(0, 0, 0)
        os.environ["GF_SYSFS_ROOT"] = self.sysfs
        os.environ["GF_LEDS_ROOT"] = self.leds
        os.environ["FORGECTRL_URL"] = "http://127.0.0.1:1"      # nothing listens
        baseline.Baseline._unreachable_until = 0.0
        self.anchor = os.path.join(self.tmp, "grblhal.homed")
        self._new_anchor(1)
        self.old_anchor, baseline.ANCHOR_PATH = baseline.ANCHOR_PATH, self.anchor
        self.spm = baseline.counter_steps_per_mm()

    def tearDown(self):
        baseline.ANCHOR_PATH = self.old_anchor
        shutil.rmtree(self.tmp, ignore_errors=True)
        for k in ("GF_SYSFS_ROOT", "GF_LEDS_ROOT", "FORGECTRL_URL"):
            os.environ.pop(k, None)

    def _attr(self, attr, val):
        with open(self.sysfs + attr, "w") as f:
            f.write(str(val))

    def _pos(self, x, y, z):
        with open(self.sysfs + "cnc/position", "wb") as f:
            f.write(struct.pack("<5i", x, y, z, 0, 0))

    def _new_anchor(self, ns):
        """A controller start or a home: a new anchor file."""
        with open(self.anchor + ".new", "w") as f:
            f.write("0 0 0 4 startup")
        os.replace(self.anchor + ".new", self.anchor)
        os.utime(self.anchor, ns=(ns, ns))

    def cap(self):
        return {"position": baseline.read_position(), "frame": baseline.counter_frame()}

    def takeover(self, cap):
        run = FakeRun(cap)
        return runner.Takeover(run.log, "test.id", run=run), run

    def mm(self, mm):
        return round(mm * self.spm)

    def start(self, t):
        """Takeover.__enter__'s order: the frame is read while the
        controller runs, then its exit takes the anchor with it (forgectrl
        unlinks it) and leaves the counters, which are then read."""
        t.frame = baseline.counter_frame()
        if os.path.exists(self.anchor):
            os.unlink(self.anchor)
        t.position_at_start()

    def test_a_head_where_the_run_expects_it_is_not_recorded(self):
        cap = self.cap()
        t, run = self.takeover(cap)
        self.start(t)
        t.position_at_end()
        self.assertNotIn("restart_positions", cap)

    def test_a_head_left_out_at_the_start_is_recorded(self):
        cap = self.cap()
        self._pos(self.mm(10), self.mm(10), 0)
        t, run = self.takeover(cap)
        self.start(t)
        self.assertEqual(cap["restart_positions"],
                         [{"where": "takeover start", "found": [self.mm(10), self.mm(10), 0],
                           "expected": [0, 0, 0]}])

    def test_the_frame_is_read_before_the_controller_goes(self):
        # found on the bench: forgectrl unlinks the anchor when the
        # controller exits, so a frame read after the stop never matched
        # and the check judged nothing. Read before the stop, it does.
        cap = self.cap()
        self._pos(self.mm(10), self.mm(10), 0)
        late, run = self.takeover(cap)
        os.unlink(self.anchor)
        late.frame = baseline.counter_frame()
        late.position_at_start()
        self.assertNotIn("restart_positions", cap)
        self._new_anchor(1)
        cap = self.cap()                        # a run that began at the origin in this frame
        cap["position"] = [0, 0, 0]
        on_time, run = self.takeover(cap)
        self.start(on_time)
        self.assertEqual([r["where"] for r in cap["restart_positions"]], ["takeover start"])

    def test_the_dead_band_is_not_a_leftover(self):
        cap = self.cap()
        self._pos(self.mm(0.05), 0, 0)
        t, run = self.takeover(cap)
        self.start(t)
        self.assertNotIn("restart_positions", cap)

    def test_an_undeclared_frame_change_is_not_judged(self):
        cap = self.cap()
        self._new_anchor(2)                     # a controller start the test did not declare
        self._pos(self.mm(10), 0, 0)
        t, run = self.takeover(cap)
        self.start(t)
        self.assertNotIn("restart_positions", cap)
        self.assertTrue(any("not in the frame the run began in" in l for l in run.lines), run.lines)

    def test_cloud_mode_is_not_judged(self):
        cap = self.cap()
        self._pos(self.mm(10), 0, 0)
        t, run = self.takeover(cap)
        t.cloud = True
        t.position_at_start()
        self._pos(self.mm(20), 0, 0)
        t.position_at_end()
        self.assertNotIn("restart_positions", cap)

    def test_a_bench_tool_is_not_judged(self):
        t = runner.Takeover(lambda s: None, "bench:tool")
        self._pos(self.mm(10), 0, 0)
        t.position_at_start()
        t.position_at_end()                     # no run: nothing to judge against, nothing raised

    def test_a_drill_that_leaves_the_head_out_is_recorded_at_the_end(self):
        cap = self.cap()
        t, run = self.takeover(cap)
        self.start(t)
        self._pos(self.mm(5), 0, 0)             # the drill moved the head and did not bring it back
        t.position_at_end()
        self.assertEqual([r["where"] for r in cap["restart_positions"]], ["takeover end"])
        self.assertEqual(cap["restart_positions"][0]["expected"], [0, 0, 0])

    def test_a_changed_microstep_mode_is_not_judged_at_the_end(self):
        cap = self.cap()
        t, run = self.takeover(cap)
        self.start(t)
        self._attr("cnc/x_mode", "8" if hw.sysfs_read("cnc/x_mode") != "8" else "16")
        self._pos(self.mm(5), 0, 0)
        t.position_at_end()
        self.assertNotIn("restart_positions", cap)

    def test_the_post_pass_fails_the_run_and_moves_nothing(self):
        # setup.check-envelope's case: a declared camera home, the head
        # left out, the record put back under a takeover, whose restart
        # zeroes the counters where the head stands
        cap = self.cap()
        self._new_anchor(3)                     # the camera home
        cap["position"], cap["rezero_declared"], cap["frame"] = [0, 0, 0], True, baseline.counter_frame()
        self._pos(self.mm(10), self.mm(10), 0)
        t, run = self.takeover(cap)
        self.start(t)
        t.position_at_end()
        self._new_anchor(4)                     # forgectrl's start: a controller start
        self._pos(0, 0, 0)
        lines = []
        left = baseline.Baseline(lines.append).enforce("post", captured=cap)
        self.assertEqual([x.item for x in left], ["position at the takeover start"])
        self.assertTrue(left[0].action.startswith("unrestorable"), left[0].action)
        self.assertIn("not moved", left[0].action)
        self.assertEqual(baseline.read_position(), [0, 0, 0])
        # without the takeover's record the same run reads clean: the gap
        del cap["restart_positions"]
        self.assertEqual(baseline.Baseline(lines.append).enforce("post", captured=cap), [])

    def test_a_failed_reading_never_keeps_forgectrl_down(self):
        # the exit path restores the attributes, relocks the latch and
        # starts forgectrl whatever the position reading does
        os.environ["FORGETEST_MARKER"] = os.path.join(self.tmp, "marker")
        calls = []
        real = (hw.initd, baseline.read_position, runner.Takeover.wait_settled)
        hw.initd = lambda service, action, timeout=60: (calls.append((service, action)) or (0, ""))

        def broken():
            raise OSError("unreadable")
        baseline.read_position = broken
        runner.Takeover.wait_settled = lambda self: None
        try:
            cap = {"position": [0, 0, 0], "frame": baseline.counter_frame()}
            t, run = self.takeover(cap)
            t.found = ([0, 0, 0], hw.sysfs_read("cnc/x_mode"))
            t.saved = {"cnc/x_mode": hw.sysfs_read("cnc/x_mode")}
            t.__exit__(None, None, None)
        finally:
            hw.initd, baseline.read_position, runner.Takeover.wait_settled = real
            os.environ.pop("FORGETEST_MARKER", None)
        self.assertEqual(calls, [("forgectrl", "start")])
        with open(self.sysfs + "cnc/laser_latch") as f:
            self.assertEqual(f.read().strip(), "1")
        self.assertTrue(any("could not be judged" in l for l in run.lines), run.lines)

    def test_a_declared_rezero_records_the_frame_in_force(self):
        cap = self.cap()
        self._new_anchor(5)                     # the camera home
        stub = type("Ctx", (), {})()
        stub.run = FakeRun(cap)
        stub.log = stub.run.log
        runner.Context.counters_rezeroed(stub)
        self.assertEqual(cap["position"], [0, 0, 0])
        self.assertTrue(cap["rezero_declared"])
        self.assertEqual(cap["frame"], baseline.counter_frame())


if __name__ == "__main__":
    unittest.main()
