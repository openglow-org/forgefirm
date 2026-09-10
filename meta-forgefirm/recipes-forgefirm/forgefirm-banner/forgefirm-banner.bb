SUMMARY = "ForgeFIRM console banner: the control panel addresses in /etc/issue"
DESCRIPTION = "Keeps an address block in the serial-console login banner \
(/etc/issue): one control panel URL per global address of wlan0 and eth0. \
Refreshed at boot and on every DHCP lease event."
LICENSE = "MIT"
LIC_FILES_CHKSUM = "file://${COMMON_LICENSE_DIR}/MIT;md5=0835ade698e0bcf8506ecda2f7b4f302"

SRC_URI = " \
    file://forgefirm-banner \
    file://forgefirm-banner.init \
    file://60forgefirm-banner \
"

S = "${WORKDIR}"

inherit update-rc.d

INITSCRIPT_NAME = "forgefirm-banner"
# 99: after networking (S01); an address the lease brings later arrives
# through the udhcpc hook.
INITSCRIPT_PARAMS = "start 99 2 3 4 5 ."

do_install() {
    install -Dm 0755 ${WORKDIR}/forgefirm-banner ${D}${sbindir}/forgefirm-banner
    install -Dm 0755 ${WORKDIR}/forgefirm-banner.init ${D}${sysconfdir}/init.d/forgefirm-banner
    # busybox udhcpc runs /etc/udhcpc.d/* (run-parts) on every lease event
    install -Dm 0755 ${WORKDIR}/60forgefirm-banner ${D}${sysconfdir}/udhcpc.d/60forgefirm-banner
}

FILES:${PN} += "${sysconfdir}/udhcpc.d"
