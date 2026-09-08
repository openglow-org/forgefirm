# OpenGlow / ForgeFIRM firmware for Glowforge

> ### BETA
>
> **ForgeFIRM is in beta.** Every release below 0.1.0 is a beta release.
> Expect problems, and expect frequent updates. Upgrade whenever a newer
> release is available, and report what you find on the
> [community forum](https://community.openglow.org).

Open firmware for Glowforge brand CNC lasers. ForgeFIRM replaces the
cloud-dependent factory software on the **stock control board**, with no
hardware modification, and gives the machine a local controller, a local web
control panel, and a standard Grbl interface. The factory cloud experience
stays available as an option.

This repository is the **base of the build and of the release**: the
`meta-forgefirm` Yocto layer, the kas configuration, the image recipes, the
install and release scripts, the acceptance tool (`forgetest/`), the bench
tools (`scripts/bench/`), the bench actuator firmware (`fixture/`), and the
release artifacts (`releases/`).

## Start here

**<https://docs.forgefirm.org/>** is the documentation, and the source of
truth for every fact about the machine and the firmware.

| | |
|---|---|
| Read this first | [Safety](https://docs.forgefirm.org/safety/) |
| Put it on a machine | [Installation](https://docs.forgefirm.org/install/) |
| Use it | [Usage](https://docs.forgefirm.org/usage/), [LightBurn](https://docs.forgefirm.org/usage/lightburn/) |
| How the machine works | [Technical](https://docs.forgefirm.org/technical/machine/) |
| How ForgeFIRM works with it | [ForgeFIRM internals](https://docs.forgefirm.org/technical/forgefirm/) |
| Build, test, release | [Developers](https://docs.forgefirm.org/developers/) |
| Downloads | [Releases](https://github.com/openglow-org/forgefirm/releases) |
| Questions | [Community forum](https://community.openglow.org) |

## What it does

Two controller modes, selected in the web panel and switchable while the
machine is idle. **GRBL mode** runs grblHAL on the machine, speaking Grbl 1.1
over TCP port 23, so LightBurn, UGS and cncjs drive the laser directly; motion
runs on the board's own hardware step engine, fed live by a local planner.
**Cloud mode** signs in to the Glowforge web service as itself, so the phone
and web apps work as they always did; it is optional and off by default.
Around both sits a local web control panel: status and position, coolant and
fan telemetry, safety-switch states, a live camera stream, settings, hardware
diagnostics, firmware updates and boot-slot management.

The control board is common to the Basic, the Plus and the Pro, and one image
covers every model. The 5 MP camera is hardware validated; the 8 MP camera of
an "HD" machine has a complete path that has never run on one
([Cameras](https://docs.forgefirm.org/technical/machine/cameras/)).

## Build

```sh
kas build kas/forgefirm-glowforge.yml
```

[Build](https://docs.forgefirm.org/developers/building/) covers the host
setup, the two images, the source variant and the debug kernel.
[Release flow](https://docs.forgefirm.org/developers/release-flow/) covers the
pins, the push order and the signing pipeline.

## Test

```sh
cd forgetest && python3 -m unittest discover -s tests -v
```

The acceptance catalog that gates a release, and the bench tools, are on
[Acceptance](https://docs.forgefirm.org/developers/acceptance/) and
[The bench](https://docs.forgefirm.org/developers/bench/).

## Contributing

[AGENTS.md](AGENTS.md) carries the rules for this repository and for the
project: safety ordering, proof before done, the push order, and the writing
rules. They apply to human contributors too, and
[Contribute](https://docs.forgefirm.org/developers/contributing/) is the same
set on the site.

## What this costs

Nothing. ForgeFIRM is free in both senses, under MIT and GPL licenses. There
is no paid tier, no license key, no subscription and no Pro edition. If
someone offers to sell it to you, the licenses allow it, but what you take
home is their build rather than this one: get it from the source.

## Safety

**These machines contain a CO2 laser: it burns, blinds, and starts fires.**
Never defeat the lid switches or the interlock. Never leave a running job
unattended. Keep a fire extinguisher within reach. Read
[Safety](https://docs.forgefirm.org/safety/) before you cut your first job,
and [Regulatory and legal](https://docs.forgefirm.org/install/#regulatory-and-legal)
before you install.

**This is experimental software. Use of it could seriously maim or kill you or
others, and it may void your warranty. Use it at your own risk.**

This project is not affiliated with or endorsed by Glowforge.
