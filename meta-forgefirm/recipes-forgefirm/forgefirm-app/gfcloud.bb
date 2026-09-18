# SPDX-License-Identifier: MIT

DESCRIPTION = "Full Glowforge web-service controller daemon for ForgeFIRM (cloud mode)"

require forgefirm-app.inc

inherit update-rc.d

INITSCRIPT_NAME = "gfcloud"
# stop 80 < forgectrl's 90: at runlevel 0/6 the controller goes down
# BEFORE the daemon that carries the cooling engine, fire gates, and
# broker - never the other way around.
INITSCRIPT_PARAMS = "start 92 2 3 4 5 . stop 80 0 1 6 ."

do_install() {
    install -Dm 0755 ${S}/forgefirm-app/gfcloud.py ${D}${sbindir}/gfcloud.py
    install -d ${D}${sysconfdir}/init.d
    install -m 0755 ${S}/forgefirm-app/gfcloud.init ${D}${sysconfdir}/init.d/gfcloud
}

# Shares the Glowforge web-service config with gfhome (SERVICE section,
# camera captures via forgectrl); pulls the shared machine glue.
RDEPENDS:${PN} += "python3-core python3-ffmachine python3-gfhardware python3-gfutilities"
