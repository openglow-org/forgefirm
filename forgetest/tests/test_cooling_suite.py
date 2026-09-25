# Copyright 2026 514 LLC d/b/a OpenGlow
# Written by Scott Wiederhold
# https://community.openglow.org
# SPDX-License-Identifier:    MIT

"""cooling.fans-quiet-after-motion and cooling.gate-off replayed host-side
under the real runner Context against a scripted machine: a fake
forgectrl (/status fans, /cool/status phase and verdict, /settings with
the gates table, /logs/tail), a fake kernel sysfs (the fan duties), and
a fake Grbl port that answers '?' and every command.

The bench case that motivated this: the test started one second after
the engine of a previous test went idle, took the tachs still coasting
at the cooldown level as its idle reference, and then waited for the
fans to come back UP to it. The idle reference now needs the engine
idle, the idle duty applied, and tachs that have stopped changing; the
pass condition is at-or-below that level, and every sample is logged.
"""
import os
import shutil
import socket
import tempfile
import threading
import time
import unittest

import helpers
from forgetest.runner import Context, Failed, Run
from forgetest.suite import cooling, motion


class EngineLog:
    """The engine's log lines, as the tests read them: appended to a scratch
    forgectrl log (a check reads the lines written after its mark) and to the
    fake /logs/tail."""

    def log_setup(self):
        self.logdir = tempfile.mkdtemp(prefix="forgetest-cool-")
        self.saved_log = motion.FORGECTRL_LOG
        motion.FORGECTRL_LOG = os.path.join(self.logdir, "forgectrl.log")
        open(motion.FORGECTRL_LOG, "w").close()

    def log_teardown(self):
        motion.FORGECTRL_LOG = self.saved_log
        shutil.rmtree(self.logdir, ignore_errors=True)

    def emit(self, text):
        self.fc.state.setdefault("logs_tail", {"text": ""})["text"] += text
        with open(motion.FORGECTRL_LOG, "a", encoding="utf-8") as f:
            f.write(text)


class FakeGrbl:
    """Answers '?' with an Idle report and every line with ok."""

    def __init__(self):
        self.sock = socket.socket()
        self.sock.bind(("127.0.0.1", 0))
        self.sock.listen(2)
        os.environ["GRBL_HOST"] = "127.0.0.1"
        os.environ["GRBL_PORT"] = str(self.sock.getsockname()[1])
        self.commands = []
        self.on_command = None
        self._stop = False
        threading.Thread(target=self._serve, daemon=True).start()

    def _serve(self):
        self.sock.settimeout(0.2)
        while not self._stop:
            try:
                c, _ = self.sock.accept()
            except OSError:
                continue
            threading.Thread(target=self._client, args=(c,), daemon=True).start()

    def _client(self, c):
        c.settimeout(0.1)
        buf = b""
        c.sendall(b"\r\nGrbl 1.1f ['$' for help]\r\n")
        while not self._stop:
            try:
                d = c.recv(4096)
            except socket.timeout:
                continue
            except OSError:
                break
            if not d:
                break
            for ch in d:
                if ch == 0x3F:                      # '?'
                    c.sendall(b"<Idle|MPos:0.000,0.000,0.000|FS:0,0>\r\n")
                elif ch in (0x18, 0x85):
                    pass
                else:
                    buf += bytes([ch])
            while b"\n" in buf:
                line, buf = buf.split(b"\n", 1)
                line = line.decode().strip()
                if line:
                    self.commands.append(line)
                    if self.on_command:
                        self.on_command(line)
                    c.sendall(b"ok\r\n")
        c.close()

    def close(self):
        self._stop = True
        self.sock.close()
        for k in ("GRBL_HOST", "GRBL_PORT"):
            os.environ.pop(k, None)


class FansQuietTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="forgetest-cool-")
        self.sysfs = os.path.join(self.tmp, "sysfs") + os.sep
        os.makedirs(self.sysfs + "thermal")
        os.makedirs(self.sysfs + "cnc")
        with open(self.sysfs + "cnc/state", "w") as f:
            f.write("idle")
        os.environ["GF_SYSFS_ROOT"] = self.sysfs
        self.fc = helpers.FakeForgectrl().start()
        self.grbl = FakeGrbl()
        self.saved = (cooling.SAMPLE_S, cooling.IDLE_REF_TIMEOUT_S, cooling.COOLDOWN_TIMEOUT_S,
                      cooling.RESTART_IDLE_TIMEOUT_S, cooling.hw.initd)
        cooling.SAMPLE_S = 0.1
        cooling.IDLE_REF_TIMEOUT_S = 3
        cooling.COOLDOWN_TIMEOUT_S = 4
        cooling.RESTART_IDLE_TIMEOUT_S = 1
        # The daemon restart of the last phase, scripted: the init script
        # is a stub, and the engine that comes up applies `after_start`
        # (the idle duties by default; a daemon that keeps the busy-start
        # cooldown airflow applies those instead).
        self.initd_calls = []
        self.after_start = (0, 0)

        def initd(service, action, timeout=60):
            self.initd_calls.append((service, action))
            if action == "start":
                self.duty(*self.after_start)
            return 0, ""
        cooling.hw.initd = initd
        self.fc.state["cool"] = {"phase": "idle", "armed": False, "hold": False}
        self.duty(0, 0)
        self.fans(0, 736, 733)

    def tearDown(self):
        (cooling.SAMPLE_S, cooling.IDLE_REF_TIMEOUT_S, cooling.COOLDOWN_TIMEOUT_S,
         cooling.RESTART_IDLE_TIMEOUT_S, cooling.hw.initd) = self.saved
        self.grbl.close()
        self.fc.stop()
        os.environ.pop("GF_SYSFS_ROOT", None)
        shutil.rmtree(self.tmp, ignore_errors=True)

    # -- the machine -------------------------------------------------------
    def duty(self, exhaust, intake):
        for attr, v in (("thermal/exhaust_pwm", exhaust), ("thermal/intake_pwm", intake)):
            with open(self.sysfs + attr, "w") as f:
                f.write(str(v))

    def fans(self, exhaust, i1, i2, air=2080):
        self.fc.state["status"] = dict(self.fc.state["status"],
                                       fans={"air_assist": air, "exhaust": exhaust, "intake_1": i1, "intake_2": i2})

    def run_engine(self):
        """M8 -> run profile; M9 -> smoke phase, then idle duty, then the
        tachs coast down (a few samples of spin-down), like forgectrl's
        engine on the bench."""
        def on_command(line):
            if line == "M8":
                self.duty(65535, 43278)
                self.fc.state["cool"]["phase"] = "run"
                self.fans(6753, 3212, 3328, air=10997)
            elif line == "M9":
                def cooldown():
                    self.fc.state["cool"]["phase"] = "smoke"
                    time.sleep(0.3)
                    self.fc.state["cool"]["phase"] = "idle"
                    self.duty(0, 0)
                    for ex, i1 in ((5030, 3319), (3100, 2200), (1200, 1300), (0, 740)):
                        self.fans(ex, i1, i1 + 5)
                        time.sleep(0.25)
                threading.Thread(target=cooldown, daemon=True).start()
        self.grbl.on_command = on_command

    def run_test(self):
        run = Run("test", "cooling.fans-quiet-after-motion", "t")
        ctx = Context(run, None, helpers.make_test("cooling.fans-quiet-after-motion", []))
        cooling.fans_quiet(ctx)
        return run

    # -- cases ------------------------------------------------------------------
    def test_idle_machine_passes_and_logs_every_sample(self):
        self.run_engine()
        run = self.run_test()
        self.assertEqual(run.evidence["before"]["exhaust"], 0)
        self.assertIsNotNone(run.evidence["settle_s"])
        self.assertEqual(run.evidence["duty_after"], cooling.IDLE_DUTY)
        self.assertIn("M8", self.grbl.commands)
        self.assertIn("M9", self.grbl.commands)
        cooldown_lines = [ln for ln in run.lines if "cooldown +" in ln]
        self.assertGreaterEqual(len(cooldown_lines), 2, run.lines)

    def test_a_spin_down_in_progress_is_not_taken_as_the_idle_reference(self):
        """The bench case: the previous test's fans are still coasting when
        this one starts. The reference waits for them to settle, and the
        run passes instead of waiting for the fans to come back up."""
        self.run_engine()
        self.fans(5030, 3319, 3377)                      # coasting, engine already idle, duty 0

        def coast():
            for ex, i1 in ((3900, 2600), (2500, 1700), (1100, 1000), (0, 736), (0, 736)):
                time.sleep(0.12)
                self.fans(ex, i1, i1 + 5)
        threading.Thread(target=coast, daemon=True).start()
        run = self.run_test()
        self.assertEqual(run.evidence["before"]["exhaust"], 0, run.evidence["before"])
        self.assertGreater(run.evidence["idle_ref_s"], 0.3)
        self.assertIsNotNone(run.evidence["settle_s"])
        self.assertTrue(any("idle ref:" in ln for ln in run.lines))

    def test_the_restart_phase_stops_disables_starts_and_sees_idle_duty(self):
        self.run_engine()
        run = self.run_test()
        self.assertEqual(run.finished, None)
        self.assertEqual(self.initd_calls, [("forgectrl", "stop"), ("forgectrl", "start")])
        with open(self.sysfs + "cnc/disable") as f:
            self.assertEqual(f.read(), "1")
        self.assertEqual(run.evidence["duty_after_restart"], cooling.IDLE_DUTY)
        self.assertIsNotNone(run.evidence["restart_idle_s"])

    def test_a_daemon_that_keeps_the_busy_start_airflow_fails(self):
        # The bench case: after a takeover's restart the exhaust ran at
        # cooldown duty on an idle machine for good.
        self.run_engine()
        self.after_start = (32768, 21639)
        with self.assertRaises(Failed) as cm:
            self.run_test()
        self.assertIn("kept the busy-start airflow", str(cm.exception))
        self.assertIn("32768", str(cm.exception))

    def test_fans_left_running_fail_with_the_reference_in_the_message(self):
        def on_command(line):
            if line == "M8":
                self.duty(65535, 43278)
                self.fc.state["cool"]["phase"] = "run"
                self.fans(6753, 3212, 3328)
            # M9 ignored: the run profile stays on
        self.grbl.on_command = on_command
        run = Run("test", "cooling.fans-quiet-after-motion", "t")
        ctx = Context(run, None, helpers.make_test("cooling.fans-quiet-after-motion", []))
        with self.assertRaises(Failed) as cm:
            cooling.fans_quiet(ctx)
        self.assertIn("did not return to the idle profile", str(cm.exception))
        self.assertIn("idle reference", str(cm.exception))
        self.assertIn("65535", str(cm.exception))

    def test_a_reference_that_never_settles_fails_early_and_says_so(self):
        def churn():
            seq = (1000, 4000, 2500, 700, 3300, 1600, 4200)        # aperiodic against the sampler
            i = 0
            while not self.grbl._stop:
                v = seq[i % len(seq)]
                i += 1
                self.fans(v, v, v)
                time.sleep(0.03)
        threading.Thread(target=churn, daemon=True).start()
        run = Run("test", "cooling.fans-quiet-after-motion", "t")
        ctx = Context(run, None, helpers.make_test("cooling.fans-quiet-after-motion", []))
        with self.assertRaises(Failed) as cm:
            cooling.fans_quiet(ctx)
        self.assertIn("never settled to an idle reference", str(cm.exception))
        self.assertEqual(self.grbl.commands, [])         # nothing was jogged


