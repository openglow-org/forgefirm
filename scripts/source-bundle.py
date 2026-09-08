#!/usr/bin/env python3
# Copyright 2020-2026 514 LLC d/b/a OpenGlow
# Written by Scott Wiederhold
# https://community.openglow.org
# SPDX-License-Identifier:    MIT
#
# Pack the source bundle of a ForgeFIRM release.
#
#   source-bundle.py <version> [--build] [--out DIR] [--deploy DIR]
#                    [--image-name NAME]
#
# The bundle holds the source of the software that the release image
# carries: the archives that the Yocto archiver writes when the build runs
# with kas/source-bundle.yml, the license manifests of the image, the
# license texts, the ForgeFIRM layers, and the kas configuration.
#
# What the bundle must hold comes from the image itself, not from a list in
# this file:
#
#   deploy/licenses/<arch>/<image>/license.manifest        root filesystem
#   deploy/licenses/<arch>/<image>/image_license.manifest  kernel, DTB, U-Boot
#
# Every recipe in those two files whose license is in
# COPYLEFT_LICENSE_INCLUDE (kas/source-bundle.yml, the list the archiver
# filters with) must have an archive. A recipe with no archive stops the
# script, because the release would go out with source missing.
#
# --build runs the archiver pass first. Without it the script packs what
# build/tmp/deploy/sources already holds. scripts/release.sh builds the
# images with the overlay merged, so the release path needs no second build.
import argparse
import gzip
import hashlib
import io
import json
import os
import re
import subprocess
import sys
import tarfile
import time
from fnmatch import fnmatchcase

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
KAS_OVERLAY = os.path.join(REPO, "kas", "source-bundle.yml")
KAS_CONFIG = "kas/forgefirm-glowforge.yml:kas/source-bundle.yml"
# The targets of the archiver pass. The image pulls in the source of
# everything in the root filesystem; the boot loader, the kernel and the
# kernel module reach the machine outside the root filesystem, so they are
# named too. scripts/release.sh builds the same list.
BUILD_TARGETS = ["forgefirm-image", "u-boot", "virtual/kernel",
                 "kernel-module-glowforge"]
# The layers that ForgeFIRM controls. The bundle carries them whole, because
# they are the recipes that build the image. The upstream layers are in
# metadata/LAYERS.txt with their revisions.
FORGEFIRM_LAYERS = [
    ("meta-forgefirm", os.path.join(REPO, "meta-forgefirm")),
    ("meta-glowforge-bsp", os.path.join(REPO, "..", "meta-openglow", "meta-glowforge-bsp")),
    ("meta-openglow-core", os.path.join(REPO, "..", "meta-openglow", "meta-openglow-core")),
]
# A recipe whose source is in the archive of another recipe. The archiver
# skips these by design (archiver.bbclass, include_package).
COVERED_BY = {
    "glibc-locale": "glibc",
    "gcc": "gcc-source-{pv}",
    "gcc-runtime": "gcc-source-{pv}",
    "gcc-sanitizers": "gcc-source-{pv}",
    "libgcc": "gcc-source-{pv}",
}
# GitHub refuses a release asset above 2 GiB.
ASSET_WARN = 1536 * 1024 * 1024
ASSET_FAIL = 2048 * 1024 * 1024
EXCLUDE_NAMES = {".git", "__pycache__", ".pytest_cache"}


def die(msg):
    print("SOURCE BUNDLE FAILED: %s" % msg, file=sys.stderr)
    sys.exit(1)


def warn(msg):
    print("WARNING: %s" % msg, file=sys.stderr)


# --- the policy of the archiver pass -----------------------------------------

def read_policy(path):
    """The license filter of kas/source-bundle.yml. The archiver pass and
    this check read one list, so a recipe cannot be necessary here and
    filtered out there."""
    try:
        text = open(path, encoding="utf-8").read()
    except OSError as exc:
        die("cannot read the archiver policy: %s" % exc)
    policy = {}
    for var in ("COPYLEFT_LICENSE_INCLUDE", "COPYLEFT_LICENSE_EXCLUDE",
                "COPYLEFT_PN_INCLUDE"):
        m = re.search(r'^\s*%s\s*=\s*"([^"]*)"\s*$' % var, text, re.M)
        if not m:
            die("%s is not in %s" % (var, path))
        policy[var] = m.group(1).split()
    return policy


