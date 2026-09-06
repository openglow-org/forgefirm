"""commission.* - the first-run commissioning: the record and the
controller gate, the advisories, the account and its login, the HTTPS
boundary, SSH, the cloud switch, the mDNS name, and the factory return.

The daemon reads the commissioning record (commissioning.json in the
data directory) and the account record (users) once, at its start, and
keeps them in memory; only its own routes write them. A test that needs
a different record installs the file under a forgectrl restart (a
takeover) and puts the previous file back the same way. Thus the daemon
never saves a test record over the real one, and a restore is complete
when the daemon is up again.

The layer content of this work (the account replay init script, the
sshd policy, the console banner, the avahi service) is part of the
platform identity that every fingerprint carries, not of a component.
No coverage map names it: a change there makes every test necessary
again.
"""
import contextlib
import hashlib
import posixpath
import http.client
import json
import os
import random
import select
import socket
import ssl
import struct
import time
import urllib.parse

from ..catalog import test
from .. import hw
from .forgectrl import lan_ip

RECORD = "commissioning.json"
USERS = "users"
OVERRIDE = "commissioning-override"
SSH_FLAG = "ssh-enabled"
TLS_BASE = "https://127.0.0.1"          # FORGECTRL_TLS_URL overrides (host tests)

# The image's required wizards, all at version 1 (commission.c).
REQUIRED_WIZARDS = ("advisories", "account", "preferences", "machine", "cloud")
ADVISORY_DOCS = ("safety-and-risk", "licenses", "privacy", "cloud-service")
SAFETY_PHRASE = "I UNDERSTAND"
LOGIN_FAILS = 5
LOGIN_LOCK_S = 30
MDNS_NAME = "forgefirm.local"
MDNS_GROUP = "224.0.0.251"
MDNS_PORT = 5353


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


def override_path():
    return posixpath.join(run_dir(), OVERRIDE)


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


def override_present():
    return os.path.exists(override_path())


def set_override(on):
    write_file(override_path(), b"" if on else None)


def tmpfs_mount(path):
    """The (mount point, fstype) that holds path, from /proc/mounts;
    (None, None) when unreadable."""
    best = (None, None)
    try:
        with open("/proc/mounts", "r", encoding="utf-8", errors="replace") as f:
            for line in f:
                parts = line.split()
                if len(parts) < 3:
                    continue
                mp = parts[1]
                if path == mp or path.startswith(mp.rstrip("/") + "/"):
                    if best[0] is None or len(mp) > len(best[0]):
                        best = (mp, parts[2])
    except OSError:
        pass
    return best


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
    consent and the account stay, so the gate closes on the wizard part
    alone, which is the part the override lifts."""
    out = json.loads(json.dumps(record))
    out["wizards"] = {}
    out["flags"] = {}
    return out


def complete_record(status, base=None, account_name="bench"):
    """A complete commissioning record for a machine that must count as
    commissioned: every advisory at the hash GET /wiz reports, the
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
    wizards = status.get("wizards") or [{"id": w, "version": 1} for w in REQUIRED_WIZARDS]
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
    return {k: w.get(k) for k in ("first_run", "gate", "override", "why", "required",
                                  "advisories_complete", "acceptance_done", "account",
                                  "completed", "versions")}


def wait_settled(ctx):
    """The supervisor settled (the runner's own wait): the /mode body."""
    return ctx.takeover().wait_settled()


# ------------------------------------------------------------- prechecks

def gate_is_open():
    """Precheck: the tests that close the gate start from an open one."""
    try:
        w = wiz(hw.Forgectrl())
    except hw.HwError as e:
        return "forgectrl unreachable: %s" % e
    if w.get("gate") != "open":
        return ("the controller gate is closed (%s); the test starts from an open gate"
                % (w.get("why") or "no reason"))
    return None



# -------------------------------------------------------------- the gate

_GATE_COVERS = [("forgectrl", "src/commission.*"), ("forgectrl", "src/super.c"),
                ("forgectrl", "src/main.c"), ("forgectrl", "src/paths.h")]


@test("commission.gate-blocks-controllers", title="The commissioning gate spawns no controller",
      subsystem="commission", kind="auto", hardware="takeover", mode="grbl", est_min=5,
      covers=_GATE_COVERS, requires=["forgectrl.auth"], precheck=gate_is_open,
      description="With the override removed and a record that lacks every required wizard "
                  "(installed under a forgectrl restart, the consent and the account kept), "
                  "GET /mode reports the controller gated with the reason naming the wizards, "
                  "GET /wiz lists them as required, no controller process runs and the Grbl port "
                  "is closed. The override file then opens the gate live: the supervisor spawns "
                  "the controller and it comes back running. The real record and the override "
                  "state are restored under another restart and the machine reads as before.")
def gate_blocks_controllers(ctx):
    fc = ctx.forgectrl
    ev = ctx.evidence
    before = wiz(fc)
    ev["before"] = wiz_summary(before)
    ctx.log("before: gate %s, override %s, required %s", before.get("gate"), before.get("override"),
            before.get("required"))
    raw = read_file(record_path())
    ctx.check(raw, "no commissioning record at %s", record_path())
    try:
        record = json.loads(raw.decode("utf-8"))
    except ValueError as e:
        ctx.fail("the commissioning record is not JSON: %s", e)
    lacking = json.dumps(record_without_wizards(record), indent=1, sort_keys=True).encode() + b"\n"
    had_override = override_present()
    ev["override_before"] = had_override

    with installed(ctx, {record_path(): lacking, override_path(): None}):
        # the daemon is up again with the record that lacks the wizards
        took = ctx.wait_for(lambda: mode(fc).get("controller") == "gated", 60)
        m = mode(fc)
        ev["gated_mode"] = m
        ctx.log("/mode after the restart: %s (after %s s)", m, took)
        ctx.check(m.get("controller") == "gated" and m.get("gated") is True,
                  "the supervisor did not report the gate: %s", m)
        ctx.check("commissioning required" in (m.get("why") or ""),
                  "the reason does not name the missing wizards: %r", m.get("why"))
        w = wiz(fc)
        ev["gated_wiz"] = wiz_summary(w)
        ctx.check(w.get("gate") == "closed" and w.get("override") is False,
                  "GET /wiz does not agree with the closed gate: %s", wiz_summary(w))
        ctx.check(set(w.get("required") or []) >= set(REQUIRED_WIZARDS),
                  "GET /wiz does not list every required wizard: %s", w.get("required"))
        ctx.check(not hw.grbl_port_open(timeout=2), "the Grbl port answers while the gate is closed")
        pids = hw.pidof("grblHAL_glowfor") + hw.pgrep_f("gfcloud.py")
        ev["controller_pids_gated"] = pids
        ctx.check(not pids, "a controller process runs while the gate is closed: %s", pids)
        ctx.log("gated: no controller process, the Grbl port is closed")

        # the override lifts the wizard part live: no restart
        set_override(True)
        ctx.wait_for(lambda: wiz(fc).get("gate") == "open", 15)
        w = wiz(fc)
        ev["override_wiz"] = wiz_summary(w)
        ctx.check(w.get("gate") == "open" and w.get("override") is True,
                  "the override did not open the gate: %s", wiz_summary(w))
        ctx.check((w.get("why") or "").startswith("override active"),
                  "the reason does not say the override stands: %r", w.get("why"))
        # The supervisor spawns on its next pass; a gated machine counts
        # as settled, so wait for the spawn before the settle wait.
        took = ctx.wait_for(lambda: mode(fc).get("controller") == "running", 60)
        ctx.log("the controller spawned under the override (%s s)", took)
        m = wait_settled(ctx)
        ev["override_mode"] = m
        ctx.check(took is not None and isinstance(m, dict) and m.get("controller") == "running",
                  "the controller did not come back under the override: %s", m)
        ctx.check(hw.grbl_port_open(timeout=5), "the Grbl port is closed with the controller running")
        ctx.log("override: the controller is back (%s)", m)

    # the real record is back under a restart; the override reads as found
    after = wiz(fc)
    ev["after"] = wiz_summary(after)
    ctx.check(after.get("gate") == before.get("gate"),
              "the gate reads %s after the restore, was %s", after.get("gate"), before.get("gate"))
    ctx.check(sorted(after.get("required") or []) == sorted(before.get("required") or []),
              "the required list changed: %s -> %s", before.get("required"), after.get("required"))
    ctx.check(override_present() == had_override, "the override file does not read as found")
    m = mode(fc)
    ev["after_mode"] = m
    ctx.check(m.get("controller") == "running", "the controller is %s after the restore", m.get("controller"))
    ctx.log("restored: gate %s, controller %s", after.get("gate"), m.get("controller"))


