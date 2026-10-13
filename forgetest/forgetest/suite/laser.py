# Copyright 2026 514 LLC d/b/a OpenGlow
# Written by Scott Wiederhold
# https://community.openglow.org
# SPDX-License-Identifier:    MIT

"""laser.* - LIVE laser tests, ported from `scripts/bench/live_fire_drills.py`.

The live tests here emit. The page starts a firing one only with the
operator's eye-protection / fire-watch / exhaust acknowledgment; the
test then prompts for the scrap and the button, streams a small job
through the controller, and the machine fires only after the operator
presses the physical arm button - nothing here defeats that gate, and
forgetest never touches the laser latch. One emission per test; on abort
or error the job is soft-reset (`^X`: controlled stop, latch relocked).
The witnesses are forgectrl's `/status` (`laser.emission_samples` = the
kernel's LASER_ON sample count, `hv_current_raw`, `lid_ir`),
`/cool/status` (`armed`), the head's beam detector
(`head/beam_detect_analog`, the tube's own emission seen from the head:
a few hundred counts over its idle level while the beam is on, and
`beam_detect_digital` asserted), and the button LEDs (lit while the arm
waits or the window is open, dark after the disarm) - all sampled at
~8 Hz through the arm -> fire -> disarm lifecycle. The one thing the
operator still judges by eye is the mark the emission witness leaves,
once per campaign: the calibration of the sensor witnesses.
"""
import time

from ..catalog import test
from .. import hw
from ..runner import Failed
from .motion import (kernel_xy_mm, kernel_start, check_kernel_returned, Watch, machine_idle,
                     _job, _job_post, _job_wait, _lease_holder, _port_state, _words)

_LASER_COVERS = [("grblhal-glowforge", "src/**"), ("kernel-module-glowforge", "**"),
                 ("forgectrl", "src/super.c"), ("forgectrl", "src/cool.c"),
                 ("forgectrl", "src/status.c"), ("forgectrl", "src/main.c")]


ARM_CUE = ("LIVE FIRE. Eye protection on. Exhaust on. Extinguisher in reach. "
           "Put scrap under the head with %s clear, and close the lid.")

# The beam detector's emission signature: this much over its pre-fire
# level while the beam is on (bench: ~1834 idle, 2600-2890 at S300/S400).
BEAM_DELTA_MIN = 300


def sample(ctx):
    """One combined /status + /cool/status sample, or None on error."""
    fc = ctx.forgectrl
    try:
        st1, st = fc.get("/status")
        st2, cs = fc.get("/cool/status")
    except hw.HwError:
        return None
    if st1 != 200 or st2 != 200 or not isinstance(st, dict) or not isinstance(cs, dict):
        return None
    return {
        "t": time.time(),
        "kstate": st.get("state"),
        "emission": (st.get("laser") or {}).get("emission_samples"),
        "pgood": (st.get("laser") or {}).get("pgood_samples"),
        "faults": st.get("faults"),
        "hv": st.get("hv_current_raw"),
        "ir": st.get("lid_ir"),
        "homed": st.get("homed"),
        "armed": cs.get("armed"),
        "phase": cs.get("phase"),
        "fire_watch": cs.get("fire_watch"),
        "verdict": cs.get("verdict"),
        "beam": hw.sysfs_int("head/beam_detect_analog"),
        "beam_d": hw.sysfs_int("head/beam_detect_digital"),
        "exhaust": (st.get("fans") or {}).get("exhaust"),
        "button_lit": hw.button_lit(),
        "hv_enable": (st.get("switches") or {}).get("hv_enable"),
        "button_latch": hw.sysfs_int("cnc/button_latch"),
        # the whole readback word (bit 0 LASER_ON, 1 LASER_ENABLE, 2 button
        # latch, 3 laser latch, 4 interlock latch reset, 5 charge pump alive)
        # and the debounced switches, so a latch that sets mid-job says
        # which of its two inputs did it
        "il": hw.sysfs_int("cnc/interlock_circuit"),
        "lid": (st.get("switches") or {}).get("lid"),
        "button": (st.get("switches") or {}).get("button"),
        "locked": st.get("laser_locked"),
        "gstate": None,
        "msgs": "",
    }


TRAIL_FIELDS = ("t", "gstate", "kstate", "emission", "armed", "phase", "il", "button_latch",
                "locked", "lid", "button", "hv_enable", "beam", "hv", "msgs")

# The cooling phases that run the fans at their idle duty: the engine has
# no run session, so no cut airflow and no flow interrogation. Emission
# in one of these is the beam on the work with idle airflow.
IDLE_AIRFLOW_PHASES = ("idle", "warm-up")


def trail(samples):
    """The sample trail as rows for the evidence: relative seconds and the
    fields TRAIL_FIELDS names, so a run's timeline can be read back
    without a rerun."""
    if not samples:
        return []
    t0 = samples[0]["t"]
    rows = []
    for s in samples:
        row = [round(s["t"] - t0, 2)]
        for f in TRAIL_FIELDS[1:]:
            v = s.get(f)
            row.append(v if v != "" else None)
        rows.append(row)
    return rows


def beam_witness(ctx, ev, samples, base, tag=""):
    """The head's beam detector over a sample trail: its peak against the
    pre-fire level, and whether the digital flag asserted. Recorded, and
    judged where the trail carried emission."""
    beams = [s["beam"] for s in samples if s.get("beam") is not None]
    base_beam = base.get("beam") if base else None
    peak = max(beams) if beams else None
    digital = any(s.get("beam_d") for s in samples)
    key = ("beam" + ("_" + tag if tag else ""))
    ev[key] = {"idle": base_beam, "peak": peak, "delta": (peak - base_beam) if (peak is not None and
                                                                                   base_beam is not None) else None,
               "digital_seen": digital, "samples": len(beams)}
    ctx.log("beam detector%s: idle %s, peak %s, digital asserted %s", (" [%s]" % tag) if tag else "",
            base_beam, peak, digital)
    return ev[key]


def judge_beam(ctx, b, what="the burn"):
    ctx.check(b["samples"] > 0, "the beam detector was not readable during %s", what)
    ctx.check(b["delta"] is not None and b["delta"] >= BEAM_DELTA_MIN,
              "the head's beam detector did not see %s (idle %s, peak %s; the tube did not lase, "
              "or the detector is not reading)", what, b["idle"], b["peak"])


