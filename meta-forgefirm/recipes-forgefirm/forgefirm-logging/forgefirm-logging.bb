# SPDX-License-Identifier: MIT

SUMMARY = "ForgeFIRM logging tree: rsyslog rules at boot, rotation"
DESCRIPTION = "Boot-time glue for the ForgeFIRM logging tree \
(/data/log/forgefirm/<logger>/): renders the per-logger rsyslog rules \
from the machine settings before rsyslog starts (forgectrl \
--render-syslog), moves pre-syslog log files out of the way once, and \
drives size-capped logrotate at boot and hourly (a full /data breaks \
settings, update staging, and the controllers' own writes)."
LICENSE = "MIT"
LIC_FILES_CHKSUM = "file://${COMMON_LICENSE_DIR}/MIT;md5=0835ade698e0bcf8506ecda2f7b4f302"

SRC_URI = " \
    file://forgefirm.logrotate \
    file://forgefirm-logging.init \
"

S = "${WORKDIR}"

inherit update-rc.d

INITSCRIPT_NAME = "forgefirm-logging"
# 19: before rsyslog's own script (syslog, S20) so the rendered rules
# exist when it reads its config.
INITSCRIPT_PARAMS = "start 19 2 3 4 5 ."

RDEPENDS:${PN} = "logrotate rsyslog forgectrl"

do_install() {
    install -Dm 0644 ${WORKDIR}/forgefirm.logrotate ${D}${sysconfdir}/logrotate.d/forgefirm
    install -Dm 0755 ${WORKDIR}/forgefirm-logging.init ${D}${sysconfdir}/init.d/forgefirm-logging
}
