# Copyright 2026 514 LLC d/b/a OpenGlow
# Written by Scott Wiederhold
# https://community.openglow.org
# SPDX-License-Identifier:    MIT

"""The bench registry against scripts/bench: every python tool is
registered (or named as not-a-tool), every registered script exists,
every argument spec builds a command line, every script compiles, and
the shared helper resolves host and local mode as documented."""
import glob
import os
import py_compile
import subprocess
import sys
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

    def test_flagged_args_build_option_pairs(self):
        tool = next(t for t in bench_mod.TOOLS if t["id"] == "resume-dark-lead")
        ok, argv, err = self.bench.command(tool, {"run": "live", "feed": 900})
        self.assertTrue(ok, err)
        self.assertIn("--run", argv)
        self.assertEqual(argv[argv.index("--run") + 1], "live")
        self.assertEqual(argv[argv.index("--feed") + 1], "900.0")
        # an empty optional with an empty default is left off entirely
        self.assertNotIn("--auto", argv)
        # a choice outside the list is refused before anything runs
        ok, _argv, err = self.bench.command(tool, {"run": "sideways"})
        self.assertFalse(ok)
        self.assertIn("run must be one of", err)

    def test_unported_tools_are_host_only(self):
        # what stays unported is what cannot run on the machine at all
        for t in bench_mod.TOOLS:
            if not t["ported"]:
                self.assertEqual(t["where"], "host", t["id"])
                self.assertIn("not a bench-page tool", t["desc"], t["id"])

    def test_scripts_compile(self):
        for s in self.scripts:
            py_compile.compile(os.path.join(BENCH, s), doraise=True)


class GfbenchTests(unittest.TestCase):
    """gfbench.py in a subprocess (module-level host resolution)."""

    def run_snippet(self, code, env_extra):
        env = dict(os.environ)
        env.pop("GF_HOST", None)
        env.pop("FORGETEST_BENCH_DATA", None)
        env.update(env_extra)
        r = subprocess.run([sys.executable, "-c", code], cwd=BENCH, env=env,
                           stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, timeout=30)
        return r.returncode, r.stdout.strip()

    def test_host_mode(self):
        rc, out = self.run_snippet("import gfbench; print(gfbench.HOST, gfbench.LOCAL, gfbench.SSH[0])",
                                   {"GF_HOST": "192.0.2.7", "GF_SSH": "fakessh -x"})
        self.assertEqual(rc, 0, out)
        self.assertEqual(out, "192.0.2.7 False fakessh")

    def test_local_when_named_local(self):
        rc, out = self.run_snippet("import gfbench; print(gfbench.HOST, gfbench.LOCAL); "
                                   "print(gfbench.board('echo hi').strip())",
                                   {"GF_HOST": "127.0.0.1"})
        self.assertEqual(rc, 0, out)
        self.assertEqual(out.splitlines(), ["127.0.0.1 True", "hi"])

    def test_refuses_without_a_machine(self):
        if os.path.isdir("/sys/glowforge"):
            self.skipTest("running on the machine")
        rc, out = self.run_snippet("import gfbench", {})
        self.assertNotEqual(rc, 0)
        self.assertIn("GF_HOST", out)

    def test_degc_and_data_dir(self):
        import tempfile
        d = tempfile.mkdtemp(prefix="forgetest-bench-")
        rc, out = self.run_snippet(
            "import gfbench, os; print(round(gfbench.degc(700), 2), gfbench.degc('x') != gfbench.degc('x'), "
            "os.path.dirname(gfbench.data_path('f.json')) == os.environ['FORGETEST_BENCH_DATA'])",
            {"GF_HOST": "127.0.0.1", "FORGETEST_BENCH_DATA": d})
        self.assertEqual(rc, 0, out)
        val, nan, indir = out.split()
        # a plausible coolant reading (700 counts is room temperature territory)
        self.assertTrue(15.0 < float(val) < 35.0, val)
        self.assertEqual(nan, "True")
        self.assertEqual(indir, "True")


if __name__ == "__main__":
    unittest.main()
