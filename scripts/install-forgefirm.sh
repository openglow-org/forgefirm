#!/bin/sh
# Copyright 2020-2026 514 LLC d/b/a OpenGlow
# Written by Scott Wiederhold
# https://community.openglow.org
# SPDX-License-Identifier:    MIT
#
# Single-stage OpenGlow/ForgeFIRM installer. Runs on FACTORY firmware:
#   1. archives the factory rootfs slots and the recovery boot partitions
#      to /data/forgefirm/archive (offline factory restore, forever),
#   2. applies the signed ForgeFIRM .fw to the INACTIVE rootfs slot using
#      the factory's own fwup (signature-verified),
#   3. flips the saved U-Boot environment to the new slot and reboots.
# The factory /data partition is never repartitioned or modified beyond
# the archive directory. ForgeFIRM replaces the factory firmware: the way
# back is the factory restore from that archive.
#
# Usage: install-forgefirm.sh [local-forgefirm.fw]
#   With no argument the release .fw is downloaded from GitHub: the release
#   this installer was published with (RELEASE_TAG, which the release
#   stamps), or the latest release for a copy that carries no tag. The
#   download resumes and retries; every run appends its steps to the
#   install log in the ForgeFIRM log tree, which the panel's log export
#   carries.

# The release this installer was published with: the release stamps it,
# and a downloaded archive must be that version. Empty in the source tree.
RELEASE_TAG=''
# The root the machine's paths are read under: empty on a machine. The
# host test (forgetest/tests/test_installer.py) runs a copy with its own.
ROOT=''
if [ -n "$RELEASE_TAG" ]; then
  RELEASE_FW_URL="https://github.com/openglow-org/forgefirm/releases/download/$RELEASE_TAG/forgefirm.fw"
else
  RELEASE_FW_URL="https://github.com/openglow-org/forgefirm/releases/latest/download/forgefirm.fw"
fi
DATA_DIR="$ROOT/data/forgefirm"
ARCHIVE_DIR="$DATA_DIR/archive"
FW_FILE="$DATA_DIR/forgefirm.fw"
MIN_DATA_FREE_KB=300000
LOG_DIR="$ROOT/data/log/forgefirm/install"
LOG_FILE="$LOG_DIR/install.log"
DL_ATTEMPTS=5                   # download tries before the install gives up
DL_DELAYS="5 15 30 60"          # seconds to wait before tries 2, 3, 4, 5
OTP_DIR="$ROOT/sys/fsl_otp"    # the factory kernel's fuse words
GF_SERVICES="glowforge glowforge-datalogger glowforge-updater bugeggs"

# ForgeFIRM release-signing public key (raw 32-byte Ed25519, the format the
# factory's fwup 0.14.2 expects).
PUBKEY='\x7c\x7c\x3f\x37\x91\xff\xe6\xdb\x81\xc6\x34\x41\x4b\x6f\xab\xed\x14\x65\xfb\x29\x25\xb6\xb1\x63\xb2\x1d\x38\xcb\xfb\x85\x91\x75'

LIGHTRED="\033[1;31m"
YELLOW="\033[1;33m"
BRIGHT="\033[1;39m"
RESET="\033[0m"
ASTERISK="${LIGHTRED}✺${RESET}"

# Where the run stands, for die: the Glowforge services stopped, and the
# slot write begun (from then on the services stay stopped until a reboot).
SERVICES_STOPPED=""
WRITE_STARTED=""

# log <SEVERITY> <message>: one line in the log tree's own format
# (timestamp, program[pid], severity, message). The log is appended
# across runs, so a run that failed is still on the machine after the run
# that worked. Logging never fails the install: with no writable log the
# lines go nowhere.
log () {
  [ -n "$LOG_OK" ] || return 0
  { echo "$(date -u '+%Y-%m-%dT%H:%M:%S+00:00') install[$$] $1 $2" >> "$LOG_FILE"; } 2>/dev/null
  return 0
}

# key_fingerprint: the embedded release key in hex, in groups of eight, as
# the release notes and the install page print it.
key_fingerprint () {
  echo "$PUBKEY" | sed 's/\\x//g; s/\(........\)/\1 /g; s/ $//'
}

stop_gf_services () {
  echo -n -e "${ASTERISK}Stopping Glowforge services"
  SERVICES_STOPPED=yes
  for SVC in $GF_SERVICES; do
    sv stop /sv/$SVC 2>/dev/null >/dev/null; echo -n "."
    sv stop /sv/$SVC/log 2>/dev/null >/dev/null; echo -n "."
  done
  echo "."
}

start_gf_services () {
  for SVC in $GF_SERVICES; do
    sv start /sv/$SVC/log 2>/dev/null >/dev/null
    sv start /sv/$SVC 2>/dev/null >/dev/null
  done
  SERVICES_STOPPED=""
}

