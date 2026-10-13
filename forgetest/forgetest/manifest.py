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
import posixpath
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
    # forgeext's recipe installs the binary and its init script, and nothing of these: the author's kit
    # and the host tools run off the image (OpenGlow's own packages have repositories of their own).
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


# Paths a host test in CI proves, which no catalog test covers. The catalog
# is for what needs the machine or an operator; software whose inputs can be
# emulated is proven on a host. A row is (component, glob, host test, CI
# workflow), the last two named as "<repository>:<path>": a component of
# the manifest (its tests/ and .github/ files are listed there), or
# "forgefirm" for this repository (the null-sink harnesses under
# scripts/bench/, which grblhal-glowforge's workflow runs). The workflow
# must run the host test: name it, or discover it (a glob, unittest
# discover, pytest on its directory). A host-tested path counts as covered
# in the coverage lint, and like every path no test covers it is in no
# fingerprint: a change there makes no acceptance run necessary. A path a
# catalog test exercises on the hardware belongs in that test's covers, not
# here.
HOST_TESTED = [
]

# This repository's root, when the package runs from a checkout of it; a
# "forgefirm:" file is looked for there. Installed on an image, the package
# is not in a checkout, and such a reference is not checked.
_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
REPO_ROOT = _ROOT if os.path.isdir(os.path.join(_ROOT, "meta-forgefirm")) else None
# The pinned sources scripts/manifest-from-tree.py fetches (one bare repo
# per source URL); a component's workflow is read from there. Without them
# (on an image) whether a workflow runs a host test is not checked.
SOURCES = os.path.join(REPO_ROOT, ".manifest-cache") if REPO_ROOT else None


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


def _ref_problem(manifest, ref, what, root):
    """Why a "<repository>:<path>" reference names no file, or None. A
    component's file must be in the manifest; a "forgefirm:" one must exist
    under `root`, and is not checked without one."""
    repo, sep, path = ref.partition(":")
    if not sep or not path:
        return "names no repository:path for its %s" % what
    if repo == "forgefirm":
        if root is not None and not os.path.isfile(os.path.join(root, path)):
            return "no such %s" % what
        return None
    if repo in DEV_ONLY_COMPONENTS or manifest.files(repo) is None:
        return "no repository %r for its %s" % (repo, what)
    if path not in {p for p, _b in manifest.files(repo)}:
        return "no such %s" % what
    return None


def read_source(manifest, ref, root=REPO_ROOT, sources=SOURCES):
    """(text, why) for a "<repository>:<path>" file: a "forgefirm:" file
    from `root`, a component's from its pinned blob in the bare repo
    `sources` holds for its source URL. (None, None) when it cannot be read
    here (no root, no sources); (None, why) when it should have been and
    was not, which is a defect, never a pass."""
    import subprocess
    repo, _sep, path = ref.partition(":")
    if repo == "forgefirm":
        if root is None:
            return None, None
        try:
            with open(os.path.join(root, path), encoding="utf-8") as f:
                return f.read(), None
        except (OSError, UnicodeDecodeError) as e:
            return None, "cannot be read: %s" % e
    if not sources:
        return None, None
    comp = manifest.components.get(repo) or {}
    blob = dict(manifest.files(repo) or []).get(path)
    url = comp.get("source")
    if not (blob and url):
        return None, "cannot be read: no blob or source URL in the manifest"
    cache = os.path.join(sources, hashlib.sha1(url.encode()).hexdigest()[:16])
    try:
        return subprocess.run(["git", "cat-file", "-p", blob], cwd=cache, capture_output=True,
                              check=True).stdout.decode("utf-8", "replace"), None
    except (OSError, subprocess.CalledProcessError) as e:
        return None, "cannot be read from %s: %s" % (cache, e)


