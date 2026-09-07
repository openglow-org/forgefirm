"""Campaign rules: which results apply, what is required, and whether a
release is authorized.

A campaign is bound to one image (manifest content hash) and one catalog
(catalog hash). It is open from its record until a FAIL/ERROR result in
it, an invalidate-all, an explicit reset, or a different image or
catalog. Rules per test T with domain fingerprint F(T):

  satisfied by campaign  a PASS in the open campaign with fingerprint F(T)
  satisfied by inheritance  (never for the always-required core) the newest
                         PASS anywhere in the history with fingerprint F(T)
                         and newer than the last invalidate-all
  required               otherwise (reason: always / never-passed /
                         domain-changed)

  authorized  <=>  a campaign is open and every catalog test is satisfied

Pure functions over the record list; nothing here touches hardware or
the filesystem, which keeps the rules unit-testable and lets the release
gate reason with the same code.
"""

PASS, FAIL, ERROR, ABORTED = "PASS", "FAIL", "ERROR", "ABORTED"
CLOSING = (FAIL, ERROR)


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
    results = [r for r in records if r.get("t") == "result"]
    campaign, last, closed_by, invalidate = open_campaign(records, manifest.content_sha, catalog_hash)
    epoch = invalidate.get("ts") if invalidate else None
    open_id = campaign.get("id") if campaign else None
    # The invalidate record keeps its epoch for good: nothing older than it
    # inherits. The page and the artifact report it only while it still asks
    # for something, that is until a campaign has started after it.
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
        if same is not None:
            status, satisfied, origin, reason = "pass", True, same, "campaign"
        elif t.always:
            reason = "always"
        else:
            # The newest record on the current fingerprint decides: a PASS
            # is inherited, a FAIL or ERROR after it blocks the inheritance
            # (the test has to be run again), an ABORTED run says nothing.
            inh = None
            blocked = None
            for r in reversed(hist):
                if r.get("fingerprint") != fp:
                    continue
                if epoch and (r.get("ts") or "") <= epoch:
                    continue
                res = r.get("result")
                if res == PASS:
                    inh = r
                    break
                if res in (FAIL, ERROR):
                    blocked = r
                    break
            if inh is not None:
                status, satisfied, origin, reason = "inherited", True, inh, "inherited"
            elif blocked is not None and any(r.get("result") == PASS and r.get("fingerprint") == fp
                                             and (r.get("ts") or "") < (blocked.get("ts") or "")
                                             for r in hist):
                reason = "failed-since"
            else:
                reason = "domain-changed" if any(r.get("result") == PASS for r in hist) else "never-passed"
        if not satisfied and last_r is not None:
            status = {PASS: "stale", FAIL: "fail", ERROR: "error", ABORTED: "aborted"}.get(last_r.get("result"), "none")
        if running == t.id:
            status = "running"
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
    authorized = bool(campaign) and n_sat == len(tests) and len(tests) > 0
    return {
        "campaign": campaign,
        "last_campaign": last,
        "closed_by": closed_by,
        "invalidate": invalidate_shown,
        "authorized": authorized,
        "counts": counts,
        "tests": out_tests,
    }
