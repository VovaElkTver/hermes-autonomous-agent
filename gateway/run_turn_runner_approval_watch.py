"""П9 (rescue-claims, 14.11): a possibly-delivered approval card gets a bounded watcher.

When ``send_exec_approval`` outruns the 15s ack window the turn runner treated the send
as ``ambiguous`` and stopped: no re-send, no notice, nothing. If the card in fact died in
transport (the rtx5090 deafness window at 22:45-22:51: the approval prompt never reached
the room, the session blocked on an invisible prompt, the owner never learned what was
pending), the armed prompt stayed armed until the approval TTL expired it silently.

The clarify-prompt path already solved this exact class (``run_turn_runner_clarify_delivery
._LateFailureWatch``): watch the pending send future, and when it resolves late, act on
the verdict — a late success arms the timeout notice the ambiguous path skipped, a
definitive failure posts the plain-text ``/approve`` fallback exactly once (deduped
against a late button tap), a connector decline posts nothing (re-sending refused
content is the leak the egress guard exists to stop).

Runs as a small daemon thread parked on the send future: the agent thread must proceed
into the bounded wait, and the loop thread must not be blocked by another send's ack.
"""
from __future__ import annotations

import logging
import threading
from typing import Any, Callable, Optional

logger = logging.getLogger(__name__)

__all__ = ["watch_ambiguous_approval"]


def watch_ambiguous_approval(
    runner, fut: Any, *, approval_data: dict, cmd: str, desc: str, flags: dict) -> Optional[threading.Thread]:
    """Arm the watcher after an ``ambiguous`` button-card verdict. No-op for anything but a
    genuinely pending concurrent future (test doubles with a bare ``.result()`` are inert)."""
    done = getattr(fut, "done", None)
    if not (callable(done) and not done()):
        return
    ctx = runner._ctx
    thread = threading.Thread(
        target=_watch,
        args=(runner, fut, approval_data, cmd, desc, flags,
              getattr(ctx, "_status_adapter", None),
              getattr(ctx, "_status_chat_id", None),
              dict(ctx._status_thread_metadata or {}),
              getattr(ctx, "session_key", "") or ""),
        name="approval-ambiguous-watch", daemon=True)
    thread.start()
    return thread


def _watch(runner, fut, approval_data: dict, cmd: str, desc: str, flags: dict,
           adapter, chat_id: str, metadata: dict, session_key: str) -> None:
    from gateway.run import _approval_send_outcome
    from gateway.run_turn_runner_approval_settle import register_timeout_notice

    request_id = approval_data.get("request_id")
    # Park until the send reaches its final state (the adapter's own timeouts bound this;
    # a future that never resolves leaves only a parked daemon thread).
    try:
        fut.result()
    except Exception:
        pass
    try:
        outcome = _approval_send_outcome(fut, timeout=0)  # resolved: sent / failed / declined
    except Exception:
        logger.debug("Approval ambiguous-watch: send outcome unreadable", exc_info=True)
        return
    if outcome == "sent":
        # The card landed late — the ONE thing the ambiguous path must never skip: without a
        # settle hook a timed-out card keeps live buttons and nobody learns the command did not run.
        register_timeout_notice(runner, approval_data, command=cmd,
                                card_message_id=getattr(_result_of(fut), "message_id", None))
        logger.info("Approval card delivered late (request_id=%s) — timeout notice armed", request_id)
        return
    if outcome == "declined":
        return  # egress refused this destination: a text re-send would leak it into that chat
    # Definitive failure (or the send itself died): fall back to text unless a late tap already
    # resolved the prompt — the central queue (backed by the durable ledger) is the authority.
    if not _still_pending(request_id, session_key):
        return
    post = _post_text_fallback(runner, adapter, chat_id, cmd, desc, flags, metadata, approval_data)
    if post:
        logger.info("Approval card undeliverable; text /approve fallback posted (request_id=%s)", request_id)


def _result_of(fut):
    try:
        return fut.result(timeout=0)
    except Exception:
        return None


def _still_pending(request_id: Any, session_key: str) -> bool:
    from tools.approval import get_pending_gateway_approval

    try:
        pending = get_pending_gateway_approval(session_key)
    except Exception:
        logger.debug("Approval ambiguous-watch: pending check failed; assuming still armed", exc_info=True)
        return True  # fail toward the fallback: a duplicate prompt is cheaper than an invisible one
    if not pending:
        return False
    pending_id = pending.get("request_id")
    return (not request_id) or (not pending_id) or pending_id == request_id


def _post_text_fallback(runner, adapter, chat_id, cmd, desc, flags, metadata, approval_data) -> bool:
    from gateway.run import _interim_metadata
    from gateway.run_turn_runner_approval_settle import register_timeout_notice
    from gateway.run_turn_runner_approval_text import approval_text_prompt

    if adapter is None or chat_id is None:
        return False
    try:
        msg = approval_text_prompt(adapter, chat_id, runner._ctx.session_key or "",
                                   approval_data.get("request_id"), cmd, desc,
                                   getattr(adapter, "typed_command_prefix", "/"), **flags)
        fut = runner._schedule(
            adapter.send(chat_id, msg, metadata=_interim_metadata(
                {**metadata, "is_approval_prompt": True})),
            "Approval fallback text-send scheduling error")
        if fut is None:
            return False
        res = fut.result(timeout=15)
        if not getattr(res, "success", False):
            logger.warning("Approval text fallback send failed (success=%s, error=%s, request_id=%s)",
                           getattr(res, "success", None), getattr(res, "error", None),
                           approval_data.get("request_id"))
            return False
        # The text prompt has no buttons to drop: the timeout notice arrives as a new message.
        register_timeout_notice(runner, approval_data, command=cmd, card_message_id=None)
        return True
    except Exception:
        logger.debug("Approval ambiguous-watch: text fallback failed", exc_info=True)
        return False
