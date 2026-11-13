"""Inspector-trace emitter: tool-call events into the agency ledger `trace` track.

ТЗ Вл 09.11 («Inspector Mode — эмиттер трейсов вызовов тулов», fork side):
every native tool call dispatched by the agent adds ONE event
``{position_id, turn_id/session, toolset.tool, args_digest, result_digest,
ok|error_code, ts, idem_key}`` to the agency-state.db ledger through the
toolset's existing ``store.submit()`` — same atomicity, same envelope,
monotonic seq. No new DB, no new files, no scheduled, no background threads:
emission is synchronous inside the tool call.

Rules baked in here:
- digests only (sha256 over truncated canonical json) — raw arg/result payloads
  never reach the ledger (secret hygiene);
- single top-level config flag ``tool_trace`` (default false), one branch in
  ``ToolRegistry.dispatch``; flag off ⇒ ~zero overhead;
- fail-open (SAFE-gate model of hermes_events): any tracer error/timeout is
  swallowed and never changes the tool's answer; lost traces are not retried;
- ``position_id`` is ``-`` outside agency sessions (best-effort resolution via
  the toolset session binding).

Receiver contract is owned by @rtx5000 (agency-toolset v0.3); this module only
follows the frozen submit() envelope and may go fully dormant when the toolset
plugin is not installed.
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

TRACE_TRACK = "trace"            # sentinel issue track in the ledger (pattern: REGISTRY_ISSUE)
_DIGEST_INPUT_LIMIT = 64 * 1024  # hash head-truncated canon-json; never store raw payloads

_store_cache: Any = None


def _cfg(key: str, default: Any) -> Any:
    try:
        from hermes_cli.config import load_config
        return load_config().get(key, default)
    except Exception:
        return default


def tool_trace_enabled() -> bool:
    """Top-level config key ``tool_trace``; default OFF (ТЗ п.3)."""
    return bool(_cfg("tool_trace", False))


def _load_store():
    """Resolve the agency-toolset store module; None ⇒ emitter dormant.

    Two load paths, both cheap after first hit: normal package import when the
    plugin is on the path, or direct file load from the standard deploy target
    ``<hermes_home>/plugins/agency/store.py`` (deploy/install.sh layout).
    """
    global _store_cache
    if _store_cache is not None:
        return _store_cache if _store_cache is not False else None
    mod = None
    try:
        from plugins.agency import store as mod  # type: ignore[no-redef]
    except Exception:
        try:
            import importlib.util
            from hermes_constants import get_hermes_home
            path = Path(get_hermes_home()) / "plugins" / "agency" / "store.py"
            spec = importlib.util.spec_from_file_location("agency_store_trace", path) if path.is_file() else None
            if spec is not None and spec.loader is not None:
                mod = importlib.util.module_from_spec(spec)
                spec.loader.exec_module(mod)
        except Exception:
            mod = None
    _store_cache = mod if mod is not None else False
    return mod


def _digest(obj: Any) -> str:
    if isinstance(obj, str):
        blob = obj.encode("utf-8")[:_DIGEST_INPUT_LIMIT]
    else:
        try:
            blob = json.dumps(obj, sort_keys=True, ensure_ascii=False,
                              default=str).encode("utf-8")
        except Exception:
            blob = repr(obj).encode("utf-8")
        blob = blob[:_DIGEST_INPUT_LIMIT]
    return hashlib.sha256(blob).hexdigest()


def _resolve_position(store, session_id: str | None):
    """Best-effort session→position binding; ('-', mandate 0) outside agency."""
    if not session_id:
        return "-", 0
    try:
        row = store.position_for_session(None, session_id)
    except Exception:
        row = None
    if row is None:
        return "-", 0
    pid = getattr(row, "position_id", None) or (row[0] if not isinstance(row, dict) else "")
    mandate = getattr(row, "mandate_version", None)
    if mandate is None:
        try:
            mandate = row["mandate_version"]
        except Exception:
            mandate = 1
    return pid, int(mandate or 1)


def _run_ctx() -> dict | None:
    """Run context injected by controller workers (workers.py spawn env).

    Present only inside headless autonomous runs; interactive sessions return
    None and stay agency-ledger-only per the original Inspector-trace TZ.
    """
    run_id = os.environ.get("HERMES_CONTROLLER_RUN_ID") or None
    if not run_id:
        return None
    return {"run_id": run_id,
            "task_id": os.environ.get("HERMES_CONTROLLER_TASK_ID") or None,
            "step_id": os.environ.get("HERMES_CONTROLLER_STEP_ID") or None}


def _write_primary(payload: dict, ctx: dict) -> None:
    """Primary sink: ctl_event(kind=run_observation) in the agent's own state.db.

    Decision Вл 09.11 («фиксируем и делаем»): state.db is THE primary store of
    run observations — autonomy never depends on the agency plugin; the
    agency-ledger write below is a mirror that fail-opens independently.
    """
    from controller import model as ctl_model
    conn = ctl_model.connect()
    ctl_model.emit(conn, kind="run_observation", origin="tool_trace",
                   task_id=ctx.get("task_id"), payload=payload)


def emit_tool_call(*, session_id: str | None, name: str, toolset: str,
                   args: dict, result: Any = None, ok: bool = True,
                   error_code: str | None = None, db_path: str | None = None) -> None:
    """Emit one trace event for a completed tool dispatch. Fail-open by design.

    ``db_path`` exists for tests / local wiring; production passes None so the
    toolset resolves its own agency-state.db under HERMES_HOME.
    Two sinks (Вл 09.11: state.db — первичный сток, agency-ledger — зеркало):
    - primary: ctl_event(kind=run_observation) in state.db, only when the call
      happens inside a controller run (env ctx);
    - mirror: the agency ledger ``trace`` track via store.submit().
    """
    if not tool_trace_enabled():
        return
    try:
        ctx = _run_ctx()
        args_dig, result_dig = _digest(args), _digest(result if ok else None)
        position_id, mandate = _resolve_position(_load_store(), session_id)
        key_material = "|".join((
            position_id, session_id or "-", f"{toolset}.{name}",
            args_dig, result_dig, "ok" if ok else f"err:{error_code}"))
        idem_key = hashlib.sha256(key_material.encode("utf-8")).hexdigest()[:32]
        payload = {
            "session": session_id or "-",
            "turn": "-",
            "tool": f"{toolset}.{name}",
            "args_digest": args_dig,
            "result_digest": result_dig,
            "ok": ok,
        }
        if error_code:
            payload["error_code"] = error_code
        if ctx:
            payload.update({"run_id": ctx["run_id"], "task_id": ctx["task_id"],
                            "step_id": ctx["step_id"]})
            try:
                _write_primary(payload, ctx)
            except Exception:
                # sinks fail open INDEPENDENTLY: a wedged state.db must not
                # cost us the agency mirror, nor vice versa
                logger.debug("tool_trace: primary run_observation write failed (ignored)",
                             exc_info=True)
        store = _load_store()
        if store is None:
            logger.debug("tool_trace: receiver (plugins.agency.store) unavailable, mirror dropped")
            return
        store.submit(db_path, type_="tool_call", kind="observation",
                     issue_id=TRACE_TRACK, actor_position_id=position_id,
                     mandate_version=mandate, payload=payload, idem_key=idem_key)
    except Exception:
        # SAFE-gate model: a lost trace must never affect the tool result (ТЗ п.4).
        logger.debug("tool_trace: emission failed (ignored)", exc_info=True)
