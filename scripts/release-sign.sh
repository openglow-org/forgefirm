#!/bin/bash
# Copyright 2026 514 LLC d/b/a OpenGlow
# Written by Scott Wiederhold
# https://community.openglow.org
# SPDX-License-Identifier:    MIT
#
# ForgeFIRM release sign step: the only step of a release that reads a
# private key.
#
#   release-sign.sh <version>
#
# scripts/release.sh <version> stages the release, unsigned, in
# <staging>/v<version>.tmp: the assets, a copy of each root filesystem under
# sign/, and SIGN-REQUEST, which records the commit the run built, the
# documentation it tagged, the artifact the gate accepted, and the sha256
# of each rootfs copy and of every staged file, the release notes included.
# This step runs with the private keys at hand, from a clean forgefirm
# checkout of that commit (the build host's, or one on another machine that
# holds the stage). Before it reads any key it requires:
#
#   - its checkout to be clean and at the request's commit, as the staging
#     run requires of its own (no file a local exclude hides and no edit the
#     index ignores where the release reads), and the tag v<version> to name
#     that commit (when the run had --publish, or when the tag exists);
#   - when the run had --publish and tagged the documentation, the tag
#     v<version> of the forgefirm-docs checkout beside this one (or at
#     FORGEFIRM_DOCS_DIR) to name the documentation the staging run checked;
#   - every rootfs copy and every staged file the request lists to have the
#     hash it records, and every asset to publish to be one of them;
#   - for a gated release, the committed artifact to be the one the staging
#     run's gate accepted, and scripts/acceptance-gate.py, run again here
#     with this checkout's catalog and allowed signers, to authorize the
#     staged rootfs. A request that says gated and that the gate does not
#     confirm is refused.
#
# What the hashes catch is a stage that changed or went stale between the
# staging run and this step; what binds the release key is the gate's
# verdict, made here. A build host that runs code of its own can still hand
# this step a build of its own making: keeping the key off that host
# protects the key, not the build. Then:
#
#   - packs and signs forgefirm.fw (the release rootfs) with the release key,
#     or, when the staging run skipped the acceptance gate, with the dev key,
#     which no machine trusts;
#   - packs and signs forgefirm-dev.fw (the dev rootfs) with the dev key;
#   - verifies forgefirm.fw with the factory-era fwup 0.14.2;
#   - stages the dev key's public half as forgefirm-dev.pub;
#   - writes sha256sums.txt over every asset and signs it with the sums key
#     (sha256sums.txt.sig, ssh-keygen -Y sign, namespace forgefirm-sums);
#   - renames the stage to <staging>/v<version>, and prints the publish
#     commands when the staging run enforced the publish gates (--publish):
#     the tag pushes name the checkouts this step ran with.
#
# Environment:
#   FORGEFIRM_SIGNING_KEY  the release private key (fwup's format; its public
#                        half <key without .priv>.pub beside it), REQUIRED
#                        for a gated release. Its public half must be the
#                        release key the image trusts
#                        (meta-forgefirm/recipes-forgefirm/forgefirm-keys/
#                        files/forgefirm-release.pub).
#   FORGEFIRM_DEV_KEY    the dev private key (the same form), REQUIRED; never
#                        the release key
#   FORGEFIRM_SUMS_KEY   the sums private key (an OpenSSH Ed25519 key),
#                        REQUIRED; its public half must be the forgefirm-sums
#                        line of releases/allowed_signers
#   FWUP                 host fwup for packing (default: fwup in PATH)
#   FWUP_COMPAT          the factory-era fwup 0.14.2 binary, REQUIRED unless
#                        FWUP_COMPAT_SKIP=1
#   FWUP_COMPAT_SKIP     set to 1 to sign without the factory-era check: the
#                        release then carries NO-FACTORY-CHECK.txt and is
#                        marked a prerelease. Never the default.
#   RELEASE_STAGING_DIR  where the staging run staged (default:
#                        <repo>/release-staging)
#   FORGEFIRM_DOCS_DIR   the forgefirm-docs checkout that carries the
#                        documentation tag (default: the sibling checkout)

set -euo pipefail

REPO="$(cd "$(dirname "$0")/.." && pwd)"
TRUSTED_PUB="$REPO/meta-forgefirm/recipes-forgefirm/forgefirm-keys/files/forgefirm-release.pub"
SIGNERS="$REPO/releases/allowed_signers"
SUMS_PRINCIPAL=forgefirm-sums
SUMS_NAMESPACE=forgefirm-sums

