# Copyright 2026 514 LLC d/b/a OpenGlow
# Written by Scott Wiederhold
# https://community.openglow.org
# SPDX-License-Identifier:    MIT

"""kernel.* - glowforge.ko safety readbacks and the pulse-engine drills.

The drills are the bench scripts `scripts/bench/gate_a_kernel_drills.py`
(K1/K2/K3) and `scripts/bench/fire_test.py` (A/B/U) with their proven
sequences kept intact; they run under a hardware takeover (forgectrl and
the controller stopped, the pulse device free) and judge the software
witnesses: cnc/state, laser_enable (the FIRE line), laser_on and
laser_on_sampled (the gated LASER_ON output), interlock_circuit bit 3
(the commanded latch), faults and underruns. Every drill forces duty to
zero before any FIRE bit, keeps motor_lock=15 (no axis moves), re-locks
the latch on every exit path, and ends with a halt and a stop: none drops
the 40 V rail.

The phases that unlock the latch for a run (fire-line B, U, R and K3,
resume-lead L) put FIRE in play under the HV their own run raises: a run
feeds the charge-pump watchdog, and HV_ENABLE follows it with the lid
closed. So each runs with the safety chain held open at the
remote-interlock loop: the kernel drives INTERLOCK_RESET high while the
loop reads open, and the interlock latch blocks LASER_ON in hardware,
which no button press clears. Before each unlock the drill reads the
chain open (interlock_circuit bit 4 and the interlock latch's switch);
right after the unlock it reads the button latch set (bit 2: the lock set
it, and only a press clears it), and in every sample of the run it reads
the button latch set, the loop still open and LASER_ON off (bit 0, raw,
active low). A reading that says otherwise relocks the latch and fails
the test. The largest gap between two samples of each sampling loop is
kept in the evidence (sample_gap_ms). The check before a drill that
nothing feeds the watchdog (the charge pump dead and the engine idle)
says only that no other run holds HV up when the drill begins.
"""
import errno
import os
import struct
import time

try:
    import fcntl
except ImportError:          # host unit tests import the suite off-target
    fcntl = None

from ..catalog import test
from .. import hw
from ..runner import Failed

# interlock_circuit bits (docs.forgefirm.org, the kernel module page): bit 0 =
# the LASER_ON readback, raw and active low (clear = LASER_ON active), bit 2 =
# the button latch (set = fire blocked; the pin cnc/button_latch reads), bit 3 =
# the driven latch line (set = locked), bit 4 = INTERLOCK_RESET as driven (set =
# the interlock latch held set: the loop reads open).
LASER_ON_RAW_BIT = 1 << 0
BUTTON_LATCH_BIT = 1 << 2
LATCH_BIT = 1 << 3
INTERLOCK_RESET_BIT = 1 << 4

TICK_HZ = 10000
FIRE = b"\x10"
PAD = b"\x00"
XSTEP = b"\x01"                 # +X (DIR clear)
XSTEP_BACK = b"\x03"            # -X (DIR set)
POWER0 = bytes([0x80])          # power byte, duty 0

_KERNEL_COVERS = [("kernel-module-glowforge", "**"), ("linux-fslc", "**")]


# ---------------------------------------------------------------- helpers

def wr(attr, val):
    hw.sysfs_write(attr, val)


def rd(attr):
    v = hw.sysfs_read(attr)
    if v is None:
        raise Failed("cannot read %s" % attr)
    return v


def rd_pos():
    with open(hw.sysfs_root() + "cnc/position", "rb") as f:
        raw = f.read(32)
    return struct.unpack("<5i", raw[:20])


# The dead man's switch puts the thermal driver in its safe state too: the
# heater's duty to zero (through the lease write, so no lapse is counted)
# and the heater's and the TEC's pins low. The close test drives both on
# just before the close, so the switch has something to undo: the heater
# at full duty, which its PWM timer holds as a steady high pin, for well
# under a second.
DEADMAN_HEATER_DUTY = 65535
GPIO_DEBUG = "/sys/kernel/debug/gpio"
GPIO_LABELS = {"heater": "water-heater-gpio", "tec": "tec-gpio"}


def gpio_levels(path=GPIO_DEBUG):
    """The heater's and the TEC's pin levels ("hi"/"lo") as the GPIO
    debug listing shows them; None for a pin it does not list."""
    out = {k: None for k in GPIO_LABELS}
    try:
        with open(path) as f:
            text = f.read()
    except OSError:
        return out
    for line in text.splitlines():
        for k, label in GPIO_LABELS.items():
            if "|" + label in line:
                words = line.split(")", 1)[-1].split()
                out[k] = words[1] if len(words) > 1 and words[1] in ("hi", "lo") else None
    return out


def deadman_thermal_why(before, after):
    """None when the close took the heat sources to their safe state, else
    why not. before and after: heater_pwm, tec_on and heater_lapses as read,
    and the two pins' levels."""
    if (before.get("heater_pwm") != str(DEADMAN_HEATER_DUTY) or before.get("tec_on") != "1"
            or before.get("heater_pin") != "hi" or before.get("tec_pin") != "hi"):
        return ("the heater and the TEC were not on before the close (heater_pwm %s pin %s, tec_on %s "
                "pin %s)" % (before.get("heater_pwm"), before.get("heater_pin"), before.get("tec_on"),
                             before.get("tec_pin")))
    if (after.get("heater_pwm") != "0" or after.get("tec_on") != "0"
            or after.get("heater_pin") != "lo" or after.get("tec_pin") != "lo"):
        return ("the close left the heat sources on (heater_pwm %s pin %s, tec_on %s pin %s; want 0 lo, "
                "0 lo)" % (after.get("heater_pwm"), after.get("heater_pin"), after.get("tec_on"),
                           after.get("tec_pin")))
    if after.get("heater_lapses") != before.get("heater_lapses"):
        return ("the heater went off by its lease, not by the switch (heater_lapses %s -> %s)"
                % (before.get("heater_lapses"), after.get("heater_lapses")))
    if after.get("tec_lapses") != before.get("tec_lapses"):
        return ("the TEC went off by its lease, not by the switch (tec_lapses %s -> %s)"
                % (before.get("tec_lapses"), after.get("tec_lapses")))
    return None


# The close test lights the head's emitters and raises the lens current
# before the close, so the head's safe state has something to undo. The
# measuring laser and the UV LED are low-power and safe to light; the lens
# holds its run current from right before the close until it.
DEADMAN_UV = 64
DEADMAN_MEASURE = 1


def deadman_head_why(before, after):
    """None when the close took the head to its safe state from a head that
    was not in it, else why not. before: read back right before the close,
    the measuring laser and the UV LED lit, the lens driver enabled at run
    current (z_enable 0, z_current 0); after: the driver disabled at low
    current (1, 1), both emitters off."""
    if (before.get("z_enable") != "0" or before.get("z_current") != "0"
            or before.get("measure_laser") in (None, "0") or before.get("uv_led") in (None, "0")):
        return ("the head was not lit and driven before the close (z_enable %s z_current %s measure_laser %s "
                "uv_led %s; want 0, 0, lit, lit)" % (before.get("z_enable"), before.get("z_current"),
                                                    before.get("measure_laser"), before.get("uv_led")))
    if after.get("z_enable") != "1" or after.get("z_current") != "1":
        return ("the head was not safed: z_enable %s z_current %s (want 1, 1: driver disabled, low current)"
                % (after.get("z_enable"), after.get("z_current")))
    if after.get("measure_laser") != "0" or after.get("uv_led") != "0":
        return "measure laser %s, UV LED %s after the close" % (after.get("measure_laser"), after.get("uv_led"))
    return None


def idle_close_why(unlocked, closed):
    """The final close at idle, with nothing relocking it but the kernel:
    None when the unlock took (interlock_circuit bit 3 clear before the
    close) and the close locked the latch (bit 3 set after it, read before
    any other lock), else why not."""
    if unlocked is None or unlocked & LATCH_BIT:
        return ("the unlock before the close did not take (interlock_circuit %s): the close has nothing to "
                "relock" % unlocked)
    if closed is None or not closed & LATCH_BIT:
        return "the final close did not lock the latch (interlock_circuit %s)" % closed
    return None


def thermal_read():
    pins = gpio_levels()
    return {"heater_pwm": rd("thermal/heater_pwm"), "tec_on": rd("thermal/tec_on"),
            "heater_lapses": rd("thermal/heater_lapses"), "tec_lapses": rd("thermal/tec_lapses"),
            "heater_pin": pins["heater"], "tec_pin": pins["tec"]}


def snap(ctx, tag):
    line = ("%s: state=%s laser_enable=%s laser_on=%s laser_on_sampled=%s interlock=%s"
            % (tag, rd("cnc/state"), rd("cnc/laser_enable"), rd("cnc/laser_on"),
               rd("cnc/laser_on_sampled"), rd("cnc/interlock_circuit")))
    ctx.log(line)
    return line


def wait_state(ctx, want, timeout, poll=0.05):
    t0 = time.monotonic()
    while time.monotonic() - t0 < timeout:
        ctx.checkpoint()
        s = rd("cnc/state")
        if s == want:
            return s
        time.sleep(poll)
    return rd("cnc/state")


