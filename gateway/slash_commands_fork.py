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
        return await self._fork_impl(event, blocking=False)

    async def _handle_forkb_command(self, event) -> str:
        """`/forkb [name]` - the BLOCKING branch (the "b").

        Variant A (Вл 13.11, ratified): while a blocking branch is open it
        holds the dialogue focus exclusively — every other branch of its
        tree stands paused (not one step of execution), and no new /forkb
        may open until `/return`/`/leave` releases the current one. The
        dedicated chat thread is a CONSEQUENCE of that total blockage.
        """
        return await self._fork_impl(event, blocking=True)

    async def _blocking_branch_lock(self, session_id: str) -> dict | None:
        """The ACTIVE /forkb branch the console on ``session_id`` stands in, if any.

        Sticky-lock predicate (Вл 31.10): while a blocking branch is open, the thread's route is
        its own — resume/new/in-place-fork must not repoint it. Under variant A
        (Вл 13.11) closing via /return or /leave is ALSO what releases tree-wide
        execution; no new /forkb opens while this row exists."""
        if not self._session_db or not session_id:  # type: ignore[attr-defined]
            return None
        try:
            return await self._session_db.get_active_blocking_branch(str(session_id))  # type: ignore[attr-defined]
        except Exception:
            return None

    async def _forkb_focus(self) -> dict | None:
        """The blocking branch that currently HOLDS the dialogue focus, if any.

        Variant A (Вл 13.11): the active blocking branch holds the focus while
        its own thread console is still parked on its child session. /return closes
        it; /leave steps out and un-parks the focus, releasing every pause."""
        if not self._session_db:  # type: ignore[attr-defined]
            return None
        try:
            holder = await self._session_db.get_any_active_blocking_branch()  # type: ignore[attr-defined]
        except Exception:
            return None
        if not holder:
            return None
        child_id = holder["child_session_id"]
        try:
            key = await self._session_db.get_session_key(child_id)  # type: ignore[attr-defined]
        except Exception:
            return None
        if key and self.session_store.peek_session_id(key) == child_id:
            return holder
        return None

    async def _forkb_pause_reply(self, session_id: str) -> str | None:
        """Turn-admission gate for variant A.

        Returns the refusal text when ``session_id`` belongs to the fork tree of
        an open blocking branch tree but is NOT its holder session — other branches
        must not advance one step of execution. Unrelated sessions get None
        (the block is tree-scoped) and so does the holder session itself."""
        focus = await self._forkb_focus()
        if focus is None:
            return None
        child_id = focus["child_session_id"]
        if session_id == child_id:
            return None
        try:
            same_tree = await self._session_db.same_fork_tree(str(session_id), child_id)  # type: ignore[attr-defined]
        except Exception:
            return None
        if not same_tree:
            return None
        return t("gateway.branch.blocked_paused",
                 name=branch_display_name(focus))

    def _blocked_reply(self, action: str, branch: dict) -> str:
        return t("gateway.branch.blocked_refused", action=action,
                 name=branch_display_name(branch))

    async def _fork_impl(self, event, blocking: bool) -> str:
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

        # Variant A (Вл 13.11): while a blocking branch holds the focus, the tree is
        # closed for new forks — /forkb anywhere, /fork from any tree session but
        # the holder's own thread (which the sticky-lock path below handles).
        focus = await self._forkb_focus()
        if focus is not None:
            inside_child = parent_session_id == focus["child_session_id"]
            if blocking:
                return t("gateway.branch.second_forkb_refused",
                         name=branch_display_name(focus))
            if not inside_child:
                same_tree = await self._session_db.same_fork_tree(
                    parent_session_id, focus["child_session_id"])
                if same_tree:
                    return self._blocked_reply("/fork", focus)

        session_key = self._session_key_for_source(source)
        new_session_id = f"{datetime.now().strftime('%Y%m%d_%H%M%S')}_{uuid.uuid4().hex[:6]}"
        dest_source = await self._branch_open_thread(source, fork_title) if blocking else None
        if blocking and dest_source is None:
            # Variant A honesty: /forkb promises a blocking branch, and thread
            # ownership is the storage marker that makes a branch detectable as
            # the focus holder — without a thread it would silently lose its
            # blocking power. Refuse loudly instead of degrading.
            return t("gateway.branch.forkb_thread_required")
        in_place = dest_source is None
        if in_place:
            dest_source = source
            # Sticky /forkb branch (Вл 31.10): an in-place fork (plain /fork, or /forkb whose
            # thread could not be created) repoints THIS console — while a blocking branch is open
            # the thread keeps its stream; close it with /return or /leave.
            lock = await self._blocking_branch_lock(parent_session_id)
            if lock is not None:
                return self._blocked_reply("/forkb" if blocking else "/fork", lock)
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

        # Variant A (Вл 13.11): with a blocking branch holding the focus, merging any
        # OTHER branch back is work of a paused branch. Joining the blocking branch
        # itself is allowed — that is one of its closers.
        focus = await self._forkb_focus()
        if focus is not None and target["branch_id"] != focus["branch_id"]:
            return t("gateway.join.blocked_paused", name=branch_display_name(focus))

        strategy = ("append" if "--append" in flags
                    else "discard" if "--discard" in flags else "note")
        return await self.join_branch(session_id, target, strategy)

    async def join_branch(self, parent_session_id: str, branch: dict, strategy: str) -> str:
        """Merge one active ``branch`` (row dict) into ``parent_session_id``.

        Shared by the /join command and agent tools.
        """
        child_id = branch["child_session_id"]
        name = branch_display_name(branch)
        note = None
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
        # INVESTIGATION-HOOK: H3 capture (docs/design/investigation-mechanism.md §3 H3) —
        # a joined branch's result lands as evidence in the investigation its probe
        # forked from (structural meta_json, never embedding); whether it confirms or
        # refutes is the model's ``assess`` call. Fail-open, deduped by branch.
        with contextlib.suppress(Exception):
            from agent import investigation_hooks
            await investigation_hooks.capture_branch_join(
                self._session_db, branch, strategy, note)
        child_key = await self._session_db.get_session_key(child_id)
        if child_key:
            self._evict_cached_agent(child_key)
        return t("gateway.join.joined", branch=name, strategy=strategy)

    async def _return_or_leave(self, event, close: bool, default_strategy: str = "note") -> str:
        """Shared body of /return (close=True) and /leave (close=False).

        ``default_strategy`` preselects the join strategy when the command line
        carries no explicit flag — /return0 rides it to force ``discard``
        (fast close, no summary LLM) without re-parsing event text.

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
                        else "discard" if "--discard" in flags else default_strategy)
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
        """`/return [--discard|--append] — close the branch I stand in, back to parent."""
        if not self._session_db:
            return self._session_db_unavailable_reply()
        return await self._return_or_leave(event, close=True)

    async def _handle_return0_command(self, event) -> str:
        """`/return0` — alias for `/return --discard`.

        The fast close: no summarizer pass (the default `/return` note strategy
        runs a full compression LLM call before the console moves — measured
        28 s on a 61-message branch, 02.11). Explicit --append/--discard on the
        line still wins, mirroring /return's flag handling.
        """
        if not self._session_db:
            return self._session_db_unavailable_reply()
        return await self._return_or_leave(event, close=True, default_strategy="discard")

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
