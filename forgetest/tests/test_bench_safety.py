# Copyright 2026 514 LLC d/b/a OpenGlow
# Written by Scott Wiederhold
# https://community.openglow.org
# SPDX-License-Identifier:    MIT

"""The bench tools that drive the loop heater, on the host: the heater
goes off and the settings come back on every way out, and the tools refuse
before heating when they must. Each tool runs as written against a fake
sysfs tree, a fake forgectrl API and a fake clock, so nothing sleeps. Only
the heater and settings paths: what the tools measure and report is theirs.

heater_pwm is the kernel module's: 0 is off, 65535 full power, and a write
above zero holds only for the lease (heater_lease_ms); heater_lapses counts
the leases that ran out with the heater on."""
import contextlib
import errno
import io
import os
import shutil
import signal
import sys
import tempfile
import threading
import time as real_time
import types
import unittest
from unittest import mock

REPO = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
BENCH = os.path.join(REPO, "scripts", "bench")
HEATER_FULL = 65535


def load(script):
    """The tool as a module of its own, run from its source (no bytecode
    is written next to it), with gfbench importable and aimed at
    127.0.0.1. Every name the tool binds is its own, so a case patches the
    module it loaded and nothing else."""
    path = os.path.join(BENCH, script)
    mod = types.ModuleType("bench_" + script[:-3])
    mod.__file__ = path
    saved = (list(sys.path), sys.modules.get("gfbench"), os.environ.get("GF_HOST"), sys.dont_write_bytecode)
    sys.path.insert(0, BENCH)
    sys.modules.pop("gfbench", None)
    os.environ["GF_HOST"] = "127.0.0.1"
    sys.dont_write_bytecode = True
    try:
        with open(path, encoding="utf-8") as f:
            exec(compile(f.read(), path, "exec"), mod.__dict__)
    finally:
        sys.path[:] = saved[0]
        if saved[1] is None:
            sys.modules.pop("gfbench", None)
        else:
            sys.modules["gfbench"] = saved[1]
        if saved[2] is None:
            os.environ.pop("GF_HOST", None)
        else:
            os.environ["GF_HOST"] = saved[2]
        sys.dont_write_bytecode = saved[3]
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
        for sig in (signal.SIGTERM, getattr(signal, "SIGHUP", None)):
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
            except (Exception, SystemExit) as e:
                return None, e


ARGV = ["critical_tier_drill.py"]


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
    """offset_probe.py: one actuator at a time, the heater among them, the
    flow watch off for the run and every value put back on the way out."""

    START = {"thermal/exhaust_pwm": "0", "thermal/intake_pwm": "0", "head/air_assist_pwm": "204",
             "head/purge_air": "1", "thermal/heater_pwm": "0", "thermal/water_pump_on": "1",
             "thermal/tec_on": "0", "leds/lid_led/brightness": "100"}
    HEAT = 26214                # the probe's heater step

    def probe(self, start=None, **kw):
        mod = load("offset_probe.py")
        clock = Clock()
        root = tempfile.mkdtemp(dir=self.tmp)
        for rel, value in dict(self.START, **(start or {})).items():
            put(root, rel, value)
        cool = Cooling(clock, os.path.join(root, "thermal", "heater_pwm"), **dict({"off_after": 2.0}, **kw))
        exits = []
        mod.SYSFS, mod.LEDS = root + "/", os.path.join(root, "leds") + "/"
        mod.time, mod.Sampler = clock, StubSampler
        mod.forgectrl_get, mod.forgectrl_post = cool.get, cool.post
        mod.atexit = types.SimpleNamespace(register=exits.append)
        argv = sys.argv
        self.addCleanup(setattr, sys, "argv", argv)
        sys.argv = ["offset_probe.py"]
        env = mock.patch.dict(os.environ, {"FORGETEST_BENCH_DATA": self.tmp})
        env.start()
        self.addCleanup(env.stop)
        return mod, cool, root, exits

    def interrupt_at_heat(self, cool, act):
        """act() once, at the first sleep with the heater at the probe's duty."""
        done = []

        def hook():
            if not done and cool.heater() == self.HEAT:
                done.append(1)
                act()
        cool.clock.hooks.append(hook)

    def test_every_exit_puts_the_heater_and_the_flow_watch_back(self):
        def boom():
            raise RuntimeError("a write failed")
        exits = {"normal": None, "exception": boom,
                 "SIGTERM": lambda: deliver(signal.SIGTERM), "SIGHUP": lambda: deliver(signal.SIGHUP)}
        for name, act in exits.items():
            with self.subTest(name):
                mod, cool, root, atexit_fns = self.probe()
                if act:
                    self.interrupt_at_heat(cool, act)
                self.quiet(mod.main)
                self.assertIn(self.HEAT, [h for _, h, _ in cool.trace], "the probe never heated")
                self.assertEqual(tree(root), self.START, "a value was not put back")
                for fn in reversed(atexit_fns):     # the interpreter's exit
                    fn()
                self.assertEqual(cool.settings, Cooling.SETTINGS)

    def test_a_flow_watch_that_stays_on_stops_the_probe_before_any_write(self):
        mod, cool, root, atexit_fns = self.probe(off_after=None)
        self.assertEqual(self.quiet(mod.main), (2, None))
        self.assertEqual(tree(root), self.START)
        for fn in reversed(atexit_fns):
            fn()
        self.assertEqual(cool.settings, Cooling.SETTINGS)

    def test_the_probe_refuses_without_the_pump_or_with_the_heater_on(self):
        for rel, value in (("thermal/water_pump_on", "0"), ("thermal/heater_pwm", "1000")):
            with self.subTest(rel):
                mod, cool, root, _ = self.probe(start={rel: value})
                self.assertEqual(self.quiet(mod.main), (2, None))
                self.assertNotIn(self.HEAT, [h for _, h, _ in cool.trace])


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


if __name__ == "__main__":
    unittest.main()
