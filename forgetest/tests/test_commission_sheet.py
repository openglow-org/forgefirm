"""The setup's sheet in the catalog: one live test on the button with a
piece of wood at hand, its covers map naming the live runner, the
renderer, the sender, the page, and the driver's laser module; and the
whole run against a scripted daemon: the placement's jogs and origin,
then each card's preview and program read, its start, the arm prompt and
the pick answered, the witnesses judged, the record read at version 1,
every card's evidence filed under its own key, and the settings the cards
wrote put back as found at the end."""
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(__file__))
import helpers  # noqa: E402
from forgetest import catalog  # noqa: E402
from forgetest.runner import Context, Run  # noqa: E402
from forgetest.suite import commission_sheet  # noqa: E402

LIVE = ["sheet.frame", "laser.focus", "laser.floor", "laser.dose-curve", "laser.corner", "cooling.flow-load"]
# The focus card's program is read again after it ran: the served ladder
# must then lie in the window the card wrote.
PREVIEWS = ["sheet.frame", "laser.focus", "laser.focus", "laser.floor", "laser.dose-curve", "laser.corner",
            "cooling.flow-load"]
EMISSION = {"hv_max": 610, "laser_on_samples": 900, "thermopile_delta": 600, "lit_s": 30.0}
SPM = 2.922                     # the lens screw, half-steps per mm ($102)
EDGE_DEFAULT = 3.35


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


# The window the focus card finds and writes before its controller starts.
STOPS = {"lens_stop_below_steps": "14", "lens_stop_above_steps": "20"}
FOCUS_WINDOW = {"below": 14, "above": 20, "reach_min": round(EDGE_DEFAULT - 14 / SPM, 2),
                "reach_max": round(EDGE_DEFAULT + 20 / SPM, 2)}

# Each wizard as the daemon runs it: its prompts in order (id, kind,
# options) and the result it ends with. A "jog" prompt repeats until the
# origin is set.
SCRIPT = {
    "sheet.place": ([("place", "jog", ["X-10", "X+10", "Set origin"]),
                     ("sheet-kind", "choice", ["Full sheet", "One card"]),
                     ("thickness", "number", ["Set", "Skip"])],
                    {"origin_x": 100, "origin_y": 200, "origin_z": 0, "steps_per_mm": {"x": 40.0, "y": 40.0},
                     "z_referenced": True, "thickness_mm": 3.2, "alone": False}),
    "sheet.frame": ([("frame-arm", "continue", ["Continue"]), ("frame-ok", "choice", ["Yes", "No", "Again"])],
                    {"mark_s": 400, "mark_feed": 3000, "emission": EMISSION}),
    "laser.focus": ([("focus-arm", "continue", ["Continue"]),
                     ("focus-pick", "multichoice", [str(i) for i in range(1, 13)]),
                     ("thickness", "number", ["Keep", "Set"])],
                    {"pick": 11, "thickness_mm": 3.2, "pick_half_steps": -11, "edge_z_mm": 6.96,
                     "steps_per_mm": 2.922, "max_height_mm": 13.81,
                     "focus_range_mm": {"min": 2.17, "max": 13.81},
                     "stops": {"found": True, "below": 14, "above": 20, "contact_below": 16,
                               "contact_above": 22, "why": ""},
                     "window": dict(FOCUS_WINDOW), "ladder_z": ladder_z(EDGE_DEFAULT, 14, 20),
                     "emission": EMISSION}),
    "laser.floor": ([("floor-arm", "continue", ["Continue"]),
                     ("floor-pick", "choice", ["2", "4", "6", "8", "None"])],
                    {"faintest_density": 8.0, "floor_density": 10.0, "emission": EMISSION}),
    "laser.dose-curve": ([("dose-arm", "continue", ["Continue"])],
                         {"points": [[10, 0.44], [100, 100]], "curve": "10:0.44,100:100", "emission": EMISSION}),
    "laser.corner": ([("corner-arm", "continue", ["Continue"]),
                      ("corner-pick", "choice", ["1.00", "1.25", "1.50", "1.75", "2.00"])],
                     {"gamma": 1.5, "emission": EMISSION}),
    "cooling.flow-load": ([("load-arm", "continue", ["Continue"])],
                          {"lit_s": 30.0, "dose_raw_s": 12000, "peak_c": 0.72, "k_density": 2.14e-5,
                           "k_cw": 2.78e-5, "emission": EMISSION}),
}
# What a card writes when it starts (the focus card's window, before its
# controller starts) and when it ends.
START_WRITES = {"laser.focus": dict(STOPS)}
WRITES = {
    "laser.focus": {"lens_hall_edge_z_mm": "6.96"},
    "laser.floor": {"laser_floor_density": "10"},
    "laser.dose-curve": {"laser_dose_curve": "10:0.44,100:100"},
    "laser.corner": {"laser_corner_gamma": "1.5"},
    "cooling.flow-load": {"cool_laser_heat_density": "2.14e-5", "cool_laser_heat_cw": "2.78e-5"},
}
FOUND = {"lens_hall_edge_z_mm": "3.35", "lens_stop_below_steps": "10", "lens_stop_above_steps": "12",
         "laser_floor_density": "12",
         "laser_dose_curve": "off", "laser_corner_gamma": "2", "cool_laser_heat_density": "1e-5",
         "cool_laser_heat_cw": "1e-5"}


