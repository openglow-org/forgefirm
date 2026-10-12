# Copyright 2026 514 LLC d/b/a OpenGlow
# Written by Scott Wiederhold
# https://community.openglow.org
# SPDX-License-Identifier:    MIT

"""The setup's two burns in the catalog: two live tests on the button with
a scrap at hand, their covers naming the live runner, the renderer, the
sender, the page, and the driver's laser module; and each burn against a
scripted daemon: the preview and the program read, the start, the
prompts answered in order (the gantry stops, the head at home, the scrap,
its thickness in the machine's units, the arm, the machine's own press
prompt, the pick), the witnesses judged, the record read at version 1,
the setting the burn wrote put back as found, and for the focus burn the
controller started again on the edge and the window put back. The floor
starts only with Focus on the record and a scrap the lens reaches."""
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(__file__))
import helpers  # noqa: E402
from forgetest import catalog  # noqa: E402
from forgetest.runner import Aborted, Context, Failed, Run  # noqa: E402
from forgetest.suite import setup_burns, setup_dark  # noqa: E402

LIVE = ["laser.focus", "laser.floor"]
EMISSION = {"hv_max": 610, "laser_on_samples": 900, "thermopile_delta": 600, "beam_sensor_seen": True,
            "lit_s": 30.0}
SPM = 2.922                     # the lens screw, half-steps per mm ($102)
EDGE_DEFAULT = 3.35             # the edge's height while the setting is unset (lens.h)


def lens_block(s):
    """The /status lens block as the daemon builds it from the settings: a
    stop count from 1 to 40 stands, anything else is the fallback."""
    def count(key, fallback):
        try:
            n = float(s.get(key, ""))
        except ValueError:
            return fallback, False
        return (int(n), True) if 1 <= n <= 40 else (fallback, False)
    try:
        edge = float(s.get("lens_hall_edge_z_mm", ""))
    except ValueError:
        edge = EDGE_DEFAULT
    below, a = count("lens_stop_below_steps", 10)
    above, b = count("lens_stop_above_steps", 12)
    return {"edge_z": round(edge, 2), "below": below, "above": above, "stops_found": a and b,
            "reach_min": round(edge - below / SPM, 2), "reach_max": round(edge + above / SPM, 2)}


def ladder_z(edge, below, above):
    """The focus ladder the daemon builds: twelve whole half-steps from
    the top of the window to the bottom."""
    return [round(edge + round(above - (above + below) * i / 11) / SPM, 2) for i in range(12)]


# Each burn as the daemon runs it: its prompts in order (id, kind,
# options) and the result it ends with. The press prompt is the
# machine's own: nothing answers it, the discharge closes it. The focus
# burn's window and ladder are the ones the settings held at its start.
SETUP = [("gantry-stops", "confirm", ["Yes", "No"]),
         ("home", "choice", ["The head is home", "Release the motors"]),
         ("scrap", "continue", ["Continue"]), ("thickness", "number", ["Set"])]
PRESS = ("press", "wait", [])
SCRIPT = {
    "laser.focus": (SETUP + [("focus-arm", "continue", ["Continue"]), PRESS,
                             ("focus-pick", "multichoice", [str(i) for i in range(1, 13)])],
                    {"z_passes": [100, 101], "emission": EMISSION, "pick": 11, "picked_lines": [11, 11],
                     "pick_half_steps": -8, "edge_z_mm": 6.96, "steps_per_mm": SPM, "max_height_mm": 13.81,
                     "focus_range_mm": {"min": 2.17, "max": 13.81}, "summary": "Line 11."}),
    "laser.floor": (SETUP + [("floor-arm", "continue", ["Continue"]), PRESS,
                             ("floor-pick", "choice", [str(2 * i) for i in range(1, 13)] + ["None"])],
                    {"z_passes": [100, 101], "emission": EMISSION, "faintest_density": 8.0,
                     "floor_density": 10.0, "summary": "The floor is 10."}),
}
# What each burn writes when it ends.
WRITES = {"laser.focus": {"lens_hall_edge_z_mm": "6.96"}, "laser.floor": {"laser_floor_density": "10"}}
# The machine as the burns find it: its own edge, the window the Lens
# travel check wrote, its floor.
FOUND = {"lens_hall_edge_z_mm": "4.10", "lens_stop_below_steps": "14", "lens_stop_above_steps": "20",
         "laser_floor_density": "12", "ui_units": "metric"}
