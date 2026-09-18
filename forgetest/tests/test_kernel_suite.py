# Copyright 2026 514 LLC d/b/a OpenGlow
# Written by Scott Wiederhold
# https://community.openglow.org
# SPDX-License-Identifier:    MIT

"""wait_hv_off against a scripted charge-pump readback: a chain that
releases inside the window passes, a chain held past it is the refusal,
and a chain already off costs no wait."""
import time
import unittest

from forgetest.suite import kernel


class Ctx:
    def __init__(self):
        self.evidence = {}
        self.lines = []

    def sleep(self, s):
        time.sleep(s)

    def log(self, fmt, *a):
        self.lines.append(fmt % a)


def scripted(alive_for_s):
    t0 = time.time()

    def rd(path):
        if path == "cnc/charge_pump_alive":
            return "1" if time.time() - t0 < alive_for_s else "0"
        if path == "cnc/state":
            return "idle"
        return None
    return rd


class WaitHvOffTests(unittest.TestCase):
    def setUp(self):
        self._rd = kernel.rd

    def tearDown(self):
        kernel.rd = self._rd

    def test_release_inside_the_window_passes(self):
        kernel.rd = scripted(0.3)
        ctx = Ctx()
        t0 = time.time()
        self.assertIsNone(kernel.wait_hv_off(ctx, timeout_s=2.0))
        self.assertGreaterEqual(time.time() - t0, 0.25)
        self.assertTrue(any("released" in l for l in ctx.lines))
        self.assertGreaterEqual(ctx.evidence["hv_release_s"][0], 0.25)

    def test_chain_held_past_the_window_is_the_refusal(self):
        kernel.rd = scripted(99)
        ctx = Ctx()
        why = kernel.wait_hv_off(ctx, timeout_s=0.3)
        self.assertIn("charge_pump_alive=1", why)
        self.assertTrue(any("still held" in l for l in ctx.lines))

    def test_chain_already_off_costs_no_wait(self):
        kernel.rd = scripted(0)
        ctx = Ctx()
        t0 = time.time()
        self.assertIsNone(kernel.wait_hv_off(ctx, timeout_s=2.0))
        self.assertLess(time.time() - t0, 0.1)
        self.assertEqual(ctx.lines, [])


if __name__ == "__main__":
    unittest.main()