def check_button_dark(ctx, ev, key="button_dark", settle_s=5.0):
    """The button commanded dark (the LEDs' target level) within a few
    seconds: the controller writes it a moment after the cancel or the
    disarm it reports."""
    dt = ctx.wait_for(lambda: hw.button_lit() is False, settle_s)
    lit = hw.button_lit()
    ev[key] = lit
    ctx.check(lit is not None, "the button LEDs are not readable")
    ctx.check(lit is False, "the button is still lit %.0f s after it should have gone dark", settle_s)
    if dt:
        ctx.log("button dark after %.1f s", dt)


def prepare(ctx, g):
    """Guarantee a clean Idle start: clear a latched Door hold or an Alarm."""
    st = g.status_report()
    if "Door" in st["state"] or "Hold" in st["state"]:
        g.realtime(0x18)
        ctx.sleep(2)
        g.drain()
        st = g.status_report()
    if "Alarm" in st["state"]:
        ctx.log("unlock: %s", g.command("$X"))
        st = g.status_report()
    ctx.log("connect: %s", st["state"])
    ctx.check(st["state"].startswith("Idle"), "controller is %s, expected Idle", st["state"])
    return st


def stream(g, lines):
    for ln in lines:
        g.send_raw((ln + "\n").encode())


MARK_JOB = ["G91", "G21", "M4", "S400",
            "G1 X40 F200", "G1 Y40 F200", "G1 X-40 F200", "G1 Y-40 F200",
            "M5", "G90", "M2"]

# The same square at constant power. M4 scales power with speed, which hides
# what a restart does to the cut; M3 puts it on the scrap where the operator
# can see it - the deeper spot where a resumed cut accelerates from zero.
MARK_JOB_M3 = ["M3" if ln == "M4" else ln for ln in MARK_JOB]


def arm_and_fire(ctx, g, room="40 mm +X and +Y", job=None, timeout=240):
    """The arm cue, the job, and the wait for the emission witness - the
    prologue every live test shares. Returns the first sample with emission,
    or soft-resets and fails: no emission means the arm was refused or the
    button was never pressed."""
    ctx.ready(ARM_CUE % room)
    stream(g, job or MARK_JOB)
    ctx.arm_press()
    t0 = time.time()
    while time.time() - t0 < timeout:
        ctx.checkpoint()
        smp = sample(ctx)
        if smp and smp["emission"] and smp["emission"] > 0:
            ctx.clear_notice()
            return smp
        time.sleep(0.15)
    ctx.clear_notice()
    g.realtime(0x18)
    raise Failed("no emission seen within %d s (arm refused, or no button press)" % timeout)


def wait_grbl_port(ctx, timeout=30):
    """The controller's Grbl listener is accepting again. /mode reporting the
    process running is not the same thing: the supervisor has spawned it, but
    the socket may not be bound yet, and a bare connect would fail the test on
    a race rather than on the behavior it is about."""
    end = time.time() + timeout
    while time.time() < end:
        ctx.checkpoint()
        try:
            with ctx.grbl():
                return True
        except OSError:
            time.sleep(1)
    return False


def kill_trail(ctx, t0, seconds=5.0):
    """Sample emission / kernel state / armed for `seconds` after a kill."""
    trail = []
    for _ in range(int(seconds / 0.12)):
        s = sample(ctx)
        if s:
            trail.append((round(time.time() - t0, 2), s["emission"], s["kstate"], s["armed"]))
        time.sleep(0.12)
    return trail


def kill_fast_trail(t0, seconds=1.5):
    """The latch and the kernel state read from sysfs every few
    milliseconds after a kill: the supervisor's own reaction, which the
    HTTP samples of kill_trail cannot resolve. A controller death is a
    signal to the supervisor, not a poll, so the relock lands within
    milliseconds; the kernel leaves running once its stop ramp is done."""
    locked_at = stopped_at = None
    end = t0 + seconds
    while time.time() < end and (locked_at is None or stopped_at is None):
        now = time.time() - t0
        ilk = hw.sysfs_int("cnc/interlock_circuit")
        if locked_at is None and ilk is not None and ilk & IL_LASER_LATCH:
            locked_at = round(now, 3)
        st = hw.sysfs_read("cnc/state")
        if stopped_at is None and st is not None and st != "running":
            stopped_at = round(now, 3)
        time.sleep(0.005)
    return {"latch_locked_at_s": locked_at, "kernel_stopped_at_s": stopped_at}


def judge_kill(ctx, trail, what):
    """(first zero, tail stayed zero, kernel stopped running) from a trail."""
    for t in trail:
        ctx.log("  post-%s %s", what, t)
    zero_at = next((t for t, e, _, _ in trail if e == 0), None)
    tail_zero = all(e == 0 for _, e, _, _ in trail[-16:])
    not_running = all(k != "running" for _, _, k, _ in trail[-16:])
    ctx.log("emission first 0 at +%s s; last 2 s all zero: %s; kernel not running: %s",
            zero_at, tail_zero, not_running)
    return zero_at, tail_zero, not_running


IL_BUTTON_LATCH = 4      # cnc/interlock_circuit bit 2
IL_LASER_LATCH = 8       # bit 3: the SoC lock line, 1 = locked


def dwell_gap(samples):
    """The dwell-gap verdict over a sample trail. The button latch has
    two set inputs, the lid and the SoC lock, so while the lock line
    reads released the latch must read clear: from the first sample
    with emission (the press has cleared it by then) through every
    sample whose readback word shows the laser latch unlocked, which
    spans the kernel-run gap of the dwell and ends at the relock. Both
    bits come from the same word, so no lagging flag can misplace the
    window: the engine's armed flag follows the controller's next report,
    and the emission counter latches once per second and stays nonzero
    about two seconds past the relock (three runs on the bench failed on
    windows drawn from those). HV_ENABLE's dip in the gap and its return
    with emission after it are judged from the first emission on."""
    first = next((i for i, s in enumerate(samples) if s["emission"]), None)
    span = ([s for s in samples[first:] if s.get("il") is not None and not (s["il"] & IL_LASER_LATCH)]
            if first is not None else [])
    latch = [1 if (s["il"] & IL_BUTTON_LATCH) else 0 for s in span]
    set_at = [i for i, v in enumerate(latch) if v]
    hv_en = [s.get("hv_enable") for s in samples[first:]] if first is not None else []
    dip = next((i for i, v in enumerate(hv_en) if v is False), None)
    back_lit = dip is not None and any(
        s.get("hv_enable") and s["emission"] for s in samples[first + dip:])
    return {"button_latch_unlocked_max": max(latch) if latch else None,
            "button_latch_unlocked_samples": len(latch),
            "button_latch_set_at": set_at[:8],
            "hv_enable_dipped": dip is not None, "hv_enable_back_lit": back_lit}


