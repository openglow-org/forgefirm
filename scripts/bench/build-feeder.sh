#!/bin/bash
# Cross-compiles the bench feeder for the factory board, borrowing the
# Yocto cross toolchain from the ulfius recipe work directory.
#
# Environment (defaults derive from this script's location, assuming
# the standard multi-repo checkout layout):
#   FF_SRC_TOP    checkout holding the sibling repos
#   FF_BUILD_TOP  Yocto build tree (default: $FF_SRC_TOP/forgefirm/build)
set -e
SP="$(cd "$(dirname "$0")" && pwd)"
FF_SRC_TOP="${FF_SRC_TOP:-$(cd "$SP/../../.." && pwd)}"
FF_BUILD_TOP="${FF_BUILD_TOP:-$FF_SRC_TOP/forgefirm/build}"
TC="$FF_BUILD_TOP/tmp/work/cortexa9t2hf-neon-fslc-linux-gnueabi/ulfius/2.7.15"
[ -d "$TC/recipe-sysroot" ] || { echo "toolchain not staged at $TC (run: bitbake ulfius)"; exit 1; }
"$TC/recipe-sysroot-native/usr/bin/arm-fslc-linux-gnueabi/arm-fslc-linux-gnueabi-gcc" \
  --sysroot="$TC/recipe-sysroot" \
  -mthumb -mfpu=neon -mfloat-abi=hard -mcpu=cortex-a9 \
  -D_TIME_BITS=64 -D_FILE_OFFSET_BITS=64 \
  -O2 -Wall -Wextra -o "$SP/feeder" "$SP/feeder.c"
echo FEEDER-OK
