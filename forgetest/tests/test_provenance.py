# Copyright 2026 514 LLC d/b/a OpenGlow
# Written by Scott Wiederhold
# https://community.openglow.org
# SPDX-License-Identifier:    MIT

"""provenance.py - whether a result is earned on its pins as built: the
manifest's own account of its build; the installed files, the boot files
and their directories against the build's record; the running kernel and
modules; every process's program, script, environment and executable
mappings; the root filesystem's mount; and the runner recording the
verdict on the campaign and on each result. Run on a scratch machine tree
(etc/, usr/, boot/, proc/, sys/) under a temporary root."""
import copy
import hashlib
import json
import os
import shutil
import struct
import tempfile
import unittest

import helpers
from forgetest import manifest as manifest_mod
from forgetest import provenance
from forgetest.log import Log
from forgetest.runner import Run, Runner

PROGRAM = b"forgectrl as built\n"
GRBL = b"the controller as built\n"
MODULE = b"x = 1\n"
ZIMAGE = b"zImage as built\n"
RELEASE = "6.12.20-fslc+g0e01ec9f0d3f"
KERNEL_ID = "a1" * 20
MODULE_ID = "b2" * 20
SITE = "usr/lib/python3/site-packages/gfhardware"
KO = "lib/modules/%s/extras/glowforge.ko" % RELEASE
SUITE = "usr/lib/python3/site-packages/forgetest"
MOUNTS = ("/dev/root / ext4 ro,relatime 0 0\nproc /proc proc rw 0 0\n"
          "/dev/mmcblk1p2 /data ext4 rw,noatime 0 0\ntmpfs /tmp tmpfs rw 0 0\n")


def sha(data):
    return hashlib.sha256(data).hexdigest()


def note(build_id, name=b"GNU\x00", kind=3):
    desc = bytes.fromhex(build_id)
    return struct.pack("<III", len(name), len(desc), kind) + name + desc


