#!/bin/bash
# Copyright 2020-2026 514 LLC d/b/a OpenGlow
# Written by Scott Wiederhold
# https://community.openglow.org
# SPDX-License-Identifier:    MIT
#
# ForgeFIRM release pipeline (runs on the Yocto build host).
#
#   release.sh <version> [--publish]   the staging run: gates, build, the
#                                      rootfs gates, and the unsigned release
#                                      staged for the sign step (the
#                                      acceptance gate reads
#                                      releases/v<version>/acceptance.json).
#                                      No private key is read.
#   release-sign.sh <version>          the sign step (scripts/release-sign.sh):
#                                      the only step that holds a private
#                                      key; it checks the staged rootfs by
#                                      hash, signs, and prints the publish
#                                      commands
#   release.sh --dev                   build + pack a dev-signed .fw of the dev image for
#                                      the GUI upload path; no staging
#
# Environment:
#   FWUP                 host fwup for packing in --dev mode (default: fwup
#                        in PATH)
#   FORGEFIRM_DEV_KEY    private key for --dev mode (REQUIRED there; never
#                        the release key). The release's keys are the sign
#                        step's.
#   RELEASE_STAGING_DIR  where release assets are staged
#                        (default: <repo>/release-staging)
#   FORGEFIRM_ACCEPTANCE_SKIP  set to 1 to bypass the acceptance gate
#                        deliberately (never the default; see the site,
#                        Developers, "Acceptance"). The sign step then signs
#                        forgefirm.fw with the dev key, which no machine
#                        trusts, so it installs only with the machine button
#                        held; the staged release is marked a prerelease and
#                        carries NO-ACCEPTANCE.txt. REFUSED with --publish.
#   FORGEFIRM_SOURCE_SKIP  set to 1 to build a release without the source
#                        bundle. The licenses of the software in the image
#                        make source necessary, so this is never the
#                        default, and it is REFUSED with --publish: a
#                        release that conveys the binaries without the
#                        corresponding source is a license violation, which
#                        no release flag cures. Building without publishing
#                        is allowed; the staged release is marked a
#                        prerelease and carries NO-SOURCE.txt.
#   FORGEFIRM_DOCS_DIR   the forgefirm-docs checkout to tag with this
#                        release (default: <repo>/../forgefirm-docs). The
#                        firmware and the documentation that describes it
#                        share a tag, so the documentation that agrees with
#                        a machine can be found from its version.
#   FORGEFIRM_DOCS_SKIP  set to 1 to release without tagging the
#                        documentation. Never the default.
#
# A skip variable takes the value 1 and no other: any other value stops the
# script, so a 0 never skips.
#
# The source bundle: a release build merges kas/source-bundle.yml, so the
# build writes the source of every recipe of both images beside the images.
# The deploy directory's sources/ is emptied first, so an archive an earlier
# build left cannot answer for a recipe. scripts/source-bundle.py packs that
# source, the license manifests, the license texts and the ForgeFIRM layers
# into forgefirm-source-v<version>.tar.gz, and refuses to pack a bundle in
# which a recipe that needs source has none.
#
# Version contract: <version> (a.b.c) == FORGEFIRM_RELEASE in
# forgefirm-release.inc == /etc/forgefirm-version ("v<version>") in the
# built rootfs == .fw meta-version ("v<version>") == release tag
# ("v<version>"). With --publish the tag must exist and name HEAD: the
# release is attached to it, and gh creates none (--verify-tag).
#
# Keys: the release key's public half is the one the image trusts
# (meta-forgefirm/recipes-forgefirm/forgefirm-keys/files/
# forgefirm-release.pub), and the installer must embed the same key. The
# private keys live off the build host, except while the sign step runs,
# and only the sign step reads them.
#
# Staging: the assets are made in <staging>/v<version>.tmp, with copies of
# the two root filesystems under sign/ and the request SIGN-REQUEST (the
# commit built, the artifact the gate accepted, the sha256 of each rootfs
# copy and of every staged file, what to sign and publish). Every gate reads
# those copies. The sign step holds the request to its own checkout and
# runs the acceptance gate again before it reads a key, and renames the
# directory to <staging>/v<version> once it has signed, so a release that a
# gate or the sign step stopped leaves nothing where the publish command
# looks.

set -euo pipefail

REPO="$(cd "$(dirname "$0")/.." && pwd)"
DEPLOY_ROOT="$REPO/build/tmp/deploy"
DEPLOY="$DEPLOY_ROOT/images/glowforge"
IMAGE_BB="$REPO/meta-forgefirm/recipes-forgefirm/images/forgefirm-image.bb"
# The release version, in its own file so a bump is not a platform change
# (the manifest leaves it out of the layer content hash).
RELEASE_INC="$REPO/meta-forgefirm/recipes-forgefirm/images/forgefirm-release.inc"
INSTALLER="$REPO/scripts/install-forgefirm.sh"
WARN_BYTES=$((170 * 1024 * 1024))
FAIL_BYTES=$((195 * 1024 * 1024))
# The names the staged root filesystems take under sign/.
REL_ROOTFS=forgefirm-image-glowforge.rootfs.ext4
DEV_ROOTFS=forgefirm-image-dev-glowforge.rootfs.ext4

die () { echo "RELEASE FAILED: $*" >&2; exit 1; }
warn () { echo "WARNING: $*" >&2; }

# A staging run that stops before it has written the sign request leaves
# nothing staged: no copies of the root filesystems, no assets.
STAGE=""
REL_MANIFEST=""
cleanup () {
  RC=$?
  [ -z "$REL_MANIFEST" ] || rm -f "$REL_MANIFEST"
  if [ "$RC" != 0 ] && [ -n "$STAGE" ] && [ ! -f "$STAGE/SIGN-REQUEST" ]; then
    rm -rf "$STAGE"
  fi
}
trap cleanup EXIT

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