# die <message>: the install stops. Before the slot write begins, the
# Glowforge services stopped for it are started again; once it has begun,
# they stay stopped until the machine restarts.
die () {
  log ERR "install failed: $1"
  echo
  echo -e "${LIGHTRED}!! INSTALL FAILED:${RESET} $1"
  if [ -n "$SERVICES_STOPPED" ] && [ -z "$WRITE_STARTED" ]; then
    start_gf_services
    echo -e "${LIGHTRED}!! The Glowforge services were started again.${RESET}"
  elif [ -n "$SERVICES_STOPPED" ]; then
    echo -e "${LIGHTRED}!! The Glowforge services stay stopped until the machine restarts.${RESET}"
  fi
  echo -e "${LIGHTRED}!! No boot change was made unless stated otherwise. Fix and re-run.${RESET}"
  exit 1
}

# factory_busy <device>: true, with BUSY_WHY, while a factory update may
# still write: an fwup process runs (the updater's child outlives its
# service's stop; found by its name in /proc, which needs no tool), or
# something holds the target slot open (fuser, where the factory firmware
# has it).
factory_busy () {
  BUSY_WHY=""
  for C in "$ROOT"/proc/[0-9]*/comm; do
    [ "$(cat "$C" 2>/dev/null)" = fwup ] && BUSY_WHY="an fwup process is running (a factory update?)"
  done
  if [ -z "$BUSY_WHY" ] && command -v fuser >/dev/null 2>&1 && fuser "$1" >/dev/null 2>&1; then
    BUSY_WHY="$1 is open"
  fi
  [ -n "$BUSY_WHY" ]
}

# archive_dev <src-device> <out.img.gz>: dd|gzip with a live progress
# line (compressed MB so far - old busybox dd has no status=progress).
# dd's exit status is captured in the run's own temporary file, so a read
# failure is not masked by gzip succeeding on truncated input. The archive
# is written as <out>.part and renamed only when both ends succeeded: a run
# that dies mid-archive (power, a dropped SSH session) leaves nothing under
# the final name for a rerun to mistake for a complete archive.
archive_dev () {
  RC_FILE=$(mktemp /tmp/ffinstall.rc.XXXXXX) || return 1
  PART="$2.part"
  rm -f "$PART"
  ( dd if="$1" bs=1M 2>/dev/null; echo $? > "$RC_FILE" ) | gzip -1 > "$PART" &
  GZPID=$!
  while kill -0 "$GZPID" 2>/dev/null; do
    sleep 3
    SZ=$(wc -c < "$PART" 2>/dev/null)
    printf '\r    %s MB compressed...' "$((${SZ:-0} / 1048576))"
  done
  wait "$GZPID"
  GZRC=$?
  DDRC=$(cat "$RC_FILE" 2>/dev/null)
  rm -f "$RC_FILE"
  printf '\r    %s MB compressed.    \n' "$(($(wc -c < "$PART") / 1048576))"
  if [ "$GZRC" = "0" ] && [ "$DDRC" = "0" ] && mv "$PART" "$2"; then
    return 0
  fi
  rm -f "$PART"
  return 1
}

# archived_ok <out.img.gz>: an existing archive counts only when the
# manifest records it and the gzip stream is whole. Anything else (a file
# left by an older installer's interrupted run, a manifest line that
# never got written) is archived again.
archived_ok () {
  [ -s "$1" ] || return 1
  grep -q " $(basename "$1") md5=" "$ARCHIVE_DIR/manifest" 2>/dev/null || return 1
  gzip -t "$1" 2>/dev/null
}

# first_line <file>: a version file's first line, printable ASCII only,
# at most 64 characters.
first_line () {
  head -n 1 "$1" 2>/dev/null | tr -cd ' -~' | cut -c1-64
}

# slot_probe <1|2>: sets S_TYPE (factory|forgefirm|empty|unknown),
# S_VER (the build datetime, used for archive naming), and S_FWVER (the
# semantic FIRMWARE_VERSION, for display; factory only). The active slot
# is read from the running rootfs; others are mounted read-only with no
# journal replay under /tmp (newer factory firmware has no /factory/imgN
# mounts, and the rootfs is read-only). Sets S_TYPE=empty for a
# mountable-but-unknown filesystem and S_TYPE=unknown when the slot will
# not even mount.
slot_probe () {
  S_TYPE=unknown; S_VER=""; S_FWVER=""; S_MOUNTED=""
  if [ "$1" = "$ACTIVE" ]; then
    RD="$ROOT"
  else
    RD=$(sed -n "s|^/dev/mmcblk2p$1 \([^ ]*\).*|\1|p" /proc/mounts | head -n 1)
    if [ -z "$RD" ]; then
      S_MOUNTED=yes
      RD=$(mktemp -d /tmp/ffinstall.probe.XXXXXX) || return 1
      mount -o ro,noload -t ext4 "$ROOT/dev/mmcblk2p$1" "$RD" 2>/dev/null \
        || { rmdir "$RD" 2>/dev/null; S_TYPE=unknown; return 0; }
    fi
  fi
  if [ -f "$RD/etc/forgefirm-version" ]; then
    S_TYPE=forgefirm; S_VER=$(first_line "$RD/etc/forgefirm-version")
  elif [ -f "$RD/etc/version" ]; then
    S_TYPE=factory; S_VER=$(first_line "$RD/etc/version")
    S_FWVER=$(sed -n 's/^FIRMWARE_VERSION[[:space:]]*=[[:space:]]*\([^[:space:]]*\).*/\1/p' \
              "$RD/etc/build" 2>/dev/null | head -n 1 | tr -cd ' -~' | cut -c1-64)
  else
    S_TYPE=empty
  fi
  if [ -n "$S_MOUNTED" ]; then
    umount "$RD" 2>/dev/null
    rmdir "$RD" 2>/dev/null
  fi
}

