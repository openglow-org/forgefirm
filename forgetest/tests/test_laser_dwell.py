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
/status carried no laser block is a gap, never a dark sample, a tail the
daemon did not serve before the sampling stopped is a gap too, and a
stretch with a kernel window unread fails the rule; the sampling runs on
past the close; and the head's reports place the rapid inside the armed
window only."""
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


class RapidReadTests(unittest.TestCase):
    def test_every_sample_read_is_inside_the_rule(self):
        r = laser.read_gaps(rapid_trail(25), "emission")
        self.assertEqual((r["read"], r["unread"]), (25, 0))
        self.assertAlmostEqual(r["gap_max_s"], 0.2)
        self.assertTrue(covered(r))

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
        r = laser.read_gaps(rapid_trail(25, holes=(10, 11, 12, 13, 14)), "emission")
        self.assertAlmostEqual(r["gap_max_s"], 1.2)
        self.assertFalse(covered(r))

    def test_the_block_gone_after_the_dark_point_fails(self):
        r = laser.read_gaps(rapid_trail(25, holes=tuple(range(1, 25))), "emission")
        self.assertEqual((r["read"], r["unread"]), (1, 24))
        self.assertAlmostEqual(r["gap_max_s"], 4.8)
        self.assertFalse(covered(r))

    def test_the_block_gone_for_the_last_window_and_a_half_fails(self):
        # the tail is an edge: the window that latches after the close
        # goes unread
        r = laser.read_gaps(rapid_trail(25, holes=tuple(range(17, 25))), "emission")
        self.assertAlmostEqual(r["gap_max_s"], 1.6)
        self.assertFalse(covered(r))

    def test_a_tail_the_daemon_never_served_fails(self):
        # every sample served was read, and the served samples end at the
        # close: the sampling ran on past it with nothing served, so the
        # stretch from the last reading to where the sampling stopped is a gap
        trail = rapid_trail(25)
        tail = 2 * laser.EMISSION_WINDOW_S + laser.EMISSION_READ_GAP_S
        r = laser.read_gaps(trail, "emission", trail[-1]["t"] + tail)
        self.assertAlmostEqual(r["gap_max_s"], tail)
        self.assertFalse(covered(r))
        # served through: the sampling stops one loop after the last sample
        self.assertTrue(covered(laser.read_gaps(trail, "emission", trail[-1]["t"] + 0.125)))

    def test_a_stretch_of_one_sample_fails(self):
        r = laser.read_gaps(rapid_trail(1), "emission")
        self.assertEqual(r, {"read": 1, "unread": 0, "gap_max_s": 0.0})
        self.assertFalse(covered(r))


class Clock:
    """time.time and time.sleep on a clock the test moves."""

    def __init__(self, now=1000.0):
        self.now = now

    def time(self):
        return self.now

    def sleep(self, s):
        self.now += s


class RapidTailTests(unittest.TestCase):
    def test_the_sampling_runs_on_past_the_close_and_ends_where_it_stopped(self):
        # The window closes 3 s in, and the daemon serves the closed
        # sample and nothing after it: the sampling still runs two kernel
        # windows and the read gap allowed past the close, and the time it
        # stopped is the trail's end, so the tail it never served fails the
        # read rule.
        clock = Clock()
        close = clock.now + 3.0
        served = []

        def sample(_ctx):
            clock.now += 0.06                       # the two GETs
            if served and not served[-1]["armed"]:
                return None                         # nothing served after the close
            s = {"t": clock.now, "armed": clock.now < close, "emission": 0, "hv": 0}
            served.append(s)
            return s

        class Ctx:
            def checkpoint(self):
                pass

        class Grbl:
            def status_report(self):
                # the time of the read, as the head's X
                return {"state": "Idle", "MPos": (clock.now, 0.0, 0.0)}
        saved = laser.time, laser.sample
        self.addCleanup(setattr, laser, "time", saved[0])
        self.addCleanup(setattr, laser, "sample", saved[1])
        laser.time, laser.sample = clock, sample
        tail, t_end = laser.sample_to_disarm(Ctx(), Grbl())
        closed = tail[-1]
        self.assertFalse(closed["armed"])
        self.assertTrue(all(s["armed"] for s in tail[:-1]))
        self.assertGreaterEqual(t_end - closed["t"], 2 * laser.EMISSION_WINDOW_S + laser.EMISSION_READ_GAP_S)
        self.assertFalse(covered(laser.read_gaps(tail, "emission", t_end)))
        # each sample's X is read before its armed flag: an X that places
        # the head was read with the window open
        self.assertTrue(all(s["x"] < s["t"] for s in tail))


def place_trail(xs, closes_at):
    """A dark stretch with the head at machine X `xs` (one sample each,
    0.2 s apart), the window closing at sample `closes_at`."""
    return [{"t": 100.0 + i * 0.2, "x": float(x), "armed": i < closes_at} for i, x in enumerate(xs)]


class RapidPlaceTests(unittest.TestCase):
    # out to +20 mm, the dwell there, back at the start, the dwell before the M2
    XS = [0, 5, 20, 20, 20, 10, 0, 0, 0, 0]

    def test_a_window_closed_before_the_head_is_back_does_not_place_the_rapid(self):
        # the window closes after the head is back: out at 2, back at 6, closed at 8
        self.assertEqual(laser.rapid_place(place_trail(self.XS, 8), 0.0), (2, 6, 8))
        # the window closes in the dwell at +20 mm and the head comes back
        # after the close: the samples past the close do not place it
        self.assertEqual(laser.rapid_place(place_trail(self.XS, 4), 0.0), (2, None, 4))
        # the window reads closed at the first sample with the head back:
        # that sample does not tell whether the head was back before the
        # close, so it does not place it
        self.assertEqual(laser.rapid_place(place_trail(self.XS, 6), 0.0), (2, None, 6))


if __name__ == "__main__":
    unittest.main()
