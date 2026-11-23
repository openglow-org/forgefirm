# Copyright 2026 514 LLC d/b/a OpenGlow
# Written by Scott Wiederhold
# https://community.openglow.org
# SPDX-License-Identifier:    MIT

"""The release pipeline: scripts/release.sh (the staging run) - the gates a
release passes before anything is built, and what it stages for signing -
scripts/release-sign.sh (the sign step, the only one that reads a private
key), scripts/mkfw.sh (the packer and its self-check), and
scripts/host-test-gate.py (the host tests behind the release).

Each pipeline case runs the scripts themselves, copied into a scratch
checkout laid out as forgefirm's: the release key the image trusts, the key
the installer embeds, the allowed signers file with the sums key, the
version file, a clean git checkout with its upstream and its tag, a
documentation checkout on its next branch with an origin whose main next
can fast-forward, and a deploy directory whose links name two small ext4
root filesystems carrying what the gates read. The keys are made for the
run. The build host's tools a case has no use for are stubs that record
what they were asked: kas (the build), fwup, the packer (mkfw.sh), the
factory-era fwup, the release-branch check, the acceptance gate, the
host-test gate and the source bundle."""
import base64
import importlib.util
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest

import helpers

REPO = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
SCRIPT = os.path.join(REPO, "scripts", "release.sh")
SIGN = os.path.join(REPO, "scripts", "release-sign.sh")
MKFW = os.path.join(REPO, "scripts", "mkfw.sh")
HOST_GATE = os.path.join(REPO, "scripts", "host-test-gate.py")
VERSION = "0.0.42"
# Each image is stamped when it is itself made, so the two stamps differ.
REL_NAME = "forgefirm-image-glowforge.rootfs-20260101000000"
DEV_NAME = "forgefirm-image-dev-glowforge.rootfs-20260101000300"
OTHER_DEV_NAME = "forgefirm-image-dev-glowforge.rootfs-20251201000000"
DIRTY_REL_NAME = "forgefirm-image-glowforge.rootfs-20260102000000"
DIRTY_DEV_NAME = "forgefirm-image-dev-glowforge.rootfs-20260102000300"
PW_REL_NAME = "forgefirm-image-glowforge.rootfs-20260103000000"
RW_REL_NAME = "forgefirm-image-glowforge.rootfs-20260104000000"
BARE_REL_NAME = "forgefirm-image-glowforge.rootfs-20260105000000"
SEARCH = os.pathsep.join([os.environ.get("PATH", ""), "/usr/sbin", "/sbin"])
TOOLS = ("bash", "git", "debugfs", "mkfs.ext4", "base64", "sha256sum", "python3", "ssh-keygen")
STAGED_NAMES = ("forgefirm-image-glowforge.rootfs.ext4", "forgefirm-image-dev-glowforge.rootfs.ext4")

KAS = r"""#!/bin/sh
# A build records its command line, and runs what KAS_DURING_BUILD says
# someone did while it ran; kas dump prints a config with no debug-tweaks,
# and as JSON the repositories of the file KAS_DUMP names.
case "$1 $2" in
  shell*) printf '%s\n' "$*" >> "$STUB_LOGS/kas.log"
          [ -z "$KAS_DURING_BUILD" ] || sh -c "$KAS_DURING_BUILD" ;;
  "dump --format") cat "$KAS_DUMP" ;;
  *) echo 'header: {version: 14}' ;;
esac
"""
# The resolved configuration as kas dump --format json prints it: this
# repository (no url, no path), an upstream layer at its url, and the
# commit the lock file names for it.
KAS_DUMP = {"header": {"version": 14},
            "repos": {"forgefirm": {"layers": {"meta-forgefirm": None}},
                      "poky": {"url": "https://git.yoctoproject.org/poky.git", "branch": "scarthgap",
                               "path": "layers/poky"}},
            "overrides": {"repos": {"poky": {"commit": "7" * 40}}}}
# The build section of a manifest built from the pins as they are committed.
CLEAN_BUILD = {"layers": {"meta-forgefirm": {"rev": "8" * 40, "dirty": False}}, "kas": {"dirty": False}}
# fwup -V -i <archive> -p <raw key>: verifies when the key file holds the
# 32 bytes of the key RELEASE_RAW_KEY names.
FWUP_COMPAT = r"""#!/bin/sh
printf '%s\n' "$*" >> "$STUB_LOGS/fwup-compat.log"
[ -z "$FWUP_COMPAT_FAIL" ] || exit 1
while [ $# -gt 1 ]; do
    if [ "$1" = -p ]; then cmp -s "$2" "$RELEASE_RAW_KEY"; exit; fi
    shift
done
exit 1
"""
# xxd -p, for a host that has no xxd (the scripts read key bytes with it).
XXD = r"""#!/usr/bin/env python3
import binascii, sys
h = binascii.hexlify(sys.stdin.buffer.read()).decode()
print("\n".join(h[i:i + 60] for i in range(0, len(h), 60)))
"""
MKFW_STUB = r"""#!/bin/sh
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
# sshd's configuration as the release image carries it, twice (the
# read-only rootfs runs sshd_config_readonly): root and empty passwords
# refused, the host keys on /data.
SSHD = "PermitRootLogin no\nPermitEmptyPasswords no\n" + "".join(
    "HostKey /data/forgefirm/ssh/ssh_host_%s_key\n" % k for k in ("rsa", "ecdsa", "ed25519"))
# The Python library of the release image, and the dev image's: the same,
# plus what the real dev image carries beyond it, under the names it
# carries them (a package, a module file, and extension modules with a
# leading underscore, a _test suffix and the ABI tag), which forgetest
# declares (FORGETEST_BB), and forgetest itself.
LIB = "usr/lib/python3.12/"
DYN = LIB + "lib-dynload/%s.cpython-312-arm-linux-gnueabihf.so"
PYLIB = {LIB + "json/__init__.py": "", DYN % "_json": "", LIB + "site-packages/README.txt": ""}
DEV_PYLIB = dict(PYLIB, **{DYN % "mmap": "", LIB + "ctypes/__init__.py": "", DYN % "_ctypes": "",
                           DYN % "_ctypes_test": "", LIB + "statistics.py": "", DYN % "_statistics": "",
                           LIB + "site-packages/forgetest/__init__.py": ""})
FORGETEST_BB = ('RDEPENDS:${PN} = " \\\n    python3-core \\\n    python3-ctypes \\\n    python3-mmap \\\n'
                '    python3-statistics \\\n    forgectrl \\\n"\n')
# A gate stub: records its arguments, keeps a copy of each file it is
# handed as <log>.<argument index> (the last call's), runs what
# <variable>_DURING says someone did while it ran, and exits with the
# status the environment variable it is given names (0 when unset).
GATE_STUB = r"""import json, os, shutil, subprocess, sys
log = os.path.join(os.environ["STUB_LOGS"], "%(log)s")
with open(log + ".log", "a") as f:
    f.write(json.dumps(sys.argv[1:]) + "\n")
for i, a in enumerate(sys.argv[1:]):
    if os.path.isfile(a):
        shutil.copy(a, "%%s.%%d" %% (log, i))
if os.environ.get("%(rc)s_DURING"):
    subprocess.run(["sh", "-c", os.environ["%(rc)s_DURING"]], check=True)