if __name__ == "__main__":
    unittest.main()


class GateOffTests(EngineLog, unittest.TestCase):
    """cooling.gate-off against a scripted engine: the fake forgectrl
    re-reads the ceiling at every M8 (as the engine reloads its tunables
    at run start), trips OVERTEMP when the coolant is over it, skips the
    gate and reports gates_off when the ceiling sits at its top, and
    writes the run-start line the test looks for."""

    TOP, BOTTOM = 60.0, 5.0

    def setUp(self):
        self.fc = helpers.FakeForgectrl().start()
        self.log_setup()
        self.grbl = FakeGrbl()
        self.saved = (cooling.VERDICT_WAIT_S, cooling.SESSION_END_WAIT_S)
        cooling.VERDICT_WAIT_S = 3
        cooling.SESSION_END_WAIT_S = 3
        self.fc.state["status"] = dict(self.fc.state["status"],
                                       coolant={"down_c": 22.4, "up_c": 22.3, "pump": True, "tec": False},
                                       temps={"chassis_c": 29.0, "supply_raw": 589, "soc_c": 42.8,
                                              "soc_throttle": 0},
                                       gates_off=[])
        self.fc.state["cool"] = {"phase": "idle", "verdict": "OK", "fire_ok": False, "hold": False,
                                 "gates_off": []}
        self.fc.state["settings"].update({"cool_temp_max": "", "cool_temp_resume": ""})
        self.log_line = True        # the engine writes its run-start line
        self.temps_line = True      # the engine writes its run-end temperature line
        self.report_off = True      # the engine reports the off gate
        self.trips = True           # the engine trips a low ceiling
        self.sessions = 0           # run sessions the engine saw (M8 with the phase not run)
        self._describe()
        self.grbl.on_command = self._engine
        self.fc.on_post = self._on_post

    def tearDown(self):
        cooling.VERDICT_WAIT_S, cooling.SESSION_END_WAIT_S = self.saved
        self.grbl.close()
        self.fc.stop()
        self.log_teardown()

    # -- the scripted machine --------------------------------------------------
    def ceiling(self):
        v = self.fc.state["settings"].get("cool_temp_max") or ""
        return float(v) if v else 33.0

    def _describe(self):
        """/settings carries the gates table the way forgectrl's gates.c
        publishes it, classified from the stored value."""
        v = self.ceiling()
        state = "off" if v >= self.TOP else ("ok" if 25 <= v <= 38 else "warn")
        self.fc.state["settings"]["gates"] = {
            "cool_temp_max": {"gate": "coolant_max", "def": 33, "lo": self.BOTTOM, "hi": self.TOP,
                              "band": [25, 38], "off": "high", "value": v, "state": state}}

    def _on_post(self, path, form):
        """The real settings reply re-classifies the gates from the new values."""
        if path != "/settings":
            return None
        self.fc.state["settings"].update(form)
        self._describe()
        return (200, self.fc.state["settings"])

    def _engine(self, line):
        """M8 opens a run session: the engine re-reads the ceiling and
        ticks the gate. M9 ends it a report period later (the phase
        leaves run); a hold taken against the old ceiling stands until
        the next session re-reads."""
        self._describe()
        cool = self.fc.state["cool"]
        if line == "M9":
            def end():
                time.sleep(0.3)
                cool["phase"] = "smoke"
                if self.temps_line:
                    self.emit(
                        "Aug 22 12:00:30 forgectrl: cool: temps this job: chassis 29.0..29.4 C, "
                        "soc 42.8..47.1 C, supply raw 587..592\n")
                time.sleep(0.2)
                cool["phase"] = "idle"
            threading.Thread(target=end, daemon=True).start()
            return
        if line != "M8":
            return
        self.sessions += 1
        cool["phase"] = "run"
        v = self.ceiling()
        off = v >= self.TOP
        if off and self.log_line:
            self.emit(
                "Aug 21 12:00:00 forgectrl: cool: gate coolant_max OFF: cool_temp_max = 60 "
                "(the high end of 5 to 60; recommended 25 to 38, default 33)\n")
        gates_off = ["coolant_max"] if off and self.report_off else []
        if not off and self.trips and 22.3 > v:
            cool.update(verdict="OVERTEMP", fire_ok=False, hold=True, gates_off=gates_off)
        else:
            cool.update(verdict="OK", fire_ok=True, hold=False, gates_off=gates_off)
        self.fc.state["status"]["gates_off"] = gates_off

    def run_test(self):
        run = Run("test", "cooling.gate-off", "t")
        ctx = Context(run, None, helpers.make_test("cooling.gate-off", []))
        cooling.gate_off(ctx)
        return run

    def settings_posts(self):
        return [f for p, f in self.fc.posts if p == "/settings"]

    # -- cases ------------------------------------------------------------------
    def test_trip_then_off_then_restored_passes(self):
        run = self.run_test()
        self.assertEqual(run.evidence["trip"]["verdict"], "OVERTEMP")
        self.assertEqual(run.evidence["off"]["gates_off"], ["coolant_max"])
        self.assertEqual(run.evidence["restored"]["verdict"], "OK")
        posts = self.settings_posts()
        self.assertEqual(posts[0], {"cool_temp_max": "6.0", "cool_temp_resume": "5.0"})
        self.assertEqual(posts[1], {"cool_temp_max": "60.0", "cool_temp_resume": ""})
        self.assertEqual(posts[-1], {"cool_temp_max": "", "cool_temp_resume": ""})
        self.assertEqual(self.fc.state["settings"]["cool_temp_max"], "")
        self.assertEqual(self.grbl.commands.count("M8"), 3)
        self.assertEqual(self.grbl.commands.count("M9"), 3)

    def test_an_engine_that_does_not_trip_fails_and_restores(self):
        self.trips = False
        with self.assertRaises(Failed) as cm:
            self.run_test()
        self.assertIn("did not trip", str(cm.exception))
        self.assertEqual(self.settings_posts()[-1], {"cool_temp_max": "", "cool_temp_resume": ""})
        self.assertEqual(self.fc.state["settings"]["cool_temp_max"], "")
        # The restore cycles a run session so the engine re-reads the
        # restored values; the bench is not left holding on the test's.
        self.assertEqual(self.grbl.commands.count("M8"), 2)
        self.assertEqual(self.grbl.commands.count("M9"), 2)
        self.assertEqual(self.fc.state["cool"]["verdict"], "OK")

    def test_every_session_waits_for_the_previous_one_to_end(self):
        """Each M8 must find the engine out of phase run, or the engine
        never re-reads: the bench failure behind this case sent M9 and
        the next M8 300 ms apart and the 1 Hz report pipeline swallowed
        the session end."""
        seen = []
        inner = self._engine

        def engine(line):
            if line == "M8":
                seen.append(self.fc.state["cool"]["phase"])
            inner(line)
        self.grbl.on_command = engine
        self.run_test()
        self.assertEqual(seen, ["idle", "idle", "idle"])
        self.assertEqual(self.sessions, 3)

    def test_an_engine_that_hides_the_off_gate_fails(self):
        self.report_off = False
        with self.assertRaises(Failed) as cm:
            self.run_test()
        self.assertIn("gates_off", str(cm.exception))
        self.assertEqual(self.fc.state["settings"]["cool_temp_max"], "")

    def test_a_missing_run_start_log_line_fails(self):
        self.log_line = False
        with self.assertRaises(Failed) as cm:
            self.run_test()
        self.assertIn("run-start log line", str(cm.exception))
        self.assertEqual(self.fc.state["settings"]["cool_temp_max"], "")

    def test_an_earlier_runs_line_is_not_this_runs(self):
        # The off gate's line from a run before this one is in the log, and
        # the engine writes none this time: the line judged is one written
        # after the test's own mark, so the old one does not pass for it.
        self.emit("Aug 21 11:00:00 forgectrl: cool: gate coolant_max OFF: cool_temp_max = 60 "
                  "(the high end of 5 to 60; recommended 25 to 38, default 33)\n")
        self.log_line = False
        with self.assertRaises(Failed) as cm:
            self.run_test()
        self.assertIn("run-start log line", str(cm.exception))
        self.assertEqual(self.fc.state["settings"]["cool_temp_max"], "")

    def test_a_missing_run_end_temperature_line_fails(self):
        self.temps_line = False
        with self.assertRaises(Failed) as cm:
            self.run_test()
        self.assertIn("board-temperature line", str(cm.exception))

    def test_a_custom_ceiling_is_restored_verbatim(self):
        self.fc.state["settings"].update({"cool_temp_max": "30", "cool_temp_resume": "28"})
        self._describe()
        run = self.run_test()
        self.assertEqual(run.evidence["orig"], {"cool_temp_max": "30", "cool_temp_resume": "28"})
        self.assertEqual(self.settings_posts()[-1], {"cool_temp_max": "30", "cool_temp_resume": "28"})
        self.assertEqual(self.fc.state["settings"]["cool_temp_max"], "30")


