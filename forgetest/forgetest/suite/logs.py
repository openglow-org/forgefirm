"""logs.* - unified logging: the log tree, tail, and the sanitized export;
the routing path (emitters, relays, rendered rules); the level settings."""
import gzip
import io
import os
import re
import tarfile
import time

from ..catalog import test
from .. import hw


def _read(path, default=None):
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as f:
            return f.read()
    except OSError:
        return default


_LOG_COVERS = [("forgectrl", "src/logs.*"), ("forgectrl", "src/fflog.*"), ("forgectrl", "src/sanitize.*"),
               ("forgectrl", "src/main.c")]


@test("logs.tree-tail-export", title="Log tree, tail, and sanitized export", subsystem="logs",
      kind="auto", est_min=1,
      covers=_LOG_COVERS, requires=["forgectrl.auth"],
      description="/logs lists the loggers with their levels and files, /logs/tail returns the "
                  "forgectrl logger's tail, and POST /logs/export streams a sanitized tar.gz "
                  "bundle that contains neither the panel token nor the camera key.")
def tree_tail_export(ctx):
    fc = ctx.forgectrl
    ev = ctx.evidence
    st, body = fc.get("/logs")
    ctx.log("GET /logs -> %s", st)
    ctx.check(st == 200 and isinstance(body, dict), "GET /logs -> %s", st)
    loggers = body.get("loggers") or []
    names = [l.get("name") for l in loggers] if isinstance(loggers, list) else list(loggers)
    ev["loggers"] = names
    ctx.log("loggers: %s", names)
    ctx.check("forgectrl" in names, "/logs lacks the forgectrl logger: %s", names)

    st, tail = fc.get("/logs/tail", params={"name": "forgectrl", "lines": "20"})
    ctx.log("GET /logs/tail?name=forgectrl -> %s", st)
    ctx.check(st == 200, "GET /logs/tail -> %s %s", st, tail if isinstance(tail, dict) else "")
    st, tail = fc.get("/logs/tail", params={"name": "no-such-logger"})
    ctx.check(st == 404, "unknown logger -> %s, expected 404", st)

    # the sanitizer walks every log file; a full tree takes tens of
    # seconds on the target, so this call gets its own client timeout
    slow = hw.Forgectrl(token=fc.token, timeout=300.0)
    t0 = time.time()
    st, data = slow.post("/logs/export", raw=True)
    ev["export_s"] = round(time.time() - t0, 1)
    ctx.log("POST /logs/export -> %s (%d bytes, %.1f s)", st, len(data) if data else 0, ev["export_s"])
    ctx.check(st == 200, "POST /logs/export -> %s", st)
    ev["export_bytes"] = len(data)
    try:
        raw = gzip.decompress(data)
        tf = tarfile.open(fileobj=io.BytesIO(raw))
        members = tf.getnames()
    except (OSError, tarfile.TarError, EOFError) as e:
        ctx.fail("export is not a readable tar.gz: %s", e)
    ev["members"] = len(members)
    ctx.log("bundle: %d members, e.g. %s", len(members), members[:5])
    ctx.check(members, "empty bundle")
    ctx.check(any(m.endswith("README.txt") for m in members), "sanitized bundle lacks README.txt")
    # the two secrets the sanitizer knows by value and a bundle could
    # carry: the panel token and the camera key (a URL parameter that
    # lands in sender logs)
    secrets = {}
    if fc.token:
        secrets["panel token"] = fc.token
    st, body = fc.get("/system/camera-key")
    if st == 200 and isinstance(body, dict) and body.get("key"):
        secrets["camera key"] = body["key"]
    ctx.check("camera key" in secrets, "GET /system/camera-key -> %s: no key to check for", st)
    contents = [(m.name, tf.extractfile(m).read()) for m in tf.getmembers() if m.isfile()]
    for what, value in secrets.items():
        leaked = [name for name, content in contents if value.encode() in content]
        ev[what.replace(" ", "_") + "_leaks"] = leaked
        ctx.check(not leaked, "the sanitized bundle contains the %s: %s", what, leaked)


