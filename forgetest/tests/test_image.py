# Copyright 2026 514 LLC d/b/a OpenGlow
# Written by Scott Wiederhold
# https://community.openglow.org
# SPDX-License-Identifier:    MIT

import os
import shutil
import subprocess
import tarfile
import tempfile
import unittest

from forgetest.suite import image

# The layer's scripts, when the tests run from a checkout of forgefirm.
LAYER = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))),
                     "meta-forgefirm", "recipes-forgefirm")
# The BSP layer's interfaces file, from the meta-openglow checkout beside
# forgefirm's (forgetest-ci checks out its scarthgap branch there).
BSP_INTERFACES = os.path.join(
    os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))),
    "meta-openglow", "meta-openglow-core", "recipes-core", "init-ifupdown", "init-ifupdown", "interfaces")

# A layer script run as the image runs it, in private mount, UTS and
# network namespaces: /run a fresh tmpfs, the scratch tree's net directory
# over /sys/class/net, and /etc read-only with the scratch tree's files
# over the host's (an overlay with no upper layer, as the image's rootfs is
# read-only), so the script makes its own copy under /run/forgefirm and
# writes through its own bind mount. With "net", wlan0 is a veth carrying a
# global IPv4 and IPv6 address and a link-local one. Exit 97: the
# namespace could not be set up.
NAMESPACE = """
T=$1; SCRIPT=$2
mount --make-rprivate / && mount -t tmpfs tmpfs /run &&
    mount --bind "$T/net" /sys/class/net &&
    mount -t overlay overlay -o "lowerdir=$T/etc:/etc" /etc || exit 97
if [ "$3" = net ]; then
    ip link add wlan0 type veth peer name vpeer0 &&
        ip addr add 192.0.2.5/24 dev wlan0 &&
        ip -6 addr add 2001:db8::5/64 dev wlan0 nodad &&
        ip -6 addr add fe80::5/64 dev wlan0 nodad &&
        ip link set wlan0 up || exit 97
fi
sh "$SCRIPT"
echo "rc=$?"
echo "name=$(hostname)"
echo "file=$(cat /etc/hostname)"
echo "--- issue"
cat /etc/issue
"""


def run_layer_script(script, macs, etc, net=False):
    """Run a layer script over a scratch tree: macs {interface: address or
    None for a directory with no address file}, etc {file: text}. Returns
    the namespace's output, or None when no namespace can be made here
    (unprivileged where user namespaces are open, else root via sudo -n)."""
    tree = tempfile.mkdtemp(prefix="forgetest-net.")
    try:
        for dev, mac in macs.items():
            os.makedirs(os.path.join(tree, "net", dev))
            if mac is not None:
                with open(os.path.join(tree, "net", dev, "address"), "w") as f:
                    f.write(mac + "\n")
        os.makedirs(os.path.join(tree, "net"), exist_ok=True)
        os.makedirs(os.path.join(tree, "etc"))
        for name, text in etc.items():
            with open(os.path.join(tree, "etc", name), "w") as f:
                f.write(text)
        args = ["sh", "-c", NAMESPACE, "sh", tree, os.path.join(LAYER, script), "net" if net else ""]
        setup = []
        for cmd in (["unshare", "-Urmun"], ["sudo", "-n", "unshare", "-mun"]):
            try:
                p = subprocess.run(cmd + args, capture_output=True, text=True, timeout=60)
            except (OSError, subprocess.TimeoutExpired):
                continue
            if "rc=" in p.stdout:
                return p.stdout
            if p.returncode == 97:
                setup.append(p.stderr.strip())
        if setup:
            raise AssertionError("the namespace was made but not set up: %s" % setup)
        return None
    finally:
        shutil.rmtree(tree, ignore_errors=True)


def field(out, key):
    return [ln[len(key) + 1:] for ln in out.splitlines() if ln.startswith(key + "=")][0]


