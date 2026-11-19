# Copyright 2026 514 LLC d/b/a OpenGlow
# Written by Scott Wiederhold
# https://community.openglow.org
# SPDX-License-Identifier:    MIT

"""The bench tools that change the machine, on the host: what a tool
changes (the loop heater, the pump, a setting, a driver binding, a
register) comes back on every way out, the bench page's Abort (a SIGTERM)
and a lost terminal included; a tool refuses before it changes anything
when it must, the thermal tools while anything can arm a job and the live
drills while their rungs would not be what they say; and a drill's verdict
is its exit status. Each tool runs as written against a fake sysfs tree, a
fake forgectrl API and a fake clock, so nothing sleeps. Only those paths:
what the tools measure and report is theirs.

heater_pwm is the kernel module's: 0 is off, 65535 full power, and a write
above zero holds only for the lease (heater_lease_ms); heater_lapses counts
the leases that ran out with the heater on."""
import contextlib
import errno
import glob
import io
import json
import os
import shlex
import shutil
import signal
import struct
import subprocess
import sys
import tempfile
import threading
import time as real_time
import types
import unittest
from unittest import mock

from forgetest import hw

REPO = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
BENCH = os.path.join(REPO, "scripts", "bench")
HEATER_FULL = 65535


def load(script):
    """The tool as a module of its own, run from its source (no bytecode
    is written next to it), with gfbench importable and aimed at
    127.0.0.1. Every name the tool binds is its own, so a case patches the
    module it loaded and nothing else; the bench modules it imports
    (gfbench, xy_pattern_accel, ...) are fresh copies too, in
    mod.bench_modules, and leave sys.modules as they found it."""
    path = os.path.join(BENCH, script)
    mod = types.ModuleType("bench_" + script[:-3])
    mod.__file__ = path
    names = [os.path.basename(p)[:-3] for p in glob.glob(os.path.join(BENCH, "*.py"))]
    held = {n: sys.modules.get(n) for n in names}
    saved = (list(sys.path), os.environ.get("GF_HOST"), sys.dont_write_bytecode)
    sys.path.insert(0, BENCH)
    for n in names:
        sys.modules.pop(n, None)
    os.environ["GF_HOST"] = "127.0.0.1"
    sys.dont_write_bytecode = True
    try:
        with open(path, encoding="utf-8") as f:
            exec(compile(f.read(), path, "exec"), mod.__dict__)
        mod.bench_modules = {n: sys.modules[n] for n in names if n in sys.modules}
    finally:
        sys.path[:] = saved[0]
        for n in names:
            if held[n] is None:
                sys.modules.pop(n, None)
            else:
                sys.modules[n] = held[n]
        if saved[1] is None:
            os.environ.pop("GF_HOST", None)
        else:
            os.environ["GF_HOST"] = saved[1]
        sys.dont_write_bytecode = saved[2]
    return mod


class Clock:
    """time as the tools use it: a sleep runs the hooks, then moves the
    clock on at once."""

    def __init__(self):
        self.t = 1.0e9
        self.hooks = []

    def time(self):
        return self.t

    monotonic = time

    def monotonic_ns(self):
        return int(self.t * 1e9)

    def sleep(self, s):
        for hook in list(self.hooks):
            hook()
        self.t += max(0.0, s)

    strftime = staticmethod(real_time.strftime)
    gmtime = staticmethod(real_time.gmtime)


def deliver(sig):
    """sig to this process, as kill sends it: the tool's handler runs here.
    A tool that left the signal at its default fails the case instead of
    ending the test run."""
    handler = signal.getsignal(sig)
    if not callable(handler):
        raise AssertionError("%s is left at %r" % (signal.Signals(sig).name, handler))
    signal.raise_signal(sig)


def send(sig):
    """sig to this process at a moment the tool may have set it to ignore:
    nothing happens then. Left at its default, it fails the case."""
    if signal.getsignal(sig) == signal.SIG_DFL:
        raise AssertionError("%s is left at its default" % signal.Signals(sig).name)
    signal.raise_signal(sig)


def put(root, rel, text):
    path = os.path.join(root, rel)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w") as f:
        f.write(text)


def tree(root):
    """{relative path: content} of every file under root."""
    out = {}
    for d, _, files in os.walk(root):
        for name in files:
            path = os.path.join(d, name)
            with open(path) as f:
                out[os.path.relpath(path, root)] = f.read().strip()
    return out


class Cooling:
    """forgectrl as the tools read it: /settings, /cool/flow, /cool/status,
    the Acknowledge, and a run session the controller's M8 opens and M9
    closes. The flow watch reads off off_after seconds after a write turns
    it off (None: never). In the session's at_read-th status read the
    trigger happens once: the verdict CRITICAL or SENSOR, 45 C downstream,
    or another writer's heater value; or on_read(self) runs, and a reply it
    returns is the read's. A CRITICAL session ends waiting for the
    Acknowledge, its cause live for live_s seconds."""

    SETTINGS = {"cool_temp_max": "30.0", "cool_temp_resume": "28.0", "cool_flow_watch": "1"}

    def __init__(self, clock, heater_path, off_after=10.0, trigger=None, at_read=3, on_read=None,
                 live_s=30.0, ack_ok=True, attention=None):
        self.clock, self.heater_path = clock, heater_path
        self.settings = dict(self.SETTINGS)
        self.off_after, self.off_at = off_after, None
        self.trigger, self.at_read, self.on_read = trigger, at_read, on_read
        self.live_s, self.live_until, self.ack_ok = live_s, None, ack_ok
        self.attention = dict(attention or {})
        self.phase, self.verdict, self.down_c = "idle", "OK", 25.5
        self.run_reads, self.fired_at = 0, None
        self.reads, self.trace, self.posts, self.acks = [], [], [], []
        clock.hooks.append(self.sample)

    def heater(self):
        try:
            with open(self.heater_path) as f:
                return int(f.read().strip() or 0)
        except (OSError, ValueError):
            return None

    def watch(self):
        off = (self.settings.get("cool_flow_watch") == "0" and self.off_at is not None
               and self.clock.t >= self.off_at)
        return "off" if off else "hold"

    def live(self):
        return self.live_until is not None and self.clock.t < self.live_until

    def sample(self):
        self.trace.append((self.clock.t, self.heater(), self.watch()))

    def get(self, path, **kw):
        self.sample()
        if path == "/settings":
            return 200, dict(self.settings)
        if path == "/cool/flow":
            return 200, {"watch": self.watch()}
        if path == "/cool/status":
            return self.status()
        return 404, "no such route"

    def post(self, path, params=None, **kw):
        self.sample()
        self.posts.append((path, dict(params or {})))
        if path == "/settings":
            for k, v in (params or {}).items():
                if v == "":
                    self.settings.pop(k, None)
                else:
                    self.settings[k] = v
            if (params or {}).get("cool_flow_watch") == "0" and self.off_after is not None:
                self.off_at = self.clock.t + self.off_after
            return 200, {}
        if path == "/cool/attention/ack":
            self.acks.append(self.clock.t)
            if not self.attention or self.live() or not self.ack_ok:
                return 409, {"error": "refused"}
            self.attention, self.verdict = {}, "OK"
            return 200, {}
        return 404, "no such route"

    def status(self):
        self.reads.append((self.clock.t, self.heater(), self.phase))
        if self.phase == "run":
            self.run_reads += 1
            if self.run_reads == self.at_read and self.fired_at is None:
                self.fired_at = len(self.reads) - 1
                if self.on_read:
                    reply = self.on_read(self)
                    if reply is not None:
                        return reply
                if self.trigger in ("CRITICAL", "SENSOR"):
                    self.verdict = self.trigger
                elif self.trigger == "down":
                    self.down_c = 45.0
                elif self.trigger == "foreign":
                    with open(self.heater_path, "w") as f:
                        f.write("1000")
        attention = dict(self.attention, live=self.live()) if self.attention else {}
        return 200, {"phase": self.phase, "verdict": self.verdict, "fire_ok": self.verdict == "OK",
                     "hold": self.phase == "run" and self.verdict != "OK", "up_c": 25.0,
                     "down_c": self.down_c, "reason": "", "limits": {}, "attention": attention}

    def open_session(self):
        self.phase, self.run_reads = "run", 0

    def end_session(self):
        self.phase = "idle"
        if self.verdict == "CRITICAL":
            self.attention = {"standing": True, "verdict": "CRITICAL", "reason": "coolant critical"}
            self.live_until = self.clock.t + self.live_s
            self.verdict = "ACK"


def grbl_for(cooling):
    """The controller's Grbl port, as far as an M8 and an M9 reach."""
    class FakeGrbl:
        def __init__(self, *a, **kw):
            pass

        def cmd(self, line, wait=0.4, **kw):
            if line == "M8":
                cooling.open_session()
            elif line == "M9":
                cooling.end_session()
            cooling.clock.sleep(wait)
            return "ok"

        def close(self):
            pass
    return FakeGrbl


class StubSampler:
    """offset_probe's sensor sampler, not run: what the probe measures is
    not these cases'."""

    def __init__(self):
        self.samples = [(0.0, None, None), (1.0, None, None)]
        self.stop = threading.Event()

    def start(self):
        pass

    def join(self, timeout=None):
        pass


class ToolCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="forgetest-bench-safety.")
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        for sig in (signal.SIGTERM, signal.SIGINT, getattr(signal, "SIGHUP", None), getattr(signal, "SIGTSTP", None)):
            if sig is not None:
                self.addCleanup(signal.signal, sig, signal.getsignal(sig))

    def quiet(self, fn, *args):
        """(fn's result, None) or (None, the exception it ended with), its
        printing dropped. A failed assertion inside the tool stays one."""
        with contextlib.redirect_stdout(io.StringIO()):
            try:
                return fn(*args), None
            except AssertionError:
                raise
            except (Exception, SystemExit, KeyboardInterrupt) as e:
                return None, e


ARGV = ["critical_tier_drill.py"]


class AaOffsetCheckTests(ToolCase):
    """aa_offset_check.py reads the coolant from /status, where a count that
    is no temperature is null, and from the engine, where it is -273.15: a
    channel with no reading is reported as such, never raised on."""

    def test_a_coolant_channel_with_no_reading_is_reported_not_raised_on(self):
        mod = load("aa_offset_check.py")
        docs = {"/status": {"coolant": {"down_c": None, "up_c": 22.5}},
                "/cool/status": {"down_c": -273.15, "up_c": 22.4, "phase": "idle"},
                "/settings": {"cool_aa_offset_counts": "20"}}

        def urlopen(url, timeout=None):
            return io.BytesIO(json.dumps(docs[url[len(mod.BASE):]]).encode())

        class Grbl:
            def __init__(self, *args):
                pass

            def status(self):
                return "<Idle|MPos:0.000,0.000,0.000>"

            def cmd(self, line):
                return "ok"

        out = io.StringIO()
        with mock.patch.object(mod, "time", Clock()), mock.patch.object(mod, "raw", lambda: [1023, 694]), \
                mock.patch.object(mod, "Grbl", Grbl), mock.patch.object(mod.urllib.request, "urlopen", urlopen), \
                mock.patch.object(mod.sys, "argv", ["aa_offset_check.py", "2"]), contextlib.redirect_stdout(out):
            rc = mod.main()
        self.assertEqual(rc, 0, out.getvalue())
        # The rule itself: null and the engine's -273.15 are no reading, and
        # a mean over no reading is none.
        self.assertIsNone(mod.reading(None))
        self.assertIsNone(mod.reading(-273.15))
        self.assertEqual(mod.reading(22.5), 22.5)
        self.assertIsNone(mod.mean([None, None]))
        self.assertEqual(mod.mean([None, 22.0]), 22.0)


