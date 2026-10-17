# Copyright 2026 514 LLC d/b/a OpenGlow
# Written by Scott Wiederhold
# https://community.openglow.org
# SPDX-License-Identifier:    MIT

"""The bench actuator as the tool sees it.

The fixture is a box on the network that opens the lid loop, pulls the
interlock loop and presses the button on request. What has to hold: the
tool speaks its API under the key, falls back to the operator when it
cannot act, moves an operator test it can perform alone into the
unattended queue (never a live one, never one that needs a person for
anything else), refuses a prompt during such a run rather than hanging on
it, releases what the box still holds after every run, and presses the
arm button only where the bench opted in.
"""
import json
import os
import shutil
import tempfile
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler, HTTPServer

import helpers
from forgetest import fixture as fx
from forgetest import runner as runner_mod
from forgetest.log import Log
from forgetest.runner import Context, Run, Runner

KEY = "0123456789abcdef0123456789abcdef"


class FakeFixture:
    """The device's API, as fixture/README.md describes it."""

    def __init__(self, button_enabled=True):
        self.state = {"lid": "closed", "interlock": "closed", "button": "idle"}
        self.button_enabled = button_enabled
        self.calls = []
        self.presses = 0            # presses the device performed
        self.reply_delay = 0.0      # seconds between a press and its reply
        self.replied = 0            # delayed replies the device is done with (sent or lost)
        srv = self

        class H(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def _json(self, code, obj):
                body = json.dumps(obj).encode()
                self.send_response(code)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def _state(self):
                return {"device": "forgefixture", "hostname": "forgefixture", "version": "1.0.0",
                        "idf": "v5.5.5", "uptime_s": 12, "channels": dict(srv.state),
                        "button_enabled": srv.button_enabled,
                        "button_pulsing": srv.state["button"] == "pressed",
                        "wifi": {"connected": True, "ip": "127.0.0.1", "rssi": -50}}

            def _auth(self):
                if self.headers.get("X-Fixture-Key") != KEY:
                    self._json(401, {"error": "X-Fixture-Key missing or wrong"})
                    return False
                return True

            def do_GET(self):
                srv.calls.append(("GET", self.path))
                if not self._auth():
                    return
                self._json(200, self._state())

            def do_POST(self):
                n = int(self.headers.get("Content-Length") or 0)
                body = json.loads(self.rfile.read(n).decode() or "{}")
                srv.calls.append(("POST", self.path, body))
                if not self._auth():
                    return
                if self.path in ("/lid", "/interlock"):
                    st = body.get("state")
                    if st not in ("open", "close", "closed"):
                        return self._json(400, {"error": "state must be \"open\" or \"close\""})
                    srv.state[self.path[1:]] = "open" if st == "open" else "closed"
                    return self._json(200, self._state())
                if self.path == "/button":
                    if not srv.button_enabled:
                        return self._json(409, {"error": "button disabled: the enable jumper is out"})
                    if srv.state["button"] == "pressed":
                        return self._json(409, {"error": "a button pulse is in progress"})
                    srv.state["button"] = "pressed"
                    srv.presses += 1
                    threading.Timer(0.05, lambda: srv.state.__setitem__("button", "idle")).start()
                    s = self._state()
                    s["pulse_ms"] = 200
                    if srv.reply_delay:
                        time.sleep(srv.reply_delay)     # the reply comes after the client gave up
                        try:
                            self._json(200, s)
                        except OSError:
                            pass
                        srv.replied += 1
                        return None
                    return self._json(200, s)
                if self.path == "/release":
                    srv.state = {"lid": "closed", "interlock": "closed", "button": "idle"}
                    return self._json(200, self._state())
                self._json(404, {"error": "no such path"})

        self.httpd = HTTPServer(("127.0.0.1", 0), H)
        self.port = self.httpd.server_address[1]
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()

    def stop(self):
        self.httpd.shutdown()
        self.httpd.server_close()

    def wait_replied(self, n, timeout=5.0):
        deadline = time.time() + timeout
        while self.replied < n and time.time() < deadline:
            time.sleep(0.02)


class ConfigTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="forgetest-fixture-")

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def write(self, obj):
        p = os.path.join(self.tmp, "fixture.json")
        with open(p, "w") as f:
            json.dump(obj, f)
        return p

    def test_no_file_is_no_fixture(self):
        self.assertIsNone(fx.load_config(os.path.join(self.tmp, "none.json")))

    def test_defaults_and_checks(self):
        cfg = fx.load_config(self.write({"key": KEY}))
        self.assertEqual(cfg["hostname"], "forgefixture")
        self.assertEqual(cfg["channels"], ["lid", "interlock", "button"])
        self.assertFalse(cfg["arm_press"])
        self.assertIsNone(cfg["ip"])
        with self.assertRaises(fx.FixtureError):
            fx.load_config(self.write({"hostname": "x"}))                  # no key
        with self.assertRaises(fx.FixtureError):
            fx.load_config(self.write({"key": KEY, "channels": ["lid", "laser"]}))
        p = self.write({"key": KEY})
        with open(p, "w") as f:
            f.write("{not json")
        with self.assertRaises(fx.FixtureError):
            fx.load_config(p)


