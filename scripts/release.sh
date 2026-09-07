#!/bin/bash
# (C) Copyright 2020-2026
# Scott Wiederhold, s.e.wiederhold@gmail.com
# https://community.openglow.org
# SPDX-License-Identifier:    MIT
#
# ForgeFIRM release pipeline (runs on the Yocto build host).
#
#   release.sh <version> [--publish]   full release: gates, build, pack,
#                                      sign, checksums, stage, publish cmd
#                                      (the acceptance gate reads
#                                      releases/v<version>/acceptance.json)
#   release.sh --dev                   build + pack a dev-signed .fw of the dev image for
#                                      the GUI upload path; no staging
#
# Environment:
#   FWUP                 host fwup for packing (default: fwup in PATH)
#   FWUP_COMPAT          factory-era fwup 0.14.2 binary; when set, the
#                        packed archive is verified with it (raw-format
#                        key), replicating the factory-compat guarantee
#   FORGEFIRM_SIGNING_KEY  private key for release mode (REQUIRED - no
#                        default, so key choice is always deliberate)
#   FORGEFIRM_DEV_KEY    private key for --dev mode (REQUIRED for --dev)
#   RELEASE_STAGING_DIR  where release assets are staged
#                        (default: <repo>/release-staging)
#   FORGEFIRM_ACCEPTANCE_SKIP  set to 1 to bypass the acceptance gate
#                        deliberately (never the default; see the site,
#                        Developers, "Acceptance")
#   FORGEFIRM_SOURCE_SKIP  set to 1 to build a release without the source
#                        bundle. The licenses of the software in the image
#                        make source necessary, so this is never the
#                        default.
#
# The source bundle: a release build merges kas/source-bundle.yml, so the
# build writes the source of every recipe of the image beside the image.
# scripts/source-bundle.py packs that source, the license manifests, the
# license texts and the ForgeFIRM layers into
# forgefirm-source-v<version>.tar.gz, and refuses to pack a bundle in which
# a recipe that needs source has none.
#
# Version contract: <version> == FORGEFIRM_RELEASE in forgefirm-image.bb
# == /etc/forgefirm-version ("v<version>") in the built rootfs == .fw
# meta-version ("v<version>") == release tag ("v<version>").

set -euo pipefail

REPO="$(cd "$(dirname "$0")/.." && pwd)"
DEPLOY_ROOT="$REPO/build/tmp/deploy"
DEPLOY="$DEPLOY_ROOT/images/glowforge"
IMAGE_BB="$REPO/meta-forgefirm/recipes-forgefirm/images/forgefirm-image.bb"
INSTALLER="$REPO/scripts/install-forgefirm.sh"
WARN_BYTES=$((170 * 1024 * 1024))
FAIL_BYTES=$((195 * 1024 * 1024))

die () { echo "RELEASE FAILED: $*" >&2; exit 1; }
warn () { echo "WARNING: $*" >&2; }

VERSION=""
MODE=release
PUBLISH=0
for ARG in "$@"; do
  case "$ARG" in
    --dev) MODE=dev ;;
    --publish) PUBLISH=1 ;;
    -*) die "unknown option $ARG" ;;
    *)
      [ -z "$VERSION" ] \
        || die "multiple versions given ('$VERSION' and '$ARG')"
      VERSION="$ARG"
      ;;
  esac
done

FWUP="${FWUP:-fwup}"
command -v "$FWUP" >/dev/null || die "fwup not found (set FWUP=)"
command -v kas >/dev/null || die "kas not found on PATH"

# The source bundle belongs to a release. A --dev archive goes to one bench
# and publishes nothing, so it builds without the archiver.
SOURCE_BUNDLE=0
if [ "$MODE" = release ] && [ -z "${FORGEFIRM_SOURCE_SKIP:-}" ]; then
  SOURCE_BUNDLE=1
fi

build_images () {
  CFG="kas/forgefirm-glowforge.yml"
  TARGETS="forgefirm-image forgefirm-image-dev"
  if [ "$SOURCE_BUNDLE" = 1 ]; then
    # The archiver rides the release build, so the source that the bundle
    # publishes is the source that this image is built from. The overlay
    # adds tasks and nothing else: no file of the root filesystem and no
    # component changes, so the image manifest and the acceptance result
    # are the same with it and without it. The boot loader, the kernel and
    # the kernel module reach the machine outside the root filesystem, so
    # they are named as targets as well (scripts/source-bundle.py,
    # BUILD_TARGETS).
    CFG="$CFG:kas/source-bundle.yml"
    TARGETS="$TARGETS u-boot virtual/kernel kernel-module-glowforge"
  fi
  echo "== building images ($CFG) =="
  ( cd "$REPO" && kas shell "$CFG" -c "bitbake $TARGETS" ) \
    || die "bitbake failed"
}

