#!/usr/bin/env python3
"""Live-fire bench drills - Phases 4, 5, 6. Runs on the board (the bench
page) or from a LAN host, against grblHAL over TCP (port 23) and
forgectrl over HTTP (:80, FORGECTRL_PORT overrides); the machine is GF_HOST, default 127.0.0.1.
LIVE LASER: the operator must be armed with eye protection, a fire
watch, an extinguisher, and the exhaust running. Every drill waits for
the operator to press the physical arm button before the machine fires;
nothing here defeats that gate.

Usage: live_fire_drills.py <drill> [arg] [F]  (ircut/pthresh take S,
       dladder takes the base period in machine ticks, pcurve takes
       F, the line length and an optional rung list)

Drills (pass a name):
  witness   Phase 5 A-1/A-2/A-5: a short vector mark at S400. Samples
            forgectrl /status (emission_samples, lid_ir[], hv_current)
            and /cool/status (armed, fire_watch) at ~8 Hz through the
            job. PASS: emission_samples goes nonzero during the fire
            window and returns to 0; lid_ir peak recorded per channel
            vs the ambient baseline; hv_current range logged. Also
            asserts X-3: armed drops at Idle with the job close, not at
            +60 s.
  hold      Phase 4 G-10: arm + start a longer job, feed-hold mid-run,
            then hold. PASS: the disarm grace counts down in Hold and
            the window closes (armed -> false) without the job resuming.
  ircut     Lid-IR fire characterization at cutting power: a 30 mm
            square at S<power> (default 1000 = full) and F<feed>
            (default 300) on scrap, sampled like `witness`. Prints the
            per-channel peak delta over the ambient baseline and the
            engine's own "run telemetry" line is the record. Run it
            >= 3 times on representative material; the highest peaks
            are what the engine's cool_fire_q1/q2 alert and critical
            thresholds (absolute counts) must sit above.
              ircut [S] [F]             e.g. ircut 1000 300
  pthresh   Laser power-threshold ladder: one line per power level on
            scrap, climbing from 2 % to 30 % of full, at constant power
            (M3) so nothing scales the duty with velocity. The ladder
            separates two thresholds that are far apart: the discharge
            strikes at a duty well below the one the tube lases at, and
            the rungs between them show only a spot at the line start
            (the strike transient) with a dark line after it. The
            lowest rung that leaves a CONTINUOUS mark is the lasing
            threshold, and because $35 is a percent of full duty and
            the rungs are percents of $30 with $31 = 0, that rung's
            percent IS the $35 value. Requires $35 = 0 for the run: a
            floor already in place lifts every rung and hides both
            thresholds.
              pthresh [Smax] [F]        e.g. pthresh 1000 300
  dladder   Density ladder for the FIRE-bit dose model: one line per
            dose level on scrap, 5 % to 100 %, at constant power (M3)
            so nothing scales the dose with velocity. Under this model
            the duty is pinned at full and the level is carried by how
            many ticks of each base period fire, so a rung's burst is
            density x period - and the base period is the parameter the
            host cannot choose for you. Run it at 20, then 40, then 10
            on the same material: 20 ticks is 710 us, the factory's
            ~1.43 kHz, and 40 and 10 bracket it. Two questions the
            material answers: does mark depth track density linearly,
            and how short a burst still marks. Requires
            laser_power_model = density; reads $30/$31/$35/$36 and
            reports the mapping, so a run with a density floor set shows
            what a shipped machine would actually emit. Sets
            laser_pulse_ticks itself when run on the board.
              dladder [period] [F]      e.g. dladder 20 300
  pcurve    Laser performance-curve ladder, both instruments: one line
            per level at constant feed (default F600, 10 mm/s, 100 mm),
            M3 constant power, the laser off between rungs and a
            mid-ladder rung repeated at the end as the drift witness. Reads
            the HV current and the head thermopile (a scatter detector
            in the beam path upstream of the final mirror, so it sees
            the beam, not the material) straight from sysfs at ~25 Hz
            while the machine runs, brackets each rung on the
            controller's Run/Idle states, and reports per rung the
            current (mean, spread, max, CLIPPED at 1023), the thermopile
            delta over its laser-off baseline with its in-line drift,
            the digital flag's duty and the coolant temperature; then
            the normalized curve, a monotonicity check, a straight-line
            fit with its x-intercept as the measured threshold, and the
            repeat-rung comparison. Rungs follow laser_power_model:
            analog 16..100 % of duty (dense at the knee), density
            1..100 %; a comma list overrides. Records the actual level
            each S lands on from $30/$31/$35/$36; measuring the curve
            itself wants $35 = 0. JSON record (with the raw trace) in
            FORGETEST_BENCH_DATA, else /tmp. Runs on the board for the
            thermopile; from a host it falls back to /status (current
            only, no curve). Reaches FULL power for 10 s per line.
              pcurve [F] [len] [pcts]   e.g. pcurve 600 100 16,20,30,50,100
  m5dark    The rapids after an M5 ship dark: one 20 mm line at M3 S400,
            M5, a dwell, a rapid back over the line, a dwell, a rapid
            forward, a dwell, M2. Samples sysfs at 25 Hz (board only):
            PASS when the current shows exactly one discharge segment,
            reads dark after the M5, and laser_on_sampled never goes
            nonzero again after its first zero past the line. Prints the
            9 s after the line at 40 ms steps. The catalog's
            laser.m5-rapid-dark is its port.
  m4corner  M4 velocity-scaled power into corners and short segments:
            a corner-heavy vector pattern (a long line, 1.1 mm zigzag
            teeth, a 2 mm square, a 180 degree reversal, 0.5 mm teeth)
            at 30 %% under M4 at F2000, one pass under density. The
            floor is the guard: velocity brings the commanded power to
            the floor at every corner, and the model cannot go dark by
            construction - the operator confirms every commanded
            segment marks. One discharge window, dark after.
              m4corner [S] [F]   e.g. m4corner 300 2000
  m4feeds   B3, the density time base across feeds: under M4 density,
            one out-and-back 20 mm line pair per feed (default 1000 and
            4000 mm/min) at the same S, passes offset +Y, one armed
            run. M4 scales the commanded power with velocity inside a
            move, so each line should read evenly dark from its slow
            ends to its fast middle; the reversal point is where the
            factory's own compensation still let dose per mm rise
            ~1.8x. The operator compares evenness within each pass and
            the reversal darkness across the two feeds. Requires
            laser_power_model = density.
              m4feeds [S] [F1] [F2]   e.g. m4feeds 600 1000 4000
  xymode    The XY microstep mode under the laser: one out-and-back
            20 mm line pair per feed (default 1200 and 6000 mm/min) at
            the same S under M4 density, passes offset +Y, one armed
            run, the block shifted +X by an offset so the runs at 8, 16
            and 32 sit side by side for the operator's eye. Reports the
            mode, tick and ramp the kernel holds, the discharge window
            and the HV current per pass: with the laser ticks scaled to
            the tick in force, the current at cruise should read alike
            at every mode, and the operator compares the marks. Set the
            mode from the panel (xy_microsteps) before each run.
              xymode [S] [F1] [F2] [X offset mm]   e.g. xymode 400 1200 6000 25
  xycircle  The XY microstep mode under the laser, the operator's eye
            version: a dark rapid +X by an offset, then one full circle
            of the given diameter starting at its top (the center below
            the start in +Y) at S under M4 density, laser off, and a
            rapid back to the origin. One armed run; the runs at 8, 16
            and 32 use different offsets so the circles sit side by
            side. Reports the mode, tick and ramp, the discharge window
            and the HV current.
              xycircle [S] [F] [X offset mm] [diameter mm]   e.g. xycircle 400 2400 76.2 152.4
  dpatch    Depth witness for the dose curve of the configured
            laser_power_model: two rows of small engraved patches
            (serpentine G1 fills) on the stock. Row A is CW (S1000) at
            feeds giving relative doses 1.0 to 0.25 of the reference
            feed; row B is 100/80/60/45/30 %% density or duty at the
            reference feed. Match each row-B patch to the row-A patch
            of equal depth by eye: that reads the model's light
            fraction off the material, next to what the thermopile
            says it should be. Samples sysfs at 25 Hz like pcurve;
            JSON record in the bench data directory.
              dpatch [F] [pitch] [length]   e.g. dpatch 1500 0.3 30
  flowload  Cooling under laser load, one armed run per invocation, the
            conf keys it writes put back when the run ends; the pump is
            never commanded off. `t1` reproduces the flow-check trip:
            the check ON at its defaults (50 s window, 14.4 C limit,
            150 s re-check) and two 30 x 4 mm serpentine fills at F1500
            CW starting on the press with no dark dwell, so the tube is
            lit for about 35 s of the window; reports the engine's own
            rise/dT verdict beside the 25 Hz trace of both coolant
            sensors, the current, the digital witness and the heater
            output, in 5 s bins across the window, and the shape at
            fire start (a common-mode step is electrical, a lagged ramp
            is thermal). `t2 <secs> [pct]` turns the check OFF for the
            run (cool_flow_check_s = 0) and fires one fill of about
            <secs> lit seconds at CW, or at <pct> density; reports the
            lag to each sensor, the rise per raw-second of hv_current
            (k) and what a full 50 s window would add against the 1.6 C
            margin. `fit` reads every t2 record in the bench data dir
            and fits rise against dose. JSON records like dpatch.
              flowload t1 | flowload t2 <secs> [pct] | flowload fit
  senderchg A sender change mid-job: a 20 mm line at F60 lit on the
            press, the connection dropped five seconds in, the job held
            where it stopped, a reconnect, then ~ from the new session,
            which must light the button and wait. PASS: the hold, the
            resume prompt, no lit sample between the drop and the second
            press, the rest of the line marks. Two presses; JSON record.
  holdres   Feed hold and resume, the pause as a corner in time: a 30 mm
            M4 line held near 10 mm and resumed, then a 90 degree corner
            to compare the marks; the same hold under M3 on the return
            line (no dark lead on the resume); then a hold past the
            disarm grace, resumed with ~ through the re-arm prompt. Two
            presses: the arm, and the re-arm after the long hold.
  overrun   An RX overrun mid-job: a 20 mm line at F60 lit on the press,
            a 93-line fill written at once three seconds in (about 1270
            bytes against the 1023-byte ring). PASS: the controller
            reports the overrun, stops in alarm with the window closed
            and emission ending at the alarm; after $X a fresh M3 prompts
            for the button again and a 5 mm line marks. Two presses.
  expstop   Armed kill on the EXPECTED-stop path: start a mark job,
            then mid-burn POST /controller/stop (the supervisor stops
            the controller: SIGTERM, reap, exit safing). PASS: emission
            drops to 0 within a few samples of the stop and stays 0,
            the kernel is not running, and POST /controller/start
            is a SEPARATE step (`ctrlstart`, run after the operator has
            judged the stop). Needs the panel token: GF_TOKEN, or
            /data/forgefirm/panel.token when running on the board.
  ctrlstart POST /controller/start after an expstop; no motion, no laser.

The G-4 arm-refuses-when-a-fire-gate-is-active drill is operator-manual
(kill the pump during the button wait); this harness prints the cue.
"""
import json
import math
import os
import re
import socket
import sys
import time
import urllib.request

HOST = os.environ.get('GF_HOST') or '127.0.0.1'
PORT = 23
CTRL_PORT = int(os.environ.get('FORGECTRL_PORT') or 80)
BASE = 'http://%s:%d' % (HOST, CTRL_PORT)


def panel_token():
    tok = os.environ.get('GF_TOKEN', '')
    if tok:
        return tok
    try:
        with open('/data/forgefirm/panel.token') as f:
            return f.read().strip()
    except OSError:
        return ''


def sample_ir_baseline(seconds=3.0):
    """Per-channel lid-IR means at idle, lid closed, taken before the job:
    the baseline this run is judged against (never a stored one)."""
    acc = [0.0, 0.0, 0.0, 0.0]
    n = 0
    t_end = time.time() + seconds
    while time.time() < t_end:
        s = sample_forgectrl()
        if s and s['ir'] and len(s['ir']) == 4:
            for i in range(4):
                acc[i] += s['ir'][i]
            n += 1
        time.sleep(0.25)
    if not n:
        return None
    return [round(a / n, 1) for a in acc]


def ir_delta(peak, base):
    if base is None:
        return ['n/a'] * 4
    return [round(peak[i] - base[i], 1) for i in range(4)]


def get_json(path):
    with urllib.request.urlopen(BASE + path, timeout=4) as r:
        return json.load(r)


class Grbl:
    def __init__(self, host, port):
        self.s = socket.create_connection((host, port), timeout=5)
        self.s.settimeout(0.2)
        self.buf = b''
        self.partial = ''
        self.log = []                       # (t, line) for every non-status reply
        time.sleep(0.5)
        self.drain()

    def drain(self):
        try:
            while True:
                d = self.s.recv(4096)
                if not d:
                    break
                self.buf += d
        except socket.timeout:
            pass
        out, self.buf = self.buf, b''
        text = out.decode('ascii', 'replace')
        # Keep every complete reply that is not a status frame: ok, error,
        # [MSG:...], ALARM. The drills otherwise discard them, and the
        # controller reports arming and refusals only to the sender.
        pieces = (self.partial + text).split('\n')
        self.partial = pieces.pop()
        now = time.time()
        for ln in pieces:
            ln = ln.strip()
            if ln and not ln.startswith('<'):
                self.log.append((now, ln))
        return text

    def cmd(self, line, timeout=5.0):
        self.s.sendall(line.encode() + b'\n')
        deadline = time.time() + timeout
        text = ''
        while time.time() < deadline:
            text += self.drain()
            if 'ok' in text or 'error' in text or 'ALARM' in text:
                return text.strip()
            time.sleep(0.02)
        return '(timeout) ' + text.strip()

    def status(self):
        self.s.sendall(b'?')
        t = time.time() + 1.0
        text = ''
        while time.time() < t:
            text += self.drain()
            if '>' in text:
                break
            time.sleep(0.02)
        if '<' in text and '>' in text:
            return text[text.rfind('<'):text.rfind('>') + 1]
        return ''

    def state(self):
        st = self.status()
        return st[1:].split('|')[0] if st else ''

    def rt(self, ch):
        self.s.sendall(ch)

    def wait_state(self, want, timeout=60.0, poll=0.1):
        deadline = time.time() + timeout
        while time.time() < deadline:
            s = self.state()
            if s.startswith(want):
                return s
            time.sleep(poll)
        return self.state()


def sample_forgectrl():
    """One combined /status + /cool/status sample, or None on error."""
    try:
        st = get_json('/status')
        cs = get_json('/cool/status')
    except Exception:
        return None
    return {
        't': time.time(),
        'kstate': st.get('state'),
        'emission': st.get('laser', {}).get('emission_samples'),
        'pgood': st.get('laser', {}).get('pgood_samples'),
        'faults': st.get('faults'),
        'hv': st.get('hv_current_raw'),
        'ir': st.get('lid_ir'),
        'armed': cs.get('armed'),
        'fire_watch': cs.get('fire_watch'),
        'verdict': cs.get('verdict'),
        'phase': cs.get('phase'),
        'reason': cs.get('reason'),
    }


def arm_cue():
    print('\n>>> OPERATOR: eye protection on, exhaust running, fire watch,')
    print('>>> extinguisher in reach, scrap under the head with room to move.')
    print('>>> The job is starting. The white button will light and the')
    print('>>> stream will BLOCK until you press the physical arm button.')
    print('>>> The machine fires only after your press.\n')


def run_and_sample(g, gcode_lines, sample_hz=8, overall_timeout=200):
    """Stream gcode; sample forgectrl through the whole arm -> fire ->
    disarm lifecycle. The arm phase (fan run + flow interrogation) plus
    the operator button wait present grblHAL as Idle, so completion must
    NOT trigger on an early Idle. Complete on one of:
      - real fire captured: emission was seen > 0, then grbl Idle > 3 s;
      - no-fire disarm: armed went True then False, grbl Idle, > 15 s in;
      - overall timeout.
    """
    samples = []
    period = 1.0 / sample_hz
    s0 = sample_forgectrl()
    if s0:
        samples.append(s0)
    for ln in gcode_lines:
        g.s.sendall(ln.encode() + b'\n')
    t_start = time.time()
    next_t = t_start
    seen_emission = False
    seen_armed = False
    disarmed_now = False
    idle_since = None
    while time.time() - t_start < overall_timeout:
        now = time.time()
        if now >= next_t:
            smp = sample_forgectrl()
            if smp:
                samples.append(smp)
                if smp['emission'] and smp['emission'] > 0:
                    seen_emission = True
                if smp['armed']:
                    seen_armed = True
                disarmed_now = seen_armed and not smp['armed']
            next_t = now + period
        st = g.state()
        if st.startswith('Idle'):
            if idle_since is None:
                idle_since = now
            idle_for = now - idle_since
            if seen_emission and idle_for > 3.0:
                break                       # captured the burn
            if disarmed_now and (now - t_start) > 15 and idle_for > 3.0:
                break                       # armed then disarmed, no fire
        else:
            idle_since = None
        time.sleep(0.05)
    return samples


def prepare(g):
    """Guarantee a clean Idle start: clear a latched Door hold (lid was
    opened to inspect) or an Alarm before the run."""
    st = g.status()
    if 'Door' in st or 'Hold' in st:
        g.rt(b'\x18')                       # soft reset clears the hold
        time.sleep(2)
        g.drain()
        st = g.status()
    if 'Alarm' in st:
        print('unlock: %s' % g.cmd('$X'))
        st = g.status()
    return st


def drill_witness(g):
    print('=== Phase 5 witness drill: S400 vector mark ===')
    print('connect: %s' % prepare(g))
    base = sample_forgectrl()
    print('pre-fire: %s' % base)
    arm_cue()
    # A small square outline at S400, motion-only feed so the fire window
    # is unambiguous. Absolute-relative: use G91 so no homing is needed.
    job = [
        'G91', 'G21',           # relative, mm
        'M4',                   # dynamic laser mode, spindle enable
        'S400',
        'G1 X20 F600',
        'G1 Y20 F600',
        'G1 X-20 F600',
        'G1 Y-20 F600',
        'M5',                   # laser off
        'G90',
        'M2',                   # program end: X-3 job-based disarm trigger
    ]
    ir_base = sample_ir_baseline()
    samples = run_and_sample(g, job)
    # Analysis.
    emis = [s['emission'] for s in samples if s['emission'] is not None]
    peak_emis = max(emis) if emis else 0
    end_emis = emis[-1] if emis else None
    ir_peak = [0, 0, 0, 0]
    hv_vals = []
    for s in samples:
        if s['ir'] and len(s['ir']) == 4:
            for i in range(4):
                ir_peak[i] = max(ir_peak[i], s['ir'][i])
        if s['hv'] is not None:
            hv_vals.append(s['hv'])
    armed_seen = any(s['armed'] for s in samples)
    pgood_vals = [s['pgood'] for s in samples if s['pgood'] is not None]
    print('\n--- results ---')
    print('emission_samples: peak=%s end=%s (PASS if peak>0 and end==0)'
          % (peak_emis, end_emis))
    print('pgood_samples during job: peak=%s (>=128 = power-good)'
          % (max(pgood_vals) if pgood_vals else '-'))
    print('lid_ir peak=%s vs baseline=%s  delta=%s'
          % (ir_peak, ir_base, ir_delta(ir_peak, ir_base)))
    print('hv_current range: %s..%s' % (min(hv_vals) if hv_vals else '-',
                                        max(hv_vals) if hv_vals else '-'))
    print('armed observed during job: %s' % armed_seen)
    # X-3: measure time-to-disarm after the job completes at Idle. The
    # job-based window (Phase 4) should disarm promptly at Idle entry,
    # not wait out the ~60 s laser_disarm_s grace.
    t0 = time.time()
    disarm_dt = None
    while time.time() - t0 < 75:
        s = sample_forgectrl()
        if s and not s['armed']:
            disarm_dt = time.time() - t0
            break
        time.sleep(1)
    print('X-3 time-to-disarm after Idle: %s s (job-based PASS if prompt, '
          'not ~60 s grace)'
          % (round(disarm_dt, 1) if disarm_dt is not None else '>75 (REVIEW)'))
    ok = peak_emis > 0 and end_emis == 0
    print('WITNESS emission %s' % ('PASS' if ok else 'REVIEW - see values above'))
    # The engine's fire watch trips on absolute lid-IR counts, per quartile
    # pair (cool_fire_q1_alert / _critical, cool_fire_q2_alert / _critical):
    # the peaks above are what those thresholds must clear on a clean job.
    print('fire-watch thresholds must sit above these peaks: %s '
          '(cool_fire_q1_* for channels 1-2, cool_fire_q2_* for 3-4)' % ir_peak)
    return samples


