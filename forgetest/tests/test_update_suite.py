# Copyright 2026 514 LLC d/b/a OpenGlow
# Written by Scott Wiederhold
# https://community.openglow.org
# SPDX-License-Identifier:    MIT

"""update.slots-and-signature against a scripted daemon and scripted
tools: /slots must mark booted and next boot the slots `ffboot -l` marks,
the drill starts only with a slot that is neither booted nor the next
boot, and the apply is judged refused at its signature gate on the job's
result and on the job's own record in the forgectrl log: its start, the
gate's refusal, and no fwup. The inventory is the bench reference's,
booted from its card with both firmware slots free."""
import json
import os
import re
import shutil
import tempfile
import unittest
from unittest import mock

import helpers
from forgetest import catalog
from forgetest.runner import Context, Failed, Run
from forgetest.suite import update

FFBOOT_L = """env.mmcdev=0
env.mmchwpart=0
env.mmcpart=1
env.mmcroot=/dev/mmcblk1p1
booted.root=/dev/mmcblk1p1
slot.sd.device=/dev/mmcblk1p1
slot.sd.present=yes
slot.sd.state=ok
slot.sd.type=forgefirm
slot.sd.version=0.0.11
slot.sd.kernel=yes
slot.sd.booted=yes
slot.sd.next=yes
slot.a.device=/dev/mmcblk2p1
slot.a.present=yes
slot.a.state=ok
slot.a.type=factory
slot.a.version=v2.6.0-2228
slot.a.kernel=yes
slot.b.device=/dev/mmcblk2p2
slot.b.present=yes
slot.b.state=ok
slot.b.type=forgefirm
slot.b.version=v0.0.9
slot.b.kernel=yes
"""
REFUSED = ("archive is not signed with the ForgeFIRM release key (confirm_unsigned=1 to apply "
           "anyway)")


def slots_body():
    """/slots as forgectrl builds it from FFBOOT_L."""
    entry = {"present": "yes", "state": "ok", "kernel": "yes", "booted": False, "next": False}
    return {"booted": "/dev/mmcblk1p1", "env": {}, "archives": [], "staged": {},
            "slots": {"a": dict(entry, device="/dev/mmcblk2p1", type="factory", version="v2.6.0-2228"),
                      "b": dict(entry, device="/dev/mmcblk2p2", type="forgefirm", version="v0.0.9"),
                      "sd": dict(entry, device="/dev/mmcblk1p1", type="forgefirm", version="0.0.11",
                                 booted=True, next=True)}}


class SlotsAndSignatureTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="forgetest-update-")
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.slots = slots_body()
        self.log = os.path.join(self.tmp, "forgectrl.log")
        open(self.log, "wb").close()
        key = os.path.join(self.tmp, "release.pub")
        open(key, "w").close()
        for name, value in (("RELEASE_KEY", key), ("STAGED_UPLOAD", os.path.join(self.tmp, "upload.fw")),
                            ("FORGECTRL_LOG", self.log)):
            p = mock.patch.object(update, name, value)
            p.start()
            self.addCleanup(p.stop)
        for target, name, value in ((update.hw, "run", self.run_tool),
                                    (Context, "sleep", lambda ctx, s: None)):      # the job's poll
            p = mock.patch.object(target, name, value)
            p.start()
            self.addCleanup(p.stop)
        self.fake = helpers.FakeForgectrl().start()
        self.addCleanup(self.fake.stop)
        self.apply_status = 202
        self.job_lines = ["update: writing the uploaded archive into slot %s",
                          "update: the apply failed: " + REFUSED]
        self.applied = []
        self.ffboot = FFBOOT_L
        self.result = {"ok": False, "error": REFUSED}
        self.recipes = []
        self.fake.on_get = self.on_get
        self.fake.on_post = self.on_post

    def run_tool(self, cmd, timeout=60):
        """ffboot and fwup as the drill calls them: the inventory, a garbage
        archive refused, a key pair and a signed archive made, the archive
        good for its own key and refused by the shipped one."""
        if cmd[:2] == ["ffboot", "-l"]:
            return 0, self.ffboot
        if cmd[0] == "sh":
            work = cmd[2].split("cd ", 1)[1].split(" ", 1)[0]
            if "fwup -c" in cmd[2]:
                with open(os.path.join(work, "fwup.conf")) as f:
                    self.recipes.append(f.read())
            made = "fwup-key.pub" if "fwup -g" in cmd[2] else "signed.fw"
            open(os.path.join(work, made), "w").close()
            return 0, ""
        if cmd[:2] == ["fwup", "-V"]:
            own = cmd[-1].endswith("fwup-key.pub")
            return (0, "") if own and cmd[3].endswith("signed.fw") else (1, "signature check failed")
        raise AssertionError("unexpected tool %s" % cmd)

    def on_get(self, path, q):
        if path == "/slots":
            return 200, self.slots
        if path == "/update/status":
            done = self.result if self.applied else None
            return 200, {"running": False, "result": done}
        return None

    def on_post(self, path, form):
        if path == "/update/apply":
            if self.apply_status != 202:
                return self.apply_status, {"error": "slot %s is selected for the next boot" % form.get("slot")}
            self.applied.append(form.get("slot"))
            with open(self.log, "a") as f:
                for ln in self.job_lines:
                    f.write("2026-11-18T20:00:00.000000+00:00 forgectrl[900] NOTICE " +
                            (ln % form["slot"] if "%s" in ln else ln) + "\n")
            return 202, {"started": True}
        return None

    def run_test(self):
        t = catalog.load_suite()["update.slots-and-signature"]
        run = Run("test", t.id, t.title)
        t.fn(Context(run, None, t))
        return run

    def assertFails(self, needle):
        with self.assertRaises(Failed) as cm:
            self.run_test()
        self.assertIn(needle, str(cm.exception))

    def test_an_apply_refused_at_its_signature_gate_passes(self):
        run = self.run_test()
        self.assertEqual(self.applied, ["b"])                   # free, and the last such
        # the archive's one task is complete: an apply asks fwup for
        # upgrade.<slot>, which it does not carry, so it writes nothing
        self.assertEqual(len(self.recipes), 1)
        self.assertEqual(re.findall(r"^task (\S+)", self.recipes[0], re.M), ["complete"])
        self.assertEqual(run.evidence["next_slots"], {"slots": ["sd"], "ffboot": ["sd"]})

    def test_a_refusal_the_log_does_not_record_at_the_gate_fails(self):
        # the job's result names the signature, but its outcome line is not
        # the signature gate's
        self.job_lines = self.job_lines[:1] + ["update: the apply failed: an archive of another product"]
        self.assertFails("does not record the signature gate's refusal")

    def test_an_apply_whose_record_names_fwup_fails(self):
        # the one outcome is the gate's refusal, yet the job's record names
        # fwup: the write was tried
        self.job_lines.insert(1, "update: fwup -a -q -d /dev/mmcblk2p2 -t upgrade.b")
        self.assertFails("got past its gates")

    def test_a_refusal_that_did_not_end_the_job_fails(self):
        # the signature gate's refusal, then the product gate's: two
        # outcomes, whatever the job's result says
        self.job_lines = self.job_lines + ["update: the apply failed: this archive is not firmware"]
        self.assertFails("2 outcomes of the apply")

    def test_a_job_result_that_names_another_refusal_fails(self):
        self.result = {"ok": False, "error": "this archive is not firmware"}
        self.assertFails("was not refused at its signature gate")

    def test_a_job_started_on_another_slot_fails(self):
        self.job_lines = ["update: writing the uploaded archive into slot a"] + self.job_lines[1:]
        self.assertFails("does not record the apply's start on slot b")

    def test_an_apply_refused_with_409_fails(self):
        # the slot was free at the start; a 409 now proves nothing
        self.apply_status = 409
        self.assertFails("the apply job did not start")
        self.assertEqual(self.applied, [])

    def test_slots_that_mark_another_next_boot_fail(self):
        self.slots["slots"]["b"]["next"] = True
        self.assertFails("/slots marks next")
        self.assertEqual(self.applied, [])

    def test_slots_that_mark_another_booted_slot_or_none_fail(self):
        self.slots["slots"]["sd"]["booted"] = False
        self.slots["slots"]["a"]["booted"] = True
        self.assertFails("/slots marks booted")
        # neither side marks a slot booted: no inventory of a running machine
        self.slots["slots"]["a"]["booted"] = False
        self.ffboot = FFBOOT_L.replace("slot.sd.booted=yes\n", "")
        self.assertFails("/slots marks booted [], ffboot -l []")
        self.assertEqual(self.applied, [])

    def test_the_drill_starts_only_with_a_free_slot(self):
        t = catalog.load_suite()["update.slots-and-signature"]
        self.assertIsNone(t.cannot_start())
        # booted from eMMC slot a, slot b selected for the next boot
        self.slots["slots"]["a"]["booted"] = True
        self.slots["slots"]["b"]["next"] = True
        self.assertIn("no firmware slot is free", t.cannot_start())
        self.assertIsNone(update.apply_target(json.loads(json.dumps(self.slots))))


if __name__ == "__main__":
    unittest.main()
