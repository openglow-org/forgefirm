# Copyright 2026 514 LLC d/b/a OpenGlow
# Written by Scott Wiederhold
# https://community.openglow.org
# SPDX-License-Identifier:    MIT

"""exthost.* - what holds an extension package: the image's sandbox platform."""
import json
import os
import re
import signal
import subprocess
import sys
import time

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
        # The kernel reports a task frozen before its last way into
        # schedule() is charged, so the window opens once that has landed.
        ctx.sleep(0.2)
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


# ------------------------------------------------- the armed window

def _frozen(id_):
    ev = _read("%s/%s/cgroup.events" % (POOL_GROUP, id_))
    return "frozen 1" in ev if ev else None


@test("exthost.armed-freeze", title="A package's service is frozen for the armed window",
      subsystem="exthost", kind="operator", hardware="takeover", est_min=6,
      covers=[("forgeext", "src/super.*"), ("forgeext", "src/run.*"), ("forgeext", "src/machine.*"),
              ("forgeext", "src/cgroup.*"), ("forgectrl", "src/cool.*"), ("forgectrl", "src/cam.*"),
              ("forgectrl", "src/main.c")],
      requires=["exthost.platform"], actions=["button"],
      steps=[OFFLINE_STEP,
             "Bed clear (the job is dark: a 30 s square at S0, nothing fires). Press the button "
             "when it lights."],
      description="The reference package runs, its heartbeat advancing twice a second, and a dark "
                  "cloud print opens a real armed window over it: the cloud suite's dark-print "
                  "helper, whose latch checks still run (locked at the button, unlocked for the run, "
                  "locked after it), and the print completes. Sampled five times a second from before the button to after the end: the "
                  "engine's armed flag, the group's frozen state as the kernel reports it, and the "
                  "heartbeat. The window is open for at least 10 s. From 2 s after it opens to its "
                  "close the group reads frozen in every sample and the heartbeat does not move, the "
                  "freeze is in place before the run starts (the latch unlocks only for the run), and "
                  "within 3 s of the close the group is thawed and the heartbeat advances again. The "
                  "service is the same process throughout. A background capture is refused in words "
                  "while the window is open and served again once it closes, which is the other half "
                  "of keeping a package off the step stream: a capture costs kernel-side work that a "
                  "thread priority does not cover. The package, the key, the setting and the setup "
                  "record are put back as found, the record under a forgectrl restart.")
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