def drill_hold(g):
    print('=== Phase 4 G-10 drill: disarm grace counts down in Hold ===')
    print('connect: %s' % prepare(g))
    arm_cue()
    # +X move at F300 (~5 mm/s). Held after ~2 s of motion (~10 mm),
    # so ~30 mm of +X clearance is plenty.
    job = ['G91', 'G21', 'M4', 'S400', 'G1 X40 F300']
    for ln in job:
        g.s.sendall(ln.encode() + b'\n')
    print('armed; waiting for motion to start (arm + your button press)...')
    st = g.wait_state('Run', 180)           # arming + button wait, then motion
    if not st.startswith('Run'):
        print('FAIL: motion never started (state=%s) - arm refused or no press'
              % st)
        g.cmd('M5', timeout=1)
        g.rt(b'\x18')
        return
    print('moving under laser: %s; feed-hold in 2 s' % st)
    time.sleep(2)
    g.rt(b'!')                      # feed hold mid-move
    st = g.wait_state('Hold', 5)
    print('feed-held mid-move: %s' % st)
    print('watching the disarm grace count down IN HOLD (not resuming)...')
    t0 = time.time()
    disarmed_at = None
    while time.time() - t0 < 120:
        s = sample_forgectrl()
        held = g.state().startswith('Hold')
        if s and not s['armed']:
            disarmed_at = time.time() - t0
            break
        if not held:
            print('note: left Hold (state=%s) before disarm' % g.state())
        time.sleep(1)
    # Recover: laser off, abort out of hold.
    g.cmd('M5', timeout=1)
    g.rt(b'\x18')                   # soft reset / abort out of hold
    time.sleep(1)
    print('G-10 disarmed in Hold after %s s (PASS if it disarms while held; '
          'the bug left it armed for hours)'
          % (round(disarmed_at, 1) if disarmed_at else 'NOT WITHIN 120 - REVIEW'))
    if 'Alarm' in g.status():
        g.cmd('$X')


def drill_ircut(g):
    power = int(sys.argv[2]) if len(sys.argv) > 2 else 1000
    feed = int(sys.argv[3]) if len(sys.argv) > 3 else 300
    print('=== lid-IR characterization: S%d F%d 30 mm square ===' % (power, feed))
    print('connect: %s' % prepare(g))
    base = sample_forgectrl()
    print('pre-fire: %s' % base)
    arm_cue()
    job = [
        'G91', 'G21', 'M4', 'S%d' % power,
        'G1 X30 F%d' % feed, 'G1 Y30 F%d' % feed,
        'G1 X-30 F%d' % feed, 'G1 Y-30 F%d' % feed,
        'M5', 'G90', 'M2',
    ]
    ir_base = sample_ir_baseline()
    samples = run_and_sample(g, job, overall_timeout=400)
    ir_peak = [0, 0, 0, 0]
    ir_min = [10 ** 6] * 4
    hv_vals = []
    emis = []
    fw = set()
    for s in samples:
        if s['ir'] and len(s['ir']) == 4:
            for i in range(4):
                ir_peak[i] = max(ir_peak[i], s['ir'][i])
                ir_min[i] = min(ir_min[i], s['ir'][i])
        if s['hv'] is not None:
            hv_vals.append(s['hv'])
        if s['emission'] is not None:
            emis.append(s['emission'])
        if s['fire_watch']:
            fw.add(s['fire_watch'])
    print('\n--- results ---')
    print('samples: %d  emission peak=%s  fire_watch states=%s'
          % (len(samples), max(emis) if emis else '-', sorted(fw)))
    delta = ir_delta(ir_peak, ir_base)
    print('lid_ir min=%s peak=%s baseline=%s  peak delta=%s'
          % (ir_min, ir_peak, ir_base, delta))
    print('hv_current range: %s..%s' % (min(hv_vals) if hv_vals else '-',
                                        max(hv_vals) if hv_vals else '-'))
    print('worst peaks this job: %s counts -> the cool_fire_q1/q2 alert '
          'thresholds must sit above the worst across ALL jobs' % ir_peak)
    print('the engine logged its own "run telemetry: lid IR ..." line for this job')
    return samples


# Power ladder for `pthresh`, in percent of full duty. The spacing is fine
# at the bottom because that is where the tube stops striking.
PTHRESH_PCT = (2, 3, 4, 5, 6, 8, 10, 12, 14, 16, 20, 25, 30)
PTHRESH_LEN = 25.0                      # mm of burn per rung
PTHRESH_PITCH = 3.0                     # mm between rungs


def drill_pthresh(g):
    smax = int(sys.argv[2]) if len(sys.argv) > 2 else 1000
    feed = int(sys.argv[3]) if len(sys.argv) > 3 else 300
    levels = [(p, max(1, int(round(smax * p / 100.0)))) for p in PTHRESH_PCT]
    print('=== laser power threshold ladder: %d rungs, F%d, %g mm each ==='
          % (len(levels), feed, PTHRESH_LEN))
    print('constant power (M3): the commanded duty is the tested duty.')
    print('PRECONDITION: $35 must be 0 for this run. A floor already in')
    print('place lifts every rung and the threshold cannot be read.')
    print('rungs (drawn in order, alternating direction, +Y between):')
    for i, (pct, s) in enumerate(levels):
        print('  %2d: %2d%% -> S%d' % (i + 1, pct, s))
    print('connect: %s' % prepare(g))
    base = sample_forgectrl()
    print('pre-fire: %s' % base)
    arm_cue()
    print('>>> This ladder reaches %d%% of full power - use scrap you are'
          % PTHRESH_PCT[-1])
    print('>>> willing to cut through.\n')
    job = ['G91', 'G21', 'M3']
    for i, (_pct, s) in enumerate(levels):
        job.append('S%d' % s)
        job.append('G1 X%g F%d' % (PTHRESH_LEN if i % 2 == 0 else -PTHRESH_LEN,
                                   feed))
        job.append('G0 Y%g' % PTHRESH_PITCH)
    job += ['M5', 'G90', 'M2']
    samples = run_and_sample(g, job, overall_timeout=600)
    hv_vals = [s['hv'] for s in samples if s['hv'] is not None]
    emis = [s['emission'] for s in samples if s['emission'] is not None]
    print('\n--- results ---')
    print('samples: %d  emission peak=%s' % (len(samples),
                                             max(emis) if emis else '-'))
    print('hv_current range: %s..%s' % (min(hv_vals) if hv_vals else '-',
                                        max(hv_vals) if hv_vals else '-'))
    if samples:
        t0 = samples[0]['t']
        print('hv_current trace (t s : raw) - the discharge current is the')
        print('electrical witness of striking, and it lifts off baseline')
        print('well below the rung that marks. Read it for the rung')
        print('boundaries: the laser-off G0 between rungs reads 0, so the')
        print('runs of nonzero current count the rungs that struck at all.')
        line = []
        for s in samples:
            if s['hv'] is None:
                continue
            line.append('%5.1f:%s' % (s['t'] - t0, s['hv']))
            if len(line) == 8:
                print('  ' + '  '.join(line))
                line = []
        if line:
            print('  ' + '  '.join(line))
    print('\nRead the material: count rungs from the FIRST one drawn. The')
    print('lowest rung carrying a CONTINUOUS mark is the lasing threshold;')
    print('set $35 to that rung\'s percent. A rung showing only a spot at')
    print('the start of its line struck but did not sustain - it is below')
    print('the threshold, not at it. Note the emission counter proves the')
    print('safety chain asserted LASER_ON, not that the tube lased - only')
    print('the mark and the discharge current say that.')
    return samples


# --- density ladder -------------------------------------------------------

# Dose levels in percent of full, weighted to the bottom: with a density
# floor set, what matters is whether the lowest levels a user can dial in
# still mark, not how the top half behaves.
DLADDER_PCT = (1, 2, 5, 10, 20, 40, 70, 100)
DLADDER_LEN = 25.0                      # mm of burn per rung
DLADDER_PITCH = 3.0                     # mm between rungs
STREAM_RATE_HZ = 28160                  # machine tick (GFSINK_RATE default)
PWM_PERIOD = 127                        # 7-bit power byte against PWMSAR
CONF = os.environ.get('GFHOME_CONF') or '/data/forgefirm.conf'
PULSE_MIN_KEY = 'laser_pulse_min_ticks'
PULSE_MIN_DEFAULT = 3                   # glowforge_laser.c PULSE_MIN_TICKS_DEFAULT


def conf_get(key):
    """One key from the shared machine config, or None."""
    try:
        with open(CONF) as f:
            for line in f:
                head = line.split('#', 1)[0]
                if '=' in head:
                    k, v = head.split('=', 1)
                    if k.strip() == key:
                        return v.strip()
    except OSError:
        return None
    return None


def conf_set(key, val):
    """Set one key, preserving every other line. False when the config is
    not ours to write - running from a LAN host, say."""
    try:
        with open(CONF) as f:
            lines = f.read().splitlines()
    except OSError:
        return False
    out, done = [], False
    for line in lines:
        head = line.split('#', 1)[0]
        if '=' in head and head.split('=', 1)[0].strip() == key:
            if done:
                continue
            out.append('%s = %s' % (key, val))
            done = True
        else:
            out.append(line)
    if not done:
        out.append('%s = %s' % (key, val))
    try:
        mode = os.stat(CONF).st_mode & 0o777
        with open(CONF + '.tmp', 'w') as f:
            f.write('\n'.join(out) + '\n')
        os.chmod(CONF + '.tmp', mode)
        os.replace(CONF + '.tmp', CONF)
    except OSError:
        return False
    return True


def grbl_setting(g, key):
    """One $-setting as a float, read from the controller."""
    for line in g.cmd('$$', timeout=8.0).splitlines():
        line = line.strip()
        if line.startswith(key + '='):
            try:
                return float(line.split('=', 1)[1])
            except ValueError:
                return None
    return None


def drill_dladder(g):
    period = int(sys.argv[2]) if len(sys.argv) > 2 else 20
    feed = int(sys.argv[3]) if len(sys.argv) > 3 else 300
    if period < 1:
        print('period must be at least 1 machine tick')
        return 2

    print('=== density ladder: %d rungs, base period %d ticks, F%d, %g mm each ==='
          % (len(DLADDER_PCT), period, feed, DLADDER_LEN))

    # Preconditions. The model is read at each arm, so it must already be
    # selected; the floor must be gone, or every rung is lifted off the
    # bottom of the range this drill exists to explore.
    # Density is the shipped default, so an absent key selects it; only an
    # explicit analog selection is a refusal.
    model = conf_get('laser_power_model') or 'density'
    if model != 'density':
        print('PRECONDITION FAILED: laser_power_model is %r, need "density"'
              % (model,))
        print('(or the key absent, which selects it). The model is read at')
        print('each arm, so this key needs no controller restart.')
        return 2
    print('dose model: density (%s)'
          % ('set in ' + CONF if conf_get('laser_power_model') else 'driver default'))
    # The core maps S onto the level this model renders as density, and
    # $35/$36 are its floor and ceiling. Read them rather than assuming:
    # with a floor set, the ladder is testing the shipping mapping, and
    # every rung sits higher than its commanded percent.
    floor = grbl_setting(g, '$35')
    ceil = grbl_setting(g, '$36')
    rpm_max = grbl_setting(g, '$30')
    rpm_min = grbl_setting(g, '$31')
    if None in (floor, ceil, rpm_max, rpm_min) or rpm_max <= rpm_min:
        print('PRECONDITION FAILED: cannot read $30/$31/$35/$36 (%s/%s/%s/%s)'
              % (rpm_max, rpm_min, floor, ceil))
        return 2
    min_value = int(PWM_PERIOD * floor / 100.0)
    max_value = int(PWM_PERIOD * ceil / 100.0)
    gradient = (max_value - min_value) / (rpm_max - rpm_min)
    print('mapping: $30=%g $31=%g $35=%g $36=%g -> density %.1f%%..%.1f%%'
          % (rpm_max, rpm_min, floor, ceil,
             100.0 * min_value / PWM_PERIOD, 100.0 * max_value / PWM_PERIOD))
    if floor > 0.0:
        print('a floor is set, so the rungs below it all land on it - that is')
        print('the shipping mapping, not the raw range.')
    if conf_set('laser_pulse_ticks', str(period)):
        print('laser_pulse_ticks = %d (written to %s)' % (period, CONF))
    else:
        have = conf_get('laser_pulse_ticks')
        if have != str(period):
            print('PRECONDITION FAILED: cannot write %s from here, and' % CONF)
            print('laser_pulse_ticks is %r, not %d. Set it on the board.'
                  % (have, period))
            return 2
        print('laser_pulse_ticks already %d' % period)

    # What each rung actually emits. The on-count is dithered between
    # adjacent integers, so the burst below is the mean.
    min_ticks = int(conf_get(PULSE_MIN_KEY) or PULSE_MIN_DEFAULT)
    if min_ticks < 1:
        min_ticks = 1
    print('period %d ticks = %.0f us at %d Hz -> %.0f Hz pulse rate'
          % (period, period * 1e6 / STREAM_RATE_HZ, STREAM_RATE_HZ,
             STREAM_RATE_HZ / float(period)))
    print('minimum pulse %d ticks = %.0f us (%s): below it the model skips'
          % (min_ticks, min_ticks * 1e6 / STREAM_RATE_HZ,
             PULSE_MIN_KEY if conf_get(PULSE_MIN_KEY) else 'driver default'))
    print('periods and carries the debt, so the pulse never falls under it.')
    print('rungs (drawn in order, alternating direction, +Y between):')
    levels = []
    for pct in DLADDER_PCT:
        sval = int(round(rpm_max * pct / 100.0))
        level = int((sval - rpm_min) * gradient) + min_value
        level = min(level, max_value)
        dens = level / float(PWM_PERIOD)
        on = dens * period
        levels.append((pct, sval, dens))
        # The on-count is a whole number of ticks, dithered between the
        # two nearest, so quote the pulse the tube actually sees. Below
        # one tick per period the pulse stays one tick and periods are
        # skipped instead - that is the short end this drill is for.
        lo = int(on)
        tick_us = 1e6 / STREAM_RATE_HZ
        if on < min_ticks:
            # Below the minimum the model skips periods and carries the
            # debt, so the pulse holds at the minimum and the rate drops.
            burst = '%d ticks (%.0f us) every %.1f periods (%.0f Hz)' % (
                min_ticks, min_ticks * tick_us, min_ticks / on,
                STREAM_RATE_HZ / float(period) * on / min_ticks)
        elif on == lo:
            burst = '%d ticks (%.0f us)' % (lo, lo * tick_us)
        else:
            burst = '%d-%d ticks (%.0f-%.0f us)' % (lo, lo + 1, lo * tick_us,
                                                    (lo + 1) * tick_us)
        print('  %3d%% -> S%-4d  density %.4f  %5.2f on-ticks  pulse %s'
              % (pct, sval, dens, on, burst))
    print('connect: %s' % prepare(g))
    base = sample_forgectrl()
    print('pre-fire: %s' % base)
    arm_cue()
    print('>>> This ladder reaches FULL dose - use scrap you are willing')
    print('>>> to cut through.\n')

    job = ['G91', 'G21', 'M3']
    for i, (_pct, sval, _d) in enumerate(levels):
        job.append('S%d' % sval)
        job.append('G1 X%g F%d' % (DLADDER_LEN if i % 2 == 0 else -DLADDER_LEN,
                                   feed))
        job.append('G0 Y%g' % DLADDER_PITCH)
    job += ['M5', 'G90', 'M2']
    samples = run_and_sample(g, job, overall_timeout=600)

    emis = [s['emission'] for s in samples if s['emission'] is not None]
    hv_vals = [s['hv'] for s in samples if s['hv'] is not None]
    print('\n--- results ---')
    print('samples: %d  emission peak=%s' % (len(samples),
                                             max(emis) if emis else '-'))
    print('hv_current range: %s..%s' % (min(hv_vals) if hv_vals else '-',
                                        max(hv_vals) if hv_vals else '-'))
    if samples:
        t0 = samples[0]['t']
        print('hv_current trace (t s : raw) - the laser-off G0 between rungs')
        print('reads 0, so the runs of nonzero current count the rungs that')
        print('struck, and their level tracks the dose of each:')
        line = []
        for smp in samples:
            if smp['hv'] is None:
                continue
            line.append('%5.1f:%s' % (smp['t'] - t0, smp['hv']))
            if len(line) == 8:
                print('  ' + '  '.join(line))
                line = []
        if line:
            print('  ' + '  '.join(line))
    print('Check the controller said "laser armed (density, ...)" - an')
    print('"(analog, ...)" arm means the analog path ran and this is a duty')
    print('ladder, not a density one.')
    print('\nRead the material: count rungs from the FIRST one drawn.')
    print('Two readings, and the second is the one only the bench can give:')
    print(' 1. LINEARITY - does depth/darkness track the density column')
    print('    above, or does the low end mark harder than its share? Every')
    print('    burst restarts the discharge, so each carries the strike')
    print('    transient, and dose per burst need not scale with its length.')
    print(' 2. THE SHORT END - the lowest rung that still marks cleanly. Its')
    print('    burst length in us is the number to keep; a rung that stops')
    print('    marking sets the floor this base period can reach.')
    print('The base period does not decide the low end: below the minimum')
    print('the pulse interval is min_ticks x tick / density, which the')
    print('period cancels out of. What sets the bottom is the density')
    print('floor ($35), so a rung that fails is telling you to raise it.')
    return samples


# --- performance curve ladder ---------------------------------------------

# One line per level at constant feed, two instruments read through each:
# the HV current (the supply's curve under analog; presence only under
# density, where every pulse is full current) and the head thermopile, a
# scatter detector in the beam path upstream of the final mirror, so it
# reads the beam and not the material. Rungs are percents of full. The
# analog list is dense at the knee where the tube starts to lase; the
# density list is weighted to the bottom, where the per-pulse strike
# deficit lives.
PCURVE_ANALOG_PCT = (16, 18, 20, 23, 26, 30, 35, 42, 50, 60, 72, 85, 100)
PCURVE_DENSITY_PCT = (1, 2, 3, 5, 7, 10, 15, 20, 30, 45, 60, 80, 100)
PCURVE_FEED = 600                       # mm/min: 10 mm/s
PCURVE_LEN = 100.0                      # mm of burn per rung
PCURVE_PITCH = 3.0                      # mm between rungs (+X)
PCURVE_GAP_S = 4.0                      # laser-off settle before each rung
PCURVE_GAP_SKIP_S = 1.5                 # of which the first part still decays
PCURVE_SAMPLE_HZ = 25                   # sysfs sampler target rate
PCURVE_TRIM_HEAD_S = 1.0                # dropped from the start of each line
PCURVE_TRIM_TAIL_S = 0.5                # dropped from its end
SYSFS = '/sys/glowforge'
HV_FULL_SCALE = 1023                    # the PIC ADC's top count
# (key, sysfs attribute) per sampled channel.
PCURVE_CHANNELS = (
    ('hv', 'pic/hv_current'),
    ('tp', 'head/beam_detect_analog'),
    ('tpd', 'head/beam_detect_digital'),
    ('lon', 'cnc/laser_on_sampled'),
    ('wt1', 'pic/water_temp_1'),
    ('wt2', 'pic/water_temp_2'),
    ('pt', 'pic/pwr_temp'),
)


class Sampler:
    """Reads the pcurve channels in a thread. On the board they come
    straight from sysfs, each read a live bus transaction, so the achieved
    rate is whatever the PIC (SPI) and head (I2C) buses allow and it is
    reported rather than assumed. From a host the only source is
    forgectrl's /status at ~8 Hz, which carries the current and nothing
    the thermopile needs."""

    def __init__(self, hz, channels=PCURVE_CHANNELS):
        import threading
        self.channels = channels
        self.local = os.path.isdir(SYSFS)
        self.period = 1.0 / (hz if self.local else 8)
        self.samples = []
        self.errors = 0
        self._stop = threading.Event()
        self._thr = threading.Thread(target=self._run, daemon=True)

    def start(self):
        self._thr.start()

    def stop(self):
        self._stop.set()
        self._thr.join(timeout=3)

    def _read_sysfs(self):
        smp = {'t': time.time()}
        for key, attr in self.channels:
            try:
                with open(os.path.join(SYSFS, attr)) as f:
                    smp[key] = int(f.read().strip())
            except (OSError, ValueError):
                smp[key] = None
                self.errors += 1
        return smp

    def _read_status(self):
        st = sample_forgectrl()
        smp = dict((key, None) for key, _attr in self.channels)
        smp['t'] = time.time()
        if st is None:
            self.errors += 1
            return smp
        smp['hv'] = st['hv']
        smp['lon'] = st['emission']
        return smp

    def _run(self):
        read = self._read_sysfs if self.local else self._read_status
        next_t = time.time()
        while not self._stop.is_set():
            self.samples.append(read())
            next_t += self.period
            delay = next_t - time.time()
            if delay > 0:
                time.sleep(delay)
            else:
                next_t = time.time()

    def rate(self):
        if len(self.samples) < 2:
            return 0.0
        span = self.samples[-1]['t'] - self.samples[0]['t']
        return (len(self.samples) - 1) / span if span > 0 else 0.0


