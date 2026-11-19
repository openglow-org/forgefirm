# Copyright 2026 514 LLC d/b/a OpenGlow
# Written by Scott Wiederhold
# https://community.openglow.org
# SPDX-License-Identifier:    MIT

import contextlib
import fnmatch
import glob
import gzip
import hashlib
import io
import json
import os
import re
import shutil
import struct
import subprocess
import sys
import tarfile
import tempfile
import time
import types
import unittest
from unittest import mock

from forgetest.suite import image

# The layer's scripts, when the tests run from a checkout of forgefirm.
REPO = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
LAYER = os.path.join(REPO, "meta-forgefirm", "recipes-forgefirm")
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
# global IPv4 and IPv6 address and a link-local one. With "held", the
# banner's lock is held while the script runs, and the script is given a
# second. Exit 97: the namespace could not be set up.
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
if [ "$4" = held ]; then
    mkdir -p /run/forgefirm || exit 97
    flock /run/forgefirm/issue.lock timeout 1 sh "$SCRIPT"
else
    sh "$SCRIPT"
fi
echo "rc=$?"
echo "name=$(hostname)"
echo "file=$(cat /etc/hostname)"
echo "--- issue"
cat /etc/issue
"""


def run_layer_script(script, macs, etc, net=False, held=False):
    """Run a layer script over a scratch tree: macs {interface: address or
    None for a directory with no address file}, etc {file: text}, held the
    banner's lock held while it runs. Returns the namespace's output, or
    None when no namespace can be made here (unprivileged where user
    namespaces are open, else root via sudo -n)."""
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
        args = ["sh", "-c", NAMESPACE, "sh", tree, os.path.join(LAYER, script), "net" if net else "",
                "held" if held else ""]
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


def no_namespace(case, why):
    """A layer script that could not be run here: a skip on a workstation,
    a failure on a hosted runner, which has root through sudo, so a case
    that did not run there is a broken harness, never a pass."""
    if os.environ.get("GITHUB_ACTIONS"):
        case.fail(why)
    case.skipTest(why)


@unittest.skipUnless(os.path.isdir(LAYER) and shutil.which("unshare"), "needs Linux and a forgefirm checkout")
class MachineNameTests(unittest.TestCase):
    """forgefirm-hostname and forgefirm-banner, the layer's own files."""

    def named(self, macs):
        out = run_layer_script("forgefirm-hostname/files/forgefirm-hostname", macs, {"hostname": "forgefirm\n"})
        if out is None:
            no_namespace(self, "no namespace can be made here")
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
            no_namespace(self, "no namespace can be made here")
        self.assertEqual(field(out, "rc"), "0", out)
        issue = out.split("--- issue\n", 1)[1]
        self.assertTrue(issue.startswith(base + "\n"), issue)
        heading, *urls = issue[len(base) + 1:].strip("\n").split("\n")
        self.assertEqual(urls, ["  https://192.0.2.5/", "  https://[2001:db8::5]/"], issue)
        self.assertFalse(heading.startswith("#") or ".local" in issue, issue)

    def test_the_banner_waits_for_a_run_in_progress(self):
        # The boot's run and a lease event can come together: while one
        # holds the banner, the other writes nothing.
        base = "ForgeFIRM\n"
        out = run_layer_script("forgefirm-banner/files/forgefirm-banner", {"wlan0": "02:00:00:00:12:ab"},
                               {"issue": base}, net=True, held=True)
        if out is None:
            no_namespace(self, "no namespace can be made here")
        self.assertEqual(field(out, "rc"), "124", "the banner ran past the lock: " + out)
        self.assertEqual(out.split("--- issue\n", 1)[1], base, out)


# A harness that needs /data runs in a root of its own, so nothing it does
# can make or leave a /data on the host: a private mount namespace as root,
# a tmpfs scratch root holding a recursive bind of every top-level
# directory of the host but /data (and its top-level symlinks as they are),
# and the harness run under chroot there, where /data is the harness's to
# make. Two harnesses at once never share it, and its mounts go with the
# namespace. Exit 97: the root could not be made. Arguments: the empty
# directory to make it on, the harness script, then the script's own.
PRIVATE_ROOT = r"""
R=$1; S=$2; shift 2
mount --make-rprivate / && mount -t tmpfs -o mode=0755 tmpfs "$R" || exit 97
for p in /*; do
    n=${p#/}
    [ "$n" = data ] && continue
    if [ -L "$p" ]; then
        ln -s "$(readlink "$p")" "$R/$n" || exit 97
    elif [ -d "$p" ]; then
        { mkdir "$R/$n" && mount --rbind "$p" "$R/$n"; } || exit 97
    fi
done
exec chroot "$R" sh -c "$S" sh "$@"
"""


def private_root(case, script, *args):
    """The command that runs a harness script in a root of its own (root
    through sudo -n), on a directory the case removes when it ends."""
    where = tempfile.mkdtemp(prefix="forgetest-root.")
    case.addCleanup(os.rmdir, where)
    return ["sudo", "-n", "unshare", "-m", "sh", "-c", PRIVATE_ROOT, "sh", where, script] + list(args)


# forgefirm-users rendering a record, as the image runs it, in a private
# root as root (PRIVATE_ROOT): /run a fresh tmpfs, /etc read-only with the
# scratch tree's account files over the host's, and the scratch data
# directory over /data (made in the private root, as fflog_e2e.sh does,
# and removed after). The data directory is root's, as
# on the machine, whoever made the scratch tree. With "held", the harness
# holds the render lock and the render waits no time for it. Then the four
# account files as rendered, and, as the account's own uid when the render
# made it, what it can do with its home and with the data directory around
# it. Exit 97: the namespace could not be set up.
ACCOUNTS = r"""
T=$1; SCRIPT=$2
made=
[ -d /data ] || { mkdir /data && made=1; } || exit 97
chown -R 0:0 "$T/data" &&
    mount --make-rprivate / && mount -t tmpfs tmpfs /run &&
    mount -t overlay overlay -o "lowerdir=$T/etc:/etc" /etc &&
    mount --bind "$T/data" /data || { [ -z "$made" ] || rmdir /data; exit 97; }
if [ "$3" = held ]; then
    { mkdir -p /run/forgefirm && exec 8> /run/forgefirm/accounts.lock && flock 8; } || exit 97
    export FF_USERS_LOCK_WAIT=0
fi
sh "$SCRIPT" reload
echo "rc=$?"
echo "mode=$(stat -c %a /data/forgefirm)"
python3 - <<'PY'
import errno, json, os, pwd
print("files=" + json.dumps({f: open("/etc/" + f).read() for f in ("passwd", "shadow", "group", "gshadow")}))
try:
    p = pwd.getpwnam("bench")
except KeyError:
    print("reach=null")
    raise SystemExit
os.setgroups([])
os.setgid(p.pw_gid)
os.setuid(p.pw_uid)
def tried(fn):
    try:
        fn()
        return "ok"
    except OSError as e:
        return errno.errorcode.get(e.errno, str(e.errno))
def write():
    path = os.path.join(p.pw_dir, ".reach")
    with open(path, "w") as f:
        f.write("x")
    os.remove(path)
print("reach=" + json.dumps({"uid": os.getuid(), "home": p.pw_dir, "cd": tried(lambda: os.chdir(p.pw_dir)),
                             "write": tried(write), "list_data_dir": tried(lambda: os.listdir("/data/forgefirm")),
                             "read_record": tried(lambda: open("/data/forgefirm/users").read())}))
PY
cd /
umount /data
[ -z "$made" ] || rmdir /data
rm -rf "$T/data"
"""


