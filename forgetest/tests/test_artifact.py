# Copyright 2026 514 LLC d/b/a OpenGlow
# Written by Scott Wiederhold
# https://community.openglow.org
# SPDX-License-Identifier:    MIT

import copy
import json
import os
import shutil
import subprocess
import sys
import tempfile
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
        self.cool = helpers.make_test("cooling.aa-offset-calibrate", [("forgectrl", "src/cool.c")])
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

    def forge(self, art, edit):
        """The artifact with `edit` applied to its body and its self-hash
        computed again: what an edit of the file can always make."""
        body = json.loads(artifact.to_json(art))
        body.pop("sha256")
        edit(body)
        body["sha256"] = artifact.body_sha(body)
        return body

    def test_core_inherited_refused(self):
        art, _ = self.build()
        # a core record from an older campaign, marked as not inherited: the
        # record's campaign says what the flag would hide
        def older_core(body):
            for t in body["tests"]:
                if t["id"] == "image.health":
                    t["record"]["campaign"] = "c1"
                    t["inherited"] = False
        ok, rows, problems = artifact.verify(self.forge(art, older_core), self.release_manifest(), self.tests,
                                             self.chash)
        self.assertFalse(ok)
        self.assertTrue(any("always-required" in p for p in problems), problems)
        self.assertTrue(any("marked not inherited" in p for p in problems), problems)

    def test_a_forged_artifact_is_refused_for_what_it_claims(self):
        art, _ = self.build()

        def test_entry(body, tid):
            return [t for t in body["tests"] if t["id"] == tid][0]

        def failed(body):
            test_entry(body, "cooling.aa-offset-calibrate")["record"]["result"] = "FAIL"

        def no_record(body):
            test_entry(body, "cooling.aa-offset-calibrate")["record"] = None

        def extra(body):
            body["tests"].append(dict(test_entry(body, "forgectrl.panel"), id="no.such-test"))

        def covers(body):
            test_entry(body, "forgectrl.panel")["covers"] = [["forgectrl", "src/**"]]

        def fmt(body):
            body["format"] = artifact.FORMAT + 1

        for edit, said in ((failed, "recorded result is FAIL"), (no_record, "no PASS recorded"),
                           (extra, "in the artifact but not in the catalog"),
                           (covers, "definition differs"), (fmt, "artifact format")):
            with self.subTest(edit.__name__):
                ok, rows, problems = artifact.verify(self.forge(art, edit), self.release_manifest(), self.tests,
                                                     self.chash)
                self.assertFalse(ok)
                self.assertTrue(any(said in p for p in problems), problems)
        # a release manifest of another machine
        rel = self.release_manifest(helpers.with_platform(self.man2, machine="other"))
        ok, rows, problems = artifact.verify(art, rel, self.tests, self.chash, expect_machine="glowforge")
        self.assertTrue(any("machine 'other'" in p for p in problems), problems)

    def test_an_unanswered_fail_on_a_tests_fingerprint_refuses_it(self):
        # The cooling test failed in c2 (asking for a disposition), the
        # operator answered it, and the rerun passed: the artifact carries
        # the FAIL with its disposition. Without the disposition the gate
        # refuses the test.
        fp = self.cool.fingerprint(self.man2)
        self.recs.insert(-1, dict(rec_result("c2", self.cool, self.man2, "FAIL", "2026-08-21T10:01:30Z"),
                                  needs_disposition=True))
        self.recs.insert(-1, {"t": "campaign", "ts": "2026-08-21T10:01:40Z", "id": "c3",
                              "manifest_sha": self.man2.content_sha, "catalog_hash": self.chash,
                              "image": self.man2.version})
        self.recs.insert(-1, {"t": "disposition", "ts": "2026-08-21T10:01:50Z", "test": self.cool.id,
                              "fingerprint": fp, "reason": "the bench's chiller was off"})
        self.recs.insert(-1, rec_result("c3", self.cool, self.man2, "ABORTED", "2026-08-21T10:01:55Z"))
        self.recs[-1] = rec_result("c3", self.cool, self.man2, "PASS", "2026-08-21T10:02:00Z")
        self.recs.append(rec_result("c3", self.core, self.man2, "PASS", "2026-08-21T10:03:00Z"))
        art, st = self.build()
        self.assertTrue(art["authorized"], st["tests"])
        others = [t for t in art["tests"] if t["id"] == self.cool.id][0]["others"]
        self.assertEqual([(o["result"], (o["disposition"] or {}).get("reason")) for o in others],
                         [("FAIL", "the bench's chiller was off"), ("ABORTED", None)])
        self.assertTrue(artifact.verify(art, self.release_manifest(), self.tests, self.chash)[0])
        fail_seq = others[0]["seq"]

        def unanswered(body):
            for t in body["tests"]:
                for o in t["others"]:
                    o["disposition"] = None
        ok, rows, problems = artifact.verify(self.forge(art, unanswered), self.release_manifest(), self.tests,
                                             self.chash)
        self.assertFalse(ok)
        self.assertTrue(any("has no disposition" in p for p in problems), problems)

        # an invalidate-all after it answers nothing
        def invalidated(body):
            unanswered(body)
            body["invalidate_epoch"] = {"seq": fail_seq + 1, "ts": "2026-08-21T10:01:31Z", "reason": "rerun"}
        ok, rows, problems = artifact.verify(self.forge(art, invalidated), self.release_manifest(), self.tests,
                                             self.chash)
        self.assertTrue(any("has no disposition" in p for p in problems), problems)

    def test_an_unanswered_leftover_charged_to_a_pass_refuses_it(self):
        # What the next pre pass found after the panel test passed in c1 is
        # charged to it; answered, the artifact carries it with its answer.
        fp = self.ui.fingerprint(self.man2)
        self.recs.insert(3, {"t": "leftover", "ts": "2026-08-20T10:02:30Z", "campaign": "c1", "test": self.ui.id,
                             "fingerprint": fp, "found_before": "test cooling.aa-offset-calibrate",
                             "leftovers": [{"item": "cnc/motor_lock", "found": "15", "expected": "0",
                                            "action": "restored"}],
                             "needs_disposition": True, "message": "left behind"})
        self.recs.insert(4, {"t": "disposition", "ts": "2026-08-20T10:02:40Z", "test": self.ui.id,
                             "fingerprint": fp, "reason": "the operator jogged the head between the tests"})
        art, st = self.build()
        self.assertTrue(art["authorized"], st["tests"])
        others = [t for t in art["tests"] if t["id"] == self.ui.id][0]["others"]
        self.assertEqual([(o["result"], o["disposition"]["reason"]) for o in others],
                         [("LEFTOVER", "the operator jogged the head between the tests")])
        self.assertTrue(artifact.verify(art, self.release_manifest(), self.tests, self.chash)[0])

        def unanswered(body):
            for t in body["tests"]:
                for o in t["others"]:
                    o["disposition"] = None
        ok, rows, problems = artifact.verify(self.forge(art, unanswered), self.release_manifest(), self.tests,
                                             self.chash)
        self.assertTrue(any("LEFTOVER" in p and "has no disposition" in p for p in problems), problems)

        # Answered, but newer than the PASS: a leftover on the laser's safety
        # state (by its flag, or by the items it names with the flag gone),
        # or a FAIL or ERROR (one that asked for no disposition too), still
        # wants a newer PASS, which the gate derives from the places in the log.
        cases = (("LEFTOVER", {"safety": True}),
                 ("LEFTOVER", {"leftovers": [{"item": "laser_latch", "found": "unlocked", "expected": "locked",
                                              "action": "restored"}]}),
                 ("FAIL", {}),
                 ("ERROR", {"needs_disposition": None, "not_run": True}))
        for result, extra in cases:
            def newer(body, result=result, extra=extra):
                for t in body["tests"]:
                    if t["id"] == self.ui.id:
                        o = t["others"][0]
                        o.update(dict(extra, result=result, seq=t["record"]["seq"] + 1))
            with self.subTest(result=result, extra=sorted(extra)):
                ok, rows, problems = artifact.verify(self.forge(art, newer), self.release_manifest(), self.tests,
                                                     self.chash)
                self.assertFalse([r for r in rows if r["id"] == self.ui.id][0]["ok"])

    def test_a_pass_not_earned_on_its_pins_as_built_is_refused(self):
        # A forged artifact that claims authorization: one PASS recorded on a
        # hot-deployed binary, one with no verdict at all (an older tool's),
        # and verdicts that are not an object with a true "clean". Each is
        # a refused row, never a traceback.
        cases = [{"clean": False, "why": ["forgectrl: /usr/bin/forgectrl not as built"]}, None, "clean",
                 {"clean": "no"}, {"clean": True, "why": "not a list"}]
        for verdict in cases:
            art, _ = self.build()
            body = json.loads(artifact.to_json(art))
            body.pop("sha256")
            for t in body["tests"]:
                if t["id"] == "forgectrl.panel":
                    if verdict is None:
                        del t["record"]["provenance"]
                    else:
                        t["record"]["provenance"] = verdict
            body["sha256"] = manifest_mod.sha256_text(manifest_mod.canonical(body))
            ok, rows, problems = artifact.verify(body, self.release_manifest(), self.tests, self.chash)
            refused = sorted(r["id"] for r in rows if not r["ok"])
            expect = [] if isinstance(verdict, dict) and verdict.get("clean") is True else ["forgectrl.panel"]
            self.assertEqual(refused, expect, (verdict, problems))

    def test_invalidate_epoch(self):
        # An invalidate-all after campaign 2, then a full campaign 3: the
        # exporter carries the invalidate-all and its place in the log for
        # good, though its notice is gone.
        self.recs.append({"t": "invalidate", "ts": "2026-08-22T09:00:00Z", "reason": "tube"})
        self.recs.append(rec_campaign("c3", self.man2, self.chash, "2026-08-22T10:00:00Z"))
        for i, t in enumerate(self.tests):
            self.recs.append(rec_result("c3", t, self.man2, "PASS", "2026-08-22T10:0%d:00Z" % (i + 1)))
        art, _ = self.build()
        self.assertTrue(art["authorized"])
        self.assertIsNone(art["invalidate"])
        self.assertEqual(art["invalidate_epoch"]["seq"], 8)
        self.assertTrue(artifact.verify(art, self.release_manifest(), self.tests, self.chash)[0])
        # the panel's campaign 1 PASS put in, as an inherited one: it comes
        # before the invalidate-all in the log, though its clock time (a
        # board clock set later) reads after the invalidate-all's
        old = [r for r in self.recs if r.get("test") == "forgectrl.panel"][0]

        def inherited_old(body):
            for t in body["tests"]:
                if t["id"] == "forgectrl.panel":
                    t["record"] = dict(t["record"], campaign="c1", ts="2026-08-23T00:00:00Z",
                                       seq=self.recs.index(old) + 1)
                    t["inherited"] = True
        ok, rows, problems = artifact.verify(self.forge(art, inherited_old), self.release_manifest(),
                                             self.tests, self.chash)
        self.assertFalse(ok)
        self.assertTrue(any("before the invalidate-all" in p for p in problems), problems)

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
            if t["id"] == "cooling.aa-offset-calibrate":
                t["source_sha"] = "0" * 64
        body["sha256"] = manifest_mod.sha256_text(manifest_mod.canonical(body))
        ok, rows, problems = artifact.verify(body, self.release_manifest(), self.tests, self.chash)
        self.assertFalse(ok)
        self.assertTrue(any("implementation changed" in p for p in problems), problems)


