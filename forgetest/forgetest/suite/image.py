# Copyright 2026 514 LLC d/b/a OpenGlow
# Written by Scott Wiederhold
# https://community.openglow.org
# SPDX-License-Identifier:    MIT

"""image.* - the post-flash health of the running image (always-required)."""
import gzip
import mmap
import os
import struct
import re
import stat

from ..catalog import test
from .. import hw


WDOG1_BASE = 0x020BC000     # i.MX6 WDOG1; WCR is the 16-bit word at offset 0


def _wdog1_wcr():
    """WDOG1's control register, read through /dev/mem (MMIO stays readable
    under STRICT_DEVMEM), or None."""
    try:
        with open("/dev/mem", "r+b") as f:
            m = mmap.mmap(f.fileno(), 4096, offset=WDOG1_BASE)
            try:
                return struct.unpack_from("<H", m, 0)[0]
            finally:
                m.close()
    except (OSError, ValueError):
        return None


def _plain(line):
    """A line without its ANSI color sequences: what a terminal shows,
    counted in columns rather than in bytes."""
    return re.sub(r"\x1b\[[0-9;]*m", "", line)


def _read(path, default=None):
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as f:
            return f.read()
    except OSError:
        return default


def kernel_config():
    """The running kernel's config as a dict, from /proc/config.gz or
    /boot/config-<release>. None when neither is available."""
    try:
        with gzip.open("/proc/config.gz", "rb") as f:
            text = f.read().decode("utf-8", "replace")
    except OSError:
        text = None
    if text is None:
        rel = _read("/proc/sys/kernel/osrelease", "").strip()
        text = _read("/boot/config-%s" % rel) if rel else None
    if text is None:
        return None
    cfg = {}
    for line in text.splitlines():
        m = re.match(r"^(CONFIG_[A-Z0-9_]+)=(.*)$", line)
        if m:
            cfg[m.group(1)] = m.group(2)
        m = re.match(r"^# (CONFIG_[A-Z0-9_]+) is not set$", line)
        if m:
            cfg[m.group(1)] = "n"
    return cfg


def kernel_ident(name):
    """A kernel release or modules-directory name less its LOCALVERSION_AUTO
    hash (+g<hash> or -g<hash>). The hash does not reproduce across a
    re-patch of the same source; the manifest lists modules directories
    without it, and the kernel's identity is its @srcrev and @config."""
    return re.sub(r"[+-]g[0-9a-f]{7,}$", "", name)


def kernel_matches(release, modules_dirs):
    """True when the running release names one of the manifest's modules
    directories, hashes aside (a manifest without them constrains nothing)."""
    return not modules_dirs or kernel_ident(release) in {kernel_ident(m) for m in modules_dirs}


def _dt_u32(path):
    """A device-tree cell as an int (big-endian), or None."""
    try:
        with open(path, "rb") as f:
            raw = f.read(4)
    except OSError:
        return None
    return int.from_bytes(raw, "big") if len(raw) == 4 else None


def fds_of(pid):
    out = []
    try:
        for fd in os.listdir("/proc/%d/fd" % pid):
            try:
                out.append(os.readlink("/proc/%d/fd/%s" % (pid, fd)))
            except OSError:
                pass
    except OSError:
        pass
    return out


@test("image.health", title="Post-flash image health", subsystem="image", kind="auto",
      always=True, est_min=1,
      covers=[("forgectrl", "init/**"), ("forgeext", "init/**"), ("forgectrl", "src/main.c"), ("forgectrl", "src/auth.c"),
              ("forgectrl", "src/status.*"),
              ("forgectrl", "CMakeLists.txt"), ("grblhal-glowforge", "src/boards/**"),
              ("grblhal-glowforge", "CMakeLists.txt"), ("kernel-module-glowforge", "**"),
              ("linux-fslc", "**")],
      description="The image that is running is the image the manifest describes, with the "
                  "version stamped in /etc/forgefirm-version and under the machine mark in the "
                  "console banner and the motd, "
                  "the kernel options, the module, the pulse ring it maps and the SDMA clocks it holds, "
                  "the daemon ownership, the board temperatures /status reports (the chassis and "
                  "the SoC die in a plausible range, the supply as a 10-bit count, the CPU "
                  "unthrottled), "
                  "the extension host as one process on a machine with no package installed and "
                  "nothing running under a pool account, no userspace watchdog running, "
                  "the data directory open for search on a machine with an account, the file modes the release depends on, and the mounts: the "
                  "rootfs read-only, /data writable, the account files and the banner rendered "
                  "into tmpfs, the sshd host keys on /data, the dev image's factory slots read-only. "
                  "And the boot does not wait on the network: ifup has returned for wlan0 (it is "
                  "in the state file and no ifup is running) and the console login is up; each "
                  "DHCP client runs with -b and has left ifup (its parent is pid 1); no DHCPv6 "
                  "client is running; and IPv6 is the "
                  "kernel's own: enabled on wlan0, router advertisements accepted, a link-local "
                  "address up (a global address is logged, not checked).")