class ProvenanceTests(unittest.TestCase):
    def setUp(self):
        self.root = tempfile.mkdtemp(prefix="forgetest-prov-")
        self.put("usr/bin/forgectrl", PROGRAM)
        self.put("usr/bin/grblHAL_glowforge", GRBL)
        self.put("usr/bin/python3.12", b"python")
        self.put("bin/busybox", b"busybox")
        self.put(SITE + "/x.py", MODULE)
        self.put(SUITE + "/__init__.py", b"")
        self.put("boot/zImage", ZIMAGE)
        self.put("proc/mounts", MOUNTS.encode())
        self.put("proc/sys/kernel/osrelease", (RELEASE + "\n").encode())
        # the kernel's notes: its own "Linux" note, then the build ID
        self.put("sys/kernel/notes", note("00000006", name=b"Linu", kind=6) + note(KERNEL_ID))
        self.put("proc/modules", b"glowforge 16384 0 - Live 0xbf000000 (O)\n")
        self.put("sys/module/glowforge/notes/.note.gnu.build-id", note(MODULE_ID))
        self.put("sys/module/glowforge/parameters/heater_lease_ms", b"5000\n")
        self.put(KO, b"\x7fELF" + note(MODULE_ID))
        self.process(100, "usr/bin/forgectrl", ["/usr/bin/forgectrl"])
        comps = {"forgectrl": {"srcrev": "a" * 40, "source": "git://example.org/forgectrl.git",
                               "files": [["src/main.c", "1" * 40]]},
                 "forgetest": {"srcrev": None, "source": "files", "files": [["runner.py", "2" * 40]]}}
        plat = {"machine": "glowforge", "layers": {"meta-forgefirm": {"content_sha256": "2" * 64}},
                "kas": {"content_sha256": "3" * 64}}
        self.data = {"format": 1, "image": {"name": "forgefirm-image-dev", "version": "dev"},
                     "components": comps, "platform": plat,
                     "build": {"layers": {"meta-forgefirm": {"rev": "4" * 40, "dirty": False},
                                          "poky": {"rev": "5" * 40, "dirty": False}},
                               "kas": {"dirty": False},
                               "installed": {"forgectrl": {"/usr/bin/forgectrl": sha(PROGRAM)},
                                             "grblhal-glowforge": {"/usr/bin/grblHAL_glowforge": sha(GRBL)},
                                             "python3-gfhardware": {"/%s/x.py" % SITE: sha(MODULE)},
                                             "forgetest": {"/%s/__init__.py" % SUITE: sha(b"")},
                                             "kernel-module-glowforge": {"/" + KO: sha(b"\x7fELF" + note(MODULE_ID))}},
                               "boot": {"/boot/zImage": sha(ZIMAGE)},
                               "dirs": {"/usr/bin": ["forgectrl", "grblHAL_glowforge", "python3.12"],
                                        "/" + SITE: ["x.py"], "/boot": ["zImage"]},
                               "code": ["/bin/busybox", "/usr/bin/forgectrl", "/usr/bin/grblHAL_glowforge",
                                        "/usr/bin/python3.12"],
                               "kernel": {"release": [RELEASE], "build_id": KERNEL_ID,
                                          "modules": {"glowforge": MODULE_ID, "ov5648": "c3" * 20}}},
                     "content_sha256": "6" * 64}

    def tearDown(self):
        shutil.rmtree(self.root, ignore_errors=True)

    def path(self, rel):
        return os.path.join(self.root, rel)

    def put(self, rel, data):
        os.makedirs(os.path.dirname(self.path(rel)), exist_ok=True)
        with open(self.path(rel), "wb") as f:
            f.write(data)

    def process(self, pid, exe, argv, env=b"PATH=/usr/bin:/bin\0", uid=0, cwd="", maps=None):
        """A process in the scratch /proc: its program, command line,
        environment, owner, working directory and executable mappings."""
        d = self.path("proc/%d" % pid)
        os.makedirs(d)
        os.symlink(self.path(exe), os.path.join(d, "exe"))
        os.symlink(self.path(cwd), os.path.join(d, "cwd"))
        self.put("proc/%d/cmdline" % pid, b"\0".join(a.encode() for a in argv) + b"\0")
        self.put("proc/%d/environ" % pid, env)
        self.put("proc/%d/status" % pid, ("Name:\tx\nUid:\t%d\t%d\t%d\t%d\n" % ((uid,) * 4)).encode())
        lines = ["00010000-00020000 r-xp 00000000 b3:01 1234 %s" % self.path(m) for m in (maps or [exe])]
        self.put("proc/%d/maps" % pid, ("\n".join(lines) + "\n").encode())

    def end(self, pid):
        shutil.rmtree(self.path("proc/%d" % pid))

    def prov(self, data=None, where="etc/forgefirm-manifest.json"):
        self.put(where, json.dumps(data or self.data).encode())
        return provenance.Provenance(manifest_mod.Manifest.load(self.path(where)), root=self.root)

    def clean(self, prov=None):
        return (prov or self.prov()).check()["clean"]


    def test_a_machine_running_its_pins_as_built_is_clean(self):
        v = self.prov().check()
        self.assertEqual(v["why"], [])
        self.assertTrue(v["clean"])
        self.assertEqual(v["ran"], {"/usr/bin/forgectrl": sha(PROGRAM)})

    def test_a_manifest_that_does_not_vouch_for_its_pins_is_not_clean(self):
        def change(fn):
            data = copy.deepcopy(self.data)
            fn(data)
            return data
        cases = {
            "a component from a dirty tree": change(lambda m: m["components"]["forgectrl"].update(dirty=True)),
            "the acceptance tool from a dirty tree":
                change(lambda m: m["components"]["forgetest"].update(dirty=True)),
            "a component from a local checkout":
                change(lambda m: m["components"]["forgectrl"].update(local_source=True)),
            "a dirty layer": change(lambda m: m["build"]["layers"]["poky"].update(dirty=True)),
            "a layer that is no checkout": change(lambda m: m["build"]["layers"]["poky"].update(rev=None)),
            "a dirty kas configuration": change(lambda m: m["build"]["kas"].update(dirty=True)),
            "the debug kernel": change(lambda m: m["platform"].update(kernel_debug=True)),
        }
        for gone in ("layers", "installed", "boot", "dirs", "code", "kernel"):
            cases["no %s record" % gone] = change(lambda m, g=gone: m["build"].pop(g))
        for what, data in cases.items():
            self.assertFalse(self.clean(self.prov(data)), what)
        # the image's manifest, read from another file (FORGETEST_MANIFEST)
        self.assertFalse(self.clean(self.prov(where="tmp/manifest.json")))

    def test_an_installed_or_boot_file_not_as_built_is_not_clean(self):
        prov = self.prov()
        self.assertTrue(self.clean(prov))
        module = SITE + "/x.py"
        # a hot copy of an installed module (no process runs it as a program)
        self.put(module, b"x = 2  # from the bench\n")
        self.assertFalse(self.clean(prov))
        self.put(module, MODULE)
        self.assertTrue(self.clean(prov))
        # the same size, edited in place after a check: its times moved
        st = os.stat(self.path(module))
        self.put(module, MODULE.replace(b"1", b"9"))
        os.utime(self.path(module), ns=(st.st_atime_ns, st.st_mtime_ns + 10 ** 9))
        self.assertFalse(self.clean(prov))
        self.put(module, MODULE)
        # removed
        os.remove(self.path(module))
        self.assertFalse(self.clean(prov))
        self.put(module, MODULE)
        # another kernel copied into /boot
        self.put("boot/zImage", b"zImage from the bench\n")
        self.assertFalse(self.clean(prov))

    def test_a_file_beside_the_installed_ones_is_not_clean(self):
        # a .pth in a directory the image installed into
        self.put(SITE + "/bench.pth", b"/data/bench\n")
        self.assertFalse(self.clean())
        os.remove(self.path(SITE + "/bench.pth"))
        self.assertTrue(self.clean())
        # a listed directory that cannot be read
        data = copy.deepcopy(self.data)
        data["build"]["dirs"]["/usr/share/forgefirm"] = ["licenses.tar.gz"]
        self.assertFalse(self.clean(self.prov(data)))

    def test_a_process_running_other_than_the_image_is_not_clean(self):
        prov = self.prov()
        self.put("data/grbl-dev", b"a controller built on the bench\n")
        # from /data, under a name no component has (its mappings are the
        # mapping rule's; this is the program's)
        self.process(101, "data/grbl-dev", ["/data/grbl-dev"], maps=["usr/bin/forgectrl"])
        self.assertFalse(self.clean(prov))
        self.end(101)
        # a program replaced while it ran
        self.process(102, "usr/bin/forgectrl", ["/usr/bin/forgectrl"])
        os.remove(self.path("proc/102/exe"))
        os.symlink(self.path("usr/bin/forgectrl") + " (deleted)", self.path("proc/102/exe"))
        self.assertFalse(self.clean(prov))
        self.end(102)
        # a program on the root that the image does not carry
        self.put("usr/lib/grbl-dev", b"a controller built on the bench\n")
        self.process(103, "usr/lib/grbl-dev", ["grblHAL_glowforge"], maps=["usr/bin/forgectrl"])
        self.assertFalse(self.clean(prov))
        self.end(103)
        # an extension package's service runs from /data, under its pool
        # account: data, not the image
        self.process(104, "data/grbl-dev", ["/data/grbl-dev"], uid=provenance.POOL_UIDS[0])
        self.assertTrue(self.clean(prov))

    def test_a_script_the_image_does_not_carry_is_not_clean(self):
        prov = self.prov()
        self.put("data/bench/drill.py", b"print()\n")
        self.put("data/bench/drill.sh", b"echo\n")
        self.put("usr/share/bench/drill.py", b"print()\n")
        self.put("data/scratch/forgetest/__init__.py", b"")
        self.put("data/scratch2/json.py", b"")
        self.put("data/scratch3/gfhardware/__main__.py", b"")
        python, busybox = "usr/bin/python3.12", "bin/busybox"
        for what, exe, argv, cwd in (
                ("a script off the root", python, ["python3", "-u", "/data/bench/drill.py"], ""),
                ("a shell's, named from its directory", busybox, ["sh", "drill.sh"], "data/bench"),
                ("busybox's shell by its applet", busybox, ["/bin/busybox", "sh", "/data/bench/drill.sh"], ""),
                ("a login shell's", busybox, ["-sh", "/data/bench/drill.sh"], ""),
                ("on the root, no program of the image's", python, ["python3", "/usr/share/bench/drill.py"], ""),
                # -m looks in its working directory first, for its module and
                # for every module it imports
                ("a module in the working directory", python, ["python3", "-m", "forgetest"], "data/scratch"),
                ("an import in the working directory", python, ["python3", "-m", "forgetest"], "data/scratch2"),
                # a component's namespace package (gfhardware here has no
                # __init__) takes the working directory's portion first
                ("a namespace portion", python, ["python3", "-m", "gfhardware"], "data/scratch3"),
                # a module no component installed: the script runners
                ("pdb", python, ["python3", "-m", "pdb", "/data/bench/drill.py"], ""),
                ("runpy", python, ["python3", "-m", "runpy", "drill"], "data/bench"),
                # Python code from its command line or its input
                ("-c", python, ["python3", "-c", "exec(open('/data/bench/drill.py').read())"], ""),
                ("its input", python, ["python3", "-"], ""),
                ("a prompt", python, ["python3"], ""),
                ("bytecode from elsewhere", python, ["python3", "-X", "pycache_prefix=/data/pyc",
                                                     "/usr/bin/forgectrl"], "")):
            self.process(110, exe, argv, cwd=cwd)
            self.assertFalse(self.clean(prov), what)
            self.end(110)
        # The image's own: its respawn wrappers' `sh -c`, a login shell, a
        # shell option's value, forgetest started in /data (its data
        # directory there is a bare one, which the package's own regular one
        # wins over), a module of a component's, and a component's file run
        # as a script, which needs no execute bit.
        self.put("data/forgetest/results.jsonl", b"")
        for pid, (exe, argv, cwd) in enumerate((
                (busybox, ["/bin/sh", "-c", "while :; do /usr/bin/forgectrl; done"], ""),
                (busybox, ["-sh"], "data"),
                (busybox, ["sh", "-eo", "pipefail", "/usr/bin/forgectrl"], ""),
                (python, ["/usr/bin/python3", "-m", "forgetest"], "data"),
                (python, ["python3", "-m", "gfhardware"], ""),
                (python, ["python3", "-u", "/%s/x.py" % SITE], "data")), start=120):
            self.process(pid, exe, argv, cwd=cwd)
        self.assertEqual(prov.check()["why"], [])

    def test_a_process_that_execs_while_it_is_read_is_judged_as_what_it_became(self):
        # forgectrl's child, read as forgectrl, execs the controller before
        # its program is hashed: it is read again, as the controller
        self.process(120, "usr/bin/forgectrl", ["/usr/bin/forgectrl"])
        prov = self.prov()
        hashed, link, execs = prov._sha, self.path("proc/120/exe"), []

        def exec_while_hashed(path, every=False):
            if path == link and (every or not execs):
                was = os.readlink(link)
                to = "usr/bin/forgectrl" if was.endswith("grblHAL_glowforge") else "usr/bin/grblHAL_glowforge"
                os.remove(link)
                os.symlink(self.path(to), link)
                execs.append(to)
            return hashed(path)
        prov._sha = exec_while_hashed
        v = prov.check()
        self.assertEqual(v["why"], [])
        self.assertEqual(v["ran"]["/usr/bin/grblHAL_glowforge"], sha(GRBL))
        # one whose program changes on the second read too
        prov._sha = lambda path: exec_while_hashed(path, every=True)
        self.assertFalse(self.clean(prov))

    def test_a_variable_that_changes_what_runs_is_not_clean(self):
        # forgectrl told to run its controller from elsewhere (the override
        # stays in its environment while it runs), a bench knob in the
        # controller's, Python's bytecode and library from elsewhere, and
        # the controller's sink in a file
        for env in (b"PATH=/usr/bin\0FORGECTRL_GRBL_BIN=/data/grbl-dev\0", b"GFSINK_LEAD_MS=80\0",
                    b"PYTHONPYCACHEPREFIX=/data/pyc\0", b"PYTHONPLATLIBDIR=../../data/x/lib\0",
                    b"GFSINK=/tmp/sink.bin\0"):
            self.end(100)
            self.process(100, "usr/bin/forgectrl", ["/usr/bin/forgectrl"], env=env)
            self.assertFalse(self.clean(), env)
        # what forgectrl's supervisor sets for the controller is the image's
        self.end(100)
        self.process(100, "usr/bin/forgectrl", ["/usr/bin/forgectrl"],
                     env=b"GF_PULSE_FD=3\0GF_REPORT_SECRET=x\0GFSINK=/dev/glowforge\0")
        self.assertTrue(self.clean())

    def test_an_executable_mapping_the_image_does_not_carry_is_not_clean(self):
        self.put("data/libpreload.so", b"elf")
        self.put("usr/lib/libbench.so", b"elf")
        for lib in ("data/libpreload.so", "usr/lib/libbench.so"):
            self.end(100)
            self.process(100, "usr/bin/forgectrl", ["/usr/bin/forgectrl"], maps=["usr/bin/forgectrl", lib])
            self.assertFalse(self.clean(), lib)
        # a library replaced under the running daemon
        self.end(100)
        self.process(100, "usr/bin/forgectrl", ["/usr/bin/forgectrl"])
        with open(self.path("proc/100/maps"), "a") as f:
            f.write("b6e00000-b6f00000 r-xp 00000000 b3:01 200 %s%s\n"
                    % (self.path("usr/bin/forgectrl"), " (deleted)"))
        self.assertFalse(self.clean())

    def test_a_kernel_or_module_not_the_images_is_not_clean(self):
        prov = self.prov()
        self.assertTrue(self.clean(prov))
        cases = [("proc/sys/kernel/osrelease", b"6.12.20-fslc+gdeadbeef0000\n"),
                 ("sys/kernel/notes", note("d4" * 20)),
                 ("sys/module/glowforge/notes/.note.gnu.build-id", note("e5" * 20)),
                 ("proc/modules", b"glowforge 16384 0 - Live 0xbf000000 (O)\nbench 4096 0 - Live 0xbf100000 (O)\n"),
                 # the image's module, loaded again with another heat bound
                 ("sys/module/glowforge/parameters/heater_lease_ms", b"60000\n")]
        for rel, data in cases:
            with open(self.path(rel), "rb") as f:
                was = f.read()
            self.put(rel, data)
            self.assertFalse(self.clean(prov), rel)
            self.put(rel, was)
        self.assertTrue(self.clean(prov))
        # no build ID recorded, and notes that cannot be read
        data = copy.deepcopy(self.data)
        data["build"]["kernel"]["build_id"] = None
        os.remove(self.path("sys/kernel/notes"))
        self.assertFalse(self.clean(self.prov(data)))

    def test_a_writable_root_is_not_clean(self):
        self.put("proc/mounts", MOUNTS.replace(" ro,", " rw,").encode())
        self.assertFalse(self.clean())

    def test_a_mount_over_what_the_image_recorded_is_not_clean(self):
        # the image's own: /data and its binds, the /var/lib overlay, the
        # account files bound over /etc's
        own = MOUNTS + ("/dev/mmcblk1p2 /data/forgefirm ext4 rw 0 0\noverlay /var/lib overlay rw 0 0\n"
                        "tmpfs /etc/passwd tmpfs rw 0 0\n")
        self.put("proc/mounts", own.encode())
        self.assertTrue(self.clean())
        # a bind over the manifest, and a mount above an installed module
        for mp in ("/etc/forgefirm-manifest.json", "/usr/lib"):
            self.put("proc/mounts", (own + "/dev/mmcblk1p2 %s ext4 ro 0 0\n" % mp).encode())
            self.assertFalse(self.clean(), mp)

    def test_a_link_resolves_on_the_machine_not_the_host(self):
        # An absolute link target names a path on the machine: /bin is a
        # directory there, where the host may link it elsewhere.
        self.put("bin/tool.sh", b"echo\n")
        os.makedirs(self.path("usr/local/bin"))
        os.symlink("/bin/tool.sh", self.path("usr/local/bin/tool"))
        self.assertEqual(self.prov()._resolve("/usr/local/bin/tool"), "/bin/tool.sh")

    def test_the_build_id_is_read_from_the_notes(self):
        # Another owner's note, and a GNU note of another type, come first.
        notes = note("00000006", b"Linu", 6) + note("0000000000000000", b"GNU\x00", 5) + note(KERNEL_ID)
        self.assertEqual(provenance.note_build_id(notes), KERNEL_ID)
        self.assertIsNone(provenance.note_build_id(note("00000006", b"Linu", 6)))


