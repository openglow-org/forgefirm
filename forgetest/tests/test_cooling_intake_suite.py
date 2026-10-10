# Copyright 2026 514 LLC d/b/a OpenGlow
# Written by Scott Wiederhold
# https://community.openglow.org
# SPDX-License-Identifier:    MIT

"""cooling.intake-one-fan replayed host-side under the real runner
Context against a scripted engine: the fake reads cool_intake_fans and
the floors at every M8 (the engine reloads at run start), publishes
intake_2 not fitted with no floor when one intake fan is set, holds every
fan in grace for the configured seconds, then judges the bench readings
against the floors. The negative engines are the two ways the change
could go wrong: the setting ignored (intake 2 still judged) and the
setting taking intake 1 out with it."""
import threading
import time
import unittest

import helpers
from forgetest.runner import Context, Failed, Run
from forgetest.suite import cooling, cooling_intake
from test_cooling_suite import FakeGrbl


class IntakeOneFanTests(unittest.TestCase):
    READINGS = {"exhaust": 11640, "intake_1": 4157, "intake_2": 4158, "air_assist": 11050, "purge": 625}
    FLOORS = {"exhaust": ("cool_tach_exhaust_min_rpm", 6400.0, 20000.0),
              "intake_1": ("cool_tach_intake_min_rpm", 2290.0, 20000.0),
              "intake_2": ("cool_tach_intake_min_rpm", 2290.0, 20000.0),
              "air_assist": ("cool_tach_air_assist_min_rpm", 6000.0, 30000.0),
              "purge": ("cool_purge_min_current", 300.0, 1023.0)}
    GATE_OF = {"exhaust": "exhaust", "intake_1": "intake", "intake_2": "intake",
               "air_assist": "air_assist", "purge": "purge"}

    def setUp(self):
        self.fc = helpers.FakeForgectrl().start()
        self.grbl = FakeGrbl()
        self.saved = (cooling.VERDICT_WAIT_S, cooling.SESSION_END_WAIT_S, cooling_intake.INTAKE_TRIP_WAIT_S)
        cooling.VERDICT_WAIT_S = 4
        cooling.SESSION_END_WAIT_S = 3
        cooling_intake.INTAKE_TRIP_WAIT_S = 4
        self.fc.state["status"] = dict(self.fc.state["status"], gates_off=[])
        self.fc.state["cool"] = {"phase": "idle", "verdict": "OK", "fire_ok": False, "hold": False,
                                 "gates_off": [], "fan_gates": {}}
        for key, _d, _h in self.FLOORS.values():
            self.fc.state["settings"].setdefault(key, "")
        self.fc.state["settings"].setdefault("cool_fan_grace_s", "")
        self.fc.state["settings"].setdefault("cool_intake_fans", "")
        self.honors_setting = True     # the engine takes intake 2 out with one intake fan
        self.drops_both = False        # the engine takes intake 1 out with it
        self.grace_scale = 0.1         # seconds of fake grace per configured second
        self._describe()
        self.grbl.on_command = self._engine
        self.fc.on_post = self._on_post

    def tearDown(self):
        cooling.VERDICT_WAIT_S, cooling.SESSION_END_WAIT_S, cooling_intake.INTAKE_TRIP_WAIT_S = self.saved
        self.grbl.close()
        self.fc.stop()

    def setting(self, key, default):
        v = self.fc.state["settings"].get(key) or ""
        return float(v) if v else default

    def _describe(self):
        gates = {}
        for fan, (key, default, hi) in self.FLOORS.items():
            v = self.setting(key, default)
            gates[key] = {"gate": self.GATE_OF[fan], "def": default, "lo": 0.0, "hi": hi,
                          "band": [default * 0.9, default * 1.1], "off": "low", "value": v,
                          "state": "off" if v <= 0 else "ok"}
        self.fc.state["settings"]["gates"] = gates

    def _on_post(self, path, form):
        if path != "/settings":
            return None
        self.fc.state["settings"].update(form)
        self._describe()
        return (200, self.fc.state["settings"])

    def _engine(self, line):
        self._describe()
        cool = self.fc.state["cool"]
        if line == "M9":
            def end():
                time.sleep(0.3)
                cool["phase"] = "idle"
                cool.update(verdict="OK", fire_ok=True, hold=False, reason="")
                for g in cool.get("fan_gates", {}).values():
                    if g["state"] != "not fitted":
                        g["state"] = "idle"
            threading.Thread(target=end, daemon=True).start()
            return
        if line != "M8":
            return
        cool["phase"] = "run"
        one = self.fc.state["settings"].get("cool_intake_fans") == "1" and self.honors_setting
        out = {"intake_2"} | ({"intake_1"} if self.drops_both else set()) if one else set()
        floors = {fan: 0.0 if fan in out else self.setting(key, default)
                  for fan, (key, default, _h) in self.FLOORS.items()}
        cool.update(verdict="OK", fire_ok=True, hold=False, resume_ok=True, reason="", gates_off=[],
                    fan_gates={f: {"reading": self.READINGS[f], "floor": floors[f],
                                   "state": "not fitted" if f in out else "grace"} for f in floors})
        grace = self.setting("cool_fan_grace_s", 15.0) * self.grace_scale

        def judge():
            time.sleep(grace)
            if cool["phase"] != "run":
                return
            for f in floors:
                if f not in out:
                    cool["fan_gates"][f]["state"] = "ok" if self.READINGS[f] >= floors[f] else "under"
            time.sleep(0.3)
            if cool["phase"] != "run":
                return
            for f in floors:
                if f not in out and floors[f] > self.READINGS[f]:
                    cool["fan_gates"][f]["state"] = "TRIPPED"
                    if cool["verdict"] != "AIRFLOW":
                        cool.update(verdict="AIRFLOW", fire_ok=False, hold=True, resume_ok=False,
                                    reason="AIRFLOW: %s %d under the %d floor for 3 s - hold, no resume this job"
                                           % (f, self.READINGS[f], floors[f]))
        threading.Thread(target=judge, daemon=True).start()

    def run_test(self):
        run = Run("test", "cooling.intake-one-fan", "t")
        ctx = Context(run, None, helpers.make_test("cooling.intake-one-fan", []))
        cooling_intake.intake_one_fan(ctx)
        return run

    def settings_posts(self):
        return [f for p, f in self.fc.posts if p == "/settings"]

    def restored(self):
        return {"cool_intake_fans": "", "cool_tach_intake_min_rpm": "", "cool_fan_grace_s": ""}

    def test_one_fan_then_intake_1_trip_then_restore_pass(self):
        run = self.run_test()
        ev = run.evidence
        self.assertEqual(ev["one_fan"]["intake_2"]["state"], "not fitted")
        self.assertEqual(ev["one_fan"]["intake_1"]["state"], "ok")
        self.assertEqual(ev["intake_1_trip"]["intake_1"]["state"], "TRIPPED")
        self.assertEqual(ev["intake_1_trip"]["intake_2"]["state"], "not fitted")
        self.assertTrue(all(g["state"] == "ok" for g in ev["restored"].values()))
        posts = self.settings_posts()
        self.assertEqual(posts[0], {"cool_intake_fans": "1", "cool_fan_grace_s": "8"})
        self.assertEqual(posts[1], {"cool_tach_intake_min_rpm": "20000.0"})
        self.assertEqual(posts[-1], self.restored())
        self.assertEqual(self.grbl.commands.count("M8"), 3)
        self.assertEqual(self.grbl.commands.count("M9"), 3)

    def test_an_engine_that_ignores_the_setting_fails_and_restores(self):
        self.honors_setting = False
        with self.assertRaises(Failed) as cm:
            self.run_test()
        self.assertIn("intake_2 is not published not fitted", str(cm.exception))
        self.assertEqual(self.settings_posts()[-1], self.restored())
        self.assertEqual(self.fc.state["cool"]["verdict"], "OK")

    def test_an_engine_that_drops_intake_1_too_fails_and_restores(self):
        self.drops_both = True
        with self.assertRaises(Failed) as cm:
            self.run_test()
        self.assertIn("intake_1 is not judged ok at the intake floor", str(cm.exception))
        self.assertEqual(self.settings_posts()[-1], self.restored())

    def test_a_machine_already_at_one_intake_fan_is_refused_untouched(self):
        self.fc.state["settings"]["cool_intake_fans"] = "1"
        with self.assertRaises(Failed) as cm:
            self.run_test()
        self.assertIn("already 1", str(cm.exception))
        self.assertEqual(self.settings_posts(), [])
        self.assertEqual(self.grbl.commands.count("M8"), 0)


if __name__ == "__main__":
    unittest.main()
