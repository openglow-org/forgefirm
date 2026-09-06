"""The commission.* suite on the host: the registration (ids, kinds, the
takeover tests, the operator tests' hands), the record builders, the
mDNS packet code, the cookie parsing, the LED cue, the settle rule for a
gated supervisor, and the cloud-off surface test driven end to end
against the fake daemon."""
import json
import os
import shutil
import socket
import struct
import tempfile
import unittest

import helpers
from forgetest import baseline, catalog
from forgetest.runner import Context, Run
from forgetest.suite import commission

IDS = ("commission.gate-blocks-controllers", "commission.override-until-reboot",
       "commission.advisories-rehash", "commission.account-login", "commission.https-only-writes",
       "commission.ssh-until-reboot", "commission.cloud-disabled-surface",
       "commission.factory-return", "commission.mdns-announce", "commission.first-run-flow",
       "commission.first-run-page", "commission.what-changed", "commission.record-export",
       "commission.mirror")
OPERATOR = ("commission.first-run-flow", "commission.first-run-page")


class RegistrationTests(unittest.TestCase):
    def setUp(self):
        self.reg = catalog.load_suite()

    def test_every_test_is_registered_with_its_kind(self):
        kinds = {tid: self.reg[tid].kind for tid in IDS if tid in self.reg}
        self.assertEqual(sorted(kinds), sorted(IDS))
        for tid in OPERATOR:
            self.assertEqual(kinds[tid], "operator", tid)
        for tid in IDS:
            if tid not in OPERATOR:
                self.assertEqual(kinds[tid], "auto", tid)
        self.assertNotIn("commission.root-ssh-refused", self.reg)     # folded into ssh-until-reboot
        self.assertNotIn("commission.wizard-first-run", self.reg)     # split into the flow and the page

    def test_the_record_swapping_tests_are_takeovers(self):
        for tid in ("commission.gate-blocks-controllers", "commission.advisories-rehash",
                    "commission.first-run-flow", "commission.first-run-page",
                    "commission.what-changed", "commission.mirror"):
            self.assertEqual(self.reg[tid].hardware, "takeover", tid)
        self.assertEqual(self.reg["commission.record-export"].hardware, "api")

    def test_the_lifecycle_tests_cover_their_files(self):
        changed = set(self.reg["commission.what-changed"].covers)
        export = set(self.reg["commission.record-export"].covers)
        mirror = set(self.reg["commission.mirror"].covers)
        self.assertIn(("forgectrl", "src/ui/panel.js"), changed)
        self.assertIn(("forgectrl", "src/recordhtml.*"), export)
        self.assertIn(("forgectrl", "src/logs.*"), export)
        self.assertIn(("forgectrl", "src/wizdark.*"), mirror)
        self.assertIn("commission.check-sensors", self.reg["commission.mirror"].requires)

    def test_flag_of_reads_the_status_document(self):
        w = {"required": ["airflow", {"id": "laser.floor", "reason": "the tube was replaced"}],
             "recommended": [{"id": "laser.focus", "reason": "the tray was replaced"}]}
        self.assertEqual(commission.flag_of(w, "required", "laser.floor"), "the tube was replaced")
        self.assertEqual(commission.flag_of(w, "recommended", "laser.focus"), "the tray was replaced")
        self.assertIsNone(commission.flag_of(w, "required", "airflow"))       # a plain id is the table, not a flag
        self.assertIsNone(commission.flag_of(w, "required", "laser.focus"))
        self.assertIsNone(commission.flag_of({}, "recommended", "x"))

    def test_the_first_run_flow_runs_unattended_with_the_fixture_and_the_page_walk_does_not(self):
        flow = self.reg["commission.first-run-flow"]
        self.assertEqual(list(flow.actions), ["button"])
        self.assertFalse(flow.hands)
        self.assertTrue(flow.fixture_runnable(("button", "lid", "interlock")))
        page = self.reg["commission.first-run-page"]
        self.assertTrue(page.hands)
        self.assertFalse(page.fixture_runnable(("button", "lid", "interlock")))
        self.assertIn("commission.first-run-flow", page.requires)

    def test_the_first_run_split_keeps_the_backend_and_the_page_apart(self):
        flow = set(self.reg["commission.first-run-flow"].covers)
        page = set(self.reg["commission.first-run-page"].covers)
        for want in (("forgectrl", "src/wiz.*"), ("forgectrl", "src/commission.*"), ("forgectrl", "src/users.*"),
                     ("forgectrl", "src/button.*"), ("forgectrl", "src/led.*"), ("forgectrl", "src/advisories.*")):
            self.assertIn(want, flow, want)
            self.assertNotIn(want, page, want)
        for want in (("forgectrl", "src/ui/wizard.*"), ("forgectrl", "src/ui/md.js"), ("forgectrl", "src/tls.*")):
            self.assertIn(want, page, want)

    def test_the_only_attended_tests_need_a_workstation_or_the_sheet(self):
        # With the bench actuator up, three commissioning tests keep a
        # person: the page walk, the print from the Glowforge app, and
        # the sheet. Everything else runs from the queue.
        attended = sorted(tid for tid, t in self.reg.items() if tid.startswith("commission.")
                          and t.kind != "auto" and not t.fixture_runnable(("button", "lid", "interlock")))
        self.assertEqual(attended, ["commission.cloud-header-capture", "commission.first-run-page",
                                    "commission.sheet"])

    def test_the_factory_return_never_runs_the_return(self):
        import inspect
        t = self.reg["commission.factory-return"]
        src = inspect.getsource(t.fn)
        self.assertNotIn('"confirm": "1"', src)         # the only argument that starts the return
        self.assertIn('"confirm": "0"', src)
        self.assertEqual(t.kind, "auto")                 # the return itself is a bench drill, not a test
        self.assertFalse(t.hands)

    def test_mdns_covers_nothing_by_design(self):
        self.assertEqual(self.reg["commission.mdns-announce"].covers, ())

    def test_the_login_test_makes_its_own_account(self):
        # No bench credentials, no precheck: the test installs a temporary
        # account under a takeover and restores the real one.
        t = self.reg["commission.account-login"]
        self.assertIsNone(getattr(t, "precheck", None))
        self.assertEqual(t.hardware, "takeover")
        with open(commission.__file__) as f:
            src = f.read()
        for k in ("FORGETEST_LOGIN_NAME", "FORGETEST_LOGIN_PASSWORD"):
            self.assertNotIn(k, src)


