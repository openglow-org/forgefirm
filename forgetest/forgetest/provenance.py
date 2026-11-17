# Copyright 2026 514 LLC d/b/a OpenGlow
# Written by Scott Wiederhold
# https://community.openglow.org
# SPDX-License-Identifier:    MIT

"""Whether a result was earned on the build its manifest names, as built.

A result is bound to its build through the image manifest: the
fingerprints the release gate recomputes come from the manifest's source
lists and its platform, never from the files on the machine. A result
speaks for the pins it is credited to only when all of this holds:

- the build is of its pins: no component (the dev-only ones included)
  compiled from a working tree that differs from its commit ("dirty") or
  from a local checkout ("local_source", an externalsrc build), no layer
  and no kas file that differs from its commit, and not the debug kernel
  ("kernel_debug");
- the files are what the build installed: each file a component installed
  and each file under /boot has the content the image was built with (the
  hashes in the manifest's build section), and no directory that holds
  one carries a file the image did not put there;
- the kernel is what the build made: the running kernel's release and
  build ID are the image's, every loaded module is one the image carries,
  by its build ID, and a component's module keeps the parameters it was
  loaded with;
- what runs is what the build installed: every process (an extension
  package's, under the pool accounts, aside) runs a program the image
  carries (the build section's "code": every program and shared object),
  from the read-only root and not deleted, and a component's program is
  the installed file; a shell's or Python's script is one the image
  carries; Python runs a module (-m) only of a component's, with nothing
  in an off-root working directory that it imports ahead of the image,
  and no code from its command line or its input; every executable
  mapping is code the image carries; no process carries a variable that
  changes what runs (SUBSTITUTES), or one the image sets at another value
  (IMAGE_VALUES);
- the root filesystem is mounted read-only, no mount covers what the
  build section records, and the manifest is the image's own
  (/etc/forgefirm-manifest.json).

The runner records the verdict ("provenance") on the campaign when it
opens, with the hashes of the component programs that run, and on each
result when it is written, from a check before the run and one after it.
A result whose verdict is not clean is never inherited
(campaign.compute), does not authorize, and the release gate refuses it
(artifact.verify): every release runs its campaign on the pins-only image
as built.
"""
import hashlib
import os
import re
import threading

from . import hw
from . import manifest as _manifest

