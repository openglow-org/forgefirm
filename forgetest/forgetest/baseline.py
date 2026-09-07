"""The fresh-boot idle state: what every test starts from and leaves behind.

The runner checks the machine against this baseline before a run and
restores it after (on every exit path), so a test cannot hand the next one
- or the operator - a machine that only looks idle. Two kinds of items:

  fixed      a resting value the boot establishes and nothing at idle
             changes: kernel module defaults, forgectrl's start-up writes,
             the GRBL controller's init writes. Verified against the value,
             restored by writing it back.
  preserved  state with no resting policy that a run must hand back as it
             found it: the position counters, the settings map, the
             controller mode. Captured before the run, compared after,
             restored where the interface allows.

The lid lamp is fixed too, at forgectrl's `lid_lamp_idle` setting (unset =
236): the daemon asserts it at start and at every controller spawn.

The controller mode in force decides what the baseline owns. In GRBL mode
everything above applies. In cloud mode the cloud client owns what it
configures for itself - the GRBL controller's init values (it writes its
own from each pulse header), the lid lamp (its lid-image level), and the
position counters (re-zeroed at every service action, so not a preserved
value) - and the baseline checks the rest: the safety readbacks, the
latch, the ring, the module defaults, the diagnostics tools, the cooling
engine. The mode itself is preserved: a run hands back the mode it
found, unless it declared a deliberate change (Context.mode_changed) -
the cloud tests enter cloud mode once and stay there.

Everything found off-baseline is a "leftover": logged, kept in the run's
evidence, and surfaced on the page. The pre-run pass attributes leftovers
to the previous run; the post-run pass attributes them to the run itself.
The machine is restored either way - a leftover is a defect in the test
that made it, not a reason to hand the dirt on.
"""
import json
import os
import struct
import time

from . import hw
from .log import now_ts

# GRBL-mode resting values (kernel attribute -> value as read back), as a
# fresh boot of the dev image leaves them (2026-08-16 bench dump).
# motor_lock/x_mode/y_mode/x_decay/y_decay and the hold currents are the
# GRBL controller's init writes (glowforge_io.c): motor_lock 0, every axis
# in the pulse path (a job's Z moves the lens; the driver's Z soft limit
# guards it), step_freq its machine tick and ramp_rate the kernel stop
# ramp, both derived from the XY microstep mode (the values here are the
# x8 default's; fixed_sysfs() gives the mode's); streaming is only ever 1
# inside a live job; the head white LED is a camera lamp, off at idle; the
# loop heater and TEC are the diagnostics' tools, off at idle.
FIXED_SYSFS = [
    ("cnc/motor_lock", "0"),
    ("cnc/x_mode", "8"),
    ("cnc/y_mode", "8"),
    ("cnc/x_decay", "1"),
    ("cnc/y_decay", "1"),
    ("cnc/step_freq", "28160"),         # GFSINK_RATE_DEFAULT (grblHAL stepper_stream.c)
    ("cnc/ramp_rate", "125000"),
    ("cnc/streaming", "0"),
    ("pic/x_step_current", "33"),
    ("pic/y_step_current", "5"),
    ("head/white_led", "0"),
    ("thermal/heater_pwm", "0"),
    ("thermal/tec_on", "0"),
]

# The subset the GRBL controller writes at its start: checked and restored
# in GRBL mode only. The cloud client sets its own values for these from
# every pulse header (step_freq 10 kHz, the run currents), runs at the
# service's own x8 with the module's ramp whatever xy_microsteps says, and
# hands the hold currents back at idle; forcing the GRBL values under it
# would be the baseline configuring another controller's machine.
GRBL_CONTROLLER_SYSFS = ("cnc/motor_lock", "cnc/x_mode", "cnc/y_mode", "cnc/x_decay", "cnc/y_decay",
                         "cnc/step_freq", "cnc/ramp_rate", "pic/x_step_current", "pic/y_step_current")

# Settings the baseline never hands back as bare settings: controller_mode
# is the persisted mirror of the live mode (the mode item restores it
# through POST /mode, which keeps the two in step; a bare settings write
# would leave the runtime in one mode and the boot in the other).
UNPRESERVED_SETTINGS = ("controller_mode",)

# A cloud client the baseline starts (the mode a test declares, the mode
# or controller handed back after a run) comes up without the service's
# connect-time hunt: the marker gfcloud reads first thing at start and
# takes down (one start), so it is left in place once the start is
# accepted and removed only when the start never happened. The service
# keeps the head position it has; a test that homes or prints under the
# service starts its own client with the hunt.
NOHUNT_MARKER = os.environ.get("FORGETEST_NOHUNT_MARKER", "/run/gfcloud-nohunt")

