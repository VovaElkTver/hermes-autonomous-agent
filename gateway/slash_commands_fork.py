"""Gateway slash-command handlers for lazy conversation forks (``/fork``, ``/join``).

Design: ~/hermes-scheduler-study/fork-join-design.md + fork-join-implementation.md.
A fork is a *lazy* branch: the child session shares the parent transcript by
reference (session_branches row pins ``base_message_uid``) instead of copying
history the way ``/branch`` does.  A join merges a finished branch back: the
default strategy appends one durable work-note (a compressed summary of the
branch side-conversation) to this session; ``--append`` moves raw rows when the
parent tail after the fork point is still uncompressed; ``--discard`` drops it.

Handler names follow the gateway convention ``_handle_<name>_command``
(gateway/run_busy.py:907).
"""

from __future__ import annotations

import contextlib
import json
import logging
import uuid
from datetime import datetime
from typing import Any

from agent.i18n import t
from gateway.branch_prefix import branch_display_name

logger = logging.getLogger(__name__)

JOIN_NOTE_MAX_CHARS = 4800  # ~1200 tokens of summary text, hard ceiling


def parse_join_args(raw: str):
    """``[branch-id] [--discard|--append]`` -> (positional, flags)."""
    args = (raw or "").split()
    flags = {a for a in args if a.startswith("--")}
    positional = [a for a in args if not a.startswith("--")]
    return positional, flags


