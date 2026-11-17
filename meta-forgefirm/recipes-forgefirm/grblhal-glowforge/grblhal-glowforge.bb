# Copyright 2026 514 LLC d/b/a OpenGlow
# Written by Scott Wiederhold
# SPDX-License-Identifier: MIT

DESCRIPTION = "grblHAL motion controller for the Glowforge factory board"
HOMEPAGE = "https://github.com/openglow-org/grblHAL-glowforge"

LICENSE = "GPL-3.0-or-later"
LIC_FILES_CHKSUM = "file://COPYING;md5=ea8d0b3d9e732587396d0345870910f0"

# gitsm: the grblHAL core rides as a submodule (openglow-org/grblHAL-core,
# branch forgefirm: upstream master plus the changes the fork's
# FORK-CHANGES.md lists).
SRC_URI = "gitsm://github.com/openglow-org/grblHAL-glowforge.git;protocol=https;branch=next"
# SRCREV and PV live in the pin file (forgefirm-image-manifest.bbclass).
require grblhal-glowforge-pin.inc

SRC_URI += "file://grblhal.init"

S = "${WORKDIR}/git"

inherit cmake update-rc.d forgefirm-manifest

INITSCRIPT_NAME = "grblhal"
# Stop links only. forgectrl's supervisor starts the selected controller
# at boot, so no runlevel starts this script. stop 80 < forgectrl's 90: at
# runlevel 0/6 the controller goes down BEFORE the daemon that carries the
# cooling engine, fire gates, and broker - never the other way around.
INITSCRIPT_PARAMS = "stop 80 0 1 6 ."

do_install:append() {
    install -d ${D}${sysconfdir}/init.d
    install -m 0755 ${WORKDIR}/grblhal.init ${D}${sysconfdir}/init.d/grblhal
}

# The controller streams into /dev/glowforge (glowforge.ko) at runtime.
RRECOMMENDS:${PN} = "kernel-module-glowforge"