def license_tokens(expr):
    """The license identifiers of a LICENSE expression, without the
    operators and the brackets."""
    return [t for t in re.split(r"[()&|;\s]+", expr or "") if t]


def source_needed(expr, policy):
    """True when a license of the expression is in the include list.

    This follows the decision that oe.license.is_included makes for the
    archiver: an OR selects the branch with the most included licenses, so
    one included license anywhere in the expression archives the recipe.

    A recipe that names an included license and an excluded one in the same
    AND expression is the one case where the two decisions can differ: the
    archiver drops it, and this check keeps it. That direction is the safe
    one. The check then fails and asks for a decision, instead of letting
    the release go out with source missing."""
    return any(fnmatchcase(t, p) for t in license_tokens(expr)
               for p in policy["COPYLEFT_LICENSE_INCLUDE"])


# --- the manifests of the image ----------------------------------------------

def parse_manifest(path, version_key):
    """A license manifest is blocks of `KEY: value` lines, one block for
    each package. Returns {recipe: {"version", "licenses"}}; a recipe with
    several packages collects every license that its packages name."""
    recipes = {}
    block = {}

    def flush():
        name = block.get("RECIPE NAME")
        if not name:
            return
        entry = recipes.setdefault(name, {"version": block.get(version_key, ""),
                                          "licenses": []})
        lic = block.get("LICENSE")
        if lic and lic not in entry["licenses"]:
            entry["licenses"].append(lic)

    for line in open(path, encoding="utf-8"):
        line = line.strip()
        if not line:
            flush()
            block = {}
            continue
        if ":" in line:
            key, val = line.split(":", 1)
            block[key.strip()] = val.strip()
    flush()
    return recipes


def image_recipes(licdir):
    """Every recipe that the image carries, from both manifests. Both are
    necessary: license.manifest alone leaves out the kernel, the device
    tree and the boot loader, which reach the machine outside the root
    filesystem."""
    recipes = {}
    for fn, key in (("license.manifest", "PACKAGE VERSION"),
                    ("image_license.manifest", "VERSION")):
        path = os.path.join(licdir, fn)
        if not os.path.isfile(path):
            die("no %s in %s" % (fn, licdir))
        for name, entry in parse_manifest(path, key).items():
            recipes.setdefault(name, entry)
    return recipes


# --- the archives ------------------------------------------------------------

def index_archives(srcdir, names):
    """{recipe: [(version, directory)]} from
    deploy/sources/<arch>/<recipe>-<version>-<revision>. A directory belongs
    to the longest recipe name that is a prefix of it, so python3-certifi
    does not become python3."""
    index = {}
    if not os.path.isdir(srcdir):
        return index
    ordered = sorted(names, key=len, reverse=True)
    for arch in sorted(os.listdir(srcdir)):
        archdir = os.path.join(srcdir, arch)
        if arch == "mirror" or not os.path.isdir(archdir):
            continue
        for pf in sorted(os.listdir(archdir)):
            path = os.path.join(archdir, pf)
            if not os.path.isdir(path):
                continue
            stem = re.sub(r"-r\d+$", "", pf)
            for name in ordered:
                if stem.startswith(name + "-"):
                    index.setdefault(name, []).append((stem[len(name) + 1:], path))
                    break
    return index


def select_archives(index, recipe, version):
    """The archive directories of one recipe. The deploy directory keeps
    the archives of earlier builds, so the version of the image decides:
    the exact version first, then a version that starts with it (the kernel
    carries the revision of its git source in PV), and every archive of the
    recipe last. The name of an archive carries the epoch of the recipe
    (`1_0.1.4`) and a license manifest does not, so the epoch comes off
    before the comparison."""
    found = [(re.sub(r"^\d+_", "", v), p) for v, p in index.get(recipe, [])]
    for pick in ([p for v, p in found if v == version],
                 [p for v, p in found if v.startswith(version)],
                 [p for _, p in found]):
        if pick:
            return sorted(pick)
    return []


