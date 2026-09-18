#!/usr/bin/env python3
# Copyright 2026 514 LLC d/b/a OpenGlow
# Written by Scott Wiederhold
# https://community.openglow.org
# SPDX-License-Identifier:    MIT
"""Extract motion profiles from Glowforge pulse files.

Decodes a factory .puls byte stream (one byte per machine tick) and reports
the velocity/acceleration profile the factory planner actually produced:
peak axis/vector speeds, acceleration ramp slopes, and per-move segments.
Used to derive factory-true grblHAL settings ($110/$111 max rate, $120/$121
accel) for the ForgeFIRM step backend (milestone 2, motion quality).

Accepts either a raw header-stripped stream (a bare .puls body) or a
full download with the GF1 header (magic at [1:4], total header length at
[4:8], then 8-byte key/value records). Header settings (STfr, XSmm) override
the --rate/--mode defaults when present.

Byte layout (docs.forgefirm.org, the step engine page; hardware-verified):
  bit7 set        -> laser power byte (low 7 bits = power, no steps this tick)
  bit0 X_STEP, bit1 X_DIR (set = -X)
  bit2 Y_STEP, bit3 Y_DIR (set = +Y)
  bit5 Z_STEP, bit6 Z_DIR (set = +Z, lens up)
  bit4 LASER_EN
"""
import argparse
import json
import math
import sys

MM_PER_FULL_STEP = 0.15         # X/Y, hardware-verified
MM_PER_HALF_STEP_Z = 0.3534     # Z, hardware-verified


def parse_header(data: bytes):
    """Return (settings dict, pulse-data offset). Empty dict if headerless."""
    if len(data) > 8 and data[1:4] == b'GF1':
        total = int.from_bytes(data[4:8], 'little')
        hdr = {}
        pos = 8
        while pos + 8 <= total:
            key = data[pos:pos + 4].decode('ascii', 'replace')
            hdr[key] = int.from_bytes(data[pos + 4:pos + 8], 'little')
            pos += 8
        return hdr, total
    return {}, 0


def decode(data: bytes, offset: int):
    """Per-tick signed step deltas and laser state."""
    n = len(data) - offset
    dx = [0] * n
    dy = [0] * n
    dz = [0] * n
    laser = [0] * n
    power = []                  # (tick, power value)
    for i in range(n):
        b = data[offset + i]
        if b & 0x80:
            power.append((i, b & 0x7F))
            continue
        if b & 0x01:
            dx[i] = -1 if b & 0x02 else 1
        if b & 0x04:
            dy[i] = 1 if b & 0x08 else -1
        if b & 0x20:
            dz[i] = 1 if b & 0x40 else -1
        laser[i] = (b >> 4) & 1
    return dx, dy, dz, laser, power


def velocity(deltas, rate: float, mm_per_step: float, win: int):
    """Signed mm/s via centered window difference of the cumulative count."""
    n = len(deltas)
    cum = [0] * (n + 1)
    for i, d in enumerate(deltas):
        cum[i + 1] = cum[i] + d
    v = [0.0] * n
    scale = mm_per_step * rate / (2 * win)
    for i in range(n):
        lo = max(0, i - win)
        hi = min(n, i + win)
        v[i] = (cum[hi] - cum[lo]) * mm_per_step * rate / (hi - lo)
    return v, cum[n]


