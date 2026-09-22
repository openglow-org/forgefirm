# Copyright 2026 514 LLC d/b/a OpenGlow
# Written by Scott Wiederhold
# https://community.openglow.org
# SPDX-License-Identifier:    MIT

"""What the extension host makes of the firmware's own version file.

A campaign only ever runs on a dev image - the release image does not
ship this suite - and a dev image stamps ${DATETIME} (dev), which is no
version at all. So the branch where a core range is actually judged
cannot be reached from here by any machine this suite runs on, and it is
not this module's to prove: that is forgeext's version_test, over the
file as each image build writes it. What is left here is the half a
machine can show, and it is the half that ties the daemon to the image:
that the host reads the image's own file, finds a build stamp, and
judges nothing rather than guessing.

Its own module rather than a test in exthost.py: a test's fingerprint
holds the shared text of the module it lives in, and the blank lines
between tests are shared text, so a test added to exthost.py moves the
fingerprint of every other test there and costs a campaign a re-run of
each - exthost.panel-install among them, which is an operator drill with
the button held. The helpers come from exthost, and importing them puts
that module's shared text in this test's fingerprint and not the other
way round.
"""

import json
import os
import subprocess

from ..catalog import test
from .exthost import FWUP, _forgeext, _read, _write


@test("exthost.core-range", title="The host reads the image's own version file and judges by it",
      subsystem="exthost", kind="auto", est_min=2,
      covers=[("forgeext", "src/install.*"), ("forgeext", "src/manifest.*"), ("forgeext", "src/main.c")],
      description="The firmware's version reaches the host from /etc/forgefirm-version, which the image "
                  "build writes. This test asks the host what it made of that file rather than reading the "
                  "file and believing it. On the dev image a campaign runs on, the first word is a build "
                  "stamp and no version: inspect answers core_checked false, and a package asking for a "
                  "minimum far above or a maximum far below is accepted all the same, because a range "
                  "cannot be judged against a stamp and the host does not guess one. A dev image that "
                  "began stamping something version-shaped would fail here, which is the regression this "
                  "test is for; the judging branch belongs to forgeext's version_test, since no image "
                  "carrying this suite can reach it. Nothing is installed, nothing runs, and nothing "
                  "moves: inspect changes nothing and the work directory goes at the end.")
def core_range(ctx):
    import io
    import re
    import shutil
    import tarfile
    import tempfile
    ev = ctx.evidence

    stamp = (_read("/etc/forgefirm-version", "").split() or [""])[0]
    ctx.check(stamp, "/etc/forgefirm-version says nothing: the image did not stamp itself")
    version = stamp[1:] if stamp[:1] == "v" and stamp[1:2].isdigit() else stamp
    looks_like_a_version = bool(re.match(r"^\d+\.\d+\.\d+(-[0-9A-Za-z.-]+)?$", version))
    ctx.log("/etc/forgefirm-version: %r -> %r", stamp, version)
    ev["firmware_version"] = {"file": stamp, "version": version, "looks_like_a_version": looks_like_a_version}

    # The suite ships on the dev image alone, so this is the only shape a
    # campaign can meet. If it ever is not, the image changed and the
    # judging path needs a proof that is not here.
    ctx.check(not looks_like_a_version,
              "this image stamped %r, which is a version: a campaign cannot run on a release image, so "
              "either the dev image's stamp changed or this is not the image this suite belongs on", stamp)

    work = tempfile.mkdtemp(prefix="forgetest-core-")
    try:
        def packed(core):
            """A data package that asks for nothing, with the core range given."""
            m = {"manifest": 1, "id": "org.forgetest.corerange", "name": "forgetest core range",
                 "version": "1.0.0", "author": "forgetest", "license": "MIT", "api": "0.1",
                 "runtime": "data", "capabilities": []}
            if core:
                m["core"] = core
            payload = os.path.join(work, "payload.tar.gz")
            with tarfile.open(payload, "w:gz") as t:
                data = json.dumps(m).encode()
                info = tarfile.TarInfo("manifest.json")
                info.size, info.mode = len(data), 0o644
                t.addfile(info, io.BytesIO(data))
            conf = os.path.join(work, "fwup.conf")
            _write(conf, 'meta-product = "ForgeFIRM extension"\nmeta-description = "core range"\n'
                         'meta-version = "1.0.0"\nmeta-platform = "forgefirm-ext"\n'
                         'file-resource payload.tar.gz {\n    host-path = "%s"\n}\n' % payload)
            out = os.path.join(work, "corerange.ffx")
            if os.path.exists(out):
                os.remove(out)
            subprocess.run([FWUP, "-c", "-f", conf, "-o", out], check=True, capture_output=True,
                           timeout=60, cwd=work)
            return out

        r = _forgeext("inspect", packed(None))
        ctx.check(r.get("ok") is True, "a package with no core range was refused: %s", r.get("error"))
        ctx.check(r.get("core_checked") is False,
                  "the host judged a range against the stamp %r: core_checked %s", stamp, r.get("core_checked"))
        ev["no_range"] = r

        # A stamp is no version, so neither end of a range is judged and
        # neither refuses the package. The host says so rather than
        # refusing on a comparison it cannot make.
        ev["ranges"] = []
        for core in ({"min": "99.0.0"}, {"max": "0.0.1"}, {"min": "0.0.1", "max": "0.0.2"}):
            r = _forgeext("inspect", packed(core))
            ctx.check(r.get("ok") is True and r.get("core_checked") is False,
                      "the host judged %s against the stamp anyway: %s", core, r)
            ev["ranges"].append({"core": core, "inspect": r})

        ctx.check("org.forgetest.corerange" not in
                  [x.get("id") for x in (_forgeext("list").get("packages") or [])],
                  "inspect installed the package: it must change nothing")
    finally:
        shutil.rmtree(work, ignore_errors=True)
