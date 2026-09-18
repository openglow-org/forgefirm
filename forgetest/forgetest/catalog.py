# Copyright 2026 514 LLC d/b/a OpenGlow
# Written by Scott Wiederhold
# https://community.openglow.org
# SPDX-License-Identifier:    MIT

"""The acceptance catalog: test definitions and the registry.

A test is a function decorated with @test(...). The decorator records
what the release gate needs to know without running anything: the id,
the subsystem, the kind (auto / operator / live), how it takes the
hardware (api / takeover), the controller mode it needs (if any), what
source it covers, what it requires, and whether it belongs to the
always-required core. Two more fields describe the operator's part
without affecting the gate: `actions`, the machine actions the test asks
for by name (the page lists them before a start; a bench actuator can
perform them), and `precheck`, a condition the machine must meet for the
test to start at all (a reason string refuses the start, the way an
unmet prerequisite does, and records no result). The function body runs
under the runner with a Context (log, prompts, evidence, hardware
helpers) and reports by returning normally (PASS) or raising
runner.Failed (FAIL).
"""
import ast
import hashlib
import inspect
import os
import re
import threading

from . import manifest as _manifest

KINDS = ("auto", "operator", "live")
HARDWARE = ("api", "takeover")
MODES = ("grbl", "cloud")
# The machine actions a test may ask of the operator (Context.act):
# the lid, the remote-interlock loop, and the big button. Everything a
# test needs done to the machine is one of these, so a bench actuator
# that covers a channel can stand in for the hands on it.
ACTIONS = ("lid", "interlock", "button")
_ID_RX = re.compile(r"^[a-z][a-z0-9-]*\.[a-z][a-z0-9-]*$")

REGISTRY = {}


class Test:
    def __init__(self, id, title, subsystem, kind, hardware, covers, requires,
                 always, est_min, steps, description, fn, mode=None, actions=(),
                 precheck=None, hands=()):
        self.id = id
        self.title = title
        self.subsystem = subsystem
        self.kind = kind
        self.hardware = hardware
        self.mode = mode
        self.covers = tuple((str(c), str(g)) for c, g in covers)
        self.requires = tuple(requires)
        self.always = bool(always)
        self.est_min = est_min
        self.steps = tuple(steps)
        self.actions = tuple(actions)
        self.hands = tuple(hands)
        self.precheck = precheck
        self.description = description or (fn.__doc__ or "").strip()
        self.fn = fn
        self._source_sha = None
        self._fp = (None, None)   # (manifest content sha, fingerprint)

    @property
    def source_sha(self):
        """The hash of the test's own implementation: its function (the
        decorator included) together with the code its module shares
        among its tests - everything outside the @test functions. Part
        of the fingerprint: a change inside one test's body invalidates
        that test's earlier passes and no other; a change to a helper
        invalidates the tests of that module. A test the module does not
        define in the @test form hashes its whole file."""
        if self._source_sha is None:
            path = inspect.getsourcefile(self.fn) or inspect.getfile(self.fn)
            self._source_sha = implementation_sha(path, self.id)
        return self._source_sha

    def fingerprint(self, manifest):
        """The domain fingerprint on this manifest, memoized by the
        manifest's content hash: the page recomputes every test's
        fingerprint on every poll, and a manifest never changes under a
        running tool."""
        key = manifest.content_sha
        if key and self._fp[0] == key:
            return self._fp[1]
        fp = _manifest.fingerprint(manifest, self.covers, extra=[self.source_sha])
        if key:
            self._fp = (key, fp)
        return fp

    def definition(self):
        """The gate-visible definition (no implementation, no prose)."""
        return {
            "id": self.id,
            "subsystem": self.subsystem,
            "kind": self.kind,
            "hardware": self.hardware,
            "covers": [list(c) for c in self.covers],
            "requires": list(self.requires),
            "always": self.always,
        }

    def describe(self):
        d = self.definition()
        d.update({"title": self.title, "est_min": self.est_min, "mode": self.mode,
                  "steps": list(self.steps), "actions": list(self.actions),
                  "hands": list(self.hands), "precheck": bool(self.precheck),
                  "description": self.description})
        return d

    def fixture_runnable(self, channels):
        """Whether a bench actuator covering `channels` lets this test
        run with nobody in the room: an operator test whose every action
        is a covered channel and which asks a person for nothing else. A
        live test never qualifies (the arm press and the fire watch are
        a person's); an auto test needs no fixture."""
        if self.kind != "operator" or self.hands or not self.actions:
            return False
        return all(a in channels for a in self.actions)

    def cannot_start(self):
        """The reason the test cannot start on the machine as it is, or
        None. Evaluated right before a start; never a result."""
        if self.precheck is None:
            return None
        try:
            reason = self.precheck()
        except Exception as e:  # noqa: BLE001 - a broken precheck refuses, it never crashes the runner
            return "precheck errored: %s: %s" % (type(e).__name__, e)
        return reason or None


