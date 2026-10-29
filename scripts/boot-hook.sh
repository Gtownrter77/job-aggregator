#!/bin/sh
# job-aggregator autostart trigger. Called from shell startup files, the D-Bus
# activation shim and anything else that fires at boot. MUST stay fast, silent
# and non-blocking: it only spawns scripts/boot.sh fully detached and returns.
#   $1 = name of the trigger (for logs/boot.log)
[ -n "${JOBAGG_BOOT_ACTIVE:-}" ] && exit 0          # never recurse
uid=$(id -u 2>/dev/null) || exit 0
[ "$uid" = 1000 ] || [ "$uid" = 0 ] || exit 0       # box (or root -> boot.sh drops to box)
BOOT=/workspace/job-aggregator/scripts/boot.sh
[ -x "$BOOT" ] || exit 0
# Rate limit: at most one spawn per 60 s across all triggers (shells start often).
STAMP=/tmp/.jobagg-boot-hook.$uid.stamp
if [ -f "$STAMP" ]; then
  now=$(date +%s); last=$(stat -c %Y "$STAMP" 2>/dev/null || echo 0)
  [ $((now - last)) -lt 60 ] && exit 0
fi
touch "$STAMP" 2>/dev/null
JOBAGG_BOOT_ACTIVE=1 setsid -f "$BOOT" "--from=${1:-hook}" </dev/null >/dev/null 2>&1
exit 0