# What grblHAL reads at its start: the edge and the window.
LENS_KEYS = ("lens_hall_edge_z_mm", "lens_stop_below_steps", "lens_stop_above_steps")
HOME = "The head is home"


class RegistrationTests(unittest.TestCase):
    def setUp(self):
        self.reg = catalog.load_suite()

    def test_two_live_tests_on_the_button_with_a_scrap_at_hand(self):
        for tid in ("setup.focus", "setup.floor"):
            t = self.reg[tid]
            self.assertEqual(t.kind, "live", tid)
            self.assertEqual(t.mode, "grbl", tid)
            self.assertEqual(t.hardware, "takeover", tid)
            self.assertEqual(list(t.actions), ["button"], tid)
            self.assertEqual(list(t.hands), ["scrap"], tid)
            self.assertFalse(t.fixture_runnable(("button", "lid", "interlock")), tid)
            self.assertIsNone(getattr(t, "precheck", None), tid)
        self.assertEqual(list(self.reg["setup.focus"].requires),
                         ["forgectrl.auth", "setup.check-motion", "laser.emission-witness", "setup.check-lens"])
        # the floor burns at the focus Focus measured, and refuses without it
        self.assertEqual(list(self.reg["setup.floor"].requires),
                         ["forgectrl.auth", "setup.check-motion", "laser.emission-witness", "setup.focus"])

    def test_the_sheet_is_gone(self):
        self.assertFalse([i for i in self.reg if i.startswith("setup.sheet")])

    def test_covers_the_live_runner_the_renderer_the_sender_the_page_and_the_driver(self):
        for tid in ("setup.focus", "setup.floor"):
            covers = set(self.reg[tid].covers)
            for want in (("forgectrl", "src/wizlive.*"), ("forgectrl", "src/sheet.*"),
                         ("forgectrl", "src/jobstream.*"), ("forgectrl", "src/curverec.*"),
                         ("forgectrl", "src/font_hershey.*"), ("forgectrl", "src/lens.*"),
                         ("forgectrl", "src/lenshome.*"),       # the lens back on its reference at the end
                         ("forgectrl", "src/ui/wizard.*"), ("forgectrl", "src/wizrun.h"),
                         ("grblhal-glowforge", "src/glowforge_laser.c")):
                self.assertIn(want, covers, "%s lacks %s" % (tid, want))

    def test_the_steps_ask_for_a_scrap_in_the_corner_and_one_press(self):
        for tid, size in (("setup.focus", "78 x 68 mm"), ("setup.floor", "86 x 68 mm")):
            text = " ".join(self.reg[tid].steps)
            self.assertIn(size, text, tid)
            self.assertIn("back-left corner", text, tid)
            self.assertIn("gantry stops", text, tid)
            self.assertIn("press the machine's button once", text, tid)


