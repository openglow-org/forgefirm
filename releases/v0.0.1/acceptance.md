# ForgeFIRM acceptance - 20260909193551 (dev)

- Image: `20260909193551 (dev)` (forgefirm-image-dev)
- Manifest identity: `d02c25b76481a559c9a67df278a181ed16912bd20c7ce7eb60f93ca140b1a64d`
- Catalog: `e2a2fe18e70fee24f81780b33c7210a530bb6d3f5267503ed8a97ee493944241`
- Campaign: `c-20260909204732-3fa9` opened 2026-09-09T20:47:32Z
- Exported: 2026-09-09T21:55:08Z
- **Release authorized: YES**
- Tests: 83 total, 83 satisfied (62 inherited), 0 required

| Test | Kind | Result | Run at | Campaign | Inherited |
|---|---|---|---|---|---|
| `image.health` | auto, core | PASS | 2026-09-09T20:54:35Z | `c-20260909204732-3fa9` | no |
| `image.license-bundle` | auto | PASS | 2026-09-09T20:01:00Z | `c-20260909200054-7947` | yes |
| `kernel.latch-locked-idle` | auto, core | PASS | 2026-09-09T20:54:36Z | `c-20260909204732-3fa9` | no |
| `kernel.k1-k2` | auto, core, takeover | PASS | 2026-09-09T20:54:59Z | `c-20260909204732-3fa9` | no |
| `kernel.deadman-close` | auto, core, takeover | PASS | 2026-09-09T20:55:09Z | `c-20260909204732-3fa9` | no |
| `kernel.backtrack-bounds` | auto, core, takeover | PASS | 2026-09-09T20:55:28Z | `c-20260909204732-3fa9` | no |
| `kernel.fire-line` | auto, core, takeover | PASS | 2026-09-09T20:56:12Z | `c-20260909204732-3fa9` | no |
| `kernel.resume-lead` | auto, takeover | PASS | 2026-09-09T20:02:56Z | `c-20260909200054-7947` | yes |
| `kernel.pic-soc-load` | auto | PASS | 2026-09-09T20:03:00Z | `c-20260909200054-7947` | yes |
| `forgectrl.auth` | auto | PASS | 2026-09-09T20:03:01Z | `c-20260909200054-7947` | yes |
| `forgectrl.settings-bounds` | auto | PASS | 2026-09-09T20:03:01Z | `c-20260909200054-7947` | yes |
| `forgectrl.panel-serves` | auto | PASS | 2026-09-09T20:00:57Z | `c-20260909200054-7947` | yes |
| `commission.gate-blocks-controllers` | auto, takeover | PASS | 2026-09-09T20:03:20Z | `c-20260909200054-7947` | yes |
| `commission.override-until-reboot` | auto | PASS | 2026-09-09T20:03:20Z | `c-20260909200054-7947` | yes |
| `commission.advisories-rehash` | auto, takeover | PASS | 2026-09-09T20:03:29Z | `c-20260909200054-7947` | yes |
| `commission.account-login` | auto, takeover | PASS | 2026-09-09T20:04:19Z | `c-20260909200054-7947` | yes |
| `commission.https-only-writes` | auto | PASS | 2026-09-09T20:04:19Z | `c-20260909200054-7947` | yes |
| `commission.ssh-until-reboot` | auto | PASS | 2026-09-09T20:04:20Z | `c-20260909200054-7947` | yes |
| `commission.cloud-disabled-surface` | auto | PASS | 2026-09-09T20:04:21Z | `c-20260909200054-7947` | yes |
| `commission.factory-return` | auto | PASS | 2026-09-09T20:04:23Z | `c-20260909200054-7947` | yes |
| `commission.mdns-announce` | auto | PASS | 2026-09-09T20:04:24Z | `c-20260909200054-7947` | yes |
| `commission.first-run-flow` | operator, takeover | PASS | 2026-09-09T20:04:46Z | `c-20260909200054-7947` | yes |
| `commission.first-run-page` | operator, takeover | PASS | 2026-09-09T20:58:30Z | `c-20260909204732-3fa9` | no |
| `commission.cert-page` | auto | PASS | 2026-09-09T20:04:47Z | `c-20260909200054-7947` | yes |
| `commission.what-changed` | auto, takeover | PASS | 2026-09-09T20:04:56Z | `c-20260909200054-7947` | yes |
| `commission.record-export` | auto | PASS | 2026-09-09T20:06:17Z | `c-20260909200054-7947` | yes |
| `commission.mirror` | auto, takeover | PASS | 2026-09-09T20:06:49Z | `c-20260909200054-7947` | yes |
| `commission.check-switches` | operator | PASS | 2026-09-09T20:06:54Z | `c-20260909200054-7947` | yes |
| `commission.check-sensors` | auto | PASS | 2026-09-09T20:06:29Z | `c-20260909200054-7947` | yes |
| `commission.check-airflow` | auto, takeover | PASS | 2026-09-09T20:08:28Z | `c-20260909200054-7947` | yes |
| `commission.check-cameras` | operator | PASS | 2026-09-09T20:08:59Z | `c-20260909200054-7947` | yes |
| `commission.check-motion` | auto, takeover | PASS | 2026-09-09T20:09:40Z | `c-20260909200054-7947` | yes |
| `commission.check-flow-verify` | auto, takeover | PASS | 2026-09-09T20:17:05Z | `c-20260909200054-7947` | yes |
| `commission.cloud-header-capture` | operator, takeover | PASS | 2026-09-09T21:26:48Z | `c-20260909204732-3fa9` | no |
| `commission.sheet` | live, takeover | PASS | 2026-09-09T21:41:23Z | `c-20260909204732-3fa9` | no |
| `logs.tree-tail-export` | auto | PASS | 2026-09-09T20:05:36Z | `c-20260909200054-7947` | yes |
| `logs.routing` | auto | PASS | 2026-09-09T20:17:09Z | `c-20260909200054-7947` | yes |
| `logs.level-settings` | auto | PASS | 2026-09-09T20:17:09Z | `c-20260909200054-7947` | yes |
| `motion.pacing` | auto | PASS | 2026-09-09T20:09:14Z | `c-20260909200054-7947` | yes |
| `motion.jog-roundtrip` | auto | PASS | 2026-09-09T20:17:23Z | `c-20260909200054-7947` | yes |
| `motion.microstep-modes` | auto | PASS | 2026-09-09T20:17:48Z | `c-20260909200054-7947` | yes |
| `motion.liveness-probe` | auto | PASS | 2026-09-09T20:17:56Z | `c-20260909200054-7947` | yes |
| `motion.gate-waits-for-lid` | operator | PASS | 2026-09-09T20:18:12Z | `c-20260909200054-7947` | yes |
| `motion.cancel-abort` | auto | PASS | 2026-09-09T20:18:19Z | `c-20260909200054-7947` | yes |
| `motion.deadman` | auto | PASS | 2026-09-09T20:18:49Z | `c-20260909200054-7947` | yes |
| `motion.button-hold-resume` | operator | PASS | 2026-09-09T20:19:00Z | `c-20260909200054-7947` | yes |
| `motion.lid-cancel-home` | operator | PASS | 2026-09-09T20:19:16Z | `c-20260909200054-7947` | yes |
| `motion.interlock-cancel-home` | operator | PASS | 2026-09-09T20:19:25Z | `c-20260909200054-7947` | yes |
| `motion.lid-policy-hold` | operator | PASS | 2026-09-09T20:19:38Z | `c-20260909200054-7947` | yes |
| `motion.step-timing-under-load` | auto | PASS | 2026-09-09T20:20:09Z | `c-20260909200054-7947` | yes |
| `cooling.aa-offset-calibrate` | auto | PASS | 2026-09-09T20:11:13Z | `c-20260909200054-7947` | yes |
| `cooling.flow-verify` | auto | PASS | 2026-09-09T20:13:58Z | `c-20260909200054-7947` | yes |
| `cooling.fans-quiet-after-motion` | auto | PASS | 2026-09-09T20:21:04Z | `c-20260909200054-7947` | yes |
| `cooling.gate-off` | auto | PASS | 2026-09-09T20:07:02Z | `c-20260909200054-7947` | yes |
| `cooling.floor-and-warm-up` | auto | PASS | 2026-09-09T20:22:31Z | `c-20260909200054-7947` | yes |
| `cooling.tec-drive` | auto | PASS | 2026-09-09T20:22:39Z | `c-20260909200054-7947` | yes |
| `cooling.fire-watch-tiers` | auto | PASS | 2026-09-09T20:22:55Z | `c-20260909200054-7947` | yes |
| `cooling.crash-watch-plumbing` | auto | PASS | 2026-09-09T20:23:02Z | `c-20260909200054-7947` | yes |
| `cooling.critical-tier` | auto | PASS | 2026-09-09T20:23:11Z | `c-20260909200054-7947` | yes |
| `cooling.fan-gate-trips` | auto | PASS | 2026-09-09T20:07:48Z | `c-20260909200054-7947` | yes |
| `cooling.flow-under-load` | live | PASS | 2026-09-09T21:43:23Z | `c-20260909204732-3fa9` | no |
| `laser.power-floor` | auto | PASS | 2026-09-09T20:23:11Z | `c-20260909200054-7947` | yes |
| `laser.emission-witness` | live, core | PASS | 2026-09-09T21:27:32Z | `c-20260909204732-3fa9` | no |
| `laser.m5-rapid-dark` | live | PASS | 2026-09-09T21:44:10Z | `c-20260909204732-3fa9` | no |
| `laser.disarm-in-hold` | live | PASS | 2026-09-09T21:45:37Z | `c-20260909204732-3fa9` | no |
| `laser.armed-kill` | live | PASS | 2026-09-09T21:46:25Z | `c-20260909204732-3fa9` | no |
| `laser.arm-wait-lid` | operator | PASS | 2026-09-09T20:23:30Z | `c-20260909200054-7947` | yes |
| `laser.pause-resume-lid-cancel` | live | PASS | 2026-09-09T21:46:57Z | `c-20260909204732-3fa9` | no |
| `camera.snapshot` | auto | PASS | 2026-09-09T20:08:44Z | `c-20260909200054-7947` | yes |
| `camera.sensor-profile` | auto | PASS | 2026-09-09T20:23:43Z | `c-20260909200054-7947` | yes |
| `camera.h264-stream` | auto | PASS | 2026-09-09T20:23:57Z | `c-20260909200054-7947` | yes |
| `camera.frame-health` | auto | PASS | 2026-09-09T20:24:09Z | `c-20260909200054-7947` | yes |
| `camera.lid-privacy` | operator | PASS | 2026-09-09T20:24:25Z | `c-20260909200054-7947` | yes |
| `camera.key-read` | auto | PASS | 2026-09-09T20:24:26Z | `c-20260909200054-7947` | yes |
| `update.slots-and-signature` | auto | PASS | 2026-09-09T20:04:23Z | `c-20260909200054-7947` | yes |
| `cloud.mode-switch` | operator | PASS | 2026-09-09T20:26:09Z | `c-20260909200054-7947` | yes |
| `cloud.service-protocol` | operator | PASS | 2026-09-09T21:47:53Z | `c-20260909204732-3fa9` | no |
| `cloud.lid-interlock-abort` | live | PASS | 2026-09-09T21:00:28Z | `c-20260909204732-3fa9` | no |
| `cloud.lid-during-button-wait` | operator | PASS | 2026-09-09T20:26:47Z | `c-20260909200054-7947` | yes |
| `cloud.verdict-hold` | operator | PASS | 2026-09-09T20:50:39Z | `c-20260909204732-3fa9` | no |
| `cloud.pause-resume` | live | PASS | 2026-09-09T21:49:34Z | `c-20260909204732-3fa9` | no |
| `cloud.oversize-stream` | live | PASS | 2026-09-09T21:03:24Z | `c-20260909204732-3fa9` | no |
| `cloud.paused-lid-cancel` | live | PASS | 2026-09-09T21:04:09Z | `c-20260909204732-3fa9` | no |

Artifact sha256: `2aa22f306e56792a8120de6d3b2b6817162f011544099da0c37540a0dae90b09`
