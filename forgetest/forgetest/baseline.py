# Copyright 2026 514 LLC d/b/a OpenGlow
# Written by Scott Wiederhold
# https://community.openglow.org
# SPDX-License-Identifier:    MIT

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
240): the daemon asserts it at start and at every controller spawn. A
fade the cloud client started can still be moving it, so a level written
a moment before the check is read again until it holds before it is
judged.

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

Emission first, then motion: a pass reads the laser's readbacks and
locks the latch before it waits for or judges anything (the runner lets
the bench actuator go only after that), then ends an armed window and a
job still moving before it waits on a diagnostic, the cameras or the
cooling engine. A read that decides whether a job is still there and goes
unanswered stands the machine down as if one were, and a window found
armed later in the pass is ended at once. The latch and the window are read
before everything the pass asks for that ends a controller's session (a
controller stop, mode change or start, a settings write-back that can
restart the controller, a connection to the Grbl port: forgectrl locks the
latch on every controller exit, erasing an unlock unseen), and the latch
again at the end of the pass, independent of forgectrl, and locked when
clear or unreadable. Every
leftover on the laser's safety state goes to the runner the moment it is
read (on_safety), which applies the safety rule (runner._safety_found).

A side that does not answer is never read as a clean one. forgectrl is
waited for once at the start of a pass (FC_WAIT_S); a request that then
goes unanswered is a leftover of its own, and so is a kernel whose state
cannot be read. Only a host run (FORGETEST_HOST_RUN=1, the unit tests,
which have neither) skips them, and says so.
"""
import json
import os
import re
import struct
import subprocess
import time

from . import hw
from .campaign import SAFETY_ITEMS
from .log import now_ts

# A host run: the unit tests run the runner and the hand-back with no
# forgectrl and no kernel module, by design, and set this to "1". The
# hand-back then skips a side that does not answer, with a line in the
# log. Nothing on a machine sets it, and the provenance check refuses a
# process that carries it (provenance.SUBSTITUTES), so a run recorded
# under it is never clean.
def host_run():
    return os.environ.get("FORGETEST_HOST_RUN") == "1"


# forgectrl is waited for once at the start of a pass: a takeover's end or
# a restart the run made can still be bringing it up. After a request goes
# unanswered, the rest of that pass asks no more for FC_SILENCE_S, and the
# miss is a leftover.
FC_WAIT_S = 60
FC_SILENCE_S = 30

# GRBL-mode resting values (kernel attribute -> value as read back), as a
# fresh boot of the dev image leaves them (2026-08-16 bench dump).
# motor_lock/x_mode/y_mode/x_decay/y_decay and the hold currents are the
# GRBL controller's init writes (glowforge_io.c): motor_lock 0, every axis
# in the pulse path (a job's Z moves the lens; the driver's Z soft limit
# guards it), step_freq its machine tick and ramp_rate the kernel stop
# ramp, both derived from the XY microstep mode (the values here are the
# x8 base; fixed_sysfs() gives the mode's); streaming is only ever 1
# inside a live job; the head white LED is a camera lamp, off at idle; the
# TEC is the diagnostics' tool, off at idle. The loop heater is not here: the
# coolant flow watch, when an owner turns it on, holds it at idle (see
# Baseline._heater_side).
HEATER_ATTR = "thermal/heater_pwm"
PUMP_ATTR = "thermal/water_pump_on"
# Two of the cooling engine's ticks and a margin: what a pump read off at
# idle is given to come back on (_pump_side).
PUMP_TICK_S = 2.5
# How far the heater may sit from the duty the coolant flow watch reports
# while it holds, in counts (2 % of 65535): the hold moves its duty a little
# every half second, and the heater and the watch are read apart.
FLOW_HEATER_TOL = 1311

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
    ("thermal/tec_on", "0"),
    # Purge air runs continuously, as on the factory machine: the engine
    # turns it on at start and only a listening (the quiet hold) switches
    # it off, which puts it back on release. A check that switches it off
    # to measure it and forgets leaves the machine one job away from an
    # airflow hold mid-cut, judged against the floor that same check just
    # wrote, and nothing notices until the daemon restarts: the engine's
    # idle phase never re-applies its own duties. The airflow check did
    # exactly that, and it reached an operator's first fire.
    ("head/purge_air", "1"),
    # The lens motor at rest: hold current, half step. The lens checks and
    # the sheet cards take it to the run current in full or half step and
    # must hand it back, or the motor sits hot and the next reference
    # starts from a state nobody chose.
    ("head/z_current", "1"),
    ("head/z_mode", "1"),
]

# The subset the GRBL controller writes at its start: checked and restored
# in GRBL mode only. The cloud client sets its own values for these from
# every pulse header (step_freq 10 kHz, the run currents), runs at the
# service's own x8 with the module's ramp whatever xy_microsteps says, and
# hands the hold currents back at idle; forcing the GRBL values under it
# would be the baseline configuring another controller's machine. The Z
# pair belongs with them: the cloud client takes the lens microstep mode
# from the pulse header it is playing (ZSmd, gfhardware z_axis
# set_mode_from_puls) and the current with it, so a job whose header asks
# for full step leaves z_mode 0 behind by doing what it was told.
GRBL_CONTROLLER_SYSFS = ("cnc/motor_lock", "cnc/x_mode", "cnc/y_mode", "cnc/x_decay", "cnc/y_decay",
                         "cnc/step_freq", "cnc/ramp_rate", "pic/x_step_current", "pic/y_step_current",
                         "head/z_current", "head/z_mode")

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

# The offline service: gfcloud's one-start offline marker and the socket
# the service listens on while it runs. A takeover that finds it running
# makes forgectrl's start at its end under the marker again (runner.Takeover).
OFFLINE_MARKER = os.environ.get("FORGETEST_OFFLINE_MARKER", "/run/gfcloud-offline")
OFFLINE_SOCKET = os.environ.get("FORGETEST_OFFLINE_SOCKET", "/run/gfcloud-offline.sock")

# Read-only readbacks with their idle values (no direct restore: the state
# comes right through forgectrl - see restore_forgectrl - or is fatal).
IDLE_READBACKS = [
    ("cnc/state", "idle"),
    ("cnc/laser_enable", "0"),
    ("cnc/laser_on", "0"),
    ("cnc/faults", "0"),
]

# The laser's readbacks: the FIRE line and LASER_ON. Read first, before
# either relock: a lock makes the FIRE pad an input, which then reads low.
LASER_READBACKS = ("cnc/laser_enable", "cnc/laser_on")

LATCH_BIT = 0x08                    # interlock_circuit bit 3: latch locked

# The leftovers on the laser's safety state (campaign.SAFETY_ITEMS: the
# latch, as the kernel and forgectrl read it, and LASER_READBACKS). Each one
# a pass judges goes to the runner the moment it is read (on_safety), which
# applies the safety rule; found before a run, it also stops the run: the run
# would be judged on a machine the pass has just relocked.
assert set(LASER_READBACKS) <= set(SAFETY_ITEMS)

# A side the pass could not read at all, and what a value it could not read
# is found as: neither is a state anything left (Leftover.judged).
BLIND_ITEMS = ("forgectrl", "kernel sysfs")
UNREADABLE = "unreadable"

# The attributes whose fixed value follows the XY microstep mode
# (fixed_sysfs): with the mode unread they are not judged.
XY_MODE_SYSFS = ("cnc/x_mode", "cnc/y_mode", "cnc/step_freq", "cnc/ramp_rate")

# The controller's $ settings (technical/forgefirm/grblhal-driver.md, "The
# XY scale" and "Laser control"). The derived ones the driver makes from
# forgectrl's settings, in RAM (the scale from xy_microsteps, $35 from the
# laser floor at every arm): the settings
# hand-back judges what they come from, and a job's arm moves $35, so they
# are logged, not judged. The pinned ones the driver re-asserts at every
# settings dispatch: one that differs is a leftover, not written. A laser
# setting that differs is a leftover, not written: the laser's settings
# are not the bench's to set. Every other one is written back through the
# Grbl port, and is a leftover too.
# $63, $341 and $673 are pinned: the driver re-asserts them at every
# settings dispatch. $22 is not: the driver pins its homing bits only, and
# the other bits are the sender's.
GRBL_DERIVED_SETTINGS = ("$35", "$100", "$101")
GRBL_PINNED_SETTINGS = ("$32", "$44", "$45", "$46", "$63", "$102", "$341", "$673")
GRBL_LASER_SETTINGS = ("$30", "$31", "$33", "$34", "$36")
GRBL_SETTING_LINE = re.compile(r"^\$(\d+)=(.*)$")


def parse_grbl_settings(text):
    """{"$N": value} from the controller's $$ view, or None."""
    if not isinstance(text, str):
        return None
    out = {}
    for line in text.splitlines():
        m = GRBL_SETTING_LINE.match(line.strip())
        if m:
            out["$" + m.group(1)] = m.group(2).strip()
    return out or None

