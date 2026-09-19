# Copyright 2026 514 LLC d/b/a OpenGlow
# Written by Scott Wiederhold
# https://community.openglow.org
# SPDX-License-Identifier:    MIT

"""scripts/install-forgefirm.sh - the download that retries, and the log.

The installer runs once per machine, on factory firmware, over whatever
network the owner has. These tests run its own functions under sh with a
scripted curl in front of them: a transfer that fails resumes and is
tried again, the failures that waiting cannot fix end the tries at once,
and every step leaves a line in the install log in the log tree's own
format."""
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

# The log tree's line format (forgetest/suite/logs.py keeps the same one).
LINE_RE = re.compile(r"^\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d(\.\d+)?[+-]\d\d:\d\d install\[\d+\] "
                     r"(EMERG|ALERT|CRIT|ERR|WARNING|NOTICE|INFO|DEBUG) .+$")

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
                           ("ip", "#!/bin/sh\nexit 0\n"), ("sleep", "#!/bin/sh\nexit 0\n")):
            path = os.path.join(self.bin, name)
            with open(path, "w", newline="\n") as f:
                f.write(body)
            os.chmod(path, os.stat(path).st_mode | stat.S_IXUSR)
        self.fw = os.path.join(self.tmp, "forgefirm.fw")
        self.log = os.path.join(self.tmp, "log", "install.log")
        self.calls = os.path.join(self.tmp, "curl.calls")
        self.plan = os.path.join(self.tmp, "curl.plan")

    def run_sh(self, body, plan=()):
        with open(self.plan, "w", newline="\n") as f:
            f.write("".join("%s\n" % line for line in plan))
        open(self.calls, "w").close()
        script = "%s\nFW_FILE='%s'\nLOG_DIR='%s'\nLOG_FILE='%s'\nmkdir -p \"$LOG_DIR\"\nLOG_OK=yes\n%s\n" % (
            functions(), self.fw, os.path.dirname(self.log), self.log, body)
        env = dict(os.environ, PATH=self.bin + os.pathsep + os.environ["PATH"],
                   CURL_PLAN=self.plan, CURL_CALLS=self.calls)
        return subprocess.run(["sh", "-c", script], env=env, capture_output=True, text=True, timeout=60)

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

    def test_a_tls_failure_names_the_clock(self):
        out, _, _ = self.download(["60 000 0"] * 5)
        self.assertRegex(out, r"TLS handshake failed \(a wrong clock does this: \d{4}-\d\d-\d\d")

    def test_every_log_line_is_in_the_tree_format(self):
        self.download(["7 000 0", "0 200 10"])
        self.run_sh('log NOTICE "operator declined to continue; no changes made"')
        lines = self.log_lines()
        self.assertGreater(len(lines), 5)
        for line in lines:
            self.assertRegex(line, LINE_RE)

    def test_die_leaves_the_reason_in_the_log(self):
        r = self.run_sh('die "archiving slot 1 failed"; echo not-reached')
        self.assertEqual(r.returncode, 1)
        self.assertNotIn("not-reached", r.stdout)
        self.assertTrue(any(" ERR install failed: archiving slot 1 failed" in line
                            for line in self.log_lines()))

    def test_no_writable_log_never_fails_the_install(self):
        r = self.run_sh('LOG_OK=""; log INFO "x"; LOG_OK=yes; LOG_FILE=/nonexistent/dir/x.log; '
                        'log INFO "y"; echo "still-running rc=$?"')
        self.assertIn("still-running rc=0", r.stdout)


class InstallerShapeTests(unittest.TestCase):
    """What holds on any host, with no shell to run."""

    def test_the_log_lives_in_the_tree_the_export_carries(self):
        head = functions()
        self.assertIn('LOG_DIR="/data/log/forgefirm/install"', head)
        self.assertIn('LOG_FILE="$LOG_DIR/install.log"', head)

    def test_the_download_is_the_retrying_one(self):
        with open(SCRIPT, encoding="utf-8") as f:
            text = f.read()
        body = text.partition(MARK)[2]
        self.assertIn("download_fw", body)
        self.assertNotRegex(body, r"(?m)^\s*curl ")       # no bare curl past the functions


if __name__ == "__main__":
    unittest.main()