def archive_files(path):
    """The files of one archive directory."""
    out = []
    for root, dirs, files in os.walk(path):
        dirs[:] = sorted(d for d in dirs if d not in EXCLUDE_NAMES)
        for fn in sorted(files):
            full = os.path.join(root, fn)
            if os.path.isfile(full) and not os.path.islink(full):
                out.append(full)
    return out


# --- the layers --------------------------------------------------------------

def git_out(args, cwd):
    try:
        return subprocess.check_output(["git"] + args, cwd=cwd,
                                       stderr=subprocess.DEVNULL).decode().strip()
    except (subprocess.CalledProcessError, OSError):
        return ""


def layer_records(repo):
    """Every layer checkout of the build, with its remote and its
    revision."""
    roots = [("forgefirm", repo),
             ("meta-openglow", os.path.join(repo, "..", "meta-openglow"))]
    layers_dir = os.path.join(repo, "layers")
    if os.path.isdir(layers_dir):
        for name in sorted(os.listdir(layers_dir)):
            path = os.path.join(layers_dir, name)
            if os.path.isdir(os.path.join(path, ".git")):
                roots.append((name, path))
    out = []
    for name, path in roots:
        if not os.path.isdir(path):
            continue
        out.append({
            "checkout": name,
            "url": git_out(["remote", "get-url", "origin"], path),
            "revision": git_out(["rev-parse", "HEAD"], path),
            "branch": git_out(["rev-parse", "--abbrev-ref", "HEAD"], path),
            "modified": bool(git_out(["status", "--porcelain"], path)),
        })
    return out


def tar_tree(name, path):
    """One layer as a reproducible tar.gz in memory."""
    files = []
    for root, dirs, fns in os.walk(path):
        dirs[:] = sorted(d for d in dirs if d not in EXCLUDE_NAMES)
        files += [os.path.join(root, f) for f in sorted(fns)
                  if not f.endswith((".pyc", ".pyo"))]
    buf = io.BytesIO()
    with gzip.GzipFile(fileobj=buf, mode="wb", compresslevel=6, mtime=0) as gz:
        with tarfile.open(fileobj=gz, mode="w", format=tarfile.GNU_FORMAT) as tar:
            for f in sorted(files):
                if os.path.islink(f):
                    continue
                arc = "%s/%s" % (name, os.path.relpath(f, path).replace(os.sep, "/"))
                info = tar.gettarinfo(f, arcname=arc)
                info.mtime, info.uid, info.gid = 0, 0, 0
                info.uname, info.gname = "", ""
                with open(f, "rb") as fh:
                    tar.addfile(info, fh)
    return buf.getvalue()


# --- the bundle --------------------------------------------------------------

class Bundle:
    """The files of the bundle: a name inside the archive with either a path
    on disk or the bytes to write."""

    def __init__(self, root):
        self.root = root
        self.members = []

    def add_file(self, name, path):
        self.members.append((name, path, None))

    def add_bytes(self, name, data):
        self.members.append((name, None, data.encode("utf-8")
                             if isinstance(data, str) else data))

    def add_tree(self, name, path):
        for root, dirs, files in os.walk(path):
            dirs[:] = sorted(d for d in dirs if d not in EXCLUDE_NAMES)
            for fn in sorted(files):
                full = os.path.join(root, fn)
                if os.path.islink(full) or fn.endswith((".pyc", ".pyo")):
                    continue
                self.add_file("%s/%s" % (name, os.path.relpath(full, path).replace(os.sep, "/")),
                              full)

    def checksums(self):
        lines = []
        for name, path, data in sorted(self.members):
            h = hashlib.sha256()
            if path is not None:
                with open(path, "rb") as f:
                    for chunk in iter(lambda: f.read(1 << 20), b""):
                        h.update(chunk)
            else:
                h.update(data)
            lines.append("%s  %s\n" % (h.hexdigest(), name))
        return "".join(lines)

    def write(self, out):
        """One reproducible tar.gz: sorted names, no timestamps, no
        owners."""
        self.add_bytes("sha256sums.txt", self.checksums())
        with open(out, "wb") as raw:
            with gzip.GzipFile(fileobj=raw, mode="wb", compresslevel=6, mtime=0) as gz:
                with tarfile.open(fileobj=gz, mode="w", format=tarfile.GNU_FORMAT) as tar:
                    for name, path, data in sorted(self.members):
                        arc = "%s/%s" % (self.root, name)
                        if path is not None:
                            info = tar.gettarinfo(path, arcname=arc)
                            info.mtime, info.uid, info.gid, info.mode = 0, 0, 0, 0o644
                            info.uname, info.gname = "", ""
                            with open(path, "rb") as f:
                                tar.addfile(info, f)
                        else:
                            info = tarfile.TarInfo(arc)
                            info.size, info.mtime, info.mode = len(data), 0, 0o644
                            tar.addfile(info, io.BytesIO(data))


