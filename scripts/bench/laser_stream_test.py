#!/usr/bin/env python3
"""Host-side verification of the laser pulse-stream emission.

Runs the native grblHAL_glowforge binary in null-sink mode with
GFSINK_DUMP capturing the shipped byte stream, drives small laser jobs
over TCP, then checks the dumps against the kernel feeder contract:

  1. a power byte (bit 7) leads the stream, before any tick byte
  2. no two consecutive power bytes (the SDMA script drops the second)
  3. the first FIRE bit (0x10) comes after a nonzero power byte
  4. power values match the S words through the core's mapping, floor
     included ($30=1000, $31=0, $35 = the board's floor), and no duty
     under FIRE falls below that floor
  5. FIRE only spans the cutting moves: none before the job, none during
     the G0 return, none at the tail
  6. step accounting survives the insertions: X returns to net zero and
     peaks at the programmed 10 mm
  7. termination: every stream ends with FIRE clear, including an M3
     (constant-power) job whose core never issues a laser-off update -
     the stream must never lean on the kernel's end-of-data backstop
  8. no FIRE bit ever rides a zero-step gap: a stepless run of stream
     bytes carrying FIRE longer than any legitimate between-step
     interval is a stationary dwell burn
  9. rules 7-8 hold across rapid cycle stop/start churn (planner-starve
     shaped jobs), where the FIRE state of the previous cycle must not
     leak into the idle-gap pad bytes
 10. a power ladder fires every rung at the duty commanded for it: no
     FIRE tick rides a duty that was never commanded (a run start resets
     the hardware duty to ~100 %, so a fire bit reaching the stream
     ahead of the rung's power byte would burn at full power), and the
     fire ticks divide evenly across the rungs, which is what fails if a
     rung's opening ticks carry the previous rung's duty
 11. under the density dose model no level ever reaches PWMSAR: every
     power byte carries full duty (one still leads each kernel run), and
     a level change inside a run costs no stream byte at all
 12. density matches the level the core commanded, rung by rung, and no
     burst is longer than the base period
 13. the model is a mask and never a source: run the same job under both
     models and every FIRE tick of the density run is a FIRE tick of the
     analog run, on an identical motion grid
 15. the minimum pulse width holds: no emitted burst is shorter than
     laser_pulse_min_ticks, and the levels too faint to fill it still
     render their exact average density - the debt is carried, so a low
     level becomes fewer full-width pulses rather than stubs
 14. a laser state change made while the stream is idle survives to the
     next run: a standalone S word between moves, from a sender slow
     enough to drain the planner, must still cut at the level it asked
     for rather than dark at a stale duty
 16. and the off transition survives the same way: an M5 executed with
     the planner drained and the kernel run over must darken the rapids
     that follow it, and a bare G0 sent with the spindle off must ship
     dark, under both dose models - the stream's wanted fire state is
     the only thing those moves consult, and a stale true there lights
     the next run at the last level (full duty under density)
 17. and a job's first cut at the level the previous job ended at
     fires: S is modal across M2, the core records the level a set_state
     carries and skips the per-segment update while it is unchanged, so
     the M3 that opens the next job is the only thing that can light its
     first move - set_state must push the whole state, fire included,
     never the duty alone
 22. a jog never fires, whatever the modal spindle says: M3 S1000 with
     the window open and then jogs from Idle (a sender's Fire button plus
     its Move panel) ship every jog tick dark, and the cut after them lit
 23. the corner rolloff shapes against the executing block's own S: two
     cuts at S300 and S1000 queued together render the S300 cruise at the
     S300 density however far ahead the parser has read
 18. the floor is derived, never typed: $35 is loaded from the floor
     key at every precompute, so a $35 typed by the sender is
     overwritten - the ladder renders through the key's floor, and the
     arm report names the model, the floor and the curve in force
 19. the dose curve bends S onto the density that delivers the
     commanded light fraction: with the bench-default curve in force a
     ladder of S rungs renders the curve's densities (half light lands
     near 80 percent density), monotonic, floored and ceiled by
     $35/$36; every other session runs with laser_dose_curve = off so
     its S-to-level arithmetic stays exact
 20. the corner rolloff starves the slow spots: under M4 with the curve
     in force, the accelerate-in head of a line renders less density at
     the default gamma of 2 than at gamma 1, and the cruise middle
     renders the same - the exponent shapes only the velocity-scaled
     rolloff, never the programmed level
 24. a hold verdict is held again after a resume: with the engine's
     verdict at its fail tier (hold, fire blocked, no resume) the client
     holds the job; a ~ under that verdict, which is what a button press
     or a sender does, moves the head for at most one client poll, dark,
     before the client holds it again and says so; the clean verdict then
     resumes the hold the client took, and the rest of the line cuts lit

The analog sessions select the reference mode through the config; on
hardware the controller ignores it (density is the only product model -
analog's strike transient puts a spot at every beam-on), but the
null-sink build honors it so these rules can hold the density model to
account against the continuous rendering (rule 13's mask above all).

Usage: laser_stream_test.py [path-to-binary]   (default ./build-native/grblHAL_glowforge)
"""
import os
import re
import shutil
import signal
import socket
import subprocess
import sys
import tempfile
import threading
import time

BIN = os.path.abspath(sys.argv[1] if len(sys.argv) > 1 else "build-native/grblHAL_glowforge")
PORT = 2399
STEPS_PER_MM = 53.333

# The S -> level mapping the board defaults produce: $30 = 1000, $31 = 0,
# and a $35 floor (boards/glowforge.h DEFAULT_SPINDLE_PWM_MIN_VALUE)
# against the hardware's 127-count period. The shipped floor is the
# density one; the analog sessions below select their model explicitly
# rather than inheriting the default, so both paths stay covered. Changing the board's floor
# changes every expectation below, which is why it is mirrored here
# rather than inferred from the stream.
PWM_PERIOD = 127
PWM_MIN_PCT = 10.0
PWM_MIN = int(PWM_PERIOD * PWM_MIN_PCT / 100.0)
RPM_MAX = 1000.0


def duty_for(s):
    """Duty the core computes for an S word, floor included."""
    return int(s * (PWM_PERIOD - PWM_MIN) / RPM_MAX) + PWM_MIN

# Longest stepless run allowed to carry FIRE, in machine ticks. The
# slowest legitimate between-step interval in these jobs is the first
# step of an accel-from-rest: sqrt(2 * (1/53.333 mm) / 700 mm/s^2)
# = 7.3 ms = ~206 ticks at 28160 Hz. 500 gives >2x margin while staying
# far below any idle-gap pad run.
FIRE_GAP_LIMIT_TICKS = 500

WAIT_IDLE = ("wait_idle",)

# Session A: the original M4 dynamic-power job (rules 1-6).
JOB_M4 = [
    "M4 S0",
    "G1 X5 F600 S500",
    "G1 X10 S1000",
    "G0 X0",
    "M5",
]