def run_and_sample(ctx, g, job, sample_hz=8, overall_timeout=200):
    """Stream the job; sample forgectrl through arm -> fire -> disarm.
    Completes on: emission seen then Idle > 3 s; or armed then disarmed
    with no fire, Idle > 3 s, > 15 s in; or the overall timeout."""
    samples = []
    period = 1.0 / sample_hz
    s0 = sample(ctx)
    if s0:
        samples.append(s0)
    stream(g, job)
    t_start = time.time()
    next_t = t_start
    seen_emission = seen_armed = disarmed_now = False
    idle_since = None
    while time.time() - t_start < overall_timeout:
        ctx.checkpoint()
        now = time.time()
        if now >= next_t:
            smp = sample(ctx)
            if smp:
                samples.append(smp)
                if smp["emission"] and smp["emission"] > 0:
                    seen_emission = True
                if smp["armed"]:
                    seen_armed = True
                disarmed_now = seen_armed and not smp["armed"]
            next_t = now + period
        st = g.status_report()["state"]
        if samples:
            # the controller's state and whatever it said since the last
            # sample, next to the sample they belong to
            samples[-1]["gstate"] = st
            said = " ".join(l.strip() for l in g.drain().splitlines()
                            if l.strip() and l.strip() != "ok")
            if said:
                samples[-1]["msgs"] = (samples[-1]["msgs"] + " " + said).strip()[:400]
        if st.startswith("Idle"):
            if idle_since is None:
                idle_since = now
            idle_for = now - idle_since
            if seen_emission and idle_for > 3.0:
                break
            if disarmed_now and (now - t_start) > 15 and idle_for > 3.0:
                break
        else:
            idle_since = None
        time.sleep(0.05)
    return samples


class LiveJob:
    """Leaves the laser commanded off on every exit; a soft reset on
    abort/failure stops motion, relocks, and closes the armed window."""

    def __init__(self, ctx, g):
        self.ctx, self.g = ctx, g

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        try:
            if exc_type is not None:
                self.ctx.log("stopping the job: soft reset (%s)", exc_type.__name__)
                self.g.realtime(0x18)
                time.sleep(1)
            self.g.command("M5", timeout=1)
        except Exception:  # noqa: BLE001 - best effort on the way out
            pass
        return False


def wait_disarm(ctx, timeout):
    t0 = time.time()
    while time.time() - t0 < timeout:
        ctx.checkpoint()
        s = sample(ctx)
        if s and not s["armed"]:
            return time.time() - t0
        time.sleep(0.5)
    return None


@test("laser.emission-witness", title="Live emission witness (S400 vector mark) and job-based disarm",
      subsystem="laser", kind="live", mode="grbl", always=True, est_min=5,
      covers=_LASER_COVERS,
      requires=["kernel.latch-locked-idle", "kernel.k1-k2", "motion.jog-roundtrip"],
      actions=["button"],
      steps=["Scrap under the head with 20 mm of free +X and +Y travel; lid closed; exhaust on.",
             "Press the physical button when it lights white (the arm).",
             "At the end, confirm the square the laser marked, all four sides: the one judgment "
             "by eye in the campaign, the calibration of the sensor witnesses."],
      description="A 20 mm square outline at S400/F600 in dynamic laser mode with a 2 s dwell "
                  "(G4) between its second and third sides: emission_samples (the kernel's "
                  "LASER_ON sample count) goes nonzero during the fire window and returns to 0 "
                  "at Idle, HV current rises during the burn, the head's beam detector sees the "
                  "beam, and the armed window is observed. Across the dwell the kernel run ends "
                  "and restarts: HV_ENABLE drops with the charge-pump watchdog and is back for "
                  "the third side, while the hardware button latch stays clear (cnc/button_latch "
                  "0 in every armed sample), so the second half of the square marks without a "
                  "second press. The square ends at M5 with the window still open; once the "
                  "LASER_ON count reads 0 (the dark point), a rapid runs 20 mm out over the "
                  "first side and back, then the program ends. From the dark point until the "
                  "window closes, every sample reads no emission and the HV current at its idle "
                  "reading, and the head's reports place the rapid inside that stretch: LASER_ON "
                  "stays off with FIRE clear while the chain can fire. The M2 program end "
                  "disarms promptly (job-based, not the 60 s idle grace) and the button goes "
                  "dark. The operator confirms all four sides.")
