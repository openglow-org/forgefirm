#!/usr/bin/env python3
# Copyright 2026 514 LLC d/b/a OpenGlow
# Written by Scott Wiederhold
# https://community.openglow.org
# SPDX-License-Identifier:    MIT
"""End-of-data protocol bench.

Motion-safe by construction: all four motors are locked via motor_lock (the
SDMA script masks the STEP bits), the laser latch is locked (LASER_ON pin is
Hi-Z and the hardware safety chain is open anyway), and only NOP (0x00) and
laser-bit (0x10) bytes are streamed.

Exercises: normal completion, underrun detection/ack, parked no-replay
guard, alldone GPIO clear (via GPIO2_DR readback - the data register
retains the last value the script wrote), resume(0), continuous-feed
stability, and 20x run/underrun cycling (wedge check).
"""
import os, re, sys, time, mmap, struct

C = "/sys/glowforge/cnc"
DEV = "/dev/glowforge"
GPIO2_BASE = 0x020A0000  # GPIO2 DR at offset 0

passed = failed = 0
def chk(name, cond, detail=""):
    global passed, failed
    tag = "PASS" if cond else "FAIL"
    if cond: passed += 1
    else: failed += 1
    print(f"{tag}: {name} [{detail}]", flush=True)

def rd(attr):
    with open(f"{C}/{attr}") as f: return f.read().strip()
def wr(attr, val):
    with open(f"{C}/{attr}", "w") as f: f.write(str(val))
def state(): return rd("state")

def gpio2_dr():
    with open("/dev/mem", "rb") as f:
        m = mmap.mmap(f.fileno(), 4096, mmap.MAP_SHARED, mmap.PROT_READ,
                      offset=GPIO2_BASE)
        v = struct.unpack("<I", m[:4])[0]
        m.close()
        return v

def sc(ctx, n):
    return int(re.search(rf"sc{n}=([0-9a-f]{{8}})", ctx).group(1), 16)

def wait_state(target, timeout=15):
    t0 = time.time()
    while time.time() - t0 < timeout:
        s = state()
        if s == target: return s
        time.sleep(0.05)
    return state()

# ---- setup: everything locked ----
wr("laser_latch", 1)   # LASER_ON pin Hi-Z
wr("motor_lock", 15)   # X|Y1|Y2|Z locked: no motion regardless of data
if state() == "disabled":
    wr("enable", 1)
    time.sleep(0.5)
SEC = int(rd("step_freq"))  # bytes per second of playback
print(f"setup: state={state()} step_freq={SEC} underruns={rd('underruns')}", flush=True)

fd = os.open(DEV, os.O_WRONLY)  # exclusive open, held for the whole bench

def clear_data():
    os.lseek(fd, 1, os.SEEK_SET)  # clear pulse data + byte counters
def feed(b, n):
    return os.write(fd, bytes([b]) * n)

# ---- T1: normal completion (streaming=0) -> idle ----
wr("streaming", 0)
clear_data()
feed(0x00, 2 * SEC)
wr("run", 1)
chk("T1 run starts", state() == "running", state())
s = wait_state("idle", 10)
chk("T1 normal completion -> idle", s == "idle", s)

# ---- T2: underrun (streaming=1): state, counter, run refused ----
u0 = int(rd("underruns"))
wr("streaming", 1)
clear_data()
feed(0x00, 1 * SEC)
wr("run", 1)
s = wait_state("underrun", 10)
chk("T2 underrun state entered", s == "underrun", s)
chk("T2 underrun counter incremented", int(rd("underruns")) == u0 + 1, rd("underruns"))
try:
    wr("run", 1)
    chk("T2 run refused while unacked", False, "run was accepted!")
except OSError as e:
    chk("T2 run refused while unacked", True, str(e))

# ---- T3: parked no-replay guard (M4): append while parked, counters frozen ----
ctx_a = rd("sdma_context")
feed(0x01, 1000)  # X-step bytes appended while parked (motors locked anyway)
time.sleep(1.0)
ctx_b = rd("sdma_context")
chk("T3 byte counter frozen while parked", sc(ctx_a, 3) == sc(ctx_b, 3),
    f"sc3 {sc(ctx_a,3)} -> {sc(ctx_b,3)}")
chk("T3 head frozen while parked", sc(ctx_a, 4) == sc(ctx_b, 4),
    f"sc4 {sc(ctx_a,4)} -> {sc(ctx_b,4)}")
wr("stop", 1)
chk("T3 stop acks underrun -> idle", state() == "idle", state())
clear_data()

# ---- T4: alldone clears laser+step GPIO bits (M2) ----
wr("streaming", 1)
feed(0x10, 1 * SEC)  # laser bit set, no steps
wr("run", 1)
s = wait_state("underrun", 10)
chk("T4 underrun after laser-bit stream", s == "underrun", s)
time.sleep(0.2)
dr = gpio2_dr()
chk("T4 LASER_ENABLE/LASER_ON_HEAD bits low in GPIO2_DR", (dr >> 30) == 0,
    f"DR=0x{dr:08x}")
chk("T4 STEP bits low in GPIO2_DR",
    (dr & ((1 << 20) | (1 << 21) | (1 << 22) | (1 << 29))) == 0, f"DR=0x{dr:08x}")
wr("stop", 1)
clear_data()

# ---- T5: resume(0) completes instead of wedging (M3) ----
wr("streaming", 0)
feed(0x00, SEC // 2)
wr("resume", 0)
chk("T5 resume(0) starts", state() == "running", state())
s = wait_state("idle", 10)
chk("T5 resume(0) completes -> idle (no wedge)", s == "idle", s)
clear_data()

# ---- T6: continuous feed never underruns; stopping the feed does ----
wr("streaming", 1)
feed(0x00, SEC)
wr("run", 1)
t0 = time.time(); ok = True
while time.time() - t0 < 5:
    feed(0x00, SEC // 4)
    if state() != "running":
        ok = False
        break
    time.sleep(0.2)
chk("T6 no underrun while feeding (5 s)", ok, state())
s = wait_state("underrun", 10)
chk("T6 underrun after feed stops", s == "underrun", s)
wr("stop", 1)
clear_data()

# ---- T7: 20x run/underrun cycles, no wedge (M3) ----
anomalies = 0
for i in range(20):
    feed(0x00, SEC // 10)
    wr("run", 1)
    s = wait_state("underrun", 5)
    if s != "underrun":
        anomalies += 1
        print(f"  cycle {i}: state={s}", flush=True)
    wr("stop", 1)
chk("T7 20 run/underrun cycles clean", anomalies == 0, f"{anomalies} anomalies")

# ---- wrap up ----
wr("streaming", 0)
clear_data()
os.close(fd)
wr("motor_lock", 0)
print(f"\nRESULT: {passed} passed, {failed} failed; underruns total={rd('underruns')}",
      flush=True)
sys.exit(1 if failed else 0)
