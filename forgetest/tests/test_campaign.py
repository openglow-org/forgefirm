import unittest

import helpers
from forgetest import campaign
from forgetest import catalog


def rec_campaign(cid, man, chash, ts):
    return {"t": "campaign", "ts": ts, "id": cid, "manifest_sha": man.content_sha,
            "catalog_hash": chash, "image": man.version}


def rec_result(cid, t, man, result, ts, fp=None):
    return {"t": "result", "ts": ts, "campaign": cid, "test": t.id, "result": result,
            "fingerprint": fp or t.fingerprint(man), "manifest_sha": man.content_sha,
            "image": man.version, "duration_s": 1, "message": ""}


class CampaignTests(unittest.TestCase):
    def setUp(self):
        self.man = helpers.make_manifest()
        self.core = helpers.make_test("image.health", [("linux-fslc", "**")], always=True)
        self.ui = helpers.make_test("forgectrl.panel", [("forgectrl", "src/ui.c")])
        self.cool = helpers.make_test("cooling.flow", [("forgectrl", "src/cool.c")])
        self.live = helpers.make_test("laser.witness", [("grblhal-glowforge", "src/**")],
                                      requires=("forgectrl.panel",), kind="live")
        self.reg = helpers.registry(self.core, self.ui, self.cool, self.live)
        self.tests = list(self.reg.values())
        self.chash = catalog.catalog_hash(self.reg)

    def compute(self, records, man=None, chash=None):
        return campaign.compute(records, self.tests, man or self.man, chash or self.chash)

    def test_empty(self):
        st = self.compute([])
        self.assertIsNone(st["campaign"])
        self.assertFalse(st["authorized"])
        for t in self.tests:
            self.assertTrue(st["tests"][t.id]["required"])
            self.assertEqual(st["tests"][t.id]["reason"], "always" if t.always else "never-passed")
        self.assertFalse(st["tests"][self.live.id]["requires_met"])

    def test_full_campaign_authorizes(self):
        recs = [rec_campaign("c1", self.man, self.chash, "2026-08-20T10:00:00Z")]
        for i, t in enumerate(self.tests):
            recs.append(rec_result("c1", t, self.man, "PASS", "2026-08-20T10:0%d:00Z" % (i + 1)))
        st = self.compute(recs)
        self.assertTrue(st["authorized"])
        self.assertEqual(st["counts"], {"total": 4, "satisfied": 4, "inherited": 0, "required": 0})
        self.assertEqual(st["tests"][self.core.id]["status"], "pass")

    def test_fail_closes_campaign(self):
        recs = [rec_campaign("c1", self.man, self.chash, "2026-08-20T10:00:00Z"),
                rec_result("c1", self.core, self.man, "PASS", "2026-08-20T10:01:00Z"),
                rec_result("c1", self.ui, self.man, "PASS", "2026-08-20T10:02:00Z"),
                rec_result("c1", self.cool, self.man, "FAIL", "2026-08-20T10:03:00Z")]
        st = self.compute(recs)
        self.assertIsNone(st["campaign"])
        self.assertEqual(st["closed_by"], "fail")
        self.assertFalse(st["authorized"])
        # the core PASS in the closed campaign no longer counts; the ui PASS is inheritable
        self.assertTrue(st["tests"][self.core.id]["required"])
        self.assertEqual(st["tests"][self.core.id]["reason"], "always")
        self.assertEqual(st["tests"][self.ui.id]["status"], "inherited")
        self.assertEqual(st["tests"][self.cool.id]["status"], "fail")
        self.assertEqual(st["tests"][self.cool.id]["reason"], "never-passed")

    def test_a_fail_after_a_pass_blocks_inheritance(self):
        # The ui test passes in c1, then fails on the same image (an
        # intermittent gate): the FAIL closes c1. In c2 the older PASS must
        # not be inherited over the newer FAIL: the test is required again.
        recs = [rec_campaign("c1", self.man, self.chash, "2026-08-20T10:00:00Z"),
                rec_result("c1", self.ui, self.man, "PASS", "2026-08-20T10:01:00Z"),
                rec_result("c1", self.ui, self.man, "FAIL", "2026-08-20T10:02:00Z"),
                rec_campaign("c2", self.man, self.chash, "2026-08-20T11:00:00Z")]
        st = self.compute(recs)
        t = st["tests"][self.ui.id]
        self.assertEqual(t["status"], "fail")
        self.assertEqual(t["reason"], "failed-since")
        self.assertTrue(t["required"])
        self.assertFalse(t["satisfied"])
        self.assertFalse(st["authorized"])

    def test_an_abort_after_a_pass_does_not_block_inheritance(self):
        recs = [rec_campaign("c1", self.man, self.chash, "2026-08-20T10:00:00Z"),
                rec_result("c1", self.ui, self.man, "PASS", "2026-08-20T10:01:00Z"),
                rec_result("c1", self.ui, self.man, "ABORTED", "2026-08-20T10:02:00Z"),
                rec_campaign("c2", self.man, self.chash, "2026-08-20T11:00:00Z")]
        st = self.compute(recs)
        self.assertEqual(st["tests"][self.ui.id]["status"], "inherited")

    def test_a_pass_after_a_fail_is_inherited(self):
        recs = [rec_campaign("c1", self.man, self.chash, "2026-08-20T10:00:00Z"),
                rec_result("c1", self.ui, self.man, "FAIL", "2026-08-20T10:01:00Z"),
                rec_campaign("c2", self.man, self.chash, "2026-08-20T11:00:00Z"),
                rec_result("c2", self.ui, self.man, "PASS", "2026-08-20T11:01:00Z"),
                rec_campaign("c3", self.man, self.chash, "2026-08-20T12:00:00Z")]
        st = self.compute(recs)
        self.assertEqual(st["tests"][self.ui.id]["status"], "inherited")

    def test_error_closes_aborted_does_not(self):
        recs = [rec_campaign("c1", self.man, self.chash, "2026-08-20T10:00:00Z"),
                rec_result("c1", self.ui, self.man, "ABORTED", "2026-08-20T10:01:00Z")]
        st = self.compute(recs)
        self.assertIsNotNone(st["campaign"])
        self.assertEqual(st["tests"][self.ui.id]["status"], "aborted")
        recs.append(rec_result("c1", self.ui, self.man, "ERROR", "2026-08-20T10:02:00Z"))
        st = self.compute(recs)
        self.assertIsNone(st["campaign"])
        self.assertEqual(st["closed_by"], "fail")

    def test_inheritance_follows_fingerprint(self):
        recs = [rec_campaign("c1", self.man, self.chash, "2026-08-20T10:00:00Z")]
        for i, t in enumerate(self.tests):
            recs.append(rec_result("c1", t, self.man, "PASS", "2026-08-20T10:0%d:00Z" % (i + 1)))
        # a new image with only cool.c changed
        man2 = helpers.with_file(self.man, "forgectrl", "src/cool.c", "cool v2")
        st = self.compute(recs, man=man2)
        self.assertIsNone(st["campaign"])
        self.assertEqual(st["closed_by"], "image")
        self.assertEqual(st["tests"][self.ui.id]["status"], "inherited")
        self.assertEqual(st["tests"][self.live.id]["status"], "inherited")
        self.assertEqual(st["tests"][self.cool.id]["status"], "stale")
        self.assertEqual(st["tests"][self.cool.id]["reason"], "domain-changed")
        self.assertEqual(st["tests"][self.core.id]["reason"], "always")
        # open a campaign on the new image, run the core + cool -> authorized
        recs.append(rec_campaign("c2", man2, self.chash, "2026-08-21T10:00:00Z"))
        recs.append(rec_result("c2", self.core, man2, "PASS", "2026-08-21T10:01:00Z"))
        st = self.compute(recs, man=man2)
        self.assertFalse(st["authorized"])
        recs.append(rec_result("c2", self.cool, man2, "PASS", "2026-08-21T10:02:00Z"))
        st = self.compute(recs, man=man2)
        self.assertTrue(st["authorized"])
        self.assertEqual(st["counts"]["inherited"], 2)
        self.assertEqual(st["tests"][self.ui.id]["origin"]["campaign"], "c1")

    def test_platform_change_invalidates_everything(self):
        recs = [rec_campaign("c1", self.man, self.chash, "2026-08-20T10:00:00Z")]
        for i, t in enumerate(self.tests):
            recs.append(rec_result("c1", t, self.man, "PASS", "2026-08-20T10:0%d:00Z" % (i + 1)))
        man2 = helpers.with_platform(self.man, dtb={"glowforge.dtb": "f" * 64})
        st = self.compute(recs, man=man2)
        for t in self.tests:
            self.assertTrue(st["tests"][t.id]["required"], t.id)

    def test_invalidate_all(self):
        recs = [rec_campaign("c1", self.man, self.chash, "2026-08-20T10:00:00Z")]
        for i, t in enumerate(self.tests):
            recs.append(rec_result("c1", t, self.man, "PASS", "2026-08-20T10:0%d:00Z" % (i + 1)))
        recs.append({"t": "invalidate", "ts": "2026-08-22T09:00:00Z", "reason": "new tube"})
        st = self.compute(recs)
        self.assertIsNone(st["campaign"])
        self.assertEqual(st["closed_by"], "invalidate")
        self.assertEqual(st["invalidate"]["reason"], "new tube")
        for t in self.tests:
            self.assertTrue(st["tests"][t.id]["required"], t.id)
            self.assertNotEqual(st["tests"][t.id]["status"], "inherited")
        # after the invalidate, a new campaign's passes count and later ones inherit again
        recs.append(rec_campaign("c2", self.man, self.chash, "2026-08-22T10:00:00Z"))
        st = self.compute(recs)
        # the campaign that started after it answers the invalidation: the
        # page no longer announces it, while its epoch still holds
        self.assertIsNone(st["invalidate"])
        for t in self.tests:
            self.assertTrue(st["tests"][t.id]["required"], t.id)
            self.assertNotEqual(st["tests"][t.id]["status"], "inherited")
        for i, t in enumerate(self.tests):
            recs.append(rec_result("c2", t, self.man, "PASS", "2026-08-22T10:0%d:00Z" % (i + 1)))
        st = self.compute(recs)
        self.assertTrue(st["authorized"])
        self.assertIsNone(st["invalidate"])

    def test_reset_and_catalog_change(self):
        recs = [rec_campaign("c1", self.man, self.chash, "2026-08-20T10:00:00Z"),
                rec_result("c1", self.core, self.man, "PASS", "2026-08-20T10:01:00Z"),
                {"t": "reset", "ts": "2026-08-20T11:00:00Z", "reason": "x"}]
        st = self.compute(recs)
        self.assertIsNone(st["campaign"])
        self.assertEqual(st["closed_by"], "reset")
        recs = [rec_campaign("c1", self.man, self.chash, "2026-08-20T10:00:00Z")]
        st = self.compute(recs, chash="different")
        self.assertIsNone(st["campaign"])
        self.assertEqual(st["closed_by"], "catalog")

    def test_requires_uses_satisfied(self):
        recs = [rec_campaign("c1", self.man, self.chash, "2026-08-20T10:00:00Z"),
                rec_result("c1", self.ui, self.man, "PASS", "2026-08-20T10:01:00Z")]
        st = self.compute(recs)
        self.assertTrue(st["tests"][self.live.id]["requires_met"])
        man2 = helpers.with_file(self.man, "forgectrl", "src/ui.c", "ui v2")
        st = self.compute(recs, man=man2)
        self.assertFalse(st["tests"][self.live.id]["requires_met"])
        self.assertEqual(st["tests"][self.live.id]["missing_requires"], ["forgectrl.panel"])

    def test_running_marker(self):
        st = campaign.compute([], self.tests, self.man, self.chash, running=self.ui.id)
        self.assertEqual(st["tests"][self.ui.id]["status"], "running")


if __name__ == "__main__":
    unittest.main()
