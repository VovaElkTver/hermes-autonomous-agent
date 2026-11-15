#!/bin/bash
# gw_cycle_planning.sh v3 (15.11) — self-arming drain-aware gateway cycle.
#
# Incident 15.11 18:09 ("orphan-deafness"): a stop+start chain executed from
# INSIDE hermes-gateway.service loses its executor at the stop half — the
# cgroup kill sweeps the shell between the two commands — so StartUnit is
# never issued and the gateway stays down. setsid/nohup do NOT escape a
# cgroup kill; only a separate systemd scope does.
#
# Enforcement:
#   * running inside $UNIT's cgroup and not armed -> relaunch self via
#     `systemd-run --user` transient (own cgroup) and exit 0;
#   * arming failure -> non-zero exit BEFORE any stop (fail-safe: prefer
#     "no restart" over an amputated half-cycle);
#   * GW_CYCLE_DRYRUN=1 exercises every decision without touching systemd.
#
# Test/env knobs: GW_CYCLE_EVENTS, GW_CYCLE_LOG, GW_CYCLE_CGROUP_FILE,
#                 GW_CYCLE_DRYRUN, GW_CYCLE_SELFCHECK_OUT
set -u

UNIT=hermes-gateway.service
MAX_WAIT=${1:-600}
EV="${GW_CYCLE_EVENTS:-/home/qwen/.hermes/planning-runs/events.jsonl}"
LOG="${GW_CYCLE_LOG:-/home/qwen/.hermes/planning-runs/activation.log}"
CGFILE="${GW_CYCLE_CGROUP_FILE:-/proc/self/cgroup}"
DRYRUN="${GW_CYCLE_DRYRUN:-0}"

SELF="$(readlink -f "$0")"
log(){ echo "$(date -Is) $*" >> "$LOG"; }

in_gateway_cgroup(){ grep -q "$UNIT" "$CGFILE" 2>/dev/null; }

# --- mode: inner half of a live selfcheck (runs inside the transient) ---
if [ "${GW_CYCLE_SELFCHECK_OUT:-}" ]; then
  {
    echo "--- inner $(date -Is)"
    cat "$CGFILE"
    if grep -q "$UNIT" "/proc/self/cgroup" 2>/dev/null; then
      echo "VERDICT: FAIL still inside $UNIT"
    else
      echo "VERDICT: OK outside $UNIT"
    fi
  } >> "${GW_CYCLE_SELFCHECK_OUT}"
  exit 0
fi

# --- mode: prove the arming path without stopping anything ---
if [ "${1:-}" = "--selfcheck" ]; then
  OUT="${GW_CYCLE_SELFCHECK_OUT:-/home/qwen/.hermes/planning-runs/selfcheck-$(date +%Y%m%d-%H%M%S).log}"
  if [ "$DRYRUN" = "1" ]; then echo "[dryrun] selfcheck-out=$OUT"; exit 0; fi
  systemd-run --user --unit="gw-selfcheck-$(date +%H%M%S)" --collect \
      --setenv=GW_CYCLE_SELFCHECK_OUT="$OUT" "$SELF" 600 >/dev/null || {
    echo "selfcheck arming failed" >&2; exit 3; }
  echo "$OUT"
  exit 0
fi

# --- the cycle itself ---
count_open() {
python3 - "$EV" "$BOOT_EPOCH" <<'PY'
import json, sys
try:
    lines = [l for l in open(sys.argv[1]) if l.strip()]
except FileNotFoundError:
    print(0); raise SystemExit
boot = float(sys.argv[2] or 0)
created, done = {}, set()
for l in lines:
    e = json.loads(l)
    b = e.get("branch_id") or ""
    ev = e.get("event", "")
    if not isinstance(b, str) or not b:
        continue
    if ev == "planning.fork.created":
        if e.get("ts", 0) > boot:
            created[b] = e["ts"]
    elif any(k in ev for k in ("joined", "failed", "spawn_failed")):
        done.add(b)
print(sum(1 for b in created if b not in done))
PY
}

BOOT_EPOCH=$(date -d "$(systemctl --user show $UNIT -p ActiveEnterTimestamp --value)" +%s 2>/dev/null || echo 0)

ARMED="${ARMED_VIA_SYSTEMD_RUN:-0}"
if in_gateway_cgroup && [ "$ARMED" != "1" ]; then
  # Orphan-trap guard: never stop the unit whose cgroup we are living in.
  if [ "$DRYRUN" = "1" ]; then
    echo "[dryrun] inside $UNIT cgroup -> would re-arm via systemd-run transient"
    exit 0
  fi
  ts=$(date +%Y%m%d-%H%M%S); UNAME="gw-cycle-planning-$ts"
  log "re-arm: executor inside $UNIT cgroup -> spawning transient $UNAME"
  if systemd-run --user --unit="$UNAME" --collect \
        --setenv=ARMED_VIA_SYSTEMD_RUN=1 "$SELF" "$MAX_WAIT"; then
    log "re-arm ok: transient $UNAME owns the cycle; original executor exits"
  else
    log "re-arm FAILED: refusing stop from inside own cgroup (no half-cycle)"
    exit 3
  fi
  exit 0
fi

OPEN=$(count_open)
if [ "${OPEN:-0}" -gt 0 ]; then
  log "drain: $OPEN open fork(s), waiting up to ${MAX_WAIT}s"
  deadline=$(( $(date +%s) + MAX_WAIT ))
  while [ "$(date +%s)" -lt "$deadline" ]; do
    sleep 15
    OPEN=$(count_open)
    [ "${OPEN:-0}" -le 0 ] && break
  done
fi

if [ "$DRYRUN" = "1" ]; then
  echo "[dryrun] would: systemctl --user stop $UNIT; sleep 3; systemctl --user start $UNIT"
  exit 0
fi
systemctl --user stop "$UNIT"
sleep 3
systemctl --user start "$UNIT"
log "gw cycled for planning activation (drain-aware v3, cap ${MAX_WAIT}s)"
