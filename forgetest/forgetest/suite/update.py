# Copyright 2026 514 LLC d/b/a OpenGlow
# Written by Scott Wiederhold
# https://community.openglow.org
# SPDX-License-Identifier:    MIT

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
                  "the update job before anything is written, and the forgectrl log records the "
                  "job's start and its refusal. Nothing is written to any slot.")
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
        log_path = "/data/log/forgefirm/forgectrl/forgectrl.log"
        try:
            log_from = os.path.getsize(log_path)
        except OSError:
            log_from = 0
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

            # The forgectrl log records the job: what it set out to write
            # and why it stopped.
            want = ["update: writing the uploaded archive into slot %s" % target,
                    "update: the apply failed: archive is not signed"]

            def log_text():
                try:
                    with open(log_path, "r", encoding="utf-8", errors="replace") as f:
                        f.seek(log_from)
                        return f.read()
                except OSError:
                    return ""
            ctx.wait_for(lambda: all(w in log_text() for w in want), 10, poll=0.5)
            text = log_text()
            ev["apply_log"] = [ln for ln in text.splitlines() if "update: " in ln][-4:]
            for w in want:
                ctx.check(w in text, "the forgectrl log lacks %r: %s", w, ev["apply_log"])
    finally:
        try:
            os.unlink(staged)
        except OSError:
            pass
        shutil.rmtree(work, ignore_errors=True)


@test("update.product-gate", title="The firmware upload takes firmware only: an extension package is refused by name",
      subsystem="update", kind="auto", est_min=1,
      covers=_UPDATE_COVERS + [("forgectrl", "src/fwproduct.c"), ("forgectrl", "src/fwproduct.h")],
      requires=["forgectrl.auth"],
      description="Firmware and extension packages are the same container, and a signature says who "
                  "made an archive, never what it is. Archives made on the spot with the machine's own "
                  "fwup go to POST /update/upload: an extension package unsigned, one signed with a "
                  "throwaway key, and one that carries a task are each refused with 400 in words that "
                  "say what they are, and nothing is staged; an archive of some other product is "
                  "refused as not firmware; and one whose product is ForgeFIRM firmware is taken as an "
                  "unsigned upload, which is what it is. Nothing is applied: the apply calls the same "
                  "gate before fwup -a, and its proof is the daemon's host test, because a catalog test "
                  "that could fail there would write a slot.")
def product_gate(ctx):
    fc = ctx.forgectrl
    ev = ctx.evidence
    staged = "/data/forgefirm/upload.fw"
    ctx.check(not os.path.exists(staged), "an upload is already staged: %s", staged)
    work = tempfile.mkdtemp(prefix="forgetest-gate-")

    def archive(name, product, task=False, sign=False):
        conf = os.path.join(work, name + ".conf")
        with open(conf, "w") as f:
            f.write('meta-product = "%s"\nmeta-version = "0.0.0-test"\n'
                    'file-resource payload.tar.gz { host-path = "payload.tar.gz" }\n' % product)
            if task:
                f.write('task upgrade.a { on-resource payload.tar.gz { raw_write(0) } }\n')
        out = name + ".fw"
        cmd = "cd %s && fwup -c -f %s.conf -o %s" % (work, name, out)
        if sign:
            cmd += " && fwup -S -s fwup-key.priv -i %s -o %s.signed && mv %s.signed %s" % (out, out, out, out)
        rc, text = hw.run(["sh", "-c", cmd], timeout=60)
        ctx.check(rc == 0, "could not make %s (rc %s): %s", name, rc, text.strip()[:200])
        return os.path.join(work, out)

    def upload(path):
        mark = "forgetestGateBoundary9c2"
        body = ('--%s\r\nContent-Disposition: form-data; name="file"; filename="%s"\r\n'
                'Content-Type: application/octet-stream\r\n\r\n' % (mark, os.path.basename(path))).encode()
        with open(path, "rb") as f:
            body += f.read() + ("\r\n--%s--\r\n" % mark).encode()
        return fc.post("/update/upload", data=body,
                       headers={"Content-Type": "multipart/form-data; boundary=%s" % mark})

    try:
        with open(os.path.join(work, "payload.tar.gz"), "wb") as f:
            f.write(b"forgetest product-gate drill\n" * 8)
        rc, text = hw.run(["sh", "-c", "cd %s && fwup -g" % work], timeout=60)
        ctx.check(rc == 0, "fwup -g did not make a key pair (rc %s): %s", rc, text.strip()[:200])
        seen = {}
        for name, product, task, sign in (("extension-unsigned", "ForgeFIRM extension", False, False),
                                          ("extension-signed", "ForgeFIRM extension", False, True),
                                          ("extension-with-task", "ForgeFIRM extension", True, False)):
            st, body = upload(archive(name, product, task, sign))
            words = body.get("error", "") if isinstance(body, dict) else str(body)
            seen[name] = [st, words]
            ctx.log("upload %s -> %s %s", name, st, words)
            ctx.check(st == 400 and "an extension package, not firmware" in words,
                      "%s at the firmware upload -> %s %s", name, st, words)
            ctx.check(not os.path.exists(staged), "%s was refused and is staged all the same", name)
        st, body = upload(archive("another-product", "forgetest"))
        words = body.get("error", "") if isinstance(body, dict) else str(body)
        seen["another-product"] = [st, words]
        ctx.log("upload another-product -> %s %s", st, words)
        ctx.check(st == 400 and "not firmware" in words and not os.path.exists(staged),
                  "an archive of another product -> %s %s", st, words)
        st, body = upload(archive("firmware-unsigned", "ForgeFIRM firmware"))
        seen["firmware-unsigned"] = [st, body]
        ctx.log("upload firmware-unsigned -> %s %s", st, body)
        ctx.check(st == 200 and isinstance(body, dict) and body.get("signature") == "unsigned"
                  and os.path.exists(staged), "an unsigned archive that is firmware -> %s %s", st, body)
        ev["uploads"] = seen
    finally:
        try:
            os.unlink(staged)
        except OSError:
            pass
        shutil.rmtree(work, ignore_errors=True)
    ctx.log("PASS: three extension packages and one foreign product refused at the firmware upload with "
            "nothing staged; firmware taken as the unsigned upload it is")
