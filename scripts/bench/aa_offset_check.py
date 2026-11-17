#!/usr/bin/env python3
# Copyright 2026 514 LLC d/b/a OpenGlow
# Written by Scott Wiederhold
# https://community.openglow.org
# SPDX-License-Identifier:    MIT
"""Does the coolant reading hold still when the run airflow comes on?

With cool_aa_offset_counts at the machine's value the engine takes the
air-assist fan's ground shift off both coolant readings, so /status and
/cool/status should read the same coolant before and after M8 brings the
fans to the run profile, while the raw counts step by the offset. With
the setting at zero the readings drop by about a degree instead. Dark,
no press: M8 opens a run session without a laser-on. Runs on the board
(the bench directory on PYTHONPATH for the Grbl client).

Usage: aa_offset_check.py [dwell_s]
"""
import json
import sys
import time
import urllib.request

from live_fire_drills import Grbl, HOST, PORT, BASE


def raw():
    out = []
    for a in ('pic/water_temp_1', 'pic/water_temp_2'):
        with open('/sys/glowforge/' + a) as f:
            out.append(int(f.read().strip()))
    return out


def reading(v):
    """A coolant reading in degrees, or None: /status gives null for a
    count that is no temperature, and the engine -273.15."""
    return v if isinstance(v, (int, float)) and v > -100 else None


def status():
    with urllib.request.urlopen(BASE + '/status', timeout=2) as r:
        s = json.load(r)
    with urllib.request.urlopen(BASE + '/cool/status', timeout=2) as r:
        c = json.load(r)
    with urllib.request.urlopen(BASE + '/settings', timeout=2) as r:
        st = json.load(r)
    return (reading(s['coolant']['down_c']), reading(s['coolant']['up_c']), reading(c['down_c']),
            reading(c['up_c']), c['phase'], st.get('cool_aa_offset_counts'))


def mean(vals):
    """The mean of the readings, or None when there is none."""
    vs = [v for v in vals if v is not None]
    return sum(vs) / len(vs) if vs else None


def fmt(v, spec='%.2f'):
    return 'no reading' if v is None else spec % v


def step(a, b):
    return 'no reading' if a is None or b is None else '%+.2f' % (a - b)


def mean_over(secs):
    rs, ss = [], []
    end = time.time() + secs
    while time.time() < end:
        rs.append(raw())
        ss.append(status()[:4])
        time.sleep(0.5)
    n = len(rs)
    return ([sum(x[i] for x in rs) / n for i in range(2)],
            [mean([x[i] for x in ss]) for i in range(4)])


def line(r, s):
    return '  raw %.1f/%.1f  /status %s/%s  engine %s/%s' % (r[0], r[1], *(fmt(v) for v in s))


def main():
    dwell = float(sys.argv[1]) if len(sys.argv) > 1 else 20.0
    g = Grbl(HOST, PORT)
    if 'Idle' not in g.status():
        print('REFUSED: controller not Idle')
        return 2
    setting = status()[5]
    print('cool_aa_offset_counts = %s' % setting)
    print('before M8: settling 8 s')
    r0, s0 = mean_over(8.0)
    print(line(r0, s0))
    print('M8: %s' % g.cmd('M8'))
    time.sleep(6.0)                       # the fans to run duty, the offset in
    r1, s1 = mean_over(dwell)
    print('%s  (phase %s)' % (line(r1, s1), status()[4]))
    print('M9: %s' % g.cmd('M9'))
    time.sleep(25.0)                      # the session closes, the fans idle
    r2, s2 = mean_over(8.0)
    print(line(r2, s2))
    print('\nraw counts stepped %+.1f / %+.1f under the run profile; /status moved %s / %s C; '
          'the engine moved %s / %s C'
          % (r1[0] - r0[0], r1[1] - r0[1], *(step(s1[i], s0[i]) for i in range(4))))
    return 0


if __name__ == '__main__':
    sys.exit(main())
