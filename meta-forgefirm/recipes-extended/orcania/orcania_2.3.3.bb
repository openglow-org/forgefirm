# SPDX-License-Identifier: MIT

DESCRIPTION = "Potluck with different functions for C programs (babelouest base library)"
HOMEPAGE = "https://github.com/babelouest/orcania"

LICENSE = "LGPL-2.1-or-later"
LIC_FILES_CHKSUM = "file://LICENSE;md5=fc178bcd425090939a8b634d1d6a9594"

# Pinned to the v2.3.3 release tag: the version ulfius v2.7.15 builds against.
SRC_URI = "git://github.com/babelouest/orcania;protocol=https;branch=master"
SRCREV = "ffc8b55d09a3488f4f6be38034b33bc64bf8b0ce"

S = "${WORKDIR}/git"

inherit cmake pkgconfig
