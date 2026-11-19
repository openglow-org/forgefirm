# Copyright 2026 514 LLC d/b/a OpenGlow
# Written by Scott Wiederhold
# https://community.openglow.org
# SPDX-License-Identifier:    MIT

"""wait_hv_off against a scripted charge-pump readback: a chain that
releases inside the window passes, a chain held past it is the refusal,
and a chain already off costs no wait. The dead man's close judges of the
heat sources and the head: each passes only when the close had something
to undo and undid it, with no lease lapse. The drills over a scripted
kernel (FakeKernel, on a fake clock): every phase that unlocks the latch
(B, U, R, K3, L: the list is held to the suite's source) is refused with
the chain closed or the charge pump fed,
reads the chain in every sample, relocks and fails at a press mid-run, at
each moment it samples, and has locked the latch itself when it lets the
pulse device go, whether it returns or a read fails under it; R's drain
starts right after a lock; a run sample passes only dark behind both
latches with the loop still open; a loop that closes mid-run relocks and
fails; the largest gap between two guarded samples is kept;
fire-line and resume-lead lock, halt and stop before the loop is closed
again and fail on a loop left open; the final close at idle must relock by
itself, and its unlock is refused with the charge pump fed and relocked on
a button latch that does not read set; the drill ends halted and stopped,
never disabled. With the latch
locked: K2 fails a resume that never ran, the backward run at the boundary
is declared streaming, and the dead man's close turns the head's emitters
off on every way out. The judges that a run played: a resume over its
window, a backward run to its waypoint without the dead stop and, over FIRE
bytes, dark; the backtrack tail as the kernel counts it; the PIC settle
handed back as found."""
import collections
import contextlib
import errno
import inspect
import os
import shutil
import tempfile
import time
import types
import unittest
from unittest import mock

from forgetest.suite import kernel


class Ctx:
    def __init__(self):
        self.evidence = {}
        self.lines = []

    def sleep(self, s):
        time.sleep(s)

    def log(self, fmt, *a):
        self.lines.append(fmt % a)


def scripted(alive_for_s):
    t0 = time.time()

    def rd(path):
        if path == "cnc/charge_pump_alive":
            return "1" if time.time() - t0 < alive_for_s else "0"
        if path == "cnc/state":
            return "idle"
        return None
    return rd


class WaitHvOffTests(unittest.TestCase):
    def setUp(self):
        self._rd = kernel.rd

    def tearDown(self):
        kernel.rd = self._rd

    def test_release_inside_the_window_passes(self):
        kernel.rd = scripted(0.3)
        ctx = Ctx()
        t0 = time.time()
        self.assertIsNone(kernel.wait_hv_off(ctx, timeout_s=2.0))
        self.assertGreaterEqual(time.time() - t0, 0.25)
        self.assertTrue(any("released" in l for l in ctx.lines))
        self.assertGreaterEqual(ctx.evidence["hv_release_s"][0], 0.25)

    def test_chain_held_past_the_window_is_the_refusal(self):
        kernel.rd = scripted(99)
        ctx = Ctx()
        why = kernel.wait_hv_off(ctx, timeout_s=0.3)
        self.assertIn("charge_pump_alive=1", why)
        self.assertTrue(any("still held" in l for l in ctx.lines))

    def test_chain_already_off_costs_no_wait(self):
        kernel.rd = scripted(0)
        ctx = Ctx()
        t0 = time.time()
        self.assertIsNone(kernel.wait_hv_off(ctx, timeout_s=2.0))
        self.assertLess(time.time() - t0, 0.1)
        self.assertEqual(ctx.lines, [])


GPIO_LISTING = """gpiochip5: GPIOs 160-191, parent: platform/20b0000.gpio, 20b0000.gpio:
 gpio-168 (                    |water-pump-gpio     ) out hi
 gpio-170 (                    |tec-gpio            ) out %s
gpiochip6: GPIOs 192-223, parent: platform/20b4000.gpio, 20b4000.gpio:
 gpio-201 (                    |water-heater-gpio   ) out %s
"""


