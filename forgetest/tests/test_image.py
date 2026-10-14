# Copyright 2026 514 LLC d/b/a OpenGlow
# Written by Scott Wiederhold
# https://community.openglow.org
# SPDX-License-Identifier:    MIT

import unittest

from forgetest.suite import image


class NetworkBootTests(unittest.TestCase):
    STANZA = ("auto wlan0\n"
              "iface wlan0 inet dhcp\n"
              "\twpa-driver nl80211\n"
              "\tudhcpc_opts -x hostname:$(hostname)\n")

    def test_ifstate_names_the_interfaces_ifup_finished(self):
        self.assertEqual(image.ifstate_names("lo=lo\nwlan0=wlan0\n"), {"lo", "wlan0"})
        self.assertEqual(image.ifstate_names("lo=lo\n"), {"lo"})
        self.assertEqual(image.ifstate_names(""), set())
        self.assertEqual(image.ifstate_names(None), set())

    def test_an_interfaces_file_without_a_dhcpv6_client_is_clean(self):
        self.assertEqual(image.dhcp6_in_interfaces(self.STANZA), [])
        self.assertEqual(image.dhcp6_in_interfaces(None), [])

    def test_a_command_that_starts_a_dhcpv6_client_is_named(self):
        text = self.STANZA + ("iface wlan0 inet6 manual\n"
                              "\tup udhcpc6 -i wlan0 -b -S -p /run/udhcpc6.wlan0.pid\n")
        self.assertEqual(image.dhcp6_in_interfaces(text),
                         ["up udhcpc6 -i wlan0 -b -S -p /run/udhcpc6.wlan0.pid"])
        detached = "\tup start-stop-daemon -S -b -x /usr/bin/udhcpc6 -- -f -i wlan0\n"
        self.assertEqual(len(image.dhcp6_in_interfaces(self.STANZA + detached)), 1)

    def test_an_inet6_method_that_runs_a_client_is_named(self):
        self.assertEqual(image.dhcp6_in_interfaces("iface wlan0 inet6 dhcp\n"),
                         ["iface wlan0 inet6 dhcp"])
        self.assertEqual(image.dhcp6_in_interfaces("iface wlan0 inet6 static\n"), [])
        self.assertEqual(image.dhcp6_in_interfaces("iface wlan0 inet dhcp\n"), [])

    def test_a_comment_starts_nothing(self):
        text = self.STANZA + "# No DHCPv6 client runs; udhcpc6 is not on the image.\n"
        self.assertEqual(image.dhcp6_in_interfaces(text), [])

    def test_parse_stat_reads_the_parent_past_an_awkward_comm(self):
        self.assertEqual(image.parse_stat("371 (udhcpc) S 1 371 371 0 -1 4194624"),
                         (371, "udhcpc", 1))
        self.assertEqual(image.parse_stat("9 (a (b) c) S 7 9 9 0"), (9, "a (b) c", 7))
        self.assertIsNone(image.parse_stat(""))
        self.assertIsNone(image.parse_stat("12 (x) S"))

    def test_link_addrs_sorts_one_interface_by_scope(self):
        text = ("fe800000000000002e6b7dfffe0db00a 02 40 20 80    wlan0\n"
                "26001700000000000000000000001234 02 80 00 00    wlan0\n"
                "00000000000000000000000000000001 01 80 10 80       lo\n")
        local, world = image.link_addrs(text, "wlan0")
        self.assertEqual(local, ["fe800000000000002e6b7dfffe0db00a"])
        self.assertEqual(world, ["26001700000000000000000000001234"])
        self.assertEqual(image.link_addrs(text, "eth0"), ([], []))


if __name__ == "__main__":
    unittest.main()