# Read-only readbacks with their idle values (no direct restore: the state
# comes right through forgectrl - see restore_forgectrl - or is fatal).
IDLE_READBACKS = [
    ("cnc/state", "idle"),
    ("cnc/laser_enable", "0"),
    ("cnc/laser_on", "0"),
    ("cnc/faults", "0"),
]

LATCH_BIT = 0x08                    # interlock_circuit bit 3: latch locked

BUTTON_LEDS = ("button_led_1", "button_led_2", "button_led_3")

PRESERVED_SYSFS = []                # sysfs attrs captured before, restored after

LID_LAMP_ATTR = "pic/lid_led"
LID_LAMP_DEFAULT = "236"            # forgectrl's lid_lamp_idle default

BOOT_MAX_AGE_S = 600                # a boot reference is taken only this soon after boot
SETTLE_S = 150                      # the supervisor's probe + rail-off ladder
CAM_IDLE_S = 20                     # camera engine idle stop is 10 s
COOL_IDLE_S = 120                   # cooldown after motion
IDLE_S = 30                         # cnc/state back to idle after a job
GRBL_PORT_S = 30                    # the Grbl port after the supervisor reports grblHAL running

XY_STEPS_PER_MM = 53.333            # boards/glowforge.h (x8 microstepping)
RETURN_MAX_MM = 100.0               # a displaced head is jogged back at most this far

# The XY microstep mode (the xy_microsteps setting: 8, 16 or 32; unset =
# 8). The GRBL controller reads it at its start and derives its scale,
# its machine tick and the kernel stop ramp from it (boards/glowforge.h):
# the fixed values of x/y_mode, step_freq and ramp_rate are the mode's.
XY_MODES = (8, 16, 32)
XY_MODE_DEFAULT = 8
XY_STEPS_PER_MM_OF = {8: 53.333, 16: 106.667, 32: 213.333}     # $100/$101 per mode
XY_TICK_X8_HZ = 28160                                            # the x8 machine tick
XY_RAMP_X8_HZ_PER_S = 125000                                     # the kernel stop ramp at it


def xy_mode_of(settings):
    """The mode a /settings body names (unset, unreadable, or not a mode
    reads as the default)."""
    try:
        mode = int((settings or {}).get("xy_microsteps") or XY_MODE_DEFAULT)
    except (TypeError, ValueError, AttributeError):
        return XY_MODE_DEFAULT
    return mode if mode in XY_MODES else XY_MODE_DEFAULT


def xy_steps_per_mm(mode):
    return XY_STEPS_PER_MM_OF[mode]


def fixed_sysfs(mode=XY_MODE_DEFAULT):
    """FIXED_SYSFS with the mode's own x/y_mode, step_freq and ramp_rate:
    the tick and the ramp scale with the mode (x16 doubles both, x32
    quadruples them)."""
    k = mode // XY_MODE_DEFAULT
    own = {"cnc/x_mode": str(mode), "cnc/y_mode": str(mode),
           "cnc/step_freq": str(XY_TICK_X8_HZ * k),
           "cnc/ramp_rate": str(XY_RAMP_X8_HZ_PER_S * k)}
    return [(attr, own.get(attr, val)) for attr, val in FIXED_SYSFS]


def leds_root():
    r = os.environ.get("GF_LEDS_ROOT") or "/sys/class/leds/"
    return r if r.endswith("/") else r + "/"


def read_led(name):
    try:
        with open(leds_root() + name + "/brightness") as f:
            return f.read().strip()
    except OSError:
        return None


def write_led(name, value):
    with open(leds_root() + name + "/target", "w") as f:
        f.write(str(value))


def read_position():
    """(x, y, z) step counters, or None when unreadable."""
    try:
        with open(hw.sysfs_root() + "cnc/position", "rb") as f:
            raw = f.read(32)
        return list(struct.unpack("<3i", raw[:12]))
    except (OSError, struct.error):
        return None


def read_ring_residue():
    """Unplayed bytes queued in the kernel pulse ring: total written minus
    processed (the cnc/position byte counters), or None when unreadable.
    Anything but 0 at idle is stale motion that the NEXT run would replay
    before its own bytes."""
    try:
        with open(hw.sysfs_root() + "cnc/position", "rb") as f:
            raw = f.read(32)
        processed, total = struct.unpack("<2I", raw[12:20])
        return int(total) - int(processed)
    except (OSError, struct.error):
        return None


def read_program_total():
    """Bytes of program the kernel has been given for this run, or None when
    unreadable. Under a live feed it grows while the job plays: that growth is
    what says the ring is being topped up rather than preloaded."""
    try:
        with open(hw.sysfs_root() + "cnc/position", "rb") as f:
            raw = f.read(32)
        _, total = struct.unpack("<2I", raw[12:20])
        return int(total)
    except (OSError, struct.error):
        return None


