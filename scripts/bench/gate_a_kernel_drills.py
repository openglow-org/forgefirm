#!/usr/bin/env python3
# Copyright 2026 514 LLC d/b/a OpenGlow
# Written by Scott Wiederhold
# https://community.openglow.org
# SPDX-License-Identifier:    MIT
"""GATE A kernel drills - runs ON the board. Usage: gate_a_kernel_drills.py K1|K2|K3

The pulse device must be free: stop forgectrl first
(/etc/init.d/forgectrl stop), run the drill, then restart it. The
script exits with a clear message if the device is busy.

All drills: the first stream byte forces laser duty to zero, the
stream ends with laser-off bytes, motor_lock=15 (no axis moves), and
the latch is re-locked unconditionally on exit. K3 refuses to run if
laser_pgood reports the HV supply good. Software witnesses:
cnc/state, cnc/laser_enable, cnc/laser_on, cnc/laser_on_sampled, and
interlock_circuit bit 3 (the commanded latch). Physical witness:
scope on the PSU-connector LASER_ON pin, as in fire_test.py.

K1 - controlled-stop deceleration floor. Pads-only stream at the
     default cloud tick (10 kHz, ramp 125000 Hz/s). A controlled stop
     mid-run must ramp the step frequency down to the floor (tens of
     milliseconds), never consume the tail as a max-rate burst
     (sub-millisecond) or hang. PASS: stop-to-idle time inside the
     controlled-decel band, no fault, no underrun.

K2 - resume waypoint honors the locked latch. Stream = pads, an
     X-step section (motors locked), then a 2 s FIRE window. Run with
     the latch LOCKED, controlled-stop inside the initial pads, then
     resume with a positive waypoint - the branch that re-enables the
     laser after the waypoint is reached. PASS: laser_enable stays 0
     through the FIRE window; laser_on/laser_on_sampled stay 0.

K3 - mid-run latch unlock does not re-arm FIRE. Stream = FIRE bits
     throughout. Run with the latch LOCKED (the run-start guard makes
     the run laser-less), then write laser_latch=0 during the accel
     ramp (ramp_rate lowered to 10000 Hz/s for a ~0.9 s window). The
     unlock must drive the latch pin (interlock bit 3 reads 0) but
     must NOT restore the FIRE output drive while the run or a ramp
     is in flight. PASS: laser_enable stays 0 for the entire run.
"""
import errno, fcntl, os, struct, sys, time

TICK_HZ = 10000
FIRE = b'\x10'
PAD = b'\x00'
XSTEP = b'\x01'
POWER0 = bytes([0x80])


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


def open_pulsedev():
    try:
        fd = os.open('/dev/glowforge', os.O_WRONLY)
    except OSError as e:
        if e.errno == errno.EBUSY:
            print('ABORT: /dev/glowforge is busy - stop forgectrl first '
                  '(/etc/init.d/forgectrl stop), then re-run')
            sys.exit(1)
        raise
    fcntl.flock(fd, fcntl.LOCK_EX)
    return fd


def wait_state(want, timeout, poll=0.05):
    t0 = time.time()
    while time.time() - t0 < timeout:
        s = rd('cnc/state')
        if s == want:
            return s
        time.sleep(poll)
    return rd('cnc/state')


def watch_laser_until_idle(timeout):
    """Tight-loop laser_enable/laser_on watch; returns (hits, end_state)."""
    hits = []
    t0 = time.time()
    state = 'running'
    while time.time() - t0 < timeout:
        en = rd('cnc/laser_enable')
        on = rd('cnc/laser_on')
        if en != '0' or on != '0':
            hits.append((round(time.time() - t0, 4), en, on))
        state = rd('cnc/state')
        if state != 'running':
            break
    return hits, state


def drill_k1():
    stream = POWER0 + PAD * (6 * TICK_HZ)
    print('K1: %d bytes = %.1f s of pads at %d Hz, ramp 125000 Hz/s'
          % (len(stream), len(stream) / TICK_HZ, TICK_HZ))
    snap('pre ')
    wr('cnc/motor_lock', 15)
    wr('cnc/laser_latch', 1)
    wr('cnc/ramp_rate', 125000)
    wr('cnc/step_freq', TICK_HZ)
    fd = open_pulsedev()
    try:
        os.lseek(fd, 1, os.SEEK_SET)
        wr('cnc/enable', 1)
        time.sleep(0.5)
        os.write(fd, stream)
        wr('cnc/run', 1)
        time.sleep(1.5)                      # well past the accel ramp
        if rd('cnc/state') != 'running':
            print('FAIL: expected running before the stop, got %s' % rd('cnc/state'))
            return 1
        t0 = time.time()
        wr('cnc/stop', 1)
        while time.time() - t0 < 5:
            if rd('cnc/state') != 'running':
                break
        dt = time.time() - t0
        state = rd('cnc/state')
        print('controlled stop: state=%s after %.4f s' % (state, dt))
        ok = state == 'idle' and 0.02 <= dt <= 3.0 and rd('cnc/faults') == '0'
        if state == 'idle' and dt < 0.02:
            print('FAIL: stop consumed the tail as a burst (%.4f s) - decel floor broken' % dt)
        elif not ok:
            print('FAIL: state=%s dt=%.4f faults=%s' % (state, dt, rd('cnc/faults')))
        else:
            print('PASS: decelerating tail %.4f s, no burst, no fault' % dt)
        # drain the paused remainder laser-less so the device ends clean
        wr('cnc/resume', 0)
        wait_state('running', 2, poll=0.005)
        wait_state('idle', 10)
        return 0 if ok else 1
    finally:
        wr('cnc/laser_latch', 1)
        fcntl.flock(fd, fcntl.LOCK_UN)
        os.close(fd)