class RecordTests(unittest.TestCase):
    STATUS = {"sheet_id": "ABCDE-FGHIJ",
              "documents": [{"id": "safety-and-risk", "hash": "a" * 64, "consent": "typed"},
                            {"id": "licenses", "hash": "b" * 64, "consent": "check"}],
              "wizards": [{"id": "advisories", "version": 1}, {"id": "account", "version": 1},
                          {"id": "machine", "version": 2}]}

    def test_complete_record_counts_as_commissioned(self):
        rec = commission.complete_record(self.STATUS)
        self.assertEqual(rec["schema"], 1)
        self.assertEqual(rec["advisories"]["safety-and-risk"]["hash"], "a" * 64)
        self.assertEqual(rec["advisories"]["safety-and-risk"]["method"], "typed")
        self.assertIn("pressed_at", rec["acceptance"])
        self.assertEqual(rec["account"]["name"], "bench")
        self.assertEqual(rec["wizards"]["machine"]["version"], 2)
        self.assertEqual(rec["sheet_id"], "ABCDE-FGHIJ")
        self.assertTrue(rec["completed"])
        json.dumps(rec)

    def test_complete_record_keeps_the_base_account_and_machine(self):
        base = {"account": {"name": "owner", "uid": 1000, "created": "x"}, "machine": {"model": "pro"},
                "sheet_id": "KKKKK-LLLLL", "created": "then"}
        rec = commission.complete_record(self.STATUS, base)
        self.assertEqual(rec["account"]["name"], "owner")
        self.assertEqual(rec["machine"], {"model": "pro"})
        self.assertEqual(rec["sheet_id"], "KKKKK-LLLLL")
        self.assertEqual(rec["created"], "then")

    def test_without_wizards_keeps_consent_and_account(self):
        rec = commission.complete_record(self.STATUS)
        rec["flags"] = {"machine": {"level": "required", "reason": "x"}}
        out = commission.record_without_wizards(rec)
        self.assertEqual(out["wizards"], {})
        self.assertEqual(out["flags"], {})
        self.assertEqual(out["advisories"], rec["advisories"])
        self.assertEqual(out["account"], rec["account"])
        self.assertEqual(rec["flags"], {"machine": {"level": "required", "reason": "x"}})   # a copy

    def test_write_file_and_remove(self):
        tmp = tempfile.mkdtemp(prefix="forgetest-comm-")
        try:
            p = os.path.join(tmp, "sub", "commissioning.json")
            commission.write_file(p, b"{}\n")
            self.assertEqual(commission.read_file(p), b"{}\n")
            if os.name == "posix":              # a mode means nothing on a Windows host
                self.assertEqual(oct(os.stat(p).st_mode & 0o777), oct(0o600))
            commission.write_file(p, None)
            self.assertIsNone(commission.read_file(p))
            commission.write_file(p, None)          # a second remove is silent
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    def test_paths_follow_the_daemon_environment(self):
        os.environ["FORGECTRL_DATA_DIR"] = "/tmp/ffdata"
        os.environ["GF_RUN_DIR"] = "/tmp/ffrun"
        try:
            self.assertEqual(commission.record_path(), "/tmp/ffdata/commissioning.json")
            self.assertEqual(commission.users_path(), "/tmp/ffdata/users")
            self.assertEqual(commission.override_path(), "/tmp/ffrun/commissioning-override")
            self.assertEqual(commission.ssh_flag_path(), "/tmp/ffrun/ssh-enabled")
        finally:
            os.environ.pop("FORGECTRL_DATA_DIR", None)
            os.environ.pop("GF_RUN_DIR", None)