class RegistrationTests(unittest.TestCase):
    def setUp(self):
        self.t = catalog.load_suite()["commission.sheet"]

    def test_one_live_test_on_the_button_with_the_sheet_at_hand(self):
        t = self.t
        self.assertEqual(t.kind, "live")
        self.assertEqual(t.mode, "grbl")
        self.assertEqual(t.hardware, "takeover")
        self.assertIn("button", t.actions)
        self.assertTrue(t.hands)
        self.assertFalse(t.fixture_runnable(("button", "lid", "interlock")))
        self.assertIsNone(getattr(t, "precheck", None))
        for want in ("commission.check-motion", "laser.emission-witness", "cooling.flow-verify"):
            self.assertIn(want, t.requires)

    def test_no_other_sheet_test_remains(self):
        ids = [i for i in catalog.load_suite() if i.startswith("commission.sheet")]
        self.assertEqual(ids, ["commission.sheet"])

    def test_covers_the_live_runner_the_renderer_the_sender_the_page_and_the_driver(self):
        covers = set(self.t.covers)
        for want in (("forgectrl", "src/wizlive.*"), ("forgectrl", "src/sheet.*"), ("forgectrl", "src/jobstream.*"),
                     ("forgectrl", "src/curverec.*"), ("forgectrl", "src/font_hershey.*"),
                     ("forgectrl", "src/ui/wizard.*"), ("forgectrl", "src/wizrun.h"),
                     ("grblhal-glowforge", "src/glowforge_laser.c")):
            self.assertIn(want, covers, "lacks %s" % (want,))

    def test_the_steps_ask_for_one_press_and_no_page(self):
        text = " ".join(self.t.steps).lower()
        self.assertIn("press the machine's button once", text)
        self.assertNotIn("when the page", text)
        self.assertEqual(len(commission_sheet.CARDS), 6)


class ScriptedDaemon:
    """The wizard runner as the suite sees it: /wiz/<id>/start opens the
    first prompt, each answer opens the next or ends the wizard with its
    result and its writes, /wiz/dark reports it, GET /wiz carries the
    versions, the sheet preview and program are served per card."""

    def __init__(self, fake):
        self.fake = fake
        fake.state["settings"].update(FOUND)
        self.dark = {"id": "", "running": False, "phase": "", "progress": 0, "elapsed_s": 0,
                     "log": [], "prompt": None, "result": None, "error": "",
                     "shots": {"lid": False, "head": False}}
        self.versions = {}
        self.answers = {}          # wid -> [values]
        self.order = []            # the wizards as started
        self.settings_at_start = {}    # wid -> the settings as the wizard found them
        self.previews = []
        self.queue = []
        self.seq = 0
        self.stray_z = None        # a Z the served program commands beyond the reach, when set
        fake.on_get = self.on_get
        fake.on_post = self.on_post

    def open_next(self):
        if not self.queue:
            return False
        pid, kind, options = self.queue[0]
        self.seq += 1
        self.dark["prompt"] = {"seq": self.seq, "id": pid, "kind": kind, "text": pid, "options": options}
        return True

    def on_get(self, path, q):
        s = self.fake.state["settings"]
        if path == "/status":
            # the lens block follows the settings, as the daemon's does
            return 200, dict(self.fake.state["status"], lens=lens_block(s))
        if path == "/wiz/dark":
            return 200, dict(self.dark)
        if path == "/wiz":
            return 200, {"versions": dict(self.versions), "required": [], "completed": True}
        if path == "/wiz/sheet.svg":
            self.previews.append(q.get("card"))
            if q.get("card") not in LIVE:
                return 404, {"error": "no such card"}
            return 200, b"<svg xmlns='http://www.w3.org/2000/svg'></svg>", "image/svg+xml"
        if path == "/wiz/sheet.gcode":
            # the frame and the text are M4 at the mark dose, as the daemon
            # serves them; the head's Z is the edge, the tail goes back to
            # it, and the focus card's ladder spans the window the settings
            # hold now
            lens = lens_block(s)
            lines = ["; mock", "G0 Z%.2f" % lens["edge_z"], "M4 S400", "G0 X60 Y42", "G1 X120 Y42 F3000"]
            if q.get("card") == "laser.focus":
                lines += ["G0 Z%.2f" % z for z in ladder_z(lens["edge_z"], lens["below"], lens["above"])]
            if self.stray_z is not None:
                lines.append("G0 Z%.2f" % self.stray_z)
            lines += ["M5", "G0 Z%.2f" % lens["edge_z"], "G0 X0 Y0", "M2"]
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
            s.update(START_WRITES.get(wid, {}))
            self.queue = list(SCRIPT[wid][0])
            self.dark.update({"id": wid, "running": True, "phase": "starting", "result": None, "error": ""})
            self.open_next()
            return 200, {"started": True, "id": wid}
        if path.startswith("/wiz/") and path.endswith("/answer"):
            wid = path[len("/wiz/"):-len("/answer")]
            p = self.dark.get("prompt")
            if not p or form.get("seq") != str(p["seq"]):
                return 409, {"error": "no such prompt is open"}
            self.answers.setdefault(wid, []).append(form.get("value"))
            if not (p["kind"] == "jog" and form.get("value") != "Set origin"):
                self.queue.pop(0)
            if not self.open_next():
                s.update(WRITES.get(wid, {}))
                self.dark.update({"running": False, "prompt": None, "progress": 100,
                                  "result": dict(SCRIPT[wid][1])})
                self.versions[wid] = 1
            return 200, {"ok": True}
        if path == "/settings":
            for k, v in form.items():
                if v == "":
                    s.pop(k, None)
                else:
                    s[k] = v
            return 200, dict(s)
        return None


