# SPDX-License-Identifier: MIT

DESCRIPTION = "Extension host for ForgeFIRM powered Glowforge"
HOMEPAGE = "https://github.com/openglow-org/forgeext"

LICENSE = "MIT"
LIC_FILES_CHKSUM = "file://LICENSE;md5=785c97b59b518e7ad943cd9e8b15fc97"

SRC_URI = "git://github.com/openglow-org/forgeext.git;protocol=https;branch=main"
# SRCREV and PV live in the pin file (forgefirm-image-manifest.bbclass).
require forgeext-pin.inc

S = "${WORKDIR}/git"

inherit cmake pkgconfig forgefirm-manifest

# jansson (the manifest, state.json, every answer), libarchive (the .ffx and
# its payload, read streaming), libsodium (the archive's signature and the
# blake2b-256 of the payload and of every installed file).
DEPENDS += "jansson libarchive libsodium"
# fwup reads an archive's metadata and task list the way the firmware
# paths would; the keyring holds the keys that sign firmware, which an
# extension is never signed with; forgefirm-sandbox is the account pool,
# the cgroup tree, and the deny rules a package's service runs inside.
RDEPENDS:${PN} = "fwup forgefirm-keys forgefirm-sandbox"