class ClientTests(unittest.TestCase):
    def setUp(self):
        self.dev = FakeFixture()
        self.resolved = []

    def tearDown(self):
        self.dev.stop()

    def resolver(self, hostname):
        self.resolved.append(hostname)
        return "127.0.0.1"

    def client(self, timeout=3.0, resolver=None, **over):
        cfg = {"hostname": "forgefixture", "key": KEY, "channels": ["lid", "interlock", "button"],
               "port": self.dev.port}
        cfg.update(over)
        return fx.Fixture(cfg, timeout=timeout, resolver=resolver or self.resolver)

    def test_status_covers_act_release(self):
        f = self.client()
        st = f.status()
        self.assertEqual(st["device"], "forgefixture")
        self.assertEqual(self.resolved, ["forgefixture"])            # resolved once
        self.assertTrue(f.covers("lid") and f.covers("interlock") and f.covers("button"))
        f.act("lid", "open")
        self.assertEqual(self.dev.state["lid"], "open")
        f.act("interlock", "open")
        f.act("button", "press")
        self.assertEqual(self.dev.calls[-1], ("POST", "/button", {}))
        time.sleep(0.15)
        # a pulse length asked for goes to the box as asked; the clamp is the box's
        f.act("button", "press", ms=500)
        self.assertEqual(self.dev.calls[-1], ("POST", "/button", {"ms": 500}))
        time.sleep(0.15)                                             # the fake's pulse ends
        self.assertEqual(sorted(fx.Fixture.energized(f.status())), ["interlock", "lid"])
        f.release()
        self.assertEqual(fx.Fixture.energized(f.status()), [])
        self.assertEqual(self.resolved, ["forgefixture"])            # the address is cached
        with self.assertRaises(fx.FixtureError):
            f.act("button", "hold")
        with self.assertRaises(fx.FixtureError):
            f.act("lid", "ajar")

    def test_the_button_is_covered_only_with_the_jumper_in(self):
        self.dev.button_enabled = False
        f = self.client()
        f.status()
        self.assertTrue(f.covers("lid"))
        self.assertFalse(f.covers("button"))
        with self.assertRaises(fx.FixtureError) as cm:
            f.act("button", "press")
        self.assertIn("jumper", str(cm.exception))

    def test_a_failure_after_the_request_went_out_says_so(self):
        # The press made and its reply lost (it comes after the client's
        # timeout): sent, one press. A refusal, a failed lookup and a
        # refused connect: not sent, no press. The one retry keeps it.
        self.dev.reply_delay = 0.6
        with self.assertRaises(fx.FixtureError) as cm:
            self.client(timeout=0.2, ip="127.0.0.1").act("button", "press")
        self.assertTrue(cm.exception.sent)
        self.dev.wait_replied(1)
        self.assertEqual(self.dev.presses, 1)
        answers = ["127.0.0.1"]                 # the refresh lookup after the timeout finds nothing
        with self.assertRaises(fx.FixtureError) as cm:
            self.client(timeout=0.2, resolver=lambda h: answers.pop(0) if answers else None).act("button", "press")
        self.assertTrue(cm.exception.sent)
        self.dev.wait_replied(2)
        self.assertEqual(self.dev.presses, 2)
        self.dev.reply_delay = 0.0
        self.dev.button_enabled = False
        with self.assertRaises(fx.FixtureError) as cm:
            self.client(ip="127.0.0.1").act("button", "press")
        self.assertFalse(cm.exception.sent)
        with self.assertRaises(fx.FixtureError) as cm:
            self.client(resolver=lambda h: None).act("button", "press")
        self.assertFalse(cm.exception.sent)
        with self.assertRaises(fx.FixtureError) as cm:
            self.client(timeout=0.5, ip="127.0.0.1", port=1).act("button", "press")
        self.assertFalse(cm.exception.sent)
        self.assertEqual(self.dev.presses, 2)

    def test_a_wrong_key_is_refused(self):
        f = self.client(key="wrong")
        with self.assertRaises(fx.FixtureError) as cm:
            f.status()
        self.assertIn("refused the key", str(cm.exception))

    def test_an_ip_override_skips_the_lookup(self):
        f = self.client(ip="127.0.0.1")
        f.status()
        self.assertEqual(self.resolved, [])

    def test_channels_not_wired_are_not_covered(self):
        f = self.client(channels=["lid"])
        f.status()
        self.assertTrue(f.covers("lid"))
        self.assertFalse(f.covers("interlock"))

    def test_probe_without_a_config_or_with_a_silent_box_is_none(self):
        lines = []
        self.assertIsNone(fx.probe(lines.append, path=os.path.join(tempfile.gettempdir(), "no-such-fixture.json")))
        tmp = tempfile.mkdtemp(prefix="forgetest-fixture-")
        try:
            p = os.path.join(tmp, "fixture.json")
            with open(p, "w") as f:
                json.dump({"key": KEY, "ip": "127.0.0.1", "port": 1}, f)
            # port 1 on localhost: nothing listens there
            f2 = fx.probe(lines.append, path=p, resolver=lambda h: None)
            self.assertIsNone(f2)
        finally:
            shutil.rmtree(tmp, ignore_errors=True)


