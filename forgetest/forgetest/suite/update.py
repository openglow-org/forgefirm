"""update.* - the A/B slot inventory and the firmware verification path."""
import os
import tempfile
import time
import shutil

from ..catalog import test
from .. import hw

_UPDATE_COVERS = [("forgectrl", "src/update.c"), ("forgectrl", "src/update.h"),
                  ("forgectrl", "src/relcheck.c"), ("forgectrl", "src/relcheck.h"),
                  ("ffboot", "**")]


@test("update.slots-and-signature", title="Boot slots readable, unsigned and foreign-signed archives refused",
      subsystem="update", kind="auto", est_min=2,
      covers=_UPDATE_COVERS, requires=["forgectrl.auth"],
      description="/slots reports the A/B inventory consistent with `ffboot -l`; /update/status "
                  "answers; `fwup` refuses a garbage archive. A tiny archive signed with a "
                  "throwaway key made on the spot verifies with its own key and fails against the "
                  "shipped release key, and an apply of it without confirm_unsigned is refused by "
                  "the update job before anything is written. Nothing is written to any slot.")
def slots_and_signature(ctx):
    fc = ctx.forgectrl
    ev = ctx.evidence
    st, slots = fc.get("/slots")
    ctx.log("GET /slots -> %s %s", st, slots)
    ctx.check(st == 200 and isinstance(slots, dict), "GET /slots -> %s", st)
    ev["slots"] = slots
    rc, out = hw.run(["ffboot", "-l"])
    ev["ffboot_l_rc"] = rc
    ctx.log("ffboot -l -> rc %s\n%s", rc, out.strip())
    ctx.check(rc == 0, "ffboot -l failed (%s)", rc)
    text = str(slots).lower()
    ctx.check("forgefirm" in text or "slot" in text, "/slots does not look like a slot inventory")

    st, us = fc.get("/update/status")
    ctx.log("GET /update/status -> %s %s", st, us)
    ctx.check(st == 200 and isinstance(us, dict) and "running" in us, "GET /update/status -> %s", st)
    ctx.check(not us.get("running"), "an update is running")

    key = "/etc/forgefirm/keys/forgefirm-release.pub"
    ctx.check(os.path.exists(key), "release key %s missing", key)
    with tempfile.NamedTemporaryFile(prefix="forgetest-", suffix=".fw", delete=False) as f:
        f.write(b"this is not a firmware archive" * 64)
        garbage = f.name
    try:
        rc, out = hw.run(["fwup", "-V", "-i", garbage, "-p", key], timeout=30)
        ev["fwup_garbage_rc"] = rc
        ctx.log("fwup -V garbage -> rc %s: %s", rc, out.strip()[:200])
        ctx.check(rc != 0, "fwup accepted a garbage archive")
    finally:
        os.unlink(garbage)

    # A foreign signature: a throwaway key pair signs a tiny archive. The
    # shipped key must refuse it, its own key must accept it, and the
    # apply job must refuse it without confirm_unsigned, before it
    # touches the slot.
    work = tempfile.mkdtemp(prefix="forgetest-fw-")
    staged = "/data/forgefirm/upload.fw"
    try:
        with open(os.path.join(work, "note.txt"), "w") as f:
            f.write("forgetest foreign-signature drill\n")
        with open(os.path.join(work, "fwup.conf"), "w") as f:
            f.write('meta-product = "forgetest"\nmeta-version = "0.0.0-test"\n'
                    'file-resource note.txt { host-path = "note.txt" }\n'
                    'task complete { on-resource note.txt { raw_write(0) } }\n')
        rc, out = hw.run(["sh", "-c", "cd %s && fwup -g" % work], timeout=60)
        ev["fwup_gen_rc"] = rc
        ctx.check(rc == 0 and os.path.exists(os.path.join(work, "fwup-key.pub")),
                  "fwup -g did not make a key pair (rc %s): %s", rc, out.strip()[:200])
        plain, signed = os.path.join(work, "plain.fw"), os.path.join(work, "signed.fw")
        rc, out = hw.run(["sh", "-c", "cd %s && fwup -c -f fwup.conf -o plain.fw && "
                          "fwup -S -s fwup-key.priv -i plain.fw -o signed.fw" % work], timeout=60)
        ctx.check(rc == 0 and os.path.exists(signed), "could not make the signed archive (rc %s): %s",
                  rc, out.strip()[:200])
        rc_own, _ = hw.run(["fwup", "-V", "-i", signed, "-p", os.path.join(work, "fwup-key.pub")], timeout=30)
        rc_ship, out = hw.run(["fwup", "-V", "-i", signed, "-p", key], timeout=30)
        ev["fwup_foreign"] = {"own_key_rc": rc_own, "shipped_key_rc": rc_ship}
        ctx.log("fwup -V signed: own key rc %s, shipped key rc %s", rc_own, rc_ship)
        ctx.check(rc_own == 0, "the archive does not verify with the key that signed it")
        ctx.check(rc_ship != 0, "the shipped release key accepted a foreign signature")

        # The apply path. The target is the slot not booted; a slot already
        # selected for the next boot is refused for its own reason, which
        # this drill records and steps over.
        target = None
        for name, si in (slots.get("slots") or {}).items():
            if name in ("a", "b") and isinstance(si, dict) and not si.get("booted"):
                target = name
        ctx.check(target is not None, "no inactive firmware slot in /slots: %s", slots)
        shutil.copyfile(signed, staged)
        st, body = fc.post("/update/apply", params={"slot": target, "file": "upload"})
        ev["apply"] = {"status": st, "body": body}
        ctx.log("POST /update/apply slot=%s file=upload (no confirm_unsigned) -> %s %s", target, st, body)
        if st == 409 and isinstance(body, dict) and "next boot" in str(body.get("error", "")):
            ctx.log("slot %s is selected for the next boot: the apply is refused before the "
                    "signature check, which is its own guard", target)
        else:
            ctx.check(st == 202 and isinstance(body, dict) and body.get("started") is True,
                      "the apply job did not start: %s %s", st, body)
            result = None
            t0 = time.time()
            while time.time() - t0 < 60:
                ctx.sleep(1)
                st, us = fc.get("/update/status")
                if isinstance(us, dict) and not us.get("running"):
                    result = us.get("result")
                    break
            ev["apply_result"] = result
            ctx.log("apply result: %s", result)
            ctx.check(isinstance(result, dict) and result.get("ok") is False
                      and "not signed" in str(result.get("error", "")),
                      "the apply of a foreign-signed archive was not refused for its signature: %s", result)
    finally:
        try:
            os.unlink(staged)
        except OSError:
            pass
        shutil.rmtree(work, ignore_errors=True)
