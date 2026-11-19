# Copyright 2026 514 LLC d/b/a OpenGlow
# Written by Scott Wiederhold
# https://community.openglow.org
# SPDX-License-Identifier:    MIT

"""The baseline: fixed resting values are restored, preserved values are
handed back, every deviation is a recorded leftover. Runs against a fake
sysfs tree. The base fixture is a host run with no forgectrl (the service
side is skipped, and says so); BaselineModeTests run against a fake
forgectrl, and ForgectrlSilenceTests against one that stops answering."""
import json
import os
import shutil
import struct
import tempfile
import unittest
from unittest import mock

import helpers
from forgetest import baseline, hw

# The fixed machine tick at the mode an unset xy_microsteps reads as.
# Taken from baseline rather than typed, so a change to the default mode
# moves the expectations with it.
DEFAULT_TICK = dict(baseline.fixed_sysfs())["cnc/step_freq"]


class BaselineFixture:
    """The fake sysfs tree and LED class a baseline runs against. It has
    no tests of its own, so a class that mixes it in runs only its own."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="forgetest-bl-")
        self.sysfs = os.path.join(self.tmp, "sysfs") + os.sep
        self.leds = os.path.join(self.tmp, "leds") + os.sep
        for group in ("cnc", "pic", "head", "thermal"):
            os.makedirs(self.sysfs + group)
        for name in baseline.BUTTON_LEDS + ("lid_led",):
            os.makedirs(self.leds + name)
            self._led(name, "0")
        # A clean machine at the mode an unset xy_microsteps reads as:
        # x/y_mode, step_freq and ramp_rate are the mode's, not the x8
        # literals in FIXED_SYSFS, and that is what enforce() compares
        # against.
        for attr, val in baseline.fixed_sysfs() + baseline.IDLE_READBACKS:
            self._attr(attr, val)
        self._attr("cnc/interlock_circuit", "45")
        self._attr("pic/lid_led", "0")
        self._pos(0, 0, 0)
        os.environ["GF_SYSFS_ROOT"] = self.sysfs
        os.environ["GF_LEDS_ROOT"] = self.leds
        os.environ["FORGECTRL_URL"] = "http://127.0.0.1:1"      # nothing listens
        helpers.host_run(self)
        helpers.latch_drives_interlock(self)
        self.lines = []

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)
        for k in ("GF_SYSFS_ROOT", "GF_LEDS_ROOT", "FORGECTRL_URL"):
            os.environ.pop(k, None)

    def _attr(self, attr, val):
        with open(self.sysfs + attr, "w") as f:
            f.write(str(val))

    def _read(self, attr):
        with open(self.sysfs + attr) as f:
            return f.read().strip()

    def _led(self, name, val):
        with open(self.leds + name + "/brightness", "w") as f:
            f.write(val)
        # the class interface writes 'target'; the fake mirrors it into brightness
        # only when the test asks (see _sync_leds)

    def _sync_leds(self):
        for name in baseline.BUTTON_LEDS:
            p = self.leds + name + "/target"
            if os.path.exists(p):
                with open(p) as f:
                    v = f.read().strip()
                with open(self.leds + name + "/brightness", "w") as f:
                    f.write(v)

    def _pos(self, x, y, z):
        with open(self.sysfs + "cnc/position", "wb") as f:
            f.write(struct.pack("<5i", x, y, z, 0, 0))

    def bl(self):
        return baseline.Baseline(self.lines.append)

    def _pos_bytes(self, x, y, z, processed, total):
        with open(self.sysfs + "cnc/position", "wb") as f:
            f.write(struct.pack("<3i2I", x, y, z, processed, total))

    def _probe_state(self):
        # what the kernel shows between the supervisor's motion probe and
        # the GRBL controller's init writes
        self._attr("cnc/motor_lock", "0")
        self._attr("cnc/step_freq", "10000")
        self._attr("cnc/y_mode", "1")


class BaselineTests(BaselineFixture, unittest.TestCase):
    def test_clean_machine_has_no_leftovers(self):
        left = self.bl().enforce("pre", captured=None)
        self.assertEqual(left, [])
        self.assertTrue(any("pre: clean" in l for l in self.lines))

    def test_fixed_values_are_restored_and_recorded(self):
        self._attr("cnc/motor_lock", "15")
        self._attr("cnc/step_freq", "10000")
        self._attr("cnc/streaming", "1")
        left = self.bl().enforce("post", captured=None)
        items = {x.item: x for x in left}
        self.assertEqual(set(items), {"cnc/motor_lock", "cnc/step_freq", "cnc/streaming"})
        for x in left:
            self.assertEqual(x.action, "restored", str(x))
        self.assertEqual(self._read("cnc/motor_lock"), "0")
        self.assertEqual(self._read("cnc/step_freq"), DEFAULT_TICK)
        self.assertEqual(self._read("cnc/streaming"), "0")
        self.assertEqual(items["cnc/motor_lock"].found, "15")
        self.assertEqual(items["cnc/motor_lock"].expected, "0")

    def test_unlocked_latch_is_relocked(self):
        self._attr("cnc/interlock_circuit", "5")        # bit 3 clear = unlocked
        left = self.bl().enforce("post", captured=None)
        self.assertEqual([x.item for x in left], ["laser_latch"])
        self.assertEqual(self._read("cnc/laser_latch"), "1")

    def test_readonly_deviation_is_unrestorable(self):
        self._attr("cnc/state", "disabled")
        left = self.bl().enforce("post", captured=None)
        self.assertEqual([(x.item, x.action) for x in left], [("cnc/state", "unrestorable")])

    def test_laser_on_after_a_run_is_a_leftover(self):
        self._attr("cnc/laser_on", "1")
        left = self.bl().enforce("post", captured=None)
        self.assertEqual([(x.item, x.action) for x in left], [("cnc/laser_on", "unrestorable")])

    def test_the_fire_line_is_read_before_the_relock(self):
        # A lock makes the FIRE pad an input, which then reads low: a FIRE
        # line driven with the latch unlocked shows only in a read taken
        # before the relock.
        self._attr("cnc/interlock_circuit", "7")        # FIRE driven (bit 1), latch unlocked (bit 3 clear)
        self._attr("cnc/laser_enable", "1")
        real = hw.sysfs_write

        def write(attr, value):
            real(attr, value)
            if attr == "cnc/laser_latch" and str(value) == "1":
                real("cnc/laser_enable", "0")
        with mock.patch.object(hw, "sysfs_write", write):
            left = self.bl().enforce("post", captured=None)
        self.assertEqual(sorted(x.item for x in left), ["cnc/laser_enable", "laser_latch"])
        self.assertEqual(self._read("cnc/laser_latch"), "1")

    def test_what_the_laser_side_cannot_read_is_a_leftover_and_the_lock_goes_in(self):
        # unreadable at the start, readable and locked again by the end: the
        # read at the start is the one that names it
        os.remove(self.sysfs + "cnc/interlock_circuit")
        os.remove(self.sysfs + "cnc/laser_on")
        left = {x.item: x for x in self.bl().enforce(
            "post", captured=None, after_laser=lambda found: self._attr("cnc/interlock_circuit", "45"))}
        self.assertEqual((left["laser_latch"].found, left["cnc/laser_on"].found), ("unreadable", "unreadable"))
        self.assertEqual(self._read("cnc/laser_latch"), "1")

    def test_a_latch_word_that_turns_unreadable_by_the_end_is_named(self):
        left = {x.item: x for x in self.bl().enforce(
            "post", captured=None, after_laser=lambda found: os.remove(self.sysfs + "cnc/interlock_circuit"))}
        self.assertEqual(left["laser_latch"].found, "unreadable")

    def test_the_lock_is_written_on_a_machine_whose_kernel_state_cannot_be_read(self):
        os.environ.pop("FORGETEST_HOST_RUN", None)
        os.remove(self.sysfs + "cnc/state")
        with mock.patch.object(baseline, "FC_WAIT_S", 0):
            self.bl().enforce("post", captured=None)
        self.assertEqual(self._read("cnc/laser_latch"), "1")

    def test_a_lens_step_alone_is_a_leftover(self):
        # Z is the lens, which no return moves: the X/Y dead band does not excuse it
        b = self.bl()
        cap = b.capture()
        self._pos(0, 0, 2)
        self.assertEqual([x.item for x in b.enforce("post", captured=cap)], ["position"])

    def test_an_unreadable_kernel_is_a_leftover_on_a_machine(self):
        # Only a host run skips a kernel side it cannot read.
        os.environ.pop("FORGETEST_HOST_RUN", None)
        os.environ["GF_SYSFS_ROOT"] = os.path.join(self.tmp, "nope") + os.sep
        with mock.patch.object(baseline, "FC_WAIT_S", 0):
            left = {x.item: x for x in self.bl().enforce("post", captured=None)}
        self.assertTrue(left["kernel sysfs"].action.startswith("failed"), left["kernel sysfs"])

    def test_loop_heater_is_the_flow_watchs_only_at_its_duty(self):
        b = self.bl()
        # Holding at 3.3 %: the heater at that duty is the watch's own.
        self._attr("thermal/heater_pwm", "2162")
        b._flow_status = lambda: {"watch": "hold", "duty": 3.3}
        self.assertEqual(b.enforce("post", captured=None), [])
        self.assertEqual(self._read("thermal/heater_pwm"), "2162")
        # Anything else is a leftover, put back to 0: a heater far from the
        # hold's duty, one under a resting watch (its heater is off, whatever
        # duty it reports), one with the watch off or not there.
        for status, value in (({"watch": "hold", "duty": 3.3}, "65535"),
                              ({"watch": "rest", "duty": 3.3}, "2162"),
                              ({"watch": "off"}, "2162"),
                              ({}, "2162")):
            self._attr("thermal/heater_pwm", value)
            b._flow_status = lambda s=status: s
            left = b.enforce("post", captured=None)
            self.assertEqual([(x.item, x.found, x.action) for x in left],
                             [("thermal/heater_pwm", value, "restored")], status)
            self.assertEqual(self._read("thermal/heater_pwm"), "0")
        # A hold that starts between the two reads of the watch.
        self._attr("thermal/heater_pwm", "2162")
        seq = iter(({"watch": "rest"}, {"watch": "hold", "duty": 3.3}))
        b._flow_status = lambda: next(seq)
        self.assertEqual(b.enforce("post", captured=None), [])

    def test_a_failure_left_for_acknowledgment_is_acknowledged_and_named(self):
        b = self.bl()
        posted = []
        status = {"phase": "idle", "armed": False, "hold": False,
                  "attention": {"standing": True, "live": False, "verdict": "CRASH"},
                  "notices": [{"seq": 3, "verdict": "BUMP"}]}
        b.fc_get = lambda path: (200, status) if path == "/cool/status" else (None, None)
        b.fc_post = lambda path, **kw: posted.append(path) or (200, {})
        left = []
        b._phase = "pre"
        b._cool_side(left)
        self.assertEqual(posted, ["/cool/attention/ack"], "before a test the panel's notices stand")
        posted[:] = []
        left = []
        b._phase = "post"
        b._cool_side(left, {"notices": {}})         # no notice stood before the run
        self.assertEqual([(x.item, x.found, x.action) for x in left],
                         [("attention", "CRASH", "restored (acknowledged)")])
        self.assertEqual(posted, ["/cool/attention/ack", "/cool/notices/dismiss"])
        # A failure that still stands is not acknowledged away.
        status["attention"] = {"standing": True, "live": True, "verdict": "CRASH"}
        status["notices"] = []
        posted[:] = []
        left = []
        b._cool_side(left)
        self.assertEqual(posted, [])

    def test_the_runs_notices_are_recorded_and_those_from_before_it_stand(self):
        b = self.bl()
        posted = []
        status = {"phase": "idle", "armed": False, "hold": False, "attention": {},
                  "notices": [{"seq": 3, "verdict": "BUMP", "at": 100, "times": 1}]}
        b.fc_get = lambda path: (200, status) if path == "/cool/status" else (None, None)
        b.fc_post = lambda path, **kw: posted.append((path, kw.get("params"))) or (200, {})
        cap = b.capture()
        # The run: the notice from before it counted again, and a new one.
        status["notices"] = [{"seq": 3, "verdict": "BUMP", "at": 100, "times": 2},
                             {"seq": 4, "verdict": "FLAME", "at": 160, "times": 1}]
        b._phase = "post"
        left = []
        b._cool_side(left, cap)
        self.assertEqual(left, [], "a notice is no leftover")
        self.assertEqual([(n["seq"], n["verdict"], n.get("times_before"), n["dismissed"]) for n in b.run_notices],
                         [(3, "BUMP", 1, False), (4, "FLAME", None, False)])
        self.assertEqual(posted, [], "a dismissal would take the notice from before the run")
        # Nothing from before the run stands (the daemon started again, and
        # its numbers with it): the run's notice goes, up to its own number.
        status["notices"] = [{"seq": 3, "verdict": "FLAME", "at": 500, "times": 1}]
        b._cool_side(left, cap)
        self.assertEqual([(n["seq"], n["verdict"], n["dismissed"]) for n in b.run_notices], [(3, "FLAME", True)])
        self.assertEqual(posted, [("/cool/notices/dismiss", {"upto": "3"})])
        # The notices could not be read before the run: none can be told
        # apart, so none is the run's and every one stands.
        posted[:] = []
        b.fc_get = lambda path: (None, None)
        cap = b.capture()
        b.fc_get = lambda path: (200, status) if path == "/cool/status" else (None, None)
        b._cool_side(left, cap)
        self.assertEqual((posted, b.run_notices), ([], []))

    def test_a_pulsing_button_is_a_leftover_whatever_its_target_reads(self):
        # A breathing LED's target swings between 0 and its level: the pulse
        # is what says it was left on.
        for name in baseline.BUTTON_LEDS:
            with open(self.leds + name + "/pulse_on", "w") as f:
                f.write("1800")
            with open(self.leds + name + "/target", "w") as f:
                f.write("0")
        left = self.bl().enforce("post", captured=None)
        self.assertEqual(sorted(x.item for x in left if x.item.startswith("leds/")),
                         ["leds/" + n for n in baseline.BUTTON_LEDS])
        with open(self.leds + baseline.BUTTON_LEDS[0] + "/pulse_on") as f:
            self.assertEqual(f.read().strip(), "0")

    def test_the_button_is_not_judged_while_a_failure_stands(self):
        # The engine's red flash (channel 1 pulsing) while a failure stands
        # is the machine's own state, and is left as it is.
        for attr, val in (("pulse_on", "300"), ("target", "255")):
            with open(self.leds + "button_led_1/" + attr, "w") as f:
                f.write(val)
        b = self.bl()
        status = {"phase": "idle", "armed": False, "hold": False,
                  "attention": {"standing": True, "live": True, "verdict": "CRASH"}}
        b.fc_get = lambda path: (200, status) if path == "/cool/status" else (None, None)
        self.assertEqual(b.enforce("post", captured=None), [])
        with open(self.leds + "button_led_1/pulse_on") as f:
            self.assertEqual(f.read().strip(), "300")
        # With no failure standing, the same LED is a leftover.
        status["attention"] = {}
        self.assertEqual([x.item for x in b.enforce("post", captured=None)], ["leds/button_led_1"])

    def test_button_leds_are_turned_off(self):
        self._led("button_led_2", "255")
        left = self.bl().enforce("post", captured=None)
        self.assertEqual([x.item for x in left], ["leds/button_led_2"])
        self._sync_leds()
        self.assertEqual(baseline.read_led("button_led_2"), "0")

    def test_preserved_position(self):
        b = self.bl()
        cap = b.capture()
        self.assertEqual(cap["position"], [0, 0, 0])
        # the run shifted the counters
        self._pos(1000, 0, 0)
        left = b.enforce("post", captured=cap)
        items = {x.item: x for x in left}
        self.assertEqual(set(items), {"position"})
        # no GRBL controller on the host: the head cannot be jogged back
        self.assertTrue(items["position"].action.startswith("unrestorable"), items["position"].action)
        self.assertEqual(items["position"].found, [1000, 0, 0])

    def test_a_lost_counter_frame_never_moves_the_head(self):
        # The controller zeroes the step counters at every start and at
        # every home, and rewrites its anchor then. A run that began at
        # -6400 steps and restarted the controller ends at 0 with the head
        # where it was: the difference is not a distance, and the hand-back
        # must neither jog the head nor call it a leftover.
        anchor = os.path.join(self.tmp, "grblhal.homed")
        old_path, baseline.ANCHOR_PATH = baseline.ANCHOR_PATH, anchor
        try:
            with open(anchor, "w") as f:
                f.write("0 0 0 4 startup")
            self._pos(-6400, 0, 0)
            b = self.bl()
            cap = b.capture()
            self.assertIsNotNone(cap["frame"])
            os.unlink(anchor)                       # a controller start: a new anchor file
            with open(anchor + ".new", "w") as f:
                f.write("0 0 0 4 startup")
            os.replace(anchor + ".new", anchor)
            os.utime(anchor, ns=(5, 5))             # a freed inode can come straight back, inside one clock tick
            self._pos(0, 0, 0)
            left = b.enforce("post", captured=cap)
            self.assertEqual([x.item for x in left], [])
            self.assertTrue(any("re-zeroed its counters during the run" in l for l in self.lines), self.lines)
            # the same counters with the frame intact are a displaced head, as before
            self.lines.clear()
            self._pos(-6400, 0, 0)
            cap = b.capture()
            self._pos(0, 0, 0)
            left = b.enforce("post", captured=cap)
            self.assertEqual([x.item for x in left], ["position"])
            # and a test that vouches for the new frame is held to it
            self._pos(-6400, 0, 0)
            cap = b.capture()
            os.utime(anchor, ns=(1, 1))             # re-zeroed, and declared (ctx.counters_rezeroed)
            cap["position"], cap["rezero_declared"] = [0, 0, 0], True
            self._pos(0, 0, 0)
            self.assertEqual(b.enforce("post", captured=cap), [])
            self._pos(1000, 0, 0)
            self.assertEqual([x.item for x in b.enforce("post", captured=cap)], ["position"])
        finally:
            baseline.ANCHOR_PATH = old_path

    def test_the_counters_are_read_at_the_modes_scale(self):
        # 6400 steps is 30 mm at x32 (a return within the bound; on the
        # host it stops at the missing controller) and 120 mm at x8
        # (beyond the bound). Read at the x8 scale, an x32 machine's
        # displaced head is never jogged back.
        b = self.bl()
        cap = b.capture()
        self._attr("cnc/x_mode", "32")
        self._pos(6400, 0, 0)
        items = {x.item: x for x in b.enforce("post", captured=cap)}
        self.assertEqual(items["position"].action, "unrestorable: no running GRBL controller")
        self._attr("cnc/x_mode", "8")
        items = {x.item: x for x in b.enforce("post", captured=cap)}
        self.assertEqual(items["position"].action, "unrestorable: 120.0/0.0 mm exceeds 100 mm")

    def test_a_held_controller_is_reset_before_the_return_jog(self):
        # a pause test that failed while held leaves the controller in
        # Hold, which refuses a jog: the baseline resets out of it first
        import helpers
        fc = helpers.FakeForgectrl().start()
        dev = helpers.FakeGrbl().start()
        try:
            dev.state = "Hold:0"
            dev.reset_to = "Idle"
            dev.on_command = lambda line: self._pos(0, 0, 0) if line.startswith("$J=") else None
            b = self.bl()
            cap = b.capture()
            # 4.144 mm into the held move, in the steps of the mode in force
            held_mm = 4.144
            self._pos(round(held_mm * baseline.xy_steps_per_mm(baseline.XY_MODE_DEFAULT)), 0, 0)
            left = b.enforce("post", captured=cap)
            items = {x.item: x for x in left}
            self.assertTrue(items["position"].action.startswith("restored"), items["position"].action)
            self.assertEqual(dev.sent[0], "^X")
            jogs = [l for l in dev.sent if l.startswith("$J=")]
            self.assertEqual(len(jogs), 1)
            self.assertIn("X-%.3f" % held_mm, jogs[0])
            self.assertTrue(any("reset out of Hold:0" in l for l in self.lines), self.lines)
        finally:
            dev.stop()
            fc.stop()
            for k in ("GRBL_HOST", "GRBL_PORT"):
                os.environ.pop(k, None)

    def test_ring_residue_is_a_leftover_and_blocks_the_return_jog(self):
        b = self.bl()
        cap = b.capture()
        # the run left 40 unplayed bytes queued in the kernel ring and the
        # head 1000 counts out: the hand-back stands the machine down to
        # empty the ring, and only refuses the return jog when the bytes
        # are still there afterwards (they are here: no daemon to restart
        # a controller, whose wait runs on a clock that moves when it sleeps)
        self._pos_bytes(1000, 0, 0, 100, 140)
        with mock.patch.object(baseline, "time", helpers.Clock()):
            left = b.enforce("post", captured=cap)
        items = {x.item: x for x in left}
        self.assertIn("pulse ring", items)
        self.assertEqual(items["pulse ring"].found, "40 unplayed bytes")
        self.assertIn("still queued", items["position"].action)
        self.assertIn("after a controller restart", items["position"].action)
        self.assertEqual(baseline.read_ring_residue(), 40)

    def test_the_hand_back_stands_the_machine_down_for_residue(self):
        """It acts, it does not report: a run that left bytes in the ring
        gets a controller restart out of the hand-back, because that is
        what empties the ring."""
        b = self.bl()
        cap = b.capture()
        called = []
        b.stand_down = lambda why: called.append(why)
        self._pos_bytes(1000, 0, 0, 100, 140)
        b.enforce("post", captured=cap)
        self.assertEqual(len(called), 1)
        self.assertIn("unplayed bytes", called[0])

    def test_clean_ring_reads_zero_residue(self):
        self.assertEqual(baseline.read_ring_residue(), 0)

    def test_lamp_needs_forgectrl(self):
        # the lamp's idle level comes from forgectrl's settings: without the
        # daemon there is nothing to compare against
        self._attr("pic/lid_led", "77")
        left = self.bl().enforce("pre", captured=None)
        self.assertEqual(left, [])
        self.assertEqual(self._read("pic/lid_led"), "77")

    def test_no_sysfs_means_skip(self):
        os.environ["GF_SYSFS_ROOT"] = os.path.join(self.tmp, "nope") + os.sep
        left = self.bl().enforce("pre", captured=None)
        self.assertEqual(left, [])
        self.assertTrue(any("kernel sysfs not present" in l for l in self.lines))

    def test_boot_reference_needs_a_recent_boot(self):
        os.environ["FORGETEST_BOOT_ID"] = "test-boot"
        try:
            # no reference file, uptime unknown on a host without /proc/uptime,
            # or too old: None, with the reason logged
            ref = baseline.boot_reference(self.lines.append, self.tmp)
            up = baseline.uptime_s()
            if up is None or up > baseline.BOOT_MAX_AGE_S:
                self.assertIsNone(ref)
                self.assertTrue(any("no fresh-boot reference" in l for l in self.lines))
            else:
                # a young host: the reference is taken from the fake tree
                self.assertIsNotNone(ref)
                self.assertEqual(ref["sysfs"]["cnc/motor_lock"], "0")
                self.assertTrue(os.path.exists(os.path.join(self.tmp, "boot-test-boot.json")))
                # and loaded back the second time
                self.lines[:] = []
                ref2 = baseline.boot_reference(self.lines.append, self.tmp)
                self.assertEqual(ref2["ts"], ref["ts"])
                self.assertTrue(any("reference loaded" in l for l in self.lines))
        finally:
            os.environ.pop("FORGETEST_BOOT_ID", None)

    def test_fixed_constants_checked_against_a_dump(self):
        ref = {"sysfs": {"cnc/motor_lock": "0", "cnc/step_freq": "10000"}}
        diffs = baseline.check_fixed_against(ref, self.lines.append)
        self.assertEqual(diffs, ["cnc/step_freq: boot=10000 constant=%s" % DEFAULT_TICK])


    # -- the reference is taken after the controller applied its config -----

    def test_wait_configured_returns_once_the_controller_wrote_its_config(self):
        self._probe_state()
        calls = {"n": 0}

        def sleep(_s):
            calls["n"] += 1
            if calls["n"] == 3:             # the controller's init writes land
                # at the mode wait_controller_configured watches by default
                for attr, val in baseline.configured_markers():
                    self._attr(attr, val)
        ok = baseline.wait_controller_configured(
            self.lines.append, {"controller": "running", "mode": "grbl", "motion": "verified"},
            timeout=5, sleep=sleep)
        self.assertTrue(ok)
        self.assertTrue(any("controller configured" in l for l in self.lines))
        self.assertGreaterEqual(calls["n"], 4)      # 3 polls + the settle

    def test_wait_configured_times_out_and_says_so(self):
        self._probe_state()
        t = {"now": 0.0}
        real_time = baseline.time.time
        baseline.time.time = lambda: t["now"]
        try:
            def sleep(s):
                t["now"] += s
            ok = baseline.wait_controller_configured(
                self.lines.append, {"controller": "running", "mode": "grbl"}, timeout=2, sleep=sleep)
        finally:
            baseline.time.time = real_time
        self.assertFalse(ok)
        self.assertTrue(any("did not apply its config" in l for l in self.lines))

    def test_wait_configured_is_a_noop_outside_grbl_mode(self):
        self._probe_state()
        ok = baseline.wait_controller_configured(
            self.lines.append, {"controller": "running", "mode": "cloud"}, timeout=1,
            sleep=lambda s: self.fail("slept in cloud mode"))
        self.assertTrue(ok)
        ok = baseline.wait_controller_configured(
            self.lines.append, {"controller": "stopped", "mode": "grbl"}, timeout=1,
            sleep=lambda s: self.fail("slept with the controller stopped"))
        self.assertTrue(ok)

    def test_preconfig_reference_is_recognized(self):
        self.assertTrue(baseline.reference_preconfig(
            {"sysfs": {"cnc/motor_lock": "0", "cnc/step_freq": "10000", "cnc/y_mode": "1"}}))
        # The markers are read at the reference's own mode, so a dump taken
        # on an x8 machine carries the setting that says so.
        self.assertFalse(baseline.reference_preconfig(
            {"sysfs": {"cnc/motor_lock": "8", "cnc/step_freq": "28160", "cnc/y_mode": "8"},
             "forgectrl": {"/settings": {"xy_microsteps": "8"}}}))
        # a genuinely different single constant is a machine fact, not pre-config
        self.assertFalse(baseline.reference_preconfig(
            {"sysfs": {"cnc/motor_lock": "8", "cnc/step_freq": "10000", "cnc/y_mode": "8"},
             "forgectrl": {"/settings": {"xy_microsteps": "8"}}}))
        self.assertFalse(baseline.reference_preconfig({"sysfs": {}}))

    # -- the XY microstep mode drives the fixed values ------------------------

    def test_the_fixed_values_follow_the_microstep_mode(self):
        x8 = dict(baseline.fixed_sysfs(8))
        self.assertEqual(x8, dict(baseline.FIXED_SYSFS))
        x16 = dict(baseline.fixed_sysfs(16))
        self.assertEqual((x16["cnc/x_mode"], x16["cnc/y_mode"], x16["cnc/step_freq"], x16["cnc/ramp_rate"]),
                         ("16", "16", "56320", "250000"))
        x32 = dict(baseline.fixed_sysfs(32))
        self.assertEqual((x32["cnc/x_mode"], x32["cnc/step_freq"], x32["cnc/ramp_rate"]),
                         ("32", "112640", "500000"))
        # everything else is the same list, in the same order
        self.assertEqual([a for a, _ in baseline.fixed_sysfs(32)], [a for a, _ in baseline.FIXED_SYSFS])
        self.assertEqual(x32["cnc/x_decay"], x8["cnc/x_decay"])

    def test_the_mode_is_read_from_the_settings_with_a_default(self):
        # An unset or unusable key reads as the driver's default, x32.
        self.assertEqual(baseline.xy_mode_of({"xy_microsteps": "16"}), 16)
        self.assertEqual(baseline.xy_mode_of({"xy_microsteps": "32"}), 32)
        self.assertEqual(baseline.xy_mode_of({"xy_microsteps": "8"}), 8)
        self.assertEqual(baseline.xy_mode_of({"xy_microsteps": ""}), 32)
        self.assertEqual(baseline.xy_mode_of({}), 32)
        self.assertEqual(baseline.xy_mode_of(None), 32)
        self.assertEqual(baseline.xy_mode_of({"xy_microsteps": "24"}), 32)
        self.assertEqual(baseline.xy_mode_of({"xy_microsteps": "abc"}), 32)
        self.assertEqual(baseline.ref_xy_mode({"forgectrl": {"/settings": {"xy_microsteps": "16"}}}), 16)
        self.assertEqual(baseline.ref_xy_mode({"forgectrl": {"/settings": None}}), 32)
        self.assertEqual(baseline.ref_xy_mode({}), 32)

    def test_fixed_constants_are_checked_at_the_reference_mode(self):
        ref = {"sysfs": {"cnc/x_mode": "16", "cnc/step_freq": "56320", "cnc/ramp_rate": "250000"},
               "forgectrl": {"/settings": {"xy_microsteps": "16"}}}
        self.assertEqual(baseline.check_fixed_against(ref, self.lines.append), [])
        ref["forgectrl"]["/settings"]["xy_microsteps"] = "8"
        diffs = baseline.check_fixed_against(ref, self.lines.append)
        self.assertEqual(diffs, ["cnc/x_mode: boot=16 constant=8",
                                 "cnc/step_freq: boot=56320 constant=28160",
                                 "cnc/ramp_rate: boot=250000 constant=125000"])

    def test_wait_configured_watches_the_mode_in_force(self):
        self._probe_state()
        calls = {"n": 0}

        def sleep(_s):
            calls["n"] += 1
            if calls["n"] == 2:             # an x16 controller's init writes land
                self._attr("cnc/step_freq", "56320")
                self._attr("cnc/y_mode", "16")
        ok = baseline.wait_controller_configured(
            self.lines.append, {"controller": "running", "mode": "grbl", "motion": "verified"},
            timeout=5, sleep=sleep, xy_mode=16)
        self.assertTrue(ok)
        self.assertTrue(any("controller configured" in l for l in self.lines))
        # a reference taken under x16 is not pre-config
        self.assertFalse(baseline.reference_preconfig(
            {"sysfs": {"cnc/step_freq": "56320", "cnc/y_mode": "16"},
             "forgectrl": {"/settings": {"xy_microsteps": "16"}}}))
        self.assertTrue(baseline.reference_preconfig(
            {"sysfs": {"cnc/step_freq": "10000", "cnc/y_mode": "1"},
             "forgectrl": {"/settings": {"xy_microsteps": "16"}}}))

    def test_stale_preconfig_reference_is_retaken_on_a_fresh_boot(self):
        # Both sides of BOOT_MAX_AGE_S, with the uptime held: read from the host, it can cross the limit
        # between boot_reference()'s reading and the test's (it did, on a CI runner 600 s after its boot).
        os.environ["FORGETEST_BOOT_ID"] = "test-boot-2"
        real = baseline.uptime_s
        try:
            for up, fresh in ((baseline.BOOT_MAX_AGE_S - 60, True), (baseline.BOOT_MAX_AGE_S + 60, False)):
                self.lines.clear()
                path = os.path.join(self.tmp, "boot-test-boot-2.json")
                with open(path, "w") as f:
                    json.dump({"ts": "old", "sysfs": {"cnc/motor_lock": "0", "cnc/step_freq": "10000",
                                                         "cnc/y_mode": "1"}}, f)
                baseline.uptime_s = lambda up=up: up
                # the retake's settle wait, with no daemon, on a clock that moves when it sleeps
                with mock.patch.object(baseline, "time", helpers.Clock()):
                    ref = baseline.boot_reference(self.lines.append, self.tmp)
                if fresh:
                    self.assertTrue(any("retaking" in l for l in self.lines), self.lines)
                    self.assertNotEqual(ref["ts"], "old")
                    self.assertEqual(ref["sysfs"]["cnc/motor_lock"], "0")
                else:
                    # too old to retake: the stale reference stands, marked
                    self.assertTrue(any("predates the controller's config" in l for l in self.lines), self.lines)
                    self.assertEqual(ref["ts"], "old")
        finally:
            baseline.uptime_s = real
            os.environ.pop("FORGETEST_BOOT_ID", None)


class ExtensionFreeTests(BaselineFixture, unittest.TestCase):
    """A campaign's machine is extension-free: what a test made is removed,
    what is the operator's is reported and left alone."""

    def setUp(self):
        super().setUp()
        self.root = os.path.join(self.tmp, "ext")
        for d in ("pkg", "keys", "data"):
            os.makedirs(os.path.join(self.root, d))
        # a stand-in for the host's command line: remove <id> takes the package away
        self.forgeext = os.path.join(self.tmp, "forgeext")
        with open(self.forgeext, "w") as f:
            f.write('#!/bin/sh\n[ "$1" = remove ] && rm -rf "%s/pkg/$2" && echo \'{"ok": true}\'\n' % self.root)
        os.chmod(self.forgeext, 0o755)
        self.pool = {}
        self._saved = (baseline.hw.EXT_ROOT, baseline.hw.FORGEEXT, baseline.hw.pool_pids, baseline.EXT_STOP_S)
        baseline.hw.EXT_ROOT, baseline.hw.FORGEEXT = self.root, self.forgeext
        baseline.hw.pool_pids = lambda: dict(self.pool)
        baseline.EXT_STOP_S = 0.2

    def tearDown(self):
        baseline.hw.EXT_ROOT, baseline.hw.FORGEEXT, baseline.hw.pool_pids, baseline.EXT_STOP_S = self._saved
        super().tearDown()

    def _package(self, id_):
        os.makedirs(os.path.join(self.root, "pkg", id_, "1.0.0"))

    def _key(self, name):
        with open(os.path.join(self.root, "keys", name), "w") as f:
            f.write("x")

    def test_an_installed_package_that_does_not_run_is_nobodys_leftover(self):
        self._package("org.example.notify")
        self._key("owner.pub")
        self.assertEqual(self.bl().enforce("pre", captured=None), [])

    def test_what_a_test_left_behind_is_removed_and_the_operators_is_not(self):
        self._package("org.forgetest.reference")
        self._package("org.example.notify")
        self._key("forgetest-reference.pub")
        self._key("owner.pub")
        left = {x.item: x for x in self.bl().enforce("post", captured=None)}
        self.assertEqual(sorted(left), ["ext.owner_key", "ext.package"])
        self.assertEqual((left["ext.package"].found, left["ext.package"].action), ("org.forgetest.reference", "restored"))
        self.assertEqual((left["ext.owner_key"].found, left["ext.owner_key"].action), ("forgetest-reference.pub", "restored"))
        self.assertEqual(os.listdir(os.path.join(self.root, "pkg")), ["org.example.notify"])
        self.assertEqual(os.listdir(os.path.join(self.root, "keys")), ["owner.pub"])

    def test_a_package_that_cannot_be_removed_says_so(self):
        self._package("org.forgetest.reference")
        with open(self.forgeext, "w") as f:
            f.write('#!/bin/sh\necho \'{"ok": false, "error": "the lock is held"}\'\nexit 1\n')
        left = self.bl().enforce("post", captured=None)
        self.assertEqual([x.item for x in left], ["ext.package"])
        self.assertTrue(left[0].action.startswith("failed:") and "the lock is held" in left[0].action, left[0].action)

    def test_running_extensions_are_reported_and_left_alone(self):
        self._package("org.example.notify")
        self.pool = {4242: 800}
        left = self.bl().enforce("pre", captured=None)
        self.assertEqual([(x.item, x.action) for x in left], [("ext.running", "unrestorable")])
        self.assertIn("org.example.notify", left[0].found)
        self.assertTrue(os.path.isdir(os.path.join(self.root, "pkg", "org.example.notify")))


