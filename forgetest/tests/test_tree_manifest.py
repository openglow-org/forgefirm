# Copyright 2026 514 LLC d/b/a OpenGlow
# Written by Scott Wiederhold
# https://community.openglow.org
# SPDX-License-Identifier:    MIT

"""The image manifest's two makers. scripts/manifest-from-tree.py, the
workstation/CI mirror: recipe pins are read through their pin files, and
a layer's content hash leaves the pin files out (a component bump is not a
platform change; a recipe-body change is). The layer's own classes, run on
a host (helpers.bbclass): a component's entry lists what a build of its
checkout compiles, the image's platform carries the kas text and the debug
kernel, and the installed files' hashes stay outside the identity. And what
the pinned components share, read from the pinned sources the mirror
fetches: one fflog in all three."""
import glob
import hashlib
import importlib.util
import os
import re
import shutil
import subprocess
import tempfile
import unittest

import helpers
from forgetest import manifest as manifest_mod

REPO = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
SCRIPT = os.path.join(REPO, "scripts", "manifest-from-tree.py")
GIT_ENV = dict(os.environ, GIT_CONFIG_GLOBAL=os.devnull, GIT_CONFIG_NOSYSTEM="1", GIT_AUTHOR_NAME="t",
               GIT_AUTHOR_EMAIL="t@example.org", GIT_COMMITTER_NAME="t", GIT_COMMITTER_EMAIL="t@example.org")


def run_git(cwd, *args):
    subprocess.run(["git", "-C", cwd, "-c", "protocol.file.allow=always"] + list(args), check=True,
                   stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=GIT_ENV)


def put(path, text):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8", newline="\n") as f:
        f.write(text)


def commit_all(cwd):
    run_git(cwd, "add", "-A")
    run_git(cwd, "commit", "-q", "-m", "change")


# The skip rules forgefirm-image-manifest.bbclass applies, from its variables.
IMAGE_VARS = dict(FORGEFIRM_MANIFEST_CONTENT_LAYERS="meta-forgefirm", FORGEFIRM_MANIFEST_PIN_SUFFIX="-pin.inc",
                  FORGEFIRM_MANIFEST_VERSION_SUFFIX="forgefirm-release.inc", FORGEFIRM_MANIFEST_KAS_SKIP=".lock.yml",
                  MACHINE="glowforge")