@test("commission.override-until-reboot", title="The override stands until the next reboot",
      subsystem="commission", kind="auto", est_min=1,
      covers=_GATE_COVERS, requires=["forgectrl.auth"],
      description="The bench seed: the dev image's forgetest init script writes "
                  "/run/forgefirm/commissioning-override at boot. With it present, GET /wiz "
                  "reports the override and an open gate, the reason names the wizards it lifts "
                  "when any is required, and GET /mode reports the controller running and not "
                  "gated. The file lives on tmpfs (/proc/mounts), so a reboot removes it by "
                  "construction and the gate is then evaluated from the record alone. The "
                  "override never lifts the advisories or the account.")
def override_until_reboot(ctx):
    fc = ctx.forgectrl
    ev = ctx.evidence
    path = override_path()
    ctx.check(os.path.exists(path),
              "no override at %s: the dev image's forgetest init script creates it at boot", path)
    mp, fstype = tmpfs_mount(os.path.dirname(path))
    ev["mount"] = {"point": mp, "fstype": fstype}
    ctx.log("%s is on %s (%s)", path, mp, fstype)
    ctx.check(fstype == "tmpfs", "%s is not on tmpfs (%s on %s): a reboot would keep the override",
              path, fstype, mp)

    w = wiz(fc)
    ev["wiz"] = wiz_summary(w)
    ctx.log("GET /wiz: %s", ev["wiz"])
    ctx.check(w.get("override") is True, "GET /wiz does not see the override")
    ctx.check(w.get("gate") == "open", "the gate is closed under the override: %s", w.get("why"))
    ctx.check(w.get("advisories_complete") is True and w.get("acceptance_done") is True
              and w.get("account") is True,
              "the consent or the account is missing (%s): nothing lifts those", wiz_summary(w))
    required = [r if isinstance(r, str) else (r or {}).get("id") for r in w.get("required") or []]
    why = w.get("why") or ""
    if required:
        ctx.check(why.startswith("override active") and all(str(r) in why for r in required),
                  "the reason does not name the lifted wizards %s: %r", required, why)
        ctx.log("the override lifts: %s", required)
    else:
        ctx.check(why == "", "every wizard is complete but the reason reads %r", why)
        ctx.log("every required wizard is complete; the override lifts nothing today")
    m = mode(fc)
    ev["mode"] = m
    ctx.check(m.get("gated") is False and m.get("controller") == "running",
              "the supervisor is gated or the controller is down: %s", m)


# ------------------------------------------------------------ advisories

@test("commission.advisories-rehash", title="An advisory is accepted at its current hash only",
      subsystem="commission", kind="auto", hardware="takeover", est_min=3,
      covers=[("forgectrl", "src/advisories.*"), ("forgectrl", "src/commission.*"),
              ("forgectrl", "src/wiz.*"), ("forgectrl", "src/sha256.*"), ("forgectrl", "src/main.c"),
              ("forgectrl", "src/ui/md.js"), ("forgectrl", "src/ui/embed_docs.cmake")],
      requires=["forgectrl.auth"],
      description="GET /advisories/safety-and-risk serves the document as markdown with an ETag "
                  "equal to the SHA-256 of the body and to the hash GET /wiz lists; an unknown "
                  "document is refused (400); an accept with a stale hash is refused (409); the "
                  "typed document without its phrase is refused (400); the accept at the current "
                  "hash with the phrase is recorded (200), and any new acceptance clears the "
                  "press (acceptance_done false, the gate closed on the consent, which the "
                  "override never lifts). The previous record is put back under a forgectrl "
                  "restart and the machine reads as before.")
