#!/usr/bin/env python3
# Copyright 2026 514 LLC d/b/a OpenGlow
# Written by Scott Wiederhold
# https://community.openglow.org
# SPDX-License-Identifier:    MIT
"""Bench drill for the starved-re-check escalation path.

Runs ON THE BOARD with the controller running. The cooling engine's
confirmation budget (`cool_confirm_max_s`, re-read at every run start)
is set to a short value for the drill through forgectrl's settings and
restored afterward. With the pump off, the job-start check reads
over-limit -> SUSPECT; the cooked stagnant loop then cannot pass the
settle gate within the budget, so it expires and the engine must
escalate: "COOLANT FLOW FAULT: no clean re-check within N s".

Usage: flow_escalate_drill.py [budget_s]   (default 60, the setting's minimum)
Prints PASS/FAIL and leaves the machine idle: M9 sent, pump on,
heater off, the budget setting as it was.
"""
import select
import socket
import sys
import time

from gfbench import forgectrl_post, setting

BUDGET_S = int(sys.argv[1]) if len(sys.argv) > 1 else 60
T0 = time.monotonic()


def el():
    return time.monotonic() - T0


def log(msg):
    print('%7.1f  %s' % (el(), msg), flush=True)


def pump(on):
    with open('/sys/glowforge/thermal/water_pump_on', 'w') as f:
        f.write('1' if on else '0')
    log('** pump -> %s' % ('ON' if on else 'OFF'))


s = socket.create_connection(('127.0.0.1', 23), timeout=5)
s.setblocking(False)
buf = b''


def wait_msg(substrs, deadline):
    global buf
    while el() < deadline:
        r, _, _ = select.select([s], [], [], 0.2)
        if not r:
            continue
        data = s.recv(4096)
        if data:
            buf += data
        while b'\n' in buf:
            line, buf = buf.split(b'\n', 1)
            line = line.strip().decode('ascii', 'replace')
            if line.startswith('[MSG'):
                log('<< ' + line)
                for m in substrs:
                    if m in line:
                        return line
    return None


budget_was = setting('cool_confirm_max_s')       # None = not set (compiled default)
st, body = forgectrl_post('/settings', data={'cool_confirm_max_s': str(BUDGET_S)})
if st != 200:
    log('!! could not set cool_confirm_max_s=%d (POST /settings -> %s %s)' % (BUDGET_S, st, body))
    s.close()
    sys.exit(2)
log('cool_confirm_max_s=%d for the drill (was %s)' % (BUDGET_S, budget_was or 'unset'))

ok = True
try:
    time.sleep(0.5)
    pump(False)
    log('--- M8 with the pump off')
    s.sendall(b'M8\n')

    if wait_msg(['FLOW SUSPECT'], el() + 180) is None:
        log('!! no SUSPECT within 180 s')
        ok = False
    elif wait_msg(['no clean re-check within'], el() + BUDGET_S + 75) is None:
        log('!! no escalation FAULT within %d s of the suspect' % (BUDGET_S + 75))
        ok = False
finally:
    try:
        if budget_was is None:
            st, _ = forgectrl_post('/settings', params={'cool_confirm_max_s': ''})
        else:
            st, _ = forgectrl_post('/settings', data={'cool_confirm_max_s': str(budget_was)})
        log('cool_confirm_max_s restored to %s (-> %s)' % (budget_was or 'unset', st))
    except OSError as e:
        log('!! could not restore cool_confirm_max_s: %s' % e)
        ok = False
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

print('ESCALATION DRILL %s' % ('COMPLETE' if ok else 'FAILED'), flush=True)
sys.exit(0 if ok else 1)