def repo_name(manifest, repo):
    """A repository's GitHub name: its source URL's last part, or this
    repository's."""
    if repo == "forgefirm":
        return "forgefirm"
    url = ((manifest.components.get(repo) or {}).get("source") or "").rstrip("/")
    name = url.rsplit("/", 1)[-1]
    return name[:-4] if name.endswith(".git") else name


_STEP_KEYS = ("name", "uses", "run", "id", "if", "with", "env", "working-directory", "shell",
              "continue-on-error", "timeout-minutes")
_STEP_RX = re.compile(r"^(\s*)-\s+(%s)\s*:" % "|".join(re.escape(k) for k in _STEP_KEYS))
_KEY_RX = re.compile(r"^(\s*)([\w-]+)\s*:\s*(.*)$")


def _indent(line):
    return len(line) - len(line.lstrip(" "))


def workflow_steps(text):
    """A workflow's steps, each a dict of its keys: "uses", "run" (the
    command text, a block's lines joined), "working-directory", and "with"
    (a dict). YAML read by its indentation, as far as these workflows use
    it; comments, whole lines and from ' #' to the end of a line, are not
    read. Anything outside a step (a trigger's paths, a job's name) is not
    a step."""
    lines = []
    for ln in text.splitlines():
        if ln.lstrip().startswith("#"):
            continue
        lines.append(re.sub(r"\s+#.*$", "", ln).rstrip())
    steps, i = [], 0
    while i < len(lines):
        m = _STEP_RX.match(lines[i])
        if not m:
            i += 1
            continue
        dash = len(m.group(1))
        body = [" " * (dash + 2) + lines[i][dash + 1:].lstrip()]
        i += 1
        while i < len(lines) and (not lines[i].strip() or _indent(lines[i]) > dash):
            body.append(lines[i])
            i += 1
        step, k = {}, 0
        key_col = _indent(body[0])
        while k < len(body):
            km = _KEY_RX.match(body[k])
            if not km or _indent(body[k]) != key_col:
                k += 1
                continue
            key, val = km.group(2), km.group(3).strip()
            if len(val) > 1 and val[0] == val[-1] and val[0] in "'\"":
                val = val[1:-1]             # a quoted scalar, not a quote inside a command
            k += 1
            child = []
            while k < len(body) and (not body[k].strip() or _indent(body[k]) > key_col):
                child.append(body[k])
                k += 1
            if key == "with":
                step["with"] = {c.group(2): c.group(3).strip().strip("'\"")
                                for c in (_KEY_RX.match(x) for x in child) if c}
            elif key == "run":
                step["run"] = "\n".join(x.strip() for x in child) if val[:1] in ("|", ">") else val
            else:
                step[key] = val
        steps.append(step)
    return steps


def checkout_path(steps, name, own):
    """Where a workflow checks the repository `name` out (its path, "" for
    the workspace), or None when it does not: its own repository is the
    checkout that names no repository, another one the checkout whose
    repository is <owner>/<name>."""
    for s in steps:
        if not s.get("uses", "").startswith("actions/checkout"):
            continue
        w = s.get("with", {})
        repo = w.get("repository")
        if (repo is None and own) or (repo is not None and repo.rsplit("/", 1)[-1].lower() == name.lower()):
            return posixpath.normpath(w.get("path") or ".")
    return None


_SPLIT_RX = re.compile(r"&&|\|\||[;|\n]|(?:^|(?<=\s))(?:then|do|else)(?=\s|$)")
_PREFIXES = ("sudo", "env", "time", "exec", "command", "nice")
_INTERPRETER_RX = re.compile(r"^(?:python[0-9.]*|bash|sh)$")


def _commands(run):
    """The simple commands of a step's run text, each as its words, with
    what only starts the real command taken off: sudo and its flags, env,
    time, exec, and variable assignments."""
    import shlex
    out = []
    for part in _SPLIT_RX.split(run.replace("\\\n", " ")):
        try:
            words = shlex.split(part)
        except ValueError:
            words = part.split()
        while words:
            if words[0] in _PREFIXES:
                words = words[1:]
                while words and words[0].startswith("-"):
                    words = words[1:]
            elif re.match(r"^[A-Za-z_]\w*=", words[0]):
                words = words[1:]
            else:
                break
        if words:
            out.append(words)
    return out


