#!/bin/sh
# Copyright 2026 514 LLC d/b/a OpenGlow
# Written by Scott Wiederhold
# https://community.openglow.org
# SPDX-License-Identifier:    MIT
#
# rootfs-lint.sh <rootfs> <release|dev>
#
# The image content that is fixed at build time, checked on the tree as it
# is imaged: a failure here fails the build, on the release image too,
# which no campaign runs. forgefirm-image.bb's rootfs_lint runs it last of
# the rootfs steps; forgetest's test_image.py runs it on fixture trees.
# Every rule is checked and every failure printed, one line each, before
# it exits 1. The rules:
#
#   syslog     rsyslog is the only logger: /usr/sbin/rsyslogd is on the
#              image, the syslog init script starts it and runlevel 5
#              starts that script; no syslogd or klogd is linked in a bin
#              directory, and no init script runs one.
#   sshd       every sshd configuration the image carries (sshd_config,
#              and sshd_config_readonly, which the read-only rootfs runs)
#              takes passwords (PasswordAuthentication yes) and sends no
#              pre-authentication banner; on a release image it refuses
#              root (PermitRootLogin no) and an empty password
#              (PermitEmptyPasswords no). Nothing in sshd_config.d sets
#              these, since an included value comes first.
#   dhcp-name  the wlan0 DHCP stanza sends the machine's name:
#              udhcpc_opts carries -x hostname:$(hostname).
#   mdns       no mDNS responder: no /etc/avahi, no avahi-daemon, no
#              libnss_mdns, and the hosts line of nsswitch.conf names no
#              mdns source.
#   init-order runlevel 5 starts forgectrl before forgeext, and runlevel 6
#              stops grblhal before forgectrl and forgeext before
#              forgectrl: the links sort in that order.
#   logging    a ForgeFIRM logging init script is on the image
#              (forgefirm-logging or forgefirm-logrotate).
#   watchdog   no userspace watchdog init script (/etc/init.d/watchdog):
#              the kernel feeds the hardware watchdog.
#   read-only  /etc/default/rcS sets ROOTFS_READ_ONLY=yes.
#   dhcpv6     no DHCPv6 client (udhcpc6, dhcp6c, odhcp6c) in a bin
#              directory, and no /usr/share/udhcpc/default6.script.
#   factory    on a release image, no /etc/fstab line mounts anything
#              under /factory/ (the dev image mounts the factory slots).

root=${1:?rootfs}
kind=${2:?release or dev}
case "$kind" in
    release|dev) ;;
    *) echo "rootfs-lint: the image kind is release or dev, not $kind" >&2; exit 2 ;;
esac
[ -d "$root" ] || { echo "rootfs-lint: $root is not a directory" >&2; exit 2; }

failed=0
fail() { echo "rootfs-lint: $1: $2" >&2; failed=1; }

# The active lines of a file: no comment, no blank line.
active() { grep -v -E '^[[:space:]]*(#|$)' "$1" 2>/dev/null; }

# ---------------------------------------------------------------- syslog
[ -x "$root/usr/sbin/rsyslogd" ] || fail syslog "/usr/sbin/rsyslogd is not on the image"
if [ -f "$root/etc/init.d/syslog" ]; then
    active "$root/etc/init.d/syslog" | grep -q '/usr/sbin/rsyslogd' \
        || fail syslog "/etc/init.d/syslog does not start /usr/sbin/rsyslogd"
else
    fail syslog "there is no /etc/init.d/syslog"
fi
started=
for l in "$root"/etc/rc5.d/S*; do
    [ -L "$l" ] && [ "$(readlink "$l")" = "../init.d/syslog" ] && started=$l
done
[ -n "$started" ] || fail syslog "runlevel 5 does not start /etc/init.d/syslog"
for d in bin sbin usr/bin usr/sbin; do
    for p in syslogd klogd; do
        if [ -e "$root/$d/$p" ] || [ -L "$root/$d/$p" ]; then
            fail syslog "/$d/$p is on the image beside rsyslog"
        fi
    done
done
for s in "$root"/etc/init.d/*; do
    [ -f "$s" ] || continue
    # syslogd or klogd as a word of its own: rsyslogd is not one
    if active "$s" | grep -q -E '(^|[^[:alnum:]_.-])(syslogd|klogd)([^[:alnum:]_.-]|$)'; then
        fail syslog "/etc/init.d/${s##*/} runs syslogd or klogd"
    fi
done

# ------------------------------------------------------------------ sshd
# The first value a configuration sets for a keyword before any Match
# block: sshd takes the first one it reads. Keywords are case-insensitive,
# and a value follows spaces or an equals sign.
sshd_value() {
    awk -v k="$2" '
        { line = $0; sub(/^[ \t]+/, "", line) }
        line == "" || line ~ /^#/ { next }
        { n = split(line, f, /[ \t=]+/); key = tolower(f[1]) }
        key == "match" { exit }
        key == tolower(k) { print (n > 1 ? f[2] : ""); exit }' "$1"
}
configs=
for c in sshd_config sshd_config_readonly; do
    [ -f "$root/etc/ssh/$c" ] && configs="$configs $c"
