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
zero before any FIRE bit, keeps motor_lock=15 (no axis moves), and
re-locks the latch on every exit path. K3 and fire B/U unlock the latch
for their run and therefore refuse to proceed unless the safety chain
holds HV off: the charge-pump watchdog dead and the pulse engine idle
(HV_ENABLE follows the pump; laser_pgood is the supply's power-good and
says nothing about HV).
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

# interlock_circuit bits (docs.forgefirm.org, the kernel module page): bit 3 = the driven latch line, set = locked.
LATCH_BIT = 1 << 3

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


def snap(ctx, tag):
    line = ("%s: state=%s laser_enable=%s laser_on=%s laser_on_sampled=%s interlock=%s"
            % (tag, rd("cnc/state"), rd("cnc/laser_enable"), rd("cnc/laser_on"),
               rd("cnc/laser_on_sampled"), rd("cnc/interlock_circuit")))
    ctx.log(line)
    return line


def wait_state(ctx, want, timeout, poll=0.05):
    t0 = time.time()
    while time.time() - t0 < timeout:
        ctx.checkpoint()
        s = rd("cnc/state")
        if s == want:
            return s
        time.sleep(poll)
    return rd("cnc/state")


def watch_laser_until_idle(ctx, timeout):
    """Tight-loop laser_enable/laser_on watch; returns (hits, end_state)."""
    hits = []
    t0 = time.time()
    state = "running"
    n = 0
    while time.time() - t0 < timeout:
        en = rd("cnc/laser_enable")
        on = rd("cnc/laser_on")
        if en != "0" or on != "0":
            hits.append((round(time.time() - t0, 4), en, on))
        state = rd("cnc/state")
        if state != "running":
            break
        n += 1
        if n % 200 == 0:
            ctx.checkpoint()
    return hits, state


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
    """Why the safety chain is NOT holding HV off right now, or None. The
    chain asserts HV_ENABLE only while a run feeds the charge-pump
    watchdog, so a dead watchdog and an idle pulse engine mean HV off.
    laser_pgood is the supply's power-good, high whenever the supply is
    healthy, and says nothing about HV, so it plays no part here."""
    alive = rd("cnc/charge_pump_alive")
    state = rd("cnc/state")
    if alive is None or state is None:
        return "cnc/charge_pump_alive or cnc/state unreadable"
    if alive != "0" or state != "idle":
        return ("charge_pump_alive=%s state=%s: the chain may be holding HV_ENABLE up" % (alive, state))
    return None


