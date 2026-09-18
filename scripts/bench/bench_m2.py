#!/usr/bin/env python3
# Copyright 2026 514 LLC d/b/a OpenGlow
# Written by Scott Wiederhold
# https://community.openglow.org
# SPDX-License-Identifier:    MIT
"""Milestone-2 motion-quality bench: factory-true rates/accels over TCP.

Runs a bounded, return-to-start jog sequence against grblHAL on the board
(argv[1] or GF_HOST, port 23) and reports peak feed reached, state transitions,
and final position drift. Every move is relative and round-trip, so the
head ends where it started; the laser stays latched (motion-only backend).

Sequence: sanity jogs (X, Y, 40 mm out/back at 2400 mm/min), max-rate X
out/back (60 mm at F12000 - peaks ~200 mm/s mid-move), diagonal out/back,
then a G1 move with a feed-hold/resume in the middle.
"""
import os
import socket
import sys
import time

HOST = sys.argv[1] if len(sys.argv) > 1 else os.environ.get('GF_HOST')
if not HOST:
    raise SystemExit('pass the machine IP as argv[1] or set GF_HOST')
PORT = 23


class Grbl:
    def __init__(self, host, port):
        self.s = socket.create_connection((host, port), timeout=5)
        self.s.settimeout(0.2)
        self.buf = b''
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
        return out.decode('ascii', 'replace')

    def cmd(self, line, timeout=5.0):
        """Send a line, wait for ok/error."""
        self.s.sendall(line.encode() + b'\n')
        deadline = time.time() + timeout
        text = ''
        while time.time() < deadline:
            text += self.drain()
            if 'ok' in text or 'error' in text:
                return text.strip()
            time.sleep(0.02)
        return '(timeout) ' + text.strip()

    def status(self):
        """Realtime ? query -> raw <...> report or ''."""
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

    def wait_idle(self, timeout=30.0, poll=0.05):
        """Poll until Idle; return (peak_feed_mm_min, states_seen)."""
        peak = 0.0
        states = []
        deadline = time.time() + timeout
        while time.time() < deadline:
            st = self.status()
            if st:
                state = st[1:].split('|')[0]
                if not states or states[-1] != state:
                    states.append(state)
                for f in st[1:-1].split('|'):
                    # FS:feed,speed with a spindle registered; F:feed without
                    if f.startswith('FS:'):
                        peak = max(peak, float(f[3:].split(',')[0]))
                    elif f.startswith('F:'):
                        peak = max(peak, float(f[2:].split(',')[0]))
                if state.startswith('Idle'):
                    return peak, states
            time.sleep(poll)
        return peak, states + ['TIMEOUT']

    def rt(self, ch):
        self.s.sendall(ch)


def main():
    g = Grbl(HOST, PORT)
    st = g.status()
    print('connect: %s' % st)
    if 'Alarm' in st:
        print('unlock: %s' % g.cmd('$X'))

    for name, out, back, feed in (
            ('X sanity 40mm', '$J=G91X40F2400', '$J=G91X-40F2400', 2400),
            ('Y sanity 40mm', '$J=G91Y40F2400', '$J=G91Y-40F2400', 2400),
            ('X max-rate 60mm', '$J=G91X60F12000', '$J=G91X-60F12000', 12000),
            ('diag 40mm', '$J=G91X40Y40F8000', '$J=G91X-40Y-40F8000', 8000),
    ):
        for jog in (out, back):
            r = g.cmd(jog)
            if 'error' in r:
                print('%s: JOG REFUSED: %s' % (name, r))
                return 1
            peak, states = g.wait_idle()
            print('%s %s: peak %.0f mm/min, states %s'
                  % (name, 'out' if jog == out else 'back', peak, states))

    # Feed-hold mid-move: G1 at 600 mm/min takes 3 s for 30 mm.
    print('hold test: %s' % g.cmd('G91'))
    g.cmd('G1X30F600', timeout=0.5)      # ok arrives while moving
    time.sleep(1.0)
    g.rt(b'!')
    time.sleep(0.8)
    st = g.status()
    print('after !: %s' % st)
    held = 'Hold' in st
    g.rt(b'~')
    peak, states = g.wait_idle()
    print('after ~: states %s' % states)
    g.cmd('G1X-30F2400', timeout=0.5)    # return to start
    g.wait_idle()
    print('hold worked: %s' % held)

    print('final: %s' % g.status())
    return 0


if __name__ == '__main__':
    sys.exit(main())