sys.exit(int(os.environ.get("%(rc)s") or 0))
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
    """One release's scratch place: a copy of the checkout and of the
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
        # A card image link an earlier build left: the release takes the
        # card made with its own rootfs, by name.
        self.link("forgefirm-image-glowforge.rootfs.wic.gz", OTHER_DEV_NAME + ".wic.gz")
        self.link("forgefirm-image-dev-glowforge.rootfs.ext4", DEV_NAME + ".ext4")

    def link(self, name, target):
        path = os.path.join(self.deploy, name)
        if os.path.lexists(path):
            os.remove(path)
        os.symlink(target if os.path.isabs(target) else os.path.join(self.fx.images, target), path)

    def commit(self, message="change"):
        git(self.repo, "add", "-A")
        git(self.repo, "commit", "-q", "--allow-empty", "-m", message)

    def env(self, env):
        e = dict(self.fx.env, RELEASE_STAGING_DIR=self.staging, FORGEFIRM_DOCS_DIR=self.docs, STUB_LOGS=self.logs)
        e.update(env)
        return {k: v for k, v in e.items() if v is not None}

    def run(self, *args, **env):
        """The staging run, release.sh."""
        return subprocess.run(["bash", os.path.join(self.repo, "scripts", "release.sh")] + list(args),
                              env=self.env(env), capture_output=True, text=True, timeout=60)

    def sign(self, *args, checkout=None, **env):
        """The sign step, release-sign.sh, from this case's checkout or from
        another one."""
        return subprocess.run(["bash", os.path.join(checkout or self.repo, "scripts", "release-sign.sh")]
                              + list(args), env=self.env(env), capture_output=True, text=True, timeout=60)

    def log(self, name):
        try:
            with open(os.path.join(self.logs, name), encoding="utf-8") as f:
                return f.read()
        except FileNotFoundError:
            return None

    def staged(self, name=""):
        return os.path.join(self.staging, "v" + VERSION, name)

    def stage(self, name=""):
        """Where the staging run stages, before the sign step."""
        return os.path.join(self.staging, "v%s.tmp" % VERSION, name)

    def request(self):
        """The sign request the staging run wrote, as a dict."""
        with open(self.stage("SIGN-REQUEST"), encoding="utf-8") as f:
            return dict(ln.rstrip("\n").split("=", 1) for ln in f if "=" in ln)

    def edit_request(self, changes):
        """The sign request with the lines `changes` sets, as anyone who can
        write the stage could make it."""
        req = self.request()
        req.update(changes)
        with open(self.stage("SIGN-REQUEST"), "w", encoding="utf-8") as f:
            f.write("".join("%s=%s\n" % kv for kv in req.items()))

    def pushed(self):
        """HEAD as its upstream: what a push leaves."""
        git(self.repo, "update-ref", "refs/remotes/origin/master", "HEAD")


@unittest.skipUnless(all(shutil.which(t, path=SEARCH) for t in TOOLS) and os.path.isfile(SCRIPT),
                     "needs Linux, git, e2fsprogs, ssh-keygen and a forgefirm checkout")
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
        write(os.path.join(cls.keys, name + ".raw"), raw)
        return os.path.join(cls.keys, name + ".priv")

    @classmethod
    def ssh_key(cls, name):
        """A throwaway OpenSSH Ed25519 key; returns the private key's path."""
        path = os.path.join(cls.keys, name)
        subprocess.run([shutil.which("ssh-keygen", path=SEARCH), "-q", "-t", "ed25519", "-N", "", "-C", name,
                        "-f", path], check=True, capture_output=True)
        return path

    @classmethod
    def rootfs(cls, name, manifest, shadow, files=PYLIB, images=None, fstab="/dev/root  /  auto  ro  1  0\n"):
        """A root filesystem (<images>/<name>.ext4, cls.images by default)
        carrying what the gates read (no manifest when it is None), and
        files {path: text}."""
        images = images or cls.images
        src = os.path.join(images, "src-" + name)
        if manifest is not None:
            write(os.path.join(src, "etc", "forgefirm-manifest.json"), json.dumps(manifest))
        write(os.path.join(src, "etc", "forgefirm-version"), "v%s\n" % VERSION)
        write(os.path.join(src, "etc", "shadow"), shadow)
        write(os.path.join(src, "etc", "fstab"), fstab)
        for c in ("sshd_config", "sshd_config_readonly"):
            write(os.path.join(src, "etc", "ssh", c), SSHD)
        for rel, text in files.items():
            write(os.path.join(src, rel), text)
        p = subprocess.run([shutil.which("mkfs.ext4", path=SEARCH), "-q", "-F", "-d", src,
                            os.path.join(images, name + ".ext4"), "4M"], capture_output=True, text=True)
        if p.returncode:
            raise AssertionError(p.stderr)
        write(os.path.join(images, name + ".wic.gz"), "card image of %s\n" % name)
        return os.path.join(images, name + ".ext4")

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
        cls.dev_key = cls.key("dev", os.urandom(32))
        cls.sums_key = cls.ssh_key("sums")
        cls.stranger_sums_key = cls.ssh_key("stranger")
        cls.bench_key = cls.ssh_key("bench")

        # Root first in a shadow file larger than a pipe holds.
        cls.shadow = "root::19000:0:99999:7:::\n" + "".join(
            "user%05d:*:19000:0:99999:7:::\n" % i for i in range(8000))
        comps = {"forgectrl": {"srcrev": "aaa", "files": [["src/main.c", "1" * 40]]}}
        plat = {"machine": "glowforge"}
        rel = {"format": 1, "components": comps, "platform": plat, "build": CLEAN_BUILD}
        cls.rootfs(REL_NAME, rel, cls.shadow)
        # The dev image carries forgetest besides: one build all the same.
        dev = dict(comps, forgetest={"srcrev": None, "files": [["forgetest/x.py", "2" * 40]]})
        cls.rel_manifest = {"format": 1, "components": comps, "platform": plat, "build": CLEAN_BUILD}
        cls.dev_manifest = {"format": 1, "components": dev, "platform": plat, "build": CLEAN_BUILD}
        cls.rootfs(DEV_NAME, cls.dev_manifest, cls.shadow, DEV_PYLIB)
        other = dict(dev, forgectrl={"srcrev": "bbb", "files": [["src/main.c", "3" * 40]]})
        cls.rootfs(OTHER_DEV_NAME, {"format": 1, "components": other, "platform": plat, "build": CLEAN_BUILD},
                   cls.shadow, DEV_PYLIB)
        # One build of a layer that differs from its commit: the same identity
        # as the clean build, a dirty layer in the build section.
        dirty = {"layers": {"meta-forgefirm": {"rev": "8" * 40, "dirty": True}}, "kas": {"dirty": False}}
        cls.rootfs(DIRTY_REL_NAME, {"format": 1, "components": comps, "platform": plat, "build": dirty},
                   cls.shadow)
        cls.rootfs(DIRTY_DEV_NAME, {"format": 1, "components": dev, "platform": plat, "build": dirty},
                   cls.shadow, DEV_PYLIB)
        # The release build of the clean one with a root password, with a
        # read-write root, and with no manifest at all.
        cls.rootfs(PW_REL_NAME, rel, "root:$6$salt$hash:19000:0:99999:7:::\n")
        cls.rootfs(RW_REL_NAME, rel, cls.shadow, fstab="/dev/root  /  auto  rw  1  0\n")
        cls.rootfs(BARE_REL_NAME, None, cls.shadow)
        cls.kas_dump = os.path.join(cls.tmp, "kas-dump.json")
        write(cls.kas_dump, json.dumps(KAS_DUMP))

        for name, text in (("kas", KAS), ("fwup-compat", FWUP_COMPAT)):
            write(os.path.join(cls.bin, name), text, 0o755)
        if not shutil.which("xxd", path=SEARCH):
            write(os.path.join(cls.bin, "xxd"), XXD, 0o755)

        # The checkout: committed, pushed to its upstream, tagged v<version>.
        t = cls.template = os.path.join(cls.tmp, "template")
        os.makedirs(os.path.join(t, "scripts"))
        shutil.copy(SCRIPT, os.path.join(t, "scripts", "release.sh"))
        shutil.copy(SIGN, os.path.join(t, "scripts", "release-sign.sh"))
        write(os.path.join(t, "scripts", "mkfw.sh"), MKFW_STUB, 0o755)
        write(os.path.join(t, "scripts", "source-bundle.py"), SOURCE_BUNDLE)
        for script, log, rc in (("acceptance-gate.py", "acceptance-gate", "ACCEPTANCE_GATE_RC"),
                                ("release-branch-check.py", "branch-check", "BRANCH_CHECK_RC"),
                                ("host-test-gate.py", "host-test-gate", "HOST_TEST_GATE_RC")):
            write(os.path.join(t, "scripts", script), GATE_STUB % {"log": log, "rc": rc})
        cls.set_installer_key(t, cls.release_raw)
        write(os.path.join(t, "meta-forgefirm", "recipes-forgefirm", "images", "forgefirm-release.inc"),
              'FORGEFIRM_RELEASE ?= "%s"\n' % VERSION)
        write(os.path.join(t, "meta-forgefirm", "recipes-forgefirm", "forgetest", "forgetest.bb"), FORGETEST_BB)
        cls.set_trusted_key(t, cls.release_raw)
        write(os.path.join(t, "README.md"), "> **ForgeFIRM is in beta.** Every release below 0.1.0 is a beta.\n")
        write(os.path.join(t, "releases", "v" + VERSION, "acceptance.json"), "{}\n")
        def pub(key):
            with open(key + ".pub", encoding="utf-8") as f:
                return " ".join(f.read().split()[:2])
        # the sums key, and a bench key listed for its own principal
        write(os.path.join(t, "releases", "allowed_signers"),
              'forgefirm-sums namespaces="forgefirm-sums" %s\n'
              'forgetest-bench namespaces="forgetest-acceptance" %s\n' % (pub(cls.sums_key), pub(cls.bench_key)))
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

        # The documentation: main on its origin, next one commit ahead of
        # it and checked out.
        d = cls.docs_template = os.path.join(cls.tmp, "docs-template")
        write(os.path.join(d, "index.md"), "docs\n")
        git(d, "init", "-q", "-b", "main")
        git(d, "add", "-A")
        git(d, "commit", "-q", "-m", "docs")
        cls.docs_origin = os.path.join(cls.tmp, "docs-origin.git")
        git(cls.tmp, "init", "-q", "--bare", cls.docs_origin)
        git(d, "remote", "add", "origin", cls.docs_origin)
        git(d, "push", "-q", "origin", "main")
        git(d, "checkout", "-q", "-b", "next")
        write(os.path.join(d, "release.md"), "the release's pages\n")
        git(d, "add", "-A")
        git(d, "commit", "-q", "-m", "next")

        cls.env = dict(GIT_ENV, PATH=cls.bin + os.pathsep + SEARCH, FWUP="true",
                       FWUP_COMPAT=os.path.join(cls.bin, "fwup-compat"),
                       RELEASE_RAW_KEY=os.path.join(cls.keys, "release.raw"),
                       FORGEFIRM_SIGNING_KEY=cls.release_key, FORGEFIRM_DEV_KEY=cls.dev_key,
                       FORGEFIRM_SUMS_KEY=cls.sums_key, KAS_DUMP=cls.kas_dump)
        cls.passed = cls.case_at("passed")
        # an archive an earlier build left in the deploy directory
        cls.stale_archive = os.path.join(cls.passed.repo, "build", "tmp", "deploy", "sources",
                                         "arm-fslc-linux-gnueabi", "busybox-1.35.0-r0", "busybox.tar.gz")
        write(cls.stale_archive, "an earlier build's source\n")
        cls.passed_run = cls.passed.run(VERSION, "--publish")
        cls.passed_request = cls.passed.request() if os.path.isfile(cls.passed.stage("SIGN-REQUEST")) else {}
        cls.passed_sign = cls.passed.sign(VERSION)
        # A release staged and not signed, which the sign step's refusals
        # leave as they found it.
        cls.unsigned = cls.case_at("unsigned")
        cls.unsigned_run = cls.unsigned.run(VERSION)

    @staticmethod
    def set_trusted_key(repo, raw):
        write(os.path.join(repo, "meta-forgefirm", "recipes-forgefirm", "forgefirm-keys", "files",
                           "forgefirm-release.pub"), base64.b64encode(raw).decode() + "\n")

    @staticmethod
    def set_installer_key(repo, raw, tag_line="RELEASE_TAG=''\n"):
        write(os.path.join(repo, "scripts", "install-forgefirm.sh"),
              "#!/bin/sh\n%sPUBKEY='%s'\n" % (tag_line, "".join("\\x%02x" % b for b in raw)), 0o755)

    @classmethod
    def case_at(cls, name):
        return Case(cls, os.path.join(cls.tmp, name))

    def case(self):
        return self.case_at(self.id().rsplit(".", 1)[1])

    def assertStoppedBeforeTheBuild(self, case, p):
        self.assertEqual(p.returncode, 1, p.stdout + p.stderr)
        self.assertIsNone(case.log("kas.log"), "the build ran:\n" + p.stderr)
        self.assertFalse(os.path.exists(case.stage()) or os.path.exists(case.staged()), "something was staged")

    def assertStoppedBeforeTheSignRequest(self, case, p):
        """A gate after the build stopped the staging run: nothing is left
        for the sign step, and nothing where the publish command looks."""
        self.assertEqual(p.returncode, 1, p.stdout + p.stderr)
        self.assertIsNotNone(case.log("kas.log"), "stopped before the build:\n" + p.stderr)
        self.assertFalse(os.path.exists(case.stage()) or os.path.exists(case.staged()), "something was staged")

    def assertSignRefused(self, case, p):
        self.assertEqual(p.returncode, 1, p.stdout + p.stderr)
        self.assertIsNone(case.log("mkfw.log"), "something was signed:\n" + p.stderr)
        self.assertFalse(os.path.exists(case.staged()), "v%s is in place" % VERSION)

    def staged_case(self, *args, **env):
        """A case whose staging run passed, ready for its sign step."""
        c = self.case()
        p = c.run(VERSION, *args, **env)
        self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
        return c

    def test_the_passed_release(self):
        self.assertEqual(self.passed_run.returncode, 0, self.passed_run.stdout + self.passed_run.stderr)
        self.assertEqual(self.passed_sign.returncode, 0, self.passed_sign.stdout + self.passed_sign.stderr)
        self.assertTrue(os.path.isfile(self.passed.staged("forgefirm.fw")))
        self.assertFalse(os.path.exists(self.passed.stage()))
        # the copies the gates judged and the request do not travel
        self.assertFalse(os.path.exists(self.passed.staged("sign")))
        self.assertFalse(os.path.exists(self.passed.staged("SIGN-REQUEST")))
        # the installer goes out as an asset, stamped with the release's tag
        self.assertIn("install-forgefirm.sh", self.passed_request.get("assets", "").split())
        with open(self.passed.staged("install-forgefirm.sh"), encoding="utf-8") as f:
            self.assertIn("RELEASE_TAG='v%s'\n" % VERSION, f.read())
        # the notes print the release key as the installer prints it: hex in groups of eight
        key = self.release_raw.hex()
        with open(self.passed.staged("notes.md"), encoding="utf-8") as f:
            self.assertIn(" ".join(key[i:i + 8] for i in range(0, 64, 8)), f.read())

    def test_an_installer_with_no_tag_line_to_stamp_stops_the_release(self):
        c = self.case()
        self.set_installer_key(c.repo, self.release_raw, tag_line="")
        c.commit()
        p = c.run(VERSION)
        self.assertStoppedBeforeTheBuild(c, p)
        self.assertIn("RELEASE_TAG", p.stdout + p.stderr)    # stopped by this gate, not another

    # ---------------------------------------------------------------- keys

    def unsigned_case(self):
        """The release staged once for the sign step's refusals."""
        self.assertEqual(self.unsigned_run.returncode, 0, self.unsigned_run.stdout + self.unsigned_run.stderr)
        return self.unsigned

    def test_the_signing_key_must_be_the_key_the_image_trusts(self):
        c = self.unsigned_case()
        self.assertSignRefused(c, c.sign(VERSION, FORGEFIRM_SIGNING_KEY=self.key("untrusted", os.urandom(32))))

    def test_a_key_file_that_holds_no_32_byte_key_stops_the_release(self):
        # The image trusts it and the installer embeds it, so only its length
        # can stop the release.
        c = self.case()
        raw = os.urandom(16)
        self.set_trusted_key(c.repo, raw)
        self.set_installer_key(c.repo, raw)
        c.commit()
        p = c.run(VERSION)
        self.assertStoppedBeforeTheBuild(c, p)
        self.assertIn("does not hold a 32-byte public key", p.stdout + p.stderr)   # this gate, not a later one

    def test_the_installer_must_embed_the_key_the_image_trusts(self):
        c = self.case()
        self.set_installer_key(c.repo, os.urandom(32))
        c.commit()
        p = c.run(VERSION)
        self.assertStoppedBeforeTheBuild(c, p)
        self.assertIn("embedded pubkey is not the release key", p.stdout + p.stderr)   # this gate, not a later one

    def test_a_dev_key_that_is_the_release_key_is_refused_in_both_modes(self):
        c = self.case_at("dev-is-release-dev")
        self.assertStoppedBeforeTheBuild(c, c.run("--dev", FORGEFIRM_DEV_KEY=self.release_key))
        self.assertIsNone(c.log("mkfw.log"))
        c = self.unsigned_case()
        self.assertSignRefused(c, c.sign(VERSION, FORGEFIRM_DEV_KEY=self.release_key))

    def test_a_release_needs_the_dev_key_and_its_public_file(self):
        c = self.unsigned_case()
        p = c.sign(VERSION, FORGEFIRM_DEV_KEY=None)
        self.assertNotEqual(p.returncode, 0)
        self.assertIsNone(c.log("mkfw.log"))
        write(os.path.join(self.keys, "nopub.priv"), "private key with no public half\n")
        self.assertSignRefused(c, c.sign(VERSION, FORGEFIRM_DEV_KEY=os.path.join(self.keys, "nopub.priv")))

    def test_the_sums_key_must_be_the_one_the_tree_publishes(self):
        # a key in no line, and a key the file lists for the bench
        c = self.unsigned_case()
        for key in (self.stranger_sums_key, self.bench_key):
            self.assertSignRefused(c, c.sign(VERSION, FORGEFIRM_SUMS_KEY=key))

    # ---------------------------------------------------------- repo state

    def test_publish_needs_the_pushed_commit_of_a_checkout(self):
        # A commit never pushed, even one that changes no file, re-tagged at HEAD.
        c = self.case_at("ahead")
        c.commit("not pushed")
        git(c.repo, "tag", "-f", "v" + VERSION)
        self.assertStoppedBeforeTheBuild(c, c.run(VERSION, "--publish"))
        # a staging run without --publish builds a pushed commit too
        c = self.case_at("ahead-staging")
        c.commit("not pushed")
        self.assertStoppedBeforeTheBuild(c, c.run(VERSION))
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
        gh = [ln for ln in self.passed_sign.stdout.splitlines() if "gh release create" in ln]
        self.assertEqual(len(gh), 1, self.passed_sign.stdout)
        self.assertIn('"v%s"' % VERSION, gh[0])
        self.assertIn("--verify-tag", gh[0])

    def test_a_file_only_a_local_exclude_hides_stops_the_release(self):
        # An externalsrc bbappend kept out of git status by the local exclude
        # file: bitbake reads it, and the layer hash counts it.
        c = self.case()
        write(os.path.join(c.repo, "meta-forgefirm", "recipes-forgefirm", "forgectrl", "forgectrl.bbappend"),
              'inherit externalsrc\nEXTERNALSRC = "/src/forgectrl"\n')
        with open(os.path.join(c.repo, ".git", "info", "exclude"), "a") as f:
            f.write("forgectrl.bbappend\n")
        self.assertStoppedBeforeTheBuild(c, c.run(VERSION))

    def test_a_file_a_local_exclude_hides_where_the_build_reads_nothing_does_not_stop_it(self):
        # A tool's output at the top of the checkout, hidden the same way.
        c = self.case()
        write(os.path.join(c.repo, "graphify-out", "graph.json"), "{}\n")
        with open(os.path.join(c.repo, ".git", "info", "exclude"), "a") as f:
            f.write("graphify-out/\n")
        p = c.run(VERSION)
        self.assertEqual(p.returncode, 0, p.stdout + p.stderr)

    def test_an_edit_the_index_is_told_to_ignore_stops_the_release(self):
        # git status does not report a skip-worktree file's edit: in the
        # layer, and in the allowed signers file the acceptance gate reads.
        for name, path in (("layer", ("meta-forgefirm", "recipes-forgefirm", "images", "forgefirm-release.inc")),
                           ("signers", ("releases", "allowed_signers"))):
            with self.subTest(name):
                c = self.case_at("unseen-" + name)
                git(c.repo, "update-index", "--skip-worktree", "/".join(path))
                with open(os.path.join(c.repo, *path), "a") as f:
                    f.write("# a local edit\n")
                self.assertStoppedBeforeTheBuild(c, c.run(VERSION))

    def test_the_checkout_must_stay_the_commit_built(self):
        # Done while the build ran: a fix committed, pushed and tagged (the
        # gates after the build would read it, and the request name it); the
        # tag alone moved; an edit the index ignores.
        for what, during in (
                ("a fix committed, pushed and tagged",
                 'git -C "$R" commit -q --allow-empty -m fix && git -C "$R" update-ref refs/remotes/origin/master HEAD'
                 ' && git -C "$R" tag -f -a -m fix v%s' % VERSION),
                ("the tag moved",
                 'git -C "$R" tag -f v%s "$(git -C "$R" commit-tree "HEAD^{tree}" -m other)"' % VERSION),
                ("an edit the index ignores",
                 'git -C "$R" update-index --skip-worktree scripts/mkfw.sh && echo "# edit" >> "$R/scripts/mkfw.sh"')):
            with self.subTest(what):
                c = self.case_at("moved-" + what.replace(" ", "-").replace(",", ""))
                p = c.run(VERSION, "--publish", KAS_DURING_BUILD='R="%s"; %s' % (c.repo, during))
                self.assertStoppedBeforeTheSignRequest(c, p)
                self.assertIsNone(c.log("acceptance-gate.log"), "the gates ran on the moved checkout")
        # and a commit while the gates ran, the last of them the host tests
        c = self.case_at("moved-during-the-gates")
        p = c.run(VERSION, HOST_TEST_GATE_RC_DURING='git -C "%s" commit -q --allow-empty -m fix' % c.repo)
        self.assertStoppedBeforeTheSignRequest(c, p)
        self.assertIsNotNone(c.log("host-test-gate.log"))

    def test_a_release_builds_only_from_its_release_branches(self):
        # The passed run asked for every check, at HEAD.
        self.assertEqual([json.loads(ln) for ln in self.passed.log("branch-check.log").splitlines()],
                         [["--rev", "HEAD"]])
        c = self.case()
        self.assertStoppedBeforeTheBuild(c, c.run(VERSION, BRANCH_CHECK_RC="1"))

    # ------------------------------------------------- the build's inputs

    def test_a_build_conf_that_kas_does_not_write_stops_the_release(self):
        c = self.case()
        write(os.path.join(c.repo, "build", "conf", "auto.conf"), 'IMAGE_INSTALL:append = " strace"\n')
        self.assertStoppedBeforeTheBuild(c, c.run(VERSION))

    def test_a_repository_from_a_local_checkout_stops_the_release(self):
        # The commented local-sibling form of meta-openglow: a path, no url.
        c = self.case()
        dump = json.loads(json.dumps(KAS_DUMP))
        dump["repos"]["meta-openglow"] = {"path": "../meta-openglow", "layers": {"meta-glowforge-bsp": None}}
        path = os.path.join(c.root, "kas-dump.json")
        write(path, json.dumps(dump))
        self.assertStoppedBeforeTheBuild(c, c.run(VERSION, KAS_DUMP=path))

    def test_a_repository_with_no_locked_commit_stops_the_release(self):
        c = self.case()
        dump = json.loads(json.dumps(KAS_DUMP))
        dump["repos"]["meta-openglow"] = {"url": "https://github.com/openglow-org/meta-openglow.git",
                                          "branch": "scarthgap", "path": "layers/meta-openglow"}
        path = os.path.join(c.root, "kas-dump.json")
        write(path, json.dumps(dump))
        self.assertStoppedBeforeTheBuild(c, c.run(VERSION, KAS_DUMP=path))

    def test_debug_tweaks_in_the_kas_config_stops_the_release(self):
        # Behind a dump longer than a pipe holds: the whole dump is read.
        c = self.case()
        dump = json.loads(json.dumps(KAS_DUMP))
        dump["local_conf_header"] = {"padding": "#" * 300000, "dev": 'EXTRA_IMAGE_FEATURES += "debug-tweaks"'}
        path = os.path.join(c.root, "kas-dump.json")
        write(path, json.dumps(dump))
        self.assertStoppedBeforeTheBuild(c, c.run(VERSION, KAS_DUMP=path))

    def test_a_release_makes_its_images_again_before_it_builds(self):
        # The images, and the acceptance tool's entry, are made from the
        # checkout as it stands, never taken from an earlier build.
        self.assertEqual(self.passed_run.returncode, 0, self.passed_run.stdout + self.passed_run.stderr)
        build = self.passed.log("kas.log").splitlines()[-1]
        clean, _, make = build.partition("&&")
        self.assertEqual(clean.split("bitbake", 1)[1].split(),
                         ["-c", "cleansstate", "forgefirm-image", "forgefirm-image-dev", "forgetest"])
        self.assertIn("forgefirm-image-dev", make.split())

    def test_a_release_takes_no_archive_an_earlier_build_left(self):
        self.assertEqual(self.passed_run.returncode, 0, self.passed_run.stdout + self.passed_run.stderr)
        self.assertFalse(os.path.exists(self.stale_archive))

    # ------------------------------------------------- skips and versions

    def test_a_skip_takes_the_value_1_and_no_other(self):
        c = self.case()
        self.assertStoppedBeforeTheBuild(c, c.run(VERSION, FORGEFIRM_ACCEPTANCE_SKIP="0"))

    def test_a_version_that_is_not_one_stops_the_release(self):
        # The version file says the same, so only the version's form stops it.
        c = self.case()
        write(os.path.join(c.repo, "meta-forgefirm", "recipes-forgefirm", "images", "forgefirm-release.inc"),
              'FORGEFIRM_RELEASE ?= "0.0.42/../x"\n')
        c.commit()
        self.assertStoppedBeforeTheBuild(c, c.run("0.0.42/../x"))

    def test_the_source_skip_is_refused_with_publish(self):
        c = self.case()
        self.assertStoppedBeforeTheBuild(c, c.run(VERSION, "--publish", FORGEFIRM_SOURCE_SKIP="1"))

    def test_the_acceptance_skip_is_refused_with_publish(self):
        c = self.case()
        self.assertStoppedBeforeTheBuild(c, c.run(VERSION, "--publish", FORGEFIRM_ACCEPTANCE_SKIP="1"))

    def test_a_skipped_gate_is_signed_with_the_dev_key_and_staged_as_a_prerelease(self):
        c = self.staged_case(FORGEFIRM_ACCEPTANCE_SKIP="1")
        self.assertIsNone(c.log("acceptance-gate.log"), "the skipped gate ran")
        self.assertEqual((c.request()["gated"], c.request()["prerelease"]), ("0", "--prerelease"))
        # The dev key signs it though the release key is at hand, and the
        # factory-era check takes the dev key.
        p = c.sign(VERSION, RELEASE_RAW_KEY=os.path.join(self.keys, "dev.raw"))
        self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
        signed = {s[2]: s[3] for s in (ln.split("|") for ln in c.log("mkfw.log").splitlines())}
        self.assertEqual(signed[c.stage("forgefirm.fw")], self.dev_key)
        self.assertTrue(os.path.isfile(c.staged("NO-ACCEPTANCE.txt")))
        self.assertFalse(os.path.exists(c.staged("acceptance.json")))
        # and the release key is not asked for at all
        c = self.case_at("skipped-without-the-release-key")
        p = c.run(VERSION, FORGEFIRM_ACCEPTANCE_SKIP="1")
        self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
        p = c.sign(VERSION, FORGEFIRM_SIGNING_KEY=None, RELEASE_RAW_KEY=os.path.join(self.keys, "dev.raw"))
        self.assertEqual(p.returncode, 0, p.stdout + p.stderr)

    def test_the_factory_era_skip_is_marked_and_staged_as_a_prerelease(self):
        c = self.staged_case()
        p = c.sign(VERSION, FWUP_COMPAT=None, FWUP_COMPAT_SKIP="1")
        self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
        self.assertTrue(os.path.isfile(c.staged("NO-FACTORY-CHECK.txt")))
        with open(c.staged("sha256sums.txt")) as f:
            self.assertIn("NO-FACTORY-CHECK.txt", f.read())

    # ------------------------------------------------------- the gates

    def test_a_refusing_acceptance_gate_stops_the_release_before_the_sign_request(self):
        c = self.case()
        self.assertStoppedBeforeTheSignRequest(c, c.run(VERSION, ACCEPTANCE_GATE_RC="1"))

    def test_a_missing_artifact_stops_the_release(self):
        c = self.case()
        os.remove(os.path.join(c.repo, "releases", "v" + VERSION, "acceptance.json"))
        c.commit("no artifact")
        c.pushed()
        self.assertStoppedBeforeTheSignRequest(c, c.run(VERSION))
        self.assertIsNone(c.log("acceptance-gate.log"), "the gate was asked about an artifact that is not there")

    def test_a_rootfs_with_no_manifest_stops_the_release(self):
        c = self.case()
        c.link("forgefirm-image-glowforge.rootfs.ext4", BARE_REL_NAME + ".ext4")
        self.assertStoppedBeforeTheSignRequest(c, c.run(VERSION))

    def test_a_refusing_host_test_gate_stops_the_release(self):
        self.assertEqual(len(self.passed.log("host-test-gate.log").splitlines()), 1)
        c = self.case()
        self.assertStoppedBeforeTheSignRequest(c, c.run(VERSION, HOST_TEST_GATE_RC="1"))

    def test_a_root_password_stops_the_release(self):
        c = self.case()
        c.link("forgefirm-image-glowforge.rootfs.ext4", PW_REL_NAME + ".ext4")
        self.assertStoppedBeforeTheSignRequest(c, c.run(VERSION))

    def test_a_read_write_root_stops_the_release(self):
        c = self.case()
        c.link("forgefirm-image-glowforge.rootfs.ext4", RW_REL_NAME + ".ext4")
        self.assertStoppedBeforeTheSignRequest(c, c.run(VERSION))

    # ---------------------------------------------- staging and signing

    def test_a_release_stopped_after_the_signatures_leaves_nothing_where_the_publish_looks(self):
        c = self.case()
        for d in ("v%s" % VERSION, "v%s.tmp" % VERSION):
            write(os.path.join(c.staging, d, "forgefirm.fw"), "an earlier run's archive\n")
        p = c.run(VERSION)
        self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
        p = c.sign(VERSION, FWUP_COMPAT_FAIL="1")
        self.assertEqual(p.returncode, 1, p.stdout + p.stderr)
        self.assertIsNotNone(c.log("mkfw.log"), "stopped before the signatures:\n" + p.stderr)
        self.assertFalse(os.path.exists(c.staged()), "a stopped release left v%s" % VERSION)
        with open(c.stage("forgefirm.fw")) as f:
            self.assertNotIn("earlier run", f.read())

    def test_the_sign_step_refuses_a_rootfs_the_gates_did_not_judge(self):
        c = self.staged_case()
        rootfs = c.stage(os.path.join("sign", STAGED_NAMES[0]))
        size = os.path.getsize(rootfs)
        with open(rootfs, "ab") as f:
            f.write(b"\0")
        self.assertSignRefused(c, c.sign(VERSION))
        # nor a card image (or any staged file) that moved
        os.truncate(rootfs, size)
        with open(c.stage("forgefirm-image-glowforge.rootfs.wic.gz"), "a") as f:
            f.write("another card\n")
        self.assertSignRefused(c, c.sign(VERSION))

    def test_the_sign_step_refuses_a_stage_of_another_commit(self):
        # The stage is copied away, then a fix is committed, pushed and
        # tagged (a second staging run would follow): the first stage is no
        # build of this checkout, though the tag names its HEAD.
        c = self.staged_case()
        write(os.path.join(c.repo, "README.md"), "> **ForgeFIRM is in beta.** A fix.\n")
        c.commit("a fix")
        c.pushed()
        git(c.repo, "tag", "-f", "v" + VERSION)
        self.assertSignRefused(c, c.sign(VERSION))
        # HEAD is the staged commit, but the release tag names another
        c = self.case_at("tag-moved")
        p = c.run(VERSION, "--publish")
        self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
        other = subprocess.run(["git", "-C", c.repo, "commit-tree", "HEAD^{tree}", "-m", "other"],
                               capture_output=True, text=True, env=GIT_ENV, check=True).stdout.strip()
        git(c.repo, "tag", "-f", "v" + VERSION, other)
        self.assertSignRefused(c, c.sign(VERSION))
        # and so without --publish, where a tag exists at all
        c = self.case_at("tag-moved-staging")
        p = c.run(VERSION)
        self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
        other = subprocess.run(["git", "-C", c.repo, "commit-tree", "HEAD^{tree}", "-m", "other"],
                               capture_output=True, text=True, env=GIT_ENV, check=True).stdout.strip()
        git(c.repo, "tag", "-f", "v" + VERSION, other)
        self.assertSignRefused(c, c.sign(VERSION))

    def test_the_sign_step_signs_only_from_a_clean_checkout(self):
        # The allowed signers file binds the release key: edited in plain
        # sight, and behind skip-worktree; and a script only a local exclude
        # hides.
        c = self.staged_case()
        signers = os.path.join(c.repo, "releases", "allowed_signers")
        with open(signers) as f:
            text = f.read()
        stranger = "forgetest-bench ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIStranger\n"
        with open(signers, "a") as f:
            f.write(stranger)
        self.assertSignRefused(c, c.sign(VERSION))
        git(c.repo, "update-index", "--skip-worktree", "releases/allowed_signers")
        self.assertSignRefused(c, c.sign(VERSION))
        git(c.repo, "update-index", "--no-skip-worktree", "releases/allowed_signers")
        write(signers, text)
        write(os.path.join(c.repo, "scripts", "extra.py"), "# a script of its own\n")
        with open(os.path.join(c.repo, ".git", "info", "exclude"), "a") as f:
            f.write("extra.py\n")
        self.assertSignRefused(c, c.sign(VERSION))

    def test_every_asset_to_publish_is_a_file_the_staging_run_listed(self):
        c = self.staged_case()
        write(c.stage("extra.bin"), "a file no staging run made\n")
        c.edit_request({"assets": c.request()["assets"] + " extra.bin"})
        self.assertSignRefused(c, c.sign(VERSION))

    def test_the_sign_step_asks_the_gate_about_the_release_rootfs(self):
        # The gate ran twice for the release that passed, the second time in
        # the sign step, on the committed artifact and the manifest of the
        # staged release rootfs.
        self.assertEqual(len(self.passed.log("acceptance-gate.log").splitlines()), 2)
        self.assertEqual(self.passed.log("acceptance-gate.0"), "{}\n")
        self.assertEqual(self.passed.log("acceptance-gate.1"), json.dumps(self.rel_manifest))

    def test_the_documentation_tag_must_name_the_documentation_staged(self):
        # The documentation moved on after the staging run, and its tag was
        # made again there: the push printed would name documentation no
        # staging run of this stage checked.
        c = self.case()
        p = c.run(VERSION, "--publish")
        self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
        write(os.path.join(c.docs, "index.md"), "docs, later\n")
        git(c.docs, "commit", "-q", "-am", "later")
        git(c.docs, "tag", "-f", "-a", "v" + VERSION, "-m", "again")
        self.assertSignRefused(c, c.sign(VERSION))

    def test_the_tag_pushes_name_the_checkouts_the_sign_step_ran_with(self):
        # The sign step on another machine: a copy of the checkout and of the
        # documentation, each carrying its tag, the documentation beside it.
        c = self.case()
        p = c.run(VERSION, "--publish")
        self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
        other = os.path.join(c.root, "other-machine")
        for name in ("forgefirm", "forgefirm-docs"):
            shutil.copytree(os.path.join(c.root, name), os.path.join(other, name), symlinks=True)
        p = c.sign(VERSION, checkout=os.path.join(other, "forgefirm"), FORGEFIRM_DOCS_DIR=None)
        self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
        pushes = [ln.strip() for ln in p.stdout.splitlines() if " push origin " in ln]
        self.assertEqual(pushes, ["git -C %s push origin v%s" % (os.path.join(other, name), VERSION)
                                  for name in ("forgefirm", "forgefirm-docs")])

    def test_a_gated_request_the_gate_does_not_confirm_is_refused(self):
        # A build staged with the gate skipped, its request edited to say
        # gated, with the committed artifact staged and named beside it:
        # the gate, run again by the sign step, decides.
        c = self.staged_case(FORGEFIRM_ACCEPTANCE_SKIP="1")
        art = os.path.join(c.repo, "releases", "v" + VERSION, "acceptance.json")
        shutil.copy(art, c.stage("acceptance.json"))
        sha = subprocess.run(["sha256sum", art], capture_output=True, text=True).stdout.split()[0]
        c.edit_request({"gated": "1", "artifact": sha, "hash.acceptance.json": sha,
                        "assets": c.request()["assets"] + " acceptance.json"})
        self.assertSignRefused(c, c.sign(VERSION, ACCEPTANCE_GATE_RC="1"))
        self.assertEqual(len(c.log("acceptance-gate.log").splitlines()), 1, "the sign step did not ask the gate")

    def test_the_staged_artifact_is_the_committed_one(self):
        # Another acceptance.json put in the stage, with its hash line
        # rewritten to match: the release would carry an artifact that is
        # not the one the gate read.
        c = self.staged_case()
        write(c.stage("acceptance.json"), '{"a": "different artifact"}\n')
        sha = subprocess.run(["sha256sum", c.stage("acceptance.json")], capture_output=True,
                             text=True).stdout.split()[0]
        c.edit_request({"hash.acceptance.json": sha})
        self.assertSignRefused(c, c.sign(VERSION))

    def test_a_request_the_staging_run_never_writes_is_refused(self):
        # a dev-signed build with the publish commands
        c = self.staged_case(FORGEFIRM_ACCEPTANCE_SKIP="1")
        c.edit_request({"publish": "1"})
        self.assertSignRefused(c, c.sign(VERSION))

    def test_only_a_run_with_the_publish_gates_prints_the_publish_command(self):
        self.assertIn("gh release create", self.passed_sign.stdout)
        c = self.staged_case()
        self.assertEqual(c.request()["publish"], "0")
        p = c.sign(VERSION)
        self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
        self.assertNotIn("gh release create", p.stdout)

    def test_an_existing_docs_tag_must_name_the_docs_head(self):
        c = self.case()
        git(c.docs, "tag", "-a", "v" + VERSION, "-m", "an earlier run")
        write(os.path.join(c.docs, "index.md"), "docs, later\n")
        git(c.docs, "commit", "-q", "-am", "later")
        self.assertStoppedBeforeTheBuild(c, c.run(VERSION))

    def test_the_docs_tag_names_the_next_that_main_fast_forwards_to(self):
        # The documentation checked out on another branch than next.
        c = self.case_at("docs-on-main")
        git(c.docs, "checkout", "-q", "main")
        self.assertStoppedBeforeTheBuild(c, c.run(VERSION))
        # main on the origin moved on, so it cannot fast-forward to next.
        c = self.case_at("docs-main-moved")
        origin = os.path.join(c.root, "docs-origin.git")
        shutil.copytree(self.docs_origin, origin)
        git(c.docs, "remote", "set-url", "origin", origin)
        git(c.docs, "checkout", "-q", "main")
        write(os.path.join(c.docs, "index.md"), "docs, fixed on main\n")
        git(c.docs, "commit", "-q", "-am", "on main")
        git(c.docs, "push", "-q", "origin", "main")
        git(c.docs, "checkout", "-q", "next")
        self.assertStoppedBeforeTheBuild(c, c.run(VERSION))
        # the passed release tagged next
        tag = subprocess.run(["git", "-C", self.passed.docs, "rev-parse", "v%s^{commit}" % VERSION, "next"],
                             capture_output=True, text=True, env=GIT_ENV).stdout.split()
        self.assertEqual(tag[0], tag[1])

    # --------------------------------------------------------- the rootfs

    def test_the_dev_rootfs_is_the_one_its_own_link_names(self):
        # What the sign step signs are the copies whose hashes the gates
        # recorded, of the rootfs each image's own link names; each card
        # image is the one made with its own rootfs.
        self.assertEqual(self.passed_run.returncode, 0, self.passed_run.stdout + self.passed_run.stderr)

        def sha(name):
            return subprocess.run(["sha256sum", os.path.join(self.images, name + ".ext4")], capture_output=True,
                                  text=True).stdout.split()[0]
        self.assertEqual(self.passed_request["rootfs"].split(), [STAGED_NAMES[0], sha(REL_NAME)])
        self.assertEqual(self.passed_request["dev_rootfs"].split(), [STAGED_NAMES[1], sha(DEV_NAME)])
        for name, image in (("forgefirm-image-glowforge.rootfs.wic.gz", REL_NAME),
                            ("forgefirm-image-dev-glowforge.rootfs.wic.gz", DEV_NAME)):
            with open(self.passed.staged(name)) as f:
                self.assertEqual(f.read(), "card image of %s\n" % image)

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
            self.assertFalse(os.path.exists(c.stage("SIGN-REQUEST")))

    def test_a_dev_rootfs_of_another_build_stops_the_release_before_the_signatures(self):
        c = self.case()
        c.link("forgefirm-image-dev-glowforge.rootfs.ext4", OTHER_DEV_NAME + ".ext4")
        self.assertStoppedBeforeTheSignRequest(c, c.run(VERSION))

    def test_a_rootfs_not_built_from_its_pins_stops_the_release_before_the_signatures(self):
        # One build, its two images agreeing, from a layer that differs from
        # its commit.
        c = self.case()
        c.link("forgefirm-image-glowforge.rootfs.ext4", DIRTY_REL_NAME + ".ext4")
        c.link("forgefirm-image-dev-glowforge.rootfs.ext4", DIRTY_DEV_NAME + ".ext4")
        self.assertStoppedBeforeTheSignRequest(c, c.run(VERSION))

    def test_a_release_rootfs_with_the_dev_images_posture_stops_the_release_before_the_signatures(self):
        # Whatever the build configuration told the rootfs lint, the release
        # rootfs is read for the dev marker, the acceptance tool and sshd's
        # root lines.
        dev_sshd = SSHD.replace("PermitRootLogin no", "PermitRootLogin yes")
        for what, files in (("the dev marker", dict(PYLIB, **{"etc/forgefirm-dev": "v0.0.42 (dev)\n"})),
                            ("the acceptance tool", dict(PYLIB, **{"etc/init.d/forgetest": "#!/bin/sh\n"})),
                            ("root over SSH", dict(PYLIB, **{"etc/ssh/sshd_config_readonly": dev_sshd})),
                            ("no word on empty passwords",
                             dict(PYLIB, **{"etc/ssh/sshd_config": SSHD.replace("PermitEmptyPasswords no\n", "")})),
                            # sshd takes a value inside a Match block for the matching
                            # connections alone: the global value is unset
                            ("root refused inside a Match block only",
                             dict(PYLIB, **{"etc/ssh/sshd_config": SSHD.replace("PermitRootLogin no\n", "")
                                            + "Match User nobody\n    PermitRootLogin no\n"}))):
            with self.subTest(what):
                c = self.case_at("posture-" + what.replace(" ", "-"))
                c.link("forgefirm-image-glowforge.rootfs.ext4",
                       self.rootfs("rel", self.rel_manifest, self.shadow, files, images=c.root))
                self.assertStoppedBeforeTheSignRequest(c, c.run(VERSION))

    def test_a_dev_image_module_forgetest_does_not_declare_stops_the_release_before_the_signatures(self):
        # The dev image's mmap, ctypes and statistics are forgetest's (the
        # release that passed); a module nothing declares is one a release
        # program could import on every campaign and miss on the release
        # image.
        c = self.case()
        c.link("forgefirm-image-dev-glowforge.rootfs.ext4",
               self.rootfs("dev", self.dev_manifest, self.shadow, dict(DEV_PYLIB, **{DYN % "_decimal": ""}),
                           images=c.root))
        p = c.run(VERSION)
        self.assertStoppedBeforeTheSignRequest(c, p)
        self.assertIn("decimal", p.stderr)

    def test_the_root_password_gate_reads_a_large_shadow_to_its_end(self):
        # Root is the first line of a shadow larger than a pipe holds, so a
        # reader that stops at it would leave debugfs writing.
        self.assertGreater(len(self.shadow), 2 * 65536)
        self.assertEqual(self.passed_run.returncode, 0, self.passed_run.stdout + self.passed_run.stderr)

    # ------------------------------------------------------- the assets

    def test_the_dev_archive_is_signed_with_the_dev_key_and_goes_out_labeled(self):
        stage = self.passed.stage()
        signed = {s[2]: s for s in (ln.split("|") for ln in self.passed.log("mkfw.log").splitlines())}
        self.assertEqual(signed[os.path.join(stage, "forgefirm-dev.fw")][3], self.dev_key)
        self.assertEqual(signed[os.path.join(stage, "forgefirm.fw")][3], self.release_key)
        with open(self.passed.staged("sha256sums.txt")) as f:
            summed = {ln.split()[1] for ln in f}
        dev_assets = ("forgefirm-dev.fw", "forgefirm-image-dev-glowforge.rootfs.wic.gz", "forgefirm-dev.pub")
        self.assertTrue(set(dev_assets) <= summed, summed)
        with open(self.passed.staged("forgefirm-dev.pub")) as a, open(self.dev_key[:-5] + ".pub") as b:
            self.assertEqual(a.read(), b.read())
        gh = self.passed_sign.stdout
        for name in dev_assets:
            self.assertEqual(gh.count('"%s#%s (' % (name, name)), 1, gh)
        # and never without its label
        listed = [ln for ln in gh.splitlines() if "sha256sums.txt.sig" in ln and ln.rstrip().endswith("\\")]
        self.assertEqual(len(listed), 1, gh)
        for name in dev_assets:
            self.assertNotIn(name, listed[0].split())

    def test_the_sums_are_signed_with_the_sums_key(self):
        signers = os.path.join(self.passed.repo, "releases", "allowed_signers")
        with open(self.passed.staged("sha256sums.txt"), "rb") as f:
            p = subprocess.run(["ssh-keygen", "-Y", "verify", "-f", signers, "-I", "forgefirm-sums",
                                "-n", "forgefirm-sums", "-s", self.passed.staged("sha256sums.txt.sig")],
                               stdin=f, capture_output=True)
        self.assertEqual(p.returncode, 0, p.stderr)
        self.assertIn("sha256sums.txt.sig", self.passed_sign.stdout)

    def test_the_factory_era_check_reads_the_key_without_its_line_ending(self):
        self.assertEqual(self.passed_sign.returncode, 0, self.passed_sign.stdout + self.passed_sign.stderr)
        self.assertIn("-p", self.passed.log("fwup-compat.log").split())

    def test_the_source_bundle_is_asked_for_both_images(self):
        argv = json.loads(self.passed.log("source-bundle.log"))
        names = [argv[i + 1] for i, a in enumerate(argv) if a == "--image-name"]
        self.assertEqual(names, [REL_NAME, DEV_NAME])


