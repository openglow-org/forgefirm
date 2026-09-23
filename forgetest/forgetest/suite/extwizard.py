# Copyright 2026 514 LLC d/b/a OpenGlow
# Written by Scott Wiederhold
# https://community.openglow.org
# SPDX-License-Identifier:    MIT

"""A package's own check on the Setup page, on the machine.

Its own module, for the reason extcore.py gives. The package is the
reference package's id and key with a service of its own that runs the
check on its call socket, so exthost's put-back takes it away like the
others.
"""

import json
import os
import subprocess

from ..catalog import test
from .exthost import (EXT_ROOT, FWUP, REF_ID, REF_KEY, _as_found, _forgeext, _put_back, _svc, _tree, _until, _write)
from .setup import SAFETY_PHRASE, read_file, record_path, request

# The service: the check's steps on the call socket the host hands it (fd 4), its own record of whether the
# check passed, and every request it was asked, kept in its data directory.
WZ_SERVICE = r'''
import json, os, socket
data = os.environ["FFX_DATA"]
lst = socket.socket(fileno=int(os.environ["FFX_CALL_FD"]))
state = {"done": False, "calls": []}
while True:
    c, _ = lst.accept()
    try:
        buf = b""
        while b"\r\n\r\n" not in buf:
            k = c.recv(4096)
            if not k:
                break
            buf += k
        head, _, body = buf.partition(b"\r\n\r\n")
        n = 0
        for line in head.split(b"\r\n")[1:]:
            k, _, v = line.partition(b":")
            if k.strip().lower() == b"content-length":
                n = int(v)
        while len(body) < n:
            k = c.recv(4096)
            if not k:
                break
            body += k
        req = json.loads(body[:n] or b"{}")
        method, path = head.split(b" ")[0].decode(), head.split(b" ")[1].decode()
        if method == "GET" and path == "/wizard":
            ans = {"title": "forgetest check", "done": state["done"]}
        elif path == "/wizard/start":
            ans = {"log": ["the check begins"], "phase": "asking", "progress": 50,
                   "prompt": {"kind": "confirm", "id": "ready", "text": "Is the machine ready?"}}
        elif path == "/wizard/answer":
            ok = req.get("id") == "ready" and req.get("answer") == "yes"
            state["done"] = ok
            ans = {"log": ["answered " + str(req.get("answer"))], "result": {"ok": ok, "summary": "ready" if ok else "not ready"}}
        elif path == "/wizard/abort":
            ans = {}
        else:
            ans = {"error": "no such call"}
        state["calls"].append([method, path, req])
        with open(os.path.join(data, "check.json.new"), "w") as f:
            json.dump(state, f)
        os.rename(os.path.join(data, "check.json.new"), os.path.join(data, "check.json"))
        out = json.dumps(ans).encode()
        c.sendall(b"HTTP/1.1 200 X\r\nContent-Type: application/json\r\nContent-Length: %d\r\nConnection: close\r\n\r\n"
                  % len(out) + out)
    except Exception:
        pass
    finally:
        c.close()
'''


def _pack_check(work):
    import io
    import tarfile
    manifest = {"manifest": 1, "id": REF_ID, "name": "forgetest check", "version": "1.0.0",
                "author": "forgetest", "license": "MIT", "api": "0.1", "runtime": "python",
                "service": {"exec": "bin/check.py"}, "capabilities": ["wizard"]}
    payload = os.path.join(work, "payload.tar.gz")
    with tarfile.open(payload, "w:gz") as t:
        for name, text, mode in (("manifest.json", json.dumps(manifest), 0o644), ("bin/check.py", WZ_SERVICE, 0o755)):
            info = tarfile.TarInfo(name)
            data = text.encode()
            info.size, info.mode = len(data), mode
            t.addfile(info, io.BytesIO(data))
    conf = os.path.join(work, "fwup.conf")
    _write(conf, 'meta-product = "ForgeFIRM extension"\nmeta-description = "%s"\nmeta-version = "1.0.0"\n'
                 'meta-platform = "forgefirm-ext"\nfile-resource payload.tar.gz {\n    host-path = "%s"\n}\n'
           % (REF_ID, payload))
    key = os.path.join(work, REF_KEY)
    raw, signed = os.path.join(work, "raw.ffx"), os.path.join(work, "check.ffx")
    for cmd in ([FWUP, "-g", "-o", key], [FWUP, "-c", "-f", conf, "-o", raw],
                [FWUP, "-S", "-s", key + ".priv", "-i", raw, "-o", signed]):
        subprocess.run(cmd, check=True, capture_output=True, timeout=60, cwd=work)
    return signed, key + ".pub"