@unittest.skipUnless(os.path.isdir(LAYER) and shutil.which("unshare"), "needs Linux and a forgefirm checkout")
class AccountRenderTests(unittest.TestCase):
    """forgefirm-users, the layer's own init script, rendering the record."""

    ETC = {"passwd": "root:x:0:0:root:/root:/bin/sh\n", "shadow": "root::19000:0:99999:7:::\n",
           "group": "root:x:0:\n", "gshadow": "root:*::\n"}
    RECORD = "bench:$6$salt$hash:1000\n"

    def render(self, record=RECORD, etc=None, data=None, held=False):
        """One render of record over the account files etc ({name: text},
        the image's own), with data ({name: text}) beside the record in
        /data/forgefirm. Returns (the rendered files, the account's reach,
        the harness's output)."""
        tree = tempfile.mkdtemp(prefix="forgetest-users.")
        self.addCleanup(shutil.rmtree, tree, ignore_errors=True)
        os.makedirs(os.path.join(tree, "etc"))
        for name, text in dict(self.ETC, **(etc or {})).items():
            with open(os.path.join(tree, "etc", name), "w") as f:
                f.write(text)
        d = os.path.join(tree, "data", "forgefirm")
        os.makedirs(d)
        os.chmod(d, 0o700)                          # as a strict umask leaves it
        for name, text in dict(data or {}, users=record).items():
            with open(os.path.join(d, name), "w") as f:
                f.write(text)
            os.chmod(os.path.join(d, name), 0o600)
        try:
            p = subprocess.run(private_root(self, ACCOUNTS, tree,
                                            os.path.join(LAYER, "forgefirm-users", "files", "forgefirm-users.init"),
                                            *(["held"] if held else [])), capture_output=True, text=True, timeout=60)
        except (OSError, subprocess.TimeoutExpired):
            p = None
        if p is None or "rc=" not in p.stdout:
            if p is not None and p.returncode == 97:
                raise AssertionError("the namespace was made but not set up: %s" % p.stderr.strip())
            no_namespace(self, "no namespace can be made here (root through sudo -n)")
        self.assertEqual(field(p.stdout, "rc"), "0", p.stdout + p.stderr)
        return json.loads(field(p.stdout, "files")), json.loads(field(p.stdout, "reach")), p.stdout

    @staticmethod
    def lines(text, name):
        return [ln.split(":") for ln in text.splitlines() if ln.split(":")[0] == name]

    def test_the_render_opens_the_data_directory_to_its_account_and_no_further(self):
        _files, reach, out = self.render()
        self.assertEqual(field(out, "mode"), "711", out)
        self.assertEqual((reach["uid"], reach["home"]), (1000, "/data/forgefirm/home/bench"), reach)
        self.assertEqual((reach["cd"], reach["write"]), ("ok", "ok"), reach)
        self.assertEqual((reach["list_data_dir"], reach["read_record"]), ("EACCES", "EACCES"), reach)

    def test_the_shadow_line_carries_no_aging(self):
        # A "last change" day of 0 reads as a password expired by root to
        # sshd: every field after the hash is empty, whatever the clock says.
        files, _reach, _out = self.render()
        self.assertEqual(self.lines(files["shadow"], "bench"), [["bench", "$6$salt$hash"] + [""] * 7], files["shadow"])

    def test_a_record_line_that_names_a_system_account_is_skipped(self):
        # below the operator range (sshd, the group video) and above it (nobody)
        etc = {"passwd": self.ETC["passwd"] + "sshd:x:799:799::/var/run/sshd:/bin/false\n"
                                              "nobody:x:65534:65534:nobody:/nonexistent:/sbin/nologin\n",
               "shadow": self.ETC["shadow"] + "sshd:!:19000::::::\n",
               "group": self.ETC["group"] + "sshd:x:799:\nvideo:x:44:\n"}
        files, reach, _out = self.render("sshd:$6$x$y:1001\nvideo:$6$x$y:1002\nnobody:$6$x$y:1003\n" + self.RECORD,
                                         etc=etc)
        self.assertEqual(self.lines(files["passwd"], "sshd"), [["sshd", "x", "799", "799", "", "/var/run/sshd",
                                                                "/bin/false"]], files["passwd"])
        self.assertEqual([ln[2] for ln in self.lines(files["passwd"], "nobody")], ["65534"], files["passwd"])
        self.assertEqual(self.lines(files["shadow"], "sshd"), [["sshd", "!", "19000"] + [""] * 6], files["shadow"])
        self.assertEqual([self.lines(files["group"], n) for n in ("sshd", "video")],
                         [[["sshd", "x", "799", ""]], [["video", "x", "44", ""]]], files["group"])
        self.assertEqual(self.lines(files["passwd"], "video"), [], files["passwd"])
        self.assertEqual(reach["uid"], 1000, "the record's other account is rendered")

    def test_a_pending_account_reset_renders_no_record_account(self):
        files, reach, _out = self.render(data={"users.reset": ""})
        self.assertIsNone(reach, "the account is rendered while its reset is pending")
        self.assertEqual(self.lines(files["shadow"], "bench"), [], files["shadow"])

    def test_a_render_waits_for_the_one_in_progress(self):
        # The lock held by another render, which this one waits no time for:
        # it writes nothing.
        files, reach, _out = self.render(held=True)
        self.assertIsNone(reach, "the render wrote the account files while another render held them")
        self.assertEqual(files["shadow"], self.ETC["shadow"])


# The grblhal init script asking forgectrl's supervisor for a start, as the
# image runs it, in a private root as root (PRIVATE_ROOT): the scratch data
# directory over /data (made in the private root, and removed after), and
# a curl first on its PATH that writes down its command line and what it
# read on its standard input. Exit 97: the namespace could not be set up.
CONTROLLER_INIT = r"""
T=$1; SCRIPT=$2
made=
[ -d /data ] || { mkdir /data && made=1; } || exit 97
mount --make-rprivate / && mount --bind "$T/data" /data || { [ -z "$made" ] || rmdir /data; exit 97; }
PATH="$T/bin:$PATH" sh "$SCRIPT" start > /dev/null
echo "rc=$?"
umount /data
[ -z "$made" ] || rmdir /data
"""


@unittest.skipUnless(os.path.isdir(LAYER) and shutil.which("unshare"), "needs Linux and a forgefirm checkout")
class ControllerInitTests(unittest.TestCase):
    """grblhal.init, the layer's controller init script."""

    def test_the_panel_token_never_rides_curls_command_line(self):
        # Every local account reads a process's command line in /proc.
        tree = tempfile.mkdtemp(prefix="forgetest-ctlinit.")
        self.addCleanup(shutil.rmtree, tree, ignore_errors=True)
        token = "0123456789abcdef" * 4
        os.makedirs(os.path.join(tree, "data", "forgefirm"))
        os.makedirs(os.path.join(tree, "bin"))
        with open(os.path.join(tree, "data", "forgefirm", "panel.token"), "w") as f:
            f.write(token + "\n")
        with open(os.path.join(tree, "bin", "curl"), "w") as f:
            f.write('#!/bin/sh\nprintf "%%s\\n" "$@" > %s/argv\ncat > %s/stdin\n' % (tree, tree))
        os.chmod(os.path.join(tree, "bin", "curl"), 0o755)
        try:
            p = subprocess.run(private_root(self, CONTROLLER_INIT, tree,
                                            os.path.join(LAYER, "grblhal-glowforge", "grblhal-glowforge", "grblhal.init")),
                               capture_output=True, text=True, timeout=60)
        except (OSError, subprocess.TimeoutExpired):
            p = None
        if p is None or "rc=" not in p.stdout:
            if p is not None and p.returncode == 97:
                raise AssertionError("the namespace was made but not set up: %s" % p.stderr.strip())
            no_namespace(self, "no namespace can be made here (root through sudo -n)")
        with open(os.path.join(tree, "argv")) as f:
            argv = f.read().splitlines()
        with open(os.path.join(tree, "stdin")) as f:
            stdin = f.read()
        self.assertEqual(argv[-1], "http://127.0.0.1:80/controller/start", argv)
        self.assertFalse([a for a in argv if token in a], argv)
        self.assertIn('header = "X-ForgeFIRM-Token: %s"\n' % token, stdin)


# A stand-in for `python3 -m forgetest` as the init script starts it: it
# writes down its environment, notes each TERM, and ends on one unless the
# tree's mode file says "deaf".
FORGETEST_STAND_IN = """#!%(python)s
import json, os, signal, sys, time
T = %(tree)r
with open(os.path.join(T, "env.%%d" %% os.getpid()), "w") as f:
    json.dump(dict(os.environ), f)
deaf = open(os.path.join(T, "mode")).read().strip() == "deaf"
def on_term(*_):
    with open(os.path.join(T, "terms"), "a") as f:
        f.write("%%d\\n" %% os.getpid())
    if not deaf:
        sys.exit(0)
signal.signal(signal.SIGTERM, on_term)
while True:
    time.sleep(0.05)
"""


def alive(pid):
    """Whether pid runs: a zombie, ended and not yet reaped, does not."""
    try:
        with open("/proc/%d/stat" % pid) as f:
            return f.read().rsplit(") ", 1)[1].split()[0] != "Z"
    except (OSError, IndexError):
        return False


def until(cond, timeout=10.0):
    deadline = time.monotonic() + timeout
    while not cond():
        if time.monotonic() > deadline:
            return False
        time.sleep(0.01)
    return True