def hv_off():
    """Precheck for the drills that unlock the latch with a zero-duty
    stream: they run only while the chain holds HV off. At idle nothing
    feeds the watchdog, so this refuses a start only on a machine that is
    not idle the way it should be."""
    why = hv_off_reason()
    if why:
        return ("%s; this drill unlocks the latch with a zero-duty stream and needs HV off "
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
    t0 = time.time()
    why = hv_off_reason()
    while why is not None and time.time() - t0 < timeout_s:
        ctx.sleep(0.05)
        why = hv_off_reason()
    dt = round(time.time() - t0, 2)
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
    """The hard check right before an unlock (no prompt: forgectrl is down)."""
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
                  "a 2 s FIRE window with laser_enable/laser_on at 0 throughout. Motors locked; "
                  "duty zero.")
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
            t0 = time.time()
            wr("cnc/stop", 1)
            while time.time() - t0 < 5:
                if rd("cnc/state") != "running":
                    break
            dt = time.time() - t0
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
        stream = (POWER0 + PAD * TICK_HZ + step_sec + PAD * (TICK_HZ // 2)
                  + FIRE * (2 * TICK_HZ) + PAD * TICK_HZ)
        ctx.log("K2: %d bytes = %.1f s; latch stays LOCKED; waypoint +200; motor_lock=15",
                len(stream), len(stream) / TICK_HZ)
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
            ctx.log("K2: paused inside the pads; resuming with waypoint +200 (latch LOCKED)")
            wr("cnc/resume", 200)
            wait_state(ctx, "running", 2, poll=0.005)
            hits, state = watch_laser_until_idle(ctx, 20)
            pos_after = rd_pos()
            snap(ctx, "K2 post")
            ev["k2"] = {"hits": hits[:10], "end_state": state, "pos_before": pos_before,
                        "pos_after": pos_after, "laser_on_sampled": rd("cnc/laser_on_sampled"),
                        "underruns": rd("cnc/underruns"), "faults": rd("cnc/faults")}
            ctx.log("K2 done: state=%s pos before=%s after=%s", state, pos_before, pos_after)
        ctx.check(not hits, "K2: laser asserted with the latch locked: %s", hits[:10])
        ctx.check(pos_before[:3] == pos_after[:3],
                  "K2: position counters did not return to start (%s -> %s)", pos_before[:3], pos_after[:3])
        ctx.log("K2 PASS: FIRE window replayed after the resume waypoint with "
                "laser_enable/laser_on at 0 throughout")


# ---------------------------------------------------------------- dead man

@test("kernel.deadman-close", title="The final close of a running pulse device halts the engine and safes the head",
      subsystem="kernel", kind="auto", hardware="takeover", always=True, est_min=1,
      covers=_KERNEL_COVERS,
      requires=["kernel.k1-k2"],
      description="The kernel dead man's switch: a pulse device closed while locked and running "
                  "halts the engine at once, locks the laser latch, and puts the head in its safe "
                  "state - lens driver disabled at low current, measure laser and UV LED off. The "
                  "lens driver is enabled at low current before the close, so the safe state has "
                  "something to undo. Motors locked; duty zero.")
def deadman_close(ctx):
    ev = ctx.evidence
    with ctx.takeover():
        stream = POWER0 + PAD * (6 * TICK_HZ)
        snap(ctx, "deadman pre")
        wr("cnc/motor_lock", 15)
        wr("cnc/laser_latch", 1)
        wr("cnc/ramp_rate", 125000)
        wr("cnc/step_freq", TICK_HZ)
        wr("head/z_current", 1)
        wr("head/z_enable", 0)
        before = {"z_enable": rd("head/z_enable"), "z_current": rd("head/z_current")}
        ev["head_before"] = before
        ctx.check(before["z_enable"] == "0", "the lens driver did not enable (z_enable %s)", before["z_enable"])
        with PulseDevice(ctx) as dev:
            dev.rewind()
            wr("cnc/enable", 1)
            ctx.sleep(0.5)
            dev.write(stream)
            wr("cnc/run", 1)
            ctx.sleep(1.0)
            st = rd("cnc/state")
            ctx.check(st == "running", "expected running before the close, got %s", st)
            t0 = time.time()
            dev.drop()
            while time.time() - t0 < 5:
                if rd("cnc/state") != "running":
                    break
            dt = time.time() - t0
        state = rd("cnc/state")
        after = {"z_enable": rd("head/z_enable"), "z_current": rd("head/z_current"),
                 "measure_laser": rd("head/measure_laser"), "uv_led": rd("head/uv_led")}
        latch = hw.sysfs_int("cnc/interlock_circuit")
        ev["close"] = {"halt_s": round(dt, 4), "state": state, "faults": rd("cnc/faults"),
                       "interlock_circuit": latch}
        ev["head_after"] = after
        snap(ctx, "deadman post")
        ctx.log("final close: state=%s after %.4f s; head %s", state, dt, after)
        ctx.check(state == "idle", "the engine did not halt on the final close (state %s)", state)
        ctx.check(dt < 1.0, "the halt took %.3f s", dt)
        ctx.check(latch is not None and latch & (1 << 3), "the latch is not locked after the close")
        ctx.check(after["z_enable"] == "1" and after["z_current"] == "1",
                  "the head was not safed: z_enable %s z_current %s (want 1, 1: driver disabled, low current)",
                  after["z_enable"], after["z_current"])
        ctx.check(after["measure_laser"] == "0" and after["uv_led"] == "0",
                  "measure laser %s, UV LED %s after the close", after["measure_laser"], after["uv_led"])
        wr("cnc/stop", 1)
        ctx.log("PASS: the final close halted the engine in %.3f s, latch locked, head safed", dt)


# ---------------------------------------------------------------- backtrack

@test("kernel.backtrack-bounds", title="A backward run is bounded by the history the ring still holds",
      subsystem="kernel", kind="auto", hardware="takeover", always=True, est_min=2,
      covers=_KERNEL_COVERS,
      requires=["kernel.k1-k2"],
      description="A pause walks the program backward to put the beam back over ground the job "
                  "already cut, and the ring is what remembers that ground. cnc/max_backtrack is "
                  "the distance still available: what has played, less the tail the deceleration "
                  "spends. This drills the boundary on real hardware - the readback matches the "
                  "bytes played, a step beyond it is refused rather than quietly shortened, and "
                  "the run at the boundary plays out and returns to idle. Motors locked, latch "
                  "locked, duty zero: nothing moves and nothing fires.")
def backtrack_bounds(ctx):
    ev = ctx.evidence
    tail = TICK_HZ * TICK_HZ // (2 * 125000)     # v^2/2a: 400 steps at the print tick
    with ctx.takeover():
        stream = POWER0 + PAD * (6 * TICK_HZ)
        ctx.log("%d bytes = %.1f s of pads at %d Hz; decel tail %d steps",
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
            ctx.log("played %d bytes; max_backtrack %d", played, budget)
            ctx.check(budget > 0, "max_backtrack is %d after %d bytes played", budget, played)
            # The two numbers come from different SDMA registers (the byte
            # counter and the ring head), so allow a few bytes of skew - but
            # not the whole gap, and not the whole played span.
            ctx.check(abs(budget - (played - tail)) <= 8,
                      "max_backtrack %d is not the %d bytes played less the %d-step decel tail",
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

            # At the boundary: runs, decelerates inside genuine data, ends idle.
            wr("cnc/resume", -budget)
            wait_state(ctx, "running", 2, poll=0.005)
            hits, state = watch_laser_until_idle(ctx, 30)
            after = int(rd("cnc/max_backtrack"))
            ev["after"] = {"state": state, "max_backtrack": after,
                           "faults": rd("cnc/faults"), "underruns": rd("cnc/underruns")}
            ctx.log("backtrack of %d done: state=%s max_backtrack now %d", budget, state, after)
            # Leave the program drained so the device ends where the other
            # drills expect it.
            wr("cnc/resume", 0)
            wait_state(ctx, "running", 2, poll=0.005)
            wait_state(ctx, "idle", 30)
        ctx.check(not hits, "the laser asserted during the backward run: %s", hits[:10])
        ctx.check(state == "idle", "the backward run ended in %s", state)
        ctx.check(ev["after"]["faults"] == "0", "faults=%s after the backward run",
                  ev["after"]["faults"])
        ctx.log("PASS: max_backtrack tracks the played history, the boundary is enforced, "
                "and the run at it plays out clean")


# ---------------------------------------------------------------- K3

def _k3_phase(ctx):
    """K3: the latch unlocked during the accel ramp must not restore the FIRE
    drive while the run is in flight. Runs inside the caller's takeover."""
    ev = ctx.evidence
    check_hv_off(ctx)
    stream = POWER0 + FIRE * (3 * TICK_HZ) + PAD * (TICK_HZ // 2)
    ctx.log("K3: %d bytes = %.1f s of FIRE bits; ramp_rate 10000 Hz/s (~0.9 s accel "
            "window); unlock at t=+0.15 s", len(stream), len(stream) / TICK_HZ)
    snap(ctx, "K3 pre")
    wr("cnc/motor_lock", 15)
    wr("cnc/laser_latch", 1)
    wr("cnc/step_freq", TICK_HZ)
    wr("cnc/ramp_rate", 10000)
    try:
        with PulseDevice(ctx) as dev:
            dev.rewind()
            wr("cnc/enable", 1)
            ctx.sleep(0.5)
            dev.write(stream)
            wr("cnc/run", 1)
            time.sleep(0.15)                # inside the accel ramp
            wr("cnc/laser_latch", 0)
            ilk = rd("cnc/interlock_circuit")
            ctx.log("K3: latch UNLOCKED mid-ramp; interlock=%s (bit 3 should read 0)", ilk)
            hits, state = watch_laser_until_idle(ctx, 20)
            snap(ctx, "K3 post")
            ev["k3"] = {"interlock_after_unlock": ilk, "hits": hits[:10], "end_state": state,
                        "laser_on_sampled": rd("cnc/laser_on_sampled"),
                        "underruns": rd("cnc/underruns"), "faults": rd("cnc/faults")}
    finally:
        wr("cnc/laser_latch", 1)
        try:
            wr("cnc/ramp_rate", 125000)
        except OSError:
            ctx.log("WARNING: could not restore ramp_rate=125000")
    ctx.check((int(ilk) & LATCH_BIT) == 0, "K3: the unlock did not drive the latch pin (interlock=%s)", ilk)
    ctx.check(not hits, "K3: FIRE drive re-armed by a mid-run unlock: %s", hits[:10])
    ctx.log("K3 PASS: laser_enable stayed 0 for the entire run after the mid-ramp unlock")


# ---------------------------------------------------------------- FIRE A/B/U

def _fire_stream():
    return (POWER0 +                    # duty zero before any FIRE bit
            PAD * TICK_HZ +             # 1 s baseline
            FIRE * (2 * TICK_HZ) +      # 2.000 s FIRE window (bounded by pads)
            PAD * TICK_HZ +             # 1 s gap
            FIRE * (2 * TICK_HZ))       # 2.000 s FIRE window ending AT end-of-data


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
                wr("cnc/laser_latch", 0)
                ctx.log("fire %s: latch UNLOCKED for this run", mode)
            wr("cnc/run", 1)
            t0 = time.time()
            state = ""
            samples = []
            while time.time() - t0 < 20:
                ctx.checkpoint()
                state = rd("cnc/state")
                dt = time.time() - t0
                en, on, ons = rd("cnc/laser_enable"), rd("cnc/laser_on"), rd("cnc/laser_on_sampled")
                samples.append((round(dt, 2), state, en, on, ons))
                if mid is None and 1.5 < dt < 3.0:
                    mid = {"t": round(dt, 2), "laser_enable": en, "laser_on": on,
                           "laser_on_sampled": ons, "interlock": rd("cnc/interlock_circuit")}
                    ctx.log("fire %s mid (inside FIRE window): laser_enable=%s laser_on=%s "
                            "laser_on_sampled=%s interlock=%s", mode, en, on, ons, mid["interlock"])
                if state != "running":
                    break
                time.sleep(0.05)
            end_dt = time.time() - t0
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
    ev = {"mid": mid, "tail": tail, "moved": pos_before[:3] != pos_after[:3],
          "underruns_before": underruns_before, "underruns_after": int(rd("cnc/underruns")),
          "faults": rd("cnc/faults"),
          "any_laser_on": any(s[3] != "0" or s[4] != "0" for s in samples),
          "any_fire_driven": any(s[2] != "0" for s in samples)}
    ctx.log("fire %s: moved=%s underruns %d->%d faults=%s", mode, ev["moved"],
            ev["underruns_before"], ev["underruns_after"], ev["faults"])
    return ev


@test("kernel.fire-line", title="FIRE line: latch locked, unlocked-unarmed, true underrun, and a "
                               "mid-run unlock",
      subsystem="kernel", kind="auto", hardware="takeover", always=True, est_min=4,
      covers=_KERNEL_COVERS,
      requires=["kernel.k1-k2"], precheck=hv_off,
      steps=["Phases B, U and K3 unlock the latch with a zero-duty stream, so the drill starts only "
             "while the HV supply does not report good (true at idle; if it is refused, open the "
             "lid - the safety chain holds HV off - and start it again)."],
      description="Four phases behind one takeover of the pulse device, all zero duty. A: latch "
                  "locked, 40 000 streamed FIRE bits, nothing on the FIRE/LASER_ON nets. B: latch "
                  "unlocked with the chain unarmed, the FIRE line is driven mid-window and "
                  "LASER_ON stays off (the safety AND-gate holds), FIRE clear at end-of-data. U: "
                  "streaming declared, the terminal end-of-data is a true underrun, the backstop "
                  "drops FIRE and stop acks it. K3: the latch unlocked during the accel ramp "
                  "drives the latch pin but never restores the FIRE drive to a run already in "
                  "flight - laser_enable stays 0 for the whole run.")
def fire_line(ctx):
    ev = ctx.evidence
    require_hv_off(ctx)
    with ctx.takeover():
        try:
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
            ctx.check(not b["any_laser_on"], "B: LASER_ON active with the chain unarmed - the AND-gate did not hold")
            ctx.check(b["tail"]["state"] == "idle", "B: ended in %s", b["tail"]["state"])
            ctx.check(b["tail"]["laser_enable"] == "0", "B: FIRE still driven after end-of-data")
            ctx.check(b["underruns_after"] == b["underruns_before"], "B: underrun counted on a normal completion")
            ctx.log("fire B PASS: FIRE driven mid-window, LASER_ON off, FIRE clear at end-of-data")

            u = _fire_phase(ctx, "U")
            ev["U"] = u
            ctx.check(u["tail"]["state"] == "underrun", "U: expected the underrun state, got %s", u["tail"]["state"])
            ctx.check(u["tail"]["laser_enable"] == "0", "U: FIRE still driven after the underrun")
            ctx.check(not u["any_laser_on"], "U: LASER_ON active with the chain unarmed")
            ctx.check(u["tail"].get("acked_state") == "idle", "U: stop did not ack the underrun (state %s)",
                      u["tail"].get("acked_state"))
            ctx.check(u["underruns_after"] == u["underruns_before"] + 1,
                      "U: underrun counter %d -> %d", u["underruns_before"], u["underruns_after"])
            ctx.log("fire U PASS: true underrun, backstop dropped FIRE, stop acked")

            _k3_phase(ctx)
        finally:
            wr("cnc/laser_latch", 1)
            try:
                wr("cnc/disable", 1)            # the script's safe state
            except OSError:
                pass
            ctx.log("safe state restored: state=%s latch=LOCKED", rd("cnc/state"))


# ---------------------------------------------------------------- resume lead

@test("kernel.resume-lead", title="A resume's laser-off lead, and an end-of-data before the waypoint",
      subsystem="kernel", kind="auto", hardware="takeover", est_min=2,
      covers=_KERNEL_COVERS,
      requires=["kernel.k1-k2"], precheck=hv_off,
      steps=["Phase L unlocks the latch with a zero-duty stream, so the drill starts only while "
             "the HV supply does not report good (true at idle; if it is refused, open the lid - "
             "the safety chain holds HV off - and start it again)."],
      description="Two phases behind one takeover, zero duty, motors locked, a 1 kHz tick. E: a "
                  "resume whose lead is longer than the data (the ring drains before the waypoint) "
                  "ends the run at end-of-data within a few ticks, not one re-notify period (255 "
                  "ticks) later: the script publishes end-of-data in its mailbox and the host "
                  "decodes on that, not on the waypoint it was still waiting for. L: with the "
                  "latch unlocked and the chain unarmed, a resume with a 1000-byte lead over a "
                  "stream of FIRE bits keeps the FIRE line low through the lead and drives it from "
                  "the waypoint byte on. The script masks the laser bits itself, so no host write "
                  "reaches the GPIO data register while the script runs; LASER_ON stays off (the "
                  "safety AND-gate holds).")
def resume_lead(ctx):
    ev = ctx.evidence
    require_hv_off(ctx)
    tick = 1000
    with ctx.takeover():
        try:
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
                t0 = time.time()
                wr("cnc/resume", 10 * tick)
                state = wait_state(ctx, "running", 2, poll=0.002)
                ctx.check(state == "running", "E: the resume did not start (state=%s)", state)
                state = wait_state(ctx, "idle", 5, poll=0.002)
                dt = time.time() - t0
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

            # ---- L: the lead, latch unlocked, chain unarmed
            check_hv_off(ctx)
            lead = tick                                 # 1.0 s of FIRE bits held off
            lead_s = lead / tick
            stream = POWER0 + FIRE * (2 * tick) + PAD * (tick // 5)
            ctx.log("L: %d bytes = %.1f s (%.1f s of FIRE bits then pads); lead %d bytes = %.1f s; "
                    "latch UNLOCKED, chain unarmed", len(stream), len(stream) / tick,
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
                    wr("cnc/laser_latch", 0)
                    t0 = time.time()
                    wr("cnc/resume", lead)
                    n = 0
                    while time.time() - t0 < 10:
                        en, on = rd("cnc/laser_enable"), rd("cnc/laser_on")
                        samples.append((round(time.time() - t0, 4), en, on))
                        state = rd("cnc/state")
                        if state != "running" and time.time() - t0 > 0.2:
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
                      "L: LASER_ON active with the chain unarmed - the AND-gate did not hold")
            ctx.check(rd("cnc/laser_enable") == "0", "L: FIRE still driven after end-of-data")
            ctx.log("L PASS: FIRE low through the %.1f s lead, driven from the waypoint on, LASER_ON off",
                    lead_s)
        finally:
            wr("cnc/laser_latch", 1)
            try:
                wr("cnc/ramp_rate", 125000)
                wr("cnc/disable", 1)            # the script's safe state
            except OSError:
                pass
            ctx.log("safe state restored: state=%s latch=LOCKED", rd("cnc/state"))


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
        _param_write(PIC_SETTLE_PARAM, saved if saved not in (None, "0") else 500)
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
    ctx.check(ev["pic_settle_us_after"] == "500", "pic_settle_us left at %s", ev["pic_settle_us_after"])
    ctx.log("PASS: settled, an idle reader and a busy reader read the same (split %+d); control split %+d",
            ev["settled_split"], ev["control_split"])
