#!/usr/bin/env python3
# Copyright 2026 514 LLC d/b/a OpenGlow
# Written by Scott Wiederhold
# https://community.openglow.org
# SPDX-License-Identifier:    MIT
"""Bench drill for the flow-check suspicion/confirmation state machine.

Runs ON THE BOARD (scp it over, then: python3 flow_confirm_drill.py).
One continuous M8 session walks the driver through every verdict
transition using real pump-off transients - the same mechanism as a
genuine failure, no threshold games:

  check 1  pump on   -> verified             (baseline behavior)
           pump OFF
  check 2  pump off  -> COOLANT FLOW SUSPECT (+ immediate re-check)
           pump ON
  check 3  pump on   -> suspicion cleared    (transient disproven)
           pump OFF
  check 4  pump off  -> COOLANT FLOW SUSPECT
  check 5  pump off  -> COOLANT FLOW FAULT   (consecutive, confirmed;
           pump ON                            or the starved-re-check
                                              escalation if the loop
                                              cannot settle in time)
  check 6  pump on   -> flow recovered

Prints PASS/FAIL per expectation and DRILL COMPLETE at the end.
Leaves the machine idle: M9 sent, pump on, heater off.
"""
import select
import socket
import time

VERDICT_MARKS = ('flow verified', 'FLOW SUSPECT', 'FLOW FAULT',
                 'suspicion cleared', 'flow recovered')

#           expected substring    action after that verdict
PLAN = [
    ('flow verified',     'pump_off'),
    ('FLOW SUSPECT',      'pump_on'),
    ('suspicion cleared', 'pump_off'),
    ('FLOW SUSPECT',      None),
    ('FLOW FAULT',        'pump_on'),
    ('flow recovered',    'done'),
]

VERDICT_TIMEOUT_S = 720
T0 = time.monotonic()


def el():
    return time.monotonic() - T0


def log(msg):
    print('%7.1f  %s' % (el(), msg), flush=True)


def pump(on):
    with open('/sys/glowforge/thermal/water_pump_on', 'w') as f:
        f.write('1' if on else '0')
    log('** pump -> %s' % ('ON' if on else 'OFF'))


def temps():
    import math
    adc_f = 1024.0 * 1.3
    rinf = 10000.0 * math.exp(-3380.0 / 298.15)

    from gfbench import degc
    with open('/sys/glowforge/pic/water_temp_1') as f:
        d = degc(f.read())
    with open('/sys/glowforge/pic/water_temp_2') as f:
        u = degc(f.read())
    return d, u


s = socket.create_connection(('127.0.0.1', 23), timeout=5)
s.setblocking(False)
buf = b''
results = []


def send(cmd):
    log('>> ' + cmd)
    s.sendall(cmd.encode() + b'\n')


def next_verdict(deadline):
    """Pump the socket until a verdict line arrives or deadline."""
    global buf
    nxt_temps = el()
    while el() < deadline:
        if el() >= nxt_temps:
            nxt_temps += 15.0
            d, u = temps()
            log('   down=%6.2f up=%6.2f' % (d, u))
        r, _, _ = select.select([s], [], [], 0.2)
        if r:
            data = s.recv(4096)
            if data:
                buf += data
            while b'\n' in buf:
                line, buf = buf.split(b'\n', 1)
                line = line.strip().decode('ascii', 'replace')
                if not line.startswith('[MSG'):
                    continue
                log('<< ' + line)
                if any(m in line for m in VERDICT_MARKS):
                    return line
    return None


try:
    time.sleep(0.5)
    pump(True)
    log('--- M8: session start')
    send('M8')

    for i, (expect, action) in enumerate(PLAN):
        line = next_verdict(el() + VERDICT_TIMEOUT_S)
        if line is None:
            results.append((expect, None))
            log('!! TIMEOUT waiting for verdict %d (%s)' % (i + 1, expect))
            break
        ok = expect in line
        results.append((expect, line if ok else 'GOT: ' + line))
        log('   verdict %d %s (expected %s)' % (i + 1, 'PASS' if ok else 'FAIL', expect))
        if not ok:
            break
        if action == 'pump_off':
            pump(False)
        elif action == 'pump_on':
            pump(True)
        elif action == 'done':
            break
finally:
    try:
        s.sendall(b'M9\n')
        time.sleep(1.0)
    except OSError:
        pass
    s.close()
    pump(True)
    with open('/sys/glowforge/thermal/heater_pwm', 'w') as f:
        f.write('0')
    log('--- M9 sent, pump on, heater off')

print('', flush=True)
passed = sum(1 for e, got in results if got is not None and e in got)
for i, (expect, got) in enumerate(results):
    print('  %d. %-18s %s' % (i + 1, expect, 'PASS' if (got and expect in got) else (got or 'TIMEOUT')), flush=True)
print('DRILL %s (%d/%d)' % ('COMPLETE' if passed == len(PLAN) else 'FAILED', passed, len(PLAN)), flush=True)
