#!/usr/bin/env python3
# Copyright 2026 514 LLC d/b/a OpenGlow
# Written by Scott Wiederhold
# https://community.openglow.org
# SPDX-License-Identifier:    MIT
"""Coolant temperature spot-check helper (runs on the board or from a
host; gfbench: GF_HOST).

The documented raw->Celsius conversion (docs.forgefirm.org, sensors) is the factory B-equation (10k
B3380 NTC in a 10k divider behind a 1.3x gain stage, 10-bit ADC). This
tool collects reference points - a measured real temperature paired with
the machine's raw ADC readings - and fits a per-machine line to
cross-check that curve against a thermometer.

Usage:
  temp_calibrate.py watch [seconds]            live raw + current-formula C
                                               (default 60 s)
  temp_calibrate.py point <measured_C> [note]  record a calibration point
  temp_calibrate.py fit                        fit and print the calibration

  temp_calibrate.py supply-watch [seconds]     the same for the power supply
  temp_calibrate.py supply-point <C> [note]    sensor (pic/pwr_temp, raw):
  temp_calibrate.py supply-fit                 thermometer on the heatsink vs
                                               the raw count; the documented guess
                                               raw * 0.08715 - 21 is shown
                                               beside it. Points go to
                                               supply_calibration.json.

Points accumulate in temp_calibration.json in the bench data directory
(gfbench.data_path: next to this script, or FORGETEST_BENCH_DATA). Take
at least two points as far apart in temperature as practical (e.g. cold
machine in the morning, and warm after a fan-off soak with the flow
heater on).
"""
import json
import os
import sys
import time

from gfbench import board, degc, data_path

STORE = data_path('temp_calibration.json')
SUPPLY_STORE = data_path('supply_calibration.json')


def supply_guess_c(raw):
    """The documented unverified guess for pic/pwr_temp."""
    return raw * 0.08715 - 21


def supply_raw(samples=5, delay=1.0):
    acc, n = 0, 0
    for _ in range(samples):
        out = board('cat /sys/glowforge/pic/pwr_temp').strip()
        if out.isdigit():
            acc += int(out); n += 1
        time.sleep(delay)
    return acc / n if n else None


def uapi_c(raw):
    """The documented factory conversion (B-equation NTC behind divider + gain)."""
    return degc(raw)


def raws(samples=5, delay=1.0):
    """Average several readings of both sensors (ADC noise is real)."""
    acc1, acc2, n = 0, 0, 0
    for _ in range(samples):
        out = board('cat /sys/glowforge/pic/water_temp_1 /sys/glowforge/pic/water_temp_2').split()
        if len(out) == 2:
            acc1 += int(out[0]); acc2 += int(out[1]); n += 1
        time.sleep(delay)
    return (acc1 / n, acc2 / n) if n else (None, None)


def load():
    if os.path.exists(STORE):
        with open(STORE) as f:
            return json.load(f)
    return {'points': []}


def save(data):
    with open(STORE, 'w') as f:
        json.dump(data, f, indent=2)


def fit(points, key):
    """Least-squares line: measured_C = slope * raw + offset."""
    xs = [p[key] for p in points]
    ys = [p['measured_c'] for p in points]
    n = len(xs)
    mx = sum(xs) / n
    my = sum(ys) / n
    den = sum((x - mx) ** 2 for x in xs)
    if den == 0:
        return None, None
    slope = sum((x - mx) * (y - my) for x, y in zip(xs, ys)) / den
    return slope, my - slope * mx