@test("exthost.wizard", title="A package's own check runs on the Setup page, and the machine's setup is untouched",
      subsystem="exthost", kind="auto", est_min=3,
      covers=[("forgectrl", "src/wizpkg.*"), ("forgectrl", "src/wizdark.c"), ("forgectrl", "src/wiz.c"),
              ("forgectrl", "src/extpkg.*"), ("forgectrl", "src/ui/wizard.js"), ("forgeext", "src/main.c"),
              ("forgeext", "src/run.*"), ("forgeext", "src/caps.*"), ("forgeext", "src/manifest.*")],
      requires=["exthost.service"],
      description="A package that asks for wizard is installed and turned on; its service runs a check on its "
                  "call socket and keeps whether it passed. GET /wiz lists it among the extensions as "
                  "pkg:<id> with the title the package gives it, not done. POST /wiz/pkg:<id>/start runs it on "
                  "the wizard runner: GET /wiz/dark shows the package's log line, phase, progress, and its "
                  "confirm prompt, and the machine lease stays free while it waits (a package's check holds the "
                  "machine no more than the package does). The operator's Yes reaches the service as the answer "
                  "yes, the run ends complete with the package's summary, and GET /wiz then lists the check as "
                  "done, from the package's own answer. The machine's setup record is the same before and after: "
                  "no pkg: wizard in it and the gate as it was. A second run is aborted from its prompt and the "
                  "service is told. A check of a package that is not installed is 404. Everything is put back "
                  "as exthost.service puts it back.")
