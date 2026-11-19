# Copyright 2026 514 LLC d/b/a OpenGlow
# Written by Scott Wiederhold
# https://community.openglow.org
# SPDX-License-Identifier:    MIT

"""The bench actuator as the tool sees it.

The fixture is a box on the network that opens the lid loop, pulls the
interlock loop and presses the button on request. What has to hold: the
tool counts the button covered only with the box's enable jumper in, never
takes a press it may have made for one it did not, never makes a press a
second time when the answer to the first was lost, signs every request
with a nonce of the box's and never sends the key, falls back
to the operator when it cannot act, moves an operator test it can perform
alone into the unattended queue (never a live one, never one that needs a
person for anything else), refuses a prompt during such a run rather than
hanging on it, releases what the box still holds after every run, presses
the arm button only where the bench opted in, and never makes a page poll
wait on its probe of the box.

The press waits here wait for what the runner's watch has read of the
button (Lamp), never for a fixed time a loaded host can outrun.
"""
import collections
import hashlib
import hmac
import json
import os
import re
import secrets
import shutil
import tempfile
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import helpers
from forgetest import fixture as fx
from forgetest import runner as runner_mod
from forgetest.log import Log
from forgetest.runner import Context, Failed, Run, Runner

KEY = "0123456789abcdef0123456789abcdef"


class FakeFixture:
    """The device's API, as fixture/README.md describes it."""

    def __init__(self, button_enabled=True, host="127.0.0.1", port=0):
        self.state = {"lid": "closed", "interlock": "closed", "button": "idle"}
        self.button_enabled = button_enabled
        self.calls = []
        self.nonces = set()         # issued and not yet presented
        self.headers = []           # every request's headers, as the box received them
        self.presses = 0            # presses the device performed
        self.reply_delay = 0.0      # seconds between a press and its reply
        self.replied = 0            # delayed replies the device is done with (sent or lost)
        self.raw = None             # (declared length, bytes): what any request answers instead
        self.press_status = None    # the status a press answers after the box made it (500: no reply built)
        self.busy_once = False      # the next press finds a pulse in progress (another client's)
        self.nonce_life = 10        # the expires_s a nonce's answer states
        self.polls = []             # (declared length, bytes): what the next reads of the state answer instead
        self.hang_up = ()           # the paths whose request is taken and never answered
        self.late_once = {}         # path: seconds the next loop request on it is answered late, once
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

            def _auth(self, body=b""):
                """A nonce this box issued, spent here, and the request's
                HMAC under the key (fixture/main/auth.h)."""
                srv.headers.append(dict(self.headers))
                nonce = self.headers.get("X-Fixture-Nonce") or ""
                if nonce not in srv.nonces:
                    self._json(401, {"error": "not authorized: nonce unknown, spent or expired"})
                    return False
                srv.nonces.discard(nonce)
                msg = ("%s\n%s\n%s\n" % (self.command, self.path, nonce)).encode() + body
                want = hmac.new(KEY.encode(), msg, hashlib.sha256).hexdigest()
                if not hmac.compare_digest(self.headers.get("X-Fixture-Mac") or "", want):
                    self._json(401, {"error": "not authorized: wrong MAC"})
                    return False
                return True

            def _raw(self, instead=None):
                """srv.raw (or `instead`) in place of the answer: True when
                sent."""
                if instead is None and srv.raw is None:
                    return False
                length, data = instead or srv.raw
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(length))
                self.end_headers()
                self.wfile.write(data)
                return True

            def do_GET(self):
                if self.path == "/nonce":
                    nonce = secrets.token_hex(16)
                    srv.nonces.add(nonce)
                    return self._json(200, {"nonce": nonce, "expires_s": srv.nonce_life})
                srv.calls.append(("GET", self.path))
                if not self._auth() or (srv.polls and self._raw(srv.polls.pop(0))) or self._raw():
                    return
                self._json(200, self._state())

            def do_POST(self):
                n = int(self.headers.get("Content-Length") or 0)
                raw = self.rfile.read(n)
                body = json.loads(raw.decode() or "{}")
                srv.calls.append(("POST", self.path, body))
                if not self._auth(raw) or self.path in srv.hang_up or self._raw():
                    return
                if self.path in ("/lid", "/interlock"):
                    st = body.get("state")
                    if st not in ("open", "close", "closed"):
                        return self._json(400, {"error": "state must be \"open\" or \"close\""})
                    srv.state[self.path[1:]] = "open" if st == "open" else "closed"
                    late = srv.late_once.pop(self.path, 0)
                    if late:
                        time.sleep(late)                # the reply comes after the client gave up
                        try:
                            self._json(200, self._state())
                        except OSError:
                            pass
                        srv.replied += 1
                        return None
                    return self._json(200, self._state())
                if self.path == "/button":
                    if not srv.button_enabled:
                        return self._json(409, {"error": "button disabled: the enable jumper is out"})
                    if srv.state["button"] == "pressed" or srv.busy_once:
                        srv.busy_once = False
                        return self._json(409, {"error": "a button pulse is in progress"})
                    srv.state["button"] = "pressed"
                    srv.presses += 1
                    threading.Timer(0.05, lambda: srv.state.__setitem__("button", "idle")).start()
                    if srv.press_status:
                        return self._json(srv.press_status, {})
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

        # One thread a request: a client that gave up on a slow answer
        # reaches the box again at once, as it would the real one.
        self.httpd = ThreadingHTTPServer((host, port), H)
        self.port = self.httpd.server_address[1]
        # a short poll: stop() waits out one, at every test's end
        threading.Thread(target=self.httpd.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True).start()

    def stop(self):
        self.httpd.shutdown()
        self.httpd.server_close()

    def wait_replied(self, n, timeout=5.0):
        deadline = time.monotonic() + timeout
        while self.replied < n and time.monotonic() < deadline:
            time.sleep(0.02)


def until(cond, timeout=5.0):
    """True as soon as cond() holds, False when it has not within timeout
    (the bound is only for a hang)."""
    deadline = time.monotonic() + timeout
    while not cond():
        if time.monotonic() > deadline:
            return False
        time.sleep(0.01)
    return True


def threads(name):
    return [t for t in threading.enumerate() if t.name == name]


class WallStep:
    """The time module as the fixture client sees it, with the wall clock
    stepped by wall_step and a sleep that only advances the monotonic
    clock (and records how long it was asked to be)."""

    def __init__(self):
        self.wall_step = 0.0
        self.slept = []

    def time(self):
        return time.time() + self.wall_step

    def monotonic(self):
        return time.monotonic() + sum(self.slept)

    def sleep(self, seconds):
        self.slept.append(seconds)


class ClockEvent:
    """A run's Abort on a fake clock: a wait moves the clock on and never
    blocks; set from `at` on the clock (None: never)."""

    def __init__(self, clock, at=None):
        self.clock, self.at = clock, at

    def is_set(self):
        return self.at is not None and self.clock.monotonic() >= self.at

    def wait(self, timeout=None):
        if not self.is_set():
            self.clock.sleep(timeout or 0.0)
        return self.is_set()