# The files of one group a reason names before it counts the rest.
NAMED = 5
# The accounts extension packages run under (forgeext's pool). A package
# is data, not the image: a campaign's machine runs none of the
# operator's, and the baseline fails a run that leaves one of a test's own.
POOL_UIDS = (hw.EXT_POOL_FIRST, hw.EXT_POOL_LAST)
# Environment variables that change what runs: every override and test
# hook a component reads (its program, library, device and file
# stand-ins, its thresholds and bench knobs), the acceptance tool's own,
# and the loader's and Python's search paths. Nothing on a machine sets
# one. test_tree_manifest holds the list to the pinned sources: a variable
# a component reads is here or in HARMLESS.
SUBSTITUTES = (
    # forgectrl (src/main.c ENV_OVERRIDES, TEST_HOOKS; src/testhook.h)
    "FFLOG_CONF", "FFLOG_LEVEL", "FFLOG_SOCK", "FFLOG_STDERR",
    "FORGECTRL_CLOUD_BIN", "FORGECTRL_CONF", "FORGECTRL_CURL", "FORGECTRL_DATA_DIR",
    "FORGECTRL_EXT_HOST_TOKEN", "FORGECTRL_EXT_INDEX_URL", "FORGECTRL_EXT_STAGE", "FORGECTRL_EXT_STATUS",
    "FORGECTRL_FLAG_EVERY", "FORGECTRL_FORGEEXT", "FORGECTRL_GPU_CHECK", "FORGECTRL_GPU_PASSES",
    "FORGECTRL_GRBL_BIN", "FORGECTRL_H264_GOP", "FORGECTRL_H264_KBPS", "FORGECTRL_HOST_TEST",
    "FORGECTRL_LAMP", "FORGECTRL_LICENSES", "FORGECTRL_NEON_CHECK", "FORGECTRL_NO_CACHED_BUFS",
    "FORGECTRL_NO_GPU", "FORGECTRL_NO_H264", "FORGECTRL_NO_HW_SKIP", "FORGECTRL_NO_NEON",
    "FORGECTRL_NO_VPU", "FORGECTRL_PORT", "FORGECTRL_RELCHECK_DOWNLOAD_FMT", "FORGECTRL_RELCHECK_URL",
    "FORGECTRL_SSHD_INIT", "FORGECTRL_STREAM_FPS", "FORGECTRL_STREAM_Q", "FORGECTRL_TLS_PORT",
    "FORGECTRL_WIZ_SHOTS",
    "GFCOOL_AA_OFFSET_COUNTS", "GFCOOL_ACCEL_ABORT", "GFCOOL_ACCEL_X_ALERT", "GFCOOL_ACCEL_Y_ALERT",
    "GFCOOL_COOLDOWN_MAX_S", "GFCOOL_COOLDOWN_S", "GFCOOL_FAN_GRACE_S",
    "GFCOOL_FIRE_Q1_ALERT", "GFCOOL_FIRE_Q1_CRIT", "GFCOOL_FIRE_Q2_ALERT", "GFCOOL_FIRE_Q2_CRIT",
    "GFCOOL_INTAKE_FANS", "GFCOOL_PURGE_MIN",
    "GFCOOL_TACH_AIR_MIN", "GFCOOL_TACH_EXHAUST_MIN", "GFCOOL_TACH_INTAKE_MIN", "GFCOOL_TEC_OFF",
    "GFCOOL_TEC_ON", "GFCOOL_TEC_PRESENT", "GFCOOL_TEMP_CRITICAL", "GFCOOL_TEMP_MAX",
    "GFCOOL_TEMP_MIN", "GFCOOL_TEMP_OFFSET_C", "GFCOOL_TEMP_RESUME", "GFCOOL_TEMP_START",
    "GF_FUSE_SERIAL", "GF_GRBL_PORT", "GF_LENS_STEP_US", "GF_LOGS_EFFECTIVE",
    "GF_LOGS_ROOT", "GF_LOGS_RSYSLOG", "GF_LOGS_STAGE", "GF_PROC_STAT", "GF_PULSE_DEV",
    "GF_SWITCH_WORD_FILE", "GF_SYSFS_DIR",
    "GF_DATA_STATE", "GF_DEV_MARKER", "GF_LED_ROOT", "GF_REBOOT_FILE", "GF_RUN_DIR", "GF_SLOT_LIST",
    "GF_SWITCH_DEV", "GF_SYSFS_ROOT",
    # the GRBL controller (src/main.c ENV_OVERRIDES, TEST_HOOKS): its stream's
    # tick, depth and lead, the bench's sweep knobs
    "GFHOME_CONF", "GFSINK_ATTR_LOG", "GFSINK_DEPTH_MS", "GFSINK_DUMP", "GFSINK_LATCH_LOG",
    "GFSINK_LEAD_MS", "GFSINK_RATE", "GFSINK_STALL_MS", "GFSINK_WRITE_STALL_MS",
    "GF_STATE_DIR", "GF_SWITCH_FILE", "GF_VERDICT_FILE",
    # the extension host (src/run.c ENV_OVERRIDES)
    "FORGEEXT_HOST_TOKEN_FILE",
    # the cloud client and the hardware library
    "GFHOME_DEADLINE", "GFHOME_PARK_HALF_STEPS", "GFHOME_TIMEOUT_S", "REMOTE_DEBUG",
    # the acceptance tool itself (hw.py and the suite): the judge's readers
    # pointed elsewhere
    "FORGECTRL_TOKEN_FILE", "FORGECTRL_URL", "GF_IIO_ROOT", "GF_LEDS_ROOT", "GRBL_HOST", "GRBL_PORT",
    "FORGETEST_ANCHOR", "FORGETEST_BENCH_DIR", "FORGETEST_BOOT_ID", "FORGETEST_DATA",
    "FORGETEST_FIXTURE_CONFIG", "FORGETEST_HOST_RUN", "FORGETEST_HTTP_LOG", "FORGETEST_INITD",
    "FORGETEST_MANIFEST",
    "FORGETEST_MARKER", "FORGETEST_NOHUNT_MARKER", "FORGETEST_OFFLINE_MARKER", "FORGETEST_OFFLINE_SOCKET",
    "FORGETEST_PROC_ROOT",
    # the loader and Python: their search paths, Python's bytecode and
    # library directories, and its prompt after the script
    "LD_PRELOAD", "LD_LIBRARY_PATH", "LD_AUDIT",
    "PYTHONPATH", "PYTHONHOME", "PYTHONSTARTUP", "PYTHONUSERBASE",
    "PYTHONPYCACHEPREFIX", "PYTHONPLATLIBDIR", "PYTHONINSPECT",
)
# The variables a component reads that substitute nothing: what forgectrl's
# supervisor sets for the controller it starts (the pulse device's
# descriptor, the cooling report's secret, the sink), the acceptance
# page's listener, and an extension package's own variables, which its
# host sets for a service that runs under the pool accounts.
HARMLESS = ("GF_PULSE_FD", "GF_REPORT_SECRET", "GFSINK", "FORGETEST_PORT", "FORGETEST_HOST",
            "FFX_API", "FFX_DATA", "FFX_PKG")