class DeadmanThermalTests(unittest.TestCase):
    ON = {"heater_pwm": "65535", "tec_on": "1", "heater_lapses": "0", "heater_pin": "hi", "tec_pin": "hi"}
    OFF = {"heater_pwm": "0", "tec_on": "0", "heater_lapses": "0", "heater_pin": "lo", "tec_pin": "lo"}

    def test_the_pins_are_read_from_the_gpio_listing(self):
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "gpio")
            with open(path, "w") as f:
                f.write(GPIO_LISTING % ("lo", "hi"))
            self.assertEqual(kernel.gpio_levels(path), {"heater": "hi", "tec": "lo"})
            self.assertEqual(kernel.gpio_levels(os.path.join(d, "absent")), {"heater": None, "tec": None})

    def test_the_close_must_take_both_heat_sources_off_with_no_lapse(self):
        self.assertIsNone(kernel.deadman_thermal_why(self.ON, self.OFF))
        for name, before, after in (
                ("the heater never on", dict(self.ON, heater_pin="lo"), self.OFF),
                ("the duty left on", self.ON, dict(self.OFF, heater_pwm="65535")),
                ("the heater pin left high", self.ON, dict(self.OFF, heater_pin="hi")),
                ("the TEC left on", self.ON, dict(self.OFF, tec_on="1", tec_pin="hi")),
                ("off by the lease", self.ON, dict(self.OFF, heater_lapses="1"))):
            with self.subTest(name):
                self.assertIsNotNone(kernel.deadman_thermal_why(before, after))

    def test_a_tec_lapse_at_the_close_is_not_the_switch(self):
        before = dict(self.ON, tec_lapses="0")
        self.assertIsNone(kernel.deadman_thermal_why(before, dict(self.OFF, tec_lapses="0")))
        self.assertIsNotNone(kernel.deadman_thermal_why(before, dict(self.OFF, tec_lapses="1")))


class DeadmanHeadTests(unittest.TestCase):
    LIT = {"z_enable": "0", "z_current": "0", "measure_laser": "1", "uv_led": "64"}
    SAFE = {"z_enable": "1", "z_current": "1", "measure_laser": "0", "uv_led": "0"}

    def test_the_close_must_take_a_lit_driven_head_to_its_safe_state(self):
        self.assertIsNone(kernel.deadman_head_why(self.LIT, self.SAFE))
        for name, before, after in (
                ("the measuring laser never lit", dict(self.LIT, measure_laser="0"), self.SAFE),
                ("the UV LED never lit", dict(self.LIT, uv_led="0"), self.SAFE),
                ("the lens never at run current", dict(self.LIT, z_current="1"), self.SAFE),
                ("the lens driver left enabled", self.LIT, dict(self.SAFE, z_enable="0")),
                ("the lens left at run current", self.LIT, dict(self.SAFE, z_current="0")),
                ("the measuring laser left on", self.LIT, dict(self.SAFE, measure_laser="1")),
                ("the UV LED left on", self.LIT, dict(self.SAFE, uv_led="64"))):
            with self.subTest(name):
                self.assertIsNotNone(kernel.deadman_head_why(before, after))


class FakeClock:
    """The suite's time module for a scripted drill: the clock moves only
    when the drill sleeps, by at least a millisecond a sleep, and when it
    reads the kernel (FakeKernel.READ_S a read)."""

    def __init__(self):
        self.t = 100.0

    def monotonic(self):
        return self.t

    time = monotonic

    def sleep(self, s):
        self.t += max(s, 0.001)


class DrillCtx:
    def __init__(self, k=None):
        self.evidence = {}
        self.lines = []
        self.kernel = k

    def log(self, fmt, *a):
        self.lines.append(fmt % a if a else fmt)

    def check(self, cond, msg, *a):
        if not cond:
            raise kernel.Failed(msg % a if a else msg)

    def checkpoint(self):
        pass

    def sleep(self, s):
        kernel.time.sleep(s)

    @contextlib.contextmanager
    def takeover(self):
        yield

    def act(self, channel, state, until=None, timeout=None, text="", fail=True):
        """The remote-interlock loop opened or closed by the bench, recorded
        among the kernel's writes (an action's order against the safe end)."""
        k = self.kernel
        k.writes.append(("act", "%s %s" % (channel, state)))
        if channel == "interlock" and not (state == "close" and k.loop_stuck):
            k.loop_open = state == "open"
        if until():
            return 0.0
        if fail:
            raise kernel.Failed("%s %s was not seen" % (channel, state))
        return None


