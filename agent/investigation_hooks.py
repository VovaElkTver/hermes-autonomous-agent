"""Runtime hooks for the investigation self-check mechanism.

Doctrine (docs/design/investigation-mechanism.md): the core stores CONDITIONS —
provenance, delivery dedup, restart recovery — and never judges a hypothesis.
The model chooses hypotheses, runs probes and interprets results; these hooks
only record what happened and re-present it. Every hook is fail-open: a broken
capture must never break the turn that produced the real result.

Hook wiring (the runtime files carry one marked call each, this module owns
the logic):
  H1  turn_request_assembly.assemble_api_request — render the active
      investigation into the API copy of the messages (never durable).
  H2  tool_executor — capture each real tool result as an observation
      candidate on the session's open investigation.
  H4  turn_stop_gates.apply_stop_gates — one nudge per turn when captured
      observations were never assessed.
  H3  gateway join — a joined branch result lands as evidence in the
      investigation its probe forked from (structural meta_json, never embedding).
  boot  run_startup — settle dispatched-without-outcome probes to
      unknown_outcome; wake rides the existing resume_pending machinery.
"""
from __future__ import annotations

import json
import logging
from typing import Any

logger = logging.getLogger(__name__)


def _db_and_session(agent: Any) -> tuple[Any, str]:
    return getattr(agent, "_session_db", None), str(getattr(agent, "session_id", "") or "")


# ----------------------------------------------------------------------- H2

def capture_tool_result(agent, function_name, function_args, tool_call_id, tool_message) -> str | None:
    """Record a real tool result as an observation-candidate (assessed=0).

    Probe binding is a structural echo, not a guess: the call's own args must
    carry a ``probe_id`` that names a probe of THIS session's open investigation.
    The ``investigation`` tool's own calls are skipped — its writes are the
    model's own bookkeeping, not evidence about the world.
    """
    try:
        db, session_id = _db_and_session(agent)
        if db is None or not session_id or function_name == "investigation":
            return None
        inv = db.get_open_investigation(session_id)
        if inv is None:
            return None
        probe_id = None
        if isinstance(function_args, dict):
            pid = str(function_args.get("probe_id") or "")
            if pid.startswith("prb"):
                probe = db.get_probe(pid)
                if probe is not None and probe.get("investigation_id") == inv["investigation_id"]:
                    probe_id = pid
        content = tool_message.get("content") if isinstance(tool_message, dict) else None
        if content is None:
            return None
        if not isinstance(content, str):
            content = json.dumps(content, ensure_ascii=False, default=str)
        return db.capture_observation(
            inv["investigation_id"],
            dedup_key=db.observation_dedup_key_call(session_id, str(tool_call_id)),
            source="tool", content=content, probe_id=probe_id,
            meta={"tool": str(function_name)},
        )
    except Exception as exc:  # fail-open: an evidence-store hiccup never touches the turn
        logger.debug("investigation H2 capture skipped: %s", exc)
        return None


# ----------------------------------------------------------------------- H1

def render_note(agent) -> str | None:
    """The short service record of the active investigation for the API copy."""
    try:
        db, session_id = _db_and_session(agent)
        if db is None or not session_id:
            return None
        return db.render_active_investigation(session_id)
    except Exception as exc:
        logger.debug("investigation H1 render skipped: %s", exc)
        return None


# ----------------------------------------------------------------------- H4

def unassessed_observation_ids(agent) -> list[str]:
    try:
        db, session_id = _db_and_session(agent)
        if db is None or not session_id:
            return []
        inv = db.get_open_investigation(session_id)
        if inv is None:
            return []
        return [o["observation_id"] for o in
                db.list_observations(inv["investigation_id"], assessed=False)]
    except Exception as exc:
        logger.debug("investigation stop-gate probe skipped: %s", exc)
        return []


def stop_nudge(agent) -> str | None:
    """One-time nudge text when the turn tries to end with captured-but-unassessed
    results; None when there is nothing outstanding (or the store is unavailable)."""
    ids = unassessed_observation_ids(agent)
    if not ids:
        return None
    return (
        "[investigation] Твои проверки дали результаты, не сопоставленные с прогнозами: "
        + ", ".join(ids)
        + ". Сопоставь через investigation(op='assess') — честный вердикт «неразличающе» тоже "
        "результат. Напоминание однократное: закончишь ход снова — исследование останется "
        "открытым с этими наблюдениями в журнале."
    )


# ------------------------------------------------------------------ fork / join

def stamp_fork_probe(db, current_session_id: str, branch_id: str,
                     probe_id: str, purpose: str = "") -> dict | None:
    """Fork-side probe binding: store what the model SAID (structural branch↔probe
    link); record nothing when the statement does not resolve to a live probe of
    this session's open investigation."""
    try:
        pid = str(probe_id or "")
        if not pid.startswith("prb"):
            return None
        probe = db.get_probe(pid)
        if probe is None:
            return None
        inv = db.get_open_investigation(current_session_id)
        if inv is None or probe.get("investigation_id") != inv["investigation_id"]:
            return None
        db.bind_probe_branch(pid, branch_id)
        db.set_branch_meta(branch_id, {
            "investigation_id": inv["investigation_id"],
            "probe_id": pid,
            "purpose": str(purpose or "")[:200],
        })
        return {"investigation_id": inv["investigation_id"], "probe_id": pid}
    except Exception as exc:
        logger.debug("investigation fork stamp skipped: %s", exc)
        return None


async def capture_branch_join(db, branch: dict, strategy: str, note: str | None) -> None:
    """H3: a joined branch's result lands as evidence in the investigation its probe
    forked from. Provenance is structural (meta_json stamped at fork time). Branch
    agreement upgrades nothing — a join is one more observation needing its own
    ``assess``; there is no vote counter in the schema."""
    try:
        meta = json.loads(branch.get("meta_json") or "{}")
        inv_id = str(meta.get("investigation_id") or "")
        if not inv_id or strategy != "note" or not note:
            return
        parent = branch["parent_session_id"]
        await db.capture_observation(
            inv_id,
            dedup_key=db.observation_dedup_key_branch(parent, branch["branch_id"]),
            source="branch", content=note,
            probe_id=(str(meta.get("probe_id") or "") or None),
            meta={"branch_id": branch["branch_id"], "purpose": meta.get("purpose")},
        )
    except Exception as exc:
        logger.debug("investigation H3 join capture skipped: %s", exc)


# ---------------------------------------------------------------------- boot

def boot_settle(db) -> list[str]:
    """At boot: probes dispatched before the crash have an UNKNOWN outcome — record
    that and re-run nothing (the model decides once the H1 render shows it). Returns
    session ids holding an open investigation with unknown-outcome probes; the
    gateway wakes them through the existing resume_pending machinery."""
    try:
        db.settle_unresolved_probes()
        return db.list_open_investigation_unknown_sessions()
    except Exception as exc:
        logger.debug("investigation boot settle skipped: %s", exc)
        return []
