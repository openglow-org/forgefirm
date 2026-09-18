#!/usr/bin/env python3
# Copyright 2026 514 LLC d/b/a OpenGlow
# Written by Scott Wiederhold
# https://community.openglow.org
# SPDX-License-Identifier:    MIT
"""Coolant flow-detection design matrix: heating cost AND detection
precision across heater duty cycles and check durations.

Method. For every (duty, case) the loop is first cooled with the
cut-profile fans back to a common baseline, so every run starts from the
same thermal state and the measured rises are comparable. Then the
heater runs at the given duty while both sensors are sampled at 1 Hz.
Because a single heating trace contains the rise at every elapsed time,
one run yields the metric for ALL candidate check durations at once.

Two questions answered from the same data:
  1. COST  - how much does the check heat the loop? (upstream/bulk rise
             in the flow case, per duty per duration)
  2. PRECISION - how well does it discriminate? (downstream rise, flow
             vs no-flow: separation, worst-case margin, and d' =
             separation / pooled standard deviation)

Repeats are interleaved (all conditions in round 1, then round 2, ...)
so slow ambient drift spreads across conditions instead of confounding
any single one.

Safety: aborts a run if downstream passes ABORT_C; heater off and pump
on at every exit path. Drives the heater, pump and fans directly: run
with forgectrl and the controller stopped for the duration (the bench
page's takeover does that; from a host, stop them first). Runs on the
board or from a host (gfbench: GF_HOST).

Output: incremental JSON to flow_matrix_results.json in the bench data
directory (gfbench.data_path; so partial runs are still usable and
resumable) and a summary table at the end.

Usage: flow_matrix.py [duties] [repeats]   e.g. flow_matrix.py 10,20,40 5
       (or env FM_DUTIES, FM_REPEATS, FM_RESULTS)
"""
import json
import math
import os
import statistics
import sys
import time

from gfbench import board, degc, data_path

RESULTS = data_path(os.environ.get('FM_RESULTS', 'flow_matrix_results.json'))

DUTIES = [int(x) for x in (sys.argv[1] if len(sys.argv) > 1 else
                           os.environ.get('FM_DUTIES', '10,15,20,30,40,50')).split(',')]
REPEATS = int(sys.argv[2]) if len(sys.argv) > 2 else int(os.environ.get('FM_REPEATS', '5'))
RUN_S = 75
SAMPLE_IV = 1.0
DURATIONS = [15, 20, 25, 30, 40, 50, 60, 75]
ABORT_C = 48.0                  # below the factory 50 C idle ceiling
BASE_TOL_C = 0.5                # cooldown target: base + this
COOL_MAX_S = 300

FANS_RUN = ('echo 65535 > /sys/glowforge/thermal/exhaust_pwm; '
            'echo 43278 > /sys/glowforge/thermal/intake_pwm; '
            'echo 204 > /sys/glowforge/head/air_assist_pwm')


def temps():
    o = board('cat /sys/glowforge/pic/water_temp_1 /sys/glowforge/pic/water_temp_2').split()
    return degc(o[0]), degc(o[1])


def heater(pct):
    board('echo %d > /sys/glowforge/thermal/heater_pwm' % int(65535 * pct / 100))


def pump(on):
    board('echo %d > /sys/glowforge/thermal/water_pump_on' % (1 if on else 0))


def safe_state():
    heater(0)
    pump(True)


def cool_to(base, log):
    """Cool with cut-profile fans until upstream is back near base."""
    safe_state()
    board(FANS_RUN)
    t0 = time.time()
    while time.time() - t0 < COOL_MAX_S:
        d, u = temps()
        if u <= base + BASE_TOL_C:
            return u, time.time() - t0
        time.sleep(10)
    d, u = temps()
    log('    (cooldown timeout at %.2f C, target %.2f)' % (u, base + BASE_TOL_C))
    return u, time.time() - t0


def run_case(duty, flow, log):
    """One heating run. Returns dict of rises at each candidate duration."""
    pump(flow)
    time.sleep(3)
    d0, u0 = temps()
    heater(duty)
    raw = board('python3 /usr/share/forgetest/bench/flow_sampler.py %d %.1f' % (RUN_S, SAMPLE_IV),
                timeout=RUN_S + 60)
    heater(0)
    pump(True)

    series = []
    for line in raw.strip().splitlines():
        try:
            el, r1, r2 = line.split(',')
            series.append((float(el), degc(r1), degc(r2)))
        except ValueError:
            continue

    aborted_at = None
    for el, d, u in series:
        if d >= ABORT_C:
            aborted_at = el
            break

    out = {'duty': duty, 'flow': flow, 'start_down': d0, 'start_up': u0,
           'aborted_at': aborted_at, 'samples': len(series), 'at': {}}
    for target in DURATIONS:
        if aborted_at is not None and target > aborted_at:
            continue
        near = [s for s in series if s[0] <= target]
        if not near:
            continue
        el, d, u = near[-1]
        if abs(el - target) > 4:      # no sample close enough
            continue
        out['at'][str(target)] = {'t': el, 'down_rise': d - d0, 'up_rise': u - u0,
                                  'diff_rise': (d - d0) - (u - u0), 'down_abs': d}
    return out


