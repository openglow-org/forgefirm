# Copyright 2026 514 LLC d/b/a OpenGlow
# Written by Scott Wiederhold
# https://community.openglow.org
# SPDX-License-Identifier:    MIT

"""The release artifact (acceptance.json / acceptance.md) and its verification.

Every test record is redacted before it enters the artifact (redact.py), so
the committed file carries no identity of the machine that ran the campaign.
It happens inside build(), ahead of the self-hash and the signature. The
gate reads none of the redacted fields.

The exporter serializes the campaign state with, for every catalog test,
the winning result record (same-campaign PASS or inherited PASS, with its
origin) and the fingerprint it was recorded under, every other result on
that fingerprint (each FAIL, ERROR and ABORTED run, with the disposition
that answered it), and the last invalidate-all with its place in the log.
The gate (scripts/acceptance-gate.py) checks the signature, then calls
verify() with the artifact, the release build's manifest, and the catalog
from the same source tree, and recomputes every fingerprint - a PASS
applies to the release exactly when the recomputation matches and the
PASS was recorded on its pins as built (provenance.py).

The artifact is signed with the bench key: an Ed25519 key the dev image
makes under the data directory at its first export (bench_key()), whose
private half never leaves the board. sign() signs the self-hash with
ssh-keygen in the namespace SIGN_NAMESPACE, and signature_problems() checks
the signature against the public halves committed in the forgefirm tree
(releases/allowed_signers, principal SIGN_PRINCIPAL). The self-hash alone
catches a damaged file, not an edited one: whoever edits the file can
compute it again. The signature is what an edit cannot make again without
the bench's key. Its limit: on the dev image root has no password and SSH
is open, and the daemon runs as root, so anyone with root on a listed
bench can read the key and sign an artifact that claims any result. The
signature proves which bench wrote the file and that nobody edited it
since, not that the campaign it describes ran.
"""
import base64
import hashlib
import json
import os
import subprocess
import tempfile

from . import VERSION
from . import campaign as _campaign
from . import manifest as _manifest
from . import provenance as _provenance
from .log import data_dir, now_ts
from .redact import scrub

FORMAT = 2
SIGN_PRINCIPAL = "forgetest-bench"
SIGN_NAMESPACE = "forgetest-acceptance"
# The fields outside the self-hash: the hash itself, and the signature over it.
UNHASHED = ("sha256", "signature")
_RESULT_KEYS = ("ts", "seq", "boot", "campaign", "test", "result", "fingerprint", "manifest_sha",
                "image", "duration_s", "message", "needs_disposition", "fixture_error", "provenance",
                "evidence", "answers", "log", "operator")
# What the artifact keeps of a result that is not the winning PASS.
_OTHER_KEYS = ("ts", "seq", "boot", "campaign", "result", "message", "needs_disposition",
               "fixture_error")


def _record(rec, keys=_RESULT_KEYS):
    return {k: rec.get(k) for k in keys if k in rec}


def body_sha(art):
    """The self-hash: the artifact without the fields UNHASHED names."""
    body = {k: v for k, v in art.items() if k not in UNHASHED}
    return _manifest.sha256_text(_manifest.canonical(body))


