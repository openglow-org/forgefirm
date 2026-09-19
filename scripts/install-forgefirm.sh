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
# the archive directory; the active factory slot stays bootable.
#
# Usage: install-forgefirm.sh [local-forgefirm.fw]
#   With no argument the latest release .fw is downloaded from GitHub. The
#   download resumes and retries; every run appends its steps to the
#   install log in the ForgeFIRM log tree, which the panel's log export
#   carries.

RELEASE_FW_URL="https://github.com/openglow-org/forgefirm/releases/latest/download/forgefirm.fw"
ARCHIVE_DIR="/data/forgefirm/archive"
FW_FILE="/data/forgefirm/forgefirm.fw"
MIN_DATA_FREE_KB=300000
LOG_DIR="/data/log/forgefirm/install"
LOG_FILE="$LOG_DIR/install.log"
DL_ATTEMPTS=5                   # download tries before the install gives up
DL_DELAYS="5 15 30 60"          # seconds to wait before tries 2, 3, 4, 5

# ForgeFIRM release-signing public key (raw 32-byte Ed25519, the format the
# factory's fwup 0.14.2 expects).
PUBKEY='\x7c\x7c\x3f\x37\x91\xff\xe6\xdb\x81\xc6\x34\x41\x4b\x6f\xab\xed\x14\x65\xfb\x29\x25\xb6\xb1\x63\xb2\x1d\x38\xcb\xfb\x85\x91\x75'

LIGHTRED="\033[1;31m"
YELLOW="\033[1;33m"
BRIGHT="\033[1;39m"
RESET="\033[0m"
ASTERISK="${LIGHTRED}✺${RESET}"

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

die () {
  log ERR "install failed: $1"
  echo
  echo -e "${LIGHTRED}!! INSTALL FAILED:${RESET} $1"
  echo -e "${LIGHTRED}!! No boot change was made unless stated otherwise. Fix and re-run.${RESET}"
  exit 1
}

stop_gf_services () {
  echo -n -e "${ASTERISK}Stopping Glowforge services"
  for SVC in glowforge glowforge-datalogger glowforge-updater bugeggs; do
    sv stop /sv/$SVC 2>/dev/null >/dev/null; echo -n "."
    sv stop /sv/$SVC/log 2>/dev/null >/dev/null; echo -n "."
  done
  echo "."
}