# The routing test proves the whole path every logger takes: emitter (or
# relay) -> /dev/log -> rsyslog rules rendered from the settings -> the
# logger's own file in the ff_line format. It stands for the emitters in
# every component (fflog in forgectrl and grblHAL, the SysLogHandler in the
# Python apps), the supervisor's per-controller relay, the daemon's fifo
# relay in its init script, and the render step.
_ROUTING_COVERS = [("forgectrl", "src/logs.*"), ("forgectrl", "src/fflog.*"), ("forgectrl", "src/super.c"),
                   ("forgectrl", "init/**"),
                   ("grblhal-glowforge", "src/fflog.*"), ("grblhal-glowforge", "src/main.c"),
                   ("forgefirm-app", "forgefirm-app/ffmachine.py"), ("forgefirm-app", "forgefirm-app/gfcloud.py"),
                   ("forgefirm-app", "forgefirm-app/gfhome.py")]

LOGS_ROOT = "/data/log/forgefirm"
LOGGERS = ("forgectrl", "grblhal", "gfcloud", "gfhome", "kernel", "system")
_LINE_RE = re.compile(r"^\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d(\.\d+)?[+-]\d\d:\d\d (?P<prog>[A-Za-z0-9_.-]+)\[(?P<pid>[-\d]+)\] "
                      r"(?P<sev>EMERG|ALERT|CRIT|ERR|WARNING|NOTICE|INFO|DEBUG) (?P<msg>.*)$")
_SEV_RANK = {"off": -1, "error": 3, "warning": 4, "notice": 5, "info": 6, "debug": 7}


def _tail_lines(path, n=400):
    try:
        with open(path, "rb") as f:
            f.seek(0, 2)
            size = f.tell()
            f.seek(max(0, size - 256 * 1024))
            data = f.read()
    except OSError:
        return []
    return data.decode("utf-8", "replace").splitlines()[-n:]


