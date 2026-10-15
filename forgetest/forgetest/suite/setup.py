# Copyright 2026 514 LLC d/b/a OpenGlow
# Written by Scott Wiederhold
# https://community.openglow.org
# SPDX-License-Identifier:    MIT

"""setup.* - what the setup tests share: the setup record and its
builders, the files of the data directory, a takeover that installs a
record, and the HTTPS client. The tests are setup_dark's and
setup_burns'; this module registers none.

The daemon reads the setup record (setup.json in the
data directory) and the account record (users) once, at its start, and
keeps them in memory; only its own routes write them. A test that needs
a different record installs the file under a forgectrl restart (a
takeover) and puts the previous file back the same way. Thus the daemon
never saves a test record over the real one, and a restore is complete
when the daemon is up again.

The layer content of this work (the account replay init script, the
sshd policy, the hostname and console banner scripts) is part of the
platform identity that every fingerprint carries, not of a component.
No coverage map names it: a change there makes every test necessary
again.
"""
import contextlib
import posixpath
import http.client
import json
import os
import ssl
import time
import urllib.parse

from .. import hw

RECORD = "setup.json"
USERS = "users"
SSH_FLAG = "ssh-enabled"

# The first run's form wizards, all at version 1 (wiz.c).
FORM_WIZARDS = ("advisories", "account", "preferences", "machine", "cloud")
# The steps the gate requires after the account, all at version 1, in
# the order the gate's reason names them (setup.c): the form steps, then
# the coolant flow calibration. The first-run tests seed the checks done,
# the calibration among them, so the form steps are what they walk.
REQUIRED_FORM_STEPS = ("preferences", "machine", "cloud")
REQUIRED_STEPS = REQUIRED_FORM_STEPS + ("cooling.flow",)
# The setting that counts the intake fans: the gate holds without it.
INTAKE_KEY = "cool_intake_fans"
SAFETY_PHRASE = "I UNDERSTAND"


# ----------------------------------------------------------------- files

def data_dir():
    return os.environ.get("FORGECTRL_DATA_DIR") or "/data/forgefirm"


def run_dir():
    return os.environ.get("GF_RUN_DIR") or "/run/forgefirm"


# The daemon's own paths on the board, POSIX on every host that names them.
def record_path():
    return posixpath.join(data_dir(), RECORD)


def users_path():
    return posixpath.join(data_dir(), USERS)


def ssh_flag_path():
    return posixpath.join(run_dir(), SSH_FLAG)


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


# --------------------------------------------------------------- records

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


def now_iso():
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def record_without_wizards(record):
    """A copy of a record with no completed wizard and no flag: the
    consent and the account stay, so the gate closes on the required
    steps."""
    out = json.loads(json.dumps(record))
    out["wizards"] = {}
    out["flags"] = {}
    return out


def record_with_required_steps(record):
    """A copy of a record with the required steps done (kept from the
    record, or written at version 1) and nothing else, and no flag: on a
    machine whose intake fans are counted the gate is open and every
    other step is recommended as not run yet."""
    out = record_without_wizards(record)
    have = record.get("wizards") or {}
    for wid in REQUIRED_STEPS:
        out["wizards"][wid] = have.get(wid) or {"version": 1, "completed": now_iso(), "result": {},
                                                "applied": {}}
    return out


def record_without_consent(record):
    """A copy of a record with no advisory accepted and no press: the
    gate closes on the consent, whatever else the record holds."""
    out = json.loads(json.dumps(record))
    out["advisories"] = {}
    out.pop("acceptance", None)
    return out


def record_without_account(record):
    """A copy of a record with the consent and no account: the gate
    closes on the account."""
    out = json.loads(json.dumps(record))
    out.pop("account", None)
    return out


def complete_record(status, base=None, account_name="bench"):
    """A complete setup record for a machine that must count as
    set up: every advisory at the hash GET /wiz reports, the
    press recorded, an account (the one of `base` when it has one),
    every wizard of the catalog at its version, and the run complete.
    `status` is a GET /wiz body; `base` is an existing record to keep
    the account, the machine facts, and the sheet id from."""
    base = base or {}
    ts = now_iso()
    rec = {"schema": 1, "created": base.get("created") or ts, "completed": ts,
           "advisories": {}, "wizards": {}, "flags": {}}
    if base.get("sheet_id"):
        rec["sheet_id"] = base["sheet_id"]
    elif status.get("sheet_id"):
        rec["sheet_id"] = status["sheet_id"]
    for d in status.get("documents") or []:
        rec["advisories"][d["id"]] = {"hash": d["hash"], "accepted": ts,
                                      "method": d.get("consent") or "check"}
    rec["acceptance"] = {"pressed_at": ts}
    acct = base.get("account") if isinstance(base.get("account"), dict) else None
    rec["account"] = acct or {"name": account_name, "uid": 1000, "created": ts}
    if isinstance(base.get("machine"), dict):
        rec["machine"] = base["machine"]
    wizards = status.get("wizards") or [{"id": w, "version": 1} for w in FORM_WIZARDS]
    for w in wizards:
        rec["wizards"][w["id"]] = {"version": int(w.get("version") or 1), "completed": ts,
                                   "result": {}, "applied": {}}
    return rec


@contextlib.contextmanager
def installed(ctx, files):
    """Install files ({path: bytes, or None to remove}) under a forgectrl
    restart, and put the previous content back under another one when
    the block ends, on every exit path. Yields the saved content. The
    restart is the runner's takeover: the controller is stopped through
    the supervisor, the marker makes a crash recoverable, and the exit
    waits for the supervisor to settle."""
    saved = {p: read_file(p) for p in files}
    with ctx.takeover():
        for p, data in files.items():
            write_file(p, data)
    ctx.log("installed under a restart: %s",
            ", ".join("%s=%s" % (os.path.basename(p), "removed" if d is None else "%d B" % len(d))
                      for p, d in files.items()))
    try:
        yield saved
    finally:
        with ctx.takeover():
            for p, data in saved.items():
                write_file(p, data)
        ctx.log("restored under a restart: %s", ", ".join(os.path.basename(p) for p in saved))


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


def session_from_cookie(value):
    """The ffsid session id out of a Set-Cookie value, or None."""
    for part in (value or "").split(";"):
        part = part.strip()
        if part.startswith("ffsid="):
            sid = part[len("ffsid="):]
            return sid or None
    return None


def cookie_flags(value):
    return set(p.strip().split("=", 1)[0].lower() for p in (value or "").split(";")[1:])


def wiz(fc):
    st, body = fc.get("/wiz")
    if st != 200 or not isinstance(body, dict):
        raise hw.HwError("GET /wiz -> %s" % st)
    return body


def recommended(w):
    """The steps a GET /wiz body recommends: {id: reason}."""
    return {x.get("id"): x.get("reason") or "" for x in w.get("recommended") or [] if isinstance(x, dict)}