# Human label for the last slot_probe result.
slot_desc () {
  case "$S_TYPE" in
    factory)   [ -n "$S_FWVER" ] && echo "factory firmware v$S_FWVER" \
                                 || echo "factory firmware $S_VER" ;;
    forgefirm) echo "ForgeFIRM $S_VER" ;;
    empty)     echo "an unrecognized filesystem" ;;
    *)         echo "unknown/unreadable content" ;;
  esac
}

# ver_lt A B: true when semantic version A < B (leading v ignored).
# Returns false on any non-numeric component (e.g. a dev datetime
# stamp) - no verdict means no downgrade prompt, never a refusal.
ver_lt () {
  VA=${1#v}; VB=${2#v}
  [ "$VA" = "$VB" ] && return 1
  VI=1
  while [ "$VI" -le 3 ]; do
    A=$(echo "$VA" | cut -d. -f$VI)
    B=$(echo "$VB" | cut -d. -f$VI)
    A=${A:-0}; B=${B:-0}
    case "$A$B" in *[!0-9]*) return 1 ;; esac
    [ "$A" -lt "$B" ] && return 0
    [ "$A" -gt "$B" ] && return 1
    VI=$((VI + 1))
  done
  return 1
}

# archive_identity <file>: reads the archive's meta-product, meta-platform
# and meta-version (M_PRODUCT, M_PLATFORM, M_VERSION) and holds it to
# ForgeFIRM firmware for this platform, and a downloaded archive to the
# release this installer was published with. 1 with GATE_WHY otherwise.
archive_identity () {
  META=$(fwup -m -i "$1")
  M_PRODUCT=$(echo "$META" | sed -n 's/^meta-product="\(.*\)"$/\1/p')
  M_PLATFORM=$(echo "$META" | sed -n 's/^meta-platform="\(.*\)"$/\1/p')
  M_VERSION=$(echo "$META" | sed -n 's/^meta-version="\(.*\)"$/\1/p')
  GATE_WHY=""
  if [ "$M_PRODUCT" != "ForgeFIRM firmware" ]; then
    GATE_WHY="archive product is '$M_PRODUCT', not ForgeFIRM firmware - wrong archive"
  elif [ "$M_PLATFORM" != "glowforge" ]; then
    GATE_WHY="archive platform is '$M_PLATFORM', not glowforge - wrong archive"
  elif [ -n "$RELEASE_TAG" ] && [ -z "$LOCAL_FW" ] && [ "$M_VERSION" != "$RELEASE_TAG" ]; then
    GATE_WHY="the downloaded archive is $M_VERSION, not $RELEASE_TAG, the release this installer was published with"
  fi
  [ -z "$GATE_WHY" ]
}

# downgrade_gate: a validly signed OLDER release never installs silently;
# a downgrade takes an explicit y (rollback stays possible, just
# deliberate). INSTALLED is the newest ForgeFIRM version in either slot.
# 1 when the operator declines.
downgrade_gate () {
  INSTALLED=""
  for S in 1 2; do
    slot_probe "$S"
    if [ "$S_TYPE" = "forgefirm" ] && [ -n "$S_VER" ]; then
      if [ -z "$INSTALLED" ] || ver_lt "$INSTALLED" "$S_VER"; then
        INSTALLED="$S_VER"
      fi
    fi
  done
  if [ -n "$INSTALLED" ] && ver_lt "$M_VERSION" "$INSTALLED"; then
    echo -e "${ASTERISK}This archive ($M_VERSION) is OLDER than the installed ForgeFIRM ($INSTALLED)."
    read -n1 -p "Install the downgrade anyway? [y/N] " YN
    echo
    case "$YN" in
      y|Y) log NOTICE "operator accepted the downgrade from $INSTALLED to $M_VERSION"; return 0 ;;
      *) return 1 ;;
    esac
  fi
  return 0
}

# Verified atomic env flip: all four variables in one fw_setenv -s
# transaction, the classic u-boot-tools script format first (that is what
# factory firmware ships), then the libubootenv format, each read back.
# Config selection matches ffboot: newer factory firmware's generic
# fw_env.config points at the wrong device; its per-device
# fw_env_mmcblk2.config is the correct one for the eMMC environment.
if [ -f "$ROOT/etc/fw_env_mmcblk2.config" ] && [ ! -d "$ROOT/factory" ]; then
  FWCONFIG="$ROOT/etc/fw_env_mmcblk2.config"
else
  FWCONFIG="$ROOT/etc/fw_env.config"
fi

env_get () { fw_printenv -c "$FWCONFIG" -n "$1" 2>/dev/null; }

env_verify () {
  [ "$(env_get mmcdev)" = "$1" ] && [ "$(env_get mmchwpart)" = "$2" ] && \
  [ "$(env_get mmcpart)" = "$3" ] && [ "$(env_get mmcroot)" = "$4" ]
}

