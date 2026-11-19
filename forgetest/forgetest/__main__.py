# Copyright 2026 514 LLC d/b/a OpenGlow
# Written by Scott Wiederhold
# https://community.openglow.org
# SPDX-License-Identifier:    MIT

"""forgetest daemon entry point.

  python3 -m forgetest [--port 8090] [--host 127.0.0.1] [--manifest PATH]

The listener is the loopback: the page carries the token every write
needs, so only the machine itself and an SSH session reach it (a login
with `ssh -L 8090:127.0.0.1:8090`, then http://127.0.0.1:8090/).

One daemon runs at a time: each takes an exclusive lock on the file beside
the takeover marker (FORGETEST_MARKER, default /run/forgetest.active, so
/run/forgetest.active.lock) before it makes its runner, which recovers a
takeover the marker records, and keeps it until the process ends.

Environment: FORGETEST_DATA (state directory, default /data/forgetest),
FORGETEST_MANIFEST, FORGETEST_BENCH_DIR, FORGETEST_MARKER, and the hw.py
overrides.
"""
import argparse
import fcntl
import os
import signal
import sys
import threading

from . import VERSION
from . import bench as _bench
from . import catalog as _catalog
from . import manifest as _manifest
from . import server as _server
from .log import Log, data_dir
from .runner import Runner, configure_journal, marker_path

LOOPBACK = "127.0.0.1"
# The daemon's lock, beside the takeover marker on /run (a tmpfs): one
# daemon at a time, whatever address or port each would listen on. Held
# from before the runner is made until the process ends.
_LOCK = None


def lock_path():
    return marker_path() + ".lock"


def hold_lock(path):
    """An exclusive lock on path, as an open descriptor that keeps it, or
    None when another process holds it. Close-on-exec: a bench tool the
    daemon starts does not keep it."""
    fd = os.open(path, os.O_RDWR | os.O_CREAT | os.O_CLOEXEC, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        os.close(fd)
        return None
    return fd


def parse_args(argv=None):
    ap = argparse.ArgumentParser(prog="forgetest")
    ap.add_argument("--port", type=int, default=int(os.environ.get("FORGETEST_PORT") or 8090))
    ap.add_argument("--host", default=os.environ.get("FORGETEST_HOST") or LOOPBACK)
    ap.add_argument("--manifest", default=None)
    ap.add_argument("--version", action="version", version="forgetest %s" % VERSION)
    return ap.parse_args(argv)


def main(argv=None):
    global _LOCK
    args = parse_args(argv)
    try:
        manifest = _manifest.Manifest.load(args.manifest)
    except (OSError, ValueError) as e:
        print("forgetest: cannot load the image manifest: %s" % e, file=sys.stderr)
        return 2
    registry = _catalog.load_suite()
    os.makedirs(data_dir(), exist_ok=True)
    configure_journal()
    # The lock, then the port: a daemon that cannot have either (another
    # one holds it) ends here, before its runner recovers a takeover
    # marker that may be the running daemon's own, under its drill.
    try:
        _LOCK = hold_lock(lock_path())
    except OSError as e:
        print("forgetest: cannot open the lock %s (FORGETEST_MARKER names its directory): %s"
              % (lock_path(), e), file=sys.stderr)
        return 2
    if _LOCK is None:
        print("forgetest: another forgetest holds %s" % lock_path(), file=sys.stderr)
        return 2
    token = _server.load_token()
    app = _server.App(None, token)
    try:
        srv = _server.make_server(app, args.host, args.port)
    except OSError as e:
        print("forgetest: cannot listen on %s:%d: %s" % (args.host, args.port, e), file=sys.stderr)
        os.close(_LOCK)
        _LOCK = None
        return 2
    log = Log()
    bench = _bench.Bench()
    app.runner = Runner(log, manifest, registry, bench)

    stop = threading.Event()

    def on_signal(*_):
        stop.set()
    signal.signal(signal.SIGTERM, on_signal)
    signal.signal(signal.SIGINT, on_signal)
    print("forgetest %s: %d tests, image %s (%s), listening on %s:%d"
          % (VERSION, len(registry), manifest.version, (manifest.content_sha or "")[:12],
             args.host, args.port), file=sys.stderr, flush=True)
    th = threading.Thread(target=srv.serve_forever, name="forgetest-http", daemon=True)
    th.start()
    try:
        while not stop.is_set():
            stop.wait(1.0)
    finally:
        srv.shutdown()
        srv.server_close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