die () { echo "RELEASE SIGNING FAILED: $*" >&2; exit 1; }
warn () { echo "WARNING: $*" >&2; }

[ $# = 1 ] || die "usage: release-sign.sh <version>"
VERSION="$1"
[[ "$VERSION" =~ ^[0-9]+\.[0-9]+\.[0-9]+$ ]] || die "'$VERSION' is not a version (a.b.c)"
case "${FWUP_COMPAT_SKIP:-}" in
  ""|1) ;;
  *) die "FWUP_COMPAT_SKIP='$FWUP_COMPAT_SKIP': a skip takes the value 1 and no other" ;;
esac
FWUP="${FWUP:-fwup}"
command -v "$FWUP" >/dev/null || die "fwup not found (set FWUP=)"
command -v ssh-keygen >/dev/null || die "ssh-keygen not found: it signs sha256sums.txt"
command -v debugfs >/dev/null || die "debugfs not found: the acceptance gate reads the staged rootfs's manifest with it"

STAGED="${RELEASE_STAGING_DIR:-$REPO/release-staging}/v$VERSION"
STAGE="$STAGED.tmp"
[ -f "$STAGE/SIGN-REQUEST" ] \
  || die "nothing staged for signing at $STAGE (run scripts/release.sh $VERSION first)"
[ ! -e "$STAGED" ] || die "$STAGED already exists: v$VERSION was signed"

# The request, read as data: each line key=value.
declare -A REQ=()
while IFS= read -r LINE || [ -n "$LINE" ]; do
  case "$LINE" in
    *=*) REQ["${LINE%%=*}"]="${LINE#*=}" ;;
    "") ;;
    *) die "SIGN-REQUEST: a line that is not key=value: $LINE" ;;
  esac
done < "$STAGE/SIGN-REQUEST"
for K in version publish gated commit rootfs dev_rootfs assets notes_opt; do
  [ -n "${REQ[$K]:-}" ] || die "SIGN-REQUEST names no $K"
done
[ "${REQ[version]}" = "$VERSION" ] || die "SIGN-REQUEST is for v${REQ[version]}, not v$VERSION"
case "${REQ[gated]}/${REQ[publish]}" in
  1/0|1/1|0/0) ;;
  *) die "SIGN-REQUEST: gated=${REQ[gated]} publish=${REQ[publish]} is not a request the staging run writes" ;;
esac

# This checkout is the commit the staging run built: the gate below reads
# its artifact, its catalog and its allowed signers, and the tag the
# publish commands push must name it. Clean as the staging run's must be:
# where the release reads, no untracked file that only a local exclude
# hides, and no tracked file whose edits the index ignores (skip-worktree,
# assume-unchanged), which git status does not report.
git -C "$REPO" rev-parse --git-dir >/dev/null 2>&1 \
  || die "$REPO is not a git checkout - sign from a clean checkout of ${REQ[commit]}"
[ -z "$(git -C "$REPO" status --porcelain)" ] \
  || die "this checkout is dirty - sign from a clean checkout of ${REQ[commit]}"
RELEASE_READS="meta-forgefirm kas forgetest scripts releases"
HIDDEN=$(git -C "$REPO" -c core.excludesFile=/dev/null ls-files --others --exclude-per-directory=.gitignore -- $RELEASE_READS)
[ -z "$HIDDEN" ] \
  || die "untracked files that only a local exclude hides (${HIDDEN%%$'\n'*} ...) - sign from a clean checkout of ${REQ[commit]}"
UNSEEN=$(git -C "$REPO" ls-files -v -- $RELEASE_READS | grep -E '^[a-zS] ' || true)
[ -z "$UNSEEN" ] \
  || die "tracked files whose changes the index ignores (${UNSEEN%%$'\n'*} ...) - clear skip-worktree and assume-unchanged"
HEAD_REV=$(git -C "$REPO" rev-parse HEAD)
[ "$HEAD_REV" = "${REQ[commit]}" ] \
  || die "this checkout is at $HEAD_REV, and the stage is a build of ${REQ[commit]} - stage again, or sign from a checkout of that commit"
TAG_REV=$(git -C "$REPO" rev-parse -q --verify "refs/tags/v$VERSION^{commit}" 2>/dev/null) || TAG_REV=""
if [ "${REQ[publish]}" = 1 ] || [ -n "$TAG_REV" ]; then
  [ "$TAG_REV" = "$HEAD_REV" ] \
    || die "the tag v$VERSION names ${TAG_REV:-nothing}, not the staged commit $HEAD_REV"