def ramps(speed, rate: float, win: int, vmax: float):
    """Find sustained accel/decel ramps; return list of fitted slopes (mm/s^2).

    A ramp = a maximal run where speed changes monotonically (within window
    noise) by at least 25% of vmax. Slope from least-squares fit over the run.
    """
    if vmax <= 0:
        return []
    n = len(speed)
    out = []
    i = 0
    step = max(1, win // 4)     # sample coarsely; windows overlap anyway
    idx = list(range(0, n, step))
    k = 1
    while k < len(idx):
        j0 = k - 1
        rising = speed[idx[k]] > speed[idx[k - 1]]
        while k < len(idx) and (speed[idx[k]] > speed[idx[k - 1]]) == rising \
                and abs(speed[idx[k]] - speed[idx[k - 1]]) > 1e-9:
            k += 1
        seg = idx[j0:k]
        dv = speed[seg[-1]] - speed[seg[0]]
        if len(seg) >= 3 and abs(dv) >= 0.25 * vmax:
            ts = [s / rate for s in seg]
            vs = [speed[s] for s in seg]
            tm = sum(ts) / len(ts)
            vm = sum(vs) / len(vs)
            num = sum((t - tm) * (v - vm) for t, v in zip(ts, vs))
            den = sum((t - tm) ** 2 for t in ts)
            if den > 0:
                out.append(num / den)
        if k < len(idx) and idx[k] == seg[-1]:
            continue
        k += 1
    return out


def segments(speed, rate: float, thresh: float = 0.5):
    """Split into moves separated by >=20 ms of near-zero speed."""
    n = len(speed)
    gap = int(0.02 * rate)
    moves = []
    i = 0
    while i < n:
        while i < n and speed[i] <= thresh:
            i += 1
        if i >= n:
            break
        start = i
        quiet = 0
        while i < n and quiet < gap:
            quiet = quiet + 1 if speed[i] <= thresh else 0
            i += 1
        end = i - quiet
        moves.append((start, end, max(speed[start:end])))
    return moves


def analyze(path: str, rate: float, mode: int, win_ms: float, dump_csv: str):
    data = open(path, 'rb').read()
    hdr, offset = parse_header(data)
    if hdr:
        rate = hdr.get('STfr', rate)
        mode = hdr.get('XSmm', mode)
    mm_us = MM_PER_FULL_STEP / mode
    win = max(8, int(win_ms / 1000 * rate))

    dx, dy, dz, laser, power = decode(data, offset)
    n = len(dx)
    vx, netx = velocity(dx, rate, mm_us, win)
    vy, nety = velocity(dy, rate, mm_us, win)
    speed = [math.hypot(a, b) for a, b in zip(vx, vy)]
    vmax = max(speed) if speed else 0.0

    rx = ramps([abs(v) for v in vx], rate, win, max(abs(v) for v in vx) if vx else 0)
    ry = ramps([abs(v) for v in vy], rate, win, max(abs(v) for v in vy) if vy else 0)
    rv = ramps(speed, rate, win, vmax)

    zticks = [i for i, d in enumerate(dz) if d]
    zint = [(b - a) / rate * 1000 for a, b in zip(zticks, zticks[1:])]

    moves = segments(speed, rate)

    rep = {
        'file': path,
        'ticks': n,
        'rate_hz': rate,
        'duration_s': round(n / rate, 3),
        'microstep_mode': mode,
        'header': {k: hdr[k] for k in ('STfr', 'XSmm', 'HAxr', 'HAyr', 'HAar',
                                       'XSrc', 'YSrc') if k in hdr} or None,
        'net_travel_mm': {'x': round(netx * mm_us, 3), 'y': round(nety * mm_us, 3)},
        'steps': {'x': sum(1 for d in dx if d), 'y': sum(1 for d in dy if d),
                  'z': len(zticks)},
        'peak_speed_mm_s': {'x': round(max((abs(v) for v in vx), default=0), 2),
                            'y': round(max((abs(v) for v in vy), default=0), 2),
                            'vector': round(vmax, 2)},
        'accel_mm_s2': {
            'x_ramps': [round(a) for a in rx],
            'y_ramps': [round(a) for a in ry],
            'vector_ramps': [round(a) for a in rv],
        },
        'z_step_interval_ms': {
            'min': round(min(zint), 2) if zint else None,
            'median': round(sorted(zint)[len(zint) // 2], 2) if zint else None,
        },
        'laser_on_ticks': sum(laser),
        'power_bytes': power[:8],
        'moves': [{'t0_s': round(a / rate, 3), 't1_s': round(b / rate, 3),
                   'peak_mm_s': round(p, 2)} for a, b, p in moves[:20]],
        'move_count': len(moves),
    }

    if dump_csv:
        stride = max(1, win // 4)
        with open(dump_csv, 'w') as f:
            f.write('t_s,vx_mm_s,vy_mm_s,speed_mm_s\n')
            for i in range(0, n, stride):
                f.write('%.4f,%.2f,%.2f,%.2f\n' % (i / rate, vx[i], vy[i], speed[i]))
    return rep


def main():
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument('files', nargs='+')
    ap.add_argument('--rate', type=float, default=10000,
                    help='machine tick Hz for headerless files (default 10000)')
    ap.add_argument('--mode', type=int, default=8,
                    help='X/Y microstep mode for headerless files (default 8)')
    ap.add_argument('--win-ms', type=float, default=8.0,
                    help='velocity window half-width in ms (default 8)')
    ap.add_argument('--csv', help='dump velocity profile CSV (single file only)')
    args = ap.parse_args()
    for path in args.files:
        rep = analyze(path, args.rate, args.mode, args.win_ms,
                      args.csv if len(args.files) == 1 else None)
        json.dump(rep, sys.stdout, indent=2)
        print()


if __name__ == '__main__':
    main()
