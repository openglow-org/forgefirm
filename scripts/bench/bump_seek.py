#!/usr/bin/env python3
# Copyright 2026 514 LLC d/b/a OpenGlow
# Written by Scott Wiederhold
# https://community.openglow.org
# SPDX-License-Identifier:    MIT
"""Accelerometer bump-seek prototype: creep toward a rail in bounded jog
segments, detect the contact jolt on the head LIS2HH12, jog-cancel at once,
back off. This is the homing-cycle detection loop, run standalone.

Usage: bump_seek.py [dir(+|-)] [feed] [seg_mm] [max_mm]
Logs every sample to /tmp/bump.csv: t,x,y,z,dev,state
"""
import fcntl
import os
import socket
import struct
import sys
import time

I2C_SLAVE = 0x0703
BUS = '/dev/i2c-3'
HEAD = 0x1e
CTRL1 = 0x20
OUT_X_L = 0x28
DRIVER = '/sys/bus/i2c/drivers/st-accel-i2c'

BASELINE_S = 1.2     # of each segment: learn moving-noise floor
EMA_A = 0.05         # gravity/slope tracker
K_SIGMA = 8.0        # detection threshold multiplier
MIN_THRESH = 800.0   # absolute floor (counts, ~0.5 g summed dev)
CONFIRM = 2          # consecutive samples over threshold


def unbind():
    try:
        with open(DRIVER + '/unbind', 'w') as f:
            f.write('3-%04x' % HEAD)
    except OSError:
        pass


def rebind():
    try:
        with open(DRIVER + '/bind', 'w') as f:
            f.write('3-%04x' % HEAD)
    except OSError:
        pass


class Accel:
    def __init__(self):
        self.fd = os.open(BUS, os.O_RDWR)
        fcntl.ioctl(self.fd, I2C_SLAVE, HEAD)
        os.write(self.fd, bytes([CTRL1, 0x6F]))
        time.sleep(0.05)
        self.ema = None

    def read(self):
        os.write(self.fd, bytes([OUT_X_L]))
        x, y, z = struct.unpack('<hhh', os.read(self.fd, 6))
        if self.ema is None:
            self.ema = [float(x), float(y), float(z)]
        dev = 0.0
        for i, v in enumerate((x, y, z)):
            dev += abs(v - self.ema[i])
            self.ema[i] += EMA_A * (v - self.ema[i])
        return x, y, z, dev


class Grbl:
    def __init__(self):
        self.s = socket.create_connection(('127.0.0.1', 23), timeout=3)
        self.s.settimeout(0.05)
        self.drain()

    def drain(self):
        buf = b''
        try:
            while True:
                d = self.s.recv(4096)
                if not d:
                    break
                buf += d
        except OSError:
            pass
        return buf.decode(errors='replace')

    def send(self, line):
        self.s.sendall((line + '\n').encode())

    def cancel(self):
        self.s.sendall(b'\x85')

    def status(self):
        self.drain()
        self.s.sendall(b'?')
        time.sleep(0.05)
        return self.drain()

    def wait_idle(self, timeout=30):
        t0 = time.monotonic()
        while time.monotonic() - t0 < timeout:
            st = self.status()
            if '<Idle' in st:
                return st
            time.sleep(0.2)
        return None


def mpos(st):
    try:
        p = st.split('MPos:')[1].split('|')[0]
        return [float(v) for v in p.split(',')]
    except (IndexError, ValueError):
        return None


def main():
    dirn = sys.argv[1] if len(sys.argv) > 1 else '-'
    feed = int(sys.argv[2]) if len(sys.argv) > 2 else 120
    seg = float(sys.argv[3]) if len(sys.argv) > 3 else 15.0
    maxmm = float(sys.argv[4]) if len(sys.argv) > 4 else 200.0

    unbind()
    acc = Accel()
    g = Grbl()
    log = open('/tmp/bump.csv', 'w')

    st = g.wait_idle(5)
    if st is None:
        print('controller not idle, aborting')
        return 1
    start = mpos(st)
    print('start MPos:', start)

    t0 = time.monotonic()
    traveled = 0.0
    hit = False
    try:
        while traveled < maxmm and not hit:
            g.send('$J=G91X%s%.3fF%d' % (dirn, seg, feed))
            seg_t0 = time.monotonic()
            exp_dur = seg / feed * 60.0
            last_poll = 0.0
            over = 0
            base_devs = []
            thresh = None
            # segment loop: sample until idle (segment done) or hit
            while True:
                t = time.monotonic() - t0
                x, y, z, dev = acc.read()
                state = 'base'
                seg_t = time.monotonic() - seg_t0
                if seg_t < 0.3:
                    state = 'ramp'      # ignore accel/decel transients
                elif seg_t < 0.3 + BASELINE_S:
                    base_devs.append(dev)
                    state = 'learn'
                else:
                    if thresh is None:
                        m = sum(base_devs) / len(base_devs)
                        sd = (sum((v - m) ** 2 for v in base_devs)
                              / len(base_devs)) ** 0.5
                        thresh = max(m + K_SIGMA * sd, MIN_THRESH)
                        print('segment thresh: %.0f (base mean %.0f sd %.0f)'
                              % (thresh, m, sd))
                    if dev > thresh:
                        over += 1
                        state = 'OVER'
                        if over >= CONFIRM:
                            g.cancel()
                            hit = True
                            state = 'HIT'
                    else:
                        over = 0
                log.write('%.5f,%d,%d,%d,%.0f,%s\n'
                          % (t, x, y, z, dev, state))
                if hit:
                    break
                # poll for segment completion only near its expected end,
                # so status round-trips never pause active detection
                if seg_t > exp_dur - 0.3 and seg_t - last_poll > 0.3:
                    last_poll = seg_t
                    stq = g.status()
                    if '<Idle' in stq:
                        break
            traveled += seg
            print('segment done, traveled<=%.0f mm, hit=%s' % (traveled, hit))
    finally:
        g.cancel()
        time.sleep(0.3)
        st = g.wait_idle(10)
        pos = mpos(st) if st else None
        print('stopped at MPos:', pos)
        if hit and pos:
            print('backing off 3 mm')
            g.send('$J=G91X%sF600' % ('3' if dirn == '-' else '-3'))
            g.wait_idle(10)
        log.close()
        rebind()
    print('HIT' if hit else 'NO CONTACT within %.0f mm' % maxmm)
    return 0


if __name__ == '__main__':
    sys.exit(main())
