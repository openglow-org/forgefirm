# Copyright 2026 514 LLC d/b/a OpenGlow
# Written by Scott Wiederhold
# https://community.openglow.org
# SPDX-License-Identifier:    MIT

import json
import os
import shutil
import subprocess
import tempfile
import unittest

import helpers
from forgetest import artifact
from forgetest import campaign
from forgetest import catalog
from forgetest import manifest as manifest_mod
from forgetest import provenance


def rec_campaign(cid, man, chash, ts):
    return {"t": "campaign", "ts": ts, "id": cid, "manifest_sha": man.content_sha,
            "catalog_hash": chash, "image": man.version}


def rec_result(cid, t, man, result, ts, fp=None, prov=None):
    return {"t": "result", "ts": ts, "campaign": cid, "test": t.id, "result": result,
            "fingerprint": fp or t.fingerprint(man), "manifest_sha": man.content_sha,
            "image": man.version, "duration_s": 1, "message": "",
            "provenance": dict(prov if prov is not None else helpers.CLEAN)}


NOT_AS_BUILT = {"clean": False, "why": ["forgectrl: /usr/bin/forgectrl not as built"], "ran": {}}


class CampaignTests(unittest.TestCase):
    def setUp(self):
        self.man = helpers.make_manifest()
        self.core = helpers.make_test("image.health", [("linux-fslc", "**")], always=True)
        self.ui = helpers.make_test("forgectrl.panel", [("forgectrl", "src/ui.c")])
        self.cool = helpers.make_test("cooling.aa-offset-calibrate", [("forgectrl", "src/cool.c")])
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
        # The ui test passes in c1, then fails (or errors) on the same image
        # (an intermittent gate): the result closes c1. In c2 the older PASS
        # must not be inherited over it: the test is required again.
        for result, status in (("FAIL", "fail"), ("ERROR", "error")):
            recs = [rec_campaign("c1", self.man, self.chash, "2026-08-20T10:00:00Z"),
                    rec_result("c1", self.ui, self.man, "PASS", "2026-08-20T10:01:00Z"),
                    rec_result("c1", self.ui, self.man, result, "2026-08-20T10:02:00Z"),
                    rec_campaign("c2", self.man, self.chash, "2026-08-20T11:00:00Z")]
            st = self.compute(recs)
            t = st["tests"][self.ui.id]
            self.assertEqual(t["status"], status)
            self.assertEqual(t["reason"], "failed-since")
            self.assertTrue(t["required"])
            self.assertFalse(t["satisfied"])
            self.assertFalse(st["authorized"])

    def test_a_fail_holds_its_fingerprint_until_a_disposition(self):
        # The FAIL closes c1; in c2 the test is run again and passes on
        # unchanged code. The PASS does not count until the operator
        # answers the FAIL for this test and this fingerprint.
        fail = dict(rec_result("c1", self.ui, self.man, "FAIL", "2026-08-20T10:01:00Z"), needs_disposition=True)
        recs = [rec_campaign("c1", self.man, self.chash, "2026-08-20T10:00:00Z"), fail,
                rec_campaign("c2", self.man, self.chash, "2026-08-20T11:00:00Z"),
                rec_result("c2", self.ui, self.man, "PASS", "2026-08-20T11:01:00Z")]
        st = self.compute(recs)
        self.assertEqual(st["tests"][self.ui.id]["reason"], "disposition")
        self.assertFalse(st["tests"][self.ui.id]["satisfied"])
        # an answer for another fingerprint answers nothing here
        recs.append({"t": "disposition", "ts": "2026-08-20T11:02:00Z", "test": self.ui.id,
                     "fingerprint": "0" * 64, "reason": "another build"})
        self.assertEqual(self.compute(recs)["tests"][self.ui.id]["reason"], "disposition")
        recs.append({"t": "disposition", "ts": "2026-08-20T11:03:00Z", "test": self.ui.id,
                     "fingerprint": self.ui.fingerprint(self.man), "reason": "a loose clamp on the bench"})
        st = self.compute(recs)
        self.assertEqual(st["tests"][self.ui.id]["status"], "pass")
        self.assertTrue(st["tests"][self.ui.id]["satisfied"])
        # A disposition at the FAIL's own place answers it no more than the
        # exporter, and so the gate, does.
        disp = {"t": "disposition", "ts": "2026-08-20T10:01:00Z", "test": self.ui.id, "seq": 2,
                "fingerprint": self.ui.fingerprint(self.man), "reason": "an earlier FAIL"}
        recs = [dict(rec_campaign("c1", self.man, self.chash, "2026-08-20T10:00:00Z"), seq=1), disp,
                dict(fail, seq=2), dict(rec_campaign("c2", self.man, self.chash, "2026-08-20T11:00:00Z"), seq=3),
                dict(rec_result("c2", self.ui, self.man, "PASS", "2026-08-20T11:01:00Z"), seq=4)]
        self.assertEqual(self.compute(recs)["tests"][self.ui.id]["reason"], "disposition")

    def test_an_invalidate_all_answers_no_fail(self):
        # A FAIL nobody answers, then an invalidate-all and a full campaign
        # in which the test passes: the FAIL still holds its fingerprint,
        # and the campaign authorizes nothing.
        fail = dict(rec_result("c1", self.ui, self.man, "FAIL", "2026-08-20T10:01:00Z"), needs_disposition=True)
        recs = [rec_campaign("c1", self.man, self.chash, "2026-08-20T10:00:00Z"), fail,
                {"t": "invalidate", "ts": "2026-08-20T10:02:00Z", "reason": "rerun everything"},
                rec_campaign("c2", self.man, self.chash, "2026-08-20T11:00:00Z")]
        for i, t in enumerate(self.tests):
            recs.append(rec_result("c2", t, self.man, "PASS", "2026-08-20T11:0%d:00Z" % (i + 1)))
        st = self.compute(recs)
        self.assertEqual(st["tests"][self.ui.id]["reason"], "disposition")
        self.assertFalse(st["authorized"])

    def test_an_invalidate_cuts_by_its_place_in_the_log_not_by_its_clock(self):
        # The board has no battery-backed clock: an invalidate recorded
        # before the clock is set carries a time older than the PASSes
        # before it in the log, and still makes every one of them stale.
        recs = [rec_campaign("c1", self.man, self.chash, "2026-11-10T10:00:00Z")]
        for i, t in enumerate(self.tests):
            recs.append(rec_result("c1", t, self.man, "PASS", "2026-11-10T10:0%d:00Z" % (i + 1)))
        recs.append({"t": "invalidate", "ts": "1970-01-01T00:04:10Z", "reason": "new tube"})
        recs.append(rec_campaign("c2", self.man, self.chash, "1970-01-01T00:05:00Z"))
        st = self.compute(recs)
        for t in self.tests:
            self.assertTrue(st["tests"][t.id]["required"], t.id)
        self.assertEqual(st["epoch"]["seq"], len(self.tests) + 2)

    def test_an_open_campaigns_pass_counts_only_on_the_current_fingerprint(self):
        # a forgetest change on the bench mid-campaign moves the test's
        # fingerprint, not the image's manifest: the campaign stays open
        recs = [rec_campaign("c1", self.man, self.chash, "2026-08-20T10:00:00Z"),
                rec_result("c1", self.ui, self.man, "PASS", "2026-08-20T10:01:00Z", fp="0" * 64)]
        st = self.compute(recs)
        self.assertIsNotNone(st["campaign"])
        self.assertTrue(st["tests"][self.ui.id]["required"])

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

    def test_a_pass_not_earned_on_the_pins_as_built_is_never_inherited(self):
        # The ui test passes on the image as built, then again after a hot
        # deploy (same fingerprint: the manifest cannot see the deploy).
        # Nothing inherits the second; the first still stands.
        recs = [rec_campaign("c1", self.man, self.chash, "2026-08-20T10:00:00Z"),
                rec_result("c1", self.ui, self.man, "PASS", "2026-08-20T10:01:00Z", prov=NOT_AS_BUILT),
                rec_result("c1", self.cool, self.man, "PASS", "2026-08-20T10:02:00Z"),
                rec_result("c1", self.cool, self.man, "PASS", "2026-08-20T10:03:00Z", prov=NOT_AS_BUILT),
                rec_campaign("c2", self.man, self.chash, "2026-08-20T11:00:00Z")]
        st = self.compute(recs[:4])
        # in its own campaign it counts on the page, never for a release
        self.assertEqual(st["tests"][self.ui.id]["status"], "pass")
        self.assertIn(self.ui.id, st["unclean"])
        st = self.compute(recs)
        self.assertTrue(st["tests"][self.ui.id]["required"])
        self.assertEqual(st["tests"][self.ui.id]["reason"], "not-as-built")
        self.assertEqual(st["tests"][self.cool.id]["status"], "inherited")
        self.assertEqual(st["tests"][self.cool.id]["origin"]["ts"], "2026-08-20T10:02:00Z")
        # a full campaign in which one test ran on a hot-deployed binary
        # does not authorize
        recs = [rec_campaign("c3", self.man, self.chash, "2026-08-21T10:00:00Z")]
        for i, t in enumerate(self.tests):
            recs.append(rec_result("c3", t, self.man, "PASS", "2026-08-21T10:0%d:00Z" % (i + 1),
                                   prov=NOT_AS_BUILT if t is self.cool else None))
        st = self.compute(recs)
        self.assertEqual(st["counts"]["satisfied"], len(self.tests))
        self.assertEqual(st["unclean"], [self.cool.id])
        self.assertFalse(st["authorized"])

    def test_the_open_campaigns_verdict_is_in_the_state(self):
        # The page shows what the machine ran when the campaign opened.
        opened = dict(rec_campaign("c1", self.man, self.chash, "2026-08-20T10:00:00Z"), provenance=NOT_AS_BUILT)
        self.assertEqual(self.compute([opened])["verdict"], NOT_AS_BUILT)
        closed = [opened, {"t": "reset", "ts": "2026-08-20T11:00:00Z", "reason": "x"}]
        self.assertIsNone(self.compute(closed)["verdict"])

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


