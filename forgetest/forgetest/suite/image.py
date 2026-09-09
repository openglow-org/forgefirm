"""image.* - the post-flash health of the running image (always-required)."""
import glob
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
      covers=[("forgectrl", "init/**"), ("forgectrl", "src/main.c"), ("forgectrl", "src/auth.c"),
              ("forgectrl", "CMakeLists.txt"), ("grblhal-glowforge", "src/boards/**"),
              ("grblhal-glowforge", "CMakeLists.txt"), ("kernel-module-glowforge", "**"),
              ("linux-fslc", "**")],
      description="The image that is running is the image the manifest describes, with the "
                  "kernel options, the module, the pulse ring it maps and the SDMA clocks it holds, "
                  "the daemon ownership, "
                  "the init ordering, the file modes the release depends on, and the mounts: the "
                  "rootfs read-only, /data writable, the account files and the banner rendered "
                  "into tmpfs, the sshd host keys on /data, the factory slots on the dev image only.")
def image_health(ctx):
    ev = ctx.evidence
    manifest = ctx.runner.manifest

    # 1. version stamp
    ver = (_read("/etc/forgefirm-version", "") or "").strip()
    ev["forgefirm_version"] = ver
    ctx.log("forgefirm-version: %s (manifest: %s)", ver, manifest.version)
    ctx.check(ver == manifest.version, "/etc/forgefirm-version %r != manifest %r", ver, manifest.version)

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

    # 5. init ordering: controllers stop before the daemon
    k = sorted(os.path.basename(p) for p in glob.glob("/etc/rc6.d/K*"))
    ev["rc6_kill"] = k
    kg = [x for x in k if "grblhal" in x]
    kf = [x for x in k if x.endswith("forgectrl")]
    ctx.log("rc6.d: %s", " ".join(k))
    ctx.check(kg and kf, "rc6.d lacks the grblhal/forgectrl kill links")
    ctx.check(kg[0] < kf[0], "controller kill link %s must sort before forgectrl's %s", kg[0], kf[0])

    # 6. logging lever present, no userspace watchdog daemon
    logging = [n for n in ("forgefirm-logging", "forgefirm-logrotate") if os.path.exists("/etc/init.d/" + n)]
    ev["logging_init"] = logging
    ctx.check(logging, "no ForgeFIRM logging/logrotate init script")
    ctx.check(not os.path.exists("/etc/init.d/watchdog"), "a userspace watchdog init script is present")
    # kernel threads (the imx2_wdt kthread is expected) have an empty cmdline
    user_wd = [p for p in hw.pidof("watchdog") if _read("/proc/%d/cmdline" % p, "")]
    ev["userspace_watchdog_pids"] = user_wd
    ctx.check(not user_wd, "a userspace watchdog daemon is running: %s", user_wd)

    # 7. file modes and space
    for path in ("/data/forgefirm/panel.token", "/data/forgefirm.conf"):
        if os.path.exists(path):
            m = stat.S_IMODE(os.stat(path).st_mode)
            ev[path] = "%o" % m
            ctx.log("%s mode %o", path, m)
            ctx.check(m == 0o600, "%s mode %o, expected 600", path, m)
    if os.path.isdir("/data"):
        s = os.statvfs("/data")
        free_mb = s.f_bavail * s.f_frsize // (1024 * 1024)
        ev["data_free_mb"] = free_mb
        ctx.log("/data free: %d MiB", free_mb)
        ctx.check(free_mb >= 20, "/data has only %d MiB free", free_mb)

    # 8. the mounts: the rootfs read-only, /data the writable partition,
    # the state a read-only rootfs hands off (the read-only-rootfs image
    # feature, forgefirm-users, forgefirm-banner, the sshd host keys). The
    # dev image alone mounts the factory slots under /factory.
    mounts = {}
    for line in (_read("/proc/mounts", "") or "").splitlines():
        parts = line.split()
        if len(parts) >= 4:
            mounts[parts[1]] = {"source": parts[0], "type": parts[2], "opts": parts[3].split(",")}

    def mount_opts(path):
        return (mounts.get(path) or {}).get("opts") or []

    ev["mounts"] = {p: mounts[p] for p in ("/", "/data", "/var/lib", "/etc/passwd", "/etc/issue") if p in mounts}
    ctx.log("/ mounted %s; /data %s; /var/lib %s", ",".join(mount_opts("/")) or "(absent)",
            ",".join(mount_opts("/data")) or "(absent)", ",".join(mount_opts("/var/lib")) or "(absent)")
    ctx.check("ro" in mount_opts("/"), "the rootfs is not mounted read-only: %s", mounts.get("/"))
    ctx.check("rw" in mount_opts("/data"), "/data is not mounted read-write: %s", mounts.get("/data"))
    ctx.check("rw" in mount_opts("/var/lib"),
              "/var/lib is not a writable copy (read-only-rootfs-hook.sh): %s", mounts.get("/var/lib"))
    rcs = _read("/etc/default/rcS", "") or ""
    ctx.check("ROOTFS_READ_ONLY=yes" in rcs.splitlines(), "/etc/default/rcS lacks ROOTFS_READ_ONLY=yes")
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
    else:
        ctx.check(not factory, "a release image mounts the factory slots: %s", factory)
    if os.path.isfile("/data/forgefirm/users"):
        names = [l.split(":")[0] for l in (_read("/data/forgefirm/users", "") or "").splitlines()
                 if l.strip() and not l.startswith("#")]
        passwd_names = {l.split(":")[0] for l in (_read("/etc/passwd", "") or "").splitlines()}
        ev["record_accounts"] = names
        for f in ("/etc/passwd", "/etc/shadow", "/etc/group"):
            ctx.check(f in mounts, "%s is not the tmpfs render of the account record", f)
        for n in names:
            ctx.check(n in passwd_names, "record account %r is missing from /etc/passwd", n)
    ctx.check("/etc/issue" in mounts, "/etc/issue is not the bind-mounted banner copy")
    if hw.pidof("sshd"):
        key = "/data/forgefirm/ssh/ssh_host_ed25519_key"
        ctx.check(os.path.isfile(key), "sshd runs but %s is missing", key)

    # 9. the manifest itself is coherent
    ctx.check(manifest.content_sha and len(manifest.content_sha) == 64, "manifest content_sha256 missing")
    ctx.check("kernel-module-glowforge" in manifest.components, "manifest lacks kernel-module-glowforge")
    ctx.check("linux-fslc" in manifest.components, "manifest lacks the kernel entry")
    ctx.log("manifest %s: %d components", manifest.content_sha[:12], len(manifest.components))


