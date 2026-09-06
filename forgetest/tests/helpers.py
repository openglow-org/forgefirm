"""Shared fixtures for the forgetest unit tests: synthetic manifests and
catalog entries that never touch hardware."""
import hashlib
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))

from forgetest import catalog as catalog_mod  # noqa: E402
from forgetest import manifest as manifest_mod  # noqa: E402


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


# ------------------------------------------------------- fake forgectrl

class FakeForgectrl:
    """A stand-in for the machine-services daemon on localhost: canned
    JSON for the endpoints the suite reads, a mutable `state` the test
    scripts, every POST recorded, and an optional `on_post(path, form)`
    hook returning (status, body) to script the daemon's reactions.
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
                if fake.on_post:
                    r = fake.on_post(path, form)
                    if r is not None:
                        return self._send(*r)
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
        self._th = _threading.Thread(target=self._srv.serve_forever, daemon=True)

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