class LogPlaceTests(unittest.TestCase):
    """The log gives every record its place in the file (seq), which is
    what the campaign rules order records by."""

    def test_each_record_carries_its_place_in_the_log(self):
        from forgetest.log import Log
        d = tempfile.mkdtemp(prefix="forgetest-log-")
        self.addCleanup(shutil.rmtree, d, True)
        path = os.path.join(d, "results.jsonl")
        # two records written before records carried a place, one line lost
        with open(path, "w", encoding="utf-8") as f:
            f.write('{"t":"campaign","ts":"2026-01-01T00:00:00Z","id":"c1"}\nnot json\n'
                    '{"t":"reset","ts":"2026-01-01T00:01:00Z"}\n')
        log = Log(path)
        rec = log.append({"t": "invalidate", "reason": "new tube"})
        self.assertEqual(rec["seq"], 3)
        self.assertEqual([r["seq"] for r in log.read()], [1, 2, 3])
        self.assertEqual(Log(path).read()[-1]["seq"], 3)

    def test_a_torn_last_line_does_not_swallow_the_next_record(self):
        # A power cut left half a line; the FAIL written next stands alone.
        from forgetest.log import Log
        d = tempfile.mkdtemp(prefix="forgetest-log-")
        self.addCleanup(shutil.rmtree, d, True)
        path = os.path.join(d, "results.jsonl")
        with open(path, "w", encoding="utf-8") as f:
            f.write('{"t":"campaign","ts":"2026-01-01T00:00:00Z","id":"c1"}\n{"t":"result","te')
        Log(path).append({"t": "result", "test": "x", "result": "FAIL", "needs_disposition": True})
        log = Log(path)
        self.assertEqual([r.get("result") for r in log.read()], [None, "FAIL"])
        self.assertEqual(log.corrupt, 1)

    def test_a_record_that_lost_only_its_newline_keeps_its_place(self):
        # A power cut took the disposition's newline and nothing else; the
        # FAIL written next takes the place after it, never its place.
        from forgetest.log import Log
        d = tempfile.mkdtemp(prefix="forgetest-log-")
        self.addCleanup(shutil.rmtree, d, True)
        path = os.path.join(d, "results.jsonl")
        with open(path, "w", encoding="utf-8") as f:
            f.write('{"t":"campaign","ts":"2026-01-01T00:00:00Z","id":"c1","seq":1}\n'
                    '{"t":"result","test":"x","result":"FAIL","needs_disposition":true,"seq":2}\n'
                    '{"t":"disposition","test":"x","reason":"a loose clamp","seq":3}')
        rec = Log(path).append({"t": "result", "test": "x", "result": "FAIL", "needs_disposition": True})
        self.assertEqual(rec["seq"], 4)
        self.assertEqual([(r["t"], r["seq"]) for r in Log(path).read()],
                         [("campaign", 1), ("result", 2), ("disposition", 3), ("result", 4)])


