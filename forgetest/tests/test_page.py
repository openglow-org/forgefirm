# Copyright 2026 514 LLC d/b/a OpenGlow
# Written by Scott Wiederhold
# https://community.openglow.org
# SPDX-License-Identifier:    MIT

"""The page assembly: the ui/ files inlined in order, the gzipped install
(what the dev image carries) assembling to the same bytes as a checkout,
the token placeholder carried exactly once, and a missing marker refused
rather than served."""
import gzip
import os
import shutil
import tempfile
import unittest

from forgetest import page


def gzipped_copy(src_dir):
    """A copy of src_dir with every file gzipped in place, the way the
    recipe installs ui/."""
    tmp = tempfile.mkdtemp(prefix="forgetest-ui-")
    for root, _, files in os.walk(src_dir):
        rel = os.path.relpath(root, src_dir)
        dst_dir = os.path.normpath(os.path.join(tmp, rel))
        os.makedirs(dst_dir, exist_ok=True)
        for f in files:
            with open(os.path.join(root, f), "rb") as i, \
                    gzip.open(os.path.join(dst_dir, f + ".gz"), "wb") as o:
                o.write(i.read())
    return tmp


class PageTests(unittest.TestCase):
    def test_checkout_and_gzipped_install_agree(self):
        plain = page.assemble()
        tmp = gzipped_copy(page.UI_DIR)
        try:
            self.assertFalse(os.path.exists(os.path.join(tmp, "index.html")))
            self.assertEqual(page.assemble(tmp), plain)
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    def test_page_is_self_contained(self):
        html = page.assemble()
        self.assertNotIn("<link ", html)
        self.assertNotIn("<script src=", html)
        self.assertEqual(html.count(page.TOKEN_MARK), 1)
        self.assertIn("data-bs-theme", html)
        for name in page.CSS_FILES + page.JS_FILES:
            self.assertNotIn('href="%s"' % name, html)
            self.assertNotIn('src="%s"' % name, html)

    def test_render_substitutes_the_token(self):
        out = page.render("deadbeef0123")
        self.assertEqual(out.count("deadbeef0123"), 1)
        self.assertNotIn(page.TOKEN_MARK, out)

    def test_missing_marker_is_refused(self):
        tmp = tempfile.mkdtemp(prefix="forgetest-ui-")
        try:
            with open(os.path.join(tmp, "index.html"), "w", encoding="utf-8") as f:
                f.write("<html><head></head><body>no markers</body></html>")
            with self.assertRaises(ValueError):
                page.assemble(tmp)
        finally:
            shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    unittest.main()
