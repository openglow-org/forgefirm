# Copyright 2026 514 LLC d/b/a OpenGlow
# Written by Scott Wiederhold
# https://community.openglow.org
# SPDX-License-Identifier:    MIT

"""Campaign rules: which results apply, what is required, and whether a
release is authorized.

A campaign is bound to one image (manifest content hash) and one catalog
(catalog hash). It is open from its record until a FAIL/ERROR result in
it, an invalidate-all, an explicit reset, or a different image or
catalog. Rules per test T with domain fingerprint F(T):

  blocked                a FAIL, or an ERROR the test raised (not one the
                         bench fixture raised), recorded with
                         "needs_disposition" under F(T) blocks F(T) until
                         the operator records a disposition for T and F(T)
                         after it, with a reason (reason: disposition). A
                         rerun that passes does not clear it, and neither
                         does an invalidate-all.
  satisfied by campaign  a PASS in the open campaign with fingerprint F(T)
  satisfied by inheritance  (never for the always-required core) the newest
                         PASS anywhere in the history with fingerprint F(T),
                         after the last invalidate-all, and recorded on its
                         pins as built (provenance.py); a PASS that was not
                         is never inherited
  required               otherwise (reason: always / never-passed /
                         domain-changed / failed-since / not-as-built)

  authorized  <=>  a campaign is open, every catalog test is satisfied,
                   and every PASS that satisfies one was recorded on its
                   pins as built

"After" and "before" are places in the log ("seq", log.py), never the
records' clock times: the board has no battery-backed clock, so a record
written before the clock is set carries an older time than the records
before it.

Pure functions over the record list; nothing here touches hardware or
the filesystem, which keeps the rules unit-testable and lets the release
gate reason with the same code.
"""
from .provenance import clean

PASS, FAIL, ERROR, ABORTED = "PASS", "FAIL", "ERROR", "ABORTED"
CLOSING = (FAIL, ERROR)


def positions(records):
    """{id(record): its place in the log}: the "seq" the log gave it, or
    its index in the list (from 1) for a record that carries none."""
    out = {}
    for i, r in enumerate(records):
        s = r.get("seq")
        out[id(r)] = s if isinstance(s, int) and not isinstance(s, bool) else i + 1
    return out


def dispositions(records, pos):
    """{(test, fingerprint): place of the newest disposition record}."""
    out = {}
    for r in records:
        if r.get("t") == "disposition":
            k = (r.get("test"), r.get("fingerprint"))
            out[k] = max(out.get(k, 0), pos[id(r)])
    return out


def blocks(rec, pos, disposed):
    """Whether a result record blocks its test's fingerprint: a FAIL or an
    ERROR that asks for a disposition and that no disposition after it
    answers. An invalidate-all answers nothing: its reason is the bench's,
    not the FAIL's, so a FAIL holds its fingerprint whatever invalidate-alls
    follow it (the fingerprint already limits it to unchanged code). A
    disposition at the result's own place answers it no more than one
    before it does, as the exporter and the gate read it."""
    return (rec.get("t") == "result" and rec.get("result") in CLOSING
            and rec.get("needs_disposition") is True
            and disposed.get((rec.get("test"), rec.get("fingerprint")), 0) <= pos[id(rec)])


def _summary(rec):
    if rec is None:
        return None
    keys = ("ts", "result", "campaign", "image", "fingerprint", "duration_s", "message")
    return {k: rec.get(k) for k in keys if k in rec}


def open_campaign(records, manifest_sha, catalog_hash):
    """(open_campaign_record_or_None, last_campaign_record_or_None,
    closed_by_or_None, invalidate_record_or_None)."""
    current = None
    closed_by = None
    invalidate = None
    for r in records:
        t = r.get("t")
        if t == "campaign":
            current = r
            closed_by = None
        elif t == "result":
            if current and r.get("campaign") == current.get("id") and r.get("result") in CLOSING:
                closed_by = closed_by or "fail"
        elif t == "invalidate":
            invalidate = r
            if current:
                closed_by = closed_by or "invalidate"
        elif t == "reset":
            if current:
                closed_by = closed_by or "reset"
    if current and not closed_by:
        if current.get("manifest_sha") != manifest_sha:
            closed_by = "image"
        elif current.get("catalog_hash") != catalog_hash:
            closed_by = "catalog"
    return (current if current and not closed_by else None), current, closed_by, invalidate


