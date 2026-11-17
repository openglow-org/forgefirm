# Copyright 2026 514 LLC d/b/a OpenGlow
# Written by Scott Wiederhold
# https://community.openglow.org
# SPDX-License-Identifier:    MIT

"""Shared fixtures for the forgetest unit tests: synthetic manifests and
catalog entries that never touch hardware."""
import hashlib
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))
REPO = os.path.dirname(os.path.dirname(HERE))

from forgetest import catalog as catalog_mod  # noqa: E402
from forgetest import manifest as manifest_mod  # noqa: E402

# A provenance verdict for a run on its pins as built (provenance.py).
CLEAN = {"clean": True, "why": [], "ran": {}}


def host_run(case=None):
    """Mark a host run (baseline.host_run): the tests that run the runner or
    the hand-back with no forgectrl and no kernel module say so, and the
    hand-back skips those sides instead of failing the run on them. With a
    test case the mark goes with the test; without one the caller takes it
    down (end_host_run, for a class-wide setup)."""
    os.environ["FORGETEST_HOST_RUN"] = "1"
    if case is not None:
        case.addCleanup(end_host_run)


def end_host_run():
    os.environ.pop("FORGETEST_HOST_RUN", None)


def latch_drives_interlock(case):
    """The fake sysfs tree's latch as the kernel's: a write of
    cnc/laser_latch drives interlock_circuit bit 3 (set on a lock, clear on
    an unlock), as LATCH_RESET does, and a lock parks FIRE, so the FIRE and
    LASER_ON readbacks read 0 after it, for the rest of the test."""
    from unittest import mock
    from forgetest import hw
    real = hw.sysfs_write

    def write(attr, value):
        real(attr, value)
        if attr != "cnc/laser_latch":
            return
        lock = str(value).strip() == "1"
        path = hw.sysfs_root() + "cnc/interlock_circuit"
        if os.path.exists(path):
            with open(path) as f:
                ilk = int(f.read().split()[0], 0)
            with open(path, "w") as f:
                f.write(str((ilk | 0x08) if lock else (ilk & ~0x08)))
        for readback in ("cnc/laser_enable", "cnc/laser_on"):
            if lock and os.path.exists(hw.sysfs_root() + readback):
                real(readback, "0")
    p = mock.patch.object(hw, "sysfs_write", write)
    p.start()
    case.addCleanup(p.stop)


class ScriptedProvenance:
    """provenance.Provenance as the runner calls it: each check answers
    with the next verdict of the script, the last one repeating."""

    def __init__(self, *verdicts):
        self.verdicts = list(verdicts) or [CLEAN]
        self.calls = 0

    def check(self):
        v = self.verdicts[min(self.calls, len(self.verdicts) - 1)]
        self.calls += 1
        return dict(v)


# --------------------------------------------- the layer's classes, on a host

class BitbakeFatal(Exception):
    pass


class _Bb:
    """bitbake's `bb` as the classes' functions use it: a fatal stops."""

    @staticmethod
    def fatal(msg):
        raise BitbakeFatal(msg)

    @staticmethod
    def note(msg):
        pass


class FakeData:
    """A bitbake datastore that answers getVar from its keyword values."""

    def __init__(self, **values):
        self.values = values

    def getVar(self, name, expand=True):
        return self.values.get(name)


def bbclass(name):
    """The top-level `def` functions of meta-forgefirm/classes/<name>.bbclass
    in one namespace, with a stand-in for bitbake's `bb`: the class's own
    code, run on a host. Line numbers are the file's."""
    path = os.path.join(REPO, "meta-forgefirm", "classes", name + ".bbclass")
    with open(path, encoding="utf-8") as f:
        lines = f.read().split("\n")
    ns = {"bb": _Bb}
    block = None
    for i, ln in enumerate(lines + ["end"]):
        if block is not None and ln.strip() and not ln.startswith((" ", "\t")):
            exec(compile("\n" * block[0] + "\n".join(block[1]), path, "exec"), ns)
            block = None
        if ln.startswith("def "):
            block = (i, [ln])
        elif block is not None:
            block[1].append(ln)
    return ns


def blob(text):
    """A git-style blob id for text (what ls-tree would report)."""
    data = text.encode()
    return hashlib.sha1(b"blob %d\0" % len(data) + data).hexdigest()


