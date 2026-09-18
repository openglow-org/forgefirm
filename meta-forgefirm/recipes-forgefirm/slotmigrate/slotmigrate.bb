# SPDX-License-Identifier: MIT

SUMMARY = "Boot-time migration from the legacy ForgeFIRM disk layout"
DESCRIPTION = "Reclaims the legacy ForgeFIRM eMMC partition (p4) and grows \
/data back to the factory footprint. Runs before mountall; state-derived \
and idempotent; a factory-layout disk is a no-op. Acts only when booted \
from an eMMC rootfs slot."
LICENSE = "MIT"
LIC_FILES_CHKSUM = "file://${COMMON_LICENSE_DIR}/MIT;md5=0835ade698e0bcf8506ecda2f7b4f302"

SRC_URI = "file://slotmigrate"

S = "${WORKDIR}"

inherit update-rc.d

INITSCRIPT_NAME = "slotmigrate"
INITSCRIPT_PARAMS = "start 2 S ."

RDEPENDS:${PN} = " \
    util-linux-sfdisk \
    util-linux-partx \
    e2fsprogs-e2fsck \
    e2fsprogs-tune2fs \
    e2fsprogs-resize2fs \
"

do_install() {
    install -Dm 0755 ${WORKDIR}/slotmigrate ${D}${sysconfdir}/init.d/slotmigrate
}