def _stats(vals):
    n = len(vals)
    if not n:
        return {'n': 0, 'mean': None, 'sd': None, 'min': None, 'max': None}
    mean = sum(vals) / float(n)
    var = sum((v - mean) ** 2 for v in vals) / float(n)
    return {'n': n, 'mean': mean, 'sd': var ** 0.5, 'min': min(vals),
            'max': max(vals)}


def _window(samples, t0, t1, key):
    return [s[key] for s in samples
            if t0 <= s['t'] < t1 and s.get(key) is not None]


def _linfit(xs, ys):
    """Least squares y = a + b x; (a, b, r2), or None below two points."""
    n = len(xs)
    if n < 2:
        return None
    mx = sum(xs) / float(n)
    my = sum(ys) / float(n)
    sxx = sum((x - mx) ** 2 for x in xs)
    if sxx == 0:
        return None
    b = sum((x - mx) * (y - my) for x, y in zip(xs, ys)) / sxx
    a = my - b * mx
    ss_res = sum((y - (a + b * x)) ** 2 for x, y in zip(xs, ys))
    ss_tot = sum((y - my) ** 2 for y in ys)
    r2 = 1.0 - ss_res / ss_tot if ss_tot > 0 else 1.0
    return a, b, r2


from gfbench import degc as _degc


def pcurve_levels(g, pcts):
    """(pct, S, level) per rung: the level is what the core maps S onto
    with the settings in force, as a fraction of full (duty/127 under
    analog, density under density). None when a setting cannot be read."""
    floor = grbl_setting(g, '$35')
    ceil = grbl_setting(g, '$36')
    rpm_max = grbl_setting(g, '$30')
    rpm_min = grbl_setting(g, '$31')
    if None in (floor, ceil, rpm_max, rpm_min) or rpm_max <= rpm_min:
        return None, (rpm_max, rpm_min, floor, ceil)
    min_value = int(PWM_PERIOD * floor / 100.0)
    max_value = int(PWM_PERIOD * ceil / 100.0)
    gradient = (max_value - min_value) / (rpm_max - rpm_min)
    levels = []
    for pct in pcts:
        sval = int(round(rpm_max * pct / 100.0))
        if sval <= rpm_min:
            level = 0
        else:
            level = min(int((sval - rpm_min) * gradient) + min_value, max_value)
        levels.append((pct, sval, level / float(PWM_PERIOD)))
    return levels, (rpm_max, rpm_min, floor, ceil)


def pcurve_analyze(samples, rungs, head_trim=PCURVE_TRIM_HEAD_S,
                   tail_trim=PCURVE_TRIM_TAIL_S):
    """Per-rung statistics over the trimmed steady window of each line,
    then the curve: normalized thermopile delta against level, a
    monotonicity count, straight-line fits with their x-intercepts, and
    the repeat-rung comparison. Pure: takes the raw trace and the rung
    brackets, returns a dict, so it can be checked without a machine."""
    rows = []
    for r in rungs:
        t0, t1 = r['t_run0'] + head_trim, r['t_run1'] - tail_trim
        if t1 - t0 < 1.0:
            t0, t1 = r['t_run0'], r['t_run1']
        hv = _stats(_window(samples, t0, t1, 'hv'))
        tp = _stats(_window(samples, t0, t1, 'tp'))
        # The thermopile falls back to baseline within about a second of a
        # line ending (measured 2026-08-25), so the first part of the gap
        # still carries the previous rung's tail; the baseline is the rest.
        base = _stats(_window(samples, r['t_gap0'] + PCURVE_GAP_SKIP_S,
                              r['t_m3'], 'tp'))
        first = _stats(_window(samples, t0, min(t0 + 2.0, t1), 'tp'))
        last = _stats(_window(samples, max(t1 - 2.0, t0), t1, 'tp'))
        tpd = _window(samples, t0, t1, 'tpd')
        # laser_on_sampled is a once-per-second window count, so the
        # last window of a line lands after Idle: look one second past.
        lon = _window(samples, r['t_run0'], r['t_run1'] + 1.0, 'lon')
        # Coolant from the UPSTREAM sensor: water_temp_1 sits downstream of
        # the flow-check heater and swings with it during a run.
        wt1 = _stats(_window(samples, r['t_gap0'], r['t_m3'], 'wt2'))
        pt = _stats(_window(samples, r['t_gap0'], r['t_m3'], 'pt'))
        delta = (tp['mean'] - base['mean']
                 if tp['mean'] is not None and base['mean'] is not None
                 else None)
        drift = (last['mean'] - first['mean']
                 if last['mean'] is not None and first['mean'] is not None
                 else None)
        rows.append({
            'rung': r['rung'], 'repeat': r.get('repeat', False),
            'pct': r['pct'], 's': r['s'], 'level': r['level'],
            'seconds': round(r['t_run1'] - r['t_run0'], 2),
            'hv_n': hv['n'], 'hv_mean': hv['mean'], 'hv_sd': hv['sd'],
            'hv_max': hv['max'],
            'hv_clipped': hv['max'] is not None and hv['max'] >= HV_FULL_SCALE,
            'tp_n': tp['n'], 'tp_mean': tp['mean'], 'tp_sd': tp['sd'],
            'tp_base': base['mean'], 'tp_base_sd': base['sd'],
            'tp_delta': delta, 'tp_drift': drift,
            'tpd_duty': (sum(1 for v in tpd if v) / float(len(tpd))
                         if tpd else None),
            'lon_max': max(lon) if lon else None,
            'fired': bool(lon) and max(lon) > 0,
            'coolant_raw': wt1['mean'],
            'coolant_c': _degc(wt1['mean']) if wt1['mean'] is not None else None,
            'supply_raw': pt['mean'],
        })
    primary = [row for row in rows if not row['repeat']]
    primary.sort(key=lambda row: row['level'])
    fired = [row for row in primary if row['fired']]
    curve = {'rows': len(rows), 'fired': len(fired)}
    # Normalize the thermopile delta to the top of the ladder.
    deltas = [row['tp_delta'] for row in fired if row['tp_delta'] is not None]
    top = max(deltas) if deltas else None
    for row in rows:
        row['tp_norm'] = (row['tp_delta'] / top
                          if top and row['tp_delta'] is not None else None)
    # Monotonicity: decreases of the delta with rising level, beyond noise.
    dec = 0
    prev = None
    for row in fired:
        if row['tp_delta'] is None:
            continue
        if prev is not None and row['tp_delta'] < prev['tp_delta'] - 2.0 * (row['tp_sd'] or 0):
            dec += 1
        prev = row
    curve['tp_decreases'] = dec
    dec = 0
    prev = None
    for row in fired:
        if row['hv_mean'] is None or row['hv_clipped']:
            continue
        if prev is not None and row['hv_mean'] < prev['hv_mean'] - 2.0 * (row['hv_sd'] or 0):
            dec += 1
        prev = row
    curve['hv_decreases'] = dec
    # Signal rungs: delta clear of the baseline noise, for the fits.
    sig = [row for row in fired if row['tp_delta'] is not None
           and row['tp_delta'] > 3.0 * (row['tp_base_sd'] or 0)]
    fit = _linfit([row['level'] for row in sig], [row['tp_delta'] for row in sig])
    if fit:
        a, b, r2 = fit
        curve['tp_fit'] = {'points': len(sig), 'intercept': a, 'slope': b,
                           'r2': r2,
                           'x_intercept': (-a / b) if b else None}
    unclipped = [row for row in fired if row['hv_mean'] is not None
                 and not row['hv_clipped']]
    fit = _linfit([row['level'] for row in unclipped],
                  [row['hv_mean'] for row in unclipped])
    if fit:
        a, b, r2 = fit
        curve['hv_fit'] = {'points': len(unclipped), 'intercept': a,
                           'slope': b, 'r2': r2,
                           'x_intercept': (-a / b) if b else None}
    curve['hv_clipped_rungs'] = [row['rung'] for row in rows if row['hv_clipped']]
    reps = [row for row in rows if row['repeat']]
    firsts = [row for row in rows if not row['repeat'] and reps
              and row['rung'] == reps[-1]['rung']]
    if reps and firsts:
        first, again = firsts[0], reps[-1]
        rep = {'rung': first['rung']}
        if first['tp_delta'] is not None and again['tp_delta'] is not None:
            rep['tp_delta_first'] = first['tp_delta']
            rep['tp_delta_again'] = again['tp_delta']
            rep['tp_delta_change'] = again['tp_delta'] - first['tp_delta']
            rep['tp_delta_change_pct'] = (100.0 * rep['tp_delta_change'] / first['tp_delta']
                                          if first['tp_delta'] else None)
        if first['hv_mean'] is not None and again['hv_mean'] is not None:
            rep['hv_change'] = again['hv_mean'] - first['hv_mean']
        bases = [row['tp_base'] for row in rows if row['tp_base'] is not None]
        if len(bases) >= 2:
            rep['baseline_walk'] = bases[-1] - bases[0]
        curve['repeat'] = rep
    return {'rungs': rows, 'curve': curve}


def _fmt(v, prec=1):
    if v is None:
        return '-'
    if isinstance(v, float):
        return '%.*f' % (prec, v)
    return str(v)


def pcurve_report(res, model):
    rows, curve = res['rungs'], res['curve']
    print('\n--- per rung (steady window, first %gs and last %gs of each line dropped) ---'
          % (PCURVE_TRIM_HEAD_S, PCURVE_TRIM_TAIL_S))
    unit = 'density' if model == 'density' else 'duty'
    print('  rung  %%    S     %-8s  hv mean  sd    max    | tp delta   sd    base    drift  norm  | dig  lon  cool'
          % unit)
    for row in rows:
        tag = '%2d%s' % (row['rung'], 'r' if row['repeat'] else ' ')
        print('  %s  %3d  %4d  %6.2f%%   %7s %5s %5s%s | %8s %5s %7s %6s %5s | %4s %4s %s'
              % (tag, row['pct'], row['s'], 100.0 * row['level'],
                 _fmt(row['hv_mean']), _fmt(row['hv_sd']), _fmt(row['hv_max'], 0),
                 '!' if row['hv_clipped'] else ' ',
                 _fmt(row['tp_delta']), _fmt(row['tp_sd']), _fmt(row['tp_base']),
                 _fmt(row['tp_drift']), _fmt(row['tp_norm'], 3),
                 _fmt(row['tpd_duty'], 2), _fmt(row['lon_max'], 0),
                 _fmt(row['coolant_c']) if row['coolant_c'] is not None
                 else _fmt(row['coolant_raw'], 0) + 'raw'))
    print('  (! = hv_current touched %d: the ADC is clipped there and the'
          % HV_FULL_SCALE)
    print('   current column is no longer a measurement on that rung)')
    print('\n--- curve ---')
    print('rungs fired (laser_on_sampled > 0): %d of %d' % (curve['fired'], curve['rows']))
    print('thermopile delta decreases with rising level (beyond 2 sd): %s'
          % curve['tp_decreases'])
    print('hv_current decreases with rising level (beyond 2 sd, unclipped): %s'
          % curve['hv_decreases'])
    if curve.get('hv_clipped_rungs'):
        print('hv_current CLIPPED on rungs %s' % curve['hv_clipped_rungs'])
    for name, key in (('thermopile', 'tp_fit'), ('hv_current', 'hv_fit')):
        f = curve.get(key)
        if not f:
            print('%s fit: not enough signal rungs' % name)
            continue
        print('%s vs level: %d points, slope %.1f per 100%%, r2 %.3f, '
              'x-intercept %s%% (the measured threshold if the fit holds)'
              % (name, f['points'], f['slope'], f['r2'],
                 _fmt(100.0 * f['x_intercept']) if f['x_intercept'] is not None else '-'))
    rep = curve.get('repeat')
    if rep:
        print('repeat of rung %d: thermopile delta %s -> %s (%s, %s%%), '
              'hv %s; baseline walked %s over the ladder'
              % (rep['rung'], _fmt(rep.get('tp_delta_first')),
                 _fmt(rep.get('tp_delta_again')), _fmt(rep.get('tp_delta_change')),
                 _fmt(rep.get('tp_delta_change_pct')), _fmt(rep.get('hv_change')),
                 _fmt(rep.get('baseline_walk'))))


def drill_pcurve(g):
    feed = int(sys.argv[2]) if len(sys.argv) > 2 else PCURVE_FEED
    length = float(sys.argv[3]) if len(sys.argv) > 3 else PCURVE_LEN
    model = conf_get('laser_power_model') or 'density'
    if len(sys.argv) > 4:
        pcts = tuple(int(x) for x in sys.argv[4].split(',') if x.strip())
    else:
        pcts = PCURVE_DENSITY_PCT if model == 'density' else PCURVE_ANALOG_PCT
    if not pcts or min(pcts) < 1 or max(pcts) > 100:
        print('rungs must be percents in 1..100')
        return 2
    print('=== laser performance curve: %s model, %d rungs + repeat, F%d, %g mm each ==='
          % (model, len(pcts), feed, length))
    print('constant power (M3): the commanded level is the tested level.')
    levels, (rpm_max, rpm_min, floor, ceil) = pcurve_levels(g, pcts)
    if levels is None:
        print('PRECONDITION FAILED: cannot read $30/$31/$35/$36 (%s/%s/%s/%s)'
              % (rpm_max, rpm_min, floor, ceil))
        return 2
    print('mapping: $30=%g $31=%g $35=%g $36=%g' % (rpm_max, rpm_min, floor, ceil))
    if floor > 0.0:
        print('a floor is set, so the low rungs land on it: this run records')
        print('the shipping mapping. To measure the curve itself set $35=0')
        print('and restart the controller first.')
    unit = 'density' if model == 'density' else 'duty'
    # The drift witness is a mid-ladder rung drawn again at the end: it
    # has real signal (the bottom rung sits at the threshold and reads
    # nothing twice) and it is not full power, so it adds little heat.
    witness = len(levels) // 2
    print('rungs (each a +X line from the block\'s X0, stepping +Y %g mm; rung'
          % PCURVE_PITCH)
    print('%d is drawn again at the end, running -X, as the drift witness;'
          % (witness + 1))
    print('the next run\'s block starts %g mm further along X):' % length)
    for i, (pct, sval, level) in enumerate(levels):
        print('  %2d: %3d%% -> S%-4d  %s %.2f%%' % (i + 1, pct, sval, unit, 100.0 * level))
    sampler = Sampler(PCURVE_SAMPLE_HZ)
    if sampler.local:
        print('sampling sysfs on the board at a target %d Hz: %s'
              % (PCURVE_SAMPLE_HZ, ' '.join(attr for _k, attr in PCURVE_CHANNELS)))
    else:
        print('NOT on the board: sampling forgectrl /status at ~8 Hz instead.')
        print('That carries the current and the emission witness only; the')
        print('thermopile is not in /status, so this run yields no curve.')
    print('connect: %s' % prepare(g))
    print('pre-fire: %s' % sample_forgectrl())
    arm_cue()
    print('>>> This ladder reaches FULL power for %.0f s per line. Use'
          % (length / feed * 60.0))
    print('>>> something you are willing to cut through and that will not')
    print('>>> flame: scrap tile, firebrick, thick draftboard on a')
    print('>>> sacrificial layer. The thermopile is in the head, so the')
    print('>>> material is not part of the measurement.\n')
    print('G91/G21: %s / %s' % (g.cmd('G91'), g.cmd('G21')))
    order = list(range(len(levels))) + [witness]
    line_s = length / feed * 60.0
    sampler.start()
    rungs = []
    aborted = None
    try:
        for n, idx in enumerate(order):
            pct, sval, level = levels[idx]
            repeat = n == len(order) - 1
            # Every line runs +X from the block's X0 at one Y and the rungs
            # step +Y, so a run occupies a block `length` wide by
            # PCURVE_PITCH x rungs tall and the next run's block starts
            # `length` further along X. The drift witness runs the other
            # way, from the far end back to X0: a swing that reverses with
            # direction is head position along the gantry; one that repeats
            # is time.
            if repeat:
                g.s.sendall(('G0 X%g\n' % length).encode())
                g.wait_state('Idle', 30)
            t_gap0 = time.time()
            time.sleep(PCURVE_GAP_S)             # laser off: the baseline
            t_m3 = time.time()
            job = ['M3 S%d' % sval,
                   'G1 X%g F%d' % (-length if repeat else length, feed),
                   'M5']
            for ln in job:
                g.s.sendall(ln.encode() + b'\n')
            st = g.wait_state('Run', 240 if n == 0 else 60)
            if not st.startswith('Run'):
                aborted = ('rung %d never ran (state=%s): arm refused, no '
                           'press, or the controller alarmed' % (idx + 1, st))
                break
            t_run0 = time.time()
            st = g.wait_state('Idle', line_s + 30.0, poll=0.05)
            t_run1 = time.time()
            if not st.startswith('Idle'):
                aborted = 'rung %d did not finish (state=%s)' % (idx + 1, st)
                break
            if t_run1 - t_run0 < line_s - 1.5:
                # A line that ended early was canceled by the operator or
                # the controller, and a cancel may have moved the head
                # (the controller returns to machine zero). From here every
                # relative move is aimed from a position this drill no
                # longer knows, so send nothing more.
                aborted = ('rung %d ran %.1f s of %.1f: canceled; no further '
                           'moves sent' % (idx + 1, t_run1 - t_run0, line_s))
                break
            rungs.append({'rung': idx + 1, 'repeat': repeat, 'pct': pct,
                          's': sval, 'level': level, 't_gap0': t_gap0,
                          't_m3': t_m3, 't_run0': t_run0, 't_run1': t_run1})
            print('  rung %2d%s: S%-4d ran %.1f s' % (idx + 1, 'r' if repeat else ' ',
                                                    sval, t_run1 - t_run0))
            back = '' if repeat else 'G0 X%g\n' % -length
            g.s.sendall((back + 'G0 Y%g\n' % PCURVE_PITCH).encode())
            g.wait_state('Idle', 30)
    finally:
        try:
            g.cmd('M5', timeout=1)
        except Exception:
            pass
        if aborted:
            g.rt(b'\x18')                   # abort out of whatever it is in
        else:
            g.s.sendall(b'G90\nM2\n')       # program end closes the window
        time.sleep(1.5)
        sampler.stop()
    if aborted:
        print('ABORTED: %s' % aborted)
    print('\nsampler: %d samples, %.1f Hz achieved, %d read errors'
          % (len(sampler.samples), sampler.rate(), sampler.errors))
    if not rungs:
        return 1
    res = pcurve_analyze(sampler.samples, rungs)
    pcurve_report(res, model)
    record = {
        'drill': 'pcurve', 'date': time.strftime('%Y-%m-%dT%H:%M:%S'),
        'model': model, 'feed': feed, 'length_mm': length,
        'gap_s': PCURVE_GAP_S, 'pitch_mm': PCURVE_PITCH,
        'settings': {'$30': rpm_max, '$31': rpm_min, '$35': floor, '$36': ceil},
        'sampler': {'local': sampler.local, 'hz': sampler.rate(),
                    'samples': len(sampler.samples), 'errors': sampler.errors},
        'aborted': aborted, 'rungs': res['rungs'], 'curve': res['curve'],
        'trace': sampler.samples,
    }
    ddir = os.environ.get('FORGETEST_BENCH_DATA') or ('/tmp' if sampler.local else os.getcwd())
    path = os.path.join(ddir, 'pcurve_%s_%s.json' % (model, time.strftime('%Y%m%d-%H%M%S')))
    try:
        with open(path, 'w') as f:
            json.dump(record, f, indent=1)
        print('record: %s' % path)
    except OSError as e:
        print('record not written: %s' % e)
    print('\nRead it in this order. First the instrument: the thermopile')
    print('delta must rise with the level on every rung that fired, settle')
    print('inside the line (small drift), return to its baseline between')
    print('rungs, and the repeat rung must agree with its first run. Any')
    print('miss there is a fact about the sensor, not the tube. Then the')
    print('curve: under analog the current column is the supply and the')
    print('thermopile is the tube; under density the current is only a')
    print('presence witness and the thermopile is the whole story. A knee')
    print('where the delta stops rising before 100%% is the ceiling S1000')
    print('should map to. The material remains the witness that it lased.')
    return res


# --- the depth witness: CW patches at speed against density patches ---------