class SampleGaps:
    """The largest gap between two samples of one sampling loop, in ms, kept
    in the evidence as sample_gap_ms[key]: what the loop's judgment can miss,
    as measured on the machine that ran it."""

    def __init__(self, ctx, key):
        self.gaps = ctx.evidence.setdefault("sample_gap_ms", {})
        self.key = key
        self.last = None
        self.gaps[key] = None

    def tick(self):
        now = time.monotonic()
        if self.last is not None:
            gap = round((now - self.last) * 1000.0, 2)
            if self.gaps[self.key] is None or gap > self.gaps[self.key]:
                self.gaps[self.key] = gap
        self.last = now


def watch_laser_until_idle(ctx, timeout, guard=None):
    """Tight-loop laser_enable/laser_on watch; returns (hits, end_state).
    guard: a phase tag for a run with the latch unlocked: every sample also
    judges the chain (run_guard), and the loop's largest gap is kept."""
    hits = []
    t0 = time.monotonic()
    state = "running"
    n = 0
    gaps = SampleGaps(ctx, guard) if guard is not None else None
    while time.monotonic() - t0 < timeout:
        if guard is not None:
            gaps.tick()
            run_guard(guard)
        en = rd("cnc/laser_enable")
        on = rd("cnc/laser_on")
        if en != "0" or on != "0":
            hits.append((round(time.monotonic() - t0, 4), en, on))
        state = rd("cnc/state")
        if state != "running":
            break
        n += 1
        if n % 200 == 0:
            ctx.checkpoint()
    return hits, state


# ---------------------------------------------------------------- the opened chain

CHAIN_TIMEOUT_S = 180


def chain_open_why():
    """None while the safety chain is held open at the remote-interlock
    loop, else why not: the kernel drives INTERLOCK_RESET high
    (interlock_circuit bit 4), the switches read the loop open (EV_SW 5)
    and the interlock latch set (EV_SW 6). The latch is set-dominant, so
    with its SET held high LASER_ON stays blocked whatever the button
    does."""
    ilk = hw.sysfs_int("cnc/interlock_circuit")
    if ilk is None:
        return "cnc/interlock_circuit unreadable"
    if not ilk & INTERLOCK_RESET_BIT:
        return "the kernel does not hold the interlock latch set (interlock_circuit 0x%x): the loop reads closed" % ilk
    sw = hw.switch_bits()
    if sw is None:
        return "the switches cannot be read: the interlock latch is not shown set"
    if not (sw >> hw.SW_INTERLOCK) & 1:
        return "the switches read the interlock loop closed"
    if not (sw >> hw.SW_INTERLOCK_LATCH) & 1:
        return "the interlock latch reads clear"
    return None


def chain_closed():
    """The loop closed again: the kernel has released INTERLOCK_RESET and
    the switches read the loop closed and the interlock latch clear (a
    reading the switches cannot give is not counted against it)."""
    ilk = hw.sysfs_int("cnc/interlock_circuit")
    if ilk is None or ilk & INTERLOCK_RESET_BIT:
        return False
    sw = hw.switch_bits()
    return sw is None or not ((sw >> hw.SW_INTERLOCK) & 1 or (sw >> hw.SW_INTERLOCK_LATCH) & 1)


CHAIN_TEXT = ("Leave it open until the test asks for it back: the drill unlocks the latch, with FIRE "
              "driven at zero duty, only while the chain is held open there.")


def open_chain(ctx):
    """The remote-interlock loop opened, by the bench actuator or a person,
    and read open on the machine before any phase runs."""
    ctx.act("interlock", "open", until=lambda: chain_open_why() is None, timeout=CHAIN_TIMEOUT_S,
            text=CHAIN_TEXT)
    ctx.evidence["chain_opened"] = True
    ctx.log("chain held open at the remote-interlock loop: INTERLOCK_RESET driven, interlock latch set")


def close_chain(ctx):
    """The loop closed again at the drill's end, after the latch is locked.
    Returns whether the machine reads it closed; never raises."""
    try:
        return ctx.act("interlock", "close", until=chain_closed, timeout=CHAIN_TIMEOUT_S,
                       fail=False) is not None
    except Exception as e:  # noqa: BLE001 - an Abort or a box error must not mask the drill's own end
        ctx.log("the interlock loop was not closed again (%s: %s)", type(e).__name__, e)
        return False


def relock_fail(tag, why):
    """Lock the latch at once, then fail the phase."""
    try:
        wr("cnc/laser_latch", 1)
    except OSError:
        pass                    # the phase's own finally locks again
    raise Failed("%s: %s - the latch is relocked" % (tag, why))


def unlock_behind_chain(ctx, tag):
    """Unlock the latch for a phase's run: refused unless the chain reads
    open, and the button latch read right after the unlock. Under the lock
    the button latch reads set whatever happened (the lock is its SET), so
    only a read after the unlock shows that no press cleared it."""
    why = chain_open_why()
    if why is not None:
        raise Failed("%s: %s - refusing the latch unlock" % (tag, why))
    wr("cnc/laser_latch_cooling", 0)        # the takeover's final close set both holders
    wr("cnc/laser_latch", 0)
    bl = hw.sysfs_read("cnc/button_latch")
    ctx.evidence.setdefault("button_latch_after_unlock", {})[tag] = bl
    if bl != "1":
        relock_fail(tag, "the button latch reads %s right after the unlock (a press cleared it, or it "
                         "cannot be read)" % bl)


def run_guard_why(ilk):
    """None while a sample of interlock_circuit says an unlocked run is still
    dark behind both blocks: LASER_ON off (bit 0 set: the readback is raw and
    active low), the button latch set (bit 2) and the interlock latch held
    set (bit 4); else why not."""
    if ilk is None:
        return "cnc/interlock_circuit unreadable"
    if not ilk & LASER_ON_RAW_BIT:
        return "LASER_ON active mid-run (interlock_circuit 0x%x)" % ilk
    if not ilk & INTERLOCK_RESET_BIT:
        return "the interlock loop reads closed mid-run (interlock_circuit 0x%x)" % ilk
    if not ilk & BUTTON_LATCH_BIT:
        return "the button latch cleared mid-run (interlock_circuit 0x%x): a press" % ilk
    return None


def run_guard(tag):
    """One sample's judgment of an unlocked run; relocks and fails on any
    reason. Returns the sample."""
    ilk = hw.sysfs_int("cnc/interlock_circuit")
    why = run_guard_why(ilk)
    if why is not None:
        relock_fail(tag, why)
    return ilk


def safe_end(ctx):
    """The drill's end on every path: the latch locked, then the engine
    halted (a program a phase left playing ends at once) and stopped (a
    halt refuses an underrun, which the stop acknowledges), each write on
    its own. The 40 V rail stays up: a disable drops it, and every rail
    cycle can wedge a stepper driver."""
    for attr in ("cnc/laser_latch", "cnc/halt", "cnc/stop"):
        try:
            wr(attr, 1)
        except OSError as e:
            ctx.log("safe state: %s refused (%s)", attr, e)


# ---------------------------------------------------------------- a run that played

# The byte counter and the ring head are different SDMA registers: a few
# bytes of skew between them, never a phase's worth.
PLAYED_SKEW = 8
# K2's resume lead: longer than the acceleration at 10 kHz and 125000 Hz/s
# (a shorter forward lead is refused, ERANGE).
K2_LEAD = 500


def replay_why(started, at_stop, processed, total, window_at, window_len):
    """None when a resume after a stop played the program to its end over a
    window still ahead of the stop, else why not: the resume ran (the state
    read running), the stop came before the window's first byte, the
    processed counter reached the program's size, and it counted every byte
    of the window (the size check allows a few bytes of skew, which a window
    that ends the program would otherwise pass short). A resume that plays
    nothing leaves the counters where they were and passes no other check."""
    if started != "running":
        return "the resume never ran (state %s)" % started
    if at_stop is None or at_stop >= window_at:
        return "the stop came at byte %s, not ahead of the window at byte %d" % (at_stop, window_at)
    if processed is None or total is None or total - processed > PLAYED_SKEW:
        return "the resume stopped short: %s of %s bytes processed" % (processed, total)
    if processed < window_at + window_len:
        return "the window (bytes %d to %d) was not played: %d processed" % (
            window_at, window_at + window_len, processed)
    return None


# ---------------------------------------------------------------- the backward run's tail

RAMP_MIN_HZ = 900
RAMP_UPDATES_PER_SEC = 100
BACKTRACK_LATENCY_INTERVALS = 2
BACKTRACK_BYTES_PER_TICK = 2


