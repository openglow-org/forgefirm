# Copyright 2026 514 LLC d/b/a OpenGlow
# Written by Scott Wiederhold
# https://community.openglow.org
# SPDX-License-Identifier:    MIT

"""An extension package keeps the Grbl sender out, by the operator's grant.

Its own module rather than a test in exthost.py, for the reason extcore.py
gives: a test added there moves the fingerprint of every test in that
module. The package is the reference package's id and key with a service
that claims the sender out as it starts and says how its claim stands
every half second, so exthost's put-back takes it away like the others.
"""

import json
import os
import socket
import time

from ..catalog import test
from .exthost import (EXT_ROOT, FWUP, REF_ID, REF_KEY, _as_found, _forgeext, _put_back, _svc, _tree, _until,
                      _write)
from .setup import SAFETY_PHRASE, read_file, record_path, request

CLAIM_DIR = "/run/forgefirm/sender-out"
GRBL_STATE = "/run/forgefirm/grbl.state"
KEEP_OUT_MSG = b"[MSG:The machine is in use: senders are kept out for now]"

# The service: it claims the sender out once, as it starts, writes the
# machine's answer, and then writes how its claim stands, twice a second.
KEEPER_SERVICE = r'''
import json, os, socket, time
data = os.environ["FFX_DATA"]


def api(method, path, body=None):
    s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    s.settimeout(10)
    try:
        s.connect(os.environ["FFX_API"])
        payload = b"" if body is None else json.dumps(body).encode()
        head = "%s %s HTTP/1.1\r\nHost: forgeext\r\n" % (method, path)
        if body is not None:
            head += "Content-Type: application/json\r\nContent-Length: %d\r\n" % len(payload)
        s.sendall(head.encode() + b"\r\n" + payload)
        buf = b""
        while True:
            c = s.recv(65536)
            if not c:
                break
            buf += c
        top, _, content = buf.partition(b"\r\n\r\n")
        return int(top.split()[1]), json.loads(content or b"null")
    finally:
        s.close()


def write(name, doc):
    p = os.path.join(data, name)
    with open(p + ".new", "w") as f:
        json.dump(doc, f)
    os.rename(p + ".new", p)


status, doc = api("POST", "/v0/sender", {"out": True})
write("claim.json", {"status": status, "doc": doc})
print("keeper up: %s %s" % (status, doc), flush=True)
while True:
    try:
        write("state.json", dict(zip(("status", "doc"), api("GET", "/v0/sender"))))
    except OSError:
        pass
    time.sleep(0.5)
'''


def _pack_keeper(work):
    """The reference package's id and key, with the service above: (archive, public key)."""
    import io
    import subprocess
    import tarfile
    manifest = {"manifest": 1, "id": REF_ID, "name": "forgetest sender keeper", "version": "1.0.0",
                "author": "forgetest", "license": "MIT", "api": "0.1", "runtime": "python",
                "service": {"exec": "bin/keeper.py"}, "capabilities": ["sender.keep_out"]}
    payload = os.path.join(work, "payload.tar.gz")
    with tarfile.open(payload, "w:gz") as t:
        for name, text, mode in (("manifest.json", json.dumps(manifest), 0o644), ("bin/keeper.py", KEEPER_SERVICE, 0o755)):
            info = tarfile.TarInfo(name)
            data = text.encode()
            info.size, info.mode = len(data), mode
            t.addfile(info, io.BytesIO(data))
    conf = os.path.join(work, "fwup.conf")
    _write(conf, 'meta-product = "ForgeFIRM extension"\nmeta-description = "%s"\nmeta-version = "1.0.0"\n'
                 'meta-platform = "forgefirm-ext"\nfile-resource payload.tar.gz {\n    host-path = "%s"\n}\n'
           % (REF_ID, payload))
    key = os.path.join(work, REF_KEY)
    raw, signed = os.path.join(work, "raw.ffx"), os.path.join(work, "keeper.ffx")
    for cmd in ([FWUP, "-g", "-o", key], [FWUP, "-c", "-f", conf, "-o", raw],
                [FWUP, "-S", "-s", key + ".priv", "-i", raw, "-o", signed]):
        subprocess.run(cmd, check=True, capture_output=True, timeout=60, cwd=work)
    return signed, key + ".pub"


def _lan_address():
    """This machine's own address on the network: a sender that connects to it comes from the network
    as the controller sees it, and not from the loopback."""
    u = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        u.connect(("192.0.2.1", 9))
        return u.getsockname()[0]
    except OSError:
        return ""
    finally:
        u.close()


def _sender(addr, secs=2.0):
    """A Grbl sender that connects and listens: (what it read, whether the controller closed it)."""
    s = socket.create_connection((addr, 23), timeout=5)
    s.settimeout(0.3)
    got, closed, end = b"", False, time.time() + secs
    try:
        while time.time() < end:
            try:
                c = s.recv(4096)
            except socket.timeout:
                continue
            if not c:
                closed = True
                break
            got += c
    finally:
        s.close()
    return got, closed


