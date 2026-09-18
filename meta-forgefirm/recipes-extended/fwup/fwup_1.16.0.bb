# SPDX-License-Identifier: MIT

SUMMARY = "Configurable embedded Linux firmware update creator and runner"
DESCRIPTION = "Applies and creates signed .fw firmware archives. ForgeFIRM \
uses the factory's own update format: fwup applies ForgeFIRM upgrades and \
Glowforge factory-restore archives to the inactive rootfs slot."
HOMEPAGE = "https://github.com/fwup-home/fwup"
LICENSE = "Apache-2.0"
LIC_FILES_CHKSUM = "file://LICENSE;md5=3b83ef96387f14655fc854ddc3c6bd57"

DEPENDS = "libconfuse libarchive libsodium"

SRC_URI = "https://github.com/fwup-home/fwup/releases/download/v${PV}/fwup-${PV}.tar.gz"
SRC_URI[sha256sum] = "a07b79268247ecee134a916ab928914be2a4ecbac0bc5e5f19212ec36ecb5c21"

inherit autotools pkgconfig bash-completion