def build(state, tests, manifest, records, catalog_hash):
    """The artifact dict (self-hash included, unsigned). records: the full
    log, used to find the winning record for each test, the other results
    on its fingerprint, and the dispositions."""
    records = list(records)
    pos = _campaign.positions(records)
    disposals = {(r.get("test"), r.get("fingerprint"), pos[id(r)]): r
                 for r in records if r.get("t") == "disposition"}
    results = [r for r in records if r.get("t") == "result"]
    by_key = {}
    for r in results:
        by_key[(r.get("test"), r.get("ts"), r.get("campaign"))] = r

    def placed(rec, keys=_RESULT_KEYS):
        out = _record(rec, keys)
        out["seq"] = pos[id(rec)]
        return out

    def answer(rec):
        """The disposition that answers a result: the first after it."""
        later = sorted(p for (tid, fp, p) in disposals
                       if tid == rec.get("test") and fp == rec.get("fingerprint") and p > pos[id(rec)])
        if not later:
            return None
        d = disposals[(rec.get("test"), rec.get("fingerprint"), later[0])]
        return {"seq": later[0], "ts": d.get("ts"), "reason": d.get("reason")}

    tests_out = []
    for t in tests:
        st = state["tests"][t.id]
        origin = st.get("origin")
        rec = None
        if origin:
            rec = by_key.get((t.id, origin.get("ts"), origin.get("campaign")))
        others = []
        for r in results:
            if r.get("test") == t.id and r.get("fingerprint") == st["fingerprint"] \
                    and r.get("result") != _campaign.PASS:
                o = placed(r, _OTHER_KEYS)
                o["disposition"] = answer(r)
                others.append(o)
        entry = t.definition()
        entry.update({
            "title": t.title,
            "source_sha": t.source_sha,
            "fingerprint": st["fingerprint"],
            "satisfied": st["satisfied"],
            "inherited": st["status"] == "inherited",
            "record": scrub(placed(rec)) if rec else None,
            # every result on this fingerprint that is not a PASS
            "others": scrub(others),
        })
        tests_out.append(entry)

    art = {
        "format": FORMAT,
        "tool_version": VERSION,
        "exported_at": now_ts(),
        "image": {"name": manifest.image_name, "version": manifest.version},
        "manifest_sha": manifest.content_sha,
        "identity_sha": manifest.identity_sha(),
        "catalog_hash": catalog_hash,
        "campaign": state["campaign"],
        "invalidate": state["invalidate"],
        "invalidate_epoch": state.get("epoch"),
        "authorized": state["authorized"],
        "counts": state["counts"],
        "unclean": state.get("unclean", []),
        "manifest": manifest.data,
        "tests": tests_out,
    }
    art["sha256"] = body_sha(art)
    return art


# -- the bench key ----------------------------------------------------------

def bench_key(path=None):
    """The path of the bench's private signing key, made at the first call
    (ssh-keygen, Ed25519, 0600): data_dir()/bench-key, its public half
    beside it as bench-key.pub. A wiped data directory makes a new key,
    whose public half the release gate refuses until it is committed."""
    path = path or os.path.join(data_dir(), "bench-key")
    if not os.path.isfile(path):
        os.makedirs(os.path.dirname(path), exist_ok=True)
        subprocess.run(["ssh-keygen", "-q", "-t", "ed25519", "-N", "", "-C", SIGN_PRINCIPAL, "-f", path],
                       check=True, capture_output=True, timeout=60)
    return path


def key_fingerprint(pub_line):
    """ssh-keygen's SHA256 fingerprint of a public key line, or None."""
    try:
        blob = base64.b64decode(pub_line.split()[1], validate=True)
    except (IndexError, ValueError):
        return None
    return "SHA256:" + base64.b64encode(hashlib.sha256(blob).digest()).decode().rstrip("=")


def sign(art, key):
    """The artifact with its signature: ssh-keygen signs the self-hash in
    SIGN_NAMESPACE with the private key at `key`."""
    sha = body_sha(art)
    if art.get("sha256") != sha:
        raise ValueError("the artifact's self-hash is not its body's")
    p = subprocess.run(["ssh-keygen", "-Y", "sign", "-f", key, "-n", SIGN_NAMESPACE],
                       input=sha.encode(), capture_output=True, check=True, timeout=60)
    with open(key + ".pub", encoding="utf-8") as f:
        pub = f.read().strip()
    out = dict(art)
    out["signature"] = {"principal": SIGN_PRINCIPAL, "namespace": SIGN_NAMESPACE, "key": pub,
                        "fingerprint": key_fingerprint(pub), "sig": p.stdout.decode()}
    return out


