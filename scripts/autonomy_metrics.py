#!/usr/bin/env python3
"""scripts/autonomy_metrics.py — TЗ thinking-enable S0: the autonomy meter.

Read-only SQL over ``state.db`` (or a snapshot of it): the seven metrics of
TЗ thinking-enable S0 (README, раздел «thinking-enable (stage004)»), computed
from EXISTING tables only — no new state, no new writes, no threads/timers.
The script itself never writes to the database.

Usage:
    python3 scripts/autonomy_metrics.py --db ~/.hermes/state.db --json
    python3 scripts/autonomy_metrics.py --db ~/.hermes/state.db --markdown
    python3 scripts/autonomy_metrics.py --db snap.db --markdown --deliver

Determinism: run against a SNAPSHOT (``sqlite3 -readonly state.db ".backup
snap.db"``). The live instance writes continuously; two runs a minute apart
drift (measured while writing the TЗ: causal_links 7 115 -> 7 124,
transport_obs 25 151 -> 25 252). The baseline recorded in
journal/20261117-thinking-enable.md points at the snapshot, not the live DB.

Metric definitions (TЗ S0, all from existing tables):
  M1  completed_without_human / completed — no human command between draft
      and acceptance. Human command = ctl_event(origin in human|user, task_id
      set) or ctl_mandate by a non-agent actor inside the task's lifetime.
  M2  median steps and median wall-time to first needs_human/blocked.
  M3  share of "done" carrying a machine predicate (acceptance.validation_cmd
      declared in the latest contract) and non-empty criteria_covered
      (catcher of false done). Both sub-metrics reported separately.
  M4  self-initiated vs human-initiated wakes: sessions.created_source
      breakdown + ctl_event.origin breakdown.
  M5  pending-acceptance depth: open vs closed registry records (+24 h
      delta) — "open must not grow faster than closed" as a METRIC.
  M6  cognitive table counts: investigation_*, ctl_todo, ctl_followup,
      ctl_review with reviewer='agent'.
  M7  cost of one human intervention: turns (ctl_run count) and tokens per
      task, with vs without intervention. Tokens: NOT MEASURABLE — the
      schema carries no run->session link, reported as null, not as 0.

Empty cognitive tables yield zeros, never an exception.
"""
from __future__ import annotations

import argparse
import datetime as _dt
import json
import os
import sqlite3
import statistics
import sys

_HUMAN_ORIGINS = ("human", "user")
_AGENT_ORIGINS = ("agent", "controller.ticker", "causal", "supervisor")
_OPEN_STATES = ("draft", "ready", "running", "blocked", "paused",
                "needs_human", "verifying", "failed")
_CLOSED_STATES = ("completed", "cancelled")


def _q(conn, sql, args=()):
    try:
        return conn.execute(sql, args).fetchall()
    except sqlite3.Error:
        return []  # missing table (fresh test DB) = zero, not a crash


def _count(conn, table: str, where: str = "") -> int:
    sql = f"SELECT COUNT(*) FROM {table}" + (f" WHERE {where}" if where else "")
    try:
        row = conn.execute(sql).fetchone()
        return int(row[0]) if row else 0
    except sqlite3.Error:
        return 0


def _median(values: list) -> float | None:
    return round(statistics.median(values), 1) if values else None


def _task_completed_ts(conn, task_id: str) -> float | None:
    rows = _q(conn,
              "SELECT ts FROM ctl_state_transition WHERE task_id=? AND to_state='completed'"
              " ORDER BY ts DESC LIMIT 1", (task_id,))
    return rows[0][0] if rows else None


def _has_human_command(conn, task_id: str, lo: float, hi: float) -> bool:
    """Human command = an explicit human/supervisor-visible command event on
    the task, or a mandate issued by a non-agent actor inside the window.
    (Permission-epoch grants are infrastructure, not task commands: the task
    did not start without them, so they are not 'between draft and
    acceptance'.)"""
    if _q(conn, "SELECT 1 FROM ctl_event WHERE task_id=? AND origin IN (?,?) "
                "AND ts >= ? AND ts <= ? LIMIT 1",
          (task_id, *_HUMAN_ORIGINS, lo, hi)):
        return True
    return bool(_q(conn,
                   "SELECT 1 FROM ctl_mandate WHERE task_id=? AND issued_at >= ? "
                   "AND issued_at <= ? AND actor NOT IN (?) LIMIT 1",
                   (task_id, lo, hi, "agent")))


