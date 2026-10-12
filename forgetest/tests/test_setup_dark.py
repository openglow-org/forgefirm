# Copyright 2026 514 LLC d/b/a OpenGlow
# Written by Scott Wiederhold
# https://community.openglow.org
# SPDX-License-Identifier:    MIT

"""The setup's checks in the catalog: registered as automatic tests, the
takeover ones marked so, every covers map naming the check runner, and
the two that put the intake fan count back as found refused unless it is
set; and the check driver itself against the fake daemon: a start, the
run polled to its end with no question asked, the result taken, the
record read at version 1, the settings read as before or put back as
found, and the controller started again on a lens window put back."""
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(__file__))
import helpers  # noqa: E402
from forgetest import catalog  # noqa: E402
from forgetest.runner import Context, Run  # noqa: E402

IDS = ["setup.check-switches", "setup.check-sensors", "setup.check-airflow", "setup.check-intakes",
       "setup.check-cameras", "setup.check-motion", "setup.check-lens", "setup.check-flow-verify"]
# The checks that put the intake fan count back as found: they start only
# with it set, so a put-back never leaves the gate closed behind them.
COUNTED = ("setup.check-airflow", "setup.check-intakes")


class RegistrationTests(unittest.TestCase):
    def setUp(self):
        self.reg = catalog.load_suite()

    def test_every_check_runs_unattended_with_the_fixture(self):
        # The lid and button checks name their machine actions, which the
        # bench fixture performs; the rest need no hands at all.
        for tid in IDS:
            self.assertIn(tid, self.reg)
        for tid in ("setup.check-switches", "setup.check-cameras"):
            self.assertEqual(self.reg[tid].kind, "operator", tid)
            self.assertTrue(self.reg[tid].fixture_runnable(("button", "lid", "interlock")), tid)
        for tid in ("setup.check-sensors", "setup.check-airflow", "setup.check-intakes",
                    "setup.check-motion", "setup.check-lens", "setup.check-flow-verify"):
            self.assertEqual(self.reg[tid].kind, "auto", tid)

    def test_no_check_asks_the_operator_to_prepare_anything(self):
        for tid in IDS:
            if tid not in COUNTED:
                self.assertIsNone(getattr(self.reg[tid], "precheck", None), tid)

    def test_the_intake_count_must_be_set_before_a_check_puts_it_back(self):
        # The refusal says why the test cannot start; it never names a
        # setting for the operator to change.
        fake = helpers.FakeForgectrl().start()
        try:
            for tid in COUNTED:
                fake.state["settings"].pop("cool_intake_fans", None)
                why = self.reg[tid].cannot_start()
                self.assertIn("the intake fans are not counted", why, tid)
                self.assertIn("the setup gate holds every controller", why, tid)
                self.assertNotIn("cool_intake_fans", why, tid)
                self.assertNotIn("Intake fans step", why, tid)
                fake.state["settings"]["cool_intake_fans"] = "2"
                self.assertIsNone(self.reg[tid].cannot_start(), tid)
        finally:
            fake.stop()

    def test_the_checks_that_take_the_machine_say_so(self):
        for tid in ("setup.check-airflow", "setup.check-intakes", "setup.check-motion", "setup.check-lens",
                    "setup.check-flow-verify"):
            self.assertEqual(self.reg[tid].hardware, "takeover", tid)

    def test_the_lens_check_covers_the_finder_and_the_frame(self):
        covers = set(self.reg["setup.check-lens"].covers)
        for want in (("forgectrl", "src/lensfind.*"), ("forgectrl", "src/lens.*")):
            self.assertIn(want, covers)
        self.assertIn("setup.check-motion", self.reg["setup.check-lens"].requires)
        covers = set(self.reg["setup.check-intakes"].covers)
        for want in (("forgectrl", "src/cool.c"), ("forgectrl", "src/airflow.*")):
            self.assertIn(want, covers)

    def test_every_check_covers_the_runner_and_the_page(self):
        for tid in IDS:
            covers = set(self.reg[tid].covers)
            self.assertIn(("forgectrl", "src/wizdark.*"), covers, tid)
            self.assertIn(("forgectrl", "src/ui/wizard.*"), covers, tid)


