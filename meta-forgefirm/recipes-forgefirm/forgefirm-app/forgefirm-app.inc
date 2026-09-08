# Shared fetch for the ForgeFIRM web-service apps (gfhome, python3-ffmachine,
# gfcloud). Their sources live in the forgefirm-app/ directory of the
# python3-gfhardware repository.
HOMEPAGE = "https://github.com/openglow-org/python3-gfhardware"

# These packages ship only the Python apps under forgefirm-app/, all MIT. The
# repository's LGPL component (the Bayer decoder compiled into
# gfhardware._cam) is not part of them; python3-gfhardware declares it.
LICENSE = "MIT"
LIC_FILES_CHKSUM = "file://LICENSE;md5=7029670f29302d9352647c6c89614bc2"

SRC_URI = "git://github.com/openglow-org/python3-gfhardware.git;protocol=https;branch=master"
# SRCREV and PV live in the pin file (forgefirm-image-manifest.bbclass).
require forgefirm-app-pin.inc

S = "${WORKDIR}/git"

# One image-manifest component for the three app recipes (same repository,
# same pinned revision): the entries merge in the image manifest.
inherit forgefirm-manifest
FORGEFIRM_MANIFEST_NAME = "forgefirm-app"
