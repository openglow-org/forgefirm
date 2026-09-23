# Copyright 2026 514 LLC d/b/a OpenGlow
# Written by Scott Wiederhold
# https://community.openglow.org
# SPDX-License-Identifier:    MIT

"""exthost.* - what holds an extension package: the image's sandbox platform."""
import contextlib
import json
import os
import re
import signal
import stat
import subprocess
import sys
import time

from .. import hw
from ..catalog import test
from .forgectrl import lan_ip
from .image import kernel_config
from .cloud import OFFLINE_STEP, Offline, dark_print_body, enter_offline, latch_locked, offline_cleanup, offline_job
from .setup import SAFETY_PHRASE, read_file, record_path, request, write_file

CG = "/sys/fs/cgroup"
POOL_GROUP = CG + "/ffx"
PROBE_GROUP = POOL_GROUP + "/forgetest-probe"
POOL_FIRST, POOL_SIZE = 800, 32
CONTROLLERS = ("cpu", "memory", "pids")
NFT = "/usr/sbin/nft"

# The probe: one short program, started as root. It puts itself into the
# group named in argv, becomes the uid named there (0 stays root), gives up
# new privileges, and then does one thing and prints one JSON line.
PROBE = r'''
import ctypes, errno, json, os, platform, socket, sys, time
group, uid, mode, arg = sys.argv[1], int(sys.argv[2]), sys.argv[3], sys.argv[4]
libc = ctypes.CDLL(None, use_errno=True)
if group != "-":
    with open(group + "/cgroup.procs", "w") as f:
        f.write(str(os.getpid()))
if uid:
    os.setgroups([])
    os.setgid(uid)
    os.setuid(uid)
libc.prctl(38, 1, 0, 0, 0)                                  # PR_SET_NO_NEW_PRIVS


def say(**kw):
    print(json.dumps(kw), flush=True)


def word(e):
    return {errno.ECONNREFUSED: "refused", errno.EPERM: "eperm", errno.EACCES: "eacces"}.get(
        e.errno, errno.errorcode.get(e.errno, str(e.errno)))


def reach(family, addr, port, udp):
    s = socket.socket(socket.AF_INET6 if family == 6 else socket.AF_INET,
                      socket.SOCK_DGRAM if udp else socket.SOCK_STREAM)
    s.settimeout(4.0)
    t0 = time.time()
    try:
        if udp:
            s.sendto(b"x", (addr, port))
        else:
            s.connect((addr, port))
        out = "ok"
    except socket.timeout:
        out = "timeout"
    except OSError as e:
        out = word(e)
    s.close()
    return [out, round(time.time() - t0, 2)]


if mode == "spin":
    say(started=True)
    while True:
        pass
elif mode == "forks":
    kids = []
    err = ""
    for _ in range(int(arg)):
        try:
            pid = os.fork()
        except OSError as e:
            err = word(e)
            break
        if pid == 0:
            time.sleep(30)
            os._exit(0)
        kids.append(pid)
    for pid in kids:
        os.kill(pid, 9)
        os.waitpid(pid, 0)
    say(forked=len(kids), stopped_by=err)
elif mode == "eat":
    say(started=True)
    held = []
    for _ in range(int(arg)):
        held.append(bytearray(os.urandom(4096)) * 256)       # 1 MiB, every page written
    say(survived=True)
elif mode == "net":
    say(results=[[t, reach(*t)] for t in json.loads(arg)])
elif mode == "unix":
    s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    s.settimeout(4.0)
    try:
        s.connect(arg)
        say(result="connected")
    except OSError as e:
        say(result=word(e))
    s.close()
elif mode == "landlock":
    target = json.loads(arg)
    before = {"etc": None, "usr": None, "tcp": reach(*target)[0]}
    for key, path in (("etc", "/etc/hostname"), ("usr", sys.executable)):
        try:
            open(path, "rb").close()
            before[key] = "ok"
        except OSError as e:
            before[key] = word(e)
    abi = libc.syscall(444, None, 0, 1)                      # LANDLOCK_CREATE_RULESET_VERSION
    after = {}
    if abi >= 1:
        # write, read file, read dir; and from ABI 4 on, connect TCP (an older kernel takes the first field alone)
        attr = (ctypes.c_uint64 * 2)((1 << 1) | (1 << 2) | (1 << 3), 1 << 1)
        ruleset = libc.syscall(444, ctypes.byref(attr), 16 if abi >= 4 else 8, 0)
        usr = os.open("/usr", os.O_PATH | os.O_CLOEXEC)

        class PathBeneath(ctypes.Structure):
            _pack_ = 1
            _fields_ = [("allowed_access", ctypes.c_uint64), ("parent_fd", ctypes.c_int32)]
        rule = PathBeneath((1 << 2) | (1 << 3), usr)
        added = libc.syscall(445, ruleset, 1, ctypes.byref(rule), 0)           # LANDLOCK_RULE_PATH_BENEATH
        applied = libc.syscall(446, ruleset, 0)
        after = {"ruleset": ruleset >= 0, "added": added, "applied": applied, "tcp": reach(*target)[0]}
        for key, path in (("etc", "/etc/hostname"), ("usr", sys.executable)):
            try:
                open(path, "rb").close()
                after[key] = "ok"
            except OSError as e:
                after[key] = word(e)
    say(abi=abi, before=before, after=after)
elif mode == "seccomp":
    arch, nr = {"armv7l": (0x40000028, 122), "aarch64": (0xC00000B7, 160),
                "x86_64": (0xC000003E, 63)}[platform.machine()]             # uname


    class Filter(ctypes.Structure):
        _fields_ = [("code", ctypes.c_uint16), ("jt", ctypes.c_uint8), ("jf", ctypes.c_uint8),
                    ("k", ctypes.c_uint32)]


    class Prog(ctypes.Structure):
        _fields_ = [("len", ctypes.c_ushort), ("filter", ctypes.POINTER(Filter))]
    before = os.uname().sysname
    ops = (Filter * 7)(Filter(0x20, 0, 0, 4),                # load arch
                       Filter(0x15, 1, 0, arch),             # ours: go on
                       Filter(0x06, 0, 0, 0x80000000),       # another ABI: kill the process
                       Filter(0x20, 0, 0, 0),                # load the syscall number
                       Filter(0x15, 0, 1, nr),               # uname?
                       Filter(0x06, 0, 0, 0x00050000 | errno.EPERM),
                       Filter(0x06, 0, 0, 0x7FFF0000))       # everything else: allow
    prog = Prog(7, ops)
    rc = libc.prctl(22, 2, ctypes.byref(prog), 0, 0)         # PR_SET_SECCOMP, SECCOMP_MODE_FILTER
    try:
        os.uname()
        after = "ok"
    except OSError as e:
        after = word(e)
    mode_now = [l.split()[1] for l in open("/proc/self/status") if l.startswith("Seccomp:")]
    say(installed=rc, before=before, after=after, status=mode_now)
'''


def _read(path, default=""):
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as f:
            return f.read()
    except OSError:
        return default


def _write(path, text):
    with open(path, "w") as f:
        f.write(text)


def _flat(path):
    """A cgroup flat-keyed file as {key: int}."""
    out = {}
    for line in _read(path).splitlines():
        parts = line.split()
        if len(parts) == 2 and parts[1].isdigit():
            out[parts[0]] = int(parts[1])
    return out


def _probe(group, uid, mode, arg="", wait=30):
    """Runs the probe to its end; (returncode, its JSON lines)."""
    p = subprocess.run([sys.executable, "-c", PROBE, group, str(uid), mode, arg],
                       capture_output=True, text=True, timeout=wait)
    lines = []
    for line in p.stdout.splitlines():
        try:
            lines.append(json.loads(line))
        except ValueError:
            pass
    return p.returncode, lines, p.stderr.strip()[-300:]


def _pids_of(comm):
    out = []
    for d in os.listdir("/proc"):
        if d.isdigit() and _read("/proc/%s/comm" % d).strip() == comm:
            out.append(int(d))
    return out


def _pool_counters():
    """The packet counts of chain refuse's two rules: [tcp reset, drop]."""
    p = subprocess.run([NFT, "-j", "list", "chain", "inet", "ffx", "refuse"], capture_output=True, text=True)
    if p.returncode != 0:
        return None
    return [e["counter"]["packets"] for c in json.loads(p.stdout)["nftables"] if "rule" in c
            for e in c["rule"]["expr"] if "counter" in e]


@test("exthost.platform", title="The extension sandbox platform holds a process",
      subsystem="exthost", kind="auto", est_min=2,
      covers=[("linux-fslc", "**")],
      description="What the image holds ready before any extension package exists, proven on a "
                  "probe process rather than read off a config. The kernel has the cpu, memory, "
                  "and pids controllers, seccomp filters, and landlock, and has no realtime "
                  "group scheduler (the pulse feeder is SCHED_FIFO and must not need a group's "
                  "leave to run). cgroup v2 is mounted with the three controllers handed down "
                  "to /sys/fs/cgroup/ffx, which is idle-class, and forgectrl and the controller "
                  "sit in the root group. In a probe group under it, as the last pool uid: a "
                  "spinning process is held to its cpu.max, stops when the group is frozen and "
                  "runs again when thawed; a fork loop stops at pids.max; and a process that "
                  "takes more than memory.max is killed by the group's own OOM while forgectrl "
                  "keeps its pid. The account pool is ffx0 to ffx31, uid and gid 800 to 831, no "
                  "shell, no home, locked, in the account files as forgefirm-users rendered "
                  "them. The deny rules are in the kernel: a pool uid's TCP connect to "
                  "forgectrl on loopback (both ports, IPv4 and IPv6), to the machine's LAN "
                  "address, and to the Grbl port is refused at once, its UDP send gets EPERM, "
                  "and the rules' counters saw every attempt, while root reaches the same "
                  "listeners. A process that restricts itself with landlock loses /etc and TCP "
                  "connects and keeps /usr; one that installs a seccomp filter gets EPERM from "
                  "the filtered call. The layer content (the recipe, the rules, the distro "
                  "option) is in the platform identity of every fingerprint.")
