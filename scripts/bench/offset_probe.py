#!/usr/bin/env python3
"""Coolant-sensor offset probe: which actuator moves both thermistors?

The coolant ADC reads about 1 C low on both sensors while the run airflow
profile is on, stepping in one sample after the fans start, out when they
stop, and toggling between two levels in between. The two thermistor
lines ride J2 pins 3 and 4 beside the pump enable (2), the TEC enable (1),
the heater PWM (7), the exhaust tach (8) and the exhaust PWM (9); the
intake fans and the HV lines are on J1, the air assist on the head. This
probe switches one actuator at a time, dark (no laser, no press), with
both sensors sampled at 25 Hz, and scores the common-mode step at every
edge and the toggling inside every dwell. Runs on the board with
forgectrl idle (the engine writes the fans only at session transitions,
so a write here stands until the next session); every value is restored
on the way out, whatever happens. The pump is stopped for one short dwell
with the tube dark and the heater off, the one pump-off state allowed.

Usage: offset_probe.py [repeat]   (repeats of the exhaust-run edge, default 2)
       offset_probe.py ladder     (the air-assist duty ladder alone: the step
                                   against duty tells a current-proportional
                                   ground drop from a threshold)
       offset_probe.py jog        (the air assist steady at its run duty while
                                   the gantry jogs, dark: do the readings toggle
                                   under motion? controller idle in GRBL mode,
                                   35 mm +X and 10 mm +Y free; no laser, no press)
Record: FORGETEST_BENCH_DATA or /tmp, offset_probe_<stamp>.json (jog: offset_jog_<stamp>.json)
"""
import json
import math
import os
import sys
import threading
import time

# forgectrl on the board (FORGECTRL_PORT overrides the port)
BASE = 'http://127.0.0.1:%s' % (os.environ.get('FORGECTRL_PORT') or '80')

SYSFS = '/sys/glowforge/'
LEDS = '/sys/class/leds/'
HZ = 25
PRE, POST, GAP = 1.5, 1.5, 0.3      # seconds around an edge for the step means

# B-equation from status.c: 10k B3380 NTC in a 10k divider behind a 1.3x
# reference.
ADC_F = 1024.0 * 1.3
RINF = 10000.0 * math.exp(-3380.0 / 298.15)


from gfbench import degc


def rd(path):
    try:
        with open(path) as f:
            return f.read().strip()
    except OSError:
        return None


def wr(path, val):
    with open(path, 'w') as f:
        f.write(str(val))


class Sampler(threading.Thread):
    def __init__(self):
        super().__init__(daemon=True)
        self.samples = []
        self.stop = threading.Event()

    def run(self):
        period = 1.0 / HZ
        nxt = time.time()
        while not self.stop.is_set():
            t = time.time()
            a, b = rd(SYSFS + 'pic/water_temp_1'), rd(SYSFS + 'pic/water_temp_2')
            self.samples.append((t, int(a) if a and a.isdigit() else None,
                                 int(b) if b and b.isdigit() else None))
            nxt += period
            d = nxt - time.time()
            if d > 0:
                time.sleep(d)
            else:
                nxt = time.time()


def mean_c(samples, t0, t1, idx):
    v = [degc(s[idx]) for s in samples if t0 <= s[0] < t1 and s[idx] is not None]
    v = [x for x in v if x == x]
    return sum(v) / len(v) if v else None


def toggles(samples, t0, t1):
    """Level steps inside a dwell: both sensors' half-second means moving
    0.45 C or more the same way, agreeing within 0.4 C (the flowload
    detector)."""
    pts = [(s[0], degc(s[1]), degc(s[2])) for s in samples
           if t0 <= s[0] < t1 and s[1] is not None and s[2] is not None]
    pts = [p for p in pts if p[1] == p[1] and p[2] == p[2]]
    w, gap, n = 12, 2, 0
    i = w + gap
    while i < len(pts) - w - gap:
        pre = pts[i - gap - w:i - gap]
        post = pts[i + gap:i + gap + w]
        dd = sum(p[1] for p in post) / w - sum(p[1] for p in pre) / w
        du = sum(p[2] for p in post) / w - sum(p[2] for p in pre) / w
        if abs(dd) >= 0.45 and abs(du) >= 0.45 and (dd > 0) == (du > 0) and abs(dd - du) <= 0.4:
            n += 1
            i += w + 2 * gap
        else:
            i += 1
    return n


