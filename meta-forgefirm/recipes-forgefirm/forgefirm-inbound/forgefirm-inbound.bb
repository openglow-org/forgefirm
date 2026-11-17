# Copyright 2026 514 LLC d/b/a OpenGlow
# Written by Scott Wiederhold
# SPDX-License-Identifier: MIT

SUMMARY = "ForgeFIRM inbound filter: new IPv6 TCP connections from the link alone"
DESCRIPTION = "The nftables table that drops the first segment of a new TCP \
connection over IPv6 from any source that is neither link-local nor in an \
on-link prefix, and the watcher that keeps its set of on-link prefixes \
current from the kernel's routes and the machine's own addresses. The rules \
load from rcS, before the \
network starts. A table of its own: it never touches the extension \
sandbox's."
LICENSE = "MIT"
LIC_FILES_CHKSUM = "file://${COMMON_LICENSE_DIR}/MIT;md5=0835ade698e0bcf8506ecda2f7b4f302"

SRC_URI = " \
    file://forgefirm-inbound \
    file://inbound.nft \
"

S = "${WORKDIR}"

inherit update-rc.d

INITSCRIPT_NAME = "forgefirm-inbound"
# 31 in rcS: /run is mounted (mountall, S03), and the network starts in rc5
# (S01), so no interface carries an address before the rules are in the
# kernel. The watcher started here keeps running through rc5.
INITSCRIPT_PARAMS = "start 31 S ."

do_install() {
    install -Dm 0755 ${WORKDIR}/forgefirm-inbound ${D}${sysconfdir}/init.d/forgefirm-inbound
    install -Dm 0644 ${WORKDIR}/inbound.nft ${D}${sysconfdir}/forgefirm/inbound.nft
}

RDEPENDS:${PN} = "nftables"
