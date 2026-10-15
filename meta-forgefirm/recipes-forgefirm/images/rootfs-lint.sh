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

exit $failed
