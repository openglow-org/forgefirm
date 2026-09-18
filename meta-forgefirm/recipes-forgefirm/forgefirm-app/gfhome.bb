# SPDX-License-Identifier: MIT

DESCRIPTION = "One-shot Glowforge web-service homing for ForgeFIRM"

require forgefirm-app.inc

do_install() {
    install -Dm 0755 ${S}/forgefirm-app/gfhome.py ${D}${sbindir}/gfhome.py
    install -Dm 0600 ${S}/forgefirm-app/gfhome.conf.sample ${D}${sysconfdir}/gfhome.conf.sample
}

RDEPENDS:${PN} += "python3-core python3-ffmachine python3-gfhardware python3-gfutilities"
