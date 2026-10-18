# Copyright 2026 514 LLC d/b/a OpenGlow
# Written by Scott Wiederhold
# https://community.openglow.org
# SPDX-License-Identifier:    MIT

import os
import sys
import unittest

import helpers
from forgetest import manifest as m
from forgetest import catalog


class GlobTests(unittest.TestCase):
    def test_star_and_doublestar(self):
        rx = m.glob_to_regex("src/*.c")
        self.assertTrue(rx.match("src/main.c"))
        self.assertFalse(rx.match("src/sub/main.c"))
        rx = m.glob_to_regex("src/**")
        self.assertTrue(rx.match("src/main.c"))
        self.assertTrue(rx.match("src/sub/deep/x.h"))
        self.assertFalse(rx.match("docs/x"))
        rx = m.glob_to_regex("**/*.md")
        self.assertTrue(rx.match("README.md"))
        self.assertTrue(rx.match("docs/a/b.md"))
        self.assertFalse(rx.match("docs/a/b.txt"))
        rx = m.glob_to_regex("**")
        self.assertTrue(rx.match("anything/at/all"))
        rx = m.glob_to_regex("src/gfcool*")
        self.assertTrue(rx.match("src/gfcool_client.c"))
        self.assertFalse(rx.match("src/x/gfcool.c"))

    def test_question_mark_and_escaping(self):
        rx = m.glob_to_regex("a?c.d")
        self.assertTrue(rx.match("abc.d"))
        self.assertFalse(rx.match("abcxd"))
        self.assertFalse(rx.match("a/c.d"))


class FingerprintTests(unittest.TestCase):
    def setUp(self):
        self.man = helpers.make_manifest()

    def test_stable_and_order_independent(self):
        c1 = [("forgectrl", "src/ui.c"), ("forgectrl", "src/auth.c")]
        c2 = list(reversed(c1))
        self.assertEqual(m.fingerprint(self.man, c1), m.fingerprint(self.man, c2))
        self.assertEqual(m.fingerprint(self.man, c1), m.fingerprint(self.man, c1))

    def test_changes_only_when_covered_file_changes(self):
        covers = [("forgectrl", "src/ui.c")]
        base = m.fingerprint(self.man, covers)
        other = helpers.with_file(self.man, "forgectrl", "src/cool.c", "cool v2")
        self.assertEqual(base, m.fingerprint(other, covers), "an uncovered change must not move the fingerprint")
        changed = helpers.with_file(self.man, "forgectrl", "src/ui.c", "ui v2")
        self.assertNotEqual(base, m.fingerprint(changed, covers))
        added = helpers.with_file(self.man, "forgectrl", "src/ui_extra.c", "new")
        self.assertEqual(base, m.fingerprint(added, covers), "an unmatched new file does not move it")
        added2 = helpers.with_file(self.man, "forgectrl", "src/x.c", "new")
        self.assertNotEqual(base, m.fingerprint(added2, [("forgectrl", "src/*.c")]))

    def test_platform_is_always_in(self):
        covers = [("forgectrl", "src/ui.c")]
        base = m.fingerprint(self.man, covers)
        p2 = helpers.with_platform(self.man, dtb={"glowforge.dtb": "e" * 64})
        self.assertNotEqual(base, m.fingerprint(p2, covers))
        p3 = helpers.with_platform(self.man, layers={"meta-forgefirm": {"content_sha256": "2" * 64},
                                                     "poky": {"rev": "p" * 40}})
        self.assertNotEqual(base, m.fingerprint(p3, covers))

    def test_kernel_pseudo_files(self):
        covers = [("linux-fslc", "**")]
        base = m.fingerprint(self.man, covers)
        k2 = helpers.with_file(self.man, "linux-fslc", "@config", "cfg2")
        self.assertNotEqual(base, m.fingerprint(k2, covers))

    def test_missing_component_marker(self):
        f1 = m.fingerprint(self.man, [("nonexistent", "**")])
        f2 = m.fingerprint(self.man, [("forgectrl", "nothing-matches-*")])
        self.assertNotEqual(f1, f2)

    def test_dev_only_refused(self):
        with self.assertRaises(ValueError):
            m.fingerprint(self.man, [("forgetest", "**")])

    def test_extra_folds_in(self):
        covers = [("forgectrl", "src/ui.c")]
        self.assertNotEqual(m.fingerprint(self.man, covers, extra=["a"]),
                            m.fingerprint(self.man, covers, extra=["b"]))

    def test_submodule_gitlink_and_files(self):
        covers = [("grblhal-glowforge", "src/grbl/**")]
        base = m.fingerprint(self.man, covers)
        core = helpers.with_file(self.man, "grblhal-glowforge", "src/grbl/core.c", "core v2")
        self.assertNotEqual(base, m.fingerprint(core, covers))
        link = [("grblhal-glowforge", "src/grbl")]
        self.assertEqual(m.fingerprint(self.man, link), m.fingerprint(core, link),
                         "the gitlink alone does not see a file-level change (fixture keeps the link id)")

    def test_identity_sha_ignores_dev_only(self):
        a = self.man.identity_sha()
        b = helpers.with_file(self.man, "forgetest", "forgetest/x.py", "changed").identity_sha()
        self.assertEqual(a, b)
        c = helpers.with_file(self.man, "forgectrl", "src/ui.c", "changed").identity_sha()
        self.assertNotEqual(a, c)


