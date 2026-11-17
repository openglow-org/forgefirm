#!/usr/bin/env python3
# Copyright 2026 514 LLC d/b/a OpenGlow
# Written by Scott Wiederhold
# https://community.openglow.org
# SPDX-License-Identifier:    MIT
#
# Check that a release builds only from its release branches.
#
#   release-branch-check.py [--rev REV] [--check NAME]... [--github URL]
#
# Every commit that the release image builds from must be on its
# repository's release branch on GitHub. The checks, each selectable with
# --check (all of them by default):
#
#   pins       each component recipe's SRCREV on the component's release
#              branch: the four meta-forgefirm pins as forgefirm holds them at
#              REV, and the three meta-openglow pins as meta-openglow holds
#              them at the commit the kas lock names; and the grblHAL-core
#              commit that the pinned grblHAL-glowforge commit's gitlink
#              (src/grbl) names, on the fork's forgefirm branch
#   lock       the meta-openglow commit that the kas lock names, on scarthgap
#   forgefirm  REV itself, on forgefirm's master
#   names      each component recipe's SRC_URI (the four in forgefirm at REV,
#              the three in meta-openglow at the lock's commit) names its
#              repository and its release branch, with no nobranch; the kas
#              meta-openglow entry names the GitHub repository and scarthgap;
#              and neither the configuration nor the lock overrides that entry
#              with a branch of its own (the configuration may override it in
#              nothing), which kas would apply over the entry's. So every
#              later fetch checks its pin against that branch too
#
# The branches come from GitHub, fetched into a temporary directory, not
# from a local checkout, so a stale or unpushed local branch cannot pass.
# Every check fails closed: a commit that cannot be read, a branch that
# cannot be fetched, or a file that does not parse is a refusal. Exit 0 when
# every check passes, 1 otherwise.
import argparse
import os
import re
import shutil
import subprocess
import sys
import tempfile

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
GITHUB = "https://github.com/openglow-org/"
# (component, layer, recipe holding SRC_URI, pin file, repository, release branch)
COMPONENTS = [
    ("forgectrl", "forgefirm", "meta-forgefirm/recipes-forgefirm/forgectrl/forgectrl.bb",
     "meta-forgefirm/recipes-forgefirm/forgectrl/forgectrl-pin.inc", "forgectrl", "main"),
    ("forgeext", "forgefirm", "meta-forgefirm/recipes-forgefirm/forgeext/forgeext.bb",
     "meta-forgefirm/recipes-forgefirm/forgeext/forgeext-pin.inc", "forgeext", "main"),
    ("grblhal-glowforge", "forgefirm", "meta-forgefirm/recipes-forgefirm/grblhal-glowforge/grblhal-glowforge.bb",
     "meta-forgefirm/recipes-forgefirm/grblhal-glowforge/grblhal-glowforge-pin.inc", "grblHAL-glowforge", "main"),
    ("forgefirm-app", "forgefirm", "meta-forgefirm/recipes-forgefirm/forgefirm-app/forgefirm-app.inc",
     "meta-forgefirm/recipes-forgefirm/forgefirm-app/forgefirm-app-pin.inc", "python3-gfhardware", "master"),
    ("kernel-module-glowforge", "meta-openglow",
     "meta-glowforge-bsp/recipes-kernel/kernel-modules/kernel-module-glowforge.bb",
     "meta-glowforge-bsp/recipes-kernel/kernel-modules/kernel-module-glowforge-pin.inc",
     "kernel-module-glowforge", "master"),
    ("python3-gfhardware", "meta-openglow", "meta-glowforge-bsp/recipes-devtools/python/python3-gfhardware.bb",
     "meta-glowforge-bsp/recipes-devtools/python/python3-gfhardware-pin.inc", "python3-gfhardware", "master"),
    ("python3-gfutilities", "meta-openglow",
     "meta-openglow-core/recipes-devtools/python/python3-gfutilities_git.bb",
     "meta-openglow-core/recipes-devtools/python/python3-gfutilities-pin.inc", "Glowforge-Utilities", "master"),
]
BSP = ("meta-openglow", "scarthgap")
CORE = ("grblHAL-core", "forgefirm", "src/grbl")
DRIVER = "grblhal-glowforge"
FORGEFIRM = ("forgefirm", "master")
BSP_URL = GITHUB + "meta-openglow.git"
KAS_CONFIG = "kas/forgefirm-glowforge.yml"
KAS_LOCK = "kas/forgefirm-glowforge.lock.yml"
CHECKS = ("pins", "lock", "forgefirm", "names")
SHA = re.compile(r"^[0-9a-f]{40}$")
GIT_TIMEOUT = 900


