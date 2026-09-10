require recipes-glowforge/images/glowforge-image.bb

# /etc/forgefirm-manifest.json: the build-input identity the acceptance tool
# and the release gate compare (forgefirm-image-manifest.bbclass).
inherit forgefirm-image-manifest

DESCRIPTION = "OpenGlow/ForgeFIRM image for Glowforge"

# ForgeFIRM cuts the cloud dependency: drop the Glowforge cloud client
# (gfui-client connects to Glowforge's servers). Removing it here (override
# only; the shared glowforge-image base is untouched) also sidesteps its
# do_package failure. Its role is filled locally by forgectrl and the
# controllers below. nano is a bench convenience: on the release rootfs it
# costs 8.7 MB, most of it libmagic and its database, which nothing else
# here uses. The dev image blanks FORGEFIRM_RELEASE_TRIM to keep it (a
# removal spec is expanded when applied, so the variable, not the removal,
# is what a requiring recipe can override). python3 is the meta-package:
# it installs every standard-library module (tkinter, idle, 2to3, pydoc,
# ensurepip, venv, the debugger, asyncio, multiprocessing, xmlrpc), which
# nothing here imports. Each Python recipe declares the module packages it
# imports (python3-core plus the few it uses), so the meta-package goes.
FORGEFIRM_RELEASE_TRIM ?= "nano"
IMAGE_INSTALL:remove = "python3 ${FORGEFIRM_RELEASE_TRIM}"

# grblhal-glowforge: the grblHAL motion controller (Grbl over TCP:23).
# forgectrl: the ForgeFIRM machine-services daemon (HTTP :80, HTTPS :443
# with a self-signed certificate): controller supervisor, pulse-device
# broker, cooling engine, cameras, telemetry, settings, diagnostics, web
# control panel, and A/B updates.
# gfhome: one-shot Glowforge web-service homing, invoked by the controller
# for $H when homing_mode = gfcloud (/data/forgefirm/forgefirm.conf).
# gfcloud: full Glowforge web-service controller daemon (the factory cloud
# experience), started when controller_mode = cloud - mutually exclusive with
# grblHAL. Pulls python3-ffmachine (shared web-service machine glue).
# v4l-utils provides media-ctl / v4l2-ctl for the imx-media pipeline (also a
# forgectrl runtime dependency, kept explicit here for bring-up use).
# fwup: applies signed .fw archives (ForgeFIRM upgrades + factory restore)
# to the inactive rootfs slot.
# ffboot: boot-slot inventory and switching (also ships fw_env.config).
# slotmigrate: boot-time reclaim of the legacy p4 layout (grows /data).
# forgefirm-logging: the ForgeFIRM logging tree - renders the per-logger
# rsyslog rules from the settings before rsyslog starts, and drives
# size-capped rotation (boot + hourly; a full /data breaks settings,
# updates, and controller writes). rsyslog itself comes in through
# VIRTUAL-RUNTIME_base-utils-syslog (conf/distro/forgefirm.conf).
IMAGE_INSTALL:append = " grblhal-glowforge forgectrl gfhome gfcloud v4l-utils fwup ffboot slotmigrate forgefirm-logging"

# forgefirm-users: renders the operator account record
# (/data/forgefirm/users, written by forgectrl) into the system account
# files at boot, before sshd, and on reload; also installs the warning an
# interactive root shell prints. forgefirm-hostname: names the machine
# forgefirm-<xxxx> from its MAC address, before the network starts.
# forgefirm-banner: keeps the control panel addresses in the
# serial-console banner (/etc/issue).
# forgefirm-persist: the boot timestamp and the random seed on /data.
IMAGE_INSTALL:append = " forgefirm-users forgefirm-hostname forgefirm-banner forgefirm-persist"

