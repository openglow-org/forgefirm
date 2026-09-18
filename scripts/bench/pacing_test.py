#!/usr/bin/env python3
# Copyright 2026 514 LLC d/b/a OpenGlow
# Written by Scott Wiederhold
# https://community.openglow.org
# SPDX-License-Identifier:    MIT
"""Pacing-fix bench check - runs ON the board (localhost). Dry motion,
no laser. Verifies:

  1. idle CPU is low (baseline - STATE_IDLE was already coarse-paced);
  2. a job parked in a completed feed hold (Hold:0) is coarse-paced now,
     not busy-spinning at the 200 us motion rate (the fix);
  3. active motion is still tight-paced (the loop rate climbs during the
     move);
  4. a feed-hold mid-move then resume preserves position - i.e. the
     decel-into-hold and resume-ramp sub-phases stay tight-paced and the
     feeder never starves (the safety guard on the fix).

Usage: pacing_test.py [mm] [feed]   (default 30 mm at F600, +X first)
"""
import glob
import os
import socket
import sys
import time

DIST = float(sys.argv[1]) if len(sys.argv) > 1 else 30.0
FEED = float(sys.argv[2]) if len(sys.argv) > 2 else 600.0
HZ = os.sysconf('SC_CLK_TCK')


def controller_pid():
    for c in glob.glob('/proc/*/comm'):
        try:
            if open(c).read().strip() == 'grblHAL_glowfor':
                return int(c.split('/')[2])
        except Exception:
            pass
    raise SystemExit('controller not found')


PID = controller_pid()


def cpu_ticks():
    s = open('/proc/%d/stat' % PID).read().split()
    return int(s[13]) + int(s[14])          # utime + stime (all threads)


def cpu_percent(window):
    a = cpu_ticks()
    time.sleep(window)
    b = cpu_ticks()
    return 100.0 * (b - a) / (HZ * window)


class Grbl:
    def __init__(self):
        self.s = socket.create_connection(('127.0.0.1', 23), timeout=5)
        self.s.settimeout(0.4)
        time.sleep(0.4)
        self.drain()

    def drain(self):
        out = b''
        try:
            while True:
                d = self.s.recv(4096)
                if not d:
                    break
                out += d
        except socket.timeout:
            pass
        return out.decode('ascii', 'replace')

    def send(self, line):
        self.s.sendall((line + '\n').encode())
        time.sleep(0.15)
        return self.drain()

    def rt(self, ch):
        self.s.sendall(ch)

    def status(self):
        self.s.sendall(b'?')
        time.sleep(0.25)
        t = self.drain()
        i, j = t.rfind('<'), t.rfind('>')
        return t[i:j + 1] if i >= 0 and j > i else ''

    def state(self):
        st = self.status()
        return st[1:].split('|')[0] if st else ''

    def mpos(self):
        st = self.status()
        for f in st[1:-1].split('|'):
            if f.startswith('MPos:'):
                return tuple(float(x) for x in f[5:].split(','))
        return None

    def wait(self, want, timeout):
        end = time.time() + timeout
        while time.time() < end:
            if self.state().startswith(want):
                return True
            time.sleep(0.1)
        return False


def main():
    print('controller pid=%d; dry move %.0f mm at F%.0f' % (PID, DIST, FEED))
    g = Grbl()
    st = g.state()
    if 'Alarm' in st or 'Door' in st or 'Hold' in st:
        g.rt(b'\x18')                       # clean slate
        time.sleep(2)
        g.drain()
    g.send('M5')                            # laser off, motion only
    g.send('G91')                           # relative

    print('\n[1] idle baseline')
    idle = cpu_percent(3)
    print('    idle CPU = %.1f%% (expect low)' % idle)

    print('\n[3] active motion pacing (during the move)')
    start = g.mpos()
    g.send('G1 X%.3f F%.0f' % (DIST, FEED))
    time.sleep(0.6)                         # let it get moving
    moving = cpu_percent(1.0)
    st_mv = g.state()
    print('    state=%s CPU during move = %.1f%% (tight-paced)' % (st_mv, moving))

    print('\n[2] feed-hold -> parked Hold:0 (the fix)')
    g.rt(b'!')                              # feed hold
    g.wait('Hold', 5)
    time.sleep(1.5)                         # let the decel complete (Hold:1->0)
    held_state = g.status()
    parked = cpu_percent(3)
    print('    %s' % held_state)
    print('    CPU parked in Hold = %.1f%% (fix: expect low, was ~28%%)' % parked)

    print('\n[4] resume -> verify no lost steps (safety)')
    g.rt(b'~')                              # resume
    if not g.wait('Idle', 30):
        print('    WARNING: did not return to Idle')
    end = g.mpos()
    moved = end[0] - start[0] if (start and end) else None
    ok = moved is not None and abs(moved - DIST) < 0.05
    print('    start X=%.3f end X=%.3f moved=%.3f (expect %.1f) -> %s'
          % (start[0], end[0], moved, DIST, 'PASS' if ok else 'MISMATCH'))

    # Return to the starting position.
    g.send('G1 X%.3f F%.0f' % (-DIST, FEED))
    g.wait('Idle', 30)
    g.send('G90')

    print('\n--- verdict ---')
    print('fix (parked Hold coarse-paced): %s (%.1f%% vs move %.1f%%)'
          % ('PASS' if parked < moving * 0.5 and parked < 8 else 'REVIEW',
             parked, moving))
    print('safety (hold+resume no lost steps): %s' % ('PASS' if ok else 'FAIL'))


if __name__ == '__main__':
    main()