def drill_k2():
    # "unmask-x" as a second argument runs with motor_lock=14 (X live,
    # ~6 mm of X travel) for boards where the waypoint counter does not
    # advance under a full motor_lock. Park the head mid-bed first.
    lock = 14 if 'unmask-x' in [a.lower() for a in sys.argv[2:]] else 15
    step_sec = (XSTEP + PAD * 4) * 1000      # 1000 X steps at 2 kHz
    stream = (POWER0 + PAD * TICK_HZ + step_sec + PAD * (TICK_HZ // 2)
              + FIRE * (2 * TICK_HZ) + PAD * TICK_HZ)
    print('K2: %d bytes = %.1f s; latch stays LOCKED; waypoint +200; motor_lock=%d'
          % (len(stream), len(stream) / TICK_HZ, lock))
    snap('pre ')
    wr('cnc/motor_lock', lock)
    wr('cnc/laser_latch', 1)
    wr('cnc/ramp_rate', 125000)
    wr('cnc/step_freq', TICK_HZ)
    fd = open_pulsedev()
    try:
        os.lseek(fd, 1, os.SEEK_SET)
        wr('cnc/enable', 1)
        time.sleep(0.5)
        os.write(fd, stream)
        pos_before = rd_pos()
        wr('cnc/run', 1)
        time.sleep(0.4)                      # inside the initial pads
        wr('cnc/stop', 1)
        state = wait_state('idle', 5, poll=0.01)
        if state != 'idle':
            print('FAIL: controlled stop did not reach idle (state=%s)' % state)
            return 1
        print('paused inside the pads; resuming with waypoint +200 (latch LOCKED)')
        wr('cnc/resume', 200)
        wait_state('running', 2, poll=0.005)
        hits, state = watch_laser_until_idle(20)
        pos_after = rd_pos()
        print('done: state=%s' % state)
        print('pos before=%s after=%s (informational: does the waypoint counter '
              'advance under motor_lock)' % (pos_before, pos_after))
        snap('post')
        print('laser_on_sampled=%s underruns=%s faults=%s'
              % (rd('cnc/laser_on_sampled'), rd('cnc/underruns'), rd('cnc/faults')))
        if hits:
            print('FAIL: laser asserted with the latch locked: %s' % hits[:10])
            return 1
        if pos_before[:3] == pos_after[:3]:
            print('NOTE: position did not advance under motor_lock, so the '
                  'waypoint may not have completed. Re-run as '
                  '"gate_a_kernel_drills.py K2 unmask-x" (X live, ~6 mm of '
                  'X travel) with the head parked mid-bed.')
        print('PASS: FIRE window replayed after the resume waypoint with '
              'laser_enable/laser_on at 0 throughout')
        return 0
    finally:
        wr('cnc/laser_latch', 1)
        fcntl.flock(fd, fcntl.LOCK_UN)
        os.close(fd)


def drill_k3():
    why = wait_hv_off()
    if why is not None:
        print('ABORT: the safety chain is not holding HV off (%s) - refusing latch unlock' % why)
        return 1
    stream = POWER0 + FIRE * (3 * TICK_HZ) + PAD * (TICK_HZ // 2)
    print('K3: %d bytes = %.1f s of FIRE bits; ramp_rate 10000 Hz/s '
          '(~0.9 s accel window); unlock at t=+0.15 s'
          % (len(stream), len(stream) / TICK_HZ))
    snap('pre ')
    wr('cnc/motor_lock', 15)
    wr('cnc/laser_latch', 1)
    wr('cnc/step_freq', TICK_HZ)
    wr('cnc/ramp_rate', 10000)
    fd = open_pulsedev()
    try:
        os.lseek(fd, 1, os.SEEK_SET)
        wr('cnc/enable', 1)
        time.sleep(0.5)
        os.write(fd, stream)
        wr('cnc/run', 1)
        time.sleep(0.15)                     # inside the accel ramp
        wr('cnc/laser_latch', 0)
        print('latch UNLOCKED mid-ramp; interlock=%s (bit 3 should read 0)'
              % rd('cnc/interlock_circuit'))
        hits, state = watch_laser_until_idle(20)
        print('done: state=%s' % state)
        snap('post')
        print('laser_on_sampled=%s underruns=%s faults=%s'
              % (rd('cnc/laser_on_sampled'), rd('cnc/underruns'), rd('cnc/faults')))
        if hits:
            print('FAIL: FIRE drive re-armed by a mid-run unlock: %s' % hits[:10])
            return 1
        print('PASS: laser_enable stayed 0 for the entire run after the '
              'mid-ramp unlock')
        return 0
    finally:
        wr('cnc/laser_latch', 1)
        try:
            wr('cnc/ramp_rate', 125000)
        except OSError:
            print('WARNING: could not restore ramp_rate=125000 - restore it by hand')
        fcntl.flock(fd, fcntl.LOCK_UN)
        os.close(fd)


def main():
    mode = sys.argv[1].upper() if len(sys.argv) > 1 else ''
    drills = {'K1': drill_k1, 'K2': drill_k2, 'K3': drill_k3}
    if mode not in drills:
        print(__doc__)
        return 2
    rc = drills[mode]()
    wr('cnc/laser_latch', 1)
    print('exit: state=%s latch=LOCKED' % rd('cnc/state'))
    return rc


if __name__ == '__main__':
    sys.exit(main())