def platform(ctx):
    ev = ctx.evidence

    # 1. the kernel
    cfg = kernel_config()
    ctx.check(cfg is not None, "kernel config unavailable (/proc/config.gz, /boot/config-*)")
    want = {"CONFIG_CGROUPS": "y", "CONFIG_CGROUP_SCHED": "y", "CONFIG_FAIR_GROUP_SCHED": "y",
            "CONFIG_CFS_BANDWIDTH": "y", "CONFIG_MEMCG": "y", "CONFIG_CGROUP_PIDS": "y",
            "CONFIG_SECCOMP_FILTER": "y", "CONFIG_SECURITY_LANDLOCK": "y", "CONFIG_NF_TABLES": "y",
            "CONFIG_NF_TABLES_INET": "y", "CONFIG_NFT_REJECT_INET": "y", "CONFIG_NFT_LIMIT": "y",
            "CONFIG_RT_GROUP_SCHED": "n", "CONFIG_NF_CONNTRACK": "n"}
    got = {k: cfg.get(k, "n") for k in want}
    ev["kernel"] = got
    wrong = {k: v for k, v in got.items() if v != want[k]}
    ctx.check(not wrong, "kernel options: %s (wanted %s)", wrong, {k: want[k] for k in wrong})
    ctx.check("landlock" in cfg.get("CONFIG_LSM", ""), "landlock is not in CONFIG_LSM: %s", cfg.get("CONFIG_LSM"))

    # 2. the cgroup tree
    mounts = [l.split() for l in _read("/proc/mounts").splitlines()]
    ctx.check(any(m[1] == CG and m[2] == "cgroup2" for m in mounts if len(m) > 2), "cgroup2 is not mounted at %s", CG)
    tree = {"controllers": _read(CG + "/cgroup.controllers").split(),
            "root_subtree": _read(CG + "/cgroup.subtree_control").split(),
            "ffx_subtree": _read(POOL_GROUP + "/cgroup.subtree_control").split(),
            "ffx_cpu_idle": _read(POOL_GROUP + "/cpu.idle").strip()}
    ev["cgroup_tree"] = tree
    ctx.log("cgroup tree: %s", tree)
    for key in ("controllers", "root_subtree", "ffx_subtree"):
        ctx.check(set(CONTROLLERS) <= set(tree[key]), "%s lacks a controller: %s", key, tree[key])
    ctx.check(tree["ffx_cpu_idle"] == "1", "the ffx group is not idle-class: cpu.idle=%r", tree["ffx_cpu_idle"])
    homes = {}
    for comm in ("forgectrl", "grblHAL_glowfor", "forgetest"):
        for pid in _pids_of(comm):
            homes["%s/%d" % (comm, pid)] = _read("/proc/%d/cgroup" % pid).strip()
    ev["firmware_groups"] = homes
    ctx.check(any(k.startswith("forgectrl/") for k in homes), "forgectrl is not running")
    ctx.check(all(v == "0::/" for v in homes.values()), "a firmware process is outside the root group: %s", homes)

    # 3. the account pool, as the boot's render left it
    passwd = {l.split(":")[0]: l.split(":") for l in _read("/etc/passwd").splitlines() if l.count(":") >= 6}
    shadow = {l.split(":")[0]: l.split(":") for l in _read("/etc/shadow").splitlines() if ":" in l}
    group = {l.split(":")[0]: l.split(":") for l in _read("/etc/group").splitlines() if l.count(":") >= 3}
    bad = []
    for n in range(POOL_SIZE):
        name, uid = "ffx%d" % n, str(POOL_FIRST + n)
        p, s, g = passwd.get(name), shadow.get(name), group.get(name)
        if not p or p[2] != uid or p[3] != uid or p[5] != "/nonexistent" or p[6] != "/bin/false":
            bad.append("passwd %s: %s" % (name, p))
        if not s or s[1][:1] not in ("!", "*"):
            bad.append("shadow %s is not locked" % name)
        if not g or g[2] != uid or g[3] != "":
            bad.append("group %s: %s" % (name, g))
    others = [p[0] for p in passwd.values() if p[2].isdigit()
              and POOL_FIRST <= int(p[2]) < POOL_FIRST + POOL_SIZE and not re.match(r"^ffx\d+$", p[0])]
    ev["pool"] = {"accounts": sum(1 for n in passwd if re.match(r"^ffx\d+$", n)), "problems": bad, "others": others}
    ctx.check(not bad, "the account pool: %s", bad[:6])
    ctx.check(not others, "another account sits in the pool's uid range: %s", others)
    ctx.check(ev["pool"]["accounts"] == POOL_SIZE, "%d ffx accounts, expected %d", ev["pool"]["accounts"], POOL_SIZE)

    uid = POOL_FIRST + POOL_SIZE - 1
    fc_pid = _pids_of("forgectrl")
    spin = None
    try:
        # 4. a probe group holds a process: cpu.max, the freezer, pids.max, memory.max
        os.makedirs(PROBE_GROUP, exist_ok=True)
        _write(PROBE_GROUP + "/cpu.max", "5000 100000")
        _write(PROBE_GROUP + "/pids.max", "6")
        _write(PROBE_GROUP + "/memory.max", str(24 << 20))
        if os.path.exists(PROBE_GROUP + "/memory.swap.max"):
            _write(PROBE_GROUP + "/memory.swap.max", "0")

        spin = subprocess.Popen([sys.executable, "-c", PROBE, PROBE_GROUP, str(uid), "spin", ""],
                                stdout=subprocess.PIPE, text=True)
        ctx.check("started" in (spin.stdout.readline() or ""), "the spinning probe did not start")
        ctx.check(_read("/proc/%d/cgroup" % spin.pid).strip() == "0::/ffx/forgetest-probe",
                  "the probe is in %r", _read("/proc/%d/cgroup" % spin.pid).strip())
        ctx.check("Uid:\t%d\t%d" % (uid, uid) in _read("/proc/%d/status" % spin.pid), "the probe is not uid %d", uid)
        ctx.sleep(1.0)
        u0, t0 = _flat(PROBE_GROUP + "/cpu.stat")["usage_usec"], time.time()
        ctx.sleep(3.0)
        u1, t1 = _flat(PROBE_GROUP + "/cpu.stat")["usage_usec"], time.time()
        share = (u1 - u0) / 1e6 / (t1 - t0)
        throttled = _flat(PROBE_GROUP + "/cpu.stat").get("nr_throttled", 0)
        ctx.log("a spinning probe under cpu.max 5%%: %.1f%% of the core, throttled %d times", share * 100, throttled)
        ctx.check(0.01 < share < 0.08 and throttled > 0, "cpu.max 5%% held the probe to %.1f%% (throttled %d)",
                  share * 100, throttled)

        _write(PROBE_GROUP + "/cgroup.freeze", "1")
        ok = ctx.wait_for(lambda: _flat(PROBE_GROUP + "/cgroup.events").get("frozen") == 1, 3, poll=0.05)
        ctx.check(ok is not None, "the group did not freeze: %s", _flat(PROBE_GROUP + "/cgroup.events"))
        f0 = _flat(PROBE_GROUP + "/cpu.stat")["usage_usec"]
        ctx.sleep(1.5)
        f1 = _flat(PROBE_GROUP + "/cpu.stat")["usage_usec"]
        _write(PROBE_GROUP + "/cgroup.freeze", "0")
        ok = ctx.wait_for(lambda: _flat(PROBE_GROUP + "/cgroup.events").get("frozen") == 0, 3, poll=0.05)
        ctx.check(ok is not None, "the group did not thaw")
        ctx.sleep(1.5)
        f2 = _flat(PROBE_GROUP + "/cpu.stat")["usage_usec"]
        ctx.log("frozen for 1.5 s the probe ran %d us; thawed for 1.5 s it ran %d us", f1 - f0, f2 - f1)
        ctx.check(f1 - f0 == 0, "a frozen probe ran %d us", f1 - f0)
        ctx.check(f2 - f1 > 10000, "a thawed probe ran only %d us", f2 - f1)
        _write(PROBE_GROUP + "/cgroup.kill", "1")
        spin.wait(timeout=5)
        ctx.check(spin.returncode == -signal.SIGKILL, "cgroup.kill left the probe with %s", spin.returncode)
        spin = None
        ev["cpu"] = {"share": round(share, 4), "throttled": throttled, "frozen_us": f1 - f0, "thawed_us": f2 - f1}

        rc, lines, err = _probe(PROBE_GROUP, uid, "forks", "12")
        forks = lines[-1] if lines else {}
        hits = _flat(PROBE_GROUP + "/pids.events").get("max", 0)
        ev["pids"] = {"result": forks, "events_max": hits}
        ctx.log("pids.max 6: the fork loop made %s of 12, stopped by %s; pids.events max %d",
                forks.get("forked"), forks.get("stopped_by"), hits)
        ctx.check(rc == 0 and forks.get("forked") == 5 and forks.get("stopped_by") == "EAGAIN" and hits > 0,
                  "pids.max 6 did not stop the fork loop at 5: rc %s %s %s, events max %d", rc, forks, err, hits)

        rc, lines, err = _probe(PROBE_GROUP, uid, "eat", "96", wait=60)
        kills = _flat(PROBE_GROUP + "/memory.events").get("oom_kill", 0)
        ev["memory"] = {"rc": rc, "lines": lines, "oom_kill": kills, "peak": _read(PROBE_GROUP + "/memory.peak").strip()}
        ctx.log("memory.max 24 MiB against a 96 MiB appetite: rc %s, oom_kill %d, peak %s", rc, kills, ev["memory"]["peak"])
        ctx.check(rc == -signal.SIGKILL and kills > 0 and not any(l.get("survived") for l in lines),
                  "memory.max did not kill the probe: rc %s, oom_kill %d, %s %s", rc, kills, lines, err)
        ctx.check(_pids_of("forgectrl") == fc_pid, "forgectrl's pid moved across the group's OOM: %s -> %s",
                  fc_pid, _pids_of("forgectrl"))

        # 5. the deny rules
        p = subprocess.run([NFT, "list", "table", "inet", "ffx"], capture_output=True, text=True)
        ctx.check(p.returncode == 0, "table inet ffx is not loaded: %s", p.stderr.strip()[:200])
        ev["rules"] = p.stdout
        ctx.check("meta skuid %d-%d jump pool" % (POOL_FIRST, uid) in p.stdout and "hook output" in p.stdout
                  and "policy accept" in p.stdout, "table inet ffx is not the image's: %s", p.stdout[:400])
        pool = p.stdout[p.stdout.find("chain pool"):].split("}")[0]
        ctx.check(0 <= pool.find('oifname "lo" jump refuse') < pool.find("vmap @allow"),
                  "the machine itself is not refused ahead of the allow map: %s", pool)
        lan = lan_ip()
        ctx.check(lan, "the machine has no LAN address to aim at")
        tcp = [[4, "127.0.0.1", 443, False], [4, "127.0.0.1", 80, False], [6, "::1", 443, False],
               [6, "::1", 23, False], [4, lan, 443, False], [4, lan, 23, False]]
        udp = [[4, "127.0.0.1", 9, True], [4, lan, 9, True]]
        control = [[4, "127.0.0.1", 443, False], [4, lan, 443, False], [4, "127.0.0.1", 9, True]]
        rc, lines, err = _probe("-", 0, "net", json.dumps(control))
        as_root = (lines[-1] if lines else {}).get("results", [])
        ev["as_root"] = as_root
        ctx.check(len(as_root) == len(control) and all(r[1][0] == "ok" for r in as_root),
                  "root does not reach its own listeners: %s %s", as_root, err)
        c0 = _pool_counters()
        heard = {}
        for who in (POOL_FIRST, uid):
            rc, lines, err = _probe("-", who, "net", json.dumps(tcp + udp))
            res = (lines[-1] if lines else {}).get("results", [])
            heard[str(who)] = res
            ctx.check(len(res) == len(tcp + udp), "the probe as uid %d said %s %s", who, lines, err)
            for target, (word, took) in res:
                if target[3]:
                    ctx.check(word == "eperm", "uid %d UDP to %s -> %s", who, target[1], word)
                else:
                    ctx.check(word == "refused" and took < 1.5, "uid %d TCP to %s port %d -> %s in %.2f s",
                              who, target[1], target[2], word, took)
        c1 = _pool_counters()
        ev["as_pool_uid"] = heard
        ev["counters"] = [c0, c1]
        ctx.log("two pool uids: %d TCP connects each refused at once, %d UDP sends each EPERM; the rules "
                "counted %s -> %s", len(tcp), len(udp), c0, c1)
        ctx.check(c0 is not None and c1 is not None and len(c1) == 2 and c1[0] - c0[0] >= 2 * len(tcp)
                  and c1[1] - c0[1] >= 2 * len(udp), "the rules did not count the attempts: %s -> %s", c0, c1)

        # The machine itself is not a destination, whatever the allow map says: an
        # allow chain that names forgectrl on loopback and on the LAN address
        # opens neither.
        chain = "u%d" % uid
        rules = ["add chain inet ffx %s" % chain,
                 "add rule inet ffx %s ip daddr { 127.0.0.1, %s } tcp dport 443 accept" % (chain, lan),
                 "add element inet ffx allow { %d : jump %s }" % (uid, chain), ""]
        opened = subprocess.run([NFT, "-f", "-"], capture_output=True, text=True, input=os.linesep.join(rules))
        try:
            ctx.check(opened.returncode == 0, "could not add an allow chain: %s", opened.stderr.strip()[:200])
            rc, lines, err = _probe("-", uid, "net", json.dumps([[4, "127.0.0.1", 443, False], [4, lan, 443, False]]))
            res = (lines[-1] if lines else {}).get("results", [])
            ev["allowlisted_self"] = res
            ctx.log("uid %d with the machine's own addresses on its allowlist: %s", uid, res)
            ctx.check(len(res) == 2 and all(r[1][0] == "refused" for r in res),
                      "an allowlist opened the machine itself to a pool uid: %s %s", res, err)
        finally:
            subprocess.run([NFT, "delete", "element", "inet", "ffx", "allow", "{ %d }" % uid], capture_output=True)
            subprocess.run([NFT, "flush", "chain", "inet", "ffx", chain], capture_output=True)
            subprocess.run([NFT, "delete", "chain", "inet", "ffx", chain], capture_output=True)
        left = subprocess.run([NFT, "list", "chain", "inet", "ffx", chain], capture_output=True)
        ctx.check(left.returncode != 0, "the test's allow chain stayed behind")

        # 6. landlock and seccomp, on a root process so that neither the uid nor the rules explain the refusal
        rc, lines, err = _probe("-", 0, "landlock", json.dumps([4, "127.0.0.1", 443, False]))
        ll = lines[-1] if lines else {}
        ev["landlock"] = ll
        ctx.log("landlock ABI %s: before %s, after %s", ll.get("abi"), ll.get("before"), ll.get("after"))
        ctx.check(rc == 0 and ll.get("abi", 0) >= 4, "landlock ABI %s (4 brings the TCP rules) %s", ll.get("abi"), err)
        ctx.check(ll["before"] == {"etc": "ok", "usr": "ok", "tcp": "ok"}, "before the restriction: %s", ll["before"])
        after = ll["after"]
        ctx.check(after.get("ruleset") and after.get("added") == 0 and after.get("applied") == 0,
                  "the ruleset did not apply: %s", after)
        ctx.check(after.get("etc") == "eacces" and after.get("tcp") == "eacces" and after.get("usr") == "ok",
                  "after the restriction (/etc and TCP lost, /usr kept): %s", after)

        rc, lines, err = _probe("-", 0, "seccomp")
        sc = lines[-1] if lines else {}
        ev["seccomp"] = sc
        ctx.log("seccomp: %s", sc)
        ctx.check(rc == 0 and sc.get("installed") == 0 and sc.get("before") == "Linux" and sc.get("after") == "eperm"
                  and sc.get("status") == ["2"], "a seccomp filter did not take: rc %s %s %s", rc, sc, err)
    finally:
        if spin is not None:
            spin.kill()
            spin.wait(timeout=5)
        if os.path.isdir(PROBE_GROUP):
            try:
                _write(PROBE_GROUP + "/cgroup.freeze", "0")
                _write(PROBE_GROUP + "/cgroup.kill", "1")
            except OSError:
                pass
            for _ in range(50):
                if not _read(PROBE_GROUP + "/cgroup.procs").strip():
                    break
                time.sleep(0.1)
            try:
                os.rmdir(PROBE_GROUP)
            except OSError as e:
                ctx.log("the probe group stayed behind: %s", e)
    ctx.check(not os.path.isdir(PROBE_GROUP), "the probe group stayed behind")
    ctx.log("PASS: the kernel, the cgroup tree, the account pool, and the deny rules are in place, and each "
            "held a probe process the way it will hold a package")


# ------------------------------------------------------------ the host

FORGEEXT = "/usr/bin/forgeext"
FWUP = "/usr/bin/fwup"
EXT_ROOT = "/data/forgefirm/ext"
HOST_STATUS = "/run/forgefirm/ext/status.json"
SAFE_FILE = "/run/forgefirm/ext-safe"
HOST_LOG = "/data/log/forgefirm/forgeext/forgeext.log"
API_DIR = "/run/forgefirm/ext/api"
REF_ID = "org.forgetest.reference"
REF_KEY = "forgetest-reference"
REF_DEST = "192.0.2.1"                      # TEST-NET-1: declared so that port 443 is, and never dialed

# The reference package's service. It looks at its own confinement from the
# inside, leaves what it found in its data directory, and stays up.
REF_SERVICE = r'''
import errno, json, os, socket, sys, time
lan = sys.argv[1]
data, pkg = os.environ["FFX_DATA"], os.environ["FFX_PKG"]
if os.path.exists(os.path.join(data, "stop")):
    sys.exit(3)


def word(e):
    return errno.errorcode.get(e.errno, str(e.errno))


def api(method, path, body=None):
    """One request to the host over the socket named in FFX_API: (status, JSON)."""
    payload = json.dumps(body).encode() if body is not None else b""
    head = "%s %s HTTP/1.1\r\nHost: forgeext\r\n" % (method, path)
    if body is not None:
        head += "Content-Type: application/json\r\nContent-Length: %d\r\n" % len(payload)
    s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    s.settimeout(5)
    try:
        s.connect(os.environ["FFX_API"])
        s.sendall(head.encode() + b"\r\n" + payload)
        buf = b""
        while True:
            c = s.recv(65536)
            if not c:
                break
            buf += c
        top, _, text = buf.partition(b"\r\n\r\n")
        return int(top.split()[1]), json.loads(text or b"null")
    except (OSError, ValueError, IndexError) as e:
        return 599, {"error": str(e)}
    finally:
        s.close()


def shot(body):
    """POST /v0/camera: (status, bytes or the error's words). A frame is
    bytes, so this one does not try to read the answer as JSON."""
    payload = json.dumps(body).encode()
    head = ("POST /v0/camera HTTP/1.1\r\nHost: forgeext\r\nContent-Type: application/json\r\n"
            "Content-Length: %d\r\n\r\n" % len(payload))
    s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    s.settimeout(40)
    try:
        s.connect(os.environ["FFX_API"])
        s.sendall(head.encode() + payload)
        buf = b""
        while True:
            c = s.recv(65536)
            if not c:
                break
            buf += c
        top, _, rest = buf.partition(b"\r\n\r\n")
        code = int(top.split()[1])
        if code == 200:
            return code, len(rest), rest[:2] == b"\xff\xd8", b"image/jpeg" in top
        try:
            return code, json.loads(rest or b"{}").get("error", ""), False, False
        except ValueError:
            return code, rest[:80].decode("utf-8", "replace"), False, False
    except (OSError, ValueError, IndexError) as e:
        return 599, str(e), False, False
    finally:
        s.close()


def opened(path, mode="r"):
    try:
        with open(path, mode) as f:
            if "r" in mode:
                f.read(1)
            else:
                f.write("x")
        return "ok"
    except OSError as e:
        return word(e)


def dial(addr, port):
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.settimeout(4.0)
    try:
        s.connect((addr, port))
        return "connected"
    except socket.timeout:
        return "timeout"
    except OSError as e:
        return word(e)
    finally:
        s.close()


def family(af):
    try:
        socket.socket(af, socket.SOCK_RAW if af == 16 else socket.SOCK_DGRAM).close()
        return "ok"
    except OSError as e:
        return word(e)


report = {
    "uid": os.getuid(), "gid": os.getgid(), "groups": os.getgroups(), "cwd": os.getcwd(),
    "env": sorted(os.environ),
    "read_settings": opened("/data/forgefirm/forgefirm.conf"),
    "read_setup": opened("/data/forgefirm/setup.json"),
    "read_etc": opened("/etc/hostname"),
    "read_pkg": opened(os.path.join(pkg, "manifest.json")),
    "write_pkg": opened(os.path.join(pkg, "x"), "w"),
    "write_data": opened(os.path.join(data, "scratch"), "w"),
    "write_tmp": opened("/tmp/forgetest-reference", "w"),
    "pulse_device": opened("/dev/glowforge"),
    "loopback_declared_port": dial("127.0.0.1", 443),
    "lan_declared_port": dial(lan, 443) if lan != "-" else "skipped",
    "loopback_undeclared_port": dial("127.0.0.1", 80),
    "netlink_socket": family(16),
    "unix_socket": family(1),
    "api_camera": shot({"camera": "lid", "resolution": "half"}),
    "api_camera_head": shot({"camera": "head"}),
    "api_camera_bad": shot({"camera": "bed"}),
    "api_camera_dark": shot({"camera": "lid", "resolution": "half", "lamp": 0}),
    "api_camera_lit": shot({"camera": "lid", "resolution": "half", "lamp": 1023}),
    "api_camera_lamp_bad": shot({"camera": "lid", "lamp": 5000}),
    "api_settings": api("GET", "/v0/settings"),
    "api_settings_set": api("POST", "/v0/settings", {"threshold": 70, "note": "set from inside"}),
    "api_settings_undeclared": api("POST", "/v0/settings", {"nothere": 1}),
    "api_settings_out_of_bounds": api("POST", "/v0/settings", {"threshold": 9000}),
    "api_self": api("GET", "/v0/self"),
    "api_mode": api("GET", "/v0/machine/mode"),
    "api_hold": api("GET", "/v0/hold"),
    "api_nowhere": api("GET", "/v0/nowhere"),
    "api_traversal": api("GET", "/v0/machine/../../settings"),
}
with open(os.path.join(data, "report.json.new"), "w") as f:
    json.dump(report, f)
os.rename(os.path.join(data, "report.json.new"), os.path.join(data, "report.json"))
print("reference service up", flush=True)
mine = api("GET", "/v0/self")[1] or {}
watching = "events" in (mine.get("capabilities") or [])
place, seen, polls = None, [], []
n = 0
while True:
    n += 1
    with open(os.path.join(data, "beat"), "w") as f:
        f.write(str(n))
    if watching:
        # A poll that waits: it comes back with an event as soon as there
        # is one, and with nothing when its two seconds are up.
        ask = {"wait": 2} if place is None else {"since": place, "wait": 2}
        began = time.time()
        st, ans = api("POST", "/v0/events", ask)
        took = round(time.time() - began, 2)
        if st == 200:
            place = ans.get("next")
            seen = (seen + [e.get("event") for e in ans.get("events") or []])[-40:]
        polls = (polls + [{"status": st, "took": took, "placed": "since" in ask,
                           "got": len(ans.get("events") or []) if isinstance(ans, dict) else 0}])[-20:]
        with open(os.path.join(data, "events.json.new"), "w") as f:
            json.dump({"place": place, "seen": seen, "polls": polls, "last": ans,
                       "connected": ans.get("connected") if isinstance(ans, dict) else None}, f)
        os.rename(os.path.join(data, "events.json.new"), os.path.join(data, "events.json"))
    # the test's word to the service: write a program and run it
    runf = os.path.join(data, "run")
    if os.path.exists(runf):
        with open(runf) as f:
            words = json.load(f)
        os.remove(runf)
        if words.get("program_text") is not None:
            with open(os.path.join(data, words.get("program", "job.gcode")), "w") as f:
                f.write(words["program_text"])
        body = {k: v for k, v in words.items() if k not in ("program_text",)}
        with open(os.path.join(data, "ran.new"), "w") as f:
            json.dump(api("POST", "/v0/motion/job", body), f)
        os.rename(os.path.join(data, "ran.new"), os.path.join(data, "ran"))
    # the test's word to the service: jog now
    jog = os.path.join(data, "jog")
    if os.path.exists(jog):
        with open(jog) as f:
            words = json.load(f)
        os.remove(jog)
        with open(os.path.join(data, "jogged.new"), "w") as f:
            json.dump(api("POST", "/v0/motion/jog", words), f)
        os.rename(os.path.join(data, "jogged.new"), os.path.join(data, "jogged"))
    # the test's word to the service: take a capture now
    ask = os.path.join(data, "shoot")
    if os.path.exists(ask):
        os.remove(ask)
        with open(os.path.join(data, "shot.new"), "w") as f:
            json.dump(shot({"camera": "lid", "resolution": "half"}), f)
        os.rename(os.path.join(data, "shot.new"), os.path.join(data, "shot"))
    # the test's word to the service: what to say of its hold
    say = os.path.join(data, "say")
    if os.path.exists(say):
        with open(say) as f:
            words = json.load(f)
        os.remove(say)
        with open(os.path.join(data, "said.new"), "w") as f:
            json.dump(api("POST", "/v0/hold", words), f)
        os.rename(os.path.join(data, "said.new"), os.path.join(data, "said"))
    time.sleep(0.5)
'''