# env_now: the four variables as the environment holds them now, in words.
env_now () {
  echo "mmcdev=$(env_get mmcdev) mmchwpart=$(env_get mmchwpart) mmcpart=$(env_get mmcpart) mmcroot=$(env_get mmcroot)"
}

# set_env <mmcdev> <mmchwpart> <mmcpart> <mmcroot>: 0 once the four read
# back. There is no variable-by-variable fallback: four separate writes
# can leave one slot's kernel over the other slot's root.
set_env () {
  SCRIPT=$(mktemp /tmp/ffinstall.env.XXXXXX) || return 1
  printf 'mmcdev %s\nmmchwpart %s\nmmcpart %s\nmmcroot %s\n' "$1" "$2" "$3" "$4" > "$SCRIPT"
  fw_setenv -c "$FWCONFIG" -s "$SCRIPT" 2>/dev/null
  if env_verify "$1" "$2" "$3" "$4"; then rm -f "$SCRIPT"; return 0; fi
  printf 'mmcdev=%s\nmmchwpart=%s\nmmcpart=%s\nmmcroot=%s\n' "$1" "$2" "$3" "$4" > "$SCRIPT"
  fw_setenv -c "$FWCONFIG" -s "$SCRIPT" 2>/dev/null
  rm -f "$SCRIPT"
  env_verify "$1" "$2" "$3" "$4"
}

# env_slot: the eMMC slot (1 or 2) the saved environment boots next, into
# NEXT_SLOT, read before anything is written. mmcroot names it (or, in an
# environment with no mmcroot, mmcpart with mmcdev 1), and it must be the
# booted root or the target: a root that is neither is an environment read
# from the wrong place, or a mixed one. 1 with ENV_WHY otherwise.
env_slot () {
  E_DEV=$(env_get mmcdev); E_PART=$(env_get mmcpart); E_ROOT=$(env_get mmcroot)
  NEXT_SLOT=""; ENV_WHY=""
  case "$E_ROOT" in
    /dev/mmcblk2p1|/dev/mmcblk2p2)
      NEXT_SLOT=${E_ROOT#/dev/mmcblk2p}
      if [ "$E_ROOT" != "$BOOTED" ] && [ "$E_ROOT" != "/dev/mmcblk2p$TARGET" ]; then
        ENV_WHY="the saved environment's mmcroot ($E_ROOT) is neither the running root ($BOOTED) nor slot $TARGET"
      elif [ -n "$E_PART" ] && { [ "$E_DEV" != 1 ] || [ "$E_PART" != "$NEXT_SLOT" ]; }; then
        ENV_WHY="the saved environment is mixed: mmcroot=$E_ROOT with mmcdev=$E_DEV mmcpart=$E_PART"
      fi ;;
    "")
      if [ "$E_DEV" = 1 ] && { [ "$E_PART" = 1 ] || [ "$E_PART" = 2 ]; }; then
        NEXT_SLOT=$E_PART
      else
        ENV_WHY="the saved environment names no eMMC slot (mmcdev=$E_DEV mmcpart=$E_PART, no mmcroot)"
      fi ;;
    *)
      ENV_WHY="the saved environment's mmcroot ($E_ROOT) is not an eMMC slot" ;;
  esac
  [ -z "$ENV_WHY" ]
}

# slot_name <1|2>: the slot's name for the incomplete-write mark, as
# forgectrl and ffboot name it.
slot_name () {
  case "$1" in 1) echo a ;; 2) echo b ;; esac
}

# marker_set <1|2> / marker_clear <1|2>: the slot's incomplete-write mark,
# <data dir>/slot-<a|b>.incomplete, on the media from before the write's
# first byte until the written filesystem verified. ffboot refuses to
# select a marked slot, and forgectrl lists it.
marker_set () {
  MARK="$DATA_DIR/slot-$(slot_name "$1").incomplete"
  mkdir -p "$DATA_DIR" 2>/dev/null
  echo "the installer wrote slot $(slot_name "$1") and did not verify it" > "$MARK" || return 1
  sync
  [ -s "$MARK" ]
}

marker_clear () {
  rm -f "$DATA_DIR/slot-$(slot_name "$1").incomplete" && sync
}

# fuse_word <file>: the fuse word's value as a number on stdout, from the
# text the factory kernel gives it (0x followed by hex digits, or decimal
# digits, a leading zero read as octal); 1 for any other text or none.
fuse_word () {
  W=$(cat "$1" 2>/dev/null)
  case "$W" in
    0x*) H=${W#0x}; case "$H" in ''|*[!0-9a-fA-F]*) return 1 ;; esac ;;
    ''|*[!0-9]*) return 1 ;;
  esac
  echo $((W))
}