class Leftover:
    def __init__(self, item, found, expected, action):
        self.item = item
        self.found = found
        self.expected = expected
        self.action = action        # "restored" | "unrestorable" | "waited" | "failed: ..."

    def as_dict(self):
        return {"item": self.item, "found": self.found, "expected": self.expected,
                "action": self.action}

    def __str__(self):
        return "%s=%s (expected %s) -> %s" % (self.item, self.found, self.expected, self.action)


class Baseline:
    """One instance per run: capture() before, check() around, restore()
    after. `log` is a callable(str); every line is prefixed 'baseline:'."""

    _unreachable_until = 0.0        # class-wide: skip forgectrl for a while after a miss

    def __init__(self, log, abort=None):
        self._log = log
        self.abort = abort or (lambda: False)
        self.captured = None
        self.forgectrl = None
        self.mode = None            # the controller mode in force (per enforce)

    def log(self, msg):
        self._log("baseline: " + msg)

    # -- forgectrl access ------------------------------------------------
    def fc(self):
        if self.forgectrl is None:
            self.forgectrl = hw.Forgectrl()
        return self.forgectrl

    def fc_get(self, path):
        if time.time() < Baseline._unreachable_until:
            return None, None
        try:
            st, body = self.fc().get(path)
        except hw.HwError:
            Baseline._unreachable_until = time.time() + 30
            return None, None
        if st is None:
            Baseline._unreachable_until = time.time() + 30
        return st, body

    def fc_post(self, path, **kw):
        try:
            return self.fc().post(path, **kw)
        except hw.HwError as e:
            return None, str(e)

    def xy_mode(self):
        """The XY microstep mode the settings name now (the default when
        forgectrl does not answer)."""
        st, body = self.fc_get("/settings")
        return xy_mode_of(body if st == 200 and isinstance(body, dict) else None)

    def wait_settled(self, timeout=SETTLE_S, unreachable_s=10):
        """Block until forgectrl reports a settled supervisor: motion
        verified (the probe passed), motion-fault (the ladder exhausted),
        standby (the manual stop lever), or gated (the commissioning gate
        is closed: no controller spawns until it opens). Gives up after
        unreachable_s without an answer. Returns the last /mode body (None
        if unreachable)."""
        t0 = time.time()
        deadline = t0 + timeout
        last = seen = heard = None
        pending_since = None        # verified but not running: the spawn follows
        while time.time() < deadline and not self.abort():
            try:
                st, body = self.fc().get("/mode")
            except hw.HwError:
                st, body = None, None
            if st is None and heard is None and time.time() - t0 >= unreachable_s:
                self.log("forgectrl unreachable for %d s - not waiting for it" % unreachable_s)
                Baseline._unreachable_until = time.time() + 30
                return None
            if st == 200 and isinstance(body, dict):
                heard = time.time()
                last = body
                key = (body.get("controller"), body.get("motion"))
                if key != seen:
                    seen = key
                    self.log("/mode controller=%s motion=%s" % key)
                ctl = body.get("controller")
                if ctl in ("motion-fault", "standby", "gated") or (ctl == "running" and body.get("motion") == "verified"):
                    if ctl == "motion-fault":
                        self.log("WARNING - motion liveness ladder failed, controllers are "
                                 "down (motion-fault); retry via POST /mode")
                    return body
                if body.get("motion") == "verified":
                    # the probe passed; the spawn (or a respawn backoff of up to
                    # 30 s) is in flight - give it a bounded moment
                    pending_since = pending_since or time.time()
                    if time.time() - pending_since > 35:
                        return body
                else:
                    pending_since = None
            time.sleep(1.0)
        self.log("WARNING - forgectrl did not settle within %d s (last /mode: %s)" % (timeout, last))
        return last

    # -- a cloud client without the hunt ---------------------------------
    def nohunt_on(self, want):
        """The no-hunt marker before a start that brings the cloud client
        up (a switch to cloud, a controller start in cloud mode)."""
        if want != "cloud":
            return
        try:
            with open(NOHUNT_MARKER, "w") as f:
                f.write("forgetest baseline\n")
        except OSError as e:
            self.log("no-hunt marker not written (%s): the client will hunt" % e)

    def nohunt_off(self):
        """The marker the client never read: a start that was refused."""
        try:
            os.remove(NOHUNT_MARKER)
        except OSError:
            pass

    # -- the mode a test needs -------------------------------------------
    def switch_mode(self, want, timeout=SETTLE_S):
        """Put the machine in controller mode `want` through the supervisor
        and wait for it to settle there: the controller running, motion
        verified, and in GRBL mode the Grbl port answering. Returns (ok,
        detail). Used by the runner for a test that declares a mode, before
        the preserved state is captured - so the baseline keeps the mode
        the test asked for, not the one the run found."""
        st, mode = self.fc_get("/mode")
        if st != 200 or not isinstance(mode, dict):
            return False, "forgectrl not answering (/mode -> %s)" % st
        if mode.get("mode") == want and mode.get("controller") == "running":
            self.mode = want
            return True, "already in %s mode" % want
        self.log("switching to %s mode (found %s, controller %s)"
                 % (want, mode.get("mode"), mode.get("controller")))
        if want == "cloud":
            # Cloud mode exists only while cloud_enabled is 1. A test that
            # needs it gets it turned on here, with a line in the log; it
            # stays on afterward, as a cloud job the owner ran would leave it.
            st, settings = self.fc_get("/settings")
            if st == 200 and isinstance(settings, dict) and settings.get("cloud_enabled") != "1":
                # the typed phrase the cloud step asks for: the runner
                # gives it under the operator's rule for cloud tests
                st, body = self.fc_post("/settings", data={"cloud_enabled": "1",
                                                            "phrase": "I UNDERSTAND"})
                if st != 200:
                    return False, "cloud_enabled=1 for the test -> %s %s" % (st, body)
                self.log("cloud mode turned on for the test (cloud_enabled was %r)"
                         % (settings.get("cloud_enabled") or ""))
        self.nohunt_on(want)
        st, body = self.fc_post("/mode", data={"controller": want})
        if st != 200:
            self.nohunt_off()
            return False, "POST /mode controller=%s -> %s %s" % (want, st, body)
        mode = self.wait_settled(timeout=timeout) or {}
        self.mode = mode.get("mode")
        if mode.get("mode") != want or mode.get("controller") != "running":
            return False, "%s mode did not come up: %s" % (want, mode)
        if want == "grbl":
            w = self._wait("Grbl port", lambda: hw.grbl_port_open(2), GRBL_PORT_S)
            if w is None:
                return False, "grbl controller is running but the Grbl port never opened"
        return True, "%s mode up" % want

    # -- capture -------------------------------------------------------
    def capture(self):
        """Record the preserved state before a run."""
        cap = {"sysfs": {}, "position": read_position(), "settings": None, "mode": None}
        for attr in PRESERVED_SYSFS:
            cap["sysfs"][attr] = hw.sysfs_read(attr)
        st, body = self.fc_get("/settings")
        if st == 200 and isinstance(body, dict):
            cap["settings"] = {k: v for k, v in body.items()
                               if isinstance(v, str) and k not in UNPRESERVED_SETTINGS}
        st, body = self.fc_get("/mode")
        if st == 200 and isinstance(body, dict):
            cap["mode"] = body.get("mode")
        self.captured = cap
        return cap

    # -- check + restore -----------------------------------------------
    def enforce(self, phase, captured=None):
        """Bring the machine to the baseline; returns the list of leftovers.
        phase is 'pre' or 'post' (log wording only). captured is the
        preserved state to hand back (post) - None compares nothing."""
        left = []
        self.mode = None
        self._forgectrl_side(left, captured)
        self._kernel_side(left)
        self._lamp_side(left)
        self._preserved(left, captured)
        if left:
            self.log("%s: %d leftover(s): %s" % (phase, len(left), "; ".join(str(x) for x in left)))
        else:
            self.log("%s: clean" % phase)
        return left

    def _wait(self, what, pred, timeout):
        t0 = time.time()
        while time.time() - t0 < timeout and not self.abort():
            if pred():
                return time.time() - t0
            time.sleep(1.0)
        return None

    def cloud_mode(self):
        return self.mode == "cloud"

    def _forgectrl_side(self, left, captured=None):
        st, mode = self.fc_get("/mode")
        if st != 200 or not isinstance(mode, dict):
            self.log("forgectrl not answering - service-side checks skipped")
            return
        self.mode = mode.get("mode")
        # a diagnostic left running seizes the thermal hardware: abort it
        st, d = self.fc_get("/diag/status")
        if st == 200 and isinstance(d, dict) and d.get("running"):
            self.fc_post("/diag/abort")
            w = self._wait("diag idle", lambda: not (self.fc_get("/diag/status")[1] or {}).get("running"), 60)
            left.append(Leftover("diag", d.get("tool"), "not running",
                                 "aborted" if w is not None else "failed: still running"))
        # the camera engine stops itself 10 s after the last client
        st, c = self.fc_get("/cam/status")
        if st == 200 and isinstance(c, dict) and c.get("running"):
            w = self._wait("cam idle", lambda: not (self.fc_get("/cam/status")[1] or {}).get("running"),
                           CAM_IDLE_S)
            if w is None:
                left.append(Leftover("cam.running", True, False, "failed: still running"))
        # supervisor: the mode the run found (or declared), controller
        # running, motion verified. A mode the run changed without
        # declaring it is handed back through the switch.
        want = (captured or {}).get("mode") or mode.get("mode") or "grbl"
        if mode.get("mode") != want:
            self.nohunt_on(want)
            st, body = self.fc_post("/mode", data={"controller": want})
            if st != 200:
                self.nohunt_off()
            mode = self.wait_settled() or mode
            self.mode = mode.get("mode")
            left.append(Leftover("mode", mode.get("mode"), want,
                                 "restored" if mode.get("mode") == want else "failed: %s %s" % (st, body)))
        if mode.get("controller") == "motion-fault":
            self.nohunt_on(want)
            st, body = self.fc_post("/mode", data={"controller": want})
            if st != 200:
                self.nohunt_off()
            mode = self.wait_settled() or mode
            left.append(Leftover("controller", "motion-fault", "running",
                                 "restored" if mode.get("controller") == "running"
                                 else "failed: %s" % mode.get("controller")))
        elif mode.get("controller") == "standby":
            self.nohunt_on(mode.get("mode"))
            st, body = self.fc_post("/controller/start")
            if st != 200:
                self.nohunt_off()
            mode = self.wait_settled() or mode
            left.append(Leftover("controller", "standby", "running",
                                 "restored" if mode.get("controller") == "running"
                                 else "failed: %s" % mode.get("controller")))
        elif mode.get("controller") != "running" or mode.get("motion") != "verified":
            before = (mode.get("controller"), mode.get("motion"))
            mode = self.wait_settled() or mode
            if mode.get("controller") == "running" and mode.get("motion") == "verified":
                left.append(Leftover("controller", "%s/%s" % before, "running/verified", "waited"))
            else:
                left.append(Leftover("controller", "%s/%s" % before, "running/verified",
                                     "failed: %s/%s" % (mode.get("controller"), mode.get("motion"))))
        # machine state through /status
        st, s = self.fc_get("/status")
        if st == 200 and isinstance(s, dict):
            if s.get("state") != "idle":
                if s.get("state") == "underrun":
                    try:
                        hw.sysfs_write("cnc/stop", "1")     # ack
                    except OSError:
                        pass
                w = self._wait("idle", lambda: (self.fc_get("/status")[1] or {}).get("state") == "idle", IDLE_S)
                left.append(Leftover("state", s.get("state"), "idle",
                                     "waited" if w is not None else "failed: not idle"))
            if s.get("laser_locked") is False:
                try:
                    hw.sysfs_write("cnc/laser_latch", "1")
                    act = "restored"
                except OSError as e:
                    act = "failed: %s" % e
                left.append(Leftover("laser_locked", False, True, act))
        # cooling engine idle, unarmed
        st, c = self.fc_get("/cool/status")
        if st == 200 and isinstance(c, dict):
            if c.get("phase") != "idle" or c.get("armed") or c.get("hold"):
                found = "%s/armed=%s/hold=%s" % (c.get("phase"), c.get("armed"), c.get("hold"))
                w = self._wait("cool idle", lambda: (lambda x: x.get("phase") == "idle" and not x.get("armed")
                                                    and not x.get("hold"))(self.fc_get("/cool/status")[1] or {}),
                               COOL_IDLE_S)
                left.append(Leftover("cool", found, "idle/unarmed/no hold",
                                     "waited" if w is not None else "failed: still %s" % found))

    def _lamp_side(self, left):
        """The lid lamp at forgectrl's idle level (the lid_lamp_idle setting).
        In cloud mode the cloud client owns the lamp (its lid-image level)."""
        if self.cloud_mode():
            return
        st, body = self.fc_get("/settings")
        if st != 200 or not isinstance(body, dict):
            return
        want = (body.get("lid_lamp_idle") or "").strip() or LID_LAMP_DEFAULT
        got = hw.sysfs_read(LID_LAMP_ATTR)
        if got is None or got == want:
            return
        try:
            hw.sysfs_write(LID_LAMP_ATTR, want)
            back = hw.sysfs_read(LID_LAMP_ATTR)
            act = "restored" if back == want else "failed: reads %s" % back
        except OSError as e:
            act = "failed: %s" % e
        left.append(Leftover(LID_LAMP_ATTR, got, want, act))

    def _kernel_side(self, left):
        if hw.sysfs_read("cnc/state") is None:
            self.log("kernel sysfs not present - kernel-side checks skipped")
            return
        for attr, want in IDLE_READBACKS:
            got = hw.sysfs_read(attr)
            if got is not None and got != want:
                left.append(Leftover(attr, got, want, "unrestorable"))
        ilk = hw.sysfs_int("cnc/interlock_circuit")
        if ilk is not None and not (ilk & LATCH_BIT):
            try:
                hw.sysfs_write("cnc/laser_latch", "1")
                act = "restored"
            except OSError as e:
                act = "failed: %s" % e
            left.append(Leftover("laser_latch", "unlocked (interlock 0x%x)" % ilk, "locked", act))
        residue = read_ring_residue()
        if residue:
            left.append(Leftover("pulse ring", "%d unplayed bytes" % residue, "0 (nothing queued)",
                                 "unrestorable: the next run would replay them first"))
        for attr, want in fixed_sysfs(self.xy_mode()):
            if self.cloud_mode() and attr in GRBL_CONTROLLER_SYSFS:
                continue
            got = hw.sysfs_read(attr)
            if got is None or got == want:
                continue
            try:
                hw.sysfs_write(attr, want)
                back = hw.sysfs_read(attr)
                act = "restored" if back == want else "failed: reads %s" % back
            except OSError as e:
                act = "failed: %s" % e
            left.append(Leftover(attr, got, want, act))
        for name in BUTTON_LEDS:
            got = read_led(name)
            if got is not None and got != "0":
                try:
                    write_led(name, 0)
                    act = "restored"
                except OSError as e:
                    act = "failed: %s" % e
                left.append(Leftover("leds/" + name, got, "0", act))

    def _return_head(self, was, now):
        """Jog the head back along its own path by the kernel-measured X/Y
        delta (Z is never touched), through the GRBL controller. Bounded:
        beyond RETURN_MAX_MM per axis, or without a running GRBL
        controller, the counters are reported and left."""
        dx = (now[0] - was[0]) / XY_STEPS_PER_MM
        dy = (now[1] - was[1]) / XY_STEPS_PER_MM
        if abs(dx) < 0.02 and abs(dy) < 0.02:
            return "unrestorable (Z only)" if now[2] != was[2] else "restored"
        if abs(dx) > RETURN_MAX_MM or abs(dy) > RETURN_MAX_MM:
            return "unrestorable: %.1f/%.1f mm exceeds %.0f mm" % (dx, dy, RETURN_MAX_MM)
        # Never jog on top of stale bytes: a run started now would replay
        # whatever the ring still holds before the jog, in a direction and
        # for a distance nobody asked for. Report and leave the head.
        residue = read_ring_residue()
        if residue:
            return ("unrestorable: %d unplayed bytes queued in the kernel ring - a jog would "
                    "replay them; clear the ring (controller restart) before moving" % residue)
        # a controller may be inside a respawn backoff (seconds): wait for it
        mode = None
        deadline = time.time() + 30
        while time.time() < deadline:
            st, mode = self.fc_get("/mode")
            if (st == 200 and isinstance(mode, dict) and mode.get("mode") == "grbl"
                    and mode.get("controller") == "running"):
                break
            if st is None:
                break
            time.sleep(1.0)
        if not (isinstance(mode, dict) and mode.get("mode") == "grbl"
                and mode.get("controller") == "running"):
            return "unrestorable: no running GRBL controller"
        try:
            with hw.Grbl() as g:
                rep = g.status_report()
                if rep["state"].startswith(("Hold", "Door")):
                    # a job left held (a pause test that failed there) refuses
                    # a jog: the soft reset ends it where it stopped, position
                    # kept, and hands the controller back
                    held = rep["state"]
                    g.realtime(0x18)
                    deadline = time.time() + 5
                    while time.time() < deadline:
                        rep = g.status_report()
                        if not rep["state"].startswith(("Hold", "Door")):
                            break
                        time.sleep(0.2)
                    self.log("controller reset out of %s: now %s" % (held, rep["state"]))
                if rep["state"].startswith("Alarm"):
                    g.command("$X")
                g.command("$J=G91X%.3fY%.3fF1200" % (-dx, -dy))
                deadline = time.time() + 60
                while time.time() < deadline:
                    rep = g.status_report()
                    if rep["state"].startswith("Idle") and time.time() > deadline - 59.5:
                        break
                    time.sleep(0.2)
                g.command("G90")
        except (hw.HwError, OSError) as e:
            return "failed: %s" % e
        self.fc().wait_idle(15)
        back = read_position()
        if back is not None and abs(back[0] - was[0]) <= 2 and abs(back[1] - was[1]) <= 2:
            self.log("head jogged back %.3f/%.3f mm to its start" % (-dx, -dy))
            return "restored (jogged back %.1f/%.1f mm)" % (-dx, -dy)
        return "failed: counters read %s after the return jog" % back

    def _preserved(self, left, captured):
        if not captured:
            return
        for attr, was in (captured.get("sysfs") or {}).items():
            now = hw.sysfs_read(attr)
            if was is None or now is None or now == was:
                continue
            try:
                hw.sysfs_write(attr, was)
                back = hw.sysfs_read(attr)
                act = "restored" if back == was else "failed: reads %s" % back
            except OSError as e:
                act = "failed: %s" % e
            left.append(Leftover(attr, now, was, act))
        # In cloud mode the counters are the cloud client's: every service
        # action re-zeroes them at its start, so they preserve nothing.
        was = captured.get("position")
        now = read_position()
        if was is not None and now is not None and now != was and not self.cloud_mode():
            left.append(Leftover("position", now, was, self._return_head(was, now)))
        was = captured.get("settings")
        if was:
            st, body = self.fc_get("/settings")
            if st == 200 and isinstance(body, dict):
                for k, v in was.items():
                    if body.get(k) == v:
                        continue
                    st2, b2 = (self.fc_post("/settings", params={k: ""}) if v == ""
                               else self.fc_post("/settings", data={k: v}))
                    left.append(Leftover("settings." + k, body.get(k), v,
                                         "restored" if st2 == 200 else "failed: %s %s" % (st2, b2)))


