"""scripts/manifest-from-tree.py - the workstation/CI mirror of the image
manifest: recipe pins are read through their pin files, and a layer's
content hash leaves the pin files out (a component bump is not a platform
change; a recipe-body change is)."""
import importlib.util
import os
import shutil
import subprocess
import tempfile
import unittest

import helpers  # noqa: F401  (sys.path)

REPO = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
SCRIPT = os.path.join(REPO, "scripts", "manifest-from-tree.py")


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
        self.assertEqual(self.mod.LAYER_SKIP_SUFFIXES,
                         (".md", "-pin.inc", "forgefirm-release.inc"))

    def test_layer_hash_ignores_the_release_version(self):
        # The version is metadata: setting the release number must not move
        # the layer hash, or the bump would invalidate the campaign that
        # authorizes the release.
        self.write("forgefirm-release.inc",'FORGEFIRM_RELEASE ?= "0.0.1"\n')
        base = self.mod.layer_content(self.layer)
        self.write("forgefirm-release.inc",'FORGEFIRM_RELEASE ?= "0.0.2"\n')
        self.assertEqual(self.mod.layer_content(self.layer), base)


if __name__ == "__main__":
    unittest.main()