# board_rev: the board revision U-Boot reads, into MAJOR, MINOR and
# BOARD_REV, from the fuse word HW_OCOTP_GP1 (a zero word reads as 12.0
# when a serial number is fused, 13.0 when not, as U-Boot reads it). Read
# in pre-flight, before anything is written. 1 with DTB_WHY when the fuse
# cannot be read.
board_rev () {
  DTB_WHY=""
  REV=$(fuse_word "$OTP_DIR/HW_OCOTP_GP1") \
    || { DTB_WHY="the board revision cannot be read ($OTP_DIR/HW_OCOTP_GP1)"; return 1; }
  if [ "$REV" = 0 ]; then
    SN=$(fuse_word "$OTP_DIR/HW_OCOTP_MAC0") \
      || { DTB_WHY="the serial fuse cannot be read ($OTP_DIR/HW_OCOTP_MAC0)"; return 1; }
    if [ "$SN" = 0 ]; then REV=$((0xD0)); else REV=$((0xC0)); fi
  fi
  MAJOR=$(((REV >> 4) & 255))
  MINOR=$((REV & 15))
  BOARD_REV="$MAJOR$MINOR"
}

# board_dtb_ok <rootfs>: true when the rootfs carries the device tree
# U-Boot loads on this board (board_rev, read first):
# imx6dl-glowforge-v<major><minor>.dtb or -v<major>.dtb. 1 with DTB_WHY
# otherwise.
board_dtb_ok () {
  DTB_WHY=""
  for N in "$BOARD_REV" "$MAJOR"; do
    [ -n "$N" ] && [ -e "$1/boot/imx6dl-glowforge-v$N.dtb" ] && return 0
  done
  DTB_WHY="the image has no device tree for this board (revision $MAJOR.$MINOR: neither imx6dl-glowforge-v$BOARD_REV.dtb nor -v$MAJOR.dtb)"
  return 1
}

# curl_why <exit-code>: why a download attempt failed, in words.
curl_why () {
  case "$1" in
    5|6)      echo "the host name did not resolve (DNS)" ;;
    7)        echo "the connection was refused or unreachable" ;;
    18|52|55|56) echo "the connection dropped mid-transfer" ;;
    22)       echo "the server answered HTTP $2" ;;
    23)       echo "the file could not be written" ;;
    28)       echo "the connection timed out or stalled" ;;
    35|51|60) echo "the TLS handshake failed (a wrong clock does this: $(date -u '+%Y-%m-%d %H:%M') UTC)" ;;
    *)        echo "curl error $1" ;;
  esac
}

# net_snapshot: what the network looked like when an attempt failed - the
# address, the default route, the resolver, whether the release host
# resolves - into the install log only.
net_snapshot () {
  log INFO "net: wlan0 $(ip -4 addr show dev wlan0 2>/dev/null | sed -n 's/^ *inet \([^ ]*\).*/\1/p' | head -n 1)"
  log INFO "net: route $(ip route 2>/dev/null | grep '^default' | head -n 1)"
  log INFO "net: resolver $(grep '^nameserver' /etc/resolv.conf 2>/dev/null | tr '\n' ' ')"
  if nslookup github.com >/dev/null 2>&1; then
    log INFO "net: github.com resolves"
  else
    log WARNING "net: github.com does not resolve"
  fi
}

# download_fw: fetch the release into $FW_FILE. A home network drops, a
# resolver hiccups, a transfer stalls: each try resumes the partial file
# where the last one stopped, and the tries are spaced out. The file
# lands as <file>.part and takes its name only when curl finished, and
# the signature check that follows is what vouches for its content. Two
# failures end the tries at once, because waiting cannot fix them: a full
# disk, and a release that is not there (HTTP 404).
download_fw () {
  PART="$FW_FILE.part"
  rm -f "$PART"
  TRY=1
  set -- $DL_DELAYS
  while :; do
    log INFO "download: try $TRY of $DL_ATTEMPTS, clock $(date -u '+%Y-%m-%d %H:%M:%S') UTC"
    HTTP=$(curl -fL -C - --connect-timeout 20 --speed-limit 1024 --speed-time 30 \
                -w '%{http_code}' --output "$PART" "$RELEASE_FW_URL")
    RC=$?
    if [ "$RC" = "0" ]; then
      mv "$PART" "$FW_FILE" || return 1
      log INFO "download: complete on try $TRY, $(wc -c < "$FW_FILE") bytes"
      return 0
    fi
    WHY=$(curl_why "$RC" "$HTTP")
    SOFAR=0
    [ -f "$PART" ] && SOFAR=$(wc -c < "$PART")
    log WARNING "download: try $TRY failed: curl exit $RC, HTTP ${HTTP:-none}: $WHY ($SOFAR bytes so far)"
    net_snapshot
    # A partial file the server will not resume (no range support, or a
    # range past its end) starts over rather than failing every try.
    case "$RC:$HTTP" in 33:*|36:*|22:416) rm -f "$PART" ;; esac
    if [ "$RC" = "23" ] || [ "$RC:$HTTP" = "22:404" ] || [ "$TRY" -ge "$DL_ATTEMPTS" ]; then
      rm -f "$PART"
      DL_WHY="$WHY"
      return 1
    fi
    WAIT=${1:-60}; [ $# -gt 0 ] && shift
    echo -e "${YELLOW}!! Download failed: $WHY.${RESET} Trying again in ${WAIT}s (try $((TRY + 1)) of $DL_ATTEMPTS)..."
    sleep "$WAIT"
    TRY=$((TRY + 1))
  done
}

