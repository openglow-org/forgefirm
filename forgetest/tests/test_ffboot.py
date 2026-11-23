# Copyright 2026 514 LLC d/b/a OpenGlow
# Written by Scott Wiederhold
# https://community.openglow.org
# SPDX-License-Identifier:    MIT

"""ffboot - the boot selection, on a host.

ffboot's functions (the script without its last two lines, which run
main), run against a saved
environment kept in a file (stand-ins for fw_printenv and fw_setenv) and
slots that are directories (the probe's mount a link to one): the switch
writes the four variables in one transaction and reads them back, or
writes nothing; the content probe gates a switch and -f overrides it; a
slot an update left marked incomplete is never selected, -f or not, and
-e passes it by; the update lock refuses a switch; the inventory prints
one line per version, whatever the version file holds."""
import fcntl
import os
import shutil
import stat
import subprocess
import tempfile
import unittest

import helpers  # noqa: F401  (sys.path)

REPO = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
FFBOOT = os.path.join(REPO, "meta-forgefirm", "recipes-forgefirm", "ffboot", "files", "ffboot")
TAIL = 'main "$@"\nexit $?\n'


def functions():
    """ffboot up to its last two lines, which run main: its functions and
    its constants, which the test then sets again."""
    with open(FFBOOT, encoding="utf-8") as f:
        text = f.read()
    assert text.endswith(TAIL), "ffboot no longer ends by running main"
    return text[:-len(TAIL)]

# The saved environment as name=value lines ($ENV_FILE); fw_setenv -s takes
# a "name=value" script (libubootenv's form) unless $SETENV_DEAD exists, and
# a single-variable write always takes. Every fw_setenv call is recorded.
FAKE_PRINTENV = r"""#!/bin/sh
while [ $# -gt 0 ]; do case $1 in -n) N=$2; shift ;; esac; shift; done
grep -q "^$N=" "$ENV_FILE" || exit 1
sed -n "s/^$N=//p" "$ENV_FILE"
"""
FAKE_SETENV = r"""#!/bin/sh
echo "$*" >> "$SETENV_CALLS"
shift 2
put () { grep -v "^$1=" "$ENV_FILE" > "$ENV_FILE.new"; echo "$1=$2" >> "$ENV_FILE.new"; mv "$ENV_FILE.new" "$ENV_FILE"; }
if [ "$1" = -s ]; then
  [ ! -e "$SETENV_DEAD" ] || exit 1
  grep -q '=' "$2" || exit 1
  while IFS== read -r k v; do put "$k" "$v"; done < "$2"
  exit 0
fi
put "$1" "$2"
"""
# The probe's unmount of a slot that stands as a link.
FAKE_UMOUNT = "#!/bin/sh\n[ -L \"$1\" ] && rm -f \"$1\"\nexit 0\n"
FAKE_REBOOT = "#!/bin/sh\necho reboot >> \"$REBOOTS\"\n"

# Each slot is the directory $SLOTS/<device name>; the probe's mount puts
# a link to it where the mount point was.
STANDINS = r"""
part_exists () { [ -d "$SLOTS/${1##*/}" ]; }
part_mount () { rmdir "$2" && ln -s "$SLOTS/${1##*/}" "$2"; }
"""


