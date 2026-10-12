# Copyright 2026 514 LLC d/b/a OpenGlow
# Written by Scott Wiederhold
# https://community.openglow.org
# SPDX-License-Identifier:    MIT

"""The setup's checks in the catalog: registered as automatic tests, the
takeover ones marked so, every covers map naming the check runner, and
the two that put the intake fan count back as found refused unless it is
set; and the check driver itself against the fake daemon: a start, the
run polled to its end with no question asked, the result taken, the
record read at version 1, and the settings read as before or put back
as found."""
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
        fake = helpers.FakeForgectrl().start()
        try:
            for tid in COUNTED:
                fake.state["settings"].pop("cool_intake_fans", None)
                why = self.reg[tid].cannot_start()
                self.assertIn("cool_intake_fans is unset", why, tid)
                self.assertIn("Intake fans step", why, tid)
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
    writes its floors and the intake fans fitted, and the test reads the
    fans the count says are there. A machine built with one intake fan
    reports intake 2 not fitted; the bench reference has two."""

    FLOORS = {"cool_tach_exhaust_min_rpm": "6400", "cool_tach_intake_min_rpm": "2290",
              "cool_tach_air_assist_min_rpm": "6050", "cool_purge_min_current": "345",
              "cool_fan_grace_s": "12"}
    FAN = {"steady_rpm": 4150, "spinup_s": 6.8, "ok": True}

    def result(self, intakes):
        fans = {"exhaust": dict(self.FAN), "intake 1": dict(self.FAN), "intake 2": dict(self.FAN),
                "air assist": dict(self.FAN)}
        if intakes == 1:
            fans["intake 2"] = {"fitted": False}
        return {"fans": fans, "intake_fans": intakes, "purge_current_on": 628, "purge_current_off": 74,
                "floors": dict(self.FLOORS)}

    def setUp(self):
        import tempfile
        self.sysfs = tempfile.mkdtemp(prefix="forgetest-airflow-")
        os.makedirs(os.path.join(self.sysfs, "head"))
        with open(os.path.join(self.sysfs, "head", "purge_air"), "w") as f:
            f.write("1\n")
        self.saved_root = os.environ.get("GF_SYSFS_ROOT")
        os.environ["GF_SYSFS_ROOT"] = self.sysfs + "/"
        self.fake = helpers.FakeForgectrl().start()
        self.fake.state["settings"].update({"cool_tach_intake_min_rpm": "2300"})
        self.fake.state["cool"] = {"phase": "idle", "fan_gates": {"purge": {"reading": 628, "floor": 345}}}
        self.dark = {"id": "", "running": False, "phase": "", "progress": 0, "elapsed_s": 0,
                     "log": [], "prompt": None, "result": None, "error": "",
                     "shots": {"lid": False, "head": False}}
        self.versions = {}
        self.polls = 0
        self.wizard_result = self.result(2)
        self.wizard_writes = None       # what the check writes; None = what its result says

        def on_get(path, q):
            if path == "/wiz/dark":
                if self.dark["running"]:
                    self.polls += 1
                    if self.polls >= 2:
                        r = self.wizard_result
                        s = self.fake.state["settings"]
                        s.update(r["floors"])
                        s["cool_intake_fans"] = self.wizard_writes or str(r["intake_fans"])
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

    def assert_put_back(self):
        s = self.fake.state["settings"]
        self.assertEqual(s.get("cool_tach_intake_min_rpm"), "2300")
        self.assertNotIn("cool_intake_fans", s)
        self.assertNotIn("cool_tach_exhaust_min_rpm", s)

    def test_two_intake_fans_pass_and_are_put_back(self):
        self.run_check()
        self.assert_put_back()

    def test_one_intake_fan_passes_with_intake_2_not_fitted(self):
        self.wizard_result = self.result(1)
        self.run_check()
        self.assert_put_back()

    def test_one_intake_fan_with_the_setting_left_at_two_fails(self):
        self.wizard_result = self.result(1)
        self.wizard_writes = "2"
        with self.assertRaises(Exception) as cm:
            self.run_check()
        self.assertIn("cool_intake_fans reads '2', the check found 1", str(cm.exception))
        self.assert_put_back()

    def test_a_result_without_the_count_fails(self):
        r = self.result(2)
        del r["intake_fans"]
        self.wizard_result = r
        self.wizard_writes = "2"
        with self.assertRaises(Exception) as cm:
            self.run_check()
        self.assertIn("no intake fan count", str(cm.exception))
        self.assert_put_back()



class ScriptedCheck(unittest.TestCase):
    """A dark check against the fake daemon: the start, one poll that sees
    the run, the next that sees its end (`finish` writes the settings and
    returns the result), the record at version 1, and /status's lens block
    read from the settings as the daemon reads them."""

    WID = ""

    def setUp(self):
        self.fake = helpers.FakeForgectrl().start()
        self.dark = {"id": "", "running": False, "phase": "", "progress": 0, "elapsed_s": 0,
                     "log": [], "prompt": None, "result": None, "error": "",
                     "shots": {"lid": False, "head": False}}
        self.versions = {}
        self.polls = 0
        self.prompt = None              # a prompt the run opens on its first poll, when set
        self.answers = []

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
            return None
        self.fake.on_get = on_get
        self.fake.on_post = on_post

    def tearDown(self):
        self.fake.stop()

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
        s = self.fake.state["settings"]
        for k, v in self.FOUND.items():
            self.assertEqual(s.get(k), v, k)

    def test_the_stops_found_are_the_window(self):
        run = self.run_check("setup.check-lens")
        self.assertEqual(run.evidence["window"]["settings"], ("14", "20"))
        self.assertEqual(run.evidence["window"]["status"]["below"], 14)
        self.assertEqual(run.evidence["result"]["stops"]["contact_above"], 22)
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


class IntakesCheckTests(ScriptedCheck):
    """setup.check-intakes's judge: the tachs count the intake fans the
    machine held, the step writes the count, and it goes back as found."""

    WID = "airflow.intakes"

    def setUp(self):
        super().setUp()
        self.fake.state["settings"]["cool_intake_fans"] = "2"
        self.count = 2

    def finish(self, s):
        s["cool_intake_fans"] = str(self.count)
        return {"intake_fans": self.count, "intake_1_rpm": 4150.0,
                "intake_2_rpm": 0.0 if self.count == 1 else 4100.0, "intake_2_silent": self.count == 1,
                "counted_by": "tachs", "summary": "x"}

    def test_two_intake_fans_counted_and_put_back(self):
        run = self.run_check("setup.check-intakes")
        self.assertEqual(run.evidence["count"]["counted"], 2)
        self.assertEqual(self.fake.state["settings"]["cool_intake_fans"], "2")
        self.assertIn(("/settings", {"cool_intake_fans": "2"}), self.fake.posts)    # the put-back

    def test_one_intake_fan_on_a_machine_built_with_one(self):
        self.fake.state["settings"]["cool_intake_fans"] = "1"
        self.count = 1
        run = self.run_check("setup.check-intakes")
        self.assertEqual(run.evidence["count"]["counted"], 1)
        self.assertEqual(self.fake.state["settings"]["cool_intake_fans"], "1")

    def test_a_count_other_than_the_machine_held_fails_and_puts_it_back(self):
        self.count = 1
        with self.assertRaises(Exception) as cm:
            self.run_check("setup.check-intakes")
        self.assertIn("the tachs count 1 intake fans, the machine held 2", str(cm.exception))
        self.assertEqual(self.fake.state["settings"]["cool_intake_fans"], "2")

    def test_the_operator_question_fails_the_check(self):
        # intake 1 never came up: the step asks a person to count, which
        # is not a count by the tachs
        self.prompt = {"id": "intake-count", "kind": "choice", "text": "how many?", "options": ["One", "Two"]}
        with self.assertRaises(Exception) as cm:
            self.run_check("setup.check-intakes")
        self.assertIn("intake 1 did not come up to speed", str(cm.exception))
        self.assertEqual(self.answers, [])
        self.assertEqual(self.fake.state["settings"]["cool_intake_fans"], "2")


if __name__ == "__main__":
    unittest.main()
