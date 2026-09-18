#!/usr/bin/env python3
# Copyright 2026 514 LLC d/b/a OpenGlow
# Written by Scott Wiederhold
# https://community.openglow.org
# SPDX-License-Identifier:    MIT
"""Characterize a SHORT periodic flow re-check for use DURING a job.

Why a separate design from the job-start check: with the laser firing
and coolant flowing, tube heat raises BOTH sensors, inflating a plain
downstream-rise metric toward a false fault. With the pump stopped that
heat never reaches the sensors at all (stagnant loop), so the no-flow
signature stays at its idle value. The two signatures therefore
converge during a cut. The differential rise

    (downstream_end - downstream_start) - (upstream_end - upstream_start)

cancels that common-mode heating and is the metric this measures.

TWO DEAD ENDS, recorded so they are not re-invented:

1. Absolute over-temperature does NOT detect a failed pump. The
   sensors read the water at their own location; with no circulation
   the tube's heat stays in the tube and never reaches them. The loop
   can read perfectly comfortable while the tube cooks. Over-temp
   monitoring detects a hot CIRCULATING loop - a different failure.

2. "The coolant should warm up while cutting" is NOT a usable
   indicator either. A low-duty engrave (say 5% duty at 30% power) can
   put so little heat in that the cooling system absorbs it with no
   measurable rise, so a flat trend is equally consistent with a light
   load working correctly and with a dead pump. Ambiguous evidence is
   worse than none - it invites false confidence.

Hence: periodic ACTIVE interrogation with the heater, which creates a
known stimulus instead of waiting for one, is the only valid method on
this hardware (there is no pump tach or pump current sense anywhere in
the machine).

Usage: flow_recheck_char.py [heater_pct] [window_s]   (default 50 30)
Runs both flow and no-flow cases from a comparable loop state and
prints the differential separation. Aborts if downstream passes 45 C.
Drives the heater and pump directly: run with forgectrl and the
controller stopped (the bench page's takeover does that). Runs on the
board or from a host (gfbench: GF_HOST).
"""
import sys
import time

from gfbench import board, degc

DOWN_ABORT_C = 45.0


def sample():
    o = board('cat /sys/glowforge/pic/water_temp_1 /sys/glowforge/pic/water_temp_2').split()
    return degc(int(o[0])), degc(int(o[1]))


def settle(seconds=100):
    board('echo 1 > /sys/glowforge/thermal/water_pump_on; echo 0 > /sys/glowforge/thermal/heater_pwm')
    time.sleep(seconds)


def check(tag, pump_on, pct, window):
    board('echo %d > /sys/glowforge/thermal/water_pump_on' % (1 if pump_on else 0))
    time.sleep(2)
    d0, u0 = sample()
    board('echo %d > /sys/glowforge/thermal/heater_pwm' % int(65535 * pct / 100))
    t0 = time.time()
    d, u = d0, u0
    while time.time() - t0 < window:
        time.sleep(5)
        d, u = sample()
        print('    %-8s t=%2.0fs  down=%5.2f (%+5.2f)  up=%5.2f (%+5.2f)  diff=%+5.2f'
              % (tag, time.time() - t0, d, d - d0, u, u - u0, (d - d0) - (u - u0)), flush=True)
        if d >= DOWN_ABORT_C:
            print('    abort: downstream at safety limit', flush=True)
            break
    board('echo 0 > /sys/glowforge/thermal/heater_pwm; echo 1 > /sys/glowforge/thermal/water_pump_on')
    return (d - d0) - (u - u0), d - d0


pct = int(sys.argv[1]) if len(sys.argv) > 1 else 50
window = int(sys.argv[2]) if len(sys.argv) > 2 else 30

print('=== periodic re-check characterization: %d%% heater, %d s window' % (pct, window))
settle()
print('  flow case:')
flow_diff, flow_rise = check('flow', True, pct, window)
settle()
print('  no-flow case:')
noflow_diff, noflow_rise = check('noflow', False, pct, window)
settle(60)

print()
print('differential rise:  flow %+.2f   no-flow %+.2f   separation %.2f C'
      % (flow_diff, noflow_diff, noflow_diff - flow_diff))
print('plain down rise:    flow %+.2f   no-flow %+.2f   separation %.2f C'
      % (flow_rise, noflow_rise, noflow_rise - flow_rise))
print('suggested differential threshold: %.2f C' % ((flow_diff + noflow_diff) / 2))