# The thermopile reads 80 % density as half the light of CW, and the
# material decides whether that is the tube or the sensor. A patch is a
# serpentine of G1 lines at one level and one feed; under constant power
# (M3) the dose per unit area is light x time, so a row of CW patches
# (S1000: the discharge continuous) at feeds F600/dose is a reference
# ladder of known relative dose, and a row of density patches at F600 is
# matched to it by engrave depth. The CW patch a density patch matches
# reads that density's light fraction straight off the stock, with the
# thermopile's prediction printed beside it. The lines alternate
# direction so no rapid crosses a fill; judge the middle of each patch,
# since under constant power the ends carry the acceleration and read
# darker at the fast feeds.
# Defaults, all overridable on the command line (dpatch [F] [pitch] [length]).
# The dose has to be an engrave, not a burn: CW at 10 mm/s with 0.2 mm lines
# turned thick draftboard to charcoal that no longer cuts (2026-08-25), so the
# reference is 25 mm/s at 0.3 mm, about a quarter of that energy density,
# and the lines are long enough that the fastest CW patch (100 mm/s) still
# has a plateau in its middle after the 7 mm acceleration at each end.
DPATCH_W = 30.0                          # mm, the line length (X)
DPATCH_H = 4.0                           # mm, the fill height (Y)
DPATCH_PITCH = 0.3                       # mm between lines
DPATCH_GAP_X = 3.0                       # mm between patches along X
DPATCH_ROW_GAP = 3.0                     # mm between the two rows
DPATCH_FEED = 1500                       # the reference feed, 25 mm/s
DPATCH_CW_DOSES = (1.0, 0.8, 0.6, 0.5, 0.35, 0.25)   # row A: S1000 at F600/dose
DPATCH_DENSITY_PCT = (100, 80, 60, 45, 30)            # row B: at F600
DPATCH_SETTLE_S = 2.0                    # laser off before each patch
# The cooling engine interrogates the flow for cool_flow_check_s (50 s)
# from the moment the window arms, on a heater pulse judged by its rise;
# a CW patch under that window puts the tube's heat into the same loop
# and reads as a blocked flow (15.1 C against a 14.4 limit, 2026-08-25),
# which holds the job. The first patch therefore waits, spindle on and
# dark, until the check has finished on the heater alone. The mid-run
# re-check (cool_flow_recheck_s, 150 s by default) has to be pushed past
# the run's length for the session, or it lands on a patch the same way.
DPATCH_ARM_DWELL_S = 60.0
# The thermopile curves, light as a fraction of CW, for the labels: the
# period-20 density ladder (E3) and the analog duty ladder (E1, whose
# means above 50 % duty did not settle inside a line, so they are a prior).
DPATCH_TP_P20 = {100: 1.0, 80: 0.53, 60: 0.37, 45: 0.21, 30: 0.07}
DPATCH_TP_ANALOG = {100: 1.0, 80: 0.72, 60: 0.52, 45: 0.37, 30: 0.07}


def dpatch_gcode(feed, width, pitch, n):
    """One patch's cut moves and where they leave the head relative to
    the patch origin: serpentine G1 lines, the Y steps cut at the edge."""
    lines = []
    x = y = 0.0
    for i in range(n):
        dx = width if i % 2 == 0 else -width
        lines.append('G1 X%g F%d' % (dx, feed))
        x += dx
        if i < n - 1:
            lines.append('G1 Y%g F%d' % (pitch, feed))
            y += pitch
    return lines, x, y


def drill_dpatch(g):
    feed_ref = int(sys.argv[2]) if len(sys.argv) > 2 else DPATCH_FEED
    pitch = float(sys.argv[3]) if len(sys.argv) > 3 else DPATCH_PITCH
    width = float(sys.argv[4]) if len(sys.argv) > 4 else DPATCH_W
    if feed_ref < 60 or not 0.05 <= pitch <= 2.0 or not 5.0 <= width <= 200.0:
        print('usage: dpatch [F mm/min >= 60] [pitch 0.05..2 mm] [length 5..200 mm]')
        return 2
    model = 'density'                   # the only model on hardware
    tp_curve = DPATCH_TP_P20
    unit = 'density'
    levels, (rpm_max, rpm_min, floor, ceil) = pcurve_levels(g, DPATCH_DENSITY_PCT)
    if levels is None:
        print('PRECONDITION FAILED: cannot read $30/$31/$35/$36 (%s/%s/%s/%s)'
              % (rpm_max, rpm_min, floor, ceil))
        return 2
    n_lines = int(round(DPATCH_H / pitch))
    period = (conf_get('laser_pulse_ticks') or 'driver default') if model == 'density' else None
    print('=== depth witness: CW patches at speed against %s patches at F%d ==='
          % (unit, feed_ref))
    print('mapping: $30=%g $31=%g $35=%g $36=%g; model %s%s'
          % (rpm_max, rpm_min, floor, ceil, model,
             (', base period %s ticks' % period) if period else ''))
    patches = []
    for dose in DPATCH_CW_DOSES:
        feed = int(round(feed_ref / dose))
        patches.append({'row': 'A', 'pct': 100, 's': int(rpm_max), 'feed': feed,
                        'dose': dose, 'tp_says': None,
                        'label': 'CW, dose %.2f' % dose})
    for pct, sval, level in levels:
        patches.append({'row': 'B', 'pct': pct, 's': sval, 'feed': feed_ref,
                        'dose': None, 'tp_says': tp_curve.get(pct),
                        'label': '%d%% %s (level %.2f%%), thermopile says %.2f of CW'
                        % (pct, unit, 100.0 * level, tp_curve.get(pct, float('nan')))})
    n_a = len(DPATCH_CW_DOSES)
    print('patches %g x %g mm, %d lines at %g mm pitch, %g mm apart along +X;'
          % (width, DPATCH_H, n_lines, pitch, DPATCH_GAP_X))
    print('row A starts at the head, row B %g mm above it:' % (DPATCH_H + DPATCH_ROW_GAP))
    for i, p in enumerate(patches):
        print('  %s%d: S%-4d F%-5d %s' % (p['row'], i + 1, p['s'], p['feed'], p['label']))
    sampler = Sampler(PCURVE_SAMPLE_HZ)
    print('connect: %s' % prepare(g))
    print('pre-fire: %s' % sample_forgectrl())
    arm_cue()
    print('>>> A fresh area of the stock: %g mm along +X by %g mm along +Y'
          % (n_a * (width + DPATCH_GAP_X), 2 * DPATCH_H + DPATCH_ROW_GAP))
    print('>>> from the head. After your press the head waits %g s dark while the'
          % DPATCH_ARM_DWELL_S)
    print('>>> flow check runs. Row A is CW at full power, up to %.0f s per patch.\n'
          % (n_lines * (width / feed_ref * 60.0 + 0.1)))
    print('G91/G21: %s / %s' % (g.cmd('G91'), g.cmd('G21')))
    sampler.start()
    done = []
    aborted = None
    row_x = 0.0
    try:
        for i, p in enumerate(patches):
            if i == n_a:
                # Row B starts above row A's first patch.
                g.s.sendall(('G0 X%g Y%g\n' % (-row_x, DPATCH_H + DPATCH_ROW_GAP)).encode())
                g.wait_state('Idle', 30)
                row_x = 0.0
            lines, dx, dy = dpatch_gcode(p['feed'], width, pitch, n_lines)
            est_s = n_lines * (width / p['feed'] * 60.0 + 0.25) + 2.0
            time.sleep(DPATCH_SETTLE_S)
            t_m3 = time.time()
            dwell = ['G4 P%g' % DPATCH_ARM_DWELL_S] if i == 0 else []
            for ln in ['M3 S%d' % p['s']] + dwell + lines + ['M5']:
                g.s.sendall(ln.encode() + b'\n')
            # The controller reports Idle inside the dwell, so the first Run
            # seen after the press is the first line of the first patch, and
            # the wait covers the button timeout plus the dwell.
            if i == 0:
                print('  waiting for the press, then %g s dark for the flow check'
                      % DPATCH_ARM_DWELL_S)
            st = g.wait_state('Run', 300 + DPATCH_ARM_DWELL_S if i == 0 else 60)
            if not st.startswith('Run'):
                aborted = ('patch %d never ran (state=%s): arm refused, no press, '
                           'or the controller alarmed' % (i + 1, st))
                break
            t_run0 = time.time()
            st = g.wait_state('Idle', est_s + 60.0, poll=0.05)
            t_run1 = time.time()
            if not st.startswith('Idle'):
                cs = sample_forgectrl() or {}
                aborted = ('patch %d did not finish (state=%s; cooling verdict %s, %r)'
                           % (i + 1, st, cs.get('verdict'), cs.get('reason')))
                break
            if t_run1 - t_run0 < 0.5 * est_s:
                # Cut short: canceled, and a cancel may have moved the
                # head, so every relative move from here is aimed blind.
                aborted = ('patch %d ran %.1f s of ~%.0f: canceled; no further '
                           'moves sent' % (i + 1, t_run1 - t_run0, est_s))
                break
            p.update({'t_m3': t_m3, 't_run0': t_run0, 't_run1': t_run1})
            done.append(p)
            print('  %s%d: S%-4d F%-5d ran %.1f s' % (p['row'], i + 1, p['s'], p['feed'],
                                                     t_run1 - t_run0))
            # Back to the patch origin, then along to the next one.
            g.s.sendall(('G0 X%g Y%g\n' % (-dx, -dy)).encode())
            g.s.sendall(('G0 X%g\n' % (width + DPATCH_GAP_X)).encode())
            row_x += width + DPATCH_GAP_X
            g.wait_state('Idle', 30)
    finally:
        try:
            g.cmd('M5', timeout=1)
        except Exception:
            pass
        if aborted:
            g.rt(b'\x18')                   # abort out of whatever it is in
        else:
            g.s.sendall(b'G90\nM2\n')       # program end closes the window
        time.sleep(1.5)
        sampler.stop()
    if aborted:
        print('ABORTED: %s' % aborted)
    tr = sampler.samples
    print('\nsampler: %d samples, %.1f Hz achieved, %d read errors'
          % (len(tr), sampler.rate(), sampler.errors))
    if not done:
        return 1
    print('\n--- per patch (steady window: first 1 s and last 0.5 s dropped) ---')
    print('  patch  S     F      hv mean   max  | tp delta   base   | lon')
    for i, p in enumerate(done):
        base = _stats(_window(tr, p['t_m3'] - DPATCH_SETTLE_S + 0.5, p['t_m3'], 'tp'))['mean']
        hv = _stats(_window(tr, p['t_run0'] + 1.0, p['t_run1'] - 0.5, 'hv'))
        tp = _stats(_window(tr, p['t_run0'] + 1.0, p['t_run1'] - 0.5, 'tp'))
        lon = max(_window(tr, p['t_run0'], p['t_run1'], 'lon') or [0])
        p['hv_mean'], p['hv_max'], p['tp_base'] = hv['mean'], hv['max'], base
        p['tp_delta'] = None if (tp['mean'] is None or base is None) else tp['mean'] - base
        p['lon_peak'] = lon
        print('  %s%-2d    %-5d %-6d %7s  %5s | %8s  %7s | %3d'
              % (p['row'], i + 1, p['s'], p['feed'], _fmt(hv['mean']), _fmt(hv['max'], 0),
                 _fmt(p['tp_delta']), _fmt(base), lon))
    row_a = [p['tp_delta'] for p in done if p['row'] == 'A' and p['tp_delta'] is not None]
    if len(row_a) >= 2:
        print('row A thermopile (CW at six feeds, the beam does not know the speed): '
              'min %.0f max %.0f, spread %.1f%% of the mean'
              % (min(row_a), max(row_a), 100.0 * (max(row_a) - min(row_a)) / (sum(row_a) / len(row_a))))
    print('\n--- the match to make by eye, in the middle of each patch ---')
    doses = [(p['dose'], i + 1) for i, p in enumerate(done) if p['row'] == 'A']
    for i, p in enumerate(done):
        if p['row'] != 'B' or p['tp_says'] is None:
            continue
        nearest = min(doses, key=lambda d: abs(d[0] - p['tp_says'])) if doses else None
        print('  B%d (%d%% %s): the thermopile says it should match A%d (dose %.2f); '
              'deeper than that and the tube gives more than the sensor reads'
              % (i + 1, p['pct'], unit, nearest[1], nearest[0]) if nearest else
              '  B%d (%d%% %s): no row A to match' % (i + 1, p['pct'], unit))
    record = {
        'drill': 'dpatch', 'date': time.strftime('%Y-%m-%dT%H:%M:%S'),
        'model': model, 'period': period, 'feed_ref': feed_ref,
        'patch_mm': [width, DPATCH_H], 'pitch_mm': pitch,
        'settings': {'$30': rpm_max, '$31': rpm_min, '$35': floor, '$36': ceil},
        'sampler': {'local': sampler.local, 'hz': sampler.rate(),
                    'samples': len(tr), 'errors': sampler.errors},
        'aborted': aborted, 'patches': done, 'trace': tr,
    }
    ddir = os.environ.get('FORGETEST_BENCH_DATA') or ('/tmp' if sampler.local else os.getcwd())
    path = os.path.join(ddir, 'dpatch_%s.json' % time.strftime('%Y%m%d-%H%M%S'))
    try:
        with open(path, 'w') as f:
            json.dump(record, f, indent=1)
        print('record: %s' % path)
    except OSError as e:
        print('record not written: %s' % e)
    return 0


# --- cooling under laser load: the flow check with the tube lit --------------

# Test 1 reproduces the 2026-08-25 trip: the arm-time heater check (50 s at
# cool_flow_heater_pct, judged on the downstream rise against cool_flow_rise)
# ran while the first dpatch patch fired CW through its window and read 15.1 C
# against 14.4, a SUSPECT that held the job. The defaults go back into the
# conf for the run and the burst starts on the press with no dark dwell, so
# the tube is lit for most of the window. Test 2 turns the check off for the
# run and fires one burst of a chosen length, so the trace holds the tube's
# heat alone: the lag to the sensors, the rise per raw-second of hv_current,
# and its shape (a step at fire start is electrical, a lagged ramp is
# thermal). Each invocation is ONE armed run; the conf keys it writes are put
# back when the run ends, and the engine re-reads them at the next run start.
# The pump is never commanded off here.
# The burst is a serpentine fill at the dpatch reference dose (25 mm/s, 0.3 mm
# lines: an engrave, not a burn), sized by the seconds the tube is to be lit.
FLOWLOAD_FEED = 1500                     # mm/min, the dpatch reference dose
FLOWLOAD_PITCH = 0.3                     # mm between lines
FLOWLOAD_W = 30.0                        # mm, the line length (X)
FLOWLOAD_Y_STEP_S = 0.1                  # about, per pitch step at the edge
FLOWLOAD_T1_H = 8.0                      # mm: two 30 x 4 mm fills, back to back
FLOWLOAD_T1_KEYS = (('cool_flow_check_s', '50'), ('cool_flow_rise', '14.4'),
                    ('cool_recheck_s', '150'))
FLOWLOAD_T2_KEYS = (('cool_flow_check_s', '0'),)
FLOWLOAD_T2_SECS = (20, 40, 60)          # the plan's CW bursts
FLOWLOAD_BASE_S = 15.0                   # baseline before the first emission
FLOWLOAD_TAIL_S = {'t1': 40.0, 't2': 90.0}   # sampled after the window closes
FLOWLOAD_BIN_S = 5.0                     # the report's bins
FLOWLOAD_RESP_BIN_S = 2.0                # the lag search's bins
FLOWLOAD_RESP_C = 0.3                    # a sensor has responded past this rise
FLOWLOAD_STEP_S = 4.0                    # a rise inside this of fire start is a step
FLOWLOAD_MARGIN_C = 1.6                  # healthy max 12.75 C to the 14.4 limit
FLOWLOAD_NOISE_C = 0.11                  # settled-loop split-half noise
FLOWLOAD_PRESS_WAIT_S = 300.0            # the operator's button timeout
FLOWLOAD_CHANNELS = PCURVE_CHANNELS + (('htr', 'thermal/heater_pwm'),)
# The engine's own verdict lines, as /cool/status "reason" carries them.
FLOWLOAD_RISE_RE = re.compile(
    r'heater rise ([0-9.]+) C(?: \(limit ([0-9.]+), dT ([0-9.]+)|, dT ([0-9.]+) C)')


class CoolPoller:
    """GET /cool/status once a second in a thread: the engine's phase,
    verdict, hold, reason (its last warning, where a check's rise and dT
    land) and its own view of both sensors."""

    def __init__(self):
        import threading
        self.samples = []
        self.errors = 0
        self._stop = threading.Event()
        self._thr = threading.Thread(target=self._run, daemon=True)

    def start(self):
        self._thr.start()

    def stop(self):
        self._stop.set()
        self._thr.join(timeout=3)

    def _run(self):
        keys = ('phase', 'verdict', 'hold', 'reason', 'down_c', 'up_c',
                'armed', 'fire_ok', 'fan_gates')
        next_t = time.time()
        while not self._stop.is_set():
            smp = {'t': time.time()}
            try:
                cs = get_json('/cool/status')
                for k in keys:
                    smp[k] = cs.get(k)
            except Exception:
                self.errors += 1
            self.samples.append(smp)
            next_t += 1.0
            delay = next_t - time.time()
            if delay > 0:
                time.sleep(delay)
            else:
                next_t = time.time()


def conf_del(key):
    """Remove one key, preserving every other line."""
    try:
        with open(CONF) as f:
            lines = f.read().splitlines()
    except OSError:
        return False
    out = [ln for ln in lines
           if not ('=' in ln.split('#', 1)[0]
                   and ln.split('#', 1)[0].split('=', 1)[0].strip() == key)]
    try:
        mode = os.stat(CONF).st_mode & 0o777
        with open(CONF + '.tmp', 'w') as f:
            f.write('\n'.join(out) + '\n')
        os.chmod(CONF + '.tmp', mode)
        os.replace(CONF + '.tmp', CONF)
    except OSError:
        return False
    return True


def conf_push(keys):
    """Write the (key, value) pairs, returning what stood before so
    conf_pop can put it back; None when the conf is not ours to write."""
    prior = []
    for key, val in keys:
        prior.append((key, conf_get(key)))
        if not conf_set(key, val):
            conf_pop(prior)
            return None
    return prior


def conf_pop(prior):
    ok = True
    for key, val in prior:
        ok = (conf_del(key) if val is None else conf_set(key, val)) and ok
    return ok


def flowload_burst(secs, feed=FLOWLOAD_FEED, width=FLOWLOAD_W,
                   pitch=FLOWLOAD_PITCH):
    """A serpentine fill that keeps the tube lit for about `secs`, and
    the seconds it should take."""
    line_s = width / (feed / 60.0) + FLOWLOAD_Y_STEP_S
    n = max(1, int(round(secs / line_s)))
    lines, dx, dy = dpatch_gcode(feed, width, pitch, n)
    return lines, dx, dy, n * line_s


def flowload_verdicts(poll):
    """Every change of the engine's verdict/hold/reason seen in the poll,
    with the time it first appeared, and the rise, limit and dT parsed
    where the line carries them."""
    out, last = [], None
    for s in poll:
        if 'verdict' not in s:
            continue
        key = (s.get('verdict'), s.get('hold'), s.get('reason'))
        if key == last:
            continue
        last = key
        ev = {'t': s['t'], 'verdict': s.get('verdict'), 'hold': s.get('hold'),
              'reason': s.get('reason')}
        m = FLOWLOAD_RISE_RE.search(s.get('reason') or '')
        if m:
            ev['rise'] = float(m.group(1))
            ev['limit'] = float(m.group(2)) if m.group(2) else None
            ev['dt'] = float(m.group(3) or m.group(4))
        out.append(ev)
    return out


FLOWLOAD_STEP_C = 0.45                   # a common-mode level change this big
FLOWLOAD_STEP_AGREE_C = 0.4              # ... on both sensors, agreeing this well
FLOWLOAD_STEP_WIN = 12                   # samples (0.5 s at 25 Hz) each side
FLOWLOAD_STEP_GAP = 2                    # samples skipped around the edge


def flowload_steps(tr):
    """The common-mode steps in the trace: the mean level of BOTH sensors
    changing by FLOWLOAD_STEP_C or more, the same way and by the same
    amount, between the half second before a sample and the half second
    after it. The coolant ADC carries an offset of about 1 C while the
    run airflow profile is on, stepping in at the session open, out when
    the fans go idle and toggling in between, on both sensors together.
    The tube's heat is a ramp a hundred times slower, and the heater's
    rise reaches one sensor only, so neither passes. Returns
    [(t, d_down_c, d_up_c)] with the step sizes in degrees."""
    pts = []
    for s in tr:
        if s.get('wt1') is None or s.get('wt2') is None:
            continue
        d, u = _degc(s['wt1']), _degc(s['wt2'])
        if d == d and u == u:
            pts.append((s['t'], d, u))
    out = []
    w, gap = FLOWLOAD_STEP_WIN, FLOWLOAD_STEP_GAP
    i = w + gap
    while i < len(pts) - w - gap:
        pre = pts[i - gap - w:i - gap]
        post = pts[i + gap:i + gap + w]
        dd = sum(p[1] for p in post) / w - sum(p[1] for p in pre) / w
        du = sum(p[2] for p in post) / w - sum(p[2] for p in pre) / w
        if abs(dd) >= FLOWLOAD_STEP_C and abs(du) >= FLOWLOAD_STEP_C \
                and (dd > 0) == (du > 0) and abs(dd - du) <= FLOWLOAD_STEP_AGREE_C:
            out.append((pts[i][0], dd, du))
            i += w + 2 * gap                # one step per edge
        else:
            i += 1
    return out


