# Copyright 2026 514 LLC d/b/a OpenGlow
# Written by Scott Wiederhold
# https://community.openglow.org
# SPDX-License-Identifier:    MIT

"""The baseline: fixed resting values are restored, preserved values are
handed back, every deviation is a recorded leftover. Runs against a fake
sysfs tree; forgectrl is unreachable (service-side checks skip)."""
import json
import os
import shutil
import struct
import tempfile
import unittest

from forgetest import baseline

# The fixed machine tick at the mode an unset xy_microsteps reads as.
# Taken from baseline rather than typed, so a change to the default mode
# moves the expectations with it.
DEFAULT_TICK = dict(baseline.fixed_sysfs())["cnc/step_freq"]


class BaselineTests(unittest.TestCase):
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
        baseline.Baseline._unreachable_until = 0.0
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

    def _led2(self, name, target, brightness):
        """Both attributes of one button LED: the trigger's commanded
        level and the fade that follows it."""
        for a, v in (("target", target), ("brightness", brightness)):
            with open(self.leds + name + "/" + a, "w") as f:
                f.write(v)

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

    def _pos_bytes(self, x, y, z, processed, total):
        with open(self.sysfs + "cnc/position", "wb") as f:
            f.write(struct.pack("<3i2I", x, y, z, processed, total))

    def test_ring_residue_is_a_leftover_and_blocks_the_return_jog(self):
        b = self.bl()
        cap = b.capture()
        # the run left 40 unplayed bytes queued in the kernel ring and the
        # head 1000 counts out: the hand-back stands the machine down to
        # empty the ring, and only refuses the return jog when the bytes
        # are still there afterwards (they are here: no daemon to restart
        # a controller)
        self._pos_bytes(1000, 0, 0, 100, 140)
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

    def _probe_state(self):
        # what the kernel shows between the supervisor's motion probe and
        # the GRBL controller's init writes
        self._attr("cnc/motor_lock", "0")
        self._attr("cnc/step_freq", "10000")
        self._attr("cnc/y_mode", "1")

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
        os.environ["FORGETEST_BOOT_ID"] = "test-boot-2"
        try:
            path = os.path.join(self.tmp, "boot-test-boot-2.json")
            with open(path, "w") as f:
                json.dump({"ts": "old", "sysfs": {"cnc/motor_lock": "0", "cnc/step_freq": "10000",
                                                     "cnc/y_mode": "1"}}, f)
            ref = baseline.boot_reference(self.lines.append, self.tmp)
            up = baseline.uptime_s()
            if up is None or up > baseline.BOOT_MAX_AGE_S:
                # too old to retake: the stale reference stands, marked
                self.assertTrue(any("predates the controller's config" in l for l in self.lines))
                self.assertEqual(ref["ts"], "old")
            else:
                self.assertTrue(any("retaking" in l for l in self.lines))
                self.assertEqual(ref["sysfs"]["cnc/motor_lock"], "0")
        finally:
            os.environ.pop("FORGETEST_BOOT_ID", None)


if __name__ == "__main__":
    unittest.main()


class TransientNotLeftoverTests(BaselineTests):
    """A leftover is what a run left behind, not the machine part-way
    through its own work. Found on the bench reference, where
    laser.arm-wait-lid passed every check it makes and failed its
    hand-back on the post-job smoke clear."""

    def test_a_fading_button_led_is_not_a_leftover(self):
        # the trigger fades brightness toward target: target 0 with
        # brightness still high is the fade from a level the machine ended
        for name in baseline.BUTTON_LEDS:
            self._led2(name, target="0", brightness="900")
        left = self.bl().enforce("post", captured=None)
        self.assertEqual([x for x in left if x.item.startswith("leds/")], [])

    def test_a_lit_button_led_is_a_leftover(self):
        # a target the run left standing is dirt, whatever the fade reads
        self._led2(baseline.BUTTON_LEDS[0], target="1014", brightness="0")
        left = self.bl().enforce("post", captured=None)
        items = [x for x in left if x.item.startswith("leds/")]
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0].found, "1014")


