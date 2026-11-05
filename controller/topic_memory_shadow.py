"""controller.topic_memory_shadow — stage003 step 6 shadow ledger.

Shadow mode changes NO behaviour: the legacy MEMORY.md flow keeps running
unimpeded while we COUNT Вл's three error classes as mechanically observable
proxies. Interpretation belongs to the human (acceptance is Вл's, not ours).

  leak_global      a controller-scoped process wrote through a builtin store
                   (memory/user) — the very channel that lets a local
                   correction become global. One row per such write.
  foreign_rule     NOT auto-detectable in v1 (it needs semantics); recorded as
                   a manual annotation via :func:`record` when a human or an
                   audit pass identifies one. Honest empty-by-default signal.
  missing_decision accepted decisions/kinds exist in the task's scope chain but
                   did not make it into the rendered prompt block (overflow cap)
                   — counted per injection render.

All writers are best-effort: shadow telemetry must never break the call it
observes (fail-open, mirrors memory_gate conventions).
"""
from __future__ import annotations

import json
import logging
import time

logger = logging.getLogger(__name__)

KINDS = ("leak_global", "foreign_rule", "missing_decision")


def record(kind: str, scope_id: str | None, detail: dict | None = None,
           *, conn=None) -> None:
    """Best-effort append to ctl_mem_shadow; opens its own connection when the
    caller has none (tool-path hooks do). Never raises."""
    import sqlite3

    if kind not in KINDS:
        logger.debug("shadow: unknown kind %r ignored", kind)
        return
    connection: sqlite3.Connection | None = conn
    try:
        if connection is None:
            from controller import model as ctl_model
            connection = ctl_model.connect()
        payload = json.dumps(detail or {}, ensure_ascii=False)
        connection.execute(
            "INSERT INTO ctl_mem_shadow (shadow_id,kind,scope_id,detail,created_at)"
            " VALUES (?,?,?,?,?)",
            (f"memsh-{time.time_ns():016x}", kind, scope_id, payload, time.time()))
        connection.commit()
    except Exception as exc:
        logger.warning("shadow record(%s) skipped: %s", kind, exc)
        try:
            if conn is None and connection is not None:
                connection.rollback()
        except Exception:
            pass
    finally:
        if conn is None and connection is not None:
            try:
                connection.close()
            except Exception:
                pass


def counts(conn, since: float = 0.0) -> dict[str, int]:
    """Per-kind counters since a timestamp — the number Проверяемость asks for
    («запросы к state.db»): SELECT kind, COUNT(*) grouped, all three keys always present."""
    out = {k: 0 for k in KINDS}
    rows = conn.execute(
        "SELECT kind, COUNT(*) AS n FROM ctl_mem_shadow WHERE created_at>=? GROUP BY kind",
        (since,)).fetchall()
    for row in rows:
        if row["kind"] in out:
            out[row["kind"]] = row["n"]
    return out
