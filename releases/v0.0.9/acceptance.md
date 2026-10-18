# ForgeFIRM acceptance - 20261018222431 (dev)

- Image: `20261018222431 (dev)` (forgefirm-image-dev)
- Manifest identity: `70ecd7867c2698d17f2f3c38db772868eda3364f541242c93fb5db4e1d593bcd`
- Catalog: `b38332a30994e16a499a9ded530062291bacabf873138461dd54421e2174c29b`
- Campaign: `c-20261018224638-4ca0` opened 2026-10-18T22:46:38Z
- Exported: 2026-10-18T23:33:36Z
- **Release authorized: YES**
- Tests: 43 total, 43 satisfied (0 inherited), 0 required

| Test | Kind | Result | Run at | Campaign | Inherited |
|---|---|---|---|---|---|
| `image.health` | auto, core | PASS | 2026-10-18T22:46:39Z | `c-20261018224638-4ca0` | no |
| `kernel.latch-locked-idle` | auto, core | PASS | 2026-10-18T22:46:39Z | `c-20261018224638-4ca0` | no |
| `kernel.k1-k2` | auto, core, takeover | PASS | 2026-10-18T22:47:02Z | `c-20261018224638-4ca0` | no |
| `kernel.deadman-close` | auto, core, takeover | PASS | 2026-10-18T22:47:12Z | `c-20261018224638-4ca0` | no |
| `kernel.backtrack-bounds` | auto, core, takeover | PASS | 2026-10-18T22:47:30Z | `c-20261018224638-4ca0` | no |
| `kernel.fire-line` | auto, core, takeover | PASS | 2026-10-18T22:48:14Z | `c-20261018224638-4ca0` | no |
| `kernel.resume-lead` | auto, takeover | PASS | 2026-10-18T22:48:37Z | `c-20261018224638-4ca0` | no |
| `kernel.pic-soc-load` | auto | PASS | 2026-10-18T22:48:41Z | `c-20261018224638-4ca0` | no |
| `forgectrl.auth` | auto | PASS | 2026-10-18T22:48:42Z | `c-20261018224638-4ca0` | no |
| `setup.check-switches` | operator | PASS | 2026-10-18T22:48:47Z | `c-20261018224638-4ca0` | no |
| `setup.check-airflow` | auto, takeover | PASS | 2026-10-18T22:49:27Z | `c-20261018224638-4ca0` | no |
| `setup.check-motion` | auto, takeover | PASS | 2026-10-18T22:50:03Z | `c-20261018224638-4ca0` | no |
| `setup.check-lens` | auto, takeover | PASS | 2026-10-18T22:50:24Z | `c-20261018224638-4ca0` | no |
| `setup.cloud-header-capture` | operator, takeover | PASS | 2026-10-18T23:08:40Z | `c-20261018224638-4ca0` | no |
| `setup.focus` | live, takeover | PASS | 2026-10-18T23:11:20Z | `c-20261018224638-4ca0` | no |
| `motion.pacing` | auto | PASS | 2026-10-18T22:49:43Z | `c-20261018224638-4ca0` | no |
| `motion.jog-roundtrip` | auto | PASS | 2026-10-18T22:50:42Z | `c-20261018224638-4ca0` | no |
| `motion.microstep-modes` | auto | PASS | 2026-10-18T22:51:07Z | `c-20261018224638-4ca0` | no |
| `motion.liveness-probe` | auto | PASS | 2026-10-18T22:51:16Z | `c-20261018224638-4ca0` | no |
| `motion.deadman` | auto | PASS | 2026-10-18T22:52:02Z | `c-20261018224638-4ca0` | no |
| `motion.step-timing-under-load` | auto | PASS | 2026-10-18T22:52:33Z | `c-20261018224638-4ca0` | no |
| `motion.release` | auto, takeover | PASS | 2026-10-18T22:52:59Z | `c-20261018224638-4ca0` | no |
| `cooling.aa-offset-calibrate` | auto | PASS | 2026-10-18T22:54:32Z | `c-20261018224638-4ca0` | no |
| `cooling.flow-verify` | auto | PASS | 2026-10-18T22:57:10Z | `c-20261018224638-4ca0` | no |
| `cooling.flow-under-load` | live | PASS | 2026-10-18T23:12:56Z | `c-20261018224638-4ca0` | no |
| `cooling.fail-tier-stop` | operator | PASS | 2026-10-18T22:57:30Z | `c-20261018224638-4ca0` | no |
| `laser.emission-witness` | live, core | PASS | 2026-10-18T23:09:51Z | `c-20261018224638-4ca0` | no |
| `laser.armed-kill` | live | PASS | 2026-10-18T23:13:40Z | `c-20261018224638-4ca0` | no |
| `laser.pause-resume-lid-cancel` | live | PASS | 2026-10-18T23:14:18Z | `c-20261018224638-4ca0` | no |
| `camera.snapshot` | auto | PASS | 2026-10-18T22:57:47Z | `c-20261018224638-4ca0` | no |
| `camera.sensor-profile` | auto | PASS | 2026-10-18T22:58:00Z | `c-20261018224638-4ca0` | no |
| `camera.h264-stream` | auto | PASS | 2026-10-18T22:58:14Z | `c-20261018224638-4ca0` | no |
| `camera.frame-health` | auto | PASS | 2026-10-18T22:58:32Z | `c-20261018224638-4ca0` | no |
| `camera.lid-privacy` | operator | PASS | 2026-10-18T22:58:48Z | `c-20261018224638-4ca0` | no |
| `update.slots-and-signature` | auto | PASS | 2026-10-18T22:58:50Z | `c-20261018224638-4ca0` | no |
| `cloud.mode-switch` | operator | PASS | 2026-10-18T23:00:59Z | `c-20261018224638-4ca0` | no |
| `cloud.service-protocol` | operator | PASS | 2026-10-18T23:15:30Z | `c-20261018224638-4ca0` | no |
| `cloud.lid-interlock-abort` | live | PASS | 2026-10-18T23:17:46Z | `c-20261018224638-4ca0` | no |
| `cloud.pause-resume` | live | PASS | 2026-10-18T23:21:27Z | `c-20261018224638-4ca0` | no |
| `cloud.oversize-stream` | live | PASS | 2026-10-18T23:24:58Z | `c-20261018224638-4ca0` | no |
| `exthost.platform` | auto | PASS | 2026-10-18T23:01:21Z | `c-20261018224638-4ca0` | no |
| `exthost.armed-freeze` | operator, takeover | PASS | 2026-10-18T23:03:09Z | `c-20261018224638-4ca0` | no |
| `forgectrl.tls-records` | auto | PASS | 2026-10-18T23:03:15Z | `c-20261018224638-4ca0` | no |

Artifact sha256: `97018f73354c1b768b893f1538c43d8c506e06e8bd788ab4e194968910c4e5f1`