@unittest.skipUnless(hasattr(signal, "SIGHUP") and hasattr(signal, "raise_signal"), "needs POSIX signals")
class CriticalTierDrillTests(ToolCase):
    """critical_tier_drill.py: the loop heater at full duty inside one run
    session, its settings the drill's until it hands the machine back."""

    def drill(self, heater_file=True, **kw):
        mod = load("critical_tier_drill.py")
        clock = Clock()
        root = tempfile.mkdtemp(dir=self.tmp)
        heater = os.path.join(root, "heater_pwm")
        if heater_file:
            put(root, "heater_pwm", "0")
        cool = Cooling(clock, heater, **kw)
        mod.HEATER, mod.time = heater, clock
        mod.forgectrl_get, mod.forgectrl_post = cool.get, cool.post
        mod.Grbl = grbl_for(cool)
        mod.data_path = lambda name: os.path.join(root, name)
        return mod, cool

    def heater_now(self, cool):
        with open(cool.heater_path) as f:
            return f.read().strip()

    def test_a_failure_waiting_for_the_acknowledge_is_refused_before_any_write(self):
        mod, cool = self.drill(heater_file=False, attention={"standing": True, "verdict": "CRITICAL"})
        self.assertEqual(self.quiet(mod.main, ARGV), (2, None))
        self.assertEqual(cool.posts, [], "the drill wrote a setting")
        self.assertFalse(os.path.exists(cool.heater_path), "the drill wrote the heater")

    def test_the_heater_waits_for_the_flow_watch_to_read_off(self):
        # A watch that never reads off: the drill stops, its settings put
        # back, never having heated.
        mod, cool = self.drill(off_after=None)
        self.assertEqual(self.quiet(mod.main, ARGV), (2, None))
        self.assertEqual(cool.settings, Cooling.SETTINGS)
        self.assertEqual(max(h or 0 for _, h, _ in cool.trace), 0)
        # One that reads off 10 s after the drill turned it off: no heat
        # before then.
        mod, cool = self.drill(off_after=10.0, trigger="CRITICAL")
        self.assertEqual(self.quiet(mod.main, ARGV), (0, None))
        heated = [w for _, h, w in cool.trace if h]
        self.assertTrue(heated, "the drill never heated")
        self.assertEqual(set(heated), {"off"}, "heated while the flow watch was on")

    def test_the_heater_goes_off_at_once_at_each_stop_line(self):
        # CRITICAL, 45 C downstream, the sensors reading SENSOR, another
        # writer's value: from the next status read on, the heater reads 0.
        for trigger in ("CRITICAL", "down", "SENSOR", "foreign"):
            with self.subTest(trigger):
                mod, cool = self.drill(trigger=trigger)
                self.quiet(mod.main, ARGV)
                self.assertIsNotNone(cool.fired_at, "the stop line was never reached")
                after = [h for _, h, _ in cool.reads[cool.fired_at + 1:]]
                self.assertTrue(after)
                self.assertEqual(set(after), {0}, "the heater stayed on past the stop line")

    def test_every_exit_leaves_the_heater_off_and_the_settings_back(self):
        exits = {"normal": dict(trigger="CRITICAL"),
                 "exception": dict(on_read=lambda c: (500, "engine error")),
                 "SIGTERM": dict(on_read=lambda c: deliver(signal.SIGTERM)),
                 "SIGHUP": dict(on_read=lambda c: deliver(signal.SIGHUP))}
        for name, kw in exits.items():
            with self.subTest(name):
                mod, cool = self.drill(**kw)
                self.quiet(mod.main, ARGV)
                self.assertIsNotNone(cool.fired_at, "the exit was never reached")
                self.assertEqual(max(h or 0 for _, h, _ in cool.trace), HEATER_FULL)
                self.assertEqual(self.heater_now(cool), "0")
                self.assertEqual(cool.settings, Cooling.SETTINGS)

    def test_a_heater_write_that_fails_at_exit_does_not_skip_the_restore(self):
        holder = {}

        def fail(cool):
            holder["mod"].HEATER = os.path.join(self.tmp, "gone", "heater_pwm")
            return 500, "engine error"
        mod, cool = self.drill(on_read=fail)
        holder["mod"] = mod
        _, err = self.quiet(mod.main, ARGV)
        self.assertIsInstance(err, RuntimeError)
        self.assertEqual(cool.settings, Cooling.SETTINGS)

    def test_the_drill_fails_unless_it_hands_the_machine_back_as_found(self):
        # The cause stays live 30 s after the session: the Acknowledge
        # waits for it, then the verdict is the start's again.
        mod, cool = self.drill(trigger="CRITICAL", live_s=30.0)
        self.assertEqual(self.quiet(mod.main, ARGV), (0, None))
        self.assertEqual(cool.verdict, "OK")
        self.assertTrue(cool.acks)
        self.assertGreaterEqual(min(cool.acks), cool.live_until, "acknowledged while the cause was live")
        # An Acknowledge that does not take: the machine still waits, and
        # the drill fails.
        mod, cool = self.drill(trigger="CRITICAL", ack_ok=False)
        self.assertEqual(self.quiet(mod.main, ARGV), (1, None))


@unittest.skipUnless(hasattr(signal, "SIGHUP") and hasattr(signal, "raise_signal"), "needs POSIX signals")
class OffsetProbeTests(ToolCase):
    """offset_probe.py: one actuator at a time, the heater and the pump
    among them, with nothing that can arm a job running, and every value
    put back on the way out."""

    START = {"thermal/exhaust_pwm": "0", "thermal/intake_pwm": "0", "head/air_assist_pwm": "204",
             "head/purge_air": "1", "thermal/heater_pwm": "0", "thermal/water_pump_on": "1",
             "thermal/tec_on": "0", "leds/lid_led/brightness": "100"}
    HEAT = 26214                # the probe's heater step

    def probe(self, start=None, owners=(), later=None):
        """owners: the processes that can arm a job, as the probe finds
        them (none: the bench page's takeover); later, when given: what it
        finds at every check after the first."""
        mod = load("offset_probe.py")
        clock = Clock()
        root = tempfile.mkdtemp(dir=self.tmp)
        for rel, value in dict(self.START, **(start or {})).items():
            put(root, rel, value)
        cool = Cooling(clock, os.path.join(root, "thermal", "heater_pwm"))
        mod.SYSFS, mod.LEDS = root + "/", os.path.join(root, "leds") + "/"
        mod.time, mod.Sampler = clock, StubSampler
        checks = []

        def arming():
            checks.append(1)
            return list(later if later is not None and len(checks) > 1 else owners)
        mod.arming_processes = arming
        self.pump = []
        clock.hooks.append(lambda: self.pump.append(tree(root)["thermal/water_pump_on"]))
        argv = sys.argv
        self.addCleanup(setattr, sys, "argv", argv)
        sys.argv = ["offset_probe.py"]
        env = mock.patch.dict(os.environ, {"FORGETEST_BENCH_DATA": self.tmp})
        env.start()
        self.addCleanup(env.stop)
        return mod, cool, root

    def interrupt_at_heat(self, cool, act):
        """act() once, at the first sleep with the heater at the probe's duty."""
        done = []

        def hook():
            if not done and cool.heater() == self.HEAT:
                done.append(1)
                act()
        cool.clock.hooks.append(hook)

    def test_every_exit_puts_every_value_back(self):
        def boom():
            raise RuntimeError("a write failed")
        exits = {"normal": None, "exception": boom,
                 "SIGTERM": lambda: deliver(signal.SIGTERM), "SIGHUP": lambda: deliver(signal.SIGHUP)}
        for name, act in exits.items():
            with self.subTest(name):
                mod, cool, root = self.probe()
                if act:
                    self.interrupt_at_heat(cool, act)
                self.quiet(mod.main)
                self.assertIn(self.HEAT, [h for _, h, _ in cool.trace], "the probe never heated")
                self.assertEqual(tree(root), self.START, "a value was not put back")

    def test_the_survey_refuses_while_anything_can_arm_a_job(self):
        # forgectrl up (a job could arm through the controller while the
        # pump is off): nothing is written.
        mod, cool, root = self.probe(owners=["412 forgectrl", "520 grblHAL_glowfor"])
        self.assertEqual(self.quiet(mod.main), (2, None))
        self.assertEqual(tree(root), self.START)
        self.assertNotIn(self.HEAT, [h for _, h, _ in cool.trace])

    def test_anything_that_can_arm_by_the_pump_off_dwell_stops_the_survey_first(self):
        # forgectrl brought up mid-survey (a forgetest recover(), an
        # operator's init script): the pump never goes off.
        mod, cool, root = self.probe(later=["731 forgectrl"])
        self.assertEqual(self.quiet(mod.main), (2, None))
        self.assertIn(self.HEAT, [h for _, h, _ in cool.trace], "the survey stopped before it ran")
        self.assertNotIn("0", self.pump, "the pump went off")
        self.assertEqual(tree(root), self.START)

    def test_the_probe_refuses_without_the_pump_or_with_the_heater_on(self):
        for rel, value in (("thermal/water_pump_on", "0"), ("thermal/heater_pwm", "1000")):
            with self.subTest(rel):
                mod, cool, root = self.probe(start={rel: value})
                self.assertEqual(self.quiet(mod.main), (2, None))
                self.assertNotIn(self.HEAT, [h for _, h, _ in cool.trace])

    def test_a_failed_restore_is_a_failed_run(self):
        # The survey run through, the survey stopped before its pump-off
        # dwell (2 when its restores took), and the jog.
        for run, later, argv, status in (("survey", None, [], 0), ("stopped survey", ["731 forgectrl"], [], 2),
                                         ("jog", None, ["jog"], 0)):
            for fails in (False, True):
                with self.subTest(run, fails=fails):
                    mod, cool, root = self.probe(later=later)
                    if argv:
                        self.jog_controller()
                    sys.argv = ["offset_probe.py"] + argv
                    if fails:
                        fail_a_restore(mod)
                    self.assertEqual(self.quiet(mod.main), (1 if fails else status, None))

    def jog_controller(self, on_wait=lambda: None):
        """An idle GRBL controller for the jog, on_wait() at every wait for
        a state."""
        class Grbl:
            def __init__(self, host, port):
                self.s, self.log = types.SimpleNamespace(sendall=lambda b: None), []

            def status(self):
                return "<Idle|MPos:0.000,0.000,0.000>"

            def cmd(self, line, timeout=5.0):
                return "ok"

            def wait_state(self, want, timeout=60.0, poll=0.1):
                on_wait()
                return want
        drills = types.ModuleType("live_fire_drills")
        drills.Grbl, drills.HOST, drills.PORT = Grbl, "127.0.0.1", 23
        modules = mock.patch.dict(sys.modules, {"live_fire_drills": drills})
        modules.start()
        self.addCleanup(modules.stop)

    def test_a_second_abort_during_the_jogs_wait_still_puts_the_air_assist_back(self):
        # The first Abort ends the jog with the air assist at its run duty;
        # the second lands while the tool waits for the controller's Idle.
        mod, cool, root = self.probe()
        aborts = []

        def second():
            if len(aborts) == 1:
                aborts.append("second")
                deliver(signal.SIGTERM)
        self.jog_controller(second)

        def hook():
            if not aborts and tree(root)["head/air_assist_pwm"] == "1023":
                aborts.append("first")
                deliver(signal.SIGTERM)
        cool.clock.hooks.append(hook)
        sys.argv = ["offset_probe.py", "jog"]
        _, err = self.quiet(mod.main)
        self.assertEqual(aborts, ["first", "second"])
        self.assertEqual(tree(root)["head/air_assist_pwm"], "204", "the air assist stayed at its run duty")
        self.assertEqual(getattr(err, "code", None), 128 + signal.SIGTERM)


class FakeOS:
    """os as platform_drills.py's dead-man drill uses it: /dev/glowforge is
    a scratch file (busy when asked, as with forgectrl holding it), and its
    close trips the dead man as the drill expects of the kernel: the
    heater, the TEC, the measure laser, the UV LED and the Z driver off, the
    run stopped. lapse: the heater's lease ran out too (heater_lapses
    counts one)."""

    def __init__(self, root, busy=False, lapse=False):
        self.root, self.busy, self.lapse, self.fd = root, busy, lapse, None

    def __getattr__(self, name):
        return getattr(os, name)

    def open(self, path, flags, *args):
        if path != "/dev/glowforge":
            return os.open(path, flags, *args)
        if self.busy:
            raise OSError(errno.EBUSY, os.strerror(errno.EBUSY), path)
        self.fd = os.open(os.path.join(self.root, "pulsedev"), os.O_WRONLY | os.O_CREAT, 0o600)
        return self.fd

    def close(self, fd):
        os.close(fd)
        if fd != self.fd:
            return
        for rel in ("thermal/heater_pwm", "thermal/tec_on", "head/measure_laser", "head/uv_led", "head/z_enable"):
            put(self.root, rel, "0")
        put(self.root, "cnc/state", "idle")
        if self.lapse:
            lapses = os.path.join(self.root, "thermal", "heater_lapses")
            with open(lapses) as f:
                n = int(f.read())
            put(self.root, "thermal/heater_lapses", str(n + 1))


