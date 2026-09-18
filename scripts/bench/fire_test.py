#!/usr/bin/env python3
# Copyright 2026 514 LLC d/b/a OpenGlow
# Written by Scott Wiederhold
# https://community.openglow.org
# SPDX-License-Identifier:    MIT
"""FIRE-line drop-timing test - runs ON the board. Usage: fire_test.py A|B|U

Stream: power(0) first byte (duty forced to ZERO before any FIRE bit -
the run-start reset would otherwise leave it at 100%), then
1 s pads / 2.000 s FIRE bits / 1 s pads / 2.000 s FIRE bits ending
exactly at end-of-data (the backstop edge the gate wants timed).
No step bytes; motor_lock=15.

Phase A: laser latch LOCKED - expects nothing on the FIRE/LASER_ON nets.
Phase B: latch UNLOCKED for the run (re-locked in finally), streaming=0
         (end-of-data = normal completion). Refuses to run if
         laser_pgood reports the HV supply good. Duty is zero throughout.
Phase U: like B but with streaming=1 declared, so the terminal
         end-of-data is a TRUE UNDERRUN: the kernel lands in the
         `underrun` fault state (expected - the script acks it via
         stop). Same SDMA backstop, exercised through the fault path;
         the scope measurement on the FIRE net is identical.
"""
import fcntl, os, struct, sys, time

TICK_HZ = 10000
FIRE = b'\x10'
PAD = b'\x00'


def wr(attr, val):
    with open('/sys/glowforge/' + attr, 'w') as f:
        f.write(str(val))


def rd(attr):
    with open('/sys/glowforge/' + attr) as f:
        return f.read().strip()


# The safety chain asserts HV_ENABLE only while a run feeds the charge-pump
# watchdog: a dead watchdog and an idle pulse engine mean HV off. That is
# the gate for a latch unlock; laser_pgood is the supply's power-good,
# high on every healthy machine, and says nothing about HV.
def hv_off_reason():
    alive = rd('cnc/charge_pump_alive')
    state = rd('cnc/state')
    if alive != '0' or state != 'idle':
        return 'charge_pump_alive=%s state=%s' % (alive, state)
    return None


def wait_hv_off(timeout_s=3.0):
    # A run feeds the charge-pump watchdog every 200 ms and the one-shot
    # holds ALIVE for 0.45 s after the last feed, so a phase that follows a
    # run finds the chain still up for under a second: wait for the release.
    t0 = time.time()
    why = hv_off_reason()
    while why is not None and time.time() - t0 < timeout_s:
        time.sleep(0.05)
        why = hv_off_reason()
    return why

def rd_pos():
    with open('/sys/glowforge/cnc/position', 'rb') as f:
        raw = f.read(32)
    return struct.unpack('<5i', raw[:20])


def snap(tag):
    print('%s: state=%s laser_enable=%s laser_on=%s laser_on_sampled=%s interlock=%s'
          % (tag, rd('cnc/state'), rd('cnc/laser_enable'), rd('cnc/laser_on'),
             rd('cnc/laser_on_sampled'), rd('cnc/interlock_circuit')))


mode = sys.argv[1].upper() if len(sys.argv) > 1 else 'A'
unlock = mode in ('B', 'U')
underrun_mode = mode == 'U'

stream = (
    bytes([0x80]) +                # power = 0: duty zero before any FIRE bit
    PAD * TICK_HZ +                # 1 s baseline
    FIRE * (2 * TICK_HZ) +         # 2.000 s FIRE window (bounded by pads)
    PAD * TICK_HZ +                # 1 s gap
    FIRE * (2 * TICK_HZ)           # 2.000 s FIRE window ending AT end-of-data
)

print('phase %s: stream %d bytes = %.3f s' % (mode, len(stream), len(stream) / TICK_HZ))

if unlock:
    why = wait_hv_off()
    if why is not None:
        print('ABORT: the safety chain is not holding HV off (%s) - refusing latch unlock' % why)
        sys.exit(1)

snap('pre ')
wr('cnc/motor_lock', 15)
wr('cnc/step_freq', TICK_HZ)
wr('cnc/laser_latch', 1)

fd = os.open('/dev/glowforge', os.O_WRONLY)
try:
    fcntl.flock(fd, fcntl.LOCK_EX)
    os.lseek(fd, 1, os.SEEK_SET)
    wr('cnc/enable', 1)
    time.sleep(0.5)
    os.write(fd, stream)
    pos_before = rd_pos()

    if underrun_mode:
        wr('cnc/streaming', 1)     # end-of-data mid-run = true underrun
        print('streaming=1: terminal end-of-data will be a TRUE UNDERRUN (expected)')

    if unlock:
        wr('cnc/laser_latch', 0)   # UNLOCK for this run only
        print('latch UNLOCKED for phase %s run' % mode)

    wr('cnc/run', 1)
    t0 = time.time()
    sampled_mid = False
    state = ''
    while time.time() - t0 < 20:
        state = rd('cnc/state')
        if not sampled_mid and 1.5 < time.time() - t0 < 3.0:
            snap('mid (inside FIRE window)')
            sampled_mid = True
        if state != 'running':
            break
        time.sleep(0.1)
    print('done: state=%s after %.1f s' % (state, time.time() - t0))
    if underrun_mode:
        if state == 'underrun':
            print('underrun state reached as EXPECTED; acking via stop')
        else:
            print('WARNING: expected underrun state, got %s' % state)
        wr('cnc/stop', 1)          # ack the underrun
        wr('cnc/streaming', 0)
        print('acked: state=%s' % rd('cnc/state'))
finally:
    wr('cnc/laser_latch', 1)       # re-lock unconditionally
    fcntl.flock(fd, fcntl.LOCK_UN)
    os.close(fd)

pos_after = rd_pos()
print('pos before=%s after=%s MOVED=%s' % (pos_before, pos_after, pos_before[:3] != pos_after[:3]))
snap('post')
print('underruns=%s faults=%s' % (rd('cnc/underruns'), rd('cnc/faults')))
wr('cnc/disable', 1)
print('safe state restored: state=%s latch=LOCKED' % rd('cnc/state'))
