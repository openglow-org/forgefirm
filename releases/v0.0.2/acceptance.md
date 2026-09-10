# ForgeFIRM acceptance - 20260910181308 (dev)

- Image: `20260910181308 (dev)` (forgefirm-image-dev)
- Manifest identity: `168e0307cef8f58f6bb871102f98ee56f9661b852e738d054891c5f072fd33b9`
- Catalog: `da9da478f3df1e5ad87319fd9d831ee224a3c88e5d3896162a09b4de14aad6b1`
- Campaign: `c-20260910181641-11a2` opened 2026-09-10T18:16:41Z
- Exported: 2026-09-10T18:32:07Z
- **Release authorized: YES**
- Tests: 83 total, 83 satisfied (57 inherited), 0 required

| Test | Kind | Result | Run at | Campaign | Inherited |
|---|---|---|---|---|---|
| `image.health` | auto, core | PASS | 2026-09-10T18:23:17Z | `c-20260910181641-11a2` | no |
| `image.license-bundle` | auto | PASS | 2026-09-10T13:11:39Z | `c-20260910131134-5cb0` | yes |
| `kernel.latch-locked-idle` | auto, core | PASS | 2026-09-10T18:23:17Z | `c-20260910181641-11a2` | no |
| `kernel.k1-k2` | auto, core, takeover | PASS | 2026-09-10T18:23:39Z | `c-20260910181641-11a2` | no |
| `kernel.deadman-close` | auto, core, takeover | PASS | 2026-09-10T18:23:50Z | `c-20260910181641-11a2` | no |
| `kernel.backtrack-bounds` | auto, core, takeover | PASS | 2026-09-10T18:24:08Z | `c-20260910181641-11a2` | no |
| `kernel.fire-line` | auto, core, takeover | PASS | 2026-09-10T18:24:51Z | `c-20260910181641-11a2` | no |
| `kernel.resume-lead` | auto, takeover | PASS | 2026-09-10T13:13:35Z | `c-20260910131134-5cb0` | yes |
| `kernel.pic-soc-load` | auto | PASS | 2026-09-10T13:13:39Z | `c-20260910131134-5cb0` | yes |
| `forgectrl.auth` | auto | PASS | 2026-09-10T13:13:40Z | `c-20260910131134-5cb0` | yes |
| `forgectrl.settings-bounds` | auto | PASS | 2026-09-10T13:13:41Z | `c-20260910131134-5cb0` | yes |
| `forgectrl.panel-serves` | auto | PASS | 2026-09-10T13:11:36Z | `c-20260910131134-5cb0` | yes |
| `commission.gate-blocks-controllers` | auto, takeover | PASS | 2026-09-10T13:14:00Z | `c-20260910131134-5cb0` | yes |
| `commission.override-until-reboot` | auto | PASS | 2026-09-10T13:14:00Z | `c-20260910131134-5cb0` | yes |
| `commission.advisories-rehash` | auto, takeover | PASS | 2026-09-10T13:14:09Z | `c-20260910131134-5cb0` | yes |
| `commission.account-login` | auto, takeover | PASS | 2026-09-10T13:14:59Z | `c-20260910131134-5cb0` | yes |
| `commission.https-only-writes` | auto | PASS | 2026-09-10T13:15:00Z | `c-20260910131134-5cb0` | yes |
| `commission.ssh-until-reboot` | auto | PASS | 2026-09-10T13:15:00Z | `c-20260910131134-5cb0` | yes |
| `commission.cloud-disabled-surface` | auto | PASS | 2026-09-10T13:15:01Z | `c-20260910131134-5cb0` | yes |
| `commission.factory-return` | auto | PASS | 2026-09-10T13:15:04Z | `c-20260910131134-5cb0` | yes |
| `commission.machine-name` | auto | PASS | 2026-09-10T13:15:04Z | `c-20260910131134-5cb0` | yes |
| `commission.first-run-flow` | operator, takeover | PASS | 2026-09-10T13:15:26Z | `c-20260910131134-5cb0` | yes |
| `commission.first-run-page` | operator, takeover | PASS | 2026-09-10T17:25:08Z | `c-20260910171338-9149` | yes |
| `commission.cert-page` | auto | PASS | 2026-09-10T13:15:26Z | `c-20260910131134-5cb0` | yes |
| `commission.what-changed` | auto, takeover | PASS | 2026-09-10T13:15:36Z | `c-20260910131134-5cb0` | yes |
| `commission.record-export` | auto | PASS | 2026-09-10T13:16:52Z | `c-20260910131134-5cb0` | yes |
| `commission.mirror` | auto, takeover | PASS | 2026-09-10T13:17:25Z | `c-20260910131134-5cb0` | yes |
| `commission.check-switches` | operator | PASS | 2026-09-10T16:06:37Z | `c-20260910160456-619b` | yes |
| `commission.check-sensors` | auto | PASS | 2026-09-10T16:06:50Z | `c-20260910160456-619b` | yes |
| `commission.check-airflow` | auto, takeover | PASS | 2026-09-10T16:08:24Z | `c-20260910160456-619b` | yes |
| `commission.check-cameras` | operator | PASS | 2026-09-10T16:08:39Z | `c-20260910160456-619b` | yes |
| `commission.check-motion` | auto, takeover | PASS | 2026-09-10T16:09:05Z | `c-20260910160456-619b` | yes |
| `commission.check-flow-verify` | auto, takeover | PASS | 2026-09-10T16:16:37Z | `c-20260910160456-619b` | yes |
| `commission.cloud-header-capture` | operator, takeover | PASS | 2026-09-10T17:26:21Z | `c-20260910171338-9149` | yes |
| `commission.sheet` | live, takeover | PASS | 2026-09-10T17:39:28Z | `c-20260910171338-9149` | yes |
| `logs.tree-tail-export` | auto | PASS | 2026-09-10T13:16:14Z | `c-20260910131134-5cb0` | yes |
| `logs.routing` | auto | PASS | 2026-09-10T13:27:51Z | `c-20260910131134-5cb0` | yes |
| `logs.level-settings` | auto | PASS | 2026-09-10T13:27:52Z | `c-20260910131134-5cb0` | yes |
| `motion.pacing` | auto | PASS | 2026-09-10T18:25:07Z | `c-20260910181641-11a2` | no |
| `motion.jog-roundtrip` | auto | PASS | 2026-09-10T18:25:21Z | `c-20260910181641-11a2` | no |
| `motion.microstep-modes` | auto | PASS | 2026-09-10T18:25:46Z | `c-20260910181641-11a2` | no |
| `motion.liveness-probe` | auto | PASS | 2026-09-10T13:28:39Z | `c-20260910131134-5cb0` | yes |
| `motion.gate-waits-for-lid` | operator | PASS | 2026-09-10T13:28:56Z | `c-20260910131134-5cb0` | yes |
| `motion.cancel-abort` | auto | PASS | 2026-09-10T18:25:53Z | `c-20260910181641-11a2` | no |
| `motion.deadman` | auto | PASS | 2026-09-10T18:26:22Z | `c-20260910181641-11a2` | no |
| `motion.button-hold-resume` | operator | PASS | 2026-09-10T18:26:33Z | `c-20260910181641-11a2` | no |
| `motion.lid-cancel-home` | operator | PASS | 2026-09-10T18:26:49Z | `c-20260910181641-11a2` | no |
| `motion.interlock-cancel-home` | operator | PASS | 2026-09-10T18:26:58Z | `c-20260910181641-11a2` | no |
| `motion.lid-policy-hold` | operator | PASS | 2026-09-10T18:27:12Z | `c-20260910181641-11a2` | no |
| `motion.step-timing-under-load` | auto | PASS | 2026-09-10T18:27:42Z | `c-20260910181641-11a2` | no |
| `cooling.aa-offset-calibrate` | auto | PASS | 2026-09-10T16:10:38Z | `c-20260910160456-619b` | yes |
| `cooling.flow-verify` | auto | PASS | 2026-09-10T16:13:21Z | `c-20260910160456-619b` | yes |
| `cooling.fans-quiet-after-motion` | auto | PASS | 2026-09-10T16:17:38Z | `c-20260910160456-619b` | yes |
| `cooling.gate-off` | auto | PASS | 2026-09-10T16:06:57Z | `c-20260910160456-619b` | yes |
| `cooling.floor-and-warm-up` | auto | PASS | 2026-09-10T16:19:05Z | `c-20260910160456-619b` | yes |
| `cooling.tec-drive` | auto | PASS | 2026-09-10T16:19:13Z | `c-20260910160456-619b` | yes |
| `cooling.fire-watch-tiers` | auto | PASS | 2026-09-10T16:19:29Z | `c-20260910160456-619b` | yes |
| `cooling.crash-watch-plumbing` | auto | PASS | 2026-09-10T16:19:36Z | `c-20260910160456-619b` | yes |
| `cooling.critical-tier` | auto | PASS | 2026-09-10T16:19:44Z | `c-20260910160456-619b` | yes |
| `cooling.fan-gate-trips` | auto | PASS | 2026-09-10T16:07:43Z | `c-20260910160456-619b` | yes |
| `cooling.flow-under-load` | live | PASS | 2026-09-10T17:41:03Z | `c-20260910171338-9149` | yes |
| `laser.power-floor` | auto | PASS | 2026-09-10T18:27:43Z | `c-20260910181641-11a2` | no |
| `laser.emission-witness` | live, core | PASS | 2026-09-10T18:17:19Z | `c-20260910181641-11a2` | no |
| `laser.m5-rapid-dark` | live | PASS | 2026-09-10T18:18:01Z | `c-20260910181641-11a2` | no |
| `laser.disarm-in-hold` | live | PASS | 2026-09-10T18:19:29Z | `c-20260910181641-11a2` | no |
| `laser.armed-kill` | live | PASS | 2026-09-10T18:20:12Z | `c-20260910181641-11a2` | no |
| `laser.arm-wait-lid` | operator | PASS | 2026-09-10T18:28:02Z | `c-20260910181641-11a2` | no |
| `laser.pause-resume-lid-cancel` | live | PASS | 2026-09-10T18:20:41Z | `c-20260910181641-11a2` | no |
| `camera.snapshot` | auto | PASS | 2026-09-10T13:19:21Z | `c-20260910131134-5cb0` | yes |
| `camera.sensor-profile` | auto | PASS | 2026-09-10T14:55:23Z | `c-20260910144945-3072` | yes |
| `camera.h264-stream` | auto | PASS | 2026-09-10T14:55:37Z | `c-20260910144945-3072` | yes |
| `camera.frame-health` | auto | PASS | 2026-09-10T14:55:50Z | `c-20260910144945-3072` | yes |
| `camera.lid-privacy` | operator | PASS | 2026-09-10T14:56:05Z | `c-20260910144945-3072` | yes |
| `camera.key-read` | auto | PASS | 2026-09-10T14:56:06Z | `c-20260910144945-3072` | yes |
| `update.slots-and-signature` | auto | PASS | 2026-09-10T13:15:03Z | `c-20260910131134-5cb0` | yes |
| `cloud.mode-switch` | operator | PASS | 2026-09-10T18:29:38Z | `c-20260910181641-11a2` | no |
| `cloud.service-protocol` | operator | PASS | 2026-09-10T18:21:52Z | `c-20260910181641-11a2` | no |
| `cloud.lid-interlock-abort` | live | PASS | 2026-09-10T17:19:59Z | `c-20260910171338-9149` | yes |
| `cloud.lid-during-button-wait` | operator | PASS | 2026-09-10T14:58:32Z | `c-20260910144945-3072` | yes |
| `cloud.verdict-hold` | operator | PASS | 2026-09-10T17:18:09Z | `c-20260910171338-9149` | yes |
| `cloud.pause-resume` | live | PASS | 2026-09-10T18:23:07Z | `c-20260910181641-11a2` | no |
| `cloud.oversize-stream` | live | PASS | 2026-09-10T17:22:39Z | `c-20260910171338-9149` | yes |
| `cloud.paused-lid-cancel` | live | PASS | 2026-09-10T17:18:57Z | `c-20260910171338-9149` | yes |

Artifact sha256: `191f416666d40efd92c77973fa392f38ca3d5ca160533ff83b4475d47b5fa6e3`