# archive_dev <src-device> <out.img.gz>: dd|gzip with a live progress
# line (compressed MB so far - old busybox dd has no status=progress).
# dd's exit status is captured via a file so a read failure is not
# masked by gzip succeeding on truncated input. The archive is written
# as <out>.part and renamed only when both ends succeeded: a run that
# dies mid-archive (power, a dropped SSH session) leaves nothing under
# the final name for a rerun to mistake for a complete archive.
archive_dev () {
  RC_FILE=$(mktemp /tmp/ffinstall.rc.XXXXXX) || return 1
  rm -f "$RC_FILE"
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
  if [ "$GZRC" = "0" ] && [ "$DDRC" = "0" ]; then
    mv "$PART" "$2"
  else
    rm -f "$PART"
    return 1
  fi
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


# slot_probe <1|2>: sets S_TYPE (factory|forgefirm|empty|unknown),
# S_VER (the build datetime, used for archive naming), and S_FWVER (the
# semantic FIRMWARE_VERSION, for display; factory only). The active slot
# is read from the running rootfs; others are mounted read-only under
# /tmp (newer factory firmware has no /factory/imgN mounts, and the
# rootfs is read-only). Sets S_TYPE=empty for a mountable-but-unknown
# filesystem and S_TYPE=unknown when the slot will not even mount.
slot_probe () {
  S_TYPE=unknown; S_VER=""; S_FWVER=""; S_MOUNTED=""
  if [ "$1" = "$ACTIVE" ]; then
    RD=""
  else
    RD=$(sed -n "s|^/dev/mmcblk2p$1 \([^ ]*\).*|\1|p" /proc/mounts | head -n 1)
    if [ -z "$RD" ]; then
      S_MOUNTED=yes
      RD=$(mktemp -d /tmp/ffinstall.probe.XXXXXX) || return 1
      mount -o ro -t ext4 "/dev/mmcblk2p$1" "$RD" 2>/dev/null \
        || { rmdir "$RD" 2>/dev/null; S_TYPE=unknown; return 0; }
    fi
  fi
  if [ -f "$RD/etc/forgefirm-version" ]; then
    S_TYPE=forgefirm; S_VER=$(cat "$RD/etc/forgefirm-version")
  elif [ -f "$RD/etc/version" ]; then
    S_TYPE=factory; S_VER=$(cat "$RD/etc/version")
    S_FWVER=$(sed -n 's/^FIRMWARE_VERSION[[:space:]]*=[[:space:]]*\([^[:space:]]*\).*/\1/p' \
              "$RD/etc/build" 2>/dev/null)
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

# Verified atomic env flip (all four variables, classic u-boot-tools script
# format first - that is what factory firmware ships - then libubootenv
# format, then per-variable writes; read-back verified in every case).
# Config selection matches ffboot: newer factory firmware's generic
# fw_env.config points at the wrong device; its per-device
# fw_env_mmcblk2.config is the correct one for the eMMC environment.
if [ -f "/etc/fw_env_mmcblk2.config" ] && [ ! -d "/factory" ]; then
  FWCONFIG="/etc/fw_env_mmcblk2.config"
else
  FWCONFIG="/etc/fw_env.config"
fi

env_get () { fw_printenv -c "$FWCONFIG" -n "$1" 2>/dev/null; }

env_verify () {
  [ "$(env_get mmcdev)" = "$1" ] && [ "$(env_get mmchwpart)" = "$2" ] && \
  [ "$(env_get mmcpart)" = "$3" ] && [ "$(env_get mmcroot)" = "$4" ]
}

set_env () {
  SCRIPT=$(mktemp /tmp/ffinstall.env.XXXXXX) || return 1
  printf 'mmcdev %s\nmmchwpart %s\nmmcpart %s\nmmcroot %s\n' "$1" "$2" "$3" "$4" > "$SCRIPT"
  fw_setenv -c "$FWCONFIG" -s "$SCRIPT" 2>/dev/null
  if env_verify "$1" "$2" "$3" "$4"; then rm -f "$SCRIPT"; return 0; fi
  printf 'mmcdev=%s\nmmchwpart=%s\nmmcpart=%s\nmmcroot=%s\n' "$1" "$2" "$3" "$4" > "$SCRIPT"
  fw_setenv -c "$FWCONFIG" -s "$SCRIPT" 2>/dev/null
  rm -f "$SCRIPT"
  if env_verify "$1" "$2" "$3" "$4"; then return 0; fi
  fw_setenv -c "$FWCONFIG" mmcdev "$1"    && \
  fw_setenv -c "$FWCONFIG" mmchwpart "$2" && \
  fw_setenv -c "$FWCONFIG" mmcpart "$3"   && \
  fw_setenv -c "$FWCONFIG" mmcroot "$4"
  env_verify "$1" "$2" "$3" "$4"
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
log INFO "run start: installer md5=${INSTALLER_MD5:-unknown}, firmware source: ${1:-latest release}"

echo
echo -e "${LIGHTRED} ✺┈┈┈┈┈┈${RESET}"
echo -e "${BRIGHT}Open${RESET}Glow ForgeFIRM Installation Tool"
echo

# --- pre-flight ---------------------------------------------------------------
[ -f /etc/version ] && [ -d /glowforge ] \
  || die "this script must be run from the FACTORY firmware"
command -v fwup >/dev/null || die "fwup not found on this system"
command -v fw_setenv >/dev/null || die "fw_setenv not found on this system"
[ -f "$FWCONFIG" ] || die "$FWCONFIG not found"

BOOTED=$(sed -n 's/.*root=\([^ ]*\).*/\1/p' /proc/cmdline)
case "$BOOTED" in
  /dev/mmcblk2p1) ACTIVE=1; TARGET=2; TASK=upgrade.b ;;
  /dev/mmcblk2p2) ACTIVE=2; TARGET=1; TASK=upgrade.a ;;
  *) die "booted from $BOOTED - expected factory eMMC slot 1 or 2" ;;
