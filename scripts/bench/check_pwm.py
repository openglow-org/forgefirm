#!/usr/bin/env python3
# Copyright 2026 514 LLC d/b/a OpenGlow
# Written by Scott Wiederhold
# https://community.openglow.org
# SPDX-License-Identifier:    MIT
"""Software check: laser PWM carrier frequency from PWM2 registers.

Expected with the fsl,extra-prescale=<13> fix: PWMCR prescaler divider = 13,
PWMPR ~125 (127 counts - 2), effective carrier = perclk / (13 * 127) ~= 40 kHz.
Without the fix: divider 1 -> ~520 kHz. Safe: read-only register
inspection; the laser PWM output feeds the PSU power input, firing stays gated
by the hardware chain. The definitive gate remains the scope on LASER_PWM.
"""
import mmap, struct

PWM2_BASE = 0x02084000
PERCLK_HZ = 66_000_000  # ipg_high; cross-check against the EPIT rate in dmesg

with open("/dev/mem", "rb") as f:
    m = mmap.mmap(f.fileno(), 4096, mmap.MAP_SHARED, mmap.PROT_READ,
                  offset=PWM2_BASE)
    cr, sr, ir, sar, pr = struct.unpack("<5I", m[:20])
    m.close()

prescaler_field = (cr >> 4) & 0xFFF
divider = prescaler_field + 1
period_counts = pr + 2
freq = PERCLK_HZ / (divider * period_counts) if period_counts else 0
enabled = cr & 1

print(f"PWMCR=0x{cr:08x}  PWMSAR={sar}  PWMPR={pr}  enabled={bool(enabled)}")
print(f"prescaler divider = {divider} (field {prescaler_field})")
print(f"period counts     = {period_counts}")
print(f"carrier frequency = {freq/1000:.2f} kHz")
ok = divider == 13 and 120 <= period_counts <= 135 and 38_000 <= freq <= 42_000
print(f"\n{'PASS' if ok else 'FAIL'}: expected divider 13, ~127 counts, ~40 kHz")