# The HARMLESS variables that pass only at the value the image gives them:
# forgectrl's supervisor points the GRBL controller's sink at the pulse
# device (forgectrl src/super.c PULSE_DEV); any other value is a file, the
# null sink.
IMAGE_VALUES = {"GFSINK": "/dev/glowforge"}
# A shell runs a script named as its first operand (busybox runs the applet
# its first argument names).
SHELLS = ("sh", "ash", "bash", "dash")
# Python's files that it imports: a source, a bytecode file or an extension
# module, and a package's __init__.
_MODULE = re.compile(r"^([^\W\d]\w*)(?:\.pyc?|(?:\.abi3|\.cpython-[^.]+)?\.so)$")
_INIT = re.compile(r"^__init__(?:\.pyc?|(?:\.abi3|\.cpython-[^.]+)?\.so)$")
_IDENT = re.compile(r"^[^\W\d]\w*$")


def manifest_reasons(manifest):
    """Why the manifest says its build was not of its pins ([] when it was)."""
    why = []
    for name in sorted(manifest.components):
        c = manifest.components.get(name) or {}
        if c.get("dirty"):
            why.append("%s was built from a working tree that differs from its commit" % name)
        if c.get("local_source"):
            why.append("%s was built from a local checkout, not from its pin" % name)
    build = manifest.data.get("build") or {}
    layers = build.get("layers")
    if not layers:
        why.append("the manifest records no layer state")
    for name in sorted(layers or {}):
        st = layers[name] or {}
        if st.get("rev") is None:
            why.append("layer %s is not a git checkout" % name)
        elif st.get("dirty") is not False:
            why.append("layer %s differs from its commit" % name)
    kas = build.get("kas")
    if kas is None:
        why.append("the manifest records no kas state")
    elif kas.get("dirty") is not False:
        why.append("the kas configuration differs from its commit")
    if manifest.platform.get("kernel_debug"):
        why.append("the image runs the debug kernel")
    return why


def clean(verdict):
    """Whether a recorded verdict (or a record that carries one) says the
    result was earned on its pins as built: an object whose "clean" is
    true. Anything else - absent, another type, another value - is no."""
    if isinstance(verdict, dict) and "provenance" in verdict:
        verdict = verdict.get("provenance")
    return isinstance(verdict, dict) and verdict.get("clean") is True


