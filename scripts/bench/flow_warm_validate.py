#!/usr/bin/env python3
# Copyright 2026 514 LLC d/b/a OpenGlow
# Written by Scott Wiederhold
# https://community.openglow.org
# SPDX-License-Identifier:    MIT
"""Validate the flow check at a WARM loop baseline - the condition a
real job actually presents.

Every design-matrix run started near ambient (21-23 C). During a cut the
loop sits warmer, where heat loss to ambient is larger, which shrinks
the measured rise in BOTH the flow and no-flow cases and could compress
the margin the threshold depends on. This warms the loop with its own
heater (which plateaus near 28-29 C - the most it can reach unaided),
then runs the real check at 40% / 50 s with the cut-profile fans, and
alternates flow / no-flow so both cases see the same conditions.

Drives the heater, pump and fans directly: run with forgectrl and the
controller stopped (the bench page's takeover does that; from a host,
stop them first). Runs on the board or from a host (gfbench: GF_HOST).
Results and the log go to the bench data directory (gfbench.data_path).

Usage: flow_warm_validate.py [cycles_per_case] [target_c] [warm_budget_min]
       (defaults 3, 28.0, 20: the loop's own heater at 50 percent plateaus
       near 28 to 29 C in a 20 C room, the most a warm baseline can be
       without the tube)
"""
import json
import statistics
import sys
import time

from gfbench import board, degc, data_path, setting

RESULTS = data_path('flow_warm_results.json')

DUTY = 40
CHECK_S = 50
THRESHOLD = float(setting('cool_flow_rise', 14.4))    # forgectrl's configured threshold
WARM_TARGET_C = float(sys.argv[2]) if len(sys.argv) > 2 else 28.0
WARM_MAX_S = 60.0 * (float(sys.argv[3]) if len(sys.argv) > 3 else 20.0)
ABORT_C = 48.0

FANS_RUN = ('echo 65535 > /sys/glowforge/thermal/exhaust_pwm; '
            'echo 43278 > /sys/glowforge/thermal/intake_pwm')
FANS_OFF = ('echo 0 > /sys/glowforge/thermal/exhaust_pwm; '
            'echo 0 > /sys/glowforge/thermal/intake_pwm')


_last_good = (22.0, 22.0)


def temps():
    """Sanity-guarded read. A single glitched sysfs value used to map to
    ~109 C and trip the safety abort, silently ending the warm phase
    after one iteration - which wasted two whole validation runs."""
    global _last_good
    o = board('cat /sys/glowforge/pic/water_temp_1 /sys/glowforge/pic/water_temp_2').split()
    if len(o) != 2:
        return _last_good
    d, u = degc(o[0]), degc(o[1])
    if any(x != x or x < 5.0 or x > 60.0 for x in (d, u)):
        return _last_good
    _last_good = (d, u)
    return d, u


def heater(pct):
    board('echo %d > /sys/glowforge/thermal/heater_pwm' % int(65535 * pct / 100))


def pump(on):
    board('echo %d > /sys/glowforge/thermal/water_pump_on' % (1 if on else 0))


WARM_ROUND_S = 180          # heater on per round, then a mix and a bulk reading
WARM_MIX_S = 45
WARM_PLATEAU_C = 0.15       # a round that lifts the bulk less than this is the end


def warm_to(target, log):
    """Warm with the loop heater in rounds, pump circulating, fans off:
    heater on for WARM_ROUND_S, heater off and WARM_MIX_S of circulation,
    then the bulk read as the two sensors' mean. Judged on that mixed
    bulk, never on the upstream sensor beside the heater, which reaches
    any target within two minutes while the bulk has barely moved (the
    2026-08-29 run stopped at 24.5 C that way). 50 % is the practical
    heater ceiling: the downstream sensor sits at the element and 100 %
    drives it past 50 C in 30 s."""
    pump(True)
    board(FANS_OFF)
    t0 = time.time()
    last_bulk = None
    while time.time() - t0 < WARM_MAX_S:
        heater(50)
        r0 = time.time()
        abort = False
        while time.time() - r0 < WARM_ROUND_S and time.time() - t0 < WARM_MAX_S:
            d, u = temps()
            if d >= ABORT_C:
                log('    warm abort: downstream %.2f C' % d)
                abort = True
                break
            time.sleep(15)
        heater(0)
        if abort:
            break
        time.sleep(WARM_MIX_S)
        d, u = temps()
        bulk = (d + u) / 2.0
        el = (time.time() - t0) / 60
        log('      warming: %.1f min  bulk %.2f (down %.2f, up %.2f)' % (el, bulk, d, u))
        if bulk >= target:
            log('    warm target reached: bulk %.2f at %.1f min' % (bulk, el))
            break
        if last_bulk is not None and bulk - last_bulk < WARM_PLATEAU_C:
            log('    (warming plateaued at %.2f C after %.1f min)' % (bulk, el))
            break
        last_bulk = bulk
    heater(0)
    pump(True)
    time.sleep(WARM_MIX_S)  # mix so the bulk is uniform before measuring
    d, u = temps()
    log('    warmed to down=%.2f up=%.2f in %.1f min' % (d, u, (time.time() - t0) / 60))
    return u