@unittest.skipUnless(os.path.isdir(os.path.join(REPO, ".git")) or os.path.isfile(os.path.join(REPO, ".git")),
                     "needs a forgefirm git checkout")
class ScriptModeTests(unittest.TestCase):

    def test_the_scripts_the_pipeline_runs_by_path_are_executable(self):
        # release.sh runs mkfw.sh by its path, and the docs and release.sh
        # tell the owner to run release.sh and release-sign.sh so: each is
        # executable as git checks it out.
        names = ("scripts/release.sh", "scripts/release-sign.sh", "scripts/mkfw.sh")
        out = subprocess.run(["git", "-C", REPO, "ls-files", "-s", "--"] + list(names), capture_output=True,
                             text=True, check=True).stdout
        modes = {ln.split("\t")[1]: ln.split()[0] for ln in out.splitlines()}
        self.assertEqual(modes, {n: "100755" for n in names})


@unittest.skipUnless(shutil.which("fwup") and os.path.isfile(MKFW), "needs the host fwup")
class MkfwTests(unittest.TestCase):
    """scripts/mkfw.sh with throwaway keys and the host fwup."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="forgetest-mkfw.")
        self.addCleanup(shutil.rmtree, self.tmp, True)
        for name in ("a", "b"):
            d = os.path.join(self.tmp, name)
            os.makedirs(d)
            subprocess.run(["fwup", "-g"], cwd=d, check=True, capture_output=True)
        self.rootfs = os.path.join(self.tmp, "rootfs.ext4")
        write(self.rootfs, os.urandom(4096))

    def mkfw(self, key, out):
        return subprocess.run(["sh", MKFW, self.rootfs, "v0.0.42", out, key], capture_output=True, text=True,
                              timeout=60)

    def test_an_archive_reaches_its_path_only_once_its_signature_checked_out(self):
        good = os.path.join(self.tmp, "good.fw")
        p = self.mkfw(os.path.join(self.tmp, "a", "fwup-key.priv"), good)
        self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
        self.assertTrue(os.path.isfile(good))
        # a key whose public sidecar is another key's: the check fails, and
        # nothing is left at the output path, an earlier archive included
        shutil.copy(os.path.join(self.tmp, "b", "fwup-key.pub"), os.path.join(self.tmp, "a", "fwup-key.pub"))
        p = self.mkfw(os.path.join(self.tmp, "a", "fwup-key.priv"), good)
        self.assertNotEqual(p.returncode, 0)
        self.assertFalse(os.path.exists(good))


def load_host_gate():
    spec = importlib.util.spec_from_file_location("host_test_gate", HOST_GATE)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


# A gh that answers each workflow-runs query from the JSON file GH_RUNS
# names, keyed by the query's repository, workflow and commit, and records
# the query.
GH = r"""#!/usr/bin/env python3
import json, os, re, sys
with open(os.environ["GH_LOG"], "a") as f:
    f.write(sys.argv[2] + "\n")