class SensorsCheckTests(unittest.TestCase):
    """The check driver against a scripted daemon."""

    RESULT = {"coolant_down_c": 22.4, "coolant_up_c": 21.9, "chassis_c": 28.0, "soc_c": 45.2,
              "lid_ir_max": [110, 120, 105, 118], "accel_events": 0, "laser_pgood": 1,
              "hv_current_max": 0, "exhaust_rpm_idle": 3400, "intake_rpm_idle": 2200}

    def setUp(self):
        self.fake = helpers.FakeForgectrl().start()
        self.fake.state["settings"].update({"cool_temp_offset_c": "0.5"})
        self.dark = {"id": "", "running": False, "phase": "", "progress": 0, "elapsed_s": 0,
                     "log": [], "prompt": None, "result": None, "error": "",
                     "shots": {"lid": False, "head": False}}
        self.versions = {}
        self.answers = []
        self.polls = 0

        def on_get(path, q):
            if path == "/wiz/dark":
                # One poll sees the sampling; the next sees the result.
                if self.dark["running"]:
                    self.polls += 1
                    if self.polls >= 2:
                        self.dark.update({"running": False, "prompt": None, "progress": 100,
                                          "result": dict(self.RESULT)})
                        self.versions["sensors"] = 1
                return 200, dict(self.dark)
            if path == "/wiz":
                return 200, {"versions": dict(self.versions), "required": [], "completed": True}
            return None

        def on_post(path, form):
            s = self.fake.state["settings"]
            if path == "/wiz/sensors/start":
                self.dark.update({"id": "sensors", "running": True, "phase": "sampling",
                                  "prompt": None})
                return 200, {"started": True, "id": "sensors"}
            if path == "/wiz/sensors/answer":
                self.answers.append(dict(form))
                return 409, {"error": "no such prompt is open"}
            if path == "/settings":
                for k, v in form.items():
                    if v == "":
                        s.pop(k, None)
                    else:
                        s[k] = v
                return 200, dict(s)
            return None
        self.fake.on_get = on_get
        self.fake.on_post = on_post

    def tearDown(self):
        self.fake.stop()

    def test_start_no_question_result_and_settings_untouched(self):
        t = catalog.load_suite()["setup.check-sensors"]
        run = Run("test", t.id, t.title)
        t.fn(Context(run, None, t))
        self.assertIn(("/wiz/sensors/start", {}), [(p, f) for p, f in self.fake.posts])
        self.assertEqual(self.answers, [])
        self.assertEqual(run.evidence["result"]["laser_pgood"], 1)
        self.assertEqual(run.evidence["settings_before"].get("cool_temp_offset_c"), "0.5")
        self.assertEqual(self.fake.state["settings"]["cool_temp_offset_c"], "0.5")
        self.assertNotIn("/settings", [p for p, _ in self.fake.posts])

    def test_a_question_fails_the_check(self):
        t = catalog.load_suite()["setup.check-sensors"]
        run = Run("test", t.id, t.title)
        real_get = self.fake.on_get

        def on_get(path, q):
            if path == "/wiz/dark" and self.dark["running"]:
                d = dict(self.dark)
                d["prompt"] = {"seq": 3, "id": "room-temp", "kind": "number", "text": "?",
                               "options": ["Set", "Skip"]}
                return 200, d
            return real_get(path, q)
        self.fake.on_get = on_get
        with self.assertRaises(Exception) as cm:
            t.fn(Context(run, None, t))
        self.assertIn("asked a question", str(cm.exception))
        self.assertEqual(self.answers, [])
        self.assertIn(("/wiz/sensors/abort", {}), [(p, f) for p, f in self.fake.posts])