def flowload_temps(tr, poll, local, steps=None):
    """(t, down_c, up_c): the sampler's counts through the factory
    conversion on the board, else the engine's own 1 Hz view. With
    `steps` (flowload_steps), every step is subtracted from all later
    samples, so the series carries the thermal signal alone."""
    if local and _degc(0) is not None:
        out = []
        steps = list(steps or [])
        off_d = off_u = 0.0
        for s in tr:
            if s.get('wt1') is None or s.get('wt2') is None:
                continue
            d, u = _degc(s['wt1']), _degc(s['wt2'])
            if d != d or u != u:                # NaN
                continue
            while steps and steps[0][0] <= s['t']:
                _t, dd, du = steps.pop(0)
                off_d += dd
                off_u += du
            out.append((s['t'], d - off_d, u - off_u))
        if out:
            return out
    return [(p['t'], p['down_c'], p['up_c']) for p in poll
            if p.get('down_c') is not None and p.get('up_c') is not None]


def _tmean(series, t0, t1, idx):
    vals = [s[idx] for s in series if t0 <= s[0] < t1]
    return sum(vals) / len(vals) if vals else None


def _tsd(series, t0, t1, idx):
    vals = [s[idx] for s in series if t0 <= s[0] < t1]
    return _stats(vals)['sd'] if len(vals) > 1 else None


def flowload_lit(tr):
    """(first, last) sample time with the tube lit, from the digital
    witness or a current above the dark ceiling; (None, None) if never."""
    ts = [s['t'] for s in tr
          if (s.get('lon') or 0) > 0 or (s.get('hv') or 0) > HV_DARK_MAX]
    return (ts[0], ts[-1]) if ts else (None, None)


def flowload_dose(tr, t0, t1):
    """The integral of hv_current from t0 to t1 in raw-seconds, and the
    mean current over the lit samples."""
    pts = [(s['t'], s['hv']) for s in tr
           if t0 <= s['t'] <= t1 and s.get('hv') is not None]
    dose = 0.0
    for (ta, ha), (tb, hb) in zip(pts, pts[1:]):
        dose += 0.5 * (ha + hb) * (tb - ta)
    lit = [h for _t, h in pts if h > HV_DARK_MAX]
    return dose, (sum(lit) / len(lit) if lit else 0.0)


def flowload_response(temps, idx, t_fire0, t_end, base, sd):
    """How one sensor answered the burst: the lag to its first bin over
    the response line, the rise inside the step window, the rise at
    t_end and the peak (value, time) up to the end of the record."""
    thr = base + max(FLOWLOAD_RESP_C, 3.0 * (sd or 0.0))
    lag = None
    t = t_fire0
    while t < temps[-1][0]:
        m = _tmean(temps, t, t + FLOWLOAD_RESP_BIN_S, idx)
        if m is not None and m > thr:
            lag = t - t_fire0
            break
        t += FLOWLOAD_RESP_BIN_S
    step = _tmean(temps, t_fire0 + 1.0, t_fire0 + FLOWLOAD_STEP_S, idx)
    at_end = _tmean(temps, t_end - FLOWLOAD_RESP_BIN_S, t_end, idx)
    at_50 = _tmean(temps, t_fire0 + 48.0, t_fire0 + 50.0, idx)
    peak, t_peak = None, None
    t = t_fire0
    while t < temps[-1][0]:
        m = _tmean(temps, t, t + FLOWLOAD_RESP_BIN_S, idx)
        if m is not None and (peak is None or m > peak):
            peak, t_peak = m, t
        t += FLOWLOAD_RESP_BIN_S
    return {
        'lag_s': lag,
        'step_c': None if step is None else step - base,
        'end_c': None if at_end is None else at_end - base,
        'r50_c': None if at_50 is None else at_50 - base,
        'peak_c': None if peak is None else peak - base,
        't_peak_s': None if t_peak is None else t_peak - t_fire0,
    }


def flowload_bins(tr, temps, t0, t1, t_ref):
    """The report's table: per FLOWLOAD_BIN_S bin, seconds from t_ref,
    both sensors, dT, the current's mean, the lit fraction, the heater."""
    print('    t(s)   down C    up C    dT C   hv mean  lit  heater')
    t = t0
    while t < t1:
        d = _tmean(temps, t, t + FLOWLOAD_BIN_S, 1)
        u = _tmean(temps, t, t + FLOWLOAD_BIN_S, 2)
        hv = _stats(_window(tr, t, t + FLOWLOAD_BIN_S, 'hv'))['mean']
        lon = _window(tr, t, t + FLOWLOAD_BIN_S, 'lon')
        lit = (sum(1 for v in lon if v > 0) / float(len(lon))) if lon else None
        htr = _stats(_window(tr, t, t + FLOWLOAD_BIN_S, 'htr'))['mean']
        print('  %6.0f  %7s  %7s  %6s  %7s  %4s  %6s'
              % (t - t_ref, _fmt(d, 2), _fmt(u, 2),
                 _fmt(None if d is None or u is None else d - u, 2),
                 _fmt(hv, 0), '' if lit is None else '%.2f' % lit,
                 _fmt(None if htr is None else 100.0 * htr / 65535.0, 0)))
        t += FLOWLOAD_BIN_S


FLOWLOAD_RX_MARGIN = 96                  # bytes kept free in the RX buffer


def _bf_free(st):
    """(free planner blocks, free RX characters) from a status report, or
    (None, None) when it carries no Bf field."""
    for fld in st.strip('<>').split('|'):
        if fld.startswith('Bf:'):
            try:
                a, b = fld[3:].split(',')
                return int(a), int(b)
            except ValueError:
                return None, None
    return None, None


def flowload_feed(g, pending, free_chars):
    """Send lines from `pending` while the controller's RX buffer has
    room for them, and return the room left. A whole fill written at
    once overruns the 1023-byte RX buffer and loses lines (the 60 s
    fill ran 46 s), so the job is fed against the Bf field instead."""
    while pending and free_chars is not None \
            and free_chars - (len(pending[0]) + 1) >= FLOWLOAD_RX_MARGIN:
        ln = pending.pop(0)
        g.s.sendall(ln.encode() + b'\n')
        free_chars -= len(ln) + 1
    return free_chars


def flowload_watch(g, est_s, pending):
    """Follow the controller from the press to the end of the burst,
    feeding `pending` lines as the RX buffer frees. Returns (outcome,
    timeline): 'done' on Idle after Run with every line sent, 'held'
    when a Hold or Door interrupted it (the SUSPECT hold looks like
    this), 'alarm', 'no-run' when the press never came, 'timeout'."""
    timeline = []
    last = None
    t0 = time.time()
    ran = False
    t_run0 = None
    while True:
        st = g.status()
        name = st[1:].split('|')[0].split(':')[0] if st else ''
        flowload_feed(g, pending, _bf_free(st)[1])
        now = time.time()
        if name != last:
            timeline.append({'t': now, 'state': st})
            last = name
        if name == 'Run':
            if not ran:
                t_run0 = now
            ran = True
        elif name in ('Hold', 'Door'):
            time.sleep(1.0)
            if g.state().split(':')[0] in ('Hold', 'Door'):
                return 'held', timeline
        elif name == 'Alarm':
            return 'alarm', timeline
        elif name == 'Idle' and ran and not pending and now - t_run0 > 1.0:
            time.sleep(0.5)
            if g.state().startswith('Idle'):
                return 'done', timeline
        if not ran and now - t0 > FLOWLOAD_PRESS_WAIT_S + 10:
            return 'no-run', timeline
        if ran and now - t_run0 > est_s + 60.0:
            return 'timeout', timeline
        time.sleep(0.1)


def flowload_run(g, test, secs, pct, keys):
    """One armed run: conf keys in, baseline, the burst on the press, the
    window closed on M2, the tail, conf keys back. Returns the record."""
    model = conf_get('laser_power_model') or 'density'
    levels, (rpm_max, rpm_min, floor, ceil) = pcurve_levels(g, (pct,))
    if levels is None:
        print('PRECONDITION FAILED: cannot read $30/$31/$35/$36 (%s/%s/%s/%s)'
              % (rpm_max, rpm_min, floor, ceil))
        return None
    _pct, s_val, level = levels[0]
    if pct < 100 and model != 'density':
        print('a %d %% burst is a density burst; laser_power_model is %s' % (pct, model))
        return None
    if test == 't1':
        n = int(round(FLOWLOAD_T1_H / FLOWLOAD_PITCH))
        lines, dx, dy = dpatch_gcode(FLOWLOAD_FEED, FLOWLOAD_W, FLOWLOAD_PITCH, n)
        est_s = n * (FLOWLOAD_W / (FLOWLOAD_FEED / 60.0) + FLOWLOAD_Y_STEP_S)
        shape = ('two %g x %g mm fills back to back (%d lines at %g mm), about %.0f s lit'
                 % (FLOWLOAD_W, FLOWLOAD_T1_H / 2, n, FLOWLOAD_PITCH, est_s))
    else:
        lines, dx, dy, est_s = flowload_burst(secs)
        n = len([ln for ln in lines if 'X' in ln])
        shape = ('a %g x %.1f mm fill (%d lines at %g mm), about %.0f s lit'
                 % (FLOWLOAD_W, n * FLOWLOAD_PITCH, n, FLOWLOAD_PITCH, est_s))
    print('=== flowload %s: %s ===' % (test, shape))
    print('burst: S%d = %s (level %.2f of full, %s model; $30=%g $31=%g $35=%g $36=%g)'
          % (s_val, 'CW' if pct >= 100 else '%d %% density' % pct, level, model,
             rpm_max, rpm_min, floor, ceil))
    prior = conf_push(keys)
    if prior is None:
        print('REFUSED: cannot write %s (the check state for this run must be known; '
              'run on the board)' % CONF)
        return None
    print('conf for this run: %s (was: %s)'
          % (', '.join('%s=%s' % kv for kv in keys),
             ', '.join('%s=%s' % (k, v if v is not None else '<unset>') for k, v in prior)))
    sampler = Sampler(PCURVE_SAMPLE_HZ, channels=FLOWLOAD_CHANNELS)
    poller = CoolPoller()
    outcome, timeline, aborted = None, [], None
    t_m3 = t_close = None
    try:
        print('connect: %s' % prepare(g))
        # The driver arms on the first laser-on of a job only while its own
        # spindle state reads off; a job whose M5 was lost leaves it on and
        # the next M3 runs unarmed. Spindle off first, and no run against a
        # window that is still open.
        print('spindle off: %s' % g.cmd('M5'))
        pre = sample_forgectrl()
        print('pre-fire: %s' % pre)
        if pre is None or pre.get('armed'):
            print('REFUSED: the armed window is still open (or forgectrl is unreachable); '
                  'close it before another run')
            return None
        sampler.start()
        poller.start()
        print('baseline: %g s dark and idle' % FLOWLOAD_BASE_S)
        time.sleep(FLOWLOAD_BASE_S + 2.0)
        arm_cue()
        print('>>> A fresh area of scrap: %g mm along +X by %.1f mm along +Y from'
              % (FLOWLOAD_W, dy + FLOWLOAD_PITCH))
        print('>>> the head. The burst starts ON YOUR PRESS with no dark dwell'
              + (' and the flow check\n>>> runs under it.' if test == 't1'
                 else ';\n>>> the flow check is OFF for this run.'))
        print('>>> Up to %.0f s of %s.\n'
              % (est_s, 'CW at full power' if pct >= 100 else '%d %% density' % pct))
        print('G91/G21: %s / %s' % (g.cmd('G91'), g.cmd('G21')))
        t_m3 = time.time()
        pending = ['M3 S%d' % s_val] + lines + ['M5']
        n_job = len(pending)
        flowload_feed(g, pending, _bf_free(g.status())[1])
        print('  waiting for the press (up to %.0f s), then the burst (%d of %d lines '
              'queued, the rest fed as the buffer frees)'
              % (FLOWLOAD_PRESS_WAIT_S, n_job - len(pending), n_job))
        outcome, timeline = flowload_watch(g, est_s, pending)
        if pending:
            print('  %d lines were never sent' % len(pending))
        if outcome == 'done':
            g.s.sendall(('G0 X%g Y%g\n' % (-dx, -dy)).encode())   # back to the origin
            g.wait_state('Idle', 30)
            if test == 't1':
                # M2 closes the window and the window's end stops the
                # check, so a check still running (the heater on) gets
                # its full window before the M2: the engine's verdict is
                # what this test is for.
                limit = time.time() + float(dict(keys)['cool_flow_check_s']) + 20.0
                print('  burst done; holding the window open until the heater check ends')
                while time.time() < limit:
                    smp = sampler.samples[-250:]
                    if smp and smp[-1].get('htr') == 0 \
                            and any((x.get('htr') or 0) > 0 for x in smp):
                        break                       # the check ran and ended
                    if smp and smp[-1].get('htr') == 0 and time.time() - t_m3 > 20.0 \
                            and not any((x.get('htr') or 0) > 0 for x in sampler.samples):
                        break                       # the check never started
                    time.sleep(0.5)
                time.sleep(2.0)                     # the verdict's poll after the heater
            print('  M2: %s' % g.cmd('G90\nM2', timeout=10))      # closes the window
            t_close = time.time()
            closed = None
            for _i in range(15):
                cs = sample_forgectrl()
                if cs and not cs.get('armed'):
                    closed = time.time() - t_close
                    break
                time.sleep(1.0)
            if closed is None:
                print('  !!! the armed window did NOT close after M2 (armed still true '
                      'after 15 s): the job did not end as sent')
            else:
                print('  window closed %.0f s after M2, sampling the %.0f s tail'
                      % (closed, FLOWLOAD_TAIL_S[test]))
        else:
            cs = sample_forgectrl() or {}
            aborted = ('%s (state %s; cooling verdict %s, %r); soft reset, no further moves'
                       % (outcome, timeline[-1]['state'] if timeline else '?',
                          cs.get('verdict'), cs.get('reason')))
            g.rt(b'\x18')
            t_close = time.time()
            print('  %s' % aborted)
            print('  sampling the %.0f s tail anyway' % FLOWLOAD_TAIL_S[test])
        time.sleep(FLOWLOAD_TAIL_S[test])
    finally:
        try:
            g.cmd('M5', timeout=1)
        except Exception:
            pass
        sampler.stop()
        poller.stop()
        if conf_pop(prior):
            print('conf restored')
        else:
            print('CONF NOT RESTORED: put back by hand: %s'
                  % ', '.join('%s=%s' % (k, v if v is not None else '<unset>')
                              for k, v in prior))
    tr, poll = sampler.samples, poller.samples
    g.drain()
    print('\nsampler: %d samples, %.1f Hz achieved, %d read errors; /cool/status %d polls, %d errors'
          % (len(tr), sampler.rate(), sampler.errors, len(poll), poller.errors))
    replies = [(t, ln) for t, ln in g.log if t >= (t_m3 or 0) - 30.0]
    n_ok = sum(1 for _t, ln in replies if ln == 'ok')
    print('controller replies since 30 s before M3: %d ok; everything else:' % n_ok)
    for t, ln in replies:
        if ln != 'ok' and not re.match(r'\$\d+=', ln):   # not the settings dump
            print('  %+7.1f s  %s' % (t - (t_m3 or t), ln))
    return {
        'replies': replies,
        'drill': 'flowload', 'test': test, 'date': time.strftime('%Y-%m-%dT%H:%M:%S'),
        'model': model, 'burst_s': secs if test == 't2' else None, 'pct': pct,
        's': s_val, 'level': level, 'feed': FLOWLOAD_FEED, 'pitch_mm': FLOWLOAD_PITCH,
        'width_mm': FLOWLOAD_W, 'lines': n, 'est_lit_s': est_s,
        'settings': {'$30': rpm_max, '$31': rpm_min, '$35': floor, '$36': ceil},
        'conf': dict(keys), 'conf_prior': dict(prior),
        'sampler': {'local': sampler.local, 'hz': sampler.rate(),
                    'samples': len(tr), 'errors': sampler.errors},
        't_m3': t_m3, 't_close': t_close, 'outcome': outcome, 'aborted': aborted,
        'timeline': timeline, 'events': flowload_verdicts(poll),
        'trace': tr, 'poll': poll,
    }