class ScriptedDaemon:
    """The wizard runner as the suite sees it: /wiz/<id>/start opens the
    first prompt (the floor ends at once, with the wizard's error, when the
    record carries no Focus), each answer opens the next (a thickness that
    is not a plain number, out of range in the machine's units, or, for
    the floor, outside the lens reach opens it again), the press prompt
    closes on the poll after the one that served it, the last answer ends
    the burn with its result and its writes, /wiz/dark reports it, GET /wiz
    carries the versions (the record carries Focus already, as the bench's
    does), the preview and the program are served per burn, the program
    ending in the wizard's tail, and the supervisor's stop and start levers
    work (a start records the settings the new controller reads)."""

    TAIL = ["G0 X0 Y0", "G92.1", "M2"]      # after the lens's return to the edge

    def __init__(self, fake):
        self.fake = fake
        fake.state["settings"].update(FOUND)
        self.dark = {"id": "", "running": False, "phase": "", "progress": 0, "elapsed_s": 0,
                     "log": [], "prompt": None, "result": None, "error": "",
                     "shots": {"lid": False, "head": False}}
        self.versions = {"laser.focus": 1}
        self.starts = []            # the lens settings each controller start read
        self.tail = list(self.TAIL)
        self.answers = {}           # wid -> [values]
        self.order = []             # the burns as started
        self.aborted = []
        self.settings_at_start = {}
        self.previews = []
        self.queue = []
        self.seq = 0
        self.press_seen = False
        self.thickness = None
        self.lens_at_start = None
        self.units = None           # the units the number prompts read; None = ui_units
        self.start_writes = {}      # wid -> settings the burn writes as it starts
        self.patch = {}             # wid -> result fields that differ from the script
        self.stray_z = None         # a Z the served program commands beyond the reach, when set
        fake.on_get = self.on_get
        fake.on_post = self.on_post

    def open_next(self):
        if not self.queue:
            return False
        pid, kind, options = self.queue[0]
        self.seq += 1
        self.dark["prompt"] = {"seq": self.seq, "id": pid, "kind": kind, "text": pid, "options": options}
        return True

    def finish(self, wid):
        s = self.fake.state["settings"]
        r = dict(SCRIPT[wid][1], thickness_mm=round(self.thickness, 3))
        if wid == "laser.focus":
            lens = self.lens_at_start
            r["window"] = {k: lens[k] for k in ("below", "above", "reach_min", "reach_max")}
            r["ladder_z"] = ladder_z(lens["edge_z"], lens["below"], lens["above"])
        r.update(self.patch.get(wid, {}))
        s.update(WRITES[wid])
        self.dark.update({"running": False, "prompt": None, "progress": 100, "result": r})
        self.versions[wid] = 1

    def on_get(self, path, q):
        s = self.fake.state["settings"]
        if path == "/status":
            # the lens block follows the settings, as the daemon's does
            return 200, dict(self.fake.state["status"], lens=lens_block(s))
        if path == "/wiz/dark":
            p = self.dark.get("prompt")
            if p and p["id"] == "press":
                if self.press_seen:
                    self.press_seen = False
                    self.queue.pop(0)
                    if not self.open_next():
                        self.finish(self.dark["id"])
                else:
                    self.press_seen = True
            return 200, dict(self.dark)
        if path == "/wiz":
            return 200, {"versions": dict(self.versions), "recommended": [], "completed": True}
        if path == "/wiz/sheet.svg":
            self.previews.append(q.get("card"))
            if q.get("card") not in LIVE:
                return 404, {"error": "no such card"}
            return 200, b"<svg xmlns='http://www.w3.org/2000/svg'></svg>", "image/svg+xml"
        if path == "/wiz/sheet.gcode":
            # the text is M4 at the mark dose, as the daemon serves it; the
            # head's Z is the edge, the tail goes back to it, and the focus
            # ladder spans the window the settings hold now
            lens = lens_block(s)
            lines = ["; mock", "G0 Z%.2f" % lens["edge_z"], "M4 S400", "G0 X12 Y12", "G1 X40 Y12 F3000"]
            if q.get("card") == "laser.focus":
                lines += ["G0 Z%.2f" % z for z in ladder_z(lens["edge_z"], lens["below"], lens["above"])]
            if self.stray_z is not None:
                lines.append("G0 Z%.2f" % self.stray_z)
            lines += ["M5", "G0 Z%.2f" % lens["edge_z"]] + self.tail
            return 200, ("\n".join(lines) + "\n").encode(), "text/plain"
        return None

    def on_post(self, path, form):
        s = self.fake.state["settings"]
        if path.startswith("/wiz/") and path.endswith("/start"):
            wid = path[len("/wiz/"):-len("/start")]
            if wid not in SCRIPT:
                return 404, {"error": "no such wizard"}
            self.order.append(wid)
            self.settings_at_start[wid] = dict(s)
            if wid == "laser.floor" and not self.versions.get("laser.focus"):
                # the floor burns at the focus Focus measured: no prompt, no motion
                self.dark.update({"id": wid, "running": False, "phase": "", "prompt": None, "result": None,
                                  "error": "run Focus first: the floor burns at the focus it measures"})
                return 200, {"started": True, "id": wid}
            s.update(self.start_writes.get(wid, {}))
            self.lens_at_start = lens_block(s)
            self.queue = list(SCRIPT[wid][0])
            self.press_seen = False
            self.dark.update({"id": wid, "running": True, "phase": "starting", "result": None, "error": ""})
            self.open_next()
            return 200, {"started": True, "id": wid}
        if path.startswith("/wiz/") and path.endswith("/answer"):
            wid = path[len("/wiz/"):-len("/answer")]
            p = self.dark.get("prompt")
            if not p or form.get("seq") != str(p["seq"]):
                return 409, {"error": "no such prompt is open"}
            value = form.get("value")
            self.answers.setdefault(wid, []).append(value)
            if p["id"] == "thickness":
                imperial = (self.units or s.get("ui_units")) == "imperial"
                try:
                    mm = float(value.strip()) * (25.4 if imperial else 1.0)
                except ValueError:
                    mm = -1                     # "1/8" or "3 mm": not a plain number
                lens = self.lens_at_start
                if not 0 < mm <= 30 or (wid == "laser.floor"
                                        and not lens["reach_min"] <= mm <= lens["reach_max"]):
                    self.open_next()            # the same prompt again, a new sequence number
                    return 200, {"ok": True}
                self.thickness = mm
            self.queue.pop(0)
            if not self.open_next():
                self.finish(wid)
            return 200, {"ok": True}
        if path.startswith("/wiz/") and path.endswith("/abort"):
            self.aborted.append(path[len("/wiz/"):-len("/abort")])
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
            self.starts.append({k: s.get(k) for k in LENS_KEYS})
            return 200, {"started": True}
        return None

    def lever_posts(self):
        return [p for p, _ in self.fake.posts if p.startswith("/controller/")]


