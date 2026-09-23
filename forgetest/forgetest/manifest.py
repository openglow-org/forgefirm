# Copyright 2026 514 LLC d/b/a OpenGlow
# Written by Scott Wiederhold
# https://community.openglow.org
# SPDX-License-Identifier:    MIT

"""The image manifest and the domain fingerprint.

/etc/forgefirm-manifest.json (written by forgefirm-image-manifest.bbclass)
identifies the build's inputs: for every component the pinned revision and
one [path, blob-id] pair per source file, plus the platform identity
(machine, kernel modules directory, device tree hashes, layer content
hashes). This module loads it and computes a test's *domain fingerprint*:
the hash of the source files its coverage globs select, plus the platform,
plus the test's own implementation. A recorded PASS applies to a build
exactly when the fingerprint recomputed from that build's manifest is the
same - the same code runs on the board and in the release gate.
"""
import functools
import hashlib
import json
import os
import re

DEFAULT_PATH = "/etc/forgefirm-manifest.json"

# Components that ship only on the dev image. They can never be part of a
# fingerprint (the release manifest lacks them, so the gate could not
# recompute it); the test implementation is folded in separately.
DEV_ONLY_COMPONENTS = ("forgetest",)


def canonical(obj):
    """Canonical JSON: sorted keys, no whitespace - the hashing form."""
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=True)


def sha256_text(text):
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


@functools.lru_cache(maxsize=512)
def glob_to_regex(pattern):
    """Coverage glob -> anchored regex. '**' spans directories, '*' and '?'
    stay inside one path segment. Paths use '/' (git paths). Cached: the
    catalog matches the same few hundred globs over and over."""
    out = []
    i, n = 0, len(pattern)
    while i < n:
        c = pattern[i]
        if c == "*":
            if pattern[i:i + 2] == "**":
                # '**/' also matches zero directories
                if pattern[i:i + 3] == "**/":
                    out.append("(?:.*/)?")
                    i += 3
                    continue
                out.append(".*")
                i += 2
                continue
            out.append("[^/]*")
        elif c == "?":
            out.append("[^/]")
        else:
            out.append(re.escape(c))
        i += 1
    return re.compile("^" + "".join(out) + "$")


def match_files(files, pattern):
    """(path, blob) pairs from a component's file list that the glob selects."""
    rx = glob_to_regex(pattern)
    return [(p, b) for p, b in files if rx.match(p)]


# Paths that carry no target behavior: docs, CI, the components' own unit
# tests, licenses, editor setup. Outside every fingerprint (a README edit
# re-requires nothing) and outside the coverage lint (no test has to name
# them). Reviewed with the catalog: widening this list is a change like
# any other. "*" applies to every component.
NON_BEHAVIORAL = [
    ("*", ".github/**"),
    ("*", ".gitignore"),
    ("*", ".gitattributes"),
    ("*", ".gitmodules"),
    ("*", "**/*.md"),
    ("*", "LICENSE*"),
    ("*", "COPYING*"),
    ("*", "docs/**"),
    ("*", "tests/**"),
    ("*", "graphify-out/**"),
    ("*", "**/.gitkeep"),
    ("*", ".devcontainer/**"),
    ("*", ".vscode/**"),
    ("*", ".env.example"),
    ("forgectrl", "tools/**"),        # host-side dev tools (panel dev server)
    ("forgectrl", "examples/**"),     # clients of the remote API, run on another computer
    # forgeext's recipe installs the binary and its init script, and nothing of these: the official
    # packages carry their own acceptance artifact, and the author's kit and host tools run off the image.
    ("forgeext", "packages/**"),
    ("forgeext", "sdk/**"),
    ("forgeext", "template/**"),
    ("forgeext", "tools/**"),
]


# Paths the list above would take out that are behavior all the same: forgectrl embeds its advisory
# documents in the binary, serves them, and records the operator's consent to a document by its hash, so
# an edited advisory is a changed consent and must move every fingerprint that covers it.
BEHAVIORAL = [
    ("forgectrl", "docs/advisories/**"),
]


def non_behavioral(comp, path, allow=NON_BEHAVIORAL):
    for c, pat in BEHAVIORAL:
        if c == comp and glob_to_regex(pat).match(path):
            return False
    for c, pat in allow:
        if c in ("*", comp) and glob_to_regex(pat).match(path):
            return True
    return False


