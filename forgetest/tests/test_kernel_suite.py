# Copyright 2026 514 LLC d/b/a OpenGlow
# Written by Scott Wiederhold
# https://community.openglow.org
# SPDX-License-Identifier:    MIT

"""wait_hv_off against a scripted charge-pump readback: a chain that
releases inside the window passes, a chain held past it is the refusal,
and a chain already off costs no wait. The dead man's close judge of the
heat sources: it passes only when the heater and the TEC were on before
the close and are off after it, duty and pins, with no lease lapse."""
import os
import tempfile
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


GPIO_LISTING = """gpiochip5: GPIOs 160-191, parent: platform/20b0000.gpio, 20b0000.gpio:
 gpio-168 (                    |water-pump-gpio     ) out hi
 gpio-170 (                    |tec-gpio            ) out %s
gpiochip6: GPIOs 192-223, parent: platform/20b4000.gpio, 20b4000.gpio:
 gpio-201 (                    |water-heater-gpio   ) out %s
"""


class DeadmanThermalTests(unittest.TestCase):
    ON = {"heater_pwm": "65535", "tec_on": "1", "heater_lapses": "0", "heater_pin": "hi", "tec_pin": "hi"}
    OFF = {"heater_pwm": "0", "tec_on": "0", "heater_lapses": "0", "heater_pin": "lo", "tec_pin": "lo"}

    def test_the_pins_are_read_from_the_gpio_listing(self):
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "gpio")
            with open(path, "w") as f:
                f.write(GPIO_LISTING % ("lo", "hi"))
            self.assertEqual(kernel.gpio_levels(path), {"heater": "hi", "tec": "lo"})
            self.assertEqual(kernel.gpio_levels(os.path.join(d, "absent")), {"heater": None, "tec": None})

    def test_the_close_must_take_both_heat_sources_off_with_no_lapse(self):
        self.assertIsNone(kernel.deadman_thermal_why(self.ON, self.OFF))
        for name, before, after in (
                ("the heater never on", dict(self.ON, heater_pin="lo"), self.OFF),
                ("the duty left on", self.ON, dict(self.OFF, heater_pwm="65535")),
                ("the heater pin left high", self.ON, dict(self.OFF, heater_pin="hi")),
                ("the TEC left on", self.ON, dict(self.OFF, tec_on="1", tec_pin="hi")),
                ("off by the lease", self.ON, dict(self.OFF, heater_lapses="1"))):
            with self.subTest(name):
                self.assertIsNotNone(kernel.deadman_thermal_why(before, after))


if __name__ == "__main__":
    unittest.main()
