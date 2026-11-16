#!/usr/bin/env python3
# Copyright 2026 514 LLC d/b/a OpenGlow
# Written by Scott Wiederhold
# https://community.openglow.org
# SPDX-License-Identifier:    MIT
"""Shared helpers for the bench tools that can run either ON the board or
from a LAN host.

    from gfbench import HOST, LOCAL, board, degc, data_path

HOST / LOCAL   the machine address and whether this process runs on it.
               GF_HOST names a remote machine (host mode: sysfs through
               ssh, Grbl and forgectrl over the LAN). Unset, the tool
               runs on the board itself when /sys/glowforge exists (local
               mode: sysfs directly, Grbl and forgectrl on 127.0.0.1) -
               the acceptance tool's bench page runs them that way.
board(cmd)     run a shell command on the machine and return its stdout
               (local: sh -c; host: ssh, or the client named by GF_SSH,
               e.g. GF_SSH='wsl -d <distro> -- ssh').
degc(raw)      the factory B-equation coolant conversion
               (docs.forgefirm.org, the sensors page).
data_path(f)   where a tool keeps its data files: FORGETEST_BENCH_DATA
               when set (the bench page passes <data>/bench/), else
               /data/forgetest/bench on the board and next to the tool on
               a LAN host.
forgectrl_*    the machine-services HTTP API (:80) with the panel token
               from GF_TOKEN or, on the board, /data/forgefirm/panel.token.
               forgectrl answers a write only from loopback, so a tool
               that writes runs on the board.
arming_processes()  forgectrl, a GRBL controller, the cloud client and
               any holder of the pulse device while they run: the
               processes that can arm a job. A tool that stops the pump or
               drives the thermal hardware itself runs only when there are
               none (the bench page's takeover).
exit_on_signal(), put_back(...)
               SIGTERM (the bench page's Abort) and SIGHUP end a tool
               through its finally blocks; each restore step there runs in
               its own try, and a second signal waits for all of them.
"""
import json
import math
import os
import shlex
import signal
import subprocess
import urllib.error
import urllib.parse
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
_LOCAL_HOSTS = ("127.0.0.1", "localhost", "::1")
BOARD_SYSFS = "/sys/glowforge"
BOARD_DATA = "/data/forgetest/bench"     # the bench page's <data>/bench/
PROC = "/proc"


def _resolve():
    host = os.environ.get("GF_HOST")
    if host:
        return host, host in _LOCAL_HOSTS
    if os.path.isdir(BOARD_SYSFS):
        return "127.0.0.1", True
    raise SystemExit("set GF_HOST to the machine IP address (or run this on the board)")


HOST, LOCAL = _resolve()
SSH = shlex.split(os.environ.get("GF_SSH", "ssh"))


def board(cmd, timeout=30):
    """stdout of a shell command run on the machine (not stripped)."""
    if LOCAL:
        argv = ["sh", "-c", cmd]
    else:
        argv = SSH + ["-o", "PreferredAuthentications=none", "root@" + HOST, cmd]
    r = subprocess.run(argv, capture_output=True, text=True, timeout=timeout)
    return r.stdout


# Factory B-equation conversion: 10k B3380 NTC in a 10k divider behind a
# 1.3x gain stage, 10-bit ADC.
F = 1024.0 * 1.3
RD, BETA = 10000.0, 3380.0
RINF = 10000.0 * math.exp(-3380.0 / 298.15)


def degc(raw):
    try:
        raw = float(raw)
    except (TypeError, ValueError):
        return float("nan")
    if raw <= 1.0 or raw >= F:
        return float("nan")
    r = RD / (F / raw - 1.0)
    return BETA / math.log(r / RINF) - 273.15


def data_dir():
    """FORGETEST_BENCH_DATA, else the page's directory on the board (the
    tools' own directory there is the read-only rootfs), else next to the
    tool."""
    d = os.environ.get("FORGETEST_BENCH_DATA")
    if not d:
        if not os.path.isdir(BOARD_SYSFS):
            return HERE
        d = BOARD_DATA
    os.makedirs(d, exist_ok=True)
    return d


def data_path(name):
    return os.path.join(data_dir(), name)


# ------------------------------------------------------------- forgectrl

def forgectrl_base():
    return os.environ.get("FORGECTRL_URL") or "http://%s:%s" % (HOST, os.environ.get("FORGECTRL_PORT") or "80")


def token():
    tok = os.environ.get("GF_TOKEN")
    if tok:
        return tok
    if LOCAL:
        try:
            with open("/data/forgefirm/panel.token", "r", encoding="utf-8") as f:
                return f.read().strip()
        except OSError:
            pass
    return ""