# ------------------------------------------------------------ boot reference

def boot_id():
    v = os.environ.get("FORGETEST_BOOT_ID")
    if v:
        return v
    try:
        with open("/proc/sys/kernel/random/boot_id") as f:
            return f.read().strip()
    except OSError:
        return None


def uptime_s():
    try:
        with open("/proc/uptime") as f:
            return float(f.read().split()[0])
    except (OSError, ValueError, IndexError):
        return None


def dump_sysfs():
    """Every readable text attribute under the module's sysfs root, by
    'group/attr'; the binary position attribute decoded to (x, y, z)."""
    out = {}
    root = hw.sysfs_root()
    for group in ("cnc", "pic", "head", "thermal"):
        d = root + group
        try:
            names = sorted(os.listdir(d))
        except OSError:
            continue
        for n in names:
            path = "%s/%s" % (d, n)
            if os.path.isdir(path) or n in ("uevent",):
                continue
            key = "%s/%s" % (group, n)
            if key == "cnc/position":
                out[key] = read_position()
                continue
            try:
                with open(path, "rb") as f:
                    raw = f.read(256)
            except OSError:
                continue
            try:
                out[key] = raw.decode("ascii").strip()
            except UnicodeDecodeError:
                out[key] = "<binary %d bytes>" % len(raw)
    return out


