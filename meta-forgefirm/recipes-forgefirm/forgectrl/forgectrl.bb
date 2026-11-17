# Copyright 2020 514 LLC d/b/a OpenGlow
# Written by Scott Wiederhold
# SPDX-License-Identifier: MIT

DESCRIPTION = "System Control Daemon for ForgeFIRM powered Glowforge"
HOMEPAGE = "https://github.com/openglow-org/forgectrl"

LICENSE = "MIT"
LIC_FILES_CHKSUM = "file://LICENSE;md5=785c97b59b518e7ad943cd9e8b15fc97"

PE = "1"

SRC_URI = "git://github.com/openglow-org/forgectrl.git;protocol=https;branch=next"
# SRCREV and PV live in the pin file (forgefirm-image-manifest.bbclass).
require forgectrl-pin.inc

S = "${WORKDIR}/git"

inherit cmake update-rc.d forgefirm-manifest

# jansson (settings, API bodies) and gnutls (the panel certificate) are
# linked directly, not only through ulfius.
DEPENDS += "ulfius jpeg jansson gnutls libxcrypt"
# media-ctl (a package of its own) and v4l2-ctl (v4l-utils) configure the
# imx-media pipeline at runtime; the update manager drives ffboot + fwup
# and verifies against the shipped keyring; release checks and downloads
# use curl; the WiFi regulatory region (wifi_country) is applied with iw
# reg set.
RDEPENDS:${PN} = "media-ctl v4l-utils ffboot fwup forgefirm-keys curl iw"

INITSCRIPT_NAME = "forgectrl"
INITSCRIPT_PARAMS = "defaults 90"

do_install:append() {
    install -d ${D}${sysconfdir}/init.d
    install -m 0755 ${S}/init/forgectrl.init ${D}${sysconfdir}/init.d/forgectrl
}
