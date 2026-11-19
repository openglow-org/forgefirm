# Copyright 2026 514 LLC d/b/a OpenGlow
# Written by Scott Wiederhold
# https://community.openglow.org
# SPDX-License-Identifier:    MIT

"""The release artifact carries no machine identity (redact.py).

The artifact is committed to a public repository. These hold the exporter
to that, and to not redacting what the gate recomputes. Every name and
address here is made up: documentation addresses and domains (RFC 5737,
RFC 3849, RFC 7042, RFC 2606), hostnames with letters a hex suffix never
has, a machine id with letters outside its alphabet, and fuse words whose
bytes all differ, the serial below 23^5 so that its machine id has five
characters.
"""
import base64
import copy
import hashlib
import json
import os
import struct
import tempfile
import unittest
from unittest import mock

import helpers
from forgetest import artifact, campaign, catalog
from forgetest import fixture as fixture_mod
from forgetest import manifest as manifest_mod
from forgetest import redact
from forgetest.redact import Redactor
from test_campaign import rec_campaign, rec_result


def scrub(obj, identity=()):
    """One Redactor over one value tree, as the exporter uses it."""
    r = Redactor(identity)
    r.learn(obj)
    return r.value(obj)


SERIAL = 0x0012A3B4
SRK = [0x00112233, 0x44556677, 0x8899AABB, 0xCCDDEEFF, 0x0F1E2D3C, 0x4B5A6978, 0x8796A5B4, 0xC3D2E1F0]
CERT = b"forgetest: these bytes stand in for a certificate"
CERT_FP = ":".join("%02X" % b for b in hashlib.sha256(CERT).digest())
# forgectrl's data directory and settings file, away from their defaults
DATA = "/srv/ffdata"
CONF = "/srv/ffconf/forgefirm.conf"
ENV = {"FORGECTRL_DATA_DIR": DATA, "FORGECTRL_CONF": CONF}


def machine_tree(root, legacy=False):
    """A machine's filesystem with each of its names where the machine
    keeps it: the hostname file, an interface and the loopback, the fuses
    (the nvmem view, or the legacy fsl_otp files), forgectrl's data
    directory and settings, the wireless configuration and the bench
    actuator's config."""
    def put(path, data):
        p = os.path.join(root, path.lstrip("/"))
        os.makedirs(os.path.dirname(p), exist_ok=True)
        with open(p, "wb") as f:
            f.write(data if isinstance(data, bytes) else data.encode())

    put("/etc/hostname", "forgefirm-zzzy\n")
    put("/sys/class/net/wlan0/address", "00:00:5e:00:53:01\n")
    put("/sys/class/net/lo/address", "00:00:00:00:00:00\n")
    if legacy:
        put("/sys/fsl_otp/HW_OCOTP_MAC0", "0x%08x\n" % SERIAL)
        for i, w in enumerate(SRK):
            put("/sys/fsl_otp/HW_OCOTP_SRK%d" % i, "0x%08x\n" % w)
    else:
        nvmem = bytearray(256)
        nvmem[96:128] = struct.pack("<8I", *SRK)
        nvmem[136:140] = struct.pack("<I", SERIAL)
        put("/sys/bus/nvmem/devices/imx-ocotp0/nvmem", bytes(nvmem))
    put(DATA + "/tls/cert.pem", "-----BEGIN CERTIFICATE-----\n%s\n-----END CERTIFICATE-----\n"
        % base64.b64encode(CERT).decode())
    put(DATA + "/users", "# the account record\nzzowner:$6$x:1000\n\nzzsecond:$6$y:1001\nzq:$6$z:1002\n")
    put(DATA + "/panel.token", "0123456789abcdef0123456789abcdef\n")
    put(DATA + "/camera.key", "zz-camera-key-0001\n")
    put(CONF, "gf_serial = 4294967295\n# gf_password=zz-commented\ngf_password=zz-cloud-pass\n"
              "syslog_server=logs.example.net\nui_units=mm\n")
    put("/data/etc/wpa_supplicant.conf", 'network={\n  ssid="ZZ-Example-Net"\n  psk="zz-secret-phrase"\n'
        '  identity="zz-wifi-user"\n  password="zz-wifi-pass"\n  bssid=00:00:5e:00:53:ff\n}\n')
    # a Latin-1 byte in one line costs no other line
    put("/etc/wpa_supplicant.conf", b'network={\n  ssid="caf\xe9"\n  psk="zz-latin-psk"\n}\n')
    put(fixture_mod.FIXTURE_CONFIG, json.dumps({"hostname": "fixture-zz", "key": "k"}))