def source_file_sha(path):
    with open(path, "rb") as f:
        data = f.read().replace(b"\r\n", b"\n")
    return hashlib.sha256(data).hexdigest()


_PARTS = {}                 # path -> (shared sha, {test id: own sha}); the files never change under a run
_PARTS_LOCK = threading.Lock()


def module_parts(path):
    """(shared sha, {test id: own sha}) for a suite module: the test ids
    are read from the @test("...") decorators on the module's top-level
    functions; a test's own text runs from its first decorator line to
    the end of its body; the shared text is every other line of the
    file. Line endings normalized."""
    with _PARTS_LOCK:
        hit = _PARTS.get(path)
    if hit is not None:
        return hit
    with open(path, "rb") as f:
        text = f.read().replace(b"\r\n", b"\n").decode("utf-8")
    lines = text.split("\n")
    spans = {}
    try:
        tree = ast.parse(text)
    except SyntaxError:
        tree = None
    for node in (tree.body if tree is not None else []):
        if not isinstance(node, ast.FunctionDef):
            continue
        for d in node.decorator_list:
            if (isinstance(d, ast.Call) and isinstance(d.func, ast.Name) and d.func.id == "test"
                    and d.args and isinstance(d.args[0], ast.Constant) and isinstance(d.args[0].value, str)):
                start = min([dd.lineno for dd in node.decorator_list] + [node.lineno])
                spans[d.args[0].value] = (start, node.end_lineno)
    owned = set()
    for a, b in spans.values():
        owned.update(range(a, b + 1))
    shared = "\n".join(ln for i, ln in enumerate(lines, 1) if i not in owned)
    own = {tid: _manifest.sha256_text("\n".join(lines[a - 1:b])) for tid, (a, b) in spans.items()}
    parts = (_manifest.sha256_text(shared), own)
    with _PARTS_LOCK:
        _PARTS[path] = parts
    return parts


def sibling_imports(path, seen=None):
    """The suite modules a module imports with `from .x import ...`, as
    paths, transitively, in a stable order. A helper a test calls from
    a sibling module is part of the judge, so its text belongs in the
    test's fingerprint."""
    if seen is None:
        seen = []
    with open(path, "rb") as f:
        text = f.read().replace(b"\r\n", b"\n").decode("utf-8")
    try:
        tree = ast.parse(text)
    except SyntaxError:
        return seen
    here = os.path.dirname(path)
    for node in tree.body:
        if isinstance(node, ast.ImportFrom) and node.level == 1 and node.module:
            sib = os.path.join(here, node.module.replace(".", os.sep) + ".py")
            if os.path.isfile(sib) and sib not in seen and sib != path:
                seen.append(sib)
                sibling_imports(sib, seen)
    return seen


def implementation_sha(path, test_id):
    """The implementation hash of one test (see Test.source_sha): the
    test's own text, the shared text of its module, and the shared text
    of every sibling suite module the module imports."""
    shared, own = module_parts(path)
    if test_id not in own:
        return source_file_sha(path)
    helpers = [module_parts(p)[0] for p in sorted(sibling_imports(path))]
    return _manifest.sha256_text("%s:%s" % (":".join([shared] + helpers), own[test_id]))