class BurnRunTests(unittest.TestCase):
    def setUp(self):
        self.fake = helpers.FakeForgectrl().start()
        self.daemon = ScriptedDaemon(self.fake)
        self.poll = setup_dark.POLL_S
        setup_dark.POLL_S = 0.01

    def tearDown(self):
        setup_dark.POLL_S = self.poll
        self.fake.stop()

    def run_test(self, tid):
        t = catalog.load_suite()[tid]
        run = Run("test", t.id, t.title)
        run.unattended = True           # the ready gate passes; the arm cue is a notice
        t.fn(Context(run, None, t))
        return run

    def assert_found(self, keys=None):
        s = self.fake.state["settings"]
        for k, v in FOUND.items():
            if keys is None or k in keys:
                self.assertEqual(s.get(k), v, k)

    def test_the_focus_burn_in_the_lens_travel_window(self):
        run = self.run_test("setup.focus")
        d = self.daemon
        self.assertEqual(d.order, ["laser.focus"])
        self.assertEqual(d.answers["laser.focus"], ["Yes", HOME, "Continue", "3.2", "Continue", "11"])
        self.assertEqual(d.previews, ["laser.focus", "laser.focus"])   # before the burn, and after it
        # a fresh head's focus: the edge cleared, the window the Lens travel check's
        start = d.settings_at_start["laser.focus"]
        self.assertNotIn("lens_hall_edge_z_mm", start)
        self.assertEqual((start["lens_stop_below_steps"], start["lens_stop_above_steps"]), ("14", "20"))
        self.assertEqual(run.evidence["result"]["emission"]["hv_max"], 610)
        fw = run.evidence["focus_window"]
        self.assertEqual(fw["window"]["below"], 14)
        self.assertEqual(fw["window"]["above"], 20)
        self.assertEqual(fw["edge"], "6.96")
        self.assertEqual(fw["program_z_before"]["reach"], [-1.44, 10.19])     # the default edge
        self.assertEqual(fw["program_z_after"]["reach"], [2.17, 13.8])        # the edge the burn wrote
        self.assertEqual(len(fw["program_z_after"]["z"]), 14)                 # head, twelve lines, tail
        # the program served after the burn ends in the tail at the edge the burn wrote
        self.assertEqual(run.evidence["program_tail"]["laser.focus"], ["G0 Z6.96", "G0 X0 Y0", "G92.1", "M2"])
        self.assertEqual(run.evidence["settings_before"],
                         {k: FOUND[k] for k in ("lens_hall_edge_z_mm", "lens_stop_below_steps",
                                                "lens_stop_above_steps")})
        self.assert_found()                                                   # restored
        self.assert_restarted_on_found()

    def assert_restarted_on_found(self, found=None):
        """grblHAL reads the edge and the window only at its start: the
        hand-back is one stop and one start after the restore, the start
        reading what was found."""
        d = self.daemon
        self.assertEqual(d.lever_posts(), ["/controller/stop", "/controller/start"])
        self.assertEqual(d.starts, [found or {k: FOUND[k] for k in LENS_KEYS}])
        self.assertEqual(self.fake.state["mode"]["controller"], "running")
        self.assertEqual(self.fake.state["mode"]["pid"], 101)

    def test_the_focus_burn_in_the_fallback_window(self):
        # A head the Lens travel check has not measured: the settings hold
        # no window, so the ladder runs in the fallback and the burn leaves
        # the keys unset.
        s = self.fake.state["settings"]
        s.pop("lens_stop_below_steps")
        s.pop("lens_stop_above_steps")
        run = self.run_test("setup.focus")
        fw = run.evidence["focus_window"]
        self.assertEqual((fw["window"]["below"], fw["window"]["above"]), setup_dark.FALLBACK_WINDOW)
        self.assertNotIn("lens_stop_below_steps", s)
        self.assertNotIn("lens_stop_above_steps", s)
        self.assertEqual(s["lens_hall_edge_z_mm"], "4.10")
        self.assert_restarted_on_found({"lens_hall_edge_z_mm": "4.10", "lens_stop_below_steps": None,
                                        "lens_stop_above_steps": None})

    def test_the_floor_burn(self):
        run = self.run_test("setup.floor")
        d = self.daemon
        self.assertEqual(d.order, ["laser.floor"])
        self.assertEqual(d.answers["laser.floor"], ["Yes", HOME, "Continue", "3.2", "Continue", "8"])
        self.assertEqual(d.previews, ["laser.floor"])
        self.assertEqual(run.evidence["result"]["floor_density"], 10.0)
        self.assertEqual(run.evidence["settings_before"], {"laser_floor_density": "12"})
        self.assertEqual(run.evidence["focus_on_record"], 1)
        self.assertEqual(run.evidence["program_tail"]["laser.floor"], ["G0 Z4.10", "G0 X0 Y0", "G92.1", "M2"])
        self.assert_found()
        # the controller loads the floor at every arm: nothing to restart
        self.assertEqual(d.lever_posts(), [])

    def test_the_floor_needs_focus_on_the_record(self):
        # The floor burns at the focus Focus measured, and the wizard
        # refuses without it: the test says so before the scrap is asked
        # for, and nothing starts.
        self.daemon.versions = {}
        with self.assertRaises(Failed) as cm:
            self.run_test("setup.floor")
        self.assertIn("the record carries no Focus", str(cm.exception))
        self.assertEqual(self.daemon.order, [])
        self.assertEqual(self.daemon.previews, [])
        self.assert_found()

    def test_the_floors_own_refusal_without_focus_ends_the_run(self):
        # Past the test's own reading (a record that lost Focus in
        # between): the wizard's refusal ends the run with its word, before
        # any prompt.
        real = self.daemon.on_get

        def on_get(path, q):
            if path == "/wiz" and not self.daemon.order:
                return 200, {"versions": {"laser.focus": 1}, "recommended": [], "completed": True}
            return real(path, q)
        self.fake.on_get = on_get
        self.daemon.versions = {}
        with self.assertRaises(Failed) as cm:
            self.run_test("setup.floor")
        self.assertIn("run Focus first", str(cm.exception))
        self.assertEqual(self.daemon.order, ["laser.floor"])
        self.assertEqual(self.daemon.answers, {})
        self.assert_found()

    def test_a_scrap_the_lens_does_not_reach_fails_before_the_floor(self):
        # An edge high enough that the lens cannot focus 3.2 mm above the
        # tray: the floor would refuse the thickness, so nothing starts.
        self.fake.state["settings"]["lens_hall_edge_z_mm"] = "9.50"
        with self.assertRaises(Failed) as cm:
            self.run_test("setup.floor")
        self.assertIn("leaves out the 3.2 mm scrap", str(cm.exception))
        self.assertEqual(self.daemon.order, [])

    def test_a_program_that_keeps_its_g92_origin_fails_before_the_burn(self):
        # The tail clears the G92 origin, which the controller would
        # otherwise carry to its next start under a sender's own offsets.
        self.daemon.tail = ["G0 X0 Y0", "M2"]
        with self.assertRaises(Failed) as cm:
            self.run_test("setup.floor")
        self.assertIn("not the tail", str(cm.exception))
        self.assertIn("G92.1", str(cm.exception))
        self.assertEqual(self.daemon.order, [])
        self.assert_found()

    def test_imperial_units_answer_the_thickness_in_inches(self):
        # The number prompts read ui_units: on an imperial machine the
        # thickness is 0.125 in and the record carries 3.175 mm.
        self.fake.state["settings"]["ui_units"] = "imperial"
        run = self.run_test("setup.floor")
        self.assertEqual(self.daemon.answers["laser.floor"][3], "0.125")
        self.assertEqual(run.evidence["thickness_answer"], "0.125")
        self.assertEqual(run.evidence["result"]["thickness_mm"], 3.175)

    def test_a_thickness_the_burn_refuses_ends_it_before_anything_fires(self):
        # 3.2 read as inches is over the 30 mm cap, and the burn asks again:
        # the test ends the burn rather than answer the same thing forever.
        self.daemon.units = "imperial"
        with self.assertRaises(Failed) as cm:
            self.run_test("setup.floor")
        self.assertIn("asked thickness again", str(cm.exception))
        self.assertEqual(self.daemon.answers["laser.floor"], ["Yes", HOME, "Continue", "3.2"])
        self.assertEqual(self.daemon.aborted, ["laser.floor"])
        self.assert_found()

    def test_a_prompt_the_test_does_not_answer_ends_the_burn(self):
        # The motors found released: the burn asks for the head pushed home
        # by hand, which the bench never needs.
        SCRIPT["laser.floor"][0].insert(1, ("home-push", "continue", ["Continue"]))
        try:
            with self.assertRaises(Failed) as cm:
                self.run_test("setup.floor")
        finally:
            SCRIPT["laser.floor"][0].pop(1)
        self.assertIn("asked home-push, which the test does not answer", str(cm.exception))
        self.assertEqual(self.daemon.aborted, ["laser.floor"])
        self.assert_found()

    def test_a_dark_burn_fails_and_still_restores(self):
        self.daemon.patch["laser.floor"] = {"emission": {"hv_max": 0, "laser_on_samples": 0, "thermopile_delta": 0}}
        with self.assertRaises(Failed) as cm:
            self.run_test("setup.floor")
        self.assertIn("the tube current never rose", str(cm.exception))
        self.assert_found()

    def test_a_burn_with_a_silent_beam_sensor_passes(self):
        # A head whose beam sensor gives no output: the tube current and
        # LASER_ON saw the beam, so the burn counts.
        self.daemon.patch["laser.floor"] = {"emission": dict(EMISSION, thermopile_delta=0,
                                                             beam_sensor_seen=False)}
        self.run_test("setup.floor")
        self.assert_found()

    def test_a_burn_that_does_not_record_the_beam_sensor_fails(self):
        self.daemon.patch["laser.floor"] = {"emission": {"hv_max": 610, "laser_on_samples": 900}}
        with self.assertRaises(Failed) as cm:
            self.run_test("setup.floor")
        self.assertIn("the result does not record the beam sensor", str(cm.exception))
        self.assert_found()

    def test_a_focus_burn_that_writes_the_window_fails(self):
        # The focus burn writes the edge alone: the window is the Lens
        # travel check's. A burn that moves it fails, and it goes back.
        self.daemon.start_writes["laser.focus"] = {"lens_stop_below_steps": "16", "lens_stop_above_steps": "22"}
        with self.assertRaises(Failed) as cm:
            self.run_test("setup.focus")
        self.assertIn("the focus burn wrote the lens window", str(cm.exception))
        self.assert_found()
        self.assert_restarted_on_found()            # a failed run hands back the controller too

    def test_an_aborted_focus_burn_still_restarts_the_controller_on_the_settings_as_found(self):
        # The run aborted at the arm press: the burn is ended, the settings
        # go back, and the controller starts again on them.
        t = catalog.load_suite()["setup.focus"]
        run = Run("test", t.id, t.title)
        run.unattended = True
        real = self.daemon.on_get

        def on_get(path, q):
            p = self.daemon.dark.get("prompt")
            if path == "/wiz/dark" and p and p["id"] == "press":
                run.aborted.set()
            return real(path, q)
        self.fake.on_get = on_get
        with self.assertRaises(Aborted):
            t.fn(Context(run, None, t))
        self.assertEqual(self.daemon.aborted, ["laser.focus"])
        self.assert_found()
        self.assert_restarted_on_found()

    def test_a_focus_ladder_outside_the_window_held_fails(self):
        self.daemon.patch["laser.focus"] = {"window": {"below": 10, "above": 12, "reach_min": -0.07,
                                                       "reach_max": 7.46}}
        with self.assertRaises(Failed) as cm:
            self.run_test("setup.focus")
        self.assertIn("the ladder ran in 10/12", str(cm.exception))
        self.assert_found()

    def test_a_focus_burn_that_reports_a_stop_search_fails(self):
        self.daemon.patch["laser.focus"] = {"stops": {"found": True, "below": 14, "above": 20}}
        with self.assertRaises(Failed) as cm:
            self.run_test("setup.focus")
        self.assertIn("searched for the stops", str(cm.exception))
        self.assert_found()

    def test_a_program_z_beyond_the_reach_fails_before_the_burn(self):
        # A served program that commands a Z the lens does not reach is
        # refused before the burn starts: nothing burns.
        self.daemon.stray_z = 12.6
        with self.assertRaises(Failed) as cm:
            self.run_test("setup.floor")
        self.assertIn("outside the lens window", str(cm.exception))
        self.assertIn("12.6", str(cm.exception))
        self.assertEqual(self.daemon.order, [])
        self.assert_found()

    def test_the_result_keys_the_tests_require(self):
        # every key either test reads is one the scripted burn reports
        self.assertTrue(set(setup_burns.FLOOR_KEYS) <= set(SCRIPT["laser.floor"][1]) | {"thickness_mm"})
        self.assertTrue(set(setup_burns.FOCUS_KEYS)
                        <= set(SCRIPT["laser.focus"][1]) | {"thickness_mm", "window", "ladder_z"})


if __name__ == "__main__":
    unittest.main()