def compute(records, tests, manifest, catalog_hash, running=None):
    """The state the page shows and the exporter serializes.

    tests: iterable of catalog.Test. running: id of the test in progress
    (marked in the per-test status), or None.
    """
    records = list(records)
    pos = positions(records)
    results = [r for r in records if r.get("t") == "result"]
    campaign, last, closed_by, invalidate = open_campaign(records, manifest.content_sha, catalog_hash)
    # The invalidate-all's place in the log: nothing at or before it counts.
    cut = pos[id(invalidate)] if invalidate else None
    disposed = dispositions(records, pos)

    def after_cut(r):
        return cut is None or pos[id(r)] > cut

    open_id = campaign.get("id") if campaign else None
    # The invalidate record keeps its epoch for good: nothing before it in
    # the log inherits. The page's notice (and the artifact's "invalidate")
    # shows it only while it still asks for something, that is until a
    # campaign has started after it; "epoch" carries it always.
    invalidate_shown = invalidate
    if invalidate:
        seen = False
        for r in records:
            if r is invalidate:
                seen = True
            elif seen and r.get("t") == "campaign":
                invalidate_shown = None
                break

    by_test = {}
    for r in results:
        by_test.setdefault(r.get("test"), []).append(r)

    out_tests = {}
    unclean = []
    tests = list(tests)
    for t in tests:
        fp = t.fingerprint(manifest)
        hist = by_test.get(t.id, [])
        last_r = hist[-1] if hist else None
        origin = None
        status = "none"
        satisfied = False
        reason = None
        same = None
        if open_id:
            for r in reversed(hist):
                if r.get("campaign") == open_id and r.get("result") == PASS and r.get("fingerprint") == fp:
                    same = r
                    break
        if any(blocks(r, pos, disposed) for r in hist if r.get("fingerprint") == fp):
            # A FAIL or ERROR on this fingerprint that nobody has answered:
            # a rerun that passes does not clear it, a disposition does.
            reason = "disposition"
        elif same is not None:
            status, satisfied, origin, reason = "pass", True, same, "campaign"
        elif t.always:
            reason = "always"
        else:
            # The newest record on the current fingerprint decides: a PASS
            # is inherited, a FAIL or ERROR after it blocks the inheritance
            # (the test has to be run again), an ABORTED run says nothing,
            # and neither does a PASS recorded on a build other than its
            # pins as built.
            inh = None
            blocked = None
            for r in reversed(hist):
                if r.get("fingerprint") != fp:
                    continue
                if not after_cut(r):
                    continue
                res = r.get("result")
                if res == PASS and clean(r):
                    inh = r
                    break
                if res in (FAIL, ERROR):
                    blocked = r
                    break
            if inh is not None:
                status, satisfied, origin, reason = "inherited", True, inh, "inherited"
            elif blocked is not None and any(r.get("result") == PASS and r.get("fingerprint") == fp
                                             and pos[id(r)] < pos[id(blocked)]
                                             for r in hist):
                reason = "failed-since"
            elif any(r.get("result") == PASS and r.get("fingerprint") == fp and after_cut(r)
                     for r in hist):
                # passed on this fingerprint, but only on a build other than
                # its pins as built
                reason = "not-as-built"
            else:
                reason = "domain-changed" if any(r.get("result") == PASS for r in hist) else "never-passed"
        if not satisfied and last_r is not None:
            status = {PASS: "stale", FAIL: "fail", ERROR: "error", ABORTED: "aborted"}.get(last_r.get("result"), "none")
        if running == t.id:
            status = "running"
        if satisfied and not clean(origin):
            unclean.append(t.id)
        out_tests[t.id] = {
            "fingerprint": fp,
            "status": status,
            "satisfied": satisfied,
            "required": not satisfied,
            "reason": reason,
            "last": _summary(last_r),
            "origin": _summary(origin),
        }

    # requires: a prerequisite counts when it is satisfied (campaign or
    # inherited); the always-required core is the freshness mechanism.
    for t in tests:
        missing = [r for r in t.requires if not out_tests.get(r, {}).get("satisfied")]
        out_tests[t.id]["missing_requires"] = missing
        out_tests[t.id]["requires_met"] = not missing

    n_sat = sum(1 for v in out_tests.values() if v["satisfied"])
    n_inh = sum(1 for v in out_tests.values() if v["status"] == "inherited")
    counts = {"total": len(tests), "satisfied": n_sat, "inherited": n_inh,
              "required": len(tests) - n_sat}
    authorized = bool(campaign) and n_sat == len(tests) and len(tests) > 0 and not unclean
    return {
        "campaign": campaign,
        "last_campaign": last,
        "closed_by": closed_by,
        "invalidate": invalidate_shown,
        # the last invalidate-all and its place in the log, for good: the
        # artifact carries it so the gate can judge every result by it
        "epoch": ({"seq": cut, "ts": invalidate.get("ts"), "reason": invalidate.get("reason")}
                  if invalidate else None),
        "authorized": authorized,
        # tests satisfied only by a PASS recorded on a build other than its
        # pins as built: they count on the page, never for a release
        "unclean": unclean,
        # what the open campaign's machine ran when it opened (provenance.py)
        "verdict": campaign.get("provenance") if campaign else None,
        "counts": counts,
        "tests": out_tests,
    }