def m1_completed_without_human(conn) -> dict:
    rows = _q(conn, "SELECT task_id, created_at, updated_at FROM ctl_task "
                    "WHERE state='completed'")
    per_task = []
    for task_id, created_at, _updated in rows:
        done = _task_completed_ts(conn, task_id) or _updated
        per_task.append(_has_human_command(conn, task_id, created_at, done))
    n = len(per_task)
    without = n - sum(per_task)
    return {"completed": n,
            "completed_without_human": without,
            "share": round(without / n, 3) if n else None,
            "note": "human command = ctl_event(origin human|user on task) or "
                    "non-agent ctl_mandate inside the task lifetime"}


def m2_median_to_stop(conn) -> dict:
    """Median steps and wall-time to the first needs_human/blocked transition."""
    rows = _q(conn,
               "SELECT t.task_id, t.created_at, MIN(tr.ts) stop_ts FROM ctl_task t "
               "JOIN ctl_state_transition tr ON tr.task_id=t.task_id "
               "WHERE tr.to_state IN ('needs_human','blocked') "
               "GROUP BY t.task_id")
    steps_list, time_list = [], []
    for task_id, created_at, stop_ts in rows:
        runs = _q(conn, "SELECT COUNT(*) FROM ctl_run WHERE task_id=? "
                        "AND started_at IS NOT NULL AND started_at <= ?",
                   (task_id, stop_ts))
        if runs:
            steps_list.append(int(runs[0][0]))
        time_list.append(round(stop_ts - created_at, 1))
    return {"tasks_stopped": len(rows),
            "median_steps": _median(steps_list),
            "median_seconds_to_stop": _median(time_list)}


def m3_machine_predicate_share(conn) -> dict:
    """Catcher of false done: of the completed, which carry a declared
    machine predicate and non-empty criteria coverage?"""
    rows = _q(conn, "SELECT t.task_id FROM ctl_task t WHERE t.state='completed'")
    with_pred, with_covered = 0, 0
    for (task_id,) in rows:
        fields = _q(conn, "SELECT fields FROM ctl_contract_revision WHERE task_id=? "
                          "ORDER BY version DESC LIMIT 1", (task_id,))
        try:
            contract = json.loads(fields[0][0]) if fields else {}
        except (ValueError, TypeError):
            contract = {}
        acc = contract.get("acceptance")
        if isinstance(acc, dict) and (acc.get("validation_cmd")
                                       or acc.get("validation_cmd_path")):
            with_pred += 1
        covered = _q(conn, "SELECT 1 FROM ctl_review WHERE step_id IN "
                           "(SELECT step_id FROM ctl_step WHERE task_id=?) "
                           "AND json_extract(detail, '$.criteria_covered') IS NOT NULL "
                           "LIMIT 1", (task_id,))
        if covered:
            with_covered += 1
    n = len(rows)
    return {"completed": n,
            "with_validation_cmd_declared": with_pred,
            "share_with_predicate": round(with_pred / n, 3) if n else None,
            "with_criteria_covered": with_covered,
            "share_with_coverage": round(with_covered / n, 3) if n else None,
            "note": "criteria_covered: S2.5 adds the column; until then the "
                    "review detail may carry it as JSON"}


def m4_wake_origin(conn) -> dict:
    created = {r[0] or "unknown": int(r[1])
               for r in _q(conn, "SELECT created_source, COUNT(*) FROM sessions "
                                  "GROUP BY 1")}
    event_origin = {r[0] or "unknown": int(r[1])
                    for r in _q(conn, "SELECT origin, COUNT(*) FROM ctl_event GROUP BY 1")}
    self_side = sum(v for k, v in created.items() if k in ("cron", "agent-fork", "subagent"))
    human_side = sum(v for k, v in created.items() if k in ("matrix", "cli"))
    unattributed = sum(v for k, v in created.items()
                       if k not in ("cron", "agent-fork", "subagent", "matrix", "cli"))
    return {"sessions_by_created_source": created,
            "self_initiated_sessions": self_side,
            "human_initiated_sessions": human_side,
            "unattributable_sessions": unattributed,
            "ctl_event_by_origin": event_origin,
            "note": "oneshot sessions (controller workers) are unattributable: "
                    "the schema carries no run->session link"}


