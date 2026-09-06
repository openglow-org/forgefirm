"""The setup's checks in the catalog: registered as automatic tests with
no precheck, the takeover ones marked so, every covers map naming the
check runner; and the check driver itself against the fake daemon: a
start, a prompt answered, the result taken, the record read at version
1, and the settings a check may write put back as found."""
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(__file__))
import helpers  # noqa: E402
from forgetest import catalog  # noqa: E402
from forgetest.runner import Context, Run  # noqa: E402

IDS = ["commission.check-switches", "commission.check-sensors", "commission.check-airflow",
       "commission.check-cameras", "commission.check-motion", "commission.check-flow-verify"]


class RegistrationTests(unittest.TestCase):
    def setUp(self):
        self.reg = catalog.load_suite()

    def test_every_check_runs_unattended_with_the_fixture(self):
        # The lid and button checks name their machine actions, which the
        # bench fixture performs; the rest need no hands at all.
        for tid in IDS:
            self.assertIn(tid, self.reg)
        for tid in ("commission.check-switches", "commission.check-cameras"):
            self.assertEqual(self.reg[tid].kind, "operator", tid)
            self.assertTrue(self.reg[tid].fixture_runnable(("button", "lid", "interlock")), tid)
        for tid in ("commission.check-sensors", "commission.check-airflow", "commission.check-motion",
                    "commission.check-flow-verify"):
            self.assertEqual(self.reg[tid].kind, "auto", tid)

    def test_no_check_asks_the_operator_to_prepare_anything(self):
        for tid in IDS:
            self.assertIsNone(getattr(self.reg[tid], "precheck", None), tid)

    def test_the_checks_that_take_the_machine_say_so(self):
        for tid in ("commission.check-airflow", "commission.check-motion", "commission.check-flow-verify"):
            self.assertEqual(self.reg[tid].hardware, "takeover", tid)

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

        def on_get(path, q):
            if path == "/wiz/dark":
                return 200, dict(self.dark)
            if path == "/wiz":
                return 200, {"versions": dict(self.versions), "required": [], "completed": True}
            return None

        def on_post(path, form):
            s = self.fake.state["settings"]
            if path == "/wiz/sensors/start":
                self.dark.update({"id": "sensors", "running": True, "phase": "sampling",
                                  "prompt": {"seq": 7, "id": "room-temp", "kind": "number",
                                             "text": "Optional: the room temperature.",
                                             "options": ["Set", "Skip"]}})
                return 200, {"started": True, "id": "sensors"}
            if path == "/wiz/sensors/answer":
                self.answers.append(dict(form))
                if form.get("seq") != "7":
                    return 409, {"error": "no such prompt is open"}
                if form.get("value") == "Skip":
                    self.dark.update({"running": False, "prompt": None, "progress": 100,
                                      "result": dict(self.RESULT)})
                    self.versions["sensors"] = 1
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

    def test_start_prompt_answer_result_and_restore(self):
        t = catalog.load_suite()["commission.check-sensors"]
        run = Run("test", t.id, t.title)
        t.fn(Context(run, None, t))
        self.assertIn(("/wiz/sensors/start", {}), [(p, f) for p, f in self.fake.posts])
        self.assertEqual(self.answers, [{"seq": "7", "value": "Skip"}])
        self.assertEqual(run.evidence["result"]["laser_pgood"], 1)
        self.assertEqual(run.evidence["settings_before"], {"cool_temp_offset_c": "0.5"})
        self.assertEqual(self.fake.state["settings"]["cool_temp_offset_c"], "0.5")


if __name__ == "__main__":
    unittest.main()