def forget_unanswered():
    """No request of an earlier case is still counted as one the box may
    serve (the client keeps those for the process)."""
    fx._unanswered.update(press=0.0, loop=0.0)


def firmware_define(header, name):
    """The integer a #define of the fixture firmware's header gives."""
    path = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))),
                        "fixture", "main", header)
    with open(path, encoding="utf-8") as f:
        found = re.search(r"^#define\s+%s\s+(\d+)" % name, f.read(), re.M)
    return int(found.group(1)) if found else None


WATCH = "forgetest-arm-notice"         # the runner's watch that takes an arm notice down
PRESS = "forgetest-arm-press"          # the runner's thread that has the fixture press


class Lamp:
    """The button as hw.button_asks reads it (True: lit, False: dark), with
    a count of the reads of each state by each runner thread: a test waits
    until a thread has read a state, never for a fixed time."""

    def __init__(self):
        self.lit = False
        self.reads = collections.Counter()      # (thread name, state): reads
        self.lock = threading.Lock()

    def __call__(self):
        with self.lock:
            self.reads[(threading.current_thread().name, self.lit)] += 1
            return self.lit

    def set(self, lit, who=WATCH):
        """Light or darken the button; a mark for seen(): the reads of the
        new state by the thread named `who` so far."""
        with self.lock:
            self.lit = lit
            return (who, lit, self.reads[(who, lit)])

    def seen(self, mark, n=1):
        """True once the thread has read the state n more times since mark."""
        who, lit, k = mark
        return until(lambda: self.reads[(who, lit)] >= k + n)


class ConfigTests(unittest.TestCase):
    def test_the_box_makes_no_arm_press_unless_the_bench_opted_in(self):
        # An arm press is the consent to fire: a config that never names
        # arm_press leaves it to the operator.
        tmp = tempfile.mkdtemp(prefix="forgetest-fixture-")
        self.addCleanup(shutil.rmtree, tmp, ignore_errors=True)
        path = os.path.join(tmp, "fixture.json")
        with open(path, "w") as f:
            json.dump({"key": KEY}, f)
        self.assertIs(fx.load_config(path)["arm_press"], False)