@unittest.skipUnless(os.path.isdir("/proc/self"), "needs Linux")
class ForgetestInitTests(unittest.TestCase):
    """forgetest.init, the dev image's init script for the acceptance
    daemon, run over a scratch tree: its pidfile, data directory and
    interpreter moved there, the daemon a stand-in."""

    def setUp(self):
        sbin = os.pathsep.join([os.environ.get("PATH", ""), "/usr/sbin", "/sbin"])
        if not shutil.which("start-stop-daemon", path=sbin):
            no_namespace(self, "no start-stop-daemon here")
        self.path = sbin
        self.tree = tempfile.mkdtemp(prefix="forgetest-ftinit.")
        self.addCleanup(shutil.rmtree, self.tree, ignore_errors=True)
        self.addCleanup(self.kill_all)
        with open(os.path.join(self.tree, "mode"), "w") as f:
            f.write("plain\n")
        stand_in = os.path.join(self.tree, "python3")
        with open(stand_in, "w") as f:
            f.write(FORGETEST_STAND_IN % {"python": sys.executable, "tree": self.tree})
        os.chmod(stand_in, 0o755)
        with open(os.path.join(REPO, "forgetest", "forgetest.init")) as f:
            script = f.read()
        for old, new in (("PIDFILE=/var/run/forgetest.pid", "PIDFILE=%s/forgetest.pid" % self.tree),
                         ("DATA=/data/forgetest", "DATA=%s/data" % self.tree),
                         ("PYTHON=/usr/bin/python3", "PYTHON=" + stand_in)):
            self.assertIn(old + "\n", script)
            script = script.replace(old + "\n", new + "\n")
        self.init_path = os.path.join(self.tree, "forgetest.init")
        with open(self.init_path, "w") as f:
            f.write(script)

    def init(self, action, path=None, **env):
        return subprocess.run(["sh", self.init_path, action], capture_output=True, text=True, timeout=60,
                              env=dict(os.environ, PATH=path or self.path, **env))

    def daemons(self):
        """The pids whose command line names the stand-in: the daemon, and
        a launch on its way to being one."""
        out = []
        for d in os.listdir("/proc"):
            if not d.isdigit() or not alive(int(d)):
                continue
            try:
                with open("/proc/%s/cmdline" % d, "rb") as f:
                    if (self.tree + "/python3").encode() in f.read():
                        out.append(int(d))
            except OSError:
                pass
        return out

    def started(self):
        return sorted(int(n[4:]) for n in os.listdir(self.tree) if n.startswith("env."))

    def kill_all(self):
        for pid in self.daemons():
            try:
                os.kill(pid, 9)
            except OSError:
                pass

    def test_a_second_start_leaves_the_daemon_alone_and_the_daemon_takes_no_environment(self):
        p = self.init("start", FORGETEST_MANIFEST="/elsewhere/manifest.json", GF_SYSFS_ROOT="/elsewhere/")
        self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
        self.assertTrue(until(lambda: self.started()), "the daemon never started")
        first = self.started()
        p = self.init("start")
        self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
        self.assertEqual(self.daemons(), first, "a second start launched a second daemon")
        with open(os.path.join(self.tree, "env.%d" % first[0])) as f:
            env = json.load(f)
        self.assertNotIn("FORGETEST_MANIFEST", env)
        self.assertNotIn("GF_SYSFS_ROOT", env)
        self.assertEqual(env.get("PATH"), "/usr/sbin:/usr/bin:/sbin:/bin")

    def test_a_pidfile_naming_another_process_is_not_the_daemon(self):
        # The daemon ended without a stop and its pid went to another
        # process, a shell (start-stop-daemon's own -x): a stop leaves that
        # process alone, and a start launches.
        other = subprocess.Popen(["/bin/sh", "-c", "sleep 60; :"], start_new_session=True)
        self.addCleanup(other.wait)
        self.addCleanup(os.killpg, other.pid, 9)
        pidfile = os.path.join(self.tree, "forgetest.pid")
        with open(pidfile, "w") as f:
            f.write("%d\n" % other.pid)
        self.assertEqual(self.init("stop").returncode, 0)
        self.assertTrue(alive(other.pid), "stop signaled a process that is not the daemon")
        with open(pidfile, "w") as f:
            f.write("%d\n" % other.pid)
        p = self.init("start")
        self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
        self.assertTrue(until(lambda: self.started()), "a start took another process for the daemon")
        self.assertTrue(alive(other.pid))

    def test_stop_waits_for_the_daemon_and_kills_one_that_does_not_end(self):
        # The daemon takes no notice of TERM; the script's waits run on a
        # sleep that returns once the daemon has noted the TERM (1 s at
        # most), at once after that, and counts its calls. The KILL ends
        # the daemon, and the one left unreaped, a zombie, has ended too:
        # the wait after the KILL takes two sleeps at most, of its five.
        with open(os.path.join(self.tree, "mode"), "w") as f:
            f.write("deaf\n")
        fast = os.path.join(self.tree, "bin")
        os.makedirs(fast)
        with open(os.path.join(fast, "sleep"), "w") as f:
            f.write("#!/bin/sh\necho >> %s/sleeps\nn=0\nwhile [ ! -e %s/terms ] && [ $n -lt 100 ]; do\n"
                    "    /bin/sleep 0.01\n    n=$((n + 1))\ndone\n" % (self.tree, self.tree))
        os.chmod(os.path.join(fast, "sleep"), 0o755)
        self.assertEqual(self.init("start").returncode, 0)
        self.assertTrue(until(lambda: self.started()), "the daemon never started")
        pid = self.started()[0]
        p = self.init("stop", path=fast + os.pathsep + self.path)
        self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
        with open(os.path.join(self.tree, "terms")) as f:
            self.assertEqual(f.read().split(), [str(pid)], "TERM was not asked first")
        self.assertFalse(alive(pid), "stop returned with the daemon still running")
        self.assertFalse(os.path.exists(os.path.join(self.tree, "forgetest.pid")))
        with open(os.path.join(self.tree, "sleeps")) as f:
            self.assertLessEqual(len(f.read().splitlines()), 10 + 2, "the wait after the KILL outlasted the daemon")


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

    def test_a_license_text_tar_cannot_read_is_refused_and_the_tree_left(self):
        """A member tar cannot read fails the pack, bundle removed and tree
        left: tar skips that member, and the manifest check alone passes
        the bundle without it."""
        if os.geteuid() == 0:
            self.skipTest("root reads every file")
        self.make_tree(self.RECIPES)
        d = os.path.join(self.tree, "zlib")
        os.makedirs(d)
        locked = os.path.join(d, "generic_Zlib")
        with open(locked, "w") as f:
            f.write("zlib license\n")
        os.chmod(locked, 0)                              # rmtree still unlinks it
        self.assertRefused(self.pack())
        self.assertTrue(os.path.isfile(self.manifest))


