# SPDX-License-Identifier: MIT

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
# The rootfs lint holds a dev image to the dev policy: root and an empty
# password over SSH are what debug-tweaks turns on.
FORGEFIRM_IMAGE_KIND = "dev"

# Sizing is the release recipe's: content + 16 MiB, capped at the 200 MiB
# slot, so the dev rootfs fits a slot too. The bench runs it from SD.

# The SD card image (forgefirm-dev-sd.wks.in, in this layer's wic/): the
# rootfs in a partition of the slot's size, so a later build's raw ext4
# can be written into it alone, and a data partition of the card's own.
# forgefirm-data shows the card's forgefirm/ at /data/forgefirm and its
# log/ at /data/log/forgefirm on a boot from the card; everything else in
# /data, the acceptance records among it, stays the eMMC's. The card
# starts as a finished first run in a known configuration, whatever the
# bench machine holds: ForgeFIRM's default settings, the account
# forgefirm with the password forgefirm (the panel refuses a password
# equal to the name when an account is made there; this one is made
# here), the intake fan count the setup gate asks for (the default, 2),
# and setup.seed, which forgectrl turns into the setup record at its
# first start: the record needs the advisories' hashes and the step
# versions, which only the running build knows. The import marker at the
# card's top has forgefirm-data copy the machine's identity (the sshd
# host keys, the TLS key and certificate, the WiFi region, the last saved
# time) from the eMMC
# at the card's first boot. The .fw keeps the eMMC's /data.
WKS_FILE = "forgefirm-dev-sd.wks.in"
FORGEFIRM_SD_DATA = "${WORKDIR}/forgefirm-sd-data"
# openssl passwd -6 -salt forgefirmsdcard forgefirm
FORGEFIRM_SD_ACCOUNT_HASH = "$6$forgefirmsdcard$y4tKCFpeLtsgRb6hpFu3YvG40VzJF5cGvAw7CHKV3KtHap2DnucjBxgN3trsyhtwpZubJ8YPHt.u5xtf2n3Mt."

# Made in do_image_wic, which runs under pseudo: the files are root's on
# the card. wic makes the partition with the image's own pseudo database
# (the directory beside the content, ${WORKDIR}/pseudo).
forgefirm_sd_data() {
    d="${FORGEFIRM_SD_DATA}"
    rm -rf "$d"
    install -d -m 0755 "$d" "$d/log"
    install -d -m 0711 "$d/forgefirm"
    printf 'forgefirm:%s:1000\n' '${FORGEFIRM_SD_ACCOUNT_HASH}' > "$d/forgefirm/users"
    printf '%s\n' \
        "# The SD card's settings: ForgeFIRM's defaults, and the intake fan" \
        "# count the setup gate asks for." \
        "cool_intake_fans = 2" > "$d/forgefirm/forgefirm.conf"
    : > "$d/forgefirm/setup.seed"
    : > "$d/import"
    chmod 0600 "$d/forgefirm/users" "$d/forgefirm/forgefirm.conf" "$d/forgefirm/setup.seed"
    chown -R 0:0 "$d"
}
do_image_wic[prefuncs] += "forgefirm_sd_data"

# The image check reads the SD card image back too (image-check.py): p1
# the slot's size with the rootfs's own fstab, p2 the card's data
# partition (1024 MiB, forgefirm-dev-sd.wks.in) seeded as above.
FORGEFIRM_IMAGE_CHECK_CARD = "--card ${IMGDEPLOYDIR}/${IMAGE_NAME}.wic \
    --card-rootfs-kib ${IMAGE_ROOTFS_MAXSIZE} --data-label forgefirm-data --data-kib 1048576 \
    --account 'forgefirm:${FORGEFIRM_SD_ACCOUNT_HASH}:1000'"
FORGEFIRM_IMAGE_CHECK_CARD[vardepsexclude] += "DATETIME"

# Dev builds identify by build timestamp (matches the artifact name),
# tagged so a bench machine is never mistaken for a release.
FORGEFIRM_VERSION_STRING = "${DATETIME} (dev)"
# The rootfs functions that write the string are in the do_rootfs
# signature; DATETIME must not be, or the basehash changes at every parse
# and bitbake refuses the build as non-deterministic.
FORGEFIRM_VERSION_STRING[vardepsexclude] += "DATETIME"

# /etc/forgefirm-dev marks a dev image on the rootfs; its content is the
# version string. The sshd init script starts sshd on a dev image without
# the panel's enable flag (/run/forgefirm/ssh-enabled), and forgectrl
# reads the same marker. A release image has no such file.
write_forgefirm_dev_marker() {
    echo "${FORGEFIRM_VERSION_STRING}" > ${IMAGE_ROOTFS}${sysconfdir}/forgefirm-dev
}
write_forgefirm_dev_marker[vardepsexclude] += "DATETIME"
ROOTFS_POSTPROCESS_COMMAND += "write_forgefirm_dev_marker "

# The two factory rootfs slots, read-only under /factory/img1 and
# /factory/img2: a bench convenience for reading a factory image in place
# (the ffboot inventory reuses the mounts). The release image mounts no
# factory slot; it reads one through a temporary read-only mount when it
# needs to (ffboot -l). No nofail: busybox mount hands it to the kernel
# as a filesystem parameter, which the kernel rejects (the mount fails
# with EINVAL); mount -a goes on past a slot that does not mount anyway.
add_factory_slot_mounts() {
    install -d ${IMAGE_ROOTFS}/factory/img1 ${IMAGE_ROOTFS}/factory/img2
    printf '%s\n' \
        '/dev/mmcblk2p1       /factory/img1        auto       ro,noatime            0  0' \
        '/dev/mmcblk2p2       /factory/img2        auto       ro,noatime            0  0' \
        >> ${IMAGE_ROOTFS}${sysconfdir}/fstab
}
ROOTFS_POSTPROCESS_COMMAND += "add_factory_slot_mounts "
