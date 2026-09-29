# ForgeFIRM acceptance - 20260928194539 (dev)

- Image: `20260928194539 (dev)` (forgefirm-image-dev)
- Manifest identity: `e7ca694f20116a46222907e7ba9222021efd2c007f9394f8b3bb9f3ff14875ba`
- Catalog: `32019673226a83358d90ee62a4a22b9ac46f730c9185343e0019ab401a6e463f`
- Campaign: `c-20260929000044-e1ea` opened 2026-09-29T00:00:44Z
- Exported: 2026-09-29T00:07:08Z
- **Release authorized: YES**
- Tests: 127 total, 127 satisfied (120 inherited), 0 required

| Test | Kind | Result | Run at | Campaign | Inherited |
|---|---|---|---|---|---|
| `image.health` | auto, core | PASS | 2026-09-29T00:00:45Z | `c-20260929000044-e1ea` | no |
| `image.license-bundle` | auto | PASS | 2026-09-28T16:23:38Z | `c-20260928162331-3969` | yes |
| `image.network-boot` | auto | PASS | 2026-09-28T16:23:38Z | `c-20260928162331-3969` | yes |
| `kernel.latch-locked-idle` | auto, core | PASS | 2026-09-29T00:00:46Z | `c-20260929000044-e1ea` | no |
| `kernel.k1-k2` | auto, core, takeover | PASS | 2026-09-29T00:01:07Z | `c-20260929000044-e1ea` | no |
| `kernel.deadman-close` | auto, core, takeover | PASS | 2026-09-29T00:01:17Z | `c-20260929000044-e1ea` | no |
| `kernel.backtrack-bounds` | auto, core, takeover | PASS | 2026-09-29T00:01:35Z | `c-20260929000044-e1ea` | no |
| `kernel.fire-line` | auto, core, takeover | PASS | 2026-09-29T00:02:18Z | `c-20260929000044-e1ea` | no |
| `kernel.resume-lead` | auto, takeover | PASS | 2026-09-28T16:25:37Z | `c-20260928162331-3969` | yes |
| `kernel.pic-soc-load` | auto | PASS | 2026-09-28T16:25:41Z | `c-20260928162331-3969` | yes |
| `forgectrl.auth` | auto | PASS | 2026-09-28T16:25:42Z | `c-20260928162331-3969` | yes |
| `forgectrl.settings-bounds` | auto | PASS | 2026-09-28T16:25:43Z | `c-20260928162331-3969` | yes |
| `forgectrl.panel-serves` | auto | PASS | 2026-09-28T16:23:34Z | `c-20260928162331-3969` | yes |
| `events.stream` | auto | PASS | 2026-09-28T16:25:54Z | `c-20260928162331-3969` | yes |
| `forgectrl.lease` | auto | PASS | 2026-09-28T16:25:58Z | `c-20260928162331-3969` | yes |
| `forgectrl.tokens` | auto | PASS | 2026-09-28T16:26:02Z | `c-20260928162331-3969` | yes |
| `setup.gate-blocks-controllers` | auto, takeover | PASS | 2026-09-28T16:26:20Z | `c-20260928162331-3969` | yes |
| `setup.override-until-reboot` | auto | PASS | 2026-09-28T16:26:21Z | `c-20260928162331-3969` | yes |
| `setup.advisories-rehash` | auto, takeover | PASS | 2026-09-28T16:26:30Z | `c-20260928162331-3969` | yes |
| `setup.account-login` | auto, takeover | PASS | 2026-09-28T16:27:19Z | `c-20260928162331-3969` | yes |
| `setup.https-only-writes` | auto | PASS | 2026-09-28T16:27:21Z | `c-20260928162331-3969` | yes |
| `setup.ssh-until-reboot` | auto | PASS | 2026-09-28T16:27:22Z | `c-20260928162331-3969` | yes |
| `setup.cloud-disabled-surface` | auto | PASS | 2026-09-28T16:27:23Z | `c-20260928162331-3969` | yes |
| `setup.extensions-consent` | auto, takeover | PASS | 2026-09-28T16:27:32Z | `c-20260928162331-3969` | yes |
| `setup.factory-return` | auto | PASS | 2026-09-28T16:27:35Z | `c-20260928162331-3969` | yes |
| `setup.machine-name` | auto | PASS | 2026-09-28T16:27:36Z | `c-20260928162331-3969` | yes |
| `setup.first-run-flow` | operator, takeover | PASS | 2026-09-28T16:27:57Z | `c-20260928162331-3969` | yes |
| `setup.first-run-page` | operator, takeover | PASS | 2026-09-28T18:52:23Z | `c-20260928183105-64ee` | yes |
| `setup.cert-page` | auto | PASS | 2026-09-28T16:27:57Z | `c-20260928162331-3969` | yes |
| `setup.what-changed` | auto, takeover | PASS | 2026-09-28T16:28:07Z | `c-20260928162331-3969` | yes |
| `setup.record-export` | auto | PASS | 2026-09-28T16:29:50Z | `c-20260928162331-3969` | yes |
| `setup.mirror` | auto, takeover | PASS | 2026-09-28T16:30:23Z | `c-20260928162331-3969` | yes |
| `setup.check-switches` | operator | PASS | 2026-09-28T16:30:27Z | `c-20260928162331-3969` | yes |
| `setup.check-sensors` | auto | PASS | 2026-09-28T16:30:02Z | `c-20260928162331-3969` | yes |
| `setup.check-airflow` | auto, takeover | PASS | 2026-09-28T16:31:59Z | `c-20260928162331-3969` | yes |
| `setup.check-cameras` | operator | PASS | 2026-09-28T16:32:30Z | `c-20260928162331-3969` | yes |
| `setup.check-motion` | auto, takeover | PASS | 2026-09-28T16:33:06Z | `c-20260928162331-3969` | yes |
| `setup.check-flow-verify` | auto, takeover | PASS | 2026-09-28T16:40:24Z | `c-20260928162331-3969` | yes |
| `setup.cloud-header-capture` | operator, takeover | PASS | 2026-09-28T18:53:27Z | `c-20260928183105-64ee` | yes |
| `setup.sheet` | live, takeover | PASS | 2026-09-28T19:06:20Z | `c-20260928183105-64ee` | yes |
| `logs.tree-tail-export` | auto | PASS | 2026-09-28T16:28:59Z | `c-20260928162331-3969` | yes |
| `logs.routing` | auto | PASS | 2026-09-28T16:40:28Z | `c-20260928162331-3969` | yes |
| `logs.level-settings` | auto | PASS | 2026-09-28T16:40:29Z | `c-20260928162331-3969` | yes |
| `motion.pacing` | auto | PASS | 2026-09-28T16:32:45Z | `c-20260928162331-3969` | yes |
| `motion.jog-roundtrip` | auto | PASS | 2026-09-28T16:40:44Z | `c-20260928162331-3969` | yes |
| `motion.microstep-modes` | auto | PASS | 2026-09-28T16:41:09Z | `c-20260928162331-3969` | yes |
| `motion.liveness-probe` | auto | PASS | 2026-09-28T16:41:18Z | `c-20260928162331-3969` | yes |
| `motion.gate-waits-for-lid` | operator | PASS | 2026-09-28T16:41:34Z | `c-20260928162331-3969` | yes |
| `motion.cancel-abort` | auto | PASS | 2026-09-28T16:41:41Z | `c-20260928162331-3969` | yes |
| `motion.deadman` | auto | PASS | 2026-09-28T16:42:27Z | `c-20260928162331-3969` | yes |
| `motion.respawn-gate` | operator | PASS | 2026-09-28T16:42:35Z | `c-20260928162331-3969` | yes |
| `motion.soft-limits` | auto | PASS | 2026-09-28T16:53:34Z | `c-20260928165212-b92e` | yes |
| `motion.button-hold-resume` | operator | PASS | 2026-09-28T17:12:49Z | `c-20260928171100-3ff5` | yes |
| `motion.lid-cancel-home` | operator | PASS | 2026-09-28T17:13:04Z | `c-20260928171100-3ff5` | yes |
| `motion.interlock-cancel-home` | operator | PASS | 2026-09-28T17:13:12Z | `c-20260928171100-3ff5` | yes |
| `motion.lid-policy-hold` | operator | PASS | 2026-09-28T17:13:26Z | `c-20260928171100-3ff5` | yes |
| `motion.step-timing-under-load` | auto | PASS | 2026-09-28T17:13:56Z | `c-20260928171100-3ff5` | yes |
| `motion.release` | auto, takeover | PASS | 2026-09-28T17:14:23Z | `c-20260928171100-3ff5` | yes |
| `homing.manual` | auto | PASS | 2026-09-28T17:14:35Z | `c-20260928171100-3ff5` | yes |
| `motion.port-jog` | auto | PASS | 2026-09-28T17:14:55Z | `c-20260928171100-3ff5` | yes |
| `motion.job` | auto | PASS | 2026-09-28T17:15:05Z | `c-20260928171100-3ff5` | yes |
| `cooling.aa-offset-calibrate` | auto | PASS | 2026-09-28T16:34:38Z | `c-20260928162331-3969` | yes |
| `cooling.flow-verify` | auto | PASS | 2026-09-28T16:37:19Z | `c-20260928162331-3969` | yes |
| `cooling.fans-quiet-after-motion` | auto | PASS | 2026-09-28T17:16:01Z | `c-20260928171100-3ff5` | yes |
| `cooling.gate-off` | auto | PASS | 2026-09-28T16:30:35Z | `c-20260928162331-3969` | yes |
| `cooling.floor-and-warm-up` | auto | PASS | 2026-09-28T17:17:23Z | `c-20260928171100-3ff5` | yes |
| `cooling.tec-drive` | auto | PASS | 2026-09-28T17:17:33Z | `c-20260928171100-3ff5` | yes |
| `cooling.fire-watch-tiers` | auto | PASS | 2026-09-28T17:17:48Z | `c-20260928171100-3ff5` | yes |
| `cooling.crash-watch-plumbing` | auto | PASS | 2026-09-28T17:17:55Z | `c-20260928171100-3ff5` | yes |
| `cooling.critical-tier` | auto | PASS | 2026-09-28T17:18:04Z | `c-20260928171100-3ff5` | yes |
| `cooling.fan-gate-trips` | auto | PASS | 2026-09-28T16:31:18Z | `c-20260928162331-3969` | yes |
| `cooling.flow-under-load` | live | PASS | 2026-09-28T19:09:14Z | `c-20260928183105-64ee` | yes |
| `cooling.fan-duty-readback` | auto | PASS | 2026-09-28T17:18:21Z | `c-20260928171100-3ff5` | yes |
| `cooling.fail-tier-stop` | operator | PASS | 2026-09-28T17:18:41Z | `c-20260928171100-3ff5` | yes |
| `cooling.report-channel` | auto | PASS | 2026-09-28T17:18:51Z | `c-20260928171100-3ff5` | yes |
| `laser.power-floor` | auto | PASS | 2026-09-28T17:18:52Z | `c-20260928171100-3ff5` | yes |
| `laser.emission-witness` | live, core | PASS | 2026-09-29T00:03:33Z | `c-20260929000044-e1ea` | no |
| `laser.m5-rapid-dark` | live | PASS | 2026-09-28T19:10:52Z | `c-20260928183105-64ee` | yes |
| `laser.disarm-in-hold` | live | PASS | 2026-09-28T19:12:23Z | `c-20260928183105-64ee` | yes |
| `laser.armed-kill` | live | PASS | 2026-09-28T19:13:11Z | `c-20260928183105-64ee` | yes |
| `laser.verdict-cut` | live | PASS | 2026-09-28T19:13:49Z | `c-20260928183105-64ee` | yes |
| `laser.arm-wait-lid` | operator | PASS | 2026-09-28T17:18:55Z | `c-20260928171100-3ff5` | yes |
| `laser.pause-resume-lid-cancel` | live | PASS | 2026-09-28T19:14:46Z | `c-20260928183105-64ee` | yes |
| `laser.port-dark` | live | PASS | 2026-09-28T19:15:23Z | `c-20260928183105-64ee` | yes |
| `laser.recorder-dark` | auto | PASS | 2026-09-28T17:19:15Z | `c-20260928171100-3ff5` | yes |
| `camera.snapshot` | auto | PASS | 2026-09-28T16:32:16Z | `c-20260928162331-3969` | yes |
| `camera.sensor-profile` | auto | PASS | 2026-09-28T17:19:28Z | `c-20260928171100-3ff5` | yes |
| `camera.h264-stream` | auto | PASS | 2026-09-28T17:19:42Z | `c-20260928171100-3ff5` | yes |
| `camera.frame-health` | auto | PASS | 2026-09-28T17:20:00Z | `c-20260928171100-3ff5` | yes |
| `camera.lid-privacy` | operator | PASS | 2026-09-28T17:20:16Z | `c-20260928171100-3ff5` | yes |
| `camera.key-read` | auto | PASS | 2026-09-28T17:20:17Z | `c-20260928171100-3ff5` | yes |
| `update.slots-and-signature` | auto | PASS | 2026-09-28T16:27:34Z | `c-20260928162331-3969` | yes |
| `update.product-gate` | auto | PASS | 2026-09-28T17:20:19Z | `c-20260928171100-3ff5` | yes |
| `update.release-check` | auto | PASS | 2026-09-28T17:20:20Z | `c-20260928171100-3ff5` | yes |
| `cloud.mode-switch` | operator | PASS | 2026-09-28T16:45:24Z | `c-20260928162331-3969` | yes |
| `cloud.service-protocol` | operator | PASS | 2026-09-28T19:17:20Z | `c-20260928183105-64ee` | yes |
| `cloud.lid-interlock-abort` | live | PASS | 2026-09-28T19:19:36Z | `c-20260928183105-64ee` | yes |
| `cloud.lid-during-button-wait` | operator | PASS | 2026-09-28T17:21:44Z | `c-20260928171100-3ff5` | yes |
| `cloud.dark-print` | operator | PASS | 2026-09-28T17:22:40Z | `c-20260928171100-3ff5` | yes |
| `cloud.verdict-refuse` | operator | PASS | 2026-09-28T17:24:02Z | `c-20260928171100-3ff5` | yes |
| `cloud.pause-resume` | live | PASS | 2026-09-28T19:22:10Z | `c-20260928183105-64ee` | yes |
| `cloud.oversize-stream` | live | PASS | 2026-09-28T19:25:42Z | `c-20260928183105-64ee` | yes |
| `cloud.paused-lid-cancel` | live | PASS | 2026-09-28T19:26:35Z | `c-20260928183105-64ee` | yes |
| `exthost.platform` | auto | PASS | 2026-09-28T18:05:37Z | `c-20260928180313-75fb` | yes |
| `exthost.service` | auto, takeover | PASS | 2026-09-28T18:06:28Z | `c-20260928180313-75fb` | yes |
| `exthost.armed-freeze` | operator, takeover | PASS | 2026-09-28T18:08:17Z | `c-20260928180313-75fb` | yes |
| `exthost.hold-pause-tier` | auto, takeover | PASS | 2026-09-28T18:09:57Z | `c-20260928180313-75fb` | yes |
| `exthost.events` | auto, takeover | PASS | 2026-09-28T18:10:19Z | `c-20260928180313-75fb` | yes |
| `exthost.motion-jog` | auto, takeover | PASS | 2026-09-28T18:10:39Z | `c-20260928180313-75fb` | yes |
| `exthost.ui-delivery` | auto | PASS | 2026-09-28T18:10:48Z | `c-20260928180313-75fb` | yes |
| `exthost.motion-job` | auto, takeover | PASS | 2026-09-28T18:11:06Z | `c-20260928180313-75fb` | yes |
| `exthost.package-routes` | auto | PASS | 2026-09-28T18:11:09Z | `c-20260928180313-75fb` | yes |
| `exthost.panel-install` | operator | PASS | 2026-09-28T18:11:12Z | `c-20260928180313-75fb` | yes |
| `exthost.core-range` | auto | PASS | 2026-09-28T18:11:13Z | `c-20260928180313-75fb` | yes |
| `exthost.page-call` | auto | PASS | 2026-09-28T18:11:30Z | `c-20260928180313-75fb` | yes |
| `exthost.operator-destinations` | auto | PASS | 2026-09-28T18:11:51Z | `c-20260928180313-75fb` | yes |
| `exthost.lifecycle` | auto | PASS | 2026-09-28T18:12:06Z | `c-20260928180313-75fb` | yes |
| `events.button-telemetry` | auto | PASS | 2026-09-28T18:12:19Z | `c-20260928180313-75fb` | yes |
| `exthost.catalog` | auto | PASS | 2026-09-28T18:12:20Z | `c-20260928180313-75fb` | yes |
| `exthost.mcode` | auto | PASS | 2026-09-28T18:12:39Z | `c-20260928180313-75fb` | yes |
| `exthost.wizard` | auto | PASS | 2026-09-28T18:13:09Z | `c-20260928180313-75fb` | yes |
| `update.job-locks` | auto | PASS | 2026-09-28T18:13:11Z | `c-20260928180313-75fb` | yes |
| `homing.cloud-offsets` | auto | PASS | 2026-09-28T18:14:18Z | `c-20260928180313-75fb` | yes |
| `setup.check-envelope` | auto | PASS | 2026-09-28T18:16:06Z | `c-20260928180313-75fb` | yes |
| `exthost.sender-keep-out` | auto | PASS | 2026-09-28T18:16:39Z | `c-20260928180313-75fb` | yes |
| `motion.tray` | auto | PASS | 2026-09-28T18:16:42Z | `c-20260928180313-75fb` | yes |
| `forgectrl.tls-records` | auto | PASS | 2026-09-28T18:33:03Z | `c-20260928183105-64ee` | yes |

Artifact sha256: `550c90e31d327d69f16407ad7539c09cd34c08711fdb62dd479321db2d5c07da`