# The install log, before anything can fail.
mkdir -p "$LOG_DIR" 2>/dev/null && : >> "$LOG_FILE" 2>/dev/null && LOG_OK=yes
INSTALLER_MD5=""
[ -f "$0" ] && INSTALLER_MD5=$(md5sum "$0" 2>/dev/null | cut -d' ' -f1)
LOCAL_FW="$1"
log INFO "run start: installer md5=${INSTALLER_MD5:-unknown}, release ${RELEASE_TAG:-latest}, firmware source: ${1:-$RELEASE_FW_URL}"

echo
echo -e "${LIGHTRED} ✺┈┈┈┈┈┈${RESET}"
echo -e "${BRIGHT}Open${RESET}Glow ForgeFIRM Installation Tool${RELEASE_TAG:+ for $RELEASE_TAG}"
echo
echo -e "Release key: ${BRIGHT}$(key_fingerprint)${RESET}"
echo -e "Compare it with the key on the install page and in the release notes."
echo

# --- pre-flight ---------------------------------------------------------------
[ -f "$ROOT/etc/version" ] && [ -d "$ROOT/glowforge" ] \
  || die "this script must be run from the FACTORY firmware"
command -v fwup >/dev/null || die "fwup not found on this system"
command -v fw_setenv >/dev/null || die "fw_setenv not found on this system"
[ -f "$FWCONFIG" ] || die "$FWCONFIG not found"

BOOTED=$(sed -n 's/.*root=\([^ ]*\).*/\1/p' "$ROOT/proc/cmdline")
case "$BOOTED" in
  /dev/mmcblk2p1) ACTIVE=1; TARGET=2; TASK=upgrade.b ;;
  /dev/mmcblk2p2) ACTIVE=2; TARGET=1; TASK=upgrade.a ;;
  *) die "booted from $BOOTED - expected factory eMMC slot 1 or 2" ;;
esac

for N in 1 2; do
  SZ=$(cat "$ROOT/sys/class/block/mmcblk2p$N/size" 2>/dev/null)
  [ "$SZ" = "409600" ] || die "slot $N is not the 200 MiB factory layout (size=$SZ)"
done

FREE_KB=$(df -k "$ROOT/data" | tail -1 | awk '{print $4}')
[ "$FREE_KB" -ge "$MIN_DATA_FREE_KB" ] 2>/dev/null \
  || die "need ${MIN_DATA_FREE_KB} KB free on /data, have ${FREE_KB:-unknown}"
env_slot || die "$ENV_WHY. Nothing was written"
# The board's revision, before anything is written: the device tree the
# new image must carry for it is checked after the write.
board_rev || die "$DTB_WHY. Nothing was written"
log INFO "pre-flight: factory $(cat "$ROOT/etc/version"), board revision $MAJOR.$MINOR, booted slot $ACTIVE, target slot $TARGET, next boot slot $NEXT_SLOT, ${FREE_KB} KB free on /data, $(curl -V 2>/dev/null | head -n 1)"

echo -e "${LIGHTRED}!!!!!!!!!!!!!!!!     WARNING     !!!!!!!!!!!!!!!!${RESET}"
echo -e "${YELLOW}         THIS IS EXPERIMENTAL SOFTWARE!${RESET}"
echo -e "The installation and/or use of this software may"
echo -e "result in damage to your device and/or property,"
echo -e "loss of warranty, and severe bodily injury and/or"
echo -e "death. This software is not affiliated with or"
echo -e "endorsed by Glowforge. ${BRIGHT}USE IT AT YOUR OWN RISK!${RESET}"
echo -e "${LIGHTRED}!!!!!!!!!!!!!!!!     WARNING     !!!!!!!!!!!!!!!!${RESET}"
echo
echo -e "Booted slot: $ACTIVE - ForgeFIRM will be installed to slot $TARGET."
echo -e "ForgeFIRM replaces the factory firmware. Every factory version is"
echo -e "archived to /data first; the way back is a factory restore from it."
echo

# What is in the target slot, and will it be archived first? The archive
# step below backs up factory images; anything else in the target slot
# is overwritten without a backup, so make that explicit.
slot_probe "$TARGET"
TARGET_DESC=$(slot_desc)
SHOWN="$S_TYPE $S_VER"
echo -e "Slot $TARGET currently holds: ${BRIGHT}$TARGET_DESC${RESET}"
log INFO "slot $TARGET holds: $TARGET_DESC"
if [ "$S_TYPE" = "factory" ]; then
  echo -e "It will be archived to /data before being overwritten."
else
  echo -e "${YELLOW}!! This is NOT factory firmware and will NOT be archived.${RESET}"
  echo -e "${YELLOW}!! Its contents will be permanently destroyed.${RESET}"
  echo
  read -p "Type ERASE to overwrite slot $TARGET, or anything else to abort: " erase
  echo
  if [ "$erase" != "ERASE" ]; then
    log NOTICE "operator declined to erase slot $TARGET; no changes made"
    echo "Aborting without changes."
    exit 0
  fi
fi
echo
read -p "Are you sure you want to continue [N/y]? " continue
echo
if [ "$continue" != "y" ]; then
  log NOTICE "operator declined to continue; no changes made"
  echo "Wise choice.  Exiting without changes."
  exit 0