class GatewayForkCommandsMixin:
    """Mixed into GatewaySlashCommandsMixin consumers; needs session plumbing."""

    # ------------------------------------------------------------------ /fork

    async def _handle_fork_command(self, event) -> str:
        """`/fork [name]` - lazy branch that stays in this room/console."""
        return await self._fork_impl(event, bound=False)

    async def _handle_forkb_command(self, event) -> str:
        """`/forkb [name]` - lazy branch BOUND to its own chat thread.

        Heavy form for write-dialog-like flows and long side-conversations:
        the branch gets a dedicated Matrix/Telegram thread (Вл 30.10: doing
        thread-binding for every casual /fork is overkill).
        """
        return await self._fork_impl(event, bound=True)

    async def _bound_branch_lock(self, session_id: str) -> dict | None:
        """The ACTIVE /forkb branch the console on ``session_id`` stands in, if any.

        Sticky-lock predicate (Вл 31.10): while a bound branch is open, the thread's route is
        its own — resume/new/in-place-fork must not repoint it. Only /return and /leave close it;
        /forkb from inside spawns a NEW thread route and never touches this one."""
        if not self._session_db or not session_id:  # type: ignore[attr-defined]
            return None
        try:
            return await self._session_db.get_bound_active_branch(str(session_id))  # type: ignore[attr-defined]
        except Exception:
            return None

    def _bound_locked_reply(self, action: str, branch: dict) -> str:
        return t("gateway.branch.bound_locked", action=action,
                 name=branch_display_name(branch))

    async def _fork_impl(self, event, bound: bool) -> str:
        if not self._session_db:
            return self._session_db_unavailable_reply()
        source = event.source
        current_entry = await self.async_session_store.get_or_create_session(source)
        parent_session_id = current_entry.session_id
        last_uid = await self._session_db.get_last_message_uid(parent_session_id)
        if not last_uid:
            return t("gateway.fork.no_conversation")
        raw_title = (event.get_command_args() or "").strip()
        fork_title = raw_title or await self._session_db.get_next_title_in_lineage(
            await self._session_db.get_session_title(parent_session_id) or "fork")

        session_key = self._session_key_for_source(source)
        new_session_id = f"{datetime.now().strftime('%Y%m%d_%H%M%S')}_{uuid.uuid4().hex[:6]}"
        dest_source = await self._branch_open_thread(source, fork_title) if bound else None
        in_place = dest_source is None
        if in_place:
            dest_source = source
            # Sticky /forkb branch (Вл 31.10): an in-place fork (plain /fork, or /forkb whose
            # thread could not be created) repoints THIS console — while a bound branch is open
            # the thread keeps its stream; close it with /return or /leave.
            lock = await self._bound_branch_lock(parent_session_id)
            if lock is not None:
                return self._bound_locked_reply("/forkb" if bound else "/fork", lock)
        dest_key = session_key if in_place else self._session_key_for_source(dest_source)
        origin_json = None
        with contextlib.suppress(Exception):
            _origin = (current_entry.origin or source) if in_place else dest_source
            origin_json = json.dumps(_origin.to_dict())
        try:
            parent = await self._session_db.get_session(parent_session_id)
            await self._session_db.create_session(
                session_id=new_session_id,
                source=source.platform.value if source.platform else "gateway",
                model=(self.config.get("model", {}) or {}).get("default") if isinstance(self.config, dict) else None,
                model_config={"_forked_from": parent_session_id},
                parent_session_id=parent_session_id, user_id=dest_source.user_id,
                session_key=dest_key, chat_id=dest_source.chat_id,
                chat_type=dest_source.chat_type, thread_id=dest_source.thread_id,
                origin_json=origin_json,
                display_name=current_entry.display_name,
                system_prompt=(parent or {}).get("system_prompt") or None)
        except Exception as e:
            logger.error("Failed to create fork session: %s", e)
            return t("gateway.fork.create_failed", error=e)

        branch_id = await self._session_db.new_branch_id()
        await self._session_db.create_branch_row(
            branch_id, parent_session_id, new_session_id, last_uid,
            title=fork_title)
        with contextlib.suppress(Exception):
            await self._session_db.set_session_title(new_session_id, fork_title)
        if not in_place:
            with contextlib.suppress(Exception):
                await self.async_session_store.get_or_create_session(dest_source)
        new_entry = await self.async_session_store.switch_session(dest_key, new_session_id)
        if not new_entry:
            return t("gateway.branch.switch_failed")
        self._clear_session_boundary_security_state(dest_key)
        self._evict_cached_agent(dest_key)
        reply = t("gateway.fork.created", branch=branch_id, title=fork_title,
                  parent=parent_session_id, new=new_session_id)
        if not in_place and source.platform and "thread" not in reply:
            pass  # thread ref added by _branch_open_thread side effects on threaded platforms
        return reply

    # ------------------------------------------------------------------ /join

    async def _handle_join_command(self, event) -> str:
        """/join [name|branch-id] [--discard|--append] - merge a forked branch back."""
        if not self._session_db:
            return self._session_db_unavailable_reply()
        positional, flags = parse_join_args(event.get_command_args())
        here_entry = await self.async_session_store.get_or_create_session(event.source)
        session_id = here_entry.session_id

        branches = await self._session_db.list_active_branches(session_id)
        if not branches:
            return t("gateway.join.none_active")
        target = None
        if positional:
            # Names first (the fork UI speaks names, Вл 30.10); hex id is the
            # fallback identity.  Exact title match, then branch-id (with or
            # without the leading 'b').
            arg = positional[0]
            target = next((b for b in branches
                           if branch_display_name(b).lower() == arg.lower()
                           and branch_display_name(b) != b["branch_id"]), None)
            if target is None:
                wanted = arg.lstrip("b")
                target = next((b for b in branches if b["branch_id"].lstrip("b") == wanted), None)
            if target is None:
                # allow a name that collides with a raw hex-style title
                target = next((b for b in branches
                               if branch_display_name(b).lower() == arg.lower()), None)
            if target is None:
                return t("gateway.join.not_found", arg=arg)
        elif len(branches) > 1:
            listing = ", ".join(branch_display_name(b) for b in branches)
            return t("gateway.join.ambiguous", branches=listing)
        else:
            target = branches[0]

        strategy = ("append" if "--append" in flags
                    else "discard" if "--discard" in flags else "note")
        return await self.join_branch(session_id, target, strategy)

    async def join_branch(self, parent_session_id: str, branch: dict, strategy: str) -> str:
        """Merge one active ``branch`` (row dict) into ``parent_session_id``.

        Shared by the /join command and agent tools.
        """
        child_id = branch["child_session_id"]
        name = branch_display_name(branch)
        if strategy == "append":
            moved = await self._session_db.move_branch_rows_to_parent(branch)
            if not moved:
                return t("gateway.join.append_unavailable", branch=name)
        elif strategy == "note":
            summary_text = await self._summarize_branch(child_id)
            note = (f"[work-note from branch {name}] {summary_text}")[:JOIN_NOTE_MAX_CHARS]
            await self._session_db.append_messages_batch(parent_session_id, [
                dict(role="user", content=note)])
        # discard falls through: just close the branch
        await self._session_db.set_branch_status(branch["branch_id"], "joined", join_strategy=strategy)
        child_key = await self._session_db.get_session_key(child_id)
        if child_key:
            self._evict_cached_agent(child_key)
        return t("gateway.join.joined", branch=name, strategy=strategy)

    async def _return_or_leave(self, event, close: bool) -> str:
        """Shared body of /return (close=True) and /leave (close=False).

        Finds the active branch whose CHILD is the session this chat stands on
        (walking compression-rotated ids via the parent chain), then merges it
        into its parent (join strategies as /join) and switches the console
        back to the parent — or just switches back, leaving the branch open.
        """
        source = event.source
        entry = await self.async_session_store.get_or_create_session(source)
        session_id = entry.session_id

        # walk up our own rotation chain: current id -> (post-compression) fork child id
        branch = None
        visited, cur = set(), session_id
        while cur and cur not in visited:
            visited.add(cur)
            row_branch = await self._session_db.get_branch_for_child(cur)
            if row_branch is not None:
                # first branch row on our chain owns this console; if it is
                # already closed there is nothing to walk back to
                if row_branch.get("status") == "active":
                    branch = row_branch
                break
            row = await self._session_db.get_session(cur)
            nxt = (row or {}).get("parent_session_id")
            if not nxt or nxt == cur:
                break
            cur = nxt
        if branch is None:
            return t("gateway.leave.not_in_branch")

        name = branch_display_name(branch)
        parent_id = branch["parent_session_id"]
        result = None
        if close:
            _pos, flags = parse_join_args(event.get_command_args())
            strategy = ("append" if "--append" in flags
                        else "discard" if "--discard" in flags else "note")
            result = await self.join_branch(parent_id, branch, strategy)

        # move this chat's console back to the parent
        session_key = self._session_key_for_source(source)
        new_entry = await self.async_session_store.switch_session(session_key, parent_id)
        if not new_entry:
            return t("gateway.branch.switch_failed")
        self._clear_session_boundary_security_state(session_key)
        self._evict_cached_agent(session_key)
        if result is not None:
            return result
        return t("gateway.leave.left", branch=name)

    async def _handle_return_command(self, event) -> str:
        """/return [--discard|--append] — close the branch I stand in, back to parent."""
        if not self._session_db:
            return self._session_db_unavailable_reply()
        return await self._return_or_leave(event, close=True)

    async def _handle_leave_command(self, event) -> str:
        """/leave — step out of the branch I stand in; it stays open."""
        if not self._session_db:
            return self._session_db_unavailable_reply()
        return await self._return_or_leave(event, close=False)

    async def _summarize_branch(self, child_id: str) -> str:
        """Compress the branch transcript into a short summary for the work-note.

        ``ContextCompressor`` takes the canonical ``(model, base_url=..., api_key=...)``
        construction (gateway/session_mutation_compress.py) — an earlier draft passed a
        ``config=`` kwarg it never had, so the note path silently fell to the tail
        fallback on every join (stage001 п.5). ``compress()`` returns the compressed
        MESSAGE LIST, not a string: the note is the summary block it embeds; short
        transcripts (below the compress window) keep the raw tail.
        """
        history = await self.async_session_store.load_transcript(child_id)
        focus = await self._session_db.get_session_title(child_id) or "branch discussion"
        gw: Any = self
        try:
            from agent.context_compressor import ContextCompressor, SUMMARY_PREFIX
            model, base_url, api_key, provider = "", "", "", ""
            resolver = getattr(gw, "_hmwa_hygiene_settings", None)
            if callable(resolver):
                with contextlib.suppress(Exception):
                    session_key = await gw._session_db.get_session_key(child_id) or ""
                    hs = await resolver(None, session_key)
                    model = hs.model or ""
                    base_url, api_key, provider = hs.base_url or "", hs.api_key or "", hs.provider or ""
            if not model and isinstance(gw.config, dict):
                model = str(((gw.config.get("model") or {}).get("default")) or "")
            compressor = ContextCompressor(
                model, base_url=base_url, api_key=api_key, provider=provider, quiet_mode=True)
            result = compressor.compress(history, focus_topic=focus, force=True)
            text = ""
            if isinstance(result, str):
                text = result
            elif isinstance(result, list):
                for m in result:
                    content = str(m.get("content") or "") if isinstance(m, dict) else ""
                    if SUMMARY_PREFIX in content:
                        text = content
                        break
            if not text:
                text = "\n".join(str(m.get("content"))[:300] for m in history[-4:])
        except Exception as e:
            logger.warning("fork-join: compressor failed (%s); using tail fallback", e)
            text = "\n".join(str(m.get("content"))[:300] for m in history[-4:])
        return text or "(empty branch)"