# forgefirm-data, the layer's boot step after mountall, in a private root
# as root (PRIVATE_ROOT): /run a fresh tmpfs and the scratch tree's eMMC
# data directory over /data (made in the private root, and removed after). The kernel command line and the card's data partition
# (an ext4 image file, loop-mounted) are the script's two host-test
# settings. A directory named in the scratch tree's busy file (one path
# under /data to a line) gets a mount of its own first, so it cannot be
# removed. With the no-data-mount file, nothing is mounted at /data: it is
# a symlink to the eMMC tree (made in the private root), which
# /proc/mounts never names. With the fail-mount file, the script's mount
# refuses once the target that file names (a shim first on its PATH).
# With a third argument, the logging init script's legacy sweep runs after
# the step, as rc5 runs it later in the boot. Then what /data shows, read
# back as JSON, with the eMMC's own files read beside it from the scratch
# tree. Exit 97: the namespace could not be set up.
CARD_DATA = r"""
T=$1; SCRIPT=$2; SWEEP=$3
made=
if [ -f "$T/no-data-mount" ]; then
    { [ -e /data ] || [ -L /data ]; } && exit 97
    ln -s "$T/emmc" /data && made=link || exit 97
    mount --make-rprivate / && mount -t tmpfs tmpfs /run || { rm -f /data; exit 97; }
else
    [ -d /data ] || { mkdir /data && made=1; } || exit 97
    mount --make-rprivate / && mount -t tmpfs tmpfs /run &&
        mount --bind "$T/emmc" /data || { [ -z "$made" ] || rmdir /data; exit 97; }
fi
if [ -f "$T/busy" ]; then
    while read -r d; do
        mount -t tmpfs tmpfs "/data/$d" || { umount -R /data; [ -z "$made" ] || rmdir /data; exit 97; }
    done < "$T/busy"
fi
if [ -f "$T/fail-mount" ]; then
    REAL_MOUNT=$(command -v mount) FAIL_MOUNT="$T/fail-mount" PATH="$T/bin:$PATH"
    export REAL_MOUNT FAIL_MOUNT PATH
fi
FF_CMDLINE="$T/cmdline" FF_CARD_DEV="$T/card.img" FF_DEV_MARKER="$T/dev-marker" \
    sh "$SCRIPT" start > "$T/script.log" 2>&1
echo "rc=$?"
if [ -n "$SWEEP" ]; then
    ( eval "$(sed -n '/^LEGACY=/p; /^sweep_legacy() {/,/^}/p' "$SWEEP")" && sweep_legacy ) >> "$T/script.log" 2>&1
fi
python3 - "$T" <<'PY'
import errno, json, os, sys
T = sys.argv[1]
def read(p):
    try:
        return open(p).read()
    except OSError:
        return None
def write(d):
    try:
        with open(os.path.join(d, ".w"), "w") as f:
            f.write("x")
        os.remove(os.path.join(d, ".w"))
        return "ok"
    except OSError as e:
        return errno.errorcode.get(e.errno, str(e.errno))
def mode(p):
    try:
        return "%o" % (os.stat(p).st_mode & 0o777)
    except OSError:
        return None
print("probe=" + json.dumps({
    "state": (read("/run/forgefirm-data.state") or "").strip(),
    "users": read("/data/forgefirm/users"),
    "conf": read("/data/forgefirm/forgefirm.conf"),
    "ssh_key_mode": mode("/data/forgefirm/ssh/ssh_host_ed25519_key"),
    "cert": read("/data/forgefirm/tls/cert.pem"),
    "timestamp": read("/data/forgefirm/timestamp"),
    "archive": read("/data/forgefirm/archive/manifest"),
    "archive_write": write("/data/forgefirm/archive") if os.path.isdir("/data/forgefirm/archive") else None,
    "data_write": write("/data/forgefirm"),
    "logs_write": write("/data/log/forgefirm"),
    "emmc_view_write": write("/run/forgefirm-emmc") if os.path.isdir("/run/forgefirm-emmc/forgefirm") else None,
    "system_log": read("/data/log/forgefirm/system/system.log"),
    "results": read("/data/forgetest/results.jsonl"),
    "wifi": read("/data/etc/wpa_supplicant.conf"),
    "import_left": os.path.exists("/run/forgefirm-card/import"),
    "listing": sorted(os.listdir("/data/forgefirm")),
    "emmc_users": read(T + "/emmc/forgefirm/users"),
    "emmc_conf": read(T + "/emmc/forgefirm/forgefirm.conf"),
    "ssh_key": read("/data/forgefirm/ssh/ssh_host_ed25519_key"),
    "mounts": {f[1]: f[3] for f in (ln.split() for ln in open("/proc/mounts"))
               if f[1].startswith(("/run/forgefirm", "/data"))},
    "emmc_legacy": ("left" if os.path.exists(T + "/emmc/forgectrl.log") else
                    "moved" if os.path.exists(T + "/emmc/forgefirm/legacy-logs/forgectrl.log") else None),
    "log": read(T + "/script.log"),
}))
PY
cd /
if [ "$made" = link ]; then
    rm -f /data
else
    umount -R /data 2>/dev/null || umount -l /data
    [ -z "$made" ] || rmdir /data
fi
rm -rf "$T" 2>/dev/null
"""

# The mount the fail-mount setting puts first on the script's PATH: it
# refuses the target named in $FAIL_MOUNT once, and runs the real mount
# for everything else.
MOUNT_SHIM = """#!/bin/sh
for a; do last=$a; done
if [ "$last" = "$(cat "$FAIL_MOUNT")" ] && [ ! -e "$FAIL_MOUNT.done" ]; then
    : > "$FAIL_MOUNT.done"
    echo "mount: $last: refused for the test" >&2
    exit 32
fi
exec "$REAL_MOUNT" "$@"
"""


@unittest.skipUnless(os.path.isdir(LAYER) and shutil.which("unshare") and shutil.which("mkfs.ext4"),
                     "needs Linux, mkfs.ext4 and a forgefirm checkout")
