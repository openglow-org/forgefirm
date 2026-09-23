# Copyright 2026 514 LLC d/b/a OpenGlow
# Written by Scott Wiederhold
# https://community.openglow.org
# SPDX-License-Identifier:    MIT

"""A package's page asking its own service, through the panel's relay.

Its own module rather than a test in exthost.py, for the reason extcore.py
gives: a test added there moves the fingerprint of every test in that
module. The helpers come from exthost, and importing them puts that
module's shared text in this test's fingerprint and not the other way
round. The package here is the reference package's id and key with a
service of its own, so exthost's put-back takes it away like the others.
"""

import json
import os
import stat

from ..catalog import test
from .exthost import (EXT_ROOT, FWUP, REF_ID, REF_KEY, _as_found, _forgeext, _put_back, _svc, _tree, _until,
                      _write)
from .setup import SAFETY_PHRASE, read_file, record_path, request

CALL_DIR = "/run/forgefirm/ext/call"

# The service: it says what it was handed, then answers its page's calls
# on the listening end the host gave it, one connection at a time.
CALL_SERVICE = r'''
import json, os, socket, stat, time
data = os.environ["FFX_DATA"]


def api(method, path):
    s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    s.settimeout(5)
    try:
        s.connect(os.environ["FFX_API"])
        s.sendall(("%s %s HTTP/1.1\r\nHost: forgeext\r\n\r\n" % (method, path)).encode())
        buf = b""
        while True:
            c = s.recv(65536)
            if not c:
                break
            buf += c
        return json.loads(buf.partition(b"\r\n\r\n")[2] or b"null")
    finally:
        s.close()


def kind(n):
    try:
        return "socket" if stat.S_ISSOCK(os.fstat(n).st_mode) else "other"
    except OSError:
        return "closed"


fd = os.environ.get("FFX_CALL_FD")
listener = socket.socket(fileno=int(fd)) if fd else None
report = {"call_env": fd, "fds": {str(n): kind(n) for n in (3, 4, 5)},
          "listening": listener.getsockopt(socket.SOL_SOCKET, socket.SO_ACCEPTCONN) if listener else None}
with open(os.path.join(data, "report.json.new"), "w") as f:
    json.dump(report, f)
os.rename(os.path.join(data, "report.json.new"), os.path.join(data, "report.json"))
print("page-call service up", flush=True)


def answer(conn, status, doc):
    body = json.dumps(doc).encode()
    conn.sendall(("HTTP/1.1 %d Answer\r\nContent-Type: application/json\r\nContent-Length: %d\r\n\r\n"
                  % (status, len(body))).encode() + body)


while listener:
    conn, _ = listener.accept()
    try:
        conn.settimeout(5)
        buf = b""
        while b"\r\n\r\n" not in buf:
            c = conn.recv(4096)
            if not c:
                break
            buf += c
        head, _, body = buf.partition(b"\r\n\r\n")
        lines = head.decode("latin-1").split("\r\n")
        method, path = lines[0].split()[:2]
        want = 0
        for line in lines[1:]:
            k, _, v = line.partition(":")
            if k.strip().lower() == "content-length":
                want = int(v)
        while len(body) < want:
            c = conn.recv(4096)
            if not c:
                break
            body += c
        if path == "/echo":
            answer(conn, 200, {"method": method, "path": path, "body": json.loads(body or b"{}")})
        elif path == "/refuse":
            answer(conn, 409, {"error": "not now"})
        elif path == "/me":
            answer(conn, 200, (api("GET", "/v0/self") or {}).get("id"))
        else:
            answer(conn, 404, {"error": "there is no " + path})
    except (OSError, ValueError):
        pass
    finally:
        conn.close()
while True:
    time.sleep(60)
'''

CALL_UI = "<!doctype html><title>forgetest page call</title>\n<p>the page that calls its own service</p>\n"


