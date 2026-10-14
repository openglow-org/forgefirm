# Copyright 2026 514 LLC d/b/a OpenGlow
# Written by Scott Wiederhold
# https://community.openglow.org
# SPDX-License-Identifier:    MIT

"""The caches behind the page's state.

Every poll used to re-read and re-parse the whole result log and recompute
every test's domain fingerprint; a result record carries its run log, so
the file reaches megabytes over a campaign. Both are now kept between
polls, and a stale cache would misjudge the gate: the log cache picks up
what is appended or replaced and never counts a partial line, and the
fingerprint cache never serves a changed manifest or one it cannot key.
"""
import os
import shutil
import tempfile
import unittest

import helpers
from forgetest import manifest as manifest_mod
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

    def test_appends_are_picked_up(self):
        self.log.append({"t": "campaign", "id": "c1"})
        self.assertEqual([r["id"] for r in self.log.read()], ["c1"])
        self.log.append({"t": "result", "test": "a.b"})
        self.log.append({"t": "result", "test": "c.d"})
        recs = self.log.read()
        self.assertEqual([r["t"] for r in recs], ["campaign", "result", "result"])
        self.assertEqual(recs[2]["test"], "c.d")

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

    def test_replaced_file_is_read_again(self):
        self.write('{"t":"result","test":"a.b"}', '{"t":"result","test":"c.d"}')
        self.assertEqual(len(self.log.read()), 2)
        with open(self.path, "w", encoding="utf-8") as f:
            f.write('{"t":"campaign","id":"c9"}\n')
        recs = self.log.read()
        self.assertEqual([r.get("id") for r in recs], ["c9"])


class FingerprintCacheTests(unittest.TestCase):
    """Memoized per manifest: the page recomputes every test's fingerprint
    on every poll, and a manifest never changes under a running tool."""

    def setUp(self):
        self.man = helpers.make_manifest()
        self.t = helpers.make_test("fake.fp", [("forgectrl", "src/ui.c")], fn=t_noop)

    def test_a_changed_manifest_is_not_served_from_the_cache(self):
        first = self.t.fingerprint(self.man)
        man2 = helpers.with_file(self.man, "forgectrl", "src/ui.c", "ui v2")
        self.assertNotEqual(self.t.fingerprint(man2), first)
        # and back again: the cache must not have latched the new one either
        self.assertEqual(self.t.fingerprint(self.man), first)

    def test_manifest_without_a_content_hash_is_not_cached(self):
        """The cache is keyed by the manifest's content hash. A manifest
        that has none must still fingerprint correctly, not collide."""
        a = manifest_mod.Manifest({"components": {"forgectrl": {"files": [["src/ui.c", "aaa"]]}},
                                   "platform": {}})
        b = manifest_mod.Manifest({"components": {"forgectrl": {"files": [["src/ui.c", "bbb"]]}},
                                   "platform": {}})
        self.assertIsNone(a.content_sha)
        self.assertNotEqual(self.t.fingerprint(a), self.t.fingerprint(b))


if __name__ == "__main__":
    unittest.main()