resolve_ext4 () {
  EXT4=$(readlink -f "$DEPLOY/forgefirm-image-glowforge.rootfs.ext4")
  [ -s "$EXT4" ] || die "release ext4 not found in $DEPLOY"
}

check_size () {
  SZ=$(stat -c%s "$EXT4")
  [ "$SZ" -lt "$FAIL_BYTES" ] \
    || die "rootfs is $SZ bytes - too close to the 200 MiB slot"
  if [ "$SZ" -ge "$WARN_BYTES" ]; then
    warn "rootfs is $((SZ / 1048576)) MiB - $(( (FAIL_BYTES - SZ) / 1048576 )) MiB of margin left before the release gate"
  fi
}

# --- dev mode -----------------------------------------------------------------
if [ "$MODE" = "dev" ]; then
  KEY="${FORGEFIRM_DEV_KEY:?set FORGEFIRM_DEV_KEY to the dev signing key}"
  build_images
  resolve_ext4
  # The dev archive carries the dev image (forgetest, the bench tools), not
  # the release rootfs: what the panel's upload path installs on the bench
  # is what the bench runs.
  EXT4="${EXT4/forgefirm-image-glowforge/forgefirm-image-dev-glowforge}"
  [ -f "$EXT4" ] || die "dev rootfs not found: $EXT4"
  check_size
  REL=$(sed -n 's/^FORGEFIRM_RELEASE ?= "\(.*\)"/\1/p' "$IMAGE_BB")
  DEVVER="v${REL}-dev-$(date +%Y%m%d%H%M%S)"
  OUT="$DEPLOY/forgefirm-dev.fw"
  "$REPO/scripts/mkfw.sh" "$EXT4" "$DEVVER" "$OUT" "$KEY"
  echo "== dev archive ready: $OUT ($DEVVER) =="
  exit 0
fi

# --- release mode -------------------------------------------------------------
[ -n "$VERSION" ] || die "usage: release.sh <version> [--publish] | release.sh --dev"
KEY="${FORGEFIRM_SIGNING_KEY:?set FORGEFIRM_SIGNING_KEY to the release signing key}"
PUB="${KEY%.priv}.pub"
[ -f "$KEY" ] || die "signing key '$KEY' not found"
[ -f "$PUB" ] || die "public key '$PUB' not found"

echo "== gates =="

# Repo state (informational when the build tree is not a git checkout).
if git -C "$REPO" rev-parse --git-dir >/dev/null 2>&1; then
  [ -z "$(git -C "$REPO" status --porcelain)" ] \
    || die "working tree is dirty - release from a clean tree"
  if ! git -C "$REPO" diff --quiet "@{upstream}" 2>/dev/null; then
    warn "HEAD differs from upstream - push before publishing"
  fi
else
  warn "$REPO is not a git checkout - repo-state gates skipped (rsynced build tree)"
fi

# Version single-source check.
BB_REL=$(sed -n 's/^FORGEFIRM_RELEASE ?= "\(.*\)"/\1/p' "$IMAGE_BB")
[ "$BB_REL" = "$VERSION" ] \
  || die "FORGEFIRM_RELEASE in forgefirm-image.bb is '$BB_REL', not '$VERSION'"

# The beta rule: every release below 0.1.0 is a beta, and 0.1.0 is the
# first release that is not. While the README carries the beta banner, a
# version at or above 0.1.0 is a mistake, not a release.
if grep -q 'ForgeFIRM is in beta' "$REPO/README.md"; then
  MAJOR=${VERSION%%.*}; REST=${VERSION#*.}; MINOR=${REST%%.*}
  case "$MAJOR.$MINOR" in
    0.0) ;;
    *) die "version $VERSION is not a beta number, and the README says ForgeFIRM is in beta (0.0.x only)" ;;
  esac
fi

# The installer must embed the pubkey matching the signing key, or every
# install will refuse the published archive.
INST_HEX=$(sed -n "s/^PUBKEY='\(.*\)'$/\1/p" "$INSTALLER" | tr -d '\\x')
KEY_HEX=$(base64 -d "$PUB" | xxd -p | tr -d '\n')
[ -n "$INST_HEX" ] || die "cannot extract the embedded pubkey from the installer"
[ "$INST_HEX" = "$KEY_HEX" ] \
  || die "installer's embedded pubkey does not match the signing key - update install-forgefirm.sh"

