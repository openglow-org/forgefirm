# Copyright 2026 514 LLC d/b/a OpenGlow
# Written by Scott Wiederhold
# https://community.openglow.org
# SPDX-License-Identifier:    MIT

"""The setup's focus burn in the catalog: a live test on the button with
a scrap at hand, its covers naming the live runner, the renderer, the
sender, the page, and the driver's laser module; and the burn against a
scripted daemon: the preview and the program read, the start, the
prompts answered in order (the gantry stops, the head at home, the scrap,
its thickness in the machine's units, the arm, the machine's own press
prompt, the pick), the witnesses judged (the burn's own, and the two the
test reads from sysfs at every poll), the record read at version 1, the
setting the burn wrote put back as found, and the controller started
again on the edge and the window put back."""
import os
import shutil
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(__file__))
import helpers  # noqa: E402
from forgetest import baseline, catalog  # noqa: E402
from forgetest.runner import Aborted, Context, Failed, Run  # noqa: E402
from forgetest.suite import setup_burns, setup_dark  # noqa: E402

LIVE = ["laser.focus"]
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


# The burn as the daemon runs it: its prompts in order (id, kind,
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
}
# What the burn writes when it ends.
WRITES = {"laser.focus": {"lens_hall_edge_z_mm": "6.96"}}
# The machine as the burn finds it: its own edge, the window the Lens
# travel check wrote, its floor.
FOUND = {"lens_hall_edge_z_mm": "4.10", "lens_stop_below_steps": "14", "lens_stop_above_steps": "20",
         "laser_floor_density": "12", "ui_units": "metric"}
# What grblHAL reads at its start: the edge and the window.
LENS_KEYS = ("lens_hall_edge_z_mm", "lens_stop_below_steps", "lens_stop_above_steps")
HOME = "The head is home"


class RegistrationTests(unittest.TestCase):
    def setUp(self):
        self.reg = catalog.load_suite()

    def test_a_live_test_on_the_button_with_a_scrap_at_hand(self):
        t = self.reg["setup.focus"]
        self.assertEqual(t.kind, "live")
        self.assertEqual(t.mode, "grbl")
        self.assertEqual(t.hardware, "takeover")
        self.assertEqual(list(t.actions), ["button"])
        self.assertEqual(list(t.hands), ["scrap"])
        self.assertFalse(t.fixture_runnable(("button", "lid", "interlock")))
        self.assertIsNone(getattr(t, "precheck", None))
        self.assertEqual(list(t.requires),
                         ["forgectrl.auth", "setup.check-motion", "laser.emission-witness", "setup.check-lens"])

    def test_the_sheet_is_gone(self):
        self.assertFalse([i for i in self.reg if i.startswith("setup.sheet")])

    def test_covers_the_live_runner_the_renderer_the_sender_the_page_and_the_driver(self):
        for tid in ("setup.focus",):
            covers = set(self.reg[tid].covers)
            for want in (("forgectrl", "src/wizlive.*"), ("forgectrl", "src/sheet.*"),
                         ("forgectrl", "src/jobstream.*"), ("forgectrl", "src/curverec.*"),
                         ("forgectrl", "src/font_hershey.*"), ("forgectrl", "src/lens.*"),
                         ("forgectrl", "src/lenshome.*"),       # the lens back on its reference at the end
                         ("forgectrl", "src/ui/wizard.*"), ("forgectrl", "src/wizrun.h"),
                         ("grblhal-glowforge", "src/glowforge_laser.c")):
                self.assertIn(want, covers, "%s lacks %s" % (tid, want))

    def test_the_steps_ask_for_a_scrap_in_the_corner_and_one_press(self):
        for tid, size in (("setup.focus", "78 x 68 mm"),):
            text = " ".join(self.reg[tid].steps)
            self.assertIn(size, text, tid)
            self.assertIn("back-left corner", text, tid)
            self.assertIn("gantry stops", text, tid)
            self.assertIn("press the machine's button once", text, tid)


