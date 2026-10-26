#!/usr/bin/env bash
# hard_reboot.sh — quit-force for the gateway, then boot it with the chosen identity.
# Detached executor for /reset-safe and /reset: kills the gateway by KILL (no drain,
# no save — sqlite is already committed per turn), sets/clears the one-shot safe boot
# flag, restarts the unit. The gateway must NOT do this to itself: a self-kill loses
# the reply, a self-drain is exactly what /reset refuses to be.
#   usage: hard_reboot.sh safe|full
set -u
UNIT="${HERMES_GATEWAY_UNIT:-hermes-gateway.service}"
HOME_DIR="${HERMES_HOME:-$HOME/.hermes}"
MODE="${1:-full}"

# Give the slash-reply one beat to leave over the wire before we die.
sleep 2

MAIN_PID=$(systemctl --user show "$UNIT" -p MainPID --value 2>/dev/null)
if [ "${MAIN_PID:-0}" != "0" ]; then
    kill -KILL "$MAIN_PID" 2>/dev/null
fi
# Reap any stragglers of this profile's gateway process group (best effort; the
# unit cgroup stop below is the authoritative sweep).
systemctl --user stop "$UNIT" 2>/dev/null

case "$MODE" in
    safe) : > "$HOME_DIR/NEXT_BOOT_SAFE" ;;
    *)    rm -f "$HOME_DIR/NEXT_BOOT_SAFE" ;;
esac

systemctl --user start "$UNIT"