@unittest.skipUnless(os.name == "posix", "platform_drills imports fcntl")
class DeadmanDrillTests(ToolCase):
    """platform_drills.py deadman: the pre-trip posture written, the dead
    man tripped, the resting posture put back whatever happened."""

    RESTING = {"thermal/heater_pwm": "0", "thermal/tec_on": "0", "thermal/water_pump_on": "1",
               "thermal/exhaust_pwm": "0", "thermal/intake_pwm": "0", "head/measure_laser": "0",
               "head/uv_led": "0", "head/z_enable": "0", "head/air_assist_pwm": "204", "head/white_led": "0"}

    def deadman(self, busy=False, lapse=False, lapses="3"):
        mod = load("platform_drills.py")
        root = tempfile.mkdtemp(dir=self.tmp)
        start = dict(self.RESTING, **{"cnc/motor_lock": "0", "cnc/laser_latch": "0", "cnc/step_freq": "0",
                                      "cnc/run": "0", "cnc/state": "running"})
        if lapses is not None:
            start["thermal/heater_lapses"] = lapses
        for rel, value in start.items():
            put(root, rel, value)
        mod.G, mod.os, mod.time = root + "/", FakeOS(root, busy, lapse), Clock()
        mod.sh = lambda cmd: ""
        mod.uptime = lambda: 0.0
        return mod, root

    def test_a_failed_drill_puts_the_resting_posture_back(self):
        # forgectrl holds the pulse device: the drill ends at the open,
        # after its pre-trip writes (the heater among them).
        mod, root = self.deadman(busy=True)
        _, err = self.quiet(mod.drill_deadman)
        self.assertIsInstance(err, SystemExit)
        now = tree(root)
        self.assertEqual({k: now[k] for k in self.RESTING}, self.RESTING)
        self.assertEqual(now["cnc/motor_lock"], "0")

    def test_the_drill_fails_when_the_heaters_lease_turned_it_off(self):
        # The dead man alone: PASS. The lease too (a lapse counted): FAIL.
        # A module with no lease: the count is not read.
        for lapse, lapses, want in ((False, "3", 0), (True, "3", 1), (False, None, 0)):
            with self.subTest(lapse=lapse, lapses=lapses):
                mod, root = self.deadman(lapse=lapse, lapses=lapses)
                self.assertEqual(self.quiet(mod.drill_deadman), (want, None))


POSIX_SIGNALS = hasattr(signal, "SIGHUP") and hasattr(signal, "raise_signal")


def set_argv(case, argv):
    saved = sys.argv
    case.addCleanup(setattr, sys, "argv", saved)
    sys.argv = list(argv)


class GfbenchTests(ToolCase):
    """gfbench.py, the helper the tools share: who can arm a job, the
    restore steps, and where a tool's data lands on the board."""

    def test_the_processes_that_can_arm_a_job_are_found(self):
        gb = load("gfbench.py")
        proc = tempfile.mkdtemp(dir=self.tmp)
        for pid, comm, cmdline in (("10", "forgectrl", b"/usr/bin/forgectrl\0"),
                                   ("11", "grblHAL_glowfor", b"/usr/bin/grblHAL_glowforge\0"),
                                   ("12", "python3", b"python3\0/usr/share/forgefirm-app/gfcloud.py\0"),
                                   ("13", "sh", b"sh\0-c\0tail gfcloud.py.log\0"),
                                   ("14", "python3", b"python3\0offset_probe.py\0"),
                                   ("15", "ctl-test", b"/tmp/ctl-test\0")):
            put(proc, os.path.join(pid, "comm"), comm + "\n")
            with open(os.path.join(proc, pid, "cmdline"), "wb") as f:
                f.write(cmdline)
            os.makedirs(os.path.join(proc, pid, "fd"))
        os.symlink("/dev/null", os.path.join(proc, "14", "fd", "0"))
        os.symlink("/dev/glowforge", os.path.join(proc, "15", "fd", "3"))   # a controller under another name
        os.makedirs(os.path.join(proc, "self"))
        gb.PROC = proc
        self.assertEqual(sorted(gb.arming_processes()), ["10 forgectrl", "11 grblHAL_glowfor", "12 gfcloud.py",
                                                         "15 ctl-test (holds /dev/glowforge)"])
        # An unreadable /proc reads as someone running, so a tool refuses.
        gb.PROC = os.path.join(self.tmp, "no-proc")
        self.assertTrue(gb.arming_processes())

    def test_every_restore_step_runs_whatever_the_one_before_it_did(self):
        gb = load("gfbench.py")
        ran = []

        def fails():
            raise OSError(errno.EIO, "write failed")

        def gives_up():
            raise SystemExit(1)                         # a fail() inside a step
        self.assertEqual(self.quiet(gb.put_back, ("a", fails), ("b", gives_up), ("c", lambda: ran.append("c"))),
                         (["a", "b"], None))
        self.assertEqual(ran, ["c"])

    @unittest.skipUnless(POSIX_SIGNALS, "needs POSIX signals")
    def test_a_second_abort_waits_for_every_restore_step(self):
        # The step in flight finishes, the next one runs, and then the tool
        # ends as the Abort asked.
        gb = load("gfbench.py")
        gb.exit_on_signal()
        ran = []

        def aborted_midway():
            deliver(signal.SIGTERM)
            ran.append("first, finished")
        _, err = self.quiet(gb.put_back, ("first", aborted_midway), ("second", lambda: ran.append("second")))
        self.assertEqual(ran, ["first, finished", "second"])
        self.assertEqual(getattr(err, "code", None), 128 + signal.SIGTERM)

    def test_on_the_board_a_tools_data_goes_to_the_pages_directory(self):
        # The tools' own directory there is the read-only rootfs.
        gb = load("gfbench.py")
        gb.BOARD_SYSFS = tempfile.mkdtemp(dir=self.tmp)
        gb.BOARD_DATA = os.path.join(self.tmp, "data", "bench")
        with mock.patch.dict(os.environ):
            os.environ.pop("FORGETEST_BENCH_DATA", None)
            self.assertEqual(gb.data_dir(), gb.BOARD_DATA)
        self.assertTrue(os.path.isdir(gb.BOARD_DATA))


class DebugKernelDrillTests(ToolCase):
    """debug_kernel_drills.py runs under the bench page's takeover: it
    refuses while anything can arm a job, and never stops or starts
    forgectrl itself (the takeover owns both)."""

    def drills(self, owners=()):
        mod = load("debug_kernel_drills.py")
        root = tempfile.mkdtemp(dir=self.tmp)
        put(root, "state", "idle")
        os.makedirs(os.path.join(root, "drv", "cnc"))
        mod.STATE, mod.CNC_DRV, mod.time = os.path.join(root, "state"), os.path.join(root, "drv"), Clock()
        mod.SYSFS = root + "/"
        mod.require_debug_kernel = lambda: None
        mod.arming_processes = lambda: list(owners)
        mod.regulator_provider = lambda: (None, None)   # drill B finds no regulator here and skips
        calls = []

        def sh(cmd, check=False):
            calls.append(cmd)
            return types.SimpleNamespace(returncode=0, stdout="", stderr="")
        mod.sh = sh
        return mod, calls

    def test_the_drills_refuse_while_anything_can_arm_a_job(self):
        mod, calls = self.drills(owners=["412 forgectrl"])
        _, err = self.quiet(mod.main)
        self.assertIsInstance(err, SystemExit)
        self.assertEqual(err.code, 2)
        self.assertEqual([c for c in calls if "rmmod" in c or "modprobe" in c], [])

    def test_the_drills_never_stop_or_start_forgectrl(self):
        mod, calls = self.drills()
        _, err = self.quiet(mod.main)
        self.assertEqual(getattr(err, "code", None), 0)
        self.assertTrue([c for c in calls if "rmmod" in c], "drill A never ran")
        self.assertEqual([c for c in calls if "forgectrl" in c], [])


class FakeFireKernel:
    """The pulse engine and the safety chain as fire_test.py and the K3
    drill read them, on a fake clock. A run plays its bytes at 10 kHz; FIRE
    follows the stream's two windows (1-3 s and 4-6 s) while the latch is
    unlocked and fire_follows is set. The lock sets the button latch; a
    press (press_at seconds into the run, or at the unlock with
    press_at="unlock") clears it only with the lock released. The
    remote-interlock loop reads open (interlock_circuit bit 4) until
    close_at (seconds into the run; "open": when the pulse device is opened;
    "unlock": with the unlock), and the switches read it the same way
    (EV_SW 5), with the interlock latch itself (EV_SW 6) set while the loop
    is open unless interlock_latch is False. LASER_ON is FIRE with both
    latches clear, or a fault from emit_at on. interlock_circuit carries the
    kernel's bits 0 to 5. From fail_at seconds into a run, a read of the
    LASER_ON count fails. pump_alive: something else feeds the charge-pump
    watchdog. read_s: what a sysfs read costs on the clock (a drill whose
    watch never sleeps). abort: (seconds into the run, a signal) delivered
    once, at the drill's next read or write (seconds "underrun": at the
    read that finds the underrun state); then: a signal sent once at the
    first lock write after that abort. fail_attr: the attribute whose read
    fails from fail_at on. A halt is refused in the underrun state, and
    always with halt_refused (a fault). at_relock: a signal sent once
    as the latch is locked again after an unlock, before that write takes.
    A halt ends a run; closed counts the closes of the pulse device, and
    closed_unlocked those that found the latch unlocked; stop_at_unlock is
    what SIGTSTP was set to at the unlock."""

    TICK = 10000

    def __init__(self, clock, loop_open=True, press_at=None, close_at=None, fire_follows=True,
                 emit_at=None, fail_at=None, interlock_latch=True, pump_alive=False, read_s=0.0, abort=None,
                 at_relock=None, fail_attr="cnc/laser_on_sampled", halt_refused=False, then=None):
        self.clock = clock
        self.abort = abort
        self.at_relock, self.closed, self.stop_at_unlock = at_relock, 0, None
        self.fail_attr, self.halt_refused, self.then, self.aborted = fail_attr, halt_refused, then, False
        self.interlock_latch = interlock_latch
        self.loop_open, self.press_at, self.close_at = loop_open, press_at, close_at
        self.fire_follows, self.emit_at, self.fail_at = fire_follows, emit_at, fail_at
        self.pump_alive, self.read_s = pump_alive, read_s
        self.locked, self.button, self.streaming, self.underruns = True, 1, 0, 0
        self.run_t0, self.length, self.idle_state = None, 0, "idle"
        self.writes, self.times, self.opened, self.closed_unlocked = [], [], 0, 0
        self.unlocked_samples = []          # seconds into the run of each interlock_circuit read, latch unlocked
        self.last_read, self.read_before = None, []     # the attribute read last before each write

    def t(self):
        return None if self.run_t0 is None else self.clock.t - self.run_t0

    def state(self):
        if self.run_t0 is not None and self.t() >= self.length / self.TICK:
            self.run_t0 = None
            self.idle_state = "underrun" if self.streaming else "idle"
            self.underruns += 1 if self.streaming else 0
        return "running" if self.run_t0 is not None else self.idle_state

    def tick(self):
        t = self.t()
        if t is None:
            return
        if self.press_at not in (None, "unlock") and t >= self.press_at and not self.locked:
            self.button = 0
        if self.close_at not in (None, "open", "unlock") and t >= self.close_at:
            self.loop_open = False
        if self.abort is not None and self.abort[0] != "underrun" and t >= self.abort[0]:
            self.send_abort()

    def send_abort(self):
        sig, self.abort, self.aborted = self.abort[1], None, True
        deliver(sig)

    def fire(self):
        t = self.t()
        return (self.state() == "running" and not self.locked and self.fire_follows
                and (1.0 <= t < 3.0 or 4.0 <= t < 6.0))

    def laser_on(self):
        t = self.t()
        if self.emit_at is not None and t is not None and t >= self.emit_at:
            return True
        return self.fire() and self.button == 0 and not self.loop_open

    def rd(self, attr):
        self.clock.t += self.read_s
        self.last_read = attr
        self.tick()
        if attr == self.fail_attr and self.fail_at is not None and (self.t() or 0) >= self.fail_at:
            raise OSError(errno.EIO, "read failed")
        if self.abort is not None and self.abort[0] == "underrun" and self.state() == "underrun":
            self.send_abort()
        running = self.state() == "running"
        on = self.laser_on()
        if attr == "cnc/interlock_circuit":
            if running and not self.locked:
                self.unlocked_samples.append(self.t())
            return str((0 if on else 1) | (2 if self.fire() else 0) | (4 if self.button else 0)
                       | (8 if self.locked else 0) | (16 if self.loop_open else 0) | (0 if running else 32))
        return {"cnc/state": self.state(),
                "cnc/charge_pump_alive": "1" if running or self.pump_alive else "0",
                "cnc/laser_enable": "1" if self.fire() else "0", "cnc/laser_on": "1" if on else "0",
                "cnc/laser_on_sampled": "1" if on else "0", "cnc/button_latch": str(self.button),
                "cnc/underruns": str(self.underruns), "cnc/faults": "0"}[attr]

    def wr(self, attr, val):
        self.tick()
        if (attr, str(val)) == ("cnc/laser_latch", "1") and not self.locked and self.at_relock is not None:
            sig, self.at_relock = self.at_relock, None
            send(sig)
        if (attr, str(val)) == ("cnc/laser_latch", "1") and self.aborted and self.then is not None:
            sig, self.then = self.then, None
            send(sig)
        self.writes.append((attr, str(val)))
        self.times.append(self.clock.t)
        self.read_before.append(self.last_read)
        if attr == "cnc/laser_latch":
            self.locked = str(val) == "1"
            if self.locked:
                self.button = 1                     # the lock sets the button latch
            else:
                self.stop_at_unlock = signal.getsignal(signal.SIGTSTP)
            if not self.locked and self.press_at == "unlock":
                self.button = 0
            if not self.locked and self.close_at == "unlock":
                self.loop_open = False
        elif attr == "cnc/streaming":
            self.streaming = int(val)
        elif attr == "cnc/halt":
            if self.halt_refused or self.state() == "underrun":     # as the kernel: a fault, the underrun
                raise OSError(errno.EPERM, "not permitted in this state")
            self.run_t0 = None
        elif attr == "cnc/run":
            self.run_t0 = self.clock.t
        elif attr == "cnc/stop" and self.state() == "underrun":
            self.idle_state = "idle"

    def latch_writes(self):
        return [v for a, v in self.writes if a == "cnc/laser_latch"]

    def switch_bits(self):
        self.tick()
        return (1 << hw.SW_INTERLOCK if self.loop_open else 0) | (
            1 << hw.SW_INTERLOCK_LATCH if self.loop_open and self.interlock_latch else 0)

    def behind_the_catalog(self, case):
        """The catalog's own reads (forgetest.hw) on this kernel too, for the
        case: its sysfs and its switches."""
        for name, fake in (("sysfs_read", lambda attr, default=None: self.rd(attr)),
                           ("switch_bits", self.switch_bits)):
            patch = mock.patch.object(hw, name, fake)
            patch.start()
            case.addCleanup(patch.stop)

    def install(self, mod):
        """The kernel behind a drill module: sysfs, the pulse device, the
        position counters and the clock."""
        kernel = self

        class Dev:
            def __getattr__(self, name):
                return getattr(os, name)

            def open(self, path, flags, *a):
                kernel.opened += 1
                if kernel.close_at == "open":
                    kernel.loop_open = False
                return 99

            def write(self, fd, data):
                kernel.length = len(data)
                return len(data)

            def lseek(self, fd, pos, how):
                return pos

            def close(self, fd):
                kernel.closed += 1
                kernel.closed_unlocked += not kernel.locked
        mod.rd, mod.wr, mod.os = self.rd, self.wr, Dev()
        mod.fcntl = types.SimpleNamespace(flock=lambda fd, op: None, LOCK_EX=2, LOCK_UN=8)
        mod.rd_pos = lambda: (0, 0, 0, 0, 0)
        mod.time = self.clock
        return mod