m = re.match(r"repos/([^/]+/[^/]+)/actions/workflows/([^/]+)/runs\?head_sha=([0-9a-f]+)", sys.argv[2])
runs = json.load(open(os.environ["GH_RUNS"])).get("%s %s %s" % m.groups(), [])
print(json.dumps({"total_count": len(runs), "workflow_runs": runs}))
"""


@unittest.skipUnless(os.path.isfile(HOST_GATE), "needs a forgefirm checkout")
class HostTestGateTests(unittest.TestCase):
    """scripts/host-test-gate.py: the lint on the release manifest, and the
    CI conclusions at the pins, with a gh stand-in."""

    def setUp(self):
        self.helpers = helpers
        self.mod = load_host_gate()
        self.tmp = tempfile.mkdtemp(prefix="forgetest-hostgate.")
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.gh = os.path.join(self.tmp, "gh")
        write(self.gh, GH, 0o755)
        self.runs = os.path.join(self.tmp, "runs.json")
        os.environ["GH_RUNS"] = self.runs
        os.environ["GH_LOG"] = os.path.join(self.tmp, "gh.log")
        self.addCleanup(os.environ.pop, "GH_RUNS", None)
        self.addCleanup(os.environ.pop, "GH_LOG", None)
        comps = {"forgectrl": {"srcrev": "c" * 40, "source": "git://github.com/openglow-org/forgectrl.git",
                               "files": [["src/holds.c", "1" * 40]]}}
        self.man = helpers.make_manifest(components=comps)
        self.rows = [("forgectrl", "src/holds.*", "forgectrl:tests/holds_test.c", "forgectrl:.github/workflows/build.yml")]
        self.rev = "f" * 40

    def answer(self, runs):
        with open(self.runs, "w") as f:
            json.dump(runs, f)

    def run_(self, n, conclusion, sha):
        return {"run_number": n, "status": "completed", "conclusion": conclusion, "head_sha": sha}

    def test_each_workflow_is_asked_for_at_the_commit_the_release_pins(self):
        self.assertEqual(self.mod.ci_targets(self.man, self.rows, self.rev),
                         {("openglow-org/forgectrl", ".github/workflows/build.yml"): "c" * 40,
                          ("openglow-org/forgefirm", ".github/workflows/forgetest-ci.yml"): self.rev})

    def test_the_newest_run_decides(self):
        self.answer({"openglow-org/forgectrl build.yml " + "c" * 40:
                     [self.run_(7, "success", "c" * 40), self.run_(9, "failure", "c" * 40)],
                     "openglow-org/forgefirm forgetest-ci.yml " + self.rev:
                     [self.run_(3, "failure", self.rev), self.run_(4, "success", self.rev)]})
        problems = self.mod.ci_problems(self.man, self.rows, self.rev, gh=self.gh)
        self.assertEqual(len(problems), 1, problems)
        self.assertIn("forgectrl build.yml", problems[0])

    def test_a_run_that_has_not_finished_is_refused(self):
        # The push a release waits on: its run is still going.
        self.answer({"openglow-org/forgectrl build.yml " + "c" * 40: [self.run_(9, "success", "c" * 40)],
                     "openglow-org/forgefirm forgetest-ci.yml " + self.rev:
                     [self.run_(4, "success", self.rev),
                      {"run_number": 5, "status": "in_progress", "conclusion": None, "head_sha": self.rev}]})
        problems = self.mod.ci_problems(self.man, self.rows, self.rev, gh=self.gh)
        self.assertEqual(len(problems), 1, problems)
        self.assertIn("forgetest-ci.yml", problems[0])

    def test_a_commit_with_no_run_is_refused(self):
        self.answer({"openglow-org/forgectrl build.yml " + "c" * 40: [self.run_(9, "success", "c" * 40)]})
        problems = self.mod.ci_problems(self.man, self.rows, self.rev, gh=self.gh)
        self.assertEqual(len(problems), 1, problems)
        self.assertIn("forgetest-ci.yml", problems[0])
        self.assertIn("no run", problems[0])

    def test_a_query_gh_cannot_answer_is_refused(self):
        write(self.gh, "#!/bin/sh\necho 'HTTP 401: Bad credentials' >&2\nexit 1\n", 0o755)
        problems = self.mod.ci_problems(self.man, self.rows, self.rev, gh=self.gh)
        self.assertEqual(len(problems), 2, problems)

    def test_a_source_that_cannot_be_fetched_refuses_the_release(self):
        import contextlib
        import io
        from unittest import mock
        path = os.path.join(self.tmp, "manifest.json")
        write(path, json.dumps(self.man.data))

        def unfetchable(*_a):
            raise self.mod.Refused("forgectrl: git fetch failed: no route to the host")
        out = io.StringIO()
        with mock.patch.object(self.mod, "fetch_sources", unfetchable), \
                mock.patch.object(self.mod, "ci_problems", lambda *_a, **_k: []), contextlib.redirect_stdout(out):
            rc = self.mod.main([path])
        self.assertEqual(rc, 1)
        self.assertIn("git fetch failed", out.getvalue())

    def test_a_path_no_test_covers_is_refused(self):
        t = self.helpers.make_test("a.one", [("forgectrl", "src/ui.c")])
        man = self.helpers.with_file(self.man, "forgectrl", "src/new_gate.c", "new")
        problems = self.mod.lint_problems(man, [t], [], None)
        self.assertTrue(any("src/new_gate.c" in p for p in problems), problems)


if __name__ == "__main__":
    unittest.main()