def advisories_rehash(ctx):
    fc = ctx.forgectrl
    ev = ctx.evidence
    raw = read_file(record_path())
    ctx.check(raw, "no commissioning record at %s", record_path())
    before = wiz(fc)
    ev["before"] = wiz_summary(before)
    docs = {d.get("id"): d for d in before.get("documents") or []}
    ctx.check(set(docs) >= set(ADVISORY_DOCS), "GET /wiz lists %s, expected %s", sorted(docs), ADVISORY_DOCS)
    doc = docs["safety-and-risk"]
    ctx.check(doc.get("consent") == "typed" and doc.get("phrase") == SAFETY_PHRASE,
              "the safety document is not the typed one: %s", doc)

    st, body, hdrs = request(fc.base, "GET", "/advisories/safety-and-risk", headers={"Host": fc.host_header()})
    etag = hdrs.get("etag")
    ev["etag"] = etag
    ev["content_type"] = hdrs.get("content-type")
    ctx.log("GET /advisories/safety-and-risk -> %s, %d bytes, ETag %s", st, len(body), etag)
    ctx.check(st == 200 and body, "GET /advisories/safety-and-risk -> %s", st)
    ctx.check((hdrs.get("content-type") or "").startswith("text/markdown"),
              "the document is not served as markdown: %r", hdrs.get("content-type"))
    ctx.check(etag == hashlib.sha256(body).hexdigest(), "the ETag is not the SHA-256 of the body")
    ctx.check(etag == doc.get("hash"), "the ETag %s differs from the hash GET /wiz lists %s", etag, doc.get("hash"))
    st, body, hdrs = request(fc.base, "GET", "/advisories/no-such-document", headers={"Host": fc.host_header()})
    ctx.check(st == 404, "an unknown document -> %s, expected 404", st)

    st, body = fc.post("/wiz/advisories/accept", data={"doc": "no-such-document", "hash": etag})
    ev["unknown_doc"] = st
    ctx.check(st == 400, "an accept of an unknown document -> %s, expected 400", st)
    st, body = fc.post("/wiz/advisories/accept",
                       data={"doc": "safety-and-risk", "hash": "0" * 64, "phrase": SAFETY_PHRASE})
    ev["stale_hash"] = st
    ctx.log("accept with a stale hash -> %s %s", st, body if isinstance(body, dict) else "")
    ctx.check(st == 409, "an accept with a stale hash -> %s, expected 409", st)
    st, body = fc.post("/wiz/advisories/accept", data={"doc": "safety-and-risk", "hash": etag})
    ev["no_phrase"] = st
    ctx.check(st == 400, "the typed document without its phrase -> %s, expected 400", st)
    w = wiz(fc)
    ctx.check(w.get("acceptance_done") == before.get("acceptance_done"),
              "a refused accept changed the press record")

    try:
        st, body = fc.post("/wiz/advisories/accept",
                           data={"doc": "safety-and-risk", "hash": etag, "phrase": SAFETY_PHRASE})
        ev["accept"] = st
        ctx.log("accept at the current hash with the phrase -> %s", st)
        ctx.check(st == 200 and isinstance(body, dict), "the accept -> %s %r", st, body)
        after = wiz(fc)
        ev["after_accept"] = wiz_summary(after)
        accepted = {d.get("id"): d.get("accepted") for d in after.get("documents") or []}
        ctx.check(accepted.get("safety-and-risk") is True, "the document does not read accepted")
        ctx.check(after.get("acceptance_done") is False,
                  "a new acceptance did not clear the press (acceptance_done %s)", after.get("acceptance_done"))
        ctx.check(after.get("gate") == "closed" and "not accepted" in (after.get("why") or ""),
                  "the gate does not close on the cleared press: %s", wiz_summary(after))
        # the record on disk moved with it
        try:
            on_disk = json.loads((read_file(record_path()) or b"{}").decode("utf-8"))
        except ValueError:
            on_disk = {}
        ctx.check("acceptance" not in on_disk and
                  (on_disk.get("advisories") or {}).get("safety-and-risk", {}).get("hash") == etag,
                  "the record on disk does not carry the acceptance")
        ctx.log("acceptance_done cleared, gate %s: %s", after.get("gate"), after.get("why"))
    finally:
        with ctx.takeover():
            write_file(record_path(), raw)
        ctx.log("the previous record is back under a restart")

    final = wiz(fc)
    ev["after_restore"] = wiz_summary(final)
    ctx.check(final.get("acceptance_done") == before.get("acceptance_done")
              and final.get("gate") == before.get("gate")
              and final.get("advisories_complete") == before.get("advisories_complete"),
              "the machine does not read as before: %s", wiz_summary(final))


# -------------------------------------------------------------- the login

