# Copyright 2026 514 LLC d/b/a OpenGlow
# Written by Scott Wiederhold
# https://community.openglow.org
# SPDX-License-Identifier:    MIT

"""The controller mode a test declares: the runner puts the machine there
before the test and the baseline keeps it there afterward.

The cloud tests enter cloud mode and stay (the operator rule: no switch
back after every test), so a queue that runs on past them reaches the
motion tests with gfcloud as the controller and no grblHAL process to
find. A test that names its mode gets the switch made for it, once, in the
pre pass - after the leftovers are handled and before the preserved state
is captured, so the post pass hands back the mode the test asked for
rather than the one the run found.
"""
import os
import shutil
import socket
import tempfile
import threading
import time
import unittest

import helpers
from forgetest import baseline
from forgetest.log import Log
from forgetest.runner import Runner


class GrblPort:
    """A listening socket standing in for the Grbl port of grblHAL."""

    def __init__(self):
        self.sock = socket.socket()
        self.sock.bind(("127.0.0.1", 0))
        self.sock.listen(5)
        os.environ["GRBL_HOST"] = "127.0.0.1"
        os.environ["GRBL_PORT"] = str(self.sock.getsockname()[1])
        self._stop = False
        self._th = threading.Thread(target=self._accept, daemon=True)
        self._th.start()

    def _accept(self):
        self.sock.settimeout(0.2)
        while not self._stop:
            try:
                c, _ = self.sock.accept()
                c.close()
            except OSError:
                pass

    def close(self):
        self._stop = True
        self.sock.close()
        for k in ("GRBL_HOST", "GRBL_PORT"):
            os.environ.pop(k, None)


def cloud(fc):
    fc.state["mode"] = {"mode": "cloud", "controller": "running", "pid": 7, "motion": "verified"}
    fc.state["settings"]["controller_mode"] = "cloud"


class SwitchModeTests(unittest.TestCase):
    """Baseline.switch_mode against the fake forgectrl."""

    def setUp(self):
        self.fc = helpers.FakeForgectrl().start()
        self.port = GrblPort()
        baseline.Baseline._unreachable_until = 0.0
        self.lines = []

    def tearDown(self):
        self.port.close()
        self.fc.stop()

    def bl(self):
        return baseline.Baseline(self.lines.append)

    def test_already_in_the_mode_posts_nothing(self):
        ok, detail = self.bl().switch_mode("grbl")
        self.assertTrue(ok, detail)
        self.assertEqual(self.fc.posts, [])

    def test_the_supervisors_levers_get_the_slow_timeout(self):
        from forgetest import hw
        fc = hw.Forgectrl(timeout=1.0, slow_timeout=2.0)
        self.assertEqual(fc.timeout_for("POST", "/mode"), 2.0)
        self.assertEqual(fc.timeout_for("POST", "/controller/start"), 2.0)
        self.assertEqual(fc.timeout_for("POST", "/controller/stop"), 2.0)
        self.assertEqual(fc.timeout_for("GET", "/mode"), 1.0)
        self.assertEqual(fc.timeout_for("POST", "/settings"), 1.0)
        self.assertGreater(hw.SLOW_TIMEOUT_S, 15.0)         # forgectrl's first-report wait
        # a switch that answers after the ordinary timeout still lands
        import time

        def on_post(path, form):
            time.sleep(1.5)
            self.fc.state["mode"] = dict(self.fc.state["mode"], mode="cloud")
            return None
        self.fc.on_post = on_post
        st, body = fc.post("/mode", data={"controller": "cloud"})
        self.assertEqual(st, 200)                            # 1.5 s > timeout, < slow_timeout
        self.assertEqual(self.fc.state["mode"]["mode"], "cloud")

    def test_cloud_to_grbl_switches_and_waits_for_the_port(self):
        cloud(self.fc)
        b = self.bl()
        ok, detail = b.switch_mode("grbl")
        self.assertTrue(ok, detail)
        self.assertEqual(self.fc.posts, [("/mode", {"controller": "grbl"})])
        self.assertEqual(self.fc.state["mode"]["mode"], "grbl")
        self.assertEqual(b.mode, "grbl")

    def test_a_controller_that_never_comes_up_is_reported(self):
        cloud(self.fc)
        self.fc.on_post = lambda path, form: (200, {"ok": True})     # accepted, nothing happens
        ok, detail = self.bl().switch_mode("grbl", timeout=2)
        self.assertFalse(ok)
        self.assertIn("did not come up", detail)

    def test_a_refused_switch_is_reported(self):
        cloud(self.fc)
        self.fc.on_post = lambda path, form: (409, {"error": "busy"})
        ok, detail = self.bl().switch_mode("grbl")
        self.assertFalse(ok)
        self.assertIn("409", detail)

    def test_grbl_needs_the_port_open(self):
        cloud(self.fc)
        self.port.close()                          # grblHAL running, port never listening
        os.environ["GRBL_HOST"], os.environ["GRBL_PORT"] = "127.0.0.1", "1"
        old = baseline.GRBL_PORT_S
        baseline.GRBL_PORT_S = 2
        try:
            ok, detail = self.bl().switch_mode("grbl")
        finally:
            baseline.GRBL_PORT_S = old
        self.assertFalse(ok)
        self.assertIn("Grbl port", detail)