class CriticalTierTests(EngineLog, unittest.TestCase):
    """cooling.critical-tier against a scripted engine: two tiers on the
    coolant, the fail tier winning over the pause tier in a run session
    and ending with it, the critical line off at its top, and the settings
    cross-check refusing a critical line at or below the ceiling."""

    UP = 22.3
    ROWS = {  # key: (gate, default, lo, hi, band_lo, band_hi, off)
        "cool_temp_max": ("coolant_max", 33.0, 5.0, 60.0, 25.0, 38.0, "high"),
        "cool_temp_resume": (None, 31.0, 5.0, 59.0, 20.0, 36.0, "none"),
        "cool_temp_critical_c": ("coolant_critical", 38.0, 6.0, 70.0, 36.0, 45.0, "high"),
    }

    def setUp(self):
        self.fc = helpers.FakeForgectrl().start()
        self.log_setup()
        self.grbl = FakeGrbl()
        self.saved = (cooling.VERDICT_WAIT_S, cooling.SESSION_END_WAIT_S)
        cooling.VERDICT_WAIT_S = 3
        cooling.SESSION_END_WAIT_S = 3
        self.fc.state["status"] = dict(self.fc.state["status"],
                                       coolant={"down_c": 22.4, "up_c": self.UP, "pump": True, "tec": False},
                                       gates_off=[])
        self.fc.state["cool"] = {"phase": "idle", "verdict": "OK", "fire_ok": False, "hold": False,
                                 "gates_off": []}
        for k in self.ROWS:
            self.fc.state["settings"].setdefault(k, "")
        self.fc.state["logs_tail"] = {"text": ""}
        self.faults = True          # the engine has the critical tier
        self.ends_with_session = True
        self.cross_checks = True    # the settings API refuses crit <= max
        self._describe()
        self.grbl.on_command = self._engine
        self.fc.on_post = self._on_post

    def tearDown(self):
        cooling.VERDICT_WAIT_S, cooling.SESSION_END_WAIT_S = self.saved
        self.grbl.close()
        self.fc.stop()
        self.log_teardown()

    def setting(self, key):
        v = self.fc.state["settings"].get(key) or ""
        return float(v) if v else self.ROWS[key][1]

    def _describe(self):
        gates = {}
        for k, (gate, d, lo, hi, blo, bhi, off) in self.ROWS.items():
            v = self.setting(k)
            state = "off" if (off == "high" and v >= hi) else ("ok" if blo <= v <= bhi else "warn")
            gates[k] = {"gate": gate, "def": d, "lo": lo, "hi": hi, "band": [blo, bhi], "off": off,
                        "value": v, "state": state}
        self.fc.state["settings"]["gates"] = gates

    def _on_post(self, path, form):
        if path != "/settings":
            return None
        if self.cross_checks:
            merged = dict(self.fc.state["settings"])
            merged.update(form)
            tmax = float(merged.get("cool_temp_max") or 33.0)
            tcrit = float(merged.get("cool_temp_critical_c") or 38.0)
            ceiling_off = tmax >= self.ROWS["cool_temp_max"][3]
            if not ceiling_off and tcrit <= tmax:
                return (400, {"error": "cool_temp_critical_c must be above cool_temp_max"})
        self.fc.state["settings"].update(form)
        self._describe()
        return (200, self.fc.state["settings"])

    def _verdict_idle(self):
        """Outside a run only the ceiling's pause tier stands."""
        return "OVERTEMP" if self.UP > self.setting("cool_temp_max") else "OK"

    def _engine(self, line):
        self._describe()
        cool = self.fc.state["cool"]
        if line == "M9":
            def end():
                time.sleep(0.3)
                cool["phase"] = "idle"
                if self.ends_with_session or cool["verdict"] != "CRITICAL":
                    v = self._verdict_idle()
                    cool.update(verdict=v, fire_ok=v == "OK", hold=v != "OK", reason="")
            threading.Thread(target=end, daemon=True).start()
            return
        if line != "M8":
            return
        cool["phase"] = "run"
        tmax, tcrit = self.setting("cool_temp_max"), self.setting("cool_temp_critical_c")
        crit_off = tcrit >= self.ROWS["cool_temp_critical_c"][3]
        off = ["coolant_critical"] if crit_off else []
        if crit_off:
            self.emit(
                "Aug 22 12:00:00 forgectrl: cool: gate coolant_critical OFF: cool_temp_critical_c = 70 "
                "(the high end of 6 to 70; recommended 36 to 45, default 38)\n")
        if self.faults and not crit_off and self.UP >= tcrit:
            cool.update(verdict="CRITICAL", fire_ok=False, hold=True, resume_ok=False,
                        reason="CRITICAL: coolant %.1f C at or over the %.0f C critical line - hold, "
                               "no resume this job" % (self.UP, tcrit), gates_off=off)
        elif self.UP > tmax:
            cool.update(verdict="OVERTEMP", fire_ok=False, hold=True, resume_ok=False,
                        reason="coolant over the ceiling", gates_off=off)
        else:
            cool.update(verdict="OK", fire_ok=True, hold=False, resume_ok=True, reason="", gates_off=off)
        self.fc.state["status"]["gates_off"] = off

    def run_test(self):
        run = Run("test", "cooling.critical-tier", "t")
        ctx = Context(run, None, helpers.make_test("cooling.critical-tier", []))
        cooling.critical_tier(ctx)
        return run

    def settings_posts(self):
        return [f for p, f in self.fc.posts if p == "/settings"]

    # -- cases ------------------------------------------------------------------
    def test_fault_then_off_then_restored_passes(self):
        run = self.run_test()
        self.assertEqual(run.evidence["cross_check"]["status"], 400)
        self.assertEqual(run.evidence["critical"]["verdict"], "CRITICAL")
        self.assertEqual(run.evidence["critical_after"]["verdict"], "OVERTEMP")
        self.assertEqual(run.evidence["critical_off"]["gates_off"], ["coolant_critical"])
        self.assertEqual(run.evidence["restored"]["verdict"], "OK")
        self.assertEqual(run.evidence["ceiling_off_accepted"], 200)
        posts = self.settings_posts()
        # refused cross-check, the ceiling at its off end (accepted), its undo, then the low leg
        self.assertEqual(posts[1], {"cool_temp_max": "60.0"})
        self.assertEqual(posts[3], {"cool_temp_max": "6.0", "cool_temp_resume": "5.0",
                                    "cool_temp_critical_c": "7.0"})
        self.assertEqual(posts[-1], {"cool_temp_max": "", "cool_temp_resume": "", "cool_temp_critical_c": ""})
        self.assertEqual(self.grbl.commands.count("M8"), 3)
        self.assertEqual(self.grbl.commands.count("M9"), 3)

    def test_an_engine_without_the_tier_fails_and_restores(self):
        self.faults = False
        with self.assertRaises(Failed) as cm:
            self.run_test()
        self.assertIn("did not fault", str(cm.exception))
        self.assertEqual(self.settings_posts()[-1],
                         {"cool_temp_max": "", "cool_temp_resume": "", "cool_temp_critical_c": ""})
        self.assertEqual(self.fc.state["cool"]["verdict"], "OK")

    def test_a_fault_that_outlives_its_session_fails(self):
        self.ends_with_session = False
        with self.assertRaises(Failed) as cm:
            self.run_test()
        self.assertIn("after the faulted session", str(cm.exception))

    def test_a_missing_cross_check_fails_before_any_session(self):
        self.cross_checks = False
        with self.assertRaises(Failed) as cm:
            self.run_test()
        self.assertIn("was accepted", str(cm.exception))
        self.assertEqual(self.grbl.commands.count("M8"), 0)


