---
name: fork-ops
description: "Use when restarting the fork gateway, touching controller/planning forks, or debugging autonomy silence."
version: 1.0.0
author: VovaElkTver fork
license: MIT
platforms: [linux]
metadata:
  hermes:
    tags: [autonomy, gateway, restart, controller, planning]
    related_skills: [controller-protocol, supervisor-protocol, hermes-agent]
---

# Fork lifecycle ops: restarts, drains, ticker ownership, alerts

Operational contracts of this fork's managed-autonomy core. All of it is
enforced by code (`controller/ticker.py`, `controller/alerts.py`,
`gateway/planning_fork.py`, `hermes_cli/plugins_discovery.py`) — these are the
rules to follow, not preferences.

## Gateway restarts are drain-aware — always

Planning forks (`session_branches` rows created by the multistage planner /
interim-fork) are children of the gateway process. A naked
`systemctl --user restart hermes-gateway.service` can amputate live forks.

Never restart by hand while forks may be alive. Use the drain-aware cycle:

```
~/.hermes/scripts/gw_cycle_planning.sh [max_wait_s]   # default 600
```

It reads `ActiveEnterTimestamp` (BOOT_EPOCH), counts forks from
`~/.hermes/planning-runs/events.jsonl` created after boot without a terminal
event, waits until they finish (or the budget expires), then stops/starts.
Backstop: systemd drop-in `TimeoutStopSec=600`
(`~/.config/systemd/user/hermes-gateway.service.d/timeout.conf`) so SIGTERM
drain cannot be cut off earlier than the legal runtime of a child.

For long-running external actions, arm a detached/transient unit instead of
in-process sleep — the caller's own death must not cancel the restart.

Terminal fork events (the elif-chain in any drain tooling depends on exact
names): `planning.fork.created` / `.joined` / `.failed` / `.spawn_failed`,
each carrying `branch_id`. No terminal event ⇒ the fork counts as open.

## Boot-sweep: reconciliation, not retry

On every boot `sweep_orphaned_planning_forks()` (gateway/planning_fork.py)
marks fork rows whose parent lost them to a restart: status `joined`,
work-note to the parent, journal line `planning.fork.joined` with
`join_strategy=boot_sweep`. It reports facts; it does NOT auto-resume the
interrupted task — a human decides on retries. If drain tooling waits for
terminal events, the sweep MUST emit one per orphaned row (else the waiter
hangs forever).

## Ticker ownership (flock + owner row + reclaim watch)

The autonomy ticker is a gateway singleton by two gates: process flock
(`~/.hermes/run/controller-ticker.lock`) and the CAS'd `kind='owner'` row in
`ctl_ticker_heartbeat`. Since `69755cd31e` a third layer exists — the
reclaim-watch daemon thread: every `RECLAIM_WATCHDOG_S`=60 s it checks for a
live local ticker; when absent AND the holder pid is dead (or its row stale,
>2× pump interval) it retakes ownership **at runtime** — no gateway restart
needed — emitting `ticker-reclaimed` plus a deduped `ticker-reclaim` chat line.

Known asymmetry: a *foreign* dead pid is detected instantly via liveness; a
*locally wedged* ticker thread whose pid is still alive is reclaimed only via
the stale-ts branch (>600 s). Do not "fix" by weakening the CAS.

The watch survives ticker-thread death inside the same process by design; do
not kill it between tests (fixture calls `_stop_reclaim_watchdog()`).

## Fail-loud plugin gate (enabled-vs-disabled contradiction)

If `controller.enabled=true` (top-level, per-entry, or env) while the
plugin is listed in `plugins.disabled`, discovery logs a WARNING at the loader
gate ("managed autonomy ... will NOT run"). Managed autonomy silently OFF was
a real outage shape: check both flags before assuming a code bug.

## Alert semantics: digest change vs backoff ladder

`maybe_alert()` always fires on a digest CHANGE. An UNCHANGED digest re-fires
only inside its backoff window's remainder and only when `backoff_s > 0`
(`backoff_s * 2**count`, capped daily). Consequences:

- human-gates pass `backoff_s=3600` — a persisting gate re-nags hourly, not
  every 5-min pump tick;
- `backoff_s=0` callers (pump-stale, machine-park, resume-interrupted...)
  intentionally re-fire each window — do not add a global default;
- recovery clears the ledger (`clear_alert`), so recurrence is news again.

Alert templates live in `ALERT_TEMPLATES` (fixed wording); new alert kinds
are added there, never as inline f-strings.

## Surfaces you will be asked about

| Thing | Where |
|---|---|
| branch registry | `state.db session_branches` (`status`, `join_strategy`, `joined_at`) |
| ticker liveness / owner | `state.db ctl_ticker_heartbeat` (`pump`, `watchdog`, `owner`, `ticker-start/-stop`) |
| alert dedupe ledger | `state.db ctl_alert_state` |
| fork event stream | `~/.hermes/planning-runs/events.jsonl` (+ `works.jsonl`, `run-NNNN/`) |
| drain script / drop-in | `~/.hermes/scripts/gw_cycle_planning.sh`, systemd `timeout.conf` |

Config discipline: `~/.hermes/config.yaml` sits under the config-protection
hook — mutate only via `hermes config set`; read via `load_config_readonly()`.
