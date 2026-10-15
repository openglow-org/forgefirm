# Copyright 2026 514 LLC d/b/a OpenGlow
# Written by Scott Wiederhold
# https://community.openglow.org
# SPDX-License-Identifier:    MIT

"""The setup.* suite on the host: the registration (ids, kinds, the
takeover tests, the operator tests' hands), the record builders, the
machine's name, the cookie parsing, the LED cue, and the settle rule
for a gated supervisor."""
import json
import os
import shutil
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(__file__))
import helpers  # noqa: E402
from forgetest import baseline, catalog  # noqa: E402
from forgetest.suite import setup  # noqa: E402

IDS = ("setup.account-login",
       "setup.ssh-until-reboot", "setup.machine-name", "setup.first-run-flow")
OPERATOR = ("setup.first-run-flow",)


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

    def test_the_record_swapping_tests_are_takeovers(self):
        self.assertEqual(self.reg["setup.first-run-flow"].hardware, "takeover")

    def test_recommended_reads_the_status_document(self):
        w = {"recommended": [{"id": "laser.floor", "reason": "the tube was replaced"},
                             {"id": "laser.focus", "reason": "the tray was replaced"},
                             {"id": "airflow", "reason": "not run yet"}, "stray"]}
        self.assertEqual(setup.recommended(w), {"laser.floor": "the tube was replaced",
                                                "laser.focus": "the tray was replaced",
                                                "airflow": "not run yet"})

    def test_the_form_wizards_and_the_required_steps(self):
        self.assertEqual(setup.FORM_WIZARDS, ("advisories", "account", "preferences", "machine", "cloud"))
        # in the order the gate's reason names them
        self.assertEqual(setup.REQUIRED_FORM_STEPS, ("preferences", "machine", "cloud"))
        self.assertEqual(setup.REQUIRED_STEPS, ("preferences", "machine", "cloud", "cooling.flow"))
        self.assertIn("cooling.flow", setup.REQUIRED_STEPS)

    def test_the_intake_tests_start_from_a_count(self):
        reg = self.reg
        from forgetest.suite import setup_dark
        self.assertIs(reg["setup.check-airflow"].precheck, setup_dark.intakes_counted)

    def test_the_first_run_flow_runs_unattended_with_the_fixture(self):
        flow = self.reg["setup.first-run-flow"]
        self.assertEqual(list(flow.actions), ["button"])
        self.assertFalse(flow.hands)
        self.assertTrue(flow.fixture_runnable(("button", "lid", "interlock")))

    def test_the_first_run_flow_covers_the_backend(self):
        flow = set(self.reg["setup.first-run-flow"].covers)
        for want in (("forgectrl", "src/wiz.*"), ("forgectrl", "src/setup.*"), ("forgectrl", "src/users.*"),
                     ("forgectrl", "src/button.*"), ("forgectrl", "src/led.*"), ("forgectrl", "src/advisories.*")):
            self.assertIn(want, flow, want)

    def test_the_only_attended_tests_need_a_workstation_or_a_scrap(self):
        # With the bench actuator up, two setup tests keep a person: the
        # print from the Glowforge app, and the focus burn on a fresh
        # scrap. Everything else runs from the queue.
        attended = sorted(tid for tid, t in self.reg.items() if tid.startswith("setup.")
                          and t.kind != "auto" and not t.fixture_runnable(("button", "lid", "interlock")))
        self.assertEqual(attended, ["setup.cloud-header-capture", "setup.focus"])

    def test_the_machine_name_covers_nothing_by_design(self):
        self.assertEqual(self.reg["setup.machine-name"].covers, ())

    def test_the_login_test_makes_its_own_account(self):
        # No bench credentials, no precheck: the test installs a temporary
        # account under a takeover and restores the real one.
        t = self.reg["setup.account-login"]
        self.assertIsNone(getattr(t, "precheck", None))
        self.assertEqual(t.hardware, "takeover")
        with open(setup.__file__) as f:
            src = f.read()
        for k in ("FORGETEST_LOGIN_NAME", "FORGETEST_LOGIN_PASSWORD"):
            self.assertNotIn(k, src)


