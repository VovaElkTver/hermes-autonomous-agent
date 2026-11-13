"""plan_multistage tool (planning.multistage v0.5, delivery 1): run the five-pass planner.

The tool executes inside the calling agent's turn; stages run as one-shot subagents via the
standard delegate_task machinery (agent/planning_multistage.HermesSubagentRunner). No second
agent runtime. Flag: planning.multistage in config.yaml (default false).
"""

from __future__ import annotations

import asyncio
import concurrent.futures
import json
import os
from pathlib import Path

from tools.registry import registry


def _load_planning_cfg() -> dict:
    try:
        from hermes_cli.config import load_config_readonly

        cfg = load_config_readonly().get("planning")
        return cfg if isinstance(cfg, dict) else {}
    except Exception:
        return {}


def _run_async(coro, timeout: float):
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return asyncio.run(coro)
    # Called from a thread with a loop (e.g. gateway): keep blocking work off that loop.
    with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
        return pool.submit(asyncio.run, coro).result(timeout=timeout)


def plan_multistage(args: dict, parent_agent=None) -> str:
    cfg = _load_planning_cfg()
    if not cfg.get("multistage"):
        return json.dumps({"error": "planning.multistage выключен (config.yaml); /plan работает в обычном режиме"},
                          ensure_ascii=False)
    task = str(args.get("task") or "").strip()
    if not task:
        return json.dumps({"error": "task: требуется непустая строка"}, ensure_ascii=False)
    if parent_agent is None:
        return json.dumps({"error": "plan_multistage требует родительский контекст агента"}, ensure_ascii=False)

    from agent.planning_multistage import build_planner

    planner = build_planner(parent_agent, cfg)
    result = _run_async(planner.plan(task=task), timeout=float(cfg.get("stage_timeout", 600.0)) * 10)
    return json.dumps(result, ensure_ascii=False)


PLAN_MULTISTAGE_SCHEMA = {
    "name": "plan_multistage",
    "description": (
        "Run the five-pass planning pipeline (contract → research → strategy → plan → review) "
        "for a non-trivial task. Returns a status plus artifact refs and, on success, the "
        "reviewed plan. Planning only — no implementation."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "task": {
                "type": "string",
                "description": "The task to plan, verbatim from the user request.",
            },
        },
        "required": ["task"],
    },
}


_SKIP_LOG_PATH: str | None = None  # test seam; None = default ~/.hermes/planning-runs/skip.log


def _skip_record(reason: str) -> None:
    """v0.5 trigger audit: any task execution path that bypasses the pipeline writes an
    append-only record under ~/.hermes/planning-runs/skip.log so skips stay observable."""
    try:
        path = Path(_SKIP_LOG_PATH) if _SKIP_LOG_PATH else (
            Path(os.path.expanduser("~")) / ".hermes" / "planning-runs" / "skip.log")
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path, "a", encoding="utf-8") as fh:
            fh.write(json.dumps({"reason": reason}, ensure_ascii=False) + "\n")
    except Exception:
        pass  # audit is best-effort by design; never blocks the turn


registry.register(
    name="plan_multistage",
    toolset="tasks",
    schema=PLAN_MULTISTAGE_SCHEMA,
    handler=lambda args, **kw: plan_multistage(args, parent_agent=kw.get("parent_agent")),
    emoji="📐",
)
