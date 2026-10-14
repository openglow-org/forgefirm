# Copyright 2026 514 LLC d/b/a OpenGlow
# Written by Scott Wiederhold
# https://community.openglow.org
# SPDX-License-Identifier:    MIT

"""cooling.flow-under-load reads the engine's flow verdict from what the
forgectrl log gained since the job began: only the text after the offset,
never an older verdict before it, and the whole of a log rotated under
the test."""
import os
import shutil
import tempfile
import unittest

from forgetest.suite import cooling


class FlowVerdictLogTests(unittest.TestCase):
    """cooling.flow-under-load finds the engine's verdict in what the log
    gained since the job began, not in a count over a tail of fixed length:
    there a new verdict line comes in at the bottom as an old one leaves at
    the top, the count does not move, and a check that verified reads as
    one that never judged."""

    VERDICT = ("2026-09-19T21:37:46.869674+00:00 forgectrl[2796] INFO cool: coolant flow verified "
               "(heater rise 11.5 C, dT 9.5 C; laser 1.6 off 13.1)\n")
    OLD = ("2026-09-19T21:17:27.200840+00:00 forgectrl[965] INFO cool: coolant flow verified "
           "(heater rise 9.5 C, dT 8.7 C)\n")

    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="flowlog.")
        self.addCleanup(shutil.rmtree, self.dir, ignore_errors=True)
        self.path = os.path.join(self.dir, "forgectrl.log")

    def write(self, text, mode="w"):
        with open(self.path, mode, newline="\n") as f:
            f.write(text)

    def test_only_the_text_after_the_offset_comes_back(self):
        self.write(self.OLD + "filler\n" * 400)
        off = os.path.getsize(self.path)
        self.assertEqual(cooling._log_since(self.path, off), "")
        self.write(self.VERDICT, mode="a")
        text = cooling._log_since(self.path, off)
        self.assertEqual(text, self.VERDICT)
        m = cooling._load_verdict_rx().search(text)
        self.assertEqual(m.group(1), "coolant flow verified")
        self.assertEqual((m.group(2), m.group(5)), ("11.5", "1.6"))

    def test_an_old_verdict_before_the_offset_is_not_the_new_one(self):
        self.write(self.OLD)
        off = os.path.getsize(self.path)
        self.write("2026-09-19T21:36:40+00:00 forgectrl[2796] INFO cool: crash watch armed\n", mode="a")
        self.assertIsNone(cooling._load_verdict_rx().search(cooling._log_since(self.path, off)))

    def test_a_log_rotated_under_the_test_is_read_whole(self):
        self.write(self.OLD + "filler\n" * 400)
        off = os.path.getsize(self.path)
        self.write(self.VERDICT)                 # the rotation: a new, shorter file
        self.assertEqual(cooling._log_since(self.path, off), self.VERDICT)


if __name__ == "__main__":
    unittest.main()