def wizard(ctx):
    import shutil
    import tempfile
    fc = ctx.forgectrl
    ev = ctx.evidence
    ctx.check(fc.wait_idle(timeout=30, abort=ctx.aborted), "machine not idle")
    prior = fc.settings().get("ext_enabled") or ""
    raw = read_file(record_path())
    found_tree = _tree(EXT_ROOT)
    dir_mode = os.stat(os.path.dirname(EXT_ROOT)).st_mode & 0o7777
    st, body, hdrs = request(fc.base, "GET", "/advisories/extensions", headers={"Host": fc.host_header()})
    etag = hdrs.get("etag")
    work = tempfile.mkdtemp(prefix="forgetest-ffx.")
    owner_key = os.path.join(EXT_ROOT, "keys", REF_KEY + ".pub")
    wid = "pkg:" + REF_ID
    check_file = os.path.join(EXT_ROOT, "data", REF_ID, "check.json")

    def own():
        try:
            return json.loads(read_file(check_file) or "{}")
        except ValueError:
            return {}

    def listed():
        st_, w = fc.get("/wiz")
        return next((e for e in (w.get("extensions") or []) if e.get("id") == wid), None) if isinstance(w, dict) else None

    def dark():
        st_, d = fc.get("/wiz/dark")
        return d if isinstance(d, dict) else {}

    def record_view():
        try:
            rec = json.loads(read_file(record_path()) or "{}")
        except ValueError:
            return None
        return {"wizards": sorted((rec.get("wizards") or {}).keys()), "completed": rec.get("completed")}

    before = record_view()
    gate_before = fc.get("/wiz")[1].get("gate")
    try:
        archive, pub = _pack_check(work)
        shutil.copy(pub, owner_key)
        os.chmod(owner_key, 0o644)
        r = _forgeext("install", archive, "--consent-community")
        ctx.check(r.get("ok") is True, "the install -> %s", r.get("error"))
        st, reply = fc.post("/settings", data={"ext_enabled": "1", "advisory": etag, "phrase": SAFETY_PHRASE})
        ctx.check(st == 200, "ext_enabled=1 -> %s %r", st, reply)
        x = _until(ctx, lambda: _svc(REF_ID) if _svc(REF_ID).get("state") == "running" else None, 90, poll=0.5)
        ctx.check(x, "the service is not running: %s", _svc(REF_ID))

        e = _until(ctx, listed, 30, poll=1.0)
        ev["listed"] = e
        ctx.check(e and e.get("title") == "forgetest check" and e.get("done") is False,
                  "GET /wiz does not list the package's check, not done: %s", e)

        st, body = fc.post("/wiz/%s/start" % wid)
        ctx.check(st == 200, "POST /wiz/%s/start -> %s %s", wid, st, body)
        d = _until(ctx, lambda: dark() if (dark().get("prompt") or {}).get("id") == "ready" else None, 20, poll=0.3)
        ev["prompt"] = d
        ctx.check(d and d.get("running") and d["prompt"].get("kind") == "confirm"
                  and any(l.endswith("the check begins") for l in (d.get("log") or [])) and d.get("progress") == 50,
                  "the check's prompt, log, and progress: %s", d)
        lease = (fc.status().get("lease") or {}).get("holder")
        ev["lease_while_waiting"] = lease
        ctx.check(not lease, "a package's check holds the machine: %s", lease)
        st, body = fc.post("/wiz/%s/answer" % wid, data={"seq": str(d["prompt"]["seq"]), "value": "Yes"})
        ctx.check(st == 200, "the answer -> %s %s", st, body)
        end = _until(ctx, lambda: dark() if not dark().get("running") else None, 20, poll=0.3)
        ev["end"] = end
        ctx.log("the check ended: %s", end)
        ctx.check(end and not end.get("error") and (end.get("result") or {}).get("summary") == "ready",
                  "the check did not end complete with the package's summary: %s", end)
        mine = own()
        ctx.check(mine.get("done") is True and ["POST", "/wizard/answer", {"id": "ready", "answer": "yes"}] in
                  (mine.get("calls") or []), "the service was not answered yes: %s", mine)
        ctx.sleep(11.0)                 # the Setup page's list is asked again at most every 10 s
        e = listed()
        ev["listed_after"] = e
        ctx.check(e and e.get("done") is True, "GET /wiz does not list the check as done: %s", e)

        after = record_view()
        ev["record"] = {"before": before, "after": after}
        ctx.check(after == before and not any(w.startswith("pkg:") for w in (after or {}).get("wizards", [])),
                  "the machine's setup record changed: %s -> %s", before, after)
        ctx.check(fc.get("/wiz")[1].get("gate") == gate_before, "the setup gate moved")

        # A second run, aborted from its prompt.
        st, body = fc.post("/wiz/%s/start" % wid)
        d = _until(ctx, lambda: dark() if (dark().get("prompt") or {}).get("id") == "ready" else None, 20, poll=0.3)
        ctx.check(st == 200 and d, "the second run did not reach its prompt: %s %s", st, body)
        fc.post("/wiz/%s/abort" % wid)
        end = _until(ctx, lambda: dark() if not dark().get("running") else None, 20, poll=0.3)
        ctx.check(end and end.get("error") == "aborted", "the abort: %s", end)
        ctx.check(["POST", "/wizard/abort", {}] in (own().get("calls") or []), "the service was not told of the abort")

        st, body = fc.post("/wiz/pkg:org.forgetest.nothere/start")
        ctx.check(st == 404, "a check of a package that is not installed -> %s %s", st, body)
    finally:
        # A check that failed mid-run leaves the package's check waiting at its prompt.
        if dark().get("running") and dark().get("id") == wid:
            fc.post("/wiz/%s/abort" % wid)
            _until(ctx, lambda: not dark().get("running"), 20, poll=0.3)
        _put_back(ctx, fc, work, prior, etag, raw, dir_mode)
    _as_found(ctx, fc, prior, raw, dir_mode, found_tree)
