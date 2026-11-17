# Copyright 2026 514 LLC d/b/a OpenGlow
# Written by Scott Wiederhold
# https://community.openglow.org
# SPDX-License-Identifier:    MIT

"""motion.deadman's stall drills hang the controller with SIGSTOP. The
hang ends on every way out of the drill, a failed check or an Abort among
them, and only for the controller: a pid that has since become another
process is never sent SIGCONT. Runs real child processes; Linux only."""
import os
import shutil
import signal
import subprocess
import sys
import tempfile
import time
import unittest

from forgetest.runner import Aborted
from forgetest.suite import motion


class Ctx:
    def __init__(self):
        self.lines = []

    def log(self, msg, *args):
        self.lines.append(msg % args if args else msg)


def stopped(pid):
    """True while the process is stopped (state T in /proc/<pid>/stat)."""
    with open("/proc/%d/stat" % pid) as f:
        return f.read().rsplit(")", 1)[1].split()[0] in ("T", "t")


@unittest.skipUnless(sys.platform.startswith("linux"), "reads /proc")
class StoppedControllerTests(unittest.TestCase):
    def child(self, name):
        """A sleeping child whose comm is `name` (a copy of sleep under it)."""
        tmp = tempfile.mkdtemp(prefix="forgetest-stop-")
        self.addCleanup(shutil.rmtree, tmp, ignore_errors=True)
        exe = os.path.join(tmp, name)
        shutil.copy(shutil.which("sleep"), exe)
        p = subprocess.Popen([exe, "30"])

        def end():
            p.kill()
            p.wait()
        self.addCleanup(end)
        return p.pid

    def wait_state(self, pid, want):
        deadline = time.time() + 5
        while stopped(pid) != want and time.time() < deadline:
            time.sleep(0.01)
        return stopped(pid)

    def test_the_hang_ends_with_the_drill_and_only_for_the_controller(self):
        pid = self.child("grblHAL_glowforge")               # its comm: the 15 characters the kernel keeps
        with self.assertRaises(Aborted):
            with motion._stopped(Ctx(), pid):
                self.assertTrue(self.wait_state(pid, True))
                raise Aborted("aborted")
        self.assertFalse(self.wait_state(pid, False))
        other = self.child("sleep")                         # the pid is no longer the controller's
        with motion._stopped(Ctx(), other):
            self.assertTrue(self.wait_state(other, True))
        time.sleep(0.05)
        self.assertTrue(stopped(other))
        os.kill(other, signal.SIGCONT)


if __name__ == "__main__":
    unittest.main()