def test(id, *, title, subsystem, kind="auto", hardware="api", mode=None, covers=(),
         requires=(), always=False, est_min=1, steps=(), description="", actions=(),
         precheck=None, hands=()):
    """`mode` names the controller mode the test needs live when it starts
    ("grbl" or "cloud"); the runner switches the machine there before the
    test and leaves it there, so a queue crosses modes only where a test
    asks it to. None means the test runs in whatever mode it finds (or
    manages the mode itself, as the cloud tests do through enter_cloud,
    which also waits for the service session).

    `actions` names the machine actions (ACTIONS) the test performs
    through Context.act; an `auto` test declares none. `hands` names what
    the test asks of a person beyond those actions ("app" for a job in
    the Glowforge app, "scrap", "mark" for an eye on the result...): an
    operator test with none, whose actions a bench actuator covers, runs
    unattended when one is up. `precheck` is a callable returning a
    reason string when the machine cannot run the test as it is (None
    when it can)."""
    if not _ID_RX.match(id):
        raise ValueError("test id %r must look like subsystem.name" % id)
    if kind not in KINDS:
        raise ValueError("test %s: kind %r" % (id, kind))
    if hardware not in HARDWARE:
        raise ValueError("test %s: hardware %r" % (id, hardware))
    if mode is not None and mode not in MODES:
        raise ValueError("test %s: mode %r" % (id, mode))
    for c, g in covers:
        if c in _manifest.DEV_ONLY_COMPONENTS:
            raise ValueError("test %s: may not cover dev-only component %r" % (id, c))
    for a in actions:
        if a not in ACTIONS:
            raise ValueError("test %s: action %r" % (id, a))
    if actions and kind == "auto":
        raise ValueError("test %s: an auto test asks for no machine actions" % id)
    if hands and kind == "auto":
        raise ValueError("test %s: an auto test asks nothing of a person" % id)
    if precheck is not None and not callable(precheck):
        raise ValueError("test %s: precheck must be callable" % id)

    def deco(fn):
        if id in REGISTRY:
            raise ValueError("duplicate test id %r" % id)
        REGISTRY[id] = Test(id, title, subsystem, kind, hardware, covers, requires,
                            always, est_min, steps, description, fn, mode=mode,
                            actions=actions, precheck=precheck, hands=hands)
        return fn
    return deco


def all_tests(registry=None):
    """Tests in registration order (the suite modules import in
    subsystem order, so this is the display order)."""
    return list((registry if registry is not None else REGISTRY).values())


def get(id, registry=None):
    return (registry if registry is not None else REGISTRY).get(id)


def order_by_requires(tests, selected):
    """`selected` ids in an order that runs a prerequisite before the test
    that names it.

    Registration order otherwise, so a run reads down the page. A
    prerequisite outside the selection places nothing: either it is
    already satisfied, or the run that needs it will be refused and
    recorded as skipped. validate() has ruled out cycles; the stack check
    only keeps a malformed registry from recursing forever.
    """
    want = set(selected)
    by_id = {t.id: t for t in tests}
    out, placed = [], set()

    def visit(tid, stack):
        if tid in placed or tid not in want or tid not in by_id or tid in stack:
            return
        stack.add(tid)
        for r in by_id[tid].requires:
            visit(r, stack)
        stack.discard(tid)
        placed.add(tid)
        out.append(tid)

    for t in tests:
        visit(t.id, set())
    return out


def validate(registry=None):
    """Every `requires` names a known test and there are no cycles."""
    reg = registry if registry is not None else REGISTRY
    for t in reg.values():
        for r in t.requires:
            if r not in reg:
                raise ValueError("test %s requires unknown test %s" % (t.id, r))
    seen = {}

    def visit(tid, stack):
        if tid in stack:
            raise ValueError("requires cycle: %s" % " -> ".join(stack + [tid]))
        if seen.get(tid):
            return
        for r in reg[tid].requires:
            visit(r, stack + [tid])
        seen[tid] = True
    for tid in reg:
        visit(tid, [])


def catalog_hash(registry=None):
    """Identity of the catalog's definitions (ids, kinds, coverage,
    requirements, core membership) - not of the implementations, which
    the per-test fingerprints carry."""
    defs = sorted((t.definition() for t in all_tests(registry)), key=lambda d: d["id"])
    return _manifest.sha256_text(_manifest.canonical(defs))


def load_suite():
    """Import the suite modules (each registers its tests) and validate."""
    from . import suite  # noqa: F401  (registers on import)
    validate()
    return REGISTRY


def suite_dir():
    return os.path.join(os.path.dirname(os.path.abspath(__file__)), "suite")