class FakeKernel:
    """The pulse engine and the safety chain as the drills read them through
    sysfs, on the drill's FakeClock (each sysfs read costs READ_S).

    A run plays the program's bytes at step_freq: cnc/run, and a resume of 0,
    forward from where the program stands; a positive resume forward with
    that many bytes held dark; a negative one backward to its waypoint with
    the laser bits masked, refused past max_backtrack (what has played less
    the backward run's tail). FIRE (laser_enable) is bit 4 of the byte
    playing while the run start restored the drive (no holder held the
    latch then; any lock takes it away again). A stop decelerates for
    DECEL_S with FIRE high impedance; a halt ends a run at once and refuses
    an underrun; the end of the data is idle, or an underrun under
    streaming, which a stop acknowledges. The pulse device's final close
    halts the engine and, with close_relocks, locks both holders.

    The chain is held open at the loop (interlock_circuit bit 4, switches 5
    and 6) while loop_open. LASER_ON is FIRE with both latches clear (the
    button latch, bit 2, and the interlock latch). Events land at a read of
    interlock_circuit or button_latch at which their predicate (k -> bool)
    first holds: press (the button latch clears, only with no holder),
    loop_closes, laser_on (LASER_ON active), stall (the read takes
    STALL_S), blind (from then on every other attribute is unreadable).
    pump_alive: something else feeds the charge-pump watchdog. reads counts
    the reads of each attribute; closed_unlocked the closes of the pulse
    device that found the latch unlocked."""

    READ_S = 0.0005
    DECEL_S = 0.04
    STALL_S = 0.05

    def __init__(self, chain_open=True, close_relocks=True, press=None, loop_closes=None, laser_on=None,
                 stall=None, resume_plays=True, loop_stuck=False, pump_alive=False, blind=None):
        self.clock = None
        self.loop_open = chain_open
        self.close_relocks = close_relocks
        self.events = {"press": press, "loop_closes": loop_closes, "laser_on": laser_on, "stall": stall,
                       "blind": blind}
        self.pump_alive = pump_alive
        self.blinded = False
        self.reads = collections.Counter()
        self.closed_unlocked = 0
        self.resume_plays = resume_plays
        self.loop_stuck = loop_stuck
        self.holders = {"cnc/laser_latch": 1, "cnc/laser_latch_cooling": 1}
        self.button = 1
        self.laser_forced = False
        self.data = b""
        self.processed = 0
        self.step_freq = 10000
        self.streaming = 0
        self.underruns = 0
        self.idle_state = "idle"
        self.fire_restored = False
        self.run = None
        self.stops = 0
        self.writes = []
        self.attrs = {}

    # -- the engine --------------------------------------------------------
    def locked(self):
        return any(self.holders.values())

    def _start(self, direction, end, lead_end=0):
        self.run = {"t0": self.clock.t, "p0": self.processed, "dir": direction, "end": end,
                    "lead_end": lead_end, "stop_t": None}
        self.fire_restored = not self.locked()

    def _pos(self, t):
        r = self.run
        played = int((t - r["t0"]) * self.step_freq)
        p = r["p0"] + r["dir"] * played
        return min(p, r["end"]) if r["dir"] > 0 else max(p, r["end"])

    def _settle(self):
        r = self.run
        if r is None:
            return
        t = self.clock.t
        if r["stop_t"] is not None:
            if t >= r["stop_t"] + self.DECEL_S:
                self.processed = self._pos(r["stop_t"] + self.DECEL_S)
                self.run, self.idle_state = None, "idle"
            return
        if self._pos(t) == r["end"]:
            self.processed = r["end"]
            self.run = None
            if r["dir"] > 0 and self.streaming:
                self.idle_state = "underrun"
                self.underruns += 1
            else:
                self.idle_state = "idle"

    def moving(self):
        """+1 or -1 while a run plays (not decelerating), else 0."""
        self._settle()
        r = self.run
        return 0 if r is None or r["stop_t"] is not None else r["dir"]

    def fire(self):
        if self.moving() <= 0 or not self.fire_restored:
            return False
        p = self._pos(self.clock.t)
        return p >= self.run["lead_end"] and p < len(self.data) and bool(self.data[p] & 0x10)

    def laser_on(self):
        return self.laser_forced or (self.fire() and not self.button and not self.loop_open)

    def pos(self):
        self._settle()
        p = self.processed if self.run is None else self._pos(self.clock.t)
        return (0, 0, 0, p, len(self.data))

    def max_backtrack(self):
        self._settle()
        return max(0, self.processed - kernel.backtrack_tail(10000, 125000)) if self.run is None else 0

    def _events(self):
        for name, pred in list(self.events.items()):
            if pred is None or not pred(self):
                continue
            if name == "press":
                if self.locked():
                    continue
                self.button = 0
            elif name == "loop_closes":
                self.loop_open = False
            elif name == "laser_on":
                self.laser_forced = True
            elif name == "stall":
                self.clock.t += self.STALL_S
            elif name == "blind":
                self.blinded = True
            self.events[name] = None

    # -- sysfs -------------------------------------------------------------
    def sysfs_write(self, attr, val):
        self.writes.append((attr, str(val)))
        self._settle()
        v = int(val) if str(val).lstrip("-").isdigit() else val
        if attr in self.holders:
            self.holders[attr] = v
            if v:
                self.button = 1                     # the lock is the button latch's SET
                self.fire_restored = False          # and parks FIRE
        elif attr == "cnc/step_freq":
            self.step_freq = v
        elif attr == "cnc/streaming":
            self.streaming = v
        elif attr == "cnc/run":
            self._start(1, len(self.data))
        elif attr == "cnc/resume":
            if v < 0:
                if -v > self.max_backtrack():
                    raise OSError(errno.EPERM, "past the history the ring holds")
                self._start(-1, self.processed + v)
            elif v == 0 or self.resume_plays:
                self._start(1, len(self.data), lead_end=self.processed + v)
        elif attr == "cnc/stop":
            if self.run is not None and self.run["stop_t"] is None:
                self.run["stop_t"] = self.clock.t
                self.stops += 1
            elif self.run is None and self.idle_state == "underrun":
                self.idle_state = "idle"
        elif attr == "cnc/halt":
            if self.run is not None:
                self.processed = self._pos(self.clock.t)
                self.run, self.idle_state = None, "idle"
            elif self.idle_state != "idle":
                raise OSError(errno.EPERM, "not running")
        else:
            self.attrs[attr] = str(val)

    def sysfs_read(self, attr, default=None):
        self.clock.t += self.READ_S
        self.reads[attr] += 1
        if attr in ("cnc/interlock_circuit", "cnc/button_latch"):
            self._events()
        elif self.blinded:
            return default
        self._settle()
        if attr == "cnc/state":
            return "running" if self.run is not None else self.idle_state
        if attr == "cnc/interlock_circuit":
            on, fire = self.laser_on(), self.fire()
            return str((0 if on else 1) | (2 if fire else 0) | (4 if self.button else 0)
                       | (8 if self.locked() else 0) | (16 if self.loop_open else 0)
                       | (0 if self.run is not None else 32))
        if attr == "cnc/button_latch":
            return str(self.button)
        if attr == "cnc/laser_enable":
            return "1" if self.fire() else "0"
        if attr == "cnc/laser_on":
            return "1" if self.laser_on() else "0"
        if attr == "cnc/charge_pump_alive":
            return "1" if self.run is not None or self.pump_alive else "0"
        if attr == "cnc/underruns":
            return str(self.underruns)
        if attr == "cnc/max_backtrack":
            return str(self.max_backtrack())
        return self.attrs.get(attr, "0")

    def sysfs_int(self, attr, default=None):
        v = self.sysfs_read(attr)
        return int(v) if v is not None else default

    def switch_bits(self):
        return (1 << 5) | (1 << 6) if self.loop_open else 0

    # -- the pulse device ----------------------------------------------------
    def rewind(self):
        self.data, self.processed = b"", 0

    def close(self):
        self._settle()
        self.closed_unlocked += not self.locked()
        if self.run is not None:                    # the dead man's switch halts the engine
            self.processed = self._pos(self.clock.t)
            self.run, self.idle_state = None, "idle"
        if self.close_relocks:
            for h in self.holders:
                self.holders[h] = 1
            self.button, self.fire_restored = 1, False

    def latch_writes(self):
        return [v for a, v in self.writes if a == "cnc/laser_latch"]


