# Copyright 2026 514 LLC d/b/a OpenGlow
# Written by Scott Wiederhold
# https://community.openglow.org
# SPDX-License-Identifier:    MIT

"""A test that lets the service move the head hands it back where it found
it. Every service motion zeroes the step counters at its start, and a home
and every controller start zero them again, so no counter reading says
where the test found the head: the client's own record does. Every motion
logs the counters it ended on, and the travel is their sum (x8 steps, the
one mode a service motion runs at), plus what the counters read since. The
log lines are the machine's own, trimmed to the motion records: a homing
session whose one motion the cooling engine stopped short, and a whole
three-motion one. Runs against scratch logs and fakes for the controller."""
import os
import shutil
import sys
import tempfile
import unittest

from forgetest.runner import Failed
from forgetest.suite import cloud, homeoff

STOPPED = """\
2026-09-25T15:56:19.641824+00:00 gfhome[7387] INFO gfhome:home service action: motion (ready)
2026-09-25T15:56:19.644041+00:00 gfhome[7387] INFO machine:_motion start motion
2026-09-25T15:56:19.970342+00:00 gfhome[7387] INFO machine:_run_loop starting run
2026-09-25T15:56:20.905040+00:00 gfhome[7387] ERR machine:_run_loop run stopped short: 8130 of 21756 bytes played; the job did not finish
2026-09-25T15:56:20.918652+00:00 gfhome[7387] INFO machine:_feed_and_run end positions (actual/expected): X (4390/12115), Y (2682/7402), Z (0/0)
2026-09-25T15:56:20.937235+00:00 gfhome[7387] INFO machine:_motion_locked end positions (4390, 2682, 0)
2026-09-25T15:56:20.938026+00:00 gfhome[7387] INFO machine:_motion end motion
2026-09-25T15:56:20.938677+00:00 gfhome[7387] INFO basemachine:_finish_action motion [1588842275]: finished with event ":cancelled"
2026-09-25T15:56:21.145580+00:00 gfhome[7387] INFO gfhome:home motion completed
"""

WHOLE = "".join(
    "2026-09-07T20:%s gfhome[28592] INFO machine:_motion start motion\n"
    "2026-09-07T20:%s gfhome[28592] INFO machine:_feed_and_run end positions (actual/expected): "
    "X (%d/%d), Y (%d/%d), Z (0/0)\n"
    "2026-09-07T20:%s gfhome[28592] INFO machine:_motion_locked end positions (%d, %d, 0)\n"
    "2026-09-07T20:%s gfhome[28592] INFO machine:_motion end motion\n"
    "2026-09-07T20:%s gfhome[28592] INFO basemachine:_finish_action motion [%d]: finished with event "
    "\":completed\"\n" % (t, t, x, x, y, y, t, x, y, t, t, i)
    for i, (t, x, y) in enumerate((("25:50.1", 4774, 4600), ("25:56.2", -71, -144), ("26:02.9", -13096, -7399))))


class FakeGrbl:
    def __init__(self, ctx):
        self.ctx = ctx

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def command(self, line, timeout=None):
        self.ctx.jogs.append(line)
        return ["ok"]


class FakeCtx:
    def __init__(self):
        self.forgectrl = object()
        self.jogs = []
        self.lines = []
        self.rezeroed = 0

    def check(self, cond, msg, *args):
        if not cond:
            raise Failed(msg % args if args else msg)

    def log(self, msg, *args):
        self.lines.append(msg % args if args else msg)

    def grbl(self):
        return FakeGrbl(self)

    def counters_rezeroed(self):
        self.rezeroed += 1