@test("commission.account-login", title="The panel login over HTTPS", subsystem="commission",
      kind="auto", est_min=2,
      covers=[("forgectrl", "src/auth.*"), ("forgectrl", "src/session.*"), ("forgectrl", "src/users.*"),
              ("forgectrl", "src/tls.*"), ("forgectrl", "src/peer.*"), ("forgectrl", "src/main.c"),
              ("forgectrl", "src/ui/login.*")],
      requires=["forgectrl.auth"], hardware="takeover",
      description="The test makes its own account: the account record is moved aside under a "
                  "forgectrl restart, the account route creates a temporary one with a password "
                  "only the test knows, and the real record comes back under another restart at "
                  "the end, the system accounts replayed and the temporary home removed. Over "
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
    with installed(ctx, {users_path(): None}):
        w = wiz(fc)
        ctx.check(not (w.get("users") or {}).get("exists"), "an account still exists: %s", w.get("users"))
        st, body = fc.post("/wiz/account", data={"name": name, "password": pw})
        ctx.log("POST /wiz/account (temporary %r) -> %s %s", name, st, body if isinstance(body, dict) else "")
        ctx.check(st == 200, "the temporary account was not created: %s %s", st, body)
        w = wiz(fc)
        ctx.check((w.get("users") or {}).get("name") == name, "the account reads %s", w.get("users"))
        login_checks(ctx, name, pw)
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

@test("commission.https-only-writes", title="Writes from the LAN go to HTTPS", subsystem="commission",
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
    """The three ForgeFIRM keys of the effective sshd policy (sshd -T,
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
                                                "passwordauthentication"):
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


@test("commission.ssh-until-reboot", title="SSH is switched from the panel until the next reboot",
      subsystem="commission", kind="auto", est_min=1,
      covers=[("forgectrl", "src/main.c"), ("forgectrl", "src/paths.h")],
      requires=["forgectrl.auth"],
      description="GET /system/ssh reports enabled, running, and dev_image, and agrees with the "
                  "flag file and port 22. POST enable=1 writes /run/forgefirm/ssh-enabled and "
                  "sshd listens on 22, with the effective policy (sshd -T) at PasswordAuthentication "
                  "yes, and on a release image PermitRootLogin no and PermitEmptyPasswords no (the "
                  "dev image turns root over SSH back on for the bench; the release gate checks "
                  "the release image's sshd_config as built). An enable outside 0 and 1 is refused (400). On a release "
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


# ---------------------------------------------------------------- cloud

@test("commission.cloud-disabled-surface", title="Nothing points at the cloud while it is off",
      subsystem="commission", kind="auto", mode="grbl", est_min=1,
      covers=[("forgectrl", "src/main.c"), ("forgectrl", "src/super.c"), ("forgectrl", "src/settings.*"),
              ("forgectrl", "src/wiz.c"), ("forgectrl", "src/hooks.h")],
      requires=["forgectrl.settings-bounds"],
      description="The test turns cloud mode off itself with one write and puts every setting "
                  "back as found. cloud_enabled=0 takes the gfcloud homing and the cloud boot mode "
                  "down with it, as the cloud step does. With it at 0, POST /settings "
                  "controller_mode=cloud and homing_mode=gfcloud are refused (409) and leave the "
                  "settings unchanged, cloud_enabled=1 without the typed phrase is refused (400) "
                  "and leaves it at 0, and POST /mode controller=cloud is refused (409) with a "
                  "message that names cloud mode, the machine staying in GRBL mode.")
def cloud_disabled_surface(ctx):
    fc = ctx.forgectrl
    ev = ctx.evidence
    ctx.check(fc.wait_idle(timeout=30, abort=ctx.aborted), "machine not idle: settings are locked")
    found = fc.settings()
    prior = found.get("cloud_enabled") or ""
    ev["found"] = {k: found.get(k) for k in ("cloud_enabled", "homing_mode", "controller_mode")}
    try:
        if prior != "0":
            # the one write sweeps what pointed at the cloud
            st, body = fc.post("/settings", data={"cloud_enabled": "0"})
            ctx.check(st == 200, "cloud_enabled=0 -> %s %s", st, body)
            ctx.log("cloud mode turned off for the test (found %s)", ev["found"])
            swept = fc.settings()
            if found.get("homing_mode") == "gfcloud":
                ctx.check(swept.get("homing_mode") == "none",
                          "cloud_enabled=0 left homing_mode at %r, expected none", swept.get("homing_mode"))
            if found.get("controller_mode") == "cloud":
                ctx.check(swept.get("controller_mode") == "grbl",
                          "cloud_enabled=0 left controller_mode at %r, expected grbl",
                          swept.get("controller_mode"))
        ctx.check(fc.settings().get("cloud_enabled") == "0", "cloud_enabled does not read 0")
        before = fc.settings()
        for key, val in (("controller_mode", "cloud"), ("homing_mode", "gfcloud")):
            st, body = fc.post("/settings", data={key: val})
            ev["settings %s=%s" % (key, val)] = st
            ctx.log("POST /settings %s=%s -> %s %s", key, val, st, body if isinstance(body, str) else "")
            ctx.check(st == 409, "%s=%s with cloud off -> %s, expected 409", key, val, st)
        # the pair in one request is refused too: the request's own switch counts
        st, body = fc.post("/settings", data={"cloud_enabled": "0", "controller_mode": "cloud"})
        ctx.check(st == 409, "cloud_enabled=0 with controller_mode=cloud -> %s, expected 409", st)
        after = fc.settings()
        for key in ("controller_mode", "homing_mode"):
            ctx.check(after.get(key) == before.get(key), "%s changed by a refused write: %r -> %r",
                      key, before.get(key), after.get(key))
        # on again takes the typed phrase, as the cloud step asks for it
        st, body = fc.post("/settings", data={"cloud_enabled": "1"})
        ev["settings cloud_enabled=1 no phrase"] = st
        ctx.log("POST /settings cloud_enabled=1 (no phrase) -> %s %s", st, body if isinstance(body, str) else "")
        ctx.check(st == 400, "cloud_enabled=1 without the phrase -> %s, expected 400", st)
        ctx.check(fc.settings().get("cloud_enabled") == "0", "a refused cloud_enabled=1 changed the setting")
        st, body = fc.post("/mode", data={"controller": "cloud"})
        text = body if isinstance(body, str) else json.dumps(body)
        ev["mode_cloud"] = {"status": st, "body": text}
        ctx.log("POST /mode controller=cloud -> %s %s", st, text)
        ctx.check(st == 409, "POST /mode controller=cloud with cloud off -> %s, expected 409", st)
        ctx.check("cloud mode" in text.lower(), "the refusal does not name cloud mode: %r", text)
        m = mode(fc)
        ctx.check(m.get("mode") == "grbl" and m.get("controller") == "running",
                  "the machine did not stay in GRBL mode: %s", m)
    finally:
        if prior == "":
            st, body = fc.post("/settings", params={"cloud_enabled": ""})
        elif prior == "1":
            st, body = fc.post("/settings", data={"cloud_enabled": "1", "phrase": "I UNDERSTAND"})
        else:
            st, body = fc.post("/settings", data={"cloud_enabled": prior})
        ctx.log("restore cloud_enabled=%r -> %s", prior, st)
        # the swept choices go back after cloud mode is on again
        for key, cloud_val in (("homing_mode", "gfcloud"), ("controller_mode", "cloud")):
            if found.get(key) == cloud_val:
                st, body = fc.post("/settings", data={key: cloud_val})
                ctx.log("restore %s=%r -> %s", key, cloud_val, st)
    after = fc.settings()
    for key in ("cloud_enabled", "homing_mode", "controller_mode"):
        ctx.check((after.get(key) or "") == (found.get(key) or ""), "%s not restored: %r, was %r",
                  key, after.get(key), found.get(key))


# ---------------------------------------------------------- the operator

@test("commission.factory-return", title="The return to the factory firmware is guarded",
      subsystem="commission", kind="auto", est_min=1,
      covers=[("forgectrl", "src/update.*"), ("forgectrl", "src/main.c"), ("forgectrl", "src/ui/wizard.js")],
      requires=["update.slots-and-signature"],
      description="The return itself reboots the machine as a Glowforge, so it never runs from the "
                  "catalog (it is a bench drill, logged once per release in CAMPAIGN-LOG). The "
                  "test probes the guards: POST /restore/factory-return without confirm=1 is "
                  "refused (400) and with confirm=0 too, no update job starts, /slots lists the "
                  "archived factory images, and the setup page carries the footer link's call "
                  "with confirm=1 behind its confirmation.")
def factory_return(ctx):
    fc = ctx.forgectrl
    ev = ctx.evidence
    st, body = fc.post("/restore/factory-return")
    ev["no_confirm"] = {"status": st, "body": body if isinstance(body, (str, dict)) else None}
    ctx.log("POST /restore/factory-return (no confirm) -> %s %s", st, body)
    ctx.check(st == 400 and "confirm" in str(body), "the return without confirm=1 -> %s %s, expected 400", st, body)
    st, body = fc.post("/restore/factory-return", params={"confirm": "0"})
    ctx.check(st == 400, "the return with confirm=0 -> %s, expected 400", st)
    st, us = fc.get("/update/status")
    ctx.check(st == 200 and isinstance(us, dict) and not us.get("running"),
              "an update job runs after the refused returns: %s", us)
    st, slots = fc.get("/slots")
    archives = (slots or {}).get("archives") if isinstance(slots, dict) else None
    ev["archives"] = archives
    ctx.log("GET /slots archives: %s", archives)
    ctx.check(st == 200 and isinstance(archives, list), "GET /slots -> %s without an archive list", st)
    if not archives:
        ctx.log("no archived factory image on this machine: the return would be refused (409) and "
                "the factory recovery mode is the way back")
    st, page = fc.get("/setup", raw=True)
    ev["setup_page"] = {"status": st, "bytes": len(page or b"")}
    ctx.check(st == 200 and b"/restore/factory-return?confirm=1" in (page or b""),
              "the setup page does not carry the footer link's confirmed call (%s)", st)
    ctx.log("PASS: the return is refused without confirm=1; the page confirms before it calls")


# ----------------------------------------------------------------- mDNS

def mdns_query(name, qid=None):
    """A DNS query packet for the A record of name with the unicast-
    response bit set (RFC 6762 5.4), so the responder answers this
    socket directly."""
    qid = random.randrange(1, 65536) if qid is None else qid
    labels = b"".join(struct.pack("B", len(p)) + p.encode("ascii") for p in name.strip(".").split("."))
    return struct.pack(">HHHHHH", qid, 0, 1, 0, 0, 0) + labels + b"\x00" + struct.pack(">HH", 1, 0x8001)


def _dns_name(pkt, off):
    """(name, offset after the name); follows compression pointers."""
    parts = []
    jumped = False
    end = off
    hops = 0
    while True:
        if off >= len(pkt):
            raise ValueError("truncated name")
        n = pkt[off]
        if n == 0:
            off += 1
            break
        if n & 0xC0 == 0xC0:
            if off + 1 >= len(pkt):
                raise ValueError("truncated pointer")
            ptr = ((n & 0x3F) << 8) | pkt[off + 1]
            if not jumped:
                end = off + 2
            jumped = True
            off = ptr
            hops += 1
            if hops > 32:
                raise ValueError("pointer loop")
            continue
        off += 1
        parts.append(pkt[off:off + n].decode("ascii", "replace"))
        off += n
    if not jumped:
        end = off
    return ".".join(parts), end


def mdns_answers(pkt, qid=None):
    """The A records of a DNS response: [(name, address)]. A qid, when
    given, must match the packet's id; the answer flag (QR) must be set."""
    if len(pkt) < 12:
        return []
    pid, flags, qd, an, ns, ar = struct.unpack(">HHHHHH", pkt[:12])
    if qid is not None and pid != qid:
        return []
    if not flags & 0x8000:
        return []
    off = 12
    try:
        for _ in range(qd):
            _name, off = _dns_name(pkt, off)
            off += 4
        out = []
        for _ in range(an + ns + ar):
            name, off = _dns_name(pkt, off)
            if off + 10 > len(pkt):
                break
            rtype, rclass, _ttl, rdlen = struct.unpack(">HHIH", pkt[off:off + 10])
            off += 10
            rdata = pkt[off:off + rdlen]
            off += rdlen
            if rtype == 1 and rdlen == 4:
                out.append((name.lower(), socket.inet_ntoa(rdata)))
        return out
    except ValueError:
        return []


def mdns_resolve(ip, name=MDNS_NAME, timeout=3.0, tries=3):
    """Ask the LAN for the A record of name over mDNS from the interface
    that holds ip, and collect the answers: {address}. The query goes to
    the multicast group; the responder on this machine answers the
    unicast-response bit directly (a legacy query from a port that is
    not 5353 is answered the same way), and a listener on 5353 catches
    a multicast answer too."""
    found = set()
    qid = random.randrange(1, 65536)
    q = mdns_query(name, qid)
    tx = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    rx = None
    try:
        tx.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        tx.bind((ip, 0))
        tx.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_IF, socket.inet_aton(ip))
        tx.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_TTL, 255)
        tx.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_LOOP, 1)
        try:
            rx = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            rx.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            if hasattr(socket, "SO_REUSEPORT"):
                rx.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEPORT, 1)
            rx.bind(("", MDNS_PORT))
            rx.setsockopt(socket.IPPROTO_IP, socket.IP_ADD_MEMBERSHIP,
                          socket.inet_aton(MDNS_GROUP) + socket.inet_aton(ip))
        except OSError:
            rx = None
        socks = [s for s in (tx, rx) if s is not None]
        for _ in range(tries):
            tx.sendto(q, (MDNS_GROUP, MDNS_PORT))
            deadline = time.time() + timeout
            while time.time() < deadline:
                ready, _w, _x = select.select(socks, [], [], max(0.05, deadline - time.time()))
                for s in ready:
                    try:
                        pkt, _peer = s.recvfrom(4096)
                    except OSError:
                        continue
                    # a multicast answer carries id 0; the direct one echoes the query's
                    for n, addr in mdns_answers(pkt, qid if s is tx else None):
                        if n == name.lower():
                            found.add(addr)
                if found:
                    return found
    finally:
        tx.close()
        if rx is not None:
            rx.close()
    return found