GIT_ENV = dict(os.environ, GIT_CONFIG_GLOBAL=os.devnull, GIT_CONFIG_NOSYSTEM="1", GIT_AUTHOR_NAME="t",
               GIT_AUTHOR_EMAIL="t@example.org", GIT_COMMITTER_NAME="t", GIT_COMMITTER_EMAIL="t@example.org")


@unittest.skipUnless(shutil.which("git"), "git not available")
class AsBuiltInheritanceTests(unittest.TestCase):
    """End to end, with the layer's own entry function
    (forgefirm-manifest.bbclass), the fingerprint, the campaign rules and
    the gate: a PASS earned on a build of uncommitted code is inherited by
    no build of the commit without it, and the gate refuses it. The
    control: a PASS earned on a clean build is inherited by a later clean
    build whose change it does not cover, and the gate accepts it."""

    def setUp(self):
        self.cls = helpers.bbclass("forgefirm-manifest")
        self.tmp = tempfile.mkdtemp(prefix="forgetest-asbuilt-")
        self.src = os.path.join(self.tmp, "forgectrl")
        self.write("src/ui.c", "ui")
        self.write("src/cool.c", "cool")
        self.git("init", "-q")
        self.commit()
        self.core = helpers.make_test("image.health", [("linux-fslc", "**")], always=True)
        self.ui = helpers.make_test("forgectrl.panel", [("forgectrl", "src/ui.c")])
        self.cool = helpers.make_test("cooling.calibrate", [("forgectrl", "src/cool.c")])
        self.tests = [self.core, self.ui, self.cool]
        self.chash = catalog.catalog_hash(helpers.registry(*self.tests))

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def write(self, path, text):
        os.makedirs(os.path.dirname(os.path.join(self.src, path)), exist_ok=True)
        with open(os.path.join(self.src, path), "w", encoding="utf-8", newline="\n") as f:
            f.write(text)

    def git(self, *args):
        subprocess.run(["git", "-C", self.src] + list(args), check=True, capture_output=True, env=GIT_ENV)

    def commit(self):
        self.git("add", "-A")
        self.git("commit", "-q", "-m", "change")

    def build(self, name):
        """The manifest of a build of the checkout as it stands, and the
        verdict a run on it records (its files as built)."""
        d = helpers.FakeData(PN="forgectrl", PV="1", FORGEFIRM_MANIFEST_NAME="forgectrl", S=self.src,
                             SRC_URI="git://example.org/forgectrl.git;protocol=https",
                             FORGEFIRM_MANIFEST_DIR="/etc/forgefirm-manifest.d")
        entry = self.cls["forgefirm_manifest_entry"](d)
        entry.pop("recipe")
        entry.pop("installed")
        comps = {"forgectrl": entry,
                 "linux-fslc": {"srcrev": "eee", "files": [["@srcrev", "eee"], ["@config", "cfg1"]]}}
        plat = {"machine": "glowforge", "layers": {"meta-forgefirm": {"content_sha256": "1" * 64}}}
        data = {"format": 1, "image": {"name": "forgefirm-image-dev", "version": name},
                "components": comps, "platform": plat,
                "build": {"layers": {"meta-forgefirm": {"rev": "2" * 40, "dirty": False}}, "kas": {"dirty": False}},
                "content_sha256": manifest_mod.sha256_text(manifest_mod.canonical(
                    {"components": comps, "platform": plat}))}
        man = manifest_mod.Manifest(data)
        why = provenance.manifest_reasons(man)
        return man, {"clean": not why, "why": why, "ran": {}}

    def campaign(self, recs, cid, man, verdict, tests, ts):
        recs.append(rec_campaign(cid, man, self.chash, ts + ":00Z"))
        for i, t in enumerate(tests):
            recs.append(rec_result(cid, t, man, "PASS", ts + ":%02dZ" % (i + 1), prov=verdict))

    def test_a_pass_on_uncommitted_code_speaks_for_no_build_of_the_commit(self):
        # A build of the commit with cool.c edited and not committed: the
        # whole catalog passes on it.
        self.write("src/cool.c", "cool, edited")
        dirty, dirty_verdict = self.build("dirty")
        self.assertFalse(dirty_verdict["clean"])
        recs = []
        self.campaign(recs, "c1", dirty, dirty_verdict, self.tests, "2026-08-20T10:00")
        # The edit dropped, the commit built clean. The entry lists what was
        # compiled: the edited file's fingerprint moves...
        self.git("checkout", "--", "src/cool.c")
        clean, clean_verdict = self.build("clean")
        self.assertTrue(clean_verdict["clean"], clean_verdict)
        self.assertNotEqual(self.cool.fingerprint(clean), self.cool.fingerprint(dirty))
        # ...and a test the edit did not touch keeps its fingerprint, but the
        # PASS it earned on the dirty build is still not inherited.
        self.assertEqual(self.ui.fingerprint(clean), self.ui.fingerprint(dirty))
        st = campaign.compute(recs, self.tests, clean, self.chash)
        self.assertEqual(st["tests"][self.ui.id]["reason"], "not-as-built")
        self.assertEqual(st["tests"][self.cool.id]["reason"], "domain-changed")
        # The gate refuses the dirty campaign's export for the clean build.
        st = campaign.compute(recs, self.tests, dirty, self.chash)
        art = json.loads(artifact.to_json(artifact.build(st, self.tests, dirty, recs, self.chash)))
        ok, rows, problems = artifact.verify(art, clean, self.tests, self.chash)
        self.assertFalse(ok)
        self.assertIn(self.ui.id, [r["id"] for r in rows if not r["ok"]])

        # The control: the whole catalog on the clean build; then cool.c
        # changes in a commit. The new build reruns the core and cool, and
        # inherits the panel's PASS.
        self.campaign(recs, "c2", clean, clean_verdict, self.tests, "2026-08-21T10:00")
        self.write("src/cool.c", "cool v2")
        self.commit()
        nxt, nxt_verdict = self.build("next")
        self.campaign(recs, "c3", nxt, nxt_verdict, [self.core, self.cool], "2026-08-22T10:00")
        st = campaign.compute(recs, self.tests, nxt, self.chash)
        self.assertEqual(st["tests"][self.ui.id]["status"], "inherited")
        self.assertEqual(st["tests"][self.ui.id]["origin"]["campaign"], "c2")
        self.assertTrue(st["authorized"])
        art = json.loads(artifact.to_json(artifact.build(st, self.tests, nxt, recs, self.chash)))
        ok, rows, problems = artifact.verify(art, nxt, self.tests, self.chash)
        self.assertEqual(problems, [])


if __name__ == "__main__":
    unittest.main()