def run_jog():
    """The air assist steady at its run duty while the gantry jogs, dark:
    does motion make the readings toggle between two levels? Phases: still
    / fan on still / fan on jogging / fan on still / fan off jogging / fan
    off still. Needs the controller idle in GRBL mode and 35 mm of free +X
    and 10 mm of +Y travel; no laser, no press. The Grbl client comes from
    live_fire_drills (the bench directory on PYTHONPATH)."""
    from live_fire_drills import Grbl, HOST, PORT
    aa = SYSFS + 'head/air_assist_pwm'
    orig = rd(aa)
    g = Grbl(HOST, PORT)
    st = g.status()
    if 'Idle' not in st:
        print('REFUSED: controller is %s, expected Idle' % st)
        return 2
    print('G91/G21: %s / %s' % (g.cmd('G91'), g.cmd('G21')))
    sampler = Sampler()
    sampler.start()
    tach = []
    phases = []
    stop_motion = threading.Event()

    def mover():
        while not stop_motion.is_set():
            for ln in ('G0 X30 F3000', 'G0 Y8 F3000', 'G0 X-30 F3000', 'G0 Y-8 F3000'):
                g.s.sendall(ln.encode() + b'\n')
            g.wait_state('Idle', 20)
            if stop_motion.is_set():
                break
            time.sleep(0.2)

    def phase(label, fan, motion, dwell):
        t0 = time.time()
        if fan is not None:
            wr(aa, fan)
        th = None
        if motion:
            stop_motion.clear()
            th = threading.Thread(target=mover, daemon=True)
            th.start()
        end = t0 + dwell
        while time.time() < end:
            v = rd(SYSFS + 'head/air_assist_tach')
            tach.append((time.time(), int(v) if v and v.isdigit() else None))
            time.sleep(1.0)
        if th:
            stop_motion.set()
            th.join(25)
            g.wait_state('Idle', 20)
        phases.append((label, t0, time.time()))
        print('  %+7.1f s  %s done' % (time.time() - phases[0][1], label), flush=True)

    try:
        phase('still, fan idle', None, False, 10)
        phase('fan run, still', 1023, False, 15)
        phase('fan run, jogging', None, True, 40)
        phase('fan run, still again', None, False, 10)
        phase('fan idle, jogging', 204, True, 30)
        phase('fan idle, still', None, False, 10)
    finally:
        stop_motion.set()
        try:
            g.wait_state('Idle', 20)
            g.cmd('G90')
        except Exception:
            pass
        if orig is not None:
            wr(aa, orig)
        time.sleep(2)
        sampler.stop.set()
        sampler.join(2)
    tr = sampler.samples
    print('\n%d samples, %.1f Hz' % (len(tr), (len(tr) - 1) / (tr[-1][0] - tr[0][0])))
    print('--- per phase: level toggles (both sensors, >= 0.45 C), the readings and the air-assist tach ---')
    print('    phase                    toggles   down mean/sd     up mean/sd     tach mean/sd')
    rows = []
    for label, t0, t1 in phases:
        d = [degc(s[1]) for s in tr if t0 + 1 <= s[0] < t1 and s[1] is not None]
        u = [degc(s[2]) for s in tr if t0 + 1 <= s[0] < t1 and s[2] is not None]
        tk = [v for t, v in tach if t0 <= t < t1 and v is not None]
        sd = lambda v: (sum((x - sum(v) / len(v)) ** 2 for x in v) / len(v)) ** 0.5 if len(v) > 1 else 0.0
        tg = toggles(tr, t0 + 1.0, t1)
        rows.append({'phase': label, 'toggles': tg, 'down_mean': sum(d) / len(d) if d else None,
                     'down_sd': sd(d), 'up_mean': sum(u) / len(u) if u else None, 'up_sd': sd(u),
                     'tach_mean': sum(tk) / len(tk) if tk else None, 'tach_sd': sd(tk), 'dwell_s': t1 - t0})
        print('  %-26s %5d    %6.2f/%.2f     %6.2f/%.2f    %s'
              % (label, tg, rows[-1]['down_mean'] or 0, sd(d), rows[-1]['up_mean'] or 0, sd(u),
                 '-' if not tk else '%.0f/%.0f' % (rows[-1]['tach_mean'], sd(tk))))
    rec = {'drill': 'offset_probe_jog', 'date': time.strftime('%Y-%m-%dT%H:%M:%S'),
           'phases': rows, 'tach': tach, 'trace': tr}
    ddir = os.environ.get('FORGETEST_BENCH_DATA') or '/tmp'
    path = os.path.join(ddir, 'offset_jog_%s.json' % time.strftime('%Y%m%d-%H%M%S'))
    with open(path, 'w') as f:
        json.dump(rec, f)
    print('record: %s' % path)
    return 0