fi
# The documentation tag the publish commands push names the documentation
# the staging run checked and tagged: the tag of the forgefirm-docs
# checkout this step runs with, the build host's or one the tag was
# fetched into.
DOCS_DIR=""
if [ "${REQ[publish]}" = 1 ] && [ -n "${REQ[docs_commit]:-}" ]; then
  DOCS_DIR="${FORGEFIRM_DOCS_DIR:-$REPO/../forgefirm-docs}"
  DOCS_TAG_REV=$(git -C "$DOCS_DIR" rev-parse -q --verify "refs/tags/v$VERSION^{commit}" 2>/dev/null) || DOCS_TAG_REV=""
  [ "$DOCS_TAG_REV" = "${REQ[docs_commit]}" ] \
    || die "the forgefirm-docs tag v$VERSION at $DOCS_DIR names ${DOCS_TAG_REV:-nothing}, not ${REQ[docs_commit]}, the documentation the staging run checked"
  DOCS_DIR=$(cd "$DOCS_DIR" && pwd)
fi

# Each root filesystem is the copy the gates judged: its hash now is the
# hash the staging run recorded.
rootfs () {
  local NAME SHA GOT
  read -r NAME SHA <<< "$1"
  case "$NAME" in
    */*|""|.*) die "SIGN-REQUEST names the rootfs '$NAME'" ;;
  esac
  [ -s "$STAGE/sign/$NAME" ] || die "the staged rootfs sign/$NAME is missing"
  GOT=$(sha256sum "$STAGE/sign/$NAME" | cut -d' ' -f1)
  [ "$GOT" = "$SHA" ] \
    || die "the staged rootfs sign/$NAME is not the one the gates judged (sha256 $GOT, recorded $SHA)"
  printf '%s' "$STAGE/sign/$NAME"
}
REL_IMG=$(rootfs "${REQ[rootfs]}") || exit 1
DEV_IMG=$(rootfs "${REQ[dev_rootfs]}") || exit 1
# So is every other staged file the request lists (the card images, the
# acceptance files, the source bundle, the notes), and every asset to
# publish that this step does not make itself is one of them.
for K in "${!REQ[@]}"; do
  case "$K" in hash.*) ;; *) continue ;; esac
  F="${K#hash.}"
  case "$F" in
    */*|""|.*) die "SIGN-REQUEST names the file '$F'" ;;
  esac
  [ -f "$STAGE/$F" ] || die "the staged file $F is missing"
  GOT=$(sha256sum "$STAGE/$F" | cut -d' ' -f1)
  [ "$GOT" = "${REQ[$K]}" ] \
    || die "the staged $F is not the one the staging run made (sha256 $GOT, recorded ${REQ[$K]})"
done
for A in ${REQ[assets]}; do
  case "$A" in
    forgefirm.fw|sha256sums.txt) ;;
    *) [ -n "${REQ[hash.$A]:-}" ] || die "the asset $A is not a file the staging run listed" ;;
  esac
done
echo "staged files: their hashes are the ones the staging run recorded"

# The release key signs only what the acceptance gate authorizes, by a
# verdict made here: the committed artifact (the one the staging run's gate
# accepted, and the one staged) against the staged rootfs's manifest, with
# this checkout's catalog and allowed signers.
if [ "${REQ[gated]}" = 1 ]; then
  ART="$REPO/releases/v$VERSION/acceptance.json"
  [ -f "$ART" ] || die "no acceptance artifact at releases/v$VERSION/acceptance.json in this checkout"
  ART_SHA=$(sha256sum "$ART" | cut -d' ' -f1)
  [ "$ART_SHA" = "${REQ[artifact]:-}" ] \
    || die "the committed artifact (sha256 $ART_SHA) is not the one the staging run's gate accepted"
  [ "${REQ[hash.acceptance.json]:-}" = "$ART_SHA" ] \
    || die "the staged acceptance.json is not the committed artifact"
  REL_MANIFEST=$(mktemp)
  trap 'rm -f "$REL_MANIFEST"' EXIT
  debugfs -R "cat /etc/forgefirm-manifest.json" "$REL_IMG" > "$REL_MANIFEST" 2>/dev/null || true
  [ -s "$REL_MANIFEST" ] || die "the staged release rootfs carries no /etc/forgefirm-manifest.json"
  python3 "$REPO/scripts/acceptance-gate.py" "$ART" "$REL_MANIFEST" --machine glowforge \
    || die "the acceptance gate, run again here, refuses this build: it is not signed with the release key"
  echo "acceptance gate OK, here ($ART)"
