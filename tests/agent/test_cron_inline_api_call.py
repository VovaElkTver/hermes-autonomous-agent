"""Guard for #62151's surviving mechanism: the INLINE api path used by delegated
children (interrupt aborts the active client from another thread).

The gateway scheduled jobs that originally motivated #62151 were excised from
this fork on 10.11 (see test_scheduled_cron_platform_no_longer_special_cased);
the inline machinery itself stays — delegated children (#60203) still need it.

These tests pin: (1) the inline gate is no longer platform="cron", (2) the
inline call runs on the *calling* thread — no worker is spawned, and (3) the
shared dispatch closes the per-request client.
"""

import threading
from types import SimpleNamespace
from unittest.mock import MagicMock

from run_agent import AIAgent

from agent.chat_completion_helpers import (
    direct_api_call,
    interruptible_api_call,
    interruptible_streaming_api_call,
    should_use_direct_api_call,
)


def _make_agent(*, platform: str | None = "cron"):
    agent = MagicMock()
    agent.platform = platform
    agent.api_mode = "chat_completions"
    agent.provider = "openrouter"
    agent._interrupt_requested = False
    agent._consecutive_stale_streams = 0
    agent._touch_activity = MagicMock()
    agent._close_request_openai_client = MagicMock()
    return agent


def test_scheduled_cron_platform_no_longer_special_cased():
    """Excision lock (10.11): the #62151/#69734 cron gate died with the scheduler.

    No launcher sets platform="cron" anymore; delegated children keep inlining
    (pinned by the delegated-child suites). If a donor sync re-adds the gate,
    this test flags the resurrected surface, not a regression in delivery.
    """
    for platform in ("cron", "cli", "telegram", None):
        assert should_use_direct_api_call(_make_agent(platform=platform)) is False
    codex = _make_agent(platform="cron")
    codex.api_mode = "codex_responses"
    assert should_use_direct_api_call(codex) is False
    moa = _make_agent(platform="cron")
    moa.provider = "moa"
    assert should_use_direct_api_call(moa) is False






def test_direct_api_call_interrupt_aborts_active_client_and_raises():
    """Scheduled's outer watchdog interrupts from another thread while inline."""
    agent = _make_agent()
    client_ready = threading.Event()
    release_request = threading.Event()
    fake_client = MagicMock()

    def _create(**_kwargs):
        client_ready.set()
        return fake_client

    def _request(**_kwargs):
        assert release_request.wait(timeout=2)
        raise RuntimeError("socket closed")

    fake_client.chat.completions.create.side_effect = _request
    agent._create_request_openai_client.side_effect = _create
    result = {}

    def _run():
        try:
            direct_api_call(agent, {"model": "m", "messages": []})
        except Exception as exc:
            result["exception"] = exc

    worker = threading.Thread(target=_run)
    worker.start()
    assert client_ready.wait(timeout=1)
    assert callable(agent._active_request_abort)
    AIAgent.interrupt(agent, "scheduled timeout")
    agent._abort_request_openai_client.assert_called_once_with(
        fake_client, reason="interrupt_abort"
    )
    release_request.set()
    worker.join(timeout=2)
    assert not worker.is_alive()
    assert isinstance(result.get("exception"), InterruptedError)
    assert agent._active_request_abort is None