@unittest.skipUnless(shutil.which("sh") and os.name == "posix", "needs a POSIX sh")
class FfbootTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="ffboot-test.")
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.bin = os.path.join(self.tmp, "bin")
        os.mkdir(self.bin)
        for name, body in (("fw_printenv", FAKE_PRINTENV), ("fw_setenv", FAKE_SETENV),
                           ("umount", FAKE_UMOUNT), ("reboot", FAKE_REBOOT)):
            path = os.path.join(self.bin, name)
            with open(path, "w", newline="\n") as f:
                f.write(body)
            os.chmod(path, os.stat(path).st_mode | stat.S_IXUSR)
        self.slots = os.path.join(self.tmp, "slots")
        self.data = os.path.join(self.tmp, "data")
        os.makedirs(self.data)
        self.envfile = os.path.join(self.tmp, "uboot.env")
        self.calls = os.path.join(self.tmp, "setenv.calls")
        self.reboots = os.path.join(self.tmp, "reboots")
        self.conf = os.path.join(self.tmp, "fw_env.config")
        open(self.conf, "w").close()
        self.booted = "/dev/mmcblk2p1"
        self.set_env(mmcdev="1", mmchwpart="0", mmcpart="1", mmcroot="/dev/mmcblk2p1")
        # slot a: ForgeFIRM, booted; slot b: the factory image
        self.slot("mmcblk2p1", {"etc/forgefirm-version": "v0.0.11\n", "boot/zImage": "k"})
        self.slot("mmcblk2p2", {"etc/version": "20200101000000\n", "etc/build": "FIRMWARE_VERSION = 2.6.0\n",
                                "boot/zImage": "k"})

    def slot(self, dev, files):
        for p, text in files.items():
            path = os.path.join(self.slots, dev, p)
            os.makedirs(os.path.dirname(path), exist_ok=True)
            with open(path, "w", newline="\n") as f:
                f.write(text)

    def set_env(self, **values):
        with open(self.envfile, "w", newline="\n") as f:
            f.write("".join("%s=%s\n" % kv for kv in values.items()))

    def env(self):
        with open(self.envfile) as f:
            return dict(ln.rstrip("\n").split("=", 1) for ln in f if "=" in ln)

    def mark(self, name):
        open(os.path.join(self.data, "slot-%s.incomplete" % name), "w").close()

    def ffboot(self, *args):
        open(self.calls, "w").close()
        script = ('%s\n%s\nFWCONFIG="%s"\nDATA_DIR="%s"\nBOOTED_ROOT=%s\n'
                  'main "$@"\necho "rc=$?"\n') % (functions(), STANDINS, self.conf, self.data, self.booted)
        env = dict(os.environ, PATH=self.bin + os.pathsep + os.environ["PATH"],
                   SLOTS=self.slots, ENV_FILE=self.envfile, SETENV_CALLS=self.calls,
                   SETENV_DEAD=os.path.join(self.tmp, "setenv.dead"), REBOOTS=self.reboots)
        r = subprocess.run(["sh", "-c", script, "ffboot"] + list(args), env=env, capture_output=True,
                           text=True, timeout=60)
        rc = r.stdout.strip().rsplit("rc=", 1)[-1] if "rc=" in r.stdout else None
        return rc, r.stdout + r.stderr

    def test_a_switch_writes_the_four_in_one_transaction_and_reads_them_back(self):
        rc, out = self.ffboot("-e2", "-n")
        self.assertEqual(rc, "0", out)
        self.assertEqual(self.env(), {"mmcdev": "1", "mmchwpart": "0", "mmcpart": "2", "mmcroot": "/dev/mmcblk2p2"})
        with open(self.calls) as f:
            self.assertEqual(len(f.read().splitlines()), 1)
        self.assertFalse(os.path.exists(self.reboots))

    def test_a_write_that_does_not_read_back_writes_nothing_one_variable_at_a_time(self):
        open(os.path.join(self.tmp, "setenv.dead"), "w").close()
        rc, out = self.ffboot("-e2", "-n")
        self.assertEqual(rc, "1", out)
        self.assertIn("mmcpart=1", out)                   # the read-back, in the refusal
        self.assertEqual(self.env()["mmcpart"], "1")
        with open(self.calls) as f:
            self.assertTrue(all(" -s " in ln for ln in f), "a variable was written on its own")

    def test_the_probe_gates_a_switch_and_force_overrides_it(self):
        os.remove(os.path.join(self.slots, "mmcblk2p2", "boot", "zImage"))
        rc, out = self.ffboot("-e2", "-n")
        self.assertEqual(rc, "1", out)
        self.assertIn("does not look bootable", out)
        self.assertEqual(self.env()["mmcpart"], "1")
        rc, out = self.ffboot("-e2", "-n", "-f")
        self.assertEqual(rc, "0", out)
        self.assertEqual(self.env()["mmcpart"], "2")

    def test_a_slot_marked_incomplete_is_never_selected(self):
        self.mark("b")
        for args in (("-e2", "-n"), ("-e2", "-n", "-f"), ("-e", "-n")):
            rc, out = self.ffboot(*args)
            self.assertEqual(rc, "1", (args, out))
            self.assertEqual(self.env()["mmcpart"], "1", args)

    def test_e_picks_the_newest_factory_image(self):
        # booted from the card: both eMMC slots hold a factory image
        self.booted = "/dev/mmcblk1p1"
        self.slot("mmcblk2p1", {"etc/version": "20210101000000\n", "boot/zImage": "k"})
        os.remove(os.path.join(self.slots, "mmcblk2p1", "etc", "forgefirm-version"))
        rc, out = self.ffboot("-e", "-n")
        self.assertEqual((rc, self.env()["mmcpart"]), ("0", "1"), out)
        self.mark("a")
        rc, out = self.ffboot("-e", "-n")
        self.assertEqual((rc, self.env()["mmcpart"]), ("0", "2"), out)

    def test_the_update_lock_refuses_a_switch(self):
        fd = os.open(os.path.join(self.data, "update.lock"), os.O_RDWR | os.O_CREAT, 0o644)
        self.addCleanup(os.close, fd)
        fcntl.flock(fd, fcntl.LOCK_EX)
        rc, out = self.ffboot("-e2", "-n")
        self.assertEqual(rc, "1", out)
        self.assertIn("update lock", out)
        self.assertEqual(self.env()["mmcpart"], "1")

    def test_the_inventory_prints_one_line_per_version(self):
        self.slot("mmcblk2p2", {"etc/forgefirm-version": "x\x01y\nslot.a.next=yes\n", "boot/zImage": "k"})
        self.mark("b")
        rc, out = self.ffboot("-l")
        self.assertEqual(rc, "0", out)
        lines = out.splitlines()
        self.assertIn("slot.b.version=xy", lines)
        self.assertIn("slot.b.incomplete=yes", lines)
        self.assertEqual([ln for ln in lines if ln.endswith(".next=yes")], ["slot.a.next=yes"])


if __name__ == "__main__":
    unittest.main()