@test("commission.mdns-announce", title="The machine answers forgefirm.local", subsystem="commission",
      kind="auto", est_min=1,
      requires=["forgectrl.auth"],
      description="avahi-daemon runs, its configuration names the host forgefirm on the WiFi and "
                  "wired links, and the service file advertises the panel on 443 and 80. An mDNS "
                  "query for the A record of forgefirm.local, sent to the multicast group from "
                  "the board's own LAN interface, is answered with the board's LAN address; that "
                  "is what `ping forgefirm.local` on a workstation resolves. No component "
                  "covers this: the avahi files are layer content, in the platform identity of "
                  "every fingerprint.")
def mdns_announce(ctx):
    ev = ctx.evidence
    pids = hw.pidof("avahi-daemon")
    ev["avahi_pids"] = pids
    ctx.check(pids, "avahi-daemon is not running")
    conf = read_file("/etc/avahi/avahi-daemon.conf") or b""
    ctx.check(b"host-name=forgefirm" in conf, "avahi-daemon.conf does not name the host forgefirm")
    ctx.check(b"allow-interfaces=wlan0,eth0" in conf, "avahi-daemon.conf does not restrict the interfaces")
    svc = read_file("/etc/avahi/services/forgefirm.service") or b""
    ev["service_file_bytes"] = len(svc)
    ctx.check(b"_https._tcp" in svc and b"<port>443</port>" in svc, "the service file lacks HTTPS 443")
    ctx.check(b"_http._tcp" in svc and b"<port>80</port>" in svc, "the service file lacks HTTP 80")
    ip = lan_ip()
    ev["lan_ip"] = ip
    ctx.check(ip, "cannot determine the board's LAN address")
    t0 = time.time()
    found = mdns_resolve(ip)
    ev["answers"] = sorted(found)
    ev["resolve_s"] = round(time.time() - t0, 2)
    ctx.log("%s -> %s (%.1f s)", MDNS_NAME, sorted(found) or "no answer", ev["resolve_s"])
    ctx.check(found, "no mDNS answer for %s from the LAN interface (%s)", MDNS_NAME, ip)
    ctx.check(ip in found, "%s resolves to %s, not the LAN address %s", MDNS_NAME, sorted(found), ip)


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
    """A record that reads as a first run with the checks and the sheet
    behind it: no consent, no press, no account, no form wizard, not
    complete; every check and live wizard of the catalog kept from
    `base` (the real record) or written at its catalog version. It is
    the machine after the button-hold reset of the consent and the
    account, and it lets the first run reach Finish without the hour of
    checks and burns that a fresh machine owes."""
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
    restart, with the override kept; the settings the form steps write
    put back as found; the real records restored under another restart,
    the system accounts replayed from them, and the temporary home
    directory removed. Yields a dict the block fills with `temp_account`."""
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
    with Restore(ctx, FIRST_RUN_SETTINGS):
        with installed(ctx, {record_path(): seed, users_path(): None, override_path(): b""}):
            w = wiz(fc)
            ev["fresh"] = wiz_summary(w)
            ctx.check(w.get("first_run") is True and not (w.get("users") or {}).get("exists")
                      and w.get("advisories_complete") is False and w.get("acceptance_done") is False,
                      "the machine does not read as a first run: %s", wiz_summary(w))
            m = mode(fc)
            ctx.check(m.get("controller") == "gated", "the controller is %s on a first run", m.get("controller"))
            yield state
    # the real records are back; the system accounts follow the record
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
    controller back."""
    fc = ctx.forgectrl
    ev = ctx.evidence
    w = wiz(fc)
    ev["completed"] = wiz_summary(w)
    ctx.check(w.get("completed") is True and w.get("gate") == "open" and not w.get("required"),
              "the run did not complete with the gate open: %s", wiz_summary(w))
    m = wait_settled(ctx)
    ev["mode_after_setup"] = m
    ctx.check(isinstance(m, dict) and m.get("controller") == "running",
              "the controller did not come back after the setup: %s", m)


