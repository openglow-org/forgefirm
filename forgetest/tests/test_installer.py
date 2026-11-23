# Copyright 2026 514 LLC d/b/a OpenGlow
# Written by Scott Wiederhold
# https://community.openglow.org
# SPDX-License-Identifier:    MIT

"""scripts/install-forgefirm.sh - the download that retries, the log, and
the installer's protections.

The installer runs once per machine, on factory firmware, over whatever
network the owner has. These tests run its own functions under sh with a
scripted curl in front of them: a transfer that fails resumes and is
tried again, the failures that waiting cannot fix end the tries at once,
the reason an install stopped is in the install log, and a log that cannot
be written never fails the install. Then each protection, against
stand-ins for fwup, fw_printenv and fw_setenv, dd and sv: the
version order and the downgrade prompt, an archive kept only whole, the
manifest's say over an archive, the archive's product, platform and tag,
the environment written in one transaction or not at all, the saved
environment read before anything is written, the incomplete-write mark,
the board's device tree, a factory update still writing, and the services
started again by a failure before the write. Last, the installer's main
body run end to end in a scratch root (a copy whose ROOT names it) against
stand-ins that log their calls: the order of its steps on the way to the
flip, and each refusal's leftovers."""
import os
import re
import shutil
import stat
import subprocess
import tempfile
import unittest

import helpers  # noqa: F401  (sys.path)

REPO = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
SCRIPT = os.path.join(REPO, "scripts", "install-forgefirm.sh")
MARK = "# The install log, before anything can fail."

# A curl that plays a script: each call takes the next "rc http bytes"
# line of $CURL_PLAN, appends that many bytes to the --output file the way
# a resumed transfer does, prints the HTTP code as -w '%{http_code}'
# would, records its arguments, and exits rc.
FAKE_CURL = r"""#!/bin/sh
echo "$*" >> "$CURL_CALLS"
OUT=""
while [ $# -gt 0 ]; do
  [ "$1" = "--output" ] && OUT="$2"
  shift
done
LINE=$(sed -n 1p "$CURL_PLAN")
sed -i 1d "$CURL_PLAN"
set -- $LINE
[ "${3:-0}" -gt 0 ] && dd if=/dev/zero bs=1 count="$3" 2>/dev/null | tr '\0' 'x' >> "$OUT"
printf '%s' "$2"
exit "$1"
"""

# The saved environment as a file of name=value lines ($ENV_FILE).
# fw_printenv -n reads one; fw_setenv -s applies a script in the classic
# "name value" form only, as the factory's u-boot-tools does, unless
# $SETENV_DEAD exists (then no script takes); a single-variable write
# always takes. Every fw_setenv call is recorded.
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
  grep -q '=' "$2" && exit 1
  while read -r k v; do put "$k" "$v"; done < "$2"
  exit 0