class CoverageReportTests(unittest.TestCase):
    def test_report(self):
        man = helpers.make_manifest()
        t1 = helpers.make_test("a.one", [("forgectrl", "src/ui.c"), ("forgectrl", "src/auth.c")])
        t2 = helpers.make_test("a.two", [("grblhal-glowforge", "**"), ("kernel-module-glowforge", "**"),
                                         ("linux-fslc", "**")])
        rep = m.coverage_report(man, [t1, t2], allow=[("*", "**/*.md")])
        self.assertEqual(set(rep), {"forgectrl"})
        self.assertEqual(rep["forgectrl"], ["src/cool.c", "src/main.c"])
        rep2 = m.coverage_report(man, [t1, t2, helpers.make_test("a.three", [("forgectrl", "src/**")])],
                                 allow=[("*", "**/*.md")])
        self.assertEqual(rep2, {})
        self.assertNotIn("forgetest", m.coverage_report(man, [], allow=[]),
                         "dev-only components are outside the report")

    def test_behavioral_paths_are_never_allowed_away(self):
        # An advisory document is behavior (the consent is to its hash): the allowlist's docs/** and
        # **/*.md do not take it out of the report, as they do not take it out of a fingerprint. No
        # host-tested rows here: the real table's rows name the first-run documents.
        man = helpers.with_file(helpers.make_manifest(), "forgectrl", "docs/advisories/privacy.md", "text")
        rest = helpers.make_test("a.one", [("forgectrl", "src/**"), ("grblhal-glowforge", "**"),
                                           ("kernel-module-glowforge", "**"), ("linux-fslc", "**")])
        self.assertEqual(m.coverage_report(man, [rest], host=()), {"forgectrl": ["docs/advisories/privacy.md"]})
        doc = helpers.make_test("a.two", [("forgectrl", "docs/advisories/privacy.md")])
        self.assertEqual(m.coverage_report(man, [rest, doc], host=()), {})
        self.assertNotEqual(m.fingerprint(man, doc.covers),
                            m.fingerprint(helpers.with_file(man, "forgectrl", "docs/advisories/privacy.md", "new"),
                                          doc.covers))

    def test_non_behavioral_paths_are_outside_every_fingerprint(self):
        man = helpers.make_manifest()
        t = helpers.make_test("a.one", [("forgectrl", "**")])
        a = m.fingerprint(man, t.covers)
        for comp, path in (("forgectrl", "README.md"), ("forgectrl", "docs/SERVICES.md"),
                           ("forgectrl", "tests/test_x.py"), ("forgectrl", ".github/workflows/ci.yml")):
            self.assertEqual(a, m.fingerprint(helpers.with_file(man, comp, path, "changed"), t.covers),
                             path)
        self.assertNotEqual(a, m.fingerprint(helpers.with_file(man, "forgectrl", "src/ui.c", "changed"),
                                             t.covers))
        self.assertTrue(m.non_behavioral("forgectrl", "tools/devserver.py"))
        self.assertFalse(m.non_behavioral("grblhal-glowforge", "tools/devserver.py"))
        # forgeext's image content is its binary and init script: the kit, the template, the host tools
        # and the shared package workflow are not, and the host's sources are
        for path in ("sdk/js/ffx-bridge.js", "template/manifest.json", "tools/ffx", ".github/actions/kit/action.yml"):
            self.assertTrue(m.non_behavioral("forgeext", path), path)
        for path in ("src/api.c", "init/forgeext.init", "CMakeLists.txt"):
            self.assertFalse(m.non_behavioral("forgeext", path), path)
        # an advisory is a document forgectrl serves and records consent to by its hash: behavior, though
        # it is Markdown under docs/
        self.assertFalse(m.non_behavioral("forgectrl", "docs/advisories/extensions.md"))
        self.assertTrue(m.non_behavioral("forgectrl", "docs/SERVICES.md"))
        self.assertTrue(m.non_behavioral("grblhal-glowforge", "docs/advisories/x.md"))

    def test_an_entry_that_selects_nothing_is_reported(self):
        man = helpers.make_manifest()
        good = helpers.make_test("a.one", [("forgectrl", "src/ui.c")])
        # the recipe's subdirectory left out of the glob, and an unknown component
        bad = helpers.make_test("a.two", [("forgectrl", "ui.c"), ("no-such", "**")])
        self.assertEqual(m.empty_covers(man, [good]), [])
        self.assertEqual(m.empty_covers(man, [good, bad]),
                         [("a.two", "forgectrl", "ui.c"), ("a.two", "no-such", "**")])
        # a glob that selects docs only names nothing any fingerprint carries
        docs = helpers.make_test("a.three", [("forgectrl", "**/*.md")])
        self.assertEqual(m.empty_covers(man, [docs]), [("a.three", "forgectrl", "**/*.md")])

    def test_a_host_tested_path_is_covered_and_in_no_fingerprint(self):
        man = helpers.with_file(helpers.make_manifest(), "forgectrl", "tests/cool_test.c", "test")
        t = helpers.make_test("a.one", [("forgectrl", "src/ui.c"), ("forgectrl", "src/auth.c"),
                                        ("grblhal-glowforge", "**"), ("kernel-module-glowforge", "**"),
                                        ("linux-fslc", "**")])
        self.assertEqual(m.coverage_report(man, [t], host=[]), {"forgectrl": ["src/cool.c", "src/main.c"]})
        ci = "forgectrl:.github/workflows/build.yml"
        host = [("forgectrl", "src/cool.c", "forgectrl:tests/cool_test.c", ci),
                ("forgectrl", "src/main.c", "forgectrl:tests/cool_test.c", ci)]
        self.assertEqual(m.coverage_report(man, [t], host=host), {})
        # a change to a host-tested path makes no acceptance run necessary
        self.assertEqual(m.fingerprint(man, t.covers),
                         m.fingerprint(helpers.with_file(man, "forgectrl", "src/cool.c", "changed"), t.covers))

    def _root(self, files):
        """A scratch checkout of this repository: {path: text}."""
        import shutil
        import tempfile
        root = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, root)
        for path, text in files.items():
            os.makedirs(os.path.dirname(os.path.join(root, path)), exist_ok=True)
            with open(os.path.join(root, path), "w", encoding="utf-8") as f:
                f.write(text)
        return root

    # A workflow of this repository as the scratch checkouts carry it: on
    # every push, and on the pull requests that touch one bench tool, it
    # checks itself out at the workspace root and runs the one harness.
    CI_YML = ("on:\n  push:\n  pull_request:\n    paths:\n      - 'scripts/bench/drill.py'\n"
              "jobs:\n  test:\n    steps:\n"
              "      - uses: actions/checkout@v4\n"
              "      # drill.py is a bench tool\n"
              "      - name: The stream\n"
              "        run: python3 scripts/bench/stream_test.py  # not drill.py\n")

    def test_a_host_tested_row_selects_a_path_and_names_a_real_test_and_workflow(self):
        man = helpers.make_manifest()
        for path in ("tests/cool_test.c", ".github/workflows/build.yml"):
            man = helpers.with_file(man, "forgectrl", path, path)
        root = self._root({"scripts/bench/stream_test.py": "", ".github/workflows/ci.yml": self.CI_YML})
        ci = "forgectrl:.github/workflows/build.yml"
        good = [("forgectrl", "src/cool.c", "forgectrl:tests/cool_test.c", ci),
                ("grblhal-glowforge", "src/driver.c", "forgefirm:scripts/bench/stream_test.py",
                 "forgefirm:.github/workflows/ci.yml")]
        self.assertEqual(m.host_row_problems(man, good, root=root, sources=None), [])
        bad = [("forgectrl", "cool.c", "forgectrl:tests/cool_test.c", ci),       # globs anchor at the root
               ("forgectrl", "**/*.md", "forgectrl:tests/cool_test.c", ci),      # docs only
               ("forgectrl", "src/main.c", "forgectrl:tests/gone_test.c", ci),
               ("forgectrl", "src/main.c", "tests/cool_test.c", ci),
               ("forgectrl", "src/main.c", "forgetest:forgetest/x.py", ci),     # the acceptance tool itself
               ("forgectrl", "src/main.c", "forgefirm:scripts/bench/gone_test.py", ci),
               ("forgectrl", "src/main.c", "forgectrl:tests/cool_test.c", "forgectrl:.github/workflows/gone.yml"),
               ("forgectrl", "src/main.c", "forgectrl:tests/cool_test.c", "build.yml")]
        self.assertEqual([r[3] for r in m.host_row_problems(man, bad, root=root, sources=None)],
                         ["selects nothing", "selects nothing", "no such host test",
                          "names no repository:path for its host test", "no repository 'forgetest' for its host test",
                          "no such host test", "no such workflow", "names no repository:path for its workflow"])
        # installed on an image the package is in no checkout: a "forgefirm:" file is not looked for
        self.assertEqual(m.host_row_problems(man, bad[5:6], root=None, sources=None), [])

    def test_the_workflow_has_to_run_the_host_test(self):
        man = helpers.make_manifest()
        root = self._root({"scripts/bench/stream_test.py": "", "scripts/bench/drill.py": "",
                           ".github/workflows/ci.yml": self.CI_YML})
        ci = "forgefirm:.github/workflows/ci.yml"
        runs = ("grblhal-glowforge", "src/driver.c", "forgefirm:scripts/bench/stream_test.py", ci)
        # drill.py is in the trigger's paths, a comment line and an inline comment, and run by no step
        idle = ("grblhal-glowforge", "src/driver.c", "forgefirm:scripts/bench/drill.py", ci)
        self.assertEqual(m.host_row_problems(man, [runs], root=root, sources=None), [])
        self.assertEqual(m.host_row_problems(man, [idle], root=root, sources=None),
                         [(idle[0], idle[1], idle[2], ci + " does not run it")])
        # a workflow whose own on: names neither push nor pull_request is listed, and a step whose
        # failure the run tolerates does not run it
        job = "jobs:\n  test:\n    steps:\n      - uses: actions/checkout@v4\n      - run: python3 scripts/bench/stream_test.py\n"
        root = self._root({"scripts/bench/stream_test.py": "",
                           ".github/workflows/manual.yml": "on:\n  workflow_dispatch:\n" + job,
                           ".github/workflows/soft.yml": "on: [push]\n" + job.replace(
                               "      - run:", "      - continue-on-error: true\n        run:")})
        manual = runs[:3] + ("forgefirm:.github/workflows/manual.yml",)
        soft = runs[:3] + ("forgefirm:.github/workflows/soft.yml",)
        self.assertEqual(m.host_row_problems(man, [manual, soft], root=root, sources=None),
                         [manual[:3] + (manual[3] + ": the lint reads no push or pull_request in its own on:",),
                          soft[:3] + (soft[3] + " does not run it",)])
        # a step in a job that needs a conditional job does not run it (the checkout in a job of
        # its own, as the lint reads a checkout for the whole workflow)
        root = self._root({"scripts/bench/stream_test.py": "",
                           ".github/workflows/needs.yml": "on: [push]\njobs:\n  lint:\n    steps:\n"
                           "      - uses: actions/checkout@v4\n  build:\n    if: false\n    steps:\n"
                           "      - run: make\n  test:\n    needs: build\n    steps:\n"
                           "      - run: python3 scripts/bench/stream_test.py\n"})
        needs = runs[:3] + ("forgefirm:.github/workflows/needs.yml",)
        self.assertEqual(m.host_row_problems(man, [needs], root=root, sources=None),
                         [needs[:3] + (needs[3] + " does not run it",)])

    def _runs(self, yml, path):
        return m.workflow_runs(m.workflow_steps(yml), path)

    def _block(self, *lines, keys=""):
        """A one-step workflow whose run text is these lines, the step's other keys before it."""
        return "steps:\n  - name: x\n%s    run: |\n%s" % (keys, "".join("      %s\n" % ln for ln in lines))

    def test_what_running_a_host_test_looks_like(self):
        step = "steps:\n  - name: x\n    run: %s\n"
        self.assertTrue(self._runs(step % "./build/tray_test", "tests/tray_test.c"))
        self.assertTrue(self._runs(step % "sudo ./build/sandbox_test", "tests/sandbox_test.c"))
        self.assertTrue(self._runs(step % "python3 -B tests/run_test.py", "tests/run_test.py"))
        self.assertTrue(self._runs(step % "python3 forgefirm/scripts/bench/ctlport_test.py build/x",
                                   "forgefirm/scripts/bench/ctlport_test.py"))
        self.assertTrue(self._runs(step % "python -m pytest -q tests", "tests/test_frame.py"))
        # a block, with the step's working-directory: the words are its paths (the loop as
        # python3-gfhardware's CI writes it, an expansion in an assignment)
        block = ("steps:\n  - name: every module\n    working-directory: %s\n    run: |\n"
                 "      set -e\n      for t in tests/test_*.py; do\n        m=$(basename \"$t\" .py)\n"
                 "        python -m unittest -v \"tests.$m\"\n"
                 "      done\n  - working-directory: forgefirm/forgetest\n"
                 "    run: python -B -m unittest discover -s tests -v\n")
        self.assertTrue(self._runs(block % "gfh", "gfh/tests/test_feeder.py"))
        self.assertTrue(self._runs(block % "gfh", "forgefirm/forgetest/tests/test_x.py"))
        self.assertFalse(self._runs(block % "other", "gfh/tests/test_feeder.py"))
        # what the review found accepted: a trigger's paths, an inline comment, a glob or a discover
        # that the path only ends like
        self.assertFalse(self._runs("on:\n  push:\n    paths:\n      - 'kas/source-bundle.yml'\n"
                                    + step % "python3 x.py", "kas/source-bundle.yml"))
        self.assertFalse(self._runs(step % "./build/lens_test  # tray_test is manual", "tests/tray_test.c"))
        self.assertFalse(self._runs("steps:\n  - run: |\n      for t in tests/test_*.py; do\n        python $t\n"
                                    "      done\n", "tools/tests/test_feeder.py"))
        self.assertFalse(self._runs(step % "python -m unittest discover -s tests",
                                    "scripts/bench/tests/foo_test.py"))
        # a longer name, another file, a step's name, a comment line
        self.assertFalse(self._runs(step % "./build/tray_test_more", "tests/tray_test.c"))
        self.assertFalse(self._runs(step % "./build/lens_test", "tests/fflog_e2e.sh"))
        self.assertFalse(self._runs("steps:\n  - name: tests/run_test.py\n    run: make\n", "tests/run_test.py"))
        self.assertFalse(self._runs("steps:\n  - run: |\n      # python3 tests/run_test.py\n      make\n",
                                    "tests/run_test.py"))
        # the second review: an argument to a command that is not the test does not run it, and a
        # discover or pytest runs its own directory's matching files only
        self.assertFalse(self._runs(step % "echo tray_test", "tests/tray_test.c"))
        self.assertFalse(self._runs(step % "echo ./build/tray_test", "tests/tray_test.c"))
        self.assertFalse(self._runs(step % "command -v ./build/tray_test", "tests/tray_test.c"))   # prints it
        self.assertFalse(self._runs(step % "cmake --build build --target tray_test", "tests/tray_test.c"))
        self.assertFalse(self._runs(step % "cat tests/run_test.py", "tests/run_test.py"))
        self.assertFalse(self._runs(step % "python -m unittest discover -s forgefirm",
                                    "forgefirm/scripts/bench/fire_test.py"))
        self.assertFalse(self._runs(step % "python -m unittest discover -s tests", "tests/foo_test.py"))
        self.assertFalse(self._runs(step % "python -m pytest forgefirm", "forgefirm/tests/test_a.py"))
        self.assertTrue(self._runs(step % "python -m unittest discover -s tests -p '*_test.py'", "tests/foo_test.py"))
        self.assertTrue(self._runs(step % "pytest -q tests/test_a.py", "tests/test_a.py"))
        # a script run directly or by its shell, and a command continued across lines
        self.assertTrue(self._runs(step % "./tests/fflog_e2e.sh", "tests/fflog_e2e.sh"))
        self.assertTrue(self._runs(step % "bash tests/fflog_e2e.sh", "tests/fflog_e2e.sh"))
        self.assertTrue(self._runs("steps:\n  - run: |\n      sudo -E FORGEEXT=build/forgeext \\\n"
                                   "        python3 -B tests/run_test.py\n", "tests/run_test.py"))
        # python -m unittest given the test's file or its module (the third review: forgectrl's CI
        # runs its mock test as a file), and not a module beside it or a -k pattern
        self.assertTrue(self._runs(step % "python3 -B -m unittest -v tests/test_devserver_mock.py",
                                   "tests/test_devserver_mock.py"))
        self.assertTrue(self._runs(step % "python -m unittest tests.test_feeder", "tests/test_feeder.py"))
        self.assertTrue(self._runs(step % "python -m unittest tests.test_feeder.FeederTests.test_one",
                                   "tests/test_feeder.py"))
        self.assertFalse(self._runs(step % "python -m unittest tests.test_lens", "tests/test_feeder.py"))
        self.assertFalse(self._runs(step % "python -m unittest -k test_feeder tests.test_lens",
                                    "tests/test_feeder.py"))

    def test_a_step_counts_only_when_every_run_of_the_workflow_runs_it_and_its_failure_fails_the_run(self):
        step = "steps:\n  - name: x\n%s    run: ./build/tray_test\n"
        test = "tests/tray_test.c"
        # the lint evaluates no condition: an if: other than the literal true counts as no step,
        # always() included; literally true is no condition
        self.assertFalse(self._runs(step % "    if: false\n", test))
        self.assertFalse(self._runs(step % "    if: github.event_name == 'push'\n", test))
        self.assertFalse(self._runs(step % "    if: ${{ always() }}\n", test))
        self.assertTrue(self._runs(step % "    if: true\n", test))
        self.assertTrue(self._runs(step % "    if: 'true'\n", test))
        # however the step is spelled: more than one space after its dash, or a quoted key
        self.assertFalse(self._runs("steps:\n  -   run: ./build/tray_test\n      if: false\n", test))
        self.assertFalse(self._runs(step % "    \"if\": false\n", test))
        # a key with one stray quote is no key the lint reads: at a step's key (its first one on
        # its dash line too), a job's key or the top level, no step of the workflow counts
        self.assertFalse(self._runs(step % "    if\": false\n", test))
        self.assertFalse(self._runs("steps:\n  - name: x\n    run\": ./build/tray_test\n", test))
        self.assertFalse(self._runs("steps:\n  - run\": make\n  - run: ./build/tray_test\n", test))
        self.assertFalse(self._runs("jobs:\n  t:\n    if\": false\n    steps:\n      - run: ./build/tray_test\n",
                                    test))
        self.assertFalse(self._runs("jobs\":\n  t:\n    if: false\n    steps:\n      - run: ./build/tray_test\n",
                                    test))
        # a step whose failure the run tolerates proves nothing; literally false is no tolerance
        self.assertFalse(self._runs(step % "    continue-on-error: true\n", test))
        self.assertFalse(self._runs(step % "    continue-on-error: ${{ inputs.soft }}\n", test))
        self.assertTrue(self._runs(step % "    continue-on-error: false\n", test))
        # the job's own if: or continue-on-error: holds for each of its steps, wherever it is
        # written, over a step's own, and not for another job's steps
        job = "jobs:\n  t:\n%s    steps:\n      - run: ./build/tray_test\n%s"
        self.assertFalse(self._runs(job % ("    if: github.ref == 'refs/heads/main'\n", ""), test))
        self.assertFalse(self._runs(job % ("", "    if: false\n"), test))
        self.assertFalse(self._runs(job % ("    continue-on-error: true\n", ""), test))
        self.assertFalse(self._runs("jobs:\n  t:\n    continue-on-error: true\n    steps:\n"
                                    "      - continue-on-error: false\n        run: ./build/tray_test\n", test))
        self.assertTrue(self._runs(job % ("    if: true\n    runs-on: ubuntu-latest\n", ""), test))
        self.assertTrue(self._runs(job % ("    permissions:\n      contents: read\n", ""), test))
        two = "jobs:\n  a:\n    if: false\n    steps:\n      - run: %s\n  b:\n    steps:\n      - run: %s\n"
        self.assertTrue(self._runs(two % ("make", "./build/tray_test"), test))
        self.assertFalse(self._runs(two % ("./build/tray_test", "make"), test))
        # the run text is read as bash -e or sh -e runs it: the test counts on a line that holds it
        # alone, or in a pipeline under pipefail; any other operator on the line, or a pipe with no
        # pipefail, keeps it from counting
        for line in ("./build/tray_test || true", "true || ./build/tray_test", "./build/tray_test; true",
                     "./build/tray_test &", "./build/tray_test | tee log"):
            self.assertFalse(self._runs(self._block(line), test), line)
        self.assertTrue(self._runs(self._block("set -o pipefail", "./build/tray_test | tee log"), test))
        self.assertTrue(self._runs(self._block("./build/tray_test | tee log", keys="    shell: bash\n"), test))
        # no line counts after one that ends the step, turns errexit off, sets an option the lint
        # does not follow (noexec), or opens a here-document or a compound command
        for before in ("exit 0", "set +e", "set -n", "python3 - <<'EOF'"):
            self.assertFalse(self._runs(self._block(before, "./build/tray_test"), test), before)
        self.assertFalse(self._runs(self._block("{", "./build/tray_test", "} || true"), test))
        # a loop counts only with a body whose lines count
        for body in ("python $t || true", "python $t | tee log"):
            self.assertFalse(self._runs(self._block("for t in tests/test_*.py; do", body, "done"), "tests/test_x.py"),
                             body)
        # a folded block, and a scalar continued under its key, are one line to the shell
        self.assertFalse(self._runs("steps:\n  - run: >\n      ./build/tray_test\n      || true\n", test))
        self.assertFalse(self._runs("steps:\n  - run: ./build/tray_test\n      || true\n", test))
        # the lint reads the text of bash and sh alone: the step's shell, or its job's or its
        # workflow's default
        self.assertFalse(self._runs(self._block("./build/tray_test", keys="    shell: bash {0}\n"), test))
        self.assertTrue(self._runs(self._block("./build/tray_test", keys="    shell: sh\n"), test))
        self.assertFalse(self._runs("jobs:\n  t:\n    defaults:\n      run:\n        shell: pwsh\n    steps:\n"
                                    "      - run: ./build/tray_test\n", test))
        self.assertFalse(self._runs("defaults:\n  run:\n    shell: pwsh\njobs:\n  t:\n    steps:\n"
                                    "      - run: ./build/tray_test\n", test))
        # a checkout under a condition is no checkout
        yml = "steps:\n  - uses: actions/checkout@v4\n%s    with:\n      path: own\n"
        self.assertEqual(m.checkout_path(m.workflow_steps(yml % ""), "x", True), "own")
        self.assertIsNone(m.checkout_path(m.workflow_steps(yml % "    if: false\n"), "x", True))

    def test_a_job_runs_only_when_every_job_it_needs_runs(self):
        test = "tests/tray_test.c"
        # GitHub skips a job whose needed job did not run: a job that needs a conditional job runs
        # on not every run, however the needs: is written
        yml = "jobs:\n  build:\n%s    steps:\n      - run: make\n  test:\n%s    steps:\n      - run: ./build/tray_test\n"
        for needs in ("    needs: build\n", "    needs: [build]\n", "    needs: ['build']\n",
                      "    needs:\n      - build\n", "    needs:\n    - build\n",
                      "    needs: [\n      build\n    ]\n"):
            self.assertTrue(self._runs(yml % ("", needs), test), needs)
            self.assertFalse(self._runs(yml % ("    if: false\n", needs), test), needs)
            self.assertFalse(self._runs(yml % ("    if: github.event_name == 'push'\n", needs), test), needs)
        # the needed job's continue-on-error: does not bear: its failure fails neither the run nor
        # the jobs after it
        self.assertTrue(self._runs(yml % ("    continue-on-error: true\n", "    needs: build\n"), test))
        # transitively; a job the workflow does not have, a cycle, and a needs: the lint does not
        # read each fail closed
        chain = "jobs:\n  a:\n%s    steps:\n      - run: make\n  b:\n    needs: a\n    steps:\n      - run: make\n" \
                "  c:\n    needs: [b]\n    steps:\n      - run: ./build/tray_test\n"
        self.assertTrue(self._runs(chain % "", test))
        self.assertFalse(self._runs(chain % "    if: false\n", test))
        self.assertFalse(self._runs(yml % ("", "    needs: lint\n"), test))
        self.assertFalse(self._runs(yml % ("    needs: test\n", "    needs: build\n"), test))
        self.assertFalse(self._runs(yml % ("", "    needs: test\n"), test))
        self.assertFalse(self._runs(yml % ("", "    needs:\n      build: true\n"), test))
        # a flow list split after a comma keeps the name on its second line: a conditional job
        # there holds the dependent job back
        split = (chain % "").replace("    needs: [b]\n", "    needs: [a,\n      b]\n")
        self.assertTrue(self._runs(split, test))
        self.assertFalse(self._runs(split.replace("  b:\n    needs: a\n", "  b:\n    needs: a\n    if: false\n"), test))
        # an empty needs: (which GitHub refuses), a flow list a key interrupts before its ], and
        # list items at two indentations each fail closed
        for needs in ("    needs: []\n", "    needs:\n", "    needs: [\n      build\n",
                      "    needs:\n      - build\n    - build\n"):
            self.assertFalse(self._runs(yml % ("", needs), test), needs)
        # through a row: the test job needs, in a block list at the key's own indentation, a
        # build job under if: false (the checkout in a job of its own, as the lint reads a
        # checkout for the whole workflow)
        man = helpers.make_manifest()
        root = self._root({"scripts/bench/stream_test.py": "",
                           ".github/workflows/needs.yml": "on: [push]\njobs:\n  lint:\n    steps:\n"
                           "      - uses: actions/checkout@v4\n  build:\n    if: false\n    steps:\n"
                           "      - run: make\n  test:\n    needs:\n    - build\n    steps:\n"
                           "      - run: python3 scripts/bench/stream_test.py\n"})
        row = ("grblhal-glowforge", "src/driver.c", "forgefirm:scripts/bench/stream_test.py",
               "forgefirm:.github/workflows/needs.yml")
        self.assertEqual(m.host_row_problems(man, [row], root=root, sources=None),
                         [row[:3] + (row[3] + " does not run it",)])
        # a job under always() runs on every run, and the lint evaluates no condition: it counts as
        # no job for its own steps and for the jobs after it
        self.assertFalse(self._runs(yml % ("    if: false\n", "    needs: build\n    if: always()\n"), test))
        self.assertFalse(self._runs((chain % "    if: false\n").replace(
            "  b:\n    needs: a\n", "  b:\n    needs: a\n    if: ${{ always() }}\n"), test))

    def test_the_events_a_workflow_runs_on(self):
        self.assertEqual(m.workflow_triggers("on:\n  push:\n    branches: [main]\n  pull_request:\njobs:\n  t:\n"),
                         {"push", "pull_request"})
        self.assertEqual(m.workflow_triggers("on: [push, pull_request]\n"), {"push", "pull_request"})
        self.assertEqual(m.workflow_triggers("on: push\n"), {"push"})
        self.assertEqual(m.workflow_triggers("on:\n  - push\n  - workflow_dispatch\n"), {"push", "workflow_dispatch"})
        self.assertEqual(m.workflow_triggers("\"on\":\n  workflow_dispatch:\n    inputs:\n      push:\n"
                                             "        type: string\n"), {"workflow_dispatch"})
        self.assertEqual(m.workflow_triggers("on:\n  workflow_call:\n"), {"workflow_call"})
        self.assertEqual(m.workflow_triggers("name: x\njobs:\n  t:\n    steps: []\n"), set())
        # an event key with one stray quote is no event, and the workflow then names none
        self.assertEqual(m.workflow_triggers("on:\n  push\":\n"), set())
        self.assertEqual(m.workflow_triggers("on:\n  push:\n  'pull_request:\n"), set())

    def test_a_push_or_pull_request_under_a_filter_runs_on_not_every_change(self):
        f = m.workflow_filters
        self.assertEqual(f("on:\n  push:\n    branches: [main]\n  pull_request:\njobs:\n  t:\n"), {"push": ["branches"]})
        self.assertEqual(f("on:\n  push:\n    branches-ignore:\n      - wip/*\n    paths-ignore:\n      - 'docs/**'\n"
                           "  pull_request:\n    paths:\n      - 'src/**'\n"),
                         {"push": ["branches-ignore", "paths-ignore"], "pull_request": ["paths"]})
        self.assertEqual(f("on:\n  push:\n    tags: ['v*']\n"), {"push": ["tags"]})
        self.assertEqual(f("on:\n  push:\n    tags-ignore: ['v*']\n"), {"push": ["tags-ignore"]})
        # a pull_request under types: runs on the activity types it names and no other; the lint
        # reads no type, so any types: is a filter, on the pull_request alone
        self.assertEqual(f("on:\n  push:\n  pull_request:\n    types: [closed]\n"), {"pull_request": ["types"]})
        self.assertEqual(f("on:\n  pull_request:\n    types:\n      - opened\n      - synchronize\n    paths-ignore:\n"
                           "      - '**.md'\n"), {"pull_request": ["types", "paths-ignore"]})
        self.assertEqual(f("on:\n  push:\n    types: [x]\n"), {})
        # a value on the event's line, and a key with one stray quote under it, are filters the
        # lint does not read
        self.assertEqual(f("on:\n  push: {branches: [main]}\n"), {"push": ["a value the lint does not read"]})
        self.assertEqual(f("on:\n  push:\n    paths\":\n      - x\n"), {"push": ["a key the lint does not read"]})
        # a filter is read at the event's own level: a dispatch's inputs are none, and an on: list
        # or scalar carries none
        self.assertEqual(f("on:\n  workflow_dispatch:\n    inputs:\n      paths:\n        type: string\n  push:\n"),
                         {})
        self.assertEqual(f("on: [push, pull_request]\n"), {})
        self.assertEqual(f("on: push\n"), {})
        self.assertEqual(f("on:\n  - push\n"), {})
        # a row's workflow counts when its push or its pull_request carries no filter: a push to
        # the default branch alone with every pull request does, a push with paths-ignore alone
        # does not, nor do a push and a pull request each under a filter; every push with a pull
        # request under types: does, a pull request under types: alone does not
        man = helpers.make_manifest()
        job = "jobs:\n  test:\n    steps:\n      - uses: actions/checkout@v4\n      - run: python3 scripts/bench/stream_test.py\n"
        root = self._root({"scripts/bench/stream_test.py": "",
                           ".github/workflows/main.yml": "on:\n  push:\n    branches: [main]\n  pull_request:\n" + job,
                           ".github/workflows/docs.yml": "on:\n  push:\n    paths-ignore:\n      - 'docs/**'\n" + job,
                           ".github/workflows/both.yml": "on:\n  push:\n    branches: [main]\n  pull_request:\n"
                           "    paths:\n      - 'src/**'\n" + job,
                           ".github/workflows/push.yml": "on:\n  push:\n  pull_request:\n    types: [closed]\n" + job,
                           ".github/workflows/closed.yml": "on:\n  pull_request:\n    types: [closed]\n" + job})
        rows = [("grblhal-glowforge", "src/driver.c", "forgefirm:scripts/bench/stream_test.py",
                 "forgefirm:.github/workflows/%s.yml" % w) for w in ("main", "docs", "both", "push", "closed")]
        filtered = ": every push or pull_request in its own on: carries a filter (%s)"
        self.assertEqual(m.host_row_problems(man, rows, root=root, sources=None),
                         [rows[1][:3] + (rows[1][3] + filtered % "push: paths-ignore",),
                          rows[2][:3] + (rows[2][3] + filtered % "pull_request: paths; push: branches",),
                          rows[4][:3] + (rows[4][3] + filtered % "pull_request: types",)])

    def test_the_lint_says_when_it_could_not_read_a_workflow(self):
        import contextlib
        import io
        import json
        import tempfile
        from unittest import mock
        from forgetest import coverage
        work = tempfile.mkdtemp()
        self.addCleanup(__import__("shutil").rmtree, work)
        comps = {"forgectrl": {"srcrev": "aaa", "source": "https://example.invalid/forgectrl.git", "files": [
            ["src/tray.c", "1" * 40], ["tests/tray_test.c", "2" * 40], [".github/workflows/build.yml", "3" * 40]]}}
        path = os.path.join(work, "manifest.json")
        with open(path, "w", encoding="utf-8") as f:
            f.write(json.dumps(helpers.make_manifest(components=comps).data))
        row = ("forgectrl", "src/tray.c", "forgectrl:tests/tray_test.c", "forgectrl:.github/workflows/build.yml")
        out = io.StringIO()
        with mock.patch.object(m, "HOST_TESTED", [row]), contextlib.redirect_stdout(out):
            rc = coverage.main(["--manifest", path, "--sources", work, "--json"])
        self.assertEqual(rc, 0)                         # reporting only
        rep = json.loads(out.getvalue())
        self.assertFalse(rep["workflows_read"])
        self.assertTrue(any("cannot be read" in r[3] for r in rep["host_tested"]))
        out = io.StringIO()
        with mock.patch.object(m, "HOST_TESTED", [row]), contextlib.redirect_stdout(out):
            self.assertEqual(coverage.main(["--manifest", path, "--sources", work, "--enforce"]), 1)

    def test_where_a_workflow_checks_a_repository_out(self):
        steps = m.workflow_steps("jobs:\n  t:\n    steps:\n"
                                 "      - uses: actions/checkout@v4\n        with:\n          path: own\n"
                                 "      - name: Fetch the harnesses\n        uses: actions/checkout@v4\n"
                                 "        with:\n          repository: openglow-org/forgefirm\n          path: forgefirm\n")
        self.assertEqual(m.checkout_path(steps, "grblHAL-glowforge", True), "own")
        self.assertEqual(m.checkout_path(steps, "forgefirm", False), "forgefirm")
        self.assertIsNone(m.checkout_path(steps, "forgectrl", False))
        self.assertEqual(m.checkout_path(m.workflow_steps("steps:\n  - uses: actions/checkout@v4\n"), "x", True), ".")
        self.assertEqual(m.repo_name(helpers.make_manifest(components={"python3-gfutilities": {
            "source": "https://github.com/openglow-org/Glowforge-Utilities.git", "files": []}}),
            "python3-gfutilities"), "Glowforge-Utilities")

    def test_a_components_workflow_is_read_at_its_pinned_blob(self):
        import hashlib
        import shutil
        import subprocess
        import tempfile
        if not shutil.which("git"):
            self.skipTest("no git")
        sources = tempfile.mkdtemp()

        def writable(func, path, _exc):         # git writes its objects read-only
            os.chmod(path, 0o700)
            func(path)
        self.addCleanup(shutil.rmtree, sources, **({"onexc": writable} if sys.version_info >= (3, 12)
                                                   else {"onerror": writable}))
        url = "https://example.invalid/forgectrl.git"
        cache = os.path.join(sources, hashlib.sha1(url.encode()).hexdigest()[:16])
        os.makedirs(cache)
        subprocess.run(["git", "init", "-q", "--bare"], cwd=cache, check=True)

        def blob(text):
            return subprocess.run(["git", "hash-object", "-w", "--stdin"], cwd=cache, input=text.encode(),
                                  capture_output=True, check=True).stdout.decode().strip()
        yml = "on: [push, pull_request]\nsteps:\n  - uses: actions/checkout@v4\n  - run: ./build/tray_test\n"
        comps = {"forgectrl": {"srcrev": "aaa", "source": url, "files": [
            ["src/tray.c", "1" * 40], ["tests/tray_test.c", "2" * 40], ["tests/lens_test.c", "3" * 40],
            [".github/workflows/build.yml", blob(yml)], [".github/workflows/lost.yml", "4" * 40]]}}
        man = helpers.make_manifest(components=comps)
        ci = "forgectrl:.github/workflows/build.yml"
        self.assertEqual(m.read_source(man, ci, root=None, sources=sources), (yml, None))
        tray = ("forgectrl", "src/tray.c", "forgectrl:tests/tray_test.c", ci)
        lens = ("forgectrl", "src/tray.c", "forgectrl:tests/lens_test.c", ci)
        self.assertEqual(m.host_row_problems(man, [tray, lens], root=None, sources=sources),
                         [(lens[0], lens[1], lens[2], ci + " does not run it")])
        # without the sources the workflow cannot be read, and is not judged
        self.assertEqual(m.host_row_problems(man, [lens], root=None, sources=None), [])
        # with them, a workflow that should be read and is not is a problem, never a pass
        lost = ("forgectrl", "src/tray.c", "forgectrl:tests/tray_test.c", "forgectrl:.github/workflows/lost.yml")
        probs = m.host_row_problems(man, [lost], root=None, sources=sources)
        self.assertEqual(len(probs), 1)
        self.assertIn("cannot be read", probs[0][3])

    def test_the_real_host_tested_rows_are_well_formed(self):
        for row in m.HOST_TESTED:
            self.assertEqual(len(row), 4, row)
            self.assertIn(":", row[2], row)
            self.assertIn(":", row[3], row)


