# Copyright 2026 514 LLC d/b/a OpenGlow
# Written by Scott Wiederhold
# https://community.openglow.org
# SPDX-License-Identifier:    MIT

"""Coverage lint: every source path in the manifest must be selected by
some test's coverage globs, except the allowlisted non-behavioral paths,
and every coverage glob must select at least one path of its component.

Why a hard rule: under the domain model an uncovered file is worse than an
untested one - a change there leaves every inherited PASS valid when it
should have invalidated them. The lint is the floor (the file is
fingerprinted by at least one test); whether that test exercises the
change stays with the change author. An entry that selects nothing is
the same defect from the other side: the test's fingerprint ignores the
file it named.

  python3 -m forgetest.coverage [--manifest PATH] [--enforce] [--json]

Exit 0 when nothing is uncovered (or when only reporting), 1 under
--enforce with uncovered paths, 2 on a usage or load error.
"""
import argparse
import json
import sys

from . import catalog as _catalog
from . import manifest as _manifest

# Non-behavioral paths need no acceptance coverage, and no fingerprint
# carries them: the one list, in the manifest module.
ALLOW = _manifest.NON_BEHAVIORAL


def run(manifest, tests, allow=ALLOW):
    return _manifest.coverage_report(manifest, tests, allow)


def empty(manifest, tests):
    return _manifest.empty_covers(manifest, tests)


def main(argv=None):
    ap = argparse.ArgumentParser(description="forgetest coverage lint")
    ap.add_argument("--manifest", default=None, help="manifest JSON (default: the running image's)")
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
    if args.json:
        print(json.dumps({"uncovered": report, "total": total, "tests": len(tests),
                          "empty": [list(e) for e in hollow]}, indent=1, sort_keys=True))
    else:
        for comp in sorted(report):
            print("%s: %d uncovered path(s)" % (comp, len(report[comp])))
            for p in report[comp]:
                print("  %s" % p)
        for tid, comp, pat in hollow:
            print("%s: covers (%s, %s) selects nothing" % (tid, comp, pat))
        print("coverage: %d uncovered path(s) across %d component(s), %d empty entr%s, %d tests"
              % (total, len(report), len(hollow), "y" if len(hollow) == 1 else "ies", len(tests)))
    if args.enforce and (total or hollow):
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