def _host_pids():
    """The extension host: /usr/bin/forgeext run, and not an install or a
    check someone has under way."""
    out = []
    for d in os.listdir("/proc"):
        if not d.isdigit():
            continue
        try:
            if os.readlink("/proc/%s/exe" % d) != FORGEEXT:
                continue
            with open("/proc/%s/cmdline" % d, "rb") as f:
                argv = f.read().split(b"\0")
        except OSError:
            continue
        if argv[:2] == [FORGEEXT.encode(), b"run"]:
            out.append(int(d))
    return out


def _host_status():
    try:
        with open(HOST_STATUS) as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}


def _svc(id_):
    return next((x for x in _host_status().get("services", []) if x.get("id") == id_), {})


def _forgeext(*args, wait=120):
    p = subprocess.run([FORGEEXT] + list(args), capture_output=True, text=True, timeout=wait)
    try:
        return json.loads(p.stdout)
    except ValueError:
        return {"ok": False, "error": "no JSON answer (exit %s): %s %s" % (p.returncode, p.stdout[-200:], p.stderr[-200:])}


def _until(ctx, cond, seconds, poll=0.25):
    end = time.time() + seconds
    while time.time() < end:
        v = cond()
        if v:
            return v
        ctx.checkpoint()
        time.sleep(poll)
    return cond()


def _tree(root):
    """Every path under the extension root but the host's own bookkeeping."""
    out = []
    for base, dirs, files in os.walk(root):
        for n in dirs + files:
            rel = os.path.relpath(os.path.join(base, n), root)
            if rel not in ("state.json", "lock"):
                out.append(rel)
    return sorted(out)


EVENTS_MAX_STREAMS = 3                  # forgectrl's own cap (src/events.h)
EVENTS_HOST_HEADER = "X-ForgeFIRM-Client: extension-host"


def _stream(addr, host_client=False, keep=False, source=None):
    """One GET /events straight at forgectrl's read-only listener: (status,
    the words of a refusal, the socket). The socket is left open when keep,
    so the caller can hold a stream. `source` binds the address the
    connection comes from, because the cap counts peer addresses and the
    kernel would otherwise give every loopback connection 127.0.0.1."""
    import socket
    c = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    c.settimeout(8)
    try:
        if source:
            c.bind((source, 0))
        c.connect((addr, 80))
        req = "GET /events HTTP/1.0\r\nHost: %s\r\nAccept: text/event-stream\r\n%s\r\n" % (
            addr, EVENTS_HOST_HEADER + "\r\n" if host_client else "")
        c.sendall(req.encode())
        head = b""
        while b"\r\n\r\n" not in head and len(head) < 8192:
            k = c.recv(4096)
            if not k:
                break
            head += k
        top, _, rest = head.partition(b"\r\n\r\n")
        code = int(top.split()[1]) if top.split()[1:] else 0
        why = ""
        if code != 200:
            try:
                why = (json.loads(rest or b"{}") or {}).get("error") or ""
            except ValueError:
                why = rest.decode("utf-8", "replace")[:120]
        if not keep or code != 200:
            c.close()
            c = None
        return code, why, c
    except OSError as e:
        c.close()
        return 0, str(e), None


REF_UI = ("<!doctype html><title>forgetest reference</title>\n"
          "<p>the reference package's own page</p>\n")


def _pack_reference(work, lan, more_caps=(), ui=None):
    """The reference package, signed with a key made here: (archive, public key)."""
    import io
    import tarfile
    manifest = {"manifest": 1, "id": REF_ID, "name": "forgetest reference", "version": "1.0.0",
                "author": "forgetest", "license": "MIT", "api": "0.1", "runtime": "python",
                "service": {"exec": "bin/reference.py", "args": [lan or "-"]},
                "settings": {"threshold": {"type": "number", "default": 40, "min": 0, "max": 100},
                             "note": {"type": "string", "default": "", "max": 32, "label": "A note"},
                             "when": {"type": "choice", "default": "end",
                                      "choices": ["start", "end", "never"]}},
                "capabilities": ["net.outbound:%s:443" % REF_DEST, "storage:1", "machine.read",
                                 "settings.own", "camera.lid"] + list(more_caps)}
    payload = os.path.join(work, "payload.tar.gz")
    with tarfile.open(payload, "w:gz") as t:
        files = [("manifest.json", json.dumps(manifest), 0o644),
                 ("bin/reference.py", REF_SERVICE, 0o755)]
        if ui is None:
            ui = "ui" in (manifest.get("capabilities") or [])
        if ui:
            files.append(("ui/index.html", REF_UI, 0o644))
        for name, text, mode in files:
            info = tarfile.TarInfo(name)
            data = text.encode()
            info.size, info.mode = len(data), mode
            t.addfile(info, io.BytesIO(data))
    conf = os.path.join(work, "fwup.conf")
    _write(conf, 'meta-product = "ForgeFIRM extension"\nmeta-description = "%s"\nmeta-version = "1.0.0"\n'
                 'meta-platform = "forgefirm-ext"\nfile-resource payload.tar.gz {\n    host-path = "%s"\n}\n'
           % (REF_ID, payload))
    key = os.path.join(work, REF_KEY)
    raw, signed = os.path.join(work, "raw.ffx"), os.path.join(work, "reference.ffx")
    for cmd in ([FWUP, "-g", "-o", key], [FWUP, "-c", "-f", conf, "-o", raw],
                [FWUP, "-S", "-s", key + ".priv", "-i", raw, "-o", signed]):
        subprocess.run(cmd, check=True, capture_output=True, timeout=60, cwd=work)
    return signed, key + ".pub"


def _put_back(ctx, fc, work, prior, etag, raw, dir_mode):
    """Everything a test of the host put on the machine, taken away again:
    safe mode, the package, the owner key, the work directory, the
    setting, the data directory's mode, and the setup record (under a
    forgectrl restart)."""
    import shutil
    if os.path.exists(SAFE_FILE):
        os.remove(SAFE_FILE)
    st, reply = fc.post("/settings", data={"ext_enabled": "0"})
    r = _forgeext("remove", REF_ID)
    ctx.log("remove %s -> %s", REF_ID, "ok" if r.get("ok") else r.get("error"))
    owner_key = os.path.join(EXT_ROOT, "keys", REF_KEY + ".pub")
    if os.path.exists(owner_key):
        os.remove(owner_key)
    shutil.rmtree(work, ignore_errors=True)
    if prior == "1":
        st, reply = fc.post("/settings", data={"ext_enabled": "1", "advisory": etag, "phrase": SAFETY_PHRASE})
    elif prior == "":
        st, reply = fc.post("/settings", params={"ext_enabled": ""})
    ctx.log("restore ext_enabled=%r -> %s", prior, st)
    if prior != "1":
        os.chmod(os.path.dirname(EXT_ROOT), dir_mode)
    with ctx.takeover():
        write_file(record_path(), raw)
    ctx.log("the previous record is back under a restart")


def _as_found(ctx, fc, prior, raw, dir_mode, found_tree):
    ctx.check((fc.settings().get("ext_enabled") or "") == prior, "ext_enabled not restored: %r, was %r",
              fc.settings().get("ext_enabled"), prior)
    ctx.check(read_file(record_path()) == raw, "the setup record on disk is not the one found")
    ctx.check(prior == "1" or os.stat(os.path.dirname(EXT_ROOT)).st_mode & 0o7777 == dir_mode,
              "the data directory's mode is not the one found")
    left = _tree(EXT_ROOT)
    ctx.check(left == found_tree, "the extension root is not as found: %s", sorted(set(left) ^ set(found_tree)))
    ctx.check(len(_host_pids()) == 1, "the extension host is not running at the end")


@test("exthost.service", title="A package's service runs confined under the extension host",
      subsystem="exthost", kind="auto", hardware="takeover", est_min=5,
      covers=[("forgeext", "**"), ("forgectrl", "src/main.c"), ("forgectrl", "src/logs.*")],
      requires=["exthost.platform", "setup.extensions-consent"],
      description="The extension host is one running process (/usr/bin/forgeext run) and the status "
                  "file is its word, and with extensions off it runs nothing and says why. The test builds a reference "
                  "package on the board, signs it with a key it makes there, and adds that key as an owner "
                  "key: an install without the community consent is refused, with it the package is "
                  "installed at the community tier. It turns extensions on over the advisory. The service "
                  "then runs as its pool account with no_new_privs and a seccomp filter, in its own group "
                  "under /sys/fs/cgroup/ffx with the limits of a service (25 percent of the core, 48 MiB, 32 "
                  "processes), with its chain in the rule table. From the inside it reads /etc and its "
                  "package, writes its data directory and nothing else, cannot read the settings file, the "
                  "setup record, or the pulse device, is refused by the machine on loopback and on its LAN "
                  "address even on the port it declared, cannot connect on a port it did not declare, and "
                  "cannot open a netlink socket. Its one way to the machine is its API socket (root's and its "
                  "account's, 0660, refused to another pool account): GET /v0/self names it and what it may use, "
                  "GET /v0/machine/mode is forgectrl's answer relayed, POST /v0/camera gives it a JPEG from the "
                  "lid camera it holds (with the camera's lamp at 0 and at 1023 two different pictures, the lit "
                  "one the larger) and refuses the head camera and a lamp past its range, a hold it was not granted is 403, a path "
                  "the API does not have 404, a path with .. 400; its output is in the forgeext log under its id. Safe mode "
                  "stops it and its end starts it again. A host killed outright takes its services with it "
                  "at once (the init wrapper), comes back, and starts the service again. ext_enabled=0 "
                  "stops it and leaves no group and no chain. The package, the key, the setting, and the "
                  "setup record are put back as found, the record under a forgectrl restart. The layer "
                  "content (the recipe, the init script's install, the image list) is in the platform "
                  "identity of every fingerprint.")