def _wait_line(path, needle, timeout=6.0):
    """Poll a log file for a line containing needle; the matched line or None."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        for line in _tail_lines(path, 200):
            if needle in line:
                return line
        time.sleep(0.25)
    return None


@test("logs.routing", title="Log routing, relays, and the rendered rules", subsystem="logs",
      kind="auto", est_min=1,
      covers=_ROUTING_COVERS, requires=["logs.tree-tail-export"],
      description="rsyslog is the only logger (no busybox syslogd/klogd), the rules rendered from "
                  "the settings and the effective-levels record exist and agree with /logs, every "
                  "logger has its directory, the daemon's own emitter and the per-controller "
                  "`logger` relay both land in the right file in the ff_line format, a stray "
                  "program's line goes to system/ and nowhere else, kernel lines reach kernel/, "
                  "and nothing of ours is written outside the tree.")
def routing(ctx):
    fc = ctx.forgectrl
    ev = ctx.evidence

    # 1. one logger daemon
    rs = hw.pidof("rsyslogd")
    ev["rsyslogd_pids"] = rs
    ctx.check(rs, "rsyslogd is not running")
    for legacy in ("syslogd", "klogd"):
        pids = hw.pidof(legacy)
        ctx.check(not pids, "busybox %s is running (%s) next to rsyslog", legacy, pids)
    ctx.log("rsyslogd %s; no syslogd/klogd", rs)

    # 2. the render step: rules + effective levels, consistent with what
    #    the API reports as effective
    ctx.check(os.path.isfile("/data/forgefirm/rsyslog-forgefirm.conf"),
              "rendered rules missing: /data/forgefirm/rsyslog-forgefirm.conf")
    rules = _read("/data/forgefirm/rsyslog-forgefirm.conf") or ""
    for name in LOGGERS:
        ctx.check(("# %s:" % name) in rules, "rendered rules carry no block for %s", name)
    ctx.check(os.path.isfile("/var/run/forgefirm-loglevels"), "effective-levels record missing")
    st, body = fc.get("/logs")
    ctx.check(st == 200 and isinstance(body, dict), "GET /logs -> %s", st)
    ctx.check(body.get("effective_known") is True, "/logs does not know the effective levels")
    eff = {}
    for l in body.get("loggers") or []:
        eff[l["name"]] = (l.get("effective_disk"), l.get("effective_remote"))
    ev["effective"] = eff
    ctx.log("effective levels: %s", eff)
    ctx.check(set(eff) == set(LOGGERS), "loggers reported: %s", sorted(eff))
    levels = _read("/var/run/forgefirm-loglevels") or ""
    for name, (disk, _remote) in eff.items():
        ctx.check(("log_%s_disk=%s" % (name, disk)) in levels,
                  "effective record and /logs disagree on %s (%s)", name, disk)

    # 3. the tree
    for name in LOGGERS:
        ctx.check(os.path.isdir(os.path.join(LOGS_ROOT, name)), "no directory for %s", name)

    # 4. the daemon's own emitter (fflog): a line from the running pid, if
    #    its level admits info
    pids = hw.pidof("forgectrl")
    ctx.check(pids, "forgectrl is not running")
    disk_rank = _SEV_RANK.get(eff.get("forgectrl", ("info", "off"))[0], 6)
    fpath = os.path.join(LOGS_ROOT, "forgectrl", "forgectrl.log")
    if disk_rank >= 6:
        own = [l for l in _tail_lines(fpath, 2000)
               if any((" forgectrl[%d] " % p) in l for p in pids)]
        ev["own_lines"] = len(own)
        ctx.log("forgectrl[%s] lines in its file: %d", pids, len(own))
        ctx.check(own, "no line from the running daemon in %s (emitter path broken?)", fpath)
        m = _LINE_RE.match(own[-1])
        ctx.check(m and m.group("prog") == "forgectrl", "not in the ff_line format: %r", own[-1])
    else:
        ctx.log("forgectrl disk level %s: own-emitter check skipped", eff["forgectrl"][0])

    # 5. the relay path: a `logger` probe under a controller's name lands
    #    in that controller's file with the ff_line format
    nonce = "forgetest-routing-%d" % int(time.time())
    for name in ("grblhal", "gfhome"):
        rank = _SEV_RANK.get(eff.get(name, ("info", "off"))[0], 6)
        if rank < 3:
            ctx.log("%s disk level off: relay probe skipped", name)
            continue
        rc, out = hw.run(["logger", "-t", name, "-p", "daemon.err", "%s %s" % (nonce, name)])
        ctx.check(rc == 0, "logger -t %s failed: %s", name, out)
        path = os.path.join(LOGS_ROOT, name, name + ".log")
        line = _wait_line(path, nonce)
        ev["probe_" + name] = line
        ctx.check(line, "%s probe did not reach %s", name, path)
        m = _LINE_RE.match(line)
        ctx.check(m and m.group("prog") == name and m.group("sev") == "ERR",
                  "%s probe line not in the ff_line format: %r", name, line)
        ctx.log("%s: %s", name, line)
    # a probe under another name must NOT leak into these files
    rc, out = hw.run(["logger", "-t", "forgetest-stray", "-p", "daemon.err", "%s stray" % nonce])
    time.sleep(1.5)
    for name in ("grblhal", "gfhome", "forgectrl"):
        path = os.path.join(LOGS_ROOT, name, name + ".log")
        ctx.check(not any((nonce + " stray") in l for l in _tail_lines(path, 100)),
                  "a stray program's line landed in %s", path)
    sys_rank = _SEV_RANK.get(eff.get("system", ("info", "off"))[0], 6)
    if sys_rank >= 3:
        line = _wait_line(os.path.join(LOGS_ROOT, "system", "system.log"), nonce + " stray")
        ctx.check(line, "the stray program's line did not reach system/")

    # 6. relay processes: the supervisor spawns one `logger` per controller
    #    (its stdin is the controller's output pipe); the daemon's init
    #    script keeps one on the fifo
    st, mode = fc.get("/mode")
    relays = hw.pidof("logger")
    ev["logger_relays"] = relays
    ev["controller"] = mode.get("controller") if isinstance(mode, dict) else None
    ctx.log("logger relays: %s (controller %s)", relays, ev["controller"])
    if isinstance(mode, dict) and mode.get("controller") == "running":
        ctx.check(relays, "controller running but no `logger` relay process")
    if os.path.exists("/var/run/forgectrl.stderr"):
        ctx.check(len(relays) >= 2 or ev["controller"] != "running",
                  "fifo relay present but only %s logger process(es)", relays)

    # 7. the kernel reaches kernel/ (unless filtered off)
    if _SEV_RANK.get(eff.get("kernel", ("info", "off"))[0], 6) >= 6:
        klines = _tail_lines(os.path.join(LOGS_ROOT, "kernel", "kernel.log"), 50)
        ev["kernel_lines"] = len(klines)
        ctx.check(klines and any(" kernel[" in l for l in klines), "no kernel lines in kernel/")

    # 8. nothing outside the tree
    for stray in ("/data/forgectrl.log", "/data/gfcloud.log", "/data/gfhome.log",
                  "/data/log/gfcloud", "/data/log/gfhome", "/var/log/messages"):
        ctx.check(not os.path.exists(stray), "pre-syslog log path still present: %s", stray)
    ctx.log("no ForgeFIRM log outside %s", LOGS_ROOT)


@test("logs.level-settings", title="Log level and remote-target settings", subsystem="logs",
      kind="auto", est_min=1,
      covers=[("forgectrl", "src/logs.*"), ("forgectrl", "src/main.c"), ("forgectrl", "src/settings.*")],
      requires=["logs.tree-tail-export", "forgectrl.settings-bounds"],
      description="A log level outside off..debug, a bad remote port, a bad protocol and a bad "
                  "server name are refused (400); a valid remote level is accepted and /logs shows "
                  "it as configured but not effective (pending_reboot); the setting is restored to "
                  "its prior value.")
def level_settings(ctx):
    fc = ctx.forgectrl
    ev = ctx.evidence
    ctx.check(fc.wait_idle(timeout=30, abort=ctx.aborted), "machine not idle: settings are locked")

    for key, val in (("log_grblhal_disk", "verbose"), ("log_kernel_remote", "7"),
                     ("syslog_port", "70000"), ("syslog_proto", "tls"),
                     ("syslog_server", "bad host name")):
        st, body = fc.post("/settings", data={key: val})
        ev["refused %s=%s" % (key, val)] = st
        ctx.log("POST /settings %s=%s -> %s", key, val, st)
        ctx.check(st == 400, "%s=%s -> %s, expected 400", key, val, st)

    st, before = fc.get("/logs")
    ctx.check(st == 200 and isinstance(before, dict), "GET /logs -> %s", st)
    prior = {l["name"]: l for l in before.get("loggers") or []}
    ctx.check("system" in prior, "/logs lacks the system logger")
    was = prior["system"].get("remote") or "off"
    was_pending = bool(before.get("pending_reboot"))
    new = "warning" if was != "warning" else "error"
    ev["system_remote_before"] = was
    ev["pending_before"] = was_pending
    try:
        st, body = fc.post("/settings", data={"log_system_remote": new})
        ctx.log("POST /settings log_system_remote=%s -> %s", new, st)
        ctx.check(st == 200, "valid level refused: %s", st)
        st, after = fc.get("/logs")
        now = {l["name"]: l for l in after.get("loggers") or []}
        ctx.check(now["system"].get("remote") == new, "configured level reads %r", now["system"].get("remote"))
        ctx.check(now["system"].get("effective_remote") == prior["system"].get("effective_remote"),
                  "effective level changed without a reboot")
        ctx.check(after.get("pending_reboot") is True, "pending_reboot not raised by a level change")
        ctx.log("configured %s, effective %s, pending_reboot %s",
                now["system"].get("remote"), now["system"].get("effective_remote"), after.get("pending_reboot"))
    finally:
        # restore: a value that was stored goes back as a value; a default
        # is restored by clearing the key (a query parameter - empty
        # values never reach the form body)
        stored = fc.settings().get("log_system_remote", "")
        if was == "off" and stored in ("", None, new):
            st, body = fc.post("/settings", params={"log_system_remote": ""})
        else:
            st, body = fc.post("/settings", data={"log_system_remote": was})
        ctx.log("restore log_system_remote -> %s", st)
    st, final = fc.get("/logs")
    fin = {l["name"]: l for l in final.get("loggers") or []}
    ctx.check(fin["system"].get("remote") == was, "system remote level not restored (%r)", fin["system"].get("remote"))
    ctx.check(bool(final.get("pending_reboot")) == was_pending,
              "pending_reboot did not return to %s after the restore", was_pending)