fi

# The key a public key file holds, as hex. Keys are compared by their bytes:
# two files that hold one key differ as text by a line ending.
key_hex () {
  HEX=$(tr -d '[:space:]' < "$1" | base64 -d 2>/dev/null | xxd -p | tr -d '\n') || HEX=""
  [ "${#HEX}" = 64 ] || die "'$1' does not hold a 32-byte public key"
  printf '%s' "$HEX"
}
[ -f "$TRUSTED_PUB" ] || die "the trusted release key '$TRUSTED_PUB' is missing"
TRUSTED_HEX=$(key_hex "$TRUSTED_PUB") || exit 1

# The dev key: never the release key. No machine trusts it, so the panel
# installs an archive it signed only with the machine button held.
DEV_KEY="${FORGEFIRM_DEV_KEY:?set FORGEFIRM_DEV_KEY to the dev signing key (it signs forgefirm-dev.fw)}"
DEV_PUB="${DEV_KEY%.priv}.pub"
[ -f "$DEV_KEY" ] || die "dev signing key '$DEV_KEY' not found"
[ -f "$DEV_PUB" ] || die "dev public key '$DEV_PUB' not found"
DEV_HEX=$(key_hex "$DEV_PUB") || exit 1
[ "$DEV_HEX" != "$TRUSTED_HEX" ] \
  || die "the dev key is the release key - an image with an open root login is never one a release-key signature vouches for"

# The key forgefirm.fw is signed with: the release key the image trusts, for
# a release whose campaign authorized it; the dev key otherwise.
if [ "${REQ[gated]}" = 1 ]; then
  KEY="${FORGEFIRM_SIGNING_KEY:?set FORGEFIRM_SIGNING_KEY to the release signing key}"
  PUB="${KEY%.priv}.pub"
  [ -f "$KEY" ] || die "signing key '$KEY' not found"
  [ -f "$PUB" ] || die "public key '$PUB' not found"
  [ "$(key_hex "$PUB")" = "$TRUSTED_HEX" ] \
    || die "the signing key is not the release key the image trusts ($TRUSTED_PUB)"
else
  warn "the acceptance gate was skipped: forgefirm.fw is signed with the dev key"
  KEY="$DEV_KEY"
  PUB="$DEV_PUB"
fi

# The sums key: its public half is the forgefirm-sums line of the allowed
# signers file in this tree, which an owner checks the sums against.
SUMS_KEY="${FORGEFIRM_SUMS_KEY:?set FORGEFIRM_SUMS_KEY to the sums signing key (it signs sha256sums.txt)}"
[ -f "$SUMS_KEY" ] || die "sums signing key '$SUMS_KEY' not found"
[ -f "$SIGNERS" ] || die "no allowed signers file at $SIGNERS"
SUMS_PUB=$(ssh-keygen -y -f "$SUMS_KEY" 2>/dev/null | awk '{print $1, $2}') || SUMS_PUB=""
[ -n "$SUMS_PUB" ] || die "'$SUMS_KEY' is not a private key ssh-keygen reads"
awk -v p="$SUMS_PRINCIPAL" -v k="$SUMS_PUB" '
  $1 == p { for (i = 2; i < NF; i++) if ($i " " $(i + 1) == k) found = 1 }
  END { exit found ? 0 : 1 }' "$SIGNERS" \
  || die "the sums key is not the $SUMS_PRINCIPAL key of $SIGNERS"

echo "== pack + sign =="
rm -f "$STAGE/forgefirm.fw" "$STAGE/forgefirm-dev.fw" "$STAGE/sha256sums.txt" \
      "$STAGE/sha256sums.txt.sig" "$STAGE/forgefirm-dev.pub" "$STAGE/NO-FACTORY-CHECK.txt"
sh "$REPO/scripts/mkfw.sh" "$REL_IMG" "v$VERSION" "$STAGE/forgefirm.fw" "$KEY"
sh "$REPO/scripts/mkfw.sh" "$DEV_IMG" "v$VERSION-dev" "$STAGE/forgefirm-dev.fw" "$DEV_KEY"

ASSETS="${REQ[assets]}"
PRERELEASE="${REQ[prerelease]:-}"
# Factory-era compat verification (fwup 0.14.2 wants raw 32-byte keys),
# against the key forgefirm.fw was signed with.
if [ -n "${FWUP_COMPAT:-}" ]; then
  RAW=$(mktemp)
  tr -d '[:space:]' < "$PUB" | base64 -d > "$RAW"
  "$FWUP_COMPAT" -V -i "$STAGE/forgefirm.fw" -p "$RAW" \
    || { rm -f "$RAW"; die "factory-era fwup rejects the archive"; }
  rm -f "$RAW"
  echo "factory-era fwup verification OK"
