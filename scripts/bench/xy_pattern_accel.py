#!/usr/bin/env python3
"""The XY microstep modes by the head accelerometer, the machine silent (on the board).

Usage: xy_pattern_accel.py [modes...]        (default: 8 16 32)

The pattern, from home at full speed (F12000), one leg at a time:

  1. home to X 18 in, Y 9 in        (457.2, 228.6 mm)
  2. to X 9 in, Y 9 in              (228.6, 228.6 mm)
  3. a 9 in circle from its mid-bottom, the center 4.5 in toward the
     back, ending back at X 9 in, Y 9 in
  4. to X 9 in, Y 0                 (228.6, 0 mm)
  5. to X 0, Y 0                    home

Per mode: stores xy_microsteps through forgectrl (which restarts the idle
GRBL controller), waits for the controller at that mode, takes the
engine's quiet hold (POST /cool/quiet: air assist, exhaust, intake and
purge fans, the coolant pump and the TEC all off, so the machine is silent
and the modes can be heard as well as measured; a dry pattern, the laser
latched), waits the fixed QUIET_S, then samples the head accelerometer
(the crash watch's LIS2HH12 on i2c-3, straight over the bus at 800 Hz)
through the pattern. Reports, per leg and overall, the cruise-window
RMS and peak-to-peak of X and Y with the mean removed, the leg times,
the kernel counters against home, and cnc/underruns; JSON with the raw
trace to the bench data directory. Releases the hold and puts the
accelerometer back on every exit path. Ends at x8.

Needs the controller in GRBL mode, homed and standing at home, the lid
closed, no other Grbl client, and the bed clear across the pattern.
"""
import ctypes
import fcntl
import json
import math
import os
import re
import socket
import struct
import sys
import threading
import time

from gfbench import data_path, forgectrl_get, forgectrl_post

def modes_from_argv(argv):
    """The modes named on the command line ("8 16 32" as one argument or
    several), the three by default."""
    return [int(a) for arg in argv for a in arg.split()] or [8, 16, 32]


FEED = 12000
IN = 25.4
LEGS = [("home to (18, 9) in", "G1 X%.3f Y%.3f F%d" % (18 * IN, 9 * IN, FEED)),
        ("to (9, 9) in", "G1 X%.3f F%d" % (-9 * IN, FEED)),
        ("9 in circle", "G2 X0 Y0 I0 J%.3f F%d" % (-4.5 * IN, FEED)),
        ("to (9, 0) in", "G1 Y%.3f F%d" % (-9 * IN, FEED)),
        ("to home", "G1 X%.3f F%d" % (-9 * IN, FEED))]
QUIET_S = 10.0                          # the fixed wait after every fan is commanded off
TRIM_S = 0.35                           # the ramps at each end of a leg, outside the cruise window
TOL_MM = 0.05
SYS = "/sys/glowforge/"

I2C_DEV = "/dev/i2c-3"
I2C_SLAVE_FORCE = 0x0706
I2C_SMBUS = 0x0720
I2C_SMBUS_READ = 1
I2C_SMBUS_I2C_BLOCK_DATA = 8
ADDR = 0x1E
CTRL1 = 0x20
CTRL1_RUN = 0x6F                        # 800 Hz, BDU, XYZ on (the crash watch's run value)
CTRL4 = 0x23
OUT_X_L = 0x28


class SmbusData(ctypes.Union):
    _fields_ = [("byte", ctypes.c_uint8), ("word", ctypes.c_uint16), ("block", ctypes.c_uint8 * 34)]


class SmbusIoctl(ctypes.Structure):
    _fields_ = [("read_write", ctypes.c_uint8), ("command", ctypes.c_uint8),
                ("size", ctypes.c_uint32), ("data", ctypes.POINTER(SmbusData))]