class CatalogTests(unittest.TestCase):
    def test_fixture_runnable(self):
        op = helpers.make_test("m.op", [], kind="operator", actions=("lid", "button"))
        self.assertTrue(op.fixture_runnable(("lid", "interlock", "button")))
        self.assertFalse(op.fixture_runnable(("lid",)))              # the button is not covered
        self.assertFalse(helpers.make_test("m.live", [], kind="live", actions=("lid",))
                         .fixture_runnable(("lid",)))                 # live never downgrades
        self.assertFalse(helpers.make_test("m.app", [], kind="operator", hands=("app",))
                         .fixture_runnable(("lid", "interlock", "button")))
        self.assertFalse(helpers.make_test("m.none", [], kind="operator")
                         .fixture_runnable(("lid",)))                 # no actions: a person does something
        self.assertFalse(helpers.make_test("m.auto", []).fixture_runnable(("lid",)))

    def test_an_auto_test_asks_nothing_of_a_person(self):
        from forgetest import catalog
        with self.assertRaises(ValueError):
            catalog.test("z.auto", title="t", subsystem="z", hands=("app",))(lambda ctx: None)

    def test_every_action_a_test_takes_is_declared(self):
        # The queues route a test by its kind and its actions: an auto test
        # runs unattended with or without a fixture, and an operator test
        # whose actions the fixture covers runs unattended only while it
        # does. A test that acts on the lid, the interlock or the button
        # without saying so runs unattended with no fixture and waits for a
        # person who is not there.
        import inspect
        import re
        from forgetest import catalog
        bad = []
        for t in catalog.all_tests(catalog.load_suite()):
            src = inspect.getsource(t.fn)
            taken = set(re.findall(r'ctx\.act\(\s*"(\w+)"', src))
            if taken and (t.kind == "auto" or not taken <= set(t.actions)):
                bad.append("%s (%s) acts on %s, declares %s" % (t.id, t.kind, sorted(taken), list(t.actions)))
        self.assertEqual(bad, [])


