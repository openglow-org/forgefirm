#!/usr/bin/env python3
# Copyright 2026 514 LLC d/b/a OpenGlow
# Written by Scott Wiederhold
# https://community.openglow.org
# SPDX-License-Identifier:    MIT
"""LASER_PGOOD (J1_14) against the rest of the laser chain, through the
kernel's readbacks.

Runs ON the board as root. Polls the kernel's GPIO readbacks (sysfs, one
open descriptor each, re-read with pread) and the switch device's EV_SW
word (EVIOCGSW) as fast as the loop allows, a few hundred hertz, and
reports every transition of the watched lines with a timestamp, plus a
per-line summary, so the meaning of the supply's line can be read off
against what the chain and the supply were doing: idle, a dry run
(HV_ENABLE follows the charge pump), an armed cut (LASER_ON, FIRE,
hv_current), a pause and a resume, a lid open. hv_current comes from the
PIC at a lower rate and rides along as a range.

PGOOD is the raw pin level as the kernel publishes it (laser_pgood is the
supply's power-good: 1 = the supply reports its outputs within spec).
Everything else is the kernel's logical value.

The loop must not hog the CPU: single core, the protocol thread is
SCHED_OTHER, so the sampler sleeps between passes and reports its worst gap.

Usage: pgood_probe.py [--secs N] [--json FILE]
Drive the machine from a sender or the button meanwhile; the probe only
watches. GRBL mode, any state.
"""
import argparse
import fcntl
import json
import os
import struct
import sys
import time

CNC = '/sys/glowforge/cnc/'
ATTRS = [                                   # name, attribute, invert-to-raw
    ('PGOOD',     CNC + 'laser_pgood',       False),
    ('LASER_ON',  CNC + 'laser_on',          False),
    ('FIRE',      CNC + 'laser_enable',      False),
    ('CP_ALIVE',  CNC + 'charge_pump_alive', False),
]
SWITCH_DEV = '/dev/input/event0'
SW_BITS = [('HV_ENABLE', 4), ('DOORS', 3)]  # EV_SW codes on the switch device
EVIOCGSW = (2 << 30) | (8 << 16) | (0x45 << 8) | 0x1b
HV_CURRENT = '/sys/glowforge/pic/hv_current'
STATE = CNC + 'state'
HV_HZ = 20.0
SLEEP_S = 0.001


def rd(fd):
    return os.pread(fd, 32, 0).strip()


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--secs', type=float, default=60.0)
    ap.add_argument('--json', default='')
    args = ap.parse_args()
    try:
        os.nice(5)
    except OSError:
        pass
    fds = [(n, os.open(p, os.O_RDONLY), inv) for n, p, inv in ATTRS]
    sw = os.open(SWITCH_DEV, os.O_RDONLY)
    hv_fd = os.open(HV_CURRENT, os.O_RDONLY)
    st_fd = os.open(STATE, os.O_RDONLY)

    def sample():
        s = {}
        for n, fd, inv in fds:
            v = int(rd(fd) or b'0')
            s[n] = (1 - v) if inv else v
        buf = fcntl.ioctl(sw, EVIOCGSW, b'\0' * 8)
        for n, bit in SW_BITS:
            s[n] = (buf[bit >> 3] >> (bit & 7)) & 1
        return s

    t0 = time.monotonic()
    state = sample()
    hv = int(rd(hv_fd) or b'0')
    print('t=%8.3f start %s hv=%d kstate=%s'
          % (0.0, ' '.join('%s=%d' % kv for kv in state.items()), hv, rd(st_fd).decode()))
    sys.stdout.flush()
    trans = []
    hv_log = []
    counts = {k: [0, 0] for k in state}
    worst_gap = 0.0
    n = 0
    next_hv = t0
    t_prev = t0
    while True:
        now = time.monotonic()
        if now - t0 > args.secs:
            break
        gap = now - t_prev
        if gap > worst_gap:
            worst_gap = gap
        t_prev = now
        new = sample()
        n += 1
        for k in new:
            if new[k] != state[k]:
                trans.append((now - t0, k, new[k]))
                print('t=%8.3f %-9s -> %d   hv=%s kstate=%s'
                      % (now - t0, k, new[k], rd(hv_fd).decode(), rd(st_fd).decode()))
                sys.stdout.flush()
            counts[k][new[k]] += 1
        state = new
        if now >= next_hv:
            hv_log.append((now - t0, int(rd(hv_fd) or b'0')))
            next_hv = now + 1.0 / HV_HZ
        time.sleep(SLEEP_S)
    secs = time.monotonic() - t0
    print('--- %.1f s, %d passes (%.0f Hz), worst gap %.1f ms' % (secs, n, n / secs, worst_gap * 1e3))
    for k, (c0, c1) in counts.items():
        print('  %-9s 0: %6.2f %%   1: %6.2f %%' % (k, 100.0 * c0 / n, 100.0 * c1 / n))
    if hv_log:
        vals = [v for _t, v in hv_log]
        lit = [t for t, v in hv_log if v > 20]
        print('  hv_current min %d max %d; > 20 raw for %.1f s%s'
              % (min(vals), max(vals), len(lit) / HV_HZ,
                 (' (%.1f .. %.1f s)' % (lit[0], lit[-1])) if lit else ''))
    if args.json:
        with open(args.json, 'w') as f:
            json.dump({'secs': secs, 'passes': n, 'worst_gap_ms': worst_gap * 1e3,
                       'counts': counts, 'transitions': trans, 'hv': hv_log}, f)
        print('record: %s' % args.json)
    return 0


if __name__ == '__main__':
    sys.exit(main())