class ScriptedDaemon:
    """The wizard runner as the suite sees it: /wiz/<id>/start opens the
    first prompt, each answer opens the next (a thickness that is not a
    plain number or out of range in the machine's units opens it again),
    the press prompt
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
                if not 0 < mm <= 30:
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
        # the two witnesses as sysfs serves them through a lit burn (the
        # shape of a bench record: the LASER_ON count and the tube current)
        self.sysfs = tempfile.mkdtemp(prefix="forgetest-burn-")
        self.plant("cnc/laser_on_sampled", "118")
        self.plant("pic/hv_current", "431")
        self.saved_root = os.environ.get("GF_SYSFS_ROOT")
        os.environ["GF_SYSFS_ROOT"] = self.sysfs + "/"
        self.fake = helpers.FakeForgectrl().start()
        # the head at its home against the gantry stops, X and Y referenced
        # by a manual home with no offset (the fake's pos is X0 Y0 Z0), its
        # anchor declaring the stops at X0 Y0
        self.fake.state["status"].update({"homed": True, "homed_axes": 7, "home_source": "manual"})
        self.saved_anchor = baseline.ANCHOR_PATH
        baseline.ANCHOR_PATH = os.path.join(self.sysfs, "grblhal.homed")
        self.anchor("0.000 0.000 0.000 7 manual")
        self.daemon = ScriptedDaemon(self.fake)
        self.poll = setup_dark.POLL_S
        setup_dark.POLL_S = 0.01

    def tearDown(self):
        setup_dark.POLL_S = self.poll
        baseline.ANCHOR_PATH = self.saved_anchor
        self.fake.stop()
        if self.saved_root is None:
            os.environ.pop("GF_SYSFS_ROOT", None)
        else:
            os.environ["GF_SYSFS_ROOT"] = self.saved_root
        shutil.rmtree(self.sysfs, ignore_errors=True)

    def plant(self, attr, value):
        path = os.path.join(self.sysfs, *attr.split("/"))
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w") as f:
            f.write(value + "\n")

    def anchor(self, line):
        """The controller's home anchor as a home writes it: X Y Z, the
        axes it references, and what set it."""
        with open(baseline.ANCHOR_PATH, "w") as f:
            f.write(line + "\n")

    def run_test(self, tid):
        t = catalog.load_suite()[tid]
        run = self.run = Run("test", t.id, t.title)     # kept for a run that fails
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
        # the head's place on record before the start, judged at the stops: X0 Y0 with no offset
        self.assertEqual(run.evidence["head_at_start"],
                         {"pos": {"x": 0.0, "y": 0.0, "z": 0.0}, "homed_axes": 7, "home_source": "manual",
                          "judged": True, "stops": {"x": 0.0, "y": 0.0}})
        # the witnesses read from here at every poll: each at its peak
        seen = run.evidence["emission_seen"]
        self.assertEqual((seen["laser_on_max"], seen["hv_max"]), (118, 431))
        self.assertGreater(seen["reads"], 0)
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

    def test_a_program_that_keeps_its_g92_origin_fails_before_the_burn(self):
        # The tail clears the G92 origin, which the controller would
        # otherwise carry to its next start under a sender's own offsets.
        self.daemon.tail = ["G0 X0 Y0", "M2"]
        with self.assertRaises(Failed) as cm:
            self.run_test("setup.focus")
        self.assertIn("not the tail", str(cm.exception))
        self.assertIn("G92.1", str(cm.exception))
        self.assertEqual(self.daemon.order, [])
        self.assert_found()

    def test_imperial_units_answer_the_thickness_in_inches(self):
        # The number prompts read ui_units: on an imperial machine the
        # thickness is 0.125 in and the record carries 3.175 mm.
        self.fake.state["settings"]["ui_units"] = "imperial"
        run = self.run_test("setup.focus")
        self.assertEqual(self.daemon.answers["laser.focus"][3], "0.125")
        self.assertEqual(run.evidence["thickness_answer"], "0.125")
        self.assertEqual(run.evidence["result"]["thickness_mm"], 3.175)

    def test_a_thickness_the_burn_refuses_ends_it_before_anything_fires(self):
        # 3.2 read as inches is over the 30 mm cap, and the burn asks again:
        # the test ends the burn rather than answer the same thing forever.
        self.daemon.units = "imperial"
        with self.assertRaises(Failed) as cm:
            self.run_test("setup.focus")
        self.assertIn("asked thickness again", str(cm.exception))
        self.assertEqual(self.daemon.answers["laser.focus"], ["Yes", HOME, "Continue", "3.2"])
        self.assertEqual(self.daemon.aborted, ["laser.focus"])
        self.assert_found()

    def test_a_prompt_the_test_does_not_answer_ends_the_burn(self):
        # The motors found released: the burn asks for the head pushed home
        # by hand, which the bench never needs.
        SCRIPT["laser.focus"][0].insert(1, ("home-push", "continue", ["Continue"]))
        try:
            with self.assertRaises(Failed) as cm:
                self.run_test("setup.focus")
        finally:
            SCRIPT["laser.focus"][0].pop(1)
        self.assertIn("asked home-push, which the test does not answer", str(cm.exception))
        self.assertEqual(self.daemon.aborted, ["laser.focus"])
        self.assert_found()

    def test_a_dark_burn_fails_and_still_restores(self):
        self.daemon.patch["laser.focus"] = {"emission": {"hv_max": 0, "laser_on_samples": 0, "thermopile_delta": 0}}
        with self.assertRaises(Failed) as cm:
            self.run_test("setup.focus")
        self.assertIn("the tube current never rose", str(cm.exception))
        # what the test read itself is on record beside the daemon's word
        seen = self.run.evidence["emission_seen"]
        self.assertEqual((seen["laser_on_max"], seen["hv_max"]), (118, 431))
        self.assert_found()

    def test_a_head_off_its_home_fails_before_any_start(self):
        # X and Y referenced by a manual home, and the head 12.5 mm from
        # the stops: nothing starts, no controller lever is touched.
        self.fake.state["status"]["pos"] = {"x": 12.5, "y": 0.0, "z": 0.0}
        with self.assertRaises(Failed) as cm:
            self.run_test("setup.focus")
        self.assertIn("not at its home", str(cm.exception))
        self.assertEqual(self.daemon.order, [])
        self.assertEqual(self.daemon.lever_posts(), [])
        self.assert_found()

    def test_a_manual_home_offset_puts_the_stops_off_x0_y0(self):
        # A manual home made with manual_home_x 5 and manual_home_y 3
        # declares the stops X-5 Y-3 (its anchor) and leaves the head at
        # X0 Y0, in front of them, where the wizard does not draw from. X0
        # Y0 fails before any start, with no controller lever touched; the
        # head at the stops is home.
        self.fake.state["settings"].update({"manual_home_x": "5", "manual_home_y": "3"})
        self.anchor("-5.000 -3.000 0.000 7 manual")
        with self.assertRaises(Failed) as cm:
            self.run_test("setup.focus")
        self.assertIn("not at its home", str(cm.exception))
        self.assertEqual(self.run.evidence["head_at_start"]["stops"], {"x": -5.0, "y": -3.0})
        self.assertEqual(self.daemon.order, [])
        self.assertEqual(self.daemon.lever_posts(), [])
        self.assert_found()
        self.fake.state["status"]["pos"] = {"x": -5.0, "y": -3.0, "z": 0.0}
        run = self.run_test("setup.focus")
        self.assertTrue(run.evidence["head_at_start"]["judged"])
        self.assertEqual(self.daemon.order, ["laser.focus"])
        # The offsets cleared after that home leave its reference as it is:
        # the stops are still X-5 Y-3, and the head at X0 Y0 still fails
        # before any start.
        for k in ("manual_home_x", "manual_home_y"):
            self.fake.state["settings"].pop(k)
        self.fake.state["status"]["pos"] = {"x": 0.0, "y": 0.0, "z": 0.0}
        with self.assertRaises(Failed) as cm:
            self.run_test("setup.focus")
        self.assertIn("not at its home", str(cm.exception))
        self.assertEqual(self.daemon.order, ["laser.focus"])
        # The offsets set after a home made with none: the head at X0 Y0 is
        # at the stops that home declared, and the burn runs.
        self.fake.state["settings"].update({"manual_home_x": "5", "manual_home_y": "3"})
        self.anchor("0.000 0.000 0.000 7 manual")
        run = self.run_test("setup.focus")
        self.assertEqual(run.evidence["head_at_start"]["stops"], {"x": 0.0, "y": 0.0})
        self.assertEqual(self.daemon.order, ["laser.focus", "laser.focus"])
        # An anchor that is gone, or that is not the manual home /status
        # reports, places no stops: nothing starts.
        os.unlink(baseline.ANCHOR_PATH)
        with self.assertRaises(Failed) as cm:
            self.run_test("setup.focus")
        self.assertIn("home anchor", str(cm.exception))
        self.anchor("0.000 0.000 0.000 4 startup")
        with self.assertRaises(Failed) as cm:
            self.run_test("setup.focus")
        self.assertIn("home anchor", str(cm.exception))
        self.assertEqual(self.daemon.order, ["laser.focus", "laser.focus"])

    def test_an_unreferenced_head_runs_on_the_operators_word(self):
        # A fresh controller's counters-only position places nothing: the
        # read is recorded, not judged, and the burn runs.
        self.fake.state["status"].update({"homed": False, "homed_axes": 4, "home_source": "startup",
                                          "pos": {"x": 12.5, "y": 0.0, "z": 0.0}})
        run = self.run_test("setup.focus")
        self.assertEqual(run.evidence["head_at_start"]["judged"], False)
        self.assertEqual(run.evidence["head_at_start"]["home_source"], "startup")
        self.assertEqual(self.daemon.order, ["laser.focus"])

    def test_a_burn_the_test_itself_never_saw_fails_and_still_restores(self):
        # The burn's result reports a lit burn, and the test's own reads
        # of the two witnesses never leave zero: the burn is judged on
        # what was read from here, not on the daemon's word alone.
        self.plant("cnc/laser_on_sampled", "0")
        self.plant("pic/hv_current", "0")
        with self.assertRaises(Failed) as cm:
            self.run_test("setup.focus")
        self.assertIn("LASER_ON count never read nonzero", str(cm.exception))
        self.assert_found()
        self.assert_restarted_on_found()

    def test_a_burn_whose_tube_current_the_test_never_saw_rise_fails(self):
        # The LASER_ON count read nonzero from here, and the tube current
        # never read over HV_ON: the second witness is judged on its own
        # reads, against the daemon's own threshold for a lit burn.
        self.plant("pic/hv_current", str(setup_burns.HV_ON))
        with self.assertRaises(Failed) as cm:
            self.run_test("setup.focus")
        self.assertIn("tube current never read over %d from here" % setup_burns.HV_ON, str(cm.exception))
        self.assert_found()

    def test_a_burn_with_a_silent_beam_sensor_passes(self):
        # A head whose beam sensor gives no output: the tube current and
        # LASER_ON saw the beam, so the burn counts.
        self.daemon.patch["laser.focus"] = {"emission": dict(EMISSION, thermopile_delta=0,
                                                             beam_sensor_seen=False)}
        self.run_test("setup.focus")
        self.assert_found()

    def test_a_burn_that_does_not_record_the_beam_sensor_fails(self):
        self.daemon.patch["laser.focus"] = {"emission": {"hv_max": 610, "laser_on_samples": 900}}
        with self.assertRaises(Failed) as cm:
            self.run_test("setup.focus")
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
        self.assertIn("emission_seen", run.evidence)    # the test's own reads, on record all the same
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
            self.run_test("setup.focus")
        self.assertIn("outside the lens window", str(cm.exception))
        self.assertIn("12.6", str(cm.exception))
        self.assertEqual(self.daemon.order, [])
        self.assert_found()

    def test_the_result_keys_the_tests_require(self):
        # every key the test reads is one the scripted burn reports
        self.assertTrue(set(setup_burns.FOCUS_KEYS)
                        <= set(SCRIPT["laser.focus"][1]) | {"thickness_mm", "window", "ladder_z"})


if __name__ == "__main__":
    unittest.main()
