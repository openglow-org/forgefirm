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
                         bench fixture raised, nor one before the test
                         ran), recorded with "needs_disposition" under F(T)
                         blocks F(T) until the operator records a
                         disposition for T and F(T) after it, with a reason
                         (reason: disposition). A rerun that passes does not
                         clear it, and neither does an invalidate-all. A
                         "leftover" record blocks F(T) the same way: one on
                         the laser's safety state (the safety rule,
                         runner._safety_found: found in T's own pass after
                         its run, or before a later run with T the newest
                         run of the boot to finish), or another a pre pass
                         found after T passed (runner._charge_previous). A
                         safety leftover costs what a FAIL does: after the
                         disposition the test needs a PASS newer than the
                         leftover. A run whose
                         start is in the log with no result after it is
                         such an ERROR, and a safety record a result
                         carries holds as a leftover record (as_judged,
                         applied by the runner before these rules).
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
# A leftover record as the artifact lists it among a test's other results.
LEFTOVER = "LEFTOVER"
# The leftovers on the laser's safety state (baseline.py judges them): the
# latch as the kernel and forgectrl read it, the FIRE and LASER_ON readbacks,
# and an armed window the cooling engine still shows past its tick (a session
# waiting for its press, which the client unlocks the latch for). A leftover
# record that names one is a safety leftover, whatever its own flag says
# (artifact.verify).
SAFETY_ITEMS = ("laser_locked", "laser_latch", "cnc/laser_enable", "cnc/laser_on", "cool.armed")


def positions(records):
    """{id(record): its place in the log}: the "seq" the log gave it, or
    its index in the list (from 1) for a record that carries none."""
    out = {}
    for i, r in enumerate(records):
        s = r.get("seq")
        out[id(r)] = s if isinstance(s, int) and not isinstance(s, bool) else i + 1
    return out


def lost_runs(records, running=None):
    """The test runs the log saw start and never saw end: each "start"
    record of a test (runner.py appends one before a run's first pass) that
    no "result" names as its start, other than the run in progress
    (`running`: its start's place). A bench tool's start names no test."""
    pos = positions(records)
    ended = set(r.get("start") for r in records if r.get("t") == "result")
    return [r for r in records if r.get("t") == "start" and r.get("test")
            and pos[id(r)] not in ended and pos[id(r)] != running]


def with_lost_runs(records, running=None):
    """The log with each lost run (lost_runs) in the place it started, as
    the ERROR it is: it closes its campaign and holds its test's fingerprint
    until a disposition, as a result the log could not take does."""
    pos = positions(records)
    lost = set(id(r) for r in lost_runs(records, running))
    out = []
    for r in records:
        if id(r) in lost:
            r = {"t": "result", "ts": r.get("ts"), "seq": pos[id(r)], "boot": r.get("boot"),
                 "campaign": r.get("campaign"), "test": r.get("test"), "fingerprint": r.get("fingerprint"),
                 "start": pos[id(r)], "result": ERROR, "needs_disposition": True, "not_recorded": True,
                 "lost": True, "message": "the run started and left no result in the log"}
        out.append(r)
    return out


def with_carried_charges(records):
    """The log with each safety record a run's result carries ("charges":
    the records the log refused when the pass made them, runner.py) placed
    beside that result, where it holds as the record would have."""
    pos = positions(records)
    out = []
    for r in records:
        out.append(r)
        if r.get("t") == "result" and isinstance(r.get("charges"), list):
            out.extend(dict(c, seq=pos[id(r)], carried=True) for c in r["charges"] if isinstance(c, dict))
    return out


def as_judged(records, running=None):
    """The log as the campaign rules read it: the lost runs (with_lost_runs)
    and the carried charges (with_carried_charges) in their places."""
    return with_carried_charges(with_lost_runs(records, running))


def dispositions(records, pos):
    """{(test, fingerprint): place of the newest disposition record}."""
    out = {}
    for r in records:
        if r.get("t") == "disposition":
            k = (r.get("test"), r.get("fingerprint"))
            out[k] = max(out.get(k, 0), pos[id(r)])
    return out


def blocks(rec, pos, disposed):
    """Whether a record blocks its test's fingerprint: a FAIL or an ERROR
    that asks for a disposition, or a leftover record, that no disposition
    after it answers. An invalidate-all answers nothing: its reason is the
    bench's, not the FAIL's, so a FAIL holds its fingerprint whatever
    invalidate-alls follow it (the fingerprint already limits it to
    unchanged code). A disposition at the record's own place answers it no
    more than one before it does, as the exporter and the gate read it."""
    holds = ((rec.get("t") == "result" and rec.get("result") in CLOSING) or rec.get("t") == "leftover")
    return (holds and rec.get("needs_disposition") is True
            and disposed.get((rec.get("test"), rec.get("fingerprint")), 0) <= pos[id(rec)])


def _summary(rec):
    if rec is None:
        return None
    keys = ("ts", "result", "campaign", "image", "fingerprint", "duration_s", "message", "lost")
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
    leftovers = {}
    for r in records:
        if r.get("t") == "leftover":
            leftovers.setdefault(r.get("test"), []).append(r)

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
        charged = [r for r in leftovers.get(t.id, []) if r.get("fingerprint") == fp]
        # A leftover on the laser's safety state costs what a FAIL does: the
        # test needs a PASS newer than it, whatever answers it.
        unsafe = [r for r in charged if r.get("safety") is True]
        unsafe_at = max((pos[id(r)] for r in unsafe), default=0)
        if open_id:
            for r in reversed(hist):
                if (r.get("campaign") == open_id and r.get("result") == PASS and r.get("fingerprint") == fp
                        and pos[id(r)] > unsafe_at):
                    same = r
                    break
        if any(blocks(r, pos, disposed) for r in hist + charged if r.get("fingerprint") == fp):
            # A FAIL or ERROR on this fingerprint, or a leftover charged to
            # it, that nobody has answered: a rerun that passes does not
            # clear it, a disposition does.
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
            for r in sorted(hist + unsafe, key=lambda x: pos[id(x)], reverse=True):
                if r.get("fingerprint") != fp:
                    continue
                if not after_cut(r):
                    continue
                res = r.get("result")
                if res == PASS and clean(r):
                    inh = r
                    break
                if res in (FAIL, ERROR) or r.get("t") == "leftover":
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
            # the newest leftover charged to this fingerprint, for the row:
            # what a disposition would answer
            "leftover": ({"ts": charged[-1].get("ts"), "message": charged[-1].get("message"),
                          "safety": charged[-1].get("safety") is True,
                          "answered": not blocks(charged[-1], pos, disposed)} if charged else None),
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
