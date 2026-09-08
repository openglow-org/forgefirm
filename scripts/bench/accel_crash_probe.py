#!/usr/bin/env python3
"""De-risk drill for the head-accelerometer crash detector.

The LIS2HH12 on the head bus (i2c-3 @0x1e) carries an on-chip interrupt
generator: a per-axis high-event threshold (IG_THS_X1/Y1/Z1), a duration
counter (IG_DUR1), an axis enable/AND-OR word (IG_CFG1) and a latched
per-axis source register (IG_SRC1). The factory arms this generator per
job and reads trips from IG_SRC1; ForgeFIRM does not. This drill arms it
on the bench and answers the three questions the crash-detector design
rides on, before any kernel or forgectrl work:

  1. Does IG_SRC1 latch a real head strike at a plausible threshold, and
     which axes does it report?
  2. Can the IG registers be programmed and polled over i2c-dev while
     st_accel stays bound for the motion-liveness reads (coexist mode,
     I2C_SLAVE_FORCE)? If so the detector is forgectrl-only, no kernel
     change and the liveness path untouched. If the two collide, the
     accel must move under glowforge.ko instead.
  3. What raw magnitude does a strike produce, so the shipped threshold
     can be set in register units against the rail-contact signature
     (facts bank: 20-40x over creep within ~4 ms on a fast strike).

The drill touches the IG registers plus the IG-latch bit of CTRL7,
which st_accel never writes, and CTRL1: st_accel powers the part down
between one-shot reads (ODR=0), and the IG generator only samples at a
running ODR, so the drill saves CTRL1, sets 800 Hz for the window and
restores the saved value on exit. The full scale (CTRL4) is never
changed, so st_accel's raw scaling is undisturbed. Default is coexist
mode with forgectrl left running: no emission, no commanded motion. Provoke a trip by hand (a firm tap or nudge on the head) or pass
--jog to send one gentle grblHAL jog; a jog needs the controller free,
so run it from the bench page's takeover or stop the controller first.

Usage:
  accel_crash_probe.py [seconds] [--mode coexist|unbind] [--ths N]
                       [--dur N] [--axes xyz] [--jog GCODE] [--rate HZ]

  seconds   arm-and-watch window (default 20)
  --mode    coexist (default): leave st_accel bound, reach the IG
            registers with I2C_SLAVE_FORCE, forgectrl stays up; unbind:
            unbind st_accel for the run and rebind after (clean access,
            but liveness is down meanwhile, so stop the controller and
            forgectrl first)
  --ths N   per-axis threshold register value 0..255 (default 40); the
            LSB is full scale / 256 (bench-confirmed against gravity:
            +/-2 g gives ~7.8 mg/LSB, 1 g ~= 128), so the printed CTRL4
            FS fixes the g conversion
  --dur N   IG_DUR1 duration counter, samples at the running ODR
            (default 0 = fire on the first over-threshold sample)
  --axes    which axes arm high events (default xyz)
  --jog     one grblHAL jog at t=2 s, e.g. "$J=G91 X5 F1000" (+X first,
            never -X: a cable lives at the end of LEFT travel)
  --rate    IG_SRC1 poll rate in Hz (default 200)

CSV of the poll trace to /tmp/accel_crash.csv: t,ig_src,xh,yh,zh,x,y,z
Exit 0 on a clean run whether or not a trip was seen; the summary states
what happened. Reads/writes only; relocks nothing (no laser path).
"""
import fcntl
import os
import struct
import sys
import time

I2C_SLAVE = 0x0703
I2C_SLAVE_FORCE = 0x0706
BUS = '/dev/i2c-3'
ADDR = 0x1e                      # head accel; 0x1d is the board accel

WHO_AM_I = 0x0F
WHO_AM_I_LIS2HH12 = 0x41
CTRL1 = 0x20
CTRL4 = 0x23                     # bits 5:4 = FS (00=+/-2g, 10=+/-4g, 11=+/-8g)
CTRL7 = 0x26                     # bit 2 = LIR1 (latch IG_SRC1, read-to-clear;
                                 # bit 3 = LIR2, bits 1:0 are the 4D enables)