def backtrack_tail(step_freq, ramp_rate):
    """The bytes a backward run plays past its waypoint, as the kernel
    counts them (cnc_backtrack_decel_steps): the controlled deceleration's
    ticks, one 10 ms interval at each frequency from step_freq down to the
    900 Hz floor, each counted whole, plus two intervals at step_freq for a
    late waypoint and late ramp updates, at two bytes a tick, plus one for
    the waypoint's own tick. 1323 at 10 kHz and 125000 Hz/s. None for a
    ramp that never decelerates."""
    delta = ramp_rate // RAMP_UPDATES_PER_SEC
    if delta == 0:
        return None
    per = RAMP_UPDATES_PER_SEC
    freq, ramp = step_freq, 0
    while True:
        ramp += (freq + per - 1) // per
        if freq <= RAMP_MIN_HZ:
            break
        freq = freq - delta if freq > RAMP_MIN_HZ + delta else RAMP_MIN_HZ
    latency = BACKTRACK_LATENCY_INTERVALS * ((step_freq + per - 1) // per)
    return (ramp + latency) * BACKTRACK_BYTES_PER_TICK + 1


class PulseDevice:
    """Exclusive hold of /dev/glowforge for one drill."""

    def __init__(self, ctx):
        self.ctx = ctx
        self.fd = None

    def __enter__(self):
        try:
            self.fd = os.open("/dev/glowforge", os.O_WRONLY)
        except OSError as e:
            if e.errno == errno.EBUSY:
                raise Failed("/dev/glowforge is busy - the takeover did not free the pulse device")
            raise
        fcntl.flock(self.fd, fcntl.LOCK_EX)
        return self

    def write(self, data):
        os.write(self.fd, data)

    def rewind(self):
        os.lseek(self.fd, 1, os.SEEK_SET)

    def drop(self):
        """Close the device with the lock still held: the dead man's switch."""
        fd, self.fd = self.fd, None
        os.close(fd)

    def __exit__(self, *exc):
        try:
            wr("cnc/laser_latch", 1)         # re-lock unconditionally
        finally:
            if self.fd is not None:
                fcntl.flock(self.fd, fcntl.LOCK_UN)
                os.close(self.fd)
        return False


def hv_off_reason():
    """Why something may be holding HV up right now, or None. The chain
    asserts HV_ENABLE only while a run feeds the charge-pump watchdog, so a
    dead watchdog and an idle pulse engine mean no run holds it up before a
    drill starts. A drill's own run raises HV again: this is not a guard of
    the run (the opened chain is). laser_pgood is the supply's power-good,
    high whenever the supply is healthy, and says nothing about HV, so it
    plays no part here."""
    alive = rd("cnc/charge_pump_alive")
    state = rd("cnc/state")
    if alive is None or state is None:
        return "cnc/charge_pump_alive or cnc/state unreadable"
    if alive != "0" or state != "idle":
        return ("charge_pump_alive=%s state=%s: the chain may be holding HV_ENABLE up" % (alive, state))
    return None


def hv_off():
    """Precheck for the drills that unlock the latch: they start only while
    nothing feeds the watchdog. At idle nothing does, so this refuses a
    start only on a machine that is not idle the way it should be."""
    why = hv_off_reason()
    if why:
        return ("%s; this drill starts only while no run feeds the charge-pump watchdog "
                "(let the machine go idle, then start again)" % why)
    return None


HV_RELEASE_S = 3.0


def wait_hv_off(ctx, timeout_s=HV_RELEASE_S):
    """hv_off_reason() once the chain has released, or the reason that still
    stands after timeout_s. A run feeds the charge-pump watchdog every
    200 ms and the one-shot holds ALIVE for 0.45 s after the last feed (the
    feed's soft timer can add one more feed after the state leaves
    running), so a phase that follows a run finds the chain still up for
    under a second. The wait covers that release and nothing more; the
    time it took is logged when it was not immediate and kept in the
    evidence."""
    t0 = time.monotonic()
    why = hv_off_reason()
    while why is not None and time.monotonic() - t0 < timeout_s:
        ctx.sleep(0.05)
        why = hv_off_reason()
    dt = round(time.monotonic() - t0, 2)
    ctx.evidence.setdefault("hv_release_s", []).append(dt)
    if why is not None or dt >= 0.1:
        ctx.log("hv off: %s after %.2f s", "released" if why is None else "still held", dt)
    return why


def require_hv_off(ctx):
    """The same rule at the start of the run (the precheck ran a moment
    earlier; the machine must still agree)."""
    why = wait_hv_off(ctx)
    ctx.evidence["charge_pump_alive"] = rd("cnc/charge_pump_alive")
    ctx.evidence["kernel_state"] = rd("cnc/state")
    ctx.check(why is None, "%s - refusing the latch unlock", why)


def check_hv_off(ctx):
    """The same check right before a phase (no prompt: forgectrl is down):
    the run before it has released the watchdog."""
    why = wait_hv_off(ctx)
    ctx.check(why is None, "%s - refusing the latch unlock", why)


# ---------------------------------------------------------------- readbacks

@test("kernel.latch-locked-idle", title="Laser latch locked at idle", subsystem="kernel",
      kind="auto", always=True, est_min=1,
      covers=_KERNEL_COVERS + [("forgectrl", "src/super.c"),
                               ("grblhal-glowforge", "src/glowforge_laser.c"),
                               ("grblhal-glowforge", "src/driver.c")],
      description="With the machine idle the kernel latch reads locked, the FIRE line is not "
                  "driven, no LASER_ON sample is seen, and no stepper fault is pending; forgectrl "
                  "agrees.")
def latch_locked_idle(ctx):
    ev = ctx.evidence
    state = hw.sysfs_read("cnc/state")
    ev["cnc_state"] = state
    ctx.log("cnc/state: %s", state)
    ctx.check(state is not None, "cnc/state unreadable")
    ctx.check(state in ("idle", "disabled"), "machine is %r, run this test at idle", state)

    ilk = hw.sysfs_int("cnc/interlock_circuit")
    ev["interlock_circuit"] = ilk
    ctx.check(ilk is not None, "cnc/interlock_circuit unreadable")
    ctx.log("interlock_circuit: %d (0x%x)", ilk, ilk)
    ctx.check(ilk & LATCH_BIT, "latch line reads unlocked at idle (bit 3 clear)")

    fire = hw.sysfs_int("cnc/laser_enable")
    ev["laser_enable"] = fire
    ctx.log("laser_enable (FIRE line): %s", fire)
    ctx.check(fire == 0, "FIRE line driven at idle (laser_enable=%s)", fire)

    on = hw.sysfs_int("cnc/laser_on")
    on_s = hw.sysfs_int("cnc/laser_on_sampled")
    ev["laser_on"] = on
    ev["laser_on_sampled"] = on_s
    ctx.log("laser_on: %s, laser_on_sampled: %s", on, on_s)
    ctx.check(on == 0, "LASER_ON active at idle")
    ctx.check(on_s == 0, "LASER_ON samples seen at idle (%s)", on_s)

    faults = hw.sysfs_int("cnc/faults")
    ev["faults"] = faults
    ctx.log("faults: %s", faults)
    ctx.check(faults == 0, "stepper faults pending: %s", faults)

    st = ctx.forgectrl.status()
    ev["forgectrl_laser_locked"] = st.get("laser_locked")
    ctx.log("forgectrl /status laser_locked=%s state=%s", st.get("laser_locked"), st.get("state"))
    ctx.check(st.get("laser_locked") is True, "forgectrl reports the latch unlocked")


# ---------------------------------------------------------------- K1 + K2

@test("kernel.k1-k2", title="Controlled-stop floor and resume honors the locked latch",
      subsystem="kernel", kind="auto", hardware="takeover", always=True, est_min=2,
      covers=_KERNEL_COVERS,
      requires=["kernel.latch-locked-idle"],
      description="K1: a controlled stop mid-run ramps the step frequency down (tens of ms), "
                  "never consumes the tail as a burst or hangs. K2: with the latch locked, a "
                  "stop inside the leading pads and a resume with a positive waypoint replays "
                  "a 2 s FIRE window with laser_enable/laser_on at 0 throughout: the resume reads "
                  "running and the processed counter reaches the program's end, past the window. "
                  "Motors locked; duty zero.")
def k1_k2(ctx):
    ev = ctx.evidence
    with ctx.takeover():
        # ---- K1
        stream = POWER0 + PAD * (6 * TICK_HZ)
        ctx.log("K1: %d bytes = %.1f s of pads at %d Hz, ramp 125000 Hz/s",
                len(stream), len(stream) / TICK_HZ, TICK_HZ)
        snap(ctx, "K1 pre")
        wr("cnc/motor_lock", 15)
        wr("cnc/laser_latch", 1)
        wr("cnc/ramp_rate", 125000)
        wr("cnc/step_freq", TICK_HZ)
        with PulseDevice(ctx) as dev:
            dev.rewind()
            wr("cnc/enable", 1)
            ctx.sleep(0.5)
            dev.write(stream)
            wr("cnc/run", 1)
            ctx.sleep(1.5)                      # well past the accel ramp
            st = rd("cnc/state")
            ctx.check(st == "running", "K1: expected running before the stop, got %s", st)
            t0 = time.monotonic()
            wr("cnc/stop", 1)
            while time.monotonic() - t0 < 5:
                if rd("cnc/state") != "running":
                    break
            dt = time.monotonic() - t0
            state = rd("cnc/state")
            faults = rd("cnc/faults")
            ev["k1"] = {"stop_to_idle_s": round(dt, 4), "state": state, "faults": faults}
            ctx.log("K1 controlled stop: state=%s after %.4f s, faults=%s", state, dt, faults)
            # drain the paused remainder laser-less so the device ends clean
            wr("cnc/resume", 0)
            wait_state(ctx, "running", 2, poll=0.005)
            wait_state(ctx, "idle", 10)
        ctx.check(state == "idle", "K1: state %s after the stop", state)
        ctx.check(dt >= 0.02, "K1: stop consumed the tail as a burst (%.4f s) - decel floor broken", dt)
        ctx.check(dt <= 3.0, "K1: stop took %.4f s", dt)
        ctx.check(faults == "0", "K1: faults=%s", faults)
        ctx.log("K1 PASS: decelerating tail %.4f s, no burst, no fault", dt)

        # ---- K2
        # 1000 X steps out and 1000 back at 2 kHz (masked by motor_lock):
        # the position counters end where they started
        step_sec = (XSTEP + PAD * 4) * 1000 + (XSTEP_BACK + PAD * 4) * 1000
        lead = K2_LEAD
        fire_at = len(POWER0 + PAD * TICK_HZ + step_sec + PAD * (TICK_HZ // 2))
        stream = (POWER0 + PAD * TICK_HZ + step_sec + PAD * (TICK_HZ // 2)
                  + FIRE * (2 * TICK_HZ) + PAD * TICK_HZ)
        ctx.log("K2: %d bytes = %.1f s; latch stays LOCKED; waypoint +%d; motor_lock=15",
                len(stream), len(stream) / TICK_HZ, lead)
        snap(ctx, "K2 pre")
        wr("cnc/motor_lock", 15)
        wr("cnc/laser_latch", 1)
        wr("cnc/ramp_rate", 125000)
        wr("cnc/step_freq", TICK_HZ)
        with PulseDevice(ctx) as dev:
            dev.rewind()
            wr("cnc/enable", 1)
            ctx.sleep(0.5)
            dev.write(stream)
            pos_before = rd_pos()
            wr("cnc/run", 1)
            ctx.sleep(0.4)                      # inside the initial pads
            wr("cnc/stop", 1)
            state = wait_state(ctx, "idle", 5, poll=0.01)
            ctx.check(state == "idle", "K2: controlled stop did not reach idle (state=%s)", state)
            at_stop = rd_pos()
            ctx.log("K2: paused inside the pads; resuming with waypoint +%d (latch LOCKED)", lead)
            wr("cnc/resume", lead)
            started = wait_state(ctx, "running", 2, poll=0.005)
            hits, state = watch_laser_until_idle(ctx, 20)
            pos_after = rd_pos()
            snap(ctx, "K2 post")
            ev["k2"] = {"hits": hits[:10], "end_state": state, "pos_before": pos_before,
                        "at_stop": at_stop, "resume_started": started,
                        "pos_after": pos_after, "laser_on_sampled": rd("cnc/laser_on_sampled"),
                        "underruns": rd("cnc/underruns"), "faults": rd("cnc/faults")}
            ctx.log("K2 done: state=%s pos before=%s after=%s", state, pos_before, pos_after)
        ctx.check(not hits, "K2: laser asserted with the latch locked: %s", hits[:10])
        why = replay_why(started, at_stop[3], pos_after[3], pos_after[4], fire_at, 2 * TICK_HZ)
        ctx.check(why is None, "K2: %s", why)
        ctx.check(pos_before[:3] == pos_after[:3],
                  "K2: position counters did not return to start (%s -> %s)", pos_before[:3], pos_after[:3])
        ctx.log("K2 PASS: FIRE window replayed after the resume waypoint with "
                "laser_enable/laser_on at 0 throughout")


# ---------------------------------------------------------------- dead man

@test("kernel.deadman-close", title="The final close of a running pulse device halts the engine and safes the head",
      subsystem="kernel", kind="auto", hardware="takeover", always=True, est_min=1,
      covers=_KERNEL_COVERS,
      requires=["kernel.k1-k2"],
      description="The kernel's final close of the pulse device, twice. At idle, with no run: the "
                  "latch unlocked (no run feeds the watchdog, so HV stays off, and FIRE is driven "
                  "low), the button latch read set after the unlock, then the device closed with "
                  "nothing else relocking it: the latch reads locked before any other lock. Running: "
                  "a pulse device closed while locked and running halts the engine at once, locks "
                  "the laser latch, and puts the head in its safe state - lens driver disabled at "
                  "low current, measure laser and UV LED off. The measuring laser and the UV LED are "
                  "lit and the lens driver enabled at run current right before the close, so the "
                  "safe state has all of it to undo; both emitters go off and the current back to "
                  "low on every way out. The same switch takes the thermal driver to its safe state: "
                  "the heater (driven at full duty for well under a second before the close, which "
                  "its PWM timer holds as a steady high pin) and the TEC go off, duty and pins, with "
                  "no lease lapse of either, as read in sysfs and in the GPIO debug listing; both are "
                  "put back as found. Motors locked; duty zero.")
def deadman_close(ctx):
    ev = ctx.evidence
    found = {"heater_pwm": rd("thermal/heater_pwm"), "tec_on": rd("thermal/tec_on")}
    try:
        deadman_close_body(ctx, ev)
    finally:
        # The baseline does not own the head's emitters: the test's own
        # restore is the only one.
        for attr, val in (("head/measure_laser", 0), ("head/uv_led", 0), ("head/z_current", 1)):
            try:
                wr(attr, val)
            except OSError as e:
                ctx.log("the head's %s could not be put back to %s: %s", attr, val, e)
        wr("thermal/heater_pwm", found["heater_pwm"])
        wr("thermal/tec_on", found["tec_on"])


def idle_close(ctx, ev):
    """The final close at idle relocks the latch by itself: unlocked with no
    run (nothing feeds the watchdog, so HV stays off, and the unlock drives
    FIRE low), the button latch read after the unlock, the device closed with
    no sysfs lock before it, and the latch read before the exit's own lock."""
    check_hv_off(ctx)
    wr("cnc/motor_lock", 15)
    wr("cnc/laser_latch", 1)
    with PulseDevice(ctx) as dev:
        wr("cnc/laser_latch_cooling", 0)        # the takeover's final close set both holders
        wr("cnc/laser_latch", 0)
        unlocked = hw.sysfs_int("cnc/interlock_circuit")
        bl = hw.sysfs_read("cnc/button_latch")
        ev["idle_close"] = {"unlocked": unlocked, "button_latch": bl}
        if bl != "1":
            relock_fail("idle close", "the button latch reads %s right after the unlock" % bl)
        dev.drop()                              # the final close, nothing else locking
        closed = hw.sysfs_int("cnc/interlock_circuit")
    ev["idle_close"]["closed"] = closed
    ctx.log("idle close: interlock_circuit %s unlocked, %s after the close", unlocked, closed)
    why = idle_close_why(unlocked, closed)
    ctx.check(why is None, "idle close: %s", why)
    ctx.log("idle close PASS: the final close locked the latch it found unlocked")


def deadman_close_body(ctx, ev):
    with ctx.takeover():
        idle_close(ctx, ev)
        stream = POWER0 + PAD * (6 * TICK_HZ)
        snap(ctx, "deadman pre")
        wr("cnc/motor_lock", 15)
        wr("cnc/laser_latch", 1)
        wr("cnc/ramp_rate", 125000)
        wr("cnc/step_freq", TICK_HZ)
        wr("head/z_current", 1)
        wr("head/z_enable", 0)
        wr("head/measure_laser", DEADMAN_MEASURE)
        wr("head/uv_led", DEADMAN_UV)
        ctx.check(rd("head/z_enable") == "0", "the lens driver did not enable (z_enable %s)", rd("head/z_enable"))
        with PulseDevice(ctx) as dev:
            dev.rewind()
            wr("cnc/enable", 1)
            ctx.sleep(0.5)
            dev.write(stream)
            wr("cnc/run", 1)
            ctx.sleep(1.0)
            st = rd("cnc/state")
            ctx.check(st == "running", "expected running before the close, got %s", st)
            wr("thermal/heater_pwm", DEADMAN_HEATER_DUTY)
            wr("thermal/tec_on", 1)
            ctx.sleep(0.05)                     # a few PWM periods: the timer has set the pin
            thermal_before = thermal_read()
            wr("head/z_current", 0)             # run current, from here to the close only
            before = {"z_enable": rd("head/z_enable"), "z_current": rd("head/z_current"),
                      "measure_laser": rd("head/measure_laser"), "uv_led": rd("head/uv_led")}
            t0 = time.monotonic()
            dev.drop()
            while time.monotonic() - t0 < 5:
                if rd("cnc/state") != "running":
                    break
            dt = time.monotonic() - t0
        ev["head_before"] = before
        thermal_after = thermal_read()
        ev["thermal"] = {"before": thermal_before, "after": thermal_after}
        state = rd("cnc/state")
        after = {"z_enable": rd("head/z_enable"), "z_current": rd("head/z_current"),
                 "measure_laser": rd("head/measure_laser"), "uv_led": rd("head/uv_led")}
        latch = hw.sysfs_int("cnc/interlock_circuit")
        ev["close"] = {"halt_s": round(dt, 4), "state": state, "faults": rd("cnc/faults"),
                       "interlock_circuit": latch}
        ev["head_after"] = after
        snap(ctx, "deadman post")
        ctx.log("final close: state=%s after %.4f s; head before %s, after %s", state, dt, before, after)
        ctx.check(state == "idle", "the engine did not halt on the final close (state %s)", state)
        ctx.check(dt < 1.0, "the halt took %.3f s", dt)
        ctx.check(latch is not None and latch & LATCH_BIT, "the latch is not locked after the close")
        why = deadman_head_why(before, after)
        ctx.check(why is None, "%s", why)
        why = deadman_thermal_why(thermal_before, thermal_after)
        ctx.log("thermal: before %s, after %s", thermal_before, thermal_after)
        ctx.check(why is None, "%s", why)
        wr("cnc/stop", 1)
        ctx.log("PASS: the final close halted the engine in %.3f s, latch locked, head safed, heater and "
                "TEC off", dt)


# ---------------------------------------------------------------- backtrack

def backward_why(started, end_state, played, budget, at_end, tail):
    """None when a backward run asked for `budget` bytes from byte `played`
    ran (the state read running), ended idle (declared streaming, a run
    that reaches the program's start ends as an underrun) and came back
    to within its deceleration tail past the waypoint without reaching the
    program's start; else why not. A run that never played leaves the
    counter at `played` and fails here."""
    if started != "running":
        return "the backward run never ran (state %s)" % started
    if end_state != "idle":
        return "the backward run ended in %s%s" % (
            end_state, " (the program's start: its deceleration played past the %d-byte tail)" % tail
            if end_state == "underrun" else "")
    waypoint = played - budget
    if at_end is None or at_end < 1:
        return "the backward run reached the program's start (processed %s)" % at_end
    if at_end > waypoint + PLAYED_SKEW:
        return "the backward run did not play back to its waypoint (processed %d, waypoint %d)" % (at_end, waypoint)
    if at_end < waypoint - tail - PLAYED_SKEW:
        return ("the backward run played %d bytes past its waypoint, more than the %d-byte tail"
                % (waypoint - at_end, tail))
    return None


@test("kernel.backtrack-bounds", title="A backward run is bounded by the history the ring still holds",
      subsystem="kernel", kind="auto", hardware="takeover", always=True, est_min=2,
      covers=_KERNEL_COVERS,
      requires=["kernel.k1-k2"],
      description="A pause walks the program backward to put the beam back over ground the job "
                  "already cut, and the ring is what remembers that ground. cnc/max_backtrack is "
                  "the distance still available: what has played, less the tail the deceleration "
                  "plays past the waypoint, as the kernel counts it (1323 bytes at the print tick). "
                  "This drills the boundary on real hardware - the readback matches the bytes "
                  "played less that tail, a step beyond it is refused rather than quietly "
                  "shortened, and the run at the boundary, declared streaming so that one reaching "
                  "the program's start ends as an underrun, reads running, comes back to within the "
                  "tail past its waypoint without reaching the start, and ends idle; the drain after "
                  "it ends idle too. Motors locked, latch locked, duty zero: nothing moves and "
                  "nothing fires.")
def backtrack_bounds(ctx):
    ev = ctx.evidence
    tail = backtrack_tail(TICK_HZ, 125000)
    with ctx.takeover():
        stream = POWER0 + PAD * (6 * TICK_HZ)
        ctx.log("%d bytes = %.1f s of pads at %d Hz; decel tail %d bytes",
                len(stream), len(stream) / TICK_HZ, TICK_HZ, tail)
        snap(ctx, "pre")
        wr("cnc/motor_lock", 15)
        wr("cnc/laser_latch", 1)
        wr("cnc/ramp_rate", 125000)
        wr("cnc/step_freq", TICK_HZ)
        with PulseDevice(ctx) as dev:
            dev.rewind()
            wr("cnc/enable", 1)
            ctx.sleep(0.5)
            dev.write(stream)
            wr("cnc/run", 1)
            ctx.sleep(1.5)                       # well past the accel ramp
            wr("cnc/stop", 1)
            state = wait_state(ctx, "idle", 5, poll=0.01)
            ctx.check(state == "idle", "the controlled stop did not reach idle (state=%s)", state)

            played = rd_pos()[3]
            budget = int(rd("cnc/max_backtrack"))
            ev["played"] = played
            ev["max_backtrack"] = budget
            ev["tail"] = tail
            ctx.log("played %d bytes; max_backtrack %d", played, budget)
            ctx.check(budget > 0, "max_backtrack is %d after %d bytes played", budget, played)
            # The two numbers come from different SDMA registers (the byte
            # counter and the ring head), so allow a few bytes of skew - but
            # not the whole gap, and not the whole played span.
            ctx.check(abs(budget - (played - tail)) <= PLAYED_SKEW,
                      "max_backtrack %d is not the %d bytes played less the %d-byte decel tail",
                      budget, played, tail)

            # One step past the boundary: refused, and the device stays idle.
            refused = None
            try:
                wr("cnc/resume", -(budget + 1))
            except OSError as e:
                refused = e.errno
            ev["over_long_errno"] = refused
            ctx.check(refused == errno.EPERM,
                      "a backtrack one step past the boundary was not refused with EPERM (%s)",
                      refused)
            state = rd("cnc/state")
            ctx.check(state == "idle", "the refused backtrack left the device %s", state)

            # At the boundary, streaming: a run that reached the program's
            # start would end as an underrun, not as an idle that passes.
            try:
                wr("cnc/streaming", 1)
                wr("cnc/resume", -budget)
                started = wait_state(ctx, "running", 2, poll=0.005)
                hits, state = watch_laser_until_idle(ctx, 30)
            finally:
                wr("cnc/streaming", 0)
            at_end = rd_pos()[3]
            after = int(rd("cnc/max_backtrack"))
            ev["after"] = {"started": started, "state": state, "processed": at_end, "max_backtrack": after,
                           "faults": rd("cnc/faults"), "underruns": rd("cnc/underruns")}
            ctx.log("backtrack of %d done: state=%s processed %d, max_backtrack now %d",
                    budget, state, at_end, after)
            if state == "underrun":
                wr("cnc/stop", 1)                # acknowledged before the drain
            # Leave the program drained so the device ends where the other
            # drills expect it.
            wr("cnc/resume", 0)
            wait_state(ctx, "running", 2, poll=0.005)
            drained = wait_state(ctx, "idle", 30)
            ev["drained"] = drained
        ctx.check(not hits, "the laser asserted during the backward run: %s", hits[:10])
        why = backward_why(started, state, played, budget, at_end, tail)
        ctx.check(why is None, "%s", why)
        ctx.check(drained == "idle", "the drain after the backward run ended in %s", drained)
        ctx.check(ev["after"]["faults"] == "0", "faults=%s after the backward run",
                  ev["after"]["faults"])
        ctx.log("PASS: max_backtrack tracks the played history less the tail, the boundary is enforced, "
                "and the run at it plays back to its waypoint and ends idle")


# ---------------------------------------------------------------- K3

def _k3_phase(ctx):
    """K3: the latch unlocked 0.15 s into a run must not restore the FIRE
    drive while the run is in flight. cnc/run starts at the full step rate,
    so the unlock lands in the RUNNING state, whose term of the guard
    covers a ramp too. Runs inside the caller's takeover, behind the opened
    chain."""
    ev = ctx.evidence
    check_hv_off(ctx)
    stream = POWER0 + FIRE * (3 * TICK_HZ) + PAD * (TICK_HZ // 2)
    ctx.log("K3: %d bytes = %.1f s of FIRE bits; unlock at t=+0.15 s, the run in flight",
            len(stream), len(stream) / TICK_HZ)
    snap(ctx, "K3 pre")
    wr("cnc/motor_lock", 15)
    wr("cnc/laser_latch", 1)
    wr("cnc/step_freq", TICK_HZ)
    try:
        with PulseDevice(ctx) as dev:
            dev.rewind()
            wr("cnc/enable", 1)
            ctx.sleep(0.5)
            dev.write(stream)
            wr("cnc/run", 1)
            time.sleep(0.15)                # the run in flight
            unlock_behind_chain(ctx, "K3")
            ilk = rd("cnc/interlock_circuit")
            ctx.log("K3: latch UNLOCKED mid-run; interlock=%s (bit 3 should read 0)", ilk)
            hits, state = watch_laser_until_idle(ctx, 20, guard="K3")
            snap(ctx, "K3 post")
            ev["k3"] = {"interlock_after_unlock": ilk, "hits": hits[:10], "end_state": state,
                        "laser_on_sampled": rd("cnc/laser_on_sampled"),
                        "underruns": rd("cnc/underruns"), "faults": rd("cnc/faults")}
    finally:
        wr("cnc/laser_latch", 1)
    ctx.check((int(ilk) & LATCH_BIT) == 0, "K3: the unlock did not drive the latch pin (interlock=%s)", ilk)
    ctx.check(not hits, "K3: FIRE drive re-armed by a mid-run unlock: %s", hits[:10])
    ctx.log("K3 PASS: laser_enable stayed 0 for the entire run after the mid-run unlock")


# ---------------------------------------------------------------- FIRE A/B/U

def _fire_stream():
    return (POWER0 +                    # duty zero before any FIRE bit
            PAD * TICK_HZ +             # 1 s baseline
            FIRE * (2 * TICK_HZ) +      # 2.000 s FIRE window (bounded by pads)
            PAD * TICK_HZ +             # 1 s gap
            FIRE * (2 * TICK_HZ))       # 2.000 s FIRE window ending AT end-of-data


SAMPLE_POLL_S = 0.002


def _fire_phase(ctx, mode):
    """One phase of fire_test.py; returns the evidence dict."""
    unlock = mode in ("B", "U")
    underrun_mode = mode == "U"
    stream = _fire_stream()
    ctx.log("fire %s: stream %d bytes = %.3f s", mode, len(stream), len(stream) / TICK_HZ)
    if unlock:
        check_hv_off(ctx)
    snap(ctx, "fire %s pre" % mode)
    wr("cnc/motor_lock", 15)
    wr("cnc/step_freq", TICK_HZ)
    wr("cnc/laser_latch", 1)
    underruns_before = int(rd("cnc/underruns"))
    mid = None
    tail = None
    with PulseDevice(ctx) as dev:
        dev.rewind()
        wr("cnc/enable", 1)
        ctx.sleep(0.5)
        dev.write(stream)
        pos_before = rd_pos()
        if underrun_mode:
            wr("cnc/streaming", 1)      # end-of-data mid-run = true underrun
            ctx.log("fire U: streaming=1, the terminal end-of-data will be a TRUE UNDERRUN")
        try:
            if unlock:
                unlock_behind_chain(ctx, mode)
                ctx.log("fire %s: latch UNLOCKED for this run, behind the opened chain", mode)
            wr("cnc/run", 1)
            t0 = time.monotonic()
            state = ""
            samples = []
            gaps = SampleGaps(ctx, mode) if unlock else None
            while time.monotonic() - t0 < 20:
                ctx.checkpoint()
                if unlock:
                    gaps.tick()
                ilk = run_guard(mode) if unlock else None
                state = rd("cnc/state")
                dt = time.monotonic() - t0
                en, on, ons = rd("cnc/laser_enable"), rd("cnc/laser_on"), rd("cnc/laser_on_sampled")
                samples.append((round(dt, 3), state, en, on, ons))
                if mid is None and 1.5 < dt < 3.0:
                    mid = {"t": round(dt, 2), "laser_enable": en, "laser_on": on,
                           "laser_on_sampled": ons,
                           "interlock": ilk if ilk is not None else rd("cnc/interlock_circuit")}
                    ctx.log("fire %s mid (inside FIRE window): laser_enable=%s laser_on=%s "
                            "laser_on_sampled=%s interlock=%s", mode, en, on, ons, mid["interlock"])
                if state != "running":
                    break
                time.sleep(SAMPLE_POLL_S)
            end_dt = time.monotonic() - t0
            tail = {"state": state, "after_s": round(end_dt, 2), "laser_enable": rd("cnc/laser_enable"),
                    "laser_on": rd("cnc/laser_on")}
            ctx.log("fire %s done: state=%s after %.1f s (laser_enable=%s)", mode, state, end_dt,
                    tail["laser_enable"])
            if underrun_mode:
                if state == "underrun":
                    ctx.log("fire U: underrun state reached as EXPECTED; acking via stop")
                else:
                    ctx.log("fire U: WARNING expected underrun state, got %s", state)
                wr("cnc/stop", 1)
                wr("cnc/streaming", 0)
                tail["acked_state"] = rd("cnc/state")
                ctx.log("fire U: acked: state=%s", tail["acked_state"])
        finally:
            wr("cnc/laser_latch", 1)
            if underrun_mode:
                try:
                    wr("cnc/streaming", 0)
                except OSError:
                    pass
    pos_after = rd_pos()
    snap(ctx, "fire %s post" % mode)
    ev = {"mid": mid, "tail": tail, "moved": pos_before[:3] != pos_after[:3], "samples": len(samples),
          "underruns_before": underruns_before, "underruns_after": int(rd("cnc/underruns")),
          "faults": rd("cnc/faults"),
          "any_laser_on": any(s[3] != "0" or s[4] != "0" for s in samples),
          "any_fire_driven": any(s[2] != "0" for s in samples)}
    ctx.log("fire %s: moved=%s underruns %d->%d faults=%s", mode, ev["moved"],
            ev["underruns_before"], ev["underruns_after"], ev["faults"])
    return ev


# ---------------------------------------------------------------- R: the retrace, unlocked

RETRACE_BYTES = TICK_HZ // 2            # half a second of FIRE bytes played backward


def retrace_why(started, samples, end_state, played, back, at_end, tail):
    """None when the backward run over FIRE bytes, with the latch unlocked,
    ran, kept FIRE and LASER_ON low in every sample, ended idle, and came
    back to its waypoint within the deceleration tail; else why not.
    samples: (t, laser_enable, laser_on) read through the backward run."""
    if started != "running":
        return "the backward run never ran (state %s)" % started
    if not samples:
        return "nothing was sampled through the backward run"
    lit = [s for s in samples if s[1] != "0" or s[2] != "0"]
    if lit:
        return "FIRE or LASER_ON driven during the backward run over FIRE bytes: %s" % lit[:5]
    if end_state != "idle":
        return "the backward run ended in %s" % end_state
    waypoint = played - back
    if at_end is None or at_end > waypoint + PLAYED_SKEW or at_end < waypoint - tail - PLAYED_SKEW:
        return "the backward run did not play back to its waypoint (processed %s, waypoint %d, tail %d)" % (
            at_end, waypoint, tail)
    return None


def _retrace_phase(ctx):
    """R: a backward run over FIRE bytes with the latch unlocked. The run
    start restores the FIRE drive (no holder holds), and the script masks
    the laser bits of every byte it plays backward, so FIRE stays low
    through the retrace. The forward run before it drives FIRE at duty 0
    behind the opened chain; the drain after it plays with the latch
    locked."""
    ev = ctx.evidence
    check_hv_off(ctx)
    tail = backtrack_tail(TICK_HZ, 125000)
    stream = POWER0 + FIRE * (2 * TICK_HZ) + PAD * TICK_HZ
    ctx.log("R: %d bytes = %.1f s (2 s of FIRE bits, then pads); stop 1 s in, then %d bytes backward, "
            "latch UNLOCKED", len(stream), len(stream) / TICK_HZ, RETRACE_BYTES)
    snap(ctx, "R pre")
    wr("cnc/motor_lock", 15)
    wr("cnc/step_freq", TICK_HZ)
    wr("cnc/ramp_rate", 125000)
    wr("cnc/laser_latch", 1)
    samples = []
    started = end_state = None
    played = back = at_end = None
    with PulseDevice(ctx) as dev:
        dev.rewind()
        wr("cnc/enable", 1)
        ctx.sleep(0.5)
        dev.write(stream)
        try:
            unlock_behind_chain(ctx, "R")
            wr("cnc/run", 1)
            t0 = time.monotonic()
            gaps = SampleGaps(ctx, "R forward")
            while time.monotonic() - t0 < 1.0:       # inside the FIRE window
                ctx.checkpoint()
                gaps.tick()
                run_guard("R")
                time.sleep(SAMPLE_POLL_S)
            wr("cnc/stop", 1)
            state = wait_state(ctx, "idle", 5, poll=0.002)
            ctx.check(state == "idle", "R: the controlled stop did not reach idle (state=%s)", state)
            played = rd_pos()[3]
            budget = int(rd("cnc/max_backtrack"))
            back = min(budget, RETRACE_BYTES)
            ev["R_stop"] = {"played": played, "max_backtrack": budget, "back": back}
            ctx.check(back > 0 and played - back > len(POWER0),
                      "R: no FIRE bytes to play backward (played %d, max_backtrack %d)", played, budget)
            run_guard("R")
            wr("cnc/resume", -back)
            t0 = time.monotonic()
            gaps = SampleGaps(ctx, "R back")
            while time.monotonic() - t0 < 5:
                ctx.checkpoint()
                gaps.tick()
                run_guard("R")
                en, on = rd("cnc/laser_enable"), rd("cnc/laser_on")
                state = rd("cnc/state")
                if state == "running":
                    started = "running"
                    samples.append((round(time.monotonic() - t0, 4), en, on))
                elif started is not None or time.monotonic() - t0 > 0.5:
                    break
            end_state = rd("cnc/state")
            at_end = rd_pos()[3]
        finally:
            wr("cnc/laser_latch", 1)
        # the rest of the program, latch locked: FIRE stays high impedance
        wr("cnc/resume", 0)
        wait_state(ctx, "running", 2, poll=0.005)
        drained = wait_state(ctx, "idle", 10)
    snap(ctx, "R post")
    ev["R"] = {"started": started, "samples": len(samples), "end_state": end_state, "processed": at_end,
               "drained": drained, "laser_on_sampled": rd("cnc/laser_on_sampled"), "faults": rd("cnc/faults")}
    ctx.log("R: %d samples through the backward run, end state %s, processed %s", len(samples), end_state, at_end)
    why = retrace_why(started, samples, end_state, played, back, at_end, tail)
    ctx.check(why is None, "R: %s", why)
    ctx.check(drained == "idle", "R: the drain ended in %s", drained)
    ctx.log("R PASS: FIRE low through a backward run over FIRE bytes with the latch unlocked")


def _lead_phase(ctx, tick):
    """L: a resume's laser-off lead with the latch unlocked behind the
    opened chain: a resume with a 1000-byte lead over a stream of FIRE
    bits keeps the FIRE line low through the lead and drives it from the
    waypoint byte on, LASER_ON off. Runs inside the caller's takeover."""
    ev = ctx.evidence
    check_hv_off(ctx)
    lead = tick                                 # 1.0 s of FIRE bits held off
    lead_s = lead / tick
    stream = POWER0 + FIRE * (2 * tick) + PAD * (tick // 5)
    ctx.log("L: %d bytes = %.1f s (%.1f s of FIRE bits then pads); lead %d bytes = %.1f s; "
            "latch UNLOCKED behind the opened chain", len(stream), len(stream) / tick,
            2 * tick / tick, lead, lead_s)
    wr("cnc/motor_lock", 15)
    wr("cnc/laser_latch", 1)
    wr("cnc/step_freq", tick)
    snap(ctx, "L pre")
    samples = []
    state = ""
    with PulseDevice(ctx) as dev:
        dev.rewind()
        wr("cnc/enable", 1)
        ctx.sleep(0.5)
        dev.write(stream)
        try:
            unlock_behind_chain(ctx, "L")
            t0 = time.monotonic()
            wr("cnc/resume", lead)
            n = 0
            gaps = SampleGaps(ctx, "L")
            while time.monotonic() - t0 < 10:
                gaps.tick()
                run_guard("L")
                en, on = rd("cnc/laser_enable"), rd("cnc/laser_on")
                samples.append((round(time.monotonic() - t0, 4), en, on))
                state = rd("cnc/state")
                if state != "running" and time.monotonic() - t0 > 0.2:
                    break
                n += 1
                if n % 200 == 0:
                    ctx.checkpoint()
        finally:
            wr("cnc/laser_latch", 1)
    snap(ctx, "L post")
    driven = [s for s in samples if s[1] != "0"]
    first = driven[0][0] if driven else None
    last = driven[-1][0] if driven else None
    inside_lead = [s for s in driven if s[0] < lead_s - 0.02]
    ev["L"] = {"samples": len(samples), "driven_samples": len(driven),
               "first_driven_s": first, "last_driven_s": last, "end_state": state,
               "any_laser_on": any(s[2] != "0" for s in samples),
               "inside_lead": inside_lead[:5], "laser_on_sampled": rd("cnc/laser_on_sampled"),
               "faults": rd("cnc/faults")}
    ctx.log("L: %d samples, FIRE driven in %d, first at %s s, last at %s s, end state %s",
            len(samples), len(driven), first, last, state)
    ctx.check(state == "idle", "L: ended in %s", state)
    ctx.check(not inside_lead, "L: FIRE driven inside the lead: %s", inside_lead[:5])
    ctx.check(driven, "L: FIRE never driven after the lead (the inhibit never cleared)")
    ctx.check(first is not None and first < lead_s + 0.3,
              "L: FIRE first driven at %s s; the lead ends at %.1f s", first, lead_s)
    ctx.check(not ev["L"]["any_laser_on"],
              "L: LASER_ON active behind the opened chain")
    ctx.check(rd("cnc/laser_enable") == "0", "L: FIRE still driven after end-of-data")
    ctx.log("L PASS: FIRE low through the %.1f s lead, driven from the waypoint on, LASER_ON off",
            lead_s)


CHAIN_STEP = ("The remote-interlock loop is opened for the drill and closed again at its end: by the "
              "bench actuator's interlock channel, or by you when the page asks (unplug the Pro's "
              "interlock plug, or pull the jumper at J8 on a Basic/Plus). The phases that unlock the "
              "latch run only while the kernel reads the loop open and the interlock latch set, which "
              "blocks LASER_ON whatever the button does.")
HANDS_STEP = ("Stay at the machine with your hands off its button while the drill runs: the latch is "
              "unlocked with FIRE driven at zero duty under the HV the drill's own runs raise, and a "
              "press clears the button latch. The drill reads it after every unlock and through every "
              "run, and a press relocks the latch and fails the test.")
START_STEP = ("The drill starts only while no run feeds the charge-pump watchdog (true at idle; if it "
              "is refused, let the machine go idle and start it again).")


@test("kernel.fire-line", title="FIRE line: latch locked, unlocked behind the opened chain, true underrun, "
                               "a backward run, and a mid-run unlock",
      subsystem="kernel", kind="operator", hardware="takeover", always=True, est_min=5,
      covers=_KERNEL_COVERS,
      requires=["kernel.k1-k2"], precheck=hv_off, actions=["interlock"], hands=["present"],
      steps=[CHAIN_STEP, HANDS_STEP, START_STEP],
      description="Five phases behind one takeover of the pulse device, all zero duty, with the "
                  "safety chain held open at the remote-interlock loop (the kernel holds the "
                  "interlock latch set, which blocks LASER_ON in hardware). Each unlock is refused "
                  "unless the chain reads open, and the button latch is read set right after it and "
                  "in every sample of the run, or the latch relocks and the test fails. A: latch "
                  "locked, 40 000 streamed FIRE bits, nothing on the FIRE/LASER_ON nets. B: latch "
                  "unlocked, the FIRE line is driven mid-window, LASER_ON stays off and the button "
                  "latch stays set, FIRE clear at end-of-data. U: streaming declared, the terminal "
                  "end-of-data is a true underrun, the backstop drops FIRE and stop acks it. R: a "
                  "controlled stop inside a FIRE window, then a backward run over the FIRE bytes "
                  "with the latch unlocked: the run reads running, FIRE and LASER_ON stay low in "
                  "every sample, and it plays back to its waypoint. K3: the latch unlocked 0.15 s "
                  "into a run drives the latch pin but never restores the FIRE drive to the run in "
                  "flight - laser_enable stays 0 for the whole run. The drill ends with the latch "
                  "locked, a halt and a stop, the 40 V rail up, and the loop closed again.")
def fire_line(ctx):
    ev = ctx.evidence
    require_hv_off(ctx)
    closed = False
    with ctx.takeover():
        try:
            open_chain(ctx)
            a = _fire_phase(ctx, "A")
            ev["A"] = a
            ctx.check(a["mid"] is not None, "A: no mid-window sample")
            ctx.check(not a["any_fire_driven"], "A: FIRE line driven with the latch locked")
            ctx.check(not a["any_laser_on"], "A: LASER_ON seen with the latch locked")
            ctx.check(a["tail"]["state"] == "idle", "A: ended in %s", a["tail"]["state"])
            ctx.check(not a["moved"], "A: position moved with motors locked")
            ctx.log("fire A PASS: latch locked, no FIRE drive, no LASER_ON")

            b = _fire_phase(ctx, "B")
            ev["B"] = b
            ctx.check(b["mid"] is not None, "B: no mid-window sample")
            ctx.check(b["mid"]["laser_enable"] != "0",
                      "B: FIRE line not driven mid-window with the latch unlocked (%s)", b["mid"])
            ctx.check(not b["any_laser_on"], "B: LASER_ON active behind the opened chain")
            ctx.check(b["tail"]["state"] == "idle", "B: ended in %s", b["tail"]["state"])
            ctx.check(b["tail"]["laser_enable"] == "0", "B: FIRE still driven after end-of-data")
            ctx.check(b["underruns_after"] == b["underruns_before"], "B: underrun counted on a normal completion")
            ctx.log("fire B PASS: FIRE driven mid-window, LASER_ON off, the button latch set throughout, "
                    "FIRE clear at end-of-data")

            u = _fire_phase(ctx, "U")
            ev["U"] = u
            ctx.check(u["tail"]["state"] == "underrun", "U: expected the underrun state, got %s", u["tail"]["state"])
            ctx.check(u["tail"]["laser_enable"] == "0", "U: FIRE still driven after the underrun")
            ctx.check(not u["any_laser_on"], "U: LASER_ON active behind the opened chain")
            ctx.check(u["tail"].get("acked_state") == "idle", "U: stop did not ack the underrun (state %s)",
                      u["tail"].get("acked_state"))
            ctx.check(u["underruns_after"] == u["underruns_before"] + 1,
                      "U: underrun counter %d -> %d", u["underruns_before"], u["underruns_after"])
            ctx.log("fire U PASS: true underrun, backstop dropped FIRE, stop acked")

            _retrace_phase(ctx)
            _k3_phase(ctx)
        finally:
            safe_end(ctx)
            closed = close_chain(ctx)
            ctx.log("safe state restored: state=%s latch=LOCKED; the loop %s", rd("cnc/state"),
                    "closed" if closed else "NOT closed")
    ctx.check(closed, "the remote-interlock loop does not read closed again after the drill")


# ---------------------------------------------------------------- resume lead

@test("kernel.resume-lead", title="A resume's laser-off lead, and an end-of-data before the waypoint",
      subsystem="kernel", kind="operator", hardware="takeover", est_min=3,
      covers=_KERNEL_COVERS,
      requires=["kernel.k1-k2"], precheck=hv_off, actions=["interlock"], hands=["present"],
      steps=[CHAIN_STEP, HANDS_STEP, START_STEP],
      description="Two phases behind one takeover, zero duty, motors locked, a 1 kHz tick, with "
                  "the safety chain held open at the remote-interlock loop. E: a resume whose lead "
                  "is longer than the data (the ring drains before the waypoint) ends the run at "
                  "end-of-data within a few ticks, not one re-notify period (255 ticks) later: the "
                  "script publishes end-of-data in its mailbox and the host decodes on that, not on "
                  "the waypoint it was still waiting for. L: with the latch unlocked behind the "
                  "opened chain (refused unless it reads open, the button latch read set after the "
                  "unlock and in every sample), a resume with a 1000-byte lead over a stream of FIRE "
                  "bits keeps the FIRE line low through the lead and drives it from the waypoint "
                  "byte on. The script masks the laser bits itself, so no host write reaches the "
                  "GPIO data register while the script runs; LASER_ON stays off. The drill ends with "
                  "the latch locked, a halt and a stop, the 40 V rail up, and the loop closed again.")
def resume_lead(ctx):
    ev = ctx.evidence
    require_hv_off(ctx)
    tick = 1000
    closed = False
    with ctx.takeover():
        try:
            open_chain(ctx)
            # ---- E: end-of-data before the waypoint
            stream = POWER0 + PAD * (tick // 2)         # 0.5 s of pads
            expected = len(stream) / tick
            ctx.log("E: %d bytes = %.1f s of pads at %d Hz; resume with a %d-byte lead (never reached)",
                    len(stream), expected, tick, 10 * tick)
            wr("cnc/motor_lock", 15)
            wr("cnc/laser_latch", 1)
            wr("cnc/ramp_rate", 125000)
            wr("cnc/step_freq", tick)
            snap(ctx, "E pre")
            with PulseDevice(ctx) as dev:
                dev.rewind()
                wr("cnc/enable", 1)
                ctx.sleep(0.5)
                dev.write(stream)
                underruns_before = int(rd("cnc/underruns"))
                t0 = time.monotonic()
                wr("cnc/resume", 10 * tick)
                state = wait_state(ctx, "running", 2, poll=0.002)
                ctx.check(state == "running", "E: the resume did not start (state=%s)", state)
                state = wait_state(ctx, "idle", 5, poll=0.002)
                dt = time.monotonic() - t0
                ev["E"] = {"run_to_idle_s": round(dt, 3), "data_s": expected, "state": state,
                           "underruns_before": underruns_before,
                           "underruns_after": int(rd("cnc/underruns")), "faults": rd("cnc/faults")}
                ctx.log("E: %s after %.3f s (the data is %.3f s; a lost end-of-data would add %.3f s)",
                        state, dt, expected, 255.0 / tick)
            ctx.check(state == "idle", "E: state %s after the data ran out", state)
            ctx.check(dt < expected + 0.15,
                      "E: the run ended %.3f s after its data: end-of-data decoded late", dt - expected)
            ctx.check(ev["E"]["underruns_after"] == underruns_before,
                      "E: an underrun was counted on a normal completion")
            ctx.check(ev["E"]["faults"] == "0", "E: faults=%s", ev["E"]["faults"])
            ctx.log("E PASS: end-of-data before the waypoint ended the run on time")

            _lead_phase(ctx, tick)
        finally:
            try:
                wr("cnc/ramp_rate", 125000)
            except OSError:
                pass
            safe_end(ctx)
            closed = close_chain(ctx)
            ctx.log("safe state restored: state=%s latch=LOCKED; the loop %s", rd("cnc/state"),
                    "closed" if closed else "NOT closed")
    ctx.check(closed, "the remote-interlock loop does not read closed again after the drill")


# ---------------------------------------------------------------- PIC and the SoC's load

PIC_SETTLE_PARAM = "/sys/module/glowforge/parameters/pic_settle_us"


def _param_read(path):
    try:
        with open(path) as f:
            return f.read().strip()
    except OSError:
        return None


def _param_write(path, value):
    with open(path, "w") as f:
        f.write("%s\n" % value)


def _level_stats(xs):
    s = sorted(xs)
    n = len(s)
    return {"n": n, "mean": round(sum(s) / float(n), 2), "med": s[n // 2],
            "iqr": s[(3 * n) // 4] - s[n // 4], "min": s[0], "max": s[-1]}


def _spin(sec):
    t0 = time.perf_counter()
    x = 1.0
    while time.perf_counter() - t0 < sec:
        x = x * 1.000001 + 0.5
    return x


@test("kernel.pic-soc-load", title="A PIC reading does not depend on what the reader was doing",
      subsystem="kernel", kind="auto", est_min=1,
      covers=_KERNEL_COVERS + [("forgectrl", "src/diag.c")],
      description="The PIC converts its sensors in a free-running loop and a read returns the "
                  "last conversion of that channel; the count depends on the SoC's load when the "
                  "conversion was made (the PIC converts against its own supply, the dividers hang "
                  "on the board's reference), about 6 counts between an idle and a busy CPU on the "
                  "coolant thermistors. The module keeps the CPU busy for pic_settle_us before every "
                  "transaction, longer than one PIC loop, so the value read was converted under the "
                  "same load whoever reads. The drill reads a coolant thermistor 200 times each after "
                  "3 ms of sleep and after 3 ms of spinning, with the settle off (the control, "
                  "reported) and on (the claim): settled, the two agree.")
def pic_soc_load(ctx):
    ev = ctx.evidence
    saved = _param_read(PIC_SETTLE_PARAM)
    ctx.check(saved is not None, "the module has no pic_settle_us parameter (%s)", PIC_SETTLE_PARAM)
    ev["pic_settle_us_before"] = saved
    fd = os.open(hw.sysfs_root() + "pic/water_temp_1", os.O_RDONLY)

    def reads(regime, n=200):
        xs = []
        for i in range(n):
            if regime == "idle":
                time.sleep(0.003)
            else:
                _spin(0.003)
            xs.append(int(os.pread(fd, 32, 0).strip()))
            if i % 100 == 99:
                ctx.checkpoint()
        return _level_stats(xs)

    try:
        _param_write(PIC_SETTLE_PARAM, 0)
        ctx.sleep(0.05)
        control = {"idle": reads("idle"), "busy": reads("busy")}
        _param_write(PIC_SETTLE_PARAM, 500)
        ctx.sleep(0.05)
        settled = {"idle": reads("idle"), "busy": reads("busy")}
    finally:
        os.close(fd)
        _param_write(PIC_SETTLE_PARAM, saved)          # handed back as found, a found 0 included
    ev["control"] = control
    ev["settled"] = settled
    ev["pic_settle_us_after"] = _param_read(PIC_SETTLE_PARAM)
    for name, r in (("control (settle off)", control), ("settled (500 us)", settled)):
        ctx.log("%s: after sleep median %d (iqr %d, %d..%d); after spin median %d (iqr %d, %d..%d)",
                name, r["idle"]["med"], r["idle"]["iqr"], r["idle"]["min"], r["idle"]["max"],
                r["busy"]["med"], r["busy"]["iqr"], r["busy"]["min"], r["busy"]["max"])
    ev["control_split"] = control["busy"]["med"] - control["idle"]["med"]
    ev["settled_split"] = settled["busy"]["med"] - settled["idle"]["med"]
    ctx.log("split busy minus idle: control %+d, settled %+d", ev["control_split"], ev["settled_split"])
    ctx.check(abs(ev["settled_split"]) <= 2,
              "settled reads still split by %+d counts between an idle and a busy reader", ev["settled_split"])
    ctx.check(settled["idle"]["iqr"] <= 4 and settled["busy"]["iqr"] <= 4,
              "settled reads spread %d / %d counts (interquartile)", settled["idle"]["iqr"], settled["busy"]["iqr"])
    # The kernel's spin is its own load level, a couple of counts under a
    # Python spin, so a settled reader lands above the idle regime without
    # reaching the busy one. The failure this guards is a settle that
    # overshoots and lets the conversion fall back to idle, which reads as
    # no move at all; the split collapsing above is the primary proof.
    #
    # The bound is a third of the control split, not half. Measured over
    # eleven runs on this bench the move is 3 counts (once 2, once 4)
    # against a control split of 6 (once 7), so a half-split bound sits
    # exactly on the median and decides on one count of noise: two of those
    # eleven runs failed with the machine reading identically to the nine
    # that passed. A third keeps the guard against no move at all and
    # leaves a count of margin either way.
    moved = settled["idle"]["med"] - control["idle"]["med"]
    ev["settled_idle_moved"] = moved
    ctx.check(ev["control_split"] <= 2 or moved * 3 >= ev["control_split"],
              "a settled idle reader moved %+d counts off the idle regime, the control split is %+d: "
              "the settle did not land the conversion under load", moved, ev["control_split"])
    ctx.check(ev["pic_settle_us_after"] == saved, "pic_settle_us left at %s, found at %s",
              ev["pic_settle_us_after"], saved)
    ctx.log("PASS: settled, an idle reader and a busy reader read the same (split %+d); control split %+d",
            ev["settled_split"], ev["control_split"])