def _pack_caller(work):
    """The reference package's id and key, with a page and the service above: (archive, public key)."""
    import io
    import subprocess
    import tarfile
    manifest = {"manifest": 1, "id": REF_ID, "name": "forgetest page call", "version": "1.0.0",
                "author": "forgetest", "license": "MIT", "api": "0.1", "runtime": "python",
                "service": {"exec": "bin/caller.py"}, "capabilities": ["ui", "machine.read"]}
    payload = os.path.join(work, "payload.tar.gz")
    with tarfile.open(payload, "w:gz") as t:
        for name, text, mode in (("manifest.json", json.dumps(manifest), 0o644),
                                 ("bin/caller.py", CALL_SERVICE, 0o755), ("ui/index.html", CALL_UI, 0o644)):
            info = tarfile.TarInfo(name)
            data = text.encode()
            info.size, info.mode = len(data), mode
            t.addfile(info, io.BytesIO(data))
    conf = os.path.join(work, "fwup.conf")
    _write(conf, 'meta-product = "ForgeFIRM extension"\nmeta-description = "%s"\nmeta-version = "1.0.0"\n'
                 'meta-platform = "forgefirm-ext"\nfile-resource payload.tar.gz {\n    host-path = "%s"\n}\n'
           % (REF_ID, payload))
    key = os.path.join(work, REF_KEY)
    raw, signed = os.path.join(work, "raw.ffx"), os.path.join(work, "caller.ffx")
    for cmd in ([FWUP, "-g", "-o", key], [FWUP, "-c", "-f", conf, "-o", raw],
                [FWUP, "-S", "-s", key + ".priv", "-i", raw, "-o", signed]):
        subprocess.run(cmd, check=True, capture_output=True, timeout=60, cwd=work)
    return signed, key + ".pub"


@test("exthost.page-call", title="A package's page asks its own service, through the panel",
      subsystem="exthost", kind="auto", hardware="api", est_min=4,
      covers=[("forgeext", "src/call.*"), ("forgeext", "src/run.*"), ("forgeext", "src/sandbox.*"),
              ("forgeext", "src/main.c"), ("forgectrl", "src/extpkg.*"), ("forgectrl", "src/main.c")],
      requires=["exthost.service"],
      description="A package with a page and a service may have the one ask the other. The host binds the "
                  "service's call socket in /run/forgefirm/ext/call, a directory that is root's alone "
                  "(0700), as root's 0600 socket, and hands the service only the listening end, at "
                  "descriptor 4 (FFX_CALL_FD=4), with nothing at 3 or 5. POST /ext/call relays a call "
                  "through the host's command line: a POST's JSON object reaches the service and its "
                  "answer comes back as its status and its JSON; a GET carries no body; the service's own "
                  "refusal is its status and its words, not the relay's; a service may ask the machine "
                  "while it answers. The form is held before anything runs: a method that is not GET or "
                  "POST, a GET with a body, and a body that is not a JSON object are 400, and a path with "
                  ".. in it and a package that is not installed are the host's refusals, 409 in its words. "
                  "The command line answers alike. Disabled, the package's service stops, a call is refused "
                  "in words, and the socket's name is gone; enabled again, the service answers again. A "
                  "call to a frozen service and the relay's bridge in a browser are not this test's: "
                  "forgeext's sdk_test and the frame-isolation harness hold those. The package, the key, "
                  "the setting, and the setup record are put back as found.")
