#!/usr/bin/env python3
# Copyright 2026 514 LLC d/b/a OpenGlow
# Written by Scott Wiederhold
# https://community.openglow.org
# SPDX-License-Identifier:    MIT
"""Parse every script of the acceptance page as forgetest serves it.

forgetest's page.py inlines index.html's linked scripts (the vendored
Bootstrap bundle, help.js, app.js) into one self-contained page beside the
page's own inline script. A syntax error in any of them stops that script,
and the page comes up empty while every unit test passes. This assembles
the page as the daemon does and parses each inline script block as the
browser runs it, a classic script: node's vm.Script, which refuses what a
browser refuses there (a top-level return, an export, a top-level await)
where `node --check` would read a module or a CommonJS body.

Usage: check-page-js.py [UI_DIR]   (default forgetest/forgetest/ui)
The node binary is NODE from the environment, else `node` on the PATH. No
node, a page that does not assemble, or a page with no script is a
failure, never a pass.
"""
import os
import re
import shutil
import subprocess
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "forgetest"))
from forgetest import page  # noqa: E402

# An inline script: no attribute named src, and the body up to the end tag
# as the browser's tokenizer ends it, at "</script" and then white space,
# "/" or ">".
SCRIPT = re.compile(r"<script(?![^>]*\ssrc\s*=)[^>]*>(.*?)</script[\s/>]", re.S | re.I)
# A classic script's parse, as the browser does it; a syntax error exits 1.
PARSE = ('new (require("vm").Script)(require("fs").readFileSync(process.argv[1], "utf8"),'
         ' {filename: process.argv[1]})')


def main():
    ui = sys.argv[1] if len(sys.argv) > 1 else page.UI_DIR
    node = os.environ.get("NODE") or shutil.which("node")
    if not node:
        print("check-page-js: no node: set NODE or put node on the PATH")
        return 2
    try:
        html = page.assemble(ui)
    except (OSError, ValueError) as e:
        print("check-page-js: the page does not assemble: %s" % e)
        return 2
    failures = checked = 0
    with tempfile.TemporaryDirectory(prefix="check-page-js-") as tmp:
        for i, body in enumerate(SCRIPT.findall(html)):
            if not body.strip():
                continue
            path = os.path.join(tmp, "script-%d.js" % i)
            with open(path, "w", encoding="utf-8") as f:
                f.write(body)
            r = subprocess.run([node, "-e", PARSE, path], capture_output=True, text=True)
            checked += 1
            if r.returncode != 0:
                failures += 1
                print("FAIL script %d:\n%s" % (i, (r.stderr or r.stdout).strip()))
    if not checked:
        print("check-page-js: no scripts in the page")
        return 2
    print("%s: %d script(s) checked, %d failed" % ("FAIL" if failures else "PASS", checked, failures))
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
