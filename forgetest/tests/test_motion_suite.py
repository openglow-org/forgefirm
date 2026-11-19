# Copyright 2026 514 LLC d/b/a OpenGlow
# Written by Scott Wiederhold
# https://community.openglow.org
# SPDX-License-Identifier:    MIT

"""The motion suite's own judgments on the host. motion.deadman's stall
drills hang the controller with SIGSTOP: the hang ends on every way out of
the drill, a failed check or an Abort among them, and only for the
controller, so a pid that has since become another process is never sent
SIGCONT (real child processes; Linux only). The wait for a move counts an
Idle only once the move was seen started. A controller's kill is judged
safed when it landed on a running kernel, the run ended inside a second,
and the latch never read unlocked; the reap's time comes off the
supervisor's own line. The kill during $H runs
whatever cloud mode the machine was found in, and puts it back. A check
that a line was not logged fails on a window it cannot read."""
import os
import shutil
import signal
import struct
import subprocess
import sys
import tempfile
import time
import unittest
from unittest import mock

import helpers
from forgetest import hw
from forgetest.runner import Aborted, Failed
from forgetest.suite import motion


class Ctx:
    def __init__(self):
        self.lines = []

    def log(self, msg, *args):
        self.lines.append(msg % args if args else msg)


def stopped(pid):
    """True while the process is stopped (state T in /proc/<pid>/stat)."""
    with open("/proc/%d/stat" % pid) as f:
        return f.read().rsplit(")", 1)[1].split()[0] in ("T", "t")


@unittest.skipUnless(sys.platform.startswith("linux"), "reads /proc")
class StoppedControllerTests(unittest.TestCase):
    def child(self, name):
        """A sleeping child whose comm is `name` (a copy of sleep under it)."""
        tmp = tempfile.mkdtemp(prefix="forgetest-stop-")
        self.addCleanup(shutil.rmtree, tmp, ignore_errors=True)
        exe = os.path.join(tmp, name)
        shutil.copy(shutil.which("sleep"), exe)
        p = subprocess.Popen([exe, "30"])

        def end():
            p.kill()
            p.wait()
        self.addCleanup(end)
        return p.pid

    def wait_state(self, pid, want):
        deadline = time.time() + 5
        while stopped(pid) != want and time.time() < deadline:
            time.sleep(0.01)
        return stopped(pid)

    def test_the_hang_ends_with_the_drill_and_only_for_the_controller(self):
        pid = self.child("grblHAL_glowforge")               # its comm: the 15 characters the kernel keeps
        with self.assertRaises(Aborted):
            with motion._stopped(Ctx(), pid):
                self.assertTrue(self.wait_state(pid, True))
                raise Aborted("aborted")
        self.assertFalse(self.wait_state(pid, False))
        other = self.child("sleep")                         # the pid is no longer the controller's
        with motion._stopped(Ctx(), other):
            self.assertTrue(self.wait_state(other, True))
        time.sleep(0.05)
        self.assertTrue(stopped(other))
        os.kill(other, signal.SIGCONT)


class ScriptedPort:
    """The Grbl port's '?' answers, one state per report from a script (the
    last one repeats), with the kernel's program total moved by a hook."""

    def __init__(self, states, on_report=None):
        self.states = list(states)
        self.on_report = on_report or (lambda n: None)
        self.n = 0

    def status_report(self):
        state = self.states[min(self.n, len(self.states) - 1)]
        self.on_report(self.n)
        self.n += 1
        return {"state": state, "FS": "0,0"}


class SysfsTree:
    """A scratch sysfs tree the suite reads through GF_SYSFS_ROOT."""

    def __init__(self, case):
        self.root = tempfile.mkdtemp(prefix="forgetest-motion-") + os.sep
        os.makedirs(self.root + "cnc")
        case.addCleanup(shutil.rmtree, self.root, ignore_errors=True)
        saved = os.environ.get("GF_SYSFS_ROOT")
        os.environ["GF_SYSFS_ROOT"] = self.root
        case.addCleanup(lambda: os.environ.__setitem__("GF_SYSFS_ROOT", saved) if saved
                        else os.environ.pop("GF_SYSFS_ROOT", None))
        self.position(0, 0)

    def write(self, attr, value):
        with open(self.root + attr, "w") as f:
            f.write(value)

    def position(self, played, handed):
        with open(self.root + "cnc/position", "wb") as f:
            f.write(struct.pack("<3i2I", 0, 0, 0, played, handed) + b"\0" * 12)