def service(ctx):
    import shutil
    import tempfile
    fc = ctx.forgectrl
    ev = ctx.evidence
    ctx.check(os.path.isfile(FORGEEXT) and os.access("/etc/init.d/forgeext", os.X_OK),
              "the image has no forgeext, or no init script for it")
    hosts = _host_pids()
    ev["host_pids"] = hosts
    ctx.check(len(hosts) == 1, "the extension host is not one running process: %s", hosts)
    ctx.check(_until(ctx, lambda: _host_status().get("pid") == hosts[0], 10),
              "the status file %s is not the running host's: %s", HOST_STATUS, _host_status().get("pid"))
    ctx.check(fc.wait_idle(timeout=30, abort=ctx.aborted), "machine not idle: settings are locked")
    prior = fc.settings().get("ext_enabled") or ""
    raw = read_file(record_path())
    ctx.check(raw, "no setup record at %s", record_path())
    ctx.check(not os.path.exists(SAFE_FILE), "%s exists: the machine is in safe mode", SAFE_FILE)
    ctx.check(REF_ID not in [x.get("id") for x in _forgeext("list").get("packages", [])],
              "%s is already installed", REF_ID)
    found_tree = _tree(EXT_ROOT)
    data_dir = os.path.dirname(EXT_ROOT)
    dir_mode = os.stat(data_dir).st_mode & 0o7777
    ev["data_dir_mode_found"] = "%04o" % dir_mode
    if prior != "1":
        st = _host_status()
        ev["off"] = {k: st.get(k) for k in ("enabled", "off_reason")}
        ctx.check(st.get("enabled") is False and "ext_enabled" in (st.get("off_reason") or "")
                  and not [x for x in st.get("services", []) if x.get("state") == "running"],
                  "with extensions off the host does not say so: %s", ev["off"])

    st, body, hdrs = request(fc.base, "GET", "/advisories/extensions", headers={"Host": fc.host_header()})
    etag = hdrs.get("etag")
    ctx.check(st == 200 and etag, "GET /advisories/extensions -> %s", st)
    work = tempfile.mkdtemp(prefix="forgetest-ffx.")
    owner_key = os.path.join(EXT_ROOT, "keys", REF_KEY + ".pub")
    uid = None

    def cg(name):
        return _read("%s/%s/%s" % (POOL_GROUP, REF_ID, name)).strip()

    def chains():
        return subprocess.run([NFT, "list", "table", "inet", "ffx"], capture_output=True, text=True, timeout=30).stdout

    def running(other_than=0):
        # The status file outlives a killed host: a pid that is the old one is no news.
        x = _svc(REF_ID)
        return x if x.get("state") == "running" and x.get("pid") and x["pid"] != other_than             and os.path.exists("/proc/%d" % x["pid"]) else None

    try:
        lan = lan_ip()
        archive, pub = _pack_reference(work, lan)
        r = _forgeext("inspect", archive)
        ev["inspect_without_the_key"] = r.get("tier")
        ctx.check(r.get("ok") and r.get("tier") == "unverified", "before its key is the owner's the package reads %s", r)
        shutil.copy(pub, owner_key)
        os.chmod(owner_key, 0o644)
        r = _forgeext("inspect", archive)
        ctx.check(r.get("ok") and r.get("tier") == "community", "with the owner's key the package reads %s", r)
        r = _forgeext("install", archive)
        ev["install_without_consent"] = r.get("error")
        ctx.log("install without the consent -> %s", r.get("error"))
        ctx.check(r.get("ok") is False, "a community package was installed without the consent")
        r = _forgeext("install", archive, "--consent-community")
        ctx.check(r.get("ok") is True, "the install -> %s", r.get("error"))
        ctx.check(_forgeext("check", REF_ID).get("ok") is True, "the installed tree fails its integrity check")

        st, reply = fc.post("/settings", data={"ext_enabled": "1", "advisory": etag, "phrase": SAFETY_PHRASE})
        ctx.check(st == 200, "ext_enabled=1 over the advisory -> %s %r", st, reply)
        x = _until(ctx, running, 90, poll=0.5)
        ev["status_at_start"] = _host_status()
        ctx.check(x, "the service is not running: %s", {k: ev["status_at_start"].get(k) for k in ("enabled", "off_reason", "not_ready")}
                  if not _svc(REF_ID) else _svc(REF_ID))
        pid = x["pid"]
        uid = POOL_FIRST + int(x["account"][3:])
        proc = _read("/proc/%d/status" % pid)
        ev["service"] = {"pid": pid, "account": x["account"], "cgroup": _read("/proc/%d/cgroup" % pid).strip(),
                         "cpu.max": cg("cpu.max"), "memory.max": cg("memory.max"), "pids.max": cg("pids.max"),
                         "memory.current": cg("memory.current")}
        ctx.log("the service: %s", ev["service"])
        ctx.check(("Uid:\t%d\t%d\t%d\t%d" % ((uid,) * 4)) in proc and ("Gid:\t%d\t%d\t%d\t%d" % ((uid,) * 4)) in proc,
                  "the service does not run as %s alone", x["account"])
        ctx.check("NoNewPrivs:\t1" in proc and "Seccomp:\t2" in proc, "no no_new_privs or no seccomp filter on the service")
        ctx.check(ev["service"]["cgroup"] == "0::/ffx/" + REF_ID, "the service's group is %s", ev["service"]["cgroup"])
        ctx.check(cg("cpu.max").split() == ["25000", "100000"] and cg("memory.max") == str(48 << 20) and cg("pids.max") == "32",
                  "the group's limits are not a service's: %s", ev["service"])
        table = chains()
        ctx.check(("chain u%d " % uid) in table and REF_DEST in table, "the service's chain is not in the rule table")

        report_path = os.path.join(EXT_ROOT, "data", REF_ID, "report.json")
        ctx.check(_until(ctx, lambda: os.path.isfile(report_path), 60, poll=0.5), "the service left no report")
        rep = json.loads(_read(report_path))
        ev["from_the_inside"] = rep
        ctx.log("from the inside: %s", rep)
        want = {"uid": uid, "gid": uid, "groups": [], "read_etc": "ok", "read_pkg": "ok", "write_data": "ok",
                "read_settings": "EACCES", "read_setup": "EACCES", "write_pkg": "EACCES", "write_tmp": "EACCES",
                "pulse_device": "EACCES", "loopback_declared_port": "ECONNREFUSED",
                "loopback_undeclared_port": "EACCES", "unix_socket": "ok", "netlink_socket": "EPERM"}
        if lan:
            want["lan_declared_port"] = "ECONNREFUSED"
        for k, v in want.items():
            ctx.check(rep.get(k) == v, "from the inside, %s is %r, expected %r", k, rep.get(k), v)
        # LC_CTYPE is the interpreter's own doing (it coerces the C locale at its start)
        fixed = {"PATH", "LANG", "HOME", "TMPDIR", "FFX_ID", "FFX_PKG", "FFX_DATA", "FFX_API", "PYTHONDONTWRITEBYTECODE",
                 "PYTHONUNBUFFERED"}
        ctx.check(rep.get("cwd") == os.path.join(EXT_ROOT, "data", REF_ID) and set(rep.get("env") or []) - {"LC_CTYPE"} == fixed,
                  "its working directory or its environment is not the fixed one: %s %s", rep.get("cwd"), rep.get("env"))
        # its one way to the machine: the API socket, and the broker behind it
        me = rep.get("api_self") or [0, {}]
        ev["api"] = {k: rep.get(k) for k in ("api_self", "api_mode", "api_hold", "api_nowhere", "api_traversal")}
        ctx.check(me[0] == 200 and me[1].get("id") == REF_ID and me[1].get("version") == "1.0.0"
                  and sorted(me[1].get("capabilities") or []) == sorted(["net.outbound:%s:443" % REF_DEST, "storage:1",
                                                                          "machine.read", "settings.own",
                                                                          "camera.lid"]),
                  "GET /v0/self from the inside: %s", me)

        # a camera: the one it was granted, and not the one it was not
        cam = rep.get("api_camera") or [0, "", False, False]
        ev["api_camera"] = {"status": cam[0], "bytes": cam[1] if cam[0] == 200 else None,
                            "jpeg": cam[2], "image_type": cam[3],
                            "why": cam[1] if cam[0] != 200 else None}
        ctx.log("the lid camera, from inside the sandbox: %s", ev["api_camera"])
        ctx.check(cam[0] == 200 and cam[2] and cam[3] and isinstance(cam[1], int) and cam[1] > 4096,
                  "the granted camera did not come back as a JPEG: %s", ev["api_camera"])
        head_ = rep.get("api_camera_head") or [0, ""]
        ev["api_camera_head"] = head_[:2]
        ctx.check(head_[0] == 403, "the head camera, which it was not granted -> %s", head_[:2])
        badcam = rep.get("api_camera_bad") or [0, ""]
        ev["api_camera_bad"] = badcam[:2]
        ctx.check(badcam[0] == 400, "a camera there is none of -> %s", badcam[:2])
        # The camera's own lamp for one frame: the machine lights it for
        # the shot and puts its level back, so a frame with the lamp off
        # and one with it full are two different pictures, the lit one the
        # larger, and a level past the range is refused before the machine
        # is asked.
        dark = rep.get("api_camera_dark") or [0, "", False, False]
        lit = rep.get("api_camera_lit") or [0, "", False, False]
        ev["api_camera_lamp"] = {"dark": dark[:3], "lit": lit[:3]}
        ctx.log("the lid camera with its lamp at 0 and at 1023: %s", ev["api_camera_lamp"])
        ctx.check(dark[0] == 200 and dark[2] and lit[0] == 200 and lit[2],
                  "a frame with the lamp named did not come back as a JPEG: %s", ev["api_camera_lamp"])
        ctx.check(isinstance(lit[1], int) and isinstance(dark[1], int) and lit[1] > dark[1],
                  "the lamp did not reach the camera: %d bytes at 1023, %d at 0", lit[1] or 0, dark[1] or 0)
        lampbad = rep.get("api_camera_lamp_bad") or [0, ""]
        ev["api_camera_lamp_bad"] = lampbad[:2]
        ctx.check(lampbad[0] == 400 and "lamp" in str(lampbad[1]), "a lamp past its range -> %s", lampbad[:2])

        # A package's capture yields to somebody watching a camera: it is
        # never the person standing at the machine.
        import urllib.request
        shot_file = os.path.join(EXT_ROOT, "data", REF_ID, "shot")
        if os.path.exists(shot_file):
            os.remove(shot_file)
        held = urllib.request.urlopen(
            urllib.request.Request(fc.base + "/cam/stream", headers={"Host": fc.host_header()}), timeout=15)
        try:
            held.read(8192)
            watching = _until(ctx, lambda: (fc.get("/cam/status")[1] or {}).get("clients"), 15, poll=0.5)
            ctx.check(watching, "no viewer was counted while the stream was held")
            _write(os.path.join(EXT_ROOT, "data", REF_ID, "shoot"), "now")
            yielded = _until(ctx, lambda: json.loads(_read(shot_file)) if os.path.exists(shot_file) else None,
                             30, poll=0.5)
            ev["camera_while_watched"] = yielded
            ctx.log("the package's capture while a viewer watches: %s", yielded)
            ctx.check(yielded and yielded[0] == 409,
                      "a package's capture did not yield to a viewer: %s", yielded)
            ctx.check(yielded and "watching" in str(yielded[1]),
                      "it yielded without the machine's own words: %s", yielded)
        finally:
            held.close()
        os.remove(shot_file)
        _until(ctx, lambda: not (fc.get("/cam/status")[1] or {}).get("clients"), 15, poll=0.5)
        _write(os.path.join(EXT_ROOT, "data", REF_ID, "shoot"), "now")
        again = _until(ctx, lambda: json.loads(_read(shot_file)) if os.path.exists(shot_file) else None,
                       40, poll=0.5)
        ev["camera_after_watching"] = [again[0], again[1]] if again else None
        ctx.check(again and again[0] == 200 and again[2],
                  "the package's capture did not come back once the viewer stopped: %s", again)

        # its own settings: read, set, and what the schema will not take
        got = rep.get("api_settings") or [0, {}]
        ev["api_settings"] = got
        ctx.check(got[0] == 200 and (got[1] or {}).get("settings", {}).get("threshold") == 40
                  and (got[1] or {}).get("settings", {}).get("when") == "end"
                  and len((got[1] or {}).get("schema") or []) == 3,
                  "GET /v0/settings before anything is set, with its schema: %s", got)
        set_ = rep.get("api_settings_set") or [0, {}]
        ev["api_settings_set"] = set_
        ctx.check(set_[0] == 200 and (set_[1] or {}).get("settings", {}).get("threshold") == 70
                  and (set_[1] or {}).get("settings", {}).get("note") == "set from inside"
                  and (set_[1] or {}).get("settings", {}).get("when") == "end",
                  "POST /v0/settings sets what it names and leaves the rest: %s", set_)
        for key, why in (("api_settings_undeclared", "a setting the package does not declare"),
                         ("api_settings_out_of_bounds", "a value outside its bounds")):
            bad_ = rep.get(key) or [0, {}]
            ev[key] = bad_
            ctx.check(bad_[0] == 400 and (bad_[1] or {}).get("error"), "%s -> %s", why, bad_)
        store = "%s/settings/%s.json" % (EXT_ROOT, REF_ID)
        ev["settings_store"] = {"mode": "%04o" % (os.stat(store).st_mode & 0o7777) if os.path.exists(store) else None}
        ctx.check(os.path.exists(store) and os.stat(store).st_mode & 0o7777 == 0o600,
                  "the settings store is not root's alone: %s", ev["settings_store"])
        ctx.check(os.path.exists(store) and json.loads(_read(store)).get("threshold") == 70,
                  "the store does not hold what the package set")
        mode_ = rep.get("api_mode") or [0, {}]
        ctx.check(mode_[0] == 200 and mode_[1].get("mode") in ("grbl", "cloud") and "controller" in mode_[1],
                  "GET /v0/machine/mode from the inside is not forgectrl's answer: %s", mode_)
        ctx.check((rep.get("api_hold") or [0])[0] == 403 and (rep.get("api_nowhere") or [0])[0] == 404
                  and (rep.get("api_traversal") or [0])[0] == 400,
                  "a hold it was not granted, a path the API does not have, a path with ..: %s %s %s",
                  rep.get("api_hold"), rep.get("api_nowhere"), rep.get("api_traversal"))
        # the storage quota: the host measures what it holds and sets it aside
        big = os.path.join(EXT_ROOT, "data", REF_ID, "toobig")
        with open(big, "wb") as f:
            f.write(b"x" * (2 << 20))
        ctx.log("wrote 2 MiB into its data directory (it declares storage:1)")

        def quarantined():
            x = _svc(REF_ID)
            return x if x.get("state") == "quarantined" else None

        q = _until(ctx, quarantined, 90, poll=1.0)
        ev["quota"] = {"state": (q or _svc(REF_ID)).get("state"), "reason": (q or _svc(REF_ID)).get("reason")}
        ctx.log("over its quota: %s", ev["quota"])
        ctx.check(q, "a service 2 MiB over its 1 MiB quota was not set aside: %s", _svc(REF_ID))
        ctx.check("may hold" in ((q or {}).get("reason") or ""),
                  "it was set aside without the quota's reason: %s", (q or {}).get("reason"))
        ctx.check(not os.path.exists("/proc/%d" % pid), "it is still running after being set aside")
        # The operator's way out: clear the data, enable it again.
        os.remove(big)
        r = _forgeext("enable", REF_ID)
        ctx.check(r.get("ok") is True, "enable after the quota -> %s", r.get("error"))
        back = _until(ctx, lambda: _svc(REF_ID) if _svc(REF_ID).get("state") == "running" else None, 90, poll=0.5)
        ctx.check(back, "it did not come back once its data was cleared and it was enabled: %s", _svc(REF_ID))
        pid = (back or {}).get("pid") or pid

        sock = "%s/%s.sock" % (API_DIR, REF_ID)
        st_ = os.stat(sock)
        ctx.check(stat.S_ISSOCK(st_.st_mode) and stat.S_IMODE(st_.st_mode) == 0o660 and (st_.st_uid, st_.st_gid) == (0, uid),
                  "its API socket is not root's and its account's at 0660: %o %d:%d", stat.S_IMODE(st_.st_mode), st_.st_uid, st_.st_gid)
        other = POOL_FIRST + POOL_SIZE - 1 if uid != POOL_FIRST + POOL_SIZE - 1 else POOL_FIRST
        rc, lines, errtext = _probe("-", other, "unix", sock)
        ev["another_account_at_its_socket"] = lines
        ctx.check(lines and lines[0].get("result") == "eacces", "another pool account at its socket: %s %s", lines, errtext)
        ctx.check(_until(ctx, lambda: ("ext %s: reference service up" % REF_ID) in _read(HOST_LOG), 20, poll=1),
                  "the service's output is not in %s under its id", HOST_LOG)

        _write(SAFE_FILE, "")
        ctx.check(_until(ctx, lambda: not running() and "safe mode" in (_host_status().get("off_reason") or ""), 15),
                  "safe mode did not stop the service: %s", _host_status())
        ctx.check(not os.path.exists("/proc/%d" % pid) and not os.path.isdir("%s/%s" % (POOL_GROUP, REF_ID)),
                  "safe mode left the process or its group")
        os.remove(SAFE_FILE)
        x = _until(ctx, lambda: running(pid), 60, poll=0.5)
        ctx.check(x, "out of safe mode the service did not start again: %s", _svc(REF_ID))
        pid = x["pid"]
        ctx.log("safe mode stopped it and its end started it again (pid %d)", pid)

        host = _host_pids()
        ctx.check(len(host) == 1, "the extension host is not one process: %s", host)
        # Only once the service is in its quiet loop: a service still on its way up ends by
        # itself when its first line meets the dead host's pipe, and that would prove nothing.
        beat_path = os.path.join(EXT_ROOT, "data", REF_ID, "beat")
        seen = set()
        ctx.check(_until(ctx, lambda: seen.add(_read(beat_path)) or len(seen) >= 3, 30, poll=0.2),
                  "the service's heartbeat does not advance")
        t0 = time.time()
        os.kill(host[0], signal.SIGKILL)
        ctx.check(_until(ctx, lambda: not os.path.exists("/proc/%d" % pid), 2, poll=0.05),
                  "a killed host left its service running for 2 s: nobody would freeze it in an armed window")
        ev["service_outlived_a_killed_host_s"] = round(time.time() - t0, 2)
        x = _until(ctx, lambda: running(pid) if _host_status().get("pid") in _host_pids() else None, 60, poll=0.5)
        ev["host_back_s"] = round(time.time() - t0, 1)
        ctx.check(x, "the host did not come back and start the service again: %s", _host_status())
        pid = x["pid"]
        ctx.log("a killed host: its service gone in %.2f s, the host back and the service running after %.1f s",
                ev["service_outlived_a_killed_host_s"], ev["host_back_s"])

        st, reply = fc.post("/settings", data={"ext_enabled": "0"})
        ctx.check(st == 200, "ext_enabled=0 -> %s %r", st, reply)
        ctx.check(_until(ctx, lambda: not running() and not os.path.exists("/proc/%d" % pid), 15),
                  "ext_enabled=0 did not stop the service: %s", _svc(REF_ID))
        ctx.check(not os.path.isdir("%s/%s" % (POOL_GROUP, REF_ID)) and ("chain u%d " % uid) not in chains(),
                  "ext_enabled=0 left the service's group or its chain")
        ctx.check("ext_enabled" in (_host_status().get("off_reason") or ""), "the host does not say why nothing runs: %s",
                  _host_status().get("off_reason"))
    finally:
        _put_back(ctx, fc, work, prior, etag, raw, dir_mode)
    _as_found(ctx, fc, prior, raw, dir_mode, found_tree)


# ------------------------------------------------- the armed window

def _frozen(id_):
    ev = _read("%s/%s/cgroup.events" % (POOL_GROUP, id_))
    return "frozen 1" in ev if ev else None


@test("exthost.armed-freeze", title="A package's service is frozen for the armed window",
      subsystem="exthost", kind="operator", hardware="takeover", est_min=6,
      covers=[("forgeext", "src/super.*"), ("forgeext", "src/run.*"), ("forgeext", "src/machine.*"),
              ("forgeext", "src/cgroup.*"), ("forgectrl", "src/cool.*"), ("forgectrl", "src/cam.*"),
              ("forgectrl", "src/main.c")],
      requires=["exthost.service", "cloud.dark-print"], actions=["button"],
      steps=[OFFLINE_STEP,
             "Bed clear (the job is dark: a 30 s square at S0, nothing fires). Press the button "
             "when it lights."],
      description="The reference package runs, its heartbeat advancing twice a second, and a dark "
                  "cloud print (cloud.dark-print's own job and checks) opens a real armed window over "
                  "it. Sampled five times a second from before the button to after the end: the "
                  "engine's armed flag, the group's frozen state as the kernel reports it, and the "
                  "heartbeat. The window is open for at least 10 s. From 2 s after it opens to its "
                  "close the group reads frozen in every sample and the heartbeat does not move, the "
                  "freeze is in place before the run starts (the latch unlocks only for the run), and "
                  "within 3 s of the close the group is thawed and the heartbeat advances again. The "
                  "service is the same process throughout. A background capture is refused in words "
                  "while the window is open and served again once it closes, which is the other half "
                  "of keeping a package off the step stream: a capture costs kernel-side work that a "
                  "thread priority does not cover. Everything is put back as exthost.service puts it "
                  "back.")
