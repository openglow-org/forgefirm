# Copyright 2026 514 LLC d/b/a OpenGlow
# Written by Scott Wiederhold
# https://community.openglow.org
# SPDX-License-Identifier:    MIT

"""What keeps the page answering the operator instead of the timer.

Three things went wrong on the bench and are pinned here:

  - the state cost. Every poll re-read and re-parsed the whole result log,
    and recomputed every test's domain fingerprint. A result record carries
    its run log, so the file reaches megabytes over a campaign and the poll
    grew with it. Both are now parsed and computed once.
  - the first state's cost. Hashing the implementations parsed a test's
    module and every module it imports once per test, and each poll the
    page timed out on started the same work again beside the first. Each
    module is now parsed once, and a poll waits for the work in progress.
  - the wasted payload. An idle page polls an unchanged state; it now gets
    a 304 instead of the whole thing.

The click-swallowing defect these were found with lives in the page's
JavaScript and is not reachable from here: rows, prompt buttons and tool
entries are built once and afterwards only updated in place, because a
poll that rebuilt them removed the button between the operator's mousedown
and mouseup, and no click event was ever raised. `test_page_never_rebuilds`
holds the shape of that rule.
"""
import json
import os
import re
import shutil
import tempfile
import unittest

import helpers
from forgetest import catalog, manifest as manifest_mod, page
from forgetest.log import Log


def t_noop(ctx):
    pass