def emission_witness(ctx):
    ev = ctx.evidence
    # The rapid after the M5 (G91 is modal from the square): out over the
    # first side and back, then the program end.
    rapid_tail = ["G0 X20", "G4 P2.5", "G0 X-20", "G4 P0.5", "G90", "M2"]

    def sample_to_disarm(g, timeout=20.0, sample_hz=8):
        """Sample until the armed window closes, or the timeout, with the
        controller's state and machine X beside each sample ("x"). A G4
        dwell reports Idle, so Idle cannot end this; the window closing at
        the program end does."""
        samples = []
        period = 1.0 / sample_hz
        t0 = time.time()
        while time.time() - t0 < timeout:
            ctx.checkpoint()
            smp = sample(ctx)
            rep = g.status_report()
            if smp:
                smp["gstate"] = rep.get("state")
                smp["x"] = (rep.get("MPos") or (None,))[0]
                samples.append(smp)
                if not smp["armed"]:
                    break
            time.sleep(period)
        return samples

    with ctx.grbl() as g, LiveJob(ctx, g):
        prepare(ctx, g)
        base = sample(ctx)
        ctx.check(base, "forgectrl /status or /cool/status unavailable")
        ev["pre_fire"] = base
        ctx.log("pre-fire: emission=%s hv=%s armed=%s verdict=%s", base["emission"], base["hv"],
                base["armed"], base["verdict"])
        ctx.check(not base["emission"], "emission_samples nonzero before the job (%s)", base["emission"])
        ctx.ready(ARM_CUE % "20 mm +X and +Y")
        # The square ends at M5 with no program end: the window stays open
        # for the rapid that follows.
        job = ["G91", "G21", "M4", "S400",
               "G1 X20 F600", "G1 Y20 F600", "G4 P2", "G1 X-20 F600", "G1 Y-20 F600",
               "M5"]
        ctx.arm_press()
        try:
            cut = run_and_sample(ctx, g, job)
            # The dark point: the M5 has run, the LASER_ON count reads 0, and
            # the window is still open. The rapid is sent only after it, so
            # the dark stretch covers the rapid by construction, and the
            # mid-square dwell's own zero cannot be taken for it.
            ctx.wait_for(lambda: (sample(ctx) or {}).get("emission") == 0, 10, poll=0.125)
            dark = sample(ctx)
            ctx.check(dark and dark["emission"] == 0,
                      "the LASER_ON count did not return to 0 after the M5 (%s)", dark and dark["emission"])
            ctx.check(dark["armed"], "the armed window closed after the M5, before the rapid "
                      "(phase %s): the rapid would not run under it", dark["phase"])
            # The engine's armed flag can still read true after the latch has
            # locked, and a dark reading with the latch locked is the unarmed
            # case this sample is here to exclude: the SoC lock line must read
            # released.
            ctx.check(dark["locked"] is False, "the laser latch is locked at the dark point "
                      "(laser_locked %s): the rapid would run unarmed", dark["locked"])
            rep = g.status_report()
            dark["gstate"] = rep.get("state")
            dark["x"] = (rep.get("MPos") or (None,))[0]
            ctx.check(dark["x"] is not None, "the controller reports no machine position: %s", rep)
            stream(g, rapid_tail)
            tail = sample_to_disarm(g)
        finally:
            ctx.clear_notice()
        samples = cut + [dark] + tail
        ev["trail_fields"] = list(TRAIL_FIELDS)
        ev["trail"] = trail(samples)
        beam = beam_witness(ctx, ev, cut, base)
        emis = [s["emission"] for s in cut if s["emission"] is not None]
        peak = max(emis) if emis else 0
        end = next((s["emission"] for s in reversed(samples) if s["emission"] is not None), None)
        hv = [s["hv"] for s in cut if s["hv"] is not None]
        ir_peak = [0, 0, 0, 0]
        for s in samples:
            if s["ir"] and len(s["ir"]) == 4:
                for i in range(4):
                    ir_peak[i] = max(ir_peak[i], s["ir"][i])
        armed_seen = any(s["armed"] for s in samples)
        # The beam may only come on inside a run session. Until the engine
        # has taken the armed window it is still holding the fans at their
        # idle duty, and the verdict standing on file is the one it
        # computed for the idle session before the arm.
        idle_fire = [s for s in samples
                     if s["emission"] and s["phase"] in IDLE_AIRFLOW_PHASES]
        ev.update({"samples": len(samples), "emission_peak": peak, "emission_end": end,
                   "hv_min": min(hv) if hv else None, "hv_max": max(hv) if hv else None,
                   "lid_ir_peak": ir_peak, "armed_seen": armed_seen,
                   "idle_airflow_fire": [(round(s["t"] - samples[0]["t"], 2),
                                          s["phase"], s["emission"]) for s in idle_fire],
                   "pgood_peak": max((s["pgood"] for s in samples if s["pgood"] is not None), default=None)})
        ctx.log("emission_samples peak=%s end=%s; hv %s..%s; lid_ir peak %s; armed seen %s",
                peak, end, ev["hv_min"], ev["hv_max"], ir_peak, armed_seen)
        # The dwell: a kernel-run gap inside the unlocked window. The button
        # latch has no input from the charge-pump watchdog (its SET inputs
        # are the lid and the SoC lock), so it must read clear from the
        # first emission until the relock, across the gap, while HV_ENABLE
        # drops with the watchdog and returns for the third side.
        gap = dwell_gap(samples)
        ev.update(gap)
        ctx.log("dwell gap: button latch through the unlocked window max=%s over %d samples (set at %s); "
                "HV_ENABLE dipped=%s, back with emission after it=%s", gap["button_latch_unlocked_max"],
                gap["button_latch_unlocked_samples"], gap["button_latch_set_at"], gap["hv_enable_dipped"],
                gap["hv_enable_back_lit"])
        # The first fire waits for the airflow: the exhaust reads at or
        # above its floor on the first sample that shows emission.
        floor = (ctx.forgectrl.settings() or {}).get("cool_tach_exhaust_min_rpm") or ""
        try:
            floor = float(floor) if floor else 6400.0
        except ValueError:
            floor = 6400.0
        first_lit = next((s for s in samples if s["emission"]), None)
        ev["exhaust_at_first_fire"] = first_lit["exhaust"] if first_lit else None
        ev["exhaust_floor"] = floor
        ctx.log("exhaust at the first emission sample: %s rpm (floor %.0f)",
                ev["exhaust_at_first_fire"], floor)
    # The rapid after the M5, with the window open and FIRE clear: every
    # sample from the dark point until the window closes reads no emission
    # and the HV current at its idle reading. The head's reports place the
    # rapid inside that stretch: out to +20 mm, then back at the start.
    stretch = [dark] + tail
    x0 = dark["x"]
    i_out = next((i for i, s in enumerate(stretch) if s.get("x") is not None and s["x"] - x0 >= 19.5), None)
    i_back = (next((i for i in range(i_out + 1, len(stretch))
                    if stretch[i].get("x") is not None and abs(stretch[i]["x"] - x0) < 0.05), None)
              if i_out is not None else None)
    closed = stretch[-1] if not stretch[-1]["armed"] else None
    relit = [s for s in stretch if s["emission"]]
    hv_dark = max((s["hv"] for s in stretch if s["hv"] is not None), default=0)
    dt = (closed["t"] - stretch[i_back]["t"]) if (closed and i_back is not None) else None
    ev["rapid"] = {"samples": len(stretch), "span_s": round(stretch[-1]["t"] - dark["t"], 2),
                   "x_start": x0, "x_max": max((s["x"] for s in stretch if s.get("x") is not None), default=None),
                   "out_at_s": round(stretch[i_out]["t"] - dark["t"], 2) if i_out is not None else None,
                   "back_at_s": round(stretch[i_back]["t"] - dark["t"], 2) if i_back is not None else None,
                   "relit": [(round(s["t"] - dark["t"], 2), s["emission"]) for s in relit][:8],
                   "hv_max": hv_dark, "locked_at_dark": dark["locked"],
                   "locked_at_out": stretch[i_out]["locked"] if i_out is not None else None}
    ev["disarm_after_idle_s"] = round(dt, 1) if dt is not None else None
    ctx.log("the rapid after the M5: %d samples over %.1f s from the dark point, out at +%s s, back at +%s s, "
            "%d with emission, HV max %s; the window closed %s s after the head was back", len(stretch),
            ev["rapid"]["span_s"], ev["rapid"]["out_at_s"], ev["rapid"]["back_at_s"], len(relit), hv_dark,
            ev["disarm_after_idle_s"])
    ctx.check(armed_seen, "the armed window was never observed (arm refused, or no button press)")
    ctx.check(peak > 0, "no emission witnessed on the square (emission_samples stayed 0)")
    first_idle_fire = ev["idle_airflow_fire"][0] if idle_fire else (None, None, None)
    ctx.check(not idle_fire,
              "the laser fired while the cooling engine had no run session, so the fans were at "
              "their idle duty (phase %s at t=%s s, emission %s)",
              first_idle_fire[1], first_idle_fire[0], first_idle_fire[2])
    ctx.check(end == 0, "emission_samples did not return to 0 at Idle (%s)", end)
    ctx.check(ev["exhaust_at_first_fire"] is not None and ev["exhaust_at_first_fire"] >= floor,
              "the beam started before the exhaust reached its floor (%s rpm at the first "
              "emission sample, floor %.0f)", ev["exhaust_at_first_fire"], floor)
    ctx.check(hv and max(hv) > min(hv), "HV current did not rise during the burn (%s..%s)",
              ev["hv_min"], ev["hv_max"])
    ctx.check(i_out is not None and i_back is not None,
              "the rapid did not run inside the dark stretch: out at %s, back at %s (X from %s, max %s)",
              ev["rapid"]["out_at_s"], ev["rapid"]["back_at_s"], x0, ev["rapid"]["x_max"])
    ctx.check(stretch[i_out]["locked"] is False, "the laser latch was locked with the head at +20 mm "
              "(laser_locked %s): the rapid ran unarmed", stretch[i_out]["locked"])
    ctx.check(not relit, "the laser emitted after the M5: %d samples, first at +%s s (emission_samples %s) "
              "- a rapid after M5 fired", len(relit), relit[0]["t"] - dark["t"] if relit else None,
              relit[0]["emission"] if relit else None)
    ctx.check(hv_dark <= HV_DARK_MAX, "HV current %s after the M5 (idle reads ~0): the discharge ran "
              "through the rapid", hv_dark)
    ctx.check(dt is not None and dt < 10.0,
              "the M2 job did not disarm promptly (%s s after the head was back; the idle grace is ~60 s)",
              ev["disarm_after_idle_s"])
    ctx.check(gap["button_latch_unlocked_samples"] and gap["button_latch_unlocked_max"] == 0,
              "the hardware button latch read SET while the laser latch was unlocked (max %s over %d "
              "samples, at %s)", gap["button_latch_unlocked_max"], gap["button_latch_unlocked_samples"],
              gap["button_latch_set_at"])
    ctx.check(ev["hv_enable_dipped"], "HV_ENABLE never dropped across the 2 s dwell (the kernel run did not end)")
    ctx.check(gap["hv_enable_back_lit"],
              "HV_ENABLE did not return with emission after the dwell (the third side ran dark)")
    judge_beam(ctx, beam)
    check_button_dark(ctx, ev)
    ctx.confirm("Did the laser mark a 20 mm square outline on the scrap, all four sides?")
    ctx.log("PASS: emission peak %s -> 0, HV %s..%s, beam +%s; the rapid after the M5 dark (HV max %s); "
            "disarmed %.1f s after the head was back, button dark, mark confirmed", peak, ev["hv_min"],
            ev["hv_max"], beam["delta"], hv_dark, dt)


