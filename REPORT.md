# Ticker hardening — execution report

Branch `elk/ticker-audit-fixes`, base `42bc79aa5e`, worktree `/home/qwen/hermes-ticker`.
Workorder: `WORKORDER.md` (audit: `docs/critiques/2026-10-28-ticker-pre-launch-audit.md`).
One commit per point; each test proven red on base sources (stash of the touched
source files) and green after the fix.

## Points

1: OK — `dbccf97c11` — watchdog moved to its own daemon thread with its own DB
   connection (`_start_watchdog`/`_watchdog_run`, thread name
   `controller-ticker-watchdog`); `test_watchdog_ticks_and_alerts_while_pump_wedged`.
2: OK — `708e41690a` — whole `_run` body under try/except (log + `ticker-fatal`
   heartbeat), protected finally (stop flag, watchdog join, `ticker-stop`,
   suppressed conn close + ownership release);
   `test_ticker_survives_a_locked_database`.
3: OK — `708e41690a` — `_tick_error` never raises (rollback main conn, report on a
   scratch connection); `_heartbeat` retries 3× with growing `busy_timeout` then
   skips with a debug log — never kills a tick;
   `test_heartbeat_retries_then_skips_without_raising`.
4: OK — `97abc67daf` — `pump_lock`: self-pid holder is never killed (alert
   `pump-lock-stale:...:self-held`), `DEFAULT_STALE_S=7200`, takeover needs a
   second consecutive sighting (`_STALE_SUSPECTS`); new `pump-lock-stale` template;
   `tests/plugins/test_controller_pump_lock.py` (3 tests).
5: OK — `a50a4c8500` — `pump_cli` non-todo path drains through `pump_lock`
   (held → `{"status":"held"}` exit 0; release in finally; heartbeat per scope +
   `on_poll` into `make_worker`); `tests/plugins/test_controller_pump_cli.py` (2 tests).
6: OK — `74df584ec9` — first pump tick at `interval/2` (watchdog keeps its full
   interval); `test_first_pump_tick_waits_half_an_interval`.
7: OK — `aad96a5a9c` — `ctl_ticker_heartbeat kind='owner'` row as data-level
   exactly-once: partial unique index, one CAS claim (`UPDATE ... WHERE ts < ?`),
   per-tick refresh keyed on payload string, `start_default` gated on claim;
   6 tests (double call / reimport / threads race / gating rules / refresh).
8: OK — `aa45411522` — tick-error digest = exception class identity + error kind
   only (message text can no longer flip the digest); `maybe_alert(backoff_s=…)`
   repeat curve 60→120→240… capped 86400 s; healthy tick clears the ledger;
   `tests/plugins/test_controller_alerts.py` (5 tests).
9: OK — `ca35a06bb4` — watchdog step 0 prunes `ctl_ticker_heartbeat` rows older
   than 7 days, owner row excluded; `test_watchdog_prunes_old_heartbeats_keeps_owner`.
10: OK — `3b09130d13` — `pump-gates.json` written tmp+fsync+`os.replace`;
    module-level `read_gates_file` degrades to `None` (log) on torn/missing file;
    `test_gates_file_written_atomically_reader_degrades`.
11: OK — `04edfe08ab` — pump and watchdog cadence on injected monotonic clock
    (`mono_fn`); wall clock only in DB `ts` fields and ages vs stored ts;
    `test_schedule_cadence_survives_frozen_wall_clock`.
12: OK — `6510ee8e32` — `stop()` under the instance lock, joins and clears both
    thread refs, idempotent, `start()` after `stop()` works;
    `test_stop_locked_idempotent_restartable`.

Docs: `c8819b48e4` — `docs/controller.md` ticker section synced (prune, atomic
gates snapshot, digest normalization/backoff).

## Acceptance

A. `pytest tests/ -q -k "controller or ticker or pump" --continue-on-collection-errors`:
   PENDING — see below.
B. `pytest tests/cron -q`: 8 failed, 1483 passed, 2 skipped — the same 8 failures
   as on pristine base `42bc79aa5e` (verified in a separate worktree at the base
   commit); names identical, none from the controller/ticker area.
C. Controller-area suites: 140 passed, 0 failed
   (13 files `tests/plugins/test_controller_*.py`, incl. repaired transport
   simulation `7751d62e5c`).

## What was done OUTSIDE the points

- Schema DDL the ticker referenced but that never shipped on this branch:
  `ctl_ticker_heartbeat` (+ owner partial-unique and kind/ts indexes),
  `ctl_alert_state`, `ctl_run.worker_pid/worker_pgid` columns
  (`hermes_state_common.py`). Without them every tick died on OperationalError,
  making points 1/4/7 unexecutable.
- Worker surface for the same reason: `model.record_run_process`,
  `workers.kill_worker_tree(pid, pgid)`, `make_worker(..., on_poll=...)`,
  pid/pgid recording after `Popen` (`controller/model.py`, `controller/workers.py`).
- `read_gates_file` module function — the reader side of point 10's contract.
- `_guard` clears the `tick-error:{kind}` alert ledger on a healthy tick —
  point 8's "reset after clear" needs a consumer.
- `tests/plugins/test_controller_transport.py` legacy-schema simulation repaired
  (`7751d62e5c`): its end-marker lookup broke on base (marker text moved in the
  DDL earlier), failing `-k controller` acceptance; proven red on base sources
  first.
- `docs/controller.md` ticker-section paragraphs rewritten for threads,
  intervals, owner row, prune, gates atomicity, alert backoff.
- Lint-mandated touch-ups inside touched functions only (annotation dequoting).
- `WORKORDER.md` committed so `git status --porcelain` is empty.