def run_armed():
    """An armed window with the tube dark: M3 S0 opens the window on the
    press (the fire level is zero, nothing is ever requested), a 60 s
    dwell, then M2. Phases: before the session / session open, waiting
    for the press / armed (the HV enable asserted) / after M2. Any lit
    sample (LASER_ON witness or tube current) soft-resets at once."""
    import json as _json
    import urllib.request
    from live_fire_drills import Grbl, HOST, PORT
    g = Grbl(HOST, PORT)
    st = g.status()
    if 'Idle' not in st:
        print('REFUSED: controller is %s, expected Idle' % st)
        return 2
    print('spindle off: %s' % g.cmd('M5'))

    def status():
        try:
            with urllib.request.urlopen(BASE + '/status', timeout=2) as r:
                s = _json.load(r)
            with urllib.request.urlopen(BASE + '/cool/status', timeout=2) as r:
                c = _json.load(r)
            return {'t': time.time(), 'armed': c.get('armed'), 'phase': c.get('phase'),
                    'hv_enable': (s.get('switches') or {}).get('hv_enable'),
                    'emission': (s.get('laser') or {}).get('emission_samples'),
                    'hv': s.get('hv_current_raw')}
        except Exception:
            return None
    pre = status()
    if not pre or pre['armed']:
        print('REFUSED: forgectrl unreachable or the window is already armed (%s)' % pre)
        return 2
    print('>>> ARMED DARK DWELL: the button lights on the M3; press it. The fire level is')
    print('>>> zero and nothing is commanded; any lit sample soft-resets the job.')
    sampler = Sampler()
    sampler.start()
    poll = []
    lit = []
    stop = threading.Event()

    def poller():
        while not stop.is_set():
            s = status()
            if s:
                poll.append(s)
                if (s['emission'] or 0) > 0 or (s['hv'] or 0) > 20:
                    lit.append(s)
            v = rd(SYSFS + 'cnc/laser_on_sampled')
            if v and v.isdigit() and int(v) > 0:
                lit.append({'t': time.time(), 'lon': int(v)})
            time.sleep(0.5)
    th = threading.Thread(target=poller, daemon=True)
    th.start()
    phases = []
    t_m3 = None
    try:
        time.sleep(8)
        phases.append(('before the session', sampler.samples[0][0], time.time()))
        print('G91/G21: %s / %s' % (g.cmd('G91'), g.cmd('G21')))
        t_m3 = time.time()
        for ln in ('M3 S0', 'G4 P60', 'M5', 'G90', 'M2'):
            g.s.sendall(ln.encode() + b'\n')
        # wait for the press: the engine's armed goes true
        t_armed = None
        deadline = time.time() + 300
        while time.time() < deadline and not lit:
            g.drain()
            if poll and poll[-1]['armed']:
                t_armed = poll[-1]['t']
                break
            time.sleep(0.2)
        if lit:
            raise RuntimeError('lit sample before the press: %s' % lit[:2])
        if t_armed is None:
            raise RuntimeError('no press within 300 s')
        phases.append(('session open, waiting for the press', t_m3, t_armed))
        print('  armed %.1f s after the M3' % (t_armed - t_m3), flush=True)
        # the dwell: armed, dark
        t_end = None
        while time.time() < t_armed + 75 and not lit:
            g.drain()
            if poll and not poll[-1]['armed'] and time.time() > t_armed + 5:
                t_end = poll[-1]['t']
                break
            time.sleep(0.2)
        if lit:
            raise RuntimeError('LIT SAMPLE DURING THE ARMED DWELL: %s' % lit[:2])
        t_end = t_end or time.time()
        phases.append(('armed, dark dwell', t_armed, t_end))
        print('  window closed %.1f s after the arm' % (t_end - t_armed), flush=True)
        time.sleep(20)
        phases.append(('after M2, fans still at run', t_end, time.time()))
    except Exception as e:
        print('ABORT: %s' % e)
        g.rt(b'\x18')
        time.sleep(2)
    finally:
        try:
            g.cmd('M5', timeout=1)
        except Exception:
            pass
        stop.set()
        sampler.stop.set()
        sampler.join(2)
        th.join(2)
    tr = sampler.samples
    print('\n%d samples, %.1f Hz; lit samples: %d' % (len(tr), (len(tr) - 1) / (tr[-1][0] - tr[0][0]), len(lit)))
    print('--- replies ---')
    for t, ln in g.log:
        if ln != 'ok':
            print('  %+7.1f s  %s' % (t - (t_m3 or t), ln))
    print('--- hv_enable / armed transitions ---')
    last = None
    for p in poll:
        k = (p['hv_enable'], p['armed'])
        if k != last:
            print('  %+7.1f s  hv_enable=%s armed=%s phase=%s' % (p['t'] - (t_m3 or p['t']), p['hv_enable'], p['armed'], p['phase']))
            last = k
    print('--- per phase: toggles, readings ---')
    print('    phase                                toggles   down mean/sd     up mean/sd')
    rows = []
    for label, t0, t1 in phases:
        d = [degc(s[1]) for s in tr if t0 + 1 <= s[0] < t1 and s[1] is not None]
        u = [degc(s[2]) for s in tr if t0 + 1 <= s[0] < t1 and s[2] is not None]
        sd = lambda v: (sum((x - sum(v) / len(v)) ** 2 for x in v) / len(v)) ** 0.5 if len(v) > 1 else 0.0
        tg = toggles(tr, t0 + 1.0, t1)
        rows.append({'phase': label, 'toggles': tg, 'down_mean': sum(d) / len(d) if d else None,
                     'down_sd': sd(d), 'up_mean': sum(u) / len(u) if u else None, 'up_sd': sd(u), 'dwell_s': t1 - t0})
        print('  %-38s %5d    %6.2f/%.2f     %6.2f/%.2f'
              % (label, tg, rows[-1]['down_mean'] or 0, sd(d), rows[-1]['up_mean'] or 0, sd(u)))
    rec = {'drill': 'offset_probe_armed', 'date': time.strftime('%Y-%m-%dT%H:%M:%S'), 't_m3': t_m3,
           'phases': rows, 'poll': poll, 'lit': lit, 'replies': g.log, 'trace': tr}
    ddir = os.environ.get('FORGETEST_BENCH_DATA') or '/tmp'
    path = os.path.join(ddir, 'offset_armed_%s.json' % time.strftime('%Y%m%d-%H%M%S'))
    with open(path, 'w') as f:
        json.dump(rec, f)
    print('record: %s' % path)
    return 0 if not lit else 1


