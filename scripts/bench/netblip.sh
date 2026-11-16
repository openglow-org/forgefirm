#!/bin/sh
# Copyright 2026 514 LLC d/b/a OpenGlow
# Written by Scott Wiederhold
# https://community.openglow.org
# SPDX-License-Identifier:    MIT
# Runs ON the board (not on the image: copy it to /tmp there) while a
# cloud session is live: blackhole every established :443 peer and break
# name resolution for N seconds, then restore both. The cloud client must
# notice the dead socket, exit toward stopped-and-safe, and the
# supervisor's respawn must reconnect once the network is back. The routes
# and resolv.conf come back on every exit, an interrupt, a SIGTERM and a
# lost terminal included. Usage: netblip.sh [seconds]   (default 75)
N=${1:-75}
RESOLV=${NETBLIP_RESOLV:-/etc/resolv.conf}
KEEP=$(mktemp) || exit 1
PEERS=""
added=""
slept=""
T0=$(date +%s)
restore() {
  trap '' INT TERM HUP
  # The background sleep, when one started and has not ended ($! is set
  # the moment it forks).
  [ -z "$slept" ] && [ -n "$!" ] && kill "$!" 2>/dev/null
  for p in $added; do ip route del blackhole "$p/32" 2>/dev/null; done
  if [ -s "$KEEP" ]; then cp "$KEEP" "$RESOLV"; fi
  rm -f "$KEEP"
  echo "restored after $(( $(date +%s) - T0 )) s"
}
trap restore EXIT
trap 'exit 130' INT
trap 'exit 143' TERM
trap 'exit 129' HUP
PEERS=$(netstat -tn 2>/dev/null | awk '$6=="ESTABLISHED" && $5 ~ /:443$/ {split($5,a,":"); print a[1]}' | sort -u)
echo "blip $N s; peers: $PEERS"
cp "$RESOLV" "$KEEP" || exit 1
# Each peer is listed for the restore before its route goes in, so a
# signal during the add still deletes it (deleting a route never added is
# harmless).
for p in $PEERS; do added="$added $p"; ip route add blackhole "$p/32" 2>/dev/null && echo "blackholed $p"; done
echo "nameserver 127.0.0.2" > "$RESOLV"
T0=$(date +%s)
# In the background, so a signal ends the wait at once rather than after the
# sleep; its output goes nowhere, so a reader of this script's never waits on it.
sleep "$N" </dev/null >/dev/null 2>&1 &
wait $! && slept=1