build_images
resolve_ext4
check_size

# Rootfs version stamp.
STAMP=$(debugfs -R "cat /etc/forgefirm-version" "$EXT4" 2>/dev/null)
[ "$STAMP" = "v$VERSION" ] \
  || die "rootfs stamp is '$STAMP', expected 'v$VERSION'"

# Acceptance gate: the committed acceptance artifact must authorize THIS
# build. scripts/acceptance-gate.py recomputes every catalog test's domain
# fingerprint from the manifest inside the release rootfs and requires the
# recorded PASS to match (https://docs.forgefirm.org/developers/acceptance/).
# A release is never signed
# without it; FORGEFIRM_ACCEPTANCE_SKIP=1 bypasses deliberately and loudly.
ART="$REPO/releases/v$VERSION/acceptance.json"
GATED=1
if [ -n "${FORGEFIRM_ACCEPTANCE_SKIP:-}" ]; then
  warn "acceptance gate SKIPPED by FORGEFIRM_ACCEPTANCE_SKIP - this release carries no acceptance proof"
  GATED=0
else
  [ -f "$ART" ] \
    || die "no acceptance artifact at releases/v$VERSION/acceptance.json - run the campaign on the bench, export, commit"
  REL_MANIFEST=$(mktemp)
  debugfs -R "cat /etc/forgefirm-manifest.json" "$EXT4" > "$REL_MANIFEST" 2>/dev/null
  [ -s "$REL_MANIFEST" ] \
    || { rm -f "$REL_MANIFEST"; die "release rootfs carries no /etc/forgefirm-manifest.json"; }
  python3 "$REPO/scripts/acceptance-gate.py" "$ART" "$REL_MANIFEST" --machine glowforge \
    || { rm -f "$REL_MANIFEST"; die "acceptance gate refused this build (see the table above)"; }
  rm -f "$REL_MANIFEST"
  echo "acceptance gate OK ($ART)"
fi

# Root policy gate. The release image ships root WITHOUT a password (the
# serial console is the recovery path) and sshd refuses root and empty
# passwords: PermitRootLogin no and PermitEmptyPasswords no must be
# active (uncommented) in the built sshd_config, and root's shadow field
# must be empty. Read the built files, not the recipes: this catches a
# drift however it got in (recipe, local.conf, an inherited class).
SSHD_CONFIG=$(debugfs -R "cat /etc/ssh/sshd_config" "$EXT4" 2>/dev/null)
[ -n "$SSHD_CONFIG" ] \
  || die "release rootfs carries no /etc/ssh/sshd_config"
printf '%s\n' "$SSHD_CONFIG" | grep -Eq '^PermitRootLogin[[:space:]]+no[[:space:]]*$' \
  || die "release sshd_config has no active 'PermitRootLogin no' (recipes-connectivity/openssh drift?)"
printf '%s\n' "$SSHD_CONFIG" | grep -Eq '^PermitEmptyPasswords[[:space:]]+no[[:space:]]*$' \
  || die "release sshd_config has no active 'PermitEmptyPasswords no' (recipes-connectivity/openssh drift?)"
ROOT_SHADOW=$(debugfs -R "cat /etc/shadow" "$EXT4" 2>/dev/null \
              | awk -F: '$1=="root"{print; exit}')
[ -n "$ROOT_SHADOW" ] \
  || die "release rootfs has no root entry in /etc/shadow"
ROOT_PW=$(printf '%s\n' "$ROOT_SHADOW" | awk -F: '{print $2}')
[ -z "$ROOT_PW" ] \
  || die "release rootfs has a non-empty root password field: the policy is an empty field (empty-root-password in forgefirm-image.bb); a build drift"
echo "root policy gate OK (root field empty; sshd refuses root and empty passwords)"

# Config-level guard: debug-tweaks must not sit in the shared kas config,
# where it would apply to every target including the release image.
if ( cd "$REPO" && kas dump kas/forgefirm-glowforge.yml 2>/dev/null ) \
     | grep -q 'debug-tweaks'; then
  die "debug-tweaks appears in the resolved kas config - it must live only in forgefirm-image-dev.bb"
fi
echo "kas config gate OK (no debug-tweaks in the shared config)"

echo "== pack + sign =="
STAGE="${RELEASE_STAGING_DIR:-$REPO/release-staging}/v$VERSION"
mkdir -p "$STAGE"
"$REPO/scripts/mkfw.sh" "$EXT4" "v$VERSION" "$STAGE/forgefirm.fw" "$KEY"

