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
This test reads the phrases straight out of suite/cloud.py and
suite/cloudlatch.py and checks each against the logger calls in the app
sources at the revisions the recipes pin (python3-gfhardware and
python3-gfutilities, from the manifest cache the tree manifest builds, or
the sibling checkouts when no cache is at hand), placeholder-aware:
"paused at" is covered by 'paused at %s', "warm up: holding" by
'%s: holding %.1f s', and "authenticate_machine SUCCESS" by
logger.info('SUCCESS') inside def authenticate_machine (the log format is
module:function message). A phrase must run through the message's fixed
text: one made of values alone is covered by nothing, and one whose fixed
text is a separator only when each value in it is one of the app's own
constants ("print [" by '%s [%s]: finished...' and the action type
'print'; "zzz qqq" by no '%s %s'). A %s the call fills with a string
constant is fixed text ("OFFLINE service" by offline.py's
'%s: no web session...' % (OFFLINE_MARK, ...)).
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
CLOUDLATCH_PY = os.path.join(FORGETEST, "forgetest", "suite", "cloudlatch.py")
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
    the message's fixed text split at its fields. A %s field the call fills
    with a string constant, written out or a module-level name bound to one
    (offline.py's OFFLINE_MARK), is fixed text too."""
    out = []
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return out
    consts = {n.targets[0].id: n.value.value for n in tree.body
              if isinstance(n, ast.Assign) and len(n.targets) == 1 and isinstance(n.targets[0], ast.Name)
              and isinstance(n.value, ast.Constant) and isinstance(n.value.value, str)}

    def constant(node):
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            return node.value
        return consts.get(node.id) if isinstance(node, ast.Name) else None

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
        msg, args = node.args[0], node.args[1:]
        if isinstance(msg, ast.BinOp) and isinstance(msg.op, ast.Mod):
            msg, args = msg.left, (msg.right.elts if isinstance(msg.right, ast.Tuple) else
                                   [] if isinstance(msg.right, ast.Dict) else [msg.right])
        frags = None
        if isinstance(msg, ast.Constant) and isinstance(msg.value, str):
            frags, k, pos = [""], 0, 0
            for m in PLACEHOLDER.finditer(msg.value):
                frags[-1] += msg.value[pos:m.start()]
                pos = m.end()
                if m.group() == "%%":
                    frags[-1] += "%"
                    continue
                value = constant(args[k]) if m.group() == "%s" and k < len(args) else None
                k += 1
                if value is None:
                    frags.append("")
                else:
                    frags[-1] += value
            frags[-1] += msg.value[pos:]
        elif isinstance(msg, ast.JoinedStr):
            frags = [""]
            for v in msg.values:
                value = (str(v.value) if isinstance(v, ast.Constant) else
                         constant(v.value) if v.conversion == -1 and v.format_spec is None else None)
                if value is None:
                    frags.append("")
                else:
                    frags[-1] += value
        if frags is not None:
            out.append((funcs.get(id(node), ""), frags, label))
    return out


def app_words(text):
    """The one-word string constants of an app module: the values a field
    of a logged line carries by name (the action types 'hunt', 'motion' and
    'print')."""
    try:
        tree = ast.parse(text)
    except SyntaxError:
        return set()
    return {n.value for n in ast.walk(tree) if isinstance(n, ast.Constant) and isinstance(n.value, str)
            and re.fullmatch(r"[\w.-]+", n.value)}


def covers(frags, needle, words=frozenset()):
    """True when `needle` is a substring of a rendering of the message whose
    fixed text is `frags` (the gaps between fragments are fields, each any
    value; the rendered line is `function message`, so frags[0] carries the
    function). A field is a value, never the phrase itself: the fixed text
    the needle runs through must carry a word (a letter or a digit), or,
    where it is a separator and never a bare space, every piece of a field
    in the needle must be one of the app's own one-word constants (`words`:
    "print [" is the action type print before the action's id)."""
    # adjacent fields are one field
    frags = [f for i, f in enumerate(frags) if f or i in (0, len(frags) - 1)]
    n = len(frags)
    found = []                                      # (fixed text, field pieces) of each rendering

    def fixed(rest, i, off, text, fields):
        """rest starts at frags[i][off]."""
        frag = frags[i][off:]
        if frag.startswith(rest):
            found.append((text + rest, fields))
        elif rest.startswith(frag) and i + 1 < n:
            field(rest[len(frag):], i + 1, text + frag, fields)

    def field(rest, i, text, fields):
        """rest starts in the field before frags[i]."""
        if not rest:
            found.append((text, fields))
            return
        found.append((text, fields + [rest]))       # the needle ends in this field
        nxt, j = frags[i], 0
        while nxt and j >= 0:                       # the field takes rest[:j], then frags[i]
            if rest[j:].startswith(nxt) or nxt.startswith(rest[j:]):
                fixed(rest[j:], i, 0, text, fields + ([rest[:j]] if j else []))
            j = rest.find(nxt[0], j + 1)

    for i, frag in enumerate(frags):
        off = frag.find(needle[:1])
        while needle and off >= 0:
            fixed(needle, i, off, "", [])
            off = frag.find(needle[0], off + 1)
        if i >= 1:
            field(needle, i, "", [])
    return any(re.search(r"\w", text) or (text.strip() and fields and all(p in words for p in fields))
               for text, fields in found)


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
        needles = needles_in(CLOUD_PY) + needles_in(CLOUDLATCH_PY)
        self.assertGreaterEqual(len(needles), 30, needles)
        self.assertIn("authenticate_machine SUCCESS", needles)
        self.assertIn("RX-EVENT: ready", needles)
        self.assertIn("button pushed", needles)
        sources = pinned_sources()
        missing = [c for c in APP_COMPONENTS if c not in sources]
        if missing:
            if os.environ.get("CI"):
                self.fail("no source for %s (run scripts/manifest-from-tree.py first)" % ", ".join(missing))
            self.skipTest("no source for %s" % ", ".join(missing))
        literals, words = [], set()
        for comp, (where, files) in sources.items():
            for path, text in files:
                literals.extend(log_literals(text, "%s:%s" % (comp, path)))
                if not path.startswith(("tests/", "examples/")):
                    words |= app_words(text)
        self.assertGreater(len(literals), 100, "too few logger calls found: is the app source complete?")
        uncovered = []
        for needle in needles:
            if not any(covers([fn + " " + frags[0]] + frags[1:], needle, words) for fn, frags, _ in literals):
                uncovered.append(needle)
        self.assertEqual(uncovered, [], "the cloud suite greps for lines the pinned app never logs (sources: %s)"
                         % ", ".join("%s=%s" % (c, w) for c, (w, _) in sources.items()))

    def test_cover_rules(self):
        self.assertTrue(covers(["_run_loop paused at ", ""], "paused at"))
        self.assertTrue(covers(["_dwell ", ": holding ", " s"], "warm up: holding"))
        self.assertTrue(covers(["_dwell ", ": skipped (", " = ", ""], "warm up: skipped"))
        self.assertTrue(covers(["authenticate_machine SUCCESS"], "authenticate_machine SUCCESS"))
        self.assertTrue(covers(["_resume_retraced resuming (laser lead ", " ticks)"], "resuming (laser lead"))
        self.assertTrue(covers(["_finish_action ", " [", "]: finished with event \"", "\""],
                               'finished with event ":completed"'))
        self.assertFalse(covers(["_run_loop button pressed while paused"], "button pressed while paused; resuming"))
        self.assertFalse(covers(["_run_loop paused at ", ""], "paused it"))
        # Fields alone are never the phrase: not two values around a space,
        # and around a separator only the app's own constants.
        self.assertFalse(covers(["img_upload FAILED: ", " ", ""], "zzz qqq", {"zzz", "qqq"}))
        finish = ["_finish_action ", " [", "]: finished with event \":", "\""]
        self.assertTrue(covers(finish, "print [", {"print"}))
        self.assertFalse(covers(finish, "zzz [", {"print"}))
        # A field the call fills with a constant is the line's own text.
        (_, frags, _), = log_literals("MARK = 'OFFLINE service'\ndef open():\n"
                                      "    logger.info('%s: listening on %s' % (MARK, path))\n", "x")
        self.assertTrue(covers(["open " + frags[0]] + frags[1:], "OFFLINE service"))


if __name__ == "__main__":
    unittest.main()
