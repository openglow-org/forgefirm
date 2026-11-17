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
never moved on it. Runs against a fake sysfs tree and a fake anchor file.

The same fake machine carries the rest of the hand-back's verdict: what a
takeover's end hands back (an underrun acknowledged, a program still
playing stopped, a forgectrl that did not start, a pulse-device holder that
outlived the stop, a takeover a dead forgetest left), and how the runner
turns a pass into a result (a leftover after a passing body, a hand-back
that raises, the laser's safety state found off before a run, a leftover
charged to the run before, a state the pre pass could not capture or judge,
an Abort inside that pass, a result the log cannot take, a run the log saw
start and never end)."""
import http.client
import json
import os
import shutil
import struct
import tempfile
import time
import types
import unittest
from unittest import mock

import helpers
from forgetest import artifact, baseline, hw, runner
from forgetest import bench as bench_mod
from forgetest import fixture as fx_mod
from forgetest.log import Log


class Death(BaseException):
    """forgetest's process ending at the point it is raised: nothing the
    runner catches."""


class FakeRun:
    def __init__(self, cap):
        self.baseline_captured = cap
        self.lines = []

    def log(self, s):
        self.lines.append(s)


class MachineFixture:
    """A clean fake machine (the sysfs tree test_baseline.py builds) and a
    fake anchor file. No tests of its own."""

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
        os.environ["FORGETEST_HOST_RUN"] = "1"                  # no forgectrl: its side is skipped
        helpers.latch_drives_interlock(self)
        self.anchor = os.path.join(self.tmp, "grblhal.homed")
        self._new_anchor(1)
        self.old_anchor, baseline.ANCHOR_PATH = baseline.ANCHOR_PATH, self.anchor
        self.spm = baseline.counter_steps_per_mm()

    def tearDown(self):
        baseline.ANCHOR_PATH = self.old_anchor
        shutil.rmtree(self.tmp, ignore_errors=True)
        for k in ("GF_SYSFS_ROOT", "GF_LEDS_ROOT", "FORGECTRL_URL", "FORGETEST_HOST_RUN"):
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


class TakeoverPositionTests(MachineFixture, unittest.TestCase):
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

    def test_the_post_pass_fails_the_run_and_moves_nothing(self):
        # a check's case: a declared camera home, the head
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


class TakeoverEndTests(MachineFixture, unittest.TestCase):
    """What a takeover hands back at its end, and what forgetest's start
    hands back for a takeover a dead forgetest never ended. The init script
    records the machine forgectrl's start found."""

    WATCHED = ("cnc/laser_latch", "cnc/stop", "cnc/motor_lock", "cnc/x_mode")

    def setUp(self):
        super().setUp()
        os.environ["FORGETEST_MARKER"] = os.path.join(self.tmp, "marker")
        self.addCleanup(os.environ.pop, "FORGETEST_MARKER", None)
        self.at_start = None
        self.start_rc = 0
        for target, attr, value in ((hw, "initd", self._initd),
                                    (runner.Takeover, "wait_settled", lambda t: {"controller": "running"})):
            p = mock.patch.object(target, attr, value)
            p.start()
            self.addCleanup(p.stop)

    def _initd(self, service, action, timeout=60):
        if action == "start":
            self.at_start = {a: hw.sysfs_read(a) for a in self.WATCHED}
            return self.start_rc, ""
        return 0, ""

    def test_the_lock_comes_before_everything_the_takeover_hands_back(self):
        self._attr("cnc/state", "underrun")
        writes = []
        real = hw.sysfs_write
        with mock.patch.object(hw, "sysfs_write", lambda attr, value: writes.append(attr) or real(attr, value)):
            t, run = self.takeover(self.cap())
            t.saved = {"cnc/motor_lock": "0"}
            t.restore_attrs()
        self.assertEqual(writes, ["cnc/laser_latch", "cnc/stop", "cnc/motor_lock"])

    def test_an_underrun_is_acknowledged_before_forgectrl_starts_and_named(self):
        # forgectrl starts on an idle kernel, and the drill that left the
        # underrun has it as its leftover
        self._attr("cnc/state", "underrun")
        cap = self.cap()
        t, run = self.takeover(cap)
        t.__exit__(None, None, None)
        self.assertEqual((self.at_start["cnc/stop"], self.at_start["cnc/laser_latch"]), ("1", "1"))
        self.assertEqual([r["item"] for r in cap["restart_leftovers"]], ["kernel state at the takeover end"])

    def test_a_program_still_playing_is_stopped_before_the_attributes_go_back(self):
        # a drill that raised inside its run: a controlled stop first, and the
        # attributes only on an idle kernel; a program that plays on keeps them
        real = hw.sysfs_write
        clock = helpers.Clock()
        clocked = types.SimpleNamespace(time=clock.time, sleep=clock.sleep, strftime=time.strftime)
        for stops, motor_lock in ((True, "0"), (False, "15")):
            with self.subTest(stops=stops):
                self._attr("cnc/state", "running")
                self._attr("cnc/motor_lock", "15")

                def write(attr, value):
                    real(attr, value)
                    if attr == "cnc/stop" and stops:
                        real("cnc/state", "idle")
                cap = self.cap()
                with mock.patch.object(hw, "sysfs_write", write), mock.patch.object(runner, "time", clocked):
                    t, run = self.takeover(cap)
                    t.saved = {"cnc/motor_lock": "0"}
                    t.__exit__(None, None, None)
                self.assertEqual((self.at_start["cnc/laser_latch"], self.at_start["cnc/motor_lock"]),
                                 ("1", motor_lock))
                self.assertEqual([(r["item"], r["found"]) for r in cap["restart_leftovers"]],
                                 [("kernel state at the takeover end", "running")])

    def test_a_forgectrl_that_did_not_start_is_the_runs_leftover(self):
        self.start_rc = 1
        cap = self.cap()
        t, run = self.takeover(cap)
        t.__exit__(None, None, None)
        self.assertEqual([r["item"] for r in cap["restart_leftovers"]], ["forgectrl at the takeover end"])

    def test_a_holder_that_outlives_the_stop_fails_the_takeover(self):
        clock = helpers.Clock()
        clocked = types.SimpleNamespace(time=clock.time, sleep=clock.sleep, strftime=time.strftime)
        with mock.patch.object(runner, "time", clocked), \
                mock.patch.object(hw, "pidof", lambda comm: [4242] if comm == "forgectrl" else []), \
                mock.patch.object(hw, "pgrep_f", lambda needle: []):
            t, run = self.takeover(self.cap())
            with self.assertRaises(runner.Failed):
                t.__enter__()
        self.assertIsNotNone(self.at_start, "forgectrl is started again on the way out")

    def marker(self, saved):
        with open(os.environ["FORGETEST_MARKER"], "w") as f:
            json.dump({"ts": "2026-11-17T00:00:00Z", "who": "kernel.k1-k2", "saved": saved}, f)

    def recover(self):
        runner.Runner.recover(types.SimpleNamespace(_note=lambda msg: None))

    def test_a_takeover_a_dead_forgetest_left_is_handed_back_before_forgectrl_starts(self):
        # only a takeover's own attributes are written back: never the latch
        self.marker({"cnc/motor_lock": "0", "cnc/x_mode": "32", "cnc/laser_latch": "0"})
        self._attr("cnc/motor_lock", "15")                  # what the drill left
        self._attr("cnc/x_mode", "8")
        self._attr("cnc/state", "underrun")
        self.recover()
        self.assertEqual(self.at_start, {"cnc/laser_latch": "1", "cnc/stop": "1",
                                         "cnc/motor_lock": "0", "cnc/x_mode": "32"})
        self.assertFalse(os.path.exists(os.environ["FORGETEST_MARKER"]))

    def test_a_recovered_takeover_leaves_the_attributes_of_a_machine_something_still_drives(self):
        # a drill that outlived the forgetest that ran it, a kernel still
        # playing (one a stop would end: recover does not try), a forgectrl
        # already up: the latch is locked, nothing else
        proc = os.path.join(self.tmp, "proc")
        real = hw.sysfs_write

        def write(attr, value):
            real(attr, value)
            if attr == "cnc/stop":
                real("cnc/state", "idle")
        p = mock.patch.object(hw, "sysfs_write", write)
        p.start()
        self.addCleanup(p.stop)
        for case in ("a holder of the pulse device", "the kernel running", "forgectrl up"):
            with self.subTest(case):
                self.marker({"cnc/motor_lock": "0"})
                self._attr("cnc/motor_lock", "15")
                self._attr("cnc/state", "running" if case == "the kernel running" else "idle")
                if os.path.exists(self.sysfs + "cnc/laser_latch"):
                    os.remove(self.sysfs + "cnc/laser_latch")
                shutil.rmtree(proc, ignore_errors=True)
                os.makedirs(os.path.join(proc, "999", "fd"))
                if case == "a holder of the pulse device":
                    os.symlink(runner.PULSE_DEVICE, os.path.join(proc, "999", "fd", "3"))
                os.environ["FORGETEST_PROC_ROOT"] = proc
                self.addCleanup(os.environ.pop, "FORGETEST_PROC_ROOT", None)
                url = os.environ["FORGECTRL_URL"]
                fc = helpers.FakeForgectrl().start() if case == "forgectrl up" else None
                try:
                    self.recover()
                finally:
                    if fc is not None:
                        fc.stop()
                        os.environ["FORGECTRL_URL"] = url
                self.assertEqual((self.at_start["cnc/laser_latch"], self.at_start["cnc/motor_lock"]), ("1", "15"))