class MdnsTests(unittest.TestCase):
    def test_query_is_a_unicast_response_question(self):
        q = commission.mdns_query("forgefirm.local", qid=0x1234)
        qid, flags, qd, an, ns, ar = struct.unpack(">HHHHHH", q[:12])
        self.assertEqual((qid, flags, qd, an, ns, ar), (0x1234, 0, 1, 0, 0, 0))
        self.assertEqual(q[12:], b"\x09forgefirm\x05local\x00" + struct.pack(">HH", 1, 0x8001))

    def _response(self, qid, name_bytes, addr, flags=0x8400, extra=b""):
        q = b"\x09forgefirm\x05local\x00" + struct.pack(">HH", 1, 1)
        rr = name_bytes + struct.pack(">HHIH", 1, 0x8001, 120, 4) + socket.inet_aton(addr)
        return struct.pack(">HHHHHH", qid, flags, 1, 1, 0, 0) + q + rr + extra

    def test_answers_follow_a_compression_pointer(self):
        pkt = self._response(7, b"\xc0\x0c", "192.168.1.9")
        self.assertEqual(commission.mdns_answers(pkt, 7), [("forgefirm.local", "192.168.1.9")])

    def test_answers_with_the_name_spelled_out(self):
        pkt = self._response(0, b"\x09forgefirm\x05local\x00", "10.0.0.5")
        self.assertEqual(commission.mdns_answers(pkt), [("forgefirm.local", "10.0.0.5")])

    def test_wrong_id_or_a_query_yields_nothing(self):
        pkt = self._response(7, b"\xc0\x0c", "192.168.1.9")
        self.assertEqual(commission.mdns_answers(pkt, 8), [])
        self.assertEqual(commission.mdns_answers(self._response(7, b"\xc0\x0c", "1.2.3.4", flags=0), 7), [])
        self.assertEqual(commission.mdns_answers(b"\x00" * 5), [])

    def test_a_truncated_packet_yields_nothing(self):
        pkt = self._response(7, b"\xc0\x0c", "192.168.1.9")
        self.assertEqual(commission.mdns_answers(pkt[:20], 7), [])


class SmallHelpersTests(unittest.TestCase):
    def test_cookie_parsing(self):
        sid = "ab" * 32
        value = "ffsid=%s; Path=/; HttpOnly; Secure; SameSite=Strict; Max-Age=43200" % sid
        self.assertEqual(commission.session_from_cookie(value), sid)
        self.assertEqual(commission.cookie_flags(value), {"path", "httponly", "secure", "samesite", "max-age"})
        self.assertIsNone(commission.session_from_cookie("ffsid=; Path=/; Max-Age=0"))
        self.assertIsNone(commission.session_from_cookie(""))

    def test_the_teal_breathe_cue(self):
        self.assertTrue(commission.breathes_teal({"target": [0, 180, 200], "pulse_on": [0, 1400, 1400],
                                                  "pulse_off": [0, 1400, 1400]}))
        self.assertFalse(commission.breathes_teal({"target": [0, 255, 40], "pulse_on": [0, 0, 0],
                                                   "pulse_off": [0, 0, 0]}))          # solid green
        self.assertFalse(commission.breathes_teal({"target": [200, 200, 200], "pulse_on": [1800, 1800, 1800],
                                                   "pulse_off": [1800] * 3}))         # white
        self.assertFalse(commission.breathes_teal({"target": [None, None, None], "pulse_on": [None] * 3}))
        self.assertFalse(commission.breathes_teal({}))

    def test_decode(self):
        self.assertEqual(commission.decode(b'{"a": 1}'), {"a": 1})
        self.assertEqual(commission.decode(b"cloud mode is not enabled"), "cloud mode is not enabled")