HV_DARK_MAX = 20            # hv_current_raw reads 0 with the tube off; hundreds under fire


@test("laser.armed-kill", title="Armed kill mid-fire: the expected stop, then a SIGKILL",
      subsystem="laser", kind="live", mode="grbl", est_min=6,
      covers=_LASER_COVERS + [("forgectrl", "src/main.c")],
      requires=["laser.emission-witness", "motion.deadman"], actions=["button"],
      steps=["Scrap under the head with 40 mm of free +X and +Y travel; lid closed; exhaust on.",
             "Press the physical button when it lights white - twice over the test, once per burn. "
             "Each burn is cut short by the test; the sample trails judge the stops."],
      description="Both ways an armed job is killed, on one setup. Expected: mid-burn "
                  "POST /controller/stop - the supervisor writes cnc/stop and relocks before the "
                  "SIGTERM, so emission drops within 2.5 s and stays 0, the kernel is not running, "
                  "and the restart is a separate operator-judged step. Unexpected: mid-burn SIGKILL "
                  "of the controller - the supervisor's exit safing must end the fire tail inside "
                  "the ring's in-flight window, leave the latch locked, and respawn the controller. "
                  "The death is a signal to the supervisor, not a poll: the latch relocks within "
                  "300 ms of the kill and the kernel is out of running within a second.")