OUT_X_L = 0x28
IG_CFG1 = 0x30                   # AOI,6D,ZHIE,ZLIE,YHIE,YLIE,XHIE,XLIE
IG_SRC1 = 0x31                   # IA(6),ZH,ZL,YH,YL,XH,XL
IG_THS_X1 = 0x32
IG_THS_Y1 = 0x33
IG_THS_Z1 = 0x34
IG_DUR1 = 0x35

# IG_CFG1 high-event enables and the matching IG_SRC1 source bits share
# the same odd bit positions: X high = 1, Y high = 3, Z high = 5 (the even
# positions 0/2/4 are the low-event bits, which this drill does not arm).
XHIE = 1 << 1
YHIE = 1 << 3
ZHIE = 1 << 5
IG_IA = 1 << 6
IG_XH = 1 << 1
IG_YH = 1 << 3
IG_ZH = 1 << 5

DRIVER = '/sys/bus/i2c/drivers/st-accel-i2c'
DEV = '3-%04x' % ADDR
FS_G = {0b00: 2, 0b10: 4, 0b11: 8}


def bind_ctl(op):
    try:
        with open(DRIVER + '/' + op, 'w') as f:
            f.write(DEV)
    except OSError as e:
        print('%s %s: %s' % (op, DEV, e))


def rd(fd, reg, n=1):
    os.write(fd, bytes([reg]))
    d = os.read(fd, n)
    return d[0] if n == 1 else d


def wr(fd, reg, val):
    os.write(fd, bytes([reg, val & 0xff]))


VALUE_OPTS = ('--mode', '--ths', '--dur', '--axes', '--jog', '--rate')


def parse(argv):
    """Return (positionals, {opt: value}). Value options consume the next
    token."""
    pos, opts = [], {}
    i = 0
    while i < len(argv):
        a = argv[i]
        if a in VALUE_OPTS:
            opts[a] = argv[i + 1] if i + 1 < len(argv) else None
            i += 2
        else:
            pos.append(a)
            i += 1
    return pos, opts


