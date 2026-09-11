# ForgeFIRM acceptance - 20260911172215 (dev)

- Image: `20260911172215 (dev)` (forgefirm-image-dev)
- Manifest identity: `fb24c3f4508ae54592f8d0b98ed0b2a07a933274980d8c1e4e5a967618ef9bd8`
- Catalog: `ceabc830345fc4cac4832de691c0571e93a5c4e155666ce91601c844c40942fc`
- Campaign: `c-20260911182256-078f` opened 2026-09-11T18:22:56Z
- Exported: 2026-09-11T18:54:20Z
- **Release authorized: YES**
- Tests: 83 total, 83 satisfied (57 inherited), 0 required

| Test | Kind | Result | Run at | Campaign | Inherited |
|---|---|---|---|---|---|
| `image.health` | auto, core | PASS | 2026-09-11T18:22:57Z | `c-20260911182256-078f` | no |
| `image.license-bundle` | auto | PASS | 2026-09-11T17:29:26Z | `c-20260911172921-9426` | yes |
| `kernel.latch-locked-idle` | auto, core | PASS | 2026-09-11T18:22:58Z | `c-20260911182256-078f` | no |
| `kernel.k1-k2` | auto, core, takeover | PASS | 2026-09-11T18:23:19Z | `c-20260911182256-078f` | no |
| `kernel.deadman-close` | auto, core, takeover | PASS | 2026-09-11T18:23:29Z | `c-20260911182256-078f` | no |
| `kernel.backtrack-bounds` | auto, core, takeover | PASS | 2026-09-11T18:23:48Z | `c-20260911182256-078f` | no |
| `kernel.fire-line` | auto, core, takeover | PASS | 2026-09-11T18:24:32Z | `c-20260911182256-078f` | no |
| `kernel.resume-lead` | auto, takeover | PASS | 2026-09-11T17:31:23Z | `c-20260911172921-9426` | yes |
| `kernel.pic-soc-load` | auto | PASS | 2026-09-11T17:31:26Z | `c-20260911172921-9426` | yes |
| `forgectrl.auth` | auto | PASS | 2026-09-11T17:31:27Z | `c-20260911172921-9426` | yes |
| `forgectrl.settings-bounds` | auto | PASS | 2026-09-11T17:31:27Z | `c-20260911172921-9426` | yes |
| `forgectrl.panel-serves` | auto | PASS | 2026-09-11T17:29:23Z | `c-20260911172921-9426` | yes |
| `commission.gate-blocks-controllers` | auto, takeover | PASS | 2026-09-11T17:31:46Z | `c-20260911172921-9426` | yes |
| `commission.override-until-reboot` | auto | PASS | 2026-09-11T17:31:46Z | `c-20260911172921-9426` | yes |
| `commission.advisories-rehash` | auto, takeover | PASS | 2026-09-11T17:31:55Z | `c-20260911172921-9426` | yes |
| `commission.account-login` | auto, takeover | PASS | 2026-09-11T17:32:44Z | `c-20260911172921-9426` | yes |
| `commission.https-only-writes` | auto | PASS | 2026-09-11T17:32:45Z | `c-20260911172921-9426` | yes |
| `commission.ssh-until-reboot` | auto | PASS | 2026-09-11T17:32:46Z | `c-20260911172921-9426` | yes |
| `commission.cloud-disabled-surface` | auto | PASS | 2026-09-11T17:32:47Z | `c-20260911172921-9426` | yes |
| `commission.factory-return` | auto | PASS | 2026-09-11T17:32:49Z | `c-20260911172921-9426` | yes |
| `commission.machine-name` | auto | PASS | 2026-09-11T17:32:50Z | `c-20260911172921-9426` | yes |
| `commission.first-run-flow` | operator, takeover | PASS | 2026-09-11T17:54:32Z | `c-20260911172921-9426` | yes |
| `commission.first-run-page` | operator, takeover | PASS | 2026-09-11T17:55:49Z | `c-20260911172921-9426` | yes |
| `commission.cert-page` | auto | PASS | 2026-09-11T17:32:50Z | `c-20260911172921-9426` | yes |
| `commission.what-changed` | auto, takeover | PASS | 2026-09-11T17:32:59Z | `c-20260911172921-9426` | yes |
| `commission.record-export` | auto | PASS | 2026-09-11T17:34:22Z | `c-20260911172921-9426` | yes |
| `commission.mirror` | auto, takeover | PASS | 2026-09-11T17:34:54Z | `c-20260911172921-9426` | yes |
| `commission.check-switches` | operator | PASS | 2026-09-11T17:56:07Z | `c-20260911172921-9426` | yes |
| `commission.check-sensors` | auto | PASS | 2026-09-11T17:34:34Z | `c-20260911172921-9426` | yes |
| `commission.check-airflow` | auto, takeover | PASS | 2026-09-11T17:36:26Z | `c-20260911172921-9426` | yes |
| `commission.check-cameras` | operator | PASS | 2026-09-11T17:56:22Z | `c-20260911172921-9426` | yes |
| `commission.check-motion` | auto, takeover | PASS | 2026-09-11T17:37:06Z | `c-20260911172921-9426` | yes |
| `commission.check-flow-verify` | auto, takeover | PASS | 2026-09-11T17:44:42Z | `c-20260911172921-9426` | yes |
| `commission.cloud-header-capture` | operator, takeover | PASS | 2026-09-11T17:59:41Z | `c-20260911172921-9426` | yes |
| `commission.sheet` | live, takeover | PASS | 2026-09-11T18:17:29Z | `c-20260911172921-9426` | yes |
| `logs.tree-tail-export` | auto | PASS | 2026-09-11T17:33:40Z | `c-20260911172921-9426` | yes |
| `logs.routing` | auto | PASS | 2026-09-11T17:44:46Z | `c-20260911172921-9426` | yes |
| `logs.level-settings` | auto | PASS | 2026-09-11T17:44:46Z | `c-20260911172921-9426` | yes |
| `motion.pacing` | auto | PASS | 2026-09-11T17:36:41Z | `c-20260911172921-9426` | yes |
| `motion.jog-roundtrip` | auto | PASS | 2026-09-11T17:45:00Z | `c-20260911172921-9426` | yes |
| `motion.microstep-modes` | auto | PASS | 2026-09-11T17:45:25Z | `c-20260911172921-9426` | yes |
| `motion.liveness-probe` | auto | PASS | 2026-09-11T17:45:33Z | `c-20260911172921-9426` | yes |
| `motion.gate-waits-for-lid` | operator | PASS | 2026-09-11T18:24:48Z | `c-20260911182256-078f` | no |
| `motion.cancel-abort` | auto | PASS | 2026-09-11T17:45:40Z | `c-20260911172921-9426` | yes |
| `motion.deadman` | auto | PASS | 2026-09-11T17:46:09Z | `c-20260911172921-9426` | yes |
| `motion.button-hold-resume` | operator | PASS | 2026-09-11T18:24:59Z | `c-20260911182256-078f` | no |
| `motion.lid-cancel-home` | operator | PASS | 2026-09-11T18:25:15Z | `c-20260911182256-078f` | no |
| `motion.interlock-cancel-home` | operator | PASS | 2026-09-11T18:25:24Z | `c-20260911182256-078f` | no |
| `motion.lid-policy-hold` | operator | PASS | 2026-09-11T18:25:37Z | `c-20260911182256-078f` | no |
| `motion.step-timing-under-load` | auto | PASS | 2026-09-11T17:46:40Z | `c-20260911172921-9426` | yes |
| `cooling.aa-offset-calibrate` | auto | PASS | 2026-09-11T17:38:38Z | `c-20260911172921-9426` | yes |
| `cooling.flow-verify` | auto | PASS | 2026-09-11T17:41:29Z | `c-20260911172921-9426` | yes |
| `cooling.fans-quiet-after-motion` | auto | PASS | 2026-09-11T17:47:35Z | `c-20260911172921-9426` | yes |
| `cooling.gate-off` | auto | PASS | 2026-09-11T17:35:02Z | `c-20260911172921-9426` | yes |
| `cooling.floor-and-warm-up` | auto | PASS | 2026-09-11T17:49:02Z | `c-20260911172921-9426` | yes |
| `cooling.tec-drive` | auto | PASS | 2026-09-11T17:49:10Z | `c-20260911172921-9426` | yes |
| `cooling.fire-watch-tiers` | auto | PASS | 2026-09-11T17:49:25Z | `c-20260911172921-9426` | yes |
| `cooling.crash-watch-plumbing` | auto | PASS | 2026-09-11T17:49:32Z | `c-20260911172921-9426` | yes |
| `cooling.critical-tier` | auto | PASS | 2026-09-11T17:49:41Z | `c-20260911172921-9426` | yes |
| `cooling.fan-gate-trips` | auto | PASS | 2026-09-11T17:35:45Z | `c-20260911172921-9426` | yes |
| `cooling.flow-under-load` | live | PASS | 2026-09-11T18:41:47Z | `c-20260911182256-078f` | no |
| `laser.power-floor` | auto | PASS | 2026-09-11T17:49:42Z | `c-20260911172921-9426` | yes |
| `laser.emission-witness` | live, core | PASS | 2026-09-11T18:40:15Z | `c-20260911182256-078f` | no |
| `laser.m5-rapid-dark` | live | PASS | 2026-09-11T18:42:25Z | `c-20260911182256-078f` | no |
| `laser.disarm-in-hold` | live | PASS | 2026-09-11T18:43:51Z | `c-20260911182256-078f` | no |
| `laser.armed-kill` | live | PASS | 2026-09-11T18:44:43Z | `c-20260911182256-078f` | no |
| `laser.arm-wait-lid` | operator | PASS | 2026-09-11T18:25:57Z | `c-20260911182256-078f` | no |
| `laser.pause-resume-lid-cancel` | live | PASS | 2026-09-11T18:45:12Z | `c-20260911182256-078f` | no |
| `camera.snapshot` | auto | PASS | 2026-09-11T17:49:57Z | `c-20260911172921-9426` | yes |
| `camera.sensor-profile` | auto | PASS | 2026-09-11T17:50:10Z | `c-20260911172921-9426` | yes |
| `camera.h264-stream` | auto | PASS | 2026-09-11T17:50:23Z | `c-20260911172921-9426` | yes |
| `camera.frame-health` | auto | PASS | 2026-09-11T17:50:36Z | `c-20260911172921-9426` | yes |
| `camera.lid-privacy` | operator | PASS | 2026-09-11T18:26:12Z | `c-20260911182256-078f` | no |
| `camera.key-read` | auto | PASS | 2026-09-11T17:50:37Z | `c-20260911172921-9426` | yes |
| `update.slots-and-signature` | auto | PASS | 2026-09-11T17:32:49Z | `c-20260911172921-9426` | yes |
| `cloud.mode-switch` | operator | PASS | 2026-09-11T17:58:28Z | `c-20260911172921-9426` | yes |
| `cloud.service-protocol` | operator | PASS | 2026-09-11T18:46:13Z | `c-20260911182256-078f` | no |
| `cloud.lid-interlock-abort` | live | PASS | 2026-09-11T18:47:21Z | `c-20260911182256-078f` | no |
| `cloud.lid-during-button-wait` | operator | PASS | 2026-09-11T18:26:51Z | `c-20260911182256-078f` | no |
| `cloud.verdict-hold` | operator | PASS | 2026-09-11T18:38:58Z | `c-20260911182256-078f` | no |
| `cloud.pause-resume` | live | PASS | 2026-09-11T18:49:33Z | `c-20260911182256-078f` | no |
| `cloud.oversize-stream` | live | PASS | 2026-09-11T18:52:19Z | `c-20260911182256-078f` | no |
| `cloud.paused-lid-cancel` | live | PASS | 2026-09-11T18:52:48Z | `c-20260911182256-078f` | no |

Artifact sha256: `d7bb5d900f9237711a17ae207df90025605340c2eb2eaf34f9ed508cc3a899a6`