# Factory-era compat verification (fwup 0.14.2 wants raw 32-byte keys).
if [ -n "${FWUP_COMPAT:-}" ]; then
  RAW=$(mktemp)
  base64 -d "$PUB" > "$RAW"
  "$FWUP_COMPAT" -V -i "$STAGE/forgefirm.fw" -p "$RAW" \
    || { rm -f "$RAW"; die "factory-era fwup rejects the archive"; }
  rm -f "$RAW"
  echo "factory-era fwup verification OK"
elif [ "$MODE" = release ] && [ -z "${FWUP_COMPAT_SKIP:-}" ]; then
  # The factory-compat guarantee is a release property: a public release
  # must not skip it silently. FWUP_COMPAT_SKIP=1 bypasses deliberately.
  die "FWUP_COMPAT not set - factory-era verification is required for a release (set FWUP_COMPAT_SKIP=1 to bypass deliberately)"
else
  warn "FWUP_COMPAT not set - factory-era verification skipped"
fi

echo "== stage assets =="
cp -L "$DEPLOY/forgefirm-image-glowforge.rootfs.wic.gz" "$STAGE/forgefirm-image-glowforge.rootfs.wic.gz"
# The acceptance artifact travels with the release (see the site,
# Developers, "Acceptance") - only when the gate accepted it for THIS
# rootfs. A skipped gate ships no artifact: an acceptance.json next to a
# rootfs it never authorized would read as proof. The release says so
# instead, and goes out as a prerelease.
ASSETS="forgefirm.fw sha256sums.txt forgefirm-image-glowforge.rootfs.wic.gz"
PRERELEASE=""
rm -f "$STAGE/acceptance.json" "$STAGE/acceptance.md" "$STAGE/NO-ACCEPTANCE.txt"
if [ "$GATED" = 1 ] && [ -f "$ART" ]; then
  cp "$ART" "$STAGE/acceptance.json"
  ASSETS="$ASSETS acceptance.json"
  if [ -f "${ART%.json}.md" ]; then
    cp "${ART%.json}.md" "$STAGE/acceptance.md"
    ASSETS="$ASSETS acceptance.md"
  fi
else
  cat > "$STAGE/NO-ACCEPTANCE.txt" <<NOTE
ForgeFIRM v$VERSION was signed with the acceptance gate skipped
(FORGEFIRM_ACCEPTANCE_SKIP). No acceptance campaign authorized this
rootfs. Treat it as a prerelease.
NOTE
  ASSETS="$ASSETS NO-ACCEPTANCE.txt"
  PRERELEASE="--prerelease"
fi
# The source bundle. It is packed from the license manifests of THIS
# rootfs, and source-bundle.py stops the release when a recipe of the image
# has no source (see the site, Developers, "Release flow").
rm -f "$STAGE"/forgefirm-source-v*.tar.gz
if [ "$SOURCE_BUNDLE" = 1 ]; then
  echo "== source bundle =="
  python3 "$REPO/scripts/source-bundle.py" "$VERSION" \
      --deploy "$DEPLOY_ROOT" --image-name "$(basename "$EXT4" .ext4)" \
      --out "$STAGE" \
    || die "the source bundle failed"
  ASSETS="$ASSETS forgefirm-source-v$VERSION.tar.gz"
else
  warn "source bundle SKIPPED by FORGEFIRM_SOURCE_SKIP - this release publishes no source"
fi

# Every attached file is bound to the release by the sums, the artifact
# included.
( cd "$STAGE" && sha256sum $(echo "$ASSETS" | tr ' ' '\n' | grep -v '^sha256sums.txt$') > sha256sums.txt )
ls -la "$STAGE"

cat <<EOF

== release v$VERSION staged ==

Pre-publish checklist (docs.forgefirm.org, Developers, "Release flow"):
  - meta-openglow pushed; kas config flipped to the pinned-remote block
  - kas lock refreshed
  - self-containment proven from a fresh clone

Publish (from a directory with an authenticated gh):
  cd "$STAGE"
  gh release create "v$VERSION" --repo openglow-org/forgefirm \\
    --title "ForgeFIRM v$VERSION" --generate-notes $PRERELEASE \\
    $ASSETS
EOF

if [ "$PUBLISH" = "1" ]; then
  command -v gh >/dev/null || die "--publish requested but gh is not on PATH"
  ( cd "$STAGE" && gh release create "v$VERSION" --repo openglow-org/forgefirm \
      --title "ForgeFIRM v$VERSION" --generate-notes $PRERELEASE \
      $ASSETS ) \
    || die "gh release create failed"
  echo "== published v$VERSION =="
fi
