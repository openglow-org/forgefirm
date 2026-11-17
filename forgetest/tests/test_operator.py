# Copyright 2026 514 LLC d/b/a OpenGlow
# Written by Scott Wiederhold
# https://community.openglow.org
# SPDX-License-Identifier:    MIT

"""The operator channel: notices, `ready`, `act`, and the precheck.

A test asks for the operator's part in a way the page can show before
it is needed and a bench actuator can later perform: `act` names a
machine action and watches the machine for it (a standing notice for
the human, no button to race), `ready` pre-announces a timed step, and
a `precheck` refuses a start the machine cannot honor instead of
recording a FAIL.
"""
import os
import shutil
import tempfile
import threading
import time
import unittest

import helpers
from forgetest import catalog, hw
from forgetest.log import Log
from forgetest.runner import Context, Failed, Run, Runner


class Answerer:
    """Answers every prompt with its first option and records it, and
    records every notice it sees."""

    def __init__(self, run, on_prompt=None):
        self.run = run
        self.on_prompt = on_prompt
        self.asked = []
        self.notices = []
        self.stop = False
        threading.Thread(target=self._loop, daemon=True).start()

    def _loop(self):
        seen = None
        seen_n = None
        while not self.stop:
            n = self.run.notice
            if n and n["id"] != seen_n:
                seen_n = n["id"]
                self.notices.append(n["text"])
            p = self.run.prompt
            if p and p["id"] != seen:
                seen = p["id"]
                self.asked.append((p["question"], list(p["options"])))
                if self.on_prompt:
                    self.on_prompt(p)
                self.run.answer(p["id"], p["options"][0])
            time.sleep(0.01)


class ActTests(unittest.TestCase):
    def setUp(self):
        self.fc = helpers.FakeForgectrl().start()
        self.run = Run("test", "t.x", "t.x")
        self.ctx = Context(self.run, None, helpers.make_test("t.x", [], kind="operator"))
        self.ans = Answerer(self.run)

    def tearDown(self):
        self.ans.stop = True
        self.fc.stop()

    def lid(self, closed):
        self.fc.state["status"]["switches"]["lid"] = bool(closed)

    def interlock(self, ok):
        self.fc.state["status"]["switches"]["interlock_ok"] = bool(ok)

    def test_lid_open_is_a_notice_until_the_switch_shows_it(self):
        threading.Timer(0.3, self.lid, args=(False,)).start()
        dt = self.ctx.act("lid", "open", timeout=5)
        self.assertGreater(dt, 0.2)
        self.assertEqual(self.ans.asked, [])                   # no button to click
        self.assertEqual(self.ans.notices, ["Open the lid."])
        self.assertIsNone(self.run.notice)                     # cleared when seen
        rec = self.run.evidence["actions"][0]
        self.assertEqual((rec["channel"], rec["state"], rec["by"]), ("lid", "open", "operator"))
        self.assertIsNotNone(rec["took_s"])
        self.assertTrue(any("ACT lid open: done" in ln for ln in self.run.lines))

    def test_already_done_returns_at_once(self):
        self.lid(False)
        dt = self.ctx.act("lid", "open", timeout=2)
        self.assertLess(dt, 1.0)

    def test_interlock_watches_interlock_ok(self):
        threading.Timer(0.2, self.interlock, args=(False,)).start()
        self.ctx.act("interlock", "open", timeout=5)
        threading.Timer(0.2, self.interlock, args=(True,)).start()
        self.ctx.act("interlock", "close", timeout=5)
        self.assertEqual([r["state"] for r in self.run.evidence["actions"]], ["open", "close"])

    def test_a_timeout_fails_the_test_and_clears_the_notice(self):
        with self.assertRaises(Failed) as cm:
            self.ctx.act("lid", "open", timeout=0.6)
        self.assertIn("lid open was not seen", str(cm.exception))
        self.assertIsNone(self.run.notice)
        self.assertIsNone(self.run.evidence["actions"][0]["took_s"])

    def test_a_timeout_can_be_handed_back_instead(self):
        self.assertIsNone(self.ctx.act("lid", "open", timeout=0.4, fail=False))

    def test_a_button_press_needs_what_it_is_expected_to_do(self):
        with self.assertRaises(ValueError):
            self.ctx.act("button", "press")
        held = []
        threading.Timer(0.2, held.append, args=(1,)).start()
        self.ctx.act("button", "press", until=lambda: bool(held), timeout=5)
        self.assertEqual(self.ans.notices, ["Press the button once."])

    def test_unknown_actions_are_refused(self):
        with self.assertRaises(ValueError):
            self.ctx.act("lid", "press")

    def test_ready_is_a_single_button_prompt(self):
        self.ctx.ready("On Ready the head moves.")
        self.assertEqual(self.ans.asked, [("On Ready the head moves.", ["Ready", "Cannot"])])

    def test_cannot_on_ready_fails(self):
        self.ans.stop = True
        ans = Answerer(self.run, on_prompt=lambda p: None)
        ans.stop = True
        time.sleep(0.05)

        def say_cannot():
            while self.run.prompt is None:
                time.sleep(0.01)
            self.run.answer(self.run.prompt["id"], "Cannot")
        threading.Thread(target=say_cannot, daemon=True).start()
        with self.assertRaises(Failed):
            self.ctx.ready("x")