class ClientTests(unittest.TestCase):
    def setUp(self):
        self.dev = FakeFixture()
        self.resolved = []

    def tearDown(self):
        self.dev.stop()
        forget_unanswered()

    def resolver(self, hostname):
        self.resolved.append(hostname)
        return "127.0.0.1"

    def fake_clock(self):
        """The client's clock for the rest of the case: its sleeps only
        move it on."""
        clock = WallStep()
        saved = fx.time
        fx.time = clock
        self.addCleanup(setattr, fx, "time", saved)
        return clock

    def client(self, timeout=3.0, resolver=None, **over):
        cfg = {"hostname": "forgefixture", "key": KEY, "channels": ["lid", "interlock", "button"],
               "port": self.dev.port}
        cfg.update(over)
        return fx.Fixture(cfg, timeout=timeout, resolver=resolver or self.resolver)

    def test_the_button_is_covered_only_with_the_jumper_in(self):
        self.dev.button_enabled = False
        f = self.client()
        f.status()
        self.assertTrue(f.covers("lid"))
        self.assertFalse(f.covers("button"))
        with self.assertRaises(fx.FixtureError):
            f.act("button", "press")

    def test_a_press_whose_answer_cannot_be_read_was_sent(self):
        # cut short (a wifi drop mid-reply), not JSON, not a JSON object:
        # the press went out, so each is the box's failure with sent set,
        # which the press paths read as a press the box may have made
        clock = self.fake_clock()
        for length, data in ((100, b'{"device": "forge'), (8, b"not json"), (2, b"[]")):
            with self.subTest(data=data):
                self.dev.raw = (length, data)
                with self.assertRaises(fx.FixtureError) as cm:
                    self.client(ip="127.0.0.1").act("button", "press")
                self.assertTrue(cm.exception.sent)
        # Each of the two presses that followed such a press came from a new
        # client, and still waited until the box could no longer make the
        # one before it (its nonce's life, a pulse and the gap): the time is
        # the process's, since a probe replaces the runner's client.
        self.assertAlmostEqual(sum(clock.slept), 2 * (fx.NONCE_LIFE_S + fx.PULSE_MAX_S + fx.BUTTON_GAP_S), delta=0.5)
        # The box pressed and then could not build its reply (a 500): any
        # answer but 200 and the refusals it makes before it presses.
        self.dev.raw = None
        for made, status in enumerate((500, 503, 404), 1):
            with self.subTest(status=status):
                self.dev.press_status = status
                with self.assertRaises(fx.FixtureError) as cm:
                    self.client(ip="127.0.0.1").act("button", "press")
                self.assertEqual((cm.exception.sent, self.dev.presses), (True, made))
                self.dev.state["button"] = "idle"            # the box's pulse is over at once
        # A press refused for a pulse in progress, and a poll of the state
        # in the wait for it whose answer cannot be read: no press went out
        # with a poll, so this is no press the box may have made. The press
        # goes on to its second request.
        self.dev.press_status = None
        self.dev.busy_once, self.dev.polls = True, [(8, b"not json")]
        try:
            self.client(ip="127.0.0.1").act("button", "press")
        except fx.FixtureError as e:
            self.fail("a failed poll ended the press (sent=%s): %s" % (e.sent, e))
        self.assertEqual((self.dev.presses, self.dev.polls), (4, []))

    def test_the_clients_times_are_the_boxs(self):
        # Every wait for a request the box may still serve counts the nonce's
        # life and the longest pulse: the client's two numbers are the
        # firmware's, and a box whose nonce lives longer than the client
        # counts is not used (nothing is sent on such a nonce).
        self.assertEqual(firmware_define("auth.h", "AUTH_NONCE_LIFE_US"), fx.NONCE_LIFE_S * 1e6)
        self.assertEqual(firmware_define("policy.h", "BUTTON_MAX_MS"), fx.PULSE_MAX_S * 1e3)
        for life in (11, 10.5):                 # the box states a whole number of seconds
            with self.subTest(life=life):
                self.dev.nonce_life = life
                with self.assertRaises(fx.FixtureError) as cm:
                    self.client(ip="127.0.0.1").act("button", "press")
                self.assertEqual((cm.exception.sent, self.dev.calls), (False, []))

    def test_a_loop_request_whose_answer_was_lost_is_waited_out_before_the_next(self):
        # The box takes a request until its nonce's life is over. A close or
        # a release that went out and was never answered may be held up and
        # served late: the next loop request, from any client of this
        # process, goes out only once it no longer can be, or it would close
        # a loop the next one opened.
        clock = self.fake_clock()
        for name, lost, then in (
                ("a close, then the release", lambda f: f.act("lid", "close"), lambda f: f.release()),
                ("the release, then an open", lambda f: f.release(), lambda f: f.act("interlock", "open"))):
            with self.subTest(name):
                self.dev.hang_up = ("/lid", "/release")
                f = self.client(ip="127.0.0.1")
                with self.assertRaises(fx.FixtureError) as cm:
                    lost(f)
                self.assertTrue(cm.exception.sent)
                self.assertAlmostEqual(fx.unanswered_left("loop"), fx.NONCE_LIFE_S - f.nonce_age(), delta=0.05)
                self.dev.hang_up = ()
                before = sum(clock.slept)
                then(self.client(ip="127.0.0.1"))
                self.assertAlmostEqual(sum(clock.slept) - before, fx.NONCE_LIFE_S, delta=0.5)
                self.assertEqual(fx.unanswered_left("loop"), 0.0)
        # A resend that is answered does not hide a first copy still held
        # up: with no ip set the client sends a lost loop request again by
        # itself, and the first copy can still be served for its nonce's life.
        self.dev.late_once = {"/interlock": 0.5}
        f = self.client(timeout=0.2)
        f.act("interlock", "close")
        self.assertEqual(len([c for c in self.dev.calls if c[:2] == ("POST", "/interlock")]), 3)
        self.assertGreater(fx.unanswered_left("loop"), fx.NONCE_LIFE_S - 1.0)
        self.dev.wait_replied(1)

    def test_a_failure_after_the_request_went_out_says_so(self):
        # The press made and its reply lost (it comes after the client's
        # timeout): sent, one press. With the box found again at once (a
        # resolver that answers the refresh), the press is still not made a
        # second time. A refusal, a failed lookup and a refused connect:
        # not sent, no press.
        self.dev.reply_delay = 0.6
        f = self.client(timeout=0.2, ip="127.0.0.1")
        t0 = time.monotonic()
        with self.assertRaises(fx.FixtureError) as cm:
            f.act("button", "press")
        self.assertTrue(cm.exception.sent)
        # Timed as the box can still make it: from the nonce in hand (after
        # the press was asked), its life, then the longest pulse. The same
        # time is kept for the process, past this client.
        self.assertLessEqual(f.nonce_age(), time.monotonic() - t0)
        self.assertAlmostEqual(f.pulse_left(), fx.NONCE_LIFE_S + fx.PULSE_MAX_S - f.nonce_age(), delta=0.05)
        self.assertAlmostEqual(fx.unanswered_left("press"), f.pulse_left(), delta=0.05)
        self.dev.wait_replied(1)
        self.assertEqual(self.dev.presses, 1)
        forget_unanswered()                     # the next press here does not wait that press out
        with self.assertRaises(fx.FixtureError) as cm:
            self.client(timeout=0.2).act("button", "press")
        self.assertTrue(cm.exception.sent)
        forget_unanswered()
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

    def test_a_press_is_judged_right_before_its_request_and_again_after_a_fresh_lookup(self):
        # After the spacing and the nonce: a judgment that refuses sends
        # nothing, and the nonce it was given is never presented.
        f = self.client(ip="127.0.0.1")
        seen = []

        def refuses():
            seen.append(len(self.dev.nonces))
            return "the kernel stopped playing"
        with self.assertRaises(fx.PressWithheld) as cm:
            f.act("button", "press", judge=refuses)
        self.assertFalse(cm.exception.sent)
        self.assertEqual((seen, self.dev.presses), ([1], 0))
        # A judgment that fails withholds it too: its exception is kept
        # apart from the words an operator is shown.
        with self.assertRaises(fx.PressWithheld) as cm:
            f.act("button", "press", judge=lambda: 1 / 0)
        self.assertEqual(self.dev.presses, 0)
        self.assertNotIn("ZeroDivisionError", cm.exception.why)
        self.assertIn("ZeroDivisionError", cm.exception.error)
        # A press whose first request found the box gone is judged again at
        # the fresh address, after its new nonce.
        dev2 = FakeFixture(host="127.0.0.2", port=self.dev.port)
        self.addCleanup(dev2.stop)
        where = iter(["127.0.0.1", "127.0.0.2"])
        f = self.client(resolver=lambda h: next(where))
        calls = []

        def box_moves():
            calls.append(len(dev2.nonces))
            if len(calls) == 1:
                self.dev.stop()                     # the box leaves the first address
            return None
        f.act("button", "press", judge=box_moves)
        self.assertEqual(calls, [0, 1])
        self.assertEqual((self.dev.presses, dev2.presses), (0, 1))
        self.dev = FakeFixture()                    # tearDown stops it
        # A press that found another pulse in progress (a 409) is judged
        # again before it is sent a second time: that other press may have
        # paused the job.
        clock = WallStep()
        saved = fx.time
        fx.time = clock
        self.addCleanup(setattr, fx, "time", saved)
        self.dev.busy_once = True
        asked = []

        def paused_meanwhile():
            asked.append(len(asked))
            return None if len(asked) == 1 else "the job paused meanwhile"
        with self.assertRaises(fx.PressWithheld):
            self.client(ip="127.0.0.1").act("button", "press", judge=paused_meanwhile)
        self.assertEqual((len(asked), self.dev.presses), (2, 0))

    def test_a_wall_clock_step_moves_no_wait_and_no_lookup(self):
        # The board's first time sync after a boot steps its wall clock by
        # any amount: the address's age and the spacing of two presses are
        # timed on the monotonic clock, so neither moves with it.
        clock = WallStep()
        saved = fx.time
        fx.time = clock
        self.addCleanup(setattr, fx, "time", saved)
        f = self.client()
        f.status()
        clock.wall_step = 1e6
        f.status()
        self.assertEqual(len(self.resolved), 1)          # the address found a moment ago is not stale
        f.act("button", "press")
        self.dev.state["button"] = "idle"                # the box's pulse is over at once
        clock.wall_step = -1e6
        f.act("button", "press")
        self.assertEqual(self.dev.presses, 2)
        # the second press waited out the first one's pulse and the gap, no more
        self.assertTrue(fx.BUTTON_GAP_S <= max(clock.slept, default=0.0) < 1.0, clock.slept)

    def test_a_request_is_signed_and_the_key_never_travels(self):
        # The MAC is the firmware's for the same request: fixture/test/
        # auth_test.c holds auth.c to this vector. The box presses on a
        # signed request, and no header it received carries the key.
        self.assertEqual(fx.request_mac(KEY, "POST", "/button", "00112233445566778899aabbccddeeff",
                                        b'{"ms": 200}'),
                         "88147db98785e1a8d89d2457eae37b64a5f2fa1143fd6ada4faf3878fede3907")
        f = self.client(ip="127.0.0.1")
        self.assertEqual(f._request("GET", "/")[0], 200)
        self.assertEqual(f._request("POST", "/button", {})[0], 200)      # a fresh nonce for each request
        self.assertEqual(self.dev.presses, 1)
        self.assertTrue(self.dev.headers)
        self.assertFalse([h for h in self.dev.headers if any(KEY in v for v in h.values())])


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

    def test_the_tests_that_put_fire_in_play_never_run_unattended(self):
        # The kernel drills that unlock the latch for a run open the
        # interlock loop and keep a person present, whatever the bench
        # covers; the tests that open an armed window are live.
        from forgetest import catalog
        tests = {t.id: t for t in catalog.all_tests(catalog.load_suite())}
        for tid in ("kernel.fire-line", "kernel.resume-lead"):
            t = tests[tid]
            self.assertIn("interlock", t.actions, tid)
            self.assertNotEqual(t.kind, "auto", tid)
            self.assertFalse(t.fixture_runnable(fx.CHANNELS), tid)
        for tid in ("cooling.fail-tier-stop", "exthost.armed-freeze"):
            self.assertEqual(tests[tid].kind, "live", tid)


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
        self.pulse = []                 # what pulse_left answers, one value a call, then 0
        self.age = 0.0                  # what nonce_age answers

    def covers(self, channel):
        return channel in self.channels and (channel != "button" or self.button_enabled)

    def pulse_left(self):
        return self.pulse.pop(0) if self.pulse else 0.0

    def nonce_age(self):
        return self.age

    def act(self, channel, state, ms=None, judge=None):
        if self.fail:
            raise fx.FixtureError("the box is off")
        why = judge() if judge is not None else None
        if why is not None:
            raise fx.PressWithheld(why)
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


