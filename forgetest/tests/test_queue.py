"""The two queues: what they select, the order they run in, and where
they stop.

A campaign is mostly waiting, so the page offers to run what is still
owed in one go: the unattended tests for an empty room, the attended ones
for an operator at the machine. The rules that matter here are that a
queue takes exactly the unsatisfied tests of its kinds, runs a
prerequisite before the test that names it, stops on the first result
that is not a PASS (a FAIL closes the campaign, so going on would open a
second one behind the operator), and never fires the laser without the
acknowledgment.
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
from forgetest import catalog, server
from forgetest.log import Log
from forgetest.runner import BATCH_GROUPS, Failed, Runner


def t_pass(ctx):
    ctx.log("ok")


def t_fail(ctx):
    ctx.check(False, "deliberate")


def t_prompt(ctx):
    ctx.prompt("Ready?", ("Continue",))


def t_slow(ctx):
    ctx.sleep(30)


class OrderTests(unittest.TestCase):
    """order_by_requires is a pure function over the requires graph."""

    def order(self, tests, selected):
        return catalog.order_by_requires(tests, selected)

    def test_prerequisite_first(self):
        a = helpers.make_test("s.a", [], fn=t_pass)
        b = helpers.make_test("s.b", [], requires=("s.a",), fn=t_pass)
        # selection order must not matter; registration order breaks ties
        self.assertEqual(self.order([b, a], ["s.b", "s.a"]), ["s.a", "s.b"])
        self.assertEqual(self.order([a, b], ["s.b", "s.a"]), ["s.a", "s.b"])

    def test_chain(self):
        c = helpers.make_test("s.c", [], requires=("s.b",), fn=t_pass)
        b = helpers.make_test("s.b", [], requires=("s.a",), fn=t_pass)
        a = helpers.make_test("s.a", [], fn=t_pass)
        self.assertEqual(self.order([c, b, a], ["s.c", "s.b", "s.a"]), ["s.a", "s.b", "s.c"])

    def test_registration_order_otherwise(self):
        ts = [helpers.make_test("s.%d" % i, [], fn=t_pass) for i in range(4)]
        self.assertEqual(self.order(ts, [t.id for t in ts]), ["s.0", "s.1", "s.2", "s.3"])

    def test_prerequisite_outside_the_selection_places_nothing(self):
        a = helpers.make_test("s.a", [], fn=t_pass)
        b = helpers.make_test("s.b", [], requires=("s.a",), fn=t_pass)
        self.assertEqual(self.order([a, b], ["s.b"]), ["s.b"])

    def test_unknown_and_empty(self):
        a = helpers.make_test("s.a", [], fn=t_pass)
        self.assertEqual(self.order([a], []), [])
        self.assertEqual(self.order([a], ["s.nope"]), [])

    def test_a_cycle_does_not_recurse_forever(self):
        """validate() rejects cycles; a malformed registry must still not
        hang the runner."""
        a = helpers.make_test("s.a", [], requires=("s.b",), fn=t_pass)
        b = helpers.make_test("s.b", [], requires=("s.a",), fn=t_pass)
        out = self.order([a, b], ["s.a", "s.b"])
        self.assertEqual(sorted(out), ["s.a", "s.b"])


class RunClockTests(unittest.TestCase):
    """The page shows the last run until the next one starts, so a
    finished run's elapsed figure must be the time it took, not the time
    since. Found on the bench reference, where a failed test went on
    counting past 5000 s and read as still running."""

    def test_a_finished_runs_clock_stops(self):
        from forgetest.runner import Run
        run = Run("test", "t.x", "t.x")
        run.started = time.time() - 30
        self.assertGreaterEqual(run.snapshot()["elapsed_s"], 30)
        run.finished = {"result": "FAIL", "message": "", "duration_s": 31}
        first = run.snapshot()["elapsed_s"]
        time.sleep(1.1)
        self.assertEqual(first, 31)
        self.assertEqual(run.snapshot()["elapsed_s"], 31)

    def test_a_running_runs_clock_ticks(self):
        from forgetest.runner import Run
        run = Run("test", "t.x", "t.x")
        run.started = time.time() - 5
        first = run.snapshot()["elapsed_s"]
        time.sleep(1.1)
        self.assertGreater(run.snapshot()["elapsed_s"], first)


class QueueTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.mkdtemp(prefix="forgetest-queue-")
        os.environ["FORGETEST_DATA"] = cls.tmp
        os.environ["FORGETEST_MARKER"] = os.path.join(cls.tmp, "marker")
        cls.man = helpers.make_manifest()
        cls.reg = helpers.registry(
            # auto: b requires a, so a queue must run a first even though
            # b registers earlier
            helpers.make_test("q.b", [("forgectrl", "src/ui.c")], requires=("q.a",), fn=t_pass),
            helpers.make_test("q.a", [("forgectrl", "src/auth.c")], fn=t_pass),
            helpers.make_test("q.c", [("forgectrl", "src/cool.c")], fn=t_pass),
            # attended
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

    def setUp(self):
        # A clean history per test: a PASS left by an earlier one would be
        # inherited and drop that test out of the selection. Truncating is
        # also the shrunk-file path Log.read has to notice.
        open(self.log.path, "w").close()
        self.assertEqual(self.log.read(), [])
        self.runner.batch = None

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

    def wait_queue(self, timeout=30):
        deadline = time.time() + timeout
        while time.time() < deadline:
            st, d = self.call("GET", "/state")
            if d["batch"] and d["batch"]["finished"]:
                return d
            time.sleep(0.05)
        self.fail("the queue did not finish")

    # -- selection ---------------------------------------------------------
    def test_groups_split_the_catalog_by_who_has_to_be_there(self):
        self.assertEqual(BATCH_GROUPS["unattended"], ("auto",))
        self.assertEqual(sorted(BATCH_GROUPS["attended"]), ["live", "operator"])
        st, d = self.call("GET", "/state")
        self.assertEqual(d["batch_available"]["unattended"], ["q.a", "q.b", "q.c"])
        self.assertEqual(d["batch_available"]["attended"], ["q.op", "q.live"])

    def test_selection_runs_prerequisites_first(self):
        """q.b registers before q.a but requires it."""
        st, d = self.call("GET", "/state")
        order = d["batch_available"]["unattended"]
        self.assertLess(order.index("q.a"), order.index("q.b"))

    def test_satisfied_tests_drop_out(self):
        st, d = self.call("POST", "/start", {"test": "q.c"})
        self.assertEqual(st, 200)
        self.wait_idle()
        st, d = self.call("GET", "/state")
        self.assertEqual(d["tests"]["q.c"]["status"], "pass")
        self.assertNotIn("q.c", d["batch_available"]["unattended"])
        self.assertEqual(d["batch_available"]["unattended"], ["q.a", "q.b"])

    def wait_idle(self, timeout=20):
        deadline = time.time() + timeout
        while time.time() < deadline:
            st, d = self.call("GET", "/state")
            if not d["running"]:
                return d
            time.sleep(0.05)
        self.fail("run did not finish")

    # -- running -----------------------------------------------------------
    def test_unattended_queue_runs_everything_in_order(self):
        st, d = self.call("POST", "/batch", {"group": "unattended"})
        self.assertEqual(st, 200)
        self.assertEqual(d["order"], ["q.a", "q.b", "q.c"])
        d = self.wait_queue()
        b = d["batch"]
        self.assertEqual([x["test"] for x in b["done"]], ["q.a", "q.b", "q.c"])
        self.assertEqual(set(x["result"] for x in b["done"]), {"PASS"})
        self.assertEqual(b["skipped"], [])
        self.assertIsNone(b["stopped"])
        for tid in ("q.a", "q.b", "q.c"):
            self.assertEqual(d["tests"][tid]["status"], "pass", tid)
        # and the run records say which queue put them there
        st, rec = self.call("GET", "/result?test=q.b")
        self.assertEqual(rec["evidence"]["batch"]["group"], "unattended")

    def test_an_empty_queue_is_refused_not_started(self):
        self.call("POST", "/batch", {"group": "unattended"})
        self.wait_queue()
        st, d = self.call("POST", "/batch", {"group": "unattended"})
        self.assertEqual(st, 409)
        self.assertIn("already satisfied", d["message"])

    def test_a_failure_stops_the_queue(self):
        reg = helpers.registry(
            helpers.make_test("f.a", [("forgectrl", "src/ui.c")], fn=t_pass),
            helpers.make_test("f.b", [("forgectrl", "src/auth.c")], fn=t_fail),
            helpers.make_test("f.c", [("forgectrl", "src/cool.c")], fn=t_pass),
        )
        tmp = tempfile.mkdtemp(prefix="forgetest-fail-")
        try:
            r = Runner(Log(os.path.join(tmp, "r.jsonl")), self.man, reg)
            ok, msg, order = r.start_batch("unattended")
            self.assertTrue(ok, msg)
            self.assertEqual(order, ["f.a", "f.b", "f.c"])
            deadline = time.time() + 20
            while not r.batch["finished"] and time.time() < deadline:
                time.sleep(0.05)
            b = r.batch_snapshot()
            self.assertEqual([x["test"] for x in b["done"]], ["f.a", "f.b"])
            self.assertEqual(b["done"][1]["result"], "FAIL")
            self.assertEqual(b["stopped"], "FAIL on f.b")
            self.assertEqual(b["pending"], ["f.c"], "f.c ran after a FAIL closed the campaign")
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    def test_a_test_that_cannot_start_is_skipped_not_silently_dropped(self):
        """An unmet prerequisite outside the queue refuses the start; the
        queue records why and carries on."""
        reg = helpers.registry(
            helpers.make_test("k.op", [("forgectrl", "src/main.c")], kind="operator", fn=t_pass),
            helpers.make_test("k.a", [("forgectrl", "src/ui.c")], requires=("k.op",), fn=t_pass),
            helpers.make_test("k.b", [("forgectrl", "src/auth.c")], fn=t_pass),
        )
        tmp = tempfile.mkdtemp(prefix="forgetest-skip-")
        try:
            r = Runner(Log(os.path.join(tmp, "r.jsonl")), self.man, reg)
            ok, msg, order = r.start_batch("unattended")
            self.assertEqual(order, ["k.a", "k.b"])
            deadline = time.time() + 20
            while not r.batch["finished"] and time.time() < deadline:
                time.sleep(0.05)
            b = r.batch_snapshot()
            self.assertEqual([x["test"] for x in b["skipped"]], ["k.a"])
            self.assertIn("prerequisites not satisfied", b["skipped"][0]["reason"])
            self.assertEqual([x["test"] for x in b["done"]], ["k.b"])
            self.assertIsNone(b["stopped"])
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    # -- safety and exclusion ----------------------------------------------
    def test_an_attended_queue_with_a_live_test_needs_the_acknowledgment(self):
        st, d = self.call("POST", "/batch", {"group": "attended"})
        self.assertEqual(st, 409)
        self.assertIn("fires the laser", d["message"])
        self.assertIn("q.live", d["message"])
        st, d = self.call("GET", "/state")
        self.assertIsNone(d["batch"], "a refused queue must not exist")

    def test_the_acknowledgment_reaches_every_live_run(self):
        st, d = self.call("POST", "/batch", {"group": "attended", "ack_live": True})
        self.assertEqual(st, 200)
        # the operator test prompts; answer it so the queue can reach the live one
        deadline = time.time() + 20
        while time.time() < deadline:
            st, s = self.call("GET", "/state")
            if s["running"] and s["running"]["prompt"]:
                self.call("POST", "/answer", {"prompt_id": s["running"]["prompt"]["id"],
                                              "value": "Continue"})
                break
            if s["batch"] and s["batch"]["finished"]:
                break
            time.sleep(0.05)
        d = self.wait_queue()
        self.assertEqual([x["test"] for x in d["batch"]["done"]], ["q.op", "q.live"])
        st, rec = self.call("GET", "/result?test=q.live")
        self.assertTrue(rec["evidence"]["operator"]["ack_live"])

    def test_a_single_start_is_refused_while_a_queue_runs(self):
        reg = helpers.registry(
            helpers.make_test("s.slow", [("forgectrl", "src/ui.c")], fn=t_slow),
            helpers.make_test("s.other", [("forgectrl", "src/auth.c")], fn=t_pass),
        )
        tmp = tempfile.mkdtemp(prefix="forgetest-excl-")
        try:
            r = Runner(Log(os.path.join(tmp, "r.jsonl")), self.man, reg)
            r.start_batch("unattended")
            deadline = time.time() + 10
            while r.current is None and time.time() < deadline:
                time.sleep(0.02)
            ok, msg = r.start_test("s.other")
            self.assertFalse(ok)
            self.assertEqual(msg, "a queue is running")
            ok, msg = r.start_bench("anything")
            self.assertFalse(ok)
            self.assertEqual(msg, "a queue is running")
            ok, msg, _ = r.start_batch("unattended")
            self.assertFalse(ok)
            self.assertIn("already running", msg)
            r.stop_batch()
            r.abort()
            deadline = time.time() + 20
            while not r.batch["finished"] and time.time() < deadline:
                time.sleep(0.05)
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    def test_stop_cancels_what_is_waiting(self):
        reg = helpers.registry(
            helpers.make_test("p.slow", [("forgectrl", "src/ui.c")], fn=t_slow),
            helpers.make_test("p.next", [("forgectrl", "src/auth.c")], fn=t_pass),
        )
        tmp = tempfile.mkdtemp(prefix="forgetest-stop-")
        try:
            r = Runner(Log(os.path.join(tmp, "r.jsonl")), self.man, reg)
            r.start_batch("unattended")
            deadline = time.time() + 10
            while r.current is None and time.time() < deadline:
                time.sleep(0.02)
            ok, msg = r.stop_batch()
            self.assertTrue(ok, msg)
            self.assertTrue(r.batch_snapshot()["stopping"])
            r.abort()          # the run in progress needs its own lever
            deadline = time.time() + 20
            while not r.batch["finished"] and time.time() < deadline:
                time.sleep(0.05)
            b = r.batch_snapshot()
            self.assertNotIn("p.next", [x["test"] for x in b["done"]])
            ok, msg = r.stop_batch()
            self.assertFalse(ok)
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    def test_unknown_group(self):
        st, d = self.call("POST", "/batch", {"group": "everything"})
        self.assertEqual(st, 409)
        self.assertEqual(d["message"], "unknown queue")


if __name__ == "__main__":
    unittest.main()