README = """\
# ForgeFIRM {version} - source

This archive holds the source of the software in ForgeFIRM {version}.

ForgeFIRM is built with the Yocto Project. The archive holds one directory
for each recipe of the release image:

    sources/<recipe>-<version>-<revision>/

A recipe directory holds the upstream source as upstream publishes it, the
patches that the recipe applies, the `series` file that gives their order
and their strip level, and the recipe with its includes. A recipe that gets
its source from git holds a tar of the checkout at the pinned revision.

`SOURCES.txt` maps each recipe of the image to its directory.
`MANIFEST.json` holds the same information for a program to read.

## What the archive holds

| Path | Content |
|---|---|
| `sources/` | The source of each recipe of the image. |
| `licenses/license.manifest` | Every package of the root filesystem with its license. |
| `licenses/image_license.manifest` | The kernel, the device tree and the boot loader. |
| `licenses/texts/` | The license text of each recipe. |
| `metadata/LAYERS.txt` | Each layer of the build with its remote and its revision. |
| `metadata/kas/` | The kas configuration that builds the image. |
| `metadata/meta-*.tar.gz` | The ForgeFIRM layers, whole. |
| `metadata/forgefirm-manifest.json` | The build identity of the release image. |
| `sha256sums.txt` | The checksum of every file above. |

The archive does not hold the upstream layers (poky, meta-openembedded,
meta-freescale, meta-freescale-distro). They are public git repositories.
`metadata/LAYERS.txt` and the kas lock file in `metadata/kas/` give the
revision of each one.

## How to build the image again

1. Get the ForgeFIRM repository. The kas configuration in `metadata/kas/`
   names every layer and revision.
2. Install kas.
3. Build:

       cd forgefirm
       kas build kas/forgefirm-glowforge.yml

The build documentation is at
https://docs.forgefirm.org/developers/building/.

## The ForgeFIRM components

The components that OpenGlow writes are in this archive too. Their
repositories are at https://github.com/openglow-org/.
"""