class FanGateTests(unittest.TestCase):
    """cooling.fan-gate-trips against a scripted engine: the fake reads the
    floors at every M8 (the engine reloads at run start), holds every fan
    in grace for the configured seconds, then judges the bench readings
    (exhaust 6753, intakes 3212/3328, air assist 10997 rpm, purge 628)
    against the floors: a floor a reading cannot meet trips AIRFLOW three
    ticks after the grace, a floor of zero reads off."""

    READINGS = {"exhaust": 6753, "intake_1": 3212, "intake_2": 3328, "air_assist": 10997, "purge": 628}
    FLOORS = {"exhaust": ("cool_tach_exhaust_min_rpm", 3700.0, 20000.0),
              "intake_1": ("cool_tach_intake_min_rpm", 1800.0, 20000.0),
              "intake_2": ("cool_tach_intake_min_rpm", 1800.0, 20000.0),
              "air_assist": ("cool_tach_air_assist_min_rpm", 6000.0, 30000.0),
              "purge": ("cool_purge_min_current", 300.0, 1023.0)}
    GATE_OF = {"exhaust": "exhaust", "intake_1": "intake", "intake_2": "intake",
               "air_assist": "air_assist", "purge": "purge"}

    def setUp(self):
        self.fc = helpers.FakeForgectrl().start()
        self.grbl = FakeGrbl()
        self.saved = (cooling.VERDICT_WAIT_S, cooling.SESSION_END_WAIT_S, cooling.FAN_TRIP_WAIT_S)
        cooling.VERDICT_WAIT_S = 4
        cooling.SESSION_END_WAIT_S = 3
        cooling.FAN_TRIP_WAIT_S = 4
        self.fc.state["status"] = dict(self.fc.state["status"], gates_off=[])
        self.fc.state["cool"] = {"phase": "idle", "verdict": "OK", "fire_ok": False, "hold": False,
                                 "gates_off": [], "fan_gates": {}}
        for key, _d, _h in self.FLOORS.values():
            self.fc.state["settings"].setdefault(key, "")
        self.fc.state["settings"].setdefault("cool_fan_grace_s", "")
        self.trips = True          # the engine trips an unmeetable floor
        self.reports_off = True    # the engine reports a zero floor as off
        self.grace_scale = 0.1     # seconds of fake grace per configured second
        self._describe()
        self.grbl.on_command = self._engine
        self.fc.on_post = self._on_post

    def tearDown(self):
        cooling.VERDICT_WAIT_S, cooling.SESSION_END_WAIT_S, cooling.FAN_TRIP_WAIT_S = self.saved
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
                          "band": [default * 0.7, default * 1.4], "off": "low", "value": v,
                          "state": "off" if v <= 0 else "ok"}
        g = self.setting("cool_fan_grace_s", 15.0)
        gates["cool_fan_grace_s"] = {"gate": None, "def": 15.0, "lo": 0.0, "hi": 120.0, "band": [5, 30],
                                     "off": "none", "value": g, "state": "ok"}
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
                # the fan fault is the session's: it ends with it
                cool.update(verdict="OK", fire_ok=True, hold=False, reason="")
                for g in cool.get("fan_gates", {}).values():
                    g["state"] = "idle"
            threading.Thread(target=end, daemon=True).start()
            return
        if line != "M8":
            return
        cool["phase"] = "run"
        floors = {fan: self.setting(key, default) for fan, (key, default, _h) in self.FLOORS.items()}
        off = sorted({self.GATE_OF[f] for f, v in floors.items() if v <= 0}) if self.reports_off else []
        cool.update(verdict="OK", fire_ok=True, hold=False, resume_ok=True, reason="", gates_off=off,
                    fan_gates={f: {"reading": self.READINGS[f], "floor": floors[f],
                                   "state": "off" if floors[f] <= 0 else "grace"} for f in floors})
        self.fc.state["status"]["gates_off"] = off
        grace = self.setting("cool_fan_grace_s", 15.0) * self.grace_scale

        def judge():
            time.sleep(grace)
            if cool["phase"] != "run":
                return
            for f in floors:
                if floors[f] > 0:
                    cool["fan_gates"][f]["state"] = "ok" if self.READINGS[f] >= floors[f] else "under"
            time.sleep(0.3)
            if cool["phase"] != "run" or not self.trips:
                return
            for f in floors:
                if 0 < floors[f] > self.READINGS[f]:
                    cool["fan_gates"][f]["state"] = "TRIPPED"
                    cool.update(verdict="AIRFLOW", fire_ok=False, hold=True, resume_ok=False,
                                reason="AIRFLOW: %s %d under the %d floor for 3 s - hold, no resume this job"
                                       % (f, self.READINGS[f], floors[f]))
                    break
        threading.Thread(target=judge, daemon=True).start()

    def run_test(self):
        run = Run("test", "cooling.fan-gate-trips", "t")
        ctx = Context(run, None, helpers.make_test("cooling.fan-gate-trips", []))
        cooling.fan_gate_trips(ctx)
        return run

    def settings_posts(self):
        return [f for p, f in self.fc.posts if p == "/settings"]

    def test_trip_purge_off_and_restore_pass(self):
        run = self.run_test()
        ev = run.evidence
        self.assertEqual(ev["exhaust_trip"]["cool"]["verdict"], "AIRFLOW")
        self.assertEqual(ev["exhaust_trip"]["gate"]["state"], "TRIPPED")
        self.assertEqual(ev["purge_trip"]["gate"]["state"], "TRIPPED")
        self.assertEqual(ev["exhaust_off"]["cool"]["gates_off"], ["exhaust"])
        self.assertTrue(all(g["state"] == "ok" for g in ev["restored"].values()))
        posts = self.settings_posts()
        self.assertEqual(posts[0], {"cool_tach_exhaust_min_rpm": "20000.0", "cool_fan_grace_s": "8"})
        self.assertEqual(posts[-1], {"cool_tach_exhaust_min_rpm": "", "cool_purge_min_current": "",
                                     "cool_fan_grace_s": ""})
        self.assertEqual(self.grbl.commands.count("M8"), 4)
        self.assertEqual(self.grbl.commands.count("M9"), 4)

    def test_an_engine_that_does_not_trip_fails_and_restores(self):
        self.trips = False
        with self.assertRaises(Failed) as cm:
            self.run_test()
        self.assertIn("did not trip", str(cm.exception))
        self.assertEqual(self.settings_posts()[-1], {"cool_tach_exhaust_min_rpm": "", "cool_purge_min_current": "",
                                                     "cool_fan_grace_s": ""})
        self.assertEqual(self.fc.state["cool"]["verdict"], "OK")

    def test_an_engine_that_hides_an_off_floor_fails(self):
        self.reports_off = False
        with self.assertRaises(Failed) as cm:
            self.run_test()
        self.assertIn("lacks exhaust", str(cm.exception))
        self.assertEqual(self.fc.state["settings"]["cool_tach_exhaust_min_rpm"], "")