def make_manifest(components=None, platform=None, version="20260101000000 (dev)", name="forgefirm-image-dev"):
    comps = components if components is not None else {
        "forgectrl": {"srcrev": "aaa", "files": [["src/main.c", blob("main")], ["src/ui.c", blob("ui")],
                                                  ["src/auth.c", blob("auth")], ["src/cool.c", blob("cool")],
                                                  ["README.md", blob("readme")]]},
        "grblhal-glowforge": {"srcrev": "bbb", "files": [["src/driver.c", blob("drv")],
                                                          ["src/grbl", "cccc"], ["src/grbl/core.c", blob("core")]]},
        "kernel-module-glowforge": {"srcrev": "ddd", "files": [["src/cnc.c", blob("cnc")]]},
        "linux-fslc": {"srcrev": "eee", "files": [["@srcrev", "eee"], ["@config", "cfg1"]]},
        "forgetest": {"srcrev": None, "files": [["forgetest/x.py", blob("x")]]},
    }
    plat = platform if platform is not None else {
        "machine": "glowforge", "kernel_modules": ["6.12.20-fslc+g0e01ec9f0d3f"],
        "dtb": {"glowforge.dtb": "d" * 64},
        "layers": {"meta-forgefirm": {"content_sha256": "1" * 64}, "poky": {"rev": "p" * 40}},
    }
    data = {"format": 1, "image": {"name": name, "version": version},
            "components": comps, "platform": plat}
    data["content_sha256"] = manifest_mod.sha256_text(
        manifest_mod.canonical({"components": comps, "platform": plat}))
    return manifest_mod.Manifest(data)


def with_file(manifest, component, path, text):
    """A copy of the manifest with one file's content changed/added."""
    import copy
    data = copy.deepcopy(manifest.data)
    files = data["components"][component]["files"]
    for f in files:
        if f[0] == path:
            f[1] = blob(text)
            break
    else:
        files.append([path, blob(text)])
    data["content_sha256"] = manifest_mod.sha256_text(
        manifest_mod.canonical({"components": data["components"], "platform": data["platform"]}))
    return manifest_mod.Manifest(data)


def with_platform(manifest, **changes):
    import copy
    data = copy.deepcopy(manifest.data)
    data["platform"].update(changes)
    data["content_sha256"] = manifest_mod.sha256_text(
        manifest_mod.canonical({"components": data["components"], "platform": data["platform"]}))
    return manifest_mod.Manifest(data)


def _noop(ctx):
    pass


def make_test(id, covers, always=False, requires=(), kind="auto", fn=None, subsystem=None, mode=None,
              actions=(), precheck=None, steps=(), hands=()):
    return catalog_mod.Test(id, "Title " + id, subsystem or id.split(".")[0], kind, "api",
                            covers, requires, always, 1, steps, "desc", fn or _noop, mode=mode,
                            actions=actions, precheck=precheck, hands=hands)


def registry(*tests):
    return {t.id: t for t in tests}


# ------------------------------------------- a test body's judgment alone

class JudgeCtx:
    """The runner's Context as a test body sees it, with nobody at the
    machine, for driving a catalog test's judgment over a scripted machine:
    check raises the runner's Failed, the sleeps take no time, a wait reads
    its condition once, and act is the press the scripted machine answers."""

    def __init__(self, forgectrl=None, press=None):
        self.evidence = {}
        self.lines = []
        self.forgectrl = forgectrl
        self.press = press

    def check(self, cond, msg, *args):
        if not cond:
            from forgetest.runner import Failed
            raise Failed(msg % args if args else msg)

    def log(self, msg, *args):
        self.lines.append(msg % args if args else msg)

    def checkpoint(self):
        pass

    def sleep(self, seconds):
        pass

    def aborted(self):
        return False

    def wait_for(self, cond, timeout, poll=0.25):
        try:
            return 0.0 if cond() else None
        except Exception:
            return None

    def act(self, channel, state, **kw):
        self.press()


class Clock:
    """time.time and time.sleep for a module under test: the clock moves
    only when the module sleeps, and each sleep runs the machine's tick."""

    def __init__(self, tick=None):
        self.t = 1000.0
        self.tick = tick or (lambda: None)

    def time(self):
        return self.t

    def sleep(self, seconds):
        self.t += seconds
        self.tick()


# ------------------------------------------------------- fake forgectrl

def supervisor_locks():
    """The latch locked, as forgectrl locks it when a controller's session
    ends or begins: bit 3 of the fake sysfs tree's interlock word reads
    locked after it. Nothing when the test has no tree. FakeForgectrl calls
    it where forgectrl does (see do_POST)."""
    from forgetest import hw
    root = hw.sysfs_root()
    if not os.path.isdir(root + "cnc"):
        return
    try:
        hw.sysfs_write("cnc/laser_latch", "1")
    except OSError:
        return
    path = root + "cnc/interlock_circuit"
    if os.path.exists(path):
        with open(path) as f:
            ilk = int(f.read().split()[0], 0)
        with open(path, "w") as f:
            f.write(str(ilk | 0x08))


