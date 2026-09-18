#!/usr/bin/env python3
# Copyright 2026 514 LLC d/b/a OpenGlow
# Written by Scott Wiederhold
# https://community.openglow.org
# SPDX-License-Identifier:    MIT
"""LASER_PWM scope test - runs ON the board.

check: read-only - safety-chain readbacks + PWM2 register dump.
sweep: step the PWM duty register through known values with pauses so
       the scope can capture each, then restore the original sample.

The duty register (PWMSAR) is the laser power SETPOINT only. No motion
subsystem is touched (steppers are disabled before this runs), no
stream runs, the laser latch is locked, FIRE is never asserted.

Locked state only: run with the controller and forgectrl stopped (the
bench page's takeover), the pulse device closed. The latch is relocked
here as well, and the sweep refuses to write if the FIRE line reads
driven or LASER_ON reads active. Usage: pwm_sweep.py [check|sweep]
"""
import mmap, struct, sys, time

PWM2_BASE = 0x02084000
PERCLK_HZ = 66_000_000
SAR_OFF = 0x0C


def rd(name):
    try:
        with open('/sys/glowforge/' + name) as f:
            return f.read().strip()
    except OSError as e:
        return '<%s>' % e


def dump_regs(m):
    cr, sr, ir, sar, pr = struct.unpack('<5I', m[:20])
    divider = ((cr >> 4) & 0xFFF) + 1
    counts = pr + 2
    freq = PERCLK_HZ / (divider * counts) if counts else 0
    print('PWMCR=0x%08x PWMSAR=%d PWMPR=%d enabled=%s divider=%d counts=%d carrier=%.2f kHz'
          % (cr, sar, pr, bool(cr & 1), divider, counts, freq / 1000))
    return sar, pr


def wr(name, val):
    with open('/sys/glowforge/' + name, 'w') as f:
        f.write(str(val))


def locked_state():
    """The latch commanded locked, FIRE not driven, no emission."""
    try:
        wr('cnc/laser_latch', 1)
    except OSError as e:
        print('!! could not lock the laser latch: %s' % e)
        return False
    time.sleep(0.2)
    en, on = rd('cnc/laser_enable'), rd('cnc/laser_on')
    if en != '0' or on != '0':
        print('!! not the locked state: laser_enable=%s laser_on=%s' % (en, on))
        return False
    return True


def safety_readback():
    print('cnc/state          =', rd('cnc/state'))
    print('cnc/laser_on       =', rd('cnc/laser_on'))
    print('cnc/laser_enable   =', rd('cnc/laser_enable'))
    print('cnc/laser_pgood    =', rd('cnc/laser_pgood'))
    print('cnc/interlock_circuit =', rd('cnc/interlock_circuit'),
          '(b0 LASER_ON b1 LASER_ENABLE b2 BUTTON_LATCH b3 LASER_LATCH b4 ILK_RESET)')
    print('cnc/laser_on_sampled  =', rd('cnc/laser_on_sampled'))


mode = sys.argv[1] if len(sys.argv) > 1 else 'check'

with open('/dev/mem', 'r+b') as f:
    m = mmap.mmap(f.fileno(), 4096, mmap.MAP_SHARED,
                  mmap.PROT_READ | mmap.PROT_WRITE, offset=PWM2_BASE)

    print('--- safety readback (before)')
    safety_readback()
    print('--- PWM2 registers')
    sar0, pr = dump_regs(m)

    if mode == 'sweep':
        if not locked_state():
            m.close()
            sys.exit(2)
        period = pr + 2
        steps = [(64, '50%'), (32, '25%'), (96, '75%'), (8, '6%'), (127, '100%')]
        print('--- duty sweep: 4 s per step, watch the scope')
        for sar, label in steps:
            m[SAR_OFF:SAR_OFF + 4] = struct.pack('<I', sar)
            time.sleep(0.1)
            cur = struct.unpack('<I', m[SAR_OFF:SAR_OFF + 4])[0]
            print('  PWMSAR=%-3d (%s of %d counts)  readback=%d' % (sar, label, period, cur))
            time.sleep(4)
        m[SAR_OFF:SAR_OFF + 4] = struct.pack('<I', sar0)
        print('--- restored PWMSAR=%d' % sar0)
        print('--- safety readback (after)')
        safety_readback()
        dump_regs(m)

    m.close()
