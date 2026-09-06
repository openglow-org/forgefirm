DESCRIPTION = "System Control Daemon for ForgeFIRM powered Glowforge"
HOMEPAGE = "https://github.com/openglow-org/forgectrl"

LICENSE = "MIT"
LIC_FILES_CHKSUM = "file://LICENSE;md5=19ed4e3e8c28a4311c16b0b2b91357ec"

PE = "1"

SRC_URI = "git://github.com/openglow-org/forgectrl.git;protocol=https;branch=main"
# SRCREV and PV live in the pin file (forgefirm-image-manifest.bbclass).
require forgectrl-pin.inc

S = "${WORKDIR}/git"

inherit cmake update-rc.d forgefirm-manifest

# jansson (settings, API bodies) and gnutls (the panel certificate) are
# linked directly, not only through ulfius.
DEPENDS += "ulfius jpeg jansson gnutls libxcrypt"
# media-ctl / v4l2-ctl configure the imx-media pipeline at runtime;
# the update manager drives ffboot + fwup and verifies against the
# shipped keyring; release checks and downloads use curl; the WiFi
# regulatory region (wifi_country) is applied with iw reg set.
RDEPENDS:${PN} = "v4l-utils ffboot fwup forgefirm-keys curl iw"

INITSCRIPT_NAME = "forgectrl"
INITSCRIPT_PARAMS = "defaults 90"

do_install:append() {
    install -d ${D}${sysconfdir}/init.d
    install -m 0755 ${S}/init/forgectrl.init ${D}${sysconfdir}/init.d/forgectrl
}
