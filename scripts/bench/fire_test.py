#!/usr/bin/env python3
# Copyright 2026 514 LLC d/b/a OpenGlow
# Written by Scott Wiederhold
# https://community.openglow.org
# SPDX-License-Identifier:    MIT
"""FIRE-line drop-timing test - runs ON the board. Usage: fire_test.py A|B|U

Stream: power(0) first byte (duty zero before any FIRE bit), then
1 s pads / 2.000 s FIRE bits / 1 s pads / 2.000 s FIRE bits ending
exactly at end-of-data (the backstop edge the gate wants timed).
No step bytes; motor_lock=15. The drill ends idle with the 40 V rail up.

Phase A: laser latch LOCKED - expects nothing on the FIRE/LASER_ON nets.
Phase B: latch UNLOCKED for the run (re-locked in finally), streaming=0
         (end-of-data = normal completion). The drill's own run feeds the
         charge pump, so HV is up while FIRE plays at duty 0: B and U run
         only with the remote-interlock loop open (unplug the interlock
         connector on a Pro, pull the J8 jumper on a Basic or Plus), which
         sets the interlock latch and blocks LASER_ON whatever the button
         does. Before the unlock it checks that nothing else feeds the
         watchdog (charge_pump_alive 0, the engine idle) and that the
         chain reads open as the catalog's kernel.fire-line requires it
         before an unlock, by that test's own guard: the kernel drives
         the interlock latch's SET (interlock_circuit bit 4), and the
         switches read the loop open (EV_SW 5) and the interlock latch
         itself set (EV_SW 6). The guard is read before the device is
         opened and again right before the unlock. After the unlock,
         and in every sample of the run, by the catalog's own judgment
         of a sample: the button latch must read set (bit 2), the loop
         open (bit 4) and LASER_ON off (bit 0, raw, active low). A
         reading that says otherwise relocks the latch at once and
         fails the drill.
Phase U: like B but with streaming=1 declared, so the terminal
         end-of-data is a TRUE UNDERRUN: the kernel lands in the
         `underrun` fault state (expected - the script acks it via
         stop). Same SDMA backstop, exercised through the fault path;
         the scope measurement on the FIRE net is identical.

The exit status is the verdict, judged as the catalog's kernel.fire-line
judges the same phases: 0 pass, 1 fail (any LASER_ON sample among them),
2 refused with the latch never unlocked. A SIGTERM (the bench page's
Abort), a SIGHUP (a lost terminal) or a SIGINT (Ctrl-C) ends the drill
through the same way out as an error: the latch relocked, the run halted
and, in U, streaming off. The first of them sets all three to ignore, and
so does the way out itself, so no later one cuts it short. A Ctrl-Z does
not stop the drill (SIGTSTP is ignored): a stopped drill would hold the
latch unlocked with nothing judging the run. What kills the interpreter
(a SIGKILL, a SIGQUIT) falls to the kernel's close of the pulse device,
which halts the run and locks the latch.
"""
import fcntl, os, signal, struct, sys, time

# The acceptance tool is installed with the bench tools. Its kernel suite
# holds the guard an unlock stands behind (chain_open_why) and the judgment
# of a sample of an unlocked run (run_guard_why), used here as they are so
# the drill and the catalog test refuse, and relock, on one rule. The
# drill reads and writes sysfs under the root the guard reads.
from forgetest.suite import kernel as catalog

TICK_HZ = 10000
FIRE = b'\x10'
PAD = b'\x00'
ENDING = (signal.SIGTERM, signal.SIGHUP, signal.SIGINT)


def end_on_signal(signum, frame):
    for sig in ENDING:                 # no second one ends the way out
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
# feeds it before this drill's own run, which raises HV itself. laser_pgood
# is the supply's power-good, high on every healthy machine, and says
# nothing about HV.
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


def stream():
    return (
        bytes([0x80]) +                # power = 0: duty zero before any FIRE bit
        PAD * TICK_HZ +                # 1 s baseline
        FIRE * (2 * TICK_HZ) +         # 2.000 s FIRE window (bounded by pads)
        PAD * TICK_HZ +                # 1 s gap
        FIRE * (2 * TICK_HZ)           # 2.000 s FIRE window ending AT end-of-data
    )