def main():
    pos, opts = parse(sys.argv[1:])
    dur_s = float(pos[0]) if pos else 20.0
    ths = int(opts.get('--ths', 40))
    ig_dur = int(opts.get('--dur', 0))
    axes = opts.get('--axes', 'xyz')
    rate = float(opts.get('--rate', 200))
    jog = opts.get('--jog')
    unbind = opts.get('--mode', 'coexist') == 'unbind'

    cfg = 0
    if 'x' in axes:
        cfg |= XHIE
    if 'y' in axes:
        cfg |= YHIE
    if 'z' in axes:
        cfg |= ZHIE

    if unbind:
        bind_ctl('unbind')

    fd = os.open(BUS, os.O_RDWR)
    # Coexist mode leaves st_accel bound, so FORCE the address; unbind
    # mode owns it outright.
    fcntl.ioctl(fd, I2C_SLAVE if unbind else I2C_SLAVE_FORCE, ADDR)

    who = rd(fd, WHO_AM_I)
    if who != WHO_AM_I_LIS2HH12:
        print('WHO_AM_I=0x%02x, expected 0x41 (LIS2HH12) at %s' % (who, DEV))
        os.close(fd)
        if unbind:
            bind_ctl('bind')
        return 1

    orig_ctrl1 = rd(fd, CTRL1)
    ctrl1 = orig_ctrl1
    ctrl4 = rd(fd, CTRL4)
    fs = FS_G.get((ctrl4 >> 4) & 0b11, '?')
    if (ctrl1 & 0x70) == 0:      # ODR bits 6:4 zero = power-down; IG needs
        wr(fd, CTRL1, 0x6F)      # a running ODR: 800 Hz, BDU, XYZ on
        ctrl1 = rd(fd, CTRL1)
    lsb_mg = (fs * 1000.0 / 256.0) if isinstance(fs, int) else 0
    print('WHO_AM_I=0x%02x CTRL1=0x%02x CTRL4=0x%02x  FS=+/-%sg  '
          'ths=%d (~%.0f mg, ~%.2f g)  dur=%d  axes=%s  mode=%s'
          % (who, ctrl1, ctrl4, fs, ths, ths * lsb_mg, ths * lsb_mg / 1000.0,
             ig_dur, axes, 'unbind' if unbind else 'coexist(FORCE)'))

    # Arm IG1: latch the source (CTRL7 LIR1), set the thresholds and
    # duration, then enable the axes last. Preserve CTRL7's other bits.
    ctrl7 = rd(fd, CTRL7)
    wr(fd, CTRL7, ctrl7 | 0x04)
    wr(fd, IG_THS_X1, ths)
    wr(fd, IG_THS_Y1, ths)
    wr(fd, IG_THS_Z1, ths)
    wr(fd, IG_DUR1, ig_dur & 0x7f)
    wr(fd, IG_CFG1, cfg)
    rd(fd, IG_SRC1)              # clear any stale latch

    sock = None
    if jog:
        import socket
        sock = socket.create_connection(('127.0.0.1', 23), timeout=3)
        sock.settimeout(0.1)

    out = open('/tmp/accel_crash.csv', 'w')
    out.write('t,ig_src,xh,yh,zh,x,y,z\n')
    period = 1.0 / rate
    t0 = time.monotonic()
    jogged = False
    trips = []
    live_ok = True
    n = 0
    while True:
        t = time.monotonic() - t0
        if t > dur_s:
            break
        if sock and not jogged and t > 2.0:
            sock.sendall((jog + '\n').encode())
            jogged = True
        src = rd(fd, IG_SRC1)
        n += 1
        if src & IG_IA:
            raw = rd(fd, OUT_X_L, 6)
            x, y, z = struct.unpack('<hhh', raw)
            xh = 1 if src & IG_XH else 0
            yh = 1 if src & IG_YH else 0
            zh = 1 if src & IG_ZH else 0
            out.write('%.5f,0x%02x,%d,%d,%d,%d,%d,%d\n'
                      % (t, src, xh, yh, zh, x, y, z))
            trips.append((t, xh, yh, zh, x, y, z))
        time.sleep(period)
    out.close()

    # Coexist proof: st_accel's raw reads still work while we polled.
    if not unbind:
        try:
            base = ('/sys/bus/i2c/devices/%s/iio:device' % DEV)
            import glob
            hits = glob.glob(base + '*/in_accel_x_raw')
            if hits:
                with open(hits[0]) as f:
                    _ = int(f.read())
            else:
                live_ok = False
        except (OSError, ValueError):
            live_ok = False

    # Disarm and restore.
    wr(fd, IG_CFG1, 0x00)
    wr(fd, CTRL7, ctrl7)
    if ctrl1 != orig_ctrl1:
        wr(fd, CTRL1, orig_ctrl1)
    if sock:
        try:
            sock.recv(4096)
        except OSError:
            pass
        sock.close()
    os.close(fd)
    if unbind:
        bind_ctl('bind')

    print('polled %d samples at ~%.0f Hz over %.0f s' % (n, n / dur_s, dur_s))
    if trips:
        ax = ''.join(a for a, f in zip('xyz', (
            any(t[1] for t in trips),
            any(t[2] for t in trips),
            any(t[3] for t in trips))) if f)
        first = trips[0]
        print('TRIPPED %d times, axes seen: %s' % (len(trips), ax or '-'))
        print('  first at t=%.3f s  raw x=%d y=%d z=%d (FS +/-%sg, 1 g ~= %d)'
              % (first[0], first[4], first[5], first[6], fs,
                 int(32768 / fs) if isinstance(fs, int) else 0))
    else:
        print('no trip in the window (raise the strike or lower --ths)')
    if not unbind:
        print('coexist liveness read after the run: %s'
              % ('OK (st_accel still serving raw)' if live_ok else 'FAILED'))
    return 0


if __name__ == '__main__':
    sys.exit(main())
