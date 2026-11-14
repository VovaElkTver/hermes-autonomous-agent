"""Headless planning forks (v0.10): heavy-core conveyor riding a non-switching /fork child.

Owner-approved 14.11 («го» на headless-форк): admission detaches the work to a
fork CHILD session (branch row created, console NOT switched), the child turn
runs the heavy planning model with full toolsets in background, and the result
lands twice: pushed to the chat (visibility) and appended to the PARENT session
as a work-note (context join — no cron-style "not my message" disowning).
"""

from __future__ import annotations

import contextlib
import json
import logging
import os
import uuid
from datetime import datetime
from typing import Any, Optional

logger = logging.getLogger("gateway.planning_fork")


def _planning_fork_cfg(user_config: dict) -> dict:
    cfg = user_config.get("planning") if isinstance(user_config, dict) else None
    cfg = cfg if isinstance(cfg, dict) else {}
    return {
        "base_url": str(cfg.get("fork_base_url") or "").strip() or None,
        "model": str(cfg.get("fork_model") or "").strip() or None,
        "api_key_env": str(cfg.get("fork_api_key_env") or "").strip() or None,
        "max_tokens": int(cfg.get("fork_max_tokens") or 0) or None,
    }


class GatewayPlanningForkMixin:  # composed into GatewayRunner (session plumbing host)
    """Mixed into GatewayRunner; needs the same session plumbing as the fork mixin."""

    async def spawn_planning_fork(self, prompt: str, source: Any) -> Optional[str]:
        """Create a headless fork child of ``source``'s session, run ``prompt`` on it in
        background with the heavy planning model, deliver the result to the chat and
        append a durable work-note into the parent transcript. Returns branch_id."""
        from agent.i18n import t
        from gateway.branch_prefix import branch_display_name

        parent_entry = await self.async_session_store.get_or_create_session(source)
        parent_session_id = parent_entry.session_id
        last_uid = await self._session_db.get_last_message_uid(parent_session_id)
        title = f"plan-{datetime.now().strftime('%H%M%S')}"
        new_session_id = f"{datetime.now().strftime('%Y%m%d_%H%M%S')}_{uuid.uuid4().hex[:6]}"
        branch_id = await self._session_db.new_branch_id()

        try:
            parent = await self._session_db.get_session(parent_session_id)
            await self._session_db.create_session(
                session_id=new_session_id,
                source=source.platform.value if source.platform else "gateway",
                model=None,
                model_config={"_forked_from": parent_session_id},
                parent_session_id=parent_session_id,
                user_id=source.user_id, session_key=parent_entry.session_key,
                chat_id=source.chat_id, chat_type=source.chat_type,
                thread_id=source.thread_id,
                origin_json=None,
                display_name=parent_entry.display_name,
                system_prompt=(parent or {}).get("system_prompt") or None)
            await self._session_db.create_branch_row(
                branch_id, parent_session_id, new_session_id, last_uid, title=title)
        except Exception as e:
            logger.error("planning fork create failed: %s", e)
            return None
        self._journal_fork(parent_session_id, {"event": "planning.fork.created",
                                               "branch_id": branch_id,
                                               "child": new_session_id})

        result = await self._run_planning_fork_child(new_session_id, source, prompt)

        # chat visibility (same channel the interim ack promised)
        if result.get("response"):
            adapter = self._delivery_adapter_for(source)
            metadata = {}
            with contextlib.suppress(Exception):
                metadata = {"thread_id": source.thread_id} if source.thread_id else {}
            if adapter:
                header = t("gateway.background.complete_header", preview=prompt[:60])
                with contextlib.suppress(Exception):
                    await adapter.send(chat_id=source.chat_id,
                                       content=header + result["response"],
                                       metadata=metadata)
        # durable context join into the PARENT transcript (work-note semantics, no LLM)
        note = result.get("response") or result.get("error") or "(пустой ответ ядра)"
        row = {"branch_id": branch_id, "title": title}
        name = branch_display_name(row)
        with contextlib.suppress(Exception):
            await self._session_db.append_messages_batch(parent_session_id, [
                dict(role="user",
                     content=f"[work-note from branch {name}] {note}"[:4800])])
        with contextlib.suppress(Exception):
            await self._session_db.set_branch_status(branch_id, "joined", join_strategy="note")
        self._journal_fork(parent_session_id, {
            "event": "planning.fork.joined", "branch_id": branch_id,
            "ok": bool(result.get("response")), "error": result.get("error", "")[:200]})
        return branch_id

    async def _run_planning_fork_child(self, session_id: str, source: Any, prompt: str) -> dict:
        """One full agent turn on the fork child: heavy model, full toolsets."""
        from agent.secret_scope import get_secret_str
        from gateway.run import _checkpoint_agent_kwargs, _current_max_iterations, \
            _load_gateway_config, _platform_config_key
        from run_agent import AIAgent

        user_config = _load_gateway_config()
        fcfg = _planning_fork_cfg(user_config)
        try:
            model, runtime_kwargs = self._resolve_session_agent_runtime(
                source=source, user_config=user_config)
        except Exception:
            model, runtime_kwargs = None, {}
        platform_key = _platform_config_key(source.platform)
        enabled_toolsets, disabled_toolsets = self._resolve_turn_toolsets(user_config, source, platform_key)
        max_iterations = _current_max_iterations()

        fork_model = fcfg["model"] or model
        api_key = (get_secret_str(fcfg["api_key_env"], "") if fcfg["api_key_env"] else "") \
            or get_secret_str("OPENAI_API_KEY", "") or "local"
        runtime_overrides = {}
        if fcfg["base_url"]:
            runtime_overrides = {"base_url": fcfg["base_url"], "api_key": api_key,
                                 "api_mode": "chat"}
        reasoning_config = self._resolve_session_reasoning_config(source=source, model=model)

        def run_sync():
            from agent.planning_coordinator import PLANNING_PURPOSE
            from gateway.session_context import declare_stateless_channel
            declare_stateless_channel()
            token = PLANNING_PURPOSE.set("planning")  # child must not re-enter admission
            try:
                agent = AIAgent(
                    model=fork_model,
                    **{k: v for k, v in runtime_kwargs.items() if k not in runtime_overrides},
                    **runtime_overrides,
                    **_checkpoint_agent_kwargs(user_config),
                    max_iterations=max_iterations,
                    quiet_mode=True, verbose_logging=False,
                    enabled_toolsets=enabled_toolsets, disabled_toolsets=disabled_toolsets,
                    reasoning_config=reasoning_config,
                    service_tier=self._service_tier,
                    session_id=session_id,
                    platform=platform_key,
                    **{k: getattr(source, k) for k in (
                        "user_id", "user_id_alt", "user_name", "chat_id", "chat_name",
                        "chat_type", "thread_id")},
                    session_db=getattr(self._session_db, "_db", self._session_db),
                    fallback_model=self._refresh_fallback_model(),
                )
                try:
                    out = agent.run_conversation(user_message=prompt, task_id=session_id)
                    return {"response": (out or {}).get("final_response", ""),
                            "error": (out or {}).get("error", "")}
                finally:
                    self._cleanup_agent_resources(agent)
            finally:
                PLANNING_PURPOSE.reset(token)

        try:
            return await self._run_in_executor_with_context(run_sync)
        except Exception as exc:
            logger.exception("planning fork child %s failed", session_id)
            return {"response": "", "error": f"{type(exc).__name__}: {exc}"}

    def _journal_fork(self, _session_id: str, event: dict) -> None:
        """Mirror fork lifecycle into the planning events ledger (observer parity)."""
        with contextlib.suppress(Exception):
            runs_dir = os.path.join(os.path.expanduser("~"), ".hermes", "planning-runs")
            os.makedirs(runs_dir, exist_ok=True)
            with open(os.path.join(runs_dir, "events.jsonl"), "a", encoding="utf-8") as fh:
                fh.write(json.dumps({"ts": datetime.now().timestamp(),
                                     **event}, ensure_ascii=False) + "\n")
