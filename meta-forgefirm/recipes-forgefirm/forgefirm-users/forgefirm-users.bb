SUMMARY = "ForgeFIRM operator accounts: record replay and the root shell warning"
DESCRIPTION = "Replays the account record (/data/forgefirm/users, written \
by forgectrl) into the system account files at boot and on reload, \
removes the local accounts the record does not name, and installs the \
warning an interactive root shell prints."
LICENSE = "MIT"
LIC_FILES_CHKSUM = "file://${COMMON_LICENSE_DIR}/MIT;md5=0835ade698e0bcf8506ecda2f7b4f302"

SRC_URI = " \
    file://forgefirm-users.init \
    file://forgefirm-root.sh \
"

S = "${WORKDIR}"

inherit update-rc.d

INITSCRIPT_NAME = "forgefirm-users"
# 05: rcS has run (mountall mounted /data at S03) and sshd starts at
# S09, so the accounts exist before the first login can arrive.
INITSCRIPT_PARAMS = "start 05 2 3 4 5 ."

# useradd, groupadd, usermod, userdel, groupdel
RDEPENDS:${PN} += "shadow"

do_install() {
    install -Dm 0755 ${WORKDIR}/forgefirm-users.init ${D}${sysconfdir}/init.d/forgefirm-users
    install -Dm 0644 ${WORKDIR}/forgefirm-root.sh ${D}${sysconfdir}/profile.d/forgefirm-root.sh
}
