#!/usr/bin/env python3
"""A dry raster at top speed at each XY microstep mode (on the board).

Usage: raster_dry.py [modes...]        (default: 8 16 32)

Per mode: stores xy_microsteps through forgectrl (which restarts the
idle GRBL controller), waits for the controller to come back at that
mode, then streams a raster with the laser off (M5): LINES passes of
LEN mm along X at F12000, Y stepped by PITCH between passes, and back to
the start. Reports the peak feed, the controller CPU over the job, the
kernel counters against the start (the head must come back within TOL mm
on both axes), Grbl's own drift, cnc/underruns, and any "late events
clamped" line the driver logged. Ends with the key cleared (x8).

Needs the controller in GRBL mode, idle, no other Grbl client, the bed
clear, and LEN mm of free +X travel plus LINES x PITCH mm of free +Y
travel from where the head stands.
"""
import json
import os
import re
import socket
import subprocess
import sys
import time
import urllib.request

FC = "http://127.0.0.1"
FC_TOKEN = "/data/forgefirm/panel.token"
MODES = [int(a) for arg in sys.argv[1:] for a in arg.split()] or [8, 16, 32]
LINES, LEN, PITCH, FEED, TOL = 60, 150.0, 0.2, 12000, 0.05
LOG = "/data/log/forgefirm/grblhal/grblhal.log"


def fc(path, data=None):
    token = open(FC_TOKEN).read().strip()
    body = None
    if data is not None:
        body = "&".join("%s=%s" % (k, v) for k, v in data.items()).encode()
    req = urllib.request.Request(FC + path, data=body,
                                 headers={"Authorization": "Bearer " + token, "X-ForgeFIRM-Token": token,
                                          "Content-Type": "application/x-www-form-urlencoded"})
    try:
        return json.load(urllib.request.urlopen(req, timeout=30))
    except urllib.error.HTTPError as e:
        return {"http": e.code, "body": e.read().decode()}


def sysfs(attr):
    with open("/sys/glowforge/" + attr) as f:
        return f.read().strip()


def kernel_mm():
    p = fc("/status").get("pos") or {}
    return float(p.get("x", 0)), float(p.get("y", 0))


def controller_pid():
    out = subprocess.run(["pidof", "grblHAL_glowforge"], capture_output=True, text=True).stdout.split()
    return int(out[0]) if out else None


def cpu_ticks(pid):
    with open("/proc/%d/stat" % pid) as f:
        s = f.read().split()
    return int(s[13]) + int(s[14])


def set_mode(mode):
    r = fc("/settings?xy_microsteps=", {}) if mode == 8 else fc("/settings", {"xy_microsteps": str(mode)})
    if "http" in r:
        raise SystemExit("settings write refused: %s" % r)
    time.sleep(3)
    t0 = time.time()
    while time.time() - t0 < 180:
        m = fc("/mode")
        if m.get("controller") == "running" and m.get("motion") == "verified" and sysfs("cnc/x_mode") == str(mode):
            time.sleep(2)
            print("x%d: controller pid %s, step_freq %s, ramp_rate %s"
                  % (mode, m.get("pid"), sysfs("cnc/step_freq"), sysfs("cnc/ramp_rate")))
            return
        time.sleep(2)
    raise SystemExit("x%d: the controller did not come back" % mode)


class Grbl:
    def __init__(self):
        self.s = socket.create_connection(("127.0.0.1", 23), timeout=5)
        time.sleep(0.3)
        self.drain()

    def drain(self):
        self.s.settimeout(0.2)
        out = b""
        try:
            while True:
                d = self.s.recv(4096)
                if not d:
                    break
                out += d
        except (socket.timeout, OSError):
            pass
        return out.decode(errors="replace")

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
        m = re.search(r"<(\w+)[^>]*MPos:([-\d.]+),([-\d.]+),([-\d.]+)", buf)
        f = re.search(r"FS:([\d.]+),", buf)
        if not m:
            return None, None, 0.0
        return m.group(1), (float(m.group(2)), float(m.group(3))), float(f.group(1)) if f else 0.0

    def wait_idle(self, timeout):
        peak = 0.0
        end = time.time() + timeout
        seen_run = False
        while time.time() < end:
            st, pos, f = self.status()
            peak = max(peak, f)
            if st and st.startswith(("Run", "Jog")):
                seen_run = True
            if st and st.startswith("Idle") and seen_run:
                return True, peak, pos
            if st and st.startswith("Alarm"):
                return False, peak, pos
            time.sleep(0.1)
        return False, peak, None

    def close(self):
        self.s.close()