class KernelDrillTests(ToolCase):
    """The takeover drills that unlock the latch (fire_test.py's B and U,
    gate_a_kernel_drills.py's K3) on a fake kernel: each is refused unless
    nothing else feeds the charge pump and the chain reads open by the
    catalog's guard, read before the device is opened and again at the
    unlock; the button latch and the loop are read right after the unlock;
    every sample of the unlocked run is judged as the catalog judges one,
    and a reason relocks the latch at once; the latch is locked before the
    pulse device is let go on every way out, the page's Abort, a lost
    terminal and a Ctrl-C among them, and a drill cut short halts its run
    right after that lock; no signal cuts the way out short, whether a
    signal began it or the drill ended by itself; a Ctrl-Z does not stop a
    drill that unlocks; a drill's verdict is the catalog's; and no takeover
    drill on the page takes the 40 V rail down."""

    def drill(self, tool, mode, expect_exit=None, hook=None, **kernel):
        """One run of a drill's main on a fake kernel: (exit status, the
        kernel). The K drills' watches never sleep, so their reads move the
        clock, and their kernel keeps FIRE an input over a mid-run unlock
        unless the case says otherwise. hook(mod): the module made ready
        further, before it runs."""
        if mode.startswith("K"):
            kernel = dict({"read_s": 0.0005, "fire_follows": False}, **kernel)
        mod = load(tool)
        k = FakeFireKernel(Clock(), **kernel)
        k.install(mod)
        k.behind_the_catalog(self)
        if hook is not None:
            hook(mod)
        set_argv(self, [tool, mode])
        # every signal the drills set is at its default again: what a run
        # before this one left set says nothing of this one
        for sig in (signal.SIGTERM, signal.SIGHUP, signal.SIGTSTP):
            signal.signal(sig, signal.SIG_DFL)
        signal.signal(signal.SIGINT, signal.default_int_handler)
        rc, err = self.quiet(mod.main)
        if expect_exit is None:
            self.assertIsNone(err)
        else:
            self.assertIsInstance(err, expect_exit)
            rc = getattr(err, "code", None)
        self.assertEqual(k.closed_unlocked, 0, "the pulse device was let go with the latch unlocked")
        if "0" in k.latch_writes():
            self.assertEqual(k.stop_at_unlock, signal.SIG_IGN, "a Ctrl-Z would stop the drill with the latch unlocked")
        return rc, k

    def assertWholeWayOut(self, k, cut):
        """The drill's way out ran to its end: the latch locked last, the
        pulse device let go, streaming off; and, for a drill cut short, its
        run halted by the write right after that lock."""
        self.assertEqual((k.latch_writes()[-1], k.closed, k.streaming), ("1", 1, 0))
        if cut:
            self.assertIn(("cnc/halt", "1"), k.writes, "the run of a drill cut short was left playing")
            self.assertEqual(k.writes[k.writes.index(("cnc/halt", "1")) - 1], ("cnc/laser_latch", "1"))
            self.assertNotEqual(k.state(), "running")

    def fire(self, mode, **kernel):
        return self.drill("fire_test.py", mode, **kernel)

    def k3(self, **kernel):
        return self.drill("gate_a_kernel_drills.py", "K3", **kernel)

    def test_an_unlocked_phase_runs_only_behind_the_open_interlock_loop(self):
        # The interlock_latch row: the kernel drives the latch's SET and the
        # loop reads open, yet the interlock latch itself (EV_SW 6) does not
        # read set. The pump_alive rows: something else feeds the watchdog.
        for tool, mode, kernel in (("fire_test.py", "B", {"loop_open": False}),
                                   ("fire_test.py", "U", {"loop_open": False}),
                                   ("fire_test.py", "B", {"interlock_latch": False}),
                                   ("fire_test.py", "B", {"pump_alive": True}),
                                   ("gate_a_kernel_drills.py", "K3", {"loop_open": False}),
                                   ("gate_a_kernel_drills.py", "K3", {"pump_alive": True})):
            with self.subTest(mode=mode, **kernel):
                rc, k = self.drill(tool, mode, **kernel)
                self.assertEqual(rc, 2)
                self.assertNotIn("0", k.latch_writes(), "the latch was unlocked with the loop closed")
                self.assertEqual(k.opened, 0)

    def test_the_guard_is_read_again_at_the_unlock(self):
        # The loop closes after the first read of the guard, while the drill
        # sets its run up: no unlock.
        for tool, mode in (("fire_test.py", "B"), ("gate_a_kernel_drills.py", "K3")):
            with self.subTest(mode=mode):
                rc, k = self.drill(tool, mode, close_at="open")
                self.assertEqual((rc, k.opened), (2, 1))
                self.assertNotIn("0", k.latch_writes())
                # K3's run was in flight, locked: it is not left playing
                self.assertEqual(k.state(), "idle")

    def test_a_bad_reading_right_after_the_unlock_relocks_before_the_run(self):
        for name, kernel in (("the button latch clear", {"press_at": "unlock"}),
                             ("the loop closed", {"close_at": "unlock"})):
            with self.subTest(name):
                rc, k = self.fire("B", **kernel)
                self.assertEqual(rc, 1)
                self.assertNotIn(("cnc/run", "1"), k.writes)
                self.assertEqual(k.latch_writes()[-1], "1")

    def test_a_sample_that_is_not_dark_behind_both_latches_relocks_at_once(self):
        # A press, the loop closing, LASER_ON active; and, for K3, the FIRE
        # drive restored by the unlock (FIRE plays from 1.0 s on). The last
        # element: the reads that can find it, the chain's sample first.
        chain = ("cnc/interlock_circuit",)
        for tool, mode, name, kernel, at, found_by in (
                ("fire_test.py", "B", "a press", {"press_at": 2.0}, 2.0, chain),
                ("fire_test.py", "B", "the loop closed", {"close_at": 2.0}, 2.0, chain),
                ("fire_test.py", "B", "LASER_ON active", {"emit_at": 2.0}, 2.0, chain),
                ("gate_a_kernel_drills.py", "K3", "a press", {"press_at": 1.5}, 1.5, chain),
                ("gate_a_kernel_drills.py", "K3", "the loop closed", {"close_at": 1.5}, 1.5, chain),
                ("gate_a_kernel_drills.py", "K3", "LASER_ON active", {"emit_at": 1.5}, 1.5,
                 chain + ("cnc/laser_on",)),
                ("gate_a_kernel_drills.py", "K3", "FIRE driven", {"fire_follows": True}, 1.0,
                 ("cnc/laser_on",))):
            with self.subTest(mode=mode, case=name):
                rc, k = self.drill(tool, mode, **kernel)
                self.assertEqual(rc, 1)
                run = k.writes.index(("cnc/run", "1"))
                unlock = k.writes.index(("cnc/laser_latch", "0"))
                relock = k.writes.index(("cnc/laser_latch", "1"), max(run, unlock))
                # Locked within a sample of the event, `at` seconds into the
                # run, and by the sample that read it: the run is not read
                # unlocked again, and nothing is read between the read that
                # found it and the lock.
                self.assertLess(k.times[relock] - k.times[run] - at, 0.01)
                self.assertLessEqual(len([t for t in k.unlocked_samples if t >= at]), 1)
                self.assertIn(k.read_before[relock], found_by)

    def test_an_abort_mid_run_relocks_and_ends_streaming(self):
        # The bench page's Abort is a SIGTERM, a lost terminal a SIGHUP, a
        # Ctrl-C a SIGINT: the drill ends through its own way out, and its
        # run ends with it (K2's Abort lands 0.2 s in, inside its pads).
        fire, gate = "fire_test.py", "gate_a_kernel_drills.py"
        for tool, mode, sig, at in ((fire, "A", signal.SIGTERM, 2.0), (fire, "B", signal.SIGTERM, 2.0),
                                    (fire, "U", signal.SIGTERM, 2.0), (fire, "U", signal.SIGHUP, 2.0),
                                    (fire, "U", signal.SIGINT, 2.0), (gate, "K1", signal.SIGTERM, 1.0),
                                    (gate, "K2", signal.SIGTERM, 0.2), (gate, "K3", signal.SIGTERM, 2.0),
                                    (gate, "K3", signal.SIGHUP, 2.0), (gate, "K3", signal.SIGINT, 2.0)):
            with self.subTest(mode=mode, sig=sig):
                rc, k = self.drill(tool, mode, expect_exit=SystemExit, abort=(at, sig))
                self.assertEqual(rc, 128 + sig)
                self.assertIsNone(k.abort, "the abort never came")
                if mode in ("B", "U", "K3"):
                    self.assertIn("0", k.latch_writes(), "the abort came before the unlock")
                self.assertWholeWayOut(k, cut=True)
        # An error ends a drill the same way: a read that fails mid-run.
        for tool, mode, kernel in ((fire, "B", {"fail_at": 2.0}),
                                   (gate, "K1", {"fail_at": 1.0, "fail_attr": "cnc/state"}),
                                   (gate, "K2", {"fail_at": 0.2, "fail_attr": "cnc/state"})):
            with self.subTest(mode=mode, case="a read fails mid-run"):
                rc, k = self.drill(tool, mode, expect_exit=OSError, **kernel)
                self.assertWholeWayOut(k, cut=True)
        # A halt the kernel refuses is passed over, and the way out goes on:
        # U aborted in its underrun state, before its acknowledgment, and K3
        # aborted on a kernel in a fault.
        for tool, mode, kernel in ((fire, "U", {"abort": ("underrun", signal.SIGTERM)}),
                                   (gate, "K3", {"abort": (2.0, signal.SIGTERM), "halt_refused": True})):
            with self.subTest(mode=mode, case="the halt refused"):
                rc, k = self.drill(tool, mode, expect_exit=SystemExit, **kernel)
                self.assertIsNone(k.abort, "the abort never came")
                self.assertIn(("cnc/halt", "1"), k.writes)
                self.assertWholeWayOut(k, cut=False)

    def test_no_signal_cuts_the_way_out_short(self):
        # A second signal at the worst moment, as the first is being set to
        # ignore: the way out the first began still runs whole.
        # The same for another signal once the handler has returned, as the
        # way out begins (its own first call, SIGTERM already ignored): the
        # handler set all three to ignore, not only the one that came.
        fire, gate = "fire_test.py", "gate_a_kernel_drills.py"
        for tool, mode, first, second, begun in ((fire, "U", signal.SIGTERM, signal.SIGHUP, False),
                                                 (fire, "U", signal.SIGINT, signal.SIGINT, False),
                                                 (gate, "K3", signal.SIGTERM, signal.SIGHUP, False),
                                                 (fire, "U", signal.SIGTERM, signal.SIGHUP, True),
                                                 (gate, "K3", signal.SIGTERM, signal.SIGHUP, True)):
            with self.subTest(mode=mode, first=first, second=second, way_out_begun=begun):
                sent = []

                def second_as_the_first_is_ignored(mod):
                    real = mod.signal

                    def set_handler(sig, handler):
                        now = (sig == real.SIGTERM and real.getsignal(sig) == real.SIG_IGN) if begun else (
                            sig != real.SIGTSTP)
                        if handler is real.SIG_IGN and now and not sent:
                            sent.append(second)
                            send(second)
                        return real.signal(sig, handler)
                    mod.signal = types.SimpleNamespace(**{n: getattr(real, n) for n in dir(real)
                                                          if not n.startswith("__")})
                    mod.signal.signal = set_handler
                rc, k = self.drill(tool, mode, expect_exit=SystemExit, abort=(2.0, first),
                                   hook=second_as_the_first_is_ignored)
                self.assertEqual(sent, [second])
                self.assertWholeWayOut(k, cut=True)
        # K1 and K2 never unlock, and their way out sets nothing to ignore:
        # the handler alone holds a second, different signal at its lock.
        for mode, at in (("K1", 1.0), ("K2", 0.2)):
            with self.subTest(mode=mode, case="an Abort, then a lost terminal at the way out's lock"):
                rc, k = self.drill(gate, mode, expect_exit=SystemExit, abort=(at, signal.SIGTERM),
                                   then=signal.SIGHUP)
                self.assertEqual((rc, k.then), (128 + signal.SIGTERM, None))
                self.assertWholeWayOut(k, cut=True)
        # A drill that ended by itself: a signal as its way out locks the
        # latch changes nothing, and the verdict stands.
        for tool, mode in ((fire, "B"), (gate, "K3")):
            with self.subTest(mode=mode, case="a signal in the way out of a drill that ended by itself"):
                rc, k = self.drill(tool, mode, at_relock=signal.SIGTERM)
                self.assertEqual((rc, k.at_relock), (0, None))
                self.assertWholeWayOut(k, cut=False)

    def test_a_drill_reads_and_writes_under_the_root_its_guard_reads(self):
        root = tempfile.mkdtemp(dir=self.tmp)
        put(root, "cnc/motor_lock", "0")
        for tool in ("fire_test.py", "gate_a_kernel_drills.py"):
            with self.subTest(tool), mock.patch.dict(os.environ, {"GF_SYSFS_ROOT": root + os.sep}):
                mod = load(tool)
                try:
                    mod.wr("cnc/motor_lock", 15)
                    got = mod.rd("cnc/motor_lock")
                except OSError as e:
                    self.fail("the drill does not use the root its guard reads: %s" % e)
                self.assertEqual((got, hw.sysfs_read("cnc/motor_lock")), ("15", "15"))
                mod.wr("cnc/motor_lock", 0)

    def test_each_phase_is_judged_as_the_catalog_judges_it(self):
        for mode in ("A", "B", "U"):
            with self.subTest(mode=mode, case="clean"):
                self.assertEqual(self.fire(mode)[0], 0)
        with self.subTest(mode="K3", case="clean"):
            self.assertEqual(self.k3()[0], 0)
        cases = (("a LASER_ON sample", "B", {"emit_at": 4.5}),
                 ("FIRE never driven mid-window", "B", {"fire_follows": False}),
                 ("a LASER_ON sample, latch locked", "A", {"emit_at": 2.0}))
        for name, mode, kernel in cases:
            with self.subTest(mode=mode, case=name):
                self.assertEqual(self.fire(mode, **kernel)[0], 1)

    def test_an_underrun_phase_that_breaks_mid_run_ends_with_streaming_off(self):
        mod = load("fire_test.py")
        k = FakeFireKernel(Clock(), fail_at=2.0)
        k.install(mod)
        k.behind_the_catalog(self)
        set_argv(self, ["fire_test.py", "U"])
        _, err = self.quiet(mod.main)
        self.assertIsInstance(err, OSError)
        self.assertEqual(k.streaming, 0)
        self.assertEqual(k.latch_writes()[-1], "1")

    def test_k3_unlocks_only_under_a_set_button_latch(self):
        mod = load("gate_a_kernel_drills.py")
        k = FakeFireKernel(Clock(), press_at="unlock", fire_follows=False)
        k.install(mod)
        k.behind_the_catalog(self)
        watched = []
        mod.watch_unlocked_until_idle = lambda timeout: watched.append(1) or ([], "idle", None)
        rc, err = self.quiet(mod.drill_k3)
        self.assertEqual((rc, err), (1, None))
        self.assertEqual(watched, [], "K3 ran on with the button latch clear")
        self.assertEqual(k.latch_writes()[-1], "1")

    def test_no_takeover_drill_takes_the_rail_down(self):
        rc, k = self.fire("A")
        self.assertEqual(rc, 0)
        self.assertNotIn("cnc/disable", [a for a, _ in k.writes])
        mod = load("pwm_stream_test.py")
        k = FakeFireKernel(Clock())
        k.install(mod)
        mod.pwmsar = lambda: 0
        self.assertEqual(self.quiet(mod.main), (0, None))
        self.assertNotIn("cnc/disable", [a for a, _ in k.writes])
        self.assertEqual(k.state(), "idle")
        mod = load("platform_drills.py")
        ran = []
        mod.subprocess = types.SimpleNamespace(run=lambda cmd, **kw: ran.append(cmd) or types.SimpleNamespace(
            returncode=0, stdout="", stderr=""))
        mod.sh, mod.time = (lambda cmd: ran.append(cmd) or ""), Clock()
        set_argv(self, ["platform_drills.py", "rmmod"])
        self.assertEqual(self.quiet(mod.main), (2, None))
        self.assertEqual([c for c in ran if "rmmod" in c or "modprobe" in c], [])


