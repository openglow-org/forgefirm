# Copyright 2026 514 LLC d/b/a OpenGlow
# Written by Scott Wiederhold
# https://community.openglow.org
# SPDX-License-Identifier:    MIT

"""The dwell-gap verdict of laser.emission-witness over synthetic trails:
the button latch judged from the first emission through every sample
whose readback word shows the laser latch unlocked, both bits from that
word, so the relocked tail (where the relock itself sets the latch and
the emission counter still reads nonzero) is never judged. And its
read-gap verdict over the dark stretch after the M5: a sample whose
/status carried no laser block is a gap, never a dark sample, and a
stretch with a kernel window unread fails the rule."""
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


def rapid_trail(n, dt=0.2, holes=()):
    """A dark stretch of n samples dt seconds apart, the emission count
    and the HV current read on every sample but the ones in `holes`: a
    /status served without its laser block, which sample() records as
    None."""
    out = []
    for i in range(n):
        s = {"t": 100.0 + i * dt, "emission": 0, "hv": 0, "armed": True}
        if i in holes:
            s["emission"] = None
            s["hv"] = None
        out.append(s)
    return out


def covered(r):
    """The rule emission_witness applies to read_gaps' report."""
    return (r["read"] >= laser.RAPID_MIN_READS and r["gap_max_s"] is not None
            and r["gap_max_s"] <= laser.EMISSION_READ_GAP_S)


def relit(trail):
    """The samples that read emission: the relit judge alone, which an
    unread sample never reaches."""
    return [s for s in trail if s["emission"]]


class RapidReadTests(unittest.TestCase):
    def test_every_sample_read_is_inside_the_rule(self):
        trail = rapid_trail(25)
        r = laser.read_gaps(trail, "emission")
        self.assertEqual((r["read"], r["unread"]), (25, 0))
        self.assertAlmostEqual(r["gap_max_s"], 0.2)
        self.assertTrue(covered(r))
        self.assertEqual(relit(trail), [])

    def test_one_transient_miss_is_inside_the_rule(self):
        r = laser.read_gaps(rapid_trail(25, holes=(12,)), "emission")
        self.assertEqual((r["read"], r["unread"]), (24, 1))
        self.assertAlmostEqual(r["gap_max_s"], 0.4)
        self.assertTrue(covered(r))

    def test_a_hole_shorter_than_a_window_is_inside_the_rule(self):
        r = laser.read_gaps(rapid_trail(25, holes=(10, 11, 12)), "emission")
        self.assertAlmostEqual(r["gap_max_s"], 0.8)
        self.assertTrue(covered(r))

    def test_a_hole_longer_than_a_window_fails(self):
        trail = rapid_trail(25, holes=(10, 11, 12, 13, 14))
        r = laser.read_gaps(trail, "emission")
        self.assertAlmostEqual(r["gap_max_s"], 1.2)
        self.assertFalse(covered(r))
        self.assertEqual(relit(trail), [])

    def test_the_block_gone_after_the_dark_point_fails(self):
        trail = rapid_trail(25, holes=tuple(range(1, 25)))
        r = laser.read_gaps(trail, "emission")
        self.assertEqual((r["read"], r["unread"]), (1, 24))
        self.assertAlmostEqual(r["gap_max_s"], 4.8)
        self.assertFalse(covered(r))
        self.assertEqual(relit(trail), [])

    def test_the_block_gone_for_the_last_window_and_a_half_fails(self):
        # the tail is an edge: the window that latches after the close
        # goes unread
        trail = rapid_trail(25, holes=tuple(range(17, 25)))
        r = laser.read_gaps(trail, "emission")
        self.assertAlmostEqual(r["gap_max_s"], 1.6)
        self.assertFalse(covered(r))
        self.assertEqual(relit(trail), [])

    def test_a_stretch_of_one_sample_fails(self):
        r = laser.read_gaps(rapid_trail(1), "emission")
        self.assertEqual(r, {"read": 1, "unread": 0, "gap_max_s": 0.0})
        self.assertFalse(covered(r))


if __name__ == "__main__":
    unittest.main()
