# Copyright 2026 514 LLC d/b/a OpenGlow
# Written by Scott Wiederhold
# https://community.openglow.org
# SPDX-License-Identifier:    MIT

"""The dwell-gap verdict of laser.emission-witness over synthetic trails:
the button latch judged from the first emission through every sample
whose readback word shows the laser latch unlocked, both bits from that
word, so the relocked tail (where the relock itself sets the latch and
the emission counter still reads nonzero) is never judged."""
import unittest

from forgetest.suite import laser


def smp(emission, latch, locked=False, hv_enable=True, armed=True):
    il = (laser.IL_BUTTON_LATCH if latch else 0) | (laser.IL_LASER_LATCH if locked else 0)
    return {"emission": emission, "armed": armed, "il": il, "hv_enable": hv_enable}


class DwellGapTests(unittest.TestCase):
    def test_the_relocked_tail_is_not_judged(self):
        # the arm wait (unlocked, latch set until the press), fire, the
        # gap, fire, then the relock sets the latch while the counter
        # still reads nonzero
        trail = [smp(0, 1), smp(0, 0), smp(10, 0), smp(20, 0, hv_enable=False),
                 smp(20, 0, hv_enable=False), smp(30, 0), smp(40, 0),
                 smp(40, 1, locked=True, armed=False), smp(40, 1, locked=True, armed=False),
                 smp(0, 1, locked=True, armed=False)]
        g = laser.dwell_gap(trail)
        self.assertEqual(g["button_latch_unlocked_max"], 0)
        self.assertEqual(g["button_latch_unlocked_samples"], 5)
        self.assertEqual(g["button_latch_set_at"], [])
        self.assertTrue(g["hv_enable_dipped"])
        self.assertTrue(g["hv_enable_back_lit"])

    def test_latch_set_while_unlocked_is_the_defect(self):
        trail = [smp(0, 0), smp(10, 0), smp(20, 1, hv_enable=False), smp(30, 0), smp(0, 1, locked=True)]
        g = laser.dwell_gap(trail)
        self.assertEqual(g["button_latch_unlocked_max"], 1)
        self.assertEqual(g["button_latch_set_at"], [1])

    def test_no_emission_judges_nothing(self):
        g = laser.dwell_gap([smp(0, 0), smp(0, 1, locked=True)])
        self.assertIsNone(g["button_latch_unlocked_max"])
        self.assertEqual(g["button_latch_unlocked_samples"], 0)
        self.assertFalse(g["hv_enable_dipped"])


if __name__ == "__main__":
    unittest.main()