class CatalogTests(unittest.TestCase):
    def test_catalog_hash_is_definition_only(self):
        r1 = helpers.registry(helpers.make_test("a.one", [("forgectrl", "src/ui.c")]))
        r2 = helpers.registry(helpers.make_test("a.one", [("forgectrl", "src/ui.c")], fn=lambda ctx: 1))
        self.assertEqual(catalog.catalog_hash(r1), catalog.catalog_hash(r2))
        r3 = helpers.registry(helpers.make_test("a.one", [("forgectrl", "src/**")]))
        self.assertNotEqual(catalog.catalog_hash(r1), catalog.catalog_hash(r3))
        r4 = helpers.registry(helpers.make_test("a.one", [("forgectrl", "src/ui.c")], always=True))
        self.assertNotEqual(catalog.catalog_hash(r1), catalog.catalog_hash(r4))

    def test_validate(self):
        r = helpers.registry(helpers.make_test("a.one", [], requires=("a.two",)))
        with self.assertRaises(ValueError):
            catalog.validate(r)
        r = helpers.registry(helpers.make_test("a.one", [], requires=("a.two",)),
                             helpers.make_test("a.two", [], requires=("a.one",)))
        with self.assertRaises(ValueError):
            catalog.validate(r)

    def test_decorator_rules(self):
        with self.assertRaises(ValueError):
            catalog.test("bad id", title="x", subsystem="s")(lambda c: None)
        with self.assertRaises(ValueError):
            catalog.test("s.x", title="x", subsystem="s", covers=[("forgetest", "**")])(lambda c: None)

    def test_real_suite_loads_and_validates(self):
        reg = catalog.load_suite()
        self.assertIn("image.health", reg)
        self.assertTrue(reg["image.health"].always)
        catalog.validate(reg)
        for t in catalog.all_tests(reg):
            self.assertEqual(len(t.source_sha), 64)

    def test_the_shared_judges_exist_and_take_every_package_import_of_the_suite(self):
        # The list is kept by hand: the Context reaches runner.py, hw.py
        # and baseline.py with no import to find. So every package module
        # a suite module does import, at its top level or inside a function
        # or a block, has to be on it (or be the registry, which judges
        # nothing), or that judge would be in no fingerprint. The sibling
        # walk follows the relative spellings alone (`from .x import` and
        # `from . import x`), so a suite module imports the package by no
        # absolute name.
        import ast
        import glob
        paths = catalog.shared_judge_paths()
        self.assertEqual([os.path.basename(p) for p in paths], list(catalog.SHARED_JUDGES))
        for p in paths:
            self.assertTrue(os.path.isfile(p), p)
        allowed = set(catalog.SHARED_JUDGES) | {"catalog.py"}
        for mod in sorted(glob.glob(os.path.join(catalog.suite_dir(), "*.py"))):
            with open(mod, encoding="utf-8") as f:
                tree = ast.parse(f.read())
            for node in ast.walk(tree):
                if isinstance(node, ast.ImportFrom) and node.level == 2:
                    for name in ([node.module] if node.module else [a.name for a in node.names]):
                        self.assertIn(name + ".py", allowed,
                                      "%s imports %s, which no fingerprint carries" % (os.path.basename(mod), name))
                absolute = ([node.module or ""] if isinstance(node, ast.ImportFrom) and node.level == 0
                            else [a.name for a in node.names] if isinstance(node, ast.Import) else [])
                for name in absolute:
                    self.assertFalse(name == "forgetest" or name.startswith("forgetest."),
                                     "%s imports %s by its absolute name, which no hash follows: import it "
                                     "relatively (from .. or from .)" % (os.path.basename(mod), name))


