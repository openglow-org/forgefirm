#!/usr/bin/env python3
# Copyright 2026 514 LLC d/b/a OpenGlow
# Written by Scott Wiederhold
# https://community.openglow.org
# SPDX-License-Identifier:    MIT
"""Charge-pump watchdog timing (runs ON the board, as root).

Measures the HV watchdog one-shot (U1-1, SN74AHC123A) directly from the
SoC's own pins, with no kernel change and no scope:

  CHG_PUMP     GPIO3_24  the kernel's feed pulse (sub-microsecond, so it is
                         latched by GPIO3's edge detector, not caught live)
  !Q readback  GPIO1_08  inverted one-shot Q (`cnc/charge_pump_alive`)
  !HV_ENABLE   GPIO4_06  the `hv_enable` switch pad, inverted

The sampler arms rising-edge detection for GPIO3 pin 24 only (ICR2 bits
17:16), clears that pin's sticky ISR flag, then polls ISR bit 24 and the two
readback pads in a loop with a ~100 us sleep per pass, restoring ICR2 on
exit. GPIO3 IMR bit 24 must be clear (nothing has an interrupt on that pin;
the run refuses otherwise), so the kernel never sees the flag. Reads and
writes go through /dev/mem; the CHG_PUMP pad has SION set in the device
tree, so its pad state is visible. The loop must not hog the CPU: only the
controller's shipper thread is SCHED_FIFO, its protocol/producer thread is
SCHED_OTHER, and a busy loop starves it so a run never ends.

Every run of the pulse engine (a jog is enough) primes the pump once and
then feeds it every 200 ms while `state == running`; when the run ends the
feed stops and Q falls one one-shot period after the last pulse. Reported per
run: t_w = last latched pulse -> !Q rising (Q fell), the !HV_ENABLE edge
relative to Q, the priming latency (first pulse -> !Q falling), the pulse
count and period. Resolution = the loop period (tens of microseconds; the
worst gap is printed).

Usage: cp_watchdog_timing.py [seconds] [jog ...]
  default: 14 s, jogs "$J=G91 X5 F300" "$J=G91 X-5 F300" (out and back,
  5 mm, head ends where it started), sent to the local grblHAL on
  127.0.0.1:23 at t = 2 s and 6 s. Pass "-" as the only jog to sample
  without commanding motion (drive the runs yourself).
Motion only, laser locked; needs the GRBL controller idle with no other
Grbl client attached (a connection here displaces the sender).
"""
import mmap, os, socket, sys, time

GPIO1, GPIO3, GPIO4 = 0x0209C000, 0x020A4000, 0x020A8000
PSR, ICR2, IMR, ISR = 0x08, 0x10, 0x14, 0x18
PULSE_PIN = 24            # GPIO3_24 CHG_PUMP
NQ_BIT, NHV_BIT = 8, 6    # GPIO1_08 !Q, GPIO4_06 !HV_ENABLE
JOG_T0, JOG_DT = 2.0, 4.0
SLEEP_S = 0.0001

DEFAULT_JOGS = ['$J=G91 X5 F300', '$J=G91 X-5 F300']


