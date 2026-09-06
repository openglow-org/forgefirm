#!/usr/bin/env python3
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
               when set (the bench page passes <data>/bench/), else next
               to the tool.
forgectrl_*    the machine-services HTTP API (:80) with the panel token
               from GF_TOKEN or, on the board, /data/forgefirm/panel.token.
"""
import json
import math
import os
import shlex
import subprocess
import urllib.error
import urllib.parse
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
_LOCAL_HOSTS = ("127.0.0.1", "localhost", "::1")


def _resolve():
    host = os.environ.get("GF_HOST")
    if host:
        return host, host in _LOCAL_HOSTS
    if os.path.isdir("/sys/glowforge"):
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
    d = os.environ.get("FORGETEST_BENCH_DATA")
    if d:
        os.makedirs(d, exist_ok=True)
        return d
    return HERE


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


SETTINGS_FILE = "/data/forgefirm.conf"


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