class CardDataTests(unittest.TestCase):
    """forgefirm-data, the layer's boot step: an SD card's own settings and
    logs on a boot from the card, /data left as it is on any other boot,
    and a requested reset."""

    SCRIPT = os.path.join(LAYER, "forgefirm-data", "files", "forgefirm-data")
    LOGGING = os.path.join(LAYER, "forgefirm-logging", "files", "forgefirm-logging.init")

    def tree(self, root, files):
        for rel, text in files.items():
            path = os.path.join(root, rel)
            os.makedirs(os.path.dirname(path), exist_ok=True)
            if text is not None:
                with open(path, "w") as f:
                    f.write(text)
                os.chmod(path, 0o600)

    def run_boot(self, cmdline, emmc, card=None, dev=True, busy=(), card_feature=None, no_data_mount=False,
                 fail_mount=None, sweep=False):
        """One boot of the step: the eMMC's /data tree and the card's data
        partition (None: no partition, card_feature: an ext4 feature set on
        it), as
        {path: text}, on a dev image or a release one; busy names
        directories under /data that cannot be removed (each has a mount of
        its own); no_data_mount leaves /data unmounted; fail_mount names a
        target the script's mount refuses once; sweep runs the logging init
        script's legacy sweep after the step. Returns the probe."""
        t = tempfile.mkdtemp(prefix="forgetest-card.")
        # Root's files go through sudo; where there is no root (the boot did
        # not run) the tree is this user's own.
        self.addCleanup(shutil.rmtree, t, ignore_errors=True)
        self.addCleanup(subprocess.run, ["sudo", "-n", "rm", "-rf", t], capture_output=True)
        self.tree(os.path.join(t, "emmc"), emmc)
        if busy:
            with open(os.path.join(t, "busy"), "w") as f:
                f.write("".join(d + "\n" for d in busy))
        if dev:
            open(os.path.join(t, "dev-marker"), "w").close()
        if no_data_mount:
            open(os.path.join(t, "no-data-mount"), "w").close()
        if fail_mount:
            with open(os.path.join(t, "fail-mount"), "w") as f:
                f.write(fail_mount + "\n")
            os.makedirs(os.path.join(t, "bin"))
            with open(os.path.join(t, "bin", "mount"), "w") as f:
                f.write(MOUNT_SHIM)
            os.chmod(os.path.join(t, "bin", "mount"), 0o755)
        with open(os.path.join(t, "cmdline"), "w") as f:
            f.write(cmdline + "\n")
        if card is not None:
            src = os.path.join(t, "card")
            os.makedirs(src)
            self.tree(src, card)
            p = subprocess.run(["mkfs.ext4", "-q", "-F", "-d", src, os.path.join(t, "card.img"), "8M"],
                               capture_output=True, text=True)
            self.assertEqual(p.returncode, 0, p.stderr)
            if card_feature:
                p = subprocess.run(["debugfs", "-w", "-R", "feature " + card_feature, os.path.join(t, "card.img")],
                                   capture_output=True, text=True)
                self.assertIn(card_feature, p.stdout, p.stderr)
        args = [t, self.SCRIPT] + ([self.LOGGING] if sweep else [])
        try:
            p = subprocess.run(private_root(self, CARD_DATA, *args), capture_output=True, text=True, timeout=120)
        except (OSError, subprocess.TimeoutExpired):
            p = None
        if p is None or "rc=" not in p.stdout:
            if p is not None and p.returncode == 97:
                raise AssertionError("the namespace was made but not set up: %s" % p.stderr.strip())
            why = "no namespace can be made here (root through sudo -n)"
            # A hosted runner has root through sudo: there, a boot that did
            # not run is a hang or a broken harness, never a skip.
            if os.environ.get("GITHUB_ACTIONS"):
                self.fail("%s: %s" % (why, "the boot did not finish in 120 s" if p is None
                                      else "status %s, %s" % (p.returncode, p.stderr.strip())))
            self.skipTest(why)
        self.assertEqual(field(p.stdout, "rc"), "0", p.stdout + p.stderr)
        return json.loads(field(p.stdout, "probe"))

    EMMC = {
        "forgefirm/users": "bench:$6$salt$hash:1000\n",
        "forgefirm/forgefirm.conf": "wifi_country = DE\next_enabled = 1\n",
        "forgefirm/ssh/ssh_host_ed25519_key": "KEY\n",
        "forgefirm/tls/cert.pem": "CERT\n",
        "forgefirm/timestamp": "202610211901\n",
        "forgefirm/archive/manifest": "factory-rootfs-1.img.gz\n",
        "forgetest/results.jsonl": "{}\n",
        "log/forgefirm/system/system.log": "bench work\n",
        "etc/wpa_supplicant.conf": "network={}\n",
    }
    CARD = {
        "forgefirm/users": "forgefirm:$6$forgefirmsdcard$x:1000\n",
        "forgefirm/forgefirm.conf": "cool_intake_fans = 2\n",
        "forgefirm/setup.seed": "",
        "log/.keep": "",
        "import": "",
    }

    def test_a_card_boot_shows_the_cards_settings_and_logs_and_brings_the_identity_once(self):
        emmc = dict(self.EMMC, **{"forgectrl.log": "old\n"})
        pr = self.run_boot("console=ttymxc0 root=/dev/mmcblk1p1 rw", emmc, self.CARD, sweep=True)
        self.assertEqual(pr["state"], "card", pr["log"])
        # a filesystem error found while the card is in use stops its writes,
        # and the folders shown over /data honor no set-uid bit and no
        # device node, as the eMMC's /data
        opts = pr["mounts"].get("/run/forgefirm-card", "").split(",")
        self.assertTrue({"errors=remount-ro", "nosuid", "nodev"} <= set(opts), pr["mounts"])
        self.assertTrue({"nosuid", "nodev"} <= set(pr["mounts"].get("/data/forgefirm", "").split(",")), pr["mounts"])
        # the logs from before syslog stay where they are on the eMMC: the
        # folder the sweep would take them to is the card's
        self.assertEqual(pr["emmc_legacy"], "left", pr["log"])
        self.assertTrue(pr["users"].startswith("forgefirm:"), pr)
        self.assertIn("cool_intake_fans = 2", pr["conf"])
        self.assertIn("wifi_country = DE", pr["conf"])
        self.assertNotIn("ext_enabled", pr["conf"])
        self.assertEqual((pr["ssh_key_mode"], pr["cert"], pr["timestamp"]), ("600", "CERT\n", "202610211901\n"), pr)
        self.assertFalse(pr["import_left"], "the import marker stays on the card")
        # the factory archive is the eMMC's, read-only; the rest of the
        # eMMC's /data shows as before, the acceptance records among it
        self.assertEqual((pr["archive"], pr["archive_write"]), ("factory-rootfs-1.img.gz\n", "EROFS"), pr)
        self.assertEqual((pr["results"], pr["wifi"]), ("{}\n", "network={}\n"), pr)
        self.assertIsNone(pr["system_log"], "the eMMC's logs show through the card's log folder")
        self.assertEqual(pr["emmc_view_write"], "EROFS", pr)
        self.assertEqual(pr["emmc_users"], self.EMMC["forgefirm/users"])
        self.assertEqual(pr["emmc_conf"], self.EMMC["forgefirm/forgefirm.conf"])

    def test_any_other_boot_leaves_data_as_mountall_made_it(self):
        emmc = dict(self.EMMC, **{"forgectrl.log": "old\n"})
        for cmdline, card, dev, state in (("root=/dev/mmcblk2p1 rw", self.CARD, True, "emmc"),
                                          ("root=/dev/mmcblk1p1 rw", None, True, "card-none"),
                                          ("root=/dev/mmcblk1p1 rw", self.CARD, False, "card-none")):
            pr = self.run_boot(cmdline, emmc, card, dev=dev, sweep=True)
            self.assertEqual(pr["state"], state, pr["log"])
            self.assertEqual((pr["users"], pr["system_log"]), (self.EMMC["forgefirm/users"], "bench work\n"),
                             "%s: %s" % (state, pr))
            self.assertIsNone(pr["emmc_view_write"], state)
            self.assertEqual(pr["data_write"], "ok", state)
            # the logs from before syslog go to the eMMC's legacy-logs
            self.assertEqual(pr["emmc_legacy"], "moved", "%s: %s" % (state, pr["log"]))

    def test_a_card_whose_data_partition_cannot_be_used_fails_closed(self):
        # Not ForgeFIRM's: the eMMC's settings are shown, read-only, and the
        # boot's logs go to RAM, so the dev image never writes the eMMC's.
        pr = self.run_boot("root=/dev/mmcblk1p1 rw", self.EMMC, {"lost+found/.keep": ""})
        self.assertEqual(pr["state"], "card-failed", pr["log"])
        self.assertEqual(pr["users"], self.EMMC["forgefirm/users"], pr)
        self.assertEqual(pr["data_write"], "EROFS", pr)
        self.assertIsNone(pr["system_log"], "the eMMC's logs show under the boot's RAM logs")
        self.assertEqual(pr["logs_write"], "ok", pr)
        self.assertEqual(pr["emmc_conf"], self.EMMC["forgefirm/forgefirm.conf"])

    def assertFailedClosed(self, pr):
        """The eMMC's settings shown read-only, nothing of the card's."""
        self.assertEqual(pr["state"], "card-failed", pr["log"])
        self.assertEqual((pr["users"], pr["data_write"]), (self.EMMC["forgefirm/users"], "EROFS"), pr)

    def test_a_card_partition_that_mounts_read_only_is_not_used(self):
        # A write-protected card, or one with an ext4 error, mounts read-only.
        pr = self.run_boot("root=/dev/mmcblk1p1 rw", self.EMMC, self.CARD, card_feature="read-only")
        self.assertFailedClosed(pr)

    def test_the_cards_folders_are_not_shown_over_a_data_that_is_not_mounted(self):
        pr = self.run_boot("root=/dev/mmcblk1p1 rw", self.EMMC, self.CARD, no_data_mount=True)
        self.assertFailedClosed(pr)

    def test_a_folder_that_cannot_be_shown_unwinds_the_cards_mounts(self):
        # The log folder's bind is refused after the forgefirm folder's took:
        # the view that fails closed is the eMMC's, and nothing of the card
        # stays mounted.
        pr = self.run_boot("root=/dev/mmcblk1p1 rw", self.EMMC, self.CARD, fail_mount="/data/log/forgefirm")
        self.assertFailedClosed(pr)
        self.assertEqual([m for m in pr["mounts"] if m.startswith("/run/forgefirm-")], [], pr["mounts"])

    def test_the_identity_never_replaces_what_the_card_holds(self):
        card = dict(self.CARD, **{"forgefirm/ssh/ssh_host_ed25519_key": "CARDKEY\n",
                                  "forgefirm/tls/cert.pem": "CARDCERT\n", "forgefirm/timestamp": "1\n",
                                  "forgefirm/forgefirm.conf": "cool_intake_fans = 2\nwifi_country = US\n"})
        pr = self.run_boot("root=/dev/mmcblk1p1 rw", self.EMMC, card)
        self.assertEqual(pr["state"], "card", pr["log"])
        self.assertEqual((pr["ssh_key"], pr["cert"], pr["timestamp"]), ("CARDKEY\n", "CARDCERT\n", "1\n"), pr)
        self.assertEqual(re.findall(r"(?m)^\s*wifi_country\s*=\s*(\w+)", pr["conf"]), ["US"], pr["conf"])

    def test_a_failed_card_on_an_emmc_with_no_forgefirm_folder_runs_from_ram(self):
        # Nothing on the eMMC to keep: the boot's folder is in RAM, writable,
        # so the host keys, the certificate and the log rules can be made.
        emmc = {"etc/wpa_supplicant.conf": "network={}\n"}
        pr = self.run_boot("root=/dev/mmcblk1p1 rw", emmc, {"lost+found/.keep": ""})
        self.assertEqual(pr["state"], "card-failed", pr["log"])
        self.assertEqual((pr["data_write"], pr["logs_write"]), ("ok", "ok"), pr)
        self.assertIsNone(pr["emmc_users"], "nothing lands on the eMMC")

    def test_a_requested_reset_clears_the_folder_but_what_an_install_keeps(self):
        emmc = dict(self.EMMC)
        emmc.update({"forgefirm/reset-requested": "requested\n", "forgefirm/setup.json": "{}\n",
                     "forgefirm/tokens": "t\n", "forgefirm/ext/org.example/state": "s\n",
                     "forgefirm/home/bench/.profile": "p\n", "forgefirm/.hidden": "h\n",
                     "forgefirm/timestamp": "0\n", "forgefirm/random-seed": "r\n",
                     "forgefirm/legacy-logs/forgectrl.log": "old\n"})
        pr = self.run_boot("root=/dev/mmcblk2p2 rw", emmc)
        self.assertEqual(pr["listing"], ["archive", "legacy-logs", "random-seed", "ssh", "timestamp", "tls"],
                         pr["log"])
        self.assertEqual((pr["results"], pr["system_log"]), ("{}\n", "bench work\n"), pr)

    def test_a_reset_that_could_not_remove_everything_keeps_its_request(self):
        # The package's directory cannot be removed: the rest goes, and the
        # request stays for the next boot.
        emmc = dict(self.EMMC)
        emmc.update({"forgefirm/reset-requested": "requested\n", "forgefirm/setup.json": "{}\n",
                     "forgefirm/ext/org.example/state": "s\n"})
        pr = self.run_boot("root=/dev/mmcblk2p2 rw", emmc, busy=["forgefirm/ext/org.example"])
        self.assertEqual(pr["listing"], ["archive", "ext", "reset-requested", "ssh", "timestamp", "tls"],
                         pr["log"])

    def test_a_setup_record_that_cannot_be_removed_keeps_the_account(self):
        # A record that names an account is never left without the
        # account's file, which forgectrl would read as no account at all.
        emmc = dict(self.EMMC)
        emmc.update({"forgefirm/reset-requested": "requested\n", "forgefirm/setup.json/busy/.keep": ""})
        pr = self.run_boot("root=/dev/mmcblk2p2 rw", emmc, busy=["forgefirm/setup.json/busy"])
        self.assertEqual(pr["listing"], ["archive", "reset-requested", "setup.json", "ssh", "timestamp", "tls",
                                         "users"], pr["log"])

    def test_a_failed_card_boot_runs_no_reset(self):
        # The folder shown is the eMMC's, read-only: the request waits, and
        # nothing tries the folder (a removal tried there is refused, and the
        # refusal names the file).
        emmc = dict(self.EMMC)
        emmc.update({"forgefirm/reset-requested": "requested\n", "forgefirm/setup.json": "{}\n"})
        pr = self.run_boot("root=/dev/mmcblk1p1 rw", emmc, {"lost+found/.keep": ""})
        self.assertEqual(pr["state"], "card-failed", pr["log"])
        self.assertIn("reset-requested", pr["listing"], pr["log"])
        self.assertIn("setup.json", pr["listing"], pr["log"])
        self.assertNotIn("/data/forgefirm/setup.json", pr["log"])