class ImplementationHashTests(unittest.TestCase):
    """A test's implementation hash is its own function plus the code its
    module shares: a body edit moves one test, a helper edit moves them
    all, and a file without @test functions hashes whole."""

    MODULE = '''"""a suite module"""
from forgetest.catalog import test


def helper(x):
    return x + 1


@test("m.one", title="one", subsystem="m")
def one(ctx):
    ctx.log(helper(1))


@test("m.two", title="two", subsystem="m",
      steps=["a step"])
def two(ctx):
    ctx.log(helper(2))
'''

    def setUp(self):
        import tempfile
        self.tmp = tempfile.mkdtemp(prefix="forgetest-impl-")
        self.path = os.path.join(self.tmp, "mod.py")
        self.write(self.MODULE)

    def tearDown(self):
        import shutil
        shutil.rmtree(self.tmp, ignore_errors=True)

    def write(self, text):
        catalog.forget(self.path)
        with open(self.path, "w", newline="\n") as f:
            f.write(text)

    def shas(self):
        return catalog.implementation_sha(self.path, "m.one"), catalog.implementation_sha(self.path, "m.two")

    def test_a_body_edit_moves_that_test_only(self):
        a1, b1 = self.shas()
        self.write(self.MODULE.replace("ctx.log(helper(2))", "ctx.log(helper(3))"))
        a2, b2 = self.shas()
        self.assertEqual(a1, a2)
        self.assertNotEqual(b1, b2)

    def test_a_decorator_edit_moves_that_test_only(self):
        a1, b1 = self.shas()
        self.write(self.MODULE.replace('steps=["a step"]', 'steps=["another step"]'))
        a2, b2 = self.shas()
        self.assertEqual(a1, a2)
        self.assertNotEqual(b1, b2)

    def test_a_helper_edit_moves_every_test_of_the_module(self):
        a1, b1 = self.shas()
        self.write(self.MODULE.replace("return x + 1", "return x + 2"))
        a2, b2 = self.shas()
        self.assertNotEqual(a1, a2)
        self.assertNotEqual(b1, b2)

    def test_a_helper_edit_in_an_imported_sibling_moves_the_tests(self):
        # laser.py imports its judges from motion.py: an edit to such a
        # helper changes the judge, so it has to move the fingerprints of
        # the tests that import it, transitively.
        sib = os.path.join(self.tmp, "judge.py")
        with open(sib, "w", newline="\n") as f:
            f.write("def judge(x):\n    return x > 1\n")
        catalog.forget(sib)
        self.write("from .judge import judge\n" + self.MODULE)
        a1, b1 = self.shas()
        with open(sib, "w", newline="\n") as f:
            f.write("def judge(x):\n    return x > 2\n")
        catalog.forget(sib)
        a2, b2 = self.shas()
        self.assertNotEqual(a1, a2)
        self.assertNotEqual(b1, b2)
        # a module that is not imported changes nothing
        other = os.path.join(self.tmp, "other.py")
        with open(other, "w", newline="\n") as f:
            f.write("def o():\n    return 1\n")
        self.assertEqual((a2, b2), self.shas())

    def test_a_shared_judge_edit_moves_every_test(self):
        # runner.py, hw.py and baseline.py are in every test's path with no
        # import to follow (the Context, the baseline around each run), and
        # puls.py comes in by a package import the walk does not follow, so
        # their text joins every implementation hash.
        judge = os.path.join(self.tmp, "hwlike.py")

        def write_judge(text):
            with open(judge, "w", newline="\n") as f:
                f.write(text)
            catalog.forget(judge)

        def shas():
            return tuple(catalog.implementation_sha(self.path, t, judges=[judge]) for t in ("m.one", "m.two"))

        write_judge("def sysfs_int(attr, default=None):\n    return default\n")
        a1, b1 = shas()
        write_judge("def sysfs_int(attr, default=None):\n    return 8\n")
        a2, b2 = shas()
        self.assertNotEqual(a1, a2)
        self.assertNotEqual(b1, b2)
        # a package file outside the list changes nothing
        other = os.path.join(self.tmp, "server.py")
        with open(other, "w", newline="\n") as f:
            f.write("def serve():\n    return 1\n")
        self.assertEqual((a2, b2), shas())
        # Test.source_sha, which the fingerprint, the campaign and the gate
        # read, takes the shared judges itself: a suite test's hash is the
        # one with SHARED_JUDGES folded in, not the one without them.
        import inspect
        t = catalog.load_suite()["image.health"]
        src = inspect.getsourcefile(t.fn)
        self.assertEqual(t.source_sha, catalog.implementation_sha(src, t.id, judges=catalog.shared_judge_paths()))
        self.assertNotEqual(t.source_sha, catalog.implementation_sha(src, t.id, judges=[]))

    def test_a_sibling_imported_inside_a_function_moves_the_tests_that_run_it(self):
        # A test that imports a judge inside its own function carries the
        # judge in its hash, and what the judge imports in turn, at any
        # depth, and no other test of its module does; a helper of the
        # module that imports one inside its body carries it into every
        # test of the module.
        def put(name, text):
            path = os.path.join(self.tmp, name)
            with open(path, "w", newline="\n") as f:
                f.write(text)
            catalog.forget(path)

        def write_judge(text):
            put("judge.py", text)
        write_judge("def judge(x):\n    return x > 1\n")
        self.write(self.MODULE.replace("def two(ctx):\n",
                                       "def two(ctx):\n    from .judge import judge\n    judge(2)\n"))
        a1, b1 = self.shas()
        write_judge("def judge(x):\n    return x > 2\n")
        a2, b2 = self.shas()
        self.assertEqual(a1, a2)
        self.assertNotEqual(b1, b2)
        # judge.py imports limit.py, which imports base.py: an edit to
        # either moves m.two, and not m.one
        put("base.py", "BASE = 1\n")
        put("limit.py", "from .base import BASE\n\n\nLIMIT = BASE\n")
        write_judge("from .limit import LIMIT\n\n\ndef judge(x):\n    return x > LIMIT\n")
        a5, b5 = self.shas()
        put("limit.py", "from .base import BASE\n\n\nLIMIT = BASE + 1\n")
        a6, b6 = self.shas()
        self.assertEqual(a5, a6)
        self.assertNotEqual(b5, b6)
        put("base.py", "BASE = 2\n")
        a7, b7 = self.shas()
        self.assertEqual(a6, a7)
        self.assertNotEqual(b6, b7)
        self.write(self.MODULE.replace("    return x + 1\n",
                                       "    from .judge import judge\n    return judge(x)\n"))
        a3, b3 = self.shas()
        write_judge("def judge(x):\n    return x > 3\n")
        a4, b4 = self.shas()
        self.assertNotEqual(a3, a4)
        self.assertNotEqual(b3, b4)
        # the spelling with no module (`from . import x`, as homeoff.py imports cloud.py) is
        # walked the same
        self.write(self.MODULE.replace("def two(ctx):\n",
                                       "def two(ctx):\n    from . import judge as j\n    j.judge(2)\n"))
        a8, b8 = self.shas()
        write_judge("def judge(x):\n    return x > 4\n")
        a9, b9 = self.shas()
        self.assertEqual(a8, a9)
        self.assertNotEqual(b8, b9)

    def test_line_endings_do_not_count(self):
        a1, b1 = self.shas()
        catalog.forget(self.path)
        with open(self.path, "wb") as f:
            f.write(self.MODULE.replace("\n", "\r\n").encode())
        self.assertEqual((a1, b1), self.shas())

    def test_an_unknown_id_hashes_the_whole_file(self):
        self.assertEqual(catalog.implementation_sha(self.path, "m.none"), catalog.source_file_sha(self.path))

    def test_every_suite_test_is_spanned(self):
        import inspect
        reg = catalog.load_suite()
        for t in catalog.all_tests(reg):
            path = inspect.getsourcefile(t.fn)
            self.assertIn(t.id, catalog.module_parts(path)[1], t.id)


if __name__ == "__main__":
    unittest.main()