def _segments_match(path, pattern):
    """A path against a path or a glob, one segment against one segment
    ('*' never crosses a '/')."""
    import fnmatch
    a, b = path.split("/"), pattern.split("/")
    return len(a) == len(b) and all(fnmatch.fnmatchcase(x, y) for x, y in zip(a, b))


def _discovers(args, full, at):
    """Whether `python -m unittest discover ARGS` finds the test: its
    directory is the start directory (-s, default the working directory)
    and its file name matches the pattern (-p, default test*.py)."""
    import fnmatch
    start, pattern = ".", "test*.py"
    for i, a in enumerate(args):
        if a in ("-s", "--start-directory") and i + 1 < len(args):
            start = args[i + 1]
        elif a in ("-p", "--pattern") and i + 1 < len(args):
            pattern = args[i + 1]
    return posixpath.dirname(full) == at(start) and fnmatch.fnmatchcase(posixpath.basename(full), pattern)


def _unittest_names(args, full, at):
    """Whether `python -m unittest ARGS` (not discover) runs the test: an
    argument is its file (tests/test_x.py), or a dotted name whose module
    is its file (tests.test_x, tests.test_x.Case.test_one)."""
    skip = False
    for a in args:
        if skip:
            skip = False
            continue
        if a in ("-k",):
            skip = True
            continue
        if a.startswith("-") or "$" in a:
            continue
        if a.endswith(".py"):
            if at(a) == full:
                return True
            continue
        parts = a.split(".")
        if any(at("/".join(parts[:k]) + ".py") == full for k in range(len(parts), 0, -1)):
            return True
    return False


def _pytest_runs(args, full, at):
    """Whether pytest ARGS collects the test: named, or its directory given
    and its file name one pytest collects (test_*.py, *_test.py)."""
    import fnmatch
    name = posixpath.basename(full)
    collected = fnmatch.fnmatchcase(name, "test_*.py") or fnmatch.fnmatchcase(name, "*_test.py")
    for a in args:
        if a.startswith("-") or "$" in a:
            continue
        if at(a) == full or (collected and at(a) == posixpath.dirname(full)):
            return True
    return False


def workflow_runs(steps, test_path):
    """Whether a step of the workflow runs the test at test_path (its path
    in the workspace: the checkout's path joined to the test's path in its
    repository). Only what a step's run commands execute counts, each path
    resolved against the step's working-directory: the command itself
    (a script run directly, or a C test's built binary, <dir>/<stem>);
    the script an interpreter runs (python, bash, sh), a glob matching the
    whole path; a for loop's list over the test; python -m unittest given
    the test's file or a dotted name whose module is that file
    (tests.test_x, tests.test_x.Case.test_one); unittest discover from the
    test's own directory with a pattern its name matches; pytest on the
    test or its own directory. An argument to anything else (echo, a build,
    an install) does not run the test."""
    full = posixpath.normpath(test_path)
    stem, ext = posixpath.splitext(posixpath.basename(full))
    compiled = ext in (".c", ".cc", ".cpp")
    for s in steps:
        run = s.get("run")
        if not run:
            continue
        wd = s.get("working-directory") or "."

        def at(p):
            return posixpath.normpath(posixpath.join(wd, p))

        def names(p):
            return "$" not in p and _segments_match(full, at(p))
        for words in _commands(run):
            cmd, args = words[0], words[1:]
            if cmd == "for" and "in" in args:
                if any(names(p) for p in args[args.index("in") + 1:]):
                    return True
            elif _INTERPRETER_RX.match(posixpath.basename(cmd)):
                if "-m" in args:
                    i = args.index("-m")
                    mod, rest = (args[i + 1], args[i + 2:]) if i + 1 < len(args) else ("", [])
                    if mod == "unittest" and rest[:1] == ["discover"]:
                        if _discovers(rest[1:], full, at):
                            return True
                    elif (mod == "unittest" and _unittest_names(rest, full, at)) or \
                         (mod == "pytest" and _pytest_runs(rest, full, at)):
                        return True
                else:
                    script = next((a for a in args if not a.startswith("-")), None)
                    if script and names(script):
                        return True
            elif posixpath.basename(cmd) == "pytest":
                if _pytest_runs(args, full, at):
                    return True
            elif names(cmd) or (compiled and "/" in cmd and posixpath.basename(cmd) == stem):
                return True
    return False


