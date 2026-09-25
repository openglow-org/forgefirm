#!/usr/bin/env python3
# Copyright 2026 514 LLC d/b/a OpenGlow
# Written by Scott Wiederhold
# https://community.openglow.org
# SPDX-License-Identifier:    MIT
"""Host harness: the controller's cooling reports carry the supervisor's secret.

POST /cool/state is the running controller's channel alone: forgectrl hands
each controller it spawns a secret in its environment (GF_REPORT_SECRET), and
the route asks for it. This harness stands in for forgectrl's listener, runs
the null-sink controller against it, and reads every report as it arrives.

  secret     every report carries X-ForgeFIRM-Report with the secret it was
             started with, and the request is otherwise as it always was
  none       started with no secret, the reports carry no such header
  malformed  a value that is not 32 hex digits (short, a letter out of range,
             a CR LF and a header of its own behind it) is never sent, and
             nothing of it reaches the wire
  runner     the homing runner the controller starts is handed the secret (it
             reports in the controller's place while a gfcloud $H holds the
             machine, and the controller is quiet then), and the reports
             still carry it afterwards; a controller with no secret, or with
             one that is not 32 hex digits, hands the runner none

Usage: cool_report_test.py <path-to-grblHAL_glowforge>
"""
import http.server
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
import time

BIN = os.path.abspath(sys.argv[1]) if len(sys.argv) > 1 else "build/grblHAL_glowforge"
SECRET = "0123456789abcdef0123456789abcdef"
GRBL_PORT = 23960


def fail(msg):
    print("FAIL:", msg)
    sys.exit(1)


class Listener:
    """forgectrl's HTTP listener, as far as the reports need it: every
    request is kept whole (the raw header block included) and answered 200."""

    def __init__(self):
        self.seen = []
        outer = self

        class H(http.server.BaseHTTPRequestHandler):
            def do_POST(self):
                outer.seen.append({"path": self.path, "headers": {k.lower(): v for k, v in self.headers.items()},
                                   "raw": bytes(self.headers)})
                self.send_response(200)
                self.send_header("Content-Length", "2")
                self.end_headers()
                self.wfile.write(b"{}")

            def log_message(self, *a):
                pass
        self.srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), H)
        self.port = self.srv.server_address[1]
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()

    def wait(self, n, timeout=8.0):
        end = time.time() + timeout
        while time.time() < end and len(self.seen) < n:
            time.sleep(0.05)
        return list(self.seen)

    def close(self):
        self.srv.shutdown()


def publish_verdicts(path, stop):
    while not stop.is_set():
        tmp = path + ".tmp"
        with open(tmp, "w") as f:
            f.write('{"ts_mono":%.3f,"fire_ok":true,"verdict":"OK","hold":false,'
                    '"resume_ok":true,"armed":false,"reason":""}' % time.clock_gettime(time.CLOCK_MONOTONIC))
        os.replace(tmp, path)
        stop.wait(0.5)