def page_call(ctx):
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
    work = tempfile.mkdtemp(prefix="forgetest-call.")
    owner_key = os.path.join(EXT_ROOT, "keys", REF_KEY + ".pub")
    sock = os.path.join(CALL_DIR, REF_ID + ".sock")
    report = os.path.join(EXT_ROOT, "data", REF_ID, "report.json")

    def call(method, path, body=None, id_=REF_ID):
        form = {"id": id_, "method": method, "path": path}
        if body is not None:
            form["body"] = body
        return fc.post("/ext/call", data=form)

    try:
        archive, pub = _pack_caller(work)
        shutil.copy(pub, owner_key)
        os.chmod(owner_key, 0o644)
        r = _forgeext("install", archive, "--consent-community")
        ctx.check(r.get("ok") is True, "the install of a package with a page and a service -> %s", r.get("error"))
        st, reply = fc.post("/settings", data={"ext_enabled": "1", "advisory": etag, "phrase": SAFETY_PHRASE})
        ctx.check(st == 200, "ext_enabled=1 -> %s %r", st, reply)
        up = _until(ctx, lambda: _svc(REF_ID).get("state") == "running" and os.path.exists(report), 60)
        ctx.check(up, "the service did not come up: %s", _svc(REF_ID))

        # What the service was handed, from inside, and the socket from outside.
        with open(report) as f:
            inside = json.load(f)
        ev["inside"] = inside
        ctx.log("inside: %s", inside)
        ctx.check(inside.get("call_env") == "4", "FFX_CALL_FD is %r, not 4", inside.get("call_env"))
        ctx.check(inside.get("fds") == {"3": "closed", "4": "socket", "5": "closed"},
                  "its descriptors 3, 4, 5: %s", inside.get("fds"))
        ctx.check(inside.get("listening") == 1, "descriptor 4 is not a listening socket: %s", inside.get("listening"))
        d, s = os.lstat(CALL_DIR), os.lstat(sock)
        ev["socket"] = {"dir": [oct(d.st_mode & 0o7777), d.st_uid], "sock": [oct(s.st_mode & 0o7777), s.st_uid]}
        ctx.check(stat.S_ISDIR(d.st_mode) and d.st_uid == 0 and d.st_mode & 0o7777 == 0o700,
                  "%s is not root's 0700 directory: %s", CALL_DIR, ev["socket"]["dir"])
        ctx.check(stat.S_ISSOCK(s.st_mode) and s.st_uid == 0 and s.st_mode & 0o777 == 0o600,
                  "%s is not root's 0600 socket: %s", sock, ev["socket"]["sock"])

        # Calls through the relay, as the panel's bridge makes them.
        st_, doc = call("POST", "/echo", '{"a": 1, "b": "two"}')
        ev["post"] = [st_, doc]
        ctx.log("POST /ext/call /echo -> %s %s", st_, doc)
        ctx.check(st_ == 200 and (doc or {}).get("status") == 200
                  and doc.get("body") == {"method": "POST", "path": "/echo", "body": {"a": 1, "b": "two"}},
                  "a POST -> %s %s", st_, doc)
        st_, doc = call("GET", "/echo")
        ev["get"] = [st_, doc]
        ctx.check(st_ == 200 and (doc or {}).get("body") == {"method": "GET", "path": "/echo", "body": {}},
                  "a GET -> %s %s", st_, doc)
        st_, doc = call("GET", "/refuse")
        ev["refuse"] = [st_, doc]
        ctx.check(st_ == 200 and (doc or {}).get("status") == 409 and doc.get("body") == {"error": "not now"},
                  "the service's own refusal -> %s %s", st_, doc)
        st_, doc = call("GET", "/me")
        ev["me"] = [st_, doc]
        ctx.check(st_ == 200 and (doc or {}).get("body") == REF_ID, "a service asking the machine while it answers "
                  "-> %s %s", st_, doc)
        for args, want, words in ((("PUT", "/echo"), 400, "a call is GET or POST"),
                                  (("GET", "/echo", "{}"), 400, "a GET call has no body"),
                                  (("POST", "/echo", "[1]"), 400, "a call's body is a JSON object"),
                                  (("GET", "/../etc"), 409, "a call's path starts with"),
                                  (("GET", "/echo", None, "org.forgetest.nothere"), 409, "that package is not installed")):
            st_, why = call(*args)
            ev.setdefault("refusals", []).append([list(args), st_, why])
            ctx.check(st_ == want and words in json.dumps(why), "%s -> %s %s, wanted %s %s", args, st_, why, want, words)

        # The command line answers alike.
        r = _forgeext("call", REF_ID, "POST", "/echo", '{"from": "the console"}')
        ev["cli"] = r
        ctx.check(r.get("ok") is True and r.get("status") == 200
                  and (r.get("body") or {}).get("body") == {"from": "the console"}, "forgeext call -> %s", r)

        # Disabled: no service, no socket, a refusal in words; and back.
        r = _forgeext("disable", REF_ID)
        ctx.check(r.get("ok") is True, "disable -> %s", r.get("error"))
        st_, why = call("GET", "/echo")
        ev["disabled"] = [st_, why]
        ctx.check(st_ == 409 and "disabled" in json.dumps(why), "a call to a disabled package -> %s %s", st_, why)
        gone = _until(ctx, lambda: not os.path.exists(sock) and _svc(REF_ID).get("state") != "running", 30)
        ctx.check(gone, "disabled, the socket's name is still there or the service still runs: %s", _svc(REF_ID))
        r = _forgeext("enable", REF_ID)
        ctx.check(r.get("ok") is True, "enable -> %s", r.get("error"))
        back = _until(ctx, lambda: _svc(REF_ID).get("state") == "running" and os.path.exists(sock), 60)
        ctx.check(back, "enabled again, the service did not come back: %s", _svc(REF_ID))
        st_, doc = _until(ctx, lambda: (lambda a: a if a[0] == 200 else None)(call("GET", "/echo")), 20) or (0, None)
        ctx.check(st_ == 200 and (doc or {}).get("status") == 200, "enabled again, it answers -> %s %s", st_, doc)
    finally:
        _put_back(ctx, fc, work, prior, etag, raw, dir_mode)
    ctx.check(not os.path.exists(sock), "the call socket's name is left after the package went")
    _as_found(ctx, fc, prior, raw, dir_mode, found_tree)