class RunnerRecordsTests(unittest.TestCase):
    """The runner writes the verdict on the campaign when it opens, and on
    the result from a check before the run and one after it."""

    def test_the_verdict_is_recorded_and_a_change_during_the_run_counts(self):
        tmp = tempfile.mkdtemp(prefix="forgetest-prov-run-")
        try:
            t = helpers.make_test("fake.pass", [("forgectrl", "src/main.c")])
            log = Log(os.path.join(tmp, "results.jsonl"))
            runner = Runner(log, helpers.make_manifest(), helpers.registry(t))
            deployed = {"clean": False, "why": ["forgectrl: /usr/bin/forgectrl not as built"], "ran": {}}
            # the campaign's check, the run's before, the run's after
            runner.provenance = helpers.ScriptedProvenance(helpers.CLEAN, helpers.CLEAN, deployed)
            camp = runner._open_campaign_if_needed(runner.state()[0])
            runner._exec_test(t, Run("test", t.id, t.title), camp)
            recs = log.read()
            self.assertEqual([r["provenance"] for r in recs if r.get("t") == "campaign"], [helpers.CLEAN])
            res = [r for r in recs if r.get("t") == "result"]
            self.assertEqual(res[0]["result"], "PASS")
            self.assertFalse(provenance.clean(res[0]))
        finally:
            shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    unittest.main()
