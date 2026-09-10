SUMMARY = "ForgeFIRM hostname: forgefirm-<xxxx> from the MAC address"
DESCRIPTION = "Names the machine forgefirm-<xxxx>, where xxxx is the last \
four hex digits of the wlan0 MAC address (eth0 on a machine with no WiFi). \
The name is the same at every boot, two machines on one network answer to \
different names, and the name carries no serial number. The DHCP client \
sends it as the hostname option, so a network with dynamic DNS resolves it."
LICENSE = "MIT"
LIC_FILES_CHKSUM = "file://${COMMON_LICENSE_DIR}/MIT;md5=0835ade698e0bcf8506ecda2f7b4f302"

SRC_URI = " \
    file://forgefirm-hostname \
    file://forgefirm-hostname.init \
"

S = "${WORKDIR}"

inherit update-rc.d

INITSCRIPT_NAME = "forgefirm-hostname"
# 38 in rcS: after udev (S04) has probed the network drivers, before
# poky's hostname.sh (S39) reads /etc/hostname and before the network
# starts (rc5 S01).
INITSCRIPT_PARAMS = "start 38 S ."

do_install() {
    install -Dm 0755 ${WORKDIR}/forgefirm-hostname ${D}${sbindir}/forgefirm-hostname
    install -Dm 0755 ${WORKDIR}/forgefirm-hostname.init ${D}${sysconfdir}/init.d/forgefirm-hostname
}
