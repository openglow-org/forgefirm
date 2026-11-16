#!/usr/bin/env python3
# Copyright 2020-2026 514 LLC d/b/a OpenGlow
# Written by Scott Wiederhold
# https://community.openglow.org
# SPDX-License-Identifier:    MIT
#
# Build a ForgeFIRM image manifest from the source tree - the same file lists
# forgefirm-image-manifest.bbclass puts in the image, computed from the recipe
# pins with git instead of a Yocto build. For the coverage lint in CI and for
# checking a coverage map on a workstation; NOT a substitute for the image's
# manifest in the release gate (the platform section carries placeholders
# where only a build knows the answer: kernel config hash, modules dir, DTB).
#
#   manifest-from-tree.py [--meta-openglow PATH] [--out manifest.json]
#                         [--cache DIR] [--kernel-srcrev REV]
#
# Component revisions come from the recipes in meta-forgefirm and the sibling
# meta-openglow checkout (default ../meta-openglow relative to this repo); a
# recipe's `require`d files in its own directory are read too, which is
# where the pins live (<recipe>-pin.inc). Each pinned commit is fetched
# shallowly into --cache (default .manifest-cache/, gitignored) and listed
# with `git ls-tree`; a submodule gitlink is followed through .gitmodules.
# The layer content hash mirrors forgefirm-image-manifest.bbclass: every
# file under the layer that the checkout's own .gitignore files do not
# ignore, except *.md, *-pin.inc and the release version file; a pin file
# or the version file that assigns more than its rule allows stops the
# script, as it stops the build. The kas configuration's text (kas/, the
# lock file left out) is hashed the same way into the platform.
import argparse
import hashlib
import json
import os
import re
import subprocess
import sys

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(REPO, "forgetest"))
from forgetest import manifest as manifest_mod  # noqa: E402

RECIPES = [
    # (component, recipe path relative to the repo or meta-openglow, layer)
    ("forgectrl", "meta-forgefirm/recipes-forgefirm/forgectrl/forgectrl.bb", "forgefirm"),
    ("forgeext", "meta-forgefirm/recipes-forgefirm/forgeext/forgeext.bb", "forgefirm"),
    ("grblhal-glowforge", "meta-forgefirm/recipes-forgefirm/grblhal-glowforge/grblhal-glowforge.bb", "forgefirm"),
    ("forgefirm-app", "meta-forgefirm/recipes-forgefirm/forgefirm-app/forgefirm-app.inc", "forgefirm"),
    ("kernel-module-glowforge", "meta-glowforge-bsp/recipes-kernel/kernel-modules/kernel-module-glowforge.bb", "meta-openglow"),
    ("python3-gfhardware", "meta-glowforge-bsp/recipes-devtools/python/python3-gfhardware.bb", "meta-openglow"),
    ("python3-gfutilities", "meta-openglow-core/recipes-devtools/python/python3-gfutilities_git.bb", "meta-openglow"),
]
# Components built from files in a layer (FORGEFIRM_MANIFEST_SRC in the
# recipe): (component, directory relative to the repo, layer, recipe).
FILE_COMPONENTS = [
    ("ffboot", "meta-forgefirm/recipes-forgefirm/ffboot/files", "forgefirm", "ffboot.bb"),
]
CONTENT_LAYERS = {"meta-forgefirm": ("forgefirm", "meta-forgefirm"),
                  "meta-glowforge-bsp": ("meta-openglow", "meta-glowforge-bsp"),
                  "meta-openglow-core": ("meta-openglow", "meta-openglow-core")}
# Left out of a layer's content, as in forgefirm-image-manifest.bbclass
# (FORGEFIRM_MANIFEST_PIN_SUFFIX, FORGEFIRM_MANIFEST_VERSION_SUFFIX):
# documentation, the component pin files, and the release version file.
# All three are metadata; hashing them would turn a pin bump or a version
# bump into a platform change and invalidate every acceptance result.
LAYER_SKIP_SUFFIXES = (".md", "-pin.inc", "forgefirm-release.inc")
# What a file left out of the layer content may assign, as in the
# bbclass (forgefirm_manifest_metadata_rules): a pin file its component's
# SRCREV and PV, the version file FORGEFIRM_RELEASE.
LAYER_METADATA = (("-pin.inc", ("SRCREV", "SRCREV_[A-Za-z0-9_-]+", "PV")),
                  ("forgefirm-release.inc", ("FORGEFIRM_RELEASE",)))
# The kas configuration's files left out of its hash (FORGEFIRM_MANIFEST_KAS_SKIP).
KAS_SKIP_SUFFIXES = (".md", ".lock.yml")


def git(args, cwd=None, input=None):
    return subprocess.run(["git"] + args, cwd=cwd, input=input, stdout=subprocess.PIPE,
                          stderr=subprocess.PIPE, check=True).stdout