esac

for N in 1 2; do
  SZ=$(cat /sys/class/block/mmcblk2p$N/size 2>/dev/null)
  [ "$SZ" = "409600" ] || die "slot $N is not the 200 MiB factory layout (size=$SZ)"
done

FREE_KB=$(df -k /data | tail -1 | awk '{print $4}')
[ "$FREE_KB" -ge "$MIN_DATA_FREE_KB" ] 2>/dev/null \
  || die "need ${MIN_DATA_FREE_KB} KB free on /data, have ${FREE_KB:-unknown}"
log INFO "pre-flight: factory $(cat /etc/version), booted slot $ACTIVE, target slot $TARGET, ${FREE_KB} KB free on /data, $(curl -V 2>/dev/null | head -n 1)"

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
echo -e "The factory firmware in slot $ACTIVE stays installed and bootable."
echo

# What is in the target slot, and will it be archived first? The archive
# step below backs up factory images; anything else in the target slot
# is overwritten without a backup, so make that explicit.
slot_probe "$TARGET"
TARGET_DESC=$(slot_desc)
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

log INFO "operator confirmed; stopping the Glowforge services"
stop_gf_services

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
  archive_dev /dev/mmcblk2p$N "$ARC" \
    || { rm -f "$ARC"; die "archiving slot $N failed"; }
  echo "$(date '+%Y-%m-%d %H:%M:%S') slot$N factory $S_VER ver=${S_FWVER:-unknown} $(basename $ARC) md5=$(md5sum "$ARC" | cut -d' ' -f1)" >> "$ARCHIVE_DIR/manifest"
  log INFO "archive: slot $N (factory $S_VER) -> $(basename "$ARC"), $(wc -c < "$ARC") bytes"
done
for B in 0 1; do
  ARC="$ARCHIVE_DIR/recovery-boot$B.img.gz"
  archived_ok "$ARC" && continue
  echo -e "${ASTERISK}Archiving recovery boot$B:"
  archive_dev /dev/mmcblk2boot$B "$ARC" \
    || { rm -f "$ARC"; die "archiving boot$B failed"; }
  echo "$(date '+%Y-%m-%d %H:%M:%S') boot$B recovery - $(basename $ARC) md5=$(md5sum "$ARC" | cut -d' ' -f1)" >> "$ARCHIVE_DIR/manifest"
  log INFO "archive: boot$B -> $(basename "$ARC"), $(wc -c < "$ARC") bytes"
done

# --- acquire the ForgeFIRM .fw ------------------------------------------------
mkdir -p /data/forgefirm
if [ -n "$1" ]; then
  [ -s "$1" ] || die "local firmware file '$1' not found"
  cp "$1" "$FW_FILE" || die "cannot copy '$1' to $FW_FILE"
  echo -e "${ASTERISK}Using local firmware file: $1"
  log INFO "firmware: local file $1, $(wc -c < "$FW_FILE") bytes"
else
  echo -e "${ASTERISK}Downloading latest OpenGlow/ForgeFIRM release:"
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
META=$(fwup -m -i "$FW_FILE")
M_PRODUCT=$(echo "$META" | sed -n 's/^meta-product="\(.*\)"$/\1/p')
M_PLATFORM=$(echo "$META" | sed -n 's/^meta-platform="\(.*\)"$/\1/p')
M_VERSION=$(echo "$META" | sed -n 's/^meta-version="\(.*\)"$/\1/p')
[ "$M_PRODUCT" = "ForgeFIRM firmware" ] \
  || { rm -f "$KEYFILE"; die "archive product is '$M_PRODUCT', not ForgeFIRM firmware - wrong archive"; }
