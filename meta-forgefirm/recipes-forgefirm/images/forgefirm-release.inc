# The release version, and nothing else.
#
# This file is metadata, not platform content: the image manifest leaves
# it out of the layer content hash the way it leaves out the component
# pin files (forgefirm-image-manifest.bbclass,
# FORGEFIRM_MANIFEST_VERSION_SUFFIX). A version bump therefore changes
# the version and invalidates nothing, so the number can be set at the
# moment a release is cut rather than before the campaign that authorizes
# it. Setting it inside forgefirm-image.bb instead would hash as layer
# content and invalidate every acceptance result, which is what it did
# until v0.0.1.
#
# Nothing else belongs here. The version is still proven end to end:
# scripts/release.sh reads FORGEFIRM_RELEASE from this file and requires
# it to equal the rootfs stamp, the .fw meta-version and the release tag,
# and the acceptance test image.health compares the stamp on the running
# machine with the manifest's.
FORGEFIRM_RELEASE ?= "0.0.1"