def dump_all(bl):
    """The whole idle picture: sysfs, LEDs, forgectrl's status endpoints."""
    d = {"sysfs": dump_sysfs(), "leds": {}, "forgectrl": {}}
    for name in BUTTON_LEDS + ("lid_led",):
        d["leds"][name] = read_led(name)
    for path in ("/mode", "/status", "/cool/status", "/cam/status", "/diag/status", "/settings"):
        st, body = bl.fc_get(path)
        d["forgectrl"][path] = body if st == 200 else None
    return d


def check_fixed_against(ref, log):
    """Compare the fixed constants with a fresh-boot dump; log the diffs.
    With the dump taken after the controller applied its config (see
    wait_controller_configured) a differing value is a fact about this
    machine worth a look, not a leftover."""
    diffs = []
    sysfs = ref.get("sysfs") or {}
    for attr, want in fixed_sysfs(ref_xy_mode(ref)) + IDLE_READBACKS:
        got = sysfs.get(attr)
        if got is not None and got != want:
            diffs.append("%s: boot=%s constant=%s" % (attr, got, want))
    for d in diffs:
        log("baseline: NOTE fresh boot differs from the fixed value - " + d)
    return diffs


# The attributes the GRBL controller writes at its own start (its analog
# config + machine tick): once they read the fixed values the controller
# has configured the machine. Before that the kernel shows the supervisor's
# motion-probe leftovers (step_freq 10000, y_mode at the module default) -
# the state /mode already calls "running", because "running" is the spawn,
# not the config. motor_lock is no marker: the probe and the controller
# both leave it 0.
CONFIGURED_MARKER_ATTRS = ("cnc/step_freq", "cnc/y_mode")
CONFIGURED_MARKERS = [(a, dict(FIXED_SYSFS)[a]) for a in CONFIGURED_MARKER_ATTRS]
CONFIGURED_TIMEOUT_S = 20
CONFIGURED_SETTLE_S = 1.0


