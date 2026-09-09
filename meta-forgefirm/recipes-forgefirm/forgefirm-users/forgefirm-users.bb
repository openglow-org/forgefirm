SUMMARY = "ForgeFIRM operator accounts: record render and the root shell warning"
DESCRIPTION = "Renders the account record (/data/forgefirm/users, written \
by forgectrl) into the system account files at boot and on reload. The \
rootfs is read-only: the four files show tmpfs copies, bind-mounted, and \
a render writes through them; the accounts the record does not name are \
left out. Also installs the warning an interactive root shell prints."
LICENSE = "MIT"
LIC_FILES_CHKSUM = "file://${COMMON_LICENSE_DIR}/MIT;md5=0835ade698e0bcf8506ecda2f7b4f302"

SRC_URI = " \
    file://forgefirm-users.init \
    file://forgefirm-root.sh \
"

S = "${WORKDIR}"

inherit update-rc.d

INITSCRIPT_NAME = "forgefirm-users"
# 05: rcS has run (mountall mounted /data and /run at S03) and sshd starts
# at S09, so the accounts exist before the first login can arrive.
INITSCRIPT_PARAMS = "start 05 2 3 4 5 ."

# No shadow tools: the read-only-rootfs image feature drops the shadow
# package from the image, and the script writes the account lines itself.

do_install() {
    install -Dm 0755 ${WORKDIR}/forgefirm-users.init ${D}${sysconfdir}/init.d/forgefirm-users
    install -Dm 0644 ${WORKDIR}/forgefirm-root.sh ${D}${sysconfdir}/profile.d/forgefirm-root.sh
}