def pinned_forgectrl_file(path):
    """A file of forgectrl at the revision the recipe pins, from the pinned
    sources scripts/manifest-from-tree.py fetched into this checkout
    (.manifest-cache); None when they do not hold it."""
    with open(os.path.join(LAYER, "forgectrl", "forgectrl-pin.inc"), encoding="utf-8") as f:
        rev = re.search(r'^SRCREV = "([0-9a-f]{40})"$', f.read(), re.M).group(1)
    for repo in sorted(glob.glob(os.path.join(REPO, ".manifest-cache", "*"))):
        try:
            p = subprocess.run(["git", "-C", repo, "show", "%s:%s" % (rev, path)], capture_output=True)
        except OSError:
            return None
        if p.returncode == 0:
            return p.stdout.decode("utf-8", "replace")
    return None


@unittest.skipUnless(os.path.isdir(LAYER), "needs a forgefirm checkout")
class LogTreeTests(unittest.TestCase):
    """The logging tree under /data/log/forgefirm: a directory that gets a
    log and no rotation grows until /data is full."""

    TREE = r"/data/log/forgefirm/([\w.-]+)"

    def test_every_directory_that_gets_a_log_is_rotated(self):
        def text(*rel):
            with open(os.path.join(REPO, *rel), encoding="utf-8") as f:
                return f.read()
        rules = "\n".join(ln for ln in text("meta-forgefirm", "recipes-forgefirm", "forgefirm-logging", "files",
                                            "forgefirm.logrotate").splitlines() if not ln.lstrip().startswith("#"))
        rotated = set(re.findall(self.TREE + r"/\*\.log", rules))
        # The directories this repository's own files write: the default
        # rules of the logging init script, and the installer's log.
        ours = set(re.findall(self.TREE + "/", text("meta-forgefirm", "recipes-forgefirm", "forgefirm-logging",
                                                    "files", "forgefirm-logging.init")))
        ours |= set(re.findall(r'^LOG_DIR="%s"$' % self.TREE, text("scripts", "install-forgefirm.sh"), re.M))
        self.assertTrue({"system", "install"} <= ours, ours)
        self.assertEqual(ours - rotated, set(), "no rotation for a directory this repository writes")
        # forgetest's journal on the dev image, the file its init script appends to
        init = text("forgetest", "forgetest.init")
        journal = re.search(r"^DATA=(\S+)$", init, re.M).group(1) + "/" + \
            re.search(r"^LOG=\$DATA/(\S+)$", init, re.M).group(1)
        ft_rules = "\n".join(ln for ln in text("forgetest", "forgetest.logrotate").splitlines()
                             if not ln.lstrip().startswith("#"))
        self.assertTrue([g for g in re.findall(r"^(/\S+)\s*\{", ft_rules, re.M) if fnmatch.fnmatch(journal, g)],
                        "no rotation for forgetest's journal %s" % journal)
        # and the dev image's recipe fetches the rule and installs it where
        # logrotate.conf reads it
        recipe = text("meta-forgefirm", "recipes-forgefirm", "forgetest", "forgetest.bb")
        self.assertIn("file://forgetest/forgetest.logrotate", recipe)
        self.assertRegex(recipe, r"(?m)^\s*install\s+-Dm\s*0644\s+\$\{WORKDIR\}/forgetest/forgetest\.logrotate\s+"
                                 r"\$\{D\}\$\{sysconfdir\}/logrotate\.d/\S+\s*$")
        # The loggers themselves are forgectrl's list (it renders their
        # rsyslog rules and makes their directories), with the directories
        # of the tree no logger feeds, read at the pinned revision.
        logs_c = pinned_forgectrl_file("src/logs.c")
        if logs_c is None:
            why = "forgectrl at its pin is not in .manifest-cache (scripts/manifest-from-tree.py fetches it)"
            if os.environ.get("GITHUB_ACTIONS"):
                self.fail(why)
            self.skipTest(why + ": the logger list was not compared")
        loggers = set()
        for array in ("logs_names", "tree_extra_names"):
            m = re.search(r"\b%s\[\]\s*=\s*\{(.*?)\};" % array, logs_c, re.S)
            self.assertIsNotNone(m, "forgectrl's src/logs.c has no %s[]" % array)
            loggers |= set(re.findall(r'"([^"]+)"', m.group(1)))
        self.assertTrue({"forgectrl", "system"} <= loggers, loggers)
        self.assertEqual(loggers - rotated, set(), "no rotation for a directory of forgectrl's logging tree")

    @unittest.skipUnless(shutil.which("logrotate") or os.environ.get("GITHUB_ACTIONS"), "needs logrotate")
    def test_logrotate_takes_each_directory_once_on_its_own_budget(self):
        # The rules as the image installs them, run by logrotate over a
        # scratch tree: the tree's root in place of /data/log/forgefirm, no
        # signal to the host's syslog, the files this user's.
        root = tempfile.mkdtemp(prefix="forgetest-logrotate.")
        self.addCleanup(shutil.rmtree, root, ignore_errors=True)
        with open(os.path.join(LAYER, "forgefirm-logging", "files", "forgefirm.logrotate"), encoding="utf-8") as f:
            rules = f.read()
        for old, new in (("/data/log/forgefirm/", root + "/"), ("create 0640 root root", "create 0640"),
                         ("pkill -HUP rsyslogd 2>/dev/null || true", "true")):
            self.assertIn(old, rules)
            rules = rules.replace(old, new)
        conf = os.path.join(root, "rules")
        with open(conf, "w") as f:
            f.write(rules)
        dirs = set(re.findall(re.escape(root) + r"/([\w.-]+)/\*\.log", rules)) | {"sensors"}

        def log(rel, mib):
            os.makedirs(os.path.dirname(os.path.join(root, rel)), exist_ok=True)
            with open(os.path.join(root, rel), "wb") as f:
                f.truncate(mib << 20)
        for d in dirs:
            log("%s/%s.log" % (d, d), 3)
        # the sensors logger's own budget: 3 MiB stays, a 5 MiB file goes,
        # and 25 compressed ones are kept
        log("sensors/old.log", 5)
        for n in range(1, 26):
            open(os.path.join(root, "sensors", "old.log.%d.gz" % n), "wb").close()
        p = subprocess.run(["logrotate", "-s", os.path.join(root, "state"), conf],
                           capture_output=True, text=True, timeout=60)
        self.assertEqual((p.returncode, p.stderr), (0, ""), "a file two stanzas select is an error to logrotate")
        exists = lambda rel: os.path.exists(os.path.join(root, rel))                # noqa: E731
        self.assertEqual(sorted(d for d in dirs if d != "sensors" and not exists("%s/%s.log.1.gz" % (d, d))), [],
                         "past 2 MiB, every other directory's log rotates")
        self.assertEqual([exists(r) for r in ("sensors/sensors.log.1.gz", "sensors/old.log.1.gz",
                                              "sensors/old.log.25.gz", "sensors/old.log.26.gz")],
                         [False, True, True, False], os.listdir(os.path.join(root, "sensors")))

    @unittest.skipUnless(shutil.which("logrotate") or os.environ.get("GITHUB_ACTIONS"), "needs logrotate")
    def test_forgetests_journal_rotates_under_the_daemon_that_holds_it(self):
        # The dev image's rule over a scratch tree, the journal held open for
        # appending as the init script holds it: past 2 MiB it rotates, and
        # what the daemon writes after lands in the journal, not the copy.
        root = tempfile.mkdtemp(prefix="forgetest-ftlog.")
        self.addCleanup(shutil.rmtree, root, ignore_errors=True)
        with open(os.path.join(REPO, "forgetest", "forgetest.logrotate"), encoding="utf-8") as f:
            rules = f.read()
        self.assertIn("/data/forgetest/", rules)
        conf = os.path.join(root, "rules")
        with open(conf, "w") as f:
            f.write(rules.replace("/data/forgetest/", root + "/"))
        journal = os.path.join(root, "daemon.log")
        with open(journal, "ab") as daemon:
            daemon.write(b"x" * (3 << 20))
            daemon.flush()
            p = subprocess.run(["logrotate", "-s", os.path.join(root, "state"), conf],
                               capture_output=True, text=True, timeout=60)
            self.assertEqual((p.returncode, p.stderr), (0, ""))
            daemon.write(b"after the rotation\n")
            daemon.flush()
        self.assertTrue(os.path.exists(journal + ".1.gz"), os.listdir(root))
        with open(journal, "rb") as f:
            self.assertEqual(f.read(), b"after the rotation\n")


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
        for name in ("forgectrl", "forgeext", "grblhal", "gfcloud", "forgefirm-logging"):
            self.put("etc/init.d/" + name, "#!/bin/sh\n", mode=0o755)
        os.makedirs(self.path("etc/rc6.d"))
        os.makedirs(self.path("etc/rc0.d"))
        for link in ("rc5.d/S90forgectrl", "rc5.d/S91forgeext") + tuple(
                "%s/%s" % (rl, k) for rl in ("rc0.d", "rc6.d")
                for k in ("K09forgeext", "K80gfcloud", "K80grblhal", "K90forgectrl")):
            os.symlink("../init.d/" + link.split("/")[1][3:], self.path("etc/" + link))
        self.put("etc/default/rcS", "VERBOSE=no\nROOTFS_READ_ONLY=yes\n")
        self.put("etc/fstab", "/dev/root  /  auto  ro  1  0\n/dev/mmcblk2p3  /data  auto  defaults  0  0\n")
        # rcS as the images run it: forgefirm-data after mountall and
        # slotmigrate, before bootmisc.sh, which reads its timestamp from
        # /data/forgefirm through /etc/default/timestamp
        self.put("etc/default/timestamp", "TIMESTAMP_FILE=/data/forgefirm/timestamp\n")
        self.put("etc/init.d/bootmisc.sh", "#!/bin/sh\n[ -f /etc/default/timestamp ] && . /etc/default/timestamp\n",
                 mode=0o755)
        for name in ("slotmigrate", "mountall.sh", "forgefirm-data"):
            self.put("etc/init.d/" + name, "#!/bin/sh\n", mode=0o755)
        os.makedirs(self.path("etc/rcS.d"))
        for link in ("S02slotmigrate", "S03mountall.sh", "S04forgefirm-data", "S36bootmisc.sh"):
            os.symlink("../init.d/" + link[3:], self.path("etc/rcS.d/" + link))

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

    def test_the_extension_host_starting_before_forgectrl_fails(self):
        os.rename(self.path("etc/rc5.d/S91forgeext"), self.path("etc/rc5.d/S89forgeext"))
        self.assertFails("init-order")

    def test_the_cloud_controller_stopped_after_forgectrl_at_halt_fails(self):
        os.rename(self.path("etc/rc0.d/K80gfcloud"), self.path("etc/rc0.d/K95gfcloud"))
        self.assertFails("init-order")

    def test_a_runlevel_that_starts_a_controller_fails(self):
        os.symlink("../init.d/grblhal", self.path("etc/rc5.d/S92grblhal"))
        self.assertFails("init-order")

    def test_forgefirm_data_after_a_reader_of_its_folder_fails(self):
        os.rename(self.path("etc/rcS.d/S04forgefirm-data"), self.path("etc/rcS.d/S37forgefirm-data"))
        self.assertFails("data-order")

    def test_an_image_without_forgefirm_data_fails(self):
        os.remove(self.path("etc/init.d/forgefirm-data"))
        self.assertFails("data-order")

    def test_no_logging_init_script_fails(self):
        os.remove(self.path("etc/init.d/forgefirm-logging"))
        self.assertFails("logging")

    def test_a_userspace_watchdog_init_script_fails(self):
        self.put("etc/init.d/watchdog", "#!/bin/sh\n", mode=0o755)
        self.assertFails("watchdog")

    def test_rcs_without_the_read_only_rootfs_fails(self):
        self.put("etc/default/rcS", "VERBOSE=no\n")
        self.assertFails("read-only")

    def test_a_dhcpv6_client_on_the_image_fails(self):
        os.makedirs(self.path("sbin"))
        os.symlink("/bin/busybox.nosuid", self.path("sbin/udhcpc6"))
        self.assertFails("dhcpv6")

    def test_a_factory_slot_in_fstab_fails_a_release_image_only(self):
        self.put("etc/fstab", "/dev/root  /  auto  ro  1  0\n/dev/mmcblk2p1  /factory/img1  auto  ro,noatime  0  0\n")
        self.assertFails("factory", "release")
        p = self.lint("dev")
        self.assertEqual(p.returncode, 0, p.stderr)

    def test_the_dev_images_files_fail_a_release_image_only(self):
        for rel in ("etc/forgefirm-dev", "etc/init.d/forgetest", "usr/lib/python3.12/site-packages/forgetest/__init__.py"):
            with self.subTest(rel):
                self.put(rel, "")
                self.assertFails("dev-only", "release")
                p = self.lint("dev")
                self.assertEqual(p.returncode, 0, p.stderr)
                os.remove(self.path(rel))
                shutil.rmtree(self.path("usr/lib/python3.12/site-packages/forgetest"), ignore_errors=True)