BUTTON_LEDS = ("button_led_1", "button_led_2", "button_led_3")

PRESERVED_SYSFS = []                # sysfs attrs captured before, restored after

LID_LAMP_ATTR = "pic/lid_led"
LID_LAMP_DEFAULT = "240"            # forgectrl's lid_lamp_idle default
# The lid LED has two writers: forgectrl sets the register (the idle level,
# re-applied at every controller spawn), and the cloud client and its
# homing runner set the kernel's "smooth" trigger, whose fade takes a
# fraction of a second and writes the register as it goes. A read right
# after a spawn can find the lamp part-way through such a fade. The lamp is
# read again every LID_LAMP_POLL_S until it holds the level, for at most
# LID_LAMP_SETTLE_S, before it is called a leftover.
LID_LAMP_SETTLE_S = 3.0
LID_LAMP_POLL_S = 0.1

BOOT_MAX_AGE_S = 600                # a boot reference is taken only this soon after boot
SETTLE_S = 150                      # the supervisor's probe + rail-off ladder
CAM_IDLE_S = 20                     # camera engine idle stop is 10 s
COOL_IDLE_S = 120                   # cooldown after motion
COOL_PUBLISH_S = 2.5                # the engine publishes once a tick (1 Hz): two ticks and a margin
IDLE_S = 30                         # cnc/state back to idle after a job
GRBL_PORT_S = 30                    # the Grbl port after the supervisor reports grblHAL running

XY_STEPS_PER_MM = 53.333            # boards/glowforge.h (x8 microstepping)
RETURN_MAX_MM = 100.0               # a displaced head is jogged back at most this far
# The counters count steps; the controller's own returns land within a
# tenth of a millimeter of where they started, not on the step. A
# difference under this is that quantization, not a leftover: the head is
# still put back, so nothing accumulates over a campaign, but the test
# that made it does not fail for it. X and Y only - Z is the lens, which
# the return never moves, so any Z difference is still a leftover.
POSITION_DEADBAND_MM = 0.1

# The XY microstep mode (the xy_microsteps setting: 8, 16 or 32; unset =
# 32). The GRBL controller reads it at its start and derives its scale,
# its machine tick and the kernel stop ramp from it (boards/glowforge.h):
# the fixed values of x/y_mode, step_freq and ramp_rate are the mode's.
XY_MODES = (8, 16, 32)
XY_MODE_BASE = 8                                                 # the factory x8 reference the tick and ramp scale from
XY_MODE_DEFAULT = 32                                             # the mode an unset or invalid setting reads as
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
    k = mode // XY_MODE_BASE
    own = {"cnc/x_mode": str(mode), "cnc/y_mode": str(mode),
           "cnc/step_freq": str(XY_TICK_X8_HZ * k),
           "cnc/ramp_rate": str(XY_RAMP_X8_HZ_PER_S * k)}
    return [(attr, own.get(attr, val)) for attr, val in FIXED_SYSFS]


def leds_root():
    r = os.environ.get("GF_LEDS_ROOT") or "/sys/class/leds/"
    return r if r.endswith("/") else r + "/"


def read_led(name, attr="target"):
    """A button LED's commanded level. The trigger fades brightness toward
    target, so brightness is where the fade has reached and target is what
    the machine was told: a run that left the button lit left a target, and
    a run that ended a moment ago may still be fading from one. Falls back
    to brightness where no target exists."""
    for a in (attr, "brightness"):
        try:
            with open(leds_root() + name + "/" + a) as f:
                return f.read().strip()
        except OSError:
            continue
    return None


def read_led_attr(name, attr):
    """One attribute of a button LED, or None (no fallback)."""
    try:
        with open(leds_root() + name + "/" + attr) as f:
            return f.read().strip()
    except OSError:
        return None


def write_led(name, value):
    # The LED trigger keeps a pulse going under a new target, so it stops first.
    for attr in ("pulse_on", "pulse_off"):
        try:
            with open(leds_root() + name + "/" + attr, "w") as f:
                f.write("0")
        except OSError:
            pass
    with open(leds_root() + name + "/target", "w") as f:
        f.write(str(value))


# The GRBL controller zeroes the kernel's step counters at every start (the
# lens reference) and at every home, and rewrites its home anchor each time.
# The anchor's identity is therefore the counters' frame: if it changed
# during a run, the counters at the end are not in the frame of the counters
# at the start, and their difference is not a distance the head traveled.
ANCHOR_PATH = os.environ.get("FORGETEST_ANCHOR", "/run/grblhal.homed")


def counter_frame():
    """What identifies the frame the step counters are in, or None."""
    try:
        st = os.stat(ANCHOR_PATH)
        return [st.st_ino, st.st_mtime_ns]
    except OSError:
        return None


def read_position():
    """(x, y, z) step counters, or None when unreadable."""
    try:
        with open(hw.sysfs_root() + "cnc/position", "rb") as f:
            raw = f.read(32)
        return list(struct.unpack("<3i", raw[:12]))
    except (OSError, struct.error):
        return None


def counter_steps_per_mm():
    """The scale of the X/Y position counters: the kernel counts the
    microsteps of the mode it runs (cnc/x_mode), so x16 and x32 count two
    and four times the x8 steps for the same millimeter. The x8 scale when
    the attribute is unreadable."""
    return XY_STEPS_PER_MM_OF.get(hw.sysfs_int("cnc/x_mode"), XY_STEPS_PER_MM)


def position_quantized(was, now, spm=XY_STEPS_PER_MM):
    """True when two step-counter readings differ by no more than the
    dead band on X and Y and not at all on Z: the step quantization of a
    move that landed where it meant to, rather than a leftover. A cancel
    that returns the head to the job start lands within a few hundredths
    of a millimeter, which is a step or four, and whether that rounds to
    the same integer is chance. spm is the counters' scale (the mode's)."""
    if was is None or now is None or now[2] != was[2]:
        return False
    return (abs(now[0] - was[0]) / spm <= POSITION_DEADBAND_MM and
            abs(now[1] - was[1]) / spm <= POSITION_DEADBAND_MM)


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


# The extension host stops a removed package's service on its next
# one-second turn; its group is killed and waited for inside that turn.
EXT_STOP_S = 8


def notice_marks(notices):
    """{seq: (at, times)} for the cooling engine's notices: which fault
    each one is (its sequence number and the time it began; the numbers
    start over with the daemon) and how many times it has been counted."""
    return {n.get("seq"): (n.get("at"), n.get("times")) for n in notices or [] if isinstance(n, dict)}


class Leftover:
    def __init__(self, item, found, expected, action):
        self.item = item
        self.found = found
        self.expected = expected
        self.action = action        # "restored" | "unrestorable" | "waited" | "failed: ..."

    def judged(self):
        """Whether the pass judged a state at all: not a side it could not
        read (forgectrl silent, the kernel or a readback unreadable). What it
        could not judge stops a run but is charged to nobody (runner.py)."""
        return self.item not in BLIND_ITEMS and self.found != UNREADABLE

    def as_dict(self):
        return {"item": self.item, "found": self.found, "expected": self.expected,
                "action": self.action}

    def __str__(self):
        return "%s=%s (expected %s) -> %s" % (self.item, self.found, self.expected, self.action)


