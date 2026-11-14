"""Excision lock: the donor scheduled-agent prompt surface must stay gone.

Pre-excision behavior (community report: Slack-delivered scheduled jobs and
their attachments): platform "cron" stacked the delivery channel's hint onto
the scheduled hint via HERMES_SCHEDULED_AUTO_DELIVER_PLATFORM. The 10.11 cron
excision removed the scheduler — no launcher in the fork sets platform="cron"
or publishes the auto-deliver vars. These tests pin the absence so a donor
sync cannot silently resurrect the surface; deferred continuation
(task_queue / controller.followups) wakes sessions on the carrier platform.
"""

import types

import pytest

from agent.prompt_builder import PLATFORM_HINTS
from agent.system_prompt import platform_hint
from gateway.platform_registry import platform_registry
from gateway.session_context import _VAR_MAP
from hermes_cli.plugins import discover_plugins


def _agent(platform, overrides=None):
    return types.SimpleNamespace(platform=platform, _platform_hint_overrides=overrides or {})


@pytest.fixture
def deliver_to(monkeypatch):
    var = _VAR_MAP["HERMES_SCHEDULED_AUTO_DELIVER_PLATFORM"]
    token = var.set("slack")
    monkeypatch.delenv("HERMES_DESKTOP_TERMINAL", raising=False)
    discover_plugins()  # messaging hints ship on each plugin's PlatformEntry
    yield
    var.reset(token)


def test_no_cron_hint_and_no_delivery_stacking(deliver_to):
    assert "cron" not in PLATFORM_HINTS
    # Even with the legacy auto-deliver var published, a "cron" platform agent
    # gets no built-in hint (registry override below still resolves normally).
    assert platform_hint(_agent("cron")) == ""


def test_carrier_platform_hints_untouched(deliver_to):
    assert platform_hint(_agent("telegram")) == platform_registry.get("telegram").platform_hint