elif [ "${FWUP_COMPAT_SKIP:-}" = 1 ]; then
  # The factory-compat guarantee is a release property: a release that
  # skips it says so, and goes out as a prerelease.
  warn "FWUP_COMPAT not set - factory-era verification SKIPPED by FWUP_COMPAT_SKIP"
  cat > "$STAGE/NO-FACTORY-CHECK.txt" <<NOTE
ForgeFIRM v$VERSION was signed with the factory-era verification skipped
(FWUP_COMPAT_SKIP): nothing checked that the factory's fwup 0.14.2 accepts
forgefirm.fw. Treat it as a prerelease.
NOTE
  ASSETS="$ASSETS NO-FACTORY-CHECK.txt"
  PRERELEASE="--prerelease"
else
  die "FWUP_COMPAT not set - factory-era verification is required for a release (set FWUP_COMPAT_SKIP=1 to bypass deliberately)"
fi

# The dev key's public half travels with the release, so anyone can check
# forgefirm-dev.fw; the signed sums vouch for it.
cp "$DEV_PUB" "$STAGE/forgefirm-dev.pub"
DEV_ASSETS="forgefirm-dev.fw forgefirm-image-dev-glowforge.rootfs.wic.gz forgefirm-dev.pub"

# Every attached file is bound to the release by the sums, the artifact
# included, and the sums by the sums key's signature.
for A in $ASSETS $DEV_ASSETS; do
  [ "$A" = sha256sums.txt ] || [ -f "$STAGE/$A" ] || die "the asset $A is missing from $STAGE"
done
( cd "$STAGE" && sha256sum $(echo "$ASSETS $DEV_ASSETS" | tr ' ' '\n' | grep -v '^sha256sums.txt$') > sha256sums.txt )
ssh-keygen -q -Y sign -f "$SUMS_KEY" -n "$SUMS_NAMESPACE" "$STAGE/sha256sums.txt" \
  || die "cannot sign sha256sums.txt"
ssh-keygen -q -Y verify -f "$SIGNERS" -I "$SUMS_PRINCIPAL" -n "$SUMS_NAMESPACE" \
    -s "$STAGE/sha256sums.txt.sig" < "$STAGE/sha256sums.txt" >/dev/null \
  || die "the signature of sha256sums.txt does not verify against $SIGNERS"
echo "sha256sums.txt signed and verified ($SUMS_PRINCIPAL)"

# Signed: the stage goes to its place, without the copies and the request.
rm -rf "$STAGE/sign" "$STAGE/SIGN-REQUEST"
mv "$STAGE" "$STAGED"
STAGE="$STAGED"
ls -la "$STAGE"

# The dev image goes out with every release. Its words stay out of the
# release body, which every machine's update notice shows: the dev assets
# carry a label on the release page, and the docs site's bench page says
# the rest.
DEV_LABEL="development only: root SSH open, not release-signed (docs.forgefirm.org/developers/bench)"

if [ "${REQ[publish]}" != 1 ]; then
  cat <<EOF

== release v$VERSION signed in $STAGE ==

The staging run did not enforce the publish gates (no --publish): these
assets are not for publication.
EOF
  exit 0
fi

cat <<EOF

== release v$VERSION signed in $STAGE ==

Publish (from a directory with an authenticated gh). The notes are
notes.md, as approved before the release commit:
  git -C $REPO push origin v$VERSION
  cd "$STAGE"
  gh release create "v$VERSION" --repo openglow-org/forgefirm --verify-tag \\
    --title "ForgeFIRM v$VERSION" ${REQ[notes_opt]} $PRERELEASE \\
    $ASSETS sha256sums.txt.sig \\
    "forgefirm-dev.fw#forgefirm-dev.fw ($DEV_LABEL)" \\
    "forgefirm-image-dev-glowforge.rootfs.wic.gz#forgefirm-image-dev-glowforge.rootfs.wic.gz ($DEV_LABEL)" \\
    "forgefirm-dev.pub#forgefirm-dev.pub ($DEV_LABEL)"
EOF
if [ -n "$DOCS_DIR" ]; then
  cat <<EOF

Push the documentation tag with it:
  git -C $DOCS_DIR push origin v$VERSION
EOF
fi