class FlowVerdictLogTests(unittest.TestCase):
    """cooling.flow-under-load finds the engine's verdict in what the log
    gained since the job began, not in a count over a tail of fixed length:
    there a new verdict line comes in at the bottom as an old one leaves at
    the top, the count does not move, and a check that verified reads as
    one that never judged."""

    VERDICT = ("2026-09-19T21:37:46.869674+00:00 forgectrl[2796] INFO cool: coolant flow verified "
               "(heater rise 11.5 C, dT 9.5 C; laser 1.6 off 13.1)\n")
    OLD = ("2026-09-19T21:17:27.200840+00:00 forgectrl[965] INFO cool: coolant flow verified "
           "(heater rise 9.5 C, dT 8.7 C)\n")

    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="flowlog.")
        self.addCleanup(shutil.rmtree, self.dir, ignore_errors=True)
        self.path = os.path.join(self.dir, "forgectrl.log")

    def write(self, text, mode="w"):
        with open(self.path, mode, newline="\n") as f:
            f.write(text)

    def test_only_the_text_after_the_offset_comes_back(self):
        self.write(self.OLD + "filler\n" * 400)
        off = os.path.getsize(self.path)
        self.assertEqual(cooling._log_since(self.path, off), "")
        self.write(self.VERDICT, mode="a")
        text = cooling._log_since(self.path, off)
        self.assertEqual(text, self.VERDICT)
        m = cooling._load_verdict_rx().search(text)
        self.assertEqual(m.group(1), "coolant flow verified")
        self.assertEqual((m.group(2), m.group(5)), ("11.5", "1.6"))

    def test_an_old_verdict_before_the_offset_is_not_the_new_one(self):
        self.write(self.OLD)
        off = os.path.getsize(self.path)
        self.write("2026-09-19T21:36:40+00:00 forgectrl[2796] INFO cool: crash watch armed\n", mode="a")
        self.assertIsNone(cooling._load_verdict_rx().search(cooling._log_since(self.path, off)))

    def test_a_log_rotated_under_the_test_is_read_whole(self):
        self.write(self.OLD + "filler\n" * 400)
        off = os.path.getsize(self.path)
        self.write(self.VERDICT)                 # the rotation: a new, shorter file
        self.assertEqual(cooling._log_since(self.path, off), self.VERDICT)

    def test_a_missing_log_reads_as_nothing(self):
        self.assertEqual(cooling._log_since(os.path.join(self.dir, "none.log"), 0), "")
