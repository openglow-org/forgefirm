"""The bench diagnostics page: registry of the bench tools and the
subprocess runner behind the #bench tab.

The registry lists every tool of scripts/bench (the README is the human
index; this is the machine one) with its safety class and argument spec.
A tool is runnable from the page once `ported` is set: the script is
installed under the tool directory (/usr/share/forgetest/bench on the
image, override FORGETEST_BENCH_DIR) and runs as a subprocess with the
form's arguments, output streamed to the page. Unported tools are listed
so the catalog of what exists is complete, with Start disabled.

Safety classes:
  dry       reads or dry motion, no emission, forgectrl stays up
  takeover  needs forgectrl stopped and the pulse device free
  scope     a takeover whose result only means something with the named
            instrument on the bench (a scope on LASER_PWM / LASER_ON)
  live      laser emission possible (operator acknowledgment required)

The tools that also run from a LAN host (gfbench.py: GF_HOST) run on the
board here with GF_HOST=127.0.0.1, the panel token in GF_TOKEN, and
their data files under FORGETEST_BENCH_DATA (<data>/bench/). Bench runs
are recorded in <data>/bench.jsonl and never enter a campaign.
"""
import json
import os
import shlex
import sys
import threading

from .log import data_dir, now_ts

DEFAULT_TOOL_DIR = "/usr/share/forgetest/bench"


def _arg(name, type="str", default=None, help="", choices=None, flag=None):
    """One form field. `flag` names the option the value is passed as
    (--feed 600); without it the value is positional, in registry order."""
    a = {"name": name, "type": type, "default": default, "help": help}
    if choices:
        a["choices"] = list(choices)
    if flag:
        a["flag"] = flag
    return a