_FIRST_RUN_REQUIRES = ["commission.gate-blocks-controllers", "commission.advisories-rehash"]


@test("commission.first-run-flow", title="The first run, driven as the page drives it", subsystem="commission",
      kind="operator", hardware="takeover", est_min=3,
      covers=[("forgectrl", "src/wiz.*"), ("forgectrl", "src/commission.*"), ("forgectrl", "src/users.*"),
              ("forgectrl", "src/session.*"), ("forgectrl", "src/button.*"), ("forgectrl", "src/led.*"),
              ("forgectrl", "src/sheetid.*"), ("forgectrl", "src/advisories.*"), ("forgectrl", "src/hooks.h"),
              ("forgectrl", "src/main.c")],
      requires=_FIRST_RUN_REQUIRES, actions=["button"],
      description="With the record seeded as a first run behind the checks and no account, the "
                  "test makes the page's own calls in the page's order: POST /wiz/advisories/accept "
                  "for each document at the hash GET /wiz lists (the phrase for the typed one), "
                  "advisories_complete; POST /wiz/advisories/press, the button state waiting and "
                  "the button LEDs breathing teal, the press (the bench actuator's), "
                  "acceptance_done; POST /wiz/account with a temporary name and password, the "
                  "account exists and the response sets the session cookie; POST /wiz/preferences "
                  "with the units as found; POST /wiz/machine with the model as found (basic when "
                  "unknown) and no TEC; POST /wiz/cloud as found (enabled with the phrase and the "
                  "homing as found, or off); POST /wiz/complete, the record complete, the gate "
                  "open, the controller back. The settings the steps write, the real record and "
                  "the account are restored, the system accounts replayed, the temporary home "
                  "removed. Nothing moves and the laser is not involved.")
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

        st, body = fc.post("/wiz/preferences", data={"ui_units": settings.get("ui_units") or "metric"})
        ctx.check(st == 200, "POST /wiz/preferences -> %s %s", st, body if isinstance(body, dict) else "")
        machine = (w.get("machine") or {})
        model = machine.get("model") if machine.get("model") in ("basic", "plus", "pro") else "basic"
        st, body = fc.post("/wiz/machine", data={"model": model, "tec": "0"})
        ctx.check(st == 200, "POST /wiz/machine -> %s %s", st, body if isinstance(body, dict) else "")
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
        for wid in REQUIRED_WIZARDS:
            ctx.check(done.get(wid) == 1, "the record does not carry %s at version 1: %s", wid, done)
        ctx.log("preferences, machine (%s), cloud (%s) written", model, data.get("enabled"))

        st, body = fc.post("/wiz/complete")
        ctx.check(st == 200, "POST /wiz/complete -> %s %s", st, body if isinstance(body, dict) else "")
        finished_first_run(ctx)
        ctx.log("the run completed: gate open, the controller back")


@test("commission.first-run-page", title="The first-run page, walked once", subsystem="commission",
      kind="operator", hardware="takeover", est_min=6,
      covers=[("forgectrl", "src/ui/wizard.*"), ("forgectrl", "src/ui/forms.js"), ("forgectrl", "src/ui/md.js"),
              ("forgectrl", "src/ui/login.*"), ("forgectrl", "src/ui/embed_docs.cmake"), ("forgectrl", "src/tls.*")],
      requires=_FIRST_RUN_REQUIRES + ["commission.first-run-flow"],
      actions=["button"], hands=["workstation"],
      steps=["From the workstation open https://<address>/ and accept the self-signed certificate.",
             "Click through the setup: scroll each document to its end and accept it (type I "
             "UNDERSTAND for the safety document); press the machine's button when it breathes "
             "teal; create a temporary account (any name and password: it is removed at the end); "
             "take the preferences, the machine, and the cloud step as offered; click Finish. The "
             "checks and the sheet are behind you already.",
             "Confirm that the control panel opened."],
      description="The page against the real daemon over HTTPS, the one place a browser is the "
                  "client: the record seeded as a first run behind the checks and no account, "
                  "'/' serves the wizard, and the test follows the record while the operator "
                  "clicks through (the documents accepted, the press step with the LEDs "
                  "breathing teal, the account created, the run complete, the gate open, the "
                  "controller back). The operator confirms the panel opened. Everything the run "
                  "wrote is restored as commission.first-run-flow restores it.")
def first_run_page(ctx):
    fc = ctx.forgectrl
    ev = ctx.evidence
    ip = lan_ip()
    ctx.check(ip, "cannot determine the board's LAN address")
    with first_run(ctx) as state:
        st, page, hdrs = request("https://%s" % ip, "GET", "/")
        ev["root_page"] = st
        ctx.check(st == 200 and b'id="exitlink"' in page, "'/' over HTTPS does not serve the wizard (%s)", st)
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
        ctx.notice("Continue on the page: the account, the preferences, the machine, the cloud step, Finish.")
        took = ctx.wait_for(lambda: (wiz(fc).get("users") or {}).get("exists") is True, 600)
        ctx.check(took is not None, "no account was created within 10 min")
        state["temp_account"] = (wiz(fc).get("users") or {}).get("name")
        ev["temp_account"] = state["temp_account"]
        ctx.log("account %r created (%.0f s)", state["temp_account"], took)
        took = ctx.wait_for(lambda: wiz(fc).get("completed") is True, 900)
        ctx.check(took is not None, "the setup did not complete within 15 min")
        finished_first_run(ctx)
        ctx.clear_notice()
        ctx.confirm("Did the control panel open after Finish?")


