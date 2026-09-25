# Copyright 2026 514 LLC d/b/a OpenGlow
# Written by Scott Wiederhold
# https://community.openglow.org
# SPDX-License-Identifier:    MIT

import os
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
        # **/*.md do not take it out of the report, as they do not take it out of a fingerprint.
        man = helpers.with_file(helpers.make_manifest(), "forgectrl", "docs/advisories/privacy.md", "text")
        rest = helpers.make_test("a.one", [("forgectrl", "src/**"), ("grblhal-glowforge", "**"),
                                           ("kernel-module-glowforge", "**"), ("linux-fslc", "**")])
        self.assertEqual(m.coverage_report(man, [rest]), {"forgectrl": ["docs/advisories/privacy.md"]})
        doc = helpers.make_test("a.two", [("forgectrl", "docs/advisories/privacy.md")])
        self.assertEqual(m.coverage_report(man, [rest, doc]), {})
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


if __name__ == "__main__":
    unittest.main()


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
