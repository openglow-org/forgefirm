# ForgeFIRM acceptance - 20261112205721 (dev)

- Image: `20261112205721 (dev)` (forgefirm-image-dev)
- Manifest identity: `66e88a51ec745b068a7ac7257ee23b9e6659516ac3a1f5aac13de36fd3d4f819`
- Catalog: `1308927a8698cb046505f8873f31e18084d11e10c7760e4da6ec682415082c20`
- Campaign: `c-20261112210649-3fc2` opened 2026-11-12T21:06:49Z
- Exported: 2026-11-13T20:51:27Z
- **Release authorized: YES**
- Tests: 42 total, 42 satisfied (11 inherited), 0 required

| Test | Kind | Result | Run at | Campaign | Inherited |
|---|---|---|---|---|---|
| `image.health` | auto, core | PASS | 2026-11-12T21:06:51Z | `c-20261112210649-3fc2` | no |
| `kernel.latch-locked-idle` | auto, core | PASS | 2026-11-12T21:06:52Z | `c-20261112210649-3fc2` | no |
| `kernel.k1-k2` | auto, core, takeover | PASS | 2026-11-12T21:07:15Z | `c-20261112210649-3fc2` | no |
| `kernel.deadman-close` | auto, core, takeover | PASS | 2026-11-12T21:07:27Z | `c-20261112210649-3fc2` | no |
| `kernel.backtrack-bounds` | auto, core, takeover | PASS | 2026-11-12T21:07:47Z | `c-20261112210649-3fc2` | no |
| `kernel.fire-line` | auto, core, takeover | PASS | 2026-11-12T21:08:22Z | `c-20261112210649-3fc2` | no |
| `kernel.resume-lead` | auto, takeover | PASS | 2026-11-12T21:08:37Z | `c-20261112210649-3fc2` | no |
| `kernel.pic-soc-load` | auto | PASS | 2026-11-12T21:08:40Z | `c-20261112210649-3fc2` | no |
| `forgectrl.auth` | auto | PASS | 2026-11-12T00:40:21Z | `c-20261112003811-edec` | yes |
| `setup.check-switches` | operator | PASS | 2026-11-12T00:40:26Z | `c-20261112003811-edec` | yes |
| `setup.check-airflow` | auto, takeover | PASS | 2026-11-12T00:41:07Z | `c-20261112003811-edec` | yes |
| `setup.check-motion` | auto, takeover | PASS | 2026-11-12T00:41:43Z | `c-20261112003811-edec` | yes |
| `setup.check-lens` | auto, takeover | PASS | 2026-11-12T00:42:17Z | `c-20261112003811-edec` | yes |
| `setup.cloud-header-capture` | operator, takeover | PASS | 2026-11-13T20:18:58Z | `c-20261112210649-3fc2` | no |
| `setup.focus` | live, takeover | PASS | 2026-11-13T20:21:43Z | `c-20261112210649-3fc2` | no |
| `motion.pacing` | auto | PASS | 2026-11-12T21:08:56Z | `c-20261112210649-3fc2` | no |
| `motion.jog-roundtrip` | auto | PASS | 2026-11-12T21:09:14Z | `c-20261112210649-3fc2` | no |
| `motion.microstep-modes` | auto | PASS | 2026-11-12T21:09:39Z | `c-20261112210649-3fc2` | no |
| `motion.liveness-probe` | auto | PASS | 2026-11-12T21:09:48Z | `c-20261112210649-3fc2` | no |
| `motion.deadman` | auto | PASS | 2026-11-12T21:10:35Z | `c-20261112210649-3fc2` | no |
| `motion.step-timing-under-load` | auto | PASS | 2026-11-12T21:11:05Z | `c-20261112210649-3fc2` | no |
| `motion.release` | auto, takeover | PASS | 2026-11-12T21:11:33Z | `c-20261112210649-3fc2` | no |
| `cooling.aa-offset-calibrate` | auto | PASS | 2026-11-12T00:46:21Z | `c-20261112003811-edec` | yes |
| `cooling.fail-tier-stop` | operator | PASS | 2026-11-12T00:46:38Z | `c-20261112003811-edec` | yes |
| `laser.emission-witness` | live, core | PASS | 2026-11-13T20:20:12Z | `c-20261112210649-3fc2` | no |
| `laser.armed-kill` | live | PASS | 2026-11-13T20:22:57Z | `c-20261112210649-3fc2` | no |
| `laser.pause-resume-lid-cancel` | live | PASS | 2026-11-13T20:23:59Z | `c-20261112210649-3fc2` | no |
| `camera.snapshot` | auto | PASS | 2026-11-12T21:11:50Z | `c-20261112210649-3fc2` | no |
| `camera.sensor-profile` | auto | PASS | 2026-11-12T21:12:03Z | `c-20261112210649-3fc2` | no |
| `camera.h264-stream` | auto | PASS | 2026-11-12T21:12:16Z | `c-20261112210649-3fc2` | no |
| `camera.frame-health` | auto | PASS | 2026-11-12T21:12:35Z | `c-20261112210649-3fc2` | no |
| `camera.lid-privacy` | operator | PASS | 2026-11-12T21:12:51Z | `c-20261112210649-3fc2` | no |
| `update.slots-and-signature` | auto | PASS | 2026-11-12T00:52:39Z | `c-20261112005039-d6cc` | yes |
| `cloud.mode-switch` | operator | PASS | 2026-11-12T00:54:39Z | `c-20261112005039-d6cc` | yes |
| `cloud.service-protocol` | operator | PASS | 2026-11-13T20:25:30Z | `c-20261112210649-3fc2` | no |
| `cloud.lid-interlock-abort` | live | PASS | 2026-11-13T20:27:54Z | `c-20261112210649-3fc2` | no |
| `cloud.pause-resume` | live | PASS | 2026-11-13T20:32:01Z | `c-20261112210649-3fc2` | no |
| `cloud.oversize-stream` | live | PASS | 2026-11-13T20:35:39Z | `c-20261112210649-3fc2` | no |
| `cloud.button-latch` | live | PASS | 2026-11-13T20:37:05Z | `c-20261112210649-3fc2` | no |
| `exthost.platform` | auto | PASS | 2026-11-12T00:55:02Z | `c-20261112005039-d6cc` | yes |
| `exthost.armed-freeze` | operator, takeover | PASS | 2026-11-12T21:14:45Z | `c-20261112210649-3fc2` | no |
| `forgectrl.tls-records` | auto | PASS | 2026-11-12T00:57:01Z | `c-20261112005039-d6cc` | yes |

Artifact sha256: `8fa0dc590a9334a10f5d651c2c74a2e039d8383a48a345b452927fc5ed8136a8`