class CoolPublishTests(unittest.TestCase):
    """The cooling engine publishes once a tick. A status read inside the
    tick after a run ended is the engine's last word on the run, not the
    machine as the run left it. Found on the bench reference: a diagnostic
    that finished clean (cooling.aa-offset-calibrate) and a fail tier that
    did its work (cooling.fail-tier-stop) both failed their hand-back on a
    hold the engine's next tick cleared."""

    IDLE = {"phase": "idle", "armed": False, "hold": False}

    def setUp(self):
        self.lines = []

    def bl(self, statuses):
        """A baseline whose /cool/status answers play in order (the last
        one repeats) and whose waits count reads, not seconds: three for
        the engine's next tick, as the real wait makes, and enough for the
        cooldown to let a slow script run out."""
        b = baseline.Baseline(self.lines.append)
        seq = list(statuses)
        b.fc_get = lambda path: (200, dict(seq.pop(0) if len(seq) > 1 else seq[0]))

        def wait(what, pred, timeout):
            for i in range(3 if what == "cool publish" else 50):
                if pred():
                    return float(i)
            return None
        b._wait = wait
        b.stood_down = []
        b.stand_down = b.stood_down.append
        return b

    def test_a_diagnostic_hold_the_next_tick_clears_is_not_a_leftover(self):
        left = []
        b = self.bl([{"phase": "diag", "armed": False, "hold": True}, self.IDLE])
        b._cool_side(left)
        self.assertEqual(left, [])
        self.assertTrue(any("last word on the run" in ln for ln in self.lines), self.lines)

    def test_a_fail_tier_hold_clears_into_the_engines_own_post_job_phase(self):
        left = []
        b = self.bl([{"phase": "run", "armed": False, "hold": True},
                     {"phase": "smoke", "armed": False, "hold": False},
                     {"phase": "smoke", "armed": False, "hold": False}, self.IDLE])
        b._cool_side(left)
        self.assertEqual(left, [])
        self.assertTrue(any("not a leftover" in ln for ln in self.lines), self.lines)

    def test_a_hold_that_outlives_the_tick_is_the_runs_doing(self):
        left = []
        b = self.bl([{"phase": "run", "armed": True, "hold": True}])
        b._cool_side(left)
        self.assertEqual([x.item for x in left], ["cool"])
        self.assertEqual(left[0].found, "run/armed=True/hold=True")
        self.assertTrue(left[0].action.startswith("failed: still"), left[0].action)
        self.assertEqual(len(b.stood_down), 1)

    def test_a_hold_that_clears_only_later_is_still_recorded(self):
        left = []
        held = {"phase": "run", "armed": False, "hold": True}
        b = self.bl([held] * 10 + [self.IDLE])
        b._cool_side(left)
        self.assertEqual([x.item for x in left], ["cool"])
        self.assertEqual(left[0].action, "waited")
        self.assertEqual(b.stood_down, [])

    def test_an_idle_engine_and_a_silent_daemon_leave_nothing(self):
        left = []
        self.bl([self.IDLE])._cool_side(left)
        b = baseline.Baseline(self.lines.append)
        b.fc_get = lambda path: (None, None)
        b._cool_side(left)
        self.assertEqual(left, [])


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
    small is quantization, not a leftover. Found on the bench reference,
    where motion.lid-cancel-home returned the head twice, each within
    0.04 mm, and failed the hand-back on the four steps left over."""

    def test_a_few_steps_of_xy_are_the_quantization(self):
        self.assertTrue(baseline.position_quantized([0, 0, 0], [4, 0, 0]))
        self.assertTrue(baseline.position_quantized([0, 0, 0], [-4, 4, 0]))
        self.assertTrue(baseline.position_quantized([10, 20, 30], [10, 20, 30]))

    def test_past_the_dead_band_is_a_leftover(self):
        over = int(baseline.POSITION_DEADBAND_MM * baseline.XY_STEPS_PER_MM) + 2
        self.assertFalse(baseline.position_quantized([0, 0, 0], [over, 0, 0]))
        self.assertFalse(baseline.position_quantized([0, 0, 0], [0, over, 0]))

    def test_the_dead_band_scales_with_the_counters(self):
        # the same step count is a quarter of the distance at x32
        over = int(baseline.POSITION_DEADBAND_MM * baseline.XY_STEPS_PER_MM) + 2
        x32 = baseline.XY_STEPS_PER_MM_OF[32]
        self.assertTrue(baseline.position_quantized([0, 0, 0], [over, 0, 0], x32))
        self.assertFalse(baseline.position_quantized([0, 0, 0], [4 * over, 0, 0], x32))

    def test_z_is_exact_because_the_return_never_moves_it(self):
        self.assertFalse(baseline.position_quantized([0, 0, 0], [0, 0, 1]))

    def test_an_unreadable_reading_is_never_quantization(self):
        self.assertFalse(baseline.position_quantized(None, [0, 0, 0]))
        self.assertFalse(baseline.position_quantized([0, 0, 0], None))


class BaselineModeTests(BaselineTests):
    """The baseline against a fake forgectrl: what the mode in force
    owns. Reuses the fake sysfs tree of BaselineTests; only the new
    tests run here (the inherited ones are skipped)."""

    def setUp(self):
        super().setUp()
        import helpers
        self.fc = helpers.FakeForgectrl().start()
        baseline.Baseline._unreachable_until = 0.0

    def tearDown(self):
        self.fc.stop()
        super().tearDown()

    def run(self, result=None):
        # only this class's own tests, not the base class's
        if self._testMethodName not in BaselineModeTests.__dict__:
            return
        return super().run(result)

    def cloud(self):
        self.fc.state["mode"] = {"mode": "cloud", "controller": "running", "pid": 7, "motion": "verified"}
        self.fc.state["settings"]["controller_mode"] = "cloud"

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
        self.assertEqual(self._read("pic/lid_led"), "236")

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
