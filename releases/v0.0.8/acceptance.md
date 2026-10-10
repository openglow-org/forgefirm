# ForgeFIRM acceptance - 20261010224118 (dev)

- Image: `20261010224118 (dev)` (forgefirm-image-dev)
- Manifest identity: `c25844db9b022c976fbc02f5db9dc46bc2cde776cd6a9c90e7d789214ebe257e`
- Catalog: `ee8534fed1b8e932ad5a9367f1b38844c9bdce1abe625ba9346bc042a5bd7b1d`
- Campaign: `c-20261010224522-439d` opened 2026-10-10T22:45:22Z
- Exported: 2026-10-10T23:40:44Z
- **Release authorized: YES**
- Tests: 128 total, 128 satisfied (80 inherited), 0 required

| Test | Kind | Result | Run at | Campaign | Inherited |
|---|---|---|---|---|---|
| `image.health` | auto, core | PASS | 2026-10-10T22:45:22Z | `c-20261010224522-439d` | no |
| `image.license-bundle` | auto | PASS | 2026-10-10T21:42:55Z | `c-20261010214232-4790` | yes |
| `image.network-boot` | auto | PASS | 2026-09-28T16:23:38Z | `c-20260928162331-3969` | yes |
| `kernel.latch-locked-idle` | auto, core | PASS | 2026-10-10T22:45:23Z | `c-20261010224522-439d` | no |
| `kernel.k1-k2` | auto, core, takeover | PASS | 2026-10-10T22:45:45Z | `c-20261010224522-439d` | no |
| `kernel.deadman-close` | auto, core, takeover | PASS | 2026-10-10T22:45:55Z | `c-20261010224522-439d` | no |
| `kernel.backtrack-bounds` | auto, core, takeover | PASS | 2026-10-10T22:46:15Z | `c-20261010224522-439d` | no |
| `kernel.fire-line` | auto, core, takeover | PASS | 2026-10-10T22:46:59Z | `c-20261010224522-439d` | no |
| `kernel.resume-lead` | auto, takeover | PASS | 2026-09-28T16:25:37Z | `c-20260928162331-3969` | yes |
| `kernel.pic-soc-load` | auto | PASS | 2026-09-28T16:25:41Z | `c-20260928162331-3969` | yes |
| `forgectrl.auth` | auto | PASS | 2026-10-10T21:45:06Z | `c-20261010214232-4790` | yes |
| `forgectrl.settings-bounds` | auto | PASS | 2026-10-10T21:45:13Z | `c-20261010214232-4790` | yes |
| `forgectrl.panel-serves` | auto | PASS | 2026-10-10T21:42:46Z | `c-20261010214232-4790` | yes |
| `events.stream` | auto | PASS | 2026-10-10T21:45:29Z | `c-20261010214232-4790` | yes |
| `forgectrl.lease` | auto | PASS | 2026-10-10T21:45:39Z | `c-20261010214232-4790` | yes |
| `forgectrl.tokens` | auto | PASS | 2026-10-10T21:45:48Z | `c-20261010214232-4790` | yes |
| `setup.gate-blocks-controllers` | auto, takeover | PASS | 2026-10-10T21:46:11Z | `c-20261010214232-4790` | yes |
| `setup.override-until-reboot` | auto | PASS | 2026-10-10T21:46:18Z | `c-20261010214232-4790` | yes |
| `setup.advisories-rehash` | auto, takeover | PASS | 2026-10-10T21:46:33Z | `c-20261010214232-4790` | yes |
| `setup.account-login` | auto, takeover | PASS | 2026-10-10T21:47:28Z | `c-20261010214232-4790` | yes |
| `setup.https-only-writes` | auto | PASS | 2026-10-10T21:47:35Z | `c-20261010214232-4790` | yes |
| `setup.ssh-until-reboot` | auto | PASS | 2026-10-10T21:47:42Z | `c-20261010214232-4790` | yes |
| `setup.cloud-disabled-surface` | auto | PASS | 2026-10-10T21:47:49Z | `c-20261010214232-4790` | yes |
| `setup.extensions-consent` | auto, takeover | PASS | 2026-10-10T21:48:04Z | `c-20261010214232-4790` | yes |
| `setup.factory-return` | auto | PASS | 2026-10-10T21:48:11Z | `c-20261010214232-4790` | yes |
| `setup.machine-name` | auto | PASS | 2026-09-28T16:27:36Z | `c-20260928162331-3969` | yes |
| `setup.first-run-flow` | operator, takeover | PASS | 2026-10-10T22:47:21Z | `c-20261010224522-439d` | no |
| `setup.first-run-page` | operator, takeover | PASS | 2026-09-28T18:52:23Z | `c-20260928183105-64ee` | yes |
| `setup.cert-page` | auto | PASS | 2026-10-10T21:48:17Z | `c-20261010214232-4790` | yes |
| `setup.what-changed` | auto, takeover | PASS | 2026-10-10T21:48:33Z | `c-20261010214232-4790` | yes |
| `setup.record-export` | auto | PASS | 2026-10-10T21:50:38Z | `c-20261010214232-4790` | yes |
| `setup.mirror` | auto, takeover | PASS | 2026-10-10T21:51:21Z | `c-20261010214232-4790` | yes |
| `setup.check-switches` | operator | PASS | 2026-10-10T22:47:26Z | `c-20261010224522-439d` | no |
| `setup.check-sensors` | auto | PASS | 2026-10-10T21:50:55Z | `c-20261010214232-4790` | yes |
| `setup.check-airflow` | auto, takeover | PASS | 2026-10-10T21:53:11Z | `c-20261010214232-4790` | yes |
| `setup.check-cameras` | operator | PASS | 2026-10-10T22:47:39Z | `c-20261010224522-439d` | no |
| `setup.check-motion` | auto, takeover | PASS | 2026-10-10T21:53:38Z | `c-20261010214232-4790` | yes |
| `setup.check-flow-verify` | auto, takeover | PASS | 2026-10-10T22:01:19Z | `c-20261010214232-4790` | yes |
| `setup.cloud-header-capture` | operator, takeover | PASS | 2026-10-10T23:04:58Z | `c-20261010224522-439d` | no |
| `setup.sheet` | live, takeover | PASS | 2026-10-10T23:18:06Z | `c-20261010224522-439d` | no |
| `logs.tree-tail-export` | auto | PASS | 2026-10-10T21:49:36Z | `c-20261010214232-4790` | yes |
| `logs.routing` | auto | PASS | 2026-09-28T16:40:28Z | `c-20260928162331-3969` | yes |
| `logs.level-settings` | auto | PASS | 2026-10-10T22:01:26Z | `c-20261010214232-4790` | yes |
| `motion.pacing` | auto | PASS | 2026-09-28T16:32:45Z | `c-20260928162331-3969` | yes |
| `motion.jog-roundtrip` | auto | PASS | 2026-09-28T16:40:44Z | `c-20260928162331-3969` | yes |
| `motion.microstep-modes` | auto | PASS | 2026-10-10T22:01:57Z | `c-20261010214232-4790` | yes |
| `motion.liveness-probe` | auto | PASS | 2026-09-28T16:41:18Z | `c-20260928162331-3969` | yes |
| `motion.gate-waits-for-lid` | operator | PASS | 2026-10-10T22:47:56Z | `c-20261010224522-439d` | no |
| `motion.cancel-abort` | auto | PASS | 2026-09-28T16:41:41Z | `c-20260928162331-3969` | yes |
| `motion.deadman` | auto | PASS | 2026-10-10T22:02:49Z | `c-20261010214232-4790` | yes |
| `motion.respawn-gate` | operator | PASS | 2026-09-28T16:42:35Z | `c-20260928162331-3969` | yes |
| `motion.soft-limits` | auto | PASS | 2026-09-28T16:53:34Z | `c-20260928165212-b92e` | yes |
| `motion.button-hold-resume` | operator | PASS | 2026-09-28T17:12:49Z | `c-20260928171100-3ff5` | yes |
| `motion.lid-cancel-home` | operator | PASS | 2026-09-28T17:13:04Z | `c-20260928171100-3ff5` | yes |
| `motion.interlock-cancel-home` | operator | PASS | 2026-09-28T17:13:12Z | `c-20260928171100-3ff5` | yes |
| `motion.lid-policy-hold` | operator | PASS | 2026-09-28T17:13:26Z | `c-20260928171100-3ff5` | yes |
| `motion.step-timing-under-load` | auto | PASS | 2026-09-28T17:13:56Z | `c-20260928171100-3ff5` | yes |
| `motion.release` | auto, takeover | PASS | 2026-10-10T22:03:22Z | `c-20261010214232-4790` | yes |
| `homing.manual` | auto | PASS | 2026-10-10T22:03:39Z | `c-20261010214232-4790` | yes |
| `motion.port-jog` | auto | PASS | 2026-10-10T22:04:05Z | `c-20261010214232-4790` | yes |
| `motion.job` | auto | PASS | 2026-10-10T22:04:21Z | `c-20261010214232-4790` | yes |
| `cooling.aa-offset-calibrate` | auto | PASS | 2026-10-10T21:55:16Z | `c-20261010214232-4790` | yes |
| `cooling.flow-verify` | auto | PASS | 2026-10-10T21:58:05Z | `c-20261010214232-4790` | yes |
| `cooling.fans-quiet-after-motion` | auto | PASS | 2026-10-10T22:05:23Z | `c-20261010214232-4790` | yes |
| `cooling.gate-off` | auto | PASS | 2026-10-10T21:51:35Z | `c-20261010214232-4790` | yes |
| `cooling.floor-and-warm-up` | auto | PASS | 2026-10-10T22:06:55Z | `c-20261010214232-4790` | yes |
| `cooling.tec-drive` | auto | PASS | 2026-10-10T22:07:10Z | `c-20261010214232-4790` | yes |
| `cooling.fire-watch-tiers` | auto | PASS | 2026-10-10T22:07:31Z | `c-20261010214232-4790` | yes |
| `cooling.crash-watch-plumbing` | auto | PASS | 2026-10-10T22:07:44Z | `c-20261010214232-4790` | yes |
| `cooling.critical-tier` | auto | PASS | 2026-10-10T22:07:58Z | `c-20261010214232-4790` | yes |
| `cooling.fan-gate-trips` | auto | PASS | 2026-10-10T21:52:25Z | `c-20261010214232-4790` | yes |
| `cooling.flow-under-load` | live | PASS | 2026-10-10T23:20:48Z | `c-20261010224522-439d` | no |
| `cooling.fan-duty-readback` | auto | PASS | 2026-10-10T22:08:19Z | `c-20261010214232-4790` | yes |
| `cooling.fail-tier-stop` | operator | PASS | 2026-10-10T22:48:15Z | `c-20261010224522-439d` | no |
| `cooling.report-channel` | auto | PASS | 2026-10-10T22:08:36Z | `c-20261010214232-4790` | yes |
| `cooling.intake-one-fan` | auto | PASS | 2026-10-10T22:09:20Z | `c-20261010214232-4790` | yes |
| `laser.power-floor` | auto | PASS | 2026-10-10T22:09:27Z | `c-20261010214232-4790` | yes |
| `laser.emission-witness` | live, core | PASS | 2026-10-10T23:05:49Z | `c-20261010224522-439d` | no |
| `laser.m5-rapid-dark` | live | PASS | 2026-10-10T23:21:48Z | `c-20261010224522-439d` | no |
| `laser.disarm-in-hold` | live | PASS | 2026-10-10T23:23:20Z | `c-20261010224522-439d` | no |
| `laser.armed-kill` | live | PASS | 2026-10-10T23:24:03Z | `c-20261010224522-439d` | no |
| `laser.verdict-cut` | live | PASS | 2026-10-10T23:25:09Z | `c-20261010224522-439d` | no |
| `laser.arm-wait-lid` | operator | PASS | 2026-10-10T22:48:33Z | `c-20261010224522-439d` | no |
| `laser.pause-resume-lid-cancel` | live | PASS | 2026-10-10T23:26:26Z | `c-20261010224522-439d` | no |
| `laser.port-dark` | live | PASS | 2026-10-10T23:27:10Z | `c-20261010224522-439d` | no |
| `laser.recorder-dark` | auto | PASS | 2026-10-10T22:09:54Z | `c-20261010214232-4790` | yes |
| `camera.snapshot` | auto | PASS | 2026-10-10T22:10:16Z | `c-20261010214232-4790` | yes |
| `camera.sensor-profile` | auto | PASS | 2026-10-10T22:10:36Z | `c-20261010214232-4790` | yes |
| `camera.h264-stream` | auto | PASS | 2026-10-10T22:10:55Z | `c-20261010214232-4790` | yes |
| `camera.frame-health` | auto | PASS | 2026-10-10T22:11:19Z | `c-20261010214232-4790` | yes |
| `camera.lid-privacy` | operator | PASS | 2026-10-10T22:48:49Z | `c-20261010224522-439d` | no |
| `camera.key-read` | auto | PASS | 2026-10-10T22:11:26Z | `c-20261010214232-4790` | yes |
| `update.slots-and-signature` | auto | PASS | 2026-09-28T16:27:34Z | `c-20260928162331-3969` | yes |
| `update.product-gate` | auto | PASS | 2026-09-28T17:20:19Z | `c-20260928171100-3ff5` | yes |
| `update.release-check` | auto | PASS | 2026-10-10T22:11:33Z | `c-20261010214232-4790` | yes |
| `cloud.mode-switch` | operator | PASS | 2026-10-10T22:51:05Z | `c-20261010224522-439d` | no |
| `cloud.service-protocol` | operator | PASS | 2026-10-10T23:28:24Z | `c-20261010224522-439d` | no |
| `cloud.lid-interlock-abort` | live | PASS | 2026-10-10T23:30:40Z | `c-20261010224522-439d` | no |
| `cloud.lid-during-button-wait` | operator | PASS | 2026-10-10T22:52:29Z | `c-20261010224522-439d` | no |
| `cloud.dark-print` | operator | PASS | 2026-10-10T22:53:25Z | `c-20261010224522-439d` | no |
| `cloud.verdict-refuse` | operator | PASS | 2026-10-10T22:54:47Z | `c-20261010224522-439d` | no |
| `cloud.pause-resume` | live | PASS | 2026-10-10T23:34:32Z | `c-20261010224522-439d` | no |
| `cloud.oversize-stream` | live | PASS | 2026-10-10T23:38:02Z | `c-20261010224522-439d` | no |
| `cloud.paused-lid-cancel` | live | PASS | 2026-10-10T23:38:55Z | `c-20261010224522-439d` | no |
| `exthost.platform` | auto | PASS | 2026-09-28T18:05:37Z | `c-20260928180313-75fb` | yes |
| `exthost.service` | auto, takeover | PASS | 2026-10-10T22:12:20Z | `c-20261010214232-4790` | yes |
| `exthost.armed-freeze` | operator, takeover | PASS | 2026-10-10T22:56:02Z | `c-20261010224522-439d` | no |
| `exthost.hold-pause-tier` | auto, takeover | PASS | 2026-10-10T22:14:00Z | `c-20261010214232-4790` | yes |
| `exthost.events` | operator, takeover | PASS | 2026-10-10T22:56:23Z | `c-20261010224522-439d` | no |
| `exthost.motion-jog` | auto, takeover | PASS | 2026-10-10T22:56:43Z | `c-20261010224522-439d` | no |
| `exthost.ui-delivery` | auto | PASS | 2026-10-10T22:56:52Z | `c-20261010224522-439d` | no |
| `exthost.motion-job` | auto, takeover | PASS | 2026-09-28T18:11:06Z | `c-20260928180313-75fb` | yes |
| `exthost.package-routes` | auto | PASS | 2026-10-10T22:56:54Z | `c-20261010224522-439d` | no |
| `exthost.panel-install` | operator | PASS | 2026-10-10T22:56:58Z | `c-20261010224522-439d` | no |
| `exthost.core-range` | auto | PASS | 2026-09-28T18:11:13Z | `c-20260928180313-75fb` | yes |
| `exthost.page-call` | auto | PASS | 2026-10-10T22:57:14Z | `c-20261010224522-439d` | no |
| `exthost.operator-destinations` | auto | PASS | 2026-10-10T22:57:36Z | `c-20261010224522-439d` | no |
| `exthost.lifecycle` | auto | PASS | 2026-09-28T18:12:06Z | `c-20260928180313-75fb` | yes |
| `events.button-telemetry` | operator | PASS | 2026-10-10T22:57:48Z | `c-20261010224522-439d` | no |
| `exthost.catalog` | auto | PASS | 2026-10-10T22:57:50Z | `c-20261010224522-439d` | no |
| `exthost.mcode` | auto | PASS | 2026-09-28T18:12:39Z | `c-20260928180313-75fb` | yes |
| `exthost.wizard` | auto | PASS | 2026-10-10T22:58:18Z | `c-20261010224522-439d` | no |
| `update.job-locks` | auto | PASS | 2026-09-28T18:13:11Z | `c-20260928180313-75fb` | yes |
| `homing.cloud-offsets` | auto | PASS | 2026-10-10T22:59:11Z | `c-20261010224522-439d` | no |
| `setup.check-envelope` | auto | PASS | 2026-10-10T23:00:57Z | `c-20261010224522-439d` | no |
| `exthost.sender-keep-out` | auto | PASS | 2026-10-10T23:01:30Z | `c-20261010224522-439d` | no |
| `motion.tray` | auto | PASS | 2026-10-10T23:01:33Z | `c-20261010224522-439d` | no |
| `forgectrl.tls-records` | auto | PASS | 2026-10-10T23:01:37Z | `c-20261010224522-439d` | no |

Artifact sha256: `c8b5e4c24a59e92337e0ec9607343b64b0d8193919efca7cd0b3d18b3c4d31f7`