def flowload_analyze(rec):
    """Print the run's reading and put the numbers into the record."""
    tr, poll, test = rec['trace'], rec['poll'], rec['test']
    steps = flowload_steps(tr) if rec['sampler']['local'] and _degc(0) is not None else []
    temps = flowload_temps(tr, poll, rec['sampler']['local'], steps)
    t_fire0, t_fire1 = flowload_lit(tr)
    rec['offset_steps'] = steps
    if steps:
        net_d = sum(dd for _t, dd, _du in steps)
        print('--- ADC offset steps masked: %d common-mode steps (first %+.2f/%+.2f C at %+.1f s '
              'from M3, net %+.2f C over the record) ---'
              % (len(steps), steps[0][1], steps[0][2], steps[0][0] - rec['t_m3'], net_d))
    print('\n--- engine events (verdict / hold / reason, as /cool/status showed them) ---')
    for ev in rec['events']:
        print('  %+7.1f s  %-8s hold=%-5s %s'
              % (ev['t'] - (t_fire0 or rec['t_m3']), ev['verdict'], ev['hold'],
                 ev['reason'] or ''))
    print('--- controller ---')
    for s in rec['timeline']:
        print('  %+7.1f s  %s' % (s['t'] - (t_fire0 or rec['t_m3']), s['state']))
    fans = [p for p in poll if p.get('fan_gates')]
    if fans:
        names = list(fans[0]['fan_gates'].keys())
        print('--- fan gates, reading per 5 s from the session open (floor: %s) ---'
              % ', '.join('%s %.0f' % (n, fans[0]['fan_gates'][n]['floor']) for n in names))
        t_open = rec['t_m3']
        for p in fans[::5]:
            if p['t'] < t_open - 5:
                continue
            print('  %+7.1f s  %s' % (p['t'] - (t_fire0 or t_open), '  '.join(
                '%s %5.0f %s' % (n[:8], p['fan_gates'][n]['reading'], p['fan_gates'][n]['state'][:4])
                for n in names)))
    if not temps:
        print('no temperature series (no sysfs and no engine readings): nothing to read')
        return
    if t_fire0 is None:
        print('the tube never lit: nothing to read')
        rec['lit'] = None
        return
    lit_s = t_fire1 - t_fire0
    dose, hv_mean = flowload_dose(tr, t_fire0, t_fire1 + 1.0)
    # The baseline is the settled loop before anything heated it: before
    # the heater when the check ran ahead of the fire (t1), else before
    # the fire.
    htr_on = [s['t'] for s in tr if (s.get('htr') or 0) > 0]
    t_base = min(t_fire0, htr_on[0]) if htr_on else t_fire0
    base = {i: _tmean(temps, t_base - FLOWLOAD_BASE_S, t_base - 0.5, i) for i in (1, 2)}
    sd = {i: _tsd(temps, t_base - FLOWLOAD_BASE_S, t_base - 0.5, i) for i in (1, 2)}
    rec['lit'] = {'t_fire0': t_fire0, 't_fire1': t_fire1, 'lit_s': lit_s,
                  'dose_raw_s': dose, 'hv_mean': hv_mean}
    rec['baseline'] = {'down_c': base[1], 'up_c': base[2],
                       'down_sd': sd[1], 'up_sd': sd[2]}
    print('--- the burst ---')
    print('  lit %.1f s (first to last lit sample), hv mean %.0f raw, dose %.0f raw-s'
          % (lit_s, hv_mean, dose))
    print('  baseline over the %g s before %s: down %s C (sd %s), up %s C (sd %s), dT %s'
          % (FLOWLOAD_BASE_S, 'the heater' if t_base < t_fire0 else 'fire',
             _fmt(base[1], 2), _fmt(sd[1], 2), _fmt(base[2], 2),
             _fmt(sd[2], 2), _fmt(None if None in base.values() else base[1] - base[2], 2)))
    if None in base.values():
        print('  no baseline: the series starts after the fire')
        return
    resp = {}
    for i, name in ((1, 'down'), (2, 'up')):
        resp[name] = flowload_response(temps, i, t_fire0, t_fire1, base[i], sd[i])
    rec['response'] = resp
    print('  response:  lag to +%.1f C   rise in %g s   rise at burst end   peak (at)'
          % (FLOWLOAD_RESP_C, FLOWLOAD_STEP_S))
    for name in ('down', 'up'):
        r = resp[name]
        print('    %-5s  %9s s       %7s C        %7s C        %7s C (%s s)'
              % (name, _fmt(r['lag_s'], 1), _fmt(r['step_c'], 2), _fmt(r['end_c'], 2),
                 _fmt(r['peak_c'], 2), _fmt(r['t_peak_s'], 0)))
    cm_step = [resp[n]['step_c'] for n in ('down', 'up') if resp[n]['step_c'] is not None]
    if cm_step and min(cm_step) > 1.0:
        shape = 'STEP on both sensors inside %g s of fire start: electrical, not thermal' % FLOWLOAD_STEP_S
    elif any(resp[n]['lag_s'] is not None for n in ('down', 'up')):
        shape = 'lagged ramp: thermal'
    else:
        shape = 'no response above the line inside the record'
    rec['shape'] = shape
    print('  shape: %s' % shape)

    if test == 't1':
        if htr_on:
            t_w0 = htr_on[0]
            after = [s['t'] for s in tr if s['t'] > t_w0 and s.get('htr') == 0]
            t_w1 = after[0] if after else tr[-1]['t']
            src = 'heater output'
        else:
            armed = [p['t'] for p in poll if p.get('armed')]
            t_w0 = armed[0] if armed else t_fire0
            t_w1 = t_w0 + float(rec['conf'].get('cool_flow_check_s', 50))
            src = 'the arm time (no heater trace)'
        lon = _window(tr, t_w0, t_w1, 'lon')
        lit_frac = (sum(1 for v in lon if v > 0) / float(len(lon))) if lon else None
        rise = None
        d0 = _tmean(temps, t_w0, t_w0 + 2.0, 1)
        d1 = _tmean(temps, t_w1 - 2.0, t_w1, 1)
        if d0 is not None and d1 is not None:
            rise = d1 - d0
        dts = [d - u for t, d, u in temps if t_w0 + 30.0 <= t < t_w1]
        dt = sum(dts) / len(dts) if dts else None
        eng = [ev for ev in rec['events'] if 'rise' in ev]
        rec['window'] = {'t0': t_w0, 't1': t_w1, 'source': src, 'lit_frac': lit_frac,
                         'trace_rise_c': rise, 'trace_dt_c': dt,
                         'engine_rise_c': eng[-1]['rise'] if eng else None,
                         'engine_dt_c': eng[-1]['dt'] if eng else None}
        print('--- the check window (%s): %.0f s, opened %+.1f s from fire start ---'
              % (src, t_w1 - t_w0, t_w0 - t_fire0))
        print('  lit for %s of the window; trace rise (down, last 2 s over first 2 s) %s C, '
              'dT after 30 s %s C' % ('' if lit_frac is None else '%.0f %%' % (100 * lit_frac),
                                      _fmt(rise, 1), _fmt(dt, 1)))
        if eng:
            print('  the engine read: rise %.1f C (limit %s), dT %.1f C  ->  %s'
                  % (eng[-1]['rise'], _fmt(eng[-1].get('limit'), 1), eng[-1]['dt'],
                     eng[-1]['verdict']))
        else:
            print('  the engine published no rise line inside the record (check deferred, '
                  'or the run ended first)')
        print('--- 5 s bins from 10 s before the window to 30 s after (t from fire start) ---')
        flowload_bins(tr, temps, t_w0 - 10.0, t_w1 + 30.0, t_fire0)
        print('--- reading the branch ---')
        print('  rise 11 to 12 C on all three runs: the check works under load; the '
              'defect is elsewhere.')
        print('  about 15 C, common-mode STEP at fire start: electrical (ADC/bias '
              'under the HV load).')
        print('  about 15 C, lagged common-mode RAMP: thermal; Test 2 sets k and the '
              'tracer design applies.')
        print('  no trip: the 22:11 run\'s own conditions; one trace-only t2 run closes it.')
    else:
        k = {}
        for name in ('down', 'up'):
            p = resp[name]['peak_c']
            k[name] = (p / dose) if (p is not None and dose > 0) else None
        rec['k_c_per_raw_s'] = k
        print('--- the tube\'s signature ---')
        for name in ('down', 'up'):
            if k[name] is None:
                print('  %s: no k (no peak or no dose)' % name)
                continue
            per_cw_s = k[name] * hv_mean
            print('  %-5s k = %.3g C per raw-s = %.3f C per lit second at hv %.0f; '
                  'a full 50 s window at this level adds %.2f C against the %.1f C margin'
                  % (name, k[name], per_cw_s, hv_mean, per_cw_s * 50.0, FLOWLOAD_MARGIN_C))
        pk = resp['down']['peak_c']
        if pk is not None:
            print('  down peak %.2f C at this dose is %.0fx the %.2f C settled noise'
                  % (pk, pk / FLOWLOAD_NOISE_C, FLOWLOAD_NOISE_C))
        print('--- 5 s bins from 15 s before fire to the end of the tail (t from fire start) ---')
        flowload_bins(tr, temps, t_fire0 - 15.0, temps[-1][0], t_fire0)
        print('  linearity across doses: run 20, 40 and 60 s, then `flowload fit`')


def flowload_fit():
    """Rise against dose across every t2 record in the bench data dir:
    k through the origin per sensor, and the fit's r2 as the linearity."""
    import glob
    ddir = os.environ.get('FORGETEST_BENCH_DATA') or os.getcwd()
    paths = sorted(glob.glob(os.path.join(ddir, 'flowload_t2_*.json')))
    if not paths:
        print('no flowload_t2_*.json under %s' % ddir)
        return 1
    import io
    import contextlib
    rows = []
    for p in paths:
        try:
            with open(p) as f:
                r = json.load(f)
        except (OSError, ValueError) as e:
            print('skip %s: %s' % (p, e))
            continue
        # Re-read every record from its trace, so the offset masking and
        # the current reading apply to records written before them.
        with contextlib.redirect_stdout(io.StringIO()):
            flowload_analyze(r)
        if not r.get('lit') or not r.get('response') or r['lit']['lit_s'] < 15.0:
            print('  %s: no usable burst (%s)' % (os.path.basename(p), r.get('outcome')))
            continue
        rows.append((os.path.basename(p), r))
    if not rows:
        return 1
    print('  offset steps masked per record; rises are the downstream sensor over its pre-burst baseline')
    print('  record                                pct   lit s   hv    dose raw-s   +50 s    end    peak (at)    steps')
    for name, r in rows:
        d = r['response']['down']
        print('  %-38s %4d  %6.1f  %4.0f  %10.0f  %6s  %6s  %6s (%3s s)  %3d'
              % (name, r['pct'], r['lit']['lit_s'], r['lit']['hv_mean'],
                 r['lit']['dose_raw_s'], _fmt(d['r50_c'], 2), _fmt(d['end_c'], 2),
                 _fmt(d['peak_c'], 2), _fmt(d['t_peak_s'], 0), len(r.get('offset_steps') or [])))
    for label, sel in (('CW', lambda r: r['pct'] >= 100), ('density', lambda r: r['pct'] < 100)):
        pts = [(r['lit']['dose_raw_s'], r['response']['down']['end_c'], r['lit']['lit_s'])
               for _n, r in rows if sel(r) and r['response']['down']['end_c'] is not None]
        if not pts:
            continue
        sxy = sum(d * c for d, c, _s in pts)
        sxx = sum(d * d for d, _c, _s in pts)
        k0 = sxy / sxx if sxx else 0.0
        hv = sum(r['lit']['hv_mean'] for _n, r in rows if sel(r)) / len([1 for _n, r in rows if sel(r)])
        line = ('  %s (%d run%s): rise at burst end = %.3g C per raw-s through the origin '
                '= %.4f C per lit second at hv %.0f; a fully lit 50 s window adds %.2f C '
                '(margin %.1f)' % (label, len(pts), '' if len(pts) == 1 else 's', k0, k0 * hv,
                                   hv, k0 * hv * 50.0, FLOWLOAD_MARGIN_C))
        fit = _linfit([d for d, _c, _s in pts], [c for _d, c, _s in pts]) if len(pts) >= 3 else None
        if fit:
            line += '; free fit intercept %.2f C, r2 %.3f' % (fit[0], fit[2])
        print(line)
    cw = [r for _n, r in rows if r['pct'] >= 100 and r['response']['down']['end_c']]
    dn = [r for _n, r in rows if r['pct'] < 100 and r['response']['down']['end_c']]
    if cw and dn:
        kc = sum(r['response']['down']['end_c'] / r['lit']['dose_raw_s'] for r in cw) / len(cw)
        kd = sum(r['response']['down']['end_c'] / r['lit']['dose_raw_s'] for r in dn) / len(dn)
        print('  density heat per raw-s is %.2f of CW: the current integral overstates '
              'density heat, one k per model' % (kd / kc))
    return 0


def drill_flowload(g):
    args = sys.argv[2:]
    test = args[0] if args else ''
    if test == 'fit':
        return flowload_fit()
    if test == 't1':
        if len(args) > 1:
            print('usage: flowload t1')
            return 2
        secs, pct, keys = None, 100, FLOWLOAD_T1_KEYS
    elif test == 't2':
        try:
            secs = float(args[1])
            pct = int(args[2]) if len(args) > 2 else 100
        except (IndexError, ValueError):
            secs, pct = 0, 0
        if not 5.0 <= secs <= 120.0 or not 1 <= pct <= 100:
            print('usage: flowload t2 <secs 5..120> [density pct 1..100]   e.g. flowload t2 60 45')
            return 2
        keys = FLOWLOAD_T2_KEYS
    else:
        print('usage: flowload t1 | flowload t2 <secs> [pct] | flowload fit')
        return 2
    rec = flowload_run(g, test, secs, pct, keys)
    if rec is None:
        return 2
    flowload_analyze(rec)
    ddir = os.environ.get('FORGETEST_BENCH_DATA') or ('/tmp' if rec['sampler']['local'] else os.getcwd())
    ts = time.strftime('%Y%m%d-%H%M%S')
    stem = ('flowload_t1_%s' % ts if test == 't1'
            else 'flowload_t2_%ds_%d_%s' % (int(secs), pct, ts))
    path = os.path.join(ddir, stem + '.json')
    try:
        with open(path, 'w') as f:
            json.dump(rec, f, indent=1)
        print('record: %s' % path)
    except OSError as e:
        print('record not written: %s' % e)
    return 0 if rec['outcome'] == 'done' else 1


# --- a sender change mid-job closes the window; the next laser-on prompts ----

# The driver arms at a laser-on only while the window is closed, and a
# sender change closes it whatever the spindle state (the consent belonged
# to the displaced session). This drill lights a 20 mm line at F60, drops
# the connection five seconds in with the tube lit, reconnects, lets the
# move finish dark (the Grbl expectation: the controller runs what it
# holds), then sends a fresh M3 and a 5 mm line: the controller must prompt
# for the button again, and nothing may fire between the drop and the
# second press. The material is the witness: the first mark ends where the
# connection dropped, the second line marks whole.
SENDERCHG_S = 400
SENDERCHG_LINE1 = 'G1 X20 F60'            # 20 s of motion
SENDERCHG_DROP_S = 5.0                    # lit seconds before the drop


def senderchg_wait_reply(g, needle, timeout):
    """Poll the socket until a reply containing `needle` arrives."""
    end = time.time() + timeout
    seen = len(g.log)
    while time.time() < end:
        g.drain()
        for _t, ln in g.log[seen:]:
            if needle in ln:
                return True
        seen = len(g.log)
        time.sleep(0.1)
    return False


def drill_senderchg(g):
    print('=== sender change mid-job: the next laser-on must prompt again ===')
    print('connect: %s' % prepare(g))
    print('spindle off: %s' % g.cmd('M5'))
    pre = sample_forgectrl()
    if pre is None or pre.get('armed'):
        print('REFUSED: the armed window is still open (or forgectrl is unreachable)')
        return 2
    sampler = Sampler(PCURVE_SAMPLE_HZ)
    poller = CoolPoller()
    sampler.start()
    poller.start()
    arm_cue()
    print('>>> Scrap with 25 mm of free +X travel from the head. TWO presses: one')
    print('>>> for the line, and a second one after the reconnect, when the button')
    print('>>> lights again. The line goes dark at the drop and the head HOLDS')
    print('>>> where it is; the new session resumes it with your second press.\n')
    print('G91/G21: %s / %s' % (g.cmd('G91'), g.cmd('G21')))
    t_m3 = time.time()
    for ln in ('M3 S%d' % SENDERCHG_S, SENDERCHG_LINE1, 'M5'):
        g.s.sendall(ln.encode() + b'\n')
    prompt1 = senderchg_wait_reply(g, 'press the button', 10)
    print('  first prompt: %s' % prompt1)
    st = g.wait_state('Run', FLOWLOAD_PRESS_WAIT_S)
    if not st.startswith('Run'):
        print('ABORTED: the first job never ran (%s)' % st)
        g.rt(b'\x18')
        sampler.stop()
        poller.stop()
        return 1
    t_run1 = time.time()
    time.sleep(SENDERCHG_DROP_S)
    t_drop = time.time()
    replies_a = list(g.log)
    g.s.close()                              # the sender goes away, mid-line, lit
    print('  connection dropped %.1f s into the first line' % (t_drop - t_run1))
    time.sleep(1.0)
    g2 = Grbl(HOST, PORT)                    # a new session
    t_reconnect = time.time()
    st = g2.wait_state('Hold', 10)
    print('  reconnected; the job is held %.1f s after the drop (state %s)'
          % (time.time() - t_drop, st))
    cs = sample_forgectrl() or {}
    print('  engine after the drop: armed=%s verdict=%s' % (cs.get('armed'), cs.get('verdict')))
    outcome = 'done'
    t_run2 = None
    try:
        if not st.startswith('Hold'):
            outcome = 'not-held'
            g2.rt(b'\x18')
        else:
            g2.rt(b'~')                      # the new sender resumes the held job
            prompt2 = senderchg_wait_reply(g2, 'press the button to resume', 10)
            print('  resume prompt after the reconnect: %s' % prompt2)
            if not prompt2:
                outcome = 'no-prompt'
                g2.rt(b'\x18')
            else:
                st = g2.wait_state('Run', FLOWLOAD_PRESS_WAIT_S)
                if not st.startswith('Run'):
                    outcome = 'no-run'
                    g2.rt(b'\x18')
                else:
                    t_run2 = time.time()
                    g2.wait_state('Idle', 60)
                    print('  M2: %s' % g2.cmd('G90\nM2', timeout=10))
    finally:
        try:
            g2.cmd('M5', timeout=1)
        except Exception:
            pass
        time.sleep(3.0)
        sampler.stop()
        poller.stop()
    tr = sampler.samples
    lit = [s['t'] for s in tr if (s.get('lon') or 0) > 0 or (s.get('hv') or 0) > HV_DARK_MAX]
    # laser_on_sampled is a one-second window count: it reads nonzero for
    # up to a second after the beam stops, so the gap opens 2.5 s after
    # the drop; hv_current and the thermopile answer within a sample.
    lit1 = [t for t in lit if t_run1 - 1 <= t <= t_drop + 2.5]
    lit_gap = [t for t in lit if t_drop + 2.5 < t < (t_run2 or time.time()) - 0.5]
    lit2 = [t for t in lit if t_run2 and t >= t_run2] if t_run2 else []
    hv_off = [s['t'] - t_drop for s in tr if s['t'] > t_drop and (s.get('hv') or 0) <= HV_DARK_MAX]
    print('  hv_current read dark %.2f s after the drop' % (hv_off[0] if hv_off else -1))
    print('\n--- replies (t from the first M3) ---')
    for t, ln in replies_a + g2.log:
        if ln != 'ok' and not re.match(r'\$\d+=', ln):
            print('  %+7.1f s  %s' % (t - t_m3, ln))
    print('--- engine armed transitions ---')
    last = None
    for p in poller.samples:
        if 'armed' in p and p['armed'] != last:
            print('  %+7.1f s  armed=%s' % (p['t'] - t_m3, p['armed']))
            last = p['armed']
    print('--- emission (lit samples at 25 Hz) ---')
    print('  line, press to drop:        %d samples (%.1f s)' % (len(lit1), len(lit1) / 25.0))
    print('  drop to second press:       %d samples  <- must be 0' % len(lit_gap))
    print('  the resumed rest of it:     %d samples (%.1f s)' % (len(lit2), len(lit2) / 25.0))
    ok = outcome == 'done' and prompt1 and len(lit1) > 25 and not lit_gap and len(lit2) > 5
    print('\n%s: %s' % ('PASS' if ok else 'FAIL',
                        'the drop held the job and closed the window, nothing fired until '
                        'the second press, and ~ from the new session prompted, re-armed '
                        'and finished the line' if ok else outcome))
    rec = {'drill': 'senderchg', 'date': time.strftime('%Y-%m-%dT%H:%M:%S'),
           't_m3': t_m3, 't_run1': t_run1, 't_drop': t_drop, 't_reconnect': t_reconnect,
           't_run2': t_run2, 'outcome': outcome, 'pass': bool(ok),
           'replies': replies_a + g2.log, 'poll': poller.samples, 'trace': tr}
    ddir = os.environ.get('FORGETEST_BENCH_DATA') or ('/tmp' if sampler.local else os.getcwd())
    path = os.path.join(ddir, 'senderchg_%s.json' % time.strftime('%Y%m%d-%H%M%S'))
    try:
        with open(path, 'w') as f:
            json.dump(rec, f, indent=1)
        print('record: %s' % path)
    except OSError as e:
        print('record not written: %s' % e)
    return 0 if ok else 1


# --- an RX overrun mid-job stops the job and closes the window ---------------

# A sender that writes past the RX ring (Bf: is the contract) loses lines,
# and the driver now drops the overrunning line whole, reports the overrun
# and stops the job the way a ^X does: alarm, latch relocked, window
# closed. This drill lights a 20 mm line at F60 on the press, writes a
# 93-line fill at once three seconds in (about 1270 bytes against the
# 1023-byte ring), and expects the report, the alarm and the disarm, with
# emission ending at the alarm; then $X, a fresh M3 and a 5 mm line must
# prompt for the button again and mark.
OVERRUN_S = 400
OVERRUN_LINE1 = 'G1 X20 F60'
OVERRUN_BLAST_S = 3.0                     # lit seconds before the blast
OVERRUN_LINE2 = 'G1 X5 F600'


