# Copyright 2026 514 LLC d/b/a OpenGlow
# Written by Scott Wiederhold
# SPDX-License-Identifier: MIT

# ForgeFIRM image manifest: kernel identity (forgefirm-image-manifest.bbclass
# collects it from DEPLOY_DIR_IMAGE). The kernel tree is too large to list
# per file and every acceptance test depends on it anyway, so the entry
# carries the pinned revision and the hash of the configuration actually
# built as pseudo-files ("@srcrev", "@config") that the fingerprint globs
# match like any other path. Device-tree sources and config fragments come
# from the BSP layer, which the image manifest hashes by content. The
# entry also carries the build ID of the vmlinux built (kernel_build_id):
# the image class moves it to the manifest's build section, outside the
# identity, and forgetest compares it with the running kernel's.
do_deploy:append:glowforge() {
    install -d ${DEPLOYDIR}/forgefirm-manifest.d
    cfg=$(sha256sum ${B}/.config | cut -d' ' -f1)
    bid=$(${READELF} -n ${B}/vmlinux 2>/dev/null | sed -n 's/^ *Build ID: *\([0-9a-f]*\) *$/\1/p' | head -n 1)
    printf '{"component":"%s","config_sha256":"%s","files":[["@config","%s"],["@srcrev","%s"]],"kernel_build_id":"%s","linux_version":"%s","pv":"%s","recipe":"%s","source":"%s","srcrev":"%s"}\n' \
        "${PN}" "$cfg" "$cfg" "${SRCREV}" "$bid" "${LINUX_VERSION}" "${PV}" "${PN}" \
        "git://github.com/Freescale/linux-fslc.git" "${SRCREV}" \
        > ${DEPLOYDIR}/forgefirm-manifest.d/${PN}.json
}
