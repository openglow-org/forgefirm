# Copyright 2026 514 LLC d/b/a OpenGlow
# Written by Scott Wiederhold
# https://community.openglow.org
# SPDX-License-Identifier:    MIT

import copy
import json
import unittest

import helpers
from forgetest import artifact, campaign, catalog
from forgetest import manifest as manifest_mod
from test_campaign import rec_campaign, rec_result


class ArtifactTests(unittest.TestCase):
    def setUp(self):
        self.man = helpers.make_manifest()
        self.core = helpers.make_test("image.health", [("linux-fslc", "**")], always=True)
        self.ui = helpers.make_test("forgectrl.panel", [("forgectrl", "src/ui.c")])
        self.cool = helpers.make_test("cooling.flow", [("forgectrl", "src/cool.c")])
        self.reg = helpers.registry(self.core, self.ui, self.cool)
        self.tests = list(self.reg.values())
        self.chash = catalog.catalog_hash(self.reg)
        # campaign 1 on the base image: everything passes
        self.recs = [rec_campaign("c1", self.man, self.chash, "2026-08-20T10:00:00Z")]
        for i, t in enumerate(self.tests):
            self.recs.append(rec_result("c1", t, self.man, "PASS", "2026-08-20T10:0%d:00Z" % (i + 1)))
        # image 2: cool.c changed; campaign 2 reruns core + cool
        self.man2 = helpers.with_file(self.man, "forgectrl", "src/cool.c", "cool v2")
        self.recs.append(rec_campaign("c2", self.man2, self.chash, "2026-08-21T10:00:00Z"))
        self.recs.append(rec_result("c2", self.core, self.man2, "PASS", "2026-08-21T10:01:00Z"))
        self.recs.append(rec_result("c2", self.cool, self.man2, "PASS", "2026-08-21T10:02:00Z"))

    def build(self, man=None):
        man = man or self.man2
        st = campaign.compute(self.recs, self.tests, man, self.chash)
        return artifact.build(st, self.tests, man, self.recs, self.chash), st

    def release_manifest(self, man=None):
        """The release build's manifest: same identity, no forgetest component."""
        man = man or self.man2
        data = copy.deepcopy(man.data)
        data["components"].pop("forgetest", None)
        data["image"] = {"name": "forgefirm-image", "version": "v0.1.0"}
        return manifest_mod.Manifest(data)

    def test_build_and_verify(self):
        art, st = self.build()
        self.assertTrue(art["authorized"])
        by = {t["id"]: t for t in art["tests"]}
        self.assertTrue(by["forgectrl.panel"]["inherited"])
        self.assertEqual(by["forgectrl.panel"]["record"]["campaign"], "c1")
        self.assertFalse(by["image.health"]["inherited"])
        text = artifact.to_json(art)
        art2 = json.loads(text)
        ok, rows, problems = artifact.verify(art2, self.release_manifest(), self.tests, self.chash,
                                             expect_machine="glowforge")
        self.assertEqual(problems, [])
        self.assertTrue(ok)
        md = artifact.to_markdown(art)
        self.assertIn("Release authorized: YES", md)
        self.assertIn("forgectrl.panel", md)

    def test_tamper_detected(self):
        art, _ = self.build()
        art2 = json.loads(artifact.to_json(art))
        art2["tests"][1]["record"]["result"] = "PASS"
        art2["counts"]["required"] = 0
        art2["authorized"] = True
        art2["tests"][2]["record"]["ts"] = "2026-08-21T10:02:01Z"
        ok, rows, problems = artifact.verify(art2, self.release_manifest(), self.tests, self.chash)
        self.assertFalse(ok)
        self.assertIn("self-hash", problems[0])

    def test_release_differs_in_covered_file(self):
        art, _ = self.build()
        rel = self.release_manifest(helpers.with_file(self.man2, "forgectrl", "src/ui.c", "ui v3"))
        ok, rows, problems = artifact.verify(art, rel, self.tests, self.chash)
        self.assertFalse(ok)
        self.assertTrue(any("forgectrl.panel" in p and "fingerprint" in p for p in problems), problems)
        # an uncovered change is fine
        rel2 = self.release_manifest(helpers.with_file(self.man2, "forgectrl", "README.md", "docs"))
        ok, rows, problems = artifact.verify(art, rel2, self.tests, self.chash)
        self.assertTrue(ok, problems)

    def test_release_platform_differs(self):
        art, _ = self.build()
        rel = self.release_manifest(helpers.with_platform(self.man2, dtb={"glowforge.dtb": "0" * 64}))
        ok, rows, problems = artifact.verify(art, rel, self.tests, self.chash)
        self.assertFalse(ok)
        self.assertEqual(len([r for r in rows if not r["ok"]]), 3)

    def test_unauthorized_artifact(self):
        self.recs.append(rec_result("c2", self.ui, self.man2, "FAIL", "2026-08-21T10:03:00Z"))
        art, st = self.build()
        self.assertFalse(art["authorized"])
        ok, rows, problems = artifact.verify(art, self.release_manifest(), self.tests, self.chash)
        self.assertFalse(ok)
        self.assertTrue(any("does not claim authorization" in p for p in problems))

    def test_core_inherited_refused(self):
        art, _ = self.build()
        # forge an artifact whose core record is marked inherited (self-hash recomputed)
        body = json.loads(artifact.to_json(art))
        body.pop("sha256")
        for t in body["tests"]:
            if t["id"] == "image.health":
                t["inherited"] = True
        body["sha256"] = manifest_mod.sha256_text(manifest_mod.canonical(body))
        ok, rows, problems = artifact.verify(body, self.release_manifest(), self.tests, self.chash)
        self.assertFalse(ok)
        self.assertTrue(any("always-required" in p for p in problems), problems)

    def test_invalidate_epoch(self):
        art, _ = self.build()
        body = json.loads(artifact.to_json(art))
        body.pop("sha256")
        body["invalidate"] = {"t": "invalidate", "ts": "2026-08-20T12:00:00Z", "reason": "tube"}
        body["sha256"] = manifest_mod.sha256_text(manifest_mod.canonical(body))
        ok, rows, problems = artifact.verify(body, self.release_manifest(), self.tests, self.chash)
        self.assertFalse(ok)
        self.assertTrue(any("predates the invalidate" in p for p in problems), problems)

    def test_catalog_changed(self):
        art, _ = self.build()
        reg = helpers.registry(self.core, self.ui, self.cool, helpers.make_test("new.one", []))
        tests = list(reg.values())
        ok, rows, problems = artifact.verify(art, self.release_manifest(), tests, catalog.catalog_hash(reg))
        self.assertFalse(ok)
        self.assertTrue(any("catalog changed" in p for p in problems))
        self.assertTrue(any("new.one" in p for p in problems))

    def test_implementation_changed(self):
        art, _ = self.build()
        body = json.loads(artifact.to_json(art))
        body.pop("sha256")
        for t in body["tests"]:
            if t["id"] == "cooling.flow":
                t["source_sha"] = "0" * 64
        body["sha256"] = manifest_mod.sha256_text(manifest_mod.canonical(body))
        ok, rows, problems = artifact.verify(body, self.release_manifest(), self.tests, self.chash)
        self.assertFalse(ok)
        self.assertTrue(any("implementation changed" in p for p in problems), problems)


if __name__ == "__main__":
    unittest.main()