def forgectrl_request(method, path, params=None, data=None, timeout=8.0):
    """(status, body): body is parsed JSON when the response is JSON, else
    text. Raises OSError-derived errors when the daemon is unreachable."""
    url = forgectrl_base() + path
    if params:
        url += ("&" if "?" in url else "?") + urllib.parse.urlencode(params)
    body = None
    hdrs = {"Host": urllib.parse.urlsplit(forgectrl_base()).netloc}
    if data is not None:
        body = urllib.parse.urlencode(data).encode()
        hdrs["Content-Type"] = "application/x-www-form-urlencoded"
    tok = token()
    if tok:
        hdrs["X-ForgeFIRM-Token"] = tok
    req = urllib.request.Request(url, data=body, method=method, headers=hdrs)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            status, content, ctype = resp.status, resp.read(), resp.headers.get("Content-Type", "")
    except urllib.error.HTTPError as e:
        status, content = e.code, e.read()
        ctype = e.headers.get("Content-Type", "") if e.headers else ""
    text = content.decode("utf-8", "replace")
    if "json" in ctype:
        try:
            return status, json.loads(text)
        except ValueError:
            pass
    return status, text


def forgectrl_get(path, **kw):
    return forgectrl_request("GET", path, **kw)


def forgectrl_post(path, **kw):
    return forgectrl_request("POST", path, **kw)


SETTINGS_FILE = "/data/forgefirm/forgefirm.conf"


def setting(key, default=None):
    """One shared machine setting (forgectrl's /settings; on the board with
    forgectrl stopped - a takeover - the settings file itself), or default
    when unset or unreadable."""
    try:
        st, body = forgectrl_get("/settings")
        if st == 200 and isinstance(body, dict):
            val = body.get(key)
            return default if val in (None, "") else val
    except OSError:
        pass
    if LOCAL:
        try:
            with open(SETTINGS_FILE, "r", encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if not line or line.startswith("#") or "=" not in line:
                        continue
                    k, v = line.split("=", 1)
                    if k.strip() == key:
                        v = v.strip()
                        return default if v == "" else v
        except OSError:
            pass
    return default


# ------------------------------------------------- the machine's owners

# The command names (comm, 15 characters at most) of the processes that
# hold the pulse device or can arm a job; the cloud client is a script,
# found by its command line. The takeover of the bench page waits for the
# same three to be gone.
ARMING_COMMS = ("forgectrl", "grblHAL_glowfor")
ARMING_SCRIPT = b"gfcloud.py"
PULSE_DEVICE = "/dev/glowforge"


def holds_pulse_device(pid):
    """True when the process has the pulse device open, whatever its name
    (a controller under another name included). A descriptor table this
    process may not read (another user's, off the board) reads as not."""
    fds = os.path.join(PROC, pid, "fd")
    try:
        names = os.listdir(fds)
    except OSError:
        return False
    for name in names:
        try:
            if os.readlink(os.path.join(fds, name)) == PULSE_DEVICE:
                return True
        except OSError:
            continue
    return False


def arming_processes():
    """['<pid> <name>', ...] for every process that can arm a job, from
    /proc: forgectrl, a controller, the cloud client, and anything holding
    the pulse device. An unreadable /proc reads as one, so a caller
    refuses."""
    try:
        pids = [p for p in os.listdir(PROC) if p.isdigit()]
    except OSError as e:
        return ["(%s unreadable: %s)" % (PROC, e)]
    found = []
    for pid in pids:
        try:
            with open(os.path.join(PROC, pid, "comm"), "rb") as f:
                comm = f.read().strip().decode("ascii", "replace")
            with open(os.path.join(PROC, pid, "cmdline"), "rb") as f:
                cmdline = f.read()
        except OSError:
            continue                    # gone while listed
        if comm in ARMING_COMMS:
            found.append("%s %s" % (pid, comm))
        elif any(os.path.basename(a) == ARMING_SCRIPT for a in cmdline.split(b"\0")):
            found.append("%s %s" % (pid, ARMING_SCRIPT.decode()))
        elif holds_pulse_device(pid):
            found.append("%s %s (holds %s)" % (pid, comm, PULSE_DEVICE))
    return found


# ------------------------------------------------------------- signals

def stop_on_signal(signum, frame):
    raise SystemExit(128 + signum)


def exit_on_signal():
    """SIGTERM (the bench page's Abort) and SIGHUP (a lost terminal) end
    the tool through SystemExit, so its finally blocks put back what it
    changed; Python's default for both ends the process without them."""
    for sig in (signal.SIGTERM, signal.SIGHUP):
        signal.signal(sig, stop_on_signal)


def put_back(*steps):
    """Run each (label, callable) restore step in its own try, so a step
    that fails never skips the steps after it. A SIGTERM or a SIGHUP that
    arrives meanwhile (a second Abort) is held until every step has run,
    then ends the tool. Returns the labels of the steps that failed."""
    held = []
    saved = {}
    for sig in (signal.SIGTERM, signal.SIGHUP):
        saved[sig] = signal.getsignal(sig)
        signal.signal(sig, lambda signum, frame: held.append(signum))
    failed = []
    try:
        for label, step in steps:
            try:
                step()
            except BaseException as e:  # noqa: B036 - a fail()'s SystemExit included
                failed.append(label)
                print("WARNING: could not %s: %s: %s" % (label, type(e).__name__, e), flush=True)
    finally:
        for sig, handler in saved.items():
            signal.signal(sig, handler if handler is not None else signal.SIG_DFL)
    if held:
        raise SystemExit(128 + held[0])
    return failed
