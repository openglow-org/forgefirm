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
# The firmware slots an apply can name.
SLOTS = ("a", "b")
RELEASE_KEY = "/etc/forgefirm/keys/forgefirm-release.pub"
STAGED_UPLOAD = "/data/forgefirm/upload.fw"
FORGECTRL_LOG = "/data/log/forgefirm/forgectrl/forgectrl.log"
# The update job's refusal at its signature gate (forgectrl update.c,
# apply_worker): the job's result carries it, and the job's outcome line
# in the log ("update: the apply failed: ...") repeats it.
SIGNATURE_REFUSAL = "archive is not signed with the ForgeFIRM release key"


def ffboot_marked(text, what):
    """The slots `ffboot -l` marks slot.<name>.<what>=yes (booted, next)."""
    out = set()
    for ln in (text or "").splitlines():
        key, eq, value = ln.strip().partition("=")
        parts = key.split(".")
        if eq and value == "yes" and len(parts) == 3 and parts[0] == "slot" and parts[2] == what:
            out.add(parts[1])
    return out


def slots_marked(slots, what):
    """The slots /slots marks `what` (booted, next)."""
    return {n for n, si in (slots.get("slots") or {}).items() if isinstance(si, dict) and si.get(what) is True}


def apply_target(slots):
    """The firmware slot the apply drill names: one that is neither booted
    nor selected for the next boot (an apply refuses those for a reason of
    its own, before the signature gate the drill is for), the last such in
    the inventory. None when there is none."""
    free = [n for n in SLOTS if isinstance((slots.get("slots") or {}).get(n), dict)
            and not slots["slots"][n].get("booted") and not slots["slots"][n].get("next")]
    return free[-1] if free else None


def apply_slot_free():
    """Precheck: a firmware slot the apply drill can name. With each slot
    booted or selected for the next boot, the apply is refused before its
    signature gate, so the drill could prove nothing."""
    try:
        st, slots = hw.Forgectrl().get("/slots")
    except hw.HwError as e:
        return "forgectrl unreachable: %s" % e
    if st != 200 or not isinstance(slots, dict):
        return "GET /slots -> %s" % st
    if apply_target(slots) is None:
        return ("no firmware slot is free for the apply drill: each one is the booted slot or the one "
                "selected for the next boot, which an apply refuses before its signature gate")
    return None


def apply_refused_at_the_gate(ctx, target, result, lines):
    """The apply is judged refused at its signature gate: the job's result
    names the signature, the job's log (`lines`, the forgectrl log's lines
    since the apply) holds its start on the drill's slot and one outcome,
    the gate's refusal, and no line of it names fwup (the gates passed and
    the write tried). A second outcome is a job the refusal did not end."""
    update = [ln for ln in lines if "update: " in ln]
    ctx.check(isinstance(result, dict) and result.get("ok") is False
              and str(result.get("error", "")).startswith(SIGNATURE_REFUSAL),
              "the apply of a foreign-signed archive was not refused at its signature gate: %s", result)
    ctx.check(any(("update: writing the uploaded archive into slot %s" % target) in ln for ln in update),
              "the forgectrl log does not record the apply's start on slot %s: %s", target, update[-4:])
    outcomes = [ln for ln in update if "update: the apply " in ln]
    ctx.check(len(outcomes) == 1, "the forgectrl log records %d outcomes of the apply, not one: %s",
              len(outcomes), outcomes)
    ctx.check(("update: the apply failed: " + SIGNATURE_REFUSAL) in outcomes[0],
              "the forgectrl log does not record the signature gate's refusal: %s", outcomes[0])
    past = [ln for ln in update if "fwup" in ln]
    ctx.check(not past, "the apply got past its gates (fwup ran): %s", past)


@test("update.slots-and-signature", title="Boot slots readable, unsigned and foreign-signed archives refused",
      subsystem="update", kind="auto", est_min=2,
      covers=_UPDATE_COVERS, requires=["forgectrl.auth"], precheck=apply_slot_free,
      description="/slots marks booted and next boot the slots the machine's own `ffboot -l` "
                  "marks (how forgectrl parses the rest of the inventory is its host test, "
                  "host_routes.sh); /update/status answers; `fwup` refuses a garbage archive. A tiny "
                  "archive signed with a throwaway key made on the spot verifies with its own key "
                  "and fails against the shipped release key. An apply of it without "
                  "confirm_unsigned, naming a slot that is neither booted nor the next boot, is "
                  "refused at the update job's signature gate: the job's result names the "
                  "signature, and the forgectrl log records the job's start and that refusal and "
                  "no fwup. The archive carries no task an apply runs (its one task is `complete`, "
                  "and an apply asks fwup for upgrade.<slot>), so an apply that got past its gates "
                  "would still write nothing: the drill never writes a slot. The test starts only "
                  "with such a slot to name.")
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
    for what in ("booted", "next"):
        mine, its = slots_marked(slots, what), ffboot_marked(out, what)
        ev["%s_slots" % what] = {"slots": sorted(mine), "ffboot": sorted(its)}
        ctx.check(mine == its and (its or what == "next"), "/slots marks %s %s, ffboot -l %s", what,
                  sorted(mine), sorted(its))

    st, us = fc.get("/update/status")
    ctx.log("GET /update/status -> %s %s", st, us)
    ctx.check(st == 200 and isinstance(us, dict) and "running" in us, "GET /update/status -> %s", st)
    ctx.check(not us.get("running"), "an update is running")

    key = RELEASE_KEY
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
    # apply job must refuse it without confirm_unsigned, at its signature
    # gate. Its one task is `complete`: an apply asks fwup for
    # upgrade.<slot>, which this archive does not carry, so not even an
    # apply past every gate would write the slot.
    work = tempfile.mkdtemp(prefix="forgetest-fw-")
    staged = STAGED_UPLOAD
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
        signed = os.path.join(work, "signed.fw")
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

        # The apply path. The target is a slot neither booted nor selected
        # for the next boot: an apply refuses those for a reason of its own,
        # before the signature gate this drill is for.
        target = apply_target(slots)
        ctx.check(target is not None, "no firmware slot is free for the apply (each is booted or the next "
                  "boot): %s", slots)
        ev["target"] = target
        shutil.copyfile(signed, staged)
        log_from = hw.log_size(FORGECTRL_LOG)
        st, body = fc.post("/update/apply", params={"slot": target, "file": "upload"})
        ev["apply"] = {"status": st, "body": body}
        ctx.log("POST /update/apply slot=%s file=upload (no confirm_unsigned) -> %s %s", target, st, body)
        ctx.check(st == 202 and isinstance(body, dict) and body.get("started") is True,
                  "the apply job did not start: %s %s (the drill's slot was free at its start)", st, body)
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

        # The forgectrl log records the job: what it set out to write and
        # why it stopped. Its lines reach the file through rsyslog a
        # moment after the job's end.
        outcome = "update: the apply failed: "
        ctx.wait_for(lambda: outcome in (hw.log_window(FORGECTRL_LOG, log_from) or ""), 10, poll=0.5)
        text = hw.log_window(FORGECTRL_LOG, log_from)
        ctx.check(text is not None, "%s since the apply cannot be read (rotated away, or unreadable): the "
                  "job's record cannot be judged", FORGECTRL_LOG)
        lines = text.splitlines()
        ev["apply_log"] = [ln for ln in lines if "update: " in ln][-4:]
        apply_refused_at_the_gate(ctx, target, result, lines)
    finally:
        try:
            os.unlink(staged)
        except OSError:
            pass
        shutil.rmtree(work, ignore_errors=True)
