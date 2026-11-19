# Copyright 2026 514 LLC d/b/a OpenGlow
# Written by Scott Wiederhold
# https://community.openglow.org
# SPDX-License-Identifier:    MIT

"""A queue never fires the laser without the acknowledgment: one that
holds a live test is refused, and nothing of it is left to start, until
the operator acknowledges eye protection, the fire watch and the exhaust.
A queue stops at its first result that is not a PASS: a FAIL closes the
campaign, and going on would open a second one behind the operator. The
operator's Stop starts nothing after the run in progress.
"""
import json
import os
import shutil
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.request

import helpers
from forgetest import server
from forgetest.log import Log
from forgetest.runner import Runner


def t_pass(ctx):
    ctx.log("ok")


def t_prompt(ctx):
    ctx.prompt("Ready?", ("Continue",))


def t_fail(ctx):
    ctx.check(False, "deliberate")


def t_slow(ctx):
    ctx.sleep(30)


class QueueTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.mkdtemp(prefix="forgetest-queue-")
        os.environ["FORGETEST_DATA"] = cls.tmp
        os.environ["FORGETEST_MARKER"] = os.path.join(cls.tmp, "marker")
        helpers.host_run()
        cls.man = helpers.make_manifest()
        cls.reg = helpers.registry(
            helpers.make_test("q.a", [("forgectrl", "src/auth.c")], fn=t_pass),
            helpers.make_test("q.op", [("forgectrl", "src/main.c")], kind="operator", fn=t_prompt),
            helpers.make_test("q.live", [("grblhal-glowforge", "src/**")], kind="live", fn=t_pass),
        )
        cls.log = Log(os.path.join(cls.tmp, "results.jsonl"))
        cls.runner = Runner(cls.log, cls.man, cls.reg)
        cls.token = server.load_token(os.path.join(cls.tmp, "token"))
        cls.app = server.App(cls.runner, cls.token, export_dir=os.path.join(cls.tmp, "export"))
        cls.srv = server.make_server(cls.app, "127.0.0.1", 0)
        cls.port = cls.srv.server_address[1]
        threading.Thread(target=cls.srv.serve_forever, daemon=True).start()

    @classmethod
    def tearDownClass(cls):
        cls.srv.shutdown()
        cls.srv.server_close()
        shutil.rmtree(cls.tmp, ignore_errors=True)
        helpers.end_host_run()

    def call(self, method, path, body=None):
        url = "http://127.0.0.1:%d%s" % (self.port, path)
        hdrs = {"Host": "127.0.0.1:%d" % self.port, "X-ForgeFIRM-Token": self.token}
        data = None
        if body is not None:
            data = json.dumps(body).encode()
            hdrs["Content-Type"] = "application/json"
        req = urllib.request.Request(url, data=data, method=method, headers=hdrs)
        try:
            with urllib.request.urlopen(req, timeout=10) as r:
                return r.status, json.loads(r.read().decode())
        except urllib.error.HTTPError as e:
            return e.code, json.loads(e.read().decode())

    def test_an_attended_queue_with_a_live_test_needs_the_acknowledgment(self):
        st, d = self.call("POST", "/batch", {"group": "attended"})
        self.assertEqual(st, 409)
        self.assertIn("q.live", d["message"])
        st, d = self.call("GET", "/state")
        self.assertIsNone(d["batch"], "a refused queue must not exist")
        self.assertIsNone(d["running"], "a refused queue started a run")

    def test_a_failure_stops_the_queue(self):
        reg = helpers.registry(
            helpers.make_test("f.a", [("forgectrl", "src/ui.c")], fn=t_pass),
            helpers.make_test("f.b", [("forgectrl", "src/auth.c")], fn=t_fail),
            helpers.make_test("f.c", [("forgectrl", "src/cool.c")], fn=t_pass),
        )
        tmp = tempfile.mkdtemp(prefix="forgetest-fail-")
        self.addCleanup(shutil.rmtree, tmp, ignore_errors=True)
        r = Runner(Log(os.path.join(tmp, "r.jsonl")), self.man, reg)
        ok, msg, order = r.start_batch("unattended")
        self.assertTrue(ok, msg)
        self.assertEqual(order, ["f.a", "f.b", "f.c"])
        deadline = time.monotonic() + 20
        while not r.batch["finished"] and time.monotonic() < deadline:
            time.sleep(0.05)
        b = r.batch_snapshot()
        self.assertEqual([x["test"] for x in b["done"]], ["f.a", "f.b"])
        self.assertEqual(b["pending"], ["f.c"], "f.c ran after a FAIL closed the campaign")

    def test_stop_cancels_what_is_waiting(self):
        # The operator's Stop: the run in progress goes on (Abort is its own
        # lever), and nothing after it starts.
        reg = helpers.registry(
            helpers.make_test("p.slow", [("forgectrl", "src/ui.c")], fn=t_slow),
            helpers.make_test("p.next", [("forgectrl", "src/auth.c")], fn=t_pass),
        )
        tmp = tempfile.mkdtemp(prefix="forgetest-stop-")
        self.addCleanup(shutil.rmtree, tmp, ignore_errors=True)
        r = Runner(Log(os.path.join(tmp, "r.jsonl")), self.man, reg)
        r.start_batch("unattended")
        deadline = time.monotonic() + 10
        while r.current is None and time.monotonic() < deadline:
            time.sleep(0.02)
        ok, msg = r.stop_batch()
        self.assertTrue(ok, msg)
        self.assertTrue(r.batch_snapshot()["stopping"])
        r.abort()
        deadline = time.monotonic() + 20
        while not r.batch["finished"] and time.monotonic() < deadline:
            time.sleep(0.05)
        self.assertNotIn("p.next", [x["test"] for x in r.batch_snapshot()["done"]])


if __name__ == "__main__":
    unittest.main()