def drill_overrun(g):
    print('=== RX overrun mid-job: report, alarm, disarm; the next laser-on prompts ===')
    print('connect: %s' % prepare(g))
    print('spindle off: %s' % g.cmd('M5'))
    pre = sample_forgectrl()
    if pre is None or pre.get('armed'):
        print('REFUSED: the armed window is still open (or forgectrl is unreachable)')
        return 2
    lines, _dx, _dy = dpatch_gcode(1500, 30.0, 0.3, 46)
    blast = ''.join(ln + '\n' for ln in lines + ['M5']).encode()
    sampler = Sampler(PCURVE_SAMPLE_HZ)
    poller = CoolPoller()
    sampler.start()
    poller.start()
    arm_cue()
    print('>>> Scrap with 25 mm of free +X and 15 mm of +Y travel from the head.')
    print('>>> TWO presses: one for the first line, and a second one after the')
    print('>>> overrun alarm has been cleared, when the prompt comes again. The')
    print('>>> blast is %d bytes written at once %.0f s into the line.\n'
          % (len(blast), OVERRUN_BLAST_S))
    print('G91/G21: %s / %s' % (g.cmd('G91'), g.cmd('G21')))
    t_m3 = time.time()
    for ln in ('M3 S%d' % OVERRUN_S, OVERRUN_LINE1):
        g.s.sendall(ln.encode() + b'\n')
    prompt1 = senderchg_wait_reply(g, 'press the button', 10)
    print('  first prompt: %s' % prompt1)
    st = g.wait_state('Run', FLOWLOAD_PRESS_WAIT_S)
    if not st.startswith('Run'):
        print('ABORTED: the first job never ran (%s)' % st)
        g.rt(b'\x18')
        sampler.stop()
        poller.stop()
        return 1
    t_run1 = time.time()
    time.sleep(OVERRUN_BLAST_S)
    t_blast = time.time()
    g.s.sendall(blast)
    reported = senderchg_wait_reply(g, 'RX overrun', 10)
    t_report = time.time()
    st = g.wait_state('Alarm', 10)
    t_alarm = time.time()
    print('  overrun reported: %s (%.1f s after the blast); state %s' % (reported, t_report - t_blast, st))
    outcome = 'done'
    t_run2 = None
    prompt2 = False
    try:
        if not reported or not st.startswith('Alarm'):
            outcome = 'no-report' if not reported else 'no-alarm'
            g.rt(b'\x18')
        else:
            time.sleep(2.0)
            g.drain()
            print('  unlock: %s' % g.cmd('$X'))
            for ln in ('M3 S%d' % OVERRUN_S, OVERRUN_LINE2, 'M5'):
                g.s.sendall(ln.encode() + b'\n')
            prompt2 = senderchg_wait_reply(g, 'press the button', 10)
            print('  second prompt after the alarm: %s' % prompt2)
            if not prompt2:
                outcome = 'no-prompt'
                g.rt(b'\x18')
            else:
                st = g.wait_state('Run', FLOWLOAD_PRESS_WAIT_S)
                if not st.startswith('Run'):
                    outcome = 'no-run'
                    g.rt(b'\x18')
                else:
                    t_run2 = time.time()
                    g.wait_state('Idle', 30)
                    print('  M2: %s' % g.cmd('G90\nM2', timeout=10))
    finally:
        try:
            g.cmd('M5', timeout=1)
        except Exception:
            pass
        time.sleep(3.0)
        sampler.stop()
        poller.stop()
    tr = sampler.samples
    lit = [s['t'] for s in tr if (s.get('lon') or 0) > 0 or (s.get('hv') or 0) > HV_DARK_MAX]
    # laser_on_sampled is a one-second window count (see senderchg): the
    # gap opens 2.5 s after the alarm; hv_current answers within a sample.
    lit1 = [t for t in lit if t_run1 - 1 <= t <= t_alarm + 2.5]
    lit_gap = [t for t in lit if t_alarm + 2.5 < t < (t_run2 or time.time()) - 0.5]
    lit2 = [t for t in lit if t_run2 and t >= t_run2] if t_run2 else []
    hv_off = [s['t'] - t_blast for s in tr if s['t'] > t_blast and (s.get('hv') or 0) <= HV_DARK_MAX]
    print('  hv_current read dark %.2f s after the blast' % (hv_off[0] if hv_off else -1))
    last_lit_after_blast = max([t for t in lit if t <= (t_run2 or time.time())], default=t_blast)
    print('\n--- replies (t from the first M3) ---')
    for t, ln in g.log:
        if ln != 'ok' and not re.match(r'\$\d+=', ln):
            print('  %+7.1f s  %s' % (t - t_m3, ln))
    print('--- engine armed transitions ---')
    last = None
    for p in poller.samples:
        if 'armed' in p and p['armed'] != last:
            print('  %+7.1f s  armed=%s' % (p['t'] - t_m3, p['armed']))
            last = p['armed']
    print('--- emission (lit samples at 25 Hz) ---')
    print('  first line, press to alarm:  %d samples (%.1f s); last lit sample %.2f s after the blast'
          % (len(lit1), len(lit1) / 25.0, last_lit_after_blast - t_blast))
    print('  alarm to second press:       %d samples  <- must be 0' % len(lit_gap))
    print('  second line:                 %d samples (%.1f s)' % (len(lit2), len(lit2) / 25.0))
    ok = outcome == 'done' and prompt1 and prompt2 and reported and not lit_gap and len(lit2) > 5
    print('\n%s: %s' % ('PASS' if ok else 'FAIL',
                        'the overrun was reported, the job stopped in alarm with the window '
                        'closed, nothing fired until the second press, and the next laser-on '
                        'prompted and armed' if ok else outcome))
    rec = {'drill': 'overrun', 'date': time.strftime('%Y-%m-%dT%H:%M:%S'),
           't_m3': t_m3, 't_run1': t_run1, 't_blast': t_blast, 't_report': t_report,
           't_alarm': t_alarm, 't_run2': t_run2, 'blast_bytes': len(blast),
           'outcome': outcome, 'pass': bool(ok),
           'replies': g.log, 'poll': poller.samples, 'trace': tr}
    ddir = os.environ.get('FORGETEST_BENCH_DATA') or ('/tmp' if sampler.local else os.getcwd())
    path = os.path.join(ddir, 'overrun_%s.json' % time.strftime('%Y%m%d-%H%M%S'))
    try:
        with open(path, 'w') as f:
            json.dump(rec, f, indent=1)
        print('record: %s' % path)
    except OSError as e:
        print('record not written: %s' % e)
    return 0 if ok else 1


# --- the rapids after an M5 ship dark ----------------------------------------

# One constant-power line, M5, then two rapids over it with dwells between,
# the shape every ladder above uses between rungs. M5 executes with the
# planner drained and the kernel run over, and the core issues no
# per-segment laser update for moves made with the spindle off, so only the
# stream's wanted fire state decides whether those rapids fire.
# S1000 is a certain strike and, under the density model, the worst case
# for the bug: full duty pinned, so a rapid that inherited fire would run
# at full power.
M5DARK_JOB = ['G91', 'G21', 'M3', 'S400',
              'G1 X20 F600',
              'M5', 'G4 P2.5',
              'G0 X-20', 'G4 P2.5',
              'G0 X20', 'G4 P2.5',
              'G90', 'M2']
HV_DARK_MAX = 20                        # hv_current reads 0 with the tube off


def drill_m5dark(g):
    print('=== the rapids after an M5 ship dark: M3 S400, 20 mm line, M5, two rapids ===')
    sampler = Sampler(PCURVE_SAMPLE_HZ)
    if not sampler.local:
        print('run this on the board: the witnesses are sysfs at 25 Hz')
        return 2
    print('connect: %s' % prepare(g))
    print('pre-fire: %s' % sample_forgectrl())
    arm_cue()
    print('>>> 20 mm of free +X travel at the head. One 20 mm line at S400,')
    print('>>> then the head rapids back over it and forward again, dark.\n')
    sampler.start()
    for ln in M5DARK_JOB:
        g.s.sendall(ln.encode() + b'\n')
    st = g.wait_state('Run', 240)
    if not st.startswith('Run'):
        print('FAIL: the job never ran (state=%s)' % st)
        g.rt(b'\x18')
        sampler.stop()
        return 1
    # The controller reports Idle inside a G4 dwell, so Idle is no sign the
    # job is over; the armed window closing at M2 is.
    t0 = time.time()
    seen_armed = False
    while time.time() - t0 < 90:
        smp = sample_forgectrl()
        if smp and smp['armed']:
            seen_armed = True
        elif smp and seen_armed and not smp['armed']:
            break
        time.sleep(0.2)
    time.sleep(1.5)
    sampler.stop()
    tr = sampler.samples
    # Discharge segments from the current, 1 s hysteresis: the line is one;
    # a rapid that fired is another.
    segs, cur = [], None
    for s in tr:
        on = s['hv'] is not None and s['hv'] > 30
        if on and cur is None:
            cur = [s['t'], s['t']]
        elif on:
            cur[1] = s['t']
        elif cur is not None and s['t'] - cur[1] > 1.0:
            segs.append(cur)
            cur = None
    if cur:
        segs.append(cur)
    print('\n--- results (%d samples, %.1f Hz) ---' % (len(tr), sampler.rate()))
    if not segs:
        print('FAIL: no discharge seen at all (arm refused, no press, or no fire)')
        return 1
    t_end = segs[0][1]
    base = _stats(_window(tr, segs[0][0] - 2.0, segs[0][0] - 0.2, 'tp'))['mean']
    print('line: %.2f s of discharge; %d discharge segment(s) in the run%s'
          % (t_end - segs[0][0], len(segs),
             '' if len(segs) == 1 else ': the extra ones are rapids that FIRED'))
    hv_after = max((s['hv'] for s in tr if s['t'] > t_end + 0.3 and s['hv'] is not None),
                   default=0)
    lon_after = [s for s in tr if s['t'] > t_end + 0.3 and s.get('lon')]
    # laser_on_sampled lags a window: the first zero past the line is the
    # dark point, and nothing after it may be nonzero.
    zeros = [s['t'] for s in tr if s['t'] > t_end and s.get('lon') == 0]
    relit = [s for s in tr if zeros and s['t'] > zeros[0] and s.get('lon')]
    print('after the M5: hv max %d (dark <= %d); laser_on_sampled nonzero samples %d, '
          'after its first zero %d' % (hv_after, HV_DARK_MAX, len(lon_after), len(relit)))
    print('trace from 0.2 s before the line ended, 40 ms steps (hv / thermopile delta):')
    row = [s for s in tr if t_end - 0.2 <= s['t'] <= t_end + 9.0]
    for i in range(0, len(row), 25):
        chunk = row[i:i + 25]
        print('  +%4.1fs hv: %s' % (chunk[0]['t'] - t_end, ' '.join('%d' % (s['hv'] or 0) for s in chunk)))
        print('         tp: %s' % ' '.join('%d' % ((s['tp'] or 0) - (base or 0)) for s in chunk))
    covered = tr[-1]['t'] - t_end
    if covered < 7.0:
        print('M5DARK INCONCLUSIVE: the trace ends %.1f s after the line, before '
              'the rapids (the job runs ~8 s past the M5)' % covered)
        return 1
    ok = len(segs) == 1 and hv_after <= HV_DARK_MAX and not relit
    print('M5DARK %s' % ('PASS: the rapids after the M5 shipped dark (%.1f s sampled past the line)'
                         % covered if ok else 'FAIL: the laser fired after the M5'))
    return 0 if ok else 1


# The corner pattern: a long segment above the ~1.6 mm accelerate-in-
# and-out distance at F2000, teeth and a square well below it, and a
# reversal - every place M4 drives the commanded power to the floor.
M4C_PATTERN = (
    ('G1', 10.0, 0.0),                  # long: reaches programmed feed
    ('G1', 1.0, 0.6), ('G1', 1.0, -0.6), ('G1', 1.0, 0.6), ('G1', 1.0, -0.6),
    ('G1', 1.0, 0.6), ('G1', 1.0, -0.6),               # 1.2 mm teeth
    ('G1', 2.0, 0.0), ('G1', 0.0, 2.0), ('G1', -2.0, 0.0), ('G1', 0.0, -2.0),
    ('G1', 5.0, 0.0), ('G1', -5.0, 0.0),               # 180 degree reversal
    ('G1', 0.5, 0.4), ('G1', 0.5, -0.4), ('G1', 0.5, 0.4), ('G1', 0.5, -0.4),
    ('G1', 3.0, 0.0),                   # finish long
)
M4C_ROW_GAP = 8.0                       # mm between the two passes


def drill_m4corner(g):
    sval = int(sys.argv[2]) if len(sys.argv) > 2 else 300
    feed = int(sys.argv[3]) if len(sys.argv) > 3 else 2000
    if not 50 <= sval <= 1000 or not 300 <= feed <= 6000:
        print('usage: m4corner [S 50..1000] [F 300..6000]')
        return 2
    sampler = Sampler(PCURVE_SAMPLE_HZ)
    if not sampler.local:
        print('run this on the board: the witnesses are sysfs at 25 Hz')
        return 2
    fails = []

    def check(cond, msg):
        print('  %s: %s' % ('ok' if cond else 'FAIL', msg))
        if not cond:
            fails.append(msg)

    def logged(needle):
        return any(needle in ln for _t, ln in g.log)

    dx = sum(x for _c, x, _y in M4C_PATTERN)
    dy = sum(y for _c, _x, y in M4C_PATTERN)
    span_x = 0.0
    run_x = 0.0
    for _c, x, _y in M4C_PATTERN:
        run_x += x
        span_x = max(span_x, run_x)

    print('=== M4 into corners: the pattern at S%d F%d under density ===' % (sval, feed))
    print('connect: %s' % prepare(g))
    print('pre-fire: %s' % sample_forgectrl())
    arm_cue()
    print('>>> A fresh area: %g mm along +X by %g mm along +Y from the head.\n' % (span_x + 2, 4))
    sampler.start()
    aborted = False
    try:
        for ln in ('G91', 'G21'):
            g.cmd(ln)
        for i, mname in enumerate(('density',)):
            g.s.sendall(b'M4 S%d\n' % sval)
            for cmd, x, y in M4C_PATTERN:
                parts = [cmd]
                if x:
                    parts.append('X%g' % x)
                if y:
                    parts.append('Y%g' % y)
                parts.append('F%d' % feed)
                g.s.sendall((' '.join(parts) + '\n').encode())
            g.s.sendall(b'M5\n')
            st = g.wait_state('Run', 300 if i == 0 else 60)
            if not st.startswith('Run'):
                print('FAIL: pass %d never ran (state=%s)' % (i + 1, st))
                g.rt(b'\x18')
                aborted = True
                return 1
            g.wait_state('Idle', 120)
            time.sleep(0.5)
            g.drain()
            print('  %s pass ran' % mname)
            g.s.sendall(('G0 X%g Y%g\n' % (-dx, -dy)).encode())
            g.wait_state('Idle', 30)
        check(logged('laser armed (density, floor'), 'the arm named the density model')
        g.cmd('G90')
        g.cmd('M2')
        time.sleep(0.5)
        g.drain()
        t0 = time.time()
        while time.time() - t0 < 90:
            smp = sample_forgectrl()
            if smp and not smp['armed']:
                break
            time.sleep(0.2)
        time.sleep(1.5)
    except Exception as e:
        aborted = True
        print('ABORTED: %s' % e)
        g.rt(b'\x18')
    finally:
        sampler.stop()
    if aborted:
        return 1

    tr = sampler.samples
    segs, cur = [], None
    for smp in tr:
        on = smp['hv'] is not None and smp['hv'] > HV_DARK_MAX
        if on and cur is None:
            cur = [smp['t'], smp['t']]
        elif on:
            cur[1] = smp['t']
        elif cur is not None and smp['t'] - cur[1] > 1.5:
            segs.append(cur)
            cur = None
    if cur:
        segs.append(cur)
    print('\n--- results (%d samples, %.1f Hz) ---' % (len(tr), sampler.rate()))
    check(len(segs) == 1, '%d discharge window(s), expected 1' % len(segs))
    for name, (a, b) in zip(('density',), segs):
        hv = _stats(_window(tr, a + 0.2, b - 0.1, 'hv'))
        tp = _stats(_window(tr, a + 0.2, b - 0.1, 'tp'))
        print('  %s pass: %.1f s lit, hv mean %.0f max %d, tp mean %.0f'
              % (name, b - a, hv['mean'], hv['max'], tp['mean'] or 0))
    if segs:
        t_end = segs[-1][1]
        hv_after = max((smp['hv'] for smp in tr if smp['t'] > t_end + 0.5
                        and smp['hv'] is not None), default=0)
        check(hv_after <= HV_DARK_MAX, 'dark after the last M5 (hv max %d)' % hv_after)

    print('\n--- the operator reads the material ---')
    print('Trace the whole path with your eye:')
    print('  1. DROPOUT: any commanded segment with NO mark at all - look hardest')
    print('     at the 1.2 mm teeth, the 0.5 mm teeth and the 2 mm square,')
    print('     where M4 never lets the power rise off the floor. Under density')
    print('     a dropout should be impossible; confirm it.')
    print('  2. The long 10 mm lines are the reference: mid-line is the')
    print('     pattern cut at full commanded power.')
    ok = not fails
    print('M4CORNER %s' % ('instrument checks PASS - the material verdict is yours'
                           if ok else 'FAIL: %d check(s) failed' % len(fails)))
    return 0 if ok else 1


M4F_MM = 60.0
M4F_LEG_GAP = 0.6                       # the return leg sits beside the out leg
M4F_ROW_GAP = 5.0


def drill_m4feeds(g):
    sval = int(sys.argv[2]) if len(sys.argv) > 2 else 600
    f1 = int(sys.argv[3]) if len(sys.argv) > 3 else 1000
    f2 = int(sys.argv[4]) if len(sys.argv) > 4 else 4000
    if not 50 <= sval <= 1000 or not 300 <= f1 < f2 <= 8000:
        print('usage: m4feeds [S 50..1000] [F1] [F2]  (300 <= F1 < F2 <= 8000)')
        return 2
    sampler = Sampler(PCURVE_SAMPLE_HZ)
    if not sampler.local:
        print('run this on the board: the witnesses are sysfs at 25 Hz')
        return 2
    model = conf_get('laser_power_model') or 'density'
    if model != 'density':
        print('PRECONDITION FAILED: laser_power_model is %s; B3 is the density '
              'time-base question' % model)
        return 2
    fails = []

    def check(cond, msg):
        print('  %s: %s' % ('ok' if cond else 'FAIL', msg))
        if not cond:
            fails.append(msg)

    print('=== B3, dose per mm across feeds: S%d under M4 density at F%d and F%d ==='
          % (sval, f1, f2))
    print('connect: %s' % prepare(g))
    print('pre-fire: %s' % sample_forgectrl())
    arm_cue()
    print('>>> A fresh area: %g mm along +X by %g mm along +Y from the head.' % (M4F_MM + 2, M4F_ROW_GAP + 4))
    print('>>> Pass 1 (F%d) cuts at the head; pass 2 (F%d) %g mm in +Y.'
          % (f1, f2, M4F_ROW_GAP))
    print('>>> Each pass is an out leg and a return leg %g mm apart - a long' % M4F_LEG_GAP)
    print('>>> U with its turn at the far end, so both legs read separately.\n')
    sampler.start()
    aborted = False
    try:
        for ln in ('G91', 'G21'):
            g.cmd(ln)
        for i, feed in enumerate((f1, f2)):
            g.s.sendall(b'M4 S%d\n' % sval)
            g.s.sendall(('G1 X%g F%d\n' % (M4F_MM, feed)).encode())
            g.s.sendall(('G1 Y%g F%d\n' % (M4F_LEG_GAP, feed)).encode())
            g.s.sendall(('G1 X%g F%d\n' % (-M4F_MM, feed)).encode())
            g.s.sendall(b'M5\n')
            g.s.sendall(('G0 Y%g\n' % -M4F_LEG_GAP).encode())
            st = g.wait_state('Run', 300 if i == 0 else 60)
            if not st.startswith('Run'):
                print('FAIL: pass %d never ran (state=%s)' % (i + 1, st))
                g.rt(b'\x18')
                aborted = True
                return 1
            g.wait_state('Idle', 120)
            time.sleep(0.5)
            g.drain()
            print('  pass at F%d ran' % feed)
            if i == 0:
                g.s.sendall(('G0 Y%g\n' % M4F_ROW_GAP).encode())
                g.wait_state('Idle', 30)
        g.cmd('G90')
        g.cmd('M2')
        t0 = time.time()
        while time.time() - t0 < 90:
            smp = sample_forgectrl()
            if smp and not smp['armed']:
                break
            time.sleep(0.2)
        time.sleep(1.5)
    except Exception as e:
        aborted = True
        print('ABORTED: %s' % e)
        g.rt(b'\x18')
    finally:
        sampler.stop()
    if aborted:
        return 1

    tr = sampler.samples
    segs, cur = [], None
    for smp in tr:
        on = smp['hv'] is not None and smp['hv'] > HV_DARK_MAX
        if on and cur is None:
            cur = [smp['t'], smp['t']]
        elif on:
            cur[1] = smp['t']
        elif cur is not None and smp['t'] - cur[1] > 1.5:
            segs.append(cur)
            cur = None
    if cur:
        segs.append(cur)
    print('\n--- results (%d samples, %.1f Hz) ---' % (len(tr), sampler.rate()))
    check(len(segs) == 2, '%d discharge window(s), expected 2 (one per feed)' % len(segs))
    for feed, (a, b) in zip((f1, f2), segs):
        hv = _stats(_window(tr, a + 0.2, b - 0.1, 'hv'))
        tp = _stats(_window(tr, a + 0.2, b - 0.1, 'tp'))
        print('  F%d pass: %.1f s lit, hv mean %.0f, tp mean %.0f (the beam at '
              'cruise should read alike at both feeds: M4 commands S at speed)'
              % (feed, b - a, hv['mean'], tp['mean'] or 0))
    if segs:
        t_end = segs[-1][1]
        hv_after = max((smp['hv'] for smp in tr if smp['t'] > t_end + 0.5
                        and smp['hv'] is not None), default=0)
        check(hv_after <= HV_DARK_MAX, 'dark after the last M5 (hv max %d)' % hv_after)

    print('\n--- the operator reads the material ---')
    print('Two out-and-back line pairs, F%d nearest you first, F%d %g mm past it.' % (f1, f2, M4F_ROW_GAP))
    print('  1. EVENNESS within each pass: each line should be equally dark from')
    print('     its slow ends to its fast middle - that is M4 holding dose per')
    print('     mm constant through the accel. Ends darker than the middle =')
    print('     partial compensation (the factory rides a ~1.8x rise).')
    print('  2. The REVERSAL point (far end) is the worst case - compare its')
    print('     darkness against the mid-line of the same pass, at both feeds.')
    print('  3. ACROSS the passes: the faster pass is expected ~%gx lighter per' % (float(f2) / f1))
    print('     mm overall (feed is the dose control at cruise); the question is')
    print('     whether the within-line evenness holds at both.')
    ok = not fails
    print('M4FEEDS %s' % ('instrument checks PASS - the material verdict is yours'
                          if ok else 'FAIL: %d check(s) failed' % len(fails)))
    return 0 if ok else 1


XYM_MM = 20.0
XYM_LEG_GAP = 0.6
XYM_ROW_GAP = 5.0