fi

# The slot about to be written must not be the next boot: a write there
# has no revert behind it. A selection that was never booted (a factory
# update that has not restarted yet, an earlier ffboot -n) is pointed back
# at the running slot first.
if [ "$NEXT_SLOT" = "$TARGET" ]; then
  echo -e "${ASTERISK}Slot $TARGET is selected for the next boot: pointing the next boot back at slot $ACTIVE first."
  set_env 1 0 "$ACTIVE" "$BOOTED" \
    || die "the next boot could not be pointed back at slot $ACTIVE: the environment reads $(env_now). Nothing was written"
  log NOTICE "the next boot was slot $TARGET; pointed back at the running slot $ACTIVE"
fi

log INFO "operator confirmed; stopping the Glowforge services"
stop_gf_services

# A factory update the stop did not end (its fwup outlives the service),
# or a slot that changed since it was shown, stops the install here.
factory_busy "$ROOT/dev/mmcblk2p$TARGET" \
  && die "$BUSY_WHY: a factory update may be writing. Wait for it to finish, then run the installer again"
slot_probe "$TARGET"
[ "$S_TYPE $S_VER" = "$SHOWN" ] \
  || die "slot $TARGET changed after it was shown (it now holds $(slot_desc)): run the installer again"
# The environment read again, now that no factory update runs: one that
# flipped it alone between the first read and the stop is seen here.
env_slot || die "$ENV_WHY. Nothing was written"
if [ "$NEXT_SLOT" = "$TARGET" ]; then
  set_env 1 0 "$ACTIVE" "$BOOTED" \
    || die "the next boot could not be pointed back at slot $ACTIVE: the environment reads $(env_now). Nothing was written"
  log NOTICE "the next boot became slot $TARGET before the write; pointed back at the running slot $ACTIVE"
fi

# --- archive factory content --------------------------------------------------
mkdir -p "$ARCHIVE_DIR"
for N in 1 2; do
  slot_probe $N
  [ "$S_TYPE" = "factory" ] || continue
  ARC="$ARCHIVE_DIR/factory-rootfs-$S_VER.img.gz"
  if archived_ok "$ARC"; then
    echo -e "${ASTERISK}Slot $N (factory $S_VER) already archived."
    log INFO "archive: slot $N (factory $S_VER) already archived"
    continue
  fi
  [ -e "$ARC" ] && echo -e "${ASTERISK}Slot $N: the archive on disk is incomplete; archiving again."
  echo -e "${ASTERISK}Archiving slot $N ($(slot_desc)) - takes a few minutes:"
  archive_dev "$ROOT/dev/mmcblk2p$N" "$ARC" \
    || { rm -f "$ARC"; die "archiving slot $N failed"; }
  echo "$(date '+%Y-%m-%d %H:%M:%S') slot$N factory $S_VER ver=${S_FWVER:-unknown} $(basename "$ARC") md5=$(md5sum "$ARC" | cut -d' ' -f1)" >> "$ARCHIVE_DIR/manifest" \
    || die "the archive manifest could not be written for slot $N"
  log INFO "archive: slot $N (factory $S_VER) -> $(basename "$ARC"), $(wc -c < "$ARC") bytes"
done
for B in 0 1; do
  ARC="$ARCHIVE_DIR/recovery-boot$B.img.gz"
  archived_ok "$ARC" && continue
  echo -e "${ASTERISK}Archiving recovery boot$B:"
  archive_dev "$ROOT/dev/mmcblk2boot$B" "$ARC" \
    || { rm -f "$ARC"; die "archiving boot$B failed"; }
  echo "$(date '+%Y-%m-%d %H:%M:%S') boot$B recovery - $(basename "$ARC") md5=$(md5sum "$ARC" | cut -d' ' -f1)" >> "$ARCHIVE_DIR/manifest" \
    || die "the archive manifest could not be written for boot$B"
  log INFO "archive: boot$B -> $(basename "$ARC"), $(wc -c < "$ARC") bytes"
done
# The archive of the target slot is now the only copy of that factory
# version: on the media before the slot is written.
sync

# --- acquire the ForgeFIRM .fw ------------------------------------------------
mkdir -p "$DATA_DIR"
if [ -n "$LOCAL_FW" ]; then
  [ -s "$LOCAL_FW" ] || die "local firmware file '$LOCAL_FW' not found"
  cp "$LOCAL_FW" "$FW_FILE" || die "cannot copy '$LOCAL_FW' to $FW_FILE"
  echo -e "${ASTERISK}Using local firmware file: $LOCAL_FW"
  log INFO "firmware: local file $LOCAL_FW, $(wc -c < "$FW_FILE") bytes"
else
  echo -e "${ASTERISK}Downloading OpenGlow/ForgeFIRM ${RELEASE_TAG:-(the latest release)}:"
  download_fw \
    || die "firmware download failed: ${DL_WHY:-unknown}. Check the network and re-run; the archives are kept, so a re-run goes straight to the download"
fi

