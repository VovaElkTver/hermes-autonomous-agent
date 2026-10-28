"""controller.alerts — deterministic template alerts, deduped in state.db.

No LLM composes status text in the autonomy loop: every human-facing line is a
fixed template keyed by a state transition in state.db. ``maybe_alert`` fires a
template only when its digest differs from the last one stored in
``ctl_alert_state`` — a stable state stays silent, and every state CHANGE
produces exactly one human-facing line. Digests are cleared (not just
compared) so a recurring condition re-alerts after it clears.

Delivery is a pluggable ``deliver_fn``: in the gateway the ticker passes a
wrapper around the ``hermes send`` CLI (deterministic, no agent loop, no
running-gateway requirement). The state.db record of each alert is the primary
artifact; the chat line is its mirror (an empty target records but stays out
of the chat).
"""
from __future__ import annotations

import hashlib
import json
import subprocess
import time

# Templates are the contract: fixed wording, placeholders only. New alert
# kinds land here as a row, never as inline f-strings in the loop.
ALERT_TEMPLATES = {
    # Gate digest (per pump tick): the queue is legally parked on human
    # decisions; silence about THAT is the real failure (night incident 20.10/21.10).
    "human-gates": "Гейты к человеку ({n}): {gates}",
    "gate-closed": "Гейт закрыт: {task_id} (task → {state})",
    # Watchdog (30m): the pump mechanism itself went quiet.
    "pump-stale": "PUMP TICK ЗАМОЛЧАЛ на {age_min} мин",
    # Watchdog: a machine-parked task (verify: basis, not a revise question)
    # is stuck, not a human question — the executor/pump owns it.
    "machine-park": "⚙ машинная парковка зависла: {task_id} (basis: {basis})",
    # Ticker mechanics: a tick crashed.
    "tick-error": "TICKER: ошибка тика {kind}: {error}",
    # Pump lock takeover decisions (first sighting / self-hold), pre-kill signal.
    "pump-lock-stale": "PUMP LOCK: {phase} — holder pid {pid} молчит {age_s} с",
}


def _digest(text: str) -> str:
    return hashlib.sha1(text.encode()).hexdigest()[:12]


_MAX_REALERT_S = 86400.0  # a persisting condition screams at most daily


def maybe_alert(conn, key: str, template_key: str, backoff_s: float = 0.0,
                **fields) -> str | None:
    """Render the template and return the line iff its digest changed since the
    last stored alert for ``key`` (state.db dedupe); else None. The digest is
    stored either way, so a cleared-then-recurred condition re-alerts.

    ``backoff_s`` adds the growing re-alert interval (audit 28.10 §3б): a
    SAME-digest condition that keeps repeating re-fires at
    ``backoff_s * 2**count``, capped at one day; ``clear_alert`` (or a digest
    change) resets the count, so the first alert after recovery is immediate."""
    if template_key not in ALERT_TEMPLATES:
        raise KeyError(f"unknown alert template {template_key!r}")
    text = ALERT_TEMPLATES[template_key].format(**fields)
    d = _digest(text)
    now = time.time()
    row = conn.execute("SELECT digest, last_ts, count FROM ctl_alert_state"
                       " WHERE alert_key=?", (key,)).fetchone()
    count = 0
    if row and row["digest"] == d:
        if backoff_s > 0 and now - row["last_ts"] < min(backoff_s * 2 ** row["count"],
                                                        _MAX_REALERT_S):
            return None
        count = row["count"] + 1
    conn.execute(
        "INSERT INTO ctl_alert_state(alert_key, digest, last_ts, count) VALUES (?,?,?,?)"
        " ON CONFLICT(alert_key) DO UPDATE SET digest=excluded.digest,"
        " last_ts=excluded.last_ts, count=excluded.count",
        (key, d, now, count))
    conn.commit()
    return text


def clear_alert(conn, key: str) -> None:
    """Forget the stored digest so the next change re-alerts (condition cleared)."""
    conn.execute("DELETE FROM ctl_alert_state WHERE alert_key=?", (key,))
    conn.commit()


def deliver(line: str, target: str, *, hermes_bin: str = "hermes",
            timeout_s: float = 60.0) -> bool:
    """Send one human-facing line through the ``hermes send`` CLI (the same
    platform credentials the gateway uses; no LLM, no agent loop). ``target``
    is the ``hermes send -t`` target grammar (e.g. ``matrix:!ROOM:HOST``).
    An empty target records nothing here — the caller still holds the line in
    state.db via maybe_alert."""
    if not target:
        return False
    try:
        p = subprocess.run([hermes_bin, "send", "-t", target, "-q", line],
                           input=None, capture_output=True, text=True,
                           timeout=timeout_s, check=False)
        return p.returncode == 0
    except (OSError, subprocess.TimeoutExpired):
        return False


def render_gates_snapshot(conn) -> dict:
    """The deterministic gate snapshot (same rules the cron tick used):
    needs_human tasks + tasks whose latest contract revision is unagreed while
    the task is draft/ready. Returns {task_id: reason} — the digest over it is
    what dedupes the human-gates alert."""
    gates: dict[str, str] = {}
    for r in conn.execute(
            "SELECT task_id, substr(goal, 1, 60) g FROM ctl_task WHERE state='needs_human'"):
        gates[r["task_id"]] = "needs_human: " + ((r["g"] or "").replace("\n", " "))
    # Guard semantics: only the LATEST revision decides agreement.
    for r in conn.execute(
            "SELECT cr.task_id FROM ctl_contract_revision cr"
            " JOIN (SELECT task_id, MAX(version) v FROM ctl_contract_revision"
            "       GROUP BY task_id) t ON cr.task_id=t.task_id AND cr.version=t.v"
            " WHERE cr.agreed_at IS NULL"):
        tid = r["task_id"]
        st = conn.execute("SELECT state FROM ctl_task WHERE task_id=?", (tid,)).fetchone()
        if st and st[0] in ("ready", "draft"):
            gates[tid] = f"contract MATERIAL not agreed (state={st[0]})"
    return gates


def gate_change_lines(conn, snapshot: dict, prev_snapshot: dict | None) -> list[str]:
    """Human-facing lines for gate SET changes: the changed digest (when gates
    are present) plus a close line per gate that vanished."""
    lines = []
    if snapshot:
        lines.append(maybe_alert(
            conn, "human-gates", "human-gates",
            n=len(snapshot),
            gates=json.dumps(snapshot, ensure_ascii=False, sort_keys=True)[:1200]) or "")
    else:
        clear_alert(conn, "human-gates")
    if prev_snapshot:
        for tid in sorted(set(prev_snapshot) - set(snapshot)):
            state = conn.execute("SELECT state FROM ctl_task WHERE task_id=?",
                                 (tid,)).fetchone()
            lines.append(maybe_alert(
                conn, f"gate-closed:{tid}", "gate-closed",
                task_id=tid, state=(state[0] if state else "unknown")) or "")
    return [l for l in lines if l]