class VerdictPrimitiveTests(unittest.TestCase):
    """What every catalog verdict goes through, on the runner's own Context."""

    def setUp(self):
        self.run = Run("test", "t.x", "t.x")
        self.ctx = Context(self.run, None, helpers.make_test("t.x", [], kind="operator"))

    def answer(self, value):
        def go():
            while self.run.prompt is None:
                time.sleep(0.01)
            self.run.answer(self.run.prompt["id"], value)
        threading.Thread(target=go, daemon=True).start()

    def test_a_reading_that_failed_fails_the_check(self):
        # A reader returns None when it could not read: never a pass.
        with self.assertRaises(Failed):
            self.ctx.check(None, "the print did not start after the press")

    def test_the_operators_no_and_cannot_fail_the_test(self):
        self.answer("No")
        with self.assertRaises(Failed):
            self.ctx.confirm("Is there a mark on the scrap?")
        self.answer("Cannot")
        with self.assertRaises(Failed):
            self.ctx.instruct("Open the lid.")


class ReaderTests(unittest.TestCase):
    """What the readers behind a test's judgment (hw.py) make of a value
    they cannot use: never a reading."""

    def test_an_unparseable_attribute_reads_as_no_value(self):
        # never 0, which the core latch test would take for a dark FIRE line
        tmp = tempfile.mkdtemp(prefix="forgetest-rd-")
        self.addCleanup(shutil.rmtree, tmp, ignore_errors=True)
        os.makedirs(os.path.join(tmp, "cnc"))
        with open(os.path.join(tmp, "cnc", "laser_enable"), "w") as f:
            f.write("garbage\n")
        os.environ["GF_SYSFS_ROOT"] = tmp + os.sep
        self.addCleanup(os.environ.pop, "GF_SYSFS_ROOT", None)
        self.assertIsNone(hw.sysfs_int("cnc/laser_enable"))

    def test_an_error_status_is_never_the_machines_status(self):
        fc = helpers.FakeForgectrl().start()
        self.addCleanup(fc.stop)
        fc.on_get = lambda path, q: (503, {"state": "idle", "laser_locked": True}) if path == "/status" else None
        with self.assertRaises(hw.HwError):
            hw.Forgectrl().status()

    def test_a_status_report_buffered_before_the_question_is_dropped(self):
        dev = helpers.FakeGrbl().start()
        self.addCleanup(dev.stop)
        with hw.Grbl() as g:
            g.buf = b"<Run|MPos:1.000,0.000,0.000|FS:0,0>\r\n"
            dev.unanswered = 1                  # the answer comes only to the '?' sent again
            self.assertEqual(g.status_report()["state"], "Idle")


class PrecheckTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="forgetest-pre-")
        os.environ["FORGETEST_DATA"] = self.tmp
        os.environ["FORGETEST_MARKER"] = os.path.join(self.tmp, "marker")
        helpers.host_run(self)
        self.man = helpers.make_manifest()
        self.blocked = {"why": "HV reports good: open the lid"}
        self.reg = helpers.registry(
            helpers.make_test("p.gated", [("forgectrl", "src/main.c")],
                              precheck=lambda: self.blocked["why"]),
            helpers.make_test("p.free", [("forgectrl", "src/ui.c")]),
        )
        self.log = Log(os.path.join(self.tmp, "r.jsonl"))
        self.runner = Runner(self.log, self.man, self.reg)

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_a_precheck_reason_refuses_the_start_without_a_result(self):
        ok, msg = self.runner.start_test("p.gated")
        self.assertFalse(ok)
        self.assertEqual(msg, "cannot start: HV reports good: open the lid")
        self.assertEqual([r for r in self.log.read() if r["t"] == "result"], [])
        # the campaign is not even opened by a refused start
        self.assertEqual([r for r in self.log.read() if r["t"] == "campaign"], [])

    def test_a_clear_precheck_starts(self):
        self.blocked["why"] = None
        ok, msg = self.runner.start_test("p.gated")
        self.assertTrue(ok, msg)
        while self.runner.busy():
            time.sleep(0.02)
        self.assertEqual(self.runner.state()[0]["tests"]["p.gated"]["status"], "pass")

    def test_the_queue_skips_it_with_the_reason_and_carries_on(self):
        ok, msg, order = self.runner.start_batch("unattended")
        self.assertTrue(ok, msg)
        deadline = time.time() + 20
        while not self.runner.batch["finished"] and time.time() < deadline:
            time.sleep(0.02)
        b = self.runner.batch_snapshot()
        self.assertEqual([x["test"] for x in b["skipped"]], ["p.gated"])
        self.assertIn("cannot start: HV reports good", b["skipped"][0]["reason"])
        self.assertEqual([x["test"] for x in b["done"]], ["p.free"])

    def test_a_body_that_returns_after_an_abort_is_aborted(self):
        def ends_aborted(ctx):
            ctx.run.abort()                 # the Abort lands as the body returns
        self.reg["p.abort"] = helpers.make_test("p.abort", [("forgectrl", "src/cool.c")], fn=ends_aborted)
        self.runner.registry = self.reg
        ok, msg, run = self.runner._start_test("p.abort")
        self.assertTrue(ok, msg)
        while run.finished is None:
            time.sleep(0.02)
        self.assertEqual(run.finished["result"], "ABORTED")

    def test_an_erroring_precheck_refuses_rather_than_crashes(self):
        def boom():
            raise RuntimeError("sysfs gone")
        t = helpers.make_test("p.boom", [], precheck=boom)
        self.assertIn("precheck errored: RuntimeError: sysfs gone", t.cannot_start())

    def test_the_catalog_describes_actions_and_the_precheck(self):
        t = helpers.make_test("p.x", [], kind="operator", actions=("lid", "button"),
                              precheck=lambda: None)
        d = t.describe()
        self.assertEqual(d["actions"], ["lid", "button"])
        self.assertTrue(d["precheck"])
        # neither is part of the gate-visible definition
        self.assertNotIn("actions", t.definition())

    def test_the_decorator_checks_actions(self):
        saved = dict(catalog.REGISTRY)
        try:
            with self.assertRaises(ValueError):
                catalog.test("x.a", title="t", subsystem="x", kind="operator", actions=("foot",))(lambda c: None)
            with self.assertRaises(ValueError):
                catalog.test("x.b", title="t", subsystem="x", kind="auto", actions=("lid",))(lambda c: None)
            with self.assertRaises(ValueError):
                catalog.test("x.c", title="t", subsystem="x", precheck="no")(lambda c: None)
            catalog.test("x.d", title="t", subsystem="x", kind="operator", actions=("lid",),
                         precheck=lambda: None)(lambda c: None)
            self.assertIn("x.d", catalog.REGISTRY)
        finally:
            catalog.REGISTRY.clear()
            catalog.REGISTRY.update(saved)


class AccelSamplerTests(unittest.TestCase):
    """The head accelerometer sampler over a fake iio tree."""

    def setUp(self):
        from forgetest import hw
        self.hw = hw
        self.tmp = tempfile.mkdtemp(prefix="forgetest-iio-")
        d = os.path.join(self.tmp, "iio_device1")      # no colon: the host may be Windows
        os.makedirs(d)
        with open(os.path.join(d, "name"), "w") as f:
            f.write("lis2hh12 3-001e\n")
        self.dir = d
        self.write(100, 200)
        os.environ["GF_IIO_ROOT"] = self.tmp

    def tearDown(self):
        os.environ.pop("GF_IIO_ROOT", None)
        shutil.rmtree(self.tmp, ignore_errors=True)

    def write(self, x, y):
        for axis, v in (("x", x), ("y", y)):
            with open(os.path.join(self.dir, "in_accel_%s_raw" % axis), "w") as f:
                f.write("%d\n" % v)

    def test_found_by_bus_address_and_sampled(self):
        self.assertEqual(self.hw.head_accel_dir(), self.dir)
        s = self.hw.AccelSampler(period=0.02)
        self.assertTrue(s.available)
        with s:
            t0 = time.time()
            time.sleep(0.1)
            self.write(1100, 200)
            time.sleep(0.1)
            self.write(100, 200)
            time.sleep(0.1)
            t1 = time.time()
        p2px, p2py, n = s.p2p(t0, t1)
        self.assertGreaterEqual(n, 5)
        self.assertEqual((p2px, p2py), (1000, 0))
        # a window with no samples reads as nothing, not as an error
        self.assertEqual(s.p2p(t1 + 10, t1 + 20), (0, 0, 0))

    def test_absent_device_is_unavailable(self):
        os.environ["GF_IIO_ROOT"] = os.path.join(self.tmp, "nowhere")
        s = self.hw.AccelSampler()
        self.assertFalse(s.available)
        with s:
            pass
        self.assertEqual(s.p2p(0), (0, 0, 0))


if __name__ == "__main__":
    unittest.main()
