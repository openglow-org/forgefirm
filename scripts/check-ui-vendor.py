#!/usr/bin/env python3
# Copyright 2026 514 LLC d/b/a OpenGlow
# Written by Scott Wiederhold
# https://community.openglow.org
# SPDX-License-Identifier:    MIT
"""The files forgetest's page shares with forgectrl's panel, byte for byte.

theme.css (the OpenGlow theme on Bootstrap) and the vendored Bootstrap
files live in both repos so each page is self-contained; they are meant
to be identical, and this is the check. forgetest's copies are compared
against forgectrl's at the revision the recipe pins (fetched from GitHub
by default) or in a local checkout (--forgectrl PATH). Exit status 1 on
any difference or missing file.

    python3 scripts/check-ui-vendor.py
    python3 scripts/check-ui-vendor.py --forgectrl ../forgectrl
"""
import argparse
import hashlib
import os
import re
import sys
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.normpath(os.path.join(HERE, ".."))
SHARED = ("theme.css", "vendor/bootstrap.min.css", "vendor/bootstrap.bundle.min.js",
          "vendor/LICENSE")
FORGETEST_UI = os.path.join(REPO, "forgetest", "forgetest", "ui")
PIN = os.path.join(REPO, "meta-forgefirm", "recipes-forgefirm", "forgectrl", "forgectrl-pin.inc")
RAW = "https://raw.githubusercontent.com/openglow-org/forgectrl/%s/src/ui/%s"


def pinned_rev():
    with open(PIN, encoding="utf-8") as f:
        m = re.search(r'^SRCREV\s*=\s*"([0-9a-f]{7,40})"', f.read(), re.M)
    if not m:
        sys.exit("check-ui-vendor: no SRCREV in %s" % PIN)
    return m.group(1)


def sha(data):
    return hashlib.sha256(data).hexdigest()


def theirs(name, local, rev):
    if local:
        path = os.path.join(local, "src", "ui", *name.split("/"))
        with open(path, "rb") as f:
            return f.read(), path
    url = RAW % (rev, name)
    with urllib.request.urlopen(url, timeout=30) as r:
        return r.read(), url


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--forgectrl", help="a local forgectrl checkout instead of the pinned revision")
    args = ap.parse_args()
    rev = None if args.forgectrl else pinned_rev()
    print("forgectrl: %s" % (args.forgectrl or ("pinned %s" % rev)))
    bad = 0
    for name in SHARED:
        ours_path = os.path.join(FORGETEST_UI, *name.split("/"))
        try:
            with open(ours_path, "rb") as f:
                ours = f.read()
        except OSError as e:
            print("  MISSING forgetest copy of %s (%s)" % (name, e))
            bad += 1
            continue
        try:
            other, where = theirs(name, args.forgectrl, rev)
        except Exception as e:  # noqa: BLE001
            print("  MISSING forgectrl copy of %s (%s)" % (name, e))
            bad += 1
            continue
        if sha(ours) == sha(other):
            print("  ok       %s (%d bytes, %s)" % (name, len(ours), sha(ours)[:12]))
        else:
            print("  DIFFERS  %s: forgetest %s, forgectrl %s (%s)"
                  % (name, sha(ours)[:12], sha(other)[:12], where))
            bad += 1
    if bad:
        print("check-ui-vendor: %d shared file(s) out of step; copy from forgectrl/src/ui/ "
              "(or push forgectrl and bump its pin)" % bad)
        return 1
    print("check-ui-vendor: the shared UI files are identical")
    return 0


if __name__ == "__main__":
    sys.exit(main())
