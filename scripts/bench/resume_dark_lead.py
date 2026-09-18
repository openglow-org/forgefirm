#!/usr/bin/env python3
# Copyright 2026 514 LLC d/b/a OpenGlow
# Written by Scott Wiederhold
# https://community.openglow.org
# SPDX-License-Identifier:    MIT
"""Resume dark-lead characterization (runs ON the board, as root).

Measures what the safety chain does across a pause and a resume, at pad
resolution, so the GRBL resume dwell can be decided from numbers instead
of from the mark alone. The pause and the resume are the operator's
physical button presses - the same toggle the controller ships.

Sampled straight from the SoC pads (no kernel change, no scope), one
32-bit read per bank per pass:

  LASER_ON      GPIO1_05  gated output of the safety AND gate (active low)
  CP_ALIVE      GPIO1_08  charge-pump watchdog !Q      (0 = alive)
  BUTTON_LATCH  GPIO1_03  1 = latch SET (fire blocked)
  DOORS         GPIO1_00  0 = both lid switches closed
  FIRE          GPIO2_30  laser-enable line, read from the data register
                          (the SDMA writes it; GDIR says whether it is driven)
  BUTTON        GPIO4_09  big button        (0 = pressed)
  HV_ENABLE     GPIO4_06  chain output readback, inverted (0 = asserted)

Levels are read once at idle and every later sample is reported as a
change from that baseline, so no polarity assumption is baked in.

The headline number is the **dark lead**: FIRE re-asserted (the stream is
commanding emission again) -> LASER_ON asserted (the chain actually lets
the beam through). At the job's feed rate that is also a distance, which
is what shows up in the mark. The pause side reports how long HV_ENABLE
survives the stream stopping, and the charge-pump one-shot period behind it.

The loop must not hog the CPU: this is a single-core part, only the
shipper thread is SCHED_FIFO, and starving the SCHED_OTHER protocol thread
mid-job would underrun the ring. It samples at ~2 kHz with a positive nice
and reports the worst gap it actually achieved.

Usage: resume_dark_lead.py [--run dry|live] [--power S] [--feed F]
                           [--len MM] [--passes N] [--mode m3|m4]
                           [--secs N] [--auto P,R] [--json FILE]

  --run dry   (default) a plain G1 travel of --len at --feed, no laser
              command at all. Exercises the button pause/resume and gives
              the HV_ENABLE / charge-pump timings, which do not need fire -
              the pump runs for any pulse-engine run. Run this first.
  --run live  LIVE FIRE: M3 (or M4) at --power. Requires the arm press, eye
              protection, exhaust, fire watch, extinguisher, scrap under the
              head. Adds the LASER_ON edge - the dark lead itself - and the
              physical mark to measure.

Operator sequence in both cases: press to arm (live only), then press once
mid-move to pause, and once more to resume. GRBL mode only, with the
controller idle and no other Grbl client attached (a connection here
displaces the sender).
"""
import argparse
import json
import mmap
import os
import socket
import struct
import sys
import time

GPIO1, GPIO2, GPIO4 = 0x0209C000, 0x020A0000, 0x020A8000
DR, GDIR, PSR = 0x00, 0x04, 0x08

# name -> (bank, register, bit). Order is the report order.
SIGNALS = [
    ('FIRE',         'g2', DR,  30),
    ('LASER_ON',     'g1', PSR,  5),
    ('HV_ENABLE',    'g4', PSR,  6),
    ('CP_ALIVE',     'g1', PSR,  8),
    ('BUTTON',       'g4', PSR,  9),
    ('BUTTON_LATCH', 'g1', PSR,  3),
    ('DOORS',        'g1', PSR,  0),
]

# Motion comes from the kernel's step counters, not from the step lines:
# a step pulse is microseconds wide, so a pad sampler at this rate catches
# only the occasional one. cnc/position is 32 binary bytes (X, Y, Z steps
# first) and re-reading it at MOTION_HZ dates the restart closely enough to
# compare against a chain that re-arms in single-digit milliseconds.
POSITION = '/sys/glowforge/cnc/position'
MOTION_HZ = 50.0
MOTION_GAP_S = 0.10

SLEEP_S = 0.0005
HOST, PORT = '127.0.0.1', 23


