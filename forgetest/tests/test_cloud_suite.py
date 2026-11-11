# Copyright 2026 514 LLC d/b/a OpenGlow
# Written by Scott Wiederhold
# https://community.openglow.org
# SPDX-License-Identifier:    MIT

"""The cloud.* tests' false-PASS guards, against a fake forgectrl and a
replayed gfcloud log: the real test functions run under the real runner
Context, the operator prompts answered - and the standing notices of the
machine actions acted on - by a script that also drives the replay. The
log lines are the machine's own: the lid-abort bench excerpt, and for the
pause, the run loop's lines as its host test emits them on the print
skeleton of that excerpt.

What is proven here: each test fails when the machine did the unsafe
thing - a dark print whose press is asked for with the latch still locked,
a print the kernel plays with the hardware button latch set, a lid stop
that is polled rather than edge-driven, and a resume the kernel refused.
cloud.button-latch's every judgment, and exthost.armed-freeze's of the
run's start (it runs the dark print), are proven on their sample records
over a scripted machine and a clock that moves only when the test sleeps."""
import contextlib
import os
import shutil
import struct
import tempfile
import threading
import time
import types
import unittest
from unittest import mock

import helpers
from forgetest.runner import Context, Failed, Run
from forgetest.suite import cloud
from forgetest.suite import cloudlatch
from forgetest.suite import exthost

HERE = os.path.dirname(os.path.abspath(__file__))
FIX = os.path.join(HERE, "fixtures")


def fixture(name):
    with open(os.path.join(FIX, "gfcloud-%s.log" % name), "rb") as f:
        return f.read().decode().splitlines()


def cut(lines, marker, count=1):
    """(before, after) at the count-th line containing marker (the line
    itself opens `after`)."""
    n = 0
    for i, ln in enumerate(lines):
        if marker in ln:
            n += 1
            if n == count:
                return lines[:i], lines[i:]
    raise KeyError(marker)


def dark_print(ctx):
    """The dark print as exthost.armed-freeze runs it: the cloud suite's
    dark_print_body between the offline service's setup and cleanup."""
    offset = cloud.enter_offline(ctx)
    job = cloud.offline_job(ctx, "dark.puls", seconds=30)
    off = cloud.Offline().__enter__()
    try:
        cloud.dark_print_body(ctx, ctx.evidence, off, job, offset, ctx.forgectrl)
    finally:
        off.__exit__(None, None, None)
        cloud.offline_cleanup(ctx)


class Script:
    """Answers every prompt with its first option; a hook per prompt or
    notice substring drives the fake machine and the log replay (a
    machine action is a notice the test watches the machine for, so the
    hook is what makes the machine show it)."""

    def __init__(self, run, hooks=None):
        self.run = run
        self.hooks = hooks or {}
        self.asked = []
        self.noticed = []
        self.th = threading.Thread(target=self._loop, daemon=True)
        self.stop = False

    def start(self):
        self.th.start()
        return self

    def _fire(self, text):
        for key, fn in self.hooks.items():
            if key in text:
                fn()

    def _loop(self):
        seen = None
        seen_n = None
        while not self.stop:
            n = self.run.notice
            if n and n["id"] != seen_n:
                seen_n = n["id"]
                self.noticed.append(n["text"])
                self._fire(n["text"])
            p = self.run.prompt
            if p and p["id"] != seen:
                seen = p["id"]
                self.asked.append(p["question"])
                self._fire(p["question"])
                self.run.answer(p["id"], p["options"][0])
            time.sleep(0.02)


OFFLINE_LINE = ("2026-08-22T23:00:00.100000+00:00 gfcloud[3100] INFO offline:open OFFLINE service: "
                "no web session; listening on /run/gfcloud-offline.sock")


class FakeOffline:
    """The offline service's socket, as the tests see it: what was sent,
    and a hook that lands the machine's lines for each message."""
    on_send = None
    sent = []

    def __init__(self, path=None):
        self.events = []
        self.buf = b""

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def send(self, msg):
        FakeOffline.sent.append(msg)
        if FakeOffline.on_send:
            FakeOffline.on_send(msg)

    def poll(self):
        return []

    def print_ready(self, action_id, path, settings=None):
        self.send({"id": action_id, "action_type": "print", "status": "ready",
                   "motion_url": "file://" + path, "settings": settings or {}})

    def cancel(self, action_id, action_type="print"):
        self.send({"id": action_id, "action_type": action_type, "status": "cancelled"})


class CloudSuiteTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="forgetest-cloud-")
        self.log = os.path.join(self.tmp, "gfcloud.log")
        open(self.log, "wb").close()
        self.sysfs = os.path.join(self.tmp, "sysfs") + os.sep
        os.makedirs(self.sysfs + "cnc")
        self._attr("cnc/interlock_circuit", "45")          # latch locked (bit 3)
        self._pos(0, 0, 3)
        os.environ["GF_SYSFS_ROOT"] = self.sysfs
        # the button LEDs, dark (the tests check a cancel leaves them so)
        for n in ("button_led_1", "button_led_2", "button_led_3"):
            os.makedirs(os.path.join(self.tmp, "leds", n), exist_ok=True)
            with open(os.path.join(self.tmp, "leds", n, "brightness"), "w") as f:
                f.write("0")
        os.environ["GF_LEDS_ROOT"] = os.path.join(self.tmp, "leds") + os.sep
        self.fc = helpers.FakeForgectrl().start()
        self.grbl = None
        self.fclog = os.path.join(self.tmp, "forgectrl.log")
        open(self.fclog, "wb").close()
        self.homelog = os.path.join(self.tmp, "gfhome.log")
        open(self.homelog, "wb").close()
        self.saved = (cloud.GFCLOUD_LOG, cloud.FORGECTRL_LOG, cloud.QUIET_S, cloud.QUIET_TIMEOUT_S,
                      cloud.HUNT_TIMEOUT_S, cloud.GFHOME_LOG, cloud.Offline, cloud.OFFLINE_MARKER,
                      cloud.JOB_DIR)
        cloud.GFCLOUD_LOG = self.log
        cloud.FORGECTRL_LOG = self.fclog
        cloud.GFHOME_LOG = self.homelog
        cloud.Offline = FakeOffline
        cloud.OFFLINE_MARKER = os.path.join(self.tmp, "offline-marker")
        cloud.JOB_DIR = os.path.join(self.tmp, "jobs")
        self.saved_emu = (cloud.EMULATE_MARKER, cloud.EMULATOR_WORK, cloud.NOHUNT_MARKER)
        cloud.EMULATE_MARKER = os.path.join(self.tmp, "emulate-marker")
        cloud.EMULATOR_WORK = os.path.join(self.tmp, "emu-work")
        cloud.NOHUNT_MARKER = os.path.join(self.tmp, "nohunt-marker")
        FakeOffline.sent = []
        FakeOffline.on_send = None
        cloud.QUIET_S = 0.4
        # A deadline, not a wait: a quiet machine is seen at once. The
        # replays land their lines from threads, so under a loaded host a
        # tighter deadline expires before the last of them is written.
        cloud.QUIET_TIMEOUT_S = 15
        cloud.HUNT_TIMEOUT_S = 8
        self.script = None

    def tearDown(self):
        if self.script:
            self.script.stop = True
        self.fc.stop()
        if self.grbl:
            self.grbl.stop()
        (cloud.GFCLOUD_LOG, cloud.FORGECTRL_LOG, cloud.QUIET_S, cloud.QUIET_TIMEOUT_S,
         cloud.HUNT_TIMEOUT_S, cloud.GFHOME_LOG, cloud.Offline, cloud.OFFLINE_MARKER,
         cloud.JOB_DIR) = self.saved
        (cloud.EMULATE_MARKER, cloud.EMULATOR_WORK, cloud.NOHUNT_MARKER) = self.saved_emu
        os.environ.pop("GF_SYSFS_ROOT", None)
        os.environ.pop("GF_LEDS_ROOT", None)
        shutil.rmtree(self.tmp, ignore_errors=True)

    # -- fakes -----------------------------------------------------------
    def _attr(self, attr, val):
        with open(self.sysfs + attr, "w") as f:
            f.write(val)

    def _pos(self, x, y, z):
        with open(self.sysfs + "cnc/position", "wb") as f:
            f.write(struct.pack("<3i2I", x, y, z, 0, 0))

    def append(self, lines, delay=0.0):
        def go():
            if delay:
                time.sleep(delay)
            with open(self.log, "ab") as f:
                f.write(("\n".join(lines) + "\n").encode())
        if delay:
            threading.Thread(target=go, daemon=True).start()
        else:
            go()

    def in_cloud(self, pid=2278):
        self.fc.state["mode"] = {"mode": "cloud", "controller": "running", "pid": pid, "motion": "verified"}
        self.fc.state["settings"]["controller_mode"] = "cloud"

    def lid(self, closed):
        self.fc.state["status"]["switches"]["lid"] = bool(closed)

    def hunted(self, pid):
        """The running client homed the machine under the service (a test
        that prints reuses only such a session)."""
        self.append(["2026-08-17T09:41:20.000000+00:00 gfcloud[%d] INFO basemachine:_finish_action hunt "
                     "[1578200001]: finished with event \":completed\"" % pid])

    def in_offline(self, pid=3100):
        """Cloud mode with the offline service already up (its mark is the
        newest websocket-state line for the pid)."""
        self.in_cloud(pid=pid)
        self.append([OFFLINE_LINE.replace("gfcloud[3100]", "gfcloud[%d]" % pid)])

    def offline_print_hooks(self, pre, cancel_tail=None):
        """The fake socket's reactions: a print lands its prologue through
        the run (the operator's press is already in it), a cancel lands the
        app-cancel tail."""
        def on_send(msg):
            if msg["action_type"] == "print" and msg["status"] == "ready":
                self.append(pre, delay=0.1)
            elif msg["status"] == "cancelled" and cancel_tail:
                self.append(cancel_tail, delay=0.05)
        FakeOffline.on_send = on_send

    def run_test(self, fn, hooks=None, test_id="cloud.x"):
        run = Run("test", test_id, test_id)
        run.baseline_captured = {"mode": self.fc.state["mode"]["mode"], "position": [0, 0, 0]}
        ctx = Context(run, None, helpers.make_test(test_id, []))
        self.script = Script(run, hooks).start()
        try:
            fn(ctx)
        finally:
            self.script.stop = True
        return run

    def assertFails(self, fn, needle, hooks=None):
        with self.assertRaises(Failed) as cm:
            self.run_test(fn, hooks)
        self.assertIn(needle, str(cm.exception))
        return cm.exception

    # -- pause / resume ---------------------------------------------------------
    def test_pause_resume_fails_when_the_kernel_refuses_the_resume(self):
        self.in_cloud(pid=1522)
        self.hunted(1522)
        self.append(["2026-08-17T09:41:00.500000+00:00 gfcloud[1522] INFO websocket:_on_open RX-EVENT: ready"])
        lines = [l.replace("machine:_resume_retraced resuming (laser lead 1950 ticks)",
                           "machine:_resume_retraced resume refused ([Errno 22] Invalid argument); canceling")
                 for l in fixture("pause")]
        pre, rest = cut(lines, "current state: MachineState.RUNNING")
        pre, rest = pre + [rest[0]], rest[1:]
        with self.fast_wait_log():
            hooks = {"Click Done here": lambda: self.append(pre, delay=0.1),
                     "the press pauses the print": lambda: self.append(rest, delay=0.05)}
            self.assertFails(cloud.pause_resume, "the resume was refused", hooks=hooks)

    @contextlib.contextmanager
    def fast_wait_log(self):
        """The wait for the pause lines is 90 s: a replay whose line never
        comes would sit it out. Cap it through the module's wait."""
        saved = cloud.wait_log

        def fast(ctx, offset, needles, timeout, poll=0.5):
            return saved(ctx, offset, needles, min(timeout, 1.5), poll=0.1)
        cloud.wait_log = fast
        press = cloud.PRESS_TIMEOUT_S
        cloud.PRESS_TIMEOUT_S = 1.5
        try:
            yield
        finally:
            cloud.wait_log = saved
            cloud.PRESS_TIMEOUT_S = press

    # -- the merged lid + interlock abort test -------------------------------
    def abort_parts(self):
        """The lid-abort excerpt split for the merged test: the print
        prologue (replayed for both prints), the lid stop + park + cancel,
        and the same tail with the interlock as the trigger, cut where the
        test stops to have the lid opened during the park."""
        lines = fixture("lidabort")
        pre, rest = cut(lines, "waiting for button")
        run_pre, rest = cut(rest, "machine:_run_loop starting run")
        pre, rest = pre + run_pre + [rest[0]], rest[1:]
        ilk = [l.replace("lid opened mid-run; stopping motion",
                         "interlock opened mid-run; stopping motion") for l in rest]
        stop, tail = cut(ilk, "start return home")
        return pre, rest, stop + [tail[0]], tail[1:]

    def abort_hooks(self, pre, lid_tail, ilk_stop, ilk_park, prints):
        def on_send(msg):
            if msg["status"] == "ready":
                prints.append(msg["id"])
                self.append(pre, delay=0.1)
        FakeOffline.on_send = on_send

        def pull_interlock():
            self.fc.state["status"]["switches"]["interlock_ok"] = False
            self.append(ilk_stop, delay=0.05)

        def restore():
            self.lid(True)
            self.fc.state["status"]["switches"]["interlock_ok"] = True
        return {"leave the lid open until the head has returned": lambda: (self.lid(False),
                                                                           self.append(lid_tail, delay=0.05)),
                "Close the lid.": lambda: self.lid(True),
                "Open the remote-interlock loop": pull_interlock,
                "on its way back": lambda: (self.lid(False), self.append(ilk_park, delay=0.05)),
                "Restore the remote-interlock loop": restore}

    def test_lid_interlock_abort_fails_when_the_lid_stop_is_not_edge_driven(self):
        # a polled stop (the pre-parity behavior) shows up as a long edge->stop gap
        self.in_offline()
        pre, lid_tail, ilk_stop, ilk_park = self.abort_parts()
        lid_tail = [l.replace("2026-08-17T09:42:30.838627", "2026-08-17T09:42:31.838627")
                    if "lid opened mid-run; stopping motion" in l else l for l in lid_tail]
        self.assertFails(cloud.lid_interlock_abort, "not edge-driven",
                         hooks=self.abort_hooks(pre, lid_tail, ilk_stop, ilk_park, []))

    # -- the dark print exthost.armed-freeze runs: unlocked at the button ------
    def dark_parts(self):
        """(the print prologue through the button prompt, the run and the
        print's end with no pause in it)."""
        lines = fixture("pause")
        pre, rest = cut(lines, "waiting for button")
        pre = pre + [rest[0]]
        _, run = cut(rest, "machine:_run_loop starting run")
        run = [l for l in run if "paus" not in l and "resum" not in l]
        return pre, run

    def test_dark_print_fails_when_the_latch_is_locked_at_the_button(self):
        # The press is asked for with the lock still held (setUp's state):
        # the hardware would not count it.
        self.in_offline()
        pre, run = self.dark_parts()
        self.offline_print_hooks(pre)
        hooks = {"press it. The print runs dark": lambda: self.append(run, delay=0.05)}
        self.assertFails(dark_print, "the latch is locked at the button wait", hooks=hooks)

    def test_dark_print_fails_when_the_latch_is_unlocked_before_the_print(self):
        self.in_offline()
        pre, run = self.dark_parts()
        self._attr("cnc/interlock_circuit", "37")          # unlocked with no print asked for
        self.offline_print_hooks(pre)
        hooks = {"press it. The print runs dark": lambda: self.append(run, delay=0.05)}
        self.assertFails(dark_print, "the latch is unlocked before the print", hooks=hooks)

    # -- cloud.button-latch: the hardware latch while the kernel plays -----------
    def test_button_latch_fails_when_the_print_runs_with_the_hardware_latch_set(self):
        # The lock is released for the press and through the run, as it
        # should be, but the hardware's own latch never clears: a press the
        # chain did not count. The kernel plays the job all the same.
        self.in_offline()
        pre, run = self.dark_parts()
        self._attr("cnc/button_latch", "1")
        self._attr("cnc/state", "idle")
        self.offline_print_hooks(pre)
        landed = FakeOffline.on_send

        def on_send(msg):
            if msg["action_type"] == "print" and msg["status"] == "ready":
                self._attr("cnc/interlock_circuit", "37")  # the client unlocks, then asks for the press
            landed(msg)
        FakeOffline.on_send = on_send
        saved = cloudlatch.SAMPLE_S
        cloudlatch.SAMPLE_S = 0.02
        self.addCleanup(setattr, cloudlatch, "SAMPLE_S", saved)

        def pressed():
            self.append(run, delay=0.05)
            self._attr("cnc/state", "running")
            threading.Timer(2.5, lambda: self._attr("cnc/state", "idle")).start()
        hooks = {"press it once. The print runs dark": pressed}
        self.assertFails(cloudlatch.button_latch_print,
                         "the hardware button latch read set while the kernel ran", hooks=hooks)