TOOLS = [
    # -- board-side, dry ------------------------------------------------------
    {"id": "check-pwm", "title": "Laser PWM register check", "script": "check_pwm.py",
     "safety": "dry", "where": "board", "ported": True, "args": [],
     "desc": "Reads PWM2 PWMCR/PWMPR via /dev/mem; expects divider 13 x ~127 counts = ~40 kHz. Read-only."},
    {"id": "pacing-test", "title": "Protocol-loop pacing check", "script": "pacing_test.py",
     "safety": "dry", "where": "board", "ported": True,
     "args": [_arg("mm", "float", 30.0, "jog distance (+X first)"), _arg("feed", "float", 600.0, "feed rate")],
     "desc": "Dry motion: idle/parked states coarse-paced, active motion tight-paced, hold/resume mid-move keeps position."},
    {"id": "bench-m2", "title": "Motion-quality bench", "script": "bench_m2.py",
     "safety": "dry", "where": "board", "ported": True, "args": [],
     "argv_fixed": ["127.0.0.1"],
     "desc": "Bounded round-trip jogs (sanity, max-rate, diagonal) + feed-hold/resume; reports peak feed, transitions, drift."},
    {"id": "bench-phase2", "title": "End-of-data protocol bench", "script": "bench_phase2.py",
     "safety": "takeover", "where": "board", "ported": True, "args": [],
     "desc": "Underrun detection/ack, parked no-replay guard, resume(0), continuous feed, run/underrun cycles. Motors locked, laser latched."},
    {"id": "cp-watchdog", "title": "HV charge-pump watchdog timing", "script": "cp_watchdog_timing.py",
     "safety": "dry", "where": "board", "ported": True,
     "args": [_arg("duration_s", "float", 14.0, "capture length")],
     "desc": "Latches CHG_PUMP feed pulses, polls the watchdog readbacks while commanding short local jogs. Motion only, laser locked."},
    {"id": "accel-fast", "title": "Head accelerometer sampler", "script": "accel_fast.py",
     "safety": "dry", "where": "board", "ported": True,
     "args": [_arg("duration_s", "float", 5.0, "capture length"),
              _arg("jog1", "str", None, "optional mid-capture jog, e.g. $J=G91X20F2400"),
              _arg("jog2", "str", None, "optional second jog")],
     "desc": "Direct-I2C sampler for the head-bus LIS2HH12s with optional mid-capture jogs. CSV to /tmp/accel.csv."},
    {"id": "bump-seek", "title": "Accelerometer bump-seek homing prototype", "script": "bump_seek.py",
     "safety": "dry", "where": "board", "ported": True,
     "args": [_arg("direction", "choice", "-", "X direction", ["-", "+"]),
              _arg("feed", "int", 120, "creep feed"), _arg("segment_mm", "float", 15.0, "jog segment"),
              _arg("max_mm", "float", 200.0, "travel bound")],
     "desc": "Creeps toward a rail in bounded jog segments, detects the contact jolt, jog-cancels and backs off."},
    {"id": "accel-crash-probe", "title": "Head accel crash-detector probe", "script": "accel_crash_probe.py",
     "safety": "dry", "where": "board", "ported": True,
     "args": [_arg("seconds", "float", 20.0, "arm-and-watch window"),
              _arg("mode", "choice", "coexist", "coexist keeps st_accel bound (forgectrl up); unbind frees it",
                   ["coexist", "unbind"], flag="--mode"),
              _arg("ths", "int", 40, "per-axis threshold register value 0..255", flag="--ths"),
              _arg("dur", "int", 0, "IG_DUR1 duration counter (ODR samples)", flag="--dur"),
              _arg("axes", "str", "xyz", "axes to arm for high events", flag="--axes"),
              _arg("jog", "str", None, "optional one jog at t=2 s, e.g. $J=G91X5F1000 (+X only)", flag="--jog")],
     "desc": "Arms the head LIS2HH12's on-chip interrupt generator (IG1) and polls IG_SRC1 for a latched strike. "
             "coexist mode reaches the IG registers over i2c-dev with st_accel still bound (the forgectrl-only "
             "path proof); provoke a trip by hand or with --jog. Touches the IG registers and CTRL1 "
             "(saved, run at 800 Hz, restored; the IG only samples at a running ODR), no emission, "
             "no full-scale change. CSV to /tmp/accel_crash.csv."},
    # -- board-side, takeover / scope ------------------------------------------
    {"id": "pwm-sweep", "title": "LASER_PWM scope sweep", "script": "pwm_sweep.py",
     "safety": "scope", "where": "board", "ported": True,
     "args": [_arg("mode", "choice", "check", "check = read-only, sweep = duty staircase", ["check", "sweep"])],
     "desc": "check: readbacks + PWM2 dump; sweep: PWMSAR through 50/25/75/6/100 percent with 4 s holds. "
             "Locked state (the takeover): latch relocked, refuses if FIRE or LASER_ON reads active."},
    {"id": "pwm-hold", "title": "LASER_PWM scope hold", "script": "pwm_hold.py",
     "safety": "scope", "where": "board", "ported": True,
     "args": [_arg("sar", "int", 64, "PWMSAR value"), _arg("seconds", "int", 10, "hold time")],
     "desc": "Holds one PWMSAR value for a scope window, then restores. Locked state (the takeover): "
             "latch relocked, refuses if FIRE or LASER_ON reads active."},
    {"id": "fire-test", "title": "FIRE drop-timing test (A/B/U)", "script": "fire_test.py",
     "safety": "takeover", "where": "board", "ported": True,
     "args": [_arg("mode", "choice", "A", "A latch locked, B unlocked/unarmed, U true underrun", ["A", "B", "U"])],
     "desc": "Duty 0 throughout; refuses to unlock if HV reports good. Software witnesses + the PSU-connector LASER_ON scope point."},
    {"id": "pwm-stream", "title": "LASER_PWM stream-path test", "script": "pwm_stream_test.py",
     "safety": "takeover", "where": "board", "ported": True, "args": [],
     "desc": "Streams power bytes only (no steps, no FIRE, motor_lock=15, latch locked) through /dev/glowforge; "
             "PASS = counters unmoved, idle at the end, no FIRE/emission read back. The scope on LASER_PWM sees the duty steps."},
    {"id": "gate-a-kernel", "title": "Kernel laser-safety drills K1/K2/K3", "script": "gate_a_kernel_drills.py",
     "safety": "takeover", "where": "board", "ported": True,
     "args": [_arg("drill", "choice", "K1", "K1 stop floor, K2 resume honors latch, K3 mid-ramp unlock", ["K1", "K2", "K3"])],
     "desc": "Software witnesses (cnc/state, laser_enable, laser_on, interlock bit 3); K3 refuses if HV reports good."},
    {"id": "platform-drills", "title": "Kernel platform drills", "script": "platform_drills.py",
     "safety": "takeover", "where": "board", "ported": True,
     "args": [_arg("drill", "choice", "decay", "deadman / rmmod / decay / led / all",
                   ["deadman", "rmmod", "decay", "led", "all"])],
     "desc": "Dead-man trip readback, rmmod/modprobe cycles under load, decay/microstep readback, LED sequence."},
    # -- cooling ------------------------------------------------------------------
    {"id": "flow-confirm", "title": "Coolant flow suspicion/confirmation drill", "script": "flow_confirm_drill.py",
     "safety": "dry", "where": "board", "ported": True, "args": [],
     "desc": "One M8 session walks the verdict state machine through real pump-off transients; PASS/FAIL per transition."},
    {"id": "flow-escalate", "title": "Coolant starved re-check escalation drill", "script": "flow_escalate_drill.py",
     "safety": "dry", "where": "board", "ported": True,
     "args": [_arg("budget_s", "int", 60, "cool_confirm_max_s for the drill (60-3600), restored after")],
     "desc": "With the pump off the job-start check reads SUSPECT and the engine must escalate to FAULT when "
             "the confirmation budget expires; the budget setting is shortened for the drill and restored."},
    {"id": "flow-characterize", "title": "Coolant flow characterization", "script": "flow_characterize.py",
     "safety": "takeover", "where": "board", "ported": True,
     "args": [_arg("duty", "int", 30, "heater duty percent")],
     "desc": "Baseline -> flow -> no-flow -> recovery with the factory temperature curve; aborts past 45 C "
             "downstream. Drives the heater and pump directly (about 9 minutes)."},
    {"id": "flow-sustained", "title": "Coolant sustained re-check run", "script": "flow_sustained.py",
     "safety": "dry", "where": "board", "ported": True,
     "args": [_arg("minutes", "float", 30.0, "how long to hold M8")],
     "desc": "Long run of the real re-check cadence via M8: verdicts, false faults, loop heat accumulation."},
    {"id": "flow-warm", "title": "Coolant warm-baseline validation", "script": "flow_warm_validate.py",
     "safety": "takeover", "where": "board", "ported": True,
     "args": [_arg("cycles", "int", 3, "cycles per case (flow / no-flow)"),
              _arg("target_c", "float", 28.0, "warm the upstream sensor to this before each check"),
              _arg("warm_min", "float", 20.0, "warm-up budget per check, minutes")],
     "desc": "Runs the real check (40 percent / 50 s, cut-profile fans) from a heater-warmed baseline, alternating "
             "flow and no-flow; results to the bench data directory. Slow: about 15 minutes per cycle."},
    {"id": "offset-probe", "title": "Coolant-sensor offset probe", "script": "offset_probe.py",
     "safety": "dry", "where": "board", "ported": True,
     "args": [_arg("mode", "choice", "survey", "survey: one actuator at a time; ladder: the air-assist "
                   "duty ladder; jog: the air assist at run duty while the gantry jogs",
                   ["survey", "ladder", "jog"])],
     "desc": "Which actuator moves both coolant thermistors together? Switches the exhaust (100/50/25 "
             "percent), intakes, air assist, purge, heater, pump, TEC and lid lamp one at a time, dark, "
             "with both sensors at 25 Hz, and scores the common-mode step at every edge and the level "
             "toggling inside every dwell; every value is restored on exit. The pump stops once for "
             "8 s with the tube dark and the heater off. About five minutes."},
    {"id": "aa-offset-check", "title": "Coolant offset correction under the run airflow", "script": "aa_offset_check.py",
     "safety": "dry", "where": "board", "ported": True,
     "args": [_arg("dwell", "float", 20.0, "seconds to read under the run profile")],
     "desc": "M8 brings the fans to the run profile (no laser-on, no press) while the raw coolant "
             "counts, /status and the engine's readings are averaged before, during and after: with "
             "cool_aa_offset_counts at the machine's value the readings hold still while the raw counts "
             "step; at zero they drop by about a degree. About a minute."},
    {"id": "critical-tier", "title": "Coolant critical-tier warm-loop drill", "script": "critical_tier_drill.py",
     "safety": "dry", "where": "board", "ported": True,
     "args": [_arg("max_seconds", "int", 1200, "give up after this long without CRITICAL", flag="--max-seconds")],
     "desc": "Sets the ceiling, the resume gate and the critical line a few tenths above the live upstream "
             "reading and lets the engine's own flow-check heater warm the loop through them inside one M8 "
             "session: OVERTEMP at the ceiling, then CRITICAL (fire blocked, hold, no resume), the fault ending "
             "with the session; settings restored and re-read. Results to the bench data directory."},
    {"id": "flow-recheck", "title": "Coolant re-check characterization", "script": "flow_recheck_char.py",
     "safety": "takeover", "where": "board", "ported": True,
     "args": [_arg("heater_pct", "int", 50, "heater duty percent"), _arg("window_s", "int", 30, "re-check window")],
     "desc": "Short in-run re-checks and the differential metric, flow vs no-flow from a settled loop; "
             "aborts past 45 C downstream (about 5 minutes)."},
    {"id": "flow-matrix", "title": "Coolant flow-detection design matrix", "script": "flow_matrix.py",
     "safety": "takeover", "where": "board", "ported": True,
     "args": [_arg("duties", "str", "10,15,20,30,40,50", "heater duties, percent, comma-separated"),
              _arg("repeats", "int", 5, "interleaved repeats per case")],
     "desc": "duty x flow/no-flow x repeats from a common cooled baseline; cost and precision tables and a "
             "ranked shortlist (the derivation of cool_flow_rise). Very slow: about 1.6 h for the full matrix; "
             "resumable from the results file in the bench data directory."},
    {"id": "flow-sampler", "title": "Coolant sampler", "script": "flow_sampler.py",
     "safety": "dry", "where": "board", "ported": True,
     "args": [_arg("duration_s", "int", 30, "capture length"), _arg("interval_s", "float", 1.0, "sample interval")],
     "desc": "Prints elapsed,raw_down,raw_up at the interval; the sampler behind the flow tools."},
    {"id": "temp-calibrate", "title": "Coolant temperature spot-check", "script": "temp_calibrate.py",
     "safety": "dry", "where": "board", "ported": True,
     "args": [_arg("mode", "choice", "watch", "coolant: watch / point / fit; supply (pic/pwr_temp): "
                   "supply-watch / supply-point / supply-fit",
                   ["watch", "point", "fit", "supply-watch", "supply-point", "supply-fit"]),
              _arg("value", "str", None, "point: the thermometer reading in C; watch: seconds (default 60)")],
     "desc": "Pairs a measured temperature with averaged raw readings; fits a per-machine line. The coolant "
             "sensors, or the power supply's (thermometer on its heatsink, against the documented unverified "
             "guess). Points accumulate in the bench data directory."},
    {"id": "fan-test", "title": "Fan/coolant bench", "script": "fan_test.py",
     "safety": "dry", "where": "board", "ported": True, "args": [],
     "desc": "Snapshots fan PWMs/tachs/temps, drives M8 -> cut fans, M9 -> cooldown -> idle; the tach "
             "readbacks in each snapshot are the evidence."},
    {"id": "fan-floor", "title": "Fan floor measurement", "script": "fan_floor_measure.py",
     "safety": "dry", "where": "board", "ported": True,
     "args": [_arg("mode", "choice", "spinup", "spinup (M8, from idle) / cut (sample only, during a real cut)",
                   ["spinup", "cut"]),
              _arg("seconds", "int", 120, "sampling window", flag="--seconds"),
              _arg("steady", "int", 60, "spinup: the steady window at the end", flag="--steady")],
     "desc": "The numbers the airflow gates ship with: per fan the steady speed at run duty, the time to "
             "90 percent and the spread, the purge current off and on, and (cut) the spread under a real "
             "cut; results to the bench data directory."},
    # -- laser (live) --------------------------------------------------------------
    {"id": "live-fire", "title": "LIVE laser drills", "script": "live_fire_drills.py",
     "safety": "live", "where": "board", "ported": True,
     "args": [_arg("drill", "choice", "witness", "witness / hold / ircut / expstop / ctrlstart",
                   ["witness", "hold", "ircut", "expstop", "ctrlstart"]),
              _arg("power", "int", 1000, "ircut: S value"), _arg("feed", "int", 300, "ircut: F value")],
     "desc": "Emission witness, disarm grace in Hold, lid-IR characterization cut, armed "
             "kill on the expected-stop path (+ the separate controller restart). The operator's arm press is "
             "required for every drill; eye protection, fire watch, extinguisher, exhaust."},
    {"id": "resume-dark-lead", "title": "Pause / resume chain timing (dark lead)", "script": "resume_dark_lead.py",
     "safety": "live", "where": "board", "ported": True,
     "args": [_arg("run", "choice", "dry", "dry travel, or LIVE FIRE", ["dry", "live"], flag="--run"),
              _arg("mode", "choice", "m3", "laser mode for a live run", ["m3", "m4"], flag="--mode"),
              _arg("power", "int", 400, "live: S value", flag="--power"),
              _arg("feed", "float", 600.0, "feed rate", flag="--feed"),
              _arg("len", "float", 60.0, "move length in mm (+X)", flag="--len"),
              _arg("passes", "int", 1, "alternating +X/-X moves", flag="--passes"),
              _arg("secs", "float", 45.0, "sampling window", flag="--secs"),
              _arg("auto", "str", "", "dry only: 'P,R' seconds to send ! and ~ unattended", flag="--auto")],
     "desc": "Samples LASER_ON, FIRE, HV_ENABLE and the charge-pump watchdog straight off the SoC pads "
             "across a pause and a resume, with motion dated from the kernel counters: how long HV survives "
             "a pause, how fast the chain re-arms, and - on a live run - the dark lead between FIRE and "
             "LASER_ON that a resumed cut loses. Dry by default; --run live needs the arm press, eye "
             "protection, fire watch, extinguisher, exhaust."},
    {"id": "pgood-probe", "title": "Supply power-good line against the chain", "script": "pgood_probe.py",
     "safety": "dry", "where": "board", "ported": True,
     "args": [_arg("secs", "float", 90.0, "sampling window", flag="--secs")],
     "desc": "Watches the supply's power-good line (cnc/laser_pgood, reported as the raw pin level) beside "
             "LASER_ON, FIRE, the charge-pump watchdog, HV_ENABLE and the doors through the kernel readbacks at a "
             "few hundred hertz, with hv_current at 20 Hz, and prints every transition. It only watches: drive "
             "the machine meanwhile (a jog, an armed cut, a pause, a lid open). Needs no pad mapping."},
    # -- host-side harnesses (CI) ------------------------------------------------------
    {"id": "laser-stream-test", "title": "Laser pulse-stream emission harness", "script": "laser_stream_test.py",
     "safety": "dry", "where": "host", "ported": False, "args": [],
     "desc": "Null-sink controller stream capture against the feeder contract. A CI harness (the grblHAL repo): "
             "it needs the host-built null-sink controller, not the machine, so it is not a bench-page tool."},
    {"id": "laser-lifecycle-test", "title": "Armed-window lifecycle harness", "script": "laser_lifecycle_test.py",
     "safety": "dry", "where": "host", "ported": False, "args": [],
     "desc": "Arm/disarm lifecycle on the null-sink controller. A CI harness (the grblHAL repo): needs the "
             "host-built null-sink controller, not the machine, so it is not a bench-page tool."},
    {"id": "z-envelope-test", "title": "Z envelope harness", "script": "z_envelope_test.py",
     "safety": "dry", "where": "host", "ported": False, "args": [],
     "desc": "The Z soft limit belongs to the driver, not to $20: an unreferenced Z is collapsed to where the "
             "lens stands, and neither a $20 nor a $132 write frees it. A CI harness (the grblHAL repo): needs "
             "the host-built null-sink controller, not the machine, so it is not a bench-page tool."},
    {"id": "puls-profile", "title": "Factory .puls profile decoder", "script": "puls_profile.py",
     "safety": "dry", "where": "host", "ported": False, "args": [],
     "desc": "Decodes factory pulse streams into velocity/accel profiles. Runs anywhere; needs a .puls file "
             "(the reference captures live off the machine), so it is not a bench-page tool."},
    {"id": "debug-kernel-drills", "title": "Debug-kernel lock drills (load/unload, forced defer)",
     "script": "debug_kernel_drills.py",
     "safety": "takeover", "where": "board", "ported": True, "args": [],
     "desc": "Runs only on the debug-kernel image (kas/forgefirm-glowforge-debug.yml): three glowforge.ko "
             "load/unload cycles under DEBUG_MUTEXES and a forced -EPROBE_DEFER unwind, each read against "
             "dmesg for lock splats. Cycles the 40 V rail; refuses on a non-debug kernel or a non-idle machine."},
]