class Refused(Exception):
    """A check that cannot pass, or cannot be made."""


def git(args, cwd, input=None):
    try:
        p = subprocess.run(["git"] + args, cwd=cwd, input=input, capture_output=True,
                           timeout=GIT_TIMEOUT)
    except (OSError, subprocess.TimeoutExpired) as e:
        raise Refused("git %s: %s" % (" ".join(args[:2]), e))
    return p.returncode, p.stdout.decode("utf-8", "replace"), p.stderr.decode("utf-8", "replace").strip()


class GitHub:
    """One bare repository per GitHub repository, in a temporary directory:
    each release branch fetched with its whole history, and a commit fetched
    by its id only to read files from it."""

    def __init__(self, base, root):
        self.base = base
        self.root = root
        self.repos = {}

    def repo(self, name):
        if name not in self.repos:
            path = os.path.join(self.root, name + ".git")
            rc, _o, err = git(["init", "-q", "--bare", path], self.root)
            if rc:
                raise Refused("cannot make a cache for %s: %s" % (name, err))
            self.repos[name] = path
        return self.repos[name]

    def url(self, name):
        return self.base + name + ".git"

    def branch(self, name, branch):
        """The tip of `branch` as GitHub holds it now, with its history."""
        path = self.repo(name)
        ref = "refs/heads/" + branch
        if not git(["rev-parse", "-q", "--verify", ref], path)[0] == 0:
            rc, _o, err = git(["fetch", "-q", self.url(name), "+%s:%s" % (ref, ref)], path)
            if rc:
                raise Refused("cannot fetch %s %s from %s: %s" % (name, branch, self.url(name), err))
        rc, out, err = git(["rev-parse", "--verify", ref], path)
        if rc:
            raise Refused("%s has no branch %s: %s" % (name, branch, err))
        return out.strip()

    def on_branch(self, name, commit, branch):
        """Whether `commit` is on `branch`: present once the branch's whole
        history is fetched, and an ancestor of its tip. A commit that was
        fetched by its id alone is not on the branch unless the tip reaches
        it."""
        if not SHA.match(commit or ""):
            raise Refused("%s: %r is not a full commit id" % (name, commit))
        tip = self.branch(name, branch)
        path = self.repo(name)
        if git(["cat-file", "-e", commit + "^{commit}"], path)[0] != 0:
            return False
        rc, _o, err = git(["merge-base", "--is-ancestor", commit, tip], path)
        if rc not in (0, 1):
            raise Refused("%s: cannot compare %s with %s: %s" % (name, commit[:12], branch, err))
        return rc == 0

    def ensure(self, name, commit):
        """The repository's cache, holding `commit`: fetched by its id if no
        fetched branch holds it, only to read files from it."""
        if not SHA.match(commit or ""):
            raise Refused("%s: %r is not a full commit id" % (name, commit))
        repo = self.repo(name)
        if git(["cat-file", "-e", commit + "^{commit}"], repo)[0] != 0:
            rc, _o, err = git(["fetch", "-q", self.url(name), commit], repo)
            if rc:
                raise Refused("cannot fetch %s %s: %s" % (name, commit[:12], err))
        return repo

    def show(self, name, commit, path):
        """A file's text at `commit`."""
        rc, out, err = git(["show", "%s:%s" % (commit, path)], self.ensure(name, commit))
        if rc:
            raise Refused("%s %s has no %s: %s" % (name, commit[:12], path, err))
        return out

    def gitlink(self, name, commit, path):
        """The commit that the submodule at `path` names in `commit`'s tree."""
        rc, out, err = git(["ls-tree", commit, path], self.ensure(name, commit))
        parts = out.split()
        if rc or len(parts) < 3 or parts[1] != "commit" or not SHA.match(parts[2]):
            raise Refused("%s %s names no submodule commit at %s: %s" % (name, commit[:12], path, err or out))
        return parts[2]


