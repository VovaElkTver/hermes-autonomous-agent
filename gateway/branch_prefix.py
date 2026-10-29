"""Multi-console branch labels: every interface is an active console.

When a session is a fork child, its outbound responses carry ``⎇ <fork-name>: ``;
when a session has active forks of its own, it speaks as ``⎇ main: ``.  A session
with no branch relationship at all (and no active children) is unlabeled — the
single-console default stays silent, exactly like a plain shell prompt.

The label is resolved from ``session_branches`` at send time, so it follows the
branch lifecycle with no bookkeeping: /join or /fork --discard closes a branch
and the next turn is already unlabeled (or main loses ``main`` when the last
active child is gone).  Applied in gateway/run_turn_persistence.finalize so it
covers every surface the turn result reaches — Matrix, Telegram, CLI, TUI.
"""
from __future__ import annotations

import inspect
from typing import Optional

BRANCH_GLYPH = "\u23e7"  # ⎇ — git's branch glyph
MAIN_LABEL = "main"


async def _maybe_await(result):
    return await result if inspect.isawaitable(result) else result


def format_branch_prefix(name: str) -> str:
    return f"{BRANCH_GLYPH} {name}: "


async def resolve_branch_label(session_db, session_id) -> Optional[str]:
    """The console name ``session_id`` speaks as, or None when unlabeled.

    Fork child wins over parent identity: a branch that itself forked speaks
    with its own branch name, never as ``main``.
    """
    if not session_db or not session_id:
        return None
    branch = await _maybe_await(session_db.get_branch_for_child(session_id))
    if branch and branch.get("status") == "active":
        title = await _maybe_await(session_db.get_session_title(session_id))
        name = (title or "").strip()
        return name or str(branch.get("branch_id") or "") or MAIN_LABEL
    has_children = await _maybe_await(session_db.has_active_branches(session_id))
    return MAIN_LABEL if has_children else None


async def decorate_response(session_db, session_id, response: str) -> str:
    """Prefix ``response`` with this session's console label (idempotent)."""
    if not response:
        return response
    label = await resolve_branch_label(session_db, session_id)
    if not label:
        return response
    prefix = format_branch_prefix(label)
    if response.startswith(prefix):
        return response
    return prefix + response