class SheetRunTests(unittest.TestCase):
    def setUp(self):
        self.fake = helpers.FakeForgectrl().start()
        self.daemon = ScriptedDaemon(self.fake)

    def tearDown(self):
        self.fake.stop()

    def test_the_whole_sheet_on_one_piece(self):
        t = catalog.load_suite()["commission.sheet"]
        run = Run("test", t.id, t.title)
        run.unattended = True           # the ready gate passes; the arm cue is a notice
        t.fn(Context(run, None, t))
        d = self.daemon
        self.assertEqual(d.order, ["sheet.place"] + LIVE)
        self.assertEqual(d.answers["sheet.place"], ["X+10", "X-10", "Set origin", "Full sheet", "3.2"])
        self.assertEqual(d.answers["sheet.frame"], ["Continue", "Yes"])
        self.assertEqual(d.answers["laser.focus"], ["Continue", "11", "Keep"])
        self.assertEqual(d.answers["laser.floor"], ["Continue", "8"])
        self.assertEqual(d.answers["laser.corner"], ["Continue", "1.50"])
        self.assertEqual(d.previews, PREVIEWS)
        cards = run.evidence["cards"]
        self.assertEqual(sorted(cards), sorted(["sheet.place"] + LIVE))
        for wid in LIVE:
            self.assertEqual(cards[wid]["emission"]["hv_max"], 610, wid)
            self.assertEqual(cards[wid]["result"], SCRIPT[wid][1], wid)
        self.assertNotIn("emission", run.evidence)
        self.assertEqual(cards["sheet.place"]["result"]["origin_x"], 100)
        self.assertEqual(run.evidence["settings_before"], FOUND)
        # A fresh machine's run: the lens settings were gone before the
        # frame, the frame's program ran in the fallback window, the focus
        # card's window was in the settings when the floor card started,
        # and the window check filed what it compared.
        for k in commission_sheet.LENS_SETTINGS:
            self.assertNotIn(k, d.settings_at_start["sheet.frame"], k)
        self.assertEqual(run.evidence["lens_fresh"]["stops_found"], False)
        self.assertEqual(run.evidence["program_z"]["sheet.frame"]["reach"], [-0.07, 7.46])
        floor_start = d.settings_at_start["laser.floor"]
        self.assertEqual({k: floor_start[k] for k in commission_sheet.LENS_SETTINGS},
                         {"lens_hall_edge_z_mm": "6.96", "lens_stop_below_steps": "14", "lens_stop_above_steps": "20"})
        fw = run.evidence["focus_window"]
        self.assertEqual(fw["window"], FOCUS_WINDOW)
        self.assertEqual(fw["settings"]["lens_stop_below_steps"], "14")
        self.assertEqual(len(fw["ladder_z"]), 12)
        self.assertEqual(fw["program_z_before"]["reach"], [-0.07, 7.46])      # the fallback ladder
        self.assertEqual(fw["program_z_after"]["reach"], [2.17, 13.8])        # the written window at the new edge
        self.assertEqual(len(fw["program_z_after"]["z"]), 14)                 # head, twelve lines, tail
        for k, v in FOUND.items():
            self.assertEqual(self.fake.state["settings"][k], v, k)      # restored

    def test_imperial_units_answer_the_thickness_in_inches(self):
        # The number prompts read ui_units: on an imperial machine the
        # placement's thickness is 0.125 in and the record carries 3.175 mm.
        self.fake.state["settings"]["ui_units"] = "imperial"
        place_prompts, place_result = SCRIPT["sheet.place"]
        SCRIPT["sheet.place"] = (place_prompts, dict(place_result, thickness_mm=3.175))
        try:
            t = catalog.load_suite()["commission.sheet"]
            run = Run("test", t.id, t.title)
            run.unattended = True
            t.fn(Context(run, None, t))
            self.assertEqual(self.daemon.answers["sheet.place"][-1], "0.125")
            self.assertEqual(run.evidence["thickness_answer"], "0.125")
        finally:
            SCRIPT["sheet.place"] = (place_prompts, place_result)

    def test_a_metric_answer_on_an_imperial_machine_fails_the_placement(self):
        # The defect the bench found: 3.2 read as inches is over the 30 mm
        # cap and the wizard records 0; the test must not read that as a pass.
        from forgetest.runner import Failed
        place_prompts, place_result = SCRIPT["sheet.place"]
        SCRIPT["sheet.place"] = (place_prompts, dict(place_result, thickness_mm=0.0))
        try:
            t = catalog.load_suite()["commission.sheet"]
            run = Run("test", t.id, t.title)
            run.unattended = True
            with self.assertRaises(Failed) as cm:
                t.fn(Context(run, None, t))
            self.assertIn("thickness 0.0 mm", str(cm.exception))
            self.assertEqual(self.daemon.order, ["sheet.place"])       # nothing burned
        finally:
            SCRIPT["sheet.place"] = (place_prompts, place_result)

    def test_a_dark_burn_fails_the_run_and_still_restores(self):
        from forgetest.runner import Failed
        SCRIPT_DARK = dict(SCRIPT["laser.floor"][1])
        SCRIPT_DARK["emission"] = {"hv_max": 0, "laser_on_samples": 0, "thermopile_delta": 0}
        SCRIPT["laser.floor"] = (SCRIPT["laser.floor"][0], SCRIPT_DARK)
        try:
            t = catalog.load_suite()["commission.sheet"]
            run = Run("test", t.id, t.title)
            run.unattended = True
            with self.assertRaises(Failed):
                t.fn(Context(run, None, t))
            self.assertEqual(self.daemon.order, ["sheet.place", "sheet.frame", "laser.focus", "laser.floor"])
            for k, v in FOUND.items():
                self.assertEqual(self.fake.state["settings"][k], v, k)
        finally:
            SCRIPT["laser.floor"] = (SCRIPT["laser.floor"][0], dict(SCRIPT_DARK, emission=EMISSION))

    def test_a_focus_window_the_settings_do_not_hold_fails_the_run(self):
        # The defect the bench found: the focus card burned its ladder over
        # the stops it found while the controller's Z limit stood on the
        # settings, which did not hold them. A daemon whose focus result
        # names a window the settings do not hold fails the run before the
        # floor card, and the settings still go back.
        from forgetest.runner import Failed
        saved = dict(START_WRITES["laser.focus"])
        START_WRITES["laser.focus"] = {}
        try:
            t = catalog.load_suite()["commission.sheet"]
            run = Run("test", t.id, t.title)
            run.unattended = True
            with self.assertRaises(Failed) as cm:
                t.fn(Context(run, None, t))
            self.assertIn("the settings hold", str(cm.exception))
            self.assertEqual(self.daemon.order, ["sheet.place", "sheet.frame", "laser.focus"])
            for k, v in FOUND.items():
                self.assertEqual(self.fake.state["settings"][k], v, k)
        finally:
            START_WRITES["laser.focus"] = saved

    def test_a_program_z_beyond_the_reach_fails_before_the_burn(self):
        # A served program that commands a Z the lens does not reach is
        # refused before the card starts: nothing burns.
        from forgetest.runner import Failed
        self.daemon.stray_z = 10.6
        t = catalog.load_suite()["commission.sheet"]
        run = Run("test", t.id, t.title)
        run.unattended = True
        with self.assertRaises(Failed) as cm:
            t.fn(Context(run, None, t))
        self.assertIn("outside the lens window", str(cm.exception))
        self.assertIn("10.6", str(cm.exception))
        self.assertEqual(self.daemon.order, ["sheet.place"])
        for k, v in FOUND.items():
            self.assertEqual(self.fake.state["settings"][k], v, k)


if __name__ == "__main__":
    unittest.main()