class RunnerModeTests(unittest.TestCase):
    """The runner end to end: a declared mode is established before the
    test function runs and kept by the post pass."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="forgetest-mode-")
        os.environ["FORGETEST_DATA"] = self.tmp
        os.environ["FORGETEST_MARKER"] = os.path.join(self.tmp, "marker")
        self.fc = helpers.FakeForgectrl().start()
        self.port = GrblPort()
        baseline.Baseline._unreachable_until = 0.0
        self.seen = {}
        fc, seen = self.fc, self.seen

        def t_sees_mode(ctx):
            seen["mode"] = fc.state["mode"]["mode"]

        reg = helpers.registry(
            helpers.make_test("m.grbl", [("forgectrl", "src/ui.c")], fn=t_sees_mode, mode="grbl"),
            helpers.make_test("m.any", [("forgectrl", "src/ui.c")], fn=t_sees_mode),
        )
        self.runner = Runner(Log(os.path.join(self.tmp, "results.jsonl")), helpers.make_manifest(), reg)

    def tearDown(self):
        self.port.close()
        self.fc.stop()
        shutil.rmtree(self.tmp, ignore_errors=True)
        for k in ("FORGETEST_DATA", "FORGETEST_MARKER"):
            os.environ.pop(k, None)

    def run_test(self, tid):
        ok, msg = self.runner.start_test(tid)
        self.assertTrue(ok, msg)
        run = self.runner.current
        deadline = time.time() + 30
        while not run.finished and time.time() < deadline:
            time.sleep(0.05)
        self.assertTrue(run.finished, "run did not finish")
        return run

    def test_grbl_test_found_in_cloud_mode_gets_the_switch_and_keeps_it(self):
        cloud(self.fc)
        run = self.run_test("m.grbl")
        self.assertEqual(run.finished["result"], "PASS", run.finished)
        self.assertEqual(self.seen["mode"], "grbl")
        # one switch, made before the test; the post pass keeps grbl
        self.assertEqual(self.fc.posts, [("/mode", {"controller": "grbl"})])
        self.assertEqual(self.fc.state["mode"]["mode"], "grbl")
        self.assertEqual(run.baseline_captured["mode"], "grbl")
        self.assertEqual(run.evidence["mode"]["found"], "cloud")
        self.assertTrue(run.evidence["mode"]["switched"])
        self.assertTrue(any("mode: test needs grbl" in ln for ln in run.lines), run.lines)

    def test_grbl_test_in_grbl_mode_switches_nothing(self):
        run = self.run_test("m.grbl")
        self.assertEqual(run.finished["result"], "PASS", run.finished)
        self.assertEqual(self.fc.posts, [])
        self.assertFalse(run.evidence["mode"]["switched"])

    def test_undeclared_test_runs_in_the_mode_it_finds(self):
        cloud(self.fc)
        run = self.run_test("m.any")
        self.assertEqual(run.finished["result"], "PASS", run.finished)
        self.assertEqual(self.seen["mode"], "cloud")
        self.assertEqual(self.fc.posts, [])
        self.assertNotIn("mode", run.evidence)

    def test_a_switch_that_fails_fails_the_test_before_it_runs(self):
        cloud(self.fc)
        self.fc.on_post = lambda path, form: (503, {"error": "supervisor busy"})
        run = self.run_test("m.grbl")
        self.assertEqual(run.finished["result"], "FAIL")
        self.assertIn("needs grbl mode", run.finished["message"])
        self.assertNotIn("mode", self.seen)


if __name__ == "__main__":
    unittest.main()