# A skip is the value 1, or nothing: a 0, a "no" or a typo is a mistake,
# never a skip.
for SKIP in FORGEFIRM_ACCEPTANCE_SKIP FORGEFIRM_SOURCE_SKIP FORGEFIRM_DOCS_SKIP; do
  case "${!SKIP:-}" in
    ""|1) ;;
    *) die "$SKIP='${!SKIP}': a skip takes the value 1 and no other" ;;
  esac
done

# The source bundle belongs to a release. A --dev archive goes to one bench
# and publishes nothing, so it builds without the archiver.
SOURCE_BUNDLE=0
if [ "$MODE" = release ] && [ "${FORGEFIRM_SOURCE_SKIP:-}" != 1 ]; then
  SOURCE_BUNDLE=1
fi
GATED=1
[ "${FORGEFIRM_ACCEPTANCE_SKIP:-}" = 1 ] && GATED=0

# Decided first, and refused before any tool is looked for, because they
# need none. Conveying the GPL binaries of this image with no corresponding
# source is a license violation, and no release flag cures one. A rootfs no
# campaign authorized is signed with the dev key and never published.
if [ "$MODE" = release ] && [ "$PUBLISH" = 1 ]; then
  [ "$SOURCE_BUNDLE" = 1 ] \
    || die "FORGEFIRM_SOURCE_SKIP cannot be combined with --publish: a published release must carry its source"
  [ "$GATED" = 1 ] \
    || die "FORGEFIRM_ACCEPTANCE_SKIP cannot be combined with --publish: a published release is authorized by its campaign"
fi

command -v kas >/dev/null || die "kas not found on PATH"

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
  # A release makes both images, and the acceptance tool's entry, again
  # from the checkout as it stands: the manifest records the state of the
  # layers, the kas configuration and the acceptance tool's sources when
  # they are made, and no task signature moves when a change is committed
  # unchanged, so an image an earlier build left (or its shared state)
  # would carry the dirty marks of that build into the release check.
  BUILD="bitbake $TARGETS"
  if [ "$MODE" = release ]; then
    BUILD="bitbake -c cleansstate forgefirm-image forgefirm-image-dev forgetest && $BUILD"
  fi
  echo "== building images ($CFG) =="
  ( cd "$REPO" && kas shell "$CFG" -c "$BUILD" ) \
    || die "bitbake failed"
}

resolve_ext4 () {
  EXT4=$(readlink -f "$DEPLOY/forgefirm-image-glowforge.rootfs.ext4")
  [ -s "$EXT4" ] || die "release ext4 not found in $DEPLOY"
}

# The dev rootfs, through the dev image's own deploy link. Each image is
# stamped when it is itself made, so neither name follows from the other.
resolve_dev_ext4 () {
  DEV_EXT4=$(readlink -f "$DEPLOY/forgefirm-image-dev-glowforge.rootfs.ext4")
  [ -s "$DEV_EXT4" ] || die "dev rootfs not found in $DEPLOY"
}

# The two rootfs images are one build: what binds them is the manifest each
# carries (/etc/forgefirm-manifest.json), not a file name. Their identities
# must be equal: every component at its revision with its files, the dev
# image's own components left out, plus the platform
# (forgetest/forgetest/manifest.py, identity_sha). A dev rootfs left by an
# earlier build of other sources stops the release here, before
# forgefirm-dev.fw and the source bundle would speak for it.
same_build () {
  REL_M=$(mktemp)
  DEV_M=$(mktemp)
  debugfs -R "cat /etc/forgefirm-manifest.json" "$1" > "$REL_M" 2>/dev/null || true
  debugfs -R "cat /etc/forgefirm-manifest.json" "$2" > "$DEV_M" 2>/dev/null || true
  SAME=0
  python3 -B -c '
import sys
sys.path.insert(0, sys.argv[1])
from forgetest.manifest import Manifest
a, b = (Manifest.load(p).identity_sha() for p in sys.argv[2:4])
sys.exit(0 if a == b else 1)' "$REPO/forgetest" "$REL_M" "$DEV_M" || SAME=1
  rm -f "$REL_M" "$DEV_M"
  [ "$SAME" = 0 ] \
    || die "the dev rootfs $(basename "$DEV_EXT4") is not the build of the release rootfs $(basename "$EXT4"): their manifests differ, or one carries none"
}