def load_script():
    spec = importlib.util.spec_from_file_location("manifest_from_tree", SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def git_ok():
    try:
        subprocess.run(["git", "--version"], stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=True)
        return True
    except (OSError, subprocess.CalledProcessError):
        return False


RECIPE = '''DESCRIPTION = "component"
LICENSE = "MIT"
SRC_URI = "git://github.com/example/comp.git;protocol=https;branch=main"
# SRCREV and PV live in the pin file.
require comp-pin.inc
S = "${WORKDIR}/git"
'''
PIN_A = 'SRCREV = "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"\nPV = "1.0"\n'
PIN_B = 'SRCREV = "bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb"\nPV = "1.1"\n'


@unittest.skipUnless(git_ok(), "git not available")
class TreeManifestTests(unittest.TestCase):
    def setUp(self):
        self.mod = load_script()
        self.tmp = tempfile.mkdtemp(prefix="forgetest-tree-")
        self.layer = os.path.join(self.tmp, "meta-x")
        self.rdir = os.path.join(self.layer, "recipes-x", "comp")
        os.makedirs(self.rdir)
        subprocess.run(["git", "init", "-q"], cwd=self.tmp, check=True,
                       stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        self.write("comp.bb", RECIPE)
        self.write("comp-pin.inc", PIN_A)
        self.write("README.md", "# docs\n")

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def write(self, name, text):
        with open(os.path.join(self.rdir, name), "w", encoding="utf-8", newline="\n") as f:
            f.write(text)

    def test_parse_recipe_follows_the_pin_file(self):
        url, rev = self.mod.parse_recipe(os.path.join(self.rdir, "comp.bb"))
        self.assertEqual(url, "https://github.com/example/comp.git")
        self.assertEqual(rev, "a" * 40)
        self.write("comp-pin.inc", PIN_B)
        self.assertEqual(self.mod.parse_recipe(os.path.join(self.rdir, "comp.bb"))[1], "b" * 40)

    def test_parse_recipe_inline_pin_still_works(self):
        self.write("comp.bb", RECIPE.replace("require comp-pin.inc\n", PIN_A))
        os.remove(os.path.join(self.rdir, "comp-pin.inc"))
        self.assertEqual(self.mod.parse_recipe(os.path.join(self.rdir, "comp.bb"))[1], "a" * 40)

    def test_a_pinned_commits_submodule_is_listed_file_by_file(self):
        # The driver's core is a submodule: CI's manifest lists its files
        # under its path, as the image's does, or no fingerprint sees them.
        lib = os.path.join(self.tmp, "lib")
        put(os.path.join(lib, "core.c"), "core")
        run_git(lib, "init", "-q")
        commit_all(lib)
        comp = os.path.join(self.tmp, "comp")
        put(os.path.join(comp, "src", "main.c"), "main")
        run_git(comp, "init", "-q")
        run_git(comp, "submodule", "add", "-q", lib, "src/grbl")
        commit_all(comp)
        rev = subprocess.run(["git", "-C", comp, "rev-parse", "HEAD"], capture_output=True, text=True,
                             check=True).stdout.strip()
        cache = os.path.join(self.tmp, "cache")
        files = []
        self.mod.ls_tree(self.mod.fetch(comp, rev, cache), rev, comp, cache, "", files)
        self.assertIn("src/grbl/core.c", [p for p, _b in files])
        self.assertIn("src/main.c", [p for p, _b in files])

    def test_the_app_and_the_library_are_pinned_together(self):
        revs = {"forgefirm-app": "a" * 40, "python3-gfhardware": "b" * 40}
        with self.assertRaises(SystemExit):
            self.mod.same_pin(revs, PIN_A)
        # a split the app's pin file says it means
        self.mod.same_pin(revs, PIN_A + "# split from python3-gfhardware: the app needs the new reader first\n")
        self.mod.same_pin({"forgefirm-app": "a" * 40, "python3-gfhardware": "a" * 40}, PIN_A)
        # The script stops on it before it fetches anything, with this
        # tree's app pin file, which says no split.
        from unittest import mock
        pins = lambda path: ("https://example.invalid/x.git",
                             "1" * 40 if "forgefirm-app" in path else "2" * 40)
        with mock.patch.object(self.mod, "parse_recipe", pins), \
                mock.patch.object(self.mod, "fetch", side_effect=AssertionError("fetched")):
            with self.assertRaises(SystemExit) as caught:
                self.mod.main(["--cache", os.path.join(self.tmp, "cache"), "--out", os.devnull])
        self.assertIn("forgefirm-app", str(caught.exception.code))

    def test_layer_hash_ignores_pin_bumps_and_docs(self):
        base = self.mod.layer_content(self.layer)
        self.assertIsNotNone(base)
        self.write("comp-pin.inc", PIN_B)
        self.assertEqual(self.mod.layer_content(self.layer), base, "a pin bump is not layer content")
        self.write("README.md", "# docs, revised\n")
        self.assertEqual(self.mod.layer_content(self.layer), base, "documentation is not layer content")

    def test_layer_hash_sees_recipe_body_changes(self):
        base = self.mod.layer_content(self.layer)
        self.write("comp.bb", RECIPE + 'EXTRA_OEMAKE += "KCFLAGS=-Werror"\n')
        self.assertNotEqual(self.mod.layer_content(self.layer), base)

    def test_layer_hash_sees_a_pin_written_into_the_recipe(self):
        # The safe direction: a pin that bypasses its pin file still hashes.
        base = self.mod.layer_content(self.layer)
        self.write("comp.bb", RECIPE + PIN_B)
        self.assertNotEqual(self.mod.layer_content(self.layer), base)

    def test_layer_hash_matches_the_bbclass_rule(self):
        # The same skip list as *.md plus FORGEFIRM_MANIFEST_PIN_SUFFIX and
        # FORGEFIRM_MANIFEST_VERSION_SUFFIX in
        # forgefirm-image-manifest.bbclass. The two implementations compute
        # the same identity - one on the build host, one on a workstation -
        # so a suffix added to either without the other is the failure this
        # guards.
        bbclass = os.path.join(REPO, "meta-forgefirm", "classes", "forgefirm-image-manifest.bbclass")
        with open(bbclass, encoding="utf-8") as f:
            text = f.read()
        self.assertIn('FORGEFIRM_MANIFEST_PIN_SUFFIX ?= "-pin.inc"', text)
        self.assertIn('FORGEFIRM_MANIFEST_VERSION_SUFFIX ?= "forgefirm-release.inc"', text)
        self.assertIn('FORGEFIRM_MANIFEST_KAS_SKIP ?= ".lock.yml"', text)
        self.assertEqual(self.mod.LAYER_SKIP_SUFFIXES,
                         (".md", "-pin.inc", "forgefirm-release.inc"))
        self.assertEqual(self.mod.KAS_SKIP_SUFFIXES, (".md", ".lock.yml"))

    def test_layer_hash_ignores_the_release_version(self):
        # The version is metadata: setting the release number must not move
        # the layer hash, or the bump would invalidate the campaign that
        # authorizes the release.
        self.write("forgefirm-release.inc",'FORGEFIRM_RELEASE ?= "0.0.1"\n')
        base = self.mod.layer_content(self.layer)
        self.write("forgefirm-release.inc",'FORGEFIRM_RELEASE ?= "0.0.2"\n')
        self.assertEqual(self.mod.layer_content(self.layer), base)

    def image_class_hash(self):
        """The layer's identity as the image class computes it."""
        run_git(self.tmp, "commit", "--allow-empty", "-q", "-m", "head")     # a revision to read
        cls = helpers.bbclass("forgefirm-image-manifest")
        d = helpers.FakeData(BBLAYERS=self.layer, **dict(IMAGE_VARS, FORGEFIRM_MANIFEST_CONTENT_LAYERS="meta-x"))
        return cls["forgefirm_manifest_layers"](d)[0]["meta-x"]["content_sha256"]

    def test_a_file_only_a_local_exclude_hides_is_layer_content(self):
        # bitbake reads an externalsrc bbappend whatever .git/info/exclude
        # says, so the layer hash and the dirty flag count it, in the class
        # and in the mirror alike. The checkout's own .gitignore still
        # leaves a file out.
        cls = helpers.bbclass("forgefirm-image-manifest")
        commit_all(self.tmp)
        base = self.mod.layer_content(self.layer)
        self.assertEqual(self.image_class_hash(), base)
        self.assertFalse(cls["forgefirm_manifest_dirty"](self.layer))
        put(os.path.join(self.tmp, ".gitignore"), "*.pyc\n")
        self.write("comp.pyc", "bytecode")
        commit_all(self.tmp)
        self.assertEqual(self.mod.layer_content(self.layer), base, "a file .gitignore ignores is not content")
        self.write("comp_%.bbappend", 'inherit externalsrc\nEXTERNALSRC = "/src/comp"\n')
        with open(os.path.join(self.tmp, ".git", "info", "exclude"), "a") as f:
            f.write("*.bbappend\n")
        moved = self.mod.layer_content(self.layer)
        self.assertNotEqual(moved, base)
        self.assertEqual(self.image_class_hash(), moved)
        self.assertTrue(cls["forgefirm_manifest_dirty"](self.layer))

    def test_an_edit_the_index_is_told_to_ignore_is_dirty(self):
        # skip-worktree and assume-unchanged keep an edit out of git status;
        # the layer's dirty flag, and a component's source tree, see it.
        image = helpers.bbclass("forgefirm-image-manifest")
        component = helpers.bbclass("forgefirm-manifest")
        commit_all(self.tmp)
        self.assertFalse(image["forgefirm_manifest_dirty"](self.layer))
        self.assertFalse(component["forgefirm_manifest_checkout_dirty"](self.layer))
        for bit in ("--skip-worktree", "--assume-unchanged"):
            run_git(self.tmp, "update-index", bit, os.path.relpath(os.path.join(self.rdir, "comp.bb"), self.tmp))
            self.write("comp.bb", RECIPE + 'EXTRA_OECMAKE += "-DGF_TRACE=1"\n')
            self.assertTrue(image["forgefirm_manifest_dirty"](self.layer), bit)
            self.assertTrue(component["forgefirm_manifest_checkout_dirty"](self.layer), bit)
            run_git(self.tmp, "update-index", bit.replace("--", "--no-"),
                    os.path.relpath(os.path.join(self.rdir, "comp.bb"), self.tmp))
            self.write("comp.bb", RECIPE)

    def test_a_skipped_file_that_assigns_more_than_its_rule_is_refused(self):
        # A pin file holds SRCREV and PV, the version file FORGEFIRM_RELEASE:
        # anything else there would change the build and move no fingerprint.
        self.write("comp-pin.inc", PIN_A + 'EXTRA_OECMAKE += "-DGF_TRACE=1"\n')
        with self.assertRaises(SystemExit):
            self.mod.layer_content(self.layer)
        with self.assertRaises(helpers.BitbakeFatal):
            self.image_class_hash()
        self.write("comp-pin.inc", PIN_A)
        self.write("forgefirm-release.inc", 'FORGEFIRM_RELEASE ?= "0.0.2"\nrequire extra.inc\n')
        with self.assertRaises(SystemExit):
            self.mod.layer_content(self.layer)
        with self.assertRaises(helpers.BitbakeFatal):
            self.image_class_hash()


@unittest.skipUnless(git_ok(), "git not available")
class ComponentEntryTests(unittest.TestCase):
    """forgefirm-manifest.bbclass: a component's entry is what a build of
    its checkout compiles."""

    def setUp(self):
        self.cls = helpers.bbclass("forgefirm-manifest")
        self.tmp = tempfile.mkdtemp(prefix="forgetest-entry-")
        self.src = os.path.join(self.tmp, "comp")
        lib = os.path.join(self.tmp, "lib")
        put(os.path.join(lib, "core.c"), "core")
        run_git(lib, "init", "-q")
        commit_all(lib)
        put(os.path.join(self.src, "src", "main.c"), "main")
        put(os.path.join(self.src, ".gitignore"), "*.o\n/build/\n")
        run_git(self.src, "init", "-q")
        run_git(self.src, "submodule", "add", "-q", lib, "lib")
        commit_all(self.src)
        self.dest = os.path.join(self.tmp, "image")

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def entry(self, **values):
        d = helpers.FakeData(PN="comp", PV="1.0", FORGEFIRM_MANIFEST_NAME="comp", S=self.src,
                             SRC_URI="git://example.org/comp.git;protocol=https", D=self.dest,
                             FORGEFIRM_MANIFEST_DIR="/etc/forgefirm-manifest.d", **values)
        return self.cls["forgefirm_manifest_entry"](d)

    def test_a_clean_checkout_lists_its_commit(self):
        # Build products the checkout's .gitignore covers change nothing.
        put(os.path.join(self.src, "src", "main.o"), "object")
        put(os.path.join(self.src, "build", "CMakeCache.txt"), "cache")
        committed = []
        self.cls["forgefirm_manifest_tree"](committed, self.src, "")
        e = self.entry()
        self.assertEqual(e["files"], sorted(committed))
        self.assertIn("lib/core.c", dict(e["files"]))
        self.assertNotIn("dirty", e)
        self.assertNotIn("local_source", e)

    def test_the_entry_lists_the_working_tree(self):
        # An uncommitted edit, an untracked file, a file a local exclude
        # hides, and an edit inside the submodule: each is compiled, so each
        # is listed as it is, and the entry is dirty.
        put(os.path.join(self.src, "src", "main.c"), "main, edited")
        put(os.path.join(self.src, "src", "extra.c"), "extra")
        put(os.path.join(self.src, "src", "hidden.c"), "hidden")
        with open(os.path.join(self.src, ".git", "info", "exclude"), "a") as f:
            f.write("hidden.c\n")
        put(os.path.join(self.src, "lib", "core.c"), "core, edited")
        e = self.entry()
        files = dict(e["files"])
        self.assertEqual(files["src/main.c"], helpers.blob("main, edited"))
        self.assertEqual(files["src/extra.c"], helpers.blob("extra"))
        self.assertEqual(files["src/hidden.c"], helpers.blob("hidden"))
        self.assertEqual(files["lib/core.c"], helpers.blob("core, edited"))
        self.assertTrue(e["dirty"])

    def test_files_from_a_checkout_that_differs_from_its_commit_are_dirty(self):
        # The acceptance tool's entry: files copied out of the forgefirm
        # checkout. The checkout's state marks the entry, as a layer's
        # marks the layer.
        repo = os.path.join(self.tmp, "forgefirm")
        tree = os.path.join(repo, "forgetest", "forgetest")
        put(os.path.join(tree, "provenance.py"), "judge")
        run_git(repo, "init", "-q")
        commit_all(repo)
        copy = os.path.join(self.tmp, "workdir", "forgetest")
        shutil.copytree(tree, copy)
        d = dict(FORGEFIRM_MANIFEST_SRC=copy, FORGEFIRM_MANIFEST_DIRTY_TREES=tree)
        self.assertNotIn("dirty", self.entry(**d))
        put(os.path.join(tree, "provenance.py"), "judge, edited")
        self.assertTrue(self.entry(**d)["dirty"])
        run_git(repo, "checkout", "--", ".")
        self.assertNotIn("dirty", self.entry(**d))
        # a new suite file, not yet committed
        put(os.path.join(tree, "suite", "new.py"), "a test")
        self.assertTrue(self.entry(**d)["dirty"])

    def test_a_local_checkout_and_the_installed_files_are_recorded(self):
        put(os.path.join(self.dest, "usr", "bin", "comp"), "program")
        put(os.path.join(self.dest, "etc", "init.d", "comp"), "init")
        put(os.path.join(self.dest, "etc", "forgefirm-manifest.d", "comp.json"), "{}")
        e = self.entry(EXTERNALSRC=self.src)
        self.assertTrue(e["local_source"])
        self.assertEqual(e["installed"], ["/etc/init.d/comp", "/usr/bin/comp"])


@unittest.skipUnless(git_ok(), "git not available")
class ImageManifestTests(unittest.TestCase):
    """forgefirm-image-manifest.bbclass on a scratch forgefirm checkout:
    the platform and the build section."""

    def setUp(self):
        self.cls = helpers.bbclass("forgefirm-image-manifest")
        self.tmp = tempfile.mkdtemp(prefix="forgetest-image-")
        self.repo = os.path.join(self.tmp, "forgefirm")
        self.layer = os.path.join(self.repo, "meta-forgefirm")
        put(os.path.join(self.layer, "recipes-x", "comp", "comp.bb"), RECIPE)
        put(os.path.join(self.layer, "recipes-x", "comp", "comp-pin.inc"), PIN_A)
        put(os.path.join(self.repo, "kas", "forgefirm-glowforge.yml"),
            "local_conf_header:\n  eula: |\n    ACCEPT_FSL_EULA = \"1\"\n")
        put(os.path.join(self.repo, "kas", "forgefirm-glowforge.lock.yml"),
            "overrides:\n  repos:\n    poky:\n      commit: %s\n" % ("a" * 40))
        run_git(self.repo, "init", "-q")
        commit_all(self.repo)
        self.rootfs = os.path.join(self.tmp, "rootfs")
        os.makedirs(self.rootfs)

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def platform(self, **values):
        d = helpers.FakeData(BBLAYERS=self.layer, **dict(IMAGE_VARS, **values))
        return self.cls["forgefirm_manifest_platform"](d, self.rootfs)

    def test_the_kas_configuration_is_platform(self):
        # A local_conf_header line changes the image: the text of the kas
        # files is in the platform. The lock file's commits are not (the
        # layers carry them).
        # The tree manifest computes the same hash (the coverage lint's).
        mirror = load_script().kas_content
        kas = os.path.join(self.repo, "kas")
        base, build = self.platform()
        self.assertFalse(build["kas"]["dirty"])
        self.assertEqual(mirror(kas), base["kas"]["content_sha256"])
        put(os.path.join(self.repo, "kas", "forgefirm-glowforge.lock.yml"),
            "overrides:\n  repos:\n    poky:\n      commit: %s\n" % ("b" * 40))
        self.assertEqual(self.platform()[0]["kas"], base["kas"])
        self.assertEqual(mirror(kas), base["kas"]["content_sha256"])
        put(os.path.join(self.repo, "kas", "forgefirm-glowforge.yml"),
            "local_conf_header:\n  eula: |\n    ACCEPT_FSL_EULA = \"1\"\n"
            "  extra: |\n    IMAGE_INSTALL:append = \" strace\"\n")
        plat, build = self.platform()
        self.assertNotEqual(plat["kas"], base["kas"])
        self.assertEqual(mirror(kas), plat["kas"]["content_sha256"])
        self.assertTrue(build["kas"]["dirty"])

    def test_the_debug_kernel_moves_every_fingerprint(self):
        comps = {"comp": {"srcrev": "a" * 40, "files": [["src/main.c", "1" * 40]]}}
        docs = {}
        for debug in (None, "1"):
            plat, build = self.platform(FORGEFIRM_KERNEL_DEBUG=debug)
            self.assertEqual(plat.get("kernel_debug"), True if debug else None)
            docs[debug] = manifest_mod.Manifest(
                self.cls["forgefirm_manifest_document"]("img", "v", comps, plat, build))
        fps = [manifest_mod.fingerprint(m, [("comp", "src/**")]) for m in docs.values()]
        self.assertNotEqual(fps[0], fps[1])

    def test_the_installed_files_stay_outside_the_identity(self):
        entries = [{"component": "comp", "recipe": "comp", "srcrev": "a" * 40, "files": [["src/main.c", "1" * 40]],
                    "installed": ["/usr/bin/comp", "/usr/share/comp/absent"]},
                   {"component": "linux-fslc", "recipe": "linux-fslc", "srcrev": "e" * 40,
                    "files": [["@srcrev", "e" * 40]], "kernel_build_id": "f" * 40}]
        plat, build = self.platform()
        docs = []
        for program in ("as built", "hot-deployed"):
            put(os.path.join(self.rootfs, "usr", "bin", "comp"), program)
            components, installed, kernel_id = self.cls["forgefirm_manifest_components"](entries)
            self.assertNotIn("installed", components["comp"])
            self.assertNotIn("kernel_build_id", components["linux-fslc"])
            self.assertEqual(kernel_id, "f" * 40)
            b = dict(build, installed=self.cls["forgefirm_manifest_installed_hashes"](self.rootfs, installed))
            docs.append(self.cls["forgefirm_manifest_document"]("img", "v", components, plat, b))
        self.assertEqual(docs[0]["content_sha256"], docs[1]["content_sha256"])
        self.assertEqual(docs[0]["build"]["installed"]["comp"],
                         {"/usr/bin/comp": hashlib.sha256(b"as built").hexdigest()})
        self.assertNotEqual(docs[0]["build"]["installed"], docs[1]["build"]["installed"])

    def test_one_components_recipes_merge_their_marks(self):
        # forgefirm-app is three recipes of one checkout: a mark on any one
        # marks the component.
        entry = {"component": "forgefirm-app", "srcrev": "a" * 40, "files": [["gfcloud.py", "1" * 40]]}
        entries = [dict(entry, recipe="gfcloud"), dict(entry, recipe="gfhome", dirty=True),
                   dict(entry, recipe="python3-ffmachine", local_source=True)]
        comp = self.cls["forgefirm_manifest_components"](entries)[0]["forgefirm-app"]
        self.assertTrue(comp["dirty"])
        self.assertTrue(comp["local_source"])

    def test_the_boot_files_directories_and_kernel_are_recorded(self):
        def note(build_id, kind=3):
            desc = bytes.fromhex(build_id)
            return (b"\x04\x00\x00\x00" + len(desc).to_bytes(4, "little") + kind.to_bytes(4, "little")
                    + b"GNU\x00" + desc)
        import lzma
        rel = "6.12.20-fslc+g0e01ec9f0d3f"
        put(os.path.join(self.rootfs, "boot", "zImage-6.12.20"), "kernel")
        os.symlink("zImage-6.12.20", os.path.join(self.rootfs, "boot", "zImage"))
        put(os.path.join(self.rootfs, "boot", "uEnv.txt"), "bootargs")
        put(os.path.join(self.rootfs, "usr", "bin", "comp"), "program")
        put(os.path.join(self.rootfs, "usr", "bin", "other"), "not a component's")
        mods = os.path.join(self.rootfs, "lib", "modules", rel)
        os.makedirs(os.path.join(mods, "extras"))
        with open(os.path.join(mods, "extras", "glowforge.ko"), "wb") as f:
            # a GNU property note comes before the build ID
            f.write(b"\x7fELF" + b"\0" * 32 + note("00" * 8, kind=5) + note("a1" * 20))
        os.makedirs(os.path.join(mods, "kernel"))
        with lzma.open(os.path.join(mods, "kernel", "ov-5648.ko.xz"), "wb") as f:
            f.write(b"\x7fELF" + note("b2" * 20))
        os.chmod(os.path.join(self.rootfs, "usr", "bin", "comp"), 0o755)
        put(os.path.join(self.rootfs, "usr", "lib", "libc.so.6"), "libc")
        put(os.path.join(self.rootfs, "usr", "lib", "python3.12", "site-packages", "_cam.cpython-312.so"), "ext")
        put(os.path.join(self.rootfs, "usr", "lib", "python3.12", "os.py"), "module")
        boot = self.cls["forgefirm_manifest_boot_hashes"](self.rootfs)
        self.assertEqual(sorted(boot), ["/boot/uEnv.txt", "/boot/zImage-6.12.20"])
        watched = self.cls["forgefirm_manifest_watched_dirs"](self.rootfs)
        self.assertEqual(watched, ["/", "/usr/lib/python3.12", "/usr/lib/python3.12/site-packages"])
        dirs = self.cls["forgefirm_manifest_dir_entries"](self.rootfs, ["/usr/bin/comp"] + list(boot),
                                                          also=["/usr/bin/written-later", "/lost+found"],
                                                          dirs=watched)
        self.assertEqual(dirs, {"/": ["boot", "lib", "lost+found", "usr"],
                                "/boot": ["uEnv.txt", "zImage", "zImage-6.12.20"],
                                "/usr/bin": ["comp", "other", "written-later"],
                                "/usr/lib/python3.12": ["os.py", "site-packages"],
                                "/usr/lib/python3.12/site-packages": ["_cam.cpython-312.so"]})
        # the programs and shared objects; not a module, a data file or a link
        self.assertEqual(self.cls["forgefirm_manifest_code"](self.rootfs),
                         ["/usr/bin/comp", "/usr/lib/libc.so.6", "/usr/lib/python3.12/site-packages/_cam.cpython-312.so"])
        kern = self.cls["forgefirm_manifest_kernel"](self.rootfs, "c3" * 20)
        self.assertEqual(kern, {"release": [rel], "build_id": "c3" * 20,
                                "modules": {"glowforge": "a1" * 20, "ov_5648": "b2" * 20}})


def pinned_file(recipe, path):
    """A file of a component at the revision its recipe pins, from the
    pinned sources manifest-from-tree.py fetched into this checkout
    (.manifest-cache); None when they do not hold it."""
    with open(os.path.join(REPO, "meta-forgefirm", "recipes-forgefirm", recipe, recipe + "-pin.inc"),
              encoding="utf-8") as f:
        rev = re.search(r'^SRCREV = "([0-9a-f]{40})"$', f.read(), re.M).group(1)
    for repo in sorted(glob.glob(os.path.join(REPO, ".manifest-cache", "*"))):
        p = subprocess.run(["git", "-C", repo, "show", "%s:%s" % (rev, path)], capture_output=True)
        if p.returncode == 0:
            return p.stdout
    return None


def pinned_tree(pin):
    """(bare repository, revision) of the component a pin file pins, from
    .manifest-cache; (None, rev) when the cache does not hold it."""
    with open(pin, encoding="utf-8") as f:
        rev = re.search(r'^SRCREV = "([0-9a-f]{40})"$', f.read(), re.M).group(1)
    for repo in sorted(glob.glob(os.path.join(REPO, ".manifest-cache", "*"))):
        if subprocess.run(["git", "-C", repo, "cat-file", "-e", rev + "^{commit}"], capture_output=True).returncode == 0:
            return repo, rev
    return None, rev


# A variable read in C (getenv) and in Python (os.environ, os.getenv).
C_ENV = re.compile(r'getenv\("([A-Za-z0-9_]+)"\)')
PY_ENV = re.compile(r'''os\.(?:environ\.(?:get|pop|setdefault)\(|getenv\(|environ\[)\s*["']([A-Za-z0-9_]+)["']''')
# A component's own list of what it reads (forgectrl, the controller,
# forgeext), which each component's tests hold to its sources.
C_LISTS = re.compile(r'(?:ENV_OVERRIDES|TEST_HOOKS)\[\]\s*=\s*\{(.*?)\};', re.S)


@unittest.skipUnless(git_ok(), "git not available")
class PinnedSourcesTests(unittest.TestCase):
    """What the pinned components share, read from their pinned sources."""

    def test_every_variable_a_component_reads_is_judged(self):
        # The verdict refuses a process that carries a variable which
        # changes what runs (provenance.SUBSTITUTES). Every variable a
        # pinned component, or the acceptance tool itself, reads is there
        # or in HARMLESS, so an override added to a component and not to
        # the list fails here.
        from forgetest import provenance
        mo = os.path.join(os.path.dirname(REPO), "meta-openglow")
        pins = {
            "forgectrl": os.path.join(REPO, "meta-forgefirm/recipes-forgefirm/forgectrl/forgectrl-pin.inc"),
            "grblhal-glowforge": os.path.join(REPO, "meta-forgefirm/recipes-forgefirm/grblhal-glowforge/"
                                                    "grblhal-glowforge-pin.inc"),
            "forgeext": os.path.join(REPO, "meta-forgefirm/recipes-forgefirm/forgeext/forgeext-pin.inc"),
            "forgefirm-app": os.path.join(REPO, "meta-forgefirm/recipes-forgefirm/forgefirm-app/"
                                                "forgefirm-app-pin.inc"),
            "python3-gfhardware": os.path.join(mo, "meta-glowforge-bsp/recipes-devtools/python/"
                                                   "python3-gfhardware-pin.inc"),
            "python3-gfutilities": os.path.join(mo, "meta-openglow-core/recipes-devtools/python/"
                                                    "python3-gfutilities-pin.inc"),
        }
        names, missing = {}, []
        for comp, pin in sorted(pins.items()):
            repo, rev = pinned_tree(pin) if os.path.isfile(pin) else (None, None)
            if repo is None:
                missing.append(comp)
                continue
            for pattern, rx in (("getenv\\(\"", C_ENV), ("os\\.(environ|getenv)", PY_ENV)):
                p = subprocess.run(["git", "-C", repo, "grep", "-E", pattern, rev], capture_output=True, text=True)
                for line in p.stdout.splitlines():
                    # rev:path:line; what ships only (no tests, docs, author kits)
                    _rev, path, text = line.split(":", 2)
                    if not manifest_mod.non_behavioral(comp, path):
                        for name in rx.findall(text):
                            names.setdefault(name, comp)
            for path in ("src/main.c", "src/run.c"):
                p = subprocess.run(["git", "-C", repo, "show", "%s:%s" % (rev, path)], capture_output=True, text=True)
                for block in C_LISTS.findall(p.stdout if p.returncode == 0 else ""):
                    for name in re.findall(r'"([A-Za-z0-9_]+)"', block):
                        names.setdefault(name, comp)
        if missing:
            why = "the pinned sources of %s are not in .manifest-cache (manifest-from-tree.py fetches them)" % (
                ", ".join(missing))
            if os.environ.get("GITHUB_ACTIONS"):
                self.fail(why)
            self.skipTest(why)
        for path in glob.glob(os.path.join(REPO, "forgetest", "forgetest", "**", "*.py"), recursive=True):
            with open(path, encoding="utf-8") as f:
                for name in PY_ENV.findall(f.read()):
                    names.setdefault(name, "forgetest")
        judged = set(provenance.SUBSTITUTES) | set(provenance.HARMLESS)
        self.assertEqual(sorted("%s (%s)" % (n, c) for n, c in names.items() if n not in judged), [])

    def test_the_three_fflog_copies_are_one(self):
        # forgectrl's host tests prove fflog; grblHAL and forgeext carry
        # copies of it, so the proof holds for them only while all three
        # pinned copies are the same bytes.
        for path in ("src/fflog.c", "src/fflog.h"):
            copies = {r: pinned_file(r, path) for r in ("forgectrl", "grblhal-glowforge", "forgeext")}
            missing = sorted(r for r, text in copies.items() if text is None)
            if missing:
                why = "%s of %s at its pin is not in .manifest-cache (manifest-from-tree.py fetches it)" % (
                    path, ", ".join(missing))
                if os.environ.get("GITHUB_ACTIONS"):
                    self.fail(why)
                self.skipTest(why)
            self.assertEqual(len(set(copies.values())), 1,
                             "%s differs between the pinned components: %s" % (
                                 path, {r: hashlib.sha256(t).hexdigest()[:12] for r, t in copies.items()}))


if __name__ == "__main__":
    unittest.main()
