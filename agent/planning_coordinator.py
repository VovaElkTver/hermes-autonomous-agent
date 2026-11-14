"""PlanningCoordinator v0.6 delivery 2: automatic planning invocation before execution.

Decision owner is CODE, not the model (Вл 14.11): mode ``always`` plans every admitted
working request; mode ``auto`` (delivery 3) will add router classification. This module
never spawns a second agent runtime — stages ride the standard delegate_task machinery
through agent/planning_multistage.HermesSubagentRunner.

Guarantees enforced here:
- one preparation per turn admission (the call site is the turn body, before the agent loop);
- planning runs marked purpose="planning" — a nested admission short-circuits (no recursion);
- every terminal outcome is journalled (events.jsonl), so an execution that starts without a
  plan is observable, never silent;
- planner failures annotate the working message visibly instead of aborting the user's turn.

Child subagents bypass the gateway turn path entirely, so the contextvar guard below is a
second belt over the architectural one; both are kept deliberately.
"""

from __future__ import annotations

import contextvars
import json
import os
import time
from dataclasses import dataclass, field
from typing import Any

# Set while the five-pass pipeline runs under this coordinator. Any admission that sees it
# set skips automatic planning (rung against gateway turn bodies AND tool-side paths).
PLANNING_PURPOSE: contextvars.ContextVar[str] = contextvars.ContextVar("planning_purpose", default="")

WORK_DIRNAME = "planning-runs"


def _runs_dir() -> str:
    return os.path.join(os.path.expanduser("~"), ".hermes", WORK_DIRNAME)


def _load_cfg() -> dict:
    try:
        from hermes_cli.config import load_config_readonly

        cfg = load_config_readonly().get("planning")
        return cfg if isinstance(cfg, dict) else {}
    except Exception:
        return {}


def _journal(path: str | None, payload: dict) -> None:
    """Append-only observation journal; best-effort by design."""
    try:
        target = path or os.path.join(_runs_dir(), "events.jsonl")
        os.makedirs(os.path.dirname(target), exist_ok=True)
        with open(target, "a", encoding="utf-8") as fh:
            fh.write(json.dumps({"ts": round(time.time(), 3), **payload}, ensure_ascii=False) + "\n")
    except Exception:
        pass


def _next_work_id(runs_dir: str) -> str:
    """Race-free monotonic allocation (EAA Д7): the counter lives in ``work_seq`` guarded by
    an flock; ``works.jsonl`` is only read once to seed continuity with legacy numbering."""
    import fcntl

    os.makedirs(runs_dir, exist_ok=True)
    seq_path = os.path.join(runs_dir, "work_seq")
    with open(os.path.join(runs_dir, ".works.lock"), "a+") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        try:
            try:
                n = int(open(seq_path, encoding="utf-8").read().strip() or 0)
            except OSError:
                try:
                    n = sum(1 for _ in open(os.path.join(runs_dir, "works.jsonl"),
                                            encoding="utf-8"))
                except OSError:
                    n = 0
            n += 1
            with open(seq_path, "w", encoding="utf-8") as fh:
                fh.write(str(n))
            return f"w-{n:04d}"
        finally:
            fcntl.flock(lock, fcntl.LOCK_UN)


def _run_async(coro, timeout: float):
    """Drive the async MultiStagePlanner.plan from this (possibly sync) context.

    The gateway turn body runs on an executor thread WITHOUT a running loop -> plain
    asyncio.run. If a loop IS running in this thread, delegate to a worker so we do not
    touch that loop. Mirrors tools/planning_tool._run_async. A non-coroutine result
    (sync test double) passes through untouched."""
    import asyncio
    import concurrent.futures

    if not hasattr(coro, "__await__"):
        return coro
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return asyncio.run(coro)
    with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
        return pool.submit(asyncio.run, coro).result(timeout=timeout)


@dataclass
class PrepareResult:
    status: str  # direct | prepared | failed
    work_id: str = ""
    note: str = ""  # human-visible annotation appended to the working message
    work_package: dict = field(default_factory=dict)