def m5_pending_depth(conn, now: float) -> dict:
    ph_open = ",".join("?" * len(_OPEN_STATES))
    ph_closed = ",".join("?" * len(_CLOSED_STATES))
    open_rows = _q(conn, f"SELECT state, COUNT(*) FROM ctl_task WHERE state IN ({ph_open}) "
                   "GROUP BY state", _OPEN_STATES)
    closed_rows = _q(conn, f"SELECT state, COUNT(*) FROM ctl_task WHERE state IN ({ph_closed}) "
                    "GROUP BY state", _CLOSED_STATES)
    open_by = {r[0]: int(r[1]) for r in open_rows}
    closed_by = {r[0]: int(r[1]) for r in closed_rows}
    window_start = now - 24 * 3600
    new_open = sum(int(r[0]) for r in _q(conn,
                   f"SELECT COUNT(*) FROM ctl_state_transition WHERE to_state IN ({ph_open}) "
                   "AND ts >= ?",
                   (*_OPEN_STATES, window_start)))
    new_closed = sum(int(r[0]) for r in _q(conn,
                   f"SELECT COUNT(*) FROM ctl_state_transition WHERE to_state IN ({ph_closed}) "
                   "AND ts >= ?",
                   (*_CLOSED_STATES, window_start)))
    total_open, total_closed = sum(open_by.values()), sum(closed_by.values())
    return {"open_by_state": open_by, "closed_by_state": closed_by,
            "open_total": total_open, "closed_total": total_closed,
            "open_over_closed": round(total_open / total_closed, 3) if total_closed else None,
            "delta_24h_open": new_open, "delta_24h_closed": new_closed,
            "rule": "open must not grow faster than closed (delta_24h_open > "
                    "delta_24h_closed while open_total > 0 = breach)"}


def m6_cognitive_tables(conn) -> dict:
    return {
        "investigations": _count(conn, "investigations"),
        "investigation_hypotheses": _count(conn, "investigation_hypotheses"),
        "investigation_probes": _count(conn, "investigation_probes"),
        "investigation_predictions": _count(conn, "investigation_predictions"),
        "investigation_observations": _count(conn, "investigation_observations"),
        "investigation_observations_assessed":
            _count(conn, "investigation_observations", "assessed=1"),
        "ctl_todo": _count(conn, "ctl_todo"),
        "ctl_followup": _count(conn, "ctl_followup"),
        "ctl_review_total": _count(conn, "ctl_review"),
        "ctl_review_by_agent": _count(conn, "ctl_review", "reviewer='agent'"),
        "causal_links": _count(conn, "ctl_causal_link"),
        "transport_observations": _count(conn, "transport_observations"),
    }


def m7_intervention_cost(conn) -> dict:
    rows = _q(conn, "SELECT task_id, created_at, updated_at FROM ctl_task "
                    "WHERE state='completed'")
    with_interv, without_interv = [], []
    for task_id, created_at, _updated in rows:
        done = _task_completed_ts(conn, task_id) or _updated
        turns_rows = _q(conn, "SELECT COUNT(*) FROM ctl_run WHERE task_id=?", (task_id,))
        turns = int(turns_rows[0][0]) if turns_rows else 0
        (with_interv if _has_human_command(conn, task_id, created_at, done)
         else without_interv).append(turns)
    return {
        "turns_per_task_with_intervention":
            {"n": len(with_interv), "median": _median(with_interv),
             "total": sum(with_interv)},
        "turns_per_task_without_intervention":
            {"n": len(without_interv), "median": _median(without_interv),
             "total": sum(without_interv)},
        "tokens": None,
        "tokens_note": "NOT MEASURABLE: no run->session link in the schema "
                       "(ctl_run carries no session_id); reported as null, not 0",
    }


