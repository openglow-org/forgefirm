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
the latch is re-locked unconditionally on exit, a SIGTERM (the bench
page's Abort), a SIGHUP (a lost terminal) and a SIGINT (Ctrl-C) included,
and a drill cut short that way, or by an error, halts its run. The first
of those signals sets all three to ignore, so no later one cuts the way
out short, and a Ctrl-Z does not stop a drill (SIGTSTP is ignored: K3
stopped would hold the latch unlocked with nothing judging the run). What
kills the interpreter (a SIGKILL, a SIGQUIT) falls to the kernel's close
of the pulse device, which halts the run and locks the latch. K3 unlocks the
latch under its own run, which feeds the charge pump and so raises HV. It
runs only with the remote-interlock loop open (unplug the interlock
connector on a Pro, pull the J8 jumper on a Basic or Plus), which sets
the interlock latch and blocks LASER_ON whatever the button does: it
starts only while nothing else feeds the watchdog (charge_pump_alive 0,
the engine idle) and the chain reads open by the guard the catalog's
kernel.fire-line puts before an unlock (interlock_circuit bit 4, and the
switches' EV_SW 5 and 6), read before the device is opened and again
right before the unlock. After its unlock the button latch must still
read set (the lock set it; only a press clears it), and every sample of
the run is judged as the catalog judges one: the button latch set
(bit 2), the loop open (bit 4), LASER_ON off (bit 0, raw, active low). A
reading that says otherwise, or a FIRE or LASER_ON reading, relocks the
latch at once and K3 fails. Exit 2 is a refusal with the latch never
unlocked. Software witnesses:
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
     the run laser-less), then write laser_latch=0 0.15 s into the run.
     A plain run starts at the full step rate with no acceleration
     ramp, so this proves the RUNNING term of the kernel's guard (a
     ramp runs only inside RUNNING). The unlock must drive the latch
     pin (interlock bit 3 reads 0) but must NOT restore the FIRE output
     drive while the run is in flight. PASS: laser_enable stays 0 for
     the entire run.