class StubFixture:
    """What the runner needs of a fixture, scripted."""

    def __init__(self, channels=("lid", "interlock", "button"), button_enabled=True, arm_press=False,
                 fail=False, fc=None):
        self.fc = fc                    # the fake forgectrl whose switches follow the actions
        self.hostname = "forgefixture"
        self._ip = "127.0.0.1"
        self.channels = tuple(channels)
        self.button_enabled = button_enabled
        self.arm_press = arm_press
        self.fail = fail
        self.acts = []
        self.held = []
        self.released = 0

    def covers(self, channel):
        return channel in self.channels and (channel != "button" or self.button_enabled)

    def act(self, channel, state, ms=None):
        if self.fail:
            raise fx.FixtureError("the box is off")
        self.acts.append((channel, state) if ms is None else (channel, state, ms))
        if state == "open":
            self.held.append(channel)
        if self.fc is not None and channel in ("lid", "interlock"):
            sw = self.fc.state["status"]["switches"]
            sw["lid" if channel == "lid" else "interlock_ok"] = state != "open"

    def status(self):
        return {"channels": {c: ("open" if c in self.held else "closed") for c in ("lid", "interlock")}}

    @staticmethod
    def energized(state):
        return fx.Fixture.energized(state)

    def release(self):
        self.released += 1
        self.held = []

    def summary(self):
        return {"hostname": self.hostname, "ip": self._ip, "channels": list(self.channels),
                "button_enabled": self.button_enabled, "arm_press": self.arm_press}


def t_lid(ctx):
    ctx.ready("On Ready the lid opens")
    ctx.act("lid", "open")
    ctx.act("lid", "close")


def t_asks(ctx):
    ctx.act("lid", "open")
    ctx.confirm("Did it?")


