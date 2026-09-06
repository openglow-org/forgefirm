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
EMISSION = {"hv_max": 610, "laser_on_samples": 900, "thermopile_delta": 600, "lit_s": 30.0}

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
                               "contact_above": 22, "why": ""}, "emission": EMISSION}),
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
# What each card writes when it ends.
WRITES = {
    "laser.focus": {"lens_hall_edge_z_mm": "6.96", "lens_stop_below_steps": "14",
                    "lens_stop_above_steps": "20"},
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
                     ("forgectrl", "src/ui/wizard.*"), ("forgectrl", "tools/hershey_gen.py"),
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
        self.previews = []
        self.queue = []
        self.seq = 0
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
            # the frame and the text are M4 at the mark dose, as the daemon serves them
            return 200, b"; mock\nM4 S400\nG0 X60 Y42\nG1 X120 Y42 F3000\nM5\n", "text/plain"
        return None

    def on_post(self, path, form):
        s = self.fake.state["settings"]
        if path.startswith("/wiz/") and path.endswith("/start"):
            wid = path[len("/wiz/"):-len("/start")]
            if wid not in SCRIPT:
                return 404, {"error": "no such wizard"}
            self.order.append(wid)
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
        self.assertEqual(d.previews, LIVE)
        cards = run.evidence["cards"]
        self.assertEqual(sorted(cards), sorted(["sheet.place"] + LIVE))
        for wid in LIVE:
            self.assertEqual(cards[wid]["emission"]["hv_max"], 610, wid)
            self.assertEqual(cards[wid]["result"], SCRIPT[wid][1], wid)
        self.assertNotIn("emission", run.evidence)
        self.assertEqual(cards["sheet.place"]["result"]["origin_x"], 100)
        self.assertEqual(run.evidence["settings_before"], FOUND)
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


if __name__ == "__main__":
    unittest.main()