def _read_json(path):
    try:
        with open(path) as f:
            return json.load(f)
    except (OSError, ValueError):
        return None


@test("exthost.sender-keep-out", title="An extension package keeps the Grbl sender out, by the operator's grant",
      subsystem="exthost", kind="auto", hardware="api", est_min=4,
      covers=[("forgectrl", "src/senderout.*"), ("forgectrl", "src/lease.*"), ("forgectrl", "src/grblport.*"),
              ("forgectrl", "src/main.c"), ("forgectrl", "src/status.*"), ("forgeext", "src/api.*"),
              ("forgeext", "src/run.*"), ("forgeext", "src/machine.*"), ("forgeext", "src/holdkeep.*"),
              ("forgeext", "src/caps.*"), ("grblhal-glowforge", "src/ctlport.*"), ("grblhal-glowforge", "src/serial.*")],
      requires=["exthost.service"],
      description="A package the operator granted sender.keep_out keeps the Grbl sender out while it uses the "
                  "machine: the test installs a reference package whose service asks POST /v0/sender "
                  "{\"out\": true} as it starts, on an idle machine with no sender connected. The machine "
                  "grants it: /status names the package in sender_out and holds the machine lease as "
                  "ext:<id>, of the extension kind; the host keeps the claim fresh under "
                  "/run/forgefirm/sender-out; a Grbl sender that connects from the network (the machine's "
                  "own LAN address) reads the controller's message and is closed at once, and one from "
                  "this host still connects. The operator lets the sender back in from the panel "
                  "(POST /motion/sender out=0): the holder and the lease go, a sender from the network is "
                  "welcomed again, and the package's service reads released. Turned off and on, the "
                  "package's service claims again; turned off with its claim standing, forgectrl ends the "
                  "claim within seconds by itself, with the notice that names it, and lets senders in; the "
                  "notice is cleared from the panel. The package, the key, the setting, and the setup "
                  "record are put back as found.")