# A release is built from its pins as they are committed: the manifest of
# the rootfs records no component built from a local checkout or from a
# working tree that differs from its commit, no layer and no kas file that
# differs from its commit, and no debug kernel (forgetest/forgetest/
# provenance.py, manifest_reasons). forgetest never lets a PASS recorded
# on such a build be inherited, and a release is no different.
pins_only () {
  PM=$(mktemp)
  debugfs -R "cat /etc/forgefirm-manifest.json" "$1" > "$PM" 2>/dev/null || true
  WHY=$(python3 -B -c '
import sys
sys.path.insert(0, sys.argv[1])
from forgetest.manifest import Manifest
from forgetest.provenance import manifest_reasons
print("; ".join(manifest_reasons(Manifest.load(sys.argv[2]))))' "$REPO/forgetest" "$PM" 2>&1) \
    || WHY="its manifest cannot be read: $WHY"
  rm -f "$PM"
  [ -z "$WHY" ] \
    || die "the rootfs $2 is not built from its pins: $WHY"
}

# The dev rootfs is sized and capped like the release one, so both take
# the same slot size gate.
check_size () {
  SZ=$(stat -c%s "$1")
  [ "$SZ" -lt "$FAIL_BYTES" ] \
    || die "rootfs $(basename "$1") is $SZ bytes - too close to the 200 MiB slot"
  if [ "$SZ" -ge "$WARN_BYTES" ]; then
    warn "rootfs $(basename "$1") is $((SZ / 1048576)) MiB - $(( (FAIL_BYTES - SZ) / 1048576 )) MiB of margin left before the release gate"
  fi
}

# The release public key every machine trusts (the image carries it). A dev
# signing key is refused when its public half is this one: an image with an
# open root login is never one a release-key signature vouches for.
TRUSTED_PUB="$REPO/meta-forgefirm/recipes-forgefirm/forgefirm-keys/files/forgefirm-release.pub"

# The key a public key file holds, as hex. Keys are compared by their bytes:
# two files that hold one key differ as text by a line ending. A file that
# does not hold a 32-byte key stops the script.
key_hex () {
  HEX=$(tr -d '[:space:]' < "$1" | base64 -d 2>/dev/null | xxd -p | tr -d '\n') || HEX=""
  [ "${#HEX}" = 64 ] || die "'$1' does not hold a 32-byte public key"
  printf '%s' "$HEX"
}

refuse_release_key () {
  [ -f "$TRUSTED_PUB" ] || die "the trusted release key '$TRUSTED_PUB' is missing"
  [ -f "$1" ] || die "dev public key '$1' not found"
  A=$(key_hex "$1") || exit 1
  B=$(key_hex "$TRUSTED_PUB") || exit 1
  [ "$A" != "$B" ] \
    || die "the dev key is the release key - forgefirm-dev.fw must not carry the release signature"
}

# --- dev mode -----------------------------------------------------------------
if [ "$MODE" = "dev" ]; then
  FWUP="${FWUP:-fwup}"
  command -v "$FWUP" >/dev/null || die "fwup not found (set FWUP=)"
  KEY="${FORGEFIRM_DEV_KEY:?set FORGEFIRM_DEV_KEY to the dev signing key}"
  [ -f "$KEY" ] || die "dev signing key '$KEY' not found"
  refuse_release_key "${KEY%.priv}.pub"
  build_images
  resolve_ext4
  # The dev archive carries the dev image (forgetest, the bench tools), not
  # the release rootfs: what the bench installs is what the bench runs.
  resolve_dev_ext4
  check_size "$DEV_EXT4"
  REL=$(sed -n 's/^FORGEFIRM_RELEASE ?= "\(.*\)"/\1/p' "$RELEASE_INC")
  DEVVER="v${REL}-dev-$(date +%Y%m%d%H%M%S)"
  OUT="$DEPLOY/forgefirm-dev.fw"
  "$REPO/scripts/mkfw.sh" "$DEV_EXT4" "$DEVVER" "$OUT" "$KEY"
  echo "== dev archive ready: $OUT ($DEVVER) =="
  exit 0
fi

# --- release mode: the staging run ---------------------------------------------
[ -n "$VERSION" ] || die "usage: release.sh <version> [--publish] | release.sh --dev"
# The version names directories that this run empties and a tag, so it is a
# version and nothing else.
[[ "$VERSION" =~ ^[0-9]+\.[0-9]+\.[0-9]+$ ]] \
  || die "'$VERSION' is not a version (a.b.c)"
# The installer must embed the release key the image trusts, or every
# install would refuse the published archive. Both are files of this tree:
# no private key is read here.
[ -f "$TRUSTED_PUB" ] || die "the trusted release key '$TRUSTED_PUB' is missing"
TRUSTED_HEX=$(key_hex "$TRUSTED_PUB") || exit 1
INST_HEX=$(sed -n "s/^PUBKEY='\(.*\)'$/\1/p" "$INSTALLER" | tr -d '\\x')
[ -n "$INST_HEX" ] || die "cannot extract the embedded pubkey from the installer"
[ "$INST_HEX" = "$TRUSTED_HEX" ] \
  || die "installer's embedded pubkey is not the release key the image trusts - update install-forgefirm.sh"

echo "== gates =="

# Repo state. A release builds from a clean git checkout of its release
# branch: what it builds must be the pushed, clean commit its tag names, or
# the tag and the binaries disagree and an untracked file (a bench tool,
# say) rides into the dev image. Commits are compared, not trees: HEAD must
# be the very commit its upstream names, so a commit that was never pushed
# is refused even when it changes no file.
git -C "$REPO" rev-parse --git-dir >/dev/null 2>&1 \
  || die "$REPO is not a git checkout - a release builds from a clean checkout of its release branch"
# What the release reads must be the commit, file for file: the layer
# (bitbake reads every file its globs match), the kas configuration, the
# acceptance tool and the bench tools the dev image takes from this
# checkout, the release scripts, and the release records the gates read
# (the artifact, the allowed signers). Only the checkout's own .gitignore
# files may leave a file out there: a local exclude (.git/info/exclude,
# the user's global excludes file) hides a file from git status, and the
# image manifest counts it as content. Nor may a tracked file carry an
# edit the index is told to ignore (skip-worktree, assume-unchanged),
# which git status does not report. Elsewhere in the checkout (a tool's
# output directory at the top) nothing is read. Checked before the build
# and again after it, with the commit and the tag.
RELEASE_READS="meta-forgefirm kas forgetest scripts releases"
clean_checkout () {
  [ -z "$(git -C "$REPO" status --porcelain)" ] \
    || die "working tree is dirty - release from a clean tree"
  HIDDEN=$(git -C "$REPO" -c core.excludesFile=/dev/null ls-files --others --exclude-per-directory=.gitignore -- $RELEASE_READS)
  [ -z "$HIDDEN" ] \
    || die "untracked files that only a local exclude hides (${HIDDEN%%$'\n'*} ...) - commit or remove them"
  UNSEEN=$(git -C "$REPO" ls-files -v -- $RELEASE_READS | grep -E '^[a-zS] ' || true)
  [ -z "$UNSEEN" ] \
    || die "tracked files whose changes the index ignores (${UNSEEN%%$'\n'*} ...) - clear skip-worktree and assume-unchanged"
}
clean_checkout
# The commit this run builds, and records in the sign request.
BUILT_REV=$(git -C "$REPO" rev-parse HEAD)
# Every staging run builds a pushed commit: the release-branch check and
# the host-test gate (whose CI run exists only for a pushed commit) refuse
# any other, and this says so first.
UP_REV=$(git -C "$REPO" rev-parse -q --verify "@{upstream}^{commit}" 2>/dev/null) || UP_REV=
[ -n "$UP_REV" ] && [ "$UP_REV" = "$BUILT_REV" ] \
  || die "HEAD is not the commit its upstream names - push it, and wait for its CI, before a release"
# The release is attached to the tag v<version>, and the tag must name the
# commit this build is made from. Checked here, before the build: gh is told
# to create no tag of its own (--verify-tag).
if [ "$PUBLISH" = 1 ]; then
  TAG_REV=$(git -C "$REPO" rev-parse -q --verify "v$VERSION^{commit}" 2>/dev/null) \
    || die "no tag v$VERSION in $REPO - tag the release commit before publishing"
  [ "$TAG_REV" = "$BUILT_REV" ] \
    || die "tag v$VERSION names $TAG_REV, not HEAD - the release must be built from the commit its tag names"
fi
# The checkout as the build left it: still the clean commit it was built
# from, and with --publish still the commit the tag names. A commit, a
# moved tag or an edit made while the build ran would put the release
# under a tag whose tree is not the one built.
same_checkout () {
  [ "$(git -C "$REPO" rev-parse HEAD)" = "$BUILT_REV" ] \
    || die "the forgefirm checkout moved $1: HEAD is no longer $BUILT_REV, the commit built - stage again"
  clean_checkout
  if [ "$PUBLISH" = 1 ]; then
    [ "$(git -C "$REPO" rev-parse -q --verify "v$VERSION^{commit}" 2>/dev/null)" = "$BUILT_REV" ] \
      || die "the tag v$VERSION moved $1: it no longer names $BUILT_REV, the commit built - stage again"
  fi
}

# A release builds only from its release branches, as GitHub holds them:
# HEAD on forgefirm's master, each component pin on its repository's
# release branch, the kas lock's meta-openglow commit on scarthgap, and the
# recipes and the kas configuration naming those branches
# (scripts/release-branch-check.py, every check). Run before the tag is
# pushed, so a release that would build from anything else is stopped
# before it has a tag on GitHub.
python3 "$REPO/scripts/release-branch-check.py" --rev HEAD \
  || die "release-branch-check refused this build (see the table above) - a release builds only from its release branches"

# The documentation of a release is the docs' next branch, which stays
# local until the release is published and is then merged into main by
# fast-forward: so the checkout is clean and on next, main on GitHub is an
# ancestor of it, and a tag v<version> an earlier run made names it. Checked
# before the build; the tag itself is made once every gate has passed
# (below).
if [ "${FORGEFIRM_DOCS_SKIP:-}" != 1 ]; then
  DOCS_DIR="${FORGEFIRM_DOCS_DIR:-$REPO/../forgefirm-docs}"
  [ -d "$DOCS_DIR/.git" ] || [ -f "$DOCS_DIR/.git" ] \
    || die "no forgefirm-docs checkout at $DOCS_DIR (set FORGEFIRM_DOCS_DIR, or FORGEFIRM_DOCS_SKIP=1 to release without one)"
  [ -z "$(git -C "$DOCS_DIR" status --porcelain)" ] \
    || die "forgefirm-docs has uncommitted changes; commit them before a release"
  DOCS_HEAD=$(git -C "$DOCS_DIR" rev-parse HEAD)
  DOCS_NEXT=$(git -C "$DOCS_DIR" rev-parse -q --verify "refs/heads/next^{commit}" 2>/dev/null) || DOCS_NEXT=""
  [ "$DOCS_HEAD" = "$DOCS_NEXT" ] \
    || die "forgefirm-docs HEAD ($DOCS_HEAD) is not its next branch (${DOCS_NEXT:-none}): a release tags the documentation on next"
  DOCS_MAIN=$(git -C "$DOCS_DIR" ls-remote --exit-code origin refs/heads/main 2>/dev/null | cut -f1) || DOCS_MAIN=""
  [ -n "$DOCS_MAIN" ] \
    || die "cannot read main from the origin of forgefirm-docs"
  git -C "$DOCS_DIR" merge-base --is-ancestor "$DOCS_MAIN" "$DOCS_HEAD" 2>/dev/null \
    || die "main of forgefirm-docs on its origin ($DOCS_MAIN) is not an ancestor of next: main cannot fast-forward to the documentation this release tags (fetch, and bring next up to main)"
  if DOCS_TAG_REV=$(git -C "$DOCS_DIR" rev-parse -q --verify "refs/tags/v$VERSION^{commit}" 2>/dev/null); then
    # A tag an earlier run made names the documentation of that run. It
    # must be the documentation this release ships with.
    [ "$DOCS_TAG_REV" = "$DOCS_HEAD" ] \
      || die "forgefirm-docs tag v$VERSION names $DOCS_TAG_REV, not HEAD ($DOCS_HEAD): remove the stale tag or check out the commit it names"
  fi
fi

# The build configuration is the kas files and nothing else. bitbake also
# reads build/conf/site.conf and build/conf/auto.conf, which kas leaves in
# place and no manifest records.
for CONF in site.conf auto.conf; do
  [ ! -e "$REPO/build/conf/$CONF" ] \
    || die "build/conf/$CONF changes the build and is recorded nowhere - remove it before a release"
done

# Every repository of the build comes from its url at the commit the lock
# names. A repository with no url is a local checkout (the commented
# local-sibling form of meta-openglow): kas performs no git operation on
# it, the build takes whatever it holds, and the tag does not reproduce
# the release. The dump is captured whole and its status checked before
# its text is read: a dump that failed, or one read only to its first
# match, proves nothing.
KAS_JSON=$( cd "$REPO" && kas dump --format json kas/forgefirm-glowforge.yml ) \
  || die "kas dump failed"
printf '%s' "$KAS_JSON" | python3 -B -c '
import json, sys
cfg = json.load(sys.stdin)
locked = (cfg.get("overrides") or {}).get("repos") or {}
bad = []
for name, repo in sorted((cfg.get("repos") or {}).items()):
    repo = repo or {}
    if "url" not in repo and "path" not in repo:
        continue    # this repository, which the repository-state gates check
    if not repo.get("url"):
        bad.append("%s has no url (a local checkout)" % name)
    elif not (repo.get("commit") or (locked.get(name) or {}).get("commit")):
        bad.append("%s names no commit (kas lock)" % name)
for b in bad:
    print(b, file=sys.stderr)
sys.exit(1 if bad else 0)' \
  || die "the kas configuration builds a repository from something other than its url at a locked commit"
# debug-tweaks must not sit in the shared kas config, where it would apply
# to every target including the release image.
case "$KAS_JSON" in
  *debug-tweaks*) die "debug-tweaks appears in the resolved kas config - it must live only in forgefirm-image-dev.bb" ;;