def forgefirm_file(rev, path):
    rc, out, err = git(["show", "%s:%s" % (rev, path)], REPO)
    if rc:
        raise Refused("forgefirm %s has no %s: %s" % (rev, path, err))
    return out


def pin(text, where):
    m = re.search(r'^SRCREV\s*=\s*"([0-9a-f]{40})"\s*$', text, re.M)
    if not m:
        raise Refused("%s names no full SRCREV" % where)
    return m.group(1)


def src_uri(text, where):
    """The first SRC_URI entry's URL and parameters."""
    m = re.search(r'^SRC_URI\s*=\s*"([^"]+)"', text, re.M)
    if not m:
        raise Refused("%s has no SRC_URI" % where)
    first = m.group(1).split()[0]
    url, _s, rest = first.partition(";")
    params = rest.split(";") if rest else []
    if any("=" not in p for p in params):
        raise Refused("%s: a SRC_URI parameter with no value: %s" % (where, first))
    return url, dict(p.split("=", 1) for p in params)


def yaml_block(text, keys):
    """{key: value} for the scalar keys directly under a nested mapping key
    path of a plain YAML file, read by indentation (the kas files hold no
    flow collections or multi-line scalars), or None when the path is not
    there. Each key of the path is matched at its parent's first child
    indentation only."""
    lines = [ln.rstrip() for ln in text.splitlines()
             if ln.strip() and not ln.lstrip().startswith("#")]

    def children(start, parent):
        """(index, line) of the lines directly under the parent indentation."""
        child = None
        for i in range(start, len(lines)):
            ind = len(lines[i]) - len(lines[i].lstrip())
            if ind <= parent:
                return
            child = ind if child is None else child
            if ind == child:
                yield i, lines[i]

    start, indent = 0, -1
    for key in keys:
        found = next((i for i, ln in children(start, indent) if ln.strip() == key + ":"), None)
        if found is None:
            return None
        start, indent = found + 1, len(lines[found]) - len(lines[found].lstrip())
    out = {}
    for _i, ln in children(start, indent):
        k, sep, v = ln.strip().partition(":")
        if sep:
            out[k.strip()] = v.strip().strip("'\"")
    return out


def lock_commit(rev):
    entry = yaml_block(forgefirm_file(rev, KAS_LOCK), ["overrides", "repos", BSP[0]])
    commit = (entry or {}).get("commit", "")
    if not SHA.match(commit):
        raise Refused("%s names no full commit for %s" % (KAS_LOCK, BSP[0]))
    return commit