@test("commission.cert-page", title="The certificate is checkable before the warning",
      subsystem="commission", kind="auto", est_min=1,
      covers=[("forgectrl", "src/tls.*"), ("forgectrl", "src/main.c"),
              ("forgectrl", "src/ui/wizard.html"), ("forgectrl", "src/ui/wizard.js")],
      requires=["forgectrl.auth"],
      description="From the board's LAN address, GET /cert over plain HTTP answers 200 with the "
                  "certificate's SHA-256 fingerprint (the one /settings reports), the names on "
                  "it, and no redirect and no login; /cert.pem serves the certificate in PEM form; "
                  "both answer over HTTPS too.")
def cert_page(ctx):
    fc = ctx.forgectrl
    ev = ctx.evidence
    ip = lan_ip()
    ctx.check(ip, "cannot determine the board's LAN address")
    fp = fc.settings().get("tls_fingerprint", "")
    ctx.check(len(fp) == 95, "no fingerprint in /settings (%r): no HTTPS listener?", fp)
    ev["fingerprint"] = fp
    for base in ("http://%s" % ip, "https://%s" % ip):
        st, body, hdrs = request(base, "GET", "/cert")
        text = body.decode("utf-8", "replace")
        ctx.log("GET %s/cert -> %s", base, st)
        ctx.check(st == 200, "GET %s/cert -> %s, expected 200", base, st)
        ctx.check("location" not in hdrs, "GET %s/cert redirected", base)
        ctx.check(fp in text, "the fingerprint is not on the page from %s", base)
        ctx.check("forgefirm.local" in text, "forgefirm.local is not among the names on the page")
        st, body, hdrs = request(base, "GET", "/cert.pem")
        ctx.check(st == 200 and body.startswith(b"-----BEGIN CERTIFICATE-----"),
                  "GET %s/cert.pem -> %s, not a PEM certificate", base, st)
    ev["pem_bytes"] = len(body)


# ------------------------------------------------------- the lifecycle

@contextlib.contextmanager
def preserved(ctx, *paths):
    """The files put back as found under a forgectrl restart when the
    block ends, on every exit path. For a test whose routes change the
    record through the daemon (which keeps it in memory and rewrites
    it whole), so the restore must happen under a restart."""
    saved = {p: read_file(p) for p in paths}
    try:
        yield saved
    finally:
        with ctx.takeover():
            for p, data in saved.items():
                write_file(p, data)
        ctx.log("restored under a restart: %s", ", ".join(os.path.basename(p) for p in saved))


def flag_of(w, level, wid):
    """The reason a GET /wiz body gives for wid at level ("required" or
    "recommended"), or None when the flag is not raised."""
    for x in w.get(level) or []:
        if isinstance(x, dict) and x.get("id") == wid:
            return x.get("reason") or ""
    return None


@test("commission.what-changed", title="A replaced part asks for its checks again",
      subsystem="commission", kind="auto", est_min=2, hardware="takeover",
      covers=[("forgectrl", "src/commission.*"), ("forgectrl", "src/wiz.*"), ("forgectrl", "src/main.c"),
              ("forgectrl", "src/ui/panel.js"), ("forgectrl", "src/ui/index.html"),
              ("forgectrl", "src/ui/help.js")],
      requires=["forgectrl.auth"],
      description="GET /wiz carries the what-changed menu (every change with the wizards it maps "
                  "to, all of them in the catalog). POST /wiz/changed what=tray flags the focus "
                  "card as recommended with the tray as the reason and leaves the gate as it was; "
                  "what=fan flags the airflow check as required, and the gate closes unless the "
                  "override stands; an unknown change is refused (400). The record is put back "
                  "as found under a forgectrl restart.")
def what_changed(ctx):
    fc = ctx.forgectrl
    ev = ctx.evidence
    before = wiz(fc)
    ev["before"] = wiz_summary(before)
    menu = before.get("changes") or []
    catalog_ids = {z["id"] for z in before.get("wizards") or []}
    ev["menu"] = [c.get("id") for c in menu]
    ctx.check(len(menu) >= 7, "the menu lists %d changes, expected at least 7", len(menu))
    unknown = [x["id"] for c in menu for x in c.get("wizards") or [] if x.get("id") not in catalog_ids]
    ctx.check(not unknown, "the menu names wizards outside the catalog: %s", unknown)
    ctx.check(all(c.get("title") for c in menu), "a change has no title")
    gated_before = bool(mode(fc).get("gated"))
    override = bool(before.get("override"))
    ev["gated_before"] = gated_before
    ev["override"] = override

    with preserved(ctx, record_path()):
        st, body = fc.post("/wiz/changed", data={"what": "nothing-like-this"})
        ctx.log("POST /wiz/changed what=nothing-like-this -> %s", st)
        ctx.check(st == 400, "an unknown change -> %s, expected 400", st)

        st, body = fc.post("/wiz/changed", data={"what": "tray"})
        ctx.log("POST /wiz/changed what=tray -> %s", st)
        ctx.check(st == 200 and isinstance(body, dict), "what=tray -> %s", st)
        w = body if isinstance(body, dict) else wiz(fc)
        reason = flag_of(w, "recommended", "laser.focus")
        ev["tray_reason"] = reason
        ctx.check(reason is not None and "tray" in reason,
                  "the tray does not recommend the focus card: %s", w.get("recommended"))
        ctx.check(flag_of(w, "required", "laser.focus") is None, "the tray made the focus card required")
        ctx.check(bool(mode(fc).get("gated")) == gated_before, "a recommendation moved the gate")

        st, body = fc.post("/wiz/changed", data={"what": "fan"})
        ctx.log("POST /wiz/changed what=fan -> %s", st)
        ctx.check(st == 200, "what=fan -> %s", st)
        w = wiz(fc)
        reason = flag_of(w, "required", "airflow")
        ev["fan_reason"] = reason
        ctx.check(reason is not None and "fan" in reason,
                  "a fan does not require the airflow check: %s", w.get("required"))
        m = mode(fc)
        ev["gated_after_fan"] = m.get("gated")
        ctx.check("airflow" in (w.get("why") or ""), "the gate's reason does not name airflow: %r", w.get("why"))
        if override:
            ctx.check(m.get("gated") is False, "the override stands but the gate closed: %s", m)
        else:
            ctx.check(m.get("gated") is True, "a required check left the gate open: %s", m)
        ctx.log("tray -> focus recommended; fan -> airflow required; gate %s (override %s)",
                "closed" if m.get("gated") else "open", override)

    after = wiz(fc)
    ev["after"] = wiz_summary(after)
    ctx.check(flag_of(after, "required", "airflow") is None and flag_of(after, "recommended", "laser.focus") is None,
              "the flags survive the restore: %s / %s", after.get("required"), after.get("recommended"))
    ctx.check(bool(mode(fc).get("gated")) == gated_before, "the gate does not read as before")