class Accel:
    """The chip over the bus. The six output registers come in ONE bus
    transaction (an SMBus block read), so another reader on the bus (the
    crash watch) cannot slip a register pointer between the write and
    the read."""

    def __init__(self):
        self.fd = os.open(I2C_DEV, os.O_RDWR)
        fcntl.ioctl(self.fd, I2C_SLAVE_FORCE, ADDR)
        self.ctrl1_found = self.reg(CTRL1)
        self.ctrl4 = self.reg(CTRL4)

    def reg(self, r):
        os.write(self.fd, bytes([r]))
        return os.read(self.fd, 1)[0]

    def set_reg(self, r, v):
        os.write(self.fd, bytes([r, v]))

    def start(self):
        self.set_reg(CTRL1, CTRL1_RUN)
        time.sleep(0.02)

    def restore(self):
        try:
            self.set_reg(CTRL1, self.ctrl1_found)
        finally:
            os.close(self.fd)

    def read(self):
        data = SmbusData()
        data.block[0] = 6
        req = SmbusIoctl(I2C_SMBUS_READ, OUT_X_L, I2C_SMBUS_I2C_BLOCK_DATA, ctypes.pointer(data))
        fcntl.ioctl(self.fd, I2C_SMBUS, req)
        return struct.unpack("<hhh", bytes(data.block[1:7]))


class Sampler(threading.Thread):
    def __init__(self, accel):
        super().__init__(daemon=True)
        self.accel = accel
        self.samples = []               # (t, x, y, z)
        self.errors = 0
        self._halt = threading.Event()

    def run(self):
        while not self._halt.is_set():
            try:
                x, y, z = self.accel.read()
                self.samples.append((time.monotonic(), x, y, z))
            except OSError:
                self.errors += 1
                time.sleep(0.002)

    def stop(self):
        self._halt.set()
        self.join(2.0)


def sysfs(attr):
    with open(SYS + attr) as f:
        return f.read().strip()


def fc_get(path):
    st, body = forgectrl_get(path)
    return body if st == 200 and isinstance(body, dict) else {}


def fail(msg):
    print("FAIL: %s" % msg)
    raise SystemExit(1)


def set_mode(mode):
    if mode == 8:
        st, body = forgectrl_post("/settings", params={"xy_microsteps": ""})
    else:
        st, body = forgectrl_post("/settings", data={"xy_microsteps": str(mode)})
    if st != 200:
        fail("settings write refused: %s %s" % (st, body))
    time.sleep(3)
    t0 = time.time()
    while time.time() - t0 < 180:
        m = fc_get("/mode")
        if m.get("controller") == "running" and m.get("motion") == "verified" and sysfs("cnc/x_mode") == str(mode):
            time.sleep(2)
            print("x%d: controller pid %s, step_freq %s, ramp_rate %s"
                  % (mode, m.get("pid"), sysfs("cnc/step_freq"), sysfs("cnc/ramp_rate")))
            return
        time.sleep(2)
    fail("x%d: the controller did not come back" % mode)


def quiet(on):
    """The engine's quiet hold with the pump: every fan, the coolant pump
    and the TEC off (on), or the phase's posture back (off)."""
    st, body = forgectrl_post("/cool/quiet", params={"on": "1" if on else "0", "pump": "1"})
    if st != 200:
        fail("quiet hold %s refused: %s %s" % ("on" if on else "off", st, body))
    return body


class Grbl:
    def __init__(self):
        self.s = socket.create_connection(("127.0.0.1", 23), timeout=5)
        time.sleep(0.3)
        self.drain()

    def drain(self):
        self.s.settimeout(0.2)
        try:
            while self.s.recv(4096):
                pass
        except (socket.timeout, OSError):
            pass

    def cmd(self, line, timeout=30):
        self.s.sendall((line + "\n").encode())
        self.s.settimeout(timeout)
        buf = ""
        end = time.time() + timeout
        while time.time() < end:
            try:
                buf += self.s.recv(4096).decode(errors="replace")
            except socket.timeout:
                break
            if "ok\r\n" in buf or "error" in buf or "ALARM" in buf:
                break
        return buf

    def status(self):
        self.s.sendall(b"?")
        self.s.settimeout(1.0)
        buf = ""
        end = time.time() + 1.0
        while time.time() < end and ">" not in buf:
            try:
                buf += self.s.recv(4096).decode(errors="replace")
            except socket.timeout:
                break
        if "error:" in buf or "ALARM" in buf:
            fail("the controller answered %r" % buf.strip()[-200:])
        m = re.search(r"<(\w+)[^>]*MPos:([-\d.]+),([-\d.]+),([-\d.]+)", buf)
        f = re.search(r"FS:([\d.]+),", buf)
        if not m:
            return None, None, 0.0
        return m.group(1), (float(m.group(2)), float(m.group(3))), float(f.group(1)) if f else 0.0

    def run_leg(self, gcode, timeout=120):
        """Send one move and time it by the state transitions: t_run at
        the first Run report, t_idle at the Idle after it. The line's own
        "ok" is not waited for first: an arc is fed to the planner
        segment by segment and its ok arrives late in the motion, so it
        is drained along with the reports instead."""
        self.s.sendall((gcode + "\n").encode())
        t_run = t_idle = None
        peak = 0.0
        pos = None
        end = time.time() + timeout
        while time.time() < end:
            st, pos, fs = self.status()
            now = time.monotonic()
            peak = max(peak, fs)
            if st and st.startswith(("Run", "Jog")) and t_run is None:
                t_run = now
            if st and st.startswith("Idle") and t_run is not None:
                t_idle = now
                break
            if st and st.startswith("Alarm"):
                fail("alarm during %r: %s" % (gcode, st))
            time.sleep(0.02)
        if t_idle is None:
            fail("the move %r did not finish" % gcode)
        self.drain()
        return t_run, t_idle, peak, pos

    def close(self):
        self.s.close()