esac
echo "kas config gate OK (every repository at its locked commit, no debug-tweaks in the shared config)"

# Version single-source check.
BB_REL=$(sed -n 's/^FORGEFIRM_RELEASE ?= "\(.*\)"/\1/p' "$RELEASE_INC")
[ "$BB_REL" = "$VERSION" ] \
  || die "FORGEFIRM_RELEASE in forgefirm-release.inc is '$BB_REL', not '$VERSION'"

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

# The assets are staged beside their place, and the sign step renames them
# into it, so a run that a gate stops leaves no signed archive where the
# publish command looks. A stage an earlier run left for this version goes
# now, for the same reason: from here on the place holds the assets of this
# run or nothing.
STAGED="${RELEASE_STAGING_DIR:-$REPO/release-staging}/v$VERSION"
STAGE="$STAGED.tmp"
rm -rf "$STAGED" "$STAGE"

# The archiver writes each recipe's source under the deploy directory's
# sources/, which keeps the archives of earlier builds. Emptied here, the
# bundle can only take an archive this build wrote.
if [ "$SOURCE_BUNDLE" = 1 ]; then
  rm -rf "$DEPLOY_ROOT/sources"
fi

build_images
same_checkout "during the build"
resolve_ext4
check_size "$EXT4"
resolve_dev_ext4
check_size "$DEV_EXT4"

