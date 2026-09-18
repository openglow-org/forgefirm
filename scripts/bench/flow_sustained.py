#!/usr/bin/env python3
# Copyright 2026 514 LLC d/b/a OpenGlow
# Written by Scott Wiederhold
# https://community.openglow.org
# SPDX-License-Identifier:    MIT
"""Sustained-load test for periodic flow re-checks.

Question: what does repeated interrogation cost thermally over a long
job, and does the loop reach equilibrium or climb without bound? Runs
the driver's real check cadence (M8 held for the duration) with the
cut-profile fans, logging bulk coolant temperature and every verdict.

Needs the controller running with forgectrl's cooling engine (the
verdict lines arrive on the Grbl socket). Runs on the board or from a
host (gfbench: GF_HOST).

Usage: flow_sustained.py [minutes]   (default 30)
"""
import re
import socket
import sys
import time

from gfbench import HOST, board, degc, setting

THRESHOLD = float(setting('cool_flow_rise', 14.4))    # forgectrl's configured threshold


def temps():
    o = board('cat /sys/glowforge/pic/water_temp_1 /sys/glowforge/pic/water_temp_2').split()
    return degc(o[0]), degc(o[1])


minutes = float(sys.argv[1]) if len(sys.argv) > 1 else 30

s = socket.create_connection((HOST, 23), timeout=5)
s.settimeout(0.3)
time.sleep(0.5)
try:
    while s.recv(4096):
        pass
except socket.timeout:
    pass

d0, u0 = temps()
print('start: down=%.2f up=%.2f  running M8 for %.0f min' % (d0, u0, minutes))
s.sendall(b'M8\n')

t0 = time.time()
verdicts = []
peak_up = u0
buf = ''
while time.time() - t0 < minutes * 60:
    try:
        buf += s.recv(4096).decode('ascii', 'replace')
    except socket.timeout:
        pass
    for line in buf.split('\n'):
        if 'flow' in line and ('verified' in line or 'FAULT' in line):
            m = re.search(r'rise ([\d.]+) C', line)
            if m and (not verdicts or verdicts[-1][1] != float(m.group(1))):
                el = (time.time() - t0) / 60
                verdicts.append((el, float(m.group(1)), 'FAULT' in line))
                d, u = temps()
                peak_up = max(peak_up, u)
                print('  %5.1f min  rise=%5.2f  %-8s  loop down=%.2f up=%.2f (%+.2f from start)'
                      % (el, float(m.group(1)), 'FAULT' if 'FAULT' in line else 'ok', d, u, u - u0),
                      flush=True)
    buf = buf[-2000:]
    time.sleep(5)

s.sendall(b'M9\n')
time.sleep(1)
s.close()

d, u = temps()
print()
print('checks run: %d over %.0f min' % (len(verdicts), minutes))
if verdicts:
    rises = [v[1] for v in verdicts]
    print('rise values: min=%.2f max=%.2f mean=%.2f  (threshold %.1f)'
          % (min(rises), max(rises), sum(rises) / len(rises), THRESHOLD))
    print('false faults: %d' % sum(1 for v in verdicts if v[2]))
print('loop temperature: start %.2f -> end %.2f (%+.2f C), peak %.2f'
      % (u0, u, u - u0, peak_up))
print('cadence: %.1f min between checks' % (minutes / max(1, len(verdicts))))