[ "$M_PLATFORM" = "glowforge" ] \
  || { rm -f "$KEYFILE"; die "archive platform is '$M_PLATFORM', not glowforge - wrong archive"; }
echo -e "${ASTERISK}Archive: $M_PRODUCT $M_VERSION ($M_PLATFORM)"
log INFO "firmware: signature verified; $M_PRODUCT $M_VERSION ($M_PLATFORM)"

# A validly signed OLDER release must never install silently; downgrades
# need an explicit yes (rollback stays possible, just deliberate).
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
    y|Y) log NOTICE "operator accepted the downgrade from $INSTALLED to $M_VERSION" ;;
    *) rm -f "$KEYFILE"; die "downgrade declined" ;;
  esac
fi

# --- apply to the inactive slot -----------------------------------------------
echo -e "${ASTERISK}Writing ForgeFIRM to slot $TARGET (/dev/mmcblk2p$TARGET):"
log INFO "apply: writing $M_VERSION to slot $TARGET"
for M in $(sed -n "s|^/dev/mmcblk2p$TARGET \([^ ]*\).*|\1|p" /proc/mounts); do
  umount "$M" 2>/dev/null
done
fwup -a -d "/dev/mmcblk2p$TARGET" -i "$FW_FILE" -t "$TASK" -p "$KEYFILE" \
  || { rm -f "$KEYFILE"; die "fwup apply failed - slot $TARGET is now undefined, factory slot $ACTIVE is untouched"; }
rm -f "$KEYFILE"

# --- post-write verify --------------------------------------------------------
MP=$(mktemp -d /tmp/ffinstall.verify.XXXXXX) || die "cannot create temp dir"
mount -o ro -t ext4 "/dev/mmcblk2p$TARGET" "$MP" || die "new rootfs does not mount"
NEWVER=$(cat "$MP/etc/forgefirm-version" 2>/dev/null)
[ -n "$NEWVER" ] || { umount "$MP"; die "new rootfs has no ForgeFIRM version stamp"; }
[ -f "$MP/boot/zImage" ] || { umount "$MP"; die "new rootfs has no kernel"; }

# Take ffboot (the factory-side boot-slot tool) from the rootfs we just
# signature-verified and mounted read-only - never fetch+exec it from a
# mutable network ref, which would be an unverified code path in an
# otherwise signature-gated install.
[ -s "$MP/usr/sbin/ffboot" ] \
  || { umount "$MP"; die "new rootfs does not contain /usr/sbin/ffboot"; }
cp "$MP/usr/sbin/ffboot" /data/ffboot.new \
  || { umount "$MP"; die "cannot copy ffboot out of the new rootfs"; }

umount "$MP"
rmdir "$MP" 2>/dev/null
echo -e "${ASTERISK}Slot $TARGET now holds ForgeFIRM $NEWVER"
log INFO "apply: slot $TARGET holds ForgeFIRM $NEWVER; the written filesystem verified"

# --- ffboot for the factory side ----------------------------------------------
mv /data/ffboot.new /data/ffboot
chmod +x /data/ffboot

# --- flip the boot selection --------------------------------------------------
echo -e "${ASTERISK}Setting boot to /dev/mmcblk2p$TARGET"
set_env 1 0 "$TARGET" "/dev/mmcblk2p$TARGET" \
  || die "environment write did not verify - boot selection unchanged; run /data/ffboot -e$TARGET manually"
log INFO "boot selection set to slot $TARGET and read back; install complete"

echo
echo -e "${BRIGHT}Installation complete.${RESET}"
echo -e "To return to factory firmware later: ${BRIGHT}/data/ffboot -e${RESET} (from ForgeFIRM: ${BRIGHT}ffboot -e${RESET})"
echo
read -n1 -p "Press any key to reboot into OpenGlow/ForgeFIRM..." continue
echo
log INFO "rebooting into ForgeFIRM $NEWVER"
sync
reboot
exit 0
