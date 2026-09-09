SUMMARY = "ForgeFIRM boot state on /data: the timestamp and the random seed"
DESCRIPTION = "The two files the poky init scripts keep across boots, \
pointed at /data/forgefirm because the rootfs is read-only: the boot \
timestamp (bootmisc.sh restores it, save-rtc.sh writes it at shutdown; \
the board has no battery-backed RTC) and the random seed (the urandom \
script carries it from shutdown to the next boot)."
LICENSE = "MIT"
LIC_FILES_CHKSUM = "file://${COMMON_LICENSE_DIR}/MIT;md5=0835ade698e0bcf8506ecda2f7b4f302"

SRC_URI = " \
    file://timestamp \
    file://urandom \
"

S = "${WORKDIR}"

# The scripts that read these defaults.
RDEPENDS:${PN} = "initscripts"

do_install() {
    install -Dm 0644 ${WORKDIR}/timestamp ${D}${sysconfdir}/default/timestamp
    install -Dm 0644 ${WORKDIR}/urandom ${D}${sysconfdir}/default/urandom
}