class FakeBus:
    """os as accel_fast.py uses it for the head bus: the bus opens to a
    fake descriptor, a read moves the clock 10 ms and returns zeros, and the
    at-th read runs act() first."""

    def __init__(self, clock, at=None, act=None):
        self.clock, self.at, self.act, self.reads = clock, at, act, 0

    def __getattr__(self, name):
        return getattr(os, name)

    def open(self, path, flags, *args):
        return 1000

    def write(self, fd, data):
        return len(data)

    def read(self, fd, n):
        self.reads += 1
        self.clock.t += 0.01
        if self.reads == self.at:
            self.act()
        return bytes(n)

    def close(self, fd):
        pass


def raise_io():
    raise OSError(errno.EIO, "bus error")


def fail_a_restore(mod):
    """The tool's put_back with one more restore step, which fails: the
    tool's exit must then say its restores did not all take."""
    real = mod.put_back
    mod.put_back = lambda *steps: real(*steps, ("a restore that fails", raise_io))


@unittest.skipUnless(POSIX_SIGNALS, "needs POSIX signals")
class AccelFastTests(ToolCase):
    """accel_fast.py: it unbinds st_accel, the part forgectrl's liveness
    probe and crash watch read, so it runs only with nothing that can arm
    a job running; st_accel is bound again on every way out, and a rebind
    that did not take is a failed run."""

    def sampler(self, argv, at=None, act=None, owners=(), binds=True):
        """binds: a bind write takes (the device's driver link appears)."""
        mod = load("accel_fast.py")
        clock = Clock()
        mod.time, mod.fcntl = clock, types.SimpleNamespace(ioctl=lambda *a: 0)
        mod.os = FakeBus(clock, at, act)
        mod.CSV = os.path.join(self.tmp, "accel.csv")
        mod.DEVICES = tempfile.mkdtemp(dir=self.tmp)
        mod.arming_processes = lambda: list(owners)
        ops = []

        def bind_ctl(op, dev):
            ops.append((op, dev))
            link = os.path.join(mod.DEVICES, dev, "driver")
            if op == "unbind" and os.path.isdir(link):
                os.rmdir(link)
            elif op == "bind" and binds:
                os.makedirs(link, exist_ok=True)
        mod.bind_ctl = bind_ctl
        set_argv(self, ["accel_fast.py"] + argv)
        return mod, ops

    def test_the_sampler_refuses_while_anything_can_arm_a_job(self):
        mod, ops = self.sampler(["0.5"], owners=["412 forgectrl"])
        self.assertEqual(self.quiet(mod.main), (2, None))
        self.assertEqual(ops, [])

    def test_every_exit_binds_st_accel_again(self):
        exits = {"normal": None, "exception": raise_io, "SIGTERM": lambda: deliver(signal.SIGTERM),
                 "SIGHUP": lambda: deliver(signal.SIGHUP)}
        for name, act in exits.items():
            with self.subTest(name):
                mod, ops = self.sampler(["0.5"], at=10 if act else None, act=act)
                self.quiet(mod.main)
                self.assertEqual([o for o in ops if o[0] == "bind"], [("bind", "3-001e"), ("bind", "3-001d")])

    def test_a_rebind_that_did_not_take_is_a_failed_run(self):
        for binds, status in ((True, 0), (False, 1)):
            with self.subTest(binds=binds):
                mod, ops = self.sampler(["0.5"], binds=binds)
                self.assertEqual(self.quiet(mod.main), (status, None))


class FakeSeekGrbl:
    """The controller as bump_seek.py drives it: idle, every segment done
    at its first status poll."""

    IDLE = "<Idle|MPos:0.000,0.000,0.000|FS:0,0>"

    def __init__(self, idle=True):
        self.idle, self.sent, self.cancels = idle, [], 0

    def wait_idle(self, timeout=30):
        return self.IDLE if self.idle else None

    def status(self):
        return self.IDLE

    def send(self, line):
        self.sent.append(line)

    def cancel(self):
        self.cancels += 1


class FakeSeekAccel:
    def __init__(self, clock, at=None, act=None):
        self.clock, self.at, self.act, self.reads = clock, at, act, 0

    def read(self):
        self.reads += 1
        self.clock.t += 0.002
        if self.reads == self.at:
            self.act()
        return 0, 0, 0, 0.0


@unittest.skipUnless(POSIX_SIGNALS, "needs POSIX signals")
class BumpSeekTests(ToolCase):
    """bump_seek.py: st_accel bound again, and a jog in flight canceled,
    on every way out."""

    def seek(self, idle=True, at=None, act=None):
        mod = load("bump_seek.py")
        clock = Clock()
        grbl = FakeSeekGrbl(idle)
        binds = []
        mod.time, mod.CSV = clock, os.path.join(self.tmp, "bump.csv")
        mod.Grbl = lambda: grbl
        mod.Accel = lambda: FakeSeekAccel(clock, at, act)
        mod.unbind, mod.rebind = (lambda: binds.append("unbind")), (lambda: binds.append("bind"))
        set_argv(self, ["bump_seek.py", "-", "6000", "1", "2"])
        return mod, grbl, binds

    def test_every_exit_binds_st_accel_again(self):
        exits = {"no contact": {}, "controller not idle": {"idle": False},
                 "SIGTERM": {"at": 5, "act": lambda: deliver(signal.SIGTERM)}}
        for name, kw in exits.items():
            with self.subTest(name):
                mod, grbl, binds = self.seek(**kw)
                self.quiet(mod.main)
                self.assertEqual(binds, ["unbind", "bind"])
                if "at" in kw:
                    self.assertGreaterEqual(grbl.cancels, 1, "the jog in flight was not canceled")

    def test_a_failed_restore_is_a_failed_run(self):
        for fails, status in ((False, 0), (True, 1)):
            with self.subTest(fails=fails):
                mod, _, _ = self.seek()
                if fails:
                    fail_a_restore(mod)
                self.assertEqual(self.quiet(mod.main), (status, None))


def duty_register(case, size, found, offset):
    """A fake /dev/mem of size bytes with found at offset: (its path, a
    reader)."""
    mem = os.path.join(tempfile.mkdtemp(dir=case.tmp), "mem")
    with open(mem, "wb") as f:
        f.truncate(size)
        f.seek(offset)
        f.write(struct.pack("<I", found))

    def read():
        with open(mem, "rb") as f:
            f.seek(offset)
            return struct.unpack("<I", f.read(4))[0]
    return mem, read