def configured_markers(mode=XY_MODE_DEFAULT):
    """The markers with the mode's own values (the tick and the mode both
    follow the setting)."""
    return [(a, dict(fixed_sysfs(mode))[a]) for a in CONFIGURED_MARKER_ATTRS]


def ref_xy_mode(ref):
    """The XY microstep mode a saved reference was taken under: its own
    /settings dump, else the default."""
    fc = (ref or {}).get("forgectrl") or {}
    return xy_mode_of(fc.get("/settings") if isinstance(fc, dict) else None)


def reference_preconfig(ref):
    """True when a saved reference shows the pre-controller state: every
    marker present differs from its fixed value (the probe's step_freq and
    the module's y_mode together), i.e. it was dumped before the
    controller's init writes landed."""
    sysfs = (ref or {}).get("sysfs") or {}
    markers = configured_markers(ref_xy_mode(ref))
    seen = [(sysfs.get(a), want) for a, want in markers if sysfs.get(a) is not None]
    return bool(seen) and all(got != want for got, want in seen)


def wait_controller_configured(log, mode_body, timeout=CONFIGURED_TIMEOUT_S, sleep=time.sleep,
                               xy_mode=XY_MODE_DEFAULT):
    """After the supervisor reports the controller running: block until the
    GRBL controller's init writes have landed (the configured markers read
    the fixed values of the XY microstep mode in force), then a short
    settle. Only the GRBL controller writes those; in any other mode
    return at once. Bounded: on timeout the caller proceeds and the
    reference will say so."""
    if not (isinstance(mode_body, dict) and mode_body.get("controller") == "running"
            and (mode_body.get("mode") or "grbl") == "grbl"):
        return True
    markers = configured_markers(xy_mode)
    t0 = time.time()
    while time.time() - t0 < timeout:
        got = [(a, hw.sysfs_read(a)) for a, _ in markers]
        if all(g == want for (a, g), (_, want) in zip(got, markers)):
            sleep(CONFIGURED_SETTLE_S)
            log("baseline: controller configured %.1f s after running" % (time.time() - t0))
            return True
        if any(g is None for _, g in got):
            return True                 # no kernel sysfs (host run): nothing to wait for
        sleep(0.25)
    log("baseline: WARNING - controller did not apply its config within %d s (%s); "
        "the reference may show the supervisor's probe values"
        % (timeout, ", ".join("%s=%s" % (a, hw.sysfs_read(a)) for a, _ in markers)))
    return False


