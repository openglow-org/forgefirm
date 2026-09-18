# Copyright 2026 514 LLC d/b/a OpenGlow
# Written by Scott Wiederhold
# https://community.openglow.org
# SPDX-License-Identifier:    MIT

import unittest

from forgetest.suite import image


class KernelIdentTests(unittest.TestCase):
    def test_localversion_hash_is_stripped(self):
        self.assertEqual(image.kernel_ident("6.12.20-fslc-fslc-g72a0b1431a9d"), "6.12.20-fslc-fslc")
        self.assertEqual(image.kernel_ident("6.12.20-fslc-fslc+g72a0b1431a9d"), "6.12.20-fslc-fslc")

    def test_plain_name_is_unchanged(self):
        self.assertEqual(image.kernel_ident("6.12.20-fslc-fslc"), "6.12.20-fslc-fslc")
        self.assertEqual(image.kernel_ident("6.12.20-fslc-g12"), "6.12.20-fslc-g12")

    def test_release_matches_manifest_with_or_without_hash(self):
        rel = "6.12.20-fslc-fslc-g72a0b1431a9d"
        self.assertTrue(image.kernel_matches(rel, ["6.12.20-fslc-fslc"]))
        self.assertTrue(image.kernel_matches(rel, ["6.12.20-fslc-fslc-gf52af5b522f4"]))
        self.assertTrue(image.kernel_matches(rel, []))
        self.assertFalse(image.kernel_matches(rel, ["6.12.19-fslc-fslc"]))


if __name__ == "__main__":
    unittest.main()