class WaitIdleTests(unittest.TestCase):
    """An Idle report counts only once the move just commanded was seen
    started, on a clock that moves only when the wait sleeps."""

    def setUp(self):
        self.tree = SysfsTree(self)
        clock = helpers.Clock()
        p = mock.patch.object(motion, "time", clock)
        p.start()
        self.addCleanup(p.stop)

    def test_an_idle_before_the_move_starts_is_not_its_end(self):
        ctx = helpers.JudgeCtx()
        # Jog reported a second in (twenty reports at the 0.05 s poll), well
        # past any start the wait could miss: the wait sees it through
        _, states, _ = motion.wait_idle(ctx, ScriptedPort(["Idle"] * 20 + ["Jog"] * 5 + ["Idle"]))
        self.assertEqual(states, ["Idle", "Jog", "Idle"])
        self.assertTrue(motion.ran(states))
        # a move that never shows: not done, NOSTART at the bound
        _, states, _ = motion.wait_idle(ctx, ScriptedPort(["Idle"]))
        self.assertEqual(states, ["Idle", "NOSTART"])
        self.assertFalse(motion.ran(states))
        # a move short enough to fall between two reports still moved the
        # kernel's program total: it ran
        bump = lambda n: self.tree.position(0, 4800) if n == 3 else None     # noqa: E731
        _, states, _ = motion.wait_idle(ctx, ScriptedPort(["Idle"], on_report=bump))
        self.assertTrue(motion.ran(states))

    def test_a_move_handed_whole_before_the_wait_shows_against_the_mark_taken_before_it(self):
        ctx = helpers.JudgeCtx()
        mark = motion._program_total()
        self.tree.position(0, 640)          # a return of a few tenths, all in the kernel already
        _, states, _ = motion.wait_idle(ctx, ScriptedPort(["Idle"]), since=mark)
        self.assertEqual(states, ["Idle"])
        self.assertTrue(motion.ran(states))


class KillSafingTests(unittest.TestCase):
    def test_a_kill_is_safed_when_the_run_it_landed_on_ends_with_the_latch_locked(self):
        safed = {"left_running_at_s": 0.29, "states": ["running", "idle"], "state_at_end": "idle",
                 "latch_unlocked_seen": False}
        self.assertIsNone(motion._kill_safed(safed))
        # a reap later than the ring's bytes can cover the stop's ramp: the
        # run ends in an underrun the stop does not clear, recorded, not judged
        late = dict(safed, states=["running", "underrun"], state_at_end="underrun")
        self.assertIsNone(motion._kill_safed(late))
        # a kill on a kernel already idle proves no stop at all
        self.assertIn("did not land mid-move", motion._kill_safed(dict(safed, states=["idle"])))
        self.assertIn("still running", motion._kill_safed(dict(safed, left_running_at_s=None,
                                                                 states=["running"])))
        self.assertIn("latch read unlocked", motion._kill_safed(dict(safed, latch_unlocked_seen=True)))

    def test_the_reap_time_is_read_off_the_supervisors_line(self):
        line = "2026-11-12T21:09:46.751820+00:00 forgectrl[1380] NOTICE super: grbl controller exited"
        self.assertAlmostEqual(motion._line_epoch(line), 1794517786.75182, places=4)
        self.assertIsNone(motion._line_epoch("no stamp here"))

    def test_the_trail_reads_the_kernel_and_the_latch(self):
        tree = SysfsTree(self)
        tree.write("cnc/state", "running")
        tree.write("cnc/interlock_circuit", "45")
        clock = helpers.Clock()
        steps = iter(range(1000))

        def tick():
            if next(steps) == 40:          # 0.2 s in: the stop's ramp is done
                tree.write("cnc/state", "idle")
                tree.position(9000, 12000)
        clock.tick = tick
        with mock.patch.object(motion, "time", clock):
            rec = motion._kill_safing(clock.time())
        self.assertEqual((rec["states"], rec["state_at_end"], rec["unplayed_bytes"]),
                         (["running", "idle"], "idle", 3000))
        self.assertAlmostEqual(rec["left_running_at_s"], 0.205, places=2)
        self.assertFalse(rec["latch_unlocked_seen"])
        tree.write("cnc/interlock_circuit", "37")       # the lock line released
        with mock.patch.object(motion, "time", clock):
            self.assertTrue(motion._kill_safing(clock.time(), seconds=0.02)["latch_unlocked_seen"])
        os.remove(tree.root + "cnc/interlock_circuit")  # unreadable: not shown locked
        with mock.patch.object(motion, "time", clock):
            self.assertTrue(motion._kill_safing(clock.time(), seconds=0.02)["latch_unlocked_seen"])

    def test_a_run_that_ends_in_underrun_left_running_and_a_brief_unlock_is_seen(self):
        tree = SysfsTree(self)
        tree.write("cnc/state", "running")
        tree.write("cnc/interlock_circuit", "45")
        clock = helpers.Clock()
        steps = iter(range(1000))

        def tick():
            n = next(steps)
            if n == 38:                    # 0.19 s in: the ring ran dry before the stop's ramp ended
                tree.write("cnc/state", "underrun")
            elif n == 100:                 # half a second in, for 50 ms: the lock line released
                tree.write("cnc/interlock_circuit", "37")
            elif n == 110:
                tree.write("cnc/interlock_circuit", "45")
        clock.tick = tick
        with mock.patch.object(motion, "time", clock):
            rec = motion._kill_safing(clock.time())
        self.assertAlmostEqual(rec["left_running_at_s"], 0.195, places=3)
        self.assertEqual((rec["states"], rec["state_at_end"]), (["running", "underrun"], "underrun"))
        self.assertTrue(rec["latch_unlocked_seen"])
        self.assertIn("latch read unlocked", motion._kill_safed(rec))
        # the same run with the latch locked throughout is safed
        rec["latch_unlocked_seen"] = False
        self.assertIsNone(motion._kill_safed(rec))