# Session Z: the lens is in the stream. The sender references the lens
# (M103, as a commissioning card does), then a 1 mm move up and back at
# the screw's 2.922 half-steps per millimeter: three Z steps with the
# direction bit set, three with it clear.
JOB_Z = [
    "M103 Z3 P14 Q20",
    "G0 Z4",
    "G0 Z3",
]

# Session B: M3 constant power to the end of the stream. The core never
# issues a laser-off update for M3, so the stream engine itself must
# terminate the cycle dark (rule 7).
JOB_M3_TERM = [
    "M3 S1000",
    "G1 X5 F600",
    WAIT_IDLE,
    ("sleep", 1.0),
    "M5",
]

# Session C: rapid cycle churn - many tiny laser moves sent one at a
# time with small gaps, so cycles stop and restart the way a planner
# starve produces them (rules 8-9).
JOB_CHURN = []
for _ in range(30):
    JOB_CHURN.append("G1 X0.2 F600 S800")
    JOB_CHURN.append(("sleep", 0.02))
    JOB_CHURN.append("G1 X0 S800")
    JOB_CHURN.append(("sleep", 0.02))
JOB_CHURN.insert(0, "M4 S0")
JOB_CHURN.append("M5")

# Session D: a power ladder in the shape the bench threshold drill uses -
# constant power (M3) so the commanded duty is the tested duty, rungs
# ascending, a dark G0 between them. Full power is deliberately absent
# from the ladder, so duty 127 under FIRE can only be a leak.
LADDER_S = (20, 30, 60, 120, 200, 300)
LADDER_DUTY = tuple(duty_for(s) for s in LADDER_S)
LADDER_MM = 5.0
JOB_LADDER = ["G91", "G21", "M3"]
for _i, _s in enumerate(LADDER_S):
    JOB_LADDER.append("S%d" % _s)
    JOB_LADDER.append("G1 X%g F300" % (LADDER_MM if _i % 2 == 0 else -LADDER_MM))
    JOB_LADDER.append("G0 Y1")
JOB_LADDER.append("M5")


# Sessions E-G: the density dose model. $35 = 0 for the ladder because
# the floor exists only to keep an analog duty out of the tube's dead
# band - under density every pulse is full-power, and a floor would just
# clamp the light end of the range.
# The floors are config keys, loaded into $35 at every arm (rule 18).
# The analog sessions pin theirs at the board's density floor so the
# duty expectations above hold unchanged; the analog default is the
# tube's lasing duty (16), covered by the switch sessions below.
ANALOG_FLOOR_DEFAULT_PCT = 16.0
ANALOG_CONF = ("laser_power_model = analog\n"
               "laser_dose_curve = off\n"
               "laser_floor_analog = %g\n" % PWM_MIN_PCT)
DENSITY_PERIOD = 20
DENSITY_MIN_TICKS = 3
DENSITY_CONF_BASE = ("laser_pulse_ticks = %d\n"
                     "laser_pulse_min_ticks = %d\n"
                     % (DENSITY_PERIOD, DENSITY_MIN_TICKS))
# The density ladder runs unfloored: the floor exists only to keep an
# analog duty out of the tube's dead band, and here it would just clamp
# the light end of the range. A floor of 0 is honored as written.
DENSITY_CONF = ("laser_power_model = density\n"
                "laser_dose_curve = off\n"
                "laser_floor_density = 0\n" + DENSITY_CONF_BASE)
# The shipped density default: no floor key, so the board's floor applies.
DENSITY_CONF_FLOORED = ("laser_power_model = density\n"
                        "laser_dose_curve = off\n" + DENSITY_CONF_BASE)
# The shipped default: the bench curve in force (no keys at all).
DENSITY_CONF_CURVED = "laser_power_model = density\n" + DENSITY_CONF_BASE

# The compiled bench-default curve (glowforge_laser.c curve_default),
# mirrored here the way the floor is: changing it changes rule 19.
CURVE_DEFAULT = ((10.0, 0.5), (20.0, 2.0), (30.0, 7.0), (45.0, 21.0),
                 (60.0, 37.0), (80.0, 50.0), (100.0, 100.0))


def curve_density_for(s_val):
    """The density fraction the bench-default curve maps an S onto,
    before the $35/$36 clamp (mirrors curve_apply)."""
    l = s_val / RPM_MAX * 100.0
    pts = CURVE_DEFAULT
    if l <= pts[0][1]:
        return pts[0][0] * (l / pts[0][1]) / 100.0
    i = 1
    while i < len(pts) - 1 and l > pts[i][1]:
        i += 1
    d0, l0 = pts[i - 1]
    d1, l1 = pts[i]
    f = min(1.0, (l - l0) / (l1 - l0))
    return (d0 + f * (d1 - d0)) / 100.0


CURVE_S = (100, 300, 500, 800, 1000)
JOB_CURVE = ["G91", "G21", "M3"]
for _s in CURVE_S:
    JOB_CURVE.append("S%d" % _s)
    JOB_CURVE.append("G1 X%g F300" % (LADDER_MM if _s % 2 == 0 else LADDER_MM))
    JOB_CURVE.append("G0 Y1")
JOB_CURVE.append("M5")
DENSITY_LEVEL = tuple(int(x * PWM_PERIOD / RPM_MAX) for x in LADDER_S)
# A $35 typed ahead of the job: rule 18 says the arm overwrites it.
JOB_DENSITY = ["$35=0"] + JOB_LADDER


def duty_for_floor(s, floor_pct):
    """Duty the core computes for an S word against a given floor."""
    lo = int(PWM_PERIOD * floor_pct / 100.0)
    return int(s * (PWM_PERIOD - lo) / RPM_MAX) + lo


# Session H: three levels inside one kernel run. The moves are short and
# fast so the planner never drains, and each carries its own S word, so
# the level changes land mid-run. Analog pays a power byte per level;
# density pays none, because the level rides the FIRE bits.
JOB_LEVELS = ["G91", "G21", "M3"]
for _s in (100, 300, 600):
    for _ in range(20):
        JOB_LEVELS.append("G1 X0.5 F3000 S%d" % _s)
JOB_LEVELS.append("M5")


# Session I: the levels arrive on their own lines, and the moves are long
# enough that the planner drains between them, so each S is executed with
# nothing streaming. The state has no event to ride and must be
# re-asserted at the next run's first byte.
IDLE_S_LEVELS = (100, 300, 600)
IDLE_S_MM = 5.0
IDLE_S_FEED = 300
JOB_IDLE_S = ["G91", "G21", "M3"]
for _i, _s in enumerate(IDLE_S_LEVELS):
    JOB_IDLE_S.append("S%d" % _s)
    JOB_IDLE_S.append("G1 X%g F%d" % (IDLE_S_MM if _i % 2 == 0 else -IDLE_S_MM,
                                      IDLE_S_FEED))