def merge(*checks):
    """One verdict from the checks of one run: clean only when each was."""
    why, ran = [], {}
    for c in checks:
        if not c:
            why.append("no provenance check")
            continue
        for w in c.get("why") or ([] if c.get("clean") else ["not clean"]):
            if w not in why:
                why.append(w)
        ran.update(c.get("ran") or {})
    return {"clean": not why, "why": why, "ran": ran}


def note_build_id(data):
    """The GNU build ID (hex) in ELF note bytes - a .ko file, a module's
    notes/.note.gnu.build-id, /sys/kernel/notes - or None. The note is
    namesz 4, descsz, type 3 (NT_GNU_BUILD_ID), "GNU" and a NUL, then the ID;
    little-endian, as the machine is."""
    i = data.find(b"GNU\x00")
    while i != -1:
        if i >= 12 and data[i - 12:i - 8] == b"\x04\x00\x00\x00" and data[i - 4:i] == b"\x03\x00\x00\x00":
            n = int.from_bytes(data[i - 8:i - 4], "little")
            if 0 < n <= 64 and len(data) >= i + 4 + n:
                return data[i + 4:i + 4 + n].hex()
        i = data.find(b"GNU\x00", i + 1)
    return None


def _unescape(field):
    return re.sub(r"\\([0-7]{3})", lambda m: chr(int(m.group(1), 8)), field)