@unittest.skipUnless(os.path.isdir(LAYER) and shutil.which("unshare"), "needs Linux and a forgefirm checkout")
class MachineNameTests(unittest.TestCase):
    """forgefirm-hostname and forgefirm-banner, the layer's own files."""

    def named(self, macs):
        out = run_layer_script("forgefirm-hostname/files/forgefirm-hostname", macs, {"hostname": "forgefirm\n"})
        if out is None:
            self.skipTest("no namespace can be made here")
        self.assertEqual(field(out, "rc"), "0", out)
        self.assertEqual(field(out, "file"), field(out, "name"), out)
        return field(out, "name")

    def test_the_name_is_built_from_the_first_interface_with_an_address(self):
        self.assertEqual(self.named({"wlan0": "02:00:00:00:12:AB", "eth0": "02:00:00:00:34:cd"}), "forgefirm-12ab")
        self.assertEqual(self.named({"eth0": "02:00:00:00:34:cd"}), "forgefirm-34cd")
        self.assertEqual(self.named({"wlan0": "00:00:00:00:00:00", "eth0": "02:00:00:00:34:cd"}), "forgefirm-34cd")

    def test_the_banner_adds_the_global_addresses_and_nothing_else(self):
        base = "ForgeFIRM\n"
        out = run_layer_script("forgefirm-banner/files/forgefirm-banner", {"wlan0": "02:00:00:00:12:ab"},
                               {"issue": base}, net=True)
        if out is None:
            self.skipTest("no namespace can be made here")
        self.assertEqual(field(out, "rc"), "0", out)
        issue = out.split("--- issue\n", 1)[1]
        self.assertTrue(issue.startswith(base + "\n"), issue)
        heading, *urls = issue[len(base) + 1:].strip("\n").split("\n")
        self.assertEqual(urls, ["  https://192.0.2.5/", "  https://[2001:db8::5]/"], issue)
        self.assertFalse(heading.startswith("#") or ".local" in issue, issue)


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

    def test_the_layer_interfaces_file_starts_no_dhcpv6_client(self):
        self.assertTrue(os.path.isfile(BSP_INTERFACES),
                        "%s is missing: meta-openglow is checked out beside forgefirm" % BSP_INTERFACES)
        with open(BSP_INTERFACES, encoding="utf-8") as f:
            self.assertEqual(image.dhcp6_in_interfaces(f.read()), [])

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


class PackLicensesTests(unittest.TestCase):
    """pack-licenses.sh, the image's license step, on a fixture tree."""

    RECIPES = ("forgectrl", "grblhal-glowforge", "kernel-module-glowforge")

    def setUp(self):
        self.root = tempfile.mkdtemp(prefix="forgetest-lic.")
        self.addCleanup(shutil.rmtree, self.root, ignore_errors=True)
        self.tree = os.path.join(self.root, "common-licenses")
        self.manifest = os.path.join(self.tree, "license.manifest")
        self.out = os.path.join(self.root, "forgefirm", "licenses.tar.gz")

    def make_tree(self, recipes=None):
        """The tree, with a manifest naming `recipes` (None: no manifest)."""
        os.makedirs(os.path.join(self.tree, "generic_MIT"))
        with open(os.path.join(self.tree, "generic_MIT", "generic_MIT"), "w") as f:
            f.write("MIT License\n")
        if recipes is not None:
            with open(self.manifest, "w") as f:
                for r in recipes:
                    f.write("PACKAGE NAME: %s\nPACKAGE VERSION: 1.0\nRECIPE NAME: %s\nLICENSE: MIT\n\n" % (r, r))

    def pack(self):
        return subprocess.run(["sh", os.path.join(LAYER, "images", "pack-licenses.sh"), self.tree, self.out],
                              capture_output=True, text=True, timeout=60)

    def assertRefused(self, p):
        self.assertNotEqual(p.returncode, 0, p.stdout)
        self.assertTrue(p.stderr.strip(), "a refusal with no message on stderr")
        self.assertFalse(os.path.exists(self.out), "a refusal wrote the tar")

    def test_a_missing_tree_is_refused(self):
        self.assertRefused(self.pack())

    def test_a_tree_with_no_manifest_is_refused_and_left(self):
        self.make_tree()
        self.assertRefused(self.pack())
        self.assertTrue(os.path.isdir(self.tree))

    def test_a_manifest_that_leaves_out_a_recipe_is_refused_and_left(self):
        for left_out in self.RECIPES:
            with self.subTest(left_out=left_out):
                shutil.rmtree(self.tree, ignore_errors=True)
                self.make_tree([r for r in self.RECIPES if r != left_out])
                self.assertRefused(self.pack())
                self.assertTrue(os.path.isfile(self.manifest))

    def test_a_manifest_that_names_all_three_is_packed_and_the_tree_removed(self):
        self.make_tree(self.RECIPES)
        p = self.pack()
        self.assertEqual(p.returncode, 0, p.stderr)
        with tarfile.open(self.out, "r:gz") as t:
            self.assertIn("common-licenses/license.manifest", t.getnames())
        self.assertFalse(os.path.exists(self.tree))