def recipe_text(path, seen=None):
    """The recipe's text with its `require`/`include`d files from the same
    directory appended (bitbake resolves a relative name against the
    including file's directory first). Only local files are followed;
    anything else is left to BBPATH and skipped here."""
    seen = seen if seen is not None else set()
    path = os.path.abspath(path)
    if path in seen:
        return ""
    seen.add(path)
    with open(path, encoding="utf-8") as f:
        text = f.read()
    for m in re.finditer(r'^\s*(?:require|include)\s+(\S+)\s*$', text, re.M):
        cand = os.path.join(os.path.dirname(path), m.group(1))
        if os.path.isfile(cand):
            text += "\n" + recipe_text(cand, seen)
    return text


def parse_recipe(path):
    text = recipe_text(path)
    uri = re.search(r'^SRC_URI\s*\+?=\s*"([^"]+)"', text, re.M)
    rev = re.search(r'^SRCREV\s*\??=\s*"([0-9a-fA-F]+)"', text, re.M)
    if not uri or not rev:
        raise SystemExit("cannot find SRC_URI/SRCREV in %s" % path)
    first = uri.group(1).split()[0]
    url = first.split(";")[0]
    params = dict(p.split("=", 1) for p in first.split(";")[1:] if "=" in p)
    proto = params.get("protocol", "https")
    if url.startswith("git://") or url.startswith("gitsm://"):
        url = proto + "://" + url.split("://", 1)[1]
    return url, rev.group(1)


def fetch(url, rev, cache):
    """A bare cache repo containing rev (fetched shallowly)."""
    name = hashlib.sha1(url.encode()).hexdigest()[:16]
    repo = os.path.join(cache, name)
    if not os.path.isdir(repo):
        os.makedirs(repo)
        git(["init", "-q", "--bare"], cwd=repo)
    try:
        git(["cat-file", "-e", rev + "^{commit}"], cwd=repo)
    except subprocess.CalledProcessError:
        git(["fetch", "-q", "--depth", "1", url, rev], cwd=repo)
    return repo


def ls_tree(repo, rev, url, cache, prefix, files):
    out = git(["ls-tree", "-r", "--full-tree", rev], cwd=repo).decode()
    modules = None
    for line in out.splitlines():
        if not line.strip():
            continue
        meta, path = line.split("\t", 1)
        typ, obj = meta.split()[1], meta.split()[2]
        files.append([prefix + path, obj])
        if typ == "commit":
            if modules is None:
                modules = {}
                try:
                    gm = git(["show", "%s:.gitmodules" % rev], cwd=repo).decode()
                except subprocess.CalledProcessError:
                    gm = ""
                cur = None
                for l in gm.splitlines():
                    l = l.strip()
                    m = re.match(r'^path\s*=\s*(.+)$', l)
                    if m:
                        cur = m.group(1).strip()
                    m = re.match(r'^url\s*=\s*(.+)$', l)
                    if m and cur:
                        modules[cur] = m.group(1).strip()
            sub_url = modules.get(path)
            if sub_url:
                if sub_url.startswith("../") or sub_url.startswith("./"):
                    base = url.rsplit("/", 1)[0]
                    sub_url = base + "/" + sub_url.lstrip("./")
                sub_repo = fetch(sub_url, obj, cache)
                ls_tree(sub_repo, obj, sub_url, cache, prefix + path + "/", files)


def dir_files(path):
    """[path, blob-id] per regular file under path, relative paths, the
    same walk and hashing as forgefirm-manifest.bbclass (hash-object, so
    the ids compare with tree ids)."""
    paths = []
    for root, dirs, fns in os.walk(path):
        dirs[:] = sorted(x for x in dirs if x not in (".git", "__pycache__"))
        for fn in fns:
            if fn.endswith((".pyc", ".pyo")):
                continue
            p = os.path.join(root, fn)
            if os.path.isfile(p) and not os.path.islink(p):
                paths.append(os.path.relpath(p, path).replace(os.sep, "/"))
    paths.sort()
    if not paths:
        return []
    # absolute paths: hash-object --stdin-paths resolves relative ones against
    # the enclosing repository's top level, not the cwd
    ids = git(["hash-object", "--stdin-paths"], cwd=path,
              input=(chr(10).join(os.path.join(path, p) for p in paths) + chr(10)).encode()).decode().split()
    return [[p, i] for p, i in zip(paths, ids)]


def listing(path):
    """The files under path that a build reads: tracked ones, and untracked
    ones that the checkout's own .gitignore files do not ignore (a local
    exclude file and the user's global excludes do not count)."""
    out = git(["-c", "core.excludesFile=/dev/null", "ls-files", "-z", "--cached", "--others",
               "--exclude-per-directory=.gitignore", "--", "."], cwd=path)
    paths = sorted(set(p.decode("utf-8", "replace") for p in out.split(b"\0") if p))
    return [p for p in paths if os.path.isfile(os.path.join(path, p))]


def metadata_problem(name, text, allowed):
    """Why a file left out of the layer content holds more than its rule
    allows, or None (forgefirm_manifest_metadata_problem in the bbclass)."""
    rx = re.compile(r'^(?:%s)\s*(?:\?\?=|\?=|:=|=)\s*"[^"\\]*"$' % "|".join(allowed))
    for n, line in enumerate(text.splitlines(), 1):
        s = line.strip()
        if not s or s.startswith("#"):
            continue
        if not rx.match(s):
            return "%s:%d: %s (it may assign only %s)" % (name, n, s, ", ".join(allowed))
    return None