def armed_freeze(ctx):
    import tempfile
    import threading
    fc = ctx.forgectrl
    ev = ctx.evidence
    ctx.check(len(_host_pids()) == 1, "the extension host is not one running process: %s", _host_pids())
    ctx.check(fc.wait_idle(timeout=30, abort=ctx.aborted), "machine not idle: settings are locked")
    prior = fc.settings().get("ext_enabled") or ""
    raw = read_file(record_path())
    ctx.check(raw, "no setup record at %s", record_path())
    ctx.check(not os.path.exists(SAFE_FILE), "%s exists: the machine is in safe mode", SAFE_FILE)
    found_tree = _tree(EXT_ROOT)
    dir_mode = os.stat(os.path.dirname(EXT_ROOT)).st_mode & 0o7777
    st, body, hdrs = request(fc.base, "GET", "/advisories/extensions", headers={"Host": fc.host_header()})
    etag = hdrs.get("etag")
    ctx.check(st == 200 and etag, "GET /advisories/extensions -> %s", st)
    work = tempfile.mkdtemp(prefix="forgetest-ffx.")
    beat_path = os.path.join(EXT_ROOT, "data", REF_ID, "beat")
    samples = []
    probe = {}
    stop = threading.Event()

    def sampler():
        while not stop.is_set():
            st_, cool = fc.get("/cool/status")
            armed = bool(cool.get("armed")) if st_ == 200 and isinstance(cool, dict) else None
            samples.append((time.time(), armed, _frozen(REF_ID), _read(beat_path).strip(),
                            (_svc(REF_ID) or {}).get("pid"), latch_locked()))
            # One background capture from inside the window. The refusal is
            # decided before any frame is taken, so it costs the cut nothing;
            # if it were served instead, that is what this case is here to
            # catch, and the capture is the one the rule forbids.
            if armed and "in" not in probe:
                probe["in"] = fc.get("/cam/snapshot", params={"cam": "lid", "background": "1"}, raw=True)
            time.sleep(0.2)

    try:
        archive, pub = _pack_reference(work, lan_ip())
        with open(pub, "rb") as f:
            write_file(os.path.join(EXT_ROOT, "keys", REF_KEY + ".pub"), f.read())
        os.chmod(os.path.join(EXT_ROOT, "keys", REF_KEY + ".pub"), 0o644)
        r = _forgeext("install", archive, "--consent-community")
        ctx.check(r.get("ok") is True, "the install -> %s", r.get("error"))
        st, reply = fc.post("/settings", data={"ext_enabled": "1", "advisory": etag, "phrase": SAFETY_PHRASE})
        ctx.check(st == 200, "ext_enabled=1 over the advisory -> %s %r", st, reply)
        seen = set()
        ctx.check(_until(ctx, lambda: seen.add(_read(beat_path)) or len(seen) >= 4, 120, poll=0.2),
                  "the reference service's heartbeat does not advance: %s", _svc(REF_ID) or _host_status())
        pid = _svc(REF_ID).get("pid")

        offset = enter_offline(ctx)
        job = offline_job(ctx, "dark.puls", seconds=30)
        off = Offline().__enter__()
        t = threading.Thread(target=sampler, daemon=True)
        t.start()
        try:
            dark_print_body(ctx, ev, off, job, offset, fc)
            ctx.sleep(6)                                # past the close, for the thaw
        finally:
            stop.set()
            t.join(timeout=5)
            off.__exit__(None, None, None)
            offline_cleanup(ctx)

        opens = [x for x in samples if x[1]]
        ctx.check(opens, "the engine never read armed across the print: %d samples", len(samples))
        t_open, t_close = opens[0][0], opens[-1][0]
        ev["window_s"] = round(t_close - t_open, 1)
        ctx.check(t_close - t_open >= 10, "the armed window was open for %.1f s only", t_close - t_open)
        inside = [x for x in samples if t_open + 2.0 <= x[0] <= t_close]
        thawed = [x for x in inside if x[2] is not True]
        ev["samples"] = {"all": len(samples), "inside": len(inside), "not_frozen_inside": len(thawed)}
        first_frozen = next((x[0] for x in samples if x[0] >= t_open and x[2] is True), None)
        ev["freeze_lag_s"] = round(first_frozen - t_open, 2) if first_frozen else None
        ctx.check(inside and not thawed, "inside the armed window the group read not frozen in %d of %d samples "
                  "(the first %.1f s after it opened)", len(thawed), len(inside), (thawed[0][0] - t_open) if thawed else 0)
        beats = {x[3] for x in inside}
        ctx.check(len(beats) == 1, "the heartbeat moved inside the armed window: %s", sorted(beats)[:6])
        # the latch unlocks only for the run: its first unlocked sample is the run's start
        run_start = next((x[0] for x in samples if x[5] is False), None)
        ctx.check(run_start is not None, "the latch never read unlocked: the print did not run under the samples")
        ctx.check(first_frozen is not None and first_frozen <= run_start,
                  "the run started before the freeze was in place (frozen %s, the latch unlocked %s after the window opened)",
                  None if first_frozen is None else round(first_frozen - t_open, 2), round(run_start - t_open, 2))
        ev["freeze_led_the_run_s"] = round(run_start - first_frozen, 2)
        after = [x for x in samples if x[0] >= t_close + 3.0]
        ctx.check(after and all(x[2] is False for x in after), "3 s after the close the group is not thawed: %s",
                  [x[2] for x in after][:8])
        ctx.check(len({x[3] for x in after}) >= 2, "the heartbeat did not advance again after the close")
        last_frozen = max((x[0] for x in samples if x[2] is True), default=None)
        ev["thaw_lag_s"] = round(last_frozen - t_close, 2) if last_frozen else None
        ctx.check({x[4] for x in samples if x[4]} == {pid}, "the service did not stay the same process: %s",
                  sorted({x[4] for x in samples if x[4]}))

        # The camera, inside the window and after it.
        ctx.check("in" in probe, "no background capture was tried inside the armed window")
        cam_st, cam_body = probe.get("in", (None, b""))
        words = cam_body.decode("utf-8", "replace")[:200] if isinstance(cam_body, bytes) else str(cam_body)[:200]
        ev["camera_in_window"] = {"status": cam_st, "said": words}
        ctx.check(cam_st == 409, "a background capture inside the armed window -> %s (%d bytes)", cam_st,
                  len(cam_body or b""))
        ctx.check("armed" in words, "the refusal does not say why: %s", words)
        st, after_body = fc.get("/cam/snapshot", params={"cam": "lid", "background": "1"}, raw=True)
        ev["camera_after_window"] = {"status": st, "bytes": len(after_body or b"")}
        ctx.check(st == 200 and (after_body or b"")[:2].hex() == "ffd8",
                  "a background capture after the window -> %s (%d bytes)", st, len(after_body or b""))
        ctx.log("a background capture inside the window -> %s (%s); after it -> %s, %d bytes", cam_st, words, st,
                len(after_body or b""))
        ctx.log("the armed window was open %.1f s; frozen %.2f s after it opened and %.2f s before the latch unlocked "
                "for the run, in every one of %d samples from 2 s in to the close, the heartbeat still; thawed %.2f s "
                "after the close", ev["window_s"], ev["freeze_lag_s"], ev["freeze_led_the_run_s"], len(inside),
                ev["thaw_lag_s"] if ev["thaw_lag_s"] is not None else -1)
    finally:
        _put_back(ctx, fc, work, prior, etag, raw, dir_mode)
    _as_found(ctx, fc, prior, raw, dir_mode, found_tree)


# ------------------------------------------------------------- the hold

HOLDS_DIR = "/run/forgefirm/holds"
REQUIRED_HOLDS = EXT_ROOT + "/required-holds"


@test("exthost.hold-pause-tier", title="A package's hold withholds fire, and the operator's exits end it",
      subsystem="exthost", kind="auto", hardware="takeover", est_min=7,
      covers=[("forgeext", "src/holdkeep.*"), ("forgeext", "src/run.*"), ("forgeext", "src/install.*"),
              ("forgeext", "src/state.*"), ("forgeext", "src/main.c"), ("forgectrl", "src/holds.*"),
              ("forgectrl", "src/cool.*")],
      requires=["exthost.service"],
      description="The reference package is installed with the hold grant and its hold is marked required "
                  "(the package is then named under required-holds for forgectrl). Until its service has "
                  "run healthy (60 s) the required hold stands in the host's words; from then on the host "
                  "keeps its hold file fresh and clear and the engine's verdict is OK. The package then raises "
                  "its hold itself over its API socket (POST /v0/hold): EXT with the package's own words, "
                  "words the form does not take refused with the hold unchanged, and cleared again. The test then "
                  "makes the service end at every start: the host raises the hold in its own words and GET "
                  "/cool/status reads verdict EXT, fire_ok false, hold true, with the reason naming the "
                  "package. Safe mode ends the hold within two ticks and leaving it brings the hold back; "
                  "marked advisory the hold is dropped and the verdict is OK; marked required again, with "
                  "the host suspended the file goes stale and the reason becomes that the host is not "
                  "answering, and resumed it is the package's again; ext_enabled=0 ends it. Nothing moves "
                  "and nothing fires: the verdict is read at idle. Everything is put back as "
                  "exthost.service puts it back, and the required holds are empty at the end.")
def hold_pause_tier(ctx):
    import tempfile
    fc = ctx.forgectrl
    ev = ctx.evidence
    ctx.check(len(_host_pids()) == 1, "the extension host is not one running process: %s", _host_pids())
    ctx.check(fc.wait_idle(timeout=30, abort=ctx.aborted), "machine not idle: settings are locked")
    prior = fc.settings().get("ext_enabled") or ""
    raw = read_file(record_path())
    ctx.check(raw, "no setup record at %s", record_path())
    ctx.check(not os.path.exists(SAFE_FILE), "%s exists: the machine is in safe mode", SAFE_FILE)
    found_tree = _tree(EXT_ROOT)
    dir_mode = os.stat(os.path.dirname(EXT_ROOT)).st_mode & 0o7777
    st, body, hdrs = request(fc.base, "GET", "/advisories/extensions", headers={"Host": fc.host_header()})
    etag = hdrs.get("etag")
    ctx.check(st == 200 and etag, "GET /advisories/extensions -> %s", st)

    def cool():
        st_, c = fc.get("/cool/status")
        return c if st_ == 200 and isinstance(c, dict) else {}

    def verdict_is(name, seconds, reason=None):
        def ok():
            c = cool()
            return c if c.get("verdict") == name and (reason is None or reason in (c.get("reason") or "")) else None
        return _until(ctx, ok, seconds, poll=0.25)

    def hold_file():
        try:
            h = json.loads(_read("%s/%s.json" % (HOLDS_DIR, REF_ID)))
        except ValueError:
            return None
        h["age"] = time.monotonic() - h["ts_mono"]
        return h

    start = cool()
    ev["verdict_at_start"] = {k: start.get(k) for k in ("verdict", "fire_ok", "hold", "reason")}
    ctx.check(start.get("verdict") == "OK", "the engine's own verdict %r stands above a hold: the test needs OK",
              start.get("verdict"))
    work = tempfile.mkdtemp(prefix="forgetest-ffx.")
    stop_path = os.path.join(EXT_ROOT, "data", REF_ID, "stop")
    why_not_running = REF_ID + ": the extension is not running"
    try:
        archive, pub = _pack_reference(work, lan_ip(), more_caps=["hold"])
        with open(pub, "rb") as f:
            write_file(os.path.join(EXT_ROOT, "keys", REF_KEY + ".pub"), f.read())
        os.chmod(os.path.join(EXT_ROOT, "keys", REF_KEY + ".pub"), 0o644)
        r = _forgeext("install", archive, "--consent-community")
        ev["install_without_the_grant"] = r.get("error")
        ctx.check(r.get("ok") is False and "hold" in (r.get("error") or ""), "a hold was installed without the operator's grant: %s", r)
        r = _forgeext("install", archive, "--consent-community", "--grant", "hold")
        ctx.check(r.get("ok") is True, "the install -> %s", r.get("error"))
        ctx.check(not os.listdir(REQUIRED_HOLDS), "a granted hold is required before the operator marks it: %s",
                  os.listdir(REQUIRED_HOLDS))
        r = _forgeext("hold", REF_ID, "required")
        ctx.check(r.get("ok") is True and os.listdir(REQUIRED_HOLDS) == [REF_ID], "marked required -> %s, named %s",
                  r.get("error"), os.listdir(REQUIRED_HOLDS))

        st, reply = fc.post("/settings", data={"ext_enabled": "1", "advisory": etag, "phrase": SAFETY_PHRASE})
        ctx.check(st == 200, "ext_enabled=1 over the advisory -> %s %r", st, reply)
        beat_path = os.path.join(EXT_ROOT, "data", REF_ID, "beat")
        seen = set()
        ctx.check(_until(ctx, lambda: seen.add(_read(beat_path)) or len(seen) >= 3, 120, poll=0.2),
                  "the reference service's heartbeat does not advance: %s", _svc(REF_ID) or _host_status())
        h = hold_file()
        ev["running"] = {"hold_file": h, "verdict": cool().get("verdict"), "status_hold": _svc(REF_ID).get("hold")}
        ctx.check(h and h["required"] is True and h["age"] < 1.5, "a running package's required hold is not fresh: %s", h)
        # a required hold stands until its package has run healthy (60 s): one that ends at every
        # start reads as running for a moment each time, and must not flicker clear
        ctx.check(cool().get("verdict") == "EXT" and "only just started" in (cool().get("reason") or ""),
                  "a required hold is clear before its package has run healthy: %s", cool())
        ctx.check(verdict_is("OK", 100), "with the package running healthy the verdict is %s (%s)", cool().get("verdict"),
                  cool().get("reason"))
        h = hold_file()
        ctx.check(h and h["raised"] is False and h["age"] < 1.5, "healthy: the hold file is not fresh and clear: %s", h)
        ctx.check(_svc(REF_ID).get("hold") == "required", "the status file does not say the hold is required: %s", _svc(REF_ID))

        # the package's own word, over its API socket: raised in its words, and cleared
        def tell(words):
            said = os.path.join(EXT_ROOT, "data", REF_ID, "said")
            if os.path.exists(said):
                os.remove(said)
            _write(os.path.join(EXT_ROOT, "data", REF_ID, "say"), json.dumps(words))
            ctx.check(_until(ctx, lambda: os.path.exists(said), 10), "the service did not pass %s on", words)
            return json.loads(_read(said))

        r = tell({"raised": True, "reason": "no badge presented"})
        ctx.check(r[0] == 200 and r[1] == {"raised": True, "reason": "no badge presented"}, "POST /v0/hold raise -> %s", r)
        t0 = time.time()
        c = verdict_is("EXT", 6, REF_ID + ": no badge presented")
        ev["raised_by_the_package"] = {"after_s": round(time.time() - t0, 1), "cool": {k: (c or cool()).get(k) for k in ("verdict", "fire_ok", "hold", "reason")}}
        ctx.log("the package raised its hold: %s", ev["raised_by_the_package"])
        ctx.check(c and c.get("fire_ok") is False and c.get("hold") is True, "the package's own hold does not withhold fire: %s", c or cool())
        r = tell({"raised": True, "reason": "a \"quote\""})
        ctx.check(r[0] == 400 and verdict_is("EXT", 3, REF_ID + ": no badge presented"), "words the form does not take -> %s, and the "
                  "hold now reads %s", r, cool().get("reason"))
        r = tell({"raised": False})
        ctx.check(r[0] == 200 and r[1].get("raised") is False and verdict_is("OK", 6), "POST /v0/hold clear -> %s, verdict %s", r,
                  cool().get("verdict"))

        # the package can no longer speak for itself
        _write(stop_path, "")
        os.kill(_svc(REF_ID)["pid"], signal.SIGKILL)
        t0 = time.time()
        c = verdict_is("EXT", 40, why_not_running)
        ev["held"] = {"after_s": round(time.time() - t0, 1), "cool": {k: (c or {}).get(k) for k in ("verdict", "fire_ok", "hold", "reason")},
                      "hold_file": hold_file()}
        ctx.log("the service ends at every start: %s", ev["held"])
        ctx.check(c and c.get("fire_ok") is False and c.get("hold") is True,
                  "the hold does not withhold fire: %s", c or cool())
        # it reads as running for a moment at every start on its way to quarantine: never clear
        looks = []
        for _ in range(24):
            ctx.sleep(0.5)
            c = cool()
            looks.append((c.get("verdict"), c.get("fire_ok")))
        ev["looks_over_the_crash_loop"] = sorted(set(looks), key=str)
        ctx.check(all(v == "EXT" and ok is False for v, ok in looks),
                  "the required hold flickered clear while its package kept ending: %s", ev["looks_over_the_crash_loop"])

        # the operator's exits
        _write(SAFE_FILE, "")
        t0 = time.time()
        ctx.check(verdict_is("OK", 4), "safe mode did not end the hold within four seconds: %s", cool())
        ev["safe_mode_released_s"] = round(time.time() - t0, 1)
        os.remove(SAFE_FILE)
        ctx.check(verdict_is("EXT", 30, why_not_running), "out of safe mode the hold did not come back: %s", cool())

        r = _forgeext("hold", REF_ID, "advisory")
        ctx.check(r.get("ok") is True and not os.listdir(REQUIRED_HOLDS), "marked advisory -> %s, still named %s",
                  r.get("error"), os.listdir(REQUIRED_HOLDS))
        ctx.check(verdict_is("OK", 6), "an advisory hold of a package that is not running was not dropped: %s", cool())
        r = _forgeext("hold", REF_ID, "required")
        ctx.check(r.get("ok") is True and verdict_is("EXT", 6, why_not_running), "marked required again: %s", cool())

        # the host itself gone quiet: the file goes stale, and that is what stands
        host = _host_pids()[0]
        os.kill(host, signal.SIGSTOP)
        try:
            c = verdict_is("EXT", 8, REF_ID + ": the extension host is not answering")
            ev["host_suspended"] = {k: (c or cool()).get(k) for k in ("verdict", "fire_ok", "reason")}
            ctx.check(c and c.get("fire_ok") is False, "with the host suspended the required hold does not stand on its "
                      "stale file: %s", c or cool())
        finally:
            os.kill(host, signal.SIGCONT)
        ctx.check(verdict_is("EXT", 8, why_not_running), "the host resumed: the hold is not the package's again: %s", cool())

        st, reply = fc.post("/settings", data={"ext_enabled": "0"})
        ctx.check(st == 200, "ext_enabled=0 -> %s %r", st, reply)
        t0 = time.time()
        ctx.check(verdict_is("OK", 4), "ext_enabled=0 did not end the hold within four seconds: %s", cool())
        ev["ext_off_released_s"] = round(time.time() - t0, 1)
        ctx.log("PASS: held in %s s; safe mode released it in %s s, extensions off in %s s", ev["held"]["after_s"],
                ev["safe_mode_released_s"], ev["ext_off_released_s"])
    finally:
        for pid in _host_pids():
            try:
                os.kill(pid, signal.SIGCONT)
            except OSError:
                pass
        _put_back(ctx, fc, work, prior, etag, raw, dir_mode)
    _as_found(ctx, fc, prior, raw, dir_mode, found_tree)
    ctx.check(not os.listdir(REQUIRED_HOLDS), "a required hold is still named at the end: %s", os.listdir(REQUIRED_HOLDS))
    ctx.check(_until(ctx, lambda: cool().get("verdict") == "OK", 6), "the verdict at the end is %s", cool().get("verdict"))