class HandBackTests(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.log = os.path.join(self.dir, "gfhome.log")
        self.cloudlog = os.path.join(self.dir, "gfcloud.log")
        self.ctx = FakeCtx()
        self.anchor = True
        self.counters = [0, 0, 0]
        self.restarts = 0
        self.spm = 213.333                  # the controller's x32 scale
        saved_logs = (cloud.GFHOME_LOG, cloud.GFCLOUD_LOG)
        self.addCleanup(lambda: setattr(cloud, "GFHOME_LOG", saved_logs[0]))
        self.addCleanup(lambda: setattr(cloud, "GFCLOUD_LOG", saved_logs[1]))
        cloud.GFHOME_LOG, cloud.GFCLOUD_LOG = self.log, self.cloudlog
        patches = {
            "counter_frame": lambda: [1, 2] if self.anchor else None,
            "read_position": lambda: list(self.counters),
            "counter_steps_per_mm": lambda: self.spm,
            "_drop_reference": self._restart,
            "clean_slate": lambda ctx, g: None,
            "wait_idle": self._jog_ends,
            "machine_idle": lambda ctx: None,
        }
        saved = {k: getattr(homeoff, k) for k in patches}
        self.addCleanup(lambda: [setattr(homeoff, k, v) for k, v in saved.items()])
        for k, v in patches.items():
            setattr(homeoff, k, v)
        self.addCleanup(shutil.rmtree, self.dir, True)

    def _restart(self, ctx, fc):
        # a controller start zeroes the counters where the head stands and
        # removes the anchor
        self.restarts += 1
        self.counters = [0, 0, 0]
        self.anchor = False

    def _jog_ends(self, ctx, g, timeout=30.0):
        # the jog played: the counters moved by what it asked, at the scale
        words = dict((w[0], float(w[1:])) for w in self.ctx.jogs[-1].split()[2:4])
        self.counters = [self.counters[0] + round(words["X"] * self.spm),
                         self.counters[1] + round(words["Y"] * self.spm), 0]
        return 0.0, ["Jog", "Idle"], {}

    def write(self, text, path=None):
        with open(path or self.log, "a", encoding="utf-8", newline="\n") as f:
            f.write(text)

    def expect_jog(self, dx, dy):
        self.assertEqual(self.ctx.jogs, ["$J=G91 G21 X%.3f Y%.3f F2400" % (-dx, -dy)])

    # -- the travel ------------------------------------------------------
    def test_the_travel_is_the_sum_of_the_motions_ends(self):
        self.write(WHOLE)
        self.assertEqual(homeoff.session_travel(0), (4774 - 71 - 13096, 4600 - 144 - 7399))

    def test_the_travel_starts_at_the_mark(self):
        self.write(WHOLE)
        mark = homeoff.session_mark()
        self.write(STOPPED)
        self.assertEqual(homeoff.session_travel(mark), (4390, 2682))

    def test_the_cloud_clients_log_is_read_the_same_way(self):
        self.write(WHOLE.replace("gfhome[", "gfcloud["), self.cloudlog)
        self.assertEqual(homeoff.session_travel(0, cloud.GFCLOUD_LOG), (4774 - 71 - 13096, 4600 - 144 - 7399))
        self.assertEqual(homeoff.session_travel(0), (0, 0))          # gfhome's log is its own

    def test_a_motion_with_no_end_leaves_the_travel_unknown(self):
        self.write(STOPPED.split("machine:_motion_locked")[0])
        self.assertIsNone(homeoff.session_travel(0))

    def test_a_log_rotated_under_the_run_leaves_the_travel_unknown(self):
        self.write(STOPPED)
        self.assertIsNone(homeoff.session_travel(os.path.getsize(self.log) + 1))

    # -- the stopped-short judge --------------------------------------------
    def test_a_motion_stopped_short_fails_the_judge(self):
        self.write(STOPPED)
        ev = {}
        with self.assertRaises(Failed):
            homeoff.judge_whole_motions(self.ctx, ev, 0)
        self.assertEqual(len(ev["short_motions"]), 2)

    def test_whole_motions_pass_the_judge(self):
        self.write(WHOLE)
        ev = {}
        homeoff.judge_whole_motions(self.ctx, ev, 0)
        self.assertEqual(ev["short_motions"], [])

    # -- after a camera home -------------------------------------------------
    def test_homed_the_head_goes_back_by_the_travel_and_the_counters_since(self):
        # the home zeroed the counters; the test's own jogs left a step or so
        self.write(STOPPED)
        self.counters = [round(1.0 * self.spm), 0, 0]
        since = self.counters[0] / self.spm
        homeoff.camera_home_return(self.ctx, {"homed": True}, 0)
        self.expect_jog(4390 / homeoff.XY_STEPS_PER_MM + since, 2682 / homeoff.XY_STEPS_PER_MM)
        # dropped before the jog, and once more with the head back
        self.assertEqual(self.restarts, 2)
        self.assertEqual(self.counters, [0, 0, 0])
        self.assertEqual(self.ctx.rezeroed, 1)

    def test_unhomed_the_counters_are_not_counted_twice(self):
        # a failed session leaves the last motion's end in the counters,
        # which the travel already counts
        self.write(STOPPED)
        self.counters = [4390, 2682, 0]
        self.anchor = False
        homeoff.camera_home_return(self.ctx, {"homed": False}, 0)
        self.expect_jog(4390 / homeoff.XY_STEPS_PER_MM, 2682 / homeoff.XY_STEPS_PER_MM)

    def test_an_unknown_travel_moves_nothing(self):
        self.write(STOPPED.split("machine:_motion_locked")[0])
        with self.assertRaises(Failed):
            homeoff.camera_home_return(self.ctx, {"homed": True}, 0)
        self.assertEqual(self.ctx.jogs, [])
        self.assertEqual(self.ctx.rezeroed, 0)

    def test_a_restart_since_the_home_moves_nothing(self):
        # the anchor gone: the counters no longer read from the home
        self.write(STOPPED)
        self.anchor = False
        with self.assertRaises(Failed):
            homeoff.camera_home_return(self.ctx, {"homed": True}, 0)
        self.assertEqual(self.ctx.jogs, [])

    def test_a_head_that_never_moved_is_not_jogged(self):
        homeoff.camera_home_return(self.ctx, {}, 0)
        self.assertEqual(self.ctx.jogs, [])
        self.assertEqual((self.restarts, self.ctx.rezeroed), (1, 1))

    # -- after cloud mode ----------------------------------------------------
    def test_after_cloud_mode_the_head_goes_back_by_the_clients_record(self):
        # the service's re-hunt, cut short by the switch back: to the middle
        # of the bed and one correction (the bench reference, 2026-09-25)
        self.write(WHOLE[:WHOLE.index("2026-09-07T20:26:02.9")].replace("gfhome[", "gfcloud["), self.cloudlog)
        ev = {}
        homeoff.cloud_mode_return(self.ctx, ev, 0)
        self.expect_jog((4774 - 71) / homeoff.XY_STEPS_PER_MM, (4600 - 144) / homeoff.XY_STEPS_PER_MM)
        self.assertEqual(tuple(ev["cloud_return"]["session_travel_steps"]), (4774 - 71, 4600 - 144))
        # no drop before the jog (the switch back started the controller), one after
        self.assertEqual((self.restarts, self.ctx.rezeroed), (1, 1))

    def test_after_cloud_mode_an_unknown_travel_moves_nothing(self):
        self.write(STOPPED.replace("gfhome[", "gfcloud[").split("machine:_motion_locked")[0], self.cloudlog)
        with self.assertRaises(Failed):
            homeoff.cloud_mode_return(self.ctx, {}, 0)
        self.assertEqual(self.ctx.jogs, [])

    # -- the first failure wins --------------------------------------------
    def test_a_hand_back_that_fails_after_the_test_failed_is_logged_not_raised(self):
        self.write(STOPPED.split("machine:_motion_locked")[0])
        ev = {"homed": True}
        with self.assertRaises(Failed) as cm:
            try:
                raise Failed("the test's own failure")
            finally:
                homeoff.camera_home_return(self.ctx, ev, 0)
        self.assertEqual(str(cm.exception), "the test's own failure")
        self.assertIn("cannot be known", ev["return"]["failed"])
        self.assertTrue(any("the head is not back" in l for l in self.ctx.lines), self.ctx.lines)
        self.assertEqual(self.ctx.jogs, [])
        self.assertIsNone(sys.exc_info()[1])


if __name__ == "__main__":
    unittest.main()
