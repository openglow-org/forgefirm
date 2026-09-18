# SPDX-License-Identifier: MIT

SUMMARY = "Boot-slot selection and inventory for Glowforge factory hardware"
DESCRIPTION = "Inventories the bootable partitions (ffboot -l) and switches \
the boot target by rewriting the saved U-Boot environment with read-back \
verification. Ships the fw_env.config for the factory eMMC environment \
layout."
LICENSE = "MIT"
LIC_FILES_CHKSUM = "file://${COMMON_LICENSE_DIR}/MIT;md5=0835ade698e0bcf8506ecda2f7b4f302"

# ffboot rewrites the boot environment on every install and slot switch,
# so it is a fingerprinted component like the daemons: the manifest entry
# records the two files, and the acceptance tests that cover ffboot are
# invalidated when either moves (the installer copies the tool out of the
# rootfs it just wrote).
inherit forgefirm-manifest
FORGEFIRM_MANIFEST_SRC = "${THISDIR}/files"

SRC_URI = " \
    file://ffboot \
    file://fw_env.config \
"

S = "${WORKDIR}"

RDEPENDS:${PN} = "libubootenv-bin"

do_install() {
    install -Dm 0755 ${WORKDIR}/ffboot ${D}${sbindir}/ffboot
    install -Dm 0644 ${WORKDIR}/fw_env.config ${D}${sysconfdir}/fw_env.config
}
