#!/usr/bin/env python3
# Copyright 2026 514 LLC d/b/a OpenGlow
# Written by Scott Wiederhold
# https://community.openglow.org
# SPDX-License-Identifier:    MIT
"""Fan floor measurement: the numbers the airflow gates ship with.

Two modes, both sampling the four tachometers (/status fans, rpm) and the
purge-air current (sysfs head/purge_air_current, raw) once a second:

  spinup   From the engine's idle posture, M8 opens a run session (the cut
           fan profile), the tool samples for --seconds, then M9 ends it and
           the tool waits for the engine to leave phase "run". Per fan it
           reports the steady speed (the mean of the last --steady seconds),
           the time to 90 % of it, and the spread (min, max, standard
           deviation) over the steady window; for the purge fan the current
           at idle and at run duty (the engine holds purge air on at all
           times, so both are the "on" reading; a dead pump reads about 1).
           Needs the controller in GRBL mode, idle, fans quiet.

  cut      No commands: samples for --seconds while the operator runs a
           real cut, and reports the spread the same way. This is the
           number the debounce has to ride over, measured under the only
           load that matters.

Results print as a table and land as JSON in the bench data directory
(FORGETEST_BENCH_DATA, else beside this file): fan_floor_<mode>_<stamp>.json.
Runs on the board (the bench page) or from a LAN host (gfbench: GF_HOST).
"""
import json
import math
import socket
import sys
import time

from gfbench import HOST, LOCAL, board, data_path, forgectrl_request

FANS = ("exhaust", "intake_1", "intake_2", "air_assist")
PURGE_ATTR = "/sys/glowforge/head/purge_air_current"


def usage():
    raise SystemExit("usage: fan_floor_measure.py spinup|cut [--seconds N] [--steady N]")


def purge_current():
    try:
        if LOCAL:
            with open(PURGE_ATTR) as f:
                return int(f.read().strip())
        return int(board("cat " + PURGE_ATTR).strip())
    except (OSError, ValueError):
        return None


def fans_now():
    st, body = forgectrl_request("GET", "/status")
    if st != 200 or not isinstance(body, dict):
        return {}
    return dict(body.get("fans") or {})


def phase():
    st, body = forgectrl_request("GET", "/cool/status")
    if st != 200 or not isinstance(body, dict):
        raise SystemExit("GET /cool/status: HTTP %s with a body that is not a JSON object: %r"
                         % (st, body if isinstance(body, str) else type(body).__name__))
    return body.get("phase")


def sample(seconds, label):
    rows = []
    t0 = time.time()
    while time.time() - t0 < seconds:
        t = round(time.time() - t0, 1)
        f = fans_now()
        row = {"t": t, "purge": purge_current()}
        row.update({k: f.get(k, 0) for k in FANS})
        rows.append(row)
        if len(rows) % 10 == 1:
            print("  %s +%5.1f s  exhaust %5d  intake %5d/%5d  air %5d  purge %s"
                  % (label, t, row["exhaust"], row["intake_1"], row["intake_2"],
                     row["air_assist"], row["purge"]))
        time.sleep(max(0.0, 1.0 - ((time.time() - t0) % 1.0)))
    return rows


def stats(vals):
    vals = [v for v in vals if v is not None]
    if not vals:
        return {"n": 0}
    mean = sum(vals) / len(vals)
    sd = math.sqrt(sum((v - mean) ** 2 for v in vals) / len(vals)) if len(vals) > 1 else 0.0
    return {"n": len(vals), "mean": round(mean, 1), "min": min(vals), "max": max(vals),
            "sd": round(sd, 1)}


def spinup_report(rows, steady_s):
    steady = [r for r in rows if r["t"] >= rows[-1]["t"] - steady_s]
    out = {}
    for k in FANS:
        st = stats([r[k] for r in steady])
        target = 0.9 * st.get("mean", 0)
        t90 = next((r["t"] for r in rows if r[k] >= target), None) if target > 0 else None
        st["t90_s"] = t90
        out[k] = st
    out["purge_run"] = stats([r["purge"] for r in steady])
    return out


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


def main(argv):
    if len(argv) < 2 or argv[1] not in ("spinup", "cut"):
        usage()
    mode = argv[1]
    seconds = 120
    steady_s = 60
    args = argv[2:]
    while args:
        a = args.pop(0)
        if a == "--seconds":
            seconds = int(args.pop(0))
        elif a == "--steady":
            steady_s = int(args.pop(0))
        else:
            usage()
    stamp = time.strftime("%Y%m%d-%H%M%S")
    result = {"mode": mode, "seconds": seconds, "steady_s": steady_s, "started": stamp}

    if mode == "spinup":
        ph = phase()
        if ph != "idle":
            raise SystemExit("the engine is in phase %r; run this from idle" % ph)
        idle = fans_now()
        result["idle_fans"] = idle
        result["purge_idle"] = purge_current()
        print("idle: fans %s purge %s" % (idle, result["purge_idle"]))
        g = Grbl()
        try:
            print("M8:", g.cmd("M8"))
            rows = sample(seconds, "run")
        finally:
            print("M9:", g.cmd("M9"))
            t0 = time.time()
            while time.time() - t0 < 20 and phase() == "run":
                time.sleep(1)
            g.close()
        result["rows"] = rows
        result["report"] = spinup_report(rows, steady_s)
        rep = result["report"]
        print()
        print("%-11s %8s %8s %8s %6s %7s" % ("fan", "steady", "min", "max", "sd", "t90"))
        for k in FANS:
            r = rep[k]
            print("%-11s %8.0f %8d %8d %6.1f %7s" % (k, r.get("mean", 0), r.get("min", 0),
                                                   r.get("max", 0), r.get("sd", 0),
                                                   "%.0f s" % r["t90_s"] if r.get("t90_s") is not None else "-"))
        po = rep["purge_run"]
        print("purge current: idle %s, run %.0f (min %s max %s); the pump is always on, a dead one reads ~1"
              % (result["purge_idle"], po.get("mean", 0), po.get("min"), po.get("max")))
        print()
        print("candidate floors at 55 %% of steady: " + "  ".join(
            "%s %.0f rpm" % (k, 0.55 * rep[k].get("mean", 0)) for k in FANS))
    else:
        print("sampling %d s while the cut runs (no commands sent)" % seconds)
        rows = sample(seconds, "cut")
        result["rows"] = rows
        result["report"] = {k: stats([r[k] for r in rows]) for k in FANS}
        result["report"]["purge"] = stats([r["purge"] for r in rows])
        rep = result["report"]
        print()
        print("%-11s %8s %8s %8s %6s" % ("fan", "mean", "min", "max", "sd"))
        for k in FANS + ("purge",):
            r = rep[k]
            print("%-11s %8.0f %8s %8s %6.1f" % (k, r.get("mean", 0), r.get("min"), r.get("max"), r.get("sd", 0)))

    path = data_path("fan_floor_%s_%s.json" % (mode, stamp))
    with open(path, "w") as f:
        json.dump(result, f, indent=1)
    print("wrote", path)


if __name__ == "__main__":
    main(sys.argv)
