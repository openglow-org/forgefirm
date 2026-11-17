#!/usr/bin/env python3
# Copyright 2026 514 LLC d/b/a OpenGlow
# Written by Scott Wiederhold
# https://community.openglow.org
# SPDX-License-Identifier:    MIT
#
# Release host-test gate: does a host test that passed answer for every path
# of THIS release build that no catalog test covers?
#
#   host-test-gate.py <release-manifest.json> [--rev REV] [--sources DIR]
#
# The release manifest is /etc/forgefirm-manifest.json read out of the
# release rootfs release.sh just built. Two checks, and the gate refuses on
# any problem in either:
#
#   lint  The coverage lint (forgetest/forgetest/coverage.py) on the release
#         manifest itself, not on the tree's: every source path of the build
#         is selected by a catalog test's covers or by a HOST_TESTED row, no
#         covers entry and no row selects nothing, and every row names a host
#         test and a CI workflow that runs it. The workflows are read at the
#         commits the release pins, from the pinned sources this script
#         fetches into --sources (a temporary directory by default).
#   ci    For every CI workflow a HOST_TESTED row names, the newest run of
#         that workflow for the commit the release pins (the component's
#         srcrev in the manifest; forgefirm's REV for a forgefirm workflow)
#         concluded success, as GitHub reports it (gh api). So does
#         forgefirm's forgetest-ci at REV. GitHub runs a workflow for a pushed
#         commit only, so a pin on a commit that was never a branch's head has
#         no run and is refused; the latest attempt of a run that was re-run
#         is the one counted.
#
# Every check fails closed: a source that cannot be fetched, a workflow that
# cannot be read, a query gh cannot answer, or a run that has not finished is
# a refusal. Exit 0 when both checks pass, 1 otherwise, 2 on a usage or load
# error.
import argparse
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(HERE)
sys.path.insert(0, os.path.join(REPO, "forgetest"))

from forgetest import catalog as _catalog  # noqa: E402
from forgetest import coverage as _coverage  # noqa: E402
from forgetest import manifest as _manifest  # noqa: E402

FORGEFIRM = "openglow-org/forgefirm"
# forgefirm's own workflow that runs the lint on the tree and the unit tests
# of the acceptance tool and the release scripts.
FORGEFIRM_CI = "forgefirm:.github/workflows/forgetest-ci.yml"
_GITHUB = re.compile(r"^(?:git|gitsm|https?|ssh)://(?:[^@/]+@)?github\.com/([^/]+)/([^/]+?)(?:\.git)?/?$")


class Refused(Exception):
    """A check that cannot pass, or cannot be made."""


def github_repo(url):
    """"owner/repo" of a GitHub source URL."""
    m = _GITHUB.match(url or "")
    if not m:
        raise Refused("%r is not a GitHub repository" % url)
    return "%s/%s" % (m.group(1), m.group(2))


def fetch_sources(manifest, repos, cache):
    """Each named component's pinned commit, fetched into the bare repository
    the lint reads it from (manifest.read_source: the cache directory named
    by the sha1 of the manifest's own source URL)."""
    for name in sorted(repos):
        comp = manifest.components.get(name) or {}
        url, rev = comp.get("source"), comp.get("srcrev")
        if not (url and rev):
            raise Refused("%s: the manifest names no source and revision to read its workflows from" % name)
        repo = os.path.join(cache, hashlib.sha1(url.encode()).hexdigest()[:16])
        os.makedirs(repo, exist_ok=True)
        steps = [["git", "init", "-q", "--bare"],
                 ["git", "fetch", "-q", "--depth", "1", "https://github.com/%s.git" % github_repo(url), rev]]
        for cmd in steps:
            p = subprocess.run(cmd, cwd=repo, capture_output=True, timeout=900)
            if p.returncode:
                raise Refused("%s: %s failed: %s" % (name, " ".join(cmd[:3]),
                                                     p.stderr.decode("utf-8", "replace").strip()))


def lint_problems(manifest, tests, rows, sources):
    """The coverage lint on the manifest, as forgetest.coverage --enforce
    judges it: uncovered paths, empty covers entries, row problems."""
    out = []
    for comp, paths in sorted(_manifest.coverage_report(manifest, tests, _coverage.ALLOW, host=rows).items()):
        for p in paths:
            out.append("%s: %s is covered by no catalog test and no host test" % (comp, p))
    for tid, comp, pat in _manifest.empty_covers(manifest, tests):
        out.append("%s: covers (%s, %s) selects nothing" % (tid, comp, pat))
    for comp, pat, ref, why in _manifest.host_row_problems(manifest, host=rows, sources=sources):
        out.append("host-tested (%s, %s) by %s: %s" % (comp, pat, ref, why))
    return out