# What the gates judge is what the sign step signs: each rootfs is copied
# into the stage first and its hash recorded, every gate below reads the
# copy, and the sign step refuses a copy whose hash moved.
mkdir -p "$STAGE/sign"
cp "$EXT4" "$STAGE/sign/$REL_ROOTFS"
cp "$DEV_EXT4" "$STAGE/sign/$DEV_ROOTFS"
REL_SHA=$(sha256sum "$STAGE/sign/$REL_ROOTFS" | cut -d' ' -f1)
DEV_SHA=$(sha256sum "$STAGE/sign/$DEV_ROOTFS" | cut -d' ' -f1)
REL_IMG="$STAGE/sign/$REL_ROOTFS"
DEV_IMG="$STAGE/sign/$DEV_ROOTFS"

same_build "$REL_IMG" "$DEV_IMG"
pins_only "$REL_IMG" "$(basename "$EXT4")"
pins_only "$DEV_IMG" "$(basename "$DEV_EXT4")"

# Rootfs version stamp.
STAMP=$(debugfs -R "cat /etc/forgefirm-version" "$REL_IMG" 2>/dev/null)
[ "$STAMP" = "v$VERSION" ] \
  || die "rootfs stamp is '$STAMP', expected 'v$VERSION'"

REL_MANIFEST=$(mktemp)
debugfs -R "cat /etc/forgefirm-manifest.json" "$REL_IMG" > "$REL_MANIFEST" 2>/dev/null || true
[ -s "$REL_MANIFEST" ] \
  || die "release rootfs carries no /etc/forgefirm-manifest.json"

# Acceptance gate: the committed acceptance artifact, signed by a bench key
# releases/allowed_signers holds, must authorize THIS build.
# scripts/acceptance-gate.py recomputes every catalog test's domain
# fingerprint from the manifest inside the release rootfs and requires the
# recorded PASS to match (https://docs.forgefirm.org/developers/acceptance/).
# A build whose gate was skipped (FORGEFIRM_ACCEPTANCE_SKIP=1, never with
# --publish) is signed with the dev key by the sign step.
ART="$REPO/releases/v$VERSION/acceptance.json"
if [ "$GATED" = 0 ]; then
  warn "acceptance gate SKIPPED by FORGEFIRM_ACCEPTANCE_SKIP - forgefirm.fw will carry the dev signature, and the release no acceptance proof"
