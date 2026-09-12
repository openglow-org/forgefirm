# ForgeFIRM acceptance - 20260912180956 (dev)

- Image: `20260912180956 (dev)` (forgefirm-image-dev)
- Manifest identity: `8bb5b6f39ca4e0a5afca9b620aa38980c4ede8cdfda240b24bbc01fd6c28fa05`
- Catalog: `f906de649b7533e3713309f141afacbc06420f96824936a60b70febd71bb2df7`
- Campaign: `c-20260912182427-e1ee` opened 2026-09-12T18:24:27Z
- Exported: 2026-09-12T19:21:35Z
- **Release authorized: YES**
- Tests: 85 total, 85 satisfied (50 inherited), 0 required

| Test | Kind | Result | Run at | Campaign | Inherited |
|---|---|---|---|---|---|
| `image.health` | auto, core | PASS | 2026-09-12T18:24:28Z | `c-20260912182427-e1ee` | no |
| `image.license-bundle` | auto | PASS | 2026-09-12T16:56:02Z | `c-20260912165539-ac51` | yes |
| `kernel.latch-locked-idle` | auto, core | PASS | 2026-09-12T18:24:29Z | `c-20260912182427-e1ee` | no |
| `kernel.k1-k2` | auto, core, takeover | PASS | 2026-09-12T18:24:50Z | `c-20260912182427-e1ee` | no |
| `kernel.deadman-close` | auto, core, takeover | PASS | 2026-09-12T18:25:02Z | `c-20260912182427-e1ee` | no |
| `kernel.backtrack-bounds` | auto, core, takeover | PASS | 2026-09-12T18:25:21Z | `c-20260912182427-e1ee` | no |
| `kernel.fire-line` | auto, core, takeover | PASS | 2026-09-12T18:26:04Z | `c-20260912182427-e1ee` | no |
| `kernel.resume-lead` | auto, takeover | PASS | 2026-09-11T17:31:23Z | `c-20260911172921-9426` | yes |
| `kernel.pic-soc-load` | auto | PASS | 2026-09-11T17:31:26Z | `c-20260911172921-9426` | yes |
| `forgectrl.auth` | auto | PASS | 2026-09-12T16:58:12Z | `c-20260912165539-ac51` | yes |
| `forgectrl.settings-bounds` | auto | PASS | 2026-09-12T16:58:19Z | `c-20260912165539-ac51` | yes |
| `forgectrl.panel-serves` | auto | PASS | 2026-09-12T16:55:54Z | `c-20260912165539-ac51` | yes |
| `commission.gate-blocks-controllers` | auto, takeover | PASS | 2026-09-12T16:58:44Z | `c-20260912165539-ac51` | yes |
| `commission.override-until-reboot` | auto | PASS | 2026-09-12T16:58:44Z | `c-20260912165539-ac51` | yes |
| `commission.advisories-rehash` | auto, takeover | PASS | 2026-09-12T16:58:53Z | `c-20260912165539-ac51` | yes |
| `commission.account-login` | auto, takeover | PASS | 2026-09-12T16:59:43Z | `c-20260912165539-ac51` | yes |
| `commission.https-only-writes` | auto | PASS | 2026-09-12T16:59:44Z | `c-20260912165539-ac51` | yes |
| `commission.ssh-until-reboot` | auto | PASS | 2026-09-12T16:59:46Z | `c-20260912165539-ac51` | yes |
| `commission.cloud-disabled-surface` | auto | PASS | 2026-09-12T16:59:46Z | `c-20260912165539-ac51` | yes |
| `commission.factory-return` | auto | PASS | 2026-09-12T16:59:49Z | `c-20260912165539-ac51` | yes |
| `commission.machine-name` | auto | PASS | 2026-09-11T17:32:50Z | `c-20260911172921-9426` | yes |
| `commission.first-run-flow` | operator, takeover | PASS | 2026-09-12T17:10:18Z | `c-20260912170819-8dec` | yes |
| `commission.first-run-page` | operator, takeover | PASS | 2026-09-11T17:55:49Z | `c-20260911172921-9426` | yes |
| `commission.cert-page` | auto | PASS | 2026-09-12T16:59:50Z | `c-20260912165539-ac51` | yes |
| `commission.what-changed` | auto, takeover | PASS | 2026-09-12T16:59:59Z | `c-20260912165539-ac51` | yes |
| `commission.record-export` | auto | PASS | 2026-09-12T17:01:26Z | `c-20260912165539-ac51` | yes |
| `commission.mirror` | auto, takeover | PASS | 2026-09-11T17:34:54Z | `c-20260911172921-9426` | yes |
| `commission.check-switches` | operator | PASS | 2026-09-12T17:10:23Z | `c-20260912170819-8dec` | yes |
| `commission.check-sensors` | auto | PASS | 2026-09-12T18:26:17Z | `c-20260912182427-e1ee` | no |
| `commission.check-airflow` | auto, takeover | PASS | 2026-09-12T18:27:51Z | `c-20260912182427-e1ee` | no |
| `commission.check-cameras` | operator | PASS | 2026-09-12T17:10:54Z | `c-20260912170819-8dec` | yes |
| `commission.check-motion` | auto, takeover | PASS | 2026-09-12T18:28:17Z | `c-20260912182427-e1ee` | no |
| `commission.check-flow-verify` | auto, takeover | PASS | 2026-09-12T17:16:41Z | `c-20260912170819-8dec` | yes |
| `commission.cloud-header-capture` | operator, takeover | PASS | 2026-09-12T17:29:14Z | `c-20260912170819-8dec` | yes |
| `commission.sheet` | live, takeover | PASS | 2026-09-12T19:02:59Z | `c-20260912182427-e1ee` | no |
| `logs.tree-tail-export` | auto | PASS | 2026-09-12T17:00:43Z | `c-20260912165539-ac51` | yes |
| `logs.routing` | auto | PASS | 2026-09-11T17:44:46Z | `c-20260911172921-9426` | yes |
| `logs.level-settings` | auto | PASS | 2026-09-12T17:16:42Z | `c-20260912170819-8dec` | yes |
| `motion.pacing` | auto | PASS | 2026-09-11T17:36:41Z | `c-20260911172921-9426` | yes |
| `motion.jog-roundtrip` | auto | PASS | 2026-09-11T17:45:00Z | `c-20260911172921-9426` | yes |
| `motion.microstep-modes` | auto | PASS | 2026-09-12T17:17:07Z | `c-20260912170819-8dec` | yes |
| `motion.liveness-probe` | auto | PASS | 2026-09-11T17:45:33Z | `c-20260911172921-9426` | yes |
| `motion.gate-waits-for-lid` | operator | PASS | 2026-09-12T17:17:23Z | `c-20260912170819-8dec` | yes |
| `motion.cancel-abort` | auto | PASS | 2026-09-11T17:45:40Z | `c-20260911172921-9426` | yes |
| `motion.deadman` | auto | PASS | 2026-09-12T17:17:53Z | `c-20260912170819-8dec` | yes |
| `motion.button-hold-resume` | operator | PASS | 2026-09-11T18:24:59Z | `c-20260911182256-078f` | yes |
| `motion.lid-cancel-home` | operator | PASS | 2026-09-11T18:25:15Z | `c-20260911182256-078f` | yes |
| `motion.interlock-cancel-home` | operator | PASS | 2026-09-11T18:25:24Z | `c-20260911182256-078f` | yes |
| `motion.lid-policy-hold` | operator | PASS | 2026-09-11T18:25:37Z | `c-20260911182256-078f` | yes |
| `motion.step-timing-under-load` | auto | PASS | 2026-09-11T17:46:40Z | `c-20260911172921-9426` | yes |
| `cooling.aa-offset-calibrate` | auto | PASS | 2026-09-12T18:29:50Z | `c-20260912182427-e1ee` | no |
| `cooling.flow-verify` | auto | PASS | 2026-09-12T18:32:33Z | `c-20260912182427-e1ee` | no |
| `cooling.fans-quiet-after-motion` | auto | PASS | 2026-09-12T18:33:39Z | `c-20260912182427-e1ee` | no |
| `cooling.gate-off` | auto | PASS | 2026-09-12T18:26:26Z | `c-20260912182427-e1ee` | no |
| `cooling.floor-and-warm-up` | auto | PASS | 2026-09-12T18:35:06Z | `c-20260912182427-e1ee` | no |
| `cooling.tec-drive` | auto | PASS | 2026-09-12T18:35:15Z | `c-20260912182427-e1ee` | no |
| `cooling.fire-watch-tiers` | auto | PASS | 2026-09-12T18:35:31Z | `c-20260912182427-e1ee` | no |
| `cooling.crash-watch-plumbing` | auto | PASS | 2026-09-12T18:35:38Z | `c-20260912182427-e1ee` | no |
| `cooling.critical-tier` | auto | PASS | 2026-09-12T18:35:47Z | `c-20260912182427-e1ee` | no |
| `cooling.fan-gate-trips` | auto | PASS | 2026-09-12T18:27:10Z | `c-20260912182427-e1ee` | no |
| `cooling.flow-under-load` | live | PASS | 2026-09-12T19:05:38Z | `c-20260912182427-e1ee` | no |
| `cooling.fan-duty-readback` | auto | PASS | 2026-09-12T18:36:02Z | `c-20260912182427-e1ee` | no |
| `laser.power-floor` | auto | PASS | 2026-09-12T17:20:57Z | `c-20260912170819-8dec` | yes |
| `laser.emission-witness` | live, core | PASS | 2026-09-12T18:50:46Z | `c-20260912182427-e1ee` | no |
| `laser.m5-rapid-dark` | live | PASS | 2026-09-12T19:06:30Z | `c-20260912182427-e1ee` | no |
| `laser.disarm-in-hold` | live | PASS | 2026-09-12T19:08:04Z | `c-20260912182427-e1ee` | no |
| `laser.armed-kill` | live | PASS | 2026-09-12T19:08:58Z | `c-20260912182427-e1ee` | no |
| `laser.arm-wait-lid` | operator | PASS | 2026-09-12T18:36:21Z | `c-20260912182427-e1ee` | no |
| `laser.pause-resume-lid-cancel` | live | PASS | 2026-09-12T19:09:43Z | `c-20260912182427-e1ee` | no |
| `camera.snapshot` | auto | PASS | 2026-09-12T17:10:39Z | `c-20260912170819-8dec` | yes |
| `camera.sensor-profile` | auto | PASS | 2026-09-12T17:21:13Z | `c-20260912170819-8dec` | yes |
| `camera.h264-stream` | auto | PASS | 2026-09-12T17:21:27Z | `c-20260912170819-8dec` | yes |
| `camera.frame-health` | auto | PASS | 2026-09-12T17:21:40Z | `c-20260912170819-8dec` | yes |
| `camera.lid-privacy` | operator | PASS | 2026-09-12T17:21:56Z | `c-20260912170819-8dec` | yes |
| `camera.key-read` | auto | PASS | 2026-09-12T17:21:57Z | `c-20260912170819-8dec` | yes |
| `update.slots-and-signature` | auto | PASS | 2026-09-12T16:59:48Z | `c-20260912165539-ac51` | yes |
| `update.release-check` | auto | PASS | 2026-09-12T17:21:58Z | `c-20260912170819-8dec` | yes |
| `cloud.mode-switch` | operator | PASS | 2026-09-12T18:38:00Z | `c-20260912182427-e1ee` | no |
| `cloud.service-protocol` | operator | PASS | 2026-09-12T19:10:50Z | `c-20260912182427-e1ee` | no |
| `cloud.lid-interlock-abort` | live | PASS | 2026-09-12T19:11:59Z | `c-20260912182427-e1ee` | no |
| `cloud.lid-during-button-wait` | operator | PASS | 2026-09-12T17:24:21Z | `c-20260912170819-8dec` | yes |
| `cloud.verdict-hold` | operator | PASS | 2026-09-12T18:40:33Z | `c-20260912182427-e1ee` | no |
| `cloud.pause-resume` | live | PASS | 2026-09-12T19:14:09Z | `c-20260912182427-e1ee` | no |
| `cloud.oversize-stream` | live | PASS | 2026-09-12T19:16:55Z | `c-20260912182427-e1ee` | no |
| `cloud.paused-lid-cancel` | live | PASS | 2026-09-12T19:17:30Z | `c-20260912182427-e1ee` | no |

Artifact sha256: `9650b1ed9862eefab808ca32cdaa8f6b9b6debd8ee640adde9d18dcdfc03ba25`