class ButtonReaderTests(unittest.TestCase):
    """What the runner's press waits read from the button's LEDs (hw.py),
    over a fake LED class tree: RoutingTests stub the reader itself."""

    def setUp(self):
        self.leds = tempfile.mkdtemp(prefix="forgetest-leds-")
        self.addCleanup(shutil.rmtree, self.leds, ignore_errors=True)
        os.environ["GF_LEDS_ROOT"] = self.leds
        self.addCleanup(os.environ.pop, "GF_LEDS_ROOT", None)

    def lamp(self, *leds):
        """leds: (target, pulse_on) for button_led_1..3."""
        for name, (target, pulse) in zip(runner_mod.hw.BUTTON_LEDS, leds):
            os.makedirs(os.path.join(self.leds, name), exist_ok=True)
            for attr, val in (("target", target), ("pulse_on", pulse)):
                with open(os.path.join(self.leds, name, attr), "w") as f:
                    f.write("%d\n" % val)

    def test_the_button_asks_only_while_all_three_breathe(self):
        hw = runner_mod.hw
        self.assertIsNone(hw.button_asks(), "no LED readable")
        self.lamp((0, 1800), (255, 1800), (40, 1800))         # breathing white
        self.assertTrue(hw.button_asks())
        self.lamp((0, 300), (0, 0), (0, 0))                   # the red flash
        self.assertFalse(hw.button_asks())
        self.lamp((254, 0), (254, 0), (254, 0))               # solid white
        self.assertFalse(hw.button_asks())

    def test_a_pulsing_led_is_lit_whatever_its_target_reads(self):
        hw = runner_mod.hw
        self.assertIsNone(hw.button_lit(), "no LED readable")
        self.lamp((0, 0), (0, 0), (0, 0))
        self.assertFalse(hw.button_lit())
        self.lamp((0, 300), (0, 0), (0, 0))                   # the red flash at its dark half
        self.assertTrue(hw.button_lit())

    @unittest.skipUnless(hasattr(os, "mkfifo"), "needs a FIFO for the switches' event device")
    def test_a_lid_edge_is_not_a_press(self):
        # the presence press is the button's edge alone: a lid edge on the
        # same switch device is not the operator at the button
        hw = runner_mod.hw
        dev = os.path.join(self.leds, "switches")
        os.mkfifo(dev)
        os.environ["GF_SWITCH_DEV"] = dev
        self.addCleanup(os.environ.pop, "GF_SWITCH_DEV", None)
        edges = hw.ButtonEdges()
        self.addCleanup(edges.close)
        with open(dev, "wb") as w:
            w.write(hw.ButtonEdges.EVENT.pack(0, 0, hw.EV_SW, 0, 1))     # EV_SW code 0: the lid
        self.assertIsNone(edges.wait(1, 0.3))


