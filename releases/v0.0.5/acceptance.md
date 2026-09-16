# ForgeFIRM acceptance - 20260915225440 (dev)

- Image: `20260915225440 (dev)` (forgefirm-image-dev)
- Manifest identity: `371bf2b8e0779a641b844b48e18b7fbe1a13adee0f6bf8706d1a720d3e000d22`
- Catalog: `7929ef2ce875fd4cccec3f794f7d49ceadd46bdd5068a3cab75b021947198525`
- Campaign: `c-20260915231006-07cc` opened 2026-09-15T23:10:06Z
- Exported: 2026-09-15T23:59:10Z
- **Release authorized: YES**
- Tests: 90 total, 90 satisfied (53 inherited), 0 required

| Test | Kind | Result | Run at | Campaign | Inherited |
|---|---|---|---|---|---|
| `image.health` | auto, core | PASS | 2026-09-15T23:10:06Z | `c-20260915231006-07cc` | no |
| `image.license-bundle` | auto | PASS | 2026-09-15T18:50:17Z | `c-20260915185011-0c8a` | yes |
| `kernel.latch-locked-idle` | auto, core | PASS | 2026-09-15T23:10:07Z | `c-20260915231006-07cc` | no |
| `kernel.k1-k2` | auto, core, takeover | PASS | 2026-09-15T23:10:28Z | `c-20260915231006-07cc` | no |
| `kernel.deadman-close` | auto, core, takeover | PASS | 2026-09-15T23:10:38Z | `c-20260915231006-07cc` | no |
| `kernel.backtrack-bounds` | auto, core, takeover | PASS | 2026-09-15T23:10:56Z | `c-20260915231006-07cc` | no |
| `kernel.fire-line` | auto, core, takeover | PASS | 2026-09-15T23:11:39Z | `c-20260915231006-07cc` | no |
| `kernel.resume-lead` | auto, takeover | PASS | 2026-09-11T17:31:23Z | `c-20260911172921-9426` | yes |
| `kernel.pic-soc-load` | auto | PASS | 2026-09-11T17:31:26Z | `c-20260911172921-9426` | yes |
| `forgectrl.auth` | auto | PASS | 2026-09-15T18:51:51Z | `c-20260915185011-0c8a` | yes |
| `forgectrl.settings-bounds` | auto | PASS | 2026-09-15T18:51:52Z | `c-20260915185011-0c8a` | yes |
| `forgectrl.panel-serves` | auto | PASS | 2026-09-15T23:11:41Z | `c-20260915231006-07cc` | no |
| `setup.gate-blocks-controllers` | auto, takeover | PASS | 2026-09-15T18:52:10Z | `c-20260915185011-0c8a` | yes |
| `setup.override-until-reboot` | auto | PASS | 2026-09-15T18:52:11Z | `c-20260915185011-0c8a` | yes |
| `setup.advisories-rehash` | auto, takeover | PASS | 2026-09-15T18:52:19Z | `c-20260915185011-0c8a` | yes |
| `setup.account-login` | auto, takeover | PASS | 2026-09-15T18:53:08Z | `c-20260915185011-0c8a` | yes |
| `setup.https-only-writes` | auto | PASS | 2026-09-15T18:53:09Z | `c-20260915185011-0c8a` | yes |
| `setup.ssh-until-reboot` | auto | PASS | 2026-09-15T18:53:10Z | `c-20260915185011-0c8a` | yes |
| `setup.cloud-disabled-surface` | auto | PASS | 2026-09-15T18:53:11Z | `c-20260915185011-0c8a` | yes |
| `setup.factory-return` | auto | PASS | 2026-09-15T18:53:12Z | `c-20260915185011-0c8a` | yes |
| `setup.machine-name` | auto | PASS | 2026-09-15T18:53:13Z | `c-20260915185011-0c8a` | yes |
| `setup.first-run-flow` | operator, takeover | PASS | 2026-09-15T18:53:33Z | `c-20260915185011-0c8a` | yes |
| `setup.first-run-page` | operator, takeover | PASS | 2026-09-15T23:26:39Z | `c-20260915231006-07cc` | no |
| `setup.cert-page` | auto | PASS | 2026-09-15T18:53:34Z | `c-20260915185011-0c8a` | yes |
| `setup.what-changed` | auto, takeover | PASS | 2026-09-15T18:53:44Z | `c-20260915185011-0c8a` | yes |
| `setup.record-export` | auto | PASS | 2026-09-15T18:55:14Z | `c-20260915185011-0c8a` | yes |
| `setup.mirror` | auto, takeover | PASS | 2026-09-15T18:55:44Z | `c-20260915185011-0c8a` | yes |
| `setup.check-switches` | operator | PASS | 2026-09-15T18:55:49Z | `c-20260915185011-0c8a` | yes |
| `setup.check-sensors` | auto | PASS | 2026-09-15T18:55:25Z | `c-20260915185011-0c8a` | yes |
| `setup.check-airflow` | auto, takeover | PASS | 2026-09-15T18:57:23Z | `c-20260915185011-0c8a` | yes |
| `setup.check-cameras` | operator | PASS | 2026-09-15T18:57:51Z | `c-20260915185011-0c8a` | yes |
| `setup.check-motion` | auto, takeover | PASS | 2026-09-15T18:58:27Z | `c-20260915185011-0c8a` | yes |
| `setup.check-flow-verify` | auto, takeover | PASS | 2026-09-15T19:05:54Z | `c-20260915185011-0c8a` | yes |
| `setup.cloud-header-capture` | operator, takeover | PASS | 2026-09-15T23:28:02Z | `c-20260915231006-07cc` | no |
| `setup.sheet` | live, takeover | PASS | 2026-09-15T23:40:57Z | `c-20260915231006-07cc` | no |
| `logs.tree-tail-export` | auto | PASS | 2026-09-15T18:54:29Z | `c-20260915185011-0c8a` | yes |
| `logs.routing` | auto | PASS | 2026-09-15T19:05:59Z | `c-20260915185011-0c8a` | yes |
| `logs.level-settings` | auto | PASS | 2026-09-15T19:06:00Z | `c-20260915185011-0c8a` | yes |
| `motion.pacing` | auto | PASS | 2026-09-15T23:11:57Z | `c-20260915231006-07cc` | no |
| `motion.jog-roundtrip` | auto | PASS | 2026-09-15T23:12:10Z | `c-20260915231006-07cc` | no |
| `motion.microstep-modes` | auto | PASS | 2026-09-15T23:12:36Z | `c-20260915231006-07cc` | no |
| `motion.liveness-probe` | auto | PASS | 2026-09-15T20:59:39Z | `c-20260915205702-a7b9` | yes |
| `motion.gate-waits-for-lid` | operator | PASS | 2026-09-15T20:59:55Z | `c-20260915205702-a7b9` | yes |
| `motion.cancel-abort` | auto | PASS | 2026-09-15T23:12:43Z | `c-20260915231006-07cc` | no |
| `motion.deadman` | auto | PASS | 2026-09-15T23:13:29Z | `c-20260915231006-07cc` | no |
| `motion.respawn-gate` | operator | PASS | 2026-09-15T23:13:38Z | `c-20260915231006-07cc` | no |
| `motion.soft-limits` | auto | PASS | 2026-09-15T23:15:35Z | `c-20260915231006-07cc` | no |
| `motion.button-hold-resume` | operator | PASS | 2026-09-15T23:15:46Z | `c-20260915231006-07cc` | no |
| `motion.lid-cancel-home` | operator | PASS | 2026-09-15T23:16:02Z | `c-20260915231006-07cc` | no |
| `motion.interlock-cancel-home` | operator | PASS | 2026-09-15T23:16:10Z | `c-20260915231006-07cc` | no |
| `motion.lid-policy-hold` | operator | PASS | 2026-09-15T23:16:24Z | `c-20260915231006-07cc` | no |
| `motion.step-timing-under-load` | auto | PASS | 2026-09-15T23:16:54Z | `c-20260915231006-07cc` | no |
| `cooling.aa-offset-calibrate` | auto | PASS | 2026-09-15T19:00:00Z | `c-20260915185011-0c8a` | yes |
| `cooling.flow-verify` | auto | PASS | 2026-09-15T19:02:43Z | `c-20260915185011-0c8a` | yes |
| `cooling.fans-quiet-after-motion` | auto | PASS | 2026-09-15T21:04:26Z | `c-20260915205702-a7b9` | yes |
| `cooling.gate-off` | auto | PASS | 2026-09-15T18:55:57Z | `c-20260915185011-0c8a` | yes |
| `cooling.floor-and-warm-up` | auto | PASS | 2026-09-15T21:05:53Z | `c-20260915205702-a7b9` | yes |
| `cooling.tec-drive` | auto | PASS | 2026-09-15T21:06:01Z | `c-20260915205702-a7b9` | yes |
| `cooling.fire-watch-tiers` | auto | PASS | 2026-09-15T21:06:17Z | `c-20260915205702-a7b9` | yes |
| `cooling.crash-watch-plumbing` | auto | PASS | 2026-09-15T21:06:25Z | `c-20260915205702-a7b9` | yes |
| `cooling.critical-tier` | auto | PASS | 2026-09-15T21:06:34Z | `c-20260915205702-a7b9` | yes |
| `cooling.fan-gate-trips` | auto | PASS | 2026-09-15T18:56:43Z | `c-20260915185011-0c8a` | yes |
| `cooling.flow-under-load` | live | PASS | 2026-09-15T23:45:14Z | `c-20260915231006-07cc` | no |
| `cooling.fan-duty-readback` | auto | PASS | 2026-09-15T21:06:50Z | `c-20260915205702-a7b9` | yes |
| `cooling.fail-tier-stop` | operator | PASS | 2026-09-15T21:19:25Z | `c-20260915211732-5800` | yes |
| `laser.power-floor` | auto | PASS | 2026-09-15T23:16:56Z | `c-20260915231006-07cc` | no |
| `laser.emission-witness` | live, core | PASS | 2026-09-15T23:28:39Z | `c-20260915231006-07cc` | no |
| `laser.m5-rapid-dark` | live | PASS | 2026-09-15T23:45:54Z | `c-20260915231006-07cc` | no |
| `laser.disarm-in-hold` | live | PASS | 2026-09-15T23:47:56Z | `c-20260915231006-07cc` | no |
| `laser.armed-kill` | live | PASS | 2026-09-15T23:48:48Z | `c-20260915231006-07cc` | no |
| `laser.verdict-cut` | live | PASS | 2026-09-15T23:49:28Z | `c-20260915231006-07cc` | no |
| `laser.arm-wait-lid` | operator | PASS | 2026-09-15T23:16:59Z | `c-20260915231006-07cc` | no |
| `laser.pause-resume-lid-cancel` | live | PASS | 2026-09-15T23:49:58Z | `c-20260915231006-07cc` | no |
| `camera.snapshot` | auto | PASS | 2026-09-15T18:57:38Z | `c-20260915185011-0c8a` | yes |
| `camera.sensor-profile` | auto | PASS | 2026-09-15T21:19:42Z | `c-20260915211732-5800` | yes |
| `camera.h264-stream` | auto | PASS | 2026-09-15T21:19:55Z | `c-20260915211732-5800` | yes |
| `camera.frame-health` | auto | PASS | 2026-09-15T21:20:07Z | `c-20260915211732-5800` | yes |
| `camera.lid-privacy` | operator | PASS | 2026-09-15T21:20:21Z | `c-20260915211732-5800` | yes |
| `camera.key-read` | auto | PASS | 2026-09-15T21:20:22Z | `c-20260915211732-5800` | yes |
| `update.slots-and-signature` | auto | PASS | 2026-09-12T16:59:48Z | `c-20260912165539-ac51` | yes |
| `update.release-check` | auto | PASS | 2026-09-15T21:20:23Z | `c-20260915211732-5800` | yes |
| `cloud.mode-switch` | operator | PASS | 2026-09-15T23:15:16Z | `c-20260915231006-07cc` | no |
| `cloud.service-protocol` | operator | PASS | 2026-09-15T23:51:07Z | `c-20260915231006-07cc` | no |
| `cloud.lid-interlock-abort` | live | PASS | 2026-09-15T23:52:16Z | `c-20260915231006-07cc` | no |
| `cloud.lid-during-button-wait` | operator | PASS | 2026-09-15T21:21:03Z | `c-20260915211732-5800` | yes |
| `cloud.dark-print` | operator | PASS | 2026-09-15T21:21:58Z | `c-20260915211732-5800` | yes |
| `cloud.verdict-refuse` | operator | PASS | 2026-09-15T21:23:20Z | `c-20260915211732-5800` | yes |
| `cloud.pause-resume` | live | PASS | 2026-09-15T23:54:31Z | `c-20260915231006-07cc` | no |
| `cloud.oversize-stream` | live | PASS | 2026-09-15T23:57:18Z | `c-20260915231006-07cc` | no |
| `cloud.paused-lid-cancel` | live | PASS | 2026-09-15T23:57:46Z | `c-20260915231006-07cc` | no |

Artifact sha256: `35e54b5db01fb23eb9091302e86d158852f9b4eaddca2a127a1bc957efb2ea0c`