class AirflowCheckTests(unittest.TestCase):
    """setup.check-airflow's judge against a scripted daemon: the check
    writes its floors, and the intake fan count is the machine's. Intake
    2 turning proves two fans and the check writes the count as 2; intake
    2 silent on a machine counted with one is reported not fitted and not
    turning, and the count is not written; intake 2 silent on a machine
    counted with two fails the check. The bench reference has two, both
    turning."""

    FLOORS = {"cool_tach_exhaust_min_rpm": "6400", "cool_tach_intake_min_rpm": "2290",
              "cool_tach_air_assist_min_rpm": "6050", "cool_purge_min_current": "345",
              "cool_fan_grace_s": "12"}
    FAN = {"steady_rpm": 4150, "spinup_s": 6.8, "ok": True}

    def result(self, silent2):
        fans = {"exhaust": dict(self.FAN), "intake 1": dict(self.FAN), "intake 2": dict(self.FAN),
                "air assist": dict(self.FAN)}
        if silent2:
            fans["intake 2"] = {"fitted": False, "turning": False}
        return {"fans": fans, "intake_fans": 1 if silent2 else 2, "purge_current_on": 628,
                "purge_current_off": 74, "floors": dict(self.FLOORS)}

    def setUp(self):
        import tempfile
        self.sysfs = tempfile.mkdtemp(prefix="forgetest-airflow-")
        os.makedirs(os.path.join(self.sysfs, "head"))
        with open(os.path.join(self.sysfs, "head", "purge_air"), "w") as f:
            f.write("1\n")
        self.saved_root = os.environ.get("GF_SYSFS_ROOT")
        os.environ["GF_SYSFS_ROOT"] = self.sysfs + "/"
        self.fake = helpers.FakeForgectrl().start()
        # the bench reference: two intake fans, counted
        self.fake.state["settings"].update({"cool_tach_intake_min_rpm": "2300", "cool_intake_fans": "2"})
        self.fake.state["cool"] = {"phase": "idle", "fan_gates": {"purge": {"reading": 628, "floor": 345}}}
        self.dark = {"id": "", "running": False, "phase": "", "progress": 0, "elapsed_s": 0,
                     "log": [], "prompt": None, "result": None, "error": "",
                     "shots": {"lid": False, "head": False}}
        self.versions = {}
        self.polls = 0
        self.silent2 = False            # intake 2 gives no tach pulse
        self.wizard_result = None       # the result the check ends with; None = the daemon's own
        self.wizard_writes = None       # the count the check writes; None = the daemon's own rule

        def on_get(path, q):
            if path == "/wiz/dark":
                if self.dark["running"]:
                    self.polls += 1
                    if self.polls >= 2:
                        s = self.fake.state["settings"]
                        if self.silent2 and s.get("cool_intake_fans") == "2":
                            # a silent intake 2 on a machine counted with two
                            self.dark.update({"running": False, "progress": 100, "result": None,
                                              "error": "a fan did not come up to speed at the run "
                                                       "profile: see the log"})
                            return 200, dict(self.dark)
                        r = self.wizard_result or self.result(self.silent2)
                        s.update(r["floors"])
                        if self.wizard_writes:
                            s["cool_intake_fans"] = self.wizard_writes
                        elif not self.silent2:
                            s["cool_intake_fans"] = "2"     # intake 2 turned: two fans
                        self.dark.update({"running": False, "progress": 100, "result": r})
                        self.versions["airflow"] = 1
                return 200, dict(self.dark)
            if path == "/wiz":
                return 200, {"versions": dict(self.versions), "required": [], "completed": True}
            return None

        def on_post(path, form):
            s = self.fake.state["settings"]
            if path == "/wiz/airflow/start":
                self.dark.update({"id": "airflow", "running": True, "phase": "fans to the run profile"})
                return 200, {"started": True, "id": "airflow"}
            if path == "/settings":
                for k, v in form.items():
                    if v == "":
                        s.pop(k, None)
                    else:
                        s[k] = v
                return 200, dict(s)
            return None
        self.fake.on_get = on_get
        self.fake.on_post = on_post

    def tearDown(self):
        import shutil
        self.fake.stop()
        if self.saved_root is None:
            os.environ.pop("GF_SYSFS_ROOT", None)
        else:
            os.environ["GF_SYSFS_ROOT"] = self.saved_root
        shutil.rmtree(self.sysfs, ignore_errors=True)

    def run_check(self):
        t = catalog.load_suite()["setup.check-airflow"]
        run = Run("test", t.id, t.title)
        t.fn(Context(run, None, t))
        return run

    def count(self, n):
        self.fake.state["settings"]["cool_intake_fans"] = n

    def assert_put_back(self, count="2"):
        s = self.fake.state["settings"]
        self.assertEqual(s.get("cool_tach_intake_min_rpm"), "2300")
        self.assertEqual(s.get("cool_intake_fans"), count)
        self.assertNotIn("cool_tach_exhaust_min_rpm", s)

    def test_two_intake_fans_pass_and_are_put_back(self):
        # the bench path: both intakes turn, the count is written as 2
        run = self.run_check()
        self.assertEqual(run.evidence["intakes"], {"counted": "2", "found": 2, "intake_2_silent": False})
        self.assert_put_back()

    def test_a_machine_counted_with_one_whose_intake_2_turns_is_counted_two_and_put_back(self):
        # intake 2 turning proves two fans: the check writes 2, and the
        # count goes back as found
        self.count("1")
        self.run_check()
        self.assert_put_back("1")
        self.assertIn(("/settings", {"cool_intake_fans": "1"}), self.fake.posts)

    def test_one_intake_fan_counted_one_passes_and_the_count_is_not_written(self):
        self.count("1")
        self.silent2 = True
        run = self.run_check()
        self.assertEqual(run.evidence["intakes"], {"counted": "1", "found": 1, "intake_2_silent": True})
        self.assert_put_back("1")

    def test_a_silent_intake_2_on_a_machine_counted_with_two_fails_the_check(self):
        self.silent2 = True
        with self.assertRaises(Exception) as cm:
            self.run_check()
        self.assertIn("a fan did not come up to speed at the run profile", str(cm.exception))
        self.assert_put_back()

    def test_a_silent_intake_2_whose_count_was_written_fails(self):
        # a silent intake 2 never writes the count
        self.count("1")
        self.silent2 = True
        self.wizard_writes = "2"
        with self.assertRaises(Exception) as cm:
            self.run_check()
        self.assertIn("cool_intake_fans reads '2', expected '1' (intake 2 silent", str(cm.exception))
        self.assert_put_back("1")

    def test_a_turning_intake_2_counted_one_fails(self):
        r = self.result(False)
        r["intake_fans"] = 1
        self.wizard_result = r
        with self.assertRaises(Exception) as cm:
            self.run_check()
        self.assertIn("intake 2 turned, and the check found 1 intake fans", str(cm.exception))
        self.assert_put_back()

    def test_a_silent_intake_2_reported_fitted_fails(self):
        self.count("1")
        self.silent2 = True
        r = self.result(True)
        r["fans"]["intake 2"] = {"fitted": True, "turning": False}
        self.wizard_result = r
        with self.assertRaises(Exception) as cm:
            self.run_check()
        self.assertIn("intake 2 with one intake fan", str(cm.exception))
        self.assert_put_back("1")

    def test_a_result_without_the_count_fails(self):
        r = self.result(False)
        del r["intake_fans"]
        self.wizard_result = r
        with self.assertRaises(Exception) as cm:
            self.run_check()
        self.assertIn("no intake fan count", str(cm.exception))
        self.assert_put_back()