class LogCacheTests(unittest.TestCase):
    """The log is append-only, so a read parses each line exactly once."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="forgetest-log-")
        self.path = os.path.join(self.tmp, "results.jsonl")
        self.log = Log(self.path)

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def write(self, *lines):
        with open(self.path, "a", encoding="utf-8") as f:
            for line in lines:
                f.write(line + "\n")

    def test_missing_file_reads_empty(self):
        self.assertEqual(self.log.read(), [])

    def test_appends_are_picked_up(self):
        self.log.append({"t": "campaign", "id": "c1"})
        self.assertEqual([r["id"] for r in self.log.read()], ["c1"])
        self.log.append({"t": "result", "test": "a.b"})
        self.log.append({"t": "result", "test": "c.d"})
        recs = self.log.read()
        self.assertEqual([r["t"] for r in recs], ["campaign", "result", "result"])
        self.assertEqual(recs[2]["test"], "c.d")

    def test_only_new_bytes_are_parsed(self):
        for i in range(5):
            self.log.append({"t": "result", "test": "t%d" % i})
        self.log.read()
        # a second read must not touch the file at all
        real_open = open
        opened = []

        def counting_open(*a, **kw):
            opened.append(a[0])
            return real_open(*a, **kw)

        import builtins
        builtins.open = counting_open
        try:
            recs = self.log.read()
        finally:
            builtins.open = real_open
        self.assertEqual(opened, [], "an unchanged log was re-opened")
        self.assertEqual(len(recs), 5)

    def test_read_returns_a_fresh_list(self):
        """Callers filter and sort the result; the cache must not be theirs."""
        self.log.append({"t": "result", "test": "a.b"})
        first = self.log.read()
        first.append({"t": "bogus"})
        self.assertEqual(len(self.log.read()), 1)

    def test_partial_trailing_line_is_held_not_counted_corrupt(self):
        self.write('{"t":"result","test":"a.b"}')
        with open(self.path, "a", encoding="utf-8") as f:
            f.write('{"t":"result","te')       # a line still being written
        recs = self.log.read()
        self.assertEqual(len(recs), 1)
        self.assertEqual(self.log.corrupt, 0)
        with open(self.path, "a", encoding="utf-8") as f:
            f.write('st":"c.d"}\n')            # ... now finished
        recs = self.log.read()
        self.assertEqual([r["test"] for r in recs], ["a.b", "c.d"])
        self.assertEqual(self.log.corrupt, 0)

    def test_corrupt_lines_counted_once_not_per_read(self):
        self.write('{"t":"result","test":"a.b"}', "not json at all", '["not","a","record"]',
                   '{"t":"result","test":"c.d"}')
        recs = self.log.read()
        self.assertEqual([r["test"] for r in recs], ["a.b", "c.d"])
        self.assertEqual(self.log.corrupt, 2)
        self.log.read()
        self.log.read()
        self.assertEqual(self.log.corrupt, 2, "corrupt lines re-counted on every read")

    def test_replaced_file_is_read_again(self):
        self.write('{"t":"result","test":"a.b"}', '{"t":"result","test":"c.d"}')
        self.assertEqual(len(self.log.read()), 2)
        with open(self.path, "w", encoding="utf-8") as f:
            f.write('{"t":"campaign","id":"c9"}\n')
        recs = self.log.read()
        self.assertEqual([r.get("id") for r in recs], ["c9"])

    def test_blank_lines_are_not_corrupt(self):
        self.write('{"t":"result","test":"a.b"}', "", "   ", '{"t":"result","test":"c.d"}')
        self.assertEqual(len(self.log.read()), 2)
        self.assertEqual(self.log.corrupt, 0)


class FingerprintCacheTests(unittest.TestCase):
    """Memoized per manifest: the page recomputes every test's fingerprint
    on every poll, and a manifest never changes under a running tool."""

    def setUp(self):
        self.man = helpers.make_manifest()
        self.t = helpers.make_test("fake.fp", [("forgectrl", "src/ui.c")], fn=t_noop)

    def test_repeat_calls_agree(self):
        first = self.t.fingerprint(self.man)
        self.assertEqual(self.t.fingerprint(self.man), first)
        self.assertEqual(self.t.fingerprint(self.man), first)

    def test_a_changed_manifest_is_not_served_from_the_cache(self):
        first = self.t.fingerprint(self.man)
        man2 = helpers.with_file(self.man, "forgectrl", "src/ui.c", "ui v2")
        self.assertNotEqual(self.t.fingerprint(man2), first)
        # and back again: the cache must not have latched the new one either
        self.assertEqual(self.t.fingerprint(self.man), first)

    def test_a_file_outside_the_coverage_does_not_move_it(self):
        first = self.t.fingerprint(self.man)
        man2 = helpers.with_file(self.man, "forgectrl", "src/cool.c", "cool v2")
        self.assertEqual(self.t.fingerprint(man2), first)

    def test_manifest_without_a_content_hash_is_not_cached(self):
        """The cache is keyed by the manifest's content hash. A manifest
        that has none must still fingerprint correctly, not collide."""
        a = manifest_mod.Manifest({"components": {"forgectrl": {"files": [["src/ui.c", "aaa"]]}},
                                   "platform": {}})
        b = manifest_mod.Manifest({"components": {"forgectrl": {"files": [["src/ui.c", "bbb"]]}},
                                   "platform": {}})
        self.assertIsNone(a.content_sha)
        self.assertNotEqual(self.t.fingerprint(a), self.t.fingerprint(b))

    def test_component_file_list_is_stable_and_read_only(self):
        files = self.man.files("forgectrl")
        self.assertEqual(files, self.man.files("forgectrl"))
        self.assertIsNone(self.man.files("no-such-component"))


class ImplementationCostTests(unittest.TestCase):
    """The first state after a start hashes every test's implementation.
    It parses each suite module once, and a poll that arrives while it
    runs waits for it: on the board a parse per test took minutes, and
    the page's timed-out polls each started the work again beside it."""

    def test_each_suite_module_is_parsed_once(self):
        import ast
        import inspect
        reg = catalog.load_suite()
        paths = sorted({inspect.getsourcefile(t.fn) for t in catalog.all_tests(reg)})
        for p in os.listdir(catalog.suite_dir()):
            catalog.forget(os.path.join(catalog.suite_dir(), p))
        for p in paths:
            catalog.forget(p)
        real_parse, parsed = ast.parse, []

        def counting_parse(source, *a, **kw):
            parsed.append(source[:40])
            return real_parse(source, *a, **kw)

        ast.parse = counting_parse
        try:
            for t in catalog.all_tests(reg):
                catalog.implementation_sha(inspect.getsourcefile(t.fn), t.id)
        finally:
            ast.parse = real_parse
        modules = [p for p in os.listdir(catalog.suite_dir()) if p.endswith(".py")]
        self.assertLessEqual(len(parsed), len(modules),
                             "%d parses for %d suite modules" % (len(parsed), len(modules)))

    def test_a_concurrent_fill_waits_instead_of_repeating(self):
        import threading
        import time
        t = helpers.make_test("fake.impl", [("forgectrl", "src/ui.c")], fn=t_noop)
        real, calls = catalog.implementation_sha, []

        def slow(path, test_id):
            calls.append(test_id)
            time.sleep(0.3)
            return real(path, test_id)

        catalog.implementation_sha = slow
        try:
            got = []
            threads = [threading.Thread(target=lambda: got.append(t.source_sha)) for _ in range(3)]
            for th in threads:
                th.start()
            for th in threads:
                th.join(10)
        finally:
            catalog.implementation_sha = real
        self.assertEqual(calls, ["fake.impl"], "the implementation hash was computed %d times" % len(calls))
        self.assertEqual(len(set(got)), 1)
        self.assertEqual(len(got), 3)


