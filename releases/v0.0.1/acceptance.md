# ForgeFIRM acceptance - 20260909150456 (dev)

- Image: `20260909150456 (dev)` (forgefirm-image-dev)
- Manifest identity: `58b4d750b4244589ef26d802a50300c38bfa868fc460448386b4b530e100cf84`
- Catalog: `e2a2fe18e70fee24f81780b33c7210a530bb6d3f5267503ed8a97ee493944241`
- Campaign: `c-20260909160235-7649` opened 2026-09-09T16:02:35Z
- Exported: 2026-09-09T17:13:20Z
- **Release authorized: YES**
- Tests: 83 total, 83 satisfied (0 inherited), 0 required

| Test | Kind | Result | Run at | Campaign | Inherited |
|---|---|---|---|---|---|
| `image.health` | auto, core | PASS | 2026-09-09T16:02:36Z | `c-20260909160235-7649` | no |
| `image.license-bundle` | auto | PASS | 2026-09-09T16:02:40Z | `c-20260909160235-7649` | no |
| `kernel.latch-locked-idle` | auto, core | PASS | 2026-09-09T16:02:41Z | `c-20260909160235-7649` | no |
| `kernel.k1-k2` | auto, core, takeover | PASS | 2026-09-09T16:03:03Z | `c-20260909160235-7649` | no |
| `kernel.deadman-close` | auto, core, takeover | PASS | 2026-09-09T16:03:13Z | `c-20260909160235-7649` | no |
| `kernel.backtrack-bounds` | auto, core, takeover | PASS | 2026-09-09T16:03:31Z | `c-20260909160235-7649` | no |
| `kernel.fire-line` | auto, core, takeover | PASS | 2026-09-09T16:04:15Z | `c-20260909160235-7649` | no |
| `kernel.resume-lead` | auto, takeover | PASS | 2026-09-09T16:04:37Z | `c-20260909160235-7649` | no |
| `kernel.pic-soc-load` | auto | PASS | 2026-09-09T16:04:41Z | `c-20260909160235-7649` | no |
| `forgectrl.auth` | auto | PASS | 2026-09-09T16:04:42Z | `c-20260909160235-7649` | no |
| `forgectrl.settings-bounds` | auto | PASS | 2026-09-09T16:04:43Z | `c-20260909160235-7649` | no |
| `forgectrl.panel-serves` | auto | PASS | 2026-09-09T16:02:37Z | `c-20260909160235-7649` | no |
| `commission.gate-blocks-controllers` | auto, takeover | PASS | 2026-09-09T16:05:01Z | `c-20260909160235-7649` | no |
| `commission.override-until-reboot` | auto | PASS | 2026-09-09T16:05:02Z | `c-20260909160235-7649` | no |
| `commission.advisories-rehash` | auto, takeover | PASS | 2026-09-09T16:05:11Z | `c-20260909160235-7649` | no |
| `commission.account-login` | auto, takeover | PASS | 2026-09-09T16:06:01Z | `c-20260909160235-7649` | no |
| `commission.https-only-writes` | auto | PASS | 2026-09-09T16:06:01Z | `c-20260909160235-7649` | no |
| `commission.ssh-until-reboot` | auto | PASS | 2026-09-09T16:06:03Z | `c-20260909160235-7649` | no |
| `commission.cloud-disabled-surface` | auto | PASS | 2026-09-09T16:06:03Z | `c-20260909160235-7649` | no |
| `commission.factory-return` | auto | PASS | 2026-09-09T16:06:06Z | `c-20260909160235-7649` | no |
| `commission.mdns-announce` | auto | PASS | 2026-09-09T16:06:06Z | `c-20260909160235-7649` | no |
| `commission.first-run-flow` | operator, takeover | PASS | 2026-09-09T16:06:28Z | `c-20260909160235-7649` | no |
| `commission.first-run-page` | operator, takeover | PASS | 2026-09-09T16:36:40Z | `c-20260909160235-7649` | no |
| `commission.cert-page` | auto | PASS | 2026-09-09T16:06:28Z | `c-20260909160235-7649` | no |
| `commission.what-changed` | auto, takeover | PASS | 2026-09-09T16:06:37Z | `c-20260909160235-7649` | no |
| `commission.record-export` | auto | PASS | 2026-09-09T16:07:57Z | `c-20260909160235-7649` | no |
| `commission.mirror` | auto, takeover | PASS | 2026-09-09T16:08:30Z | `c-20260909160235-7649` | no |
| `commission.check-switches` | operator | PASS | 2026-09-09T16:08:35Z | `c-20260909160235-7649` | no |
| `commission.check-sensors` | auto | PASS | 2026-09-09T16:08:10Z | `c-20260909160235-7649` | no |
| `commission.check-airflow` | auto, takeover | PASS | 2026-09-09T16:10:07Z | `c-20260909160235-7649` | no |
| `commission.check-cameras` | operator | PASS | 2026-09-09T16:10:38Z | `c-20260909160235-7649` | no |
| `commission.check-motion` | auto, takeover | PASS | 2026-09-09T16:11:19Z | `c-20260909160235-7649` | no |
| `commission.check-flow-verify` | auto, takeover | PASS | 2026-09-09T16:18:38Z | `c-20260909160235-7649` | no |
| `commission.cloud-header-capture` | operator, takeover | PASS | 2026-09-09T16:37:55Z | `c-20260909160235-7649` | no |
| `commission.sheet` | live, takeover | PASS | 2026-09-09T16:54:01Z | `c-20260909160235-7649` | no |
| `logs.tree-tail-export` | auto | PASS | 2026-09-09T16:07:17Z | `c-20260909160235-7649` | no |
| `logs.routing` | auto | PASS | 2026-09-09T16:18:42Z | `c-20260909160235-7649` | no |
| `logs.level-settings` | auto | PASS | 2026-09-09T16:18:42Z | `c-20260909160235-7649` | no |
| `motion.pacing` | auto | PASS | 2026-09-09T16:10:53Z | `c-20260909160235-7649` | no |
| `motion.jog-roundtrip` | auto | PASS | 2026-09-09T16:18:56Z | `c-20260909160235-7649` | no |
| `motion.microstep-modes` | auto | PASS | 2026-09-09T16:19:20Z | `c-20260909160235-7649` | no |
| `motion.liveness-probe` | auto | PASS | 2026-09-09T16:19:29Z | `c-20260909160235-7649` | no |
| `motion.gate-waits-for-lid` | operator | PASS | 2026-09-09T16:19:45Z | `c-20260909160235-7649` | no |
| `motion.cancel-abort` | auto | PASS | 2026-09-09T16:19:52Z | `c-20260909160235-7649` | no |
| `motion.deadman` | auto | PASS | 2026-09-09T16:20:21Z | `c-20260909160235-7649` | no |
| `motion.button-hold-resume` | operator | PASS | 2026-09-09T16:20:32Z | `c-20260909160235-7649` | no |
| `motion.lid-cancel-home` | operator | PASS | 2026-09-09T16:20:48Z | `c-20260909160235-7649` | no |
| `motion.interlock-cancel-home` | operator | PASS | 2026-09-09T16:20:57Z | `c-20260909160235-7649` | no |
| `motion.lid-policy-hold` | operator | PASS | 2026-09-09T16:21:11Z | `c-20260909160235-7649` | no |
| `motion.step-timing-under-load` | auto | PASS | 2026-09-09T16:21:41Z | `c-20260909160235-7649` | no |
| `cooling.aa-offset-calibrate` | auto | PASS | 2026-09-09T16:12:52Z | `c-20260909160235-7649` | no |
| `cooling.flow-verify` | auto | PASS | 2026-09-09T16:15:33Z | `c-20260909160235-7649` | no |
| `cooling.fans-quiet-after-motion` | auto | PASS | 2026-09-09T16:22:37Z | `c-20260909160235-7649` | no |
| `cooling.gate-off` | auto | PASS | 2026-09-09T16:08:43Z | `c-20260909160235-7649` | no |
| `cooling.floor-and-warm-up` | auto | PASS | 2026-09-09T16:23:58Z | `c-20260909160235-7649` | no |
| `cooling.tec-drive` | auto | PASS | 2026-09-09T16:24:06Z | `c-20260909160235-7649` | no |
| `cooling.fire-watch-tiers` | auto | PASS | 2026-09-09T16:24:22Z | `c-20260909160235-7649` | no |
| `cooling.crash-watch-plumbing` | auto | PASS | 2026-09-09T16:24:29Z | `c-20260909160235-7649` | no |
| `cooling.critical-tier` | auto | PASS | 2026-09-09T16:24:38Z | `c-20260909160235-7649` | no |
| `cooling.fan-gate-trips` | auto | PASS | 2026-09-09T16:09:27Z | `c-20260909160235-7649` | no |
| `cooling.flow-under-load` | live | PASS | 2026-09-09T16:55:46Z | `c-20260909160235-7649` | no |
| `laser.power-floor` | auto | PASS | 2026-09-09T16:24:39Z | `c-20260909160235-7649` | no |
| `laser.emission-witness` | live, core | PASS | 2026-09-09T16:39:25Z | `c-20260909160235-7649` | no |
| `laser.m5-rapid-dark` | live | PASS | 2026-09-09T16:56:44Z | `c-20260909160235-7649` | no |
| `laser.disarm-in-hold` | live | PASS | 2026-09-09T16:58:24Z | `c-20260909160235-7649` | no |
| `laser.armed-kill` | live | PASS | 2026-09-09T16:59:11Z | `c-20260909160235-7649` | no |
| `laser.arm-wait-lid` | operator | PASS | 2026-09-09T16:24:41Z | `c-20260909160235-7649` | no |
| `laser.pause-resume-lid-cancel` | live | PASS | 2026-09-09T16:59:42Z | `c-20260909160235-7649` | no |
| `camera.snapshot` | auto | PASS | 2026-09-09T16:10:23Z | `c-20260909160235-7649` | no |
| `camera.sensor-profile` | auto | PASS | 2026-09-09T16:24:55Z | `c-20260909160235-7649` | no |
| `camera.h264-stream` | auto | PASS | 2026-09-09T16:25:08Z | `c-20260909160235-7649` | no |
| `camera.frame-health` | auto | PASS | 2026-09-09T16:25:21Z | `c-20260909160235-7649` | no |
| `camera.lid-privacy` | operator | PASS | 2026-09-09T16:25:36Z | `c-20260909160235-7649` | no |
| `camera.key-read` | auto | PASS | 2026-09-09T16:25:37Z | `c-20260909160235-7649` | no |
| `update.slots-and-signature` | auto | PASS | 2026-09-09T16:06:05Z | `c-20260909160235-7649` | no |
| `cloud.mode-switch` | operator | PASS | 2026-09-09T16:27:18Z | `c-20260909160235-7649` | no |
| `cloud.service-protocol` | operator | PASS | 2026-09-09T17:00:56Z | `c-20260909160235-7649` | no |
| `cloud.lid-interlock-abort` | live | PASS | 2026-09-09T17:02:05Z | `c-20260909160235-7649` | no |
| `cloud.lid-during-button-wait` | operator | PASS | 2026-09-09T16:27:57Z | `c-20260909160235-7649` | no |
| `cloud.verdict-hold` | operator | PASS | 2026-09-09T16:33:14Z | `c-20260909160235-7649` | no |
| `cloud.pause-resume` | live | PASS | 2026-09-09T17:03:58Z | `c-20260909160235-7649` | no |
| `cloud.oversize-stream` | live | PASS | 2026-09-09T17:06:44Z | `c-20260909160235-7649` | no |
| `cloud.paused-lid-cancel` | live | PASS | 2026-09-09T17:07:12Z | `c-20260909160235-7649` | no |

Artifact sha256: `0984d4f8517148a64cbc9ddf1271f0371df890e15318f33ad5433567fec30a0f`
