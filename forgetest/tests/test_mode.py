# Copyright 2026 514 LLC d/b/a OpenGlow
# Written by Scott Wiederhold
# https://community.openglow.org
# SPDX-License-Identifier:    MIT

"""The controller mode a test declares: a switch the supervisor refuses,
or one it takes and whose controller never comes up, fails the test before
its body runs, so a test never judges the machine in a mode it did not ask
for.
"""
import os
import shutil
import tempfile
import time
import unittest
from unittest import mock

import helpers
from forgetest import baseline
from forgetest.log import Log
from forgetest.runner import Runner


class RunnerModeTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="forgetest-mode-")
        os.environ["FORGETEST_DATA"] = self.tmp
        os.environ["FORGETEST_MARKER"] = os.path.join(self.tmp, "marker")
        self.fc = helpers.FakeForgectrl().start()
        helpers.host_run(self)                  # a fake forgectrl, no kernel module
        p = mock.patch.object(baseline, "NOHUNT_MARKER", os.path.join(self.tmp, "nohunt"))
        p.start()
        self.addCleanup(p.stop)
        self.seen = {}
        fc, seen = self.fc, self.seen

        def t_sees_mode(ctx):
            seen["mode"] = fc.state["mode"]["mode"]

        self.reg = helpers.registry(
            helpers.make_test("m.grbl", [("forgectrl", "src/ui.c")], fn=t_sees_mode, mode="grbl"),
            helpers.make_test("m.cloud", [("forgectrl", "src/ui.c")], fn=t_sees_mode, mode="cloud"))

    def tearDown(self):
        self.fc.stop()
        shutil.rmtree(self.tmp, ignore_errors=True)
        for k in ("FORGETEST_DATA", "FORGETEST_MARKER"):
            os.environ.pop(k, None)

    def run_test(self, tid, log):
        runner = Runner(Log(os.path.join(self.tmp, log)), helpers.make_manifest(), self.reg)
        ok, msg = runner.start_test(tid)
        self.assertTrue(ok, msg)
        run = runner.current
        deadline = time.monotonic() + 30
        while not run.finished and time.monotonic() < deadline:
            time.sleep(0.05)
        self.assertTrue(run.finished, "run did not finish")
        return run

    def test_a_switch_that_fails_fails_the_test_before_it_runs(self):
        fc = self.fc

        def refused(path, form):
            return 503, {"error": "supervisor busy"}

        def never_comes_up(path, form):
            # the switch is taken and the cloud client never starts (stopped,
            # or held by the setup gate): no Grbl port wait stands behind it
            if path == "/mode":
                fc.state["mode"] = {"mode": "cloud", "controller": "starting", "pid": 0, "motion": "unverified"}
                return 200, {"ok": True}
            return None

        for name, test, found, on_post in (("refused", "m.grbl", "cloud", refused),
                                           ("never comes up", "m.cloud", "grbl", never_comes_up)):
            with self.subTest(name):
                self.seen.clear()
                fc.state["mode"] = {"mode": found, "controller": "running", "pid": 7, "motion": "verified"}
                fc.state["settings"]["controller_mode"] = found
                fc.on_post = on_post
                # the supervisor's settle wait on a clock that moves when it sleeps
                with mock.patch.object(baseline, "time", helpers.Clock()):
                    run = self.run_test(test, "results-%s.jsonl" % name.replace(" ", "-"))
                self.assertEqual(run.finished["result"], "FAIL", run.finished)
                self.assertNotIn("mode", self.seen, "the test's body ran in a mode it did not ask for")


if __name__ == "__main__":
    unittest.main()