class RoutingTests(unittest.TestCase):
    """The queues with a fixture up: the runner's probe is scripted."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="forgetest-fixture-run-")
        os.environ["FORGETEST_DATA"] = self.tmp
        os.environ["FORGETEST_MARKER"] = os.path.join(self.tmp, "marker")
        self.fc = helpers.FakeForgectrl().start()
        self.man = helpers.make_manifest()
        self.reg = helpers.registry(
            helpers.make_test("r.auto", [("forgectrl", "src/ui.c")]),
            helpers.make_test("r.lid", [("forgectrl", "src/auth.c")], kind="operator", actions=("lid",), fn=t_lid),
            helpers.make_test("r.btn", [("forgectrl", "src/cool.c")], kind="operator", actions=("button",), fn=t_lid),
            helpers.make_test("r.app", [("forgectrl", "src/main.c")], kind="operator", actions=("lid",),
                              hands=("app",), fn=t_lid),
            helpers.make_test("r.live", [("grblhal-glowforge", "src/**")], kind="live", actions=("lid",)),
            helpers.make_test("r.asks", [("linux-fslc", "**")], kind="operator", actions=("lid",), fn=t_asks),
        )
        self.log = Log(os.path.join(self.tmp, "results.jsonl"))
        self.runner = Runner(self.log, self.man, self.reg)
        self.saved_probe = fx.probe
        self.stub = StubFixture(fc=self.fc)
        fx.probe = lambda log, path=None, resolver=None: self.stub

    def tearDown(self):
        fx.probe = self.saved_probe
        self.fc.stop()
        for k in ("FORGETEST_DATA", "FORGETEST_MARKER"):
            os.environ.pop(k, None)
        shutil.rmtree(self.tmp, ignore_errors=True)

    def selection(self):
        st, _ = self.runner.state()
        return st["batch_available"], st["fixture"]

    def test_operator_tests_the_fixture_covers_move_to_the_unattended_queue(self):
        av, summary = self.selection()
        self.assertEqual(av["unattended"], ["r.auto", "r.lid", "r.btn", "r.asks"])
        self.assertEqual(av["attended"], ["r.app", "r.live"])       # needs the app; live never moves
        self.assertEqual(summary["hostname"], "forgefixture")

    def run_queue(self, group, ack_live=False):
        ok, msg, order = self.runner.start_batch(group, ack_live=ack_live)
        self.assertTrue(ok, msg)
        deadline = time.time() + 30
        while not self.runner.batch["finished"] and time.time() < deadline:
            time.sleep(0.05)
        return self.runner.batch_snapshot(), order

    def last_result(self, tid):
        for rec in reversed(self.log.read()):
            if rec.get("t") == "result" and rec["test"] == tid:
                return rec
        return None

    def test_the_fixture_performs_the_actions_and_the_ready_gate_passes(self):
        # r.asks confirms after its action: a person is asked -> a FAIL that
        # says so, and the queue stops there, after r.lid and r.btn passed
        b, order = self.run_queue("unattended")
        self.assertEqual(order, ["r.auto", "r.lid", "r.btn", "r.asks"])
        results = {x["test"]: x["result"] for x in b["done"]}
        self.assertEqual(results["r.lid"], "PASS")
        self.assertEqual(results["r.btn"], "PASS")
        self.assertEqual(results["r.asks"], "FAIL")
        rec = self.last_result("r.lid")
        acts = rec["evidence"]["actions"]
        self.assertEqual([(a["channel"], a["state"], a["by"]) for a in acts],
                         [("lid", "open", "fixture"), ("lid", "close", "fixture")])
        self.assertTrue(any("READY (fixture performs the step)" in l for l in rec["log"]), rec["log"])
        self.assertEqual(rec["evidence"]["fixture"]["channels"], ["lid", "interlock", "button"])
        asks = self.last_result("r.asks")
        self.assertIn("declare the step in hands=", asks["message"])

    def test_a_failing_box_in_an_unattended_run_ends_the_test_as_an_error(self):
        # nobody is in the room: the run must not wait ACT_TIMEOUT_S for a
        # hand that is not there; it ends at once, as the harness's error
        self.stub.fail = True
        t0 = time.time()
        b, order = self.run_queue("unattended")
        results = {x["test"]: x["result"] for x in b["done"]}
        self.assertEqual(results["r.lid"], "ERROR")
        self.assertLess(time.time() - t0, 20)
        rec = self.last_result("r.lid")
        self.assertIn("the fixture could not perform a step", rec["message"])
        self.assertIn("the box is off", rec["message"])
        act = rec["evidence"]["actions"][0]
        self.assertEqual((act["by"], act["fixture_error"]), ("fixture", "the box is off"))
        self.assertFalse(any("asking the operator" in l for l in rec["log"]), rec["log"])

    def test_what_the_box_still_holds_is_released_after_a_run(self):
        def holds(ctx):
            ctx.act("lid", "open")              # and never closes it
        self.reg["r.hold"] = helpers.make_test("r.hold", [("forgectrl", "src/ui.c")], kind="operator",
                                               actions=("lid",), fn=holds)
        self.runner.registry = self.reg
        ok, msg, run = self.runner._start_test("r.hold")
        self.assertTrue(ok, msg)
        while run.finished is None:
            time.sleep(0.05)
        self.assertEqual(self.stub.released, 1)
        rec = self.last_result("r.hold")
        self.assertEqual(rec["evidence"]["fixture"]["released"], ["lid"])

    def press_button(self, down):
        self.fc.state["status"]["switches"]["button"] = bool(down)

    def test_ready_takes_a_press_on_the_machine_as_the_presence_check(self):
        """With an actuator wired to the button, the operator proves they
        are at the machine by pressing it, not by clicking the page. The
        actuator then owns every press in the test, so no press in a live
        cut is a person's and none can go uncounted."""
        run = Run("test", "r.live", "r.live")
        ctx = Context(run, self.runner, self.reg["r.live"])
        self.runner.probe_fixture(force=True)
        asked = []
        run.ask = lambda q, o: asked.append(q)
        self.press_button(False)
        threading.Timer(0.3, self.press_button, args=(True,)).start()
        threading.Timer(0.8, self.press_button, args=(False,)).start()
        ctx.ready("LIVE FIRE. Scrap under the head.")
        self.assertEqual(asked, [])                      # no page click asked for
        self.assertTrue(run.fixture_takeover)
        rec = [r for r in run.evidence["actions"] if r["state"] == "presence"]
        self.assertEqual(len(rec), 1)
        self.assertEqual(rec[0]["by"], "operator")
        self.assertTrue(any("press the button on the machine" in ln.lower() for ln in run.lines))

    def test_the_presence_press_hands_the_arm_press_to_the_actuator(self):
        """The bench's standing opt-in is not needed once the operator has
        proved presence: that press is what the opt-in existed to
        establish."""
        run = Run("test", "r.live", "r.live")
        ctx = Context(run, self.runner, self.reg["r.live"])
        self.runner.probe_fixture(force=True)
        self.stub.arm_press = False
        run.fixture_takeover = True
        saved = runner_mod.hw.button_lit
        runner_mod.hw.button_lit = lambda: True
        try:
            self.assertTrue(ctx.arm_press())
            deadline = time.time() + 5
            while ("button", "press") not in self.stub.acts and time.time() < deadline:
                time.sleep(0.05)
            self.assertIn(("button", "press"), self.stub.acts)
        finally:
            runner_mod.hw.button_lit = saved

    def test_an_actuator_lost_after_the_takeover_is_said_out_loud(self):
        """Falling back to the operator without a word is how one dropped
        actuator becomes a press nobody can account for afterwards."""
        run = Run("test", "r.live", "r.live")
        ctx = Context(run, self.runner, self.reg["r.live"])
        self.runner.probe_fixture(force=True)
        run.fixture_takeover = True
        self.runner.fixture = None                       # the box drops off mid-test
        self.fc.state["status"]["switches"]["lid"] = True
        threading.Timer(0.3, lambda: self.fc.state["status"]["switches"].__setitem__("lid", False)).start()
        ctx.act("lid", "open", timeout=5)
        rec = run.evidence["actions"][-1]
        self.assertTrue(rec.get("fixture_lost"))
        self.assertEqual(rec["by"], "operator")

    def test_the_arm_press_is_the_operators_unless_the_bench_opted_in(self):
        run = Run("test", "r.live", "r.live")
        ctx = Context(run, self.runner, self.reg["r.live"])
        self.runner.probe_fixture(force=True)
        seen = []
        run.set_notice = lambda text: seen.append(text)
        self.assertFalse(ctx.arm_press())
        self.assertEqual(run.evidence["actions"][0]["by"], "operator")
        self.assertTrue(seen)
        # opted in: the press waits for the button to light
        self.stub.arm_press = True
        lit = {"v": False}
        saved = runner_mod.hw.button_lit
        runner_mod.hw.button_lit = lambda: lit["v"]
        try:
            seen[:] = []
            self.assertTrue(ctx.arm_press())
            time.sleep(0.3)
            self.assertEqual(self.stub.acts, [])                    # not yet: dark button
            lit["v"] = True
            deadline = time.time() + 5
            while not self.stub.acts and time.time() < deadline:
                time.sleep(0.05)
            self.assertEqual(self.stub.acts, [("button", "press")])
            self.assertEqual(run.evidence["actions"][1]["by"], "fixture")
            self.assertEqual(seen, [])                              # no notice went up
        finally:
            runner_mod.hw.button_lit = saved

    @unittest.skipUnless(hasattr(os, "mkfifo"), "needs a FIFO for the switches' event device")
    def test_ready_reads_a_short_press_as_an_edge(self):
        """The presence press is the switches' input events, so a press
        far shorter than a quarter second counts: the press and release
        land back to back, and the status the old poll read never shows
        the button down at all."""
        dev = os.path.join(self.tmp, "switches")
        os.mkfifo(dev)
        os.environ["GF_SWITCH_DEV"] = dev
        ev = runner_mod.hw.ButtonEdges.EVENT

        def press():
            with open(dev, "wb") as w:
                w.write(ev.pack(0, 0, runner_mod.hw.EV_SW, runner_mod.hw.SW_BUTTON, 1) +
                        ev.pack(0, 0, 0, 0, 0) +
                        ev.pack(0, 0, runner_mod.hw.EV_SW, runner_mod.hw.SW_BUTTON, 0))
        try:
            run = Run("test", "r.live", "r.live")
            ctx = Context(run, self.runner, self.reg["r.live"])
            self.runner.probe_fixture(force=True)
            self.press_button(False)                     # the status never shows it down
            threading.Timer(0.3, press).start()
            t0 = time.time()
            ctx.ready("LIVE FIRE. Scrap under the head.")
            self.assertLess(time.time() - t0, 5)
            self.assertTrue(run.fixture_takeover)
            rec = [r for r in run.evidence["actions"] if r["state"] == "presence"]
            self.assertEqual(len(rec), 1)
            self.assertIsNone(run.notice)                # the notice is down with the press
        finally:
            os.environ.pop("GF_SWITCH_DEV", None)

    def test_the_arm_notice_comes_down_when_the_press_is_taken(self):
        """The controller puts the button out at the press: the operator's
        arm notice goes down then, not at the test's next step, so nobody
        presses again (a press during the job pauses it)."""
        run = Run("test", "r.live", "r.live")
        ctx = Context(run, self.runner, self.reg["r.live"])
        self.runner.probe_fixture(force=True)
        lit = {"v": False}
        saved = runner_mod.hw.button_lit
        runner_mod.hw.button_lit = lambda: lit["v"]
        try:
            self.assertFalse(ctx.arm_press())            # the operator's press
            self.assertIsNotNone(run.notice)
            time.sleep(0.2)
            self.assertIsNotNone(run.notice)             # dark before the arm: still asked for
            lit["v"] = True
            time.sleep(0.2)
            self.assertIsNotNone(run.notice)             # lit: the arm waits for the press
            lit["v"] = False                             # the press taken
            deadline = time.time() + 2
            while run.notice is not None and time.time() < deadline:
                time.sleep(0.02)
            self.assertIsNone(run.notice)
            # A notice put up meanwhile is not the arm's: it stays.
            lit["v"] = False
            self.assertFalse(ctx.arm_press())
            ctx.notice("Something else.")
            lit["v"] = True
            time.sleep(0.2)
            lit["v"] = False
            time.sleep(0.3)
            self.assertEqual((run.notice or {}).get("text"), "Something else.")
            # The watch ends with the run: a test that ends before the press
            # (a FAIL on the way to the arm, say) leaves no thread polling the
            # LEDs for up to the arm's whole timeout.
            def watching():
                return [t for t in threading.enumerate() if t.name == "forgetest-arm-notice"]
            deadline = time.time() + 2
            while watching() and time.time() < deadline:
                time.sleep(0.02)                         # the earlier watches are gone
            self.assertFalse(watching())
            lit["v"] = False
            self.assertFalse(ctx.arm_press())
            self.assertTrue(watching())                  # up while the arm is still dark
            run.finished = {"result": "FAIL", "message": "", "duration_s": 0}
            deadline = time.time() + 2
            while watching() and time.time() < deadline:
                time.sleep(0.02)
            self.assertFalse(watching())
            self.assertIsNotNone(run.notice)             # the finished run's notice is left as it was
            # A press the fixture could not make is asked of the operator, and
            # that notice comes down at the press like any other: press_now and
            # arm_press with the box refusing, and arm_press with the button
            # never lit for the box.
            self.stub.arm_press = True
            self.stub.fail = True
            run = Run("test", "r.live", "r.live")
            ctx = Context(run, self.runner, self.reg["r.live"])

            def settles(up):
                deadline = time.time() + 2
                while (run.notice is not None) != up and time.time() < deadline:
                    time.sleep(0.02)
                return (run.notice is not None) == up
            lit["v"] = True                              # the machine waits for the press
            self.assertFalse(ctx.press_now())            # the box is off: the operator is asked
            self.assertIsNotNone(run.notice)
            time.sleep(0.2)                              # lit: the arm waits for the press
            lit["v"] = False                             # the operator's press is taken
            self.assertTrue(settles(False))
            lit["v"] = True
            self.assertTrue(ctx.arm_press())             # the fixture is asked, and refuses
            self.assertTrue(settles(True))
            time.sleep(0.2)
            lit["v"] = False
            self.assertTrue(settles(False))
            self.assertTrue(ctx.arm_press(lit_timeout=0))  # the button never lit for the fixture
            self.assertTrue(settles(True))
            lit["v"] = True
            time.sleep(0.2)
            lit["v"] = False
            self.assertTrue(settles(False))
            # A press the box made and reported failed (its reply lost after
            # the press): the button is out when the error arrives, so the
            # press was taken, and no notice asks for a second one.
            def pressed_then_failed(channel, state, ms=None):
                lit["v"] = False                         # the controller takes the press
                raise fx.FixtureError("timed out", sent=True)
            self.stub.act = pressed_then_failed
            lit["v"] = True
            self.assertTrue(ctx.press_now())
            self.assertIsNone(run.notice)
            self.assertEqual(ctx.evidence["actions"][-1]["by"], "fixture")
            lit["v"] = True
            self.assertTrue(ctx.arm_press())
            deadline = time.time() + 2
            while ctx.evidence["actions"][-1]["by"] != "fixture" and time.time() < deadline:
                time.sleep(0.02)
            time.sleep(0.1)
            self.assertEqual(ctx.evidence["actions"][-1]["by"], "fixture")
            self.assertIsNone(run.notice)
            # A refusal (or a request that never reached the box) while the
            # button went out: a press at the machine was taken, so no notice
            # goes up either, and the record does not give it to the box.
            def refused_while_pressed(channel, state, ms=None):
                lit["v"] = False                         # a person's press is taken
                raise fx.FixtureError("button press -> 409 button disabled: the enable jumper is out")
            self.stub.act = refused_while_pressed
            lit["v"] = True
            self.assertFalse(ctx.press_now())
            self.assertIsNone(run.notice)
            self.assertEqual(ctx.evidence["actions"][-1]["by"], "operator")
            lit["v"] = True
            self.assertTrue(ctx.arm_press())
            deadline = time.time() + 2
            while "fixture_error" not in ctx.evidence["actions"][-1] and time.time() < deadline:
                time.sleep(0.02)
            time.sleep(0.1)
            self.assertEqual(ctx.evidence["actions"][-1]["by"], "operator")
            self.assertIsNone(run.notice)
        finally:
            runner_mod.hw.button_lit = saved


if __name__ == "__main__":
    unittest.main()