def main():
    if len(sys.argv) > 1 and sys.argv[1] == 'jog':
        return run_jog()
    if len(sys.argv) > 1 and sys.argv[1] == 'armed':
        return run_armed()
    ladder = len(sys.argv) > 1 and sys.argv[1] == 'ladder'
    repeat = int(sys.argv[1]) if len(sys.argv) > 1 and sys.argv[1].isdigit() else 2
    attrs = {
        'exhaust': SYSFS + 'thermal/exhaust_pwm',
        'intake': SYSFS + 'thermal/intake_pwm',
        'air_assist': SYSFS + 'head/air_assist_pwm',
        'purge': SYSFS + 'head/purge_air',
        'heater': SYSFS + 'thermal/heater_pwm',
        'pump': SYSFS + 'thermal/water_pump_on',
        'tec': SYSFS + 'thermal/tec_on',
        'lid_led': LEDS + 'lid_led/brightness',
    }
    orig = {k: rd(p) for k, p in attrs.items()}
    print('start values: %s' % orig)
    if orig['pump'] != '1' or orig['heater'] not in ('0', None):
        print('REFUSED: the pump is not on or the heater is not off (%s, %s)' % (orig['pump'], orig['heater']))
        return 2
    # (label, key, value, dwell)
    sched = [('baseline', None, None, 10)]
    if ladder:
        for duty in (256, 512, 768, 1023, 768, 512, 256):
            sched.append(('air assist %d/1023' % duty, 'air_assist', duty, 10))
        sched.append(('air assist idle', 'air_assist', 204, 10))
        for duty in (1023, 204, 1023, 204):
            sched.append(('air assist %d' % duty, 'air_assist', duty, 8))
    for _ in range(0 if ladder else repeat):
        sched += [('exhaust 100% (run, DC)', 'exhaust', 65535, 12), ('exhaust off', 'exhaust', 0, 12)]
    if not ladder:
      sched += [('exhaust 50% (PWM edges)', 'exhaust', 32768, 12), ('exhaust off', 'exhaust', 0, 12),
              ('exhaust 25%', 'exhaust', 16384, 12), ('exhaust off', 'exhaust', 0, 12),
              ('intake 66% (run)', 'intake', 43278, 12), ('intake off', 'intake', 0, 12),
              ('air assist run', 'air_assist', 1023, 12), ('air assist idle', 'air_assist', 204, 12),
              ('purge off', 'purge', 0, 8), ('purge on', 'purge', 1, 8),
              ('heater 40%', 'heater', 26214, 8), ('heater off', 'heater', 0, 14),
              ('pump off (dark, heater off)', 'pump', 0, 8), ('pump on', 'pump', 1, 12),
              ('tec on', 'tec', 1, 6), ('tec off', 'tec', 0, 6),
              ('lid lamp full', 'lid_led', 1023, 6), ('lid lamp back', 'lid_led', orig['lid_led'] or 0, 6),
              ('all run fans', None, None, 0)]
    sampler = Sampler()
    sampler.start()
    edges = []
    try:
        for label, key, val, dwell in sched:
            if label == 'all run fans':
                t = time.time()
                wr(attrs['exhaust'], 65535)
                wr(attrs['intake'], 43278)
                wr(attrs['air_assist'], 1023)
                edges.append((t, label))
                time.sleep(15)
                t = time.time()
                wr(attrs['exhaust'], 0)
                wr(attrs['intake'], 0)
                wr(attrs['air_assist'], 204)
                edges.append((t, 'all fans off'))
                time.sleep(12)
                continue
            if key:
                t = time.time()
                wr(attrs[key], val)
                edges.append((t, label))
                print('  %+7.1f s  %s' % (t - edges[0][0], label), flush=True)
            else:
                edges.append((time.time(), label))
            time.sleep(dwell)
    finally:
        for k, p in attrs.items():
            if orig[k] is not None:
                try:
                    wr(p, orig[k])
                except OSError as e:
                    print('RESTORE FAILED %s: %s' % (k, e))
        time.sleep(3)
        sampler.stop.set()
        sampler.join(2)
    tr = sampler.samples
    t0 = edges[0][0]
    print('\n%d samples, %.1f Hz' % (len(tr), (len(tr) - 1) / (tr[-1][0] - tr[0][0])))
    print('--- edges: common-mode step (1.5 s means after minus before), then toggles inside the dwell ---')
    print('    t(s)   edge                          down step   up step   | common | toggles')
    rows = []
    for i, (t, label) in enumerate(edges):
        t_end = edges[i + 1][0] if i + 1 < len(edges) else tr[-1][0]
        b1, a1 = mean_c(tr, t - PRE - GAP, t - GAP, 1), mean_c(tr, t + GAP, t + GAP + POST, 1)
        b2, a2 = mean_c(tr, t - PRE - GAP, t - GAP, 2), mean_c(tr, t + GAP, t + GAP + POST, 2)
        d1 = None if None in (a1, b1) else a1 - b1
        d2 = None if None in (a2, b2) else a2 - b2
        common = (d1 is not None and d2 is not None and abs(d1) >= 0.4 and abs(d2) >= 0.4
                  and (d1 > 0) == (d2 > 0))
        tg = toggles(tr, t + 2.0, t_end)
        rows.append({'t': t - t0, 'edge': label, 'down_step_c': d1, 'up_step_c': d2,
                     'common': common, 'toggles': tg, 'dwell_s': t_end - t})
        print('  %6.1f   %-28s %8s   %8s  | %-6s | %d'
              % (t - t0, label, '-' if d1 is None else '%+.2f' % d1,
                 '-' if d2 is None else '%+.2f' % d2, 'YES' if common else '', tg))
    rec = {'drill': 'offset_probe', 'date': time.strftime('%Y-%m-%dT%H:%M:%S'), 'orig': orig,
           'edges': rows, 'trace': tr}
    ddir = os.environ.get('FORGETEST_BENCH_DATA') or '/tmp'
    path = os.path.join(ddir, 'offset_probe_%s.json' % time.strftime('%Y%m%d-%H%M%S'))
    with open(path, 'w') as f:
        json.dump(rec, f)
    print('record: %s' % path)
    print('end values: %s' % {k: rd(p) for k, p in attrs.items()})
    return 0


if __name__ == '__main__':
    sys.exit(main())
