# Copyright 2026 514 LLC d/b/a OpenGlow
# Written by Scott Wiederhold
# https://community.openglow.org
# SPDX-License-Identifier:    MIT

"""The bench registry against scripts/bench: every python tool is
registered (or named as not-a-tool), every registered script exists,
a ported tool builds a command line with its defaults, and every
script compiles."""
import glob
import os
import py_compile
import unittest

import helpers  # noqa: F401  (sys.path)
from forgetest import bench as bench_mod

REPO = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
BENCH = os.path.join(REPO, "scripts", "bench")


class RegistryTests(unittest.TestCase):
    def setUp(self):
        self.bench = bench_mod.Bench(tool_dir=BENCH, index_path=os.devnull)
        self.scripts = sorted(os.path.basename(p) for p in glob.glob(os.path.join(BENCH, "*.py")))

    def test_every_python_tool_is_registered(self):
        registered = {t["script"] for t in bench_mod.TOOLS}
        missing = [s for s in self.scripts if s not in registered and s not in bench_mod.NOT_TOOLS]
        self.assertEqual(missing, [], "scripts/bench python files missing from the registry")

    def test_every_registered_script_exists(self):
        for t in bench_mod.TOOLS:
            self.assertTrue(os.path.exists(os.path.join(BENCH, t["script"])), t["script"])

    def test_ids_unique_and_fields_valid(self):
        ids = [t["id"] for t in bench_mod.TOOLS]
        self.assertEqual(len(ids), len(set(ids)))
        for t in bench_mod.TOOLS:
            self.assertIn(t["safety"], ("dry", "takeover", "scope", "live"), t["id"])
            self.assertIn(t["where"], ("board", "host"), t["id"])
            self.assertIsInstance(t["ported"], bool, t["id"])
            self.assertTrue(t["desc"], t["id"])
            for a in t.get("args", []):
                self.assertIn(a["type"], ("str", "int", "float", "choice"), (t["id"], a["name"]))
                if a["type"] == "choice":
                    self.assertIn(a["default"], a["choices"], (t["id"], a["name"]))

    def test_ported_tools_build_a_command_with_defaults(self):
        for t in bench_mod.TOOLS:
            if not t["ported"]:
                continue
            ok, argv, err = self.bench.command(t, {})
            self.assertTrue(ok, "%s: %s" % (t["id"], err))
            self.assertEqual(argv[1], os.path.join(BENCH, t["script"]))

    def test_scripts_compile(self):
        for s in self.scripts:
            py_compile.compile(os.path.join(BENCH, s), doraise=True)


if __name__ == "__main__":
    unittest.main()
