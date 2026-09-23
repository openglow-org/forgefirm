# Copyright 2026 514 LLC d/b/a OpenGlow
# Written by Scott Wiederhold
# https://community.openglow.org
# SPDX-License-Identifier:    MIT

"""The host's own events: a service is told before it is stopped.

Its own module, for the reason extcore.py gives. The package here is the
reference package's id and key with a service of its own, so exthost's
put-back takes it away like the others.
"""

import json
import os
import subprocess
import time

from ..catalog import test
from .exthost import (EXT_ROOT, FWUP, REF_ID, REF_KEY, _as_found, _forgeext, _put_back, _svc, _tree, _until, _write)
from .setup import SAFETY_PHRASE, read_file, record_path, request

# The service: it follows the host's event feed and keeps every host event it reads, with the
# monotonic time it read it, in its data directory.
LIFE_SERVICE = r'''
import json, os, socket, time
data = os.environ["FFX_DATA"]


def api(body):
    s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    s.settimeout(40)
    try:
        s.connect(os.environ["FFX_API"])
        payload = json.dumps(body).encode()
        s.sendall(b"POST /v0/events HTTP/1.1\r\nHost: forgeext\r\nContent-Type: application/json\r\n"
                  b"Content-Length: %d\r\n\r\n" % len(payload) + payload)
        buf = b""
        while True:
            c = s.recv(65536)
            if not c:
                break
            buf += c
        return json.loads(buf.partition(b"\r\n\r\n")[2] or b"null")
    finally:
        s.close()


def keep(name, doc):
    with open(os.path.join(data, name + ".new"), "w") as f:
        json.dump(doc, f)
    os.rename(os.path.join(data, name + ".new"), os.path.join(data, name))


place = api({})["next"]
keep("ready.json", {"place": place, "t": time.monotonic()})
got = []
while True:
    ans = api({"since": place, "wait": 20})
    for e in ans.get("events") or []:
        if e.get("event", "").startswith("ext."):
            got.append({"event": e["event"], "data": e.get("data"), "t": time.monotonic()})
            keep("host-events.json", got)
    place = ans.get("next", place)
'''


def _pack_life(work):
    import io
    import tarfile
    manifest = {"manifest": 1, "id": REF_ID, "name": "forgetest lifecycle", "version": "1.0.0",
                "author": "forgetest", "license": "MIT", "api": "0.1", "runtime": "python",
                "service": {"exec": "bin/life.py"}, "capabilities": ["events"]}
    payload = os.path.join(work, "payload.tar.gz")
    with tarfile.open(payload, "w:gz") as t:
        for name, text, mode in (("manifest.json", json.dumps(manifest), 0o644), ("bin/life.py", LIFE_SERVICE, 0o755)):
            info = tarfile.TarInfo(name)
            data = text.encode()
            info.size, info.mode = len(data), mode
            t.addfile(info, io.BytesIO(data))
    conf = os.path.join(work, "fwup.conf")
    _write(conf, 'meta-product = "ForgeFIRM extension"\nmeta-description = "%s"\nmeta-version = "1.0.0"\n'
                 'meta-platform = "forgefirm-ext"\nfile-resource payload.tar.gz {\n    host-path = "%s"\n}\n'
           % (REF_ID, payload))
    key = os.path.join(work, REF_KEY)
    raw, signed = os.path.join(work, "raw.ffx"), os.path.join(work, "life.ffx")
    for cmd in ([FWUP, "-g", "-o", key], [FWUP, "-c", "-f", conf, "-o", raw],
                [FWUP, "-S", "-s", key + ".priv", "-i", raw, "-o", signed]):
        subprocess.run(cmd, check=True, capture_output=True, timeout=60, cwd=work)
    return signed, key + ".pub"


@test("exthost.lifecycle", title="A package is told, in the feed it reads, before the host stops it",
      subsystem="exthost", kind="auto", hardware="api", est_min=3,
      covers=[("forgeext", "src/run.*"), ("forgeext", "src/machine.*"), ("forgeext", "src/evfeed.*"),
              ("forgeext", "src/api.*")],
      requires=["exthost.service"],
      description="A package that holds events reads the host's own events beside the machine's. With the "
                  "package running and following its feed, extensions are turned off: the host puts "
                  "ext.shutdown into the feed with the reason (\"extensions are off (ext_enabled)\"), and "
                  "stops the service a second later, so that the package has read it - the event is in its "
                  "data directory, read before its process ended. Holds do not wait for that second: they "
                  "follow extensions off at once, as exthost.hold-pause-tier holds. ext.will_freeze and "
                  "ext.thawed, which an armed window brings, are the automation package's host test's "
                  "(automation_test.py, where a service with job_time.run reads both). The package, the "
                  "key, the setting, and the setup record are put back as found.")
def lifecycle(ctx):
    import shutil
    import tempfile
    fc = ctx.forgectrl
    ev = ctx.evidence
    ctx.check(fc.wait_idle(timeout=30, abort=ctx.aborted), "machine not idle: settings are locked")
    prior = fc.settings().get("ext_enabled") or ""
    raw = read_file(record_path())
    found_tree = _tree(EXT_ROOT)
    dir_mode = os.stat(os.path.dirname(EXT_ROOT)).st_mode & 0o7777
    st, body, hdrs = request(fc.base, "GET", "/advisories/extensions", headers={"Host": fc.host_header()})
    etag = hdrs.get("etag")
    work = tempfile.mkdtemp(prefix="forgetest-life.")
    owner_key = os.path.join(EXT_ROOT, "keys", REF_KEY + ".pub")
    data = os.path.join(EXT_ROOT, "data", REF_ID)

    def doc(name):
        try:
            with open(os.path.join(data, name)) as f:
                return json.load(f)
        except (OSError, ValueError):
            return None

    try:
        archive, pub = _pack_life(work)
        shutil.copy(pub, owner_key)
        os.chmod(owner_key, 0o644)
        r = _forgeext("install", archive, "--consent-community")
        ctx.check(r.get("ok") is True, "the install -> %s", r.get("error"))
        st, reply = fc.post("/settings", data={"ext_enabled": "1", "advisory": etag, "phrase": SAFETY_PHRASE})
        ctx.check(st == 200, "ext_enabled=1 -> %s %r", st, reply)
        ready = _until(ctx, lambda: _svc(REF_ID).get("state") == "running" and doc("ready.json"), 60)
        ctx.check(ready, "the service did not come up and follow its feed: %s", _svc(REF_ID))
        time.sleep(1.0)
        pid = _svc(REF_ID).get("pid")
        st, reply = fc.post("/settings", data={"ext_enabled": "0"})
        ctx.check(st == 200, "ext_enabled=0 -> %s %r", st, reply)
        stopped = _until(ctx, lambda: _svc(REF_ID).get("state") != "running" and time.monotonic(), 20)
        ctx.check(stopped, "the service was not stopped: %s", _svc(REF_ID))
        ctx.check(not os.path.exists("/proc/%s" % pid), "its process %s outlived the stop", pid)
        got = doc("host-events.json") or []
        ev["host_events"] = got
        ctx.log("what it read: %s", got)
        ctx.check(len(got) >= 1 and got[0].get("event") == "ext.shutdown"
                  and (got[0].get("data") or {}).get("reason") == "extensions are off (ext_enabled)",
                  "it read ext.shutdown, with the reason, before it was stopped: %s", got)
    finally:
        _put_back(ctx, fc, work, prior, etag, raw, dir_mode)
    _as_found(ctx, fc, prior, raw, dir_mode, found_tree)