class ForgectrlSilenceTests(BaselineFixture, unittest.TestCase):
    """On a machine (no host run) a forgectrl that does not answer is never a
    clean hand-back: a pass waits for it once, and what went unanswered is a
    leftover. One miss silences only the pass it happened in."""

    def setUp(self):
        super().setUp()
        os.environ.pop("FORGETEST_HOST_RUN", None)
        self.fc = helpers.FakeForgectrl().start()
        self.addCleanup(self.fc.stop)
        self._attr("pic/lid_led", "240")
        p = mock.patch.object(baseline, "time", helpers.Clock())
        p.start()
        self.addCleanup(p.stop)

    def test_a_daemon_gone_at_the_post_pass_is_a_leftover(self):
        b = self.bl()
        cap = b.capture()
        url = self.fc.url
        self.fc.stop()
        os.environ["FORGECTRL_URL"] = url                   # the port is closed now
        left = {x.item: x for x in b.enforce("post", captured=cap)}
        self.assertTrue(left["forgectrl"].action.startswith("failed"), left["forgectrl"])

    def test_one_missed_request_does_not_blind_the_pass(self):
        # A request that times out at the start of a pass, with forgectrl
        # answering after it: the pass waits, and the run's change is found.
        self.fc.state["settings"]["cool_max_c"] = "30"
        b = self.bl()
        cap = b.capture()
        self.fc.state["settings"]["cool_max_c"] = "40"
        real = b.fc().get
        missed = []

        def flaky(path, **kw):
            if not missed:
                missed.append(path)
                raise hw.HwError("forgectrl GET %s: timed out" % path)
            return real(path, **kw)
        b.fc().get = flaky
        left = {x.item: x for x in b.enforce("post", captured=cap)}
        self.assertEqual(missed, ["/mode"])
        self.assertEqual(left["settings.cool_max_c"].action, "restored")
        self.assertNotIn("forgectrl", left)

    def test_the_latch_is_locked_before_forgectrl_is_waited_for(self):
        self.fc.stop()
        os.environ["FORGECTRL_URL"] = self.fc.url           # silent for the whole pass
        self._attr("cnc/interlock_circuit", "37")
        clock = baseline.time
        t0 = clock.t
        locked_at = []
        real = hw.sysfs_write

        def write(attr, value):
            if attr == "cnc/laser_latch":
                locked_at.append(clock.t)
            real(attr, value)
        with mock.patch.object(hw, "sysfs_write", write):
            self.bl().enforce("post", captured=None)
        self.assertEqual(locked_at[0], t0)

    def test_with_the_mode_unread_the_controllers_values_are_left(self):
        # forgectrl silent: a cloud program's motor mask and run current are
        # not the GRBL controller's to put back
        self.fc.stop()
        os.environ["FORGECTRL_URL"] = self.fc.url
        self._attr("cnc/motor_lock", "8")
        self._attr("pic/x_step_current", "135")
        left = {x.item for x in self.bl().enforce("post", captured=None)}
        self.assertEqual((self._read("cnc/motor_lock"), self._read("pic/x_step_current")), ("8", "135"))
        self.assertIn("controller mode", left)

    def test_a_mode_the_settings_cannot_tell_leaves_the_values_that_follow_it(self):
        # an x8 machine and one timed-out request: the settings that name the
        # mode go unread, and nothing is written for a default mode
        self.fc.state["settings"]["xy_microsteps"] = "8"
        for attr, val in baseline.fixed_sysfs(8):
            self._attr(attr, val)
        b = self.bl()
        real = b.fc().get
        missed = []

        def flaky(path, **kw):
            if path == "/diag/status" and not missed:
                missed.append(path)
                raise hw.HwError("forgectrl GET %s: timed out" % path)
            return real(path, **kw)
        b.fc().get = flaky
        left = {x.item: x for x in b.enforce("post", captured=None)}
        self.assertEqual([self._read(a) for a in baseline.XY_MODE_SYSFS],
                         [v for a, v in baseline.fixed_sysfs(8) if a in baseline.XY_MODE_SYSFS])
        self.assertIn("xy_microsteps", left)

    def test_a_status_report_that_is_not_an_answer_is_a_miss(self):
        # a diagnostic behind an error status is not "not running"
        self.fc.on_get = lambda path, q: (503, {"running": True, "tool": "fans"}) if path == "/diag/status" else None
        left = {x.item: x for x in self.bl().enforce("post", captured=None)}
        self.assertIn("/diag/status", left["forgectrl"].found)