def main():
    dur = float(sys.argv[1]) if len(sys.argv) > 1 else 14.0
    jogs = sys.argv[2:] if len(sys.argv) > 2 else DEFAULT_JOGS
    if jogs == ['-']:
        jogs = []

    fd = os.open('/dev/mem', os.O_RDWR | os.O_SYNC)

    def M(base):
        return mmap.mmap(fd, 4096, mmap.MAP_SHARED, mmap.PROT_READ | mmap.PROT_WRITE, offset=base)

    g1, g3, g4 = M(GPIO1), M(GPIO3), M(GPIO4)

    def rd(m, o):
        return int.from_bytes(m[o:o + 4], 'little')

    def wr(m, o, v):
        m[o:o + 4] = v.to_bytes(4, 'little')

    if rd(g3, IMR) & (1 << PULSE_PIN):
        print('ABORT: GPIO3 IMR bit %d is set - something has an interrupt on the pump pin' % PULSE_PIN)
        return 2

    def cnc_state():
        try:
            with open('/sys/glowforge/cnc/state') as f:
                return f.read().strip()
        except OSError:
            return '?'

    sock = None
    if jogs:
        sock = socket.create_connection(('127.0.0.1', 23), timeout=3)
        sock.settimeout(0.05)
        end = time.monotonic() + 1.5
        while time.monotonic() < end:      # drain the greeting
            try:
                sock.recv(4096)
            except socket.timeout:
                pass

    icr2_orig = rd(g3, ICR2)
    isr_clear = (1 << PULSE_PIN).to_bytes(4, 'little')
    events = []
    maxgap = n = 0
    try:
        wr(g3, ICR2, (icr2_orig & ~(0x3 << 16)) | (0x2 << 16))   # pin 24: rising edge
        wr(g3, ISR, 1 << PULSE_PIN)                               # clear the stale flag (w1c)
        nq_prev = (rd(g1, PSR) >> NQ_BIT) & 1
        nhv_prev = (rd(g4, PSR) >> NHV_BIT) & 1
        t0 = time.monotonic_ns()
        tend = t0 + int(dur * 1e9)
        tprev = t0
        sent = 0
        events.append((t0, 'start !Q=%d !HV=%d' % (nq_prev, nhv_prev)))
        while True:
            t = time.monotonic_ns()
            isr = g3[ISR:ISR + 4]
            p1 = g1[PSR:PSR + 4]
            p4 = g4[PSR:PSR + 4]
            if isr[3] & 0x01:                       # bit 24 -> byte 3 bit 0
                g3[ISR:ISR + 4] = isr_clear
                events.append((t, 'PULSE'))
            nq = p1[1] & 0x01                       # bit 8 -> byte 1 bit 0
            nhv = (p4[0] >> 6) & 0x01               # bit 6 -> byte 0 bit 6
            if nq != nq_prev:
                events.append((t, '!Q->%d' % nq))
                nq_prev = nq
                events.append((time.monotonic_ns(), 'cnc/state=' + cnc_state()))
            if nhv != nhv_prev:
                events.append((t, '!HV->%d' % nhv))
                nhv_prev = nhv
            gap = t - tprev
            if gap > maxgap:
                maxgap = gap
            tprev = t
            n += 1
            if sock and sent < len(jogs) and t - t0 > (JOG_T0 + JOG_DT * sent) * 1e9:
                sock.sendall((jogs[sent] + '\n').encode())
                events.append((t, 'JOG %s' % jogs[sent]))
                sent += 1
            if t > tend:
                break
            time.sleep(SLEEP_S)
    finally:
        wr(g3, ICR2, icr2_orig)
        wr(g3, ISR, 1 << PULSE_PIN)
        if sock:
            sock.close()

    print('samples=%d  mean period=%.1f us  worst gap=%.1f us' % (n, (tprev - t0) / max(n, 1) / 1e3, maxgap / 1e3))
    print()
    tw, prime, hvq, periods = [], [], [], []
    last_pulse = first_pulse = q_fall = None
    for t, k in events:
        line = '%9.4f  %s' % ((t - t0) / 1e9, k)
        if k == 'PULSE':
            if last_pulse is not None and first_pulse is not None and t - last_pulse < 1e9:
                periods.append((t - last_pulse) / 1e6)
            else:
                first_pulse = t
            last_pulse = t
        elif k == '!Q->0' and first_pulse is not None:
            prime.append((t - first_pulse) / 1e3)
            line += '   (Q rose %.1f us after the priming pulse)' % prime[-1]
        elif k == '!Q->1' and last_pulse is not None:
            tw.append((t - last_pulse) / 1e6)
            q_fall = t
            line += '   t_w = %.2f ms after the last pulse' % tw[-1]
            first_pulse = None
        elif k == '!HV->1' and q_fall is not None:
            hvq.append((t - q_fall) / 1e3)
            line += '   (%.1f us after Q fell)' % hvq[-1]
        print(line)
    print()
    if tw:
        print('one-shot period t_w: n=%d  mean %.2f ms  min %.2f  max %.2f' % (len(tw), sum(tw) / len(tw), min(tw), max(tw)))
    if periods:
        print('feed period: n=%d  mean %.3f ms  min %.3f  max %.3f' % (len(periods), sum(periods) / len(periods), min(periods), max(periods)))
    if prime:
        print('prime -> Q high: mean %.1f us (n=%d)' % (sum(prime) / len(prime), len(prime)))
    if hvq:
        print('Q fall -> HV_ENABLE fall: mean %.1f us (n=%d)' % (sum(hvq) / len(hvq), len(hvq)))
    return 0


if __name__ == '__main__':
    sys.exit(main())