def armed_kill(ctx):
    ev = ctx.evidence
    fc = ctx.forgectrl

    # -- 1. the expected stop -------------------------------------------------
    with ctx.grbl() as g, LiveJob(ctx, g):
        prepare(ctx, g)
        smp = arm_and_fire(ctx, g)
        ctx.log("emission live (%s) - stopping the controller NOW", smp["emission"])
        t_stop = time.time()
        code, body = fc.post("/controller/stop")
        post_dt = time.time() - t_stop
        ctx.log("POST /controller/stop -> %s %s (%.2f s)", code, body, post_dt)
        trail = kill_trail(ctx, t_stop)
        zero_at, tail_zero, not_running = judge_kill(ctx, trail, "stop")
        st_mode, mode = fc.get("/mode")
        ev["expected"] = {"post_status": code, "post_s": round(post_dt, 2), "zero_at_s": zero_at,
                          "tail_zero": tail_zero, "kernel_not_running": not_running,
                          "mode_after_stop": mode, "trail": trail}
    ctx.check(code == 200, "POST /controller/stop -> %s", code)
    ctx.check(zero_at is not None and zero_at < 2.5,
              "emission did not drop within 2.5 s of the stop (first 0 at %s)", zero_at)
    ctx.check(tail_zero, "emission returned after the stop")
    ctx.check(not_running, "the kernel was still running after the stop")
    ev["beam_at_fire_stop"] = smp.get("beam")
    ctx.log("the controller is stopped (supervision held); restarting it (no motion, no laser)")
    st, body = fc.post("/controller/start")
    ctx.log("POST /controller/start -> %s %s", st, body)
    ctx.check(st == 200, "POST /controller/start -> %s", st)
    ctx.sleep(6)
    st, mode = fc.get("/mode")
    ev["mode_after_start"] = mode
    ctx.log("/mode after start: %s", mode)
    ctx.check(isinstance(mode, dict) and mode.get("controller") == "running",
              "controller not running after the restart: %s", mode)
    ctx.log("expected stop PASS: returned in %.2f s, emission 0 at +%s s, controller restarted",
            post_dt, zero_at)

    # -- 2. the SIGKILL -------------------------------------------------------
    # The pid is the RESTARTED controller's, not the one phase 1 stopped.
    import os as _os
    import signal as _signal
    st, m0 = fc.get("/mode")
    ctx.check(st == 200 and isinstance(m0, dict) and m0.get("controller") == "running",
              "controller not running before the kill: %s", m0)
    pid = m0.get("pid")
    ctx.check(wait_grbl_port(ctx), "the restarted controller never accepted a Grbl connection")
    with ctx.grbl() as g, LiveJob(ctx, g):
        prepare(ctx, g)
        smp = arm_and_fire(ctx, g)
        ctx.log("emission live (%s) - SIGKILL controller pid %s NOW", smp["emission"], pid)
        t_kill = time.time()
        _os.kill(pid, _signal.SIGKILL)
        fast = kill_fast_trail(t_kill)
        trail = kill_trail(ctx, t_kill)
    ctx.log("after the kill (sysfs): %s", fast)
    zero_at, tail_zero, not_running = judge_kill(ctx, trail, "kill")
    ilk = hw.sysfs_int("cnc/interlock_circuit")
    locked = ilk is not None and bool(ilk & (1 << 3))
    ev["sigkill"] = {"pid": pid, "zero_at_s": zero_at, "tail_zero": tail_zero,
                     "kernel_not_running": not_running, "latch_locked": locked, "trail": trail,
                     "fast": fast}
    ctx.log("latch locked after the kill: %s", locked)
    ctx.check(fast["latch_locked_at_s"] is not None and fast["latch_locked_at_s"] < 0.3,
              "the latch was not locked within 300 ms of the kill: %s", fast)
    ctx.check(fast["kernel_stopped_at_s"] is not None and fast["kernel_stopped_at_s"] < 1.0,
              "the kernel was still running 1 s after the kill: %s", fast)
    ctx.check(zero_at is not None and zero_at < 2.5,
              "emission did not drop within 2.5 s of the kill (first 0 at %s)", zero_at)
    ctx.check(tail_zero, "emission returned after the kill")
    ctx.check(not_running, "the kernel was still running after the kill")
    ctx.check(locked, "latch not locked after the kill")
    t0 = time.time()
    m1 = None
    while time.time() - t0 < 60:
        st, m1 = fc.get("/mode")
        if isinstance(m1, dict) and m1.get("controller") == "running" and m1.get("pid") != pid:
            break
        ctx.sleep(1)
    ev["mode_after_kill"] = m1
    ctx.log("/mode after the kill: %s", m1)
    ctx.check(m1 and m1.get("controller") == "running" and m1.get("pid") != pid,
              "supervisor did not respawn the controller: %s", m1)
    # The respawned controller starts locked and stays locked: a latch it
    # did not unlock is never relit by a run start, and no arm has asked.
    ctx.check(wait_grbl_port(ctx), "the respawned controller never accepted a Grbl connection")
    ctx.sleep(2)
    ilk = hw.sysfs_int("cnc/interlock_circuit")
    ev["latch_locked_after_respawn"] = ilk is not None and bool(ilk & IL_LASER_LATCH)
    ctx.check(ev["latch_locked_after_respawn"],
              "the respawned controller unlocked the latch with no arm (interlock_circuit=%s)", ilk)
    ev["beam_at_fire_kill"] = smp.get("beam")
    check_button_dark(ctx, ev)
    ctx.log("PASS: expected stop 0 at +%s s and SIGKILL 0 at +%s s, latch locked, controller "
            "respawned with the latch still locked, button dark", ev["expected"]["zero_at_s"], zero_at)


@test("laser.pause-resume-lid-cancel", title="One live cut: the button pauses and resumes it, the lid "
                                             "cancels it and sends the head home",
      subsystem="laser", kind="live", mode="grbl", est_min=7,
      covers=_LASER_COVERS + [("grblhal-glowforge", "src/glowforge_switches.c"),
                              ("grblhal-glowforge", "src/glowforge_switch_map.h")],
      requires=["laser.emission-witness", "motion.lid-cancel-home"],
      actions=["button", "lid"],
      steps=["Scrap under the head with 40 mm of free +X and +Y travel; lid closed; exhaust on.",
             "Press the physical button when it lights white (arm). Once the cut is under way the "
             "test asks for a press (pause), then another (resume), then for the lid: open it and "
             "leave it open until the head has come back, then close it.",
             "A pause longer than the armed window's idle grace (about a minute) closes the window; "
             "the resume then lights the button again and waits for a press."],
      description="The machine's own controls during one armed burn, in the order the factory uses "
                  "them. Press: the job feed-holds, emission stops, and the latch stays UNLOCKED "
                  "with the armed window open - a pause is not a cancel. Press again: the cut "
                  "resumes from where it stopped, lit from the first step (a pause is a sharp corner "
                  "in time; the cut is M3 so the corner dose is there to look at). Lid: emission "
                  "stops in hardware, the job is canceled "
                  "with the reason reported, the controller resets with the position kept and no "
                  "alarm, the armed window closes and the kernel latch relocks, the hardware button "
                  "latch reads SET, and the head returns to the job start with the lid still open.")
