"""Matrix reply_expected verdicts (multi-bot silence storm fix).

A group-room message NOT addressed to the bot must carry reply_expected=False so the
model's bare silence marker may stand: the visible ⚠️ fallback otherwise posts into a
room full of peer bots, each of which ingests it as a fresh unaddressed message and
answers with its own fallback — an amplification loop. Addressed traffic (mention,
slash command, reply to the bot's own message) keeps the fallback armed (True), and DMs
keep None (addressed by construction).
"""
import sys
from pathlib import Path
from typing import Optional
from unittest.mock import AsyncMock, MagicMock

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from gateway.platforms.base import MessageEvent
from plugins.platforms.matrix.adapter import MatrixAdapter, PlatformConfig


def _adapter(require_mention: bool = False) -> MatrixAdapter:
    adapter = MatrixAdapter(PlatformConfig(
        enabled=True, token="syt_test",
        extra={"homeserver": "https://matrix.example.org", "user_id": "@bot:example.org",
               "require_mention": require_mention}))
    adapter._user_id = "@bot:example.org"
    adapter._resolve_room_identity = AsyncMock(return_value=MagicMock(display_name="Bots Room"))
    adapter._get_display_name = AsyncMock(return_value="PeerBot")
    adapter._background_read_receipt = MagicMock()
    adapter._threads = MagicMock(mark_async=AsyncMock())
    # Neutralize host-env leakage (MATRIX_ALLOWED_ROOMS/FREE_RESPONSE_ROOMS are
    # production env vars; a dev box that exports them must not gate unit-test rooms).
    adapter._allowed_rooms = set()
    adapter._free_rooms = set()
    return adapter


async def _event(adapter: MatrixAdapter, *, is_dm: bool, body: str, content: dict,
                 relates_to: Optional[dict] = None) -> MessageEvent:
    relates_to = relates_to or {}
    adapter._is_dm_room = AsyncMock(return_value=is_dm)
    evt = await adapter._build_inbound_event(
        room_id="!bots:example.org", sender="@peer:example.org", event_id="$e1",
        body=body, source_content=content, relates_to=relates_to)
    assert evt is not None, "message unexpectedly dropped by gating"
    return evt


@pytest.mark.asyncio
async def test_unaddressed_group_message_allows_silence():
    adapter = _adapter()
    evt = await _event(adapter, is_dm=False, body="💾 Self-improvement review: ...",
                       content={"body": "💾 Self-improvement review: ..."})
    assert evt.reply_expected is False


@pytest.mark.asyncio
async def test_mentioned_group_message_keeps_fallback():
    adapter = _adapter()
    body = "@bot:example.org look at this"
    evt = await _event(adapter, is_dm=False, body=body,
                       content={"body": body, "m.mentions": {"user_ids": ["@bot:example.org"]}})
    assert evt.reply_expected is True


@pytest.mark.asyncio
async def test_slash_command_in_group_is_addressed():
    adapter = _adapter()
    evt = await _event(adapter, is_dm=False, body="/model glm",
                       content={"body": "/model glm"})
    assert evt.reply_expected is True


@pytest.mark.asyncio
async def test_reply_to_own_message_is_addressed():
    adapter = _adapter()
    adapter._fetch_reply_origin = AsyncMock(
        return_value=("earlier bot text", "@bot:example.org", "Bot", True))
    evt = await _event(adapter, is_dm=False, body="still waiting?",
                       content={"body": "still waiting?"},
                       relates_to={"m.in_reply_to": {"event_id": "$own"}})
    assert evt.reply_expected is True


@pytest.mark.asyncio
async def test_reply_to_someone_else_in_thread_is_unaddressed():
    adapter = _adapter()
    adapter._fetch_reply_origin = AsyncMock(
        return_value=("peer question", "@third:example.org", "Third", False))
    # the reply pill makes _extract_reply_context resolve author != self, != sender
    evt = await _event(adapter, is_dm=False, body="> <@third:example.org> peer question\n\nanswer",
                       content={"body": "> <@third:example.org> peer question\n\nanswer"},
                       relates_to={"m.in_reply_to": {"event_id": "$t"}})
    assert evt.reply_expected is False


@pytest.mark.asyncio
async def test_dm_keeps_none():
    adapter = _adapter()
    evt = await _event(adapter, is_dm=True, body="hi", content={"body": "hi"})
    assert evt.reply_expected is None