# ------------------------------------------------- the operator's door

@test("exthost.events", title="The machine's events reach a package, over one subscription",
      subsystem="exthost", kind="auto", hardware="takeover", est_min=6,
      covers=[("forgeext", "src/evfeed.*"), ("forgeext", "src/api.*"), ("forgeext", "src/run.*"),
              ("forgectrl", "src/events.*"), ("forgectrl", "src/main.c")],
      requires=["exthost.service"],
      description="The reference package is installed with the events capability. The host holds one "
                  "subscription to forgectrl's stream for every package that wants events, and none when "
                  "no package does: with the service running the status file says the stream is connected "
                  "and one service wants it, and with extensions off it says neither. The package polls "
                  "POST /v0/events over its API socket. A poll with nothing to say comes back at its own "
                  "deadline and not before, with an empty list and the place it already had; the lid "
                  "opened and closed on the fixture reaches the package as lid events, in order and "
                  "numbered, within a second, so a waiting poll is woken and not left to time out. The "
                  "host's slot is its own: with the three ordinary streams taken, the host's subscription "
                  "is still there and a LAN client that asks for the host's slot by name is refused, "
                  "because only a loopback peer may claim it. Nothing moves and nothing fires: the lid is "
                  "the only thing touched. Everything is put back as exthost.service puts it back.")
def events(ctx):
    import shutil
    import tempfile
    fc = ctx.forgectrl
    ev = ctx.evidence
    ctx.check(len(_host_pids()) == 1, "the extension host is not one running process: %s", _host_pids())
    ctx.check(fc.wait_idle(timeout=30, abort=ctx.aborted), "machine not idle: settings are locked")
    prior = fc.settings().get("ext_enabled") or ""
    raw = read_file(record_path())
    ctx.check(raw, "no setup record at %s", record_path())
    ctx.check(not os.path.exists(SAFE_FILE), "%s exists: the machine is in safe mode", SAFE_FILE)
    found_tree = _tree(EXT_ROOT)
    dir_mode = os.stat(os.path.dirname(EXT_ROOT)).st_mode & 0o7777
    st, body, hdrs = request(fc.base, "GET", "/advisories/extensions", headers={"Host": fc.host_header()})
    etag = hdrs.get("etag")
    ctx.check(st == 200 and etag, "GET /advisories/extensions -> %s", st)
    work = tempfile.mkdtemp(prefix="forgetest-ffx.")
    owner_key = os.path.join(EXT_ROOT, "keys", REF_KEY + ".pub")

    def said():
        """What the package has written of the events it polled for."""
        try:
            return json.loads(_read(os.path.join(EXT_ROOT, "data", REF_ID, "events.json")))
        except (OSError, ValueError):
            return {}

    def feed():
        return _host_status().get("events") or {}

    try:
        archive, pub = _pack_reference(work, lan_ip(), more_caps=["events"])
        shutil.copy(pub, owner_key)
        os.chmod(owner_key, 0o644)
        r = _forgeext("install", archive, "--consent-community")
        ctx.check(r.get("ok") is True, "the install -> %s", r.get("error"))
        st, reply = fc.post("/settings", data={"ext_enabled": "1", "advisory": etag, "phrase": SAFETY_PHRASE})
        ctx.check(st == 200, "ext_enabled=1 over the advisory -> %s %r", st, reply)
        x = _until(ctx, lambda: _svc(REF_ID) if _svc(REF_ID).get("state") == "running" else None, 90, poll=0.5)
        ctx.check(x, "the service is not running: %s", _svc(REF_ID))

        # One subscription, and only while a package wants it.
        f = _until(ctx, lambda: feed() if feed().get("connected") else None, 30)
        ev["feed_with_a_reader"] = f
        ctx.log("the host's subscription: %s", f)
        ctx.check(f and f.get("connected") is True and f.get("wanted") == 1,
                  "with one package that wants events the host says %s", f)

        # A poll that has nothing to say waits for its own deadline. The
        # first carries no place, so the wait is for one that does.
        def polled():
            x = said()
            return x if [q for q in x.get("polls") or [] if q.get("placed")] else None

        p = _until(ctx, polled, 40)
        ctx.check(p, "the package has not polled with a place of its own: %s", said())
        if not p:
            p = said()
        ev["first_polls"] = (p.get("polls") or [])[:4]
        ctx.check(all(q.get("status") == 200 for q in p.get("polls") or []),
                  "a poll was refused: %s", ev["first_polls"])
        # The first poll carries no place: it asks where the present is and
        # is answered at once, by design. Only a poll that named its place
        # and had nothing to be told waits for its deadline.
        first = (p.get("polls") or [])[0]
        ctx.check(first.get("placed") is False and first.get("took", 9) < 1.0,
                  "the first poll, which asks only where the present is, did not come back at once: %s", first)
        quiet = [q for q in p.get("polls") or [] if q.get("got") == 0 and q.get("placed")]
        ctx.check(quiet and all(q.get("took", 0) >= 1.8 for q in quiet),
                  "a poll with nothing to say came back early: %s", quiet[:4])
        place = p.get("place")
        ctx.check(isinstance(place, int), "the package has no place in the stream: %r", place)

        # An edge on the machine reaches it, and wakes a waiting poll.
        began = time.time()
        ctx.act("lid", "open", text="The package is to be told of it.")
        got = _until(ctx, lambda: said() if "lid" in (said().get("seen") or []) else None, 20, poll=0.25)
        took = round(time.time() - began, 2)
        ev["lid_event"] = {"after_s": took, "seen": (got or {}).get("seen"), "place": (got or {}).get("place")}
        ctx.log("the lid reached the package after %.2f s: %s", took, ev["lid_event"]["seen"])
        ctx.check(got, "the lid did not reach the package: %s", said())
        ctx.check(got and got.get("place", 0) > place, "the package's place did not move: %r -> %r",
                  place, (got or {}).get("place"))
        ctx.act("lid", "close", text="The lid ends as it was found.")
        ctx.check(_until(ctx, lambda: (said().get("seen") or []).count("lid") >= 2, 20, poll=0.25),
                  "the lid closing did not reach the package: %s", said().get("seen"))
        ev["polls_after"] = (said().get("polls") or [])[-4:]
        woken = [q for q in said().get("polls") or [] if q.get("got") and q.get("placed")]
        ctx.check(woken and min(q.get("took", 99) for q in woken) < 1.5,
                  "no poll was woken by an event: every one ran to its deadline: %s", ev["polls_after"])

        # The host's slot is outside the cap, and only a loopback peer may claim it.
        ctx.check(fc.settings().get("panel_open_reads") in (None, "", "1"),
                  "panel_open_reads is %r: this test reads /events without a session",
                  fc.settings().get("panel_open_reads"))
        held = []
        try:
            # Three addresses, because the cap is one stream per address,
            # and the whole of 127/8 is this host: each stream comes from
            # one of them, or the kernel would send all three from
            # 127.0.0.1 and they would replace each other.
            for i in range(EVENTS_MAX_STREAMS):
                code, why, sock = _stream("127.0.0.1", keep=True, source="127.0.0.%d" % (i + 2))
                held.append((code, why, sock))
            ev["ordinary_streams"] = [h[0] for h in held]
            ctx.check(all(h[0] == 200 for h in held), "the three ordinary streams: %s",
                      [(h[0], h[1]) for h in held])
            code, why, _sock = _stream("127.0.0.1", source="127.0.0.9")
            ev["fourth_ordinary_stream"] = [code, why]
            ctx.check(code == 503 and "every event stream is taken" in (why or ""),
                      "with the three taken a fourth ordinary stream got %s %r", code, why)
            ctx.check(feed().get("connected") is True, "the host's stream went with the three being taken: %s", feed())
            code, why, _sock = _stream(lan_ip(), host_client=True)
            ev["lan_claims_the_host_slot"] = [code, why]
            ctx.log("a LAN client asking for the host's slot -> %s %s", code, why)
            ctx.check(code == 503 and "every event stream is taken" in (why or ""),
                      "a LAN client that asked for the host's slot got %s %r", code, why)
            ctx.check(feed().get("connected") is True, "the host lost its stream to a LAN client: %s", feed())
        finally:
            for h in held:
                if h[2]:
                    try:
                        h[2].close()
                    except OSError:
                        pass

        # No package wants events: the subscription is let go.
        st, reply = fc.post("/settings", params={"ext_enabled": ""})
        ctx.check(st == 200, "ext_enabled='' -> %s %r", st, reply)
        f = _until(ctx, lambda: feed() if not feed().get("connected") else None, 30)
        ev["feed_with_no_reader"] = f
        ctx.check(f is not None and not f.get("connected") and not f.get("wanted"),
                  "with no package wanting events the host still holds a stream: %s", feed())
    finally:
        _put_back(ctx, fc, work, prior, etag, raw, dir_mode)
    _as_found(ctx, fc, prior, raw, dir_mode, found_tree)


@test("exthost.motion-jog", title="A package jogs the machine, bounded and dark",
      subsystem="exthost", kind="auto", hardware="takeover", mode="grbl", est_min=7,
      covers=[("forgeext", "src/api.*"), ("forgeext", "src/machine.*"), ("forgeext", "src/run.*"),
              ("forgectrl", "src/tokens.*"), ("forgectrl", "src/auth.*"), ("forgectrl", "src/main.c"),
              ("forgectrl", "src/grblport.*")],
      requires=["exthost.service"],
      description="The reference package is installed with motion.jog. Its jog reaches the machine "
                  "through the host's own credential, which forgectrl mints for each run of the daemon "
                  "into a file only root can read: the file is 0600 and root's, an extension account "
                  "cannot read it, and the credential holds motion.jog and not the routes it was not "
                  "given. The package's jog moves the head, and the head accelerometer - the "
                  "supervisor's own motion witness - sees it move; the laser stays dark throughout "
                  "(the latch locked, no fire seconds, no emission). A jog past the bounds is refused "
                  "by the host without the machine being asked, and a jog with the capability removed "
                  "is refused. Everything is put back as exthost.service puts it back, and the head "
                  "returns to where it started.")
def motion_jog(ctx):
    import shutil
    import tempfile
    fc = ctx.forgectrl
    ev = ctx.evidence
    ctx.check(len(_host_pids()) == 1, "the extension host is not one running process: %s", _host_pids())
    ctx.check(fc.wait_idle(timeout=30, abort=ctx.aborted), "machine not idle: settings are locked")
    prior = fc.settings().get("ext_enabled") or ""
    raw = read_file(record_path())
    ctx.check(raw, "no setup record at %s", record_path())
    ctx.check(not os.path.exists(SAFE_FILE), "%s exists: the machine is in safe mode", SAFE_FILE)
    found_tree = _tree(EXT_ROOT)
    dir_mode = os.stat(os.path.dirname(EXT_ROOT)).st_mode & 0o7777
    st, body, hdrs = request(fc.base, "GET", "/advisories/extensions", headers={"Host": fc.host_header()})
    etag = hdrs.get("etag")
    work = tempfile.mkdtemp(prefix="forgetest-ffx.")
    owner_key = os.path.join(EXT_ROOT, "keys", REF_KEY + ".pub")

    # The host's credential: root's alone, and holding only what it relays.
    cred = "/run/forgefirm/ext-host.token"
    ctx.check(os.path.exists(cred), "forgectrl minted no host credential at %s", cred)
    cst = os.stat(cred)
    ev["host_credential"] = {"mode": "%04o" % (cst.st_mode & 0o7777), "uid": cst.st_uid, "gid": cst.st_gid}
    ctx.check((cst.st_mode & 0o7777) == 0o600 and cst.st_uid == 0,
              "the host credential is not root's alone: %s", ev["host_credential"])
    token = _read(cred).strip()
    ctx.check(token.startswith("fft_"), "the host credential is not a scoped credential")
    # It holds motion.jog and nothing it was not given.
    st_, why_ = fc.get("/cam/snapshot", params={"cam": "lid"}, headers={"X-ForgeFIRM-Token": token})
    ev["credential_beyond_its_grant"] = [st_, why_ if st_ != 200 else "served"]
    ctx.check(st_ != 200, "the host credential reached a camera it does not hold: %s", st_)

    try:
        archive, pub = _pack_reference(work, lan_ip(), more_caps=["motion.jog"])
        shutil.copy(pub, owner_key)
        os.chmod(owner_key, 0o644)
        r = _forgeext("install", archive, "--consent-community", "--grant", "motion.jog")
        if r.get("ok") is not True:
            r = _forgeext("install", archive, "--consent-community")
        ctx.check(r.get("ok") is True, "the install -> %s", r.get("error"))
        st, reply = fc.post("/settings", data={"ext_enabled": "1", "advisory": etag, "phrase": SAFETY_PHRASE})
        ctx.check(st == 200, "ext_enabled=1 over the advisory -> %s %r", st, reply)
        x = _until(ctx, lambda: _svc(REF_ID) if _svc(REF_ID).get("state") == "running" else None, 90, poll=0.5)
        ctx.check(x, "the service is not running: %s", _svc(REF_ID))

        data_dir = os.path.join(EXT_ROOT, "data", REF_ID)
        jogged = os.path.join(data_dir, "jogged")

        def ask_jog(words, seconds=40):
            if os.path.exists(jogged):
                os.remove(jogged)
            _write(os.path.join(data_dir, "jog"), json.dumps(words))
            return _until(ctx, lambda: json.loads(_read(jogged)) if os.path.exists(jogged) else None,
                          seconds, poll=0.5)

        # A jog the host refuses on its own: the machine is never asked.
        far = ask_jog({"x": 500})
        ev["jog_past_the_bounds"] = far
        ctx.check(far and far[0] == 400 and "100 mm" in str(far[1]),
                  "a jog past the bounds was not refused by the host: %s", far)

        # A real jog, watched by the accelerometer and by the laser's own witnesses.
        accel = hw.AccelSampler()
        ctx.check(accel.available, "no head accelerometer found: the motion witness is missing")
        before = fc.get("/status")[1] or {}
        with accel:
            # The sampler stamps its samples with the wall clock, so the
            # window is read from the same clock.
            t0 = time.time()
            got = ask_jog({"x": 20, "feed": 3000})
            ctx.sleep(1.5)
            t1 = time.time()
        ev["jog"] = got
        ctx.check(got and got[0] == 200, "the package's jog was refused: %s", got)
        p2px, p2py, n = accel.p2p(t0, t1)
        ev["accel"] = {"p2p_x": p2px, "p2p_y": p2py, "samples": n}
        ctx.log("the package jogged X 20 mm: accel p2p x=%d y=%d over %d samples", p2px, p2py, n)
        ctx.check(n > 0 and max(p2px, p2py) > 0,
                  "the accelerometer saw no motion for the package's jog: %s", ev["accel"])

        after = fc.get("/status")[1] or {}

        def laser_of(st_):
            return {"locked": st_.get("laser_locked"),
                    "emission_samples": (st_.get("laser") or {}).get("emission_samples")}

        ev["laser"] = {"before": laser_of(before), "after": laser_of(after)}
        ctx.log("the laser across the jog: %s", ev["laser"])
        # The names are read from the machine's own status, so a field
        # that is not there fails rather than comparing None to None.
        ctx.check(ev["laser"]["before"]["locked"] is True and ev["laser"]["after"]["locked"] is True,
                  "the laser latch was not locked across a package's jog: %s", ev["laser"])
        ctx.check(ev["laser"]["after"]["emission_samples"] == 0
                  and ev["laser"]["before"]["emission_samples"] == 0,
                  "the emission witness is not zero across a package's jog: %s", ev["laser"])

        # Put the head back where it started.
        back = ask_jog({"x": -20, "feed": 3000})
        ctx.check(back and back[0] == 200, "the head was not jogged back: %s", back)
        ctx.sleep(1.5)
    finally:
        _put_back(ctx, fc, work, prior, etag, raw, dir_mode)
    _as_found(ctx, fc, prior, raw, dir_mode, found_tree)


@test("exthost.ui-delivery", title="A package's interface is served, and only when it has one",
      subsystem="exthost", kind="auto", hardware="api", est_min=4,
      covers=[("forgeext", "src/install.*"), ("forgeext", "src/main.c"), ("forgectrl", "src/extpkg.*"),
              ("forgectrl", "src/main.c")],
      requires=["exthost.service"],
      description="A package that asks for ui must ship ui/index.html, and one that ships the file must "
                  "ask: the install refuses each of those the other way round, in words. With both, GET "
                  "/ext/ui hands the page back as a JSON string, byte for byte what the package shipped, "
                  "and a package that is not installed is refused, as is one the operator disabled - "
                  "disabling a package is the way out of everything it does, its interface included. "
                  "The page is a string in JSON and never "
                  "markup this daemon composed. Nothing runs and nothing moves: the machine is only asked "
                  "for files. The frame the panel builds around the page is a browser's to judge, and that "
                  "is exthost.ui-frame-isolation's, which is a browser harness and not this catalog.")
