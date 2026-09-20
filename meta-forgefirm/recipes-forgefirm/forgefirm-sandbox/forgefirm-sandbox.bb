# SPDX-License-Identifier: MIT

SUMMARY = "ForgeFIRM extension sandbox: the account pool, the cgroup tree, and the deny rules"
DESCRIPTION = "What the image holds ready before any extension package runs: \
a static pool of system accounts (ffx0 to ffx31, uid and gid 800 to 831), \
the cgroup v2 tree with the cpu, memory, and pids controllers handed down to \
/sys/fs/cgroup/ffx, and the nftables table that refuses every packet a pool \
uid sends, loopback included, until an allowlist names its destination. The \
rules load from rcS, before the network starts."
LICENSE = "MIT"
LIC_FILES_CHKSUM = "file://${COMMON_LICENSE_DIR}/MIT;md5=0835ade698e0bcf8506ecda2f7b4f302"

SRC_URI = " \
    file://forgefirm-sandbox.init \
    file://ffx.nft \
"

S = "${WORKDIR}"

inherit update-rc.d useradd

INITSCRIPT_NAME = "forgefirm-sandbox"
# 30 in rcS: /sys is mounted (sysfs.sh, S02) and nothing the script needs
# lives on /data. The network starts in rc5 (S01), so no pool uid ever has
# an interface to send on before the rules are in the kernel.
INITSCRIPT_PARAMS = "start 30 S ."

# The pool. Static ids below 1000, in a block no other account uses (the
# image's dynamic system ids count down from 999): the forgefirm-users render
# replaces only the accounts from 1000 up, so an account reset leaves these
# alone, and the read-only rootfs never needs a useradd at run time. One
# group per account, so no two slots share a group. No home, no shell, and
# the password field useradd leaves locked. The rules in ffx.nft and the
# pool size here name the same range.
FFX_POOL_SIZE = "32"
FFX_POOL_BASE = "800"
USERADD_PACKAGES = "${PN}"
GROUPADD_PARAM:${PN} = "${@'; '.join('--system -g %d ffx%d' % (int(d.getVar('FFX_POOL_BASE')) + n, n) for n in range(int(d.getVar('FFX_POOL_SIZE'))))}"
USERADD_PARAM:${PN} = "${@'; '.join('--system -u %d -g ffx%d -M -d /nonexistent -s /bin/false ffx%d' % (int(d.getVar('FFX_POOL_BASE')) + n, n, n) for n in range(int(d.getVar('FFX_POOL_SIZE'))))}"

do_install() {
    install -Dm 0755 ${WORKDIR}/forgefirm-sandbox.init ${D}${sysconfdir}/init.d/forgefirm-sandbox
    install -Dm 0644 ${WORKDIR}/ffx.nft ${D}${sysconfdir}/forgefirm/ffx.nft
}

RDEPENDS:${PN} = "nftables"
