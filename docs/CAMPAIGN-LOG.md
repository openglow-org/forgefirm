# ForgeFIRM campaign log

The dated record of how ForgeFIRM was brought up: bench campaigns, drills,
scope gates, the audit remediation, and the acceptance campaigns. Entries are
verbatim from the day they were written and are never revised — a correction is
a later entry, not an edit.

**Present state lives in [`BRINGUP.md`](BRINGUP.md)**, which is the runbook and
the authoritative list of open work. Read that first; come here for how a
result was obtained.

Two reading rules for this file:

- **Item numbers refer to the "Next work" list as it stood when the entry was
  written.** That list has since been renumbered around the closed items.
- **"above" and "below" refer to the document as it stood when the entry was
  written**, not to this file's arrangement. Later entries supersede earlier
  ones on the same subject; where an entry was proven wrong, the correction is
  further down.

## Before 2026-08-02 — platform bring-up

### Where the platform stood when the controller work started

**Platform bring-up: complete and hardware-verified.** Both motion blockers
fixed (cnc probe / 40v-supply; SDMA script relocated to `<26 0xF00>` with a
pre-run integrity guard); the end-of-data protocol reworked and bench-proven
(underrun is a first-class `underrun` state behind the `streaming` attr;
16/16 protocol bench); laser PWM verified at 39.98 kHz (register level);
`CONFIG_PREEMPT=y`; uEnv/u-boot/ulfius build integrity restored; legacy
cloud mode repaired (nvmem identity → fuse hostname verified;
deadman/safety loop; camera error paths).

**The controller spike: achieved.**
- grblHAL (unmodified core) runs on the board, speaking Grbl
  1.1f over **TCP port 23** (LightBurn-confirmed).
- Underrun proof: 100 kHz × 120 s under full load, 150 ms queue, 0.2 ms
  worst write latency, zero underruns. Measured SDMA script ceiling:
  **~165 kHz effective** (~6 µs/byte).
- **The step backend works**: the driver resamples grblHAL's step
  events into pulse bytes and live-feeds `/dev/glowforge`. X and Y jogs
  from TCP G-code move the real gantry; grblHAL and kernel position
  counters agree step-for-step. Motion-only: the laser latch is forced
  locked, byte bit 4 is never emitted.

## 2026-08-02 — motion quality, the scope gates, the cooling design

### First real LightBurn job

**First real LightBurn job: 2026-08-02, operator-verified.** Device
setup per `LIGHTBURN.md` (GRBL over TCP:23); a full design job — rapid
in, M4 dynamic-power cut trace at commanded speed, return rapid — ran
smoothly end to end on grblHAL-glowforge (laser locked, motion only).
Two driver fixes came out of the first attempts: the locked laser
spindle (M4/$32 support without fire capability) and the
continuation-wakeup cursor alignment (back-to-back cycles previously
clamped into step bursts — jerky, step-losing rapids; found via the
per-run `clamped` stat from the operator's own job log).

### Milestone 2 — motion quality

**Milestone 2 (motion quality): bench-verified 2026-08-02.** The factory
motion constants were extracted from captured factory pulse files
(`scripts/bench/puls_profile.py`) and applied end-to-end:
- grblHAL defaults now factory-true: 12000 mm/min max rate (X/Y),
  700/590 mm/s² accel (X/Y). Machine tick default 28160 Hz (the factory's
  own travel-move tick; 10 kHz caps an axis at 187.5 mm/s).
- The sink now applies the whole analog machine config itself at init
  (modes, decay, motor_lock, PIC currents) and switches PIC currents
  run↔hold around motion like the factory did (135/22 running, 33/5 idle,
  drop deferred until the kernel queue has drained).
- Bench (`scripts/bench/bench_m2.py`, all green): sustained 200 mm/s on a
  120 mm jog, exact round-trip positioning, feed-hold parks and resumes
  cleanly, current switching observed live, zero underruns at 28160 Hz.
- NOTE: stored $-settings beat freshly baked defaults — after changing
  `GLOWFORGE_DEFAULTS` values, run `$RST=$` once on the board (the sim
  persists settings in its eeprom file in /data).

### Backend milestone 2 closed

**Backend milestone 2 — motion quality: DONE and human-verified
2026-08-02.** Operator confirmed motion is "butter smooth" (and near
silent) on a full observation run — slow/fast/diagonal/zigzag jogs at
up to 200 mm/s under grblHAL-glowforge with the factory-true analog
config. The pre-tuning loudness was the 150/150 currents + unset
decay mode. Milestone closed.

### The standing scope gates

- **LASER_PWM waveform: PASSED 2026-08-02** (scope on the physical
  pin). Method: direct PWMSAR duty steps (`scripts/bench/pwm_sweep.py`
  / `pwm_hold.py`) with the controller stopped, cnc `disabled`
  (steppers unpowered), laser latch locked, lid closed;
  `laser_on_sampled` stayed 0 throughout. Measured: 25.0 µs period /
  40 kHz at every duty; 50/25/75 % confirmed visually; low end
  cursor-measured **6.4 % vs 6.3 % commanded** (PWMSAR=8) — clean
  pulse, no runts, carrier stable across the full range. Matches the
  register-level audit numbers (divider 13 × 127 counts, 39.98 kHz).
- **Stream-path power bytes: PASSED 2026-08-02** (scope on
  LASER_PWM, `scripts/bench/pwm_stream_test.py`: power-bytes-only
  program preloaded and played by the pulse engine; steppers
  energized but motor_lock=15 + zero step bits — position counters
  pinned at 0). Operator observed the full staircase AND both
  contract rules on the pin: **run-start duty reset to 100%**
  (first pulses would fire at full power unless the stream's first
  power byte precedes its first FIRE bit) and **consecutive power
  bytes dropped** (saw 25 % where a 75 % byte rode directly behind;
  75 % applied only after a spacer). Also measured: **duty persists
  after end-of-data** (PWMSAR retains the last value; the end-of-data
  backstop forces FIRE/step lines low, not the power setpoint) — the
  laser-off guarantee rests entirely on FIRE.
- **Laser latch + safety-chain gating: scope-verified 2026-08-02**
  (`scripts/bench/fire_test.py`, probe on the PSU-connector LASER_ON
  pin; power byte 0 throughout, zero step bytes, HV unpowered,
  operator at the power switch; phase B latch-unlock executed by the
  operator). Phase A (latch LOCKED): 40,000 streamed FIRE bits →
  pin dead flat AND kernel `laser_enable` stayed 0 — the latch
  severs the FIRE drive entirely. Phase B (latch unlocked, chain
  unarmed): kernel `laser_enable=1` mid-window, but the PSU pin
  stayed flat and `laser_on`/`laser_on_sampled` stayed 0 — the
  factory board gates LASER_ON behind OK_2_FIRE exactly like the
  OpenGlow AND design (FIRE ∧ OK_2_FIRE, active high at the PSU
  pin). **Interlock snapshot semantics pinned by experiment**
  (13→7 during the unlocked FIRE window): b0 = SoC-side LASER_ON
  monitor, active LOW (1 = not lasing); b1 = FIRE, active high;
  b3 = latch, 1 = locked/0 = unlocked.
- **≤1-tick FIRE drop at underrun/end-of-data: PASSED 2026-08-02**
  (scope on GPIO2_IO30, the SoC FIRE drive feeding the safing
  logic; `fire_test.py` B and U, operator-executed, duty 0, chain
  unarmed). Stream: two 2.000 s FIRE windows, the second ending
  exactly at end-of-data so its falling edge IS the SDMA backstop.
  Measured: **both pulses 2.0000 s exactly, clean edges, on BOTH
  termination paths** — normal completion (streaming=0) and true
  underrun (streaming=1, kernel `underrun` state reached and
  acked). The backstop drops FIRE within one tick (≤100 µs at
  10 kHz) regardless of how the stream dies.
  Signal naming (per the OpenGlow LASER SAFING sheet, confirmed to
  match the factory board): FIRE = per-tick request (kernel
  `laser_enable`, GPIO2_IO30); OK_2_FIRE = chain verdict; LASER_ON
  = FIRE∧OK_2_FIRE to the PSU; HV_EN = HV enable, safing-driven
  only.
- **ALL STANDING SCOPE GATES ARE NOW PASSED.** Live fire remains
  gated on the laser-milestone software itself (power-byte + FIRE
  emission in the stream engine with power-before-fire ordering,
  HV_WDOG retriggering only while genuinely cutting, M3/M4/$32
  mapping) plus a chain-armed first-light procedure; the hardware
  verification prerequisites are complete. Interlock-trip recovery
  (the one non-scope check that was left) was exercised in
  commissioning runs and closed 2026-08-12.

### Fan and thermal control

- **Fan/thermal control (operator-mandated laser-on prerequisite):
  DONE 2026-08-02, bench-verified** (test
  `scripts/bench/fan_test.py`). The policy described in this and the
  following bullets is the cooling engine's; it is now
  forgectrl `cool.c`, serving both controller modes, and the
  `GFCOOL_*` env names carry over as bench overrides (the conf keys
  are the `cool_*` ones — see the cooling-tunables note in the
  forgectrl section). Factory pulse-header
  values throughout: init = pump on / TEC off / purge on / idle
  fans (air assist 204); **M8** (coolant flood — LightBurn's
  per-layer Air Assist) = cut profile (air 1023, exhaust 65535,
  intake 43278); **M9** = 15 s cooldown (`GFCOOL_COOLDOWN_S`) then
  idle. Water temp polled at 1 Hz vs the ~31 °C factory run
  ceiling → one-shot controller warning (laser milestone upgrades
  it to a hard fire gate). Verified via tach readbacks: air tach
  period 4439→699 under M8, exhaust stopped→full, intakes ~3×,
  cooldown hold, clean return to idle; coolant temp visibly
  dropped during the blast. Absolute ceiling 33 °C (job-header
  CMrx).

### Coolant temperature conversion corrected

**Coolant temperature conversion CORRECTED 2026-08-02** — the
UAPI "best guess" `raw*-0.09653+94` was wrong (3–5 °C high, wrong
slope); the real one is the factory B-equation recovered from the
v2.6.0 binary (10 k B3380 NTC, 10 k divider, ×1.3 gain, 10-bit
ADC), proven by reproducing this machine's `WT*` cloud settings
exactly, and thermometer-checked to ~1 °C. Full derivation now in
`kernel-module-glowforge/UAPI.md`. Consequence: the 33 °C ceiling
had been firing at a real ~29 °C, and **anything derived from the
old formula had to be re-derived** — which is how the flow check
below got rebuilt.

### Coolant flow verification rebuilt on a 60-run design matrix

**Coolant flow verification — REBUILT ON A 60-RUN DESIGN MATRIX
(2026-08-02 overnight).** Everything below supersedes the earlier
ΔT-based designs; the tools are `scripts/bench/flow_matrix.py`
(+`flow_sampler.py` on the board), `flow_sustained.py`,
`flow_warm_validate.py`, `flow_recheck_char.py`.
- **Duty is the decisive parameter.** Below ~40 % the stagnant
  loop sheds the heater's output by natural convection well enough
  to **mimic flow**: at 30 %/50 s the five pump-stopped trials read
  8.15, 8.69, 8.78, 12.25, 13.33 °C while flow never exceeded 9.08
  — three of five dead-pump cases looked *healthier* than a working
  pump. At 40 % heat input outruns convection (flow ≤11.46,
  no-flow ≥16.04, d′ 8.4) and it is also the cheapest viable
  option (~0.8 °C of loop heating per check vs ~2.0 °C at 50 %).
- **Operating point: 40 % duty, 50 s window, threshold 14.4 °C**
  (balanced midpoint of 17 flow observations peaking at 12.75 and
  8 no-flow observations bottoming at 16.04).
- **Periodic re-checks every 150 s** (`GFCOOL_RECHECK_S`), because
  a stopped pump is undetectable any other way — absolute
  temperature only tracks a *circulating* loop, and "coolant
  should warm while cutting" is ambiguous (a light engrave may add
  no measurable heat). Sustained 40-minute run: zero false faults,
  and **no thermal accumulation** — with cut-profile fans the loop
  *cooled* 2 °C while being interrogated throughout.
- **Settle gate (safety-critical).** The check measures a rise
  from a baseline; capturing that baseline while the loop is still
  cooling from earlier heat produces garbage and was bench-proven
  to **miss** (reported flow with the pump stopped). Checks are now
  requested, and start only once the sensors agree **and** the
  downstream reading is stationary. Stationarity uses a
  **split-half mean difference**, not peak-to-peak: measured noise
  on a settled loop is 0.52 °C p-p (0.70 worst) but only 0.11 °C
  split-half (0.21 worst), so any p-p threshold tight enough to
  catch drift sits *below the noise floor* and the gate never
  opens.
- **Record: 25/25 correct classifications at 40 %**, plus all three
  settle cases (settled/flow, settled/no-flow, and the unsettled
  no-flow case that previously missed → now defers, then faults).
- **NOT YET VALIDATED (first-light commissioning items):** all
  baselines were 19–23 °C (an overnight-cool room; the loop
  equilibrates near ambient and the heater cannot reach a
  cutting-session loop temperature — 100 % duty drives the
  downstream sensor past 50 °C in 30 s while the bulk barely
  moves). Behavior at 27–32 °C baselines, and under real laser
  heating, must be characterized at first light. Physics argues
  the dependence is weak — with forced flow ΔT = P/(ṁ·c), which
  carries no absolute-temperature term — but that is reasoning,
  not measurement.

### Coolant flow verification — the superseded first design

*(Superseded earlier text kept below for context.)*
**Coolant flow verification (first attempt, live-verified both ways).**
Continuous 10 % heating was never viable on the corrected curve:
flow ΔT ≤3.69 vs no-flow ΔT ≥3.74 — a 0.04 °C gap against ~0.9 °C
of sensor noise. At 30 % the ΔT bands separate (≤9.32 / ≥10.99)
but a ΔT threshold still **failed a live pump-off drill** (8.8 °C
vs a 10.2 °C limit), because a check starting from a cold heater
never reaches the steady-state delta. Final design: a **one-shot
check at job start (M8)** — heater to 30 % for 50 s — with the
discriminator being **downstream temperature RISE** (flow ≈10.3 °C
vs no-flow ≈15.1 °C, ~6 °C separation; threshold 12.7 °C,
`GFCOOL_FLOW_RISE`). Heater goes off afterwards, so the loop is
not warmed for the rest of the job, and absolute over-temp
monitoring carries protection from there (a pump failure mid-cut
shows as a temperature climb far faster than any heater delta).
Verified twice each way from a cooled loop. **v2 (same day): heater job-scoped** (M8..M9 only — an
always-on heater eats headroom below the 31 °C start gate at
idle; flow faulting arms 30 s after heater-on), **two-phase
cooldown** (15 s smoke clear at run duty, then half-duty airflow
until the upstream temp is under the 31 °C resume gate or
`GFCOOL_COOLDOWN_MAX_S`), and **factory-style over-temp pause**
using the factory coolant windows (run ceiling 33 °C / resume
31 °C, env-adjustable: `GFCOOL_TEMP_MAX`/`GFCOOL_TEMP_RESUME`):
a CYCLE over the ceiling gets a feed hold + forced cooling
airflow + auto-resume on recovery; a JOG gets a jog-cancel
(grblHAL refuses HOLD from the jog state by design). Senders see
the Hold state and [MSG:Warning:…] lines. Drilled live with test
limits: jog canceled mid-move, cycle held and auto-resumed, fan
profiles restored on stand-down. TEC control remains for the
laser milestone; these warnings/holds become hard fire gates
there.

## 2026-08-03 — the camera service

### Bench record

Bench (2026-08-03, on the board): stream **15.0 fps** sustained at
1296×972 (NEON demosaic + VPU encode; 3.2 fps on the full software
fallback);
full-res snapshot 2.4 s warm / 2.7 s cold (cold includes the pipeline
bring-up); two parallel same-camera clients share the frame rate; idle
teardown observed. Borrow verified: head snapshot 200 during a lid
stream, the stream riding through the ~1-2 s gap. Preemption verified:
a head-stream request ended the lid viewer's stream cleanly (curl exit
0 mid-stream) and was serving head frames within ~2 s; switching back
likewise. **Motion coexistence proven**: X
round-trip jogs at F1200 with an active stream — producer stats
`clamped 0`, max behind 4.5 ms (the daemon runs at nice +5, single

### LightBurn consumes the stream

**LightBurn consumes the stream directly — operator-verified
2026-08-03** ("without issue", via the mjpg-streamer-compatible
`/?action=stream` alias) while jogging the machine from the same
LightBurn session.

### VPU JPEG offload

**VPU JPEG offload: DONE 2026-08-03, bench-verified — 7.9 fps** (2.5×
the software rate). The stream path demosaics the 2×2 superpixels
straight to planar YUV420 (JFIF full-range 601) and the **CODA960 VPU
JPEG encoder** (mainline coda, V4L2 mem2mem; found by personality, not
node number) does the encode: per-frame **copy 43 ms + convert 75 ms +
encode 7 ms**. Two hard-won facts:
- **Coherent V4L2 MMAP capture buffers are uncached** — demosaicing
  in-place out of one costs ~340 ms/frame at this resolution; one bulk
  memcpy into a cached bounce buffer first (43 ms) makes the same
  demosaic run in 75 ms. The bounce copy is now the fallback path only —
  non-coherent (cached) capture buffers (below) are the default on the
  patched kernel, and all camera paths read the capture buffer directly
  through them.
- The VPU encoder accepts 1296×972 exactly (no MCU-alignment padding
  needed) with quality via V4L2_CID_JPEG_COMPRESSION_QUALITY.
- **A CSI noise/glitch frame can out-size the coda driver's default
  ~2 B/px JPEG capture buffer** (kernel logs "JPEG too large for
  capture buffer" + a vb2 WARN; observed once under streaming+motion
  load). forgectrl requests 3 B/px and drops error-flagged dequeues as
  single bad frames — hardware encode stays active; software fallback
  engages only on repeated consecutive hard failures.
libjpeg remains the automatic fallback (`FORGECTRL_NO_VPU=1` forces
it) and the snapshot path; `/cam/status` reports `"encoder"`.

### NEON demosaic

**NEON demosaic: DONE 2026-08-03 — 15.0 fps, sensor-limited.** The
YUV420 superpixel convert has a NEON kernel (vld2q deinterleave,
vrhaddq greens, vmlal/vrshrn luma, vpaddlq block sums for chroma;
`FORGECTRL_NO_NEON=1` forces scalar): convert 75 → 18 ms, per-frame
copy 34 + convert 18 + encode 7 ≈ 59 ms against the sensor's 66 ms
frame period. The NEON and scalar paths are bit-identical — proven on
a live frame via `FORGECTRL_NEON_CHECK=1` (one-shot memcmp, logs
IDENTICAL). Motion coexistence re-proven at 15 fps: jogs with an
active stream show clamped 0, max behind 7.2 ms (~4 % of the 200 ms
queue) — the worst-case contention signature so far; if real jobs
ever clamp, a stream-fps cap knob is the relief valve.
The IPU cannot help with demosaic (its IC is CSC/scale only — the
`imx-csc-scaler` at /dev/video8 matters only for a future full-res
stream). Not yet done: lens calibration / bed alignment (the
fisheye needs LightBurn's camera calibration pass), and the deferred
5.6 emulator homing-image smoke (the cloud emulator can now be pointed
at live snapshots).

### Cloud-mode complete review (operator-directed)

**Cloud-mode complete review** (operator-directed 2026-08-03):
`load_motion` preloads a job's ENTIRE pulse file into the ring with
no backpressure recovery — with the 16 MiB default ring that caps
cloud jobs at ~28 min and a too-big job fails mid-download; the
write path needs rework (stream-during-run or graceful
too-big rejection). Also: a marked TODO in `load_motion` copies
every job's full pulse file into the logging directory (disk
filler), and many cloud actions are not currently handled at all —
review the action surface end to end (gfutilities service layer).

## 2026-08-07 — pacing, the fortify crash, cached buffers, homing

### Protocol-loop pacing is fd-blocking

**Protocol-loop pacing is fd-blocking (2026-08-07).** `serial_wait()`
drains TX then `ppoll()`s the listen/client fds with the
state-dependent timeout (idle/alarm 10 ms — 1 ms while a delay
callback is pending — motion 200 µs), so traffic wakes the loop
instantly while idle ticks stay coarse. **Bench-verified: idle CPU
7–12% → ~2%** (1.95% with the camera streaming beside it), status
RTT ~1.0 ms median, jogs exact, `clamped 0` with an active stream.
Client RX is armed only while the ring has a full read's worth of
room, so a flow-control-violating sender is paced, not spun on.

### Fortify overflow fixed in the core

**Fortify overflow fixed in the core (2026-08-07): images before this
fix boot with a DEAD controller.** The Yocto-built binary (compiled
with `-D_FORTIFY_SOURCE`) aborted at `settings_init` — "buffer
overflow detected" in /data/glowforge.log — before serving: the core's
`step_us_min[4]` holds `ftoa(hal.step_us_min, 1)` and our 28160 Hz
stream tick renders "35.5" (5 bytes). Bench builds (no fortify)
silently truncated the adjacent unit string instead, which is why it
never showed on the bench. Fixed by sizing the buffer (the single
local commit the core fork's `forgefirm` branch carries atop upstream
master); `$ES` now reports `[SETTING:0|…|35.5|…]` intact. Repro/diagnosis path if ever needed
again: `scripts/bench/build-glowforge.sh` variant with
`-D_FORTIFY_SOURCE=2`, gdb `set breakpoint pending on` + `break
__chk_fail`, run on the board. **Whole-image boot verified 2026-08-07
on the flashed 20260807214320 SD**: both services autostart from the
image binaries — grblHAL (fortified) serves at 1.0 ms RTT with exact
jogs, $0 min 35.5 intact, $H rejected ($22=0); forgectrl streams
15.0 fps, `"buffers":"cached"`, vpu, 41% CPU; grblHAL idle 2.1%.

### Non-coherent (cached) capture buffers + stream FPS cap

**Non-coherent (cached) capture buffers + stream FPS cap: DONE
2026-08-07, bench-verified on the flashed patch-0010 image.** The
remaining per-frame CPU cost was the ~34 ms bulk copy out of the
uncached V4L2 MMAP buffer. forgectrl REQBUFS with
`V4L2_MEMORY_FLAG_NON_COHERENT`; kernel patch 0010 (meta-glowforge-bsp
linux-fslc, `allow_cache_hints` on the imx capture queue) makes vb2
honor it — CPU-cached mmaps with the cache invalidate done inside
DQBUF — so the demosaic reads the capture buffer in place and the
bounce copy disappears. **Bench (2026-08-07, flashed image): stream
stats `dqbuf 0 ms, copy 0 ms, convert 19-20 ms, encode 7 ms` (the
invalidate is sub-ms in practice), 15.0 fps sustained, daemon 41.5%
CPU with one viewer vs ~66% on the bounce path** — per-frame CPU
roughly halved (~27 ms vs ~60 ms busy). Full-res snapshot through the
cached path visually verified (clean fisheye bed image, live frames
differ). Detection is by the `MMAP_CACHE_HINTS` capability bit: on a
kernel without patch 0010 the daemon falls back to the bounce-copy
path unchanged — **fallback bench-verified 2026-08-07 on the
unpatched kernel** (copy 35 ms / convert 18 / encode 7, 15.0 fps, vpu
— identical to before). `/cam/status` reports
`"buffers":"cached|uncached"`; `FORGECTRL_NO_CACHED_BUFS` forces the
bounce path for A/B; the stats log line includes the DQBUF time.
`FORGECTRL_STREAM_FPS` caps the stream rate — capped frames are
requeued without demosaic/encode (snapshots still ride on them) and
don't count toward fps — **bench-verified 2026-08-07**: cap 5 →
5.0 fps exact, daemon 23% CPU vs ~66% uncapped (bounce path; the
relief valve if future CPU work needs headroom). Default stays sensor
max. Images from 20260807204056 carry forgectrl at the bumped SRCREV
(73283b6), so a fresh burn ships the right daemon.

### Web-service homing: bench record and live verification

- Bench record 2026-08-07: forgectrl `/settings` verified on the
  board; `$H` mode dispatch verified (none → error 5); a stub
  gfcloud session (`gfcloud_home_cmd = /bin/true`) completed the
  full real-device handover — `H:1`, MPos set — and post-resume X
  jogs ran the gantry clean (clamped 0). Host tests covered
  success, calibrated coords, runner-failure and timeout-kill.
- **LIVE gfcloud homing VERIFIED 2026-08-07 (bench, via `$H`):**
  full sequence in 65 s — hunt (Z hall + hunt puls), lid image,
  corner move (head physically to back-left), confirmation lid
  image, quiet detect, final Z re-reference — `ok` +
  `<Idle|MPos:0,0,10.593|H:1>`, stream resumed clean. The FIRST
  live attempt failed and exposed four real bugs, all fixed the
  same day:
  1. gfhardware `_run_loop` halted every motion ~0.1 s in on a
     false SW_ESTOP trip — the estop sense reads low during any
     motion (facts bank in `BRINGUP.md`). Gate is now opt-in
     (`MOTION.ESTOP_HALTS_MOTION`, off in gfhome.conf).
  2. `cnc.halt()` didn't exist → the halt path crashed →
     deadman fd closed mid-run → real kernel e-stop (40V off,
     every later hunt skipped as 'Disabled').
  3. Camera conflict: gfhardware's direct V4L2 grab fails while
     forgectrl serves a stream (LightBurn holds one); the runner
     now captures via forgectrl `/cam/snapshot` (full-res, mux
     borrow, per-shot `lamp=` override — head images torch-off).
  4. Kernel: the deadman e-stop path ran sync SPI (PIC safing)
     inside the ATOMIC dms notifier chain → RCU splat. Chain is
     now blocking (trip point = pulsedev release, process ctx);
     the panic handler keeps only the atomic motion stop.
  Also mapped kernel state 'underrun' in gfhardware (state polls
  raised ValueError on it). Commits: gfhardware 8aa4a49 (+02e66c6
  `_hunt` offset), forgectrl 0b05e48, forgefirm cc838f1,
  kernel-module 5fa558c — board runs all of it (module hot-swapped;
  gfhardware hot-patched over the pinned package). All repos are
  pushed and every recipe pin is bumped to these revisions
  (forgefirm 2dce136, meta-openglow 9e2aa34; recipes
  bitbake-verified from the new pins), so a fresh image build
  carries the whole homing release. Remaining homing polish:
  calibrate `gfcloud_home_x/y` against a jog to a known reference
  if the factory corner offset matters.

## 2026-08-08 — diagnostics, panel rework, wireless, install/update

### Diagnostics bench record

Bench record 2026-08-08 (hot-deployed binaries, all through the HTTP
API): **conf plumbing** — `cool_flow_rise=8` posted, next M8's healthy
check read `limit 8.0` → SUSPECT; key cleared mid-session, next M8
re-read 14.4 and the confirming pass cleared the suspicion (also
proving episode continuity across M9/M8). **Takeover** — during a
running verify: grblHAL process gone, marker present, settings POST
409, second start 409. **flow-verify PASS** in 2:42: flow 11.4
(dT 9.7) / threshold 14.4 / no-flow 17.6 (dT 12.8), margins +3.0 and
+3.2; controller back (fresh pid), marker removed, heater 0, pump on
after. Validation ranges live-checked (rise 0.5 → 400, pct 101 → 400,
confirm 45 → 400). UI browser-verified mid-run: Diagnostics panel
streaming phase/temps/log with the lock banner up, Machine tab
Cooling card showing defaults as placeholders, inputs disabled.
**flow-calibrate COMPLETE in 8:45**: flow band 11.6/12.0/12.0
(max 12.0), no-flow band 17.6/17.7/18.1 (min 17.6), gap 5.7 →
**recommended 14.8 — within 0.4 °C of the hand-derived 14.4** from
the original 60-run matrix (the tool independently reproducing the
ground-truth calibration). Result panel + Apply button
browser-verified: the click wrote `cool_flow_rise = 14.8` to the
conf (cleared after; the compiled default stands until the operator
chooses otherwise).

### Units / identity / position panel rework

**Units/identity/position panel rework (2026-08-08, later):
OFFLINE-VERIFIED ONLY — board deploy + bump HELD during the
operator's firmware-upgrade bench testing.** Verified against the
`tools/mock.py` harness in forgectrl (serves the ui.c panel with
mock endpoints; POSTs logged): fuse-identity header (sample id), red
unreferenced position (needed the `.kv>span:first-child` selector
fix — the old descendant selector out-specified `.b-bad` on nested
value spans), imperial placeholders 14.4→25.9 (delta) / 33→91.4
(absolute), position 12.34 mm→0.486 in, dirty-save posting exactly
one changed key converted back (27 °F→15 °C), diag bands ×1.8 with
Apply still posting metric, and a units round-trip leaving nothing
dirty. The C serial→hostname derivation matches gfhardware id.py on
200k random 32-bit serials (host-side cross-check). Also
offline-verified the same way: the **fuse-identity viewer** (GF
Cloud tab, `GET /fuse-identity` fetched on demand only — serial,
derived hostname, and the 64-hex SRK password with a
keep-these-secret warning; modal outside the settings lock, both
dismiss paths clear the values from the DOM).
**LIVE-VERIFIED 2026-08-08 after the firmware-testing hold lifted**
(both binaries hot-deployed onto the fresh 20260808171449 image,
which already shipped the driver at the bumped pin): header reads
the machine's **real fuse identity** — the C derivation confirmed
against its known factory hostname — with
`gf_hostname`/`hostname` gone from /settings; position shows
0,0,0 in red on the unhomed fresh boot and re-renders in inches on
the live units toggle (placeholder 25.9, clean metric round-trip,
conf key cleared after); /fuse-identity returns the real 8-digit
serial + the derived hostname + a 64-hex password (verified by shape, not
echoed), modal opens and clears on close; driver smoke: one M8
flow check verified 10.5/9.5 on the redeployed binary. forgectrl
pin bumped to the panel rework revision.

### Wireless regulatory + region setting

**Wireless regulatory + region setting (2026-08-08, later):** the
boot-time `cfg80211: failed to load regulatory.db` never was a
missing file — packagegroup-base-wifi has always shipped
regulatory.db(.p7s) + iw on both images. The cause:
imx_v6_v7_defconfig builds cfg80211 IN (=y), so it requests the db at
~2.51 s, before `VFS: Mounted root` at ~2.62 s; the load fails (-2)
and stays failed — a later `iw reg set` alone does NOT retry the
file, only an explicit `iw reg reload` recovers it. Fixes shipped:
glowforge.cfg flips CFG80211/MAC80211 to =m (they load with wlcore at
~5.5 s, well after mount, so the direct load succeeds — kills the
message; in the kernel batch above, awaiting the next SD burn), and
forgectrl gained `wifi_country` (System-tab Wireless card, full ISO
3166-1 alpha-2 dropdown, default 00 = world) applied via
`iw reg reload` + `iw reg set <cc>` at daemon startup and on every
change. LIVE-VERIFIED on the flashed 20260808171449 image
(hot-deployed forgectrl): startup domain is the db-backed world
regdom (it shows the 755–928 MHz S1G rules only the db carries),
`POST /settings?wifi_country=US` flipped the kernel to
`country US: DFS-FCC`, and clearing the key returned 00 and removed
it from the conf. The release image still builds under the 200 MiB
slot cap; an explicit wireless-regdb-static image entry was reverted
as redundant (packagegroup-base-wifi covers it).
Power save: the flashed kernel default is on
(CFG80211_DEFAULT_PS=y), so the same forgectrl startup pass pins
`wlan0 power_save off` (cold-boot verified off on the flashed
image); the kernel batch flips the default off too. Quirk: hinting
`iw reg set 00` while the kernel is already in its default world
domain makes cfg80211 intersect world-with-world and report the
alias `country 98` (identical rules, confusing label) — the startup
pass therefore hints a region only when one is set, and hints 00
only to revert a live region change. Consequence, reboot-verified:
with the db loaded and no user hint, cfg80211 follows the AP's
802.11d country IE (the bench AP advertises US — fresh boot came up
`country US: DFS-FCC` with the setting unset; no `country=` in the
supplicant conf, wl18xx does not self-hint), a user-set region
overrides the IE (DE applied while associated to the US AP), and
clearing reverts to the 00 hint. The UI labels the default
accordingly ("Automatic — AP country, else World").

### Flow-fault triage resolved

- **TRIAGE RESOLVED 2026-08-08 — the 2026-08-03 faults were a
  REAL transient stagnation, not false positives; loop trusted
  again.** The log lines (pass rise 11.4, then FAULT 16.5 / 15.9,
  dT 11.6) postdate the warm-baseline validation session:
  `flow_warm_validate.py`'s controller restart truncates
  `/data/glowforge.log` (single `>`), so they were written by a
  driver M8 session after 23:21 on 2026-08-02 — right after a
  bench session that stopped/started the pump 8+ times with
  ~50 °C heater excursions (classic airlock conditions).
  Signature analysis against the design matrix: the fault rises
  sit at the characterized no-flow floor (16.04), and the
  establish-window dT 11.6 sits in the no-flow band (driver-
  equivalent dT-mean from the matrix: no-flow 11.9–13.2 vs flow
  9.8–10.2) — the checks correctly read stagnant/near-stagnant
  water at that moment. Probable cause: transient pump airlock
  from the bench session's pump cycling, self-cleared (the
  preceding 11.4 pass shows flow was fine minutes earlier).
  **Re-verified 2026-08-08 through the production path** (M8 on
  the flashed v0.1.0 image, pump operator-confirmed, 22 °C
  settled loop): rise 11.3 dT 9.5, and after an M9→M8
  layer-cycle, rise 10.8 dT 9.3 — textbook flow-band values.
  Also measured: **no recirculating heat slug** — each check's
  heat is fully shed within ~60 s (two checks left the loop
  0.4 °C net cooler), and fan-profile transitions inject brief
  ~1.7 °C COLD slugs from the radiator (~20 s), showing the loop
  circulates in tens of seconds. Operational lesson: expect a
  possible legitimate flow SUSPECT on the first checks after
  manual pump stop/start cycling — the confirmation machinery
  below absorbs it.

### Suspicion / confirmation state machine

- **Suspicion/confirmation state machine — IMPLEMENTED
  2026-08-08, bench-drilled 6/6 + escalation** (now in the
  forgectrl engine). An over-limit check is a SUSPICION,
  not a fault: `COOLANT FLOW SUSPECT` warning + an immediate
  re-check request (no cadence wait). The next completed check
  decides it — "consecutive" means no clean check in between,
  whatever the wall-clock gap: over-limit again →
  `COOLANT FLOW FAULT`; clean → `coolant flow suspicion
  cleared`, episode counted (3 cleared episodes in one job earn
  an aggregated check-your-coolant warning; counter resets when
  cooldown reaches idle). A suspicion that cannot produce any
  verdict within `GFCOOL_CONFIRM_MAX_S` (default 480 s; budget
  restarts per flood session, runs only in Cool_Run) escalates
  to FAULT — a loop that will not settle after a fault-level
  reading has shown no evidence of health. A clean check from
  the FAULT state logs `coolant flow recovered`. Laser
  milestone: safe posture (hold + laser off + forced cooling)
  moves to the SUSPECT edge; FAULT stays the hard fire gate.
  Every threshold in this machinery is a `cool_*` conf key since
  2026-08-08 (forgectrl Machine tab, re-read per flood start;
  verification/calibration tools in the Diagnostics section).
  Bench drill (`scripts/bench/flow_confirm_drill.py`, on-board,
  real pump-off transients through the production path, single
  M8 session): verified 11.6/9.4 → pump off SUSPECT 16.4/12.0 →
  pump on cleared 11.9/9.5 in 92 s (the 2026-08-03 field case,
  now non-fatal) → pump off SUSPECT 18.5 → still-off confirmed
  FAULT 16.1 just 109 s after the suspect → pump on recovered
  11.1/9.4. All six verdicts in order, 6/6. Escalation drilled
  separately (`flow_escalate_drill.py` with
  GFCOOL_CONFIRM_MAX_S=45): suspect → starved settle → "no
  clean re-check within 45 s" FAULT.

### Install / update system — Phases 0, 1 and 2

**Install/update system overhaul** (planned 2026-08-08): adopt the
factory A/B slot scheme end-to-end — fwup-packaged signed `.fw`
releases, single-stage installer, GUI update manager + boot
selector in forgectrl, offline factory restore from a `/data`
archive, legacy-p4 migration, and later a refreshed recovery image
in boot0. Full phased plan with invariants and decision gates:
`docs/UPDATE-SYSTEM.md` (builds on the facts-bank eMMC map).
**Phase 0 COMPLETE, hardware-verified 2026-08-08**: slot-agnostic
images (`root=${mmcroot}`; the SAME release ext4 boot-verified from
SD and from eMMC p4, steered by env alone — bench flip test), fwup
toolchain cross-version proven (modern-packed signed `.fw` applies
with the factory's 0.14.2; 0.14.2 wants raw 32-byte pubkeys), fwup
in both images, slot-sized release rootfs + hard size gate + ext4
artifact + `scripts/mkfw.sh`. GAP found for Phase 1: the image
ships fw_env tooling but **no `/etc/fw_env.config`** — hand-placed
on the bench SD system (factory-identical: mmcblk2 0x80000/0x82000,
0x2000, redundant) — the ffboot-v2 recipe must install it.
**Phase 1 COMPLETE, hardware-verified 2026-08-08**: ffboot v2 —
`-l` machine-parsable slot inventory (the shared probe for the
installer and the forgectrl update manager), verified atomic
four-variable env flips (one `fw_setenv -s` transaction, read-back
verify, libubootenv→classic→per-var format fallbacks — works on
both fw_setenv flavors), content-probe gate on switch targets
(`-f` overrides), probe-based `-e` newest-factory selection. The
`ffboot` recipe installs `/usr/sbin/ffboot` + `/etc/fw_env.config`
in the image (closes the gap above; build 20260808160821, ext4
still 180.8 MiB). Bench: `-l` classified every slot correctly, and
ffboot itself drove the SD→p4→SD flip cycle (probe gate, both
flips, clean returns). Untested edge: empty/unreadable-slot
classification (no such slot on the bench; exercised naturally
when Phase 2 overwrites a slot mid-install).
**Phase 2 COMPLETE — FULL SLOT INSTALL bench-proven end-to-end
2026-08-08** (operator at the factory console, agent over SSH):
single-stage installer ran on the FACTORY 2024 firmware — archived
both factory rootfs versions + boot0/boot1 (~88 MB total, manifest
with md5s), signature-verified the dev-signed forgefirm.fw, applied
it to slot 2 with the factory's own fwup (29 s), post-verified,
verified-flipped, and ForgeFIRM booted from slot 2; slotmigrate
reclaimed p4 and grew /data to the **byte-exact factory geometry**
(827392/6725632; 0.7 s at boot, silent no-op thereafter); factory
round-trip proven (`ffboot -e` → factory 2024 boots → `-e2` back).
2024-firmware facts learned: no `/factory/imgN` mounts, generic
fw_env.config points at the WRONG device (use per-device
`fw_env_mmcblk2.config` — ffboot's selection logic), no SSH (serial
console only), factory kernel cannot see the SD card (ffboot -s
needs `-f` from factory). The **bench board now runs ForgeFIRM
v0.1.0 from eMMC slot 2** (factory 2024 in slot 1, archives in
/data/forgefirm/archive, dev image still on SD via `ffboot -s`).
The installer's embedded pubkey is the **production release key**
(ceremony executed 2026-08-08; `release.sh` enforces the match).
**Post-test: the bench rests on the SD dev image again** (`ffboot
-s`; slot 1 = factory 2024, slot 2 = ForgeFIRM v0.1.0, archives in
/data/forgefirm/archive). Platform fact pinned by experiment while
chasing a console cosmetic: **busybox mount's auto-type iteration
against an already-mounted ext4 device prints a kernel
"`Can't open blockdev`" for each foreign-type (ext3/ext2) exclusive
claim before the ext4 attempt joins the existing superblock** — the
image's fstab keeps the factory slots mounted under /factory, so
any auto-type probe of a slot triggered it. Cosmetic only; ffboot
and the installer now reuse existing mountpoints from /proc/mounts
and mount fresh targets with explicit `-t ext4` (verified: dmesg
count unchanged across `ffboot -l`).

### SD images 20260808011035

Previously — **SD images 20260808011035 built**
(forgefirm-image + -dev): the first images carrying the whole
control-panel era — gfcloud homing, the OpenGlow-branded panel with
the /status dashboard, controller-mode selector + boot dispatch, the
idle settings lock, and all four platform bug fixes (estop gate,
cnc.halt, forgectrl-routed captures, blocking dms chain). Also: the
control panel carries the
**OpenGlow visual identity** (navy header + recreated starburst
wordmark, light content, laser red as accent only) and the status
page is an **operational dashboard**: motion state + true machine
position (kernel step counters anchored at homing via
`/run/grblhal.homed` — the Grbl socket is never polled, a connection
there displaces the sender), coolant temps, pump/TEC, all four fan
tachs (air assist µs @ 8 ppr, chassis fans ns @ 2 ppr — live-checked),
laser lockout (interlock_circuit b3; `cnc/laser_latch` is
write-only), and the safety switches via EVIOCGSW (head sense reads
not-detected with a working head — display it dim, not alarming).
Previous same-day work: control panel + calibration + identity
overrides + multi-key `/settings`; **gfcloud homing LIVE-VERIFIED
end-to-end** ($H → homed at the factory corner in 65 s; four platform
bugs fixed — see Next work #3); fd-blocking protocol pacing; the
fortify step_us_min fix.

## 2026-08-11 … 2026-08-13 — first light, the wedge, shared services

### First light and the no-motion root cause

- **2026-08-11: the failed first-light attempts' no-motion root
  cause — fast 40 V motor-rail bounces — found and mitigated.**
  An off→on bounce of the 40 V rail within ~tens to hundreds of
  ms (the gfhome→grbl homing handover measured 38–360 ms in
  dmesg) can leave the supply folded back: SDMA playback and the
  position/byte counters run in exact real time while the X/Y
  motors produce no torque, or stall mid-sweep. Bench matrix:
  raw replay of the captured job stream (bytes verified to carry
  correct steps/fire/power content) reproduced no-motion with
  perfect counters; `disable` → ≥2 s rail-off → `clear_all
  (lseek 0)` → `enable` restores torque; a deliberate 40 ms
  bounce reproduced a mid-sweep stall; one post-heal baseline
  still failed — **the rail is marginal at the hardware level;
  watch it**. Exonerated by bisection (Z-hall stream probes +
  operator-observed 20 mm X sweeps): stream content, kernel
  module and SDMA context, the granular lseek clears, analog
  config values, PIC currents, close/reopen, stop, halt.
  Driver mitigation (grblHAL-glowforge b7264bf): every takeover
  of the pulse device (init and homing-session resume) starts
  with a deliberate rail-off settle, conf key `rail_settle_s`
  (default 2.5 s, 0 disables).
  **SAFETY COROLLARY: advancing position counters are NOT proof
  of physical motion** — an armed job can fire with the gantry
  stalled (dwell burn). The laser milestone needs a physical
  motion-liveness gate (limit switches when they land, or the
  head accelerometer); until then the first-light procedure is:
  operator watches from the first commanded move and stops the
  job on any no-motion.
- **2026-08-11 (later, same day): root cause corrected and the
  liveness gate landed.** The supply is fine — the **DRV8825
  stepper drivers wedge on rail glitches** (operator diagnosis;
  see the hardware facts bank in `BRINGUP.md`): whether a given power-up leaves
  them unserviceable is chance, which is why one clean-settle
  baseline still failed. The mitigation stack is now: the
  pulse-device broker (the rail never cycles on handovers), the
  supervisor's **head-accelerometer liveness probe** before each
  session's first controller spawn (+X-first per the cable rule,
  laser latched; rail-off recovery ladder 5/15/30 s on a dead
  verdict; `motion-fault` state when the drivers won't recover),
  and **gfhome's hardened completion** (a run of near-identical
  cloud corrections aborts the session; quiet without an
  accel-witnessed motion window is a failure, not a homing —
  proven the hard way when the service repeated one correction
  eleven times into a motionless gantry, gave up, and the old
  quiet heuristic reported homed). A genuine accel-witnessed
  homing (8 motion windows, head at the corner,
  operator-confirmed) closed the episode.

### GRBL-mode laser software: implementation record

- **GRBL-MODE LASER SOFTWARE: IMPLEMENTED 2026-08-09, bench-verified
  without fire. FIRST LIGHT LANDED 2026-08-11 — first GRBL-mode burn
  completed (operator-run LightBurn job, chain armed, motor-rail
  settle in place).**
  - Architecture: the real spindle lives in
    `grblHAL-glowforge/src/glowforge_laser.c`; per-segment spindle
    updates (the core's laser-mode path, running on the stepper
    producer thread at exact virtual-tick positions) map power/fire
    transitions onto the pulse-byte grid via `gf_stream_laser()`,
    and the shipper emits them: a power byte (0x80 | 7-bit duty,
    raw PWMSAR counts, 127 = 100 %) inserted ahead of the first
    tick byte it covers, FIRE as bit 4 OR'd into tick bytes. The
    spindle PWM is precomputed to a period of exactly 127 so
    computed values ARE power bytes ($30 default 1000 → S1000 =
    127). Contract rules enforced structurally: a power byte leads
    every kernel run before any fire bit (run start resets duty to
    ~100 %), transitions are coalesced per tick so power bytes are
    never consecutive, and power bytes cost no machine tick (the
    SDMA script processes the following byte in the same EPIT
    interrupt), leaving the wall-clock due math untouched. Fire
    only ever rides motion segments of laser blocks - jogs, G0 and
    homing are fire-free by construction, and the end-of-data
    backstop covers every stream end.
  - **Arming - the operator's button press is required.** The first
    laser-on of a job (M3/M4, always planner-synced by the core)
    refuses outright if a coolant fire gate stands, else forces the
    run fan profile on, unlocks the kernel laser latch, lights the
    button white and blocks the gcode stream - pumping real-time
    traffic exactly like the homing session - until the operator
    presses the physical button (EV_SW bit 2), a soft reset aborts,
    or `laser_button_timeout_s` (default 300 s) expires into
    alarm 3. The armed window survives S changes and M5/M3 toggles
    (no re-prompt mid-job) and closes - relocking the latch - after
    `laser_disarm_s` (default 60 s) of spindle-off idle, or
    immediately on alarm/homing/reset/stream fault. Both keys live
    in the shared machine config, re-read per arm.
  - **Underrun policy while armed: fail safe, no retry.** The
    stop/run recovery restarts the kernel run, which resets the
    duty to ~100 % - replaying queued fire bits would fire at full
    power - so an armed underrun acks the kernel and faults (alarm,
    latch relock). Motion-only streams keep the one-shot retry.
  - **Coolant fire gates live** (`gfcool_fire_ok`): flow FAULT or
    over-ceiling coolant temperature (resume-gate hysteresis)
    blocks arming and suppresses fire mid-job with a loud warning.
    While armed the run fan profile + flow interrogation are forced
    on regardless of the sender's M8/M9; a flow SUSPECT/FAULT
    verdict inside an armed window takes the safe posture (feed
    hold + run airflow; laser mode drops the spindle in hold).
    SUSPECT auto-resumes on a clean re-check; FAULT leaves the hold
    and the gate for the operator.
  - **Host verification** (`scripts/bench/laser_stream_test.py`,
    null-sink + `GFSINK_DUMP` stream capture, M4 job S500→S1000
    with a G0 return): power byte leads the stream, no consecutive
    power bytes, first FIRE bit rides nonzero duty, M4 dynamic
    accel scaling visible (duties 44/52 on the ramp), S500 plateau
    63 / S1000 127 exact, 28 354 fire ticks = the cutting time at
    28160 Hz, X peak 533 steps net 0 (steps survive the
    insertions), and 534 dark steps after the last fire bit = the
    entire G0 return.
  - **On-board no-fire verification 15/15 PASS** (chain unarmed,
    nobody at the button; the drill script was a bench one-off and
    is not retained — the arm-window state machine is reproduced
    host-side by `scripts/bench/laser_lifecycle_test.py` and grblHAL's
    `tests/laser_arm_test.c`, and the latch readbacks on hardware by
    `gate_a_kernel_drills.py` and `live_fire_drills.py`): latch locked
    at idle and through jogs (interlock_circuit 13), M4 → prompt +
    latch unlocked (5) + button LED white + run fans forced +
    status served during the wait, soft-reset abort relocks + LED
    off, 3 s timeout drill → warning + ALARM:3 + relock, jogs
    clean after. One transient on the first-ever arm: the
    air-assist run write didn't land (204) - a head-I²C first-write
    blip; deterministic PASS on every rerun, and real jobs re-apply
    run fans with every M8. Note for senders: a disconnecting
    sender leaves a pending arm wait until the button timeout
    clears it (latch relocks then).

### Interlock readback semantics cross-check

- **Interlock readback semantics cross-check: CLOSED 2026-08-12.**
  The full `interlock_circuit` bitmask is mapped: b0 (SoC-side
  LASER_ON monitor, active low), b1 (FIRE, active high) and b3
  (latch, 1 = locked) were pinned by the 2026-08-02 scope
  experiment recorded in the gate section above; b2 (button latch)
  and b4 (interlock latch reset) come from the factory decode the
  attrs were ported from. The armed kill-mid-FIRE drills exercised
  the mask across armed, firing, idle and disarmed states with
  consistent readings, and interlock-trip recovery is confirmed
  from commissioning runs. Attribute semantics are documented in
  `kernel-module-glowforge/UAPI.md`; note `cnc/laser_latch` is
  write-only, so lock state is read from `interlock_circuit` b3.

### Shared machine services complete and closed out

Previously — **shared machine services complete and
closed out (2026-08-13).** forgectrl is the one machine-services daemon behind both
controller modes: the cooling engine (single owner of the thermal
hardware), controller-mode supervision, the pulse-device broker, and
the motion-liveness gate. Both controllers are cooling-engine clients
that enforce the published verdict in-process, and cloud mode ran an
**11.4 h signed-in soak** on that final stack (12 auth-token refreshes,
clean stop from the panel and from SIGTERM). First light landed
2026-08-11 (GRBL mode, operator-run) and the armed kill-mid-FIRE drill
passed 2026-08-12. The contract is `forgectrl/docs/SERVICES.md`;
what is left of that work is item 8 under Next work.

### Shared machine services — the drills

**Shared machine services: complete, bench-verified, and closed out
(2026-08-11 … 2026-08-13).**
forgectrl is the machine-services daemon: the cooling engine (single
thermal-hardware owner for both controller modes, flow verification and
over-temp policy behind the `/cool/state` + verdict-file channels), the
controller-mode supervisor (one managed child, live `POST /mode`
switching, crash respawn with machine safing, a respawn wrapper on
forgectrl itself with retake-at-idle), the pulse-device broker (one
exclusive `/dev/glowforge` hold for the daemon's lifetime — handovers
and respawns never cycle the 40 V rail), and the **motion-liveness
gate**: the head accelerometer is the only truth about physical motion
(the DRV8825 drivers can wedge unserviceably on rail glitches with
counters running normally — see the hardware facts bank in `BRINGUP.md`), so the
supervisor probes real motion before each session's first controller
spawn and gfhome refuses to report a homing the accelerometer did not
witness. The contract for all of it is `forgectrl/docs/SERVICES.md`.
Both controllers are clients of the engine: the GRBL driver's
`glowforge_cooling.c` and the cloud client's `coolsvc.py` report job
state at 1 Hz and enforce the verdict file on their own fire paths,
each with a compiled-in run-duty fallback for the case where the engine
is provably absent. Drilled on the board with the operator present:
engine loss mid-flood and mid-flow-check (warning, fans held, heater
dropped, restore and resume), an armed kill-mid-FIRE (FIRE gone within
15–171 ms, latch relocked, burn line ends abruptly), over-temp hold and
auto-resume inside a real cycle, live mode switches, and a **11.4 h
cloud-mode soak** on the finished stack. Remaining polish: Next work
item 8.

## 2026-08-13 … 2026-08-15 — audit remediation (159 findings, Phases 0-11)

An independent whole-tree audit dated 2026-08-13 produced 159 findings. The
remediation ran as twelve phases, sequenced behind two gates — **GATE A**
(uncommanded energy) before any further live fire, **GATE B** (control surface
and release) before any published release. Both are bench-closed; the drills
are in the bench-campaign section that follows. The audit's own working files
(the findings list, the remediation plan) were retired when the last phase
landed.

### The images that carried it

Image `20260814223300` (forgefirm-image + forgefirm-image-dev)
carries every kernel/image row through Phase 9 and is flashed on the
bench; built-image checks pass: the release rootfs has root locked (`*`
in `/etc/shadow`), no watchdog daemon, forgefirm-logrotate installed,
and `K80grblhal`/`K80gfcloud` ahead of `K90forgectrl` at runlevel 6;
the kernel config carries `CONFIG_IMX2_WDT`, `CONFIG_PANIC_ON_OOPS`,
and `CONFIG_PREEMPT`; the DTB fallback bootargs is console-only;
`glowforge.ko` (the full hardening batch) is in `/lib/modules`. The
Phase 11 sweep (below) is host-verified, pinned, and its controller and
daemon halves are installed on the bench; its kernel half is doc/SPDX-only.
**Image `20260815105250` (forgefirm-image + forgefirm-image-dev) is built
on the Phase 11 pins** — the first image whose license manifest declares
`python3-gfhardware` as `MIT & LGPL-2.1-or-later` and `wlconf` as
`GPL-2.0-only` (packaged output) — with the same built-image checks passing
(root locked, no watchdog daemon, K80/K90 order, `glowforge.ko` and both
controller binaries present) and the buildpaths QA warning gone (the shipped
grblHAL `--version` flags string carries no host paths). Its only build
warning is a stamp-taint note from an earlier forced `do_compile`. **Flashed
on the bench by the operator 2026-08-15** — the board now runs the pinned
Phase 11 userspace from the image rather than hot-installed binaries. With
that, the audit's working files (the findings list, the remediation plan)
are retired: every finding is fixed, every deliberate leftover lives in
"Next work" in `BRINGUP.md`, and the runbook is the record.

### Phase 0 and Phase 1 (GATE A, uncommanded energy)

Phase 0: user-facing laser-safety and
regulatory text is in place (LIGHTBURN.md "Before you cut", README,
INSTALL.md "Regulatory and legal" + updater-first update path, a
persistent panel safety banner), the walkthrough no longer claims the
laser cannot fire, bench-machine identity and the signing-key location
are scrubbed from tracked files (bench scripts take `GF_HOST`), and
every repo has a commit-msg hook enforcing commit attribution. Phase 1
(GATE A, uncommanded energy) is **code-complete and host-verified**:
the stream engine records the cycle-end laser-off so idle-gap pads ship
dark and every stream terminates FIRE-clear (G-1), latch writes are
serialized against the shipper's relight (G-5) with the arm-state and
verdict caches made properly atomic (G-19/G-20/G-21), the cooling
report path moved to a bounded-connect reporter thread off the protocol
thread (A-3/G-7), and gf.lock is priority-inheriting with PIC-SPI and
rail-settle work moved outside it (G-8). Kernel fixes K-1 (saturating
decel ramp + EPIT divisor clamp), K-2 (resume-waypoint latch guard) and
K-3 (latch writes under status_lock; FIRE drive never restored mid-run
or mid-ramp) are code-complete and **ride the pending full-image
flash** with the platform-hygiene batch. `scripts/bench/
laser_stream_test.py` now asserts the termination and zero-step-gap
rules across M4, M3-to-stream-end, and cycle-churn sessions (with a
hermetic cooling-verdict publisher): all PASS on the fixed controller
(the M4 session reproduces the recorded baseline byte-for-byte:
28 354 fire ticks, X peak 533 net 0, 534 dark return steps), and a
build with only the G-1 hunks reverted FAILS on the M3 termination
rule — the harness catches the defect class. **GATE A stays open — no
live-fire — until the flashed image passes the bench drills**
(controlled stop decelerates at the default cloud tick, resume with the
latch locked stays laser-less, mid-ramp latch writes do not re-arm
FIRE) and the harness is wired into CI.

### Phase 2 (GATE B, control surface + release)

**Phase 2 (GATE B, control surface + release) is code-complete and
host-verified.** forgectrl now has one auth layer applied to every
endpoint (`src/auth.c`): a first-boot bearer token in `/data`, embedded
in the panel and required on every state-changing call; a Host
address-literal check plus `Sec-Fetch-Site`/`Origin` validation that
refuses cross-site (CSRF) and DNS-rebinding requests; `/cool/state`
restricted to a loopback peer so a LAN client can no longer spoof a
thermal stand-down (F-1, F-2). The irrevocable fuse view and
unsigned-firmware installs additionally require the physical button held
(F-19, F-1). A native unit test of the real `auth.c` decision logic
passes all ten cases (authorized POST allowed; CSRF refused even with a
token; rebinding host refused; missing/wrong token refused; panel
bootstrap refused over a rebinding host; loopback report allowed, LAN
spoof refused). Also fixed: the `reply_settings` accumulator overflow
and its unbounded validators (F-4, F-18); cooling-tunable caps + a
resume-below-max cross-check + a loud flow-checks-disabled indicator
(F-5); the upload path is auth+idle+job gated (F-9); the liveness probe
refuses to move the gantry with a lid/interlock open (F-13);
`update_job_running()` cross-checks added to the diag and mode-switch
gates (F-14, partial — targeted checks, not yet a single-lock arbiter);
`machine_is_idle()` fails **closed** on a read error so a connection
flood can no longer read as idle mid-cut (X-2); the fd ceiling is raised
(F-15, partial — the MHD connection cap and moving the camera
`ensure_engine` `popen()`s out of the HTTP callback are deferred);
`esc()` and the panel attribute/innerHTML interpolations are escaped
(F-20); the restore `sh -c` double-shell is gone and the archive name is
charset-restricted (B-9). Release engineering: `debug-tweaks` moved out
of the shared kas config into `forgefirm-image-dev.bb` so the release
`forgefirm-image` is no longer passwordless-root, with a `release.sh`
gate that reads the built rootfs `/etc/shadow` and fails on an empty
root password (B-1); the installer copies `ffboot` out of the
signature-verified new rootfs instead of curl-ing it from a mutable ref
(B-2); `CONFIG_PANIC_ON_OOPS=y` + `panic=10` route a kernel oops into
the laser-safing panic handler (B-3, rides the image flash). **GATE B
requires a bench pass** (a CSRF probe from a second host rejected; a
spoofed `/cool/state` no longer drops the fans; a 13-max-length
`POST /settings` does not crash the daemon; a built release image shows
a non-empty root password), after which — combined with Phase 0's
safety/regulatory text — the first public `.fw` is allowed.

### Phase 3 (broker ownership / dead-man second pass)

**Phase 3 (broker ownership / dead-man second pass) is code-complete
and host-verified.** The "broker changed who owns safing" theme is
closed on the code side. The supervisor writes the two safing lines
(`cnc/stop`, `cnc/laser_latch=1`) on **every** transition out of a
running child — mode switch, diagnostics suspend, shutdown, not just
unexpected death — and again immediately after a SIGKILL escalation
(F-3). The cooling engine is the dead-man for **hangs**: a controller
silent past the 5 s report timeout with the armed window open — or with
`cnc/state` still reading `running` (a preloaded cloud ring can play
for minutes with no live feeder) — gets the same two writes from the
engine itself, and exhaust/intake never drop below cooldown duty while
the kernel still reports a run in progress (X-1). The broker fd is now
`O_CLOEXEC` with only the controller spawn clearing the flag, so
`curl`/`fwup`/`media-ctl` children can no longer pin the pulse device,
defeat the final-close backstop, or EBUSY-storm a respawn (F-6). The
GRBL stream shutdown relocks the latch explicitly, since under the
broker its close is not the final close (G-6); the cloud `_shutdown`
hook stops motion, locks the latch, and files a final disarmed/idle
report in **all** modes — gfcloud and gfhome share the hook (C-5).
OOM/RT hardening: `oom_score_adj` respawn wrapper −1000 / daemon −900 /
controllers −500, and the controller `mlockall`s so the SCHED_FIFO
shipper cannot take a major page fault (X-6; the MHD connection cap
remains the deferred half of F-15). Kernel rows **ride the pending
image flash**: pulse-device exclusivity is an atomic in-use bit instead
of a mutex locked in `open()` and unlocked in `release()` — cross-task
release is the *normal* case under the broker (K-4); a fresh open
starts with the flock dead-man disarmed and shared locks are rejected
(K-17); `thermal_make_safe()` de-energizes only the heat sources
(heater, TEC) — the coolant pump and exhaust/intake stay with the
cooling engine, so a dead-man trip no longer stops circulation and
airflow over a hot tube or airlocks the pump, and the heater soft-PWM
duty is zeroed so its timer holds the pin low (X-4). SERVICES.md now
records the watchdog scope — the hardware watchdog is a boot/system
watchdog, not a laser-safety watchdog; the fast beam stop is the
ring-drain chain, and the cloud-ring-depth residual is covered by the
engine's hang dead-man (X-7) — plus the full dead-man ownership map.
Host verification: forgectrl and the controller build clean
(`-Wall -Wextra`), the null-sink stream harness passes all emission
rules on the changed controller, and the cloud client byte-compiles.
**Bench drills pend the image flash**: SIGSTOP a controller
mid-(dry)-run — motion stopped and latch locked within the silence
window, airflow held at ≥ cooldown duty; kill forgectrl during an
update download — no pinned device, no EBUSY respawn storm; re-run the
armed kill drill on the *expected*-stop path; a kernel dead-man trip
leaves pump and airflow running.

### Phase 4 (stale-gate cluster)

**Phase 4 (stale-gate cluster) is code-complete and host-verified; all
of it is hot-deployable (no kernel rows).** The operator-armed window
is now **job-based**, not 60-second-idle-based: it closes at program
end (`M2`/`M30`/`%`, through the kernel-idle-guarded relock so a queue
tail is never severed), whenever the sender connection changes (the
serial layer exposes a client-session generation; the press that armed
the window belongs to the displaced session), and after the disarm
grace — which now counts down in Hold, Door, and Tool Change too, so a
job abandoned in Hold no longer sits armed for hours (X-3, G-10). The
coolant fire gate is re-checked after the button wait, immediately
before the window opens (G-4), and the wait budget is clamped to
1–3600 s — garbage or zero can no longer mean wait-forever with the
latch unlocked (G-18). Cloud mode's `_button_wait` gets the same
treatment: bounded by the shared `laser_button_timeout_s`, lid
re-checked every pass, and timeout/lid/cancel all relock the latch and
disarm (C-7). The cloud cancel-drop is fixed: a settings action
rejected mid-print no longer wipes the running action's id, so a
subsequent cancel actually stops the cut (C-1). forgectrl: a
controller stop that times out restores supervision instead of leaving
the machine permanently controller-less (F-7); settings mutations are
lock-serialized and a multi-key POST lands as one atomic replace
(F-10); graceful shutdown is busy-aware — fans hold their duty and the
verdict ages out instead of being unlinked, so `forgectrl restart` no
longer feed-holds a live cut and drops exhaust (F-12; the flow-check
heater still goes off unconditionally, as this engine's own heat
source). Host verification: forgectrl and the controller build clean,
the null-sink stream harness passes all emission rules byte-identical
to the recorded baseline, and both Python clients byte-compile.
**Bench items:** finish a job and confirm disarm at Idle within the
cycle (not at +60 s); abandon a job in Hold and confirm it disarms;
kill the pump during the button wait and confirm arming refuses;
cancel a cloud print with a settings action in flight and confirm
motion stops; `forgectrl restart` mid-(dry)-cut holds exhaust. These
are dry/no-fire drills except where GATE A already applies.

### Phase 5 (physical-evidence instrumentation)

**Phase 5 (physical-evidence instrumentation) is code-complete and
host-verified.** The machine now watches what it *does*, not just what
it commanded. The cooling engine's 1 Hz tick runs the witnesses:
`cnc/laser_on_sampled` — the sampled, gated output of the hardware
AND-gate — is the emission ground truth, and emission sensed with no
armed window in the recent past stops motion and locks the latch
(repeating while the evidence persists); laser power-good degradation
during an armed window warns once per session; `cnc/faults`
transitions are warned during a run; `pic/hv_current` (the only live
HV telemetry) is ranged per job (A-1, A-4, A-5). The GRBL controller
carries its own in-process witness: emission sensed while the armed
window is closed relocks the latch and raises an alarm (A-1 ctrl
half). The four `pic/lid_ir_*` channels are polled every tick — each
job logs baseline and peaks (the characterization dataset), and the
fire-abort gate (`cool_fire_ir_delta`: sustained rise above run-start
baseline → motion stopped, latch locked, verdict `FIRE` + hold, smoke
airflow held) **ships watch-only (delta 0) until the sensors are
characterized on the bench** (A-2). `/status` exposes the sampled
evidence, faults, HV, and lid IR; the panel's latch row is relabeled
*commanded* with sensed emission and power rows beside it. Cloud: a
failed head capture can no longer leave the measure laser lit — the
capture runs under try/finally and `_action_cleanup` extinguishes the
head emitters (C-3). Kernel (rides the pending image flash): the head
I²C read helpers return signed values with errno propagated, so a bus
glitch reads as an error instead of `beam_detect_analog=65531` /
`accel_irq=1` — the witnesses can no longer be spoofed by a failed
read (K-11). Host verification: forgectrl and the controller build
clean, stream harness all-PASS byte-identical, cloud client
byte-compiles. **Bench items:** command a fire window and confirm
`laser_on_sampled` tracks it (and confirm the idle-state PGOOD
polarity for the panel row); force a head I²C error and confirm the
witnesses report error, not a positive; baseline the lid IR channels
across real jobs and set `cool_fire_ir_delta`; confirm a failed head
capture leaves the measure laser off.

### Phase 6 (motion integrity)

**Phase 6 (motion integrity) is code-complete and host-verified.** A
mid-run underrun or stepper fault is no longer silently absorbed: the
shipper polls `cnc/state` at its own cadence while a kernel run is in
flight and raises the stream fault path — disarm, homing-anchor
invalidation, alarm — the moment it happens (G-2), and the sanctioned
one-shot underrun retry now invalidates the anchor and logs
position-untrusted instead of leaving `homed:true` standing (G-3). The
supervisor unlinks `/run/grblhal.homed` on every controller transition,
so a homed GRBL anchor cannot survive into cloud mode, which re-zeros
the counters it anchors (X-5). Kernel rows (**ride the pending image
flash**): backtrack is bounded by what is physically intact in the ring
and refused outright once the ring has been live-streamed since the
last clear (K-5); `resume` range-checks against the 28-bit waypoint
field instead of silently truncating — 268 435 457 no longer becomes a
waypoint of 1 (K-12); `pulsebuf_total_bytes` is 64-bit with a
saturating 32-bit position ABI, so a long stream cannot wrap it
mid-soak (K-5); ring mutators are mutex-serialized — concurrent
writers on the inherited fd, the clear-vs-run TOCTOU, and the
run-start scratch publish (K-13); and `STATE_FAULT` is recoverable via
`enable` once every non-ignored fault line physically reads clear, so
an edge glitch no longer bricks motion until module reload (K-6).
UAPI.md documents all the contract changes.

### Phase 7 (cloud-mode robustness)

**Phase 7 (cloud-mode robustness) is code-complete and host-verified;
all hot-deployable.** Cloud now fails toward stopped-and-safe: the
service loop survives malformed frames with safing in a `finally`, and
a dead WS client thread ends the session cleanly for the supervisor to
respawn (C-2); network exceptions no longer kill the reconnect thread —
an hourly reconnect during a DNS blip cannot take the machine offline
permanently (C-4); the in-run safety poll cannot be raised out of
(`cnc.state` degrades to FAULT, the verdict reader covers `TypeError`
and future-dated timestamps) and `_action_cleanup` stops motion, not
just the beam (C-6, C-24); an accepted action is never dropped and a
crashed one emits a terminal `:failed` (C-11, C-12); the cooling
reporter is exception-proof with a parting report (C-13); Z homing is
bounded (C-15). Input clamps: pulse-header values clamp to their
now-live min/max bounds before touching motion hardware (C-9);
`load_motion` validates the header before the first byte reaches the
ring and its failure return is handled (C-10); the −273.15 dead-sensor
sentinel no longer passes the start-temp gate (C-16); the dead
`firmware_download()` is deleted (C-19); `EMULATOR.BYPASS_HOMING` keys
on a code-set emulator marker (C-21). Hygiene: tokens no longer reach
the logs — no forced DEBUG, no sign-in dump, owner-only log files
(C-8); the homing accelerometer witness samples at ~100 Hz instead of
saturating the head I²C bus (C-14; re-verify the motion-window counts
against the characterized thresholds on the next live homing); one
hostname derivation, fuzz-verified over 200 k serials with the
short-serial trailing dash fixed (C-18); bounded TX queue + locked
`response_id` (C-20); plus C-17/C-22/C-23. **Bench items:** null-sink
starve drill (sender alarms, `homed` invalidated, armed job refuses at
the stale origin); `STATE_FAULT` glitch recovery without a module
reload; malformed-frame and DNS-blip injections against a live
session; oversize/bad-header job rejected before the ring loads.

### Phase 8 (kernel-module hardening)

**Phase 8 (kernel-module hardening) is code-complete; rides the image
flash.** Probe: `/dev/glowforge` registers last so the error unwind can
never deregister a device userspace already opened; the unwind clears
the SDMA interrupt callback (previously dangling into devm-freed driver
data across an `-EPROBE_DEFER` cycle) and releases the state dirent
(K-7). Remove: every userspace surface comes down before the hardware —
a concurrent attribute read can no longer reach `gpio_get_value` on
freed descriptors — and the dirent is `sysfs_put`, not leaked (K-8).
The fan-tach spinlock is initialized and taken in the IRQ handler (the
cooling engine's fan verdicts ride these two 64-bit timestamps, which
tear on arm32 unlocked) (K-9); tach IRQ setup cleans up after itself
and records only actually-requested IRQs, with idempotent teardown
(K-10). The LED trigger removes its attributes before the sync timer
delete and serializes the simulation step against its store handlers
(K-15). The kernel dead-man now **halts instead of disabling** — no
40 V rail drop, so a crash recovery is never left in the exact state
that wedges the DRV8825 drivers (K-18). Bounds: the safing-path
pin-change off-by-one (K-14); `ignored_faults` capped to the documented
0–7 with the probe fault state decided on the masked value (K-16);
`PIN_LASER_ON_HEAD` joins the SDMA pin set and the stop/shutdown
change sets (K-19); the run-start no-data gate refuses the run on a
failed head fetch (K-20); PIC single-register writes reject values
above the documented 10-bit range instead of wrapping (K-21).
**Bench (on the flashed image):** module load/unload clean under
`CONFIG_DEBUG_MUTEXES`; forced `-EPROBE_DEFER` unwinds without a
dangling callback; concurrent `cat` during remove does not fault; the
Phase 1/3/5/6 kernel drills all re-run green on this one image.

### Phase 9 (build, BSP, and release engineering)

**Phase 9 (build, BSP, and release engineering) is code-complete and
host-verified** (all shell changes pass bash and POSIX-sh syntax
checks; forgectrl builds clean). Shutdown order: controllers stop at
K80, before forgectrl at K90, so runlevel 0/6 never tears down the
cooling engine, fire gates, and broker under a running controller
(B-4). The grblhal/gfcloud init scripts are real emergency levers
routed through new authenticated `POST /controller/stop|start`
endpoints — stop halts the child and holds supervision suspended, not
idle-gated — with `status` verbs and path-anchored pkill fallbacks
(B-6); the forgectrl `restart` self-kill guard matches
`/proc/pid/exe` (B-5). `slotmigrate` gets the 2048-sector grow
tolerance (no more MBR rewrite every boot on disks where the grow
cannot land exactly), progress verification, and a three-attempt
`resize2fs` bound with the counter on p3 (B-7). `CONFIG_IMX2_WDT` is
pinned and the unconfigured watchdog daemon is deliberately dropped —
the hardware watchdog is a boot/system watchdog, and a userspace
petter only added the mid-job-reset failure mode (B-8). The
booted-slot write guard compares device numbers and fails closed under
any `root=` spelling (F-8); settings writes fsync before rename and
never rewrite a file they could not read in full (F-11); the /data
logs rotate size-capped at boot and hourly, and the camera stats spam
dropped ~100× (F-16). Release path: `release.sh` rejects multiple
versions and requires factory-era verification (explicit bypass only);
`mkfw.sh` refuses to pack without the post-sign self-check; the
installer verifies archive product/platform and prompts on a signed
downgrade instead of installing it silently; installer/ffboot temp
paths are `mktemp` (B-13, B-18, B-19, B-20). DTS: the bootargs
fallback is console-only (no `quiet`, no hardcoded SD root) and the
stale 128 MiB ring comment reads 16 MiB (B-11, B-12); wlconf data
files are 0644 (B-16); the U-Boot v2020.01 pin's security posture is
recorded in the recipe (B-17); the bench build scripts carry no
machine-local paths (B-14) and the SSH banner escape is fixed (B-15).
**Bench items:** runlevel 6 teardown order observed; `forgectrl
restart` actually restarts; the routed emergency stop holds the
controller down; a boot on a disk that cannot grow-to-last-sector does
not rewrite the MBR; a `PARTUUID=` cmdline still refuses a write into
the running slot.

### Phase 10 (tests & CI)

**Phase 10 (tests & CI) is code-complete; the safety rules are now
machine-enforced.** The grblHAL controller repo's CI builds the
null-sink binary (driver sources under `-Werror`; the core submodule
is upstream code and exempt) and runs three suites on every push: the
**laser stream emission harness** (the G-1 class), a new
**armed-window lifecycle harness** (`scripts/bench/
laser_lifecycle_test.py`: arm-once-per-job with M5/M3 persistence and
the M2 close, sender-change re-consent, grace countdown in Hold, and
blocking-verdict arm refusal — test-the-test proven: a build with the
job-based window reverted fails the first discriminating assertion),
and a **switch-map decode truth table** (D-13): the EV_SW mapping is
extracted into a pure header and asserted, including the inverted
remote-interlock sense whose flip would read a Pro lockout as
satisfied-while-open, and the opt-in e-stop gating. forgectrl's CI
builds with `-Werror` and the tree is warning-free (the remaining
unused-result and deliberate-truncation warnings are now explicit)
(D-30). `kernel-module-glowforge` has a CI at all (D-4): it
cross-compiles the module against linux-fslc 6.12 with the Glowforge
BSP overlay and config fragment, hardfp toolchain, `KCFLAGS=-Werror`
— the same bar the recipe holds — with symbol resolution left to the
image build (a `modules_prepare` tree has no `Module.symvers`). Every
CI sequence was validated locally before pushing — and CI immediately
earned its keep: running the harnesses as a non-root user exposed that
the controller's `mlockall(MCL_FUTURE)` under a finite
`RLIMIT_MEMLOCK` makes every later thread-stack mmap count against the
limit, killing the stream threads at startup. Root (the production
spawn) carries `CAP_IPC_LOCK` and is exempt, so the flashed image is
unaffected; the lock is now root-only (grblHAL `12977eb`). Not
host-testable (bench items, documented per phase): the kernel latch
relock-on-close and dead-man trip, and the motion-liveness gate.

### Phase 11 (licensing, legal, documentation hygiene)

**Phase 11 (licensing, legal, and documentation hygiene — the last
phase) is code-complete and host-verified, 2026-08-15.** Licensing:
`python3-gfhardware` declares the libdc1394 Bayer decoder it compiles
into `gfhardware._cam` (`MIT AND LGPL-2.1-or-later` in `setup.py`, SPDX
lines on `bayer.c`/`.h`, the LGPL text shipped, the rebuild/relink offer
stated in its README) and the BSP recipe carries `MIT & LGPL-2.1-or-later`
with checksums on both license texts and the decoder header; the `wlconf`
recipe declares the three regimes its vendored TI tarball actually
contains (`GPL-2.0-only & BSD-3-Clause & TI-TSPA`, checksums on the
GPL notice, `COPYING`, and the TSPA `LICENCE`; the packaged output is the
GPL-2.0-only `wlconf/` subtree — nothing from `hw/firmware/` is
installed; provenance recorded as TI WiLink8 R8.7 SP3 with its sha256;
the TSPA text lives in the layer's `custom-licenses`); `python-gfutilities`
anchors its checksum to the upstream repo's own `LICENSE`; the dead
`meta-openglow-bsp` layer is removed; SPDX identifiers now sit on every
grblHAL driver source, every kernel-module source and header, and the
cloud-mode app files; the kernel module credits both authors and the
third-party SDMA assembler tools. `bitbake -c populate_lic` on
`python3-gfhardware`, `wlconf`, and `python3-gfutilities` succeeds against
the bumped pins and deploys the expected license files. Controller
robustness (grblHAL `da4c8eb`, CI green host-side): the pulse write
treats `-ENOMEM`/`-EAGAIN` as bounded back-off (the UAPI's backpressure
semantics), retries `EINTR`, and completes partial writes; the verdict
parser trusts only a complete document (closing brace, 1 KiB buffer)
and defaults a missing `hold` to true; the listen socket and accepted
clients are close-on-exec so the homing runner can never keep port 23
bound; a missing or unwritable settings file falls back to a RAM-backed
NVS with a diagnostic instead of a crash-respawn loop; `-e`/`-p`
argument walks, the `serial_wait` ≥1 s busy spin, `GFSINK_RATE`/
`GFSINK_DEPTH_MS` ranges, the blocking delay's `sys.abort` test, and
`gfio_wr_attr` short-write/`EINTR`/missing-attr semantics are all fixed;
messages from the SCHED_FIFO shipper and from under the stream lock go
through a raw `write(2)` (no stdio lock convoy); the `--version` C-flags
string no longer carries toolchain path-remapping flags (the buildpaths
QA warning). Daemon robustness (forgectrl `ed2934b`, `-Werror` build +
unit test green): the controller environment is built before `fork()`
and passed to `execle()` (no `setenv` in the child of a multithreaded
parent), a SIGKILL escalation is never aimed at a pid the supervisor
thread already reaped, the settings file is created 0600 (the cloud
password lives there; the Python side matches), the verdict publisher
refuses an over-long document, and the release download carries
`curl --max-filesize`. `laser_button_timeout_s`, `laser_disarm_s`, and
`rail_settle_s` are accepted by `POST /settings` (bounded like the
controller's clamps) and have a home on the panel's GRBL tab. Docs:
`kas/README.md` #5 states the real 16 MiB ring arithmetic (~84 s at
200 kHz; the PREEMPT_RT decision stands on the bounded-queue-depth
argument), the deleted `kernel-module-glowforge.bbappend`/externalsrc
references are gone from kas, `release.sh`, and the cold-build workflow,
`BUILD.md` clones only what a builder needs, `README.md` states the
homing dependency honestly (GRBL mode jogs and cuts cloud-free; `$H` is
camera-referenced homing that needs a Glowforge session until switch
homing lands), `CLOUD.md` and `SERVICES.md` agree that the supervisor
starts controllers, `UAPI.md`'s sysfs tree lists `free`/`streaming`/
`underruns` (with `free` stated as advisory — the `-ENOMEM` write return
is the backpressure primitive) and the `position` counter wrap/saturate
behavior, `SERVICES.md` carries a monotonic-clock rule (no RTC on the
board), the `COOL_FLOW_RISE_C` derivation is documented for a third party
to re-run (`scripts/bench/README.md`; the bench tools take `GF_HOST` and
`GF_SSH`), the pre-first-light no-fire drill's citation names the retained
reproductions (its one-off script was never committed), British spellings
are corrected (the wire-protocol literal `cancelled` untouched),
`3d-models/` is a git repo, dev-machine paths and the build-distro name are
out of every tracked file, and the doc-nit bundle (dual-boot wording,
`tested_against_gf` described as it is wired, the image recipe comment,
the bench README tool list, the panel's System tab) is closed. Pins:
forgectrl `ed2934b`, grblHAL `da4c8eb`, kernel module `1862ad3`,
gfhardware `6c7534a`, gfutilities `6d309ae` — all pushed, bumped, and
`bitbake -c fetch`-verified. **Bench (operator, 2026-08-15): the new
controller and daemon binaries are installed on the board and the
settings file is confirmed 0600** — Phase 11 has no open items.

### Kernel platform hygiene batch

**Kernel platform hygiene — CODE-COMPLETE and build-verified
2026-08-13 (kernel-module `6fdc4b2`, meta-openglow `34a0e2e`), bench
validation pending.** The batch edits the kernel overlay (DTS +
config fragment), so it **ships with a full image flash, not a module
hot-swap** — flash the next image before running the checks. What
changed and what each item needs on the bench:
- **Panic handler enabled** (`INSTALL_PANIC_HANDLER 1`), reduced to
  what is legal in atomic context: `epit_stop()` plus a direct
  `io_change_pins(cnc_shutdown_pin_changes)` — FIRE parked, charge
  pump low so the hardware watchdog stops being fed, latch reset
  asserted, steppers de-energized. It no longer calls
  `_driver_stop()` (hrtimer cancel, sysfs notify).
  **Bench:** panic mid-motion with motors locked and the laser
  latched; confirm motion stops and the safety lines read safe.
- **`control_12v` node dropped** along with
  `CONFIG_REGULATOR_USERSPACE_CONSUMER`; the 12 V rail is
  `regulator-always-on` and nothing in userspace referenced the node.
  **Bench:** confirm the rail still comes up and the machine behaves
  identically.
- **`struct gpio_desc` layout hack removed.** The commanded decay
  mode is tracked per axis and seeded at probe to mixed decay (both
  pins requested `GPIOF_IN`), instead of reading a private kernel
  struct. **Bench:** set each mode per axis and read the attr back.
- **Module build hygiene:** `-Wno-error` dropped, `.DELETE_ON_ERROR`
  added, and the warnings that surfaced fixed (missing prototypes now
  static or declared in the new `ledtrig_smooth.h`; LED teardown no
  longer flushes the system work queue — the LED work runs on an
  ordered queue the driver owns and destroys). The recipe passes
  `KCFLAGS=-Werror` to hold the zero-warning state without making the
  module's own Makefile unusable against other kernels.
  **Bench:** LED brightness behavior, and a clean module unload.
- **Platform guards** (not reservations — dmaengine has no channel
  reservation for this path): the SDMA channel number is
  range-checked and its takeover logged; the EPIT clock rate is read
  back at probe, failing probe at zero and warning below the rate
  needed to quantize step frequencies within 1 %; and
  `io_verify_base_address()` checks the GPIO-number→bank math against
  each pin's controller node in the DT, warning rather than failing.
  **Bench:** read the two new probe lines in dmesg and confirm no
  bank warnings.
- **`head_make_safe` implemented:** measure laser off, UV LED off,
  lens motor de-energized (group-register clear-bits write) — legal
  now that the dead-man chain is blocking. Head fans and the white
  LED are deliberately left alone: `SERVICES.md` gives the fans to
  the cooling engine (whose stand-down keeps airflow after a job
  dies) and the white LED to the camera. **Bench:** trip the dead
  man's switch and read the head registers back.
- The uniprocessor locking assumption and the panic/dead-man safe
  states are documented in `kernel-module-glowforge/UAPI.md`; no
  bench item.
- **`hv_enable` rename + polarity flip (2026-08-15) rides the same
  flash.** The gpio-keys node for GPIO4_06 is now `hv_enable`,
  declared active-low, so EV_SW bit 4 reads as the HV_ENABLE output
  itself (inactive at idle, active through a run). forgectrl
  (`/status` key `switches.hv_enable`, panel "HV enable"), the grblHAL
  driver (`SW_BIT_HV_ENABLE`, no gating) and gfhardware
  (`InputSwitch.SW_HV_ENABLE`, no gating) all ship in the same image
  and read the new polarity; the DTS and that userspace must not be
  mixed across the flash (a mismatch only inverts the telemetry — nothing
  gates on the bit — but the dashboard would lie). **Image
  `20260815162923` (forgefirm-image + forgefirm-image-dev) is built on
  these pins** (forgectrl 801f1f3, grblHAL-glowforge b629c18,
  python3-gfhardware c3d1790, kernel module d750784, meta-openglow
  b1ba543): the built DTB carries the `hv_enable` node with
  `gpios = <&gpio4 6 GPIO_ACTIVE_LOW>` and no `estop` string, the rootfs
  forgectrl emits `"hv_enable"` and no `"estop"`, the grblHAL binary has
  no `estop_halts_motion`, `gfhardware/_common.py` carries
  `SW_HV_ENABLE`, and the standard built-image checks pass (root locked,
  no watchdog daemon, K80/K90 order, `glowforge.ko` in `extras/`); the
  only build warning is the usual forced-`do_compile` taint note.
  **Flashed and BENCH-VALIDATED 2026-08-15 (operator flashed; image
  reports `20260815162923 (dev)`, `/proc/device-tree/switches/hv_enable`
  present):** with `/status` and `cnc/charge_pump_alive` sampled together
  at ~10 Hz on the board through a 5 mm X jog (`$J=G91 X-5 F300`, no
  Grbl client attached, laser locked): `hv_enable:false` / pump 0 at
  idle; `true` / 1 in the same sample the state went `running`; still
  `true` / 1 in the first `idle` sample after the run; pump 0 ≈0.4 s
  after that idle sample with `hv_enable` false in the next sample
  (89 ms later); the head returned to `MPos 0.000`. The switch reads as
  HV_ENABLE itself, in lockstep with the watchdog readback.
- **GATE A kernel fixes added to the same flash (2026-08-14):**
  the controlled-deceleration ramp now floors at the minimum step
  frequency with a saturating decrement, and `epit_hz_to_divisor()`
  can no longer return the degenerate divisor 0 (a 0 Hz request maps
  to the slowest achievable tick); the resume waypoint re-enables
  the FIRE drive only when the laser latch is unlocked; and
  `laser_latch` writes run under `status_lock`, restoring the FIRE
  output drive only when no run or ramp is in flight.
  **Bench (GATE A stays open — no live-fire — until these pass):**
  a controlled-stop drill at the default cloud tick (10 kHz, ramp
  125000) shows a decelerating tail rather than a max-rate burst;
  feed-hold, jog-cancel and `^X` each land in a controlled stop with
  position preserved; a resume waypoint with the latch locked stays
  laser-less; `laser_latch=0` written mid-ramp does not re-arm FIRE
  (probe the PSU-connector LASER_ON line as in `fire_test.py`).
  **The GATE A part of this list is DONE (K1/K2/K3 + `fire_test`
  A/B/U pass on image `20260814223300`, campaign record above); the
  platform-hygiene items themselves are consolidated in item 10.**

## 2026-08-14 … 2026-08-15 — the bench campaign

### Post-flash health, GATE B, GATE A

**Bench campaign — opened 2026-08-14; image `20260814223300` flashed and
booted.** Post-flash health check passes on the board: it reports
`20260814223300 (dev)`; kernel `6.12.20-fslc` with `CONFIG_PREEMPT` and
the console-only `panic=10` command line; `CONFIG_IMX2_WDT` and
`CONFIG_PANIC_ON_OOPS` present in the running config; the hardened
`glowforge.ko` loaded with the 16 MiB `cnc-pulsebuf` no-map pool mapped
and SDMA channel 26 / EPIT up; forgectrl holds `/dev/glowforge` (40 V up,
dead-man active) and supervises the grbl controller with the
motion-liveness probe reading **verified**; the latch reads **locked**
and faults 0 at idle; the only `watchdogd` is the kernel kthread (no
userspace watchdog daemon). **GATE B is bench-verified on the
software/control-surface side.** From a second LAN host every
state-changing endpoint refuses an unauthenticated write
(`403 authentication required`); a spoofed non-literal `Host`, a
non-literal `Origin`, and a cross-site `Sec-Fetch-Site` are each refused
(`403 request origin refused`); `/cool/state` refuses a non-loopback peer
(`403 loopback only`); the four-POST unsigned-flash chain
(`upload → apply?confirm_unsigned=1 → boot → reboot`) and
`restore/factory` are each refused unauthenticated; `/fuse-identity` is
fully token-gated (F-1, F-2, F-19). The authenticated max-length
`POST /settings` probe passes without a crash: a 300-character value is
refused `400`, thirteen 16-character in-range values are accepted `200`,
and `/status`, the panel `/`, and `/settings` all keep serving, with the
settings restore verified byte-identical to the pre-test snapshot (F-4,
F-18). On-board build facts re-confirmed on the running image:
controllers stop at `K80` before forgectrl at `K90` (rc0/rc6, B-4); the
forgefirm logrotate config and init lever are installed (F-16); there is
no `/etc/watchdog.conf` or watchdog init (B-8); the wlconf data files are
0644 (B-16); the panel token is stored 0600 (the settings file's 0600
creation is Phase 11's F-23, host-verified there). **GATE A dry motion
drills pass
(latch locked, no emission, operator watching):** bounded relative jogs
move the gantry (operator-witnessed) and the grblHAL position counter
tracks the commanded moves exactly, returning to rest; a
jog-cancel (`0x85`) stops the jog cleanly short of target and returns to
`Idle` with position preserved; a feed-hold (`!`) parks with the feed
ramping to 0 (`Hold:1`→`Hold:0`) and a resume (`~`) completes the move
with no lost-step alarm; a `^X` abort decelerates under control into
`Alarm` with machine position retained, `$X` recovers to `Idle`, and a
subsequent jog runs — no DRV8825 wedge after the abort (the rail never
cycled). **Dry dead-man / disruption drills pass (latch locked, no
emission):** with only the broker and the controller holding
`/dev/glowforge` — no stray process pins it (F-6) — a `SIGKILL` of the
controller mid-move is reaped by the supervisor, which writes `cnc/stop`
+ `cnc/laser_latch=1`, unlinks the homing anchor, and respawns a fresh
controller in about a second with the latch never unlocking (F-3); a
`SIGSTOP` (hang) mid-move drains the ring into a kernel `pulse data
underrun; position no longer trusted`, halting motion fast with the latch
locked while the cooling engine's report-silence clock runs past its
window; and a `forgectrl restart` mid-move leaves the busy controller
running (reparented), lets the move finish uninterrupted, never unlinks
the cooling verdict, and has the new daemon stand by and retake at idle
(F-12). The liveness probe's designed skip-on-open path — the safety-chain
output is known to de-assert during motion, so an at-that-moment read can
skip the probe, proceed without a motion fault, and re-probe on the next
spawn — was exercised and behaved per `liveness.c`. **GATE A kernel drills PASS on this image (operator present, HV
unpowered, software witnesses — the bit-to-pin correspondence was
scope-pinned 2026-08-02):** run with forgectrl stopped so the pulse
device is free (`scripts/bench/gate_a_kernel_drills.py`). K1: a
controlled stop from the 10 kHz cloud tick decelerates in 0.091 s
(theoretical ramp 0.072 s) to `idle` with no max-rate burst and no
fault. K2: with the latch locked, a `stop` + `resume +200` replays a
2 s FIRE window with `laser_enable`/`laser_on` at 0 throughout and
interlock pinned at 13 — the waypoint provably completed (the position
counter advanced all 1000 masked steps; `motor_lock` masks the output
drive, not the counters). K3: `laser_latch=0` written inside the accel
ramp drives the latch pin (interlock 13→5, bit 3 clear) but the FIRE
output drive is never restored while the run is in flight —
`laser_enable` 0 for the entire 3.5 s FIRE-bit stream. `fire_test.py`
A/B/U reproduce the 2026-08-02 reference on the rebuilt kernel: A
(latch locked) pins interlock at 13 through 40,000 FIRE bits; B (latch
unlocked, chain unarmed) shows `laser_enable=1`/interlock 7 mid-window
with `laser_on`/`laser_on_sampled` 0 — the safety AND-gate holds; U
reaches a true underrun, the backstop drops FIRE, and `stop` acks it.
**GATE A IS CLOSED**: every Phase 1 row is fixed, the G-1 assertion is
green in CI, and the drills above are the bench log. Live fire is
permitted again. The masked K2 steps leave the un-anchored X counter
offset (+1000 steps); `homed:false` already enforces the re-home.

### The live defect the campaign caught

**The campaign caught a live defect (fixed same day):** the liveness
probe's enclosure guard read the combined-doors EV_SW bit with the
sense inverted (bit 3 set means *closed*, as the controller's switch
map decodes; the guard treated set as *open*), so the probe skipped on
every spawn with the lid closed — and would have moved the gantry with
it open. Verified live against `EVIOCGSW` (lid closed, bit 3 = 1,
probe reporting "door/interlock open"). Fixed in forgectrl `424f185`
and hot-deployed; on the next start the probe genuinely ran and the
supervision behaved exactly as designed: a first gray-zone read (head
accel p2p x=455, below the ≥500 moving threshold) was treated as NO
MOTION and re-probed rather than false-passed, and the second probe
returned MOTION OK (p2p x=3919, y=1636) — the DRV8825s are not wedged
after the drill session's rail cycles.

### X-2 connection-flood robustness

**X-2 connection-flood robustness exercised (dry):** a 500-connection
slow-drip flood from a second LAN host drove forgectrl from 7 to a peak
of 379 open fds, where it plateaued — MHD's own connection handling
caps concurrency far below the raised 4096 `RLIMIT_NOFILE`, so the
flood could not manufacture the EMFILE that the X-2 fix guards against.
The daemon never crashed, the kernel `cnc/state` stayed readable
throughout (two local `/status` probes timed out at the peak and
recovered within a second), and it returned to 7 fds with `/status`
`200` after the flood drained. The fail-closed branch itself
(`machine_is_idle()` returns busy on any `rd_attr` failure) is now
covered by a host unit test in forgectrl CI
(`tests/status_idle_test.c`, X-2): it points the sysfs reader at a temp
tree via a `GF_SYSFS_ROOT` seam and asserts not-idle on a missing state
file and under real fd exhaustion (`EMFILE`) — the connection-flood
trigger the runtime flood cannot reach while MHD caps connections below
the fd limit. Test-the-test verified: a fail-open revert fails it. Note for F-15/X-6: the
absence of an explicit `MHD_OPTION_CONNECTION_LIMIT` + per-IP cap is
still the deferred half; the default ceiling held here but a per-IP cap
remains the right hardening.

### Idle-CPU diagnosis and the pacing fix

**Idle-CPU diagnosis + pacing fix (2026-08-14).** The controller was
found at ~28% CPU while the machine appeared idle. Traced to grblHAL
being parked in the safety-door state (`Door:0`) — entered when the lid
was opened for inspection between drills, and held there awaiting a
cycle-start even after the lid closed. In any state other than
`STATE_IDLE`/`STATE_ALARM` the driver's `serial_wait` took the 200 µs
segment-production pace, so a parked Door (or Hold) busy-spun the
protocol thread. Not a regression in the audit work; the parked-state
pacing had always been tight. Fixed in grblHAL `b2cad8d`
(`motion_parked()`): a completed feed hold, a parked door (ajar or
closed), and sleep now take the coarse idle poll, while the motion
sub-phases (`Hold_Pending` decel, `Parking_Retracting`/`Resuming`) keep
the tight pace. Hot-deployed; pin bumped and fetch-verified.
**Bench-validated dry (`scripts/bench/pacing_test.py`):** idle 2.7%,
active move 35% (tight, segments flowing), parked `Hold:0` 2.7% (was
~28%), parked `Door:1`/`Door:0` 3.0% (was ~28%), and a mid-move
feed-hold→resume preserved position exactly (30.000 mm, no lost steps —
the feeder never starved through the decel and resume ramps). This pin
bump also rides P10's grblHAL CI/tests and the mlockall-root-only change
into the next image.

### Live-fire drills

**Live-fire drills PASS (operator armed, S400/40% vector marks on
scrap, `scripts/bench/live_fire_drills.py`):**

- **Phase 5 A-1 emission witness — PASS.** On a commanded fire window
  `cnc/laser_on_sampled` (surfaced as `/status` `laser.emission_samples`)
  goes to its full 255 count and returns to 0 at Idle, across two
  separate burns. This is the reliable live-emission witness.
- **Phase 5 A-5 HV telemetry — PASS.** `pic/hv_current` (`hv_current_raw`)
  tracks the cut: 0 at idle, 0→1023/661/482 raw during the three burns
  (the tube draws real current). The only HV witness on this PSU —
  `hv_voltage` is grounded, as the audit noted.
- **Phase 5 A-2 lid IR — characterized, gate left watch-only.** A 40 %
  vector cut lifts the four `pic/lid_ir` channels only ~+3 counts over
  the ambient baseline (37/36/40/40 → peaks ~40/39/42/43) — barely above
  the ±3-count ambient noise, i.e. a weak fire signal at this power.
  `cool_fire_ir_delta` therefore stays 0 (watch-only) until a
  representative high-power job is characterized; a real ignition flare
  is far brighter than a cut, so the eventual threshold sits well above
  both the cut delta and the noise (a floor near 15 counts is the
  working target, not yet committed). forgectrl's per-job telemetry line
  logs baseline/peak for all four channels.
- **pgood is not a usable witness on this PSU.** `cnc/laser_pgood_sampled`
  stayed 0 (forgectrl reads <128 as "not good") through every burn even
  while `hv_current` rail'd and the tube cut — so A-1's "surface
  laser_pgood loss" warning is a false alarm on this hardware and must
  be gated/suppressed here (or documented as expected); the emission and
  HV witnesses are the trustworthy ones. Recorded for the A-1 follow-up.
- **Phase 4 X-3 job-based disarm — PASS.** A job ending in `M2`
  (program end, as LightBurn sends) disarms in **0.1 s** at Idle; a job
  with no program end falls back to the ~60 s `laser_disarm_s` idle
  grace (measured 56.8 s). The window is job-based, not 60-s-idle-based.
- **Phase 4 G-10 disarm-in-Hold — PASS.** Armed, fired a +X move,
  feed-held mid-move (`Hold:1`); the disarm grace counts down while held
  and closes the window at 61.3 s (the bug left a job abandoned in Hold
  armed for hours).

### Closed by host unit test instead of a bench drill

**Closed by host unit test instead of a bench drill:** G-4 (the arm
must re-check `gfcool_fire_ok()` after the button wait — the verdict can
go bad during a wait that runs for minutes) now has a grblHAL CI test
(`tests/laser_arm_test.c`) that includes the driver source, stubs the
core, and drives the real `gflaser_arm()` with a good-then-bad verdict
sequence, asserting the arm refuses at the post-wait re-check (latch
locked, window never opened, alarm raised). Test-the-test verified:
removing the re-check fails it. This is cleaner than the bench drill,
which needed the pump killed in the instant after the press. **Still
config-dependent, left as-is:** Phase 6's "armed job refuses at the
stale origin after an underrun" (GRBL mode permits unhomed cutting), and
the core underrun behavior — `pulse data underrun; position no longer
trusted` with the homing anchor unlinked — is already logged in the dry
dead-man drills above. Live fire only with
the operator armed: eye protection, fire watch, exhaust running.

### Lid-IR ambient baseline

The lid-IR **ambient baseline** for the fire-watch characterization is
captured on this image (600 samples over 5.6 min at 2 Hz, lid closed,
machine idle, coolant ≈25 °C): `lid_ir_1..4` read 37.3 ±0.6, 36.3 ±0.6,
39.5 ±0.7, and 40.0 ±0.6 raw counts (total spread ±3 counts),
`hv_current` reads 0 throughout, and the emission witness
(`laser_on_sampled`) read 0 on all 600 samples — the idle plumbing for
the emission/fire/HV evidence is verified quiet end to end (`/status`
carries the sensed rows; `/cool/status` reports `fire_watch:"watch"`).
Dataset: `scripts/bench/lid_ir_ambient_baseline.csv`. When the fire
characterization sets `cool_fire_ir_delta`, it must land comfortably
above the worst normal-cut peak delta and never below ~15 counts, so
ambient noise can never trip the fire abort. The three pending GATE A
kernel drills are scripted and staged on the bench
(`scripts/bench/gate_a_kernel_drills.py`): K1 proves the
controlled-stop deceleration floor at the default cloud tick, K2
proves a resume waypoint honors the locked latch through a replayed
FIRE window, and K3 proves a mid-ramp latch unlock never re-arms the
FIRE drive — each with software witnesses (`laser_enable`, `laser_on`,
`laser_on_sampled`, interlock bit 3) plus the PSU-connector LASER_ON
scope point, run with forgectrl stopped so the pulse device is free.

### Bench session 2026-08-15

**Bench session 2026-08-15 (image `20260815105250`, operator present) —
two live-fire findings closed, one real defect found and fixed.**
Lid-IR characterization at cutting power: three 30 mm squares on scrap
(S1000 F300, S1000 F150, S800 F600); the engine's per-job telemetry read
run-start baseline → peak `58/59/64/63 → 62/60/66/66`, `56/56/61/62 →
60/61/66/68`, `56/55/61/63 → 61/60/65/66` — a worst normal-cut rise of
**+6 counts** on any channel, against ±3 counts of ambient noise. Ambient
that day read ~57–64 vs 37–40 on 08-14 (day-to-day drift ≈ +22 counts),
which is why the gate keys off the run-start baseline and never off an
absolute level. `cool_fire_ir_delta = 15` is the sized gate (≥ 2× the
worst cut rise, at the ~15-count floor); it is a hand-edited
`/data/forgefirm.conf` key, **set on the bench 2026-08-15** (verified
present, file 0600), and takes effect at the next run start — the fire
watch is armed from here on and the next real jobs are the false-trip
watch. **Flame
signature, measured the same day (machine idle):** a small candle burning
on the bed under the closed lid read `38–41 / 38–41 / 42–45 / 42–45`
against a lid-open level of `36 / 35 / 38 / 39` and a lid-closed-empty
control of `34–37 / 34–36 / 36–39 / 37–40` — closing the lid changes
nothing, the candle is **+3 to +6 counts on all four channels** for as
long as it burns. That is the same size as a full-power cut's rise, so a
threshold cannot separate a candle-sized flame from cutting and the
15-count gate will not react to a flame that small; what a material fire
of a size worth stopping for produces is unmeasured. **Then the decisive measurement, dry, the same
day: the lid-IR channels track the lid LED.** `lid_led` 0 → `2 2 1 2`,
8 → `2 2 3 2`, 131 (the resting level) → `54 55 61 62`, 255 → `172 171
190 188`. The sensors are, first of all, a photometer for the lid lamp;
every rise measured above (cuts +4–6, candle +3–6, the "+22 drift"
between sessions) is a small modulation on a lamp-set level. forgectrl's
camera engine drives `pic/lid_led` for every lid capture (132 during the
grab, previous level restored), and the resting level is not fixed (131
here, 8 after one reboot, cloud mode sets its own `LLvl`) — so a snapshot
mid-run can step every channel by tens of counts and a fixed-count gate
fires a phantom FIRE stop. **`cool_fire_ir_delta` was therefore set back
to 0 (watch-only) the same day**; the gate stays disabled until the fire
watch is lamp-aware (Next work item 10). **Armed kill on
the expected-stop path — first run FAILED, defect fixed, re-run PASS.**
With emission live, `POST /controller/stop` returned only after 5.30 s
and the operator saw ~17 mm / ~5 s of continued cutting before a
decelerated stop: the supervisor's SIGTERM was honored by the controller
as "exit once motion is done" (`driver.c` exited only outside
CYCLE/JOG/HOMING), so the job ran on until the 5 s SIGKILL escalation
and the exit safing (`escalating to SIGKILL`, exit status 0x9). Fixed on
both sides and bench-proven the same session: forgectrl `3edb7bd` writes
`cnc/stop` + `cnc/laser_latch=1` **before** the SIGTERM (kernel-level,
instantaneous, no-op when idle); grblHAL `5960f05` treats SIGINT/SIGTERM
during motion as `^X` (controlled decel, latch relocked, alarm) and exits
on the next pass, with the handler kept installed so the supervisor's
second SIGTERM cannot hard-kill it mid-cleanup — CI case
`sigterm-mid-job` (exit in 0.10 s; the old logic fails it). Re-run with
the new binaries installed: POST returned in 0.46 s, `grbl controller
exited (status 0x0)` with no SIGKILL, kernel `idle` and `armed:false` at
the first post-stop sample, emission gone within the counter's ~1 s
window; the operator saw ~1 s / a few mm of cut, then the stop. Also
found and fixed: `auth.c` read `X-ForgeFIRM-Token`/`Host`/`Origin`/
`Sec-Fetch-Site` case-sensitively (a title-casing client was refused);
now `u_map_get_case`. Bench tooling for the session is committed
(`scripts/bench/platform_drills.py`, `live_fire_drills.py` `ircut` /
`expstop` / `ctrlstart`, `fdscan.sh`). Session rules, now standing: one
live-laser run per turn with the operator's confirmation before the next;
only observations, never inferences, in live-fire reporting.
**Dry drills the same session, all PASS on the board:** decay/microstep
readback per axis (every value reads back, out-of-range `3` refused
`EINVAL`); dead-man trip readback (closing the flock'd fd mid-run →
`closed while locked and driver is running! Emergency stop`, `pic`/`head`/
`thermal: making safe`; heater and TEC off, measure laser, UV LED and Z
driver off, pump/exhaust/intake/air-assist **unchanged**); three
`rmmod`/`modprobe` cycles with a thread reading state/position/faults/
hall_sensor throughout (6618 reads served, 14162 refused while unloaded,
no oops/BUG/WARNING); the LED sequence (all bright / all dark / button
pulse 300 ms / restore) behaved as commanded, operator-witnessed;
the module's probe lines read `EPIT clock 66000000 Hz` and `SDMA channel
26 reserved for pulse playback (script at halfword 7680)` with no bank
warnings; forgectrl's helper children (`curl` during `/update/check`, the
snapshot path) never hold a pulse-device descriptor — only the controller
does; a `$H` gfcloud homing session completed in 56 s with 7 accelerometer
motion windows above the 500-count threshold at the ~100 Hz sampler
(anchor written, `H:1`); a kernel panic (`sysrq c`) mid-move stopped
motion instantly (operator-witnessed) and the board rebooted on `panic=10`
into a healthy state (liveness MOTION OK, controller running, latch
commanded locked). Observed once, cause not established: after the three
module reloads the first liveness probe read NO MOTION (p2p 343/241); the
ladder's rail-off/re-probe recovered it (p2p 3466/2163) — a module reload
resets the analog configuration, and the ladder exists for this.
**Head-absent negatives (head unplugged, machine powered up):** the head
driver fails probe (`head not detected`) and the whole `head/` sysfs
group is absent, so every head attribute reads as missing rather than
as a number; neither the daemon nor the controller logs anything
repetitive with the head gone; the liveness probe skips (`head
accelerometer not found`) and the controller starts. Three findings,
fixed and re-proven the same session: `/status switches.head` was EV_SW
bit 7 raw (reads `true` with the head unplugged) — now real presence
(the head group exists) and it read `false`; `/mode` said `motion:
"verified"` after a probe that could not run — now `"unverified"`
(forgectrl `73eda9a`); and **nothing gated arming on
head presence** — the GRBL controller now refuses the first laser-on
of a job when the head group is absent, before the latch unlocks and
before the button lights (grblHAL `91807a2`, "laser fire blocked: no head
detected" + `ALARM:3`, operator-witnessed: the button stayed dark).
The K-11 runtime-I²C-error case (a present head answering badly) and
the C-3 failed-head-capture case are not reachable with the head
unplugged and stay open.

### Interlock latch, charge-pump watchdog, hv_enable rename

**Interlock latch has no hardware trip path in ForgeFIRM (found
2026-08-15, bench-verified).** With the interlock connector unjumpered
at idle: EV_SW `interlock`=1 (loop open), `interlock_latch`=0 (not
tripped), `cnc/interlock_circuit`=13 (b4 INTERLOCK_RESET=0),
`interlock_latch_reset`=0. This matches the safing schematic: the
interlock latch (U23-2, CD4043B) has RESET = loop-closed and
SET = INTERLOCK_RESET (GPIO4_05) — an open loop only *releases* the
reset, and nothing in ForgeFIRM drives INTERLOCK_RESET (the driver
exposes it as a read-only readback, initialized low; the former
`interlock_reset` LED node that let userspace drive it is gone). So on
a machine with a real external lockout (Pro), an open loop does **not**
cut LASER_ON in hardware; enforcement is the GRBL safety-door hold on
switch code 5 and the cloud client's motion gate. Basic/Plus ship the
loop jumpered. **Decision + fix needed:** drive INTERLOCK_RESET high
whenever the loop is open and hold it until the loop closes, so Q2
blocks the LASER_ON gate in hardware (the CD4043B is set-dominant, so
the latch stays blocked until the SoC releases SET *and* the loop is
closed). **IMPLEMENTED 2026-08-15 (kernel-module, code-complete, bench
validation pending; kernel-module 015913b, meta-openglow 92d6e20 DTS +
897c175 pin, forgectrl a451e7c docs, all pushed and pins bumped
2026-08-15):** `src/cnc_interlock.{c,h}` — an in-kernel input
handler on the gpio-keys switch device (no DT change, GPIO stays with
gpio-keys) drives INTERLOCK_RESET high while EV_SW code 5 reads open,
from probe until the switch device attaches, and if it detaches
(unobservable = open); low only while an attached device reports the
loop closed. Pin init changed to `GPIOF_OUT_INIT_HIGH`. Proof so far:
host test `tests/interlock_test.c` (8 cases, `make -C tests check`,
new CI job `host-tests`) green; module cross-compiled clean against
the staged 6.12.20-fslc kernel with `KCFLAGS=-Werror`, MODPOST silent.
Ships with the next image flash (kernel changes are never hot-swapped);
bench re-run of this exact reading then expects `interlock_latch`=1 /
`interlock_circuit` b4=1 with the loop open, both clearing after it is
closed. **BENCH-VALIDATED 2026-08-15 on image 20260815150546:** loop
pulled → `interlock`=1, `interlock_latch_reset`=1, `interlock_latch`=1,
`interlock_circuit` 45→61 (b4 set), all within one 50 ms sample;
reinserted → all clear the same way. Side effect to know: the pull is
a grblHAL safety-door hold — the controller sits in `Door:0` after the
loop closes until a cycle start (`~`) returns it to Idle (a client
connecting then sees Door, not a dead link). Same batch: the charge-pump
watchdog readback (`cnc/charge_pump_alive`,
`interlock_circuit` b5; GPIO1_08 = inverted one-shot Q, new
`charge-pump-alive-gpio` + GPIO_8 pad in the linux-fslc DTS — kernel
module and DTB must ship together, the pin is required at probe; DTB
compile-checked with cpp+dtc against the staged kernel) — **also
bench-validated 2026-08-15:** two X jogs sampled at 50 Hz: `state`
running → `charge_pump_alive` 1 and `estop` 0 (pre-rename name and
polarity of today's `hv_enable`) in the same 20 ms sample;
after each run `charge_pump_alive` fell 0.325 s / 0.326 s after `idle`,
which with the 200 ms feed phase (last pulse 0.136 s / 0.118 s before
the run end) is a one-shot period of **0.46 s / 0.44 s** — matching
the measured R·C (≈500 kΩ × ≈900 nF = 0.45 s); `estop` re-asserted
with the drop both times, i.e. HV_ENABLE = DOORS_OK · WDOG_ALIVE
observed live. Full write-up of the chain: `docs/SAFETY.md`
(+ `docs/img/safety-chain.svg`).

### LightBurn door-open handling

**LightBurn door-open handling — CLOSED by item 16.** The lid no
longer parks a job in `Door` on the default policy: it cancels the job,
ends the sender's stream with a clean reset and returns the head to the
job start, so LightBurn never lives in `Door` and the Resume convention
it used to need is gone. The `Door` residency that remains under
`lid_policy = hold` is covered by `motion.lid-policy-hold` (lid parks the
job, cycle start after the lid closes finishes the move with its position
intact), bench-validated 2026-08-17.

### uSDHC pad strength brought to the factory values

**uSDHC pad strength brought to the factory values (DTS change
2026-08-15, bench validation pending — ships with the next full image
flash, per the batched kernel/BSP rule).** Trigger: one
`wl1271_sdio mmc0:0001:2: sdio write failed (-84)` (`-EILSEQ` = SDIO
bus CRC error) on the WL1805 Wi-Fi bus at 49.5 MHz SD-high-speed,
followed by wlcore's designed hardware recovery (firmware reboot +
reassociation, ~1.0 s of Wi-Fi outage) and one `ipu1_csi0: NFB4EOF`
160 ms later (a consequence of the recovery/WARN console burst, not a
co-cause). It happened at idle, 1.7 s after a kernel run ended and
~2 s after a button press — no motion, no fire, HV_ENABLE already
down — so nothing points at laser or stepper EMI. Rate observed:
1 event in 49 min of uptime. Effect if it lands mid-job: a 1–2 s
sender stall (planner drains, head pauses; laser off in M4 mode) —
a cut-quality nuisance, never a safety matter (nothing safety-relevant
crosses Wi-Fi). Finding: `glowforge.dts` drove all three uSDHC
controllers with `0x17019` (SPEED_LOW, DSE 80 Ω, 47 kΩ pull-up on
CLK too), while the factory DTB uses `0x17069`/`0x10069` (SPEED_MED,
DSE 48 Ω; no pull on CLK) for the Wi-Fi bus and `0x17059`/`0x10059`
(80 Ω) for eMMC and SD (SD2_DAT3 `0x13059`) — softer edges than the
factory at the same 50 MHz clock. `openglow_common.dtsi` now carries
the four factory-exact values (`USDHC_PAD_CTRL`, `USDHC_CLK_PAD_CTRL`,
`USDHC_SDIO_PAD_CTRL`, `USDHC_SDIO_CLK_PAD_CTRL`) and the compiled
`fsl,pins` tuples were checked byte-identical to the factory DTB's
`glowforge_usdhc1/2` and `usdhc3grp`. **Bench:** on the next image
confirm `pinconf-pins` reads `0x17069`/`0x10069` on SD1, eMMC and
Wi-Fi come up, then watch `dmesg | grep -c "sdio .* failed"` across
sessions (baseline: 1 per ~49 min). Only if it still recurs, cap
the bus with `max-frequency = <25000000>` on `&usdhc1` (halves Wi-Fi
throughput — last resort; the factory ran 50 MHz on these pads). The
`WARNING … wlcore/main.c:874 wl12xx_queue_recovery_work` block that
accompanies the event is upstream noise (an "unintended recovery"
`WARN_ON`), not a crash — the `-84` line is the signal to watch.

### Unified logging — bench validation

**Unified logging — CODE-COMPLETE, host-verified, pushed and pinned
2026-08-15; bench validation pending — ships with the next full image
flash (rsyslog replaces busybox syslogd/klogd, so it is an image
change).** Design and contract: `forgectrl/docs/SERVICES.md`
"Logging". In brief: rsyslog is the only log writer; forgectrl and
the grblHAL driver emit through the shared non-blocking `fflog`
emitter (drops, never waits — a stalled log daemon can never park a
controller thread), gfcloud/gfhome through `SysLogHandler`, the
kernel through `imklog`; a controller's stray stdout/stderr rides a
per-controller `logger` relay under its own name; the daemon's own
stray output a fifo relay in its init script. Tree:
`/data/log/forgefirm/{forgectrl,grblhal,gfcloud,gfhome,kernel,system}/`,
size-capped and rotated (`forgefirm-logging` recipe: renders the
rsyslog rules from the settings at S19 via `forgectrl
--render-syslog`, sweeps the pre-syslog files once into
`/data/forgefirm/legacy-logs/`, logrotate at boot + hourly with a
`HUP`, never `copytruncate`). Levels: `log_<logger>_disk` /
`_remote` and `syslog_server/port/proto` in `/data/forgefirm.conf`,
**applied at reboot** (the panel's Logs tab shows configured vs.
effective and offers the reboot); a process emits at the more
verbose of its two levels, rsyslog filters per destination. Export:
`POST /logs/export` streams a `tar.gz` (tree + system snapshot),
sanitized by default (`src/sanitize.c`: known values first — serial,
hostname, cloud credentials, panel token, WiFi SSID/PSK — then
patterns; stable placeholders; `tests/sanitize_test.c` in CI, 39
fixtures). Host proof done: forgectrl/grblHAL `-Werror` builds and
all three CI test sets green (sanitizer, idle fail-closed, switch
map, arm re-check, laser stream + armed-window harnesses on the
null-sink build); `tests/fflog_e2e.sh` against a private rsyslogd on
the shipped `rsyslog.conf` (emitter format, per-logger routing,
level filtering, `logger` relay routing) and the equivalent Python
check both pass; `/logs`, `/logs/tail` (full + incremental follow),
and both export variants exercised over HTTP on a host build and
the panel's Logs tab driven in a browser (levels table, viewer,
follow, export). **Bench, on the flashed image (dev image
`20260815191634`, flashed and booted by the operator 2026-08-15):**
- ~~boot~~ **DONE 2026-08-15**: `S19forgefirm-logging` → `S20syslog`
  → `S90forgectrl`, `K80`/`K90`/`K95syslog`; `rsyslogd` up, no
  busybox `syslogd`/`klogd`; rules and `/var/run/forgefirm-loglevels`
  rendered (all defaults); six directories under
  `/data/log/forgefirm`; `/var/log/messages` gone; legacy files
  moved to `/data/forgefirm/legacy-logs/` (`forgectrl.log`,
  `forgectrl.log.old`, `gfcloud.log`, `gfcloud/`, `gfhome/`),
  `/data/log/gfcloud` and `/data/log/gfhome` gone, the factory's
  `/data/glowforge.log*` untouched; the forgectrl fifo relay and the
  grblhal relay both running (`logger` ×2, `/var/run/forgectrl.stderr`).
  The swept legacy files (10.6 MB) were deleted from the bench on
  2026-08-15 once the new tree had proven itself; the sweep itself
  stays in the init script for any board upgrading from before the
  syslog tree.
- ~~routing~~ **DONE 2026-08-15** for GRBL mode: forgectrl lines
  (`super: liveness probe: MOTION OK …`, `NOTICE super: started grbl
  controller`) in `forgectrl/forgectrl.log`; grblHAL's (`gfstream:
  pulse device inherited from the broker`) in `grblhal/grblhal.log`;
  the whole boot ring (350 lines, `glowforge_cnc cnc: 40V on` …) in
  `kernel/kernel.log` with correlated timestamps; sshd/rsyslogd in
  `system/system.log`; `logger -t grblhal` / `-t gfhome` probes land
  in the right files tagged `grblhal[-]` / `gfhome[-]` (the relay
  path). `/logs`, `/logs/tail` and the sanitized export served over
  the LAN: the bundle carried `<SERIAL>` ×2, `<IP-1>` for the LAN
  peer (sshd `Accepted … from <IP-1>`), MACs and e-mails redacted,
  no LAN address anywhere in it. Still open: cloud-mode routing
  (`gfcloud/gfcloud.log` + a Python traceback via the relay) and a
  `$H` for the gfhome lines. Found and fixed the same day: rsyslogd
  warned at start that the fallback rule after the include was
  unreachable (the rendered rules end in `stop`) — the default rules
  now come from the init script when the render leaves none
  (forgefirm 7487f90, next image).
- ~~levels~~ **DONE 2026-08-15** (three reboots): `forgectrl`/`grblhal`
  → `debug`: `pending_reboot:true` before, effective after; the per-run
  `gfstream: run:` DEBUG stats appear on jogs; `forgectrl` → `warning`
  + `grblhal` → `off`: the new boot wrote zero NOTICE/INFO forgectrl
  lines and kept a WARNING probe, grblhal wrote nothing even for an
  `err` probe, kernel/system unaffected; defaults restored and
  re-verified. ~~Remote~~ **DONE 2026-08-15, real hop to a LAN
  collector (172.16.1.95:5514) over UDP and TCP** (after a first pass
  on a loopback listener): RFC 5424 lines arrive (`<31>1 …
  glowforge grblhal - - - …`), filtered exactly per logger across a
  whole boot (kernel at warning only, forgectrl/grblhal at info,
  sshd from `system`; a gfhome err and a forgectrl debug probe held
  back). Collector down through an entire boot on TCP: `omfwd
  suspended … Connection refused` in `system.log`, the machine
  unaffected (jogs, local logging), and 30 s after the listener came
  up `omfwd resumed` and the queued boot lines were delivered. Note
  for future probes: `busybox nc -u` on the board never sends — use
  `python3 … sendto`; my first "the workstation drops inbound UDP"
  reading was that false negative.
- ~~rotation~~ **DONE 2026-08-15**: a 30 000-line burst (4.8 MB) into
  `grblhal`, one `logrotate` run → `grblhal.log.1.gz` (all 30 024
  lines), the live file recreated and receiving (rsyslogd's fd on the
  new inode). The imuxsock per-pid rate limit did not engage for
  `logger` bursts (each line is a new pid) — it bounds a single
  runaway process only, as intended.
- ~~export~~ **DONE 2026-08-15**: both variants downloaded over the LAN;
  the sanitized bundle carries `<SERIAL>`, `<IP-n>`, `<MAC-n>` and no
  LAN address, the full one has them; staging empty afterwards.
- ~~RT~~ **DONE 2026-08-15**, with a finding that is NOT logging: X
  jogs (F600/F1200, ±5 mm) with `grblhal` at debug: without a camera
  stream `max behind 0–5.8 ms, clamped 0`; with the lid stream running
  steady, `max behind 6–19 ms, clamped 0–11` per run — and the same
  with rsyslogd frozen (SIGSTOP) during the runs (`clamped 0/1/0/10`),
  so the producer clamping under a live stream is the stream's CPU
  load, not the logger (no underrun, the shipper is unaffected). The
  2026-08-03 baseline said `clamped 0` at F1200 with a stream —
  re-check under item 1/10 (VPU stream + cooling engine + telemetry
  polling all landed since).
- ~~stop/start~~ **DONE 2026-08-15**: `kill -9` of the daemon → the
  wrapper's `forgectrl[-] ERR exited (137) - respawning in 5 s` lands
  through the fifo relay, the respawned daemon stood by, took over the
  unmanaged controller, re-probed motion and restarted it; a mode
  switch to cloud put gfcloud's lines (`ffmachine:_lid_image …`,
  `websocket:img_upload COMPLETE`) in `gfcloud/gfcloud.log` with a
  per-controller relay alive, and back. A `$H` (web-service homing,
  58 s, homed X0 Y0 Z10.60) put gfhome's session lines in
  `gfhome/gfhome.log` under its own pid and grblHAL's `starting
  homing session` / `homed` in `grblhal.log`. **Item closed.** Not
  separately drilled: a Python traceback through the relay — there
  is no external trigger for that; the relay pipe is the same one the
  `logger` probes and the wrapper's `exited (137)` line went through.
  Acceptance catalog: `logs.tree-tail-export` (list, tail, sanitized
  export with the token-leak check), `logs.routing` (one logger
  daemon, rendered rules and effective record consistent with
  `/logs`, the tree, the daemon's own emitter line, `logger` relay
  probes routed by name in the ff_line format, a stray program only
  in system/, kernel lines, relay processes, nothing outside the
  tree) and `logs.level-settings` (bad level/port/proto/server
  refused, a level change configured-not-effective with
  `pending_reboot`, restored) — all three PASS on the bench
  2026-08-15 through the real Runner against an isolated results log
  (the image's forgetest still carries the older catalog until the
  next dev image). Finding from that run, fixed: the sanitized export
  took 13.9 s on the target (0.95 s unsanitized) and tripped the hw
  client's 10 s default — the export call now has its own timeout
  and the sanitizer skips a pattern pass when the line cannot match
  it (4x faster on the host; forgectrl 4d19e9d). **Images
  `20260815215236` (forgefirm-image, 192.7 MB rootfs) and
  `20260815215332` (forgefirm-image-dev) are built on that pin with
  the three logs tests in the dev image's catalog** — the next flash
  carries the fast sanitizer, the init-script default rules, and the
  catalog; nothing else in the logging system is pending.

### Outstanding bench validations, as consolidated 2026-08-15

**Outstanding bench validations (consolidated 2026-08-15).** Every
safety-critical drill is done: GATE A (K1/K2/K3, `fire_test` A/B/U),
GATE B (auth/CSRF/loopback/settings-flood probes), dry motion and
dead-man drills (SIGKILL reap+safing, SIGSTOP → underrun, restart
mid-move, no stray fd), the X-2 flood, and the live-fire set (A-1
emission witness, A-5 HV telemetry, X-3 job-based disarm, G-10
grace-in-Hold, A-2 lid-IR first look). What has **not** been run on
hardware, none of it gating, in rough priority order:
- ~~**Lid-IR fire characterization at cutting power**~~ — **DONE
  2026-08-15** (three cutting-power jobs, worst rise +6 counts,
  `cool_fire_ir_delta = 15` set by hand in `/data/forgefirm.conf`).
  **Then disabled again the same day (`cool_fire_ir_delta = 0`)**:
  the channels track the lid LED (0→2, 131→~58, 255→~180 counts),
  so any lamp change during a run — a panel snapshot lights the
  lamp — steps them by tens of counts and a fixed-count gate would
  stop the job on a phantom FIRE. Redesign before re-arming: the
  engine must own or observe the lamp level (suspend the watch and
  re-baseline for a few ticks after any `lid_led` change; forgectrl
  drives it for captures, the cloud client for lid images), and the
  threshold should be relative to the lamp-set level, not a fixed
  count. Even then the signal is weak (a candle reads like a cut);
  the head camera or a real flame sensor is the honest path to fire
  detection that means something.
- ~~**Kernel platform-hygiene batch (item 9), on the flashed
  image**~~ — **DONE 2026-08-15** (panic mid-motion, decay/microstep
  readback, LED sequence + clean unload, probe lines, dead-man head
  readback, concurrent `cat` during `rmmod` — session record above).
  Still needing a debug kernel build: load/unload under
  `CONFIG_DEBUG_MUTEXES` and a forced `-EPROBE_DEFER` unwind.
- ~~**Dead-man collateral**~~ — **DONE 2026-08-15**: the trip leaves
  pump and airflow running (readback drill); helper children never
  hold the pulse device (fd-scan during `/update/check` + snapshot);
  the armed kill on the *expected*-stop path failed first (5 s of
  continued fire), the defect is fixed on both sides, and the re-run
  passed. The literal "kill forgectrl mid-download" variant needs a
  published `.fw` to download and was covered by the fd-scan instead.
- **Physical-evidence negatives:** ~~head absent at power-up~~ **DONE
  2026-08-15** (head group absent → no readings, arm refused, presence
  and motion labels fixed). Still open: a present head answering I²C
  badly (the K-11 runtime case) and a failed head capture leaving the
  measure laser off — both need the head connected and a fault
  injected.
- **Cloud mode — mostly DONE 2026-08-15:** mode switch clean (GRBL
  controller exit 0x0, gfcloud signed in, connect-time hunt + lid
  image ran); **network/DNS blip** (service peers blackholed + dead
  resolver for 75 s while the session was live): `ping/pong timed
  out - goodbye` → in-process `RECONNECTING`, sign-in retried with
  backoff through the outage, `authenticate_machine SUCCESS` and the
  service's `settings` action answered right after restore, same
  process, supervisor never involved — PASS; **a real print** (22.9 s,
  motion bytes actual = expected, emission peak 91, HV 0..932): the
  header's `AArd 1023 / EFrd 65535 / IFrd 43278` drove air 11.0 k /
  exhaust 11.8 k / intake 4.1 k rpm through the armed window and the
  hunt/Z headers (`204/0/0`) left the fans at idle levels — the
  per-job profile round-trips (directional; duty→rpm not calibrated);
  no false FIRE trip on the job. `$H` witness re-verified (7 windows
  ≥ 500 at ~100 Hz). Still open, not inducible from the bench: the
  cancel-with-a-rejected-`settings`-action case, a malformed frame
  (needs a MITM), the oversize/bad-header job (tracked in `CLOUD.md`).
- **Opportunistic:** `STATE_FAULT` recovery via `enable` without a
  module reload the next time a DRV8825 fault line actually trips.
- **Config-dependent, deliberately not gated:** an armed GRBL job
  after an underrun cuts at the stale origin unless homing is
  required (GRBL mode permits unhomed cutting; the underrun itself
  alarms and unlinks the anchor).

## 2026-08-15 … 2026-08-16 — release acceptance (forgetest)

### Campaigns on the bench

**Bench campaign opened 2026-08-15 on the flashed dev image
`20260815194415` (manifest identity `2d69a61e…`, equal to the release
build's).** The tool came up on `:8090` with all 24 tests required.
Passed that day, driven through the API with the operator present:
`image.health` (kernel options, module + 16 MiB ring, forgectrl holding
`/dev/glowforge`, K80 controllers before K90 forgectrl, 0600 token and
settings, 2.6 GiB free on /data), `kernel.latch-locked-idle` (interlock
`0x2d`, FIRE 0, LASER_ON 0/0, faults 0), `forgectrl.auth`,
`forgectrl.settings-bounds`, `forgectrl.panel-serves`,
`logs.tree-tail-export` (sanitized bundle carries no panel token),
`update.slots-and-signature` - 7 of 24. One finding, on the tool side:
`forgectrl.auth` first failed because it expected `/fuse-identity` to
answer 200 to the token alone; the endpoint is two-factor (token AND the
physical button held) by design, so the test now asserts both refusals
and never fetches the identity (a 200 would have put the fuse password
in the result log). That FAIL closed the first campaign, as the rules
say; the second campaign held the passes.

**2026-08-16, dev image `20260815215332` (26-test catalog), campaign
`c-20260816171010-cd59`: 14 of 26 satisfied** - the always-required core
`image.health`, `kernel.latch-locked-idle`, `kernel.k1-k2` (controlled
stop 0.09 s, no burst; K2 FIRE window replayed after the resume waypoint
with laser_enable/laser_on 0 throughout, counters back to start),
`kernel.k3-unlock` (mid-ramp unlock drives the latch pin, FIRE drive stays
0), `kernel.fire-abu` (A: no FIRE drive under the lock; B: FIRE driven,
LASER_ON off with the chain unarmed, FIRE clear at end-of-data; U: true
underrun, backstop drops FIRE, stop acks) and `cooling.flow-verify`
(flow 9.5 / threshold 14.4 / no-flow 16.4 C, margins 4.9/2.0, not
thin), plus `camera.snapshot` (lid snapshot half/full + a stream, operator
confirmed the bed) and the seven forgectrl/logs/update tests, which
re-passed on this image and then **inherited across two campaign
closures** - the domain-scoped inheritance and the always-required core
behaved as specified. Two campaigns closed by test-side FAILs, both
fixed: forgectrl answers a started diagnostic with 202 (the test asserted
200); and the takeover wrapper returned as soon as `forgectrl start`
succeeded, so the next test found the machine busy under the
supervisor's liveness probe (`409 machine is not idle`).

**The important finding of the day (machine-side symptom, tool-side
cause):** after the kernel takeover tests, forgectrl's supervisor reported
NO MOTION on its liveness probe, ran the rail-off ladder (5/15/30 s), and
once ended in `motion-fault` - the driver-wedge signature. The cause was
`cnc/motor_lock=15` left behind by the takeover drills (they mask every
axis and never restored the mask), and the supervisor's probe does not
reset the mask: its steps were masked, so no motion by construction. Real
probes read head-accel p2p 1779-2857; the masked ones 144-480; the two
"MOTION OK" ladder recoveries seen under the mask (p2p 541 and 718)
were false positives against the fixed `>=500` threshold, plausibly the
rail re-energize jolt. Two consequences: (1) **the rule, from the
operator: every test starts from, and leaves, the fresh-boot idle state
(atomic clean start), and the baseline is taken after a reboot** - the
runner now brackets every test and bench tool with a baseline pass
(`forgetest/baseline.py`, contract in `docs/ACCEPTANCE.md`), takes a
fresh-boot reference once per boot, and takeover runs capture the
controller-owned kernel attributes on entry and write them back before
forgectrl restarts; the fresh-boot dump of this image (uptime 235 s)
confirmed the fixed values (`motor_lock 8`, `x/y_mode 8`, `x/y_decay 1`,
`step_freq 28160` - the controller's tick, not the probe's 10000 -
`ramp_rate 125000`, hold currents 33/5, lamps and button LEDs 0, heater
and TEC off). A reference dumped at uptime 30 s on 2026-08-16 showed the
probe values instead (`motor_lock 0`, `step_freq 10000`, `y_mode 1`): the
dump raced the controller's init writes - `/mode` reports `running` at the
spawn, not at the config - so `boot_reference()` now waits for the
controller's markers (`step_freq`/`motor_lock`/`y_mode` at their fixed
values, bounded 20 s) before dumping, and retakes a pre-config reference
while the boot is still fresh. Proof: k1-k2 / k3 / fire-abu re-run under the baseline -
counters (0,0,0) before and after, the probe verified in 3 s after every
takeover, no ladder, `post: clean` every time. (2) Two forgectrl items
for the operator's decision, not changed: the liveness probe should write
`cnc/motor_lock=0` for its move (a leftover mask from any tool must not
read as a wedge), and the `P2P_MOVING >= 500` threshold has little margin
over the noise floor seen today (~480) against a real-move signature of
~1800+ - a settle after the rail-on before sampling, or a threshold near
1000, would keep a false MOTION OK from starting a controller on a dead
machine. Also noted: at a fresh boot in GRBL mode `pic/lid_led` is 0 and
nothing in the GRBL stack lights the lid lamp; the lit bed the bench was
used to is cloud mode's `LLvl=132`, which persists across the switch back
to GRBL - a resting-lamp setting in forgectrl would be a product
decision. (Later the same day the operator power-cycled the machine and
the fresh-boot reference read `lid_led=132`: the PIC lights the lid lamp
at power-on; the dark lamp after my soft `reboot` was the module's remove
path. The reference is therefore taken after a **power cycle** -
`docs/ACCEPTANCE.md`.)

**Same day, after the power cycle, campaign `c-20260816181534-d07a`:
22 of 26 - everything but the four live-laser tests.** The motion group
under the baseline: `motion.pacing` (idle CPU 2.7 %, moving 34 %, parked
2.7 %, hold/resume exact), `liveness-probe`, `cancel-abort` (jog cancel
16.8 mm short of 40, `^X` abort mid-move into Alarm with position
retained, `$X`, return drift 0.000), `jog-roundtrip` (8 jogs, peak 10500
mm/min, hold parked, drift 0.000, operator confirmed the gantry) and
`deadman` (SIGKILL respawn 1.3 s, SIGSTOP -> kernel underrun 0.21 s with
the latch locked, forgectrl restart mid-move: the busy controller
finished unmanaged and supervision was retaken at idle);
`cooling.fans-quiet-after-motion` (idle profile back 30 s after M9);
`cloud.mode-switch` (session established 2 s after the switch, back to
GRBL Idle) and `cloud.gfhome-homing` (`$H`, homed in 50.5 s, corner
confirmed). Tool findings fixed on the way, each a real bench lesson:
grblHAL's Idle precedes the machine's by the stream depth and the decel
tail, so motion tests now end on forgectrl's idle; a soft reset (`^X`)
flushes the controller's read buffer and eats a `?` that lands in it, so
the Grbl client re-sends `?` until a report arrives; a killed controller
still reads as running until the supervisor reaps it (wait for a
different pid); a forgectrl restart mid-move ends in a **replace-at-idle**
- stop the unmanaged controller, hold the device, re-probe, start a
supervised one - because the old inherited fd cannot be adopted
(SERVICES.md now says so; the test expected the same pid); the cloud
session's evidence is gfcloud's own authenticate/ws-connect lines, not
the optional firmware-probe file; cloud mode's connect zeroes the kernel
counters at the head's start and its hunt homes the head 245/139 mm to
the corner - the test tells the runner and jogs the head back. Two
product observations left visible as baseline leftovers: `$H` (gfhome)
and cloud mode leave the lid lamp at 236 (gfhome should hand the lamp
back; the mode-switch test hands it back itself because it caused the
switch), and a mode switch back to GRBL keeps cloud's lamp level. The
first `Idle`-before-motion race also lived in the fans-quiet test's
second wait (harmless there). **The operator then ran the four live tests
from the page - `laser.emission-witness`, `disarm-in-hold`,
`expected-stop`, `kill-mid-fire`, all PASS - and exported: 26 of 26,
*Release authorized*, and `scripts/acceptance-gate.py` authorizes the
image's own manifest with the artifact. That was an exercise of the
release mechanism, not a release: no `releases/v…` directory was
committed.** Two observations from the live runs, both restored by the
baseline: each live test left the head a few mm +X of its start (11 /
2.8 / 3.3 mm - the tests should end on a return jog), and
`kill-mid-fire` left `cnc/streaming=1` (the supervisor's controller-exit
safing writes `cnc/stop` and the latch, not the streaming flag; the
respawned controller manages the flag per run, so it is hygiene, not a
hazard - noted for the safing sequence).

**Same day, the forgectrl changes decided from the campaign - landed,
built in the forge-yocto tree, hot-deployed on the bench (forgectrl
`ff9a7c9` + `c8f6558`, recipe pin bumped in `1d9b553`):** (1) the lid lamp
has a resting policy - the `lid_lamp_idle` setting (0-255, default 236,
Settings > Lid lamp), asserted at daemon start, on a settings change
(live), and at every controller spawn, so a warm reboot no longer leaves
the bed dark and a cloud session's level does not linger; the camera
engine owns the write (a running lid capture applies it at teardown). (2)
The liveness probe writes `cnc/motor_lock=0` for its move (a leftover
mask from any tool must not read as a wedge), settles 300 ms after the
run-current step before sampling (the current step jolts the head), and
the moving threshold is 800 (live >= 1040, typically 1800-2900; the
rail-on / current-step jolt up to ~700). Proof, through the acceptance
tool: `forgectrl.settings-bounds` (lamp resting at 236, 256 / -1 /
"bright" refused, 100 applied at once, cleared back to 236) and
`motion.liveness-probe` (every axis masked, forgectrl restarted, the
fresh probe MOTION OK on the first try at p2p 2047/1341, mask cleared by
the controller's init). The baseline expects the lamp at the setting now
(the boot capture is the record, not the lamp reference), and its settle
waits for the controller to be running - the post pass had run between
the probe's own writes and the controller's init writes once. **Images
`20260816191838` (forgefirm-image, v0.1.0, 192.7 MB rootfs) and
`20260816191951` (forgefirm-image-dev) are built on that tree - forgectrl
`c8f6558`, the day's forgetest, acceptance identity `c72448c2…` equal on
both - and archived under `images/20260816191838/` with checksums (the
previous pair, `215236`/`215332`, under `images/20260815215236/`).**

**2026-08-16, dev image `20260816191951` flashed: every test came up
`domain-changed`, none inherited - the expectation above ("every domain
forgectrl does not touch inherits") was wrong, and the tool was right.**
The two dev-image manifests differ in exactly one platform field:
`platform.layers.meta-forgefirm.content_sha256` (`b2d13d87…` →
`7ef5555d…`); machine, kernel modules and DTB hashes are equal, and the
only components that moved are forgectrl (7 files) and the dev-only
forgetest. The only non-`.md` change in `meta-forgefirm` between the two
builds is the one-line SRCREV bump in `forgectrl.bb` (`1d9b553`) - the
layer is content-hashed into the platform identity, the platform is
folded into every fingerprint, so the pin bump counted as a platform
change and invalidated the whole catalog. Structural, not a fluke: every
component update that ships in an image rides a pin bump in a
content-hashed layer, so under that rule every image with any component
change was an invalidate-all and the per-domain inheritance the contract
promises could never hold across images (the component entry already
carries the change file by file; the pin double-counted it). Fixed the
same day: component pins live in `<recipe>-pin.inc` (SRCREV + the PV that
moves with it, nothing else) and `forgefirm-image-manifest.bbclass` leaves
`*-pin.inc` out of the layer content (`FORGEFIRM_MANIFEST_PIN_SUFFIX`),
mirrored in `scripts/manifest-from-tree.py` and proven by
`forgetest/tests/test_tree_manifest.py` (pin bump → hash unchanged; recipe
body change or a pin written into the recipe → hash changed, the safe
direction); the six component recipes (forgectrl, grblhal-glowforge,
forgefirm-app in meta-forgefirm; kernel-module-glowforge,
python3-gfhardware, python3-gfutilities in meta-openglow) require their
pin files and resolve the same SRCREV/PV under bitbake. A second, smaller
contributor stays as designed: a test's implementation hash is its suite
module, so the day's edits to `suite/{cloud,cooling,forgectrl,kernel,
motion}.py` alone would have re-required 16 of the 26. **Consequence for
the bench: the fix changes the layer content itself, so the first image
built with it is a platform change against everything recorded so far -
that image's campaign is a full one, unavoidably; from then on a
component pin bump re-requires only the tests covering that component.
Run the full campaign on the first pin-file image, not on `191951`.**

**2026-08-16, bench-tab ports complete (item 15b).** Every tool that can
run against the machine is now runnable from the bench page: the scope
tools (`pwm_sweep`, `pwm_hold` - now a takeover with a locked-state guard:
the latch relocked, refused if FIRE or LASER_ON reads active;
`pwm_stream_test` with a PASS/FAIL exit), the flow characterization
family (`flow_characterize`, `flow_recheck_char`, `flow_warm_validate`,
`flow_matrix` as takeovers - forgectrl owns the thermal hardware, so the
page's takeover replaces the tools' own controller stop/restart, whose
command line predated the supervisor; `flow_sustained`, `fan_test` and
`temp_calibrate` stay dry), the escalation drill (`cool_confirm_max_s`
shortened through forgectrl's settings and restored; the setting's
minimum, 60 s, is the default budget) and the live drills (`<drill> [S]
[F]`, all six, the token from the board). The host tools keep working
from a workstation: `scripts/bench/gfbench.py` resolves `GF_HOST` (host
mode, ssh) or the board itself (local mode; the page runs them that way
with `GF_HOST=127.0.0.1`, `GF_TOKEN`, and their data files under
`/data/forgetest/bench/`). Not ported, by nature: the two null-sink CI
harnesses and the `.puls` decoder. Proof: `forgetest/tests/
test_bench_registry.py` (registry <-> `scripts/bench` consistency, every
ported tool builds its command line, every script compiles, gfbench host
and local modes), the server test (a scope tool runs inside the takeover
wrapper; the bench environment reaches the tool), and a local-mode smoke
run on the bench (`temp_calibrate.py watch`, `gfbench.setting`, the
token) staged in `/tmp` and removed. **The ported tools themselves have
not been exercised from the page on the bench yet - that rides the next
dev image (the confirmation campaign's image).**

### Tool status record

**Release acceptance tool (forgetest) - BENCH-VALIDATED 2026-08-16.**
Contract: `docs/ACCEPTANCE.md`; catalog v1 (26 tests, coverage lint
enforced in CI, rule in `CLAUDE.md`). The full catalog ran on the
dev image `20260815215332` through the tool - takeover, motion,
cooling, camera, cloud, and the live tests from the page - to 26 of
26 and an export the gate authorizes against the image's manifest;
the campaign rules (domain-scoped inheritance, the always-required
core, FAIL/ERROR closing a campaign, implementation and component
changes invalidating exactly their domains) behaved as specified
across the day's closures; the baseline rule was added on the way
(record in "Release acceptance" above). The flash of `20260816191951`
exposed the layer-hash over-invalidation (a component pin bump counted
as a platform change; fixed - pins in `<recipe>-pin.inc`, left out of
the layer content; record in "Release acceptance" above). The catalog
has since grown to **35 tests** (item 16's parity work, then a sweep
that merged the tests sharing a setup: `kernel.fire-line` runs A/B/U
and the mid-ramp unlock behind one takeover, `laser.armed-kill` covers
the expected stop and a SIGKILL on one scrap setup,
`laser.pause-resume-lid-cancel` pauses, resumes and then cancels one
armed burn, and `cloud.lid-interlock-abort` runs the lid and the
interlock as two prints; the 17 `auto` tests were left separate, since
merging them buys no operator time and costs failure isolation).
Every board-runnable bench tool is ported to the page, including
`resume_dark_lead.py`. Remaining: the first release runs the campaign
and commits `releases/v<version>/acceptance.json` - **not yet: no
release is cut.**

## 2026-08-16 … 2026-08-17 — lid / button / interlock parity

### The parity record

**Lid / button / interlock parity with the factory firmware — DONE,
bench-validated 2026-08-17 on dev image `20260817124714`.** Both controller
modes react to the lid, the remote-interlock loop and the button the way the
factory daemon does. The factory behavior was decoded and then recorded on
the bench machine booted into factory 2.6.0-2228; that session covered five
prints and its measured numbers are in the facts bank in `BRINGUP.md`.
- **What the machine does, both modes.** Lid or interlock open during a job,
  running or paused: motion stops within milliseconds of the edge, the job is
  **cancelled and not resumable**, the head returns to the position the job
  started from **with the lid still open**, the kernel laser latch relocks and
  the armed window closes. The next job re-arms with a button press — the same
  press the hardware button latch needs, so the software window and the
  hardware latch agree by construction. The return-home park ignores the lid
  and always runs to completion. A lid or interlock open during the pre-run
  button wait cancels the job with the reason named. A lid open during a hunt,
  homing, a jog or at idle is ignored. The button pauses and resumes a job:
  in cloud mode with the factory's laser-off backtrack and resume lead
  (`cloud_pause_backtrack_ticks` 2000 / `cloud_resume_lead_ticks` 1950), in
  GRBL mode as feed hold / cycle start — the kernel refuses a backtrack on a
  live-streamed ring, so a resumed GRBL cut picks up where the deceleration
  ended. A pause is not a cancel: the latch stays unlocked and the armed
  window open across it. `lid_policy = hold` selects stock grblHAL door
  behavior (park in Door, cycle start resumes) instead of the cancel.
- **GRBL** (`grblHAL-glowforge/src/glowforge_switches.c`, `glowforge_laser.c`):
  the arm wait cancels on lid or interlock with a clean soft reset — no alarm,
  reason reported — and a press with the lid open never arms; the button is
  the pause/resume toggle outside that wait, the arming press consumed so it
  is never also a pause; a lid or interlock open mid-job parks the job through
  the core's door state (planned deceleration, spindle off, position kept) and
  the driver then cancels it, resets from the parked state and enqueues a
  `G53 G0` back to the job start with the door hidden and the latch locked.
  The job start is the machine position at the Idle → Cycle transition.
  `GF_SWITCH_FILE` is the file-backed EV_SW word that lets null-sink builds
  drive these edges in CI.
- **Cloud** (`python3-gfhardware/gfhardware/machine.py`, `Glowforge-Utilities`):
  the interlock joins the lid in every gate; the switch thread wakes the run
  loop on the edge, with the level read kept as a backstop; the park ignores
  the lid and the cancel flag and clears the ring before it moves, so nothing
  of the abandoned job plays ahead of it; a hunt ignores the lid; a job refused
  at start ends `:cancelled`, never `:completed`; the button pauses and resumes
  a print exactly as the factory does (`print:paused` / `print:resumed`), and a
  lid, interlock or service cancel while paused cancels from where it stands.
- **No resume dwell.** The GRBL resume was suspected of losing its first ~90 ms
  to the HV_ENABLE re-arm. Measured on the pads instead
  (`scripts/bench/resume_dark_lead.py`, numbers in the facts bank): the chain
  is back within ~3 ms of the resume and motion only restarts ~219 ms later, so
  there is nothing for a dark dwell to cover and none was added.
- **Proof.** Host: `laser_arm_test`, `laser_lifecycle_test.py` (button wait,
  lid and interlock in the wait, button toggle, cancel + return without alarm,
  `lid_policy=hold`), `python3-gfhardware/tests/test_machine_lid_button.py`,
  the gfutilities suite, and the forgetest unit tests + coverage lint. Bench,
  through the acceptance catalog: `motion.button-hold-resume`,
  `motion.lid-cancel-home` (cancel from Run and from a hold),
  `motion.interlock-cancel-home`, `motion.lid-policy-hold`,
  `cloud.lid-interlock-abort`, `cloud.lid-during-button-wait`,
  `cloud.hunt-lid-open`, `cloud.pause-resume`, `cloud.pause-cancel-paths`,
  `cloud.gfhome-homing` and `cloud.mode-switch` all PASS 2026-08-17; the live
  arm-wait, mid-burn lid cancel, expected stop and armed-kill drills passed
  the same day (`laser.arm-wait-lid`, `laser.emission-witness`,
  `laser.disarm-in-hold`, and the mid-burn lid cancel that the stream-engine
  fix below made honest).
- **The stream-engine defect this work found and fixed.** A mid-burn lid
  cancel reported a return the machine never made: the park's `cnc/run` landed
  while the kernel was still playing the hold's queued tail, was refused with
  EPERM, and "refused, kernel running" was taken for a start — the kernel then
  idled with the park bytes stranded, and the next run played them first. Fixed
  in `stepper_stream.c`: a refused run stays *pending* and is re-issued the
  moment the kernel reads idle; a soft reset never stops a kernel that is only
  draining a completed stream; a mid-motion reset clears the unplayed residue
  once the stop has played out, before any new bytes ship or the device changes
  hands; the cancel path waits for the drain before the reset. The lesson is in
  the catalog: the lid tests check the **kernel counters**, not grblHAL's
  belief about them, and the baseline refuses to jog while unplayed ring bytes
  exist.
- Items 4 and 12 above are closed by this policy.

### The controller safety mapping it replaced

**Controller safety mapping — DONE.** The mid-job Door hold described
here is the `lid_policy = hold` path; the default is the factory-parity
cancel of item 16 (lid or interlock = cancel + return to the job start),
bench-validated 2026-08-17 (`grblHAL-glowforge/src/glowforge_switches.c`). The
controller reads EV_SW with `EVIOCGSW` from the protocol thread's
realtime hook (no grab — forgectrl polls the same device) and maps:
- **doors (bit 3) not closed, or interlock (bit 5) loop open →
  the core's `safety_door_ajar`.** A running job parks in the door
  state and resumes when the condition clears, which is what the
  hardware chain already does to the beam. Bit 3 is the series
  combination the safety chain itself uses, not the individual door
  switches.
- **hv_enable (bit 4): never gated on.** It is the readback of the
  chain's HV_ENABLE output (facts bank in `BRINGUP.md`), telemetry only; the
  core's `e_stop` capability is not advertised. (The `estop_halts_motion`
  opt-in that existed until 2026-08-15 is gone, together with the
  name — see the facts bank.)
- **interlock latch (bit 6): deliberately not gated on.** Its
  resting state on a healthy machine is not characterized and a
  false assertion would wedge every job; the hardware chain enforces
  it regardless.
- No switch device (host builds) = no capability advertised, no
  signals.
**N5 answered: no software latch-reset path is needed.**
Interlock-trip recovery was exercised in commissioning runs without
one — the chain recovers when the condition clears. `cnc/laser_latch`
stays write-only (1 = lock), the driver's arm flow unlocks per job,
and `interlock_latch_reset` remains a readback. **Amended 2026-08-15:**
the *interlock* latch never trips at all in ForgeFIRM — see Next work
item 11; the "recovery" seen in commissioning was the software
safety-door path, not the hardware latch.
**Bench items:** open the lid mid-job (expect `Door` at the sender,
motion parked, cycle start resumes after close); a Pro with an
unjumpered interlock connector (expect the same door behavior);
confirm no spurious door events across a full job. Underrun → alarm
was already covered by the stream-fault path.
**Changed 2026-08-15 (grblHAL a9446fe, host-tested, pin bumped, bench
validation pending):** the door signal is now hidden from the core while it is
IDLE, JOG or HOMING (`gfsw_visible`, applied to both `get_state()` and
the edge delivery) and delivered the moment it is in any other state.
Reason: a lid cycle at idle — every material load, and a power-up with
the lid open — left grblHAL parked in `Door:0` until a cycle start,
and LightBurn then sat at "Waiting for connection". Consequences: jog
and `$H` are allowed with the lid open (beam hardware-blocked; upstream
"ignore when idle" semantics), a job started with the lid open parks on
the first poll, mid-job opens park exactly as before, and the cloud
client (own EV_SW reader) is unaffected. Bench check: lid open/close
at idle → state stays Idle; open mid-job → Door, close, `~` → resumes;
Start with the lid open → Door immediately. **Partly validated
2026-08-15 on image 20260815154622: LightBurn now connects after the
lid has been opened and closed at idle (the original complaint).**
The mid-job and start-with-lid-open checks are still open, and the
session surfaced further LightBurn door-open issues — see Next work
item 12.

## 2026-08-17 — step timing under CPU contention

Opened by an operator report: the `LB-GF-OG-FM` LightBurn job, GRBL mode
at 2000 mm/min and 30 % power, ran jerky and lost many steps.
`cnc/underruns` and `cnc/faults` both read 0 throughout, which is the
whole reason the condition had gone unnoticed — the kernel ring never
runs dry, so the stream stays continuous and only its *timing* is wrong.

### What was wrong

The board runs one core. Of grblHAL's four threads only the shipper held
`SCHED_FIFO`; the producer — which advances virtual time and stamps every
step onto the pulse grid — ran `SCHED_OTHER` at nice 5, the same class and
nice as forgectrl's MHD connection threads. forgectrl was measured at
~41 % of the core serving the panel's MJPEG camera stream, one connection
thread alone at ~35 %.

When the producer's virtual clock falls behind the ship cursor,
`gf_stream_pulse` clamps late events forward and the backlog ships one
step per machine tick: 28 160 steps/s against the 1 778 that 2000 mm/min
asks for, a ~16× velocity burst no motor follows.

The margin absorbing a stall was **2 ms**, not the 200 ms queue depth it
appears to be: the shipper's due index carries the same `+ gf.depth` the
producer's base starts at, so the two cancel and the pacing lead is the
only slack there is.

### What was changed

- Producer on `SCHED_FIFO` one priority below the shipper, and `core_mx`
  given priority inheritance — the producer holds it across the stepper
  callback while the protocol thread also takes it, so promoting without
  PI would have traded jitter for unbounded inversion (grblHAL 026c169).
- Producer lead made tunable (`GFSINK_LEAD_MS`) and defaulted to 10 ms;
  the per-run `LOG_DEBUG` line now reports the **measured** `min margin`
  in ms against it (grblHAL fd059b3).
- Clamp count reported per run at `WARNING`, not only cumulatively at
  process exit.

The lead defaults to 10 rather than higher because of the cycle-churn
path: `gf_stream_wakeup` re-bases production onto the wall cursor only
when the cursor has passed it, so a larger lead survives an idle gap,
skips the re-base and accumulates as dark padding. Measured on the
`laser_stream_test.py` churn harness: 2 ms and 10 ms both give an
identical 64 790-byte stream, 15 ms and above inflate it to ~225 k and
stop being deterministic.

### Bench record

Two `motion.step-timing-under-load` runs 90 s apart on image
20260817210307, same campaign, camera streaming at 2592×1944 in both,
plus the test's own nice-5 CPU hog:

- **PASS** — 20 legs, 26.7 s, 0 clamps. Camera not streaming.
- **FAIL** — 20 legs, 26.7 s, **7 runs clamped, 81 events**, max behind
  3.9–4.4 ms. Camera streaming.

That isolates it: `SCHED_FIFO` covers a userspace CPU competitor and does
not cover the camera, whose per-frame cache maintenance over a 4.8 MB
non-coherent capture buffer is kernel-context work no userspace priority
can preempt.

On image 20260817220126 with the 10 ms lead, same conditions as the
failing run (camera at 2592×1944 **and** the CPU hog): **PASS, 0 clamps**,
worst `min margin` 4.9 ms of 10 across 12 legs.

Then the original `LB-GF-OG-FM` LightBurn job again, operator-run, with
the video stream live (independently corroborated: forgectrl was holding
`video0`/`video4` with four `:8080` connections mid-job):

    run: 686850 callbacks in 62.255 s (90.6 us/call incl. pacing),
    50721 pace sleeps, max behind 4.7 ms, min margin 3.1 ms of 10, clamped 0

Identical callback count and duration to an earlier run of the same job,
so it is the same work. Operator judgment: ran clean. `underruns` 0,
`faults` 0, and no clamp warning from the current controller instance.

### What the numbers say

`max behind` is **not** the instrument — it read 0.0 ms on every leg of
the passing acceptance run while the real margin fell to 4.9 ms, because
the producer never falls behind its own wakeup epoch; the margin is
consumed by the offset between that epoch and the shipper's `ship_t0`.
Only the measured `min margin` shows the condition.

The real job is the harsher adversary: 3.1 ms of 10 remaining, against
the synthetic test's 4.9 ms, at a *lower* callback rate (11 033/s vs
13 784/s). Real cut geometry costs more headroom than uniform jog legs.

So the fix holds on the job that prompted it, with ~31 % of the budget
left at the worst moment. Both remaining levers are unspent: camera
capture resolution (the mainline `ov5648` offers 1280×960 and 640×480
binned modes, 4.1× and 16.4× fewer bytes, which shortens the stall rather
than merely spacing stalls out) and the churn re-base (which is what
would allow a lead beyond 10 ms). Tracked in BRINGUP "Next work" item 16.

## 2026-08-17 — the laser duty threshold ladder

The first owed step of "Next work" item 17: measure where the tube starts
lasing, so `$35` can stop M4's velocity-scaled power falling below it.

### The run

`live_fire_drills.py pthresh 1000 300` on wood scrap, operator-run on dev
image `20260817220126`, machine idle and homed, coolant 23.9/24.1 °C. The
precondition was read off the machine first: `$30`=1000, `$31`=0, `$32`=1,
**`$35`=0.0**, `$36`=100 — no floor in place to lift the rungs.

Thirteen rungs, 2 %…30 % of full, 25 mm each at F300, constant power (M3),
3 mm of `+Y` between them. Before firing, the two conversions were checked
against each other: a rung of *P* % sends `S = 10·P`, which the core maps to
`floor(127·P/100)` counts, and `$35 = P` computes `min_value =
(uint)(127·P/100)` — the same integer, so the rung's percent *is* the `$35`
value exactly, not approximately.

### What came back

Material, counting from the first rung drawn: rung 1 (2 %) nothing at all;
rungs 2–9 (3–14 %) a tiny spot at the start of each line and a dark line
after it; rungs 10–13 (16–30 %) continuous marks.

The `hv_current` trace agrees independently. It holds 0 for 19.5 s (arm
wait), then runs nonzero to 82.8 s, immediately before Idle. Within it the
laser-off `G0` between rungs reads 0, so the current runs count the rungs:
**12 segments of ~4.7 s at a ~5.27 s period, not 13.** The last segment ends
at the job end, so it is rung 13; counting back 12 puts the first current at
rung 2. Rung 1 drew no measurable discharge current — the same rung that
left no mark, from a completely separate witness.

So the tube has **two thresholds, far apart**:

| | rung | duty | witness |
|---|---|---|---|
| Discharge strikes | 2 (3 %) | PWMSAR 3 | current lifts off; spot only |
| Sustained lasing | 10 (16 %) | PWMSAR 20 | first continuous mark |

Between them, 3–14 % is a **dead band**: current flows and climbs (per-rung
means 133 → 289 raw) with essentially no light out. Each line's opening spot
is the strike transient; the tube lights, drops below lasing gain, and coasts
dark for the remaining 25 mm.

This falsifies the drill's own guidance, which said the current "lifts off
baseline at the same rung the material starts marking" — lift-off is rung 2,
marking is rung 10. The docstring and the printed read-the-material text were
corrected to name both thresholds and to tell the operator that a rung
showing only a start-of-line spot is *below* the threshold, not at it.

Raw `hv_current` is a presence/absence witness only. Per-rung means are
non-monotonic at the top (429 at 20 %, then 311 and 302) and the variance
collapses on the top two rungs, which is what an aliased point-sample of a
pulsed current looks like; the signal has no characterized transfer function.

### Ruling out the firmware explanation for the spots

A start-of-line spot is also what a full-power leak would look like: a kernel
run start resets the hardware duty to ~100 %, so a fire bit reaching the
stream ahead of its power byte would burn at full power. The material already
argued against it — rung 1 is the first fire of the run, the likeliest place
for such a leak, and it is blank — but the stream is the record, so
`laser_stream_test.py` gained a fourth session (rule 10): a ladder in the same
shape, full power deliberately absent, asserting that every FIRE tick rides a
commanded duty and that the fire ticks divide evenly across rungs (a rung
opening at its neighbor's duty shows up as a surplus on one and a deficit on
the next).

Result on the native build: duties under FIRE were exactly `[22, 23, 26, 32,
41, 52]`, nothing else, and **28296 fire ticks on every rung, identical to the
tick**. No full-power window, no stale-duty window. The spots are the tube and
supply, not the firmware.

### What landed

- `DEFAULT_SPINDLE_PWM_MIN_VALUE 16.0f` in `boards/glowforge.h` — the
  measured lasing rung. Chosen over the next rung up (20 %) because the floor
  is spent at corners, where velocity and dose per unit length already move
  the wrong way, and because `$35` is a user setting anyone can raise.
- The harness now derives its expectations from that floor (`duty_for()`), so
  the M4 session's S500 plateau moved 63 → 73 and its ramp `[44, 52, 63, 127]`
  → `[57, 64, 73, 127]`, plus a new check that no duty under FIRE falls below
  the floor. All four sessions pass, as do `switch_map_test`, `laser_arm_test`
  and `laser_lifecycle_test`.
- `laser.power-floor`, an auto acceptance test (the suite's only non-firing
  one): reads `$$` and checks the machine actually carries the commissioned
  floor, since stored settings beat freshly baked defaults and a machine with
  an older EEPROM needs `$RST=$` once. Coverage lint clean at 40 tests.

### What it means for the model

The usable analog range is 16–100 %, about 6:1, with the bottom sixth of the
control range physically dead — and the factory's captured pulse files pin the
power byte at 127 and modulate dose by dithering the FIRE bit at 6.5–18.8 %
density. The dead band is why. `$35` is a patch that buys freedom from dropout
by putting its full 16 % into every corner; dose set by pulse density cannot
fall below the lasing threshold by construction. Item 17 is now the density
model itself, with the analog path as the fallback.

## 2026-08-17 — how the factory sets power

Three cloud-mode cuts of the same 1" square, same location, same material,
same speed, changing only the Glowforge UI power setting: Precision Power 1,
Precision Power 100, then Full Power, with the pulse file captured from each.

Pulse-file capture ships off (`LOGGING.SAVE_PULS`), and the machine's copy of
`/data/etc/gfhome.conf` predated the key, so it was enabled for this session
and turned off afterward. A first attempt appended the key past the last
section, where `get_cfg('LOGGING.SAVE_PULS')` would never have found it — it
belongs inside `[LOGGING]`, and was verified through the app's own parser
rather than by eye.

### The measurement

**Analog duty is not a power control.** All three runs carry the power byte
exactly three times, always 127: once as the cut begins, then a refresh every
~27 000 ticks (~2.7 s). Nothing modulates PWMSAR, at any setting.

**Dose is FIRE-bit density on a fixed 7-tick period** — 700 µs at
`STfr` = 10 000, ~1.43 kHz — with the on-count dithered between adjacent
integers:

| Setting | on-runs | mean of 7 | density |
|---|---|---|---|
| Precision Power 1 | 1 (×359), 2 (×212) | 1.371 | 0.1953 |
| Precision Power 100 | 5 (×236), 6 (×334) | 5.576 | 0.7952 |
| Full Power | continuous | 7 | 0.9965 |

The period was exactly 7 in all 570 measured cycles of both dithered runs, and
the mix of adjacent on-counts matches the fractional part exactly: PP 1 wants
1.371 on-ticks, and 2-runs are 212 of 571 = 0.371. That is an error
accumulator, not a repeating pattern.

**The power setting never reaches the machine.** The three headers are
identical — no key differs — so the model lives entirely in the service, which
bakes it into the FIRE bits. The motion is identical too: 5420 steps,
101.62 mm (4 × 25.4), 10.81 s at 9.44 mm/s. Full Power's file is longer only
in the lead-in before the cut.

**Velocity compensation is real but partial.** Density falls as the head slows
into a corner, by the same relative factor at every power setting
(corner/cruise 0.38, 0.38, 0.41). Measured per step interval, though, fire
ticks per step *rise* from 3.89 at 9.44 mm/s to 7.00 at 1.22 mm/s, so dose per
unit length still climbs ~1.8× at a corner — against the ~7.7× it would climb
with no compensation at all. Only ~24 of 5420 step intervals are below cruise
speed, so the direction and rough magnitude are solid and the exact law is
not.

On the UI scale, PP 1→100 is linear in density (~0.006 per unit, intercept
~0.189); Full Power sits off that line, where PP ~134 would land, which fits a
setting the UI presents as outside the normal range.

### Two corrections to earlier readings

The first pass at the dither sampled the mid-point of the cut, which for a
square is a corner, and truncated its distributions — it showed 8-on/4-off
bursts that are corner behavior, not the steady pattern. The first pass at the
dose law counted every tick as a step, because in this encoding bits 1 and 3
are *direction*, held for the whole side, and only bits 0 and 2 are step
pulses; the tell was 2026 mm of travel on a 101.62 mm cut.

### A defect found by using the feature

Deleting the capture directory under a running gfcloud showed that with
capture enabled, a missing directory or a full disk makes `load_motion` raise
on the capture write and kills the print. A debug aid must never cost a job:
the capture open, the per-chunk write and the `.info` write are now each
non-fatal, dropping the capture with a warning and running the job on
(`gfutilities`, with a regression test in `tests/test_lifecycle.py`; verified
in three cases — missing directory still loads the job, a writable directory
still gets the copy, capture off writes nothing). No acceptance-catalog
consequence: the path is an off-by-default debug capture with no bearing on
emission, motion or the release surface.

## 2026-08-17 — the density dose model, phases 1 and 2

Implemented and host-proven; off by default, so nothing about a shipped
machine changes until `laser_power_model = density` is set.

### The change

The whole hot path is one predicate in the shipper:

    if(gf.cur_fire)          ->   if(gf.cur_fire && (!gf.dith_period || dither_tick()))
        b |= 0x10;                    b |= 0x10;

That `&&` is the safety property, structurally: the model masks the core's
fire state and can never be a source of one, so it stays out of the safety
argument entirely — the armed window, the latch, the coolant gates and the
hardware chain are all upstream and untouched.

Around it: a fixed base period of `laser_pulse_ticks` (default 20 = 710 us
at 28160 Hz, the factory's ~1.43 kHz), on-count `level x period / 127` with
the remainder carried across periods so finer densities average out, the
on-ticks leading each period so a level renders as one burst rather than
isolated ticks. The accumulator resets only where the dose itself restarts —
run boundary, fire off, disarm, abort — never per segment. In density mode
the duty is pinned: a power byte still leads every kernel run, because the
run start resets the hardware duty, but it always carries full duty and a
level never reaches PWMSAR. Selected per arm from the shared machine config
and reported as `laser armed (density)`; the arm warns when `$35` is set,
since the floor only clamps the light end of a range that cannot fall into
the dead band anyway.

### What the harness holds (rules 11-13)

- Density renders the commanded level exactly: levels 2, 3, 7, 15, 25, 38
  came back as 0.0158, 0.0237, 0.0551, 0.1182, 0.1969, 0.2993 against
  level/127 of 0.01575, 0.02362, 0.05512, 0.11811, 0.19685, 0.29921.
- S1000 renders density 1.0000 and still ends dark.
- Every power byte carries full duty; a level change inside a run costs no
  stream byte, where analog ships one per level (4 bytes, duties 0/30/52/84,
  against density's 1).
- The mask invariant, measured rather than argued: the same job run under
  both models produced an identical motion grid tick for tick, and all
  20051 density FIRE ticks fell inside the 169776 the analog run fired.
- Churn (planner-starve run boundaries) still terminates dark under the
  model, with no FIRE across a stepless gap.

The analog path is byte-identical to before the change — same byte counts,
duties and fire ticks on every pre-existing session — so the fallback is
intact.

### Two things the work turned up

**Spindle `$`-settings take effect at controller start, not at the write.**
The core precomputes the S -> duty mapping once, when the spindle is
enabled; a settings write does not re-run it. After a runtime `$35=0` the
shipped duties stayed floored at 57/64/73/127. So `$35=16` set on the bench
earlier today persisted immediately and was reported by `$$` immediately,
but only entered force at the next controller restart — which the capture
work then supplied. The harness now models this the way an operator would:
one launch writes the setting, the next runs the job.

**A laser state change made while the stream is idle was lost — found,
root-caused and fixed.** Reproduced in both dose models, so it was not the
density model's doing: with a line-at-a-time sender and moves long enough to
drain the planner, `S100 / G1 X5 / S300 / G1 X5 / S600 / G1 X5` fired only
the first move and shipped duty 30 three times.

It was two faults wearing one symptom, and fixing the first exposed the
second. `gf_stream_laser()` dropped transitions while nothing was streaming,
so nothing re-asserted the state for the next run, which a run end leaves
dark — the stream engine now records the state the core last asked for
whether or not it is streaming, and re-asserts it at the first byte of the
next run, fire only inside an armed window (an abort clears it, so a closed
window can never be resurrected). With that in, all three moves fired, and
all three fired at duty 30: the level had never reached the driver at all,
because `spindleSetState` discarded its `rpm` argument. Per-segment updates
carry the level inside a laser block, but an S executed between blocks
arrives only through that synchronous path. It now publishes the duty, and
only the duty — fire stays where `spindleUpdatePWM` and its gates put it,
so the new path carries no consent to fire.

Rule 14 in the harness is the regression: the same standalone-S job must
show each level firing its own move. It does — 28338 fire ticks each at
duties 30, 52 and 84, where before the fix duty 30 held all 85014 and the
two other levels never appeared.

## 2026-08-17 — the density ladders, and the minimum pulse

Four live ladders on one piece of scrap, 8 rungs each from 5 % to 100 % of
dose at constant power: base period 20, 40 and 10 ticks at F300, then period
20 again at F100. The same six rungs marked every time — 20 % and up. 5 % and
10 % never marked in any of the four.

### Pulse length is not the variable; average power is

Because the same pulse length occurs at different densities across the
periods, the runs contain matched pairs:

| pulse | density | period | marked |
|---|---|---|---|
| 107–142 µs | 20 % | 20 | yes |
| 107–142 µs | 10 % | 40 | no |
| 36–71 µs | 20 % | 10 | yes |
| 36–71 µs | 10 % | 20 | no |

Hold the pulse and halve the density: the mark goes. Hold the density and
vary the pulse 3×: nothing changes. Feed does not move it either — 10 % at
F100 carries 0.0567 dose/mm against 20 % at F300's 0.0394, **44 % more energy
per millimeter than a rung that marks**, and it still left nothing. Two
independent variables moved without shifting the boundary. What sets the
low-end marking limit is average power reaching a quasi-steady surface
temperature; going slower does not help, because the heat conducts away
between pulses.

So the base period can be chosen on other grounds, and stays at 20.

### The trace separates two different failures

The F100 run carried the `hv_current` trace (`pthresh` printed one, `dladder`
did not until this run — the omission cost the three F300 ladders their
per-rung witness). It shows **seven** current segments for eight rungs:
boundaries at 36.6, ~52.0, 67.3, 82.5, 98.0 and 113.2 s, each segment
14.4–14.9 s, one 25 mm rung at F100. The fire window is 106.8 s where eight
rungs would need 121.6 s.

The final segment anchors the count: 113.5–128.4 s reads 943–986 dead flat,
the saturated steady current of continuous fire, which can only be 100 %.
Counting back, the segment means rise monotonically — 182, 320, 330, 390,
450, 540, 967 — for rungs 10 % through 100 %. Rung 1 has no segment at all:
its fifteen seconds are the zeros before 21.6 s, indistinguishable from the
arm wait because nothing happened in them.

| rung | outcome |
|---|---|
| 5 % | **no discharge at all** |
| 10 % | discharge for the full 15 s, no mark |
| 20 %+ | discharge and mark |

Supply current is not light — `pthresh` already showed this tube drawing
current across a whole band while emitting nothing — so "10 % struck" is not
"10 % lased". But 5 % not striking is unambiguous, and it is ours to fix.

### The factory's own numbers, for scale

Precision Power 1 runs the power byte at 127 (PWM duty 100 %) and a **FIRE
duty cycle of 19.53 %** — 1.371 on-ticks of every 7-tick window. Fitting the
three captures, the factory maps its entire 1–100 scale onto density
18.9–79.5 %, with Full Power off that line at ~99.7 %. Its "1 %" is the
bottom of the band that does useful work, not 1 % of the physical range —
which is why no user ever meets the dead zone. Older captured factory
jobs run 6.5–18.8 % density, so 18.9 % is a product
decision about cutting, not a physical floor.

### The fix: a minimum pulse width

At 5 % the model emitted one-tick stubs, 36 µs, and the supply did not
strike. The factory never emits below one 100 us tick and reaches low density
by skipping windows instead. `laser_pulse_min_ticks` (default 3 = 106 µs)
does the same: when the computed on-count falls below the minimum the period
is skipped and the **whole** debt carried, rather than a stub emitted. The
debt is conserved, so the average density is untouched.

Measured on the stream, level 2 (density 0.0159):

| | bursts | density |
|---|---|---|
| minimum 1 tick | 444 × 36 µs | 0.0158 |
| minimum 3 ticks | **147 × 106 µs** | 0.0159 |

147 × 3 = 441 against 444 — the same energy as fewer, longer pulses, and
every level already above the minimum is bit-identical, so the change touches
only what it must. Rule 15 in the stream harness holds both halves: no burst
below the minimum (excepting one clipped by fire going off mid-burst), and
the rendered density still exact.

### The fifth ladder, and a conclusion retracted

Same ladder, period 20, F300, with the 3-tick minimum in place. **The floor
moved down a full rung: only 5 % failed to mark, and 5 % now strikes.**

The trace carries eight current segments where the F100 run had seven.
Segmenting by time rather than by zeros — at 5 % density the sampled current
aliases, so isolated zeros appear mid-rung and cannot serve as boundaries —
the rung period is 5.25 s and lines up end to end: fire begins at 6.2 s,
exactly at rung 1's start, boundaries fall at 11.4, 16.6, 21.9, 27.3, 32.6,
38.1 and 43.3 s, and the span is 42.0 s against 41.4 s for eight rungs. The
final segment reads 937–981 flat and saturated, which can only be full
density. Rung 1 shows peaks of 291, 286 and 204 where the F100 run held a
flat zero for the rung's entire fifteen seconds.

**This retracts the conclusion in the entry above.** Pulse length is not
irrelevant: 10 % moved from no mark at F100 — with three times the dose per
millimeter — to a mark at F300, at the same density, the only change being
its pulses growing from 36–71 µs stubs to 106 µs. The matched-pairs argument
was sound but drawn entirely from comparisons at or above 20 % density, where
every pulse length in play was already sufficient; it generalized from the one
regime where pulse length does not bite. Above ~100 µs dose governs, below it
pulse length does, and below ~36 µs the supply does not strike at all. The
factory's 100 µs quantum sits exactly on that boundary.

Not read into: the low-rung current means (76 and 82 raw for 5 % and 10 %).
At those duties a 3.3 Hz point sample of a pulsed current carries
presence-versus-absence and nothing more. Noted as a confound, though it cuts
against the result rather than for it — this ladder started at MPos 0,0 after
the controller restart, so it may be on different material than the stacked
Y=0/24/48/72 runs.

### The sixth ladder: a longer minimum is worse, and why

`min_ticks = 6` (213 µs), same ladder otherwise. **It broke 5 % striking** —
seven current segments again, boundaries at 14.5, ~19.85, 25.2, 30.4, 35.9
and 41.1 s, segments 4.4–4.9 s with none double-length, fire spanning
9.5 → 46.0 s = 36.5 s against 41.4 s for eight rungs, and the flat saturated
tail anchoring rung 8. Seven rungs marked, matching.

The arithmetic explains it. Below the minimum the model emits `min` ticks
every `min/on` periods, so the interval between pulse starts is

    interval = min_ticks × tick / density

and **the base period cancels** — which retroactively explains why periods
10, 20 and 40 gave identical results in the first three ladders. At 5 %
density that is 2.26 ms at `min_ticks` 3, which struck, against 4.51 ms at 6,
which did not. Doubling the minimum doubles the gap as well as the pulse, and
the gap is what decides: the discharge is re-struck each pulse and past
roughly 2–4 ms it has decayed too far to catch.

That also puts `min_ticks` 3 at the factory's own operating point — its 6.5 %
engrave jobs place 100 µs pulses 1.54 ms apart, against 1.64 ms for
`min_ticks` 3 at that density — and puts 6 outside anything the factory does,
in the direction that fails. The bench is back at 3.

**Measured band for this tube: strikes from ~5 %, marks from ~10 % at F300.**

Which closes the pulse-structure route to a usable 1 %. The interval grows as
1/density, so 1 % implies an 11 ms gap, five times what already failed — no
pulse shape reaches down there. The low end is a scaling problem.

### The seventh ladder: the scale, and the goal met

`$35 = 10` — a density floor under this model, not a duty floor — with the
ladder reweighted to the bottom of the user scale (1, 2, 5, 10, 20, 40, 70,
100 % of S), since with a floor in place what matters is whether the lowest
levels a user can dial in still mark.

The mapping puts S onto 9.4–100 % density, so a commanded 1 % lands at 10.2 %,
just above the ~10 % marking floor the earlier ladders measured. **All eight
rungs marked.** The trace carries eight current segments — boundaries at 14.3,
19.5, ~24.85, 30.2, 35.4, 40.9 and 46.1 s, fire spanning 9.1 → 51.1 s = 42.0 s
against exactly 8 × 5.25 — with means climbing monotonically:

| rung | commanded | density | mean current |
|---|---|---|---|
| 1 | **1 %** | 10.2 % | 136 |
| 2 | 2 % | 11.0 % | 190 |
| 3 | 5 % | 13.4 % | 214 |
| 4 | 10 % | 18.1 % | 262 |
| 5 | 20 % | 27.6 % | 331 |
| 6 | 40 % | 45.7 % | 340 |
| 7 | 70 % | 72.4 % | 444 |
| 8 | 100 % | 100 % | 968 flat |

So the original goal is met: a user's 1 % is a real, visible mark rather than
silence, and 100 % is full power. It took the density model to make every
level real pulses, the minimum pulse to keep them strikeable, and the floor to
put the user's range on the band that works — the same three pieces the
factory uses, arrived at from this bench's own measurements.

### The defaults flipped

`laser_power_model` now defaults to `density` and `$35` to 10, so a stock
machine runs the model and a commanded 1 % marks. The analog path stays as an
explicit `laser_power_model = analog`.

The two settings are coupled and the pairing matters: `$35` is a **density**
floor under the shipped model and a **duty** floor under the fallback, wanting
~10 and ~16 respectively, and the wrong pairing is a dead band in either
direction. The arm warns on both mismatches — a zero floor under density,
where the bottom of the S range asks for pulses too far apart to re-strike,
and a sub-lasing floor under analog.

Test-side consequences worth noting, since the default reaches into the
harness: every analog session in `laser_stream_test.py` now selects its model
explicitly rather than inheriting it, or the flip would have silently turned
them into density runs and taken the analog fallback's coverage with them.
`laser_arm_test` asserts the inverse of what it used to — no config key now
means density — and `laser.power-floor` carries the new floor and its
PWMSAR minimum. All ten stream sessions, both C harnesses and the lifecycle
harness pass on the new defaults; the analog duties shift exactly as the new
floor predicts (min_value 12, gradient 0.115).

Owed: validation at a production feed. Every ladder behind these defaults ran
at F300 or F100 at constant power, so none of them exercised M4's velocity
scaling into corners, a real sender's mid-run level changes, or the raster
path, which has not run at all. The arithmetic says dotting will not be the
problem — at 10 % density the pulse interval is 1.07 ms, 35 µm at
2000 mm/min against a ~200 µm spot — but that is reasoning, not a cut.

## 2026-08-20: how the factory reports progress (F1)

The open question behind cloud-mode progress reporting was which carrier the
factory uses and how often: a `<action>:progress` event, a `progress_bytes`
query on the action endpoint, or the periodic settings report. The strings in
the factory binary named all three and settled none. It was answered by
observing the factory application's own cloud session on the machine, running
the factory slot end to end (a hunt, images, five motions, and a print with a
button pause and resume).

The answer is none of the three as posed, because two of them collapse into
one. Progress rides an **outbound WSS `type:"progress"` frame**, machine to
service, and that frame **is** the periodic settings report: its
`settings.values` block is exactly `periodic_settings_tags`. No
`<action>:progress` event and no `progress_bytes` query appeared in the whole
session. Cadence is `progress_update_interval_ms` = 30000, i.e. one frame every
30 s during a cut, with a burst at each phase transition; during the cut
`current` advances at the 10 kHz print tick.

Two things fell out of the same capture. `CCbp` in the frame reads the byte
position (1009 against a `current` of 994), re-confirming it as telemetry and
not the pause constant an earlier reading had guessed. And the factory's own
progress `total` grew during the cut, 33,291,208 → 33,553,352 → 33,815,496,
256 KiB per interval, because the factory live-appends to its ring: even the
factory's progress bar divides by a denominator that is still growing. Under
ForgeFIRM's streaming feed a progress report must divide by the feeder's own
job total, never the kernel byte counter. That is the F2 work; the carrier,
the frame shape and the cadence are now known.

The decision that came with it: the `type:"progress"` frame is carried as a
deliberate exception to the telemetry exclusion. It is a UI status update, not
the sensor firehose, and it is the operator's only sign a multi-hour print is
advancing. The write-up is in `CLOUD.md` ("Progress reporting" and the scope
exception); the plan's F1/F2 rows are updated. The pause is also reported by
the factory as a ten-event phase machine against the two ForgeFIRM sends, noted
there as optional polish on the F2 work.

## 2026-08-20: the campaign behind a print longer than the ring

The work that made a cloud print independent of the ring size ran from
2026-08-18 to 2026-08-20 and is finished, so the plan it ran from is retired
into this entry and the durable documents. What follows is how the result was
obtained, which is the part that does not belong anywhere else.

**It started from a wrong belief.** The ring was 16 MiB and a job that did not
fit was going to be refused with a clean message, on the reasoning that the
factory must refuse one too. Re-reading the factory application against the
Ghidra project said otherwise on every point. Its ring is 32 MiB, allocated
through `dma_alloc_attrs` out of a 320 MiB CMA area with no device-tree pool
and no module parameter. It models the downloaded body as a pulse data source
with a cursor, gzip or plain behind one vtable, and stages it into the ring in
segments that are checked against free space, refusing with `-ENOMEM` rather
than writing a partial chunk. And it does not stop when the ring is full: it
starts the job and keeps appending for as long as the job lasts.

**The proof was on the machine already.** This board's own factory logs, kept
across slot switches on the shared `/data`, carry a 107 MB job played through a
32 MiB ring. Nothing needed to be induced; the factory had already done it and
written it down. A capture of the factory's own cloud session later showed the
same behavior on the wire, its progress `total` growing 256 KiB per interval as
it appended.

**So ForgeFIRM streams too**, and the shape follows the factory's: hold the
compressed body in memory and inflate only as far as the ring asks, which keeps
a three-hour job to a few MB and off the eMMC entirely; fill the ring before
the button is offered, so a job that cannot be loaded fails before the laser is
ever armed; declare the live feed to the kernel only when the job actually
outran the ring, so a job that fits behaves exactly as it always did; top up on
`-ENOMEM`; clear the live-feed flag after the last byte, so the real
end-of-data is a completion rather than a starved ring. The ring itself moved
to 32 MiB, at factory parity, through a size-aligned no-map device-tree pool.

**Two defects surfaced in the building.** A dry ring used to end the run loop
with `aborted=False`, which reported a job that stopped mid-cut as completed;
it aborts now. And the pause on a streamed job could not retrace, because the
old bound deducted the retained gap from a budget that was zero under a
topped-up feed, counted bytes enqueued rather than bytes played, and set its
dead stop a whole program back instead of one ring back. The retained gap *is*
the backtrack history, which is what the kernel now publishes as
`max_backtrack`, and a request longer than that is refused rather than quietly
shortened.

**What the campaign settled along the way**, each recorded where it belongs:
the factory reports progress on a `type:"progress"` frame that is the periodic
settings report; `CCbp`/`CCbt` are reported progress rather than the pause
constants an earlier reading took them for; and `CFrh`, `CCwp`, `CCrp` and
`CCup` have no consumer in the factory at all, so there is nothing to drive a
warm-up or a rest off. The contract is `kernel-module-glowforge/UAPI.md`, the
client behavior is `CLOUD.md`, and the tag findings are in the firmware
reference alongside the captures.

**What it cost to be sure:** the pulse decoder was 41x too slow to keep a ring
fed (a `sorted()` per byte, 32 kB/s against the 1.33 MB/s it manages now), and
that only showed up when a real 53 MB job was replayed through a fake ring
rather than a synthetic one. The job that hung the bench is kept as the
regression fixture.

## 2026-08-21: a lid cancel that went back to the wrong place

`laser.pause-resume-lid-cancel` failed its first run on the dev image of
2026-08-21 with "head not back at the job start (drift 14.925 mm)", and at the
bench the job had looked right: the button paused the cut, the button resumed
it, the lid stopped it, and the head came back. It came back to the wrong
place. The kernel counters agreed with the controller's own position report:
Y exactly where the job began, X 14.925 mm along the first leg, which at F200
is about four and a half seconds of cutting, the moment the pause landed.

**The controller had told the truth by its own bookkeeping.** The driver takes
the job start as the machine position at the Idle to Cycle transition, and the
grblHAL core restarts a held cycle by passing through Idle: `state_await_resume`
sets Idle and then Cycle back to back, so every resume from a feed hold was
recorded as a new job beginning where the hold had stopped. The lid cancel then
returned the head to the pause point and reported "returned to the job start",
which was exactly what it had written down.

**The fix is a definition.** A job is under way from that first transition
until the core is Idle with the planner empty (the program ran out, a stop, a
reset) or in an alarm. A resume passes through Idle with the planner still
loaded, so it is the same job and keeps its start; a job abandoned in a hold
and reset is over, and the next one starts where it starts. Both sides are
held by the null-sink lifecycle harness now, which reproduced the bench
failure to the millimeter (returned to X=13.088, the pause point) before the
fix and returns to X=0.000 after it.

**Why the acceptance test caught it and the eye did not:** the test measures
the return against the position it recorded before the job, not against the
controller's message. An operator watching the head come back has no such
reference, and fifteen millimeters on a forty millimeter square reads as
"back". The test stays as it is.

## 2026-08-21: the first full campaign on a pin-file image

Completed. Dev image `20260821181036`, the first built on the
`<recipe>-pin.inc` layout: 42 of 42 satisfied (12 run on that image, 30
inherited under the domain model from the day's earlier dev images), all
eight `cloud.*` tests run on that image, and the export reads "Release authorized:
YES" for that image's manifest (campaign `c-20260821182204-dc01`, exported
2026-08-21T18:40:30Z, artifact sha256 `6f17f690...43273055`). No release is
cut from it.

## 2026-08-21: cooling.gate-off, first bench run

PASS on dev image `20260821210903`, campaign `c-20260821213027-0b47`, at
21:47:16Z: the coolant ceiling set to 6 C tripped `OVERTEMP` (hold, fire
blocked) one second into its run session; set to 60 C the next session read
`OK` with `gates_off` `["coolant_max"]` on `/cool/status` and `/status` and the
run-start line in the forgectrl log; restored to 33/31 the third session read
`OK` with nothing off, the settings back verbatim.

The first attempt on the same image (21:18Z) failed in the test, not the
engine: its M9 and the next M8 were 300 ms apart, the GRBL client reports
level-triggered at 1 Hz and the engine samples at 1 Hz, so the engine never
saw the session end and never re-read the ceiling; the restore-on-failure
then rewrote the file without opening a session, which left the bench
holding `OVERTEMP` against the test's ceiling until the next job. The fixed
test (forgefirm f274eb1) waits for the engine's phase to leave `run` after
every M9 and cycles a session after restoring; it was hot-deployed to the
board for this run and is in the next dev image.

## 2026-08-21: the job's limits pass through, seen on a live session

`cloud.pause-resume` PASS on dev image `20260821220926` (campaign
`c-20260821222752-4d93`, 22:30:52Z), the first print under the header
pass-through. The two logs together, from the same session:

- Every hunt and motion file the service sent carried a coolant window of
  10 to 50 C; the client derived `coolant_max_c=50.0 coolant_min_c=10.0`
  from each, and the engine answered `effective limits: coolant ceiling
  33.0 C (local 33.0, header 50.0)` with `header coolant ceiling 50.0 C is
  not stricter than the local 33.0 C; the local one stands`.
- The print carried `air_assist_min_rpm=116 coolant_max_c=33.0
  coolant_min_c=5.0` (the captured cut-job values: `AArx` 64500 us, `CMrx`
  33000, `CMrn` 5000); the engine resolved the ceiling at 33.0 (equal to
  the local one, so the local stands) and published the floors (coolant
  5.0 C, air assist 116 rpm, exhaust and intake 0) for the gates to come.
- At the job's end the limits left with it and the effective set fell
  back to local.

Two refinements from the run, neither a behavior change: the "not
stricter" notice printed twice per job (forgectrl e0b41b3 names it once per
value), and the test quoted the session's first job-limits line, a hunt's,
where the print's is the one worth keeping (it now takes the first line
after the print's action request).

## 2026-08-22: the fan floors measured, and a hunt that would have tripped them

`fan_floor_measure.py spinup` (bench page `fan-floor`, 120 s at the cut
profile from idle, GRBL mode, the exhaust duct's inline booster fan off, so
the exhaust worked against more back pressure than a normal cut):

| fan | steady rpm | min | max | sd | t90 |
|---|---|---|---|---|---|
| exhaust | 11638 | 11444 | 11947 | 103 | 5 s |
| intake 1 | 4157 | 4102 | 4193 | 12 | 7 s |
| intake 2 | 4158 | 4128 | 4173 | 6 | 7 s |
| air assist | 11048 | 11029 | 11061 | 8 | 1 s |

Purge current 627 at idle and 625 at run duty: the engine holds purge air on
continuously, so both are the "on" reading (the off reading, ~1, is from an
earlier observation). The floors shipped from this: exhaust 6400, intake
2290, air assist 6000 rpm (55 percent of steady, bands 50 to 60 percent),
purge current 300, grace 15 s (twice the slowest time to 90 percent). The
provisional floors had come from a snapshot at a lower exhaust speed; the
measured margin is larger.

The run also sent the first `/cool/status` with the limits and the fan rows
through a 512-byte reply buffer, and then a 160-byte limits fragment: twice
a cut-off document, found by the measurement tool refusing to parse it.
Both fixed in forgectrl with a host test that renders the widest legal
document and parses it (`tests/coolfmt_test.c`).

Reading the measurement through cloud mode found a defect in the gates as
first built: the service reports every action as a run, and its hunt and
motion headers command the exhaust and the intakes off and the air assist
at idle, so a hunt longer than the grace would have tripped `AIRFLOW` at
any floor. Decision (operator): a fan is judged only at the operating
point its floor was measured at: always while the laser is armed, when a
job's profile may raise a fan but never lower it below the run duty; and
unarmed when commanded at the run duty. A hunt is measured, published as
`unjudged`, and not judged. `cloud.mode-switch` now watches the connect-time
hunt's gate rows for it.

Both tests ran the same day on a hot-deployed forgectrl (the working tree
cross-built and installed over dev image `20260821230723`; informational,
not acceptance). `cloud.mode-switch` PASS: the hunt reported two run ticks
with the exhaust off and `unjudged`, verdict `OK` throughout.
`cooling.fan-gate-trips` FAIL first, for two reasons worth keeping: the
run-start tick resolved the effective limits before it reloaded the
settings, so for one tick `gates_off` named the exhaust while its row still
carried the old floor (fixed: the session start re-resolves the limits after
the reload, so rows, `gates_off` and the log line agree); and the test's
2 s grace, chosen against the provisional 1800 rpm intake floor, now let
both intakes trip at ~1850 rpm on their 7 s spin-up (the test grace is 8 s).
Rerun PASS in 59 s: only the exhaust tripped in the exhaust leg, only the
purge in the purge leg, the exhaust read `off` with floor 0 in the off leg,
and the restore showed every fan `ok` at the shipped floors (exhaust 11723,
intakes 4157 and 4162, air assist 11078 rpm, purge 628 counts).

## 2026-08-22: the measured floors and the operating-point rule on a pinned image

Dev image `20260822135848` (forgectrl 47e4256 pinned by forgefirm adcd1ad;
release `20260822135751` built alongside), flashed after a fetch-verified
both-image build. Campaign `c-20260822140659-2f25`, every auto test the
pin bump invalidated plus the rest of the non-operator, non-live catalog:
**18 of 18 PASS** (cooling.flow-verify, image.health, kernel.latch-locked-idle,
kernel.k1-k2, kernel.backtrack-bounds, forgectrl.auth,
forgectrl.settings-bounds, forgectrl.panel-serves, logs.tree-tail-export,
logs.level-settings, motion.deadman, cooling.fans-quiet-after-motion,
cooling.gate-off, cooling.fan-gate-trips, camera.sensor-profile,
camera.frame-health, cloud.mode-switch, kernel.fire-line).

The two that carry this change, as recorded: `cooling.fan-gate-trips` in
58 s with only the exhaust `TRIPPED` in its leg, only the purge in its,
the exhaust `off` at floor 0 in the same tick `gates_off` named it, and
the restore reading exhaust 11726, intakes 4160 and 4193, air assist
11095 rpm, purge 627 counts, every gate `ok` at the shipped floors;
`cloud.mode-switch` in 29 s with the connect-time hunt's run tick reading
the exhaust at 0 rpm, `unjudged`, the air assist `unjudged`, verdict `OK`
throughout, and the hunt finishing `:completed`. The hunt's run phase is a
few seconds long and gave one sample at a 1 s poll, so the watcher now
samples twice a second.

## 2026-08-22: the unplugged-exhaust-fan drill

The gate on the real failure path, not a settings override: the operator
unplugged the exhaust fan's whole connector at the Interconnect PCB (fan
dead, tach silent), the machine idle in GRBL mode, lid closed, nothing
armed. One `M8` session from the board, `/cool/status` read once a second
(dev image `20260822135848`):

- 1 to 14 s: every gate `grace` (the shipped 15 s), exhaust reading 0.
- 15 and 16 s: exhaust `under` at 0 rpm; intakes, air assist and purge
  `ok`, up to speed inside the grace.
- 17 s: verdict **`AIRFLOW`**, `fire_ok false`, `hold true`, exhaust
  `TRIPPED`, reason `AIRFLOW: exhaust 0 under the 6400 floor for 3 s -
  hold, no resume this job`; the other four fans held at run duty around
  the dead one. grblHAL relayed the reason on the Grbl port as a
  `[MSG:Warning: ...]`, and forgectrl logged the `WARNING` line.
- `M9` ended the session into the smoke-clear phase with the fault still
  named; the operator replugged the connector.
- The next `M8` session: exhaust 4112 rpm at 1 s, 6623 at 2 s (past the
  floor), 11640 at 7 s (the measured time to 90 percent), `ok` with every
  gate at the end of the grace, verdict `OK`, clean end.

One observation for a decision: between the two sessions the engine sat
at idle with the verdict still `AIRFLOW`, `hold=true`, `fire_ok=false`.
The fan fault latches for the run session and clears at the next session
start, the same shape as the fire alarm; at idle the hold cancels GRBL
jogs and the cloud client's print pre-check refuses a print before a
session could re-prove the fan (a hunt clears it). Decision (operator,
the same day): the fault ends with its session, since every session
judges every fan afresh after the grace before anything can fire;
`cooling.fan-gate-trips` now checks the verdict is `OK` with no hold once
the tripped session is over. The fire alarm keeps its idle hold. Built
and flashed the same day (dev image `20260822145201`, forgectrl d51dbdb):
`cooling.fan-gate-trips` PASS in 60 s, the engine reading `OK`, no hold,
fire allowed in the smoke-clear phase right after the tripped session.

## 2026-08-22: the coolant critical tier, on an image and on a rising loop

Dev image `20260822154257` (forgectrl a1875a8 pinned by forgefirm f51140e):
`cooling.critical-tier` PASS in 23 s. As recorded: the settings API refused
a critical line equal to the ceiling (`400 cool_temp_critical_c must be
above cool_temp_max`) and changed nothing; with the ceiling at 6 C, the
resume gate at 5 C and the critical line at 7 C under 24.3 C coolant the
session read `CRITICAL` (`fire_ok false`, `hold true`, no `resume_ok`,
reason `CRITICAL: coolant 24.3 C at or over the 7 C critical line - hold,
no resume this job`); after the session the ceiling alone held
(`OVERTEMP`); with the critical line at its top of 70 the gate was off
(`gates_off` naming `coolant_critical`, the run-start log line) and the
ceiling alone paused; restored, `OK` with nothing off.

The physical drill, `critical_tier_drill.py`, the same day. The loop heater
reaches the high twenties at most, so the lines were set a few tenths
above the live upstream reading (24.57 C: ceiling 25.0, resume 24.8,
critical 25.3) and the engine's own flow-check heater (100 percent, 300 s
windows, rechecks every 30 s, the suspect threshold at its top) warmed the
loop through them inside one `M8` session. Transitions, as sampled once a
second: `OK` at 24.1 C; `OVERTEMP` at 10 s with the upstream at 25.05 C
(`coolant 25.0 C over 25 C limit - hold until 25 C`); `CRITICAL` at 14 s
at 26.24 C (`fire_ok false`, `hold true`, relayed on the Grbl port as a
`[MSG:Warning: CRITICAL: ...]`). `M9` ended the fault and the ceiling's
`OVERTEMP` stood in the smoke-clear phase (upstream 27.7 C, downstream
49.7 C from the heater, which the session end switched off). Settings
restored and re-read in a short session: `OK`, limits back to 33 / 31 / 38.

One find, cosmetic: after the session the reason text still read the
critical line's message under the ceiling's verdict, because the ceiling
names itself only on its rising edge and the critical fault had overwritten
it. The engine now re-publishes the standing hold's reason when a critical
fault clears (no new log line), and `cooling.critical-tier` checks it.

## 2026-08-22: the board temperatures on an image, and a cross-check that bound too far

Dev image `20260822165832` (forgectrl 76115fd pinned by forgefirm 9fae47c):
`cooling.critical-tier` PASS; `cooling.gate-off` FAIL in its off leg: the
POST that sets the ceiling to its off end (60 C) came back `400
cool_temp_critical_c must be above cool_temp_max`, because the step 3
cross-check compared the default critical line (38 C) against the ceiling
wherever the ceiling stood. Under the settings rule every gate is off by
value on its own, so a ceiling at its off end is no ceiling and the
critical line stands alone as the fail tier: the cross-check now binds
only while the ceiling is a gate (its own table row's off end decides),
`cooling.critical-tier` pins that the off-end POST is accepted with the
default line, and the unit fake mirrors it. The test restored the
settings on its failure path as designed (the trip leg had passed: a 6 C
ceiling read `OVERTEMP` in 1 s). Rebuilt and flashed as dev image
`20260822174523`: `cooling.gate-off` and `cooling.critical-tier` PASS.

## 2026-08-22: the pulse-header envelope closed out

Dev image `20260822182931` (forgectrl b27398a, python3-gfhardware e65cfc2
pinned by meta-openglow 6bfd26e and forgefirm 4e0c90b), the close-out
image of the envelope work. Campaign `c-20260822183742-95a9`, every
unattended test: **18 of 18 PASS**, the same set as the 2026-08-22
morning campaign plus `cooling.critical-tier`.

What the new instrumentation said on the machine: `/status` `temps`
read chassis 29.9 C, SoC die 44.0 C, supply 602 raw, throttle state 0 at
idle; the run-end line after a session read `temps this job: chassis
26.8..26.9 C, soc 36.6..37.1 C, supply raw 549..553`; and the cloud
client's connect-time hunt logged `79 of 101 header keys have no applier
here (30 declared ignored, 49 undecided)`. The 49 are the families the
disposition table calls undecided (the client's network backoff, the air
filter's fans, the camera exposure and gain values, the per-phase idle
variants of the limits), named at debug level by every job; the number is
recorded here so the next decision on them starts from a measurement.

The first build of this image ran on a stale layer: the launch's shell
session closed while the source sync was still copying, rsync took the
hangup, and meta-openglow stayed one commit behind (the old gfhardware
pin). The build was stopped, the tree synced and every moved pin checked
in it, and the build relaunched detached; the image manifest carries all
three components at the intended commits.

## 2026-08-22: the operator's part cut down, and the first campaign with it

Dev image `20260822204234` (forgefirm de324cc packaged forgetest, the
pins unchanged from the envelope close-out), the first image with the
catalog as rebuilt for fewer hands: every attended test asking for its
operator's part by name (Ready prompts before timed steps, standing
notices the test takes down when the machine shows the action done,
one confirm by eye left), `cloud.mode-switch` carrying the lid-open
hunt and the web-service homing, and `kernel.fire-line`,
`camera.snapshot` and `motion.jog-roundtrip` run unattended.

Three bench findings, all in the harness, none in the machine:

- `motion.jog-roundtrip` failed its first run on the accelerometer
  witness: the sysfs read lands two or three samples in a one-second
  leg, and two samples on the constant-velocity stretch read near idle
  with the head in full flight (the accelerometer sees the ramps, not
  the travel); where a ramp was caught the head was plainly moving (p2p
  3019, 1330, 1698, 1663). The verdict became the whole sequence (p2p
  across the eight legs at or above the liveness threshold, motion on at
  least two distinct legs). Rerun: p2p 2897 over 17 samples, motion on
  three legs. forgefirm 9139e92.
- `laser.arm-wait-lid` failed its first run on "the button is still
  lit": the cancel had relocked, disarmed and emitted nothing, but the
  check read the LEDs' `brightness` the instant after, and the smooth
  trigger fades it; the controller writes `target`. The readback is the
  commanded level now, with a few seconds for it to land. forgefirm
  296fd68.
- The operator's campaign showed `cloud.oversize-stream` and
  `cloud.pause-cancel-paths` both cancelling a print from the app. The
  app cancel stays in the oversize test, which has to end that way, and
  is judged in full there; the other became `cloud.paused-lid-cancel`,
  one print instead of two. Same commit.

Those fixes showed the catalog's implementation hash for what it was: a
whole suite file, so a two-line fix in `laser.py` re-required every
laser test and the cloud rename every cloud test. The hash is now the
test's own function plus the module's code outside the `@test`
functions (forgefirm 2547a8e), every recorded fingerprint moved once,
and the campaign was run from nothing on the board's installed copy of
that tree (the six changed files verified identical to the commit).

Campaign `c-20260822220701-a1c0`: **43 of 43 PASS, nothing inherited,
release authorized**; 29 minutes of test time in all, the 16 attended
tests 19 minutes of it (22:16 to 22:46 UTC) against the catalog's own
111-minute estimate for the attended set before this work. No release
cut. What the witnesses read on the machine: the head accelerometer
p2p 4206/2442 over 17 samples across the jogs, motion on four legs; the
beam detector idle 1864, peak 2364 during the emission witness (delta
500 against the 300 the test asks, digital flag seen), 479 during the
pause/resume/lid cut; the lid camera 98.8 kB lit against 49.5 kB with
the lamp off; the cloud round trip's hunt `:completed` with the lid
open and gfhome's `homing complete (service quiet 10s, 6 motion
windows)` 40 s after `$H`. Every one of the 74 machine actions the
campaign asked for was performed by the operator and recorded so in
the evidence; a bench actuator, when there is one, takes the same
calls.

## 2026-08-22: the machine's print behavior without the service

Dev image `20260822232347` (forgefirm 628f2f7; python3-gfutilities 768730e
and python3-gfhardware a3ca36f pinned by meta-openglow a52e68c and the
forgefirm-app pin), the first image with the offline service: gfcloud
restarted under the `/run/gfcloud-offline` marker comes up with no
account and no network, takes the service's action messages on
`/run/gfcloud-offline.sock`, and hands the machine's events back. Four
cloud tests run on it with a job synthesized on the board
(`forgetest/puls.py`: a factory print's header over a square the laser is
never commanded on): the lid and interlock aborts, the button-wait
cancel, a paused print ended by the lid, and a print longer than the ring
ended the way the app ends one. Nothing on the bed; the arm press is the
operator's only hand on a print.

Before the operator's run the plumbing was dry-checked from a shell:
stop, marker, start, the `OFFLINE service` line and the socket, a
`settings` action answered `settings:completed`, then a restart without
the marker and the web session `ready` again. One lesson from the dry
script, not the harness: the marker has to stay until the offline line
is logged, because the supervisor reports the client running seconds
before Python has finished importing and read it.

Campaign `c-20260822233344-08de`: 30 of 43 inherited across the pin bump
(the catalog's covers put every `cloud.*` test on the moved components,
and the core always runs), **13 run, 13 PASS, 43 of 43, release
authorized**; 11 minutes of test time, the four offline tests 5.5 of it.
No release cut. What the machine said under the offline service: the
lid edge to the stop 10 ms; the button-wait cancel with no run started;
the paused print cancelled by the lid, parked to the job start, latch
locked, button dark; the long job (33.4 MiB of ticks in an 87 kB gzip)
live-fed with the kernel's program total climbing 33.29 to 34.60 MB while
the report divided by the job's 35.0 MB, no underrun, the backtrack held
at 164 214 steps, the pause and resume taken, and the cancel's tail the
same as the lid's. Every print's `print:running`,
`print:return_to_home:succeeded` and `print:cancelled` came back over
the socket. The real service was still proven on the same image by
`cloud.mode-switch` and the one real print, `cloud.pause-resume`.

## 2026-08-23: the service protocol proven by the emulator, and the hunt paid only where it is the subject

Three dev images in one day, each a campaign, the last one full and
clean.

**Dev image `20260823002125`** (forgefirm 4c9dcca, python3-gfhardware
12ad3b1 pinned by meta-openglow a4e3abf, the first image with the
`python3-gfutilities-emulator` fixtures) carried a layer change, so every
test was owed. Campaign `c-20260823140444-80ae`: the 27 unattended tests
passed in 10 minutes. Before the operator's part, the emulator path was
dry-checked from a shell the way the offline one had been, and it caught
a defect the host replays could not: the session came up (sign-in, the
firmware check, the WebSocket ready) and the service sent `settings` and
nothing else. The real client's hunt lands 1 to 2 s after `ws_connect
ESTABLISHED`; the emulator waited minutes. `build_emulator` had set
`EMULATOR.BYPASS_HOMING`, which makes gfutilities answer the settings
request with `"settings":{}`, the reconnect form the service answers by
keeping its head position and skipping the hunt. The fix (gfhardware
b7e8035: the report carries the values; a host test proves it red on the
old flag) was hot-patched on the bench for the rest of the dry-check: the
hunt landed 1 s after the settings report and completed; the service was
satisfied after two home frames and one motion (the real client takes
four frames and three motions); the app showed Ready; a Print from the
app reached the emulator 7 s later, behind a pre-print motion pair and a
`lidar_image` request the emulator answered, and downloaded (20 KB
gzip, 643 KB of pulses, a 134-tag header, STfr 10000) and completed. The
shipped file was put back and the real client restarted before anything
else ran.

**The operator's change, before the next image:** a mode switch from GRBL
to cloud costs the service's connect-time hunt, and during cloud
development those add up. gfcloud gained `--no-hunt` and the one-start
marker `/run/gfcloud-nohunt` (gfhardware 351a623): the first settings
report goes out in the reconnect form, which is what the factory client
does on every reconnect within a session. With it, every one-start marker
is now read and taken down by the client itself, first thing, before the
imports that take seconds on this board, so a respawn never inherits one
and a writer can move on once the supervisor reports the client up.
forgetest (forgefirm 969bac6) sets the marker for every cloud client it
starts except where the hunt is the subject: `cloud.mode-switch` and
`cloud.service-protocol` keep theirs, and so does the one real print,
since `enter_cloud` now reuses a running session only when that client
has hunted the machine itself (`session_hunted`: never the emulator's
session, never a no-hunt start) and otherwise restarts the client with
the hunt. The decision, the operator's: all starts skip the hunt but
those three. The hazard it leaves, written into ACCEPTANCE.md: a machine
a campaign leaves in cloud mode may not have hunted since GRBL mode moved
the head, so a lid cycle or a controller restart before printing from
the app.

**Dev image `20260823153019`** (forgefirm 908e0c7, gfhardware 351a623 by
meta-openglow 9e988b5). The pin-file mechanism held across the bump: 21
tests inherited, the 6 always-required core tests ran (76 s), and the
operator took the attended block: the four motion tests, the five laser
tests, `camera.lid-privacy` and `cloud.mode-switch` passed, and
`cloud.service-protocol` ERRORed on its first line, `forgectrl POST /mode:
timed out`. Two defects, one each side. forgectrl's mode switch answers
only after the new controller's first job-state report to the cooling
engine, 15 s without one; the real client reports within seconds of its
machine coming up, and the emulator never reported at all, so the answer
came at the deadline, past forgetest's 10 s client timeout (the dry-check
had used curl, which has none, and so never showed it). The emulator now
runs the same idle, unarmed 1 Hz reporter as the hardware machine
(gfhardware 537d0db), and forgetest gives the supervisor's three levers
(`/mode`, `/controller/start`, `/controller/stop`) a 120 s timeout, above
the daemon's own waits (forgefirm ab0a515).

**Dev image `20260823161333`** (forgefirm 8379aa5, gfhardware 537d0db by
meta-openglow 730db53). Campaign `c-20260823161923-0dd7`: 29 inherited,
the 6 core tests in 74 s, then 9 attended: the emission witness,
`camera.lid-privacy`, `cloud.mode-switch`, `cloud.service-protocol` (68 s:
the session, the hunt, three image uploads, a print from the app
downloaded with its 134-tag header and completed against the real
service with nothing behind it, then the real client back in 14 s under
`NO-HUNT` with no hunt), the four offline tests, and the one real print,
`cloud.pause-resume`, which found the offline client running and started
a fresh one with its own hunt before printing, as the rule requires.
**44 of 44, release authorized**, 868 s of attended test time. No release
cut. The offline client is what a campaign now leaves running in cloud
mode; the next thing that needs the service restarts it.

What this closes: the cloud split of the acceptance plan is complete.
The service protocol is proven by the emulator with only the app to
drive, the machine's print behavior by the offline service with nothing
on the bed, and the two together by one real print. Still open from that
plan: the bench actuator for the lid, interlock and button, and the
finer coverage maps.

## 2026-08-24: the GPU demosaic's first light, and what the probes caught

Dev image 20260824122014 (the first with Mesa etnaviv; release ext4
204,140,544 bytes, ~5 MiB under the slot cap) flashed by the operator;
the drill ran forgectrl builds from `/tmp` against the running image,
each iteration probed over the stream, `FORGECTRL_GPU_CHECK`, and frame
captures diffed against the CPU demosaic on the host.

Five faults found and fixed in one session, each named by a probe log
line or the compare (forgectrl 6614833):

1. `eglChooseConfig` returned nothing: EGL_SURFACE_TYPE defaults to
   WINDOW_BIT and the surfaceless platform has no window configs. Ask
   for surface type 0.
2. Every fourth output byte was 255: the render engine writes an
   XRGB8888 surface's undefined X byte as opaque. Render ARGB8888.
3. The raw import failed etnaviv's stride check (width padded to 16
   texels): 2592 bytes is 1296 GR88 texels exactly, not 656 padded
   XRGB ones. Import GR88, one texel per Bayer pair.
4. The GPU cannot write the CODA's buffers at all: 64-byte render rows
   versus round_up(width,16) strides never meet at these widths. New
   `ipu_copy` module: render into the IPU CSC/scaler's wider source
   (stride align(w,128)) and let the IPU crop into each encoder over
   dmabuf. 14 ms a copy, no CPU touch.
5. The chroma mirror used a quarter-width plane where the plane is
   half-width: the right half of both chroma planes clamped to
   column 0. The three-frame diff-by-transform analysis on the host
   named both this and fault 2.

End state on the bench: `convert: "gpu"` serving MJPEG, the GPU/CPU
compare clean to 2 counts except the bottom row (1296 samples, max
delta 134, unexplained); `/cam/h264` delivering valid fragmented MP4
from the CODA BIT processor (avc1.424020, ~480 kbit/s on the static
bed). Open, measured: the render costs 140 ms a frame against the IPU's
14 (GPU at its full 528 MHz - the suspicion is pre-HALTI linear-texture
sampling), so the GPU path holds ~6 fps until that is run down. The
`getenv` implicit-declaration fix in debayer.c rode along. Bench left
clean; stock service restored.

## 2026-08-24: the render run to ground, and the chroma box paid for

Second session on the flashed fixes (dev 20260824131335, drills from
/tmp, forgectrl 2d59d78). Findings by measurement:

- The GPU has LINEAR_TEXTURE_SUPPORT (minor_features1 bit 22 read from
  debugfs), so Mesa samples the imported buffers directly: no shadow
  copy, and no risk of the seqno-gated shadow going stale under
  external DMA - a hazard that was checked for and does not exist here.
- FORGECTRL_GPU_PASSES decomposed the 140 ms render: 41 ms for luma,
  49 ms per chroma pass. The chroma box filter (four superpixels, 32
  dependent fetches per fragment) was the cost, sixteen times the
  per-fragment price of the luma pass.
- The chroma passes now point-sample the block's top-left superpixel:
  render 64 ms, stream ~9 fps at ~7 % daemon CPU (NEON: 15 fps at
  41 %). Luma stays bit-clean against the CPU path (max delta 1, zero
  samples off by more than 2), and the first session's bottom-row
  artifact went with the old chroma pass. Chroma against the box
  reference reads mean 1.7 on the bench scene: detail, not error.
- CSI hardware frame skip proven with the GPU path:
  FORGECTRL_STREAM_FPS=7 programs keep-1-of-2 in the receiver,
  hw_fps_skip true, steady ~7 fps, the daemon sampling 0.0 % in top.
  The loop split at rest: wait 26, render 64, IPU copy 14, encode 7 -
  15 fps needs the render overlapped with the previous frame's encode,
  recorded as the item-20 remainder.

Bench left clean; stock service restored.

## 2026-08-24: the pipeline goes two frames deep

Third session, on dev 20260824133616 (drills from /tmp, forgectrl
deee6a1). The serialized loop (wait 26, render 64, IPU copy 14,
encode 7) became a two-frame pipeline: a frame's render is kicked
behind an EGL fence and the previous frame's finished render is
cropped, encoded and published while it runs. ipu_copy keeps two
source buffers so the rendering and the copying frame never share one;
the rendering frame's capture buffer stays out of the queue until its
fence clears, and every teardown, failure and frame-health queue cycle
settles the in-flight state first.

Measured: 13.8 fps single-viewer (from 9.2), fence stall 7-9 ms
against the 64 ms render - the render is hidden behind the copies,
the encodes and the frame wait. Daemon ~14 % CPU at that rate with a
WiFi viewer attached (the NEON path: 15 fps at 41 %). MJPEG and H.264
served together run 9.8 fps with the stall at zero (two IPU copies and
two encodes per frame, 33 ms, all still off-CPU). Luma stays bit-clean
against the CPU demosaic; H.264 fragments now carry the delivered
frame's timestamps. Bench left clean; stock service restored.

## 2026-08-24: the browser plays it, and the head moves under it

Fourth session, on dev 20260824140057 (the shipped image runs the
pipelined GPU path stock: convert gpu, 13.6 fps, before any drill
binary). Chrome driven against the panel found what byte-level checks
could not:

- The video element buffered data at t=801 s while playback sat at
  zero: the fragments carried the raw 90 kHz boot clock. Each viewer's
  fragments are now zero-based (the mux context subtracts the first
  frame's clock).
- The live-edge chaser's fixed 0.2 s back-off overshot the one-frame
  buffered window into a gap and the element stalled at readyState 0;
  the seek now clamps inside the newest buffered range, and a paused
  element is kicked back into play after a seek.

With the fixes (forgectrl d97cb35, drill from /tmp): the panel's Live
button plays H.264 over MSE at 1296x972, timeline from zero, no MJPEG
fallback, verified by script and by eye in Chrome.

Coexistence, with the H.264 view live in the browser AND an MJPEG
viewer attached: a jog out (+X 5 mm F600) and back completed at its
commanded feed (mid-status Jog, MPos 3.619, FS 600; end Idle at
origin), the step ring's underrun counter read 0 before and 0 after,
and the planner buffer never left 99-100. The GPU stream path and
motion coexist. The laser latch stayed locked and emission dark
throughout; no armed anything.

What remains of the video offload: the full acceptance campaign on an
image carrying d97cb35 (a platform change: Mesa joined the image), and
first light of all of it on an 8 MP machine when one exists. Bench
left clean; stock service restored.

## 2026-08-24: the kernel built for one board

A read-only review of the running kernel (config, dmesg, bindings,
module tree, image manifests) found the multi-board defconfig doing
what multi-board defconfigs do: USB, Ethernet, CAN, Bluetooth, SATA,
PCIe, NAND, audio, a display stack, touchscreens, ten other i.MX SoCs
and 153 DVB modules, none with a node in the device tree or a driver
bound. Two real defects sat among them: `evbug` autoloading for the
switch block and logging every lid and button transition to the
kernel log, and the fragment's hung-task and soft-lockup panic lines
silently dropped because their detectors were off, so only
`PANIC_ON_OOPS` stood behind the laser-safing notifier. No crash
record existed either: `panic=10` rebooted and the reason left with
it.

The fragment was rewritten as the board's driver set plus the
defconfig's leftovers turned off, and the machine conf names the
modules and firmware the rootfs carries. The first configure pass
taught what the defconfig never says: `PM`, the regulator core and
`EXT4_FS` only ever arrived by selection from suspend, the PMICs and
ext3, so they are pinned by name now. Built into dev 20260824164619:
zImage 9.13 MB to 4.76 MB, kernel-module packages 254 to 31,
`/lib/firmware` down to the WL18xx set and the DualLite VPU blob,
ARMv7-only code, no virtual console, ramoops in the 1 MiB the factory
bootloader already holds back at the top of DRAM, and ecspi2 without
`dmas`, so the pulse ring is the SDMA's only client (the ROM scripts
stay; the RAM firmware never loaded and no client here needs it).

On the bench, fresh boot of that image: every node binds and nothing
defers; `hung_task_panic` and `softlockup_panic` read 1, `panic` 10;
`/sys/fs/pstore` mounts and ramoops registers at 0x2ff00000 with ECC
(the ten "uncorrectable error in header" lines are the never-written
region's first initialization, expected once); `/dev/dri/renderD128`
present and both cameras streamed through the GPU demosaic
(`gpu: GLES2 debayer up` for lid and head, GPU interrupts 0 to 135,
snapshot 200 OK); Wi-Fi associated with the regulatory database
loaded; the switches on `event0`; 31 modules loaded, `evbug` gone; no
DMA channel held by anyone. MemTotal rose by 9.4 MB. Two new dmesg
lines, both cosmetic: spi-imx reports the absent DMA channel at ERR
level and continues in PIO (the PIC probes and reads), and
`consoleblank=0` is now an unknown parameter without a virtual
console. `cannot start cut; no data enqueued` at 31 s is not new (47
earlier occurrences in the kernel log).

The crash record, proven the direct way: `echo c > /proc/sysrq-trigger`
panicked the kernel, the ten-second timeout rebooted it, and the next
boot logged no header errors and mounted `/sys/fs/pstore` holding
`dmesg-ramoops-0` (24 KB, "Panic#1 Part1", the kmsg buffer from
"Booting Linux" to the panic) and `console-ramoops-0` (23 KB, ending
"sysrq: Trigger a crash / Kernel panic - not syncing / Rebooting in
10 seconds.. / ECC: No errors detected"). A panic now leaves its
reason where the next boot can read it.

Still owed: a GRBL job on the image, the acceptance campaign (platform
change), and the `spi_device_id` table for `glowforge,pic`, which
rides the module's next pin bump. Bench left clean.

## 2026-08-24: the second kernel round, on the bench

Dev 20260824200726, cold boot after the flash (the pstore region came up
empty and re-initialized its headers, as a power cycle must). The dmesg
lines the round set out to remove are gone: no spi-imx "can't get the TX
DMA channel", no `consoleblank` in the unknown-parameter list (only
`board=`, which userspace reads), no "cannot start cut; no data enqueued"
(grblHAL treats that run-on-empty-ring race as ordinary; the module now
agrees), no `spi_device_id` warning (the pinned module carries the
table), no "unconfigured mac address in nvs". One line took its place:
with no NVS on the rootfs the firmware loader reports the missing file
at ERR level; patch 0015 asks for the optional file the quiet way and
rides the next build.

The kernel is UP: `nproc` 1, no IPI rows, the TWD still the tick and the
GPT the clocksource. The performance governor is the only one and the
core reads 996 MHz. Wi-Fi associated on `wl18xx-fw-4.bin` with
`wl18xx-conf.bin` beside it and nothing else in `ti-connectivity`; PG 2.2
silicon, firmware 8.9.0.0.83, regulatory database loaded.

IPv6: the kernel took the router advertisement (link-local, a ULA by
SLAAC, the ULA and GUA prefix routes, the default route) and `udhcpc6`
ran from the `wlan0 inet6` stanza. It got no address: the DHCPv6 server
answered every Solicit with an IA_NA carrying only a status option (18
bytes, which busybox reports as "IA_NA option is too short"), which is
NoAddrsAvail; the GUA prefix is advertised on-link without the
autonomous flag. So the board has a routable ULA and no GUA until the
network hands one out; the client side is doing its part. Every service
answered over IPv6 on the ULA from the board itself: sshd (banner),
grblHAL TCP:23, forgectrl :8080 (200), forgetest :8090 (200);
`netstat` shows all four on `:::`. This host sits on another IPv6 LAN,
so cross-network reachability was not testable from here.

The rootfs: nano and `file` present on the dev image, the udev hardware
database gone, `cryptography` not importable while `urllib3` and
`requests` import; `/lib/firmware` down to the WL18xx pair and the
DualLite VPU blob. A sanitized log export ran (200, 1.9 MB) with the
`system/` snapshot in place; the `system/pstore/` directory appears only
when records exist, and after the cold boot there were none. Memory
475 MB total, 325 MB available at idle in GRBL mode.

Owed: the NVS line (patch 0015, next build), the item-16 drill on this
kernel, the campaign. Bench left clean.

## 2026-08-24: the second DHCPv6 responder

Dev 20260824201945 (patch 0015 in): the NVS loader line is gone; the
rest of the second round holds. The missing GUA was not the firewall's
doing. Its DHCPv6 server was enabled, in Managed RA mode, with a pool on
the delegated /64, and a packet capture on the bench VLAN showed it
answering the board's Solicit with an address 1.3 ms later. The board's
Request went to a different server-ID (a UUID) with `NoAddrsAvail`
echoed back. A raw sniff on the board's own link named the other party:
one of the VLAN's three OpenWrt access points still ran its LAN-side
defaults, RA in server mode (its own ULA prefix with SLAAC, M and O
flags, itself as DNS) and a DHCPv6 server with nothing to hand out. Its
unicast Advertise beat the firewall's, and busybox's `udhcpc6` keeps the
first Advertise it sees and keeps Requesting from that server, which is
where the "IA_NA option is too short" line came from (an IA_NA carrying
only a status code). The other two access points have RA, DHCPv6 and
NDP-Proxy disabled, which is the setting that belongs on all three. The
ULA the board carried all along was that access point's. Nothing on the
board needs to change; the network side owns the fix.

With RA and DHCPv6 disabled on that access point, the next Solicit took
the firewall's lease: a global address on wlan0 (a /128 with the lease
as its lifetime, renewed on schedule), sshd, grblHAL TCP:23, forgectrl
and forgetest all answered on it from a host on a different VLAN, and
the board reached the IPv6 WAN gateway. IPv6 is on end to end; the
stale ULA ages out with its own lifetime.

## 2026-08-24: the SDMA clocks, held by nobody

The first campaign on dev 20260824201945 (`c-20260824204310-6b6d`) stalled
on `image.health`: the pre-baseline waited its full 150 s for
`motion=verified`, which forgectrl never reported, and the test then
failed on `cnc/free 35618816 exceeds the ring less its 32 KiB gap`. The
forgectrl log had the shape of it: `liveness probe: ERROR - cannot start
the probe run` at every controller spawn on every boot since the first
kernel-trim image (164619), and `MOTION OK` with a healthy p2p on every
boot before it, the last at 17:47Z on 161618. The kernel log had nothing,
because the one line that would have said so had been demoted to
`dev_dbg` the same afternoon on the belief that it was grblHAL's benign
race.

The ring's own readbacks named the fault. `cnc/position` read X =
0x200000 steps on a machine that had not moved (forgectrl showed 39321.60
mm), the head index sat 2 MiB ahead of the tail on an idle ring, and
scratch6/7 read back the script's constants (the 0x01ffffff index mask
and the PWM sample-register address), which is the start of the channel
context, not its scratch registers. Every context fetch was returning the
bounce page as last written, not SDMA memory. `/sys/kernel/debug/clk/sdma`
confirmed it: `clk_enable_count 0`, prepare 1, the engine unclocked.

The mechanism: imx-sdma enables the engine's `ipg` and `ahb` clocks only
in `sdma_alloc_chan_resources`, for a dmaengine client, and disables them
at the end of probe. glowforge.ko takes channel 26 through the SDMA API
patch's `sdma_get_channel()`, which returned `&sdma->channel[ch]` and
nothing more. Until the trim, spi-imx on ecspi2 held two SDMA channels
and so held the clocks; the pulse engine had run on that accident since
its first image. The round-1 device tree deleted ecspi2's `dmas` on
purpose (the ring as the only SDMA client), and took the last clock holder
with it. With the block gated a channel-0 transfer completes at once and
moves nothing: the script load, the context load, the head sync and the
position fetch all "succeed"; `cnc/run` sees head == tail right after the
tail publish and returns -ENODATA; grblHAL treats that as its ordinary
race and carries on idle; `verify_sdma_script` passes by construction,
because the write copies the script into the same bounce page the read
returns. The "47 earlier occurrences" of `cannot start cut; no data
enqueued` on 164619 and the "no DMA channel held by anyone" observation
were this fault, read as noise. The SDMA RAM firmware is not involved: it
never loaded on any image.

The fix, and what proves it so far: `sdma_get_channel()` enables both
clocks and a new `sdma_put_channel()` releases them (patch 0003 and the
API header); the module calls put in remove and in the probe unwind; the
empty-ring run request logs at ERR level again; `image.health` asserts
`clk_enable_count >= 1` directly, ahead of the 150 s settle it would
otherwise wait out. The ecspi2 `dmas` stay deleted. Host-proven: the
patch round-trips against the kernel tree with 0008 on top, the kernel
object compiles with no new warnings, the module compiles under `-Werror`
(its modpost waits on the rebuilt kernel's export), the module's host
tests and the 270 forgetest tests pass. Owed: the image, then on the
bench `clk_enable_count` reading 1, `MOTION OK` from the probe,
`cnc/free` at 33521664 idle, a GRBL job, and the campaign.

## 2026-08-24: the clocks proven, and the listener that heard nobody

Dev 20260824215906, built with the SDMA clock fix, on the bench: `sdma`
`clk_enable_count` 1, the supervisor's probe `MOTION OK` (p2p x=3390
y=1720), `/mode` verified, `cnc/free` 33521664 at idle, position 0.
Campaign `c-20260824223050-0356` (36 unattended, the fixture in the loop):
`image.health` passed in seconds with its new clock assertion, and every
kernel, forgectrl, logs and motion test passed, `motion.liveness-probe`
and `motion.button-hold-resume` among them. `cooling.flow-verify` passed.
`cooling.fans-quiet-after-motion` failed: `M8 did not raise the fan duty
off idle`.

The engine had heard nothing. `/cool/status` showed `report_age_s` -1 for
the controller the supervisor had just respawned, and a hand-sent
`POST /cool/state?mode=idle` from 127.0.0.1 answered `403 loopback only`.
The listener is dual-stack since the second kernel round (`:::8080`), so
every peer arrives as a `sockaddr_in6`, the IPv4 client as
`::ffff:127.0.0.1`. forgectrl's check handles that spelling; ulfius 2.7.15
does not hand it over: `src/ulfius.c` allocates and copies
`client_address` as `sizeof(struct sockaddr)`, 16 bytes, which holds the
family, the port, the flow label and eight address bytes. The mapped
prefix and the 127 sit at bytes 10 to 12 of the address, past the copy,
in heap the check should never have read. Every report since dev
20260824200726 was refused the same way; nothing ran the cooling tests on
those images until now. The direction was safe: the engine treats silence
as a stand-down, so no run profile, no armed window, no fire.

The fix and its proof so far: the image carries a ulfius patch (a
`sockaddr_storage` allocation, a copy of the family's length, in the
dispatcher and in `ulfius_copy_request`); the recipe builds it clean under
ulfius's own `-Werror -Wconversion`. The peer check moved into
`forgectrl/src/peer.c` unchanged in meaning, with `tests/auth_peer_test.c`
in CI: 127/8, `::1` and mapped 127/8 pass; LAN addresses in both families,
a mapped LAN address, link-local, unspecified, `AF_UNIX`, NULL and a
`::ffff:127.0.0.1` cut to sixteen bytes are refused. forgectrl cross-builds
under `-Werror`. `forgectrl.auth` now asserts the loopback acceptance
(200) next to the LAN refusal (403), so a listener that truncates the
peer fails the catalog on the first forgectrl test rather than the first
cooling one. Owed: the image, the loopback report accepted on the bench,
the campaign.

## 2026-08-24: the listener heard, and the campaign ran through

Dev 20260824230512 (the ulfius peer patch, forgectrl 78efd16 with
`src/peer.c`, the SDMA clock fix underneath) on the bench 54 s after
boot: `POST /cool/state` from 127.0.0.1 answered 200 and the same report
from the board's LAN address with the token answered `403 loopback only`;
`/cool/status` showed `report_age_s` 0.1 from the freshly spawned
controller; the SDMA clock count 1, the probe `MOTION OK` (p2p x=1879
y=1906), `cnc/free` 33521664. Campaign `c-20260824231028-b7ca`, the 36
unattended tests with the fixture in the loop: 36 passed in 13 minutes,
`forgectrl.auth` with its loopback assertion, `motion.liveness-probe`,
`cooling.fans-quiet-after-motion` (the fans up on M8 through the accepted
channel, quiet again within the cooldown), the fan-gate trips, both
unattended laser tests, the cameras, the update slots and the two cloud
tests. Nothing inherited: the image is a platform change twice over. The
nine attended tests (four laser live, five cloud) stand between this
image and an authorized release. The bench was left in cloud mode on the
offline client, as the cloud tests leave it; nothing of the session's on
the board.

## 2026-08-24: the attended nine, and a release authorized

The operator ran the nine attended tests on dev 20260824230512 after the
unattended 36, in one sitting: `laser.emission-witness` (23:25Z, 33 s),
`laser.disarm-in-hold` (83 s), `laser.armed-kill` (66 s),
`laser.pause-resume-lid-cancel` (31 s), `cloud.service-protocol` (65 s),
`cloud.lid-interlock-abort` (66 s), `cloud.pause-resume` (198 s),
`cloud.oversize-stream` (166 s) and `cloud.paused-lid-cancel` (23:37Z,
28 s): every one passed. Campaign `c-20260824231028-b7ca` closed at 45 of
45 from nothing, the whole of it in 27 minutes of test time (36 unattended
in 13, the attended block in 12), and the export authorizes the image.
No release is cut. This is the first campaign on an image carrying the
board-only kernel, the SDMA clock fix and the ulfius peer patch together,
so it is the bench proof of all three, and the first with the bench actuator
doing the operator's door, interlock and button work end to end.

With it, three BRINGUP items close and two working files at the tree root
are merged: item 12's campaign narrative, item 20 (the video offload's bench
validation, `camera.h264-stream` in the campaign), item 21 (the kernel
trim, the campaign being what it owed), the acceptance burden plan (every
step landed, its decisions taken) and the kernel configuration review (its
status section is the record of what changed). Their texts, as they stood,
are in "Superseded status notes" below; what stays open went into BRINGUP
items 12, 13, 16 and the new item 20.

## 2026-08-24: the SoC under a full core, and where it settles

The die read 65.6 C (150 F) with the camera stream running and nothing
else, in a 30 C chassis, which was enough of a number to ask what a full
core does to it. The drill: `openssl speed -seconds 50 sha256` on the one
core for 300 s, on top of the live stream, cloud mode at idle with the
laser locked, a monitor sampling the thermal zone, `cpufreq-cpu0`, both GPU
cooling devices and the load average every 5 s. The die climbed 2.9 C in
the first 30 s and 4.6 C by 2.5 minutes, then sat at **70.8 C (159 F)** from
3.5 minutes to the end, at 0 percent idle and a load average near 3. No
cooling device left state 0, the core stayed at 996 MHz, `/status` read
`soc_throttle 0` throughout, and the driver's grade line in dmesg is the
one the facts bank quotes: `Commercial CPU temperature grade - max:95C
critical:90C passive:85C`. Thirty-five seconds after the load ended the
die was back at 67.9 C.

Read: the bare SoC, no heatsink, holds **14 C of headroom to the passive
trip and 19 C to the poweroff** under the worst load the one core can
produce, in a 30 C chassis. The die-to-chassis delta at full load is about
41 C, so by arithmetic, not measurement, a chassis above roughly 44 C is
what reaches the passive trip; the load alone does not. The facts bank's
open question, whether ForgeFIRM's load wants the heatsink the factory's
never did, closes with this entry: it does not. The per-job SoC range and
the throttle log line stay as the running record.

## 2026-08-25: the performance-curve ladders, and the rapids that fired after M5

The day opened with a new instrument. The head carries a thermopile that
reads scatter off the beam inside the head, upstream of the mirror that turns
it down to the work, so it sees the beam and not the material. A ladder of
100 mm lines at 10 mm/s, one per level, sampled from sysfs at 25 Hz along
with the HV current, is a performance curve for this tube and supply, and the
`pcurve` drill in `scripts/bench/live_fire_drills.py` runs it.

- **Analog ladder (E1), `$35` = 0, 13 rungs from 16 to 100 percent plus a
  repeat of rung 7:** the current is proportional to duty above 30 percent
  (slope 959 counts per 100 percent, r-squared 0.9999) and reaches 990 at
  full, so this PSU's ADC does not clip; below 30 percent the discharge is
  unstable. The thermopile is monotonic to 85 percent and puts the lasing
  knee between 19.7 and 22.8 percent duty, not at 16, with the strike spot
  showing as a first-second spike on the low rungs; its baseline holds within
  50 counts over the ladder and the repeat rung reads 3.5 percent high. It
  does not settle inside a line above about 50 percent (swings of 20 to 30
  percent at constant current), so the top of the analog curve is not yet a
  measurement. Record `pcurve_analog_20260825-195947.json`.
- **Density ladder (E3), `$35` = 0, period 20, minimum 3, 13 rungs from 1 to
  100 percent:** the dose is strongly convex in density at a 710 us period
  (80 percent of density reads 0.53 of full, 60 reads 0.37, 45 reads 0.21,
  30 reads 0.07), while `laser_on_sampled` tracked the commanded on-fraction
  exactly, so the drive delivered what was asked and the light did not
  follow. Whether that is the per-pulse strike deficit or the sensor is the
  next ladder's question. Lines were flat inside to within a few percent.
  Record `pcurve_density_20260825-202317.json`.

**The rapids fired after `M5`.** Seen by the operator on the density block
and confirmed in both traces: the pulsed current ran on through the `G0`
back and the `G0` up after every line, at the rung's level, and through a
bare `G0` sent with no `M3` at all. Under density that is full-power light
where nothing was commanded. `M5` executed with the stream idle only stored
the off state; the stream re-asserts its wanted fire state at the first byte
of every run, and the wanted state was still the last cut's true. Live fire
stopped.

**The first fix made the second job dark.** Pushing `fire=false` on `M5`
darkened the rapids (bench run 1 passed: the current fell from 393 to 0
inside one 40 ms sample) and then every following job in the same controller
process shipped no fire at all (runs 2 and 3, HV 0..0, motion ran). That was
first read as hardware, with the `laser power-good degraded` warning as the
suspect. It was software, and it reproduces on the null sink with two jobs
in one process: the second G1 ships zero FIRE ticks under both models.

**The root cause is a core contract.** grblHAL's per-segment laser update is
edge-triggered on rpm: `set_state(on, rpm)` records the rpm, and a block at
that same rpm gets no `update_pwm`, because the core takes the driver's
`set_state` as having lit the laser. Our `spindleSetState` pushed the duty
only. A process's first job always fired because the parser starts in G0,
where the `M3` and `S` words run at rpm 0 and the first G1 differs; after
`M2` the motion mode is G1 and S is modal, so the next job's `M3` runs at the
old level, the core records it, and nothing lights the G1 except the stale
wanted state. The old build fired job 2 by that accident, the same stale flag
that lit the rapids; removing the accident exposed the hole.

**The fix, and its proof.** `spindleSetState` now computes the pwm for the
state it is given (the off value when off, refused, or rpm 0) and pushes it
through `spindleUpdatePWM`, the whole state through the same armed and
coolant gates; the duty-only stream call is gone. Harness rule 17 and the
`next-job` sessions (two jobs in one process, `M2` between, same S) join
rule 16 and the `m5-idle` sessions; the build that went dark fails the new
session with one fire span, and the fix passes all 14 stream sessions, the
13 lifecycle cases and the arm test. On the bench, with the corrected
controller hot-installed: `m5dark` run 4 (the process's first job) and run 5
(its second, the case that went dark) both passed, 2.00 s of discharge, the
`M5` taking the current to 0 inside one sample, both rapids and every dwell
dark over 11.4 s of sampling, the operator confirming by eye. The catalog
gains `laser.m5-rapid-dark` (46 tests).

**Power-good is not a witness of anything here.** The factory 2.6.0 binary
carries no power-good string at all; ForgeFIRM warns on it once per armed
window and reports it in `/status`, and nothing gates fire on it. On this PSU
it reads not-good at full tube current.

A bench note for the next hot install: a file copied to the board with `scp`
lands without its execute bit, and busybox `cp` keeps that, so the supervisor
loops on exit 127 until a `chmod 755`.

## 2026-08-26: step timing under CPU contention closed

The operator closed BRINGUP "Next work" item 16, step timing under CPU
contention: the video work resolved it. The basis is above (2026-08-24, "the
SoC under a full core"): the kernel runs UP with the performance governor as
the only governor, the hardware frame skip of the video offload halves the
dequeues that the cache maintenance rides on, and the catalog test
`motion.step-timing-under-load` passed on that image with no clamped events.
The stream-live re-measure and the camera gate that the item still listed
are not owed. The item is removed from BRINGUP, and the items after it are
renumbered: 17 to 20 are now 16 to 19. `GFSINK_LEAD_MS` (default 10) and
the per-run margin report stay as shipped.

## 2026-08-29: the flow check under laser load, Tests 1 and 2

The 2026-08-25 flow-check trip (heater rise 15.1 C against the 14.4 C limit,
dT 9.4, while the first `dpatch` patch fired CW through the check window) was
run down with a new drill, `flowload` in `scripts/bench/live_fire_drills.py`.
`flowload t1` puts the three check keys at their defaults for the run and
fires two 30 x 4 mm CW fills (F1500, 0.3 mm pitch, about 35 s lit) on the
press with no dark dwell; `flowload t2 <secs> [pct]` writes
`cool_flow_check_s = 0` for the run and fires one fill sized to the lit
seconds at CW or at a density level; `flowload fit` fits rise against dose
over the t2 records. The sampler is the `dpatch` one plus
`thermal/heater_pwm`, `/cool/status` is polled at 1 Hz with the fan gates,
and every controller reply is kept. Every key the drill writes goes back to
what stood before when the run ends. The pump was never commanded off.
Records: `bench-data/flowload_t1_20260829-*.json` and
`bench-data/flowload_t2_*_20260829-*.json` in the tree.

**Test 1, three runs.** The check starts at the session open (the heater
comes on about one second after the M3, not at the press), so the press must
come at once for the fire to overlap the window. Runs 1 and 2 ended their
windows early (the drill closed the session on M2 before the 50 s were up;
fixed: the drill now holds the window open until the heater trace ends).
Run 3 ran the full window with the tube lit for 70 % of it, and the engine
read `coolant flow verified (heater rise 14.1 C, dT 9.5 C)`: 0.3 C from a
SUSPECT, where the same loop reads 11.7 to 12.1 C dark. The trip is
reproduced in kind, and it is not flow. Two things stack:

- **A common-mode ADC offset while the run airflow profile is on.** One
  sample after the session opens (fans to run duty) both coolant sensors
  drop 1.0 to 1.9 C together; they step back up when the fans return to
  idle; in between the readings toggle between two levels 0.6 to 1.1 C apart,
  both sensors in lockstep, up to 22 times in a run, with every fan steady
  at speed. The one session in which the air-assist fan never left idle
  showed no step and no toggling, the only pointer to a source so far. The
  engine captures `flow_base_down` from one sample at its first tick after
  the heater starts, inside that offset, so every rise carries about +1.1
  to +1.4 C from the offset and about +-0.5 C of single-sample scatter, dark
  or lit.
- **The tube's heat at the sensors.** Test 2 below: about 1.5 C inside a
  fully lit 50 s CW window.

Dark 11.7 plus the tube's 1.5 plus one low base sample reaches 14.1; the
15.1 trip is the tail of the same distribution.

**Test 2, the tube's signature, check off.** CW bursts of 20.8, 40.8, 47.9
and 59.0 s and one 59.4 s burst at 45 % density (S450). With the ADC offset
steps masked (a step is both sensors' half-second mean levels changing by
0.45 C or more the same way, agreeing within 0.4 C, subtracted from all
later samples), the downstream rise at burst end against the `hv_current`
integral is linear through the origin: **k = 3.06e-5 C per raw-second**
(r2 0.981, intercept 0.04 C), 0.030 C per lit second at hv 971, so **a fully
lit 50 s CW check window adds 1.49 C** against the 1.6 C margin; the rise
50 s after fire start read 1.43 to 1.49 C on every burst long enough. The
lag from first emission to the first sensor response is 10 to 20 s, a
smooth ramp on both sensors together, never a step at fire start. At 45 %
density the same window adds 0.46 C, and the heat per raw-second is 0.77 of
CW: the current integral overstates density heat, so a tracer needs one k
per power model.

**Events on the way.** One `t2 40` run held at +7 s on the airflow gate
(`air_assist 1895 under the 6000 floor for 3 s`): the air-assist fan never
left its idle reading inside the 15 s grace, the first air-assist trip on
record; it spun up normally in every other session. The first `t2 60` run
was written to the controller as one 93-line block, about 1270 bytes
against the 1023-byte RX ring, and the serial layer drops bytes on a full
ring, so the fill ran 46 s instead of 60 and the job's M5 and M2 were lost:
the window stayed open (engine phase `run`, `armed` true) until the drill
exited and dropped the connection. The next `t2 60` then ran its whole fill
with no arm, no button wait, no run report and no airflow: the driver's
spindle-state record was still on from the lost M5, and the arm at the
first laser-on is skipped when that record reads on. Fire stayed suppressed
at the stream (no HV, no emission, thermopile flat), so no energy left the
tube, but the head ran a full job without the operator's press. This is
BRINGUP "Next work" item 20 (arm on `state.on && !laser_ok`, clear the
spindle state in `gflaser_disarm`, consume the RX overflow flag), a fix
owed before the next image. The drill now feeds the job against the `Bf:`
free-character count, sends and acknowledges M5 before every run, refuses
to start while `/cool/status` shows the window armed, acknowledges M2 and
waits for the window to close, and prints the controller's replies: the
last three runs show `press the button to start the laser job`,
`laser armed (density)` on the press, and `Pgm End` with
`laser disarmed - latch locked` on the M2.

**Also seen.** `laser power-good degraded during the armed window` is
warned by the engine at every session open, a separate item. The check
window's own baseline and the tube term are the two candidates the fix
chooses between; nothing is built yet.

## 2026-08-29: the arm-skip and RX-overrun fix, host-proven

The driver fix for BRINGUP "Next work" item 20 is written in
grblHAL-glowforge and proven on the host; it is not yet on an image.

**The arm.** `spindleSetState` now arms on `state.on && !laser_ok`: the
consent question reads the window alone, never the previous spindle state.
`gflaser_disarm` leaves the spindle-state record alone, since it is the
core's own view (`spindleGetState`, the `A:S` field, planner sync) and the
condition no longer depends on it. `tests/laser_arm_test.c` gained case H:
arm through the press, bump the sender generation, `gflaser_poll` closes
the window with the record still on, the next laser-on must run the button
wait again and arm; and case I: a laser-on inside the open window does not
re-prompt. On the old condition case H fails three checks; on the new one
the whole harness passes.

**The ring.** In `serial.c`, `rx_byte` on a full ring now drops the
overrunning line whole: what the ring already holds of it is unwritten back
to the last newline, the rest is discarded through the line's own newline,
real-time characters keep passing (they are taken before the ring), and
the overrun is latched. The driver's realtime hook takes it once, logs it,
reports `RX overrun: the sender ignored flow control (Bf:); job aborted` to
the sender and enqueues `^X`, the same stop as the lid cancel: controlled
deceleration, latch relocked, alarm. `tests/serial_test.c` (new, in CMake
and CI) pushes 73 lines of 14 bytes, overruns on the 74th with a `?` in
the middle, and checks: one byte free after 1022, the `?` taken, the
overrun reported once and only once, every line that fit delivered whole,
no fragment left behind, the next line after the overrun whole, and the
same with the overrun landing mid-line; 11 checks pass.

**The null-sink harness** (`laser_lifecycle_test.py`, over TCP, no
hardware) gained two scenarios and passes 15 of 15: `sender-change-mid-job`
(M4, a 5 s move, the socket closed at 1 s with the spindle on, reconnect,
`M3 S100` must prompt and re-arm; the disarm message itself is written while
no client is connected and is discarded, so the re-arm is the evidence) and
`rx-overrun` (an armed job, then 120 lines written at once: the overrun
report arrives, the state goes to Alarm, the window closes, and after `$X`
a clean job arms again). `laser_stream_test.py` still passes.

**What a sender change means in Grbl terms**, for BRINGUP item 21: neither
Grbl nor grblHAL knows a sender is present; a lost connection leaves the
controller executing what its planner and RX ring hold, then waiting; the
core's `stream_disconnect` only switches streams; senders treat the loss as
a failed job. ForgeFIRM keeps that for the motion and adds the disarm.

## 2026-08-29: the flow check's reading, host-proven

The cooling engine's flow check (forgectrl `cool.c`) now reads its rise
from means and takes the tube's share off before the limit; written and
proven on the host, not yet on an image (BRINGUP item 22).

**The reading.** The baseline is the mean of the settled window the gate
has just verified (15 samples at 1 Hz), and that history restarts when the
run airflow profile is applied, so the window is taken entirely under the
profile and the ADC offset that comes with it sits on both sides of the
rise; the arm-time check therefore starts about 15 s after the session
opens instead of one second after. The end reading is the mean of the
check's last 5 s. The tube's share is one coefficient per power model
(`cool_laser_heat_cw` 3.06e-5, `cool_laser_heat_density` 2.36e-5 C per
raw-second of `pic/hv_current`, the numbers of the 2026-08-29 burst runs)
times the current integral from 15 s before the window to 15 s before its
end (the heat's lag to the sensor; emission later than that has not
arrived, so it is not counted, the safe side), bounded at 3 C so no setting
can subtract the check away. The verdict lines carry the share and the raw
rise: `coolant flow verified (heater rise 11.8 C, dT 10.7 C; laser 1.5 off
13.2)`. The two keys are validated in `main.c` (0 to 2e-4) and described in
`SERVICES.md`; the diagnostics' own flow-verify and flow-calibrate are
untouched, since they run with the tube dark.

**The proof.** `tests/cool_flow_test.c` (new, in CMake and CI) includes the
engine source against a fake sysfs tree and a fake clock, with a loop model
that carries the run-profile offset (-1 C stepping in at the session open
and toggling 0.6 C), the heater's rise with flow (12 C plateau) or without
(0.4 C per second) and the tube's heat arriving 15 s after emission. Fifteen
checks pass: a dark check with flow is verified with no share and its
baseline is the window's mean; a dark check without flow is SUSPECT; a lit
CW check with flow is verified with about 1.5 C taken off (raw 13.2, judged
11.8); a lit check without flow is SUSPECT (20.2 raw, 18.8 judged); an
absurd coefficient is bounded at 3 C and no flow is still SUSPECT (17.2);
under the density model the density coefficient applies (1.1 off). The
`flowload` drill's verdict parser accepts the new suffix.

## 2026-08-29: image 20260829190323, the flow check under load on the bench

Pins forgefirm d577629 (grblHAL-glowforge a7dcdca, forgectrl 2f18b16),
both images built rc=0 from the committed trees, the dev image flashed by
the operator at 19:11. Three `flowload t1` runs from the installed drill,
each a prompt press with the tube lit for 58 to 63 percent of the check
window, the check opening 4 to 7 s after the fire (about 15 s after the
session open, from the fresh history):

| run | engine line | lit |
|---|---|---|
| 191517 | `verified (heater rise 11.3 C, dT 9.6 C; laser 0.8 off 12.0)` | 58 % |
| 192051 | `verified (heater rise 11.1 C, dT 9.4 C; laser 0.7 off 11.9)` | 63 % |
| 192439 | `verified (heater rise 11.8 C, dT 9.8 C; laser 0.7 off 12.5)` | 58 % |

The judged rise sits in the loop's dark band, 2.6 to 3.3 C under the
14.4 C limit, where the same conditions read 14.1 in the morning. The arm
sequence on the new driver was clean in every run (prompt, armed on the
press, `Pgm End` and the disarm at program end, the window closed with
the M2, all 55 lines queued at once). The power-good warning at the session
open persists (BRINGUP item 23). Board left idle, heater off, conf
restored, nothing under `/data`; records in `bench-data/`.

## 2026-08-29: the flow check from a warm loop (Test 3)

The plan's third test: the check's bands from a heater-warmed loop, the
tube dark, run from the bench page as the `flow-warm` takeover
(`flow_warm_validate.py 3`, forgectrl and the controller stopped for the
run and restarted on its exit), three checks with the pump on and three
with it commanded off, alternating, each from a fresh warm-up. The tool's
warm target reads the upstream sensor, which sits near the heater and
reaches 28 C within two minutes while the mixed bulk settles near 24.5, so
the baselines landed at 23.6 to 24.9 C rather than the 28 to 30 the plan
asked for; the tree's copy now takes the target and the warm-up budget as
arguments (defaults 28 C, 20 min, registered on the bench page), and a
warm-up judged on the mixed bulk is the follow-up.

| case | rises (C) | band |
|---|---|---|
| pump on | 11.54, 12.15, 11.78 | max 12.15 (cold data: 12.75) |
| pump off | 18.97, 18.70, 18.07 | min 18.07 (cold data: 16.04) |

Every verdict correct; the 14.4 C limit sits 2.25 C above the warm flow
band and 3.67 C below the warm no-flow band, a 5.9 C gap where the cold
data has 3.3. A warmer loop sheds the heater's heat no worse with the pump
on and holds it better with the pump off, so `cool_flow_rise` needs no
warm-end value through 25 C; above that is not measured. Records:
`bench-data/flow_warm_log_20260829.txt`, `flow_warm_results_20260829.json`.

## 2026-08-29: the arm-skip and RX-overrun fix on the bench

Two live-fire drills on image 20260829190323, both new in
`scripts/bench/live_fire_drills.py`, both passed.

**`senderchg`** (record `bench-data/senderchg_20260829-195933.json`): a
20 mm line at F60 lit on the press, the connection dropped 5.0 s in with
the tube lit, a new session, the move finishing dark, then a fresh M3 and
a 5 mm line. The engine's window closed 1.3 s after the drop; `hv_current`
read dark within one 40 ms sample of it and the thermopile fell to its
floor by 0.4 s; the head ran the remaining 15 mm without a sender (the
Grbl expectation, BRINGUP item 21); the fresh M3 prompted for the button
again, the second press armed, and the 5 mm line marked. Lit samples:
177 on the first line, 0 between the drop and the second press, 31 on the
second line. `cnc/laser_on_sampled` is a one-second window count and reads
nonzero for up to a second after the beam stops, so the drills open their
"nothing lit" window 2.5 s after the event and read `hv_current` and the
thermopile for the instant.

**`overrun`** (record `bench-data/overrun_20260829-200256.json`): the same
first line, and 3 s in a 93-line fill (1270 bytes) written at once against
the 1023-byte ring. `RX overrun: the sender ignored flow control (Bf:);
job aborted` 0.3 s after the blast, `ALARM:3`, the disarm and the reset
banner; `hv_current` dark 0.11 s after the blast; the engine's window
closed within the second. After `$X` the fresh M3 prompted again and the
second press cut the 5 mm line. Lit samples: 134 before the alarm, 0
between the alarm and the second press, 87 on the second line. The report
and the reset came twice, 0.1 s apart: the tail of the blast arrived after
the first reset had flushed the ring and overran it again. Harmless, the
job was already stopped.

## 2026-08-29: the air-assist fan and the airflow gate, twice

Twice in the day the airflow gate held a job at the end of its 15 s grace
with the air-assist tach still at its idle reading (1895 at 17:09 in a
`flowload t2 40` run, 2207 at 20:19 in the first bench run of
`cooling.flow-under-load`; the floor is 6000 and every other session of the
day read about 10,700 within 5 s of the run profile). The head probed clean
and the kernel log carried no I2C error; the fan read normal at idle both
times. The gate did what it is for: the hold came before the arm, the
operator's press was refused, the session closed with the fans, and no job
ran without air. The operator's reading is a bench hardware glitch, the
head's pogo-pin connection to the air assist; the machine was powered down
and the head reseated, and the case was run again. Not a project item.

## 2026-08-29: `cooling.flow-under-load`, the catalog's case for the lit check

The catalog case for BRINGUP item 21 (`forgetest/suite/cooling.py`, kind
live, mode grbl, one press): two 30 x 4 mm fills at full power on the press,
the window held open until the engine's verdict lands in the forgectrl log,
then M2. PASS needs `coolant flow verified` with the laser's share on the
line (at least 0.3 C, the proof the window and the fire overlapped) and the
judged rise at least 1 C under `cool_flow_rise`; an arm refused by a gate
names the gate. First run on image 20260829190323 (the module staged over
the installed suite, forgetest restarted; the prerequisites overridden, no
campaign results on this image yet): the airflow gate held before the arm
(the entry above). Second run after the head reseat: **PASS**, the engine
line `coolant flow verified (heater rise 11.9 C, dT 9.5 C; laser 0.6 off
12.5)`, 66 s from job to verdict, the window closed 0.0 s after M2, the
head jogged back by the baseline; the case now brings the head back
itself. The case rides the next image; its campaign standing comes with
that image's campaign.

## 2026-08-29: the coolant ADC offset is the air-assist fan's return current

The common-mode offset on the two coolant sensors (about 1 C low while the
run airflow profile is on, BRINGUP item 21) was run down differentially,
dark, with `scripts/bench/offset_probe.py`: forgectrl idle, one actuator
switched at a time, both sensors at 25 Hz, the step at every edge scored
as the 1.5 s means after minus before. Records
`bench-data/offset_probe_20260829-205928.json` (the survey) and
`offset_probe_20260829-210233.json` (the ladder).

The wiring first, from the OpenGlow board's netlist (pin-compatible with
the factory board): the thermistors enter on J2 pins 3 and 4 with the pump
enable (2), the TEC enable (1), the TEC thermistor (6), the heater PWM (7),
the exhaust tach (8) and the exhaust PWM (9) beside them, then 12 V, the
beam-detect lines, Z step and direction, the head I2C and the head camera
lanes; the intake fans, the HV lines, `LASER_ON` and `HV_EN` are on J1;
the air assist is driven on the head. The run profile drives the exhaust
at 65535 (100 %, no PWM edges) and the intakes at 43278.

The survey: exhaust at 100 %, 50 % and 25 %, the intakes at their run
duty, purge, the TEC enable and the lid lamp each move both sensors by
0.1 C or less; the heater and the pump edges move the downstream sensor
only (thermal). The air assist from its idle 204 to its run 1023 steps
both sensors together, -1.37 and -1.25 C in one sample, and back +1.18 and
+1.13; "all run fans" gives the same -1.28 and -1.23. The ladder: 256
-0.03, 512 -0.27, 768 -0.6, 1023 -1.2 C cumulative, both sensors alike,
each step reversed on the way down, and -1.2 to -1.3 C on two full on and
off repeats. The offset is proportional to the air-assist fan's current: a
ground-return drop on a path the thermistor reference shares, not
crosstalk on J2 and not HV (the `flowload` traces already show the step
before any HV and no further step at emission). Both sensors read low by
the same amount whenever the air assist runs, so the flow check's rise is
untouched now that its baseline is taken under the run profile, and the
over-temperature gates read the coolant about 1.2 C cooler than it is
during a job. The mid-run toggling between two levels is not reproduced by
a steady fan (0 toggles in every dwell); the fan's own current variation
under motion is the remaining candidate.

## 2026-08-29: the toggling is not motion

`offset_probe.py jog` (record `bench-data/offset_jog_20260829-211710.json`),
dark, no press: the air assist steady at its run duty while the gantry
jogged 30 mm in X and 8 mm in Y for 40 s, then the same jog with the fan
idle. Zero level toggles in every phase; with the fan on the readings sat
1.1 C low and as quiet while jogging (sd 0.26 C) as while still (0.32),
and the fan's tach held 677 to 678 under motion. Motion and the head's
pogo contacts under vibration are out. The toggling seen in the day's
`flowload t2` traces sits inside the armed windows only (from the moment
`armed` went true, past the burst's end, gone when the fans went idle), and
the dark runs with the fans at run duty show none, which leaves the HV
supply's enable, asserted from the press to the disarm, as the candidate;
an armed dark dwell (`M3 S0`, the window open, no emission) is the test.

## 2026-08-29: the toggling needs the tube lit

`offset_probe.py armed` (record `bench-data/offset_armed_20260829-212245.json`):
`M3 S0`, the press 0.3 s after the M3, the window open 73 s with the head
still, the flow check's heater running inside it, no emission (0 lit
samples on the LASER_ON witness and the tube current), then M2. Zero level
toggles on both sensors through the armed window; the one step after M2
is the fans returning to idle. The `hv_enable` switch read false
throughout, so it does not follow the arm. Every toggle in the day's
`flowload t2` traces sits inside a lit period (seven between +7 and +34 s
around a burst lit from +7 to +27; thirteen under a 41 s burst;
twenty-two under a 48 s burst), and none appear dark, with the fans
alone, under motion, or in an armed dark window. The jitter comes with
tube current: the HV supply's input current on a return the thermistor
reference shares, or its switching, is what remains, and a scope on the
two sensor lines during a cut is the next instrument. Its size is 0.6 to
1.1 C either way, inside the over-temperature ceiling's 2 C hysteresis,
and the flow check reads means.

## 2026-08-29: the air-assist offset taken off the coolant readings, on the bench

forgectrl `cool_aa_offset_counts` (f9b4893, the status link fix 25cf969),
image 20260829214735 flashed by the operator. The correction is in ADC
counts, keyed to the air-assist duty the engine commands, taken off both
raw readings in the engine and in `/status` (more counts read colder, so
the fan's ground lift reads as a drop and the correction subtracts; the
host test caught the first cut adding it).

**The calibrate tool** (`aa-offset-calibrate`, the panel's "Calibrate
coolant offset") ran end to end on the bench: three idle-to-run cycles,
six edges reading 12.7/18.3, -15.8/-18.5, 16.3/16.8, -12.5/-10.3,
15.7/17.8, -15.7/-21.0 counts (down/up), mean 16.0. It refused its own
result on the spread (10.7 counts against its 8-count limit): 1.5 s at
4 Hz is six samples a side against about 5 counts of single-sample noise.
The tool now reads 3 s at 8 Hz a side (forgectrl 42cdb71, the next
image); the value was applied directly, `cool_aa_offset_counts = 16`, this
machine's number.

**The proof** (`scripts/bench/aa_offset_check.py`, dark, no press: M8
brings the fans to the run profile, the raw counts, `/status` and the
engine's readings averaged before, during and after). Uncorrected, the
upstream reading dropped 1.02 C under the run profile (raw +15.2 counts).
Corrected, with the flow check off for the session so its heater stayed
out of the downstream sensor: the raw counts stepped +15.4 / +13.3 and
`/status` read 23.96 / 23.94 against 23.87 / 23.96 before, +0.09 and
-0.02 C; the engine's own readings +0.31 / +0.25 inside the same window.
The readings hold still while the fan runs. A first run of the check had
left `cool_flow_check_s` at 0 (the script's restore posted an empty value
and got a 400; fixed); the setting was put back to 50 and the engine
re-read it at the next session.

## 2026-08-29: the calibrate tool's recommendation, on image 20260829220329

forgectrl 42cdb71 (each edge read over 3 s at 8 Hz a side), image
20260829220329 flashed by the operator. `aa-offset-calibrate` from the
panel's API, dark: six edges 15.5/16.1, -14.3/-14.2, 16.2/17.3,
-13.8/-14.8, 17.8/17.4, -18.1/-18.3 counts (down/up), mean 16.2, spread
4.5, recommendation 16.2, which is the value already applied. The
compensation's whole path stands on the bench: the tool measures the
machine's number, Apply writes it, and both coolant readings hold within
0.1 C when the run airflow comes on.

## 2026-08-29: the flow check from a warm loop, to the heater's ceiling

`flow_warm_validate.py` with its warm-up rewritten to judge the mixed bulk
(the heater at 50 % for three minutes, off, 45 s of circulation, the two
sensors' mean; a round that lifts the bulk under 0.15 C ends the warm-up),
run from the bench page as the `flow-warm` takeover with a 28 C target and
a 20 min budget, three checks pump on and three pump off, alternating.
The bulk plateaus at 27.2 C in this room, so the checks ran at 26.2 to
27.2 C baselines, the most the loop heater can give.

| case | rises (C) | band |
|---|---|---|
| pump on | 11.08, 11.89, 10.98 | max 11.89 |
| pump off | 18.69, 18.42, 18.00 | min 18.00 |

Every verdict correct; the 14.4 C limit sits 2.51 C above the warm flow
band and 3.60 C below the warm no-flow band. With the 19 to 23 C
characterization (12.75 / 16.04) and the 24 to 25 C run earlier in the
day (12.15 / 18.07), the bands hold from 19 to 27 C with the margin
widening warm. Above 27 C only a running tube warms this loop, and the
check takes the tube's share off; `cool_flow_rise` needs no warm-end
value. Records `bench-data/flow_warm_log_20260829b.txt`,
`flow_warm_results_20260829b.json`.

## 2026-08-31: the low-hanging items, one pass

One session over the small open items, grouped into one build and one
bench window. Host proof first, bench facts second, build third.

**Rail policy (item 8, closed).** The GRBL driver wrote `cnc/enable` at
init and at homing resume under the broker too. Now both writes run only
when the driver opened the device itself (`!gfio_pulse_inherited()`);
under the broker forgectrl owns the rail. The kernel probe starts in
`disabled`, so a standalone controller still needs the write.
grblHAL-glowforge fa9ed78; SERVICES.md "Rail policy" retagged
`[implemented]`, no `[contract]` item is left. Proof: the null-sink build,
`laser_stream_test.py` and `laser_lifecycle_test.py` green; a `$H` homing
pass on the built image is the bench check.

**`/cool/status` (item 8, closed).** Two changes in forgectrl 0e907f7.
`armed` in the document now comes from `coolfmt_armed()`: the last
report's flag while that report is within the 5 s timeout, false after
(`tests/coolfmt_test.c`, five cases). A run session that never had the
armed window open gets a zero-length smoke phase (`session_armed`,
cleared as a session opens and set on any armed tick), so a homing
motion, a hunt or a dark job goes run, thermal gate, idle. The cloud
hunt keeps reporting `run`: the cloud catalog asserts it (the hunt's
run phase carries the factory's motion fan profile), so the client is
right and the engine changed. forgectrl host tests 14 of 14 under
`-Werror`.

**`laser.armed-kill` placement (item 12, decided).** It stays in the
laser domain. The always-required core carries the emission witness with
the armed-window disarm; the kill path is the forgectrl supervisor, and
the `forgectrl/src/main.c` entry in the test's map re-requires it on each
forgectrl change. Recorded in the site's Acceptance page (forgefirm-docs
657d32e).

**Image trims (item 18).** The import audit (an AST pass over
python3-gfhardware, gfutilities, forgetest and the bench scripts against
poky's `python3-manifest.json`) maps the runtime imports to `python3-core`
plus `fcntl`, `json`, `logging`, `netclient`, `threading` (gfhardware and
the apps) and `datetime`, `io`, `json`, `logging`, `threading`
(gfutilities); forgetest adds `compression`, `crypt`, `io`, `math`,
`netclient`, `netserver`, `shell`, `statistics`. The recipes declare
those (meta-openglow 722bc00, forgetest.bb) and the image drops the
`python3` meta-package that pulled `python3-modules`. The four libraries
were not orphans: pkgdata names `libmicrohttpd` (its `https`
PACKAGECONFIG) and `ulfius` (`WITH_GNUTLS`) as the holders of gnutls,
which pulls nettle, gmp, libunistring and libtasn1. forgectrl serves
plain HTTP and no websockets, so `libmicrohttpd_%.bbappend` removes
`https` and ulfius builds with `-DWITH_GNUTLS=off -DWITH_WEBSOCKET=off`.
forgefirm b334c4c. Built as release 20260831130656: the manifest lists 30 `python3-*`
packages where the previous image had 62 (`python3-modules`,
`python3-tkinter`, `-2to3`, `-asyncio`, `-idle`, `-pydoc`, `-venv` and
the rest of the meta-package family gone; `python3-core`, `-fcntl`,
`-json`, `-logging`, `-netclient`, `-threading`, `-datetime`, `-io` and
what `requests`, `urllib3` and `websocket-client` pull stay), and
`libgnutls30`, `nettle`, `libgmp10` and `libtasn1-6` are gone.
`libunistring5` stays: `libidn2-0` holds it, and `libcurl4` holds
`libidn2-0` (curl's IDN support, about 1 MB together; not taken). The
release rootfs ext4 went from 151.3 MB to 129.8 MB and the wic.gz from
44.0 MB to 35.1 MB. Platform change: the full campaign is owed on this
image, and the cloud tests are the module check. The dev image is 20260831141210; against the release it adds only the
emulator fixtures, `python3-statistics` (declared by forgetest for the
bench scripts, with `python3-numbers` behind it) and `libgmp10` (gdb,
from `tools-debug`), so the campaign on it proves the release module set.

**Lid IR against the lamp (item 4).** The four `pic/lid_ir_*` channels
read at eleven `lid_led` levels (sysfs brightness 0 to 1023, 2 s settle,
three samples 1 s apart), lid closed, machine idle, on dev 20260831021059:

| lid_led | ir1 | ir2 | ir3 | ir4 |
|---|---|---|---|---|
| 0 | 2 | 2 | 1 to 2 | 2 |
| 64 | 18 to 19 | 17 | 18 to 19 | 20 |
| 128 | 32 to 33 | 32 | 33 to 35 | 34 to 35 |
| 192 | 43 to 44 | 42 to 43 | 44 to 45 | 45 to 46 |
| 256 | 54 to 56 | 54 to 55 | 57 to 60 | 60 to 61 |
| 384 | 77 | 75 to 77 | 81 to 83 | 82 to 85 |
| 512 | 96 to 98 | 95 to 97 | 103 to 104 | 104 to 105 |
| 640 | 115 | 113 to 115 | 121 to 123 | 123 to 124 |
| 768 | 131 to 133 | 131 to 133 | 139 to 140 | 141 to 143 |
| 896 | 146 to 148 | 147 to 148 | 157 to 160 | 157 to 159 |
| 1023 | 161 to 162 | 161 to 163 | 172 | 173 to 177 |

A straight line on every channel, about 0.16 counts per unit, channels 3
and 4 about 7 percent above 1 and 2. The factory header's alert (275)
and critical (688) sit above a fully lit lamp, and its baseline (3)
matches the dark floor (2): the factory rides out the lamp by choosing
thresholds above it. What stays unproven is the header's channel
mapping, since all four channels behave alike while the header leaves
the third and fourth quartiles at zero. The next cloud job's header is
the comparison.

**Wi-Fi SDIO (item 11).** `dmesg | grep -c "sdio .* failed"` read 0 after
30 min on dev 20260831021059.

**Lens-shading files (item 6).** `/data` is one partition for every slot,
and a search of it (names with cam, lenc, regs, lens, shad, calib, four
levels deep) found no camera register file. If the factory pushes such
files, the app fetches them at run time; a factory-slot session with the
app running is the step before any reimplementation.

**Debug-kernel checks (item 10, assessed).** Not a quick check: a module
unload powers the 40 V rail off (`stepper_power_off` in the remove path),
and a forced `-EPROBE_DEFER` needs the 40 V regulator or the SDMA device
unbound under the module's probe. Both are the rail-cycle gamble the
rail policy exists to avoid. The item now says so; it waits for a bench
slot that accepts the gamble.

**Bench hygiene.** The 2026-08-20 factory-session shim was still on the
board: `/data/manufacturing/run.sh` (the app launcher with the bench CA)
and `/data/glowforge.conf` (server URLs and pin pointed at the bench
proxy), while their `/data/bench-scratch/f1` was already gone. Removed as
the tool's teardown does (no `glowforge.conf.orig` existed, so the conf
is deleted and the factory app runs on its built-in defaults). `/data`
holds only factory state and ForgeFIRM's own files again.

## 2026-08-31: the bench pass on image 20260831141210

Dev 20260831141210 burned to the SD and booted. The apps and forgetest
import on the trimmed module set (`gfhardware`, `gfutilities`, the
machine, the cooling client, the camera, the websocket service; the
forgetest server, suite, bench and catalog); `/usr/lib/python3.12/tkinter`
is gone. Wi-Fi SDIO failures: 0 (the count is reset by the boot).

**Rail policy.** `$H` in GRBL mode with `homing_mode = gfcloud`: gfhome
ran the hunt and the homing motion and homed (X0.00 Y0.00 Z10.60, 10
motion windows on the head accelerometer), `ok` after 53.8 s, grbl
`Idle` and `cnc/state` `idle` after it, and a jog out and back (5 mm at
F600) ran on the resumed controller. `dmesg` carries one `40V on`, at
boot (18.6 s): the rail did not move through the handover or the
resume, and the driver wrote no `cnc/enable`.

**`/cool/status`.** Sampled at 2 Hz through both drills (116 and 111
samples). The homing session's three dark run sessions each went `run`
to `idle` with no `smoke` phase (13.6 to 15.7 s, 23.8 to 26.8 s, 35.9 to
38.0 s). The dark GRBL session (M8, a 10 mm jog out and back at F1200,
M9) went `run` at M8 and `idle` at M9, no `smoke` phase. `armed` read
false in every sample, `verdict` OK.

Nothing left on the board: the drill script was staged in `/tmp` and
removed, `/data` holds factory state and ForgeFIRM's own files. Owed on
this image: the full campaign (platform change).

## 2026-08-31: lens shading, resolved from the factory rootfs

Item 6 carried a per-unit lens-shading (OmniVision LENC) table the
factory was said to push into the sensor at every stream start. The
factory v2.6.0-2228 rootfs, dumped and searched, says otherwise:

- `/usr/bin/load_cam_regs.sh` exists: it takes `<lid|head> <regfile>`,
  writes each register line into `/sys/bus/i2c/devices/<bus>-0036/regs`,
  and remaps OV8858 `0x58xx` addresses to the OV8856's `0x59xx`. Nothing
  on the rootfs calls it (no script, no init file, no reference in the
  app binary).
- The app binary references `/usr/bin/apply_cam_regs.sh` next to its
  camera-selection strings. That script is not on the rootfs.
- Only `ov8856.ko` carries the `regs` attribute (`ov8856_regs_attr_store`)
  and an OTP mode; `ov5648.ko` has neither.
- On the bench machine (OV5648) `/data` holds no register file, and
  `/data/manufacturing` did not exist before the 2026-08-20 bench tool
  created it.

So no shipped machine applies a per-unit shading table: the loader is a
manufacturing-side tool, the app's hook points at a script the image
does not carry, and the 5 MP driver has no way to take one. BRINGUP
item 6 now says so, and the factory-slot session it asked for is not
needed.

## 2026-08-31: the mid-job gap question, answered from the chain

Item 1 asked whether the hardware button latch persists across a
kernel-run gap inside an armed job, with a stream keepalive as the fix if
it did not. The safing chain answers it (`docs/SAFETY.md` section 2): the
button latch is U23 latch 1, SET by lid-open OR the SoC lock (U32) and
RESET by the physical button only. The charge-pump watchdog feeds
HV_ENABLE, not the latch. A gap (a `G4` dwell, a hold, the end of a cycle
before the next) drops HV_ENABLE 454 ms after the last pump pulse and
leaves the latch as it was, while the driver keeps the SoC lock released
through the armed window; on the next run HV_ENABLE is back within ~3 ms,
~216 ms before the first step (the pads measurement of 2026-08-15). A
planner starve is not a gap: the stream pads the ring dark and the run
keeps playing. A keepalive would hold HV_ENABLE up while nothing is cut,
the one state the watchdog exists to prevent, so none is built.

The one thing never watched on the bench, the latch readback through a
lit-dwell-lit sequence, rides `laser.emission-witness` from now on: the
square carries a `G4 P2` between its second and third sides, the sampler
reads `cnc/button_latch` and `switches.hv_enable`, and the test checks
the latch clear in every armed sample, HV_ENABLE dropped across the
dwell and back with emission after it, and the operator confirms all
four sides. It runs with the attended set. Item 1 is retired; its
flow-band sentence is a fact and moved to the facts bank ("Cooling
operating point").

## 2026-08-31: the unattended set on the hot-deployed board

The first unattended run of the day (campaign c-20260831143855) stopped
on `cooling.aa-offset-calibrate`, the test's first campaign run ever,
queued right after `cooling.flow-verify`: the loop was still mixing
after the no-flow trial (19.6 C rise, the sensors 13.5 C apart), the
tool's fixed 6 s settle let the trend into its first edge (+44.7 and
+20.6 counts against 13 to 18 on the settled edges), and its spread
check refused the result (35.5 counts against a limit of 8). Fix: the
tool waits at the flow tools' stationary gate before its first edge
(forgectrl 7dbb5e1) and the test gives it 540 s (forgefirm eb8935c).

By operator decision the fix went onto the board without an image:
the pinned forgectrl built by bitbake (md5 7e1f28cc) and the suite
file installed over ssh on dev 20260831141210, forgectrl and forgetest
restarted. The test then passed alone (offset 15.0 counts, spread 6.0,
edges 12.2 to 18.2), and the unattended queue of 20 ran green behind it
in campaign c-20260831151846 (21 PASS, the 17 other unattended tests
inherited from the morning run, one ABORTED record from a page start
before the fix was in). The attended set is deferred by decision. The
campaign that authorizes a release runs on the image that carries every
fix, burned once.

## 2026-08-31: the coolant floor and the warm-up gate, on the bench

The low-temperature gates landed (forgectrl 5a12f55 and 9d0b757, pinned
in forgefirm f773866) and are bench-proven on the hot-deployed board,
image 20260831141210. `cool_temp_min` is the `coolant_min` gate: under
it the verdict is COLD with a hold, clearing 1 C above the floor
(`gate_floor_trip`, host-tested); a header floor (CMrn) can only raise
it. `cool_temp_start` is the `warm_up` gate: a run session opening under
it holds (WARMUP, fire blocked, phase `warm-up`) with the loop heater at
the flow duty and the fans idle, and releases into a normal run session
with the run fans up and the flow check requested on a fresh history.
The settings cross-check keeps floor under start under ceiling between
gates that are on; both fields are on the panel's Cooling card.

The first bench run (16:15Z) passed its checks but released 11 s after
the hold: with the pump on, the heater's slug reaches the upstream
sensor within seconds and inflated the instant reading past the gate,
while the bulk warms about half a degree a minute. The release now
judges a one-minute rolling minimum of the upstream reading (between
slugs it falls back to the bulk), the stall warning tracks the same
number, and the catalog test refuses a release under 60 s. The second
run (16:33Z) passed with the right physics: WARMUP held with the heater
at 40 percent (26214 of 65535) and the fans idle, released at 75 s on
the bulk minimum, heater off and run fans up after the release, COLD
tripped with the start gate off, both gates at zero reported off with
the run-start log line, and the settings restored.

`cooling.floor-and-warm-up` is the catalog case, proving both gates at
room temperature by moving them above the loop; `cool_gate_test` holds
the table rows and the floor hysteresis on the host. The warm-up
holds indefinitely under its gate by design: a loop that stops warming
(the heater plateaus 8 to 9 C over ambient) is named once and keeps
holding, so a shop colder than about 8 C under the gate needs the gate
lowered or the room warmed. The TEC item owns the chill side, with
`cool_temp_min` as its floor.

## 2026-08-31: the TEC drive, on the bench

TEC handling landed (forgectrl 7d8a580, pinned in forgefirm 6edd3e5) and
ran on the hot-deployed board. `thermal/tec_on` has no readback, so the
part's presence is the operator's word: `cool_tec_present` on the
Machine tab, default 0, and the engine never touches the line otherwise,
which also covers retrofits. When present, the engine drives it on a
hysteresis pair over the upstream reading (`cool_tec_on_c` 20 C,
`cool_tec_off_c` 18 C) and only while the fans run - the run, smoke-clear
and thermal phases, or a forced cooldown - because the cooler's heat
sink sits in their airflow. Off at idle, off in the warm-up hold, off
within a degree of the coolant floor; off is immediate, on waits a 30 s
dwell; the state is rewritten after a diagnostic hand-back. The settings
cross-check keeps off under on and above the floor.

A correction to the item as written: `CMet`/`CMdt` are readings, not
setpoints (attribute word 1, not header-legal; the coolant notes). The
factory's knob is `tec_temp_threshold` (`TCth`), on above it on the
filtered upstream reading; the non-Pro capture parks it at INT32_MAX and
no Pro capture is on hand, so the defaults are chosen, not inherited:
near the observed Pro loop point (18.1 to 18.4 C readings), above the
warm-up gate, above an ordinary room's dew point.

`cooling.tec-drive` passed on the deployed board (16:55Z): the
cross-checks refused off over on and off under the floor; declared
fitted with the pair moved under the loop, the line went to 1 one
second into an M8 session ("TEC on: coolant 26.4 C over 24.1 C,
airflow up") and back to 0 at the session's end ("TEC off: no
airflow"); declared not fitted, the same session left the line at 0;
the settings were blank before and are blank again.

Host proof: the two table rows in `cool_gate_test`. This machine is not
teardown-verified to carry the part, so the drive is proven at the GPIO;
the first Pro on the bench proves the cooling itself.

## 2026-08-31: the fire watch, armed in the factory's shape

The lid-IR fire watch is redesigned, armed by default, and bench-proven
on the hot-deployed board (forgectrl 77a6434, pinned in forgefirm
b506e67). The four channels sorted ascending are the quartiles, the
factory's own statistic (`IR?v` value tags exist for exactly those, and
the thresholds ride on quartiles, not channels); a sustained first or
second quartile over its alert threshold is the pause tier (verdict
FLAME, hold, fire blocked, released once the signal clears), over its
critical threshold the fail tier (motion stopped, latch locked, verdict
FIRE until the next session, smoke airflow held) - the same two classes
the factory's fault registry gives them (`lid_ir_*_quartile_alert` rings
the pause chain, `lid_ir_*_quartile_critical` is a hard FAILURE). The
watch runs through the run, smoke and thermal phases and gates both
controller modes through the one verdict.

The knobs are four gate rows with the factory's header values as
defaults: alert 275 / critical 688 on the first quartile, 374 / 1022 on
the second, quartiles three and four left off as the factory leaves
them at zero; 0 turns a tier off, and the cross-check keeps each alert
under its critical. The defaults sit far above a fully lit lid lamp
(161 to 177 counts plus 22 of drift), so no lamp change can trip them;
a candle-sized flame (+3 to +6 counts) stays under them too - the watch
catches a developed fire, which is what the factory's catches. The
relative `cool_fire_ir_delta` watch retires; per-job baseline and peak
logging stays. The header's own `IR??` values stay declared-ignored
(the standing envelope decision); reading what the cloud sets, per
machine, stays with the commissioning item. One interpretation is
recorded rather than recovered: that the quartile values are the sorted
instantaneous readings; the factory's exact computation is not decoded.

`cooling.fire-watch-tiers` passed on the deployed board (17:48Z), the
lid lamp as the flame stand-in (idle readings 51 to 56): a q1 alert
moved under the lamp held the session (verdict FLAME, `fire_watch`
alert, hold, fire blocked, the reason naming the quartiles) and did not
survive into a fresh session; a q1 critical latched FIRE with the laser
latch locked (`interlock_circuit` bit 3); all four thresholds at zero
read as the four flame gates off with the watch at `watch`; restored,
the watch read `armed` at OK. A first run failed only on its own log
check racing rsyslog by a second; the checks now poll the tail
(forgefirm 678a155).

## 2026-08-31: the shared-services polish, closed as a set

The three leftovers of the 2026-08-13 consolidation are done or decided
(forgectrl c930b41 and 4d3abd4, pinned in forgefirm 78c40ef).

**Diagnostics as an engine mode.** A tool owns the thermal hardware
between `cool_diag_take` and `cool_diag_release`: take succeeds only
from an idle engine, every write goes through guarded engine helpers
(one owner; the air-assist writes now ride the tracked path, so the
coolant-offset correction stays true through a diagnostic), the tick
publishes phase `diag` and touches nothing, and the release reasserts
the idle posture in one place. The polled suspend/resume dance and the
tool's own attribute writers are gone. The tools keep their airflow
profile exactly as the flow bands were characterized: exhaust and
intake at the run duty, the air assist untouched.

**HTTP surface caps.** The daemon starts MHD through ulfius'
with-options call with the flags ulfius computes reproduced verbatim,
plus a 64-connection ceiling and 16 per client address, so a flood is
bounded at the accept side. The first deploy taught the call's footgun:
the option array must carry ulfius' own connection plumbing
(`mhd_request_completed`, `ulfius_uri_logger`, both externalized for
this call), or the dispatcher answers every request with MHD's internal
error - the bench caught it inside a minute and the fix rode the next
deploy. The camera pipeline's setup children (media-ctl, v4l2-ctl) run
in their own process groups under a 10 s deadline and are killed past
it: a wedged V4L2 pipeline costs one bounded error, never a pinned
request thread. Moving the setup out of the callback entirely was
considered and not taken: the bound removes the hazard at a fraction
of the risk.

**Busy-state arbitration, declined.** With diagnostics folded into the
engine, the remaining idle/busy gates (`POST /settings`, `/mode`,
upload/apply) are independent 409 checks that fail closed and are
drilled; a single arbiter would rearrange them without closing a
reachable window, so it is not built.

The proof ran on the hot-deployed board through three passes of the
unattended queue plus a directed set. Along the way the queue itself
caught two rigid cross-checks the day's gates had introduced (a start
gate pinned under the ceiling refused the gate-off trip leg, a TEC off
threshold pinned above the floor refused the floor leg's raised floor);
both relations came out in favor of the bench patterns, with the
engine's runtime behavior as the enforcement (forgectrl de1f755 and
c77f040). The final queue ran 9 of 9 green (the always core,
floor-and-warm-up, tec-drive, critical-tier, fan-gate-trips), with
flow-verify, aa-offset-calibrate, gate-off and fans-quiet-after-motion
green earlier in the same campaign through the folded diagnostics, and
the directed set - panel-serves, snapshot, sensor-profile, h264-stream,
frame-health, lid-privacy - green on the capped server with the bounded
setup children.

## 2026-08-31: a failed head capture leaves the measure laser off

The physical-evidence negative, proven on the bench with the head
connected. The injection: a `v4l2-ctl` streamer held the shared
`ipu1_csi0` capture node busy, then the real `machine._head_image` path
ran with `HCil` arming the measure laser (verified lit at 1023 on the
real sysfs first). The busy pipeline refused the link change, the
capture raised, and the path's finally left `head/measure_laser` at 0.
forgectrl's camera engine recovered on its next request (snapshot 200,
engine running). The `STATE_FAULT`-recovery note is dropped from the
item by operator decision: the fault line has never tripped and the
recovery lever is documented in the UAPI; nothing is held open for it.
What remains of the item is the K-11 runtime case, one bench slot with
the head's bus flooded from userspace.

## 2026-08-31: the K-11 runtime case, a badly-answering present head

The last physical-evidence negative, proven on the bench with the head
connected and the fault injected in software. The injection is the
head's own reset register (`0xc9` <- `0x5a`, forced past the bound
driver under the adapter lock): the MCU reboots, and through the reboot
window its I2C reads NAK - a present head answering badly, the one
condition unreachable with the head unplugged. A tight poll of the four
witness attributes (`beam_detect_analog`, `accel_irq`, `hall_sensor`,
`beam_detect_digital`) over the window took 3596 samples; 8 returned an
errno (the K-11 propagation: `head_read_bit_ascii` and
`head_read_dword_ascii` return the negative i2c result rather than
formatting a value), and none returned a spoof-shaped positive (the old
`beam_detect_analog=65531` / `accel_irq=1`). The head was restored by a
`glowforge_head` rebind (re-probe rewrites the lambda/theta calibration)
and recovered fully: witnesses read clean, `info` `id=044c`,
`head_probe: done` in dmesg, and forgectrl's `/status` `head: true`.
This closes item 3; the drill's script was staged in `/tmp` and removed.

Note on injection: on the i.MX i2c adapter the kernel driver and
userspace i2c-dev serialize under the adapter lock, so flooding the bus
does not collide on the wire - the head reset is the reachable way to
make a present head answer badly.

## 2026-08-31: the debug-kernel drills, on the lock-debugging image

Both drills passed on the debug-kernel image
(forgefirm-image-dev-debug-glowforge.rootfs-20260831203241, kernel
`DEBUG_MUTEXES=y PROVE_LOCKING=y LOCKDEP=y DEBUG_ATOMIC_SLEEP=y
DEBUG_SPINLOCK=y`, verified in /proc/config.gz).

**Load/unload.** forgectrl stopped, `glowforge.ko` unloaded and reloaded
three times with a rail-settle between; each cycle clean, and the kernel
log over the three carried no lock splat.

**Forced `-EPROBE_DEFER`.** The cnc device unbound, its 40 V regulator
(`regulators:40v` on `reg-fixed-voltage`) unbound, then cnc re-bound:
the probe deferred (cnc did not bind while the regulator was gone) and
its devm unwind left the log free of splats; restoring the regulator
let the deferred probe complete and cnc bind again.

The machine ended healthy: cnc idle, the GRBL controller running with
motion verified, the head present, and no BUG/WARNING/lockdep splat
anywhere after the first drill mark. Both drills cycle the 40 V rail (5
`40V on` events across the session) and the drivers came back each time.
Three bench facts hardened the drill in the running (forgefirm 2319735):
the splat filter ignores the benign lockdep boot banner, the regulator
search reaches the `reg-fixed-voltage` driver, and the idle gate waits
out the transient `running` a forgectrl restart passes through. The
debug image and the drill scripts were staged in `/tmp` and removed.

## Superseded status notes

### Shared machine services — remaining polish, as listed 2026-08-13

**Shared machine services — remaining polish.** The consolidation
itself is complete and drilled (see "Where the project stands" and
`forgectrl/docs/SERVICES.md`); these are the deliberate leftovers,
none of them blocking:
- **Diagnostics as engine modes.** The Diagnostics flow tools still
  drive the thermal hardware themselves while the cooling engine
  suspends its writes and publishes fire-blocked. The check
  parameters and factory duties are already shared (`cool.h`, one
  definition for both), so what remains is folding the tools into
  the engine as modes and retiring the suspend/resume dance.
- **Rail policy** (SERVICES.md "Pulse-device ownership", the one
  `[contract]` item left there). `cnc/enable` / `cnc/disable`
  are not forgectrl-only writes yet: under the broker no client
  drops the rail any more, but the GRBL driver still writes
  `cnc/enable` at init and at homing resume — idempotent, since the
  rail is already up and settled, so this is tidiness rather than a
  bounce source. (An idle-rail-off policy is not part of this: the
  rail stays up while the machine is on, per the wedge model in the
  facts bank.)
- **Busy-state arbitration under one lock.** forgectrl's idle/busy
  gates (`POST /settings`, `/mode`, diagnostics start, upload/apply)
  each cross-check `machine_is_idle()` and `update_job_running()` at
  their own call sites. They fail closed and are drilled, but a
  single arbiter (one lock, one "who owns the machine right now"
  answer) would replace N targeted checks with one and close the
  remaining request-interleaving windows by construction.
- **HTTP surface caps.** The daemon relies on MHD's default connection
  ceiling (a 500-connection flood plateaued at 379 fds under the raised
  4096 `RLIMIT_NOFILE`, no crash, `cnc/state` readable throughout).
  An explicit `MHD_OPTION_CONNECTION_LIMIT` plus a per-IP cap is the
  right hardening, and the camera `ensure_engine` `popen()`s should
  move out of the HTTP callback so a slow media-ctl can never stall the
  request thread. Changing the MHD start flags touches the streaming
  model, so this waits for a bench slot of its own.
- **Cloud per-job fan profile.** The cloud client passes the pulse
  header's `AArd`/`EFrd`/`IFrd` duties to the engine as the per-job
  run profile. Homing headers are verified end to end (they carry
  the idle-quiet profile the factory uses — no fans during a hunt);
  a real print header's duties should be confirmed through the same
  round trip at the next cloud print.
- **`/cool/status` cosmetics.** The endpoint echoes the last
  reported `armed` flag even when that report is stale
  (`report_age_s` tells the truth), and a gfcloud homing session
  reports every motion as a job, so the engine cycles run → smoke →
  idle per motion. Both are silent and safe — the homing profile
  keeps the fans at idle duties — but motion actions reporting
  `idle` would be more honest.
- **Button edge detection.** The GRBL arm flow reads the button as
  an EV_SW level; edge detection belongs in that reader. It does
  not change where the button is read (per-mode direct evdev, for
  latency) — the switch map itself is contract-documented and
  shared.

### Camera service — closed 2026-08-03

**Camera service: DONE 2026-08-03, bench- and operator-verified**
(see "The camera service" section above; LightBurn streams it
directly). Remaining camera work: lens calibration / bed alignment,
the deferred 5.6 emulator homing-image smoke.

### Housekeeping entries

**Housekeeping**: ~~pick the controller's remote home~~ **DONE
2026-08-02** — the controller is now the canonical driver repo
`github.com/ScottW514/grblHAL-glowforge` (+ `ScottW514/core` fork;
the settings-write crash fix is upstream PR grblHAL/core#999; repoint
the submodule to upstream when it merges). ~~Yocto recipe for
grblHAL-glowforge~~ **DONE 2026-08-03** (`grblhal-glowforge` in
meta-forgefirm, boot autostart, reboot-verified). ~~Documentation
sweep (CLAUDE.md charter, README roadmap, INSTALL/BUILD/kas README)~~
**DONE 2026-08-13.** Remaining: kas flip + first GitHub release per
kas/README.md once ready to publish.

### kas/README.md status sections, as listed 2026-08-24

The two status sections of `kas/README.md` (the push/release checklist with its DONE markers, and the Scarthgap migration backlog), verbatim, before the README was cut back to build procedure and present-state facts; outstanding items are in BRINGUP, and the OV8856 reasoning lives in the 0011-0013 patch headers.

#### Push & release order (source-of-truth sequencing)

The build is only reproducible when recipe pins, layer branches, and the kas
config move in the right order. The sequence, with current status:

1. **Source repos pushed & pinned** — **DONE.** Every source repo
   (`kernel-module-glowforge`, `python3-gfhardware`, `Glowforge-Utilities`,
   `grblHAL-glowforge`, `forgectrl`) is on GitHub and its recipe pins an exact
   `SRCREV` — no `AUTOREV` anywhere. Whenever a source repo changes: push it,
   then bump the pin deliberately (BSP recipes in meta-openglow, ForgeFIRM
   components in meta-forgefirm) and re-verify with
   `bitbake -c fetch <recipe>`. A component's `SRCREV` (and the `PV` that
   moves with it) lives in `<recipe>-pin.inc` next to the recipe, nothing
   else goes in that file: the image manifest leaves `*-pin.inc` out of the
   layer content hash, so a pin bump changes the component's fingerprint
   and only that (`docs/ACCEPTANCE.md`) — a pin written into the recipe body
   still builds, but counts as a platform change and forces a full
   acceptance campaign.
2. **meta-openglow pushed** — **DONE.** The Scarthgap port lives on
   the **`scarthgap` branch** (Yocto layer convention; the Dunfell-era `master`
   is untouched). Development continues on the local sibling checkout; push /
   fast-forward `scarthgap` as work lands.
3. **forgefirm pushed** with the kas config and a `kas lock` lockfile pinning
   the upstream layers (poky, meta-openembedded, meta-freescale,
   meta-freescale-distro).
4. **At release time**:
   - flip `meta-openglow` in `forgefirm-glowforge.yml` from the local-sibling
     block to the pinned-remote block (commented FUTURE block in the file);
   - refresh `kas lock`, tag all repos, and prove self-containment by building
     from a **fresh clone**.
5. **GitHub release**: run `scripts/release.sh <version>` on the build
   host. It gates (version single-source, rootfs-vs-slot size,
   installer-embedded pubkey vs the signing key, factory-era fwup
   verification, and the **acceptance gate** - the committed
   `releases/v<version>/acceptance.json` from the bench campaign must
   authorize the built rootfs, `docs/ACCEPTANCE.md`), builds, packs and
   signs `forgefirm.fw`, stages the assets with `sha256sums.txt`, and
   prints the `gh release create` command. Assets and their exact names
   (the installer and the update manager download them verbatim):
   `forgefirm.fw`, `sha256sums.txt`,
   `forgefirm-image-glowforge.rootfs.wic.gz`, plus `acceptance.json` and
   `acceptance.md`. The release tag
   `v<version>` = `FORGEFIRM_RELEASE` = the rootfs `/etc/forgefirm-version`
   = the `.fw` meta-version; `release.sh` enforces the agreement.

All recipes fetch their pinned revision from GitHub, so an image build is
reproducible from the repos alone. For fast iteration on a source repo, bump
its pin per iteration, or add a **local, untracked** `externalsrc` bbappend
pointing at a working checkout — never commit one, or released images stop
matching the pins.

#### Scarthgap migration backlog

The kas scaffold + `LAYERSERIES_COMPAT` bumps let the layers be *selected* under
Scarthgap, but the legacy (Dunfell/Gatesgarth) layers won't build clean until:

1. ~~**Override-syntax migration**~~ — **DONE.** All `_append`/`_prepend`/
   `_remove`/`_${PN}` override syntax converted to the colon form across
   `meta-forgefirm`, `meta-openglow-core`, and `meta-glowforge-bsp` (22
   occurrences).
2. **Kernel forward-port (4.14 to linux-fslc 6.12.20).** The factory NXP vendor
   kernel (linux-imx 4.14.98) carried 7 out-of-tree changes; these are re-derived
   against mainline 6.12 in `meta-glowforge-bsp/recipes-kernel/linux/linux-fslc_%.bbappend`
   (the forward-port landing zone), **not** re-applied as the 4.14 patches.
   - **Foundation: DONE.** `linux-fslc` 6.12.20 builds for `glowforge` with a
     ported device tree (`glowforge.dts` + `openglow_common.dtsi` overlaid into
     `arch/arm/boot/dts/nxp/imx/`, registered via a Makefile patch) and deploys
     `zImage` + `glowforge.dtb`. Boot-core + mainline-bound peripherals only.
   - **Free wins: DONE.** bus-freq disable *dropped* (no mainline busfreq);
     `st,lis2hh12` x3 + `national,lm75b` + `ti,wl1805` + gpio keys/leds bind to
     mainline drivers. The 12 V control rail is a plain always-on fixed
     regulator with no userspace consumer node (nothing in the firmware
     switches it). The PIC SPI delay and the laser PWM prescaler are layer
     patches; see Motion polish below.
   - **Config: the board's kernel.** `glowforge.cfg` names this board's driver
     set and turns off what `imx_v6_v7_defconfig` adds for the other i.MX
     boards, and `conf/machine/glowforge.conf` names the modules and firmware
     the rootfs carries (the `kernel-modules` meta-package is not used). Every
     line of the fragment is expected to land in the built `.config` as
     written; a line that does not means a parent symbol is missing. The
     defconfig never names `PM`, the regulator core or ext4 (it had them by
     selection from suspend, the PMICs and ext3), so the fragment pins them.
     Bench record: BRINGUP item 21, CAMPAIGN-LOG 2026-08-24.
   - **Motion path: DONE and hardware-validated** (live-fed pulse stream,
     real gantry motion, laser fire). The whole chain forward-ports and
     compiles on 6.12:
       - **EPIT API**: `epit_api.c` in `arch/arm/mach-imx` (`CONFIG_MXC_EPIT_API`),
         in vmlinux, symbols exported; `&epit1/&epit2` in the DT.
       - **SDMA-expose**: re-created `dma-imx-sdma.h` + `0003-imx-sdma-*.patch`
         (un-static survivors, re-added the glowforge helpers, custom int-callback
         hook); expose symbols in `Module.symvers`.
       - **`glowforge.ko`**: ported across many 6.12 API changes
         (`tasklet_hrtimer`→soft hrtimer, `timer_setup`, LED-trigger API,
         `pwm_get`, `spi_delay`/`controller`, `filelock.h`, void `.remove`,
         1-arg i2c probe). Compiles + links, 0 undefined symbols; the recipe
         fetches the module by pinned `SRCREV`.
       - **DT**: `glowforge,cnc/thermal/pic/head` re-added with `pwms`/`pwm-names`
         phandles; `glowforge.dtb` compiles with all motion nodes.
     Motion polish, both carried as layer patches in the bbappend: the laser
     PWM prescaler (factory 1001) is patch 0009, `fsl,extra-prescale` on
     `pwm-imx27`, set to 13 on `&pwm2`; the cnc engine programs a ~1925 ns
     period so the SDMA script writes raw 7-bit power levels into PWMSAR, and
     the extra divider stretches the output to ~25 us, the ~40 kHz carrier the
     laser PSU expects (mainline `pwm-imx27` alone would run the laser PWM at
     ~520 kHz, and asking for 25 us directly caps the script's writes at ~8 %
     duty). The PIC inter-word SPI delay (factory 1005) is patch 0004: mainline
     `spi-imx.c` has no `PERIODREG` support, so the patch programs the ECSPI
     sample period from `spi_transfer.delay` (which pic.c sets) and forces
     fixed per-word bursts while a delay is requested, so the wait-states land
     between words; without it the PIC answers 0x0000 to the ID read. Both are
     in every image and hardware-validated (the PIC reads and the laser fires
     on them). The factory `glowforge,imx-pwm-audio` (buzzer) driver is not
     part of ForgeFIRM.
   - **Camera — DONE and hardware-validated.** The factory
     `ov5648_mipi.c` (NXP's removed `v4l2_int_device`/`mxc_v4l2_capture`) is
     replaced by the mainline `ovti,ov5648` subdev + imx6 `imx-media` (IPU CSI)
     + `imx6-mipi-csi2` receiver. The factory CAM_SEL MIPI switch is modeled
     with the mainline `video-mux` (gpio-mux on `gpio7 10`): both sensors →
     video-mux → `mipi_csi` → IPU CSI. Sensor `xvclk` is the board's 24 MHz
     fixed oscillator (matching the factory DTB); avdd/dovdd/dvdd rails are in
     the DT. Both cameras stream live through forgectrl (MJPEG at 15 fps with
     VPU JPEG encode, full-resolution snapshots, mux arbitration).
     **HD units (8 MP OV8856) — code complete, UNTESTED.** The DT lists both
     `ovti,ov5648` (5 MP) and `ovti,ov8856` (8 MP) at 0x36 so one image covers
     both, and the driver matching the chip ID wins. Everything the OV8856
     needs is in the build: patch 0011 gives it the `get_mbus_config` the
     IPU-CSI hard-fails without (the same gap 0006 closes for ov5648), patch
     0012 retunes both PLL multipliers for the board's 24 MHz xvclk (mainline's
     tables are written for 19.2 MHz, which would run the link 25 % above the
     frequency the driver publishes), patch 0013 adds the 2-lane RAW8 modes
     (below), the endpoint's `link-frequencies` list carries the driver's whole
     2-lane menu (it rejects the endpoint outright if any entry is missing —
     the old list omitted 720 MHz, so probe would have failed), and forgectrl
     and gfhardware pick geometry and the sensor's control set from whichever
     driver bound.

     The capture mode is the **full 3264×2448**, reached in RAW8. The
     sensor's stock RAW10 full-resolution 2-lane mode asks for 1.44 Gbps/lane
     and the i.MX6 CSI-2 D-PHY stops at 1 Gbps (`hsfreq_map` in
     `imx6-mipi-csi2.c` ends at 1000 Mbps and `max_mbps_to_hsfreqrange_sel()`
     returns `-EINVAL` above it), so `imx6-mipi-csi2` refuses to program it —
     but 8-bit samples carry the same frame at half the rate, which puts it on
     the 360 MHz link the binned modes already use, at 180 Mpx/s and 15 fps.
     Patch 0013 builds those modes from mainline's own 4-lane 3264×2448 and
     1632×1224 register lists plus a per-mode delta list: `0x3018` for two
     lanes, `0x3031` for 8-bit readout, and double the HTS because half the
     lanes carry half a line in the same time. It also makes the sample depth a
     mode property, so pixel rate, blanking and exposure ranges follow the mode
     instead of a fixed 10. Side effect worth having: the OV8856 path becomes
     byte-identical in shape to the OV5648's (8-bit BGGR, one byte per sample),
     and 3264 is a multiple of 32 so the NEON superpixel converter applies,
     which 1640 did not allow.

     The values are the factory firmware's: its own OV8856 driver is RAW8-only
     and ships exactly these two resolutions over two lanes with the same
     `0x3018`/`0x3031` and the same HTS/VTS pairs. It reaches them through a
     different PLL divider chain (`0x0302=0x1e`, `0x0303=0x03`, `0x030f=0x07`,
     `0x0312=0x05`, `0x4837=0x58`) that halves the link again to 180 MHz and
     the internal SCLK with it — a self-consistent alternative, recorded in the
     patch header as the configuration to fall back to if the D-PHY will not
     lock at 720 Mbps/lane on real hardware.

     Open, and only answerable on an 8 MP machine: whether it streams at all at
     720 Mbps/lane, and exposure/gain/white-balance commissioning — the OV8856
     driver publishes no red/blue balance controls, so white balance is
     uncorrected.
3. **u-boot** — **DONE.** The `glowforge` u-boot is
   a standalone `u-boot_2020.01.bb` (Scarthgap's poky has no u-boot 2020.01
   base recipe to extend). It reuses poky's
   `u-boot-common.inc`/`u-boot.inc`, pins `SRCREV` to the upstream **v2020.01**
   tag with the matching `Licenses/README` md5, and overlays the glowforge board
   support + arch-Kconfig patch. **Builds clean under Scarthgap (GCC 13, no
   source fixes) and deploys `u-boot-glowforge.imx`.** Remaining: move
   `fw_printenv`/`fw_setenv` from `u-boot-fw-utils` to `libubootenv`
   (`PREFERRED_PROVIDER_u-boot-fw-utils` in `glowforge.inc`) when the rootfs needs
   them.
4. **Device tree — DONE.** The `glowforge` `.dts` is validated against the
   linux-fslc 6.12 bindings and against the running board (motion, safety
   readbacks, cameras, sensors all bind and work).
5. **Real-time strategy — decided.** The kernel runs
   `CONFIG_PREEMPT=y` (factory behavior; `imx_v6_v7_defconfig` alone gives only
   `PREEMPT_VOLUNTARY`). **PREEMPT_RT is not selectable on arm32 6.12** (no
   `ARCH_SUPPORTS_RT`) and is **not needed for the pulse feeder**. The
   argument is about queue depth, not ring size: the ring drains at 1 byte
   per EPIT tick (≤200 KB/s even at the 200 kHz ceiling), so the live
   feeder's bounded queue depth of ~150 ms — a few KB in flight — already
   rides out worst-case scheduling latency with orders of magnitude to spare
   (measured: 0.2 ms worst write latency under full CPU + I/O load; the
   underrun bench ran 100 kHz for 120 s with zero underruns). The ring
   itself is 32 MiB (the `ring_mb` module parameter, backed by the 32 MiB
   reserved pool, matching the factory ring): ~168 s of stream at 200 kHz,
   ~56 min at the 10 kHz cloud-mode tick — a capacity that matters for the whole-job preload of
   cloud mode, not for latency. Bounded queue depth + `SCHED_FIFO` for the
   feeder is the design; revisit RT only if the underrun bench ever
   contradicts this arithmetic.

6. **gfui-client → forgectrl — DONE.** The stock `gfui-client` is excluded
   from `forgefirm-image` (`IMAGE_INSTALL:remove = "gfui-client"` in
   `meta-forgefirm/recipes-forgefirm/images/forgefirm-image.bb`). Its slot is
   filled by `forgectrl` (github.com/ScottW514/forgectrl — the machine-services
   daemon: web control panel, cameras, telemetry, settings, diagnostics,
   cooling engine, updates, and controller-mode supervision) plus the two
   controllers it supervises, `grblhal-glowforge` (Grbl over TCP:23) and
   `gfcloud` (the optional Glowforge web-service client, off unless selected).

---

**Image status:** `forgefirm-image` **builds end-to-end** on the forward-ported
stack and deploys `forgefirm-image-glowforge.rootfs.wic.gz` (+ `zImage`,
`glowforge.dtb`, `u-boot-glowforge.imx`) under `build/tmp/deploy/images/glowforge/`.
Build-time prerequisites baked into the config: `ACCEPT_FSL_EULA = "1"` (NXP
firmware-imx — the image also installs `firmware-imx-lic` so the EULA text
ships beside the blobs) and the kernel default in `glowforge.conf`. Every
`LICENSE` string in the layers (`meta-forgefirm`, `meta-glowforge-bsp`,
`meta-openglow-core`) is SPDX, and the recipes for third-party components
that carry more than one license (`wlconf`, `python3-gfhardware`) declare
each of them with a checksum on its license text. The stack
is hardware-validated end to end: motion timing, the laser and safety chain,
the camera pipeline, both controller modes, and the A/B install path.

### Release acceptance follow-through (item 12), as listed 2026-08-24

Closed by campaign `c-20260824231028-b7ca` (45 of 45 on dev 20260824230512, the bench actuator proven in it). The leftovers (bench tools from the page, two unported cooling tests, the armed-kill core question, the websocket.py split) stay in BRINGUP item 12; the first release is item 13.
12. **Release acceptance follow-through.** The full campaign on the first
    image built with the `<recipe>-pin.inc` layout is done: dev image
    `20260821181036`, 42 of 42, release authorized (the export is on the
    board at `/data/forgetest/export/`). From here a component pin bump
    re-requires only the tests covering that component. That image also
    carries the 32 MiB pulse ring (DT pool plus the module default):
    `image.health` reads the pool and `ring_mb` back, and
    `cloud.oversize-stream` fed a print longer than the ring from the live
    service. Still owed: exercising the ported bench tools from the page
    (they are registered and unit-tested, not yet driven from the page), and
    the first release, which commits `releases/v<version>/acceptance.json`.
    Cutting the operator's part of a campaign: the forgetest-only step
    (the operator channel, the merged mode-switch, the sensor witnesses,
    the steps pane, the journal, per-test implementation hashing) is done
    and bench-validated (CAMPAIGN-LOG 2026-08-22). The offline cloud
    service for the machine-behavior tests (gfutilities `OfflineService`,
    `gfcloud --offline`, `forgetest/puls.py`, four tests re-ported) is done
    and bench-validated on dev image `20260822232347` (43 of 43, the four
    offline tests in 5.5 minutes, nothing on the bed). The service-protocol
    test on the emulator (`cloud.service-protocol`, `gfcloud --emulate`, the
    `python3-gfutilities-emulator` fixtures on the dev image; only a Print
    in the app to drive) is done and bench-validated on dev image
    `20260823161333` (44 of 44, CAMPAIGN-LOG 2026-08-23). The service's
    connect-time hunt is paid only where it is the subject: every cloud
    client the tool starts for anything else comes up under
    `/run/gfcloud-nohunt` (`gfcloud --no-hunt`, the first settings report in
    the reconnect form), while the two homing tests and the one real print
    get theirs, the print by never reusing a session that has not hunted
    the machine itself (the contract's cloud split in ACCEPTANCE.md). The
    coverage maps follow the split (a sign-in change re-requires the
    protocol test and the print, a feeder change the offline tests and the
    print, a doc edit nothing), and the bench actuator `forgefixture`
    (`fixture/`: ESP32-S3, three relays, the `ctx.act` seam, an operator
    test it covers routed into the unattended queue) is written and
    host-proven (the firmware builds in the pinned ESP-IDF container, the
    policy and the tool's client have host tests) and **owed its bench
    proof**: the harness at the machine's connectors, then a campaign with
    it up. Catalog
    gaps left from the tool's own plan: `cooling.confirm-escalate` and
    `cooling.fire-gate-blocks-arm` are not ported (both need the pump switched
    by hand mid-run, so they are bench-tab material first), and whether
    `laser.armed-kill` belongs in the always-required core rather than its
    domain is still an open call (the core carries the emission witness).
    Tools that genuinely need a second host (LAN flood, remote auth probes)
    stay host-side by design, and the registry marks them so.

### Video pipeline offload: bench validation (item 20), closed 2026-08-24

Closed: `camera.h264-stream`, `camera.frame-health` and `camera.snapshot` passed in the campaign on the GPU path, and motion ran under the stream. The strip switches and diagnostics named at the end are documented in `forgectrl/docs/SERVICES.md`; 8 MP first light stays with item 6.
20. **Video pipeline offload - bench validation.** First hardware session
    done (2026-08-24, dev image 20260824122014, drill binaries; fixes in
    forgectrl 6614833). Proven: Mesa etnaviv fits the release slot
    (~5 MiB margin); surfaceless EGL and dmabuf import both directions;
    `GL_MAX_TEXTURE_SIZE` 8192 (no tiling even at 8 MP); the full path
    GPU render → IPU stride-fix crop (`src/ipu_copy.c`, the render
    engine's 64-byte rows and the CODA's round_up(width,16) stride never
    meet, so the IPU crops between them, 14 ms, no CPU touch) → VPU
    encode, `convert: "gpu"`, image correct to within 2 counts of the
    scalar demosaic; `/cam/h264` serving valid fragmented MP4 on
    hardware (avc1.424020, ~480 kbit/s on a static bed). Second session
    (2026-08-24 evening, forgectrl 2d59d78): the render decomposed to
    41 ms luma + 49 ms per chroma pass; the chroma passes now
    point-sample instead of box-average (16x fewer per-fragment fetch
    chains), taking the render to 64 ms - **~9 fps at ~7 % CPU**
    against the NEON path's 15 fps at 41 % - with luma measured
    bit-clean against the CPU path (which also retired the bottom-row
    artifact of the first session). The **CSI hardware frame skip is
    live-proven** with the GPU path (`FORGECTRL_STREAM_FPS=7` →
    `hw_fps_skip: true`, steady ~7 fps, daemon sampling 0.0 % in top):
    that is the recommended low-CPU configuration today. Third session
    (2026-08-24 night, forgectrl deee6a1): the render and the encode now
    overlap - a frame renders behind an EGL fence while the previous
    frame is IPU-cropped, encoded and published (two IPU source buffers;
    the rendering frame's capture buffer held until its fence clears) -
    measured **13.8 fps single-viewer at ~14 % CPU** (fence stall
    7-9 ms of the 64 ms render, so it is fully hidden), **9.8 fps with
    MJPEG and H.264 served at once** (stall 0), luma still bit-clean.
    Fourth session (2026-08-24 night, forgectrl d97cb35): MSE playback
    in Chrome found the fragments carrying raw boot-clock timestamps
    and the panel's live-edge seek overshooting the one-frame buffered
    window; each viewer's fragments are now zero-based, the seek clamps
    into the newest range, and a paused element is kicked back into
    play. Verified live: the panel's H.264 view plays at 1296x972 with
    no MJPEG fallback, and with that view plus an MJPEG viewer running,
    a jog out and back completed at its commanded feed with the step
    ring's underrun counter unmoved and the planner buffer full - the
    GPU stream path and motion coexist. Bench validation of the video
    offload is complete. The acceptance campaign is not tracked here:
    it rides the release flow as always (item 12; Mesa joining the
    image makes the next one full, and `camera.h264-stream` rides in
    it). The one piece of video work that needs hardware this bench
    does not have is 8 MP first light (item 6). Switches to strip a suspect layer: `FORGECTRL_NO_GPU`,
    `FORGECTRL_NO_H264`, `FORGECTRL_NO_HW_SKIP`, plus the existing
    `FORGECTRL_NO_VPU` / `FORGECTRL_NO_NEON` /
    `FORGECTRL_NO_CACHED_BUFS`; diagnostics under `FORGECTRL_GPU_CHECK`
    (tight stats cadence, render-versus-copy split, luma/chroma
    compare) plus `FORGECTRL_GPU_PASSES` (limit the draws) and the
    frame-wait column in the stream stats.

### Kernel trim: bench validation (item 21), closed 2026-08-24

Closed: the campaign it owed ran green on dev 20260824230512, after the two faults it uncovered on the way (the SDMA clocks and the truncated peer address, both above) were fixed. The kernel's present shape is in the facts bank ("Reserved memory", "SDMA pulse engine", "The SoC guards itself"); the item-16 drill stays with item 16; the trims not taken are the new item 20.
21. **Kernel trim: bench validation.** The kernel is built for this board
    alone: `glowforge.cfg` names the driver set and turns off what the
    multi-board defconfig adds, and `glowforge.conf` names the modules and
    firmware the rootfs carries. Built into image 20260824164619, not yet
    flashed: zImage 4.8 MB (was 9.1 MB), 31 kernel-module packages (was 254),
    no SDMA, EPDC or Quad-VPU firmware, ARMv7-only code, no virtual console
    (`USE_VT = "0"`), and no `dmas` on ecspi2, so the pulse ring is the SDMA's
    only client. New on the same image: pstore/ramoops in the 1 MiB the
    bootloader holds back at the top of DRAM (`/sys/fs/pstore` mounts from
    fstab), the hung-task and soft-lockup detectors behind the panic
    notifier, `PANIC_TIMEOUT=10` in Kconfig, and `evbug` gone from the kernel
    log. Bench-validated on that image (CAMPAIGN-LOG 2026-08-24): every node
    binds and nothing defers, the panic sysctls read as configured,
    `/sys/fs/pstore` mounts with ramoops registered, `/dev/dri/renderD128`
    is present and both cameras stream through the GPU demosaic, Wi-Fi
    associates with `regulatory.db` loaded, the switches sit on `event0`,
    31 modules load and no DMA channel is held by anyone (which, as the
    campaign later showed, was the problem: see below). Two cosmetic dmesg
    lines came with it: spi-imx reports the absent DMA channel at ERR level
    and runs PIO, and `consoleblank=0` (uEnv) is an unknown parameter without
    a virtual console. The crash record is proven: a forced `sysrq-c`
    panicked, rebooted on the timeout, and the next boot read back
    `dmesg-ramoops-0` and `console-ramoops-0` with no ECC errors (the
    first boot's header-init lines did not repeat). The `spi_device_id`
    table for `glowforge,pic` is pinned into the next build (its boot
    warning goes with it). Still owed: a GRBL job on the image, then the
    acceptance campaign (platform change).

    A second round rides the next image, host-proven and unflashed: the
    kernel is UP (`SMP` off) with performance as its only cpufreq governor;
    spi-imx no longer logs the absent DMA channel (patch 0014);
    `consoleblank=0` is gone from the boot
    arguments; only `wl18xx-fw-4.bin` and `wl18xx-conf.bin` ship for the
    WL1805 (the current factory image's set); IPv6 is on end to end
    (distro feature, `udhcpc6` from the `wlan0 inet6` stanza, forgectrl,
    grblHAL's TCP:23 and forgetest listening dual-stack); the log export
    carries `/sys/fs/pstore`; and the release rootfs drops nano/libmagic,
    the udev hardware database and urllib3's pyOpenSSL chain (~22 MB).
    Bench-validated on dev 20260824200726 (CAMPAIGN-LOG 2026-08-24, second
    round): the three lines are gone, the kernel is UP at 996 MHz on the
    performance governor, Wi-Fi is up on the two-file firmware set, every
    port answers over IPv6 on the board's ULA, the export runs. Dev
    20260824201945 adds patch 0015 (wlcore asks for its optional NVS the
    quiet way) and the last stray dmesg line is gone. The missing GUA is a
    network matter, diagnosed (CAMPAIGN-LOG 2026-08-24, "the second DHCPv6
    responder"): the firewall advertises an address, but an access point on
    the bench VLAN still ran its own RA and DHCPv6 server, its Advertise
    arrived first with nothing to give, and busybox's `udhcpc6` stays with
    the first Advertise it sees. With that access point's RA and DHCPv6
    disabled (as on the other two) the board took the firewall's lease, and
    every service answered on the global address from another VLAN: IPv6 is
    on end to end.

    The campaign on dev 20260824201945 then found what every check above had
    missed: the machine cannot move on any image since the trim. The SDMA
    engine's `ipg`/`ahb` clocks are enabled only while a dmaengine client
    holds a channel; imx-sdma leaves them off after probe, and glowforge.ko
    takes its channel through the SDMA API patch without touching them. The
    ecspi2 `dmas` had been the only clock holder since the first image, by
    accident. With the block gated every channel-0 transfer completes at
    once and moves nothing, so the ring reads back the bounce page: the
    supervisor's probe logs `cannot start the probe run` at every spawn
    (`cnc/run` returns -ENODATA because the head sync reads the tail it just
    published), `cnc/free` exceeds the ring, and `/status` reports a position
    that never moved. `image.health` failed on the free check after the
    150 s settle timeout, which is how it surfaced (CAMPAIGN-LOG 2026-08-24,
    "the SDMA clocks, held by nobody"). The fix, host-proven and unbuilt:
    `sdma_get_channel()` enables the clocks and `sdma_put_channel()`
    releases them (patch 0003, the API header), the module calls put on
    remove and on the probe unwind, the empty-ring run request logs at ERR
    level again (it was the only kernel-log trace of the fault), and
    `image.health` asserts the SDMA clock enable count directly. The ecspi2
    `dmas` stay deleted. Bench-proven on dev 20260824215906: the clock count
    reads 1, the probe reports MOTION OK, `cnc/free` reads the ring less its
    gap, and the campaign ran every kernel, forgectrl, logs and motion test
    green.

    That campaign then stopped on `cooling.fans-quiet-after-motion`: M8
    raised no fan duty because forgectrl had accepted no cooling report
    from the controller at all (`report_age_s` -1). The dual-stack listener
    of the second round reports every peer as a `sockaddr_in6`, and ulfius
    2.7.15 copies the peer into `client_address` as `sizeof(struct
    sockaddr)`, 16 bytes; the mapped-loopback bytes the check reads lie
    beyond the copy, so `POST /cool/state` from 127.0.0.1 got `403 loopback
    only` (fail-safe: the engine treats silence as a stand-down, so nothing
    fired, but no run profile and no armed window either). The fix: the
    image patches ulfius to allocate a `sockaddr_storage` and copy the
    family's length (`meta-forgefirm/recipes-extended/ulfius`), the peer
    check lives in `src/peer.c` with a host unit test (`auth_peer_test`,
    including the truncated-copy case, which fails closed), and
    `forgectrl.auth` asserts that the loopback peer is accepted as well as
    that a LAN peer is refused. Bench-proven on dev 20260824230512: the
    loopback report answers 200 and the LAN peer 403, the controller's
    reports land (`report_age_s` 0.1), and campaign `c-20260824231028-b7ca`
    ran the 36 unattended tests green in 13 minutes, `fans-quiet` among
    them (CAMPAIGN-LOG 2026-08-24, "the listener heard, and the campaign
    ran through"). Left: the item-16 drill and the nine attended tests
    (four laser live, five cloud).

### The acceptance burden plan (tree-root working file), merged 2026-08-24

The working file `ACCEPTANCE_BURDEN_PLAN.md`, verbatim, at the point every step had landed: step 1 (the operator channel and the witnesses), 2a (the offline service), 2b (the protocol test on the emulator), 3 (the bench actuator) and 4 (finer covers) bench-validated, the operator's decisions taken (the fixture built; arm presses human by default with the opt-in; the mode-switch merge done; the protocol test a catalog test). The contract lives in `ACCEPTANCE.md`; the one thing it left open, the `websocket.py` split, is in BRINGUP item 12. The file is deleted.
#### Acceptance campaign: cutting the operator's burden

**Working file, not a repo document.** The tree root is not a git repo. When this
closes, its conclusions merge into `forgefirm/docs/ACCEPTANCE.md` (the catalog
kinds, the action seam, the fixture contract, the cloud split),
`forgefirm/docs/BRINGUP.md` (the open work it leaves behind, and the fixture in
the hardware facts bank if one is built), `forgectrl/docs/SERVICES.md` (the
offline cloud service, if it becomes a setting), and the dated record goes to
`forgefirm/docs/CAMPAIGN-LOG.md`. Then this file is deleted. Same
merge-and-remove convention as the audit and acceptance plans.

**Status: step 1 DONE and bench-validated 2026-08-22** (forgefirm de324cc,
9139e92, 296fd68, 2547a8e, 60db956; dev image `20260822204234`; campaign
`c-20260822220701-a1c0`, 43 of 43 from nothing, the 16 attended tests in 19
minutes of test time against the 111-minute estimate the plan started from).
**Step 2a DONE and bench-validated 2026-08-22** (gfutilities 768730e,
gfhardware a3ca36f, forgefirm 9cc2e4e/628f2f7/0cb9044, meta-openglow a52e68c;
dev image `20260822232347`; campaign `c-20260822233344-08de`, 43 of 43, the
four offline tests in 5.5 minutes with nothing on the bed). **Step 2b
CODE-COMPLETE, BENCH OWED** (gfhardware 12ad3b1 `gfcloud --emulate`,
meta-openglow a4e3abf `python3-gfutilities-emulator`, forgefirm 1c8197f/4c9dcca
`cloud.service-protocol`; dev image `20260823002125` BUILT, NOT FLASHED; it
carries a layer change, so its first campaign is a full one: 28 unattended, 17
attended). 2c (one real print stays) is `cloud.pause-resume` by construction.
Steps 3 to 4 not started.

**Step 2b DONE and bench-validated 2026-08-23** (CAMPAIGN-LOG 2026-08-23,
forgefirm e5fa444): the emulator's hunt bypass found by the dry-check and
fixed (gfhardware b7e8035); the operator's no-hunt change (gfhardware
351a623, forgefirm 969bac6: `--no-hunt`/`/run/gfcloud-nohunt`, markers
consumed by the client first thing, `session_hunted` guarding the real
print; policy in ACCEPTANCE.md); the POST /mode timeout found on the bench
and fixed both sides (gfhardware 537d0db: the emulator reports idle to the
cooling engine; forgefirm ab0a515: 120 s for the supervisor's levers).
Final: dev `20260823161333`, campaign `c-20260823161923-0dd7`, **44 of 44,
release authorized**, 29 inherited, 868 s attended. `cloud.service-protocol`
68 s with one Print in the app; the real client back in 14 s under NO-HUNT.
No release cut. The cloud split (L2) is complete.

**Step 4 (L5, finer covers) DONE 2026-08-23, host-proven, bench re-baseline
owed** (forgefirm 53e4fa2 + 335c6de, pushed, forgetest 241/241, lint clean):
the cloud maps by what each test proves (`_SERVICE_LAYER`, `_MACHINE_RUN`,
`_HOMING_PATH`, the print `_CLOUD_ALL`), two hollow entries of the protocol
test found and fixed (globs anchor at the repository root), the lint now
fails any entry that selects nothing, and non-behavioral paths (docs, CI,
unit tests, licenses: `NON_BEHAVIORAL` in manifest.py) are outside every
fingerprint. Measured on the tree manifest: a sign-in change re-requires
proto + print; a feeder change the 4 offline tests + print; a camera change
mode-switch + print; a doc edit nothing. Cost: 28 fingerprints move once
(the 7 cloud tests by their maps, 21 laser/motion/kernel tests because
their `**` used to hash grblHAL/kernel docs and tests): 16 attended + 12
unattended on the next image. Not done: splitting gfutilities'
websocket.py (transport vs. transfer helpers), which would take
websocket-transport changes off the offline tests; a gfutilities refactor,
not a map.

**Step 3 (L1, the fixture) CODE-COMPLETE 2026-08-23, bench owed**
(forgefirm 8ee4ee3 firmware + b54b94e tool side, pushed, both CI green):
`fixture/` = ESP-IDF v5.5 project for the ESP32-S3 DevKitC-1 (GPIO 4 lid,
5 interlock, 6 button, 7 enable jumper to GND; active-high 3.3 V opto
relay modules; HTTP :80, `X-Fixture-Key`; mDNS `forgefixture.local`;
`fixture.env` baked at build; `fixture.sh env|build|flash|monitor|test`;
builds in `espressif/idf:v5.5.5` via podman from Git Bash, 837 KB).
forgetest: `fixture.py` (config `/data/forgetest/fixture.json`, own mDNS
resolver, client), `hands=` on tests, routing of covered operator tests
into the unattended queue, Ready pass-through, prompt guard, release after
every run, `arm_press` opt-in (`Context.arm_press`, used by the laser
suite). Operator decisions: ESP-IDF native, .env baked, jumper, name.
Settled: the interlock loop is J8 (J6 is the speaker), the 3.3 V rail has
the headroom. 2026-08-23 18:50Z: flashed (COM15), on the air as
forgefixture at 172.16.1.135, found by forgetest (dev 20260823184050) over
mDNS by itself, every API path verified from the board; lid and interlock
relays switch; the button reports disabled until the jumper is in. Left:
the harness, then the campaign.

**Step 3 BENCH-PROVEN 2026-08-24 (harness wired by the operator):** every
channel proven through forgectrl's switch readings (lid 50 ms, interlock
40/200 ms, button pulse seen); forgetest routed 8 operator tests into the
unattended queue. The first fixture campaign (`c-20260824171919-e4f0`)
failed `motion.button-hold-resume` on a harness defect: the second press
was asked while the first 200 ms pulse was still on, the fixture answered
409, the runner fell back to an operator who was not there, and the
post-baseline could not jog a controller left in Hold. Fixed in forgetest
(press spacing in `fixture.py`, unattended fixture refusal = ERROR in
`runner.py`, soft reset out of Hold/Door before the return jog in
`baseline.py`; 5 host tests; ACCEPTANCE.md + fixture/README.md),
hot-installed by the operator, rerun `c-20260824174545-0bdc`: 25/25,
36 unattended satisfied (11 inherited), every fixture action `by:
fixture`, 0.04 to 0.34 s each; the baseline's hold reset proven by a dry
drill the same day (a move held at 7.988 mm reset and jogged back).
Committed and pushed as forgefirm 00ded74. Left:
the 9 attended tests (4 laser live, 5 cloud), the CAMPAIGN-LOG entry,
then the merge of this file.

**COLD PICKUP (next session):** 1. the operator builds the harness and
flashes the DevKit (`fixture/README.md`); settle the interlock connector
first and fix whichever doc is wrong; 2. `/data/forgetest/fixture.json`
on the bench (key from fixture.env, mode 0600); the forgetest change
reaches the bench with the next image (or a hot-install); 3. a campaign
with the fixture up: the 28-test re-baseline of step 4 plus the fixture's
own proof (operator tests in the unattended queue, `by: fixture` in the
evidence, the release leftover); 4. CAMPAIGN-LOG entry, then merge this
file into ACCEPTANCE.md / BRINGUP / SERVICES.md per the header and delete
it. Written 2026-08-22 from a read of the 45-test
catalog, the runner, the page, and the cloud client's seams; the numbers in
§1 and §4 are the catalog's own `est_min` and `steps` declarations, not a
stopwatch; the campaign record is `CAMPAIGN-LOG.md` 2026-08-22.

**What step 1 settled, beyond the plan:**

- **Per-test implementation hashing** (not in the original plan): the
  fingerprint's implementation half was the whole suite file, so a two-line
  witness fix re-required every test of its module. It is now the test's
  function plus the module's shared code. One-time cost paid (every
  fingerprint moved; the 43/43 campaign above).
- **`cloud.pause-cancel-paths` became `cloud.paused-lid-cancel`**: the app
  cancel lives in `cloud.oversize-stream` only, judged in full there. Catalog
  stays 43 (the protocol test of L2c is still to come).
- **Witness facts from the bench:** the head accelerometer lands 2 to 3 sysfs
  samples per one-second jog leg and sees ramps, not travel (judge the
  sequence, never a single leg); the button LEDs fade under the smooth
  trigger (read `target`, the commanded level); the beam detector read delta
  500 and 479 against the 300 threshold at S400 (digital flag seen both
  times); the lid camera's half-res frame is ~2x the bytes lit vs lamp-off.
- **Every action was the operator's** (74 recorded `by: operator`); the
  fixture seam (`runner.fixture`, `covers()`/`act()`) is exercised only by the
  host test until step 3.
- **Step 2a decisions:** the offline lever is a volatile marker
  (`/run/gfcloud-offline`, one start, never a persisted setting: a reboot can
  never come up offline) plus `gfcloud --offline`; no forgectrl change. The
  service is `OfflineService(GFUIService)` in gfutilities (same dispatch
  loop; a UNIX-socket listener stands in for the WsClient; a requests
  Session with a `file://` adapter and an upload sink stands in for the web
  session). Jobs are synthesized on the board from a captured factory print
  header (MCsn 0, so no serial lock) over a laser-free square; a job longer
  than the ring is the whole file gzip-compressed (the client's gzip ISIZE
  is its progress denominator). Lesson: the marker must stay until the
  `OFFLINE service` line is logged (Python import time on the i.MX6 runs
  seconds past the supervisor's "running").

---

##### 0. The problem in one paragraph

A full campaign is 45 tests: 25 `auto` (48 min), 12 `operator` (46 min), 8
`live` (65 min). The attended block is 111 of 159 catalog minutes, and it asks a
human for roughly eighty discrete things: open the lid, press the button, pull
the interlock, set up a job in the Glowforge app, place scrap, look at the
scrap, look at the app, answer a popup before the head finishes its move. The
inheritance model spares most of this on a quiet day, but during development
of the cloud client every change invalidates all eight cloud tests, which means
six real prints and a full-bed raster designed in the app. The goal here is to
take the hands out of the campaign wherever a sensor or a relay can stand in
for them, without moving a single safety line.

##### 1. Where the burden actually is

Counting what the 20 attended tests ask of a person in one full campaign:

| Action | Count | Where |
|---|---|---|
| Lid open or close | ~23 | 9 tests; every one a software-visible EV_SW edge the test already verifies |
| Button press, pause/resume | ~10 | 6 tests |
| Button press, arm consent | 11 | the 8 live tests |
| Interlock unplug/restore | 4 | 2 tests |
| App: set up a job and press Print | 7 jobs (one a full-bed raster), 2 cancels | 5 cloud tests |
| Scrap placement | ~8 | every live test |
| Eyeball confirmation (`ctx.confirm`) | ~16 | 13 tests |

Three facts shape everything that follows.

1. **The switch actions are the majority and the cheapest to remove.** All
   three consumers (grblHAL `glowforge_switches.c`, gfhardware `switches.py`,
   forgectrl `status.c`/`liveness.c`/`auth.c`) read the same gpio-keys device
   (`/dev/input/event0`, EVIOCGSW). There is no software injection path on the
   board; grblHAL's `GF_SWITCH_FILE` hook exists only in the null-sink host
   build. Adding one to three repos' safety paths is the wrong trade when a
   relay exercises the real edge, the real hardware button latch, and the real
   interlock latch drive.
2. **The app operations and the eyeball confirmations are the slow items**, and
   nearly every confirmation duplicates evidence the test already collects
   (log needles, kernel counters, `armed`, the latch bit) or could collect from
   a witness the machine already has: `head/beam_detect_analog` (baseline
   ~1834, 2600 to 2890 during S300/S400 fire, measured 2026-08-12),
   `beam_detect_digital`, the head accelerometer (the supervisor's own
   liveness witness), the button LEDs (`/sys/class/leds/button_led_*`, already
   read by the baseline), `pic/hv_current`.
3. **The cloud tests conflate two mechanisms.** The service protocol (auth,
   WSS, action dispatch, pulse download, lifecycle events, progress) and
   gfhardware's run loop (button wait, lid/interlock abort, park, retrace,
   cancel). Only the first needs the real service; only the second needs the
   real machine. The seam is clean: `GFUIService` feeds
   `dispatch_action(machine, msg)`, and the hardware sits behind `Machine`
   (gfhardware) or `Emulator` (gfutilities, which already completes a homing
   to print cycle against the real service with canned images).

##### 2. The levers, ranked by payoff

###### L1. A bench actuator fixture, and a typed action seam in forgetest

**Hardware.** Three relay channels at the connectors, no board modification:

| Channel | Where | Contact | Why it is fail-safe |
|---|---|---|---|
| Lid | in series with the lid-switch loop (the J4.12/13 net) | normally closed | a series contact can only add an open, never mask a real lid open; the hardware chain sees exactly what it sees today |
| Interlock | in place of the J8 jumper (Basic/Plus), or in the Pro's plug loop | normally closed | same argument |
| Button | from J5's 12 V to J5 BTN | normally open, pulsed by the fixture firmware (max ~500 ms), never held | a parallel contact can only add a press; see the consent question in §5 |

A Pico W or ESP32 with a trivial HTTP API on the LAN; forgetest gets
`FORGETEST_FIXTURE_URL` and the channel inventory from a bench-local file
(`/data/forgetest/fixture.json`). The interposer harness lives with the bench
and is described in the hardware facts bank, never in the public repos.

**Software: `ctx.act()`.** Replace the free-text `ctx.instruct("Open the lid
NOW ...")` calls with typed actions: `ctx.act("lid", "open")`,
`ctx.act("interlock", "open")`, `ctx.act("button", "press")`, with the
existing wording kept as the human fallback text. The runner fulfills an
action through the fixture when the channel is present and then verifies the
resulting EV_SW state through `/status switches` (the tests already make this
check by hand after every prompt), otherwise it falls back to exactly today's
prompt. Tests declare `actions=[...]` next to `steps`. The `kind` stays the
conservative truth for a bench without a fixture; a declared-`operator` test
whose actions the bench's fixture all covers is routed into the unattended
queue at runtime; `live` never downgrades. Every result records, per action,
whether the fixture or a human fulfilled it (`evidence.operator.actions`).

**Payoff.** All 12 operator tests become unattended. The live tests lose every
action except the arm press. About 37 of the 80 actions are gone.

###### L2. Split the cloud tests: service protocol vs. machine behavior

**(a) Offline action injection for the machine-behavior tests.** An
`OfflineService` in `forgefirm-app` with `GFUIService`'s interface: no auth, no
WSS, a local UNIX socket that accepts action messages in the exact WSS shape
(`{"id", "action_type", "motion_url", "settings", ...}`) and writes every
`send_wss_event` as the same `<action> [id]: finished with event ":..."` lines
the tests already needle on. `load_motion` gains a `file://` branch. forgectrl
passes the mode through as a named setting (`cloud_service = offline`), a test
lever like the `cool_*` gates, harmless on a release image because it only
ever runs offline. The connect-time hunt becomes an injected `hunt` when a
test wants one.

Pulse files: this machine's own captured factory files in `_RESOURCES` are
serial-locked to the bench (`MCsn` passes), so a tool that strips the FIRE
bits and zeroes the power bytes turns them into FIRE-less jobs; or a generator
on top of gfutilities' pulse helpers plus a header generator from the decoded
tag table (`_RESOURCES/FW/PULSE-HEADER-TAGS.md`) synthesizes any job, which
gives the oversize test a 40 MiB job in seconds instead of a full-bed raster
designed in the app.

This moves `cloud.lid-interlock-abort`, `cloud.pause-cancel-paths`,
`cloud.lid-during-button-wait`, and `cloud.oversize-stream` off the app and
off the scrap: no job set-up, no Print, no app cancel (an injected `cancel`),
nothing to burn. They still arm (the run loop unlocks the latch on the button
press), so by the contract's definition they stay `live` even FIRE-less; with
L1 their only human input is the arm press. Going fully offline, rather than
injecting into a live session, is deliberate: a half-measure would send events
for invented action ids to the real service.

**(b) One real print stays.** `cloud.pause-resume` is the right one: it is
where progress, warm-up and rest, the header limits reaching the engine, and
the laser-off resume lead all show, and the lead is only observable with FIRE
bits. It keeps the service-to-machine path honest once per cloud change, and
with L1 it costs one app job and one arm press.

**(c) `cloud.service-protocol`, new.** Under `POST /controller/stop` (cloud
standby), run the existing gfutilities `Emulator` on the board with the board's
credentials and the canned images (small JPEGs, shipped with the dev package);
the operator, or an agent with a browser, only presses Print in the app (the
emulator's `_button_wait` is a no-op). Judge the session, the hunt, the image
uploads, the pulse download, and the lifecycle events from the emulator's log;
optionally a cancel from the app. Then stop the emulator and
`POST /controller/start`. No motion, no lid, no button, no scrap. This needs
none of the emulator-parity work declined on 2026-08-21; the emulator already
does what this test needs. Because `POST /answer` exists, an agent can run it
end to end with nobody at the machine.

###### L3. Replace eyeballs with the witnesses the machine already has

| Today's confirmation | Replacement |
|---|---|
| "Did it mark the scrap?" (4 tests) | `beam_detect_analog` delta over baseline plus `beam_detect_digital` asserted during the fire window plus `hv_current`, in the existing 8 Hz sample trail. The human mark confirm stays in `laser.emission-witness` only: one per campaign, the bench's calibration of the sensor witness. |
| "Is the button dark / lit?" (5 tests) | the button LED brightness attrs. |
| "Did the gantry move?" (`motion.jog-roundtrip`) | the head accelerometer sampled per jog against the thresholds already established for the liveness gate. This also frees `motion.step-timing-under-load` (auto, requires jog-roundtrip) and the whole live block's prerequisite chain from the attended queue. |
| "Did the head reach the home corner?" (`cloud.gfhome-homing`) | accelerometer motion seen plus a kernel displacement consistent with the corner; stronger follow-up: a lid snapshot matched against a bench-local "head at home" reference frame under `/data/forgetest/`. |
| "Does the panel show the bed?" (`camera.snapshot`) | toggle `pic/lid_led` between two snapshots and require a luminance change (proves a live capture, not a stale frame), plus an optional correlation against a bench-local reference frame for orientation. |
| "Did both burns end abruptly?", "did the head back up?", "does the app show cancelled?" | already in the evidence: the emission and beam trails, the retrace log lines, the `:cancelled` event sent. |

###### L4. Merges where a setup is shared, and two reclassifications

**`cloud.mode-switch` absorbs `cloud.hunt-lid-open` and `cloud.gfhome-homing`.**
Sequenced, not simultaneous, because the two need opposite lid states (the
reason they were kept apart on 2026-08-17): lid open, switch to cloud, session
established, the connect-time hunt completes with the lid open (no "unsafe to
move" before its terminal line, airflow gates unjudged, exhaust off), lid
closed, the re-hunt waited quiet, switch back to grbl, Idle, `$H` with
`homing_mode = gfcloud`, homed within the session timeout. One test, one lid
open and close, carrying both absorbed tests' `covers` (grblhal `src/**`,
forgectrl `super.c`, `cool.*`, `airflow.*`). The standing merge rule applies:
merge only where a setup is shared, never auto tests. Without a fixture this
saves an operator cycle; with one, all three are free and separate ids give
invalidation finer teeth, so the merge is right now and can be unwound later.

**`kernel.fire-line` to `auto`.** It is in the always-required core, so it
costs a person every campaign, but its only prompt is conditional on HV
reporting good at idle, which the chain holds low. Reclassify with a
"cannot start" precondition (the same outcome as an unmet prerequisite, not a
FAIL that closes the campaign) when `laser_pgood` reads good; with L1 the
fixture opens the lid instead. Check `results.jsonl` first: if `laser_pgood`
was 0 in every recorded run, the prompt has never fired.

**`camera.snapshot` to `auto`** via L3.

Optional, lower value: the four GRBL travel-job tests (`motion.button-hold-resume`,
`motion.lid-cancel-home`, `motion.interlock-cancel-home`, `motion.lid-policy-hold`)
share a trivial setup (bed clear, 40 to 60 mm of +X). A merge saves three
baseline cycles and no hand actions; not worth it once L1 exists.

###### L5 (secondary). Finer `covers` maps

Every cloud test covers all of `forgefirm-app`, `gfhardware`, and
`gfutilities`, so a one-line websocket change invalidates six real prints.
With L2 the natural partition is: the protocol test covers
`gfutilities/service/**`, `basemachine.py`, `emulator.py`; the offline behavior
tests cover `gfhardware/machine.py`, `feeder.py`, `switches.py`, `cnc.py`,
`gfcloud.py`, the offline service; the real print stays coarse as the
integration. A websocket change then reruns the protocol test (agent-runnable)
plus one real print. The coverage lint still requires every path covered; this
is a re-partition, not a relaxation. The laser block's `kernel **` coverage is
honest (the kernel is the emission path) and stays.

###### Usability tweak A: the message area goes to the log

The notes at the top of the Campaign card come from `Runner._note` and two
direct appends (`runner.py` ~341, ~588, ~608), a bounded list rendered as
`state.messages`:

| Source | Already recorded elsewhere? |
|---|---|
| baseline boot-reference failure | nowhere else |
| takeover recovery at start-up | nowhere else |
| queue opened / skipped / stopped / driver errored | the queue card renders the live queue state; a stop-on-FAIL shows in the test row |
| leftovers before and after a run | the run's own log pane and the result's `evidence.baseline.pre/post` |

`state.messages` and the `#msgs` div go away. Every `_note` goes to a runner
journal: a `forgetest` logger under the unified tree
(`/data/log/forgefirm/forgetest/`, so it shows in the panel's Logs tab and the
sanitized export like the other daemons), and, when a run is in progress, into
that run's log as well (the leftovers and baseline lines already do). The
Campaign card keeps only the invalidate note and the transient click feedback
(`actmsg`, `qmsg`). Nothing is lost: leftovers stay in evidence, queue outcomes
stay in the rows and the queue card, the raw log stays the bench's record.

###### Usability tweak B: instructions before the test, not popups during it

Every attended test already declares `steps=[...]`, rendered today only under
each row's *details*; `ctx.instruct()` then appears inline in the run card
(`#prompt`) with no warning, and many of those prompts are timed. Two changes:

**Presentation: a standing "What you will do" pane in the run card.** When a
test is selected or started, the run card shows its steps as a numbered
checklist above the log, for the whole run. For an attended queue, the pane
shows the union for the queue before it starts, then the per-test pane takes
over as each test begins. Prompts advance the checklist in place instead of
opening a new box: the current step highlights, the buttons attach to it, done
steps gray out. With `actions=[...]` the pane is typed: a step the fixture
performs is marked *automatic* so the operator knows what not to do, and a
timed step says so up front ("step 3 is timed: about 8 s").

**Structure: timed steps become Ready-gated.** The surprise is partly how the
tests are written: start the move, then `instruct("press NOW")`. Flip the
order wherever a step is timed: `instruct("When you click Ready, the head
starts a 12 s move; press the button about 2 s in")`, Ready, then the test
starts the move and waits with a generous window. `arm_and_fire` already works
this way ("Ready?" then the stream); the button, lid, and interlock steps in
`motion.*`, `laser.pause-resume-lid-cancel`, and the cloud tests do not. This
changes nothing about what is measured, is replayable host-side, and is the
same seam the fixture plugs into later (the fixture fulfills the step with
exact timing; a human gets the Ready gate).

##### 3. Per-test disposition

| Test | Today (the operator does) | Proposal | Kind: no fixture, then with fixture |
|---|---|---|---|
| camera.snapshot | look at the panel | L3 lamp toggle + reference frame | auto, auto |
| camera.lid-privacy | lid x3 | L1 | operator, auto |
| cloud.mode-switch | (auto) | L4 merge host: lid open for the connect, `$H` after the switch back | operator, auto |
| cloud.gfhome-homing | watch, confirm the corner | merged into mode-switch; L3 evidence | eliminated |
| cloud.hunt-lid-open | lid x2, confirm | merged into mode-switch | eliminated |
| cloud.lid-during-button-wait | app job, Print, lid x2, confirm | L2a offline print + L1 lid; LED for "button dark" | operator (one lid), auto |
| kernel.fire-line (core) | conditional lid | L4 precondition, or fixture lid | auto, auto |
| laser.arm-wait-lid | lid x2 | L1 | operator, auto |
| motion.jog-roundtrip | bed clear, confirm motion | L3 accelerometer | auto, auto |
| motion.button-hold-resume | button x2 | L1 | operator, auto |
| motion.lid-cancel-home | lid x4, button x1 | L1 | operator, auto |
| motion.interlock-cancel-home | interlock x2 | L1 | operator, auto |
| motion.lid-policy-hold | lid x2 | L1 | operator, auto |
| laser.emission-witness (core) | scrap, ack, arm, confirm mark + dark | keep the mark confirm; LED for dark | live, 1 press |
| laser.disarm-in-hold | ack, arm, confirm | L3 (Hold state + armed + LED) | live, 1 press |
| laser.armed-kill | ack, arm x2, judge x2 | L3 trails | live, 2 presses |
| laser.pause-resume-lid-cancel | ack, arm, button x2, lid x2, confirm | L1 + L3 | live, 1 press |
| cloud.lid-interlock-abort | 2 app jobs, 2 Prints, arm x2, lid x4, interlock x2, confirm x2 | L2a offline FIRE-less + L1 | live, 2 presses, no app, no scrap |
| cloud.pause-resume | app job, Print, arm, button x2, confirm x2 | keep real (L2b); L1 for pause/resume; L3 | live, 1 press + 1 app job |
| cloud.oversize-stream | full-bed raster in the app, Print, arm, 2 min burn, button x2, app cancel, confirm | L2a synthesized 40 MiB FIRE-less job + L1; injected cancel | live, 1 press, no app |
| cloud.pause-cancel-paths | 2 app jobs, 2 Prints, arm x2, button, lid x2, app cancel, confirm | L2a + L1 | live, 2 presses, no app |
| cloud.service-protocol (new) | Print in the app | L2c, agent-runnable | operator (app only) |

##### 4. The campaign after

| | Today | L2 + L3 + L4 + tweaks, no fixture | + L1 fixture | + fixture arm press (opt-in) |
|---|---|---|---|---|
| Catalog | 45 | 44 | 44 | 44 |
| Unattended | 25 | 28 | 41 | 41 |
| Operator actions | ~80 | ~45 (app 7 jobs to 1, confirms 16 to 2) | ~16 (11 arm presses, 1 app job, scrap, 1 mark) | ~5 |
| Attended minutes | 111 | ~85 | ~60, sitting through the live block | the same, hands-free |

The floor is by design: the always-required core wants one real emission
witness per campaign, so every campaign needs a person with eye protection in
the room for one burn, and the contract wants the arm press through the
controller's normal path.

##### 5. Lines not crossed, and the one policy question

- A FIRE-less armed run stays `live`. It is "emission possible" by the
  contract's conservative definition, even though it needs no scrap.
- No software switch injection in the three consumers' safety paths on the
  board. The fixture exercises the real edge, the real hardware button latch
  (`laser.pause-resume-lid-cancel` checks it SET), and the real interlock
  latch drive.
- forgetest never touches the laser latch. The offline service never talks to
  the real service. The protocol test never moves the machine.
- The cloud `requires` chains stay minimal (the 2026-08-17 rule), and every
  re-ported test gets its host-side replay (`tests/test_cloud_suite.py` and
  friends) before the operator sees it.
- **The policy question: may the fixture press the button for the arm?** A
  fixture press goes through the controller's normal path (the hardware
  input), so the consent becomes the queue's live acknowledgment with the
  operator present. Recommendation: human by default, since the operator is
  in the room for the fire watch anyway; fixture arm presses as an explicit
  opt-in (a physical enable on the fixture's button channel, plus the page's
  live ack, plus the per-action record in evidence).

##### 6. Decisions that are the operator's

1. Build the fixture? It is the single biggest lever and a small build (three
   relays, an interposer harness at J4/J5/J8, a Pico W). Everything else here
   stands without it.
2. Fixture arm presses: never, or opt-in under the live ack?
3. Merge mode-switch + hunt-lid-open + gfhome-homing now, or keep them
   separate and wait for the fixture?
4. Is `cloud.service-protocol` a catalog test (carries `covers` for the
   service layer, participates in inheritance) or a bench tool? Recommendation:
   a catalog test.

##### 7. Order of work

1. **DONE 2026-08-22. forgetest only, no new hardware:** L3, L4, usability
   tweaks A and B, Ready-gating the timed steps, and per-test implementation
   hashing. Two tests gone, three to `auto`, the confirms down to two, the
   page quiet, the operator reading the whole list once instead of racing
   popups.
2. **The cloud split:** L2a offline service and the pulse-file tooling, the
   four behavior tests re-ported onto it: **DONE 2026-08-22.** L2c, the
   protocol test with the existing emulator: **code-complete 2026-08-23,
   bench owed** (see COLD PICKUP above).
3. **The fixture:** L1 hardware and the `ctx.act()` seam, with the fallback
   wired so a bench without a fixture behaves exactly as today.
4. **L5** once the cloud split exists.

Each step is a catalog change, so each lands with its coverage map kept
current (`python3 -m forgetest.coverage --enforce`) and is proven in the
order the working rules require: host test, then a bench drill logged in
`CAMPAIGN-LOG.md`.

### The kernel configuration review (tree-root working file), merged 2026-08-24

The report `KERNEL_CONFIG_REVIEW.md`, verbatim: its status section is the record of the two rounds that built the board-only kernel (what each finding became, with its proof), and the original report below it is the evidence they were decided on. Its "cannot start cut" row carries the correction the campaign forced. The suggestions it left are BRINGUP item 20; the cosmetic upstream dmesg lines are left by decision. The file is deleted.
#### ForgeFIRM kernel configuration review (2026-08-24)

##### Status (2026-08-24): what was done, what remains

Section numbers below refer to the original report that follows.

###### Done: implemented, built, bench-validated, committed and pushed

Commits: `meta-openglow e1bb4ac` (kernel trim) and `8f8b540` (module pin),
`kernel-module-glowforge 615a36f`, `forgefirm cb9cd53` (BRINGUP item 21, CAMPAIGN-LOG
entry "2026-08-24: the kernel built for one board"). Bench validation ran on dev image
20260824164619 (built from the same tree state before the commits); the post-commit
build that adds the module pin is what the acceptance campaign runs on.

| Report item | What was done | Proof |
|---|---|---|
| 1.1 `evbug` autoload | `# CONFIG_INPUT_EVBUG is not set` | Not in `lsmod`; no `evbug:` lines in dmesg |
| 1.2 dropped lockup-panic lines | `DETECT_HUNG_TASK=y`, `SOFTLOCKUP_DETECTOR=y`; the two `BOOTPARAM_*_PANIC=y` lines now land | `/proc/sys/kernel/hung_task_panic` = 1, `softlockup_panic` = 1 |
| 1.3 `MULTIPLEXER`/`MUX_GPIO` requested `=y`, landed `=m` | Written as `=m` (plus `MUX_MMIO=m`); every fragment line now matches the built `.config` (checked line by line) | Configure-check diff: no unlanded lines |
| 1.4 distro/kernel mismatch | Bluetooth, sound/ASoC, NFS/SUNRPC, PCI/PCIe, ext2/ext3, `IPV6_SIT` off. IPv6 core kept (see Remaining) | dmesg has none of them; `sit0` gone |
| 1.5 DT leftovers | `&asrc`, `&usbphy1/2`, `&usbphynop1/2`, `&usbmisc` disabled | Built DTB shows `status = "disabled"`; the phy/dummy-supply lines are gone from dmesg |
| 1.6 `glowforge_pic` without `spi_device_id` | Table `{ "pic" }` + `MODULE_DEVICE_TABLE(spi)`; pinned at `615a36f` | `alias=spi:pic` in the built module; the boot warning clears on the post-commit image |
| 2.1 no crash record | `PSTORE`, `PSTORE_RAM`, `PSTORE_CONSOLE`; `ramoops@2ff00000` (1 MiB, `no-map`, 32 KiB records, 256 KiB console, 16-byte ECC) in the region the factory bootloader already holds back; `pstore` line in fstab | Forced `sysrq-c`: reboot on the timeout, next boot 0 header errors, `dmesg-ramoops-0` (24 KB, "Panic#1 Part1") and `console-ramoops-0` (ends "Kernel panic - not syncing: sysrq triggered crash / Rebooting in 10 seconds.. / ECC: No errors detected") |
| 2.2 `panic=10` only on the cmdline | `CONFIG_PANIC_TIMEOUT=10` | `/proc/sys/kernel/panic` = 10; the DTS fallback boot now reboots on panic too |
| 2.4 SDMA firmware never loads | Decision (a): ROM scripts stay; `linux-firmware-imx-sdma-imx6q` and `-imx7d` removed from `MACHINE_FIRMWARE`; `dmas`/`dma-names` deleted from `&ecspi2` | `/lib/firmware/imx` gone; dmaengine summary holds no channels; PIC probes and reads in PIO |
| 4.1 built-in dead weight | USB, Ethernet/PHY/PTP/PPS, CAN, BT, SATA/SCSI, PCI, MTD/NAND/UBI, RAM disks, JFFS2/UBIFS/NFS/FUSE/autofs/quota/ISO/UDF/MSDOS/binfmt_misc, DRM_IMX + HDMI/LVDS/panels/bridges/MXSFB, FB/fbcon/logo/VT/backlight, all audio, touchscreens/HID/mouse/serio/beeper/RC, PMICs/expanders/W1/SIOX/other-board sensors and bus glue, ten other i.MX SoCs + Vybrid, PSCI, TEE, `ARCH_MULTI_V6` (ARMv7-only code), suspend/kexec/crash-dump/ATAGs/swap/HIGHMEM, three cpufreq governors, BFQ/Kyber, connector, five initrd decompressors. Kept by decision: `DRM` + `DRM_ETNAVIV`, `IMX_IPUV3_CORE`, `DEBUG_FS`, `DEVMEM`, `MAGIC_SYSRQ`, `KPROBES`, `PERF_EVENTS`, `IKCONFIG_PROC`, `NETFILTER`, `SMP` | `.config` 1634/245 to 819/32 (`=y`/`=m`); zImage 9.13 MB to 4.76 MB; vmlinux text 14.7 MB to 7.8 MB; MemTotal +9.4 MB |
| 4.2 253 modules shipped, 27 needed | `MEDIA_SUPPORT_FILTER=y` + `SUBDRV_AUTOSELECT` (the DVB tree gone), other sensors off; `kernel-modules` replaced by the board's 13-module list in `glowforge.conf` (dependencies follow through modules.dep RDEPENDS; the Wi-Fi ciphers are built in so nothing loads by alias from the rootfs) | 31 `kernel-module-*` packages; built modules 9.7 MB to 2.4 MB; 31 loaded on the bench, all needed |
| 4.3 firmware dead weight | `firmware-imx-epdc`, `firmware-imx-vpu-imx6q`, both SDMA packages removed | `/lib/firmware` 7.7 MB to 2.6 MB |
| 5.1 `DRM_IMX` removal vs the GPU demosaic | Removed; verified | `/dev/dri/renderD128` present, `card1` gone; lid and head streams ran with `gpu: GLES2 debayer up`, GPU IRQs 0 to 135 |
| New: virtual console gone | `USE_VT = "0"` so no tty1 getty respawns against a device that no longer exists | inittab carries only `ttymxc0` |

Also validated on the live image: every node binds, `devices_deferred` empty, Wi-Fi
associated with `regulatory.db` loaded (country US), switches on `event0`, no QA
warnings in the build.

Lessons now written into the fragment's comments: the defconfig never names `PM`,
`REGULATOR`, `EXT4_FS`, `CONFIGFS_FS`; it got them by selection from suspend, the
PMIC drivers, ext3 and the USB gadget, so a trimmed fragment must pin what it keeps.
`KEYBOARD_ATKBD` selects `SERIO`, `I2C_IMX` selects `I2C_SLAVE`, `DRM_MXSFB` selects
`DRM_MXS`, `SOC_VF610` selects `PINCTRL_VF610`.

###### Remaining: issues found and not acted on

State after round 1. Round 2 (below) closes 1.4 (IPv6 is on), 2.5 (firmware set),
2.6 (performance governor), the `consoleblank` and spi-imx lines, and the empty-ring
message; the cosmetic upstream lines stand.

| Report item | Issue | Suggested action |
|---|---|---|
| 1.4 | `IPV6=y` while `DISTRO_FEATURES` removes `ipv6`; `forgectrl/src/auth.c` references `AF_INET6` | Decide once: either put `ipv6` back into the distro (the kernel matches the code) or make `auth.c` IPv4-only and drop `IPV6` from the kernel |
| 2.5 | `wlcore: WARNING Detected unconfigured mac address in nvs` / `This default nvs file can be removed`: `linux-firmware-wl18xx` ships the generic `wl1271-nvs.bin` (and `wl127x-nvs.bin`, three unused `wl18xx-fw*` variants, three `TIInit_*.bts` BT scripts) | Cosmetic. A `linux-firmware` bbappend can drop the NVS and BT files; keep all four `wl18xx-fw*` unless every board is PG 2.2 |
| 2.6 | cpufreq policy is `ondemand` from the defconfig default; nothing sets a governor. A single core with a `SCHED_FIFO` producer (BRINGUP item 16) idles at 396 MHz | Policy, not a defect: `performance` while a job runs (forgectrl) or `CPU_FREQ_DEFAULT_GOV_PERFORMANCE`; measure against item 16 first |
| 3 | `Unknown kernel command line parameters "consoleblank=0 board=glowforge"`: `consoleblank` is a VT parameter and VT is gone; `board=` is for userspace | Drop `consoleblank=0` from the uEnv (forgefirm-uenv); `board=` stays |
| 3 (new) | `spi_imx 200c000.spi: error -ENODEV: can't get the TX DMA channel!` at ERR level at every boot: upstream logs the absent channel with `dev_err_probe` and continues in PIO | Cosmetic. Accept, or a one-line layer patch demoting it (a 14th patch in the bbappend) |
| 3 | `hwmon hwmon1: temp1_input not attached to any thermal zone` (lm75 with `THERMAL_OF`) | Cosmetic; leave |
| 3 | `glowforge_cnc cnc: cannot start cut; no data enqueued` at 31 s after boot | WRONG, corrected 2026-08-24: those occurrences were the SDMA clocks gated by the ecspi2 dmas deletion (CAMPAIGN-LOG 2026-08-24, "the SDMA clocks, held by nobody"); no motion on any image since the trim. Someone issues a run on an empty ring at controller start (forgectrl liveness probe or grblHAL init); worth finding and silencing, in the module's owner's time |
| 3 | fw_devlink "Fixed dependency cycle(s)" (46 lines), "Static allocation of GPIO base is deprecated" (7), the SDIO "voltages below defined range" and "read-only switch" lines | Upstream behavior; leave |
| 5.6 | `kas/README.md` backlog #2 said the PWM prescaler port was obsolete, the PIC SPI delay a bring-up TODO, and `reg-userspace-consumer` enabled by the fragment | Done: the paragraph now describes patches 0009 and 0004 as carried, the 12 V rail without a consumer node, and the config fragment as the board's kernel (uncommitted in `forgefirm`) |

###### Remaining: suggestions not acted on

State after round 1. Round 2 closes 5.2 (`SMP=n`) and the pstore export; 5.5 is
answered (kept, root-only exposure); the firmware split is done as part of 2.5.

| Report item | Suggestion | Why it waits |
|---|---|---|
| 5.2 | `CONFIG_SMP=n` on the single core (no spinlock/IPI overhead, `NR_CPUS=4` gone) | Needs a measurement against BRINGUP item 16 (producer stalls) before it is worth a platform change |
| 5.5 | `KPROBES`, `PERF_EVENTS`, `BPF_SYSCALL`, `DEBUG_FS`, `DEVMEM`, `MAGIC_SYSRQ` off for release | One kernel serves both images; a release-only config needs a second kernel variant or a fragment switch, which is more machinery than the gain |
| 4.3 | Split `linux-firmware-wl18xx` to the one `wl18xx-fw-*` this hardware boots | Only once every board's PG revision is known |
| 2.1 follow-up | Have forgectrl's log export include `/sys/fs/pstore` (and clear records after export) | The records exist now; the consumer is a forgectrl feature |
| 4.2 note | Five helper modules are built and not shipped (`crc7`, `crc-ccitt`, `libcrc32c`, `st-accel-spi`, `st-sensors-spi`; the last two are selected by the accelerometer driver) | 0.1 MB of build output; harmless |

###### Round 2 (2026-08-24, later): implemented, host-proven, committed, built as images/20260824200726 (unflashed)

| Item | What was done | Proof so far |
|---|---|---|
| 1.4 IPv6 | `ipv6` back in `DISTRO_FEATURES` (busybox IPv6 + ifupdown inet6, openssh/ntp/rsyslog IPv6); busybox `udhcpc6` (+RFC 3646) with a hook script (`default6.script`) and a `wlan0 inet6 manual` stanza that starts it; forgectrl listens dual-stack (`ulfius_init_instance_ipv6`, `U_USE_ALL`), grblHAL's TCP:23 is an `AF_INET6` socket with `IPV6_V6ONLY=0`, forgetest binds `::` (dual-stack). The kernel already did SLAAC (the board holds a ULA and the GUA prefix route); the DHCPv6 address is what the client adds | forgectrl `-Werror` build + 11 tests, grblHAL build + 4 CI tests, forgetest 252 tests, bind smoke (`AF_INET6`, v6only 0). Bench (dev 20260824201945): proven end to end. A GUA from pfSense's Kea via `udhcpc6` (lease 7200 s, renew OK); ports 22/23/8080/8090 answer on it from a host on another VLAN; IPv6 egress to the WAN gateway works. The earlier "no GUA" was an OpenWrt access point on the bench VLAN still running RA + DHCPv6 in server mode (its NoAddrsAvail Advertise beat pfSense's, and busybox keeps the first Advertise); disabled by the operator, the other two were already off |
| 2.5 firmware files | `linux-firmware_%.bbappend`: only `wl18xx-fw-4.bin` stays (the current factory image ships exactly that plus `wl18xx-conf.bin`); the `wlcommon` package (NVS files, BT `.bts`) is no longer pulled | Factory `/factory/img1/lib/firmware/ti-connectivity` = `wl18xx-conf.bin` + `wl18xx-fw-4.bin`. Bench: Wi-Fi up, no NVS warning |
| 2.6 / BRINGUP 16 | `CPU_FREQ_DEFAULT_GOV_PERFORMANCE=y`, ondemand and the other governors off: 996 MHz always. Item 16 carries the re-measure plan | Bench: `scaling_governor` = performance; the item-16 drill (clamps, min margin) on this image |
| 3 `consoleblank=0` | Dropped from `uEnv.txt` and the U-Boot default env (`glowforge.h`) | Bench: no "Unknown kernel command line parameters" line |
| 3 spi-imx ERR line | Patch 0014: `dev_err_probe` only when the failure is not `-ENODEV` (no DMA described = PIO by design) | Bench: no `can't get the TX DMA channel` line |
| 3 `cannot start cut` | Found: grblHAL's `issue_run` already treats a run on an empty ring as an ordinary race (`idle` + `ENODATA`, "already consumed"); only the module logged it at ERR. `cnc.c` now `dev_dbg`s it | Module compiles; bench: line gone from dmesg |
| 5.2 `SMP=n` | `# CONFIG_SMP is not set` (UP kernel: GPT tick, no IPIs, no spinlock cost) | Configure check + bench boot owed |
| 2.1 follow-up | forgectrl's log export stages `/sys/fs/pstore/*` under `system/pstore/` (README lists it) | forgectrl build + tests; bench: export after the sysrq record |
| Fluff | `nano` (+`file`/libmagic, 8.7 MB) release-image only via `IMAGE_INSTALL:remove`, kept on dev; `BAD_RECOMMENDATIONS += eudev-hwdb` (7.7 MB of USB/PCI IDs); `python3-urllib3` bbappend drops its pyOpenSSL/cryptography recommendation (~6 MB: cryptography, pyopenssl, cffi, pycparser, ply; nothing imports them) | Build + campaign |

Fluff found and not acted on: the `python3` meta-package installs `python3-modules` (tkinter, idle, 2to3, pydoc, ensurepip, venv, debugger, doctest, asyncio, multiprocessing, xmlrpc: ~10 MB) where the apps declare only `python3-core` + a few modules; replacing `python3` with the explicit module set needs an import audit of gfcloud/gfhome/gfhardware/gfutilities (the campaign's cloud tests are the check). `libgnutls30`, `libunistring5`, `nettle`, `libgmp10` (~4.9 MB) are installed with no package depending on them and no binary on the rootfs linking them; a `PACKAGE_EXCLUDE` experiment on a build would name the holder if there is one. `v4l-utils` (1.8 MB) is a declared runtime dependency of forgectrl and gfhardware (media-ctl); `shadow` is pulled by openssh/ntp/dbus; `curl` is the update downloader; `openssl-bin` serves `ca-certificates`.

Debug features in release (5.5): no runtime cost when unused; the exposure is root-only (`/dev/mem`, debugfs, sysrq over a physically attached console, kprobes/perf/BPF with unprivileged BPF already off) and root can load modules anyway, so a compromise of root is the actual boundary. Kept.

###### Owed, in the operator's hands

A GRBL job on the image, then the full acceptance campaign (platform change) on the
post-commit build, which also confirms the `spi_device_id` warning is gone at boot.

##### Original report

Report only at the time of writing. Nothing was changed on the bench, in any repo, or in the build tree.

##### Scope and evidence

| Source | What was examined |
|---|---|
| `meta-openglow/.../linux-fslc/glowforge.cfg` + `linux-fslc_%.bbappend` | The config fragment and the 13 patches |
| `arch/arm/boot/dts/nxp/imx/glowforge.dts` + `imx6qdl.dtsi`/`imx6dl.dtsi` defaults | Which peripherals the board actually enables |
| Bench board, fresh boot (9 min uptime) | `dmesg`, `/proc/config.gz`, `lsmod`, platform/i2c/spi/sdio driver bindings, `/proc/interrupts`, `/proc/iomem`, debugfs gpio + clk tree, cpuidle/cpufreq, sysctl, `/lib/modules`, `/lib/firmware` |
| WSL `forge-yocto` build tree (`linux-fslc/6.12.20+git`) | Built `.config`, `imx_v6_v7_defconfig`, module sizes, `imx-base.inc`, the image manifests |
| `forgectrl/src`, `python3-gfhardware`, `Glowforge-Utilities`, `kernel-module-glowforge/src` | Which kernel interfaces userspace consumes |

The running kernel config is byte-identical to the built `.config` (the board runs the
current build). Kernel: `6.12.20-fslc`, `SMP PREEMPT`, zImage 9.1 MB, vmlinux text
14.7 MB; 1634 `=y` and 245 `=m` symbols against a defconfig of 403 + 63.

##### 1. Misconfigurations (wrong today)

###### 1.1 `evbug` autoloads and logs every switch event to the kernel log
`CONFIG_INPUT_EVBUG=m` (inherited from the defconfig). `evbug` carries a catch-all
`MODULE_DEVICE_TABLE(input, ...)`, so udev loads it for the `switches` gpio-keys device on
every boot (`lsmod` shows it; `dmesg` shows `evbug: Connected device: input0` and
`evbug: Event. Dev: input0, Type: 5, Code: 4, Value: 1` for the HV-enable readback).
Every lid, button, interlock, and HV-enable transition lands in `dmesg`/rsyslog for the
life of the machine. It is a kernel debugging aid, nothing consumes it.
Fix: `# CONFIG_INPUT_EVBUG is not set` in `glowforge.cfg`.

###### 1.2 Two safety lines of the fragment were silently dropped
`glowforge.cfg` requests `CONFIG_BOOTPARAM_HUNG_TASK_PANIC=y` and
`CONFIG_BOOTPARAM_SOFTLOCKUP_PANIC=y` with the comment "Hung-task and softlockup also
panic on the same reasoning". Neither symbol exists in the built `.config`, because their
parents are off: `# CONFIG_DETECT_HUNG_TASK is not set`, `# CONFIG_SOFTLOCKUP_DETECTOR is
not set`. On the board `/proc/sys/kernel/hung_task_panic` and `softlockup_panic` do not
exist. Only `PANIC_ON_OOPS` is live; a hard lockup or a hung feeder does not reach the
laser-safing panic notifier the fragment describes.
Fix: add `CONFIG_DETECT_HUNG_TASK=y` and `CONFIG_SOFTLOCKUP_DETECTOR=y` (which pulls
`LOCKUP_DETECTOR`) ahead of the two `BOOTPARAM_*` lines; consider
`CONFIG_HARDLOCKUP_DETECTOR=y` (the buddy detector is available: `HAVE_HARDLOCKUP_DETECTOR_BUDDY=y`,
though on one core it has no buddy, so the perf-based NMI detector is the only real one and
arm32 lacks it; softlockup is the practical ceiling). Verify after the build with
`ls /proc/sys/kernel/{hung_task,softlockup}_panic`.

###### 1.3 Two more fragment lines are not what landed
`CONFIG_MULTIPLEXER=y` and `CONFIG_MUX_GPIO=y` are requested, `=m` is what the build
produced (both load fine as modules; `mux_gpio`, `mux_mmio`, `mux_core` are in `lsmod`).
Functionally harmless, but the fragment does not describe the kernel. Either write `=m`
(and `CONFIG_MUX_MMIO=m`, which the IPU CSI muxes need and which nothing pins) or find out
why the merge demoted them.

###### 1.4 Distro features and the kernel disagree
`forgefirm.conf` removes `bluetooth bluez5 alsa nfs pci ipv6 ext2` from `DISTRO_FEATURES`,
but the kernel is built from the multi-board `imx_v6_v7_defconfig`, which does not follow
distro features. The kernel therefore still carries, built in:

| Feature removed from the distro | Still in the kernel |
|---|---|
| bluetooth | `BT=y`, `BT_HCIUART=y` (+LL, serdev), `BT_BNEP=m`; `Bluetooth: Core ver 2.22` in dmesg. The WL1805 is Wi-Fi only (no BT core, no serdev node). |
| alsa | `SOUND/SND/SND_SOC=y` with the whole i.MX ASoC stack and ten codec drivers; `fsl-asrc` binds to the SoC's ASRC (16 clocks + an IRQ) because `imx6qdl.dtsi` leaves `&asrc` `okay`. "No soundcards found." Audio/buzzer is not a planned feature. |
| nfs | `NFS_FS=y` (v3, v4, v4.1, v4.2) + SUNRPC; `rpciod`, `xprtiod`, `nfsiod` kthreads at boot. |
| pci | `PCI=y`, `PCIE_DW_HOST=y`, `PCI_IMX6=y`, MSI, ASPM. No PCIe node is enabled. |
| ipv6 | `IPV6=y`, `IPV6_SIT=y` (creates the `sit0` device seen in `/sys/class/net`). `forgectrl/src/auth.c` references `AF_INET6`, so keep IPv6 core unless that is resolved; `IPV6_SIT` has no consumer. |
| ext2 | `EXT2_FS=y`, `EXT3_FS=y` as separate drivers; ext4 mounts both formats. |

###### 1.5 Device-tree leftovers enabled by the SoC defaults
`imx6qdl.dtsi` enables these without a `status`, and the board has no consumer:
- `usbphy1`/`usbphy2` (mxs_phy), `usbphynop1`/`usbphynop2`, `usbmisc`: no USB controller
  node is enabled (`usbotg`, `usbh1` are `disabled`, as in the factory tree). They produce
  `supply phy-3p0 not found, using dummy regulator` and `dummy supplies not allowed for
  exclusive requests (id=vbus)` at every boot.
- `asrc`: `status = "okay"` by default; binds `fsl-asrc` as above.
Fix (DTS): `status = "disabled"` on `&asrc`, `&usbphy1`, `&usbphy2`, `&usbphynop1`,
`&usbphynop2`, `&usbmisc`.

###### 1.6 `glowforge_pic` has no `spi_device_id` table
`SPI driver glowforge_pic has no spi_device_id for glowforge,pic` at every boot. The SPI
core wants an `spi_device_id` table alongside `of_device_id` (module alias generation and
the non-OF match path). `kernel-module-glowforge/src/glowforge.c` has `pic_dt_ids` only.
Hygiene, no functional effect while the device comes from the DT.

##### 2. Incomplete configuration

###### 2.1 No crash record survives a panic
`# CONFIG_PSTORE is not set`; no ramoops. The design is `PANIC_ON_OOPS` + `panic=10`, so
the machine reboots ten seconds after any oops and the reason is gone unless a serial
console happens to be attached. The factory environment carried
`ramoops.mem_address/mem_size/record_size/console_size` on the command line for exactly
this reason (visible in the U-Boot `mmcargs`). Recommend `CONFIG_PSTORE=y`,
`CONFIG_PSTORE_RAM=y`, `CONFIG_PSTORE_CONSOLE=y` (and `PSTORE_PMSG` if forgectrl wants to
leave breadcrumbs), backed by a `ramoops` node under `reserved-memory` in `glowforge.dts`
so it does not depend on the bootloader environment. The record is then readable from
`/sys/fs/pstore` after the reboot, and forgectrl's log export can pick it up.

###### 2.2 `panic=10` lives only in the boot arguments
`CONFIG_PANIC_TIMEOUT=0`. The DTS fallback `bootargs` has no `panic=`, so a boot that falls
through to the DTS (the documented recovery ladder) hangs on panic instead of rebooting.
`CONFIG_PANIC_TIMEOUT=10` makes the behavior independent of the environment. (Keep the
cmdline value too; the cmdline wins when present.)

###### 2.3 Lockup detectors (see 1.2).

###### 2.4 SDMA RAM firmware never loads (decided: keep the ROM scripts, drop the packages)
`imx-sdma 20ec000.dma-controller: external firmware not found, using ROM firmware`.
`IMX_SDMA=y` probes at 0.42 s, before the rootfs, so `sdma-imx6q.bin` (installed by
`linux-firmware-imx-sdma-imx6q`) is never used; the ROM scripts run. The pulse script is
loaded by glowforge.ko itself (halfword 7680), not by the firmware.

Decision: the ROM-script behavior stays, and the two SDMA firmware packages
(`linux-firmware-imx-sdma-imx6q`, plus `linux-firmware-imx-sdma-imx7d`, which the
`imx-mainline-bsp` override also pulls) leave `MACHINE_FIRMWARE`. This is a runtime no-op:
nothing on the board runs on the RAM firmware. Loading it deliberately was rejected because
no client gains from it and it would let a PIC SPI burst run through SDMA channel 0 next to
the pulse channel during a job (channel 0 has the highest priority).

SDMA client inventory behind that decision (running board + DT + driver source):

| Client | `dmas` in the SoC dtsi | Use today | Scripts needed |
|---|---|---|---|
| glowforge.ko pulse ring | n/a (driven directly, channel 26, priority 6, EPIT1 event 16) | The only real user | Its own script, loaded by the module |
| ecspi2 (PIC) | yes | Two dmaengine channels held since probe, 0 bytes transferred; `spi-imx` uses DMA only for transfers of 64 bytes or more, PIC transactions are 3 bytes and the largest observed bucket is 32-63 bytes (full register-map reads). Only the sysfs `raw` write can cross 64 bytes; today that path logs `sdma firmware not ready!` once, and the SPI core retries in PIO and disables DMA for the controller for good. | RX ROM; TX `mcu_2_ecspi` is a RAM script on i.MX6Q/DL (ERR009165 path) |
| uart1 (console) | yes | Never; the console port is excluded from DMA | n/a |
| uart2 | yes | Enabled in the DTS, nothing opens `ttymxc1` | ROM |
| asrc | yes | Enabled only by the dtsi default, never opened (removal list) | RAM |
| i2c1/2/4 | none | PIO | n/a |
| uSDHC x3, IPU/CSI, VPU, GPU, CAAM | own DMA masters | not SDMA | n/a |
| mxs-dma (`110000`) | separate APBH engine | no client | n/a |

`/sys/kernel/debug/dmaengine/summary` lists exactly the two ecspi2 channels; the SDMA IRQ
count is static at idle (all 170 came from boot: script load and verify, device open, 40 V on).
The firmware layout in the binary checks out against the DTS comment: v3.6,
`ram_code_size` 2754 bytes = 1377 halfwords at 6144, so RAM code spans 6144-7520 and the
highest script entry is 7419; the pulse script at 7680-7819 is clear.

Companion change (DTS): drop `dmas`/`dma-names` from `&ecspi2`. That releases the two held
channels, makes the PIC's PIO behavior explicit instead of relying on the 64-byte threshold
and the fallback path, and leaves the pulse ring as the SDMA's only client, which is what the
timing argument in BRINGUP item 16 assumes.

###### 2.5 Wi-Fi NVS
`wlcore: WARNING Detected unconfigured mac address in nvs, derive from fuse instead` and
`This default nvs file can be removed from the file system`: the generic
`wl1271-nvs.bin` from linux-firmware is installed. The MAC comes from the chip fuse anyway,
so this is cosmetic. Removing the file (or shipping a real NVS) silences it.

###### 2.6 cpufreq policy is the defconfig default
`CONFIG_CPU_FREQ_DEFAULT_GOV_ONDEMAND`, OPPs 396/792/996 MHz, `ondemand` at runtime,
nothing in forgectrl sets a governor. On a single core with a `SCHED_FIFO` step producer
(BRINGUP item 16), the 396 MHz idle floor plus ondemand's sampling delay is a latency
source at job start and between moves. Not a defect; a policy to decide. `performance`
while a job runs (forgectrl) or `CONFIG_CPU_FREQ_DEFAULT_GOV_PERFORMANCE` (mains-powered
machine, SoC at 47 C with 85 C passive trip) are the two levers. Drop
`CONSERVATIVE`/`POWERSAVE`/`USERSPACE` governors either way.

##### 3. dmesg review (fresh boot)

No driver probe failed; `/sys/kernel/debug/devices_deferred` is empty; every DTS node
binds (`cnc`, `thermal`, `pic`, `head`, both cameras, 3 accelerometers, lm75, wl18xx,
watchdog, 3 PWMs, EPIT1/2). The SDIO CRC watch (BRINGUP item 11) shows 0 events this boot.

| Line | Cause | Action |
|---|---|---|
| `evbug: Event. Dev: input0 ...` (every switch transition) | `INPUT_EVBUG=m` autoloaded | Remove (1.1) |
| `SPI driver glowforge_pic has no spi_device_id for glowforge,pic` | Missing id table in glowforge.ko | Add table (1.6) |
| `imx-sdma: external firmware not found, using ROM firmware` | Built-in driver, firmware on rootfs | Decided: ROM scripts stay, packages go (2.4) |
| `mxs_phy 20c9000.usbphy: supply phy-3p0 not found` x2, `usb_phy_generic usbphynop1/2: dummy supplies not allowed for exclusive requests` | USB PHY nodes enabled with no USB controller | Disable in DTS (1.5) |
| `imx-drm display-subsystem: [drm] Cannot find any crtc or sizes` + 4 `card1-crtcN` kthreads | `DRM_IMX=y` with no display | Remove DRM_IMX (4.1) |
| `Bluetooth: Core ver 2.22 ...`, `HCI UART protocol H4/LL registered` | `BT=y` | Remove (1.4) |
| `ALSA device list: No soundcards found.` | `SND=y` | Remove (1.4) |
| `usbcore: registered new interface driver r8152/lan78xx/asix/...` (13 lines) | USB net drivers built in, no USB | Remove (4.1) |
| `CAN device driver interface`, `can: raw/bcm/gw` | `CAN=y`, `CAN_FLEXCAN=y` | Remove (4.1) |
| `SCSI subsystem initialized`, `libata version 3.00 loaded`, `kworker/R-ata_sff` | `SCSI=y`, `ATA=y` | Remove (4.1) |
| `PCI: CLS 0 bytes, default 64`, `vgaarb: loaded` | `PCI=y`, `VGA_ARB=y` | Remove (4.1) |
| `jffs2: version 2.2. (NAND)`, `fuse: init`, `NFS: Registering the id_resolver`, `RPC: Registered ...` | JFFS2/FUSE/NFS built in | Remove (4.1) |
| `brd: module loaded` + 16 `ram0..15` in `/proc/partitions` | `BLK_DEV_RAM=y`, 16 x 64 MiB | Remove |
| `mxs-dma 110000.dma-controller: initialized` | APBH DMA (GPMI NAND) | Remove `MXS_DMA` |
| `hwmon hwmon1: temp1_input not attached to any thermal zone` | `THERMAL_OF` + lm75 without a zone | Cosmetic |
| `Unknown kernel command line parameters "board=glowforge"` | uEnv passes it for userspace | Cosmetic |
| `No ATAGs?` | `ATAGS=y` on a DT boot | Drop `ATAGS`/`ATAGS_PROC` |
| `snvs_rtc: setting system clock to 1970-01-01` | No RTC battery | Expected; ntpd sets time |
| `Fixed dependency cycle(s) with ...` (about 40 lines) | fw_devlink over the video-mux graph and the CSI muxes | Upstream noise, harmless |
| `gpio gpiochipN: Static allocation of GPIO base is deprecated` x7 | Upstream `gpio-mxc` | Harmless |
| `sdhci-esdhc-imx 2190000.mmc: card claims to support voltages below defined range` | WL18xx advertises 1.8 V, host is `no-1-8-v` | Harmless |
| `mmc1: host does not support reading read-only switch` | `broken-cd` SD slot | Harmless |
| `imx_media_common/imx6_media/...: module is from the staging directory` | imx-media lives in staging | Expected |
| `glowforge: loading out-of-tree module taints kernel` | Expected | None |

##### 4. Drivers that are configured and unnecessary

Evidence for "unnecessary": no node in `glowforge.dts` (and none in the factory 4.14 tree
either), no driver bound on the running board, and no consumer in forgectrl, gfhardware,
gfutilities, or grblHAL. The board's peripheral set is: UART1/2, eCSPI2 (PIC), I2C1/2/4,
uSDHC1 (WL1805 SDIO) / 2 (SD) / 3 (eMMC), PWM1/2/4, EPIT1/2, SDMA, MIPI CSI-2 + IPU CSI,
VPU (coda), GPU (etnaviv, used by forgectrl's surfaceless-EGL demosaic), CAAM (RNG),
OCOTP, SNVS RTC, WDOG1, tempmon, GPIO switches/leds, the glowforge nodes.

###### 4.1 Built in (`=y`), removable from `glowforge.cfg`
These are what the 9.1 MB zImage is made of. Grouped; each group is one `# CONFIG_X is not
set` cluster in the fragment (the defconfig sets them, the fragment must unset them).

| Group | Symbols (parents; children fall with them) | Note |
|---|---|---|
| USB (all) | `USB_SUPPORT`, `USB`, `USB_CHIPIDEA*`, `USB_EHCI_HCD`, `USB_GADGET` + `USB_CONFIGFS*`/`USB_F_*`, `USB_USBNET` + `USB_NET_*`, `USB_RTL8152`, `USB_LAN78XX`, `USB_STORAGE`, `USB_HID`, `USB_MXS_PHY`, `USB_ULPI_BUS`, `USB_ONBOARD_DEV`, `USB_ROLE_SWITCH`, `EXTCON_USB_GPIO`, `USB_PCI` | No USB controller on the board |
| Wired/other networking | `FEC`, `PHYLIB`/`MDIO_*`, `PTP_1588_CLOCK`, `PPS`, `NET_VENDOR_*` (58 gates), `CAN` + `CAN_FLEXCAN/RAW/BCM/GW`, `BT` + `BT_HCIUART*`, `SERIAL_DEV_BUS`, `CFG80211_WEXT`, `IPV6_SIT`, `IP_PNP` | No Ethernet, CAN, or BT |
| Storage buses | `SCSI` (+`SCSI_LOWLEVEL`), `ATA` (+`ATA_SFF`, `ATA_BMDMA`), `PCI` + `PCIE_DW_HOST` + `PCI_IMX6` + `PCI_MSI` + `PCIEASPM`, `MTD` (+`MTD_CFI*`, `MTD_RAW_NAND`, `MTD_NAND_GPMI_NAND`, `MTD_NAND_MXC`, `MTD_SPI_NOR`, `MTD_UBI`, `MTD_DATAFLASH`, `MTD_PHYSMAP`), `MXS_DMA`, `FSL_EDMA`, `IMX_WEIM`, `BLK_DEV_RAM` | eMMC/SD only; EIM pads are plain GPIOs here |
| Filesystems | `JFFS2_FS`, `UBIFS_FS`, `NFS_FS` (+SUNRPC), `FUSE_FS`, `AUTOFS_FS`, `EXT2_FS`, `EXT3_FS`, `QUOTA`, `ISO9660_FS`/`UDF_FS`/`MSDOS_FS` (modules), `BINFMT_MISC` | Keep `EXT4_FS`, `VFAT_FS` + `NLS_*` (SD cards), `TMPFS`, `CONFIGFS_FS` |
| Display | `DRM_IMX` (+`DRM_IMX_HDMI/LDB/PARALLEL_DISPLAY/TVE`), `DRM_DW_HDMI` (+CEC, AHB audio), `DRM_MSM` (Qualcomm; the whole `DRM_MSM_*` block), `DRM_MXSFB`, `DRM_PANEL_*`, `DRM_SII902X`, `DRM_TI_TFP410`, `DRM_I2C_NXP_TDA998X`, `DRM_LVDS_CODEC`, `DRM_FBDEV_EMULATION`, `FB`, `FRAMEBUFFER_CONSOLE`, `LOGO`, `VT` + `DUMMY_CONSOLE` + `CONSOLE_TRANSLATIONS`, `VGA_ARB`, `BACKLIGHT_CLASS_DEVICE`/`BACKLIGHT_GPIO`/`BACKLIGHT_PWM`, `LCD_CLASS_DEVICE`, `CEC_CORE`, `MEDIA_CEC_SUPPORT` | **Keep `DRM`, `DRM_ETNAVIV`, `DRM_ETNAVIV_THERMAL`, `IMX_IPUV3_CORE`** (GPU demosaic needs the etnaviv render node; the IPU core drives CSI capture). See 5.1 for the verification this needs. |
| Audio | `SOUND`, `SND`, `SND_SOC`, `SND_IMX_SOC`, `SND_SOC_FSL_SSI/SAI/ESAI/SPDIF/ASRC/AUDMUX/UTILS`, `SND_SOC_IMX_PCM_DMA/FIQ`, all codec drivers (`SGTL5000`, `WM8960`, `WM8962`, `WM8994`, `TLV320AIC23/31XX/3X`, `CS42XX8`, `ES8328`), `SND_SIMPLE_CARD`, `SND_SOC_HDMI_CODEC`, `SND_AC97_CODEC`, `SND_USB_AUDIO` | Plus `&asrc` disabled in the DTS |
| Input | `INPUT_TOUCHSCREEN` + 19 `TOUCHSCREEN_*`, `HID`/`HID_GENERIC`/`HID_MULTITOUCH`/`HID_WACOM`/`I2C_HID*`, `INPUT_MOUSE` (psmouse), `SERIO`/serport, `INPUT_MISC` + `INPUT_GPIO_BEEPER`, `INPUT_EVBUG`, `INPUT_LEDS`, `INPUT_MATRIXKMAP`, `RC_CORE`/`RC_DEVICES`/`IR_GPIO_CIR`/`VIDEO_IR_I2C` | **Keep `INPUT`, `INPUT_EVDEV`, `KEYBOARD_GPIO`** (`/dev/input/event0` = the switches) |
| PMIC / board-support for other boards | `MFD_DA9052_I2C`, `MFD_DA9062`, `MFD_DA9063` (+`da9063_wdt`), `MFD_MC13XXX*` (+`SENSORS_MC13783_ADC`, `TOUCHSCREEN_MC13783`), `MFD_RN5T618` (+`rn5t618_power`), `MFD_ROHM_BD71828` + `GPIO_BD71815` + `REGULATOR_ROHM`, `MFD_STMPE` (+gpio, ts), `MFD_SY7636A` (+`SENSORS_SY7636A`), `MFD_WM8994`, `GPIO_74X164`, `GPIO_MAX732X`, `GPIO_PCA953X`, `GPIO_PCF857X`, `GPIO_VF610`, `GPIO_SIOX`/`SIOX`, `REGULATOR_GPIO`, `POWER_SUPPLY`, `W1` (+`ds2482`, `w1_therm`), `I2C_GPIO`, `I2C_MUX_GPIO`, `I2C_ALGOPCA/PCF`, `I2C_SLAVE`, `SPI_GPIO`/`SPI_BITBANG`, `SPI_FSL_DSPI`, `SPI_FSL_QUADSPI`, `PWM_FSL_FTM`, `PWM_IMX_TPM`, `RTC_DRV_MXC`, `SENSORS_GPIO_FAN`, `SENSORS_PWM_FAN`, `SENSORS_IIO_HWMON`, `SENSORS_ISL29018`, `IIO_ST_SENSORS_SPI` (+`st_accel_spi`), `LEDS_PWM`, `LEDS_TRIGGER_*`, `IMX_IRQSTEER`, `IMX_GPCV2*`, `SERIAL_FSL_LPUART*`, `DMATEST`, `IRQ_IMX_MU_MSI`, `HW_RANDOM_IMX_RNGC`, `HW_RANDOM_MXC_RNGA`, `HW_RANDOM_OPTEE`, `HW_RANDOM_ARM_SMCCC_TRNG`, `CRYPTO_DEV_MXS_DCP`, `CRYPTO_DEV_SAHARA`, `TEE`/`OPTEE`, `ARM_PSCI*` | **Keep `REGULATOR_FIXED_VOLTAGE`, `REGULATOR_ANATOP`, `GPIO_MXC`, `GPIO_CDEV`, `GPIO_SYSFS`, `LEDS_GPIO`, `LEDS_CLASS`, `I2C_IMX`, `I2C_CHARDEV`, `SPI_IMX`, `PWM_IMX27`, `RTC_DRV_SNVS`, `NVMEM_IMX_OCOTP`, `NVMEM_SNVS_LPGPR`, `CRYPTO_DEV_FSL_CAAM*`, `SENSORS_LM75`, `IIO_ST_SENSORS_CORE`/`I2C`, `IMX_THERMAL`, `CPU_THERMAL`** |
| Other SoCs | `SOC_IMX31/35/50/51/53/6SL/6SLL/6SX/6UL/7D/7ULP/8M`, `PINCTRL_IMX35/50/51/53/6SL/6SLL/6SX/6UL/7D/7ULP/8MM/8MN/8MP/8MQ`, `PINCTRL_VF610` | **Keep `SOC_IMX6Q`, `PINCTRL_IMX6Q`, `MXC_CLK`, `CLKSRC_IMX_GPT`, `IMX2_WDT`, `ARM_IMX6Q_CPUFREQ`** |
| Media (non-camera) | `MEDIA_ANALOG_TV_SUPPORT`, `MEDIA_DIGITAL_TV_SUPPORT`, `MEDIA_RADIO_SUPPORT`, `MEDIA_SDR_SUPPORT`, `MEDIA_TEST_SUPPORT`, `MEDIA_USB_SUPPORT`, `DVB_CORE`, `VIDEO_IMX_PXP` (the 6DL PXP node's compatible is not one this driver matches; unbound), `VIDEO_OV2680`/`OV5640`/`OV5645`/`ADV7180`, `USB_VIDEO_CLASS` | **Keep `MEDIA_SUPPORT`, `MEDIA_CAMERA_SUPPORT`, `MEDIA_PLATFORM_SUPPORT`, `MEDIA_CONTROLLER`, `VIDEO_DEV`, `VIDEO_V4L2_SUBDEV_API`, `V4L_PLATFORM_DRIVERS`, `V4L_MEM2MEM_DRIVERS`, `VIDEO_CODA`, `VIDEO_IMX_VDOA`, `VIDEO_IMX_MEDIA`, `VIDEO_MUX`, `VIDEO_OV5648`, `VIDEO_OV8856`, `STAGING_MEDIA`**. The single switch `CONFIG_MEDIA_SUPPORT_FILTER=y` (then enable only CAMERA + PLATFORM) is what removes the DVB/tuner tree (4.2). |
| Debug / misc | `KEXEC`, `CRASH_DUMP`, `PROC_VMCORE`, `ATAGS` + `ATAGS_PROC`, `SUSPEND`/`PM_SLEEP`/`PM_TEST_SUSPEND`/`PM_DEBUG` (no suspend use on a laser), `SWAP`, `CPU_FREQ_GOV_CONSERVATIVE/POWERSAVE/USERSPACE`, `IOSCHED_BFQ`, `MQ_IOSCHED_KYBER`, `CONNECTOR`/`PROC_EVENTS`, `RD_BZIP2/LZ4/LZMA/LZO/XZ/ZSTD` (no initrd), `HIGHMEM` (512 MB fits lowmem; dmesg: `HighMem empty`) | `DEBUG_FS`, `DEVMEM`, `MAGIC_SYSRQ`, `KPROBES`, `PERF_EVENTS`, `IKCONFIG_PROC` are bench tools; keep on the dev image at least |

###### 4.2 Modules shipped and never used
`imx-base.inc` sets `MACHINE_EXTRA_RRECOMMENDS = "kernel-modules"`, so every module built
lands on the rootfs: 253 modules, 9.7 MB, in the release image as well (its manifest lists
254 `kernel-module-*` packages). The board loads 28; 27 are needed (`wl12xx` is not).
Breakdown of the dead weight on the board:

| Group | Modules | Size | Why they exist |
|---|---|---|---|
| DVB frontends + tuners | 153 | 3.1 MB | `MEDIA_SUPPORT_FILTER` off + `MEDIA_SUBDRV_AUTOSELECT` off makes every frontend `default m` |
| Non-TI Wi-Fi (`ath10k`, `brcmfmac`, `mwifiex`, `wl12xx`) | 13 | 1.4 MB | `WLAN_VENDOR_*` gates + defconfig |
| USB (gadget legacy, serial, `cdc-acm`, `usbtest`, `ehset`, `uvcvideo`, `snd-usb-audio`, USB net) | 20 | 1.2 MB | No USB |
| Other (`psmouse`, `serport`, `gpio-beeper`, `w1`, `siox`, `dmatest`, `evbug`, `bnep`, `udf`/`isofs`/`msdos`, `binfmt_misc`, `da9063_wdt`, `rn5t618_power`, `lvds-codec`, `dw-hdmi-ahb-audio`, `qcaspi`, `ov2680/ov5640/ov5645/adv7180`, `cxd2880-spi`, `irq-imx-mu-msi`, `st_accel_spi`, `i2c-algo-pca/pcf`, `nls_iso8859-15`) | ~40 | 1.3 MB | Defconfig |

Two independent fixes: (1) unset the symbols so the modules are not built (4.1 plus
`MEDIA_SUPPORT_FILTER=y`); (2) replace the blanket `kernel-modules` recommendation in
`glowforge.conf` with the explicit list (`kernel-module-glowforge`, the `wlcore`/`wl18xx`/
`mac80211`/`cfg80211`/`ccm`/`ctr`/`gcm`/`ghash`/`libarc4` set, `ov5648`, `ov8856`,
`video-mux`, `mux-core/gpio/mmio`, `imx-media-common`, `imx6-media`, `imx6-media-csi`,
`imx6-mipi-csi2`, `coda-vpu`, `v4l2-jpeg`, `imx-vdoa`, `lm75`, `st-accel`/`st-accel-i2c`/
`st-sensors`/`st-sensors-i2c`). (2) alone already shrinks the release rootfs by about
8 MB against the 200 MiB slot; (1) is what shrinks the kernel and the build.

###### 4.3 Firmware packages (adjacent, same mechanism)
`MACHINE_FIRMWARE` in `imx-base.inc` adds, for `mx6dl-generic-bsp` and `imx-mainline-bsp`:
`firmware-imx-epdc` (5.0 MB of e-paper controller firmware; no EPDC on this board),
`firmware-imx-vpu-imx6q` (the 6DL uses `vpu_fw_imx6d.bin`), `linux-firmware-imx-sdma-imx7d`
(wrong SoC), plus `linux-firmware-imx-sdma-imx6q` (never loads; both SDMA packages are
decided out, 2.4). `/lib/firmware` is 7.7 MB; about 5.5 MB of it has no consumer. `linux-firmware-wl18xx` carries four `wl18xx-fw*`
variants; this board (PG 2.2) boots `wl18xx-fw-4.bin`. Keep all four unless every board
is known to be PG 2.2. The `TIInit_*.bts` files are BT init scripts (no BT).

##### 5. Considerations (not defects; decide, then measure)

###### 5.1 `DRM_IMX` removal must be verified against the GPU demosaic
forgectrl's `gpu_debayer.c` opens EGL through `EGL_PLATFORM_SURFACELESS_MESA`, which
enumerates render nodes (`/dev/dri/renderD128`, etnaviv). It does not need `card1`
(imx-drm). After removing `DRM_IMX`, confirm on the bench that `/dev/dri/renderD128` still
exists and forgectrl logs the GPU path as active (the `gpu:` lines) rather than falling back
to NEON. Mesa's `PACKAGECONFIG:pn-mesa = "... gallium etnaviv"` is unaffected.

###### 5.2 `SMP=y` on a single core
`CONFIG_SMP=y`, `NR_CPUS=4`, one CPU brought up. Every spinlock and per-CPU path pays the
SMP cost for nothing. `CONFIG_SMP=n` (the multi-platform build allows it) removes that and
the seven IPI vectors. Worth measuring against BRINGUP item 16 (producer stalls); it is
not a correctness issue.

###### 5.3 Kernel-side latency knobs that are already right
`PREEMPT=y`, `HZ=100` with `NO_HZ_IDLE` and `HIGH_RES_TIMERS`, `imx6q_cpuidle` (WFI + WAIT,
50 us exit), `RCU_PREEMPT`, no `DEBUG_PREEMPT`/lock debugging, `DEBUG_INFO_NONE`,
`INIT_STACK_ALL_ZERO` (small cost, fine). `sched_rt_runtime_us=950000` is the default RT
throttle; a `SCHED_FIFO` feeder that ever runs a full 950 ms without sleeping is throttled
for 50 ms. The feeder sleeps, so this is a note, not a finding.

###### 5.4 Watchdog
`IMX2_WDT=y` + `WATCHDOG_HANDLE_BOOT_ENABLED=y`: the kernel adopts U-Boot's 60 s watchdog
and keeps it fed while `/dev/watchdog` stays closed (nothing opens it, by design per the
image recipe). Consistent with the fragment. A hung userspace does not reset the machine;
that is the documented decision.

###### 5.5 Tracing and BPF
`FTRACE`-family symbols are absent from the config (no function tracer), but `BPF_SYSCALL`,
`KPROBES`, `RCU_TRACE`, `TASKS_TRACE_RCU`, `PERF_EVENTS` are on. Keep on the dev image
(latency work), consider off for release.

###### 5.6 Documentation drift noticed on the way (kas/README.md #2)
The README says the PWM prescaler port is "obsolete", that the PIC SPI delay is a
"hardware-bring-up TODO", and that `reg-userspace-consumer` is enabled via `glowforge.cfg`.
The bbappend carries patch 0009 (`fsl,extra-prescale = <13>` on `&pwm2`) and patch 0004
(the PERIODREG delay), and the fragment has no userspace-consumer line (the DTS dropped
the node). The bbappend header is current; the README paragraph is not.

##### 6. What to keep (the board's real driver set)

Built in: `IMX_SDMA`, `MXC_EPIT_API`, `PREEMPT`, `PANIC_ON_OOPS`, `IMX2_WDT`, `CMA`/`DMA_CMA`,
`SERIAL_IMX` (+console), `MMC_SDHCI_ESDHC_IMX`, `MMC_BLOCK`, `I2C_IMX`, `I2C_CHARDEV`,
`SPI_IMX`, `PWM_IMX27`, `GPIO_MXC`, `GPIO_CDEV`, `GPIO_SYSFS`, `PINCTRL_IMX6Q`, `SOC_IMX6Q`,
`KEYBOARD_GPIO`, `INPUT_EVDEV`, `LEDS_GPIO`, `REGULATOR_FIXED_VOLTAGE`, `REGULATOR_ANATOP`,
`IMX_THERMAL`, `CPU_THERMAL`, `ARM_IMX6Q_CPUFREQ` (+`ondemand`, `performance`), `CPU_IDLE`,
`RTC_DRV_SNVS`, `NVMEM_IMX_OCOTP`, `IMX_IPUV3_CORE`, `DRM` + `DRM_ETNAVIV`,
`CRYPTO_DEV_FSL_CAAM` (RNG, `hwrng` thread), `IIO` + triggered buffer, `HWMON`, `WATCHDOG`,
`EXT4_FS`, `VFAT_FS` + `NLS_*`, `TMPFS`, `DEVTMPFS`, `CONFIGFS_FS`, `IKCONFIG_PROC`,
`INET`/`UNIX`/`PACKET`, `IPV6` (until `auth.c` says otherwise), `RFKILL` (wpa_supplicant),
`WIRELESS`/`WLAN`/`WLAN_VENDOR_TI`, `MEDIA_SUPPORT` + camera/platform, `STAGING_MEDIA`,
`SRAM`, `MXC_CLK`, `CLKSRC_IMX_GPT`, `HAVE_ARM_TWD`, `IMX_GPC` + PM domains (`vddpu`).

Modules (27): `glowforge`, `wl18xx`, `wlcore`, `wlcore_sdio`, `mac80211`, `cfg80211`,
`libarc4`, `ccm`, `ctr` (+`gcm`, `ghash` for WPA3/GCMP), `ov5648`, `ov8856`, `video_mux`,
`mux_core`, `mux_gpio`, `mux_mmio`, `imx_media_common`, `imx6_media`, `imx6_media_csi`,
`imx6_mipi_csi2`, `coda_vpu`, `v4l2_jpeg`, `imx_vdoa`, `lm75`, `st_accel`, `st_accel_i2c`,
`st_sensors`, `st_sensors_i2c`.

##### 7. Suggested order, if acted on

1. `glowforge.cfg`: unset `INPUT_EVBUG`; add `DETECT_HUNG_TASK` + `SOFTLOCKUP_DETECTOR`;
   add `PSTORE`/`PSTORE_RAM`/`PSTORE_CONSOLE` (+ ramoops node in the DTS); set
   `PANIC_TIMEOUT=10`; write `MULTIPLEXER`/`MUX_GPIO`/`MUX_MMIO` as `=m`. (Safety and
   diagnostics first.)
2. `glowforge.conf`: replace the `kernel-modules` recommendation with the explicit module
   list; drop `firmware-imx-epdc`, `firmware-imx-vpu-imx6q`, `linux-firmware-imx-sdma-imx6q`,
   and `linux-firmware-imx-sdma-imx7d` from `MACHINE_FIRMWARE` (2.4, decided).
3. `glowforge.cfg`: the removal clusters in 4.1 with `MEDIA_SUPPORT_FILTER=y`; `glowforge.dts`:
   disable `&asrc` and the USB PHY nodes, and drop `dmas`/`dma-names` from `&ecspi2` (2.4).
4. `kernel-module-glowforge`: `spi_device_id` table for `glowforge,pic`.
5. Bench: fresh-boot `dmesg` diff, `/proc/sys/kernel/*_panic` present, `/sys/fs/pstore`
   mounts, `renderD128` present and forgectrl on the GPU path, cameras stream, Wi-Fi up,
   then the acceptance catalog.

All of 1 through 4 ride one image flash (kernel/BSP changes batch), and every item here is
a platform change under the acceptance model.

### Arm skipped on a stale spindle state (item 20), closed 2026-08-29

Closed: the arm is decided by the window alone (grblHAL-glowforge a7dcdca), an RX overrun drops the overrunning line whole and stops the job, both on image 20260829190323; proven by `tests/laser_arm_test.c` case H, `tests/serial_test.c`, the null-sink scenarios `sender-change-mid-job` and `rx-overrun`, and the bench drills `senderchg` and `overrun` (the 2026-08-29 entries above). The catalog's `laser.*` covers name `grblhal-glowforge/src/**`, which holds both files. The mid-job sender-change discussion is its own item.
20. **Arm skipped on a stale spindle state (safety, fix before the next
    image).** `glowforge_laser.c` arms on the first laser-on of a job only
    while its own record of the spindle state reads off
    (`state.on && !cur.on && !laser_ok` in `spindleSetState`), and
    `gflaser_disarm` does not clear that record. A job whose M5 never
    executes leaves the record on, and the next job's M3 runs with no arm:
    no button wait, no run report to forgectrl, no run airflow. Fire stays
    suppressed at the stream, so no energy leaves the tube, but the head
    runs the whole job without the operator's consent. Seen on the bench:
    a sender wrote a 93-line job at once, the RX ring (1023 bytes)
    overflowed, and the serial layer drops bytes on a full ring (`serial.c`,
    the overflow flag is set and never read), so the job's M5 and M2 were
    lost, the window stayed open until the sender disconnected, and the
    following job ran unarmed. Owed: the driver fix on an image (the arm
    decided by the window alone, an RX overrun dropping the overrunning
    line whole and aborting the job), one bench drill of each scenario,
    and the catalog's `covers` widened to `glowforge_laser.c` and
    `serial.c`.


### The dose curves judged on material, and the model switch built, 2026-08-30

Bench, dev image 20260829220329, three `dpatch` depth-witness runs, each two rows of 30 x 4 mm serpentine fills (row A CW at six feeds for relative doses 1.0 to 0.25, row B at the reference feed at 100 / 80 / 60 / 45 / 30 % of the model's range), the operator matching each row-B patch to the row-A patch of equal depth by eye:

- Density on Thick Draftboard at F1500 (`bench-data/dpatch_20260830-172227.json`) and on acrylic at F1125 (`dpatch_20260830-173933.json`): the operator's verdict on both, "sensor predictions are accurate", B8 (80 % density) = A4 (CW at half the dose), B9 = A5, B10 = A6, B7 = A1. Thermopile 0.47 / 0.35 / 0.19 / 0.07 of CW at 80 / 60 / 45 / 30 % (0.45 / 0.34 / 0.19 / 0.06 on acrylic), tube current 0.56 / 0.45 / 0.39 / 0.35. Row A flat within 17 % and 16 %. The density curve is the tube's, not the sensor's, and the pending question from 2026-08-25 (B8 = A4 or A2) is closed on A4. By eye: every box deeper at both ends (the reversal slow-down under constant power), a slight line pattern from the 0.3 mm serpentine, no dot pattern.
- Analog on acrylic at F1125 (`dpatch_20260830-175759.json`), the drill extended to run under `laser_power_model = analog` (forgefirm f88c778): "they match the sensors", B8 (80 % duty) = A2, B9 between A2 and A3, B10 = A4, B11 = A5; light 0.82 / 0.68 / 0.54 / 0.37 of CW at 80 / 60 / 45 / 30 % duty, current 0.80 / 0.61 / 0.46 / 0.31. The 2026-08-25 ladder's prior (0.72 / 0.52 / 0.37 / 0.07) was low, worst at 30 % duty where its F600 lines sat in the unstable discharge band. The thermopile drifted inside this run (row A 1733 down to 1290, baseline 1835 up to 1896), so the ratios carry more uncertainty than the density runs.
- Finish, the two acrylic runs side by side: "finish is the same"; the only visible pattern is present at every power, under CW and under 100 % density (continuous fire) alike, so it is mechanical, not the dither. Operator's decision: the analog model stays and is developed as a second, near-linear model.

Each run: `cool_flow_recheck_s = 600` for the run and removed after, the `dpatch` record copied to the tree, `/tmp` cleaned, `laser_power_model` put back to `density`, the machine idle and disarmed.

Built the same day, host-proven, no fire: the dose-model switch. `M101 P0` (analog) / `M101 P1` (density) as a driver M-code, refused with the spindle commanded on (error 253, reason reported) or the controller not idle, program-scoped with `Q1` to stick; the per-model floors as config keys (`laser_floor_density` 10, `laser_floor_analog` 16) loaded into `$35` in RAM at every arm and switch with the PWM mapping re-precomputed, the stored setting never written; the stream leading the first run after a switch dark; the cooling report carrying the model in force (`model=` on `POST /cool/state`, the engine preferring it to the config key for the tube-heat share); the five `laser_*` keys in forgectrl's settings whitelist and the panel's GRBL tab with help text; `laser.power-floor` made model-aware and the new catalog test `laser.power-model-switch`. Proof: `tests/laser_arm_test.c` cases J to O (derived floors, validate and execute refusals, switch, revert, Q1, reset), `laser_stream_test.py` rules 18 to 21 (a typed `$35` overwritten at the arm; both switch directions rendering exactly at the boundary with no continuous FIRE at full duty across it; the refusal leaving the stream unchanged; `M2` reverting and `Q1` holding), forgectrl's host tests. Found on the way: the core skips every G-code line after an error until the sender resyncs with an empty line or a `$` command (`protocol.c`), which is how the harness now follows a refused switch. One flake, not a defect: rule 13's mask comparison slipped one tick at the tail while a build ran alongside in the same VM (the shipper is wall-paced); three runs alone were identical.

Built the same evening, host-proven: the controller's published state (the C6 gap, shaped as files by the operator's decision rather than an HTTP push). The controller writes `grbl.settings` and `grbl.state` under `/run/forgefirm` atomically on edges plus a heartbeat; forgectrl echoes the state file in `/status` as the `grbl` block only while its supervisor holds a live GRBL controller, serves the settings file at `GET /grbl/settings`, and the panel's GRBL card renders the sender session, machine state, laser window with its dose model, and the modal report. Proof: the new `status_grbl_test` (fresh, torn, stale and dead-controller cases), the lifecycle harness's state-files scenario (the files follow connect, arm, `M101`, the `M2` revert and a reconnect's generation bump; found on the way - an arm or `M101` moves `$35` in RAM with no settings-changed event, so the publisher watches the value), the full forgectrl host-test sweep, the stream harness, 252 forgetest unit tests and the coverage lint. Not yet on the bench: the board still runs the pre-C6 hot-deployed binaries, so the panel card reads "no report" until the next deploy or image.

Bench-proven the same day, one armed run of the new `mswitch` drill on the hot-installed cross-built binaries (forgectrl md5 4dc4677a, grblHAL_glowforge 17e5502f, operator-run install): the arm reported "laser armed (density, floor 10 %)", `M5` then `M101 P0` answered ok and reported "laser power model set for this program (analog, floor 16 %)", `$$` read `$35=16` while analog was in force and `$35=10` again after `M2` reported the revert, the armed window carried across the switch with no re-prompt, and the 25 Hz trace showed exactly two discharge segments, the density line pulsed (hv mean 390, max-mean 547) and the analog line steady (mean 567, max-mean 28), dark after the second `M5` (hv max 0). Board cleaned; the hot-deployed binaries stay until the next flash, `/tmp/*.prev` is the rollback.

### M4 into corners under both models, no dropouts, 2026-08-30

Image 20260830200842 (fresh flash; the C6 state files verified live on first boot, the sender field exercised end to end). The `m4corner` drill, one armed run: a corner-heavy pattern (10 mm line, 1.2 mm teeth, 2 mm square, 5 mm reversal, 0.5 mm teeth) at S300 F2000 under M4, cut under density and again under analog via `M101 P0`, passes offset 8 mm (`bench-data/m4corner_20260830.log`). Operator: no unmarked commanded segment on either pass - the derived floors close the analog dead-band dropout, and density cannot reach one. Artifacts as the curves predicted: analog scorches the line start and the square corners (floor 16 plus M4 dwell) and reads ~0.35 of CW at the same commanded 30 % where density reads ~0.12-0.15 and runs faint; the operator accepts density's weakness pending E4 (the per-model S correction). The M101 switch, its report, the M2 revert and two clean discharge windows all held on the shipped image. Items A3 and B4 of the working file close.

### The density time base across feeds: even all the way, 2026-08-30

The `m4feeds` drill on image 20260830200842, one armed run: a 60 mm U per feed (out and return legs 0.6 mm apart, the turn at the far end) at S1000 under M4 density, F1000 and F4000, passes 5 mm apart (`bench-data/m4feeds_S1000_20260830.log`; a first 20 mm retraced attempt was unreadable, `m4feeds_20260830.log`). At cruise the beam read identically at both feeds (thermopile 3202 / 3184, current 961 / 895), as M4 commands; the operator read the material "even all the way" - end to middle and through the turns, both feeds. B3 of the working file closes: M4's velocity scaling holds dose per mm through the accel, the per-tick time base stands, no per-step base is needed.

### The first rasters, and the decision that ends the analog mode, 2026-08-30

B5 on image 20260830200842, the generated grayscale wedge (`bench-data/gray-wedge.png`) from LightBurn in Image/Grayscale mode under density. Run 1, 254 DPI at 3000 mm/min (37 min, one kernel run of 23.5M callbacks): every band distinguishable, the ramp graduated, no pattern beyond the mechanical stepper signature, no dropout; the dark third saturates into char at 100 % layer power (dose, not modulation). One mid-job "fire suppressed: coolant" warning traced to a single stale-verdict beat under CPU starvation (the controller logged "1 late events clamped ... step generation was starved of CPU", min pacing margin 0.0 ms); the flow re-checks passed all job with the tube share off, the ceiling gates on the upstream sensor and never tripped, and nothing shows on the material. Run 2, 508 DPI at 6000 mm/min (39 min, 25.1M callbacks, min margin 13.3 ms, zero clamps, no suppression): tonality held at ~14 pulse slots per pixel - the dither accumulator's cross-pixel averaging recovered the levels - no coarsening, no moire. Witness photos `bench-data/raster_run1_254dpi_20260830.jpg` and `raster_run2_508dpi_20260830.jpg`.

The analog pass (A5) was cancelled by the operator's decision that ends the mode: analog fires a spot at every turn-on, low power included - the strike transient, seen at A1 as the start-of-line spots and at the corner drill as the scorch halo - and the finish comparison had already found no advantage (identical finish on acrylic). Density is the only product model from here; the analog rendering survives only as the host harness's conservatism reference (the mask rule), selectable only in the null-sink build.

### The dose curve, the corner rolloff, and the recorder's first live fit, 2026-08-31

Shipped across images 20260831003319 to 20260831021059 (pins forgectrl fac30a4, grblhal 06965b6): S commands a light fraction through `laser_dose_curve` (bench-default compiled in), `laser_corner_gamma` (default 2) bends the M4 rolloff so corners are starved rather than proportional - built after the first curved corner job over-burned, and reached driver-only after three instrumented probes showed the programmed S is unreachable from the compute path in laser mode (the driver now mirrors the parser's S into an atomic each poll pass) - and the dose-curve recorder streams its own ladder from one panel press, absolute from X0 Y0, gated on the published sender flag. Two defects found by their own gates on the way: forgectrl's -Werror CI caught a literal NUL byte a patching tool had written into a char constant, and the recorder's first live fit refused cleanly ("1 discharge segments, the ladder has 7 rungs") because the inter-rung rapids were darker than a second only - a G4 P2 dwell per rung fixed it. The status echo also gained a sub-second negative-age tolerance after a millisecond-rounding coin flip in its host test.

The first live recording on image 20260831021059 then fit all seven rungs and the operator applied it: this machine's own curve is 10:0.52, 20:2.63, 30:8.18, 45:23.15, 60:40.66, 80:54.76, 100:100 - within a few points of the bench-default at every rung, the 80 percent rung reading 55 percent of full light where the depth witnesses had said about half. The floor key was restored by the recorder as designed. E4 and E5 of the working file are bench-proven; the corner look at the default rolloff is the file's last open item.

### The laser power model closes; the working file retires, 2026-08-31

The corner look on image 20260831021059 with the machine's own recorded curve: the operator lowered the corner rolloff from the default 2 to 1.5 and expects to go lower - the knob works, its right value is per machine, and the commissioning item gains a side-by-side chooser (the same pattern cut at several settings, the operator picks by eye, Apply writes the winner) as the tool for it. With that, every question the tree-root working file `LASER_DUTY_WORK.md` held is answered or homed: the model decision (density only), the measured curves, the floors, the switch's rise and removal, the rolloff, the recorder, and the raster proofs. Its conclusions live in BRINGUP's "Laser control (GRBL mode)" and the facts bank, its dated record in this log, and the file is deleted per its own charter. BRINGUP's "Next work" item 16 (the laser power model) closes and the later items renumber down by one (17 to 23 become 16 to 22).

### Rail policy (item 8 bullet), closed 2026-08-31

Closed: grblHAL-glowforge fa9ed78 writes `cnc/enable` only standalone;
SERVICES.md "Rail policy" is `[implemented]`. The entry above has the proof.

   - **Rail policy** (the one `[contract]` item left in SERVICES.md). The GRBL
     driver still writes `cnc/enable` at init and at homing resume —
     idempotent, since the rail is already up, so this is tidiness rather than
     a bounce source.

### `/cool/status` cosmetics (item 8 bullet), closed 2026-08-31

Closed: forgectrl 0e907f7, `armed` from a fresh report only and a
zero-length smoke phase for a session that never armed. The hunt keeps
reporting `run` by design. The entry above has the proof.

   - **`/cool/status` cosmetics.** The endpoint echoes the last reported `armed`
     flag even when that report is stale (`report_age_s` tells the truth), and a
     gfcloud homing session reports every motion as a job, so the engine cycles
     run → smoke → idle per motion. Both are silent and safe.

### The armed-kill core question (item 12), closed 2026-08-31

Closed: `laser.armed-kill` stays in its domain, recorded in the site's
Acceptance page (forgefirm-docs 657d32e).

    whether `laser.armed-kill` belongs in the always-required core rather
    than its domain is still an open call (the core carries the emission
    witness).

### Image trims not taken (item 18), closed 2026-08-31

Closed: both reductions landed in forgefirm b334c4c and meta-openglow
722bc00 and are on release 20260831130656 (the entry above has the
manifest). The five unshipped helper modules and the debug features
stay as decided there.

18. **Image trims not taken.** Two rootfs reductions the kernel review left
    on the table, each wanting a check before it lands. The `python3`
    meta-package installs `python3-modules` (tkinter, idle, 2to3, pydoc,
    ensurepip, venv, the debugger, doctest, asyncio, multiprocessing,
    xmlrpc: ~10 MB) where the apps declare `python3-core` and a few modules,
    so replacing it with the explicit set needs an import audit of gfcloud,
    gfhome, gfhardware and gfutilities (the cloud tests are the check).
    `libgnutls30`, `libunistring5`, `nettle` and `libgmp10` (~4.9 MB) sit on
    the rootfs with no package depending on them and no binary linking them;
    a `PACKAGE_EXCLUDE` experiment on a build would name the holder if there
    is one. Five helper modules are built and not shipped (`crc7`,
    `crc-ccitt`, `libcrc32c`, `st-accel-spi`, `st-sensors-spi`: 0.1 MB,
    harmless). The debug features stay in the release kernel by decision
    (`KPROBES`, `PERF_EVENTS`, `BPF_SYSCALL`, `DEBUG_FS`, `DEVMEM`,
    `MAGIC_SYSRQ`: no runtime cost unused, root-only exposure, and root can
    load modules anyway).

### Laser commissioning leftovers (item 1), retired 2026-08-31

Closed: the gap question is answered from the safing chain (the entry
above); the confirmation rides `laser.emission-witness`; the flow-band
sentence is in the facts bank. Items 2 to 21 are now 1 to 20.

1. **Laser commissioning leftovers.** Verify the hardware button latch persists
   across kernel-run gaps mid-job (if OK_2_FIRE drops between motion bursts, the
   fix is a stream keepalive across armed gaps). The flow check's bands
   hold from 19 to 27 C, the loop heater's ceiling in a 20 C room, with the
   margin widening warm; above that only a running tube warms the loop,
   and the check takes the tube's share off.

### Low-temperature gates and warm-up (item 1), closed 2026-08-31

Closed: implemented and bench-proven the same day (the entry above);
the catalog case is `cooling.floor-and-warm-up`. Items 2 to 20 are now
1 to 19.

1. **Low-temperature gates and warm-up (planned).** Two keys in the Cooling
   card: `cool_temp_min` (hard floor, default ~5 °C, a fire gate) and
   `cool_temp_start` (warm-up gate, default ~16 °C) — a job starting below the
   gate holds in a factory-style warm-up phase with the loop heater on and
   releases above it; below the floor nothing fires. Rationale: cold-tube
   thermal shock, condensation when the TEC pulls below the dew point, frozen
   coolant. Sequencing: warm-up first, flow check after. Measured physics on
   this bench: 50 % duty warms the bulk ~0.5–0.8 °C/min and plateaus ~8–9 °C
   above ambient — the same unaided limit the factory has.

### TEC handling (item 1), closed 2026-08-31

Closed: implemented and bench-proven at the GPIO the same day (the
entry above, with the CMet/CMdt correction); the catalog case is
`cooling.tec-drive`. Items 2 to 19 are now 1 to 18.

1. **TEC handling (planned).** `thermal/tec_on` is a bare on/off output with no
   readback, so presence cannot be detected: it becomes a `tec_present` user
   setting (Machine tab, default off; ForgeFIRM never drives `tec_on` unless
   set), which also covers retrofits. Operation when present: simple hysteresis
   while a job runs — TEC on above `cool_tec_on_c`, off below `cool_tec_off_c`,
   defaults from the factory setpoints (CMet/CMdt 18134/18364 mdeg — the same
   WTub/WTvb raw-754/751 pair that proved the thermistor curve), off at idle —
   with `cool_temp_min` as the chill floor, so the TEC can never drive the loop
   toward condensation or freeze territory. Whether a given unit has a TEC at
   all is a spec-level claim (Glowforge ships it on the Pro; Basic/Plus use the
   same passive closed-loop cooling), not teardown-verified per unit — another
   reason it is a setting.

### Fire watch (lid IR) redesign (item 1), closed 2026-08-31

Closed: armed in the factory's shape with knobs, bench-proven with the
lamp as the flame stand-in (the entry above). Items 2 to 18 are now 1
to 17.

1. **Fire watch (lid IR) redesign.** The gate stays disabled
   (`cool_fire_ir_delta = 0`) until it is lamp-aware: the engine must own or
   observe the lamp level (suspend the watch and re-baseline for a few ticks
   after any `lid_led` change) and the threshold must be relative to the
   lamp-set level, not a fixed count. Even then the signal is weak — a candle
   reads like a cut — so the head camera or a real flame sensor is the honest
   path to fire detection that means something.

   One lead worth a bench hour before building anything. The cloud ships flame
   thresholds in every pulse header, and the numbers do not look lamp-naive:
   baseline 3 counts on all four channels, alert at 275 and critical at 688 on
   the first quartile, 374 and 1022 on the second, with the third and fourth
   left at zero. The lamp response (facts bank) puts a fully lit lamp at 161
   to 177 counts on every channel and the dark floor at 2, so the factory's
   alert sits above the lamp and its baseline matches the floor: the factory
   rides out the lamp by choosing thresholds above it rather than by tracking
   it, and the watch could be re-armed on fixed numbers after all. Still
   unproven: that the header's quartiles map onto the raw channels and share
   their units (all four channels behave alike, while the header leaves the
   third and fourth quartiles at zero). Confirm against the header the next
   cloud job carries. By decision those header thresholds (`IR??`) are the
   prior for this redesign and nothing else: the cloud client declares them
   ignored, and the watch stays disabled until it is lamp-aware.

### Cloud mode (item 3), closed 2026-08-31

Closed as a status recitation: the content is present-state fact that
lives in `CLOUD.md` (the envelope, the guards, the actions, the
declined set), and the one open question, whether the service accepts
an 8 MP machine's larger images, stays in `CLOUD.md` "Outstanding
items" and rides the 8 MP first light (the cameras item). Items 4 and
up move down one.

3. **Cloud mode.** A print is no longer capped by the ring: the client holds
   the compressed body, fills the ring before the button, and tops it up as it
   plays, with the body bounded by `pulse_reject_threshold_bytes` because
   memory is what that costs. A feed that wedges is caught by progress rather
   than by ring depth (a healthy feeder keeps the ring brim-full, so depth
   only falls an hour after the feed died): thirty seconds of no progress with
   room in the ring stops the job cleanly and retraces, and it resumes if the
   feed moves again. A running print also reports itself to the app again, on
   the carrier a factory-session capture settled: the `type:"progress"` frame
   that is the periodic settings report, every 30 s and at every phase change,
   divided by the job's own length rather than by the kernel byte counter that
   climbs all job long under a live feed. The `cloud.*` acceptance tests
   cover all of it on the bench, a print longer than the ring fed from the
   live service included, and the app has been watched reporting a print's
   progress. `gfcloud.init` autostart with `controller_mode = cloud` is
   validated on a flashed image, and the lid flash follows the action's
   `LCfl`. What is left is tracked in
   `python3-gfhardware/forgefirm-app/docs/CLOUD.md` "Outstanding items" and
   is short: whether the service accepts an 8 MP machine's larger images (no
   HD machine has been on the bench). The pulse header's envelope is
   settled: every tag the service fills in is applied, passed through as a
   limit that can only tighten, refused on, logged or declared ignored with
   its reason (`CLOUD.md` "The pulse header"), and the gates behind it live
   in the cooling engine so they hold in GRBL mode too. The memory guards
   (`pulse_reject_threshold_bytes`, 128 MiB of compressed body) stay
   reasoned rather than measured, by decision: nothing the service sends
   comes near them, and every job logs the body and program sizes the
   guards are reasoned from. The lifecycle keys (`CFrh`, `CCwp`, `CCrp`,
   `CCup`) are settled as inert, in the factory too, so the configured
   warm-up and rest on the factory's measured timings are the model, and
   `CCbp`/`CCbt` are report-only tags that cannot appear in a header. The
   four actions the service has never been seen to send were read out of
   the factory binary: `user_image` is a lid capture and is implemented;
   `update_check`, `factory_reset` and `head_firmware_update` each hand off
   to a program this machine does not have (a factory updater, a reset
   script, a head firmware push), so each is answered on the wire and none
   is performed, and `focus` is ignored exactly as the factory ignores it.
   Declined outright: SPKI pinning, emulator full-session parity, and the
   factory's ten-event pause phase machine. Not inducible from the bench:
   the cancel-with-a-rejected-`settings`-action case, a malformed frame
   (needs a MITM), a body past the memory guard (the service has no such job
   to send), and a wedged feed (a healthy machine will not stall on request).

### Wi-Fi SDIO CRC watch (item 6), closed 2026-08-31

Closed: the persisted kernel logs hold 97 boots across 16 days
(2026-08-15 to 2026-08-31, the rotated file and the live one) with
zero `sdio ... failed` events - the only SDIO lines are the per-boot
card detect - against the pre-fix baseline of one event in 49 minutes.
The factory-exact uSDHC pads hold; the 25 MHz cap stays unneeded.
Items 7 and up move down one.

6. **Wi-Fi SDIO CRC watch.** The uSDHC pads now carry the factory-exact values
    and ship in every image. Watch `dmesg | grep -c "sdio .* failed"` across
    sessions (baseline: 1 event in 49 min of uptime). Effect if one lands
    mid-job: a 1–2 s sender stall — a cut-quality nuisance, never a safety
    matter. Only if it still recurs, cap the bus with
    `max-frequency = <25000000>` on `&usdhc1` (halves Wi-Fi throughput — last
    resort; the factory ran 50 MHz on these pads).

### Shared machine services, remaining polish (item 3), closed 2026-08-31

Closed: the diagnostics fold and the HTTP caps are implemented and
bench-proven, the arbitration is declined with its reasoning (the
entry above). Items 4 and up move down one.

3. **Shared machine services — remaining polish.** None of it blocking:
   - **Diagnostics as engine modes.** The flow tools still drive the thermal
     hardware themselves while the engine suspends its writes; the check
     parameters are already shared (`cool.h`), so what remains is folding the
     tools into the engine and retiring the suspend/resume dance.
   - **Busy-state arbitration under one lock.** The idle/busy gates (`POST
     /settings`, `/mode`, diagnostics start, upload/apply) each cross-check
     `machine_is_idle()` and `update_job_running()` at their own call sites.
     They fail closed and are drilled, but a single arbiter would close the
     remaining request-interleaving windows by construction.
   - **HTTP surface caps.** An explicit `MHD_OPTION_CONNECTION_LIMIT` plus a
     per-IP cap is the right hardening (a 500-connection flood plateaued at 379
     fds under the raised 4096 `RLIMIT_NOFILE`, no crash), and the camera
     `ensure_engine` `popen()`s should move out of the HTTP callback so a slow
     media-ctl cannot stall the request thread. Changing the MHD start flags
     touches the streaming model, so this wants a bench slot of its own.

### Physical-evidence negatives (item 3), closed 2026-08-31

Closed: the failed-head-capture negative and the K-11 badly-answering
head are both proven (the entries above); the STATE_FAULT-recovery note
was dropped by operator decision. Items 4 and up move down one.

3. **Physical-evidence negative still open.** A present head answering I²C
   badly (the K-11 runtime case) needs the head connected and the fault
   injected: flood the head's bus from userspace while the driver talks, one
   bench slot.

### Debug-kernel checks (item 3), closed 2026-08-31

Closed: both drills passed on the debug-kernel image (the entry above);
the variant and the drill tool are in the tree. Items 4 and up move
down one.

3. **Debug-kernel checks.** Run the module load/unload and forced
    `-EPROBE_DEFER` drills (`scripts/bench/debug_kernel_drills.py`) on the
    debug-kernel image (`kas/forgefirm-glowforge-debug.yml`, built beside the
    closing image). Both cycle the 40 V rail: a module unload powers it off (a
    stepper driver can come out of the power-up unserviceable), and the forced
    defer needs the 40 V regulator unbound under the probe. It is a bench slot
    with the rail-cycle gamble accepted, and it rides the closing burn.

## 2026-08-31: head MCU firmware decode - the accel IRQ, HEAD_IRQ arming, and the beam-detect chain

A read of the head MCU firmware (the KL17 at i2c-3 @0x47; disassembly
in `GF_Reverse/HEAD_PY`, cross-checked against the factory pinout
tables, the factory `head-board.sh`, and this project's DTS and head
driver) settles what drives the head IRQ and how the factory's head
crash detector works. It ties the two open next-work items together:
the accelerometer crash detector and the head IRQ source are two views
of one mechanism. The durable result is distilled into the BRINGUP
facts bank ("The head MCU flag register and HEAD_IRQ", "The head
accelerometer", "Beam detect in the head MCU"); the decode itself is
recorded here.

**How the KL17 assembles reg 0x05 and drives the head IRQ.** The MCU
is I2C-slave-only to the SoC (no I2C-master path is linked, so it never
touches the accelerometer). Once per main-loop pass it samples four
head-local GPIO input levels into the read-only flag register 0x05: b0
hall (pad PTE19), b1 the accelerometer INT pin (PTA1, a bare level, not
an I2C read and not computed), b2 the beam-detect comparator output
(PTE18), b3 a fourth, unidentified input (PTA19, pulled down; candidate
second hall or head-present). A fifth flag, b7, is the processed
beam-detect verdict (below). No GPIO pin interrupts are configured
anywhere in the firmware (`PORTA`/`PORTC_PORTD` vectors are the default
infinite loop); every input is level-polled. The outgoing head IRQ line
is the MCU's PTC2 output (our EV_SW head bit, GPIO3_22, an active-high
input at the SoC): it is level-driven and mirrors reg 0x02 (the latched
IRQ status) being nonzero. reg 0x02 latches edges on the reg-0x05 bits,
but only those the SoC arms through reg 0x03 (rising) and reg 0x04
(falling) edge-enable masks; reg 0x02 is read-to-clear. So the SoC
chooses which head events raise the IRQ, answers it by reading reg 0x02
to identify and clear, and reads reg 0x05 for live levels. ForgeFIRM
writes neither 0x03/0x04 nor reads 0x02, so the head IRQ is dormant by
construction, which is why the bench sees GPIO3_22 idle low with a
healthy head. (This corrects nothing measured earlier; it explains it.)

**The head accelerometer is a LIS2HH12 with a full on-chip interrupt
generator, and the factory arms it.** The part (i2c-3 @0x1e; the board
and lid accels are the same part at @0x1d and i2c-0 @0x1e) carries
per-axis 8-bit thresholds (IG_THS_X1/Y1/Z1, regs 0x32/0x33/0x34), a
duration counter (IG_DUR1 0x35), a per-axis event register
(IG_SRC1 0x31), full-scale +/-2/4/8 g (CTRL4 FS), and two independent
generators (IG1/IG2). The factory arms this generator from the pulse
header's HA* accel tags, which map bit-exactly onto its registers
(per-axis threshold to IG_THS, duration to IG_DUR1, decimator/ODR to
CTRL5, FIFO to CTRL3/FIFO_CTRL, full scale to CTRL4), and reads trips by
polling IG_SRC1 over the accel's own bus (the `head_accel_x/y/z_alert`
sources, from an 8-bit I2C register), running two tiers (alert pauses,
abort fails). The accel INT pin also wires to the KL17's PTA1, so the
same event surfaces coarsely as reg 0x05 b1. So the factory head crash
detector is the sensor's own interrupt generator, and the HA*
thresholds are LIS2HH12 register values at the full scale the HAsr tag
sets, not values in an unknown unit or behind an unknown filter. The
factory DTS does not declare the accel at all (its head I2C controller
is `status = "disabled"` with no children; the factory drove the accel,
the head MCU, and the LM75 from userspace over `/dev/i2c-2`), so the
factory too reaches the accel only over the bus, never as a host
interrupt.

**Beam detect, fully decoded (contrast, for the emission question).**
In the MCU: PTE16 to ADC0 gives reg 0x16 (raw analog level); a float
EWMA/CUSUM over the coefficients LAMBDA_K (0x07ae), LAMBDA_T
(0x1999 = 0.1), THETA_R (0x20), THETA_T (0x28) and E_T (0x60) feeds an
N-of-M sliding-window verdict in reg 0x05 b7. A DAC (reg 0x1e, default
0x3ff) sets an analog comparator threshold whose raw digital output is
reg 0x05 b2. Our head probe writes those five coefficients, but they are
the firmware's own power-on defaults. Our head driver exposes b2 (the
raw comparator) and reg 0x16 (the raw analog), but not b7 (the processed
verdict) - a gap if the emission question is ever pursued. `0xc9 <- 0x5a`
is a system reset; `0xc9 <- 0x5b` forces the ROM bootloader; `reg 0x0f`
enables SEGGER RTT telemetry; regs 0x3c-0x3f read a debug capture ring
(the previously-unexplained `i2cget 0x47 0x02`, `0x0f`, `0x3c`
commands).

## 2026-08-31: crash-detector de-risk drill - coexist proven, detector is forgectrl-only

The de-risk drill for the head-accelerometer crash detector
(`scripts/bench/accel_crash_probe.py`, bench page `accel-crash-probe`) ran
three coexist windows on dev 20260831204710 with forgectrl and grblHAL up
and idle: a rest window on xyz, a rest window on xy, and an xy window with
one gentle jog (`$J=G91 X5 F1000`, +X first). No emission, no unbind.

- **Coexist PROVEN.** The IG registers (0x30-0x35) program and poll over
  i2c-dev with I2C_SLAVE_FORCE while `st_accel` stays bound; IG_SRC1
  polled at ~166 Hz from Python, and `st_accel` raw reads kept working
  through and after every window. The detector is **forgectrl-only**: no
  kernel change, the liveness path untouched. This was the item's one
  open design decision.
- **Latch and per-axis source report work.** At threshold 40 (~0.62 g at
  the factory +/-2 g full scale) the gravity axis Z (raw -16916, about
  -1.03 g) latched IG_SRC1 on every poll; X and Y stayed silent at rest
  and through the jog. So the shipped detector arms X and Y below 1 g and
  a Z threshold must sit above 1 g plus margin.
- **New fact: the IG needs a running ODR.** The first window returned no
  trips at all because `st_accel` leaves the part in power-down between
  one-shot reads (CTRL1 ODR bits 0) and the interrupt generator only
  samples at a running ODR. The armed detector must set the ODR and
  re-assert it after any liveness read (each one-shot powers the part
  down again). The drill script now saves CTRL1, runs the window at
  800 Hz, and restores the saved value on exit; its old ODR test checked
  the axis-enable bits (0x07) instead of the ODR bits (0x70), fixed in
  the same change.
- **No strike was provoked, none owed.** The rail-contact signature from
  the retired homing spike (29-42 k counts within ~4 ms) already fixes
  the strike magnitude, 3x and more over a threshold that gravity
  already trips; a physical tap would add nothing the design needs.

## 2026-08-31: IG threshold LSB confirmed FS/256; the factory HA* seed values recovered

Two facts that size the crash detector's thresholds, found while seeding
them from the captured headers. They correct the previous entry's g
conversion, which assumed FS/128 (values half of what it stated: the
drill's threshold 40 is 0.31 g, not 0.62 g).

- **The captured headers carry the factory's IG programming.** All 23
  captured `.puls` headers parse (gfutilities `PulseSource`): hunts ship
  every HA threshold zero (detector off, `HAsi/HAsr=2`); travel files
  ship abort-only (`HAar=133`, `HAsr=4`); the cut job ships alert-only
  (`HAxr=132`, `HAyr=112`, `HAsr=4`, `HAar=0`). `HAz*` and every idle
  (`*i`) threshold are zero in every header. So the factory never arms
  Z (the gravity axis), never arms the idle state, pauses cuts on a
  ~2 g event and fails travels on one.
- **IG_THS LSB = full scale / 256, twice proven.** By the factory's own
  values: at FS/128 the travel abort 133 would be 4.16 g at +/-4 g,
  over the measurable range, an abort that could never trip. On the
  bench (two Z-only coexist windows, dev 20260831204710): threshold
  100 trips on the 1.03 g gravity reading and threshold 150 does not;
  under FS/256 those are 0.78 g and 1.17 g, bracketing gravity, while
  under FS/128 the trip at 100 (1.56 g) would be impossible. The
  datasheet states no IG_THS LSB (only ACT_THS = FS/128), so the bench
  check was the proof. The drill script's printed conversion is fixed
  to FS/256 in the same change.
- **The factory seed values in g**: X alert 132 = 2.06 g, Y alert
  112 = 1.75 g, abort 133 = 2.08 g, all at the +/-4 g run full scale.
  Normal commanded motion reads under 0.2 g and a rail strike 1.8 g
  and up, so the factory band sits where the bench says it should.

## 2026-09-01: feed hold and resume in GRBL mode, measured on the null-sink stream

Prompted by the "gapless pause" item, which said the head travels the
hold's deceleration dark. A scratch harness ran the native null-sink
controller (`GFSINK_DUMP`, the stream harness's launch pattern) built from
grblHAL 575ff97: `G1 X150 F6000` at S500 (`laser_dose_curve = off`, floor
10 %), `!` 0.7 s in, `~` after `Hold:0`, under `M4` and then `M3`; then the
same job with `laser_disarm_s = 2` and a switch file, held past the grace
and resumed by `~` and by the button. The dump was read in 25 ms windows
(704 ticks) on both sides of the stop.

- **The deceleration is lit, in both modes.** The item's claim was wrong:
  the core's `disable_laser_during_hold` acts in `state_suspend_manager`,
  which runs only once the handler is `state_await_resume`, so the beam
  goes off when the hold completes, not when it starts. `M4`: fire per
  step 2.89 at 100 mm/s, 2.86 at 82, 3.02 at 64, 3.27 at 47, 3.74 at 29,
  5.41 at 13 (the 10 % floor). `M3`: 381 to 385 fire ticks in every
  window, so fire per step rises from 2.9 to 22.4.
- **The dwell is dark.** Between the last step and the first step: 10 fire
  ticks under `M4` (the tail of the last pulse, far inside the stepless
  FIRE limit), 0 under `M3`.
- **`M4` resumes lit from its first step.** First fire 9 ticks after the
  first step; fire per step 7.60 at 11 mm/s, 4.51 at 28, 3.67 at 46, 3.32
  at 63, 3.08 at 81, 2.94 at 96, 2.84 at 100: the deceleration's profile
  in reverse. A pause under `M4` is a sharp corner in time.
- **`M3` resumes dark for 87 ms.** First fire 2453 ticks after the first
  step; the first three windows (113 steps, 2.1 mm) carry no fire, the
  fourth 198 ticks, then 384. The cause is not isolated; the restore's
  laser-on reaches the stream about one segment buffer late.
- **Resume after the grace closed the window.** `~`: `[MSG:Restoring
  spindle]`, then the arm prompt, then nothing: presses of 0.15 s and
  0.5 s were not honored, `?` kept answering `Hold:0` at the pause
  position, `M5` got no ok. The controller sits in the arm wait until its
  timeout (not waited out). Button: the press that resumes is still down
  when the arm wait starts, so it is taken as the consent (`button
  pressed - job resumed`, `Restoring spindle`, the prompt, `laser armed`,
  all in one press) and the job continued lit with the plain `M4` profile.

Disposition: item 7 is re-scoped to the `M3` resume lead plus a harness
rule; the `~` wedge is recorded under item 8, whose hold option depends on
it; the measurements are in the facts bank. Nothing changed in code.

## 2026-09-01: the M3 resume lead and the held-job resume fixed, host-proven

Both findings of the entry above, fixed the same day and proven on the
null-sink harnesses. Items 7 and 8 close.

- **The `M3` resume lead, root cause.** A laser-push trace in the stream
  engine showed the core issuing the `M3` relight 2451 producer ticks
  after the first resume step: the segments a resume executes first are
  prepared while the job is still held (`state_await_hold` clears the
  step-control flags at hold completion and prepping proceeds from there),
  and with `update_spindle_rpm` cleared an `M3` block prepares them
  without a spindle update, so the level the restore sets reaches the
  stream only with the first segment prepared after the buffer drains.
  `M4` never showed it because a dynamic block updates every segment.
  Fix, core fork (`state_machine.c`, hold completion): in laser mode reset
  the stepper's rpm cache to 0 and set `update_spindle_rpm`, so the first
  segment prepared while held re-asserts the programmed power. Proof: the
  same drill, `M3` first fire 0 ticks after the first resume step (was
  2453), the relight landing at the segment load about 90 ticks before
  the step, exactly as at a job start; `M4` unchanged (9 ticks).
- **The `~` wedge, root cause.** The resume's spindle restore runs inside
  the held state; the blocking arm wait it reaches pumps
  `protocol_execute_realtime`, which enters the core's suspend loop
  (`while(sys.suspend)`) and spins there until the hold ends, so the arm
  loop's switch read never runs again. The button path escaped only
  because the resuming press was still down at the wait's first read.
  Fix, driver: a resume gate (`gflaser_resume_gate`) that every cycle
  start passes on its way to the core: the sender's `~` in `serial.c`,
  the button toggle in Hold, and the cooling client's auto-resume. A held
  laser job whose window has closed re-arms first, with the press
  collected from the poll (`rearm_poll`, never inside the held state) and
  the cycle start issued once the window is open; the blocking wait is
  refused inside a held state as a belt-and-braces (dark, reported). The
  arm flow is split into `arm_gates` and `arm_complete`, shared by both.
  Proof: the same drill, `~` after the grace: the prompt, the press,
  `laser armed`, the job finished at X=150 with the plain `M4` profile.
- **A sender change now holds the job.** `gflaser_poll` feed-holds a
  running job before it disarms on a sender change, so the next sender
  finds the cut in Hold where it stopped (the deceleration behind it runs
  dark, since the consent belonged to the displaced session) and resumes
  it through the gate, or resets it.
- **Harness coverage.** Stream harness rule 21 (`hold-m4`, `hold-m3`):
  lit into the hold, dark while held, lit from the first step out, with
  the realtime `!`/`~` steps and a `wait_state` helper added to the
  session runner. Lifecycle harness: `sender-change-mid-job` now asserts
  the hold and the `~` re-arm; new `sender-change-rearm` (button build:
  the prompt on `~`, the press, the finish), `sender-change-reset` (a
  reset from the held job ends in Idle, no alarm), `resume-after-grace`
  (the window closes in Hold, `~` prompts, the press re-arms, the cut
  finishes). Unit tests: `serial_test` and `laser_arm_test` stub the new
  calls.

**Bench, the same day, on dev image 20260831225403 with the controller
hot-deployed from the working tree** (cross-built with the recipe's own
toolchain and flags from the Yocto work directory; md5 c8494aac, the image
binary saved as `/tmp/grblHAL_glowforge.prev`). Two armed runs, driven
from the LAN by `live_fire_drills.py`, the operator on the button:

- **`holdres`** (new drill): a 30 mm `M4` line at F300 S400 held about 2 s
  in and resumed, then the 90 degree corner; the same hold under `M3` on
  the return line; then a hold that outlived the grace. All three legs
  held and resumed; the window closed in Hold after 61 s, `~` lit the
  button with `press the button to resume the laser job`, the press
  re-armed, the line finished. The operator judged the marks good: the
  `M4` pause against the corner, and no dark lead after the `M3` pause.
- **`senderchg`** (rewritten for the hold): a 20 mm `M3` line at F60, the
  connection dropped 5.0 s in. The job was held 1.9 s after the drop with
  the window closed (`hv_current` dark 0.03 s after the drop), the new
  session's `~` prompted at +9.7 s, the press re-armed at +15.2 s
  (`laser armed`, then the core's `Restoring spindle`), and the rest of
  the line marked: 44 lit samples before the drop, 0 between the drop and
  the press, 106 after. Record `senderchg_20260901-180135.json` on the
  driving host.

Items 7 and 8 are bench-proven. The board runs the hot-deployed binary
until the next flash.

## 2026-09-01: the laser supply's power-good line, characterized without a scope

The "laser power-good" item asked what J1_14 reports. No scope on the
bench, so the line was read through the kernel's own readbacks with a
new probe (`scripts/bench/pgood_probe.py`, fed to the board over ssh
stdin) at 770 to 790 Hz, against LASER_ON, FIRE, the charge-pump
watchdog, HV_ENABLE and the doors from the switch device, and
`hv_current` at 20 Hz. Image 20260901220626 (dev), fresh boot.

- **Dry run, 75 s.** Four jogs; `charge_pump_alive` and HV_ENABLE rose
  and fell together within 5 ms at every run start and end. The pin
  stayed high for every one of 59,518 samples.
- **Armed run, 150 s**: the `witness` drill, a 20 mm square at S400 F600.
  714 LASER_ON pulses over 8.0 s, `hv_current` 0 to 1023, HV_ENABLE up
  for the run. The pin stayed high for every one of 115,872 samples.
- **Driven, not floating.** With a cross-built register tool the pad's
  internal pull was switched to 100 kΩ pull-down (IOMUXC `0x020E03C4`,
  `0x100b0` to `0x130b0`), then pull-up, then restored; the pin read
  high under all three and the pinctrl view confirmed the restore.
- **What the supply has.** The reverse-engineering archive holds the
  supply's datasheets and board photos: the supervisor board carries a
  Weltrend WT7525 (PC-supply supervisor: open-drain PGO high once every
  DC output is within spec, low on an over/under-voltage or over-current
  fault, 300 ms delay), LM2901 comparators and four PC817 optocouplers.
  The pinout and test-point sheets label J1_14 `HV_PFC_STOP` (TP_A2C).
  The factory app reports the line as the `HVpg`/`HVps` header tags and
  its logs show 0 at idle under the same inverted convention the module
  inherited.

Disposition: J1_14 is the supply's power-good, active high, static across
HV enable and emission, and driven. The module now reads it active high
(`laser_pgood` 1, `laser_pgood_sampled` counts good samples, 255 on a
healthy supply); the cooling engine's once-per-session warning keeps its
threshold and now means a supply fault; the catalog's kernel-drill
precheck, which read the old value as "HV not good", moves to the chain's
own witnesses. The line has never been seen low; a supply fault is the
only thing that would take it there. Owed: the change rides the next image
(kernel module), and the dev image regains `python3-mmap` and
`python3-ctypes`, which the python trim removed and which
`resume_dark_lead.py`, `cp_watchdog_timing.py` and the accelerometer
probes need.

## 2026-09-01: the repositories move to the openglow-org organization

All eleven ForgeFIRM repositories moved from the personal account to the
GitHub organization `openglow-org`. The grblHAL core fork was renamed from
`core` to `grblHAL-core` in the same step. Stars, watchers, issues and both
fork parents survived the move, and the old URLs redirect. Three things do
not follow a transfer and are handled separately: the Pages custom domain
with its DNS record, the PyPI trusted publisher for `gfutilities`, and every
`raw.githubusercontent.com` URL. The documentation site kept serving through
the move.

The rewrite that followed named the organization in the recipe `SRC_URI` and
`HOMEPAGE` values, the release and install URLs, the vendor check, the CI
checkouts, the driver URL, the core submodule, the package metadata and every
documentation link. The campaign log keeps its original URLs, as an
append-only record.

Acceptance consequence, measured rather than assumed: the recipe edits sit
inside the three content layers, so all three layer content hashes moved
(`meta-forgefirm` db0a7b58 to 25bf2d4d, `meta-glowforge-bsp` 20e77834 to
444eccc7, `meta-openglow-core` 18634ebd to 0a299582). Every test fingerprint
folds the platform block in, so the whole catalog re-runs. Two behavioral
files outside the layers changed as well: `forgectrl/src/update.c`, which the
update suite covers, and `grblHAL-glowforge/src/driver.c`, which three tests
cover. The move was timed before the first release so that one campaign
serves both.

Build p28 on the moved sources: fetch-verify green for every component at the
new URLs (`forgectrl`, `grblhal-glowforge`, `kernel-module-glowforge`,
`gfcloud`, `gfhome`, `python3-ffmachine`, `python3-gfhardware`,
`python3-gfutilities`), then both images built clean, release
`20260901234804` and dev `20260901234900`. The dev image manifest records the
`openglow-org` source URLs. Owed: the operator flashes the dev image, takes a
fresh-boot baseline, and runs the campaign that authorizes v0.0.1.

## 2026-09-01: correction, raw.githubusercontent.com does follow a transfer

The entry above says raw content URLs do not follow a repository transfer.
Measured after the move, they do. The old owner's path returns 200 and serves
live post-move content, including a commit made after the transfer:
`raw.githubusercontent.com/<old owner>/forgefirm/master/scripts/install-forgefirm.sh`
carries the rewritten release URL, and a request for a commit created after
the move also returns 200. The old release path answers 301 to the
organization, as expected. The redirect still lasts only while no repository
reclaims the old name, so the published links were rewritten anyway.

## 2026-09-02: the audit remediation, host-tested and committed locally

The 2026-09-01 whole-tree audit (221 findings) is remediated in local
commits in every repository, none pushed, nothing yet on the bench. The
work stayed local by decision: one pooled bench session on a locally built
dev image proves it, then one push per repository in CI order, then the pin
bumps and one image build.

What moved, by repository (local commits since the last push):

- forgefirm 8ff37d1 through 88ec984: three new drills
  (`kernel.deadman-close`, always required; `cloud.verdict-hold`; the
  lid-at-button-wait drill on a job longer than the ring), inheritance
  blocked by a later FAIL, ffboot fingerprinted as a component (and moved
  under the recipe), a skipped gate ships no acceptance artifact, the
  installer verifies the factory archive before it trusts it, the first
  release is 0.0.1, helper imports move the fingerprints, the foreign-
  signature update drill, the stream harness rules 22 and 23, the runbook
  brought to the present, the bench records moved out of the tool
  directory, `release.sh --dev` packs the dev image, and ffboot probes with
  `noload`.
- forgectrl 3e6899c through 8c83a03: the SENSOR verdict, bounded HTTP
  bodies, uploads keyed on their authorization and owned by one sender,
  the engine tick on an absolute one-second grid, `cloud_hold_max_s`, the
  panel's verdict and controller-state rows, `resume_ok` in `/cool/status`,
  `cool_recheck_s` as a gate, condvars on CLOCK_MONOTONIC, a libjpeg fatal
  error that ends the encode and not the daemon, the GPU fence timeout.
- grblHAL-glowforge 3405179 through b951f3a (core fork f32d17e in the
  submodule): jogs ship dark under an armed window, the rolloff shapes
  against the segment's own ratio, a reset acknowledges a stream fault, the
  exit ramps down, the arm follows its sender, the verdict polled at 500 ms.
- kernel-module-glowforge cb202ef, 8315fd8: the head safe state disables
  the lens driver, a stopped start does not commit, faults log once, dead
  weight removed.
- python3-gfhardware f76e96e through 1511336: the feeder stopped on every
  way out of a job, a kernel fault is an abort with the safing run to the
  end, the client honors hold and resume, the switch monitor restarts, the
  button level is the backstop, the thermal writers the engine owns are
  gone, host tests in CI.
- Glowforge-Utilities 15b415e, dbf80b8: the transmit pump survives a socket
  error, short GET retries, the report claim released in a finally, config
  values with a percent sign, host tests in CI.
- meta-openglow 634de93 through 454ab2a: uart2 and i2c2 disabled, the
  camera-select pad on its own pinctrl group, `CONFIG_STRICT_DEVMEM`, the
  v2 device-tree name, the dead gfui-client and legacy kernel recipes
  removed, fstab, the prompt in profile.d.
- forgefirm-docs 9972dfe through c9602ea: every contract page matched to
  the code it describes.

Host proof, all green on 2026-09-02: forgectrl 15 host tests and the
daemon build with -Werror; grblHAL unit tests plus the laser-stream (rules
1 to 23) and lifecycle harnesses; the kernel module cross-compiled against
the image kernel in the Yocto work directory; gfhardware 125 tests across
five modules (one Windows-only failure in test_cam_lid_gate, the fail-
closed O_NONBLOCK path, not a defect); gfutilities 127 tests; forgetest 258
unit tests and the coverage lint (54 tests, no uncovered path); the docs
site's strict build and style lint.

Deferred, with the reason: P-13 and P-14 (edits inside the SDMA and SPI
kernel patch files; regenerating a patch needs the kernel tree, and the
failures are latent); K-4 and K-8 (script and scope work outside a fix);
FA-20 (the devserver mock); the B-16 residual (a `__pycache__` inside the
package directory still enters the fetch checksum; the tests' caches no
longer do); P-1 and P-2 (bench measurements, pooled into the session).

Owed: the pooled bench session (the drills named in BRINGUP "Next work"
item 9 plus the full campaign, since every layer hash moves), the two
bench measurements, one local image build of both images, then the pushes
in CI order (forgefirm first), the pin bumps, and the operator's
decisions listed in the same item.

## 2026-09-02: the pooled bench session, first pass: the unattended set

Image 20260902144848 (dev), built locally from the audit remediation, was
flashed to the SD slot; the fresh-boot reference was taken at uptime 24 s.
The unattended queue ran nine times. Every stop was a harness or
diagnostic defect, none an image defect, and every fix went the same way:
host tests, hot deploy to the board, bench PASS, one local commit. At the
end the unattended set is green and the campaign is open with 43 of 54
tests satisfied; the 11 left are the attended queue, the operator's
(`laser.emission-witness`, `cooling.flow-under-load`, `laser.m5-rapid-dark`,
`laser.disarm-in-hold`, `laser.armed-kill`, `laser.pause-resume-lid-cancel`,
`cloud.service-protocol`, `cloud.lid-interlock-abort`, `cloud.pause-resume`,
`cloud.oversize-stream`, `cloud.paused-lid-cancel`), plus the two bench
measurements BRINGUP item 9 lists.

The defects, in the order the queue found them:

1. `image.health` compared the running kernel's full release string with
   the manifest's modules directory, which the remediation (forgefirm
   88ec984) lists without the `LOCALVERSION_AUTO` hash. The test strips the
   same suffix from both sides (forgefirm 133b61a).
2. `kernel.fire-line` phase B was refused by the HV-off latch-unlock gate
   (forgefirm 64f552fc, its first bench run): the gate ran within a second
   of phase A's run, and the one-shot holds `CHARGE_PUMP_ALIVE` for 0.45 s
   after the last 200 ms feed. The gate now waits up to 3 s for the chain
   to release and records the wait; the chain released after 0.41 s at each
   of the three phase boundaries (forgefirm b3efab9).
3. `motion.deadman`'s hang case (new in the remediation) sent `$X` alone.
   The stream fault raises Alarm 17, a critical event: the core refuses
   `$X` with error 79 until a soft reset, and the reset is what re-arms the
   stream (grblHAL 38b450e). The drill records the refusal, resets,
   unlocks, requires the ring back at its idle free count, then jogs; the
   final assertion had also read the state off the report dict as a string
   (forgefirm d536963). Bench: kill respawn 1.2 s, hang to underrun 0.21 s,
   `$X` -> `ALARM:17 error:79`, reset and `$X` -> Idle, ring 33521664 of
   33521664, jog Jog -> Idle, restart retook supervision.
4. `cooling.aa-offset-calibrate` refused four runs (spreads 17.9, 11.3,
   23.2 and 8.3 counts over six edges of a 15-count step). Two causes. The
   queue ran it right after `cooling.flow-verify`, whose no-flow trial
   heats the tube water by 17 C; the warm slug circulates past the sensors
   for minutes and the stationary gate, which compares split-half means,
   passes at a wave's crest: the calibration is registered before the
   heater tools now (forgefirm 1fef9c6). And the readings themselves: a
   PIC read's value depends on how soon it follows the previous PIC read
   (a pair 0.1 ms apart: the second reads 6 to 8 counts high with a wide
   spread; 0.5 to 10 ms apart: tight, a steady 3 counts above sparse
   reads; other readers land such pairs at random). The tool read both
   sensors back to back at 8 Hz and averaged; an 8 Hz sampler with spaced
   reads found every edge within 2 counts of 15 at the same moments. The
   tool reads the sensors 31 ms apart at 16 Hz, reduces each window to its
   interquartile mean, logs every window's count, extremes and value, and
   its spread limit is 12 (forgectrl 0b35f8a; docs 7df312b). Bench: spread
   3.6 and 4.2 standalone, 7.9 under the queue, 5.0 on the final binary;
   the idle windows within 0.6 counts across a run. The cooling engine and
   `/status` still read the PIC back to back; the bias is inside the gates'
   margins and is a facts-bank entry and Next work item 10 (a pacing of PIC
   reads in the kernel).
5. `update.slots-and-signature`'s apply section (forgefirm b182a5a, never
   bench-run) required 200 where the daemon answers 202 with `started`,
   and looked for "not signed", which is the daemon's wording ("archive is
   not signed with the ForgeFIRM release key"); its cleanup had deleted the
   staged archive under the running job, which is why the first run's job
   ended with "not a usable fwup archive" (forgefirm 7605a90).
6. A queue started 4 s after a forgetest restart ran 7 tests instead of
   10: the bench page's `/state` poll had a fixture probe in flight (an
   mDNS answer), the probe stamped its time at its start, and the queue
   start read the stale "no fixture", so the three operator tests the
   fixture runs (`cloud.mode-switch`, `cloud.lid-during-button-wait`,
   `cloud.verdict-hold`) were routed to nobody. The probe is serialized
   and stamped at completion; `tests/test_fixture.py` holds the race
   (forgefirm 7605a90).

The campaign rules cost what they promise: a FAIL closes the campaign, so
the always-required core (`image.health` and the five `kernel.*` drills,
about five minutes) ran again after every stop, nine times in all.

The board at the end of the pass: the image's forgectrl is replaced by the
0b35f8a build, and the forgetest suite files `image.py`, `kernel.py`,
`motion.py`, `cooling.py`, `update.py` and `runner.py` are the committed
ones, all hot-deployed; the manifest still names the pins the image was
built from until the next flash. `/tmp` is empty and `/data` holds nothing
of the session's. The head was returned to its start by the baselines.

## 2026-09-02: the pooled bench session, second pass: the attended set

The attended queue ran on image 20260902144848 later the same day and
passed in full: `laser.emission-witness` (on its fifth run; the four
before it are below), `cooling.flow-under-load`, `laser.m5-rapid-dark`,
`laser.disarm-in-hold`, `laser.armed-kill`, `laser.pause-resume-lid-cancel`,
`cloud.service-protocol`, `cloud.lid-interlock-abort`, `cloud.pause-resume`,
`cloud.oversize-stream` and `cloud.paused-lid-cancel`, and the always
core ran green once more behind them (21:50 to 21:52 UTC). The campaign
was not run again after the last harness change of the day (the cooling
implementation hashes moved), by the operator's decision: the image
built from the pushed pins gets its own campaign, and the release gate
asks for that one anyway.

Three more defects, none in the image:

1. `laser.emission-witness`'s dwell-gap latch rule (forgefirm 85d266e,
   2026-08-31, its first runs under fire) refused three clean runs. It
   required the hardware button latch clear in every sample the engine
   reported armed and then, after a first fix, in every sample up to the
   last nonzero emission count; both windows came from lagging signals
   (the engine's armed flag follows the controller's next report, the
   emission counter latches once per second and reads nonzero about two
   seconds past the relock) and reached into the tail where the job-end
   relock sets the latch by design. The machine was right every time:
   all four sides burned, and the per-sample trail the drill keeps now
   shows the latch clear from the press to the relock, emission through
   the fourth side, HV_ENABLE's dip in the dwell and its return. The rule
   judges the hardware's own window now, from the first emission in every
   sample whose readback word shows the laser latch unlocked, both bits
   from that word, and the third run's recorded trail replays to a pass.
   A fourth run errored on a name the refactor had removed and one later
   check still used, which py_compile cannot catch and a live drill never
   executes on the host; the CI job fails on any undefined name in the
   harness now (forgefirm 970f10a).
2. The daemon dropped 18 to 46 log lines at every job start ("fflog: N
   message(s) dropped (syslog socket unavailable or full)"), the named
   safing writes among the lines at risk. The kernel's queue for a unix
   datagram socket is 10 datagrams and the engine's arm-time settings
   dump alone was 18 in two milliseconds. The logging init sets
   `net.unix.max_dgram_qlen` to 512 before rsyslog and the daemons start
   (forgefirm 3bb16a4; set at runtime on the board for the rest of the
   session), the dump is one line, and the four engine paths that safe the
   machine write their stop and lock before their log line rather than
   after (forgectrl 5c6ee35).
3. The exhaust ran at 6200 rpm on an idle machine after
   `kernel.fire-line`'s takeover restarted forgectrl with the kernel in the
   drill's safe state (disabled). The remediation's busy-start rule took
   the cooldown airflow, as it should over a live cut, but left the engine
   in its idle state, which never re-applies its own duties, so the
   posture had no exit unless a job opened a session. The engine remembers
   a busy start and takes the idle duties on the first tick that finds the
   machine idle with no session (forgectrl 522cdb2).
   `cooling.fans-quiet-after-motion` gained the case: forgectrl stopped,
   `cnc/disable` written, forgectrl started; the busy start logged, idle
   airflow one tick later, the duties idle within 15 s (forgefirm
   9258dea). Seen beside it and left as designed: after every
   takeover restart the supervisor's first liveness probe finds no motion
   and its ladder cycles the rail for 5 s before the retry passes, the
   DRV8825 wedge on the rail power-up that disable-then-enable causes.

The board at the end of the day: the image's forgectrl replaced by the
522cdb2 build, the forgetest suite files and runner as committed, the
datagram queue at 512 until the next boot; `/tmp` empty, `/data`
untouched. Owed, in order: the two bench measurements BRINGUP item 9
lists, the pushes in CI order (forgefirm first), the pin bumps with
`bitbake -c fetch`, one image build, and the campaign on that image.

## 2026-09-02: the forced kernel hang, and the two measurements closed

The audit asked for two bench measurements with the pooled session. The
operator dropped the first, the reset-to-probe state of the 40 V enable,
heater enable, TEC enable and laser-enable nets at the connector during a
cold boot: the device tree carries the factory's pad configuration for
those pins, and the factory machine shows no trouble there; nothing to
measure.

The second was done: one forced kernel hang on image 20260902144848, the
machine idle and the laser locked. `kernel.panic` was set to 0 at runtime
(the command line's `panic=10` would have rebooted the kernel by a
software reset, which is not the path in question), then `c` was written
to `/proc/sysrq-trigger`. The panic spins with interrupts off, the driver
cannot feed WDOG1, and the timeout reset follows.

What happened, with the times: the board went silent at 22:21:27 UTC, two
seconds after the write. WDOG1 was armed at 60 s (WCR 0x771f: enabled,
external reset output on; read back from the register after the return),
so the reset came at about 22:22:27. U-Boot took its watchdog-timeout
branch (the button turned purple, the operator's observation) and, the
board being fused for eMMC boot, booted the factory recovery image, which
took the machine's lease and answered ping from 22:23:48 with no SSH. The
serial console showed nothing from the hang until the power cycle: the
recovery boot prints nothing there, and the purple button is the only
sign. A power cycle at about 22:26:05 (a power-on reset reloads the saved
environment, `boot_recovery=no`) booted ForgeFIRM from the SD slot again;
forgectrl came up, and WRSR read POR.

So the documented path holds: a hard hang ends in the factory recovery
after the 60 s watchdog, and a power cycle returns. The recovery page and
the storage page say now what the console does not show.

## 2026-09-02: the push, the pins, and build p29

With the pooled session green and the measurements closed, the remediation
went public in CI order: Glowforge-Utilities dbf80b8, python3-gfhardware
1511336, kernel-module-glowforge 8315fd8, forgectrl 522cdb2, forgefirm-docs
824f8c4, the grblHAL core fork f32d17e, meta-openglow 6a450f5 (the pins for
the kernel module at 0.0.2, gfhardware, and gfutilities at 0.9.14+git),
forgefirm 05cf734 (the pins for forgectrl at 0.1.1, grblhal-glowforge at
0.1.1, and forgefirm-app at 0.1.22+git), then grblHAL-glowforge b951f3a.
Every CI run on those heads is green.

Build p29 on the pushed pins: `bitbake -c fetch` verified every bumped pin
against GitHub; linux-fslc and the module were cleaned together (the kernel
configuration gained `CONFIG_WATCHDOG_SYSFS`, and the module's package name
carries the kernel's build hash); then both images built clean, release and
dev `20260902230436`. The built-image checks: the kernel and the module both
name `6.12.20-fslc-fslc-g3dc18b0dc67b`; the dev manifest records every pushed
commit at the planned versions; `WATCHDOG_SYSFS`, `IMX2_WDT` and
`STRICT_DEVMEM` are set; forgectrl carries the one-line settings dump and the
busy-start exit; the logging init sets the datagram queue; the suite carries
the corrected drills; no package list names gfui-client; the factory slots
mount `nofail`; the release image has no mmap or ctypes; no QA warnings.
Owed: the operator flashes the dev image, takes a fresh-boot baseline, and
runs the full campaign (all three layer hashes moved), the campaign the
release gate asks for.

## 2026-09-02: the audit's deferred findings, host-proven

The operator's rule for the campaign set the order: no campaign until every
audit finding, the deferred six included, is on one image for a final test.
So the deferred batch was done at once, all of it local, all of it host-proven.

**K-4 and K-8, in the SDMA script and the module.** The waypoint and the
end-of-data interrupts share one line, and the callback cannot fetch the
channel context (a channel-0 transfer, which sleeps), so it decoded on
host-side arming: an end-of-data before an armed waypoint read as the
waypoint. The script now writes 1 to a coherent mailbox word (allocated
before the dedicated pool is attached, so the pool stays the ring's alone;
its physical address rides a reserved context word) before its end-of-data
notify, clears the waypoint counter, and the callback decodes on the
mailbox; run start clears it. The resume lead moved into the script too: a
laser inhibit mask in a second reserved context word, ANDed out of every
GPIO word beside the motor lock, set by run start for an accelerating
forward run and cleared by the script at the waypoint byte. Run start
restores the FIRE drive while the script is idle, the callback writes
nothing to the GPIO data register, and only the deceleration parks the line
(the direction register alone). A resume with no lead plays laser-less for
the whole run, as before, now by construction. The script grew from 160 to
173 instructions; its branches are 8-bit displacements and the growth pushed
one past the range, which the assembler refuses, so the layout changed: the
waypoint action, the power-level path and the first end-of-data tick sit
past the main loop, reached by short branches; the worst displacement is
+120 of 127. Host proof: the module cross-compiles against the pinned
kernel with -Werror, its host tests pass. The bench proof is the new
`kernel.resume-lead` drill, two phases behind one takeover at a 1 kHz tick:
E, a resume whose lead is longer than the data, where a lost end-of-data
would show as 255 ms; L, a 1000-byte lead over FIRE bits with the latch
unlocked and the chain unarmed, the FIRE line sampled through the run. The
catalog counts 55 tests, 0 uncovered.

**P-13 and P-14, in the kernel patches.** `sdma_get_channel()` claims the
channel through `dma_get_slave_channel()`, whose resource hook holds the
engine's ipg/ahb clocks the way the explicit enables did, and
`sdma_put_channel()` is `dma_release_channel()`; the callback setter kills
the tasklet before clearing and initializes it only when setting. The SPI
patch reads `spi_transfer.word_delay` for the PERIODREG wait states, and
`pic.c` sets `word_delay` beside `delay` (the post-transfer gap the factory
kernel also had). The hunks were edited in place; do_patch took them and
the kernel rebuilt clean.

**B-16.** The real fix is bitbake's own knob: `BB_SIGNATURE_LOCAL_DIRS_EXCLUDE`
in the distro conf names `__pycache__` and `.pytest_cache` beside the VCS
directories, so the file fetcher's checksum never sees a bytecode cache.
The experiment in the build VM: a fetch on a clean tree ran the task; a
cache injected under the package directory left 2 of 2 tasks alone; a real
source change ran the task again. The CI's `-B` stays as a second layer.

**FA-20.** The panel's dev-server mock carried 10 gate rows of the daemon's
22, lacked 18 settings keys, accepted a mode the daemon refuses, and sent
`/cool/status`, `/diag`, `/boot`, `/update` and `/slots` shapes the daemon
does not. It now carries the daemon's tables and reply shapes, and a host
test (`tests/test_devserver_mock.py`, 15 cases, one CI step) parses the C
tables and holds the mock to them.

Owed: one local image with the batch (the module pinned from its local
commit, the rest from the pushed pins), the flash, the fresh-boot baseline
and the full campaign; then the push in CI order and the pin bumps.

## 2026-09-02: build p30, the campaign's image

Build p30 carries the deferred batch on top of the pushed pins: the module
from its local commit (pinned for this build only through a kas overlay,
its download mirror primed from the local repository, at version 0.0.3),
the kernel from the edited patches, the distro conf, the suite with the new
drill. Fetch-verify green; the kernel and the module cleaned together; both
images built clean, release and dev `20260903000529`. The checks: the
kernel and the module both name `6.12.20-fslc-fslc-gbe4aba1c1504`; the dev
manifest records the module at its batch commit and 0.0.3 and every other
component at its pushed pin; the module in the dev image carries the
mailbox and inhibit strings and the 32-word script, with a vermagic that
matches the kernel; the patched tree carries the dmaengine claim and the
word_delay source; the suite in the image carries `kernel.resume-lead`;
`WATCHDOG_SYSFS`, `IMX2_WDT` and `STRICT_DEVMEM` are set; no gfui-client;
`nofail` factory slots; no mmap or ctypes in the release image; no QA
warnings. Owed: the operator flashes the dev image, takes a fresh-boot
baseline, and runs the full campaign; then the push in CI order and the
pin bumps.

## 2026-09-02: PIC transaction pacing in the module, host-proven

The operator put item 10 on the campaign's image. The module now paces every
transaction with the sensor PIC: under the driver's lock, a transaction
waits until `pic_gap_us` (a new module parameter, 1000 microseconds by
default, writable at runtime) has passed since the last one ended, whoever
the reader is, so the cooling engine, `/status`, a diagnostic and a bench
sampler read the same value whatever the others do. The pacing wraps all
five transaction paths (single and range reads and writes, and the raw
write), the LED work and the dead-man safing included. Host proof: the
-Werror cross-build against the pinned kernel. Bench proof: the new
`kernel.pic-pacing` drill reads a coolant thermistor twice back to back, 300
pairs, with the pacing off (the control, reported) and on (the claim: the
second read agrees with the first, mean within 2 counts, interquartile
within 3), and the coolant-reading tests. The diagnostic's spaced reads and
interquartile means stay: they take the steady bias out of its edges.

## 2026-09-02: build p31, the campaign's image with item 10

Build p31 replaces p30 as the campaign's image: the same batch plus the
PIC transaction pacing, the module from its local commit at 0.0.3 (pinned
for this build only, its mirror primed), the kernel from the edited
patches, everything else from the pushed pins. Fetch-verify green; the
kernel and the module cleaned together; both images built clean, release
and dev `20260903003213`. The checks: the kernel and the module both name
`6.12.20-fslc-fslc-g08aa91b3b59f`; the dev manifest records the module at
its commit and 0.0.3 and every other component at its pushed pin; the
module in the dev image carries the inhibit, the mailbox and the
`pic_gap_us` strings, with a vermagic that matches the kernel; the patched
tree carries the dmaengine claim and the word_delay source; the suite in
the image carries the two new drills; `WATCHDOG_SYSFS`, `IMX2_WDT` and
`STRICT_DEVMEM` are set; no gfui-client; `nofail` factory slots; no mmap or
ctypes in the release image; no QA warnings. Owed: the operator flashes the
dev image, takes a fresh-boot baseline, and runs the full campaign; then
the push in CI order and the pin bumps.

## 2026-09-03: the campaign on image 20260903003213, first pass, and the PIC read regimes

The board booted the image (kernel `6.12.20-fslc-fslc-g08aa91b3b59f`, the
module's probe clean, the SDMA channel claimed through dmaengine, forgectrl
in GRBL mode with motion verified). `image.health` found the first harness
defect of the image: it asserted the watchdog's sysfs `state` reads
`active`, but that attribute says whether a process holds the device, and
none does; the kernel's core feeds the boot-armed hardware. The check now
reads WDOG1's own control register through /dev/mem (WCR 0x771f: enabled,
a 60 s period) and expects `inactive`; PASS, and the fresh-boot baseline
with it. Campaign c-20260903004540-2ef4 opened with the fixture up.

The unattended queue (44 tests) ran seven and stopped at the eighth:
`kernel.latch-locked-idle`, `kernel.k1-k2`, `kernel.deadman-close`,
`kernel.backtrack-bounds`, `kernel.fire-line` and `kernel.resume-lead` PASS,
so the script's end-of-data mailbox and its laser inhibit (K-4, K-8) are
bench-proven on the first try; `kernel.pic-pacing` FAIL: its paced pairs
spread 5 counts (interquartile) where the drill allowed 3, and its control
pairs were the tight ones (interquartile 2).

The drill's model was wrong, and a study of the PIC's readings on the
idle machine (the module's pacing switched at runtime, pairs read through
pre-opened descriptors, 200 pairs per regime, both coolant sensors) says
what the PIC does:

- A read that follows the previous transaction within about 0.1 ms
  returns the same held sample (a same-sensor pair differs by 0 with an
  interquartile range of 0), so such a pair cannot show a disturbance.
- A read issued at least a millisecond after the previous transaction
  (the paced regime, whether the module or the caller spaces it) is the
  tight one: interquartile 2 on 200 reads, at about 659 to 660 counts.
- A read after 5 to 50 ms of quiet is wide (interquartile 11 to 13) at
  about 661 to 664 counts, and the second of a 0.1 ms pair after such a
  quiet reads 3.6 to 4.3 counts higher still, wider yet (this is the pair
  bias the 2026-09-02 diagnostic saw).
- The sparse reference (single reads 100 ms apart) reads about 668, so the
  quiet-then-read regime and the sparse regime sit 5 to 8 counts (0.3 to
  0.5 C) above the tight regime. Excursions of 10 to 25 counts appear in
  every regime at a small share of samples.

So a fixed gap between transactions does not give every reader the same
value: a reader's first read after a quiet tick lands in the wide regime
and its next read, a millisecond later, in the tight one, 5 counts lower,
which splits the two coolant sensors by their position in the read order
the way the 0.1 ms pair did, in the other direction. The value every reader
would share needs the PIC kept in one regime for every read (a warm-up
transaction ahead of a read after quiet, or a fixed-cadence sampler that
every reader takes its values from), and that regime's level is 0.3 to
0.5 C below the one the machine's gates and calibration were set under.
That is a design decision, recorded here for item 10; the numbers are
what the bench measured.

## 2026-09-03: the PIC worked backward from its firmware; item 10 redone

The operator's direction: understand the PIC's ADC from its code and its
datasheet, then design from the mechanism, not from sampling. The PIC is a
PIC16F1713 (the firmware read from the part, annotated). Its main loop
converts the analog inputs one after another with no delay between them,
about 25 µs a channel (the channel selected and the ADC enabled together,
a 10 µs acquisition loop, an 11.5 µs conversion at FOSC/32, the ADC switched
off after each), and stores each result in a slot with interrupts masked;
the SPI interrupt handler answers a read with the slot's current contents
and never touches the ADC. So a read returns the last conversion of that
channel, at most one loop (about 0.35 ms) old, and no spacing of SPI
transactions can change what it converts. The ADC references the PIC's own
supply (ADPREF left at its reset value) while the sensor dividers hang on
the board's reference net, so a conversion's count follows whatever moves
the PIC's supply at that moment.

The experiment that follows from that, on the idle machine with no kernel
pacing: 200 reads of a coolant thermistor taken right after 3 ms of sleep
(the value converted while the CPU idled) against 200 taken after 3 ms of
spinning (converted under load), twice each, then 200 after a sleep
followed by a 1 ms spin. Idle: median 659, interquartile 2. Busy: median
665, interquartile 2. Sleep then spin: 665. The count depends on the SoC's
load at conversion time, by 6 counts (about 0.35 C), and both regimes are
tight; the wide spreads seen earlier were mixtures across the transition.
The 2026-09-02 pair bias (the second read of a back-to-back pair 6 to 8
counts high) is this: the first read comes right after the reader woke, the
second after the ARM had been up for a fraction of a millisecond. The
pacing as built (a kernel sleep before each transaction) forced the idle
regime onto every second read, which is why its drill failed, and why the
two coolant sensors would have split by read order.

Item 10 redone from the mechanism: before every PIC transaction the module
keeps the CPU busy for `pic_settle_us` (500 µs, a runtime-writable
parameter, 0 to turn it off), longer than one PIC loop, so the value read
was converted under the same load whoever reads and whatever it was doing;
the sleep-based gap is gone. The drill is `kernel.pic-soc-load`: 200 reads
after 3 ms of sleep and 200 after 3 ms of spinning, with the settle off
(the control: the split, reported) and on (the claim: the two agree within
2 counts, each tight, and the settled idle reader reads the busy regime's
value). Host proof: the -Werror cross-build; the mechanism's proof is the
measurement above. The bench proof is the drill, on the next image, with
the coolant-reading tests. Rare excursions of 10 to 20 counts appear in
every regime at a few samples per hundred and are a separate matter the
diagnostic's interquartile means already drop.

## 2026-09-03: build p32, the campaign's image with item 10 redone

Build p32 replaces p31 as the campaign's image: the same batch with the
PIC settle in place of the sleep-based gap, the module from its local
commit at 0.0.3 (pinned for this build only, its mirror primed), the
kernel from the edited patches, everything else from the pushed pins.
Fetch-verify green; the kernel and the module cleaned together; both
images built clean, release and dev `20260903011655`. The checks: the kernel and
the module both name `6.12.20-fslc-fslc-g649f0f50c451`; the dev manifest records the module at its
commit and 0.0.3 and every other component at its pushed pin; the module
in the dev image carries the inhibit, the mailbox and the `pic_settle_us`
strings, with a vermagic that matches the kernel; the patched tree carries
the dmaengine claim and the word_delay source; the suite in the image
carries the new drills; `WATCHDOG_SYSFS`, `IMX2_WDT` and `STRICT_DEVMEM`
are set; no gfui-client; `nofail` factory slots; no mmap or ctypes in the
release image; no QA warnings. Owed: the operator flashes the dev image,
takes a fresh-boot baseline, and runs the full campaign; then the push in
CI order and the pin bumps.

## 2026-09-03: the campaign on image 20260903011655, the unattended set

The board booted p32 (kernel `6.12.20-fslc-fslc-g649f0f50c451`, the settle
at 500 µs, the module's probe clean, forgectrl in GRBL mode with motion
verified). `image.health` PASS with the watchdog read from WCR (0x771f) is
the fresh-boot baseline; campaign c-20260903012535-f7be opened with the
fixture up. The unattended queue ran all 44 tests to PASS, 01:25 to 01:48
UTC, with these results worth their numbers:

- `kernel.resume-lead` PASS (21 s): the end-of-data before the waypoint
  ended the run on time, and the FIRE line stayed low through the 1 s lead
  and drove from the waypoint on (K-4 and K-8 bench-proven, twice now).
- `kernel.pic-soc-load` PASS (3 s): with the settle off, an idle reader
  read 660 and a busy reader 666 (interquartile 2 each, the split +6); with
  the settle on, 663 and 664 (the split +1). The third check, which had
  compared the settled level with a Python spin's, is re-based on the move
  off the idle regime (the kernel's spin is its own load level, a count or
  two under a Python spin) and the re-run passed under the campaign.
- `cooling.aa-offset-calibrate` PASS: the air-assist offset 15.5 counts
  with a spread of 0.6 across six edges, where the same diagnostic spread
  3.6 on 2026-09-02 after its interquartile-mean fix and 11 to 23 before
  it. That is the settle's effect in the real diagnostic: every edge lands
  within 0.6 of the same value.
- `cloud.verdict-hold` PASS (129 s on its second start; see below).

One incident, mine. With 43 results in and the 44th (`cloud.verdict-hold`,
fixture-driven, a cloud print armed under the warm-up gate with the loop
heater on) still running, a suite-file hot-deploy restarted forgetest,
because the count of result lines had reached 44 with `image.health` among
them. The orphaned test's cleanup never ran: the print proceeded when the
warm-up verdict cleared (01:44:08), ran its 32 s laser-less job
(`hold.puls`; `laser_on_sampled` 0, forgectrl's emission counter 0), the
client relocked the latch at 01:44:40, and the machine was left in cloud
mode. The latch was relocked again by hand, GRBL mode restored through
forgectrl, and the test started again, which passed. The rule that keeps
this from recurring: no forgetest restart, suite deploy, or test start
while /state shows a test running or the batch unfinished; progress is
counted from the batch's own done list.

Owed: the attended eleven (live fire, the operator, one run per turn):
`laser.emission-witness`, `cooling.flow-under-load`, `laser.m5-rapid-dark`,
`laser.disarm-in-hold`, `laser.armed-kill`, `laser.pause-resume-lid-cancel`,
`cloud.service-protocol`, `cloud.lid-interlock-abort`, `cloud.pause-resume`,
`cloud.oversize-stream`, `cloud.paused-lid-cancel`; then the push in CI
order and the pin bumps.

## 2026-09-03: the attended set stopped at its first test

The operator rebooted the machine before the attended set, because the
interrupted cloud test had left the fans running, then started
`laser.emission-witness`. The fixture pressed at 01:52:46; the job started
and, one engine tick later, the engine declared a warm-up hold (coolant
22.9 C under a 24 C start gate) with the heater on and idle airflow.
grblHAL honored the hold and feed-held the job. So the laser fired and the
head moved for about a second with the fans at idle duty, then the job sat
in the hold; when the warm-up and the flow verification ended, the run
airflow came up and the job waited for the button. The operator stopped the
session there; the test was aborted, the latch relocked, the machine left
idle, disarmed and locked.

Two causes. The 24 C start gate was `cool_temp_start = 23.9`, a setting
`cloud.verdict-hold` raises to force its hold and restores afterward: the
run the forgetest restart killed never restored it, and the next run
captured 23.9 as the original and restored it to 23.9. The setting is
stored and survives a reboot, and it is still in force. The second cause
is the engine's own: after an arm, a job can fire for up to one tick before
the warm-up and flow gates are evaluated and the run airflow applied. The
raised start gate exposed it. Both are owed: the setting reset to its
default, and the engine evaluating fire and airflow at arm time, before
the first fire, which is a change to forgectrl and another image before
any further live fire. The attended eleven were not run; the campaign on
20260903011655 stands at 45 of 56.

## 2026-09-03: the fire that preceded the airflow, and what it really was

The setting was reset first: `cool_temp_start` back to 16, its default, by a
POST to /settings on the machine. Every cooling gate row then read its
compiled default again. The only remaining non-default values are the bench
calibrations, the air-assist offset of 16 counts, the recorded dose curve, and
the corner gamma of 1.5.

The second cause turned out to be narrower and worse than "the gates are
evaluated one tick late". The cooling verdict carried nothing that said which
session it answered. A controller opens its armed window, reports it, and the
engine applies the run airflow and the flow interrogation only when it reads
that report on its next tick. Until then the verdict on file is the one the
engine computed for the idle session before the arm, and at idle nothing is
wrong, so it reads `fire_ok=true` and stays fresh inside the two second
window. In GRBL mode `arm_complete()` re-checks the gate right after the
button press and opened the window on exactly that verdict. The fixture
pressed at 0.0 s, so the re-check ran before the engine had ticked at all.
The hold that followed was the engine catching up, not the cause. Cloud mode
had the same exposure in `_verdict_wait()`, which broke as soon as the verdict
read clean.

The fix gives the verdict a run-session identity. The engine publishes
`armed`, its own view of the window, set from the reported state before
`flood_apply` and before the publish, so `armed=true` means the verdict was
computed with the run session open. `gfcool_fire_ok()` additionally requires
it while the window is open and leaves the pre-arm gate alone, where there is
nothing yet to acknowledge. `arm_complete()` waits for it, bounded at five
seconds, and refuses the job if it never comes. The cloud wait requires it
too. The verdict body grew a key, so its buffer went to 384 bytes behind a
static assert on the budget: an oversized document is not published, and no
verdict reads to every controller as a fault.

Proofs, all host. The two new cloud tests were run against the unfixed code
and failed there: `_verdict_wait()` returned in 72 microseconds on the pre-arm
verdict, which is the defect itself. The lifecycle harness gained two cases,
one where the engine never takes the window (refused arm, no emission) and one
where it takes it two seconds late. The late case is the one that proves the
controller keeps reading the verdict while it is blocked in the arm; it waited
1.6 s and then armed. Without that path every job on the machine would fail at
the arm. Also green: the grblHAL arm unit test, the stream harness at 22
cases, the 16 forgectrl host tests, the 289 forgetest tests, the coverage lint
at zero uncovered paths, and the docs build.

The acceptance catalog gained the bench form of the rule. `laser.emission-witness`
now fails if any sample shows emission while the cooling engine reports a phase
that runs the fans at idle duty, which is what this failure looked like from
outside. The sampler records the phase, and the trail carries it.

## 2026-09-03: build p33, image 20260903163543

Both images built green from the committed trees, with four components taken
from local commits that are not pushed: forgectrl 8ef9509 at PV 0.1.2,
grblhal-glowforge 2f5edee at 0.1.2, kernel-module-glowforge faa1034 at 0.0.3,
and python3-gfhardware dd0ebf3 with the app recipes at 0.1.23+git. Each was
pinned for this build only through a kas overlay that is never committed, with
its download mirror primed from the local repository; the grblHAL submodule
came from its own pushed commit. The kernel and the module were cleaned
together, so the feed carries one kernel version string.

The built images were checked for the fix rather than assumed to carry it: the
verdict format string with `armed` in the forgectrl binary, the refusal message
in the controller binary, the armed test in the cloud module, and the new
airflow witness in the acceptance suite. The p32 content is still present, and
there were no QA warnings. This image supersedes 20260903011655 as the
campaign's image, and the campaign must start again on it: three layer hashes
moved, so nothing is inherited.

## 2026-09-03: the shakedown on image 20260903163543, and the arm fix under fire

The image went on the bench and the whole catalog ran. It does not authorize
that image, and it was never meant to: one suite file was hot-deployed part
way through, so the catalog on the machine is not the catalog in the image.
This pass was for finding defects, and the clean run on a rebuilt image is
the one that counts.

Two things had to be cleared before any of it. The board came up with its
clock at March 2018 and the time daemon never stepped it, twenty-one minutes
in, with the large-step flag set and name resolution working. Every campaign
identifier and result would have carried that date, so the clock was set from
the workstation and written to the hardware clock, and the daemon disciplined
from there. Nothing in this image touches time keeping and the same pattern
appears on earlier boots, so this is not new, but it is worth a look on the
next cold boot. The fresh-boot reference itself was clean: eighty attributes,
no disagreement with the fixed values.

The unattended set then stopped at its seventh test on `kernel.pic-soc-load`.
The machine was not at fault. Over eleven runs the settle collapsed the load
dependence from a control split of six or seven counts to one, every time,
which is the drill's primary assertion and it never failed. What failed was
the third assertion, which asked the settled reader to move off the idle
regime by at least half the control split. The move is three counts against a
split of six, so the bound sat exactly on the median and decided on one count
of noise: two of the eleven runs failed while the machine read identically to
the nine that passed. The kernel's spin is a couple of counts lighter than a
Python spin, so a settled reader lands above the idle regime without reaching
the busy one, and asking it to reach halfway asked for something the mechanism
does not promise. The bound is now a third of the split, which still catches
the failure it guards, a settle that overshoots and lets the conversion fall
back to idle and so does not move at all, with a count of margin either way.
That file was hot-deployed and the set re-run: thirty-one of thirty-one.

Then the attended ten, with the fix under live fire. `laser.emission-witness`
passed, and the evidence that matters is that its new airflow witness found
nothing: not one sample showed emission while the cooling engine was in a
phase that runs the fans at idle duty. The fixture pressed 0.3 s after the
button lit, the same instant press that produced last night's burn, and this
time the arm waited for the engine to take the job before the window opened.
Against last night's aborted run on the same test, emission peaked at 154
rather than 42 and the high-voltage current reached full scale rather than
236, because the job ran its whole square instead of a second of it. The
operator confirmed the mark on all four sides. The button latch never read set
while the laser latch was unlocked, the supply dipped across the two second
dwell and came back lit for the third side, and the job disarmed a tenth of a
second after Idle on the program end.

The rest went through without a stop: fifty-six of fifty-six, nothing skipped.
`laser.armed-kill` took the controller down mid-fire twice, with emission at
zero about two seconds after each, the latch relocked and the controller
respawned. `laser.disarm-in-hold` held the window open in feed hold for 60.7 s
and then disarmed on the grace. `cooling.flow-under-load` judged its rise with
the tube lit through the window. The cloud four passed, including the
operator's own judgement that the app's progress advanced as it cut.

The bench fixture dropped off the network in the middle of the attended set,
after the emission witness and before the fourth test, and was not reachable
by address or by name from either the machine or the workstation. Its config
carries no address, only a hostname resolved by a multicast query the fixture
answers itself, so when it stops answering there is no fallback. The operator
covered the presses by hand until a power cycle brought it back, and it
resumed taking the arm press immediately. Pinning its address in the config
would keep a failed name query from hiding it.

## 2026-09-03: the full campaign on 20260903211413, 56 of 56, authorized

The campaign the release gate asks for, on the image as built: nothing
inherited, nothing hot-deployed, both halves run on one flash.

  campaign     c-20260903212054-4063
  image        20260903211413 (dev)
  manifest     c802d7639ac236f1fbf03fb7d65216f32aa395c0dcb04bce9a12d6274b0f0c61

Forty-five unattended, then the attended eleven, all PASS. The inherited
results were invalidated before the start: the earlier runs were taken on a
harness whose press routing has since changed, and the catalog hash does not
cover that, so carrying them forward would have been the very inheritance the
campaign model exists to prevent.

The arm-acknowledgment fix held under live fire again, and its witness is the
line that says nothing happened: `idle_airflow_fire` empty, so no sample
showed emission while the cooling engine was in a phase that runs the fans at
idle duty. Emission peaked at 155.

The pause test passed this time, and how it passed is the point. Its trail
reads 90, 90, 90, 65, 65, 65, 65, then zero and zero for the rest of the four
seconds: the latched sample window draining, and then real darkness. The run
that failed earlier read 153, 12, 99, 152 - a second kernel run inside the
pause, which the controller log confirmed. The difference between the two runs
is who pressed the button. Here the actuator did, on the operator's behalf,
after a single presence press: the evidence records `presence` by the operator
at the gate and every press after it by the fixture. That does not prove the
earlier failure was a double press rather than a machine fault; it does mean
the press count is no longer a matter of anyone's memory, so a repeat would be
answerable.

Two harness faults were found and fixed along the way, neither of them the
machine. The kill drill calls the shared arm-and-fire helper twice, and the
helper holds the ready gate, so the new presence gate asked the operator for a
second press mid-test while the actuator stood by holding the presses. That is
fixed: presence is proved once per test, and the second gate returns at once
with its setup line still shown in case the scrap wants moving. The kill drill
is the only test in the catalog with two ready gates.

The bench actuator dropped off the network twice during the earlier attended
runs, and both times the harness fell back to asking the operator without
saying so. That silence is what made the first pause failure unanswerable. A
lost actuator is now said out loud, in the log and in the evidence. Its
address is pinned in the bench config, so a failed name query can no longer
hide a box that is present. And its wifi signal, which reads -82 to -83 dBm
here, now goes into every run's record beside its uptime, so the next drop
says whether the link faded or the box restarted rather than only that it was
gone.

## 2026-09-04: the audit's last finding under test, 56 of 56, and the audit retired

The 2026-09-01 audit had one finding left. The web framework collected every
POST body in memory before an endpoint callback ran, so before the token check,
and it had no ceiling. An unauthenticated client on the network could send data
until the board had no memory left, and the daemon and its job would stop.
`forgectrl` now sets the ceiling to 64 KiB. The fix was proven on the host on
2026-09-03, but it had never run on the machine, so the audit stayed open.

Build p37 put it on an image with every other current commit.

    campaign     c-20260904132654-d731
    image        20260904131106 (dev)
    manifest     3e3f65e90022c107908a86cafb85c59eaa8df0196a59ca4c431a41389ef708c5

The image carries forgectrl ff89288 and the forgefirm layer at 637fb67. The
other three components came from their pushed pins, and the build made sure
each pin was the local HEAD. forgectrl was not pushed when the image was built,
so it came from a local commit through a kas overlay with its download mirror
primed from the local repository.

This change adds no new string to the forgectrl binary, so the build proved the
source instead. The image manifest records a git blob hash for each file. The
build compared the manifest's `src/main.c` and `src/update.h` against the blobs
of the local commit and stopped if they disagreed. They agreed.

Twenty results were inherited from the campaign on 20260903211413. The forgectrl
revision and the changed suite file made the other thirty-six stale, which is
the domain model at work: every test that covers `src/main.c` lost its result,
`forgectrl.auth` among them. Nothing was hot-deployed.

The unattended set ran 25 of 25 PASS in nine minutes. `forgectrl.auth` carried
the new case, and its evidence is the answer the audit asked for:

    13:28:26 POST /settings (no token, 4 MiB body) -> 403
    13:28:26 GET /status after the oversized body -> 200

The daemon refused the oversized body from a client with no token, and it was
still serving in the same second. The firmware upload is not affected, because
the ceiling truncates only the framework's own copy while its post processor
still receives every chunk.

The attended set ran one live test at a time. The emission witness marked its
square with the fans behind the beam: emission peak 155 and 0 at the end, HV 0
to 1004, beam detector idle 1839 and peak 2392, button latch 0 through the
dwell, disarm and dark 0.0 s after Idle. The M5 rapids stayed dark: peak 152 on
the cut, then 35 samples across 8.3 s with no emission and HV 0. The operator
ran the remaining nine. All PASS.

The result is 56 of 56, authorized, on one flash of one image.

With that the audit is retired. Of its 221 findings, all are now fixed,
note-only, or closed by decision (P-1 declined, P-2 observed, X-I3 rejected).
The audit file is deleted, as the 2026-07-03 and 2026-08-13 audits were before
it. forgefirm 637fb67 and forgectrl ff89288 are pushed, and the forgectrl pin
moves to the revision this campaign ran.

## 2026-09-04: commissioning phase 1 on the bench, the first run walked and the commission set passed

Phase 1 of the commissioning work is the consent flow, the operator account,
the preferences, the machine facts, the cloud decision, the controller gate,
HTTPS on 443 with the plain listener on 80, and the return to the factory
firmware. It was written and proven on the host, and nothing of it was pushed.
Build p39 made both images from the working trees of the layers and the test
suite, so the image carries the new recipes (the account replay, the sshd
policy, the console banner, mDNS, the license bundle, the TLS build of the
HTTP stack) while the daemon and the controller came from their pushed pins.
The phase 1 daemon and controller were cross-built from the working trees and
hot-deployed. Both images carry one kernel. The release rootfs uses 92 MiB.

    campaign     c-20260904220822-e8fe
    image        20260904203403 (dev)
    manifest     7b867e05f6ef8a3c1e0e19b6ea00d44180d1c4742db36a5f206f58c15e7119ea

The first hot-deploy found five defects that the host could not, and each was
fixed and redeployed the same evening:

- The bench cross-build scripts compiled with a 32-bit `time_t`. Yocto builds
  the arm32 image with `-D_TIME_BITS=64`, so every `time_t` argument to GnuTLS
  arrived in the wrong registers and the certificate was never made. The
  scripts now pass the time64 and file-offset flags, and `tls.c` names the
  step that fails and why.
- The route table was registered as the framework's URL prefix. ulfius fills
  the `:id` parameters from the URL format only, so `/agreements/:id` matched
  and answered 404 with no document. The paths are registered as the format.
- The `/mode` body had a 128-byte buffer. A gate reason that names five
  wizards is longer, so the JSON was cut mid-string and the harness could not
  read the mode of a gated machine. The buffer is 512 bytes.
- Finish lit the button solid green and nothing released it. The light now
  goes out when the control panel page is first served after Finish.
- `GET /` over HTTP from the machine itself was sent to HTTPS like a LAN
  client. A loopback peer now gets the page, as it gets every route.

Two harness cases were written for the old boundary. `forgectrl.auth` posted a
cooling report from the LAN address over HTTP and expected 403; it now expects
the 302 to HTTPS and then the 403 over HTTPS, without following the redirect.
`commission.gate-blocks-controllers` called the settle wait right after it
created the override; a gated machine counts as settled, so the wait returned
before the supervisor had spawned. It now waits for the controller first. The
test-suite init sources the bench env file with allexport.

The operator walked the real first run from a workstation on the same evening:
the four documents (33 s from the first to the last), the press at the teal
light, the account, the preferences, the machine facts, cloud mode on with a
serial the operator typed, Finish. The record shows each step with its time,
the agreement hashes and methods, and the account's uid. The account was
replayed into the system files, the home directory made at mode 0700, the
gate opened and the controller came back with motion verified. One observation
from the operator, the green light that stayed on, is the fourth fix above.

The automated set then ran one test at a time, each with its prerequisites
satisfied on this image: `forgectrl.auth`, `forgectrl.panel-serves`,
`commission.cert-page`, `commission.https-only-writes`,
`commission.override-until-reboot`, `commission.ssh-until-reboot`,
`commission.mdns-announce`, `commission.account-login`,
`commission.agreements-rehash`, `commission.gate-blocks-controllers`,
`camera.key-read`, and `image.license-bundle`. Twelve PASS. The two takeover
tests restored the real record and the override under a restart each time.

`commission.wizard-first-run` ran last, with the operator at the workstation
and the bench fixture on the button: the documents in 33 s, the LEDs read
breathing teal from sysfs, the fixture's press accepted in 0.7 s, a throwaway
account in 25 s, Finish, the gate open and the controller back, the operator's
Yes to the panel opening. PASS in 102 s. The real record and account came back
under the restart, the throwaway account and its home were removed, and the
machine read as before.

`commission.cloud-disabled-surface` needs cloud mode off, and the operator
had chosen it on. There was no way to change that choice from the panel: the
setup page after the first run showed only its last screen, and its rail was
not clickable. The rail now opens the preferences, machine, and cloud steps
again once the setup is complete, and the cloud step starts from the current
choice. The operator turned cloud mode off through it, and the test ran with
`forgectrl.settings-bounds` before it: both PASS, the cloud mode and the
gfcloud homing refused with 409 while cloud is off.

That run broke a rule the operator then stated: every acceptance test runs
from the page as the machine is, and a queue runs through, so no test asks the
operator to change a setting first. Two tests had. `commission.account-login`
read the bench account's password from a file the operator had to write; it
now moves the account record aside under a takeover, creates a temporary
account through the account route, runs the login rules against it, and
restores the real account, the system accounts replayed and the temporary home
removed. `commission.cloud-disabled-surface` now takes the gfcloud homing and
the cloud boot mode down itself before it turns cloud mode off, and restores
the three settings in reverse order. `cloud.mode-switch` lost its homing
precheck and sets the gfcloud homing for its `$H` leg itself, and the runner
turns cloud mode on when a test declares cloud mode and it is off. The env
file, its init sourcing, and its README row are gone. The acceptance contract
on the docs site states the rule. With the operator's cloud choice back in
place (cloud on, gfcloud homing), both rewritten tests ran again: PASS, the
login one in 49 s with its two restarts, the cloud one in 3 s.

The welcome screen of the setup no longer mentions the certificate: whoever
reads it has accepted the warning already. The fingerprint stays on the
`/cert` page and the panel's Commissioning card, and the documentation says
where.

Not run: `commission.factory-return` reboots into the factory firmware, and
`commission.root-ssh-refused` needs the release image. Both wait for a later
session. The libmicrohttpd messages that the stderr relay logged as warnings
during browser page loads now go through the daemon's logger at debug.

Nothing is pushed and no pin moved. The phase 1 change is proven on the bench
and waits for its commits, in CI order, and then a campaign on an image built
from the pins.

## 2026-09-05: commissioning phase 2 on the bench, the setup checks proven, two motion truncations found

Phase 2 of the commissioning work is the setup checks (switches, sensors,
airflow, motion, cameras, the coolant diagnostics as checks, the cloud header
capture), the Commissioning tab, and the engine-raised flags. It was written
and proven on the host on 2026-09-04, and the daemon reached the bench by
hot-deploy on the phase 1 image. Nothing of it is pushed.

    campaigns    c-20260904235822-ffa8, c-20260905000007-3173, c-20260905004142-0de4
    image        20260904203403 (dev)

The first bench evening (2026-09-04) found three defects the host could not:
the check's finish handed the result to the record writer and then freed it
again, so every check that measured well ended with "the record could not be
written"; the motion check armed the crash watch's abort tier on the jogs,
which is the head motion that tier is built to catch, so it always fired; and
the sensors check read raw crash-source bits as accelerometer events and
judged the tachometers with the fans off. Each was fixed that evening. The
switches and cameras checks passed with the bench fixture on the lid and the
button.

On the fixed daemon, one test at a time with the long prerequisites skipped
for the proof: `commission.check-sensors` (12 s), `check-switches` (5 s),
`check-cameras` (14 s), `check-airflow` (39 s) and `check-flow-verify` (157 s)
PASS. `commission.check-motion` FAILED on the -X jog: the witness read a
peak-to-peak of 331 against a threshold of 400.

**The witness.** The check sampled the head accelerometer through the sysfs
one-shots, about 150 ms each, so a one-second jog gave four sample pairs and
the peak-to-peak was luck. Each one-shot also powers the part down, under the
crash watch that had set it to 800 Hz for the jogs. The witness now reads the
output registers over the crash watch's own i2c path (800 Hz, +/-4 g, about
175 samples a second) and judges the peak-to-peak of the signal averaged over
about 30 ms: the wideband vibration averages out and what stands is the
commanded acceleration itself, about 500 counts up at the start of a jog and
down at its end. Bench numbers, at rest with the pump and fans on and over the
four 50 mm jogs at 3000 mm/min:

    rest         low-passed p2p x 216  y 247     rms x 152  y 102
    +X           x 1771  y  664                  rms x 812  y 321
    -X           x 2119  y  722                  rms x 917  y 323
    +Y           x  726  y 1343                  rms x 241  y 409
    -Y           x  598  y 1789                  rms x 281  y 456

Moving is the busier axis at 2.5 times its rest reading and 400 counts or
more; the margins above are 2.2 to 3.4. An RMS rule was tried first and left
the Y jogs at 1.1 to 1.5 times the threshold: the gantry moves more smoothly
than the head, and an RMS window that ran only to the driver's Idle missed
the deceleration ramp altogether.

**Two truncations.** After the first passing motion run the harness's
baseline found the kernel position counters at Y +1332 steps (25.0 mm, half
the last jog) and jogged the head back. The kernel counts what the SDMA
played, so half of the -Y jog never played. A second run with the counters
polled every 10 ms showed two cuts:

- The liveness probe's return leg ended 186 steps short (22 in an earlier
  run). The cooling engine's hang dead-man stops motion when the controller's
  last report is older than 5 s and the kernel is running; the check stops
  the controller on purpose and runs the probe about two seconds later, so
  the report went stale mid-probe and the engine wrote `cnc/stop`. The
  supervisor's stop now forgets the engine's last report
  (`cool_controller_stopped`): a deliberate stop is a death the supervisor
  covers, not a hang, and the probe plays with nobody counting.
- The driver reports Idle when the stream is produced; the kernel plays it a
  queue depth behind, and a jog sent at Idle while the kernel still drains
  the last one starts up to a depth of pad slots ahead of its own bytes, so
  the physical end ran 160, 290, 500 and 540 ms behind Idle across the four
  chained jogs. The check stopped the controller 16 to 75 ms after the last
  Idle and `cnc/stop` discarded the half second still queued. Replicas from
  the Grbl socket measured the pieces: a stop 30 ms after Idle with a 300 ms
  tail queued loses 229 steps; back-to-back jogs across the drain lose
  nothing (three reps on X and Y); closing the socket at Idle loses nothing.
  The check now samples until `cnc/state` reads idle after each jog and
  judges and stops only then. The driver's Idle-before-drain is BRINGUP
  "Next work" item 9 and a fact under "Running the controller".

`commission.check-motion` now also requires the position counters to read as
before the check, within two steps, and names `cool.c` in its coverage. The
suite's own unit tests, run for the phase 2 tree for the first time, found
one order defect: `commission.check-flow-verify` names `cooling.flow-verify`
as a prerequisite, and the queue's dependency sort pulled that heater tool
ahead of `cooling.aa-offset-calibrate`, which must run before any heater
trial warms the coolant. The check now names the offset calibration first
among its prerequisites. On
the rebuilt daemon it PASSES in 23 s with every move played to its end (the
kernel idle with every byte done after each jog, the counters back exactly,
no dead-man line), and `check-sensors`, `check-airflow` and
`check-flow-verify` PASS again after it (12, 38 and 156 s). The record
carries switches, sensors, airflow, motion, cameras and cooling.flow-verify at
version 1. The bench machine's Y counter carries a 229-step residue from the
replicas, inside the boot baseline the harness restores against.

`commission.cloud-header-capture` was attempted by the operator twice. Its
steps and notice said "when the page asks", against the rule that an attended
test names its one action in terms of the thing under test and sends the
operator nowhere; the harness answers the wizard's print prompt itself, so the
text was wrong, not the mechanics, and it now reads "once the app shows the
machine online, press Print". The first attempt ran cloud mode up (the client
connected, homed, and uploaded the lid image in 40 s) and was aborted from the
bench page 185 s in with no print request in the client's log; the harness
left the wizard waiting in the daemon, so the second attempt failed at once
with 409, "a wizard is already running". The check runner now aborts its
wizard on every exit, and a start that meets its own check still running from
an earlier run aborts that one first. The client's log also showed its lid
snapshot request to the daemon refused on port 8080: the default in
`ffmachine.py` and the bench's `gfhome.conf` still named the bring-up port
after the move to 80; both read `http://127.0.0.1` now (the direct capture
fallback had covered it). The test stays owed: the coolant offset
and flow calibration checks wrap the diagnostics the cooling catalog proves
and ran as the flow-verify check does. The head's Y room for the jogs was
confirmed from a lid snapshot before the first unattended run (+Y moves toward
the front; the head sat at the back, out of the camera's view).

Nothing is pushed and no pin moved. Phase 2 is proven on the bench apart from
the operator's header capture and waits, with phase 1, for the commit hold to
lift.

## 2026-09-05: commissioning phase 3 on the bench, the sheet burned end to end, the lens model rebuilt

Phase 3 of the commissioning work is the sheet: the font, the renderer, the
daemon's own sender, the nine live cards, and the record burn. It reached the
bench by hot-deploy on the phase 1 image, one card at a time, the operator at
the machine, and every card ran to a result by the evening. Nothing of it is
pushed.

    image        20260904203403 (dev), forgectrl hot-deployed 14 times, the driver twice
    record       sheet PVHHW-32AQ7, the record burn 7530 lines, 11 cards, completed 18:36Z

**The lens.** The first focus cards taught the model. A full-step sweep up
and back from the hall edge found 24 half-steps of travel and a slope of
0.38 mm of lens per mm of material; both were wrong. The operator's facts:
the lens is a 2 in lens under a collimated beam, so it moves with its focal
point 1:1, and the carriage travels 0.485 in (12.32 mm, the head drawing).
A count in half-steps, stop to stop, gave 36 (37 on a second count), the hall
edge 13 above the bottom stop. The service's Z commands are full steps (its
header's `ZSmd` 0 is full-step mode): 15 for 0.5 in, 4 for 0.1 in; an early
community capture of the same law in half-steps (8 at 0.1 in, 28 at 0.4 in,
30 at 0.5 in) saturates at 30, the service's idea of the usable travel. The
model is now in the lens's own half-steps from the bottom stop: the focus
card homes the lens on the stop in agreeing rounds (three within a half-step)
and counts up to the edge, burns twelve numbered lines over the travel, and
the pick (two adjacent lines at most, their middle) on the thickness gives
`laser_focus_bed_steps` (the half-step where the lens is 2 in from the bed:
3.3 here) and `laser_focus_steps_per_mm` (the count over 12.32 mm: 2.92).
The travel above the bed step is the tallest material the lens focuses,
11.2 mm (0.44 in) on this head. The operator's pick, lines 8 and 9 on
0.110 in plywood, sits about one line above the service's own law. The
stop-to-stop sweep was retired the same day ("testing the travel? Stop it."):
the travel is the screw's constant, only the edge's place in it is per head.
In four cloud prints the lens never moved: the client kept the factory's idle
Z lock (`cnc/motor_lock` 8) through its motions; fixed in the client, the
bench proof owed.

**The cards, one press each, what each run found:**
- `sheet.place`, `sheet.frame`: proven the day before; the frame's mark dose
  S400 at F3000.
- `laser.focus`: as above; the pick prompt became a multichoice after two
  lines looked alike; the prompt timeout went from 180 to 600 s after a
  careful look timed the card out.
- `laser.floor`: the faintest continuous rung 10 (2 to 8 blank), the floor
  12. A re-run after the key switch moved mid-job burned the 2 rung: the
  driver's M102 reloaded the gamma and the curve but not the floor, so `$35`
  lifted every S; M102 now runs the arm's spindle configuration (the floor
  too), proven in the lifecycle harness, and the rungs stayed blank again.
- `laser.dose-curve`: 10:0.44, 20:2.98, 30:9.31, 45:26.26, 60:45.50,
  80:57.30, 100:100, within two points of the panel recorder's earlier
  curve. The first run "did not fit": the card's text, burned before the key
  switch, was an eighth discharge segment; a `;record` directive starts the
  witnesses at the first rung.
- `laser.corner`: gamma 1.50, the same as before; five reloads in order.
- `motion.scale`: 99.9998 x 35.9918 mm, diagonal 106.2736 (entered in
  inches), scale 0.00 and -0.02 percent, squareness 0.02 mm per 100, the
  crosses coincident; nothing to correct.
- `cooling.flow-load`: the first run failed twice over. The card burned its
  box, settled inside the job with the laser off, the armed window relocked
  after 60 s idle (a second press, unprompted), and the second arm's flow
  check ran its heater under the patch: rise 9.13 C, k 2.7e-4, refused by the
  validator. Rebuilt: the loop settles before the press (quiet after 60 s),
  the engine's check is held for the card (`cool_flow_check_hold`, not a
  gate, self-releasing), one press burns the text and the patch. Result: lit
  61 s, dose 33557 raw-s, rise 0.72 C with the peak 75 s after the fire began
  and a 55 s lag, k 2.14e-5 (density) and 2.78e-5 (CW), within 10 percent of
  the bench defaults from 2026-08-29.
- `sheet.record`: burned; the layout's text was shortened to its cards first
  (the renderer clips at the card's edge) and the focus sentence went to the
  footer.

**What the sheet taught about the plate.** The percent glyph at a 2.5 mm cap
is smaller than the kerf and blobs; the labels are bare numbers now. The
ladders used a third of their cards; they span them. The sender kept one
line in flight and the planner starved on the text's 0.3 mm segments, the
head stopping at every one; it keeps lines in flight up to half the
controller's RX ring, with `$`, M102 and M2 alone as barriers. Text under
the floor-and-curve override came out fainter (with the curve off, M4's
velocity scaling cuts the density linearly); every card's text burns under
the machine's keys and the override moves mid-job by directive.

**Also this day:** sessions survive a daemon restart (`/run/forgefirm/
sessions`, 0600) and the login returns to the page that asked; the setup
page's address follows the step shown; lengths on the page and the plate
follow the units preference; every look at the sheet says not to move it;
the sweep ran on every card through an unzeroed session struct (fixed). The
operator's verdict on the cards' feedback ("crap"; "it sucks") stands: a
usability pass leads phase 4.

Nothing is pushed and no pin moved. Phase 3 is proven on the bench and waits,
with phases 1 and 2, for the commit hold to lift.

## 2026-09-05: the commission acceptance set slimmed to three attended tests, and all twenty proven

**The order.** The operator refused the commission acceptance set as it
stood: 26 cases, 13 of them attended (seven `operator`, six `live`, one per
sheet card with a press and a piece of wood each, plus two errands: booting
the release image to try SSH by hand, and performing the return to the
factory firmware). The set was rebuilt the same day for minimal operator
involvement (plan decision 18): 20 cases, three attended with the bench
actuator up. The sheet is one live case, `commission.sheet` (the placement,
the frame, and the five cards on one piece, one press at the ready gate,
the actuator arms the rest); the first run is `first-run-flow` (the page's
own calls in the page's order, the press the actuator's, unattended) and
`first-run-page` (the walk, page covers only); `root-ssh-refused` is gone
(the effective policy from `sshd -T` runs inside `ssh-until-reboot`; the
release image's policy as built is the release gate's check);
`factory-return` probes the guards only. Host: the full forgetest unit suite
(317) and the coverage lint (0 uncovered, 78 tests) in the build VM.

**The unattended set, image 20260904203403, campaign
`c-20260905204752-2e3a`.** The 17 unattended commission tests and their 11
auto prerequisites were started one at a time through the API in
prerequisite order: 22 runs PASS, 0 FAIL. `check-switches`, `check-cameras`,
and `first-run-flow` ran with nobody in the room, the actuator on the lid
and the button.

- **Defect 1, in the new flow test:** its first run stalled at the press
  step with the button breathing teal after the actuator had pressed. Not a
  race: the daemon records the acceptance only when `GET
  /wiz/agreements/press` is polled while the button reads pressed
  (`button_take_pressed` in `cb_wiz_press_status`); the page polls it from
  its press step, the test did not. Run aborted (the record and the account
  came back clean under the restart), the test made to poll `press_status`
  the way the page does, redeployed; the rerun passed in 21 s.

**The attended three.** `first-run-page` PASS (the operator's walk, 85 s;
the actuator took the press in 0.7 s). `cloud.mode-switch` (a prerequisite
the campaign lacked) PASS unattended. `cloud-header-capture` PASS on one
Print from the Glowforge app: 134 tags of job 1582523977, and the client
log reads capture, cool-down skipped, `:cancelled`, then the capture file:
the header-capture hang fix of this morning is proven. Then
`commission.sheet`, campaign `c-20260905213015-6d2f`, on one 8 x 6 in
piece, 807 s: the placement (thickness 0.125 in), and six burns each seen
by all three witnesses: frame (tube current max 1023, 144 LASER_ON samples,
thermopile rise 813, 82 s lit), focus (1264, 48 s), floor (668, 91 s),
dose curve (2273, 111 s; the fit 10:0.37 to 100:100), corner (795, 31 s),
flow-load (1243, 90 s; k 2.80e-5 density, 3.64e-5 CW, peak 1.03 C). The
seven settings the cards wrote were put back as found.

- **Defect 2, in the sheet test:** the first attempt failed in the
  placement, `thickness 0.0`: the bench is in imperial units, the wizard
  asked for inches, and the test answered 3.2 (81 mm, over the 30 mm cap).
  The test now answers in the machine's units (0.125 in or 3.2 mm) and
  checks the millimeters the record carries. Nothing fired.
- **Defect 3, in the sheet test:** the second attempt failed before the
  frame on the test's own program check, which looked for `M3 S`; the
  frame and the text are `M4 S400`. The check now accepts either laser-on
  command. Nothing fired.
- **Defect 4, in the runner:** the flow-load card settles the coolant loop
  before it lights the button (60 to 240 s), and the actuator's arm press
  waited only 60 s for the light, so the sixth arm went to the operator
  (the record shows five presses by the fixture and one by the operator).
  `arm_press` takes a `lit_timeout` now and the sheet test passes 420 s
  for that card (host-proven in test_fixture; the bench proof is the next
  sheet run, whose fingerprint the fix moved).

Nothing is pushed and no pin moved; the commit hold stands. Board at the
end: GRBL mode, the real record and account, `/tmp` and `/data` clean.

## 2026-09-05: the cloud focus stalls at the lens driver's hold current

The operator printed the same job in cloud mode at 0.1 in and at 0.5 in
focus on the client with the lens unlock (`motor_lock` 0 for the motion)
and saw no difference in the cut. The client's log carried the service's Z
steps counted in full: the hunts -4 of -4, the prints 4 of 4 and 15 of 15,
the lock lifted before each and set after. The hunt after the 0.5 in print
told the rest: its first homing sweep took 6 steps down to leave the hall,
against the 3 every other hunt of the day took, so the lens sat about 3
steps above the edge where 11 were commanded. The stream moved the lens,
and not far enough.

Measured on the board with single steps (`cnc/z_step`) at the service's
own ramp (630, 164, 115 ms, then 77 ms per step), each trial from a fresh
home and counted back to the hall edge at 180 ms:

| `head/z_current` | move | executed |
|---|---|---|
| 1 (low, the hold current) | 5 down | 5 of 5 |
| 0 (high) | 5 down | 5 of 5 |
| 1 (low) | 10 up, 77 ms plateau | 2 of 10 |
| 0 (high) | 10 up, 77 ms plateau | 10 of 10 |
| 1 (low) | 10 up, 40 ms plateau | 2 of 10 |
| 0 (high) | 10 up, 40 ms plateau | 10 of 10 |

The lens rises at the high current only; the low current is a hold
current. The cloud client's homing sweep runs at the high current and
leaves the driver at the low one, and nothing set it back for the stream:
the print's focus move rose two steps and stalled. The focus card in
forgectrl had the same two conditions right from its first bench run (the
lock cleared and the high current set before its ladder), which is why the
sheet's focus proved out while the cloud focus did not.

Fix in `python3-gfhardware/gfhardware/machine.py`: the motion path enables
the Z driver and sets the high current with the unlock, the idle posture
and the action cleanup set the low current with the lock, and the hunt's
home-offset steps run at the high current. Host-proven: the lens test in
`test_machine_lid_button` orders the enable and the current against the
lock and the feed, and the cleanup test checks the rest posture (92 tests
pass). Deployed to the board as a hot copy. Bench proof owed: the same
print pair, 0.1 in against 0.5 in, with a visible difference in the cut.
The docs carry the fact (motion-hardware, kernel-module `z_current`,
cloud-mode step 4) and BRINGUP's Z paragraph.

Bench-proven the same evening: the operator printed the same job through
the Glowforge app at 0.1 in and at 0.5 in focus on the deployed client,
and the two heights focus correctly. The cloud focus is closed. Nothing
is pushed and no pin moved; the commit hold stands.

## 2026-09-05: the Z frame corrected to the focal height, installed, proof owed

The operator's definition, restated as the rule: Z is the focal point's
height above the tray (Z 0 focuses on the bed, Z +1 focuses 1 mm above it),
never a lens-carriage coordinate. Two frames written earlier had been wrong
(the hall edge as the top of travel at Z 10.6, then the bottom stop as Z 0)
and were removed from the driver, the wizard, and the docs the same evening.
As built: the driver's camera-referenced home sets Z to the hall edge's lens
half-step above the focus card's bed step, over the lens's half-steps per
millimeter; `$102` follows `laser_focus_steps_per_mm` when set; the Z
envelope runs from the bottom stop over the carriage's 12.32 mm; the focus
card writes the edge's step as `lens_hall_edge_steps`; `gfcloud_home_z` is
gone. The GRBL homing session answers the service's hunt as done without
moving the lens and takes its own hall reference after the service goes
quiet; cloud mode's hunt is the service's, unchanged. Host-proven: the
client's 93 unit tests (one new: an acknowledged hunt moves nothing),
forgectrl with -Werror and its sheet, commission, and wizcalc tests, the
dev-server mock 15 of 15, forgetest's sheet unit test 8 of 8, the driver's
host build and lifecycle harness.

Installed on the bench by the operator (forgectrl fd86adee, the driver
a82da585, machine.py, ffmachine.py, gfhome.py, the sheet suite file; all
hashes verified). The copy left the two binaries without the execute bit:
forgectrl's init loop respawned every 5 s with "Permission denied" until a
`chmod 755`; then forgectrl listened on 80 and 443, the GRBL controller
started, the liveness probe passed. The driver publishes `$102=2.922`, the
head's count from the config; `$132` still reads 10.6, a persisted value,
and wants a one-time `$132=12.32`. Board at the end: GRBL mode, idle,
`/tmp` and `/data` clean (the config still carries the superseded
`laser_focus_z_bed_mm` key, unread). Nothing is pushed and no pin moved; the
commit hold stands.

Owed, next session: a GRBL `$H` (gfcloud) that skips the lens hunt and
reads Z 3.3 on this head; a focus card run writing `lens_hall_edge_steps`;
the redesigned sheet's bench run (commission.sheet, fingerprint moved).

## 2026-09-06: GRBL $H in gfcloud mode skips the lens hunt, Z lands on the step grid

The first of the owed proofs, on the installed binaries (forgectrl
fd86adee, the driver a82da585, gfhome.py 2ddefc40): one Grbl connection,
`$H`, then `?`. The board before the drill: GRBL mode, idle, not homed,
lid closed, no other port-23 client, forgetest idle, `$102=2.922`,
`$132=10.6`.

`$H` returned ok in 46.5 s with `H:1`. The homing session's log shows the
service's hunt answered without motion (the `HUNT-ACK` line at the session
start, then "hunt acknowledged without motion" at the service's hunt action,
`hunt:starting` and `hunt:completed` sent 4 ms apart) and the session's own
hall reference at the end (five `z_axis:home` passes, lens current left
LOW). Earlier sessions on this head ran 34 to 75 s with six or seven hunt
lines each, so the wall time is the service's, not the lens's.

Z after the home: the driver's log line says Z 3.32 (the arithmetic:
edge 13 minus bed 3.3, over 2.922 half-steps per millimeter), the
controller reports `MPos` Z 3.422. The difference is the step grid: the
controller's position is a whole step count, the home Z is 9.7 half-steps
above the bed step and rounds to 10, and 10 over 2.922 is 3.422. A
half-step is 0.34 mm, so the rounding is a third of one. The log line and
`sys.home_position` carry the unrounded value while `MPos` carries the
rounded one; the driver can quantize the home Z to the step grid before it
stores and logs it so the three agree (decision owed). `$132` still reads
10.6; the one-time `$132=12.32` is still owed.

The operator took the fix. The driver's home Z is now the edge's height
above the bed step rounded to a whole step, over the scale, in a pure
helper (`gfhome_edge_z` in glowforge_homing.h) with a host test
(tests/lens_home_test.c: the reference head stores ten steps and reports
Z 3.4223, a whole-step pick is exact, halves round up, and 11935 edge and
bed pairs over the settings' ranges store the count they were rounded to;
in the CMake -Werror list and the CI workflow). Host-proven: the -Werror
host build, the armed-window lifecycle harness, the new test. Cross-built
and staged (grblHAL_glowforge 0e2c0d6a), installed with the execute bit,
the controller killed and respawned by the supervisor in one second
(motion verified). The second `$H` on this binary: ok in 39.1 s, `H:1`,
the log line `homed - X0.00 Y0.00 Z3.42`, `MPos` Z 3.422: the three agree.
Then `$132=12.32` was written on the same connection and `$$` reads
`$132=12.320`, persisted. The docs now carry the rounding (grblhal-driver
"The lens (Z)", homing "The position after a home", motion-hardware,
usage/homing, which also lost a stale `gfcloud_home_x/y/z` mention;
BRINGUP's position paragraph reads Z 3.42 on the bench head; the plan's
6.15 Z bullet and its `G92 Z<edge>` note). The catalog: the homing files
are under an existing `src/**` cover, and host test files need none.

Board at the end: GRBL mode, idle, homed at Z 3.42, `$132` 12.32;
`/data` clean, `/tmp` holds only the previous driver binary as the
rollback (volatile). Nothing is committed; the hold stands.

The board rebooted at 14:38Z (forgetest's fresh-boot baseline at uptime
25 s), between the second home and the focus card; the installed driver
(0e2c0d6a on the rootfs) and `$132` survived it, the `/tmp` rollback copy
did not.

The focus card, on its own piece (the operator at the machine, the
prompts relayed from the dev host over the wizard API: POST start, GET
/wiz/dark polled, POST answer with form fields; the daemon sends LAN HTTP
to its self-signed HTTPS). `sheet.place`: the lens referenced (two passes,
0 steps each), the origin set at the head's home position with no jog,
"One card", 0.110 in (2.794 mm); 68 s. `laser.focus`: the lens homed on
its bottom stop in three agreeing rounds, the hall edge 18 half-steps
above it (yesterday's count on this head was 13; the travel is 36 either
way, so the edge now sits mid-travel), the button lit 41 s in, the press
by the operator, the ladder streamed as 297 lines with the tube at peak
1023, 235 LASER_ON samples, thermopile +1354, lit 47 s; the operator
picked line 7 alone; the thickness kept. Result: the lens at half-step
16.4 of 36 on the pick, the bed step 8.2, 2.922 half-steps per
millimeter, material up to 9.5 mm (0.375 in) above the bed. Written and
verified in the config and `/settings`: `laser_focus_bed_steps` 3.3 to
8.2, `laser_focus_steps_per_mm` 2.922 unchanged, `lens_hall_edge_steps`
empty to 18; the record carries both wizards at 14:43Z and 14:46Z. The
controller the wizard respawned reads `$102=2.922` and `$132=12.320`.
The edge sits 9.8 half-steps above the bed step (9.7 yesterday from 13
and 3.3), so the driver's home Z on this head stays ten steps, 3.42 mm.
Both absolute counts moved by five between the two days with their
spacing kept; whether the bottom-stop reference moved or the earlier
count was low is open. The driver's reference-head defaults (13 and 3.3)
are placeholders and were not changed. 172 s. Board at the end: GRBL
mode, idle, unhomed after the respawn, the lens at the pick step;
nothing of the drill on the board.

## 2026-09-06: the lens travel measured, the stall count found unreliable

The operator wanted the 13-to-18 change understood, so a lens travel drill
(`scripts/bench/lens_travel.py`, run on the board with the controller
stopped through forgectrl and restarted after; lens motion only) stepped
the lens over sysfs the way the focus card's lens home does, at the card's
180 ms cadence and the run current, and counted under each condition for
three rounds.

The hall's rising edge is exact. A ladder of descents below the point
where the hall leaves home returned every commanded step until 18
half-steps below the rising edge: drive 4 came back 10, 8 came back 14, 12
came back 18 (the 6 to leave home included), in half-step and in full-step
mode alike. Its hysteresis band is 4 to 6 half-steps: going down, the hall
stays home that far below the rising edge, and the same count brings it
home again; a 100 ms rest before every read and a 400 ms cadence changed
nothing, so the band is the sensor's, not timing.

The bottom stop is 18 half-steps below the rising edge and the top stop 20
above it (eight measurements, every condition, the band subtracted): 38
half-steps of travel, not the 36 the card assumes. Every count that ends in
a stall on a stop is unreliable: past 18 the return count read 18, 16, 14,
or 12 at random (12 to 18 in the ladder, 16 to 18 in the card's own
condition), always an even shortfall, in full-step mode as in half-step,
at any cadence. The rotor slips whole steps against the stop and
re-engages up to three full steps out of phase with the drive, so the
first steps of the ascent move nothing. Three agreeing rounds do not
protect against it: the slip repeats. The focus card's 18, 18, 18 on this
day was the no-slip case. Yesterday's 13 was the earlier code's short drive
(fewer than 18 half-steps below the edge), which never touched the stop
and counted its own descent back, as a 16-step drive does today (13, 13,
13).

The hold current is unusable for the lens in either direction: with it
throughout, the lens never came back to the edge (the run current brought
it back afterward); with it only for the drive down and the run current
for the count, the carriage stopped following after about 14 half-steps
and every count read 10. The run current, the factory's own choice for its
lens home, stays.

What follows for the model: the driver's Z after a home depends only on
the edge step minus the bed step, and both move together with a slipped
count, so today's settings (18 and 8.2) place Z as well as an unslipped
pair would; the absolute counts from the stop and the travel above the bed
are what a slip shifts. The rising edge is the one exact reference the
head has. The scale is open: 38 half-steps over the drawing's 12.32 mm is
3.08 per millimeter, and the service's four full steps for 0.1 in give
3.15, against the 2.922 the card writes from 36. Decisions owed to the
operator: whether the card references the edge alone and takes the stops
as measured constants, and what the millimeter scale rests on. The drill
is in the repo; the board carries nothing of it. Board at the end: GRBL
mode, controller running, the lens on the edge, the idle posture (motor
lock 8, hold current, half-step mode).

The operator's decisions the same afternoon. The lens is never driven into
its stops on a user's machine (a stripped stepper drive gear is the
concern); stall drills run on the bench reference machine only, which is
that machine's official name. Every unit is taken to share the travel in
steps and the height of a step; what differs per unit is the step along the
travel at which the hall trips. The focus card's purpose is that one
number: the focal height when the lens is on the hall's rising edge, read
off the pick (the picked line is a known number of half-steps from the
edge, and the material top is at the pick). A home then goes to the edge
and sets Z to that number. The stops drop out of the model. The travel
constant stays 36 half-steps, a half-step of leeway at each end for a unit
a touch out of specification, and Z below the tray stays allowed (the tray
comes out for tall work; a feature for another day).

The depth gauge, with the drill's park mode holding the lens (the
controller in standby, the hold current): bottom stop 56.4 mm, top stop
45.7 mm, the rising edge 52.3 mm, readings the operator calls approximate.
The stop-to-stop 10.7 mm and the edge's 4.1 below / 6.6 above do not fit
one step height against the stall counts of 18 and 20, so the stall
readings carry compression or tilt, or a reading is off by about a
millimeter; a stall-free pair at ten half-steps below and above the edge
was parked for the same gauge, and the operator confirmed the documented
step height (0.342 mm per half-step, 2.922 per millimeter, 36 half-steps
over the drawing's 12.32 mm) and saw the lens ring on every step: it
overtravels a touch and returns, which is the hall band's plus or minus one
that no settle time cured. The lens went back to the edge, the controller
restarted (motion verified), the drill removed from `/tmp`.

## 2026-09-06: the edge-referenced lens model built, installed, and proven

The operator's order, with one addition: after a home the focus parks at a
user-settable height, 3 mm by default. As built the same afternoon: the
driver's `$102` is the screw's constant 2.922; the one per-head setting is
`lens_hall_edge_z_mm`, the focal height with the lens on the hall's rising
edge (the default 3.35 is the bench reference machine's); a gfcloud home
hands the runner the whole half-steps from the edge's grid step to
`lens_park_z_mm`'s (`GFHOME_PARK_HALF_STEPS`, clamped to the window ten
half-steps below the edge to twelve above), the runner takes them at the
run current after its reference (`ffmachine.park_lens`), and Z after a home
is the park height on the step grid; the Z envelope is the travel around the
edge on the bench reference machine in the 36 convention. The focus card
lost its stall home and the bottom-stop frame: it references the lens on
the edge (the two full-step passes every live session takes), burns twelve
lines from twelve half-steps above the edge to ten below, two apart, refuses
a pick on line 1 or 12, and writes the one setting from the pick's offset
under the thickness. `laser_focus_bed_steps`, `laser_focus_steps_per_mm`,
and `lens_hall_edge_steps` are gone from the registry, the driver, the
docs, the plan, the mocks, and the sheet acceptance test, whose expected
keys and restored settings follow the model. Docs rewritten: the driver's
"The lens (Z)", homing's "The position after a home", usage homing and
commissioning, motion-hardware's lens section; BRINGUP's position
paragraph; the plan's 6.15 and its settings and catalog lines.

Host-proven: the driver's rewritten lens test (grid heights over the
settings' range store their step; 60551 parks stay inside the window) and
the armed-window lifecycle harness; forgectrl with -Werror and its sheet,
commission, wizcalc, and jobstream tests, the dev-server mock 15 of 15; a
new runner park test (up, down, and a zero park that touches nothing) with
gfhardware's 132 others (one Windows-only camera test fails with or without
the change); forgetest's sheet unit test 8 of 8. Cross-built and staged
(forgectrl bbe99ec0, the driver d53bb933, gfhome.py 3c0c4ff7, ffmachine.py
551251e9, the sheet suite 868d2086), installed on the board with the
execute bits, the four old lens keys deleted from the config, forgectrl and
forgetest restarted (78 tests, queue idle).

Bench, three steps. A `$H` on the defaults: `MPos` Z 3.080, the log
`homed - X0.00 Y0.00 Z3.08 (the hall edge at Z3.42, the lens parked -1
half-steps from it)`, the runner's `lens parked -1`. The focus card on a
fresh piece, 0.110 in, one card (the placement 13 s, the card at its arm in
8 s with no stop homing; witnessed: tube 1023, 232 LASER_ON samples,
thermopile +1212, lit 46 s): the operator picked lines 8 and 9 ("a bit less
separation between the lines than before", the spacing being two half-steps
now), so line 8.5, three half-steps below the edge, and the card wrote
`lens_hall_edge_z_mm` 3.82 (the earlier frame's 18 and 8.2 gave 3.35; the
difference is 1.4 half-steps, inside the old ladder's spacing); the reach
it reports is Z 0.40 to 7.93 mm, so on this head the bare bed sits 0.4 mm
under the window's floor. Then a `$H` on the measured edge: `MPos` Z 3.080,
the log `homed - X0.00 Y0.00 Z3.08 (the hall edge at Z3.76, the lens parked
-2 half-steps from it)`, the runner's `lens parked -2`. Board at the end:
GRBL mode, controller running, homed at the park, the idle lens posture,
`/tmp` and `/data` clean. Nothing is committed; the hold stands.

The operator saw the card's safe reach (Z 0.40 to 7.93 mm) and refused it:
it should approach the factory's 12 mm. The cause is the window: the bench
reference machine's stops sit 18 half-steps below the edge and 20 above, the
36 convention takes one off each end, and a guessed six half-steps of
head-to-head spread plus one more were taken off both ends again, 4.8 mm of
the 12.3. The guess had no data behind it. Two facts weigh against it: the
factory's own law runs 30 half-steps above its zero, 22 above the edge, past
this head's top stop at 20, so every field machine stalls the top on 0.5 in
material; and a single touched step is not the 18 to 36 stall steps the old
homing took. The operator asked the pick to accept a run of adjacent lines
(five looked alike at the two-half-step spacing); built, host-proven
(sheet and commission tests), staged as forgectrl 762dd884, the deploy held
for the window decision.

## 2026-09-06: the head accelerometer finds the lens stops without a slip

The operator asked whether the head accelerometer could detect a stop hit.
The drill (`scripts/bench/lens_stop_accel.py`) reads the crash watch's chip,
an ST LIS2HH12 at 0x1e on i2c-3, the way the crash watch does: straight over
the bus in six-byte bursts with the rate register set to 800 Hz for the run
(the iio path idles at 10 Hz and waits a period per read, 2 Hz in practice,
useless here); 670 samples a second, about 115 per 170 ms step window. It
steps the lens one half-step at a time from the hall's rising edge toward
each stop and six past it, with the peak-to-peak per axis per step.

The signature is unmistakable. A free step rings on every second half-step
(the even ones from the edge, on this head): the strong steps sum 14000 to
21000 across the three axes, the quiet ones 2000 to 8000. At the top stop the
ringing dies at once: +21 to +24 all under 3300 summed, then a slip burst at
+25 (18000 on z alone, 40000 summed). At the bottom the ringing fades from
-15 (the carriage loads before the hard stop), and the slip burst comes at
-19 (23000 on z). The burst is the bang; the dead ring precedes it by two to
four steps.

A contact rule on that: learn the strong parity from the first two steps,
call contact when a strong-parity step rings under 9000 summed, or at once
on a burst over 36000, then back off two. Three rounds per stop: the bottom
called at -16 every time and the count back to the edge read 14 every time,
exactly the steps taken, so nothing slipped; the top called at +22 every
time (the first strong-parity step past the stop at 20), and the count back
down to leave home read 25 or 26, the stop's 20 plus the hall band, so
nothing slipped there either. A first form of the rule ("two quiet steps in
a row") was late at the top, +22 to +24, and its returns showed slips of
about five; the parity form fixed it.

What it opens: a per-head stop measurement at commissioning with at most two
touched steps and no slip, which makes the window per head instead of a
guess, and gives this head 36 half-steps of reach (-15 to +21 around the
edge, 12.3 mm) instead of 22. The chip's rate register was put back (0x0f),
the lens left on the edge in the idle posture, the controller restarted
(motion verified), the drill and its trace removed from `/tmp`.

## 2026-09-06: the stop finding built into the focus card, with its fallback

The operator's order: "Build the stop finding into the focus card. Have a
fall back in case the stops are not detectable on a particular machine. In
that case, reduced travel is acceptable, and the user should be notified
about it and recommend that they open a git issue or post it on the
community forum." As built: `accel.c` gained `crash_hw_listen` (the run
rate at the 2 g scale, put back after) and `crash_hw_burst` (the three
axes in one auto-incremented read on the crash watch's bus handle);
`wizcalc.c` the pure contact rule, `wizcalc_stop_call` (the strong parity
learned from the first two steps, contact at the first strong-parity step
under 9000 summed, or at any burst over 36000 as a slip) and
`wizcalc_ring_readable` (the strong steps' median at least twice the quiet
threshold), eleven cases in `wizcalc_test`; `wizlive.c` the finder
(`z_find_stops`: after the reference, at the run current in half-step mode,
each leg one half-step at a time listened to through 170 ms of the 180 ms
cadence, the lens backed off two after whatever was seen, the count back
to the edge required to match the steps taken within one, the top's count
down to leave home required to be the steps plus a band of 2 to 9), the
window (`lens_window`: the session's finding, else the head's settings,
else the fallback of 10 below and 12 above), the ladder spread over the
window on whole half-steps, the cards' Z clamp on it, and the card's
result, settings, and summary: `lens_stop_below_steps` and
`lens_stop_above_steps` written beside the edge height (the found free
travel, or the fallback numbers), and when the stops could not be found
the summary names the reason and asks for an issue at
github.com/openglow-org/forgefirm or a post on community.openglow.org. The
driver's park clamp and Z envelope read the two settings (the fallback
window until then). The settings registry, the dev-server mocks, the
page's card description, the sheet acceptance test (the `stops` key, the
two settings restored, the covers widened to `accel.*` and `wizcalc.*`),
its unit test, the docs (usage commissioning, motion-hardware, the driver
page), the plan's 6.15 and settings lines, and BRINGUP follow.

Host-proven: forgectrl with -Werror and its sheet, commission, and wizcalc
tests, the dev-server mock 15 of 15, forgetest's sheet unit test 8 of 8;
the driver's host build, lens test, and lifecycle harness. Staged and
installed (forgectrl c7e2ba37, the driver 485e9221, the suite 38079bfe).

Bench, the card run to its arm prompt and aborted before the burn, three
times. The first found the fallback path working but no accelerometer: the
finder had not opened the crash watch's bus handle, which the dark checks
open around their own use; fixed. The second read the ring at half strength
and called it unreadable: the listener had taken the crash watch's 4 g
scale where the drill had run at 2 g; fixed to 2 g. The third: "the bottom
stop: the ring died 16 half-steps below the edge", "the top stop: the ring
died 22 half-steps above the edge", both legs' counts home exact, 14 s in
all, and the arm prompt read "Its stops were found by the head
accelerometer, 14 half-steps below the reference and 20 above, without a
slip. Twelve 1.772 in lines burn heavy, the lens stepped from 0.269 in
above the reference to 0.189 in below it." The idle posture and the chip's
rate register were put back each time; `/tmp` is clean.

The card in full, on a fresh piece, 0.110 in, one card (the placement's
prompts answered by their live sequence numbers; the daemon's counter runs
on across wizards): the stops found again at 16 and 22, so 14 below and 20
above, no slip; the ladder over the whole free travel, 0.269 in above the
reference to 0.189 in below; the burn witnessed (tube 1023, 234 LASER_ON
samples, thermopile +1218, lit 47 s); the operator picked line 8 alone,
"much easier to distinguish this time around", two half-steps below the
edge, and the card wrote `lens_hall_edge_z_mm` 3.48 (the day's three
frames, 3.35, 3.82, and 3.48, lie within a half-step of one another),
`lens_stop_below_steps` 14, `lens_stop_above_steps` 20; the reach it
reports is Z -1.3 to 10.3 mm, 11.6 mm. Then a `$H`: `MPos` Z 3.080, the log
`homed - X0.00 Y0.00 Z3.08 (the hall edge at Z3.42, the lens parked -1
half-steps from it)`, the runner's `lens parked -1`. Board at the end:
GRBL mode, controller running, homed at the park, the idle lens posture,
`/tmp` and `/data` clean. Nothing is committed; the hold stands.

## 2026-09-06: commission.sheet passes on the redesigned sheet, and the finder fails inside it

The `commission.sheet` acceptance test on a fresh 8 x 6 in piece, started
over forgetest's API with its own token (`/data/forgetest/token`, the
`X-ForgeFIRM-Token` header), the emission-witness prerequisite overridden
as on 2026-09-05 and the live acknowledgment given on the operator's
instruction; a new campaign, c-20260906172748-0b36, on image
20260904203403 with the hot-deployed forgectrl c7e2ba37 and driver
485e9221. The operator's one presence press came after 44 s and the bench
actuator made every arm press after it. PASS in 822 s: the placement 16 s,
the frame 102 s (mark dose 400 at 3000 mm/min), the focus card 69 s, the
floor 109 s, the dose curve 147 s, the corner 53 s, the flow-load card
281 s (its coolant settle 60 s); every burn witnessed by the tube current
at its 1023 clip, LASER_ON samples 136 to 255, and the thermopile +674 to
+2306; the seven settings restored as found (the config reads the
operator's edge 3.48 and stops 14 and 20 after it). The sheet's status:
pass, satisfied; 33 of 45 required satisfied in the campaign.

Inside the test the focus card's stop finder took its fallback: "the lens
steps do not ring clearly enough on this head", the reduced window, the
notice in the result, the test's pick on the fallback ladder. The card had
found the stops twice standalone before the test (13:12 and 13:21); it
failed again standalone after the test (13:43) while the bench drill,
reading the same chip over the same bus, saw a clean ring (contact 16 and
22, no slip); it worked again after a daemon restart (13:47) and after a
dark motion check that arms, polls, and disarms the crash watch the way
the cooling engine does around a burn (13:50). Two things changed in the
daemon meanwhile: the finder no longer closes the crash watch's bus handle
when it did not open it (the first build closed it after every finding,
which left the cooling engine holding a closed handle with its state
saying open, so its next arm would have failed and the watch stood down;
the listener now opens its own handle only when none is open and closes
only that), and the finder logs its ring per half-step (a healthy leg on
the bench reference machine: 8616 19780 5766 23541 7855 21106 5387 19954,
the strong steps even). The failure after a burn is not yet reproduced
on the new build; a frame burn followed by a dry finding is the check,
and the sheet test wants a clean re-run for a record with the stops found.
Board: GRBL mode, controller running, the idle lens posture, `/tmp`
clean; forgectrl b7bb2638 installed.

Reproduced with one frame burn on the used sheet (the operator's press,
witnessed: tube 1023, 153 LASER_ON samples, thermopile +954, lit 82 s)
and a dry finding right after: the fallback again, and this time the ring
in the log: 7984 15584 7041 21160 7983 15373 7569 17797 on the bottom leg
before the line ran out (the wizard's log line holds 92 characters). The
strong steps had come down from about 20000 to 15000 to 18000 and the
quiet ones up to 7000 to 8000 with the fans at their run duty after the
burn, so a strong step under the fixed 9000 called contact and the fixed
readability bar of twice 9000 then failed on a median of 17797. Fixed
thresholds were the wrong tool for unknown heads anyway. The rule is now
adaptive (`wizcalc_stop_call`, `wizcalc_ring_readable`, fifteen host
cases): the strong and quiet levels are the medians of each parity's
steps before the step judged, nothing is judged before the fifth step,
contact is a strong-parity step under the midpoint of the two levels, a
slip is any step over 1.8 times the strong level, and a ring is readable
when the strong level is at least 6000 summed and 1.8 times the quiet
level. The ring is logged in hundreds over two short lines per leg. On
the bench reference machine (forgectrl be3a9ded installed) the finder
then read the bottom leg as 73 207 60 196 73 167 70 196 66 164 73 192 90
166 48 40 (times 100) and called contact at 16, the top leg as 53 209 35
203 47 186 34 187 42 206 27 195 51 206 31 198 33 227 43 236 31 93 and
called it at 22, no slip. The fans had returned to idle by then; the case
a minute after a burn is proven by the sheet test itself, where the focus
card follows the frame, so the sheet's clean re-run is that proof.

The re-run on a third fresh sheet: PASS in 805 s (the presence press after
18 s, every burn witnessed, the settings restored), and the finder inside
it called the bottom stop at 4 half-steps below the edge, a false contact:
its ring, a minute after the frame with the fans at their run duty, read
98 172 64 198 108 140 (times 100); the strong level was taken as 198 (the
median of two picked the larger), the quiet level sat at 98 with the fans
up, and a free step at 140 fell under the midpoint. Two changes to the
rule (the strong level takes the lower middle of an even count, contact
needs a strong step under a quarter of the way up from the quiet level)
and a plausibility floor (a stop called nearer than 8 half-steps to the
edge is a misread, since the factory's own zero sits 8 below the edge on
every head), with the two real legs from the logs as host cases.

Then the operator stopped that line of work: "Why would you ever run this
while fans are running? No, the fans MUST be stopped before you run the
test. Every time." And on the how: no tach reading ("the user may have an
external fan running; the fans will still show that they are spinning as
the airflow moves over them"), a set wait after the fans are commanded
off, about ten seconds, nothing more complicated. As built: the cooling
engine gained a quiet hold (`cool_quiet_hold`: every fan and the purge to
zero, the engine's own fan writes go to zero while it stands, the phase's
posture and the purge back at the release), the finder takes it, waits ten
seconds, listens, and hands it back, whatever the outcome. On the bench
reference machine (forgectrl 167f85c5 installed) the dry finding then
read the bottom leg as 91 235 62 215 93 162 73 232 95 157 73 213 103 119
and called it at 14 (the carriage loads from about 14 on this head; 12
below stays inside the free travel), the top at 22, the fans back in
their idle posture after. During the hold the exhaust read zero and the
air assist ran down; the intake tachs kept their idle 725, which they
also show at the engine's idle posture of intake PWM zero: those fans run
at a floor with no PWM, or the bench's external airflow spins them. The
finder had worked at that same intake reading every time. The case a
minute after a burn is the next sheet run's to prove; the frame card that
was waiting at its press for an after-burn finding was aborted unburned.

The fourth sheet of the day, and the record: `commission.sheet` PASS in
827 s in campaign c-20260906172748-0b36 on forgectrl 167f85c5 (the
presence press after 21 s, every arm press by the bench actuator, every
burn witnessed: the tube at 1023, LASER_ON samples 145 to 255, the
thermopile +784 to +2097; the seven settings restored, the config
reading the operator's 3.48, 14, and 20 after it). Inside it the focus
card, two minutes after the frame burn, stopped every fan for ten
seconds and found the stops at 14 below and 20 above the edge, no slip,
the reach Z 2.1 to 13.8 mm on the test's pick; the card took 96 s with
the finding. The sheet's status: pass, satisfied; the after-burn case is
proven the way the operator ordered it. Board at the end: GRBL mode,
controller running, the exhaust in its cooldown after the flow-load card,
`/tmp` clean. Nothing is committed; the hold stands.

## 2026-09-06: a job's Z moves the lens, referenced only, inside the reach

The operator asked where a user sees the lens's reach and what happens past
it, and the answers exposed two things: the panel's Machine tab showed
none of the lens settings (a wrong claim of mine), and in GRBL mode a
sender's Z had never moved the lens at all: the driver's posture kept the
lens locked out of the pulse path (`motor_lock` 8, the factory's idle
posture, documented as such), so LightBurn's Z was bookkeeping and the
frame work of 2026-09-05 had been a coordinate frame over an axis that did
not move. The operator: "fully and completely unacceptable, and I thought
that was already fixed." As built the same afternoon: the driver puts
every axis in the pulse path (`motor_lock` 0, half-step mode, the lens's
drive current with the run currents and its hold current with the hold
currents); its Z soft limit is always on, whatever `$20` says, with Z
counted as referenced to where it stands until a reference exists (the
core checks the limit on homed axes only), so an unreferenced Z move is
refused (a jog with error 15, a program move with the soft-limit alarm
before it starts); a gfcloud home opens the envelope to the head's free
travel with a half-step of slack at each end, and a commissioning card,
which references the lens itself, tells the driver with `M103 Z<focal
height at the edge> P<free half-steps below> Q<above>` at the head of
every program (P and Q optional: the settings, else the fallback). The
stream harness checks that a 1 mm Z move after `M103` steps the lens three
up and three back (the fourth session of `laser_stream_test.py`). The
panel's Machine tab gained a Lens card (the park height, the focus at the
reference, the free travel counts, the reach in the user's units from
`/status`'s new `lens` block), the Motion card a "Lens reach" line, and Z
reads as unreferenced until a home; the status's Z scale is the screw's
(0.684 mm per full step, not the service's 0.706). The docs (the driver
page, LightBurn, motion-hardware), BRINGUP, and the plan follow; the sheet
test's covers gained the driver's homing and posture files.

Host-proven: the driver's build, lens test, lifecycle harness, and the
stream harness with the Z session (the arm test stubs the reference);
forgectrl with -Werror and its sheet, status, lid-gate, and wizcalc tests
(the status tests stub the settings store). Bench, on forgectrl 3aa24d60
and the driver 0aea0dda over one Grbl connection: an unreferenced
`$J=G91 Z-1` refused with error 15 and no motion; `$H` in 53 s to Z 3.08
with the panel homed at 3.08; `$J=G91 Z-2` took the lens off its hall edge
(the sensor left home) with the panel following to 1.03; `$J=G91 Z2` back
to 3.08 (the hall reads not-home at the park when reached from below, its
hysteresis); `Z-5` and `Z8` past the reach refused with error 15 and no
motion. The first driver build had the hold without the homed bit and
refused nothing, which the drill showed. Board at the end: GRBL mode,
homed at the park, `/tmp` clean.

The operator found the Lens card's two length fields showing millimeters
on a machine set to inches: the panel fills each field by name through
its unit conversion and the new fields were not in that list. Fixed
(forgectrl 60f7b92f), the placeholders in the user's units too.

The fifth sheet of the day, on the pair as it stands (forgectrl 60f7b92f,
the driver 0aea0dda): `commission.sheet` PASS in 818 s in a new campaign,
c-20260906201504-2a77 (the catalog changed with the covers). The presence
press, the actuator's arm presses, every burn witnessed (the tube at 997
to 1023, LASER_ON samples 143 to 255, the thermopile +757 to +2305), the
settings restored (the operator's 3.48, 14, and 20 stand). Inside it the
focus card found the stops at 14 and 20 with the fans stopped, and the
driver's log shows each card's `M103` taken: "lens referenced at Z6.84,
free 14 half-steps below and 20 above" three times, the test's own pick
while it ran. Board at the end: GRBL mode, controller running, `/tmp`
clean. Nothing is committed; the hold stands.

## 2026-09-06: commissioning phase 4, the lifecycle, built and proven

The plan's last phase, as built in forgectrl (plan section 6.16): the
what-changed menu on the Commissioning tab (a replaced tube, pump,
coolant, fan, head, or tray, or a service with a cover off, mapped by a
compiled table to the wizards that measured the old part, required, and
the ones that only prove it, recommended; `POST /wiz/changed`); the record
as a download named after the sheet id and as a printable page
(`recordhtml.c`, no script, every value escaped, the steps in catalog
order with their sentence, the settings written with the values before,
and the numbers) and inside the sanitized log bundle as
`system/commissioning.json`; the button LED choreography (white breathing
while a wizard holds the machine with the controller stopped, handed back
dark before any controller start, amber blinking while a check waits for
the lid to close); and the second-browser mirror (a run belongs to the
login session that started it; another session follows it, is refused an
answer or an abort with 409, and can take the run over). On the way:
`GET /wiz/record` used a 16 KB buffer against a bench record of 18.7 KB
on disk (11.6 KB compact); the record's routes now hand out a malloc'd
dump. The docs site (commissioning, control panel, forgectrl, logging),
the help entries, the dev server's mock, and the plan follow.

Host-proven: forgectrl under -Werror with `commission_test` (the change
table, the dump), the new `recordhtml_test`, `wizcalc_test` (the
ownership rule), `sheet_test`, `jobstream_test`, `users_test`; the mock's
15 (its `/status` gained the `lens` block the daemon has had since the
Lens card, a pre-existing gap); forgetest's `test_commission_suite` (31,
the three new cases registered) and `test_baseline` (30).

Bench, forgectrl e6bd6635 and the suite hot-deployed on image
20260904203403, campaign c-20260906210652-b9a0, every case started over
the forgetest API with the prerequisites overridden: `commission.what-changed`
PASS in 7 s (the menu of seven, the tray recommends the focus card, a fan
requires airflow with the gate held open by the dev image's override, an
unknown change 400, the record put back under one restart);
`commission.record-export` PASS in 37 s (the record 11558 bytes with 21
wizards, the download named `forgefirm-commissioning-<sheet id>.json`, the
page 16102 bytes with every completed wizard and no script, 403 without a
login or the token, the 3.0 MB bundle carrying the record at 18519 bytes
with the same sheet id); `commission.mirror` PASS in 18 s (session A owns
the sensors check, B mirrors, a tool with the token is never held back,
B's abort 409, B takes over, A no longer owns it, B's abort ends the
check aborted; the temporary account and both sessions gone at the end).
The LED read from sysfs at 2 Hz: through `commission.check-airflow` (PASS,
40 s) the three channels pulse at 1800 ms from the controller's stop to
its restart, then read dark; with the lid held open by the fixture and the
switches check started by hand, the red and green channels pulse at 340 ms
for the whole "Close the lid to begin" wait and read dark two seconds
after the abort; the lid was closed again by the fixture and the check
left no record. The sheet's own record still carries the two wizards
removed on 2026-09-05 (`motion.scale`, `sheet.record`); the page leaves
them out, since they are not in the catalog.

Found by the runs' baseline lines: the fixed `motor_lock` value in
`forgetest/baseline.py` was still 8 from before the Z work of this
afternoon, so every test since then reported a leftover of 0 and wrote 8
under the running controller, which locked the lens out of the pulse path
until the next controller start. The fixed value is 0, `motor_lock` is no
longer one of the configured markers (the probe and the controller both
leave it 0), and the motion suite's masked-restart case and the
acceptance page say 0; the baseline unit tests follow. Board at the end:
GRBL mode, controller running, lid closed, `/tmp` holds the previous
forgectrl as a volatile rollback, `/data` as found. Nothing is committed;
the hold stands until the agreements review.

## 2026-09-06: the web service sees ForgeFIRM/<version>

The User-Agent gfutilities presents to the Glowforge service is a
configuration key, `SERVICE.USER_AGENT` (`user_agent` under `[SERVICE]`);
unset, the library keeps its `OpenGlow/<factory firmware version>`
default. Both ForgeFIRM clients set it after the config is parsed:
`ffmachine.apply_user_agent()` reads the image stamp
`/etc/forgefirm-version`, drops a leading `v` before a digit, keeps the
dev image's `<timestamp> (dev)` form, and gives `ForgeFIRM/unknown` when
the stamp is unreadable; a non-empty `user_agent` in `gfhome.conf` wins.
The value carries on the HTTPS session and on the WebSocket handshake.
Host tests: gfutilities `tests/test_user_agent.py` (default, empty
value, override, session header; 34 pass with the lifecycle and firmware
policy modules) and python3-gfhardware `tests/test_ffmachine_agent.py`
(8 pass).

Bench 2026-09-06 21:33-21:35Z, dev image 20260904203403 hot-deployed
(the five files staged in `/tmp`, installed, the staging removed): the
GRBL-to-cloud switch through `POST /mode` came up running in a session
that logged `user agent: ForgeFIRM/20260904203403 (dev)`, sign-in
SUCCESS, the firmware check at the tested baseline, `RX-EVENT: ready`,
the hunt `:completed`, the four head-finding motions and five lid image
uploads, no error or warning. The service treats the new agent as it
treated the old one. Board at the end: cloud mode, session live, `/data`
as found. Nothing is committed; the change rides with the next push of
Glowforge-Utilities, python3-gfhardware, and the docs site.

## 2026-09-06: the agreements reviewed against the code, and three fixes

The four agreement documents (`forgectrl/docs/agreements/`) were checked
claim by claim against the code as built through commissioning phase 4.
Most claims held; the operator revised the texts for the rest. Three
findings were code, not text, and were fixed the same day, host-proven:

- **A hold verdict could be resumed dark.** The cooling client took the
  feed hold on AIRFLOW or CRITICAL (no resume for the session) but kept
  `hold_ours` set, so a button press, a `~`, or a sender's cycle start
  inside the 60 s disarm grace resumed motion with fire suppressed and
  the client never held again: the job ran dark to its end. Now the
  client holds again whenever the core is back in Cycle under a standing
  hold (fresh or stale), once per resume with "cooling hold stands - job
  held again; reset the job" (`glowforge_cooling.c`, `hold_take`).
  Proof: stream rule 24 (`laser_stream_test.py`, the null-sink build):
  the harness publishes the fail-tier verdict mid-line, the client holds,
  a `~` moves the head for at most one client poll with zero FIRE ticks,
  the client holds again and says so, and the clean verdict resumes the
  hold it took with the rest of the line lit.
- **The camera key was masked by luck.** The log-export sanitizer knew
  the panel token by value but the camera key only through the 32-hex
  pattern, which its length happened to satisfy. The key is now a known
  value (`logs.c` `load_known`, placeholder `CAMERA_KEY`).
- **`POST /settings` took `cloud_enabled=1` as a plain switch**, without
  the cloud step's typed phrase, and `cloud_enabled=0` there left
  `homing_mode=gfcloud` standing, which the driver's `$H` honors on its
  own. Now `1` from `0` takes `phrase=I UNDERSTAND` (400 without it;
  re-sending `1` while it stands asks nothing), and `0` takes
  `homing_mode` to `none` and `controller_mode` to `grbl` when they point
  at the cloud, logged, as the step does (`main.c` `cb_settings_post`,
  `WIZ_CLOUD_PHRASE` in `wiz.h`). The acceptance runner gives the phrase
  when it turns cloud mode on for a test that declares it (`baseline.py`);
  `commission.cloud-disabled-surface` turns cloud mode off with the one
  write, checks the sweep, checks the phrase-less `1` is refused, and
  restores with the phrase; the fake-daemon unit tests and the dev
  server's mock carry the same rules.

Proofs: grblHAL host build + the stream and lifecycle harnesses (rule 24
PASS: resumed dark for 0.60 s, held again, lit after the clear); forgectrl
host build with `-Werror` + the mock tests (16); forgetest unit tests (the
commission suite's 31, now green on a Windows host too: the record helper
writes with `O_BINARY` and the daemon-path helpers join with `posixpath`,
both no-ops on the board). Docs:
grblhal-driver (the cooling client), cooling-engine (the verdict), logging
(the sanitizer's known values), settings, forgectrl, and commissioning
(`cloud_enabled` through the API). Nothing committed: the commissioning
hold stands until the agreements are final.

**Bench 2026-09-06 23:17Z (image 20260904203403 dev):** forgectrl
`42000459` and the driver `8bfbe88b` installed by scp to `/tmp`, hash check,
`init.d stop`, cp, `chmod 755`, start; the three suite files (`baseline.py`,
`suite/commission.py`, `suite/logs.py`) installed and forgetest restarted
with the queue idle; both `.prev` rollbacks kept in `/tmp`, the staging
copies removed. The new daemon closed the controller gate at once with
"the agreements are not accepted": all four documents changed bytes, and
the override file does not lift that, as designed. `logs.tree-tail-export`
PASS (38 s) on the new daemon with the camera-key leak check.
The operator's walk to accept the four documents again found two page
defects on that path, fixed and installed the same hour (forgectrl
`a23d0a6d`, then `be23dd73`): the setup page counted the agreements step
done while the recorded press stood, whatever the documents' state, and
skipped it (`stepDone` now needs `agreements_complete` too); and the rail
linked only the re-runnable steps, so an open Agreements or Account entry
was dead and `/setup?step=agreements` was refused (any open step is a link
and is honored in the address). Then the path proved: the four documents
accepted at 23:33Z, the press at 23:34:11Z, the gate open, the GRBL
controller respawned with motion verified. `commission.cloud-disabled-surface`
PASS at 23:35Z on the new daemon: found `cloud_enabled=1` with
`homing_mode=gfcloud`; the one write `cloud_enabled=0` swept the homing
choice to `none` with the "(cloud mode off)" log line; `controller_mode=cloud`
409, `homing_mode=gfcloud` 409, `cloud_enabled=1` without the phrase 400;
`POST /mode controller=cloud` 409 naming cloud mode; the restore with the
phrase put all three settings back as found, the machine in GRBL mode
throughout. Board at the end: `/tmp` holds the two `.prev` rollbacks,
`/data` as found.

## 2026-09-06: the consent documents renamed from agreements to advisories

By the operator's order, late on 2026-09-06: the folder
`forgectrl/docs/agreements/` became `docs/advisories/`, then every route,
identifier, key, and name followed: `GET /advisories/<id>`,
`POST /wiz/advisories/accept|press|press/cancel`, `GET /wiz/advisories/press`;
`src/advisories.c` and `.h` (`advisory_t`, `advisories_find`, the
`advisories_blob.c` the embed step generates); the wizard step id and title;
the record's `advisories` block and the `advisories` wizard entry; the
`advisories_complete` flag in `GET /wiz`; the daemon's log lines; the
acceptance test `commission.advisories-rehash` with its `covers`; the dev
server's mock; the docs site (commissioning, control panel, forgectrl,
usage index, install); BRINGUP; the plan. The four document texts do not
contain the word, so their hashes and the recorded consent are unchanged.
Sensor-agreement prose in the cooling code and docs was left as written.

Proofs: forgectrl host build with `-Werror`, `commission_test` and
`recordhtml_test` PASS, the mock tests 16, the forgetest commission suite 31,
the catalog loading 81 tests with every prerequisite resolving. Bench: the
daemon stopped, the record's two keys renamed in place (a copy kept in
`/tmp/commissioning.json.prev`), forgectrl `25256609` installed with the
suite files, forgetest restarted with the queue idle: the gate stayed open on
the migrated consent, `GET /advisories/privacy` 200 with its ETag, the old
route gone, GRBL running. `commission.advisories-rehash` PASS (7 s) in the
new campaign c-20260906234523-4987 (the catalog hash moved), the record put
back, the gate open. Board at the end: `/tmp` holds the record copy and the
two `.prev` binaries, `/data` as found.

## Reference notes

### Head-IRQ source validation — the beam-emission hypothesis

- **Head-IRQ source validation — beam-emission hypothesis: OPEN
  (exploratory feature; NOT a first-light prerequisite).** The
  EV_SW `head` bit (GPIO3_22, factory pad name HEAD_IRQ; the
  panel's "Head sense" row) is the head MCU's attention line —
  idle LOW with a healthy head attached (measured 2026-08-08); it
  pulses on head reboot (hence the 60 ms DT debounce) and floats
  to the SoC pull-up with no head driving it, so the raw level is
  NOT a presence signal (presence = the head answering at I²C
  0x47). The factory app answers this IRQ by reading the head's
  interrupt flags over I²C, and the only flag register is the reg
  0x05 RO group — bit0 hall_sensor, bit1 accel_irq, bit2
  beam_detect_digital (head_private.h) — so there are exactly
  three candidate IRQ sources; working hypothesis (operator): the
  in-cut source is the head's IR beam-emission detector — digital
  flag 0x05 b2 + analog level reg 0x16 (both already head sysfs
  attrs), tunable detection model at regs 0x22–0x2a
  (lambda_k/lambda_t/theta_r/theta_t/e_t = the factory
  BDlk/BDlt/BDtr/BDtt/BDet settings; regs defined in
  head_private.h, not yet exposed as attrs).
  Priority/scope (operator, 2026-08-08): later exploration, not a
  must-have —
  - The bench head is **gen2** (a first-round Kickstarter unit
    already shipped gen2). Gen1 heads are presumed rare to
    nonexistent in the wild, though the factory images still
    support them, so some must be assumed to exist. The gen1
    board-level beam chain (!BEAM_DET GPIO4_15, !BEAM_DET_XOR
    GPIO4_08, !BEAM_DET_TIMEOUT GPIO4_07, BEAM_DET_ERR GPIO4_10 —
    DT-pinmuxed, not driver-requested; BEAM_DET_LATCH_RST GPIO7_13
    pulsed at cut start, boards v13/v14 only) is documented here
    as legacy reference only.
  - Whether the factory actually USES beam detect is unknown. The
    v2.6.0 factory app carries a complete but config-gated
    subsystem (separate printing/idle enables, severities
    failing-abort / pausing-alert / silent-alert, level-vs-edge
    trigger option, beam_detect_irq + irq_override, fault report
    upload; an invalid severity defaults to DISABLED), so the
    plumbing exists but production enablement is an open question.
    Detection at low fire energies is also unverified — the sensor
    may simply not trip on a low-power pulse.
  - Same status for the accelerometer: a promo-touted factory
    feature that was not active in early releases and may not be
    today. Its data path is direct (lis2hh12 on the I²C bus) but
    its INT pin routes to the head MCU as flag 0x05 b1, so it is
    also a head-IRQ source.
  Cheap opportunistic check during live-fire bring-up (no gating):
  log EV_SW head-bit edges + head/beam_detect_digital/_analog
  while firing — if the beam flag level-holds the IRQ, the panel
  row asserts during sustained emission. Later-feature decisions
  if it pans out: beam-absent-while-FIRE as an optional fault
  input, attrs for the calibration regs, panel row relabel (e.g.
  "Head IRQ / emission").

### Homing: limit switches planned, the accelerometer approach retired

- Limit-switch homing remains the planned second method; the
  accelerometer approach stays retired (implementation and bench
  record in grblHAL-glowforge history before commit 26298a3;
  durable accel/rail-contact measurements below).
Durable measurements from the accelerometer spike (relevant to any
future contact/vibration sensing; tools `accel_fast.py`,
`bump_seek.py` remain in scripts/bench):
- **Sensors**: the HEAD accel (lis2hh12) is **i2c-3 addr 0x1e**
  (0x1d on the same bus is a static board part; i2c-0 0x1e is the
  lid). st_accel sysfs one-shots are ~6 Hz and the kernel has no
  IIO triggers; direct I2C (unbind st-accel, CTRL1=0x6F = 800 Hz
  ODR) reads ~530 Hz from Python.
- **Rail-contact signature**: creep baseline ≈0.5–2 k counts;
  contact jumps to 29–42 k within ~4 ms (20–40×). But **slow
  approaches are near-silent** — belt compliance turns slow-speed
  skipping into sub-threshold grinding — so any contact-sensing
  scheme must strike fast.
