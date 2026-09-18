# SPDX-License-Identifier: MIT

# ForgeFIRM image manifest: kernel identity (forgefirm-image-manifest.bbclass
# collects it from DEPLOY_DIR_IMAGE). The kernel tree is too large to list
# per file and every acceptance test depends on it anyway, so the entry
# carries the pinned revision and the hash of the configuration actually
# built as pseudo-files ("@srcrev", "@config") that the fingerprint globs
# match like any other path. Device-tree sources and config fragments come
# from the BSP layer, which the image manifest hashes by content.
do_deploy:append:glowforge() {
    install -d ${DEPLOYDIR}/forgefirm-manifest.d
    cfg=$(sha256sum ${B}/.config | cut -d' ' -f1)
    printf '{"component":"%s","config_sha256":"%s","files":[["@config","%s"],["@srcrev","%s"]],"linux_version":"%s","pv":"%s","recipe":"%s","source":"%s","srcrev":"%s"}\n' \
        "${PN}" "$cfg" "$cfg" "${SRCREV}" "${LINUX_VERSION}" "${PV}" "${PN}" \
        "git://github.com/Freescale/linux-fslc.git" "${SRCREV}" \
        > ${DEPLOYDIR}/forgefirm-manifest.d/${PN}.json
}
