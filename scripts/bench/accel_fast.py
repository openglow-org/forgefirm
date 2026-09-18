#!/usr/bin/env python3
# Copyright 2026 514 LLC d/b/a OpenGlow
# Written by Scott Wiederhold
# https://community.openglow.org
# SPDX-License-Identifier:    MIT
"""Fast direct-I2C sampler for the two LIS2HH12s on the head bus (i2c-3).

Unbinds st_accel from both for the capture (rebinds after), programs
800 Hz ODR, and polls OUT_X..OUT_Z as fast as the bus allows.

Usage: accel_fast.py <seconds> [jog-gcode [jog2-gcode]]
Jogs are sent to the local grblHAL at t=2 s (and t=2+4 s for jog2).
CSV to /tmp/accel.csv: t,addr,x,y,z
"""
import fcntl
import os
import socket
import struct
import sys
import time

I2C_SLAVE = 0x0703
BUS = '/dev/i2c-3'
ADDRS = [0x1e, 0x1d]
WHO_AM_I = 0x0F
CTRL1 = 0x20
OUT_X_L = 0x28

DRIVER = '/sys/bus/i2c/drivers/st-accel-i2c'


def bind_ctl(op, dev):
    try:
        with open(DRIVER + '/' + op, 'w') as f:
            f.write(dev)
    except OSError:
        pass


def main():
    dur = float(sys.argv[1]) if len(sys.argv) > 1 else 5.0
    jogs = sys.argv[2:4]

    for a in ADDRS:
        bind_ctl('unbind', '3-%04x' % a)

    fd = os.open(BUS, os.O_RDWR)
    for a in ADDRS:
        fcntl.ioctl(fd, I2C_SLAVE, a)
        os.write(fd, bytes([WHO_AM_I]))
        who = os.read(fd, 1)[0]
        os.write(fd, bytes([CTRL1, 0x6F]))   # 800 Hz, BDU, XYZ on
        print('addr 0x%02x WHO_AM_I=0x%02x' % (a, who))
    time.sleep(0.05)

    sock = None
    if jogs:
        sock = socket.create_connection(('127.0.0.1', 23), timeout=3)
        sock.settimeout(0.1)

    out = open('/tmp/accel.csv', 'w')
    t0 = time.monotonic()
    sent = 0
    n = 0
    while True:
        t = time.monotonic() - t0
        if t > dur:
            break
        if sock and sent < len(jogs) and t > 2.0 + 4.0 * sent:
            sock.sendall((jogs[sent] + '\n').encode())
            sent += 1
        for a in ADDRS:
            fcntl.ioctl(fd, I2C_SLAVE, a)
            os.write(fd, bytes([OUT_X_L]))
            d = os.read(fd, 6)
            x, y, z = struct.unpack('<hhh', d)
            out.write('%.5f,%02x,%d,%d,%d\n' % (t, a, x, y, z))
            n += 1
    out.close()
    if sock:
        try:
            sock.recv(4096)
        except OSError:
            pass
        sock.close()
    os.close(fd)

    for a in ADDRS:
        bind_ctl('bind', '3-%04x' % a)
    print('samples:', n, 'rate: %.0f Hz per device' % (n / 2 / dur))


if __name__ == '__main__':
    main()