class RedactTests(unittest.TestCase):

    def test_keyed_identity_is_replaced(self):
        fp = "00:11:22:33:44:55:66:77:88:99:AA:BB:CC:DD:EE:FF:" * 2
        out = json.dumps(scrub({"gf_serial": "4294967295", "machine_id": "AAA-000",
                                "hostname": "forgefirm-zzzz", "tls_fingerprint": fp[:-1],
                                "sheet_id": "zz-sheet-01", "account_before": "zzbefore",
                                "temp_account": "zztemp", "serial": SERIAL}))
        for value in ("4294967295", "AAA-000", "forgefirm-zzzz", "zz-sheet-01", "zzbefore", "zztemp", str(SERIAL)):
            self.assertNotIn(value, out)
        # no part of the fingerprint, not even the octets a MAC match leaves
        self.assertNotIn("EE:FF", out)

    def test_a_known_value_is_replaced_in_free_text_too(self):
        # A hostname in a log line is the same leak as one in its own field,
        # and so is one in the key of a nested object; two keys that redact
        # alike both stay.
        out = scrub({"evidence": {"hostname": "forgefirm-zzzz",
                                  "answers": {"forgefirm-zzzz.local": "up", "FORGEFIRM-ZZZZ.local": "down"}},
                     "log": ["set hostname 'forgefirm-zzzz' ok"]})
        self.assertNotIn("forgefirm-zzzz", json.dumps(out).lower())
        self.assertEqual(sorted(out["evidence"]["answers"].values()), ["down", "up"])

    def test_the_default_names_are_never_known_values(self):
        line = "/usr/share/forgefirm/x on localhost via forgefixture and glowforge, forgetest"
        out = scrub({"evidence": {"fixture": {"hostname": "forgefixture"}, "host": "glowforge",
                                  "record_accounts": ["forgetest"]}, "log": [line]},
                    identity=[("hostname", "forgefirm", False), ("hostname", "localhost", False)])
        self.assertEqual(out["log"], [line])

    def test_addresses_and_macs_go(self):
        out = scrub(["ping 192.0.2.97", "wlan0 00:00:5E:00:53:0A up", "inet6 2001:db8::7/64 scope global",
                     "peer ::ffff:192.0.2.77", "via 64:ff9b::203.0.113.9", "at 2001:db8::192.0.2.1",
                     "mapped ::ffff:c000:24d"])
        t = json.dumps(out)
        for gone in ("192.0.2.97", "00:00:5E:00:53:0A", "2001:db8::7", "0.2.77", "0.113.9", "0.2.1", "c000:24d"):
            self.assertNotIn(gone, t)

    def test_loopback_unspecified_and_text_that_is_no_address_are_kept(self):
        # (a colon run glued to a word, "g1:2a:3b:4c", is part of the word)
        kept = ("curl http://127.0.0.1:8090/", "listen [::1]:80", "cgroup 0::/forgefirm.slice",
                "std::string", "frame 01:02:03:04", "rev g1:2a:3b:4c")
        self.assertEqual(scrub(list(kept)), list(kept))

    def test_numbering_is_stable_within_an_export(self):
        out = scrub(["a 192.0.2.5", "b 192.0.2.6", "c 192.0.2.5"])
        self.assertEqual(out[0].split()[1], out[2].split()[1])
        self.assertNotEqual(out[0].split()[1], out[1].split()[1])

    def test_a_clock_time_is_not_an_address(self):
        out = scrub(["2026-09-18T18:50:11Z baseline: pre: clean"])
        self.assertIn("18:50:11", out[0])

    def test_machine_identity_reads_each_name_where_the_machine_keeps_it(self):
        want = {("hostname", "forgefirm-zzzx", False), ("hostname_file", "forgefirm-zzzy", False),
                ("mac", "00:00:5e:00:53:01", False), ("mac", "00005e005301", False),
                ("mac", "00-00-5e-00-53-01", False), ("mac", "00%3a00%3a5e%3a00%3a53%3a01", False),
                ("mac_suffix", "5301", True), ("fixture", "fixture-zz", False),
                ("tls_fingerprint", CERT_FP, False), ("tls_fingerprint", CERT_FP.replace(":", ""), False),
                ("account", "zzowner", True), ("account", "zzsecond", True), ("account", "zq", True),
                ("panel_token", "0123456789abcdef0123456789abcdef", False),
                ("camera_key", "zz-camera-key-0001", False),
                ("gf_serial", "4294967295", False), ("gf_password", "zz-cloud-pass", False),
                ("syslog_server", "logs.example.net", False),
                ("ssid", "ZZ-Example-Net", False), ("psk", "zz-secret-phrase", False),
                ("wifi_identity", "zz-wifi-user", False), ("wifi_password", "zz-wifi-pass", False),
                ("bssid", "00:00:5e:00:53:ff", False),
                ("ssid", "caf\udce9", False), ("psk", "zz-latin-psk", False),
                ("serial", "1221556", False), ("serial", "0012a3b4", False), ("machine_id", "GMQ-GF", False),
                ("password", "00112233445566778899aabbccddeeff0f1e2d3c4b5a69788796a5b4c3d2e1f0", False)}
        for legacy in (False, True):
            with self.subTest(legacy=legacy), tempfile.TemporaryDirectory() as root, \
                    mock.patch.object(redact.socket, "gethostname", return_value="forgefirm-zzzx"), \
                    mock.patch.dict(os.environ, ENV):
                machine_tree(root, legacy)
                self.assertEqual(set(redact.machine_identity(root)), want)


