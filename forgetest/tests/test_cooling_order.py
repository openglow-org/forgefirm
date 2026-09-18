# Copyright 2026 514 LLC d/b/a OpenGlow
# Written by Scott Wiederhold
# https://community.openglow.org
# SPDX-License-Identifier:    MIT

"""cooling.aa-offset-calibrate is registered before the heater tools, so
a queue runs it first: a heater trial leaves a warm slug circulating past
the coolant sensors for minutes, and the tool's edges read it as
disagreement."""
import unittest

from forgetest import catalog


class CoolingOrderTests(unittest.TestCase):
    def test_aa_offset_precedes_flow_verify(self):
        reg = catalog.load_suite()
        tests = catalog.all_tests(reg)
        order = catalog.order_by_requires(tests, [t.id for t in tests])
        self.assertLess(order.index("cooling.aa-offset-calibrate"), order.index("cooling.flow-verify"))


if __name__ == "__main__":
    unittest.main()
