# Copyright 2026 514 LLC d/b/a OpenGlow
# Written by Scott Wiederhold
# https://community.openglow.org
# SPDX-License-Identifier:    MIT

"""The destinations the operator names for a package that asks for them.

Its own module rather than a test in exthost.py, for the reason extcore.py
gives. The package here is the reference package's id and key with a
service of its own, so exthost's put-back takes it away like the others.

What it dials is the machine's own DNS resolver on TCP port 53: an address
every machine on a network has, that is not the machine, and that answers.
Named by address, it gives the service no resolver of its own, so the
only way through to it is the one the operator named.
"""

import json
import os
import socket
import subprocess

from ..catalog import test
from .exthost import (EXT_ROOT, FWUP, NFT, REF_ID, REF_KEY, _as_found, _forgeext, _put_back, _read, _svc, _tree,
                      _until, _write)
from .forgectrl import lan_ip
from .setup import SAFETY_PHRASE, read_file, record_path, request

# The service: at every start, where it may connect and what one dial does.
DEST_SERVICE = r'''
import errno, json, os, socket, sys, time
data, target, port = os.environ["FFX_DATA"], sys.argv[1], int(sys.argv[2])


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


def dial(p):
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.settimeout(4)
    try:
        s.connect((target, p))
        return "connected"
    except socket.timeout:
        return "timeout"
    except OSError as e:
        return errno.errorcode.get(e.errno, str(e.errno))
    finally:
        s.close()


report = {"pid": os.getpid(), "destinations": (api("GET", "/v0/self") or {}).get("destinations"),
          "dial": dial(port), "dial_other_port": dial(port + 1)}
with open(os.path.join(data, "report.json.new"), "w") as f:
    json.dump(report, f)
os.rename(os.path.join(data, "report.json.new"), os.path.join(data, "report.json"))
print("destinations service up: %s" % report, flush=True)
while True:
    time.sleep(60)
'''


def _resolver():
    """The first IPv4 nameserver of /etc/resolv.conf, or None."""
    for line in _read("/etc/resolv.conf").splitlines():
        parts = line.split()
        if len(parts) >= 2 and parts[0] == "nameserver" and parts[1].count(".") == 3:
            return parts[1]
    return None


def _pack_dest(work, target, port):
    """The reference package's id and key, asking for the operator's destinations: (archive, public key)."""
    import io
    import tarfile
    manifest = {"manifest": 1, "id": REF_ID, "name": "forgetest destinations", "version": "1.0.0",
                "author": "forgetest", "license": "MIT", "api": "0.1", "runtime": "python",
                "service": {"exec": "bin/dest.py", "args": [target, str(port)]},
                "capabilities": ["net.outbound.operator"]}
    payload = os.path.join(work, "payload.tar.gz")
    with tarfile.open(payload, "w:gz") as t:
        for name, text, mode in (("manifest.json", json.dumps(manifest), 0o644), ("bin/dest.py", DEST_SERVICE, 0o755)):
            info = tarfile.TarInfo(name)
            data = text.encode()
            info.size, info.mode = len(data), mode
            t.addfile(info, io.BytesIO(data))
    conf = os.path.join(work, "fwup.conf")
    _write(conf, 'meta-product = "ForgeFIRM extension"\nmeta-description = "%s"\nmeta-version = "1.0.0"\n'
                 'meta-platform = "forgefirm-ext"\nfile-resource payload.tar.gz {\n    host-path = "%s"\n}\n'
           % (REF_ID, payload))
    key = os.path.join(work, REF_KEY)
    raw, signed = os.path.join(work, "raw.ffx"), os.path.join(work, "dest.ffx")
    for cmd in ([FWUP, "-g", "-o", key], [FWUP, "-c", "-f", conf, "-o", raw],
                [FWUP, "-S", "-s", key + ".priv", "-i", raw, "-o", signed]):
        subprocess.run(cmd, check=True, capture_output=True, timeout=60, cwd=work)
    return signed, key + ".pub"


@test("exthost.operator-destinations", title="The operator names where a package may connect, and takes it away",
      subsystem="exthost", kind="auto", hardware="api", est_min=4,
      covers=[("forgeext", "src/install.*"), ("forgeext", "src/state.*"), ("forgeext", "src/run.*"),
              ("forgeext", "src/super.*"), ("forgeext", "src/api.*"), ("forgeext", "src/caps.*"),
              ("forgeext", "src/manifest.*"), ("forgeext", "src/netrules.*"), ("forgeext", "src/main.c"),
              ("forgectrl", "src/extpkg.*"), ("forgectrl", "src/main.c")],
      requires=["exthost.service"],
      description="A package that asks for net.outbound.operator may reach the places the operator names for it "
                  "and nothing else. Installed, it has none: its own GET /v0/self names no destination and its "
                  "dial to the machine's DNS resolver on TCP port 53 is refused inside the sandbox (EACCES). "
                  "The operator names that resolver through POST /ext/dest; the host starts the service again "
                  "(said in its log, and no crash), its answer names the destination, its dial connects, the "
                  "next port up is still refused, and the account's chain in the rule table carries the "
                  "address. The machine's own LAN address and loopback are refused in words, a destination "
                  "out of form is 400, and taking away one that was never named is refused. Taken away, the "
                  "service starts again with no destination, the dial is refused again, and the chain no longer "
                  "carries the address. The package, the key, the setting, and the setup record are put back as "
                  "found.")