@test("image.license-bundle", title="The license texts travel with the image",
      subsystem="image", kind="auto", est_min=1,
      covers=[("forgectrl", "src/main.c"), ("forgectrl", "src/ui/index.html"),
              ("forgectrl", "src/ui/wizard.html"), ("forgectrl", "src/ui/login.html")],
      requires=["forgectrl.panel-serves"],
      description="The image carries /usr/share/forgefirm/licenses.tar.gz, a gzip tar with "
                  "the license manifest (every installed package with its license) and the "
                  "license texts, and no loose common-licenses tree; the panel serves the "
                  "bundle, the manifest as text, and the Licenses page every footer links, "
                  "each without a login.")
def license_bundle(ctx):
    import io
    import tarfile
    fc = ctx.forgectrl
    ev = ctx.evidence
    path = "/usr/share/forgefirm/licenses.tar.gz"
    ctx.check(os.path.isfile(path), "%s is missing", path)
    ctx.check(not os.path.isdir("/usr/share/common-licenses"),
              "the loose common-licenses tree is still on the rootfs")
    size = os.path.getsize(path)
    ev["bundle_bytes"] = size
    ctx.log("%s: %d bytes", path, size)
    with tarfile.open(path, "r:gz") as t:
        names = t.getnames()
        ctx.check("common-licenses/license.manifest" in names, "no license.manifest in the bundle")
        manifest = t.extractfile("common-licenses/license.manifest").read().decode("utf-8", "replace")
    packages = manifest.count("PACKAGE NAME: ")
    texts = sum(1 for n in names if "/generic_" in n and not n.endswith("/"))
    ev["packages"] = packages
    ev["generic_texts"] = texts
    ctx.log("manifest lists %d packages; %d generic license entries", packages, texts)
    ctx.check(packages >= 40, "only %d packages in the manifest", packages)
    ctx.check(texts >= 5, "only %d generic license entries", texts)
    for name in ("forgectrl", "grblhal-glowforge", "kernel-module-glowforge"):
        ctx.check("RECIPE NAME: %s\n" % name in manifest, "%s is not in the manifest", name)

    st, body = fc.get("/system/licenses/manifest", raw=True)
    ctx.check(st == 200, "GET /system/licenses/manifest -> %s", st)
    ctx.check(isinstance(body, (bytes, bytearray)) and b"PACKAGE NAME: " in body,
              "the served manifest is not the manifest")
    st, body = fc.get("/system/licenses", raw=True)
    ctx.check(st == 200, "GET /system/licenses -> %s", st)
    ctx.check(isinstance(body, (bytes, bytearray)) and len(body) == size,
              "the served bundle is %s bytes, the file %d", len(body) if body else None, size)
    with tarfile.open(fileobj=io.BytesIO(bytes(body)), mode="r:gz") as t:
        ctx.check("common-licenses/license.manifest" in t.getnames(),
                  "the served bundle holds no manifest")
    st, body = fc.get("/licenses", raw=True)
    ctx.check(st == 200, "GET /licenses -> %s", st)
    text = bytes(body).decode("utf-8", "replace") if body else ""
    ctx.check("/system/licenses" in text and "PACKAGE NAME: " in text,
              "the Licenses page lacks the download link or the manifest")
