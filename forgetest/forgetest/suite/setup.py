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
