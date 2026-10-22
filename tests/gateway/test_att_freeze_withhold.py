"""/att freeze contract: the paused turn must not answer its own pause notice.

Two halves:
1. ``_busy_att_command`` marks the adapter's live-session interrupt event
   (``_hermes_att_frozen`` + set) so the delivery layer knows the operator —
   not the agent — owns the next move.
2. ``_process_message_background`` withholds the final response of an
   ATT-frozen turn even when no follow-up message is queued.

Plus: Matrix approval prompts inherit ``approvals.timeout`` instead of a
hardcoded 300s window that expired prompts before the human saw them.
"""

import asyncio
import types
from types import SimpleNamespace

import pytest

import gateway.run as gateway_run
from gateway.config import Platform, PlatformConfig
from gateway.platforms.base import BasePlatformAdapter, MessageEvent, MessageType, SendResult
from gateway.session import SessionSource, build_session_key


def _make_event(text: str) -> MessageEvent:
    source = SessionSource(
        platform=Platform.TELEGRAM, user_id="u1", chat_id="c1", user_name="tester", chat_type="dm",
    )
    return MessageEvent(text=text, source=source, message_type=MessageType.TEXT)


class _SendRecordingAdapter(BasePlatformAdapter):
    def __init__(self):
        super().__init__(PlatformConfig(enabled=True, token="***"), Platform.TELEGRAM)
        self.sent: list = []

    async def connect(self) -> bool:
        return True

    async def disconnect(self) -> None:
        return None

    async def send(self, chat_id, content, reply_to=None, metadata=None) -> SendResult:
        self.sent.append(content)
        return SendResult(success=True, message_id="sent-1")

    async def send_typing(self, chat_id, metadata=None) -> None:
        return None

    async def stop_typing(self, chat_id) -> None:
        return None

    async def get_chat_info(self, chat_id: str):
        return {"id": chat_id, "type": "dm"}


class _FakeAgent:
    def interrupt(self, message=None, **kw):
        return True


def _runner_with(agent, adapter):
    runner = object.__new__(gateway_run.GatewayRunner)
    state = types.SimpleNamespace(turn=types.SimpleNamespace(agent=agent))
    runner._peek_session_state = lambda session_key: state
    runner._delivery_adapter_for = lambda source: adapter
    return runner


class TestAttMarksFreeze:
    @pytest.mark.asyncio
    async def test_accepted_att_sets_and_marks_live_session_event(self):
        adapter = _SendRecordingAdapter()
        key = "k"
        evt = asyncio.Event()
        adapter._active_sessions[key] = evt
        runner = _runner_with(_FakeAgent(), adapter)

        await runner._busy_att_command(_make_event("/att"), key, None)

        assert evt.is_set() is True
        assert getattr(evt, "_hermes_att_frozen", False) is True

    @pytest.mark.asyncio
    async def test_missing_adapter_does_not_crash(self):
        runner = _runner_with(_FakeAgent(), None)
        result = await runner._busy_att_command(_make_event("/att"), "k", None)
        assert "continue" in result.text.lower()  # notice still delivered

    @pytest.mark.asyncio
    async def test_refused_interrupt_does_not_mark(self):
        class Refusing:
            def interrupt(self, message=None, **kw):
                return False

        adapter = _SendRecordingAdapter()
        evt = asyncio.Event()
        adapter._active_sessions["k"] = evt
        runner = _runner_with(Refusing(), adapter)

        await runner._busy_att_command(_make_event("/att"), "k", None)

        assert evt.is_set() is False
        assert getattr(evt, "_hermes_att_frozen", False) is False


class TestFrozenTurnWithheld:
    @pytest.mark.asyncio
    async def test_att_frozen_turn_final_response_not_delivered(self):
        adapter = _SendRecordingAdapter()
        event = _make_event("hi")
        key = build_session_key(event.source)
        evt = asyncio.Event()
        evt.set()
        evt._hermes_att_frozen = True
        adapter._active_sessions[key] = evt

        async def handler(_event):
            return "self-answer that must stay frozen"

        adapter._message_handler = handler
        await adapter._process_message_background(event, key)

        assert adapter.sent == []

    @pytest.mark.asyncio
    async def test_plain_set_event_without_att_still_delivers(self):
        # Legacy behavior preserved: a set interrupt event alone (no pending queue,
        # no att mark) does not withhold the response.
        adapter = _SendRecordingAdapter()
        event = _make_event("hi")
        key = build_session_key(event.source)
        evt = asyncio.Event()
        evt.set()
        adapter._active_sessions[key] = evt

        async def handler(_event):
            return "normal final answer"

        adapter._message_handler = handler
        await adapter._process_message_background(event, key)

        assert any("normal final answer" in str(c) for c in adapter.sent)


class TestApprovalTimeoutFollowsConfig:
    def test_default_timeout_reads_approvals_config(self, monkeypatch):
        from plugins.platforms.matrix import adapter as matrix_adapter

        monkeypatch.setattr("tools.approval_context._get_approval_timeout", lambda: 3600)
        assert matrix_adapter._default_approval_timeout_seconds() == 3600

    def test_default_timeout_floors_to_one(self, monkeypatch):
        from plugins.platforms.matrix import adapter as matrix_adapter

        monkeypatch.setattr("tools.approval_context._get_approval_timeout", lambda: 0)
        assert matrix_adapter._default_approval_timeout_seconds() == 1

    def test_default_timeout_survives_import_failure(self, monkeypatch):
        from plugins.platforms.matrix import adapter as matrix_adapter

        def boom():
            raise RuntimeError("no config")

        monkeypatch.setattr("tools.approval_context._get_approval_timeout", boom)
        assert matrix_adapter._default_approval_timeout_seconds() == 300
