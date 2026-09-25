# Copyright 2026 514 LLC d/b/a OpenGlow
# Written by Scott Wiederhold
# https://community.openglow.org
# SPDX-License-Identifier:    MIT

"""A takeover hands back the cloud client it found. In cloud mode
forgectrl's start at the end of a takeover starts a cloud client, and what
that client is comes only from gfcloud's one-start markers. Started bare it
is the online client with the service's connect-time hunt, and the moves
the service sends after the hunt outlast the run. The start is made under
the no-hunt marker, and under the offline marker when the takeover found
the offline service, which it then proves came back; a miss is a leftover
that fails the run. Runs against the fake forgectrl, a fake /proc, and a
fake init script that plays the client's start."""
import os
import shutil
import tempfile
import unittest

from forgetest import baseline, hw, runner

from helpers import FakeForgectrl


class FakeRun:
    def __init__(self, cap):
        self.baseline_captured = cap
        self.lines = []

    def log(self, s):
        self.lines.append(s)


class TakeoverClientTests(unittest.TestCase):
    OLD_PID, NEW_PID = 200, 201

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="forgetest-tkc-")
        self.proc = os.path.join(self.tmp, "proc")
        os.makedirs(os.path.join(self.proc, "net"))
        self.sysfs = os.path.join(self.tmp, "sysfs") + os.sep
        for group in ("cnc", "pic", "head"):
            os.makedirs(self.sysfs + group)
        os.environ["GF_SYSFS_ROOT"] = self.sysfs
        os.environ["FORGETEST_PROC_ROOT"] = self.proc
        os.environ["FORGETEST_MARKER"] = os.path.join(self.tmp, "forgetest.active")
        self.saved = (baseline.NOHUNT_MARKER, baseline.OFFLINE_MARKER, baseline.OFFLINE_SOCKET,
                      hw.initd, runner.Takeover.OFFLINE_BACK_S)
        baseline.NOHUNT_MARKER = os.path.join(self.tmp, "gfcloud-nohunt")
        baseline.OFFLINE_MARKER = os.path.join(self.tmp, "gfcloud-offline")
        baseline.OFFLINE_SOCKET = "/run/gfcloud-offline.sock"
        runner.Takeover.OFFLINE_BACK_S = 0
        baseline.Baseline._unreachable_until = 0.0
        self.unix = []                          # (pid, inode, path, listening)
        self.fc = FakeForgectrl().start()
        self.fc.state["mode"] = {"mode": "cloud", "controller": "running", "pid": self.OLD_PID,
                                 "motion": "verified"}
        self.calls = []
        self.at_start = None                    # the markers present when forgectrl started
        self.comes_up = "running"               # the controller forgectrl's start settles on
        hw.initd = self._initd

    def tearDown(self):
        (baseline.NOHUNT_MARKER, baseline.OFFLINE_MARKER, baseline.OFFLINE_SOCKET,
         hw.initd, runner.Takeover.OFFLINE_BACK_S) = self.saved
        self.fc.stop()
        for k in ("GF_SYSFS_ROOT", "FORGETEST_PROC_ROOT", "FORGETEST_MARKER"):
            os.environ.pop(k, None)
        shutil.rmtree(self.tmp, ignore_errors=True)

    # -- the fakes ------------------------------------------------------
    def _socket(self, pid, inode, path, listening=True):
        self.unix.append((pid, inode, path, listening))
        fd_dir = os.path.join(self.proc, str(pid), "fd")
        os.makedirs(fd_dir, exist_ok=True)
        os.symlink("socket:[%d]" % inode, os.path.join(fd_dir, str(len(os.listdir(fd_dir)) + 3)))
        with open(os.path.join(self.proc, "net", "unix"), "w") as f:
            f.write("Num       RefCount Protocol Flags    Type St Inode Path\n")
            for _pid, ino, p, lis in self.unix:
                f.write("00000000: 00000002 00000000 %08X 0001 %s %d %s\n"
                        % (0x10000 if lis else 0, "01" if lis else "03", ino, p))

    def _offline_client(self, pid):
        self._socket(pid, 9000 + pid, baseline.OFFLINE_SOCKET)

    def _initd(self, service, action, timeout=60):
        """forgectrl stop/start; the start plays gfcloud's: it reads and
        takes down the markers, and it is the offline service when the
        offline marker was there."""
        self.calls.append((service, action))
        if action != "start":
            return 0, ""
        self.at_start = sorted(os.path.basename(p) for p in (baseline.NOHUNT_MARKER, baseline.OFFLINE_MARKER)
                               if os.path.exists(p))
        mode = self.fc.state["mode"]
        if self.comes_up != "running":
            self.fc.state["mode"] = dict(mode, controller=self.comes_up, pid=None, motion="unverified")
            return 0, ""
        offline = os.path.exists(baseline.OFFLINE_MARKER)
        for p in (baseline.NOHUNT_MARKER, baseline.OFFLINE_MARKER):
            if os.path.exists(p):
                os.remove(p)
        self.fc.state["mode"] = dict(mode, controller="running", pid=self.NEW_PID, motion="verified")
        if offline and mode.get("mode") == "cloud":
            self._offline_client(self.NEW_PID)
        return 0, ""

    def takeover(self):
        cap = {"position": None, "mode": self.fc.state["mode"]["mode"]}
        run = FakeRun(cap)
        with runner.Takeover(run.log, "test.id", run=run):
            pass
        return cap, run

    def markers_left(self):
        return [p for p in (baseline.NOHUNT_MARKER, baseline.OFFLINE_MARKER) if os.path.exists(p)]

    # -- the socket judge -------------------------------------------------
    def test_the_listener_is_the_process_own_descriptor(self):
        self._offline_client(self.OLD_PID)
        self.assertTrue(hw.listens_on(self.OLD_PID, baseline.OFFLINE_SOCKET))
        # the path alone is not enough: another process's socket, a
        # connected (not listening) socket, another path, no pid
        self.assertFalse(hw.listens_on(self.NEW_PID, baseline.OFFLINE_SOCKET))
        self._socket(self.NEW_PID, 7001, baseline.OFFLINE_SOCKET, listening=False)
        self.assertFalse(hw.listens_on(self.NEW_PID, baseline.OFFLINE_SOCKET))
        self.assertFalse(hw.listens_on(self.OLD_PID, "/run/forgefirm/grbl.ctl"))
        self.assertFalse(hw.listens_on(None, baseline.OFFLINE_SOCKET))

    # -- the takeover ------------------------------------------------------
    def test_the_offline_service_comes_back_offline(self):
        # exthost.armed-freeze's case: the setup record put back under a
        # takeover while the offline service ran
        self._offline_client(self.OLD_PID)
        cap, run = self.takeover()
        self.assertEqual(self.calls, [("forgectrl", "stop"), ("forgectrl", "start")])
        self.assertEqual(self.at_start, ["gfcloud-nohunt", "gfcloud-offline"])
        self.assertTrue(hw.listens_on(self.NEW_PID, baseline.OFFLINE_SOCKET))
        self.assertNotIn("restart_clients", cap)
        self.assertEqual(self.markers_left(), [])
        self.assertTrue(any("back as the offline service (pid %d)" % self.NEW_PID in l for l in run.lines),
                        run.lines)

    def test_an_online_client_after_the_offline_service_fails_the_run(self):
        # the client that came up is not listening: an online client, or
        # a marker the start never saw
        self._offline_client(self.OLD_PID)
        real = self._initd

        def lost_marker(service, action, timeout=60):
            if action == "start" and os.path.exists(baseline.OFFLINE_MARKER):
                os.remove(baseline.OFFLINE_MARKER)
            return real(service, action, timeout)
        hw.initd = lost_marker
        cap, run = self.takeover()
        self.assertEqual(cap["restart_clients"],
                         [{"where": "takeover end", "expected": "the offline service",
                           "found": "a client that is not the offline service (pid %d)" % self.NEW_PID}])
        left = baseline.Baseline(run.log)
        items = []
        left._preserved(items, cap)
        self.assertEqual([x.item for x in items], ["cloud client at the takeover end"])
        self.assertTrue(items[0].action.startswith("not restored"), items[0].action)

    def test_no_client_started_takes_the_markers_down(self):
        # the gate closed, or standby: nothing read the markers, and a
        # later start must not come up offline or without its hunt
        self._offline_client(self.OLD_PID)
        self.comes_up = "gated"
        cap, run = self.takeover()
        self.assertEqual(self.at_start, ["gfcloud-nohunt", "gfcloud-offline"])
        self.assertEqual(self.markers_left(), [])
        self.assertEqual(len(cap["restart_clients"]), 1)
        self.assertIn("no cloud client running", cap["restart_clients"][0]["found"])

    def test_an_online_client_comes_back_without_the_hunt(self):
        cap, run = self.takeover()
        self.assertEqual(self.at_start, ["gfcloud-nohunt"])
        self.assertFalse(hw.listens_on(self.NEW_PID, baseline.OFFLINE_SOCKET))
        self.assertNotIn("restart_clients", cap)
        self.assertEqual(self.markers_left(), [])

    def test_a_stale_offline_socket_file_is_not_the_offline_service(self):
        # a socket another process left at the path: the online client
        # found stays online
        self._socket(99, 7002, baseline.OFFLINE_SOCKET)
        cap, run = self.takeover()
        self.assertEqual(self.at_start, ["gfcloud-nohunt"])
        self.assertNotIn("restart_clients", cap)

    def test_grbl_mode_starts_under_no_marker(self):
        self.fc.state["mode"] = {"mode": "grbl", "controller": "running", "pid": self.OLD_PID,
                                 "motion": "verified"}
        cap, run = self.takeover()
        self.assertEqual(self.at_start, [])
        self.assertNotIn("restart_clients", cap)


if __name__ == "__main__":
    unittest.main()