class Pads:
    """One mmap per bank; a pass reads three words."""

    def __init__(self):
        self.fd = os.open('/dev/mem', os.O_RDWR | os.O_SYNC)
        self.m = {
            'g1': mmap.mmap(self.fd, 4096, mmap.MAP_SHARED,
                            mmap.PROT_READ, offset=GPIO1),
            'g2': mmap.mmap(self.fd, 4096, mmap.MAP_SHARED,
                            mmap.PROT_READ, offset=GPIO2),
            'g4': mmap.mmap(self.fd, 4096, mmap.MAP_SHARED,
                            mmap.PROT_READ, offset=GPIO4),
        }

    # (bank, register) -> mask of the bits actually watched there. The step
    # lines and the PWM share these banks, so an unmasked word compare would
    # log a transition on every step.
    SOURCES = []
    for _n, _b, _r, _bit in SIGNALS:
        for _i, (_sb, _sr, _sm) in enumerate(SOURCES):
            if (_sb, _sr) == (_b, _r):
                SOURCES[_i] = (_sb, _sr, _sm | (1 << _bit))
                break
        else:
            SOURCES.append((_b, _r, 1 << _bit))

    def words(self):
        m = self.m
        return tuple(struct.unpack_from('<I', m[b], r)[0] & mask
                     for b, r, mask in self.SOURCES)


    def decode(self, w):
        src = {(b, r): v for (b, r, _m), v in zip(self.SOURCES, w)}
        return {n: (src[(b, r)] >> bit) & 1 for n, b, r, bit in SIGNALS}

    def fire_is_driven(self):
        gdir = struct.unpack('<I', self.m['g2'][GDIR:GDIR + 4])[0]
        return bool((gdir >> 30) & 1)

    def close(self):
        for m in self.m.values():
            m.close()
        os.close(self.fd)


class Grbl:
    def __init__(self):
        self.s = socket.create_connection((HOST, PORT), timeout=5)
        self.s.settimeout(0.2)
        time.sleep(0.5)
        self.drain()

    def drain(self):
        out = b''
        try:
            while True:
                d = self.s.recv(4096)
                if not d:
                    break
                out += d
        except socket.timeout:
            pass
        return out.decode('ascii', 'replace')

    def send(self, line):
        self.s.sendall(line.encode() + b'\n')

    def status(self):
        self.s.sendall(b'?')
        deadline = time.time() + 1.0
        text = ''
        while time.time() < deadline:
            text += self.drain()
            if '>' in text:
                break
            time.sleep(0.02)
        return text[text.rfind('<'):text.rfind('>') + 1] if '<' in text else ''

    def state(self):
        st = self.status()
        return st[1:].split('|')[0] if st else ''


class Counters:
    """X/Y/Z step counters, re-read from the same open descriptor."""

    def __init__(self):
        self.f = open(POSITION, 'rb', buffering=0)

    def read(self):
        self.f.seek(0)
        return struct.unpack_from('<iii', self.f.read(32), 0)

    def close(self):
        self.f.close()


def sysfs(name):
    try:
        with open('/sys/glowforge/cnc/' + name) as f:
            return f.read().strip()
    except OSError:
        return '?'


def sample(pads, seconds, grbl, job_lines, marks, auto=()):
    """Sample the pads for `seconds`, sending `job_lines` once settled.
    `auto` is a schedule of (t_seconds, realtime_byte, label) sent from the
    sampling loop - used by the unattended rehearsal, where `!` and `~`
    stand in for the operator's pause and resume presses.
    Returns (baseline, events, worst_gap). An event is (t, changed_dict)."""
    words = pads.words()
    base = pads.decode(words)
    events = []
    motion = []
    t0 = time.perf_counter()
    prev = words
    prev_state = base
    counters = Counters()
    prev_pos = counters.read()
    next_pos_t = t0
    last_move = None
    last = t0
    worst = 0.0
    sent = False
    end = t0 + seconds
    while True:
        now = time.perf_counter()
        if now >= end:
            break
        gap = now - last
        if gap > worst:
            worst = gap
        last = now
        w = pads.words()
        if w != prev:
            st = pads.decode(w)
            changed = {k: v for k, v in st.items() if v != prev_state[k]}
            if changed:
                events.append((now - t0, changed, st))
            prev, prev_state = w, st
        if now >= next_pos_t:
            next_pos_t = now + 1.0 / MOTION_HZ
            pos = counters.read()
            if pos != prev_pos:
                prev_pos = pos
                if last_move is None or now - last_move > MOTION_GAP_S:
                    motion.append([now - t0, now - t0])
                else:
                    motion[-1][1] = now - t0
                last_move = now
        if not sent and now - t0 > 1.0:
            for ln in job_lines:
                grbl.send(ln)
            marks.append(('job sent', now - t0))
            sent = True
        while auto and now - t0 >= auto[0][0]:
            _at, ch, label = auto.pop(0)
            grbl.s.sendall(ch)
            marks.append((label, now - t0))
        time.sleep(SLEEP_S)
    counters.close()
    return base, events, worst, motion