def raster(mode):
    pid = controller_pid()
    k0 = kernel_mm()
    und0 = int(sysfs("cnc/underruns"))
    log_off = os.path.getsize(LOG) if os.path.exists(LOG) else 0
    g = Grbl()
    peak = 0.0
    try:
        st, pos0, _ = g.status()
        if not st or not st.startswith("Idle"):
            print("x%d: controller is %s, not Idle" % (mode, st))
            return False
        for line in ("M5", "G21", "G90", "G91"):
            g.cmd(line)
        c0 = cpu_ticks(pid)
        t0 = time.time()
        for i in range(LINES):
            dx = LEN if i % 2 == 0 else -LEN
            r = g.cmd("G1 X%.3f F%d" % (dx, FEED), timeout=60)
            if "error" in r or "ALARM" in r:
                print("x%d: line %d refused: %r" % (mode, i, r.strip()))
                break
            g.cmd("G1 Y%.3f F%d" % (PITCH, FEED), timeout=60)
            peak = max(peak, g.status()[2])
        g.cmd("G1 Y%.3f F%d" % (-LINES * PITCH, FEED), timeout=60)
        ok, peak_end, pos1 = g.wait_idle(120)
        peak = max(peak, peak_end)
        g.cmd("G90")
        elapsed = time.time() - t0
        cpu = 100.0 * (cpu_ticks(pid) - c0) / (os.sysconf("SC_CLK_TCK") * elapsed)
    finally:
        g.close()
    # the machine itself idle: the kernel plays out the depth behind Idle
    end = time.time() + 20
    while time.time() < end and fc("/status").get("state") != "idle":
        time.sleep(0.5)
    k1 = kernel_mm()
    und1 = int(sysfs("cnc/underruns"))
    clamped = []
    if os.path.exists(LOG):
        with open(LOG, errors="replace") as f:
            f.seek(log_off)
            clamped = [l.strip() for l in f if "late events clamped" in l or "underrun" in l]
    drift = (pos1[0] - pos0[0], pos1[1] - pos0[1]) if (pos1 and pos0) else None
    kd = (k1[0] - k0[0], k1[1] - k0[1])
    verdict = ok and abs(kd[0]) <= TOL and abs(kd[1]) <= TOL and und1 == und0 and not clamped
    print("x%d: %s | %d passes of %.0f mm at F%d in %.1f s, peak %.0f mm/min, controller CPU %.1f %% | "
          "kernel return dx=%.3f dy=%.3f mm | grbl drift %s | underruns %d->%d | clamped lines %d"
          % (mode, "PASS" if verdict else "FAIL", LINES, LEN, FEED, elapsed, peak, cpu, kd[0], kd[1],
             drift, und0, und1, len(clamped)))
    for l in clamped[:5]:
        print("   ", l)
    return verdict


def main():
    verdicts = {}
    for mode in MODES:
        print("=== x%d ===" % mode)
        set_mode(mode)
        verdicts[mode] = raster(mode)
    if MODES[-1] != 8:
        print("=== restore x8 ===")
        set_mode(8)
    print("settings xy_microsteps:", repr(fc("/settings").get("xy_microsteps")))
    print("RESULT:", " ".join("x%d=%s" % (m, "PASS" if v else "FAIL") for m, v in verdicts.items()))
    return 0 if all(verdicts.values()) else 1


if __name__ == "__main__":
    sys.exit(main())