def drill_xymode(g):
    sval = int(sys.argv[2]) if len(sys.argv) > 2 else 400
    f1 = int(sys.argv[3]) if len(sys.argv) > 3 else 1200
    f2 = int(sys.argv[4]) if len(sys.argv) > 4 else 6000
    xoff = float(sys.argv[5]) if len(sys.argv) > 5 else 0.0
    if not 50 <= sval <= 1000 or not 300 <= f1 < f2 <= 12000 or not 0 <= xoff <= 300:
        print('usage: xymode [S 50..1000] [F1] [F2] [X offset 0..300]  (300 <= F1 < F2 <= 12000)')
        return 2
    sampler = Sampler(PCURVE_SAMPLE_HZ)
    if not sampler.local:
        print('run this on the board: the witnesses are sysfs at 25 Hz')
        return 2
    model = conf_get('laser_power_model') or 'density'
    if model != 'density':
        print('PRECONDITION FAILED: laser_power_model is %s' % model)
        return 2

    def sysfs(attr):
        with open(SYSFS + '/' + attr) as f:
            return f.read().strip()

    fails = []

    def check(cond, msg):
        print('  %s: %s' % ('ok' if cond else 'FAIL', msg))
        if not cond:
            fails.append(msg)

    mode, tick, ramp = sysfs('cnc/x_mode'), sysfs('cnc/step_freq'), sysfs('cnc/ramp_rate')
    print('=== XY microstep mode under the laser: x%s, %s Hz tick, %s Hz/s ramp, S%d under M4 density at F%d and F%d ==='
          % (mode, tick, ramp, sval, f1, f2))
    check(sysfs('cnc/y_mode') == mode, 'both axes at x%s' % mode)
    print('connect: %s' % prepare(g))
    print('pre-fire: %s' % sample_forgectrl())
    arm_cue()
    print('>>> The block: %g mm along +X by %g mm along +Y, starting %g mm in +X from the head.'
          % (XYM_MM + 2, XYM_ROW_GAP + 4, xoff))
    print('>>> Pass 1 (F%d) first, pass 2 (F%d) %g mm past it in +Y; each pass an out leg'
          % (f1, f2, XYM_ROW_GAP))
    print('>>> and a return leg %g mm apart. The head comes back to where it started.\n' % XYM_LEG_GAP)
    sampler.start()
    aborted = False
    try:
        for ln in ('G91', 'G21'):
            g.cmd(ln)
        if xoff:
            g.cmd('G0 X%g' % xoff)
            g.wait_state('Idle', 30)
        for i, feed in enumerate((f1, f2)):
            g.s.sendall(b'M4 S%d\n' % sval)
            g.s.sendall(('G1 X%g F%d\n' % (XYM_MM, feed)).encode())
            g.s.sendall(('G1 Y%g F%d\n' % (XYM_LEG_GAP, feed)).encode())
            g.s.sendall(('G1 X%g F%d\n' % (-XYM_MM, feed)).encode())
            g.s.sendall(b'M5\n')
            g.s.sendall(('G0 Y%g\n' % -XYM_LEG_GAP).encode())
            st = g.wait_state('Run', 300 if i == 0 else 60)
            if not st.startswith('Run'):
                print('FAIL: pass %d never ran (state=%s)' % (i + 1, st))
                g.rt(b'\x18')
                aborted = True
                return 1
            g.wait_state('Idle', 120)
            time.sleep(0.5)
            g.drain()
            print('  pass at F%d ran' % feed)
            if i == 0:
                g.s.sendall(('G0 Y%g\n' % XYM_ROW_GAP).encode())
                g.wait_state('Idle', 30)
        g.s.sendall(('G0 Y%g\n' % -XYM_ROW_GAP).encode())
        g.wait_state('Idle', 30)
        if xoff:
            g.cmd('G0 X%g' % -xoff)
            g.wait_state('Idle', 30)
        g.cmd('G90')
        g.cmd('M2')
        t0 = time.time()
        while time.time() - t0 < 90:
            smp = sample_forgectrl()
            if smp and not smp['armed']:
                break
            time.sleep(0.2)
        time.sleep(1.5)
    except Exception as e:
        aborted = True
        print('ABORTED: %s' % e)
        g.rt(b'\x18')
    finally:
        sampler.stop()
    if aborted:
        return 1
    tr = sampler.samples
    segs, cur = [], None
    for smp in tr:
        on = smp['hv'] is not None and smp['hv'] > HV_DARK_MAX
        if on and cur is None:
            cur = [smp['t'], smp['t']]
        elif on:
            cur[1] = smp['t']
        elif cur is not None and smp['t'] - cur[1] > 1.5:
            segs.append(cur)
            cur = None
    if cur:
        segs.append(cur)
    print('\n--- results x%s (%d samples, %.1f Hz) ---' % (mode, len(tr), sampler.rate()))
    check(len(segs) == 2, '%d discharge window(s), expected 2 (one per feed)' % len(segs))
    for feed, (a, b) in zip((f1, f2), segs):
        hv = _stats(_window(tr, a + 0.2, b - 0.1, 'hv'))
        tp = _stats(_window(tr, a + 0.2, b - 0.1, 'tp'))
        print('  F%d pass: %.1f s lit, hv mean %.0f, tp mean %.0f' % (feed, b - a, hv['mean'], tp['mean'] or 0))
    if segs:
        t_end = segs[-1][1]
        hv_after = max((smp['hv'] for smp in tr if smp['t'] > t_end + 0.5 and smp['hv'] is not None), default=0)
        check(hv_after <= HV_DARK_MAX, 'dark after the last M5 (hv max %d)' % hv_after)
    check(sysfs('cnc/underruns') == '0', 'no underrun (cnc/underruns %s)' % sysfs('cnc/underruns'))
    print('\n--- the operator reads the material ---')
    print('Two line pairs, F%d nearest the start, F%d %g mm past it in +Y, this block %g mm in +X.'
          % (f1, f2, XYM_ROW_GAP, xoff))
    print('  Compare this block against the other modes: evenness along each line, the')
    print('  darkness at cruise, the reversal at the far end. The current per pass above')
    print('  should read alike across the modes: the laser ticks are scaled to the tick.')
    ok = not fails
    print('XYMODE x%s %s' % (mode, 'instrument checks PASS - the material verdict is yours'
                              if ok else 'FAIL: %d check(s) failed' % len(fails)))
    return 0 if ok else 1


def drill_xycircle(g):
    sval = int(sys.argv[2]) if len(sys.argv) > 2 else 400
    feed = int(sys.argv[3]) if len(sys.argv) > 3 else 2400
    xoff = float(sys.argv[4]) if len(sys.argv) > 4 else 76.2
    dia = float(sys.argv[5]) if len(sys.argv) > 5 else 152.4
    if not 50 <= sval <= 1000 or not 300 <= feed <= 12000 or not 0 <= xoff <= 300 or not 10 <= dia <= 250:
        print('usage: xycircle [S 50..1000] [F 300..12000] [X offset 0..300] [diameter 10..250]')
        return 2
    sampler = Sampler(PCURVE_SAMPLE_HZ)
    if not sampler.local:
        print('run this on the board: the witnesses are sysfs at 25 Hz')
        return 2
    model = conf_get('laser_power_model') or 'density'
    if model != 'density':
        print('PRECONDITION FAILED: laser_power_model is %s' % model)
        return 2

    def sysfs(attr):
        with open(SYSFS + '/' + attr) as f:
            return f.read().strip()

    fails = []

    def check(cond, msg):
        print('  %s: %s' % ('ok' if cond else 'FAIL', msg))
        if not cond:
            fails.append(msg)

    r = dia / 2.0
    mode, tick, ramp = sysfs('cnc/x_mode'), sysfs('cnc/step_freq'), sysfs('cnc/ramp_rate')
    print('=== XY microstep mode under the laser: x%s, %s Hz tick, %s Hz/s ramp, a %g mm circle at S%d under M4 density at F%d ==='
          % (mode, tick, ramp, dia, sval, feed))
    check(sysfs('cnc/y_mode') == mode, 'both axes at x%s' % mode)
    print('connect: %s' % prepare(g))
    print('pre-fire: %s' % sample_forgectrl())
    arm_cue()
    print('>>> A dark rapid %g mm in +X, then the circle from its top: %g mm across, its' % (xoff, dia))
    print('>>> center %g mm in +Y from the start, so it spans %g mm each side of the start' % (r, r))
    print('>>> and %g mm in +Y. Laser off, then a rapid back to the origin.\n' % dia)
    sampler.start()
    aborted = False
    try:
        for ln in ('G91', 'G21'):
            g.cmd(ln)
        if xoff:
            g.cmd('G0 X%g' % xoff)
            g.wait_state('Idle', 60)
        g.s.sendall(b'M4 S%d\n' % sval)
        g.s.sendall(('G2 X0 Y0 I0 J%g F%d\n' % (r, feed)).encode())
        g.s.sendall(b'M5\n')
        st = g.wait_state('Run', 300)
        if not st.startswith('Run'):
            print('FAIL: the circle never ran (state=%s)' % st)
            g.rt(b'\x18')
            aborted = True
            return 1
        g.wait_state('Idle', 300)
        time.sleep(0.5)
        g.drain()
        print('  the circle ran')
        if xoff:
            g.cmd('G0 X%g' % -xoff)
            g.wait_state('Idle', 60)
        g.cmd('G90')
        g.cmd('M2')
        t0 = time.time()
        while time.time() - t0 < 90:
            smp = sample_forgectrl()
            if smp and not smp['armed']:
                break
            time.sleep(0.2)
        time.sleep(1.5)
    except Exception as e:
        aborted = True
        print('ABORTED: %s' % e)
        g.rt(b'\x18')
    finally:
        sampler.stop()
    if aborted:
        return 1
    tr = sampler.samples
    segs, cur = [], None
    for smp in tr:
        on = smp['hv'] is not None and smp['hv'] > HV_DARK_MAX
        if on and cur is None:
            cur = [smp['t'], smp['t']]
        elif on:
            cur[1] = smp['t']
        elif cur is not None and smp['t'] - cur[1] > 1.5:
            segs.append(cur)
            cur = None
    if cur:
        segs.append(cur)
    print('\n--- results x%s (%d samples, %.1f Hz) ---' % (mode, len(tr), sampler.rate()))
    check(len(segs) == 1, '%d discharge window(s), expected 1' % len(segs))
    for (a, b) in segs:
        hv = _stats(_window(tr, a + 0.2, b - 0.1, 'hv'))
        tp = _stats(_window(tr, a + 0.2, b - 0.1, 'tp'))
        print('  circle: %.1f s lit (expected about %.1f s at F%d), hv mean %.0f, tp mean %.0f'
              % (b - a, math.pi * dia * 60.0 / feed, feed, hv['mean'], tp['mean'] or 0))
    if segs:
        t_end = segs[-1][1]
        hv_after = max((smp['hv'] for smp in tr if smp['t'] > t_end + 0.5 and smp['hv'] is not None), default=0)
        check(hv_after <= HV_DARK_MAX, 'dark after M5 (hv max %d)' % hv_after)
    check(sysfs('cnc/underruns') == '0', 'no underrun (cnc/underruns %s)' % sysfs('cnc/underruns'))
    ok = not fails
    print('XYCIRCLE x%s %s' % (mode, 'instrument checks PASS - the material verdict is yours'
                                if ok else 'FAIL: %d check(s) failed' % len(fails)))
    return 0 if ok else 1


def post_ctrl(action):
    # http.client preserves the header-name case exactly as given.
    import http.client
    tok = panel_token()
    c = http.client.HTTPConnection(HOST, CTRL_PORT, timeout=8)
    c.putrequest('POST', '/controller/' + action)
    c.putheader('X-ForgeFIRM-Token', tok)
    c.putheader('Content-Length', '0')
    c.endheaders()
    r = c.getresponse()
    body = r.read().decode()
    c.close()
    return r.status, body


def drill_expstop(g):
    print('=== armed kill on the expected-stop path (POST /controller/stop) ===')
    if not panel_token():
        raise SystemExit('set GF_TOKEN to the panel token first (or run on the board)')
    print('connect: %s' % prepare(g))
    arm_cue()
    job = ['G91', 'G21', 'M4', 'S400',
           'G1 X40 F200', 'G1 Y40 F200', 'G1 X-40 F200', 'G1 Y-40 F200',
           'M5', 'G90', 'M2']
    for ln in job:
        g.s.sendall(ln.encode() + b'\n')
    # Wait for the burn to be under way (emission > 0), then stop.
    t0 = time.time()
    seen = False
    while time.time() - t0 < 240:
        smp = sample_forgectrl()
        if smp and smp['emission'] and smp['emission'] > 0:
            seen = True
            break
        time.sleep(0.15)
    if not seen:
        print('no emission seen within the wait - operator did not arm? ABORT')
        return []
    print('emission live (%s) - stopping the controller NOW' % smp['emission'])
    t_stop = time.time()
    code, body = post_ctrl('stop')
    print('POST /controller/stop -> %s %s (%.2f s)' % (code, body.strip(), time.time() - t_stop))
    trail = []
    for _ in range(40):                     # ~5 s at 8 Hz
        smp = sample_forgectrl()
        if smp:
            trail.append((round(time.time() - t_stop, 2), smp['emission'], smp['kstate'], smp['armed']))
        time.sleep(0.12)
    print('post-stop trail (t, emission_samples, kstate, armed):')
    for t in trail:
        print('  %s' % (t,))
    zero_at = next((t for t, e, _, _ in trail if e == 0), None)
    tail_zero = all(e == 0 for _, e, _, _ in trail[-16:])
    not_running = all(k != 'running' for _, _, k, _ in trail[-16:])
    print('emission first 0 at +%s s; last 2 s all zero: %s; kernel not running: %s'
          % (zero_at, tail_zero, not_running))
    try:
        mode = get_json('/mode')
    except Exception as e:
        mode = str(e)
    print('/mode after stop: %s' % mode)
    ok = zero_at is not None and zero_at < 2.5 and tail_zero and not_running
    print('EXPSTOP %s' % ('PASS' if ok else 'REVIEW'))
    print('the controller is left STOPPED (supervision held); resume it with '
          'the ctrlstart step once the operator has judged the stop')
    return trail


# --- feed hold and resume: the pause is a corner in time ----------------------

# One armed run on scrap, at F300 and S400. Under M4 a 30 mm line is held
# about 2 s in and resumed a second later, then the path turns 90 degrees:
# the pause mark near 10 mm and the corner at 30 mm sit on the same scrap
# for the eye, and should match. Under M3 the same hold on the return
# line: the resume must show no dark lead. Then a hold that outlives the
# disarm grace: the window closes in Hold, the sender's ~ must light the
# button and wait, the press re-arms, and the line finishes lit. Two
# presses: the arm, and the re-arm after the long hold.
HOLDRES_S = 400
HOLDRES_F = 300


def holdres_pause(g, label, wait_disarm=False):
    """Hold the running move ~2 s in, then resume; with wait_disarm the hold
    outlives the grace and the resume goes through the re-arm prompt."""
    st = g.wait_state('Run', 20)
    if not st.startswith('Run'):
        return 'no-run (%s)' % st
    time.sleep(2.0)
    g.rt(b'!')
    st = g.wait_state('Hold', 5)
    if not st.startswith('Hold'):
        return 'no-hold (%s)' % st
    seen = len(g.log)
    if wait_disarm:
        t0 = time.time()
        while time.time() - t0 < 120:
            s = sample_forgectrl()
            if s and not s['armed']:
                break
            time.sleep(1)
        else:
            return 'still armed after 120 s in Hold'
        print('  %s: the window closed in Hold after %.0f s; sending ~' % (label, time.time() - t0))
        g.rt(b'~')
        if not senderchg_wait_reply(g, 'press the button to resume', 10):
            return 'no resume prompt on ~'
        print('  >>> PRESS the button to resume the job')
        st = g.wait_state('Run', FLOWLOAD_PRESS_WAIT_S)
        if not st.startswith('Run'):
            return 'no-run after the press (%s)' % st
        if not any('laser armed' in ln for _t, ln in g.log[seen:]):
            return 'resumed without re-arming'
    else:
        time.sleep(1.0)
        g.rt(b'~')
        st = g.wait_state('Run', 5)
        if not st.startswith('Run'):
            return 'no-run on ~ (%s)' % st
    st = g.wait_state('Idle', 60)
    return 'ok' if st.startswith('Idle') else 'never idle (%s)' % st


def drill_holdres(g):
    print('=== feed hold and resume: the pause is a corner in time ===')
    print('connect: %s' % prepare(g))
    print('spindle off: %s' % g.cmd('M5'))
    pre = sample_forgectrl()
    if pre is None or pre.get('armed'):
        print('REFUSED: the armed window is still open (or forgectrl is unreachable)')
        return 2
    arm_cue()
    print('>>> Scrap with 35 mm of free +X and 25 mm of free +Y travel. TWO presses:')
    print('>>> one to arm, one when the button lights again after the long hold.\n')
    print('G91/G21: %s / %s' % (g.cmd('G91'), g.cmd('G21')))
    results = []

    def leg(lines):
        for ln in lines:
            g.s.sendall(ln.encode() + b'\n')

    def plain(lines):
        leg(lines)
        g.wait_state('Run', 20)
        g.wait_state('Idle', 60)

    try:
        # A: M4, a 30 mm line held near 10 mm, then the corner leg.
        leg(('M4 S%d' % HOLDRES_S, 'G1 X30 F%d' % HOLDRES_F))
        if not senderchg_wait_reply(g, 'press the button', 10):
            print('ABORTED: no arm prompt')
            return 1
        r = holdres_pause(g, 'M4 pause')
        results.append(('A  M4 pause beside the corner', r))
        print('  A: %s' % r)
        if r != 'ok':
            return 1
        plain(('G1 Y10 F%d' % HOLDRES_F,))
        # B: M3 on the return line, the same hold.
        leg(('M3 S%d' % HOLDRES_S, 'G1 X-30 F%d' % HOLDRES_F))
        r = holdres_pause(g, 'M3 pause')
        results.append(('B  M3 pause, no dark lead', r))
        print('  B: %s' % r)
        if r != 'ok':
            return 1
        # C: back under M4, a plain leg up, then the hold past the grace.
        leg(('M4 S%d' % HOLDRES_S,))
        plain(('G1 Y10 F%d' % HOLDRES_F,))
        leg(('G1 X30 F%d' % HOLDRES_F,))
        r = holdres_pause(g, 'grace', wait_disarm=True)
        results.append(('C  hold past the grace, ~ re-arms', r))
        print('  C: %s' % r)
    finally:
        try:
            g.cmd('M5', timeout=1)
        except Exception:
            pass
        if 'Hold' in g.status():
            g.rt(b'\x18')
    ok = bool(results) and all(r == 'ok' for _n, r in results)
    print('\n--- results ---')
    for name, r in results:
        print('  %s: %s' % (name, r))
    print('\n%s: %s' % ('PASS' if ok else 'FAIL',
                        'held and resumed under M4 and M3, and the hold past the grace '
                        're-armed on ~ and finished; now judge the marks: the M4 pause '
                        'against the corner, the M3 pause for a dark lead' if ok
                        else 'see above'))
    return 0 if ok else 1


def drill_ctrlstart(g):
    """Resume supervision after expstop: POST /controller/start, then
    report /mode. No motion, no laser."""
    code, body = post_ctrl('start')
    print('POST /controller/start -> %s %s' % (code, body.strip()))
    time.sleep(6)
    try:
        print('/mode after start: %s' % get_json('/mode'))
    except Exception as e:
        print('/mode after start: %s' % e)
    return []


def main():
    drill = sys.argv[1] if len(sys.argv) > 1 else ''
    drills = {'witness': drill_witness, 'hold': drill_hold,
              'ircut': drill_ircut,
              'pthresh': drill_pthresh, 'dladder': drill_dladder,
              'pcurve': drill_pcurve, 'm5dark': drill_m5dark,
              'dpatch': drill_dpatch, 'flowload': drill_flowload,
              'm4corner': drill_m4corner, 'm4feeds': drill_m4feeds,
              'xymode': drill_xymode, 'xycircle': drill_xycircle,
              'senderchg': drill_senderchg, 'overrun': drill_overrun,
              'holdres': drill_holdres,
              'expstop': drill_expstop, 'ctrlstart': drill_ctrlstart}
    if drill not in drills:
        print(__doc__)
        return 2
    if drill == 'ctrlstart' or (drill == 'flowload' and sys.argv[2:3] == ['fit']):
        return drills[drill](None) or 0      # no controller connection needed
    g = Grbl(HOST, PORT)
    try:
        rc = drills[drill](g)
    finally:
        # Always leave the laser commanded off.
        try:
            g.cmd('M5', timeout=1)
        except Exception:
            pass
    return rc or 0          # the bench page's verdict is this exit status


if __name__ == '__main__':
    sys.exit(main())
