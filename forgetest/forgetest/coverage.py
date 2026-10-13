# Copyright 2026 514 LLC d/b/a OpenGlow
# Written by Scott Wiederhold
# https://community.openglow.org
# SPDX-License-Identifier:    MIT

"""Coverage lint: every source path in the manifest must be selected by
some test's coverage globs or by a host-tested row (manifest.HOST_TESTED),
except the allowlisted non-behavioral paths; every coverage glob and every
host-tested glob must select at least one path of its component; and every
host-tested row must name a host test that exists and a CI workflow that
runs it.

Why a hard rule: under the domain model an uncovered file is worse than an
untested one - a change there leaves every inherited PASS valid when it
should have invalidated them. The lint is the floor (the file is
fingerprinted by at least one test, or a named host test that CI runs
answers for it); whether that test exercises the change stays with the
change author. An entry that selects nothing is the same defect from the
other side: the test's fingerprint ignores the file it named.

Whether a workflow runs its host test is read from the workflow's text at
the pinned revision, in the sources scripts/manifest-from-tree.py fetched
(--sources, default the checkout's .manifest-cache). Without them (on an
image) that part is not checked, and the report says so.

  python3 -m forgetest.coverage [--manifest PATH] [--sources DIR] [--enforce] [--json]

Exit 0 when nothing is uncovered (or when only reporting), 1 under
--enforce with uncovered paths, 2 on a usage or load error.
"""
import argparse
import json
import os
import sys

from . import catalog as _catalog
from . import manifest as _manifest

# Non-behavioral paths need no acceptance coverage, and no fingerprint
# carries them: the one list, in the manifest module.
ALLOW = _manifest.NON_BEHAVIORAL


def run(manifest, tests, allow=ALLOW):
    return _manifest.coverage_report(manifest, tests, allow, host=_manifest.HOST_TESTED)


def empty(manifest, tests):
    return _manifest.empty_covers(manifest, tests)


def host_problems(manifest, sources=_manifest.SOURCES):
    return _manifest.host_row_problems(manifest, host=_manifest.HOST_TESTED, sources=sources)


def main(argv=None):
    ap = argparse.ArgumentParser(description="forgetest coverage lint")
    ap.add_argument("--manifest", default=None, help="manifest JSON (default: the running image's)")
    ap.add_argument("--sources", default=_manifest.SOURCES,
                    help="the pinned sources manifest-from-tree.py fetched (default: the checkout's .manifest-cache)")
    ap.add_argument("--enforce", action="store_true", help="exit 1 when any path is uncovered")
    ap.add_argument("--json", action="store_true", help="machine-readable report")
    args = ap.parse_args(argv)
    try:
        manifest = _manifest.Manifest.load(args.manifest)
    except (OSError, ValueError) as e:
        print("cannot load manifest: %s" % e, file=sys.stderr)
        return 2
    registry = _catalog.load_suite()
    tests = _catalog.all_tests(registry)
    report = run(manifest, tests)
    total = sum(len(v) for v in report.values())
    hollow = empty(manifest, tests)
    sources = args.sources if args.sources and os.path.isdir(args.sources) else None
    rows = host_problems(manifest, sources)
    if args.json:
        print(json.dumps({"uncovered": report, "total": total, "tests": len(tests),
                          "empty": [list(e) for e in hollow],
                          "host_tested": [list(r) for r in rows],
                          "workflows_read": sources is not None and not any("cannot be read" in r[3] for r in rows)},
                         indent=1, sort_keys=True))
    else:
        if _manifest.HOST_TESTED and sources is None:
            print("host-tested: no sources, so whether a workflow runs each host test is not checked")
        for comp in sorted(report):
            print("%s: %d uncovered path(s)" % (comp, len(report[comp])))
            for p in report[comp]:
                print("  %s" % p)
        for tid, comp, pat in hollow:
            print("%s: covers (%s, %s) selects nothing" % (tid, comp, pat))
        for comp, pat, ref, why in rows:
            print("host-tested (%s, %s) by %s: %s" % (comp, pat, ref, why))
        print("coverage: %d uncovered path(s) across %d component(s), %d empty entr%s, "
              "%d host-tested row problem(s), %d tests"
              % (total, len(report), len(hollow), "y" if len(hollow) == 1 else "ies", len(rows),
                 len(tests)))
    if args.enforce and (total or hollow or rows):
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
