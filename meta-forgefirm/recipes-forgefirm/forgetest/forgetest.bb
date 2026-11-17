# Copyright 2026 514 LLC d/b/a OpenGlow
# Written by Scott Wiederhold
# SPDX-License-Identifier: MIT

SUMMARY = "ForgeFIRM release acceptance tool (dev image)"
DESCRIPTION = "Runs the release acceptance catalog against the machine from a \
self-contained web page (HTTP :8090), keeps the append-only result log under \
/data/forgetest, exports the release artifact the release gate reads, and \
carries the bench diagnostics page. Installed only on the dev image."
HOMEPAGE = "https://github.com/openglow-org/forgefirm"
LICENSE = "MIT"
LIC_FILES_CHKSUM = "file://forgetest/LICENSE;md5=6657cce322ff8bed163c02ceffb4c60c"

# The tool's canonical home is forgetest/ in this repo; the bench scripts
# it drives live in scripts/bench/. Both are packaged from the same tree
# that builds the images, so the catalog and the gate agree by construction.
FILESEXTRAPATHS:prepend := "${THISDIR}/../../../:"

# The package directory and the init script, not the tool's whole tree: the
# host unit tests and their caches would otherwise enter the fetch checksum
# and rebuild the dev image after a workstation test run.
SRC_URI = " \
    file://forgetest/LICENSE \
    file://forgetest/forgetest/ \
    file://forgetest/forgetest.init \
    file://scripts/bench/ \
"

do_unpack:append() {
    import subprocess
    subprocess.call(['find', d.getVar('WORKDIR') + '/forgetest', d.getVar('WORKDIR') + '/scripts',
                     '-name', '__pycache__', '-type', 'd', '-prune', '-exec', 'rm', '-rf', '{}', '+'])
}

S = "${WORKDIR}"

inherit python3-dir update-rc.d forgefirm-manifest

# Non-git sources: fingerprint the package directory (dev-only component;
# it identifies the campaign, never a test fingerprint). The sources come
# from the forgefirm checkout, outside the layer: the entry is marked dirty
# when they differ from its commit, so a dev image built from uncommitted
# acceptance code or bench tools records no result that can be inherited.
FORGEFIRM_MANIFEST_SRC = "${WORKDIR}/forgetest/forgetest"
FORGEFIRM_MANIFEST_DIRTY_TREES := "${THISDIR}/../../../forgetest/forgetest \
    ${THISDIR}/../../../forgetest/forgetest.init ${THISDIR}/../../../scripts/bench"

# The standard-library packages the tool imports (the page server, the
# catalog, the bench scripts), not the python3 meta-package: the dev image
# must carry no module the release image lacks but the tool's own, or a
# missing module in the release rootfs would pass the campaign unseen.
# Three of them the dev image alone carries: mmap, which reads the SoC's
# registers through /dev/mem (image.health and the bench pad readers),
# ctypes, the extension sandbox probe's, and statistics.
# scripts/release.sh refuses a release whose dev image carries a
# standard-library module this list does not name and the release image
# lacks.
RDEPENDS:${PN} = " \
    python3-core \
    python3-compression \
    python3-crypt \
    python3-ctypes \
    python3-fcntl \
    python3-io \
    python3-json \
    python3-logging \
    python3-math \
    python3-mmap \
    python3-netclient \
    python3-netserver \
    python3-shell \
    python3-statistics \
    forgectrl \
"

INITSCRIPT_NAME = "forgetest"
INITSCRIPT_PARAMS = "start 95 2 3 4 5 . stop 70 0 1 6 ."

do_install() {
    # the package (sources only; python compiles on first import)
    for f in $(cd ${WORKDIR}/forgetest/forgetest && find . -name '*.py' -type f); do
        install -Dm 0644 ${WORKDIR}/forgetest/forgetest/$f \
            ${D}${PYTHON_SITEPACKAGES_DIR}/forgetest/$f
    done
    # the page's sources (ui/), gzipped: page.py inflates them once at
    # first request, and bytes here are bytes on the ext4 rootfs
    for f in $(cd ${WORKDIR}/forgetest/forgetest/ui && find . -type f); do
        install -d ${D}${PYTHON_SITEPACKAGES_DIR}/forgetest/ui/$(dirname $f)
        gzip -9 -n -c ${WORKDIR}/forgetest/forgetest/ui/$f \
            > ${D}${PYTHON_SITEPACKAGES_DIR}/forgetest/ui/$f.gz
        chmod 0644 ${D}${PYTHON_SITEPACKAGES_DIR}/forgetest/ui/$f.gz
    done
    # the bench scripts the #bench tab drives
    install -d ${D}${datadir}/forgetest/bench
    for f in ${WORKDIR}/scripts/bench/*.py; do
        install -m 0755 $f ${D}${datadir}/forgetest/bench/
    done
    install -Dm 0755 ${WORKDIR}/forgetest/forgetest.init ${D}${sysconfdir}/init.d/forgetest
}

FILES:${PN} += "${PYTHON_SITEPACKAGES_DIR}/forgetest ${datadir}/forgetest"
