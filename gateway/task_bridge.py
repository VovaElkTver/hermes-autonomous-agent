"""Bridge: inbound owner message → tasks_kernel queue advance.

Deterministic, fail-open, zero-LLM. Called from ``BasePlatformAdapter._enqueue_text_event``
for every admitted owner text message; a no-op unless some ``tk_task`` row is open
for this chat. Errors are logged, never raised into the messaging path.
"""
from __future__ import annotations

import logging
import os

logger = logging.getLogger(__name__)

_flag_cache: bool | None = None


def _enabled() -> bool:
    global _flag_cache
    if _flag_cache is None:
        _flag_cache = os.environ.get("HERMES_TASK_BRIDGE", "1") != "0"
    return _flag_cache


def on_owner_message(chat_id: str) -> dict | None:
    """Advance the walk-list head for ``chat_id`` if one is parked on the owner."""
    if not _enabled() or not chat_id:
        return None
    try:
        from tasks_kernel import waiting_head_for_bridge, on_owner_message as advance

        if not waiting_head_for_bridge(chat_id):
            return None
        delivered = advance(chat_id)
        if delivered is not None:
            logger.info("task bridge: chat %s advanced to %s (%s)",
                        chat_id, delivered.get("task_id"), delivered.get("title"))
        return delivered
    except Exception as exc:  # fail-open: never break messaging
        logger.warning("task bridge skipped (chat %s): %s", chat_id, exc)
        return None
