# Copyright 2026 514 LLC d/b/a OpenGlow
# Written by Scott Wiederhold
# https://community.openglow.org
# SPDX-License-Identifier:    MIT

"""Every registered bench tool carries one of the four safety classes. The
runner asks for the operator's acknowledgment before a tool whose class
reads "live", and stops forgectrl around one whose class reads "takeover"
or "scope": a class outside the four would run a live tool with no
acknowledgment asked."""
import unittest

import helpers  # noqa: F401  (sys.path)
from forgetest import bench as bench_mod


class RegistryTests(unittest.TestCase):
    def test_every_tool_carries_a_safety_class(self):
        bad = [(t["id"], t.get("safety")) for t in bench_mod.TOOLS
               if t.get("safety") not in ("dry", "takeover", "scope", "live")]
        self.assertEqual(bad, [])


if __name__ == "__main__":
    unittest.main()