def image_health(ctx):
    ev = ctx.evidence
    manifest = ctx.runner.manifest

    # 1. version stamp: the machine-readable file, and the two a person
    # reads - the console banner (/etc/issue, which forgefirm-banner
    # renders from the image's own text) and the motd a login prints.
    # Each carries the machine mark with the version on the mark's own
    # last line, right-justified to the mark's last column.
    ver = (_read("/etc/forgefirm-version", "") or "").strip()
    ev["forgefirm_version"] = ver
    ctx.log("forgefirm-version: %s (manifest: %s)", ver, manifest.version)
    ctx.check(ver == manifest.version, "/etc/forgefirm-version %r != manifest %r", ver, manifest.version)
    for path in ("/etc/issue", "/etc/motd"):
        text = _read(path, "") or ""
        ev[path] = text
        lines = text.splitlines()
        at = [i for i, line in enumerate(lines) if line.rstrip().endswith(ver)]
        ctx.check(at, "%s carries no line ending in the version %r", path, ver)
        if not at:
            continue
        i = at[0]
        ctx.check(lines[i].rstrip() == lines[i], "%s pads the version line: %r", path, lines[i])
        ctx.check(_plain(lines[i]).strip() != ver,
                  "%s puts the version on a line of its own, not on the mark's last line", path)
        # Right-justified to the mark's last column: the stamped line is
        # exactly as wide as the widest line above it. Only the motd is
        # measured, because /etc/issue holds the mark with its
        # backslashes doubled for the getty that reads it, so its bytes
        # are wider than the columns a person sees.
        if path == "/etc/motd":
            above = [len(_plain(line)) for line in lines[:i] if line.strip()]
            width = max(above) if above else 0
            ev["motd_columns"] = [len(_plain(lines[i])), width]
            ctx.check(len(_plain(lines[i])) == width,
                      "/etc/motd stamped line is %d columns, the mark above is %d",
                      len(_plain(lines[i])), width)

    # 2. kernel options
    cfg = kernel_config()
    ctx.check(cfg is not None, "kernel config unavailable (/proc/config.gz, /boot/config-*)")
    for opt, want in (("CONFIG_PREEMPT", ("y",)), ("CONFIG_IMX2_WDT", ("y", "m")),
                      ("CONFIG_PANIC_ON_OOPS", ("y",))):
        val = cfg.get(opt, "n")
        ev[opt] = val
        ctx.log("%s=%s", opt, val)
        ctx.check(val in want, "%s=%s, expected one of %s", opt, val, want)
    # The boot-armed hardware watchdog: U-Boot arms WDOG1 at 60 s and the
    # kernel's core keeps it fed (no userspace opens it, so the sysfs state
    # reads "inactive": that attribute says whether a process holds the
    # device, not whether the hardware runs). The hardware's own word is
    # WCR: WDE set, and WT giving the period. The sysfs view needs
    # CONFIG_WATCHDOG_SYSFS; bootstatus carries the last reset's cause (32 =
    # the watchdog's timeout).
    wd = {k: (_read("/sys/class/watchdog/watchdog0/" + k) or "").strip()
          for k in ("identity", "state", "timeout", "bootstatus")}
    wcr = _wdog1_wcr()
    wd["wcr"] = ("%#06x" % wcr) if wcr is not None else None
    wd["wde"] = bool(wcr & 0x4) if wcr is not None else None
    wd["period_s"] = ((wcr >> 8) + 1) / 2.0 if wcr is not None else None
    ev["watchdog"] = wd
    ctx.log("watchdog0: %s", wd)
    ctx.check(wd["identity"], "watchdog0 has no sysfs view (CONFIG_WATCHDOG_SYSFS)")
    ctx.check(wd["timeout"] == "60", "watchdog0 timeout %r, expected 60 s", wd["timeout"] or None)
    ctx.check(wcr is not None, "WDOG1 WCR unreadable through /dev/mem")
    ctx.check(wd["wde"], "WDOG1 is not enabled (WCR %s): nothing resets a hung kernel", wd["wcr"])
    ctx.check(wd["period_s"] == 60.0, "WDOG1 period %s s (WCR %s), expected 60", wd["period_s"], wd["wcr"])
    ctx.check(wd["state"] == "inactive", "watchdog0 is %r: a process holds the device, which the image "
              "does not do", wd["state"] or None)
    rel = _read("/proc/sys/kernel/osrelease", "").strip()
    ev["kernel_release"] = rel
    mods = manifest.platform.get("kernel_modules") or []
    ctx.log("kernel release %s (manifest modules dirs: %s)", rel, ",".join(mods))
    ctx.check(kernel_matches(rel, mods), "running kernel %r is not the manifest's %s", rel, mods)

    # 3. the module and its sysfs
    ctx.check(os.path.isdir("/sys/module/glowforge"), "glowforge.ko is not loaded")
    state = hw.sysfs_read("cnc/state")
    ev["cnc_state"] = state
    ctx.check(state is not None, "/sys/glowforge/cnc/state unreadable")
    ctx.log("cnc/state: %s", state)
    free = hw.sysfs_int("cnc/free")
    ev["cnc_free"] = free
    ctx.check(free is not None and free > 0, "cnc/free unreadable or zero (ring not mapped?)")
    ctx.log("cnc/free: %s bytes", free)

    # The ring the platform ships: the DT pool is the promise, ring_mb is what
    # the module took. They must agree - a pool the parameter does not use is
    # no-map RAM burned for nothing, and a parameter the pool cannot back would
    # have failed the probe. free can only ever be size less the 32 KiB gap.
    pool = _dt_u32("/proc/device-tree/reserved-memory/cnc-pulsebuf/size")
    try:
        ring = int((_read("/sys/module/glowforge/parameters/ring_mb", "") or "").strip()) << 20
    except ValueError:
        ring = None
    ev["cnc_pool_bytes"] = pool
    ev["cnc_ring_bytes"] = ring
    ctx.log("cnc-pulsebuf pool: %s bytes, ring_mb: %s bytes", pool, ring)
    ctx.check(pool is not None, "no cnc-pulsebuf reserved-memory node (the ring fell back to CMA?)")
    ctx.check(ring is not None, "/sys/module/glowforge/parameters/ring_mb unreadable")
    ctx.check(pool is None or ring is None or pool == ring,
              "DT pool %s and ring_mb %s disagree", pool, ring)
    ctx.check(ring is None or free is None or free <= ring - 32 * 1024,
              "cnc/free %s exceeds the ring less its 32 KiB gap (%s)", free, ring)
    ctx.check(hw.sysfs_read("cnc/interlock_circuit") is not None, "cnc/interlock_circuit unreadable")

    # The SDMA engine runs only while a channel holder keeps its ipg/ahb
    # clocks enabled; imx-sdma leaves them off after probe and only a channel
    # allocation turns them on. glowforge.ko takes its channel outside
    # dmaengine and holds the clocks itself. With the block gated every
    # channel-0 transfer is a silent no-op: the probe cannot start, and the
    # ring reads back a stale bounce page (free above the ring size, a
    # position counter that never moved).
    clk = (_read("/sys/kernel/debug/clk/sdma/clk_enable_count", "") or "").strip()
    ev["sdma_clk_enable_count"] = clk
    ctx.log("sdma clk_enable_count: %s", clk or "(unreadable)")
    ctx.check(clk.isdigit() and int(clk) >= 1,
              "SDMA clk_enable_count %r: the engine's ipg/ahb clocks are not held", clk)

    # 4. forgectrl holds /dev/glowforge and supervises the controller
    pids = hw.pidof("forgectrl")
    ev["forgectrl_pids"] = pids
    ctx.check(pids, "forgectrl is not running")
    holders = [p for p in pids if any(l == "/dev/glowforge" for l in fds_of(p))]
    ev["pulse_device_holders"] = holders
    ctx.log("forgectrl pids %s, holding /dev/glowforge: %s", pids, holders)
    ctx.check(holders, "no forgectrl process holds /dev/glowforge")
    st, mode = ctx.forgectrl.get("/mode")
    ev["mode"] = mode
    ctx.check(st == 200 and isinstance(mode, dict), "GET /mode -> %s", st)
    ctx.log("mode: %s", mode)
    ctx.check(mode.get("controller") in ("running", "standby"),
              "controller is %r (expected running or standby)", mode.get("controller"))
    ctx.check(mode.get("motion") != "fault", "supervisor reports a motion fault")

    # 4b. the board temperatures /status carries, watched and not gated:
    # the chassis and the SoC die in degrees, the supply as its raw count,
    # and the CPU unthrottled at idle.
    temps = (ctx.forgectrl.status() or {}).get("temps") or {}
    ev["temps"] = temps
    ctx.log("temps: %s", temps)
    ctx.check(isinstance(temps.get("chassis_c"), (int, float)) and 5.0 <= temps["chassis_c"] <= 80.0,
              "/status temps.chassis_c is not a plausible chassis temperature: %s", temps)
    ctx.check(isinstance(temps.get("soc_c"), (int, float)) and 10.0 <= temps["soc_c"] <= 100.0,
              "/status temps.soc_c is not a plausible die temperature: %s", temps)
    ctx.check(isinstance(temps.get("supply_raw"), int) and 0 <= temps["supply_raw"] <= 1023,
              "/status temps.supply_raw is not a 10-bit count: %s", temps)
    ctx.check(temps.get("soc_throttle") == 0,
              "the CPU is throttled (or its cooling device is missing) at idle: %s", temps)

    # 5. the extension host: one process, and a campaign's machine is
    # extension-free (its init order is the rootfs lint's)
    host = []
    for pid in hw.pidof("forgeext"):
        argv = _read("/proc/%d/cmdline" % pid, "").split("\0")
        if argv[:2] == [hw.FORGEEXT, "run"]:
            host.append(pid)
    ev["extension_host_pids"] = host
    ctx.check(len(host) == 1, "the extension host is not one running process: %s", host)
    ev["extension_packages"] = hw.ext_packages()
    ev["pool_processes"] = len(hw.pool_pids())
    ctx.log("extension host pid %s, packages %s, pool processes %d", host, ev["extension_packages"], ev["pool_processes"])
    ctx.check(not ev["extension_packages"], "a campaign wants an extension-free machine: installed %s",
              ev["extension_packages"])
    ctx.check(not ev["pool_processes"], "%d process(es) run under extension pool accounts", ev["pool_processes"])

    # 6. no userspace watchdog daemon running (the logging init and the
    # absent watchdog init script are the rootfs lint's); kernel threads
    # (the imx2_wdt kthread is expected) have an empty cmdline
    user_wd = [p for p in hw.pidof("watchdog") if _read("/proc/%d/cmdline" % p, "")]
    ev["userspace_watchdog_pids"] = user_wd
    ctx.check(not user_wd, "a userspace watchdog daemon is running: %s", user_wd)

    # 7. file modes and space
    for path in ("/data/forgefirm/panel.token", "/data/forgefirm/forgefirm.conf"):
        if os.path.exists(path):
            m = stat.S_IMODE(os.stat(path).st_mode)
            ev[path] = "%o" % m
            ctx.log("%s mode %o", path, m)
            ctx.check(m == 0o600, "%s mode %o, expected 600", path, m)
    # an operator account walks through the data directory to its home, and a
    # package's account to its files: forgefirm-users opens it for search at
    # every render, whoever made it and under whatever umask
    if os.path.exists("/data/forgefirm/users"):
        m = stat.S_IMODE(os.stat("/data/forgefirm").st_mode)
        ev["/data/forgefirm"] = "%o" % m
        ctx.log("/data/forgefirm mode %o", m)
        ctx.check(m & 0o011 == 0o011, "/data/forgefirm mode %o: no account can walk through it to its home", m)
    if os.path.isdir("/data"):
        s = os.statvfs("/data")
        free_mb = s.f_bavail * s.f_frsize // (1024 * 1024)
        ev["data_free_mb"] = free_mb
        ctx.log("/data free: %d MiB", free_mb)
        ctx.check(free_mb >= 20, "/data has only %d MiB free", free_mb)

    # 8. the mounts: the rootfs read-only, /data the writable partition,
    # the state a read-only rootfs hands off (the read-only-rootfs image
    # feature, forgefirm-users, forgefirm-hostname, forgefirm-banner, the
    # sshd host keys). The dev image's factory slots under /factory are
    # read-only; rcS's ROOTFS_READ_ONLY and the release fstab with no
    # factory slot are the rootfs lint's.
    mounts = {}
    for line in (_read("/proc/mounts", "") or "").splitlines():
        parts = line.split()
        if len(parts) >= 4:
            mounts[parts[1]] = {"source": parts[0], "type": parts[2], "opts": parts[3].split(",")}

    def mount_opts(path):
        return (mounts.get(path) or {}).get("opts") or []

    ev["mounts"] = {p: mounts[p] for p in ("/", "/data", "/var/lib", "/etc/passwd",
                                           "/etc/hostname", "/etc/issue") if p in mounts}
    ctx.log("/ mounted %s; /data %s; /var/lib %s", ",".join(mount_opts("/")) or "(absent)",
            ",".join(mount_opts("/data")) or "(absent)", ",".join(mount_opts("/var/lib")) or "(absent)")
    ctx.check("ro" in mount_opts("/"), "the rootfs is not mounted read-only: %s", mounts.get("/"))
    ctx.check("rw" in mount_opts("/data"), "/data is not mounted read-write: %s", mounts.get("/data"))
    ctx.check("rw" in mount_opts("/var/lib"),
              "/var/lib is not a writable copy (read-only-rootfs-hook.sh): %s", mounts.get("/var/lib"))
    factory = sorted(p for p in mounts if p.startswith("/factory/"))
    ev["factory_mounts"] = factory
    dev_image = os.path.exists("/etc/forgefirm-dev")
    ev["dev_image"] = dev_image
    ctx.log("dev image %s; /factory mounts: %s", dev_image, factory or "none")
    if dev_image:
        for n in (1, 2):
            if os.path.exists("/dev/mmcblk2p%d" % n):
                p = "/factory/img%d" % n
                ctx.check("ro" in mount_opts(p), "%s is not mounted read-only on the dev image: %s", p, mounts.get(p))
    if os.path.isfile("/data/forgefirm/users"):
        names = [l.split(":")[0] for l in (_read("/data/forgefirm/users", "") or "").splitlines()
                 if l.strip() and not l.startswith("#")]
        passwd_names = {l.split(":")[0] for l in (_read("/etc/passwd", "") or "").splitlines()}
        ev["record_accounts"] = names
        for f in ("/etc/passwd", "/etc/shadow", "/etc/group"):
            ctx.check(f in mounts, "%s is not the tmpfs render of the account record", f)
        for n in names:
            ctx.check(n in passwd_names, "record account %r is missing from /etc/passwd", n)
    ctx.check("/etc/hostname" in mounts, "/etc/hostname is not the bind-mounted name copy")
    ctx.check("/etc/issue" in mounts, "/etc/issue is not the bind-mounted banner copy")
    if hw.pidof("sshd"):
        key = "/data/forgefirm/ssh/ssh_host_ed25519_key"
        ctx.check(os.path.isfile(key), "sshd runs but %s is missing", key)

    # 9. the manifest itself is coherent
    ctx.check(manifest.content_sha and len(manifest.content_sha) == 64, "manifest content_sha256 missing")
    ctx.check("kernel-module-glowforge" in manifest.components, "manifest lacks kernel-module-glowforge")
    ctx.check("linux-fslc" in manifest.components, "manifest lacks the kernel entry")
    ctx.log("manifest %s: %d components", manifest.content_sha[:12], len(manifest.components))

    # 10. the network boot. init starts the rest of the boot, the console
    # login last, only after the networking script returns, so a client
    # that waits in ifup's foreground on a server's answer holds the
    # whole machine. The interfaces file starts no DHCPv6 client
    # (test_image.py reads the layer's file); the bounded DHCP client
    # (-b, which leaves ifup after three unanswered discovers) is checked
    # here, on the client that runs.
    procs = _procs()
    up = ifstate_names(_read(IFSTATE, ""))
    ev["ifstate"] = sorted(up)
    ctx.check("wlan0" in up, "ifup has not finished wlan0: %s holds %s", IFSTATE, sorted(up))
    stuck = [p for p in procs if p[2] in ("ifup", "ifdown")]
    ev["ifup_running"] = [p[3] for p in stuck]
    ctx.check(not stuck, "ifup is still running: %s", [p[3] for p in stuck])
    logins = [p[3] for p in procs if p[2] == "getty"]
    ev["getty"] = logins
    ctx.check(any("ttymxc0" in c for c in logins), "no console login is running: %s", logins)
    v4 = [p for p in procs if p[2] == "udhcpc"]
    ev["udhcpc"] = [p[3] for p in v4]
    ctx.check(v4, "no DHCP client is running")
    for pid, ppid, _comm, cmd in v4:
        ctx.check("-b" in cmd.split(), "the DHCP client runs without -b: %s", cmd)
        ctx.check(ppid == 1, "the DHCP client (pid %d) is still a child of pid %d", pid, ppid)
    # no DHCPv6 client running (none on the image is the rootfs lint's)
    alive = [p[3] for p in procs if p[2] in DHCP6_CLIENTS]
    ev["dhcp6_running"] = alive
    ctx.check(not alive, "a DHCPv6 client is running: %s", alive)
    # IPv6 is the kernel's own. A global address is evidence only: a
    # network whose router advertisement offers no SLAAC prefix gives none.
    conf = "/proc/sys/net/ipv6/conf/wlan0/"
    ev["disable_ipv6"] = (_read(conf + "disable_ipv6", "") or "").strip()
    ev["accept_ra"] = (_read(conf + "accept_ra", "") or "").strip()
    ctx.check(ev["disable_ipv6"] == "0", "IPv6 is off on wlan0 (disable_ipv6=%r)", ev["disable_ipv6"])
    ctx.check(ev["accept_ra"] not in ("", "0"),
              "wlan0 does not accept router advertisements (accept_ra=%r)", ev["accept_ra"])
    local, world = link_addrs(_read("/proc/net/if_inet6", ""), "wlan0")
    ev["link_local"] = local
    ev["global"] = world
    ctx.check(local, "wlan0 has no link-local IPv6 address")
    ctx.log("wlan0: %d link-local, %d global IPv6 address(es)", len(local), len(world))