def forward(k):
    return k.moving() > 0


def backward(k):
    return k.moving() < 0


def stopped(k):
    return k.run is None and k.stops > 0


# Every phase that unlocks the latch for a run, by the function that holds
# its unlock: the rows the refusal and the ended-locked cases run.
# test_every_unlocking_phase_is_listed holds this to the suite's source.
UNLOCKING = {"_fire_phase": (("B", lambda ctx: kernel._fire_phase(ctx, "B")),
                             ("U", lambda ctx: kernel._fire_phase(ctx, "U"))),
             "_retrace_phase": (("R", kernel._retrace_phase),),
             "_k3_phase": (("K3", kernel._k3_phase),),
             "_lead_phase": (("L", lambda ctx: kernel._lead_phase(ctx, 1000)),)}
PHASES = tuple(row for rows in UNLOCKING.values() for row in rows)


def unlocked_read(n):
    """At the n-th read of the chain with the latch unlocked: the unlock's
    own read of the button latch is the first."""
    seen = [0]

    def pred(k):
        if k.locked():
            return False
        seen[0] += 1
        return seen[0] >= n
    return pred


class Drill(unittest.TestCase):
    """A drill run over FakeKernel: the module's sysfs, switches, clock and
    pulse device scripted (the real PulseDevice, over a fake open, close
    and flock)."""

    def scripted(self, k):
        clock = FakeClock()
        k.clock = clock
        fake_os = types.SimpleNamespace(O_WRONLY=1, open=lambda path, flags: 99,
                                        write=lambda fd, d: (setattr(k, "data", k.data + d), len(d))[1],
                                        lseek=lambda fd, off, how: (k.rewind(), off)[1], SEEK_SET=0,
                                        close=lambda fd: k.close() if fd == 99 else None)
        fake_fcntl = types.SimpleNamespace(LOCK_EX=2, LOCK_UN=8, flock=lambda fd, op: None)
        for target, name, value in ((kernel.hw, "sysfs_read", k.sysfs_read), (kernel.hw, "sysfs_int", k.sysfs_int),
                                    (kernel.hw, "sysfs_write", k.sysfs_write),
                                    (kernel.hw, "switch_bits", k.switch_bits),
                                    (kernel, "rd_pos", k.pos), (kernel, "time", clock),
                                    (kernel, "os", fake_os), (kernel, "fcntl", fake_fcntl)):
            p = mock.patch.object(target, name, value)
            p.start()
            self.addCleanup(p.stop)
        return DrillCtx(k)

    def assertRelockedAndFailed(self, k, run, before=None):
        """The phase failed with the latch relocked after its unlock, and
        before the write `before` (the next step of the phase) was made."""
        self.assertRaises(kernel.Failed, run)
        unlock = k.writes.index(("cnc/laser_latch", "0"))
        relock = k.writes.index(("cnc/laser_latch", "1"), unlock)
        if before is not None:
            self.assertNotIn(before, [w[0] for w in k.writes[unlock:relock]],
                             "the phase went on to %s before it relocked" % before)

    def assertEndedLocked(self, k):
        """The phase locked the latch itself before it let the pulse device
        go (the kernel's relock at the final close is not the phase's), and
        the latch reads locked now."""
        self.assertEqual(k.closed_unlocked, 0, "the phase closed the pulse device with the latch unlocked")
        self.assertTrue(k.locked())


