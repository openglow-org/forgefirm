SUMMARY = "Firmware verification public keys"
DESCRIPTION = "Trust anchors for firmware archive verification: the \
ForgeFIRM release-signing public key (verifies release downloads and \
uploads) and the Glowforge factory keyring (verifies factory .fw \
archives for cloud restore). Public keys only."
# LICENSE covers this recipe, not the key material. The Glowforge factory
# keyring in files/gf/ is Glowforge, Inc.'s: bare Ed25519 public keys, in which
# no copyright subsists and over which OpenGlow claims nothing and grants
# nothing. files/gf/README records that and the public-key-only boundary.
LICENSE = "MIT"
LIC_FILES_CHKSUM = "file://${COMMON_LICENSE_DIR}/MIT;md5=0835ade698e0bcf8506ecda2f7b4f302"

SRC_URI = " \
    file://forgefirm-release.pub \
    file://gf \
"

S = "${WORKDIR}"

do_install() {
    install -d ${D}${sysconfdir}/forgefirm/keys/gf
    install -m 0644 ${WORKDIR}/forgefirm-release.pub \
        ${D}${sysconfdir}/forgefirm/keys/forgefirm-release.pub
    install -m 0644 ${WORKDIR}/gf/*.pub ${D}${sysconfdir}/forgefirm/keys/gf/
}

FILES:${PN} = "${sysconfdir}/forgefirm/keys"
