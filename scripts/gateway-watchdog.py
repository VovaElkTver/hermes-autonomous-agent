#!/usr/bin/env python3
"""Gateway watchdog — crash-loop revert + heartbeat hang-detect + DEAF-STALL (SPEC v1.2 local muscle).

Three detectors, one process, run by a systemd user timer every 30s (separate process
tree — survives a dead gateway):

1. CRASH-LOOP (unchanged semantics, proven): if a locally patched file is git-dirty AND
   NRestarts climbed >= RESTART_THRESHOLD within WINDOW_SECONDS (or unit failed) ->
   revert the patched files and restart the clean build. Cooldown COOLDOWN_SECONDS.

2. HANG (new, INV-1-compliant: zero LLM turns, absence-of-signal not error-search):
   the gateway's _heartbeat_watcher touches $HERMES_HOME/gateway-heartbeat ("pid epoch")
   every ~15s. Unit active + heartbeat stale > HB_STALE_SECONDS for HB_CONFIRM_SAMPLES
   consecutive checks -> hang. An idle gateway logs ZERO lines/min, so log silence must
   NEVER be a liveness signal; the heartbeat file is.

   Action policy (INV-4): every watchdog-initiated restart is recorded in the shared
   journal BUDGET_FILE and capped at BUDGET_MAX per BUDGET_WINDOW. Budget exhausted ->
   write FREEZE_FILE: the hang path stops acting until a human removes the file.
   Default WATCHDOG_HANG_ACTION=0 => SHADOW: decisions are logged, no restarts
   (an experimental subsystem may not touch the message loop without the flag).

   Missing/unreadable heartbeat = UNKNOWN, never assumed dead (fail-visible: re-alerts
   in the log after 20 min, takes no action).

3. DEAF-STALL (L1 receive-pulse sensor, rtx5090-gateway-deafness contour 15.11):
   the gateway's _heartbeat_watcher writes a THIRD token onto the heartbeat file:
   compact JSON {platform -> seconds since that platform's last receive evidence}.
   "unknown" = adapter without a published receive_evidence_age(); "no_adapter" =
   platform whose adapter never got created (the exact 14.11 deaf-boot shape).
   Unit active + a platform's pulse older than DEAF_STALE_SECONDS (or "no_adapter")
   for DEAF_CONFIRM_SAMPLES consecutive checks -> DEAF-STALL, with the SAME action
   ladder as hang (freeze / budget / HANG_ACTION / SHADOW). L3 doctrine order is
   sensor -> heal (R1's reconnect watcher retries the adapter in-process first) ->
   restart -> freeze (human). Absent or unparsable pulse map = UNKNOWN, never
   assumed deaf (old-format two-token heartbeats must not fire it).

Audit: ~/.hermes/logs/gateway-watchdog.log
Test overrides: HERMES_GATEWAY_WD_NR/_ACTIVE/_DIRTY/_HB_AGE/_PULSES/_STATE/_LOG/_REPO.
``--dry-run`` computes decisions without side effects (bookkeeping state still saved).
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import time
from datetime import datetime, UTC
from pathlib import Path

# --- tunables -------------------------------------------------------------
REPO = Path(os.environ.get("HERMES_GATEWAY_WD_REPO", "/home/qwen/hermes-autonomous-agent"))
# Files the local core patch touches (the Sep-2026 split moved the MCP-scoping
# patch from the mcp_tool.py monolith into the mcp_tool_agent sibling, and the
# matrix adapter carries the @all/**kwargs patch). Watch every one: a patch is
# "present" if ANY of these is git-dirty, and a revert restores all of them.
PATCHED = ["tools/mcp_tool_agent.py", "plugins/platforms/matrix/adapter.py"]
UNIT = os.environ.get("HERMES_GATEWAY_WD_UNIT", "hermes-gateway")
HERMES_HOME = Path(os.environ.get("HERMES_HOME") or (Path.home() / ".hermes"))
STATE = Path(os.environ.get(
    "HERMES_GATEWAY_WD_STATE", str(HERMES_HOME / "gateway-watchdog-state.json")))
LOG = Path(os.environ.get(
    "HERMES_GATEWAY_WD_LOG", str(HERMES_HOME / "logs" / "gateway-watchdog.log")))
HEARTBEAT = HERMES_HOME / "gateway-heartbeat"
BUDGET_FILE = HERMES_HOME / "restart-budget.json"
FREEZE_FILE = HERMES_HOME / "gateway-watchdog.freeze"

# Crash-loop: NRestarts climbed by >= RESTART_THRESHOLD within WINDOW_SECONDS.
# The gateway has RestartSec=5, so a true import-crash loop respawns ~every
# 5s (>> threshold); a single manual restart is +1 (<< threshold).
RESTART_THRESHOLD = 4
WINDOW_SECONDS = 90
COOLDOWN_SECONDS = 300
# Hang: heartbeat silent > this while the unit claims to be active. Gateway ticks
# the file every ~15 s; 90 s = six missed ticks. Two consecutive positives before
# acting (de-bounce); with the 30 s timer that is MTTD ~2 min.
HB_STALE_SECONDS = 90
HB_CONFIRM_SAMPLES = 2
BUDGET_MAX = 2
BUDGET_WINDOW = 1800
HANG_ACTION = os.environ.get("WATCHDOG_HANG_ACTION", "0") == "1"  # 0 = shadow mode
UNKNOWN_REALERT = 1200
# Deaf-stall: a platform's receive pulse older than this (or "no_adapter") while
# the unit claims active. Matrix long-poll answers every ~30 s even when idle, so
# 180 s = six missed pulses — far above any legit long-poll timeout, far below
# the 41-minute blind window of 14.11. Overridable per node.
DEAF_STALE_SECONDS = int(os.environ.get("HERMES_GATEWAY_WD_DEAF_SECONDS", "180"))
DEAF_CONFIRM_SAMPLES = 3

GIT = shutil.which("git") or "git"


def _now() -> float:
    return time.time()


def _run(cmd: list[str], timeout: int = 30) -> subprocess.CompletedProcess:
    return subprocess.run(cmd, capture_output=True, text=True, timeout=timeout, check=False)


def read_nr() -> int | None:
    """Current NRestarts counter, or None if unreadable."""
    override = os.environ.get("HERMES_GATEWAY_WD_NR", "").strip()
    if override.isdigit():
        return int(override)
    try:
        out = _run(["systemctl", "--user", "show", UNIT, "-p", "NRestarts", "--value"], 15)
        val = out.stdout.strip()
        return int(val) if val.isdigit() else None
    except Exception:
        return None


def read_active() -> str:
    override = os.environ.get("HERMES_GATEWAY_WD_ACTIVE", "").strip()
    if override:
        return override
    try:
        out = _run(["systemctl", "--user", "show", UNIT, "-p", "ActiveState", "--value"], 15)
        return out.stdout.strip()
    except Exception:
        return "unknown"


def read_hb_age() -> float | None:
    """Seconds since the last heartbeat; None = unknown (file missing/unparsable)."""
    override = os.environ.get("HERMES_GATEWAY_WD_HB_AGE", "").strip()
    if override.isdigit():
        return float(override)
    try:
        mtime = HEARTBEAT.stat().st_mtime
        try:
            content_ts = float(HEARTBEAT.read_text().split()[1])
        except Exception:
            content_ts = 0.0
        return max(0.0, _now() - max(mtime, content_ts))
    except Exception:
        return None


def read_receive_pulses() -> dict | None:
    """{platform -> age_seconds | "no_adapter"} from the heartbeat's third token.

    None = unknown: two-token (pre-L1) heartbeat, missing file, unparsable json.
    UNKNOWN is never assumed deaf (fail-visible, INV-1)."""
    override = os.environ.get("HERMES_GATEWAY_WD_PULSES", "").strip()
    if override:
        try:
            got = json.loads(override)
            return got if isinstance(got, dict) else None
        except Exception:
            return None
    try:
        parts = HEARTBEAT.read_text().split(" ", 2)
        if len(parts) < 3:
            return None
        got = json.loads(parts[2])
        return got if isinstance(got, dict) else None
    except Exception:
        return None


def patch_dirty() -> bool:
    """True if any patched file differs from the git baseline (a patch is present)."""
    override = os.environ.get("HERMES_GATEWAY_WD_DIRTY", "").strip()
    if override in ("0", "1"):
        return override == "1"
    try:
        out = _run([GIT, "-C", str(REPO), "status", "--porcelain"] + list(PATCHED), 30)
        return bool(out.stdout.strip())
    except Exception:
        return False


def load_state() -> dict:
    try:
        data = json.loads(STATE.read_text())
        if isinstance(data, dict):
            return data
    except Exception:
        pass
    return {"samples": [], "last_action_ts": 0.0, "hb_streak": 0, "hb_unknown_since": 0.0}


def save_state(st: dict) -> None:
    STATE.parent.mkdir(parents=True, exist_ok=True)
    STATE.write_text(json.dumps(st))


def log(msg: str) -> None:
    try:
        LOG.parent.mkdir(parents=True, exist_ok=True)
        with open(LOG, "a", encoding="utf-8") as fh:
            fh.write(f"[{datetime.now(UTC).isoformat()}] {msg}\n")
    except Exception:
        pass


def budget_count(now: float) -> int:
    try:
        ts = json.loads(BUDGET_FILE.read_text())
        return sum(1 for t in ts if now - t <= BUDGET_WINDOW)
    except Exception:
        return 0


def budget_record(now: float) -> None:
    try:
        ts = [t for t in json.loads(BUDGET_FILE.read_text()) if now - t <= BUDGET_WINDOW]
    except Exception:
        ts = []
    ts.append(now)
    BUDGET_FILE.parent.mkdir(parents=True, exist_ok=True)
    BUDGET_FILE.write_text(json.dumps(ts))


def do_revert_restart(dry_run: bool) -> str:
    if dry_run:
        return (f"[DRY-RUN] would run: git -C {REPO} checkout -- {' '.join(PATCHED)} ; "
                f"systemctl --user restart {UNIT}")
    _run([GIT, "-C", str(REPO), "checkout", "--"] + list(PATCHED), 60)
    _run(["systemctl", "--user", "restart", UNIT], 90)
    budget_record(_now())  # INV-4: общее окно рестартов считают ВСЕ действия watchdog'а
    return f"reverted {' '.join(PATCHED)} and restarted {UNIT}"


def do_restart(dry_run: bool) -> str:
    if dry_run:
        return f"[DRY-RUN] would restart {UNIT} (hang)"
    _run(["systemctl", "--user", "restart", UNIT], 90)
    budget_record(_now())
    return f"restarted {UNIT} (hang)"


def main(argv: list[str]) -> int:
    dry_run = "--dry-run" in argv
    now = _now()
    st = load_state()

    # ---- detector 1: crash-loop (исторический путь, семантика не менялась) ----
    nr = read_nr()
    active = read_active()
    st["samples"] = [s for s in st.get("samples", []) if now - s[0] <= WINDOW_SECONDS]
    if nr is not None:
        st["samples"].append([now, nr])
    climbed = (len(st["samples"]) >= 2
               and (st["samples"][-1][1] - st["samples"][0][1]) >= RESTART_THRESHOLD)
    crash_loop = (active == "failed") or climbed
    if crash_loop and now - st.get("last_action_ts", 0.0) > COOLDOWN_SECONDS:
        if patch_dirty():
            msg = do_revert_restart(dry_run)
            log(f"crash-loop active={active} climbed={climbed} dirty=True -> {msg}")
            if not dry_run:
                st["last_action_ts"] = now
                st["samples"] = []
        else:
            log(f"crash-loop active={active} climbed={climbed} but no patched file "
                "is dirty; nothing to revert (crash has another cause)")

    # ---- detector 2: heartbeat hang (shadow unless WATCHDOG_HANG_ACTION=1) ----
    hb_age = read_hb_age()
    if hb_age is None:
        st["hb_streak"] = 0
        if active == "active":
            if not st.get("hb_unknown_since"):
                st["hb_unknown_since"] = now
            elif (now - st["hb_unknown_since"] > UNKNOWN_REALERT
                  and now - st.get("hb_last_alert", 0.0) > UNKNOWN_REALERT):
                st["hb_last_alert"] = now
                log("heartbeat UNKNOWN > 20 min while unit active — heartbeat patch not loaded?")
    else:
        st["hb_unknown_since"] = 0.0
        hang = (active == "active") and hb_age > HB_STALE_SECONDS
        st["hb_streak"] = st.get("hb_streak", 0) + 1 if hang else 0
        if st["hb_streak"] >= HB_CONFIRM_SAMPLES:
            if FREEZE_FILE.exists():
                log(f"hang hb_age={hb_age:.0f}s — FROZEN (remove {FREEZE_FILE} to resume muscle)")
            elif budget_count(now) >= BUDGET_MAX:
                FREEZE_FILE.write_text(datetime.now(UTC).isoformat() + "\n")
                log(f"hang hb_age={hb_age:.0f}s — BUDGET EXHAUSTED -> FREEZE (human required)")
                st["hb_streak"] = 0
            elif HANG_ACTION:
                msg = do_restart(dry_run)
                log(f"hang hb_age={hb_age:.0f}s action={msg}")
                st["hb_streak"] = 0
            else:
                log(f"hang hb_age={hb_age:.0f}s — SHADOW (WATCHDOG_HANG_ACTION=0, no restart)")
                st["hb_streak"] = 0

    # ---- detector 3: DEAF-STALL (L1 receive-pulse sensor; shadow unless HANG_ACTION) ----
    # Same action ladder as hang: freeze -> budget -> act/SHADOW. In-process heal
    # (R1 reconnect watcher) has the window first: confirmation needs
    # DEAF_CONFIRM_SAMPLES * timer period of sustained staleness.
    pulses = read_receive_pulses()
    if active == "active" and pulses:
        deaf = {p: v for p, v in pulses.items()
                if v == "no_adapter" or (isinstance(v, (int, float)) and v > DEAF_STALE_SECONDS)}
        streaks = st.get("deaf_streaks", {})
        for p in pulses:
            streaks[p] = streaks.get(p, 0) + 1 if p in deaf else 0
        st["deaf_streaks"] = streaks
        confirmed = [p for p in deaf if streaks.get(p, 0) >= DEAF_CONFIRM_SAMPLES]
        if confirmed:
            detail = " ".join(f"{p}={pulses[p]}" for p in sorted(confirmed))
            if FREEZE_FILE.exists():
                log(f"deaf-stall {detail} — FROZEN (remove {FREEZE_FILE} to resume muscle)")
                st["deaf_streaks"] = {}
            elif budget_count(now) >= BUDGET_MAX:
                FREEZE_FILE.write_text(datetime.now(UTC).isoformat() + "\n")
                log(f"deaf-stall {detail} — BUDGET EXHAUSTED -> FREEZE (human required)")
                st["deaf_streaks"] = {}
            elif HANG_ACTION:
                msg = do_restart(dry_run)
                log(f"deaf-stall {detail} action={msg}")
                st["deaf_streaks"] = {}
            else:
                log(f"deaf-stall {detail} — SHADOW (WATCHDOG_HANG_ACTION=0, no restart)")
                st["deaf_streaks"] = {}

    # Bookkeeping is always persisted, dry or not — harmless state, not a side effect.
    # Only destructive revert/restart is gated on dry_run above.
    save_state(st)

    if dry_run:
        print(f"nr={nr} active={active} samples={len(st['samples'])} climbed={climbed} "
              f"dirty={patch_dirty()} hb_age={hb_age} streak={st.get('hb_streak')} "
              f"budget={budget_count(now)} freeze={FREEZE_FILE.exists()} "
              f"hang_flag={HANG_ACTION} pulses={pulses} -> "
              f"{'WOULD REVERT' if (crash_loop and patch_dirty() and now - st.get('last_action_ts', 0.0) > COOLDOWN_SECONDS) else 'no action'}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
