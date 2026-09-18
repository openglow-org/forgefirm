# SPDX-License-Identifier: MIT

# rsyslog is the ForgeFIRM system logger: the only log writer, one
# directory per logger under /data/log/forgefirm. This appends the
# ForgeFIRM /etc/rsyslog.conf (inputs, line format, and the include of
# the per-logger rules that forgectrl renders at boot); the feature set
# is trimmed in conf/distro/forgefirm.conf (PACKAGECONFIG:pn-rsyslog).
FILESEXTRAPATHS:prepend := "${THISDIR}/${PN}:"

# Start at 20 (after forgefirm-logging renders the rules at 19); stop
# LAST at shutdown - after the controllers (K80) and forgectrl (K90) -
# so their shutdown lines still reach the disk.
INITSCRIPT_PARAMS = "start 20 2 3 4 5 . stop 95 0 1 6 ."

do_install:append() {
    # The stock rotation set covers /var/log files this image never
    # writes; the ForgeFIRM tree has its own (forgefirm-logging).
    rm -f ${D}${sysconfdir}/logrotate.d/logrotate.rsyslog
}
