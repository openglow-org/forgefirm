# Copyright 2026 514 LLC d/b/a OpenGlow
# Written by Scott Wiederhold
# https://community.openglow.org
# SPDX-License-Identifier:    MIT

"""The dark judge of laser.verdict-cut over synthetic trails. The judge is
a function inside the test's own body, so a change to it moves that test's
fingerprint and no other laser test's; it is reached here through the test
function's code object. A trail row is a status poll; its `fast` entry is
the burst of cnc/laser_on reads taken after that poll: [start, end, reads,
reads that saw the laser on, reads that failed]."""
import types
import unittest

from forgetest.suite import laser

DRAIN_S = 0.3


def dark_span():
    code = next(c for c in laser.verdict_cut.__code__.co_consts
                if isinstance(c, types.CodeType) and c.co_name == "dark_span")
    assert code.co_freevars == (), "the judge must not close over the test's variables"
    return types.FunctionType(code, vars(laser))


def row(t, gstate, on=None, n=350, bad=0):
    r = {"t": t, "gstate": gstate}
    if on is not None:
        r["fast"] = [round(t + 0.01, 3), round(t + 0.11, 3), n, on, bad]
    return r


class DarkSpanTests(unittest.TestCase):
    def setUp(self):
        self.judge = dark_span()

    def hold(self, lit_at=(), tail_lit=0):
        """Run, then a hold from 2.0 s (Hold:0) polled every 0.14 s with a
        burst after each poll, then the resume at 3.4 s. lit_at: the
        poll times whose burst saw the laser on; tail_lit: the burst
        after the last Hold poll, which the resume ends."""
        rows = [row(1.72, "Run"), row(1.86, "Run")]
        t = 2.0
        while t < 3.3:
            rows.append(row(round(t, 2), "Hold:0", on=5 if round(t, 2) in lit_at else 0))
            t += 0.14
        rows[-1]["fast"][3] = tail_lit
        rows.append(row(3.4, "Run"))
        return rows

    def test_a_dark_hold_is_judged_dark(self):
        d = self.judge(self.hold(), DRAIN_S)
        reads, on, bad, bursts, first, last = d
        self.assertEqual((on, bad), (0, 0))
        self.assertGreater(bursts, 0)
        self.assertEqual(reads, bursts * 350)
        self.assertGreaterEqual(first, 2.0 + DRAIN_S)
        self.assertLessEqual(last, 3.4)

    def test_the_lit_deceleration_inside_the_drain_is_not_judged(self):
        # the pause tier's first deceleration plays out of the kernel's
        # queue after Hold:0: lit bursts inside the drain are expected
        d = self.judge(self.hold(lit_at=(2.0, 2.14)), DRAIN_S)
        self.assertEqual(d[1], 0)

    def test_emission_after_the_drain_is_caught(self):
        # the negative control: a lit read inside the judged span is seen
        d = self.judge(self.hold(lit_at=(2.56,)), DRAIN_S)
        self.assertEqual(d[1], 5)

    def test_a_burst_the_resume_ends_is_not_judged(self):
        # the poll after it reads Run: the cut may have relit inside it
        d = self.judge(self.hold(tail_lit=40), DRAIN_S)
        self.assertEqual(d[1], 0)

    def test_failed_reads_are_counted(self):
        rows = self.hold()
        rows[5]["fast"][4] = 3
        self.assertEqual(self.judge(rows, DRAIN_S)[2], 3)

    def test_the_drain_counts_from_the_first_hold_complete(self):
        # Hold:1 (decelerating) rows do not start the span: a lit burst
        # after a Hold:1 poll but inside the drain of the first Hold:0 is
        # not judged
        rows = [row(1.8, "Run"), row(1.94, "Hold:1", on=30), row(2.08, "Hold:0", on=12),
                row(2.22, "Hold:0", on=0), row(2.36, "Hold:0", on=0), row(2.5, "Hold:0", on=0),
                row(2.64, "Hold:0", on=0), row(2.78, "Run")]
        reads, on, bad, bursts, first, last = self.judge(rows, DRAIN_S)
        self.assertEqual(on, 0)
        self.assertGreaterEqual(first, 2.08 + DRAIN_S)
        # 2.51 only: 2.37 is inside the drain, 2.65 is ended by the resume
        self.assertEqual(bursts, 1)

    def test_no_hold_complete_cannot_be_judged(self):
        self.assertIsNone(self.judge([row(1.0, "Run"), row(1.2, "Hold:1", on=0), row(1.4, "Run")], DRAIN_S))

    def test_a_hold_too_short_for_the_drain_leaves_nothing_to_judge(self):
        # the test then fails on too few reads, never passes on none
        rows = [row(1.8, "Run"), row(2.0, "Hold:0", on=0), row(2.14, "Hold:0", on=0), row(2.28, "Run")]
        self.assertEqual(self.judge(rows, DRAIN_S)[:4], [0, 0, 0, 0])


if __name__ == "__main__":
    unittest.main()
