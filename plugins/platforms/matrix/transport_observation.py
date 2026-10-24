"""Transport-observations layer for the Matrix adapter (agency-model.md §16.9,
hermes-agency-003 §7) — observer-mode records between the adapter and the
semantic journal, extracted from ``adapter.py`` to keep that module inside its
FILE_LINES cap (moved code keeps its cap).

The transport can lie to the journal (phantom-inbound: an event the agent saw
that the room does not contain — synthetic re-insertions after a tool-result
or a session restore). This layer records what the transport delivered BEFORE
semantic processing, so the reconciliation loop can re-check each observation
against the homeserver and cut off synthetic re-insertions. Flag-gated, off by
default; no single ``verified`` flag — six independent characteristics. All
failures here degrade to no-ops: the observation layer must never affect the
receive path.
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import time
from contextlib import suppress
from typing import Any, Optional

try:
    from mautrix.types import EventID, RoomID
except ImportError:
    # Import-safe stubs without mautrix (same pattern as adapter.py): tests exercise
    # the mixin's methods, and the IDs degrade to plain strings.
    EventID = RoomID = str  # type: ignore[misc,assignment]

from gateway.platforms.base import logger  # shared platform logger, same as adapter.py

__all__ = ["TransportObservationMixin"]


class TransportObservationMixin:
    """Transport-observation methods; ``MatrixAdapter`` mixes this in and calls
    the two hooks (``_init_transport_observation`` in ``__init__``,
    ``_record_transport_observation`` on the receive path,
    ``_maybe_reconcile_transport`` on the sync loop,
    ``_close_transport_observation`` on disconnect)."""

    def _init_transport_observation(self, config: Any) -> None:
        """Observer-mode attributes (agency-model.md §16.9). Off by default; a
        profile enables it with matrix.transport_observations: true (or
        MATRIX_TRANSPORT_OBSERVATIONS). It never alters the receive path, and
        every failure in it degrades to a no-op at the call site."""
        self._transport_obs: bool = self._extra_truthy(
            config, "transport_observations", "MATRIX_TRANSPORT_OBSERVATIONS", "false")
        self._transport_reconcile_seconds: float = self._coerce_float_extra(
            "transport_reconcile_seconds", 300.0, max_value=3600.0)
        self._last_transport_reconcile: float = 0.0
        self._transport_conn: Any = None  # lazily opened state.db (same opener as ctl_*)
        self._current_sync_batch: str = ""  # next_batch token of the sync being dispatched

    def _transport_db(self) -> Any:
        """Lazily open the state.db connection (the ctl_* opener: one DDL authority,
        same durability barriers). One connection per adapter instance, closed in
        disconnect()."""
        if self._transport_conn is None:
            from controller import model as _ctl_model
            self._transport_conn = _ctl_model._connect()
        return self._transport_conn

    def _close_transport_observation(self) -> None:
        """Release the observation layer's state.db connection (disconnect path)."""
        if getattr(self, "_transport_conn", None) is not None:
            with suppress(Exception):
                self._transport_conn.close()
            self._transport_conn = None

    def _record_transport_observation(self, event: Any, room_id: str, sender: str) -> None:
        """Record the raw transport observation for one observed room event.

        Runs BEFORE any semantic gating or journaling — and before the self-sender
        filter, so own events land with channel='local_echo'. ``raw_payload_ref`` is
        the sha256 of the serialized event content (content address of the raw
        payload, not the payload itself). Any failure is swallowed: an observation
        must never affect the receive path.
        """
        if not self._transport_obs:
            return
        try:
            from controller import transport as _transport
            content = getattr(event, "content", None)
            source = content if isinstance(content, dict) else (
                content.serialize() if content is not None and hasattr(content, "serialize") else {})
            if not isinstance(source, dict):
                source = {}
            relates_to = source.get("m.relates_to")
            payload_ref = hashlib.sha256(
                json.dumps(source, sort_keys=True, default=str).encode("utf-8")).hexdigest()
            _transport.record_observation(
                self._transport_db(),
                connector="matrix",
                channel="local_echo" if self._is_self_sender(sender) else "sync",
                room_id=room_id,
                event_id=str(getattr(event, "event_id", "")),
                event_type=str(getattr(event, "type", "") or ""),
                content_hash=payload_ref,
                sender=sender,
                relation=relates_to if isinstance(relates_to, dict) else {},
                account=self._user_id, device=self._device_id,
                sync_batch_id=self._current_sync_batch)
        except Exception:  # health: allow BLE001 -- observer boundary: an observation must never affect the receive path
            logger.debug("Matrix: transport-observation recording failed", exc_info=True)

    async def _maybe_reconcile_transport(self) -> None:
        """Periodic reconciliation of pending transport observations against the same
        homeserver (§16.9 stage 2, agency-003 §7: refetch is an interface check, not
        independent truth). Observer-mode: one bounded pass per interval, every failure
        logged at debug and swallowed — the sync loop must never break on diagnostics."""
        if not self._transport_obs or self._client is None:
            return
        if time.time() - self._last_transport_reconcile < self._transport_reconcile_seconds:
            return
        self._last_transport_reconcile = time.time()
        try:
            from controller import transport as _transport
            await _transport.reconcile(
                self._transport_db(), fetch_event=self._transport_fetch_event,
                account=self._user_id or None, limit=50)
        except Exception:  # health: allow BLE001 -- observer boundary: the sync loop must never break on diagnostics
            logger.debug("Matrix: transport reconciliation failed", exc_info=True)

    async def _transport_fetch_event(self, room_id: str, event_id: str) -> dict:
        """Re-fetch one room event through the room event endpoint. NEVER raises: a
        failure returns {"error_code": ...} — a failed fetch is a reconciliation RESULT
        with an error code, not a claim the event was not there (agency-003 §7)."""
        try:
            evt = await asyncio.wait_for(
                self._client.get_event(RoomID(room_id), EventID(event_id)), timeout=10.0)
        except Exception as exc:  # health: allow BLE001 -- observer boundary: ANY fetch failure becomes an error_code result
            logger.debug("Matrix: transport event refetch failed", exc_info=True)
            errcode = getattr(exc, "errcode", None)
            return {"error_code": str(errcode or type(exc).__name__)}
        content = getattr(evt, "content", None)
        content_dict: Optional[dict] = None
        if isinstance(content, dict):
            content_dict = content
        elif content is not None and hasattr(content, "serialize"):
            with suppress(Exception):
                serialized = content.serialize()
                if isinstance(serialized, dict):
                    content_dict = serialized
        # A redacted event comes back with (essentially) no content: a fact about the
        # event, not about the fetch. Room messages normally always carry content.
        return {"type": str(getattr(evt, "type", "") or ""),
                "sender": str(getattr(evt, "sender", "") or ""),
                "content": content_dict, "redacted": not content_dict}