def layer_content(path, skip=LAYER_SKIP_SUFFIXES, metadata=LAYER_METADATA):
    paths = []
    for p in listing(path):
        if not p.endswith(skip):
            paths.append(p)
            continue
        for suffix, allowed in metadata:
            if p.endswith(suffix):
                with open(os.path.join(path, p), encoding="utf-8", errors="replace") as f:
                    why = metadata_problem(p, f.read(), allowed)
                if why:
                    raise SystemExit("%s is left out of the layer content, and %s" % (os.path.join(path, p), why))
    if not paths:
        return None
    # hash-object --stdin-paths resolves against the repository top level
    prefix = git(["rev-parse", "--show-prefix"], cwd=path).decode().strip()
    ids = git(["hash-object", "--stdin-paths"], cwd=path,
              input=("\n".join(prefix + p for p in paths) + "\n").encode()).decode().split()
    h = hashlib.sha256()
    for p, i in zip(paths, ids):
        h.update(p.encode("utf-8") + b"\0" + i.encode("ascii") + b"\n")
    return h.hexdigest()


def kas_content(path):
    """The kas configuration's content hash, as the bbclass's
    forgefirm_manifest_kas computes it: its files' text, the lock file
    left out."""
    return layer_content(path, KAS_SKIP_SUFFIXES, ())


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--meta-openglow", default=os.path.join(os.path.dirname(REPO), "meta-openglow"))
    ap.add_argument("--out", default="-")
    ap.add_argument("--cache", default=os.path.join(REPO, ".manifest-cache"))
    ap.add_argument("--kernel-srcrev", default=None,
                    help="linux-fslc SRCREV (default: read from layers/meta-freescale if present)")
    args = ap.parse_args(argv)
    os.makedirs(args.cache, exist_ok=True)

    components = {}
    for name, rel, layer in RECIPES:
        base = REPO if layer == "forgefirm" else args.meta_openglow
        path = os.path.join(base, rel)
        url, rev = parse_recipe(path)
        files = []
        if set(rev) == {"0"}:
            # A pin nothing has been pushed to yet, which is how a work
            # branch carries a component until the merge. The component is
            # named with no files, so the coverage lint reports its covers
            # entries as empty and fails - which is what a pin that has not
            # been bumped should do - rather than the manifest failing to
            # build at all.
            print("%s: the pin is not set (nothing pushed to it yet): no files" % name, file=sys.stderr)
        else:
            repo = fetch(url, rev, args.cache)
            ls_tree(repo, rev, url, args.cache, "", files)
            files.sort()
            print("%s: %s (%d files)" % (name, rev[:12], len(files)), file=sys.stderr)
        components[name] = {"srcrev": rev, "source": url, "files": files, "recipes": [os.path.basename(rel)]}
    for name, rel, layer, recipe in FILE_COMPONENTS:
        base = REPO if layer == "forgefirm" else args.meta_openglow
        files = dir_files(os.path.join(base, rel))
        components[name] = {"srcrev": None, "source": "files", "files": files, "recipes": [recipe]}
        print("%s: files (%d files)" % (name, len(files)), file=sys.stderr)

    ksrc = args.kernel_srcrev
    if not ksrc:
        for cand in ("layers/meta-freescale/recipes-kernel/linux/linux-fslc_6.12.bb",):
            p = os.path.join(REPO, cand)
            if os.path.exists(p):
                m = re.search(r'^SRCREV\s*=\s*"([0-9a-f]+)"', open(p, encoding="utf-8").read(), re.M)
                if m:
                    ksrc = m.group(1)
    components["linux-fslc"] = {"srcrev": ksrc, "source": "git://github.com/Freescale/linux-fslc.git",
                                "config_sha256": None, "recipes": ["linux-fslc"],
                                "files": [["@config", "unknown-without-a-build"], ["@srcrev", ksrc or "unknown"]]}

    layers = {}
    for lname, (repo_key, sub) in CONTENT_LAYERS.items():
        base = REPO if repo_key == "forgefirm" else args.meta_openglow
        lpath = os.path.join(base, sub)
        if os.path.isdir(lpath):
            layers[lname] = {"content_sha256": layer_content(lpath)}
    kas = {"content_sha256": kas_content(os.path.join(REPO, "kas"))}
    platform = {"machine": "glowforge", "layers": layers, "kas": kas, "kernel_modules": [], "dtb": {}}
    canonical = manifest_mod.canonical({"components": components, "platform": platform})
    out = {"format": 1, "image": {"name": "tree", "version": "tree (no build)"},
           "content_sha256": hashlib.sha256(canonical.encode()).hexdigest(),
           "components": components, "platform": platform}
    text = json.dumps(out, sort_keys=True, indent=1) + "\n"
    if args.out == "-":
        sys.stdout.write(text)
    else:
        with open(args.out, "w", encoding="utf-8") as f:
            f.write(text)
        print("wrote %s" % args.out, file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