def run_phase(mode):
    """One phase; returns (status, evidence). Status 2 is a refusal with
    the latch never unlocked (before the pulse device was opened, or at the
    unlock), 1 an unlock that found the run unblocked (relocked, no run);
    None means the evidence is to be judged."""
    unlock = mode in ('B', 'U')
    underrun_mode = mode == 'U'
    data = stream()
    print('phase %s: stream %d bytes = %.3f s' % (mode, len(data), len(data) / TICK_HZ))
    if unlock:
        why = wait_hv_off()
        if why is not None:
            print('REFUSED: something else feeds the charge-pump watchdog (%s) - refusing the latch unlock'
                  % why)
            return 2, None
        why = catalog.chain_open_why()
        if why is not None:
            print('REFUSED: %s - open the remote-interlock loop (unplug the interlock connector on a '
                  'Pro, pull the J8 jumper on a Basic or Plus) and run again' % why)
            return 2, None

    snap('pre ')
    wr('cnc/motor_lock', 15)
    wr('cnc/step_freq', TICK_HZ)
    wr('cnc/laser_latch', 1)
    ev = {'mid': None, 'tail': None, 'blocked': [], 'samples': 0, 'any_fire_driven': False,
          'any_laser_on': False, 'underruns_before': int(rd('cnc/underruns'))}

    fd = os.open('/dev/glowforge', os.O_WRONLY)
    cut = False
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        os.lseek(fd, 1, os.SEEK_SET)
        wr('cnc/enable', 1)
        time.sleep(0.5)
        os.write(fd, data)
        ev['pos_before'] = rd_pos()

        if underrun_mode:
            wr('cnc/streaming', 1)     # end-of-data mid-run = true underrun
            print('streaming=1: terminal end-of-data will be a TRUE UNDERRUN (expected)')

        unlocked = False
        if unlock:
            # The guard again, at the unlock: the setup since the first
            # read took half a second. Refused here, the latch stays locked
            # and nothing runs.
            why = catalog.chain_open_why()
            if why is not None:
                print('REFUSED: %s - the latch stays locked, no run' % why)
                return 2, None
            wr('cnc/laser_latch_cooling', 0)   # the cooling holder: no engine runs here
            wr('cnc/laser_latch', 0)   # UNLOCK for this run only
            unlocked = True
            # The lock set the button latch; the read before the unlock
            # proves nothing, since the lock holds it set.
            latch = rd('cnc/button_latch')
            ilk = int(rd('cnc/interlock_circuit'))
            why = (catalog.run_guard_why(ilk) if latch == '1'
                   else 'cnc/button_latch reads %s after the unlock' % latch)
            if why is not None:
                wr('cnc/laser_latch', 1)
                print('FAIL: %s - relocked, no run' % why)
                ev['blocked'].append((0.0, why))
                return 1, ev
            print('latch UNLOCKED for phase %s run; button latch set, interlock loop open' % mode)

        wr('cnc/run', 1)
        t0 = time.monotonic()
        state = ''
        while time.monotonic() - t0 < 20:
            dt = time.monotonic() - t0
            state = rd('cnc/state')
            ilk = int(rd('cnc/interlock_circuit'))
            if unlocked:
                # The catalog's judgment of a sample, before anything else
                # is read: the button latch set, the loop open, LASER_ON
                # off.
                why = catalog.run_guard_why(ilk)
                if why is not None:
                    # Locked at once: FIRE parks and the lock sets the
                    # button latch. The run plays out dark.
                    wr('cnc/laser_latch', 1)
                    unlocked = False
                    ev['blocked'].append((round(dt, 3), why))
                    print('FAIL at +%.3f s: %s - relocked' % (dt, why))
            en, on, ons = rd('cnc/laser_enable'), rd('cnc/laser_on'), rd('cnc/laser_on_sampled')
            ev['samples'] += 1
            ev['any_fire_driven'] |= en != '0'
            ev['any_laser_on'] |= on != '0' or ons != '0'
            if ev['mid'] is None and 1.5 < dt < 3.0:
                ev['mid'] = {'t': round(dt, 2), 'laser_enable': en, 'laser_on': on,
                             'laser_on_sampled': ons, 'interlock': ilk}
                snap('mid (inside FIRE window)')
            if state != 'running':
                break
            time.sleep(0.002)
        ev['tail'] = {'state': state, 'after_s': round(time.monotonic() - t0, 2),
                      'laser_enable': rd('cnc/laser_enable'), 'laser_on': rd('cnc/laser_on')}
        print('done: state=%s after %.1f s' % (state, ev['tail']['after_s']))
        if underrun_mode:
            if state == 'underrun':
                print('underrun state reached as EXPECTED; acking via stop')
            else:
                print('WARNING: expected underrun state, got %s' % state)
            wr('cnc/stop', 1)          # ack the underrun
            wr('cnc/streaming', 0)
            ev['tail']['acked_state'] = rd('cnc/state')
            print('acked: state=%s' % ev['tail']['acked_state'])
    except BaseException:
        cut = True                     # an Abort, a lost terminal, a read that failed
        raise
    finally:
        for sig in ENDING:             # a second Abort does not cut the way out short
            signal.signal(sig, signal.SIG_IGN)
        wr('cnc/laser_latch', 1)       # re-lock unconditionally
        if cut:
            # The run does not play on behind a drill that was cut short
            # (the flock is released before the close below, so that close
            # halts nothing). The kernel takes a halt at idle and refuses
            # one only in the fault and underrun states, where nothing plays.
            try:
                wr('cnc/halt', 1)
            except OSError:
                pass
        if underrun_mode:
            try:
                wr('cnc/streaming', 0)     # whatever ended the run
            except OSError:
                pass
        fcntl.flock(fd, fcntl.LOCK_UN)
        os.close(fd)

    pos_after = rd_pos()
    ev['moved'] = ev['pos_before'][:3] != pos_after[:3]
    ev['underruns_after'] = int(rd('cnc/underruns'))
    print('pos before=%s after=%s MOVED=%s' % (ev['pos_before'], pos_after, ev['moved']))
    snap('post')
    print('underruns=%s faults=%s' % (ev['underruns_after'], rd('cnc/faults')))
    return None, ev


