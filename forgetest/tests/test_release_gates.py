# Copyright 2026 514 LLC d/b/a OpenGlow
# Written by Scott Wiederhold
# https://community.openglow.org
# SPDX-License-Identifier:    MIT

"""scripts/release.sh - the gates a release passes before anything is built
or signed, and what a release that passes them stages and publishes.

Each case runs the script itself, copied into a scratch checkout laid out as
forgefirm's: the release key the image trusts, the key the installer embeds,
the version file, a clean git checkout with its upstream and its tag, a
documentation checkout, and a deploy directory whose links name two small
ext4 root filesystems carrying what the gates read. The keys are made for
the run. The build host's tools a test has no use for are stubs that record
what they were asked: kas (the build), fwup, the packer (mkfw.sh), the
factory-era fwup, the acceptance gate, the source bundle and gh."""
import base64
import json
import os
import shutil
import subprocess
import tempfile
import unittest

REPO = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
SCRIPT = os.path.join(REPO, "scripts", "release.sh")
VERSION = "0.0.42"
# Each image is stamped when it is itself made, so the two stamps differ.
REL_NAME = "forgefirm-image-glowforge.rootfs-20260101000000"
DEV_NAME = "forgefirm-image-dev-glowforge.rootfs-20260101000300"
OTHER_DEV_NAME = "forgefirm-image-dev-glowforge.rootfs-20251201000000"
SEARCH = os.pathsep.join([os.environ.get("PATH", ""), "/usr/sbin", "/sbin"])
TOOLS = ("bash", "git", "debugfs", "mkfs.ext4", "base64", "sha256sum", "python3")

KAS = r"""#!/bin/sh
# A build records its command line; kas dump prints a config with no debug-tweaks.
if [ "$1" = shell ]; then printf '%s\n' "$*" >> "$STUB_LOGS/kas.log"; else echo 'header: {version: 14}'; fi
"""
GH = r"""#!/bin/sh
printf '%s\n' "$@" > "$STUB_LOGS/gh.log"
"""
# fwup -V -i <archive> -p <raw key>: verifies when the key file holds the
# release key's 32 bytes.
FWUP_COMPAT = r"""#!/bin/sh
printf '%s\n' "$*" >> "$STUB_LOGS/fwup-compat.log"
[ -z "$FWUP_COMPAT_FAIL" ] || exit 1
while [ $# -gt 1 ]; do
    if [ "$1" = -p ]; then cmp -s "$2" "$RELEASE_RAW_KEY"; exit; fi
    shift
done
exit 1
"""
# xxd -p, for a host that has no xxd (release.sh reads key bytes with it).
XXD = r"""#!/usr/bin/env python3
import binascii, sys
h = binascii.hexlify(sys.stdin.buffer.read()).decode()
print("\n".join(h[i:i + 60] for i in range(0, len(h), 60)))
"""
MKFW = r"""#!/bin/sh
# mkfw.sh <rootfs> <version> <out> <key>: records what it was asked to sign.
printf '%s|%s|%s|%s\n' "$1" "$2" "$3" "$4" >> "$STUB_LOGS/mkfw.log"
printf 'archive of %s, signed with %s\n' "$1" "$4" > "$3"
"""
SOURCE_BUNDLE = r"""import json, os, sys
with open(os.path.join(os.environ["STUB_LOGS"], "source-bundle.log"), "w") as f:
    json.dump(sys.argv[1:], f)
out = sys.argv[sys.argv.index("--out") + 1]
open(os.path.join(out, "forgefirm-source-v%s.tar.gz" % sys.argv[1]), "w").close()
"""


def write(path, text, mode=0o644):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "wb" if isinstance(text, bytes) else "w") as f:
        f.write(text)
    os.chmod(path, mode)


def git(cwd, *args):
    subprocess.run(["git", "-C", cwd] + list(args), check=True, capture_output=True, env=GIT_ENV)


GIT_ENV = {"PATH": SEARCH, "HOME": tempfile.gettempdir(), "GIT_CONFIG_GLOBAL": os.devnull,
           "GIT_CONFIG_NOSYSTEM": "1", "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@example.org",
           "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@example.org", "LANG": "C.UTF-8"}