class Manifest:
    def __init__(self, data):
        self.data = data
        self.components = data.get("components", {}) or {}
        self.platform = data.get("platform", {}) or {}
        self.image = data.get("image", {}) or {}
        self.content_sha = data.get("content_sha256")
        self._files = {}

    @classmethod
    def load(cls, path=None):
        path = path or os.environ.get("FORGETEST_MANIFEST") or DEFAULT_PATH
        with open(path, "r", encoding="utf-8") as f:
            return cls(json.load(f))

    @classmethod
    def from_json(cls, text):
        return cls(json.loads(text))

    @property
    def version(self):
        return self.image.get("version") or "unknown"

    @property
    def image_name(self):
        return self.image.get("name") or "unknown"

    def files(self, component):
        """The (path, blob) pairs of a component, or None if the component
        is not in this manifest. Built once per component: the manifest is
        immutable and every coverage glob asks for the same lists."""
        if component in self._files:
            return self._files[component]
        c = self.components.get(component)
        out = None if c is None else [tuple(x) for x in c.get("files", [])]
        self._files[component] = out
        return out

    def component_names(self):
        return sorted(self.components)

    def identity_sha(self):
        """sha256 of the acceptance-relevant identity: every component
        except the dev-only ones, plus the platform. Informational (the
        gate decides per test, by fingerprint)."""
        comps = {k: v for k, v in self.components.items() if k not in DEV_ONLY_COMPONENTS}
        return sha256_text(canonical({"components": comps, "platform": self.platform}))


def fingerprint(manifest, covers, extra=()):
    """The domain fingerprint of a coverage map on a manifest.

    covers: iterable of (component, glob). extra: strings folded in after
    the files (the test's own implementation hash). A component the
    manifest lacks contributes a marker so the fingerprint is still
    defined and distinct.
    """
    parts = set()
    for comp, pat in covers:
        if comp in DEV_ONLY_COMPONENTS:
            raise ValueError("coverage may not name the dev-only component %r" % comp)
        files = manifest.files(comp)
        if files is None:
            parts.add((comp, "@missing", ""))
            continue
        for p, b in match_files(files, pat):
            if non_behavioral(comp, p):
                continue
            parts.add((comp, p, b))
    h = hashlib.sha256()
    h.update(canonical(sorted(parts)).encode("utf-8"))
    h.update(b"\n")
    h.update(canonical(manifest.platform).encode("utf-8"))
    for e in extra:
        h.update(b"\n")
        h.update(str(e).encode("utf-8"))
    return h.hexdigest()


def empty_covers(manifest, tests):
    """Coverage entries that select no file of their component: a glob
    that never matched (paths anchor at the repository root, so a file
    under a recipe's subdirectory needs that directory in the glob), or
    a component not in the manifest. Such an entry covers nothing, and
    the test's fingerprint would not move with the file it meant.
    Returns [(test id, component, glob)]. A glob selecting only
    non-behavioral paths is hollow too: nothing it names is fingerprinted."""
    out = []
    for t in tests:
        for comp, pat in t.covers:
            files = manifest.files(comp)
            if files is None or not [p for p, _b in match_files(files, pat) if not non_behavioral(comp, p)]:
                out.append((t.id, comp, pat))
    return out


def coverage_report(manifest, tests, allow=NON_BEHAVIORAL):
    """Which manifest paths no test covers.

    tests: iterable with .covers. allow: iterable of (component, glob)
    that need no coverage (docs, CI, licenses...). Returns
    {component: [uncovered paths]} for the non-dev-only components.
    """
    covered = {}
    for t in tests:
        for comp, pat in t.covers:
            covered.setdefault(comp, []).append(glob_to_regex(pat))
    allowed = {}
    for comp, pat in allow:
        allowed.setdefault(comp, []).append(glob_to_regex(pat))
    report = {}
    for comp in manifest.component_names():
        if comp in DEV_ONLY_COMPONENTS:
            continue
        rxs = covered.get(comp, []) + allowed.get(comp, [])
        star = allowed.get("*", [])
        missing = []
        for p, _b in manifest.files(comp):
            if any(rx.match(p) for rx in rxs) or any(rx.match(p) for rx in star):
                continue
            missing.append(p)
        if missing:
            report[comp] = sorted(missing)
    return report
