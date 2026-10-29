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


async def _lineage_ids(session_db, session_id, limit: int = 64):
    """``session_id`` and its compression/fork ancestors, newest first.

    Labels must survive session rotation: compaction inside a branch (or
    inside a main with live forks) mints a fresh id whose
    ``parent_session_id`` chain is the verified lineage.  Matching on the
    bare id alone silently drops the label after the first rotation.
    """
    get_session = getattr(session_db, "get_session", None)
    if get_session is None:  # fake/partial DBs in tests: exact-id only
        return [session_id]
    ids, cur, seen = [], session_id, set()
    while cur and cur not in seen and len(ids) < limit:
        ids.append(cur)
        seen.add(cur)
        try:
            row = await _maybe_await(get_session(cur))
        except Exception:
            row = None
        if isinstance(row, dict):
            cur = row.get("parent_session_id")
        else:
            cur = getattr(row, "parent_session_id", None)
    return ids


async def resolve_branch_label(session_db, session_id) -> Optional[str]:
    """The console name ``session_id`` speaks as (via its lineage), or None.

    Fork child wins over parent identity: a branch that itself forked speaks
    with its own branch name, never as ``main``.
    """
    if not session_db or not session_id:
        return None
    lineage = await _lineage_ids(session_db, session_id)
    for sid in lineage:
        branch = await _maybe_await(session_db.get_branch_for_child(sid))
        if branch and branch.get("status") == "active":
            title = await _maybe_await(
                session_db.get_session_title(branch.get("child_session_id") or sid))
            name = (title or "").strip()
            return name or str(branch.get("branch_id") or "") or MAIN_LABEL
    for sid in lineage:
        if await _maybe_await(session_db.has_active_branches(sid)):
            return MAIN_LABEL
    return None


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
