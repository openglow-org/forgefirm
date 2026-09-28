# Copyright 2026 514 LLC d/b/a OpenGlow
# Written by Scott Wiederhold
# SPDX-License-Identifier: MIT

# Kernel TLS. GnuTLS runs the handshake, then gives the connection's keys
# to the socket, and the kernel seals and opens every record after that:
# ChaCha20-Poly1305 in NEON, AES-GCM with its AES on the CAAM crypto
# engine and its GHASH in NEON (the kernel options are in the BSP's
# kernel fragment). --enable-ktls builds the support in; /etc/gnutls/config
# turns it on, because the library leaves it off without that line. A
# connection on a cipher that kernel TLS does not take (AES-256-CCM, the
# CBC suites) stays in GnuTLS. forgectrl is the only program on the image
# that links GnuTLS (curl and Python use OpenSSL). The other build options
# are in conf/distro/forgefirm.conf.
#
# The backport: 3.8.4 hands the kernel the record sequence number where the
# connection's IV belongs for ChaCha20-Poly1305 over TLS 1.2, so every such
# connection fails the kernel's first decryption. Fixed upstream after 3.8.4.
FILESEXTRAPATHS:prepend := "${THISDIR}/files:"

SRC_URI:append:class-target = " file://config \
                                file://0001-ktls-fix-ChaCha20-Poly1305-IV-passing-for-TLS-1.2.patch"

EXTRA_OECONF:append:class-target = " --enable-ktls"

do_install:append:class-target() {
    install -d ${D}${sysconfdir}/gnutls
    install -m 0644 ${WORKDIR}/config ${D}${sysconfdir}/gnutls/config
}

FILES:${PN}:append:class-target = " ${sysconfdir}/gnutls/config"