def sender_keep_out(ctx):
    fc = ctx.forgectrl
    ev = ctx.evidence
    ctx.check(fc.wait_idle(timeout=30, abort=ctx.aborted), "machine not idle: settings are locked")
    st, m0 = fc.get("/mode")
    ctx.check(st == 200 and isinstance(m0, dict) and m0.get("mode") == "grbl" and m0.get("controller") == "running",
              "start this test in grbl mode with the controller running (now %s)", m0)
    grbl = _read_json(GRBL_STATE) or {}
    ctx.check(not (grbl.get("sender") or {}).get("connected"),
              "a Grbl sender is connected: close it first (this test would disconnect it)")
    lan = _lan_address()
    ctx.check(lan and not lan.startswith("127."), "the machine has no address on the network: %r", lan)
    prior = fc.settings().get("ext_enabled") or ""
    raw = read_file(record_path())
    found_tree = _tree(EXT_ROOT)
    dir_mode = os.stat(os.path.dirname(EXT_ROOT)).st_mode & 0o7777
    st, body, hdrs = request(fc.base, "GET", "/advisories/extensions", headers={"Host": fc.host_header()})
    etag = hdrs.get("etag")
    import shutil
    import tempfile
    work = tempfile.mkdtemp(prefix="forgetest-sender.")
    owner_key = os.path.join(EXT_ROOT, "keys", REF_KEY + ".pub")
    data = os.path.join(EXT_ROOT, "data", REF_ID)
    claim_file = os.path.join(CLAIM_DIR, REF_ID + ".json")

    def sender_out():
        return fc.status().get("sender_out") or {}

    def holder():
        return (sender_out().get("holder") or {}).get("id")

    try:
        got, closed = _sender(lan)
        ev["before"] = [got[:80].decode(errors="replace"), closed]
        ctx.check(b"Grbl" in got and not closed, "before: a sender from the network is not welcomed: %r", got[:80])

        archive, pub = _pack_keeper(work)
        shutil.copy(pub, owner_key)
        os.chmod(owner_key, 0o644)
        r = _forgeext("install", archive, "--consent-community")
        ctx.check(r.get("ok") is False and "sender.keep_out" in (r.get("error") or ""),
                  "an install without the operator's grant is refused -> %s", r.get("error"))
        r = _forgeext("install", archive, "--consent-community", "--grant", "sender.keep_out")
        ctx.check(r.get("ok") is True, "the install with the grant -> %s", r.get("error"))
        st, reply = fc.post("/settings", data={"ext_enabled": "1", "advisory": etag, "phrase": SAFETY_PHRASE})
        ctx.check(st == 200, "ext_enabled=1 -> %s %r", st, reply)
        up = _until(ctx, lambda: _svc(REF_ID).get("state") == "running" and holder() == REF_ID, 60)
        claim = _read_json(os.path.join(data, "claim.json"))
        ev["claim"] = claim
        ctx.log("the service's claim: %s; sender_out %s", claim, sender_out())
        ctx.check(up and (claim or {}).get("status") == 200 and (claim.get("doc") or {}).get("out") is True,
                  "the service's claim was not granted: %s, %s", claim, sender_out())

        doc = fc.status()
        ev["kept"] = {"sender_out": doc.get("sender_out"), "lease": (doc.get("lease") or {}).get("holder")}
        lease = (doc.get("lease") or {}).get("holder") or {}
        ctx.check(lease.get("owner") == "ext:" + REF_ID and lease.get("kind") == "extension",
                  "the lease is not the package's: %s", lease)
        def fresh():
            k = _read_json(claim_file) or {}
            return k if (k.get("id") == REF_ID and k.get("raised") is True and
                         time.clock_gettime(time.CLOCK_MONOTONIC) - float(k.get("ts_mono") or 0) < 2.0) else None
        # The host writes the claim at its next turn; past forgectrl's 3 s grace the claim stands only
        # because the host keeps it fresh.
        kept = _until(ctx, fresh, 5)
        ev["claim_file"] = kept
        ctx.check(kept, "the host does not write the claim: %s", _read_json(claim_file))
        time.sleep(5.0)
        ctx.check(holder() == REF_ID and fresh(), "past the grace, the claim did not stand fresh: %s, %s",
                  sender_out(), _read_json(claim_file))
        got, closed = _sender(lan)
        ev["kept_out"] = [got[:120].decode(errors="replace"), closed]
        ctx.check(closed and KEEP_OUT_MSG in got and b"Grbl" not in got,
                  "a sender from the network was not turned away: %r closed=%s", got[:120], closed)
        got, closed = _sender("127.0.0.1", 1.0)
        ev["loopback"] = [got[:80].decode(errors="replace"), closed]
        ctx.check(b"Grbl" in got, "a sender from this host was not let in: %r", got[:80])

        # The operator lets it back in, from the panel.
        st, reply = fc.post("/motion/sender", data={"out": "0"})
        ev["released"] = [st, reply]
        ctx.check(st == 200 and holder() is None, "the operator's release -> %s %s", st, reply)
        ctx.check(not ((fc.status().get("lease") or {}).get("holder")), "the lease stayed after the release")
        got, closed = _sender(lan)
        ctx.check(b"Grbl" in got and not closed, "released, a sender from the network is not welcomed: %r", got[:80])
        saw = _until(ctx, lambda: ((_read_json(os.path.join(data, "state.json")) or {}).get("doc") or {}).get("released"), 10)
        ctx.check(saw is True, "the package's service did not read released: %s",
                  _read_json(os.path.join(data, "state.json")))

        # Off and on: the operator's release lasts until the package stops claiming, and the
        # service claims again as it starts.
        ctx.check(_forgeext("disable", REF_ID).get("ok") is True, "disable")
        ctx.check(_until(ctx, lambda: not os.path.exists(claim_file), 15), "disabled, the host kept the claim file")
        time.sleep(3.0)                         # past forgectrl's freshness window, so the release is forgotten
        ctx.check(_forgeext("enable", REF_ID).get("ok") is True, "enable")
        again = _until(ctx, lambda: holder() == REF_ID, 60)
        ctx.check(again, "enabled again, the service's claim was not granted: %s", sender_out())

        # Turned off with its claim standing: forgectrl ends it by itself.
        t0 = time.time()
        ctx.check(_forgeext("disable", REF_ID).get("ok") is True, "disable with the claim standing")
        gone = _until(ctx, lambda: holder() is None, 15)
        ev["stale_s"] = round(time.time() - t0, 1)
        notice = sender_out().get("notice") or {}
        ev["notice"] = notice
        ctx.log("the claim ended %.1f s after the disable; notice %s", time.time() - t0, notice)
        ctx.check(gone is True and notice.get("id") == REF_ID and notice.get("why") == "stopped",
                  "a claim its keeper stopped keeping did not end with the notice: %s", sender_out())
        ctx.check(not os.path.exists(claim_file), "the claim file stayed after the service stopped")
        got, closed = _sender(lan)
        ctx.check(b"Grbl" in got and not closed, "after it ended, a sender from the network is not welcomed: %r", got[:80])
        st, reply = fc.post("/motion/sender", data={"notice": "clear"})
        ctx.check(st == 200 and not sender_out().get("notice"), "the notice was not cleared -> %s %s", st, reply)
    finally:
        if holder():
            fc.post("/motion/sender", data={"out": "0"})
        if sender_out().get("notice"):
            fc.post("/motion/sender", data={"notice": "clear"})
        _put_back(ctx, fc, work, prior, etag, raw, dir_mode)
    ctx.check(holder() is None, "the sender is still kept out at the end")
    _as_found(ctx, fc, prior, raw, dir_mode, found_tree)
