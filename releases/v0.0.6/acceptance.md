# ForgeFIRM acceptance - 20260919221704 (dev)

- Image: `20260919221704 (dev)` (forgefirm-image-dev)
- Manifest identity: `58495afe066c6964484fb07d5664895a4e742e65509c81c10056d9303570f182`
- Catalog: `1f1f7f5daba32301dbf31723ecf2bc4a045a0a6a7ce8647c084876493e79be64`
- Campaign: `c-20260919222216-7903` opened 2026-09-19T22:22:16Z
- Exported: 2026-09-19T22:32:19Z
- **Release authorized: YES**
- Tests: 91 total, 91 satisfied (73 inherited), 0 required

| Test | Kind | Result | Run at | Campaign | Inherited |
|---|---|---|---|---|---|
| `image.health` | auto, core | PASS | 2026-09-19T22:23:01Z | `c-20260919222216-7903` | no |
| `image.license-bundle` | auto | PASS | 2026-09-19T20:29:40Z | `c-20260919202934-3d3a` | yes |
| `image.network-boot` | auto | PASS | 2026-09-19T20:29:41Z | `c-20260919202934-3d3a` | yes |
| `kernel.latch-locked-idle` | auto, core | PASS | 2026-09-19T22:23:02Z | `c-20260919222216-7903` | no |
| `kernel.k1-k2` | auto, core, takeover | PASS | 2026-09-19T22:23:24Z | `c-20260919222216-7903` | no |
| `kernel.deadman-close` | auto, core, takeover | PASS | 2026-09-19T22:23:35Z | `c-20260919222216-7903` | no |
| `kernel.backtrack-bounds` | auto, core, takeover | PASS | 2026-09-19T22:23:54Z | `c-20260919222216-7903` | no |
| `kernel.fire-line` | auto, core, takeover | PASS | 2026-09-19T22:24:38Z | `c-20260919222216-7903` | no |
| `kernel.resume-lead` | auto, takeover | PASS | 2026-09-19T20:31:37Z | `c-20260919202934-3d3a` | yes |
| `kernel.pic-soc-load` | auto | PASS | 2026-09-19T20:31:41Z | `c-20260919202934-3d3a` | yes |
| `forgectrl.auth` | auto | PASS | 2026-09-19T20:31:42Z | `c-20260919202934-3d3a` | yes |
| `forgectrl.settings-bounds` | auto | PASS | 2026-09-19T20:31:43Z | `c-20260919202934-3d3a` | yes |
| `forgectrl.panel-serves` | auto | PASS | 2026-09-19T20:29:37Z | `c-20260919202934-3d3a` | yes |
| `setup.gate-blocks-controllers` | auto, takeover | PASS | 2026-09-19T20:32:00Z | `c-20260919202934-3d3a` | yes |
| `setup.override-until-reboot` | auto | PASS | 2026-09-19T20:32:01Z | `c-20260919202934-3d3a` | yes |
| `setup.advisories-rehash` | auto, takeover | PASS | 2026-09-19T20:32:10Z | `c-20260919202934-3d3a` | yes |
| `setup.account-login` | auto, takeover | PASS | 2026-09-19T20:33:00Z | `c-20260919202934-3d3a` | yes |
| `setup.https-only-writes` | auto | PASS | 2026-09-19T20:33:01Z | `c-20260919202934-3d3a` | yes |
| `setup.ssh-until-reboot` | auto | PASS | 2026-09-19T20:33:02Z | `c-20260919202934-3d3a` | yes |
| `setup.cloud-disabled-surface` | auto | PASS | 2026-09-19T20:33:03Z | `c-20260919202934-3d3a` | yes |
| `setup.factory-return` | auto | PASS | 2026-09-19T20:33:06Z | `c-20260919202934-3d3a` | yes |
| `setup.machine-name` | auto | PASS | 2026-09-19T20:33:07Z | `c-20260919202934-3d3a` | yes |
| `setup.first-run-flow` | operator, takeover | PASS | 2026-09-19T20:33:28Z | `c-20260919202934-3d3a` | yes |
| `setup.first-run-page` | operator, takeover | PASS | 2026-09-19T21:22:41Z | `c-20260919210959-8780` | yes |
| `setup.cert-page` | auto | PASS | 2026-09-19T20:33:29Z | `c-20260919202934-3d3a` | yes |
| `setup.what-changed` | auto, takeover | PASS | 2026-09-19T20:33:38Z | `c-20260919202934-3d3a` | yes |
| `setup.record-export` | auto | PASS | 2026-09-19T20:35:06Z | `c-20260919202934-3d3a` | yes |
| `setup.mirror` | auto, takeover | PASS | 2026-09-19T20:35:36Z | `c-20260919202934-3d3a` | yes |
| `setup.check-switches` | operator | PASS | 2026-09-19T20:35:42Z | `c-20260919202934-3d3a` | yes |
| `setup.check-sensors` | auto | PASS | 2026-09-19T20:35:18Z | `c-20260919202934-3d3a` | yes |
| `setup.check-airflow` | auto, takeover | PASS | 2026-09-19T20:37:14Z | `c-20260919202934-3d3a` | yes |
| `setup.check-cameras` | operator | PASS | 2026-09-19T20:37:43Z | `c-20260919202934-3d3a` | yes |
| `setup.check-motion` | auto, takeover | PASS | 2026-09-19T20:38:19Z | `c-20260919202934-3d3a` | yes |
| `setup.check-flow-verify` | auto, takeover | PASS | 2026-09-19T20:45:43Z | `c-20260919202934-3d3a` | yes |
| `setup.cloud-header-capture` | operator, takeover | PASS | 2026-09-19T21:23:44Z | `c-20260919210959-8780` | yes |
| `setup.sheet` | live, takeover | PASS | 2026-09-19T21:36:30Z | `c-20260919210959-8780` | yes |
| `logs.tree-tail-export` | auto | PASS | 2026-09-19T20:34:22Z | `c-20260919202934-3d3a` | yes |
| `logs.routing` | auto | PASS | 2026-09-19T20:45:47Z | `c-20260919202934-3d3a` | yes |
| `logs.level-settings` | auto | PASS | 2026-09-19T20:45:48Z | `c-20260919202934-3d3a` | yes |
| `motion.pacing` | auto | PASS | 2026-09-19T20:37:58Z | `c-20260919202934-3d3a` | yes |
| `motion.jog-roundtrip` | auto | PASS | 2026-09-19T20:46:03Z | `c-20260919202934-3d3a` | yes |
| `motion.microstep-modes` | auto | PASS | 2026-09-19T20:46:28Z | `c-20260919202934-3d3a` | yes |
| `motion.liveness-probe` | auto | PASS | 2026-09-19T20:46:37Z | `c-20260919202934-3d3a` | yes |
| `motion.gate-waits-for-lid` | operator | PASS | 2026-09-19T20:46:53Z | `c-20260919202934-3d3a` | yes |
| `motion.cancel-abort` | auto | PASS | 2026-09-19T20:47:00Z | `c-20260919202934-3d3a` | yes |
| `motion.deadman` | auto | PASS | 2026-09-19T20:47:47Z | `c-20260919202934-3d3a` | yes |
| `motion.respawn-gate` | operator | PASS | 2026-09-19T20:47:55Z | `c-20260919202934-3d3a` | yes |
| `motion.soft-limits` | auto | PASS | 2026-09-19T20:49:56Z | `c-20260919202934-3d3a` | yes |
| `motion.button-hold-resume` | operator | PASS | 2026-09-19T20:50:08Z | `c-20260919202934-3d3a` | yes |
| `motion.lid-cancel-home` | operator | PASS | 2026-09-19T20:50:24Z | `c-20260919202934-3d3a` | yes |
| `motion.interlock-cancel-home` | operator | PASS | 2026-09-19T20:50:32Z | `c-20260919202934-3d3a` | yes |
| `motion.lid-policy-hold` | operator | PASS | 2026-09-19T20:50:46Z | `c-20260919202934-3d3a` | yes |
| `motion.step-timing-under-load` | auto | PASS | 2026-09-19T20:51:16Z | `c-20260919202934-3d3a` | yes |
| `cooling.aa-offset-calibrate` | auto | PASS | 2026-09-19T22:11:27Z | `c-20260919220954-20d3` | yes |
| `cooling.flow-verify` | auto | PASS | 2026-09-19T22:27:19Z | `c-20260919222216-7903` | no |
| `cooling.fans-quiet-after-motion` | auto | PASS | 2026-09-19T22:28:20Z | `c-20260919222216-7903` | no |
| `cooling.gate-off` | auto | PASS | 2026-09-19T22:28:28Z | `c-20260919222216-7903` | no |
| `cooling.floor-and-warm-up` | auto | PASS | 2026-09-19T22:30:09Z | `c-20260919222216-7903` | no |
| `cooling.tec-drive` | auto | PASS | 2026-09-19T22:30:18Z | `c-20260919222216-7903` | no |
| `cooling.fire-watch-tiers` | auto | PASS | 2026-09-19T22:30:33Z | `c-20260919222216-7903` | no |
| `cooling.crash-watch-plumbing` | auto | PASS | 2026-09-19T22:30:41Z | `c-20260919222216-7903` | no |
| `cooling.critical-tier` | auto | PASS | 2026-09-19T22:30:49Z | `c-20260919222216-7903` | no |
| `cooling.fan-gate-trips` | auto | PASS | 2026-09-19T22:31:34Z | `c-20260919222216-7903` | no |
| `cooling.flow-under-load` | live | PASS | 2026-09-19T21:52:48Z | `c-20260919215024-c402` | yes |
| `cooling.fan-duty-readback` | auto | PASS | 2026-09-19T22:31:49Z | `c-20260919222216-7903` | no |
| `cooling.fail-tier-stop` | operator | PASS | 2026-09-19T22:32:10Z | `c-20260919222216-7903` | no |
| `laser.power-floor` | auto | PASS | 2026-09-19T21:11:53Z | `c-20260919210959-8780` | yes |
| `laser.emission-witness` | live, core | PASS | 2026-09-19T22:22:57Z | `c-20260919222216-7903` | no |
| `laser.m5-rapid-dark` | live | PASS | 2026-09-19T21:53:28Z | `c-20260919215024-c402` | yes |
| `laser.disarm-in-hold` | live | PASS | 2026-09-19T21:57:17Z | `c-20260919215024-c402` | yes |
| `laser.armed-kill` | live | PASS | 2026-09-19T21:58:06Z | `c-20260919215024-c402` | yes |
| `laser.verdict-cut` | live | PASS | 2026-09-19T21:58:41Z | `c-20260919215024-c402` | yes |
| `laser.arm-wait-lid` | operator | PASS | 2026-09-19T21:12:13Z | `c-20260919210959-8780` | yes |
| `laser.pause-resume-lid-cancel` | live | PASS | 2026-09-19T21:59:14Z | `c-20260919215024-c402` | yes |
| `camera.snapshot` | auto | PASS | 2026-09-19T20:37:29Z | `c-20260919202934-3d3a` | yes |
| `camera.sensor-profile` | auto | PASS | 2026-09-19T21:12:25Z | `c-20260919210959-8780` | yes |
| `camera.h264-stream` | auto | PASS | 2026-09-19T21:12:38Z | `c-20260919210959-8780` | yes |
| `camera.frame-health` | auto | PASS | 2026-09-19T21:12:50Z | `c-20260919210959-8780` | yes |
| `camera.lid-privacy` | operator | PASS | 2026-09-19T21:13:04Z | `c-20260919210959-8780` | yes |
| `camera.key-read` | auto | PASS | 2026-09-19T21:13:05Z | `c-20260919210959-8780` | yes |
| `update.slots-and-signature` | auto | PASS | 2026-09-19T20:33:05Z | `c-20260919202934-3d3a` | yes |
| `update.release-check` | auto | PASS | 2026-09-19T21:13:06Z | `c-20260919210959-8780` | yes |
| `cloud.mode-switch` | operator | PASS | 2026-09-19T20:49:38Z | `c-20260919202934-3d3a` | yes |
| `cloud.service-protocol` | operator | PASS | 2026-09-19T22:00:08Z | `c-20260919215024-c402` | yes |
| `cloud.lid-interlock-abort` | live | PASS | 2026-09-19T22:01:17Z | `c-20260919215024-c402` | yes |
| `cloud.lid-during-button-wait` | operator | PASS | 2026-09-19T21:13:46Z | `c-20260919210959-8780` | yes |
| `cloud.dark-print` | operator | PASS | 2026-09-19T21:14:40Z | `c-20260919210959-8780` | yes |
| `cloud.verdict-refuse` | operator | PASS | 2026-09-19T21:16:03Z | `c-20260919210959-8780` | yes |
| `cloud.pause-resume` | live | PASS | 2026-09-19T22:03:16Z | `c-20260919215024-c402` | yes |
| `cloud.oversize-stream` | live | PASS | 2026-09-19T21:18:52Z | `c-20260919210959-8780` | yes |
| `cloud.paused-lid-cancel` | live | PASS | 2026-09-19T22:04:00Z | `c-20260919215024-c402` | yes |

Artifact sha256: `74f10b1e46fb0097199b179607546fb9964252915c74fcb6c09c23d81b421972`
