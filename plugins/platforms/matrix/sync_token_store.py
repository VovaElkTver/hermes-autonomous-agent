"""Cross-restart persistence of the Matrix sync token (gateway-deafness report R3, 14.11).

``connect()`` rebuilt a fresh ``MemorySyncStore`` every time, so the ``next_batch`` token
died with the process. The first sync after a restart was a full initial sync whose
timeline chunk was then discarded by the startup grace filter (5s), so every message that
arrived during the outage window stayed permanently unread: the rtx5090 deafness incident
left ~41 minutes of DMs sitting in rooms the bot "reconnected" to and never replayed.

This mixin persists the token (with the timestamp of the sync that produced it) next to
the crypto store, resumes the first sync from it when it is fresh, and lowers the startup
grace floor to the persisted timestamp so the outage-window events survive the grace
filter. The token is stored AFTER a batch's handlers ran (at-least-once): a crash
mid-batch re-delivers that one batch on next boot — the event-id dedup only spans one
process, so the recovery price is a duplicate answer for at most a handful of messages,
never a silently lost one. A token the homeserver rejects (``M_UNKNOWN_TOKEN`` — the
server restarted and expired its sync tokens) falls back to today's full-sync behaviour.
All I/O is fail-soft: a corrupt, missing or unwritable token file changes nothing except
the loss of replay.

Extracted from ``adapter.py`` per the FILE_LINES doctrine; the adapter keeps only the
one-line hooks.
"""
from __future__ import annotations

import json
import os
import time
from contextlib import suppress
from pathlib import Path
from typing import Any, Optional

from gateway.platforms.base import logger  # shared platform logger, same as adapter.py

__all__ = ["SyncTokenStoreMixin"]

_TOKEN_FILE = "sync_token.json"
# Grace margin kept on resume: a token saved between two syncs still lets events
# _STARTUP_GRACE_SECONDS-older than the save point through (same margin as the live check).
_GRACE_MARGIN_SECONDS = 5.0


class SyncTokenStoreMixin:
    """Sync-token persistence methods; ``MatrixAdapter`` mixes this in and calls the hooks:
    ``_init_sync_token_state`` in ``__init__``, ``_prepare_sync_resume`` in ``connect()``
    before the initial sync, ``_persist_batch``/``_clear_persisted_batch`` in
    ``_absorb_sync``, ``_event_older_than_grace`` on the receive path."""

    # Class-level defaults so object.__new__-built test instances keep working.
    _grace_floor_ts: float = 0.0
    _sync_resumed: bool = False

    def _init_sync_token_state(self) -> None:
        """Read the persistence knob: ``matrix.sync_token_max_age_seconds`` (default 24h,
        0 disables the feature and reverts to the pre-R3 full-sync behaviour)."""
        self._sync_token_max_age: float = self._coerce_float_extra(
            "sync_token_max_age_seconds", 86400.0, max_value=604800.0)

    # ---------- token file I/O (fail-soft everywhere) ----------

    def _sync_token_file(self) -> Path:
        store = getattr(self, "_store_dir", None) or self._resolve_store_dir()
        return Path(store) / _TOKEN_FILE

    def _load_persisted_batch(self) -> Optional[tuple[str, float]]:
        """Return ``(next_batch, saved_ts)`` when a fresh token exists, else None.

        Fresh means younger than ``sync_token_max_age_seconds``: an older token means the
        bot was down across a window whose replay is not wanted (or the homeserver has
        certainly expired it), so start a clean full sync instead.
        """
        max_age = getattr(self, "_sync_token_max_age", 86400.0)
        if max_age <= 0:
            return None
        try:
            raw = json.loads(self._sync_token_file().read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None
        if not isinstance(raw, dict):
            return None
        batch, ts = raw.get("next_batch"), raw.get("ts")
        if not isinstance(batch, str) or not batch or not isinstance(ts, (int, float)):
            return None
        age = time.time() - float(ts)
        if age < -300 or age > max_age:  # negative beyond clock jitter: distrust the file
            return None
        return batch, float(ts)

    def _persist_batch(self, batch: str) -> None:
        """Atomically store the token + its wall-clock moment (at-least-once: called after
        the batch's events were dispatched). Every failure degrades to no-token."""
        if getattr(self, "_sync_token_max_age", 86400.0) <= 0 or not batch:
            return
        path = self._sync_token_file()
        tmp = path.with_suffix(".json.tmp")
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            tmp.write_text(json.dumps({"next_batch": batch, "ts": time.time()}), encoding="utf-8")
            os.replace(tmp, path)
        except OSError as exc:
            logger.debug("Matrix: could not persist sync token: %s", exc)
            with suppress(OSError):
                tmp.unlink(missing_ok=True)

    def _clear_persisted_batch(self) -> None:
        with suppress(OSError):
            self._sync_token_file().unlink(missing_ok=True)

    # ---------- resume path ----------

    async def _prepare_sync_resume(self, sync_store: Any) -> str:
        """Seed the in-memory sync store from the persisted token before the first sync.

        Returns the ``since`` token for the initial sync ("" = full sync, the pre-R3
        behaviour). When resuming, the startup grace floor moves back to the persisted
        timestamp: the outage-window events arrive with old ``origin_server_ts`` and the
        grace filter must not drop the very backlog we are resuming for."""
        self._sync_resumed = False
        loaded = self._load_persisted_batch()
        if loaded is None:
            return ""
        batch, saved_ts = loaded
        await sync_store.put_next_batch(batch)
        self._sync_resumed = True
        self._grace_floor_ts = saved_ts - _GRACE_MARGIN_SECONDS
        logger.info(
            "Matrix: resuming sync from persisted token (offline gap %.0fs) — "
            "messages from the outage window will be replayed", time.time() - saved_ts)
        return batch

    def grace_floor_ts(self, startup_ts: float) -> float:
        """The ``origin_server_ts`` below which inbound events are backfill to be dropped."""
        if self._sync_resumed and self._grace_floor_ts:
            return self._grace_floor_ts
        return startup_ts - _GRACE_MARGIN_SECONDS

    def _event_older_than_grace(self, event_ts: float) -> bool:
        floor = self.grace_floor_ts(getattr(self, "_startup_ts", 0.0))
        return bool(event_ts) and event_ts < floor

    async def _seed_joined_rooms_after_resume(self, client: Any) -> None:
        """An incremental (``since=``) sync lists only rooms with new events, so the
        process-global ``_joined_rooms`` would stay partial until each room spoke again.
        Pull the authoritative list once; fail-soft — every consumer already tolerates a
        room missing until its events arrive."""
        getter = getattr(client, "get_joined_rooms", None)
        if getter is None:
            return
        try:
            resp = await getter()
        except Exception as exc:
            logger.debug("Matrix: joined-rooms seed after resume failed (non-fatal): %s", exc)
            return
        rooms = getattr(resp, "joined_rooms", None)
        if rooms is None and isinstance(resp, dict):
            rooms = resp.get("joined_rooms")
        if rooms:
            self._joined_rooms.update(str(r) for r in rooms)
            self._invalidate_room_identities()

    @staticmethod
    def _is_unknown_sync_token(exc: BaseException) -> bool:
        """Structured check: the homeserver says THIS ``since`` token is unknown (server
        restarted / token expired). Deliberately errcode-only — a bare 401 here could also
        mean the access token died, which must keep its permanent-stop semantics."""
        return isinstance(getattr(exc, "errcode", None), str) and exc.errcode.strip() == "M_UNKNOWN_TOKEN"
