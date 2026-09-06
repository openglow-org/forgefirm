#!/bin/sh
# Runs ON the board. While forgectrl spawns helper children (the update
# check's curl, the snapshot's media-ctl/v4l2-ctl), scan every child for a
# descriptor on the pulse device. Expected: none - only the controller
# inherits /dev/glowforge (F_SETFD cleared for that one spawn); every other
# child gets the O_CLOEXEC default. Usage: fdscan.sh <panel-token>
TOK="$1"
P=$(pidof forgectrl)
[ -n "$P" ] || { echo "forgectrl not running"; exit 1; }
CTRL=$(pidof grblHAL_glowforge)
echo "forgectrl pid $P, controller pid ${CTRL:-none}"
# Kick the helpers off in the background.
( curl -s -X POST -H "X-ForgeFIRM-Token: $TOK" http://127.0.0.1:${FORGECTRL_PORT:-80}/update/check >/tmp/upd.out 2>&1 ) &
( curl -s -o /tmp/snap.jpg http://127.0.0.1:${FORGECTRL_PORT:-80}/cam/snapshot ) &
hits=0; seen=0; names=""
i=0
while [ $i -lt 60 ]; do          # ~6 s of scanning at 10 Hz
  for c in $(pgrep -P "$P"); do
    seen=$((seen+1))
    n=$(cat /proc/$c/comm 2>/dev/null)
    names="$names $n"
    if ls -l /proc/$c/fd 2>/dev/null | grep -q glowforge; then
      if [ "$c" != "$CTRL" ]; then hits=$((hits+1)); echo "HIT: child $c ($n) holds the pulse device"; ls -l /proc/$c/fd | grep glowforge; fi
    fi
  done
  i=$((i+1)); usleep 100000 2>/dev/null || sleep 0.1
done
wait
echo "children observed: $(echo $names | tr ' ' '\n' | sort | uniq -c | tr '\n' ';')"
echo "controller holds pulse fd: $(ls -l /proc/$CTRL/fd 2>/dev/null | grep -c glowforge)"
echo "update/check reply: $(cat /tmp/upd.out | head -c 200)"
echo "snapshot bytes: $(wc -c < /tmp/snap.jpg 2>/dev/null)"
echo "non-controller children holding the pulse device: $hits  ($([ $hits -eq 0 ] && echo PASS || echo FAIL))"