else
  [ -f "$ART" ] \
    || die "no acceptance artifact at releases/v$VERSION/acceptance.json - run the campaign on the bench, export, commit"
  python3 "$REPO/scripts/acceptance-gate.py" "$ART" "$REL_MANIFEST" --machine glowforge \
    || die "acceptance gate refused this build (see the table above)"
  echo "acceptance gate OK ($ART)"
fi

# The host tests: every path of the release manifest is covered by a
# catalog test or by a host test (the coverage lint, run on this build's
# manifest), and the CI that runs each host test passed at the commit this
# build pins, as did forgefirm's own CI at HEAD (scripts/host-test-gate.py).
python3 "$REPO/scripts/host-test-gate.py" "$REL_MANIFEST" \
  || die "host-test gate refused this build (see above)"

# Root policy gate. The release image ships root WITHOUT a password (the
# serial console is the recovery path, and su opens root to the operator
# account: forgefirm-image.bb, Root policy): root's shadow field must be
# empty. Read the built file, not the recipes: this catches a drift
# however it got in (recipe, local.conf, an inherited class).
SSHD_CONFIG=$(debugfs -R "cat /etc/ssh/sshd_config" "$REL_IMG" 2>/dev/null)
[ -n "$SSHD_CONFIG" ] \
  || die "release rootfs carries no /etc/ssh/sshd_config"
# awk reads to the end: an early exit would SIGPIPE a debugfs still
# writing, and pipefail would end the release there.
ROOT_SHADOW=$(debugfs -R "cat /etc/shadow" "$REL_IMG" 2>/dev/null \
              | awk -F: '$1=="root" && !seen {print; seen=1}')
[ -n "$ROOT_SHADOW" ] \
  || die "release rootfs has no root entry in /etc/shadow"
ROOT_PW=$(printf '%s\n' "$ROOT_SHADOW" | awk -F: '{print $2}')
[ -z "$ROOT_PW" ] \
  || die "release rootfs has a non-empty root password field: the policy is an empty field (empty-root-password in forgefirm-image.bb); a build drift"
echo "root policy gate OK (root field empty)"

# Release posture gate. The release rootfs carries nothing of the dev
# image's posture, whatever the build configuration said, since the rootfs
# lint holds the image to the policy of the kind its recipe names: no dev
# marker (it opens forgectrl's writes to the panel token alone and starts
# sshd at every boot), no acceptance tool, and every sshd configuration it
# carries refuses root and an empty password (sshd_config_readonly is the
# one the read-only rootfs runs).
for F in /etc/forgefirm-dev /etc/init.d/forgetest; do
  case "$(debugfs -R "stat $F" "$REL_IMG" 2>/dev/null)" in
    *Inode:*) die "release rootfs carries $F, which belongs to the dev image" ;;
  esac