class FakeForgectrl:
    """A stand-in for the machine-services daemon on localhost: canned
    JSON for the endpoints the suite reads, a mutable `state` the test
    scripts, every POST recorded, and an optional `on_post(path, form)`
    hook returning (status, body) to script the daemon's reactions. Where
    forgectrl locks the latch, the fake locks it too (supervisor_locks),
    after the hook, unless the hook refused the request: a stop with a
    controller running (super.c stop_locked), a GRBL controller's start (it
    locks at its init, stepper_stream.c; the supervisor's spawn and the
    cloud client write no latch), a mode switch that stops a running
    controller, and a settings write of xy_microsteps that restarts a
    running GRBL controller (main.c restart_grbl).
    Point the suite at it with FORGECTRL_URL (see start/stop)."""

    def __init__(self):
        import http.server
        import json as _json
        import threading as _threading
        import urllib.parse as _up
        self.state = {
            "mode": {"mode": "grbl", "controller": "running", "pid": 100, "motion": "verified"},
            "status": {"state": "idle", "homed": False, "diag": False, "laser_locked": True,
                       "pos": {"x": 0.0, "y": 0.0, "z": 0.0},
                       "switches": {"lid": True, "button": False, "interlock_ok": True,
                                    "head": True, "hv_enable": False}},
            "cool": {"phase": "idle", "armed": False, "hold": False},
            "cam": {"running": False, "clients": 0},
            "diag": {"running": False},
            "settings": {"controller_mode": "grbl", "lid_lamp_idle": ""},
            "logs_tail": {"name": "forgectrl", "text": "", "truncated": False, "exists": True},
        }
        self.posts = []
        self.on_post = None
        self.on_get = None          # (path, query) -> (status, body) | (status, bytes, ctype) | None
        fake = self

        class H(http.server.BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def _send(self, st, body):
                data = _json.dumps(body).encode()
                self.send_response(st)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def do_GET(self):
                path, _, query = self.path.partition("?")
                if fake.on_get:
                    r = fake.on_get(path, dict(_up.parse_qsl(query, keep_blank_values=True)))
                    if r is not None:
                        if isinstance(r[1], (bytes, bytearray)):
                            self.send_response(r[0])
                            self.send_header("Content-Type", r[2] if len(r) > 2 else "application/octet-stream")
                            self.send_header("Content-Length", str(len(r[1])))
                            self.end_headers()
                            self.wfile.write(r[1])
                            return
                        return self._send(r[0], r[1])
                key = {"/mode": "mode", "/status": "status", "/cool/status": "cool", "/cam/status": "cam",
                       "/diag/status": "diag", "/settings": "settings",
                       "/logs/tail": "logs_tail"}.get(path)
                if key is None:
                    return self._send(404, {"error": "no " + path})
                self._send(200, fake.state[key])

            def do_POST(self):
                path, _, query = self.path.partition("?")
                n = int(self.headers.get("Content-Length") or 0)
                raw = self.rfile.read(n).decode() if n else ""
                form = dict(_up.parse_qsl(raw if raw else query, keep_blank_values=True))
                fake.posts.append((path, form))
                mode = fake.state["mode"]
                running = mode.get("controller") == "running"
                locks = ((path == "/controller/stop" and running)
                         or (path == "/controller/start" and not running and mode.get("mode") == "grbl")
                         or (path == "/mode" and form.get("controller")
                             and ((running and form.get("controller") != mode.get("mode"))
                                  or (not running and form.get("controller") == "grbl")))
                         or (path == "/settings" and "xy_microsteps" in form and running
                             and mode.get("mode") == "grbl"
                             and form["xy_microsteps"] != fake.state["settings"].get("xy_microsteps")))
                if fake.on_post:
                    r = fake.on_post(path, form)
                    if r is not None:
                        if locks and r[0] < 400:
                            supervisor_locks()
                        return self._send(*r)
                if locks:
                    supervisor_locks()
                if path == "/mode" and form.get("controller"):
                    fake.state["mode"] = dict(fake.state["mode"], mode=form["controller"], controller="running",
                                              pid=fake.state["mode"].get("pid", 0) + 1)
                    fake.state["settings"]["controller_mode"] = form["controller"]
                elif path == "/settings":
                    fake.state["settings"].update(form)
                    return self._send(200, fake.state["settings"])
                self._send(200, {"ok": True})

        self._srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), H)
        self._srv.daemon_threads = True
        self._srv.block_on_close = False        # never wait on a lingering connection
        self.url = "http://127.0.0.1:%d" % self._srv.server_address[1]
        # a short poll: stop() waits for the serving loop's next poll
        self._th = _threading.Thread(target=self._srv.serve_forever, kwargs={"poll_interval": 0.05},
                                     daemon=True)

    def start(self):
        self._th.start()
        os.environ["FORGECTRL_URL"] = self.url
        os.environ["FORGECTRL_TOKEN_FILE"] = os.devnull
        return self

    def stop(self):
        self._srv.shutdown()
        self._srv.server_close()
        os.environ.pop("FORGECTRL_URL", None)
        os.environ.pop("FORGECTRL_TOKEN_FILE", None)