class GatedSettleTests(unittest.TestCase):
    """A supervisor that reports the commissioning gate closed is settled:
    it spawns nothing until the gate opens, so a takeover that installs
    a gating record must not wait the whole settle timeout for it."""

    def setUp(self):
        self.fake = helpers.FakeForgectrl().start()
        baseline.Baseline._unreachable_until = 0.0

    def tearDown(self):
        self.fake.stop()

    def test_gated_returns_at_once(self):
        self.fake.state["mode"] = {"mode": "grbl", "controller": "gated", "pid": 0, "motion": "unverified",
                                   "gated": True, "why": "commissioning required: machine"}
        lines = []
        body = baseline.Baseline(lines.append).wait_settled(timeout=8)
        self.assertEqual(body["controller"], "gated")
        self.assertTrue(any("controller=gated" in ln for ln in lines))


class FirstRunSeedTests(unittest.TestCase):
    """The record the first-run tests install: a first run behind the
    checks and the sheet."""

    STATUS = {"wizards": [{"id": "advisories", "version": 1, "class": "form"},
                          {"id": "account", "version": 1, "class": "form"},
                          {"id": "cloud", "version": 1, "class": "form"},
                          {"id": "switches", "version": 1, "class": "dark"},
                          {"id": "motion", "version": 2, "class": "dark"},
                          {"id": "sheet.frame", "version": 1, "class": "live"}]}

    def test_no_consent_no_account_not_complete_and_every_check_done(self):
        base = {"created": "2026-09-01T00:00:00Z", "completed": "2026-09-02T00:00:00Z",
                "advisories": {"safety-and-risk": {"hash": "x"}}, "acceptance": {"pressed_at": "t"},
                "account": {"name": "scott", "uid": 1000}, "flags": {"flow_thin": True},
                "wizards": {"advisories": {"version": 1}, "switches": {"version": 1, "result": {"lid": True}}}}
        rec = commission.first_run_record(self.STATUS, base)
        self.assertNotIn("completed", rec)
        self.assertNotIn("acceptance", rec)
        self.assertNotIn("account", rec)
        self.assertEqual(rec["advisories"], {})
        self.assertEqual(rec["created"], "2026-09-01T00:00:00Z")
        self.assertEqual(rec["flags"], {"flow_thin": True})
        self.assertEqual(sorted(rec["wizards"]), ["motion", "sheet.frame", "switches"])
        self.assertEqual(rec["wizards"]["switches"]["result"], {"lid": True})   # kept from the base
        self.assertEqual(rec["wizards"]["motion"]["version"], 2)                 # written at the catalog version

    def test_without_a_base_record(self):
        rec = commission.first_run_record(self.STATUS, None)
        self.assertEqual(sorted(rec["wizards"]), ["motion", "sheet.frame", "switches"])
        self.assertEqual(rec["wizards"]["sheet.frame"]["version"], 1)


class SshdPolicyTests(unittest.TestCase):
    def test_the_three_keys_out_of_sshd_t(self):
        from forgetest import hw
        calls = []

        def fake_run(cmd, timeout=60):
            calls.append(cmd)
            return 0, "port 22\npermitrootlogin no\npasswordauthentication yes\npermitemptypasswords no\nx11forwarding no\n"
        real = hw.run
        hw.run = fake_run
        try:
            self.assertEqual(commission.sshd_policy(), {"permitrootlogin": "no", "passwordauthentication": "yes",
                                                        "permitemptypasswords": "no"})
        finally:
            hw.run = real
        self.assertEqual(calls, [["/usr/sbin/sshd", "-T"]])

    def test_a_failing_sshd_reports_the_error(self):
        from forgetest import hw
        real = hw.run
        hw.run = lambda cmd, timeout=60: (1, "sshd: no hostkeys available")
        try:
            self.assertEqual(commission.sshd_policy(), {"error": "sshd: no hostkeys available"})
        finally:
            hw.run = real


