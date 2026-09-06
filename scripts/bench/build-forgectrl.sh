#!/bin/bash
# Cross-compiles forgectrl (the sibling repo) for the factory board,
# borrowing the Yocto cross toolchain + sysroot from the forgectrl
# recipe work directory (which carries ulfius, jansson, gnutls,
# libxcrypt and libjpeg). The newest version directory under the
# recipe's work tree is taken. If that path ages out after a clean,
# regenerate it with: bitbake forgectrl.
#
# Environment (defaults derive from this script's location, assuming
# the standard multi-repo checkout layout):
#   FF_SRC_TOP    checkout holding the sibling repos
#   FF_BUILD_TOP  Yocto build tree (default: $FF_SRC_TOP/forgefirm/build)
set -e
SP="$(cd "$(dirname "$0")" && pwd)"
FF_SRC_TOP="${FF_SRC_TOP:-$(cd "$SP/../../.." && pwd)}"
FF_BUILD_TOP="${FF_BUILD_TOP:-$FF_SRC_TOP/forgefirm/build}"
TCROOT="$FF_BUILD_TOP/tmp/work/cortexa9t2hf-neon-fslc-linux-gnueabi/forgectrl"
TC=$(ls -d "$TCROOT"/*/ 2>/dev/null | sort -V | tail -n 1)
TC="${TC%/}"
[ -n "$TC" ] && [ -d "$TC/recipe-sysroot" ] || { echo "toolchain not staged under $TCROOT (run: bitbake forgectrl)"; exit 1; }
echo "toolchain: $TC"
export PATH="$TC/recipe-sysroot-native/usr/bin:$TC/recipe-sysroot-native/usr/bin/arm-fslc-linux-gnueabi:$PATH"
LOG=$(mktemp -t fcbuild.XXXXXX)
cd "$FF_SRC_TOP/forgectrl"
rm -rf build-arm
cmake -B build-arm \
  -DCMAKE_SYSTEM_NAME=Linux -DCMAKE_SYSTEM_PROCESSOR=arm \
  -DCMAKE_C_COMPILER=arm-fslc-linux-gnueabi-gcc \
  -DCMAKE_BUILD_TYPE=None \
  "-DCMAKE_C_FLAGS=--sysroot=$TC/recipe-sysroot -mthumb -mfpu=neon -mfloat-abi=hard -mcpu=cortex-a9 -D_TIME_BITS=64 -D_FILE_OFFSET_BITS=64 -O2 -g" \
  "-DCMAKE_EXE_LINKER_FLAGS=--sysroot=$TC/recipe-sysroot" \
  > "$LOG" 2>&1
cmake --build build-arm -j8 >> "$LOG" 2>&1 || { tail -30 "$LOG"; exit 1; }
rm -f "$LOG"
echo BUILD-OK
file build-arm/forgectrl 2>/dev/null || ls build-arm/
