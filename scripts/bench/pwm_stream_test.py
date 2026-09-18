#!/usr/bin/env python3
# Copyright 2026 514 LLC d/b/a OpenGlow
# Written by Scott Wiederhold
# https://community.openglow.org
# SPDX-License-Identifier:    MIT
"""LASER_PWM stream-path test - runs ON the board.

Streams POWER BYTES ONLY (bit 7 set) through /dev/glowforge and lets the
kernel pulse engine play them, so the scope on LASER_PWM verifies the
real power path the laser milestone will use, including two contract
rules (run-start duty reset; consecutive power bytes dropped).

Safety posture:
- The stream contains ZERO step bytes and ZERO FIRE (bit 4) bits.
- motor_lock=15 masks step output on all axes regardless.
- laser_latch re-asserted locked; lid closed; HV watchdog untouched.
- Position counters compared before/after - must be identical.
- streaming stays 0: end-of-data is a normal completion.

Runs with the controller and forgectrl stopped (the bench page's
takeover). Exit status 0 when the counters did not move, the run ended
in an idle state and no FIRE/emission was read back; 1 otherwise.
"""
import fcntl, mmap, os, struct, sys, time

TICK_HZ = 10000
PWM2_BASE = 0x02084000


def wr(attr, val):
    with open('/sys/glowforge/' + attr, 'w') as f:
        f.write(str(val))


def rd(attr):
    with open('/sys/glowforge/' + attr) as f:
        return f.read().strip()


def rd_pos():
    with open('/sys/glowforge/cnc/position', 'rb') as f:
        raw = f.read(32)
    x, y, z, done, total = struct.unpack('<5i', raw[:20])
    return x, y, z, done, total


def pwmsar():
    with open('/dev/mem', 'rb') as f:
        m = mmap.mmap(f.fileno(), 4096, mmap.MAP_SHARED, mmap.PROT_READ,
                      offset=PWM2_BASE)
        sar = struct.unpack('<I', m[0x0C:0x10])[0]
        m.close()
    return sar


def power(v):
    return bytes([0x80 | (v & 0x7F)])


PAD = b'\x00'

stream = (
    PAD * TICK_HZ +                 # 1 s: shows the run-start 100% reset
    power(64) + PAD * (2 * TICK_HZ) +               # 50%
    power(32) + power(96) + PAD * (2 * TICK_HZ) +   # 25%; the 96 must be DROPPED
    power(96) + PAD * (2 * TICK_HZ) +               # 75% (applies after pads)
    power(8)  + PAD * (2 * TICK_HZ) +               # ~6%
    power(127) + PAD * (2 * TICK_HZ)                # 100%
)

print('stream: %d bytes = %.1f s at %d Hz' % (len(stream), len(stream) / TICK_HZ, TICK_HZ))
print('pre:  state=%s pos=%s laser_enable=%s PWMSAR=%d'
      % (rd('cnc/state'), rd_pos(), rd('cnc/laser_enable'), pwmsar()))

wr('cnc/laser_latch', 1)
wr('cnc/motor_lock', 15)
wr('cnc/step_freq', TICK_HZ)

fd = os.open('/dev/glowforge', os.O_WRONLY)
try:
    fcntl.flock(fd, fcntl.LOCK_EX)
    os.lseek(fd, 1, os.SEEK_SET)       # clear pulse data + byte counters
    wr('cnc/enable', 1)
    time.sleep(0.5)
    os.write(fd, stream)               # preload the whole program
    pos_before = rd_pos()
    print('run: state=%s, starting playback...' % rd('cnc/state'))
    wr('cnc/run', 1)
    t0 = time.time()
    state = ''
    while time.time() - t0 < 30:
        state = rd('cnc/state')
        if state != 'running':
            break
        time.sleep(0.2)
    dt = time.time() - t0
    print('done: state=%s after %.1f s' % (state, dt))
    pos_after = rd_pos()
    moved = pos_before[:3] != pos_after[:3]
    print('post: pos before=%s after=%s  MOVED=%s' % (pos_before, pos_after, moved))
    en, on, sampled = rd('cnc/laser_enable'), rd('cnc/laser_on'), rd('cnc/laser_on_sampled')
    print('post: laser_enable=%s laser_on=%s laser_on_sampled=%s faults=%s underruns=%s'
          % (en, on, sampled, rd('cnc/faults'), rd('cnc/underruns')))
    print('post: PWMSAR=%d  (duty after end-of-data)' % pwmsar())
    ok = (not moved) and state == 'idle' and en == '0' and on == '0' and sampled == '0'
finally:
    fcntl.flock(fd, fcntl.LOCK_UN)
    os.close(fd)

wr('cnc/disable', 1)
print('safe state restored: state=%s' % rd('cnc/state'))
print('RESULT %s' % ('PASS' if ok else 'FAIL'))
sys.exit(0 if ok else 1)
