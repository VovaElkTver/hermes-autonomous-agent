"""PR#61: Matrix progress-bubble cleanup (delete_message via redaction).

Closes the "словесный понос" class on Matrix: progress/heartbeat bubbles are
redacted once the final answer lands, on the same mechanism Telegram already
uses (display.platforms.matrix.cleanup_progress=true).  Failed runs keep the
bubbles as breadcrumbs (gated in run_turn, not here).
"""

from __future__ import annotations

import asyncio

from gateway.platforms.base import BasePlatformAdapter
from plugins.platforms.matrix.adapter import MatrixAdapter


class _FakeClient:
    def __init__(self, fail: bool = False) -> None:
        self.redacted: list[tuple[str, str, str | None]] = []
        self.fail = fail

    async def redact(self, room_id, event_id, reason=None):
        if self.fail:
            raise RuntimeError("homeserver says no")
        self.redacted.append((str(room_id), str(event_id), reason))
        return type("Resp", (), {"body": b"{}", "event_id": str(event_id), "error_type": None})()


def _adapter(fail: bool = False) -> MatrixAdapter:
    adapter = MatrixAdapter.__new__(MatrixAdapter)
    adapter._client = _FakeClient(fail=fail)
    return adapter


class TestDeleteMessage:
    def test_delegates_to_redaction(self):
        adapter = _adapter()
        assert asyncio.run(adapter.delete_message("!room:example", "$evt123")) is True
        room, evt, _reason = adapter._client.redacted[0]
        assert (room, evt) == ("!room:example", "$evt123")

    def test_homeserver_failure_returns_false(self):
        adapter = _adapter(fail=True)
        assert asyncio.run(adapter.delete_message("!r:s", "$e")) is False

    def test_overrides_base_not_supported(self):
        # Base returns False ("no deletion API"); Matrix must override so the
        # run_turn cleanup_progress gate (hasattr delete_message) can fire.
        assert MatrixAdapter.delete_message is not BasePlatformAdapter.delete_message
