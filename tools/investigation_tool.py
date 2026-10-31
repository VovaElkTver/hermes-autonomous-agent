#!/usr/bin/env python3
"""Investigation self-check mechanism: the agent-facing tool.

The model owns every semantic decision (which question to open, which
hypotheses compete, which expectations differentiate, what an observation
means, when to close). This tool only records those decisions into the
session DB (hermes_state_investigations.InvestigationMixin) and reads them
back; the runtime hooks (capture_tool_result / capture_branch_result / the
pre-request render) are the mechanism's other side — see
docs/design/investigation-mechanism.md.

Operations:
  open    — question + hypothesis versions (+ optional probes with predictions)
  predict — differentiating expectations for a probe, written BEFORE it runs
  assess  — compare an observation-candidate with the predictions; the ONLY
            path by which a candidate becomes accounted evidence
  close   — conclusion + constraints; refused while any observation is
            unassessed (investigation stays open with a diagnostic)
"""

import json
import logging
from typing import Any

logger = logging.getLogger(__name__)

INVESTIGATION_SCHEMA: dict[str, Any] = {
    "name": "investigation",
    "description": (
        "Self-check your own hypotheses. open: record a question and the "
        "competing hypothesis versions. predict: write what each hypothesis "
        "expects a specific check (probe) to show BEFORE running the check. "
        "assess: after tool results arrive (the runtime captures them for "
        "you), state how each observation bears on each prediction. close: "
        "state your conclusion and its limits (optional before answering the "
        "human; refused while any captured observation is unassessed). The "
        "system stores conditions only — the judgment is yours."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "op": {"type": "string", "enum": ["open", "predict", "assess", "close"]},
            "investigation_id": {
                "type": "string",
                "description": "Target investigation; omit to address the session's open one."},
            "question": {"type": "string", "description": "op=open: the question under investigation."},
            "hypotheses": {
                "type": "array", "items": {"type": "string"},
                "description": "op=open: competing hypothesis statements (versions)."},
            "probes": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "description": {"type": "string"},
                        "predictions": {
                            "type": "array",
                            "items": {
                                "type": "object",
                                "properties": {
                                    "hypothesis": {"type": "integer",
                                                   "description": "Index into hypotheses (0-based)."},
                                    "expected": {"type": "string"},
                                },
                                "required": ["hypothesis", "expected"]},
                        }},
                    "required": ["description"]},
                "description": "op=open: checks planned with their per-hypothesis expectations."},
            "probe_id": {"type": "string", "description": "op=predict: the probe being predicted."},
            "predictions": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "hypothesis_id": {"type": "string"},
                        "expected": {"type": "string"}},
                    "required": ["hypothesis_id", "expected"]},
                "description": "op=predict: expectation per hypothesis."},
            "observation_id": {"type": "string", "description": "op=assess: the captured candidate."},
            "verdict": {"type": "string",
                        "description": "op=assess: how the observation bears on the predictions."},
            "hypothesis_updates": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "hypothesis_id": {"type": "string"},
                        "status": {"type": "string",
                                   "description": "Your word for the hypothesis state (stored verbatim)."}},
                    "required": ["hypothesis_id", "status"]},
                "description": "op=assess: optional hypothesis status changes."},
            "summary": {"type": "string", "description": "op=close: the conclusion."},
            "constraints": {"type": "string",
                            "description": "op=close: limits / caveats on the conclusion."},
        },
        "required": ["op"],
    },
}


def _err(message: str, **extra) -> str:
    return json.dumps({"ok": False, "error": message, **extra}, ensure_ascii=False)


def _resolve_open(db, current_session_id: str, investigation_id: str):
    """The investigation a non-open op addresses: explicit id, else the
    session's single open one. Returns (row, error_json)."""
    if investigation_id:
        row = db.get_investigation(investigation_id)
        if row is None:
            return None, _err(f"no such investigation {investigation_id!r}")
        return row, None
    row = db.get_open_investigation(current_session_id)
    if row is None:
        return None, _err("no open investigation in this session; use op=open first")
    return row, None


def _op_open(db, current_session_id: str, args: dict) -> str:
    question = str(args.get("question") or "").strip()
    if not question:
        return _err("op=open requires 'question'")
    investigation_id = db.new_investigation_id()
    investigation_id, created = db.open_investigation(investigation_id, current_session_id, question)
    if not created:
        # Idempotent replay of a delivery: report the live investigation, don't fork state.
        existing = db.get_investigation(investigation_id) or {}
        return json.dumps({"ok": True, "investigation_id": investigation_id,
                           "already_open": True, "question": existing.get("question")},
                          ensure_ascii=False)
    hypothesis_ids = [db.add_hypothesis(investigation_id, str(h))
                      for h in (args.get("hypotheses") or []) if str(h).strip()]
    probe_out = []
    for probe in (args.get("probes") or []):
        description = str((probe or {}).get("description") or "").strip()
        if not description:
            continue
        probe_id = db.add_probe(investigation_id, description)
        recorded = []
        for pred in (probe.get("predictions") or []):
            idx = (pred or {}).get("hypothesis")
            expected = str((pred or {}).get("expected") or "").strip()
            if not isinstance(idx, int) or not (0 <= idx < len(hypothesis_ids)) or not expected:
                continue
            _, ok = db.add_prediction(probe_id, hypothesis_ids[idx], expected)
            recorded.append({"hypothesis_id": hypothesis_ids[idx], "created": ok})
        probe_out.append({"probe_id": probe_id, "predictions": recorded})
    logger.info("investigation open: %s (%s) in %s", investigation_id, question[:60], current_session_id)
    return json.dumps({"ok": True, "investigation_id": investigation_id,
                       "hypothesis_ids": hypothesis_ids, "probes": probe_out},
                      ensure_ascii=False)