class RootfsLintTests(unittest.TestCase):
    """rootfs-lint.sh, the image's last rootfs step, on a fixture tree: a
    tree that holds every rule passes, and a drift fails the lint naming
    its rule."""

    SSHD = ("Include /etc/ssh/sshd_config.d/*.conf\n"
            "PermitRootLogin no\n"
            "PasswordAuthentication yes\n"
            "PermitEmptyPasswords no\n"
            "Subsystem\tsftp\t/usr/libexec/sftp-server\n")

    def setUp(self):
        self.root = tempfile.mkdtemp(prefix="forgetest-lint.")
        self.addCleanup(shutil.rmtree, self.root, ignore_errors=True)
        self.put("usr/sbin/rsyslogd", "", mode=0o755)
        self.put("etc/init.d/syslog", "#!/bin/sh\nRSYSLOGD=rsyslogd\nRSYSLOGD_BIN=/usr/sbin/rsyslogd\n", mode=0o755)
        os.makedirs(self.path("etc/rc5.d"))
        os.symlink("../init.d/syslog", self.path("etc/rc5.d/S20syslog"))
        self.put("etc/ssh/sshd_config", self.SSHD)
        self.put("etc/ssh/sshd_config_readonly", self.SSHD)
        self.put("etc/network/interfaces", "auto lo\niface lo inet loopback\n" + NetworkBootTests.STANZA)
        self.put("etc/nsswitch.conf", "hosts:          files dns\n")

    def path(self, rel):
        return os.path.join(self.root, rel)

    def put(self, rel, text, mode=0o644):
        os.makedirs(os.path.dirname(self.path(rel)), exist_ok=True)
        with open(self.path(rel), "w") as f:
            f.write(text)
        os.chmod(self.path(rel), mode)

    def lint(self, kind="release"):
        return subprocess.run(["sh", os.path.join(LAYER, "images", "rootfs-lint.sh"), self.root, kind],
                              capture_output=True, text=True, timeout=60)

    def assertFails(self, rule, kind="release"):
        p = self.lint(kind)
        self.assertEqual(p.returncode, 1, p.stderr)
        self.assertIn("rootfs-lint: %s:" % rule, p.stderr)

    def test_a_tree_that_holds_every_rule_passes(self):
        for kind in ("release", "dev"):
            p = self.lint(kind)
            self.assertEqual(p.returncode, 0, "%s: %s" % (kind, p.stderr))

    def test_the_layer_interfaces_file_sends_the_name(self):
        with open(BSP_INTERFACES, encoding="utf-8") as f:
            self.put("etc/network/interfaces", f.read())
        p = self.lint()
        self.assertEqual(p.returncode, 0, p.stderr)

    def test_busybox_syslogd_beside_rsyslog_fails(self):
        os.makedirs(self.path("sbin"))
        os.symlink("/bin/busybox.nosuid", self.path("sbin/syslogd"))
        self.assertFails("syslog")

    def test_a_pre_authentication_banner_fails_either_image(self):
        self.put("etc/ssh/sshd_config_readonly", self.SSHD + "Banner /etc/issue.net\n")
        for kind in ("release", "dev"):
            self.assertFails("sshd", kind)

    def test_root_and_an_empty_password_over_ssh_fail_a_release_image_only(self):
        dev = self.SSHD.replace("PermitRootLogin no", "PermitRootLogin yes").replace(
            "PermitEmptyPasswords no", "PermitEmptyPasswords yes")
        self.put("etc/ssh/sshd_config", dev)
        self.put("etc/ssh/sshd_config_readonly", dev)
        self.assertFails("sshd", "release")
        p = self.lint("dev")
        self.assertEqual(p.returncode, 0, p.stderr)

    def test_a_dhcp_stanza_that_does_not_send_the_name_fails(self):
        self.put("etc/network/interfaces", NetworkBootTests.STANZA.replace(
            "\tudhcpc_opts -x hostname:$(hostname)\n", ""))
        self.assertFails("dhcp-name")

    def test_an_mdns_responder_fails(self):
        os.makedirs(self.path("etc/avahi"))
        self.assertFails("mdns")


if __name__ == "__main__":
    unittest.main()