class OpenedChainTests(Drill):
    def test_the_chain_reads_open_only_with_every_block_shown(self):
        k = FakeKernel()
        self.scripted(k)
        self.assertIsNone(kernel.chain_open_why())
        for name, ilk, sw in (("interlock_circuit unreadable", None, (1 << 5) | (1 << 6)),
                              ("no INTERLOCK_RESET", kernel.LATCH_BIT, (1 << 5) | (1 << 6)),
                              ("the switches unreadable", kernel.INTERLOCK_RESET_BIT, None),
                              ("the loop read closed", kernel.INTERLOCK_RESET_BIT, 1 << 6),
                              ("the interlock latch clear", kernel.INTERLOCK_RESET_BIT, 1 << 5)):
            with self.subTest(name), mock.patch.object(kernel.hw, "sysfs_int", lambda a, d=None: ilk), \
                    mock.patch.object(kernel.hw, "switch_bits", lambda: sw):
                self.assertIsNotNone(kernel.chain_open_why())
        # It reads closed again only when shown closed: a loop that cannot
        # be read is not a closed one.
        self.scripted(FakeKernel(chain_open=False))
        self.assertTrue(kernel.chain_closed())
        with mock.patch.object(kernel.hw, "sysfs_int", lambda a, d=None: None):
            self.assertFalse(kernel.chain_closed())

    def test_a_run_sample_passes_only_dark_behind_both_latches(self):
        dark = (kernel.LASER_ON_RAW_BIT | kernel.BUTTON_LATCH_BIT | kernel.INTERLOCK_RESET_BIT)
        self.assertIsNone(kernel.run_guard_why(dark))
        for name, ilk in (("unreadable", None),
                          ("LASER_ON active (bit 0 is active low)", dark & ~kernel.LASER_ON_RAW_BIT),
                          ("the loop closed", dark & ~kernel.INTERLOCK_RESET_BIT),
                          ("the button latch cleared", dark & ~kernel.BUTTON_LATCH_BIT)):
            with self.subTest(name):
                self.assertIsNotNone(kernel.run_guard_why(ilk))

    def test_an_unlock_needs_the_chain_open_and_relocks_on_a_cleared_button_latch(self):
        k = FakeKernel(chain_open=False)
        ctx = self.scripted(k)
        with self.assertRaises(kernel.Failed):
            kernel.unlock_behind_chain(ctx, "B")
        self.assertNotIn("0", k.latch_writes())                     # never unlocked
        k = FakeKernel(press=lambda k: True)                        # a press lands with the unlock
        ctx = self.scripted(k)
        with self.assertRaises(kernel.Failed):
            kernel.unlock_behind_chain(ctx, "B")
        self.assertEqual(k.latch_writes()[-1], "1")                 # relocked
        k = FakeKernel()                                            # the button latch cannot be read
        ctx = self.scripted(k)
        with mock.patch.object(kernel.hw, "sysfs_read",
                               lambda a, d=None: None if a == "cnc/button_latch" else k.sysfs_read(a, d)):
            with self.assertRaises(kernel.Failed):
                kernel.unlock_behind_chain(ctx, "B")
        self.assertEqual(k.latch_writes()[-1], "1")
        k = FakeKernel()
        ctx = self.scripted(k)
        kernel.unlock_behind_chain(ctx, "B")
        self.assertEqual(k.latch_writes(), ["0"])
        self.assertEqual(ctx.evidence["button_latch_after_unlock"], {"B": "1"})

    def test_every_unlocking_phase_is_listed(self):
        # A phase added to the suite that unlocks behind the chain is in
        # the rows below, or this fails: the functions of the suite whose
        # source calls unlock_behind_chain.
        calls = {name for name, fn in vars(kernel).items()
                 if inspect.isfunction(fn) and fn is not kernel.unlock_behind_chain
                 and "unlock_behind_chain(" in inspect.getsource(fn)}
        self.assertEqual(calls, set(UNLOCKING))

    def test_each_unlocking_phase_is_refused_with_the_chain_closed_or_the_charge_pump_fed(self):
        for name, phase in PHASES:
            for case, machine in (("the chain closed", {"chain_open": False}),
                                  ("the charge pump fed", {"pump_alive": True})):
                with self.subTest(name, case=case):
                    k = FakeKernel(**machine)
                    ctx = self.scripted(k)
                    with self.assertRaises(kernel.Failed):
                        phase(ctx)
                    self.assertNotIn("0", k.latch_writes(), "the latch was unlocked with %s" % case)

    def test_each_unlocking_phase_relocks_and_fails_at_a_press_mid_run(self):
        # The control: each phase passes on the scripted kernel with no
        # press, reads the chain in every sample of its run, and has locked
        # the latch itself when it lets the pulse device go; a run it starts
        # after its guarded window (R's drain) follows a lock at once. The
        # same end when a read fails under the unlocked run. Then a press at
        # each guarded moment of each phase: the latch relocks before the
        # phase's next step.
        for name, phase in PHASES:
            with self.subTest(name, case="no press"):
                k = FakeKernel()
                phase(self.scripted(k))
                self.assertEndedLocked(k)
                # a sample reads the FIRE line once: the chain is read as
                # often (the reads outside the loop are a handful)
                self.assertGreater(k.reads["cnc/interlock_circuit"], k.reads["cnc/laser_enable"] - 5)
                for at, w in enumerate(k.writes):
                    if w == ("cnc/resume", "0"):
                        self.assertEqual(k.writes[at - 1], ("cnc/laser_latch", "1"),
                                         "a run after the guarded window did not start right after a lock")
            with self.subTest(name, case="a read fails mid-run"):
                k = FakeKernel(blind=unlocked_read(3))
                ctx = self.scripted(k)
                with self.assertRaises(kernel.Failed):
                    phase(ctx)
                self.assertIn("0", k.latch_writes())
                self.assertEndedLocked(k)
        for name, phase, press, before in (
                ("B", lambda ctx: kernel._fire_phase(ctx, "B"), forward, None),
                ("R, the forward run", kernel._retrace_phase, forward, "cnc/stop"),
                ("R, stopped", kernel._retrace_phase, stopped, "cnc/resume"),
                ("R, the backward run", kernel._retrace_phase, backward, None),
                ("K3", kernel._k3_phase, unlocked_read(3), None),
                ("L", lambda ctx: kernel._lead_phase(ctx, 1000), forward, None)):
            with self.subTest(name, case="a press"):
                k = FakeKernel(press=press)
                ctx = self.scripted(k)
                self.assertRelockedAndFailed(k, lambda: phase(ctx), before)

    def test_a_loop_that_closes_mid_run_relocks_and_fails_the_phase(self):
        k = FakeKernel(loop_closes=forward)
        ctx = self.scripted(k)
        self.assertRelockedAndFailed(k, lambda: kernel._fire_phase(ctx, "B"))
        self.assertGreater(len(k.data) - k.pos()[3], 0, "the phase ran on to the end of its data")

    def test_the_largest_gap_between_two_guarded_samples_is_kept(self):
        k = FakeKernel(stall=lambda k: k.moving() > 0 and k.pos()[3] > 20000)
        ctx = self.scripted(k)
        kernel._fire_phase(ctx, "B")
        gap = ctx.evidence.get("sample_gap_ms", {}).get("B")
        self.assertIsNotNone(gap)
        self.assertGreaterEqual(gap, FakeKernel.STALL_S * 1000)
        self.assertLess(gap, FakeKernel.STALL_S * 1000 + 10)

    def test_the_drills_lock_halt_and_stop_before_the_loop_closes_and_fail_on_a_loop_left_open(self):
        for name, drill in (("fire-line", kernel.fire_line), ("resume-lead", kernel.resume_lead)):
            with self.subTest(name):
                k = FakeKernel(chain_open=False)
                ctx = self.scripted(k)
                try:
                    drill(ctx)
                except kernel.Failed as e:
                    self.fail("the drill failed a kernel that did everything right: %s" % e)
                close = k.writes.index(("act", "interlock close"))
                self.assertEqual(k.writes[close - 3:close],
                                 [("cnc/laser_latch", "1"), ("cnc/halt", "1"), ("cnc/stop", "1")])
                self.assertFalse(k.loop_open)
                k = FakeKernel(chain_open=False, loop_stuck=True)
                ctx = self.scripted(k)
                with self.assertRaises(kernel.Failed):
                    drill(ctx)

    def test_the_final_close_at_idle_must_relock_by_itself(self):
        k = FakeKernel()
        ctx = self.scripted(k)
        try:
            kernel.idle_close(ctx, ctx.evidence)
        except kernel.Failed as e:
            self.fail("the idle close failed a kernel whose close relocks: %s" % e)
        k = FakeKernel(close_relocks=False)                         # the kernel's close leaves it unlocked
        ctx = self.scripted(k)
        with self.assertRaises(kernel.Failed):
            kernel.idle_close(ctx, ctx.evidence)
        # This unlock has nobody present and no opened chain before it:
        # refused, with nothing unlocked, while something feeds the charge
        # pump; and relocked, failing, when the button latch does not read
        # set right after it (a press, or a latch that cannot be read).
        k = FakeKernel(pump_alive=True)
        ctx = self.scripted(k)
        with self.assertRaises(kernel.Failed):
            kernel.idle_close(ctx, ctx.evidence)
        self.assertNotIn("0", k.latch_writes(), "unlocked at idle with the charge pump fed")
        for name, reads in (("a press with the unlock", "0"), ("the button latch unreadable", None)):
            with self.subTest(name):
                k = FakeKernel()
                ctx = self.scripted(k)
                with mock.patch.object(kernel.hw, "sysfs_read", lambda a, d=None, k=k, reads=reads: (
                        reads if a == "cnc/button_latch" else k.sysfs_read(a, d))):
                    with self.assertRaises(kernel.Failed):
                        kernel.idle_close(ctx, ctx.evidence)
                self.assertEqual((k.latch_writes()[-1], k.closed_unlocked), ("1", 0))

    def test_the_drill_ends_halted_and_stopped_with_the_rail_up(self):
        k = FakeKernel()
        ctx = self.scripted(k)

        def refuses_halt(attr, val):
            k.writes.append((attr, str(val)))
            if attr == "cnc/halt":
                raise OSError(errno.EPERM, "underrun")
        with mock.patch.object(kernel.hw, "sysfs_write", refuses_halt):
            kernel.safe_end(ctx)
        self.assertEqual([a for a, _ in k.writes], ["cnc/laser_latch", "cnc/halt", "cnc/stop"])


