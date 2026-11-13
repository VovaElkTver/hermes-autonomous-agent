"""skill-gate: fork-facing adapter to the wiki-gate skill-evolution engine.

Engine (outer contour, NOT in this repo): ~/projects/self-improvement-watch/wiki-gate/
- `gate.py check`  — measures the accepted skill set against bench best_val;
  exit 1 = regression (a skill edit broke something that was passing).
- `gate.py status` — canonical evolution state (r_best, history, holdout).

Semantics for the fork: this is CI for skills. Any change under ~/.hermes/skills
(own edits, curator, evolution loop) should leave `skill_gate(action='check')`
green. A green check is 'no regression vs the measured best', not 'skill is good'.

Fail-open registration: if the engine is absent, the tool still registers and
reports 'engine absent' at call time (a missing outer contour must never break
agent startup). Engine location is config: skill_gate.engine_dir.
"""
from __future__ import annotations

import json
import logging
import os
import subprocess

log = logging.getLogger(__name__)

DEFAULT_ENGINE = os.path.expanduser("~/projects/self-improvement-watch/wiki-gate")
CHECK_TIMEOUT_S = 900  # uncached full val pass is minutes on a busy local model


def _engine_dir() -> str:
    try:
        from hermes_cli.config import load_config_readonly
        cfg = load_config_readonly().get("skill_gate") or {}
        d = cfg.get("engine_dir") if isinstance(cfg, dict) else None
        if isinstance(d, str) and d.strip():
            return os.path.expanduser(d.strip())
    except Exception:
        pass
    return DEFAULT_ENGINE


def _run(subcmd: str, timeout: int) -> dict:
    engine = _engine_dir()
    gate = os.path.join(engine, "gate.py")
    if not os.path.isfile(gate):
        return {"ok": False, "error": f"wiki-gate engine absent at {gate} "
                "(install outer contour or set skill_gate.engine_dir)"}
    try:
        p = subprocess.run(["python3", gate, subcmd], capture_output=True,
                           text=True, timeout=timeout, cwd=engine)
    except subprocess.TimeoutExpired:
        return {"ok": False, "error": f"engine {subcmd} timed out after {timeout}s"}
    except OSError as exc:
        return {"ok": False, "error": f"engine launch failed: {exc}"}
    out = (p.stdout or "").strip().splitlines()
    payload = out[-1] if out else ""
    try:
        verdict = json.loads(payload)
    except (json.JSONDecodeError, ValueError):
        verdict = {"raw": payload, "stderr": (p.stderr or "")[-400:]}
    if subcmd == "check":
        # exit 1 = regression detected: a legitimate verdict, not a tool failure
        if p.returncode not in (0, 1):
            return {"ok": False, "error": f"engine exit {p.returncode}", **verdict}
        return {"ok": True, **verdict}
    return {"ok": p.returncode == 0, **verdict}


def _handle_skill_gate(action: str = "check", timeout: int = 0) -> str:
    action = (action or "check").strip().lower()
    if action not in ("check", "status"):
        return json.dumps({"ok": False, "error": f"unknown action {action!r}; use check|status"})
    to = timeout or (CHECK_TIMEOUT_S if action == "check" else 30)
    return json.dumps(_run(action, int(to)), ensure_ascii=False)


TOOLS = [
    (
        "skill_gate",
        {
            "description": ("Regression gate for the agent's skill set (wiki-gate engine, "
                            "outer contour). action='check': re-measure the accepted skills "
                            "against the bench best_val — run after ANY edit under "
                            "~/.hermes/skills; a regression report lists broken tasks. "
                            "action='status': evolution state (r_best, history, holdout ids). "
                            "Green check = 'no regression vs measured best', not 'skills are good'."),
            "parameters": {
                "type": "object",
                "properties": {
                    "action": {"type": "string", "enum": ["check", "status"],
                               "description": "check = regression run (minutes when uncached); status = state read (instant)."},
                    "timeout": {"type": "integer", "description": "Optional seconds cap; default 900 for check, 30 for status."},
                },
                "required": ["action"],
            },
        },
        _handle_skill_gate,
        "🧪",
    ),
]


def register(ctx) -> None:
    for name, schema, handler, emoji in TOOLS:
        ctx.register_tool(name=name, toolset="skill_gate", schema=schema,
                          handler=handler, emoji=emoji)
    log.info("skill-gate: registered skill_gate (engine: %s)", _engine_dir())
