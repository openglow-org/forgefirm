"""The cloud.* job-behavior tests against a fake forgectrl and a replayed
gfcloud log: the real test functions run under the real runner Context,
the operator prompts answered - and the standing notices of the machine
actions acted on - by a script that also drives the replay (open the
lid -> the machine's lid reads open; close it -> the service's re-hunt
lines land). The log lines are the machine's own: bench excerpts
(the lid-abort, button-wait, and lid-open-hunt runs) and, for the pause,
the run loop's lines as its host test emits them on the print skeleton
of the lid-abort excerpt.

What is proven here: the tests find the right lines in real log noise,
reuse an existing cloud session and never switch back, restart the client
for a fresh hunt, judge the print's own finish line (not another action's),
wait the service's deferred moves out, and fail for the right reasons."""
import contextlib
import json
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


# What the build under test logs around a print, in the machine's own format.
WARM_UP_LINE = ("2026-08-17T09:44:02.100000+00:00 gfcloud[1522] INFO "
                "machine:_dwell warm up: holding 3.0 s")
COOL_DOWN_LINE = ("2026-08-17T09:45:31.700000+00:00 gfcloud[1522] INFO "
                  "machine:_dwell cool down: holding 10.0 s")
PROGRESS_LINE = ("2026-08-17T09:44:05.200000+00:00 gfcloud[1522] INFO "
                 "machine:__init__ print:progress: reporting against 47848 bytes every 30 s")
LIMITS_LINE = ("2026-08-17T09:44:01.900000+00:00 gfcloud[1522] INFO "
               "machine:_motion job limits from the header: air_assist_min_rpm=116 "
               "coolant_max_c=33.0 coolant_min_c=5.0")