class Case:
    """One release run's scratch place: a copy of the checkout and of the
    documentation, the deploy links, the staging directory and the stubs'
    logs."""

    def __init__(self, fx, root):
        self.fx, self.root = fx, root
        self.repo = os.path.join(root, "forgefirm")
        self.docs = os.path.join(root, "forgefirm-docs")
        self.staging = os.path.join(root, "staging")
        self.logs = os.path.join(root, "logs")
        shutil.copytree(fx.template, self.repo, symlinks=True)
        shutil.copytree(fx.docs_template, self.docs, symlinks=True)
        os.makedirs(self.logs)
        self.deploy = os.path.join(self.repo, "build", "tmp", "deploy", "images", "glowforge")
        os.makedirs(self.deploy)
        self.link("forgefirm-image-glowforge.rootfs.ext4", REL_NAME + ".ext4")
        self.link("forgefirm-image-glowforge.rootfs.wic.gz", REL_NAME + ".wic.gz")
        self.link("forgefirm-image-dev-glowforge.rootfs.ext4", DEV_NAME + ".ext4")

    def link(self, name, target):
        path = os.path.join(self.deploy, name)
        if os.path.lexists(path):
            os.remove(path)
        os.symlink(target if os.path.isabs(target) else os.path.join(self.fx.images, target), path)

    def commit(self, message="change"):
        git(self.repo, "add", "-A")
        git(self.repo, "commit", "-q", "--allow-empty", "-m", message)

    def run(self, *args, **env):
        e = dict(self.fx.env, RELEASE_STAGING_DIR=self.staging, FORGEFIRM_DOCS_DIR=self.docs, STUB_LOGS=self.logs)
        e.update(env)
        e = {k: v for k, v in e.items() if v is not None}
        return subprocess.run(["bash", os.path.join(self.repo, "scripts", "release.sh")] + list(args),
                              env=e, capture_output=True, text=True, timeout=60)

    def log(self, name):
        try:
            with open(os.path.join(self.logs, name), encoding="utf-8") as f:
                return f.read()
        except FileNotFoundError:
            return None

    def staged(self, name=""):
        return os.path.join(self.staging, "v" + VERSION, name)


@unittest.skipUnless(all(shutil.which(t, path=SEARCH) for t in TOOLS) and os.path.isfile(SCRIPT),
                     "needs Linux, git, e2fsprogs and a forgefirm checkout")