# Files in scripts/bench the page never offers: the helper module the
# host/board tools share (the C feeder and the build scripts are not
# python), and the two lens stall drills. A stall slips the lens stepper's
# rotor under full torque, so those run from the shell on the bench
# reference machine only (scripts/bench/README.md).
NOT_TOOLS = ("gfbench.py", "lens_travel.py", "lens_stop_accel.py")


class Bench:
    def __init__(self, tools=None, tool_dir=None, index_path=None):
        self.tools = list(tools if tools is not None else TOOLS)
        self._by_id = {t["id"]: t for t in self.tools}
        self._tool_dir = tool_dir
        self.index_path = index_path or os.path.join(data_dir(), "bench.jsonl")
        self._lock = threading.Lock()

    def tool_dir(self):
        return self._tool_dir or os.environ.get("FORGETEST_BENCH_DIR") or DEFAULT_TOOL_DIR

    def get(self, tool_id):
        return self._by_id.get(tool_id)

    def command(self, tool, args):
        """argv for a tool with the form's arguments. Returns
        (ok, argv, error)."""
        script = os.path.join(self.tool_dir(), tool["script"])
        if not os.path.exists(script):
            return False, None, "script not installed: %s" % tool["script"]
        argv = [sys.executable, script] + list(tool.get("argv_fixed", []))
        for spec in tool.get("args", []):
            raw = args.get(spec["name"], spec.get("default"))
            if raw is None or raw == "":
                if spec.get("default") in (None, ""):
                    continue  # optional and absent: not passed at all
                raw = spec["default"]
            try:
                if spec["type"] == "int":
                    val = str(int(raw))
                elif spec["type"] == "float":
                    val = repr(float(raw))
                elif spec["type"] == "choice":
                    if str(raw) not in spec["choices"]:
                        return False, None, "%s must be one of %s" % (spec["name"], spec["choices"])
                    val = str(raw)
                else:
                    val = str(raw)
                    if any(ch in val for ch in "\0\n\r"):
                        return False, None, "%s: invalid characters" % spec["name"]
            except (TypeError, ValueError):
                return False, None, "%s: invalid %s" % (spec["name"], spec["type"])
            if spec.get("flag"):
                argv.append(spec["flag"])
            argv.append(val)
        argv += list(tool.get("argv_fixed_after", []))
        return True, argv, None

    def record(self, tool, args, run):
        rec = {"ts": run.started_ts, "tool": tool["id"], "args": args, "result": run.finished,
               "log_tail": run.lines[-50:]}
        with self._lock:
            os.makedirs(os.path.dirname(self.index_path), exist_ok=True)
            with open(self.index_path, "a", encoding="utf-8") as f:
                f.write(json.dumps(rec, sort_keys=True, separators=(",", ":")) + "\n")

    def last_runs(self):
        out = {}
        try:
            with open(self.index_path, "r", encoding="utf-8") as f:
                for line in f:
                    try:
                        rec = json.loads(line)
                    except ValueError:
                        continue
                    out[rec.get("tool")] = {"ts": rec.get("ts"), "result": rec.get("result"),
                                            "args": rec.get("args")}
        except OSError:
            pass
        return out

    def listing(self):
        last = self.last_runs()
        items = []
        for t in self.tools:
            item = {k: t[k] for k in ("id", "title", "script", "safety", "where", "ported", "args", "desc")}
            item["installed"] = os.path.exists(os.path.join(self.tool_dir(), t["script"]))
            item["last"] = last.get(t["id"])
            items.append(item)
        return items

    def describe_command(self, tool, args):
        ok, argv, err = self.command(tool, args)
        return " ".join(shlex.quote(a) for a in argv) if ok else err