class Controller:
    def __init__(self, listener, secret, runner_cmd=None):
        self.workdir = tempfile.mkdtemp(prefix="cool-report-")
        conf = os.path.join(self.workdir, "forgefirm.conf")
        with open(conf, "w") as f:
            f.write("cool_fan_grace_s = 0\nhoming_mode = %s\n" % ("gfcloud" if runner_cmd else "none"))
            if runner_cmd:
                f.write("gfcloud_home_cmd = %s\n" % runner_cmd)
        verdict = os.path.join(self.workdir, "cooling.state")
        self.stop = threading.Event()
        threading.Thread(target=publish_verdicts, args=(verdict, self.stop), daemon=True).start()
        env = dict(os.environ, GFHOME_CONF=conf, GF_STATE_DIR=self.workdir, GF_VERDICT_FILE=verdict,
                   GFSINK_DUMP=os.path.join(self.workdir, "stream.bin"), FFLOG_STDERR="1",
                   FORGECTRL_PORT=str(listener.port))
        for k in ("GFSINK", "GF_SWITCH_FILE", "GF_REPORT_SECRET"):
            env.pop(k, None)
        if secret is not None:
            env["GF_REPORT_SECRET"] = secret
        self.proc = subprocess.Popen([BIN, "-p", str(GRBL_PORT)], cwd=self.workdir, env=env,
                                     stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

    def sender(self):
        for _ in range(50):
            try:
                s = socket.create_connection(("127.0.0.1", GRBL_PORT), timeout=2)
                s.settimeout(6)
                return s
            except OSError:
                time.sleep(0.1)
        fail("the controller's Grbl socket never opened")

    def close(self):
        self.stop.set()
        self.proc.terminate()
        try:
            self.proc.wait(5)
        except subprocess.TimeoutExpired:
            self.proc.kill()
        shutil.rmtree(self.workdir, ignore_errors=True)


def run(name, secret, check, runner=False, handed=None):
    lis = Listener()
    ctl = Controller(lis, secret, runner_cmd=("env > %s" % "RUNNER_ENV") if runner else None)
    try:
        if runner:
            env_file = os.path.join(ctl.workdir, "RUNNER_ENV")
            s = ctl.sender()
            time.sleep(0.5)
            s.sendall(b"$H\n")
            end = time.time() + 10
            while time.time() < end and not os.path.exists(env_file):
                time.sleep(0.1)
            if not os.path.exists(env_file):
                fail("[%s] the stand-in homing runner never ran" % name)
            time.sleep(0.3)
            with open(env_file) as f:
                runner_env = f.read()
            got = [ln.split("=", 1)[1] for ln in runner_env.splitlines() if ln.startswith("GF_REPORT_SECRET=")]
            if got != ([handed] if handed else []):
                fail("[%s] the homing runner was handed %r, not %r" % (name, got, handed))
            if not handed and SECRET in runner_env:
                fail("[%s] the secret reached the homing runner another way" % name)
            if "GF_STATE_DIR" not in runner_env:
                fail("[%s] the runner's environment was not read: %r" % (name, runner_env[:80]))
            s.close()
            lis.seen.clear()            # what matters is what is reported after it
        reports = lis.wait(3)
        if len(reports) < 3:
            fail("[%s] %d reports in 8 s: the level-triggered report did not arrive" % (name, len(reports)))
        for r in reports:
            if not r["path"].startswith("/cool/state?mode="):
                fail("[%s] a request that is no report: %s" % (name, r["path"]))
            check(name, r)
        print("ok: %s (%d reports)" % (name, len(reports)))
    finally:
        ctl.close()
        lis.close()


def with_secret(name, r):
    if r["headers"].get("x-forgefirm-report") != SECRET:
        fail("[%s] a report without the secret: %s" % (name, r["headers"]))
    if set(r["headers"]) != {"host", "connection", "content-length", "x-forgefirm-report"}:
        fail("[%s] the report's headers changed: %s" % (name, sorted(r["headers"])))


def without(name, r):
    if "x-forgefirm-report" in r["headers"] or "x-evil" in r["headers"] or b"Evil" in r["raw"]:
        fail("[%s] a header that must not be sent: %s" % (name, r["headers"]))
    if set(r["headers"]) != {"host", "connection", "content-length"}:
        fail("[%s] the report's headers changed: %s" % (name, sorted(r["headers"])))


def main():
    if not os.path.exists(BIN):
        fail("no controller binary at %s" % BIN)
    run("secret", SECRET, with_secret)
    run("none", None, without)
    for i, bad in enumerate(("short", SECRET[:-1] + "g", SECRET + "0", SECRET.upper(),
                             SECRET[:16] + "\r\nX-Evil: 1\r\nX-Pad: 12",
                             SECRET[:16] + "\r\nX-Evil: 1\r\nX:1"), 1):     # the last is 32 long
        run("malformed-%d" % i, bad, without)
    run("runner", SECRET, with_secret, runner=True, handed=SECRET)
    run("runner-none", None, without, runner=True)
    run("runner-malformed", SECRET[:16] + "\r\nX-Evil: 1\r\nX:1", without, runner=True)
    print("cool_report_test: all passed")


if __name__ == "__main__":
    main()
