# Copyright 2026 514 LLC d/b/a OpenGlow
# Written by Scott Wiederhold
# https://community.openglow.org
# SPDX-License-Identifier:    MIT

"""logs.* - unified logging: the routing path (emitters, relays, rendered
rules); the level settings."""
import os
import re
import time

from ..catalog import test
from .. import hw


def _read(path, default=None):
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as f:
            return f.read()
    except OSError:
        return default


LOGS_ROOT = "/data/log/forgefirm"


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

LOGGERS =("forgectrl", "grblhal", "gfcloud", "gfhome", "forgeext", "kernel", "system")
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
      covers=_ROUTING_COVERS,
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
    for name in ("grblhal", "gfhome", "forgectrl", "forgeext"):
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
