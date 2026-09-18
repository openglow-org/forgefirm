# Copyright 2026 514 LLC d/b/a OpenGlow
# Written by Scott Wiederhold
# https://community.openglow.org
# SPDX-License-Identifier:    MIT

"""The single page: acceptance tab + bench tab, assembled from ui/ into
one self-contained response (no external assets). Bootstrap and the
OpenGlow theme are inlined; theme.css and the vendor/ files are the same
bytes forgectrl's panel carries, so the two pages look like one product
and follow the same light and dark themes (scripts/check-ui-vendor.py
holds them identical).

ui/ holds index.html, theme.css, page.css, help.js, app.js and vendor/.
A plain file is read when it is there (a checkout); otherwise its .gz
sibling is, which is what the recipe installs on the dev image: the
rootfs is raw ext4, so bytes in the package are bytes on the image, and
the page is inflated once at first request. The token placeholder is
spliced in per render."""
import gzip
import os

UI_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "ui")
# The files index.html links, in load order: one marker tag per file.
CSS_FILES = ("vendor/bootstrap.min.css", "theme.css", "page.css")
JS_FILES = ("vendor/bootstrap.bundle.min.js", "help.js", "app.js")
TOKEN_MARK = "__TOKEN__"


def read_ui(name, ui_dir=UI_DIR):
    """A ui/ file as text: the plain file, or its gzipped install."""
    path = os.path.join(ui_dir, *name.split("/"))
    try:
        with open(path, "rb") as f:
            return f.read().decode("utf-8")
    except FileNotFoundError:
        with gzip.open(path + ".gz", "rb") as f:
            return f.read().decode("utf-8")


def assemble(ui_dir=UI_DIR):
    """index.html with every linked stylesheet and script inlined in place
    of its tag. A missing marker is a broken page, not a silent one."""
    html = read_ui("index.html", ui_dir)
    for name in CSS_FILES:
        tag = '<link rel="stylesheet" href="%s" />' % name
        if tag not in html:
            raise ValueError("index.html lacks the marker %s" % tag)
        html = html.replace(tag, "<style>\n" + read_ui(name, ui_dir) + "</style>", 1)
    for name in JS_FILES:
        tag = '<script src="%s"></script>' % name
        if tag not in html:
            raise ValueError("index.html lacks the marker %s" % tag)
        html = html.replace(tag, "<script>\n" + read_ui(name, ui_dir) + "</script>", 1)
    if html.count(TOKEN_MARK) != 1:
        raise ValueError("the page must carry %s exactly once" % TOKEN_MARK)
    return html


_html = None


def render(token):
    global _html
    if _html is None:
        _html = assemble()
    return _html.replace(TOKEN_MARK, token)