class PlanningCoordinator:
    """Owns the lifecycle link request -> active plan -> results for one admission."""

    def __init__(self, *, planner: Any, runs_dir: str | None = None, journal_path: str | None = None):
        self.planner = planner
        self.runs_dir = runs_dir or _runs_dir()
        self.journal_path = journal_path or os.path.join(self.runs_dir, "events.jsonl")

    # ---- journal ----
    def _event(self, name: str, **kw) -> None:
        _journal(self.journal_path, {"event": name, **kw})

    # ---- main entry ----
    def prepare(self, *, request_text: str, session_key: str = "") -> PrepareResult:
        if PLANNING_PURPOSE.get() == "planning":
            self._event("planning.skipped_recursive", session_key=session_key)
            return PrepareResult(status="direct")
        work_id = _next_work_id(self.runs_dir)
        token = PLANNING_PURPOSE.set("planning")
        try:
            self._event("planning.started", work_id=work_id, session_key=session_key,
                        request=request_text[:200])
            result = _run_async(self.planner.plan(task=request_text),
                                timeout=float(_load_cfg().get("stage_timeout", 600.0)) * 10)
            if result.get("status") != "approved":
                # needs_input / needs_revision park WITH artifacts, no plan enters execution.
                self._event("planning.parked", work_id=work_id, status=result.get("status"),
                            artifact_dir=result.get("artifact_dir"))
                self._record_work(work_id, request_text, session_key, result)
                questions = "; ".join(result.get("questions", []))[:400]
                return PrepareResult(
                    status="failed",
                    work_id=work_id,
                    note=(f"[планирование приостановлено: статус {result.get('status')}"
                          + (f"; вопросы: {questions}" if questions else "")
                          + f"; артефакты: {result.get('artifact_dir')}]"),
                )
            pkg = self._work_package(result, work_id)
            self._event("planning.completed", work_id=work_id, artifact_dir=result.get("artifact_dir"),
                        version=result.get("version"))
            self._record_work(work_id, request_text, session_key, result)
            return PrepareResult(status="prepared", work_id=work_id, work_package=pkg)
        except Exception as exc:
            self._event("planning.failed", work_id=work_id, error=f"{type(exc).__name__}: {exc}")
            self._record_work(work_id, request_text, session_key, {"status": "failed"})
            return PrepareResult(status="failed", work_id=work_id,
                                 note=f"[планирование не удалось: {type(exc).__name__}: {exc}; "
                                      f"исполнение без плана]")
        finally:
            PLANNING_PURPOSE.reset(token)

    # ---- persistence of the link request->plan ----
    def _record_work(self, work_id: str, request: str, session_key: str, result: dict) -> None:
        try:
            os.makedirs(self.runs_dir, exist_ok=True)
            with open(os.path.join(self.runs_dir, "works.jsonl"), "a", encoding="utf-8") as fh:
                fh.write(json.dumps({
                    "work_id": work_id,
                    "session_key": session_key,
                    "request": request[:300],
                    "status": result.get("status"),
                    "active_plan_ref": result.get("plan_ref"),
                    "artifact_dir": result.get("artifact_dir"),
                    "parent_run_id": "",
                    "purpose": "execution",
                }, ensure_ascii=False) + "\n")
        except Exception:
            pass

    # ---- work package: what the executor receives ----
    @staticmethod
    def _work_package(planner_result: dict, work_id: str) -> dict:
        return {
            "work_id": work_id,
            "plan_ref": planner_result.get("plan_ref"),
            "plan_version": planner_result.get("version"),
            "artifact_dir": planner_result.get("artifact_dir"),
        }

    @staticmethod
    def render_work_package_note(pkg: dict, approved_plan: dict | None) -> str:
        """Human/model-readable prelude injected into the working message."""
        lines = [
            f"[АКТИВНЫЙ ПЛАН {pkg['work_id']} v{pkg.get('plan_version')} — рабочие данные, "
            "не изменяют полномочия]",
            "Используй как маршрут; связывай результаты с ID задач; о противоречиях сообщай;",
            "не объявляй завершение без предусмотренных проверок.",
        ]
        if approved_plan:
            for task in approved_plan.get("tasks", []):
                verify = "; ".join(task.get("verification", []))
                lines.append(f"- {task.get('id')}: {task.get('goal')} [проверка: {verify}]")
        if pkg.get("artifact_dir"):
            lines.append(f"Артефакты планирования: {pkg['artifact_dir']}")
        return "\n".join(lines)