@unittest.skipUnless(POSIX_SIGNALS, "needs POSIX signals")
class PwmHoldTests(ToolCase):
    """pwm_hold.py: the duty register it found goes back on every way out,
    and a restore step that failed is a failed run."""

    FOUND, HELD = 77, 64

    def hold(self, aborts=False):
        mod = load("pwm_hold.py")
        clock = Clock()
        root = tempfile.mkdtemp(dir=self.tmp)
        for rel in ("cnc/laser_latch", "cnc/laser_enable", "cnc/laser_on", "cnc/laser_on_sampled"):
            put(root, rel, "0")
        mem, sar = duty_register(self, mod.PWM2_BASE + 4096, self.FOUND, mod.PWM2_BASE + mod.SAR_OFF)
        held = []

        def hook():
            if aborts and not held and sar() == self.HELD:
                held.append(1)
                deliver(signal.SIGTERM)
        clock.hooks.append(hook)
        mod.SYS, mod.MEM, mod.time = root + "/", mem, clock
        set_argv(self, ["pwm_hold.py", str(self.HELD), "10"])
        return mod, sar, held

    def test_every_exit_puts_the_duty_back(self):
        for name, aborts in (("normal", False), ("SIGTERM", True)):
            with self.subTest(name):
                mod, sar, held = self.hold(aborts)
                self.quiet(mod.main)
                if aborts:
                    self.assertTrue(held, "the hold never began")
                self.assertEqual(sar(), self.FOUND)

    def test_a_failed_restore_is_a_failed_run(self):
        for fails, status in ((False, 0), (True, 1)):
            with self.subTest(fails=fails):
                mod, _, _ = self.hold()
                if fails:
                    fail_a_restore(mod)
                self.assertEqual(self.quiet(mod.main), (status, None))


@unittest.skipUnless(POSIX_SIGNALS, "needs POSIX signals")
class PwmSweepTests(ToolCase):
    """pwm_sweep.py: the duty register it found goes back on every way out,
    and a restore step that failed is a failed run."""

    FOUND = 77

    def sweep(self, aborts=False):
        mod = load("pwm_sweep.py")
        clock = Clock()
        root = tempfile.mkdtemp(dir=self.tmp)
        for rel in ("cnc/laser_latch", "cnc/laser_enable", "cnc/laser_on"):
            put(root, rel, "0")
        mem, sar = duty_register(self, mod.PWM2_BASE + 4096, self.FOUND, mod.PWM2_BASE + mod.SAR_OFF)
        swept = []

        def hook():
            if aborts and not swept and sar() != self.FOUND:
                swept.append(sar())
                deliver(signal.SIGTERM)
        clock.hooks.append(hook)
        mod.SYS, mod.MEM, mod.time = root + "/", mem, clock
        set_argv(self, ["pwm_sweep.py", "sweep"])
        return mod, sar, swept

    def test_an_abort_mid_sweep_puts_the_duty_back(self):
        mod, sar, swept = self.sweep(aborts=True)
        self.quiet(mod.main)
        self.assertTrue(swept, "the sweep never wrote the duty")
        self.assertEqual(sar(), self.FOUND)

    def test_a_failed_restore_is_a_failed_run(self):
        for fails, status in ((False, 0), (True, 1)):
            with self.subTest(fails=fails):
                mod, _, _ = self.sweep()
                if fails:
                    fail_a_restore(mod)
                self.assertEqual(self.quiet(mod.main), (status, None))


@unittest.skipUnless(POSIX_SIGNALS, "needs POSIX signals")
class CpWatchdogTests(ToolCase):
    """cp_watchdog_timing.py: GPIO3's ICR2, armed for the pump pin's edge,
    goes back on every way out, and a restore step that failed is a failed
    run."""

    FOUND = 0x5

    def capture(self, aborts=False):
        mod = load("cp_watchdog_timing.py")
        clock = Clock()
        mem, word = duty_register(self, mod.GPIO4 + 4096, self.FOUND, mod.GPIO3 + mod.ICR2)
        armed = []

        def hook():
            if aborts and not armed and word() != self.FOUND:
                armed.append(word())
                deliver(signal.SIGTERM)
        clock.hooks.append(hook)
        mod.MEM, mod.time = mem, clock
        set_argv(self, ["cp_watchdog_timing.py", "0.002", "-"])
        return mod, word, armed

    def test_an_abort_mid_capture_puts_icr2_back(self):
        mod, word, armed = self.capture(aborts=True)
        self.quiet(mod.main)
        self.assertTrue(armed, "the capture never armed the edge")
        self.assertEqual(word(), self.FOUND)

    def test_a_failed_restore_is_a_failed_run(self):
        for fails, status in ((False, 0), (True, 1)):
            with self.subTest(fails=fails):
                mod, _, _ = self.capture()
                if fails:
                    fail_a_restore(mod)
                self.assertEqual(self.quiet(mod.main), (status, None))


class FakeMachine:
    """forgectrl, the controller and the kernel as the microstep tools
    reach them, on the fake clock. A job runs until busy_until: meanwhile
    the kernel reads running, the controller reports Run mid-path and
    answers a $ write with error:8 (grblHAL takes one only at rest), and
    forgectrl answers a settings write with 409 (its idle lock). /settings
    holds xy_microsteps or not; a write restarts the controller at the
    mode (cnc/x_mode; an unset key runs at 32); a value posted in the body
    must be a mode, and an empty query parameter unsets the key. Two
    states the kernel alone cannot show: a controller in Hold until
    held_until with the kernel idle, and blips, kernel reads of idle in the
    middle of a job (between two streamed lines). refused counts the
    writes that arrived while the machine moved or held."""

    HOME, REST, MID = (0.0, 0.0), (228.6, 228.6), (114.3, 114.3)

    def __init__(self, clock, root, found):
        self.clock, self.root = clock, root
        self.settings = {"xy_microsteps": found} if found else {}
        self.busy_until, self.held_until, self.blips, self.refused = 0.0, 0.0, 0, 0
        self.dollar, self.legs, self.closed, self.at = {"$12": "0.002"}, [], False, self.HOME
        for rel, value in (("cnc/step_freq", "112640"), ("cnc/ramp_rate", "1"), ("cnc/underruns", "0")):
            put(root, rel, value)
        self.apply()
        clock.hooks.append(self.tick)

    def busy(self):
        return self.clock.t < self.busy_until

    def held(self):
        return self.clock.t < self.held_until

    def run_for(self, seconds):
        self.busy_until = self.clock.t + seconds
        self.tick()

    def hold_for(self, seconds):
        self.held_until = self.clock.t + seconds

    def tick(self):
        idle = not self.busy() or self.blips > 0
        if self.busy() and self.blips:
            self.blips -= 1
        put(self.root, "cnc/state", "idle" if idle else "running")

    def apply(self):
        put(self.root, "cnc/x_mode", self.settings.get("xy_microsteps") or "32")
        self.tick()

    def get(self, path, **kw):
        if path == "/settings":
            return 200, dict(self.settings)
        if path == "/mode":
            return 200, {"controller": "running", "motion": "verified", "pid": 1}
        return 200, {}

    def post(self, path, params=None, data=None, **kw):
        if path != "/settings":
            return 200, {}
        if self.busy():
            self.refused += 1
            return 409, {"error": "machine is not idle - settings are locked"}
        for k, v in (data or {}).items():
            if v not in ("8", "16", "32"):
                return 400, {"error": "not a mode"}
            self.settings[k] = v
        for k, v in (params or {}).items():
            if v == "":
                self.settings.pop(k, None)
            else:
                self.settings[k] = v
        self.apply()
        return 200, {}

    # the controller's port, as arc_tolerance_sweep's Grbl reaches it
    def status(self):
        if self.busy():
            return "Run", self.MID, 12000.0
        if self.held():
            return "Hold", self.MID, 0.0
        return "Idle", self.at, 0.0

    refuse_dollar = False               # an error to a $ write at rest too (an alarm, say)

    def cmd(self, line, timeout=30):
        if line.startswith("$12="):
            if self.busy() or self.held():
                self.refused += 1
                return "error:8\r\n"
            if self.refuse_dollar:
                return "error:9\r\n"
            self.dollar["$12"] = line[4:]
        return "ok\r\n"

    def run_leg(self, gcode, timeout=120):
        self.legs.append(gcode)
        self.at = self.REST if len(self.legs) == 1 else self.HOME
        return 0.0, 1.0, 0.0, self.at

    def close(self):
        self.closed = True

    def setting(self, g, key):
        return None if self.busy() or self.held() else self.dollar.get(key)


@unittest.skipUnless(POSIX_SIGNALS, "needs POSIX signals")
class MicrostepToolTests(ToolCase):
    """The tools that change the XY microstep mode hand back the mode they
    found, an unset key unset again, and arc_tolerance_sweep its $12, the
    head's place and the quiet hold: on every way out, but what needs the
    machine at rest only once it has stopped. A job already queued plays on
    after an Abort, and a machine that never stops gets no such write and
    no move aimed from a position read in motion."""

    RECORD = {"overall": {"x": {"rms": 1, "p2p": 1}, "y": {"rms": 1, "p2p": 1}}, "result": "PASS", "ctrl4": 0}
    NEVER = 1.0e12

    def machine(self, mod, found):
        """The fake machine, wired into the tool and the xy_pattern_accel
        it imports (the shared mode and settle helpers)."""
        clock = Clock()
        root = tempfile.mkdtemp(dir=self.tmp)
        m = FakeMachine(clock, root, found)
        for target in (mod, mod.bench_modules.get("xy_pattern_accel")):
            if target is not None:
                target.time = clock
                if hasattr(target, "forgectrl_get"):
                    target.forgectrl_get, target.forgectrl_post = m.get, m.post
                if hasattr(target, "SYS"):
                    target.SYS = root + "/"
        return m

    def test_xy_pattern_accel_stores_the_mode_it_found_once_the_machine_stops(self):
        # (found, the run is aborted, how long the leg in flight plays on, the mode stored back)
        for found, aborts, plays, put_back in (("", True, 5.0, True), ("16", False, 5.0, True),
                                               ("16", False, self.NEVER, False)):
            with self.subTest(found=found or "unset", aborts=aborts, plays=plays):
                mod = load("xy_pattern_accel.py")
                m = self.machine(mod, found)
                mod.preflight = lambda: (0.0, 0.0)
                mod.data_path = lambda name: os.path.join(self.tmp, name)

                def run_pattern(mode, home):
                    m.run_for(plays)
                    if aborts:
                        deliver(signal.SIGTERM)
                    return dict(self.RECORD, mode=mode)
                mod.run_pattern = run_pattern
                set_argv(self, ["xy_pattern_accel.py", "8"])
                rc, _ = self.quiet(mod.main)
                self.assertEqual(m.refused, 0, "a settings write while the machine moved")
                self.assertEqual(m.settings.get("xy_microsteps", ""), found if put_back else "8")
                if not put_back:
                    self.assertEqual(rc, 1)

    def test_settle_counts_only_still_readings_in_a_row(self):
        # The kernel's state at each reading -> stopped within the timeout.
        for states, stopped in ((["idle", "running"], False), (["idle", "running", "idle", "idle"], True)):
            with self.subTest(states=states):
                mod = load("xy_pattern_accel.py")
                mod.time = Clock()
                reads = iter(states * 100)
                mod.sysfs = lambda rel: next(reads)
                self.assertEqual(self.quiet(mod.settle, None, 30), (stopped, None))

    def test_raster_dry_stores_the_mode_it_found_once_the_queued_raster_ends(self):
        mod = load("raster_dry.py")
        m = self.machine(mod, "16")
        mod.fc_get = lambda path: {}

        def raster(mode):
            # The raster already queued plays on after the Abort, its
            # kernel reading idle for an instant between two lines.
            m.blips = 1
            m.run_for(30.0)
            deliver(signal.SIGTERM)
        mod.raster = raster
        set_argv(self, ["raster_dry.py", "8"])
        self.quiet(mod.main)
        self.assertEqual(m.refused, 0, "a settings write while the raster played")
        self.assertEqual(m.settings.get("xy_microsteps"), "16")

    def test_arc_tolerance_sweep_puts_its_tolerance_head_and_mode_back_at_rest(self):
        class Accel:
            def start(self):
                pass

            def restore(self):
                pass

        circle = {"t_run": 0.0, "t_idle": 1.0, "seconds": 1.0, "cpu_pct": 1.0, "peak_feed": 12000,
                  "min_mid_feed": 12000, "min_free_blocks": 1, "max_free_blocks": 1, "status_samples": 1,
                  "clamped": [], "underruns": [0, 0]}
        # An Abort with 3 s of circle left; one into a circle that never
        # ends; one with the controller left in Hold, its kernel idle; a
        # clean run whose $12 write the controller refuses at rest.
        for plays, holds_on, aborts, refuses, at_rest in ((3.0, 0.0, True, False, True),
                                                          (self.NEVER, 0.0, True, False, False),
                                                          (0.0, self.NEVER, True, False, False),
                                                          (0.0, 0.0, False, True, True)):
            with self.subTest(plays=plays, held=holds_on, aborts=aborts, refuses=refuses):
                mod = load("arc_tolerance_sweep.py")
                m = self.machine(mod, "8")
                holds = []
                mod.Grbl, mod.Accel, mod.grbl_setting = (lambda: m), Accel, m.setting
                mod.Sampler = lambda accel: types.SimpleNamespace(start=lambda: None, stop=lambda: None, samples=[])
                mod.quiet = lambda on: holds.append(on) or {}
                mod.preflight, mod.controller_pid = (lambda: FakeMachine.HOME), (lambda: 1)
                mod.data_path = lambda name: os.path.join(self.tmp, name)

                def run_circle(grbl, pid):
                    m.run_for(plays)
                    m.hold_for(holds_on)
                    m.refuse_dollar = refuses
                    if aborts:
                        deliver(signal.SIGTERM)
                    return dict(circle)
                mod.run_circle = run_circle
                set_argv(self, ["arc_tolerance_sweep.py", "--mode", "16", "0.0001"])
                rc, _ = self.quiet(mod.main)
                self.assertEqual(m.refused, 0, "a $ or settings write while the circle played")
                self.assertEqual(holds[-1:], [False], "the quiet hold was not released")
                self.assertTrue(m.closed)
                if refuses:
                    self.assertEqual((rc, float(m.dollar["$12"])), (1, 0.0001), "a refused $12 write passed")
                elif at_rest:
                    self.assertEqual(float(m.dollar["$12"]), 0.002)
                    self.assertEqual(m.legs[1:], ["G1 X-228.600 Y-228.600 F12000"],
                                     "the head's return was not aimed from where it stands at rest")
                    self.assertEqual(m.settings.get("xy_microsteps"), "8")
                else:
                    self.assertEqual(float(m.dollar["$12"]), 0.0001)
                    self.assertEqual(m.legs[1:], [], "a move aimed from a position read in motion")
                    self.assertEqual(m.settings.get("xy_microsteps"), "16")

    def test_the_head_is_aimed_home_only_from_an_idle_report(self):
        # The controller in Hold mid-circle, its kernel idle: no leg.
        mod = load("arc_tolerance_sweep.py")
        m = self.machine(mod, "8")
        m.hold_for(self.NEVER)
        _, err = self.quiet(mod.return_home, m, FakeMachine.HOME)
        self.assertIsInstance(err, RuntimeError)
        self.assertEqual(m.legs, [])