def window(samples, t0, t1):
    xs = [s[1] for s in samples if t0 <= s[0] <= t1]
    ys = [s[2] for s in samples if t0 <= s[0] <= t1]
    return xs, ys


def stats(vals):
    if len(vals) < 2:
        return {"n": len(vals), "rms": None, "p2p": None}
    mean = sum(vals) / len(vals)
    rms = math.sqrt(sum((v - mean) ** 2 for v in vals) / len(vals))
    return {"n": len(vals), "rms": round(rms, 1), "p2p": max(vals) - min(vals)}


def preflight():
    st = fc_get("/status")
    m = fc_get("/mode")
    if m.get("mode") != "grbl" or m.get("controller") != "running":
        fail("the GRBL controller is not running (%s)" % m)
    if not st.get("homed"):
        fail("the machine is not homed: run $H first, the pattern starts from home")
    if not (st.get("switches") or {}).get("lid"):
        fail("the lid is open")
    if st.get("state") != "idle":
        fail("the machine is not idle (%s)" % st.get("state"))
    home = (float((fc_get("/settings") or {}).get("gfcloud_home_x") or 0),
            float((fc_get("/settings") or {}).get("gfcloud_home_y") or 0))
    return home


def run_pattern(mode, home):
    g = Grbl()
    accel = None
    sampler = None
    record = {"mode": mode, "step_freq": sysfs("cnc/step_freq"), "ramp_rate": sysfs("cnc/ramp_rate"),
              "feed": FEED, "legs": [], "quiet_s": QUIET_S}
    try:
        st, pos, _ = g.status()
        if not st or not st.startswith("Idle"):
            fail("controller is %s, not Idle" % st)
        if abs(pos[0] - home[0]) > TOL_MM or abs(pos[1] - home[1]) > TOL_MM:
            fail("the head is not at home (MPos %s, home %s): run $H first" % (pos, home))
        k0 = fc_get("/status").get("pos") or {}
        und0 = int(sysfs("cnc/underruns"))
        print("x%d: quiet hold: %s" % (mode, quiet(True)))
        pwm = {a: sysfs(a) for a in ("thermal/exhaust_pwm", "thermal/intake_pwm", "head/air_assist_pwm",
                                     "head/purge_air", "thermal/water_pump_on", "thermal/tec_on")}
        record["fans"] = pwm
        if any(v != "0" for v in pwm.values()):
            fail("a fan, the pump or the TEC is still commanded on: %s" % pwm)
        print("x%d: every fan, the pump and the TEC commanded off (%s); waiting %.0f s" % (mode, pwm, QUIET_S))
        time.sleep(QUIET_S)
        accel = Accel()
        record["ctrl4"] = accel.ctrl4
        accel.start()
        sampler = Sampler(accel)
        sampler.start()
        time.sleep(1.0)
        t_base0, t_base1 = time.monotonic() - 1.0, time.monotonic()
        g.cmd("G21")
        g.cmd("G91")
        for name, gcode in LEGS:
            t_run, t_idle, peak, pos = g.run_leg(gcode)
            record["legs"].append({"leg": name, "gcode": gcode, "t_run": t_run, "t_idle": t_idle,
                                   "peak_feed": peak, "end_mpos": pos})
            print("  %-22s %5.2f s, peak %5.0f mm/min, end MPos (%.3f, %.3f)"
                  % (name, t_idle - t_run, peak, pos[0], pos[1]))
        g.cmd("G90")
        time.sleep(1.0)
        end = time.time() + 20
        while time.time() < end and fc_get("/status").get("state") != "idle":
            time.sleep(0.5)
        k1 = fc_get("/status").get("pos") or {}
        und1 = int(sysfs("cnc/underruns"))
    finally:
        # The fans first, whatever else fails: the hold must not outlive
        # the listening (the engine would drop it itself within 600 s).
        try:
            quiet(False)
        except SystemExit:
            print("WARNING: the quiet hold did not release; the engine drops it itself within 600 s")
        try:
            if sampler is not None:
                sampler.stop()
        finally:
            if accel is not None:
                accel.restore()
            g.close()
    samples = sampler.samples
    record["samples"] = len(samples)
    record["read_errors"] = sampler.errors
    record["rate_hz"] = round(len(samples) / (samples[-1][0] - samples[0][0]), 1) if len(samples) > 1 else 0
    bx, by = window(samples, t_base0, t_base1)
    record["baseline"] = {"x": stats(bx), "y": stats(by)}
    print("  baseline at rest (fans off): x rms %s p2p %s, y rms %s p2p %s, %d samples at %.0f Hz, %d read errors"
          % (record["baseline"]["x"]["rms"], record["baseline"]["x"]["p2p"],
             record["baseline"]["y"]["rms"], record["baseline"]["y"]["p2p"],
             len(samples), record["rate_hz"], sampler.errors))
    allx, ally = [], []
    for leg in record["legs"]:
        t0, t1 = leg["t_run"] + TRIM_S, leg["t_idle"] - TRIM_S
        xs, ys = window(samples, t0, t1)
        leg["cruise"] = {"x": stats(xs), "y": stats(ys), "seconds": round(max(0.0, t1 - t0), 2)}
        allx += xs
        ally += ys
        print("  %-22s cruise %4.2f s: x rms %6s p2p %6s | y rms %6s p2p %6s (%d samples)"
              % (leg["leg"], leg["cruise"]["seconds"], leg["cruise"]["x"]["rms"], leg["cruise"]["x"]["p2p"],
                 leg["cruise"]["y"]["rms"], leg["cruise"]["y"]["p2p"], leg["cruise"]["x"]["n"]))
    record["overall"] = {"x": stats(allx), "y": stats(ally)}
    kd = (float(k1.get("x", 0)) - float(k0.get("x", 0)), float(k1.get("y", 0)) - float(k0.get("y", 0)))
    record["kernel_return_mm"] = kd
    record["underruns"] = [und0, und1]
    ok = abs(kd[0]) <= TOL_MM and abs(kd[1]) <= TOL_MM and und1 == und0
    record["result"] = "PASS" if ok else "FAIL"
    print("  overall cruise: x rms %s p2p %s | y rms %s p2p %s | kernel return dx=%.3f dy=%.3f | underruns %d->%d | %s"
          % (record["overall"]["x"]["rms"], record["overall"]["x"]["p2p"], record["overall"]["y"]["rms"],
             record["overall"]["y"]["p2p"], kd[0], kd[1], und0, und1, record["result"]))
    record["trace"] = [(round(s[0] - samples[0][0], 4), s[1], s[2], s[3]) for s in samples]
    return record


