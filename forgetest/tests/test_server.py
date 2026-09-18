# Copyright 2026 514 LLC d/b/a OpenGlow
# Written by Scott Wiederhold
# https://community.openglow.org
# SPDX-License-Identifier:    MIT

"""Runner + HTTP API end to end on localhost with a fake catalog and a
fake bench tool. No hardware, no forgectrl."""
import json
import os
import shutil
import sys
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.request

import helpers
from forgetest import bench as bench_mod
from forgetest import catalog, server
from forgetest.log import Log
from forgetest.runner import Failed, Runner


def t_pass(ctx):
    ctx.log("hello")
    ctx.evidence["k"] = 1


def t_prompt(ctx):
    ans = ctx.prompt("Did the light blink?", ("Yes", "No"))
    if ans != "Yes":
        raise Failed("operator said no")


def t_fail(ctx):
    ctx.check(False, "deliberate")


def t_slow(ctx):
    ctx.sleep(30)


def t_error(ctx):
    raise RuntimeError("boom")


class ServerTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.mkdtemp(prefix="forgetest-")
        os.environ["FORGETEST_DATA"] = cls.tmp
        os.environ["FORGETEST_MARKER"] = os.path.join(cls.tmp, "marker")
        cls.man = helpers.make_manifest()
        cls.reg = helpers.registry(
            helpers.make_test("fake.pass", [("forgectrl", "src/ui.c")], always=True, fn=t_pass),
            helpers.make_test("fake.prompt", [("forgectrl", "src/auth.c")], fn=t_prompt, kind="operator"),
            helpers.make_test("fake.fail", [("forgectrl", "src/cool.c")], fn=t_fail),
            helpers.make_test("fake.slow", [("forgectrl", "src/main.c")], fn=t_slow),
            helpers.make_test("fake.error", [("forgectrl", "src/main.c")], fn=t_error),
            helpers.make_test("fake.live", [("grblhal-glowforge", "src/**")], fn=t_pass, kind="live"),
            helpers.make_test("fake.needs", [("kernel-module-glowforge", "**")], fn=t_pass,
                              requires=("fake.prompt",)),
        )
        # a fake bench tool
        cls.tooldir = os.path.join(cls.tmp, "bench")
        os.makedirs(cls.tooldir)
        with open(os.path.join(cls.tooldir, "echo_tool.py"), "w") as f:
            f.write("import os, sys, time\nprint('args', sys.argv[1:])\n"
                    "print('env', os.environ.get('GF_HOST'), os.environ.get('FORGETEST_BENCH_DATA'))\n"
                    "sys.stdout.flush()\n"
                    "if 'slow' in sys.argv: time.sleep(30)\nsys.exit(0 if 'fail' not in sys.argv else 3)\n")
        tools = [{"id": "echo", "title": "Echo", "script": "echo_tool.py", "safety": "dry", "where": "board",
                  "ported": True, "desc": "echo",
                  "args": [{"name": "word", "type": "str", "default": "hi", "help": ""},
                           {"name": "n", "type": "int", "default": 2, "help": ""}]},
                 {"id": "unported", "title": "U", "script": "nope.py", "safety": "dry", "where": "host",
                  "ported": False, "desc": "", "args": []},
                 {"id": "hot", "title": "H", "script": "echo_tool.py", "safety": "live", "where": "board",
                  "ported": True, "desc": "", "args": []},
                 {"id": "tk", "title": "T", "script": "echo_tool.py", "safety": "takeover", "where": "board",
                  "ported": True, "desc": "", "args": []},
                 {"id": "sc", "title": "S", "script": "echo_tool.py", "safety": "scope", "where": "board",
                  "ported": True, "desc": "", "args": []}]
        cls.bench = bench_mod.Bench(tools, tool_dir=cls.tooldir,
                                    index_path=os.path.join(cls.tmp, "bench.jsonl"))
        cls.log = Log(os.path.join(cls.tmp, "results.jsonl"))
        cls.runner = Runner(cls.log, cls.man, cls.reg, cls.bench)
        cls.token = server.load_token(os.path.join(cls.tmp, "token"))
        cls.app = server.App(cls.runner, cls.token, export_dir=os.path.join(cls.tmp, "export"))
        cls.srv = server.make_server(cls.app, "127.0.0.1", 0)
        cls.port = cls.srv.server_address[1]
        cls.th = threading.Thread(target=cls.srv.serve_forever, daemon=True)
        cls.th.start()

    @classmethod
    def tearDownClass(cls):
        cls.srv.shutdown()
        cls.srv.server_close()
        shutil.rmtree(cls.tmp, ignore_errors=True)

    # -- helpers ----------------------------------------------------------
    def call(self, method, path, body=None, token=True, headers=None):
        st, payload, _ = self.call_h(method, path, body, token, headers)
        return st, payload

    def call_h(self, method, path, body=None, token=True, headers=None):
        """As call, plus the response headers."""
        url = "http://127.0.0.1:%d%s" % (self.port, path)
        hdrs = {"Host": "127.0.0.1:%d" % self.port}
        if token:
            hdrs["X-ForgeFIRM-Token"] = self.token
        data = None
        if body is not None:
            data = json.dumps(body).encode()
            hdrs["Content-Type"] = "application/json"
        if headers:
            hdrs.update(headers)
        req = urllib.request.Request(url, data=data, method=method, headers=hdrs)
        try:
            with urllib.request.urlopen(req, timeout=10) as r:
                raw = r.read()
                st = r.status
                rh = r.headers
        except urllib.error.HTTPError as e:
            raw = e.read()
            st = e.code
            rh = e.headers
        ct = rh.get("Content-Type", "")
        if "json" in ct:
            return st, json.loads(raw.decode()), rh
        return st, raw, rh

    def wait_idle(self, timeout=10):
        deadline = time.time() + timeout
        while time.time() < deadline:
            st, d = self.call("GET", "/state")
            if not d["running"]:
                return d
            time.sleep(0.1)
        self.fail("run did not finish")

    def wait_prompt(self, timeout=10):
        deadline = time.time() + timeout
        while time.time() < deadline:
            st, d = self.call("GET", "/state")
            if d["running"] and d["running"]["prompt"]:
                return d["running"]["prompt"]
            time.sleep(0.05)
        self.fail("no prompt appeared")

    # -- tests -----------------------------------------------------------------
    def test_01_auth_and_page(self):
        st, d = self.call("GET", "/state", token=False)
        self.assertEqual(st, 200)
        st, d = self.call("GET", "/", token=False)
        self.assertEqual(st, 200)
        self.assertIn(self.token.encode(), d)
        page = d.decode("utf-8")
        # one self-contained response: nothing linked, the token once,
        # the theme attribute the head script sets
        self.assertEqual(page.count(self.token), 1)
        self.assertNotIn("<link ", page)
        self.assertNotIn("<script src=", page)
        self.assertIn("data-bs-theme", page)
        st, d = self.call("GET", "/state", headers={"Host": "evil.example.net"})
        self.assertEqual(st, 403)
        st, d = self.call("GET", "/state", headers={"Origin": "http://evil.example.net"})
        self.assertEqual(st, 403)
        st, d = self.call("GET", "/state", headers={"Sec-Fetch-Site": "cross-site"})
        self.assertEqual(st, 403)
        st, d = self.call("POST", "/start", {"test": "fake.pass"}, token=False)
        self.assertEqual(st, 403)
        self.assertEqual(d["error"], "authentication required")
        st, d = self.call("GET", "/catalog")
        self.assertEqual(st, 200)
        self.assertEqual(len(d["tests"]), 7)
        st, d = self.call("GET", "/nope")
        self.assertEqual(st, 404)

    def test_01c_connection_is_kept_alive(self):
        """A poll per second over a fresh TCP connection each time is
        waste the board does not need to pay."""
        import http.client
        c = http.client.HTTPConnection("127.0.0.1", self.port, timeout=10)
        try:
            for _ in range(3):
                c.request("GET", "/state", headers={"Host": "127.0.0.1:%d" % self.port})
                r = c.getresponse()
                r.read()
                self.assertEqual(r.status, 200)
                self.assertEqual(r.version, 11)
                self.assertNotEqual((r.getheader("Connection") or "").lower(), "close")
                self.assertIsNotNone(r.getheader("Content-Length"))
        finally:
            c.close()

    def test_01d_refused_post_does_not_poison_the_connection(self):
        """A POST refused before its body is read leaves that body in the
        socket. On a kept-alive connection the next read would take it for
        a request line, so a refusal must end the connection instead."""
        import http.client
        c = http.client.HTTPConnection("127.0.0.1", self.port, timeout=10)
        try:
            body = json.dumps({"test": "fake.pass"}).encode()
            c.request("POST", "/start", body=body,
                      headers={"Host": "127.0.0.1:%d" % self.port,
                               "Content-Type": "application/json"})   # no token
            r = c.getresponse()
            payload = json.loads(r.read().decode())
            self.assertEqual(r.status, 403)
            self.assertEqual(payload["error"], "authentication required")
            self.assertEqual((r.getheader("Connection") or "").lower(), "close",
                             "an unread body was left on a connection kept alive")
        finally:
            c.close()
        # the server is still healthy, and the body was never taken for a request
        st, d = self.call("GET", "/state")
        self.assertEqual(st, 200)
        self.assertIsNone(d["running"])

    def test_02_run_pass_opens_campaign(self):
        st, d = self.call("GET", "/state")
        self.assertIsNone(d["campaign"])
        st, d = self.call("POST", "/start", {"test": "fake.pass"})
        self.assertEqual(st, 200, d)
        state = self.wait_idle()
        self.assertIsNotNone(state["campaign"])
        self.assertEqual(state["tests"]["fake.pass"]["status"], "pass")
        self.assertEqual(state["last_run"]["finished"]["result"], "PASS")
        st, rec = self.call("GET", "/result?test=fake.pass")
        self.assertEqual(st, 200)
        self.assertEqual(rec["evidence"]["k"], 1)
        # the baseline passes ran (no machine on the host: nothing to restore)
        self.assertEqual(rec["evidence"]["baseline"], {"pre": [], "post": []})
        self.assertTrue(any("hello" in l for l in rec["log"]))

    def test_03_requires_and_live_gate(self):
        st, d = self.call("POST", "/start", {"test": "fake.needs"})
        self.assertEqual(st, 409)
        self.assertIn("prerequisites", d["message"])
        # the operator's override: the test runs alone and the record says so
        st, d = self.call("POST", "/start", {"test": "fake.needs", "ignore_requires": True})
        self.assertEqual(st, 200)
        state = self.wait_idle()
        self.assertEqual(state["tests"]["fake.needs"]["status"], "pass")
        st, rec = self.call("GET", "/result?test=fake.needs")
        self.assertEqual(rec["evidence"]["prerequisites"]["missing"], ["fake.prompt"])
        self.assertTrue(rec["evidence"]["prerequisites"]["overridden"])
        self.assertTrue(any("prerequisites overridden" in l for l in rec["log"]))
        # its prerequisite is still required for the release
        self.assertFalse(state["tests"]["fake.prompt"]["satisfied"])
        self.assertFalse(state["authorized"])
        st, d = self.call("POST", "/start", {"test": "fake.live"})
        self.assertEqual(st, 409)
        self.assertIn("live", d["message"])
        st, d = self.call("POST", "/start", {"test": "fake.live", "ack_live": True})
        self.assertEqual(st, 200)
        state = self.wait_idle()
        self.assertEqual(state["tests"]["fake.live"]["status"], "pass")
        st, rec = self.call("GET", "/result?test=fake.live")
        self.assertTrue(rec["evidence"]["operator"]["ack_live"])

    def test_04_prompt_flow(self):
        st, d = self.call("POST", "/start", {"test": "fake.prompt"})
        self.assertEqual(st, 200)
        p = self.wait_prompt()
        self.assertEqual(p["options"], ["Yes", "No"])
        st, d = self.call("POST", "/answer", {"prompt_id": p["id"], "value": "Maybe"})
        self.assertEqual(st, 409)
        st, d = self.call("POST", "/answer", {"prompt_id": p["id"], "value": "Yes"})
        self.assertEqual(st, 200)
        state = self.wait_idle()
        self.assertEqual(state["tests"]["fake.prompt"]["status"], "pass")
        st, rec = self.call("GET", "/result?test=fake.prompt")
        self.assertEqual(rec["answers"][0]["answer"], "Yes")
        # now the dependent test may start
        st, d = self.call("POST", "/start", {"test": "fake.needs"})
        self.assertEqual(st, 200)
        self.wait_idle()

    def test_05_busy_abort(self):
        st, d = self.call("POST", "/start", {"test": "fake.slow"})
        self.assertEqual(st, 200)
        st, d = self.call("POST", "/start", {"test": "fake.pass"})
        self.assertEqual(st, 409)
        st, d = self.call("POST", "/abort")
        self.assertEqual(st, 200)
        state = self.wait_idle()
        self.assertEqual(state["tests"]["fake.slow"]["status"], "aborted")
        self.assertIsNotNone(state["campaign"], "an abort does not close the campaign")

    def test_06_error_and_fail_close_campaign(self):
        st, d = self.call("GET", "/state")
        cid = d["campaign"]["id"]
        st, d = self.call("POST", "/start", {"test": "fake.error"})
        state = self.wait_idle()
        self.assertEqual(state["tests"]["fake.error"]["status"], "error")
        self.assertIsNone(state["campaign"])
        self.assertEqual(state["closed_by"], "fail")
        # inherited passes survive; the core is required again
        self.assertEqual(state["tests"]["fake.prompt"]["status"], "inherited")
        self.assertTrue(state["tests"]["fake.pass"]["required"])
        # a new start opens a new campaign
        st, d = self.call("POST", "/start", {"test": "fake.pass"})
        state = self.wait_idle()
        self.assertIsNotNone(state["campaign"])
        self.assertNotEqual(state["campaign"]["id"], cid)

    def test_07_export_and_invalidate(self):
        st, d = self.call("POST", "/export")
        self.assertEqual(st, 200)
        self.assertFalse(d["authorized"])
        st, raw = self.call("GET", "/export/acceptance.json")
        self.assertEqual(st, 200)
        art = raw if isinstance(raw, dict) else json.loads(raw)
        self.assertEqual(art["manifest_sha"], self.man.content_sha)
        st, raw = self.call("GET", "/export/acceptance.md")
        self.assertEqual(st, 200)
        self.assertIn(b"Release authorized: NO", raw)
        st, d = self.call("POST", "/invalidate", {"reason": ""})
        self.assertEqual(st, 400)
        st, d = self.call("POST", "/invalidate", {"reason": "new tube"})
        self.assertEqual(st, 200)
        st, d = self.call("GET", "/state")
        self.assertEqual(d["invalidate"]["reason"], "new tube")
        self.assertIsNone(d["campaign"])
        for tid, ts in d["tests"].items():
            self.assertNotEqual(ts["status"], "inherited", tid)
        st, raw = self.call("GET", "/log", token=False)
        self.assertEqual(st, 200)
        self.assertIn(b'"t": "invalidate"'.replace(b" ", b""), raw.replace(b" ", b""))

    def test_08_bench(self):
        st, d = self.call("GET", "/bench")
        self.assertEqual(st, 200)
        ids = [t["id"] for t in d["tools"]]
        self.assertEqual(ids, ["echo", "unported", "hot", "tk", "sc"])
        st, d = self.call("POST", "/bench/start", {"tool": "unported"})
        self.assertEqual(st, 409)
        st, d = self.call("POST", "/bench/start", {"tool": "hot"})
        self.assertEqual(st, 409)
        st, d = self.call("POST", "/bench/start", {"tool": "echo", "args": {"word": "yo", "n": "x"}})
        self.assertEqual(st, 409)
        st, d = self.call("POST", "/bench/start", {"tool": "echo", "args": {"word": "yo", "n": 5}})
        self.assertEqual(st, 200, d)
        state = self.wait_idle()
        self.assertEqual(state["last_run"]["kind"], "bench")
        self.assertEqual(state["last_run"]["finished"]["result"], "OK")
        self.assertTrue(any("['yo', '5']" in l for l in state["last_run"]["log"]))
        # the bench environment: the machine is local, data under <data>/bench
        self.assertTrue(any(" env 127.0.0.1 " in l and l.endswith("bench")
                            for l in state["last_run"]["log"]), state["last_run"]["log"])
        st, d = self.call("GET", "/bench")
        self.assertEqual(d["tools"][0]["last"]["result"]["result"], "OK")
        # a failing tool and an aborted one
        st, d = self.call("POST", "/bench/start", {"tool": "echo", "args": {"word": "fail", "n": 1}})
        state = self.wait_idle()
        self.assertEqual(state["last_run"]["finished"]["result"], "EXIT 3")
        st, d = self.call("POST", "/bench/start", {"tool": "echo", "args": {"word": "slow", "n": 1}})
        self.assertEqual(st, 200)
        time.sleep(0.5)
        st, d = self.call("POST", "/abort")
        state = self.wait_idle()
        self.assertEqual(state["last_run"]["finished"]["result"], "ABORTED")
        # a takeover tool runs inside the takeover wrapper (init.d is absent on the host: rc 127)
        st, d = self.call("POST", "/bench/start", {"tool": "tk"})
        self.assertEqual(st, 200, d)
        state = self.wait_idle(timeout=40)     # two unreachable-forgectrl settle waits
        log = "\n".join(state["last_run"]["log"])
        self.assertIn("takeover: pulse device free", log)
        self.assertIn("takeover: forgectrl start", log)
        self.assertIn("baseline: forgectrl unreachable for 10 s", log)
        self.assertFalse(os.path.exists(os.environ["FORGETEST_MARKER"]))
        # a scope tool is a takeover too
        st, d = self.call("POST", "/bench/start", {"tool": "sc"})
        self.assertEqual(st, 200, d)
        state = self.wait_idle(timeout=40)
        log = "\n".join(state["last_run"]["log"])
        self.assertIn("takeover: pulse device free", log)
        self.assertIn("takeover: forgectrl start", log)
        # bench runs never touched the acceptance log
        recs = self.log.read()
        self.assertFalse(any(r.get("t") == "result" and r.get("test") == "echo" for r in recs))

    def test_09_recovery_marker(self):
        marker = os.environ["FORGETEST_MARKER"]
        with open(marker, "w") as f:
            f.write("x fake.slow\n")
        import logging
        from forgetest.runner import journal
        seen = []

        class Catch(logging.Handler):
            def emit(self, rec):
                seen.append(rec.getMessage())
        h = Catch()
        journal.addHandler(h)
        try:
            r = Runner(self.log, self.man, self.reg, self.bench)
        finally:
            journal.removeHandler(h)
        # the recovery is a journal line, not a page message
        self.assertTrue(any("recovered" in m for m in seen), seen)
        self.assertNotIn("messages", r.state()[0])
        self.assertFalse(os.path.exists(marker))

    def test_10_state_is_conditional(self):
        """The page polls; an unchanged state must cost a 304, and the
        ETag must move as soon as the state does.

        Runs last because it invalidates: timestamps are whole seconds, and
        a PASS stamped in the same second as an invalidate is deliberately
        not inheritable, so an invalidate early in this class would decide
        the inheritance the earlier tests are checking.
        """
        st, d, hdrs = self.call_h("GET", "/state")
        self.assertEqual(st, 200)
        etag = hdrs.get("ETag")
        self.assertTrue(etag and etag.startswith('"'), "no ETag on /state")
        st, _, hdrs2 = self.call_h("GET", "/state", headers={"If-None-Match": etag})
        self.assertEqual(st, 304)
        self.assertEqual(hdrs2.get("ETag"), etag)
        # a stale validator must not be honored
        st, _, _ = self.call_h("GET", "/state", headers={"If-None-Match": '"stale"'})
        self.assertEqual(st, 200)
        # and the validator moves with the state
        self.call("POST", "/invalidate", {"reason": "etag check"})
        st, _, hdrs3 = self.call_h("GET", "/state", headers={"If-None-Match": etag})
        self.assertEqual(st, 200)
        self.assertNotEqual(hdrs3.get("ETag"), etag)


if __name__ == "__main__":
    unittest.main()