def ui_delivery(ctx):
    import shutil
    import tempfile
    fc = ctx.forgectrl
    ev = ctx.evidence
    ctx.check(fc.wait_idle(timeout=30, abort=ctx.aborted), "machine not idle: settings are locked")
    prior = fc.settings().get("ext_enabled") or ""
    raw = read_file(record_path())
    found_tree = _tree(EXT_ROOT)
    dir_mode = os.stat(os.path.dirname(EXT_ROOT)).st_mode & 0o7777
    st, body, hdrs = request(fc.base, "GET", "/advisories/extensions", headers={"Host": fc.host_header()})
    etag = hdrs.get("etag")
    work = tempfile.mkdtemp(prefix="forgetest-ffx.")
    owner_key = os.path.join(EXT_ROOT, "keys", REF_KEY + ".pub")

    try:
        archive, pub = _pack_reference(work, lan_ip(), more_caps=["ui"])
        shutil.copy(pub, owner_key)
        os.chmod(owner_key, 0o644)

        # Asking for an interface without shipping one, and shipping one
        # without asking: each refused, and neither installed.
        # Each archive is packed in a directory of its own: the packer
        # makes a signing key there, and fwup will not remake one over an
        # existing file.
        for caps, want_ui, why_, words in ((["ui"], False, "asks_without_the_file", "ui/index.html"),
                                           ([], True, "ships_without_asking", "does not ask for ui")):
            wn = tempfile.mkdtemp(prefix="forgetest-ffxn.")
            try:
                arch, _k = _pack_reference(wn, lan_ip(), more_caps=caps, ui=want_ui)
                r = _forgeext("inspect", arch)
                ev[why_] = r.get("error")
                ctx.check(r.get("ok") is False and words in (r.get("error") or ""),
                          "%s -> %s", why_.replace("_", " "), r)
            finally:
                shutil.rmtree(wn, ignore_errors=True)

        r = _forgeext("install", archive, "--consent-community")
        ctx.check(r.get("ok") is True, "the install of a package with an interface -> %s", r.get("error"))
        st, reply = fc.post("/settings", data={"ext_enabled": "1", "advisory": etag, "phrase": SAFETY_PHRASE})
        ctx.check(st == 200, "ext_enabled=1 -> %s %r", st, reply)

        st_, doc = fc.get("/ext/ui", params={"id": REF_ID})
        ev["ui"] = {"status": st_, "bytes": (doc or {}).get("bytes")}
        ctx.log("GET /ext/ui -> %s, %s bytes", st_, (doc or {}).get("bytes"))
        ctx.check(st_ == 200 and isinstance(doc, dict) and doc.get("ok"), "GET /ext/ui -> %s %s", st_, doc)
        ctx.check(doc.get("html") == REF_UI, "the page served is not the one the package shipped")
        ctx.check(doc.get("bytes") == len(REF_UI), "the length does not match the page: %s", doc.get("bytes"))

        st_, why = fc.get("/ext/ui", params={"id": "org.forgetest.nothere"})
        ev["ui_absent"] = [st_, why]
        # The host's refusal in its words, and 404: a 502 here is a relay
        # that took the host's refusal for no answer.
        ctx.check(st_ == 404 and "not installed" in json.dumps(why), "a package that is not installed -> %s %s", st_, why)

        # Disabled, it has no page. Disabling a package is the operator's
        # way out of everything it does, and the interface is part of
        # that: the page reaches the machine through the panel's bridge,
        # and a door that is shut is shut.
        r = _forgeext("disable", REF_ID)
        ctx.check(r.get("ok") is True, "disable -> %s", r.get("error"))
        st_, why = fc.get("/ext/ui", params={"id": REF_ID})
        ev["ui_disabled"] = [st_, why]
        ctx.check(st_ >= 400 and "disabled" in json.dumps(why), "a disabled package's page -> %s %s", st_, why)
        r = _forgeext("enable", REF_ID)
        ctx.check(r.get("ok") is True, "enable -> %s", r.get("error"))
        st_, doc = fc.get("/ext/ui", params={"id": REF_ID})
        ctx.check(st_ == 200 and (doc or {}).get("ok"), "enabled again, its page is served -> %s", st_)

        # Its settings come through the same relay, the page's way to them
        # through the bridge. A value the host refuses is 400 in the host's
        # words, and what is stored does not move.
        st_, doc = fc.get("/ext/settings", params={"id": REF_ID})
        ev["settings"] = [st_, (doc or {}).get("settings")]
        ctx.check(st_ == 200 and (doc or {}).get("ok") and (doc.get("settings") or {}).get("threshold") == 40,
                  "GET /ext/settings -> %s %s", st_, doc)
        st_, why = fc.post("/ext/settings", data={"id": REF_ID, "set": json.dumps({"threshold": 101})})
        ev["settings_refused"] = [st_, why]
        ctx.log("POST /ext/settings, a threshold over its bound -> %s %s", st_, why)
        ctx.check(st_ == 400 and "at most 100" in json.dumps(why), "a value the host refuses -> %s %s", st_, why)
        st_, doc = fc.get("/ext/settings", params={"id": REF_ID})
        ctx.check(st_ == 200 and ((doc or {}).get("settings") or {}).get("threshold") == 40,
                  "a refused value moved the stored one: %s %s", st_, doc)
    finally:
        _put_back(ctx, fc, work, prior, etag, raw, dir_mode)
    _as_found(ctx, fc, prior, raw, dir_mode, found_tree)


@test("exthost.motion-job", title="A package runs a program, under every gate a sender is under",
      subsystem="exthost", kind="auto", hardware="takeover", mode="grbl", est_min=9,
      covers=[("forgeext", "src/api.*"), ("forgeext", "src/machine.*"), ("forgeext", "src/run.*"),
              ("forgectrl", "src/jobpost.*"), ("forgectrl", "src/jobrun.*"), ("forgectrl", "src/tokens.*")],
      requires=["exthost.service", "motion.job"],
      steps=["Setup: lid closed, nothing in the bed."],
      description="The reference package is installed with motion.job, which the operator grants. It "
                  "writes a program into its own data directory and asks the host to run it. The program "
                  "is dark: it moves and never commands the laser. The job reaches the machine as the "
                  "package, under the machine lease, and the machine's own job record names it as the "
                  "owner and counts the program's lines. A dark program commands no laser, so no armed "
                  "window opens and there is nothing for a press to arm: the arm gates are in force and "
                  "are never reached, and a job that fires is cloud.dark-print's and "
                  "laser.emission-witness's. Every line is sent and acknowledged, and the runner ends the "
                  "job as one that ended without a discharge - which is what a program that commands no "
                  "laser is, and is the outcome this test wants. The job's own emission witnesses are all "
                  "zero. The head accelerometer sees the motion and the "
                  "laser's own witnesses stay at zero throughout: the latch locked before the window and "
                  "after it, and no emission at any point. A program named with a path, or one that is not "
                  "a file of the package's own data, is refused by the host with the machine never asked. "
                  "Everything is put back as exthost.service puts it back.")
def motion_job(ctx):
    import shutil
    import tempfile
    fc = ctx.forgectrl
    ev = ctx.evidence
    ctx.check(fc.wait_idle(timeout=30, abort=ctx.aborted), "machine not idle: settings are locked")
    prior = fc.settings().get("ext_enabled") or ""
    raw = read_file(record_path())
    found_tree = _tree(EXT_ROOT)
    dir_mode = os.stat(os.path.dirname(EXT_ROOT)).st_mode & 0o7777
    st, body, hdrs = request(fc.base, "GET", "/advisories/extensions", headers={"Host": fc.host_header()})
    etag = hdrs.get("etag")
    work = tempfile.mkdtemp(prefix="forgetest-ffx.")
    owner_key = os.path.join(EXT_ROOT, "keys", REF_KEY + ".pub")

    # A program that moves and never fires: no M3, no S above zero.
    PROGRAM = "G21\nG91\nG1 X10 F2000\nG1 X-10 F2000\nG90\nM2\n"

    try:
        archive, pub = _pack_reference(work, lan_ip(), more_caps=["motion.job"])
        shutil.copy(pub, owner_key)
        os.chmod(owner_key, 0o644)
        r = _forgeext("install", archive, "--consent-community", "--grant", "motion.job")
        ctx.check(r.get("ok") is True, "the install with the operator's grant -> %s", r.get("error"))
        st, reply = fc.post("/settings", data={"ext_enabled": "1", "advisory": etag, "phrase": SAFETY_PHRASE})
        ctx.check(st == 200, "ext_enabled=1 -> %s %r", st, reply)
        x = _until(ctx, lambda: _svc(REF_ID) if _svc(REF_ID).get("state") == "running" else None, 90, poll=0.5)
        ctx.check(x, "the service is not running: %s", _svc(REF_ID))

        data_dir = os.path.join(EXT_ROOT, "data", REF_ID)
        ran = os.path.join(data_dir, "ran")

        def ask_run(body, seconds=60):
            if os.path.exists(ran):
                os.remove(ran)
            _write(os.path.join(data_dir, "run"), json.dumps(body))
            return _until(ctx, lambda: json.loads(_read(ran)) if os.path.exists(ran) else None,
                          seconds, poll=0.5)

        # A program the host will not read: a path, and one that is not there.
        for body_, why_ in (({"program": "../../../etc/passwd", "program_text": None}, "a program with a path"),
                            ({"program": "nothere.gcode", "program_text": None}, "a program that is not there")):
            got = ask_run(body_)
            ev[why_.replace(" ", "_")] = got
            ctx.check(got and got[0] == 400, "%s -> %s", why_, got)

        # The real one: dark, and under the button like any other sender.
        before = fc.get("/status")[1] or {}
        ctx.check((before.get("laser") or {}).get("emission_samples") == 0 and before.get("laser_locked") is True,
                  "the laser is not dark and locked before the job: %s", before.get("laser"))
        accel = hw.AccelSampler()
        ctx.check(accel.available, "no head accelerometer found: the motion witness is missing")
        t0 = time.time()
        with accel:
            got = ask_run({"program": "job.gcode", "program_text": PROGRAM, "lit_within_s": 120,
                           "timeout_s": 300})
            ev["job"] = got
            ctx.check(got and got[0] == 200, "the package's job was refused: %s", got)
            # The machine's own record of the job: it took it, and it took
            # it as this package. A dark program commands no laser, so no
            # armed window opens and there is nothing for a press to arm -
            # the arm gates are in force and simply never reached. A job
            # that fires is cloud.dark-print's and laser.emission-witness's.
            def job_rec():
                st_, j_ = fc.get("/job")
                return j_ if isinstance(j_, dict) else {}

            # The lease owner is the job's, named for the package.
            def owned():
                r = job_rec()
                return r if (r.get("owner") or "").endswith(REF_ID) and r.get("program") else None

            took = _until(ctx, owned, 60, poll=0.5)
            ev["job_record"] = {k: (took or {}).get(k) for k in ("state", "owner", "lines")}
            ctx.log("the machine's job record: %s", ev["job_record"])
            ctx.check(took, "the machine did not take the job as the package: %s", job_rec())
            ctx.check((took or {}).get("lines", 0) > 0, "the machine read no lines of the program: %s", took)

            ended = _until(ctx, lambda: job_rec() if job_rec().get("state") in ("done", "failed", "idle")
                           else None, 180, poll=1.0)
            r = ended or job_rec()
            em = r.get("emission") or {}
            ev["job_end"] = {"state": r.get("state"), "reason": r.get("reason"), "lit": r.get("lit"),
                             "sent": r.get("sent"), "acked": r.get("acked"), "emission": em}
            ctx.log("the job ended: %s", ev["job_end"])
            ctx.check(ended, "the job did not end: %s", job_rec())
            ctx.check(r.get("sent") == r.get("lines") and r.get("acked") == r.get("lines"),
                      "the machine did not send and acknowledge every line: %s", ev["job_end"])
            # A program that commands no laser ends without a discharge,
            # and the runner says so rather than calling it a clean run.
            # That is the outcome this test wants: the whole program ran
            # and nothing fired.
            ctx.check(r.get("state") == "failed" and "without a discharge" in (r.get("reason") or ""),
                      "a program that never fires did not end as one: %s", ev["job_end"])
            ctx.check(r.get("lit") is False and em.get("laser_on_samples") == 0
                      and em.get("lit_s") == 0 and em.get("thermopile_delta") == 0,
                      "the job's own emission witnesses are not all zero: %s", em)
            ctx.sleep(1.0)
        t1 = time.time()
        p2px, p2py, n = accel.p2p(t0, t1)
        ev["accel"] = {"p2p_x": p2px, "p2p_y": p2py, "samples": n}
        ctx.log("the package's job moved the head: accel p2p x=%d y=%d over %d samples", p2px, p2py, n)
        ctx.check(n > 0 and max(p2px, p2py) > 0, "the accelerometer saw no motion for the job: %s", ev["accel"])

        after = fc.get("/status")[1] or {}
        ev["laser"] = {"before": {"locked": before.get("laser_locked"),
                                  "emission": (before.get("laser") or {}).get("emission_samples")},
                       "after": {"locked": after.get("laser_locked"),
                                 "emission": (after.get("laser") or {}).get("emission_samples")}}
        ctx.log("the laser across the package's job: %s", ev["laser"])
        ctx.check(after.get("laser_locked") is True, "the laser latch is not locked after the job: %s", ev["laser"])
        ctx.check((after.get("laser") or {}).get("emission_samples") == 0,
                  "the emission witness is not zero across a dark job: %s", ev["laser"])
    finally:
        _put_back(ctx, fc, work, prior, etag, raw, dir_mode)
    _as_found(ctx, fc, prior, raw, dir_mode, found_tree)


@test("exthost.package-routes", title="The panel's package routes are the extension host's own word",
      subsystem="exthost", kind="auto", est_min=2,
      covers=[("forgectrl", "src/extpkg.*"), ("forgectrl", "src/main.c"), ("forgectrl", "src/ui/ext.js"),
              ("forgectrl", "src/ui/index.html"), ("forgectrl", "src/ui/embed.cmake"), ("forgeext", "src/main.c"),
              ("forgeext", "src/install.*"), ("forgeext", "src/state.*")],
      requires=["exthost.service", "forgectrl.auth"],
      description="Extensions stay as found (the package is installed and never runs). The reference package is "
                  "installed with the hold grant through the host's command line. GET /ext/status without "
                  "the login is refused (403); with it, it lists the package as the host does (tier "
                  "community, enabled, the hold advisory, and the capabilities it may use - what needs no "
                  "grant and what the operator granted, which is what the panel's bridge decides on and "
                  "is never wider than what the manifest asked for) beside enabled, safe_mode, and the host's own "
                  "status with running true. POST /ext/package: hold-required names the package under "
                  "required-holds and the list says required; disable and enable change the host's state "
                  "file; an action outside the closed list and an id that has not the form of one are 400 "
                  "and run nothing; an id that is not installed is 409 in the host's words; without the "
                  "login 403; remove takes the package, its data, and its name away. The panel page carries "
                  "the card and its script, and the script is in the page before the one that calls it "
                  "(the panel runs its tab's loads while it parses). The key and the work "
                  "directory are removed and the extension root is as found.")