def supply_main(mode):
    if mode == 'supply-watch':
        seconds = float(sys.argv[2]) if len(sys.argv) > 2 else 60.0
        print('pwr_temp raw   guess-C   (%.0f s)' % seconds)
        t0 = time.time()
        while time.time() - t0 < seconds:
            r = supply_raw(1, 0)
            if r is None:
                print('  (no reading)')
            else:
                print('  %6.1f      %.2f' % (r, supply_guess_c(r)), flush=True)
            time.sleep(2)
        return 0
    if mode == 'supply-point':
        try:
            measured = float(sys.argv[2])
        except (IndexError, ValueError):
            print('supply-point needs the thermometer reading in C (value)')
            return 2
        note = sys.argv[3] if len(sys.argv) > 3 else ''
        print('sampling pwr_temp (5 s)...', flush=True)
        r = supply_raw()
        if r is None:
            print('no reading from the machine')
            return 1
        data = {'points': []}
        if os.path.exists(SUPPLY_STORE):
            with open(SUPPLY_STORE) as f:
                data = json.load(f)
        data['points'].append({'measured_c': measured, 'raw': r, 'note': note,
                               'when': time.strftime('%Y-%m-%d %H:%M:%S')})
        with open(SUPPLY_STORE, 'w') as f:
            json.dump(data, f, indent=2)
        print('recorded: measured %.2f C  raw=%.1f (guess %.1f C)  (%d points total in %s)'
              % (measured, r, supply_guess_c(r), len(data['points']), SUPPLY_STORE))
        return 0
    if mode == 'supply-fit':
        if not os.path.exists(SUPPLY_STORE):
            print('no points yet')
            return 1
        with open(SUPPLY_STORE) as f:
            pts = json.load(f)['points']
        if len(pts) < 2:
            print('need at least 2 points (have %d)' % len(pts))
            return 1
        print('points:')
        for p in pts:
            print('  %6.2f C   raw=%.1f  guess %.1f C   %s %s'
                  % (p['measured_c'], p['raw'], supply_guess_c(p['raw']), p['when'], p['note']))
        slope, offset = fit(pts, 'raw')
        if slope is None:
            print('points share one raw value; no fit')
            return 1
        print('fit: degC = %.5f * raw + %.2f   (documented guess: 0.08715 * raw - 21)' % (slope, offset))
        worst = max(abs(p['measured_c'] - supply_guess_c(p['raw'])) for p in pts)
        print('the guess is off by at most %.1f C at these points' % worst)
        return 0
    print(__doc__)
    return 2


def main():
    mode = sys.argv[1] if len(sys.argv) > 1 else 'watch'
    if mode.startswith('supply-'):
        return supply_main(mode)

    if mode == 'watch':
        seconds = float(sys.argv[2]) if len(sys.argv) > 2 else 60.0
        print('raw1(down) raw2(up)   uapi-C down/up   (%.0f s)' % seconds)
        t0 = time.time()
        while time.time() - t0 < seconds:
            r1, r2 = raws(1, 0)
            if r1 is None:
                print('  (no reading)')
            else:
                print('  %6.1f   %6.1f      %.2f / %.2f'
                      % (r1, r2, uapi_c(r1), uapi_c(r2)), flush=True)
            time.sleep(2)

    elif mode == 'point':
        try:
            measured = float(sys.argv[2])
        except (IndexError, ValueError):
            print('point needs the thermometer reading in C (value)')
            return 2
        note = sys.argv[3] if len(sys.argv) > 3 else ''
        print('sampling raws (10 s)...', flush=True)
        r1, r2 = raws()
        if r1 is None:
            print('no readings from the machine')
            return 1
        data = load()
        data['points'].append({'measured_c': measured, 'raw1': r1, 'raw2': r2,
                               'note': note, 'when': time.strftime('%Y-%m-%d %H:%M:%S')})
        save(data)
        print('recorded: measured %.2f C  raw1=%.1f raw2=%.1f  (%d points total in %s)'
              % (measured, r1, r2, len(data['points']), STORE))

    elif mode == 'fit':
        data = load()
        pts = data['points']
        if len(pts) < 2:
            print('need at least 2 points (have %d)' % len(pts))
            return 1
        print('points:')
        for p in pts:
            print('  %6.2f C   raw1=%.1f raw2=%.1f   %s %s'
                  % (p['measured_c'], p['raw1'], p['raw2'], p['when'], p['note']))
        span = max(p['measured_c'] for p in pts) - min(p['measured_c'] for p in pts)
        print('\ntemperature span: %.2f C%s' % (span, '  (WARNING: <3 C span, fit is weak)' if span < 3 else ''))
        for key, label in (('raw1', 'downstream (water_temp_1)'), ('raw2', 'upstream (water_temp_2)')):
            slope, offset = fit(pts, key)
            print('\n%s:' % label)
            print('  fitted:  C = raw * %.6f + %.4f' % (slope, offset))
            for raw in (600, 650, 700, 750, 800):
                print('    raw %3d -> fitted %6.2f C   uapi %6.2f C   diff %+.2f'
                      % (raw, raw * slope + offset, uapi_c(raw),
                         (raw * slope + offset) - uapi_c(raw)))
    else:
        print(__doc__)
        return 1
    return 0


if __name__ == '__main__':
    sys.exit(main())