class LockedDrillTests(Drill):
    """The drills that keep the latch locked, over the same scripted kernel:
    K2 judges its resume, the backward run at the boundary is declared
    streaming, and the dead man's close turns the head's emitters off on its
    way out."""

    def test_k2_fails_a_resume_that_never_ran(self):
        try:
            kernel.k1_k2(self.scripted(FakeKernel()))
        except kernel.Failed as e:
            self.fail("K1/K2 failed a kernel that did everything right: %s" % e)
        with self.assertRaises(kernel.Failed):
            kernel.k1_k2(self.scripted(FakeKernel(resume_plays=False)))

    def test_the_backward_run_at_the_boundary_is_declared_streaming(self):
        k = FakeKernel()
        ctx = self.scripted(k)
        try:
            kernel.backtrack_bounds(ctx)
        except kernel.Failed as e:
            self.fail("backtrack-bounds failed a kernel that did everything right: %s" % e)
        at = k.writes.index(("cnc/resume", str(-ctx.evidence["max_backtrack"])))
        self.assertEqual(k.writes[at - 1], ("cnc/streaming", "1"))

    def test_the_dead_mans_close_turns_the_emitters_off_on_every_way_out(self):
        k = FakeKernel()
        ctx = self.scripted(k)
        with mock.patch.object(kernel, "deadman_close_body", side_effect=kernel.Failed("the close drill failed")):
            with self.assertRaises(kernel.Failed):
                kernel.deadman_close(ctx)
        for w in (("head/measure_laser", "0"), ("head/uv_led", "0"), ("head/z_current", "1")):
            self.assertIn(w, k.writes)