class ScriptedCheck(unittest.TestCase):
    """A dark check against the fake daemon: the start, one poll that sees
    the run, the next that sees its end (`finish` writes the settings and
    returns the result), the record at version 1, /status's lens block
    read from the settings as the daemon reads them, and the supervisor's
    stop and start levers (a start records the settings the new controller
    reads)."""

    WID = ""
    LENS_KEYS = ("lens_stop_below_steps", "lens_stop_above_steps", "lens_hall_edge_z_mm")

    def setUp(self):
        self.fake = helpers.FakeForgectrl().start()
        self.dark = {"id": "", "running": False, "phase": "", "progress": 0, "elapsed_s": 0,
                     "log": [], "prompt": None, "result": None, "error": "",
                     "shots": {"lid": False, "head": False}}
        self.versions = {}
        self.polls = 0
        self.prompt = None              # a prompt the run opens on its first poll, when set
        self.answers = []
        self.starts = []                # the lens settings each controller start read

        def on_get(path, q):
            s = self.fake.state["settings"]
            if path == "/wiz/dark":
                if self.dark["running"]:
                    self.polls += 1
                    if self.polls == 1 and self.prompt:
                        self.dark["prompt"] = dict(self.prompt, seq=1)
                    elif self.polls >= 2 and not self.dark["prompt"]:
                        self.dark.update({"running": False, "progress": 100, "result": self.finish(s)})
                        self.versions[self.WID] = 1
                return 200, dict(self.dark)
            if path == "/wiz":
                return 200, {"versions": dict(self.versions), "recommended": [], "completed": True}
            if path == "/status":
                def count(key, fallback):
                    v = s.get(key)
                    return int(v) if v and v.isdigit() and 1 <= int(v) <= 40 else fallback
                lens = {"below": count("lens_stop_below_steps", 10), "above": count("lens_stop_above_steps", 12)}
                return 200, dict(self.fake.state["status"], lens=lens)
            return None

        def on_post(path, form):
            s = self.fake.state["settings"]
            if path == "/wiz/%s/start" % self.WID:
                self.dark.update({"id": self.WID, "running": True, "phase": "running", "prompt": None})
                return 200, {"started": True, "id": self.WID}
            if path == "/wiz/%s/answer" % self.WID:
                self.answers.append(dict(form))
                self.dark["prompt"] = None
                return 200, {"ok": True}
            if path == "/wiz/%s/abort" % self.WID:
                self.dark.update({"running": False, "prompt": None, "error": "aborted"})
                return 200, {"ok": True}
            if path == "/settings":
                for k, v in form.items():
                    if v == "":
                        s.pop(k, None)
                    else:
                        s[k] = v
                return 200, dict(s)
            if path == "/controller/stop":
                self.fake.state["mode"] = dict(self.fake.state["mode"], controller="standby")
                return 200, {"stopped": True}
            if path == "/controller/start":
                m = self.fake.state["mode"]
                self.fake.state["mode"] = dict(m, controller="running", motion="verified", pid=m["pid"] + 1)
                self.starts.append({k: s.get(k) for k in self.LENS_KEYS})
                return 200, {"started": True}
            return None
        self.fake.on_get = on_get
        self.fake.on_post = on_post

    def tearDown(self):
        self.fake.stop()

    def lever_posts(self):
        return [p for p, _ in self.fake.posts if p.startswith("/controller/")]

    def run_check(self, tid):
        t = catalog.load_suite()[tid]
        run = Run("test", t.id, t.title)
        t.fn(Context(run, None, t))
        return run


