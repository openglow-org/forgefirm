DESCRIPTION = "grblHAL motion controller for the Glowforge factory board"
HOMEPAGE = "https://github.com/openglow-org/grblHAL-glowforge"

LICENSE = "GPL-3.0-or-later"
LIC_FILES_CHKSUM = "file://COPYING;md5=ea8d0b3d9e732587396d0345870910f0"

# gitsm: the grblHAL core rides as a submodule (openglow-org/grblHAL-core, branch
# forgefirm = upstream master + the step_us_min buffer fix pending
# upstream).
SRC_URI = "gitsm://github.com/openglow-org/grblHAL-glowforge.git;protocol=https;branch=main"
# SRCREV and PV live in the pin file (forgefirm-image-manifest.bbclass).
require grblhal-glowforge-pin.inc

SRC_URI += "file://grblhal.init"

S = "${WORKDIR}/git"

inherit cmake update-rc.d forgefirm-manifest

INITSCRIPT_NAME = "grblhal"
# stop 80 < forgectrl's 90: at runlevel 0/6 the controller goes down
# BEFORE the daemon that carries the cooling engine, fire gates, and
# broker - never the other way around.
INITSCRIPT_PARAMS = "start 92 2 3 4 5 . stop 80 0 1 6 ."

do_install:append() {
    install -d ${D}${sysconfdir}/init.d
    install -m 0755 ${WORKDIR}/grblhal.init ${D}${sysconfdir}/init.d/grblhal
}

# The controller streams into /dev/glowforge (glowforge.ko) at runtime.
RRECOMMENDS:${PN} = "kernel-module-glowforge"
