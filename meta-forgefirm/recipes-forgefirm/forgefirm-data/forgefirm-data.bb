# SPDX-License-Identifier: MIT

SUMMARY = "ForgeFIRM data: the SD card's settings and logs, a requested reset"
DESCRIPTION = "Boot-time step after mountall. Booted from an SD card that \
carries a data partition (the dev image's SD card image), it shows the \
card's forgefirm and log folders at /data/forgefirm and \
/data/log/forgefirm, leaves the rest of /data the eMMC's, and copies the \
machine's identity over at the card's first boot. On any boot it carries \
out a settings reset that forgectrl requested, before anything reads \
/data/forgefirm."
LICENSE = "MIT"
LIC_FILES_CHKSUM = "file://${COMMON_LICENSE_DIR}/MIT;md5=0835ade698e0bcf8506ecda2f7b4f302"

SRC_URI = "file://forgefirm-data"

S = "${WORKDIR}"

inherit update-rc.d

INITSCRIPT_NAME = "forgefirm-data"
# 04 in rcS, ahead of udev by name: after mountall (S03) has mounted /data
# and /run, and after slotmigrate (S02), which needs /data unmounted;
# before the first readers of /data/forgefirm, bootmisc.sh (S36) and
# urandom (S38), which read it through /etc/default/timestamp and
# /etc/default/urandom (forgefirm-persist). rootfs-lint.sh's data-order
# rule holds this order on the built rootfs.
INITSCRIPT_PARAMS = "start 04 S ."

do_install() {
    install -Dm 0755 ${WORKDIR}/forgefirm-data ${D}${sysconfdir}/init.d/forgefirm-data
}
