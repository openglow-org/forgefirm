#!/bin/sh
# Copyright 2026 514 LLC d/b/a OpenGlow
# Written by Scott Wiederhold
# https://community.openglow.org
# SPDX-License-Identifier:    MIT
# Runs ON the board while a cloud session is live: blackhole every
# established :443 peer and break name resolution for N seconds, then
# restore both. The cloud client must notice the dead socket, exit toward
# stopped-and-safe, and the supervisor's respawn must reconnect once the
# network is back. Usage: netblip.sh [seconds]   (default 75)
N=${1:-75}
PEERS=$(netstat -tn 2>/dev/null | awk '$6=="ESTABLISHED" && $5 ~ /:443$/ {split($5,a,":"); print a[1]}' | sort -u)
echo "blip $N s; peers: $PEERS"
cp /etc/resolv.conf /tmp/resolv.conf.keep
for p in $PEERS; do ip route add blackhole "$p/32" 2>/dev/null && echo "blackholed $p"; done
echo "nameserver 127.0.0.2" > /etc/resolv.conf
T0=$(date +%s)
sleep "$N"
for p in $PEERS; do ip route del blackhole "$p/32" 2>/dev/null; done
cp /tmp/resolv.conf.keep /etc/resolv.conf
echo "restored after $(( $(date +%s) - T0 )) s"