# The rootfs mounts read-only on both images; /data (p3) is the writable
# partition. read-only-rootfs is poky's feature for it: the root line of
# /etc/fstab (the BSP's, already ro) and ROOTFS_READ_ONLY in
# /etc/default/rcS, the volatile links made at rootfs time
# (populate-volatile.sh: /etc/resolv.conf, /tmp), a writable copy of
# /var/lib at boot (read-only-rootfs-hook.sh), a build failure for a
# package whose post-install must run on the machine, and the removal of
# the packages a read-only rootfs cannot use (shadow, base-passwd,
# update-rc.d, update-alternatives; the account files stay). What must
# last or change at run time is handled file by file: the account files,
# /etc/hostname and /etc/issue (forgefirm-users, forgefirm-hostname,
# forgefirm-banner), the sshd host keys
# (recipes-connectivity/openssh), the timestamp and the random seed
# (forgefirm-persist). The facts are on the docs site,
# technical/forgefirm/image-and-bsp; scripts/release.sh checks the built
# rootfs for this state.
IMAGE_FEATURES += "read-only-rootfs"

# Root policy. root has no password and logs in at the serial console
# only: that is the recovery path when the network, the panel or an
# account is broken, and the console is behind the case. Over the
# network, sshd refuses root (PermitRootLogin no) and any account without
# a password (PermitEmptyPasswords no), both set by
# recipes-connectivity/openssh, and sshd runs only when the panel turns
# it on. Operator logins are the accounts in the record (forgefirm-users).
# empty-root-password keeps the rootfs postprocess from locking root
# (zap_empty_root_password in rootfs-postcommands.bbclass); it is not
# debug-tweaks, which belongs to the dev image alone and would open SSH.
# scripts/release.sh checks the built rootfs for exactly this state.
IMAGE_FEATURES += "empty-root-password"

# Mesa GLES2/EGL on etnaviv for forgectrl's GPU demosaic (loaded with
# dlopen at runtime; forgectrl itself has no build-time GL dependency,
# and without these packages it falls back to the NEON path).
IMAGE_INSTALL:append = " libegl-mesa libgles2-mesa libgbm mesa-megadriver"

# NXP's firmware EULA covers the i.MX VPU/EPDC blobs the BSP installs, so the
# image ships the license text with them (/usr/share/licenses/firmware-imx).
# The SDMA firmware brings its own -license package through linux-firmware.
IMAGE_INSTALL:append = " firmware-imx-lic"

# The release rootfs must fit a 200 MiB factory eMMC slot (409600 blocks).
# Sizing: content + 40 MiB working space, hard-capped at the slot size:
# the build fails rather than emit an unflashable image. The raw ext4 is
# deployed alongside the wic; scripts/mkfw.sh packs it into the signed
# .fw release artifact.
IMAGE_FSTYPES:append = " ext4"
IMAGE_OVERHEAD_FACTOR = "1.0"
IMAGE_ROOTFS_EXTRA_SPACE = "40960"
IMAGE_ROOTFS_MAXSIZE = "204800"

# The ForgeFIRM mark and the version stamp:
# /etc/forgefirm-version (machine-readable), and, under the OpenGlow mark
# the base image carries (base-files, meta-openglow), the ForgeFIRM mark
# with the version on its last line, right-justified to the mark's last
# column, in the two files a person reads - the serial-console login
# prompt (/etc/issue) and the motd, which every login prints, the network
# ones included. The mark names the firmware, so the version stands
# alone.
#
# The mark is written once here and rendered for each reader, because the
# two files are read by different programs:
#   /etc/issue  busybox getty parses it, and both a backslash and a
#               percent sign start an escape (libbb/login.c,
#               print_login_issue). An unrecognized escape prints the
#               character and swallows the backslash, so the art goes in
#               with every backslash doubled.
#   /etc/motd   a login writes it out as it is: nothing is doubled.
# Both keep their color: a getty passes an escape character through, and
# so does a login writing the motd. Only the ssh client escapes one, and
# it does that to a banner alone.
# The pre-authentication banner (/etc/issue.net) stays unused: the ssh
# client prints a control character in a banner as an octal escape, and
# the machine tells a client that has not logged in nothing anyway.
#
# Release images carry the release version; the dev image overrides the
# string with the build timestamp (the same DATETIME as the artifact
# name) plus a dev tag.
#
# FORGEFIRM_RELEASE lives in its own file, which the manifest leaves out
# of the layer content hash: the version is metadata, and a bump must not
# read as a platform change and invalidate a campaign
# (forgefirm-release.inc).
require forgefirm-release.inc
FORGEFIRM_VERSION_STRING ?= "v${FORGEFIRM_RELEASE}"

