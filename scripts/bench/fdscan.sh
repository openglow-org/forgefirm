#!/bin/sh
# Copyright 2026 514 LLC d/b/a OpenGlow
# Written by Scott Wiederhold
# https://community.openglow.org
# SPDX-License-Identifier:    MIT
# Runs ON the board (not on the image: copy it to /tmp there). While
# forgectrl spawns helper children (the update check's curl, the
# snapshot's media-ctl/v4l2-ctl), scan every child for a descriptor on the
# pulse device. Expected: none - only the controller inherits
# /dev/glowforge (F_SETFD cleared for that one spawn); every other child
# gets the O_CLOEXEC default. A scan that saw no helper child (the
# controller, itself a child of forgectrl, does not count) proves nothing
# and fails. The update check reads only the GitHub API's
# latest-release JSON; no release asset is fetched.
# Usage: fdscan.sh <panel-token>   Exit: 0 pass, 1 fail.
# FDSCAN_TICKS: the scans at 10 Hz (default 60, about 6 s).
# FDSCAN_PROC: the process table read (default /proc).
TOK="$1"
TICKS=${FDSCAN_TICKS:-60}
PROC=${FDSCAN_PROC:-/proc}
P=$(pidof forgectrl)
[ -n "$P" ] || { echo "forgectrl not running"; exit 1; }
# The controller: forgectrl's child running the GRBL controller or, in
# cloud mode, the cloud client.
CTRL=""
for c in $(pgrep -P "$P"); do
  case "$(cat "$PROC/$c/cmdline" 2>/dev/null | tr '\0' ' ')" in
    *grblHAL_glowforge*|*gfcloud.py*) CTRL=$c;;
  esac
done
echo "forgectrl pid $P, controller pid ${CTRL:-none}"
T=$(mktemp -d) || exit 1
trap 'rm -rf "$T"' EXIT
# Kick the helpers off in the background.
( curl -s -X POST -H "X-ForgeFIRM-Token: $TOK" http://127.0.0.1:${FORGECTRL_PORT:-80}/update/check >"$T/upd.out" 2>&1 ) &
( curl -s -o "$T/snap.jpg" http://127.0.0.1:${FORGECTRL_PORT:-80}/cam/snapshot ) &
hits=0; seen=0; names=""
i=0
while [ $i -lt "$TICKS" ]; do
  for c in $(pgrep -P "$P"); do
    # The controller is a child of forgectrl too, and the one meant to
    # hold the device: only the helpers count as scanned.
    [ "$c" = "$CTRL" ] || seen=$((seen+1))
    n=$(cat "$PROC/$c/comm" 2>/dev/null)
    names="$names $n"
    if ls -l "$PROC/$c/fd" 2>/dev/null | grep -q glowforge; then
      if [ "$c" != "$CTRL" ]; then hits=$((hits+1)); echo "HIT: child $c ($n) holds the pulse device"; ls -l "$PROC/$c/fd" | grep glowforge; fi
    fi
  done
  i=$((i+1)); usleep 100000 2>/dev/null || sleep 0.1
done
wait
echo "children observed: $(echo $names | tr ' ' '\n' | sort | uniq -c | tr '\n' ';')"
echo "controller holds pulse fd: $(ls -l "$PROC/$CTRL/fd" 2>/dev/null | grep -c glowforge)"
echo "update/check reply: $(head -c 200 "$T/upd.out" 2>/dev/null)"
echo "snapshot bytes: $(wc -c < "$T/snap.jpg" 2>/dev/null)"
if [ "$seen" -eq 0 ]; then
  echo "no helper child of forgectrl was seen during the scan: nothing was checked  (FAIL)"
  exit 1
fi
echo "non-controller children holding the pulse device: $hits  ($([ $hits -eq 0 ] && echo PASS || echo FAIL))"
[ "$hits" -eq 0 ]