class HomingKillTests(unittest.TestCase):
    """The kill during $H needs cloud mode: a machine found with it off gets
    it for the drill, with the phrase, and gets it back after homing_mode."""

    def test_cloud_mode_off_is_turned_on_for_the_drill_and_put_back(self):
        fake = helpers.FakeForgectrl().start()
        self.addCleanup(fake.stop)
        fake.state["settings"].update({"cloud_enabled": "", "homing_mode": "manual"})
        ran = []
        ctx = helpers.JudgeCtx(forgectrl=hw.Forgectrl())
        with mock.patch.object(motion, "_homing_kill_drill", lambda ctx, wr: ran.append(True)):
            motion.homing_kill(ctx, lambda timeout=30, not_pid=None: None)
        self.assertEqual(ran, [True])
        writes = [(p, f) for p, f in fake.posts if p == "/settings"]
        self.assertEqual(writes, [("/settings", {"cloud_enabled": "1", "phrase": "I UNDERSTAND"}),
                                  ("/settings", {"homing_mode": "gfcloud"}),
                                  ("/settings", {"homing_mode": "manual"}),
                                  ("/settings", {"cloud_enabled": ""})])
        # a drill that fails puts both back all the same
        fake.posts.clear()
        fake.state["settings"].update({"cloud_enabled": "", "homing_mode": "manual"})

        def fails(ctx, wr):
            raise Failed("the runner outlived the kill")
        with mock.patch.object(motion, "_homing_kill_drill", fails), self.assertRaises(Failed):
            motion.homing_kill(ctx, lambda timeout=30, not_pid=None: None)
        self.assertEqual([(p, f) for p, f in fake.posts if p == "/settings"][2:],
                         [("/settings", {"homing_mode": "manual"}), ("/settings", {"cloud_enabled": ""})])


class LogAbsenceTests(unittest.TestCase):
    def test_an_absence_on_a_window_that_cannot_be_read_fails(self):
        tmp = tempfile.mkdtemp(prefix="forgetest-abs-")
        self.addCleanup(shutil.rmtree, tmp, ignore_errors=True)
        log = os.path.join(tmp, "grblhal.log")
        with open(log, "w") as f:
            f.write("a line\n")
        ctx = helpers.JudgeCtx()
        self.assertEqual(motion._log_absent(ctx, log, 0, "late events clamped", "no clamp"), [])
        os.remove(log)                                  # rotated away, nothing in its place
        with self.assertRaises(Failed):
            motion._log_absent(ctx, log, 0, "late events clamped", "no clamp")


if __name__ == "__main__":
    unittest.main()