done
for C in sshd_config sshd_config_readonly; do
  TEXT=$(debugfs -R "cat /etc/ssh/$C" "$REL_IMG" 2>/dev/null)
  [ -n "$TEXT" ] || continue
  for K in PermitRootLogin PermitEmptyPasswords; do
    # The first value before any Match block, as sshd takes it; awk reads
    # to the end (the pipefail note above).
    V=$(printf '%s\n' "$TEXT" | awk -v k="$K" '
        { line = $0; sub(/^[ \t]+/, "", line) }
        done || line == "" || line ~ /^#/ { next }
        { n = split(line, f, /[ \t=]+/); key = tolower(f[1]) }
        key == "match" { done = 1; next }
        key == tolower(k) { v = (n > 1 ? f[2] : ""); done = 1 }
        END { print v }')
    [ "$V" = no ] || die "release $C: $K is '${V:-unset}', not no"
  done
done
echo "release posture gate OK (no dev marker, no forgetest, sshd refuses root and empty passwords)"

# The dev image carries no standard-library Python module that the release
# image lacks but the ones forgetest declares (python3-<name> in its
# RDEPENDS): a release program that imported such a module would pass every
# campaign on the dev image and fail on the release image. A module is
# named as its package names it: an extension module's leading underscore,
# its _test suffix and its ABI tag are dropped (_ctypes_test is ctypes).
py_modules () {
  local LIB
  LIB=$(debugfs -R "ls -p /usr/lib" "$1" 2>/dev/null \
        | awk -F/ '$6 ~ /^python3\.[0-9]+$/ { n = $6 } END { print n }')
  [ -n "$LIB" ] || return 0
  for D in "/usr/lib/$LIB" "/usr/lib/$LIB/lib-dynload"; do
    debugfs -R "ls -p $D" "$1" 2>/dev/null \
      | awk -F/ 'NF > 6 && $6 != "." && $6 != ".." { print $6 }'
  done | sed -e 's/\.py$//' -e 's/\.cpython-.*\.so$//' -e 's/\.so$//' -e 's/_test$//' -e 's/^_//' \
    | LC_ALL=C sort -u
}
FORGETEST_BB="$REPO/meta-forgefirm/recipes-forgefirm/forgetest/forgetest.bb"
DECLARED=$(grep -o -E 'python3-[a-z0-9_-]+' "$FORGETEST_BB" | sed 's/^python3-//' | LC_ALL=C sort -u)
EXTRA=$(LC_ALL=C comm -13 <(py_modules "$REL_IMG") <(py_modules "$DEV_IMG") \
        | LC_ALL=C comm -23 - <(printf '%s\n' "$DECLARED"))
[ -z "$EXTRA" ] \
  || die "the dev image carries standard-library modules the release image lacks and forgetest does not declare: $(echo $EXTRA)"
echo "dev image module gate OK (its extra modules are forgetest's)"

# Read-only rootfs gate. The release rootfs mounts read-only: the root
# line of the built fstab carries ro, and sshd keeps its host keys on
# /data, where the read-only rootfs cannot hold them. rcS's
# ROOTFS_READ_ONLY and the fstab with no factory slot are the rootfs
# lint's.
FSTAB=$(debugfs -R "cat /etc/fstab" "$REL_IMG" 2>/dev/null)
[ -n "$FSTAB" ] \
  || die "release rootfs carries no /etc/fstab"
printf '%s\n' "$FSTAB" | awk '$1 == "/dev/root" && $2 == "/" { print $4 }' \
  | grep -Eq '(^|,)ro(,|$)' \
  || die "release fstab does not mount / read-only (base-files fstab or read-only-rootfs drift?)"
[ "$(printf '%s\n' "$SSHD_CONFIG" | grep -c '^HostKey /data/forgefirm/ssh/')" = 3 ] \
  || die "release sshd_config does not keep the host keys under /data/forgefirm/ssh (recipes-connectivity/openssh drift?)"
echo "read-only rootfs gate OK (/ ro, host keys on /data)"

echo "== stage assets =="
# Each SD card image is the one its own rootfs was made with, bound by
# name, never through a deploy link an earlier build or a failed one left.
REL_WIC="${EXT4%.ext4}.wic.gz"
DEV_WIC="${DEV_EXT4%.ext4}.wic.gz"
[ -s "$REL_WIC" ] || die "no SD card image $(basename "$REL_WIC") beside the release rootfs"
[ -s "$DEV_WIC" ] || die "no SD card image $(basename "$DEV_WIC") beside the dev rootfs"
cp -L "$REL_WIC" "$STAGE/forgefirm-image-glowforge.rootfs.wic.gz"
cp -L "$DEV_WIC" "$STAGE/forgefirm-image-dev-glowforge.rootfs.wic.gz"
# The acceptance artifact travels with the release (see the site,
# Developers, "Acceptance") - only when the gate accepted it for THIS
# rootfs. A skipped gate ships no artifact: an acceptance.json next to a
# rootfs it never authorized would read as proof. The release says so
# instead, and goes out as a prerelease.
ASSETS="forgefirm.fw sha256sums.txt forgefirm-image-glowforge.rootfs.wic.gz"
PRERELEASE=""
rm -f "$STAGE/acceptance.json" "$STAGE/acceptance.md" "$STAGE/NO-ACCEPTANCE.txt"
if [ "$GATED" = 1 ]; then
  cp "$ART" "$STAGE/acceptance.json"
  ASSETS="$ASSETS acceptance.json"
  if [ -f "${ART%.json}.md" ]; then
    cp "${ART%.json}.md" "$STAGE/acceptance.md"
    ASSETS="$ASSETS acceptance.md"
  fi
else
  cat > "$STAGE/NO-ACCEPTANCE.txt" <<NOTE
ForgeFIRM v$VERSION was built with the acceptance gate skipped
(FORGEFIRM_ACCEPTANCE_SKIP). No acceptance campaign authorized this
rootfs, so forgefirm.fw carries the dev signature: no machine trusts it,
and the control panel installs it only with the machine button held.
Treat it as a prerelease.
NOTE
  ASSETS="$ASSETS NO-ACCEPTANCE.txt"
  PRERELEASE="--prerelease"
fi
# The source bundle. It is packed from the license manifests of THESE two
# rootfs images, and source-bundle.py stops the release when a recipe of
# either has no source (see the site, Developers, "Release flow").
rm -f "$STAGE"/forgefirm-source-v*.tar.gz "$STAGE/NO-SOURCE.txt"
if [ "$SOURCE_BUNDLE" = 1 ]; then
  echo "== source bundle =="
  python3 "$REPO/scripts/source-bundle.py" "$VERSION" \
      --deploy "$DEPLOY_ROOT" --image-name "$(basename "$EXT4" .ext4)" \
      --image-name "$(basename "$DEV_EXT4" .ext4)" \
      --out "$STAGE" \
    || die "the source bundle failed"
  ASSETS="$ASSETS forgefirm-source-v$VERSION.tar.gz"
else
  warn "source bundle SKIPPED by FORGEFIRM_SOURCE_SKIP - this release publishes no source"
  cat > "$STAGE/NO-SOURCE.txt" <<NOTE
ForgeFIRM v$VERSION was built with the source bundle skipped
(FORGEFIRM_SOURCE_SKIP). The image contains GPL and other copyleft
software whose licenses require the corresponding source to accompany
the binaries, and this staging directory does not carry it.

Do not publish these assets as they stand. Rebuild without
FORGEFIRM_SOURCE_SKIP, or attach the matching source bundle before the
binaries reach anyone.
NOTE
  ASSETS="$ASSETS NO-SOURCE.txt"
  PRERELEASE="--prerelease"
fi

# --- the documentation tag ----------------------------------------------------
#
# Firmware on a machine needs the documentation that agrees with it, so the
# docs repository carries the same tag as the release, on the next commit
# checked before the build. The tag is made last, once every gate has passed
# and the checkout is still the commit built, and pushed with the release,
# never before: a tag on documentation that never shipped is worse than no
# tag at all.
docs_tag () {
  if [ "${FORGEFIRM_DOCS_SKIP:-}" = 1 ]; then
    warn "docs tag SKIPPED by FORGEFIRM_DOCS_SKIP - this release ships no matching documentation tag"
    return
  fi
  [ "$(git -C "$DOCS_DIR" rev-parse HEAD)" = "$DOCS_HEAD" ] && [ -z "$(git -C "$DOCS_DIR" status --porcelain)" ] \
    || die "forgefirm-docs moved during the build: it is no longer the clean $DOCS_HEAD the gates checked"
  if git -C "$DOCS_DIR" rev-parse -q --verify "refs/tags/v$VERSION" >/dev/null 2>&1; then
    echo "docs: tag v$VERSION already exists in $DOCS_DIR"
  else
    git -C "$DOCS_DIR" tag -a "v$VERSION" -m "ForgeFIRM v$VERSION" \
      || die "cannot tag forgefirm-docs"
    echo "docs: tagged $DOCS_DIR at v$VERSION"
  fi
  echo "docs: v$VERSION -> $(git -C "$DOCS_DIR" rev-parse --short HEAD)"
}

# The release notes: releases/v<version>/notes.md of the commit built, as
# approved before the release commit (staged beside the assets, not
# uploaded as one, and bound by its hash like every staged file), GitHub's
# generated notes appended otherwise.
#
# Every release ends with the same Installation Information block. GPLv3
# section 6 asks that it be provided WITH the object code, not merely be
# possible, and the release page is where the object code is conveyed. The
# source bundle's README carries the same text for the source route.
NOTES_OPT="--notes-file notes.md"
if [ -f "$REPO/releases/v$VERSION/notes.md" ]; then
  cp "$REPO/releases/v$VERSION/notes.md" "$STAGE/notes.md"
  echo "notes: releases/v$VERSION/notes.md"
else
  # No hand-written notes: GitHub's generated changelog carries the release,
  # and gh appends it to the body the notes file provides.
  printf 'ForgeFIRM v%s. Installation, usage, and safety are documented at\nhttps://docs.forgefirm.org/.\n' \
    "$VERSION" > "$STAGE/notes.md"
  NOTES_OPT="$NOTES_OPT --generate-notes"
  echo "notes: generated by GitHub, with the installation block"
fi
cat >> "$STAGE/notes.md" <<'EOF'

## Installing your own build

Full instructions: <https://docs.forgefirm.org/install/updating/#installing-your-own-build>.
Building the image is documented at
<https://docs.forgefirm.org/developers/building/>.
EOF
# Only a release that carries the source bundle may point at it. The
# no-source path cannot be published at all, but its staged notes must not
# claim an asset that is not there either.
if [ "$SOURCE_BUNDLE" = 1 ]; then
  cat >> "$STAGE/notes.md" <<'EOF'
The source of everything in both images of this release is the
`forgefirm-source-v*.tar.gz` asset.
EOF
fi

# The request the sign step reads: the commit this run built, the
# documentation it tagged, the artifact the gate accepted, every staged
# file by hash (the notes included), and what to publish. "publish=1" only
# when this run enforced the publish gates (--publish): the sign step
# prints the publish commands for nothing else. The sign step holds the
# request to its own checkouts (the commit, the tag, the documentation
# tag) and runs the acceptance gate again itself, so the request alone
# decides nothing. Written under a temporary name and renamed into place,
# so a request is whole or absent.
same_checkout "while the gates ran"
docs_tag
{
  echo "version=$VERSION"
  echo "publish=$PUBLISH"
  echo "gated=$GATED"
  echo "commit=$BUILT_REV"
  echo "docs_commit=${DOCS_HEAD:-}"
  if [ "$GATED" = 1 ]; then
    echo "artifact=$(sha256sum "$ART" | cut -d' ' -f1)"
  fi
  echo "rootfs=$REL_ROOTFS $REL_SHA"
  echo "dev_rootfs=$DEV_ROOTFS $DEV_SHA"
  for F in $(cd "$STAGE" && find . -maxdepth 1 -type f ! -name 'SIGN-REQUEST*' | sed 's|^\./||' | LC_ALL=C sort); do
    echo "hash.$F=$(sha256sum "$STAGE/$F" | cut -d' ' -f1)"
  done
  echo "assets=$ASSETS"
  echo "prerelease=$PRERELEASE"
  echo "notes_opt=$NOTES_OPT"
} > "$STAGE/SIGN-REQUEST.tmp"
mv "$STAGE/SIGN-REQUEST.tmp" "$STAGE/SIGN-REQUEST"
ls -la "$STAGE"

cat <<EOF

== release v$VERSION staged for signing in $STAGE ==

Before the sign step (docs.forgefirm.org, Developers, "Release flow"):
  - every firmware repository merged from next and pushed, and
    scripts/release-branch-check.py passed (this run ran every check)
  - the kas lock names the meta-openglow commit of the merge, set by hand
    (kas lock --update moves every layer; it is never run for a release)
  - forgefirm-docs next current for this release and its three checks
    passed; it stays local until the release is published
  - the release notes approved before the release commit: the staged
    notes.md is that commit's, bound by its hash, and the sign step
    refuses an edit to it

Sign, with the private keys at hand, from a clean forgefirm checkout at
$(git -C "$REPO" rev-parse --short "$BUILT_REV"): this one, or one on another
machine that holds the stage. With --publish, that checkout carries the
tag v$VERSION made here, and a forgefirm-docs checkout beside it (or at
FORGEFIRM_DOCS_DIR) carries the documentation tag v$VERSION: on another
machine, fetch both tags from this host's checkouts. The sign step prints
the pushes for the checkouts it runs with.
  scripts/release-sign.sh $VERSION
EOF
