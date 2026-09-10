"""The bench actuator as the tool sees it.

The fixture is a box on the network that opens the lid loop, pulls the
interlock loop and presses the button on request. What has to hold: the
tool finds it by name without a resolver on the image, speaks its API
under the key, falls back to the operator when it cannot act, moves an
operator test it can perform alone into the unattended queue (never a
live one, never one that needs a person for anything else), refuses a
prompt during such a run rather than hanging on it, releases what the
box still holds after every run, and presses the arm button only where
the bench opted in.
"""
import json
import os
import shutil
import socket
import struct
import tempfile
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler, HTTPServer

import helpers
from forgetest import fixture as fx
from forgetest import runner as runner_mod
from forgetest.log import Log
from forgetest.runner import Context, Failed, Run, Runner

KEY = "0123456789abcdef0123456789abcdef"


class FakeFixture:
    """The device's API, as fixture/README.md describes it."""

    def __init__(self, button_enabled=True):
        self.state = {"lid": "closed", "interlock": "closed", "button": "idle"}
        self.button_enabled = button_enabled
        self.calls = []
        self.presses = 0            # presses the device performed
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


class MdnsTests(unittest.TestCase):
    def test_query_packet(self):
        q = fx.mdns_query("forgefixture.local")
        self.assertEqual(q[:12], struct.pack("!HHHHHH", 0, 0, 1, 0, 0, 0))
        self.assertEqual(q[12:], b"\x0cforgefixture\x05local\x00" + struct.pack("!HH", 1, 0x8001))

    def _response(self, name, ip, extra_name=None):
        labels = b"".join(struct.pack("B", len(p)) + p.encode() for p in name.split(".")) + b"\x00"
        hdr = struct.pack("!HHHHHH", 0, 0x8400, 0, 2 if extra_name else 1, 0, 0)
        rr = labels + struct.pack("!HHIH", 1, 0x8001, 120, 4) + socket.inet_aton(ip)
        if extra_name:
            # a second answer naming the first through a compression pointer
            # to offset 12 (the first name), plus its own first label
            other = struct.pack("B", len(extra_name)) + extra_name.encode() + struct.pack("!H", 0xC000 | 12)
            rr += other + struct.pack("!HHIH", 1, 0x8001, 120, 4) + socket.inet_aton("10.0.0.9")
        return hdr + rr

    def test_answers_are_the_named_a_records(self):
        data = self._response("forgefixture.local", "192.0.2.50")
        self.assertEqual(fx.mdns_answers(data, "forgefixture.local"), ["192.0.2.50"])
        self.assertEqual(fx.mdns_answers(data, "FORGEFIXTURE.local."), ["192.0.2.50"])
        self.assertEqual(fx.mdns_answers(data, "other.local"), [])
        # a query (QR clear) is never an answer
        self.assertEqual(fx.mdns_answers(fx.mdns_query("forgefixture.local"), "forgefixture.local"), [])
        # compression pointers are followed; the other record is not ours
        data2 = self._response("forgefixture.local", "192.0.2.50", extra_name="printer")
        self.assertEqual(fx.mdns_answers(data2, "forgefixture.local"), ["192.0.2.50"])
        self.assertEqual(fx.mdns_answers(data2, "printer.forgefixture.local"), ["10.0.0.9"])
        # garbage does not raise
        self.assertEqual(fx.mdns_answers(b"\x00\x01", "forgefixture.local"), [])


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

    def client(self, **over):
        cfg = {"hostname": "forgefixture", "key": KEY, "channels": ["lid", "interlock", "button"],
               "port": self.dev.port}
        cfg.update(over)
        return fx.Fixture(cfg, resolver=self.resolver)

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

    def test_two_presses_are_spaced_so_the_controller_sees_the_release(self):
        f = self.client()
        f.status()
        t0 = time.time()
        f.act("button", "press")
        f.act("button", "press")
        took = time.time() - t0
        posts = [c for c in self.dev.calls if c[:2] == ("POST", "/button")]
        self.assertEqual(len(posts), 2)                             # no 409 round trip was needed
        self.assertEqual(self.dev.presses, 2)
        # the second waited for the first pulse (200 ms as reported) and the gap
        self.assertGreaterEqual(took, 0.2 + fx.BUTTON_GAP_S - 0.05)

    def test_a_pulse_in_progress_is_waited_out_then_retried(self):
        f = self.client()
        f.status()
        self.dev.state["button"] = "pressed"                        # a press this client did not time
        threading.Timer(0.3, lambda: self.dev.state.__setitem__("button", "idle")).start()
        f.act("button", "press")
        posts = [c for c in self.dev.calls if c[:2] == ("POST", "/button")]
        self.assertEqual(len(posts), 2)                             # the 409, then the press
        self.assertEqual(self.dev.presses, 1)

    def test_a_pulse_that_never_ends_is_the_fixtures_error(self):
        f = self.client()
        f.status()
        self.dev.state["button"] = "pressed"
        with self.assertRaises(fx.FixtureError) as cm:
            f.act("button", "press")
        self.assertIn("in progress", str(cm.exception))

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

    def test_probe_without_a_config_is_none_and_a_silent_box_is_logged(self):
        lines = []
        self.assertIsNone(fx.probe(lines.append, path=os.path.join(tempfile.gettempdir(), "no-such-fixture.json")))
        self.assertEqual(lines, [])
        tmp = tempfile.mkdtemp(prefix="forgetest-fixture-")
        try:
            p = os.path.join(tmp, "fixture.json")
            with open(p, "w") as f:
                json.dump({"key": KEY, "ip": "127.0.0.1", "port": 1}, f)
            # port 1 on localhost: nothing listens there
            f2 = fx.probe(lines.append, path=p, resolver=lambda h: None)
            self.assertIsNone(f2)
            self.assertTrue(lines and "running without it" in lines[-1], lines)
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

    def act(self, channel, state):
        if self.fail:
            raise fx.FixtureError("the box is off")
        self.acts.append((channel, state))
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

    def test_without_the_jumper_the_button_tests_stay_attended(self):
        self.stub.button_enabled = False
        av, _ = self.selection()
        self.assertIn("r.btn", av["attended"])
        self.assertIn("r.lid", av["unattended"])

    def test_no_fixture_is_the_old_routing(self):
        fx.probe = lambda log, path=None, resolver=None: None
        self.runner._fixture_probed = 0.0
        av, summary = self.selection()
        self.assertEqual(av["unattended"], ["r.auto"])
        self.assertIsNone(summary)

    def test_a_queue_started_during_a_probe_waits_for_the_fixture(self):
        # The daemon just came up: nothing probed yet, a page poll starts
        # the first probe (slow: an mDNS answer), and the queue start
        # lands while it is in flight. The start must see the fixture.
        stub = self.stub

        def slow_probe(log, path=None, resolver=None):
            time.sleep(0.5)
            return stub
        fx.probe = slow_probe
        self.runner._fixture_probed = 0.0
        self.runner.fixture = None
        poll = threading.Thread(target=self.runner.probe_fixture)
        poll.start()
        time.sleep(0.1)
        order = self.runner.batch_selection("unattended")
        poll.join()
        self.assertIn("r.lid", order)
        self.assertIn("r.btn", order)

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

    def test_a_failing_box_hands_the_action_to_the_operator(self):
        self.stub.fail = True
        run = Run("test", "r.lid", "r.lid")
        ctx = Context(run, self.runner, self.reg["r.lid"])
        self.runner.probe_fixture(force=True)
        seen = []
        ctx.wait_for = lambda cond, timeout, poll=0.25: 1.0       # the machine shows the state
        run.set_notice = lambda text: seen.append(text)
        ctx.act("lid", "open")
        rec = run.evidence["actions"][0]
        self.assertEqual(rec["by"], "operator")
        self.assertIn("the box is off", rec["fixture_error"])
        self.assertTrue(seen and "lid" in seen[0].lower())

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

    def test_presence_is_proved_once_per_test_not_once_per_gate(self):
        """A test with two armed halves reaches the ready gate twice. The
        second one must not ask for another press: the operator proved
        presence a moment earlier and the actuator has the presses. The
        setup line still goes up, because the second half may want the
        scrap moved."""
        run = Run("test", "r.live", "r.live")
        ctx = Context(run, self.runner, self.reg["r.live"])
        self.runner.probe_fixture(force=True)
        asked = []
        run.ask = lambda q, o: asked.append(q)
        self.press_button(False)
        threading.Timer(0.3, self.press_button, args=(True,)).start()
        threading.Timer(0.8, self.press_button, args=(False,)).start()
        ctx.ready("First half.")
        self.assertTrue(run.fixture_takeover)
        presses = [r for r in run.evidence["actions"] if r["state"] == "presence"]
        self.assertEqual(len(presses), 1)

        # the second gate: returns at once, no new press, notice still shown
        seen = []
        run.set_notice = lambda text: seen.append(text)
        t0 = time.time()
        ctx.ready("Second half. Move the scrap.")
        self.assertLess(time.time() - t0, 1.0)
        self.assertEqual(asked, [])
        self.assertEqual(len([r for r in run.evidence["actions"] if r["state"] == "presence"]), 1)
        self.assertIn("Second half. Move the scrap.", seen)
        self.assertTrue(any("presence already proved" in ln for ln in run.lines))

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

    def test_the_press_prompt_is_what_the_actuator_presses_on(self):
        """The machine's own press prompt, not a button LED read from
        here: the button is lit through parts of a live check that are
        not the arm, and a press then lands before the job waits for it
        and is lost. press_now presses at the moment the check asks, with
        no LED reading and no waiting thread."""
        run = Run("test", "r.live", "r.live")
        ctx = Context(run, self.runner, self.reg["r.live"])
        self.runner.probe_fixture(force=True)
        self.stub.arm_press = False
        run.fixture_takeover = True
        saved = runner_mod.hw.button_lit
        runner_mod.hw.button_lit = lambda: False   # never consulted
        try:
            self.assertTrue(ctx.press_now())
            self.assertIn(("button", "press"), self.stub.acts)
        finally:
            runner_mod.hw.button_lit = saved
        rec = [r for r in run.evidence["actions"] if r["state"] == "arm"][-1]
        self.assertEqual(rec["by"], "fixture")

    def test_the_press_prompt_goes_to_the_operator_without_an_actuator(self):
        """No actuator, or one that never took this test's presses: the
        notice goes up for a person, who is looking at the same prompt."""
        run = Run("test", "r.live", "r.live")
        ctx = Context(run, self.runner, self.reg["r.live"])
        self.runner.probe_fixture(force=True)
        self.stub.arm_press = False
        run.fixture_takeover = False
        seen = []
        run.set_notice = lambda text: seen.append(text)
        self.assertFalse(ctx.press_now("Press it now."))
        self.assertNotIn(("button", "press"), self.stub.acts)
        self.assertIn("Press it now.", seen)

    def test_the_arm_press_waits_as_long_as_the_card_says_for_the_light(self):
        """A card that settles the coolant before it lights the button
        takes minutes; the caller names the wait, and the actuator presses
        when the light comes instead of handing the press to a person at
        the default 60 s (found on the bench by the flow-load card)."""
        run = Run("test", "r.live", "r.live")
        ctx = Context(run, self.runner, self.reg["r.live"])
        self.runner.probe_fixture(force=True)
        run.fixture_takeover = True
        lit_at = time.time() + 0.6
        saved = runner_mod.hw.button_lit
        runner_mod.hw.button_lit = lambda: time.time() >= lit_at
        seen = []
        run.set_notice = lambda text: seen.append(text)
        try:
            self.assertTrue(ctx.arm_press(lit_timeout=5))
            deadline = time.time() + 5
            while ("button", "press") not in self.stub.acts and time.time() < deadline:
                time.sleep(0.05)
            self.assertIn(("button", "press"), self.stub.acts)
            self.assertEqual([n for n in seen if n], [])            # nobody was asked
            rec = run.evidence["actions"][-1]
            self.assertEqual(rec["by"], "fixture")
            self.assertGreaterEqual(rec["took_s"], 0.5)
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
        self.assertTrue(any("WARNING" in ln and "now gone" in ln for ln in run.lines))

    def test_ready_still_asks_the_page_with_no_actuator(self):
        run = Run("test", "r.live", "r.live")
        ctx = Context(run, self.runner, self.reg["r.live"])
        self.runner.fixture = None
        asked = []

        def ask(q, o):
            asked.append((q, list(o)))
            return "Ready"
        run.ask = ask
        ctx.ready("LIVE FIRE. Scrap under the head.")
        self.assertEqual(asked, [("LIVE FIRE. Scrap under the head.", ["Ready", "Cannot"])])
        self.assertFalse(run.fixture_takeover)

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


if __name__ == "__main__":
    unittest.main()