def package_routes(ctx):
    import shutil
    import tempfile
    fc = ctx.forgectrl
    ev = ctx.evidence
    ctx.check(len(_host_pids()) == 1, "the extension host is not one running process: %s", _host_pids())
    ctx.check(REF_ID not in [x.get("id") for x in _forgeext("list").get("packages", [])], "%s is already installed", REF_ID)
    found_tree = _tree(EXT_ROOT)
    work = tempfile.mkdtemp(prefix="forgetest-ffx.")
    owner_key = os.path.join(EXT_ROOT, "keys", REF_KEY + ".pub")

    def listed():
        st, doc = fc.get("/ext/status")
        ctx.check(st == 200 and isinstance(doc, dict), "GET /ext/status -> %s", st)
        return doc, next((x for x in doc.get("packages", []) if x.get("id") == REF_ID), None)

    def act(action, pid=REF_ID):
        return fc.post("/ext/package", data={"id": pid, "action": action})

    def state():
        return json.loads(_read(EXT_ROOT + "/state.json") or "{}").get("packages", {}).get(REF_ID, {})

    try:
        # The card is in the page the image serves, and its script is loaded before the one whose
        # tab loads call it: panel.js runs them while it parses, so a later ext.js is a broken tab.
        st, page = fc.get("/", raw=True)
        text = page.decode("utf-8", "replace") if isinstance(page, bytes) else str(page)
        ev["panel_page_bytes"] = len(text)
        ctx.check(st == 200 and text, "GET / -> %s", st)
        for want in ("extpkgs", "extswitch", "extfile", "extstaged", "extinstallphrase", "extAct", "loadExt"):
            ctx.check(want in text, "the panel page carries no %s", want)
        ctx.check(0 < text.index("function loadExt") < text.index("loadExt();"),
                  "the panel page defines loadExt after the call that runs at parse time")

        archive, pub = _pack_reference(work, lan_ip(), more_caps=["hold"])
        shutil.copy(pub, owner_key)
        os.chmod(owner_key, 0o644)
        r = _forgeext("install", archive, "--consent-community", "--grant", "hold")
        ctx.check(r.get("ok") is True, "the install -> %s", r.get("error"))

        st, body, hdrs = request(fc.base, "GET", "/ext/status", headers={"Host": fc.host_header()})
        ev["status_without_login"] = st
        ctx.check(st == 403, "GET /ext/status without the login -> %s, expected 403", st)
        doc, pkg = listed()
        ev["status"] = {k: doc.get(k) for k in ("enabled", "safe_mode")}
        ev["host"] = {k: (doc.get("host") or {}).get(k) for k in ("running", "pid", "enabled", "off_reason")}
        ev["package"] = {k: (pkg or {}).get(k) for k in ("tier", "enabled", "quarantined", "grants", "hold", "account")}
        ctx.log("GET /ext/status: %s, host %s, package %s", ev["status"], ev["host"], ev["package"])
        ctx.check(set(doc) >= {"enabled", "safe_mode", "host", "packages"} and doc["safe_mode"] is False,
                  "the status document lacks a key: %s", sorted(doc))
        ctx.check((doc.get("host") or {}).get("running") is True and doc["host"].get("pid") == _host_pids()[0],
                  "the host's own status is not passed on as a running host's: %s", ev["host"])
        ctx.check(pkg and pkg.get("tier") == "community" and pkg.get("enabled") is True and pkg.get("hold") == "advisory"
                  and pkg.get("grants") == ["hold"] and (pkg.get("package") or {}).get("name") == "forgetest reference",
                  "the package is not listed as the host lists it: %s", pkg)

        # What the package may use, beside what it asked for. The panel's
        # bridge decides on this list and not on the manifest's, so a
        # capability the operator did not grant must not be in it.
        asked = set((pkg.get("package") or {}).get("capabilities") or [])
        effective = set(pkg.get("effective") or [])
        granted = set(pkg.get("grants") or [])
        ev["capabilities"] = {"asked": sorted(asked), "effective": sorted(effective), "granted": sorted(granted)}
        ctx.log("the reference package asked for %s, may use %s", sorted(asked), sorted(effective))
        ctx.check(effective and effective <= asked, "it may use what it did not ask for: %s", ev["capabilities"])
        ctx.check(granted <= effective, "a capability the operator granted is not in what it may use: %s",
                  ev["capabilities"])
        st, reply = act("hold-advisory")                 # no change; the grant stands either way
        r = _forgeext("list", REF_ID)
        host_eff = set(((r.get("packages") or [{}])[0]).get("effective") or [])
        ctx.check(host_eff == effective, "the panel's list and the host's own do not agree: %s vs %s",
                  sorted(effective), sorted(host_eff))

        st, reply = act("hold-required")
        ctx.check(st == 200 and os.listdir(REQUIRED_HOLDS) == [REF_ID] and listed()[1].get("hold") == "required",
                  "hold-required -> %s, named %s", st, os.listdir(REQUIRED_HOLDS))
        st, reply = act("hold-advisory")
        ctx.check(st == 200 and not os.listdir(REQUIRED_HOLDS), "hold-advisory -> %s, still named %s", st, os.listdir(REQUIRED_HOLDS))
        st, reply = act("disable")
        ctx.check(st == 200 and state().get("enabled") is False and listed()[1].get("enabled") is False,
                  "disable -> %s, the host's state says %s", st, state().get("enabled"))
        st, reply = act("enable")
        ctx.check(st == 200 and state().get("enabled") is True, "enable -> %s, the host's state says %s", st, state().get("enabled"))

        before = _read(EXT_ROOT + "/state.json")
        for name, form, want, words in (
                ("an action outside the list", {"id": REF_ID, "action": "install"}, 400, "action is enable"),
                ("an action with a shell's words", {"id": REF_ID, "action": "remove; reboot"}, 400, "action is enable"),
                ("an id that has not the form of one", {"id": "reference; reboot", "action": "disable"}, 400, "id is a package id"),
                ("an id with a path in it", {"id": "../../etc", "action": "remove"}, 400, "id is a package id"),
                ("a package that is not installed", {"id": "org.forgetest.nothere", "action": "disable"}, 409, "is not installed")):
            st, reply = fc.post("/ext/package", data=form)
            ev[name] = st
            ctx.log("POST /ext/package, %s -> %s %s", name, st, reply if isinstance(reply, str) else "")
            ctx.check(st == want and isinstance(reply, str) and words in reply, "%s -> %s %r, expected %s", name, st, reply, want)
        ctx.check(_read(EXT_ROOT + "/state.json") == before, "a refused request changed the host's state")
        st, body, hdrs = request(fc.base, "POST", "/ext/package", data={"id": REF_ID, "action": "remove"},
                                 headers={"Host": fc.host_header()})
        ctx.check(st == 403 and REF_ID in [x.get("id") for x in _forgeext("list").get("packages", [])],
                  "POST /ext/package without the login -> %s, expected 403 and the package still there", st)

        st, reply = act("remove")
        ctx.check(st == 200 and listed()[1] is None and not os.path.isdir(os.path.join(EXT_ROOT, "pkg", REF_ID))
                  and not os.path.isdir(os.path.join(EXT_ROOT, "data", REF_ID)), "remove -> %s, the package or its data still there", st)
    finally:
        if REF_ID in [x.get("id") for x in _forgeext("list").get("packages", [])]:
            _forgeext("remove", REF_ID)
        if os.path.exists(owner_key):
            os.remove(owner_key)
        shutil.rmtree(work, ignore_errors=True)
    left = _tree(EXT_ROOT)
    ctx.check(left == found_tree, "the extension root is not as found: %s", sorted(set(left) ^ set(found_tree)))
    ctx.check(not os.listdir(REQUIRED_HOLDS), "a required hold is still named at the end: %s", os.listdir(REQUIRED_HOLDS))


# ------------------------------------------- installing through the panel

EXT_STAGE = "/data/forgefirm/tmp/ext-upload.ffx"


@test("exthost.panel-install", title="A package is installed through the panel with the consent its tier takes",
      subsystem="exthost", kind="operator", mode="grbl", est_min=4,
      covers=[("forgectrl", "src/extpkg.*"), ("forgectrl", "src/main.c"), ("forgectrl", "src/auth.*"),
              ("forgeext", "src/main.c"), ("forgeext", "src/install.*"), ("forgeext", "src/pkg.*")],
      requires=["exthost.package-routes"], actions=["button"],
      steps=["The machine idle in GRBL mode; nothing moves and nothing fires. The test asks for the button to be "
             "HELD for a few seconds while it installs an unsigned package: hold it when the notice says so."],
      description="Extensions stay as found; the packages are installed and never run. POST /ext/upload without "
                  "the login is 403; an upload the host will not take (bytes that are no archive) is 400 in the "
                  "host's words and leaves no staged file. The reference package signed with a key nobody "
                  "trusts uploads as tier unverified with consent button: POST /ext/install without the button "
                  "held is 409 and the staged file stays, the typed phrase is no substitute, and with the "
                  "button held it installs with the hold the request granted and is listed unverified. With "
                  "the same key added as the owner's, the upload reads community with consent typed: the "
                  "install without the phrase is 400, with the phrase and without the grant 409 in the "
                  "host's words, with a grant that has not the form of a capability 400, and with the phrase "
                  "and the grant it installs. The key itself goes in through the panel: without the button "
                  "held it is 409 and no file lands, a name with a space or a path is 400, a key that is no "
                  "key is 409 from the host, and with the button held it is added and listed with its id; "
                  "removed again, the same archive reads unverified. An install with nothing staged is 409, and a discarded upload "
                  "is gone. Both packages are removed through the route; the key, the work directory, and "
                  "the staged file are removed and the extension root is as found.")
def panel_install(ctx):
    import shutil
    import tempfile
    fc = ctx.forgectrl
    ev = ctx.evidence
    ctx.check(len(_host_pids()) == 1, "the extension host is not one running process: %s", _host_pids())
    ctx.check(fc.wait_idle(timeout=30, abort=ctx.aborted), "machine not idle: an upload is refused")
    ctx.check(REF_ID not in [x.get("id") for x in _forgeext("list").get("packages", [])], "%s is already installed", REF_ID)
    found_tree = _tree(EXT_ROOT)
    work = tempfile.mkdtemp(prefix="forgetest-ffx.")
    owner_key = os.path.join(EXT_ROOT, "keys", REF_KEY + ".pub")

    def upload(path=None, raw=None, login=True):
        mark = "forgetestExtBoundary7d1"
        with open(path, "rb") if path else contextlib.nullcontext() as f:
            data = f.read() if path else raw
        body = ('--%s\r\nContent-Disposition: form-data; name="file"; filename="package.ffx"\r\n'
                'Content-Type: application/octet-stream\r\n\r\n' % mark).encode() + data + ("\r\n--%s--\r\n" % mark).encode()
        hdrs = {"Content-Type": "multipart/form-data; boundary=%s" % mark}
        if login:
            return fc.post("/ext/upload", data=body, headers=hdrs)
        st, reply, _ = request(fc.base, "POST", "/ext/upload", data=body, headers=dict(hdrs, Host=fc.host_header()))
        return st, reply

    def install(**form):
        return fc.post("/ext/install", data=form)

    def installed():
        return next((x for x in _forgeext("list").get("packages", []) if x.get("id") == REF_ID), None)

    try:
        archive, pub = _pack_reference(work, lan_ip(), more_caps=["hold"])

        st, reply = upload(archive, login=False)
        ctx.check(st == 403 and not os.path.exists(EXT_STAGE), "an upload without the login -> %s, staged %s", st, os.path.exists(EXT_STAGE))
        st, reply = upload(raw=b"these bytes are no archive\n" * 40)
        ev["not_an_archive"] = [st, reply if isinstance(reply, str) else ""]
        ctx.log("an upload that is no archive -> %s %s", st, reply if isinstance(reply, str) else "")
        ctx.check(st == 400 and not os.path.exists(EXT_STAGE), "an upload the host will not take -> %s, staged file %s", st,
                  "kept" if os.path.exists(EXT_STAGE) else "gone")
        st, reply = install(grants="hold", phrase=SAFETY_PHRASE)
        ctx.check(st == 409 and isinstance(reply, str) and "staged" in reply, "an install with nothing staged -> %s %r", st, reply)

        # nobody the machine trusts signed it: the button, held
        st, doc = upload(archive)
        ev["unverified_upload"] = {k: (doc or {}).get(k) for k in ("tier", "consent", "needs_grant")} if isinstance(doc, dict) else doc
        ctx.log("the upload, its key unknown to the machine -> %s %s", st, ev["unverified_upload"])
        ctx.check(st == 200 and isinstance(doc, dict) and doc.get("tier") == "unverified" and doc.get("consent") == "button"
                  and doc.get("needs_grant") == ["hold"] and (doc.get("package") or {}).get("id") == REF_ID,
                  "the unverified upload -> %s %s", st, doc)
        st, reply = install(grants="hold", phrase=SAFETY_PHRASE)
        ev["unverified_without_the_button"] = [st, reply if isinstance(reply, str) else ""]
        ctx.check(st == 409 and isinstance(reply, str) and "button" in reply and os.path.exists(EXT_STAGE) and not installed(),
                  "an unverified install without the button held -> %s %r", st, reply)
        done = {}

        def held_install():
            st_, reply_ = install(grants="hold")
            done["last"] = [st_, reply_ if isinstance(reply_, str) else "ok"]
            return st_ == 200

        # A person holds the button; the bench actuator's press is a half-second pulse, so it is pressed again
        # until a request has landed inside one.
        for _ in range(10):
            if ctx.act("button", "press", text="HOLD the button now, for a few seconds: an unsigned package is being installed.",
                       until=held_install, timeout=4, fail=False, ms=500) is not None:
                break
        pkg = installed()
        ev["unverified_installed"] = {k: (pkg or {}).get(k) for k in ("tier", "grants", "hold", "enabled")}
        ctx.log("with the button held: %s, listed %s", done.get("last"), ev["unverified_installed"])
        ctx.check(pkg and pkg.get("tier") == "unverified" and pkg.get("grants") == ["hold"] and not os.path.exists(EXT_STAGE),
                  "with the button held the package is not installed as unverified with its grant: %s (%s)", pkg, done.get("last"))
        st, reply = fc.post("/ext/package", data={"id": REF_ID, "action": "remove"})
        ctx.check(st == 200 and not installed(), "remove through the route -> %s", st)

        # the same key, now the owner's, added through the panel with the machine's button held
        with open(pub) as f:
            keytext = f.read().strip()
        st, reply = fc.post("/ext/key", data={"name": REF_KEY, "key": keytext})
        ev["key_without_the_button"] = [st, reply if isinstance(reply, str) else ""]
        ctx.check(st == 409 and isinstance(reply, str) and "button" in reply and not os.path.exists(owner_key),
                  "a key added without the button held -> %s %r", st, reply)
        for name, form, want in (("a name with a space", {"name": "a maker", "key": keytext}, 400),
                                 ("a name that is a path", {"name": "../../etc/passwd", "key": keytext}, 400),
                                 ("no key at all", {"name": REF_KEY}, 400),
                                 ("a key that is no key", {"name": REF_KEY, "key": "this is no key"}, 409)):
            st, reply = fc.post("/ext/key", data=form)
            ev[name] = st
            ctx.check(st == want and not os.path.exists(owner_key), "%s -> %s, expected %s", name, st, want)
        keyed = {}

        def held_key():
            st_, reply_ = fc.post("/ext/key", data={"name": REF_KEY, "key": keytext})
            keyed["last"] = [st_, reply_ if isinstance(reply_, str) else "ok"]
            return st_ == 200

        for _ in range(10):
            if ctx.act("button", "press", text="HOLD the button now, for a few seconds: a key is being added.",
                       until=held_key, timeout=4, fail=False, ms=500) is not None:
                break
        st, doc = fc.get("/ext/status")
        keys = {k.get("name"): k.get("key") for k in ((doc or {}).get("keys") or [])}
        ev["keys"] = keys
        ctx.log("with the button held: %s, the keys now %s", keyed.get("last"), sorted(keys))
        ctx.check(REF_KEY in keys and os.path.exists(owner_key), "the key was not added with the button held: %s (%s)",
                  sorted(keys), keyed.get("last"))
        ctx.check(len(keys.get(REF_KEY) or "") == 64, "the key is listed without its id: %s", keys)

        st, doc = upload(archive)
        ev["community_upload"] = {k: (doc or {}).get(k) for k in ("tier", "consent", "needs_grant")} if isinstance(doc, dict) else doc
        ctx.check(st == 200 and isinstance(doc, dict) and doc.get("tier") == "community" and doc.get("consent") == "typed",
                  "the community upload -> %s %s", st, doc)
        for name, form, want, words in (
                ("no phrase", {"grants": "hold"}, 400, "type I UNDERSTAND"),
                ("the phrase in another case", {"grants": "hold", "phrase": SAFETY_PHRASE.lower()}, 400, "type I UNDERSTAND"),
                ("the phrase and no grant", {"phrase": SAFETY_PHRASE}, 409, "hold"),
                ("a grant with a shell's words", {"phrase": SAFETY_PHRASE, "grants": "hold;reboot"}, 400, "capability names"),
                ("a grant that is an option", {"phrase": SAFETY_PHRASE, "grants": "--consent-unverified"}, 400, "capability names")):
            st, reply = install(**form)
            ev[name] = st
            ctx.log("POST /ext/install, %s -> %s %s", name, st, reply if isinstance(reply, str) else "")
            ctx.check(st == want and isinstance(reply, str) and words in reply and not installed() and os.path.exists(EXT_STAGE),
                      "%s -> %s %r, expected %s with the package not installed and the staged file kept", name, st, reply, want)
        st, reply = install(grants="hold", phrase=SAFETY_PHRASE)
        pkg = installed()
        ctx.check(st == 200 and pkg and pkg.get("tier") == "community" and pkg.get("grants") == ["hold"]
                  and not os.path.exists(EXT_STAGE), "the community install -> %s, listed %s", st, pkg)
        st, reply = fc.post("/ext/package", data={"id": REF_ID, "action": "remove"})
        ctx.check(st == 200 and not installed(), "remove through the route -> %s", st)

        # the key removed: the same archive reads unverified again
        st, reply = fc.post("/ext/key/remove", data={"name": REF_KEY})
        ctx.check(st == 200 and not os.path.exists(owner_key), "the key was not removed -> %s %r", st, reply)
        st, reply = fc.post("/ext/key/remove", data={"name": REF_KEY})
        ctx.check(st == 409, "removing a key that is not there -> %s", st)

        st, doc = upload(archive)
        ev["after_the_key_went"] = (doc or {}).get("tier") if isinstance(doc, dict) else doc
        ctx.check(st == 200 and isinstance(doc, dict) and doc.get("tier") == "unverified" and os.path.exists(EXT_STAGE),
                  "without the key the same archive reads %s", ev["after_the_key_went"])
        st, reply = fc.post("/ext/upload/discard")
        ctx.check(st == 200 and not os.path.exists(EXT_STAGE), "discard -> %s, staged file %s", st,
                  "kept" if os.path.exists(EXT_STAGE) else "gone")
        st, reply = install(grants="hold", phrase=SAFETY_PHRASE)
        ctx.check(st == 409 and not installed(), "an install after the discard -> %s", st)
    finally:
        ctx.clear_notice()
        if installed():
            _forgeext("remove", REF_ID)
        for path in (owner_key, EXT_STAGE):
            if os.path.exists(path):
                os.remove(path)
        shutil.rmtree(work, ignore_errors=True)
    left = _tree(EXT_ROOT)
    ctx.check(left == found_tree, "the extension root is not as found: %s", sorted(set(left) ^ set(found_tree)))