@test("commission.record-export", title="The record: JSON, the printable page, and the log bundle",
      subsystem="commission", kind="auto", est_min=2,
      covers=[("forgectrl", "src/commission.*"), ("forgectrl", "src/recordhtml.*"), ("forgectrl", "src/wiz.*"),
              ("forgectrl", "src/logs.*"), ("forgectrl", "src/main.c"), ("forgectrl", "src/ui/index.html"),
              ("forgectrl", "src/ui/panel.js"), ("forgectrl", "src/ui/wizard.html")],
      requires=["forgectrl.auth", "logs.tree-tail-export"],
      description="GET /wiz/record with the token is the record (schema 1, the sheet id, the "
                  "wizards) with no password hash in it; with download=1 it comes as an attachment "
                  "named after the sheet id; GET /wiz/record.html is a page with no script that "
                  "carries the sheet id and every completed wizard; without the token and without "
                  "a login both are refused (403); and the sanitized log export carries "
                  "system/commissioning.json, parseable, with the same sheet id and no panel token.")
def record_export(ctx):
    import gzip
    import io
    import tarfile
    fc = ctx.forgectrl
    ev = ctx.evidence
    base = tls_base()
    tok = {"X-ForgeFIRM-Token": fc.token}
    w = wiz(fc)
    sid = w.get("sheet_id") or ""
    ev["sheet_id"] = sid
    ctx.check(sid, "GET /wiz carries no sheet id")

    st, body, hdrs = request(base, "GET", "/wiz/record", headers=tok)
    rec = decode(body)
    ctx.log("GET /wiz/record -> %s (%d bytes)", st, len(body or b""))
    ctx.check(st == 200 and isinstance(rec, dict), "GET /wiz/record -> %s", st)
    ctx.check(isinstance(rec, dict) and rec.get("schema") == 1 and rec.get("sheet_id") == sid,
              "the record reads %s", rec if not isinstance(rec, dict)
              else {k: rec.get(k) for k in ("schema", "sheet_id")})
    done = sorted((rec.get("wizards") or {}).keys()) if isinstance(rec, dict) else []
    ev["wizards_in_record"] = done
    ctx.check(b"$6$" not in (body or b""), "the record carries a password hash")
    ctx.check("attachment" not in (hdrs.get("content-disposition") or ""), "a plain read came as an attachment")

    st, body, hdrs = request(base, "GET", "/wiz/record?download=1", headers=tok)
    cd = hdrs.get("content-disposition") or ""
    ev["download_disposition"] = cd
    ctx.check(st == 200 and "attachment" in cd and sid in cd,
              "the download is not an attachment named after the sheet id: %s %r", st, cd)
    ctx.check(decode(body) == rec, "the download differs from the record")

    st, body, hdrs = request(base, "GET", "/wiz/record.html", headers=tok)
    page = (body or b"").decode("utf-8", "replace")
    ev["page_bytes"] = len(body or b"")
    ctx.log("GET /wiz/record.html -> %s (%d bytes)", st, len(body or b""))
    ctx.check(st == 200 and "text/html" in (hdrs.get("content-type") or ""), "GET /wiz/record.html -> %s %s",
              st, hdrs.get("content-type"))
    ctx.check("<script" not in page.lower(), "the printable page carries a script")
    ctx.check(sid in page and "commissioning record" in page, "the page lacks the sheet id or its title")
    titles = {z["id"]: z["title"] for z in w.get("wizards") or []}
    missing = [d for d in done if titles.get(d) and ("<h3>" + titles[d]) not in page]
    ev["missing_on_page"] = missing
    ctx.check(not missing, "completed wizards missing from the page: %s", missing)

    for path in ("/wiz/record", "/wiz/record.html"):
        st, body, hdrs = request(base, "GET", path)
        ctx.check(st == 403, "GET %s with no token and no login -> %s, expected 403", path, st)

    ctx.check(fc.wait_idle(timeout=30, abort=ctx.aborted), "machine not idle: the export is refused")
    slow = hw.Forgectrl(token=fc.token, timeout=300.0)
    t0 = time.time()
    st, data = slow.post("/logs/export", raw=True)
    ev["export_s"] = round(time.time() - t0, 1)
    ctx.log("POST /logs/export -> %s (%d bytes, %.1f s)", st, len(data) if data else 0, ev["export_s"])
    ctx.check(st == 200, "POST /logs/export -> %s", st)
    member = None
    try:
        tf = tarfile.open(fileobj=io.BytesIO(gzip.decompress(data)))
        for m in tf.getmembers():
            if m.isfile() and m.name.endswith("system/commissioning.json"):
                member = tf.extractfile(m).read()
    except (OSError, tarfile.TarError, EOFError) as e:
        ctx.fail("export is not a readable tar.gz: %s", e)
    ctx.check(member is not None, "the bundle lacks system/commissioning.json")
    if member is not None:
        try:
            inside = json.loads(member.decode("utf-8"))
        except ValueError as e:
            inside = None
            ctx.check(False, "the bundled record is not JSON: %s", e)
        ev["bundle_record_bytes"] = len(member)
        ctx.check(isinstance(inside, dict) and inside.get("sheet_id") == sid,
                  "the bundled record is not this machine's: %s",
                  inside.get("sheet_id") if isinstance(inside, dict) else inside)
        ctx.check(isinstance(inside, dict) and sorted((inside.get("wizards") or {}).keys()) == done,
                  "the bundled record's wizards differ from the record's")
        ctx.check(fc.token.encode() not in member, "the bundled record carries the panel token")
        ctx.check(b"$6$" not in member, "the bundled record carries a password hash")
    ctx.log("record %d wizards; page %d bytes; bundle carries the record", len(done), ev["page_bytes"])


@test("commission.mirror", title="One browser drives a check; a second one follows",
      subsystem="commission", kind="auto", est_min=2, hardware="takeover",
      covers=[("forgectrl", "src/wizdark.*"), ("forgectrl", "src/wizcalc.*"), ("forgectrl", "src/wiz.*"),
              ("forgectrl", "src/session.*"), ("forgectrl", "src/ui/wizard.js"),
              ("forgectrl", "src/ui/wizard.html"), ("forgectrl", "src/ui/wizard.css")],
      requires=["forgectrl.auth", "commission.account-login", "commission.check-sensors"],
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
