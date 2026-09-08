#!/usr/bin/env python3
# Copyright 2020-2026 514 LLC d/b/a OpenGlow
# Written by Scott Wiederhold
# https://community.openglow.org
# SPDX-License-Identifier:    MIT
#
# Release acceptance gate: does the committed acceptance artifact authorize
# THIS release build?
#
#   acceptance-gate.py <acceptance.json> <release-manifest.json> [--machine glowforge]
#
# The artifact is what forgetest exported on the bench (releases/v<version>/
# acceptance.json); the release manifest is /etc/forgefirm-manifest.json read
# out of the release rootfs release.sh just built. For every catalog test the
# gate recomputes the domain fingerprint from the release manifest with the
# catalog in this source tree - the same code the bench ran - and requires the
# recorded PASS to match. Inherited results must not be core tests and must
# be newer than the last invalidate-all. Exit 0 = authorized, 1 = refused,
# 2 = usage/load error. release.sh dies on anything but 0.
import argparse
import json
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(os.path.dirname(HERE), "forgetest"))

from forgetest import artifact, catalog, manifest  # noqa: E402


def main(argv=None):
    ap = argparse.ArgumentParser(description="ForgeFIRM release acceptance gate")
    ap.add_argument("artifact")
    ap.add_argument("release_manifest")
    ap.add_argument("--machine", default="glowforge")
    ap.add_argument("--quiet", action="store_true")
    args = ap.parse_args(argv)
    try:
        with open(args.artifact, "r", encoding="utf-8") as f:
            art = json.load(f)
        rel = manifest.Manifest.load(args.release_manifest)
    except (OSError, ValueError) as e:
        print("acceptance-gate: cannot load inputs: %s" % e, file=sys.stderr)
        return 2
    registry = catalog.load_suite()
    tests = catalog.all_tests(registry)
    ok, rows, problems = artifact.verify(art, rel, tests, catalog.catalog_hash(registry),
                                         expect_machine=args.machine)
    if not args.quiet:
        print("acceptance artifact: image %s, campaign %s, exported %s, authorized=%s"
              % (art.get("image", {}).get("version"), (art.get("campaign") or {}).get("id"),
                 art.get("exported_at"), art.get("authorized")))
        print("release manifest:    %s identity %s" % (rel.version, rel.identity_sha()[:16]))
        if art.get("identity_sha") == rel.identity_sha():
            print("identity: the release build's inputs are identical to the bench image's")
        else:
            print("identity: the release build differs from the bench image (per-test check decides)")
        for r in rows:
            flag = "ok  " if r["ok"] else "FAIL"
            print("  %s %-34s %s%s%s" % (flag, r["id"], "core " if r["always"] else "",
                                          "inherited " if r["inherited"] else "",
                                          ("; ".join(r["why"]) if r["why"] else r.get("ts", ""))))
        for p in problems:
            print("PROBLEM: %s" % p)
        print("acceptance gate: %s" % ("AUTHORIZED" if ok else "REFUSED"))
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