class LensCheckTests(ScriptedCheck):
    """setup.check-lens's judge: the window is the stops found or the
    fallback, the settings and /status hold it, and both keys go back as
    found."""

    WID = "lens.travel"
    FOUND = {"lens_stop_below_steps": "12", "lens_stop_above_steps": "18"}

    def setUp(self):
        super().setUp()
        self.fake.state["settings"].update(self.FOUND)
        self.stops = {"found": True, "below": 14, "above": 20, "contact_below": 16, "contact_above": 22, "why": ""}
        self.writes = None              # what the check writes; None = the window of its result

    def finish(self, s):
        st = self.stops
        below, above = (st["below"], st["above"]) if st["found"] else (10, 12)
        w = self.writes or (below, above)
        s.update({"lens_stop_below_steps": str(w[0]), "lens_stop_above_steps": str(w[1])})
        return {"z_passes": [100, 101], "stops": dict(st),
                "window": {"below": below, "above": above, "found": st["found"]}, "summary": "x"}

    def assert_put_back(self):
        """The window as found, in the settings and in the controller:
        grblHAL reads it only at its start, so the hand-back is one stop
        and one start after the put-back, the start reading the window
        as found."""
        s = self.fake.state["settings"]
        for k, v in self.FOUND.items():
            self.assertEqual(s.get(k), v, k)
        self.assertEqual(self.lever_posts(), ["/controller/stop", "/controller/start"])
        self.assertEqual([{k: st[k] for k in self.FOUND} for st in self.starts], [self.FOUND])
        self.assertEqual(self.fake.state["mode"]["controller"], "running")
        self.assertEqual(self.fake.state["mode"]["pid"], 101)

    def test_the_stops_found_are_the_window(self):
        run = self.run_check("setup.check-lens")
        self.assertEqual(run.evidence["window"]["settings"], ("14", "20"))
        self.assertEqual(run.evidence["window"]["status"]["below"], 14)
        self.assertEqual(run.evidence["result"]["stops"]["contact_above"], 22)
        self.assertEqual(run.evidence["restart"], {"mode": "grbl", "pid": 100, "stop": 200, "start": 200})
        self.assert_put_back()

    def test_stops_not_found_write_the_fallback(self):
        self.stops = {"found": False, "below": 0, "above": 0, "contact_below": 0, "contact_above": 0,
                      "why": "the lens steps do not ring clearly enough on this head"}
        run = self.run_check("setup.check-lens")
        self.assertEqual(run.evidence["window"]["settings"], ("10", "12"))
        self.assert_put_back()

    def test_a_window_the_settings_do_not_hold_fails_and_still_restores(self):
        self.writes = (12, 18)
        with self.assertRaises(Exception) as cm:
            self.run_check("setup.check-lens")
        self.assertIn("the settings hold", str(cm.exception))
        self.assert_put_back()

    def test_a_question_fails_the_check(self):
        self.prompt = {"id": "lens-stop", "kind": "continue", "text": "?", "options": ["Continue"]}
        with self.assertRaises(Exception) as cm:
            self.run_check("setup.check-lens")
        self.assertIn("asked a question", str(cm.exception))
        self.assertEqual(self.answers, [])
        self.assertIn(("/wiz/lens.travel/abort", {}), self.fake.posts)
        self.assert_put_back()

    def test_a_controller_that_does_not_restart_fails_the_hand_back(self):
        # The start refused: the controller stays stopped on the stale
        # window's heels, and the run says so rather than pass.
        real = self.fake.on_post

        def on_post(path, form):
            if path == "/controller/start":
                return 500, {"error": "no"}
            return real(path, form)
        self.fake.on_post = on_post
        with self.assertRaises(Exception) as cm:
            self.run_check("setup.check-lens")
        self.assertIn("the controller was not restarted on the window as found", str(cm.exception))

    def test_cloud_mode_restarts_nothing(self):
        # The cloud client reads no lens window: nothing to restart.
        self.fake.state["mode"] = dict(self.fake.state["mode"], mode="cloud")
        run = self.run_check("setup.check-lens")
        self.assertEqual(self.lever_posts(), [])
        self.assertEqual(run.evidence["restart"], {"mode": "cloud", "pid": 100})