class RunJudgeTests(unittest.TestCase):
    def test_the_backtrack_tail_is_the_kernels(self):
        self.assertEqual(kernel.backtrack_tail(10000, 125000), 1323)
        self.assertIsNone(kernel.backtrack_tail(10000, 50))

    def test_a_resume_must_play_the_window_to_the_programs_end(self):
        args = dict(started="running", at_stop=4400, processed=55001, total=55001, window_at=25001,
                    window_len=20000)
        self.assertIsNone(kernel.replay_why(**args))
        for name, change in (("never ran", {"started": "idle"}),
                             ("stopped after the window began", {"at_stop": 30000}),
                             ("stopped short", {"processed": 30000}),
                             ("a window that ends the program, its last bytes unplayed",
                              {"total": 45001, "processed": 44995})):
            with self.subTest(name):
                self.assertIsNotNone(kernel.replay_why(**dict(args, **change)))

    def test_a_backward_run_must_play_back_to_its_waypoint_without_the_dead_stop(self):
        args = dict(started="running", end_state="idle", played=15461, budget=14138, at_end=500, tail=1323)
        self.assertIsNone(kernel.backward_why(**args))
        for name, change in (("never ran", {"started": "idle"}),
                             ("the dead stop under streaming", {"end_state": "underrun"}),
                             ("the program's start", {"at_end": 0}),
                             ("short of the waypoint", {"at_end": 5000})):
            with self.subTest(name):
                self.assertIsNotNone(kernel.backward_why(**dict(args, **change)))

    def test_a_backward_run_over_fire_bytes_unlocked_must_stay_dark(self):
        dark = [(0.001, "0", "0"), (0.2, "0", "0")]
        args = dict(started="running", samples=dark, end_state="idle", played=10461, back=5000, at_end=5000,
                    tail=1323)
        self.assertIsNone(kernel.retrace_why(**args))
        for name, change in (("FIRE driven", {"samples": dark + [(0.3, "1", "0")]}),
                             ("never ran", {"started": None}),
                             ("nothing sampled", {"samples": []}),
                             ("short of the waypoint", {"at_end": 9000})):
            with self.subTest(name):
                self.assertIsNotNone(kernel.retrace_why(**dict(args, **change)))


class PicSettleRestoreTests(unittest.TestCase):
    def test_the_settle_is_handed_back_as_found(self):
        tmp = tempfile.mkdtemp(prefix="forgetest-pic-")
        self.addCleanup(shutil.rmtree, tmp, ignore_errors=True)
        os.makedirs(os.path.join(tmp, "pic"))
        with open(os.path.join(tmp, "pic", "water_temp_1"), "w") as f:
            f.write("500\n")
        for found in ("300", "0"):
            with self.subTest(found=found):
                param = {"v": found}
                with mock.patch.dict(os.environ, {"GF_SYSFS_ROOT": tmp + os.sep}), \
                        mock.patch.object(kernel, "_param_read", lambda path: param["v"]), \
                        mock.patch.object(kernel, "_param_write", lambda path, v: param.update(v=str(v))), \
                        mock.patch.object(kernel, "_spin", lambda s: None), \
                        mock.patch.object(kernel, "time", FakeClock()):
                    try:
                        kernel.pic_soc_load(DrillCtx())
                    except kernel.Failed as e:
                        self.fail("the drill failed a machine it handed back as found: %s" % e)
                self.assertEqual(param["v"], found)


if __name__ == "__main__":
    unittest.main()
