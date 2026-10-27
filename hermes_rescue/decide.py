"""Decision layer: dossier → one bounded LLM call → structured verdict.

The intelligence is borrowed, the hands are ours: if no model answers (the
LLM service is part of the same fleet that may be down), the fallback is
deterministic and conservative — escalate, hands off.
"""
from __future__ import annotations

import json

import httpx

from .config import RescueConfig
from .inspect import dossier_text

SYSTEM = """You are hermes_rescue, the immune core of a Hermes node. You never
run anything yourself. From the JSON dossier of the main node choose exactly one
action and answer with JSON only:
{"action": "none|restart_unit|snapshot_db|switch_release|lift_estop|escalate",
 "release": "<name, only for switch_release>", "reason": "<one sentence in ru>"}
Rules:
- main healthy (unit active, heartbeat fresh, db ok) -> action "none".
- crash-loop (NRestarts high, MainPID churn, FATAL in journal) -> snapshot_db first.
- switch_release ONLY when a release older than the current one exists and the
  failure signature points at code (import/startup errors), not config or host.
- anything ambiguous, or db unreadable, or you lack a safe target -> escalate.
You are conservative by design: escalate is the right answer more often than it
feels like."""


def _ask(cfg: RescueConfig, dossier: str) -> dict | None:
    payload = {
        "model": cfg.model_id, "temperature": 0.2, "max_tokens": 1500,
        "messages": [{"role": "system", "content": SYSTEM},
                     {"role": "user", "content": dossier}],
    }
    for base in (cfg.model_base, cfg.model_fallback):
        try:
            r = httpx.post(f"{base}/chat/completions", json=payload, timeout=90)
            r.raise_for_status()
            text = r.json()["choices"][0]["message"].get("content") or ""
            start, end = text.find("{"), text.rfind("}")
            if start == -1 or end == -1:
                continue
            verdict = json.loads(text[start:end + 1])
            if isinstance(verdict.get("action"), str):
                return verdict
        except Exception:
            continue
    return None


def decide(cfg: RescueConfig, dossier: dict) -> dict:
    v = _ask(cfg, dossier_text(dossier))
    if v is None:
        return {"action": "escalate", "reason": "нет доступной модели — консервативный дефолт: руки убираем"}
    allowed = {"none", "restart_unit", "snapshot_db", "switch_release", "lift_estop", "escalate"}
    if v["action"] not in allowed:
        return {"action": "escalate", "reason": f"модель просит неизвестное действие '{v['action']}'"}
    return v