def main():
    modes = modes_from_argv(sys.argv[1:])
    home = preflight()
    stamp = time.strftime("%Y%m%d%H%M%S")
    results = []
    for mode in modes:
        print("=== x%d ===" % mode)
        set_mode(mode)
        rec = run_pattern(mode, home)
        path = data_path("xy_pattern_accel_x%d_%s.json" % (mode, stamp))
        with open(path, "w") as f:
            json.dump(rec, f)
        print("  record: %s" % path)
        results.append(rec)
    if modes[-1] != 8:
        print("=== restore x8 ===")
        set_mode(8)
    print("\n=== summary (cruise RMS with the mean removed, raw counts; CTRL4 0x%02x) ==="
          % results[0].get("ctrl4", 0))
    print("%-6s %10s %10s %10s %10s %8s" % ("mode", "x rms", "x p2p", "y rms", "y p2p", "result"))
    for rec in results:
        o = rec["overall"]
        print("%-6s %10s %10s %10s %10s %8s" % ("x%d" % rec["mode"], o["x"]["rms"], o["x"]["p2p"],
                                                  o["y"]["rms"], o["y"]["p2p"], rec["result"]))
    return 0 if all(r["result"] == "PASS" for r in results) else 1


if __name__ == "__main__":
    sys.exit(main())
