# OpenGlow/ForgeFIRM Firmware for Glowforge

> # ⚠️ BETA
>
> **ForgeFIRM is in beta.** Every release below 0.1.0 is a beta release.
> Expect problems, and expect frequent updates. Upgrade whenever a newer
> release is available, and report what you find on the
> [community forum](https://community.openglow.org).

Open-source firmware for Glowforge brand CNC lasers. ForgeFIRM replaces the
cloud-dependent factory software on the **stock control board** - no hardware
modification - and gives the machine a local controller, a local web control
panel, and a standard Grbl interface.

* [Latest Release](https://github.com/openglow-org/forgefirm/releases)
* [Installation Instructions](https://docs.forgefirm.org/install/)
* [Build Instructions](https://docs.forgefirm.org/developers/building/)
* [Connecting LightBurn](https://docs.forgefirm.org/usage/lightburn/)
* [How motion and the laser are driven](https://docs.forgefirm.org/technical/forgefirm/)
* [How cooling and airflow work](https://docs.forgefirm.org/technical/forgefirm/cooling-engine/)
* [The cameras and the video stream](https://docs.forgefirm.org/usage/cameras/)
* [How the laser safing works](https://docs.forgefirm.org/safety/)
* [How a release is tested before being accepted](https://docs.forgefirm.org/developers/acceptance/)
* [Community Support](https://community.openglow.org)

## What it does

**Two controller modes, selected in the web panel and switchable while the
machine is idle:**

* **GRBL mode**: [grblHAL](https://github.com/grblHAL) runs on the machine and
  speaks Grbl 1.1 over TCP port 23, so LightBurn, UGS, cncjs, etc... drive the
  laser directly. Motion runs on the board's own hardware step engine (SDMA +
  EPIT), fed live by a local planner. M3/M4 dynamic laser power, coolant-flow
  verification, over-temp holds, and an operator button press to arm the laser
  for each job.
* **Cloud mode**: The machine signs in to the Glowforge web service with its
  own identity, names its software as ForgeFIRM, and uses the service the way
  a stock machine does, so the phone and web apps work as they always did.
  Optional, and off by default. GRBL mode jogs and cuts without it; the one
  GRBL-mode function that still reaches the Glowforge service is
  camera-referenced homing (below), until limit-switch homing lands.
* A **web control panel**: Machine status and position, coolant
  and fan telemetry, safety-switch states, live camera stream, machine settings, hardware diagnostics, firmware updates, and boot-slot management.
* **Camera-referenced homing**: `$H` from any sender runs the factory-style
  camera homing cycle through the Glowforge service (a Glowforge account and a
  live service session are required for `$H`; everything else in GRBL mode
  runs without them).

## Hardware

The control board is common to Glowforge Basic, Plus, and Pro. The 5 MP
(OV5648) camera modules are fully supported and hardware-validated. The 8 MP
(OV8856) modules found in "HD" units have a complete capture path - the kernel
patches, device tree and sensor-aware capture profile they need are all in the
build, but it has never run on an 8 MP machine, so treat it as untested.

## What this costs

ForgeFIRM is free in both senses: free as in beer, free as in speech. All of
it is public and released under MIT and GPL licenses. Read it, build it, 
change it, use it, pass it on.

The work happens in public, including the parts that don't work yet, which
are written up with more candor than flatters anyone.
No paid tier. No license key, no subscription, no activation, no Pro edition,
no feature parked behind a paywall. Nothing is held back for a rainy day,
mostly because there's no plan for a rainy day.

If someone offers to sell you this firmware, the licenses allow it and
nobody's calling it theft. Just note that you'd be paying for something
that's given away. Get it from the source. Same price everywhere, and here 
you get to read what you're running.

## Safety

**These machines contain a CO₂ laser: it burns, blinds, and starts
fires.** Never defeat the lid switches or interlock. Never leave a
running job unattended. Keep a fire extinguisher within reach. Read [this](https://docs.forgefirm.org/safety/) before you cut
your first job, and read this [regulatory and legal](https://docs.forgefirm.org/install/#regulatory-and-legal) section before installing. [This](https://docs.forgefirm.org/technical/machine/safing-chain/) page describes the hardware safety chain and the software gates ForgeFIRM stacks on it.

**THIS IS EXPERIMENTAL SOFTWARE  
Use of this software
could seriously maim or kill you or others, and could void your warranty.  
Use it at your own risk.**

This project is not affiliated with or endorsed by Glowforge.