def pause_resume_lid_cancel(ctx):
    ev = ctx.evidence
    with ctx.grbl() as g, LiveJob(ctx, g):
        prepare(ctx, g)
        k0 = kernel_start(ctx)
        start = g.status_report()["MPos"]
        ev["start"] = start
        ev["kernel_start"] = k0
        base_beam = sample(ctx)
        beams = []                                  # (analog, digital) through the cut
        smp = arm_and_fire(ctx, g, job=MARK_JOB_M3)
        ev["emission_running"] = smp["emission"]
        beams.append((smp.get("beam"), smp.get("beam_d")))
        ctx.log("emission live (%s) - asking the operator to pause", smp["emission"])

        # -- the button pauses ------------------------------------------------
        g.drain()                                   # the message window opens here
        w = Watch(g)
        ctx.act("button", "press", text="The laser is cutting: the press pauses it.",
                until=w.in_state("Hold"), timeout=20, fail=False)
        st, text = w.last, w.text
        if st is not None and not st["state"].startswith("Hold"):
            st = None
        ctx.check(st is not None, "the press did not hold the job (state %s)", g.status_report()["state"])
        ev["hold_state"] = st["state"]
        ev["pause_message"] = "job paused" in text
        paused = []
        t1 = time.time()
        while time.time() - t1 < 4:
            s = sample(ctx)
            if s:
                paused.append((round(time.time() - t1, 2), s["emission"], s["armed"]))
            time.sleep(0.2)
        for p in paused:
            ctx.log("  paused %s", p)
        ev["paused_trail"] = paused
        ev["emission_zero_when_paused"] = all(e == 0 for _t, e, _a in paused[-8:]) if paused else None
        ev["armed_while_paused"] = paused[-1][2] if paused else None
        ilk = hw.sysfs_int("cnc/interlock_circuit")
        ev["latch_locked_while_paused"] = ilk is not None and bool(ilk & (1 << 3))
        ctx.log("paused: %s, emission 0 = %s, armed = %s, latch locked = %s", ev["hold_state"],
                ev["emission_zero_when_paused"], ev["armed_while_paused"],
                ev["latch_locked_while_paused"])
        ctx.check(ev["emission_zero_when_paused"], "emission did not stop while the job was paused: %s", paused)
        ctx.check(ev["armed_while_paused"], "the armed window closed on the pause (a pause is not a cancel)")
        ctx.check(not ev["latch_locked_while_paused"],
                  "the kernel latch relocked on the pause - the resume could not fire without a new arm press")

        # -- the button resumes -----------------------------------------------
        g.drain()
        w = Watch(g)
        ctx.act("button", "press", text="The cut is paused: the press resumes it.",
                until=w.left_state("Hold"), timeout=40, fail=False)
        st, text = w.last, w.text
        if st is not None and st["state"].startswith("Hold"):
            st = None
        ev["resumed_state"] = st["state"] if st else g.status_report()["state"]
        ev["resume_message"] = "job resumed" in text
        ctx.check(st is not None, "the second press did not resume the job (still held: %s)",
                  ev["resumed_state"])
        ctx.check(st["state"].startswith(("Run", "Idle")),
                  "the job left the hold into %s, not into motion", st["state"])
        back = False
        t2 = time.time()
        trail = []
        while time.time() - t2 < 20:
            ctx.checkpoint()
            s = sample(ctx)
            if s:
                trail.append((round(time.time() - t2, 2), s["emission"]))
                beams.append((s.get("beam"), s.get("beam_d")))
                if s["emission"] and s["emission"] > 0:
                    back = True
                    break
            time.sleep(0.15)
        ev["emission_back_after_s"] = trail[-1][0] if trail else None
        ev["resume_trail"] = trail[-6:]
        ctx.log("emission after the resume: %s", trail[-6:])
        ctx.check(back, "emission did not return after the resume: %s", trail[-6:])

        # -- the lid cancels --------------------------------------------------
        g.drain()
        ctx.act("lid", "open", text="The cut is running again: leave the lid open until the head "
                "has come back.", timeout=30)
        t_lid = time.time()
        lid_trail = []
        text = ""
        while time.time() - t_lid < 8:
            s = sample(ctx)
            if s:
                lid_trail.append((round(time.time() - t_lid, 2), s["emission"], s["kstate"], s["armed"]))
            text += g.drain()
            time.sleep(0.12)
        for t in lid_trail:
            ctx.log("  post-lid %s", t)
        ev["messages"] = [ln for ln in text.splitlines() if ln.startswith("[MSG:") or "help]" in ln
                          or ln.startswith("ALARM")]
        ctx.log("controller: %s", ev["messages"])
        zero_at = next((t for t, e, _, _ in lid_trail if e == 0), None)
        tail_zero = all(e == 0 for _, e, _, _ in lid_trail[-16:])
        ev.update({"lid_zero_at_s": zero_at, "lid_tail_zero": tail_zero})
        ctx.check(zero_at is not None and zero_at < 3.0,
                  "emission did not stop after the lid opened (first 0 at %s)", zero_at)
        ctx.check(tail_zero, "emission returned after the lid opened")
        ctx.check("lid opened - job canceled" in text, "the lid open was not reported as canceling the job")
        ctx.check("help]" in text, "no reset banner after the cancel")
        ctx.check("ALARM" not in text, "an alarm was raised on the cancel (position should be kept)")
        t3 = time.time()
        returned = "returned to the job start" in text
        while not returned and time.time() - t3 < 30:
            ctx.checkpoint()
            text += g.drain()
            returned = "returned to the job start" in text
            time.sleep(0.2)
        ev["returned_message"] = returned
        ctx.check(returned, "the head did not report returning to the job start")
        st = g.status_report()
        drift = max(abs(st["MPos"][i] - start[i]) for i in range(2))
        ev["drift_mm"] = round(drift, 3)
        s = sample(ctx)
        ev["armed_after"] = s["armed"] if s else None
        ilk = hw.sysfs_int("cnc/interlock_circuit")
        ev["latch_locked"] = ilk is not None and bool(ilk & (1 << 3))
        ev["button_latch"] = hw.sysfs_int("cnc/button_latch")
        ctx.log("returned: drift %.3f mm; armed=%s latch_locked=%s button_latch=%s", drift,
                ev["armed_after"], ev["latch_locked"], ev["button_latch"])
        ctx.check(drift <= 0.05, "head not back at the job start (drift %.3f mm)", drift)
        ctx.check(ctx.forgectrl.wait_idle(10, abort=ctx.aborted), "machine not idle after the return")
        check_kernel_returned(ctx, ev, k0)
        ctx.check(not ev["armed_after"], "armed window still open after the cancel")
        ctx.check(ev["latch_locked"], "kernel latch not locked after the cancel")
        ctx.check(ev["button_latch"] == 1, "hardware button latch not SET after the lid open (%s)",
                  ev["button_latch"])
        beam_witness(ctx, ev, [{"beam": b, "beam_d": d} for b, d in beams], base_beam)
        judge_beam(ctx, ev["beam"], "the cut")
        check_button_dark(ctx, ev)
        ctx.act("lid", "close")
        ctx.sleep(1)
    ctx.log("PASS: button paused the burn (emission 0, armed kept, latch unlocked) and resumed it; "
            "the lid then canceled it - emission 0 at +%s s, reset without alarm, returned (drift "
            "%.3f mm), button latch SET", zero_at, drift)


# ---------------------------------------------------------------- the recorder, as a job of the runner's

def _laser_keys(fc):
    s = fc.settings()
    return {k: s.get(k) for k in ("laser_floor_density", "laser_dose_curve")}


def _arming(fc):
    rep = ((fc.status().get("grbl") or {}).get("report") or {})
    return bool((rep.get("laser") or {}).get("arming"))