class RoutingTests(unittest.TestCase):
    """The queues with a fixture up: the runner's probe is scripted."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="forgetest-fixture-run-")
        os.environ["FORGETEST_DATA"] = self.tmp
        os.environ["FORGETEST_MARKER"] = os.path.join(self.tmp, "marker")
        helpers.host_run(self)                  # a fake forgectrl, no kernel module
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
        forget_unanswered()
        self.stub = StubFixture(fc=self.fc)
        fx.probe = lambda log, path=None, resolver=None: self.stub
        self.runner.probe_fixture(force=True)

    def tearDown(self):
        fx.probe = self.saved_probe
        forget_unanswered()
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

    def test_a_page_poll_never_waits_on_the_probe(self):
        """The probe is network I/O that a box which does not answer
        stretches to seconds, and a start reads the state holding the
        runner's lock: the state reads the last answer, and an answer past
        its age is probed again off the caller's thread."""
        callers = []
        probed = threading.Event()

        def probe(log, path=None, resolver=None):
            callers.append(threading.current_thread())
            probed.set()
            return self.stub
        fx.probe = probe
        self.runner._fixture_probed = time.monotonic() - runner_mod.FIXTURE_PROBE_S
        st, _ = self.runner.state()
        self.assertEqual(st["fixture"]["hostname"], "forgefixture")     # the last answer
        self.assertTrue(probed.wait(5), "an answer past its age was not probed again")
        self.assertNotIn(threading.current_thread(), callers)

    def test_a_queue_started_before_any_answer_routes_on_a_probe_of_its_own(self):
        # The daemon just came up: no probe has answered, and the page's
        # polls only start one in the background (here one that does not
        # answer until the end). The queue's start probes before it
        # selects, so the fixture's operator tests are in it.
        released = threading.Event()
        self.addCleanup(released.set)

        def probe(log, path=None, resolver=None):
            if threading.current_thread().name == "forgetest-fixture":
                released.wait(10)
            return self.stub
        fx.probe = probe
        self.runner.fixture, self.runner._fixture_probed = None, None
        ok, msg, order = self.runner.start_batch("unattended")
        self.assertTrue(ok, msg)
        self.runner.stop_batch()
        self.assertTrue(until(lambda: self.runner.batch["finished"], 30))
        self.assertEqual(order, ["r.auto", "r.lid", "r.btn", "r.asks"])

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
        # The box judged nothing: the test is run again, and nobody owes
        # the error a disposition.
        self.assertTrue(rec["fixture_error"])
        self.assertNotIn("needs_disposition", rec)
        st, _ = self.runner.state()
        self.assertNotEqual(st["tests"]["r.lid"]["reason"], "disposition")

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

    def test_a_presence_press_that_never_comes_fails_the_test(self):
        run = Run("test", "r.live", "r.live")
        ctx = Context(run, self.runner, self.reg["r.live"])
        self.runner.probe_fixture(force=True)
        self.press_button(False)
        saved = runner_mod.PRESENCE_TIMEOUT_S
        runner_mod.PRESENCE_TIMEOUT_S = 0.3
        try:
            with self.assertRaises(Failed):
                ctx.ready("LIVE FIRE. Scrap under the head.")
        finally:
            runner_mod.PRESENCE_TIMEOUT_S = saved
        self.assertFalse(run.fixture_takeover)

    def test_a_failed_press_with_the_button_unreadable_asks_the_operator(self):
        # Unreadable LEDs say nothing of whether the press was taken: the
        # operator is asked, and the record keeps it a person's.
        run = Run("test", "r.live", "r.live")
        ctx = Context(run, self.runner, self.reg["r.live"])
        self.runner.probe_fixture(force=True)
        self.stub.arm_press = True
        self.stub.fail = True
        saved = runner_mod.hw.button_asks
        runner_mod.hw.button_asks = lambda: None
        try:
            self.assertFalse(ctx.press_now())
            self.assertIsNotNone(run.notice)
            self.assertEqual(run.evidence["actions"][-1]["by"], "operator")
        finally:
            run.finished = {"result": "PASS", "message": "", "duration_s": 0}     # ends the notice's watch
            runner_mod.hw.button_asks = saved

    def test_a_presence_press_opts_the_box_into_no_arm_press(self):
        """Presence is not consent to fire: with the bench's arm_press off,
        the arm press stays the operator's after the presence press too."""
        run = Run("test", "r.live", "r.live")
        ctx = Context(run, self.runner, self.reg["r.live"])
        self.runner.probe_fixture(force=True)
        self.stub.arm_press = False
        run.fixture_takeover = True
        saved = runner_mod.hw.button_asks
        runner_mod.hw.button_asks = lambda: True
        try:
            self.assertFalse(ctx.arm_press())
            self.assertFalse(ctx.press_now())
            self.assertEqual(self.stub.acts, [])
            self.assertEqual([a["by"] for a in run.evidence["actions"]], ["operator", "operator"])
        finally:
            run.finished = {"result": "PASS", "message": "", "duration_s": 0}     # ends the notices' watches
            runner_mod.hw.button_asks = saved

    def machine(self, kstate="idle", asks=False, ilk=45, armed=False):
        """The machine as press_withheld reads it: the kernel's state, the
        button (hw.button_asks), interlock_circuit (45: the latch locked),
        and the cooling engine's armed (None: /cool/status unanswered;
        "absent": answered with no armed field). Each may be a list, one
        value a read."""
        from unittest import mock
        vals = {"cnc/state": kstate, "cnc/interlock_circuit": ilk, "asks": asks}

        def take(key):
            v = vals[key]
            return v.pop(0) if isinstance(v, list) else v
        for name, fn in (("sysfs_read", lambda attr, default=None: take(attr) if attr in vals else default),
                         ("sysfs_int", lambda attr, default=None: take(attr) if attr in vals else default),
                         ("button_asks", lambda: take("asks"))):
            p = mock.patch.object(runner_mod.hw, name, fn)
            p.start()
            self.addCleanup(p.stop)
        self.fc.state["cool"]["armed"] = bool(armed)
        answer = {None: (503, {}), "absent": (200, {"tier": "ok"})}.get(armed)
        self.fc.on_get = (lambda path, query: answer if path == "/cool/status" else None) if answer else None

    def test_the_box_makes_a_press_only_where_it_cannot_arm_or_resume(self):
        """Every press through act is the arm press's, the box's only under
        arm_press, unless the test says it is a pause (the box's while the
        kernel plays) or plain (the box's while no armed window is open and
        the latch is locked); never while the button asks for a press, and
        never on a reading that failed. Withheld in an unattended run, the
        test fails."""
        for name, kind, machine, arm_press, box in (
                ("an arm or resume press", None, {"armed": True}, False, False),
                ("a press with no kind, the machine at rest", None, {}, False, False),
                ("the same, the bench opted in", None, {"armed": True, "asks": True}, True, True),
                ("a pause while the kernel plays", "pause", {"kstate": "running", "armed": True}, False, True),
                ("a pause with the kernel at rest", "pause", {}, False, False),
                ("a pause in an underrun", "pause", {"kstate": "underrun"}, False, False),
                ("a pause, the kernel unread", "pause", {"kstate": None}, False, False),
                ("a pause while the button asks", "pause", {"kstate": "running", "asks": True}, False, False),
                ("a pause, the button unread", "pause", {"kstate": "running", "asks": None}, False, False),
                ("a plain press, no window open", "plain", {}, False, True),
                ("a plain press under an open window", "plain", {"armed": True}, False, False),
                ("a plain press, the cooling engine silent", "plain", {"armed": None}, False, False),
                ("a plain press, no armed field in the answer", "plain", {"armed": "absent"}, False, False),
                ("a plain press while the button asks", "plain", {"asks": True}, False, False),
                ("a plain press, the latch unlocked", "plain", {"ilk": 45 & ~0x08}, False, False),
                ("a plain press, the latch unread", "plain", {"ilk": None}, False, False)):
            with self.subTest(name):
                self.machine(**machine)
                run = Run("test", "r.live", "r.live")
                ctx = Context(run, self.runner, self.reg["r.live"])
                self.stub.acts, self.stub.arm_press = [], arm_press
                ctx.act("button", "press", until=lambda: True, press=kind, timeout=1)
                self.assertEqual(self.stub.acts == [("button", "press")], box)
                self.assertEqual(run.evidence["actions"][-1]["by"], "fixture" if box else "operator")
        run = Run("test", "r.live", "r.live")
        run.unattended = True
        ctx = Context(run, self.runner, self.reg["r.live"])
        self.stub.acts, self.stub.arm_press = [], False
        with self.assertRaises(Failed):
            ctx.act("button", "press", until=lambda: True, timeout=1)
        self.assertEqual(self.stub.acts, [])

    def test_a_press_is_judged_again_right_before_the_box_is_asked(self):
        # The job paused itself between the first judgment and the box's
        # request: the box presses nothing, the operator is asked, and an
        # unattended run fails. Withheld at either judgment, the notice is
        # not the test's own words, which the judgment contradicts.
        says = "The laser is cutting: the press pauses it."
        for at, kstate in (("the request", ["running", "idle"]), ("the first judgment", "idle")):
            for unattended in (False, True):
                with self.subTest(at=at, unattended=unattended):
                    self.machine(kstate=list(kstate) if isinstance(kstate, list) else kstate)
                    run = Run("test", "r.live", "r.live")
                    run.unattended = unattended
                    ctx = Context(run, self.runner, self.reg["r.live"])
                    self.stub.acts = []
                    notices = []
                    run.set_notice = lambda text: notices.append(text)
                    if unattended:
                        with self.assertRaises(Failed):
                            ctx.act("button", "press", until=lambda: True, press="pause", timeout=1, text=says)
                    else:
                        ctx.act("button", "press", until=lambda: True, press="pause", timeout=1, text=says)
                        asked = [n for n in notices if n]
                        self.assertEqual(len(asked), 1)
                        self.assertNotIn(says, asked[0])
                    self.assertEqual(self.stub.acts, [])
                    rec = run.evidence["actions"][-1]
                    self.assertEqual(rec["by"], "operator")
                    self.assertIn("withheld", rec)
        # A judgment that fails at the request (the machine cannot be read):
        # the notice says so in plain words, and the exception is in the
        # log. Through the client itself, which is where a judgment fails.
        dev = FakeFixture()
        self.addCleanup(dev.stop)
        box = fx.Fixture({"hostname": "forgefixture", "key": KEY, "channels": ["button"], "port": dev.port,
                          "ip": "127.0.0.1"})
        box.status()
        self.runner.fixture = box
        self.machine(kstate="running")
        judged = []

        def unreadable_at_the_request():
            judged.append(1)
            if len(judged) > 1:
                raise runner_mod.hw.HwError("the LED read broke")
            return False
        from unittest import mock
        patch = mock.patch.object(runner_mod.hw, "button_asks", unreadable_at_the_request)
        patch.start()
        self.addCleanup(patch.stop)
        run = Run("test", "r.live", "r.live")
        ctx = Context(run, self.runner, self.reg["r.live"])
        notices = []
        run.set_notice = lambda text: notices.append(text)
        ctx.act("button", "press", until=lambda: True, press="pause", timeout=1, text=says)
        asked = [n for n in notices if n]
        self.assertEqual((len(asked), dev.presses), (1, 0))
        self.assertNotIn("the LED read broke", asked[0])
        self.assertTrue(any("the LED read broke" in ln for ln in run.lines), run.lines)

    def test_a_press_the_box_may_have_made_is_asked_of_nobody(self):
        """The box's answer to a press was lost after its request went out:
        a person asked now could press a second time. Nobody is asked; what
        the press does decides, and a press not seen ends the test as the
        harness's error. The same for a pause press and for an arm or
        resume press the bench opted into."""
        self.machine(kstate="running")
        taken = []

        def lost_answer(channel, state, ms=None, judge=None):
            taken.append(True)
            raise fx.FixtureError("timed out", sent=True)
        self.stub.act = lost_answer
        kinds = (("pause", False), (None, True))             # the press's kind, the bench's arm_press
        for kind, opted_in in kinds:
            self.stub.arm_press = opted_in
            for seen in (True, False):
                with self.subTest(kind=kind, seen=seen):
                    run = Run("test", "r.live", "r.live")
                    ctx = Context(run, self.runner, self.reg["r.live"])
                    notices = []
                    run.set_notice = lambda text: notices.append(text)
                    if seen:
                        self.assertIsNotNone(ctx.act("button", "press", until=lambda: bool(taken), press=kind,
                                                     timeout=1))
                    else:
                        with self.assertRaises(fx.FixtureError):
                            ctx.act("button", "press", until=lambda: False, press=kind, timeout=0.1, fail=False)
                    self.assertEqual([n for n in notices if n], [])
        # The watch for such a press ends SENT_PRESS_WAIT_S after the press's
        # nonce was in hand at most (here 3 s before the client gave its
        # answer up), whatever the act's own timeout: a cut whose pause
        # press was lost plays on no longer than that before the test ends
        # and its end cancels it.
        run = Run("test", "r.live", "r.live")
        ctx = Context(run, self.runner, self.reg["r.live"])
        watched = []
        ctx.wait_for = lambda cond, timeout, poll=0.25: watched.append(timeout)
        self.stub.age = 3.0
        for kind, opted_in in kinds:
            self.stub.arm_press = opted_in
            with self.subTest(kind=kind, case="the watch"):
                del watched[:]
                for timeout in (90, 5):
                    with self.assertRaises(fx.FixtureError):
                        ctx.act("button", "press", until=lambda: False, press=kind, timeout=timeout, fail=False)
                self.assertEqual(watched, [runner_mod.SENT_PRESS_WAIT_S - 3.0, 5])
        # long enough that a press the box makes late is still seen by its act
        self.assertGreater(runner_mod.SENT_PRESS_WAIT_S, fx.NONCE_LIFE_S + fx.PULSE_MAX_S)
        # Only such a press: one the box made and answered keeps the act's
        # own time, as a person's does.
        del self.stub.act, watched[:]
        self.stub.arm_press = False
        ctx.act("button", "press", until=lambda: False, press="pause", timeout=90, fail=False)
        self.assertEqual(watched, [90])

    def test_a_lit_button_after_a_press_the_box_may_still_make_asks_nobody(self):
        """An arm press whose answer was lost: the box can still make it
        until its nonce's life and its pulse are over, and the controller
        takes it a moment after (PRESS_READ_S). The button is watched that
        long, on press_now and on the arm-press thread alike: a press taken
        in it is the box's and nobody is asked; a button still lit after it
        is asked of the operator, and not before; an Abort ends the watch,
        and then nobody is asked. On a clock that only the watch's own waits
        move."""
        box_s = fx.NONCE_LIFE_S + fx.PULSE_MAX_S
        self.stub.arm_press = True
        clock = WallStep()
        clock.strftime = time.strftime
        saved = runner_mod.time
        runner_mod.time = clock
        self.addCleanup(setattr, runner_mod, "time", saved)
        saved_asks = runner_mod.hw.button_asks
        self.addCleanup(setattr, runner_mod.hw, "button_asks", saved_asks)
        for name, out_at, abort_at, asked_from in (
                ("the press taken late in the box's time", box_s - 0.3, None, None),
                ("the box's last press, taken a moment after", box_s + 0.3, None, None),
                ("the button still lit", None, None, box_s + runner_mod.PRESS_READ_S),
                ("the button unreadable", "unreadable", None, box_s + runner_mod.PRESS_READ_S),
                ("an Abort in the watch", None, 2.0, None)):
            for path in ("press_now", "the arm-press thread"):
                if (out_at, path) == ("unreadable", "the arm-press thread"):
                    continue                    # the thread presses only once it has read the button lit
                with self.subTest(name, path=path):
                    t0 = clock.monotonic()

                    def lost_answer(channel, state, ms=None, judge=None, t0=t0):
                        self.stub.pulse_left = lambda: max(0.0, t0 + box_s - clock.monotonic())
                        raise fx.FixtureError("timed out", sent=True)
                    self.stub.act = lost_answer
                    runner_mod.hw.button_asks = (
                        lambda t0=t0, out_at=out_at: None if out_at == "unreadable" else (
                            out_at is None or clock.monotonic() - t0 < out_at))
                    run = Run("test", "r.live", "r.live")
                    run.aborted = ClockEvent(clock, None if abort_at is None else t0 + abort_at)
                    ctx = Context(run, self.runner, self.reg["r.live"])
                    notices = []
                    run.set_notice = lambda text, t0=t0, notices=notices: text and notices.append(
                        clock.monotonic() - t0)
                    if path == "press_now" and abort_at is not None:
                        self.assertRaises(runner_mod.Aborted, ctx.press_now)
                    elif path == "press_now":
                        ctx.press_now()
                    else:
                        self.assertTrue(ctx.arm_press())
                        self.assertTrue(until(lambda: not threads(PRESS)), "the arm-press thread never ended")
                    if asked_from is None:
                        self.assertEqual(notices, [], "the operator was asked for a press the box may make")
                    else:
                        self.assertEqual(len(notices), 1)
                        self.assertGreater(notices[0], asked_from - 0.01)
                        self.assertLess(notices[0], asked_from + 0.5)
                    if isinstance(out_at, float):
                        self.assertEqual(run.evidence["actions"][-1]["by"], "fixture")
                    if abort_at is not None:
                        self.assertLess(clock.monotonic() - t0, abort_at + 1.0, "the watch outlived the Abort")

    def test_a_loop_a_person_opened_for_a_failing_box_is_restored_by_a_person(self):
        # The box's answer to an open was lost (it may hold the loop open)
        # and the operator opened it by hand: at the close the box is asked
        # and so is the operator, or the loop stays open with nobody told.
        run = Run("test", "r.lid", "r.lid")
        ctx = Context(run, self.runner, self.reg["r.lid"])
        notices = []
        run.set_notice = lambda text: notices.append(text)
        real_act = self.stub.act

        def lost_open(channel, state, ms=None, judge=None):
            raise fx.FixtureError("timed out", sent=True)
        self.stub.act = lost_open
        ctx.act("lid", "open", until=lambda: True, timeout=1)
        self.stub.act = real_act
        del notices[:]
        ctx.act("lid", "close", until=lambda: True, timeout=1)
        self.assertEqual(self.stub.acts, [("lid", "close")])
        self.assertEqual([n for n in notices if n], [runner_mod.ACTION_TEXT[("lid", "close")]])
        del notices[:]
        ctx.act("lid", "open", until=lambda: True, timeout=1)         # the box's own from here on
        ctx.act("lid", "close", until=lambda: True, timeout=1)
        self.assertEqual([n for n in notices if n], [])

    def test_an_arm_press_thread_whose_body_ends_during_its_wait_asks_nobody(self):
        self.stub.arm_press = True
        saved = runner_mod.hw.button_asks
        runner_mod.hw.button_asks = lambda: False                # the button never lights
        try:
            run = Run("test", "r.live", "r.live")
            ctx = Context(run, self.runner, self.reg["r.live"])
            self.assertTrue(ctx.arm_press(lit_timeout=60))
            ctx.end_body()
            self.assertFalse(run.press_threads[0].is_alive(), "the thread waits on after the body ended")
            self.assertEqual(self.stub.acts, [])
            self.assertIsNone(run.notice)
        finally:
            runner_mod.hw.button_asks = saved

    def test_an_arm_press_thread_makes_no_press_once_the_body_has_ended_at_its_lit_read(self):
        # end_body lands between the button's lit read and the press: the
        # check under press_lock stops it.
        self.stub.arm_press = True
        run = Run("test", "r.live", "r.live")
        ctx = Context(run, self.runner, self.reg["r.live"])
        saved = runner_mod.hw.button_asks
        runner_mod.hw.button_asks = lambda: run.body_ended.set() or True
        try:
            self.assertTrue(ctx.arm_press())
            self.assertTrue(until(lambda: not threads(PRESS)))
            self.assertEqual(self.stub.acts, [])
        finally:
            runner_mod.hw.button_asks = saved

    def blocked_press(self):
        """An arm press whose box request is under way until go is set; the
        Context, in_act (set once the request is under way), and go."""
        in_act, go = threading.Event(), threading.Event()

        def blocking(channel, state, ms=None, judge=None):
            in_act.set()
            go.wait(5)
            self.stub.acts.append((channel, state))
        self.stub.arm_press = True
        self.stub.act = blocking
        saved = runner_mod.hw.button_asks
        runner_mod.hw.button_asks = lambda: True
        self.addCleanup(setattr, runner_mod.hw, "button_asks", saved)
        self.addCleanup(go.set)
        run = Run("test", "r.live", "r.live")
        ctx = Context(run, self.runner, self.reg["r.live"])
        self.assertTrue(ctx.arm_press())
        self.assertTrue(in_act.wait(5))
        return ctx, go

    def ender(self, ctx):
        """end_body on a thread of its own: (done event, what it saw on
        return: the box's presses, whether a press thread was alive)."""
        done, seen = threading.Event(), {}

        def run():
            ctx.end_body()
            seen["acts"] = list(self.stub.acts)
            seen["alive"] = bool(threads(PRESS))
            done.set()
        threading.Thread(target=run, daemon=True).start()
        return done, seen

    def test_end_body_waits_for_a_press_whose_request_is_under_way(self):
        ctx, go = self.blocked_press()
        ctx.PRESS_JOIN_S = 0.01                 # the lock alone holds end_body here
        done, seen = self.ender(ctx)
        self.assertFalse(done.wait(0.2), "end_body returned with the box's request under way")
        go.set()
        self.assertTrue(done.wait(5))
        self.assertEqual(seen["acts"], [("button", "press")])

    def test_end_body_returns_only_once_the_press_thread_has_ended(self):
        ctx, go = self.blocked_press()
        with ctx.run._lock:                     # the thread's record of the press waits on the run's log
            go.set()
            done, seen = self.ender(ctx)
            self.assertFalse(done.wait(0.2), "end_body returned before the press thread ended")
        self.assertTrue(done.wait(5))
        self.assertFalse(seen["alive"])

    def test_end_body_waits_out_the_boxs_pulse(self):
        run = Run("test", "r.live", "r.live")
        ctx = Context(run, self.runner, self.reg["r.live"])
        self.stub.pulse = [0.06, 0.03]
        ctx.end_body()
        self.assertEqual(self.stub.pulse, [], "end_body did not wait for the box to let the press go")
        # A press whose answer was lost is one the box can make until its
        # nonce's life is over, and hold for a pulse after: the wait covers
        # that whole time (on a clock that only the wait's sleeps move).
        clock = WallStep()
        clock.strftime = time.strftime
        saved = runner_mod.time
        runner_mod.time = clock
        self.addCleanup(setattr, runner_mod, "time", saved)
        until_s = clock.monotonic() + fx.NONCE_LIFE_S + fx.PULSE_MAX_S
        self.stub.pulse_left = lambda: max(0.0, until_s - clock.monotonic())
        ctx.end_body()
        self.assertEqual(self.stub.pulse_left(), 0.0, "end_body returned while the box could still press")
        # The same with the client gone: a probe during the test found the
        # box away (or made a new client), and the press the old one lost
        # can still be made. What the box may still do is kept for the
        # process, and the wait reads it there.
        saved = fx.time
        fx.time = clock
        self.addCleanup(setattr, fx, "time", saved)
        fx._unanswered["press"] = clock.monotonic() + fx.NONCE_LIFE_S + fx.PULSE_MAX_S
        self.runner.fixture = None
        ctx.end_body()
        self.assertEqual(fx.unanswered_left(), 0.0, "end_body returned while the box could still press")
        # And a loop request whose answer was lost: the box can still serve
        # it for its nonce's life, and the end pass comes only after that.
        forget_unanswered()
        fx._unanswered["loop"] = clock.monotonic() + fx.NONCE_LIFE_S
        ctx.end_body()
        self.assertEqual(fx.unanswered_left("loop"), 0.0, "end_body returned while the box could still serve "
                                                          "a loop request")

    def test_an_arm_press_thread_makes_no_press_once_its_test_has_ended(self):
        """A test that fails before its job reaches the arm wait leaves no
        press for the next job that does."""
        def fails_before_the_arm(ctx):
            ctx.arm_press()
            raise Failed("the job was refused before its arm wait")
        self.reg["r.armfail"] = helpers.make_test("r.armfail", [("forgectrl", "src/ui.c")], kind="live",
                                                  actions=("button",), fn=fails_before_the_arm)
        self.runner.registry = self.reg
        self.stub.arm_press = True
        lamp = Lamp()
        saved = runner_mod.hw.button_asks
        runner_mod.hw.button_asks = lamp
        try:
            lamp.set(False)
            ok, msg, run = self.runner._start_test("r.armfail", ack_live=True)
            self.assertTrue(ok, msg)
            self.assertTrue(until(lambda: run.finished is not None, 30))
            lamp.set(True)                       # the next job's arm wait
            self.assertTrue(until(lambda: not threads(PRESS)), "the arm-press thread outlived its test")
            self.assertEqual(self.stub.acts, [])
        finally:
            runner_mod.hw.button_asks = saved

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
        lamp = Lamp()
        saved = runner_mod.hw.button_asks
        runner_mod.hw.button_asks = lamp
        try:
            seen[:] = []
            mark = lamp.set(False, who=PRESS)
            self.assertTrue(ctx.arm_press())
            self.assertTrue(lamp.seen(mark, 3), "the arm press never read the button")
            self.assertEqual(self.stub.acts, [])                    # not yet: dark button
            lamp.set(True)
            self.assertTrue(until(lambda: self.stub.acts))
            self.assertEqual(self.stub.acts, [("button", "press")])
            self.assertEqual(run.evidence["actions"][1]["by"], "fixture")
            self.assertEqual(seen, [])                              # no notice went up
        finally:
            runner_mod.hw.button_asks = saved

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
        lamp = Lamp()
        saved = runner_mod.hw.button_asks
        runner_mod.hw.button_asks = lamp
        try:
            mark = lamp.set(False)
            self.assertFalse(ctx.arm_press())            # the operator's press
            self.assertIsNotNone(run.notice)
            self.assertTrue(lamp.seen(mark, 2))
            self.assertIsNotNone(run.notice)             # dark before the arm: still asked for
            mark = lamp.set(True)
            self.assertTrue(lamp.seen(mark))
            self.assertIsNotNone(run.notice)             # lit: the arm waits for the press
            lamp.set(False)                              # the press taken
            self.assertTrue(until(lambda: run.notice is None))
            # A notice put up meanwhile is not the arm's: it stays, and the
            # watch ends with the notice it guarded.
            self.assertFalse(ctx.arm_press())
            ctx.notice("Something else.")
            lamp.set(True)
            lamp.set(False)
            self.assertTrue(until(lambda: not threads(WATCH)), "the watch outlived its notice")
            self.assertEqual((run.notice or {}).get("text"), "Something else.")
            # The watch ends with the run: a test that ends before the press
            # (a FAIL on the way to the arm, say) leaves no thread polling the
            # LEDs for up to the arm's whole timeout.
            self.assertFalse(ctx.arm_press())
            self.assertTrue(threads(WATCH))              # up while the arm is still dark
            run.finished = {"result": "FAIL", "message": "", "duration_s": 0}
            self.assertTrue(until(lambda: not threads(WATCH)))
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
                return until(lambda: (run.notice is not None) == up, 2)
            mark = lamp.set(True)                        # the machine waits for the press
            self.assertFalse(ctx.press_now())            # the box is off: the operator is asked
            self.assertIsNotNone(run.notice)
            self.assertTrue(lamp.seen(mark))             # the watch read the button lit
            lamp.set(False)                              # the operator's press is taken
            self.assertTrue(settles(False))
            mark = lamp.set(True)
            self.assertTrue(ctx.arm_press())             # the fixture is asked, and refuses
            self.assertTrue(settles(True))
            self.assertTrue(lamp.seen(mark))
            lamp.set(False)
            self.assertTrue(settles(False))
            self.assertTrue(ctx.arm_press(lit_timeout=0))  # the button never lit for the fixture
            self.assertTrue(settles(True))
            mark = lamp.set(True)
            self.assertTrue(lamp.seen(mark))
            lamp.set(False)
            self.assertTrue(settles(False))
            # A press the box made and reported failed (its reply lost after
            # the press): the button is out when the error arrives, so the
            # press was taken, and no notice asks for a second one.
            def pressed_then_failed(channel, state, ms=None):
                lamp.set(False)                          # the controller takes the press
                raise fx.FixtureError("timed out", sent=True)
            self.stub.act = pressed_then_failed
            lamp.set(True)
            self.assertTrue(ctx.press_now())
            self.assertIsNone(run.notice)
            self.assertEqual(ctx.evidence["actions"][-1]["by"], "fixture")
            lamp.set(True)
            self.assertTrue(ctx.arm_press())
            self.assertTrue(until(lambda: not threads(PRESS)), "the fixture's press never ended")
            self.assertEqual(ctx.evidence["actions"][-1]["by"], "fixture")
            self.assertIsNone(run.notice)
            # A refusal (or a request that never reached the box) while the
            # button went out: a press at the machine was taken, so no notice
            # goes up either, and the record does not give it to the box.
            def refused_while_pressed(channel, state, ms=None):
                lamp.set(False)                          # a person's press is taken
                raise fx.FixtureError("button press -> 409 button disabled: the enable jumper is out")
            self.stub.act = refused_while_pressed
            lamp.set(True)
            self.assertFalse(ctx.press_now())
            self.assertIsNone(run.notice)
            self.assertEqual(ctx.evidence["actions"][-1]["by"], "operator")
            lamp.set(True)
            self.assertTrue(ctx.arm_press())
            self.assertTrue(until(lambda: not threads(PRESS)), "the fixture's press never ended")
            self.assertIn("fixture_error", ctx.evidence["actions"][-1])
            self.assertEqual(ctx.evidence["actions"][-1]["by"], "operator")
            self.assertIsNone(run.notice)
        finally:
            runner_mod.hw.button_asks = saved


if __name__ == "__main__":
    unittest.main()
