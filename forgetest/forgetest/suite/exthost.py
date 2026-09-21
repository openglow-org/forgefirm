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
REF_ID = "org.forgetest.reference"
REF_KEY = "forgetest-reference"
REF_DEST = "192.0.2.1"                      # TEST-NET-1: declared so that port 443 is, and never dialed

# The reference package's service. It looks at its own confinement from the
# inside, leaves what it found in its data directory, and stays up.
REF_SERVICE = r'''
import errno, json, os, socket, sys, time
lan = sys.argv[1]
data, pkg = os.environ["FFX_DATA"], os.environ["FFX_PKG"]


def word(e):
    return errno.errorcode.get(e.errno, str(e.errno))


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
}
with open(os.path.join(data, "report.json.new"), "w") as f:
    json.dump(report, f)
os.rename(os.path.join(data, "report.json.new"), os.path.join(data, "report.json"))
print("reference service up", flush=True)
n = 0
while True:
    n += 1
    with open(os.path.join(data, "beat"), "w") as f:
        f.write(str(n))
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


def _pack_reference(work, lan):
    """The reference package, signed with a key made here: (archive, public key)."""
    import io
    import tarfile
    manifest = {"manifest": 1, "id": REF_ID, "name": "forgetest reference", "version": "1.0.0",
                "author": "forgetest", "license": "MIT", "api": "0.1", "runtime": "python",
                "service": {"exec": "bin/reference.py", "args": [lan or "-"]},
                "capabilities": ["net.outbound:%s:443" % REF_DEST, "storage:1"]}
    payload = os.path.join(work, "payload.tar.gz")
    with tarfile.open(payload, "w:gz") as t:
        for name, text, mode in (("manifest.json", json.dumps(manifest), 0o644),
                                 ("bin/reference.py", REF_SERVICE, 0o755)):
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
                  "cannot open a netlink socket; its output is in the forgeext log under its id. Safe mode "
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
        fixed = {"PATH", "LANG", "HOME", "TMPDIR", "FFX_ID", "FFX_PKG", "FFX_DATA", "PYTHONDONTWRITEBYTECODE",
                 "PYTHONUNBUFFERED"}
        ctx.check(rep.get("cwd") == os.path.join(EXT_ROOT, "data", REF_ID) and set(rep.get("env") or []) - {"LC_CTYPE"} == fixed,
                  "its working directory or its environment is not the fixed one: %s %s", rep.get("cwd"), rep.get("env"))
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
        if os.path.exists(SAFE_FILE):
            os.remove(SAFE_FILE)
        st, reply = fc.post("/settings", data={"ext_enabled": "0"})
        r = _forgeext("remove", REF_ID)
        ctx.log("remove %s -> %s", REF_ID, "ok" if r.get("ok") else r.get("error"))
        if os.path.exists(owner_key):
            os.remove(owner_key)
        shutil.rmtree(work, ignore_errors=True)
        if prior == "1":
            st, reply = fc.post("/settings", data={"ext_enabled": "1", "advisory": etag, "phrase": SAFETY_PHRASE})
        elif prior == "":
            st, reply = fc.post("/settings", params={"ext_enabled": ""})
        ctx.log("restore ext_enabled=%r -> %s", prior, st)
        if prior != "1":
            os.chmod(data_dir, dir_mode)
        with ctx.takeover():
            write_file(record_path(), raw)
        ctx.log("the previous record is back under a restart")

    ctx.check((fc.settings().get("ext_enabled") or "") == prior, "ext_enabled not restored: %r, was %r",
              fc.settings().get("ext_enabled"), prior)
    ctx.check(read_file(record_path()) == raw, "the setup record on disk is not the one found")
    ctx.check(prior == "1" or os.stat(data_dir).st_mode & 0o7777 == dir_mode, "the data directory's mode is not the one found")
    left = _tree(EXT_ROOT)
    ctx.check(left == found_tree, "the extension root is not as found: %s", sorted(set(left) ^ set(found_tree)))
    ctx.check(len(_host_pids()) == 1, "the extension host is not running at the end")