class RecordTests(unittest.TestCase):
    STATUS = {"sheet_id": "ABCDE-FGHIJ",
              "documents": [{"id": "safety-and-risk", "hash": "a" * 64, "consent": "typed"},
                            {"id": "licenses", "hash": "b" * 64, "consent": "check"}],
              "wizards": [{"id": "advisories", "version": 1}, {"id": "account", "version": 1},
                          {"id": "machine", "version": 2}]}

    def test_complete_record_counts_as_set_up(self):
        rec = setup.complete_record(self.STATUS)
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
        rec = setup.complete_record(self.STATUS, base)
        self.assertEqual(rec["account"]["name"], "owner")
        self.assertEqual(rec["machine"], {"model": "pro"})
        self.assertEqual(rec["sheet_id"], "KKKKK-LLLLL")
        self.assertEqual(rec["created"], "then")

    def test_without_wizards_keeps_consent_and_account(self):
        rec = setup.complete_record(self.STATUS)
        rec["flags"] = {"machine": {"reason": "x"}}
        out = setup.record_without_wizards(rec)
        self.assertEqual(out["wizards"], {})
        self.assertEqual(out["flags"], {})
        self.assertEqual(out["advisories"], rec["advisories"])
        self.assertEqual(out["account"], rec["account"])
        self.assertEqual(rec["flags"], {"machine": {"reason": "x"}})   # a copy

    def test_with_required_steps_keeps_them_and_nothing_else(self):
        rec = setup.complete_record(self.STATUS)
        rec["wizards"]["machine"]["result"] = {"model": "basic"}
        rec["wizards"]["switches"] = {"version": 1}
        rec["flags"] = {"machine": {"reason": "the head was replaced"}}
        out = setup.record_with_required_steps(rec)
        self.assertEqual(sorted(out["wizards"]), ["cloud", "cooling.flow", "machine", "preferences"])
        self.assertEqual(out["wizards"]["machine"]["result"], {"model": "basic"})     # kept from the record
        self.assertEqual(out["wizards"]["preferences"]["version"], 1)               # written at version 1
        self.assertEqual(out["wizards"]["cooling.flow"]["version"], 1)
        self.assertEqual(out["flags"], {})
        self.assertEqual(out["account"], rec["account"])
        self.assertIn("switches", rec["wizards"])                                   # a copy

    def test_without_consent_keeps_the_account_and_the_wizards(self):
        rec = setup.complete_record(self.STATUS)
        out = setup.record_without_consent(rec)
        self.assertEqual(out["advisories"], {})
        self.assertNotIn("acceptance", out)
        self.assertEqual(out["account"], rec["account"])
        self.assertEqual(out["wizards"], rec["wizards"])
        self.assertIn("pressed_at", rec["acceptance"])                  # a copy

    def test_without_account_keeps_the_consent(self):
        rec = setup.complete_record(self.STATUS)
        out = setup.record_without_account(rec)
        self.assertNotIn("account", out)
        self.assertEqual(out["advisories"], rec["advisories"])
        self.assertEqual(out["acceptance"], rec["acceptance"])
        self.assertEqual(rec["account"]["name"], "bench")              # a copy

    def test_write_file_and_remove(self):
        tmp = tempfile.mkdtemp(prefix="forgetest-comm-")
        try:
            p = os.path.join(tmp, "sub", "setup.json")
            setup.write_file(p, b"{}\n")
            self.assertEqual(setup.read_file(p), b"{}\n")
            if os.name == "posix":              # a mode means nothing on a Windows host
                self.assertEqual(oct(os.stat(p).st_mode & 0o777), oct(0o600))
            setup.write_file(p, None)
            self.assertIsNone(setup.read_file(p))
            setup.write_file(p, None)          # a second remove is silent
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    def test_paths_follow_the_daemon_environment(self):
        os.environ["FORGECTRL_DATA_DIR"] = "/tmp/ffdata"
        os.environ["GF_RUN_DIR"] = "/tmp/ffrun"
        try:
            self.assertEqual(setup.record_path(), "/tmp/ffdata/setup.json")
            self.assertEqual(setup.users_path(), "/tmp/ffdata/users")
            self.assertEqual(setup.ssh_flag_path(), "/tmp/ffrun/ssh-enabled")
        finally:
            os.environ.pop("FORGECTRL_DATA_DIR", None)
            os.environ.pop("GF_RUN_DIR", None)


class MachineNameTests(unittest.TestCase):
    def setUp(self):
        self.root = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.root, ignore_errors=True)

    def _net(self, **devs):
        """A /sys/class/net tree; setup.mac_suffix reads it through
        setup.read_file, which takes an absolute path."""
        for dev, mac in devs.items():
            d = os.path.join(self.root, dev)
            os.makedirs(d)
            with open(os.path.join(d, "address"), "w") as f:
                f.write(mac + "\n")
        real = setup.read_file

        def read(path):
            head = "/sys/class/net/"
            if path.startswith(head):
                return real(os.path.join(self.root, path[len(head):]))
            return real(path)

        setup.read_file = read
        self.addCleanup(setattr, setup, "read_file", real)

    def test_the_wifi_address_names_the_machine(self):
        self._net(wlan0="2C:6B:7D:0D:B0:0A", eth0="00:11:22:33:44:55")
        self.assertEqual(setup.mac_suffix(), "b00a")

    def test_a_machine_with_no_wifi_falls_back_to_the_wired_address(self):
        self._net(eth0="00:11:22:33:44:55")
        self.assertEqual(setup.mac_suffix(), "4455")

    def test_an_unread_address_is_no_address(self):
        self._net(wlan0="00:00:00:00:00:00")
        self.assertEqual(setup.mac_suffix(), "")

    def test_no_interface_is_no_address(self):
        self._net()
        self.assertEqual(setup.mac_suffix(), "")