def operator_destinations(ctx):
    import shutil
    import tempfile
    fc = ctx.forgectrl
    ev = ctx.evidence
    target = _resolver()
    ctx.check(target, "/etc/resolv.conf names no IPv4 resolver: there is nothing to dial")
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.settimeout(4)
    try:
        s.connect((target, 53))
        reachable = True
    except OSError as e:
        reachable = str(e)
    finally:
        s.close()
    ev["resolver"] = {"address": target, "tcp_53_from_root": reachable}
    ctx.check(reachable is True, "the resolver %s does not take a TCP connection on port 53 even from outside the "
                                 "sandbox (%s): this network cannot show a way out opening", target, reachable)
    ctx.check(fc.wait_idle(timeout=30, abort=ctx.aborted), "machine not idle: settings are locked")
    prior = fc.settings().get("ext_enabled") or ""
    raw = read_file(record_path())
    found_tree = _tree(EXT_ROOT)
    dir_mode = os.stat(os.path.dirname(EXT_ROOT)).st_mode & 0o7777
    st, body, hdrs = request(fc.base, "GET", "/advisories/extensions", headers={"Host": fc.host_header()})
    etag = hdrs.get("etag")
    work = tempfile.mkdtemp(prefix="forgetest-dest.")
    owner_key = os.path.join(EXT_ROOT, "keys", REF_KEY + ".pub")
    report = os.path.join(EXT_ROOT, "data", REF_ID, "report.json")
    where = "%s:53" % target

    def seen():
        try:
            with open(report) as f:
                return json.load(f)
        except (OSError, ValueError):
            return None

    def fresh(than):
        r = seen()
        return r if r and r.get("pid") != than and _svc(REF_ID).get("state") == "running" else None

    def chain_has(addr):
        """Does the package's own chain let it through to addr on TCP 53? No chain is no."""
        acct = _svc(REF_ID).get("account") or ""
        if not acct.startswith("ffx"):
            return False
        out = subprocess.run([NFT, "list", "chain", "inet", "ffx", "u%d" % (800 + int(acct[3:]))], capture_output=True,
                             text=True, timeout=30).stdout
        return ("daddr %s tcp dport 53 accept" % addr) in out

    try:
        archive, pub = _pack_dest(work, target, 53)
        shutil.copy(pub, owner_key)
        os.chmod(owner_key, 0o644)
        r = _forgeext("install", archive, "--consent-community")
        ctx.check(r.get("ok") is True, "the install of a package asking for the operator's destinations -> %s",
                  r.get("error"))
        st, reply = fc.post("/settings", data={"ext_enabled": "1", "advisory": etag, "phrase": SAFETY_PHRASE})
        ctx.check(st == 200, "ext_enabled=1 -> %s %r", st, reply)
        first = _until(ctx, lambda: fresh(None), 60)
        ctx.check(first, "the service did not come up: %s", _svc(REF_ID))
        ev["none_named"] = first
        ctx.log("none named: %s", first)
        ctx.check(first.get("destinations") == [] and first.get("dial") == "EACCES",
                  "with nothing named it may reach nothing, and the sandbox refuses the dial: %s", first)
        ctx.check(not chain_has(target), "the rule table carries %s before the operator named it", target)

        # Refusals, in the host's words or the relay's.
        refusals = []
        for dest, want, words in (("%s:80" % lan_ip(), 409, "is this machine"), ("127.0.0.1:80", 409, "is this machine"),
                                  ("--root", 400, "host:port"), ("%s" % target, 409, "host:port")):
            st_, why = fc.post("/ext/dest", data={"id": REF_ID, "action": "add", "dest": dest})
            refusals.append([dest, st_, why])
            ctx.check(st_ == want and words in json.dumps(why), "add %s -> %s %s, wanted %s %s", dest, st_, why, want, words)
        st_, why = fc.post("/ext/dest", data={"id": REF_ID, "action": "remove", "dest": where})
        refusals.append(["remove " + where, st_, why])
        ctx.check(st_ == 409 and "is not one of" in json.dumps(why), "taking away one never named -> %s %s", st_, why)
        ev["refusals"] = refusals

        # Named: started again with its way out.
        st_, doc = fc.post("/ext/dest", data={"id": REF_ID, "action": "add", "dest": where})
        mine = [p for p in (doc or {}).get("packages", []) if p.get("id") == REF_ID] if isinstance(doc, dict) else []
        ctx.check(st_ == 200 and mine and mine[0].get("destinations") == [where], "POST /ext/dest add %s -> %s %s",
                  where, st_, doc if st_ != 200 else mine)
        named = _until(ctx, lambda: fresh(first.get("pid")), 60)
        ev["named"] = named
        ctx.log("named: %s", named)
        ctx.check(named and named.get("destinations") == [where] and named.get("dial") == "connected"
                  and named.get("dial_other_port") == "EACCES",
                  "named, it is started again, names it, reaches it, and still not the next port: %s", named)
        ctx.check(chain_has(target), "the rule table does not carry %s once named", target)
        svc = _svc(REF_ID)
        ctx.check(svc.get("state") == "running" and "quarantined" not in (svc.get("reason") or ""),
                  "the restart was no crash: %s", svc)

        # Taken away: started again with none.
        st_, doc = fc.post("/ext/dest", data={"id": REF_ID, "action": "remove", "dest": where})
        ctx.check(st_ == 200, "POST /ext/dest remove %s -> %s %s", where, st_, doc)
        gone = _until(ctx, lambda: fresh(named.get("pid") if named else None), 60)
        ev["taken_away"] = gone
        ctx.check(gone and gone.get("destinations") == [] and gone.get("dial") == "EACCES",
                  "taken away, it starts again and reaches nothing: %s", gone)
        ctx.check(not chain_has(target), "the rule table still carries %s after it was taken away", target)
    finally:
        _put_back(ctx, fc, work, prior, etag, raw, dir_mode)
    _as_found(ctx, fc, prior, raw, dir_mode, found_tree)