def run(args):
    gh = GitHub(args.github, args.cache)
    rc, rev, err = git(["rev-parse", "--verify", args.rev + "^{commit}"], REPO)
    if rc:
        raise Refused("forgefirm has no commit %s: %s" % (args.rev, err))
    rev = rev.strip()
    results = []

    def judge(check, what, ok, detail):
        results.append(ok)
        print("%-8s %-9s %-44s %s" % ("PASS" if ok else "REFUSED", check, what, detail))

    def attempt(check, what, fn):
        try:
            fn()
        except Refused as e:
            judge(check, what, False, str(e))

    def recipe_texts():
        """(component, recipe text, pin text, repository, branch) for the seven."""
        lock = lock_commit(rev)
        for comp, layer, recipe, pin_file, repo, branch in COMPONENTS:
            if layer == "forgefirm":
                yield comp, (lambda r=recipe: forgefirm_file(rev, r)), (lambda p=pin_file: forgefirm_file(rev, p)), repo, branch
            else:
                yield comp, (lambda r=recipe: gh.show(BSP[0], lock, r)), (lambda p=pin_file: gh.show(BSP[0], lock, p)), repo, branch

    if "pins" in args.check:
        def components():
            for comp, _recipe, pin_text, repo, branch in recipe_texts():
                def one(comp=comp, pin_text=pin_text, repo=repo, branch=branch):
                    commit = pin(pin_text(), comp + " pin")
                    judge("pins", "%s %s" % (comp, commit[:12]), gh.on_branch(repo, commit, branch),
                          "on %s %s" % (repo, branch))
                    if comp == DRIVER:
                        link = gh.gitlink(repo, commit, CORE[2])
                        judge("pins", "%s %s" % (CORE[0], link[:12]), gh.on_branch(CORE[0], link, CORE[1]),
                              "the gitlink of %s %s, on %s %s" % (repo, commit[:12], CORE[0], CORE[1]))
                attempt("pins", comp, one)
        attempt("pins", "the kas lock", components)

    if "lock" in args.check:
        def lock():
            commit = lock_commit(rev)
            judge("lock", "%s %s" % (BSP[0], commit[:12]), gh.on_branch(BSP[0], commit, BSP[1]),
                  "on %s %s" % (BSP[0], BSP[1]))
        attempt("lock", BSP[0], lock)

    if "forgefirm" in args.check:
        attempt("forgefirm", "forgefirm %s" % rev[:12],
                lambda: judge("forgefirm", "forgefirm %s" % rev[:12], gh.on_branch(FORGEFIRM[0], rev, FORGEFIRM[1]),
                              "on %s %s" % FORGEFIRM))

    if "names" in args.check:
        def names():
            for comp, recipe_text, _pin, repo, branch in recipe_texts():
                def one(comp=comp, recipe_text=recipe_text, repo=repo, branch=branch):
                    url, params = src_uri(recipe_text(), comp)
                    named = params.get("branch")
                    ok = (url.rsplit("/", 1)[-1] == repo + ".git" and named == branch
                          and params.get("nobranch", "0") == "0")
                    judge("names", comp, ok, "SRC_URI %s, branch=%s (wants %s.git, branch=%s)" % (
                        url, named, repo, branch))
                attempt("names", comp, one)
            config = forgefirm_file(rev, KAS_CONFIG)
            entry = yaml_block(config, ["repos", BSP[0]]) or {}
            judge("names", "kas %s" % BSP[0], entry.get("branch") == BSP[1],
                  "branch: %s (wants %s)" % (entry.get("branch"), BSP[1]))
            # kas runs no git on an entry with no url (a local checkout), so
            # the lock's commit would never be checked out.
            judge("names", "kas url %s" % BSP[0], entry.get("url") == BSP_URL,
                  "url: %s (wants %s)" % (entry.get("url"), BSP_URL))
            # The configuration's own overrides apply over the lock's.
            own = yaml_block(config, ["overrides", "repos", BSP[0]])
            judge("names", "kas overrides %s" % BSP[0], own is None,
                  "the configuration overrides it: %s" % own if own is not None
                  else "the configuration sets no override of its own")
            # kas applies a branch under the lock's override over the entry's
            # (a null one drops the branch check), so the lock may set none.
            override = yaml_block(forgefirm_file(rev, KAS_LOCK), ["overrides", "repos", BSP[0]]) or {}
            judge("names", "kas lock %s" % BSP[0], "branch" not in override,
                  "the lock sets %s" % ("branch: %s" % override["branch"] if "branch" in override
                                        else "no branch of its own"))
        attempt("names", "the recipes", names)

    if not results:
        raise Refused("no check ran")
    return all(results)


def main(argv=None):
    ap = argparse.ArgumentParser(description="Check that a release builds only from its release branches.")
    ap.add_argument("--rev", default="HEAD", help="the forgefirm commit to check (default HEAD)")
    ap.add_argument("--check", action="append", choices=CHECKS,
                    help="a check to run; repeatable (default: all of them)")
    ap.add_argument("--github", default=GITHUB, help="the URL prefix of the repositories (default %s)" % GITHUB)
    args = ap.parse_args(argv)
    args.check = args.check or list(CHECKS)
    args.cache = tempfile.mkdtemp(prefix="release-branch-check.")
    try:
        ok = run(args)
    except Refused as e:
        print("REFUSED  %s" % e)
        ok = False
    finally:
        shutil.rmtree(args.cache, ignore_errors=True)
    print("release-branch-check: %s" % ("every commit is on its release branch" if ok else "REFUSED"))
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