class CloudOwnedSysfsTests(unittest.TestCase):
    """In cloud mode the cloud client configures the machine from the
    pulse header it is playing, the lens included: z_mode comes from
    ZSmd through gfhardware's set_mode_from_puls. Handing the GRBL values
    back under it would be the baseline configuring another controller's
    machine. In GRBL mode the pair is checked as before."""

    def test_the_z_pair_is_the_cloud_clients(self):
        for attr in ("head/z_current", "head/z_mode"):
            self.assertIn(attr, baseline.GRBL_CONTROLLER_SYSFS)

    def test_the_z_pair_is_still_checked_in_grbl_mode(self):
        fixed = dict(baseline.fixed_sysfs())
        self.assertEqual(fixed.get("head/z_current"), "1")
        self.assertEqual(fixed.get("head/z_mode"), "1")


class PositionDeadbandTests(unittest.TestCase):
    """The counters count steps and a controller's own return lands within
    a few hundredths of a millimeter, not on the step: a difference that
    small is quantization, not a leftover."""

    def test_past_the_dead_band_is_a_leftover(self):
        over = int(baseline.POSITION_DEADBAND_MM * baseline.XY_STEPS_PER_MM) + 2
        self.assertFalse(baseline.position_quantized([0, 0, 0], [over, 0, 0]))
        self.assertFalse(baseline.position_quantized([0, 0, 0], [0, over, 0]))

    def test_an_unreadable_reading_is_never_quantization(self):
        self.assertFalse(baseline.position_quantized(None, [0, 0, 0]))
        self.assertFalse(baseline.position_quantized([0, 0, 0], None))

    def test_the_dead_band_is_a_tenth_of_a_millimeter(self):
        spm = baseline.XY_STEPS_PER_MM
        self.assertTrue(baseline.position_quantized([0, 0, 0], [round(0.05 * spm), 0, 0], spm))
        self.assertFalse(baseline.position_quantized([0, 0, 0], [round(0.5 * spm), 0, 0], spm))