class Provenance:
    """The check, on the machine whose filesystem is under `root`."""

    def __init__(self, manifest, root="/"):
        self.manifest = manifest
        self.root = root.rstrip("/") or "/"
        self._lock = threading.Lock()
        self._hashes = {}         # (st_dev, st_ino, size, mtime_ns, ctime_ns) -> sha256
        self._params = {}         # module -> its parameters as this check first read them
        self._static = manifest_reasons(manifest)
        own = self._path(_manifest.DEFAULT_PATH)
        if manifest.path is None or os.path.realpath(manifest.path) != os.path.realpath(own):
            self._static.append("the manifest is not the image's own (%s)"
                                % (manifest.path or "not read from a file"))

    def _path(self, path):
        return os.path.join(self.root, path.lstrip("/"))

    def _rel(self, path):
        """A path on this machine as the machine names it (the inverse of
        _path for the test trees; the identity on a machine)."""
        if self.root != "/" and (path == self.root or path.startswith(self.root + "/")):
            return path[len(self.root):] or "/"
        return path

    def _resolve(self, path, hops=0):
        """A path on this machine with its links resolved as the machine
        resolves them: an absolute link target names a path on this
        machine (under `root`), not on the host that runs the check."""
        parts = [p for p in path.split("/") if p not in ("", ".")]
        done = "/"
        for i, part in enumerate(parts):
            if part == "..":
                done = os.path.dirname(done.rstrip("/")) or "/"
                continue
            nxt = os.path.join(done, part)
            host = self._path(nxt)
            if hops < 40 and os.path.islink(host):
                target = os.readlink(host)
                base = target if target.startswith("/") else os.path.join(done, target)
                return self._resolve("/".join([base] + parts[i + 1:]), hops + 1)
            done = nxt
        return done

    def _read(self, path, binary=False):
        with open(self._path(path), "rb") as f:
            data = f.read()
        return data if binary else data.decode("utf-8", "replace")

    def _sha(self, path):
        """sha256 of a file, read again only when its inode or times change."""
        st = os.stat(path)
        key = (st.st_dev, st.st_ino, st.st_size, st.st_mtime_ns, st.st_ctime_ns)
        hit = self._hashes.get(key)
        if hit is None:
            h = hashlib.sha256()
            with open(path, "rb") as f:
                for chunk in iter(lambda: f.read(1 << 20), b""):
                    h.update(chunk)
            hit = self._hashes[key] = h.hexdigest()
        return hit

    def check(self):
        """{"clean", "why": [reason], "ran": {program: sha256}}. Never
        raises: a check that cannot finish is not a clean one."""
        with self._lock:
            try:
                return self._check()
            except Exception as e:  # noqa: BLE001
                return {"clean": False, "why": ["the provenance check failed: %s: %s" % (type(e).__name__, e)],
                        "ran": {}}

    def _check(self):
        why = list(self._static)
        build = self.manifest.data.get("build") or {}
        installed = build.get("installed") or {}
        if not installed:
            why.append("the manifest records no installed-file hashes")
        groups = sorted(installed.items())
        if build.get("boot"):
            groups.append(("boot", build["boot"]))
        else:
            why.append("the manifest records no boot files")
        for group, files in groups:
            bad = []
            for path in sorted(files):
                try:
                    same = self._sha(self._path(path)) == files[path]
                except OSError:
                    same = False
                if not same:
                    bad.append(path)
            if bad:
                more = " and %d more" % (len(bad) - NAMED) if len(bad) > NAMED else ""
                why.append("%s: %s%s not as built" % (group, ", ".join(bad[:NAMED]), more))
        self._directories(build.get("dirs"), why)
        mounts = self._mounts(why)
        self._kernel(build.get("kernel"), installed, why)
        code = build.get("code")
        if code is None:
            why.append("the manifest records no programs")
        # a component's file is the image's, whatever its mode (a script run
        # by its interpreter needs no execute bit)
        code = set(code or ()) | set(p for files in installed.values() for p in files)
        ran = {}
        if mounts is not None:
            self._covered(mounts, code | set(build.get("boot") or ()) | set(build.get("dirs") or ())
                          | {_manifest.DEFAULT_PATH}, why)
            ran = self._processes(installed, code, mounts, why)
        return {"clean": not why, "why": why, "ran": ran}

    # -- the files ---------------------------------------------------------
    def _directories(self, dirs, why):
        """No directory that holds an installed file carries a file the
        image did not put there (a .pth, a sitecustomize.py, a bytecode
        file beside a source, an /etc/ld.so.preload)."""
        if not dirs:
            why.append("the manifest records no directory listings")
            return
        for d in sorted(dirs):
            try:
                extra = sorted(set(os.listdir(self._path(d))) - set(dirs[d]))
            except OSError:
                extra = ["(the directory cannot be read)"]
            if extra:
                why.append("%s holds what the image did not install: %s" % (d, ", ".join(extra[:NAMED])))

    def _mounts(self, why):
        """The mount points, longest first, or None when they cannot be
        read; a reason when the root is not mounted read-only."""
        try:
            text = self._read("/proc/mounts")
        except OSError:
            why.append("the mounts cannot be read")
            return None
        points, root_opts = set(), None
        for line in text.splitlines():
            parts = line.split()
            if len(parts) >= 4:
                mp = _unescape(parts[1])
                points.add(mp)
                if mp == "/":
                    root_opts = parts[3].split(",")
        if root_opts is None or "ro" not in root_opts:
            why.append("the root filesystem is not mounted read-only")
        return sorted(points, key=len, reverse=True)

    @staticmethod
    def _covered(mounts, recorded, why):
        """No mount covers a path the build section records (a bind mount
        over the manifest, an installed or boot file, a program or a listed
        directory, or a mount above one): what the check would read there is
        not the root's. The image's own mounts (/data, /run, /var/lib, the
        account files in /etc) cover none."""
        for mp in mounts:
            if mp == "/":
                continue
            pre = mp.rstrip("/") + "/"
            hit = sorted(p for p in recorded if p == mp or p.startswith(pre))
            if hit:
                more = " and %d more" % (len(hit) - NAMED) if len(hit) > NAMED else ""
                why.append("a mount at %s covers %s%s" % (mp, ", ".join(hit[:NAMED]), more))

    @staticmethod
    def _on_root(path, mounts):
        """Whether a path (as the machine names it) is on the root
        filesystem: no other mount covers it."""
        for mp in mounts:
            if mp != "/" and (path == mp or path.startswith(mp.rstrip("/") + "/")):
                return False
        return True

    # -- the kernel --------------------------------------------------------
    def _kernel(self, kern, installed, why):
        if not kern:
            why.append("the manifest records no kernel identity")
            return
        ours = set(os.path.basename(p)[:-len(".ko")].replace("-", "_")
                   for files in installed.values() for p in files if p.endswith(".ko"))
        try:
            release = self._read("/proc/sys/kernel/osrelease").strip()
        except OSError:
            release = None
        if release not in (kern.get("release") or []):
            why.append("the running kernel (%s) is not the image's" % release)
        try:
            bid = note_build_id(self._read("/sys/kernel/notes", binary=True))
        except OSError:
            bid = None
        if not kern.get("build_id") or bid != kern.get("build_id"):
            why.append("the running kernel's build ID is not the image's")
        known = kern.get("modules") or {}
        try:
            loaded = [ln.split()[0] for ln in self._read("/proc/modules").splitlines() if ln.split()]
        except OSError:
            why.append("the loaded modules cannot be read")
            return
        for name in sorted(loaded):
            if name not in known:
                why.append("module %s is loaded, and the image carries no such module" % name)
                continue
            try:
                got = note_build_id(self._read("/sys/module/%s/notes/.note.gnu.build-id" % name, binary=True))
            except OSError:
                got = None
            if got is None or got != known[name]:
                why.append("module %s as loaded is not the image's" % name)
            if name in ours:
                self._parameters(name, why)

    def _parameters(self, name, why):
        """A component's module keeps the parameters this check first read
        (the image loads it at boot, before the acceptance tool starts): a
        reload with others, or a write to a writable one, is not the image
        as built."""
        d = "/sys/module/%s/parameters" % name
        try:
            now = {p: self._read("%s/%s" % (d, p)).strip() for p in sorted(os.listdir(self._path(d)))}
        except OSError:
            now = None
        first = self._params.setdefault(name, now)
        if now != first:
            why.append("module %s's parameters are not those it was loaded with" % name)

    # -- the processes -----------------------------------------------------
    def _processes(self, installed, code, mounts, why):
        """{program: sha256} of the component programs that run, and a
        reason for every process that runs anything but the image: `code`
        is every program and shared object the image carries."""
        by_path = {path: (comp, sha) for comp in installed for path, sha in installed[comp].items()}
        ran = {}
        proc = self._path("/proc")
        try:
            pids = sorted((p for p in os.listdir(proc) if p.isdigit()), key=int)
        except OSError:
            why.append("the processes cannot be read")
            return ran
        for pid in pids:
            base = os.path.join(proc, pid)
            exe = os.path.join(base, "exe")
            # A process that execs while it is read (a child of forgectrl's
            # on its way to the controller) is read again, as what it became.
            for _attempt in range(2):
                try:
                    target = os.readlink(exe)
                except OSError:
                    break       # a kernel thread, or a process that ended
                if self._pool(pid):
                    break
                w, r = [], {}
                try:
                    self._program(pid, base, target, by_path, code, mounts, r, w)
                    self._environment(pid, w)
                    self._script(pid, base, target, installed, code, mounts, w)
                    self._mappings(pid, code, mounts, w)
                    same = os.readlink(exe) == target
                except OSError:
                    break       # it ended while it was read
                if same:
                    why.extend(w)
                    ran.update(r)
                    break
            else:
                why.append("process %s changed its program while it was read" % pid)
        return ran

    def _pool(self, pid):
        """Whether a process runs under an extension package's pool account."""
        try:
            uid = int(next((ln for ln in self._read("/proc/%s/status" % pid).splitlines()
                            if ln.startswith("Uid:"))).split()[1])
        except (OSError, StopIteration, IndexError, ValueError):
            return False
        return POOL_UIDS[0] <= uid <= POOL_UIDS[1]

    def _program(self, pid, base, target, by_path, code, mounts, ran, why):
        # a program deleted while it runs ("<path> (deleted)") is no file
        # the image carries
        path = self._rel(target)
        if not self._on_root(path, mounts):
            why.append("process %s runs %s, which is not on the read-only root" % (pid, path))
            return
        if path not in code:
            why.append("process %s runs %s, a program the image does not carry" % (pid, path))
            return
        hit = by_path.get(path)
        if hit is None:
            return
        comp, want = hit
        got = self._sha(os.path.join(base, "exe"))
        ran[path] = got
        if got != want:
            why.append("%s: process %s runs %s, not the program the image was built with" % (comp, pid, path))

    def _environment(self, pid, why):
        env = [e.split(b"=", 1) for e in self._read("/proc/%s/environ" % pid, binary=True).split(b"\0")
               if b"=" in e]
        env = [(k.decode("utf-8", "replace"), v.decode("utf-8", "replace")) for k, v in env]
        names = sorted(set(k for k, _ in env) & set(SUBSTITUTES))
        names += sorted(set("%s=%s" % (k, v) for k, v in env if k in IMAGE_VALUES and v != IMAGE_VALUES[k]))
        if names:
            why.append("process %s runs with %s set" % (pid, ", ".join(names)))

    def _script(self, pid, base, target, installed, code, mounts, why):
        """What an interpreter runs beyond its program. A shell's script and
        a Python script must be programs the image carries, on the
        read-only root (a component's file counts, whatever its mode).
        Python runs a module (-m) only of a component's (_module), and no
        code from its command line (-c) or its input (no script, `-`, -i):
        nothing the image starts runs one. A shell's command line (-c) and
        its input (no script, -s) are not judged: the image's own wrappers
        run as `sh -c`, and an operator's login shell reads its input. What
        either starts is a process of its own, checked as one."""
        argv = [a.decode("utf-8", "replace") for a in self._read("/proc/%s/cmdline" % pid, binary=True).split(b"\0")]
        while argv and argv[-1] == "":
            argv.pop()
        if not argv:
            return
        exe = os.path.basename(self._rel(target))
        if exe.startswith("busybox"):
            # busybox runs the applet argv[0] names (a login shell's with a
            # leading "-"), or, called as busybox, the one argv[1] names
            if os.path.basename(argv[0]).startswith("busybox"):
                argv = argv[1:]
            applet = os.path.basename(argv[0]) if argv else ""
            if applet.startswith("-"):
                applet = applet[1:]
            kind = "shell" if applet in SHELLS else None
        elif exe.split(".")[0] in SHELLS:
            kind = "shell"
        elif exe.startswith("python"):
            kind = "python"
        else:
            kind = None
        if kind is None:
            return
        try:
            cwd = self._rel(os.readlink(os.path.join(base, "cwd")))
        except OSError:
            cwd = "/"
        if kind == "shell":
            script = self._shell_script(argv[1:])
        else:
            script = self._python_script(pid, argv[1:], cwd, installed, mounts, why)
        if script is None:
            return
        real = self._resolve(script if script.startswith("/") else os.path.join(cwd, script))
        if not self._on_root(real, mounts):
            why.append("process %s runs %s, which is not on the read-only root" % (pid, real))
        elif real not in code:
            why.append("process %s runs %s, a script the image does not carry" % (pid, real))

    @staticmethod
    def _shell_script(args):
        """A shell's script operand, or None when it runs its command line
        or its input. -o and +o (and bash's -O) take the next word as their
        value, in a cluster too (-eo pipefail)."""
        i = 0
        while i < len(args):
            a = args[i]
            if a in ("-", "--"):
                i += 1
                break
            if len(a) < 2 or a[0] not in "-+":
                break
            if a.startswith("--"):
                i += 2 if a in ("--rcfile", "--init-file") else 1
                continue
            if a[0] == "-" and ("c" in a or "s" in a):
                return None
            i += 1 + a.count("o") + a.count("O")
        return args[i] if i < len(args) else None

    def _python_script(self, pid, args, cwd, installed, mounts, why):
        """Python's script operand, or None when Python runs a module (judged
        by _module) or code from its command line or its input (a reason).
        Options cluster (-uBm); -W and -X take the rest of the word or the
        next one as their value; -X pycache_prefix reads bytecode from
        elsewhere."""
        i, prompt = 0, False
        while i < len(args):
            a = args[i]
            if a == "--":
                i += 1
                break
            if a == "-" or not a.startswith("-"):
                break
            i += 1
            if a.startswith("--"):
                if a == "--check-hash-based-pycs":
                    i += 1
                continue
            for j in range(1, len(a)):
                ch, rest = a[j], a[j + 1:]
                if ch in "cmWX":
                    value = rest
                    if not rest and i < len(args):
                        value = args[i]
                        i += 1
                if ch == "c":
                    why.append("process %s runs Python code from its command line" % pid)
                    return None
                if ch == "m":
                    if prompt:
                        why.append("process %s runs Python code from its input" % pid)
                    self._module(pid, value, cwd, installed, mounts, why)
                    return None
                if ch == "X" and value.split("=")[0] == "pycache_prefix":
                    why.append("process %s runs Python with -X %s" % (pid, value))
                if ch in "WX":
                    break
                prompt = prompt or ch == "i"
        if prompt or i >= len(args) or args[i] == "-":
            why.append("process %s runs Python code from its input" % pid)
            return None
        return args[i]

    @staticmethod
    def _component_module(installed, top):
        """Whether a component installed the module or package `top` in a
        site-packages directory: None when none did, else whether it is a
        regular one (a module, or a package with an __init__), which a
        directory of the same name earlier on the path does not shadow."""
        found = None
        for files in installed.values():
            for p in files:
                _, sep, rest = p.partition("/site-packages/")
                if not sep:
                    continue
                parts = rest.split("/")
                if len(parts) == 1:
                    m = _MODULE.match(parts[0])
                    if m and m.group(1) == top:
                        return True
                elif parts[0] == top:
                    found = bool(found) or (len(parts) == 2 and _INIT.match(parts[1]) is not None)
        return found

    def _importable(self, d):
        """The names Python imports from directory d (as the machine names
        it): a source, bytecode or extension module, or a package with an
        __init__. A bare directory (a namespace portion) gives way to a
        regular module anywhere later on the path."""
        host = self._path(d)
        try:
            names = os.listdir(host)
        except OSError:
            return set()
        out = set()
        for n in names:
            m = _MODULE.match(n)
            if m:
                out.add(m.group(1))
                continue
            if not _IDENT.match(n):
                continue
            try:
                inner = os.listdir(os.path.join(host, n))
            except OSError:
                continue
            if any(_INIT.match(x) for x in inner):
                out.add(n)
        return out

    def _module(self, pid, mod, cwd, installed, mounts, why):
        """Python's -m: a module a component installed, and an off-root
        working directory (the first place Python looks, for this module
        and for every module it imports) that holds nothing Python imports
        ahead of the image. A bare directory of the module's name (forgetest's
        /data/forgetest) gives way to the component's regular package."""
        top = mod.split(".")[0]
        regular = self._component_module(installed, top)
        if regular is None:
            why.append("process %s runs the module %s, which no component installed" % (pid, mod or "(none)"))
            return
        if self._on_root(cwd, mounts):
            return
        names = self._importable(cwd)
        if not regular and os.path.isdir(self._path(os.path.join(cwd, top))):
            names.add(top)
        if names:
            why.append("process %s runs Python in %s, which holds %s, imported ahead of the image"
                       % (pid, cwd, ", ".join(sorted(names)[:NAMED])))

    def _mappings(self, pid, code, mounts, why):
        """No executable mapping of a file off the root, or of one the image
        does not carry (a file replaced while it is mapped reads as
        "<path> (deleted)", which the image does not carry)."""
        for line in self._read("/proc/%s/maps" % pid).splitlines():
            parts = line.split(None, 5)
            if len(parts) < 6 or "x" not in parts[1] or not parts[5].startswith("/"):
                continue
            path = self._rel(parts[5])
            if not self._on_root(path, mounts):
                why.append("process %s maps %s, which is not on the read-only root" % (pid, path))
                return
            if path not in code:
                why.append("process %s maps %s, code the image does not carry" % (pid, path))
                return
