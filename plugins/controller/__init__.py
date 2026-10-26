"""controller plugin hooks — the hook surface of the managed-autonomy controller.

Off unless config `controller.enabled: true` (plugins.entries.controller.settings.enabled
or top-level `controller.enabled`). Feature-flag discipline per the spec's task:
implementation lands under a disabled-by-default flag.

Wiring:
  pre_tool_call    -> dispatch barrier for autonomous-origin actions (epoch re-read at
                      dispatch time closes the pause race; supervisor tags children as
                      autonomous by session registry).
  post_tool_call   -> budget accounting per task_id (survives restart, not per-worker).
  register()       -> restart recovery (Q5): operator /restart marker -> non-autonomous;
                      no marker (crash) -> continue with epoch bump; unfinished runs to
                      'unknown' awaiting verify (no blind retry). Then the in-thread
                      TICKER (controller.ticker) starts: one daemon thread inside this
                      gateway process drives the pump drain + watchdog on a fixed
                      cadence (the 24.10 decision: NOT systemd, NOT cron, NOT a
                      separate process). The post-session pump_cli subprocess spawn is
                      gone — the ticker's 5-minute pump tick replaces it, and the
                      residual cron tick stays a single-flight backup during the 24h
                      parallel soak.
"""
from __future__ import annotations

import logging
import threading
from pathlib import Path

logger = logging.getLogger("controller")

_ctx = None
_lock_obj = threading.RLock()
_tls = threading.local()
_scope_by_session: dict[str, str] = {}


def _enabled() -> bool:
    try:
        if _ctx is not None and _ctx.get_config("enabled", False):
            return True
    except Exception:
        pass
    # top-level config.yaml: controller.enabled (same source the core router uses)
    try:
        from gateway.run_inbound import _ctl_flag_on
        return _ctl_flag_on()
    except Exception:
        return False


def _db():
    """Per-thread state.db connection. sqlite3 enforces thread affinity and the
    gateway serves subagent tool hooks on worker threads: a module-level shared
    connection fails closed on every autonomous dispatch from a foreign thread
    (the 19.10 deleg_84b72e21 barrier outage)."""
    conn = getattr(_tls, "conn", None)
    if conn is None:
        from controller import model
        conn = model.connect()
        _tls.conn = conn
    return conn


# ---- dispatch barrier --------------------------------------------------------

def _default_scope() -> str:
    """Fallback queue scope when a session was never registered by the
    dispatcher: the process working directory (tasks are queued per workdir)."""
    from pathlib import Path
    return str(Path.cwd().resolve())


def _origin_of(task_id: str, session_id: str) -> str:
    """Children spawned through delegate_task are the autonomous origin; operator
    sessions are interactive. supervisor's registry is the tag source (Q4 decision)."""
    try:
        from tools.delegate_tool_registry import _active_subagents
        with _lock_obj:
            return "autonomous" if task_id in _active_subagents else "user"
    except Exception:
        return "user"


def _on_pre_tool_call(tool_name: str = "", args: dict | None = None, task_id: str = "",
                      session_id: str = "", **kw):
    if not _enabled():
        return None
    try:
        from controller import barrier
        origin = _origin_of(task_id, session_id)
        if origin != "autonomous":
            return None
        scope = _scope_by_session.get(session_id) or _default_scope()
        task_id_ctl = kw.get("controller_task_id") or _task_for_scope(_db(), scope)
        ok, why = barrier.check(_db(), scope=scope, origin=origin, task_id=task_id_ctl)
        if not ok:
            from . import agent_tools
            if agent_tools.is_read_only(tool_name):
                # Declared read-only tools survive a blocked dispatch: looking at
                # the queue is the recovery path when autonomy is paused/stale.
                return None
            return {"action": "block",
                    "message": f"[controller] autonomous dispatch blocked: {why}"}
    except Exception as exc:
        logger.warning("controller barrier error (fail-closed): %s", exc)
        return {"action": "block", "message": f"[controller] barrier error: {exc}"}
    return None


def _task_for_scope(conn, scope: str):
    row = conn.execute("SELECT task_id FROM ctl_task WHERE workdir=? AND state='running'"
                       " ORDER BY updated_at DESC LIMIT 1", (scope,)).fetchone()
    return row["task_id"] if row else None


# ---- budget accounting -------------------------------------------------------

def _on_post_tool_call(tool_name: str = "", args: dict | None = None, task_id: str = "",
                       session_id: str = "", **kw):
    if not _enabled() or kw.get("status") not in {"ok", "error"}:
        return
    try:
        from controller import model
        tid = kw.get("controller_task_id")
        if not tid:
            return
        model.add_budget(_db(), tid, "tool_calls", 1)
    except Exception as exc:
        logger.debug("controller budget accounting failed: %s", exc)
    return



def pump(scope: str, *, worker_fn=None, review_fn=None, max_steps: int = 20) -> list[dict]:
    """Drive the autonomous cycle for one scope (queue + loop). The engine is pure and
    tested; worker_fn is host-provided (headless executor or delegate dispatch) and
    review_fn defaults to the supervisor checkpoint. Safe to call from cron or a
    completion watcher: permission is re-checked at dispatch, so a paused queue no-ops."""
    from controller import loop as _loop
    if worker_fn is None:
        def worker_fn(run, task, step):
            return {"status": "failed", "artifacts": [],
                    "error": "no worker bound (increment-2 binding PR pending)"}
    if review_fn is None:
        review_fn = lambda run, step, arts: {"verdict": "needs_human",
                                             "detail": "no reviewer bound"}
    return _loop.run_cycle(_db(), scope=scope, worker_fn=worker_fn,
                           review_fn=review_fn, max_steps=max_steps)


def register(ctx):
    global _ctx
    _ctx = ctx
    ctx.register_hook("pre_tool_call", _on_pre_tool_call)
    ctx.register_hook("post_tool_call", _on_post_tool_call)
    try:
        from . import agent_tools
        for _name, _schema, _handler in agent_tools.tool_defs():
            ctx.register_tool(name=_name, toolset="controller", schema=_schema,
                              handler=_handler, emoji=agent_tools.EMOJI,
                              check_fn=_enabled)
    except Exception as exc:  # pragma: no cover - defensive
        logger.warning("controller: agent tools registration failed: %s", exc,
                       exc_info=True)
    if _enabled():
        logger.info("controller: managed autonomy enabled (flag on)")
        try:
            from controller import commands as _cmds, reconcile as _rec
            mode = _cmds.on_restart(_db(), scope="default")
            rep = _rec.recover(_db(), verify_fn=lambda r: "not_started")
            logger.info("controller recovery: mode=%s unresolved_runs=%s",
                        mode["mode"], [x["run_id"] for x in rep])
        except Exception as exc:
            logger.warning("controller recovery failed: %s", exc)
        # The autonomy loop itself now runs IN THIS PROCESS: one daemon thread,
        # fixed cadence, no cron for the mechanics. Ownership (flock) makes it a
        # gateway singleton even though CLI/worker processes also load this plugin.
        try:
            from agent import safe_mode as _safe_mode
            if _safe_mode.is_safe():
                logger.info("SAFE MODE: controller pump not started")
            else:
                from controller import ticker as _ticker
                started = _ticker.start_default()
                logger.info("controller ticker: %s",
                            "in-thread scheduler started" if started
                            else "not started (another live gateway owns the loop)")
        except Exception as exc:
            logger.warning("controller ticker start failed: %s", exc)
