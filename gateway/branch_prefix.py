"""Multi-console branch labels: every interface is an active console.

When a session is a fork child, its outbound responses carry ``⎇ <fork-name>: ``;
when a session has active forks of its own, it speaks as ``⎇ main: ``.  A session
with no branch relationship at all (and no active children) is unlabeled — the
single-console default stays silent, exactly like a plain shell prompt.

The label is resolved from ``session_branches`` at send time, so it follows the
branch lifecycle with no bookkeeping: /join or /fork --discard closes a branch
and the next turn is already unlabeled (or main loses ``main`` when the last
active child is gone).

Two application points, one guarantee:
* ``run_turn_persistence.finalize`` decorates the turn's final response (this is
  also what lands in the transcript), and
* ``install_outbound_labels`` wraps the adapter's send edge — the single printing
  boundary every outgoing message passes through (interim replies, progress
  bubbles, slash answers, approvals, finals) — so no dispatch point can be
  forgotten.  The room is a telegraph tape: whatever leaves for a chat carries
  the console it left from (decision "A", live audit of the tape 2026-10-30:
  only finalize-decorated finals carried the label, everything else spoke
  anonymously).  Decoration at the boundary is idempotent: content that already
  carries a ``⎇ <name>: `` prefix is never re-labelled.
"""
from __future__ import annotations

import functools
import inspect
import logging
import re
from typing import Any, Callable, Optional

logger = logging.getLogger(__name__)

BRANCH_GLYPH = "\u2387"  # ⎇ ALT KEY SYMBOL — the multi-console branch marker (not ⏧ U+23E7)  # ⎇ — git's branch glyph
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
            # Branch name is the immutable meta_json title written at fork
            # time.  The session title is display-only — the auto-titler
            # re-summarizes it from the first message, which would rename
            # the console under the user's feet.
            name = ""
            try:
                import json as _json
                name = str((_json.loads(branch.get("meta_json") or "{}"))
                           .get("title") or "").strip()
            except Exception:
                name = ""
            if not name:
                title = await _maybe_await(
                    session_db.get_session_title(branch.get("child_session_id") or sid))
                name = (title or "").strip()
            return name or str(branch.get("branch_id") or "") or MAIN_LABEL
    for sid in lineage:
        if await _maybe_await(session_db.has_active_branches(sid)):
            return MAIN_LABEL
    return None


async def resolve_console_session(session_db, current_session_id, name) -> Optional[str]:
    """Resolve a console *name* to a session id, for /resume in the multi-console model.

    ``main`` → the root of the current lineage (the console every fork hangs off);
    any other name → an active branch whose child carries that title (or branch id).
    Returns None when nothing matches — callers fall through to the normal
    id/title lookup and its not_found reply.
    """
    if not session_db or not current_session_id or not name:
        return None
    lineage = await _lineage_ids(session_db, current_session_id)
    if name == MAIN_LABEL:
        return lineage[-1] if lineage else None
    for sid in reversed(lineage):
        try:
            branches = await _maybe_await(session_db.list_active_branches(sid))
        except Exception:
            continue
        for br in branches or []:
            child = br.get("child_session_id")
            title = ""
            if child:
                try:
                    title = (await _maybe_await(session_db.get_session_title(child)) or "").strip()
                except Exception:
                    title = ""
            if title == name or str(br.get("branch_id") or "") == name:
                return child
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


# --------------------------------------------------------------------------
# Option A: the single decoration boundary at the adapter's send edge.
# --------------------------------------------------------------------------

# Content that already speaks as some console is never re-labelled — this also
# makes the boundary a no-op for finalize-decorated finals.
_ALREADY_PREFIXED = re.compile(rf"^{BRANCH_GLYPH} [^\n]{{1,120}}: ")

# Outbound text surfaces and the positional index of their ``content`` param:
#   send(chat_id, content, ...), edit_message(chat_id, message_id, content, ...),
#   send_draft(chat_id, draft_id, content, ...).
_CONTENT_ARGV = {"send": 1, "edit_message": 2, "send_draft": 2}


def _turn_session_id() -> str:
    """The session the current task is running a turn for (contextvar-bound)."""
    try:
        from gateway.session_context import get_session_env
        return (get_session_env("HERMES_SESSION_ID") or "").strip()
    except Exception:
        return ""


def _session_id_for_chat(gateway: Any, chat_id: str) -> str:
    """Fallback when no turn context exists (slash dispatch, idle notices):
    the most recently updated routing entry whose origin chat is ``chat_id``."""
    store = getattr(gateway, "session_store", None)
    if store is None or not chat_id:
        return ""
    try:
        best, best_ts = None, None
        for entry in store.list_sessions():
            origin = getattr(entry, "origin", None)
            if origin is None or str(getattr(origin, "chat_id", "")) != str(chat_id):
                continue
            session_id = str(getattr(entry, "session_id", "") or "")
            if not session_id:
                continue
            ts = getattr(entry, "updated_at", None)
            if best is None or (ts is not None and (best_ts is None or ts > best_ts)):
                best, best_ts = session_id, ts
        return best or ""
    except Exception:
        logger.debug("branch_prefix: chat fallback lookup failed", exc_info=True)
        return ""


async def label_for_outbound(gateway: Any, chat_id: str) -> Optional[str]:
    """The console label the current send should carry (None = unlabeled)."""
    session_id = _turn_session_id() or _session_id_for_chat(gateway, chat_id)
    if not session_id:
        return None
    return await resolve_branch_label(getattr(gateway, "_session_db", None), session_id)


def _make_labeled(gateway: Any, orig: Callable, content_index: int):
    """Wrap one outbound method so its ``content`` gains the console prefix."""

    async def wrapper(*args, **kwargs):
        try:
            content = kwargs.get(
                "content", args[content_index] if len(args) > content_index else None)
            if isinstance(content, str) and content.strip() \
                    and not _ALREADY_PREFIXED.match(content):
                chat_id = str(args[0]) if args else str(kwargs.get("chat_id", ""))
                label = await label_for_outbound(gateway, chat_id)
                if label:
                    decorated = format_branch_prefix(label) + content
                    if "content" in kwargs:
                        kwargs = dict(kwargs, content=decorated)
                    else:
                        args = args[:content_index] + (decorated,) + args[content_index + 1:]
        except Exception:
            logger.debug("branch_prefix: outbound decoration failed; sending as-is",
                         exc_info=True)
        return await orig(*args, **kwargs)

    wrapper = functools.wraps(orig)(wrapper)
    wrapper._branch_label_wrapped = True  # type: ignore[attr-defined]
    return wrapper


def install_outbound_labels(gateway: Any, adapter: Any) -> None:
    """Wrap the adapter's text outbound surfaces with branch labelling.

    Called by the gateway at adapter publication (the single install point), so
    every message that leaves for a chat — final, interim, progress bubble,
    slash answer, approval prompt — is decorated at the printing boundary.
    Idempotent per adapter instance; every failure mode degrades to the
    undecorated pre-A behaviour, never to a dropped send.
    """
    if adapter is None or getattr(adapter, "_branch_labels_installed", False):
        return
    for method, content_index in _CONTENT_ARGV.items():
        try:
            orig = getattr(adapter, method, None)
            if orig is None or not callable(orig) or getattr(orig, "_branch_label_wrapped", False):
                continue
            setattr(adapter, method, _make_labeled(gateway, orig, content_index))
        except Exception:
            logger.debug("branch_prefix: cannot wrap %s.%s",
                         getattr(adapter, "name", "?"), method, exc_info=True)
    try:
        adapter._branch_labels_installed = True
    except Exception:
        pass