def compute(db_path: str, now: float | None = None) -> dict:
    """All seven metrics from one read-only connection. No writes, ever."""
    uri = f"file:{db_path}?mode=ro"
    conn = sqlite3.connect(uri, uri=True)
    conn.row_factory = sqlite3.Row
    try:
        now = now if now is not None else _dt.datetime.now().timestamp()
        return {
            "generated_at": _dt.datetime.now(_dt.UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "db": os.path.abspath(db_path),
            "metrics": {
                "M1_completed_without_human": m1_completed_without_human(conn),
                "M2_median_to_stop": m2_median_to_stop(conn),
                "M3_machine_predicate_share": m3_machine_predicate_share(conn),
                "M4_wake_origin": m4_wake_origin(conn),
                "M5_pending_depth": m5_pending_depth(conn, now),
                "M6_cognitive_tables": m6_cognitive_tables(conn),
                "M7_intervention_cost": m7_intervention_cost(conn),
            },
        }
    finally:
        conn.close()


def to_markdown(report: dict) -> str:
    """The compact feed form (TЗ: суточный отчёт в ленту)."""
    m = report["metrics"]
    lines = [f"autonomy-metrics {report['generated_at']} (db: {report['db']})",
             ""]
    m1 = m["M1_completed_without_human"]
    lines.append(f"M1 completed без человека: {m1['completed_without_human']}"
                 f"/{m1['completed']} ({m1['share']})")
    m2 = m["M2_median_to_stop"]
    lines.append(f"M2 стоп: задач={m2['tasks_stopped']}, медиана шагов="
                 f"{m2['median_steps']}, до стопа {m2['median_seconds_to_stop']} c")
    m3 = m["M3_machine_predicate_share"]
    lines.append(f"M3 ложные done: done={m3['completed']}, с предикатом="
                 f"{m3['with_validation_cmd_declared']}, с coverage="
                 f"{m3['with_criteria_covered']}")
    m4 = m["M4_wake_origin"]
    lines.append(f"M4 wake: self={m4['self_initiated_sessions']}, "
                 f"human={m4['human_initiated_sessions']}, "
                 f"unattributed={m4['unattributable_sessions']}")
    m5 = m["M5_pending_depth"]
    lines.append(f"M5 pending: open={m5['open_total']} closed={m5['closed_total']} "
                 f"(24h: +{m5['delta_24h_open']}/-{m5['delta_24h_closed']})")
    m6 = m["M6_cognitive_tables"]
    lines.append(f"M6 когнитивное: inv={m6['investigations']}/obs={m6['investigation_observations']}"
                 f"(assessed={m6['investigation_observations_assessed']}), "
                 f"todo={m6['ctl_todo']}, followup={m6['ctl_followup']}, "
                 f"review_agent={m6['ctl_review_by_agent']}, "
                 f"causal={m6['causal_links']}, transport={m6['transport_observations']}")
    m7 = m["M7_intervention_cost"]
    lines.append(f"M7 цена вмешательства: turns c={m7['turns_per_task_with_intervention']['total']}"
                 f"({m7['turns_per_task_with_intervention']['n']} задач), "
                 f"без={m7['turns_per_task_without_intervention']['total']}"
                 f"({m7['turns_per_task_without_intervention']['n']}); "
                 f"tokens: {m7['tokens_note'].split(':')[0]}")
    return "\n".join(lines)


def deliver_onetime(db_path: str, report: dict) -> bool:
    """One-shot feed delivery through the stock outbox port (controller/outbox.py
    + live_ports composition). No new thread, no timer: this process is the
    event; the outbox row is durable and idempotent by msg_id."""
    from controller import live_ports
    from controller import model
    conn = model.connect()
    try:
        target = live_ports.alert_target_from_cfg()
        if not target:
            print("deliver: no controller.alert_target configured — "
                  "report stays in stdout/journal", file=sys.stderr)
            return False
        text = to_markdown(report)
        _effects, outbox = live_ports.build(conn, live_ports.default_deliver(target))
        msg_id = f"autonomy-metrics-{report['generated_at']}"
        queued = outbox.send(msg_id, text, conv_key=f"target:{target}",
                             task_id=None, kind="report")
        # drain now (one-shot context; the pump tick would drain later — the
        # out-of-band drain under the effects' own lock is the same port)
        live_ports.drain_pending(conn, live_ports.default_deliver(target))
        status = outbox.status(msg_id)
        print(f"deliver: outbox {msg_id} -> {status}", file=sys.stderr)
        return bool(queued)
    finally:
        conn.close()


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--db", default=os.path.expanduser("~/.hermes/state.db"),
                    help="state.db path (or snapshot); opened read-only")
    ap.add_argument("--json", action="store_true", help="JSON output")
    ap.add_argument("--markdown", action="store_true", help="markdown output")
    ap.add_argument("--deliver", action="store_true",
                    help="one-shot feed delivery via the stock outbox port")
    a = ap.parse_args(argv)
    if not os.path.exists(a.db):
        print(f"db not found: {a.db}", file=sys.stderr)
        return 2
    report = compute(a.db)
    if a.deliver:
        deliver_onetime(a.db, report)
    print(json.dumps(report, ensure_ascii=False, indent=2) if a.json
          else to_markdown(report))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