def signature_problems(art, signers):
    """Why the artifact's signature does not verify against the allowed
    signers file `signers` (ssh-keygen's format; the bench keys under the
    principal SIGN_PRINCIPAL, restricted to SIGN_NAMESPACE): [] when it
    does. Everything that cannot be checked is a problem."""
    sig = art.get("signature")
    if not isinstance(sig, dict) or not isinstance(sig.get("sig"), str) or not sig["sig"].strip():
        return ["the artifact carries no bench signature"]
    sha = art.get("sha256")
    if not isinstance(sha, str) or sha != body_sha(art):
        return ["artifact self-hash mismatch (edited or truncated file)"]
    if not os.path.isfile(signers):
        return ["no allowed signers file at %s: the bench key's public half is committed there" % signers]
    who = sig.get("fingerprint") if isinstance(sig.get("fingerprint"), str) else "?"
    with tempfile.TemporaryDirectory() as d:
        path = os.path.join(d, "artifact.sig")
        with open(path, "w", encoding="utf-8") as f:
            f.write(sig["sig"])
        try:
            p = subprocess.run(["ssh-keygen", "-Y", "verify", "-f", signers, "-I", SIGN_PRINCIPAL,
                                "-n", SIGN_NAMESPACE, "-s", path],
                               input=sha.encode(), capture_output=True, timeout=60)
        except (OSError, subprocess.TimeoutExpired) as e:
            return ["the bench signature cannot be checked: %s" % e]
    if p.returncode != 0:
        why = (p.stderr or p.stdout).decode("utf-8", "replace").strip().splitlines()
        return ["the bench signature (key %s) does not verify against %s: %s"
                % (who, signers, why[-1] if why else "refused")]
    return []


def to_json(art):
    return json.dumps(art, sort_keys=True, indent=1) + "\n"


def to_markdown(art):
    lines = []
    img = art.get("image", {})
    lines.append("# ForgeFIRM acceptance - %s" % img.get("version", "?"))
    lines.append("")
    lines.append("- Image: `%s` (%s)" % (img.get("version", "?"), img.get("name", "?")))
    lines.append("- Manifest identity: `%s`" % (art.get("manifest_sha") or "?"))
    lines.append("- Catalog: `%s`" % (art.get("catalog_hash") or "?"))
    c = art.get("campaign") or {}
    lines.append("- Campaign: `%s` opened %s" % (c.get("id", "-"), c.get("ts", "-")))
    inv = art.get("invalidate")
    if inv:
        lines.append("- Full campaign required since %s: %s" % (inv.get("ts"), inv.get("reason")))
    ep = art.get("invalidate_epoch")
    if ep:
        lines.append("- Last invalidate-all: log record %s (%s): %s" % (ep.get("seq"), ep.get("ts"), ep.get("reason")))
    lines.append("- Exported: %s" % art.get("exported_at"))
    lines.append("- **Release authorized: %s**" % ("YES" if art.get("authorized") else "NO"))
    cnt = art.get("counts", {})
    lines.append("- Tests: %s total, %s satisfied (%s inherited), %s required"
                 % (cnt.get("total"), cnt.get("satisfied"), cnt.get("inherited"), cnt.get("required")))
    if art.get("unclean"):
        lines.append("- Satisfied on a build that is not its pins as built (never a release's): %s"
                     % ", ".join("`%s`" % t for t in art["unclean"]))
    lines.append("")
    lines.append("| Test | Kind | Result | Run at | Campaign | Inherited | Other results |")
    lines.append("|---|---|---|---|---|---|---|")
    for t in art.get("tests", []):
        rec = t.get("record") or {}
        kind = t.get("kind", "")
        if t.get("always"):
            kind += ", core"
        if t.get("hardware") == "takeover":
            kind += ", takeover"
        others = t.get("others") or []
        lines.append("| `%s` | %s | %s | %s | `%s` | %s | %s |" % (
            t.get("id"), kind, rec.get("result", "-"), rec.get("ts", "-"),
            rec.get("campaign", "-"), "yes" if t.get("inherited") else "no",
            ", ".join("%s%s" % (o.get("result"), " (disposed)" if o.get("disposition") else "")
                      for o in others) or "-"))
    lines.append("")
    lines.append("Artifact sha256: `%s`" % art.get("sha256"))
    sig = art.get("signature") or {}
    lines.append("Signed by the bench key `%s`" % sig.get("fingerprint") if sig else "Not signed")
    lines.append("")
    return "\n".join(lines)


def _seq(d):
    s = d.get("seq") if isinstance(d, dict) else None
    return s if isinstance(s, int) and not isinstance(s, bool) else None


