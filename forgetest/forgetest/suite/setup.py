# Copyright 2026 514 LLC d/b/a OpenGlow
# Written by Scott Wiederhold
# https://community.openglow.org
# SPDX-License-Identifier:    MIT

"""setup.* - what the setup tests share: the setup record's file and its
reads and writes, the settings a test puts back as found, the wizard
status, and the HTTPS client. The tests are setup_dark's and
setup_burns'; this module registers none.

The daemon reads the setup record (setup.json in the data directory)
once, at its start, and keeps it in memory; only its own routes write
it. A test that needs a different record installs the file under a
forgectrl restart (a takeover) and puts the previous file back the same
way (exthost.armed-freeze). Thus the daemon never saves a test record
over the real one, and a restore is complete when the daemon is up
again.

The layer content of this work (the account replay init script, the
sshd policy, the hostname and console banner scripts) is part of the
platform identity that every fingerprint carries, not of a component.
No coverage map names it: a change there makes every test necessary
again.
"""
import posixpath
import http.client
import json
import os
import ssl
import urllib.parse

from .. import hw

RECORD = "setup.json"

# The setting that counts the intake fans: the gate holds without it.
INTAKE_KEY = "cool_intake_fans"
SAFETY_PHRASE = "I UNDERSTAND"


# ----------------------------------------------------------------- files

def data_dir():
    return os.environ.get("FORGECTRL_DATA_DIR") or "/data/forgefirm"


# The daemon's own paths on the board, POSIX on every host that names them.
def record_path():
    return posixpath.join(data_dir(), RECORD)


def read_file(path):
    """The bytes of a file, or None when it does not exist."""
    try:
        with open(path, "rb") as f:
            return f.read()
    except OSError:
        return None


def write_file(path, data):
    """Write data to path (temp file and rename, mode 0600), or remove
    the file when data is None."""
    if data is None:
        try:
            os.remove(path)
        except OSError:
            pass
        return
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".forgetest"
    # the bytes as given: O_BINARY keeps a Windows host (the unit tests)
    # from translating newlines; it does not exist elsewhere
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | getattr(os, "O_BINARY", 0), 0o600)
    try:
        os.write(fd, data)
    finally:
        os.close(fd)
    os.replace(tmp, path)


# -------------------------------------------------------------- settings

class Restore:
    """Settings a test or a wizard writes, put back as found at the end,
    in the order of `keys` (a value that depends on another goes after
    it)."""

    def __init__(self, ctx, keys):
        self.ctx = ctx
        self.keys = keys
        self.before = {}

    def __enter__(self):
        s = self.ctx.forgectrl.settings() or {}
        self.before = {k: s.get(k) for k in self.keys}
        self.ctx.evidence["settings_before"] = dict(self.before)
        return self

    def __exit__(self, *exc):
        fc = self.ctx.forgectrl
        for k, v in self.before.items():
            if v is None or v == "":
                st, _ = fc.post("/settings", params={k: ""})
            else:
                st, _ = fc.post("/settings", data={k: v})
            self.ctx.log("restore %s=%r -> %s", k, v, st)
        return False


# ---------------------------------------------------------------- client

def request(base, method, path, data=None, headers=None, timeout=10.0):
    """One HTTP or HTTPS request without redirect following: (status,
    body bytes, {header: value}). HTTPS is served with a self-signed
    certificate, so the peer is not verified. The Host header is the
    address literal of base, which the origin check requires."""
    u = urllib.parse.urlsplit(base)
    port = u.port or (443 if u.scheme == "https" else 80)
    if u.scheme == "https":
        cx = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
        cx.check_hostname = False
        cx.verify_mode = ssl.CERT_NONE
        conn = http.client.HTTPSConnection(u.hostname, port, timeout=timeout, context=cx)
    else:
        conn = http.client.HTTPConnection(u.hostname, port, timeout=timeout)
    hdrs = {"Host": u.netloc}
    hdrs.update(headers or {})
    body = None
    if isinstance(data, dict):
        body = urllib.parse.urlencode(data).encode()
        hdrs.setdefault("Content-Type", "application/x-www-form-urlencoded")
    elif isinstance(data, str):
        body = data.encode()
    elif data is not None:
        body = data
    if body is not None:
        hdrs.setdefault("Content-Length", str(len(body)))
    try:
        conn.request(method, path, body=body, headers=hdrs)
        r = conn.getresponse()
        content = r.read()
        return r.status, content, {k.lower(): v for k, v in r.getheaders()}
    except (OSError, http.client.HTTPException) as e:
        raise hw.HwError("%s %s%s: %s" % (method, base, path, e))
    finally:
        conn.close()


def decode(content):
    """JSON when the body parses as JSON, else text."""
    text = content.decode("utf-8", "replace")
    try:
        return json.loads(text)
    except ValueError:
        return text


def wiz(fc):
    st, body = fc.get("/wiz")
    if st != 200 or not isinstance(body, dict):
        raise hw.HwError("GET /wiz -> %s" % st)
    return body