def ci_targets(manifest, rows, rev):
    """{(owner/repo, workflow path): commit} for every workflow the rows
    name, at the commit the release pins, plus forgefirm's forgetest-ci."""
    out = {}
    for ci in sorted({r[3] for r in rows} | {FORGEFIRM_CI}):
        name, _sep, path = ci.partition(":")
        if name == "forgefirm":
            repo, sha = FORGEFIRM, rev
        else:
            comp = manifest.components.get(name)
            if not comp:
                raise Refused("%s: the release manifest has no component %s" % (ci, name))
            repo, sha = github_repo(comp.get("source")), comp.get("srcrev")
        if not re.match(r"^[0-9a-f]{40}$", sha or ""):
            raise Refused("%s: %r is not a full commit id" % (ci, sha))
        out[(repo, path)] = sha
    return out


def newest_run(gh, repo, path, sha):
    """The newest run of a workflow for a commit, as gh api reports it, or
    None when it has none."""
    url = "repos/%s/actions/workflows/%s/runs?head_sha=%s&per_page=100" % (repo, os.path.basename(path), sha)
    try:
        p = subprocess.run([gh, "api", url], capture_output=True, timeout=120)
    except (OSError, subprocess.TimeoutExpired) as e:
        raise Refused("gh api %s: %s" % (url, e))
    if p.returncode:
        raise Refused("gh api %s: %s" % (url, p.stderr.decode("utf-8", "replace").strip()))
    try:
        runs = json.loads(p.stdout.decode("utf-8"))["workflow_runs"]
        runs = [r for r in runs if r.get("head_sha") == sha]
        return max(runs, key=lambda r: int(r["run_number"])) if runs else None
    except (ValueError, KeyError, TypeError) as e:
        raise Refused("gh api %s: an answer that is not a list of runs: %s" % (url, e))


def ci_problems(manifest, rows, rev, gh="gh"):
    """Each workflow of ci_targets whose newest run for its commit did not
    conclude success."""
    out = []
    for (repo, path), sha in sorted(ci_targets(manifest, rows, rev).items()):
        try:
            run = newest_run(gh, repo, path, sha)
        except Refused as e:
            out.append(str(e))
            continue
        where = "%s %s at %s" % (repo, os.path.basename(path), sha[:12])
        if run is None:
            out.append("%s: no run (GitHub runs a workflow for a pushed head only)" % where)
        elif run.get("status") != "completed" or run.get("conclusion") != "success":
            out.append("%s: run %s is %s, %s" % (where, run.get("run_number"), run.get("status"),
                                                  run.get("conclusion")))
        else:
            print("PASS     ci        %-56s run %s" % (where, run.get("run_number")))
    return out


def main(argv=None):
    ap = argparse.ArgumentParser(description="ForgeFIRM release host-test gate")
    ap.add_argument("release_manifest")
    ap.add_argument("--rev", default="HEAD", help="the forgefirm commit of the release (default HEAD)")
    ap.add_argument("--sources", default=None,
                    help="where to fetch the pinned sources (default: a temporary directory)")
    args = ap.parse_args(argv)
    try:
        manifest = _manifest.Manifest.load(args.release_manifest)
    except (OSError, ValueError) as e:
        print("host-test-gate: cannot load %s: %s" % (args.release_manifest, e), file=sys.stderr)
        return 2
    p = subprocess.run(["git", "-C", REPO, "rev-parse", "--verify", args.rev + "^{commit}"],
                       capture_output=True, text=True)
    if p.returncode:
        print("host-test-gate: forgefirm has no commit %s" % args.rev, file=sys.stderr)
        return 2
    rev = p.stdout.strip()
    rows = list(_manifest.HOST_TESTED)
    tests = _catalog.all_tests(_catalog.load_suite())
    sources = args.sources or tempfile.mkdtemp(prefix="host-test-gate.")
    problems = []
    try:
        try:
            fetch_sources(manifest, {r[3].partition(":")[0] for r in rows} - {"forgefirm"}, sources)
            problems += lint_problems(manifest, tests, rows, sources)
        except Refused as e:
            problems.append(str(e))
        try:
            problems += ci_problems(manifest, rows, rev)
        except Refused as e:
            problems.append(str(e))
    finally:
        if not args.sources:
            shutil.rmtree(sources, ignore_errors=True)
    for why in problems:
        print("REFUSED  %s" % why)
    print("host-test gate: %s" % ("every path has a test, and each host test passed at its pin"
                                  if not problems else "REFUSED"))
    return 0 if not problems else 1


if __name__ == "__main__":
    sys.exit(main())
