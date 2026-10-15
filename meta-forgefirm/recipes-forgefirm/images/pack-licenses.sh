#!/bin/sh
# Copyright 2026 514 LLC d/b/a OpenGlow
# Written by Scott Wiederhold
# https://community.openglow.org
# SPDX-License-Identifier:    MIT
#
# pack-licenses.sh <common-licenses directory> <tar.gz>
#
# Packs the image's license tree into one reproducible tar.gz (sorted
# names, no timestamps, no owners), its members under the tree's own
# name, and removes the tree. It refuses, before it writes the tar or
# removes anything, a tree that is missing, a tree with no
# license.manifest, and a manifest that does not name the project's own
# recipes: forgectrl, grblhal-glowforge and kernel-module-glowforge.
# forgefirm-image.bb's pack_licenses runs it; forgetest's test_image.py
# runs it on a fixture.

dir=${1:?common-licenses directory}
out=${2:?output tar.gz}
fail() { echo "pack-licenses: $*" >&2; exit 1; }

[ -d "$dir" ] || fail "$dir is missing (COPY_LIC_DIRS off?)"
manifest=$dir/license.manifest
[ -f "$manifest" ] || fail "no license.manifest in $dir"
# A line counts with its newline: an unterminated last line is not read.
lines=$(wc -l < "$manifest")
for name in forgectrl grblhal-glowforge kernel-module-glowforge; do
    head -n "$lines" "$manifest" | grep -Fxq "RECIPE NAME: $name" \
        || fail "$manifest does not name the recipe $name"
done

base=$(basename "$dir")
mkdir -p "$(dirname "$out")" || fail "cannot make the directory of $out"
tar -C "$(dirname "$dir")" --sort=name --mtime=@0 --owner=0 --group=0 \
    --numeric-owner -cf - "$base" | gzip -9 -n > "$out" || fail "cannot write $out"
# the tree goes only once the bundle holds its manifest
tar -tzf "$out" "$base/license.manifest" > /dev/null 2>&1 \
    || fail "$out holds no $base/license.manifest"
rm -rf "$dir"