def boot_reference(log, data_dir):
    """The fresh-boot idle state of this boot: loaded from
    <data_dir>/boot-<boot_id>.json when forgetest already took it, taken
    now (after the supervisor settles) when the boot is recent, None
    otherwise. Blocks for the settle - call from a background thread."""
    bid = boot_id()
    if not bid:
        return None
    path = os.path.join(data_dir, "boot-%s.json" % bid)
    up = uptime_s()
    try:
        with open(path) as f:
            ref = json.load(f)
        # A reference dumped before the controller had applied its config
        # (the supervisor's probe values still showing) is retaken while
        # the boot is fresh enough; otherwise it stands, marked.
        if reference_preconfig(ref):
            if up is not None and up <= BOOT_MAX_AGE_S:
                log("baseline: fresh-boot reference %s predates the controller's config; retaking" % path)
                ref = None
            else:
                log("baseline: NOTE fresh-boot reference %s predates the controller's config "
                    "(taken %s); the machine's probe values are in it - power-cycle to retake"
                    % (path, ref.get("ts")))
        if ref is not None:
            log("baseline: fresh-boot reference loaded (%s, taken %s)" % (path, ref.get("ts")))
            return ref
    except (OSError, ValueError):
        pass
    if up is None or up > BOOT_MAX_AGE_S:
        log("baseline: no fresh-boot reference for this boot (uptime %s s > %d s); "
            "power-cycle to take one" % (up, BOOT_MAX_AGE_S))
        return None
    bl = Baseline(log)
    mode = bl.wait_settled()
    wait_controller_configured(log, mode, xy_mode=bl.xy_mode())
    ref = dump_all(bl)
    ref.update({"ts": now_ts(), "boot_id": bid, "uptime_s": uptime_s()})
    try:
        os.makedirs(data_dir, exist_ok=True)
        with open(path, "w") as f:
            json.dump(ref, f, indent=1, sort_keys=True)
    except OSError as e:
        log("baseline: could not save the fresh-boot reference: %s" % e)
    log("baseline: fresh-boot reference taken at uptime %.0f s (%d sysfs attrs)"
        % (ref["uptime_s"], len(ref["sysfs"])))
    check_fixed_against(ref, log)
    return ref