def main():
    ap = argparse.ArgumentParser(
        description="Pack the source bundle of a ForgeFIRM release")
    ap.add_argument("version", help="release version, without the leading v")
    ap.add_argument("--build", action="store_true",
                    help="run the archiver build pass before packing")
    ap.add_argument("--deploy", default=os.path.join(REPO, "build", "tmp", "deploy"),
                    help="the deploy directory of the build")
    ap.add_argument("--image", default="forgefirm-image-glowforge.rootfs",
                    help="the link name of the release image")
    ap.add_argument("--image-name",
                    help="the exact image name (IMAGE_NAME) that the release is "
                         "cut from; its license manifests decide what the bundle holds")
    ap.add_argument("--out",
                    help="output directory (default: release-staging/v<version>)")
    args = ap.parse_args()

    version = args.version.lstrip("v")
    out_dir = args.out or os.path.join(REPO, "release-staging", "v" + version)
    policy = read_policy(KAS_OVERLAY)

    if args.build:
        cmd = ["kas", "shell", KAS_CONFIG, "-c", "bitbake " + " ".join(BUILD_TARGETS)]
        print("== archiver pass: %s ==" % " ".join(cmd))
        if subprocess.call(cmd, cwd=REPO) != 0:
            die("the archiver build pass failed")

    # The license manifests must belong to the image that the release
    # carries. The link name follows the last build of any image, so the
    # exact image name decides.
    licroot = os.path.join(args.deploy, "licenses")
    archs = sorted(os.listdir(licroot)) if os.path.isdir(licroot) else []
    name = args.image_name
    if not name:
        for arch in archs:
            cand = os.path.join(licroot, arch, args.image)
            if os.path.islink(cand):
                name = os.path.basename(os.readlink(cand).rstrip("/"))
                break
        if not name:
            die("cannot find %s under %s (name the image with --image-name)"
                % (args.image, licroot))
    licdir = None
    for arch in archs:
        cand = os.path.join(licroot, arch, name)
        if os.path.isdir(cand) and not os.path.islink(cand):
            licdir = cand
            break
    if not licdir:
        die("no license manifest directory for image '%s' under %s" % (name, licroot))
    print("image:    %s" % name)
    print("licenses: %s" % licdir)

    recipes = image_recipes(licdir)
    needed = {}
    for recipe, entry in recipes.items():
        # A recipe with several packages can carry several license
        # expressions. They are kept side by side, because joining them with
        # an operator would change what they say.
        expr = " ; ".join(entry["licenses"])
        if source_needed(expr, policy) or \
           any(fnmatchcase(recipe, p) for p in policy["COPYLEFT_PN_INCLUDE"]):
            needed[recipe] = dict(entry, license=expr)
    print("recipes:  %d in the image, %d with source in the bundle"
          % (len(recipes), len(needed)))

    srcdir = os.path.join(args.deploy, "sources")
    lookup = set(needed) | {COVERED_BY[r].format(pv=e["version"])
                            for r, e in needed.items() if r in COVERED_BY}
    index = index_archives(srcdir, lookup)

    # Every recipe that needs source must have an archive.
    missing, records = [], []
    for recipe in sorted(needed):
        entry = needed[recipe]
        holder, note = recipe, None
        dirs = select_archives(index, recipe, entry["version"])
        if not dirs and recipe in COVERED_BY:
            alt = COVERED_BY[recipe].format(pv=entry["version"])
            dirs = select_archives(index, alt, entry["version"])
            if dirs:
                holder, note = alt, "the source is in the archive of %s" % alt
        if not dirs:
            missing.append((recipe, entry["version"], entry["license"]))
            continue
        records.append({"recipe": recipe, "version": entry["version"],
                        "license": entry["license"], "holder": holder,
                        "archives": [os.path.basename(d) for d in dirs],
                        "note": note, "dirs": dirs})
    if missing:
        print("\nrecipes of the image with no source archive:", file=sys.stderr)
        for recipe, ver, lic in missing:
            print("  %-28s %-18s %s" % (recipe, ver, lic), file=sys.stderr)
        die("%d recipe(s) have no source. Run the build with "
            "kas/source-bundle.yml, or name the recipe in COPYLEFT_PN_INCLUDE "
            "in that file." % len(missing))

    # --- collect ---
    bundle = Bundle("forgefirm-source-v" + version)
    packed, total = set(), 0
    for rec in records:
        for d in rec["dirs"]:
            if d in packed:
                continue
            packed.add(d)
            for f in archive_files(d):
                total += os.path.getsize(f)
                bundle.add_file("sources/%s/%s"
                                % (os.path.basename(d),
                                   os.path.relpath(f, d).replace(os.sep, "/")), f)

    bundle.add_file("licenses/license.manifest",
                    os.path.join(licdir, "license.manifest"))
    if os.path.isfile(os.path.join(licdir, "image_license.manifest")):
        bundle.add_file("licenses/image_license.manifest",
                        os.path.join(licdir, "image_license.manifest"))
    for recipe in sorted(recipes):
        for arch in archs:
            texts = os.path.join(licroot, arch, recipe)
            if os.path.isdir(texts) and not os.path.islink(texts):
                bundle.add_tree("licenses/texts/%s" % recipe, texts)
                break

    for fn in sorted(os.listdir(os.path.join(REPO, "kas"))):
        if fn.endswith(".yml"):
            bundle.add_file("metadata/kas/%s" % fn, os.path.join(REPO, "kas", fn))
    # The build identity of the image the bundle answers for. Its absence
    # means the deploy directory no longer holds that image, so the bundle
    # would speak for a build that is not there.
    image_manifest = os.path.join(args.deploy, "images", "glowforge",
                                  name + ".forgefirm-manifest.json")
    if not os.path.isfile(image_manifest):
        die("no manifest for image '%s' at %s" % (name, image_manifest))
    bundle.add_file("metadata/forgefirm-manifest.json", image_manifest)
    layers = layer_records(REPO)
    table = ["%-22s %-56s %-42s %s" % ("CHECKOUT", "URL", "REVISION", "BRANCH")]
    for lay in layers:
        table.append("%-22s %-56s %-42s %s%s"
                     % (lay["checkout"], lay["url"] or "-", lay["revision"] or "-",
                        lay["branch"] or "-", "  (modified)" if lay["modified"] else ""))
    bundle.add_bytes("metadata/LAYERS.txt", "\n".join(table) + "\n")
    for layer, path in FORGEFIRM_LAYERS:
        path = os.path.normpath(path)
        if not os.path.isdir(path):
            die("layer %s is not at %s" % (layer, path))
        bundle.add_bytes("metadata/%s.tar.gz" % layer, tar_tree(layer, path))

    table = ["ForgeFIRM v%s - the source of each recipe of the release image" % version,
             "",
             "%-28s %-20s %-46s %s" % ("RECIPE", "VERSION", "ARCHIVE", "LICENSE")]
    for rec in records:
        table.append("%-28s %-20s %-46s %s"
                     % (rec["recipe"], rec["version"], ",".join(rec["archives"]),
                        rec["license"]))
    bundle.add_bytes("SOURCES.txt", "\n".join(table) + "\n")
    bundle.add_bytes("README.md", README.format(version="v" + version))
    bundle.add_bytes("MANIFEST.json", json.dumps({
        "format": 1,
        "release": "v" + version,
        "image": name,
        "policy": {"license_include": policy["COPYLEFT_LICENSE_INCLUDE"],
                   "license_exclude": policy["COPYLEFT_LICENSE_EXCLUDE"],
                   "recipe_include": policy["COPYLEFT_PN_INCLUDE"],
                   "recipe_types": ["target"]},
        "recipes": [{k: v for k, v in rec.items() if k != "dirs"} for rec in records],
        # The other recipes of the image, with the license that keeps them
        # out. The record says what was decided, not only what was packed.
        "recipes_without_source": [
            {"recipe": r, "version": recipes[r]["version"],
             "license": " ; ".join(recipes[r]["licenses"])}
            for r in sorted(set(recipes) - set(needed))],
        "layers": layers,
    }, indent=1, sort_keys=True) + "\n")

    os.makedirs(out_dir, exist_ok=True)
    out = os.path.join(out_dir, "forgefirm-source-v%s.tar.gz" % version)
    print("packing:  %d archives, %d files, %.1f MiB of source"
          % (len(packed), len(bundle.members), total / 1048576.0))
    start = time.time()
    bundle.write(out)
    size = os.path.getsize(out)
    print("== %s (%.1f MiB, %.0f s) ==" % (out, size / 1048576.0, time.time() - start))
    if size >= ASSET_FAIL:
        die("the bundle is %.2f GiB. A release asset of GitHub must stay below 2 GiB."
            % (size / 1073741824.0))
    if size >= ASSET_WARN:
        warn("the bundle is %.2f GiB, close to the 2 GiB limit of a release asset"
             % (size / 1073741824.0))
    return 0


if __name__ == "__main__":
    sys.exit(main())