write_forgefirm_version() {
    echo "${FORGEFIRM_VERSION_STRING}" > ${IMAGE_ROOTFS}${sysconfdir}/forgefirm-version

    mark=${WORKDIR}/forgefirm-mark
    cat > $mark <<'MARK'
  ___                 [1;39m___ ___ ___ __  __[0m
 | __|__ _ _ __ _ ___[1;39m| __|_ _| _ \  \/  |[0m
 | _/ _ \ '_/ _` / -_) [1;39m_| | ||   / |\/| |[0m
 |_|\___/_| \__, \___|[1;39m_| |___|_|_\_|  |_|[0m
            |___/
MARK

    # The version rides the mark's own last line, its last character on
    # the mark's last column: under the FIRM half, in the room the
    # descender of the Forge half leaves. The mark carries the name, so
    # the version stands alone. Measured in columns, not in bytes: the
    # color sequences take no room on the screen, and the doubling below
    # is undone by the getty that reads it. A version with no room left
    # keeps one space and runs past the mark rather than being cut.
    stamped=${WORKDIR}/forgefirm-mark-stamped
    awk -v v="${FORGEFIRM_VERSION_STRING}" '
        { line[NR] = $0
          bare = $0
          gsub(/\033\[[0-9;]*m/, "", bare)
          col[NR] = length(bare)
          if (col[NR] > w) w = col[NR] }
        END { for (i = 1; i < NR; i++) print line[i]
              pad = w - col[NR] - length(v)
              if (pad < 1) pad = 1
              gap = ""
              while (length(gap) < pad) gap = gap " "
              print line[NR] gap v }' $mark > $stamped

    sed 's|\\|\\\\|g' $stamped >> ${IMAGE_ROOTFS}${sysconfdir}/issue
    echo "" >> ${IMAGE_ROOTFS}${sysconfdir}/issue

    cat $stamped >> ${IMAGE_ROOTFS}${sysconfdir}/motd
    echo "" >> ${IMAGE_ROOTFS}${sysconfdir}/motd

    rm -f $mark $stamped
}
write_forgefirm_version[vardepsexclude] += "DATETIME"
# No semicolon after a function name here or below (the vardeps rule in
# classes/forgefirm-image-manifest.bbclass).
ROOTFS_POSTPROCESS_COMMAND += "write_forgefirm_version "

# The license texts ride with the software. The license class writes
# the image's license manifest (every installed package with its
# license) and copies each package's license texts into
# /usr/share/common-licenses, one copy of each generic text and
# symlinks to it per package. That tree costs several megabytes of
# small files on a raw ext4 rootfs, so it is packed into one
# reproducible tar.gz (sorted names, no timestamps, no owners) at
# /usr/share/forgefirm/licenses.tar.gz and the tree is removed. The
# control panel serves the bundle and its manifest (GET /system/licenses,
# GET /system/licenses/manifest). The license class runs first
# (license_create_manifest is prepended to this list); this step is
# appended, so it runs after.
COPY_LIC_MANIFEST = "1"
COPY_LIC_DIRS = "1"

pack_licenses() {
    d="${IMAGE_ROOTFS}${datadir}/common-licenses"
    [ -d "$d" ] || bbfatal "pack_licenses: $d is missing (COPY_LIC_DIRS off?)"
    [ -f "$d/license.manifest" ] || bbfatal "pack_licenses: no license.manifest in $d"
    install -d "${IMAGE_ROOTFS}${datadir}/forgefirm"
    tar -C "${IMAGE_ROOTFS}${datadir}" --sort=name --mtime=@0 --owner=0 --group=0 \
        --numeric-owner -cf - common-licenses | gzip -9 -n \
        > "${IMAGE_ROOTFS}${datadir}/forgefirm/licenses.tar.gz"
    rm -rf "$d"
}
ROOTFS_POSTPROCESS_COMMAND += "pack_licenses "