def check(flow, log):
    """The real check: fans at cut profile, 40% heater, 50 s."""
    board(FANS_RUN)
    pump(flow)
    time.sleep(3)
    d0, u0 = temps()
    heater(DUTY)
    raw = board('python3 /usr/share/forgetest/bench/flow_sampler.py %d 1.0' % CHECK_S, timeout=CHECK_S + 60)
    heater(0)
    pump(True)

    series = []
    for line in raw.strip().splitlines():
        try:
            el, r1, r2 = line.split(',')
            series.append((float(el), degc(r1), degc(r2)))
        except ValueError:
            continue
    if not series:
        return None
    el, d, u = series[-1]
    return {'flow': flow, 'base_up': u0, 'base_down': d0, 'down_rise': d - d0,
            'up_rise': u - u0, 'peak_down': max(s[1] for s in series), 't': el}


def main():
    cycles = int(sys.argv[1]) if len(sys.argv) > 1 else 3
    logf = open(data_path('flow_warm_log.txt'), 'a')

    def log(msg):
        print(msg, flush=True)
        logf.write(msg + '\n')
        logf.flush()

    log('=== warm-baseline validation %s  duty=%d%% window=%ds threshold=%.1f'
        % (time.strftime('%Y-%m-%d %H:%M:%S'), DUTY, CHECK_S, THRESHOLD))

    runs = []
    try:
        for c in range(cycles):
            for flow in (True, False):
                log('  cycle %d/%d  %s' % (c + 1, cycles, 'FLOW' if flow else 'NO-FLOW'))
                warm_to(WARM_TARGET_C, log)
                r = check(flow, log)
                if r:
                    r['cycle'] = c
                    runs.append(r)
                    verdict = 'FAULT' if r['down_rise'] > THRESHOLD else 'ok'
                    correct = (verdict == 'FAULT') == (not flow)
                    log('    base_up=%.2f  down_rise=%.2f  -> %-5s  %s  (peak down %.1f C)'
                        % (r['base_up'], r['down_rise'], verdict,
                           'CORRECT' if correct else '*** WRONG ***', r['peak_down']))
                    json.dump(runs, open(RESULTS, 'w'), indent=1)
    finally:
        heater(0)
        pump(True)
        board(FANS_OFF)

    fv = [r['down_rise'] for r in runs if r['flow']]
    nv = [r['down_rise'] for r in runs if not r['flow']]
    log('')
    log('=== warm-baseline results (baselines %.1f - %.1f C)'
        % (min(r['base_up'] for r in runs), max(r['base_up'] for r in runs)))
    if len(fv) >= 2 and len(nv) >= 2:
        log('  flow    n=%d  mean %.2f  sd %.2f  max %.2f' % (len(fv), statistics.mean(fv), statistics.stdev(fv), max(fv)))
        log('  no-flow n=%d  mean %.2f  sd %.2f  min %.2f' % (len(nv), statistics.mean(nv), statistics.stdev(nv), min(nv)))
        log('  worst-case margin: %+.2f C   threshold %.1f sits %+.2f above flow max, %+.2f below no-flow min'
            % (min(nv) - max(fv), THRESHOLD, THRESHOLD - max(fv), min(nv) - THRESHOLD))
    wrong = [r for r in runs if ((r['down_rise'] > THRESHOLD) != (not r['flow']))]
    log('  misclassified: %d of %d' % (len(wrong), len(runs)))
    log('results: %s' % RESULTS)
    logf.close()
    return 1 if wrong else 0


if __name__ == '__main__':
    sys.exit(main())
