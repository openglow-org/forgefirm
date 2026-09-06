require forgefirm-image.bb

DESCRIPTION = "OpenGlow/ForgeFIRM development image for Glowforge"

# Strict superset of forgefirm-image: everything the main image ships, plus
# debug tooling. forgetest is the release acceptance tool (HTTP :8090) and
# the bench diagnostics page; it belongs to the bench, never to a release
# image (https://docs.forgefirm.org/developers/acceptance/). The gfutilities
# emulator fixtures are the canned frames its service-protocol test answers
# the service with.
IMAGE_INSTALL += " \
  forgetest \
  python3-gfutilities-emulator \
  python3-mmap \
  python3-ctypes \
  htop \
"

# python3-mmap and python3-ctypes serve the bench scripts that read the SoC
# pads through /dev/mem (resume_dark_lead.py, cp_watchdog_timing.py, the
# accelerometer probes). They are bench tooling: the release image carries
# neither, and forgetest itself imports neither.

# The release recipe trims nano from the shared base list (bench
# convenience, 8.7 MB with libmagic); the dev image keeps it. Removal specs
# accumulate and apply after every append, so the variable inside the
# release recipe's spec is what gets blanked here.
FORGEFIRM_RELEASE_TRIM = ""

# debug-tweaks (passwordless root, root SSH login) belongs ONLY to the dev
# image - never the release image. It lives here, not in the shared kas
# local.conf, so the release forgefirm-image cannot inherit it.
IMAGE_FEATURES += " \
  tools-debug \
  debug-tweaks \
"

# Dev images boot from SD, never from a 200 MiB eMMC slot: lift the slot
# ceiling and give the filesystem generous working space instead.
IMAGE_ROOTFS_MAXSIZE = ""
IMAGE_ROOTFS_EXTRA_SPACE = "262144"

# Dev builds identify by build timestamp (matches the artifact name),
# tagged so a bench machine is never mistaken for a release.
FORGEFIRM_VERSION_STRING = "${DATETIME} (dev)"

# /etc/forgefirm-dev marks a dev image on the rootfs; its content is the
# version string. The sshd init script starts sshd on a dev image without
# the panel's enable flag (/run/forgefirm/ssh-enabled), and forgectrl
# reads the same marker. A release image has no such file.
write_forgefirm_dev_marker() {
    echo "${FORGEFIRM_VERSION_STRING}" > ${IMAGE_ROOTFS}${sysconfdir}/forgefirm-dev
}
write_forgefirm_dev_marker[vardepsexclude] += "DATETIME"
ROOTFS_POSTPROCESS_COMMAND += "write_forgefirm_dev_marker;"
