#!/bin/sh
# Copyright 2020-2026 514 LLC d/b/a OpenGlow
# Written by Scott Wiederhold
# https://community.openglow.org
# SPDX-License-Identifier:    MIT
#
# Packs a ForgeFIRM rootfs.ext4 into a fwup .fw archive with the factory
# update task layout: upgrade.a / upgrade.b raw-write the rootfs into eMMC
# slot p1 / p2 (fwup is invoked with -d /dev/mmcblk2p<N>, so offsets are
# partition-relative, exactly like the factory updater).
#
# Usage: mkfw.sh <rootfs.ext4> <version> <output.fw> [private-key]
#   FWUP=<path> overrides the fwup binary (any 1.x; archives verified to
#   apply with the factory's fwup 0.14.2).
#   Unsigned output (no key) is for dev only: release and updater paths
#   require a signature.

set -e

FWUP="${FWUP:-fwup}"
ROOTFS="$1"
VERSION="$2"
OUT="$3"
KEY="$4"

usage () {
  echo "usage: mkfw.sh <rootfs.ext4> <version> <output.fw> [private-key]" >&2
  exit 2
}

[ -n "$ROOTFS" ] && [ -n "$VERSION" ] && [ -n "$OUT" ] || usage
[ -f "$ROOTFS" ] || { echo "ERROR: rootfs '$ROOTFS' not found" >&2; exit 1; }
command -v "$FWUP" >/dev/null || { echo "ERROR: fwup not found (set FWUP=)" >&2; exit 1; }

# The rootfs must fit the 200 MiB factory slot (409600 x 512-byte blocks).
SLOT_BYTES=209715200
SIZE=$(wc -c < "$ROOTFS")
[ "$SIZE" -le "$SLOT_BYTES" ] || {
  echo "ERROR: rootfs is $SIZE bytes; slot holds $SLOT_BYTES" >&2; exit 1; }

WORK=$(mktemp -d)
trap 'rm -rf "$WORK"' EXIT
cp "$ROOTFS" "$WORK/rootfs.ext4"

cat > "$WORK/fwup.conf" <<EOF
meta-product = "ForgeFIRM firmware"
meta-description = "ForgeFIRM firmware for Glowforge brand CNC lasers"
meta-version = "${VERSION}"
meta-platform = "glowforge"
meta-architecture = "arm"
meta-author = "OpenGlow"

file-resource rootfs.ext4 {
    host-path = "${WORK}/rootfs.ext4"
}

task upgrade.a {
    require-unmounted-destination = true
    verify-on-the-fly = true
    on-resource rootfs.ext4 { raw_write(0) }
}

task upgrade.b {
    require-unmounted-destination = true
    verify-on-the-fly = true
    on-resource rootfs.ext4 { raw_write(0) }
}
EOF

"$FWUP" -c -f "$WORK/fwup.conf" -o "$WORK/unsigned.fw"

if [ -n "$KEY" ]; then
  [ -f "$KEY" ] || { echo "ERROR: key '$KEY' not found" >&2; exit 1; }
  "$FWUP" -S -s "$KEY" -i "$WORK/unsigned.fw" -o "$OUT"
  # The post-sign self-check is mandatory: packing without it means a
  # release could ship with a signature nothing ever verified.
  PUB="${KEY%.priv}.pub"
  [ -f "$PUB" ] || { echo "ERROR: public key '$PUB' not found - cannot self-check the signature; refusing to pack unverified" >&2; exit 1; }
  "$FWUP" -V -i "$OUT" -p "$PUB" || { echo "ERROR: signature self-check failed" >&2; exit 1; }
  echo "signed: $OUT"
else
  cp "$WORK/unsigned.fw" "$OUT"
  echo "UNSIGNED (dev only): $OUT"
fi

"$FWUP" -m -i "$OUT" | head -4