class ReleaseGateTests(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.mkdtemp(prefix="forgetest-release.")
        try:
            cls.build_fixture()
        except BaseException:
            shutil.rmtree(cls.tmp, ignore_errors=True)
            raise

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.tmp, ignore_errors=True)

    @classmethod
    def key(cls, name, raw, line_end="\n"):
        """A throwaway key pair: <name>.priv (the packer stub never reads it)
        and <name>.pub, the raw key in base64. Returns the private key's path."""
        write(os.path.join(cls.keys, name + ".priv"), "private key %s\n" % name)
        write(os.path.join(cls.keys, name + ".pub"), base64.b64encode(raw).decode() + line_end)
        return os.path.join(cls.keys, name + ".priv")

    @classmethod
    def rootfs(cls, name, manifest, shadow):
        src = os.path.join(cls.tmp, "src-" + name)
        write(os.path.join(src, "etc", "forgefirm-manifest.json"), json.dumps(manifest))
        write(os.path.join(src, "etc", "forgefirm-version"), "v%s\n" % VERSION)
        write(os.path.join(src, "etc", "shadow"), shadow)
        write(os.path.join(src, "etc", "fstab"), "/dev/root  /  auto  ro  1  0\n")
        write(os.path.join(src, "etc", "ssh", "sshd_config"), "".join(
            "HostKey /data/forgefirm/ssh/ssh_host_%s_key\n" % k for k in ("rsa", "ecdsa", "ed25519")))
        p = subprocess.run([shutil.which("mkfs.ext4", path=SEARCH), "-q", "-F", "-d", src,
                            os.path.join(cls.images, name + ".ext4"), "4M"], capture_output=True, text=True)
        if p.returncode:
            raise AssertionError(p.stderr)
        write(os.path.join(cls.images, name + ".wic.gz"), "card image of %s\n" % name)

    @classmethod
    def build_fixture(cls):
        cls.keys = os.path.join(cls.tmp, "keys")
        cls.images = os.path.join(cls.tmp, "images")
        cls.bin = os.path.join(cls.tmp, "bin")
        os.makedirs(cls.images)
        # The release key's public file ends its line with CRLF: the gates
        # compare key bytes, never text.
        cls.release_raw = os.urandom(32)
        cls.release_key = cls.key("release", cls.release_raw, "\r\n")
        write(os.path.join(cls.keys, "release.raw"), cls.release_raw)
        cls.dev_key = cls.key("dev", os.urandom(32))

        # Root first in a shadow file larger than a pipe holds.
        cls.shadow = "root::19000:0:99999:7:::\n" + "".join(
            "user%05d:*:19000:0:99999:7:::\n" % i for i in range(8000))
        comps = {"forgectrl": {"srcrev": "aaa", "files": [["src/main.c", "1" * 40]]}}
        plat = {"machine": "glowforge"}
        cls.rootfs(REL_NAME, {"format": 1, "components": comps, "platform": plat}, cls.shadow)
        # The dev image carries forgetest besides: one build all the same.
        dev = dict(comps, forgetest={"srcrev": None, "files": [["forgetest/x.py", "2" * 40]]})
        cls.rootfs(DEV_NAME, {"format": 1, "components": dev, "platform": plat}, cls.shadow)
        other = dict(dev, forgectrl={"srcrev": "bbb", "files": [["src/main.c", "3" * 40]]})
        cls.rootfs(OTHER_DEV_NAME, {"format": 1, "components": other, "platform": plat}, cls.shadow)

        for name, text in (("kas", KAS), ("gh", GH), ("fwup-compat", FWUP_COMPAT)):
            write(os.path.join(cls.bin, name), text, 0o755)
        if not shutil.which("xxd", path=SEARCH):
            write(os.path.join(cls.bin, "xxd"), XXD, 0o755)

        # The checkout: committed, pushed to its upstream, tagged v<version>.
        t = cls.template = os.path.join(cls.tmp, "template")
        os.makedirs(os.path.join(t, "scripts"))
        shutil.copy(SCRIPT, os.path.join(t, "scripts", "release.sh"))
        write(os.path.join(t, "scripts", "mkfw.sh"), MKFW, 0o755)
        write(os.path.join(t, "scripts", "source-bundle.py"), SOURCE_BUNDLE)
        write(os.path.join(t, "scripts", "acceptance-gate.py"), "import sys\nsys.exit(0)\n")
        cls.set_installer_key(t, cls.release_raw)
        write(os.path.join(t, "meta-forgefirm", "recipes-forgefirm", "images", "forgefirm-release.inc"),
              'FORGEFIRM_RELEASE ?= "%s"\n' % VERSION)
        cls.set_trusted_key(t, cls.release_raw)
        write(os.path.join(t, "README.md"), "> **ForgeFIRM is in beta.** Every release below 0.1.0 is a beta.\n")
        write(os.path.join(t, "releases", "v" + VERSION, "acceptance.json"), "{}\n")
        write(os.path.join(t, ".gitignore"), "/build/\n")
        os.symlink(os.path.join(REPO, "forgetest"), os.path.join(t, "forgetest"))
        git(t, "init", "-q", "-b", "master")
        git(t, "add", "-A")
        git(t, "commit", "-q", "-m", "release")
        upstream = os.path.join(cls.tmp, "upstream.git")
        git(cls.tmp, "init", "-q", "--bare", upstream)
        git(t, "remote", "add", "origin", upstream)
        git(t, "push", "-q", "-u", "origin", "master")
        git(t, "tag", "v" + VERSION)

        d = cls.docs_template = os.path.join(cls.tmp, "docs-template")
        write(os.path.join(d, "index.md"), "docs\n")
        git(d, "init", "-q", "-b", "main")
        git(d, "add", "-A")
        git(d, "commit", "-q", "-m", "docs")

        cls.env = dict(GIT_ENV, PATH=cls.bin + os.pathsep + SEARCH, FWUP="true",
                       FWUP_COMPAT=os.path.join(cls.bin, "fwup-compat"),
                       RELEASE_RAW_KEY=os.path.join(cls.keys, "release.raw"),
                       FORGEFIRM_SIGNING_KEY=cls.release_key, FORGEFIRM_DEV_KEY=cls.dev_key)
        cls.passed = cls.case_at("passed")
        cls.passed_run = cls.passed.run(VERSION, "--publish")

    @staticmethod
    def set_trusted_key(repo, raw):
        write(os.path.join(repo, "meta-forgefirm", "recipes-forgefirm", "forgefirm-keys", "files",
                           "forgefirm-release.pub"), base64.b64encode(raw).decode() + "\n")

    @staticmethod
    def set_installer_key(repo, raw):
        write(os.path.join(repo, "scripts", "install-forgefirm.sh"),
              "#!/bin/sh\nPUBKEY='%s'\n" % "".join("\\x%02x" % b for b in raw), 0o755)

    @classmethod
    def case_at(cls, name):
        return Case(cls, os.path.join(cls.tmp, name))

    def case(self):
        return self.case_at(self.id().rsplit(".", 1)[1])

    def assertStoppedBeforeTheBuild(self, case, p):
        self.assertEqual(p.returncode, 1, p.stdout + p.stderr)
        self.assertIsNone(case.log("kas.log"), "the build ran:\n" + p.stderr)
        self.assertFalse(os.path.exists(case.staging), "something was staged")

    # ---------------------------------------------------------------- keys

    def test_the_signing_key_must_be_the_key_the_image_trusts(self):
        c = self.case()
        raw = os.urandom(32)
        # The installer embeds the signing key, so only the trust gate stops it.
        self.set_installer_key(c.repo, raw)
        c.commit()
        self.assertStoppedBeforeTheBuild(c, c.run(VERSION, FORGEFIRM_SIGNING_KEY=self.key("untrusted", raw)))

    def test_a_key_file_that_holds_no_32_byte_key_stops_the_release(self):
        # The image trusts it and the installer embeds it, so only its length
        # can stop the release.
        c = self.case()
        raw = os.urandom(16)
        self.set_trusted_key(c.repo, raw)
        self.set_installer_key(c.repo, raw)
        c.commit()
        self.assertStoppedBeforeTheBuild(c, c.run(VERSION, FORGEFIRM_SIGNING_KEY=self.key("short", raw)))

    def test_a_dev_key_that_is_the_release_key_is_refused_in_both_modes(self):
        for args in ((VERSION,), ("--dev",)):
            c = self.case_at("dev-is-release-" + args[0])
            self.assertStoppedBeforeTheBuild(c, c.run(*args, FORGEFIRM_DEV_KEY=self.release_key))
            self.assertIsNone(c.log("mkfw.log"), args)

    def test_a_release_needs_the_dev_key_and_its_public_file(self):
        c = self.case_at("no-dev-key")
        p = c.run(VERSION, FORGEFIRM_DEV_KEY=None)
        self.assertNotEqual(p.returncode, 0)
        self.assertIsNone(c.log("kas.log"), "the build ran:\n" + p.stderr)
        write(os.path.join(self.keys, "nopub.priv"), "private key with no public half\n")
        c = self.case_at("no-dev-pub")
        self.assertStoppedBeforeTheBuild(c, c.run(VERSION, FORGEFIRM_DEV_KEY=os.path.join(self.keys, "nopub.priv")))

    # ---------------------------------------------------------- repo state

    def test_publish_needs_the_pushed_commit_of_a_checkout(self):
        # A commit never pushed, even one that changes no file, re-tagged at HEAD.
        c = self.case_at("ahead")
        c.commit("not pushed")
        git(c.repo, "tag", "-f", "v" + VERSION)
        self.assertStoppedBeforeTheBuild(c, c.run(VERSION, "--publish"))
        c = self.case_at("no-checkout")
        shutil.rmtree(os.path.join(c.repo, ".git"))
        self.assertStoppedBeforeTheBuild(c, c.run(VERSION, "--publish"))

    def test_publish_needs_the_release_tag_on_head_and_has_gh_verify_it(self):
        c = self.case_at("no-tag")
        git(c.repo, "tag", "-d", "v" + VERSION)
        self.assertStoppedBeforeTheBuild(c, c.run(VERSION, "--publish"))
        c = self.case_at("tag-elsewhere")
        c.commit("pushed after the tag")
        git(c.repo, "update-ref", "refs/remotes/origin/master", "HEAD")
        self.assertStoppedBeforeTheBuild(c, c.run(VERSION, "--publish"))
        # The release that passed: gh attaches it to that tag and makes none.
        gh = self.passed.log("gh.log").splitlines()
        self.assertEqual(gh[:3], ["release", "create", "v" + VERSION], gh)
        self.assertIn("--verify-tag", gh)

    # -------------------------------------------------------------- staging

    def test_a_release_stopped_after_the_signatures_leaves_nothing_where_the_publish_looks(self):
        c = self.case()
        for d in ("v%s" % VERSION, "v%s.tmp" % VERSION):
            write(os.path.join(c.staging, d, "forgefirm.fw"), "an earlier run's archive\n")
        p = c.run(VERSION, FWUP_COMPAT_FAIL="1")
        self.assertEqual(p.returncode, 1, p.stdout + p.stderr)
        self.assertIsNotNone(c.log("mkfw.log"), "stopped before the signatures:\n" + p.stderr)
        self.assertFalse(os.path.exists(c.staged()), "a stopped release left v%s" % VERSION)
        with open(os.path.join(c.staging, "v%s.tmp" % VERSION, "forgefirm.fw")) as f:
            self.assertNotIn("earlier run", f.read())

    def test_an_existing_docs_tag_must_name_the_docs_head(self):
        c = self.case()
        git(c.docs, "tag", "-a", "v" + VERSION, "-m", "an earlier run")
        write(os.path.join(c.docs, "index.md"), "docs, later\n")
        git(c.docs, "commit", "-q", "-am", "later")
        p = c.run(VERSION)
        self.assertEqual(p.returncode, 1, p.stdout + p.stderr)
        self.assertFalse(os.path.exists(c.staged()))

    # --------------------------------------------------------- the rootfs

    def test_the_dev_rootfs_is_the_one_its_own_link_names(self):
        self.assertEqual(self.passed_run.returncode, 0, self.passed_run.stdout + self.passed_run.stderr)
        signed = [ln.split("|") for ln in self.passed.log("mkfw.log").splitlines()]
        self.assertEqual([s[0] for s in signed], [os.path.realpath(os.path.join(self.images, n + ".ext4"))
                                                  for n in (REL_NAME, DEV_NAME)])
        with open(self.passed.staged("forgefirm-image-dev-glowforge.rootfs.wic.gz")) as f:
            self.assertEqual(f.read(), "card image of %s\n" % DEV_NAME)

    def test_an_oversize_dev_rootfs_is_refused_in_both_modes(self):
        for args in ((VERSION,), ("--dev",)):
            c = self.case_at("oversize-" + args[0])
            big = os.path.join(c.root, DEV_NAME + ".ext4")
            shutil.copy(os.path.join(self.images, DEV_NAME + ".ext4"), big)
            os.truncate(big, 196 * 1024 * 1024)
            shutil.copy(os.path.join(self.images, DEV_NAME + ".wic.gz"), c.root)
            c.link("forgefirm-image-dev-glowforge.rootfs.ext4", big)
            p = c.run(*args)
            self.assertEqual(p.returncode, 1, p.stdout + p.stderr)
            self.assertIsNone(c.log("mkfw.log"), "%s: an oversize rootfs was packed" % args[0])

    def test_a_dev_rootfs_of_another_build_stops_the_release_before_the_signatures(self):
        c = self.case()
        c.link("forgefirm-image-dev-glowforge.rootfs.ext4", OTHER_DEV_NAME + ".ext4")
        p = c.run(VERSION)
        self.assertEqual(p.returncode, 1, p.stdout + p.stderr)
        self.assertIsNone(c.log("mkfw.log"), "signed before the identity gate")
        self.assertFalse(os.path.exists(c.staging))

    def test_the_root_password_gate_reads_a_large_shadow_to_its_end(self):
        # Root is the first line of a shadow larger than a pipe holds, so a
        # reader that stops at it would leave debugfs writing.
        self.assertGreater(len(self.shadow), 2 * 65536)
        self.assertEqual(self.passed_run.returncode, 0, self.passed_run.stdout + self.passed_run.stderr)

    # ------------------------------------------------------- the assets

    def test_the_dev_archive_is_signed_with_the_dev_key_and_goes_out_labeled(self):
        stage = os.path.join(self.passed.staging, "v%s.tmp" % VERSION)
        signed = {s[2]: s for s in (ln.split("|") for ln in self.passed.log("mkfw.log").splitlines())}
        self.assertEqual(signed[os.path.join(stage, "forgefirm-dev.fw")][3], self.dev_key)
        self.assertEqual(signed[os.path.join(stage, "forgefirm.fw")][3], self.release_key)
        with open(self.passed.staged("sha256sums.txt")) as f:
            summed = {ln.split()[1] for ln in f}
        dev_assets = ("forgefirm-dev.fw", "forgefirm-image-dev-glowforge.rootfs.wic.gz")
        self.assertTrue(set(dev_assets) <= summed, summed)
        gh = self.passed.log("gh.log").splitlines()
        for name in dev_assets:
            self.assertEqual(len([a for a in gh if a.startswith(name + "#" + name + " (")]), 1, gh)
            self.assertNotIn(name, gh, "%s goes out without its label" % name)

    def test_the_factory_era_check_reads_the_key_without_its_line_ending(self):
        self.assertEqual(self.passed_run.returncode, 0, self.passed_run.stdout + self.passed_run.stderr)
        self.assertIn("-p", self.passed.log("fwup-compat.log").split())

    def test_the_source_bundle_is_asked_for_both_images(self):
        argv = json.loads(self.passed.log("source-bundle.log"))
        names = [argv[i + 1] for i, a in enumerate(argv) if a == "--image-name"]
        self.assertEqual(names, [REL_NAME, DEV_NAME])


if __name__ == "__main__":
    unittest.main()