# What the engine logs when the job's limits reach it (forgectrl's log).
EFFECTIVE_LINE = ("2026-08-17T09:44:02.050000+00:00 forgectrl[410] INFO cool: effective limits: "
                  "coolant ceiling 33.0 C (local 33.0, header 33.0) resume 31.0 C; floors coolant "
                  "5.0 C, exhaust 0 rpm, intake 0 rpm, air assist 116 rpm (from the header, no gate yet)")


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
        self.engine_line = EFFECTIVE_LINE      # what the engine logs at the print; None = nothing
        self.client_limits = True              # the client names its header limits
        cloud.QUIET_S = 0.4
        cloud.QUIET_TIMEOUT_S = 3
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

    # -- session detection -------------------------------------------------
    def test_offline_mark_is_not_a_live_session(self):
        self.append(["2026-08-17T09:41:00.500000+00:00 gfcloud[3100] INFO websocket:_on_open RX-EVENT: ready",
                     OFFLINE_LINE])
        live, detail = cloud.session_live(3100)
        self.assertFalse(live)
        self.assertIn("offline", detail)
        self.assertTrue(cloud.client_offline(3100))

    # -- the emulator ------------------------------------------------------------------
    EMU_PID = 4100
    EMU_START = ["2026-08-22T23:50:00.100000+00:00 gfcloud[4100] INFO ffmachine:build_emulator EMULATE: the "
                 "emulator in this machine's identity, frames from /usr/share/gfutilities/emulator, no hardware",
                 "2026-08-22T23:50:02.000000+00:00 gfcloud[4100] INFO authentication:authenticate_machine SUCCESS",
                 "2026-08-22T23:50:03.000000+00:00 gfcloud[4100] INFO websocket:ws_connect ESTABLISHED",
                 "2026-08-22T23:50:05.000000+00:00 gfcloud[4100] INFO basemachine:_finish_action hunt [1578300001]: "
                 "finished with event \":completed\"",
                 "2026-08-22T23:50:06.000000+00:00 gfcloud[4100] INFO websocket:img_upload COMPLETE",
                 "2026-08-22T23:50:06.500000+00:00 gfcloud[4100] INFO basemachine:_finish_action lid_image [1578300002]: "
                 "finished with event \":completed\""]
    EMU_PRINT = ["2026-08-22T23:51:00.000000+00:00 gfcloud[4100] INFO gfuiservice:run service action request: print (ready)",
                 "2026-08-22T23:51:01.000000+00:00 gfcloud[4100] INFO websocket:fetch_motion pulse data is uncompressed, "
                 "47848 byte body",
                 "2026-08-22T23:51:02.000000+00:00 gfcloud[4100] INFO basemachine:_finish_action print [1578300003]: "
                 "finished with event \":completed\""]
    REAL_BACK = ["2026-08-22T23:52:01.000000+00:00 gfcloud[4200] INFO ffmachine:inhibit_connect_hunt NO-HUNT: the "
                 "first settings report in the reconnect form; the service keeps its head position, no "
                 "connect-time hunt",
                 "2026-08-22T23:52:02.000000+00:00 gfcloud[4200] INFO authentication:authenticate_machine SUCCESS",
                 "2026-08-22T23:52:03.000000+00:00 gfcloud[4200] INFO websocket:ws_connect ESTABLISHED",
                 "2026-08-22T23:52:03.500000+00:00 gfcloud[4200] INFO websocket:_on_open RX-EVENT: ready"]

    def emulator_setup(self, print_lines=None, info=True):
        self.in_cloud(pid=2278)
        self.append(["2026-08-22T23:49:00.500000+00:00 gfcloud[2278] INFO websocket:_on_open RX-EVENT: ready"])
        starts = []

        def on_post(path, form):
            if path == "/controller/stop":
                self.fc.state["mode"] = dict(self.fc.state["mode"], controller="standby", pid=0)
            elif path == "/controller/start":
                starts.append((os.path.exists(cloud.EMULATE_MARKER), os.path.exists(cloud.NOHUNT_MARKER)))
                if len(starts) == 1:
                    self.fc.state["mode"] = dict(self.fc.state["mode"], controller="running", pid=self.EMU_PID)
                    self.append(self.EMU_START, delay=0.2)
                else:
                    self.fc.state["mode"] = dict(self.fc.state["mode"], controller="running", pid=4200)
                    self.append(self.REAL_BACK, delay=0.2)
            return None
        self.fc.on_post = on_post

        def at_print():
            os.makedirs(cloud.EMULATOR_WORK, exist_ok=True)
            if info:
                with open(os.path.join(cloud.EMULATOR_WORK, "2026-08-22_235101_print.info"), "w") as f:
                    json.dump({"header_data": {"STfr": 10000, "MCsn": 0, "PDfm": 0}}, f)
            self.append(print_lines if print_lines is not None else self.EMU_PRINT, delay=0.1)
        return starts, {"Set up any small job": at_print}

    def test_service_protocol_against_the_emulator(self):
        starts, hooks = self.emulator_setup()
        run = self.run_test(cloud.service_protocol, hooks=hooks, test_id="cloud.service-protocol")
        ev = run.evidence
        # the emulator under its marker with the hunt; the real client back without the hunt
        self.assertEqual(starts, [(True, False), (False, True)])
        self.assertFalse(os.path.exists(cloud.EMULATE_MARKER))
        self.assertFalse(os.path.exists(cloud.NOHUNT_MARKER))
        self.assertTrue(any("no hunt" in l for l in run.lines), run.lines[-5:])
        self.assertEqual([p for p, _ in self.fc.posts],
                         ["/controller/stop", "/controller/start", "/controller/stop", "/controller/start"])
        self.assertIn(":completed", ev["hunt"])
        self.assertIn(":completed", ev["print"])
        self.assertEqual(ev["images_uploaded"], 1)
        self.assertEqual(ev["header_stfr"], 10000)
        self.assertEqual(self.script.asked, ["Does the app show the print complete?"])
        self.assertTrue(any("PASS: session, hunt" in l for l in run.lines), run.lines[-5:])
        self.assertFalse(os.path.exists(cloud.EMULATOR_WORK))

    def test_service_protocol_fails_when_the_print_does_not_complete_and_still_restores_the_client(self):
        failed = [l.replace(':completed"', ':failed"') for l in self.EMU_PRINT]
        starts, hooks = self.emulator_setup(print_lines=failed)
        self.assertFails(cloud.service_protocol, "did not complete", hooks=hooks)
        self.assertEqual(starts, [(True, False), (False, True)])   # the real client came back regardless

    def test_markers_the_client_consumed_are_not_missed(self):
        # gfcloud takes a marker it read down itself; the suite's own removal is then a no-op
        starts, hooks = self.emulator_setup()
        on_post = self.fc.on_post

        def consuming(path, form):
            r = on_post(path, form)
            if path == "/controller/start":
                for p in (cloud.EMULATE_MARKER, cloud.NOHUNT_MARKER):
                    if os.path.exists(p):
                        os.remove(p)
            return r
        self.fc.on_post = consuming
        run = self.run_test(cloud.service_protocol, hooks=hooks, test_id="cloud.service-protocol")
        self.assertEqual(starts, [(True, False), (False, True)])
        self.assertTrue(any("PASS: session, hunt" in l for l in run.lines), run.lines[-5:])
        self.assertFalse(any("marker removed" in l for l in run.lines), run.lines)

    def test_the_emulators_session_is_not_the_machines(self):
        self.append(self.EMU_START + ["2026-08-22T23:50:07.000000+00:00 gfcloud[4100] INFO websocket:_on_open "
                                      "RX-EVENT: ready"])
        live, detail = cloud.session_live(4100)
        self.assertFalse(live)
        self.assertIn("emulator", detail)

    # -- the offline entry ----------------------------------------------------------
    def test_enter_offline_reuses_a_running_offline_client(self):
        self.in_offline()
        run = Run("test", "cloud.x", "cloud.x")
        run.baseline_captured = {"mode": "cloud", "position": [0, 0, 0]}
        ctx = Context(run, None, helpers.make_test("cloud.x", []))
        cloud.enter_offline(ctx)
        self.assertEqual(self.fc.posts, [])
        self.assertFalse(os.path.exists(cloud.OFFLINE_MARKER))

    def test_enter_offline_from_grbl_sets_the_marker_for_one_start_and_declares_the_mode(self):
        seen = {}

        def on_post(path, form):
            if path == "/mode":
                seen["marker_at_start"] = os.path.exists(cloud.OFFLINE_MARKER)
                self.append([OFFLINE_LINE], delay=0.2)
            return None
        self.fc.on_post = on_post
        run = Run("test", "cloud.x", "cloud.x")
        run.baseline_captured = {"mode": "grbl", "position": [0, 0, 0]}
        ctx = Context(run, None, helpers.make_test("cloud.x", []))
        cloud.enter_offline(ctx)
        self.assertEqual([p for p, _ in self.fc.posts], ["/mode"])
        self.assertTrue(seen["marker_at_start"])                  # the client started offline
        self.assertFalse(os.path.exists(cloud.OFFLINE_MARKER))     # and the marker is gone again
        self.assertEqual(run.baseline_captured["mode"], "cloud")
        self.assertTrue(any("offline service up" in l for l in run.lines))

    def test_enter_offline_restarts_a_service_client_in_cloud_mode(self):
        self.in_cloud(pid=2278)

        def on_post(path, form):
            if path == "/controller/stop":
                self.fc.state["mode"] = dict(self.fc.state["mode"], controller="standby", pid=0)
            elif path == "/controller/start":
                self.fc.state["mode"] = dict(self.fc.state["mode"], controller="running", pid=3100)
                self.append([OFFLINE_LINE], delay=0.2)
            return None
        self.fc.on_post = on_post
        run = Run("test", "cloud.x", "cloud.x")
        run.baseline_captured = {"mode": "cloud", "position": [0, 0, 0]}
        ctx = Context(run, None, helpers.make_test("cloud.x", []))
        cloud.enter_offline(ctx)
        self.assertEqual([p for p, _ in self.fc.posts], ["/controller/stop", "/controller/start"])

    def test_enter_cloud_restarts_an_offline_client_with_the_service(self):
        self.in_offline()
        with open(cloud.OFFLINE_MARKER, "w") as f:
            f.write("stale\n")
        lines = fixture("huntlid")
        pre, post = cut(lines, "gfuiservice:__init__ INITIALIZED")
        hunt_part, _close = cut(post, "_switch_event lid closed")

        def on_post(path, form):
            if path == "/controller/stop":
                self.fc.state["mode"] = dict(self.fc.state["mode"], controller="standby", pid=0)
            elif path == "/controller/start":
                self.assertFalse(os.path.exists(cloud.OFFLINE_MARKER))   # taken down before the start
                self.fc.state["mode"] = dict(self.fc.state["mode"], controller="running", pid=2278)
                self.append(pre + hunt_part, delay=0.2)
            return None
        self.fc.on_post = on_post
        run = Run("test", "cloud.x", "cloud.x")
        run.baseline_captured = {"mode": "cloud", "position": [0, 0, 0]}
        ctx = Context(run, None, helpers.make_test("cloud.x", []))
        cloud.enter_cloud(ctx)
        self.assertEqual([p for p, _ in self.fc.posts], ["/controller/stop", "/controller/start"])
        self.assertTrue(any("offline service: restarting it with the service" in l for l in run.lines))

    def test_offline_jobs_are_the_synthesized_square(self):
        from forgetest import puls
        run = Run("test", "cloud.x", "cloud.x")
        ctx = Context(run, None, helpers.make_test("cloud.x", []))
        path = cloud.offline_job(ctx, "t.puls", seconds=5)
        tags, payload = puls.parse(open(path, "rb").read())
        self.assertEqual(tags["MCsn"], 0)
        self.assertEqual(tags["STfr"], 10000)
        self.assertFalse(any(b & 0x10 for b in payload if not b & 0x80))   # no LASER bit anywhere
        self.assertEqual(payload[0], 0x80)                                  # a leading power byte of zero
        self.assertEqual(run.evidence["jobs"]["t.puls"]["compressed"], False)

    def test_session_live_is_per_client(self):
        lines = fixture("huntlid")
        # the old client (1927) closed and left; the new one (2278) is ready
        self.append(lines)
        self.assertTrue(cloud.session_live(2278)[0])
        self.assertFalse(cloud.session_live(1927)[0])
        # an unknown pid falls back to the newest lines (ready)
        self.assertTrue(cloud.session_live(9999)[0])
        # a drop after the ready line: not live until it is ready again
        self.append(["2026-08-17T10:00:00.000000+00:00 gfcloud[2278] INFO websocket:_on_close RX-EVENT: closed (1006, )",
                     "2026-08-17T10:00:00.100000+00:00 gfcloud[2278] INFO websocket:run RECONNECTING"])
        live, detail = cloud.session_live(2278)
        self.assertFalse(live)
        self.assertIn("RECONNECTING", detail)
        self.append(["2026-08-17T10:00:06.000000+00:00 gfcloud[2278] INFO websocket:_on_open RX-EVENT: ready"])
        self.assertTrue(cloud.session_live(2278)[0])

    def test_enter_cloud_reuses_a_live_session_and_never_switches(self):
        self.in_cloud()
        self.append(fixture("huntlid"))
        run = self.run_test(lambda ctx: cloud.enter_cloud(ctx))
        self.assertTrue(any("reusing it" in l for l in run.lines), run.lines)
        self.assertEqual(self.fc.posts, [])
        self.assertEqual(run.baseline_captured["mode"], "cloud")

    NOHUNT_CLIENT = ["2026-08-22T23:52:01.000000+00:00 gfcloud[4200] INFO ffmachine:inhibit_connect_hunt NO-HUNT: the "
                     "first settings report in the reconnect form; the service keeps its head position, no "
                     "connect-time hunt",
                     "2026-08-22T23:52:02.000000+00:00 gfcloud[4200] INFO authentication:authenticate_machine SUCCESS",
                     "2026-08-22T23:52:03.000000+00:00 gfcloud[4200] INFO websocket:ws_connect ESTABLISHED",
                     "2026-08-22T23:52:03.500000+00:00 gfcloud[4200] INFO websocket:_on_open RX-EVENT: ready"]

    def test_session_hunted_is_the_clients_own_hunt(self):
        self.append(fixture("huntlid"))
        self.assertTrue(cloud.session_hunted(2278))           # its connect-time hunt completed
        self.assertFalse(cloud.session_hunted(9999))          # no lines: unknown is not hunted
        self.append(self.NOHUNT_CLIENT)
        self.assertTrue(cloud.session_live(4200)[0])
        self.assertFalse(cloud.session_hunted(4200))          # live, never hunted
        self.append(self.EMU_START)
        self.assertFalse(cloud.session_hunted(4100))          # the emulator's hunt is not the machine's
        # a later re-hunt (the lid closed) makes a no-hunt client hunted
        self.append(["2026-08-22T23:55:00.000000+00:00 gfcloud[4200] INFO basemachine:_finish_action hunt [1578300020]: "
                     "finished with event \":completed\""])
        self.assertTrue(cloud.session_hunted(4200))

    def test_enter_cloud_restarts_a_live_session_that_never_hunted_with_the_hunt(self):
        # the real print must not ride a head position the service only believes
        self.in_cloud(pid=4200)
        self.append(fixture("huntlid") + self.NOHUNT_CLIENT)
        lines = fixture("huntlid")
        _, post = cut(lines, "gfuiservice:__init__ INITIALIZED")
        starts = []

        def on_post(path, form):
            if path == "/controller/stop":
                self.fc.state["mode"] = dict(self.fc.state["mode"], controller="standby", pid=0)
            elif path == "/controller/start":
                starts.append(os.path.exists(cloud.NOHUNT_MARKER))
                self.fc.state["mode"] = dict(self.fc.state["mode"], controller="running", pid=2278)
                self.append(post, delay=0.2)
            return None
        self.fc.on_post = on_post
        run = self.run_test(lambda ctx: cloud.enter_cloud(ctx))
        self.assertEqual([p for p, _ in self.fc.posts], ["/controller/stop", "/controller/start"])
        self.assertEqual(starts, [False])                     # with the hunt: no marker
        self.assertTrue(any("never hunted" in l for l in run.lines), run.lines)
        self.assertTrue(any("connect-time hunt" in l and ":completed" in l for l in run.lines), run.lines)

    def test_enter_cloud_refuses_a_dead_session(self):
        self.in_cloud(pid=1927)                 # the client that shut down in the excerpt
        self.append(fixture("huntlid"))
        self.assertFails(cloud.enter_cloud, "no live service session")

    def test_enter_cloud_switches_once_from_grbl_and_declares_it(self):
        lines = fixture("huntlid")
        pre, post = cut(lines, "gfuiservice:__init__ INITIALIZED")
        self.append(pre)

        def on_post(path, form):
            if path == "/mode":
                self.append(post, delay=0.2)    # the new client's session + hunt + moves
            return None
        self.fc.on_post = on_post
        run = self.run_test(lambda ctx: cloud.enter_cloud(ctx))
        self.assertEqual([p for p, _ in self.fc.posts], ["/mode"])
        self.assertEqual(self.fc.state["mode"]["mode"], "cloud")
        self.assertEqual(run.baseline_captured["mode"], "cloud")     # declared: the baseline keeps it
        self.assertTrue(any("connect-time hunt" in l and ":completed" in l for l in run.lines), run.lines)

    def test_enter_cloud_waits_for_the_service_to_stop_moving(self):
        self.in_cloud()
        self.append(fixture("huntlid"))
        # a motion in flight (never idle within the timeout) fails, and says so
        self.fc.state["status"]["state"] = "running"
        self.assertFails(cloud.enter_cloud, "still running service moves")

    # -- the hunt with the lid open ------------------------------------------
    # -- the mode switch: hunt with the lid open, then $H -----------------------
    HUNT_RUN_SAMPLE = {"phase": "run", "verdict": "ok", "armed": False,
                       "fan_gates": {"exhaust": {"state": "unjudged", "reading": 0, "floor": 500}}}

    def mode_switch_setup(self, hunt_lines=None, home_complete=True):
        """The fakes a mode-switch run needs: grbl to answer $H, the lid
        lamp attr, homing_mode = gfcloud, the service lines landing on
        the switch to cloud (the hunt reads as a run to the cooling
        engine while it lasts), the re-hunt on the lid close, and gfhome
        finishing the homing after $H."""
        self.grbl = helpers.FakeGrbl().start()
        os.makedirs(self.sysfs + "pic", exist_ok=True)
        self._attr("pic/lid_led", "236")
        self.fc.state["settings"]["homing_mode"] = "gfcloud"
        lines = fixture("huntlid")
        pre, post = cut(lines, "gfuiservice:__init__ INITIALIZED")
        hunt_part, close_part = cut(post, "_switch_event lid closed")
        if hunt_lines is not None:
            hunt_part = hunt_lines(hunt_part)
        fc = self.fc

        def on_post(path, form):
            if path == "/mode" and form.get("controller") == "cloud":
                fc.state["cool"] = dict(self.HUNT_RUN_SAMPLE)

                def land():
                    self.append(pre, delay=0.0)
                    time.sleep(0.3)
                    self.append(hunt_part, delay=0.0)
                    time.sleep(4.0)          # the hunt outlasts the session wait on the bench
                    fc.state["cool"] = {"phase": "idle", "armed": False, "hold": False}
                threading.Thread(target=land, daemon=True).start()
            elif path == "/mode" and form.get("controller") == "grbl":
                self.grbl.state = "Idle"
            return None
        self.fc.on_post = on_post

        def lid_closed():
            self.lid(True)
            self.append(close_part, delay=0.2)

        def homing():
            self.grbl.state = "Home"
            time.sleep(0.4)
            self.grbl.state = "Idle"
            self.fc.state["status"]["homed"] = True
            if home_complete:
                with open(self.homelog, "ab") as f:
                    f.write(b"2026-08-17T09:46:00.000000+00:00 gfhome[2300] INFO homing complete "
                            b"(service quiet 8s, 3 motion windows)\n")
        self.home_hook = homing
        return {"Open the lid.": lambda: self.lid(False), "Close the lid.": lid_closed}

    def wait_home_command(self):
        while "$H" not in self.grbl.sent:
            time.sleep(0.02)
        self.home_hook()

    def test_mode_switch_round_trip_with_the_hunt_lid_open_and_the_homing(self):
        hooks = self.mode_switch_setup()
        threading.Thread(target=self.wait_home_command, daemon=True).start()
        run = self.run_test(cloud.mode_switch, hooks=hooks, test_id="cloud.mode-switch")
        ev = run.evidence
        posts = [(p, f.get("controller")) for p, f in self.fc.posts if p == "/mode"]
        self.assertEqual(posts, [("/mode", "cloud"), ("/mode", "grbl")])
        self.assertEqual(self.fc.state["mode"]["mode"], "grbl")
        self.assertIn(":completed", ev["hunt_line"])
        self.assertEqual(ev["refusals_before_hunt_end"], 0)
        self.assertTrue(ev["lens_homed"])
        self.assertEqual(ev["motions_after_lid_close"], 3)
        self.assertTrue(all(c.get("verdict") != "AIRFLOW" for c in ev["hunt_gates"]))
        self.assertTrue(ev["homed"])
        self.assertIn("3 motion windows", ev["gfhome_complete"])
        self.assertIn("$H", self.grbl.sent)
        # the lid was a machine action, not a prompt: two notices, no questions
        self.assertEqual([r["state"] for r in ev["actions"]], ["open", "close"])
        self.assertEqual(self.script.asked, [])
        self.assertTrue(any("PASS:" in l for l in run.lines), run.lines[-5:])

    def test_mode_switch_fails_when_the_hunt_is_refused_for_the_lid(self):
        def refused(hunt_part):
            i = next(i for i, l in enumerate(hunt_part) if "z_axis:home starting z homing cycle" in l)
            return hunt_part[:i] + ["2026-08-17T09:45:10.595000+00:00 gfcloud[2278] INFO machine:_safe_to_move "
                                    "lid opened, unsafe to move"] + hunt_part[i:]
        hooks = self.mode_switch_setup(hunt_lines=refused)
        self.assertFails(cloud.mode_switch, "refused for the lid", hooks=hooks)

    def test_mode_switch_fails_when_the_hunt_skipped_the_lens(self):
        hooks = self.mode_switch_setup(hunt_lines=lambda part: [l for l in part if "z homing cycle" not in l])
        self.assertFails(cloud.mode_switch, "did not home the lens", hooks=hooks)

    def test_mode_switch_fails_when_gfhome_never_saw_the_head_move(self):
        hooks = self.mode_switch_setup(home_complete=False)
        threading.Thread(target=self.wait_home_command, daemon=True).start()
        self.assertFails(cloud.mode_switch, "no 'homing complete' line", hooks=hooks)

    def test_mode_switch_needs_no_setting_from_the_operator(self):
        # The test sets the gfcloud homing itself and restores it: no precheck
        # names a setting the operator should change first.
        from forgetest import catalog
        t = catalog.load_suite()["cloud.mode-switch"]
        self.assertIsNone(getattr(t, "precheck", None))
        self.assertNotIn("homing_mode_is_gfcloud", dir(cloud))

    # -- pause / resume ---------------------------------------------------------
    def replay_print(self, name, at_run, at_end, tail_delay=0.3):
        """Split a print excerpt into: up to the run's RUNNING line
        (lands when the operator says Print is done), the block landing at
        the mid-run prompt, and the rest."""
        lines = fixture(name)
        pre, rest = cut(lines, "waiting for button")
        run_pre, rest = cut(rest, "current state: MachineState.RUNNING")     # the PRINT's run
        # The excerpt was captured before the machine held for a warm-up and
        # a rest, and before it reported a print's progress; the replay
        # carries those lines where it emits them now, rather than editing
        # what the machine actually said that day.
        run_pre = run_pre + ([LIMITS_LINE] if self.client_limits else []) + [WARM_UP_LINE, PROGRESS_LINE]
        pre, rest = pre + run_pre + [rest[0]], rest[1:]
        mid, tail = cut(rest, at_end)
        tail = tail + [COOL_DOWN_LINE]

        def at_done():
            self.append(pre, delay=0.1)
            if self.engine_line:
                with open(self.fclog, "ab") as f:
                    f.write((self.engine_line + "\n").encode())
        return {"Click Done here": at_done,
                at_run: lambda: (self.append(mid, delay=0.05), self.append(tail, delay=tail_delay))}

    def test_pause_resume_passes_on_the_machines_lines(self):
        self.in_cloud(pid=1522)
        self.hunted(1522)
        self.append(["2026-08-17T09:41:00.000000+00:00 gfcloud[1522] INFO authentication:authenticate_machine SUCCESS",
                     "2026-08-17T09:41:00.500000+00:00 gfcloud[1522] INFO websocket:_on_open RX-EVENT: ready",
                     "2026-08-17T09:41:00.900000+00:00 gfcloud[1522] INFO websocket:ws_connect ESTABLISHED"])
        hooks = self.replay_print("pause", "the press pauses the print", "current state: MachineState.IDLE")
        self.fc.state["cool"]["armed"] = True
        run = self.run_test(cloud.pause_resume, hooks=hooks, test_id="cloud.pause-resume")
        ev = run.evidence
        self.assertEqual(ev["log"], {"button pressed mid-run; pausing": True, "paused at": True,
                                     "button pressed while paused": True, "resuming (laser lead": True})
        self.assertTrue(ev["armed_after_resume"])
        self.assertIn(":completed", ev["log_end"]["print finished"])
        self.assertTrue(ev["log_end"]["return home complete"])
        self.assertEqual(ev["relock_or_cancel_lines"], 0)
        self.assertEqual(self.fc.posts, [])
        self.assertTrue(any("PASS: button pause/resume" in l for l in run.lines))
        # the post-print hunt was waited out
        self.assertTrue(any("machine is quiet" in l for l in run.lines))
        # the job's envelope passed through: the client's line and the engine's
        self.assertEqual(ev["header_limits"],
                         "air_assist_min_rpm=116 coolant_max_c=33.0 coolant_min_c=5.0")
        self.assertIn("header 33.0", ev["effective_limits"][0])       # the line with the header
        self.assertTrue(all("effective limits: coolant ceiling 33.0 C" in ln for ln in ev["effective_limits"]))

    def test_pause_resume_fails_when_the_client_names_no_header_limits(self):
        self.client_limits = False
        self.in_cloud(pid=1522)
        self.hunted(1522)
        self.append(["2026-08-17T09:41:00.500000+00:00 gfcloud[1522] INFO websocket:_on_open RX-EVENT: ready"])
        hooks = self.replay_print("pause", "the press pauses the print", "current state: MachineState.IDLE")
        self.fc.state["cool"]["armed"] = True
        self.assertFails(cloud.pause_resume, "named no job limits", hooks=hooks)

    def test_pause_resume_fails_when_the_engine_resolves_against_no_header(self):
        self.engine_line = EFFECTIVE_LINE.replace("header 33.0", "header none 0.0")
        self.in_cloud(pid=1522)
        self.hunted(1522)
        self.append(["2026-08-17T09:41:00.500000+00:00 gfcloud[1522] INFO websocket:_on_open RX-EVENT: ready"])
        hooks = self.replay_print("pause", "the press pauses the print", "current state: MachineState.IDLE")
        self.fc.state["cool"]["armed"] = True
        self.assertFails(cloud.pause_resume, "never resolved an effective ceiling", hooks=hooks)

    def test_pause_resume_fails_when_the_print_is_cancelled_instead(self):
        self.in_cloud(pid=1522)
        self.hunted(1522)
        self.append(["2026-08-17T09:41:00.500000+00:00 gfcloud[1522] INFO websocket:_on_open RX-EVENT: ready"])
        lines = fixture("pause")
        lines = [l.replace('print [1576550507]: finished with event ":completed"',
                           'print [1576550507]: finished with event ":cancelled"') for l in lines]
        pre, rest = cut(lines, "current state: MachineState.RUNNING")
        pre, rest = pre + [rest[0]], rest[1:]
        hooks = {"Click Done here": lambda: self.append(pre, delay=0.1),
                 "the press pauses the print": lambda: self.append(rest, delay=0.05)}
        self.assertFails(cloud.pause_resume, "did not complete after the resume", hooks=hooks)

    def test_pause_resume_fails_without_the_retraced_restart(self):
        """The second press was seen but the retraced restart never logged
        (the app's resume is its own line now): the failure names that."""
        self.in_cloud(pid=1522)
        self.hunted(1522)
        self.append(["2026-08-17T09:41:00.500000+00:00 gfcloud[1522] INFO websocket:_on_open RX-EVENT: ready"])
        lines = [l for l in fixture("pause") if "resuming (laser lead" not in l]
        pre, rest = cut(lines, "current state: MachineState.RUNNING")
        pre, rest = pre + [rest[0]], rest[1:]
        with self.fast_wait_log():
            hooks = {"Click Done here": lambda: self.append(pre, delay=0.1),
                     "the press pauses the print": lambda: self.append(rest, delay=0.05)}
            self.assertFails(cloud.pause_resume, "no retraced restart logged", hooks=hooks)

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

    def test_pause_resume_fails_without_the_pause_line(self):
        self.in_cloud(pid=1522)
        self.hunted(1522)
        self.append(["2026-08-17T09:41:00.500000+00:00 gfcloud[1522] INFO websocket:_on_open RX-EVENT: ready"])
        lines = [l for l in fixture("pause") if "button pressed mid-run" not in l]
        pre, rest = cut(lines, "current state: MachineState.RUNNING")
        pre, rest = pre + [rest[0]], rest[1:]
        with self.fast_wait_log():
            hooks = {"Click Done here": lambda: self.append(pre, delay=0.1),
                     "the press pauses the print": lambda: self.append(rest, delay=0.05)}
            self.assertFails(cloud.pause_resume, "did not pause the run", hooks=hooks)

    # -- the lid/interlock abort and the button-wait tests, on their excerpts ------
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

    def test_lid_interlock_abort_on_the_bench_excerpt(self):
        self.in_offline()
        prints = []
        run = self.run_test(cloud.lid_interlock_abort,
                            hooks=self.abort_hooks(*self.abort_parts(), prints),
                            test_id="cloud.lid-interlock-abort")
        ev = run.evidence
        self.assertEqual(prints, [9001, 9002])                 # two prints, handed over the socket
        self.assertTrue(FakeOffline.sent[0]["motion_url"].endswith("abort.puls"))
        self.assertEqual(ev["jobs"]["abort.puls"]["compressed"], False)
        # print 1: the lid
        self.assertLess(ev["edge_to_stop_ms"], 60)
        self.assertIn(":cancelled", ev["lid_log"]["print finished"])
        self.assertEqual(ev["lid_counters_after_park"], [0, 0, 3])
        self.assertTrue(ev["lid_latch_locked"])
        self.assertFalse(ev["lid_armed_after"])
        # print 2: the interlock, with the lid opened during the park
        self.assertFalse(ev["interlock_ok_after_pull"])
        self.assertIn(":cancelled", ev["interlock_log"]["print finished"])
        self.assertTrue(ev["interlock_log"]["return home complete"])
        self.assertEqual(ev["switches_at_return"], {"lid": False, "interlock_ok": False})
        self.assertEqual(ev["interlock_counters_after_park"], [0, 0, 3])
        self.assertTrue(ev["interlock_latch_locked"])
        self.assertEqual(ev["restored"], {"lid": True, "interlock_ok": True})
        self.assertEqual(self.fc.posts, [])
        self.assertTrue(any("PASS: lid open" in l for l in run.lines), run.lines)

    def test_lid_interlock_abort_refuses_when_the_loop_is_already_open(self):
        self.in_offline()
        self.fc.state["status"]["switches"]["interlock_ok"] = False
        self.assertFails(cloud.lid_interlock_abort, "already reads open")

    def test_lid_interlock_abort_fails_when_the_park_stops_at_the_lid(self):
        # the regression this guards: a park an open lid can interrupt
        self.in_offline()
        pre, lid_tail, ilk_stop, ilk_park = self.abort_parts()
        ilk_park = [l for l in ilk_park if "return home complete" not in l]
        saved = cloud.wait_log

        def fast_wait_log(ctx, offset, needles, timeout, poll=0.5):
            return saved(ctx, offset, needles, min(timeout, 1.5), poll=0.1)
        cloud.wait_log = fast_wait_log
        try:
            self.assertFails(cloud.lid_interlock_abort, "did not run to completion",
                             hooks=self.abort_hooks(pre, lid_tail, ilk_stop, ilk_park, []))
        finally:
            cloud.wait_log = saved

    def test_lid_interlock_abort_fails_when_the_lid_stop_is_not_edge_driven(self):
        # a polled stop (the pre-parity behavior) shows up as a long edge->stop gap
        self.in_offline()
        pre, lid_tail, ilk_stop, ilk_park = self.abort_parts()
        lid_tail = [l.replace("2026-08-17T09:42:30.838627", "2026-08-17T09:42:31.838627")
                    if "lid opened mid-run; stopping motion" in l else l for l in lid_tail]
        self.assertFails(cloud.lid_interlock_abort, "not edge-driven",
                         hooks=self.abort_hooks(pre, lid_tail, ilk_stop, ilk_park, []))

    def test_lid_during_button_wait_on_the_bench_excerpt(self):
        self.in_offline()
        lines = fixture("buttonwait")
        pre, rest = cut(lines, "waiting for button")
        # The bench excerpt is a print that fit the ring; the test now wants
        # one that did not, so the feeder is alive through the wait.
        long_job = ("2026-08-17T09:44:14.100000+00:00 gfcloud[1927] INFO machine:_feed_and_run "
                    "job is longer than the ring: 33554432 bytes enqueued, feeding the rest as it plays")
        pre, rest = pre + [long_job, rest[0]], rest[1:]
        self.offline_print_hooks(pre)
        hooks = {"do NOT press it": lambda: (self.lid(False), self.append(rest, delay=0.05)),
                 "Close the lid.": lambda: self.lid(True)}
        run = self.run_test(cloud.lid_during_button_wait, hooks=hooks, test_id="cloud.lid-during-button-wait")
        ev = run.evidence
        self.assertEqual([m["status"] for m in FakeOffline.sent], ["ready"])
        self.assertEqual(ev["runs_started_after_wait"], 0)
        self.assertIn(":cancelled", ev["log"]["print finished"])
        self.assertTrue(ev["latch_locked"])
        self.assertEqual(ev["program_total_after"], {"first": 0, "later": 0})
        self.assertEqual(ev["streaming_after"], 0)
        self.assertEqual(self.fc.posts, [])
        self.assertTrue(any("PASS: lid open at the button prompt" in l for l in run.lines))

    # -- the cooling verdict: the armed print waits on the warm-up ------------
    def test_verdict_hold_on_the_bench_excerpt(self):
        self.in_offline()
        self.fc.state["status"]["coolant"] = {"up_c": 21.5, "down_c": 21.4}
        self.fc.state["settings"].update({"cool_temp_min": "5", "cool_temp_start": "16"})
        self.fc.state["cool"].update({"verdict": "OK", "hold": False, "fire_ok": True, "armed": False})
        lines = fixture("pause")
        pre, rest = cut(lines, "waiting for button")
        pre = pre + [rest[0]]
        # the run and the print's end, without the pause in the middle
        _, run = cut(rest, "machine:_run_loop starting run")
        run = [l for l in run if "paus" not in l and "resum" not in l]
        stamp = "2026-08-17T09:44:14.%06d+00:00 gfcloud[1927] INFO machine:_verdict_wait "
        wait = [stamp % 100000 + "waiting on the cooling engine: WARMUP (WARM-UP: coolant 21.5 C under "
                "the 22.5 C start gate - heater on, hold)"]
        release = [stamp % 200000 + "cooling verdict clean after WARMUP; starting the run"]

        def on_post(path, form):
            if path == "/settings" and form.get("cool_temp_start") not in (None, "0", "16"):
                self.fc.state["cool"].update({"verdict": "WARMUP", "hold": True, "fire_ok": False,
                                              "armed": True, "phase": "warm-up"})
            return None
        self.fc.on_post = on_post
        self.offline_print_hooks(pre)

        def press():
            self.append(wait, delay=0.05)

            def released():
                self.fc.state["cool"].update({"verdict": "OK", "hold": False, "fire_ok": True,
                                              "armed": False, "phase": "run"})
                self.append(release + run, delay=0.0)
            threading.Timer(3.0, released).start()
        hooks = {"press it. The print then waits": press}
        run_ = self.run_test(cloud.verdict_hold, hooks=hooks, test_id="cloud.verdict-hold")
        ev = run_.evidence
        self.assertEqual(ev["gate"], 22.5)
        self.assertEqual(ev["warmup"]["verdict"], "WARMUP")
        self.assertTrue(ev["log"]["cooling verdict clean after WARMUP; starting the run"])
        self.assertIn(":completed", ev["print finished"])
        posted = [f for p, f in self.fc.posts if p == "/settings"]
        self.assertEqual(posted[0]["cool_temp_start"], "22.5")
        self.assertEqual(posted[-1], {"cool_temp_min": "5", "cool_temp_start": "16"})
        self.assertTrue(any("PASS: the armed print waited" in l for l in run_.lines))

    def test_verdict_hold_fails_when_the_print_runs_under_the_hold(self):
        self.in_offline()
        self.fc.state["status"]["coolant"] = {"up_c": 21.5, "down_c": 21.4}
        self.fc.state["settings"].update({"cool_temp_min": "5", "cool_temp_start": "16"})
        self.fc.state["cool"].update({"verdict": "WARMUP", "hold": True, "fire_ok": False, "armed": True})
        lines = fixture("pause")
        pre, rest = cut(lines, "waiting for button")
        pre = pre + [rest[0]]
        _, run = cut(rest, "machine:_run_loop starting run")
        stamp = "2026-08-17T09:44:14.%06d+00:00 gfcloud[1927] INFO machine:_verdict_wait "
        wait = [stamp % 100000 + "waiting on the cooling engine: WARMUP (no reason given)"]
        self.offline_print_hooks(pre)
        hooks = {"press it. The print then waits": lambda: self.append(wait + run[:1], delay=0.05)}
        self.assertFails(cloud.verdict_hold, "the run started under the warm-up hold", hooks=hooks)

    # -- a paused print canceled by the lid, a running one by the app --------
    def cancel_parts(self):
        """(print prologue, the pause lines, the lid stop + park + cancel,
        the same tail with the app's cancel as the trigger - what
        cloud.oversize-stream's ending looks like)."""
        lines = fixture("lidabort")
        pre, rest = cut(lines, "waiting for button")
        run_pre, rest = cut(rest, "machine:_run_loop starting run")
        pre, rest = pre + run_pre + [rest[0]], rest[1:]
        paused = ["2026-08-17T09:42:25.500000+00:00 gfcloud[1522] INFO machine:_run_loop "
                  "button pressed mid-run; pausing",
                  "2026-08-17T09:42:26.100000+00:00 gfcloud[1522] INFO machine:_run_loop "
                  "paused at Position(x=41.2, y=17.0, z=0.0)"]
        app_cancel = [l.replace("lid opened mid-run; stopping motion",
                                "action canceled mid-run; stopping motion") for l in rest]
        return pre, paused, rest, app_cancel

    def test_paused_lid_cancel_on_the_bench_excerpt(self):
        self.in_offline()
        pre, paused, lid_tail, _app = self.cancel_parts()
        self.offline_print_hooks(pre)
        run = self.run_test(cloud.paused_lid_cancel,
                            hooks={"the press pauses the print": lambda: self.append(paused, delay=0.05),
                                   "The print is paused: leave the lid open": lambda: (
                                       self.lid(False), self.append(lid_tail, delay=0.05)),
                                   "Close the lid.": lambda: self.lid(True)},
                            test_id="cloud.paused-lid-cancel")
        ev = run.evidence
        self.assertEqual(len(FakeOffline.sent), 1)            # one print, handed over the socket
        self.assertEqual(ev["paused"], {"button pressed mid-run; pausing": True, "paused at": True})
        self.assertIn(":cancelled", ev["lid_from_pause"]["print finished"])
        self.assertTrue(ev["lid_from_pause"]["return home complete"])
        self.assertEqual(ev["counters_after"], [0, 0, 3])
        self.assertTrue(ev["latch_locked_after"])
        self.assertFalse(ev["armed_after"])
        self.assertFalse(ev["button_dark"])
        self.assertEqual(self.fc.posts, [])
        self.assertTrue(any("PASS: a paused print canceled by the lid" in l for l in run.lines), run.lines)

    def test_paused_lid_cancel_fails_when_the_paused_print_resumes_instead(self):
        # a lid that resumed (or was ignored) leaves the print ':completed'
        self.in_offline()
        pre, paused, lid_tail, _app = self.cancel_parts()
        lid_tail = [l.replace(':cancelled"', ':completed"') for l in lid_tail]
        self.offline_print_hooks(pre)
        self.assertFails(
            cloud.paused_lid_cancel, "the print did not end ':cancelled'",
            hooks={"the press pauses the print": lambda: self.append(paused, delay=0.05),
                   "The print is paused: leave the lid open": lambda: (
                       self.lid(False), self.append(lid_tail, delay=0.05))})

    def test_print_finish_is_the_prints_not_another_actions(self):
        # a motion that completes before the print must not satisfy the print's finish
        lines = fixture("lidabort")
        i = cloud.action_finish_index(lines, "print")
        j = cloud.action_finish_index(lines, "motion")
        self.assertIsNotNone(i)
        self.assertIsNotNone(j)
        self.assertLess(j, i)
        self.assertIn(":cancelled", lines[i])
        self.assertIn(":completed", lines[j])


if __name__ == "__main__":
    unittest.main()