# ------------------------------------------------------- the network boot

IFSTATE = "/var/run/ifstate"                # busybox ifupdown's state file
INTERFACES = "/etc/network/interfaces"
DHCP6_CLIENTS = ("udhcpc6", "dhcp6c", "odhcp6c")


def ifstate_names(text):
    """The interfaces ifup finished configuring: one name=name line each
    in busybox's state file, written when ifup returns for the name."""
    return {line.split("=", 1)[0].strip() for line in (text or "").splitlines() if "=" in line}


def dhcp6_in_interfaces(text):
    """The lines of an interfaces file that start a DHCPv6 client: an
    inet6 stanza with a method that runs one, or a command that names
    one. A comment starts nothing."""
    bad = []
    for raw in (text or "").splitlines():
        words = raw.split("#", 1)[0].split()
        if not words:
            continue
        if words[0] == "iface":
            if len(words) >= 4 and words[2] == "inet6" and words[3] in ("dhcp", "auto"):
                bad.append(" ".join(words))
        elif any(w.split("/")[-1] in DHCP6_CLIENTS for w in words[1:]):
            bad.append(" ".join(words))
    return bad


def parse_stat(text):
    """(pid, comm, ppid) from a /proc/<pid>/stat line, or None. The comm
    sits in parentheses and may itself hold spaces and parentheses."""
    try:
        head, tail = text.split("(", 1)
        comm, rest = tail.rsplit(")", 1)
        return int(head), comm, int(rest.split()[1])
    except (ValueError, IndexError):
        return None


def _procs():
    """(pid, ppid, comm, command line) of every process."""
    out = []
    for name in os.listdir("/proc"):
        if not name.isdigit():
            continue
        st = parse_stat(_read("/proc/%s/stat" % name, ""))
        if st is None:
            continue
        cmd = (_read("/proc/%s/cmdline" % name, "") or "").replace("\0", " ").strip()
        out.append((st[0], st[2], st[1], cmd))
    return out


def link_addrs(text, iface):
    """(link-local, global) IPv6 addresses of one interface, as the hex
    strings of /proc/net/if_inet6 (address, index, prefix length, scope,
    flags, name; scope 20 is the link, 00 the world)."""
    local, world = [], []
    for line in (text or "").splitlines():
        f = line.split()
        if len(f) == 6 and f[5] == iface:
            if f[3] == "20":
                local.append(f[0])
            elif f[3] == "00":
                world.append(f[0])
    return local, world