def judge(mode, ev):
    """The checks kernel.fire-line makes of the same phase: a list of
    failures, empty for a pass."""
    fails = []

    def check(cond, msg):
        if not cond:
            fails.append(msg)
    tail = ev['tail'] or {}
    check(not ev['blocked'], 'the unlocked run did not stay blocked: %s' % ev['blocked'])
    check(not ev['any_laser_on'], 'LASER_ON seen (laser_on or laser_on_sampled nonzero)')
    check(not ev['moved'], 'the position moved with the motors locked')
    if mode == 'A':
        check(ev['mid'] is not None, 'no mid-window sample')
        check(not ev['any_fire_driven'], 'FIRE line driven with the latch locked')
        check(tail.get('state') == 'idle', 'ended in %s' % tail.get('state'))
    elif mode == 'B':
        check(ev['mid'] is not None and ev['mid']['laser_enable'] != '0',
              'FIRE line not driven mid-window with the latch unlocked (%s)' % ev['mid'])
        check(tail.get('state') == 'idle', 'ended in %s' % tail.get('state'))
        check(tail.get('laser_enable') == '0', 'FIRE still driven after end-of-data')
        check(ev['underruns_after'] == ev['underruns_before'], 'underrun counted on a normal completion')
    else:
        check(tail.get('state') == 'underrun', 'expected the underrun state, got %s' % tail.get('state'))
        check(tail.get('laser_enable') == '0', 'FIRE still driven after the underrun')
        check(tail.get('acked_state') == 'idle', 'stop did not ack the underrun (state %s)'
              % tail.get('acked_state'))
        check(ev['underruns_after'] == ev['underruns_before'] + 1,
              'underrun counter %d -> %d' % (ev['underruns_before'], ev['underruns_after']))
    return fails


def main():
    mode = sys.argv[1].upper() if len(sys.argv) > 1 else 'A'
    if mode not in ('A', 'B', 'U'):
        print(__doc__)
        return 2
    signal.signal(signal.SIGTSTP, signal.SIG_IGN)
    for sig in ENDING:
        signal.signal(sig, end_on_signal)
    rc, ev = run_phase(mode)
    if rc is not None:
        print('PHASE %s %s' % (mode, 'REFUSED' if rc == 2 else 'FAIL'))
        return rc
    fails = judge(mode, ev)
    for msg in fails:
        print('FAIL: %s' % msg)
    print('state=%s latch=LOCKED, rail up' % rd('cnc/state'))
    print('PHASE %s %s' % (mode, 'PASS' if not fails else 'FAIL'))
    return 0 if not fails else 1


if __name__ == '__main__':
    sys.exit(main())