class IntakesCheckTests(ScriptedCheck):
    """setup.check-intakes's judge: the tachs count the intake fans the
    machine held, the step writes the count, and it goes back as found.
    The tachs count only two turning fans: intake 2 giving no reading
    reads the same as a machine built with one, so the step asks the
    operator, and so it does when intake 1 does not come up. Both intakes
    turn on the bench reference, so either question fails the test
    unanswered."""

    WID = "airflow.intakes"
    # The step's two questions, as the daemon asks them.
    SILENT_2 = ("Intake 1 turns; intake 2 gives no reading. Look at the back of the machine: how many "
                "intake fans does it have? If it has two, the second one needs attention before the "
                "machine cuts.")
    SLOW_1 = ("The intake fans could not be counted: intake 1 did not come up to speed. Look at the back "
              "of the machine: how many intake fans does it have? A fan that does not turn needs "
              "attention before the machine cuts.")

    def setUp(self):
        super().setUp()
        self.fake.state["settings"]["cool_intake_fans"] = "2"

    def finish(self, s):
        # both intakes turned: two, by the tachs
        s["cool_intake_fans"] = "2"
        return {"intake_fans": 2, "intake_1_rpm": 4150.0, "intake_2_rpm": 4100.0, "intake_2_silent": False,
                "counted_by": "tachs", "summary": "x"}

    def ask(self, text):
        self.prompt = {"id": "intake-count", "kind": "choice", "text": text, "options": ["One", "Two"]}

    def test_two_intake_fans_counted_and_put_back(self):
        run = self.run_check("setup.check-intakes")
        self.assertEqual(run.evidence["count"]["counted"], 2)
        self.assertEqual(run.evidence["count"]["by"], "tachs")
        self.assertEqual(self.fake.state["settings"]["cool_intake_fans"], "2")
        self.assertIn(("/settings", {"cool_intake_fans": "2"}), self.fake.posts)    # the put-back

    def test_a_count_other_than_the_machine_held_fails_and_puts_it_back(self):
        # a machine that held one intake fan, whose tachs saw two turn
        self.fake.state["settings"]["cool_intake_fans"] = "1"
        with self.assertRaises(Exception) as cm:
            self.run_check("setup.check-intakes")
        self.assertIn("the tachs count 2 intake fans, the machine held 1", str(cm.exception))
        self.assertEqual(self.fake.state["settings"]["cool_intake_fans"], "1")

    def test_a_silent_intake_2_is_a_fault_the_test_does_not_answer(self):
        # intake 1 turns, intake 2 gives no reading: the step asks the
        # operator, and on the bench reference that is a fan needing attention
        self.ask(self.SILENT_2)
        with self.assertRaises(Exception) as cm:
            self.run_check("setup.check-intakes")
        self.assertIn("asked for the count, which the tachs could not make", str(cm.exception))
        self.assertIn("intake 2 gives no reading", str(cm.exception))
        self.assertEqual(self.answers, [])
        self.assertIn(("/wiz/airflow.intakes/abort", {}), self.fake.posts)
        self.assertEqual(self.fake.state["settings"]["cool_intake_fans"], "2")

    def test_a_silent_intake_2_on_a_machine_that_held_one_still_fails(self):
        # the test never answers the operator's question, whatever was held
        self.fake.state["settings"]["cool_intake_fans"] = "1"
        self.ask(self.SILENT_2)
        with self.assertRaises(Exception) as cm:
            self.run_check("setup.check-intakes")
        self.assertIn("asked for the count", str(cm.exception))
        self.assertEqual(self.answers, [])
        self.assertEqual(self.fake.state["settings"]["cool_intake_fans"], "1")

    def test_intake_1_not_up_to_speed_fails_the_check(self):
        self.ask(self.SLOW_1)
        with self.assertRaises(Exception) as cm:
            self.run_check("setup.check-intakes")
        self.assertIn("asked for the count", str(cm.exception))
        self.assertIn("intake 1 did not come up to speed", str(cm.exception))
        self.assertEqual(self.answers, [])
        self.assertEqual(self.fake.state["settings"]["cool_intake_fans"], "2")

    def test_the_tachs_count_is_judged_by_the_count_alone(self):
        # counted by the tachs, the count is the whole judgment: the speeds
        # are recorded, not judged again
        real = self.finish
        self.finish = lambda s: dict(real(s), intake_1_rpm=None)
        run = self.run_check("setup.check-intakes")
        self.assertIsNone(run.evidence["count"]["intake_1_rpm"])


if __name__ == "__main__":
    unittest.main()
