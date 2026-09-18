# Copyright 2026 514 LLC d/b/a OpenGlow
# Written by Scott Wiederhold
# https://community.openglow.org
# SPDX-License-Identifier:    MIT

"""The release artifact (acceptance.json / acceptance.md) and its verification.

The exporter serializes the campaign state with, for every catalog test,
the winning result record (same-campaign PASS or inherited PASS, with its
origin) and the fingerprint it was recorded under. The gate
(scripts/acceptance-gate.py) calls verify() with the artifact, the
release build's manifest, and the catalog from the same source tree, and
recomputes every fingerprint - a PASS applies to the release exactly when
the recomputation matches. The artifact is self-hashed so an edited file
is caught before any of that.
"""
import json

from . import VERSION
from . import campaign as _campaign
from . import manifest as _manifest
from .log import now_ts

FORMAT = 1
_RESULT_KEYS = ("ts", "campaign", "test", "result", "fingerprint", "manifest_sha", "image",
                "duration_s", "message", "evidence", "answers", "log", "operator")


def _record(rec):
    return {k: rec.get(k) for k in _RESULT_KEYS if k in rec}


def build(state, tests, manifest, records, catalog_hash):
    """The artifact dict (self-hash included). records: the full log,
    used to find the winning record for each test."""
    results = [r for r in records if r.get("t") == "result"]
    by_key = {}
    for r in results:
        by_key[(r.get("test"), r.get("ts"), r.get("campaign"))] = r

    tests_out = []
    for t in tests:
        st = state["tests"][t.id]
        origin = st.get("origin")
        rec = None
        if origin:
            rec = by_key.get((t.id, origin.get("ts"), origin.get("campaign")))
        entry = t.definition()
        entry.update({
            "title": t.title,
            "source_sha": t.source_sha,
            "fingerprint": st["fingerprint"],
            "satisfied": st["satisfied"],
            "inherited": st["status"] == "inherited",
            "record": _record(rec) if rec else None,
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
        "authorized": state["authorized"],
        "counts": state["counts"],
        "manifest": manifest.data,
        "tests": tests_out,
    }
    art["sha256"] = _manifest.sha256_text(_manifest.canonical(art))
    return art


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
    lines.append("- Exported: %s" % art.get("exported_at"))
    lines.append("- **Release authorized: %s**" % ("YES" if art.get("authorized") else "NO"))
    cnt = art.get("counts", {})
    lines.append("- Tests: %s total, %s satisfied (%s inherited), %s required"
                 % (cnt.get("total"), cnt.get("satisfied"), cnt.get("inherited"), cnt.get("required")))
    lines.append("")
    lines.append("| Test | Kind | Result | Run at | Campaign | Inherited |")
    lines.append("|---|---|---|---|---|---|")
    for t in art.get("tests", []):
        rec = t.get("record") or {}
        kind = t.get("kind", "")
        if t.get("always"):
            kind += ", core"
        if t.get("hardware") == "takeover":
            kind += ", takeover"
        lines.append("| `%s` | %s | %s | %s | `%s` | %s |" % (
            t.get("id"), kind, rec.get("result", "-"), rec.get("ts", "-"),
            rec.get("campaign", "-"), "yes" if t.get("inherited") else "no"))
    lines.append("")
    lines.append("Artifact sha256: `%s`" % art.get("sha256"))
    lines.append("")
    return "\n".join(lines)


def verify(art, release_manifest, tests, catalog_hash, expect_machine=None):
    """Gate decision. Returns (ok, rows, problems).

    rows: per-test dicts for the report. problems: list of strings; empty
    means the artifact authorizes the release manifest.
    """
    problems = []
    rows = []

    body = dict(art)
    sha = body.pop("sha256", None)
    if sha != _manifest.sha256_text(_manifest.canonical(body)):
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

    epoch = (art.get("invalidate") or {}).get("ts")
    for t in tests:
        a = art_tests.get(t.id)
        if a is None:
            continue
        row = {"id": t.id, "always": t.always, "inherited": bool(a.get("inherited"))}
        rec = a.get("record")
        ok = True
        why = []
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
            fp = t.fingerprint(release_manifest)
            row["fingerprint"] = fp
            if rec.get("fingerprint") != fp:
                ok = False
                why.append("fingerprint differs from the release build (domain changed)")
            if a.get("inherited"):
                if t.always:
                    ok = False
                    why.append("always-required test may not be inherited")
                if epoch and (rec.get("ts") or "") <= epoch:
                    ok = False
                    why.append("inherited PASS predates the invalidate-all")
            row["ts"] = rec.get("ts")
        row["ok"] = ok
        row["why"] = why
        rows.append(row)
        if not ok:
            problems.append("%s: %s" % (t.id, "; ".join(why)))
    return not problems, rows, problems