"""
import errno, fcntl, os, signal, struct, sys, time

# The acceptance tool is installed with the bench tools. Its kernel suite
# holds the guard an unlock stands behind (chain_open_why) and the judgment
# of a sample of an unlocked run (run_guard_why), used here as they are so
# K3 and the catalog test refuse, and relock, on one rule. The drills read
# and write sysfs under the root the guard reads.
from forgetest.suite import kernel as catalog

TICK_HZ = 10000
FIRE = b'\x10'
PAD = b'\x00'
XSTEP = b'\x01'
POWER0 = bytes([0x80])
ENDING = (signal.SIGTERM, signal.SIGHUP, signal.SIGINT)


def end_on_signal(signum, frame):
    for sig in ENDING:                       # no second one ends the way out
        signal.signal(sig, signal.SIG_IGN)
    raise SystemExit(128 + signum)


def wr(attr, val):
    with open(catalog.hw.sysfs_root() + attr, 'w') as f:
        f.write(str(val))


def rd(attr):
    with open(catalog.hw.sysfs_root() + attr) as f:
        return f.read().strip()


# A run feeds the charge-pump watchdog, and HV_ENABLE follows it with the
# lid closed: a dead watchdog and an idle pulse engine say that nothing else
# feeds it before K3's own run, which raises HV itself. laser_pgood is the
# supply's power-good, high on every healthy machine, and says nothing
# about HV.
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
    t0 = time.monotonic()
    why = hv_off_reason()
    while why is not None and time.monotonic() - t0 < timeout_s:
        time.sleep(0.05)
        why = hv_off_reason()
    return why


def rd_pos():
    with open(catalog.hw.sysfs_root() + 'cnc/position', 'rb') as f:
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


def halt_cut_run():
    """The run of a drill that was cut short (an Abort, a lost terminal, an
    error) does not play on behind it: the drill releases its flock before
    it closes the device, so that close halts nothing. The kernel takes a
    halt at idle and refuses one only in the fault and underrun states,
    where nothing plays."""
    try:
        wr('cnc/halt', 1)
    except OSError:
        pass


def wait_state(want, timeout, poll=0.05):
    t0 = time.monotonic()
    while time.monotonic() - t0 < timeout:
        s = rd('cnc/state')
        if s == want:
            return s
        time.sleep(poll)
    return rd('cnc/state')


def watch_laser_until_idle(timeout):
    """Tight-loop laser_enable/laser_on watch; returns (hits, end_state)."""
    hits = []
    t0 = time.monotonic()
    state = 'running'
    while time.monotonic() - t0 < timeout:
        en = rd('cnc/laser_enable')
        on = rd('cnc/laser_on')
        if en != '0' or on != '0':
            hits.append((round(time.monotonic() - t0, 4), en, on))
        state = rd('cnc/state')
        if state != 'running':
            break
    return hits, state


def watch_unlocked_until_idle(timeout):
    """The same watch for a run with the latch unlocked: every sample is
    judged first as the catalog judges one (run_guard_why: the button
    latch set, the loop open, LASER_ON off), and a reason, or a FIRE or
    LASER_ON reading after it, relocks the latch at once, before anything
    else is read, and ends the watch. Returns (hits, end_state, why): why
    is None for a run that stayed blocked and dark to its end."""
    hits = []
    t0 = time.monotonic()
    state = 'running'
    while time.monotonic() - t0 < timeout:
        why = catalog.run_guard_why(int(rd('cnc/interlock_circuit')))
        if why is None:
            en = rd('cnc/laser_enable')
            on = rd('cnc/laser_on')
            if en != '0' or on != '0':
                hits.append((round(time.monotonic() - t0, 4), en, on))
                why = 'FIRE or LASER_ON read driven (laser_enable=%s laser_on=%s)' % (en, on)
        if why is not None:
            wr('cnc/laser_latch', 1)
            return hits, rd('cnc/state'), why
        state = rd('cnc/state')
        if state != 'running':
            break
    return hits, state, None


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
    cut = False
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
        t0 = time.monotonic()
        wr('cnc/stop', 1)
        while time.monotonic() - t0 < 5:
            if rd('cnc/state') != 'running':
                break
        dt = time.monotonic() - t0
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
    except BaseException:
        cut = True
        raise
    finally:
        wr('cnc/laser_latch', 1)
        if cut:
            halt_cut_run()
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
    cut = False
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
    except BaseException:
        cut = True
        raise
    finally:
        wr('cnc/laser_latch', 1)
        if cut:
            halt_cut_run()
        fcntl.flock(fd, fcntl.LOCK_UN)
        os.close(fd)


def drill_k3():
    why = wait_hv_off()
    if why is not None:
        print('REFUSED: something else feeds the charge-pump watchdog (%s) - refusing the latch unlock' % why)
        return 2
    why = catalog.chain_open_why()
    if why is not None:
        print('REFUSED: %s - open the remote-interlock loop (unplug the interlock connector on a '
              'Pro, pull the J8 jumper on a Basic or Plus) and run again' % why)
        return 2
    stream = POWER0 + FIRE * (3 * TICK_HZ) + PAD * (TICK_HZ // 2)
    print('K3: %d bytes = %.1f s of FIRE bits; unlock at t=+0.15 s into the run'
          % (len(stream), len(stream) / TICK_HZ))
    snap('pre ')
    wr('cnc/motor_lock', 15)
    wr('cnc/laser_latch', 1)
    wr('cnc/step_freq', TICK_HZ)
    fd = open_pulsedev()
    try:
        os.lseek(fd, 1, os.SEEK_SET)
        wr('cnc/enable', 1)
        time.sleep(0.5)
        os.write(fd, stream)
        wr('cnc/run', 1)
        time.sleep(0.15)                     # inside the run
        # The guard again, at the unlock, the run in flight and locked.
        # Refused here, the latch stays locked.
        why = catalog.chain_open_why()
        if why is not None:
            print('REFUSED: %s - the latch stays locked' % why)
            return 2
        wr('cnc/laser_latch_cooling', 0)     # the cooling holder: no engine runs here
        wr('cnc/laser_latch', 0)
        # The lock set the button latch, and only a press clears it.
        latch = rd('cnc/button_latch')
        if latch != '1':
            wr('cnc/laser_latch', 1)
            print('FAIL: cnc/button_latch reads %s after the unlock (a press) - relocked' % latch)
            return 1
        print('latch UNLOCKED mid-run; interlock=%s (bit 3 should read 0); button latch set'
              % rd('cnc/interlock_circuit'))
        hits, state, why = watch_unlocked_until_idle(20)
        print('done: state=%s' % state)
        snap('post')
        print('laser_on_sampled=%s underruns=%s faults=%s'
              % (rd('cnc/laser_on_sampled'), rd('cnc/underruns'), rd('cnc/faults')))
        if hits:
            print('FAIL: FIRE drive re-armed by a mid-run unlock: %s - relocked' % hits[:10])
            return 1
        if why is not None:
            print('FAIL: %s - relocked' % why)
            return 1
        print('PASS: laser_enable stayed 0 for the entire run after the '
              'mid-run unlock')
        return 0
    finally:
        for sig in ENDING:                   # a second Abort does not cut the way out short
            signal.signal(sig, signal.SIG_IGN)
        wr('cnc/laser_latch', 1)
        try:
            wr('cnc/halt', 1)                # a run a refusal or a failure left playing ends here
        except OSError:
            pass
        fcntl.flock(fd, fcntl.LOCK_UN)
        os.close(fd)


def main():
    mode = sys.argv[1].upper() if len(sys.argv) > 1 else ''
    drills = {'K1': drill_k1, 'K2': drill_k2, 'K3': drill_k3}
    if mode not in drills:
        print(__doc__)
        return 2
    signal.signal(signal.SIGTSTP, signal.SIG_IGN)
    for sig in ENDING:
        signal.signal(sig, end_on_signal)
    rc = drills[mode]()
    wr('cnc/laser_latch', 1)
    print('exit: state=%s latch=LOCKED' % rd('cnc/state'))
    return rc


if __name__ == '__main__':
    sys.exit(main())