class Baseline:
    """One instance per run: capture() before, check() around, restore()
    after. `log` is a callable(str); every line is prefixed 'baseline:'."""

    def __init__(self, log, abort=None, on_safety=None):
        self._log = log
        self.abort = abort or (lambda: False)
        # on_safety(leftovers): the runner's safety rule, called the moment a
        # pass judges a leftover on the laser's safety state, before it waits
        # on anything after that read
        self.on_safety = on_safety
        self.captured = None
        self.forgectrl = None
        self.mode = None            # the controller mode in force (per enforce)
        self.run_notices = []       # the cooling notices the run made (per post pass)
        # This instance's silence after an unanswered request, and the paths
        # that went unanswered; both start over with every pass.
        self._unreachable_until = 0.0
        self.missed = []
        self._asked_misses = 0      # requests forgectrl was asked and did not answer
        self._left = []             # the pass's leftovers, for findings made inside a step
        self.switch_found = []      # what switch_mode's latch read found
        self._armed_named = False   # a window this pass already named cool.armed
        self._named_window = None   # (leftover, what): the window _latch_before named

    def log(self, msg):
        self._log("baseline: " + msg)

    # -- forgectrl access ------------------------------------------------
    def fc(self):
        if self.forgectrl is None:
            self.forgectrl = hw.Forgectrl()
        return self.forgectrl

    def _miss(self, path):
        if path not in self.missed:
            self.missed.append(path)

    def fc_get(self, path):
        """(status, body), or (None, None) when forgectrl did not answer. A
        miss is kept (self.missed) and silences this instance for
        FC_SILENCE_S, so one slow daemon costs one timeout, not one per
        request; every request the silence turns away is a miss too."""
        if time.time() < self._unreachable_until:
            self._miss(path)
            return None, None
        try:
            st, body = self.fc().get(path)
        except hw.HwError:
            st, body = None, None
        if st is None:
            self._unreachable_until = time.time() + FC_SILENCE_S
            self._miss(path)
            self._asked_misses += 1
        return st, body

    def fc_dict(self, path):
        """One of forgectrl's JSON reports, or None. An answer that is not a
        200 with a JSON object is a miss as well: the hand-back cannot judge
        what it cannot read."""
        st, body = self.fc_get(path)
        if st == 200 and isinstance(body, dict):
            return body
        if st is not None:
            self._miss("%s (%s)" % (path, st))
            self._asked_misses += 1
        return None

    def await_forgectrl(self, timeout=None):
        """Wait for forgectrl to answer /mode, once, at the start of a pass:
        up to FC_WAIT_S, or one try on a host run. Its /mode answer when it
        answers (the pass judges from that one, so no second read can miss);
        otherwise None, the miss is kept and the pass asks no more."""
        if timeout is None:
            timeout = 0 if host_run() else FC_WAIT_S
        t0 = time.time()
        while True:
            try:
                st, body = self.fc().get("/mode")
            except hw.HwError:
                st, body = None, None
            if st == 200 and isinstance(body, dict):
                return body
            if time.time() - t0 >= timeout or self.abort():
                break
            time.sleep(1.0)
        self._miss("/mode")
        self._unreachable_until = time.time() + FC_SILENCE_S
        self.log("forgectrl did not answer within %d s%s" % (
            timeout, " (a host run: the service side is skipped)" if host_run() else ""))
        return None

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

    def xy_mode_read(self):
        """As xy_mode, or None when the settings cannot be read on a machine
        (a host run, with no forgectrl at all, reads the default)."""
        body = self.fc_dict("/settings")
        if body is None and not host_run():
            return None
        return xy_mode_of(body)

    def wait_settled(self, timeout=SETTLE_S, unreachable_s=10):
        """Block until forgectrl reports a settled supervisor: motion
        verified (the probe passed), motion-fault (the ladder exhausted),
        standby (the manual stop lever), or gated (the setup gate
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
                self._unreachable_until = time.time() + FC_SILENCE_S
                self._miss("/mode")
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
        # The switch stops the controller in force, and forgectrl locks the
        # latch as it does: what the latch reads now is read first (the
        # runner stops the run on a finding here, switch_found).
        self.switch_found = []
        self._latch_before("a mode switch", self.switch_found)
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
            # the probe connects to the Grbl port, which ends another
            # sender's window (the controller relocks): read first
            self._latch_before("a Grbl port connection", self.switch_found)
            w = self._wait("Grbl port", lambda: hw.grbl_port_open(2), GRBL_PORT_S)
            if w is None:
                return False, "grbl controller is running but the Grbl port never opened"
        return True, "%s mode up" % want

    # -- capture -------------------------------------------------------
    def capture(self):
        """Record the preserved state before a run. What forgectrl did not
        answer is in self.missed: the runner does not start a run whose
        state could not be captured, since the post pass would then have
        nothing to compare."""
        cap = {"sysfs": {}, "position": read_position(), "frame": counter_frame(),
               "settings": None, "mode": None, "notices": None, "grbl": None}
        for attr in PRESERVED_SYSFS:
            cap["sysfs"][attr] = hw.sysfs_read(attr)
        # The cooling notices that stand before the run: the post pass tells
        # the run's own from them (None: not read, so none can be told).
        body = self.fc_dict("/cool/status")
        if body is not None:
            cap["notices"] = notice_marks(body.get("notices"))
        body = self.fc_dict("/settings")
        if body is not None:
            cap["settings"] = {k: v for k, v in body.items()
                               if isinstance(v, str) and k not in UNPRESERVED_SETTINGS}
        body = self.fc_dict("/mode")
        if body is not None:
            cap["mode"] = body.get("mode")
            # The GRBL controller's $ settings, while one runs (forgectrl's
            # copy of its $$ view; a 404 is no controller).
            if body.get("mode") == "grbl" and body.get("controller") == "running":
                cap["grbl"] = self.grbl_settings()
        self.captured = cap
        return cap

    def grbl_settings(self):
        """The running GRBL controller's $ settings, or None."""
        st, text = self.fc_get("/grbl/settings")
        return parse_grbl_settings(text) if st == 200 else None

    # -- check + restore -----------------------------------------------
    def enforce(self, phase, captured=None, after_laser=None):
        """Bring the machine to the baseline; returns the list of leftovers.
        phase is 'pre' or 'post' (log wording only). captured is the
        preserved state to hand back (post) - None compares nothing.
        after_laser(left), when given, runs right after the latch is locked
        and before anything is waited for (the runner's release of the
        bench actuator); what it appends to left is a leftover too. The
        latch is read and locked at the start and again at the end; every
        safety finding goes to on_safety as it is read."""
        left = []
        self._left = left
        self.mode = None
        self._phase = phase
        self._unreachable_until = 0.0
        self.missed = []
        self._stood_down = False
        self._settled_on = None
        self._asked_misses = 0
        self._misses_at_stand_down = 0
        self._armed_named = False
        self._named_window = None
        # the counters' scale is the mode the run counted under, read
        # before the kernel side puts the settings' mode back
        self._counter_spm = counter_steps_per_mm()
        # Emission first: nothing is waited for before the laser is judged
        # and the latch is locked.
        self._laser_side(left)
        if after_laser is not None:
            after_laser(left)
        mode = self.await_forgectrl()
        if mode is not None:
            self._forgectrl_side(left, captured, mode)
        self._kernel_side(left)
        self._lamp_side(left)
        self._ext_side(left)
        self._preserved(left, captured)
        # a pass forgectrl did not answer at its start can still name a
        # window later, once the silence has run out (_latch_before)
        if mode is not None or self._named_window is not None:
            self._last_window(left)
        self._final_latch(left)
        if self.missed and not host_run():
            left.append(Leftover("forgectrl", "not answering (%s)" % ", ".join(self.missed), "answering",
                                 "failed: what forgectrl owns was not judged"))
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

    def stand_down(self, why):
        """End whatever the machine is still doing, and prove it ended.

        A run that dies mid-job leaves the job alive behind it: the
        cooling engine armed and holding for a job nothing will finish,
        the fans at run duty, and the rest of the program still queued in
        the pulse ring. Waiting does not end any of that - the engine is
        holding correctly, for a job that is never coming back.

        Stopping the controller ends it. The supervisor brings the
        controller back, the arm and the hold go with the job, and the
        ring is empty on the way in. This is what the ring-residue
        message has always told an operator to do; the hand-back does it
        instead of saying it.

        Returns the /mode body the machine settled on, or None."""
        self._stood_down = True
        # forgectrl locks the latch on every controller exit: what it reads
        # now is read first, or the stop would erase an unlock unseen
        self._latch_before("a stand-down")
        self.log("baseline: standing the machine down (%s): stopping the controller" % why)
        st, _b = self.fc_post("/controller/stop")
        if st != 200:
            self.log("baseline: stand-down: /controller/stop -> %s" % st)
        deadline = time.time() + 30
        while time.time() < deadline and not self.abort():
            body = self.fc_dict("/mode") or {}
            if body.get("controller") != "running":
                break
            time.sleep(1.0)
        # a cloud client comes back without the service's hunt, as every
        # client the baseline starts does
        self.nohunt_on(self.mode)
        st, _b = self.fc_post("/controller/start")
        if st != 200:
            self.nohunt_off()
            self.log("baseline: stand-down: /controller/start -> %s" % st)
        # A settle cut short (an Abort, a silent spell) says nothing of where
        # the supervisor is: /mode is read once more, past any silence, and
        # with no answer the supervisor is taken as unknown, so no branch
        # after this starts a controller again on the body from before the
        # stand-down.
        self._settled_on = (self.wait_settled() or self._mode_now()
                            or {"mode": self.mode, "controller": "unknown"})
        self._misses_at_stand_down = self._asked_misses
        return self._settled_on

    def _mode_now(self):
        """One /mode read that no silence turns away, or None."""
        try:
            st, body = self.fc().get("/mode")
        except hw.HwError:
            return None
        return body if st == 200 and isinstance(body, dict) else None

    def _latch_before(self, what, left=None):
        """The reads before anything the pass or the runner asks for that ends
        a controller's session: a controller stop, mode change or start, a
        settings write-back that can restart the controller, a connection to
        the Grbl port. forgectrl's supervisor locks the latch on every
        controller exit (and a Grbl connection ends another sender's window
        and relocks), so an unlock found only after one would be erased
        unseen. A clear bit 3 is locked and goes through the safety rule
        ("unlocked before <what>"); so does a window still armed, once a
        pass (cool.armed), since what follows can end it. What became of
        that window is said at the pass's last read of it (_last_window).
        Returns what it found."""
        if host_run() and hw.sysfs_read("cnc/state") is None:
            return []
        found = []
        ilk = hw.sysfs_int("cnc/interlock_circuit")
        if ilk is not None and not (ilk & LATCH_BIT):
            try:
                hw.sysfs_write("cnc/laser_latch", "1")
                act = "restored"
            except OSError as e:
                act = "failed: %s" % e
            found.append(Leftover("laser_latch", "unlocked before %s (interlock 0x%x)" % (what, ilk), "locked", act))
        if not self._armed_named:
            c = self.fc_dict("/cool/status")
            if c is not None and c.get("armed"):
                self._armed_named = True
                x = Leftover("cool.armed", "%s/armed=True/hold=%s before %s" % (c.get("phase"), c.get("hold"), what),
                             "unarmed", "read again after %s" % what)
                self._named_window = (x, what)
                found.append(x)
        if found:
            self._safety(self._left if left is None else left, found)
        return found

    def _settled_mode(self, mode):
        """The /mode body to judge the supervisor by: the one a stand-down
        since the last call settled on, else `mode`. A branch acting on the
        body from before a stand-down would start a controller again."""
        body, self._settled_on = self._settled_on, None
        if isinstance(body, dict):
            self.mode = body.get("mode") or self.mode
            return body
        return mode

    def _safety(self, left, found):
        """Leftovers on the laser's safety state: kept, and the judged ones
        handed to the runner at once (on_safety), which never breaks the
        pass."""
        left.extend(found)
        judged = [x for x in found if x.judged()]
        if judged and self.on_safety is not None:
            try:
                self.on_safety(judged)
            except Exception as e:  # noqa: BLE001 - the pass goes on whatever the record did
                self.log("the safety finding could not be recorded at once: %s: %s" % (type(e).__name__, e))

    def _fail_closed(self, what):
        """A read that decides whether a job is still there went unanswered:
        the machine is stood down as if it were (the stop goes through
        fc_post, which no silence turns away). Once for a spell of silence:
        a read the silence turns away after a stand-down stands nothing down
        again, but a request forgectrl was asked after the stand-down
        settled and did not answer does."""
        if not self._stood_down or self._asked_misses > self._misses_at_stand_down:
            self.stand_down("%s went unanswered: a job may still be there" % what)

    def cloud_mode(self):
        return self.mode == "cloud"

    def _laser_side(self, left):
        """Emission first. The laser's readbacks are read before anything is
        waited for and before the lock: a lock makes the FIRE pad an input,
        which then reads low, so a FIRE line driven at idle shows only in a
        read taken before it. Then the latch is locked, whatever it read: a
        lock is never refused and never fires anything (it sets the
        controller's holder bit and parks FIRE in high impedance), so it is
        written before forgectrl is asked anything. A latch found unlocked,
        or a readback that cannot be read, is a leftover; only a host run
        with no kernel module at all skips them."""
        reads = [(attr, hw.sysfs_read(attr)) for attr in LASER_READBACKS]
        ilk = hw.sysfs_int("cnc/interlock_circuit")
        try:
            hw.sysfs_write("cnc/laser_latch", "1")
            act = "restored"
        except OSError as e:
            act = "failed: %s" % e
        if host_run() and hw.sysfs_read("cnc/state") is None:
            return                  # no kernel module here: the kernel side says so
        found = [Leftover(attr, UNREADABLE if got is None else got, "0", "unrestorable")
                 for attr, got in reads if got != "0"]
        if ilk is None or not (ilk & LATCH_BIT):
            found.append(Leftover("laser_latch", UNREADABLE if ilk is None else "unlocked (interlock 0x%x)" % ilk,
                                  "locked", act))
        self._safety(left, found)

    def _final_latch(self, left):
        """The latch read again at the end of the pass, independent of
        forgectrl, and locked when clear or unreadable: a controller that
        unlocked after the lock at the start (a client arming late) is found
        here, whatever forgectrl answered in between."""
        if host_run() and hw.sysfs_read("cnc/state") is None:
            return
        ilk = hw.sysfs_int("cnc/interlock_circuit")
        if ilk is not None and ilk & LATCH_BIT:
            return
        try:
            hw.sysfs_write("cnc/laser_latch", "1")
            act = "restored"
        except OSError as e:
            act = "failed: %s" % e
        if ilk is not None:
            self._safety(left, [Leftover("laser_latch", "unlocked at the end of the pass (interlock 0x%x)" % ilk,
                                         "locked", act)])
        elif not any(x.item == "laser_latch" for x in left):
            # readable at the start, unreadable now: named, judged nowhere
            self._safety(left, [Leftover("laser_latch", UNREADABLE, "locked", act)])

    def _end_armed(self, left):
        """An armed window still open is a job still there, and it is ended
        at once, before the waits on a diagnostic, the cameras and the
        cooling engine. The engine publishes once a tick, and a read inside
        the tick after a job ended can still show its window, so an arm is
        read again past that tick first. A first read that goes unanswered
        (or that a /status miss just before it silenced) says nothing of
        the window, and the machine is stood down as if it were open."""
        c = self.fc_dict("/cool/status")
        if c is None:
            self._fail_closed("/cool/status")
            return
        if c.get("armed"):
            self._window(left, c)

    def _window(self, left, c, named=None):
        """A read that showed the window armed, wherever the pass took it
        (_end_armed, _pump_side, the kernel side's read, _last_window): read
        again past the engine's tick, and a window still armed is named
        (cool.armed) and ended by a stand-down. A re-read that goes
        unanswered says nothing of the window: it stands until a read says it
        closed. A window _latch_before named already (named) is not named
        again: a stand-down here sets its action."""
        if self._wait("cool publish",
                      lambda: (self.fc_dict("/cool/status") or {"armed": True}).get("armed") is False,
                      COOL_PUBLISH_S) is not None:
            return
        found = named[0].found if named is not None else "%s/armed=True/hold=%s" % (c.get("phase"), c.get("hold"))
        x = named[0] if named is not None else self._armed(left, found)
        self.stand_down("an armed window is still open (%s)" % found)
        ended = self._wait("disarmed",
                           lambda: (self.fc_dict("/cool/status") or {"armed": True}).get("armed") is False,
                           COOL_PUBLISH_S)
        x.action = "restored (stood the machine down)" if ended is not None else "failed: still armed"

    def _last_window(self, left):
        """The window read once more, before the pass's last latch read: a
        window _latch_before named before something that did not end it (a
        settings write-back that restarts nothing), or one that opened after
        the kernel side's read, is still armed here, and goes through
        _window. A read that goes unanswered says nothing of a window named
        before it, which goes through _window as well."""
        named, self._named_window = self._named_window, None
        if named is not None:
            named[0].action = "ended after %s" % named[1]
        c = self.fc_dict("/cool/status")
        if (c or {}).get("armed") or (c is None and named is not None):
            self._window(left, c or {}, named)

    def _armed(self, left, found):
        """An armed window confirmed past the engine's tick is a session
        waiting for its press, which the client unlocks the latch for: the
        laser's safety state is wrong whether or not the latch read unlocked
        at the moment the pass looked, so it goes through the safety rule
        (cool.armed), before the stand-down that ends it."""
        self._armed_named = True
        x = Leftover("cool.armed", found, "unarmed", "standing the machine down")
        self._safety(left, [x])
        return x

    def _forgectrl_side(self, left, captured, mode):
        """forgectrl's side, from the /mode answer the pass waited for."""
        self.mode = mode.get("mode")
        # Emission first: forgectrl's reading of the latch (the only one when
        # the kernel's cannot be read), then a window still armed. The lock is
        # written again on a latch forgectrl reads unlocked: the pass locked
        # it at its start, and a controller can unlock it after that (the
        # cloud client's arm), which this read is the first to see.
        s = self.fc_dict("/status")
        if s is not None and s.get("laser_locked") is False:
            try:
                hw.sysfs_write("cnc/laser_latch", "1")
                act = "restored"
            except OSError as e:
                act = "failed: %s" % e
            self._safety(left, [Leftover("laser_locked", False, True, act)])
        self._end_armed(left)
        mode = self._settled_mode(mode)
        # Then motion. With a controller running (motion verified, or the
        # probe skipped), the supervisor's own start is over and motion is a
        # job's: it is ended before the waits below. A supervisor still
        # starting moves the head itself (the liveness probe, the lens
        # sweep, with no controller running yet): that motion is waited out
        # with the settle below, and judged after it.
        settled = mode.get("controller") == "running"
        if settled:
            self._state_side(left)
            mode = self._settled_mode(mode)
        # a diagnostic left running seizes the thermal hardware: abort it
        d = self.fc_dict("/diag/status")
        if d is not None and d.get("running"):
            self.fc_post("/diag/abort")
            w = self._wait("diag idle", lambda: (self.fc_dict("/diag/status") or {"running": True})
                           .get("running") is False, 60)
            left.append(Leftover("diag", d.get("tool"), "not running",
                                 "aborted" if w is not None else "failed: still running"))
        # the camera engine stops itself 10 s after the last client
        c = self.fc_dict("/cam/status")
        if c is not None and c.get("running"):
            w = self._wait("cam idle", lambda: (self.fc_dict("/cam/status") or {"running": True})
                           .get("running") is False, CAM_IDLE_S)
            if w is None:
                left.append(Leftover("cam.running", True, False, "failed: still running"))
        # A stand-down whose settle forgectrl did not answer left the
        # supervisor unknown: it is read once more, and an answer is judged
        # as any other (a standby read now is no body from before the
        # stand-down, and its start is safe).
        if mode.get("controller") == "unknown":
            mode = self.wait_settled() or mode
        # supervisor: the mode the run found (or declared), controller
        # running, motion verified. A mode the run changed without
        # declaring it is handed back through the switch.
        want = (captured or {}).get("mode") or mode.get("mode") or "grbl"
        if mode.get("mode") != want:
            self._latch_before("a mode change")
            self.nohunt_on(want)
            st, body = self.fc_post("/mode", data={"controller": want})
            if st != 200:
                self.nohunt_off()
            mode = self.wait_settled() or mode
            self.mode = mode.get("mode")
            left.append(Leftover("mode", mode.get("mode"), want,
                                 "restored" if mode.get("mode") == want else "failed: %s %s" % (st, body)))
        if mode.get("controller") == "motion-fault":
            self._latch_before("a controller start")
            self.nohunt_on(want)
            st, body = self.fc_post("/mode", data={"controller": want})
            if st != 200:
                self.nohunt_off()
            mode = self.wait_settled() or mode
            left.append(Leftover("controller", "motion-fault", "running",
                                 "restored" if mode.get("controller") == "running"
                                 else "failed: %s" % mode.get("controller")))
        elif mode.get("controller") == "standby":
            self._latch_before("a controller start")
            self.nohunt_on(mode.get("mode"))
            st, body = self.fc_post("/controller/start")
            if st != 200:
                self.nohunt_off()
            mode = self.wait_settled() or mode
            left.append(Leftover("controller", "standby", "running",
                                 "restored" if mode.get("controller") == "running"
                                 else "failed: %s" % mode.get("controller")))
        elif mode.get("controller") == "unknown":
            # unanswered past the stand-down and again here: nothing is known
            # of the supervisor (named, judged nowhere)
            left.append(Leftover("controller", UNREADABLE, "running/verified", "failed: not judged"))
        elif mode.get("controller") != "running" or mode.get("motion") != "verified":
            before = (mode.get("controller"), mode.get("motion"))
            mode = self.wait_settled() or mode
            if mode.get("controller") == "running" and mode.get("motion") == "verified":
                # The supervisor's own work: a takeover ends by starting
                # forgectrl again, and the respawn runs the liveness probe
                # and the lens reference before the controller is up and
                # motion is verified. Waiting for that is right; calling
                # it a leftover is not, because the run did put it back -
                # the machine was still doing what it was asked.
                self.log("controller: %s/%s settled to running/verified on its own; "
                         "the supervisor's own start, not a leftover" % before)
            else:
                left.append(Leftover("controller", "%s/%s" % before, "running/verified",
                                     "failed: %s/%s" % (mode.get("controller"), mode.get("motion"))))
        if not settled:
            self._state_side(left)
        self._cool_side(left, captured)
        self._pump_side(left)

    def _state_side(self, left):
        """The machine at rest, through /status. A read that goes unanswered
        says nothing of a job still moving, and the machine is stood down as
        if one were."""
        s = self.fc_dict("/status")
        if s is None:
            self._fail_closed("/status")
            return
        if s.get("state") != "idle":
            # An underrun is the run's: the engine ran out of bytes and
            # the fault is acknowledged here. Any other state is the
            # machine finishing what it was given - the ring draining
            # to the end of a job - and it reaches idle on its own.
            underrun = s.get("state") == "underrun"
            if underrun:
                try:
                    hw.sysfs_write("cnc/stop", "1")     # ack
                except OSError:
                    pass
            idle = lambda: (self.fc_dict("/status") or {}).get("state") == "idle"     # noqa: E731
            w = self._wait("idle", idle, IDLE_S)
            if w is not None and not underrun:
                self.log("state: %s reached idle on its own after %.0f s; the machine "
                         "finishing, not a leftover" % (s.get("state"), w))
            else:
                act = "waited"
                if w is None:
                    # A machine that does not reach idle is still doing
                    # what a run left it: the controller's stop ends it.
                    self.stand_down("the machine is still %s" % s.get("state"))
                    act = ("restored (stood the machine down)" if self._wait("idle", idle, IDLE_S) is not None
                           else "failed: not idle")
                left.append(Leftover("state", s.get("state"), "idle", act))

    def _run_notices(self, notices, before):
        """The notices of faults that cleared on their own, after a run.
        Those that appeared during it, or were counted another time, are the
        run's: each is named in the log and kept in run_notices, with
        times_before when it stood before the run. `before` is the capture's
        marks (notice_marks). The engine dismisses by sequence number, every
        notice up to one, so the run's go only when none from before the run
        stands; the earlier ones (the owner's own use among them) are never
        the run's to clear. With no capture nothing can be told apart, and
        every notice stands."""
        self.run_notices = []
        if not notices:
            return
        if before is None:
            self.log("cool: %d notice(s) stand and none was read before the run; left standing: %s"
                     % (len(notices), [(n.get("seq"), n.get("verdict")) for n in notices]))
            return
        earlier = 0
        for n in notices:
            mark = before.get(n.get("seq"))
            if mark is not None and mark[0] == n.get("at"):
                earlier += 1
                if mark[1] == n.get("times"):
                    continue
                self.run_notices.append(dict(n, times_before=mark[1]))
            else:
                self.run_notices.append(dict(n))
        if not self.run_notices:
            return
        dismissed = False
        if earlier:
            fate = "left standing with the %d from before the run" % earlier
        else:
            st, _ = self.fc_post("/cool/notices/dismiss",
                                 params={"upto": str(max(n.get("seq") or 0 for n in self.run_notices))})
            dismissed = st == 200
            fate = "dismissed" if dismissed else "not dismissed (%s)" % st
        for n in self.run_notices:
            n["dismissed"] = dismissed
            again = " (%s before the run)" % n["times_before"] if "times_before" in n else ""
            self.log("cool: a notice of the run: seq %s %s (%s), at %s, cleared at %s, %s time(s)%s; %s"
                     % (n.get("seq"), n.get("verdict"), n.get("reason"), n.get("at"), n.get("cleared_at"),
                        n.get("times"), again, fate))

    def _cool_side(self, left, captured=None):
        """The cooling engine idle, unarmed, holding nothing. A read that goes
        unanswered says nothing of a window armed since the first look, and
        the machine is stood down as if one were."""
        c = self.fc_dict("/cool/status")
        if c is None:
            self._fail_closed("/cool/status")
            return
        # A failure a run left unacknowledged blocks the next job.
        a = c.get("attention") or {}
        if a.get("standing") and not a.get("live"):
            st, _ = self.fc_post("/cool/attention/ack")
            left.append(Leftover("attention", a.get("verdict"), "nothing to acknowledge",
                                 "restored (acknowledged)" if st == 200 else "failed: %s" % st))
            if st == 200:
                time.sleep(1.5)        # the engine releases its red flash at its next tick
        # A fault that cleared on its own blocks nothing, and its notice is no
        # leftover. After a run the run's own notices are recorded; before a
        # test the panel's notices (the owner's own use among them) stand.
        if getattr(self, "_phase", "post") == "post":
            self._run_notices(c.get("notices") or [], (captured or {}).get("notices"))
        if c.get("phase") == "idle" and not c.get("armed") and not c.get("hold"):
            return
        found = "%s/armed=%s/hold=%s" % (c.get("phase"), c.get("armed"), c.get("hold"))
        idle = lambda: (lambda x: x.get("phase") == "idle" and not x.get("armed")     # noqa: E731
                        and not x.get("hold"))(self.fc_dict("/cool/status") or {})
        # The engine publishes once a tick (1 Hz). A read inside the tick
        # after a run ended still shows the run: a diagnostic's hold until
        # the engine has the hardware back, a fail tier's hold until the
        # session ends with the controller. What the engine's next tick
        # clears was never left behind, so an arm or a hold is read again
        # past that tick before it is called the run's doing. One that a
        # run did leave stands for a job that is never coming back, and it
        # is still there.
        if c.get("armed") or c.get("hold"):
            free = lambda: (lambda x: bool(x) and not x.get("armed")                  # noqa: E731
                            and not x.get("hold"))(self.fc_dict("/cool/status") or {})
            if self._wait("cool publish", free, COOL_PUBLISH_S) is not None:
                c = self.fc_dict("/cool/status") or {}
                self.log("cool: %s was the engine's last word on the run; its next tick reads "
                         "%s/armed=%s/hold=%s" % (found, c.get("phase"), c.get("armed"), c.get("hold")))
                if c.get("phase") == "idle":
                    return
                found = "%s/armed=%s/hold=%s" % (c.get("phase"), c.get("armed"), c.get("hold"))
        # Armed, or holding, is the run's doing. A phase alone is
        # not: the engine clears smoke at run duty after an armed
        # session and cools down after a hot one, both timed and
        # both ending on their own. Waiting for that is right;
        # calling it a leftover is not, because nothing was left -
        # the machine was still finishing.
        dirt = bool(c.get("armed") or c.get("hold"))
        # A window armed past the engine's tick is a job (a client that armed
        # late, a print still loading when the run ended): waiting does not
        # end it, and the machine is stood down at once, here or the moment
        # a read during the wait below shows one, or goes unanswered (which
        # says nothing of a window opening behind it).
        armed = {"found": found} if c.get("armed") else {}

        def idle_or_armed():
            x = self.fc_dict("/cool/status")
            if x is None:
                armed.update(found="%s, then unanswered during the wait" % found, unanswered=True)
                return True
            if x.get("armed"):
                armed["found"] = "%s/armed=True/hold=%s" % (x.get("phase"), x.get("hold"))
                return True
            return x.get("phase") == "idle" and not x.get("hold")
        w = None if armed else self._wait("cool idle", idle_or_armed, COOL_IDLE_S)
        if armed:
            if armed.get("unanswered"):
                # a hold left behind is the run's whatever the read; a phase
                # alone was nothing left, and the silence after it is no state
                # anything left (judged nowhere, charged to nobody)
                x = Leftover("cool", armed["found"] if dirt else UNREADABLE, "idle/unarmed/no hold",
                             "standing the machine down")
                left.append(x)
            else:
                x = self._armed(left, armed["found"])
            self.stand_down("the cooling engine is %s" % armed["found"])
            w = self._wait("cool idle", idle, COOL_IDLE_S)
            x.action = ("restored (stood the machine down)" if w is not None
                        else "failed: still %s" % armed["found"])
            return
        if w is not None and not dirt:
            self.log("cool: %s ended on its own after %.0f s; the engine's own post-job "
                     "work, not a leftover" % (found, w))
            return
        # The engine holds for a job. A run that ended without
        # ending its job leaves one alive, and no amount of
        # waiting ends it: stand the machine down and let the
        # supervisor bring the controller back clean.
        act = "waited"
        if w is None:
            self.stand_down("the cooling engine is still %s" % found)
            w = self._wait("cool idle", idle, COOL_IDLE_S)
            act = "restored (stood the machine down)"
        left.append(Leftover("cool", found, "idle/unarmed/no hold",
                             act if w is not None else "failed: still %s" % found))

    def _pump_side(self, left):
        """The coolant pump runs whenever the engine idles in its own
        posture: the engine writes it on every tick, and off only under a
        quiet hold's pump hold, which it applies at idle alone. A quiet hold
        still standing (every fan off, and the pump with it when a bench
        tool asked) is the run's, and its release puts the engine's posture
        back at its next tick, the fans' duties and the pump with it. A pump
        read off at idle is given two of the engine's ticks to come back on
        (PUMP_TICK_S); one that stays off (an engine that is not writing it)
        is the leftover, switched on here: no fire gate reads the pump. The
        duties themselves are the engine's to keep: its tick reads each back
        and writes again the one it did not command. In any other phase, or
        under a diagnostic, the pump is the engine's or the tool's."""
        c = self.fc_dict("/cool/status")
        if c is None:
            return
        if c.get("quiet_hold"):
            st, _ = self.fc_post("/cool/quiet", data={"on": "0"})
            c = self.fc_dict("/cool/status") or {}
            left.append(Leftover("cool.quiet_hold", True, False,
                                 "restored (released)" if st == 200 and c.get("quiet_hold") is False
                                 else "failed: %s" % st))
        if c.get("armed"):
            # a window opened since the cooling engine's read
            self._window(left, c)
            return
        if c.get("phase") != "idle" or c.get("quiet_hold") or c.get("hold"):
            return
        d = self.fc_dict("/diag/status")
        if d is None or d.get("running"):
            return
        got = hw.sysfs_read(PUMP_ATTR)
        if got is None or got == "1":
            return
        t0 = time.monotonic()
        while time.monotonic() - t0 < PUMP_TICK_S and not self.abort():
            time.sleep(min(0.25, PUMP_TICK_S))
            if hw.sysfs_read(PUMP_ATTR) == "1":
                return
        try:
            hw.sysfs_write(PUMP_ATTR, "1")
            back = hw.sysfs_read(PUMP_ATTR)
            act = "restored" if back == "1" else "failed: reads %s" % back
        except OSError as e:
            act = "failed: %s" % e
        left.append(Leftover(PUMP_ATTR, got, "1", act))

    def _ext_side(self, left):
        """A campaign's machine is extension-free. What a test made and
        left behind (a package under the tests' own prefix, the owner key
        it was signed with) is removed, and the host stops its service.
        The operator's own packages are theirs: running ones are reported
        and not touched."""
        for id_ in [p for p in hw.ext_packages() if p.startswith(hw.EXT_TEST_PREFIX)]:
            try:
                p = subprocess.run([hw.FORGEEXT, "remove", id_], capture_output=True, text=True, timeout=120)
                ok = p.returncode == 0 and id_ not in hw.ext_packages()
                act = "restored" if ok else "failed: %s" % (p.stdout.strip()[-120:] or p.returncode)
            except (OSError, subprocess.SubprocessError) as e:
                act = "failed: %s" % e
            left.append(Leftover("ext.package", id_, "not installed", act))
        keys = hw.EXT_ROOT + "/keys"
        for name in sorted(os.listdir(keys)) if os.path.isdir(keys) else []:
            if not name.startswith("forgetest-"):
                continue
            try:
                os.remove(os.path.join(keys, name))
                act = "restored"
            except OSError as e:
                act = "failed: %s" % e
            left.append(Leftover("ext.owner_key", name, "absent", act))
        # the host stops a removed package's service on its next turn
        running = self._wait("no test package running", lambda: not hw.pool_pids() or None, EXT_STOP_S) is None \
            and hw.pool_pids()
        if running:
            left.append(Leftover("ext.running", "%d process(es) under pool accounts, packages %s"
                                 % (len(running), hw.ext_packages()), "an extension-free machine", "unrestorable"))

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
        held, now = self._lamp_holds(want)
        if held:
            self.log("lid lamp: read %s, then held %s; the fade toward a level just written, "
                     "not a leftover" % (got, want))
            return
        found = now if now is not None else got
        try:
            hw.sysfs_write(LID_LAMP_ATTR, want)
            held, back = self._lamp_holds(want)
            act = "restored" if held else "failed: reads %s" % back
        except OSError as e:
            act = "failed: %s" % e
        left.append(Leftover(LID_LAMP_ATTR, found, want, act))

    def _lamp_holds(self, want):
        """Read the lid lamp every LID_LAMP_POLL_S, for at most
        LID_LAMP_SETTLE_S, until it holds `want`: two reads in a row at
        it, so a fade that only passes through the level does not count.
        Returns (held, the last read)."""
        last = prev = None
        for _ in range(max(2, int(round(LID_LAMP_SETTLE_S / LID_LAMP_POLL_S)))):
            if self.abort():
                break
            time.sleep(LID_LAMP_POLL_S)
            prev, last = last, hw.sysfs_read(LID_LAMP_ATTR)
            if last == want and prev == want:
                return True, last
        return False, last

    def _flow_status(self):
        st, c = self.fc_get("/cool/flow")
        return c if st == 200 and isinstance(c, dict) else {}

    @staticmethod
    def _flow_holds(fs, got):
        """Whether the heater reading @got is the coolant flow watch's own:
        the watch holding, at the duty it reports."""
        duty = fs.get("duty")
        if fs.get("watch") != "hold" or not isinstance(duty, (int, float)):
            return False
        try:
            return abs(int(got) - round(duty * 65535 / 100)) <= FLOW_HEATER_TOL
        except ValueError:
            return False

    def _heater_side(self, left):
        """The loop heater off at idle, unless the coolant flow watch holds
        it, and then at the duty the watch reports. Resting, given way, off,
        or not there at all, the watch leaves it at 0. The watch is read on
        both sides of the heater, so a hold that starts between the reads is
        not taken for a leftover."""
        before = self._flow_status()
        got = hw.sysfs_read(HEATER_ATTR)
        if got is None or got == "0":
            return
        if self._flow_holds(before, got) or self._flow_holds(self._flow_status(), got):
            return
        try:
            hw.sysfs_write(HEATER_ATTR, "0")
            back = hw.sysfs_read(HEATER_ATTR)
            act = "restored" if back == "0" else "failed: reads %s" % back
        except OSError as e:
            act = "failed: %s" % e
        left.append(Leftover(HEATER_ATTR, got, "0", act))

    def _kernel_side(self, left):
        if hw.sysfs_read("cnc/state") is None:
            if host_run():
                self.log("kernel sysfs not present (a host run) - kernel-side checks skipped")
            else:
                # On a machine the kernel side is the latch, the readbacks,
                # the ring and the module's values: none of it is judged.
                left.append(Leftover("kernel sysfs", "cnc/state unreadable", "readable",
                                     "failed: the kernel side was not judged"))
            return
        # The laser's readbacks and the latch were judged first (_laser_side).
        for attr, want in IDLE_READBACKS:
            if attr in LASER_READBACKS:
                continue
            got = hw.sysfs_read(attr)
            if got is not None and got != want:
                left.append(Leftover(attr, got, want, "unrestorable"))
        residue = read_ring_residue()
        if residue:
            left.append(Leftover("pulse ring", "%d unplayed bytes" % residue, "0 (nothing queued)",
                                 "unrestorable: the next run would replay them first"))
        # The values that follow the XY microstep mode are judged against
        # the mode the settings name; with the settings unread they are left
        # as found and named, never written for a default mode. The GRBL
        # controller's own values are judged only in GRBL mode: with the mode
        # unread (forgectrl silent) they may be another controller's, and they
        # are left and named. Under a program still playing none of them is
        # written: it would unmask an axis or change a current mid-move (the
        # kernel state is named above).
        xy = self.xy_mode_read()
        mode_unread = self.mode is None and not host_run()
        playing = hw.sysfs_read("cnc/state") == "running"
        unjudged, unknown, held = [], [], []
        for attr, want in fixed_sysfs(xy or XY_MODE_DEFAULT):
            if self.cloud_mode() and attr in GRBL_CONTROLLER_SYSFS:
                continue
            if playing and attr in GRBL_CONTROLLER_SYSFS:
                held.append(attr)
                continue
            if mode_unread and attr in GRBL_CONTROLLER_SYSFS:
                unknown.append(attr)
                continue
            if xy is None and attr in XY_MODE_SYSFS:
                unjudged.append(attr)
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
        if unjudged:
            left.append(Leftover("xy_microsteps", UNREADABLE, "the settings' mode",
                                 "failed: %s not judged" % ", ".join(unjudged)))
        if unknown:
            left.append(Leftover("controller mode", UNREADABLE, "the mode in force",
                                 "failed: %s not judged" % ", ".join(unknown)))
        if held:
            self.log("a program still plays: the controller's values (%s) are not written under it"
                     % ", ".join(held))
        self._heater_side(left)
        # A fault that still stands keeps the engine's red flash up: that is
        # the machine's own state, not a leftover (the attention check above
        # names the fault).
        st, c = self.fc_get("/cool/status")
        if st == 200 and isinstance(c, dict) and c.get("armed"):
            self._window(left, c)       # a window opened since the cooling engine's read
        red_standing = st == 200 and isinstance(c, dict) and (c.get("attention") or {}).get("standing")
        for name in BUTTON_LEDS:
            got = read_led(name)
            pulse = read_led_attr(name, "pulse_on")
            if red_standing:
                continue
            if pulse not in (None, "0"):
                # A pulsing LED swings its target between 0 and its level:
                # the pulse is what says it was left on.
                try:
                    write_led(name, 0)
                    act = "restored"
                except OSError as e:
                    act = "failed: %s" % e
                left.append(Leftover("leds/" + name, "pulse %s" % pulse, "0", act))
            elif got is not None and got != "0":
                try:
                    write_led(name, 0)
                    act = "restored"
                except OSError as e:
                    act = "failed: %s" % e
                left.append(Leftover("leds/" + name, got, "0", act))
            elif (read_led(name, "brightness") or "0") != "0":
                self.log("leds/%s: target 0, brightness %s; the fade from a level the machine "
                         "itself ended, not a leftover" % (name, read_led(name, "brightness")))

    def _return_head(self, was, now, spm=XY_STEPS_PER_MM):
        """Jog the head back along its own path by the kernel-measured X/Y
        delta (Z is never touched), through the GRBL controller. spm is
        the counters' scale (the mode's). Bounded: beyond RETURN_MAX_MM
        per axis, or without a running GRBL controller, the counters are
        reported and left."""
        dx = (now[0] - was[0]) / spm
        dy = (now[1] - was[1]) / spm
        if abs(dx) < 0.02 and abs(dy) < 0.02:
            return "unrestorable (Z only)" if now[2] != was[2] else "restored"
        if abs(dx) > RETURN_MAX_MM or abs(dy) > RETURN_MAX_MM:
            return "unrestorable: %.1f/%.1f mm exceeds %.0f mm" % (dx, dy, RETURN_MAX_MM)
        # Never jog on top of stale bytes: a run started now would replay
        # whatever the ring still holds before the jog, in a direction and
        # for a distance nobody asked for. Report and leave the head.
        # Stale bytes in the ring are the last job's, and a jog now would
        # replay them before its own. The ring empties when the
        # controller restarts, so the hand-back restarts it rather than
        # leaving the head where it is and the bytes where they are.
        residue = read_ring_residue()
        if residue:
            self.log("baseline: %d unplayed bytes in the kernel ring before the return" % residue)
            self.stand_down("%d unplayed bytes in the kernel ring" % residue)
            residue = read_ring_residue()
            if residue:
                return ("unrestorable: %d unplayed bytes still queued in the kernel ring after a "
                        "controller restart - a jog would replay them" % residue)
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
        self._latch_before("a Grbl port connection")
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
        # A controller start or a home during the run re-zeroed the counters
        # wherever the head then stood. Unless the test said where that was
        # (ctx.counters_rezeroed), the two readings share no frame: on the
        # bench reference a head that had not moved was "returned" 30 mm into
        # the gantry stops this way, twice. Nothing is moved on a guess.
        frame_lost = ("frame" in captured and captured["frame"] != counter_frame()
                      and not captured.get("rezero_declared"))
        if was is not None and now is not None and now != was and frame_lost and not self.cloud_mode():
            self.log("position: %s, and %s when the run began, but the controller re-zeroed its "
                     "counters during the run (a controller start or a home): the two are not in "
                     "one frame, so the head's position cannot be judged and the head is not moved"
                     % (now, was))
        elif was is not None and now is not None and now != was and not self.cloud_mode():
            spm = self._counter_spm
            act = self._return_head(was, now, spm)
            if position_quantized(was, now, spm):
                self.log("position: %s (expected %s) -> %s; inside the %.2f mm dead band, "
                         "not a leftover" % (now, was, act, POSITION_DEADBAND_MM))
            else:
                left.append(Leftover("position", now, was, act))
        # A controller restart during the run (a takeover's) re-zeroed the
        # counters wherever the head then stood, so a head left out before
        # it reads as home from here. The takeover judged it on the spot
        # (runner.Takeover); what it recorded is a leftover, never moved.
        for r in captured.get("restart_positions") or []:
            left.append(Leftover("position at the %s" % r["where"], r["found"], r["expected"],
                                 "unrestorable: a controller restart re-zeroed the counters there, "
                                 "so the head is not moved"))
        # The cloud client forgectrl's start brought back at the end of a
        # takeover, when it is not the one the takeover found (the same
        # judge, runner.Takeover). The client that came up is left as it
        # is: the cloud test that needs a client starts its own.
        for r in captured.get("restart_clients") or []:
            left.append(Leftover("cloud client at the %s" % r["where"], r["found"], r["expected"],
                                 "not restored: a cloud test starts the client it needs"))
        # What else the takeover's end found: an underrun it acknowledged, a
        # forgectrl that did not start or answer (runner.Takeover).
        for r in captured.get("restart_leftovers") or []:
            left.append(Leftover(r["item"], r["found"], r["expected"], r["action"]))
        self._grbl_settings_side(left, captured.get("grbl"))
        was = captured.get("settings")
        if was:
            body = self.fc_dict("/settings")
            if body is not None:
                # a settings write can restart the controller (xy_microsteps
                # under GRBL), and forgectrl's stop locks the latch
                if any(body.get(k) != v for k, v in was.items()):
                    self._latch_before("a settings hand-back")
                for k, v in was.items():
                    if body.get(k) == v:
                        continue
                    st2, b2 = (self.fc_post("/settings", params={k: ""}) if v == ""
                               else self.fc_post("/settings", data={k: v}))
                    left.append(Leftover("settings." + k, body.get(k), v,
                                         "restored" if st2 == 200 else "failed: %s %s" % (st2, b2)))

    def _grbl_settings_side(self, left, was):
        """The GRBL controller's $ settings as the run found them, compared
        while the same mode runs (the settings hand-back after this can
        restart the controller), by the groups above."""
        if not was or self.mode != "grbl":
            return
        now = self.grbl_settings()
        if now is None:
            return                  # no controller to compare: the controller leftover names it
        changed = sorted((k for k in was if now.get(k) != was[k]), key=lambda k: int(k[1:]))
        derived = [k for k in changed if k in GRBL_DERIVED_SETTINGS]
        if derived:
            self.log("grbl: %s, derived from the settings, moved during the run (%s); not judged here"
                     % (", ".join(derived), ", ".join("%s %s -> %s" % (k, was[k], now.get(k)) for k in derived)))
        changed = [k for k in changed if k not in GRBL_DERIVED_SETTINGS]
        if not changed:
            return
        restore = [k for k in changed if k not in GRBL_PINNED_SETTINGS + GRBL_LASER_SETTINGS]
        back = {}
        if restore:
            self._latch_before("a Grbl port connection")
            try:
                with hw.Grbl() as g:
                    for k in restore:
                        reply = g.command("%s=%s" % (k, was[k]))
                        if reply and reply[-1] == "ok":
                            got = parse_grbl_settings("\n".join(g.command(k))) or {}
                            back[k] = "restored" if got.get(k) == was[k] else "failed: reads %s" % got.get(k)
                        else:
                            back[k] = "failed: %s" % ("; ".join(reply) or "no reply")
            except (hw.HwError, OSError) as e:
                back = {k: back.get(k) or "failed: %s" % e for k in restore}
        for k in changed:
            if k in GRBL_PINNED_SETTINGS:
                act = "not restored: the driver pins it"
            elif k in GRBL_LASER_SETTINGS:
                act = "not restored: a laser setting"
            else:
                act = back[k]
            left.append(Leftover("grbl." + k, now.get(k), was[k], act))


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
