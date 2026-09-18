# Copyright 2026 514 LLC d/b/a OpenGlow
# Written by Scott Wiederhold
# https://community.openglow.org
# SPDX-License-Identifier:    MIT

"""Every phrase the cloud suite greps for in the gfcloud log must be a
phrase the pinned cloud app can actually log.

The cloud tests judge a print on the machine's own log lines. When the
app's wording moves (a resume that used to log one line logs two), a
test keeps looking for the old line and fails a print that worked. The
replay fixtures cannot catch that: they are excerpts of the old wording.
This test reads the phrases straight out of suite/cloud.py and checks
each against the logger calls in the app sources at the revisions the
recipes pin (python3-gfhardware and python3-gfutilities, from the
manifest cache the tree manifest builds, or the sibling checkouts when
no cache is at hand), placeholder-aware: "paused at" is covered by
'paused at %s', "warm up: holding" by '%s: holding %.1f s', and
"authenticate_machine SUCCESS" by logger.info('SUCCESS') inside
def authenticate_machine (the log format is module:function message).
"""
import ast
import importlib.util
import os
import re
import subprocess
import unittest

import helpers  # noqa: F401  (sys.path)

HERE = os.path.dirname(os.path.abspath(__file__))
FORGETEST = os.path.dirname(HERE)
REPO = os.path.dirname(FORGETEST)
CLOUD_PY = os.path.join(FORGETEST, "forgetest", "suite", "cloud.py")
APP_COMPONENTS = ("python3-gfhardware", "python3-gfutilities")
SIBLINGS = {"python3-gfhardware": "python3-gfhardware", "python3-gfutilities": "Glowforge-Utilities"}

# Phrases cloud.py builds at run time rather than writing out (a "%s: holding"
# with the phase filled in), listed here so they are checked too.
BUILT_PHRASES = ("warm up: holding", "cool down: holding", "warm up: skipped", "cool down: skipped",
                 'finished with event ":cancelled"', 'finished with event ":completed"',
                 "motion [", "print [")
# Phrases that are not the app's: forgetest's own log, a line prefix of
# the log format rather than a message, or a line of forgectrl's log (the
# engine's effective-limits line, judged from its own file).
NOT_APP = ("PASS", "gfcloud", "[", "effective limits: coolant ceiling", "header")

PLACEHOLDER = re.compile(r"%(?:\([^)]*\))?[-+ #0]*\d*(?:\.\d+)?[sdifrxXeEgGcu]|%%")


# ------------------------------------------------------------ the needles

def needles_in(path):
    """String constants cloud.py tests against log lines: the left side of
    an `x in ln`, the arguments of wait_log, endswith() arguments, and the
    names those resolve to (module- or function-level constants)."""
    with open(path, encoding="utf-8") as f:
        tree = ast.parse(f.read())
    consts = {}                                   # name -> list of str

    def strings_of(node):
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            return [node.value]
        if isinstance(node, (ast.List, ast.Tuple)):
            return [s for e in node.elts for s in strings_of(e)]
        if isinstance(node, ast.Name):
            return consts.get(node.id, [])
        if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Add):
            return strings_of(node.left) + strings_of(node.right)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id == "list":
            return [s for a in node.args for s in strings_of(a)]
        return []

    for node in ast.walk(tree):
        if isinstance(node, ast.Assign) and len(node.targets) == 1 and isinstance(node.targets[0], ast.Name):
            got = strings_of(node.value)
            if got:
                consts[node.targets[0].id] = got
    found = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Compare) and any(isinstance(op, ast.In) for op in node.ops):
            found.update(strings_of(node.left))
        elif isinstance(node, ast.Call):
            fn = node.func
            if isinstance(fn, ast.Name) and fn.id == "wait_log" and len(node.args) >= 3:
                found.update(strings_of(node.args[2]))
            elif isinstance(fn, ast.Attribute) and fn.attr == "endswith":
                for a in node.args:
                    found.update(strings_of(a))
    # the mark tuples the suite scans lines against in comprehensions
    # (`any(m in ln for m in SESSION_MARKS)`), where the name is a loop variable
    for name, vals in consts.items():
        if name.endswith(("_MARKS", "_LINES")):
            found.update(vals)
    found.update(BUILT_PHRASES)
    return sorted(s.strip() for s in found if s.strip() and s.strip() not in NOT_APP and " " in s or s in BUILT_PHRASES)


# ------------------------------------------------------------ the app's lines

def log_literals(source, label):
    """(function name, fragments) for every logger call in a Python source:
    the message's fixed text split at its placeholders."""
    out = []
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return out
    funcs = {}
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            for sub in ast.walk(node):
                funcs.setdefault(id(sub), node.name)
    for node in ast.walk(tree):
        if not (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
                and node.func.attr in ("debug", "info", "warning", "warn", "error", "critical", "exception")
                and node.args):
            continue
        msg = node.args[0]
        if isinstance(msg, ast.BinOp) and isinstance(msg.op, ast.Mod):
            msg = msg.left
        frags = None
        if isinstance(msg, ast.Constant) and isinstance(msg.value, str):
            frags = [p.replace("%%", "%") for p in PLACEHOLDER.split(msg.value)]
        elif isinstance(msg, ast.JoinedStr):
            frags = [""]
            for v in msg.values:
                if isinstance(v, ast.Constant):
                    frags[-1] += str(v.value)
                else:
                    frags.append("")
        if frags is not None:
            out.append((funcs.get(id(node), ""), frags, label))
    return out


