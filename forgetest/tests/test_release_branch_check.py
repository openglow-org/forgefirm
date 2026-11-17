# Copyright 2026 514 LLC d/b/a OpenGlow
# Written by Scott Wiederhold
# https://community.openglow.org
# SPDX-License-Identifier:    MIT

"""scripts/release-branch-check.py - a release builds only from its release
branches.

The script reads every commit the release image builds from (the seven
component pins, the BSP pins at the commit the kas lock names, the core
commit the driver's gitlink names, the lock commit, and forgefirm's own
commit, from forgefirm at --rev) and refuses any that its repository's
release branch on GitHub does not hold, any recipe that does not name its
repository and that branch (or sets nobranch), and a kas entry, configuration
or lock that does not leave meta-openglow on scarthgap of the GitHub
repository. These tests run it against made-up
repositories served from a directory: one pass, then one refusal for each
rule, and one for a repository it cannot reach."""
import os
import shutil
import subprocess
import tempfile
import unittest

REPO = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
SCRIPT = os.path.join(REPO, "scripts", "release-branch-check.py")
GIT = ["git", "-c", "user.name=test", "-c", "user.email=test@example.org", "-c", "init.defaultBranch=main",
       "-c", "commit.gpgsign=false"]
# (recipe directory and file, pin file, repository, release branch, scheme)
FORGEFIRM_RECIPES = {
    "forgectrl": ("meta-forgefirm/recipes-forgefirm/forgectrl/forgectrl.bb",
                  "meta-forgefirm/recipes-forgefirm/forgectrl/forgectrl-pin.inc", "forgectrl", "main", "git"),
    "forgeext": ("meta-forgefirm/recipes-forgefirm/forgeext/forgeext.bb",
                 "meta-forgefirm/recipes-forgefirm/forgeext/forgeext-pin.inc", "forgeext", "main", "git"),
    "grblhal-glowforge": ("meta-forgefirm/recipes-forgefirm/grblhal-glowforge/grblhal-glowforge.bb",
                          "meta-forgefirm/recipes-forgefirm/grblhal-glowforge/grblhal-glowforge-pin.inc",
                          "grblHAL-glowforge", "main", "gitsm"),
    "forgefirm-app": ("meta-forgefirm/recipes-forgefirm/forgefirm-app/forgefirm-app.inc",
                      "meta-forgefirm/recipes-forgefirm/forgefirm-app/forgefirm-app-pin.inc",
                      "python3-gfhardware", "master", "git"),
}
BSP_RECIPES = {
    "kernel-module-glowforge": ("meta-glowforge-bsp/recipes-kernel/kernel-modules/kernel-module-glowforge.bb",
                                "meta-glowforge-bsp/recipes-kernel/kernel-modules/kernel-module-glowforge-pin.inc",
                                "kernel-module-glowforge", "master", "git"),
    "python3-gfhardware": ("meta-glowforge-bsp/recipes-devtools/python/python3-gfhardware.bb",
                           "meta-glowforge-bsp/recipes-devtools/python/python3-gfhardware-pin.inc",
                           "python3-gfhardware", "master", "git"),
    "python3-gfutilities": ("meta-openglow-core/recipes-devtools/python/python3-gfutilities_git.bb",
                            "meta-openglow-core/recipes-devtools/python/python3-gfutilities-pin.inc",
                            "Glowforge-Utilities", "master", "git"),
}
KAS = """repos:
  forgefirm:
    layers:
      meta-forgefirm:
  meta-openglow:
    url: https://github.com/openglow-org/meta-openglow.git
    branch: %s
    path: layers/meta-openglow
"""
def kas_config(entry):
    """The kas configuration with the given lines as its meta-openglow entry."""
    return KAS.split("  meta-openglow:")[0] + "  meta-openglow:\n" + "".join("    %s\n" % ln for ln in entry)


LOCK = """header:
  version: 14
overrides:
  repos:
    meta-openglow:
      commit: %s
"""


def git(cwd, *args):
    return subprocess.run(GIT + list(args), cwd=cwd, check=True, capture_output=True,
                          text=True).stdout.strip()


