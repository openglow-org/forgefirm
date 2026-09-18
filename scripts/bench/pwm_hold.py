#!/usr/bin/env python3
# Copyright 2026 514 LLC d/b/a OpenGlow
# Written by Scott Wiederhold
# https://community.openglow.org
# SPDX-License-Identifier:    MIT
"""Hold LASER_PWM at one duty value for a measurement window, then restore.
Usage: pwm_hold.py [sar] [seconds]  (defaults: 8, 30)

The duty register (PWMSAR) is the laser power SETPOINT only. Locked
state only: run with the controller and forgectrl stopped (the bench
page's takeover), the pulse device closed. The latch is relocked here
as well, and the hold refuses to write if the FIRE line reads driven or
LASER_ON reads active."""
import mmap, struct, sys, time

PWM2_BASE = 0x02084000
SAR_OFF = 0x0C

sar = int(sys.argv[1]) if len(sys.argv) > 1 else 8
secs = int(sys.argv[2]) if len(sys.argv) > 2 else 30


def rd(name):
    with open('/sys/glowforge/' + name) as f:
        return f.read().strip()


def wr(name, val):
    with open('/sys/glowforge/' + name, 'w') as f:
        f.write(str(val))


try:
    wr('cnc/laser_latch', 1)
except OSError as e:
    raise SystemExit('could not lock the laser latch: %s' % e)
time.sleep(0.2)
en, on = rd('cnc/laser_enable'), rd('cnc/laser_on')
if en != '0' or on != '0':
    raise SystemExit('not the locked state: laser_enable=%s laser_on=%s' % (en, on))

with open('/dev/mem', 'r+b') as f:
    m = mmap.mmap(f.fileno(), 4096, mmap.MAP_SHARED,
                  mmap.PROT_READ | mmap.PROT_WRITE, offset=PWM2_BASE)
    sar0 = struct.unpack('<I', m[SAR_OFF:SAR_OFF + 4])[0]
    m[SAR_OFF:SAR_OFF + 4] = struct.pack('<I', sar)
    print('PWMSAR=%d held for %d s (was %d)...' % (sar, secs, sar0))
    sys.stdout.flush()
    try:
        time.sleep(secs)
    finally:
        m[SAR_OFF:SAR_OFF + 4] = struct.pack('<I', sar0)
        print('restored PWMSAR=%d' % sar0)
        print('laser_enable=%s laser_on=%s laser_on_sampled=%s'
              % (rd('cnc/laser_enable'), rd('cnc/laser_on'), rd('cnc/laser_on_sampled')))
        m.close()
