# Copyright 2026 514 LLC d/b/a OpenGlow
# Written by Scott Wiederhold
# https://community.openglow.org
# SPDX-License-Identifier:    MIT

"""cooling.fail-tier-stop's judgment after the crash tier, over a scripted
machine: the session ended, the fault must still block the next job (the
verdict ACK, no fire), the button must flash red alone, and the
Acknowledge must be taken, free the machine and darken the button. The
controller's restart is answered at once; the laser lock and the button
LEDs are a fake sysfs tree and LED class read by the real readers. A run
that holds every rule passes; one rule broken, the rest held, fails on
that rule."""
import contextlib
import os
import shutil
import tempfile
import unittest
from unittest import mock

import helpers
from forgetest.runner import Failed
from forgetest.suite import cooling, laser, motion

RED = ((255, 300), (0, 0), (0, 0))          # (target, pulse_on) of button_led_1..3: channel 1 is red


class FailTierStopJudgeTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="forgetest-failtier-")
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        os.makedirs(os.path.join(self.tmp, "sysfs", "cnc"))
        with open(os.path.join(self.tmp, "sysfs", "cnc", "interlock_circuit"), "w") as f:
            f.write("45")                                       # the laser lock set (bit 3)
        for k, root in (("GF_SYSFS_ROOT", "sysfs"), ("GF_LEDS_ROOT", "leds")):
            os.environ[k] = os.path.join(self.tmp, root) + os.sep
            self.addCleanup(os.environ.pop, k, None)
        for mod, name, fn in ((laser, "prepare", lambda ctx, g: None), (laser, "stream", lambda g, lines: None),
                              (laser, "sample", lambda ctx: {"armed": True, "emission": 0, "kstate": "idle"}),
                              (motion, "_log_offset", lambda path: 0),
                              (motion, "_log_lines", lambda path, offset, needle: ["forgectrl: " + needle])):
            p = mock.patch.object(mod, name, fn)
            p.start()
            self.addCleanup(p.stop)
        # the machine once the crash fault has ended with its session
        self.after_session = {"verdict": "ACK", "fire_ok": False, "attention": {"standing": True, "live": False}}
        self.button = RED
        self.ack_status = 200
        self.after_ack = {"verdict": "OK", "fire_ok": True}
        self.ack_darkens = True

    def leds(self, pattern):
        for i, (target, pulse) in enumerate(pattern):
            d = os.path.join(self.tmp, "leds", "button_led_%d" % (i + 1))
            os.makedirs(d, exist_ok=True)
            for attr, val in (("target", target), ("pulse_on", pulse)):
                with open(os.path.join(d, attr), "w") as f:
                    f.write(str(val))

    def judge(self):
        self.leds(self.button)
        settings = {k: "40" for k in cooling.CRASH_KEYS}
        state = {"mode_reads": 0, "acked": False}

        def get(path):
            if path == "/mode":
                state["mode_reads"] += 1            # the supervisor's new controller after the first read
                return 200, {"controller": "running", "pid": 100 if state["mode_reads"] == 1 else 200}
            if path == "/cool/status":
                return 200, dict(self.after_ack if state["acked"] else self.after_session)
            return None, None

        def post(path, params=None, data=None):
            if path == "/settings":
                settings.update(params or {})
                return 200, dict(settings)
            if path == "/cool/attention/ack" and self.ack_status == 200:
                state["acked"] = True
                if self.ack_darkens:
                    self.leds(((0, 0),) * 3)
            return self.ack_status, {}
        fc = mock.Mock(settings=lambda: dict(settings), wait_idle=lambda *a, **k: True, get=get, post=post)
        ctx = helpers.JudgeCtx(forgectrl=fc)
        ctx.grbl = lambda: contextlib.nullcontext(None)
        ctx.ready = ctx.arm_press = ctx.clear_notice = lambda *a, **k: None
        cooling.fail_tier_stop(ctx)
        self.assertEqual(settings, {k: "40" for k in cooling.CRASH_KEYS}, "the thresholds are put back")
        return ctx

    def assertJudged(self, needle):
        with self.assertRaises(Failed) as cm:
            self.judge()
        self.assertIn(needle, str(cm.exception))

    def test_a_crash_tier_that_holds_every_rule_passes(self):
        ctx = self.judge()
        self.assertEqual(ctx.evidence["result"]["after_session"]["verdict"], "ACK")

    def test_a_fault_that_does_not_block_the_next_job_fails(self):
        self.after_session = {"verdict": "OK", "fire_ok": True, "attention": {}}
        self.assertJudged("does not block the next job")

    def test_a_button_not_flashing_red_alone_fails(self):
        self.button = ((255, 1800), (255, 1800), (255, 1800))
        self.assertJudged("not flashing red")

    def test_an_acknowledge_refused_not_freeing_or_leaving_the_button_lit_fails(self):
        self.ack_status = 409
        self.assertJudged("the acknowledgment was refused")
        self.ack_status, self.after_ack = 200, dict(self.after_session)
        self.assertJudged("did not free the machine")
        self.after_ack, self.ack_darkens = {"verdict": "OK", "fire_ok": True}, False
        self.assertJudged("still lit after the acknowledgment")


if __name__ == "__main__":
    unittest.main()
