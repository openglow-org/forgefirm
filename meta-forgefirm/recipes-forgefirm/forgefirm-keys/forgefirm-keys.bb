# SPDX-License-Identifier: MIT

SUMMARY = "Firmware and extension verification public keys"
DESCRIPTION = "Trust anchors for archive verification: the ForgeFIRM \
release-signing public key (verifies release downloads and uploads), the \
Glowforge factory keyring (verifies factory .fw archives for cloud \
restore), and the OpenGlow extension-signing public key (the official \
tier of extension packages). Public keys only."
# LICENSE covers this recipe, not the key material. The Glowforge factory
# keyring in files/gf/ is Glowforge, Inc.'s: bare Ed25519 public keys, in which
# no copyright subsists and over which OpenGlow claims nothing and grants
# nothing. files/gf/README records that and the public-key-only boundary.
LICENSE = "MIT"
LIC_FILES_CHKSUM = "file://${COMMON_LICENSE_DIR}/MIT;md5=0835ade698e0bcf8506ecda2f7b4f302"

# The extension key is a different key from the release key on purpose: it
# signs more often, and its loss must not sign firmware. forgeext refuses an
# extension archive whose only valid signature is one of the two above.
SRC_URI = " \
    file://forgefirm-release.pub \
    file://gf \
    file://ext/forgefirm-ext.pub \
"

S = "${WORKDIR}"

do_install() {
    install -d ${D}${sysconfdir}/forgefirm/keys/gf
    install -m 0644 ${WORKDIR}/forgefirm-release.pub \
        ${D}${sysconfdir}/forgefirm/keys/forgefirm-release.pub
    install -m 0644 ${WORKDIR}/gf/*.pub ${D}${sysconfdir}/forgefirm/keys/gf/
    install -d ${D}${sysconfdir}/forgefirm/keys/ext
    install -m 0644 ${WORKDIR}/ext/forgefirm-ext.pub \
        ${D}${sysconfdir}/forgefirm/keys/ext/forgefirm-ext.pub
}

FILES:${PN} = "${sysconfdir}/forgefirm/keys"