def summarize(runs, log):
    log('')
    log('=== COST: bulk (upstream) rise during a check, flow case, degrees C')
    log('    duty ' + ''.join('%8s' % ('%ds' % t) for t in DURATIONS))
    for duty in DUTIES:
        cells = []
        for t in DURATIONS:
            vals = [r['at'][str(t)]['up_rise'] for r in runs
                    if r['duty'] == duty and r['flow'] and str(t) in r['at']]
            cells.append('%8s' % ('%.2f' % statistics.mean(vals) if vals else '-'))
        log('    %4d%%' % duty + ''.join(cells))

    log('')
    log('=== PRECISION: downstream-rise discrimination (flow vs no-flow)')
    log('    duty  dur   flow mean+-sd    noflow mean+-sd   sep   worst   d-prime')
    best = []
    for duty in DUTIES:
        for t in DURATIONS:
            fv = [r['at'][str(t)]['down_rise'] for r in runs
                  if r['duty'] == duty and r['flow'] and str(t) in r['at']]
            nv = [r['at'][str(t)]['down_rise'] for r in runs
                  if r['duty'] == duty and not r['flow'] and str(t) in r['at']]
            if len(fv) < 2 or len(nv) < 2:
                continue
            fm, fsd = statistics.mean(fv), statistics.stdev(fv)
            nm, nsd = statistics.mean(nv), statistics.stdev(nv)
            sep = nm - fm
            worst = min(nv) - max(fv)
            pooled = math.sqrt((fsd ** 2 + nsd ** 2) / 2) or 1e-9
            dprime = sep / pooled
            cost = statistics.mean([r['at'][str(t)]['up_rise'] for r in runs
                                    if r['duty'] == duty and r['flow'] and str(t) in r['at']])
            best.append((dprime, worst, duty, t, fm, fsd, nm, nsd, sep, cost))
            log('    %4d%% %4ds  %6.2f+-%4.2f    %6.2f+-%4.2f  %5.2f  %+5.2f   %5.1f'
                % (duty, t, fm, fsd, nm, nsd, sep, worst, dprime))

    log('')
    log('=== RANKED by d-prime (separation in pooled standard deviations)')
    log('    rank  duty  dur   d-prime  worst-margin  bulk-cost  threshold')
    for i, b in enumerate(sorted(best, reverse=True)[:12], 1):
        dprime, worst, duty, t, fm, fsd, nm, nsd, sep, cost = b
        log('    %4d  %4d%% %4ds  %7.1f  %+11.2f  %8.2f  %8.2f'
            % (i, duty, t, dprime, worst, cost, (fm + nm) / 2))
    log('')
    log('    (worst-margin = min(no-flow) - max(flow); positive means every')
    log('     observed no-flow run exceeded every observed flow run.')
    log('     bulk-cost = degrees C added to the loop per check.)')


def main():
    t_start = time.time()
    log_path = data_path('flow_matrix_log.txt')
    logf = open(log_path, 'a')

    def log(msg):
        print(msg, flush=True)
        logf.write(msg + '\n')
        logf.flush()

    log('=== flow matrix started %s' % time.strftime('%Y-%m-%d %H:%M:%S'))
    log('duties=%s repeats=%d run=%ds durations=%s' % (DUTIES, REPEATS, RUN_S, DURATIONS))

    board(FANS_RUN)
    safe_state()

    log('initial settle (180 s with fans)...')
    time.sleep(180)
    d, u = temps()
    base = u
    log('baseline: down=%.2f up=%.2f  (target for every run: <= %.2f)'
        % (d, u, base + BASE_TOL_C))

    runs = []
    if os.path.exists(RESULTS):
        try:
            runs = json.load(open(RESULTS)).get('runs', [])
            log('resuming with %d existing runs' % len(runs))
        except Exception:
            runs = []

    total = REPEATS * len(DUTIES) * 2
    n = 0
    try:
        for rep in range(REPEATS):
            for duty in DUTIES:
                for flow in (True, False):
                    n += 1
                    start_t, cool_s = cool_to(base, log)
                    r = run_case(duty, flow, log)
                    r['rep'] = rep
                    r['cooldown_s'] = round(cool_s)
                    r['base_at_start'] = start_t
                    runs.append(r)
                    json.dump({'base': base, 'runs': runs}, open(RESULTS, 'w'), indent=1)
                    at30 = r['at'].get('30') or r['at'].get('25') or {}
                    log('  [%2d/%2d] rep%d duty%3d%% %-7s start=%.2f  down_rise@%s=%s  %s'
                        % (n, total, rep + 1, duty, 'FLOW' if flow else 'NO-FLOW',
                           start_t, '30s' if '30' in r['at'] else '25s',
                           ('%.2f' % at30['down_rise']) if at30 else 'n/a',
                           ('ABORT@%.0fs' % r['aborted_at']) if r['aborted_at'] else ''))
                    log('           elapsed %.1f h' % ((time.time() - t_start) / 3600))
    except KeyboardInterrupt:
        log('interrupted - summarizing what we have')
    finally:
        safe_state()
        board('echo 0 > /sys/glowforge/thermal/exhaust_pwm; '
              'echo 0 > /sys/glowforge/thermal/intake_pwm')

    summarize(runs, log)
    log('results: %s; total elapsed %.2f h' % (RESULTS, (time.time() - t_start) / 3600))
    logf.close()


if __name__ == '__main__':
    sys.exit(main())
