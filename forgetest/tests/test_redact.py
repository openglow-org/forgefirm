# Copyright 2026 514 LLC d/b/a OpenGlow
# Written by Scott Wiederhold
# https://community.openglow.org
# SPDX-License-Identifier:    MIT

"""The release artifact carries no machine identity (redact.py).

The artifact is committed to a public repository. These hold the exporter
to that, and to not redacting what the gate recomputes.
"""
import json
import unittest

from forgetest.redact import scrub


class RedactTests(unittest.TestCase):

    def test_keyed_identity_is_replaced(self):
        out = scrub({"gf_serial": "15232020", "machine_id": "JTY-876",
                     "hostname": "forgefirm-b00a"})
        self.assertNotIn("15232020", json.dumps(out))
        self.assertNotIn("JTY-876", json.dumps(out))
        self.assertNotIn("forgefirm-b00a", json.dumps(out))

    def test_a_known_value_is_replaced_in_free_text_too(self):
        # A hostname in a log line is the same leak as one in its own field.
        out = scrub({"evidence": {"hostname": "forgefirm-b00a"},
                     "log": ["set hostname 'forgefirm-b00a' ok"]})
        self.assertNotIn("forgefirm-b00a", json.dumps(out))

    def test_addresses_and_macs_go(self):
        out = scrub(["ping 172.16.1.97", "wlan0 2C:6B:7D:0D:B0:0A up"])
        t = json.dumps(out)
        self.assertNotIn("172.16.1.97", t)
        self.assertNotIn("2C:6B:7D:0D:B0:0A", t)

    def test_loopback_is_kept(self):
        self.assertIn("127.0.0.1", json.dumps(scrub(["curl http://127.0.0.1:8090/"])))

    def test_numbering_is_stable_within_an_export(self):
        out = scrub(["a 10.0.0.5", "b 10.0.0.6", "c 10.0.0.5"])
        self.assertEqual(out[0].split()[1], out[2].split()[1])
        self.assertNotEqual(out[0].split()[1], out[1].split()[1])

    def test_integrity_values_survive(self):
        # The gate recomputes these; a long hex blob is what they look like.
        rec = {"fingerprint": "a" * 64, "manifest_sha": "b" * 64,
               "source_sha": "c" * 64, "sha256": "d" * 64,
               "catalog_hash": "e" * 64, "identity_sha": "f" * 64}
        self.assertEqual(scrub(rec), rec)

    def test_a_clock_time_is_not_an_address(self):
        out = scrub(["2026-09-18T18:50:11Z baseline: pre: clean"])
        self.assertIn("18:50:11", out[0])

    def test_result_fields_the_gate_reads_are_untouched(self):
        rec = {"result": "PASS", "ts": "2026-09-18T18:50:11Z",
               "campaign": "c-1", "fingerprint": "a" * 64}
        self.assertEqual(scrub(rec), rec)


if __name__ == "__main__":
    unittest.main()