class FakeDrillGrbl:
    """The controller's port as the live drills talk to it; it records
    what reaches it and answers ok."""

    def __init__(self):
        self.sent, self.log = [], []
        self.s = types.SimpleNamespace(sendall=lambda b: self.sent.append(b))

    def cmd(self, line, timeout=5.0):
        self.sent.append(line)
        return "ok"

    def status(self):
        return "<Idle|MPos:0.000,0.000,0.000>"

    def state(self):
        return "Idle"

    def rt(self, ch):
        self.sent.append(ch)

    def drain(self):
        return ""

    def wait_state(self, want, timeout=60.0, poll=0.1):
        return want


class Reached(Exception):
    """A drill went past its refusal."""


class LiveFireDrillTests(ToolCase):
    """live_fire_drills.py on the host, nothing fired: the exit status is
    the drill's verdict and each verdict holds only what it claims, a stop
    forgectrl did not take is a fail, and a ladder refuses before it arms
    unless its rungs are what it says, printed at the floor its own arm
    loads."""

    MAPPING = {"$30": 1000.0, "$31": 0.0, "$35": 0.0, "$36": 100.0}
    RAW = {"laser_floor_density": "0", "laser_dose_curve": "off"}

    def drill(self, argv, settings=None, mapping=None):
        mod = load("live_fire_drills.py")
        jobs = []
        mapping = dict(self.MAPPING, **(mapping or {}))
        mod.fc_settings = lambda: None if settings is None else dict(settings)
        mod.grbl_setting = lambda g, key: mapping[key]
        mod.CONF = os.path.join(self.tmp, "forgefirm.conf")
        mod.prepare = lambda g: "<Idle>"
        mod.sample_forgectrl = lambda: None
        mod.sample_ir_baseline = lambda seconds=3.0: [0, 0, 0, 0]
        mod.run_and_sample = lambda g, job, **kw: jobs.append(job) or []
        mod.time = Clock()
        set_argv(self, ["live_fire_drills.py"] + argv)
        return mod, jobs

    def printed(self, fn, *args):
        """What fn printed, up to its end or a Reached."""
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            try:
                fn(*args)
            except Reached:
                pass
        return out.getvalue()

    def test_witness_passes_only_when_program_end_closed_a_window_seen_armed(self):
        # (the window read armed in the job, the X-3 polls (1 s apart) it
        # still reads armed, forgectrl's laser_disarm_s (None: its settings
        # unreadable), the config file's) -> status
        cases = (("closed at program end", True, 0, "60", None, 0),
                 ("closed by the 60 s grace", True, 60, "60", None, 1),
                 ("never read armed", False, 0, "60", None, 1),
                 ("never closed", True, 1000, "60", None, 1),
                 ("a grace too short to tell apart", True, 0, "10", None, 2),
                 ("forgectrl's settings unreadable", True, 0, None, None, 2),
                 ("a hand edit forgectrl shows unset", True, 0, "", "5s", 2))
        for name, armed, open_polls, grace, in_file, status in cases:
            with self.subTest(name):
                burn = [{"emission": 5, "ir": [1, 1, 1, 1], "hv": 10, "armed": armed, "pgood": 200},
                        {"emission": 0, "ir": [1, 1, 1, 1], "hv": 0, "armed": armed, "pgood": 200}]
                mod, jobs = self.drill(["witness"], None if grace is None else {"laser_disarm_s": grace})
                put(self.tmp, "forgefirm.conf", "laser_disarm_s = %s\n" % in_file if in_file else "")
                if status != 2:
                    mod.run_and_sample = lambda g, job, b=burn, **kw: list(b)
                polls = []

                def sample(n=open_polls):
                    polls.append(1)                 # the first is the pre-fire sample
                    return {"armed": len(polls) <= n + 1}
                mod.sample_forgectrl = sample
                self.assertEqual(self.quiet(mod.drill_witness, FakeDrillGrbl()), (status, None))
                if status == 2:
                    self.assertEqual(jobs, [], "the drill ran under a grace it could not tell apart")

    def test_hold_passes_only_when_the_window_closes_with_the_job_held(self):
        # (what the feed hold reached within 5 s, the states after it, the
        # polls that read the window armed) -> status
        cases = (("held throughout", "Hold", ["Hold"] * 10, 4, 0),
                 ("the hold never took", "Run", ["Idle"] * 10, 4, 1),
                 ("the hold took only after 5 s", "Run", ["Hold"] * 10, 4, 1),
                 ("the job left the hold", "Hold", ["Hold", "Run"] + ["Idle"] * 10, 4, 1),
                 ("the window never read armed", "Hold", ["Hold"] * 10, 0, 1))
        for name, reached, states, armed_polls, status in cases:
            with self.subTest(name):
                mod, _ = self.drill(["hold"])
                g = FakeDrillGrbl()
                g.wait_state = lambda want, timeout=60.0, poll=0.1, r=reached: "Run" if want == "Run" else r
                seq = iter(states)
                g.state = lambda seq=seq: next(seq, "Idle")
                polls = []

                def sample(n=armed_polls):
                    polls.append(1)
                    return {"armed": len(polls) <= n}
                mod.sample_forgectrl = sample
                self.assertEqual(self.quiet(mod.drill_hold, g), (status, None))

    def test_a_ladder_or_cut_that_never_lit_is_a_fail(self):
        dark = [{"t": 1.0, "emission": 0, "hv": 0, "ir": [1, 1, 1, 1], "fire_watch": None}]
        lit = [{"t": 1.0, "emission": 3, "hv": 12, "ir": [1, 1, 1, 1], "fire_watch": None}]
        for drill, argv in (("ircut", []), ("pthresh", []), ("dladder", ["20"])):
            for samples, status in ((dark, 1), (lit, 0)):
                with self.subTest(drill=drill, lit=samples is lit):
                    mod, _ = self.drill([drill] + argv, dict(self.RAW))
                    mod.run_and_sample = lambda g, job, s=samples, **kw: list(s)
                    self.assertEqual(self.quiet(getattr(mod, "drill_" + drill), FakeDrillGrbl()), (status, None))

    def pcurve(self, lines_run, lit):
        """drill_pcurve on fakes: its first lines_run lines run their
        length, sampled lit or dark, and the next ends at once (canceled).
        Returns (the result, the lines it started)."""
        mod, _ = self.drill(["pcurve"], dict(self.RAW))
        clock = mod.time
        samples = []

        class Sampler:
            local, errors = False, 0

            def __init__(self, hz):
                self.samples = samples

            def start(self):
                pass

            def stop(self):
                pass

            def rate(self):
                return 0.0
        mod.Sampler = Sampler
        line_s = mod.PCURVE_LEN / mod.PCURVE_FEED * 60.0
        ends = []

        def wait_state(want, timeout=60.0, poll=0.1):
            if want == "Idle" and timeout > 30.0:         # the end of a rung's line
                ends.append(1)
                if len(ends) <= lines_run:
                    t0 = clock.t
                    clock.t += line_s
                    samples.append({"t": t0 + line_s / 2, "lon": 7 if lit else 0, "hv": 10, "tp": 100,
                                    "tpd": 1, "wt1": 500, "wt2": 500, "pt": 300})
            return want
        g = FakeDrillGrbl()
        g.wait_state = wait_state
        with mock.patch.dict(os.environ, {"FORGETEST_BENCH_DATA": self.tmp}):
            return self.quiet(mod.drill_pcurve, g), len(ends)

    def test_pcurve_stopped_partway_is_a_fail(self):
        self.assertEqual(self.pcurve(lines_run=1, lit=True), ((1, None), 2))

    def test_pcurve_with_nothing_lit_is_a_fail(self):
        for lit, status in ((True, 0), (False, 1)):
            with self.subTest(lit=lit):
                self.assertEqual(self.pcurve(lines_run=99, lit=lit)[0], (status, None))

    def test_dpatch_runs_only_with_s_straight_onto_density_and_prints_at_the_arms_floor(self):
        def stop(*a, **kw):
            raise Reached()
        # The curve in force, a floor, or forgectrl's settings unreadable:
        # refused before it reads the controller or arms.
        for name, settings in (("the curve in force", {"laser_floor_density": "0"}),
                               ("a floor", {"laser_floor_density": "10", "laser_dose_curve": "off"}),
                               ("settings unreadable", None)):
            with self.subTest(name):
                mod, _ = self.drill(["dpatch"], settings)
                mod.read_mapping = stop
                self.assertEqual(self.quiet(mod.drill_dpatch, FakeDrillGrbl()), (2, None))
        # S straight onto density: row B prints at the floor its arm loads
        # (0, under a stale $35 of 10): S300 is 38/127.
        mod, _ = self.drill(["dpatch"], dict(self.RAW), mapping={"$35": 10.0})
        mod.Sampler = stop
        self.assertIn("30% density (level 29.92%)", self.printed(mod.drill_dpatch, FakeDrillGrbl()))

    def test_the_ladders_print_the_density_their_own_arm_fires(self):
        # $$ still reports the last arm's $35; this run's arm loads the
        # floor key's. pthresh and pcurve run at floor 0 under a stale 10,
        # dladder at the unset key's default 10 under a stale 0.
        def stop(*a, **kw):
            raise Reached()
        mod, _ = self.drill(["pthresh"], dict(self.RAW), mapping={"$35": 10.0})
        mod.prepare = stop
        self.assertIn("S20   density 1.6%", self.printed(mod.drill_pthresh, FakeDrillGrbl()))   # 2/127, not 14/127
        mod, _ = self.drill(["pcurve"], dict(self.RAW), mapping={"$35": 10.0})
        mod.Sampler = stop
        self.assertIn("S10    density 0.79%", self.printed(mod.drill_pcurve, FakeDrillGrbl()))
        mod, _ = self.drill(["dladder", "20"], {"laser_dose_curve": "off"}, mapping={"$35": 0.0})
        mod.prepare = stop
        self.assertIn("S10    density 0.1024", self.printed(mod.drill_dladder, FakeDrillGrbl()))

    def test_the_exit_status_is_the_drills_verdict(self):
        # Samples, a record or nothing are no verdict: a fail.
        mod, _ = self.drill(["witness"])
        mod.Grbl = lambda host, port: FakeDrillGrbl()
        for returned, status in (([{"t": 1.0}], 1), (None, 1), (0, 0), (2, 2)):
            with self.subTest(returned=returned):
                mod.drill_witness = lambda g: returned
                self.assertEqual(self.quiet(mod.main), (status, None))

    def test_expstop_fails_when_forgectrl_does_not_take_the_stop(self):
        # The job goes dark on its own right after a refused stop: that is
        # no stop of this drill's, and never a PASS.
        mod, _ = self.drill(["expstop"])
        posted = []

        def sample():
            dark = bool(posted)
            return {"emission": 0 if dark else 5, "kstate": "idle" if dark else "running", "armed": False}
        mod.sample_forgectrl = sample
        mod.post_ctrl = lambda action: (posted.append(action), (403, "forbidden"))[1]
        mod.panel_token, mod.get_json = (lambda: "tok"), (lambda path: {})
        self.assertEqual(self.quiet(mod.drill_expstop, FakeDrillGrbl()), (1, None))
        self.assertEqual(posted, ["stop"])

    def test_a_drill_that_stops_waiting_cancels_its_job(self):
        # The button waits 300 s, longer than a drill waits for its job's
        # end: a drill that gives up soft-resets first, and fails.
        mod = load("live_fire_drills.py")
        mod.time = Clock()
        g = FakeDrillGrbl()
        mod.sample_forgectrl = lambda: {"emission": 0, "armed": False}
        samples, _ = self.quiet(lambda: mod.run_and_sample(g, ["G1 X1"], overall_timeout=200))
        self.assertTrue(mod.gave_up(samples))
        self.assertEqual(g.sent[-1], mod.SOFT_RESET)
        # A job that ends sends no reset: the window must close by itself.
        g = FakeDrillGrbl()
        lit = iter([5, 5] + [0] * 1000)
        mod.sample_forgectrl = lambda: {"emission": next(lit), "armed": True}
        samples = mod.run_and_sample(g, ["G1 X1"])
        self.assertFalse(mod.gave_up(samples))
        self.assertNotIn(mod.SOFT_RESET, g.sent)
        # witness fails a job that did not end, whatever its samples say:
        # the reset closed the window, not the program end.
        mod, _ = self.drill(["witness"], {"laser_disarm_s": "60"})
        clean = mod.Samples([{"emission": 5, "ir": [1] * 4, "hv": 9, "armed": True, "pgood": 200},
                             {"emission": 0, "ir": [1] * 4, "hv": 0, "armed": True, "pgood": 200}])
        clean.gave_up = True
        mod.run_and_sample = lambda g, job, **kw: clean
        mod.sample_forgectrl = lambda: {"armed": False}
        self.assertEqual(self.quiet(mod.drill_witness, FakeDrillGrbl()), (1, None))
        # expstop that never sees the burn.
        mod, _ = self.drill(["expstop"])
        mod.panel_token = lambda: "tok"
        mod.sample_forgectrl = lambda: {"emission": 0}
        g = FakeDrillGrbl()
        self.assertEqual(self.quiet(mod.drill_expstop, g), (1, None))
        self.assertEqual(g.sent[-1], mod.SOFT_RESET)

    @unittest.skipUnless(POSIX_SIGNALS, "needs POSIX signals")
    def test_the_abort_sends_the_soft_reset_before_anything_else(self):
        # live_fire_drills.py: the reset, then the cleanup's M5, then the
        # exit; a second Abort in the cleanup sends the reset again and
        # does not cut the cleanup short.
        mod, _ = self.drill(["witness"])
        g = FakeDrillGrbl()
        mod.Grbl = lambda host, port: g

        def drill(g):
            g.cmd("G1 X10")
            deliver(signal.SIGTERM)
            g.cmd("not reached")
        mod.drill_witness = drill
        real_cmd = g.cmd

        def cmd(line, timeout=5.0):
            if line == "M5":
                deliver(signal.SIGTERM)
            return real_cmd(line, timeout)
        g.cmd = cmd
        _, err = self.quiet(mod.main)
        self.assertEqual(getattr(err, "code", None), 128 + signal.SIGTERM)
        self.assertEqual(g.sent, ["G1 X10", mod.SOFT_RESET, mod.SOFT_RESET, "M5"])
        # resume_dark_lead.py: the reset first.
        rdl = load("resume_dark_lead.py")
        sent = []
        rdl.end_on_signals(types.SimpleNamespace(s=types.SimpleNamespace(sendall=sent.append)))
        with self.assertRaises(SystemExit) as cm:
            deliver(signal.SIGHUP)
        self.assertEqual((cm.exception.code, sent), (128 + signal.SIGHUP, [rdl.SOFT_RESET]))

    def test_expstop_refuses_from_a_lan_host_before_it_arms(self):
        # forgectrl takes the stop only from loopback: from a LAN host it
        # would be refused after the burn began.
        mod, _ = self.drill(["expstop"])
        mod.HOST = "192.0.2.10"
        posted = []
        mod.post_ctrl = lambda action: posted.append(action) or (200, "")
        mod.panel_token = lambda: "tok"
        g = FakeDrillGrbl()
        self.assertEqual(self.quiet(mod.drill_expstop, g), (2, None))
        self.assertEqual((g.sent, posted), ([], []))

    def test_dladder_runs_only_with_the_curve_off_at_the_period_the_machine_holds(self):
        # (forgectrl's settings, the config file, the drill runs)
        cases = (({"laser_dose_curve": "off", "laser_pulse_ticks": "40"}, None, False),
                 ({"laser_dose_curve": "off", "laser_pulse_ticks": "20"}, None, True),
                 ({"laser_pulse_ticks": "20"}, None, False),                      # the curve bends every rung
                 ({"laser_dose_curve": "off"}, None, True),                       # unset: the driver's 20
                 ({"laser_dose_curve": "off"}, "laser_pulse_ticks = 2000", False))  # forgectrl shows it as unset
        conf = os.path.join(self.tmp, "forgefirm.conf")
        for settings, text, runs in cases:
            with self.subTest(settings=settings, conf=text):
                if text is None:
                    if os.path.exists(conf):
                        os.remove(conf)
                else:
                    put(self.tmp, "forgefirm.conf", text)
                mod, jobs = self.drill(["dladder", "20"], settings)
                rc, err = self.quiet(mod.drill_dladder, FakeDrillGrbl())
                self.assertIsNone(err)
                self.assertEqual(bool(jobs), runs)
                if not runs:
                    self.assertEqual(rc, 2)
                if text is not None:
                    self.assertEqual(tree(self.tmp)["forgefirm.conf"], text, "the drill wrote a setting")

    def test_the_level_ladders_run_only_while_s_maps_straight_onto_density(self):
        # A floor lifts every rung onto it (an unset key is the driver's
        # 10), and the dose curve bends S: the drill refuses before it
        # reads the controller or arms.
        def reached(g):
            raise Reached()
        cases = ((("10", "off"), False), ((None, "off"), False), (("0", None), False), (("0", "off"), True))
        mod, _ = self.drill(["pthresh"])
        mod.read_mapping = reached
        for drill in ("pthresh", "pcurve"):
            for (floor, curve), runs in cases:
                with self.subTest(drill=drill, floor=floor, curve=curve):
                    settings = {}
                    if floor is not None:
                        settings["laser_floor_density"] = floor
                    if curve:
                        settings["laser_dose_curve"] = curve
                    mod.fc_settings = lambda: dict(settings)
                    rc, err = self.quiet(getattr(mod, "drill_" + drill), FakeDrillGrbl())
                    if runs:
                        self.assertIsInstance(err, Reached)
                    else:
                        self.assertEqual((rc, err), (2, None))


