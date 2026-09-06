FILESEXTRAPATHS:prepend := "${THISDIR}/${BPN}:"

# mDNS for the control panel: the machine answers forgefirm.local and
# advertises the panel on HTTPS 443 and HTTP 80. Only avahi-daemon is
# installed (forgefirm-image.bb); the build options that keep it to the
# daemon are in conf/distro/forgefirm.conf. The daemon reads the service
# file itself: no D-Bus is involved.
SRC_URI += " \
    file://avahi-daemon.conf \
    file://forgefirm.service \
"

do_install:append() {
    install -m 0644 ${WORKDIR}/avahi-daemon.conf ${D}${sysconfdir}/avahi/avahi-daemon.conf
    install -d ${D}${sysconfdir}/avahi/services
    install -m 0644 ${WORKDIR}/forgefirm.service ${D}${sysconfdir}/avahi/services/forgefirm.service
}