fi
put "$1" "$2"
"""
# fwup -m prints $FWUP_META.
FAKE_FWUP = "#!/bin/sh\n[ \"$1\" = -m ] && cat \"$FWUP_META\"\n"
# dd copies its if= file and exits $DD_RC.
FAKE_DD = r"""#!/bin/sh
for a in "$@"; do case $a in if=*) cat "${a#if=}" ;; esac; done
exit "${DD_RC:-0}"
"""
# sv records.
FAKE_SV = "#!/bin/sh\necho \"$*\" >> \"$SV_CALLS\"\n"


def functions():
    """The installer up to its first action: constants and functions."""
    with open(SCRIPT, encoding="utf-8") as f:
        text = f.read()
    head, mark, _ = text.partition(MARK)
    assert mark, "the installer lost the line the tests cut it at"
    return head


@unittest.skipUnless(shutil.which("sh") and os.name == "posix", "needs a POSIX sh")
class InstallerTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="ffinstall-test.")
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.bin = os.path.join(self.tmp, "bin")
        os.mkdir(self.bin)
        for name, body in (("curl", FAKE_CURL), ("nslookup", "#!/bin/sh\nexit 1\n"),
                           ("ip", "#!/bin/sh\nexit 0\n"), ("sleep", "#!/bin/sh\nexit 0\n"),
                           ("fw_printenv", FAKE_PRINTENV), ("fw_setenv", FAKE_SETENV), ("fwup", FAKE_FWUP),
                           ("sv", FAKE_SV), ("../ddbin/dd", FAKE_DD)):
            path = os.path.join(self.bin, name)
            os.makedirs(os.path.dirname(path), exist_ok=True)
            with open(path, "w", newline="\n") as f:
                f.write(body)
            os.chmod(path, os.stat(path).st_mode | stat.S_IXUSR)
        # dd's stand-in only where a case asks for it: the curl stand-in runs the real one
        self.ddbin = os.path.join(self.tmp, "ddbin")
        self.fw = os.path.join(self.tmp, "forgefirm.fw")
        self.log = os.path.join(self.tmp, "log", "install.log")
        self.calls = os.path.join(self.tmp, "curl.calls")
        self.plan = os.path.join(self.tmp, "curl.plan")
        self.envfile = os.path.join(self.tmp, "uboot.env")
        self.setenv_calls = os.path.join(self.tmp, "setenv.calls")
        self.sv_calls = os.path.join(self.tmp, "sv.calls")
        self.data = os.path.join(self.tmp, "data")
        self.otp = os.path.join(self.tmp, "otp")
        os.mkdir(self.otp)

    def run_sh(self, body, plan=(), stdin=None, shell="sh", **env_extra):
        with open(self.plan, "w", newline="\n") as f:
            f.write("".join("%s\n" % line for line in plan))
        open(self.calls, "w").close()
        script = ("%s\nFW_FILE='%s'\nLOG_DIR='%s'\nLOG_FILE='%s'\nmkdir -p \"$LOG_DIR\"\nLOG_OK=yes\n"
                  "DATA_DIR='%s'\nARCHIVE_DIR='%s/archive'\nOTP_DIR='%s'\nFWCONFIG=/dev/null\n%s\n") % (
            functions(), self.fw, os.path.dirname(self.log), self.log, self.data, self.data, self.otp, body)
        env = dict(os.environ, PATH=self.bin + os.pathsep + os.environ["PATH"],
                   CURL_PLAN=self.plan, CURL_CALLS=self.calls, ENV_FILE=self.envfile,
                   SETENV_CALLS=self.setenv_calls, SETENV_DEAD=os.path.join(self.tmp, "setenv.dead"),
                   FWUP_META=os.path.join(self.tmp, "meta"),
                   SV_CALLS=self.sv_calls)
        env.update(env_extra)
        return subprocess.run([shell, "-c", script], env=env, capture_output=True, text=True, timeout=60,
                              input=stdin)

    def write(self, path, text):
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", newline="\n") as f:
            f.write(text)

    def set_env(self, **values):
        self.write(self.envfile, "".join("%s=%s\n" % kv for kv in values.items()))

    def env(self):
        with open(self.envfile) as f:
            return dict(ln.rstrip("\n").split("=", 1) for ln in f if "=" in ln)

    def download(self, plan):
        r = self.run_sh('download_fw; echo "rc=$? why=$DL_WHY"', plan)
        with open(self.calls) as f:
            calls = f.read().splitlines()
        return r.stdout, calls, self.log_lines()

    def log_lines(self):
        try:
            with open(self.log) as f:
                return f.read().splitlines()
        except OSError:
            return []

    def test_a_clean_download_takes_one_try_and_its_name(self):
        out, calls, log = self.download(["0 200 5000"])
        self.assertIn("rc=0", out)
        self.assertEqual(len(calls), 1)
        self.assertEqual(os.path.getsize(self.fw), 5000)
        self.assertFalse(os.path.exists(self.fw + ".part"))
        self.assertTrue(any("complete on try 1, 5000 bytes" in line for line in log), log)

    def test_a_failed_transfer_resumes_and_is_tried_again(self):
        out, calls, log = self.download(["6 000 0", "56 200 3000", "0 206 2000"])
        self.assertIn("rc=0", out)
        self.assertEqual(len(calls), 3)
        for c in calls:                         # every try resumes, bounded in time
            self.assertIn("-C -", c)
            self.assertIn("--connect-timeout", c)
            self.assertIn("--speed-time", c)
        self.assertEqual(os.path.getsize(self.fw), 5000)     # 3000 kept, 2000 resumed
        warns = [line for line in log if " WARNING download: try " in line]
        self.assertEqual(len(warns), 2, log)
        self.assertIn("did not resolve", warns[0])
        self.assertIn("dropped mid-transfer (3000 bytes so far)", warns[1])
        self.assertIn("Trying again in 5s (try 2 of 5)", out)
        self.assertIn("Trying again in 15s (try 3 of 5)", out)

    def test_the_tries_run_out(self):
        out, calls, log = self.download(["28 000 0"] * 5)
        self.assertIn("rc=1", out)
        self.assertIn("timed out or stalled", out)
        self.assertEqual(len(calls), 5)
        self.assertFalse(os.path.exists(self.fw))
        self.assertFalse(os.path.exists(self.fw + ".part"))
        # each failed try leaves what the network looked like
        self.assertEqual(sum("github.com does not resolve" in line for line in log), 5, log)

    def test_a_missing_release_and_a_full_disk_end_the_tries_at_once(self):
        out, calls, _ = self.download(["22 404 0"])
        self.assertIn("rc=1", out)
        self.assertIn("HTTP 404", out)
        self.assertEqual(len(calls), 1)
        out, calls, _ = self.download(["23 200 100"])
        self.assertIn("rc=1", out)
        self.assertIn("could not be written", out)
        self.assertEqual(len(calls), 1)

    def test_a_partial_file_the_server_will_not_resume_starts_over(self):
        out, calls, _ = self.download(["56 200 4000", "22 416 0", "0 200 5000"])
        self.assertIn("rc=0", out)
        self.assertEqual(len(calls), 3)
        self.assertEqual(os.path.getsize(self.fw), 5000)     # not 9000: the stale part went

    def test_die_leaves_the_reason_in_the_log(self):
        r = self.run_sh('die "archiving slot 1 failed"; echo not-reached')
        self.assertEqual(r.returncode, 1)
        self.assertNotIn("not-reached", r.stdout)
        self.assertTrue(any("archiving slot 1 failed" in line for line in self.log_lines()))

    def test_no_writable_log_never_fails_the_install(self):
        r = self.run_sh('LOG_OK=""; log INFO "x"; LOG_OK=yes; LOG_FILE=/nonexistent/dir/x.log; '
                        'log INFO "y"; echo "still-running rc=$?"')
        self.assertIn("still-running rc=0", r.stdout)

    # ------------------------------------------------------------ protections

    @unittest.skipUnless(shutil.which("bash"), "the prompt's read -n1 needs bash or busybox sh")
    def test_an_older_release_installs_only_on_an_explicit_yes(self):
        r = self.run_sh('for p in "v0.0.9 v0.0.10" "0.1.0 v0.0.12" "v0.0.12 v0.0.12" "v0.0.12-dev-1 v0.0.11"; do '
                        'set -- $p; ver_lt "$1" "$2" && echo "lt $1 $2"; done')
        self.assertEqual(r.stdout.split("\n")[:-1], ["lt v0.0.9 v0.0.10"])
        # slot 1 holds ForgeFIRM v0.0.11
        probe = 'slot_probe () { S_TYPE=forgefirm; S_VER=; [ "$1" = 1 ] && S_VER=v0.0.11; }\n'
        for version, answer, rc in (("v0.0.10", "n", "1"), ("v0.0.10", "y", "0"), ("v0.0.12", "", "0")):
            r = self.run_sh(probe + 'M_VERSION=%s; downgrade_gate; echo "rc=$?"' % version, stdin=answer,
                            shell="bash")
            self.assertIn("rc=" + rc, r.stdout, (version, answer, r.stdout, r.stderr))
            self.assertEqual("OLDER than the installed" in r.stdout, version == "v0.0.10", r.stdout)

    def test_an_archive_stands_under_its_name_only_whole(self):
        src = os.path.join(self.tmp, "slot.img")
        self.write(src, "a factory slot\n")
        arc = os.path.join(self.tmp, "a.img.gz")
        path = os.pathsep.join((self.ddbin, self.bin, os.environ["PATH"]))
        r = self.run_sh('archive_dev %s %s; echo "rc=$?"' % (src, arc), DD_RC="1", PATH=path)
        self.assertIn("rc=1", r.stdout)
        self.assertFalse(os.path.exists(arc) or os.path.exists(arc + ".part"))
        r = self.run_sh('archive_dev %s %s; echo "rc=$?"' % (src, arc), PATH=path)
        self.assertIn("rc=0", r.stdout)
        self.assertEqual(subprocess.run(["gzip", "-dc", arc], capture_output=True).stdout, b"a factory slot\n")
        self.assertFalse(os.path.exists(arc + ".part"))

    def test_an_archive_counts_only_with_its_manifest_line_and_a_whole_stream(self):
        arc = os.path.join(self.data, "archive", "factory-rootfs-1.img.gz")
        os.makedirs(os.path.dirname(arc))
        subprocess.run("echo x | gzip -c > '%s'" % arc, shell=True, check=True)
        check = 'archived_ok %s && echo yes || echo no' % arc
        self.assertEqual(self.run_sh(check).stdout.strip(), "no")
        self.write(os.path.join(self.data, "archive", "manifest"), "d slot1 factory 1 ver=2 factory-rootfs-1.img.gz md5=0\n")
        self.assertEqual(self.run_sh(check).stdout.strip(), "yes")
        with open(arc, "ab") as f:
            f.write(b"not gzip")
        self.assertEqual(self.run_sh(check).stdout.strip(), "no")

    def test_only_forgefirm_firmware_for_this_platform_and_tag_passes(self):
        meta = os.path.join(self.tmp, "meta")
        check = 'archive_identity x.fw && echo pass || echo "refused: $GATE_WHY"'
        for product, platform, version, pre, verdict in (
                ("ForgeFIRM extension", "glowforge", "v0.0.12", "", "refused: archive product"),
                ("ForgeFIRM firmware", "other", "v0.0.12", "", "refused: archive platform"),
                ("ForgeFIRM firmware", "glowforge", "v0.0.11", "RELEASE_TAG=v0.0.12; LOCAL_FW=",
                 "refused: the downloaded archive is v0.0.11, not v0.0.12"),
                ("ForgeFIRM firmware", "glowforge", "v0.0.11", "RELEASE_TAG=v0.0.12; LOCAL_FW=my.fw", "pass"),
                ("ForgeFIRM firmware", "glowforge", "v0.0.12", "RELEASE_TAG=v0.0.12; LOCAL_FW=", "pass")):
            self.write(meta, 'meta-product="%s"\nmeta-platform="%s"\nmeta-version="%s"\n' % (product, platform, version))
            r = self.run_sh("%s\n%s" % (pre, check))
            self.assertTrue(r.stdout.startswith(verdict), (product, platform, version, pre, r.stdout))

    def test_the_environment_takes_one_transaction_or_nothing(self):
        self.set_env(mmcdev="1", mmchwpart="0", mmcpart="1", mmcroot="/dev/mmcblk2p1")
        r = self.run_sh('set_env 1 0 2 /dev/mmcblk2p2; echo "rc=$?"')
        self.assertIn("rc=0", r.stdout)
        self.assertEqual(self.env(), {"mmcdev": "1", "mmchwpart": "0", "mmcpart": "2", "mmcroot": "/dev/mmcblk2p2"})
        # no script form takes: nothing is written one variable at a time
        open(os.path.join(self.tmp, "setenv.dead"), "w").close()
        open(self.setenv_calls, "w").close()
        r = self.run_sh('set_env 1 0 1 /dev/mmcblk2p1; echo "rc=$?"')
        self.assertIn("rc=1", r.stdout)
        self.assertEqual(self.env()["mmcpart"], "2")
        with open(self.setenv_calls) as f:
            self.assertTrue(all(" -s " in ln for ln in f), "a variable was written on its own")

    def test_the_saved_environment_is_read_before_anything_is_written(self):
        boot = 'BOOTED=/dev/mmcblk2p1; TARGET=2\nenv_slot && echo "next=$NEXT_SLOT" || echo "refused: $ENV_WHY"'
        for env, verdict in (
                (dict(mmcdev="1", mmcpart="1", mmcroot="/dev/mmcblk2p1"), "next=1"),
                (dict(mmcdev="1", mmcpart="2", mmcroot="/dev/mmcblk2p2"), "next=2"),
                (dict(mmcdev="1", mmcpart="2"), "next=2"),
                (dict(mmcdev="1", mmcpart="1", mmcroot="/dev/mmcblk2p2"), "refused: the saved environment is mixed"),
                (dict(mmcdev="0", mmcpart="1", mmcroot="/dev/mmcblk1p1"), "refused: the saved environment's mmcroot"),
                (dict(), "refused: the saved environment names no eMMC slot")):
            self.set_env(**env)
            r = self.run_sh(boot)
            self.assertTrue(r.stdout.startswith(verdict), (env, r.stdout))

    def test_the_slot_is_marked_incomplete_until_its_write_verified(self):
        mark = os.path.join(self.data, "slot-b.incomplete")
        self.run_sh("marker_set 2")
        self.assertTrue(os.path.isfile(mark))
        self.run_sh("marker_clear 2")
        self.assertFalse(os.path.exists(mark))

    def test_the_image_must_carry_this_boards_device_tree(self):
        root = os.path.join(self.tmp, "rootfs")
        check = 'board_rev && board_dtb_ok %s && echo pass || echo "refused: $DTB_WHY"' % root
        gp1, mac0 = os.path.join(self.otp, "HW_OCOTP_GP1"), os.path.join(self.otp, "HW_OCOTP_MAC0")
        self.assertTrue(self.run_sh(check).stdout.startswith("refused: the board revision cannot be read"))
        self.write(gp1, "0x1z\n")                       # not a fuse word's text
        self.assertTrue(self.run_sh(check).stdout.startswith("refused: the board revision cannot be read"))
        self.write(gp1, "0x10 \n")                      # nor with a space after it
        self.assertTrue(self.run_sh(check).stdout.startswith("refused: the board revision cannot be read"))
        self.write(os.path.join(root, "boot", "imx6dl-glowforge-v2.dtb"), "")
        self.write(gp1, "0x00000141\n")                 # revision 20.1
        self.assertTrue(self.run_sh(check).stdout.startswith("refused: the image has no device tree"))
        self.write(os.path.join(root, "boot", "imx6dl-glowforge-v20.dtb"), "")
        self.assertEqual(self.run_sh(check).stdout.strip(), "pass")
        self.write(gp1, "0x00000000\n")                 # no revision fused: 12.0 with a serial
        self.write(mac0, "0x0000abcd\n")
        self.assertTrue(self.run_sh(check).stdout.startswith("refused: the image has no device tree"))
        self.write(os.path.join(root, "boot", "imx6dl-glowforge-v120.dtb"), "")
        self.assertEqual(self.run_sh(check).stdout.strip(), "pass")

    def test_a_factory_update_still_writing_is_seen(self):
        target = os.path.join(self.tmp, "slot2")
        self.write(target, "")
        root = os.path.join(self.tmp, "root")
        self.write(os.path.join(root, "proc", "41", "comm"), "sh\n")
        check = 'ROOT=%s; factory_busy %s && echo "busy: $BUSY_WHY" || echo free' % (root, target)
        self.assertEqual(self.run_sh(check).stdout.strip(), "free")
        self.write(os.path.join(root, "proc", "42", "comm"), "fwup\n")
        self.assertTrue(self.run_sh(check).stdout.startswith("busy: an fwup process is running"))

    def test_a_failure_before_the_write_starts_the_services_again(self):
        self.run_sh("stop_gf_services; die stopped")
        with open(self.sv_calls) as f:
            self.assertIn("start /sv/glowforge", f.read())
        open(self.sv_calls, "w").close()
        self.run_sh("stop_gf_services; WRITE_STARTED=yes; die stopped")
        with open(self.sv_calls) as f:
            self.assertNotIn("start", f.read())

    def test_the_release_key_is_printed_as_the_notes_print_it(self):
        key = re.search(r"^PUBKEY='(.*)'$", functions(), re.M).group(1).replace("\\x", "")
        r = self.run_sh("key_fingerprint")
        self.assertEqual(r.stdout.strip(), " ".join(key[i:i + 8] for i in range(0, 64, 8)))


# The installer's main body, run end to end: stand-ins for the machine's
# tools, each appending its call to $CALLS as one line. The slots and the
# boot partitions are files of "path=content" lines under the scratch root;
# mount makes those lines into files at the mount point, fwup -a writes an
# archive's "file path=content" lines into a slot, and fwup -m prints the
# archive's meta- lines.
FLOW_TOOLS = {
    # sv: with $FLIP_ON_STOP standing, the updater's stop finds the
    # environment flipped to slot 2 (an update that switched it alone);
    # with $CHANGE_ON_STOP, slot 2 holding another factory version.
    "sv": '#!/bin/sh\necho "sv $1" >> "$CALLS"\n'
          'if [ "$1 $2" = "stop /sv/glowforge-updater" ] && [ -e "$FLIP_ON_STOP" ]; then\n'
          '  sed -i "s|^mmcpart=.*|mmcpart=2|; s|^mmcroot=.*|mmcroot=/dev/mmcblk2p2|" "$ROOT/env"; fi\n'
          'if [ "$1 $2" = "stop /sv/glowforge-updater" ] && [ -e "$CHANGE_ON_STOP" ]; then\n'
          '  echo "etc/version=20200101000000" > "$ROOT/dev/mmcblk2p2"; fi\n',
    "fuser": '#!/bin/sh\necho "fuser" >> "$CALLS"\nexit 1\n',
    # sync: as the mount, " drop=3" once the page cache was dropped, so the
    # sync before the drop reads as "sync" alone
    "sync": '#!/bin/sh\necho "sync$(sed "s/^/ drop=/" "$ROOT/proc/sys/vm/drop_caches" 2>/dev/null)" >> "$CALLS"\n',
    "sleep": '#!/bin/sh\nexit 0\n',
    "reboot": '#!/bin/sh\necho reboot >> "$CALLS"\n',
    "df": '#!/bin/sh\necho "Filesystem 1K-blocks Used Available Use% Mounted"\necho "data 9999999 1 9999998 1% /data"\n',
    "dd": '#!/bin/sh\nfor a in "$@"; do case $a in if=*) f=${a#if=} ;; esac; done\n'
          'echo "dd ${f##*/}" >> "$CALLS"\ncat "$f"\n',
    "umount": '#!/bin/sh\nfor a in "$@"; do mp=$a; done\n[ -d "$mp" ] && find "$mp" -mindepth 1 -delete\nexit 0\n',
    # mount: the call names " drop=3" once the page cache was dropped
    # ($ROOT/proc/sys/vm/drop_caches written).
    "mount": '#!/bin/sh\neval "dev=\\${$(($# - 1))}"\neval "mp=\\${$#}"\n'
             'echo "mount ${dev##*/}$(sed "s/^/ drop=/" "$ROOT/proc/sys/vm/drop_caches")" >> "$CALLS"\n'
             '[ -s "$dev" ] || exit 32\n'
             'while IFS= read -r line; do p=${line%%=*}; mkdir -p "$mp/$(dirname "$p")"; '
             'printf \'%s\\n\' "${line#*=}" > "$mp/$p"; done < "$dev"\n',
    "fwup": '#!/bin/sh\nop=$1; shift; f=; d=\nwhile [ $# -gt 0 ]; do case $1 in -i) f=$2; shift ;; -d) d=$2; shift ;; esac; shift; done\n'
            'case $op in\n'
            '-V) echo "fwup -V" >> "$CALLS" ;;\n'
            '-m) echo "fwup -m" >> "$CALLS"; grep "^meta-" "$f" ;;\n'
            '-a) m=no; [ -e "$ROOT/data/forgefirm/slot-b.incomplete" ] && m=yes\n'
            '    echo "fwup -a mark=$m" >> "$CALLS"\n'
            '    [ ! -e "$FWUP_FAIL" ] || exit 1\n'
            '    sed -n "s/^file //p" "$f" > "$d" ;;\n'
            'esac\n',
    "fw_printenv": '#!/bin/sh\nwhile [ $# -gt 0 ]; do case $1 in -n) N=$2; shift ;; esac; shift; done\n'
                   'grep -q "^$N=" "$ROOT/env" || exit 1\nsed -n "s/^$N=//p" "$ROOT/env"\n',
    "fw_setenv": '#!/bin/sh\nshift 2\n[ "$1" = -s ] || exit 1\ngrep -q = "$2" && exit 1\n'
                 'while read -r k v; do grep -v "^$k=" "$ROOT/env" > "$ROOT/env.new"; echo "$k=$v" >> "$ROOT/env.new"; '
                 'mv "$ROOT/env.new" "$ROOT/env"; done < "$2"\n'
                 'echo "fw_setenv $(sed -n "s/^mmcpart=//p" "$ROOT/env")" >> "$CALLS"\n',
}


@unittest.skipUnless(shutil.which("bash") and os.name == "posix", "the installer's prompts need bash or busybox sh")
class InstallerFlowTests(unittest.TestCase):
    """The installer's main body, run as the owner runs it, booted from slot
    1 with the factory firmware in both slots and the next boot set to slot
    2, the target: the release key printed, the environment read and the
    next boot pointed back before the services' stop, a factory update
    looked for and the target probed again after it, the archives written
    and synced, the archive's signature, identity and version checked, the
    mark before fwup -a, the written slot verified with the page cache
    dropped and this board's device tree, /data/ffboot put in place, the
    mark cleared, the flip, the .fw removed, the restart.
    Then each refusal leaves the mark, the environment and the services as
    it says."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="ffinstall-flow.")
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.root = os.path.join(self.tmp, "root")
        self.bin = os.path.join(self.tmp, "bin")
        os.makedirs(self.bin)
        real_mv = shutil.which("mv")
        tools = dict(FLOW_TOOLS)
        tools["mv"] = ('#!/bin/sh\nfor a in "$@"; do t=$a; done\n'
                       'case $t in */data/ffboot) [ ! -e "$MV_FAIL" ] || exit 1 ;; esac\nexec %s "$@"\n' % real_mv)
        tools["chmod"] = ('#!/bin/sh\nfor a in "$@"; do t=$a; done\n'
                          'case $t in */data/ffboot) [ ! -e "$CHMOD_FAIL" ] || exit 1 ;; esac\nexec %s "$@"\n'
                          % shutil.which("chmod"))
        for name, body in tools.items():
            path = os.path.join(self.bin, name)
            with open(path, "w", newline="\n") as f:
                f.write(body)
            os.chmod(path, 0o755)
        w = self.write
        w("etc/version", "20190101000000\n")
        w("etc/fw_env.config", "")
        w("glowforge/.keep", "")
        w("proc/cmdline", "console=ttymxc0 root=/dev/mmcblk2p1 rw\n")
        w("proc/sys/vm/drop_caches", "")
        for n in (1, 2):
            w("sys/class/block/mmcblk2p%d/size" % n, "409600\n")
        w("sys/fsl_otp/HW_OCOTP_GP1", "0x00000141\n")          # revision 20.1
        w("dev/mmcblk2p1", "etc/version=20190101000000\n")
        w("dev/mmcblk2p2", "etc/version=20180101000000\netc/build=FIRMWARE_VERSION = 2.0.0\nboot/zImage=k\n")
        w("dev/mmcblk2boot0", "boot0\n")
        w("dev/mmcblk2boot1", "boot1\n")
        w("data/.keep", "")
        w("env", "mmcdev=1\nmmchwpart=0\nmmcpart=2\nmmcroot=/dev/mmcblk2p2\n")   # the next boot is the target
        self.fw = os.path.join(self.tmp, "forgefirm.fw")
        self.archive()
        with open(SCRIPT, encoding="utf-8") as f:
            text = f.read()
        self.assertEqual(text.count("\nROOT=''\n"), 1)
        self.script = os.path.join(self.tmp, "install-forgefirm.sh")
        with open(self.script, "w", newline="\n") as f:
            f.write(text.replace("\nROOT=''\n", "\nROOT='%s'\n" % self.root, 1))
        self.calls = os.path.join(self.tmp, "calls")
        self.mark = os.path.join(self.root, "data", "forgefirm", "slot-b.incomplete")

    def write(self, rel, text):
        path = os.path.join(self.root, rel)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", newline="\n") as f:
            f.write(text)

    def archive(self, product="ForgeFIRM firmware", version="v0.0.12", dtb="v20"):
        lines = ['meta-product="%s"' % product, 'meta-platform="glowforge"', 'meta-version="%s"' % version,
                 "file etc/forgefirm-version=%s" % version, "file boot/zImage=kernel",
                 "file usr/sbin/ffboot=#!/bin/sh"]
        if dtb:
            lines.append("file boot/imx6dl-glowforge-%s.dtb=dtb" % dtb)
        with open(self.fw, "w", newline="\n") as f:
            f.write("\n".join(lines) + "\n")

    def install(self, answers="y\nx", **env):
        open(self.calls, "w").close()
        e = dict(os.environ, PATH=self.bin + os.pathsep + os.environ["PATH"], ROOT=self.root, CALLS=self.calls,
                 FWUP_FAIL=os.path.join(self.tmp, "fwup.fail"), MV_FAIL=os.path.join(self.tmp, "mv.fail"),
                 CHMOD_FAIL=os.path.join(self.tmp, "chmod.fail"), FLIP_ON_STOP=os.path.join(self.tmp, "flip.on.stop"),
                 CHANGE_ON_STOP=os.path.join(self.tmp, "change.on.stop"))
        e.update(env)
        r = subprocess.run(["bash", self.script, self.fw], env=e, input=answers, capture_output=True, text=True,
                           timeout=120)
        with open(self.calls) as f:
            return r, f.read().splitlines()

    def env(self):
        with open(os.path.join(self.root, "env")) as f:
            return dict(ln.rstrip("\n").split("=", 1) for ln in f if "=" in ln)

    def assertInOrder(self, calls, steps):
        at = 0
        for step in steps:
            found = [i for i in range(at, len(calls)) if calls[i] == step]
            self.assertTrue(found, "%r not found after %r in %r" % (step, calls[at - 1] if at else None, calls))
            at = found[0] + 1

    def test_the_main_body_runs_its_steps_in_order_to_the_flip(self):
        r, calls = self.install()
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertInOrder(calls, ["fw_setenv 1", "sv stop", "fuser", "mount mmcblk2p2", "dd mmcblk2p1",
                                   "dd mmcblk2p2", "dd mmcblk2boot0", "dd mmcblk2boot1", "sync", "fwup -V", "fwup -m",
                                   "fwup -a mark=yes", "sync", "mount mmcblk2p2 drop=3", "fw_setenv 2", "reboot"])
        self.assertNotIn("sv start", " ".join(calls))
        # the release key, as the release notes print it, before anything runs
        key = re.search(r"^PUBKEY='(.*)'$", functions(), re.M).group(1).replace("\\x", "")
        shown = re.sub(r"\x1b\[[0-9;]*m", "", r.stdout)
        self.assertIn("Release key: " + " ".join(key[i:i + 8] for i in range(0, 64, 8)), shown)
        self.assertEqual(self.env()["mmcroot"], "/dev/mmcblk2p2")
        self.assertFalse(os.path.exists(self.mark), "the mark stands after the flip")
        self.assertTrue(os.access(os.path.join(self.root, "data", "ffboot"), os.X_OK))
        self.assertFalse(os.path.exists(os.path.join(self.root, "data", "forgefirm", "forgefirm.fw")))
        with open(os.path.join(self.root, "data", "forgefirm", "archive", "manifest")) as f:
            self.assertEqual(len(f.read().splitlines()), 4)

    def assertRefused(self, r, calls, words, mark):
        self.assertEqual(r.returncode, 1, r.stdout + r.stderr)
        self.assertIn(words, r.stdout)
        self.assertEqual(self.env()["mmcroot"], "/dev/mmcblk2p1", "the boot moved")   # pointed back, never flipped
        self.assertNotIn("reboot", calls)
        self.assertEqual(os.path.exists(self.mark), mark)

    def test_an_image_without_this_boards_device_tree_leaves_the_mark_and_the_boot(self):
        self.archive(dtb="v12")
        r, calls = self.install()
        self.assertRefused(r, calls, "no device tree for this board", mark=True)
        self.assertNotIn("sv start", " ".join(calls))      # the write began: the services stay stopped

    def test_a_write_that_fails_leaves_the_mark_and_the_services_stopped(self):
        open(os.path.join(self.tmp, "fwup.fail"), "w").close()
        r, calls = self.install()
        self.assertRefused(r, calls, "fwup apply failed", mark=True)
        self.assertNotIn("sv start", " ".join(calls))

    def test_ffboot_not_put_in_place_stops_before_the_flip(self):
        open(os.path.join(self.tmp, "mv.fail"), "w").close()
        r, calls = self.install()
        self.assertRefused(r, calls, "cannot put /data/ffboot in place", mark=True)

    def test_ffboot_not_made_executable_stops_before_the_flip(self):
        open(os.path.join(self.tmp, "chmod.fail"), "w").close()
        r, calls = self.install()
        self.assertRefused(r, calls, "cannot make /data/ffboot executable", mark=True)

    def test_a_slot_that_changed_after_it_was_shown_stops_before_anything_is_written(self):
        open(os.path.join(self.tmp, "change.on.stop"), "w").close()
        r, calls = self.install()
        self.assertRefused(r, calls, "changed after it was shown", mark=False)
        self.assertFalse([c for c in calls if c.startswith(("dd ", "fwup"))], "something was written")

    def test_a_manifest_line_not_written_stops_before_the_write(self):
        os.makedirs(os.path.join(self.root, "data", "forgefirm", "archive", "manifest"))
        r, calls = self.install()
        # the first archive written, slot 1's: its own line stops the install
        self.assertRefused(r, calls, "the archive manifest could not be written for slot 1", mark=False)
        self.assertNotIn("fwup -a mark=yes", calls)
        self.assertNotIn("fwup -a mark=no", calls)
        self.assertIn("sv start", " ".join(calls))         # before the write: the services started again

    def test_an_archive_of_another_product_stops_before_the_write(self):
        self.archive(product="ForgeFIRM extension")
        r, calls = self.install()
        self.assertRefused(r, calls, "not ForgeFIRM firmware", mark=False)
        self.assertFalse([c for c in calls if c.startswith("fwup -a")])

    def test_an_older_release_declined_stops_before_the_write(self):
        self.write("dev/mmcblk2p2", "etc/forgefirm-version=v0.0.13\nboot/zImage=k\n")
        self.archive(version="v0.0.12")
        r, calls = self.install(answers="ERASE\ny\nn")
        self.assertRefused(r, calls, "downgrade declined", mark=False)
        self.assertFalse([c for c in calls if c.startswith("fwup -a")])

    def test_a_boot_flipped_to_the_target_during_the_stop_is_pointed_back_before_the_write(self):
        open(os.path.join(self.tmp, "flip.on.stop"), "w").close()
        r, calls = self.install()
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertInOrder(calls, ["fw_setenv 1", "sv stop", "fw_setenv 1", "dd mmcblk2p1", "fwup -a mark=yes",
                                   "fw_setenv 2"])

    def test_an_unreadable_revision_fuse_stops_before_anything_is_written(self):
        os.remove(os.path.join(self.root, "sys", "fsl_otp", "HW_OCOTP_GP1"))
        r, calls = self.install()
        self.assertEqual(r.returncode, 1, r.stdout + r.stderr)
        self.assertIn("board revision cannot be read", r.stdout)
        self.assertEqual(calls, [], "something ran before the refusal")
        self.assertEqual(self.env()["mmcroot"], "/dev/mmcblk2p2")


if __name__ == "__main__":
    unittest.main()