def edges(events, name, base, to_active):
    """Times at which `name` moved to (to_active=True) or away from its
    non-baseline level."""
    out = []
    for t, changed, _st in events:
        if name in changed:
            active = changed[name] != base[name]
            if active == to_active:
                out.append(t)
    return out


def report(base, events, worst, args, marks, motion):
    print('\nbaseline at idle: %s' % ' '.join(
        '%s=%d' % (n, base[n]) for n, _b, _r, _bit in SIGNALS))
    print('worst sampling gap: %.2f ms (%d transitions)'
          % (worst * 1000.0, len(events)))
    for label, t in marks:
        print('  %-10s t=%.3f s' % (label, t))

    print('\n--- transitions (t in s from sampler start) ---')
    for t, changed, _st in events:
        desc = ' '.join('%s->%s' % (k, 'ACTIVE' if v != base[k] else 'idle')
                        for k, v in sorted(changed.items()))
        print('  %8.4f  %s' % (t, desc))

    fire_on = edges(events, 'FIRE', base, True)
    fire_off = edges(events, 'FIRE', base, False)
    lon_on = edges(events, 'LASER_ON', base, True)
    lon_off = edges(events, 'LASER_ON', base, False)
    hv_on = edges(events, 'HV_ENABLE', base, True)
    hv_off = edges(events, 'HV_ENABLE', base, False)
    cp_on = edges(events, 'CP_ALIVE', base, True)
    cp_off = edges(events, 'CP_ALIVE', base, False)
    btn = edges(events, 'BUTTON', base, True)

    print('\n--- motion (step-line activity) ---')
    for a, b in motion:
        print('  %8.4f -> %8.4f  (%.0f ms)' % (a, b, (b - a) * 1000.0))

    print('\n--- summary ---')
    print('button presses: %s' % (', '.join('%.3f' % t for t in btn) or 'none'))

    def after(times, t):
        later = [x for x in times if x > t]
        return later[0] if later else None

    mm_s = args.feed / 60.0
    starts = [a for a, _b in motion]
    stops = [b for _a, b in motion]

    # Each pause and resume is dated from what triggered it - the operator's
    # button press, or the ! / ~ the rehearsal sends in its place - so the
    # dry and the live run report the same way.
    triggers = [(lbl, t) for lbl, t in marks
                if lbl.startswith(('pause', 'resume'))]
    if btn:
        triggers = [('press %d' % (i + 1), t) for i, t in enumerate(btn)]
    triggers.sort(key=lambda x: x[1])

    for lbl, t in triggers:
        line = ['%-8s at %7.3f:' % (lbl, t)]
        for nm, times in (('motion stops', stops), ('motion starts', starts),
                          ('FIRE clear', fire_off), ('FIRE set', fire_on),
                          ('LASER_ON off', lon_off), ('LASER_ON on', lon_on),
                          ('HV drop', hv_off), ('HV up', hv_on),
                          ('CP fell', cp_off), ('CP alive', cp_on)):
            x = after(times, t)
            if x is not None and x - t < 2.0:
                line.append('%s +%.0f ms' % (nm, (x - t) * 1000.0))
        print('  ' + ' | '.join(line))

    # The dark lead is what a resumed cut loses: emission commanded again
    # (FIRE) but the chain not yet letting the beam through (LASER_ON).
    leads = []
    for t in fire_on:
        x = after(lon_on, t)
        if x is not None and x - t < 2.0:
            leads.append((x - t) * 1000.0)
    if leads:
        print('\nDARK LEAD (FIRE set -> LASER_ON): %s ms -> %s mm at F%g'
              % (', '.join('%.1f' % x for x in leads),
                 ', '.join('%.2f' % (x / 1000.0 * mm_s) for x in leads),
                 args.feed))
    elif args.run == 'dry':
        print('\n(dry run: FIRE and LASER_ON never assert - the chain '
              'numbers above are the ones this rehearsal establishes)')


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--run', choices=('dry', 'live'), default='dry',
                    dest='run',
                    help='dry: travel only, no laser commanded (default). '
                         'live: command the laser - LIVE FIRE.')
    ap.add_argument('--mode', choices=('m3', 'm4'), default='m3')
    ap.add_argument('--power', type=int, default=400)
    ap.add_argument('--feed', type=float, default=1200.0)
    ap.add_argument('--len', type=float, default=80.0, dest='length')
    ap.add_argument('--passes', type=int, default=1,
                    help='alternating +X/-X moves (dry: gives a long window '
                         'to press in; live: keep 1 so the mark is one line)')
    ap.add_argument('--secs', type=float, default=45.0)
    ap.add_argument('--auto', default='',
                    help='unattended rehearsal (dry only): PAUSE,RESUME in '
                         'seconds from the job start, e.g. --auto 3,6 - sends '
                         '! and ~ instead of waiting for button presses')
    ap.add_argument('--json', default='')
    args = ap.parse_args()

    if os.geteuid() != 0:
        sys.exit('must run as root (needs /dev/mem)')

    grbl = Grbl()
    st = grbl.state()
    if not st.startswith('Idle'):
        sys.exit('controller is not Idle (%s) - clear it first' % (st or '?'))
    print('controller: %s   interlock_circuit=%s  faults=%s  button_latch=%s'
          % (st, sysfs('interlock_circuit'), sysfs('faults'),
             sysfs('button_latch')))

    moves = ['G1 X%g F%g' % (args.length * (1 if i % 2 == 0 else -1), args.feed)
             for i in range(max(1, args.passes))]
    lines = ['G21', 'G91']
    if args.run == 'live':
        lines += ['%s S%d' % (args.mode.upper(), args.power)] + moves \
                 + ['M5', 'G90']
        print('\n>>> LIVE FIRE. Eye protection, exhaust running, fire watch,')
        print('>>> extinguisher in reach, scrap under the head with %g mm'
              % args.length)
        print('>>> of clear travel in +X.')
        print('>>> 1) press the button to ARM (the stream waits for it)')
        print('>>> 2) press once mid-cut to PAUSE')
        print('>>> 3) press once more to RESUME')
    else:
        lines += moves + ['G90']
        print('\n>>> DRY rehearsal - no laser is commanded.')
        if not args.auto:
            print('>>> 1) press the button mid-move to PAUSE')
            print('>>> 2) press once more to RESUME')
    print('>>> sampling for %g s from now.\n' % args.secs)

    try:
        os.nice(5)
    except OSError:
        pass

    auto = []
    if args.auto:
        if args.run == 'live':
            sys.exit('--auto is a dry rehearsal aid; a live run uses the button')
        t_p, t_r = (float(x) for x in args.auto.split(','))
        auto = [(1.0 + t_p, b'!', 'pause (!)'), (1.0 + t_r, b'~', 'resume (~)')]
        print('unattended: ! at +%g s and ~ at +%g s after the job starts'
              % (t_p, t_r))

    pads = Pads()
    print('FIRE line is %s at idle'
          % ('driven' if pads.fire_is_driven() else 'high impedance'))
    marks = []
    try:
        base, events, worst, motion = sample(pads, args.secs, grbl, lines,
                                             marks, auto)
    finally:
        pads.close()

    report(base, events, worst, args, marks, motion)
    print('\nfinal: state=%s  kernel=%s  interlock_circuit=%s  faults=%s'
          % (grbl.state(), sysfs('state'), sysfs('interlock_circuit'),
             sysfs('faults')))

    if args.json:
        with open(args.json, 'w') as f:
            json.dump({'baseline': base, 'worst_gap_s': worst,
                       'live': args.run == 'live', 'mode': args.mode,
                       'power': args.power, 'feed': args.feed,
                       'marks': marks, 'motion': motion,
                       'events': [(t, c) for t, c, _s in events]}, f, indent=1)
        print('wrote %s' % args.json)


if __name__ == '__main__':
    main()