@test("laser.recorder-dark", title="The dose-curve recorder takes the machine as a job, arms, and is stopped dark",
      subsystem="laser", kind="auto", mode="grbl", est_min=2,
      covers=_LASER_COVERS + [("forgectrl", "src/curverec.*"), ("forgectrl", "src/jobrun.*"),
                              ("forgectrl", "src/jobstream.*"), ("forgectrl", "src/lease.*"),
                              ("forgectrl", "src/settings.*")],
      requires=["motion.job"],
      steps=["Lid closed, bed clear. Nobody presses the button: the recorder is stopped before its press.",
             "The ladder begins at the controller's X0 Y0: the head goes there, and is brought back."],
      description="POST /curve/record starts the dose ladder as a job of the job runner's. While it "
                  "waits for a press that never comes: /status names the recorder as the lease "
                  "holder of kind sender, GET /job reports the run (owner recorder, not a posted "
                  "program), the floor and the curve are overridden (0 and off), the controller "
                  "reaches its arm wait, POST /job/abort refuses to stop a run it did not start, a "
                  "posted job is refused in the recorder's name, and the kernel's LASER_ON sample "
                  "count and the HV current stay dark. POST /curve/stop then ends it: the recorder "
                  "says it was stopped before the ladder fired, the lease is free, and both laser "
                  "keys read as they did before. The head is returned by a posted job.")
def recorder_dark(ctx):
    ev = ctx.evidence
    fc = hw.Forgectrl()
    machine_idle(ctx)
    x0, y0 = kernel_start(ctx)
    keys0 = _laser_keys(fc)
    base = sample(ctx)
    ctx.check(base and not base["emission"], "emission_samples nonzero before the recording (%s)", base)
    ctx.check(not _lease_holder(fc), "the machine is not free at the start: %s", _lease_holder(fc))
    started = False
    samples = []
    try:
        st, body = fc.post("/curve/record")
        ctx.check(st == 200 and isinstance(body, dict) and body.get("state") == "waiting",
                  "POST /curve/record -> %s %s", st, _words(body)[:200])
        started = True
        ok = ctx.wait_for(lambda: _lease_holder(fc).get("owner") == "recorder", 5, poll=0.1)
        held = _lease_holder(fc)
        ev["holder"] = held
        ctx.check(ok is not None and held.get("kind") == "sender", "the lease does not name the recorder: %s", held)
        rec = _job(fc)
        ev["job_record"] = rec
        ctx.check(rec["state"] == "running" and rec["owner"] == "recorder" and rec["program"] is False,
                  "GET /job while the recorder runs: %s", rec)
        over = _laser_keys(fc)
        ev["override"] = over
        ctx.check(str(over["laser_dose_curve"]) == "off" and float(over["laser_floor_density"] or 0) == 0.0,
                  "the floor and the curve are not overridden for the ladder: %s", over)

        ok = ctx.wait_for(lambda: _arming(fc), 40, poll=0.25)
        ctx.check(ok is not None, "the controller never reached its arm wait: %s",
                  (fc.status().get("grbl") or {}).get("report"))
        ctx.log("the recorder holds the machine as %s, and the controller waits for the press", held)
        st, body = fc.post("/job/abort")
        ev["abort_route"] = [st, _words(body)[:160]]
        ctx.check(st == 409 and "stop it where it was started" in _words(body),
                  "POST /job/abort on the recorder's run -> %s %s", st, _words(body)[:160])
        st, body = _job_post(fc, "G21\n")
        ev["job_beside"] = [st, _words(body)[:160]]
        ctx.check(st == 409 and "the dose-curve recorder holds the machine" in _words(body),
                  "a posted job beside the recorder -> %s %s", st, _words(body)[:160])
        for _ in range(16):                 # two seconds in the arm wait, by the witnesses
            samples.append(sample(ctx))
            ctx.sleep(0.125)
        ctx.check(_lease_holder(fc).get("owner") == "recorder", "a refused request took the lease away")
    finally:
        if started:
            st, body = fc.post("/curve/stop")
            ev["stopped"] = body if isinstance(body, dict) else _words(body)[:160]
    ctx.check(st == 200 and isinstance(body, dict) and body.get("state") == "failed"
              and "before the ladder fired" in body.get("reason", ""), "POST /curve/stop -> %s %s",
              st, _words(body)[:200])
    ok = ctx.wait_for(lambda: not _lease_holder(fc), 10, poll=0.2)
    ctx.check(ok is not None, "the lease was not given back: %s", _lease_holder(fc))
    keys1 = _laser_keys(fc)
    ev["keys"] = {"before": keys0, "after": keys1}
    ctx.check(keys1 == keys0, "the laser keys were not restored: %s, were %s", keys1, keys0)

    lit = [s for s in samples if s and s["emission"]]
    hv_max = max((s["hv"] for s in samples if s and s["hv"] is not None), default=0)
    ev["witnesses"] = {"samples": len(samples), "with_emission": len(lit), "hv_max": hv_max,
                       "job": _job(fc)["emission"]}
    ctx.check(not lit and hv_max <= HV_DARK_MAX and _job(fc)["emission"]["laser_on_samples"] == 0,
              "the recorder's arm wait was not dark: %s", ev["witnesses"])

    # The ladder's first move took the head to the controller's X0 Y0.
    ctx.sleep(1.0)
    dx, dy = (a - b for a, b in zip(kernel_xy_mm(ctx), (x0, y0)))
    ev["moved_mm"] = [round(dx, 3), round(dy, 3)]
    ev["controller_after_stop"] = _port_state(fc).get("state")
    ctx.log("after the stop the controller is in %s; the head is %.3f, %.3f mm from where it was found",
            ev["controller_after_stop"], dx, dy)
    if abs(dx) > 0.05 or abs(dy) > 0.05:
        st, body = _job_post(fc, "G21\nG91\nG1 X%.3f Y%.3f F2400\nG90\n" % (-dx, -dy), unlock="1")
        ctx.check(st == 200, "the returning job -> %s %s", st, _words(body)[:200])
        rec, _far = _job_wait(ctx, fc, 60)
        ctx.check(rec["state"] == "done", "the returning job: %s", rec)
    elif str(ev["controller_after_stop"]).startswith("Alarm"):
        st, body = _job_post(fc, "G21\n", unlock="1")
        rec, _far = _job_wait(ctx, fc, 20)
        ctx.check(st == 200 and rec["state"] == "done", "the unlocking job: %s %s", st, rec)
    check_kernel_returned(ctx, ev, (x0, y0), tag="recorder")
    machine_idle(ctx)
    ctx.log("PASS: the recorder ran as a job of the runner's, held the machine as a sender through its arm "
            "wait with every witness dark, and its stop freed the machine and restored the laser keys")
