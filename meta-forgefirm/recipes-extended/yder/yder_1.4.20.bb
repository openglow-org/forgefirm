# SPDX-License-Identifier: MIT

DESCRIPTION = "Logging library for C applications (babelouest)"
HOMEPAGE = "https://github.com/babelouest/yder"

LICENSE = "LGPL-2.1-or-later"
LIC_FILES_CHKSUM = "file://LICENSE;md5=40d2542b8c43a3ec2b7f5da31a697b88"

# Pinned to the v1.4.20 release tag: the version ulfius v2.7.15 builds against.
SRC_URI = "git://github.com/babelouest/yder;protocol=https;branch=master"
SRCREV = "dffe82c0483bb95d0d518ba1e36c568e63a24628"

S = "${WORKDIR}/git"

inherit cmake pkgconfig

DEPENDS = "orcania"

# No systemd journald on the forgefirm image.
EXTRA_OECMAKE += "-DWITH_JOURNALD=off"
