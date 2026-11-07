"""Bridge: inbound owner message → tasks_kernel queue advance.

v0.1 (post-demo 07.11): an ADVANCE requires an EXPLICIT acknowledgement — either
a reply to the queue's last ``[задача] …`` line or a ``/next`` command. Ordinary
discussion stays dialog: it does NOT move the cursor.

Deterministic, fail-open, zero-LLM. Called from
``BasePlatformAdapter._enqueue_text_event`` for every admitted owner text message;
a no-op unless some ``tk_task`` row is open for this chat. Errors are logged,
never raised into the messaging path.
"""
from __future__ import annotations

import logging
import os

logger = logging.getLogger(__name__)

_flag_cache: bool | None = None

#: Prefix the kernel's deliver_default() puts on every queue line. Replies must
#: quote a line starting with this marker; plain prose never matches.
LINE_MARKER = "[задача]"

#: Bare-text ack that also counts as explicit permission.
NEXT_COMMAND = "/next"


def _enabled() -> bool:
    global _flag_cache
    if _flag_cache is None:
        _flag_cache = os.environ.get("HERMES_TASK_BRIDGE", "1") != "0"
    return _flag_cache


def is_explicit_ack(text: str | None, reply_to_text: str | None) -> bool:
    """True only for: /next, or a reply quoting our own ``[задача]`` line."""
    if not text:
        return False
    stripped = text.strip()
    if stripped.startswith(NEXT_COMMAND):
        return True
    return bool(reply_to_text and reply_to_text.strip().startswith(LINE_MARKER))


def on_owner_message(chat_id: str, text: str | None = None,
                     reply_to_text: str | None = None) -> dict | None:
    """Advance the walk-list head for ``chat_id`` on an explicit owner ack."""
    if not _enabled() or not chat_id:
        return None
    if not is_explicit_ack(text, reply_to_text):
        return None  # ordinary discussion — queue untouched
    try:
        from tasks_kernel import is_held, waiting_head_for_bridge, on_owner_message as advance

        if not waiting_head_for_bridge(chat_id):
            return None
        if is_held(chat_id):
            return None  # parked: even an explicit ack does not advance
        delivered = advance(chat_id)
        if delivered is not None:
            logger.info("task bridge: chat %s advanced to %s (%s)",
                        chat_id, delivered.get("task_id"), delivered.get("title"))
        return delivered
    except Exception as exc:  # fail-open: never break messaging
        logger.warning("task bridge skipped (chat %s): %s", chat_id, exc)
        return None
