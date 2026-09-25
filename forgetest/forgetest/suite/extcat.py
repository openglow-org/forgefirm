# Copyright 2026 514 LLC d/b/a OpenGlow
# Written by Scott Wiederhold
# https://community.openglow.org
# SPDX-License-Identifier:    MIT

"""The catalog: the signed index, on the machine.

Its own module, for the reason extcore.py gives. The endorsement, the
judgment of each listed version, the index that never goes back, and a
withdrawn version are shown on a scratch root under /tmp with a throwaway
key standing in for the OpenGlow extension key, because only OpenGlow holds
the real one; the machine's own root is where every index that key did not
sign is refused.
"""

import hashlib
import io
import json
import os
import shutil
import subprocess
import tarfile
import tempfile

from ..catalog import test
from .exthost import FWUP, _forgeext, _write

INDEX_URL = "https://github.com/openglow-org/forgefirm-extensions-catalog/releases/latest/download/index-1.ffi"
STAGE_DIR = "/data/forgefirm/tmp"
PROBE_ID, OTHER_ID = "org.example.catprobe", "org.example.catother"


def _key(work, name):
    subprocess.run([FWUP, "-g", "-o", os.path.join(work, name)], check=True, capture_output=True, timeout=60, cwd=work)
    return os.path.join(work, name)


def _archive(work, name, product, members, version, key):
    """An fwup archive of the extension container's form: payload.tar.gz of members {name: text}, signed with key."""
    payload = os.path.join(work, name + ".tar.gz")
    with tarfile.open(payload, "w:gz") as t:
        for member, text in members.items():
            info = tarfile.TarInfo(member)
            data = text.encode()
            info.size, info.mode = len(data), 0o644
            t.addfile(info, io.BytesIO(data))
    conf = os.path.join(work, name + ".conf")
    _write(conf, 'meta-product = "%s"\nmeta-description = "%s"\nmeta-version = "%s"\nmeta-platform = "forgefirm-ext"\n'
                 'file-resource payload.tar.gz {\n    host-path = "%s"\n}\n' % (product, name, version, payload))
    raw, signed = os.path.join(work, name + ".raw"), os.path.join(work, name + ".signed")
    subprocess.run([FWUP, "-c", "-f", conf, "-o", raw], check=True, capture_output=True, timeout=60, cwd=work)
    subprocess.run([FWUP, "-S", "-s", key + ".priv", "-i", raw, "-o", signed], check=True, capture_output=True,
                   timeout=60, cwd=work)
    return signed


def _package(work, id_, key):
    m = {"manifest": 1, "id": id_, "name": "catalog probe", "version": "1.0.0", "author": "forgetest", "license": "MIT",
         "api": "0.1", "runtime": "data", "capabilities": []}
    return _archive(work, id_, "ForgeFIRM extension", {"manifest.json": json.dumps(m)}, "1.0.0", key)


def _index(work, doc, key, version):
    return _archive(work, "index-" + version, "ForgeFIRM extension index", {"index.json": json.dumps(doc)}, version, key)


def _staged():
    try:
        return sorted(n for n in os.listdir(STAGE_DIR) if n.startswith("ext-"))
    except OSError:
        return []


@test("exthost.catalog", title="The catalog: the signed index verified, kept, judged, and endorsing one key for one id",
      subsystem="exthost", kind="auto", hardware="api", est_min=2,
      covers=[("forgectrl", "src/extpkg.*"), ("forgectrl", "src/main.c"), ("forgeext", "src/index.*"),
              ("forgeext", "src/pkg.*"), ("forgeext", "src/install.*"), ("forgeext", "src/main.c")],
      description="GET /ext/catalog answers the index the host keeps (or null), judged against this firmware, and "
                  "the one address it is fetched from. On a scratch root, with a throwaway key standing in for the "
                  "OpenGlow extension key, the machine's own forgeext keeps an index signed with it, and the author "
                  "key it names for one id makes a package of that id signed with it read as community and "
                  "endorsed, where before it was unverified; the same key on another id counts for nothing. Read "
                  "back, each listed version is judged on the machine: one that asks for a capability this "
                  "firmware does not have is kept and not offered, and the offer is the newest it runs. An index "
                  "older than the one kept is refused however well signed, and one that withdraws the probe's "
                  "version makes the machine refuse to install it, in OpenGlow's words. On the machine's own root, "
                  "the stand-in's index (signed by a key that is not the OpenGlow extension key) is refused in "
                  "words, a package handed over as an index is refused by the product gate, and the index kept "
                  "is left as it was. POST /ext/catalog/get refuses an id with no such form (400), and one the "
                  "kept index does not list (404, or 409 with no index kept), before anything is fetched. Nothing "
                  "is left in the staging directory. POST /ext/catalog/refresh is not asked here: GitHub counts "
                  "every request of the index's address as a download, and that count is the operators'. The "
                  "refresh (curl, https alone and bounded, 502 in curl's words, 409 in the host's, the file "
                  "removed), the fetch of the offered version held to its size and SHA-256, and the tiers an "
                  "install takes from it, are forgectrl's extpkg_test and forgeext's install_test.")