def _op_predict(db, current_session_id: str, args: dict) -> str:
    row, error = _resolve_open(db, current_session_id, str(args.get("investigation_id") or ""))
    if error:
        return error
    probe_id = str(args.get("probe_id") or "")
    probe = db.get_probe(probe_id)
    if probe is None or probe["investigation_id"] != row["investigation_id"]:
        return _err(f"probe {probe_id!r} is not part of {row['investigation_id']}")
    results = []
    for pred in (args.get("predictions") or []):
        hypothesis_id = str((pred or {}).get("hypothesis_id") or "")
        expected = str((pred or {}).get("expected") or "").strip()
        if not hypothesis_id or not expected:
            continue
        prediction_id, created = db.add_prediction(probe_id, hypothesis_id, expected)
        results.append({"prediction_id": prediction_id, "created": created})
    if not results:
        return _err("op=predict requires 'predictions' [{hypothesis_id, expected}]")
    return json.dumps({"ok": True, "probe_id": probe_id, "predictions": results,
                       "note": None if all(r["created"] for r in results)
                       else "some predictions were not written: the probe already has an "
                             "observation, or the pair was already predicted"},
                      ensure_ascii=False)


def _op_assess(db, current_session_id: str, args: dict) -> str:
    observation_id = str(args.get("observation_id") or "")
    verdict = str(args.get("verdict") or "").strip()
    observation = db.get_observation(observation_id)
    if observation is None:
        return _err(f"no such observation {observation_id!r}")
    if observation["investigation_id"] != str(args.get("investigation_id") or observation["investigation_id"]):
        return _err("observation belongs to another investigation")
    if not verdict:
        return _err("op=assess requires 'verdict'")
    if not db.assess_observation(observation_id, verdict, args.get("hypothesis_updates") or []):
        return _err(f"observation {observation_id!r} could not be assessed")
    return json.dumps({"ok": True, "observation_id": observation_id, "assessed": True,
                       "unassessed_remaining": db.unassessed_count(observation["investigation_id"])},
                      ensure_ascii=False)


def _op_close(db, current_session_id: str, args: dict) -> str:
    row, error = _resolve_open(db, current_session_id, str(args.get("investigation_id") or ""))
    if error:
        return error
    summary = str(args.get("summary") or "").strip()
    if not summary:
        return _err("op=close requires 'summary' (your conclusion)")
    result = db.close_investigation(row["investigation_id"], summary,
                                    str(args.get("constraints") or ""))
    if not result.get("closed"):
        return json.dumps({"ok": True, "closed": False,
                           "investigation_id": row["investigation_id"],
                           "unassessed": result.get("unassessed", 0),
                           "note": "investigation stays open: assess the captured "
                                   "observations first (close recorded as pending)"},
                          ensure_ascii=False)
    return json.dumps({"ok": True, "closed": True,
                       "investigation_id": row["investigation_id"]}, ensure_ascii=False)


_OPS = {"open": _op_open, "predict": _op_predict, "assess": _op_assess, "close": _op_close}


def investigation(args: dict, db=None, current_session_id: str = "") -> str:
    """Dispatch one investigation operation; returns JSON status (session_fork shape)."""
    if db is None or not current_session_id:
        return _err("session database unavailable")
    op = str(args.get("op") or "")
    handler = _OPS.get(op)
    if handler is None:
        return _err(f"unknown op {op!r}", ops=sorted(_OPS))
    return handler(db, current_session_id, args)


from tools.registry import registry  # (registry-first import chain, session_fork pattern)

registry.register(
    name="investigation",
    toolset="sessions",  # rides with session_fork/session_join (fork/join family)
    schema=INVESTIGATION_SCHEMA,
    # ``current_session_id`` is injected by the inline dispatch path
    # (agent/inline_tool_executors.py); registry dispatch carries ``session_id``
    # (model_tools.py:820), so accept either name.
    handler=lambda args, **kw: investigation(
        args, db=kw.get("db"),
        current_session_id=kw.get("current_session_id") or kw.get("session_id") or ""),
    emoji="🔬")
