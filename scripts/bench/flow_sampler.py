#!/usr/bin/env python3
# Copyright 2026 514 LLC d/b/a OpenGlow
# Written by Scott Wiederhold
# https://community.openglow.org
# SPDX-License-Identifier:    MIT
"""Board-side coolant sampler: prints 'elapsed,raw_down,raw_up' lines.
Usage: flow_sampler.py <duration_s> <interval_s>
Kept on the board so sampling cadence is not at the mercy of ssh
round-trip latency."""
import sys
import time

dur = float(sys.argv[1])
iv = float(sys.argv[2])
t0 = time.time()

while True:
    el = time.time() - t0
    if el > dur:
        break
    with open('/sys/glowforge/pic/water_temp_1') as f:
        r1 = f.read().strip()
    with open('/sys/glowforge/pic/water_temp_2') as f:
        r2 = f.read().strip()
    print('%.2f,%s,%s' % (el, r1, r2), flush=True)
    time.sleep(iv)
