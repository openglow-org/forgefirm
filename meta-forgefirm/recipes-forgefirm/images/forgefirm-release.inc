# Copyright 2026 514 LLC d/b/a OpenGlow
# Written by Scott Wiederhold
# SPDX-License-Identifier: MIT

# The release version, and nothing else.
#
# This file is metadata, not platform content: the image manifest leaves
# it out of the layer content hash the way it leaves out the component
# pin files (forgefirm-image-manifest.bbclass,
# FORGEFIRM_MANIFEST_VERSION_SUFFIX). A version bump therefore changes
# the version and invalidates nothing, so the number can be set at the
# moment a release is cut rather than before the campaign that authorizes
# it. Set inside forgefirm-image.bb instead, it would hash as layer content
# and invalidate every acceptance result.
#
# Nothing else belongs here. The version is still proven end to end:
# scripts/release.sh reads FORGEFIRM_RELEASE from this file, requires it to
# equal the rootfs stamp and the release tag, and gives it to mkfw.sh as
# the .fw meta-version; the acceptance test image.health compares the
# stamp on the running machine with the manifest's.
FORGEFIRM_RELEASE ?= "0.0.11"