class RunVerdictTests(MachineFixture, unittest.TestCase):
    """The runner turns a run and its hand-back into a result, on the fake
    machine (a host run: no forgectrl, so its side is skipped)."""

    def setUp(self):
        super().setUp()
        for k, v in (("FORGETEST_DATA", self.tmp), ("FORGETEST_MARKER", os.path.join(self.tmp, "marker"))):
            os.environ[k] = v
            self.addCleanup(os.environ.pop, k, None)

    def runner_with(self, *tests):
        self.log = Log(os.path.join(self.tmp, "results.jsonl"))
        r = runner.Runner(self.log, helpers.make_manifest(), helpers.registry(*tests))
        r.provenance = helpers.ScriptedProvenance()
        return r

    def finish(self, r, tid):
        ok, msg, run = r._start_test(tid)
        self.assertTrue(ok, msg)
        deadline = time.time() + 60
        while run.finished is None and time.time() < deadline:
            time.sleep(0.02)
        return run.finished

    def last(self, kind="result"):
        return [x for x in self.log.read() if x.get("t") == kind]

    def two(self, a=None, b=None):
        """A runner with v.a and v.b; b's body records that it ran."""
        self.ran = []
        return self.runner_with(helpers.make_test("v.a", [("forgectrl", "src/ui.c")], fn=a),
                                helpers.make_test("v.b", [("forgectrl", "src/auth.c")],
                                                  fn=b or (lambda ctx: self.ran.append("v.b"))))

    def post(self, rec):
        return [x["item"] for x in rec["evidence"]["baseline"]["post"]]

    def test_a_leftover_after_a_passing_body_fails_the_run(self):
        r = self.runner_with(helpers.make_test("v.a", [("forgectrl", "src/ui.c")],
                                               fn=lambda ctx: hw.sysfs_write("cnc/motor_lock", "15")))
        fin = self.finish(r, "v.a")
        self.assertEqual((fin["result"], self.post(self.last()[-1])), ("FAIL", ["cnc/motor_lock"]))

    def test_a_hand_back_that_raises_is_not_a_pass(self):
        post = []
        real = baseline.Baseline._ext_side

        def ext(bl, left):
            if post:
                raise RuntimeError("an answer of a shape nobody expected")
            return real(bl, left)
        with mock.patch.object(baseline.Baseline, "_ext_side", ext):
            r = self.runner_with(helpers.make_test("v.a", [("forgectrl", "src/ui.c")], fn=lambda ctx: post.append(1)))
            fin = self.finish(r, "v.a")
        rec = self.last()[-1]
        self.assertEqual((fin["result"], rec["needs_disposition"]), ("ERROR", True))
        self.assertNotIn("post", rec["evidence"]["baseline"])           # the pass did not finish

    def test_the_laser_found_unlocked_before_a_run_stops_it_and_charges_the_run_before(self):
        r = self.two()
        self.assertEqual(self.finish(r, "v.a")["result"], "PASS")
        self._attr("cnc/interlock_circuit", "37")           # unlocked between the runs (bit 3 clear)
        fin = self.finish(r, "v.b")
        rec = self.last()[-1]
        self.assertEqual((fin["result"], rec.get("not_run"), self.ran), ("ERROR", True, []))
        self.assertEqual(rec["evidence"]["baseline"]["charged"], "v.a")
        self.assertNotIn("needs_disposition", rec)          # the run judged nothing
        self.assertEqual(hw.sysfs_read("cnc/laser_latch"), "1")
        self.assertEqual([(x["test"], x["needs_disposition"], x["safety"]) for x in self.last("leftover")],
                         [("v.a", True, True)])
        self.assertEqual(r.state()[0]["tests"]["v.a"]["reason"], "disposition")

    def test_laser_on_found_before_a_run_stops_it(self):
        r = self.two()
        for attr in baseline.LASER_READBACKS:                   # the FIRE line, then LASER_ON
            with self.subTest(attr):
                self.assertEqual(self.finish(r, "v.a")["result"], "PASS")
                self._attr(attr, "1")
                fin = self.finish(r, "v.b")
                self._attr(attr, "0")
                self.assertEqual((fin["result"], self.last()[-1].get("not_run"), self.ran), ("ERROR", True, []))

    def test_a_leftover_found_before_a_run_is_charged_to_the_run_before(self):
        r = self.two()
        self.assertEqual(self.finish(r, "v.a")["result"], "PASS")
        self._attr("cnc/motor_lock", "15")                  # left behind after v.a's own post pass
        self.assertEqual(self.finish(r, "v.b")["result"], "PASS")
        charged = self.last("leftover")
        self.assertEqual([(x["test"], x.get("safety", False)) for x in charged], [("v.a", False)])
        self.assertEqual([x["item"] for x in charged[0]["leftovers"]], ["cnc/motor_lock"])
        st = r.state()[0]["tests"]
        self.assertEqual((st["v.a"]["satisfied"], st["v.a"]["reason"], st["v.b"]["satisfied"]),
                         (False, "disposition", True))

    def charged_after(self, between, a=None):
        """v.a runs, then `between(r)`, then motor_lock is left at 15 and v.b
        runs: the tests the leftover was charged to."""
        r = self.two(a=a)
        self.finish(r, "v.a")
        between(r)
        self._attr("cnc/motor_lock", "15")
        self.assertEqual(self.finish(r, "v.b")["result"], "PASS")
        return [x["test"] for x in self.last("leftover")]

    def test_nothing_is_charged_across_a_boot(self):
        def reboot(r):
            p = mock.patch.object(runner, "boot_id", lambda: "a later boot")
            p.start()
            self.addCleanup(p.stop)
        self.assertEqual(self.charged_after(reboot), [])

    def test_nothing_is_charged_outside_the_open_campaign(self):
        self.assertEqual(self.charged_after(lambda r: r.reset("bench work")), [])

    def bench(self, r, script="pass\n", safety="dry"):
        """A real bench tool's run (a script that does nothing, by default) on r."""
        tools = os.path.join(self.tmp, "tools")
        os.makedirs(tools, exist_ok=True)
        with open(os.path.join(tools, "noop.py"), "w") as f:
            f.write(script)
        r.bench = bench_mod.Bench([{"id": "noop", "title": "N", "script": "noop.py", "safety": safety,
                                    "where": "board", "ported": True, "desc": "", "args": []}],
                                  tool_dir=tools, index_path=os.path.join(self.tmp, "bench.jsonl"))
        self.assertTrue(r.start_bench("noop")[0])
        deadline = time.time() + 60
        while r.busy() and time.time() < deadline:
            time.sleep(0.02)

    def test_nothing_is_charged_when_a_bench_tool_ran_since(self):
        # read from the log by a forgetest started after the tool: its own
        # post pass judged what it left
        def bench(r):
            self.bench(r)
            r.last = r.current = None                       # a restart: nothing in memory
        self.assertEqual(self.charged_after(bench), [])

    def test_a_bench_tools_pass_before_it_charges_the_test_before(self):
        r = self.two()
        self.finish(r, "v.a")
        self._attr("cnc/motor_lock", "15")
        self.bench(r)
        self.assertEqual([x["test"] for x in self.last("leftover")], ["v.a"])

    def test_only_a_pass_is_charged(self):
        # the newest result decides: a PASS of v.a, then a run of v.a that
        # was aborted (its post pass judged what it left), then the leftover
        aborting = []

        def a(ctx):
            if aborting:
                ctx.run.abort()

        def again(r):
            aborting.append(1)
            self.finish(r, "v.a")
        self.assertEqual(self.charged_after(again, a=a), [])

    def test_a_kernel_the_pass_before_a_run_cannot_read_does_not_run(self):
        # nor is what the pass could not read charged to the test before it:
        # it is no state that test left
        os.environ.pop("FORGETEST_HOST_RUN", None)          # a machine: forgectrl answers, the kernel does not
        fc = helpers.FakeForgectrl().start()
        self.addCleanup(fc.stop)
        r = self.two()
        for side in ("cnc/state", "cnc/laser_on", "forgectrl"):
            with self.subTest(unreadable=side):
                self.assertEqual(self.finish(r, "v.a")["result"], "PASS")
                if side == "forgectrl":
                    fc.stop()                               # silent from here on: one try, no wait
                    os.environ["FORGECTRL_URL"] = fc.url
                    with mock.patch.object(baseline, "FC_WAIT_S", 0):
                        fin = self.finish(r, "v.b")
                else:
                    os.rename(self.sysfs + side, self.sysfs + side + ".gone")
                    fin = self.finish(r, "v.b")
                    os.rename(self.sysfs + side + ".gone", self.sysfs + side)
                self.assertEqual((fin["result"], self.last()[-1].get("not_run"), self.ran), ("ERROR", True, []))
                self.assertEqual(self.last("leftover"), [])

    def test_a_state_the_pre_pass_could_not_capture_does_not_run(self):
        os.environ.pop("FORGETEST_HOST_RUN", None)          # a machine: forgectrl answers, but not all of it
        fc = helpers.FakeForgectrl().start()
        self.addCleanup(fc.stop)
        capturing = []                                      # the pass reads the settings fine; the capture does not
        real = baseline.Baseline.capture

        def capture(bl):
            capturing.append(1)
            return real(bl)
        fc.on_get = lambda path, q: (500, {"error": "busy"}) if path == "/settings" and capturing else None
        r = self.two()
        with mock.patch.object(baseline.Baseline, "capture", capture):
            fin = self.finish(r, "v.b")
        self.assertEqual((fin["result"], self.last()[-1].get("not_run"), self.ran), ("ERROR", True, []))

    def aborted_in_the_pass(self, r):
        """v.b with the operator's Abort landing inside its pre pass."""
        real = baseline.Baseline._ext_side
        aborted = []

        def ext(bl, left):
            if not aborted:
                aborted.append(1)
                r.current.abort()
            return real(bl, left)
        with mock.patch.object(baseline.Baseline, "_ext_side", ext):
            return self.finish(r, "v.b")

    def test_an_abort_during_the_pass_before_a_run_charges_nothing_but_the_safety_rule(self):
        # a fixed value the supervisor's own start can still be writing: what
        # the Abort cut short decides it, and it is dropped
        r = self.two()
        self.finish(r, "v.a")
        self._attr("cnc/motor_lock", "15")
        fin = self.aborted_in_the_pass(r)
        self.assertEqual((fin["result"], self.last("leftover"), self.ran), ("ABORTED", [], []))

    # -- the safety rule ---------------------------------------------------
    def fresh(self, *tests):
        """A runner on an empty log (one input path per subtest)."""
        path = os.path.join(self.tmp, "results.jsonl")
        if os.path.exists(path):
            os.remove(path)
        self._attr("cnc/interlock_circuit", "45")
        return self.two() if not tests else self.runner_with(*tests)

    def charged_safety(self):
        return [(x["test"], x["safety"]) for x in self.last("leftover")]

    def test_a_safety_finding_before_a_run_is_charged_to_the_newest_run_of_the_boot(self):
        # whatever campaign that run was in, whatever its result, and past a
        # run that started and never ended
        def a_reset(r):
            r.reset("between the runs")

        def an_aborted_run(r):
            self.aborting = True
            self.assertEqual(self.finish(r, "v.a")["result"], "ABORTED")

        def a_death_before_the_pass(r):
            def probe(force=False):
                if force:                                   # the probe right after the start record
                    raise Death()
            with mock.patch.object(r, "probe_fixture", probe):
                with self.assertRaises(Death):
                    r._start_test("v.b")
            self.assertEqual(self.last("start")[-1]["test"], "v.b")
            return self.runner_with(*r.tests())             # the restart

        for between in (a_reset, an_aborted_run, a_death_before_the_pass):
            with self.subTest(between.__name__):
                self.aborting = False
                r = self.fresh(helpers.make_test("v.a", [("forgectrl", "src/ui.c")],
                                                 fn=lambda ctx: self.aborting and ctx.run.abort()),
                               helpers.make_test("v.b", [("forgectrl", "src/auth.c")]))
                self.assertEqual(self.finish(r, "v.a")["result"], "PASS")
                r = between(r) or r
                self._attr("cnc/interlock_circuit", "37")       # unlocked after v.a's pass
                fin = self.finish(r, "v.b")
                self.assertEqual((fin["result"], self.last()[-1].get("not_run")), ("ERROR", True))
                self.assertEqual(self.charged_safety(), [("v.a", True)])
                self.assertEqual(r.state()[0]["tests"]["v.a"]["reason"], "disposition")

    def test_a_safety_finding_is_in_the_log_before_the_pass_waits_on_anything(self):
        # forgetest dies inside v.b's pass, right after the latch was read
        # and locked: the finding is already charged
        r = self.two()
        self.finish(r, "v.a")
        self._attr("cnc/interlock_circuit", "37")
        with mock.patch.object(baseline.Baseline, "await_forgectrl", side_effect=Death), \
                mock.patch.object(runner.threading, "excepthook", lambda args: None):
            self.assertTrue(r._start_test("v.b")[0])
            deadline = time.time() + 60
            while any(th.name == "forgetest-run" for th in runner.threading.enumerate()) and time.time() < deadline:
                time.sleep(0.02)
        self.assertEqual(self.charged_safety(), [("v.a", True)])
        r = self.runner_with(*r.tests())                        # the restart
        self.assertEqual(r.state()[0]["tests"]["v.a"]["reason"], "disposition")

    def test_an_aborted_run_whose_pass_after_it_finds_the_latch_fails(self):
        r = self.runner_with(helpers.make_test("v.a", [("forgectrl", "src/ui.c")],
                                               fn=lambda ctx: (self.unlocks(ctx), ctx.run.abort())))
        fin = self.finish(r, "v.a")
        rec = self.last()[-1]
        self.assertEqual((fin["result"], rec["needs_disposition"]), ("FAIL", True))
        self.assertEqual([(x["test"], x["safety"], "found_after" in x) for x in self.last("leftover")],
                         [("v.a", True, True)])

    def on_a_machine(self, unlocks=True):
        """forgectrl answering (a fake daemon whose controller stop locks the
        latch, as the supervisor's does) on a fake clock for the hand-back,
        and a client that, once self.arming is set, opens a session at the
        cooling engine's next read and, unless `unlocks` is off (its verdict
        wait), unlocks the latch for its press."""
        os.environ.pop("FORGETEST_HOST_RUN", None)
        fc = helpers.FakeForgectrl().start()
        self.addCleanup(fc.stop)
        p = mock.patch.object(baseline, "time", helpers.Clock())
        p.start()
        self.addCleanup(p.stop)
        self.arming = False

        def on_get(path, q):
            if path == "/cool/status" and self.arming:
                if unlocks:
                    self._attr("cnc/interlock_circuit", "37")   # the client's unlock for the press
                return 200, {"phase": "run", "armed": True, "hold": False}
            return None

        def on_post(path, form):
            if path == "/controller/stop":
                self.arming = False                          # the session goes with the controller
                fc.state["mode"] = dict(fc.state["mode"], controller="standby")
            elif path == "/controller/start":
                fc.state["mode"] = dict(fc.state["mode"], controller="running")
            return None
        fc.on_get, fc.on_post = on_get, on_post
        return fc

    def test_a_window_a_client_arms_before_a_run_is_charged_by_the_safety_rule(self):
        # the client v.a left arms after the pass's first look and unlocks;
        # the stand-down that ends it locks the latch, but both the window and
        # the unlock are read first, and charged to v.a, a reset or not
        self.on_a_machine()
        for reset in (True, False):
            with self.subTest(reset=reset):
                r = self.fresh()
                self.assertEqual(self.finish(r, "v.a")["result"], "PASS")
                if reset:
                    r.reset("between the runs")
                self.arming = True
                fin = self.finish(r, "v.b")
                self.assertEqual((fin["result"], self.last()[-1].get("not_run"), self.ran), ("ERROR", True, []))
                charged = self.last("leftover")
                self.assertEqual(set((x["test"], x["safety"]) for x in charged), {("v.a", True)})
                self.assertEqual(set(i["item"] for x in charged for i in x["leftovers"]), {"cool.armed", "laser_latch"})
                self.assertEqual(r.state()[0]["tests"]["v.a"]["reason"], "disposition")

    def test_an_aborted_rerun_whose_client_arms_in_the_pass_after_it_fails(self):
        self.on_a_machine()
        aborting = []
        r = self.two(a=lambda ctx: aborting and (setattr(self, "arming", True), ctx.run.abort()))
        self.assertEqual(self.finish(r, "v.a")["result"], "PASS")
        aborting.append(1)                                  # the rerun's print is still loading at the Abort
        fin = self.finish(r, "v.a")
        self.assertEqual((fin["result"], self.last()[-1]["needs_disposition"]), ("FAIL", True))

    def test_a_bench_tools_pass_after_it_charges_the_newest_run_with_the_lasers_safety_state(self):
        # the client v.a left unlocks during a read-only tool: the tool's own
        # pass after it finds it, and v.a is charged; the export is refused
        r = self.two()
        self.finish(r, "v.a")
        self.bench(r, script="import os\nroot = os.environ['GF_SYSFS_ROOT']\n"
                             "open(root + 'cnc/interlock_circuit', 'w').write('37')\n")
        self.assertEqual([(x["test"], x["safety"], x.get("found_after")) for x in self.last("leftover")],
                         [("v.a", True, "bench noop")])
        self.assertEqual(self.finish(r, "v.b")["result"], "PASS")
        ok, art = r.export()
        self.assertTrue(ok, art)
        self.assertFalse(artifact.verify(art, r.manifest, r.tests(), r.catalog_hash)[0])

    def test_a_safety_record_the_log_refuses_is_carried_on_the_runs_own_result(self):
        # before a run, under an Abort in that pass, and after a run: the
        # leftover record cannot go in, and the result holds the charge
        def before(r):
            self._attr("cnc/interlock_circuit", "37")
            return self.finish(r, "v.b")

        def aborted(r):
            self._attr("cnc/interlock_circuit", "37")
            return self.aborted_in_the_pass(r)

        def after(r):
            self.unlocking = True
            return self.finish(r, "v.a")

        for case, result in ((before, "ERROR"), (aborted, "ABORTED"), (after, "FAIL")):
            with self.subTest(case.__name__):
                self.unlocking = False
                r = self.fresh(helpers.make_test("v.a", [("forgectrl", "src/ui.c")],
                                                 fn=lambda ctx: self.unlocking and (self.unlocks(ctx), ctx.run.abort())),
                               helpers.make_test("v.b", [("forgectrl", "src/auth.c")]))
                self.assertEqual(self.finish(r, "v.a")["result"], "PASS")
                self.refusing(lambda rec: rec.get("t") == "leftover")
                self.assertEqual(case(r)["result"], result)
                self.assertEqual((self.last("leftover"), [c["test"] for c in self.last()[-1]["charges"]]), ([], ["v.a"]))
                self.assertEqual(r.state()[0]["tests"]["v.a"]["reason"], "disposition")
                # where the result stands, the charge stands: a disposition
                # alone does not bring v.a's older PASS back
                self.assertTrue(r.dispose("v.a", "the client was left running")[0])
                self.assertFalse(r.state()[0]["tests"]["v.a"]["satisfied"])

    def test_a_silence_while_waiting_out_a_cooldown_before_a_run_charges_nobody(self):
        # the engine's own cooldown (no hold, no arm), and a read in the wait
        # that answers an error: the machine is stood down, and the silence,
        # which no run left, is charged to nobody
        fc = self.on_a_machine()
        r = self.two()
        self.assertEqual(self.finish(r, "v.a")["result"], "PASS")
        n = []

        def on_get(path, q):
            if path == "/cool/status":
                n.append(path)
                if len(n) <= 2:
                    return 200, {"phase": "cooldown", "armed": False, "hold": False}
                if len(n) == 3:
                    return 503, {"error": "busy"}
            return None
        fc.on_get = on_get
        fin = self.finish(r, "v.b")
        self.assertEqual((fin["result"], self.last()[-1].get("not_run"), self.last("leftover")), ("ERROR", True, []))
        self.assertIn("/controller/stop", [p for p, _ in fc.posts])

    def test_a_supervisor_unanswered_past_a_stand_down_before_a_run_charges_nobody(self):
        # the armed-window check's read answers an error, and /mode then goes
        # unanswered through the stand-down's settle, the read after it and
        # the supervisor's own read: the run does not start, and the silence
        # is charged to nobody
        fc = self.on_a_machine()
        r = self.two()
        self.assertEqual(self.finish(r, "v.a")["result"], "PASS")
        clock, starts, missed = baseline.time, [], []
        fc.on_get = lambda path, q: ((missed.append(path) or (503, {"error": "busy"}))
                                     if path == "/cool/status" and not missed else None)
        real_post = fc.on_post
        fc.on_post = lambda path, form: (path == "/controller/start" and starts.append(clock.t)) or real_post(path, form)
        real = hw.Forgectrl.get

        def get(fc_, path, *a, **kw):
            if path == "/mode" and starts and clock.t < starts[0] + 35:
                raise hw.HwError("forgectrl GET /mode: timed out")
            return real(fc_, path, *a, **kw)
        with mock.patch.object(hw.Forgectrl, "get", get):
            fin = self.finish(r, "v.b")
        self.assertEqual((fin["result"], self.last()[-1].get("not_run"), self.last("leftover")), ("ERROR", True, []))

    def test_a_window_seen_after_the_cooling_engines_read_before_a_run_is_charged(self):
        # the client v.a left opens its session at the pump's read, the latch
        # still locked for its verdict wait: the window is the safety state
        self.on_a_machine(unlocks=False)
        r = self.two()
        self.assertEqual(self.finish(r, "v.a")["result"], "PASS")
        real = baseline.Baseline._pump_side

        def pump(bl, left):
            if bl._phase == "pre":
                self.arming = True
            return real(bl, left)
        with mock.patch.object(baseline.Baseline, "_pump_side", pump):
            fin = self.finish(r, "v.b")
        self.assertEqual((fin["result"], self.last()[-1].get("not_run"), self.ran), ("ERROR", True, []))
        self.assertEqual([(x["test"], x["safety"], x["leftovers"][0]["item"]) for x in self.last("leftover")],
                         [("v.a", True, "cool.armed")])

    def test_an_armed_window_is_in_the_log_before_the_stand_down_that_ends_it(self):
        # forgetest dies at the stand-down's stop: the window it read is
        # already charged
        fc = self.on_a_machine(unlocks=False)
        r = self.two()
        self.finish(r, "v.a")
        reads = []

        def on_get(path, q):
            # the armed-window check and its three re-reads show the window;
            # the stand-down's own read before its stop gets no answer
            if path == "/cool/status":
                reads.append(path)
                if len(reads) <= 4:
                    return 200, {"phase": "run", "armed": True, "hold": False}
                return 503, {"error": "busy"}
            return None
        fc.on_get = on_get
        real = baseline.Baseline.fc_post

        def post(bl, path, **kw):
            if path == "/controller/stop":
                raise Death()
            return real(bl, path, **kw)
        with mock.patch.object(baseline.Baseline, "fc_post", post), \
                mock.patch.object(runner.threading, "excepthook", lambda args: None):
            self.assertTrue(r._start_test("v.b")[0])
            deadline = time.time() + 60
            while any(th.name == "forgetest-run" for th in runner.threading.enumerate()) and time.time() < deadline:
                time.sleep(0.02)
        self.assertEqual([(x["test"], x["leftovers"][0]["item"]) for x in self.last("leftover")], [("v.a", "cool.armed")])

    def takeover_machine(self):
        """on_a_machine with forgectrl's own start and stop and the pulse
        device's holders faked for a takeover."""
        fc = self.on_a_machine()
        self.initd_calls = []
        self.in_settle = None

        def initd(service, action, timeout=60):
            self.initd_calls.append(action)
            if action == "start":
                fc.state["mode"] = dict(fc.state["mode"], controller="running")
            return 0, ""

        def settled(t):
            hook, self.in_settle = self.in_settle, None
            if hook:
                hook()                                      # while the takeover waits for the supervisor
            return {"controller": "running"}
        for target, attr, value in ((hw, "initd", initd),
                                    (runner.Takeover, "wait_settled", settled),
                                    (runner.Takeover, "holders", staticmethod(lambda: []))):
            p = mock.patch.object(target, attr, value)
            p.start()
            self.addCleanup(p.stop)
        real = baseline.Baseline.capture
        self.after_capture = None

        def capture(bl):
            out = real(bl)
            if self.after_capture:
                self.after_capture()                        # after the pass's last read
                self.after_capture = None
            return out
        p = mock.patch.object(baseline.Baseline, "capture", capture)
        p.start()
        self.addCleanup(p.stop)
        return fc

    def test_a_takeover_reads_the_latch_and_the_window_before_its_stop(self):
        # after the pass's last read, before a takeover: an unlock before a
        # bench tool's is charged to the run before it and the tool does not
        # start; an unlock (also one while the takeover waits for the
        # supervisor) or a window before a test's is its own, and the run
        # fails even when the body carries on past the refusal and the run
        # is aborted; a window read forgectrl does not answer (an error, or
        # no answer at all) is judged nowhere and refuses the takeover too.
        # A refusal stops the controller, which ends the window before the
        # pass after the run, and nothing else.
        fc = self.takeover_machine()
        unlock = lambda: self._attr("cnc/interlock_circuit", "37")       # noqa: E731
        arm = lambda: setattr(self, "arming", True)                       # noqa: E731
        miss, hang = [], []
        real_get = fc.on_get
        fc.on_get = lambda path, q: (miss.pop() if path == "/cool/status" and miss else real_get(path, q))
        real_hw_get = hw.Forgectrl.get

        def hw_get(fc_, path, **kw):
            if path == "/cool/status" and hang:
                hang.pop()
                raise hw.HwError("forgectrl GET /cool/status: timed out")
            return real_hw_get(fc_, path, **kw)
        p = mock.patch.object(hw.Forgectrl, "get", hw_get)
        p.start()
        self.addCleanup(p.stop)
        at_post = []
        real_laser = baseline.Baseline._laser_side

        def laser(bl, left):
            if bl._phase == "post":
                at_post.append(([p for p, _ in fc.posts].count("/controller/stop"), self.arming))
            return real_laser(bl, left)
        p = mock.patch.object(baseline.Baseline, "_laser_side", laser)
        p.start()
        self.addCleanup(p.stop)
        with self.subTest("an unlock before a bench tool"):
            r = self.two()
            self.finish(r, "v.a")
            ran = os.path.join(self.tmp, "tool-ran")
            self.after_capture = unlock
            fc.posts.clear()
            self.initd_calls.clear()
            self.bench(r, script="open(%r, 'w').close()\n" % ran, safety="takeover")
            self.assertFalse(os.path.exists(ran))
            self.assertEqual([(x["test"], x["safety"], x.get("found_during")) for x in self.last("leftover")],
                             [("v.a", True, "bench noop")])
            self.assertEqual((at_post[-1], self.initd_calls), ((1, False), []))
        for case, before, charged in (("an unlock before a test's", unlock, True),
                                      ("an unlock in the takeover's settle", lambda: setattr(self, "in_settle", unlock),
                                       True),
                                      ("a window before a test's", arm, True),
                                      ("a window read unanswered", lambda: miss.append((503, {"error": "busy"})),
                                       False),
                                      ("a window read that times out", lambda: hang.append(1), False)):
            with self.subTest(case):
                def body(ctx):
                    try:
                        with ctx.takeover():
                            pass
                    except runner.Failed:
                        ctx.run.abort()
                r = self.fresh(helpers.make_test("v.a", [("forgectrl", "src/ui.c")]),
                               helpers.make_test("v.b", [("forgectrl", "src/auth.c")], fn=body))
                self.finish(r, "v.a")
                self.after_capture = before
                fc.posts.clear()
                self.initd_calls.clear()
                fin = self.finish(r, "v.b")
                self.assertEqual(fin["result"], "FAIL" if charged else "ABORTED")
                self.assertEqual(("v.b", "test v.b") in [(x["test"], x.get("found_during"))
                                                         for x in self.last("leftover")], charged)
                self.assertEqual((at_post[-1], self.initd_calls), ((1, False), []))
        with self.subTest("a lock that fails is named so"):
            self._attr("cnc/interlock_circuit", "37")
            with mock.patch.object(hw, "sysfs_write", side_effect=OSError("read-only")):
                found, _ = runner.Takeover(lambda m: None, "t").read_before_stop()
            self.assertEqual([x.action.split(":")[0] for x in found], ["failed"])

    def test_a_refused_takeover_stops_only_a_running_controller_and_says_what_its_stop_answered(self):
        # the refusal's stop is made when the takeover's /mode read found a
        # controller running or went unanswered, never with none running (a
        # stop holds the supervisor in standby through its setup gate); the
        # window it found is labeled by what comes next, and the refusal says
        # what the stop answered: a stop refused or unanswered ended nothing
        fc = self.takeover_machine()
        miss, mode_miss, failing, stops = [], [], {}, []
        real_get = fc.on_get

        def on_get(path, q):
            if path == "/cool/status" and miss:
                return miss.pop()
            if path == "/mode" and mode_miss:
                mode_miss.pop()
                return 503, {"error": "busy"}
            return real_get(path, q)
        fc.on_get = on_get
        real_post = hw.Forgectrl.post

        def post(fc_, path, **kw):
            if path != "/controller/stop":
                return real_post(fc_, path, **kw)
            stops.append(path)
            how = failing.pop("how", None)
            if how == "refused":
                return 401, {"error": "not authorized"}
            if how == "unanswered":
                raise hw.HwError("forgectrl POST /controller/stop: timed out")
            return real_post(fc_, path, **kw)
        p = mock.patch.object(hw.Forgectrl, "post", post)
        p.start()
        self.addCleanup(p.stop)

        def gated():
            fc.state["mode"] = dict(fc.state["mode"], controller="gated")
            self.arming = True

        def waiting_unread():
            fc.state["mode"] = dict(fc.state["mode"], controller="waiting")
            miss.append((503, {"error": "busy"}))

        def mode_unanswered():
            self._attr("cnc/interlock_circuit", "37")
            mode_miss.append(1)

        def window(how):
            self.arming = True
            failing["how"] = how
        for case, before, made, label, answer in (
                ("no controller running", lambda: setattr(self, "in_settle", gated), 0,
                 "left to the hand-back", "no controller stop"),
                ("none running in the enclosure wait, the window read unanswered",
                 lambda: setattr(self, "in_settle", waiting_unread), 0, None, "no controller stop"),
                ("the mode read unanswered", lambda: setattr(self, "in_settle", mode_unanswered), 1, None,
                 "/controller/stop -> 200"),
                ("a window, the stop refused", lambda: window("refused"), 1, "ending it", "/controller/stop -> 401"),
                ("a window, the stop unanswered", lambda: window("unanswered"), 1, "ending it",
                 "/controller/stop -> no answer")):
            with self.subTest(case):
                refusals = []

                def body(ctx):
                    try:
                        with ctx.takeover():
                            pass
                    except runner.Failed as e:
                        refusals.append((str(e), len(stops)))
                        ctx.run.abort()
                r = self.fresh(helpers.make_test("v.a", [("forgectrl", "src/ui.c")]),
                               helpers.make_test("v.b", [("forgectrl", "src/auth.c")], fn=body))
                self.finish(r, "v.a")
                self.after_capture = before
                stops.clear()
                self.finish(r, "v.b")
                fc.state["mode"] = dict(fc.state["mode"], controller="running")
                self.arming = False
                labels = [i["action"].split(":")[0] for x in self.last("leftover") for i in x["leftovers"]
                          if i["item"] == "cool.armed" and i["found"].endswith("before the takeover")]
                self.assertEqual(([n for _, n in refusals], labels[:1], answer in "".join(m for m, _ in refusals)),
                                 ([made], [label] if label else [], True), refusals)

    def test_a_safety_record_nothing_took_is_held_and_refuses_starts_and_exports(self):
        # the log refuses the record, the run's result and its bare record
        # (or the tool has no result): the runner keeps the charge, and
        # starts and exports wait for it until the log takes it
        for case in ("a test", "a bench tool"):
            with self.subTest(case):
                r = self.fresh()
                self.finish(r, "v.a")
                self.refusing(lambda rec: rec.get("t") in ("leftover", "result"))
                self._attr("cnc/interlock_circuit", "37")
                if case == "a test":
                    self.assertEqual(self.finish(r, "v.b")["result"], "ERROR")
                else:
                    self.bench(r)
                self.assertFalse(r.export()[0])
                self.assertFalse(r._start_test("v.a")[0])
                del self.log.append                             # the disk freed
                self.finish(r, "v.a")
                self.assertIn(("v.a", True), self.charged_safety())

    def test_a_bare_record_carries_what_the_log_refused(self):
        r = self.two()
        self.finish(r, "v.a")
        self.refusing(lambda rec: rec.get("t") == "leftover" or (rec.get("t") == "result" and "evidence" in rec))
        self._attr("cnc/interlock_circuit", "37")
        self.finish(r, "v.b")
        bare = self.last()[-1]
        self.assertEqual((bare.get("not_recorded"), [c["test"] for c in bare.get("charges") or []]), (True, ["v.a"]))
        self.assertEqual(r.state()[0]["tests"]["v.a"]["reason"], "disposition")

    def test_a_safety_record_the_log_refuses_once_goes_in_at_the_end_of_the_pass(self):
        r = self.two()
        self.finish(r, "v.a")
        refused = []
        self.refusing(lambda rec: rec.get("t") == "leftover" and not refused and not refused.append(1))
        self._attr("cnc/interlock_circuit", "37")
        self.finish(r, "v.b")
        self.assertEqual((self.charged_safety(), self.last()[-1].get("charges")), ([("v.a", True)], None))

    def test_a_safety_finding_after_a_run_fails_it_even_when_its_record_cannot_be_made(self):
        r = self.runner_with(helpers.make_test("v.a", [("forgectrl", "src/ui.c")],
                                               fn=lambda ctx: (self.unlocks(ctx), ctx.run.abort())))
        with mock.patch.object(runner.Runner, "_safety_found", side_effect=RuntimeError("no record")):
            fin = self.finish(r, "v.a")
        self.assertEqual((fin["result"], self.last()[-1]["needs_disposition"]), ("FAIL", True))

    def test_a_safety_finding_is_charged_to_the_newest_run_not_an_older_one(self):
        r = self.two()
        self.finish(r, "v.a")
        self.finish(r, "v.b")
        self._attr("cnc/interlock_circuit", "37")
        self.finish(r, "v.a")
        self.assertEqual(self.charged_safety(), [("v.b", True)])

    def test_the_latch_read_at_the_end_of_the_pass_before_a_run_follows_the_safety_rule(self):
        # an unlock behind every read before it (here, while the pass looks at
        # the extensions): the last read of the pass finds it
        r = self.two()
        self.finish(r, "v.a")
        real = baseline.Baseline._ext_side
        unlocked = []

        def ext(bl, left):
            if not unlocked:
                unlocked.append(1)
                self._attr("cnc/interlock_circuit", "37")
            return real(bl, left)
        with mock.patch.object(baseline.Baseline, "_ext_side", ext):
            fin = self.finish(r, "v.b")
        self.assertEqual((fin["result"], self.last()[-1].get("not_run"), self.ran), ("ERROR", True, []))
        self.assertEqual(self.charged_safety(), [("v.a", True)])

    def test_forgectrls_reading_of_the_latch_before_a_run_follows_the_safety_rule(self):
        # only /status sees the unlock (the kernel's word reads locked again
        # by then): the run stops, and v.a is charged
        fc = self.on_a_machine()
        r = self.two()
        self.finish(r, "v.a")
        reads = []

        def on_get(path, q):
            if path == "/status" and not reads:
                reads.append(path)
                return 200, dict(fc.state["status"], laser_locked=False)
            return None
        fc.on_get = on_get
        fin = self.finish(r, "v.b")
        self.assertEqual((fin["result"], self.last()[-1].get("not_run"), self.ran), ("ERROR", True, []))
        self.assertEqual([(x["test"], x["safety"], x["leftovers"][0]["item"]) for x in self.last("leftover")],
                         [("v.a", True, "laser_locked")])

    def test_a_safety_finding_before_the_first_run_of_a_boot_charges_nobody(self):
        r = self.two()
        self.finish(r, "v.a")
        p = mock.patch.object(runner, "boot_id", lambda: "a later boot")
        p.start()
        self.addCleanup(p.stop)
        self._attr("cnc/interlock_circuit", "37")
        fin = self.finish(r, "v.b")
        self.assertEqual((fin["result"], self.last()[-1].get("not_run"), self.last("leftover")), ("ERROR", True, []))

    def test_an_unlock_before_a_mode_switch_stops_the_run(self):
        def switch(bl, want, timeout=None):
            bl.switch_found = [baseline.Leftover("laser_latch", "unlocked before a mode switch", "locked", "restored")]
            return True, "%s mode up" % want
        ran = []
        r = self.runner_with(helpers.make_test("v.c", [("forgectrl", "src/ui.c")], mode="cloud",
                                               fn=lambda ctx: ran.append(1)))
        with mock.patch.object(baseline.Baseline, "switch_mode", switch):
            fin = self.finish(r, "v.c")
        self.assertEqual((fin["result"], self.last()[-1].get("not_run"), ran), ("ERROR", True, []))

    def test_an_artifact_built_from_a_log_with_a_safety_leftover_is_refused_by_the_gate(self):
        # answered, with no newer PASS: an artifact built from the real log
        # that claims v.a's PASS anyway is refused, by what the export
        # carries of the leftover
        r = self.two()
        self.finish(r, "v.a")
        self._attr("cnc/interlock_circuit", "37")
        self.finish(r, "v.b")
        self.assertTrue(r.dispose("v.a", "the client was left running")[0])
        st, recs = r.state()
        passed = [x for x in recs if x.get("t") == "result" and x.get("test") == "v.a"][-1]
        st["tests"]["v.a"] = dict(st["tests"]["v.a"], satisfied=True, status="inherited",
                                  origin={"ts": passed["ts"], "campaign": passed["campaign"]})
        art = artifact.build(dict(st, authorized=True), r.tests(), r.manifest, recs, r.catalog_hash)
        rows = artifact.verify(art, r.manifest, r.tests(), r.catalog_hash)[1]
        why = [x for x in rows if x["id"] == "v.a"][0]["why"]
        self.assertEqual(len(why), 1, why)                   # the leftover, and nothing else
        self.assertFalse([x for x in rows if x["id"] == "v.a"][0]["ok"])

    def test_an_abort_during_the_pass_before_a_run_still_charges_the_lasers_safety_state(self):
        # read before any wait: the Abort cannot have made it up
        r = self.two()
        self.finish(r, "v.a")
        self._attr("cnc/interlock_circuit", "37")
        fin = self.aborted_in_the_pass(r)
        self.assertEqual((fin["result"], self.ran), ("ABORTED", []))
        self.assertEqual([(x["test"], x["safety"]) for x in self.last("leftover")], [("v.a", True)])
        self.assertEqual(r.state()[0]["tests"]["v.a"]["reason"], "disposition")

    # -- what the fixture holds, and a fixture that answers badly ----------
    class Holder:
        """The bench actuator holding the interlock loop open; its release
        notes what the latch read at that moment."""
        hostname, _ip, channels, arm_press = "forgefixture", "10.0.0.9", ("interlock",), False

        def __init__(self, status_error=None):
            self.status_error = status_error
            self.latch_at_release = []

        def covers(self, channel):
            return channel in self.channels

        def status(self):
            if self.status_error is not None:
                raise self.status_error
            return {"channels": {"interlock": "open"}}

        energized = staticmethod(fx_mod.Fixture.energized)

        def release(self):
            self.latch_at_release.append(hw.sysfs_read("cnc/laser_latch"))
            return {}

        def summary(self):
            return {}

    def with_fixture(self, holder):
        p = mock.patch.object(runner._fixture, "probe", lambda log, path=None, resolver=None: holder)
        p.start()
        self.addCleanup(p.stop)

    def test_the_latch_is_locked_before_the_fixture_lets_go(self):
        # a loop the fixture held open closes the chain when it lets go
        holder = self.Holder()
        self.with_fixture(holder)
        r = self.runner_with(helpers.make_test("v.a", [("forgectrl", "src/ui.c")], fn=self.unlocks))
        self.finish(r, "v.a")
        self.assertEqual(holder.latch_at_release, ["1"])

    def test_a_post_pass_that_breaks_before_its_lock_still_locks_before_the_fixture_lets_go(self):
        real = runner._baseline.Baseline._laser_side

        def breaks_after_the_run(bl, left):
            if bl._phase == "post":
                raise RuntimeError("the laser side broke")
            return real(bl, left)
        holder = self.Holder()
        self.with_fixture(holder)
        p = mock.patch.object(runner._baseline.Baseline, "_laser_side", breaks_after_the_run)
        p.start()
        self.addCleanup(p.stop)
        r = self.runner_with(helpers.make_test("v.a", [("forgectrl", "src/ui.c")], fn=self.unlocks))
        self.assertEqual(self.finish(r, "v.a")["result"], "ERROR")
        self.assertEqual(holder.latch_at_release, ["1"])

    def unlocks(self, ctx):
        """A body that leaves the latch unlocked."""
        self._attr("cnc/laser_latch", "0")
        self._attr("cnc/interlock_circuit", "37")

    def test_a_fixture_answer_that_cannot_be_read_still_hands_the_machine_back(self):
        def fails_unlocked(ctx):
            self.unlocks(ctx)
            raise runner.Failed("a check failed mid-cut")
        self.with_fixture(self.Holder(status_error=http.client.IncompleteRead(b"{")))
        r = self.runner_with(helpers.make_test("v.a", [("forgectrl", "src/ui.c")], fn=fails_unlocked))
        self.finish(r, "v.a")
        rec = self.last()[-1]
        self.assertEqual((rec["result"], rec["needs_disposition"]), ("FAIL", True))
        self.assertEqual(hw.sysfs_read("cnc/laser_latch"), "1")
        self.assertIn("fixture", self.post(rec))

    # -- a result the log cannot take ----------------------------------------
    def refusing(self, refuse):
        real = self.log.append

        def append(rec):
            if refuse(rec):
                raise OSError(28, "No space left on device")
            return real(rec)
        self.log.append = append

    def test_a_result_the_log_cannot_take_leaves_a_bare_error_that_holds_the_test(self):
        r = self.runner_with(helpers.make_test("v.a", [("forgectrl", "src/ui.c")]))
        self.assertEqual(self.finish(r, "v.a")["result"], "PASS")
        self.refusing(lambda rec: rec.get("t") == "result" and "evidence" in rec)
        fin = self.finish(r, "v.a")
        rec = self.last()[-1]
        self.assertEqual((fin["result"], rec["result"], rec["needs_disposition"], rec["not_recorded"]),
                         ("ERROR", "ERROR", True, True))
        self.assertFalse(r.busy())
        self.assertFalse(r.state()[0]["tests"]["v.a"]["satisfied"])

    def test_a_run_whose_start_cannot_be_recorded_does_not_start(self):
        r = self.two()
        self.refusing(lambda rec: rec.get("t") == "start")
        self.assertFalse(r._start_test("v.b")[0])
        self.assertIsNone(r.current)

    def test_a_run_the_log_saw_start_and_never_end_holds_its_test_across_a_restart(self):
        r = self.two()
        self.finish(r, "v.a")
        self.refusing(lambda rec: rec.get("t") == "result")     # v.b's result and its bare record
        self.assertEqual(self.finish(r, "v.b")["result"], "ERROR")
        r = self.runner_with(*r.tests())                       # a restart, the log taking records again
        st = r.state()[0]
        self.assertEqual((st["tests"]["v.b"]["reason"], st["campaign"]), ("disposition", None))
        self.assertTrue(st["tests"]["v.b"]["last"]["lost"])     # no result record to link to
        self.assertFalse(r.export()[0])
        # the run before the next one never ended: nothing is charged to v.a
        self._attr("cnc/motor_lock", "15")
        self.finish(r, "v.a")
        self.assertEqual(self.last("leftover"), [])
        self.assertTrue(r.reset("the disk was full")[0])
        self.assertTrue(r.export()[0])
        # answered, and run again: the lost run sits where it started
        self.assertTrue(r.dispose("v.b", "the disk was full")[0])
        self.finish(r, "v.b")
        self.assertTrue(r.state()[0]["tests"]["v.b"]["satisfied"])


if __name__ == "__main__":
    unittest.main()