class ExportTests(unittest.TestCase):
    """The exporter's redaction: one Redactor per export (artifact.redact)."""

    def setUp(self):
        self.man = helpers.make_manifest()
        self.a = helpers.make_test("image.health", [("linux-fslc", "**")], always=True)
        self.b = helpers.make_test("forgectrl.auth", [("forgectrl", "src/auth.c")])
        self.reg = helpers.registry(self.a, self.b)
        self.tests = list(self.reg.values())
        self.chash = catalog.catalog_hash(self.reg)

    def build(self, recs, identity=()):
        st = campaign.compute(recs, self.tests, self.man, self.chash)
        return json.loads(artifact.to_json(artifact.build(st, self.tests, self.man, recs, self.chash,
                                                          identity=identity)))

    def export(self, evidence, cid="c1", identity=(), ts="2026-08-20T10:00:00Z", after=()):
        """The artifact of one campaign in which each test passed with the
        record fields `evidence` gives it (test id -> fields), and the
        records `after` after them."""
        recs = [rec_campaign(cid, self.man, self.chash, ts)]
        for t in self.tests:
            recs.append(dict(rec_result(cid, t, self.man, "PASS", ts), **evidence.get(t.id, {})))
        return self.build(recs + list(after), identity)

    def release(self):
        data = copy.deepcopy(self.man.data)
        data["components"].pop("forgetest", None)
        return manifest_mod.Manifest(data)

    def test_the_machines_own_names_go_where_no_key_holds_them(self):
        # Only inside log lines and messages, under no identifying key: the
        # export is seeded with what the machine it runs on is called.
        lines = ["udhcpc -x hostname:forgefirm-zzzx", "hostname file forgefirm-zzzy",
                 "hwaddr 00005e005301 00-00-5e-00-53-01 q=00%3A00%3A5E%3A00%3A53%3A01, name suffix 5301, rev 1a5301b",
                 "forgefirm-users: account zzsecond created", "wlan0: joined ZZ-Example-Net",
                 "fixture fixture-zz up", "machine GMQ-GF, serial 1221556, cloud as 4294967295"]
        with tempfile.TemporaryDirectory() as root, \
                mock.patch.object(redact, "MACHINE_ROOT", root), \
                mock.patch.object(redact.socket, "gethostname", return_value="forgefirm-zzzx"), \
                mock.patch.dict(os.environ, ENV):
            machine_tree(root)
            art = self.export({self.a.id: {"log": lines[:5]},
                               self.b.id: {"message": "; ".join(lines[5:]), "evidence": {"fuse_word": SERIAL}}},
                              identity=None)
        out = json.dumps(art).lower()
        for name in ("forgefirm-zzzx", "forgefirm-zzzy", "00005e005301", "00-00-5e-00-53-01", "00%3a00%3a5e",
                     " 5301", "zzsecond", "zz-example-net", "fixture-zz", "gmq-gf", "1221556", "4294967295"):
            self.assertNotIn(name, out)
        # the suffix goes only where it stands alone, not out of a longer word
        self.assertIn("rev 1a5301b", out)

    def test_a_name_one_record_holds_goes_from_every_record(self):
        art = self.export({self.a.id: {"evidence": {"hostname": "forgefirm-zzzz",
                                                    "first": "192.0.2.7", "second": "192.0.2.8"}},
                           self.b.id: {"evidence": {"hostname_file": "forgefirm-zzzz"},
                                       "log": ["lease 192.0.2.8 for forgefirm-zzzz"]}})
        by = {t["id"]: t["record"] for t in art["tests"]}
        self.assertNotIn("forgefirm-zzzz", json.dumps(art))
        # one value keeps one tag across the records and the fields
        line = by[self.b.id]["log"][0].split()
        self.assertEqual(line[1], by[self.a.id]["evidence"]["second"])
        self.assertEqual({line[3], by[self.b.id]["evidence"]["hostname_file"]},
                         {by[self.a.id]["evidence"]["hostname"]})

    def test_an_account_name_in_record_accounts_goes_from_every_record(self):
        art = self.export({self.a.id: {"evidence": {"record_accounts": ["zzowner", "qx"]}},
                           self.b.id: {"log": ["forgefirm-users: account zzowner created"]}})
        out = json.dumps(art)
        self.assertNotIn("zzowner", out)
        self.assertNotIn('"qx"', out)

    def test_a_word_known_from_a_field_goes_wherever_it_appears(self):
        art = self.export({self.a.id: {"evidence": {"mac_suffix": "5301"}}, self.b.id: {"log": ["rev 1a5301b"]}},
                          identity=[("mac_suffix", "5301", True)])
        self.assertNotIn("1a5301b", json.dumps(art))

    def test_a_tag_already_in_a_record_stays_and_no_new_tag_takes_its_number(self):
        # forgectrl's log export writes the same tags: one tag, one value
        art = self.export({self.a.id: {"evidence": {"hostname": "<REDACTED-1>"},
                                       "log": ["bundle: peer <IP-1>"]},
                           self.b.id: {"log": ["ping 192.0.2.9"]}})
        by = {t["id"]: t["record"] for t in art["tests"]}
        self.assertEqual((by[self.a.id]["evidence"]["hostname"], by[self.a.id]["log"][0]),
                         ("<REDACTED-1>", "bundle: peer <IP-1>"))
        self.assertNotIn("<IP-1>", by[self.b.id]["log"][0])

    def fail_then_pass(self, message, reason="a loose clamp"):
        """The second test failed with `message`, the operator answered it
        with `reason`, and both tests passed in the next campaign."""
        fp = self.b.fingerprint(self.man)
        return [rec_campaign("c1", self.man, self.chash, "2026-08-20T10:00:00Z"),
                rec_result("c1", self.a, self.man, "PASS", "2026-08-20T10:01:00Z"),
                dict(rec_result("c1", self.b, self.man, "FAIL", "2026-08-20T10:02:00Z"), needs_disposition=True,
                     message=message),
                rec_campaign("c2", self.man, self.chash, "2026-08-20T10:03:00Z"),
                {"t": "disposition", "ts": "2026-08-20T10:04:00Z", "test": self.b.id, "fingerprint": fp,
                 "reason": reason},
                dict(rec_result("c2", self.a, self.man, "PASS", "2026-08-20T10:05:00Z"), log=["ping 192.0.2.9"]),
                rec_result("c2", self.b, self.man, "PASS", "2026-08-20T10:06:00Z")]

    def test_the_results_beside_a_pass_are_redacted_with_the_rest(self):
        # A FAIL and its disposition naming the host go; a tag in the FAIL's
        # message keeps its number to itself.
        art = self.build(self.fail_then_pass("lost forgefirm-zzzx; bundle peer <IP-1>", "rebooted forgefirm-zzzx"),
                         identity=[("hostname", "forgefirm-zzzx", False)])
        by = {t["id"]: t for t in art["tests"]}
        self.assertEqual([o["result"] for o in by[self.b.id]["others"]], ["FAIL"])
        self.assertNotIn("forgefirm-zzzx", json.dumps(art))
        self.assertIn("<IP-1>", by[self.b.id]["others"][0]["message"])
        self.assertNotIn("<IP-1>", by[self.a.id]["record"]["log"][0])

    def test_a_short_name_of_the_machine_goes_from_a_fails_message(self):
        # An account name from 2 characters, as a whole word; another of the
        # machine's names from forgectrl's floor of 3 (5 when all digits).
        art = self.build(self.fail_then_pass("record account 'zqx' is missing; 'zq' too; joined zzq; pin 1234 left"),
                         identity=[("account", "zqx", True), ("account", "zq", True), ("ssid", "zzq", False),
                                   ("gf_serial", "1234", False)])
        message = {t["id"]: t for t in art["tests"]}[self.b.id]["others"][0]["message"]
        for name in ("zqx", "'zq'", "zzq"):
            self.assertNotIn(name, message)
        self.assertIn("pin 1234 left", message)

    def test_what_the_gate_judges_stays_and_still_refuses_a_stale_pass(self):
        # Known values that occur inside "PASS", "FAIL", "laser_latch" and
        # the key "item", and all-decimal words equal to the places in a log
        # past 1000 records of the PASS, the FAIL and the invalidate-all:
        # the results, their places, the leftover items and the epoch stay
        # as recorded, so the gate still refuses a PASS older than the FAIL,
        # or older than the leftover on the laser's safety state.
        fp = self.b.fingerprint(self.man)
        recs = [{"t": "note", "ts": "2026-08-20T09:00:00Z"} for _ in range(1200)]
        recs += [rec_campaign("c1", self.man, self.chash, "2026-08-20T10:00:00Z"),
                 rec_result("c1", self.a, self.man, "PASS", "2026-08-20T10:01:00Z"),
                 dict(rec_result("c1", self.b, self.man, "FAIL", "2026-08-20T10:02:00Z"), needs_disposition=True),
                 {"t": "invalidate", "ts": "2026-08-20T10:02:30Z", "reason": "reflashed"},
                 rec_campaign("c2", self.man, self.chash, "2026-08-20T10:03:00Z"),
                 {"t": "disposition", "ts": "2026-08-20T10:04:00Z", "test": self.b.id, "fingerprint": fp,
                  "reason": "a loose clamp"},
                 {"t": "leftover", "ts": "2026-08-20T10:05:00Z", "campaign": "c2", "test": self.b.id,
                  "fingerprint": fp, "found_before": "test image.health", "needs_disposition": True,
                  "leftovers": [{"item": "cnc/motor_lock", "found": "15", "expected": "0", "action": "restored"},
                                {"item": "laser_latch", "found": "unlocked", "expected": "locked",
                                 "action": "restored"}]},
                 {"t": "disposition", "ts": "2026-08-20T10:06:00Z", "test": self.b.id, "fingerprint": fp,
                  "reason": "the operator jogged the head"},
                 rec_result("c2", self.a, self.man, "PASS", "2026-08-20T10:07:00Z"),
                 rec_result("c2", self.b, self.man, "PASS", "2026-08-20T10:08:00Z")]
        pos = campaign.positions(recs)
        passed, failed, cut, left = (pos[id(r)] for r in (recs[-1], recs[1202], recs[1203], recs[1206]))
        art = self.build(recs, identity=[("x", "pass", False), ("x", "fail", False), ("x", "latch", False),
                                         ("x", "item", False)] +
                         [("mac_suffix", str(n), True) for n in (passed, failed, cut)])
        entry = {t["id"]: t for t in art["tests"]}[self.b.id]
        self.assertEqual((entry["record"]["result"], entry["record"]["seq"]), ("PASS", passed))
        self.assertEqual([(o["result"], o["seq"]) for o in entry["others"]], [("FAIL", failed), ("LEFTOVER", left)])
        self.assertEqual([x["item"] for x in entry["others"][1]["leftovers"]], ["cnc/motor_lock", "laser_latch"])
        self.assertEqual(art["invalidate_epoch"]["seq"], cut)

        def row(art):
            _ok, rows, _problems = artifact.verify(art, self.release(), self.tests, self.chash)
            return [r for r in rows if r["id"] == self.b.id][0]["ok"]

        self.assertTrue(row(art))
        for i in (0, 1):
            body = copy.deepcopy(art)
            body.pop("sha256")
            body["tests"][[t["id"] for t in body["tests"]].index(self.b.id)]["others"][i]["seq"] = \
                entry["record"]["seq"] + 1
            body["sha256"] = artifact.body_sha(body)
            with self.subTest(newer=entry["others"][i]["result"]):
                self.assertFalse(row(body))

    def test_integrity_values_are_kept_by_place_not_by_key_name(self):
        # A test's own evidence named like an integrity value is redacted;
        # what the gate reads stays as recorded, even where one of the
        # machine's names (here a suffix) occurs inside it, and the gate
        # accepts the artifact.
        cid = "c-20260820100000-2026"
        art = self.export({self.a.id: {"evidence": {"fingerprint": "ab" * 32, "sha256": "cd" * 32}}}, cid=cid,
                          identity=[("mac_suffix", "2026", True)])
        rec = {t["id"]: t["record"] for t in art["tests"]}[self.a.id]
        self.assertNotIn("ab" * 32, json.dumps(rec["evidence"]))
        self.assertNotIn("cd" * 32, json.dumps(rec["evidence"]))
        self.assertEqual((rec["fingerprint"], rec["manifest_sha"], rec["campaign"], rec["ts"], rec["result"]),
                         (self.a.fingerprint(self.man), self.man.content_sha, cid, "2026-08-20T10:00:00Z", "PASS"))
        ok, _rows, problems = artifact.verify(art, self.release(), self.tests, self.chash)
        self.assertEqual(problems, [])

    def test_the_reason_typed_for_an_invalidate_all_is_redacted(self):
        art = self.export({}, identity=[("hostname", "forgefirm-zzzx", False), ("mac_suffix", "2026", True)],
                          after=[{"t": "invalidate", "ts": "2026-08-20T11:00:00Z",
                                  "reason": "reflashed forgefirm-zzzx at 192.0.2.44"}])
        top = json.dumps([art["invalidate"], art["invalidate_epoch"]])
        self.assertNotIn("forgefirm-zzzx", top)
        self.assertNotIn("192.0.2.44", top)
        self.assertEqual((art["invalidate_epoch"]["seq"], art["invalidate_epoch"]["ts"]), (4, "2026-08-20T11:00:00Z"))


if __name__ == "__main__":
    unittest.main()