class BaselineModeTests(BaselineFixture, unittest.TestCase):
    """The baseline against a fake forgectrl: what the mode in force
    owns, over the same fake sysfs tree."""

    def setUp(self):
        super().setUp()
        self.fc = helpers.FakeForgectrl().start()
        # the lamp's settle, short: five polls, so a lamp that never holds
        # costs the suite no real seconds
        self._lamp_timing = (baseline.LID_LAMP_SETTLE_S, baseline.LID_LAMP_POLL_S)
        baseline.LID_LAMP_SETTLE_S, baseline.LID_LAMP_POLL_S = 0.05, 0.01

    def tearDown(self):
        baseline.LID_LAMP_SETTLE_S, baseline.LID_LAMP_POLL_S = self._lamp_timing
        self.fc.stop()
        super().tearDown()

    def cloud(self):
        self.fc.state["mode"] = {"mode": "cloud", "controller": "running", "pid": 7, "motion": "verified"}
        self.fc.state["settings"]["controller_mode"] = "cloud"

    def clocked(self):
        """The hand-back's waits on a clock that moves only when it sleeps."""
        clock = helpers.Clock()
        p = mock.patch.object(baseline, "time", clock)
        p.start()
        self.addCleanup(p.stop)
        return clock

    def lamp_at_idle(self):
        self._attr("pic/lid_led", "240")

    def restarts(self, on_stop=None):
        """forgectrl's controller stop and start: a stop ends the job (the
        window, the hold) and leaves the controller in standby."""
        def on_post(path, form):
            if path == "/controller/stop":
                self.fc.state["mode"] = dict(self.fc.state["mode"], controller="standby")
                self.fc.state["cool"] = {"phase": "idle", "armed": False, "hold": False}
                if on_stop:
                    on_stop()
            elif path == "/controller/start":
                self.fc.state["mode"] = dict(self.fc.state["mode"], controller="running")
            elif path == "/diag/abort":
                self.fc.state["diag"] = {"running": False}
            return None
        self.fc.on_post = on_post

    def test_an_armed_window_is_ended_before_anything_is_waited_for(self):
        # Emission first, then motion: the window a run left open is ended
        # at once, before the diagnostic it also left is waited on.
        self.clocked()
        self.lamp_at_idle()
        self.fc.state["cool"] = {"phase": "run", "armed": True, "hold": False}
        self.fc.state["diag"] = {"running": True, "tool": "fans"}
        self.restarts()
        left = {x.item: x for x in self.bl().enforce("post", captured=None)}
        paths = [p for p, _ in self.fc.posts]
        self.assertLess(paths.index("/controller/stop"), paths.index("/diag/abort"))
        self.assertTrue(left["cool.armed"].action.startswith("restored"), left["cool.armed"])

    def test_a_job_still_moving_is_stood_down_before_anything_is_waited_on(self):
        # with a controller running, the motion is a job's: motion verified,
        # or the probe skipped (motors released), which /mode reports unverified
        self.clocked()
        for motion in ("verified", "unverified"):
            with self.subTest(motion=motion):
                self.lamp_at_idle()
                self.fc.posts.clear()
                self.fc.state["mode"] = dict(self.fc.state["mode"], controller="running", motion=motion)
                self.fc.state["status"]["state"] = "running"
                self.fc.state["diag"] = {"running": True, "tool": "fans"}
                self.restarts(on_stop=lambda: self.fc.state["status"].__setitem__("state", "idle"))
                left = {x.item: x for x in self.bl().enforce("post", captured=None)}
                paths = [p for p, _ in self.fc.posts]
                self.assertIn("state", left)
                self.assertLess(paths.index("/controller/stop"), paths.index("/diag/abort"))

    def test_a_supervisor_still_starting_has_its_own_motion_waited_out(self):
        # no controller yet: the liveness probe and the lens sweep move the
        # head for 40 s, longer than a job is given to reach idle, and that is
        # the supervisor's own work, not a job to stand down
        clock = self.clocked()
        self.lamp_at_idle()
        up = clock.t + 40

        def on_get(path, q):
            starting = clock.t < up
            if path == "/mode":
                return 200, dict(self.fc.state["mode"], controller="stopped" if starting else "running",
                                 motion="unverified" if starting else "verified")
            if path == "/status":
                return 200, dict(self.fc.state["status"], state="running" if starting else "idle")
            return None
        self.fc.on_get = on_get
        self.restarts()
        self.bl().enforce("post", captured=None)
        self.assertNotIn("/controller/stop", [p for p, _ in self.fc.posts])

    def test_a_read_that_decides_whether_a_job_is_there_fails_closed(self):
        # an armed window the cooling engine's report never shows (an error
        # status on every read), and a job still moving whose state read at
        # the stand-down check times out: each read says nothing, and the
        # machine is stood down as if the job were there
        self.clocked()
        with self.subTest(missed="/cool/status"):
            self.lamp_at_idle()
            self.fc.state["cool"] = {"phase": "run", "armed": True, "hold": False}
            self.fc.state["diag"] = {"running": True, "tool": "fans"}
            self.restarts()
            self.fc.on_get = lambda path, q: (503, {"error": "busy"}) if path == "/cool/status" else None
            self.bl().enforce("post", captured=None)
            paths = [p for p, _ in self.fc.posts]
            self.assertLess(paths.index("/controller/stop"), paths.index("/diag/abort"))     # before the waits
        with self.subTest(missed="/status"):
            self.fc.posts.clear()
            self.fc.state["cool"] = {"phase": "idle", "armed": False, "hold": False}
            self.fc.state["status"]["state"] = "running"
            self.fc.state["diag"] = {"running": True, "tool": "fans"}
            self.restarts(on_stop=lambda: self.fc.state["status"].__setitem__("state", "idle"))
            reads = []

            def on_get(path, q):
                if path == "/status":
                    reads.append(path)
                    if len(reads) == 2:                  # the first is the latch's, the second the state's
                        return 503, {"error": "busy"}
                return None
            self.fc.on_get = on_get
            self.bl().enforce("post", captured=None)
            paths = [p for p, _ in self.fc.posts]
            self.assertLess(paths.index("/controller/stop"), paths.index("/diag/abort"))     # before the waits

    def test_a_late_arm_behind_a_miss_is_stood_down_and_its_unlock_named_first(self):
        # a client that arms after /status (a print still loading) and one
        # request that times out: the cooling engine's read is silenced and
        # stands the machine down as if armed. forgectrl's stop locks the
        # latch, so the unlock is read, locked and named before the stop.
        self.clocked()
        self.lamp_at_idle()
        self.restarts()
        b = self.bl()
        real = b.fc().get

        def get(p, **kw):
            if p == "/cam/status":
                self._attr("cnc/laser_latch", "0")              # the client's unlock for the press
                self._attr("cnc/interlock_circuit", "37")
                self.fc.state["cool"] = {"phase": "run", "armed": True, "hold": False}
                raise hw.HwError("forgectrl GET /cam/status: timed out")
            return real(p, **kw)
        b.fc().get = get
        left = {x.item for x in b.enforce("post", captured=None)}
        self.assertIn("/controller/stop", [p for p, _ in self.fc.posts])
        self.assertEqual((self._read("cnc/laser_latch"), "laser_latch" in left), ("1", True))

    def test_the_latch_is_read_before_every_controller_change_the_pass_makes(self):
        # a client unlocks just before the supervisor branch acts: the mode
        # handed back, a motion fault's restart, a start from standby. Each
        # of them stops a controller or starts one, and forgectrl's exit locks
        # the latch, so the unlock is named before the request.
        self.clocked()
        for case in ("a mode change", "a motion fault", "standby"):
            with self.subTest(case):
                self.lamp_at_idle()
                self._attr("cnc/interlock_circuit", "45")
                self.fc.state["mode"] = {"mode": "grbl", "pid": 7, "motion": "verified",
                                         "controller": {"a motion fault": "motion-fault",
                                                        "standby": "standby"}.get(case, "running")}
                locked_at_request = []

                def on_post(path, form):
                    if path in ("/mode", "/controller/start"):
                        locked_at_request.append(bool(hw.sysfs_int("cnc/interlock_circuit") & baseline.LATCH_BIT))
                        self.fc.state["mode"].update(controller="running")
                    return None
                self.fc.on_post = on_post
                self.fc.on_get = lambda path, q: (self._attr("cnc/interlock_circuit", "37")
                                                  if path == "/diag/status" else None)
                cap = {"mode": "cloud"} if case == "a mode change" else None
                left = [x.item for x in self.bl().enforce("post", captured=cap)]
                self.assertEqual((locked_at_request[:1], "laser_latch" in left), ([True], True))

    def unlock_at_ext(self):
        """The latch unlocked once, while the pass looks at the extensions:
        after every forgectrl read, before the preserved state."""
        real = baseline.Baseline._ext_side
        done = []

        def ext(bl, left):
            if not done:
                done.append(1)
                self._attr("cnc/interlock_circuit", "37")
            return real(bl, left)
        p = mock.patch.object(baseline.Baseline, "_ext_side", ext)
        p.start()
        self.addCleanup(p.stop)

    def test_the_latch_is_read_before_a_settings_hand_back_that_restarts_the_controller(self):
        # xy_microsteps written back under a running GRBL controller restarts
        # it, and forgectrl's stop locks the latch: the unlock is locked and
        # named before the write
        self.clocked()
        self.lamp_at_idle()
        self.fc.state["settings"]["xy_microsteps"] = "16"
        locked_at_write = []

        def on_post(path, form):
            if path == "/settings" and "xy_microsteps" in form:
                locked_at_write.append(bool(hw.sysfs_int("cnc/interlock_circuit") & baseline.LATCH_BIT))
            return None
        self.fc.on_post = on_post
        self.unlock_at_ext()
        left = [x.item for x in self.bl().enforce("post", captured={"settings": {"xy_microsteps": "8"},
                                                                    "mode": "grbl"})]
        self.assertEqual((locked_at_write, "laser_latch" in left), ([True], True))

    def test_the_latch_is_read_before_the_pass_connects_to_the_grbl_port(self):
        # a connection ends another sender's window, and the controller then
        # relocks: the settings write-back and the return jog read it first,
        # the return right before it connects (an unlock while it waits for
        # the controller)
        self.clocked()
        for case in ("settings", "return"):
            with self.subTest(case):
                self._attr("cnc/interlock_circuit", "45")
                self._pos(0, 0, 0)
                if case == "settings":
                    dev, view, b, cap = self.grbl_view({"$12": "0.002"})
                    view["$12"] = "0.01"
                    self.unlock_at_ext()
                else:
                    dev = helpers.FakeGrbl().start()
                    self.addCleanup(dev.stop)
                    self.lamp_at_idle()
                    self.fc.on_get = None
                    b = self.bl()
                    cap = b.capture()
                    self._pos(round(4 * baseline.xy_steps_per_mm(baseline.XY_MODE_DEFAULT)), 0, 0)
                    returning = []
                    real = baseline.Baseline._return_head
                    p = mock.patch.object(baseline.Baseline, "_return_head",
                                          lambda bl, *a: (returning.append(1), real(bl, *a))[1])
                    p.start()
                    self.addCleanup(p.stop)
                    self.fc.on_get = lambda path, q: (self._attr("cnc/interlock_circuit", "37")
                                                      if path == "/mode" and returning == [1]
                                                      and not returning.append(2) else None)
                self.addCleanup(os.environ.pop, "GRBL_HOST", None)
                self.addCleanup(os.environ.pop, "GRBL_PORT", None)
                at_first_line = []
                dev.on_command = lambda line: (at_first_line.append(
                    bool(hw.sysfs_int("cnc/interlock_circuit") & baseline.LATCH_BIT)) if not at_first_line else None)
                left = [x.item for x in b.enforce("post", captured=cap)]
                self.assertEqual((at_first_line[:1], "laser_latch" in left), ([True], True))

    def test_a_window_seen_after_the_cooling_engines_read_is_named_and_ended(self):
        # the pump's read, the kernel side's read, and after it (with nothing
        # to write back, so only the pass's last read sees it; named before a
        # settings write-back that restarts nothing, and that last read
        # answered or not, or on a pass forgectrl did not answer at its start
        # and answered again by then): a window opening there is read again
        # past the tick, named cool.armed once and stood down once, at the
        # read that saw it (before the extensions and the preserved state) or
        # by the end of the pass
        clock = self.clocked()
        # reads: 1 the armed-window check, 2 the cooling engine's, 3 the
        # pump's, 4 the kernel side's; a re-read past the tick takes three
        for site, armed_reads in (("the pump's read", {3, 4, 5, 6}), ("the kernel side's read", None),
                                  ("after it, nothing to write back", None),
                                  ("after the kernel side's read", None), ("after it, the last read unanswered", None),
                                  ("after it, forgectrl silent at the start", None)):
            with self.subTest(site):
                self.lamp_at_idle()
                self.fc.posts.clear()
                self.restarts()
                reads, late = [], []
                silent = [1] if site.endswith("at the start") else []
                cap = None
                if site.startswith("after") and not site.endswith("nothing to write back"):
                    self.fc.state["settings"]["some_setting"] = "2"          # the run changed it
                    cap = {"settings": {"some_setting": "1"}, "mode": "grbl"}

                def on_get(path, q, armed_reads=armed_reads, site=site, silent=silent):
                    if path == "/mode" and silent:
                        silent.pop()                        # the pass's wait for forgectrl
                        return 503, {"error": "busy"}
                    if path == "/cool/status":
                        reads.append(path)
                        n = len(reads)
                        stopped = "/controller/stop" in [p for p, _ in self.fc.posts]
                        if late and site.endswith("unanswered"):
                            late.append(n)
                            if len(late) == 3:                  # the naming read is the first after it
                                return 503, {"error": "busy"}
                        armed = ((n in armed_reads) if armed_reads else (bool(late) if site.startswith("after")
                                                                         else n >= 4) and not stopped)
                        if armed:
                            return 200, {"phase": "run", "armed": True, "hold": False}
                    return None
                self.fc.on_get = on_get
                real = baseline.Baseline._ext_side
                stops = lambda: [p for p, _ in self.fc.posts].count("/controller/stop")      # noqa: E731
                at_ext = []
                # on the silent pass, forgectrl's silence has run out by then
                wait = baseline.FC_SILENCE_S + 1 if site.endswith("at the start") else 0
                with mock.patch.object(baseline.Baseline, "_ext_side",
                                       lambda bl, left: (at_ext.append(stops()), late.append(1), clock.sleep(wait),
                                                         real(bl, left))[3]):
                    left = self.bl().enforce("post", captured=cap)
                self.assertEqual([x.action.split(" ")[0] for x in left if x.item == "cool.armed"], ["restored"])
                self.assertEqual((at_ext, stops()), ([0 if site.startswith("after") else 1], 1))

    def test_a_window_open_when_the_pass_ends_a_controller_is_named_first(self):
        # a window that opens while the pass waits for a job to reach idle:
        # the stand-down that ends both reads it first, and the pass's last
        # read of the window says it ended
        self.clocked()
        self.lamp_at_idle()
        self.fc.state["status"]["state"] = "running"
        self.restarts(on_stop=lambda: self.fc.state["status"].__setitem__("state", "idle"))
        reads = []

        def on_get(path, q):
            if path == "/cool/status":
                reads.append(path)
                stopped = "/controller/stop" in [p for p, _ in self.fc.posts]
                if len(reads) > 1 and not stopped:
                    return 200, {"phase": "run", "armed": True, "hold": False}
            return None
        self.fc.on_get = on_get
        left = self.bl().enforce("post", captured=None)
        self.assertEqual([x.action.split(" ")[0] for x in left if x.item == "cool.armed"], ["ended"])

    def test_a_mode_switch_reads_the_latch_before_it(self):
        # before the switch, and before its probe of the Grbl port, whose
        # connection ends another sender's window (the controller relocks)
        self.clocked()
        self.lamp_at_idle()
        b = self.bl()
        self._attr("cnc/interlock_circuit", "37")               # unlocked after the pass
        b.switch_mode("cloud")
        self.assertEqual([x.item for x in b.switch_found], ["laser_latch"])
        b = self.bl()
        real = b.wait_settled
        b.wait_settled = lambda **kw: (real(**kw), self._attr("cnc/interlock_circuit", "37"))[0]
        with mock.patch.object(hw, "grbl_port_open", lambda timeout=5: helpers.supervisor_locks() or True):
            b.switch_mode("grbl")
        self.assertEqual([x.item for x in b.switch_found], ["laser_latch"])

    def test_a_safety_finding_the_runner_cannot_take_never_breaks_the_pass(self):
        def refuses(found):
            raise RuntimeError("no room for the record")
        self._attr("cnc/interlock_circuit", "37")
        left = baseline.Baseline(self.lines.append, on_safety=refuses).enforce("post", captured=None)
        self.assertIn("laser_latch", [x.item for x in left])

    def test_a_miss_while_waiting_for_a_hold_stands_the_machine_down_at_once(self):
        # the engine holds, and a read in the wait times out: a window could
        # open behind the silence, so the machine is stood down at the miss,
        # and the hold, read before it, is still judged the run's
        clock = self.clocked()
        self.lamp_at_idle()
        self.fc.state["cool"] = {"phase": "idle", "armed": False, "hold": True}
        stops = []
        self.restarts(on_stop=lambda: stops.append(clock.t))
        b = self.bl()
        real = b.fc().get
        missed = []
        t0 = clock.t

        def get(p, **kw):
            if p == "/cool/status" and clock.t >= t0 + 5 and not missed:
                missed.append(clock.t)
                raise hw.HwError("forgectrl GET /cool/status: timed out")
            return real(p, **kw)
        b.fc().get = get
        left = b.enforce("post", captured=None)
        self.assertTrue(stops and missed)
        self.assertLessEqual(stops[0] - missed[0], 1.0)
        self.assertEqual([x.judged() for x in left if x.item == "cool"], [True])

    def test_a_fresh_miss_after_a_stand_down_settled_stands_the_machine_down_again(self):
        # an armed window stood down, then the cooling engine's read answers
        # an error: asked after the stand-down settled, that read says nothing
        # of a window opening since, and the machine is stood down again
        self.clocked()
        for kind in ("an error status", "a timeout"):
            with self.subTest(kind):
                self.lamp_at_idle()
                self.fc.state["cool"] = {"phase": "run", "armed": True, "hold": False}
                stops, after = [], []
                self.restarts(on_stop=lambda stops=stops: stops.append(1))
                b = self.bl()
                real = b.fc().get

                def get(p, kind=kind, stops=stops, after=after, **kw):
                    if p == "/cool/status" and stops:
                        after.append(p)
                        if len(after) == 2:         # the first is the stand-down's own; this is the engine's
                            if kind == "a timeout":
                                raise hw.HwError("forgectrl GET /cool/status: timed out")
                            return 503, {"error": "busy"}
                    return real(p, **kw)
                b.fc().get = get
                b.enforce("post", captured=None)
                self.assertEqual(len(stops), 2)

    def test_the_stand_downs_own_misses_stand_nothing_down_again(self):
        # the stand-down's own /mode read times out, which silences forgectrl:
        # the reads the silence turns away after it settled are no fresh miss
        self.clocked()
        self.lamp_at_idle()
        self.fc.state["cool"] = {"phase": "run", "armed": True, "hold": False}
        stops = []
        self.restarts(on_stop=lambda: stops.append(1))
        b = self.bl()
        real = b.fc().get
        missed = []

        def get(p, **kw):
            if p == "/mode" and stops and not missed:
                missed.append(p)
                raise hw.HwError("forgectrl GET /mode: timed out")
            return real(p, **kw)
        b.fc().get = get
        b.enforce("post", captured=None)
        self.assertEqual((len(stops), missed), (1, ["/mode"]))

    def test_a_supervisor_unanswered_past_a_settle_is_judged_on_its_next_answer(self):
        # forgectrl busy for 11 s after the stand-down's start: the settle
        # gives up, the one read past the silence too, and the supervisor is
        # read again before it is judged: running, and the client is started
        # once; standby (the stand-down's start never landed), and it is
        # started again
        clock = self.clocked()
        marker = os.path.join(self.tmp, "nohunt-marker")
        p = mock.patch.object(baseline, "NOHUNT_MARKER", marker)
        p.start()
        self.addCleanup(p.stop)
        for found, n in (("running", 1), ("standby", 2)):
            with self.subTest(found):
                self.lamp_at_idle()
                self.cloud()
                self.fc.state["mode"]["controller"] = "standby"
                starts = []

                def on_post(path, form, found=found, starts=starts):
                    if path == "/controller/start":
                        starts.append(clock.t)
                        if found == "standby" and len(starts) == 1:
                            return None                     # the stand-down's start never lands
                        if self.fc.state["mode"]["controller"] != "running":
                            if os.path.exists(marker):
                                os.remove(marker)
                            self.fc.state["mode"] = dict(self.fc.state["mode"], controller="running")
                    elif path == "/controller/stop":
                        self.fc.state["mode"] = dict(self.fc.state["mode"], controller="standby")
                    return None
                self.fc.on_post = on_post
                missed = []
                self.fc.on_get = lambda path, q, missed=missed: ((missed.append(path) or (503, {"error": "busy"}))
                                                                 if path == "/cool/status" and not missed else None)
                b = self.bl()
                real = b.fc().get

                def get(path, starts=starts, real=real, **kw):
                    if path == "/mode" and starts and clock.t < starts[0] + 11:
                        raise hw.HwError("forgectrl GET /mode: timed out")
                    return real(path, **kw)
                b.fc().get = get
                b.enforce("post", captured=None)
                self.assertEqual((len(starts), os.path.exists(marker)), (n, False))

    def test_a_settle_cut_short_reads_the_mode_again_before_any_start(self):
        # an Abort ends the stand-down's settle at once, after a read that
        # timed out silenced forgectrl: the supervisor branch judges the mode
        # read after it, past the silence, so the client is started once and
        # the no-hunt marker goes with that start
        self.clocked()
        self.lamp_at_idle()
        marker = os.path.join(self.tmp, "nohunt-marker")
        p = mock.patch.object(baseline, "NOHUNT_MARKER", marker)
        p.start()
        self.addCleanup(p.stop)
        self.cloud()
        self.fc.state["mode"]["controller"] = "standby"
        starts = []

        def on_post(path, form):
            if path == "/controller/start":
                starts.append(path)
                if self.fc.state["mode"]["controller"] != "running":
                    if os.path.exists(marker):
                        os.remove(marker)
                    self.fc.state["mode"] = dict(self.fc.state["mode"], controller="running")
            return None
        self.fc.on_post = on_post
        b = baseline.Baseline(self.lines.append, abort=lambda: True)
        real = b.fc().get
        missed = []

        def get(path, **kw):
            if path == "/cool/status" and not missed:
                missed.append(path)
                raise hw.HwError("forgectrl GET /cool/status: timed out")
            return real(path, **kw)
        b.fc().get = get
        left = [x.item for x in b.enforce("post", captured=None)]
        self.assertEqual((len(starts), os.path.exists(marker), "controller" in left), (1, False, False))

    def test_a_window_that_arms_during_a_hold_is_stopped_within_a_tick(self):
        # the engine holds at the pass, and a client opens its session in the
        # wait for the hold to clear: it is a job, stood down at once
        clock = self.clocked()
        self.lamp_at_idle()
        arm_at = clock.t + 10
        stops, shown = [], []

        def on_get(path, q):
            # the window shows at one read of the wait: it is named from that
            # read, before the stand-down that ends it reads anything
            if path == "/cool/status":
                if stops or shown:
                    return 200, {"phase": "idle", "armed": False, "hold": False}
                if clock.t < arm_at:
                    return 200, {"phase": "idle", "armed": False, "hold": True}
                shown.append(clock.t)
                return 200, {"phase": "run", "armed": True, "hold": False}
            return None
        self.fc.on_get = on_get
        self.restarts(on_stop=lambda: stops.append(clock.t))
        left = [x.item for x in self.bl().enforce("post", captured=None)]
        self.assertTrue(stops)
        self.assertLessEqual(stops[0] - arm_at, baseline.COOL_PUBLISH_S)
        self.assertIn("cool.armed", left)

    def test_after_a_stand_down_the_supervisor_is_judged_where_it_settled(self):
        # cloud mode with the controller in standby (a failing print's stop),
        # and a stand-down: the client is started once, and the no-hunt
        # marker that start was made under is gone with it
        self.clocked()
        self.lamp_at_idle()
        marker = os.path.join(self.tmp, "nohunt-marker")
        p = mock.patch.object(baseline, "NOHUNT_MARKER", marker)
        p.start()
        self.addCleanup(p.stop)
        self.cloud()
        self.fc.state["mode"]["controller"] = "standby"
        starts = []

        def on_post(path, form):
            if path == "/controller/start":
                starts.append(path)
                if self.fc.state["mode"]["controller"] != "running":     # a client spawns, takes the marker
                    if os.path.exists(marker):
                        os.remove(marker)
                    self.fc.state["mode"] = dict(self.fc.state["mode"], controller="running")
            return None
        self.fc.on_post = on_post
        missed = []
        self.fc.on_get = lambda path, q: ((missed.append(path) or (503, {"error": "busy"}))
                                          if path == "/cool/status" and not missed else None)
        self.bl().enforce("post", captured=None)
        self.assertEqual((len(starts), os.path.exists(marker)), (1, False))

    def test_a_stand_down_whose_start_is_refused_is_named(self):
        # a job still moving, and the start after the stand-down refused (a
        # lease): the supervisor is judged in the standby it was left in
        self.clocked()
        self.lamp_at_idle()
        self.fc.state["status"]["state"] = "running"

        def on_post(path, form):
            if path == "/controller/stop":
                self.fc.state["mode"] = dict(self.fc.state["mode"], controller="standby")
                self.fc.state["status"]["state"] = "idle"
            elif path == "/controller/start":
                return 409, {"error": "leased"}
            return None
        self.fc.on_post = on_post
        left = {x.item for x in self.bl().enforce("post", captured=None)}
        self.assertIn("controller", left)

    def test_no_controller_value_is_written_under_a_program_still_playing(self):
        self.clocked()
        self.lamp_at_idle()
        self._attr("cnc/state", "running")
        self._attr("cnc/motor_lock", "15")
        self.bl().enforce("post", captured=None)
        self.assertEqual(self._read("cnc/motor_lock"), "15")

    def test_an_armed_window_is_stopped_with_every_later_mode_read_unanswered(self):
        self.clocked()
        self.lamp_at_idle()
        self.fc.state["cool"] = {"phase": "run", "armed": True, "hold": False}
        self.restarts()
        b = self.bl()
        real = b.fc().get
        reads = []

        def get(p, **kw):
            if p == "/mode":
                reads.append(p)
                if len(reads) > 1:
                    raise hw.HwError("forgectrl GET /mode: timed out")
            return real(p, **kw)
        b.fc().get = get
        b.enforce("post", captured=None)
        self.assertIn("/controller/stop", [p for p, _ in self.fc.posts])

    def test_a_job_moving_after_the_supervisor_settles_is_stood_down(self):
        clock = self.clocked()
        self.lamp_at_idle()
        up = clock.t + 10

        def on_get(path, q):
            if path == "/mode":
                starting = clock.t < up
                return 200, dict(self.fc.state["mode"], controller="stopped" if starting else "running",
                                 motion="unverified" if starting else "verified")
            return None
        self.fc.on_get = on_get
        self.fc.state["status"]["state"] = "running"
        self.restarts(on_stop=lambda: self.fc.state["status"].__setitem__("state", "idle"))
        self.bl().enforce("post", captured=None)
        self.assertIn("/controller/stop", [p for p, _ in self.fc.posts])

    def test_a_window_that_arms_after_the_first_read_is_stopped_within_a_tick(self):
        # a print still loading when the run ended arms after the pass's
        # first look: it is a job, and waiting does not end it
        clock = self.clocked()
        self.lamp_at_idle()
        reads = []

        def on_get(path, q):
            # 1 the armed-window check; 2 the cooling engine's read and 3-5
            # its re-reads past the tick show the window; it is named from
            # those, before the stand-down that ends it reads anything
            if path == "/cool/status":
                reads.append(clock.t)
                armed = 2 <= len(reads) <= 5 and self.fc.state["cool"].get("phase") != "idle"
                return 200, {"phase": "run" if armed else "idle", "armed": armed, "hold": False}
            return None
        self.fc.state["cool"] = {"phase": "run"}
        self.fc.on_get = on_get
        stops = []
        self.restarts(on_stop=lambda: stops.append(clock.t))
        t0 = clock.t
        left = [x.item for x in self.bl().enforce("post", captured=None)]
        self.assertTrue(stops)
        self.assertLessEqual(stops[0] - t0, baseline.COOL_PUBLISH_S + 1.0)
        self.assertIn("cool.armed", left)

    def test_an_unanswered_reread_of_an_armed_window_is_no_disarm(self):
        self.clocked()
        self.lamp_at_idle()
        self.fc.state["cool"] = {"phase": "run", "armed": True, "hold": False}
        self.restarts()
        b = self.bl()
        real = b.fc().get
        reads = []

        def get(path, **kw):
            if path == "/cool/status":
                reads.append(path)
                if len(reads) == 2:
                    raise hw.HwError("forgectrl GET /cool/status: timed out")
            return real(path, **kw)
        b.fc().get = get
        left = {x.item for x in b.enforce("post", captured=None)}
        self.assertIn("/controller/stop", [p for p, _ in self.fc.posts])
        self.assertIn("cool.armed", left)               # the window is named, not taken for closed

    def test_a_stand_down_in_cloud_mode_brings_the_client_back_without_the_hunt(self):
        self.clocked()
        for refuse in (False, True):
            with self.subTest(refused=refuse):
                seen = self.nohunt_marker(refuse=refuse)
                self.cloud()
                self.fc.state["status"]["state"] = "running"         # a print still playing at the post pass
                self.bl().enforce("post", captured=None)
                self.assertIn(("/controller/start", {}, True), seen)
                # a refused start takes its marker down: the next start is not the baseline's
                self.assertFalse(os.path.exists(baseline.NOHUNT_MARKER))

    def test_a_hold_in_the_idle_phase_is_a_leftover(self):
        self.clocked()
        self.lamp_at_idle()
        self.fc.state["cool"] = {"phase": "idle", "armed": False, "hold": True}
        self.restarts()
        left = {x.item: x for x in self.bl().enforce("post", captured=None)}
        self.assertEqual(left["cool"].found, "idle/armed=False/hold=True")

    def test_a_hold_that_clears_in_the_cooldown_wait_is_still_the_runs(self):
        clock = self.clocked()
        self.lamp_at_idle()
        clears = clock.t + 10                 # past the engine's publish tick, inside the cooldown wait
        self.fc.on_get = lambda path, q: ((200, {"phase": "idle", "armed": False, "hold": clock.t < clears})
                                          if path == "/cool/status" else None)
        left = {x.item: x for x in self.bl().enforce("post", captured=None)}
        self.assertEqual(left["cool"].action, "waited")

    def test_an_underrun_the_hand_back_acknowledged_is_a_leftover(self):
        self.clocked()
        self.lamp_at_idle()
        acked = lambda: os.path.exists(self.sysfs + "cnc/stop")      # noqa: E731
        self.fc.on_get = lambda path, q: ((200, dict(self.fc.state["status"],
                                                     state="idle" if acked() else "underrun"))
                                          if path == "/status" else None)
        left = {x.item: x for x in self.bl().enforce("post", captured=None)}
        self.assertEqual((left["state"].found, left["state"].action), ("underrun", "waited"))
        self.assertEqual(self._read("cnc/stop"), "1")

    def test_forgectrls_reading_of_an_unlocked_latch_relocks_it(self):
        # the only latch check when the kernel's word cannot be read, and the
        # first to see a controller that unlocks after the pass's own lock
        self.lamp_at_idle()
        self.fc.state["status"]["laser_locked"] = False
        unlocked, at_the_waits = [], []

        def on_get(path, q):
            if path == "/status" and not unlocked:
                unlocked.append(1)
                self._attr("cnc/laser_latch", "0")              # the controller unlocks after the pass's lock
            elif path == "/diag/status":
                at_the_waits.append(self._read("cnc/laser_latch"))
            return None
        self.fc.on_get = on_get
        left = {x.item: x for x in self.bl().enforce("post", captured=None)}
        self.assertIn("laser_locked", left)
        self.assertEqual(at_the_waits[:1], ["1"])               # locked again before the waits, not at the end

    def test_a_diagnostic_left_running_is_aborted_and_named(self):
        self.clocked()
        self.lamp_at_idle()
        self.fc.state["diag"] = {"running": True, "tool": "fans"}
        self.restarts()
        left = {x.item: x for x in self.bl().enforce("post", captured=None)}
        self.assertEqual((left["diag"].found, left["diag"].action), ("fans", "aborted"))

    def test_a_camera_engine_still_running_is_a_leftover(self):
        self.clocked()
        self.lamp_at_idle()
        self.fc.state["cam"] = {"running": True, "clients": 1}
        left = {x.item: x for x in self.bl().enforce("post", captured=None)}
        self.assertTrue(left["cam.running"].action.startswith("failed"))

    def test_a_setting_the_run_changed_is_written_back_and_named(self):
        self.lamp_at_idle()
        self.fc.state["settings"]["cool_max_c"] = "30"
        b = self.bl()
        cap = b.capture()
        self.fc.state["settings"]["cool_max_c"] = "40"
        left = {x.item: x for x in b.enforce("post", captured=cap)}
        self.assertEqual((left["settings.cool_max_c"].found, left["settings.cool_max_c"].action), ("40", "restored"))
        self.assertIn(("/settings", {"cool_max_c": "30"}), self.fc.posts)

    def test_a_pump_left_off_at_idle_is_switched_on(self):
        # Nothing re-asserts the pump per tick, and no fire gate reads it.
        self.lamp_at_idle()
        self._attr("thermal/water_pump_on", "0")
        left = {x.item: x for x in self.bl().enforce("post", captured=None)}
        self.assertEqual(left["thermal/water_pump_on"].action, "restored")
        self.assertEqual(self._read("thermal/water_pump_on"), "1")

    def test_a_quiet_hold_left_standing_is_released(self):
        self.lamp_at_idle()
        self.fc.state["cool"] = {"phase": "idle", "armed": False, "hold": False, "quiet_hold": True}

        def on_post(path, form):
            if path == "/cool/quiet" and form.get("on") == "0":
                self.fc.state["cool"] = dict(self.fc.state["cool"], quiet_hold=False)
            return None
        self.fc.on_post = on_post
        left = {x.item: x for x in self.bl().enforce("post", captured=None)}
        self.assertTrue(left["cool.quiet_hold"].action.startswith("restored"), left["cool.quiet_hold"])

    def test_the_lamp_holds_only_on_two_reads_in_a_row(self):
        # a fade passing through the level once is not the level held
        reads = iter(["240", "200"] * 5)
        with mock.patch.object(hw, "sysfs_read", lambda attr, default=None: next(reads)):
            self.assertFalse(self.bl()._lamp_holds("240")[0])

    def test_the_controllers_settings_are_handed_back_by_their_kind(self):
        # $12 is put back through the port; a laser setting is named and not
        # written; $100, derived from xy_microsteps, is not judged here.
        # $63 is pinned: named, not written
        dev, view, b, cap = self.grbl_view({"$12": "0.002", "$30": "1000", "$32": "1", "$63": "3",
                                            "$100": "213.333", "$110": "12000"})
        self.assertEqual(cap["grbl"], view)
        view.update({"$12": "0.01", "$30": "500", "$32": "0", "$63": "1", "$100": "53.333", "$110": "6000"})
        left = {x.item: x for x in b.enforce("post", captured=cap)}
        self.assertEqual(sorted(k for k in left if k.startswith("grbl.")),
                         ["grbl.$110", "grbl.$12", "grbl.$30", "grbl.$32", "grbl.$63"])
        self.assertEqual(sorted(ln for ln in dev.sent if "=" in ln), ["$110=12000", "$12=0.002"])

    def grbl_view(self, view):
        """A GRBL controller whose $$ view forgectrl serves from `view`, its
        port a fake, and the capture taken."""
        dev = helpers.FakeGrbl().start()
        self.addCleanup(dev.stop)
        self.lamp_at_idle()
        self.fc.on_get = lambda path, q: ((200, "".join("%s=%s\r\n" % kv for kv in view.items()).encode(),
                                           "text/plain") if path == "/grbl/settings" else None)
        b = self.bl()
        return dev, view, b, b.capture()

    def test_a_setting_the_port_does_not_keep_is_a_failed_restore(self):
        dev, view, b, cap = self.grbl_view({"$12": "0.002"})
        dev.pinned = {"$12": "0.01"}                        # the write answers ok and does not stick
        view["$12"] = "0.01"
        left = {x.item: x for x in b.enforce("post", captured=cap)}
        self.assertTrue(left["grbl.$12"].action.startswith("failed"), left["grbl.$12"])

    def test_the_settings_are_compared_only_while_the_grbl_controller_runs(self):
        dev, view, b, cap = self.grbl_view({"$12": "0.002"})
        view["$12"] = "0.01"
        self.cloud()                                        # a cloud test the run declared
        cap["mode"] = "cloud"
        left = {x.item for x in b.enforce("post", captured=cap)}
        self.assertNotIn("grbl.$12", left)
        self.assertEqual([ln for ln in dev.sent if "=" in ln], [])

    def test_the_pump_is_the_engines_outside_its_idle_posture(self):
        # a cooldown runs it as the engine chooses, and a diagnostic as the
        # tool does: the hand-back leaves it to them
        self.clocked()
        self.lamp_at_idle()
        for cool, diag in (({"phase": "cooldown", "armed": False, "hold": False}, {"running": False}),
                           ({"phase": "idle", "armed": False, "hold": False}, {"running": True, "tool": "pump"})):
            with self.subTest(cool=cool["phase"], diag=diag["running"]):
                self._attr("thermal/water_pump_on", "0")
                self.fc.state["cool"], self.fc.state["diag"] = cool, diag
                self.fc.on_post = lambda path, form: (200, {"ok": True})     # the diagnostic does not stop
                self.bl().enforce("post", captured=None)
                self.assertEqual(self._read("thermal/water_pump_on"), "0")

    def test_a_pump_write_that_does_not_take_is_a_failed_restore(self):
        self.lamp_at_idle()
        self._attr("thermal/water_pump_on", "0")
        real = hw.sysfs_write
        with mock.patch.object(hw, "sysfs_write",
                               lambda attr, value: None if attr == baseline.PUMP_ATTR else real(attr, value)):
            left = {x.item: x for x in self.bl().enforce("post", captured=None)}
        self.assertTrue(left[baseline.PUMP_ATTR].action.startswith("failed"), left[baseline.PUMP_ATTR])

    def nohunt_marker(self, refuse=False):
        """The no-hunt marker under the fake tree; what each POST found.
        The fake client takes the marker down as gfcloud does, first thing
        at its start; with refuse the POST is refused and no client starts."""
        baseline.NOHUNT_MARKER = os.path.join(self.tmp, "nohunt-marker")
        self.fc.state["settings"].setdefault("cloud_enabled", "1")
        seen = []

        def on_post(path, form):
            seen.append((path, dict(form), os.path.exists(baseline.NOHUNT_MARKER)))
            if refuse:
                return 409, {"error": "busy"}
            if os.path.exists(baseline.NOHUNT_MARKER):
                os.remove(baseline.NOHUNT_MARKER)
            if path == "/mode":
                self.fc.state["mode"] = dict(self.fc.state["mode"], mode=form["controller"], controller="running")
            elif path == "/controller/start":
                self.fc.state["mode"] = dict(self.fc.state["mode"], controller="running")
            return None
        self.fc.on_post = on_post
        return seen

    def test_a_refused_switch_leaves_no_marker_for_the_next_start(self):
        seen = self.nohunt_marker(refuse=True)
        ok, detail = self.bl().switch_mode("cloud")
        self.assertFalse(ok)
        self.assertEqual(seen, [("/mode", {"controller": "cloud"}, True)])
        self.assertFalse(os.path.exists(baseline.NOHUNT_MARKER))

    def test_a_switch_to_cloud_starts_the_client_without_the_hunt(self):
        seen = self.nohunt_marker()
        ok, detail = self.bl().switch_mode("cloud")
        self.assertTrue(ok, detail)
        self.assertEqual(seen, [("/mode", {"controller": "cloud"}, True)])
        self.assertFalse(os.path.exists(baseline.NOHUNT_MARKER))      # one start, never left behind

    def test_a_switch_to_cloud_turns_cloud_mode_on_when_it_is_off(self):
        # A test that declares cloud mode gets it: cloud_enabled goes to 1
        # first, with the typed phrase the daemon asks for and a log line,
        # and only then the mode switch.
        lines = []
        seen = self.nohunt_marker()
        self.fc.state["settings"]["cloud_enabled"] = "0"
        ok, detail = baseline.Baseline(lines.append).switch_mode("cloud")
        self.assertTrue(ok, detail)
        self.assertEqual(seen[0], ("/settings", {"cloud_enabled": "1", "phrase": "I UNDERSTAND"}, False))
        self.assertEqual(seen[1], ("/mode", {"controller": "cloud"}, True))
        self.assertTrue(any("cloud mode turned on for the test" in ln for ln in lines))

    def test_a_switch_to_grbl_sets_no_marker(self):
        seen = self.nohunt_marker()
        self.cloud()
        with mock.patch.object(baseline, "GRBL_PORT_S", 0):
            ok, detail = self.bl().switch_mode("grbl")
        self.assertFalse(ok)              # the fake has no Grbl port: the switch itself happened
        self.assertEqual(seen, [("/mode", {"controller": "grbl"}, False)])
        self.assertFalse(os.path.exists(baseline.NOHUNT_MARKER))

    def test_the_mode_handed_back_is_cloud_without_the_hunt(self):
        seen = self.nohunt_marker()
        self.cloud()
        b = self.bl()
        cap = b.capture()                             # found in cloud
        self.fc.state["mode"] = dict(self.fc.state["mode"], mode="grbl")     # the run left it in grbl
        b.enforce("post", captured=cap)
        self.assertEqual(seen[0], ("/mode", {"controller": "cloud"}, True))
        self.assertFalse(os.path.exists(baseline.NOHUNT_MARKER))

    def test_a_standby_cloud_client_is_started_without_the_hunt(self):
        seen = self.nohunt_marker()
        self.cloud()
        b = self.bl()
        cap = b.capture()
        self.fc.state["mode"] = dict(self.fc.state["mode"], controller="standby")
        b.enforce("post", captured=cap)
        self.assertEqual(seen[0], ("/controller/start", {}, True))
        self.assertFalse(os.path.exists(baseline.NOHUNT_MARKER))

    def test_grbl_mode_restores_the_controller_values_and_the_lamp(self):
        self._attr("cnc/step_freq", "10000")
        self._attr("pic/lid_led", "77")
        left = self.bl().enforce("pre", captured=None)
        self.assertEqual(sorted(x.item for x in left), ["cnc/step_freq", "pic/lid_led"])
        self.assertEqual(self._read("cnc/step_freq"), DEFAULT_TICK)
        self.assertEqual(self._read("pic/lid_led"), "240")

    def test_cloud_mode_leaves_the_clients_config_lamp_and_counters(self):
        self.cloud()
        self._attr("cnc/step_freq", "10000")          # the cloud client's tick
        self._attr("pic/x_step_current", "135")
        self._attr("pic/lid_led", "77")               # its lid-image level
        b = self.bl()
        cap = b.capture()
        self.assertEqual(cap["mode"], "cloud")
        self.assertNotIn("controller_mode", cap["settings"])
        self._pos(-13096, -7400, 0)                   # the service re-zeroed and moved
        self._attr("cnc/streaming", "1")              # NOT the client's: still restored
        left = b.enforce("post", captured=cap)
        self.assertEqual([x.item for x in left], ["cnc/streaming"])
        self.assertEqual(self._read("cnc/step_freq"), "10000")
        self.assertEqual(self._read("pic/x_step_current"), "135")
        self.assertEqual(self._read("pic/lid_led"), "77")
        self.assertEqual(self._read("cnc/streaming"), "0")
        self.assertEqual(self.fc.posts, [])           # no mode switch, no settings write

    def test_cloud_mode_still_relocks_the_latch(self):
        self.cloud()
        self._attr("cnc/interlock_circuit", "37")     # bit 3 clear: unlocked
        left = self.bl().enforce("post", captured=None)
        self.assertEqual([x.item for x in left], ["laser_latch"])
        self.assertEqual(self._read("cnc/laser_latch"), "1")

    def test_undeclared_mode_change_is_handed_back_through_the_switch(self):
        b = self.bl()
        cap = b.capture()                             # found in grbl
        self.assertEqual(cap["mode"], "grbl")
        self.cloud()                                  # the run left it in cloud, silently
        left = b.enforce("post", captured=cap)
        items = {x.item: x for x in left}
        self.assertIn("mode", items)
        self.assertEqual(items["mode"].action, "restored")
        self.assertEqual(self.fc.posts, [("/mode", {"controller": "grbl"})])
        self.assertEqual(self.fc.state["mode"]["mode"], "grbl")

    def test_declared_mode_change_is_kept(self):
        b = self.bl()
        cap = b.capture()
        self.cloud()
        cap["mode"] = "cloud"                         # Context.mode_changed("cloud")
        left = b.enforce("post", captured=cap)
        self.assertNotIn("mode", [x.item for x in left])
        self.assertEqual(self.fc.posts, [])
        self.assertEqual(self.fc.state["mode"]["mode"], "cloud")

    def test_controller_mode_setting_is_never_written_back_bare(self):
        b = self.bl()
        cap = b.capture()
        self.assertNotIn("controller_mode", cap["settings"])
        self.fc.state["settings"]["controller_mode"] = "cloud"     # a switch persisted it
        self.fc.state["mode"]["mode"] = "cloud"
        cap["mode"] = "cloud"
        b.enforce("post", captured=cap)
        self.assertEqual([p for p, _ in self.fc.posts if p == "/settings"], [])


if __name__ == "__main__":
    unittest.main()
