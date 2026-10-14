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
thing - a dark print with the latch open at the button wait, a lid stop
that is polled rather than edge-driven, and a resume the kernel refused."""
import contextlib
import os
import shutil
import struct
import tempfile
import threading
import time
import unittest

import helpers
from forgetest.runner import Context, Failed, Run
from forgetest.suite import cloud

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

    # -- the dark print exthost.armed-freeze runs: locked at the button --------
    def dark_parts(self):
        """(the print prologue through the button prompt, the run and the
        print's end with no pause in it)."""
        lines = fixture("pause")
        pre, rest = cut(lines, "waiting for button")
        pre = pre + [rest[0]]
        _, run = cut(rest, "machine:_run_loop starting run")
        run = [l for l in run if "paus" not in l and "resum" not in l]
        return pre, run

    def test_dark_print_fails_when_the_latch_is_open_at_the_button(self):
        self.in_offline()
        pre, run = self.dark_parts()
        self._attr("cnc/interlock_circuit", "37")          # unlocked before the press
        self.offline_print_hooks(pre)
        hooks = {"press it. The print runs dark": lambda: self.append(run, delay=0.05)}
        self.assertFails(dark_print, "the latch is unlocked at the button wait", hooks=hooks)


if __name__ == "__main__":
    unittest.main()