# ------------------------------------------------------------ fake grbl

class FakeGrbl:
    """A Grbl-over-TCP stand-in for the controller: answers '?' with a
    status report built from its mutable `state`/`mpos`, 'ok' to every
    command line, and records what it was sent. Point the suite at it
    with GRBL_HOST/GRBL_PORT (see start/stop)."""

    def __init__(self):
        import socket as _socket
        import threading as _threading
        self.state = "Idle"
        self.mpos = [0.0, 0.0, 0.0]
        self.sent = []
        self.extra = b""            # text pushed to the client on the next poll
        self.reset_to = None        # the state a soft reset (^X) lands in, when set
        self.on_command = None      # called with every command line received
        self.settings = {}          # "$N" -> value: "$N=V" sets one, "$N" reports it
        self.pinned = {}            # "$N" -> the value a write answers ok to and does not change
        self.unanswered = 0         # this many '?' go unanswered (a reset that ate them)
        self._sock = _socket.socket()
        self._sock.setsockopt(_socket.SOL_SOCKET, _socket.SO_REUSEADDR, 1)
        self._sock.bind(("127.0.0.1", 0))
        self._sock.listen(5)
        self.port = self._sock.getsockname()[1]
        self._stop = False
        self._th = _threading.Thread(target=self._serve, daemon=True)

    def _serve(self):
        import socket as _socket
        import threading as _threading
        self._sock.settimeout(0.2)
        while not self._stop:
            try:
                c, _ = self._sock.accept()
            except (_socket.timeout, OSError):
                continue
            _threading.Thread(target=self._client, args=(c,), daemon=True).start()

    def _client(self, c):
        import socket as _socket
        c.settimeout(0.1)
        buf = b""
        try:
            c.sendall(b"\r\nGrblHAL 1.1f ['$' for help]\r\n")
            while not self._stop:
                try:
                    d = c.recv(4096)
                    if not d:
                        break
                    buf += d
                except _socket.timeout:
                    continue
                except OSError:
                    break
                out = b""
                if self.extra:
                    out += self.extra
                    self.extra = b""
                while buf:
                    if buf[0:1] == b"?":
                        buf = buf[1:]
                        if self.unanswered > 0:
                            self.unanswered -= 1
                            continue
                        out += ("<%s|MPos:%.3f,%.3f,%.3f|FS:0,0>\r\n" % (self.state, *self.mpos)).encode()
                    elif buf[0] in (0x18, 0x85, 0x7E, 0x21):
                        if buf[0] == 0x18:
                            self.sent.append("^X")
                            if self.reset_to:
                                self.state = self.reset_to
                        buf = buf[1:]
                    elif b"\n" in buf:
                        line, buf = buf.split(b"\n", 1)
                        self.sent.append(line.strip().decode("utf-8", "replace"))
                        if self.on_command:
                            self.on_command(self.sent[-1])
                        key, eq, val = self.sent[-1].partition("=")
                        if key[:1] == "$" and key[1:].isdigit():
                            if eq:
                                self.settings[key] = self.pinned.get(key, val)
                            elif key in self.settings:
                                out += ("%s=%s\r\n" % (key, self.settings[key])).encode()
                        out += b"ok\r\n"
                    else:
                        break
                if out:
                    c.sendall(out)
        finally:
            c.close()

    def start(self):
        self._th.start()
        os.environ["GRBL_HOST"] = "127.0.0.1"
        os.environ["GRBL_PORT"] = str(self.port)
        return self

    def stop(self):
        self._stop = True
        try:
            self._sock.close()
        except OSError:
            pass
        for k in ("GRBL_HOST", "GRBL_PORT"):
            os.environ.pop(k, None)