def catalog(ctx):
    fc = ctx.forgectrl
    ev = ctx.evidence
    ctx.check(fc.wait_idle(timeout=30, abort=ctx.aborted), "machine not idle")
    staged_before = _staged()
    kept = _forgeext("index")
    ctx.check(kept.get("ok") is True, "forgeext index -> %s", kept.get("error"))
    ev["kept_before"] = (kept.get("index") or {}).get("version")
    work = tempfile.mkdtemp(prefix="ft-catalog-", dir="/tmp")
    try:
        st, doc = fc.get("/ext/catalog")
        ctx.check(st == 200 and isinstance(doc, dict) and doc.get("url") == INDEX_URL and "index" in doc
                  and doc.get("index") == kept.get("index") and "core_checked" in doc,
                  "GET /ext/catalog -> %s %s", st, doc)

        # The endorsement, on a scratch root with a stand-in for the OpenGlow extension key.
        root = os.path.join(work, "root")
        os.makedirs(os.path.join(root, "keys"))
        og, author = _key(work, "standin"), _key(work, "author")
        probe, other = _package(work, PROBE_ID, author), _package(work, OTHER_ID, author)
        with open(probe, "rb") as f:
            data = f.read()
        with open(author + ".pub") as f:
            author_pub = f.read().strip()
        listed = {"version": "1.0.0", "url": "https://example.org/catprobe.ffx", "sha256": hashlib.sha256(data).hexdigest(),
                  "size": len(data), "capabilities": [], "api": "0.1"}
        later = dict(listed, version="1.1.0", url="https://example.org/catprobe-1.1.0.ffx",
                     capabilities=["forgetest.not-a-capability"])
        entry = {"id": PROBE_ID, "name": "catalog probe", "author": "forgetest", "key": author_pub,
                 "versions": [later, listed]}
        idx = _index(work, {"index": 1, "packages": [entry]}, og, "2026.923.2")

        def scratch(*args):
            return _forgeext("--root", root, "--official-key", og + ".pub", "--no-reserve", *args)
        r = scratch("inspect", probe)
        ctx.check(r.get("ok") is True and r.get("tier") == "unverified", "before an index: %s %s", r.get("tier"), r.get("error"))
        r = scratch("index-verify", idx)
        ev["scratch_verify"] = r
        ctx.check(r.get("ok") is True and r.get("packages") == 1 and r.get("version") == "2026.923.2",
                  "the stand-in's index is kept on the scratch root: %s", r)
        read = scratch("index")
        mine = ((read.get("index") or {}).get("packages") or [{}])[0]
        vs = {v.get("version"): v for v in mine.get("versions", [])}
        ev["judged"] = {"offer": mine.get("offer"), "1.1.0": vs.get("1.1.0", {}).get("why"),
                        "core_checked": read.get("core_checked")}
        ctx.check(mine.get("id") == PROBE_ID and len(mine.get("key_id", "")) == 64, "and read back: %s", mine)
        ctx.check(vs.get("1.1.0", {}).get("usable") is False and "not a capability" in vs.get("1.1.0", {}).get("why", "")
                  and vs.get("1.0.0", {}).get("usable") is True and mine.get("offer") == "1.0.0",
                  "judged on the machine: 1.1.0 kept and not offered, 1.0.0 offered: %s", ev["judged"])
        r = scratch("inspect", probe)
        ev["endorsed"] = {"tier": r.get("tier"), "endorsed": r.get("endorsed")}
        ctx.check(r.get("tier") == "community" and r.get("endorsed") is True,
                  "signed with the key it endorses for its id: community, endorsed (%s %s)", r.get("tier"), r.get("endorsed"))
        r = scratch("inspect", other)
        ctx.check(r.get("tier") == "unverified" and r.get("endorsed") is False,
                  "the same key on another id: %s %s", r.get("tier"), r.get("endorsed"))

        # Never back: an older index is refused, and the one kept stays.
        r = scratch("index-verify", _index(work, {"index": 1, "packages": []}, og, "2026.923.1"))
        ev["older"] = r.get("error")
        ctx.check(r.get("ok") is False and "never goes back" in (r.get("error") or ""), "an older index -> %s", r.get("error"))
        ctx.check((scratch("index").get("index") or {}).get("version") == "2026.923.2", "and the one kept stays")

        # A withdrawn version does not install, from the catalog or from anywhere else.
        gone = dict(entry, versions=[later], withdrawn=[{"version": "1.0.0", "reason": "forgetest withdrew it"}])
        r = scratch("index-verify", _index(work, {"index": 1, "packages": [gone]}, og, "2026.923.3"))
        ctx.check(r.get("ok") is True, "an index withdrawing the probe's version is kept: %s", r.get("error"))
        r = scratch("inspect", probe)
        ev["withdrawn"] = r.get("error")
        ctx.check(r.get("ok") is False and "OpenGlow withdrew %s 1.0.0 from its catalog: forgetest withdrew it" % PROBE_ID
                  in (r.get("error") or ""), "the withdrawn version -> %s", r.get("error"))

        # The machine's own root keeps no index but OpenGlow's.
        refused = {}
        for name, path, words in (("the stand-in's index", idx, "not signed with the OpenGlow extension key"),
                                  ("a package as an index", probe, "product")):
            r = _forgeext("index-verify", path)
            refused[name] = r.get("error")
            ctx.check(r.get("ok") is False and words in (r.get("error") or ""), "%s -> %s", name, r.get("error"))
        ev["refused"] = refused
        ctx.check(_forgeext("index").get("index") == kept.get("index"), "a refused index changed the one kept")

        # The relay's refusals, before anything is fetched.
        st, why = fc.post("/ext/catalog/get", data={"id": "org.example;reboot"})
        ctx.check(st == 400, "an id with no such form -> %s %s", st, why)
        st, why = fc.post("/ext/catalog/get", data={"id": PROBE_ID})
        want = (404, "not in the catalog") if kept.get("index") else (409, "fetch it first")
        ev["get_unlisted"] = [st, why]
        ctx.check(st == want[0] and want[1] in json.dumps(why), "an id the kept index does not list -> %s %s", st, why)
        ctx.check(_forgeext("index").get("index") == kept.get("index"), "the refusals changed the index kept")
        ctx.check(_staged() == staged_before, "the staging directory holds %s, and held %s before", _staged(), staged_before)
    finally:
        shutil.rmtree(work, ignore_errors=True)