class SmallHelpersTests(unittest.TestCase):
    def test_cookie_parsing(self):
        sid = "ab" * 32
        value = "ffsid=%s; Path=/; HttpOnly; Secure; SameSite=Strict; Max-Age=43200" % sid
        self.assertEqual(setup.session_from_cookie(value), sid)
        self.assertEqual(setup.cookie_flags(value), {"path", "httponly", "secure", "samesite", "max-age"})
        self.assertIsNone(setup.session_from_cookie("ffsid=; Path=/; Max-Age=0"))
        self.assertIsNone(setup.session_from_cookie(""))

    def test_the_teal_breathe_cue(self):
        self.assertTrue(setup.breathes_teal({"target": [0, 180, 200], "pulse_on": [0, 1400, 1400],
                                                  "pulse_off": [0, 1400, 1400]}))
        self.assertFalse(setup.breathes_teal({"target": [0, 255, 40], "pulse_on": [0, 0, 0],
                                                   "pulse_off": [0, 0, 0]}))          # solid green
        self.assertFalse(setup.breathes_teal({"target": [200, 200, 200], "pulse_on": [1800, 1800, 1800],
                                                   "pulse_off": [1800] * 3}))         # white
        self.assertFalse(setup.breathes_teal({"target": [None, None, None], "pulse_on": [None] * 3}))
        self.assertFalse(setup.breathes_teal({}))

    def test_decode(self):
        self.assertEqual(setup.decode(b'{"a": 1}'), {"a": 1})
        self.assertEqual(setup.decode(b"cloud mode is not enabled"), "cloud mode is not enabled")


class GatedSettleTests(unittest.TestCase):
    """A supervisor that reports the setup gate closed is settled:
    it spawns nothing until the gate opens, so a takeover that installs
    a gating record must not wait the whole settle timeout for it."""

    def setUp(self):
        self.fake = helpers.FakeForgectrl().start()
        baseline.Baseline._unreachable_until = 0.0

    def tearDown(self):
        self.fake.stop()

    def test_gated_returns_at_once(self):
        self.fake.state["mode"] = {"mode": "grbl", "controller": "gated", "pid": 0, "motion": "unverified",
                                   "gated": True, "why": "the intake fans are not counted"}
        lines = []
        body = baseline.Baseline(lines.append).wait_settled(timeout=8)
        self.assertEqual(body["controller"], "gated")
        self.assertTrue(any("controller=gated" in ln for ln in lines))


class FirstRunSeedTests(unittest.TestCase):
    """The record the first-run tests install: a first run behind the
    checks and the burns."""

    STATUS = {"wizards": [{"id": "advisories", "version": 1, "class": "form"},
                          {"id": "account", "version": 1, "class": "form"},
                          {"id": "cloud", "version": 1, "class": "form"},
                          {"id": "switches", "version": 1, "class": "dark"},
                          {"id": "motion", "version": 2, "class": "dark"},
                          {"id": "laser.focus", "version": 1, "class": "live"}]}

    def test_no_consent_no_account_not_complete_and_every_check_done(self):
        base = {"created": "2026-09-01T00:00:00Z", "completed": "2026-09-02T00:00:00Z",
                "advisories": {"safety-and-risk": {"hash": "x"}}, "acceptance": {"pressed_at": "t"},
                "account": {"name": "scott", "uid": 1000}, "flags": {"flow_thin": True},
                "wizards": {"advisories": {"version": 1}, "switches": {"version": 1, "result": {"lid": True}}}}
        rec = setup.first_run_record(self.STATUS, base)
        self.assertNotIn("completed", rec)
        self.assertNotIn("acceptance", rec)
        self.assertNotIn("account", rec)
        self.assertEqual(rec["advisories"], {})
        self.assertEqual(rec["created"], "2026-09-01T00:00:00Z")
        self.assertEqual(rec["flags"], {"flow_thin": True})
        self.assertEqual(sorted(rec["wizards"]), ["laser.focus", "motion", "switches"])
        self.assertEqual(rec["wizards"]["switches"]["result"], {"lid": True})   # kept from the base
        self.assertEqual(rec["wizards"]["motion"]["version"], 2)                 # written at the catalog version

    def test_without_a_base_record(self):
        rec = setup.first_run_record(self.STATUS, None)
        self.assertEqual(sorted(rec["wizards"]), ["laser.focus", "motion", "switches"])
        self.assertEqual(rec["wizards"]["laser.focus"]["version"], 1)


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
            self.assertEqual(setup.sshd_policy(), {"permitrootlogin": "no", "passwordauthentication": "yes",
                                                        "permitemptypasswords": "no"})
        finally:
            hw.run = real
        self.assertEqual(calls, [["/usr/sbin/sshd", "-T"]])

    def test_a_failing_sshd_reports_the_error(self):
        from forgetest import hw
        real = hw.run
        hw.run = lambda cmd, timeout=60: (1, "sshd: no hostkeys available")
        try:
            self.assertEqual(setup.sshd_policy(), {"error": "sshd: no hostkeys available"})
        finally:
            hw.run = real


if __name__ == "__main__":
    unittest.main()