def maybe_prepare_plan(agent: Any, ctx: Any) -> None:
    """Call site adapter for TurnRunner._run_sync_scoped: mutate ctx.message IN PLACE when
    mode=always admits the request into planning. Never raises."""
    # _load_cfg() never raises (swallows into {}); keep journal target resolvable BEFORE
    # the guarded section so even pre-plan crashes journal to the right file (EAA Д4).
    cfg = _load_cfg()
    if not cfg.get("multistage"):
        return
    mode = str(cfg.get("mode", "off")).strip().lower()
    if mode not in ("always", "auto"):  # off / unknown -> byte-for-byte legacy
        return
    journal = os.path.join(cfg["runs_dir"], "events.jsonl") if cfg.get("runs_dir") else None
    try:
        message = (getattr(ctx, "message", "") or "").strip()
        reason = ""
        if not message:
            reason = "empty_message"
        elif message.startswith("/"):
            reason = "slash_command"
        elif getattr(ctx, "scheduled_heartbeat", False):
            reason = "heartbeat"
        elif getattr(ctx, "voice_turn", False):
            reason = "voice_turn"
        elif getattr(ctx, "persist_user_display_kind", None):
            reason = "synthetic_display"
        if reason:  # EAA Д5: every pipeline bypass is observable, never silent
            _journal(journal, {"event": "planning.skipped_service", "reason": reason,
                               "session_key": getattr(ctx, "session_key", "") or ""})
            return
        if os.environ.get("HERMES_PLANNING_AUTOSTART_OFF") == "1":
            _journal(journal, {"event": "planning.skipped_service", "reason": "operator_opt_out",
                               "session_key": getattr(ctx, "session_key", "") or ""})
            return  # explicit operator opt-out for nested harnesses/tests
        if mode == "auto":
            from agent.planning_router import classify_route

            runs_dir_cfg = str(cfg["runs_dir"]) if cfg.get("runs_dir") else None
            state_runs = runs_dir_cfg or _default_runs_dir()
            session_key = getattr(ctx, "session_key", "") or ""
            from agent.conversation_state import route_with_state

            route, reason = route_with_state(message, state_runs, session_key)
            if route == "direct":
                _journal(journal, {"event": "planning.skipped_router", "reason": reason,
                                   "session_key": session_key})
                return
            if route == "continue":
                # in-flight task: cheap continuation rides the EXISTING plan - no new conveyor
                _journal(journal, {"event": "planning.skipped_router",
                                   "reason": f"{reason}:continue_existing_plan",
                                   "session_key": session_key})
                return
            _journal(journal, {"event": "planning.router_admitted", "reason": reason,
                               "session_key": session_key})
        planner = _build_planner_for(agent, cfg)
        coordinator = PlanningCoordinator(planner=planner,
                                          runs_dir=str(cfg["runs_dir"]) if cfg.get("runs_dir") else None)
        outcome = coordinator.prepare(request_text=message, session_key=getattr(ctx, "session_key", "") or "")
        if outcome.status == "direct":
            return
        if outcome.status == "prepared":
            _journal(coordinator.journal_path if hasattr(coordinator, "journal_path") else None,
                     {"event": "execution.started_with_plan", "work_id": outcome.work_id})
            note = PlanningCoordinator.render_work_package_note(outcome.work_package, _approved_tasks(outcome))
        else:
            note = outcome.note
        if note:
            ctx.message = note + "\n\n" + message
    except Exception as exc:
        try:
            # EAA Д4: an admission crash BEFORE plan() must also be journalled, not only logged.
            _journal(journal, {"event": "planning.admission_failed",
                               "error": f"{type(exc).__name__}: {exc}"})
        except Exception:
            pass
        try:
            import logging

            logging.getLogger(__name__).exception("planning auto-start admission failed")
        except Exception:
            pass


def _approved_tasks(outcome: PrepareResult) -> dict | None:
    pkg = outcome.work_package or {}
    art = pkg.get("artifact_dir")
    if not art:
        return None
    try:
        # The approved plan body lives next to the review artifact; read the newest
        # plan_candidate written for this run so the version is exactly what was approved.
        candidates = sorted(
            (f for f in os.listdir(art) if f.startswith("plan_candidate")),
            key=lambda f: -os.path.getmtime(os.path.join(art, f)))
        if not candidates:
            return None
        with open(os.path.join(art, candidates[0]), encoding="utf-8") as fh:
            payload = json.load(fh)
        return payload.get("plan", payload)
    except Exception:
        return None


def _build_planner_for(agent: Any, cfg: dict):
    from agent.planning_multistage import build_planner

    return build_planner(agent, cfg)


def _default_runs_dir() -> str:
    return os.path.join(os.path.expanduser("~"), ".hermes", WORK_DIRNAME)