JOB_IDLE_S.append("M5")


# Session J: the bench ladder's shape. M5 executes with the planner
# drained and the kernel run over, and the rapids that follow start a
# new run; the core issues no per-segment laser update for moves made
# with the spindle off, so the stream's wanted state is all that decides
# whether those rapids fire. A bare G0 with no M3 since the M5 is the
# same case one step further.
M5_IDLE_MM = 5.0
M5_IDLE_FEED = 600
M5_IDLE_TICKS = M5_IDLE_MM / (M5_IDLE_FEED / 60.0) * 28160
JOB_M5_IDLE = [
    "G91", "G21",
    "M3 S500",
    "G1 X%g F%d" % (M5_IDLE_MM, M5_IDLE_FEED),
    WAIT_IDLE, ("sleep", 0.5),
    "M5", ("sleep", 0.5),
    "G0 X%g" % -M5_IDLE_MM, "G0 Y1",
    WAIT_IDLE,
    "G0 X%g" % M5_IDLE_MM,
    WAIT_IDLE,
    "M3 S500",
    "G1 X%g" % -M5_IDLE_MM,
    WAIT_IDLE, ("sleep", 0.5),
    "M5",
]


# Session K: two jobs in one controller process, the second at the level
# the first ended at. M2 leaves S modal and resets the motion mode to G1,
# so the next job's M3 executes at that S; the core records it and issues
# no per-segment update for a G1 at the same level, so the set_state is
# the only thing that can light it. The parser starts in G0, which is why
# a process's FIRST job never shows this: its M3 runs at rpm 0.
JOB_NEXT = [
    "G91", "G21", "M3", "S500",
    "G1 X%g F%d" % (M5_IDLE_MM, M5_IDLE_FEED),
    WAIT_IDLE, ("sleep", 0.5),
    "M5", "G0 X%g" % -M5_IDLE_MM, "G0 Y1",
    WAIT_IDLE, "G90", "M2", ("sleep", 1.0),
]


def fail(msg):
    print("FAIL: %s" % msg)
    sys.exit(1)


def send_line(sock, line, log):
    sock.sendall((line + "\n").encode())
    while True:
        r = read_avail(sock, log, 5.0, until=("ok", "error"))
        if r is None:
            fail("no ok/error for %r" % line)
        if r == "error":
            fail("error response to %r" % line)
        return


def read_avail(sock, log, timeout, until=None):
    end = time.time() + timeout
    buf = b""
    while time.time() < end:
        sock.settimeout(max(0.05, end - time.time()))
        try:
            data = sock.recv(4096)
        except socket.timeout:
            data = b""
        if data:
            buf += data
            log.append(data.decode(errors="replace"))
            if until:
                for token in until:
                    if re.search(r"^%s\b" % token, buf.decode(errors="replace"), re.M):
                        return token
        elif until is None:
            return None
    return None


def wait_idle(sock, log):
    for _ in range(100):
        sock.sendall(b"?")
        read_avail(sock, log, 0.3)
        if re.search(r"<Idle", "".join(log[-3:])):
            return
        time.sleep(0.2)
    fail("controller never returned to Idle")


def wait_state(sock, log, prefix, timeout=5.0):
    """Poll '?' until the state word starts with prefix (e.g. 'Hold:0')."""
    end = time.time() + timeout
    while time.time() < end:
        sock.sendall(b"?")
        read_avail(sock, log, 0.3)
        m = re.findall(r"<([A-Za-z]+(?::\d)?)", "".join(log[-3:]))
        if m and m[-1].startswith(prefix):
            return
        time.sleep(0.1)
    fail("controller never reached %s" % prefix)


# The published verdict is clean unless a session sets this: then it is
# the engine's fail tier (hold, fire blocked, no resume), what an airflow
# fault publishes. A ("verdict", "hold") step sets it, ("verdict",
# "clean") clears it.
VERDICT_HOLD = threading.Event()


def publish_verdicts(path, stop):
    """Publish a fresh cooling verdict every 0.5 s (the arm flow refuses
    without one; freshness window is 2 s), clean unless VERDICT_HOLD is
    set. Same-host monotonic clock, atomic rename so the reader never
    sees a torn file. "armed" is the engine's acknowledgment that it has
    taken the controller's armed window; the arm waits for it, so a
    stand-in engine that means to let jobs run must assert it."""
    while not stop.is_set():
        hold = VERDICT_HOLD.is_set()
        body = ('{"ts_mono":%.3f,"fire_ok":%s,"hold":%s,'
                '"resume_ok":%s,"armed":true,"reason":"%s"}'
                % (time.clock_gettime(time.CLOCK_MONOTONIC),
                   "false" if hold else "true", "true" if hold else "false",
                   "false" if hold else "true",
                   "harness: airflow fault" if hold else ""))
        tmp = path + ".tmp"
        with open(tmp, "w") as f:
            f.write(body)
        os.replace(tmp, path)
        stop.wait(0.5)