# --- verify signature ---------------------------------------------------------
KEYFILE=$(mktemp /tmp/forgefirm.pub.XXXXXX) || die "cannot create temp file"
printf "$PUBKEY" > "$KEYFILE"
[ "$(wc -c < "$KEYFILE")" = "32" ] || die "embedded public key corrupt"
echo -e "${ASTERISK}Verifying firmware signature:"
fwup -V -i "$FW_FILE" -p "$KEYFILE" || { rm -f "$KEYFILE"; die "signature verification FAILED - refusing to install"; }

# --- archive identity + downgrade gate ----------------------------------------
archive_identity "$FW_FILE" || { rm -f "$KEYFILE"; die "$GATE_WHY"; }
echo -e "${ASTERISK}Archive: $M_PRODUCT $M_VERSION ($M_PLATFORM)"
log INFO "firmware: signature verified; $M_PRODUCT $M_VERSION ($M_PLATFORM)"
downgrade_gate || { rm -f "$KEYFILE"; die "downgrade declined"; }

# --- apply to the inactive slot -----------------------------------------------
echo -e "${ASTERISK}Writing ForgeFIRM to slot $TARGET (/dev/mmcblk2p$TARGET):"
log INFO "apply: writing $M_VERSION to slot $TARGET"
for M in $(sed -n "s|^/dev/mmcblk2p$TARGET \([^ ]*\).*|\1|p" /proc/mounts); do
  umount "$M" 2>/dev/null
done
marker_set "$TARGET" \
  || { rm -f "$KEYFILE"; die "slot $TARGET could not be marked incomplete before its write: nothing was written"; }
WRITE_STARTED=yes
fwup -a -d "$ROOT/dev/mmcblk2p$TARGET" -i "$FW_FILE" -t "$TASK" -p "$KEYFILE" \
  || { rm -f "$KEYFILE"; die "fwup apply failed - slot $TARGET is now undefined and marked incomplete, the boot selection is unchanged"; }
rm -f "$KEYFILE"

# --- post-write verify --------------------------------------------------------
MP=$(mktemp -d /tmp/ffinstall.verify.XXXXXX) || die "cannot create temp dir"
# What the media holds, not the page cache the write just filled.
sync
{ echo 3 > "$ROOT/proc/sys/vm/drop_caches"; } 2>/dev/null
mount -o ro,noload -t ext4 "$ROOT/dev/mmcblk2p$TARGET" "$MP" || die "new rootfs does not mount"
NEWVER=$(first_line "$MP/etc/forgefirm-version")
[ -n "$NEWVER" ] || { umount "$MP"; die "new rootfs has no ForgeFIRM version stamp"; }
[ -f "$MP/boot/zImage" ] || { umount "$MP"; die "new rootfs has no kernel"; }
# The device tree this board's U-Boot loads must be in the image: without
# it the next boot stops in U-Boot. The slot keeps its incomplete mark, so
# nothing selects it.
board_dtb_ok "$MP" \
  || { umount "$MP"; die "$DTB_WHY: the boot selection is unchanged, and slot $TARGET stays marked incomplete"; }

# Take ffboot (the factory-side boot-slot tool) from the rootfs we just
# signature-verified and mounted read-only - never fetch+exec it from a
# mutable network ref, which would be an unverified code path in an
# otherwise signature-gated install.
[ -s "$MP/usr/sbin/ffboot" ] \
  || { umount "$MP"; die "new rootfs does not contain /usr/sbin/ffboot"; }
cp "$MP/usr/sbin/ffboot" "$ROOT/data/ffboot.new" \
  || { umount "$MP"; die "cannot copy ffboot out of the new rootfs"; }

umount "$MP"
rmdir "$MP" 2>/dev/null
echo -e "${ASTERISK}Slot $TARGET now holds ForgeFIRM $NEWVER"
log INFO "apply: slot $TARGET holds ForgeFIRM $NEWVER; the written filesystem verified"

# --- ffboot for the factory side ----------------------------------------------
mv "$ROOT/data/ffboot.new" "$ROOT/data/ffboot" || die "cannot put /data/ffboot in place: the boot selection is unchanged"
chmod +x "$ROOT/data/ffboot" || die "cannot make /data/ffboot executable: the boot selection is unchanged"
marker_clear "$TARGET" || die "the incomplete mark of slot $TARGET could not be cleared: the boot selection is unchanged"

# --- flip the boot selection --------------------------------------------------
echo -e "${ASTERISK}Setting boot to /dev/mmcblk2p$TARGET"
set_env 1 0 "$TARGET" "/dev/mmcblk2p$TARGET" \
  || die "the environment write did not verify: it reads $(env_now), and it may be mixed. If the machine does not start, hold its button at power-on until it turns purple, then let go: the factory recovery starts"
log INFO "boot selection set to slot $TARGET and read back; install complete"
rm -f "$FW_FILE"

echo
echo -e "${BRIGHT}Installation complete.${RESET}"
echo -e "ForgeFIRM $NEWVER starts at the next boot. To go back to the factory firmware"
echo -e "later, use the factory restore (https://docs.forgefirm.org/install/factory-restore/)."
echo
read -n1 -p "Press any key to reboot into OpenGlow/ForgeFIRM..." continue
echo
log INFO "rebooting into ForgeFIRM $NEWVER"
sync
reboot
exit 0