done
[ -n "$configs" ] || fail sshd "the image carries no /etc/ssh/sshd_config"
for c in $configs; do
    f=$root/etc/ssh/$c
    v=$(sshd_value "$f" PasswordAuthentication)
    [ "$v" = yes ] || fail sshd "$c: PasswordAuthentication is '${v:-unset}', not yes"
    v=$(sshd_value "$f" Banner)
    [ -z "$v" ] || [ "$v" = none ] || fail sshd "$c: Banner $v sends a pre-authentication banner"
    if [ "$kind" = release ]; then
        v=$(sshd_value "$f" PermitRootLogin)
        [ "$v" = no ] || fail sshd "$c: PermitRootLogin is '${v:-unset}', not no"
        v=$(sshd_value "$f" PermitEmptyPasswords)
        [ "$v" = no ] || fail sshd "$c: PermitEmptyPasswords is '${v:-unset}', not no"
    fi
done
for f in "$root"/etc/ssh/sshd_config.d/*.conf; do
    [ -f "$f" ] || continue
    for k in PasswordAuthentication Banner PermitRootLogin PermitEmptyPasswords; do
        v=$(sshd_value "$f" "$k")
        [ -z "$v" ] || fail sshd "sshd_config.d/${f##*/} sets $k $v ahead of sshd_config"
    done
done

# ------------------------------------------------------------- dhcp-name
ifaces=$root/etc/network/interfaces
if [ -f "$ifaces" ]; then
    # the udhcpc_opts lines of the wlan0 dhcp stanza, which runs to the
    # next stanza
    opts=$(awk '
        /^[ \t]*#/ { next }
        /^(iface|auto|allow-|mapping|source)/ { in_w = ($1 == "iface" && $2 == "wlan0" && $4 == "dhcp"); next }
        in_w && $1 == "udhcpc_opts" { sub(/^[ \t]*udhcpc_opts[ \t]+/, ""); print }' "$ifaces")
    case " $opts " in
        *' -x hostname:$(hostname) '*) ;;
        *) fail dhcp-name "the wlan0 dhcp stanza does not send -x hostname:\$(hostname) (udhcpc_opts: '${opts}')" ;;
    esac
else
    fail dhcp-name "there is no /etc/network/interfaces"
fi

# ------------------------------------------------------------------ mdns
[ -e "$root/etc/avahi" ] && fail mdns "/etc/avahi is on the image"
for d in sbin usr/sbin; do
    [ -e "$root/$d/avahi-daemon" ] && fail mdns "/$d/avahi-daemon is on the image"
done
for d in lib usr/lib; do
    for l in "$root/$d"/libnss_mdns*; do
        [ -e "$l" ] && fail mdns "/$d/${l##*/} is on the image"
    done
done
if active "$root/etc/nsswitch.conf" | grep -E '^[[:space:]]*hosts:' | grep -q 'mdns'; then
    fail mdns "nsswitch.conf resolves hosts through mdns"
fi

# ------------------------------------------------------------ init-order
# link <runlevel dir> <S|K> <name>: the first link of that kind whose name
# ends in name, or nothing
link() {
    for l in "$root/etc/$1"/"$2"*"$3"; do
        [ -L "$l" ] || [ -e "$l" ] || continue
        echo "${l##*/}"
        return
    done
}
# before <what> <a> <b>: a and b are both there and a sorts first
before() {
    if [ -z "$2" ] || [ -z "$3" ]; then
        fail init-order "$1: a link is missing (${2:-none}, ${3:-none})"
    elif [ "$(printf '%s\n%s\n' "$2" "$3" | LC_ALL=C sort | head -n 1)" != "$2" ] || [ "$2" = "$3" ]; then
        fail init-order "$1: $2 does not sort before $3"
    fi
}
before "rc5.d starts forgectrl before forgeext" "$(link rc5.d S forgectrl)" "$(link rc5.d S forgeext)"
before "rc6.d stops grblhal before forgectrl" "$(link rc6.d K grblhal)" "$(link rc6.d K forgectrl)"
before "rc6.d stops forgeext before forgectrl" "$(link rc6.d K forgeext)" "$(link rc6.d K forgectrl)"

# --------------------------------------------------------------- logging
[ -e "$root/etc/init.d/forgefirm-logging" ] || [ -e "$root/etc/init.d/forgefirm-logrotate" ] \
    || fail logging "no ForgeFIRM logging init script (forgefirm-logging, forgefirm-logrotate)"

# -------------------------------------------------------------- watchdog
[ -e "$root/etc/init.d/watchdog" ] && fail watchdog "/etc/init.d/watchdog is on the image"

# ------------------------------------------------------------- read-only
grep -qx 'ROOTFS_READ_ONLY=yes' "$root/etc/default/rcS" 2>/dev/null \
    || fail read-only "/etc/default/rcS does not set ROOTFS_READ_ONLY=yes"

# ---------------------------------------------------------------- dhcpv6
for d in bin sbin usr/bin usr/sbin; do
    for p in udhcpc6 dhcp6c odhcp6c; do
        if [ -e "$root/$d/$p" ] || [ -L "$root/$d/$p" ]; then
            fail dhcpv6 "/$d/$p is on the image"
        fi
    done
done
[ -e "$root/usr/share/udhcpc/default6.script" ] && fail dhcpv6 "/usr/share/udhcpc/default6.script is on the image"

# --------------------------------------------------------------- factory
if [ "$kind" = release ] && active "$root/etc/fstab" | awk '$2 ~ /^\/factory\// { f = 1 } END { exit !f }'; then
    fail factory "/etc/fstab mounts a factory slot under /factory/ on a release image"
fi

exit $failed