def covers(frags, needle):
    """True when `needle` is a substring of a rendering of the message whose
    fixed text is `frags` (the gaps between fragments are placeholders; the
    rendered line is `function message`, so frags[0] carries the function).
    A placeholder is a value, never the phrase itself: the needle must lie
    inside one fixed fragment, or run through the fragments in order and
    contain at least one complete non-empty fragment."""
    n = len(frags)
    if any(needle in f for f in frags if f):
        return True

    def in_gap(text, i, complete):
        """`text` starts in the gap before frags[i]."""
        if i >= n:
            return False
        frag = frags[i]
        if frag == "":
            return complete if i == n - 1 else in_gap(text, i + 1, complete)
        start = 0
        while True:
            j = text.find(frag, start)
            if j < 0:
                break
            if after(text[j + len(frag):], i, True):
                return True
            start = j + 1
        # the needle ends inside frags[i]: a proper prefix of it, which counts
        # as the complete fragment the rule wants only when the gap before it
        # was preceded by one, or when what is left of the fragment is a
        # trailing separator (": holding " is covered by "warm up: holding")
        for k in range(1, len(frag)):
            if text.endswith(frag[:k]) and (complete or not frag[k:].strip()):
                return True
        return False

    def after(text, i, complete):
        """`text` starts right after the whole of frags[i]."""
        if text == "":
            return complete
        return in_gap(text, i + 1, complete)

    for i, frag in enumerate(frags):
        for off in range(len(frag)):
            suffix = frag[off:]
            if needle.startswith(suffix) and after(needle[len(suffix):], i, off == 0):
                return True
        if i >= 1 and in_gap(needle, i, False):
            return True
    return False


# ------------------------------------------------------------ sources

def pinned_sources():
    """{component: [(path, text)]} for the app components at their pinned
    revisions, through the tree manifest's recipe parser and cache; the
    sibling checkouts when the cache cannot be reached."""
    spec = importlib.util.spec_from_file_location("mft", os.path.join(REPO, "scripts", "manifest-from-tree.py"))
    mft = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mft)
    meta = os.path.join(os.path.dirname(REPO), "meta-openglow")
    cache = os.path.join(REPO, ".manifest-cache")
    out = {}
    for name, rel, layer in mft.RECIPES:
        if name not in APP_COMPONENTS:
            continue
        base = REPO if layer == "forgefirm" else meta
        files = []
        try:
            url, rev = mft.parse_recipe(os.path.join(base, rel))
            repo = mft.fetch(url, rev, cache)
            names = mft.git(["ls-tree", "-r", "--name-only", rev], cwd=repo).decode().split("\n")
            for p in names:
                if p.endswith(".py"):
                    files.append((p, mft.git(["show", "%s:%s" % (rev, p)], cwd=repo).decode("utf-8", "replace")))
            out[name] = ("pinned %s" % rev[:12], files)
            continue
        except (OSError, subprocess.CalledProcessError, SystemExit):
            pass
        sib = os.path.join(os.path.dirname(REPO), SIBLINGS[name])
        if os.path.isdir(sib):
            for root, _dirs, fnames in os.walk(sib):
                if ".git" in root:
                    continue
                for fn in fnames:
                    if fn.endswith(".py"):
                        p = os.path.join(root, fn)
                        files.append((os.path.relpath(p, sib), open(p, encoding="utf-8", errors="replace").read()))
            out[name] = ("sibling checkout", files)
    return out


class CloudNeedleTests(unittest.TestCase):
    def test_every_needle_is_a_line_the_pinned_app_can_log(self):
        needles = needles_in(CLOUD_PY)
        self.assertGreaterEqual(len(needles), 30, needles)
        self.assertIn("authenticate_machine SUCCESS", needles)
        self.assertIn("RX-EVENT: ready", needles)
        sources = pinned_sources()
        missing = [c for c in APP_COMPONENTS if c not in sources]
        if missing:
            if os.environ.get("CI"):
                self.fail("no source for %s (run scripts/manifest-from-tree.py first)" % ", ".join(missing))
            self.skipTest("no source for %s" % ", ".join(missing))
        literals = []
        for comp, (where, files) in sources.items():
            for path, text in files:
                literals.extend(log_literals(text, "%s:%s" % (comp, path)))
        self.assertGreater(len(literals), 100, "too few logger calls found: is the app source complete?")
        uncovered = []
        for needle in needles:
            if not any(covers([fn + " " + frags[0]] + frags[1:], needle) for fn, frags, _ in literals):
                uncovered.append(needle)
        self.assertEqual(uncovered, [], "cloud.py greps for lines the pinned app never logs (sources: %s)"
                         % ", ".join("%s=%s" % (c, w) for c, (w, _) in sources.items()))

    def test_cover_rules(self):
        self.assertTrue(covers(["_run_loop paused at ", ""], "paused at"))
        self.assertTrue(covers(["_dwell ", ": holding ", " s"], "warm up: holding"))
        self.assertTrue(covers(["authenticate_machine SUCCESS"], "authenticate_machine SUCCESS"))
        self.assertTrue(covers(["_resume_retraced resuming (laser lead ", " ticks)"], "resuming (laser lead"))
        self.assertTrue(covers(["_finish_action ", " [", "]: finished with event \"", "\""],
                               'finished with event ":completed"'))
        self.assertFalse(covers(["_run_loop button pressed while paused"], "button pressed while paused; resuming"))
        self.assertFalse(covers(["_run_loop paused at ", ""], "paused it"))


if __name__ == "__main__":
    unittest.main()
