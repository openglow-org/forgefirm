"""scripts/source-bundle.py - the source that a release publishes.

The bundle must hold the source of every recipe of the image whose license
makes source necessary. The list comes from the license manifests that the
build writes, so a package cannot reach a machine with its source left
behind. These tests hold the decision (which recipe needs source), the
choice of archive (the version of the image, not an older build), and the
refusal that stops a release with source missing."""
import importlib.util
import json
import os
import tarfile
import unittest

import helpers  # noqa: F401  (sys.path)

REPO = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
SCRIPT = os.path.join(REPO, "scripts", "source-bundle.py")
META_OPENGLOW = os.path.join(REPO, "..", "meta-openglow")
IMAGE = "forgefirm-image-glowforge.rootfs-20260101000000"


def load_script():
    spec = importlib.util.spec_from_file_location("source_bundle", SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def package_block(recipe, version, license_, package=None):
    return ("PACKAGE NAME: %s\nPACKAGE VERSION: %s\nRECIPE NAME: %s\nLICENSE: %s\n\n"
            % (package or recipe, version, recipe, license_))


def write(path, text):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        f.write(text)


def make_deploy(root, packages, archives, image_packages=()):
    """A deploy directory with the license manifests of one image and the
    archive directories of the archiver pass."""
    licdir = os.path.join(root, "licenses", "glowforge", IMAGE)
    # The bundle answers for one image: its license manifests and its build
    # identity must be in the deploy directory.
    write(os.path.join(root, "images", "glowforge",
                       IMAGE + ".forgefirm-manifest.json"),
          '{"content_sha256": "0"}\n')
    write(os.path.join(licdir, "license.manifest"),
          "".join(package_block(*p) for p in packages))
    write(os.path.join(licdir, "image_license.manifest"),
          "".join("RECIPE NAME: %s\nVERSION: %s\nLICENSE: %s\nFILES: x\n\n" % p
                  for p in image_packages))
    for name in archives:
        write(os.path.join(root, "sources", "arm-fslc-linux-gnueabi", name,
                           "%s.tar.gz" % name), "source of %s\n" % name)
    return licdir


class Policy(unittest.TestCase):
    """The archiver pass and the check read one license list."""

    def setUp(self):
        self.mod = load_script()
        self.policy = self.mod.read_policy(self.mod.KAS_OVERLAY)

    def test_the_overlay_carries_the_filter(self):
        self.assertIn("GPL*", self.policy["COPYLEFT_LICENSE_INCLUDE"])
        self.assertIn("LGPL*", self.policy["COPYLEFT_LICENSE_INCLUDE"])
        self.assertIn("Proprietary", self.policy["COPYLEFT_LICENSE_EXCLUDE"])
        # The ForgeFIRM components are MIT and travel with the release too.
        self.assertIn("forgectrl", self.policy["COPYLEFT_PN_INCLUDE"])
        self.assertIn("gfcloud", self.policy["COPYLEFT_PN_INCLUDE"])

    def test_a_copyleft_license_needs_source(self):
        for expr in ("GPL-2.0-only",
                     "LGPL-2.1-or-later",
                     "GPL-2.0-only & bzip2-1.0.4",
                     "AFL-2.1 | GPL-2.0-or-later",
                     "(GPL-2.0-or-later | LGPL-3.0-only) & Unicode-DFS-2016",
                     "MIT ; LGPL-2.1-or-later",
                     "GPL-3.0-with-GCC-exception"):
            self.assertTrue(self.mod.source_needed(expr, self.policy), expr)

    def test_a_permissive_license_does_not(self):
        for expr in ("MIT", "BSD-3-Clause", "Apache-2.0", "ISC", "PSF-2.0",
                     "Proprietary", "Firmware-imx-sdma_firmware", "PD"):
            self.assertFalse(self.mod.source_needed(expr, self.policy), expr)


class Manifests(unittest.TestCase):

    def setUp(self):
        self.mod = load_script()

    def test_a_recipe_collects_the_licenses_of_its_packages(self):
        path = os.path.join(self.tmp(), "license.manifest")
        write(path, package_block("avahi", "0.8", "LGPL-2.1-or-later", "avahi-daemon")
              + package_block("avahi", "0.8", "GPL-2.0-or-later & LGPL-2.1-or-later",
                              "avahi-locale-en-gb"))
        recipes = self.mod.parse_manifest(path, "PACKAGE VERSION")
        self.assertEqual(list(recipes), ["avahi"])
        self.assertEqual(recipes["avahi"]["version"], "0.8")
        self.assertEqual(len(recipes["avahi"]["licenses"]), 2)

    def test_the_archive_of_the_image_version_wins(self):
        index = {"linux-fslc": [("6.11.0", "/old"), ("6.12.20+git0+844aa34", "/new")],
                 "busybox": [("1.36.1", "/b1"), ("1.35.0", "/b0")],
                 "forgectrl": [("1_0.1.4", "/fc")]}
        # An exact version first.
        self.assertEqual(self.mod.select_archives(index, "busybox", "1.36.1"), ["/b1"])
        # Then a version that starts with it: the kernel carries the
        # revision of its git source in PV, and the manifest does not.
        self.assertEqual(self.mod.select_archives(index, "linux-fslc", "6.12.20+git"),
                         ["/new"])
        # The name of an archive carries the epoch of the recipe, and a
        # license manifest does not.
        self.assertEqual(self.mod.select_archives(index, "forgectrl", "0.1.4"), ["/fc"])
        self.assertEqual(self.mod.select_archives(index, "curl", "8.7.1"), [])

    def test_a_directory_belongs_to_the_longest_recipe_name(self):
        root = self.tmp()
        make_deploy(root, [], ["python3-3.12.13-r0", "python3-certifi-2024.2.2-r0"])
        index = self.mod.index_archives(os.path.join(root, "sources"),
                                        {"python3", "python3-certifi"})
        self.assertEqual([v for v, _ in index["python3"]], ["3.12.13"])
        self.assertEqual([v for v, _ in index["python3-certifi"]], ["2024.2.2"])

    def tmp(self):
        import tempfile
        d = tempfile.mkdtemp()
        self.addCleanup(__import__("shutil").rmtree, d, True)
        return d


@unittest.skipUnless(os.path.isdir(META_OPENGLOW),
                     "the meta-openglow sibling checkout is not here")
class Pack(unittest.TestCase):
    """The bundle end to end, on a deploy directory of made-up recipes."""

    def setUp(self):
        import tempfile
        self.mod = load_script()
        self.root = tempfile.mkdtemp()
        self.addCleanup(__import__("shutil").rmtree, self.root, True)
        self.deploy = os.path.join(self.root, "deploy")
        self.out = os.path.join(self.root, "out")

    def run_main(self, packages, archives, image_packages=()):
        make_deploy(self.deploy, packages, archives, image_packages)
        import sys
        argv = sys.argv
        sys.argv = ["source-bundle.py", "0.0.1", "--deploy", self.deploy,
                    "--image-name", IMAGE, "--out", self.out]
        try:
            return self.mod.main()
        finally:
            sys.argv = argv

    def test_the_bundle_holds_the_source_and_the_accounting(self):
        self.run_main(
            packages=[("busybox", "1.36.1", "GPL-2.0-only"),
                      ("jansson", "2.14", "MIT"),
                      ("forgectrl", "0.1.4", "MIT")],
            archives=["busybox-1.36.1-r0", "forgectrl-0.1.4-r0", "jansson-2.14-r0",
                      "u-boot-2020.01-r0"],
            image_packages=[("u-boot", "2020.01", "GPL-2.0-or-later")])
        path = os.path.join(self.out, "forgefirm-source-v0.0.1.tar.gz")
        self.assertTrue(os.path.isfile(path))
        with tarfile.open(path) as tar:
            names = tar.getnames()
            manifest = json.loads(tar.extractfile(
                "forgefirm-source-v0.0.1/MANIFEST.json").read().decode())
            sums = tar.extractfile(
                "forgefirm-source-v0.0.1/sha256sums.txt").read().decode()
        # The copyleft recipe and the ForgeFIRM component are in, the
        # permissive third party is not.
        self.assertIn("forgefirm-source-v0.0.1/sources/busybox-1.36.1-r0/busybox-1.36.1-r0.tar.gz",
                      names)
        self.assertIn("forgefirm-source-v0.0.1/sources/forgectrl-0.1.4-r0/forgectrl-0.1.4-r0.tar.gz",
                      names)
        self.assertNotIn("forgefirm-source-v0.0.1/sources/jansson-2.14-r0/jansson-2.14-r0.tar.gz",
                         names)
        # The accounting travels with the source.
        for name in ("README.md", "SOURCES.txt", "MANIFEST.json",
                     "licenses/license.manifest", "licenses/image_license.manifest",
                     "metadata/LAYERS.txt", "metadata/meta-forgefirm.tar.gz",
                     "metadata/kas/source-bundle.yml"):
            self.assertIn("forgefirm-source-v0.0.1/" + name, names)
        self.assertEqual(manifest["release"], "v0.0.1")
        self.assertEqual(sorted(r["recipe"] for r in manifest["recipes"]),
                         ["busybox", "forgectrl", "u-boot"])
        self.assertEqual(manifest["recipes_without_source"],
                         [{"recipe": "jansson", "version": "2.14", "license": "MIT"}])
        # Every file of the bundle is in the checksums, and only files that
        # the bundle holds.
        listed = sorted(line.split("  ", 1)[1] for line in sums.splitlines())
        held = sorted(n.split("/", 1)[1] for n in names
                      if n != "forgefirm-source-v0.0.1/sha256sums.txt")
        self.assertEqual(listed, held)

    def test_a_recipe_with_no_source_stops_the_release(self):
        with self.assertRaises(SystemExit) as caught:
            self.run_main(packages=[("busybox", "1.36.1", "GPL-2.0-only"),
                                    ("gnutls", "3.8.4", "LGPL-2.1-or-later")],
                          archives=["busybox-1.36.1-r0"])
        self.assertEqual(caught.exception.code, 1)

    def test_the_source_of_gcc_covers_libgcc(self):
        self.run_main(
            packages=[("libgcc", "13.4.0", "GPL-3.0-with-GCC-exception"),
                      ("glibc", "2.39+git", "GPL-2.0-only & LGPL-2.1-or-later"),
                      ("glibc-locale", "2.39+git", "GPL-2.0-only & LGPL-2.1-or-later")],
            archives=["gcc-source-13.4.0-13.4.0-r0", "glibc-2.39+git-r0"])
        with tarfile.open(os.path.join(self.out, "forgefirm-source-v0.0.1.tar.gz")) as tar:
            manifest = json.loads(tar.extractfile(
                "forgefirm-source-v0.0.1/MANIFEST.json").read().decode())
        holders = {r["recipe"]: r["holder"] for r in manifest["recipes"]}
        self.assertEqual(holders["libgcc"], "gcc-source-13.4.0")
        self.assertEqual(holders["glibc-locale"], "glibc")


if __name__ == "__main__":
    unittest.main()
