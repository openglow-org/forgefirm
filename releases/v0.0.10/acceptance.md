# ForgeFIRM acceptance - 20261021220433 (dev)

- Image: `20261021220433 (dev)` (forgefirm-image-dev)
- Manifest identity: `63afee5e78f1ab2fc76a8aff4447d3cfe66961b929f9dc9ceddd7ef2fdc8915f`
- Catalog: `b38332a30994e16a499a9ded530062291bacabf873138461dd54421e2174c29b`
- Campaign: `c-20261021220815-c844` opened 2026-10-21T22:08:15Z
- Exported: 2026-10-21T22:13:24Z
- **Release authorized: YES**
- Tests: 43 total, 43 satisfied (36 inherited), 0 required

| Test | Kind | Result | Run at | Campaign | Inherited |
|---|---|---|---|---|---|
| `image.health` | auto, core | PASS | 2026-10-21T22:08:20Z | `c-20261021220815-c844` | no |
| `kernel.latch-locked-idle` | auto, core | PASS | 2026-10-21T22:08:21Z | `c-20261021220815-c844` | no |
| `kernel.k1-k2` | auto, core, takeover | PASS | 2026-10-21T22:08:42Z | `c-20261021220815-c844` | no |
| `kernel.deadman-close` | auto, core, takeover | PASS | 2026-10-21T22:08:53Z | `c-20261021220815-c844` | no |
| `kernel.backtrack-bounds` | auto, core, takeover | PASS | 2026-10-21T22:09:10Z | `c-20261021220815-c844` | no |
| `kernel.fire-line` | auto, core, takeover | PASS | 2026-10-21T22:09:54Z | `c-20261021220815-c844` | no |
| `kernel.resume-lead` | auto, takeover | PASS | 2026-10-18T22:48:37Z | `c-20261018224638-4ca0` | yes |
| `kernel.pic-soc-load` | auto | PASS | 2026-10-21T19:57:41Z | `c-20261021195559-ad7e` | yes |
| `forgectrl.auth` | auto | PASS | 2026-10-21T20:33:44Z | `c-20261021203201-45f3` | yes |
| `setup.check-switches` | operator | PASS | 2026-10-21T20:33:48Z | `c-20261021203201-45f3` | yes |
| `setup.check-airflow` | auto, takeover | PASS | 2026-10-21T20:34:29Z | `c-20261021203201-45f3` | yes |
| `setup.check-motion` | auto, takeover | PASS | 2026-10-21T20:34:49Z | `c-20261021203201-45f3` | yes |
| `setup.check-lens` | auto, takeover | PASS | 2026-10-21T20:35:23Z | `c-20261021203201-45f3` | yes |
| `setup.cloud-header-capture` | operator, takeover | PASS | 2026-10-21T21:19:22Z | `c-20261021210943-36d1` | yes |
| `setup.focus` | live, takeover | PASS | 2026-10-21T21:21:36Z | `c-20261021210943-36d1` | yes |
| `motion.pacing` | auto | PASS | 2026-10-18T22:49:43Z | `c-20261018224638-4ca0` | yes |
| `motion.jog-roundtrip` | auto | PASS | 2026-10-21T20:35:41Z | `c-20261021203201-45f3` | yes |
| `motion.microstep-modes` | auto | PASS | 2026-10-21T20:36:06Z | `c-20261021203201-45f3` | yes |
| `motion.liveness-probe` | auto | PASS | 2026-10-18T22:51:16Z | `c-20261018224638-4ca0` | yes |
| `motion.deadman` | auto | PASS | 2026-10-21T20:36:52Z | `c-20261021203201-45f3` | yes |
| `motion.step-timing-under-load` | auto | PASS | 2026-10-18T22:52:33Z | `c-20261018224638-4ca0` | yes |
| `motion.release` | auto, takeover | PASS | 2026-10-21T20:37:19Z | `c-20261021203201-45f3` | yes |
| `cooling.aa-offset-calibrate` | auto | PASS | 2026-10-21T20:38:52Z | `c-20261021203201-45f3` | yes |
| `cooling.flow-verify` | auto | PASS | 2026-10-21T20:41:35Z | `c-20261021203201-45f3` | yes |
| `cooling.flow-under-load` | live | PASS | 2026-10-21T21:23:08Z | `c-20261021210943-36d1` | yes |
| `cooling.fail-tier-stop` | operator | PASS | 2026-10-21T21:11:44Z | `c-20261021210943-36d1` | yes |
| `laser.emission-witness` | live, core | PASS | 2026-10-21T22:11:40Z | `c-20261021220815-c844` | no |
| `laser.armed-kill` | live | PASS | 2026-10-21T21:23:49Z | `c-20261021210943-36d1` | yes |
| `laser.pause-resume-lid-cancel` | live | PASS | 2026-10-21T21:24:26Z | `c-20261021210943-36d1` | yes |
| `camera.snapshot` | auto | PASS | 2026-10-21T21:12:01Z | `c-20261021210943-36d1` | yes |
| `camera.sensor-profile` | auto | PASS | 2026-10-21T21:12:14Z | `c-20261021210943-36d1` | yes |
| `camera.h264-stream` | auto | PASS | 2026-10-21T21:12:27Z | `c-20261021210943-36d1` | yes |
| `camera.frame-health` | auto | PASS | 2026-10-21T21:12:46Z | `c-20261021210943-36d1` | yes |
| `camera.lid-privacy` | operator | PASS | 2026-10-21T21:13:02Z | `c-20261021210943-36d1` | yes |
| `update.slots-and-signature` | auto | PASS | 2026-10-18T22:58:50Z | `c-20261018224638-4ca0` | yes |
| `cloud.mode-switch` | operator | PASS | 2026-10-21T21:15:29Z | `c-20261021210943-36d1` | yes |
| `cloud.service-protocol` | operator | PASS | 2026-10-21T21:25:28Z | `c-20261021210943-36d1` | yes |
| `cloud.lid-interlock-abort` | live | PASS | 2026-10-21T21:27:43Z | `c-20261021210943-36d1` | yes |
| `cloud.pause-resume` | live | PASS | 2026-10-21T21:41:06Z | `c-20261021213806-92b2` | yes |
| `cloud.oversize-stream` | live | PASS | 2026-10-21T21:44:41Z | `c-20261021213806-92b2` | yes |
| `exthost.platform` | auto | PASS | 2026-10-18T23:01:21Z | `c-20261018224638-4ca0` | yes |
| `exthost.armed-freeze` | operator, takeover | PASS | 2026-10-21T21:17:17Z | `c-20261021210943-36d1` | yes |
| `forgectrl.tls-records` | auto | PASS | 2026-10-21T21:17:21Z | `c-20261021210943-36d1` | yes |

Artifact sha256: `f6ff2b17fcf347014b7191dab5b30d2f74365ae1b03dcc52da7590ddb215c901`