def verify(art, release_manifest, tests, catalog_hash, expect_machine=None):
    """Gate decision on the artifact's content (the signature is
    signature_problems()'). Returns (ok, rows, problems).

    rows: per-test dicts for the report. problems: list of strings; empty
    means the artifact authorizes the release manifest. Nothing the
    artifact says about itself is taken on its word where it can be
    derived: whether a record was inherited is whether it came from
    another campaign than the artifact's, and its place in the log is
    judged against the last invalidate-all's.
    """
    problems = []
    rows = []

    if art.get("sha256") != body_sha(art):
        problems.append("artifact self-hash mismatch (edited or truncated file)")
        return False, rows, problems
    if art.get("format") != FORMAT:
        problems.append("artifact format %r, expected %r" % (art.get("format"), FORMAT))
    if not art.get("authorized"):
        problems.append("artifact does not claim authorization")

    if expect_machine and release_manifest.platform.get("machine") != expect_machine:
        problems.append("release manifest machine %r, expected %r"
                        % (release_manifest.platform.get("machine"), expect_machine))
    if art.get("catalog_hash") != catalog_hash:
        problems.append("catalog changed since the campaign (artifact %s, tree %s)"
                        % ((art.get("catalog_hash") or "?")[:12], catalog_hash[:12]))

    tests = list(tests)
    by_id = {t.id: t for t in tests}
    art_tests = {t["id"]: t for t in art.get("tests", [])}
    for tid in sorted(set(by_id) - set(art_tests)):
        problems.append("test %s is in the catalog but not in the artifact" % tid)
    for tid in sorted(set(art_tests) - set(by_id)):
        problems.append("test %s is in the artifact but not in the catalog" % tid)

    campaign_id = (art.get("campaign") or {}).get("id")
    epoch = art.get("invalidate_epoch")
    cut = None
    if epoch is not None:
        cut = _seq(epoch)
        if cut is None:
            problems.append("the invalidate-all carries no place in the log")
    for t in tests:
        a = art_tests.get(t.id)
        if a is None:
            continue
        rec = a.get("record")
        inherited = bool(rec) and rec.get("campaign") != campaign_id
        row = {"id": t.id, "always": t.always, "inherited": inherited}
        ok = True
        why = []
        others = a.get("others")
        if not isinstance(others, list):
            ok = False
            why.append("the results that are not a PASS are missing")
            others = []
        for o in others:
            if not isinstance(o, dict):
                ok = False
                why.append("a result that is not a PASS is malformed")
                continue
            if o.get("result") in _campaign.CLOSING and o.get("needs_disposition") is True \
                    and not o.get("disposition"):
                ok = False
                why.append("a %s (log record %s) has no disposition" % (o.get("result"), o.get("seq")))
        if a.get("covers") != [list(c) for c in t.covers] or a.get("always") != t.always \
                or a.get("kind") != t.kind or a.get("requires") != list(t.requires):
            ok = False
            why.append("definition differs from the catalog in the tree")
        if a.get("source_sha") != t.source_sha:
            ok = False
            why.append("test implementation changed since the campaign")
        if not rec:
            ok = False
            why.append("no PASS recorded")
        else:
            if rec.get("result") != _campaign.PASS:
                ok = False
                why.append("recorded result is %s" % rec.get("result"))
            # The release is its own build: a PASS recorded on a build other
            # than its pins as built speaks for neither, inherited or not.
            if not _provenance.clean(rec):
                ok = False
                prov = rec.get("provenance")
                said = prov.get("why") if isinstance(prov, dict) else None
                said = [str(w) for w in said[:3]] if isinstance(said, list) else []
                why.append("recorded on a build that is not its pins as built (%s)"
                           % ("; ".join(said) or "no usable provenance recorded"))
            fp = t.fingerprint(release_manifest)
            row["fingerprint"] = fp
            if rec.get("fingerprint") != fp:
                ok = False
                why.append("fingerprint differs from the release build (domain changed)")
            if bool(a.get("inherited")) != inherited:
                ok = False
                why.append("marked %sinherited, but its record is from campaign %s and the artifact's is %s"
                           % ("" if a.get("inherited") else "not ", rec.get("campaign"), campaign_id))
            if inherited and t.always:
                ok = False
                why.append("always-required test may not be inherited")
            if epoch is not None and (_seq(rec) is None or cut is None or _seq(rec) <= cut):
                ok = False
                why.append("PASS recorded before the invalidate-all (log record %s, the invalidate-all's %s)"
                           % (rec.get("seq"), cut))
            row["ts"] = rec.get("ts")
        row["ok"] = ok
        row["why"] = why
        rows.append(row)
        if not ok:
            problems.append("%s: %s" % (t.id, "; ".join(why)))
    return not problems, rows, problems
