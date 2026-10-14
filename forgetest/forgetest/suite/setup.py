# Copyright 2026 514 LLC d/b/a OpenGlow
# Written by Scott Wiederhold
# https://community.openglow.org
# SPDX-License-Identifier:    MIT

"""setup.* - the first-run setup: the record and the
controller gate, the advisories, the account and its login, the HTTPS
boundary, SSH, the cloud switch, the machine's name, and the factory
return.

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
import random
import re
import socket
import ssl
import time
import urllib.parse

from ..catalog import test
from .. import hw
from .forgectrl import lan_ip

RECORD = "setup.json"
USERS = "users"
SSH_FLAG = "ssh-enabled"
TLS_BASE = "https://127.0.0.1"          # FORGECTRL_TLS_URL overrides (host tests)

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
LOGIN_FAILS = 5
LOGIN_LOCK_S = 30


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


def tls_base():
    return (os.environ.get("FORGECTRL_TLS_URL") or TLS_BASE).rstrip("/")


def tcp_open(host, port, timeout=2.0):
    try:
        s = socket.create_connection((host, port), timeout=timeout)
        s.close()
        return True
    except OSError:
        return False


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


def mode(fc):
    st, body = fc.get("/mode")
    return body if (st == 200 and isinstance(body, dict)) else {}


def press_status(fc):
    """GET /wiz/advisories/press, the page's poll at the press step: the
    daemon takes a pressed button and records the acceptance on this
    read, so a press nobody polls for stays 'pressed' and unaccepted."""
    st, body = fc.get("/wiz/advisories/press")
    return body if (st == 200 and isinstance(body, dict)) else {}


def wiz_summary(w):
    return {k: w.get(k) for k in ("first_run", "gate", "why", "recommended",
                                  "advisories_complete", "acceptance_done", "account",
                                  "fans_counted", "completed", "versions")}


def recommended(w):
    """The steps a GET /wiz body recommends: {id: reason}."""
    return {x.get("id"): x.get("reason") or "" for x in w.get("recommended") or [] if isinstance(x, dict)}


def wait_settled(ctx):
    """The supervisor settled (the runner's own wait): the /mode body."""
    return ctx.takeover().wait_settled()


# -------------------------------------------------------------- the login

@test("setup.account-login", title="The panel login over HTTPS", subsystem="setup",
      kind="auto", est_min=2,
      covers=[("forgectrl", "src/auth.*"), ("forgectrl", "src/session.*"), ("forgectrl", "src/users.*"),
              ("forgectrl", "src/tls.*"), ("forgectrl", "src/peer.*"), ("forgectrl", "src/main.c"),
              ("forgectrl", "src/ui/login.*")],
      requires=["forgectrl.auth"], hardware="takeover",
      description="The test makes its own account: the account record is moved aside under a "
                  "forgectrl restart, the account route creates a temporary one with a password "
                  "only the test knows, and the real record comes back under another restart at "
                  "the end, the system accounts replayed and the temporary home removed. The data "
                  "directory is set to 0700 first, as a strict umask leaves it: the render that makes "
                  "the account opens it for search and nothing else (0711), and tried as the account, "
                  "its home can be entered and written while the data directory cannot be listed and "
                  "the account record cannot be read. Over "
                  "HTTPS (self-signed, so unverified): GET /wiz reports no session and the "
                  "certificate fingerprint; POST /login with a wrong password is refused (401); "
                  "five failures lock the address (429, wait at most 30 s) and the right password "
                  "is refused inside the lock too; after the lock a wrong name is 401 and the "
                  "right pair sets the ffsid cookie (HttpOnly, Secure, SameSite=Strict); GET /wiz "
                  "with the cookie reports the session; POST /logout ends it.")
def account_login(ctx):
    fc = ctx.forgectrl
    ev = ctx.evidence
    name = "forgetest"
    pw = "".join(random.choice("abcdefghjkmnpqrstuvwxyz23456789") for _ in range(16))
    before = wiz(fc)
    ev["account_before"] = (before.get("users") or {}).get("name")
    homes = "/data/forgefirm/home"
    dir_mode = os.stat(data_dir()).st_mode & 0o7777
    ev["data_dir_mode_found"] = "%04o" % dir_mode
    try:
        with installed(ctx, {users_path(): None}):
            w = wiz(fc)
            ctx.check(not (w.get("users") or {}).get("exists"), "an account still exists: %s", w.get("users"))
            # the data directory as a strict umask leaves it: the account's render has to open it
            os.chmod(data_dir(), 0o700)
            st, body = fc.post("/wiz/account", data={"name": name, "password": pw})
            ctx.log("POST /wiz/account (temporary %r) -> %s %s", name, st, body if isinstance(body, dict) else "")
            ctx.check(st == 200, "the temporary account was not created: %s %s", st, body)
            w = wiz(fc)
            ctx.check((w.get("users") or {}).get("name") == name, "the account reads %s", w.get("users"))
            reach = home_reach(name)
            ev["home_reach"] = reach
            ev["data_dir_mode_after_render"] = "%04o" % (os.stat(data_dir()).st_mode & 0o7777)
            ctx.log("as %s, from a data directory found 0700 (now %s): %s", name, ev["data_dir_mode_after_render"], reach)
            ctx.check(reach.get("cd") == "ok" and reach.get("write") == "ok",
                      "the account cannot use its own home %s: %s", reach.get("home"), reach)
            ctx.check(ev["data_dir_mode_after_render"] == "0711", "the render left the data directory at %s, expected "
                      "0711 (the search bit and nothing else)", ev["data_dir_mode_after_render"])
            ctx.check(reach.get("list_data_dir") == "EACCES" and reach.get("read_record") == "EACCES",
                      "the account reads what is not its own: %s", reach)
            login_checks(ctx, name, pw)
    finally:
        # as found; the replay below is the product's own say on the mode
        os.chmod(data_dir(), dir_mode)
        rc, out = hw.initd("forgefirm-users", "reload")
        ev["users_reload_rc"] = rc
        ctx.log("forgefirm-users reload -> rc %s %s", rc, out.strip()[:200])
        if name != ev["account_before"]:
            import shutil
            shutil.rmtree(os.path.join(homes, name), ignore_errors=True)
    after = wiz(fc)
    ctx.check((after.get("users") or {}).get("name") == ev["account_before"],
              "the account reads %r after the restore, was %r", (after.get("users") or {}).get("name"),
              ev["account_before"])


# Run as root: becomes the account and tries its own home, and what is
# not its own. One JSON line.
HOME_REACH = r'''
import errno, json, os, pwd, sys
p = pwd.getpwnam(sys.argv[1])
os.setgroups([])
os.setgid(p.pw_gid)
os.setuid(p.pw_uid)


def tried(fn):
    try:
        fn()
        return "ok"
    except OSError as e:
        return errno.errorcode.get(e.errno, str(e.errno))


def write():
    path = os.path.join(p.pw_dir, ".forgetest-reach")
    with open(path, "w") as f:
        f.write("x")
    os.remove(path)


print(json.dumps({"uid": os.getuid(), "home": p.pw_dir, "cd": tried(lambda: os.chdir(p.pw_dir)), "write": tried(write),
                  "list_data_dir": tried(lambda: os.listdir(sys.argv[2])),
                  "read_record": tried(lambda: open(os.path.join(sys.argv[2], "users")).read())}))
'''


def home_reach(name):
    """What the account can do with its own home and with the data
    directory around it, tried as the account."""
    import subprocess
    import sys
    p = subprocess.run([sys.executable, "-c", HOME_REACH, name, data_dir()], capture_output=True, text=True, timeout=30)
    try:
        return json.loads(p.stdout)
    except ValueError:
        return {"error": (p.stderr or p.stdout).strip()[-200:]}


def login_checks(ctx, name, pw):
    """The login rules against an account whose password the test knows."""
    ev = ctx.evidence
    base = tls_base()

    st, body, hdrs = request(base, "GET", "/wiz")
    w = decode(body)
    ctx.log("GET %s/wiz -> %s", base, st)
    ctx.check(st == 200 and isinstance(w, dict), "GET /wiz over HTTPS -> %s", st)
    ctx.check(w.get("session") is False, "a session is reported without a cookie")
    ev["tls_fingerprint"] = w.get("tls_fingerprint")
    ctx.check(w.get("tls_fingerprint"), "GET /wiz carries no certificate fingerprint")

    def attempt(n, p):
        st, body, hdrs = request(base, "POST", "/login", data={"name": n, "password": p})
        return st, decode(body), hdrs

    # a lock left by an earlier run: wait it out first
    st, body, hdrs = attempt(name, "not-" + pw)
    if st == 429:
        wait = int((body or {}).get("wait") or LOGIN_LOCK_S) if isinstance(body, dict) else LOGIN_LOCK_S
        ctx.log("the address is locked from before (%s s): waiting", wait)
        ctx.sleep(min(wait, LOGIN_LOCK_S) + 1)
        st, body, hdrs = attempt(name, "not-" + pw)
    codes = [st]
    ctx.check(st == 401, "a wrong password -> %s, expected 401", st)
    for i in range(LOGIN_FAILS + 1):
        st, body, hdrs = attempt(name, "not-%s-%d" % (pw, i))
        codes.append(st)
        if st == 429:
            break
    ev["wrong_codes"] = codes
    ctx.log("wrong password attempts -> %s", codes)
    ctx.check(codes[-1] == 429 and all(c == 401 for c in codes[:-1]),
              "the throttle did not lock after wrong attempts: %s", codes)
    ctx.check(len(codes) - 1 <= LOGIN_FAILS, "the lock came after %d failures, the rule is %d",
              len(codes) - 1, LOGIN_FAILS)
    wait = body.get("wait") if isinstance(body, dict) else None
    ev["wait"] = wait
    ctx.check(isinstance(wait, int) and 0 < wait <= LOGIN_LOCK_S, "the lock reports wait=%r", wait)
    st, body, hdrs = attempt(name, pw)
    ev["right_inside_lock"] = st
    ctx.check(st == 429, "the right password inside the lock -> %s, expected 429", st)
    ctx.log("locked for %s s; the right password is refused inside the lock", wait)
    ctx.sleep(wait + 1)

    st, body, hdrs = attempt("not-" + name, pw)
    ev["wrong_name"] = st
    ctx.check(st == 401, "a wrong name with the right password -> %s, expected 401", st)
    st, body, hdrs = attempt(name, pw)
    cookie = hdrs.get("set-cookie") or ""
    sid = session_from_cookie(cookie)
    ev["login"] = st
    ev["cookie_flags"] = sorted(cookie_flags(cookie))
    ctx.log("POST /login (right) -> %s, cookie flags %s", st, ev["cookie_flags"])
    ctx.check(st == 200, "the right name and password -> %s, expected 200", st)
    ctx.check(sid and len(sid) == 64 and all(c in "0123456789abcdef" for c in sid),
              "no ffsid session id in Set-Cookie: %r", cookie)
    ctx.check({"httponly", "secure", "samesite"} <= cookie_flags(cookie),
              "the cookie lacks HttpOnly, Secure, or SameSite: %r", cookie)
    ctx.check("samesite=strict" in cookie.lower(), "the cookie is not SameSite=Strict: %r", cookie)
    st, body, hdrs = request(base, "GET", "/wiz", headers={"Cookie": "ffsid=" + sid})
    w = decode(body)
    ctx.check(st == 200 and isinstance(w, dict) and w.get("session") is True,
              "GET /wiz with the cookie does not report the session (%s %s)", st,
              w.get("session") if isinstance(w, dict) else w)
    st, body, hdrs = request(base, "POST", "/logout", headers={"Cookie": "ffsid=" + sid})
    ev["logout"] = st
    ctx.check(st == 200, "POST /logout -> %s", st)
    ctx.check("max-age=0" in (hdrs.get("set-cookie") or "").lower(), "the logout did not clear the cookie")
    st, body, hdrs = request(base, "GET", "/wiz", headers={"Cookie": "ffsid=" + sid})
    w = decode(body)
    ctx.check(isinstance(w, dict) and w.get("session") is False, "the session survives the logout")
    ctx.log("session set by the login, seen by GET /wiz, ended by the logout")


# ------------------------------------------------------- HTTPS boundary

@test("setup.https-only-writes", title="Writes from the LAN go to HTTPS", subsystem="setup",
      kind="auto", est_min=1,
      covers=[("forgectrl", "src/auth.*"), ("forgectrl", "src/tls.*"), ("forgectrl", "src/peer.*"),
              ("forgectrl", "src/main.c")],
      requires=["forgectrl.auth"],
      description="From the board's own LAN address, which the daemon sees as a non-loopback peer: "
                  "a read over HTTP is served (200), a write over HTTP is a 302 to the same path "
                  "on HTTPS and acts on nothing (the reboot route included), the same write from "
                  "loopback over HTTP is served, and over HTTPS from the LAN address the token "
                  "writes on the dev image (a release image needs the session: 403 login "
                  "required).")
def https_only_writes(ctx):
    fc = ctx.forgectrl
    ev = ctx.evidence
    ip = lan_ip()
    ev["lan_ip"] = ip
    ctx.check(ip, "cannot determine the board's LAN address")
    u = urllib.parse.urlsplit(fc.base)
    http_base = "http://%s%s" % (ip, (":%d" % u.port) if u.port else "")
    tls = "https://%s" % ip
    token = {"X-ForgeFIRM-Token": fc.token}

    # the HTTPS listener first: without it the daemon serves every route
    # on HTTP and the probes below would be served, not redirected
    st, body, hdrs = request(tls, "GET", "/status")
    ev["lan_https_read"] = st
    ctx.log("GET %s/status -> %s", tls, st)
    ctx.check(st == 200, "a read over HTTPS from the LAN -> %s: no HTTPS listener", st)

    st, body, hdrs = request(http_base, "GET", "/status")
    ev["lan_http_read"] = st
    ctx.log("GET %s/status -> %s", http_base, st)
    ctx.check(st == 200, "a read over HTTP from the LAN -> %s, expected 200", st)

    # the probes act on nothing even when served: a settings write of the
    # value in force, and two write-class reads
    settings = fc.settings()
    key = "ui_units"
    val = settings.get(key) if isinstance(settings.get(key), str) and settings.get(key) else "metric"
    for method, path, data in (("POST", "/settings", {key: val}), ("GET", "/system/ssh", None),
                               ("GET", "/logs", None)):
        st, body, hdrs = request(http_base, method, path, data=data, headers=token)
        loc = hdrs.get("location") or ""
        ev["lan_http %s %s" % (method, path)] = {"status": st, "location": loc}
        ctx.log("%s %s%s (token) -> %s Location %s", method, http_base, path, st, loc)
        ctx.check(st == 302, "%s %s over HTTP from the LAN -> %s, expected 302", method, path, st)
        ctx.check(loc.startswith("https://%s" % ip) and loc.endswith(path),
                  "the redirect does not point at %s on HTTPS: %r", path, loc)

    st, body = fc.post("/settings", data={key: val})
    ev["loopback_http_write"] = st
    ctx.check(st == 200, "the same write from loopback over HTTP -> %s, expected 200", st)

    st, ssh = fc.get("/system/ssh")
    dev = bool(isinstance(ssh, dict) and ssh.get("dev_image"))
    account = bool((wiz(fc).get("users") or {}).get("exists"))
    ev["dev_image"] = dev
    ev["account"] = account
    st, body, hdrs = request(tls, "POST", "/settings", data={key: val}, headers=token)
    ev["lan_https_write"] = st
    ctx.log("POST %s/settings (token, no session) -> %s (dev image %s, account %s)", tls, st, dev, account)
    if dev or not account:
        ctx.check(st == 200, "the token write over HTTPS -> %s, expected 200 on the dev image", st)
    else:
        b = decode(body)
        ctx.check(st == 403 and isinstance(b, dict) and b.get("error") == "login required",
                  "the token write over HTTPS without a session -> %s %r, expected 403 login required", st, b)


# ------------------------------------------------------------------ ssh

def sshd_policy():
    """The ForgeFIRM keys of the effective sshd policy (sshd -T,
    lowercase keys), or {"error": ...} when sshd cannot report it."""
    for exe in ("/usr/sbin/sshd", "sshd"):
        rc, out = hw.run([exe, "-T"], timeout=15)
        if rc == 127 and "No such file" in out:
            continue
        if rc != 0:
            return {"error": out.strip()[:200]}
        policy = {}
        for line in out.splitlines():
            parts = line.split(None, 1)
            if len(parts) == 2 and parts[0] in ("permitrootlogin", "permitemptypasswords",
                                                "passwordauthentication", "banner"):
                policy[parts[0]] = parts[1].strip()
        return policy
    return {"error": "no sshd binary"}


def restore_ssh(fc, before):
    """Put the SSH state back as found: the flag file, then sshd."""
    flag = ssh_flag_path()
    if before.get("running") and not tcp_open("127.0.0.1", 22):
        fc.post("/system/ssh", params={"enable": "1"})
    elif not before.get("running") and tcp_open("127.0.0.1", 22):
        fc.post("/system/ssh", params={"enable": "0"})
    write_file(flag, b"" if before.get("enabled") else None)


@test("setup.ssh-until-reboot", title="SSH is switched from the panel until the next reboot",
      subsystem="setup", kind="auto", est_min=1,
      covers=[("forgectrl", "src/main.c"), ("forgectrl", "src/paths.h")],
      requires=["forgectrl.auth"],
      description="GET /system/ssh reports enabled, running, and dev_image, and agrees with the "
                  "flag file and port 22. POST enable=1 writes /run/forgefirm/ssh-enabled and "
                  "sshd listens on 22, with the effective policy (sshd -T) at PasswordAuthentication "
                  "yes, and on a release image PermitRootLogin no and PermitEmptyPasswords no (the "
                  "dev image turns root over SSH back on for the bench; the release gate checks "
                  "the release image's sshd_config as built), and with no pre-authentication "
                  "banner, so the machine says nothing to a client that has not logged in. "
                  "An enable outside 0 and 1 is refused (400). On a release "
                  "image POST enable=0 removes the flag and stops sshd; on the dev image the boot "
                  "rule keeps sshd up and the stop is never gated, so the test removes the flag "
                  "by hand (as a reboot does, the flag is on tmpfs) and sshd stays. The prior "
                  "state is restored.")
def ssh_until_reboot(ctx):
    fc = ctx.forgectrl
    ev = ctx.evidence
    st, before = fc.get("/system/ssh")
    ctx.log("GET /system/ssh -> %s %s", st, before)
    ctx.check(st == 200 and isinstance(before, dict), "GET /system/ssh -> %s", st)
    for k in ("enabled", "running", "dev_image"):
        ctx.check(isinstance(before.get(k), bool), "/system/ssh lacks a boolean %s", k)
    ev["before"] = before
    flag = ssh_flag_path()
    ctx.check(os.path.exists(flag) == before["enabled"], "the flag file %s disagrees with enabled=%s",
              flag, before["enabled"])
    ctx.check(tcp_open("127.0.0.1", 22) == before["running"], "port 22 disagrees with running=%s",
              before["running"])
    try:
        st, on = fc.post("/system/ssh", params={"enable": "1"})
        ev["enable"] = on
        ctx.log("POST /system/ssh enable=1 -> %s %s", st, on)
        ctx.check(st == 200 and isinstance(on, dict) and on.get("enabled") is True,
                  "enable=1 -> %s %s", st, on)
        ctx.check(os.path.exists(flag), "enable=1 did not write %s", flag)
        took = ctx.wait_for(lambda: tcp_open("127.0.0.1", 22), 15)
        ctx.check(took is not None, "sshd does not listen on 22 within 15 s of enable=1")
        ctx.log("sshd listens on 22 (%.1f s)", took)
        policy = sshd_policy()
        ev["sshd_policy"] = policy
        ctx.log("sshd -T: %s", policy)
        ctx.check(policy.get("passwordauthentication") == "yes",
                  "PasswordAuthentication is %r: the account cannot log in", policy.get("passwordauthentication"))

        # No pre-authentication banner: the machine says nothing to a
        # client that has not logged in. The mark and the version go in
        # the motd, which a login prints (image.health).
        ctx.check(policy.get("banner") in (None, "none"),
                  "sshd sends a pre-authentication banner (%r)", policy.get("banner"))

        if not before["dev_image"]:
            ctx.check(policy.get("permitrootlogin") == "no" and policy.get("permitemptypasswords") == "no",
                      "a release image runs sshd with PermitRootLogin %r, PermitEmptyPasswords %r",
                      policy.get("permitrootlogin"), policy.get("permitemptypasswords"))

        st, body = fc.post("/system/ssh", params={"enable": "2"})
        ctx.check(st == 400, "enable=2 -> %s, expected 400", st)

        if before["dev_image"]:
            os.remove(flag)
            st, off = fc.get("/system/ssh")
            ctx.log("dev image: flag removed by hand; GET /system/ssh -> %s %s", st, off)
            ctx.check(isinstance(off, dict) and off.get("enabled") is False and off.get("running") is True,
                      "with the flag gone the dev image reads %s", off)
        else:
            st, off = fc.post("/system/ssh", params={"enable": "0"})
            ctx.log("POST /system/ssh enable=0 -> %s %s", st, off)
            ctx.check(st == 200 and isinstance(off, dict) and off.get("enabled") is False,
                      "enable=0 -> %s %s", st, off)
            ctx.check(not os.path.exists(flag), "enable=0 left %s", flag)
            took = ctx.wait_for(lambda: not tcp_open("127.0.0.1", 22), 15)
            ctx.check(took is not None, "sshd still listens on 22 within 15 s of enable=0")
        ev["after"] = off
    finally:
        restore_ssh(fc, before)
    st, final = fc.get("/system/ssh")
    ev["final"] = final
    ctx.check(isinstance(final, dict) and final.get("enabled") == before["enabled"]
              and final.get("running") == before["running"],
              "the SSH state is not as found: %s (was %s)", final, before)


# --------------------------------------------------- the machine's name

def mac_suffix():
    """The last four hex digits of the MAC address the hostname is built
    from: wlan0, or eth0 on a machine with no WiFi."""
    for dev in ("wlan0", "eth0"):
        raw = read_file("/sys/class/net/%s/address" % dev)
        mac = (raw or b"").decode("ascii", "replace").strip().replace(":", "").lower()
        if mac and mac != "0" * 12:
            return mac[-4:]
    return ""


def cmdlines():
    """The command line of every process on the machine, one string
    each."""
    out = []
    for name in os.listdir("/proc"):
        if not name.isdigit():
            continue
        raw = read_file("/proc/%s/cmdline" % name)
        if raw:
            out.append(raw.decode("utf-8", "replace").replace("\0", " ").strip())
    return out


@test("setup.machine-name", title="The machine names itself from its MAC address",
      subsystem="setup", kind="auto", est_min=1,
      requires=["forgectrl.auth"],
      description="The machine calls itself forgefirm-<xxxx>, where xxxx is the last four hex "
                  "digits of its WiFi MAC address, so two machines on one network answer to "
                  "different names and no name carries a serial number. The live name, "
                  "/etc/hostname (a bind-mounted copy, because the rootfs is read-only) and the "
                  "MAC address agree; the DHCP client sends the name as the hostname option, "
                  "which is what a network with dynamic DNS publishes; the console banner offers "
                  "the machine's addresses and nothing else, with no marker line and no .local "
                  "name; and no mDNS responder is on the image. No component covers this: the "
                  "hostname and banner scripts are layer content, in the platform identity of "
                  "every fingerprint.")
def machine_name(ctx):
    ev = ctx.evidence

    # 1. the name, the file and the MAC address agree
    suffix = mac_suffix()
    ev["mac_suffix"] = suffix
    ctx.check(suffix, "no MAC address to build a name from")
    live = socket.gethostname()
    on_file = ((read_file("/etc/hostname") or b"").decode("utf-8", "replace")).strip()
    ev["hostname"] = live
    ev["hostname_file"] = on_file
    ctx.log("hostname %r (MAC suffix %s)", live, suffix or "none")
    ctx.check(live == "forgefirm-" + suffix,
              "the hostname is %r, expected forgefirm-%s", live, suffix)
    ctx.check(on_file == live, "/etc/hostname holds %r, the live name is %r", on_file, live)

    # 2. the DHCP client sends it (option 12), so dynamic DNS can publish it
    dhcp = [c for c in cmdlines() if c.split(" ")[0].split("/")[-1] == "udhcpc"]
    ev["udhcpc"] = dhcp
    ctx.check(dhcp, "no DHCP client is running")
    ctx.check(any("-x hostname:" + live in c for c in dhcp),
              "the DHCP client does not send the machine's name: %s", dhcp)

    # 3. the console banner: the addresses, and nothing that is not the banner
    issue = (read_file("/etc/issue") or b"").decode("utf-8", "replace")
    ev["issue"] = issue
    ip = lan_ip()
    ev["lan_ip"] = ip
    ctx.check(ip, "cannot determine the board's LAN address")
    ctx.check("Control panel:" in issue, "the console banner names no control panel")
    ctx.check("https://%s/" % ip in issue,
              "the console banner does not carry the LAN address %s", ip)
    for junk in ("# end", "# ForgeFIRM addresses", ".local"):
        ctx.check(junk not in issue, "the console banner shows %r", junk)
    urls = re.findall(r"https://(\S+)/", issue)
    ev["banner_urls"] = urls
    for u in urls:
        literal = u.startswith("[") or all(c.isdigit() or c == "." for c in u)
        ctx.check(literal, "the console banner offers %r, which is not an address", u)

    # 4. no mDNS on the image; the name is the only name the machine has
    ev["avahi_pids"] = hw.pidof("avahi-daemon")
    ctx.check(not ev["avahi_pids"], "an mDNS responder is running: %s", ev["avahi_pids"])
    ctx.check(not os.path.exists("/etc/avahi"), "/etc/avahi is on the image")


# --------------------------------------------------------- the first run

def led_pattern():
    """The pattern the button LEDs hold, from the ledtrig_smooth
    attributes: {"target": [r, g, b], "pulse_on": [...], "pulse_off": [...]}
    (None where unreadable). The hardware breathes on its own once
    pulse_on is set, so the pattern is the evidence, not a sample."""
    out = {"target": [], "pulse_on": [], "pulse_off": []}
    for name in hw.BUTTON_LEDS:
        for attr in out:
            try:
                with open(hw.leds_root() + name + "/" + attr) as f:
                    out[attr].append(int(f.read().strip()))
            except (OSError, ValueError):
                out[attr].append(None)
    return out


def breathes_teal(pattern):
    """True for the acceptance cue: red off, green and blue on, and the
    green and blue channels pulsing."""
    t = pattern.get("target") or []
    on = pattern.get("pulse_on") or []
    if len(t) < 3 or len(on) < 3:
        return False
    return (t[0] or 0) == 0 and (t[1] or 0) > 0 and (t[2] or 0) > 0 and (on[1] or 0) > 0 and (on[2] or 0) > 0


FIRST_RUN_SETTINGS = ["cloud_enabled", "homing_mode", "controller_mode", "gfcloud_home_timeout_s",
                      "cool_tec_present", "ui_units", "wifi_country"]


def first_run_record(status, base):
    """A record that reads as a first run with the checks and the burns
    behind it: no consent, no press, no account, no form wizard, not
    complete; every check and burn of the catalog kept from `base` (the
    real record) or written at its catalog version. It is the machine
    after the button-hold reset of the consent and the account, so the
    run walks the form steps and not the hour of checks and burns a
    fresh machine is offered."""
    base = base or {}
    ts = now_iso()
    rec = {"schema": 1, "created": base.get("created") or ts, "advisories": {}, "wizards": {},
           "flags": dict(base.get("flags") or {})}
    have = base.get("wizards") or {}
    for w in status.get("wizards") or []:
        if w.get("class") == "form":
            continue
        rec["wizards"][w["id"]] = have.get(w["id"]) or {
            "version": int(w.get("version") or 1), "completed": ts, "result": {}, "applied": {}}
    return rec


@contextlib.contextmanager
def first_run(ctx):
    """The machine as a first run for the block: the record seeded by
    first_run_record and the account record removed, under a forgectrl
    restart; the settings the form steps write put back as found; the
    real records restored under another restart, the system accounts
    replayed from them, and the temporary home directory removed. Yields
    a dict the block fills with `temp_account`."""
    fc = ctx.forgectrl
    ev = ctx.evidence
    before = wiz(fc)
    ev["before"] = wiz_summary(before)
    ev["account_before"] = (before.get("users") or {}).get("name")
    try:
        base = json.loads((read_file(record_path()) or b"{}").decode("utf-8"))
    except ValueError:
        base = {}
    seed = json.dumps(first_run_record(before, base), indent=1).encode("utf-8")
    homes = "/data/forgefirm/home"
    try:
        homes_before = set(os.listdir(homes))
    except OSError:
        homes_before = set()
    state = {"temp_account": None}
    try:
        with Restore(ctx, FIRST_RUN_SETTINGS):
            with installed(ctx, {record_path(): seed, users_path(): None}):
                w = wiz(fc)
                ev["fresh"] = wiz_summary(w)
                ctx.check(w.get("first_run") is True and not (w.get("users") or {}).get("exists")
                          and w.get("advisories_complete") is False and w.get("acceptance_done") is False,
                          "the machine does not read as a first run: %s", wiz_summary(w))
                m = mode(fc)
                ctx.check(m.get("controller") == "gated", "the controller is %s on a first run",
                          m.get("controller"))
                yield state
    finally:
        # the real records are back, whether or not the block passed; the
        # system accounts follow the record, and a temporary account the
        # block created goes with its home
        rc, out = hw.initd("forgefirm-users", "reload")
        ev["users_reload_rc"] = rc
        ctx.log("forgefirm-users reload -> rc %s %s", rc, out.strip()[:200])
        temp = state.get("temp_account")
        if temp and temp not in homes_before and temp != ev["account_before"]:
            import shutil
            shutil.rmtree(os.path.join(homes, temp), ignore_errors=True)
            ctx.log("removed the temporary home directory of %r", temp)
    after = wiz(fc)
    ev["after"] = wiz_summary(after)
    ctx.check((after.get("users") or {}).get("name") == ev["account_before"],
              "the account reads %r after the restore, was %r", (after.get("users") or {}).get("name"),
              ev["account_before"])
    ctx.check(after.get("completed") == before.get("completed") and after.get("gate") == before.get("gate"),
              "the machine does not read as before: %s", wiz_summary(after))


def finished_first_run(ctx):
    """The end of a first run: the record complete, the gate open, the
    controller running."""
    fc = ctx.forgectrl
    ev = ctx.evidence
    w = wiz(fc)
    ev["completed"] = wiz_summary(w)
    ctx.check(w.get("completed") is True and w.get("gate") == "open",
              "the run did not complete with the gate open: %s", wiz_summary(w))
    m = wait_settled(ctx)
    ev["mode_after_setup"] = m
    ctx.check(isinstance(m, dict) and m.get("controller") == "running",
              "the controller did not come back after the setup: %s", m)


@test("setup.first-run-flow", title="The first run, driven as the page drives it", subsystem="setup",
      kind="operator", hardware="takeover", est_min=3,
      covers=[("forgectrl", "src/wiz.*"), ("forgectrl", "src/setup.*"), ("forgectrl", "src/users.*"),
              ("forgectrl", "src/session.*"), ("forgectrl", "src/button.*"), ("forgectrl", "src/led.*"),
              ("forgectrl", "src/sheetid.*"), ("forgectrl", "src/advisories.*"), ("forgectrl", "src/hooks.h"),
              ("forgectrl", "src/main.c"), ("forgectrl", "docs/advisories/safety-and-risk.md"),
              ("forgectrl", "docs/advisories/licenses.md"), ("forgectrl", "docs/advisories/privacy.md"),
              ("forgectrl", "docs/advisories/cloud-service.md")],
      actions=["button"],
      description="With the record seeded as a first run behind the checks and no account, the "
                  "test makes the page's own calls in the page's order: POST /wiz/advisories/accept "
                  "for each document at the hash GET /wiz lists (the phrase for the typed one), "
                  "advisories_complete; POST /wiz/advisories/press, the button state waiting and "
                  "the button LEDs breathing teal, the press (the bench actuator's), "
                  "acceptance_done; POST /wiz/account with a temporary name and password, the "
                  "account exists and the response sets the session cookie, and the gate holds "
                  "with the reason 'setup required: preferences, machine, cloud' and no "
                  "controller (the seed keeps the coolant flow calibration done). Then the "
                  "required form steps, the gate's reason naming what is left "
                  "after each: POST /wiz/preferences with the units as found; POST /wiz/machine "
                  "with the model as found (basic when unknown) and no TEC; POST /wiz/cloud as "
                  "found (enabled with the phrase and the homing as found, or off). With the "
                  "intake fans counted (the machine's setting, which the seed leaves alone) the "
                  "gate opens and the supervisor starts the controller. POST /wiz/complete, which "
                  "Start using the machine and Finish later both call: the record complete, the "
                  "gate open, the controller running. The settings "
                  "the steps write, the real record and the account are restored, the system "
                  "accounts replayed, the temporary home removed. Nothing moves and the laser is "
                  "not involved.")
def first_run_flow(ctx):
    fc = ctx.forgectrl
    ev = ctx.evidence
    settings = fc.settings() or {}
    with first_run(ctx) as state:
        w = wiz(fc)
        for d in w.get("documents") or []:
            data = {"doc": d.get("id"), "hash": d.get("hash")}
            if d.get("consent") == "typed":
                data["phrase"] = d.get("phrase") or SAFETY_PHRASE
            st, body = fc.post("/wiz/advisories/accept", data=data)
            ctx.check(st == 200, "accept %s -> %s %s", d.get("id"), st, body)
        w = wiz(fc)
        ctx.check(w.get("advisories_complete") is True, "the documents do not read accepted: %s", wiz_summary(w))
        ctx.log("the four documents are accepted")

        st, body = fc.post("/wiz/advisories/press")
        ctx.check(st == 200, "POST /wiz/advisories/press -> %s %s", st, body)
        took = ctx.wait_for(lambda: wiz(fc).get("button") == "waiting", 30)
        ctx.check(took is not None, "the press step did not start")
        took = ctx.wait_for(lambda: breathes_teal(led_pattern()), 10)
        pattern = led_pattern()
        ev["led_pattern"] = pattern
        ctx.check(took is not None, "the button LEDs do not breathe teal at the press step: %s", pattern)
        # The daemon records the acceptance when the press status is polled
        # with the button read as pressed (button_take_pressed), which the
        # page does from its press step; the test polls the same way.
        ctx.act("button", "press", until=lambda: press_status(fc).get("accepted") is True, timeout=600,
                text="It breathes teal: this press is the acceptance.")
        ctx.check(wiz(fc).get("acceptance_done") is True, "the press was not accepted")
        ctx.log("the press step: teal (%s), the press accepted", pattern)

        name = "bench-%04x" % random.randrange(1 << 16)
        pw = "flow-%08x" % random.randrange(1 << 32)
        st, body, hdrs = request(fc.base, "POST", "/wiz/account",
                                 data={"name": name, "password": pw},
                                 headers={"X-ForgeFIRM-Token": fc.token})
        ev["account"] = {"status": st, "cookie": bool(hdrs.get("set-cookie"))}
        ctx.check(st == 200, "POST /wiz/account -> %s %s", st, body[:200])
        state["temp_account"] = name
        ctx.check(session_from_cookie(hdrs.get("set-cookie")) is not None,
                  "the account step did not log this client in: %r", hdrs.get("set-cookie"))
        w = wiz(fc)
        ctx.check((w.get("users") or {}).get("exists") is True and (w.get("users") or {}).get("name") == name,
                  "the account does not read created: %s", w.get("users"))
        ctx.log("account %r created, the session cookie set", name)

        # After the account the gate waits for the required steps: no
        # controller yet, and the reason names what is left.
        def still_gated(left):
            w = wiz(fc)
            m = mode(fc)
            why = "setup required: " + ", ".join(left)
            ev["gated " + why] = {"wiz": wiz_summary(w), "mode": m}
            ctx.check(w.get("gate") == "closed" and w.get("why") == why,
                      "GET /wiz does not hold the gate on %s: %s", ", ".join(left), wiz_summary(w))
            ctx.check(m.get("controller") == "gated", "the controller is %s with %s left", m.get("controller"),
                      ", ".join(left))

        still_gated(REQUIRED_FORM_STEPS)
        ctx.log("the gate holds after the account: %s", wiz(fc).get("why"))

        st, body = fc.post("/wiz/preferences", data={"ui_units": settings.get("ui_units") or "metric"})
        ctx.check(st == 200, "POST /wiz/preferences -> %s %s", st, body if isinstance(body, dict) else "")
        still_gated(REQUIRED_FORM_STEPS[1:])
        machine = (w.get("machine") or {})
        model = machine.get("model") if machine.get("model") in ("basic", "plus", "pro") else "basic"
        st, body = fc.post("/wiz/machine", data={"model": model, "tec": "0"})
        ctx.check(st == 200, "POST /wiz/machine -> %s %s", st, body if isinstance(body, dict) else "")
        # The machine facts the step records see the head: its hardware id
        # is the module's head/info "id" line.
        head = (wiz(fc).get("machine") or {}).get("head") or {}
        ev["head"] = head
        ctx.check(head.get("present") is True and bool(head.get("hw_id")),
                  "the machine facts do not see the head: %s", head)
        still_gated(REQUIRED_FORM_STEPS[2:])
        if settings.get("cloud_enabled") == "1":
            data = {"enabled": "1", "phrase": SAFETY_PHRASE,
                    "homing_mode": settings.get("homing_mode") or "gfcloud"}
        else:
            data = {"enabled": "0"}
        st, body = fc.post("/wiz/cloud", data=data)
        ev["cloud_step"] = {"data": data, "status": st}
        ctx.check(st == 200, "POST /wiz/cloud %s -> %s %s", data, st, body if isinstance(body, dict) else "")
        w = wiz(fc)
        done = w.get("versions") or {}
        for wid in FORM_WIZARDS:
            ctx.check(done.get(wid) == 1, "the record does not carry %s at version 1: %s", wid, done)
        ctx.log("preferences, machine (%s), cloud (%s) written", model, data.get("enabled"))

        # The required steps done and the intake fans counted (the
        # machine's setting, which the seed leaves alone, so the page shows
        # the Intake fans step as done): the gate opens and the controller
        # runs, whatever the setup does next.
        ev["after_required"] = wiz_summary(w)
        ctx.check(w.get("gate") == "open" and not w.get("why") and w.get("fans_counted") is True,
                  "the gate does not open with the required steps: %s", wiz_summary(w))
        took = ctx.wait_for(lambda: mode(fc).get("controller") == "running", 60)
        m = wait_settled(ctx)
        ev["mode_after_required"] = m
        ctx.check(took is not None and isinstance(m, dict) and m.get("controller") == "running",
                  "the controller did not start with the required steps: %s", m)
        ctx.log("the gate is open; the controller runs (%.1f s)", took)

        st, body = fc.post("/wiz/complete")
        ctx.check(st == 200, "POST /wiz/complete -> %s %s", st, body if isinstance(body, dict) else "")
        finished_first_run(ctx)
        ctx.log("the run completed: gate open, the controller running")


@test("setup.first-run-page", title="The first-run page, walked once", subsystem="setup",
      kind="operator", hardware="takeover", est_min=5,
      covers=[("forgectrl", "src/ui/wizard.*"), ("forgectrl", "src/ui/forms.js"), ("forgectrl", "src/ui/md.js"),
              ("forgectrl", "src/ui/login.*"), ("forgectrl", "src/ui/embed_docs.cmake"), ("forgectrl", "src/tls.*")],
      requires=["setup.first-run-flow"],
      actions=["button"], hands=["workstation"],
      steps=["From the workstation open https://<address>/ and accept the self-signed certificate.",
             "Click through the setup: scroll each document to its end and accept it (type I "
             "UNDERSTAND for the safety document); press the machine's button when it breathes "
             "teal; create a temporary account (any name and password: it is removed at the end); "
             "take the preferences, the machine, and the cloud step as offered. The intake fans "
             "show as counted already. The page then offers to continue the setup or to start "
             "using the machine: click Start using the machine.",
             "Confirm that the control panel opened."],
      description="The page against the real daemon over HTTPS, the one place a browser is the "
                  "client: the record seeded as a first run behind the checks and no account, "
                  "'/' serves the wizard with its choice after the cloud step and the intake fans, "
                  "and the test follows the record while the operator clicks through (the "
                  "documents accepted, the press step with the LEDs breathing teal, the account "
                  "created, preferences, machine, and cloud done and the gate open with the "
                  "intake fans counted, then Start using the machine: the run complete, the gate "
                  "open, the controller running). The operator confirms the panel opened. "
                  "Everything the run wrote is restored as setup.first-run-flow restores it.")
def first_run_page(ctx):
    fc = ctx.forgectrl
    ev = ctx.evidence
    ip = lan_ip()
    ctx.check(ip, "cannot determine the board's LAN address")
    with first_run(ctx) as state:
        st, page, hdrs = request("https://%s" % ip, "GET", "/")
        ev["root_page"] = st
        ctx.check(st == 200 and b'id="exitlink"' in page, "'/' over HTTPS does not serve the wizard (%s)", st)
        ctx.check(b'id="st-choice"' in page and b"Start using the machine" in page,
                  "the wizard carries no choice after the required steps")
        ctx.notice("Open https://%s/ from the workstation, accept the certificate, and click through "
                   "the setup as the steps describe. The test follows the record." % ip)
        took = ctx.wait_for(lambda: wiz(fc).get("advisories_complete") is True, 900)
        ctx.check(took is not None, "the four documents were not accepted within 15 min")
        ctx.log("the documents are accepted (%.0f s)", took)
        took = ctx.wait_for(lambda: wiz(fc).get("button") == "waiting", 300)
        ctx.check(took is not None, "the press step did not start within 5 min")
        took = ctx.wait_for(lambda: breathes_teal(led_pattern()), 10)
        pattern = led_pattern()
        ev["led_pattern"] = pattern
        ctx.check(took is not None, "the button LEDs do not breathe teal at the press step: %s", pattern)
        ctx.act("button", "press", until=lambda: wiz(fc).get("acceptance_done") is True, timeout=600,
                text="It breathes teal: this press is the acceptance. The page must be at the press step.")
        ctx.notice("Continue on the page: the account, the preferences, the machine, the cloud step, "
                   "then Start using the machine.")
        took = ctx.wait_for(lambda: (wiz(fc).get("users") or {}).get("exists") is True, 600)
        ctx.check(took is not None, "no account was created within 10 min")
        state["temp_account"] = (wiz(fc).get("users") or {}).get("name")
        ev["temp_account"] = state["temp_account"]
        ctx.log("account %r created (%.0f s)", state["temp_account"], took)
        took = ctx.wait_for(lambda: wiz(fc).get("gate") == "open", 900)
        w = wiz(fc)
        ev["after_required"] = wiz_summary(w)
        ctx.check(took is not None, "the gate did not open within 15 min of the account: %s", w.get("why"))
        done = w.get("versions") or {}
        ctx.check(all(done.get(wid) == 1 for wid in REQUIRED_STEPS) and w.get("fans_counted") is True,
                  "the gate opened without the required steps or the intake fans: %s", wiz_summary(w))
        ctx.log("preferences, machine, and cloud done; the gate is open (%.0f s)", took)
        took = ctx.wait_for(lambda: wiz(fc).get("completed") is True, 900)
        ctx.check(took is not None, "the setup did not complete within 15 min")
        finished_first_run(ctx)
        ctx.clear_notice()
        ctx.confirm("Did the control panel open after Start using the machine?")


# ------------------------------------------------------- the lifecycle

@test("setup.mirror", title="One browser drives a check; a second one follows",
      subsystem="setup", kind="auto", est_min=2, hardware="takeover",
      covers=[("forgectrl", "src/wizdark.*"), ("forgectrl", "src/wizcalc.*"), ("forgectrl", "src/wiz.*"),
              ("forgectrl", "src/session.*"), ("forgectrl", "src/ui/wizard.js"),
              ("forgectrl", "src/ui/wizard.html"), ("forgectrl", "src/ui/wizard.css")],
      requires=["forgectrl.auth", "setup.account-login", "setup.check-sensors"],
      description="With a temporary account (made and removed as the login test does) and two "
                  "login sessions: the first session starts the sensors check; GET /wiz/dark "
                  "reports the run as owned and its own to the first session, not to the second, "
                  "and as its own to a tool with the token and no session; the second session's "
                  "abort is refused (409); POST /wiz/sensors/takeover from the second session "
                  "makes the run its own and the first session's no longer; the second session's "
                  "abort then stops the check, which ends aborted. Both sessions are logged out; "
                  "the real account comes back under a restart.")
def mirror(ctx):
    fc = ctx.forgectrl
    ev = ctx.evidence
    base = tls_base()
    tok = {"X-ForgeFIRM-Token": fc.token}
    name = "forgetest"
    pw = "".join(random.choice("abcdefghjkmnpqrstuvwxyz23456789") for _ in range(16))
    before = wiz(fc)
    ev["account_before"] = (before.get("users") or {}).get("name")
    ctx.check(before.get("acceptance_done") is True, "the advisories are not accepted: no check can start")
    ctx.check(fc.wait_idle(timeout=30, abort=ctx.aborted), "machine not idle: the check cannot start")
    d = decode(request(base, "GET", "/wiz/dark", headers=tok)[1])
    ctx.check(isinstance(d, dict) and not d.get("running"), "a wizard is already running: %s", d)
    homes = "/data/forgefirm/home"

    def login():
        st, body, hdrs = request(base, "POST", "/login", data={"name": name, "password": pw})
        sid = session_from_cookie(hdrs.get("set-cookie") or "")
        ctx.check(st == 200 and sid, "POST /login -> %s, no session", st)
        return {"Cookie": "ffsid=" + (sid or "")}

    def status(hd):
        st, body, hdrs = request(base, "GET", "/wiz/dark", headers=hd)
        return decode(body) if st == 200 else {}

    with installed(ctx, {users_path(): None}):
        st, body = fc.post("/wiz/account", data={"name": name, "password": pw})
        ctx.check(st == 200, "the temporary account was not created: %s %s", st, body)
        a = login()
        b = login()
        a.update(tok)
        b.update(tok)
        try:
            st, body, hdrs = request(base, "POST", "/wiz/sensors/start", headers=a)
            ctx.log("POST /wiz/sensors/start (session A) -> %s", st)
            ctx.check(st == 200, "the sensors check did not start: %s %s", st, decode(body))
            da, db, dt = status(a), status(b), status(tok)
            ev["a_sees"] = {k: da.get(k) for k in ("running", "owned", "mine")}
            ev["b_sees"] = {k: db.get(k) for k in ("running", "owned", "mine")}
            ev["tool_sees"] = {k: dt.get(k) for k in ("running", "owned", "mine")}
            ctx.check(da.get("running") and da.get("owned") and da.get("mine"),
                      "session A does not own its run: %s", ev["a_sees"])
            ctx.check(db.get("owned") and db.get("mine") is False, "session B is not a mirror: %s", ev["b_sees"])
            ctx.check(dt.get("mine"), "a tool with the token reads as a mirror: %s", ev["tool_sees"])
            st, body, hdrs = request(base, "POST", "/wiz/sensors/abort", headers=b)
            ev["b_abort_refused"] = st
            ctx.log("POST /wiz/sensors/abort (session B) -> %s %s", st, decode(body))
            ctx.check(st == 409, "the mirror's abort -> %s, expected 409", st)
            ctx.check(status(a).get("running"), "the mirror's abort stopped the run")
            st, body, hdrs = request(base, "POST", "/wiz/sensors/takeover", headers=b)
            ctx.log("POST /wiz/sensors/takeover (session B) -> %s", st)
            ctx.check(st == 200, "the takeover -> %s", st)
            da, db = status(a), status(b)
            ev["after_takeover"] = {"a": da.get("mine"), "b": db.get("mine")}
            ctx.check(db.get("mine") and da.get("mine") is False,
                      "after the takeover B is not the owner or A still is: %s", ev["after_takeover"])
            st, body, hdrs = request(base, "POST", "/wiz/sensors/abort", headers=b)
            ctx.log("POST /wiz/sensors/abort (session B, owner) -> %s", st)
            ctx.check(st == 200, "the owner's abort -> %s", st)
        finally:
            request(base, "POST", "/wiz/sensors/abort", headers=tok)
            t0 = time.time()
            while time.time() - t0 < 60 and status(tok).get("running"):
                ctx.sleep(1)
            d = status(tok)
            ev["end"] = {"running": d.get("running"), "error": d.get("error")}
            ctx.check(not d.get("running"), "the check did not stop within 60 s")
            ctx.check((d.get("error") or "") == "aborted", "the check ended %r, expected aborted", d.get("error"))
            for hd in (a, b):
                request(base, "POST", "/logout", headers=hd)
    rc, out = hw.initd("forgefirm-users", "reload")
    ev["users_reload_rc"] = rc
    if name != ev["account_before"]:
        import shutil
        shutil.rmtree(os.path.join(homes, name), ignore_errors=True)
    after = wiz(fc)
    ctx.check((after.get("users") or {}).get("name") == ev["account_before"],
              "the account reads %r after the restore, was %r", (after.get("users") or {}).get("name"),
              ev["account_before"])
    ctx.log("A owned the run, B mirrored, B took over and stopped it; the account is back")