def run_session(name, steps, conf=None, workdir=None, keep=False,
                arm_required=True):
    """Launch the controller, run the job steps, return the dump bytes.

    Pass workdir + keep to chain launches over one settings file: the
    core precomputes the spindle PWM mapping once, when the spindle is
    enabled, so a $35 written at runtime only takes effect on the next
    controller start."""
    if workdir is None:
        workdir = tempfile.mkdtemp(prefix="laser-test-")
    dump = os.path.join(workdir, "stream.bin")
    verdict = os.path.join(workdir, "cooling.state")
    env = dict(os.environ, GFSINK_DUMP=dump, GF_VERDICT_FILE=verdict,
               FFLOG_STDERR="1")
    env.pop("GFSINK", None)
    if conf is not None:
        conf_path = os.path.join(workdir, "forgefirm.conf")
        with open(conf_path, "w") as f:
            f.write(conf)
        env["GFHOME_CONF"] = conf_path

    stop = threading.Event()
    VERDICT_HOLD.clear()
    pub = threading.Thread(target=publish_verdicts, args=(verdict, stop), daemon=True)
    pub.start()

    proc = subprocess.Popen([BIN, "-p", str(PORT)], cwd=workdir, env=env,
                            stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
    try:
        sock = None
        for _ in range(50):
            try:
                sock = socket.create_connection(("127.0.0.1", PORT), timeout=1)
                break
            except OSError:
                time.sleep(0.1)
        if sock is None:
            err = b""
            if proc.poll() is not None:
                err = proc.stderr.read() or b""
            fail("[%s] cannot connect to the controller (exit=%s)\n%s"
                 % (name, proc.poll(), err.decode(errors="replace")))

        log = []
        read_avail(sock, log, 0.5)          # banner / hello

        for step in steps:
            if step == WAIT_IDLE:
                wait_idle(sock, log)
            elif isinstance(step, tuple) and step[0] == "sleep":
                time.sleep(step[1])
            elif isinstance(step, tuple) and step[0] == "rt":
                sock.sendall(step[1])           # a realtime character: no ok follows
            elif isinstance(step, tuple) and step[0] == "wait_state":
                wait_state(sock, log, step[1])
            elif isinstance(step, tuple) and step[0] == "verdict":
                if step[1] == "hold":
                    VERDICT_HOLD.set()
                else:
                    VERDICT_HOLD.clear()
            else:
                send_line(sock, step, log)

        # Wait for the motion to play out on the wall clock (the shipper
        # is wall-paced), then for the Idle report.
        wait_idle(sock, log)
        time.sleep(1.0)                     # let the shipper drain the tail
        text = "".join(log)

        run_session.text = text
        if arm_required and "laser armed" not in text:
            fail("[%s] no 'laser armed' message (arming flow did not run)" % name)

        sock.close()
    finally:
        proc.send_signal(signal.SIGINT)
        try:
            proc.wait(5)
        except subprocess.TimeoutExpired:
            proc.kill()
        stop.set()
        pub.join(2)
        VERDICT_HOLD.clear()

    data = open(dump, "rb").read()
    if not data and arm_required:
        fail("[%s] empty stream dump" % name)
    if not keep:
        shutil.rmtree(workdir, ignore_errors=True)
    return data


def tick_bytes(data):
    """The stream with power bytes stripped (tick bytes only)."""
    return bytes(b for b in data if not b & 0x80)


def check_fire_gaps(name, data):
    """Rule 8: no stepless run carrying FIRE longer than the limit."""
    run = 0
    worst = 0
    for tick, b in enumerate(tick_bytes(data)):
        if b & 0x10 and not b & 0x25:       # FIRE, no X/Y/Z step
            run += 1
            worst = max(worst, run)
            if run >= FIRE_GAP_LIMIT_TICKS:
                fail("[%s] FIRE carried across a %d-tick zero-step gap "
                     "ending at tick %d (stationary dwell burn)"
                     % (name, run, tick))
        else:
            run = 0
    return worst


def check_z_move(name, data):
    """The lens in the stream: a job's Z move steps it, up with the
    direction bit set, down with it clear, the count the scale gives."""
    ticks = tick_bytes(data)
    up = sum(1 for b in ticks if b & 0x20 and b & 0x40)
    down = sum(1 for b in ticks if b & 0x20 and not b & 0x40)
    if up != 3 or down != 3:
        fail("[%s] Z steps up %d, down %d (expected 3 and 3 for 1 mm at "
             "2.922 half-steps per mm)" % (name, up, down))
    print("PASS [%s]: a 1 mm Z move steps the lens %d up and %d back" % (name, up, down))


def check_termination(name, data):
    """Rule 7: the stream's final tick byte must carry FIRE clear."""
    ticks = tick_bytes(data)
    if not ticks:
        fail("[%s] no tick bytes in the stream" % name)
    if ticks[-1] & 0x10:
        fail("[%s] stream ends with FIRE set (0x%02x) - termination "
             "rule violated, relies on the end-of-data backstop"
             % (name, ticks[-1]))


def check_m4_job(data):
    """Rules 1-6 on the original M4 job."""
    if not data[0] & 0x80:
        fail("stream does not lead with a power byte (first byte 0x%02x)" % data[0])

    prev_power = False
    cur_power = 0
    fire_ticks = []                 # (tick_index, power_at_that_tick)
    x_pos = 0
    x_min = x_max = 0
    tick = 0
    first_fire_power = None
    for b in data:
        if b & 0x80:
            if prev_power:
                fail("consecutive power bytes at tick %d" % tick)
            prev_power = True
            cur_power = b & 0x7F
            continue
        prev_power = False
        if b & 0x10:
            if first_fire_power is None:
                first_fire_power = cur_power
            fire_ticks.append((tick, cur_power))
        if b & 0x01:
            x_pos += -1 if b & 0x02 else 1
            x_min = min(x_min, x_pos)
            x_max = max(x_max, x_pos)
        if b & 0x24:
            fail("unexpected Y/Z step at tick %d (byte 0x%02x)" % (tick, b))
        tick += 1

    if not fire_ticks:
        fail("no FIRE bits in the stream")
    if first_fire_power == 0:
        fail("first FIRE bit rides duty 0 (power-before-fire violated)")

    powers = sorted(set(p for _, p in fire_ticks))
    if powers[-1] != PWM_PERIOD:
        fail("S1000 did not reach duty %d (max %d)" % (PWM_PERIOD, powers[-1]))
    want = duty_for(500)
    if not any(abs(p - want) <= 2 for p in powers):
        fail("S500 plateau (~%d) not seen (powers %s)" % (want, powers[:20]))
    if powers[0] < PWM_MIN:
        fail("duty %d under FIRE is below the $35 floor of %d: M4's ramp is "
             "commanding power the tube cannot lase at" % (powers[0], PWM_MIN))

    expect_peak = round(10 * STEPS_PER_MM)
    if abs(x_max - expect_peak) > 2:
        fail("X peak %d steps, expected ~%d" % (x_max, expect_peak))
    if x_pos != 0:
        fail("X net %d steps after return to 0" % x_pos)
    if x_min < 0:
        fail("X went negative (min %d)" % x_min)

    last_fire = fire_ticks[-1][0]
    tail_steps = 0
    tick = 0
    for b in data:
        if b & 0x80:
            continue
        if tick > last_fire and b & 0x01:
            tail_steps += 1
        tick += 1
    if tail_steps < 400:
        fail("only %d fire-free steps after the last FIRE bit - G0 return not dark" % tail_steps)

    return fire_ticks, powers, x_max, tail_steps


def check_power_ladder(name, data, expect):
    """Rule 10: every FIRE tick rides the duty commanded for its rung."""
    cur = None
    order = []                      # duties in the order they carry FIRE
    counts = {}
    for b in data:
        if b & 0x80:
            cur = b & 0x7F
            continue
        if b & 0x10:
            if cur is None:
                fail("[%s] FIRE bit ahead of any power byte" % name)
            counts[cur] = counts.get(cur, 0) + 1
            if not order or order[-1] != cur:
                order.append(cur)

    stray = sorted(d for d in counts if d not in expect)
    if stray:
        fail("[%s] FIRE rode uncommanded duty %s (commanded %s): power the "
             "job never asked for is uncommanded energy"
             % (name, stray, list(expect)))
    if order != list(expect):
        fail("[%s] duty sequence under FIRE was %s, expected %s"
             % (name, order, list(expect)))

    # Equal-length rungs at one feed burn equal numbers of fire ticks.
    # A rung whose opening ticks carry the previous rung's duty shows up
    # here as a surplus on one duty and a deficit on the next.
    lo, hi = min(counts.values()), max(counts.values())
    if hi > lo * 1.05:
        fail("[%s] fire ticks per rung uneven (%d..%d, %s): a rung is "
             "firing at its neighbor's duty" % (name, lo, hi, counts))
    return counts


def fire_spans(ticks, gap=500):
    """Tick spans carrying fire, split on dark gaps (the G0 between
    rungs). Within a rung the model's own dark stretches are at most a
    couple of base periods, far below the split."""
    spans = []
    start = last = None
    for i, b in enumerate(ticks):
        if b & 0x10:
            if start is None:
                start = i
            elif i - last > gap:
                spans.append((start, last + 1))
                start = i
            last = i
    if start is not None:
        spans.append((start, last + 1))
    return spans


def check_density(name, data, levels, period, min_ticks):
    """Rules 11-12: pinned duty, and density per rung matching the level."""
    # A power byte still leads every kernel run - the run start resets the
    # hardware duty - but under this model it only ever carries full duty:
    # the level rides the FIRE bits, never PWMSAR.
    powers = [b & 0x7F for b in data if b & 0x80]
    if not powers or set(powers) != {PWM_PERIOD}:
        fail("[%s] density mode shipped power bytes %s; every one must be "
             "full duty, or a level reached PWMSAR" % (name, sorted(set(powers))))

    ticks = tick_bytes(data)
    spans = fire_spans(ticks)
    if len(spans) != len(levels):
        fail("[%s] %d fire spans, expected one per rung (%d): %s"
             % (name, len(spans), len(levels), spans[:8]))

    out = []
    for (a, b), level in zip(spans, levels):
        seg = ticks[a:b]
        got = sum(1 for t in seg if t & 0x10) / float(len(seg))
        want = level / float(PWM_PERIOD)
        out.append((level, round(got, 4)))
        # A span is clipped to whole ticks, not whole periods, so allow a
        # little slack at the edges; the accumulator carries the rest.
        if abs(got - want) > max(0.01, want * 0.06):
            fail("[%s] level %d rendered density %.4f, expected %.4f"
                 % (name, level, got, want))
        # Burst lengths inside the span. The last one can be clipped by
        # the core turning fire off mid-burst, so it is not held to the
        # minimum; every other burst is a whole pulse the model chose.
        runs, run = [], 0
        for t in seg:
            if t & 0x10:
                run += 1
            elif run:
                runs.append(run)
                run = 0
        if run:
            runs.append(run)
        if not runs:
            fail("[%s] level %d produced no bursts at all" % (name, level))
        if max(runs) > period:
            fail("[%s] level %d burst of %d ticks exceeds the %d-tick base "
                 "period" % (name, level, max(runs), period))
        short = [r for r in runs[:-1] if r < min_ticks]
        if short:
            fail("[%s] level %d emitted %d burst(s) below the %d-tick minimum "
                 "(shortest %d): a stub too brief for the supply to strike"
                 % (name, level, len(short), min_ticks, min(short)))
    return out


def check_mask(analog, density):
    """Rule 13: same motion, and density fire is a subset of analog fire."""
    ta, td = tick_bytes(analog), tick_bytes(density)
    if len(ta) != len(td):
        fail("[mask] tick counts differ (analog %d, density %d): the two runs "
             "are not the same motion" % (len(ta), len(td)))
    for i, (a, b) in enumerate(zip(ta, td)):
        if (a & ~0x10) != (b & ~0x10):
            fail("[mask] motion differs at tick %d (analog 0x%02x, density "
                 "0x%02x)" % (i, a, b))
    stray = [i for i, (a, b) in enumerate(zip(ta, td)) if (b & 0x10) and not (a & 0x10)]
    if stray:
        fail("[mask] density fired %d tick(s) the core never commanded, first "
             "at %d - the model is acting as a source of emission, not a mask"
             % (len(stray), stray[0]))
    return sum(1 for b in td if b & 0x10), sum(1 for a in ta if a & 0x10)


def count_fire(data):
    return sum(1 for b in tick_bytes(data) if b & 0x10)


def check_cut_spans(name, ticks, n, cut_ticks, what):
    """Exactly n fire spans, each one cutting move long, none stepping
    at a rapid's rate: FIRE rode nothing but the G1s."""
    spans = fire_spans(ticks)
    if len(spans) != n:
        fail("[%s] %d fire spans, expected exactly %d (%s) (spans %s)"
             % (name, len(spans), n, what, spans))
    for s0, s1 in spans:
        if not 0.8 * cut_ticks <= s1 - s0 <= 1.25 * cut_ticks:
            fail("[%s] fire span of %d ticks, expected ~%d (one G1): FIRE "
                 "carried into the move after it" % (name, s1 - s0, cut_ticks))
        # A G1 at F600 steps once per ~53 ticks; a rapid at 200 mm/s
        # steps every ~2.6. Any 100-tick window under FIRE with more
        # than a handful of steps is a rapid being cut.
        worst = 0
        for i in range(s0, max(s0 + 1, s1 - 100), 50):
            worst = max(worst, sum(1 for b in ticks[i:i + 100]
                                   if (b & 0x10) and (b & 0x25)))
        if worst > 8:
            fail("[%s] %d steps in a 100-tick window under FIRE: a rapid "
                 "was cut" % (name, worst))
    return spans


def main():
    # --- session A: M4 dynamic power, rules 1-6 + 7-8 -------------------
    data = run_session("m4", JOB_M4, conf=ANALOG_CONF)
    fire_ticks, powers, x_max, tail_steps = check_m4_job(data)
    check_termination("m4", data)

    # --- session Z: the lens in the stream --------------------------------
    zdata = run_session("z", JOB_Z, arm_required=False)
    check_z_move("z", zdata)
    gap_a = check_fire_gaps("m4", data)
    print("PASS [m4]: %d bytes, %d power bytes, %d fire ticks, powers %s, "
          "X peak %d steps net 0, %d dark return steps, max fire gap %d"
          % (len(data), sum(1 for b in data if b & 0x80), len(fire_ticks),
             powers, x_max, tail_steps, gap_a))

    # --- session B: M3 constant power to stream end, rule 7 -------------
    data = run_session("m3-term", JOB_M3_TERM, conf=ANALOG_CONF)
    if not count_fire(data):
        fail("[m3-term] no FIRE bits in the stream")
    check_termination("m3-term", data)
    gap_b = check_fire_gaps("m3-term", data)
    print("PASS [m3-term]: %d bytes, %d fire ticks end dark, max fire gap %d"
          % (len(data), count_fire(data), gap_b))

    # --- session C: cycle churn, rules 8-9 ------------------------------
    data = run_session("churn", JOB_CHURN, conf=ANALOG_CONF)
    if not count_fire(data):
        fail("[churn] no FIRE bits in the stream")
    check_termination("churn", data)
    gap_c = check_fire_gaps("churn", data)
    print("PASS [churn]: %d bytes, %d fire ticks, max fire gap %d"
          % (len(data), count_fire(data), gap_c))

    # --- session D: power ladder, rule 10 -------------------------------
    data = run_session("ladder", JOB_LADDER, conf=ANALOG_CONF)
    counts = check_power_ladder("ladder", data, LADDER_DUTY)
    check_termination("ladder", data)
    gap_d = check_fire_gaps("ladder", data)
    print("PASS [ladder]: %d bytes, duties %s fire ticks %s, max fire gap %d"
          % (len(data), list(LADDER_DUTY),
             [counts[d] for d in LADDER_DUTY], gap_d))

    # --- session E: the same ladder under the density model -------------
    # Unfloored through the config key (laser_floor_density = 0), which
    # the arm loads into $35.
    dens = run_session("density", JOB_LADDER, conf=DENSITY_CONF)
    rendered = check_density("density", dens, DENSITY_LEVEL, DENSITY_PERIOD,
                             DENSITY_MIN_TICKS)
    check_termination("density", dens)
    if "laser armed (density, floor 0 %, curve off)" not in run_session.text:
        fail("[density] the arm did not select the density model at floor 0")
    print("PASS [density]: %d bytes, %d power bytes all at full duty, "
          "level->density %s"
          % (len(dens), sum(1 for b in dens if b & 0x80), rendered))

    # --- rule 13: the model masks, it never sources ---------------------
    d_fire, a_fire = check_mask(data, dens)
    print("PASS [mask]: identical motion grid, %d density fire ticks all "
          "inside the %d the core commanded" % (d_fire, a_fire))

    # --- session F: full level under the model is continuous fire -------
    full = run_session("density-full", JOB_M3_TERM, conf=DENSITY_CONF)
    ticks = tick_bytes(full)
    spans = fire_spans(ticks)
    if not spans:
        fail("[density-full] no FIRE bits in the stream")
    a, b = spans[0]
    got = sum(1 for t in ticks[a:b] if t & 0x10) / float(b - a)
    if got != 1.0:
        fail("[density-full] S1000 rendered density %.4f, expected 1.0" % got)
    check_termination("density-full", full)
    print("PASS [density-full]: S1000 -> density 1.0000 over %d ticks, ends dark"
          % (b - a))

    # --- session G: churn under the model (rules 7-9 still hold) --------
    ch = run_session("density-churn", JOB_CHURN, conf=DENSITY_CONF)
    if not count_fire(ch):
        fail("[density-churn] no FIRE bits in the stream")
    check_termination("density-churn", ch)
    gap_e = check_fire_gaps("density-churn", ch)
    print("PASS [density-churn]: %d bytes, %d fire ticks, max fire gap %d"
          % (len(ch), count_fire(ch), gap_e))

    # --- session H: a level change inside a run costs no byte -----------
    lv_a = run_session("levels-analog", JOB_LEVELS, conf=ANALOG_CONF)
    lv_d = run_session("levels-density", JOB_LEVELS, conf=DENSITY_CONF)
    pa = [b & 0x7F for b in lv_a if b & 0x80]
    pd = [b & 0x7F for b in lv_d if b & 0x80]
    if len([d for d in set(pa) if d]) < 3:
        fail("[levels] the analog run shipped duties %s: fewer than the three "
             "commanded levels, so the job is not exercising in-run changes"
             % sorted(set(pa)))
    if set(pd) != {PWM_PERIOD}:
        fail("[levels] density shipped a level as duty: %s" % sorted(set(pd)))
    if len(pd) >= len(pa):
        fail("[levels] density shipped %d power bytes against analog's %d - "
             "the level changes are still costing stream bytes" % (len(pd), len(pa)))
    print("PASS [levels]: analog %d power bytes %s, density %d at full duty"
          % (len(pa), sorted(set(pa)), len(pd)))

    # --- session I: a level set while idle still cuts (rule 14) ---------
    idle_s = run_session("idle-s", JOB_IDLE_S, conf=ANALOG_CONF)
    fire_by_duty = {}
    cur = None
    for b in idle_s:
        if b & 0x80:
            cur = b & 0x7F
        elif b & 0x10:
            fire_by_duty[cur] = fire_by_duty.get(cur, 0) + 1
    want_ticks = IDLE_S_MM / (IDLE_S_FEED / 60.0) * 28160
    for level in IDLE_S_LEVELS:
        duty = duty_for(level)
        got = fire_by_duty.get(duty, 0)
        if got < want_ticks * 0.9:
            fail("[idle-s] S%d (duty %d) fired %d ticks, expected ~%d: a level "
                 "set while the stream was idle was dropped and the move ran "
                 "dark or at a stale duty (all: %s)"
                 % (level, duty, got, want_ticks, fire_by_duty))
    check_termination("idle-s", idle_s)
    print("PASS [idle-s]: standalone S across idle gaps -> fire ticks per duty %s"
          % {duty_for(l): fire_by_duty[duty_for(l)] for l in IDLE_S_LEVELS})

    # --- session J: M5 executed while idle darkens the next run (rule 16) ---
    for model, conf in (("analog", ANALOG_CONF), ("density", DENSITY_CONF)):
        name = "m5-idle-" + model
        data = run_session(name, JOB_M5_IDLE, conf=conf)
        spans = check_cut_spans(name, tick_bytes(data), 2, M5_IDLE_TICKS,
                                "the two G1 moves: FIRE rode a rapid after M5, "
                                "or the bare G0 sent with the spindle off")
        check_termination(name, data)
        print("PASS [%s]: M5 at idle -> the rapids after it and a bare G0 ship "
              "dark; 2 fire spans of %s ticks"
              % (name, [s1 - s0 for s0, s1 in spans]))

    # --- session K: the next job, at the same level, fires (rule 17) ---
    for model, conf in (("analog", ANALOG_CONF), ("density", DENSITY_CONF)):
        name = "next-job-" + model
        data = run_session(name, JOB_NEXT + JOB_NEXT, conf=conf)
        text = run_session.text
        if text.count("laser armed") != 2 or text.count("laser disarmed") != 2:
            fail("[%s] expected two armed windows closed by M2 (armed %d, "
                 "disarmed %d)" % (name, text.count("laser armed"),
                                   text.count("laser disarmed")))
        spans = check_cut_spans(name, tick_bytes(data), 2, M5_IDLE_TICKS,
                                "one G1 per job: the second job's M3 at the "
                                "first job's S lit nothing, or a rapid fired")
        check_termination(name, data)
        print("PASS [%s]: the next job's M3 at the previous job's S fires its "
              "G1; 2 fire spans of %s ticks"
              % (name, [s1 - s0 for s0, s1 in spans]))

    # --- rule 18: the floor is derived from the key, never typed --------
    # The same ladder with a $35=0 typed ahead of it, under the shipped
    # density default (no floor key): the arm loads the board's floor and
    # every rung renders through it.
    floored = run_session("floor-derived", JOB_DENSITY, conf=DENSITY_CONF_FLOORED)
    expect_levels = tuple(duty_for(x) for x in LADDER_S)
    check_density("floor-derived", floored, expect_levels, DENSITY_PERIOD,
                  DENSITY_MIN_TICKS)
    if "laser armed (density, floor %g %%, curve off)" % PWM_MIN_PCT not in run_session.text:
        fail("[floor-derived] the arm report does not name the derived floor "
             "(text: %r)" % run_session.text[-400:])
    print("PASS [floor-derived]: a typed $35=0 is overwritten at the arm; the "
          "ladder renders through the %g %% floor key, levels %s"
          % (PWM_MIN_PCT, list(expect_levels)))

    # --- rule 20: the corner rolloff starves the accel head -------------
    # One long M4 line from rest under the curve, at gamma 1 and the
    # default 2. The accelerate-in head runs velocity-scaled; its
    # rendered density must drop with the exponent while the cruise
    # middle stays put.
    # F6000 = 100 mm/s: the accel from rest lasts ~143 ms (~4000 ticks),
    # so the first 2000 ticks are genuinely velocity-scaled.
    JOB_M4_LONG = ["G91", "G21", "M4 S1000", "G1 X60 F6000", "M5"]
    head_ticks = 2000
    dens_head = {}
    dens_mid = {}
    for gname, gconf in (("g1", "laser_corner_gamma = 1\n"), ("g2", "")):
        data = run_session("rolloff-" + gname, JOB_M4_LONG,
                           conf=DENSITY_CONF_CURVED + gconf)
        ticks = tick_bytes(data)
        spans = fire_spans(ticks)
        if len(spans) != 1:
            fail("[rolloff-%s] %d fire spans, expected 1" % (gname, len(spans)))
        a, b = spans[0]
        seg = ticks[a:b]
        head = seg[:head_ticks]
        mid_a = len(seg) // 2 - 2000
        mid = seg[mid_a:mid_a + 4000]
        dens_head[gname] = sum(1 for t in head if t & 0x10) / float(len(head))
        dens_mid[gname] = sum(1 for t in mid if t & 0x10) / float(len(mid))
    if not dens_head["g2"] < dens_head["g1"] - 0.02:
        fail("[rolloff] gamma 2 does not starve the accel head (g1 %.3f, g2 %.3f)"
             % (dens_head["g1"], dens_head["g2"]))
    if abs(dens_mid["g2"] - dens_mid["g1"]) > 0.02:
        fail("[rolloff] gamma changed the cruise density (g1 %.3f, g2 %.3f): it "
             "must shape only the rolloff" % (dens_mid["g1"], dens_mid["g2"]))
    print("PASS [rolloff]: accel-head density %.3f at gamma 1 -> %.3f at the "
          "default 2; cruise %.3f alike" % (dens_head["g1"], dens_head["g2"],
                                            dens_mid["g1"]))

    # --- rule 19: the dose curve bends S onto delivered light -----------
    cur = run_session("curve", JOB_CURVE, conf=DENSITY_CONF_CURVED)
    if "curve bench-default" not in run_session.text:
        fail("[curve] the arm does not name the bench-default curve (text: %r)"
             % run_session.text[-300:])
    floor_frac = PWM_MIN / float(PWM_PERIOD)
    expect = []
    for s_val in CURVE_S:
        d = curve_density_for(s_val)
        expect.append(min(1.0, max(d, floor_frac)))
    ticks = tick_bytes(cur)
    spans = fire_spans(ticks)
    if len(spans) != len(CURVE_S):
        fail("[curve] %d fire spans, expected %d" % (len(spans), len(CURVE_S)))
    got = []
    for (a, b) in spans:
        seg = ticks[a:b]
        got.append(sum(1 for t in seg if t & 0x10) / float(len(seg)))
    for g, w, s_val in zip(got, expect, CURVE_S):
        if abs(g - w) > max(0.012, w * 0.06):
            fail("[curve] S%d rendered density %.4f, expected %.4f through the "
                 "bench-default curve" % (s_val, g, w))
    if not all(b > a for a, b in zip(got, got[1:])):
        fail("[curve] densities not monotonic: %s" % [round(g, 4) for g in got])
    check_termination("curve", cur)
    print("PASS [curve]: S %s -> densities %s through the bench-default curve "
          "(floored at %.3f)" % (list(CURVE_S), [round(g, 3) for g in got], floor_frac))

    # --- rule 21: a feed hold leaves no dark ground, in either mode -----
    # One long line at 100 mm/s, held mid-move and resumed. The planned
    # deceleration runs lit (M4 velocity-scaled, M3 constant), the
    # stationary stretch is dark, and the acceleration out of the hold is
    # lit from its first step: a pause is a sharp corner in time. The
    # first-window check is what pins the M3 resume, which once ran dark
    # for a segment buffer because the segments prepped while held
    # carried no spindle update.
    HOLD_WIN = 704                                  # 25 ms at 28160 Hz
    HOLD_EDGE = 120                                 # one pulse straddles each edge
    for mode, job in (("m4", ["G90", "G21", "M4 S0", "G1 X150 F6000 S500"]),
                      ("m3", ["G90", "G21", "M3 S500", "G1 X150 F6000"])):
        steps = job + [("sleep", 0.7), ("rt", b"!"), ("wait_state", "Hold:0"),
                       ("sleep", 0.5), ("rt", b"~"), WAIT_IDLE, "M5"]
        data = run_session("hold-" + mode, steps, conf=DENSITY_CONF_FLOORED)
        check_fire_gaps("hold-" + mode, data)
        ticks = tick_bytes(data)
        step = [1 if t & 0x05 else 0 for t in ticks]
        fire = [1 if t & 0x10 else 0 for t in ticks]
        first = step.index(1)
        last = len(step) - 1 - step[::-1].index(1)
        best, run = (0, 0), 0
        for i in range(first, last + 1):
            if step[i]:
                if run > best[0]:
                    best = (run, i - run)
                run = 0
            else:
                run += 1
        dlen, dstart = best
        dend = dstart + dlen
        if dlen < 2000:
            fail("[hold-%s] no hold in the stream (longest stepless run %d ticks)"
                 % (mode, dlen))
        decel = sum(fire[dstart - HOLD_WIN:dstart])
        dwell = sum(fire[dstart + HOLD_EDGE:dend - HOLD_EDGE])
        accel = sum(fire[dend:dend + HOLD_WIN])
        if decel < 50:
            fail("[hold-%s] the deceleration into the hold ran dark (%d fire ticks in "
                 "its last 25 ms)" % (mode, decel))
        if dwell:
            fail("[hold-%s] FIRE while held: %d fire ticks in the stationary stretch"
                 % (mode, dwell))
        if accel < 50:
            fail("[hold-%s] the resume ran dark (%d fire ticks in the 25 ms after the "
                 "first step)" % (mode, accel))
        print("PASS [hold-%s]: lit into the hold (%d fire ticks), dark while held "
              "(%d ticks), lit from the first step out (%d fire ticks)"
              % (mode, decel, dlen, accel))

    # --- rule 24: a hold verdict is held again after a resume -----------
    # One long line at 50 mm/s. Mid-move the engine's verdict goes to its
    # fail tier (hold, fire blocked, no resume: an airflow fault) and the
    # client takes the feed hold. A ~ then resumes the job under the
    # standing verdict, which is what a button press or a sender does;
    # the client must hold it again within its poll, saying so, and the
    # stretch it moved in between ships dark. The clean verdict then
    # resumes the hold the client took, and the rest of the line cuts lit.
    TICK_HZ = 28160
    steps = ["G90", "G21", "M3 S500", "G1 X150 F3000", ("sleep", 0.7),
             ("verdict", "hold"), ("wait_state", "Hold:0"), ("sleep", 0.5),
             ("rt", b"~"), ("sleep", 1.2), ("wait_state", "Hold:0"),
             ("sleep", 0.5), ("verdict", "clean"), WAIT_IDLE, "M5"]
    data = run_session("verdict-rehold", steps, conf=DENSITY_CONF_FLOORED)
    text = run_session.text
    if "held again" not in text:
        fail("[verdict-rehold] the client did not say it held the job again")
    if "resuming" not in text:
        fail("[verdict-rehold] the client did not resume its own hold once the verdict cleared")
    ticks = tick_bytes(data)
    step = [1 if t & 0x05 else 0 for t in ticks]
    fire = [1 if t & 0x10 else 0 for t in ticks]
    first = step.index(1)
    last = len(step) - 1 - step[::-1].index(1)
    holds, run = [], 0                              # the stationary stretches inside the motion
    for i in range(first, last + 1):
        if step[i]:
            if run >= 2000:
                holds.append((i - run, i))
            run = 0
        else:
            run += 1
    if len(holds) != 2:
        fail("[verdict-rehold] expected two holds in the stream, found %d: %s"
             % (len(holds), holds))
    (_h1s, h1e), (h2s, h2e) = holds
    between = sum(fire[h1e:h2s])
    if between:
        fail("[verdict-rehold] FIRE while resumed under the hold verdict: %d fire ticks "
             "between the holds" % between)
    if h2s - h1e > TICK_HZ:
        fail("[verdict-rehold] the second hold came late: %d ticks (%.2f s) of motion under "
             "the verdict" % (h2s - h1e, (h2s - h1e) / float(TICK_HZ)))
    lit_after = sum(fire[h2e:last + 1])
    if lit_after < 50:
        fail("[verdict-rehold] the resume after the clean verdict ran dark (%d fire ticks)"
             % lit_after)
    print("PASS [verdict-rehold]: held, resumed dark for %d ticks (%.2f s), held again, "
          "lit after the clear (%d fire ticks)"
          % (h2s - h1e, (h2s - h1e) / float(TICK_HZ), lit_after))

    # --- rule 22: a jog never fires, whatever the modal spindle says ----
    # The arm flow runs on the M3 (window open), the modal spindle is on
    # at S1000, and the jogs come from Idle: exactly what a sender's Fire
    # button plus its Move panel sends. Every jog tick ships dark, and the
    # cut after them is lit, from a stream state the jogs did not disturb.
    JOB_JOG = ["G91", "G21", "M3 S1000", WAIT_IDLE,
               "$J=G91X10F1200", WAIT_IDLE, "$J=G91X-10F1200", WAIT_IDLE,
               "G1 X10 F1200", WAIT_IDLE, "M5"]
    data = run_session("jog-dark", JOB_JOG)
    ticks = tick_bytes(data)
    # Runs sit back to back in the dump, so the three moves are told
    # apart by their steps: each is 10 mm, and the cut is the last third.
    total = sum(1 for t in ticks if t & 0x01)
    if abs(total - 3 * 533) > 12:
        fail("[jog-dark] %d X steps, expected about %d (three 10 mm moves)" % (total, 3 * 533))
    cut_from = 2 * (total // 3) - 2              # a fire tick may lead the cut's first step
    jog_fire = cut_fire = steps = 0
    for t in ticks:
        if t & 0x01:
            steps += 1
        if t & 0x10:
            if steps < cut_from:
                jog_fire += 1
            else:
                cut_fire += 1
    if jog_fire:
        fail("[jog-dark] %d FIRE ticks inside the jogs: a jog fired at the modal S" % jog_fire)
    if cut_fire < 100:
        fail("[jog-dark] the cut after the jogs ran dark (%d fire ticks)" % cut_fire)
    print("PASS [jog-dark]: two jogs under M3 S1000 shipped dark (%d fire ticks), the "
          "cut after them lit (%d)" % (jog_fire, cut_fire))

    # --- rule 23: the rolloff shapes against the block's own S ----------
    # Under the default gamma 2, a second cut at S1000 queued behind the
    # S300 cut must not change what the S300 cruise renders: the ratio the
    # rolloff bends is the segment's own velocity ratio, never the newest
    # S over the executing one.
    ref = run_session("rolloff-ref", ["G91", "G21", "M4 S300", "G1 X30 F6000", "M5"],
                      conf=DENSITY_CONF_CURVED)
    two = run_session("rolloff-two", ["G91", "G21", "M4 S300", "G1 X30 F6000",
                                      "G1 X30 F6000 S1000", "M5"],
                      conf=DENSITY_CONF_CURVED)
    rt = tick_bytes(ref)
    rs = fire_spans(rt)
    if len(rs) != 1:
        fail("[rolloff-two] reference: %d fire spans, expected 1" % len(rs))
    a, b = rs[0]
    mid = rt[(a + b) // 2 - 1000:(a + b) // 2 + 1000]
    dens_ref = sum(1 for t in mid if t & 0x10) / float(len(mid))
    tt = tick_bytes(two)
    ts = fire_spans(tt)
    if len(ts) != 1:
        fail("[rolloff-two] %d fire spans, expected 1 (the two cuts join)" % len(ts))
    a, b = ts[0]
    q = a + (b - a) // 4                                  # the first cut's cruise
    first = tt[q - 1000:q + 1000]
    dens_first = sum(1 for t in first if t & 0x10) / float(len(first))
    if abs(dens_first - dens_ref) > 0.02:
        fail("[rolloff-two] the S300 cruise renders %.3f with S1000 queued behind it, "
             "%.3f alone: the rolloff shaped it against the parser's S" % (dens_first, dens_ref))
    print("PASS [rolloff-two]: the S300 cruise renders %.3f with S1000 queued behind it, "
          "%.3f alone" % (dens_first, dens_ref))

    print("PASS: all stream emission rules hold")


if __name__ == "__main__":
    main()
