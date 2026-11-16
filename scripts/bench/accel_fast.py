#!/usr/bin/env python3
# Copyright 2026 514 LLC d/b/a OpenGlow
# Written by Scott Wiederhold
# https://community.openglow.org
# SPDX-License-Identifier:    MIT
"""Fast direct-I2C sampler for the two LIS2HH12s on the head bus (i2c-3).

The capture unbinds st_accel from both parts. With forgectrl up that would
leave its liveness probe no head accelerometer and its crash watch blind,
so the sampler runs with forgectrl, the controller and the cloud client
stopped (the bench page's takeover) and refuses while any of them, or
anything holding the pulse device, runs. Nothing here commands motion.
st_accel is bound again on every exit (an exception, the page's Abort and
a lost terminal included), each rebind is checked, and one that did not
take is said loudly and exits 1. Programs 800 Hz ODR and polls
OUT_X..OUT_Z as fast as the bus allows.

Usage: accel_fast.py <seconds>
CSV to /tmp/accel.csv: t,addr,x,y,z
"""
import fcntl
import os
import struct
import sys
import time

from gfbench import arming_processes, exit_on_signal, put_back

I2C_SLAVE = 0x0703
BUS = '/dev/i2c-3'
ADDRS = [0x1e, 0x1d]
WHO_AM_I = 0x0F
CTRL1 = 0x20
OUT_X_L = 0x28

DRIVER = '/sys/bus/i2c/drivers/st-accel-i2c'
DEVICES = '/sys/bus/i2c/devices'
CSV = '/tmp/accel.csv'


def bind_ctl(op, dev):
    try:
        with open(DRIVER + '/' + op, 'w') as f:
            f.write(dev)
    except OSError:
        pass


def rebind(dev):
    """st_accel bound to dev again, checked by the device's driver link."""
    bind_ctl('bind', dev)
    if not os.path.exists(os.path.join(DEVICES, dev, 'driver')):
        raise RuntimeError('st_accel is NOT bound to %s: bind it (echo %s > %s/bind), then '
                           'restart the controller (a mode switch on the panel) or forgectrl, '
                           'or reboot: until then a controller start skips the motion check'
                           % (dev, dev, DRIVER))


def main():
    exit_on_signal()
    dur = float(sys.argv[1]) if len(sys.argv) > 1 else 5.0
    owners = arming_processes()
    if owners:
        print('REFUSED: %s running; the sampler unbinds the head accelerometer, so it needs '
              'forgectrl, the controller and the cloud client stopped (the bench page runs it '
              'under its takeover)' % ', '.join(owners))
        return 2

    unbound = []
    fd = out = None
    n = 0
    failed = []
    try:
        for a in ADDRS:
            unbound.append('3-%04x' % a)
            bind_ctl('unbind', '3-%04x' % a)

        fd = os.open(BUS, os.O_RDWR)
        for a in ADDRS:
            fcntl.ioctl(fd, I2C_SLAVE, a)
            os.write(fd, bytes([WHO_AM_I]))
            who = os.read(fd, 1)[0]
            os.write(fd, bytes([CTRL1, 0x6F]))   # 800 Hz, BDU, XYZ on
            print('addr 0x%02x WHO_AM_I=0x%02x' % (a, who))
        time.sleep(0.05)

        out = open(CSV, 'w')
        t0 = time.monotonic()
        while True:
            t = time.monotonic() - t0
            if t > dur:
                break
            for a in ADDRS:
                fcntl.ioctl(fd, I2C_SLAVE, a)
                os.write(fd, bytes([OUT_X_L]))
                d = os.read(fd, 6)
                x, y, z = struct.unpack('<hhh', d)
                out.write('%.5f,%02x,%d,%d,%d\n' % (t, a, x, y, z))
                n += 1
    finally:
        failed = put_back(('close the capture file', lambda: out is not None and out.close()),
                          ('close the bus', lambda: fd is not None and os.close(fd)),
                          *[('bind st_accel to %s' % dev, lambda dev=dev: rebind(dev)) for dev in unbound])
    print('samples:', n, 'rate: %.0f Hz per device' % (n / 2 / dur))
    return 1 if failed else 0


if __name__ == '__main__':
    sys.exit(main())