def host_row_problems(manifest, host=HOST_TESTED, root=REPO_ROOT, sources=SOURCES):
    """HOST_TESTED rows that prove nothing: a glob that selects no
    behavioral file of its component (the same defect as an empty covers
    entry), a host test or a workflow that does not exist, a workflow that
    cannot be read where it should be, one that does not check out the
    test's repository, or one with no step that runs the test. Whether a
    workflow runs its test is not checked where it cannot be read (see
    read_source). Returns [(component, glob, host test, why)]."""
    out = []
    for comp, pat, ref, ci in host:
        files = manifest.files(comp)
        if files is None or not [p for p, _b in match_files(files, pat) if not non_behavioral(comp, p)]:
            out.append((comp, pat, ref, "selects nothing"))
        why = _ref_problem(manifest, ref, "host test", root) or _ref_problem(manifest, ci, "workflow", root)
        if why:
            out.append((comp, pat, ref, why))
            continue
        text, why = read_source(manifest, ci, root, sources)
        if why:
            out.append((comp, pat, ref, "%s %s" % (ci, why)))
            continue
        if text is None:
            continue
        steps = workflow_steps(text)
        test_repo, _sep, test_path = ref.partition(":")
        prefix = checkout_path(steps, repo_name(manifest, test_repo), test_repo == ci.partition(":")[0])
        if prefix is None:
            out.append((comp, pat, ref, "%s does not check out %s" % (ci, test_repo)))
        elif not workflow_runs(steps, posixpath.join(prefix, test_path)):
            out.append((comp, pat, ref, "%s does not run it" % ci))
    return out


def coverage_report(manifest, tests, allow=NON_BEHAVIORAL, host=HOST_TESTED):
    """Which manifest paths neither a test nor a host test covers.

    tests: iterable with .covers. allow: iterable of (component, glob)
    that need no coverage (docs, CI, licenses...); a BEHAVIORAL path is
    never allowed away, as it is never taken out of a fingerprint. host:
    the HOST_TESTED rows, whose paths count as covered. Returns
    {component: [uncovered paths]} for the non-dev-only components.
    """
    covered = {}
    for t in tests:
        for comp, pat in t.covers:
            covered.setdefault(comp, []).append(glob_to_regex(pat))
    for comp, pat, _ref, _ci in host:
        covered.setdefault(comp, []).append(glob_to_regex(pat))
    allowed = {}
    for comp, pat in allow:
        allowed.setdefault(comp, []).append(glob_to_regex(pat))
    report = {}
    for comp in manifest.component_names():
        if comp in DEV_ONLY_COMPONENTS:
            continue
        rxs = covered.get(comp, [])
        free = allowed.get(comp, []) + allowed.get("*", [])
        behavior = [glob_to_regex(pat) for c, pat in BEHAVIORAL if c == comp]
        missing = []
        for p, _b in manifest.files(comp):
            if any(rx.match(p) for rx in rxs):
                continue
            if any(rx.match(p) for rx in free) and not any(rx.match(p) for rx in behavior):
                continue
            missing.append(p)
        if missing:
            report[comp] = sorted(missing)
    return report