class CloudDisabledSurfaceTests(unittest.TestCase):
    """The cloud-off surface test against the fake daemon: cloud_enabled
    is turned off with one write that sweeps the cloud choices, the three
    cloud-pointing writes and the phrase-less cloud_enabled=1 are refused,
    the settings are left alone, and the prior values come back."""

    def setUp(self):
        self.fake = helpers.FakeForgectrl().start()
        self.fake.state["settings"].update({"controller_mode": "grbl", "homing_mode": "none",
                                            "cloud_enabled": "1", "ui_units": "metric"})

        def on_post(path, form):
            s = self.fake.state["settings"]
            if path == "/settings":
                # as the daemon rules it: on from off takes the typed phrase
                if form.get("cloud_enabled") == "1" and s.get("cloud_enabled") != "1" \
                        and form.get("phrase") != "I UNDERSTAND":
                    return 400, "type I UNDERSTAND to turn cloud mode on"
                # the request's own cloud choices, as main.c checks them
                enabled = form.get("cloud_enabled", s.get("cloud_enabled")) == "1"
                if not enabled and form.get("controller_mode") == "cloud":
                    return 409, "cloud mode is not enabled on this machine"
                if not enabled and form.get("homing_mode") == "gfcloud":
                    return 409, "cloud homing needs cloud mode enabled"
                for k, v in form.items():
                    if k == "phrase":
                        continue
                    if v == "":
                        s.pop(k, None)
                    else:
                        s[k] = v
                # off sweeps what pointed at the cloud, as the cloud step does
                if form.get("cloud_enabled") == "0":
                    if "homing_mode" not in form and s.get("homing_mode") == "gfcloud":
                        s["homing_mode"] = "none"
                    if "controller_mode" not in form and s.get("controller_mode") == "cloud":
                        s["controller_mode"] = "grbl"
                return 200, dict(s)
            if path == "/mode" and form.get("controller") == "cloud" and s.get("cloud_enabled") != "1":
                return 409, "cloud mode is not enabled on this machine"
            return None
        self.fake.on_post = on_post

    def tearDown(self):
        self.fake.stop()

    def run_test(self):
        t = catalog.load_suite()["commission.cloud-disabled-surface"]
        run = Run("test", t.id, t.title)
        t.fn(Context(run, None, t))
        return run

    def test_refusals_and_restore(self):
        run = self.run_test()
        s = self.fake.state["settings"]
        self.assertEqual(s["cloud_enabled"], "1")                  # restored, with the phrase
        self.assertEqual(s["controller_mode"], "grbl")
        self.assertEqual(s["homing_mode"], "none")
        self.assertNotIn("phrase", s)
        posts = [(p, f) for p, f in self.fake.posts]
        self.assertIn(("/settings", {"cloud_enabled": "0"}), posts)
        self.assertIn(("/settings", {"cloud_enabled": "1"}), posts)          # refused: no phrase
        self.assertIn(("/settings", {"cloud_enabled": "1", "phrase": "I UNDERSTAND"}), posts)
        self.assertIn(("/mode", {"controller": "cloud"}), posts)
        self.assertEqual(run.evidence["mode_cloud"]["status"], 409)
        self.assertEqual(run.evidence["settings cloud_enabled=1 no phrase"], 400)
        self.assertEqual(run.evidence["found"]["cloud_enabled"], "1")

    def test_cloud_homing_and_boot_mode_are_swept_by_the_one_write_and_go_back(self):
        # As the daemon rules it: cloud_enabled=0 takes the gfcloud homing
        # and the cloud boot mode down with it, so the test writes the one
        # key, checks the sweep, and puts the three back, cloud mode first.
        self.fake.state["settings"].update({"controller_mode": "cloud", "homing_mode": "gfcloud"})
        run = self.run_test()
        s = self.fake.state["settings"]
        self.assertEqual((s["cloud_enabled"], s["homing_mode"], s["controller_mode"]), ("1", "gfcloud", "cloud"))
        posts = [(p, f) for p, f in self.fake.posts if p == "/settings"]
        order = [f for _, f in posts]
        self.assertEqual(order[:1], [{"cloud_enabled": "0"}])
        self.assertEqual(order[-3:], [{"cloud_enabled": "1", "phrase": "I UNDERSTAND"},
                                      {"homing_mode": "gfcloud"}, {"controller_mode": "cloud"}])
        self.assertEqual(run.evidence["found"], {"cloud_enabled": "1", "homing_mode": "gfcloud",
                                                 "controller_mode": "cloud"})

    def test_an_unset_value_is_cleared_back(self):
        self.fake.state["settings"].pop("cloud_enabled")
        self.run_test()
        self.assertNotIn("cloud_enabled", self.fake.state["settings"])
        self.assertIn(("/settings", {"cloud_enabled": ""}), self.fake.posts)


if __name__ == "__main__":
    unittest.main()
