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
# must run the host test: on a push or a pull_request with no branches,
# tags or paths filter under it (one of the two is enough), in a step
# that runs on every run of the workflow and whose failure fails it (no
# `if:` other than `true` and no `continue-on-error:` other than `false`
# on the step or its job, every job the job's `needs:` names one that
# runs on every run, and no key with one stray quote where the lint
# reads the workflow's keys), under bash or sh, on a line of the step's
# run text whose failure ends the step (the command alone on its line,
# or in a pipeline under pipefail, with errexit set and no line before
# it that ends the step or opens a compound command: workflow_runs), and
# name it, or discover it (a glob, unittest discover, pytest on its
# directory). A host-tested path counts as covered in the coverage lint,
# and like every path no test covers it is in no fingerprint: a change
# there makes no acceptance run necessary. A path a catalog test
# exercises on the hardware belongs in that test's covers, not here.
HOST_TESTED = [
    # An extension package's holds as the engine reads them (forgeext's
    # holdkeep_test and run_test prove the writer's side).
    ("forgectrl", "src/holds.*", "forgectrl:tests/holds_test.c", "forgectrl:.github/workflows/build.yml"),
    # The M-code relay to the package that answers (the controller's side is
    # the null-sink mcode_test harness, under grblhal-glowforge's src/**).
    ("forgectrl", "src/mcode.*", "forgectrl:tests/mcode_test.c", "forgectrl:.github/workflows/build.yml"),
    # The digest primitives (SHA-256, HMAC, the constant-time compare) that
    # the advisories' hashes, the tokens and the camera key are made with.
    ("forgectrl", "src/sha256.*", "forgectrl:tests/sha256_test.c", "forgectrl:.github/workflows/build.yml"),
    # The built-in extensions' table: the cloud entry, its refusals while it
    # is off, the sweep when it goes off, the list.
    ("forgectrl", "src/builtin.*", "forgectrl:tests/builtin_test.c", "forgectrl:.github/workflows/build.yml"),
    # The setup record as a printable page: no script, the sheet id, a
    # heading for each completed step, everything escaped.
    ("forgectrl", "src/recordhtml.*", "forgectrl:tests/recordhtml_test.c", "forgectrl:.github/workflows/build.yml"),
    # An extension package's claim on the Grbl sender as the machine keeps
    # it: the claim and its refusals, its end by the package, the operator
    # or a stale keeper (forgeext's run_test proves the writer's side).
    ("forgectrl", "src/senderout.*", "forgectrl:tests/senderout_test.c", "forgectrl:.github/workflows/build.yml"),
    # A package's check on the Setup page: each step held to its form, the
    # check run through the host, the result the package's own (the routes
    # against a live host's stand-in are tests/host_ext.sh).
    ("forgectrl", "src/wizpkg.*", "forgectrl:tests/wizpkg_test.c", "forgectrl:.github/workflows/build.yml"),
    # The camera key: made, kept, checked and rotated (the reads it opens
    # from the LAN with the reads closed are tests/host_lan.sh).
    ("forgectrl", "src/camkey.*", "forgectrl:tests/camkey_test.c", "forgectrl:.github/workflows/build.yml"),
    # The log export's sanitizer: every identifying class taken out, the
    # ordinary log text left as it was (the export's routes and the
    # installer's directory on a host are tests/host_guard.sh).
    ("forgectrl", "src/sanitize.*", "forgectrl:tests/sanitize_test.c", "forgectrl:.github/workflows/build.yml"),
    # The package routes' relay to the extension host: the argument vector,
    # the closed list of actions, the host's words, the catalog's fetch (the
    # routes against a live host's stand-in are tests/host_ext.sh).
    ("forgectrl", "src/extpkg.*", "forgectrl:tests/extpkg_test.c", "forgectrl:.github/workflows/build.yml"),
    # The event stream: a button edge, a lease changing hands, telemetry
    # (the live order, the take before the free, is tests/host_lease.sh).
    ("forgectrl", "src/events.*", "forgectrl:tests/events_test.c", "forgectrl:.github/workflows/build.yml"),
    # The crumb tray's mode as the daemon keeps it: its offset and what it
    # refuses (the route and the panel's switch are tests/host_routes.sh).
    ("forgectrl", "src/tray.*", "forgectrl:tests/tray_test.c", "forgectrl:.github/workflows/build.yml"),
    # The product gate on the firmware paths: firmware only, an extension
    # package refused in words that say what it is (the upload route over
    # a stand-in fwup is tests/host_update.sh).
    ("forgectrl", "src/fwproduct.*", "forgectrl:tests/fwproduct_test.c", "forgectrl:.github/workflows/build.yml"),
    # The controller port's client and its two operation sets (the motion
    # routes and a package's jog through the host's credential are
    # tests/host_routes.sh).
    ("forgectrl", "src/grblport.*", "forgectrl:tests/grblport_test.c", "forgectrl:.github/workflows/build.yml"),
    # The panel page as the daemon serves it: bundled, the theme and the
    # save bar in it, and its policy frame-src 'none'.
    ("forgectrl", "src/ui.h", "forgectrl:tests/host_routes.sh", "forgectrl:.github/workflows/build.yml"),
    # The data and run directories from the environment: the SSH flag in the
    # run directory, written and removed by POST /system/ssh.
    ("forgectrl", "src/paths.h", "forgectrl:tests/host_routes.sh", "forgectrl:.github/workflows/build.yml"),
    # The first run in the page's order: the four documents at their hashes,
    # the press step breathing teal, the press taken as the acceptance, the
    # form steps through main.c's hooks; and the on-demand Extensions
    # advisory, accepted at its hash with the typed phrase.
    ("forgectrl", "src/advisories.*", "forgectrl:tests/host_setup.sh", "forgectrl:.github/workflows/build.yml"),
    ("forgectrl", "docs/advisories/extensions.md", "forgectrl:tests/host_setup.sh",
     "forgectrl:.github/workflows/build.yml"),
    ("forgectrl", "docs/advisories/safety-and-risk.md", "forgectrl:tests/host_setup.sh",
     "forgectrl:.github/workflows/build.yml"),
    ("forgectrl", "docs/advisories/licenses.md", "forgectrl:tests/host_setup.sh", "forgectrl:.github/workflows/build.yml"),
    ("forgectrl", "docs/advisories/privacy.md", "forgectrl:tests/host_setup.sh", "forgectrl:.github/workflows/build.yml"),
    ("forgectrl", "docs/advisories/cloud-service.md", "forgectrl:tests/host_setup.sh",
     "forgectrl:.github/workflows/build.yml"),
    ("forgectrl", "src/hooks.h", "forgectrl:tests/host_setup.sh", "forgectrl:.github/workflows/build.yml"),
    # The setup sheet id from the fuse serial, against golden ids.
    ("forgectrl", "src/sheetid.*", "forgectrl:tests/sheetid_test.c", "forgectrl:.github/workflows/build.yml"),
    # The logging path: the image's rsyslog.conf and the rendered rules, each
    # logger in its own file at its level, a stray tag in system/, the kern
    # facility in kernel/.
    ("forgectrl", "src/fflog.*", "forgectrl:tests/fflog_e2e.sh", "forgectrl:.github/workflows/build.yml"),
    # POST /job's multipart reader: the form's parts reach the runner's
    # start; a missing name, a $ line and a malformed boundary are refused
    # with nothing started.
    ("forgectrl", "src/jobpost.*", "forgectrl:tests/jobpost_test.c", "forgectrl:.github/workflows/build.yml"),
    # The scoped tokens: the closed capability list, the judgment, the
    # store that holds only hashes, and the extension host's credential
    # (mode 0600, exactly the host's capabilities).
    ("forgectrl", "src/tokens.*", "forgectrl:tests/tokens_test.c", "forgectrl:.github/workflows/build.yml"),
    # The logging tree's paths, the level and target validators, the
    # configured levels.
    ("forgectrl", "src/logs.*", "forgectrl:tests/logs_test.c", "forgectrl:.github/workflows/build.yml"),
    # The extension host, each part to the test that drives it (the
    # freeze, the supervisor, the machine client and the groups stay with
    # exthost.armed-freeze, which covers them).
    ("forgeext", "src/api.*", "forgeext:tests/api_test.c", "forgeext:.github/workflows/build.yml"),
    ("forgeext", "src/caps.*", "forgeext:tests/caps_test.c", "forgeext:.github/workflows/build.yml"),
    ("forgeext", "src/evfeed.*", "forgeext:tests/evfeed_test.c", "forgeext:.github/workflows/build.yml"),
    ("forgeext", "src/holdkeep.*", "forgeext:tests/holdkeep_test.c", "forgeext:.github/workflows/build.yml"),
    ("forgeext", "src/httpreq.*", "forgeext:tests/httpreq_test.c", "forgeext:.github/workflows/build.yml"),
    ("forgeext", "src/manifest.*", "forgeext:tests/manifest_test.c", "forgeext:.github/workflows/build.yml"),
    ("forgeext", "src/quota.*", "forgeext:tests/quota_test.c", "forgeext:.github/workflows/build.yml"),
    ("forgeext", "src/sandbox.*", "forgeext:tests/sandbox_test.c", "forgeext:.github/workflows/build.yml"),
    ("forgeext", "src/settings.*", "forgeext:tests/settings_test.c", "forgeext:.github/workflows/build.yml"),
    ("forgeext", "src/state.*", "forgeext:tests/state_test.c", "forgeext:.github/workflows/build.yml"),
    ("forgeext", "src/netrules.*", "forgeext:tests/netrules_test.py", "forgeext:.github/workflows/build.yml"),
    # A package's page asking its own service (forgeext call).
    ("forgeext", "src/call.*", "forgeext:tests/sdk_test.py", "forgeext:.github/workflows/build.yml"),
    # Archives in and refused, and the signed index.
    ("forgeext", "src/install.*", "forgeext:tests/install_test.py", "forgeext:.github/workflows/build.yml"),
    ("forgeext", "src/pkg.*", "forgeext:tests/install_test.py", "forgeext:.github/workflows/build.yml"),
    ("forgeext", "src/index.*", "forgeext:tests/install_test.py", "forgeext:.github/workflows/build.yml"),
    # The daemon as built, end to end: its command line, its log, the
    # killed host under the init script's wrapper.
    ("forgeext", "src/main.c", "forgeext:tests/run_test.py", "forgeext:.github/workflows/build.yml"),
    ("forgeext", "src/fflog.*", "forgeext:tests/run_test.py", "forgeext:.github/workflows/build.yml"),
    ("forgeext", "CMakeLists.txt", "forgeext:tests/run_test.py", "forgeext:.github/workflows/build.yml"),
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
# A mapping key, plain or in a matched pair of quotes ("if": is the same
# key as if:).
_KEY_RX = re.compile(r"""^(\s*)(?=(?:"[\w-]+"|'[\w-]+'|[\w-]+)\s*:)["']?([\w-]+)["']?\s*:\s*(.*)$""")
# A key with one stray quote (if": or "if:), which _KEY_RX does not read:
# YAML reads a key with the quote in its name, which no workflow key and
# no event has, or refuses the file.
_STRAY_RX = re.compile(r"""^\s*["']?[\w-]+["']?\s*:""")
_ON_RX = re.compile(r"""^(?:on|"on"|'on')\s*:\s*(.*)$""")
# What a job or a step says about whether it runs on every run of the
# workflow, and whether its failure fails the run.
_CONDITION_KEYS = ("if", "continue-on-error")


def _indent(line):
    return len(line) - len(line.lstrip(" "))


def _stray_key(line):
    """Whether a line is a mapping key with one stray quote."""
    return _KEY_RX.match(line) is None and _STRAY_RX.match(line) is not None


def _lines(text):
    """A workflow's lines with the comments dropped: whole comment lines,
    and from ' #' to the end of a line."""
    out = []
    for ln in text.splitlines():
        if ln.lstrip().startswith("#"):
            continue
        out.append(re.sub(r"\s+#.*$", "", ln).rstrip())
    return out


def _scalar(val):
    """A key's value, a quoted scalar without its quotes (a quote inside a
    command stays)."""
    val = val.strip()
    if len(val) > 1 and val[0] == val[-1] and val[0] in "'\"":
        return val[1:-1]
    return val


def workflow_triggers(text):
    """The events a workflow runs on: the keys of its `on:` mapping, the
    items of its list (`on: [push, pull_request]`, or one per line), or
    its one scalar (`on: push`). What is under an event (its branches,
    its paths, a dispatch's inputs) is not an event (workflow_filters
    reads the filters). A flow mapping (`on: {push: ...}`) is not read,
    and names no event. An event key with one stray quote (`push":`) is
    no event, and the workflow then reads as one that runs on none."""
    lines = _lines(text)
    for i, ln in enumerate(lines):
        m = _ON_RX.match(ln) if not _indent(ln) else None
        if not m:
            continue
        val = m.group(1).strip()
        if val.startswith("["):
            return {_scalar(x) for x in val.strip("[]").split(",") if x.strip()}
        if val:
            return set() if val.startswith("{") else {_scalar(val)}
        out, at = set(), None
        for child in lines[i + 1:]:
            if not child.strip():
                continue
            ind = _indent(child)
            if ind == 0:
                break
            at = ind if at is None else at
            if ind != at:
                continue
            body = child.strip()
            if body.startswith("- "):
                out.add(_scalar(body[2:]))
            elif _stray_key(child):
                return set()
            else:
                km = _KEY_RX.match(child)
                if km:
                    out.add(km.group(2))
        return out
    return set()


# What under a push or a pull_request of `on:` keeps the workflow from a
# run: a ref or a path the event does not match.
_FILTER_KEYS = ("branches", "branches-ignore", "tags", "tags-ignore", "paths", "paths-ignore")


def workflow_filters(text):
    """The events of a workflow's `on:` block mapping that carry a filter,
    {event: [what]}: each `branches:`, `branches-ignore:`, `tags:`,
    `tags-ignore:`, `paths:` or `paths-ignore:` key under the event, by
    name; a value on the event's own line (`push: {branches: [main]}`),
    which the lint does not read, as "a value the lint does not read";
    and a key with one stray quote under the event (`paths":`), as "a
    key the lint does not read". Every filter counts: a push with only
    a branches or a tags filter runs on no push of the other kind of
    ref, and a branch it names is one the lint does not know, as the
    manifest carries no branch. A key deeper than the event's own level
    (a dispatch's inputs) is no filter. An event of a list or a scalar
    `on:` carries none."""
    lines = _lines(text)
    out = {}
    for i, ln in enumerate(lines):
        m = _ON_RX.match(ln) if not _indent(ln) else None
        if not m:
            continue
        if m.group(1).strip():
            return out
        at, event, sub = None, None, None
        for child in lines[i + 1:]:
            if not child.strip():
                continue
            ind = _indent(child)
            if ind == 0:
                break
            at = ind if at is None else at
            km = _KEY_RX.match(child)
            if ind == at:
                event, sub = None, None
                if km:
                    event = km.group(2)
                    if km.group(3).strip():
                        out.setdefault(event, []).append("a value the lint does not read")
            elif event is not None and ind > at:
                sub = ind if sub is None else sub
                if ind != sub:
                    continue
                if _stray_key(child):
                    out.setdefault(event, []).append("a key the lint does not read")
                elif km and km.group(2) in _FILTER_KEYS:
                    out.setdefault(event, []).append(km.group(2))
        return out
    return out


def _needs(lines, i):
    """The job names the `needs:` key at lines[i] names: a flow list
    (`[build, lint]`), one scalar, or a block list on the lines under
    it. None for a form the lint does not read (a line under the key
    that is no list item): a need no job meets."""
    val = _KEY_RX.match(lines[i]).group(3).strip()
    if val.startswith("["):
        return [_scalar(x) for x in val.strip("[]").split(",") if x.strip()]
    if val:
        return [_scalar(val)]
    out = []
    for ln in lines[i + 1:]:
        if not ln.strip():
            continue
        if _indent(ln) <= _indent(lines[i]):
            break
        body = ln.strip()
        if not body.startswith("- "):
            return None
        out.append(_scalar(body[2:]))
    return out


def _needs_run(jobs):
    """Whether each job's `needs:` are met on every run of the workflow,
    as "needs_run" on the job's dict: True when every job it needs runs
    on every run (no `if:` other than the literal true, and its own
    needs met, transitively), False when one does not, names no job of
    the workflow, or is in a cycle with it, or when the `needs:` is a
    form the lint does not read. The lint evaluates no condition, so a
    job under `always()` counts as no job, as its steps do. A needed
    job's `continue-on-error:` does not bear: its failure fails neither
    the run nor the jobs after it."""
    state = {}

    def runs(name, trail):
        if name in state:
            return state[name]
        job = jobs.get(name)
        if job is None or name in trail:
            return False
        needs = job.get("needs", [])
        met = needs is not None and all(runs(n, trail | {name}) for n in needs)
        job["needs_run"] = met
        state[name] = met and job.get("if", "true").lower() == "true"
        return state[name]
    for name in jobs:
        runs(name, frozenset())


def _defaults(lines, i):
    """{"shell": its value} for the `defaults:` key at lines[i] when it
    gives the run steps a shell:, {} when it gives none. A flow mapping
    (at the key or at its run:) and a key with one stray quote under it
    give None: a shell the lint does not read."""
    if _KEY_RX.match(lines[i]).group(3).strip():
        return {"shell": None}
    out = {}
    for ln in lines[i + 1:]:
        if not ln.strip():
            continue
        if _indent(ln) <= _indent(lines[i]):
            break
        km = _KEY_RX.match(ln)
        if _stray_key(ln) or (km and km.group(2) == "run" and km.group(3).strip()):
            return {"shell": None}
        if km and km.group(2) == "shell":
            out["shell"] = _scalar(km.group(3))
    return out


def workflow_steps(text):
    """A workflow's steps, each a dict of its keys: "uses", "run" (the
    command text: a literal block's lines one per line; a folded block,
    or a scalar continued on the lines under its key, as one line),
    "working-directory", "with" (a dict), "shell", "if" and
    "continue-on-error" as written; "job": the "if" and
    "continue-on-error" of the job the step is in, when it is in one,
    its "needs" (_needs) and whether they are met on every run
    ("needs_run", _needs_run), and the "shell" its `defaults:` give; and
    "workflow": the "shell" the
    workflow's own `defaults:` give, when they give one (_defaults). YAML
    read by its indentation, as far as these workflows use it; comments,
    whole lines and from ' #' to the end of a line, are not read.
    Anything outside a step (a trigger's paths, a job's name) is not a
    step. A key with one stray quote (`if":`, `"run:`) at the top level,
    at a job's name or a job's key, at a step's key, or as the first key of
    a list item outside a step (a step's first key is on its dash line,
    `- run": make`; a matrix entry is such an item; a list inside a step's
    body is not read) is no key the lint reads: every step of the workflow
    then carries "unread" and counts as no step."""
    lines = _lines(text)
    steps, i = [], 0
    in_jobs, job_at, key_at, job, jobs = False, None, None, None, {}
    stray, workflow = False, {}
    while i < len(lines):
        km = _KEY_RX.match(lines[i])
        if km:                      # a mapping key: which job this is, and what the job itself says
            ind, key = _indent(lines[i]), km.group(2)
            if ind == 0:
                in_jobs, job_at, key_at, job = key == "jobs", None, None, None
                if key == "defaults":
                    workflow.update(_defaults(lines, i))
            elif in_jobs and (job_at is None or ind == job_at):
                job_at, key_at, job = ind, None, {}
                jobs[key] = job
            elif in_jobs and job_at is not None and ind > job_at and (key_at is None or ind == key_at):
                key_at = ind
                if key in _CONDITION_KEYS:
                    job[key] = _scalar(km.group(3))
                elif key == "needs":
                    job["needs"] = _needs(lines, i)
                elif key == "defaults":
                    job.update(_defaults(lines, i))
        elif _stray_key(lines[i]):
            ind = _indent(lines[i])
            stray = stray or ind == 0 or (in_jobs and (job_at is None or ind == job_at or (
                ind > job_at and (key_at is None or ind == key_at))))
        # A list item's first key is on its dash line, where _STEP_RX reads no
        # key with a stray quote: the dash and the spaces after it become spaces.
        item = re.match(r"^\s*-\s+", lines[i])
        if item and _stray_key(" " * item.end() + lines[i][item.end():]):
            stray = True
        m = _STEP_RX.match(lines[i])
        if not m:
            i += 1
            continue
        dash = len(m.group(1))
        # The dash becomes a space, nothing realigned: the first key stays at
        # the column the step's other keys use, however many spaces follow it.
        body = [" " * (dash + 1) + lines[i][dash + 1:]]
        i += 1
        while i < len(lines) and (not lines[i].strip() or _indent(lines[i]) > dash):
            body.append(lines[i])
            i += 1
        step, k = {}, 0
        key_col = _indent(body[0])
        while k < len(body):
            km = _KEY_RX.match(body[k])
            if not km or _indent(body[k]) != key_col:
                stray = stray or (_indent(body[k]) == key_col and _stray_key(body[k]))
                k += 1
                continue
            key, val = km.group(2), _scalar(km.group(3))
            k += 1
            child = []
            while k < len(body) and (not body[k].strip() or _indent(body[k]) > key_col):
                child.append(body[k])
                k += 1
            if key == "with":
                step["with"] = {c.group(2): c.group(3).strip().strip("'\"")
                                for c in (_KEY_RX.match(x) for x in child) if c}
            elif key == "run":
                # YAML folds a folded block, and a plain or quoted scalar continued on the
                # lines under its key, into one line where no blank or more indented line
                # breaks it. Read as one line: where YAML keeps a break, the joined line
                # counts no command that the shell's own lines would not.
                text = [x.strip() for x in child if x.strip()]
                if val[:1] == "|":
                    step["run"] = "\n".join(x.strip() for x in child)
                elif val[:1] == ">":
                    step["run"] = " ".join(text)
                else:
                    step["run"] = _scalar(" ".join([km.group(3).strip()] + text))
            else:
                step[key] = val
        if job is not None:
            step["job"] = job       # the job's own dict: a job key written after its steps counts too
        steps.append(step)
    _needs_run(jobs)
    for s in steps:
        if stray:
            s["unread"] = True
        if workflow:
            s["workflow"] = workflow
    return steps


def _on_every_run(step):
    """Whether the lint counts a step as one that runs on every run of
    its workflow and fails the run when it fails: neither the step nor
    its job carries an `if:` other than the literal `true` or a
    `continue-on-error:` other than the literal `false`, the job's
    `needs:` are met on every run (_needs_run: GitHub skips a job whose
    needed job did not run, unless the job says `always()`, which the
    lint does not evaluate), and the workflow has no key the lint
    cannot read ("unread"). The lint evaluates no condition, so a step
    under `always()` or `success()` counts as no step too."""
    if step.get("unread"):
        return False
    job = step.get("job") or {}
    if not job.get("needs_run", True):
        return False
    for d in (step, job):
        if d.get("if", "true").lower() != "true" or d.get("continue-on-error", "false").lower() != "false":
            return False
    return True


def checkout_path(steps, name, own):
    """Where a workflow checks the repository `name` out (its path, "" for
    the workspace), or None when it does not: its own repository is the
    checkout that names no repository, another one the checkout whose
    repository is <owner>/<name>."""
    for s in steps:
        if not _on_every_run(s) or not s.get("uses", "").startswith("actions/checkout"):
            continue
        w = s.get("with", {})
        repo = w.get("repository")
        if (repo is None and own) or (repo is not None and repo.rsplit("/", 1)[-1].lower() == name.lower()):
            return posixpath.normpath(w.get("path") or ".")
    return None


# What only starts the real command, and the flags with which each one
# runs it. Any other flag (`command -v` and `sudo -l` print the command)
# leaves the prefix as the command.
_PREFIXES = {"sudo": ("-E", "-H", "-n"), "env": ("-i", "-"), "time": ("-p",), "command": ("-p",), "nice": ()}
_INTERPRETER_RX = re.compile(r"^(?:python[0-9.]*|bash|sh)$")
# The shell's operators, the longest first. A redirection keeps a command
# one command, and a pipe joins commands into a pipeline; any other
# operator makes a list of commands, puts one in the background, or opens
# a subshell or a here-document.
_OPERATORS = ("&>>", "&&", "||", ";;", ";&", "|&", ">>", "<<", ">&", "<&", "<>", ">|", "&>",
              "|", "&", ";", "(", ")", "<", ">")
_REDIRECTIONS = frozenset((">", ">>", "<", ">&", "<&", "<>", ">|", "&>", "&>>"))
# A command word that opens or closes a compound command (an if, a case,
# a loop, a group, a function) or negates a status.
_RESERVED = frozenset(("if", "then", "elif", "else", "fi", "case", "esac", "for", "select", "while",
                       "until", "do", "done", "function", "coproc", "{", "}", "[[", "]]", "!"))
# A command after which the lint cannot tell what a later line runs, or
# whether a failure still ends the step.
_STOPS = frozenset(("exit", "return", "exec", "trap", "eval", "source", ".", "alias", "shopt",
                    "enable", "builtin", "hash", "break", "continue", "logout"))
# The `set` options the lint follows (errexit, pipefail) or knows to
# change neither; a `set` of any other option ends what it reads.
_SET_FLAGS = {"e": "errexit", "u": "nounset", "x": "xtrace", "v": "verbose"}
_SET_NAMES = frozenset(("errexit", "pipefail", "nounset", "xtrace", "verbose"))


def _part_end(line, i):
    """Where the part of a shell word that starts at line[i] ends: a
    quoted string, an escaped character, an expansion ($(...), ${...},
    `...`), or one character; -1 when it does not close on the line."""
    n, c = len(line), line[i]
    if c == "\\":
        return i + 2 if i + 1 < n else -1
    if c == "'":
        j = line.find("'", i + 1)
        return j + 1 if j > 0 else -1
    if c in "\"`":
        j = i + 1
        while j < n and line[j] != c:
            j = _part_end(line, j) if line[j] in "\\$" or (c == '"' and line[j] == "`") else j + 1
            if j < 0:
                return -1
        return j + 1 if j < n else -1
    if c == "$" and line[i + 1:i + 2] in ("(", "{"):
        opener, depth, j = line[i + 1], 0, i + 1
        while j < n:
            if line[j] == opener:
                depth, j = depth + 1, j + 1
            elif line[j] == (")" if opener == "(" else "}"):
                depth, j = depth - 1, j + 1
                if not depth:
                    return j
            elif line[j] in "\\'\"`$":
                j = _part_end(line, j)
                if j < 0:
                    return -1
            else:
                j += 1
        return -1
    return i + 1


def _shell_words(line):
    """One line of shell as the lint reads it: [(True, word) or (False,
    operator)], a word with its quotes taken off and its expansions kept
    as written; None when a quote or an expansion does not close on the
    line."""
    out, word, i = [], None, 0
    while i < len(line):
        c = line[i]
        if c in " \t|&;()<>":
            if word is not None:
                out.append((True, word))
                word = None
            if c in " \t":
                i += 1
            else:
                op = next(o for o in _OPERATORS if line.startswith(o, i))
                out.append((False, op))
                i += len(op)
            continue
        j = _part_end(line, i)
        if j < 0:
            return None
        part = line[i:j]
        if part[0] in "'\"":
            part = part[1:-1]
        elif part[0] == "\\":
            part = part[1:]
        word = (word or "") + part
        i = j
    if word is not None:
        out.append((True, word))
    return out


def _command(words):
    """A simple command's words with what only starts the real command
    taken off: sudo, env, time, command and nice with the flags with which
    they run it (_PREFIXES), and variable assignments."""
    while words:
        if words[0] in _PREFIXES:
            rest = words[1:]
            while rest and rest[0] in _PREFIXES[words[0]]:
                rest = rest[1:]
            if rest[:1] and rest[0].startswith("-"):
                break
            words = rest
        elif re.match(r"^[A-Za-z_]\w*=", words[0]):
            words = words[1:]
        else:
            break
    return words


def _line_commands(tokens):
    """The commands of a line of shell (_shell_words) that is one simple
    command, or a pipeline of them, and nothing else, each as its words
    after _command, with its redirections taken off. None for any other
    line: one that does not close, holds another operator (a list, a
    background job, a subshell, a here-document) or a pipe at its end, or
    has a command that is a reserved word or one of _STOPS."""
    if tokens is None:
        return None
    cmds, words, k = [], [], 0
    while k < len(tokens):
        is_word, text = tokens[k]
        if is_word:
            words.append(text)
        elif text in _REDIRECTIONS and k + 1 < len(tokens) and tokens[k + 1][0]:
            k += 1                      # the redirection's target
        elif text == "|" and words:
            cmds.append(words)
            words = []
        else:
            return None
        k += 1
    if cmds and not words:
        return None
    cmds = [_command(c) for c in cmds + [words]]
    return None if any(c[:1] and (c[0] in _RESERVED or c[0] in _STOPS) for c in cmds) else cmds


def _for_header(tokens):
    """The words of a line `for <name> in <words>; do` without its `; do`,
    None for any other line."""
    if (tokens and len(tokens) >= 5 and tokens[0] == (True, "for") and tokens[2] == (True, "in")
            and tokens[-2:] == [(False, ";"), (True, "do")] and all(w for w, _t in tokens[:-2])):
        return [t for _w, t in tokens[:-2]]
    return None


def _set(args, opts):
    """Follow in opts the errexit and pipefail that a `set` command's
    arguments set or clear. False when they name an option the lint does
    not follow (noexec, onecmd, the listing of `set -o`)."""
    k = 0
    while k < len(args):
        a, k = args[k], k + 1
        if a in ("-", "--") or len(a) < 2 or a[0] not in "-+":
            return True                 # the positional parameters follow
        for ch in a[1:]:
            if ch == "o":
                name, k = (args[k] if k < len(args) else ""), k + 1
            else:
                name = _SET_FLAGS.get(ch)
            if name not in _SET_NAMES:
                return False
            if name in opts:
                opts[name] = a[0] == "-"
    return True


def _shell(step):
    """The shell a step's run text runs under: the step's own shell:, else
    its job's `defaults: run: shell:`, else the workflow's; "" for none,
    which is bash -e (sh -e with no bash) on a Linux or macOS runner (the
    lint does not read runs-on). None when any one of them is other than
    bash or sh: the lint reads the text of no other shell."""
    given = [d["shell"] for d in (step, step.get("job") or {}, step.get("workflow") or {}) if "shell" in d]
    if any(g not in ("bash", "sh") for g in given):
        return None
    return given[0] if given else ""


def _run_commands(run, shell):
    """The commands of a step's run text that the lint counts, each as its
    words (_line_commands): see workflow_runs."""
    opts = {"errexit": True, "pipefail": shell == "bash"}
    lines = [ln.strip() for ln in run.replace("\\\n", " ").split("\n") if ln.strip()]
    out, k = [], 0
    while k < len(lines):
        tokens = _shell_words(lines[k])
        k += 1
        header = _for_header(tokens)
        if header is not None:          # the loop is read whole: its header, its body, its done
            groups = [[header]]
            while k < len(lines) and lines[k] != "done":
                cmds = _line_commands(_shell_words(lines[k]))
                if (cmds is None or any(c[:1] == ["set"] for c in cmds)
                        or (len(cmds) > 1 and not opts["pipefail"])):
                    return out
                groups.append(cmds)
                k += 1
            if k == len(lines):
                return out
            k += 1
        else:
            cmds = _line_commands(tokens)
            if cmds is None:
                return out
            if any(c[:1] == ["set"] for c in cmds):
                if len(cmds) > 1 or not _set(cmds[0][1:], opts):
                    return out
                continue
            groups = [cmds]
        if opts["errexit"]:
            out.extend(c for cmds in groups if len(cmds) == 1 or opts["pipefail"] for c in cmds if c)
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
    repository) so that a failure of the test fails the step.

    Only a step that _on_every_run accepts counts: the lint evaluates no
    condition, so a step under an `if:` other than the literal true, or
    with a `continue-on-error:` other than the literal false, on the step
    or on its job, counts as no step. So does a step whose shell is other
    than bash or sh (_shell: its own shell:, or a `defaults: run: shell:`
    of its job or of the workflow).

    The lint reads a step's run text line by line as bash -e or sh -e
    runs it, a backslash at the end of a line joining the next one
    (_run_commands). A command counts on a line that holds it alone, with
    its redirections, or in a pipeline under pipefail (shell: bash, or a
    `set -o pipefail` on an earlier line), and only while errexit holds
    (`set +e` clears it). A pipeline with no pipefail does not count. The
    lint stops at a line that holds any other operator (`||`, `&&`, `;`,
    `&`, a subshell, a here-document), whose command opens a compound
    command (if, case, while, until, a group, a function) or negates a
    status (!), whose command is one of _STOPS (exit, exec, trap, eval,
    source and the like), or that sets an option it does not follow: no
    command on that line or after it counts. A for loop counts in one
    form: `for <name> in <words>; do` on its own line, body lines that
    count, and `done` on its own line.

    Each path is resolved against the step's working-directory. A command
    runs the test when it is the test (a script run directly, or a C
    test's built binary, <dir>/<stem>); the script an interpreter runs
    (python, bash, sh), a glob matching the whole path; a for loop's list
    over the test; python -m unittest given the test's file or a dotted
    name whose module is that file (tests.test_x,
    tests.test_x.Case.test_one); unittest discover from the test's own
    directory with a pattern its name matches; pytest on the test or its
    own directory. An argument to anything else (echo, a build, an
    install) does not run the test."""
    full = posixpath.normpath(test_path)
    stem, ext = posixpath.splitext(posixpath.basename(full))
    compiled = ext in (".c", ".cc", ".cpp")
    for s in steps:
        run, shell = s.get("run"), _shell(s)
        if not run or shell is None or not _on_every_run(s):
            continue
        wd = s.get("working-directory") or "."

        def at(p):
            return posixpath.normpath(posixpath.join(wd, p))

        def names(p):
            return "$" not in p and _segments_match(full, at(p))
        for words in _run_commands(run, shell):
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
    cannot be read where it should be, one whose own `on:` names neither
    push nor pull_request as the lint reads it (workflow_triggers: a
    dispatch, a call or a schedule alone, a call not followed to the
    workflow that calls it, and a flow mapping, which names no event),
    one whose every push and pull_request carries a filter
    (workflow_filters: a branches, tags or paths filter, which the lint
    does not read, so the workflow runs on not every change of the
    covered files, or on not every branch; one push or pull_request
    with no filter makes the workflow run on every change),
    one that does not check out the test's repository, or one with no
    step that runs the test on every run (workflow_runs). Whether a
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
        events = workflow_triggers(text) & {"push", "pull_request"}
        if not events:
            out.append((comp, pat, ref, "%s: the lint reads no push or pull_request in its own on:" % ci))
            continue
        filters = workflow_filters(text)
        if all(e in filters for e in events):
            out.append((comp, pat, ref, "%s: every push or pull_request in its own on: carries a filter (%s)" % (
                ci, "; ".join("%s: %s" % (e, ", ".join(filters[e])) for e in sorted(events)))))
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
