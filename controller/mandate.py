"""controller.mandate — immutable authorization records (port #2).

Ported from dsh-superagent-prototype ``model.ts`` Mandate + ``policy.ts``
revalidate @6337fc8 (audit 2026-11-01; mechanism donor, runtime NOT adopted).
Adapted to this codebase's authorization surface:

- entering ``running`` issues a mandate recording WHO the task acts for
  (owner), WHERE it may act (workdir scope), and WHICH policy version
  authorized it — immutable thereafter;
- the policy version covers only authorization-relevant state
  (:func:`compute_policy_version`), matching the donor rule that changes to
  models/concurrency/pausing do NOT invalidate existing authorizations;
- resuming a parked/blocked/failed task revalidates its mandate against the
  CURRENT policy: a stale or withdrawn mandate refuses the resume with an
  explicit reason instead of silently continuing;
- only a human stop withdraws a mandate (``cancelled`` terminal edge).

Wiring lives in :mod:`controller.transitions` (issue on ready->running,
withdraw on cancel, guards on resume edges) — same single choke point as the
donor's Engine.
"""
from __future__ import annotations

import hashlib
import json
import time

#: Bump when the versioned fields' meaning changes (donor ``POLICY_REVISION``).
POLICY_REVISION = "mandate-v1"


def compute_policy_version(conn) -> str:
    """Hash of every authorization-bearing setting, current per connection.

    Versioned fields: ctl_autonomy_permission rows (scope, enabled). When you
    add a field here, bump POLICY_REVISION so old mandates compare honestly.
    """
    rows = conn.execute(
        "SELECT scope, enabled FROM ctl_autonomy_permission ORDER BY scope").fetchall()
    payload = {"rev": POLICY_REVISION,
               "perm": [(r["scope"], bool(r["enabled"])) for r in rows]}
    blob = json.dumps(payload, sort_keys=True, ensure_ascii=False)
    return hashlib.sha256(blob.encode()).hexdigest()[:16]


def issue(conn, task_id: str, *, strength: str = "explicit",
          basis: str = "", actor: str = "", scope: str = "") -> str:
    """Record the mandate a task enters running under; idempotent upsert."""
    version = compute_policy_version(conn)
    conn.execute(
        "INSERT INTO ctl_mandate"
        " (task_id,strength,policy_version,basis,actor,scope,issued_at,withdrawn_at)"
        " VALUES (?,?,?,?,?,?,?,NULL)"
        " ON CONFLICT(task_id) DO UPDATE SET strength=excluded.strength,"
        " policy_version=excluded.policy_version, basis=excluded.basis,"
        " actor=excluded.actor, scope=excluded.scope,"
        " issued_at=excluded.issued_at, withdrawn_at=NULL",
        (task_id, strength, version, basis, actor, scope, time.time()))
    return version


def get(conn, task_id: str):
    row = conn.execute(
        "SELECT * FROM ctl_mandate WHERE task_id=?", (task_id,)).fetchone()
    return dict(row) if row else None


def withdraw(conn, task_id: str) -> bool:
    cur = conn.execute(
        "UPDATE ctl_mandate SET withdrawn_at=? WHERE task_id=? AND withdrawn_at IS NULL",
        (time.time(), task_id))
    return bool(cur.rowcount)


def revalidate(conn, task_id: str) -> bool:
    """Donor ``Policy.revalidate`` at resume time: the stored mandate still holds.

    A missing mandate means the task predates mandates — allow (the transition
    guard chain still applies its own checks). A present mandate holds iff not
    withdrawn and the policy version it was issued under is current.
    """
    m = get(conn, task_id)
    if m is None:
        return True
    if m["withdrawn_at"] is not None:
        return False
    return m["policy_version"] == compute_policy_version(conn)


def stale_reason(conn, task_id: str) -> str | None:
    """Human-readable refusal for revalidate(), None when valid."""
    m = get(conn, task_id)
    if m is None:
        return None
    if m["withdrawn_at"] is not None:
        return f"mandate withdrawn at {m['withdrawn_at']}"
    if m["policy_version"] != compute_policy_version(conn):
        return (f"mandate policy {m['policy_version']} superseded by "
                f"{compute_policy_version(conn)}")
    return None