Ctx, Clock = helpers.JudgeCtx, helpers.Clock
LOCKED, UNLOCKED = "45", "37"           # cnc/interlock_circuit, the laser lock at bit 3


class ButtonLatchJudgeTests(unittest.TestCase):
    """cloud.button-latch's judgment (button_latch_body) over a scripted
    machine: the hardware button latch (cnc/button_latch, 1 set) and the
    laser lock as the kernel shows them, the cloud client's waits answered
    at once. A print that holds every rule passes; one rule broken, the
    rest held, fails on that rule."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="forgetest-latch-")
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        os.makedirs(os.path.join(self.tmp, "cnc"))
        os.environ["GF_SYSFS_ROOT"] = self.tmp + os.sep
        self.addCleanup(os.environ.pop, "GF_SYSFS_ROOT", None)
        self.clock = Clock(self.tick)
        self.ran = 0
        for name, fn in (("wait_button_wait", lambda ctx, offset, timeout, also=(): "waiting for button"),
                         ("wait_print_running", lambda ctx, offset, timeout: "starting run"),
                         ("wait_action_finished",
                          lambda ctx, offset, action, timeout, poll=0.5: "t gfcloud[1] INFO " + cloud.COMPLETED),
                         ("log_lines_since", lambda path, offset: ["t gfcloud[1] INFO button pushed"] * self.presses),
                         ("settle_cloud", lambda ctx, offset: None)):
            p = mock.patch.object(cloud, name, fn)
            p.start()
            self.addCleanup(p.stop)
        p = mock.patch.object(cloudlatch, "time", self.clock)
        p.start()
        self.addCleanup(p.stop)

    def attr(self, name, value):
        with open(os.path.join(self.tmp, "cnc", name), "w") as f:
            f.write(str(value))

    def tick(self):
        """The kernel plays the job for run_samples of the body's reads,
        then the client relocks (the lock, and the latch with it)."""
        if self.ran is None:
            return
        self.ran += 1
        if self.m["lock_in_run"] and self.ran == 3:
            self.attr("interlock_circuit", LOCKED)
        if self.ran >= self.m["run_samples"]:
            self.attr("state", "idle")
            if self.m["relock_after"]:
                self.attr("interlock_circuit", LOCKED)
                self.attr("button_latch", 1)
            self.ran = None

    def judge(self, latch_before=1, unlock_for_press=True, latch_at_button=1, run_samples=25,
              lock_in_run=False, presses=1, relock_after=True):
        self.m = dict(lock_in_run=lock_in_run, run_samples=run_samples, relock_after=relock_after)
        self.presses = presses
        self.attr("button_latch", latch_before)
        self.attr("interlock_circuit", LOCKED)
        self.attr("state", "idle")
        self.ran = None

        def print_ready(action_id, job):
            # the client waits out the verdict, releases the lock, asks for the press
            if unlock_for_press:
                self.attr("interlock_circuit", UNLOCKED)
            self.attr("button_latch", latch_at_button)

        def press():
            # the press clears the latch; the client has the lock released by now
            self.attr("button_latch", 0)
            self.attr("interlock_circuit", UNLOCKED)
            self.attr("state", "running")
            self.ran = 0
        ctx = Ctx(press=press)
        cloudlatch.button_latch_body(ctx, ctx.evidence, types.SimpleNamespace(print_ready=print_ready), "job", 0)
        return ctx

    def assertJudged(self, needle, **machine):
        with self.assertRaises(Failed) as cm:
            self.judge(**machine)
        self.assertIn(needle, str(cm.exception))

    def test_a_print_that_holds_every_rule_passes(self):
        ctx = self.judge()
        self.assertEqual(ctx.evidence["samples_in_run"], 25)

    def test_too_few_samples_while_the_kernel_plays_fail(self):
        self.assertJudged("samples were taken", run_samples=cloudlatch.SAMPLES_MIN - 1)

    def test_the_lock_held_while_the_button_is_asked_for_fails(self):
        self.assertJudged("the lock is still held", unlock_for_press=False)

    def test_a_latch_already_clear_before_the_press_fails(self):
        self.assertJudged("before any press", latch_at_button=0)

    def test_a_latch_clear_before_the_print_fails(self):
        self.assertJudged("before the print", latch_before=0)

    def test_the_lock_held_while_the_kernel_plays_fails(self):
        self.assertJudged("the lock was held while the kernel ran", lock_in_run=True)

    def test_a_second_press_fails(self):
        self.assertJudged("2 presses", presses=2)

    def test_a_latch_not_set_again_after_the_job_fails(self):
        self.assertJudged("after the print", relock_after=False)


class ArmedFreezeJudgeTests(unittest.TestCase):
    """exthost.armed-freeze's judgment of its samples: the run's start is
    the kernel's first running sample after the window opens, and the group
    must be frozen by then. The package, the dark print and the machine are
    scripted against the test's clock, which moves only when the sampler
    sleeps (0.2 s a sample)."""

    OPEN, CLOSE, END = 1005.1, 1025.1, 1036.0       # the armed window, and the print's end

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="forgetest-freeze-")
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.clock = Clock()
        self.spans = {"frozen": (1005.5, 1026.0), "running": (1008.0, 1024.0), "unlocked": (1006.0, 1025.0)}
        ext = os.path.join(self.tmp, "data", "forgefirm", "ext")
        os.makedirs(os.path.join(ext, "keys"))
        pub = os.path.join(self.tmp, "ref.pub")
        with open(pub, "wb") as f:
            f.write(b"key")

        def write_file(path, data):
            with open(path, "wb") as f:
                f.write(data)

        def print_ends(ctx, ev, off, job, offset, fc):
            while self.clock.t < self.END:
                time.sleep(0.0005)
        machine = {
            "time": self.clock, "EXT_ROOT": ext, "SAFE_FILE": os.path.join(self.tmp, "no-safe-mode"),
            "_host_pids": lambda: [1], "read_file": lambda path: "record", "record_path": lambda: "setup.json",
            "_tree": lambda root: {}, "request": lambda *a, **k: (200, b"", {"etag": "e1"}),
            "lan_ip": lambda: "192.0.2.1", "_pack_reference": lambda work, lan, **k: ("ref.ffx", pub),
            "write_file": write_file, "_forgeext": lambda *a, **k: {"ok": True},
            "_until": lambda ctx, cond, seconds, poll=0.25: True, "_svc": lambda id_: {"pid": 77},
            "_read": lambda path, default="": "still" if self.at("frozen") else str(int(self.clock.t * 2)),
            "_frozen": lambda id_: self.at("frozen"), "latch_locked": lambda: not self.at("unlocked"),
            "hw": types.SimpleNamespace(sysfs_read=lambda attr: "running" if self.at("running") else "idle"),
            "enter_offline": lambda ctx: 0, "offline_job": lambda ctx, name, seconds=30: "job",
            "Offline": FakeOffline, "dark_print_body": print_ends, "offline_cleanup": lambda ctx: None,
            "_put_back": lambda *a: None,
        }
        for name, value in machine.items():
            p = mock.patch.object(exthost, name, value)
            p.start()
            self.addCleanup(p.stop)
        p = mock.patch("tempfile.mkdtemp", return_value=self.tmp)
        p.start()
        self.addCleanup(p.stop)

    def at(self, what):
        span = self.spans[what]
        return span is not None and span[0] <= self.clock.t <= span[1]

    def judge(self):
        armed = lambda: self.OPEN <= self.clock.t <= self.CLOSE            # noqa: E731

        def get(path, params=None, raw=False):
            if path == "/cool/status":
                return 200, {"armed": armed()}
            if path == "/cam/snapshot":
                return (409, b"refused: the laser is armed") if armed() else (200, b"\xff\xd8\xff")
            return None, None
        fc = types.SimpleNamespace(base="http://forgectrl", host_header=lambda: "forgectrl",
                                   wait_idle=lambda timeout=None, abort=None: True,
                                   settings=lambda: {"ext_enabled": ""}, post=lambda *a, **k: (200, {}), get=get)
        ctx = Ctx(forgectrl=fc)
        exthost.armed_freeze(ctx)
        return ctx

    def test_a_freeze_in_place_before_the_run_passes(self):
        ctx = self.judge()
        self.assertGreater(ctx.evidence["freeze_led_the_run_s"], 0)

    def test_a_run_that_starts_before_the_freeze_fails(self):
        self.spans.update(frozen=(1006.5, 1026.0), running=(1005.2, 1024.0))
        with self.assertRaises(Failed) as cm:
            self.judge()
        self.assertIn("the run started before the freeze", str(cm.exception))

    def test_a_kernel_that_never_reads_running_fails(self):
        self.spans["running"] = None
        with self.assertRaises(Failed) as cm:
            self.judge()
        self.assertIn("the kernel never read running", str(cm.exception))


if __name__ == "__main__":
    unittest.main()