def image_recipe_value(recipe, name):
    with open(os.path.join(LAYER, "images", recipe), encoding="utf-8") as f:
        return re.search(r'^%s = "(.*)"$' % name, f.read(), re.M).group(1)


@unittest.skipUnless(shutil.which("mkfs.ext4") and shutil.which("debugfs"), "needs mkfs.ext4 and debugfs")
class ImageCheckTests(unittest.TestCase):
    """image-check.py, the image's last build step, on small images made
    here as the build makes its own: the slot's raw ext4, and an SD card
    image (.wic.gz) of a rootfs partition and a seeded data partition. The
    images that hold every rule pass, and a drift fails naming its rule."""

    KIB = 4096                                      # each filesystem here, in 1 KiB blocks: room for a journal
    SLOT = ("-i", "8192", "-m", "0")                # the slot's mkfs options (EXTRA_IMAGECMD:ext4)
    ACCOUNT = "forgefirm:$6$salt$hash:1000"
    FSTAB = "/dev/root  /  auto  ro  1  0\n/dev/mmcblk2p3  /data  auto  defaults  0  0\n"

    def setUp(self):
        self.root = tempfile.mkdtemp(prefix="forgetest-imgcheck.")
        self.addCleanup(shutil.rmtree, self.root, ignore_errors=True)
        self.n = 0

    def mkfs(self, files, opts=SLOT, label="root", strangers=()):
        """An ext4 image of files {path: text, or None for a directory,
        or (text, mode)}, every entry root's but those in strangers."""
        self.n += 1
        tree = os.path.join(self.root, "tree%d" % self.n)
        img = os.path.join(self.root, "fs%d.img" % self.n)
        paths = []
        for rel, spec in sorted(files.items()):
            path = os.path.join(tree, rel)
            text, mode = spec if isinstance(spec, tuple) else (spec, None)
            if text is None:
                os.makedirs(path, exist_ok=True)
            else:
                os.makedirs(os.path.dirname(path), exist_ok=True)
                with open(path, "w") as f:
                    f.write(text)
            if mode is not None:
                os.chmod(path, mode)
            paths.append("/" + rel)
        p = subprocess.run(["mkfs.ext4", "-q", "-F", "-b", "1024", "-L", label, "-E", "root_owner=0:0", *opts,
                            "-d", tree, img, "%dk" % self.KIB], capture_output=True, text=True)
        self.assertEqual(p.returncode, 0, p.stderr)
        cmds = ["sif %s %s 0" % (path, field) for path in paths if path not in strangers for field in ("uid", "gid")]
        p = subprocess.run(["debugfs", "-w", "-f", "-", img], input="\n".join(cmds) + "\n",
                           capture_output=True, text=True)
        self.assertEqual(p.returncode, 0, p.stderr)
        return img

    @staticmethod
    def manifest(program="x", code=("/usr/bin/forgectrl",)):
        """The build section forgefirm-image-manifest.bbclass records for
        the rootfs below (the ext4's /lost+found and the manifest named)."""
        sha = lambda text: hashlib.sha256(text.encode()).hexdigest()  # noqa: E731
        return json.dumps({"build": {
            "dirs": {"/": ["boot", "etc", "lost+found", "usr"], "/boot": ["zImage"],
                     "/etc": ["forgefirm-manifest.json", "fstab"], "/usr/bin": ["forgectrl"]},
            "installed": {"forgectrl": {"/usr/bin/forgectrl": sha(program)}},
            "boot": {"/boot/zImage": sha("kernel")},
            "code": list(code)}})

    def rootfs(self, fstab=FSTAB, opts=SLOT, manifest=None, extra=None):
        files = {"etc/fstab": fstab, "usr/bin/forgectrl": ("x", 0o755), "boot/zImage": "kernel",
                 "etc/forgefirm-manifest.json": manifest or self.manifest()}
        files.update(extra or {})
        return self.mkfs(files, opts=opts)

    def seed(self, account=ACCOUNT, label="forgefirm-data", strangers=()):
        return self.mkfs({"forgefirm": (None, 0o711), "forgefirm/users": (account + "\n", 0o600),
                          "forgefirm/forgefirm.conf": ("# the card\ncool_intake_fans = 2\n", 0o600),
                          "forgefirm/setup.seed": ("", 0o600), "log": (None, 0o755), "import": ""},
                         label=label, strangers=strangers)

    def card(self, *parts, gz=False):
        """A card image (.wic, or .wic.gz as the build deploys it): an MBR
        and the partitions at 4 KiB, 4 KiB aligned."""
        self.n += 1
        path = os.path.join(self.root, "card%d.wic" % self.n) + (".gz" if gz else "")
        mbr, body, lba = bytearray(512), b"", 8
        for i, img in enumerate(parts):
            with open(img, "rb") as f:
                data = f.read()
            struct.pack_into("<B3xB3xII", mbr, 446 + 16 * i, 0x80 if i == 0 else 0, 0x83, lba, len(data) // 512)
            body += data
            lba += len(data) // 512
        mbr[510:512] = b"\x55\xaa"
        with (gzip.open(path, "wb", compresslevel=1) if gz else open(path, "wb")) as f:
            f.write(bytes(mbr) + bytes(8 * 512 - 512) + body)
        return path

    @classmethod
    def setUpClass(cls):
        # The checker run in this process (its main, with its arguments and
        # its stderr): a Python start per image would cost more than the image.
        path = os.path.join(LAYER, "images", "image-check.py")
        cls.mod = types.ModuleType("image_check")
        with open(path, encoding="utf-8") as f:
            exec(compile(f.read(), path, "exec"), cls.mod.__dict__)

    def check(self, ext4, card=None):
        args = ["--ext4", ext4, "--max-kib", str(self.KIB), "--inode-ratio", "8192", "--tmp", self.root]
        if card:
            # named as the recipe names it: the .wic, which the build compresses
            wic = card[:-len(".gz")] if card.endswith(".gz") else card
            args += ["--card", wic, "--card-rootfs-kib", str(self.KIB), "--data-label", "forgefirm-data",
                     "--data-kib", str(self.KIB), "--account", self.ACCOUNT]
        self.mod.FAILED[:] = []
        err = io.StringIO()
        with mock.patch.object(sys, "argv", ["image-check.py"] + args), contextlib.redirect_stderr(err):
            try:
                self.mod.main()
                code = None
            except SystemExit as e:
                code = e.code
        return types.SimpleNamespace(returncode=code, stderr=err.getvalue())

    def assertFails(self, rule, ext4, card=None):
        p = self.check(ext4, card)
        self.assertEqual(p.returncode, 1, p.stderr)
        self.assertIn("image-check: %s:" % rule, p.stderr)

    def test_a_slot_rootfs_passes_and_each_drift_fails_its_rule(self):
        p = self.check(self.rootfs())
        self.assertEqual(p.returncode, 0, p.stderr)
        for rule, opts in (("reserved", ("-i", "8192", "-m", "5")), ("inodes", ("-i", "4096", "-m", "0")),
                           ("ext4", self.SLOT + ("-O", "^has_journal"))):
            with self.subTest(rule):
                self.assertFails(rule, self.rootfs(opts=opts))
        with self.subTest("size"):
            big = os.path.join(self.root, "big.img")
            shutil.copy(self.rootfs(), big)
            with open(big, "r+b") as f:
                f.truncate((self.KIB + 4) << 10)
            self.assertFails("size", big)

    def test_a_rootfs_that_is_not_what_its_manifest_records_fails(self):
        # What the acceptance tool will judge the machine against, read back
        # from the image the build made: a name the listing misses (a file
        # a late build step wrote), a file whose content moved after it was
        # hashed, a program recorded from a tree other than the one imaged,
        # a manifest that records nothing to judge by.
        for what, img in (("an unlisted name", lambda: self.rootfs(extra={"etc/ld.so.cache": "cache"})),
                          ("a changed file", lambda: self.rootfs(manifest=self.manifest(program="y"))),
                          ("a program it lacks", lambda: self.rootfs(manifest=self.manifest(
                              code=("/usr/bin/forgectrl", "/usr/bin/grblHAL_glowforge")))),
                          ("no manifest", lambda: self.mkfs({"etc/fstab": self.FSTAB}))):
            with self.subTest(what):
                self.assertFails("manifest", img())

    def test_the_dev_card_passes_and_each_drift_fails_its_rule(self):
        ext4, data = self.rootfs(), self.seed()          # the raw ext4 is the card's p1 as well
        p = self.check(ext4, self.card(ext4, data, gz=True))
        self.assertEqual(p.returncode, 0, p.stderr)
        for rule, card in (("partitions", lambda: self.card(ext4)),
                           ("label", lambda: self.card(ext4, self.seed(label="data"))),
                           ("fstab", lambda: self.card(self.rootfs(self.FSTAB + "/dev/mmcblk1p2  /data/forgefirm  "
                                                                    "auto  defaults  0  0\n"), data)),
                           ("seed", lambda: self.card(ext4, self.seed(account="bench:$6$x$y:1000"))),
                           ("seed", lambda: self.card(ext4, self.seed(strangers=("/forgefirm/users",))))):
            with self.subTest(rule):
                self.assertFails(rule, ext4, card())

    @unittest.skipUnless(shutil.which("openssl"), "needs openssl")
    def test_the_cards_account_takes_the_password_forgefirm(self):
        crypted = image_recipe_value("forgefirm-image-dev.bb", "FORGEFIRM_SD_ACCOUNT_HASH")
        p = subprocess.run(["openssl", "passwd", "-6", "-salt", crypted.split("$")[2], "forgefirm"],
                           capture_output=True, text=True)
        self.assertEqual(p.stdout.strip(), crypted)


if __name__ == "__main__":
    unittest.main()