@unittest.skipUnless(os.name == "posix" and shutil.which("sh"), "needs a POSIX shell")
class ShellDrillTests(ToolCase):
    """fdscan.sh and netblip.sh with the board's commands faked on PATH."""

    def fake_bin(self, scripts):
        d = tempfile.mkdtemp(dir=self.tmp)
        for name, body in scripts.items():
            path = os.path.join(d, name)
            with open(path, "w") as f:
                f.write("#!/bin/sh\n%s\n" % body)
            os.chmod(path, 0o755)
        return dict(os.environ, PATH=d + os.pathsep + os.environ.get("PATH", ""))

    # The board's processes: pid -> (command line, holds /dev/glowforge).
    PROCS = {"555": ("media-ctl -p", True),
             "650": ("vi /usr/sbin/gfcloud.py", False),
             "777": ("python3 /usr/sbin/gfcloud.py", True),
             "888888": ("curl -s http://127.0.0.1/update/check", False),
             "999999": ("/usr/bin/grblHAL_glowforge", True)}

    def fdscan(self, children):
        """fdscan.sh's status with forgectrl (4242) and these children of
        it, on a faked process table; pgrep -f lists every command line
        holding gfcloud.py, children or not."""
        proc = tempfile.mkdtemp(dir=self.tmp)
        for pid, (cmdline, holds) in self.PROCS.items():
            put(proc, pid + "/cmdline", cmdline.replace(" ", "\0") + "\0")
            put(proc, pid + "/comm", cmdline.split()[0].rsplit("/", 1)[-1][:15])
            os.makedirs(os.path.join(proc, pid, "fd"))
            os.symlink("/dev/glowforge" if holds else "/dev/null", os.path.join(proc, pid, "fd", "3"))
        env = self.fake_bin({
            "pidof": '[ "$1" = forgectrl ] && echo 4242 && exit 0; exit 1',
            "pgrep": 'case "$1" in -P) [ "$2" = 4242 ] && echo %s;; -f) echo 650; echo 777;; esac; exit 0'
                     % children,
            "curl": "exit 0", "usleep": "exit 0"})
        env.update(FDSCAN_TICKS="3", FDSCAN_PROC=proc)
        r = subprocess.run(["sh", os.path.join(BENCH, "fdscan.sh"), "token"], env=env,
                           capture_output=True, text=True, timeout=60)
        return r.returncode, r.stdout

    def test_fdscan_passes_only_with_a_helper_seen_and_none_holding_the_device(self):
        # The controller (999999) is always a child of forgectrl on a board,
        # holds the device by design and does not count; a helper does, and
        # fails the scan when it holds the device (555).
        for children, status in (("999999", 1), ("999999 888888", 0), ("999999 888888 555", 1)):
            with self.subTest(children=children):
                rc, out = self.fdscan(children)
                self.assertEqual(rc, status, out)

    def test_fdscan_in_cloud_mode_takes_the_cloud_client_for_the_controller(self):
        # No GRBL controller; the cloud client (777) is the child meant to
        # hold the device and is no helper, and a process that is no child
        # of forgectrl (650) is never taken for the controller.
        for children, status in (("777", 1), ("777 888888", 0)):
            with self.subTest(children=children):
                rc, out = self.fdscan(children)
                self.assertEqual(rc, status, out)

    def blip(self, ip_body, began):
        """netblip.sh on fake commands, SIGTERM once began() holds; returns
        resolv.conf after it ended and the ip log."""
        log = os.path.join(self.tmp, "ip.log")
        resolv = os.path.join(self.tmp, "resolv.conf")
        put(self.tmp, "resolv.conf", "nameserver 192.0.2.53")
        env = self.fake_bin({"netstat": "echo 'tcp 0 0 192.0.2.10:40000 203.0.113.7:443 ESTABLISHED'",
                             "ip": ip_body % shlex.quote(log)})
        env["NETBLIP_RESOLV"] = resolv
        p = subprocess.Popen(["sh", os.path.join(BENCH, "netblip.sh"), "60"], env=env,
                             stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
        self.addCleanup(lambda: p.poll() is None and p.kill())
        deadline = real_time.monotonic() + 10
        while not began(log, resolv):
            self.assertLess(real_time.monotonic(), deadline, "the blip never began")
            real_time.sleep(0.01)
        p.send_signal(signal.SIGTERM)
        p.communicate(timeout=10)
        with open(log) as f:
            return tree(self.tmp)["resolv.conf"], f.read()

    def test_netblip_puts_the_routes_and_resolution_back_on_sigterm(self):
        resolv, log = self.blip('echo "$@" >> %s',
                                lambda log, resolv: "127.0.0.2" in tree(self.tmp)["resolv.conf"])
        self.assertEqual(resolv, "nameserver 192.0.2.53")
        self.assertIn("route del blackhole 203.0.113.7/32", log)

    def test_netblip_deletes_a_route_whose_add_a_signal_interrupted(self):
        # The TERM lands while ip route add runs (a slow ip).
        resolv, log = self.blip('echo "$@" >> %s; case "$*" in *add*) sleep 0.3;; esac',
                                lambda log, resolv: os.path.exists(log))
        self.assertEqual(resolv, "nameserver 192.0.2.53")
        self.assertIn("route del blackhole 203.0.113.7/32", log)


if __name__ == "__main__":
    unittest.main()