REPO = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
GATE = os.path.join(REPO, "scripts", "acceptance-gate.py")


@unittest.skipUnless(shutil.which("ssh-keygen") and os.path.isfile(GATE), "needs ssh-keygen and the gate script")
class GateScriptTests(unittest.TestCase):
    """scripts/acceptance-gate.py run as release.sh runs it, on an artifact
    built for every test of the real catalog and signed with a bench key
    made for the run: it authorizes (0), refuses (1), or cannot read its
    input (2), and never with a traceback."""

    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.mkdtemp(prefix="forgetest-gate-")
        man = helpers.make_manifest()
        reg = catalog.load_suite()
        tests = catalog.all_tests(reg)
        chash = catalog.catalog_hash(reg)
        recs = [rec_campaign("c1", man, chash, "2026-08-20T10:00:00Z")]
        recs += [rec_result("c1", t, man, "PASS", "2026-08-20T11:00:00Z") for t in tests]
        st = campaign.compute(recs, tests, man, chash)
        assert st["authorized"], st["counts"]
        key = artifact.bench_key(os.path.join(cls.tmp, "bench-key"))
        cls.art = cls.put("acceptance.json", artifact.to_json(
            artifact.sign(artifact.build(st, tests, man, recs, chash), key)))
        with open(key + ".pub", encoding="utf-8") as f:
            pub = " ".join(f.read().split()[:2])
        cls.signers = cls.put("allowed_signers", 'forgetest-bench namespaces="forgetest-acceptance" %s\n' % pub)
        other = artifact.bench_key(os.path.join(cls.tmp, "other-key"))
        with open(other + ".pub", encoding="utf-8") as f:
            cls.strangers = cls.put("strangers", 'forgetest-bench namespaces="forgetest-acceptance" %s\n'
                                    % " ".join(f.read().split()[:2]))
        cls.man = cls.put("manifest.json", json.dumps(man.data))
        cls.other_machine = cls.put("other.json", json.dumps(helpers.with_platform(man, machine="other").data))

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.tmp, ignore_errors=True)

    @classmethod
    def put(cls, name, text):
        path = os.path.join(cls.tmp, name)
        with open(path, "w", encoding="utf-8") as f:
            f.write(text)
        return path

    def gate(self, art, man, signers):
        p = subprocess.run([sys.executable, "-B", GATE, art, man, "--machine", "glowforge", "--signers", signers],
                           capture_output=True, text=True, timeout=120)
        self.assertNotIn("Traceback", p.stderr + p.stdout)
        return p.returncode

    def test_only_a_bench_key_signing_as_the_bench_counts(self):
        # A key the signers file lists for another principal and namespace
        # (the sums key's), and a signature that names them: refused.
        other = artifact.bench_key(os.path.join(self.tmp, "sums-key"))
        with open(other + ".pub", encoding="utf-8") as f:
            pub = " ".join(f.read().split()[:2])
        signers = self.put("signers-with-sums", 'forgefirm-sums namespaces="forgefirm-sums" %s\n' % pub)
        with open(self.art, encoding="utf-8") as f:
            art = json.load(f)
        p = subprocess.run(["ssh-keygen", "-Y", "sign", "-f", other, "-n", "forgefirm-sums"],
                           input=art["sha256"].encode(), capture_output=True, check=True)
        art["signature"] = {"principal": "forgefirm-sums", "namespace": "forgefirm-sums",
                            "sig": p.stdout.decode()}
        self.assertNotEqual(artifact.signature_problems(art, signers), [])

    def test_the_gate_authorizes_refuses_and_reads(self):
        self.assertEqual(self.gate(self.art, self.man, self.signers), 0)
        # the same artifact for a build of another machine
        self.assertEqual(self.gate(self.art, self.other_machine, self.signers), 1)
        # signed by a key the tree does not hold
        self.assertEqual(self.gate(self.art, self.man, self.strangers), 1)
        # not an artifact at all
        self.assertEqual(self.gate(self.put("garbage.json", "[1, 2]\n"), self.man, self.signers), 2)


if __name__ == "__main__":
    unittest.main()