def write(root, files):
    for path, text in files.items():
        full = os.path.join(root, path)
        os.makedirs(os.path.dirname(full), exist_ok=True)
        with open(full, "w", encoding="utf-8") as f:
            f.write(text)


def recipe(scheme, repo, branch, pin_file):
    return 'SRC_URI = "%s://github.com/openglow-org/%s.git;protocol=https;branch=%s"\nrequire %s\n' % (
        scheme, repo, branch, os.path.basename(pin_file))


def layer_files(recipes, pins, branches=None):
    files = {}
    for comp, (path, pin_file, repo, branch, scheme) in recipes.items():
        files[path] = recipe(scheme, repo, (branches or {}).get(comp, branch), pin_file)
        files[pin_file] = 'SRCREV = "%s"\nPV = "1"\n' % pins[comp]
    return files


class World:
    """The repositories a release builds from, each a bare repository in
    `gh` (the made-up GitHub) with a work tree beside it."""

    def __init__(self, root):
        self.gh = os.path.join(root, "gh")
        self.work = os.path.join(root, "work")
        os.makedirs(self.gh)
        os.makedirs(self.work)
        self.n = 0

    def repo(self, name):
        bare = os.path.join(self.gh, name + ".git")
        if not os.path.isdir(bare):
            git(self.gh, "init", "-q", "--bare", bare)
            git(bare, "config", "uploadpack.allowAnySHA1InWant", "true")
            git(self.work, "init", "-q", name)
            git(os.path.join(self.work, name), "remote", "add", "origin", bare)
        return os.path.join(self.work, name)

    def commit(self, name, branch, files=None, start=None, gitlink=None):
        """A commit on `branch` (made from `start`, or the current branch, when
        new), pushed; its id."""
        w = self.repo(name)
        if git(w, "branch", "--list", branch):
            git(w, "checkout", "-q", branch)
        else:
            git(w, "checkout", "-q", "-b", branch, *([start] if start else []))
        self.n += 1
        write(w, dict(files or {}, **{"serial": "%d\n" % self.n}))
        git(w, "add", "-A")
        if gitlink:
            git(w, "update-index", "--add", "--cacheinfo", "160000,%s,src/grbl" % gitlink)
        git(w, "commit", "-q", "-m", "commit %d" % self.n)
        git(w, "push", "-q", "-f", "origin", branch)
        return git(w, "rev-parse", "HEAD")


class ReleaseBranchCheckTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.mkdtemp(prefix="release-branch-check-test.")
        w = cls.world = World(cls.tmp)
        # Each component repository: one commit on its release branch, one on next only.
        rel, nxt = {}, {}
        for _r, _p, repo, branch, _s in list(FORGEFIRM_RECIPES.values()) + list(BSP_RECIPES.values()):
            if repo != "grblHAL-glowforge" and repo not in rel:
                rel[repo] = w.commit(repo, branch)
                nxt[repo] = w.commit(repo, "next", start=branch)
        cls.core_rel = w.commit("grblHAL-core", "forgefirm")
        core_next = w.commit("grblHAL-core", "next", start="forgefirm")
        rel["grblHAL-glowforge"] = w.commit("grblHAL-glowforge", "main", gitlink=cls.core_rel)
        # On main, but its gitlink names a core commit the fork's forgefirm does not hold.
        cls.drv_badlink = w.commit("grblHAL-glowforge", "main", gitlink=core_next)
        recipes = dict(FORGEFIRM_RECIPES, **BSP_RECIPES)
        cls.pins = {comp: rel[v[2]] for comp, v in recipes.items()}
        cls.nxt = {comp: nxt.get(v[2]) for comp, v in recipes.items()}
        bsp_pins = {c: cls.pins[c] for c in BSP_RECIPES}
        cls.bsp_rel = w.commit("meta-openglow", "scarthgap", layer_files(BSP_RECIPES, bsp_pins))
        # Three meta-openglow commits on next only: one whose kernel-module pin
        # is a next-only commit, one with every pin and name on the release
        # branches, and one whose kernel-module recipe names next.
        cls.bsp_next = w.commit("meta-openglow", "next", layer_files(
            BSP_RECIPES, dict(bsp_pins, **{"kernel-module-glowforge": cls.nxt["kernel-module-glowforge"]})),
            start="scarthgap")
        cls.bsp_next_clean = w.commit("meta-openglow", "next", layer_files(BSP_RECIPES, bsp_pins))
        cls.bsp_next_names = w.commit("meta-openglow", "next", layer_files(
            BSP_RECIPES, bsp_pins, {"kernel-module-glowforge": "next"}))
        cls.base = w.commit("forgefirm", "master", dict(
            layer_files(FORGEFIRM_RECIPES, cls.pins),
            **{"kas/forgefirm-glowforge.yml": KAS % "scarthgap",
               "kas/forgefirm-glowforge.lock.yml": LOCK % cls.bsp_rel}))
        cls.ff = w.repo("forgefirm")
        cls.script = os.path.join(cls.ff, "scripts", "release-branch-check.py")
        os.makedirs(os.path.dirname(cls.script))
        shutil.copy(SCRIPT, cls.script)
        with open(os.path.join(cls.ff, ".git", "info", "exclude"), "a") as f:
            f.write("scripts/\n")

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def run_check(self, files=None, push=True, checks=(), rev=None):
        """forgefirm's base commit with `files` changed, committed (and pushed
        to master when `push`), then the script on it. With `rev`, that
        commit is left on the branch `rev` alone, the checkout goes back to
        the base, and the script is given --rev."""
        ff = self.ff
        git(ff, "checkout", "-q", "-f", "master")
        git(ff, "reset", "-q", "--hard", self.base)
        git(ff, "push", "-q", "-f", "origin", "%s:refs/heads/master" % self.base)
        if files:
            write(ff, files)
            git(ff, "commit", "-q", "-am", "the case")
            if push:
                git(ff, "push", "-q", "-f", "origin", "HEAD:master")
            if rev:
                git(ff, "branch", "-q", "-f", rev, "HEAD")
                git(ff, "reset", "-q", "--hard", self.base)
        args = ["python3", "-B", self.script, "--github", "file://%s/" % self.world.gh]
        for c in checks:
            args += ["--check", c]
        if rev:
            args += ["--rev", rev]
        p = subprocess.run(args, cwd=ff, capture_output=True, text=True, timeout=300)
        return p.returncode, p.stdout + p.stderr

    def refused(self, out, check, what):
        """The REFUSED lines of one check that name `what`."""
        return [ln for ln in out.splitlines()
                if ln.split()[:2] == ["REFUSED", check] and what in ln]

    def test_a_release_on_its_branches_passes(self):
        rc, out = self.run_check()
        self.assertEqual((rc, [ln for ln in out.splitlines() if ln.startswith("REFUSED")]), (0, []), out)

    def test_each_commit_off_its_release_branch_is_refused(self):
        recipe_path, pin = FORGEFIRM_RECIPES["forgectrl"][:2]
        drv_pin = FORGEFIRM_RECIPES["grblhal-glowforge"][1]
        lock = "kas/forgefirm-glowforge.lock.yml"
        config = "kas/forgefirm-glowforge.yml"
        next_pin = {pin: 'SRCREV = "%s"\nPV = "1"\n' % self.nxt["forgectrl"]}
        # (label, [(the check, what its refusal names)], the checks run, files, push, --rev)
        cases = [
            ("a component pin on next only", [("pins", "forgectrl")], ("pins",), next_pin, True, None),
            ("a BSP pin on next only, read at the lock's commit", [("pins", "kernel-module-glowforge")], ("pins",),
             {lock: LOCK % self.bsp_next}, True, None),
            ("a core gitlink off the fork's forgefirm", [("pins", "grblHAL-core")], ("pins",),
             {drv_pin: 'SRCREV = "%s"\nPV = "1"\n' % self.drv_badlink}, True, None),
            ("a lock commit on next only", [("lock", "meta-openglow")], ("lock",),
             {lock: LOCK % self.bsp_next}, True, None),
            # Every check at once: reading the BSP recipes fetches the lock's
            # commit by its id first, and that does not put it on scarthgap.
            ("a lock commit on next only, with every check", [("lock", "meta-openglow")], (),
             {lock: LOCK % self.bsp_next_clean}, True, None),
            ("forgefirm's commit not on master", [("forgefirm", "forgefirm")], ("forgefirm",),
             {config: KAS % "scarthgap" + "\n"}, False, None),
            # Every input read at REV, with the checkout at the base: a pin,
            # the lock (its commit, and a branch of its own), the kas entry,
            # and forgefirm's own commit.
            ("--rev names a commit that is not the checkout",
             [("pins", "forgectrl"), ("pins", "kernel-module-glowforge"), ("lock", "meta-openglow"),
              ("forgefirm", "forgefirm"), ("names", "kas meta-openglow"), ("names", "kas lock meta-openglow")], (),
             dict(next_pin, **{lock: LOCK % self.bsp_next + "      branch: null\n", config: KAS % "next"}),
             False, "the-release"),
            ("a recipe that names next", [("names", "forgectrl")], ("names",),
             {recipe_path: recipe("git", "forgectrl", "next", pin)}, True, None),
            ("a BSP recipe at the lock's commit that names next", [("names", "kernel-module-glowforge")], ("names",),
             {lock: LOCK % self.bsp_next_names}, True, None),
            ("a recipe that names no branch", [("names", "forgectrl")], ("names",),
             {recipe_path: recipe("git", "forgectrl", "main", pin).replace(";branch=main", "")}, True, None),
            ("a recipe with nobranch=1", [("names", "forgectrl")], ("names",),
             {recipe_path: recipe("git", "forgectrl", "main;nobranch=1", pin)}, True, None),
            ("a recipe that fetches another repository", [("names", "forgectrl")], ("names",),
             {recipe_path: recipe("git", "forgeext", "main", pin)}, True, None),
            ("a kas entry that names next", [("names", "kas meta-openglow")], ("names",),
             {config: KAS % "next"}, True, None),
            ("a kas entry that names no branch", [("names", "kas meta-openglow")], ("names",),
             {config: (KAS % "scarthgap").replace("    branch: scarthgap\n", "")}, True, None),
            ("a kas entry with no url (a local checkout) that names scarthgap", [("names", "kas url meta-openglow")],
             ("names",), {config: kas_config(["branch: scarthgap", "path: ../meta-openglow"])}, True, None),
            ("a kas entry in the commented local-sibling form", [("names", "kas meta-openglow"),
                                                                 ("names", "kas url meta-openglow")],
             ("names",), {config: kas_config(["path: ../meta-openglow", "layers:", "  meta-glowforge-bsp:"])},
             True, None),
            ("a kas configuration that overrides the entry itself", [("names", "kas overrides meta-openglow")],
             ("names",), {config: KAS % "scarthgap" + "overrides:\n  repos:\n    meta-openglow:\n      branch: null\n"},
             True, None),
            ("a lock that sets the branch", [("names", "kas lock meta-openglow")], ("names",),
             {lock: LOCK % self.bsp_rel + "      branch: null\n"}, True, None),
        ]
        for label, expects, checks, files, push, rev in cases:
            with self.subTest(label):
                rc, out = self.run_check(files, push, checks, rev)
                self.assertEqual(rc, 1, out)
                for check, what in expects:
                    self.assertTrue(self.refused(out, check, what), "%s %s:\n%s" % (check, what, out))

    def test_a_repository_it_cannot_reach_is_refused(self):
        bare = os.path.join(self.world.gh, "Glowforge-Utilities.git")
        os.rename(bare, bare + ".away")
        try:
            rc, out = self.run_check(checks=("pins",))
        finally:
            os.rename(bare + ".away", bare)
        self.assertEqual(rc, 1, out)
        self.assertTrue(self.refused(out, "pins", "python3-gfutilities"), out)


if __name__ == "__main__":
    unittest.main()
