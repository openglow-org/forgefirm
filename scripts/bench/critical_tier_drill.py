#!/usr/bin/env python3
# Copyright 2026 514 LLC d/b/a OpenGlow
# Written by Scott Wiederhold
# https://community.openglow.org
# SPDX-License-Identifier:    MIT
"""Coolant critical-tier drill: the loop warmed through the lines by the
engine itself, inside one run session.

The loop heater reaches the high twenties at most, so the drill sets the
coolant ceiling, the resume gate and the critical line a few tenths above
the live upstream reading and lets the engine's own flow-check heater
(duty 100 percent, 300 s windows, rechecks every 30 s, the suspect
threshold parked at its top so the warm-up is not read as a flow fault)
carry the coolant through them during a bare M8 session. Expected, in
order: OVERTEMP at the ceiling (the pause tier), CRITICAL at the critical
line (the fail tier: fire blocked, hold, no resume), the fault ending with
the session and the ceiling's hold standing after it. The settings are
restored at the end and a short session makes the engine re-read them.

No laser, no motion, nothing armed. Runs on the board (the bench page) or
from a LAN host (gfbench: GF_HOST). Results as JSON in the bench data
directory.

Usage: critical_tier_drill.py [--max-seconds N]    (default 1200)
"""
import json
import socket
import sys
import time

from gfbench import HOST, data_path, forgectrl_get, forgectrl_post

KEYS = ("cool_temp_max", "cool_temp_resume", "cool_temp_critical_c",
        "cool_flow_heater_pct", "cool_flow_check_s", "cool_recheck_s", "cool_flow_rise")
ABOVE = {"cool_temp_max": 0.4, "cool_temp_resume": 0.2, "cool_temp_critical_c": 0.7}
HEAT = {"cool_flow_heater_pct": "100", "cool_flow_check_s": "300", "cool_recheck_s": "30",
        "cool_flow_rise": "40"}


class Grbl:
    def __init__(self):
        self.s = socket.create_connection((HOST, 23), timeout=5)
        self.s.settimeout(0.15)
        time.sleep(0.5)
        self.drain()

    def drain(self):
        out = b""
        try:
            while True:
                d = self.s.recv(4096)
                if not d:
                    break
                out += d
        except socket.timeout:
            pass
        return out.decode("ascii", "replace")

    def cmd(self, line, wait=0.4):
        self.s.sendall(line.encode() + b"\n")
        time.sleep(wait)
        return self.drain().strip()

    def close(self):
        self.s.close()


def cool():
    st, c = forgectrl_get("/cool/status")
    if st != 200 or not isinstance(c, dict):
        raise RuntimeError("/cool/status -> %s %r" % (st, c))
    return c


def row(c, t):
    return "%5.0f s  %-6s %-8s fire_ok=%-5s hold=%-5s up %.2f down %.2f | %s" % (
        t, c["phase"], c["verdict"], c["fire_ok"], c["hold"], c["up_c"], c["down_c"], c.get("reason", ""))


def session_ended(wait=60):
    for _ in range(wait):
        time.sleep(1)
        c = cool()
        if c["phase"] != "run":
            return c
    return cool()


def main(argv):
    max_s = 1200
    args = argv[1:]
    while args:
        a = args.pop(0)
        if a == "--max-seconds":
            max_s = int(args.pop(0))
        else:
            print(__doc__)
            return 2
    st, before = forgectrl_get("/settings")
    if st != 200:
        print("GET /settings -> %s" % st)
        return 2
    orig = {k: before.get(k, "") for k in KEYS}
    print("original settings: %s" % orig)
    c = cool()
    if c["phase"] == "run":
        print("a run session is already open; nothing done")
        return 2
    t0_up = c["up_c"]
    lines = {k: "%.1f" % (t0_up + d) for k, d in ABOVE.items()}
    lines.update(HEAT)
    print("upstream %.2f C; drill settings: %s" % (t0_up, lines))
    st, body = forgectrl_post("/settings", params=lines)
    if st != 200:
        print("POST /settings -> %s %r" % (st, body))
        return 2
    result = {"started": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()), "upstream_start_c": t0_up,
              "settings": lines, "transitions": [], "critical_s": None}
    g = None
    rc = 1
    try:
        g = Grbl()
        print("M8 -> %s" % g.cmd("M8"))
        t0 = time.time()
        last = None
        crit_at = None
        while time.time() - t0 < max_s:
            time.sleep(1)
            c = cool()
            t = time.time() - t0
            key = (c["verdict"], c["fire_ok"], c["hold"])
            if key != last or int(t) % 15 == 0:
                print(row(c, t))
            if key != last:
                result["transitions"].append({"t_s": round(t), "verdict": c["verdict"], "up_c": c["up_c"],
                                              "down_c": c["down_c"], "reason": c.get("reason", "")})
                last = key
            if c["verdict"] == "CRITICAL" and crit_at is None:
                crit_at = t
                result["critical_s"] = round(t)
            if crit_at is not None and t - crit_at >= 10:
                break
        if crit_at is None:
            print("no CRITICAL within %d s (upstream %.2f C)" % (max_s, c["up_c"]))
        print("M9 -> %s" % g.cmd("M9"))
        c = session_ended()
        print("after the session: %s" % row(c, time.time() - t0))
        result["after_session"] = {k: c.get(k) for k in ("phase", "verdict", "fire_ok", "hold", "reason", "up_c")}
        rc = 0 if crit_at is not None else 1
    finally:
        if g:
            try:
                g.cmd("M9")
            except OSError:
                pass
            g.close()
        st, body = forgectrl_post("/settings", params=orig)
        print("restore POST /settings -> %s" % st)
        result["restored"] = st == 200
        try:
            session_ended()
            g2 = Grbl()
            g2.cmd("M8")
            time.sleep(3)
            c = cool()
            print("re-read session: %s %s limits %s" % (c["phase"], c["verdict"], c.get("limits")))
            g2.cmd("M9")
            g2.close()
            c = session_ended()
            print("final: %s" % row(c, 0))
            result["final"] = {k: c.get(k) for k in ("phase", "verdict", "fire_ok", "hold", "up_c")}
        except OSError as e:
            print("re-read session failed: %s" % e)
    out = data_path("critical_tier_drill_%s.json" % time.strftime("%Y%m%d-%H%M%S"))
    with open(out, "w") as f:
        json.dump(result, f, indent=1)
    print("wrote %s" % out)
    print("RESULT: %s" % ("PASS" if rc == 0 else "FAIL"))
    return rc


if __name__ == "__main__":
    sys.exit(main(sys.argv))