class PageTests(unittest.TestCase):
    def test_page_never_rebuilds_what_the_operator_may_be_pressing(self):
        """A poll must update rows, prompt buttons and tool entries in
        place. Assigning innerHTML to their containers on every poll is
        what swallowed the clicks; only the one-time build may do it."""
        html = page.render("0" * 32)
        for container, builder in (("groups", "buildGroups"), ("tools", "buildBench")):
            self.assertIn("function %s(" % builder, html,
                          "no one-time builder for #%s" % container)
            assigns = html.count("$('%s').innerHTML=" % container)
            self.assertEqual(assigns, 1,
                             "#%s is assigned innerHTML %d times; it belongs to the "
                             "builder alone" % (container, assigns))
        # the per-poll path writes through the guarded setters only
        self.assertIn("function setHtml(e,h){if(e&&e.__h!==h)", html)
        self.assertIn("function updateGroups()", html)
        self.assertIn("function updateBench()", html)
        # the prompt buttons are rebuilt only when the prompt changes
        self.assertIn("if(pk!==promptKey)", html)
        # the queue controls are static markup: a poll relabels them and
        # flips disabled, it never replaces the node
        for bid in ("q-unattended", "q-attended", "q-stop"):
            self.assertIn('id="%s"' % bid, html)
            self.assertNotIn("id='" + bid + "-", html)
            self.assertNotIn('id="' + bid + "-", html)
        self.assertIn("function renderQueue()", html)
        # the help popovers sit on static markup only: a rebuilt row or
        # tool entry would orphan one
        self.assertNotIn("data-help", html[html.index("function buildGroups("):])

    def test_every_group_is_built_on_the_same_grid(self):
        """The subsystems are separate tables. Left to size themselves
        from their own content no two line up, which is what made the
        page look busy, so they share one colgroup and a fixed layout."""
        html = page.render("0" * 32)
        self.assertRegex(html, r"#groups table\s*\{\s*table-layout:\s*fixed")
        # one colgroup definition, emitted into every group's table
        self.assertEqual(html.count('var COLS="<colgroup>'), 1)
        self.assertEqual(html.count('<table>"+COLS+"'), 1,
                         "a group table built without the shared colgroup")
        colgroup = re.search(r'var COLS="(.*?)";', html).group(1)
        heads = ["Test", "Kind", "Status", "Last result"]
        for h in heads:
            self.assertIn("<th>%s</th>" % h, html)
        self.assertEqual(len(re.findall(r"<col(?:>| )", colgroup)), len(heads) + 1,
                         "a column per header, the action column included")
        # every fixed column carries a width, or the grid is only a wish
        for cls in re.findall(r"<col class='(\w)'", colgroup):
            self.assertRegex(html, r"#groups col\.%s\s*\{\s*width:\s*\d+px" % cls)

    def test_details_and_the_requires_note_stay_out_of_the_columns(self):
        """Both are long enough to stretch a cell. In a column they would
        pull one group's grid out of step with the rest: the details get a
        full-width row, and the note sits under Status, which is sized for
        it."""
        html = page.render("0" * 32)
        self.assertIn("<tr class='detrow'><td colspan='5'>", html)
        # the status cell carries the status, the unmet note and the row
        # message; the action cell carries the button and nothing else
        self.assertIn("<td><div id='st-\"+d+\"'></div><div id='unmet-\"+d+"
                      "\"'></div><div id='note-\"+d+\"'></div></td>", html)
        self.assertIn("<td><button class='btn btn-sm btn-primary' id='btn-\"+d+\"' "
                      "onclick='startTest(\\\"\"+d+\"\\\")'>Start</button></td>", html)

    def test_page_is_self_contained_ascii(self):
        html = page.render("0" * 32)
        self.assertNotIn("__TOKEN__", html)
        # our own sources stay ASCII (no stray typography in an embedded
        # page); the vendored Bootstrap carries its own glyphs
        for name in ("index.html",) + page.CSS_FILES + page.JS_FILES:
            if name.startswith("vendor/"):
                continue
            page.read_ui(name).encode("ascii")
        stray = sorted(set(hex(ord(c)) for c in html if ord(c) < 32 and c != "\n"))
        self.assertEqual(stray, [], "control characters in the page source")
        # nothing fetched from anywhere: the documentation links open a
        # site, they are not assets
        for remote in ("<link ", "<script src=", "@import", "url(http", "url(//",
                       'src="http', "src='http", "//cdn"):
            self.assertNotIn(remote, html)


if __name__ == "__main__":
    unittest.main()
