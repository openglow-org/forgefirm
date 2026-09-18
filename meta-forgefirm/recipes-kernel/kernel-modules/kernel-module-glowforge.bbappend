# SPDX-License-Identifier: MIT

# ForgeFIRM image manifest: source fingerprint of glowforge.ko.
#
# kernel-module-split packages the .ko into a versioned package that the
# recipe's main package only RPROVIDES, so a rootfs file installed here
# would never reach the image. The entry is deployed instead; the image
# collects it from DEPLOY_DIR_IMAGE (forgefirm-image-manifest.bbclass).
inherit forgefirm-manifest deploy
FORGEFIRM_MANIFEST_MODE = "deploy"

python do_deploy() {
    import os
    entry = forgefirm_manifest_entry(d)
    forgefirm_manifest_write(entry, os.path.join(d.getVar('DEPLOYDIR'), 'forgefirm-manifest.d',
                                                 d.getVar('PN') + '.json'))
}
do_deploy[vardeps] += "FORGEFIRM_MANIFEST_NAME SRCREV SRC_URI forgefirm_manifest_entry \
    forgefirm_manifest_tree forgefirm_manifest_git forgefirm_manifest_write"
addtask deploy after do_install before do_build
