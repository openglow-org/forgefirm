# Copyright 2026 514 LLC d/b/a OpenGlow
# Written by Scott Wiederhold
# https://community.openglow.org
# SPDX-License-Identifier:    MIT

"""forgetest daemon entry point.

  python3 -m forgetest [--port 8090] [--host ::] [--manifest PATH]

Environment: FORGETEST_DATA (state directory, default /data/forgetest),
FORGETEST_MANIFEST, FORGETEST_BENCH_DIR, FORGETEST_MARKER, and the hw.py
overrides.
"""
import argparse
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
from .runner import Runner, configure_journal


def main(argv=None):
    ap = argparse.ArgumentParser(prog="forgetest")
    ap.add_argument("--port", type=int, default=int(os.environ.get("FORGETEST_PORT") or 8090))
    ap.add_argument("--host", default=os.environ.get("FORGETEST_HOST") or "::")
    ap.add_argument("--manifest", default=None)
    ap.add_argument("--version", action="version", version="forgetest %s" % VERSION)
    args = ap.parse_args(argv)

    try:
        manifest = _manifest.Manifest.load(args.manifest)
    except (OSError, ValueError) as e:
        print("forgetest: cannot load the image manifest: %s" % e, file=sys.stderr)
        return 2
    registry = _catalog.load_suite()
    os.makedirs(data_dir(), exist_ok=True)
    configure_journal()
    log = Log()
    bench = _bench.Bench()
    runner = Runner(log, manifest, registry, bench)
    token = _server.load_token()
    app = _server.App(runner, token)
    srv = _server.make_server(app, args.host, args.port)

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
